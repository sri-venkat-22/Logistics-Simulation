"""Generate the labelled attack benchmark: realistic telemetry + ground truth for every injected attack.

    python -m sim.reality.bench                                    # 600 scheduled attacks over day 5 (00:00-24:00)
    python -m sim.reality.bench --attacks 100 --hours 2 --out /tmp/bench

Writes to --out (default data/benchmark/):
    telemetry.jsonl.gz   every published message in publish order (the trust layer's input)
    labels.jsonl         one line per attack: type, target, window, expected layer + reason, and the exact
                         messages it touched ({msg_id, occurrence, action}; occurrence 0 = dropped,
                         2 = the second copy of a replayed / duplicated msg_id)
    truth.json           hidden perturbations (lane bias, incidents, demand drift) and counts
    manifest.json        configuration, seeds, per-type counts, sha256 of the files: the benchmark is
                         reproducible from the seed, and the hashes prove a regenerated copy is identical
Scoring rule for Phase 9: a message is malicious iff (msg_id, occurrence) appears in a label with an
action other than "dropped"; blackout attacks are scored per silenced source (SLA_SILENT expected).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.macro.engine import ENGINE_VERSION
from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.paths import DATA
from sim.reality.attacks import EXPECTED, AttackInjector
from sim.reality.emulator import RealityEmulator
from sim.reality.telemetry import JsonlSink

OUT = DATA / "benchmark"
IST = ZoneInfo("Asia/Kolkata")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(out: Path, attacks: int = 600, day: int = 5, from_hour: float = 0.0, hours: float = 24.0, seed: int = 7,
          world_seed: int = 1042, gps_period_s: float = 5.0, micro: bool = True, start: str = "2026-10-15T00:00") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    t_start = datetime.fromisoformat(start).replace(tzinfo=IST)
    t0_h = day * 24 + from_hour
    inj = AttackInjector.random_campaign(attacks, hours, seed=seed, t0_h=t0_h)
    tel = out / "telemetry.jsonl.gz"
    sink = JsonlSink(tel)
    wall0 = time.perf_counter()
    mp = MicroProcess(seed=world_seed, background=False, fcd_every=1).start() if micro and CFG.exists() else None
    try:
        em = RealityEmulator(t_start, seed=world_seed, micro=mp, sink=sink, attacks=inj,
                             national_gps_period_s=gps_period_s, telemetry_from_h=t0_h)
        em.run_until(t0_h + hours)
        em.close()
    finally:
        if mp:
            mp.close()
    wall = time.perf_counter() - wall0
    truth = em.truth()
    labels = truth.pop("attacks")
    with (out / "labels.jsonl").open("w") as f:
        for lab in labels:
            f.write(json.dumps(lab, allow_nan=True) + "\n")
    (out / "truth.json").write_text(json.dumps(truth, indent=1) + "\n")
    done = [l for l in labels if l["status"] == "done"]
    by_type = {t: {"scheduled": sum(1 for l in labels if l["type"] == t), "done": sum(1 for l in done if l["type"] == t),
                   "msgs": sum(l["n_msgs"] for l in done if l["type"] == t), "expected_layer": EXPECTED[t][0],
                   "expected_reason": EXPECTED[t][1]} for t in EXPECTED}
    malicious = sum(1 for l in done for m in l["msgs"] if m["action"] != "dropped")
    manifest = {
        "benchmark": "AEGIS red-team telemetry benchmark", "engine": ENGINE_VERSION,
        "generated": datetime.now(IST).isoformat(timespec="seconds"), "wall_s": round(wall, 1),
        "config": {"attacks": attacks, "attack_seed": seed, "world_seed": world_seed, "start": t_start.isoformat(),
                   "window_from": em.ts(t0_h), "window_to": em.ts(t0_h + hours), "hours": hours,
                   "national_gps_period_s": gps_period_s, "city_gps_period_s": 1.0 if mp else None,
                   "micro_twin": mp is not None, **em.config()},
        "messages": {"published": sink.count, "malicious": malicious, "clean": sink.count - malicious,
                     "dropped_by_blackout": em.counts["dropped"], "by_kind": em.counts["by_kind"]},
        "attacks": {"scheduled": len(labels), "done": len(done), "skipped": sum(1 for l in labels if l["status"] == "skipped"),
                    "by_type": by_type},
        "files": {"telemetry.jsonl.gz": sha256(tel), "labels.jsonl": sha256(out / "labels.jsonl"),
                  "truth.json": sha256(out / "truth.json")},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.reality.bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--attacks", type=int, default=600, help="attacks scheduled (some find no target and are skipped)")
    ap.add_argument("--day", type=int, default=5, help="simulated day of the window (warm-up before it is silent)")
    ap.add_argument("--from-hour", type=float, default=0.0)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--seed", type=int, default=7, help="attack campaign seed")
    ap.add_argument("--world-seed", type=int, default=1042, help="reality twin seed")
    ap.add_argument("--gps-period", type=float, default=5.0, help="national truck GPS period, sim-seconds (city trucks: 1 Hz)")
    ap.add_argument("--no-micro", action="store_true", help="skip the SUMO city trucks")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    m = build(a.out, a.attacks, a.day, a.from_hour, a.hours, a.seed, a.world_seed, a.gps_period, not a.no_micro)
    msg, att = m["messages"], m["attacks"]
    print(f"AEGIS red-team benchmark · {m['config']['window_from'][:16]} → {m['config']['window_to'][11:16]} · {m['wall_s']} s")
    print(f"  messages {msg['published']:,} published ({msg['malicious']:,} malicious, {msg['clean']:,} clean) · "
          f"{msg['dropped_by_blackout']:,} withheld by blackouts · by kind {msg['by_kind']}")
    print(f"  attacks {att['done']} injected / {att['scheduled']} scheduled ({att['skipped']} found no target)")
    print(f"  {'type':<17}{'done':>6}{'msgs':>9}   expected")
    for t, v in att["by_type"].items():
        print(f"  {t:<17}{v['done']:>6}{v['msgs']:>9}   {v['expected_layer']} {v['expected_reason']}")
    print(f"  written to {a.out}/ (telemetry sha256 {m['files']['telemetry.jsonl.gz'][:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Score the 9-layer trust pipeline on the labelled red-team benchmark (data/benchmark/), offline.

    python -m services.api.app.trust_bench                      # -> docs/trust/benchmark.json + a table
    python -m services.api.app.trust_bench --train-asn          # regenerate the clean ASN training set for L8

Every published message goes through exactly the live code path: gateway layer 1 (ingest.validate), then
TrustStage.check (L2 signature, L3 replay / duplicate / stale, L4 physics, L5-L9 in trust_layers.py) with an
in-memory "seen" set in place of Redis. The SLA monitor sweeps every 10 world-seconds (blackouts, L9), the
live twin advances with the world clock so L7 has a reference, and L7 divergences are collected.

Scoring (manifest rule): a message is malicious iff (msg_id, occurrence) appears in a label with an action
other than "dropped". Per attack: detected if any of its messages was quarantined (blackout: any silenced
source flagged SLA_SILENT inside the window; cascade_failure: a TWIN_ENVELOPE divergence on a failed node
inside the window). Time to detect = first detection - attack start (world time).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from services.api.app.config import Settings
from services.api.app.ingest import validate
from services.api.app.live_state import LiveState
from services.api.app.live_twin import LiveTwin
from services.api.app.pipeline import SlaMonitor, TrustStage
from services.api.app.trust_layers import ASN_TRAIN, asn_features, iter_benchmark
from sim.macro.network import Network
from sim.paths import DATA, ROOT
from sim.reality.schemas import CHANNEL

BENCH = DATA / "benchmark"
OUT = ROOT / "docs" / "trust" / "benchmark.json"


class _Ctx:
    """The slice of services.api.app.main.Ctx that the trust stage and SLA monitor use."""

    def __init__(self, net: Network, twin: LiveTwin):
        self.settings = Settings(db_url="", redis_url="redis://unused")
        self.net = net
        self.twin = twin
        self.live = LiveState()
        self.device_keys = None


def run(bench: Path = BENCH, limit: int | None = None, progress: bool = True, max_fp_examples: int = 25) -> dict:
    manifest = json.loads((bench / "manifest.json").read_text())
    labels = [json.loads(line) for line in (bench / "labels.jsonl").read_text().splitlines() if line.strip()]
    malicious: dict[tuple[str, int], dict] = {}
    for lab in labels:
        for m in lab["msgs"]:
            if m["action"] != "dropped":
                malicious[(m["msg_id"], m["occurrence"])] = lab
    cfg = manifest["config"]
    start = datetime.fromisoformat(cfg["start"])
    t0_h = cfg["telemetry_from_h"]
    net = Network()
    wall0 = time.perf_counter()
    twin = LiveTwin(net, start, factor=0, seed=42, warmup_h=t0_h)
    ctx = _Ctx(net, twin)
    stage = TrustStage(ctx)  # type: ignore[arg-type]  # the slice of Ctx the stage uses
    sla = SlaMonitor(ctx, gps_sla_s=ctx.settings.source_sla_s)  # type: ignore[arg-type]
    setup_s = time.perf_counter() - wall0

    seen: Counter = Counter()
    verdicts: dict[tuple[str, int], tuple[str, str]] = {}
    first_detect: dict[str, datetime] = {}
    first_msg: dict[str, datetime] = {}
    by_code: Counter = Counter()
    fp_by_code: Counter = Counter()
    fp_examples: list[dict] = []
    silent_events: list[tuple[datetime, str]] = []
    next_sweep: datetime | None = None
    next_twin: datetime | None = None
    n = tp = fp = fn = tn = 0
    t_run = time.perf_counter()
    for m in iter_benchmark(bench / "telemetry.jsonl.gz"):
        n += 1
        if limit and n > limit:
            break
        mid = m.get("msg_id") if isinstance(m, dict) else None
        seen[mid] += 1
        key = (mid, seen[mid])
        ts = None
        kind = m.get("kind") if isinstance(m, dict) else None
        code = validate(m, CHANNEL.get(kind, "telemetry")) if kind in CHANNEL else "SCHEMA_KIND"
        if code is not None:
            v = ("L1", code)
            stage.engine.reject(m, "L1", ctx.live.world_now)
        else:
            ts = datetime.fromisoformat(m["ts"])
            v = stage.check(m, seen[mid] == 1)
            if v is None:
                stage.accept(m)
                live = ctx.live
                if live.world_now is None or ts > live.world_now:
                    live.world_now = ts
                src = live.sources.setdefault(m["source_id"], {"kind": m["kind"], "count": 0, "state": "live"})
                src["last_ts"], src["count"], src["state"] = max(ts, src.get("last_ts", ts)), src["count"] + 1, "live"
        now = ctx.live.world_now
        if now is not None:
            if next_sweep is None or now >= next_sweep:
                before = {k for k, s in ctx.live.sources.items() if s["state"] == "silent"}
                sla.sweep()
                stage.sweep()
                for k, s in ctx.live.sources.items():
                    if s["state"] == "silent" and k not in before:
                        silent_events.append((now, k))
                next_sweep = now + timedelta(seconds=10)
            if next_twin is None or now >= next_twin:
                target_h = (now - start).total_seconds() / 3600
                if target_h > twin.twin.env.now:
                    twin.advance(target_h - twin.twin.env.now)
                next_twin = now + timedelta(hours=1)
        lab = malicious.get(key)
        if lab is not None and lab["attack_id"] not in first_msg and ctx.live.world_now is not None:
            first_msg[lab["attack_id"]] = ctx.live.world_now
        if v is not None:
            verdicts[key] = v
            by_code[f"{v[0]}:{v[1]}"] += 1
            if lab is not None:
                tp += 1
                if ts is None:
                    try:
                        ts = datetime.fromisoformat(m["ts"])
                    except Exception:
                        ts = ctx.live.world_now
                det = ctx.live.world_now or ts
                if lab["attack_id"] not in first_detect or (det and det < first_detect[lab["attack_id"]]):
                    first_detect[lab["attack_id"]] = det
            else:
                fp += 1
                fp_by_code[f"{v[0]}:{v[1]}"] += 1
                if len(fp_examples) < max_fp_examples:
                    fp_examples.append({"msg_id": mid, "source": m.get("source_id"), "code": v[1], "layer": v[0],
                                        "ts": m.get("ts")})
        elif lab is not None:
            fn += 1
        else:
            tn += 1
        if progress and n % 50_000 == 0:
            print(f"  … {n:,} messages ({time.perf_counter() - t_run:.0f} s)", file=sys.stderr)
    run_s = time.perf_counter() - t_run

    # ---------------------------------------------------------------- per attack
    per_type: dict[str, dict] = defaultdict(lambda: {"attacks": 0, "detected": 0, "msgs": 0, "msgs_caught": 0,
                                                     "expected_layer_hits": 0, "ttd_s": [], "caught_by": Counter()})
    divs = list(stage.engine.divergences)
    for lab in labels:
        if lab["status"] != "done":
            continue
        t = lab["type"]
        row = per_type[t]
        row["attacks"] += 1
        a0 = datetime.fromisoformat(lab["start_ts"]) if lab["start_ts"] else None
        a1 = datetime.fromisoformat(lab["end_ts"]) if lab["end_ts"] else a0
        det = None
        if t == "blackout":
            silenced = set(lab["params"].get("silenced", []))
            hits = [ts_ for ts_, s in silent_events if s in silenced and a0 <= ts_ <= a1 + timedelta(minutes=2)]
            row["msgs"] += len(silenced)
            row["msgs_caught"] += len({s for ts_, s in silent_events if s in silenced and a0 <= ts_ <= a1 + timedelta(minutes=2)})
            det = min(hits) if hits else None
        elif t == "cascade_failure":
            nodes = {e["node"] for e in lab["events"]}
            hits = [datetime.fromisoformat(d.ts) for d in divs if d.node in nodes and a0 <= datetime.fromisoformat(d.ts) <= a1]
            det = min(hits) if hits else None
        else:
            for mm in lab["msgs"]:
                if mm["action"] == "dropped":
                    continue
                row["msgs"] += 1
                v = verdicts.get((mm["msg_id"], mm["occurrence"]))
                if v is not None:
                    row["msgs_caught"] += 1
                    row["caught_by"][f"{v[0]}:{v[1]}"] += 1
                    if (v[0], v[1]) == (lab["expected_layer"], lab["expected_reason"]):
                        row["expected_layer_hits"] += 1
            det = first_detect.get(lab["attack_id"])
            a0 = first_msg.get(lab["attack_id"], a0)  # message attacks: from the first malicious message
        if det is not None:
            row["detected"] += 1
            if a0 is not None:
                row["ttd_s"].append(max(0.0, (det - a0).total_seconds()))
    types = {}
    for t, r in sorted(per_type.items()):
        ttd = sorted(r.pop("ttd_s"))
        types[t] = {**r, "caught_by": dict(r["caught_by"].most_common()),
                    "attack_recall": round(r["detected"] / r["attacks"], 4) if r["attacks"] else None,
                    "msg_recall": round(r["msgs_caught"] / r["msgs"], 4) if r["msgs"] else None,
                    "ttd_p50_s": ttd[len(ttd) // 2] if ttd else None, "ttd_max_s": ttd[-1] if ttd else None}
    clean = tn + fp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    n_att = sum(r["attacks"] for r in types.values())
    n_det = sum(r["detected"] for r in types.values())
    div_nodes_attacked = {e["node"] for lab in labels if lab["type"] == "cascade_failure" for e in lab["events"]}
    return {
        "benchmark": manifest["benchmark"], "files": manifest["files"], "window": [cfg["window_from"], cfg["window_to"]],
        "messages": n if not limit else min(n, limit), "setup_s": round(setup_s, 1), "run_s": round(run_s, 1),
        "throughput_msgs_s": round(n / run_s) if run_s else None,
        "message_level": {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(precision, 4), "recall": round(recall, 4),
                          "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
                          "false_positive_rate": round(fp / clean, 6) if clean else 0.0},
        "attack_level": {"attacks": n_att, "detected": n_det, "recall": round(n_det / n_att, 4) if n_att else None},
        "by_type": types, "by_code": dict(by_code.most_common()), "false_positives_by_code": dict(fp_by_code.most_common()),
        "false_positive_examples": fp_examples,
        "divergences": {"total": len(divs), "on_attacked_nodes": sum(1 for d in divs if d.node in div_nodes_attacked),
                        "examples": [d.__dict__ for d in divs[:10]]},
        "reputation_lowest": stage.engine.rep.table(10),
    }


def train_asn(days: float = 12.0, seed: int = 2042) -> dict:
    """Clean ASN feature rows from an attack-free Reality Emulator run (different seed than the benchmark)."""
    from zoneinfo import ZoneInfo

    from sim.macro.engine import Twin  # noqa: F401  (import cost paid here, not at API start)
    from sim.reality.emulator import RealityEmulator
    from sim.reality.telemetry import MemorySink
    sink = MemorySink()
    em = RealityEmulator(datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Kolkata")), seed=seed, sink=sink, national_gps=False,
                         stock_period_s=86_400, port_period_s=86_400)
    em.run_until(days * 24)
    rows = []
    for m in sink.messages:
        if m["kind"] == "asn":
            p = m["payload"]
            rows.append([round(x, 5) for x in asn_features(p, em.twin.base_daily(p["dc"], p["sku"]))])
    out = {"seed": seed, "days": days, "n": len(rows), "features": ["log_qty_per_truck", "lead_h", "lanes", "log_qty_per_daily"],
           "rows": rows}
    ASN_TRAIN.write_text(json.dumps(out) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.api.app.trust_bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", type=Path, default=BENCH)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--train-asn", action="store_true")
    a = ap.parse_args(argv)
    if a.train_asn:
        r = train_asn()
        print(f"{r['n']} clean ASNs over {r['days']:g} days → {ASN_TRAIN}")
        return 0
    res = run(a.bench, a.limit)
    ml, al = res["message_level"], res["attack_level"]
    print(f"Trust pipeline on the red-team benchmark · {res['messages']:,} messages in {res['run_s']} s "
          f"({res['throughput_msgs_s']:,} msgs/s, one core)")
    print(f"  messages: precision {ml['precision']:.4f} · recall {ml['recall']:.4f} · F1 {ml['f1']:.4f} · "
          f"FPR {ml['false_positive_rate']:.2e} ({ml['fp']} false positives in {ml['fp'] + ml['tn']:,} clean)")
    print(f"  attacks: {al['detected']}/{al['attacks']} detected (recall {al['recall']:.3f})")
    print(f"  {'type':<17}{'attacks':>8}{'det.':>6}{'recall':>8}{'msgs':>8}{'caught':>8}{'TTD p50':>9}  caught by")
    for t, r in res["by_type"].items():
        ttd = f"{r['ttd_p50_s']:.0f} s" if r["ttd_p50_s"] is not None else "—"
        print(f"  {t:<17}{r['attacks']:>8}{r['detected']:>6}{r['attack_recall']:>8.3f}{r['msgs']:>8}{r['msgs_caught']:>8}{ttd:>9}  "
              f"{', '.join(f'{k} {v}' for k, v in list(r['caught_by'].items())[:3])}")
    if res["false_positives_by_code"]:
        print(f"  false positives by code: {res['false_positives_by_code']}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2, default=str) + "\n")
    print(f"  written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

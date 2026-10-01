"""Stream Reality Emulator telemetry to the ingest API (or a file), optionally at demo pace.

    python -m sim.reality.run --hours 1 --out telemetry.jsonl.gz --attacks 20
    python -m sim.reality.run --hours 2 --realtime 60 --http http://localhost:8000 --attacks 30
    python -m sim.reality.run --hours 1 --blackout        # + the data_blackout template as a labelled attack
    python -m sim.reality.run --hours 6 --realtime 60 --http http://localhost:8000 --chaos-redis redis://localhost:6379/3

The emulator is a separately seeded coupled twin (SimPy + its own SUMO worker) with hidden perturbations.
Ground truth (hidden perturbations + attack labels) goes to --truth, never to the ingest API.
--http posts HMAC-signed batches to {url}/api/v1/ingest/{telemetry|inventory|supplier} (Phase 4 ingest).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.paths import SCENARIO_TEMPLATES
from sim.reality.attacks import AttackInjector
from sim.reality.emulator import RealityEmulator
from sim.reality.telemetry import FanoutSink, HttpSink, JsonlSink, MemorySink
from sim.scenarios.dsl import Scenario

IST = ZoneInfo("Asia/Kolkata")


def chaos_poller(url: str):
    """Read new commands from the chaos.commands stream (written by the API) without blocking."""
    import orjson
    import redis
    r = redis.Redis.from_url(url)
    last = r.xinfo_stream("chaos.commands")["last-generated-id"] if r.exists("chaos.commands") else "0-0"

    def poll() -> list[dict]:
        nonlocal last
        resp = r.xread({"chaos.commands": last}, count=50)
        out = []
        for _, entries in resp or []:
            for eid, f in entries:
                last = eid
                out.append(orjson.loads(f[b"c"]))
                print(f"  ⚡ chaos command {out[-1]['id']}: {out[-1]['type']}")
        return out
    return poll


def plan_channel(url: str):
    """Applied plans from the plan.commands stream (POST /plans/{id}/apply) and the dispatch acks back: the hash
    plan.dispatch maps each dispatched shipment to its plan (the live UI highlights those trucks)."""
    import orjson
    import redis
    r = redis.Redis.from_url(url)
    last = r.xinfo_stream("plan.commands")["last-generated-id"] if r.exists("plan.commands") else "0-0"

    def poll() -> list[dict]:
        nonlocal last
        out = []
        for _, entries in r.xread({"plan.commands": last}, count=20) or []:
            for eid, f in entries:
                last = eid
                out.append(orjson.loads(f[b"c"]))
                print(f"  ✓ plan {out[-1]['id']} dispatched to the fleet: {len(out[-1].get('events', []))} actions")
        return out

    def ack(plan_id: str, acks: list[dict], record: bool = True) -> None:
        ships = {a["shipment"]: plan_id for a in acks if a.get("ok") and a.get("shipment")}
        pipe = r.pipeline()
        if ships:
            pipe.hset("plan.dispatch", mapping=ships)
        pipe.expire("plan.dispatch", 7 * 24 * 3600)
        if record:
            pipe.set(f"plan:dispatch:{plan_id}", orjson.dumps({"plan": plan_id, "acks": acks}), ex=7 * 24 * 3600)
        pipe.execute()
    return poll, ack


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.reality.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--start", default="2026-10-15T00:00")
    ap.add_argument("--warmup-h", type=float, default=0.0, help="simulate silently before publishing")
    ap.add_argument("--seed", type=int, default=1042)
    ap.add_argument("--attacks", type=int, default=0, help="random labelled attacks in the window")
    ap.add_argument("--attack-seed", type=int, default=7)
    ap.add_argument("--blackout", action="store_true", help="inject the data_blackout scenario template")
    ap.add_argument("--realtime", type=float, help="wall-seconds per simulated hour (60 = 1 sim-minute per second)")
    ap.add_argument("--gps-period", type=float, default=1.0, help="national truck GPS period, sim-seconds")
    ap.add_argument("--no-micro", action="store_true")
    ap.add_argument("--http", help="ingest API base URL")
    ap.add_argument("--out", type=Path, help="JSON lines file (.gz ok)")
    ap.add_argument("--truth", type=Path, help="write ground truth (perturbations + attack labels) here")
    ap.add_argument("--chaos-redis", help="Redis URL to take live chaos commands (stream chaos.commands, POST /chaos/inject) and applied "
                         "plans (stream plan.commands, POST /plans/{id}/apply) from")
    a = ap.parse_args(argv)

    start = datetime.fromisoformat(a.start).replace(tzinfo=IST)
    t0, t1 = a.warmup_h, a.warmup_h + a.hours
    inj = AttackInjector.random_campaign(a.attacks, a.hours, seed=a.attack_seed, t0_h=t0) if a.attacks else AttackInjector([])
    if a.blackout:
        sc = Scenario.load(SCENARIO_TEMPLATES / "data_blackout.json")
        atk = AttackInjector.from_blackout_scenario(sc, start)
        atk.start_h = t0 + min(atk.start_h, a.hours / 2)  # template start, relative to the window, kept inside it
        inj.attacks.append(atk)
        atk.id = f"A{len(inj.attacks):04d}"
    sinks = []
    http = HttpSink(a.http) if a.http else None
    if http:
        sinks.append(http)
    if a.out:
        sinks.append(JsonlSink(a.out))
    mem = MemorySink() if not sinks else None
    sink = FanoutSink(*(sinks or [mem]))
    micro = None if a.no_micro or not CFG.exists() else MicroProcess(seed=a.seed, background=False, fcd_every=1).start()
    pace = f"real time × {3600 / a.realtime:g}" if a.realtime else "as fast as possible"
    print(f"AEGIS Reality Emulator · seed {a.seed} · {a.hours:g} sim-h · {pace} · "
          f"{'SUMO city trucks + ' if micro else ''}national trucks · {len(inj.attacks)} attacks · "
          f"→ {', '.join(filter(None, [a.http, str(a.out) if a.out else None])) or 'memory (dry run)'}")
    try:
        em = RealityEmulator(start, seed=a.seed, micro=micro, sink=sink, attacks=inj, realtime_factor=a.realtime,
                             national_gps_period_s=a.gps_period, telemetry_from_h=t0, run_salt=f"{time.time_ns()}")
        if a.chaos_redis:
            em.chaos_poll = chaos_poller(a.chaos_redis)
            em.plan_poll, em.plan_ack = plan_channel(a.chaos_redis)
        wall0 = time.perf_counter()
        step = 0.05 if a.realtime else max(0.25, a.hours / 8)  # report every 3 wall-minutes at demo pace
        t = t0 if t0 > 0 else 0.0
        if t0 > 0:
            em.twin.fast_forward(t0)  # silent warm-up at full speed, then real-time pacing
        while t < t1 - 1e-9:
            t = min(t1, t + step)
            em.run_until(t)
            c = em.counts
            msg = sum(c["by_kind"].values())
            done = sum(1 for x in inj.attacks if x.status == "done")
            extra = f" · http {http.stats()}" if http else ""
            print(f"  {em.ts(t)[:16]}  {msg:>9,} msgs ({c['attacked']:,} attacked, {c['dropped']:,} withheld) · "
                  f"{done} attacks done · {msg / max(time.perf_counter() - wall0, 1e-6):,.0f} msg/s{extra}")
        em.close()
    finally:
        if micro:
            micro.close()
    if a.truth:
        a.truth.parent.mkdir(parents=True, exist_ok=True)
        a.truth.write_text(json.dumps(em.truth(), indent=1, allow_nan=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

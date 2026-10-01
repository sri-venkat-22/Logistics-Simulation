"""Run the coupled twin: SimPy macro network + SUMO Hyderabad micro-twin under one clock.

    python -m sim.coupling.run --hours 24                    # as fast as possible, bare road network
    python -m sim.coupling.run --hours 2 --realtime 60       # demo pace: 1 sim-minute per wall-second
    python -m sim.coupling.run --hours 30 --flood --background   # ORR flood, with background car traffic

Prints every macro -> SUMO hand-off (city_enter), every SUMO -> macro receipt (city_arrive, with the
clock lag) and road closures / lane-multiplier updates, then a summary.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.coupling.orchestrator import CoupledTwin
from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.network import Network
from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

IST = ZoneInfo("Asia/Kolkata")
SHOW = {"city_enter", "city_arrive", "road_closed", "road_reopened", "lane_multiplier", "city_fallback",
        "lane_mult_replaced_by_sumo", "disruption_start", "disruption_end"}


def fmt(e: dict) -> str:
    k = e["event"]
    if k == "city_enter":
        return f"→ SUMO  {e['shipment']} {e['lane']} ({e['leg']}) {e['from_hub']} → {e['to_hub']} · {len(e['trucks'])} truck(s) · expect {e['expected_h'] * 60:.0f} min"
    if k == "city_arrive":
        return f"← macro {e['shipment']} {e['lane']} arrived after {e['drive_min']:.1f} min drive · receipt lag {e['lag_s']:.0f} s"
    if k == "lane_multiplier":
        return f"⇄ lane {e['lane']} time × {e['multiplier']:.3f} ({e['source']})"
    if k in ("road_closed", "road_reopened"):
        return f"⚠ {k.replace('_', ' ')}: {', '.join(e['edges'])} · {e['rerouted']} trucks rerouted"
    if k in ("disruption_start", "disruption_end"):
        return f"⚡ {k.replace('_', ' ')}: {e['type']} @ {e['target']} ({e['kind']})"
    return f"{k}: " + json.dumps({x: y for x, y in e.items() if x not in ('t_h', 'ts', 'event')})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.coupling.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--start", default="2026-10-15T00:00")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--realtime", type=float, help="wall-seconds per simulated hour (60 = 1 sim-minute per second)")
    ap.add_argument("--sync", type=float, default=60, help="SUMO sync step, sim-seconds")
    ap.add_argument("--background", action="store_true", help="load hyderabad.sumocfg background traffic (slower)")
    ap.add_argument("--flood", action="store_true", help="add the road_flood template (closes ORR edges in SUMO)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--events", type=Path, help="write the event log as JSON lines")
    a = ap.parse_args(argv)
    if not CFG.exists():
        print("micro-twin not built: run  python -m sim.micro.build_hyderabad", file=sys.stderr)
        return 2
    start = datetime.fromisoformat(a.start).replace(tzinfo=IST)
    scenarios = [Scenario.load(SCENARIO_TEMPLATES / "road_flood.json")] if a.flood else []
    net = Network()
    twin = Twin(net, DemandModel(net), start, seed=a.seed, scenarios=scenarios, realtime_factor=a.realtime)
    if not a.quiet:
        twin.listeners.append(lambda e: e["event"] in SHOW and e.get("kind") != "random_failure"
                              and print(f"  {e['ts'][5:16].replace('T', ' ')}  {fmt(e)}"))
    pace = f"real time × {3600 / a.realtime:g}" if a.realtime else "as fast as possible"
    print(f"AEGIS coupled twin · {a.hours:g} sim-h from {a.start} IST · macro SimPy + SUMO Hyderabad "
          f"({'background traffic' if a.background else 'bare network'}) · sync {a.sync:g} s · {pace}")
    t0 = time.perf_counter()
    with MicroProcess(seed=a.seed, background=a.background) as micro:
        ct = CoupledTwin(twin, micro, sync_s=a.sync)
        print("  coupled lanes: " + ", ".join(f"{l} ({p.kind})" for l, p in sorted(ct.plans.items())))
        ct.run_until(a.hours)
        snap = ct.snapshot()
    wall = time.perf_counter() - t0
    hs = ct.handshakes
    k = snap["kpis_to_date"]
    print(f"\n  {a.hours:g} sim-h in {wall:.1f} s wall · {len(hs)} macro→SUMO→macro round trips · "
          f"max receipt lag {max((h['lag_s'] for h in hs), default=0):.0f} s (sync {a.sync:g} s) · "
          f"{ct.fallbacks} fallbacks · {len(ct.waiting)} still in the city")
    print(f"  macro: fill rate {k['fill_rate'] * 100:.1f} % · {k['shipments_active']} shipments moving · "
          f"{k['shipments_delivered']} delivered · FCD frames {ct.fcd_frames}")
    if a.events:
        with a.events.open("w") as f:
            for e in twin.events:
                f.write(json.dumps(e, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Move trucks across Hyderabad in the SUMO micro-twin (libsumo) and print what they do.

    python -m sim.micro.run                       # 30 sim-minutes, 40 freight trucks + background traffic
    python -m sim.micro.run --minutes 60 --trucks 120 --flood   # close the ORR corridor at t=10 min
    python -m sim.micro.run --gui                 # same, in sumo-gui via TraCI (debugging)

Build the network first with: python -m sim.micro.build_hyderabad
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

from sim.micro.runner import CFG, MicroTwin
from sim.paths import SCENARIO_TEMPLATES

FREIGHT_ORIGINS = ("Patancheru Plant", "Medchal DC", "Shamshabad DC", "Kothur Pharma", "Jeedimetla Industrial", "RGIA Air Cargo")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.micro.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--trucks", type=int, default=40, help="freight trucks spawned between hubs (on top of the cfg's demo trucks)")
    ap.add_argument("--every", type=int, default=300, help="print a position table every N sim-seconds")
    ap.add_argument("--flood", action="store_true", help="apply the road_flood template's SUMO edges at t=600 s")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--fcd", help="write positions (JSON lines) every 10 s to this file")
    a = ap.parse_args(argv)
    if not CFG.exists():
        print("micro-twin not built: run  python -m sim.micro.build_hyderabad", file=sys.stderr)
        return 2

    mt = MicroTwin(gui=a.gui, seed=a.seed)
    rnd = random.Random(a.seed)
    hubs = list(mt.hubs)
    origins = [h for h in FREIGHT_ORIGINS if h in mt.hubs]
    spawned = 0
    for i in range(a.trucks):
        src = rnd.choice(origins)
        dst = rnd.choice([h for h in hubs if h != src])
        spawned += mt.spawn_truck(f"SH-{24100 + i}", src, dst)
    watch = [f"SH-{24100 + i}" for i in range(min(6, a.trucks))]
    flood_edges = json.loads((SCENARIO_TEMPLATES / "road_flood.json").read_text())["params"]["sumo_edges"]

    print(f"AEGIS micro-twin · Hyderabad belt · {sum(not e.startswith(':') for e in mt.t.edge.getIDList()):,} edges · {spawned} freight trucks spawned "
          f"· {a.minutes:g} sim-min · libsumo{' (gui)' if a.gui else ''}")
    fcd = open(a.fcd, "w") if a.fcd else None
    t0 = time.perf_counter()
    end = a.minutes * 60
    peak = 0
    while mt.now() < end:
        for arr in mt.step(1):
            print(f"  t={mt.now():6.0f}s  ✓ {arr.vehicle} arrived {arr.to_hub} from {arr.from_hub} "
                  f"in {(arr.t - arr.depart) / 60:.1f} min")
        now = mt.now()
        peak = max(peak, mt.t.vehicle.getIDCount())
        if a.flood and now == 600 and flood_edges:
            mt.close_road(flood_edges)
            print(f"  t={now:6.0f}s  ⚡ ORR flooded: {len(flood_edges)} edges closed, trucks rerouted")
        if fcd and now % 10 == 0:
            fcd.write(json.dumps({"t": now, "vehicles": mt.positions(mt.active_trucks())}) + "\n")
        if now % a.every == 0:
            trucks = mt.active_trucks()
            print(f"\n  t={now:6.0f}s  vehicles {mt.t.vehicle.getIDCount():,} (trucks {len(trucks)})  "
                  f"arrived trucks {len(mt.arrivals)}")
            live = [v for v in watch if v in set(trucks)]
            for row in mt.positions(live):
                src, dst, _ = mt.trucks[row["id"]]
                print(f"    {row['id']}  {row['lat']:.5f}N {row['lon']:.5f}E  {row['speed'] * 3.6:5.1f} km/h  "
                      f"{mt.street(row['edge'])[:32]:<32}  {src} → {dst}")
    wall = time.perf_counter() - t0
    done = [x for x in mt.arrivals if x.vehicle.startswith("SH-")]
    mean_trip = sum(x.t - x.depart for x in done) / len(done) / 60 if done else float("nan")
    print(f"\n  {a.minutes:g} sim-min in {wall:.1f} s wall  →  {end / wall:.1f}x real time · peak {peak:,} vehicles · "
          f"{len(done)}/{spawned} freight trucks arrived (mean trip {mean_trip:.1f} min)")
    if fcd:
        fcd.close()
    mt.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

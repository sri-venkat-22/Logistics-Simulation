"""Real-time benchmark: 500 trucks + 3,000 cars must run faster than real time with libsumo.

    python -m sim.micro.bench                          # defaults: 500 trucks, 3000 cars, 30 sim-min
    python -m sim.micro.bench --trucks 500 --cars 3000 --minutes 30 --out sim/micro/hyderabad/benchmark.json

All vehicles depart within the first 5 minutes so they are on the network *together*; we report
the real-time factor (sim-seconds per wall-second) overall and during the busiest window, the peak
number of simultaneous vehicles, and teleports (SUMO's gridlock escape) as a congestion sanity check.
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import time

import sumolib

from sim.micro.runner import MicroTwin
from sim.paths import HYDERABAD


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.micro.bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trucks", type=int, default=500)
    ap.add_argument("--cars", type=int, default=3000)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--insert-window", type=float, default=300, help="seconds over which all vehicles depart")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", help="write results JSON here")
    a = ap.parse_args(argv)

    rnd = random.Random(a.seed)
    car_routes = [r.edges.split() for r in sumolib.xml.parse_fast(str(HYDERABAD / "cars.rou.xml"), "route", ["edges"])]
    if len(car_routes) < a.cars:
        car_routes = car_routes * (a.cars // len(car_routes) + 1)
    rnd.shuffle(car_routes)

    mt = MicroTwin(cfg=None, seed=a.seed)
    hubs = list(mt.hubs)
    t_setup = time.perf_counter()
    for i in range(a.cars):
        mt.add_vehicle(f"bgcar{i}", car_routes[i], "car", depart=round(rnd.uniform(0, a.insert_window), 1))
    spawned = 0
    for i in range(a.trucks):
        src = rnd.choice(hubs)
        spawned += mt.spawn_truck(f"bt{i}", src, rnd.choice([h for h in hubs if h != src]))
    setup_s = time.perf_counter() - t_setup

    end = a.minutes * 60
    peak, peak_t, samples = 0, 0, []
    busiest = (0.0, 0.0, 0)  # (sim s, wall s, vehicles) over the densest 5-minute window
    win_start_wall, win_start_t, win_count = time.perf_counter(), 0.0, []
    t0 = time.perf_counter()
    while mt.now() < end:
        mt.step(1)
        n = mt.t.vehicle.getIDCount()
        win_count.append(n)
        if n > peak:
            peak, peak_t = n, mt.now()
        if mt.now() % 300 == 0:
            now_wall = time.perf_counter()
            avg = sum(win_count) / len(win_count)
            samples.append({"t": mt.now(), "vehicles_avg": round(avg), "rtf": round(300 / (now_wall - win_start_wall), 1)})
            if avg > busiest[2]:
                busiest = (300.0, now_wall - win_start_wall, round(avg))
            win_start_wall, win_count = now_wall, []
    wall = time.perf_counter() - t0
    res = {
        "trucks_requested": a.trucks, "trucks_spawned": spawned, "cars": a.cars,
        "sim_seconds": end, "wall_seconds": round(wall, 2), "setup_seconds": round(setup_s, 2),
        "real_time_factor": round(end / wall, 2),
        "busiest_5min": {"vehicles_avg": busiest[2], "real_time_factor": round(busiest[0] / busiest[1], 2) if busiest[1] else None},
        "peak_vehicles": peak, "peak_at_s": peak_t,
        "teleports": mt.t.simulation.getStartingTeleportNumber() if hasattr(mt.t.simulation, "getStartingTeleportNumber") else None,
        "arrived_trucks": len(mt.arrivals),
        "network_edges": sum(not e.startswith(":") for e in mt.t.edge.getIDList()),
        "timeline": samples,
        "machine": f"{platform.machine()} · {platform.system()} {platform.release()} · Python {platform.python_version()}",
        "sumo": mt.t.getVersion()[1] if hasattr(mt.t, "getVersion") else "libsumo",
    }
    mt.close()
    ok = res["real_time_factor"] > 1 and (res["busiest_5min"]["real_time_factor"] or 0) > 1
    print(f"{spawned} trucks + {a.cars} cars · peak {peak:,} simultaneous vehicles at t={peak_t:.0f}s")
    print(f"{end:.0f} sim-s in {wall:.1f} wall-s → {res['real_time_factor']}x real time overall, "
          f"{res['busiest_5min']['real_time_factor']}x in the busiest 5 min ({busiest[2]:,} vehicles avg)  "
          f"{'PASS' if ok else 'FAIL'}: faster than real time")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)
            f.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

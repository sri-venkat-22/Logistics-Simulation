"""Calibration export: SUMO corridor travel-time distributions -> macro lanes inside Hyderabad.

    python -m sim.micro.calibrate                    # writes sim/micro/hyderabad/calibration.json
    python -m sim.micro.calibrate --per-corridor 6   # quicker

Trucks drive each corridor with the background traffic of hyderabad.sumocfg, departing at staggered
times through the busy first hour. Their door-to-door times (departure -> pulling into the destination
parking area) are fitted with a lognormal per macro lane. The macro twin then samples those lanes as
    lead time = handling_h (dock + loading, from the macro lane model) + SUMO drive time
instead of its distance / 38 km/h prior. Z_HYD (the city as a demand zone) is the mixture over the
consumer hubs. The Wasserstein-1 distance between the prior and the SUMO samples is reported (FR-22).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from sim.macro.network import Network
from sim.micro.runner import CFG, MicroTwin
from sim.paths import HYDERABAD

OUT = HYDERABAD / "calibration.json"
ROAD_HANDLING_H = 4.0   # fixed dock / loading time in the macro road-lane model (data/generate_network.py)
ROAD_SPEED_KMH = 38.0
CONSUMERS = ["Kompally", "Kukatpally", "Gachibowli", "Hitec City", "Mehdipatnam", "Secunderabad"]
NODE_HUB = {"PLANT_PATANCHERU": "Patancheru Plant", "DC_HYD_MEDCHAL": "Medchal DC", "DC_HYD_SHAMSHABAD": "Shamshabad DC"}
# macro lanes wholly inside the micro-twin -> hub corridors that realise them
LANE_CORRIDORS = {
    "L015": [("Patancheru Plant", "Shamshabad DC")],
    "L016": [("Patancheru Plant", "Medchal DC")],
    "L047": [("Medchal DC", "Shamshabad DC")],
    "L030": [("Medchal DC", c) for c in CONSUMERS],
    "L031": [("Shamshabad DC", c) for c in CONSUMERS],
}


def wasserstein1(a: np.ndarray, b: np.ndarray) -> float:
    """W1 between two empirical samples (mean absolute difference of matched quantiles)."""
    q = np.linspace(0.005, 0.995, 199)
    return float(np.mean(np.abs(np.quantile(a, q) - np.quantile(b, q))))


def calibrate(per_corridor: int = 12, window_s: float = 3000, seed: int = 7, max_s: float = 4 * 3600) -> dict:
    net = Network()
    rnd = random.Random(seed)
    mt = MicroTwin(cfg=CFG, seed=seed, track_edges=False)
    corridors = sorted({c for cs in LANE_CORRIDORS.values() for c in cs})
    n_per = {c: per_corridor if len([l for l, cs in LANE_CORRIDORS.items() if c in cs and len(cs) == 1]) else
             max(2, per_corridor // 3) for c in corridors}
    free = {f"{a}|{b}": mt.corridor_time(a, b) for a, b in corridors}
    spawned = 0
    for a, b in corridors:
        for i in range(n_per[(a, b)]):
            dep = window_s * (i + rnd.random()) / n_per[(a, b)]
            spawned += mt.spawn_truck(f"cal_{spawned}", a, b, depart=dep, meta={"corridor": f"{a}|{b}"})
    t0 = time.perf_counter()
    while mt.trucks and mt.now() < max_s:
        mt.step(60)
    wall = time.perf_counter() - t0
    times: dict[str, list[float]] = {}
    for arr in mt.arrivals:
        times.setdefault(arr.meta["corridor"], []).append(arr.travel_s)
    mt.close()

    corr_out = {}
    for key, xs in sorted(times.items()):
        x = np.array(xs)
        corr_out[key] = {"n": len(xs), "mean_s": round(float(x.mean()), 1), "p10_s": round(float(np.percentile(x, 10)), 1),
                         "p50_s": round(float(np.percentile(x, 50)), 1), "p90_s": round(float(np.percentile(x, 90)), 1),
                         "free_flow_s": round(free[key], 1)}
    lanes_out = {}
    for lid, cs in LANE_CORRIDORS.items():
        xs = np.array([t for a, b in cs for t in times.get(f"{a}|{b}", [])]) / 3600.0
        if len(xs) < 3:
            continue
        lx = np.log(xs)
        mu, sigma = float(lx.mean()), float(max(lx.std(ddof=1), 0.05))
        lane = net.lanes[lid]
        prior_mean = lane.distance_km / ROAD_SPEED_KMH
        prior = np.random.default_rng(0).lognormal(math.log(prior_mean) - lane.lt_sigma ** 2 / 2, lane.lt_sigma, 4000)
        lanes_out[lid] = {
            "from": lane.from_id, "to": lane.to_id, "corridors": [f"{a}|{b}" for a, b in cs], "n": int(len(xs)),
            "handling_h": ROAD_HANDLING_H, "drive_mu": round(mu, 5), "drive_sigma": round(sigma, 5),
            "drive_mean_h": round(float(xs.mean()), 4), "drive_p10_h": round(float(np.percentile(xs, 10)), 4),
            "drive_p50_h": round(float(np.percentile(xs, 50)), 4), "drive_p90_h": round(float(np.percentile(xs, 90)), 4),
            "prior_drive_mean_h": round(prior_mean, 4), "prior_lt_mean_h": lane.lt_mean_h,
            "calibrated_lt_mean_h": round(ROAD_HANDLING_H + float(xs.mean()), 4),
            "wasserstein1_prior_h": round(wasserstein1(prior, xs), 4),
            "samples_h": [round(float(v), 4) for v in xs],
        }
    return {"generated": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"), "seed": seed,
            "sumo_cfg": CFG.name, "trucks": spawned, "arrived": sum(len(v) for v in times.values()),
            "departure_window_s": window_s, "wall_s": round(wall, 1),
            "model": "lead time = handling_h + LogNormal(drive_mu, drive_sigma) hours",
            "lanes": lanes_out, "corridors": corr_out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.micro.calibrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-corridor", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    if not CFG.exists():
        print("micro-twin not built: run  python -m sim.micro.build_hyderabad", file=sys.stderr)
        return 2
    res = calibrate(a.per_corridor, seed=a.seed)
    print(f"SUMO corridor calibration · {res['arrived']}/{res['trucks']} trucks arrived · {res['wall_s']} s wall")
    print(f"  {'lane':<6}{'route':<38}{'n':>4}{'prior drive':>13}{'SUMO mean':>11}{'P10–P90':>16}{'σ(log)':>8}{'W1':>8}")
    for lid, v in res["lanes"].items():
        print(f"  {lid:<6}{v['from'] + ' → ' + v['to']:<38}{v['n']:>4}{v['prior_drive_mean_h'] * 60:>10.0f} min"
              f"{v['drive_mean_h'] * 60:>8.0f} min{v['drive_p10_h'] * 60:>8.0f}–{v['drive_p90_h'] * 60:.0f} min"
              f"{v['drive_sigma']:>8.2f}{v['wasserstein1_prior_h'] * 60:>6.0f} m")
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

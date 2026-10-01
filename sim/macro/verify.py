"""Engine verification: the macro twin against known answers (SRS §8 "Engine verification").

    python -m sim.macro.verify                       # full runs, writes docs/sim/engine_verification.json
    python -m sim.macro.verify --quick               # shorter horizons (what the test-suite runs)

V1  Single-node periodic-review (s,S), Poisson daily demand, deterministic lead time L, backorders.
    Analytic fill rate: the inventory position after ordering, Y, is a Markov chain on {s+1..S}
    (Y' = Y - D if Y - D > s else S); net stock before a day's demand is Y - D_L with D_L ~ Poisson(L*lambda),
    so fill = E[min(D, (Y - D_L)^+)] / lambda.  Pass: simulated fill within 2 % of the analytic value.
V2  EOQ: deterministic demand D, fixed order cost K, holding h, continuous-review (R,Q), lead time L.
    Sweep Q; the simulated cost-per-day curve must be minimised at the grid point nearest
    EOQ = sqrt(2KD/h), and match K*D/Q + h*(Q/2 + R - D*L) within 2 %.
V3  3-node toy (infinite supplier -> distributor (s,S), continuous review -> Poisson unit demand, lost sales)
    in the AEGIS engine and in SupplyNetPy 0.1.12 (the plan's validation reference), same parameters and
    horizon, independent random streams, many seeds.  Pass: mean fill rate within 2 percentage points,
    mean on-hand and replenishment-order counts within 3 %.
Every run is seeded; the report records seeds, horizons and engine version.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from sim.macro.engine import ENGINE_VERSION, Twin
from sim.macro.network import Network
from sim.macro.policies import RQPolicy, SSPolicy
from sim.paths import ROOT

START = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
OUT = ROOT / "docs" / "sim" / "engine_verification.json"


# ------------------------------------------------------------------------------------------ toy world
def toy_network(lead_h: float, unit_value: float = 100.0, holding_rate_yr: float = 0.25, order_cost: float = 0.0,
                penalty: float = 0.0) -> Network:
    """Infinite supplier S1 -> warehouse W1 -> demand zone Z1; deterministic lead time lead_h."""
    def lane(lid, a, b, h):
        return {"id": lid, "from_id": a, "to_id": b, "mode": "road", "distance_km": 1.0, "cost_per_unit_km": 0.0,
                "cost_per_unit": 0.0, "capacity": 1e12, "co2_per_tkm": 1e-9, "lt_mu": math.log(h), "lt_sigma": 0.0,
                "lt_mean_h": h, "lt_p90_h": h}
    nodes = [{"id": "S1", "type": "supplier", "name": "Infinite supplier", "lat": 0.0, "lon": 0.0, "capacity": math.inf},
             {"id": "W1", "type": "dc", "name": "Warehouse", "lat": 0.0, "lon": 0.1, "capacity": 1e12},
             {"id": "Z1", "type": "zone", "name": "Customers", "lat": 0.0, "lon": 0.2, "capacity": 0, "attrs": {"pop_m": 1.0}}]
    skus = [{"id": "SKU_T", "family": "fmcg", "name": "toy", "unit_value": unit_value, "unit_weight_kg": 1.0,
             "cold_chain": False, "sea_imported": False, "base_demand_per_million_day": 1.0,
             "holding_rate_yr": holding_rate_yr, "stockout_penalty": penalty, "order_cost": order_cost}]
    sourcing = {"replenishment": [{"dc": "W1", "sku": "SKU_T", "primary": {"source": "S1", "nodes": ["S1", "W1"],
                                                                          "lanes": ["L1"], "lead_mean_h": lead_h},
                                   "alternates": []}],
                "serving": [{"zone": "Z1", "sku": "SKU_T", "dc": "W1", "lane": "L2", "backup": []}]}
    return Network.from_raw(nodes, [lane("L1", "S1", "W1", lead_h), lane("L2", "W1", "Z1", 1.0)], skus, sourcing)


class PoissonDaily:
    """One order per day (random hour 08-20) of Poisson(lam) units."""
    def __init__(self, lam: float):
        self.lam = lam

    def base_mean(self, zone, sku):
        return self.lam

    def mean(self, zone, sku, d):
        return self.lam

    def variance(self, m, sku):
        return m

    def sample(self, rng, zone, sku, d, mult=1.0):
        return int(rng.poisson(self.lam * mult))


class Renewal:
    """Renewal order stream: exponential (Poisson process) or fixed inter-arrival times, fixed quantity."""
    def __init__(self, rate_h: float | None = None, every_h: float | None = None, qty: float = 1, first_h: float | None = None):
        self.rate_h, self.every_h, self.qty, self.first_h = rate_h, every_h, qty, first_h
        self._first = True

    def base_mean(self, zone, sku):
        return 24.0 * self.qty * (self.rate_h if self.rate_h else 1.0 / self.every_h)

    def mean(self, zone, sku, d):
        return self.base_mean(zone, sku)

    def variance(self, m, sku):
        return m

    def interarrival_h(self, rng, zone, sku):
        if self._first and self.first_h is not None:
            self._first = False
            return self.first_h
        return float(rng.exponential(1.0 / self.rate_h)) if self.rate_h else self.every_h

    def quantity(self, rng, zone, sku):
        return self.qty


def toy_twin(net, demand, policy, seed) -> Twin:
    return Twin(net, demand, START, seed=seed, policies={("W1", "SKU_T"): policy}, random_failures=False,
                calibration=None, log_events=False, initial_state="full")


# ------------------------------------------------------------------------------------------ V1
def _poisson_pmf(mu: float, n: int) -> np.ndarray:
    k = np.arange(n)
    logp = -mu + k * math.log(mu) - np.array([math.lgamma(i + 1) for i in k])
    return np.exp(logp)


def analytic_ss_fill_rate(lam: float, lead_days: int, s: int, S: int) -> float:
    n = int(lam * (lead_days + 1) + 12 * math.sqrt(lam * (lead_days + 1)) + S + 10)
    p = _poisson_pmf(lam, n)
    states = list(range(s + 1, S + 1))
    idx = {y: i for i, y in enumerate(states)}
    P = np.zeros((len(states), len(states)))
    for y in states:
        for d in range(n):
            nxt = y - d if y - d > s else S
            P[idx[y], idx[nxt]] += p[d]
    w, v = np.linalg.eig(P.T)
    pi = np.real(v[:, np.argmin(abs(w - 1))])
    pi = pi / pi.sum()
    pL = _poisson_pmf(lam * lead_days, n)
    exp_filled = 0.0
    for y, py in zip(states, pi):
        for dl in range(n):
            ni = y - dl
            if ni <= 0:
                break
            # E[min(D, ni)]
            dd = np.arange(n)
            exp_filled += py * pL[dl] * float(np.sum(p * np.minimum(dd, ni)))
    return exp_filled / lam


def v1_ss_fill_rate(days: int = 20000, seed: int = 11, lam: float = 20.0, lead_days: int = 2, s: int = 45, S: int = 80) -> dict:
    net = toy_network(lead_h=24.0 * lead_days)
    twin = toy_twin(net, PoissonDaily(lam), SSPolicy(s, S, review_h=24), seed)
    warm = 60
    twin.run_until(warm * 24)
    twin.reset_stats()
    twin.run_until((warm + days) * 24)
    k = twin.kpis()
    sim = k["fill_rate"]
    ana = analytic_ss_fill_rate(lam, lead_days, s, S)
    rel = abs(sim - ana) / ana
    return {"name": "V1 single-node (s,S), Poisson demand, analytic fill rate",
            "params": {"lambda_per_day": lam, "lead_days": lead_days, "s": s, "S": S, "review": "daily",
                       "days": days, "warmup_days": warm, "seed": seed},
            "analytic_fill_rate": round(ana, 5), "simulated_fill_rate": round(sim, 5),
            "rel_error": round(rel, 5), "tolerance": 0.02, "pass": rel <= 0.02}


# ------------------------------------------------------------------------------------------ V2
def eoq_cost_per_day(Q: float, D: float, K: float, h: float, lead_days: float, r: float, step: float = 0.0) -> float:
    """K*D/Q + h*(Q/2 + safety stock); `step` adds the half-step of on-hand stock that a stream of discrete
    withdrawals of `step` units leaves on the shelf between withdrawals (0 for continuous depletion)."""
    return K * D / Q + h * (Q / 2 + (r - D * lead_days) + step / 2)


def v2_eoq(cycles: int = 200, D: float = 240.0, K: float = 5000.0, unit_value: float = 1000.0, holding_rate_yr: float = 0.365,
           lead_days: float = 1.0, step: int = 150, q_min: int = 600, q_max: int = 3000) -> dict:
    """Deterministic demand of D/24 units every hour (at h+0.5). Each Q is measured over exactly `cycles`
    reorder cycles (after 10 warm-up cycles), so no partial cycle biases the ordering or holding cost."""
    h = unit_value * holding_rate_yr / 365.0  # per unit per day
    eoq = math.sqrt(2 * K * D / h)
    per_h = D / 24
    r = D * lead_days + per_h  # one withdrawal of safety so an arrival never ties with a stock-out
    net = toy_network(lead_h=24.0 * lead_days, unit_value=unit_value, holding_rate_yr=holding_rate_yr, order_cost=K)
    curve = []
    for Q in range(q_min, q_max + 1, step):
        cycle_h = Q / per_h
        twin = toy_twin(net, Renewal(every_h=1.0, qty=per_h, first_h=0.5), RQPolicy(r=r, q=Q, review_h=0), seed=1)
        twin.run_until(10 * cycle_h)
        twin.reset_stats()
        twin.run_until((10 + cycles) * cycle_h)
        k = twin.kpis()
        days = cycles * cycle_h / 24
        sim = (k["cost_inr"]["holding"] + k["cost_inr"]["ordering"]) / days
        curve.append({"Q": Q, "sim_cost_per_day": round(sim, 3),
                      "analytic_cost_per_day": round(eoq_cost_per_day(Q, D, K, h, lead_days, r, per_h), 3),
                      "orders": k["replenishment_orders"], "fill_rate": round(k["fill_rate"], 5)})
    best = min(curve, key=lambda c: c["sim_cost_per_day"])
    nearest = min(curve, key=lambda c: abs(c["Q"] - eoq))
    max_rel = max(abs(c["sim_cost_per_day"] - c["analytic_cost_per_day"]) / c["analytic_cost_per_day"] for c in curve)
    return {"name": "V2 EOQ sanity check (deterministic demand, (R,Q) continuous review)",
            "params": {"D_per_day": D, "K": K, "h_per_unit_day": h, "lead_days": lead_days, "R": r,
                       "cycles_measured": cycles, "warmup_cycles": 10, "grid": [q_min, q_max, step]},
            "eoq": round(eoq, 1), "sim_argmin_Q": best["Q"], "grid_point_nearest_eoq": nearest["Q"],
            "max_rel_error_vs_formula": round(max_rel, 5), "tolerance": 0.02,
            "all_filled": all(c["fill_rate"] == 1.0 for c in curve), "curve": curve,
            "pass": best["Q"] == nearest["Q"] and max_rel <= 0.02}


# ------------------------------------------------------------------------------------------ V3
def _aegis_toy(seed: int, hours: float, rate_h: float, lead_h: float, s: int, S: int, holding: float) -> dict:
    net = toy_network(lead_h=lead_h)
    twin = toy_twin(net, Renewal(rate_h=rate_h, qty=1), SSPolicy(s, S, review_h=0, lost_sales=True), seed)
    twin.run_until(hours)
    k = twin.kpis()
    return {"fill_rate": k["fill_rate"], "avg_on_hand": k["avg_on_hand"], "orders": k["replenishment_orders"],
            "demand": k["units_demanded"]}


def _snp_toy(seed: int, hours: float, rate_h: float, lead_h: float, s: int, S: int, holding: float) -> dict:
    import simpy
    import SupplyNetPy.Components as scm
    scm.set_seed(seed)
    rnd = random.Random(seed)
    env = simpy.Environment()
    sup = scm.Supplier(env=env, ID="S1", name="Supplier", node_type="infinite_supplier", logging=False)
    dist = scm.InventoryNode(env=env, ID="D1", name="Distributor", node_type="distributor", capacity=10 * S,
                             initial_level=S, inventory_holding_cost=holding, replenishment_policy=scm.SSReplenishment,
                             policy_param={"s": s, "S": S}, product_sell_price=1.0, product_buy_price=1.0, logging=False)
    _link = scm.Link(env=env, ID="L1", source=sup, sink=dist, cost=0, lead_time=lambda: lead_h)
    dem = scm.Demand(env=env, ID="C1", name="Customers", order_arrival_model=lambda: rnd.expovariate(rate_h),
                     order_quantity_model=lambda: 1, demand_node=dist, logging=False)
    env.run(until=hours)
    dist.inventory.update_carry_cost()
    placed = dem.stats.demand_placed[1]
    got = dem.stats.fulfillment_received[1]
    return {"fill_rate": got / placed, "avg_on_hand": dist.inventory.carry_cost / (holding * hours),
            "orders": dist.stats.demand_placed[0], "demand": placed}


def v3_supplynetpy(seeds: int = 20, hours: float = 20000.0, rate_h: float = 1.0, lead_h: float = 48.0, s: int = 40,
                   S: int = 90) -> dict:
    import SupplyNetPy
    holding = 1.0
    a = [_aegis_toy(1000 + i, hours, rate_h, lead_h, s, S, holding) for i in range(seeds)]
    b = [_snp_toy(2000 + i, hours, rate_h, lead_h, s, S, holding) for i in range(seeds)]

    def mean(xs, key):
        return float(np.mean([x[key] for x in xs]))

    def ci(xs, key):
        v = np.array([x[key] for x in xs])
        return float(1.96 * v.std(ddof=1) / math.sqrt(len(v)))

    res = {}
    for key in ("fill_rate", "avg_on_hand", "orders"):
        res[key] = {"aegis": round(mean(a, key), 4), "aegis_ci95": round(ci(a, key), 4),
                    "supplynetpy": round(mean(b, key), 4), "supplynetpy_ci95": round(ci(b, key), 4)}
    d_fill = abs(res["fill_rate"]["aegis"] - res["fill_rate"]["supplynetpy"])
    d_inv = abs(res["avg_on_hand"]["aegis"] / res["avg_on_hand"]["supplynetpy"] - 1)
    d_ord = abs(res["orders"]["aegis"] / res["orders"]["supplynetpy"] - 1)
    return {"name": "V3 3-node toy cross-checked against SupplyNetPy",
            "params": {"supplynetpy": getattr(SupplyNetPy, "__version__", "0.1.12"), "seeds": seeds, "hours": hours,
                       "demand": f"Poisson arrivals {rate_h}/h, 1 unit each, lost sales", "lead_h": lead_h,
                       "policy": f"(s,S)=({s},{S}) continuous review"},
            "metrics": res, "abs_diff_fill_pp": round(d_fill * 100, 3), "rel_diff_on_hand": round(d_inv, 4),
            "rel_diff_orders": round(d_ord, 4), "tolerance": {"fill_pp": 2.0, "on_hand": 0.03, "orders": 0.03},
            "pass": d_fill <= 0.02 and d_inv <= 0.03 and d_ord <= 0.03}


def verify(quick: bool = False) -> dict:
    t0 = time.perf_counter()
    checks = [v1_ss_fill_rate(days=4000 if quick else 20000),
              v2_eoq(cycles=60 if quick else 200),
              v3_supplynetpy(seeds=8 if quick else 20, hours=8000 if quick else 20000)]
    return {"engine": ENGINE_VERSION, "generated": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"),
            "machine": f"{platform.machine()} · {platform.system()} · Python {platform.python_version()}",
            "quick": quick, "wall_s": round(time.perf_counter() - t0, 2), "checks": checks,
            "pass": all(c["pass"] for c in checks)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.macro.verify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", type=Path, default=None, help=f"write the report (default {OUT.relative_to(ROOT)} for full runs)")
    a = ap.parse_args(argv)
    rep = verify(a.quick)
    v1, v2, v3 = rep["checks"]
    ok = lambda c: "PASS" if c["pass"] else "FAIL"  # noqa: E731
    print(f"AEGIS engine verification · {rep['engine']} · {rep['wall_s']} s")
    print(f"  {ok(v1)}  {v1['name']}: simulated {v1['simulated_fill_rate'] * 100:.2f} % vs analytic "
          f"{v1['analytic_fill_rate'] * 100:.2f} % (rel. error {v1['rel_error'] * 100:.2f} %, tolerance 2 %)")
    print(f"  {ok(v2)}  {v2['name']}: EOQ {v2['eoq']:.0f}, simulated minimum at Q={v2['sim_argmin_Q']} "
          f"(nearest grid point {v2['grid_point_nearest_eoq']}), max deviation from K·D/Q + h·(Q/2+SS) "
          f"{v2['max_rel_error_vs_formula'] * 100:.2f} %")
    m = v3["metrics"]
    print(f"  {ok(v3)}  {v3['name']}: fill {m['fill_rate']['aegis'] * 100:.2f} % vs {m['fill_rate']['supplynetpy'] * 100:.2f} % "
          f"(Δ {v3['abs_diff_fill_pp']:.2f} pp); on-hand {m['avg_on_hand']['aegis']:.1f} vs {m['avg_on_hand']['supplynetpy']:.1f} "
          f"(Δ {v3['rel_diff_on_hand'] * 100:.1f} %); orders {m['orders']['aegis']:.1f} vs {m['orders']['supplynetpy']:.1f} "
          f"(Δ {v3['rel_diff_orders'] * 100:.1f} %)")
    out = a.out or (None if a.quick else OUT)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)) + "\n")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Optimiser report: candidate plans for the stress scenarios, evaluated with Monte Carlo on common seeds.

    python -m sim.optimize.report            # -> docs/intelligence/optimizer.json + a table (n = 100 per plan)

Scenarios (the ones that actually hurt this network; the 5-day templates are absorbed at 100 % fill, see
docs/world/scenario_results.md): Chennai port closed 21 days, Patancheru vaccine plant down 7 days, the Ahmedabad
FMCG supplier down 10 days.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.montecarlo import RunSpec, monte_carlo
from sim.macro.network import Network
from sim.optimize import optimizer as O
from sim.paths import ROOT, SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

OUT = ROOT / "docs" / "intelligence" / "optimizer.json"
CASES: list[tuple[str, str, dict]] = [
    ("Chennai port closed 21 d", "port_closure", {"duration_h": 504}),
    ("Patancheru plant down 7 d", "supplier_failure", {"duration_h": 168}),
    ("Ahmedabad supplier down 10 d", "supplier_failure", {"target": "SUP_AHMEDABAD_TEX", "duration_h": 240}),
]


def run(n: int = 100, days: float = 30) -> dict:
    net = Network()
    start = "2026-10-15T00:00:00+05:30"
    twin = Twin(net, DemandModel(net), datetime.fromisoformat(start), log_events=False)
    levels = {(dc, sku): twin.warehouses[dc].policy[sku].levels(twin, dc, sku, 0) for dc, sku in twin.pairs}
    cases: list[dict] = []
    out = {"n": n, "days": days, "seeds": f"42..{41 + n} (common random numbers across plans)", "cases": cases}
    for label, tmpl, upd in CASES:
        t0 = time.perf_counter()
        sc = Scenario.model_validate({**json.loads((SCENARIO_TEMPLATES / f"{tmpl}.json").read_text()), **upd, "name": label})
        spec = RunSpec(days=days, scenarios=[sc])
        res = monte_carlo(spec, n=n)
        cands = O.candidates(twin, [sc], res, levels)
        rows: list[dict] = []
        for c in cands:
            m: dict = O.metrics(monte_carlo(O.apply_to_spec(spec, c["actions"]), n=n))
            rows.append({"name": c["name"], "kind": c["kind"], "actions": len(c["actions"]), **m})
        ranked = O.rank(rows)
        base = next(r for r in ranked if r["kind"] == "baseline")
        cases.append({"scenario": label, "wall_s": round(time.perf_counter() - t0, 1), "baseline": base,
                             "plans": ranked,
                             "best": {"name": ranked[0]["name"], "fill_gain_pp": round((ranked[0]["service"] - base["service"]) * 100, 2),
                                      "cvar95_change_lakh": round((ranked[0]["cvar95_lakh"] or 0) - (base["cvar95_lakh"] or 0), 1),
                                      "cost_change_lakh": round(ranked[0]["cost_lakh"] - base["cost_lakh"], 1)}})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.optimize.report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100)
    a = ap.parse_args(argv)
    r = run(a.n)
    for c in r["cases"]:
        print(f"\n{c['scenario']} · {len(c['plans'])} plans · {c['wall_s']} s")
        print(f"  {'plan':<46}{'fill':>8}{'cost':>9}{'CO2':>8}{'CVaR95':>9}  Pareto  score")
        for p in c["plans"]:
            print(f"  {p['name'][:45]:<46}{p['service'] * 100:>7.2f}%{p['cost_lakh']:>8.1f}L{p['co2_t']:>7.1f}t"
                  f"{(p['cvar95_lakh'] or 0):>8.1f}L  {'yes' if p['pareto'] else ' - ':>5}  {p['score']:.3f}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(r, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

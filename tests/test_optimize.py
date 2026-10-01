"""Phase 7.1: recommendation engine - k-shortest reroutes, min-cost-flow reallocation, expedite, ranking,
explanations, and the engine actions behind them (set_route, transfer)."""
import json
from datetime import datetime

import pytest

from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.montecarlo import RunSpec, monte_carlo, run_one
from sim.macro.network import Network
from sim.optimize import optimizer as O
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

START = "2026-10-15T00:00:00+05:30"


@pytest.fixture(scope="module")
def twin():
    net = Network()
    return Twin(net, DemandModel(net), datetime.fromisoformat(START), log_events=False)


def template(name, **upd):
    return Scenario.model_validate({**json.loads((SCENARIO_TEMPLATES / f"{name}.json").read_text()), **upd})


def test_k_routes_avoid_closed_nodes_and_respect_cold_chain(twin):
    dis = O.disruption_of(twin, [template("port_closure", duration_h=504)])
    assert dis.closed == {"PORT_CHENNAI"} and ("DC_HYD_SHAMSHABAD", "SKU_ELEC") in dis.affected
    routes = O.k_routes(twin, "DC_HYD_SHAMSHABAD", "SKU_ELEC", dis, k=3)
    assert routes and all("PORT_CHENNAI" not in r.nodes for r in routes)
    assert routes == sorted(routes, key=lambda r: r.weight)
    same = O.k_routes(twin, "DC_HYD_SHAMSHABAD", "SKU_ELEC", dis, k=1, sources={"SUP_SHENZHEN"})[0]
    assert same.nodes[0] == "SUP_SHENZHEN" and same.nodes[1] == "PORT_VIZAG"
    vax = O.k_routes(twin, "DC_DELHI", "SKU_VAX", O.Disruption(), k=3, allow_air=True)
    assert vax and all(twin.net.nodes[n].cold_chain for r in vax for n in r.nodes if twin.net.nodes[n].type == "dc")
    fast = O.k_routes(twin, "DC_DELHI", "SKU_VAX", O.Disruption(), k=1, lam=25.0, allow_air=True)[0]
    assert fast.lanes == ["L018"]  # Patancheru -> Delhi by air when time dominates


def test_reallocation_min_cost_flow_multi_hop(twin):
    dis = O.disruption_of(twin, [template("port_closure", duration_h=504)])
    res = {"stockout_prob": {"DC_BLR/SKU_ELEC": 0.6},
           "series": {"DC_BLR/SKU_ELEC": {"backlog": {"p50": [0, 299], "p90": [0, 400]}, "on_hand": {"p10": [0, 0], "p50": [0, 0]}},
                      "DC_HYD_SHAMSHABAD/SKU_ELEC": {"backlog": {"p50": [0, 49], "p90": [0, 80]}, "on_hand": {"p10": [0, 0], "p50": [0, 0]}},
                      "DC_HYD_MEDCHAL/SKU_ELEC": {"backlog": {"p50": [0, 0], "p90": [0, 0]}, "on_hand": {"p10": [900, 732], "p50": [0, 0]}}}}
    acts = O.reallocate_actions(twin, dis, res, {})
    got = {(a["from"], a["to"]): (a["qty"], a["lanes"]) for a in acts}
    assert got[("DC_HYD_MEDCHAL", "DC_HYD_SHAMSHABAD")] == (49, ["L047"])
    assert got[("DC_HYD_MEDCHAL", "DC_BLR")] == (299, ["L047", "L050"])  # through Shamshabad on the transfer lane
    assert sum(a["qty"] for a in acts) <= 366                            # never more than half the donor's low point


def test_engine_set_route_and_transfer_validation(twin):
    with pytest.raises(ValueError):
        twin.apply({"type": "set_route", "dc": "DC_BLR", "sku": "SKU_VAX", "lanes": ["L002", "L009"]})  # wrong DC
    with pytest.raises(ValueError):
        twin.apply({"type": "set_route", "dc": "DC_HYD_SHAMSHABAD", "sku": "SKU_VAX", "lanes": ["L002", "L009"]})  # Shenzhen makes no vaccine
    with pytest.raises(ValueError):
        twin.apply({"type": "transfer", "from": "DC_BLR", "to": "DC_DELHI", "sku": "SKU_FMCG", "qty": 5, "lanes": ["L025"]})
    spec = RunSpec(days=3, random_failures=False, transfers=[{"from": "DC_BLR", "to": "DC_HYD_SHAMSHABAD", "sku": "SKU_VAX",
                                                              "qty": 300, "lanes": ["L025"], "at_h": 0}])
    ev = O.evidence_run(spec, 42)
    t = next(e for e in ev if e["event"] == "transfer")
    r = next(e for e in ev if e["event"] == "receipt" and e["shipment"] == t["shipment"])
    assert t["qty"] == 300 and 10 < r["lead_h"] < 40


def test_rank_pareto_and_weights():
    plans = [{"name": "a", "service": 0.99, "cost_lakh": 100, "co2_t": 10, "cvar95_lakh": 50},
             {"name": "b", "service": 0.95, "cost_lakh": 90, "co2_t": 10, "cvar95_lakh": 60},
             {"name": "c", "service": 0.94, "cost_lakh": 95, "co2_t": 11, "cvar95_lakh": 70}]  # dominated by b
    r = O.rank([dict(p) for p in plans])
    assert {p["name"]: p["pareto"] for p in r} == {"a": True, "b": True, "c": False} and r[0]["name"] == "a"
    r = O.rank([dict(p) for p in plans], {"service": 0, "risk": 0, "cost": 1, "co2": 0})
    assert r[0]["name"] == "b"


def test_plans_for_a_long_chennai_closure_beat_doing_nothing(twin):
    sc = template("port_closure", duration_h=504)
    spec = RunSpec(days=30, scenarios=[sc])
    res = monte_carlo(spec, n=16)
    levels = {(dc, sku): twin.warehouses[dc].policy[sku].levels(twin, dc, sku, 0) for dc, sku in twin.pairs}
    cands = O.candidates(twin, [sc], res, levels)
    kinds = {c["kind"] for c in cands}
    assert {"baseline", "reroute", "buffer", "combined"} <= kinds
    rows = [{**c, **O.metrics(monte_carlo(O.apply_to_spec(spec, c["actions"]), n=16))} for c in cands]
    ranked = O.rank(rows)
    base = next(p for p in ranked if p["kind"] == "baseline")
    assert ranked[0]["kind"] != "baseline" and ranked[0]["cvar95_lakh"] < base["cvar95_lakh"]
    best = ranked[0]
    ex = O.explain(best, base, O.evidence_run(O.apply_to_spec(spec, best["actions"]), 42), O.evidence_run(spec, 42), twin.net)
    assert ex["text"].startswith(best["name"]) and "CVaR95" in ex["text"]


def test_monte_carlo_has_cvar_and_shortfall():
    r = run_one(RunSpec(days=5), 42)
    assert "shortfall_value" in r["scalars"] and r["scalars"]["avg_lead_h"] > 0
    band = O.metrics(monte_carlo(RunSpec(days=5), n=4, processes=1))
    assert band["cvar95_lakh"] is not None and band["delay_h"] > 0


def test_criticality_ranks_single_sources_and_cascades():
    from sim.optimize.criticality import FlowModel, cascade, criticality
    res = criticality(alpha=0.25)
    top = [r["node"] for r in res["nodes"][:3]]
    assert "SUP_AHMEDABAD_TEX" in top and "DC_DELHI" in top    # the sole FMCG source; Delhi's zones have no backup
    ahm = next(r for r in res["nodes"] if r["node"] == "SUP_AHMEDABAD_TEX")
    assert ahm["unserved_share"] > 0.5 and ahm["rei"] == 1.0
    fm = FlowModel(Network())
    c = cascade(fm, "DC_HYD_MEDCHAL", 0.05)
    assert c["steps"][0]["nodes"] == ["DC_HYD_MEDCHAL"] and len(c["steps"]) > 1 and c["failed_lanes"]
    assert cascade(fm, "DC_HYD_MEDCHAL", 1.0)["size"] < c["size"]  # more tolerance, smaller cascade
    assert c["unserved_share"] >= cascade(fm, "DC_HYD_MEDCHAL", 1.0)["unserved_share"]

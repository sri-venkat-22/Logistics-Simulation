"""Macro twin v0: deterministic KPIs, sane baseline, disruptions propagate."""
from sim.macro.engine import Twin
from sim.macro.run import main
from sim.scenarios.dsl import Scenario
from sim.paths import SCENARIO_TEMPLATES

from .conftest import START


def run(net, demand, scenarios=(), days=30, seed=42):
    t = Twin(net, demand, START, seed=seed, scenarios=list(scenarios))
    return t.run(days), t


def test_deterministic(net, demand):
    a, _ = run(net, demand, days=10)
    b, _ = run(net, demand, days=10)
    assert a["fill_rate"] == b["fill_rate"] and a["cost_inr"] == b["cost_inr"]


def test_baseline_is_healthy(net, demand):
    k, _ = run(net, demand)
    assert k["orders"] == 12 * 3 * 30
    assert k["fill_rate"] > 0.97 and k["otif"] > 0.95
    assert 2 < k["inventory_days"] < 20
    assert k["co2_t"] > 0 and k["cost_inr"]["transport"] > 0


def test_long_plant_outage_causes_vaccine_stockouts(net, demand):
    base, _ = run(net, demand)
    sc = Scenario(type="supplier_failure", target="PLANT_PATANCHERU", start="+12h", duration_h=168, severity=1.0)
    k, _ = run(net, demand, [sc])
    assert k["per_sku"]["SKU_VAX"]["fill_rate"] < base["per_sku"]["SKU_VAX"]["fill_rate"] - 0.05
    assert k["per_dc_sku"]["DC_HYD_SHAMSHABAD/SKU_VAX"]["stockout_hours"] > 0
    assert k["per_sku"]["SKU_FMCG"]["fill_rate"] == base["per_sku"]["SKU_FMCG"]["fill_rate"]  # other SKUs untouched


def test_port_closure_holds_cargo_until_reopening(net, demand):
    sc = Scenario.load(SCENARIO_TEMPLATES / "port_closure.json")  # Chennai, +6 h for 120 h
    _, t = run(net, demand, [sc], days=10)
    reopen_h = sc.start_offset_h(START) + sc.duration_h
    blr = [e for e in t.events if e["event"] == "receipt" and e["dc"] == "DC_BLR" and e["sku"] == "SKU_ELEC"]
    assert blr and all(e["t_h"] >= reopen_h or e["t_h"] < sc.start_offset_h(START) + 13 for e in blr)
    assert min(e["t_h"] for e in blr if e["t_h"] > 24) >= reopen_h  # nothing lands at Chennai while closed


def test_demand_spike_raises_demand(net, demand):
    sc = Scenario.load(SCENARIO_TEMPLATES / "demand_spike.json")  # Z_HYD FMCG + electronics x1.8 for 7 days

    def hyd(t, sku):
        return sum(o.qty for o in t.orders if o.zone == "Z_HYD" and o.sku == sku and o.created_h < 24 * 7)

    _, b = run(net, demand, days=10)
    _, s = run(net, demand, [sc], days=10)
    assert 1.7 < hyd(s, "SKU_FMCG") / hyd(b, "SKU_FMCG") < 1.9  # same random draws (CRN), scaled by 1.8
    assert hyd(s, "SKU_VAX") == hyd(b, "SKU_VAX")  # vaccines not in the spike


def test_all_templates_run(net, demand):
    for p in sorted(SCENARIO_TEMPLATES.glob("*.json")):
        k, t = run(net, demand, [Scenario.load(p)], days=12)
        assert any(e["event"] == "disruption_start" for e in t.events), p.name
        assert 0 <= k["fill_rate"] <= 1


def test_cli(capsys):
    assert main(["--days", "5"]) == 0
    out = capsys.readouterr().out
    assert "Fill rate" in out and "OTIF" in out and "CO₂" in out

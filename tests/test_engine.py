"""Phase 3a: macro engine entities, live API (run_until / snapshot / apply), event log, TTS / TTR."""
import json
import time

import pytest
import simpy

from sim.macro.engine import Twin
from sim.macro.kpis import EVIDENCE
from sim.macro.policies import DynamicSSPolicy
from sim.scenarios.dsl import Scenario
from sim.paths import SCENARIO_TEMPLATES

from .conftest import START


def twin(net, demand, **kw):
    return Twin(net, demand, START, seed=kw.pop("seed", 42), **kw)


def test_entities_use_simpy_primitives(net, demand):
    t = twin(net, demand)
    assert isinstance(t.suppliers["PLANT_PATANCHERU"].lines, simpy.Resource)
    assert t.ports["PORT_CHENNAI"].berths.capacity == net.nodes["PORT_CHENNAI"].attrs["berths"]
    wh = t.warehouses["DC_HYD_SHAMSHABAD"]
    assert isinstance(wh.stock["SKU_VAX"], simpy.Container) and wh.stock["SKU_VAX"].level > 0
    assert isinstance(wh.policy["SKU_VAX"], DynamicSSPolicy)
    assert set(t.zones) == {n.id for n in net.of_type("zone")}
    assert len(t.shipments) > 20  # steady-state pipeline seeded in transit


def test_run_until_in_steps_matches_one_run(net, demand):
    a = twin(net, demand)
    for h in (5, 17.5, 48, 100, 240):
        a.run_until(h)
    b = twin(net, demand)
    b.run_until(240)
    assert a.kpis(10) == b.kpis(10)
    assert a.events == b.events


def test_snapshot_is_json_and_complete(net, demand):
    t = twin(net, demand)
    t.run_until(30)
    s = t.snapshot()
    json.dumps(s)
    assert s["t_h"] == 30 and set(s) >= {"nodes", "inventory", "shipments", "lanes", "effects", "kpis_to_date"}
    assert s["inventory"]["DC_HYD_SHAMSHABAD/SKU_VAX"]["on_hand"] > 0
    shp = s["shipments"][0]
    assert {"lat", "lon", "progress", "status", "lane", "mode"} <= set(shp)
    assert s["nodes"]["PORT_CHENNAI"]["berths"] == 4


def test_apply_events(net, demand):
    t = twin(net, demand)
    t.run_until(10)
    assert t.apply({"type": "lane_multiplier", "lane": "L015", "multiplier": 1.5})["ok"]
    assert t.lanes["L015"].live_mult == 1.5
    ack = t.apply({"type": "node_status", "node": "PORT_CHENNAI", "factor": 0.0, "duration_h": 5})
    assert t.node_status("PORT_CHENNAI") == "closed"
    t.apply({"type": "scenario", "scenario": {"type": "demand_spike", "target": "Z_HYD", "start": "+2h", "duration_h": 4,
                                              "params": {"multiplier": 2.0}}})
    t.run_until(12.5)
    assert t.demand_mult("Z_HYD", "SKU_FMCG") == 2.0  # relative start counted from the apply time (10 h)
    t.run_until(16.5)  # both ended at 15 h / 16 h
    assert t.node_status("PORT_CHENNAI") == "up" and t.demand_mult("Z_HYD", "SKU_FMCG") == 1.0
    e = t.apply({"type": "demand_multiplier", "zone": "Z_BLR", "multiplier": 1.3})
    t.apply({"type": "disruption_end", "id": e["id"]})
    assert t.demand_mult("Z_BLR", "SKU_VAX") == 1.0
    assert t.apply({"type": "set_path", "dc": "DC_BLR", "sku": "SKU_ELEC", "path": 1})["ok"]
    assert t.path("DC_BLR", "SKU_ELEC") is net.replenishment[("DC_BLR", "SKU_ELEC")][1]
    assert t.apply({"type": "order", "zone": "Z_HYD", "sku": "SKU_VAX", "qty": 10})["order"] is not None
    for bad in ({"type": "nope"}, {"type": "lane_multiplier", "lane": "L015", "multiplier": -1},
                {"type": "node_status", "node": "X", "factor": 0}, {"type": "receive", "shipment_id": "SH9"}):
        with pytest.raises((ValueError, KeyError)):
            t.apply(bad)
    assert ack["id"].startswith("E")


def test_event_log_is_evidence_for_every_kpi(net, demand):
    sc = Scenario.load(SCENARIO_TEMPLATES / "supplier_failure.json").model_copy(update={"duration_h": 168})
    t = twin(net, demand, scenarios=[sc])
    k = t.run(20)
    for kpi in EVIDENCE:
        if kpi == "cost_ordering":
            continue  # no fixed order cost in the reference data
        assert t.evidence(kpi), kpi
    # the fill-rate evidence reproduces the KPI exactly
    orders = t.evidence("fill_rate")
    rows = [e for e in orders if e["event"] == "order"]
    assert sum(e["filled"] for e in rows) / sum(e["qty"] for e in rows) == pytest.approx(k["fill_rate"])
    assert sum(e["cost"] for e in t.evidence("cost_transport")) == pytest.approx(k["cost_inr"]["transport"], rel=1e-6)


def test_tts_ttr_exposure(net, demand):
    sc = Scenario(type="supplier_failure", target="PLANT_PATANCHERU", start="+12h", duration_h=168)
    k = twin(net, demand, scenarios=[sc], random_failures=False).run(30)
    d = next(x for x in k["disruptions"] if x["kind"] == "scenario")
    assert d["ttr_h"] == 168 and d["tts_h"] is not None and d["tts_h"] < d["ttr_h"] and d["exposed"]
    assert "DC_HYD_SHAMSHABAD/SKU_VAX" in d["stockouts"]
    assert k["tts_ttr"]["PLANT_PATANCHERU"]["exposed"]
    short = Scenario(type="supplier_failure", target="PLANT_PATANCHERU", start="+12h", duration_h=24)
    k2 = twin(net, demand, scenarios=[short], random_failures=False).run(30)
    d2 = next(x for x in k2["disruptions"] if x["kind"] == "scenario")
    assert d2["tts_h"] is None and not d2["exposed"]  # survived a 1-day outage


def test_random_failures_are_seeded_effects(net, demand):
    k1 = twin(net, demand, seed=5).run(60)
    k2 = twin(net, demand, seed=5).run(60)
    rf = [d for d in k1["disruptions"] if d["kind"] == "random_failure"]
    assert rf and rf == [d for d in k2["disruptions"] if d["kind"] == "random_failure"]
    assert not [d for d in twin(net, demand, random_failures=False).run(60)["disruptions"] if d["kind"] == "random_failure"]


def test_fulfilment_falls_back_to_nearest_dc_with_stock(net, demand):
    t = twin(net, demand, random_failures=False)
    t.apply({"type": "node_status", "node": "DC_HYD_MEDCHAL", "factor": 0.0})
    t.run_until(72)
    fmcg = [e for e in t.events if e["event"] == "order" and e["zone"] == "Z_HYD" and e["sku"] == "SKU_FMCG"]
    assert fmcg and all(e["dc"] == "DC_HYD_SHAMSHABAD" and e.get("fallback_dc") for e in fmcg if e["filled"] == e["qty"])


def test_port_berths_and_anchorage(net, demand):
    sc = Scenario.load(SCENARIO_TEMPLATES / "port_closure.json")
    t = twin(net, demand, scenarios=[sc])
    t.run_until(200)
    assert any(e["event"] == "anchorage" and e["port"] == "PORT_CHENNAI" for e in t.events)


def test_realtime_environment_paces_wall_clock(net, demand):
    t = twin(net, demand, realtime_factor=36.0)  # 36 wall-s per sim-hour -> 0.01 h = 0.36 s
    t0 = time.perf_counter()
    t.run_until(0.01)
    assert 0.3 < time.perf_counter() - t0 < 1.5
    assert isinstance(t.env, simpy.rt.RealtimeEnvironment)


def test_calibrated_hyderabad_lanes(net, demand):
    t = twin(net, demand)
    if not t.calibrated_lanes:
        pytest.skip("SUMO calibration not built (python -m sim.micro.calibrate)")
    assert {"L015", "L030", "L031"} <= set(t.calibrated_lanes)
    m, _ = t.lanes["L015"].mean_var_h()
    assert 4 < m < net.lanes["L015"].lt_mean_h  # handling + SUMO drive, faster than the 38 km/h prior

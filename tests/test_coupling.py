"""Phase 3b + 3c: SUMO worker process, corridor calibration, macro <-> micro coupling (FR-5 handshake)."""
import json

import pytest

from sim.coupling.orchestrator import CoupledTwin, bbox_crossing
from sim.macro.engine import Twin
from sim.micro.build_hyderabad import BBOX
from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.paths import HYDERABAD, SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

from .conftest import START

pytestmark = pytest.mark.skipif(not CFG.exists(), reason="micro-twin not built (python -m sim.micro.build_hyderabad)")


@pytest.fixture(scope="module")
def micro():
    with MicroProcess(seed=3, background=False, fcd_every=5) as m:
        yield m


def test_worker_commands_and_outputs(micro):
    assert "Patancheru Plant" in micro.hubs
    t0 = micro.call("status")["t"]
    assert micro.call("spawn_truck", vid="w1", from_hub="Patancheru Plant", to_hub="Medchal DC", depart=t0 + 30,
                      meta={"shipment": "SHX"})
    ct = micro.call("corridor_times", pairs=[["Patancheru Plant", "Medchal DC"]])["Patancheru Plant|Medchal DC"]
    out, t = {"arrivals": []}, t0
    while not out["arrivals"] and t < t0 + 3 * ct + 600:
        t += 300
        out = micro.step_to(t, edge_stats=True)
        assert out["t"] == t
        if out["fcd"]:
            row = out["fcd"][0]["vehicles"][0] if out["fcd"][0]["vehicles"] else None
            if row:
                assert {"id", "lon", "lat", "speed", "angle", "edge", "shipment"} <= set(row)
                w, s, e, n = BBOX
                assert w - 0.05 < row["lon"] < e + 0.05 and s - 0.05 < row["lat"] < n + 0.05
    arr = out["arrivals"][0]
    assert arr["vehicle"] == "w1" and arr["depart"] == t0 + 30 and arr["meta"]["shipment"] == "SHX"
    assert 0.7 * ct < arr["travel_s"] < 1.6 * ct + 120  # SUMO drive agrees with the router's estimate
    assert out["edge_stats"] and out["edge_stats"][0]["n"] >= 1


def test_close_and_reopen_commands(micro):
    edges = json.loads((SCENARIO_TEMPLATES / "road_flood.json").read_text())["params"]["sumo_edges"]
    assert micro.call("close_road", edges=edges)["closed"] == sorted(edges)
    assert micro.call("reopen_road", edges=edges)["closed"] == []
    assert isinstance(micro.call("reroute_all"), int)


def test_calibration_export():
    cal = json.loads((HYDERABAD / "calibration.json").read_text())
    assert {"L015", "L016", "L047", "L030", "L031"} == set(cal["lanes"]) and cal["arrived"] == cal["trucks"]
    for v in cal["lanes"].values():
        assert v["n"] >= 10 and v["drive_p10_h"] <= v["drive_p50_h"] <= v["drive_p90_h"] and v["drive_sigma"] > 0
        assert v["calibrated_lt_mean_h"] == pytest.approx(v["handling_h"] + v["drive_mean_h"], abs=1e-3)


def test_bbox_crossing():
    lat, lon = bbox_crossing(21.15, 79.09, 17.63, 78.48)  # Nagpur -> Medchal enters through the north edge
    assert lat == pytest.approx(BBOX[3], abs=1e-4) and BBOX[0] < lon < BBOX[2]


def test_fr5_handshake_macro_micro_macro(net, demand):
    """A shipment spawned in macro arrives in SUMO and is received back in macro, clocks within one sync step."""
    twin = Twin(net, demand, START, seed=42)
    with MicroProcess(seed=1, background=False) as m:
        ct = CoupledTwin(twin, m, sync_s=60)
        ct.run_until(30)
        assert ct.handshakes and ct.fallbacks == 0
        for h in ct.handshakes:
            assert 0 <= h["lag_s"] <= 60 + 1e-6
            assert h["micro_arrival_h"] > h["enter_h"]
        sid = ct.handshakes[0]["shipment"]
        kinds = [e["event"] for e in twin.events if e.get("shipment") == sid]
        assert kinds.index("city_enter") < kinds.index("city_arrive")
        if "receipt" in kinds:
            assert kinds.index("city_arrive") < kinds.index("receipt")
        enter = next(e for e in twin.events if e["event"] == "city_enter" and e["shipment"] == sid)
        assert enter["sumo_t"] == pytest.approx((enter["t_h"] - ct.t0_h) * 3600, abs=1.0)  # same clock
        assert ct.snapshot()["city"]["handshakes"] == len(ct.handshakes)


def test_road_closure_updates_macro_lane_multiplier(net, demand):
    """Closing roads in SUMO -> routed corridor times -> macro lane multipliers; reopening restores them."""
    twin = Twin(net, demand, START, seed=42)
    with MicroProcess(seed=1, background=False) as m:
        ct = CoupledTwin(twin, m, sync_s=60)
        # the ORR has parallel service roads, so one cut costs little: keep cutting the middle third of
        # whatever SUMO now considers fastest until the corridor is substantially slower
        pair = ["Patancheru Plant", "Shamshabad DC"]
        base = ct.base_corridor_s[tuple(pair)]
        cut: list[str] = []
        for _ in range(8):
            route = [e for e in m.call("corridor_route", from_hub=pair[0], to_hub=pair[1]) if not e.startswith(":")]
            cut += route[len(route) // 3: 2 * len(route) // 3]
            m.call("close_road", edges=cut)
            if m.call("corridor_times", pairs=[pair])["|".join(pair)] > 1.3 * base:
                break
        m.call("reopen_road", edges=cut)
        res = ct.close_road(cut)
        assert "L015" in res["lane_multipliers"] and res["lane_multipliers"]["L015"] > 1.0
        assert twin.lanes["L015"].live_mult == res["lane_multipliers"]["L015"]
        mults = [e for e in twin.events if e["event"] == "lane_multiplier" and e["lane"] == "L015"]
        assert mults[-1]["source"] == "sumo-routed"
        ct.reopen_road(cut)
        assert all(twin.lanes[l].live_mult == pytest.approx(1.0) for l in ct.plans)


def test_coupled_flood_replaces_static_lane_slowdown(net, demand):
    sc = Scenario.load(SCENARIO_TEMPLATES / "road_flood.json")
    twin = Twin(net, demand, START, seed=42, scenarios=[sc])
    with MicroProcess(seed=1, background=False) as m:
        ct = CoupledTwin(twin, m)
        ct.run_until(3)
        eff = next(e for e in twin.effects if e.type == "road_flood")
        assert not set(eff.lane_mult) & set(ct.plans)  # SUMO decides the city lanes
        assert any(e["event"] == "road_closed" for e in twin.events)

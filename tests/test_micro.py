"""SUMO micro-twin: build artefacts, trucks move on real roads, flood closure reroutes, speed."""
import json

import pytest

from sim.micro.runner import CFG, MicroTwin
from sim.micro.build_hyderabad import BBOX
from sim.paths import HYDERABAD, SCENARIO_TEMPLATES

pytestmark = pytest.mark.skipif(not CFG.exists(), reason="micro-twin not built (python -m sim.micro.build_hyderabad)")


@pytest.fixture(scope="module")
def mt():
    m = MicroTwin(seed=1)
    yield m
    m.close()


def test_hubs_snapped_close():
    hubs = json.loads((HYDERABAD / "hubs.json").read_text())
    assert {"Medchal DC", "Shamshabad DC", "Patancheru Plant"} <= set(hubs)
    assert all(h["snap_m"] < 1000 and h["parking_area"].startswith("pa_") for h in hubs.values())


def test_truck_vtype():
    txt = (HYDERABAD / "vtypes.add.xml").read_text()
    assert 'vClass="truck"' in txt and 'maxSpeed="22"' in txt and 'length="12"' in txt


def test_trucks_move_inside_bbox(mt):
    assert mt.spawn_truck("t_move", "Patancheru Plant", "Shamshabad DC")
    mt.step(30)
    a = mt.positions(["t_move"])[0]
    mt.step(120)
    b = mt.positions(["t_move"])[0]
    moved = abs(a["lat"] - b["lat"]) + abs(a["lon"] - b["lon"])
    assert moved > 0.005  # > ~500 m in two minutes
    w, s, e, n = BBOX
    assert w - 0.05 < b["lon"] < e + 0.05 and s - 0.05 < b["lat"] < n + 0.05
    assert b["speed"] <= 22.5


def test_flood_closure_reroutes(mt):
    flood = json.loads((SCENARIO_TEMPLATES / "road_flood.json").read_text())["params"]["sumo_edges"]
    assert flood, "road_flood template has no SUMO edges"
    # find a hub pair whose fastest route uses the flooded ORR edges
    pair = None
    for a in mt.hubs:
        for b in mt.hubs:
            if a != b:
                r = mt.t.simulation.findRoute(mt.hubs[a]["edge"], mt.hubs[b]["edge"], vType="truck")
                if set(flood) & set(r.edges):
                    pair = (a, b)
                    break
        if pair:
            break
    assert pair, "no hub-to-hub route uses the flood edges"
    assert mt.spawn_truck("t_flood", *pair)
    mt.step(1)
    assert set(flood) & set(mt.t.vehicle.getRoute("t_flood"))
    mt.close_road(flood)
    route = mt.t.vehicle.getRoute("t_flood")
    idx = mt.t.vehicle.getRouteIndex("t_flood")
    assert not set(flood) & set(route[idx:]), "truck still routed over the flooded ORR"
    mt.reopen_road(flood)


def test_recorded_benchmark_faster_than_real_time():
    res = json.loads((HYDERABAD / "benchmark.json").read_text())
    assert res["trucks_spawned"] >= 500 and res["cars"] >= 3000
    assert res["peak_vehicles"] >= 3000
    assert res["real_time_factor"] > 1 and res["busiest_5min"]["real_time_factor"] > 1


@pytest.mark.slow
def test_benchmark_live():
    from sim.micro.bench import main
    assert main(["--trucks", "500", "--cars", "3000", "--minutes", "15"]) == 0



def test_run_cli():
    """The Phase 2 CLI still runs (own process: libsumo allows one simulation per process)."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "sim.micro.run", "--minutes", "12", "--trucks", "10"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "freight trucks spawned" in r.stdout and "real time" in r.stdout

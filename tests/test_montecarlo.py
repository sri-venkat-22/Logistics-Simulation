"""Phase 3a: Monte Carlo bands, deterministic seeds; engine verification (SRS §8); Simchi-Levi stress test."""
import importlib.util

import pytest

from sim.macro.montecarlo import RunSpec, monte_carlo
from sim.macro.resilience import stress_test
from sim.macro.verify import v1_ss_fill_rate, v2_eoq, v3_supplynetpy
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

from .conftest import START

CYCLONE = Scenario.load(SCENARIO_TEMPLATES / "cyclone.json")


def test_bands_are_ordered_and_reproducible():
    spec = RunSpec(days=10, scenarios=[CYCLONE])
    a = monte_carlo(spec, n=6, processes=1)
    b = monte_carlo(spec, n=6, processes=2)  # pool vs in-process: identical, ordered by seed
    assert a["kpis"] == b["kpis"] and a["series"] == b["series"] and a["seeds"] == list(range(42, 48))
    for band in a["kpis"].values():
        assert band["p10"] <= band["p50"] <= band["p90"]
    s = a["series"]["DC_HYD_SHAMSHABAD/SKU_VAX"]
    assert len(s["t_days"]) == 11 and all(lo <= mid <= hi for lo, mid, hi in zip(*(s["on_hand"][q] for q in ("p10", "p50", "p90"))))
    assert set(a["stockout_prob"]) == {f"{d}/{k}" for d, k in __import__("sim.macro.network", fromlist=["Network"]).Network().replenishment}
    assert a["tts"][0]["ttr_h"] == 120.0


def test_explicit_seeds_and_spec_hash():
    spec = RunSpec(days=5)
    r = monte_carlo(spec, seeds=[7, 3], processes=1)
    assert r["seeds"] == [7, 3] and r["n"] == 2
    assert RunSpec(days=5).spec_hash() == spec.spec_hash() != RunSpec(days=6).spec_hash()


@pytest.mark.slow
def test_nfr3_200_reps_30_days_under_20s():
    r = monte_carlo(RunSpec(days=30, scenarios=[CYCLONE]), n=200, processes=8)
    assert r["n"] == 200 and r["wall_s"] < 20


def test_v1_ss_fill_rate_matches_analytic():
    r = v1_ss_fill_rate(days=4000)
    assert r["pass"], r


def test_v2_eoq():
    r = v2_eoq(cycles=40)
    assert r["pass"] and r["all_filled"] and r["sim_argmin_Q"] == 1500, r


@pytest.mark.skipif(importlib.util.find_spec("SupplyNetPy") is None, reason="supplynetpy not installed")
def test_v3_cross_check_supplynetpy():
    r = v3_supplynetpy(seeds=6, hours=6000)
    assert r["pass"], r


def test_stress_test_finds_single_points_of_failure(net, demand):
    res = stress_test(net, demand, START, days=30, nodes=["SUP_AHMEDABAD_TEX", "PORT_MUNDRA", "PLANT_PATANCHERU"])
    rows = {r["node"]: r for r in res["nodes"]}
    assert rows["SUP_AHMEDABAD_TEX"]["exposed"] and rows["SUP_AHMEDABAD_TEX"]["rei"] == 1.0
    assert rows["PLANT_PATANCHERU"]["tts_days"] < rows["PLANT_PATANCHERU"]["ttr_days"]
    assert rows["PORT_MUNDRA"]["tts_days"] is None and not rows["PORT_MUNDRA"]["exposed"]

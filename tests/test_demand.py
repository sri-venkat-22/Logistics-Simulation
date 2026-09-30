"""Demand = base x weekly seasonality x festival spikes, NB noise fitted from DataCo."""
import json
from datetime import date

import numpy as np

from sim.macro.demand import DemandModel
from sim.paths import DATA


def test_fitted_params_provenance():
    p = json.loads((DATA / "demand" / "demand_params.json").read_text())
    assert "8gx2fvg2k6" in p["source"] and p["rows_used"] > 100_000
    for fam in ("vaccine", "fmcg", "electronics"):
        f = p["families"][fam]
        assert len(f["dow"]) == 7 and abs(sum(f["dow"]) / 7 - 1) < 1e-3
        assert f["nb_k"] is None or f["nb_k"] > 0


def test_diwali_spike(demand: DemandModel):
    diwali = date(2026, 11, 8)
    assert abs(demand.festival_multiplier("fmcg", diwali) - 1.6) < 1e-9
    assert abs(demand.festival_multiplier("electronics", diwali) - 1.6) < 1e-9
    assert demand.festival_multiplier("vaccine", diwali) == 1.0
    assert demand.festival_multiplier("fmcg", date(2026, 9, 1)) == 1.0
    ramp = [demand.festival_multiplier("fmcg", date(2026, 10, d)) for d in range(27, 32)]
    assert ramp == sorted(ramp) and 1.0 <= ramp[0] < ramp[-1] < 1.6


def test_mean_scales_with_population(demand: DemandModel):
    d = date(2026, 9, 2)
    assert demand.mean("Z_DEL", "SKU_FMCG", d) > demand.mean("Z_VJA", "SKU_FMCG", d) * 10


def test_negative_binomial_moments(demand: DemandModel):
    rng = np.random.default_rng(0)
    d = date(2026, 9, 2)
    x = np.array([demand.sample(rng, "Z_HYD", "SKU_ELEC", d) for _ in range(20000)])
    mu = demand.mean("Z_HYD", "SKU_ELEC", d)
    assert abs(x.mean() / mu - 1) < 0.03
    assert abs(x.var() / demand.variance(mu, "SKU_ELEC") - 1) < 0.12

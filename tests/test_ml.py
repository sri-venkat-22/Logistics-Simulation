"""Phase 7.3: ML models - ETA quantiles, demand forecasting feeding (s,S), feed anomaly detection."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from ml.eta import EtaModel, twin_legs
from ml.forecast import EtsForecaster, fit_forecasts
from sim.macro.demand import DemandModel
from sim.macro.network import Network


def test_twin_legs_record_realised_transit_with_features():
    rows = twin_legs(days=6)
    assert len(rows) > 100
    r = rows[0]
    assert {"lane", "mode", "sched_h", "hour", "weekday", "weather_mult", "actual_h"} <= set(r)
    ratio = np.array([x["actual_h"] / x["sched_h"] for x in rows])
    assert 0.6 < np.median(ratio) < 1.6 and all(x["weather_mult"] >= 1 for x in rows)


@pytest.mark.skipif(not EtaModel.available(), reason="python -m ml.eta not run")
def test_eta_model_quantiles_are_ordered_and_react_to_weather():
    net = Network()
    m = EtaModel()
    t = datetime(2026, 10, 20, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
    a = m.predict(net, "L025", t)
    b = m.predict(net, "L025", t, weather_mult=2.5)
    assert a["p10_h"] <= a["p50_h"] <= a["p90_h"] and 0.5 * a["scheduled_h"] < a["p50_h"] < 2 * a["scheduled_h"]
    assert b["p50_h"] > a["p50_h"]


def test_ets_forecasts_and_the_policy_hook():
    net = Network()
    dm = DemandModel(net)
    rng = np.random.default_rng(1)
    first = date(2026, 9, 1)
    hist = {("Z_HYD", "SKU_FMCG"): np.array([dm.sample(rng, "Z_HYD", "SKU_FMCG", date.fromordinal(first.toordinal() + i))
                                            for i in range(56)], float)}
    fc = fit_forecasts(hist, first, dm, net, horizon=14)
    y = fc["ets_cal"][("Z_HYD", "SKU_FMCG")]
    assert len(y) == 14 and (y > 0).all()
    mean = dm.base_mean("Z_HYD", "SKU_FMCG")
    assert 0.5 * mean < y.mean() < 1.8 * mean
    f = EtsForecaster(dm, {("Z_HYD", "SKU_FMCG"): {date(2026, 10, 27): 999.0}})
    assert f.mean("Z_HYD", "SKU_FMCG", date(2026, 10, 27)) == 999.0
    assert f.mean("Z_HYD", "SKU_FMCG", date(2026, 12, 1)) == dm.mean("Z_HYD", "SKU_FMCG", date(2026, 12, 1))
    from sim.macro.engine import Twin
    twin = Twin(net, dm, datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")), log_events=False)
    pol = twin.warehouses["DC_HYD_MEDCHAL"].policy["SKU_FMCG"]
    s0 = pol.levels(twin, "DC_HYD_MEDCHAL", "SKU_FMCG", 0.0)[0]
    twin.forecaster = type("Double", (), {"mean": staticmethod(lambda z, s, d: 2 * dm.mean(z, s, d))})()
    assert pol.levels(twin, "DC_HYD_MEDCHAL", "SKU_FMCG", 0.0)[0] > 1.5 * s0   # reorder points follow the forecaster

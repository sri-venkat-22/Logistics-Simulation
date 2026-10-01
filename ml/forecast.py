"""Demand forecasting (Phase 7.3): statsforecast AutoETS per zone x SKU, feeding the (s,S) reorder points.

    python -m ml.forecast                   # -> docs/ml/forecast.json

History = the Reality Emulator's realised daily demand (its hidden demand drift included) from 15 Aug 2026;
train to 15 Oct, test the next 28 days, which contain the Diwali ramp. Forecasters compared on the test window
(WAPE = sum |error| / sum demand, over all 36 zone x SKU series):
    seasonal naive   last week repeated
    AutoETS          statsforecast AutoETS, weekly season, fitted per series
    AutoETS+calendar AutoETS on the festival-adjusted history, times the known festival multiplier
    planning model   the twin's own demand model (DataCo-fitted base x weekday x festival; blind to the drift)
Then the policy test: the same reality (same seed) runs the test window twice, once with the default
reorder points (planning model) and once with reorder points driven by the AutoETS+calendar forecasts
(Twin.forecaster), and the fill rate / stock-outs / inventory are compared.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from sim.paths import ROOT

REPORT = ROOT / "docs" / "ml" / "forecast.json"
IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 8, 15, tzinfo=IST)
TRAIN_END = date(2026, 10, 15)
HORIZON = 28
SEED = 4042


class EtsForecaster:
    """Forecast means per (zone, sku, date) for DynamicSSPolicy (Twin.forecaster); falls back to the demand
    model outside the forecast window."""

    def __init__(self, demand, forecasts: dict[tuple[str, str], dict[date, float]]):
        self.demand, self.forecasts = demand, forecasts

    def mean(self, zone: str, sku: str, d: date) -> float:
        v = self.forecasts.get((zone, sku), {}).get(d)
        return v if v is not None else self.demand.mean(zone, sku, d)


def daily_demand(twin, first: date, days: int) -> dict[tuple[str, str], np.ndarray]:
    out: dict[tuple[str, str], np.ndarray] = defaultdict(lambda: np.zeros(days))
    for o in twin.orders:
        d = (twin.start + timedelta(hours=o.created_h)).date()
        i = (d - first).days
        if 0 <= i < days:
            out[(o.zone, o.sku)][i] += o.qty
    return out


def fit_forecasts(history: dict[tuple[str, str], np.ndarray], first: date, demand, net, horizon: int = HORIZON) -> dict:
    """AutoETS and AutoETS+calendar forecasts per series for the `horizon` days after the history."""
    from statsforecast.models import AutoETS
    n = len(next(iter(history.values())))
    days_hist = [first + timedelta(days=i) for i in range(n)]
    days_fc = [first + timedelta(days=n + i) for i in range(horizon)]
    out: dict[str, dict] = {"ets": {}, "ets_cal": {}, "naive": {}}
    for (z, sku), y in history.items():
        fam = net.skus[sku].family
        cal_h = np.array([demand.festival_multiplier(fam, d) for d in days_hist])
        cal_f = np.array([demand.festival_multiplier(fam, d) for d in days_fc])
        out["ets"][(z, sku)] = np.maximum(AutoETS(season_length=7).fit(y.astype(float)).predict(horizon)["mean"], 0)
        adj = AutoETS(season_length=7).fit((y / cal_h).astype(float)).predict(horizon)["mean"]
        out["ets_cal"][(z, sku)] = np.maximum(adj * cal_f, 0)
        out["naive"][(z, sku)] = np.array([y[n - 7 + (i % 7)] for i in range(horizon)], float)
    return out


def wape(pred: dict, actual: dict) -> float:
    err = sum(float(np.abs(pred[k] - actual[k]).sum()) for k in actual)
    tot = sum(float(actual[k].sum()) for k in actual)
    return err / tot if tot else 0.0


def run(horizon: int = HORIZON) -> dict:
    from sim.reality.emulator import RealityEmulator
    from sim.reality.telemetry import MemorySink
    t0 = time.perf_counter()
    first = START.date()
    n_train = (TRAIN_END - first).days
    em = RealityEmulator(START, seed=SEED, sink=MemorySink(), national_gps=False, stock_period_s=30 * 86_400,
                         port_period_s=30 * 86_400)
    em.run_until((n_train + horizon + 1) * 24)
    twin, demand, net = em.twin, em.twin.demand, em.twin.net
    series = daily_demand(twin, first, n_train + horizon)
    hist = {k: v[:n_train] for k, v in series.items()}
    actual = {k: v[n_train:] for k, v in series.items()}
    fc = fit_forecasts(hist, first, demand, net, horizon)
    days_fc = [first + timedelta(days=n_train + i) for i in range(horizon)]
    fc["model"] = {k: np.array([demand.mean(k[0], k[1], d) for d in days_fc]) for k in actual}
    scores = {m: round(wape(fc[m], actual), 4) for m in ("naive", "ets", "ets_cal", "model")}
    by_family = {}
    for fam in sorted({s.family for s in net.skus.values()}):
        keys = [k for k in actual if net.skus[k[1]].family == fam]
        by_family[fam] = {m: round(wape({k: fc[m][k] for k in keys}, {k: actual[k] for k in keys}), 4) for m in fc}
    diwali = [i for i, d in enumerate(days_fc) if demand.festival_multiplier("fmcg", d) > 1.0]
    festive = {m: round(wape({k: fc[m][k][diwali] for k in actual}, {k: actual[k][diwali] for k in actual}), 4) for m in fc} if diwali else {}
    policy = policy_test(n_train, horizon, fc["ets_cal"], days_fc)
    return {"generated": datetime.now(IST).isoformat(timespec="seconds"),
            "history": {"source": f"Reality Emulator seed {SEED} (hidden demand drift)", "from": str(first),
                        "train_to": str(TRAIN_END), "test_days": horizon, "series": len(actual),
                        "test_window": [str(days_fc[0]), str(days_fc[-1])], "festive_days_in_test": len(diwali)},
            "wape": scores, "wape_by_family": by_family, "wape_festive_days": festive, "policy_test": policy,
            "wall_s": round(time.perf_counter() - t0, 1)}


def policy_test(n_train: int, horizon: int, fc: dict, days_fc: list[date]) -> dict:
    """Same reality twice over the test window: default reorder points vs AutoETS+calendar-driven ones."""
    from sim.reality.emulator import RealityEmulator
    from sim.reality.telemetry import MemorySink
    out = {}
    for label in ("planning_model", "ets_calendar"):
        em = RealityEmulator(START, seed=SEED, sink=MemorySink(), national_gps=False, stock_period_s=30 * 86_400,
                             port_period_s=30 * 86_400)
        twin = em.twin
        em.run_until(n_train * 24)
        if label == "ets_calendar":
            twin.forecaster = EtsForecaster(twin.demand, {k: dict(zip(days_fc, v.tolist())) for k, v in fc.items()})
        twin.reset_stats()
        em.run_until((n_train + horizon) * 24)
        k = twin.kpis(horizon)
        out[label] = {"fill_rate": round(k["fill_rate"], 4), "otif": round(k["otif"], 4), "units_backordered": k["units_backordered"],
                      "stockout_episodes": k["stockout_episodes"], "inventory_days": round(k["inventory_days"], 2),
                      "cost_lakh": round(k["cost_inr"]["total"] / 1e5, 1)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ml.forecast", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    r = run()
    w = r["wape"]
    print(f"Demand forecast · {r['history']['series']} series · test {r['history']['test_window'][0]} → {r['history']['test_window'][1]}"
          f" ({r['history']['festive_days_in_test']} festive days)")
    print(f"  WAPE  seasonal naive {w['naive'] * 100:.1f} % · AutoETS {w['ets'] * 100:.1f} % · AutoETS+calendar "
          f"{w['ets_cal'] * 100:.1f} % · planning model {w['model'] * 100:.1f} %")
    if r["wape_festive_days"]:
        f = r["wape_festive_days"]
        print(f"  festive days: AutoETS {f['ets'] * 100:.1f} % · AutoETS+calendar {f['ets_cal'] * 100:.1f} % · model {f['model'] * 100:.1f} %")
    for k, v in r["policy_test"].items():
        print(f"  (s,S) driven by {k:<15} fill {v['fill_rate'] * 100:.2f} % · stock-outs {v['stockout_episodes']} · "
              f"inventory {v['inventory_days']:.1f} d · cost ₹{v['cost_lakh']:.1f} L")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(r, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

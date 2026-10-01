"""ETA / delay models (Phase 7.3): LightGBM quantile regression (P10 / P50 / P90).

    python -m ml.eta                       # train both, write ml/models/*.txt and docs/ml/eta.json
    python -m ml.eta --twin-only           # skip DataCo (e.g. when the CSV is not present)

1. Twin model - "reality-emulator history". The Reality Emulator (its own seed, hidden lane bias, hidden
   incidents, demand drift) runs 360 days with announced weather episodes; every shipment leg is recorded at
   departure with what a dispatcher knows then: lane, mode, distance, scheduled mean, hour, weekday, the
   announced weather multiplier on the lane, SUMO congestion calibration. Target: the realised transit hours
   (as a ratio to the schedule). Time split: the last 20 % of days is held out.
2. DataCo model - real outcomes. Days for shipping (real) from shipping mode, scheduled days, market, region,
   category, customer segment, month / weekday / hour of the order (no leaking columns); plus a classifier for
   Late_delivery_risk (AUC). Time split by order date (last 20 %).
Reported: MAE of the P50 vs the schedule baseline, P10-P90 interval coverage (target ~ 80 %), AUC.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from sim.paths import DATA, ROOT

MODELS = ROOT / "ml" / "models"
REPORT = ROOT / "docs" / "ml" / "eta.json"
QUANTILES = (0.1, 0.5, 0.9)
TWIN_FEATURES = ["lane_code", "mode_code", "distance_km", "sched_h", "hour", "weekday", "weather_mult", "calibrated"]
MODES = {"road": 0, "rail": 1, "sea": 2, "air": 3}
LGB_PARAMS: dict[str, Any] = dict(n_estimators=250, learning_rate=0.05, num_leaves=15, min_child_samples=40, subsample=0.9,
                  subsample_freq=1, colsample_bytree=0.9, random_state=7, verbose=-1)


# ============================================================================================ twin legs
def twin_legs(days: float = 360.0, seed: int = 3042, weather_every_h: float = 30.0) -> list[dict]:
    """Shipment legs from a Reality Emulator run (no telemetry needed), with announced weather episodes."""
    from sim.macro.disruptions import Effect
    from sim.reality.emulator import RealityEmulator
    from sim.reality.telemetry import MemorySink
    em = RealityEmulator(datetime(2026, 7, 1, tzinfo=ZoneInfo("Asia/Kolkata")), seed=seed, sink=MemorySink(),
                         national_gps=False, stock_period_s=10 * 86_400, port_period_s=10 * 86_400)
    twin = em.twin
    rng = np.random.default_rng([seed, 0xE7A])
    net = twin.net
    lanes = [l for l in net.lanes.values() if not l.transfer_only]
    lane_code = {lid: i for i, lid in enumerate(sorted(net.lanes))}

    def weather():
        while True:
            yield twin.env.timeout(float(rng.exponential(weather_every_h)))
            ln = lanes[int(rng.integers(len(lanes)))]
            m = float(rng.uniform(1.3, 3.0))
            twin.add_effect(Effect(label=f"weather {ln.id}", kind="scenario", type="weather", target=ln.id,
                                   lane_mult={ln.id: m}), float(rng.uniform(12, 72)))

    twin.env.process(weather())
    rows: list[dict] = []
    orig = twin.set_status

    def spy(shp, status, where, dur_h, seg_from=None, seg_to=None):
        if status == "transit" and where in net.lanes and seg_from is None and shp.created_h >= 0 and dur_h > 0:
            ln = net.lanes[where]
            announced = 1.0
            for e in twin.effects:
                if e.kind != "hidden":
                    announced *= e.lane_mult.get(where, 1.0)
            t = twin.now_dt()
            rows.append({"t_h": twin.env.now, "lane": where, "lane_code": lane_code[where], "mode": ln.mode,
                         "mode_code": MODES[ln.mode], "distance_km": ln.distance_km, "sched_h": ln.lt_mean_h,
                         "hour": t.hour + t.minute / 60, "weekday": t.weekday(), "weather_mult": announced,
                         "calibrated": 1 if twin.lanes[where].calibration else 0, "actual_h": dur_h})
        return orig(shp, status, where, dur_h, seg_from, seg_to)

    twin.set_status = spy  # type: ignore[method-assign]
    em.run_until(days * 24)
    return rows


def _fit_quantiles(x_tr, y_tr, cat=None) -> dict:
    import lightgbm as lgb
    models = {}
    for q in QUANTILES:
        m = lgb.LGBMRegressor(objective="quantile", alpha=q, **LGB_PARAMS)
        m.fit(x_tr, y_tr, categorical_feature=cat or "auto")
        models[q] = m
    return models


def _report(y, p10, p50, p90, baseline) -> dict:
    y = np.asarray(y, float)
    return {"n": int(len(y)), "mae_h": round(float(np.mean(np.abs(p50 - y))), 3),
            "baseline_mae_h": round(float(np.mean(np.abs(baseline - y))), 3),
            "mape": round(float(np.mean(np.abs(p50 - y) / np.maximum(y, 1e-6))), 4),
            "p10_p90_coverage": round(float(np.mean((y >= p10) & (y <= p90))), 4),
            "below_p10": round(float(np.mean(y < p10)), 4), "above_p90": round(float(np.mean(y > p90)), 4),
            "interval_width_h": round(float(np.mean(p90 - p10)), 3)}


def train_twin(days: float = 360.0) -> dict:
    t0 = time.perf_counter()
    rows = twin_legs(days)
    rows.sort(key=lambda r: r["t_h"])
    cut = days * 24 * 0.8
    tr = [r for r in rows if r["t_h"] < cut]
    te = [r for r in rows if r["t_h"] >= cut]
    X = lambda rs: np.array([[r[f] for f in TWIN_FEATURES] for r in rs], float)  # noqa: E731
    y_ratio = np.array([r["actual_h"] / r["sched_h"] for r in tr])
    models = _fit_quantiles(X(tr), y_ratio, cat=[0, 1])
    sched = np.array([r["sched_h"] for r in te])
    xt = X(te)
    p = {q: models[q].predict(xt) * sched for q in QUANTILES}
    p10, p50, p90 = np.minimum(p[0.1], p[0.5]), p[0.5], np.maximum(p[0.9], p[0.5])
    y = np.array([r["actual_h"] for r in te])
    rep = _report(y, p10, p50, p90, sched * np.array([r["weather_mult"] for r in te]))
    by_mode = {}
    for mode in MODES:
        idx = np.array([r["mode"] == mode for r in te])
        if idx.sum() >= 20:
            by_mode[mode] = _report(y[idx], p10[idx], p50[idx], p90[idx], (sched * np.array([r["weather_mult"] for r in te]))[idx])
    MODELS.mkdir(parents=True, exist_ok=True)
    for q, m in models.items():
        m.booster_.save_model(str(MODELS / f"eta_twin_p{int(q * 100)}.txt"))
    imp = dict(zip(TWIN_FEATURES, map(int, models[0.5].booster_.feature_importance("gain"))))
    return {"dataset": {"source": "Reality Emulator (seed 3042, hidden lane bias + incidents, announced weather)",
                        "days": days, "legs": len(rows), "train": len(tr), "test": len(te), "split": "time, last 20 % held out"},
            "features": TWIN_FEATURES, "target": "realised transit hours / scheduled mean", "test": rep, "test_by_mode": by_mode,
            "feature_gain": imp, "wall_s": round(time.perf_counter() - t0, 1)}


# ============================================================================================ DataCo
DATACO = DATA / "dataco" / "DataCoSupplyChainDataset.csv"
DC_CATS = ["Shipping Mode", "Market", "Order Region", "Category Name", "Customer Segment"]


def load_dataco():
    import pandas as pd
    df = pd.read_csv(DATACO, encoding="latin-1", usecols=["Days for shipping (real)", "Days for shipment (scheduled)",
                                                           "Late_delivery_risk", "order date (DateOrders)", *DC_CATS])
    df["order_ts"] = pd.to_datetime(df["order date (DateOrders)"], format="%m/%d/%Y %H:%M")
    df["month"], df["weekday"], df["hour"] = df.order_ts.dt.month, df.order_ts.dt.weekday, df.order_ts.dt.hour
    for c in DC_CATS:
        df[c] = df[c].astype("category")
    return df.sort_values("order_ts").reset_index(drop=True)


def train_dataco() -> dict:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    t0 = time.perf_counter()
    df = load_dataco()
    feats = [*DC_CATS, "Days for shipment (scheduled)", "month", "weekday", "hour"]
    cut = int(len(df) * 0.8)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    y_tr, y_te = tr["Days for shipping (real)"].astype(float), te["Days for shipping (real)"].astype(float)
    models = _fit_quantiles(tr[feats], y_tr)
    p10, p50, p90 = (models[q].predict(te[feats]) for q in QUANTILES)
    p10, p90 = np.minimum(p10, p50), np.maximum(p90, p50)
    reg = _report(y_te.values, p10, p50, p90, te["Days for shipment (scheduled)"].values.astype(float))
    reg = {k.replace("_h", "_days") if k.endswith("_h") else k: v for k, v in reg.items()}
    clf = lgb.LGBMClassifier(objective="binary", **LGB_PARAMS)
    clf.fit(tr[feats], tr["Late_delivery_risk"])
    prob = np.asarray(clf.predict_proba(te[feats]))[:, 1]
    auc = roc_auc_score(te["Late_delivery_risk"], prob)
    base_auc = roc_auc_score(te["Late_delivery_risk"], te["Days for shipment (scheduled)"].map(lambda d: -d))
    MODELS.mkdir(parents=True, exist_ok=True)
    models[0.5].booster_.save_model(str(MODELS / "eta_dataco_p50.txt"))
    clf.booster_.save_model(str(MODELS / "late_dataco.txt"))
    return {"dataset": {"source": "DataCo Smart Supply Chain (Constante et al. 2019, CC BY 4.0)", "rows": int(len(df)),
                        "train": int(len(tr)), "test": int(len(te)), "split": "time by order date, last 20 % held out",
                        "test_from": str(te.order_ts.iloc[0].date())},
            "features": feats, "regression_days_real": reg,
            "late_delivery": {"auc": round(float(auc), 4), "baseline_auc_scheduled_days": round(float(base_auc), 4),
                              "late_share_test": round(float(te["Late_delivery_risk"].mean()), 4)},
            "wall_s": round(time.perf_counter() - t0, 1)}


# ============================================================================================ serving
class EtaModel:
    """The twin ETA quantile models, loaded from ml/models (for the API)."""

    def __init__(self, models_dir: Path = MODELS):
        import lightgbm as lgb
        self.boosters = {q: lgb.Booster(model_file=str(models_dir / f"eta_twin_p{int(q * 100)}.txt")) for q in QUANTILES}

    @staticmethod
    def available(models_dir: Path = MODELS) -> bool:
        return all((models_dir / f"eta_twin_p{int(q * 100)}.txt").exists() for q in QUANTILES)

    def predict(self, net, lane: str, when: datetime, weather_mult: float = 1.0, calibrated: bool = False) -> dict:
        ln = net.lanes[lane]
        code = {l: i for i, l in enumerate(sorted(net.lanes))}[lane]
        x = np.array([[code, MODES[ln.mode], ln.distance_km, ln.lt_mean_h, when.hour + when.minute / 60, when.weekday(),
                       weather_mult, 1 if calibrated else 0]], float)
        p = {q: float(self.boosters[q].predict(x)[0]) * ln.lt_mean_h for q in QUANTILES}
        p10, p50, p90 = min(p[0.1], p[0.5]), p[0.5], max(p[0.9], p[0.5])
        return {"lane": lane, "mode": ln.mode, "scheduled_h": ln.lt_mean_h, "p10_h": round(p10, 2), "p50_h": round(p50, 2),
                "p90_h": round(p90, 2), "eta_p50": (when + timedelta(hours=p50)).isoformat(timespec="minutes")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ml.eta", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=360.0)
    ap.add_argument("--twin-only", action="store_true")
    a = ap.parse_args(argv)
    out: dict[str, Any] = {"generated": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"), "quantiles": list(QUANTILES)}
    out["twin"] = train_twin(a.days)
    t = out["twin"]["test"]
    print(f"Twin ETA (LightGBM quantiles) · {out['twin']['dataset']['legs']:,} legs · test {t['n']:,}")
    print(f"  MAE {t['mae_h']:.2f} h (schedule baseline {t['baseline_mae_h']:.2f} h) · P10-P90 coverage {t['p10_p90_coverage'] * 100:.1f} %")
    if not a.twin_only and DATACO.exists():
        out["dataco"] = train_dataco()
        d = out["dataco"]
        r = d["regression_days_real"]
        print(f"DataCo · {d['dataset']['rows']:,} orders · test {d['dataset']['test']:,} from {d['dataset']['test_from']}")
        print(f"  days (real) MAE {r['mae_days']:.3f} d (scheduled baseline {r['baseline_mae_days']:.3f} d) · "
              f"P10-P90 coverage {r['p10_p90_coverage'] * 100:.1f} %")
        print(f"  late-delivery AUC {d['late_delivery']['auc']:.3f} (scheduled-days baseline {d['late_delivery']['baseline_auc_scheduled_days']:.3f})")
    elif REPORT.exists():
        prev = json.loads(REPORT.read_text())
        if "dataco" in prev:
            out["dataco"] = prev["dataco"]  # keep the last DataCo result
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

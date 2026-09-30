"""Fit demand shape and noise from the DataCo Smart Supply Chain dataset.

    .venv/bin/python data/demand/fit_dataco.py

Source: Constante, Silva, Pereira (2019), "DataCo SMART SUPPLY CHAIN FOR BIG DATA ANALYSIS",
Mendeley Data v5, doi:10.17632/8gx2fvg2k6.5 (CC BY 4.0). Place DataCoSupplyChainDataset.csv
in data/dataco/ (it is git-ignored; 96 MB).

What is fitted (written to data/demand/demand_params.json, which is committed):
  * day-of-week profile per SKU family (mean 1.0), from daily ordered quantities;
  * over-dispersion k of a negative-binomial (gamma-Poisson) model per family:
    Var = mu + mu^2 / k, estimated by moments on de-seasonalised daily quantities;
  * monthly profile (reported; DataCo is almost flat, so India seasonality comes from the
    festival calendar instead);
  * realised / scheduled shipping-time ratio per shipping mode (a cross-check for lane sigma).

DataCo is a US-style retail dataset, so each AEGIS family uses a proxy department set.
Sparse families are shrunk toward the all-orders profile: w = n / (n + 200 days), and a family
with < 60 active days borrows the all-orders dispersion k.
Window: 2015-01-01 .. 2017-09-30 (order volume and product mix change structurally after
Oct 2017). A proxy with < 1,000 rows in that window (Technology and Health & Beauty only
exist from Oct 2017) is fitted on its own active date range instead.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
CSV = HERE.parent / "dataco" / "DataCoSupplyChainDataset.csv"
OUT = HERE / "demand_params.json"
WINDOW = ("2015-01-01", "2017-09-30")
SHRINK_DAYS = 200.0
MIN_WINDOW_ROWS = 1000
MIN_K_DAYS = 60

# AEGIS family -> (DataCo proxy rule, human-readable description)
PROXIES = {
    "electronics": (lambda d: (d["Department Name"] == "Technology")
                    | d["Category Name"].isin(["Electronics", "Cameras", "Computers", "Consumer Electronics", "Video Games"]),
                    "Department 'Technology' + electronics categories"),
    "vaccine": (lambda d: d["Department Name"] == "Health and Beauty",
                "Department 'Health and Beauty' (closest health proxy; sparse, shrunk to global)"),
    "fmcg": (lambda d: ~d["Department Name"].isin(["Technology", "Health and Beauty"]),
             "All other departments (high-volume consumer goods)"),
}


def nb_k(x: np.ndarray) -> float | None:
    m, v = float(x.mean()), float(x.var(ddof=1))
    return round(m * m / (v - m), 3) if v > m else None  # None => no over-dispersion (Poisson)


def fit_family(daily: pd.Series, global_dow: np.ndarray) -> dict:
    daily = daily.asfreq("D", fill_value=0)
    by_dow = daily.groupby(daily.index.dayofweek).mean()
    raw = (by_dow / by_dow.mean()).reindex(range(7), fill_value=1.0).to_numpy()
    n = int((daily > 0).sum())
    w = n / (n + SHRINK_DAYS)
    dow = w * raw + (1 - w) * global_dow
    dow = dow / dow.mean()
    deseason = daily.to_numpy() / dow[daily.index.dayofweek]
    monthly = daily.groupby(daily.index.month).mean()
    return {
        "days": int(len(daily)), "active_days": n, "units": int(daily.sum()),
        "mean_units_per_day": round(float(daily.mean()), 3),
        "dow_raw": [round(x, 4) for x in raw], "shrink_weight": round(w, 3),
        "dow": [round(float(x), 4) for x in dow],
        "nb_k": nb_k(deseason),
        "cv_daily": round(float(deseason.std(ddof=1) / deseason.mean()), 4),
        "monthly": [round(float(x), 4) for x in (monthly / monthly.mean()).reindex(range(1, 13), fill_value=1.0)],
    }


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"missing {CSV} — download it from https://data.mendeley.com/datasets/8gx2fvg2k6/5")
    cols = ["order date (DateOrders)", "Order Item Quantity", "Category Name", "Department Name", "Order Status",
            "Days for shipping (real)", "Days for shipment (scheduled)", "Shipping Mode"]
    full = pd.read_csv(CSV, encoding="latin-1", usecols=cols)
    full["ts"] = pd.to_datetime(full["order date (DateOrders)"], format="%m/%d/%Y %H:%M")
    for c in ("Department Name", "Category Name", "Shipping Mode"):
        full[c] = full[c].str.strip()  # e.g. 'Health and Beauty ' has a trailing space
    full = full[~full["Order Status"].isin(["CANCELED", "SUSPECTED_FRAUD"])]
    df = full[(full.ts >= WINDOW[0]) & (full.ts < pd.Timestamp(WINDOW[1]) + pd.Timedelta(days=1))]

    all_daily = df.set_index("ts")["Order Item Quantity"].resample("D").sum()
    g = all_daily.groupby(all_daily.index.dayofweek).mean()
    global_dow = (g / g.mean()).to_numpy()
    result = {
        "source": "DataCo SMART SUPPLY CHAIN FOR BIG DATA ANALYSIS, Mendeley Data v5, doi:10.17632/8gx2fvg2k6.5 (CC BY 4.0)",
        "window": list(WINDOW), "rows_used": int(len(df)),
        "dow_index": "0 = Monday ... 6 = Sunday",
        "global": fit_family(all_daily, global_dow),
        "families": {},
    }
    for fam, (rule, desc) in PROXIES.items():
        sub = df[rule(df)]
        window = list(WINDOW)
        if len(sub) < MIN_WINDOW_ROWS:  # proxy only exists outside the stable window
            sub = full[rule(full)]
            window = [str(sub.ts.min().date()), str(sub.ts.max().date())]
        daily = sub.set_index("ts")["Order Item Quantity"].resample("D").sum()
        fit = fit_family(daily, global_dow)
        if fit["active_days"] < MIN_K_DAYS or fit["nb_k"] is None:
            fit["nb_k_note"] = f"only {fit['active_days']} active days: dispersion borrowed from all orders"
            fit["nb_k_own"], fit["nb_k"] = fit["nb_k"], result["global"]["nb_k"]
        result["families"][fam] = {"proxy": desc, "window": window, "rows": int(len(sub)), **fit}

    # realised / scheduled shipping time (days), lognormal fit of the ratio per mode
    lt = {}
    ok = df[df["Days for shipment (scheduled)"] > 0]
    for mode, sub in ok.groupby("Shipping Mode"):
        r = (sub["Days for shipping (real)"].clip(lower=0.5) / sub["Days for shipment (scheduled)"]).to_numpy()
        lr = np.log(r)
        lt[mode] = {"n": int(len(r)), "ratio_mean": round(float(r.mean()), 4), "ratio_p90": round(float(np.quantile(r, 0.9)), 4),
                    "log_mu": round(float(lr.mean()), 4), "log_sigma": round(float(lr.std(ddof=1)), 4),
                    "late_share": round(float((r > 1).mean()), 4)}
    result["lead_time_ratio"] = lt

    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"rows used: {result['rows_used']:,}  window: {WINDOW[0]} .. {WINDOW[1]}")
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for fam, f in result["families"].items():
        k = f["nb_k"] if f["nb_k"] is not None else math.inf
        print(f"  {fam:<12} rows={f['rows']:>7,}  shrink w={f['shrink_weight']:.2f}  NB k={k:<8}  CV={f['cv_daily']:.3f}  "
              f"window {f['window'][0]}..{f['window'][1]}  DOW " + " ".join(f"{n}:{x:.2f}" for n, x in zip(names, f["dow"])))
    for mode, v in lt.items():
        print(f"  lead-time ratio {mode:<15} mean={v['ratio_mean']:.2f}  sigma(log)={v['log_sigma']:.2f}  late={v['late_share']:.0%}")


if __name__ == "__main__":
    main()

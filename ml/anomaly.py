"""Feed anomaly detection (Phase 7.3): the L8 ASN detector (robust MAD z-score + IsolationForest), evaluated.

    python -m ml.anomaly                     # -> docs/ml/anomaly.json

Clean ASNs come from an attack-free Reality Emulator run with a different seed than the detector's training set
(data/benchmark/asn_train.json). Corrupted copies are made of each: quantity x10 (the red-team attack), x3,
x0.2, and an ETA before the ship time. Reported per detector (MAD only, IsolationForest only, the combined L8
rule): precision, recall, false-positive rate and ROC AUC of the anomaly score.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from services.api.app.trust_layers import FeedAnomaly, asn_features
from sim.paths import ROOT

REPORT = ROOT / "docs" / "ml" / "anomaly.json"
CORRUPT = {"qty_x10": lambda p: {**p, "qty": p["qty"] * 10}, "qty_x3": lambda p: {**p, "qty": p["qty"] * 3},
           "qty_x0.2": lambda p: {**p, "qty": p["qty"] * 0.2},
           "eta_before_ship": lambda p: {**p, "eta_ts": (datetime.fromisoformat(p["ship_ts"]) - timedelta(hours=2)).isoformat()}}


def clean_asns(days: float = 30.0, seed: int = 5042) -> list[dict]:
    from sim.reality.emulator import RealityEmulator
    from sim.reality.telemetry import MemorySink
    sink = MemorySink()
    em = RealityEmulator(datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Kolkata")), seed=seed, sink=sink, national_gps=False,
                         stock_period_s=86_400 * 30, port_period_s=86_400 * 30)
    em.run_until(days * 24)
    return [m["payload"] for m in sink.messages if m["kind"] == "asn"]


def evaluate() -> dict:
    from sklearn.metrics import roc_auc_score
    det = FeedAnomaly()
    clean = clean_asns()
    rows = [(p, 0, "clean") for p in clean] + [(f(p), 1, name) for p in clean for name, f in CORRUPT.items()]
    out: dict[str, Any] = {"clean": len(clean), "corrupted": len(rows) - len(clean), "detectors": {}, "by_corruption": {}}
    mad = np.array([det.z(p) for p, _, _ in rows])
    iso = np.array([-det.iforest.score_samples(np.array([asn_features(p)]))[0] for p, _, _ in rows]) if det.iforest else mad * 0
    y = np.array([lab for _, lab, _ in rows])
    verdict = {"mad": mad > det.z_max, "iforest": np.array([det.iforest.predict(np.array([asn_features(p)]))[0] == -1
                                                           for p, _, _ in rows]) if det.iforest else mad * 0 > 1,
               "l8_rule": np.array([det.check_asn(p) is not None for p, _, _ in rows])}
    over = np.array([asn_features(p)[0] for p, _, _ in rows]) - np.log(1.05)
    lead = np.array([asn_features(p)[1] for p, _, _ in rows])
    score = {"mad": mad, "iforest": iso,
             "l8_rule": np.maximum.reduce([mad / det.z_max, over * 10 + 1, (lead <= 0).astype(float) * 2])}
    for name, v in verdict.items():
        tp, fp = int((v & (y == 1)).sum()), int((v & (y == 0)).sum())
        fn = int((~v & (y == 1)).sum())
        out["detectors"][name] = {"precision": round(tp / (tp + fp), 4) if tp + fp else None,
                                  "recall": round(tp / (tp + fn), 4) if tp + fn else None,
                                  "false_positive_rate": round(fp / max(1, int((y == 0).sum())), 4),
                                  "roc_auc": round(float(roc_auc_score(y, score[name])), 4)}
    for name in CORRUPT:
        idx = np.array([k == name for _, _, k in rows])
        out["by_corruption"][name] = {d: round(float(v[idx].mean()), 4) for d, v in verdict.items()}
    out["mad_stats"] = {"median_log_qty_per_truck": round(det.med, 4), "robust_sigma": round(det.mad, 4), "z_max": det.z_max}
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m ml.anomaly", description=__doc__).parse_args(argv)
    r = evaluate()
    print(f"L8 ASN anomaly detection · {r['clean']} clean ASNs, {r['corrupted']} corrupted copies")
    for d, v in r["detectors"].items():
        print(f"  {d:<8} precision {v['precision']} · recall {v['recall']} · FPR {v['false_positive_rate']} · AUC {v['roc_auc']}")
    print(f"  recall by corruption (L8 rule): {({k: v['l8_rule'] for k, v in r['by_corruption'].items()})}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(r, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

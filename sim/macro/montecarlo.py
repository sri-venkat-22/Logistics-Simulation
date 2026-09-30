"""Monte Carlo what-ifs: N replications of a run spec -> P10 / P50 / P90 bands.

    from sim.macro.montecarlo import RunSpec, monte_carlo
    res = monte_carlo(RunSpec(days=30, scenarios=[...]), n=200)          # seeds = spec.seed + 0..n-1
    res["kpis"]["fill_rate"]      -> {"p10", "p50", "p90", "mean"}
    res["series"]["DC_HYD_SHAMSHABAD/SKU_VAX"]["on_hand"]["p10"]  -> daily band
    res["stockout_prob"]["DC_HYD_SHAMSHABAD/SKU_VAX"]           -> share of replications with a stock-out

Replications run in a multiprocessing.Pool (spawn-safe; each worker loads the network once). Results
depend only on (spec, seed): replication i always uses seed spec.seed + i (or seeds[i]), and the
output is ordered by seed, so the bands are reproducible regardless of scheduling. Different specs
with the same seeds share common random numbers (named random streams in the engine).
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import time
from datetime import datetime

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from sim.scenarios.dsl import Scenario

SCALARS = ("fill_rate", "otif", "backorder_units_end", "units_backordered", "inventory_days", "co2_t", "cost_total",
           "cost_transport", "cost_holding", "cost_penalty", "stockout_episodes")


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: float = Field(30, gt=0, le=365)
    start: str = "2026-10-15T00:00:00+05:30"
    seed: int = 42
    scenarios: list[Scenario] = Field(default_factory=list)
    policy: dict | None = None          # per-family overrides of FAMILY_POLICY
    random_failures: bool = True
    calibration: bool = True            # SUMO-calibrated Hyderabad lanes (if built)
    path_choice: dict[str, int] = Field(default_factory=dict)  # "DC/SKU" -> sourcing path index (plan actions)

    def canonical_json(self) -> str:
        d = self.model_dump(mode="json")
        d["scenarios"] = [json.loads(s.canonical_json()) for s in self.scenarios]
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


_WORLD: tuple | None = None


def _world():
    global _WORLD
    if _WORLD is None:
        from sim.macro.demand import DemandModel
        from sim.macro.network import Network
        net = Network()
        _WORLD = (net, DemandModel(net))
    return _WORLD


def run_one(spec: RunSpec, seed: int) -> dict:
    """One replication: scalar KPIs, daily series and stock-out / TTS facts (no event log, for speed)."""
    from sim.macro.engine import Twin
    net, demand = _world()
    twin = Twin(net, demand, datetime.fromisoformat(spec.start), seed=seed, scenarios=list(spec.scenarios),
                policy=spec.policy, random_failures=spec.random_failures,
                calibration="auto" if spec.calibration else None, log_events=False)
    for key, idx in spec.path_choice.items():
        dc, sku = key.split("/")
        twin.apply({"type": "set_path", "dc": dc, "sku": sku, "path": idx})
    k = twin.run(spec.days)
    twin.record_series()
    c = k["cost_inr"]
    scalars = {"fill_rate": k["fill_rate"], "otif": k["otif"], "backorder_units_end": k["backorder_units_end"],
               "units_backordered": k["units_backordered"], "inventory_days": k["inventory_days"], "co2_t": k["co2_t"],
               "cost_total": c["total"], "cost_transport": c["transport"], "cost_holding": c["holding"],
               "cost_penalty": c["penalty"], "stockout_episodes": k["stockout_episodes"]}
    scen = [d for d in k["disruptions"] if d["kind"] == "scenario"]
    return {
        "seed": seed, "scalars": scalars,
        "per_sku_fill": {s: v["fill_rate"] for s, v in k["per_sku"].items()},
        "series": twin.series,
        "stockout_hours": {p: v["stockout_hours"] for p, v in k["per_dc_sku"].items()},
        "tts_h": [d["tts_h"] for d in scen], "ttr_h": [d["ttr_h"] for d in scen],
        "exposed": [d["exposed"] for d in scen],
    }


def _job(args: tuple[str, int]) -> dict:
    spec_json, seed = args
    return run_one(RunSpec.model_validate_json(spec_json), seed)


def _band(x) -> dict:
    a = np.asarray(x, dtype=float)
    p10, p50, p90 = np.percentile(a, [10, 50, 90])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90), "mean": float(a.mean())}


def summarize(spec: RunSpec, runs: list[dict]) -> dict:
    n = len(runs)
    kpis = {name: _band([r["scalars"][name] for r in runs]) for name in SCALARS}
    per_sku = {sku: _band([r["per_sku_fill"].get(sku, 1.0) for r in runs]) for sku in runs[0]["per_sku_fill"]}
    pairs = list(runs[0]["series"][0]["on_hand"]) if runs[0]["series"] else []
    t_days = [round(s["t_h"] / 24, 3) for s in runs[0]["series"]]
    series = {}
    for p in pairs:
        oh = np.array([[s["on_hand"][p] for s in r["series"]] for r in runs])
        bl = np.array([[s["backlog"][p] for s in r["series"]] for r in runs])
        series[p] = {"t_days": t_days,
                     "on_hand": {q: np.percentile(oh, v, axis=0).round(1).tolist() for q, v in (("p10", 10), ("p50", 50), ("p90", 90))},
                     "backlog": {q: np.percentile(bl, v, axis=0).round(1).tolist() for q, v in (("p10", 10), ("p50", 50), ("p90", 90))}}
    fill_series = np.array([[s["fill_rate"] for s in r["series"]] for r in runs])
    stockout_prob = {p: sum(1 for r in runs if r["stockout_hours"][p] > 0) / n for p in runs[0]["stockout_hours"]}
    tts = []
    for i in range(len(spec.scenarios)):
        vals = [r["tts_h"][i] for r in runs if i < len(r["tts_h"])]
        hit = [v for v in vals if v is not None]
        tts.append({"scenario": spec.scenarios[i].name or spec.scenarios[i].type.value,
                    "ttr_h": runs[0]["ttr_h"][i] if i < len(runs[0]["ttr_h"]) else None,
                    "p_stockout": len(hit) / max(len(vals), 1),
                    "tts_h": _band(hit) if hit else None,
                    "p_exposed": sum(1 for r in runs if i < len(r["exposed"]) and r["exposed"][i]) / n})
    return {
        "n": n, "seeds": [r["seed"] for r in runs], "spec_hash": spec.spec_hash(), "days": spec.days,
        "kpis": kpis, "per_sku_fill": per_sku,
        "fill_rate_series": {"t_days": t_days, **{q: np.percentile(fill_series, v, axis=0).round(4).tolist()
                                                  for q, v in (("p10", 10), ("p50", 50), ("p90", 90))}},
        "series": series, "stockout_prob": stockout_prob, "tts": tts,
    }


def monte_carlo(spec: RunSpec | dict, n: int = 200, seeds: list[int] | None = None, processes: int | None = None,
                progress=None) -> dict:
    """Run n replications (seeds default to spec.seed + i) and return percentile bands.
    processes: pool size (default: all cores; 1 = run in this process). progress(done, n) is called as
    replications finish."""
    spec = spec if isinstance(spec, RunSpec) else RunSpec.model_validate(spec)
    seeds = list(seeds) if seeds is not None else [spec.seed + i for i in range(n)]
    n = len(seeds)
    processes = processes or os.cpu_count() or 1
    t0 = time.perf_counter()
    runs: list[dict] = []
    if processes == 1 or n == 1:
        for i, s in enumerate(seeds):
            runs.append(run_one(spec, s))
            if progress:
                progress(i + 1, n)
    else:
        payload = spec.model_dump_json()
        ctx = mp.get_context("spawn")
        with ctx.Pool(min(processes, n)) as pool:
            for i, r in enumerate(pool.imap(_job, [(payload, s) for s in seeds], chunksize=max(1, n // (processes * 4)))):
                runs.append(r)
                if progress:
                    progress(i + 1, n)
    out = summarize(spec, runs)
    out["wall_s"] = round(time.perf_counter() - t0, 3)
    out["processes"] = 1 if processes == 1 or n == 1 else min(processes, n)
    return out

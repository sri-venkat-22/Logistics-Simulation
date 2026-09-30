"""Simchi-Levi stress test: time-to-survive (TTS), time-to-recover (TTR) and risk exposure per node.

    python -m sim.macro.resilience --days 30 --out docs/sim/resilience.json

For every node that can fail (ports, plants, suppliers, DCs):
  TTS   run the twin with the node fully down for the whole horizon; TTS = time until the first stock-out
        at a DC x SKU that depends on it (None = the network survives the whole horizon: TTS > horizon).
        The twin keeps its policies (no re-planning), so this is the *unmitigated* TTS.
  TTR   the time the node needs to recover (node attrs["ttr_days"], else a default per node type).
  REI   run the twin with the node down for exactly its TTR and value the lost service
        (backordered units x unit value + penalty, net of the baseline); the Risk Exposure Index is that
        impact normalised to the worst node (1.0 = most exposed).
  exposed = TTS < TTR: the network runs out before the node comes back.
Same seed for every run (common random numbers), random outages off so only the tested node fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.network import Network

TTR_DAYS_DEFAULT = {"port": 7.0, "plant": 10.0, "supplier": 14.0, "overseas": 21.0, "dc": 5.0}


def ttr_days(net: Network, node: str) -> float:
    n = net.nodes[node]
    if "ttr_days" in n.attrs:
        return float(n.attrs["ttr_days"])
    return TTR_DAYS_DEFAULT["overseas" if n.attrs.get("overseas") else n.type]


def _run(net, demand, start, seed, days, node=None, down_h=None) -> tuple[dict, Twin]:
    twin = Twin(net, demand, start, seed=seed, random_failures=False, log_events=False)
    if node:
        twin.apply({"type": "node_status", "node": node, "factor": 0.0, "duration_h": down_h, "label": f"stress {node}"})
    return twin.run(days), twin


def _loss(k: dict, net: Network) -> float:
    value = 0.0
    for sku, v in k["per_sku"].items():
        backordered = v["units_demanded"] * (1 - v["fill_rate"])
        value += backordered * (net.skus[sku].unit_value + net.skus[sku].stockout_penalty)
    return value


def stress_test(net: Network | None = None, demand: DemandModel | None = None, start: datetime | None = None,
                days: float = 30, seed: int = 42, nodes: list[str] | None = None) -> dict:
    net = net or Network()
    demand = demand or DemandModel(net)
    start = start or datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    base, _ = _run(net, demand, start, seed, days)
    base_loss = _loss(base, net)
    nodes = nodes or [n.id for n in net.nodes.values() if n.type != "zone"]
    rows = []
    for nid in nodes:
        k, twin = _run(net, demand, start, seed, days, nid, None)
        d = next(x for x in k["disruptions"] if x["kind"] == "live")
        ttr = ttr_days(net, nid)
        k_ttr, _ = _run(net, demand, start, seed, days, nid, ttr * 24)
        impact = max(0.0, _loss(k_ttr, net) - base_loss)
        tts = None if d["tts_h"] is None else d["tts_h"] / 24
        rows.append({"node": nid, "type": net.nodes[nid].type, "name": net.nodes[nid].name,
                     "dependents": d["dependents"], "tts_days": None if tts is None else round(tts, 2),
                     "ttr_days": ttr, "exposed": tts is not None and tts < ttr,
                     "first_stockouts": d["stockouts"][:6], "impact_inr": round(impact),
                     "fill_rate_ttr_outage": round(k_ttr["fill_rate"], 4)})
    worst = max((r["impact_inr"] for r in rows), default=0) or 1
    for r in rows:
        r["rei"] = round(r["impact_inr"] / worst, 3)
    rows.sort(key=lambda r: (-r["rei"], r["tts_days"] if r["tts_days"] is not None else 1e9))
    return {"days": days, "seed": seed, "start": start.isoformat(timespec="minutes"),
            "baseline_fill_rate": base["fill_rate"], "nodes": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.macro.resilience", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = stress_test(days=a.days, seed=a.seed)
    print(f"Simchi-Levi stress test · {a.days:g}-day horizon · seed {a.seed} · baseline fill {res['baseline_fill_rate'] * 100:.1f} %")
    print(f"  {'node':<20}{'type':<9}{'TTS':>9}{'TTR':>8}  {'exposed':<8}{'REI':>6}{'impact':>14}  first stock-outs")
    for r in res["nodes"]:
        tts = f"{r['tts_days']:.1f} d" if r["tts_days"] is not None else f"> {a.days:g} d"
        print(f"  {r['node']:<20}{r['type']:<9}{tts:>9}{r['ttr_days']:>6.0f} d  {'YES' if r['exposed'] else 'no':<8}"
              f"{r['rei']:>6.2f}{r['impact_inr'] / 1e5:>11.1f} L  {', '.join(r['first_stockouts'][:3])}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

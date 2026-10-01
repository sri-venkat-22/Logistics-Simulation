"""Network criticality and cascades (Phase 7.2).

    python -m sim.optimize.criticality                  # ranking table -> docs/sim/criticality.json

Flow model: every DC x SKU replenishment path carries the DC's base daily demand for that SKU and every zone x SKU
serving lane carries the zone's daily demand (units/day) - the load L0 of each lane in normal operation.

  betweenness   networkx betweenness centrality on the lane graph (weight = mean lead time), normalised
  flow share    share of all unit-flow that passes through the node
  cascade       Motter-Lai load redistribution: lane capacity C = (1 + alpha) L0 + alpha (physical capacity - L0), i.e.
                a tolerance alpha on the normal load plus that share of the lane's spare capacity that can be
                mobilised at short notice (so idle lanes absorb something too). Removing a node reroutes every flow
                that used it onto the shortest surviving path (replenishment: from any source of the SKU; serving:
                the next backup DC); lanes pushed above capacity fail, their flows reroute again, and so on until
                nothing new fails. A node fails when every inbound lane it relies on has failed.
  REI           Simchi-Levi risk exposure index (TTR-based, docs/sim/resilience.json)
  SPOF score    0.4 REI + 0.3 cascade unserved share + 0.2 flow share + 0.1 betweenness
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from sim.macro.demand import DemandModel
from sim.macro.network import Network
from sim.paths import ROOT

RESILIENCE = ROOT / "docs" / "sim" / "resilience.json"


class FlowModel:
    def __init__(self, net: Network, demand: DemandModel | None = None):
        self.net = net
        demand = demand or DemandModel(net)
        self.flows: list[dict] = []   # {"kind", "key", "sku", "units", "target", "path": [lanes]}
        zones_of: dict[tuple[str, str], list[str]] = {}
        for (z, sku), (dc, lane) in net.serving.items():
            zones_of.setdefault((dc, sku), []).append(z)
            self.flows.append({"kind": "serve", "key": f"{z}/{sku}", "sku": sku, "units": demand.base_mean(z, sku),
                               "target": z, "path": [lane]})
        for (dc, sku), paths in net.replenishment.items():
            units = sum(demand.base_mean(z, sku) for z in zones_of.get((dc, sku), []))
            self.flows.append({"kind": "replenish", "key": f"{dc}/{sku}", "sku": sku, "units": units, "target": dc,
                               "path": list(paths[0].lanes)})
        self.sources = {sku: {p.source for (_, s), ps in net.replenishment.items() if s == sku for p in ps} for sku in net.skus}
        self.total = sum(f["units"] for f in self.flows) or 1.0

    def loads(self, flows: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for f in flows:
            for lid in f["path"] or []:
                out[lid] = out.get(lid, 0.0) + f["units"]
        return out

    def graph(self, dead_nodes: set[str], dead_lanes: set[str], sku: str) -> nx.DiGraph:
        net = self.net
        g = nx.DiGraph()
        cold = net.skus[sku].cold_chain
        for ln in net.lanes.values():
            if ln.id in dead_lanes or ln.from_id in dead_nodes or ln.to_id in dead_nodes:
                continue
            if cold and any(net.nodes[x].type == "dc" and not net.nodes[x].cold_chain for x in (ln.from_id, ln.to_id)):
                continue
            if not g.has_edge(ln.from_id, ln.to_id) or g[ln.from_id][ln.to_id]["w"] > ln.lt_mean_h:
                g.add_edge(ln.from_id, ln.to_id, w=ln.lt_mean_h, lane=ln.id)
        return g

    def reroute(self, f: dict, dead_nodes: set[str], dead_lanes: set[str]) -> list[str] | None:
        net = self.net
        g = self.graph(dead_nodes, dead_lanes, f["sku"])
        if f["kind"] == "serve":
            z, sku = f["key"].split("/")
            for dc, lane in net.serving_options.get((z, sku), []):
                if dc not in dead_nodes and lane not in dead_lanes:
                    return [lane]
            return None
        best = None
        for s in self.sources[f["sku"]]:
            if s in dead_nodes or s not in g or f["target"] not in g:
                continue
            try:
                nodes = nx.shortest_path(g, s, f["target"], weight="w")
            except nx.NetworkXNoPath:
                continue
            if any(net.nodes[n].type in ("zone",) for n in nodes[1:-1]):
                continue
            lanes = [g[a][b]["lane"] for a, b in zip(nodes, nodes[1:])]
            cost = sum(g[a][b]["w"] for a, b in zip(nodes, nodes[1:]))
            if best is None or cost < best[0]:
                best = (cost, lanes)
        return best[1] if best else None


def betweenness(net: Network) -> dict[str, float]:
    g = nx.DiGraph()
    for ln in net.lanes.values():
        if not g.has_edge(ln.from_id, ln.to_id) or g[ln.from_id][ln.to_id]["weight"] > ln.lt_mean_h:
            g.add_edge(ln.from_id, ln.to_id, weight=ln.lt_mean_h)
    return nx.betweenness_centrality(g, weight="weight", normalized=True)


def _unserved(net: Network, flows: list[dict]) -> float:
    """Share of zone demand with no serving lane, or served by a DC whose replenishment of that SKU is cut off
    (it runs dry once its stock is gone)."""
    starved = {(f["target"], f["sku"]) for f in flows if f["kind"] == "replenish" and f["path"] is None}
    total = bad = 0.0
    for f in flows:
        if f["kind"] != "serve":
            continue
        total += f["units"]
        if f["path"] is None or (net.lanes[f["path"][0]].from_id, f["sku"]) in starved:
            bad += f["units"]
    return bad / (total or 1.0)


def cascade(fm: FlowModel, node: str, alpha: float = 0.25) -> dict:
    """Motter-Lai cascade after `node` fails. Returns the steps (what failed when) and the unserved share."""
    net = fm.net
    base = fm.loads(fm.flows)
    cap = {lid: (1 + alpha) * base.get(lid, 0.0) + alpha * max(0.0, net.lanes[lid].capacity - base.get(lid, 0.0))
           for lid in net.lanes}
    dead_nodes: set[str] = {node}
    dead_lanes: set[str] = set()
    flows = [dict(f) for f in fm.flows]
    steps = [{"step": 0, "nodes": [node], "lanes": sorted(l.id for l in net.lanes.values() if node in (l.from_id, l.to_id)),
              "unserved_share": 0.0}]
    for step in range(1, 12):
        for f in flows:
            path = f["path"]
            broken = path is None or any(l in dead_lanes or net.lanes[l].from_id in dead_nodes or net.lanes[l].to_id in dead_nodes
                                         for l in path) or f["target"] in dead_nodes
            if broken:
                f["path"] = None if f["target"] in dead_nodes else fm.reroute(f, dead_nodes, dead_lanes)
        load = fm.loads(flows)
        over = sorted(l for l, v in load.items() if l not in dead_lanes and v > cap[l] + 1e-9)
        new_nodes = []
        for n in net.nodes.values():
            if n.id in dead_nodes or n.type in ("zone", "supplier", "plant"):
                continue
            inbound = [l for l in net.lanes.values() if l.to_id == n.id and base.get(l.id, 0) > 0]
            if inbound and all(l.id in dead_lanes or l.id in over or l.from_id in dead_nodes for l in inbound):
                new_nodes.append(n.id)
        unserved = _unserved(net, flows)
        if not over and not new_nodes:
            steps[-1]["unserved_share"] = round(unserved, 4)
            break
        dead_lanes |= set(over)
        dead_nodes |= set(new_nodes)
        steps.append({"step": step, "nodes": new_nodes, "lanes": over, "unserved_share": round(unserved, 4)})
    final = steps[-1]["unserved_share"]
    return {"node": node, "alpha": alpha, "steps": steps, "failed_nodes": sorted(dead_nodes), "failed_lanes": sorted(dead_lanes),
            "unserved_share": final, "size": len(dead_nodes) - 1 + len(dead_lanes)}


def criticality(net: Network | None = None, alpha: float = 0.25, resilience: dict | None = None) -> dict:
    net = net or Network()
    fm = FlowModel(net)
    bc = betweenness(net)
    base = fm.loads(fm.flows)
    through: dict[str, float] = {}
    for f in fm.flows:
        nodes: set[str] = set()
        for lid in f["path"]:
            nodes |= {net.lanes[lid].from_id, net.lanes[lid].to_id}
        for nid in nodes:
            through[nid] = through.get(nid, 0.0) + f["units"]
    if resilience is None:
        resilience = json.loads(RESILIENCE.read_text()) if RESILIENCE.exists() else {"nodes": []}
    rei = {r["node"]: r for r in resilience.get("nodes", [])}
    bmax = max(bc.values()) or 1.0
    rows = []
    for n in net.nodes.values():
        if n.type == "zone":
            continue
        c = cascade(fm, n.id, alpha)
        share = through.get(n.id, 0.0) / fm.total
        r = rei.get(n.id, {})
        score = 0.4 * (r.get("rei") or 0.0) + 0.3 * c["unserved_share"] + 0.2 * min(1.0, share * 2) + 0.1 * bc.get(n.id, 0) / bmax
        rows.append({"node": n.id, "type": n.type, "name": n.name, "betweenness": round(bc.get(n.id, 0.0), 4),
                     "flow_share": round(share, 4), "cascade_size": c["size"], "cascade_steps": len(c["steps"]) - 1,
                     "unserved_share": c["unserved_share"], "rei": r.get("rei"), "tts_days": r.get("tts_days"),
                     "ttr_days": r.get("ttr_days"), "exposed": r.get("exposed"), "spof_score": round(score, 4)})
    rows.sort(key=lambda r: -r["spof_score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"alpha": alpha, "lanes_loaded": sum(1 for v in base.values() if v > 0), "nodes": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.optimize.criticality", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--alpha", type=float, default=0.25)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "sim" / "criticality.json")
    a = ap.parse_args(argv)
    res = criticality(alpha=a.alpha)
    print(f"Single points of failure · Motter-Lai tolerance alpha = {a.alpha}")
    print(f"  {'#':>2} {'node':<20}{'type':<9}{'SPOF':>6}{'REI':>6}{'flow':>7}{'betw.':>7}{'cascade':>9}{'unserved':>10}")
    for r in res["nodes"]:
        print(f"  {r['rank']:>2} {r['node']:<20}{r['type']:<9}{r['spof_score']:>6.3f}{(r['rei'] or 0):>6.2f}{r['flow_share'] * 100:>6.1f}%"
              f"{r['betweenness']:>7.3f}{r['cascade_size']:>6} ({r['cascade_steps']}){r['unserved_share'] * 100:>9.1f}%")
    a.out.write_text(json.dumps(res, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

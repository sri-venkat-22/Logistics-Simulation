"""Recommendation engine (Phase 7.1): candidate plans for a disruption, Pareto / weighted ranking, explanations.

Candidates (each a list of plan actions the live twin and Monte Carlo both understand):
  reroute      k-shortest multimodal paths (networkx.shortest_simple_paths) from any source of the SKU to the
               DC on the network with the disrupted nodes removed; weight = cost + lambda * time + mu * CO2 per
               unit, with disrupted lanes slowed by their multiplier                          -> set_route
  reallocate   OR-Tools SimpleMinCostFlow: surplus DCs (unaffected, projected stock above the reorder point)
               supply the DCs Monte Carlo projects to stock out (P50 / P90 backlog); arc cost = transfer
               cost + delay penalty; flows are decomposed into DC -> DC transfers              -> transfer
  expedite     the same routing with time dominating the weight and air allowed, for the critical families
               (vaccine, electronics), plus reallocation over air arcs                          -> set_route, transfer
  buffer       raise the safety factor z of the affected families                             -> policy
  combined     reroute + reallocate (the "Plan A" of the demo story)
Every plan is then evaluated with Monte Carlo on the same seeds (common random numbers): service (fill rate),
cost, CO2, OTIF, CVaR95 of the shortfall value, TTS vs TTR. rank() marks the Pareto front on
(service up, cost down, CO2 down, CVaR down) and scores plans with user weights. explain() turns the plan's
actions and one evidence replication (event log) into a sentence with links to the events behind it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import networkx as nx

from sim.macro.disruptions import CLOSED_BELOW, build_effect
from sim.macro.network import Network
from sim.scenarios.dsl import Scenario

CARBON_INR_PER_KG = 2.0            # shadow carbon price used in the route weight (~ USD 25 / t)
CRITICAL = ("vaccine", "electronics")
DEFAULT_WEIGHTS = {"service": 0.4, "risk": 0.3, "cost": 0.2, "co2": 0.1}


@dataclass
class Route:
    lanes: list[str]
    nodes: list[str]
    lead_h: float
    cost_per_unit: float
    co2_kg_per_unit: float
    weight: float


@dataclass
class Disruption:
    closed: set[str] = field(default_factory=set)              # nodes below CLOSED_BELOW capacity
    degraded: dict[str, float] = field(default_factory=dict)   # node -> remaining capacity factor
    lane_mult: dict[str, float] = field(default_factory=dict)
    affected: set[tuple[str, str]] = field(default_factory=set)


def disruption_of(twin, scenarios: list[Scenario]) -> Disruption:
    d = Disruption()
    for sc in scenarios:
        e = build_effect(sc, twin.net)
        for table in (e.node_factor, e.production_factor, e.dispatch_factor):
            for n, f in table.items():
                d.degraded[n] = min(d.degraded.get(n, 1.0), f)
        for lid, m in e.lane_mult.items():
            d.lane_mult[lid] = max(d.lane_mult.get(lid, 1.0), m)
        d.affected |= twin.dependents(e)
    d.closed = {n for n, f in d.degraded.items() if f < CLOSED_BELOW}
    return d


# ================================================================================================ routing
def value_of_time(net: Network, sku: str) -> float:
    """INR per unit-hour of lead time: a tenth of the SKU's daily stock-out penalty (what an hour of delay risks)."""
    return net.skus[sku].stockout_penalty / 24.0 * 0.1


def lane_terms(net: Network, lid: str, sku: str, mult: float = 1.0) -> tuple[float, float, float]:
    """(cost per unit, hours, kg CO2 per unit) of moving one unit of sku over a lane."""
    ln = net.lanes[lid]
    return ln.cost_per_unit, ln.lt_mean_h * mult, net.skus[sku].unit_weight_kg / 1000.0 * ln.distance_km * ln.co2_per_tkm


def k_routes(twin, dc: str, sku: str, dis: Disruption, k: int = 3, lam: float = 1.0, mu: float = 1.0,
             allow_air: bool = False, sources: set[str] | None = None) -> list[Route]:
    """Up to k cheapest routes (by cost + lam * value_of_time * hours + mu * carbon price * CO2) from any open
    source of the SKU to the DC, avoiding closed nodes; cold-chain SKUs only through cold-chain DCs."""
    net = twin.net
    vot = value_of_time(net, sku) * lam
    cold = net.skus[sku].cold_chain
    sources = {s for s in (sources or twin.sku_sources(sku)) if s not in dis.closed}
    g = nx.DiGraph()
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for ln in net.lanes.values():
        a, b = ln.from_id, ln.to_id
        if ln.mode == "air" and not allow_air:
            continue
        if a in dis.closed or b in dis.closed:
            continue
        ta, tb = net.nodes[a].type, net.nodes[b].type
        if tb in ("zone", "supplier", "plant") or ta == "zone":
            continue
        if ta in ("supplier", "plant") and a not in sources:
            continue
        if cold and any(net.nodes[x].type == "dc" and not net.nodes[x].cold_chain for x in (a, b)):
            continue
        c, h, co2 = lane_terms(net, ln.id, sku, dis.lane_mult.get(ln.id, 1.0))
        h += (1 / max(dis.degraded.get(b, 1.0), 1e-3) - 1) * 12.0  # degraded node: expected extra dwell
        w = c + vot * h + mu * CARBON_INR_PER_KG * co2
        if (a, b) not in best or w < best[(a, b)][0]:
            best[(a, b)] = (w, ln.id)
    for (a, b), (w, lid) in best.items():
        g.add_edge(a, b, weight=w, lane=lid)
    for s in sources:
        if s in g:
            g.add_edge("_SRC", s, weight=0.0, lane=None)
    if "_SRC" not in g or dc not in g:
        return []
    out = []
    try:
        for path in nx.shortest_simple_paths(g, "_SRC", dc, weight="weight"):
            lanes = [g[a][b]["lane"] for a, b in zip(path[1:], path[2:])]
            if not lanes:
                continue
            terms = [lane_terms(net, l, sku, dis.lane_mult.get(l, 1.0)) for l in lanes]
            out.append(Route(lanes, path[1:], round(sum(t[1] for t in terms), 1), round(sum(t[0] for t in terms), 2),
                             round(sum(t[2] for t in terms), 3),
                             round(sum(g[a][b]["weight"] for a, b in zip(path, path[1:])), 2)))
            if len(out) >= k:
                break
    except nx.NetworkXNoPath:
        pass
    return out


def reroute_actions(twin, dis: Disruption, lam: float = 1.0, allow_air: bool = False,
                    families: tuple[str, ...] | None = None, same_source: bool = False) -> list[dict]:
    """set_route for every affected DC x SKU whose current path crosses the disruption and has a better route
    (same_source: keep the current supplier and only change the path, e.g. another port)."""
    acts = []
    for dc, sku in sorted(dis.affected):
        if families and twin.net.skus[sku].family not in families:
            continue
        cur = twin.path(dc, sku)
        crosses = bool(dis.closed & set(cur.nodes)) or any(dis.lane_mult.get(l, 1) > 1 for l in cur.lanes) or \
            any(dis.degraded.get(n, 1) < 1 for n in cur.nodes[:-1])
        if not crosses:
            continue
        routes = k_routes(twin, dc, sku, dis, k=1, lam=lam, allow_air=allow_air,
                          sources={cur.source} if same_source else None)
        if routes and routes[0].lanes != list(cur.lanes):
            r = routes[0]
            acts.append({"type": "set_route", "dc": dc, "sku": sku, "lanes": r.lanes, "via": r.nodes[1:-1],
                         "lead_h": r.lead_h, "cost_per_unit": r.cost_per_unit})
    return acts


# ================================================================================================ reallocation
def deficits_and_surpluses(twin, dis: Disruption, result: dict, levels: dict[tuple[str, str], tuple[float, float]]
                           ) -> tuple[dict, dict]:
    """From a scenario's Monte Carlo result: the deficit per DC x SKU (units short: the P50 backlog peak, or the
    P90 peak scaled by the stock-out probability when that is material) and the surplus at DCs that stay in stock
    (P(stock-out) < 5 %, not closed): half of the lowest P10 on-hand over the horizon (after day 0),
    so a donor keeps half of its lowest stock even in a bad (P10) run."""
    deficit, surplus = {}, {}
    for key, ser in result.get("series", {}).items():
        dc, sku = key.split("/")
        p_out = result.get("stockout_prob", {}).get(key, 0.0)
        bl50, bl90 = max(ser["backlog"]["p50"], default=0), max(ser["backlog"]["p90"], default=0)
        if bl50 > 0 or (p_out >= 0.2 and bl90 > 0):
            deficit[(dc, sku)] = math.ceil(bl50 if bl50 > 0 else bl90 * p_out)
        elif dc not in dis.closed and p_out < 0.05:
            oh = ser["on_hand"]["p10"][1:]  # skip day 0 (initial state)
            low = min(oh) if oh else 0.0
            if low > 0:
                surplus[(dc, sku)] = math.floor(0.5 * low)
    return deficit, surplus


def reallocate_actions(twin, dis: Disruption, result: dict, levels: dict, allow_air: bool = False,
                       families: tuple[str, ...] | None = None) -> list[dict]:
    """Min-cost flow per SKU over the DC -> DC lanes (multi-hop through open DCs). Benefit formulation: every
    donor must send its surplus to a sink, either directly (cost 0) or through transfer arcs to a DC that is
    short, whose arc to the sink earns -BIG per unit (capacity = its deficit). So the solver covers as much
    deficit as the donors allow, over the cheapest arcs (transfer cost + stock-out penalty x hours)."""
    from ortools.graph.python import min_cost_flow
    net = twin.net
    deficit, surplus = deficits_and_surpluses(twin, dis, result, levels)
    acts = []
    big = 10**6
    for sku in sorted({k for _, k in deficit}):
        if families and net.skus[sku].family not in families:
            continue
        dem = {dc: int(q) for (dc, s_), q in deficit.items() if s_ == sku and q > 0}
        sup = {dc: int(q) for (dc, s_), q in surplus.items() if s_ == sku and q > 0}
        if not dem or not sup:
            continue
        cold = net.skus[sku].cold_chain
        dcs = sorted({n.id for n in net.of_type("dc") if n.id not in dis.closed and (not cold or n.cold_chain)} | set(dem))
        idx = {d: i for i, d in enumerate(dcs)}
        sink = len(dcs)
        pen_h = net.skus[sku].stockout_penalty / 24.0
        f = min_cost_flow.SimpleMinCostFlow()
        best: dict[tuple[str, str], tuple[str, int]] = {}
        for ln in net.lanes.values():
            if ln.from_id in idx and ln.to_id in idx and (allow_air or ln.mode != "air"):
                h = ln.lt_mean_h * dis.lane_mult.get(ln.id, 1.0)
                cost = int(round(ln.cost_per_unit + pen_h * h))
                key = (ln.from_id, ln.to_id)
                if key not in best or cost < best[key][1]:
                    best[key] = (ln.id, cost)
        transfer_arcs = {}
        for (u, v), (lid, cost) in best.items():
            transfer_arcs[f.add_arc_with_capacity_and_unit_cost(idx[u], idx[v], 10**7, cost)] = (u, v, lid)
        term_arcs = {}
        for d, q in sup.items():
            f.add_arc_with_capacity_and_unit_cost(idx[d], sink, q, 0)
            f.set_node_supply(idx[d], q)
        for d, q in dem.items():
            term_arcs[d] = f.add_arc_with_capacity_and_unit_cost(idx[d], sink, q, -big)
        f.set_node_supply(sink, -sum(sup.values()))
        if f.solve() != f.OPTIMAL:
            continue
        out_edges: dict[str, list] = {}
        for arc, (u, v, lid) in transfer_arcs.items():
            if f.flow(arc) > 0:
                out_edges.setdefault(u, []).append([v, lid, f.flow(arc)])
        terminal = {d: f.flow(a) for d, a in term_arcs.items() if f.flow(a) > 0}
        for src in sorted(sup):
            for _ in range(50):
                path, lanes, node = [src], [], src
                while not (node != src and terminal.get(node, 0) > 0):
                    nxt = next((e for e in out_edges.get(node, []) if e[2] > 0 and e[0] not in path), None)
                    if nxt is None:
                        break
                    path.append(nxt[0])
                    lanes.append(nxt)
                    node = nxt[0]
                if not lanes or terminal.get(node, 0) <= 0:
                    break
                q = min([e[2] for e in lanes] + [terminal[node]])
                for e in lanes:
                    e[2] -= q
                terminal[node] -= q
                lids = [e[1] for e in lanes]
                acts.append({"type": "transfer", "from": src, "to": node, "sku": sku, "qty": int(q), "lanes": lids,
                             "lead_h": round(sum(net.lanes[l].lt_mean_h for l in lids), 1),
                             "modes": sorted({net.lanes[l].mode for l in lids})})
    return acts


# ================================================================================================ candidates
def candidates(twin, scenarios: list[Scenario], result: dict | None = None, levels: dict | None = None,
               lam: float = 1.0) -> list[dict]:
    """Candidate plans for a disruption: [{"name", "kind", "actions"}]. `result` (the scenario's Monte Carlo
    result) enables reallocation; `levels` maps (dc, sku) -> (reorder point, order-up-to)."""
    net = twin.net
    dis = disruption_of(twin, scenarios)
    levels = levels or {}
    plans: list[dict[str, Any]] = [{"name": "Do nothing", "kind": "baseline", "actions": []}]
    same = reroute_actions(twin, dis, lam=lam, same_source=True)
    if same:
        via = sorted({v for a in same for v in a["via"]})
        plans.append({"name": f"Reroute via {_names(via[:3])} (same suppliers)", "kind": "reroute", "actions": same})
    anysrc = reroute_actions(twin, dis, lam=lam)
    if anysrc and anysrc != same:
        srcs = sorted({twin.net.lanes[a["lanes"][0]].from_id for a in anysrc})
        plans.append({"name": f"Switch sourcing to {_names(srcs[:3])}", "kind": "resource", "actions": anysrc})
    realloc = reallocate_actions(twin, dis, result, levels) if result else []
    if realloc:
        plans.append({"name": f"Transfer stock from {_names(sorted({a['from'] for a in realloc}))}", "kind": "reallocate",
                      "actions": realloc})
    exp_routes = reroute_actions(twin, dis, lam=25.0, allow_air=True, families=CRITICAL)
    exp_transfers = reallocate_actions(twin, dis, result, levels, allow_air=True, families=CRITICAL) if result else []
    exp_transfers = [a for a in exp_transfers if "air" in a["modes"]]
    if (exp_routes and exp_routes not in (same, anysrc)) or exp_transfers:
        plans.append({"name": "Expedite critical SKUs (fastest routes, air)", "kind": "expedite",
                      "actions": exp_routes + exp_transfers})
    fams = sorted({net.skus[sku].family for _, sku in dis.affected})
    buffer = [{"type": "policy", "family": f, "dz": 0.8} for f in fams]
    if fams:
        plans.append({"name": f"Buffer: +0.8 safety factor ({', '.join(fams)})", "kind": "buffer", "actions": buffer})
    route = same or anysrc
    if route and realloc:
        plans.append({"name": "Reroute + transfer stock", "kind": "combined", "actions": route + realloc})
    parts = [x for x in (route, realloc, buffer) if x]
    if len(parts) >= 2:
        label = " + ".join(n for n, x in (("reroute", route), ("transfer", realloc), ("buffer", buffer)) if x)
        plans.append({"name": f"Full response ({label})", "kind": "combined", "actions": [a for x in parts for a in x]})
    return plans


def _names(ids) -> str:
    return ", ".join(i.replace("PORT_", "").replace("DC_HYD_", "").replace("DC_", "").replace("SUP_", "").title()
                     for i in ids) or "alternates"


def apply_to_spec(spec, actions: list[dict]):
    """A RunSpec with the plan's actions applied (routes, transfers at t = 0, policy buffers, sourcing paths)."""
    from sim.macro.policies import FAMILY_POLICY
    pc = dict(spec.path_choice)
    routes = dict(spec.routes)
    transfers = list(spec.transfers)
    pol = {k: dict(v) for k, v in (spec.policy or {}).items()}
    for a in actions:
        t = a["type"]
        if t == "set_path":
            pc[f"{a['dc']}/{a['sku']}"] = a["path"]
        elif t == "set_route":
            routes[f"{a['dc']}/{a['sku']}"] = list(a["lanes"])
        elif t == "transfer":
            transfers.append({"from": a["from"], "to": a["to"], "sku": a["sku"], "qty": a["qty"], "lanes": list(a["lanes"]),
                              "at_h": 0.0})
        elif t == "policy":
            base = {**FAMILY_POLICY[a["family"]], **pol.get(a["family"], {})}
            pol[a["family"]] = {**base, "z": base["z"] + a["dz"]}
    return spec.model_copy(update={"path_choice": pc, "routes": routes, "transfers": transfers, "policy": pol or None})


def to_twin_events(actions: list[dict]) -> list[dict]:
    """The live-twin apply() events for a plan's actions."""
    out = []
    for a in actions:
        t = a["type"]
        if t == "set_path":
            out.append({"type": "set_path", "dc": a["dc"], "sku": a["sku"], "path": a["path"]})
        elif t == "set_route":
            out.append({"type": "set_route", "dc": a["dc"], "sku": a["sku"], "lanes": a["lanes"]})
        elif t == "transfer":
            out.append({"type": "transfer", "from": a["from"], "to": a["to"], "sku": a["sku"], "qty": a["qty"], "lanes": a["lanes"]})
        elif t == "policy":
            out.append({"type": "policy_buffer", "family": a["family"], "dz": a["dz"]})
    return out


# ================================================================================================ ranking
def metrics(result: dict) -> dict:
    k = result["kpis"]
    tts = [t for t in result.get("tts", []) if t]
    return {"service": k["fill_rate"]["p50"], "service_p10": k["fill_rate"]["p10"], "otif": k["otif"]["p50"],
            "cost_lakh": k["cost_total"]["p50"] / 1e5, "co2_t": k["co2_t"]["p50"],
            "shortfall_lakh": k["shortfall_value"]["p50"] / 1e5 if "shortfall_value" in k else None,
            "cvar95_lakh": k["shortfall_value"]["cvar95"] / 1e5 if "shortfall_value" in k else None,
            "delay_h": k["avg_lead_h"]["p50"] if "avg_lead_h" in k else None,
            "backorders_p90": k["units_backordered"]["p90"],
            "stockout_p": max(result["stockout_prob"].values()) if result.get("stockout_prob") else 0.0,
            "tts_p50_h": (tts[0]["tts_h"] or {}).get("p50") if tts and tts[0].get("tts_h") else None,
            "ttr_h": tts[0].get("ttr_h") if tts else None, "p_exposed": tts[0].get("p_exposed") if tts else None}


OBJECTIVES = {"service": +1, "cost_lakh": -1, "co2_t": -1, "cvar95_lakh": -1}
WEIGHT_KEY = {"service": "service", "cost": "cost_lakh", "co2": "co2_t", "risk": "cvar95_lakh"}


def rank(plans: list[dict], weights: dict | None = None) -> list[dict]:
    """Mark the Pareto front (no other plan at least as good on every objective and better on one) and score
    each plan: sum of weights x metric normalised across plans (1 = best, 0 = worst). Sorted by score."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    tot = sum(max(v, 0) for v in w.values()) or 1.0
    for p in plans:
        p["pareto"] = not any(
            all(OBJECTIVES[m] * (q[m] or 0) >= OBJECTIVES[m] * (p[m] or 0) for m in OBJECTIVES)
            and any(OBJECTIVES[m] * (q[m] or 0) > OBJECTIVES[m] * (p[m] or 0) for m in OBJECTIVES)
            for q in plans if q is not p)
    norm: dict[str, dict[int, float]] = {}
    for key, m in WEIGHT_KEY.items():
        vals = [p[m] or 0.0 for p in plans]
        lo, hi = min(vals), max(vals)
        norm[key] = {i: (1.0 if hi == lo else ((v - lo) / (hi - lo) if OBJECTIVES[m] > 0 else (hi - v) / (hi - lo)))
                     for i, v in enumerate(vals)}
    for i, p in enumerate(plans):
        p["score"] = round(sum(max(w[k], 0) * norm[k][i] for k in WEIGHT_KEY) / tot, 4)
        p["score_parts"] = {k: round(norm[k][i], 3) for k in WEIGHT_KEY}
    plans.sort(key=lambda p: (-p["score"], p["cost_lakh"]))
    return plans


# ================================================================================================ explanation
def evidence_run(spec, seed: int) -> list[dict]:
    """One replication with the event log on: the events behind the explanation."""
    from sim.macro.engine import Twin
    from sim.macro.montecarlo import _at, _world
    net, demand = _world()
    twin = Twin(net, demand, datetime.fromisoformat(spec.start), seed=seed, scenarios=list(spec.scenarios),
                policy=spec.policy, random_failures=spec.random_failures,
                calibration="auto" if spec.calibration else None, log_events=True)
    for key, idx in spec.path_choice.items():
        dc, sku = key.split("/")
        twin.apply({"type": "set_path", "dc": dc, "sku": sku, "path": idx})
    for key, lanes in spec.routes.items():
        dc, sku = key.split("/")
        twin.apply({"type": "set_route", "dc": dc, "sku": sku, "lanes": lanes})
    for t in spec.transfers:
        twin.env.process(_at(twin, t.get("at_h", 0.0), {"type": "transfer", **{k: v for k, v in t.items() if k != "at_h"}}))
    twin.run(spec.days)
    keep = ("transfer", "receipt", "set_route", "stockout_start", "stockout_end", "disruption_start", "disruption_end")
    return [e for e in twin.events if e["event"] in keep and (e["event"] != "receipt" or e.get("transfer"))]


def explain(plan: dict, base: dict | None, plan_events: list[dict], base_events: list[dict], net: Network) -> dict:
    """A human-readable account of what the plan does and what it changes, with the evidence rows."""
    name = lambda n: net.nodes[n].name.replace(" DC", "").split(" (")[0] if n in net.nodes else n  # noqa: E731
    fam = lambda s: net.skus[s].family  # noqa: E731
    parts = []
    receipts = {e["shipment"]: e for e in plan_events if e["event"] == "receipt"}
    for e in plan_events:
        if e["event"] == "transfer":
            r = receipts.get(e["shipment"])
            arr = f", arriving in {r['lead_h']:.0f} h" if r else ""
            parts.append(f"moves {e['qty']:,} {fam(e['sku'])} units from {name(e['src'])} to {name(e['dc'])}{arr}")
    routes = [a for a in plan["actions"] if a["type"] == "set_route"]
    if routes:
        via = sorted({v for a in routes for v in a.get("via", [])})
        parts.append(f"reroutes {len(routes)} replenishment flow{'s' if len(routes) > 1 else ''} via {', '.join(name(v) for v in via) or 'a direct lane'}")
    pols = [a for a in plan["actions"] if a["type"] == "policy"]
    if pols:
        parts.append("raises safety stock for " + ", ".join(a["family"] for a in pols))
    first = lambda evs: {f"{e['dc']}/{e['sku']}": e["t_h"] for e in reversed(evs) if e["event"] == "stockout_start"}  # noqa: E731
    fb, fp = first(base_events), first(plan_events)
    saved = [k for k in fb if k not in fp]
    effect = ""
    if saved:
        k = saved[0]
        dc, sku = k.split("/")
        effect = f" It keeps {name(dc)} {fam(sku)} in stock where doing nothing stocks out at day {fb[k] / 24:.1f}"
        if len(saved) > 1:
            effect += f" (and {len(saved) - 1} more DC x SKU)"
        effect += "."
    delta = ""
    if base:
        delta = (f" P50 fill {plan['service'] * 100:.1f} % vs {base['service'] * 100:.1f} %, cost "
                 f"{plan['cost_lakh'] - base['cost_lakh']:+.1f} L, CO2 {plan['co2_t'] - base['co2_t']:+.1f} t, CVaR95 shortfall "
                 f"{(plan['cvar95_lakh'] or 0) - (base['cvar95_lakh'] or 0):+.1f} L.")
    text = (f"{plan['name']}: " + ("; ".join(parts) if parts else "no change to the network") + "." + effect + delta)
    return {"text": text, "evidence": plan_events[:40], "stockouts_avoided": saved}

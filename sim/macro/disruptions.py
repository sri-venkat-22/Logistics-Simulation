"""Disruption injector: scenario DSL -> Effect (what the engine multiplies while a disruption is active).

An Effect scales five things, each keyed by id, and all active effects multiply together:
    node_factor        port / DC / plant handling capacity   (< CLOSED_BELOW = closed: cargo waits)
    production_factor  plant / supplier output
    dispatch_factor    DC outbound throughput
    lane_mult          transit-time multiplier (only the overlap with the disruption window counts)
    demand_mult        (zone | "*", sku | "*") demand multiplier
Live events (Twin.apply) build Effects directly; scenarios go through build_effect().
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sim.macro.network import Network
from sim.scenarios.dsl import Scenario, ScenarioType, haversine_km, point_in_polygon

CLOSED_BELOW = 0.25  # a node running below 25 % of capacity is effectively closed: cargo waits


@dataclass
class Effect:
    label: str
    kind: str = "scenario"          # scenario | random_failure | live | observed
    type: str = ""                  # scenario type (port_closure, ...) or live event type
    target: str = ""
    id: str = ""
    start_h: float = 0.0
    end_h: float = float("inf")
    node_factor: dict[str, float] = field(default_factory=dict)
    production_factor: dict[str, float] = field(default_factory=dict)
    dispatch_factor: dict[str, float] = field(default_factory=dict)
    lane_mult: dict[str, float] = field(default_factory=dict)
    demand_mult: dict[tuple[str, str], float] = field(default_factory=dict)
    sumo_edges: list[str] = field(default_factory=list)  # micro-twin roads to close (road_flood, coupled mode)

    def nodes(self) -> set[str]:
        """Nodes whose function this effect degrades (for TTS / TTR accounting)."""
        return ({n for n, f in self.node_factor.items() if f < 1} | {n for n, f in self.production_factor.items() if f < 1}
                | {n for n, f in self.dispatch_factor.items() if f < 1})

    def summary(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind, "type": self.type, "target": self.target,
                "start_h": round(self.start_h, 3), "end_h": None if self.end_h == float("inf") else round(self.end_h, 3),
                "nodes": sorted(self.nodes()), "lanes": sorted(self.lane_mult),
                "zones": sorted({z for z, _ in self.demand_mult})}


def build_effect(sc: Scenario, net: Network) -> Effect:
    e = Effect(label=sc.name or sc.type.value, type=sc.type.value, target=sc.target)
    p = sc.params
    if sc.type is ScenarioType.port_closure:
        e.node_factor[sc.target] = sc.scaled(p["capacity_factor"], 1.0)
    elif sc.type is ScenarioType.cyclone:
        if sc.polygon:
            inside = lambda lat, lon: point_in_polygon(lon, lat, sc.polygon)  # noqa: E731
        else:
            c = net.nodes[sc.target]
            inside = lambda lat, lon: haversine_km(lat, lon, c.lat, c.lon) <= p["radius_km"]  # noqa: E731
        if p["close_nodes"]:
            for n in net.nodes.values():
                if n.type != "zone" and (inside(n.lat, n.lon) or n.id == sc.target):
                    e.node_factor[n.id] = sc.scaled(0.0, 1.0)
        for l in net.lanes.values():
            a, b = net.nodes[l.from_id], net.nodes[l.to_id]
            if any(inside(a.lat + (b.lat - a.lat) * i / 20, a.lon + (b.lon - a.lon) * i / 20) for i in range(21)):
                e.lane_mult[l.id] = sc.scaled(p["lane_time_multiplier"], 1.0)
    elif sc.type is ScenarioType.road_flood:
        lanes = p["lanes"] or ([sc.target] if sc.target in net.lanes else
                               [l.id for l in net.lanes.values() if l.mode == "road" and sc.target in (l.from_id, l.to_id)])
        for lid in lanes:
            e.lane_mult[lid] = 1.0 / sc.scaled(p["speed_factor"], 1.0)
        e.sumo_edges = list(p.get("sumo_edges", []))
    elif sc.type is ScenarioType.demand_spike:
        for sku in (p["skus"] or ["*"]):
            e.demand_mult[(sc.target, sku)] = sc.scaled(p["multiplier"], 1.0)
    elif sc.type is ScenarioType.supplier_failure:
        e.production_factor[sc.target] = sc.scaled(p["capacity_factor"], 1.0)
    elif sc.type is ScenarioType.strike:
        f = sc.scaled(p["throughput_factor"], 1.0)
        kind = net.nodes[sc.target].type
        if kind == "dc":  # outbound dispatch and cross-dock handling both slow down
            e.dispatch_factor[sc.target] = f
            e.node_factor[sc.target] = f
        elif kind in ("plant", "supplier"):
            e.production_factor[sc.target] = f
        else:
            e.node_factor[sc.target] = f
    # data_blackout: no physical effect on goods — the Reality Emulator drops telemetry, the trust layer reacts
    return e

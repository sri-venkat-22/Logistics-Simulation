"""Typed, validated view of the reference network in data/ (nodes, lanes, SKUs, sourcing)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from sim.paths import DATA


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # port | plant | supplier | dc | zone
    name: str
    lat: float
    lon: float
    capacity: float  # units/day: production (plant/supplier), throughput (port), dispatch (dc)
    attrs: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def cold_chain(self) -> bool:
        return bool(self.attrs.get("cold_chain"))


@dataclass(frozen=True)
class Lane:
    id: str
    from_id: str
    to_id: str
    mode: str
    distance_km: float
    cost_per_unit_km: float
    cost_per_unit: float
    capacity: float
    co2_per_tkm: float
    lt_mu: float
    lt_sigma: float
    lt_mean_h: float
    lt_p90_h: float
    transfer_only: bool = False  # lateral DC -> DC transshipment lane (never on a sourcing path)


@dataclass(frozen=True)
class Sku:
    id: str
    family: str
    name: str
    unit_value: float
    unit_weight_kg: float
    cold_chain: bool
    sea_imported: bool
    base_demand_per_million_day: float
    holding_rate_yr: float
    stockout_penalty: float
    order_cost: float = 0.0  # fixed cost per replenishment order (0 in the reference data; EOQ check sets it)


@dataclass(frozen=True)
class Path_:
    source: str
    nodes: tuple[str, ...]
    lanes: tuple[str, ...]
    lead_mean_h: float


class Network:
    def __init__(self, data_dir: Path = DATA):
        self._build(json.loads((data_dir / "nodes.json").read_text()), json.loads((data_dir / "lanes.json").read_text()),
                    json.loads((data_dir / "skus.json").read_text()), json.loads((data_dir / "sourcing.json").read_text()))

    @classmethod
    def from_raw(cls, nodes: list[dict], lanes: list[dict], skus: list[dict], sourcing: dict) -> Network:
        """Build from in-memory dicts in the data/ file formats (toy networks for engine verification)."""
        net = cls.__new__(cls)
        net._build(nodes, lanes, skus, sourcing)
        return net

    def _build(self, raw_nodes: list[dict], raw_lanes: list[dict], raw_skus: list[dict], sourcing: dict) -> None:
        self.nodes = {n["id"]: Node(n["id"], n["type"], n["name"], n["lat"], n["lon"], n["capacity"], n.get("attrs", {}))
                      for n in raw_nodes}
        self.lanes = {l["id"]: Lane(l["id"], l["from_id"], l["to_id"], l["mode"], l["distance_km"], l["cost_per_unit_km"],
                                    l["cost_per_unit"], l["capacity"], l["co2_per_tkm"], l["lt_mu"], l["lt_sigma"],
                                    l["lt_mean_h"], l["lt_p90_h"], l.get("transfer_only", False)) for l in raw_lanes}
        self.skus = {s["id"]: Sku(s["id"], s["family"], s["name"], s["unit_value"], s["unit_weight_kg"], s["cold_chain"],
                                  s["sea_imported"], s["base_demand_per_million_day"], s["holding_rate_yr"],
                                  s["stockout_penalty"], s.get("order_cost", 0.0)) for s in raw_skus}

        def to_path(p: dict) -> Path_:
            return Path_(p["source"], tuple(p["nodes"]), tuple(p["lanes"]), p["lead_mean_h"])

        # (dc, sku) -> [primary, alternates...]
        self.replenishment: dict[tuple[str, str], list[Path_]] = {
            (r["dc"], r["sku"]): [to_path(r["primary"]), *map(to_path, r["alternates"])] for r in sourcing["replenishment"]}
        # (zone, sku) -> (dc, lane)
        self.serving: dict[tuple[str, str], tuple[str, str]] = {
            (s["zone"], s["sku"]): (s["dc"], s["lane"]) for s in sourcing["serving"]}
        # (zone, sku) -> [(dc, lane), ...] primary first, then backups nearest-first (fulfilment falls back along it)
        self.serving_options: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for s in sourcing["serving"]:
            backups = sorted(((b["dc"], b["lane"]) for b in s.get("backup", [])), key=lambda x: self.lanes[x[1]].distance_km)
            self.serving_options[(s["zone"], s["sku"])] = [(s["dc"], s["lane"]), *backups]

        self.graph = nx.MultiDiGraph()
        for n in self.nodes.values():
            self.graph.add_node(n.id, type=n.type)
        for l in self.lanes.values():
            self.graph.add_edge(l.from_id, l.to_id, key=l.id, weight=l.lt_mean_h)
        self.validate()

    def of_type(self, *types: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.type in types]

    def stocks(self, dc: str, sku: str) -> bool:
        return (dc, sku) in self.replenishment

    def validate(self) -> None:
        for l in self.lanes.values():
            assert l.from_id in self.nodes and l.to_id in self.nodes, f"lane {l.id} references unknown node"
            assert l.lt_mean_h > 0 and 0 <= l.lt_sigma < 1.5, f"lane {l.id} has bad lead-time params"  # sigma 0 = deterministic
            # lognormal consistency: E[T] = exp(mu + sigma^2 / 2)
            assert abs(math.exp(l.lt_mu + l.lt_sigma**2 / 2) - l.lt_mean_h) < 0.05 * l.lt_mean_h + 0.1, l.id
        for (dc, sku), paths in self.replenishment.items():
            for p in paths:
                assert p.nodes[-1] == dc and all(x in self.lanes for x in p.lanes), f"bad sourcing path {dc}/{sku}"
                if self.skus[sku].cold_chain:
                    assert all(self.nodes[x].type != "dc" or self.nodes[x].cold_chain for x in p.nodes), \
                        f"vaccine path through a non-cold-chain DC: {p.nodes}"
        for z in self.of_type("zone"):
            for sku in self.skus:
                assert (z.id, sku) in self.serving, f"zone {z.id} has no serving DC for {sku}"

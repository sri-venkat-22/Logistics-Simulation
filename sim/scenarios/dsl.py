"""Scenario DSL: one disruption, validated with Pydantic v2 (strict shapes, typed params).

    {
      "type": "port_closure | cyclone | road_flood | demand_spike | supplier_failure | strike | data_blackout",
      "target": "PORT_CHENNAI",
      "polygon": null,
      "start": "+6h",
      "duration_h": 120,
      "severity": 1.0,
      "params": {}
    }

Semantics
  target    a node id, a lane id, or "*" (network-wide). Which kinds are allowed depends on type.
  polygon   optional [[lon, lat], ...] ring (>= 3 points) — the affected area (e.g. a cyclone track).
  start     relative to the simulation clock ("now", "+90m", "+6h", "+2d") or an ISO-8601 timestamp.
  severity  0..1, scales the effect linearly between "no impact" (0) and the full params effect (1).
  params    per-type, validated by the matching *Params model below; unknown keys are rejected.

The same schema is exported as JSON Schema (scenario.schema.json) for the AI Copilot's
create_scenario tool, and spec_hash() gives the cache key used by the scenario API (Phase 4).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ScenarioType(str, Enum):
    port_closure = "port_closure"
    cyclone = "cyclone"
    road_flood = "road_flood"
    demand_spike = "demand_spike"
    supplier_failure = "supplier_failure"
    strike = "strike"
    data_blackout = "data_blackout"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortClosureParams(_Params):
    capacity_factor: float = Field(0.0, ge=0, le=1, description="Remaining port throughput at severity 1 (0 = fully closed)")
    divert_to: list[str] = Field(default_factory=list, description="Suggested alternate ports (hint for the optimiser)")


class CycloneParams(_Params):
    radius_km: float = Field(150.0, gt=0, le=1500, description="Impact radius around the target if no polygon is given")
    lane_time_multiplier: float = Field(2.0, ge=1, le=20, description="Transit-time multiplier for lanes crossing the area")
    close_nodes: bool = Field(True, description="Close ports/DCs/plants inside the area")


class RoadFloodParams(_Params):
    lanes: list[str] = Field(default_factory=list, description="Macro lanes affected; empty = all road lanes touching the target")
    sumo_edges: list[str] = Field(default_factory=list, description="Micro-twin (SUMO) edge ids to close")
    speed_factor: float = Field(0.1, gt=0, le=1, description="Remaining speed on flooded roads at severity 1")


class DemandSpikeParams(_Params):
    multiplier: float = Field(1.6, gt=0, le=20, description="Demand multiplier at severity 1")
    skus: list[str] = Field(default_factory=list, description="Affected SKUs; empty = all")


class SupplierFailureParams(_Params):
    capacity_factor: float = Field(0.0, ge=0, le=1, description="Remaining production capacity at severity 1")


class StrikeParams(_Params):
    throughput_factor: float = Field(0.3, ge=0, le=1, description="Remaining dispatch/handling throughput at severity 1")


class DataBlackoutParams(_Params):
    fraction: float = Field(0.3, gt=0, le=1, description="Share of telemetry sources that go silent at severity 1")
    sources: list[str] = Field(default_factory=list, description="Specific source ids; empty = random fraction")


PARAMS_MODEL: dict[ScenarioType, type[_Params]] = {
    ScenarioType.port_closure: PortClosureParams,
    ScenarioType.cyclone: CycloneParams,
    ScenarioType.road_flood: RoadFloodParams,
    ScenarioType.demand_spike: DemandSpikeParams,
    ScenarioType.supplier_failure: SupplierFailureParams,
    ScenarioType.strike: StrikeParams,
    ScenarioType.data_blackout: DataBlackoutParams,
}

# Which node types (or "lane" / "*") each scenario type may target.
ALLOWED_TARGETS: dict[ScenarioType, set[str]] = {
    ScenarioType.port_closure: {"port"},
    ScenarioType.cyclone: {"port", "dc", "plant", "supplier", "zone", "*"},
    ScenarioType.road_flood: {"lane", "dc", "plant", "supplier", "port", "zone"},
    ScenarioType.demand_spike: {"zone", "*"},
    ScenarioType.supplier_failure: {"plant", "supplier"},
    ScenarioType.strike: {"dc", "port", "plant", "supplier"},
    ScenarioType.data_blackout: {"*", "source"},
}

_REL = re.compile(r"^\+(\d+(?:\.\d+)?)([mhd])$")


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    type: ScenarioType
    target: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-*]+$")
    polygon: list[tuple[float, float]] | None = None
    start: str = "+0h"
    duration_h: float = Field(gt=0, le=24 * 90)
    severity: float = Field(1.0, ge=0, le=1)
    params: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(None, max_length=120, description="Optional human label (not part of the spec hash)")
    description: str | None = Field(None, max_length=600, description="Optional note (not part of the spec hash)")

    @field_validator("polygon")
    @classmethod
    def _polygon(cls, v: list[tuple[float, float]] | None) -> list[tuple[float, float]] | None:
        if v is None:
            return v
        for lon, lat in v:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(f"polygon point ({lon}, {lat}) is not a valid [lon, lat]")
        if len({(round(a, 6), round(b, 6)) for a, b in v}) < 3:
            raise ValueError("polygon needs at least 3 distinct [lon, lat] points")
        return v if v[0] == v[-1] else [*v, v[0]]  # close the ring

    @field_validator("start")
    @classmethod
    def _start(cls, v: str) -> str:
        v = v.strip()
        if v == "now" or _REL.match(v):
            return v
        try:
            datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError('start must be "now", "+<n>m|h|d" or an ISO-8601 timestamp') from e
        return v

    @model_validator(mode="after")
    def _typed_params(self) -> Scenario:
        model = PARAMS_MODEL[self.type]
        self.params = model.model_validate(self.params).model_dump()  # fills defaults, rejects unknown keys
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def p(self) -> _Params:
        """Typed params object (e.g. CycloneParams)."""
        return PARAMS_MODEL[self.type].model_validate(self.params)

    def start_offset_h(self, sim_start: datetime) -> float:
        """Hours after the simulation start at which the disruption begins (never negative)."""
        if self.start == "now":
            return 0.0
        m = _REL.match(self.start)
        if m:
            return float(m.group(1)) * {"m": 1 / 60, "h": 1.0, "d": 24.0}[m.group(2)]
        ts = datetime.fromisoformat(self.start)
        if ts.tzinfo is None and sim_start.tzinfo is not None:
            ts = ts.replace(tzinfo=sim_start.tzinfo)
        return max(0.0, (ts - sim_start) / timedelta(hours=1))

    def scaled(self, full_effect: float, no_effect: float) -> float:
        """Interpolate a parameter by severity: severity 0 -> no_effect, 1 -> full_effect."""
        return no_effect + self.severity * (full_effect - no_effect)

    def canonical_json(self) -> str:
        core = self.model_dump(mode="json", exclude={"name", "description"})
        return json.dumps(core, sort_keys=True, separators=(",", ":"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def validate_against(self, node_types: dict[str, str], lane_ids: Iterable[str], source_ids: Iterable[str] = ()) -> None:
        """Check the target (and id lists in params) exist and suit the scenario type."""
        lanes, sources = set(lane_ids), set(source_ids)
        if self.target == "*":
            kind = "*"
        elif self.target in node_types:
            kind = node_types[self.target]
        elif self.target in lanes:
            kind = "lane"
        elif self.target in sources:
            kind = "source"
        else:
            raise ValueError(f"unknown target {self.target!r}")
        if kind not in ALLOWED_TARGETS[self.type]:
            raise ValueError(f"{self.type.value} cannot target a {kind} ({self.target}); allowed: {sorted(ALLOWED_TARGETS[self.type])}")
        if self.type is ScenarioType.cyclone and self.target == "*" and self.polygon is None:
            raise ValueError("a network-wide cyclone needs a polygon")
        for lane in self.params.get("lanes", []):
            if lane not in lanes:
                raise ValueError(f"unknown lane {lane!r} in params.lanes")
        for node in self.params.get("divert_to", []):
            if node_types.get(node) != "port":
                raise ValueError(f"divert_to {node!r} is not a port")

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        return cls.model_validate_json(Path(path).read_text())


def point_in_polygon(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting on a closed [lon, lat] ring (fine for the small areas scenarios describe)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def write_json_schema(path: Path) -> None:
    schema = Scenario.model_json_schema()
    schema["$comment"] = "params are validated per type: " + ", ".join(
        f"{t.value} -> {m.__name__} {sorted(m.model_fields)}" for t, m in PARAMS_MODEL.items())
    path.write_text(json.dumps(schema, indent=2) + "\n")

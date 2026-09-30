"""Telemetry wire format published by the Reality Emulator (and accepted by the Phase 4 ingest API).

Every message is an envelope
    {"msg_id", "source_id", "kind", "ts", "seq", "payload", "sig"}
    msg_id     globally unique id (dedupe key)
    source_id  device / system that produced it: gps:<vehicle>, wms:<dc>, asn:<supplier>, port:<port>
    kind       gps | stock | asn | port
    ts         device timestamp, ISO-8601 with offset, millisecond precision
    seq        per-source counter (monotonic for an honest device)
    sig        hex HMAC-SHA256 over the canonical JSON of the other fields, keyed by the device key
               device_key = HMAC-SHA256(master, source_id)
Batches travel to POST /api/v1/ingest/{telemetry|inventory|supplier} (gps + port -> telemetry,
stock -> inventory, asn -> supplier) with publisher headers (see telemetry.HttpSink).
The payload models are strict (no coercion, no extra keys, finite numbers, ranges) - trust layer 1.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KINDS = ("gps", "stock", "asn", "port")
CHANNEL = {"gps": "telemetry", "port": "telemetry", "stock": "inventory", "asn": "supplier"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _finite(v: float) -> float:
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError("must be a finite number")
    return v


class GpsPayload(_Strict):
    vehicle_id: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=250)
    heading_deg: float = Field(ge=0, lt=360)
    hdop: float = Field(ge=0, le=50)
    shipment_id: str | None = None
    scope: Literal["city", "national"]

    _f = field_validator("lat", "lon", "speed_kmh", "heading_deg", "hdop")(_finite)


class StockPayload(_Strict):
    node: str
    sku: str
    on_hand: float = Field(ge=0)
    on_order: float = Field(ge=0)
    backlog: float = Field(ge=0)

    _f = field_validator("on_hand", "on_order", "backlog")(_finite)


class AsnPayload(_Strict):
    asn_id: str
    supplier: str
    dc: str
    sku: str
    qty: float = Field(gt=0)
    ship_ts: str
    eta_ts: str
    lanes: list[str]

    _f = field_validator("qty")(_finite)

    @field_validator("ship_ts", "eta_ts")
    @classmethod
    def _iso(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v


class PortPayload(_Strict):
    port: str
    status: Literal["up", "degraded", "closed"]
    berths_total: int = Field(ge=0)
    berths_busy: int = Field(ge=0)
    berth_queue: int = Field(ge=0)
    anchorage: int = Field(ge=0)
    customs: int = Field(ge=0)


PAYLOAD = {"gps": GpsPayload, "stock": StockPayload, "asn": AsnPayload, "port": PortPayload}


class Envelope(_Strict):
    msg_id: str = Field(min_length=8, max_length=64)
    source_id: str = Field(min_length=3, max_length=96)
    kind: Literal["gps", "stock", "asn", "port"]
    ts: str
    seq: int = Field(ge=0)
    payload: dict
    sig: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("ts")
    @classmethod
    def _ts(cls, v: str) -> str:
        if datetime.fromisoformat(v).tzinfo is None:
            raise ValueError("ts needs a UTC offset")
        return v

    def typed_payload(self):
        return PAYLOAD[self.kind].model_validate(self.payload)

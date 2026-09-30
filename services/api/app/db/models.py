"""PLAN §7 data model (SQLAlchemy 2). `inventory` and `telemetry` are time series: TimescaleDB hypertables
with compression when the extension is available, otherwise plain tables with BRIN + btree indexes
(see migrations/versions/0001_initial.py)."""
from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Table, Text,
                        func)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def ts_col(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), index=True)   # supplier | port | plant | dc | zone
    name: Mapped[str] = mapped_column(String(160))
    geom = mapped_column(Geography("POINT", srid=4326))
    capacity: Mapped[float] = mapped_column(Float, default=0)
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="up")


class LaneRow(Base):
    __tablename__ = "lanes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    from_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    to_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))
    distance_km: Mapped[float] = mapped_column(Float)
    cost_per_unit: Mapped[float] = mapped_column(Float)
    capacity: Mapped[float] = mapped_column(Float)
    co2_per_tkm: Mapped[float] = mapped_column(Float)
    lt_mu: Mapped[float] = mapped_column(Float)
    lt_sigma: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="ok")


class SkuRow(Base):
    __tablename__ = "skus"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    family: Mapped[str] = mapped_column(String(32))
    unit_value: Mapped[float] = mapped_column(Float)
    perishable: Mapped[bool] = mapped_column(Boolean, default=False)
    shelf_life_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    cold_chain: Mapped[bool] = mapped_column(Boolean, default=False)


inventory = Table(
    "inventory", Base.metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("node_id", String(64), nullable=False),
    Column("sku_id", String(32), nullable=False),
    Column("on_hand", Float), Column("on_order", Float), Column("backorder", Float),
    Column("source", String(96)), Column("trust", Float),
    Index("ix_inventory_node_sku_ts", "node_id", "sku_id", "ts"),
)

telemetry = Table(
    "telemetry", Base.metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("vehicle_id", String(64), nullable=False),
    Column("lat", Float), Column("lon", Float), Column("speed", Float), Column("heading", Float),
    Column("source_id", String(96)), Column("sig_ok", Boolean), Column("trust", Float),
    Column("flags", ARRAY(Text)), Column("msg_id", String(64)),
    Index("ix_telemetry_vehicle_ts", "vehicle_id", "ts"),
)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    zone_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id"))
    qty: Mapped[float] = mapped_column(Float)
    created_ts: Mapped[datetime] = ts_col()
    promised_ts: Mapped[datetime | None] = ts_col(nullable=True)
    fulfilled_ts: Mapped[datetime | None] = ts_col(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")


class ShipmentRow(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    lane_id: Mapped[str | None] = mapped_column(ForeignKey("lanes.id"), nullable=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id"))
    qty: Mapped[float] = mapped_column(Float)
    vehicle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    depart_ts: Mapped[datetime | None] = ts_col(nullable=True)
    eta_pred: Mapped[datetime | None] = ts_col(nullable=True)
    eta_p10: Mapped[datetime | None] = ts_col(nullable=True)
    eta_p90: Mapped[datetime | None] = ts_col(nullable=True)
    arrive_ts: Mapped[datetime | None] = ts_col(nullable=True)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    hmac_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trust_score: Mapped[float] = mapped_column(Float, default=1.0)
    last_seen: Mapped[datetime | None] = ts_col(nullable=True)
    sla_s: Mapped[float] = mapped_column(Float, default=30.0)


class Quarantine(Base):
    __tablename__ = "quarantine"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = ts_col(server_default=func.now(), index=True)
    source_id: Mapped[str | None] = mapped_column(String(96), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    reason_code: Mapped[str] = mapped_column(String(32), index=True)
    layer: Mapped[str] = mapped_column(String(4))


class Disruption(Base):
    __tablename__ = "disruptions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    spec: Mapped[dict] = mapped_column(JSONB)
    start_ts: Mapped[datetime | None] = ts_col(nullable=True)
    end_ts: Mapped[datetime | None] = ts_col(nullable=True)
    status: Mapped[str] = mapped_column(String(16))


class ScenarioRow(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    spec: Mapped[dict] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(String(64), index=True)
    n_reps: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = ts_col(server_default=func.now())


class PlanRow(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"), index=True)
    actions: Mapped[list] = mapped_column(JSONB)
    kpis: Mapped[dict] = mapped_column(JSONB)
    score: Mapped[float] = mapped_column(Float)
    applied_at: Mapped[datetime | None] = ts_col(nullable=True)
    applied_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String(96), index=True)
    metric: Mapped[str] = mapped_column(String(32))
    made_ts: Mapped[datetime] = ts_col()
    horizon_h: Mapped[float] = mapped_column(Float)
    value: Mapped[float] = mapped_column(Float)
    p10: Mapped[float | None] = mapped_column(Float, nullable=True)
    p90: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_ts: Mapped[datetime | None] = ts_col(nullable=True)


class AttackLabel(Base):
    __tablename__ = "attack_labels"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[datetime] = ts_col(server_default=func.now())
    type: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(96), nullable=True)
    injected_by: Mapped[str] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = ts_col(server_default=func.now(), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(96), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class Role(Base):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(ForeignKey("roles.name"))
    created_at: Mapped[datetime] = ts_col(server_default=func.now())

"""Initial schema (PLAN §7): master data, time series, operations, scenarios, security.

Revision ID: 0001
Revises:
Create Date: 2026-10-01
"""
import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from services.api.app.db import timescale

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("create extension if not exists postgis")
    op.create_table("schema_info", sa.Column("key", sa.String(64), primary_key=True), sa.Column("value", sa.Text))

    # ---------------------------------------------------------------- master data
    op.create_table(
        "nodes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("geom", Geography("POINT", srid=4326, spatial_index=False)),
        sa.Column("capacity", sa.Float, server_default="0"),
        sa.Column("attrs", JSONB, server_default="{}"),
        sa.Column("status", sa.String(16), server_default="up"),
    )
    op.create_index("ix_nodes_geom", "nodes", ["geom"], postgresql_using="gist")
    op.create_table(
        "lanes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("from_id", sa.String(64), sa.ForeignKey("nodes.id"), nullable=False, index=True),
        sa.Column("to_id", sa.String(64), sa.ForeignKey("nodes.id"), nullable=False, index=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("distance_km", sa.Float), sa.Column("cost_per_unit", sa.Float), sa.Column("capacity", sa.Float),
        sa.Column("co2_per_tkm", sa.Float), sa.Column("lt_mu", sa.Float), sa.Column("lt_sigma", sa.Float),
        sa.Column("status", sa.String(16), server_default="ok"),
    )
    op.create_table(
        "skus",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("unit_value", sa.Float), sa.Column("perishable", sa.Boolean, server_default="false"),
        sa.Column("shelf_life_h", sa.Float, nullable=True), sa.Column("cold_chain", sa.Boolean, server_default="false"),
    )

    # ---------------------------------------------------------------- time series
    op.create_table(
        "inventory",
        sa.Column("ts", TZ, nullable=False), sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("sku_id", sa.String(32), nullable=False), sa.Column("on_hand", sa.Float), sa.Column("on_order", sa.Float),
        sa.Column("backorder", sa.Float), sa.Column("source", sa.String(96)), sa.Column("trust", sa.Float),
    )
    op.create_index("ix_inventory_node_sku_ts", "inventory", ["node_id", "sku_id", sa.text("ts desc")])
    op.create_table(
        "telemetry",
        sa.Column("ts", TZ, nullable=False), sa.Column("vehicle_id", sa.String(64), nullable=False),
        sa.Column("lat", sa.Float), sa.Column("lon", sa.Float), sa.Column("speed", sa.Float), sa.Column("heading", sa.Float),
        sa.Column("source_id", sa.String(96)), sa.Column("sig_ok", sa.Boolean), sa.Column("trust", sa.Float),
        sa.Column("flags", ARRAY(sa.Text)), sa.Column("msg_id", sa.String(64)),
    )
    op.create_index("ix_telemetry_vehicle_ts", "telemetry", ["vehicle_id", sa.text("ts desc")])

    # ---------------------------------------------------------------- operations
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("zone_id", sa.String(64), sa.ForeignKey("nodes.id")), sa.Column("sku_id", sa.String(32), sa.ForeignKey("skus.id")),
        sa.Column("qty", sa.Float), sa.Column("created_ts", TZ), sa.Column("promised_ts", TZ, nullable=True),
        sa.Column("fulfilled_ts", TZ, nullable=True), sa.Column("status", sa.String(16), server_default="open"),
    )
    op.create_table(
        "shipments",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("lane_id", sa.String(32), sa.ForeignKey("lanes.id"), nullable=True),
        sa.Column("sku_id", sa.String(32), sa.ForeignKey("skus.id")), sa.Column("qty", sa.Float),
        sa.Column("vehicle_id", sa.String(64), nullable=True), sa.Column("status", sa.String(16)),
        sa.Column("depart_ts", TZ, nullable=True), sa.Column("eta_pred", TZ, nullable=True), sa.Column("eta_p10", TZ, nullable=True),
        sa.Column("eta_p90", TZ, nullable=True), sa.Column("arrive_ts", TZ, nullable=True),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.String(96), primary_key=True), sa.Column("type", sa.String(16)),
        sa.Column("hmac_key_hash", sa.String(64), nullable=True), sa.Column("trust_score", sa.Float, server_default="1"),
        sa.Column("last_seen", TZ, nullable=True), sa.Column("sla_s", sa.Float, server_default="30"),
    )
    op.create_table(
        "quarantine",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TZ, server_default=sa.func.now(), index=True),
        sa.Column("source_id", sa.String(96), index=True), sa.Column("payload", JSONB),
        sa.Column("reason_code", sa.String(32), index=True), sa.Column("layer", sa.String(4)),
    )
    op.create_table(
        "disruptions",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("spec", JSONB),
        sa.Column("start_ts", TZ, nullable=True), sa.Column("end_ts", TZ, nullable=True), sa.Column("status", sa.String(16)),
    )

    # ---------------------------------------------------------------- scenarios / plans / evaluation
    op.create_table(
        "scenarios",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("spec", JSONB),
        sa.Column("spec_hash", sa.String(64), index=True), sa.Column("n_reps", sa.Integer), sa.Column("status", sa.String(16)),
        sa.Column("results", JSONB, nullable=True), sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now()),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scenario_id", sa.String(40), sa.ForeignKey("scenarios.id"), index=True),
        sa.Column("actions", JSONB), sa.Column("kpis", JSONB), sa.Column("score", sa.Float),
        sa.Column("applied_at", TZ, nullable=True), sa.Column("applied_by", sa.String(64), nullable=True),
    )
    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entity", sa.String(96), index=True), sa.Column("metric", sa.String(32)), sa.Column("made_ts", TZ),
        sa.Column("horizon_h", sa.Float), sa.Column("value", sa.Float), sa.Column("p10", sa.Float, nullable=True),
        sa.Column("p90", sa.Float, nullable=True), sa.Column("actual", sa.Float, nullable=True), sa.Column("actual_ts", TZ, nullable=True),
    )
    op.create_table(
        "attack_labels",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("ts", TZ, server_default=sa.func.now()),
        sa.Column("type", sa.String(32)), sa.Column("target", sa.String(96), nullable=True), sa.Column("injected_by", sa.String(64)),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TZ, server_default=sa.func.now(), index=True), sa.Column("user_id", sa.String(64)),
        sa.Column("action", sa.String(64)), sa.Column("target", sa.String(96), nullable=True),
        sa.Column("details", JSONB, server_default="{}"),
    )
    op.create_table("roles", sa.Column("name", sa.String(32), primary_key=True), sa.Column("rank", sa.Integer))
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("display_name", sa.String(120)),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), sa.ForeignKey("roles.name")), sa.Column("created_at", TZ, server_default=sa.func.now()),
    )
    op.bulk_insert(sa.table("roles", sa.column("name"), sa.column("rank")),
                   [{"name": "viewer", "rank": 0}, {"name": "planner", "rank": 1}, {"name": "security", "rank": 2},
                    {"name": "admin", "rank": 3}])

    timescale.enable(op.get_bind())


def downgrade() -> None:
    for t in ("users", "roles", "audit_log", "attack_labels", "predictions", "plans", "scenarios", "disruptions", "quarantine",
              "sources", "shipments", "orders", "telemetry", "inventory", "skus", "lanes", "nodes", "schema_info"):
        op.drop_table(t)

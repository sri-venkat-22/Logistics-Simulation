"""Phase 8 security: pgcrypto, rotated device keys (encrypted at rest), audit index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("create extension if not exists pgcrypto")
    op.create_table(
        "device_keys",
        sa.Column("source_id", sa.String(96), primary_key=True),
        sa.Column("key_enc", BYTEA, nullable=False),          # pgp_sym_encrypt(hex key, AEGIS_KEY_SECRET)
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("rotated_at", TZ, nullable=False),
        sa.Column("prev_enc", BYTEA),
        sa.Column("prev_until", TZ),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_action", "audit_log")
    op.drop_table("device_keys")

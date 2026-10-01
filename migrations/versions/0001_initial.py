"""Crossing history: crossing_events and camera_count_snapshots.

Revision ID: 0001
Revises:
Create Date: 2026-10-01

Hand-written against `traffic_ai.db.models`; keep the two in step. Creates no rows —
there is no seed data, and `plate_text` stays NULL until a plate model supplies one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crossing_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("vehicle_class", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("crossed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("plate_text", sa.String(length=32), nullable=True),
        sa.Column("plate_confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_crossing_events_camera_crossed_at",
        "crossing_events",
        ["camera_id", sa.text("crossed_at DESC")],
    )
    op.create_index("ix_crossing_events_crossed_at", "crossing_events", ["crossed_at"])

    op.create_table(
        "camera_count_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("vehicle_class", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("congestion", sa.String(length=16), nullable=False),
        sa.Column("throughput_per_min", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "camera_id",
            "captured_at",
            "direction",
            "vehicle_class",
            name="uq_camera_count_snapshots_bucket",
        ),
    )


def downgrade() -> None:
    op.drop_table("camera_count_snapshots")
    op.drop_index("ix_crossing_events_crossed_at", table_name="crossing_events")
    op.drop_index("ix_crossing_events_camera_crossed_at", table_name="crossing_events")
    op.drop_table("crossing_events")

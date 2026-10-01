"""Schema shape, asserted straight off the SQLAlchemy metadata — no database.

These guard the properties that are expensive to fix after data exists: a naive
timestamp column, a missing index, a NOT NULL on a field that is legitimately empty.
"""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Enum, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, Table

from traffic_ai.db.models import Base, CameraCountSnapshotRow, CrossingEventRow


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_tables_are_registered_under_their_documented_names() -> None:
    assert set(Base.metadata.tables) == {"crossing_events", "camera_count_snapshots"}
    assert CrossingEventRow.__tablename__ == "crossing_events"
    assert CameraCountSnapshotRow.__tablename__ == "camera_count_snapshots"


def test_crossing_events_has_exactly_the_crossing_event_fields_plus_bookkeeping() -> None:
    assert set(_table("crossing_events").columns.keys()) == {
        "id",
        "camera_id",
        "track_id",
        "vehicle_class",
        "direction",
        "crossed_at",
        "confidence",
        "plate_text",
        "plate_confidence",
        "created_at",
    }


def test_crossing_events_has_a_surrogate_bigint_primary_key() -> None:
    pk = list(_table("crossing_events").primary_key.columns)

    assert [c.name for c in pk] == ["id"]
    assert pk[0].type.__class__.__name__ == "BigInteger"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("crossing_events", "crossed_at"),
        ("crossing_events", "created_at"),
        ("camera_count_snapshots", "captured_at"),
    ],
)
def test_timestamp_columns_are_timezone_aware(table: str, column: str) -> None:
    col_type = _table(table).columns[column].type

    assert isinstance(col_type, DateTime)
    assert col_type.timezone is True


def test_only_the_plate_fields_are_nullable_on_crossing_events() -> None:
    nullable = {c.name for c in _table("crossing_events").columns if c.nullable}

    # None means "not read" — everything else is always known at crossing time.
    assert nullable == {"plate_text", "plate_confidence"}


def test_every_snapshot_column_is_required() -> None:
    assert not [c.name for c in _table("camera_count_snapshots").columns if c.nullable]


def test_created_at_is_defaulted_by_the_server() -> None:
    created_at = _table("crossing_events").columns["created_at"]

    assert created_at.server_default is not None
    assert "now()" in str(created_at.server_default.arg).lower() or "now" in str(
        created_at.server_default.arg
    )


def test_plate_text_has_no_default_so_it_can_never_be_invented() -> None:
    plate = _table("crossing_events").columns["plate_text"]

    assert plate.default is None
    assert plate.server_default is None


def test_no_column_uses_a_native_postgres_enum() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Enum), f"{table.name}.{column.name}"


def test_the_per_camera_history_index_leads_with_camera_and_sorts_time_descending() -> None:
    index = next(
        i
        for i in _table("crossing_events").indexes
        if i.name == "ix_crossing_events_camera_crossed_at"
    )
    first, second = index.expressions

    assert first.name == "camera_id"
    assert "crossed_at DESC" in str(second)
    sql = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "(camera_id, crossed_at DESC)" in sql


def test_there_is_a_crossed_at_only_index_for_cross_camera_range_queries() -> None:
    index = next(
        i for i in _table("crossing_events").indexes if i.name == "ix_crossing_events_crossed_at"
    )

    assert [c.name for c in index.columns] == ["crossed_at"]


def test_crossing_events_carries_no_other_indexes() -> None:
    assert {i.name for i in _table("crossing_events").indexes} == {
        "ix_crossing_events_camera_crossed_at",
        "ix_crossing_events_crossed_at",
    }


def test_snapshots_are_unique_per_camera_time_direction_and_class() -> None:
    uniques = [
        c for c in _table("camera_count_snapshots").constraints if isinstance(c, UniqueConstraint)
    ]

    assert len(uniques) == 1
    assert [c.name for c in uniques[0].columns] == [
        "camera_id",
        "captured_at",
        "direction",
        "vehicle_class",
    ]
    assert uniques[0].name == "uq_camera_count_snapshots_bucket"


def test_direction_and_class_are_short_strings() -> None:
    for table in ("crossing_events", "camera_count_snapshots"):
        for name in ("direction", "vehicle_class"):
            col_type = _table(table).columns[name].type
            assert col_type.__class__.__name__ == "String"
            assert col_type.length is not None
            assert col_type.length <= 32


def test_every_known_domain_value_fits_its_column() -> None:
    from traffic_ai.domain import VEHICLE_CLASSES, CongestionLevel, Direction

    direction_len = _table("crossing_events").columns["direction"].type.length
    class_len = _table("crossing_events").columns["vehicle_class"].type.length
    congestion_len = _table("camera_count_snapshots").columns["congestion"].type.length

    assert all(len(d.value) <= direction_len for d in Direction)
    assert all(len(c) <= class_len for c in VEHICLE_CLASSES)
    assert all(len(c.value) <= congestion_len for c in CongestionLevel)

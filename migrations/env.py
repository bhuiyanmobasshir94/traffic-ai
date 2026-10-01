"""Alembic environment.

The URL comes from `Settings.database_url`, not `alembic.ini`: one source of truth
for which database this is, shared with the running service.

The driver is asyncpg, so migrations run on an async engine. Alembic's migration
API is synchronous; `connection.run_sync` is the documented bridge between the two.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from traffic_ai.config import get_settings
from traffic_ai.db.models import Base

config = context.config

# `disable_existing_loggers=False` so importing this module under the application
# (e.g. a programmatic `alembic upgrade` at startup) does not silence its loggers.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # NullPool: a migration is a one-shot process, so there is nothing to pool and
    # no idle connection to leave open against the server when it exits.
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

import asyncio
import os
import re
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure the app package is importable when alembic runs from /app directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url from environment
db_url = os.getenv("DATABASE_URL", "")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

# Tenant schema: programmatic (app.migrate sets config.attributes) or CLI
# (alembic -x tenant_schema=t_foo upgrade head). None = legacy single-tenant
# mode against public — kept only so old dev databases can still be inspected;
# production runs go through app.migrate which always passes a schema.
_TENANT_SCHEMA = config.attributes.get(
    "tenant_schema"
) or context.get_x_argument(as_dictionary=True).get("tenant_schema")
if _TENANT_SCHEMA and not re.fullmatch(r"t_[a-z][a-z0-9_]{1,30}", _TENANT_SCHEMA):
    raise ValueError(f"invalid tenant_schema {_TENANT_SCHEMA!r}")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=_TENANT_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    if _TENANT_SCHEMA:
        # Unqualified DDL in revisions 001-012 lands in the first schema of
        # the search_path — i.e. the tenant schema.
        connection.exec_driver_sql(
            f"SET search_path TO {_TENANT_SCHEMA}, shared, public"
        )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=_TENANT_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

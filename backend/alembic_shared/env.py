import os
import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import create_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Shared track owns no ORM metadata — registry tables are raw-SQL revisions.
target_metadata = None


def _db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def run_migrations_online() -> None:
    engine = create_engine(_db_url(), future=True)
    with engine.connect() as connection:
        # The version table lives in `shared`, which S001 itself creates —
        # bootstrap the schema before alembic touches the version table.
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS shared")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="shared",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline mode is not supported for the shared track")
run_migrations_online()

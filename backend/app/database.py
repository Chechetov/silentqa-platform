import re
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from tenancy.context import get_tenant_schema

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_SCHEMA_RE = re.compile(r"^t_[a-z][a-z0-9_]{1,30}$")


@event.listens_for(engine.sync_engine, "begin")
def _set_tenant_search_path(conn):
    """SET LOCAL per transaction — the pooled connection comes back clean."""
    schema = get_tenant_schema()
    if schema:
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError(f"invalid tenant schema {schema!r}")
        conn.exec_driver_sql(
            f"SET LOCAL search_path TO {schema}, shared, public"
        )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session

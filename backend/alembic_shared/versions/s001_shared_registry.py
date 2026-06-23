"""Shared registry: tenants + platform_admins.

Revision ID: s001
Revises:
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS shared")
    op.execute(
        """CREATE TABLE shared.tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT NOT NULL UNIQUE,
            schema_name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended')),
            api_key_hash TEXT,
            api_key_required BOOLEAN NOT NULL DEFAULT TRUE,
            company_config_id TEXT,
            custom_domains TEXT[] NOT NULL DEFAULT '{}',
            dashboard_base_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE shared.platform_admins (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login TIMESTAMPTZ
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE shared.platform_admins")
    op.execute("DROP TABLE shared.tenants")

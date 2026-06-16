"""shared.tenants.modules jsonb — per-tenant module flags (universal dashboard).

Revision ID: s004
Revises: s003
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s004"
down_revision: Union[str, None] = "s003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE shared.tenants "
        "ADD COLUMN IF NOT EXISTS modules jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    # Backfill by EXPLICIT rule for ALL existing tenants (not by slug list):
    #   knowledge_base — left implicit (resolver defaults it ON);
    #   complexes/amocrm — ON only for realestate, OFF (implicit) for the rest.
    op.execute(
        "UPDATE shared.tenants "
        "SET modules = modules || '{\"complexes\": true, \"amocrm\": true}'::jsonb "
        "WHERE slug = 'realestate'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE shared.tenants DROP COLUMN IF EXISTS modules")

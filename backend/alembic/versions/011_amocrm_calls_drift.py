"""Capture schema drift: amocrm_calls.responsible_user_id was added on prod
by hand, outside migrations. IF NOT EXISTS makes this idempotent both for the
realestate schema (column already there) and for fresh tenant schemas.

Revision ID: 011
Revises: 010
"""
from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE amocrm_calls ADD COLUMN IF NOT EXISTS responsible_user_id BIGINT"
    )


def downgrade() -> None:
    # The column predates the migration history on prod — never drop it.
    pass

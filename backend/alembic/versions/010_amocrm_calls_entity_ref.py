"""Add entity_type/entity_id to amocrm_calls for recording re-resolution

A call is now ingested as soon as its AmoCRM start-event appears — before the
recording is necessarily published. To re-read the note later (and pick up the
recording once it lands) the worker needs to know which entity the note hangs
off. The poll already has this from the event; we just persist it.

Revision ID: 010
Revises: 009
Create Date: 2026-05-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE amocrm_calls ADD COLUMN entity_type VARCHAR(20)")
    op.execute("ALTER TABLE amocrm_calls ADD COLUMN entity_id BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE amocrm_calls DROP COLUMN entity_id")
    op.execute("ALTER TABLE amocrm_calls DROP COLUMN entity_type")

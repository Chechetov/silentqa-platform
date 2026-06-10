"""Add amocrm_deal_summaries table for the rolling per-lead summary note

Revision ID: 003
Revises: 002
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""CREATE TABLE amocrm_deal_summaries (
        lead_id BIGINT PRIMARY KEY,
        amo_note_id BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        content_json JSONB NOT NULL
    )""")
    op.execute("CREATE INDEX idx_deal_summaries_updated ON amocrm_deal_summaries(updated_at)")


def downgrade() -> None:
    op.drop_index("idx_deal_summaries_updated")
    op.drop_table("amocrm_deal_summaries")

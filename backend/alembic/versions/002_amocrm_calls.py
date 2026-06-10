"""Add amocrm_calls table for tracking polled calls

Revision ID: 002
Revises: 001
Create Date: 2026-04-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""CREATE TABLE amocrm_calls (
        id SERIAL PRIMARY KEY,
        amo_note_id BIGINT UNIQUE NOT NULL,
        lead_id BIGINT NOT NULL,
        contact_phone VARCHAR(20),
        direction VARCHAR(10),
        duration INTEGER,
        recording_url TEXT,
        session_id VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        error_message TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed_at TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX idx_amocrm_calls_status ON amocrm_calls(status)")
    op.execute("CREATE INDEX idx_amocrm_calls_created ON amocrm_calls(created_at)")


def downgrade() -> None:
    op.drop_index("idx_amocrm_calls_status")
    op.drop_index("idx_amocrm_calls_created")
    op.drop_table("amocrm_calls")

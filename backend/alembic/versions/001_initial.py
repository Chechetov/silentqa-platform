"""Initial migration - sessions and chunks tables

Revision ID: 001
Revises:
Create Date: 2026-03-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSON

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE session_status AS ENUM ('created', 'uploading', 'processing', 'completed', 'failed')")

    op.execute("""CREATE TABLE sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        status session_status NOT NULL DEFAULT 'created',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        duration_seconds DOUBLE PRECISION,
        file_size_bytes INTEGER,
        metadata JSONB
    )""")

    op.execute("""CREATE TABLE chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        chunk_number INTEGER NOT NULL,
        file_path VARCHAR(512) NOT NULL,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("CREATE INDEX ix_chunks_session_id ON chunks(session_id)")


def downgrade() -> None:
    op.drop_index("ix_chunks_session_id")
    op.drop_table("chunks")
    op.drop_table("sessions")
    op.execute("DROP TYPE IF EXISTS session_status")

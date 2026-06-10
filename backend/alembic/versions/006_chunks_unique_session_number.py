"""Add UNIQUE(session_id, chunk_number) on chunks for atomic upsert.

Revision ID: 006
Revises: 005
Create Date: 2026-04-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive: drop dupes (legacy max+1 logic shouldn't have produced them,
    # but make the migration safe anyway). Keeps the lowest id per (session,n).
    op.execute(
        """
        DELETE FROM chunks a USING chunks b
        WHERE a.session_id = b.session_id
          AND a.chunk_number = b.chunk_number
          AND a.ctid > b.ctid
        """
    )
    op.create_unique_constraint(
        "uq_chunks_session_number",
        "chunks",
        ["session_id", "chunk_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_chunks_session_number", "chunks", type_="unique")

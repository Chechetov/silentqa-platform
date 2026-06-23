"""Knowledge Base: kb_categories, kb_entries, kb_entry_mentions (spec §1).

Revision ID: 014
Revises: 013
"""
from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE kb_categories (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text NOT NULL,
            slug        text NOT NULL,
            description text,
            feeds_asr   boolean NOT NULL DEFAULT false,
            feeds_llm   boolean NOT NULL DEFAULT false,
            is_taxonomy boolean NOT NULL DEFAULT false,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (slug)
        )
    """)
    op.execute("""
        CREATE TABLE kb_entries (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            category_id uuid NOT NULL REFERENCES kb_categories(id) ON DELETE CASCADE,
            term        text NOT NULL,
            aliases     jsonb NOT NULL DEFAULT '[]'::jsonb,
            description text,
            metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (category_id, term)
        )
    """)
    op.execute("CREATE INDEX ix_kb_entries_category ON kb_entries (category_id)")
    op.execute("""
        CREATE TABLE kb_entry_mentions (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entry_id   uuid NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            count      integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (entry_id, session_id)
        )
    """)
    op.execute("CREATE INDEX ix_kb_mentions_session ON kb_entry_mentions (session_id, entry_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_entry_mentions")
    op.execute("DROP TABLE IF EXISTS kb_entries")
    op.execute("DROP TABLE IF EXISTS kb_categories")

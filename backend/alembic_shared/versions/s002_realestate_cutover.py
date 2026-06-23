"""Cutover: move legacy public tables into t_realestate, transfer the alembic
stamp, register the realestate tenant.

Conditional: a fresh database (no public.sessions) skips the move entirely —
new installs get tenants only via the provisioning CLI.

Revision ID: s002
Revises: s001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s002"
down_revision: Union[str, None] = "s001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Order matters only for readability; SET SCHEMA carries indexes, constraints
# and sequences with each table. The session_status ENUM type intentionally
# stays in public (search_path includes public last).
TABLES = (
    "sessions",
    "chunks",
    "extraction_templates",
    "complexes",
    "complex_extractions",
    "brokers",
    "amocrm_calls",
    "amocrm_deal_summaries",
)

LEGACY_DASHBOARD = "https://rogov.automate-it.fun"


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT to_regclass('public.sessions')")).scalar():
        return  # fresh install — nothing to cut over

    op.execute("CREATE SCHEMA IF NOT EXISTS t_realestate")
    for t in TABLES:
        op.execute(f"ALTER TABLE public.{t} SET SCHEMA t_realestate")

    # Transfer the single-track stamp ('010') into the tenant schema so the
    # tenant track resumes from 010 and applies 011+ only.
    if conn.execute(
        sa.text("SELECT to_regclass('public.alembic_version')")
    ).scalar():
        op.execute(
            "CREATE TABLE t_realestate.alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        op.execute(
            "INSERT INTO t_realestate.alembic_version "
            "SELECT version_num FROM public.alembic_version"
        )
        op.execute("DROP TABLE public.alembic_version")

    # api_key_required=false: already-deployed recorder clients send no key
    # until the Phase-1 rollout (spec 6.4) finishes.
    op.execute(
        sa.text(
            "INSERT INTO shared.tenants (slug, schema_name, display_name, "
            "status, api_key_required, company_config_id, custom_domains, "
            "dashboard_base_url) VALUES ('realestate', 't_realestate', "
            "'Агентство недвижимости', 'active', FALSE, 'realestate', "
            "ARRAY['rogov.automate-it.fun'], :dash) "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(dash=LEGACY_DASHBOARD)
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not conn.execute(
        sa.text("SELECT to_regclass('t_realestate.sessions')")
    ).scalar():
        return
    if conn.execute(
        sa.text("SELECT to_regclass('t_realestate.alembic_version')")
    ).scalar():
        op.execute(
            "CREATE TABLE public.alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        op.execute(
            "INSERT INTO public.alembic_version "
            "SELECT version_num FROM t_realestate.alembic_version"
        )
        op.execute("DROP TABLE t_realestate.alembic_version")
    for t in reversed(TABLES):
        op.execute(f"ALTER TABLE t_realestate.{t} SET SCHEMA public")
    op.execute("DELETE FROM shared.tenants WHERE slug = 'realestate'")
    op.execute("DROP SCHEMA t_realestate")

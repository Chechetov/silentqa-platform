"""Two-track migration runner.

Replaces the bare ``alembic upgrade head`` startup hook: the shared track
(registry + cutover) runs first, then the tenant track is applied to every
active tenant schema. Idempotent — safe to run on every backend start.

Usage: python -m app.migrate   (cwd-independent; paths derived from __file__)
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))  # repo root → import tenancy

from tenancy.db import shared_connect  # noqa: E402
from tenancy.identifiers import validate_schema_name  # noqa: E402


def _config(ini_name: str, script_dir: str) -> Config:
    cfg = Config(str(BACKEND_DIR / ini_name))
    cfg.set_main_option("script_location", str(BACKEND_DIR / script_dir))
    return cfg


def run_shared() -> None:
    command.upgrade(_config("alembic_shared.ini", "alembic_shared"), "head")


def run_tenant(schema_name: str) -> None:
    validate_schema_name(schema_name)
    cfg = _config("alembic.ini", "alembic")
    cfg.attributes["tenant_schema"] = schema_name
    command.upgrade(cfg, "head")


def _active_schemas() -> list[str]:
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> None:
    run_shared()
    for schema in _active_schemas():
        print(f"[migrate] tenant track → {schema}", flush=True)
        run_tenant(schema)
    print("[migrate] done", flush=True)


if __name__ == "__main__":
    main()

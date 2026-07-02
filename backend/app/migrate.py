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
from alembic.script import ScriptDirectory

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


def _script_head(ini_name: str, script_dir: str) -> str | None:
    return ScriptDirectory.from_config(_config(ini_name, script_dir)).get_current_head()


def _db_version(cur, schema: str) -> str | None:
    """version_num схемы или None, если alembic_version ещё нет."""
    cur.execute("SELECT to_regclass(%s)", (f"{schema}.alembic_version",))
    if cur.fetchone()[0] is None:
        return None
    cur.execute(f'SELECT version_num FROM "{schema}".alembic_version')
    row = cur.fetchone()
    return row[0] if row else None


def check() -> list[str]:
    """Схемы, отстающие от head'а скриптов. Read-only, ничего не мигрирует."""
    mismatched: list[str] = []
    shared_head = _script_head("alembic_shared.ini", "alembic_shared")
    tenant_head = _script_head("alembic.ini", "alembic")
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            if _db_version(cur, "shared") != shared_head:
                mismatched.append("shared")
            cur.execute(
                "SELECT schema_name FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            schemas = [r[0] for r in cur.fetchall()]
            for schema in schemas:
                validate_schema_name(schema)  # последний рубеж перед интерполяцией
                if _db_version(cur, schema) != tenant_head:
                    mismatched.append(schema)
    finally:
        conn.close()
    return mismatched


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


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        mismatched = check()
        if mismatched:
            print("[migrate] ОТСТАЮТ: " + ", ".join(mismatched), flush=True)
            raise SystemExit(1)
        print("[migrate] heads OK", flush=True)
        return
    run_shared()
    for schema in _active_schemas():
        print(f"[migrate] tenant track → {schema}", flush=True)
        run_tenant(schema)
    print("[migrate] done", flush=True)


if __name__ == "__main__":
    main()

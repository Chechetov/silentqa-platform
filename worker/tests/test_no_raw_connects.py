"""No application code may call psycopg2.connect directly — only tenancy.db.

The tenant search_path is injected per-connection by the factory; a stray
direct connect silently reads/writes the wrong schema after the cutover.
"""
import re
from pathlib import Path

WORKER_TASKS = Path(__file__).resolve().parents[1] / "tasks"
BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

# Exception: amocrm_sync.py's portal-token reader keeps its own connection —
# it queries the schema-qualified portal.amocrm_tokens table (global resource).
CONNECT_RE = re.compile(r"psycopg2\.connect\(")


def _offending(root: Path) -> list[str]:
    hits = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if CONNECT_RE.search(line):
                if f.name == "amocrm_sync.py" and "portal" in text:
                    continue
                hits.append(f"{f.name}:{i}: {line.strip()}")
    return hits


def test_no_direct_psycopg2_connect_in_worker_tasks():
    assert _offending(WORKER_TASKS) == []


def test_no_direct_psycopg2_connect_in_backend_app():
    assert _offending(BACKEND_APP) == []

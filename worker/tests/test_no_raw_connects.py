"""No application code may call psycopg2.connect directly — only tenancy.db.

The tenant search_path is injected per-connection by the factory; a stray
direct connect silently reads/writes the wrong schema after the cutover.
"""
import ast
import re
from pathlib import Path

WORKER_TASKS = Path(__file__).resolve().parents[1] / "tasks"
BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

CONNECT_RE = re.compile(r"psycopg2\.connect\(")

# The one sanctioned direct connection: amocrm_sync._read_token_from_portal_db
# queries the schema-qualified portal.amocrm_tokens table (global resource,
# owned by the partner portal — not tenant data).
ALLOWED = {("amocrm_sync.py", "_read_token_from_portal_db")}


def _allowed_lines(path: Path, text: str) -> set[int]:
    """Line numbers covered by sanctioned functions in this file."""
    names = {fn for fname, fn in ALLOWED if fname == path.name}
    if not names:
        return set()
    lines: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _offending(root: Path) -> list[str]:
    hits = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        allowed = _allowed_lines(f, text)
        for i, line in enumerate(text.splitlines(), 1):
            if CONNECT_RE.search(line) and i not in allowed:
                hits.append(f"{f.name}:{i}: {line.strip()}")
    return hits


def test_no_direct_psycopg2_connect_in_worker_tasks():
    assert _offending(WORKER_TASKS) == []


def test_no_direct_psycopg2_connect_in_backend_app():
    assert _offending(BACKEND_APP) == []


def test_exception_is_scoped_to_portal_token_reader(tmp_path):
    """A future direct connect elsewhere in amocrm_sync.py must be flagged,
    even though the file mentions "portal" (the old file-wide exception
    let any connect through on that substring)."""
    fake = tmp_path / "amocrm_sync.py"
    fake.write_text(
        "# reads the portal token elsewhere, but this connect is NOT sanctioned\n"
        "import psycopg2\n"
        "\n"
        "def _read_token_from_portal_db():\n"
        "    return None\n"
        "\n"
        "def something_else():\n"
        "    return psycopg2.connect('dbname=x')\n",
        encoding="utf-8",
    )
    hits = _offending(tmp_path)
    assert len(hits) == 1 and hits[0].startswith("amocrm_sync.py:8")

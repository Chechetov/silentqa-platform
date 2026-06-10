"""
One-shot AmoCRM users → brokers sync.

Fetches all users from AmoCRM /api/v4/users (paginated) and upserts them into
the `brokers` table. Idempotent: running it 100 times is safe. Never overwrites
`password_hash`, `claimed_at`, or `is_active` on existing rows — only `email`
and `name` are refreshed.

Usage (from /root/projects/realestate/worker):

    ../.venv/bin/python3 -m scripts.sync_brokers
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter

import psycopg2
from dotenv import load_dotenv

# `python -m scripts.sync_brokers` from the worker root puts cwd on sys.path
# automatically; no manipulation needed.
from tasks.amocrm_sync import AMOCRM_BASE_URL, _amo_request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sync_brokers")


def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


def _fetch_all_users() -> list[dict]:
    """Page through /api/v4/users and return the full user list."""
    users: list[dict] = []
    url: str | None = f"{AMOCRM_BASE_URL}/api/v4/users?limit=50"

    while url:
        resp = _amo_request("GET", url, timeout=15)
        if resp.status_code == 204:
            # No content — empty account.
            break
        if resp.status_code != 200:
            raise RuntimeError(
                f"AmoCRM /api/v4/users returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        data = resp.json()
        page_users = data.get("_embedded", {}).get("users", []) or []
        users.extend(page_users)

        next_link = (
            data.get("_links", {}).get("next", {}).get("href")
            if isinstance(data.get("_links"), dict)
            else None
        )
        url = next_link

    return users


SELECT_PREV_SQL = "SELECT email, name FROM brokers WHERE amocrm_user_id = %s"
INSERT_SQL = (
    "INSERT INTO brokers (amocrm_user_id, email, name) VALUES (%s, %s, %s)"
)
UPDATE_SQL = (
    "UPDATE brokers SET email = %s, name = %s, updated_at = NOW() "
    "WHERE amocrm_user_id = %s"
)


def main() -> int:
    db_url = _get_sync_db_url()
    if not db_url:
        print("ERROR: DATABASE_URL / DATABASE_URL_SYNC not set", file=sys.stderr)
        return 1

    try:
        users = _fetch_all_users()
    except Exception as exc:
        print(f"ERROR: failed to fetch AmoCRM users: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(users)} users from AmoCRM")

    # Pre-flight: detect duplicate emails. login-by-email becomes ambiguous
    # otherwise — refuse rather than silently create a footgun.
    email_counts = Counter(
        (u.get("email") or "").strip().lower()
        for u in users
        if (u.get("email") or "").strip()
    )
    dupes = [e for e, c in email_counts.items() if c > 1]
    if dupes:
        print(
            f"ERROR: duplicate emails in AmoCRM ({len(dupes)}): "
            f"{', '.join(dupes)}. Resolve in AmoCRM and re-run.",
            file=sys.stderr,
        )
        return 1

    try:
        conn = psycopg2.connect(db_url)
    except Exception as exc:
        print(f"ERROR: failed to connect to database: {exc}", file=sys.stderr)
        return 1

    new_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0

    try:
        with conn:
            with conn.cursor() as cur:
                for u in users:
                    amo_id = u.get("id")
                    email = (u.get("email") or "").strip()
                    name = (u.get("name") or "").strip()

                    if not amo_id:
                        print(
                            f"WARN: skipping user with no amocrm id: "
                            f"name={name!r} email={email!r}"
                        )
                        skipped_count += 1
                        continue
                    if not email:
                        # Can't log in without an email — log per-row so a
                        # typo in AmoCRM is noticed, not lost in aggregate.
                        print(
                            f"WARN: skipping amocrm_id={amo_id} name={name!r} — no email"
                        )
                        skipped_count += 1
                        continue
                    if not name:
                        # Fallback: fill placeholder name; admin can fix later.
                        # Use bare placeholder so the desktop UI shows a clean
                        # "[no name]" line instead of "<email> [no name] (<email>)".
                        name = "[no name]"
                        print(
                            f"WARN: amocrm_id={amo_id} email={email} has empty name; "
                            f"using fallback '{name}'"
                        )

                    cur.execute(SELECT_PREV_SQL, (amo_id,))
                    prev = cur.fetchone()
                    if prev is None:
                        cur.execute(INSERT_SQL, (amo_id, email, name))
                        new_count += 1
                        tag = "[NEW]"
                    elif prev[0] != email or prev[1] != name:
                        cur.execute(UPDATE_SQL, (email, name, amo_id))
                        updated_count += 1
                        tag = "[UPDATE]"
                    else:
                        unchanged_count += 1
                        tag = "[UNCHANGED]"

                    print(
                        f"{tag} amocrm_id={amo_id} email={email} name={name}"
                    )
    except Exception as exc:
        print(f"ERROR: database operation failed: {exc}", file=sys.stderr)
        try:
            conn.close()
        except Exception:
            pass
        return 1

    conn.close()

    total = new_count + updated_count + unchanged_count
    print(
        f"Synced {total} brokers "
        f"({new_count} new, {updated_count} updated, "
        f"{unchanged_count} unchanged)"
    )
    if skipped_count:
        print(f"Skipped {skipped_count} users (no id/email)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

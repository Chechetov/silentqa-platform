"""Бутстрап платформенного админа.

  python -m app.platform_admin create --email boss@x.io [--password ...]

Пароль печатается ОДИН раз (если сгенерирован).
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from argon2 import PasswordHasher

from tenancy.db import shared_connect, get_sync_db_url


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--email", required=True)
    c.add_argument("--password", default="")
    args = p.parse_args()

    if not get_sync_db_url():
        sys.exit("DATABASE_URL_SYNC / DATABASE_URL is not set")

    password = args.password or secrets.token_urlsafe(12)
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shared.platform_admins (email, password_hash) "
                "VALUES (%s, %s)",
                (args.email, PasswordHasher().hash(password)),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"platform admin: {args.email}")
    if not args.password:
        print(f"password (shown once, store it now): {password}")


if __name__ == "__main__":
    main()

"""Tenant provisioning CLI.

  python -m app.provision_tenant acme --name "ACME Corp" \
      --admin-email admin@acme.ru [--admin-password ...]

  python -m app.provision_tenant realestate --seed-only \
      --admin-email alex.chechetov@gmail.com

--seed-only: tenant already exists (the cutover created realestate) — only
create the first admin user and the API key. The API-key plaintext is printed
ONCE to stdout; only its sha256 lands in shared.tenants.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from argon2 import PasswordHasher

from tenancy.db import shared_connect, get_sync_db_url
from tenancy.identifiers import schema_for_slug, validate_slug


class ProvisionError(RuntimeError):
    """Бизнес-отказ провижининга (slug занят и т.п.)."""


@dataclass
class ProvisionResult:
    slug: str
    schema: str
    admin_email: str
    admin_password: str
    api_key: str

    def __repr__(self) -> str:
        return (f"ProvisionResult(slug={self.slug!r}, schema={self.schema!r}, "
                f"admin_email={self.admin_email!r}, admin_password='***', "
                f"api_key='***')")


def generate_api_key() -> tuple[str, str]:
    key = f"sqa_{secrets.token_urlsafe(32)}"
    return key, hashlib.sha256(key.encode()).hexdigest()


def generate_password() -> str:
    return secrets.token_urlsafe(12)


def _seed_admin(conn, schema: str, email: str, password: str) -> None:
    ph = PasswordHasher()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema}.users (email, password_hash, role) "
            "VALUES (%s, %s, 'admin')",
            (email, ph.hash(password)),
        )


def _set_api_key(conn, slug: str) -> str:
    key, digest = generate_api_key()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shared.tenants SET api_key_hash = %s WHERE slug = %s",
            (digest, slug),
        )
    return key


def provision(
    slug: str,
    name: str,
    admin_email: str,
    admin_password: str = "",
) -> ProvisionResult:
    """Создать тенанта целиком: схема → миграции → admin → API-ключ.

    ValueError — кривой slug (до коннекта); ProvisionError — slug занят.
    Частичный провал после первого commit оставляет полусозданного
    тенанта — зачистка по ранбуку (спека §3.2), автоочистки нет.
    """
    slug = validate_slug(slug)
    schema = schema_for_slug(slug)
    password = admin_password or generate_password()

    conn = shared_connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM shared.tenants WHERE slug = %s", (slug,))
            if cur.fetchone() is not None:
                raise ProvisionError(f"tenant {slug!r} already exists")
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(
                "INSERT INTO shared.tenants "
                "(slug, schema_name, display_name, status, modules) "
                "VALUES (%s, %s, %s, 'active', %s::jsonb)",
                (slug, schema, name or slug,
                 '{"knowledge_base": true, "complexes": false, "amocrm": false}'),
            )
        conn.commit()
        from app.migrate import run_tenant

        run_tenant(schema)
        _seed_admin(conn, schema, admin_email, password)
        key = _set_api_key(conn, slug)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return ProvisionResult(
        slug=slug,
        schema=schema,
        admin_email=admin_email,
        admin_password=password,
        api_key=key,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug")
    p.add_argument("--name", default="")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-password", default="")
    p.add_argument("--seed-only", action="store_true",
                   help="tenant exists; only seed admin user + API key")
    args = p.parse_args()

    slug = validate_slug(args.slug)
    schema = schema_for_slug(slug)
    if not get_sync_db_url():
        sys.exit("DATABASE_URL_SYNC / DATABASE_URL is not set")
    password = args.admin_password or getpass.getpass(
        f"Password for {args.admin_email}: "
    )

    if args.seed_only:
        # Seed-only branch: tenant already exists — only create admin + API key.
        # Поведение сохраняется бит-в-бит.
        conn = shared_connect()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM shared.tenants WHERE slug = %s", (slug,)
                )
                exists = cur.fetchone() is not None
            if not exists:
                sys.exit(f"tenant {slug!r} not found (run without --seed-only)")
            _seed_admin(conn, schema, args.admin_email, password)
            key = _set_api_key(conn, slug)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        print(f"tenant: {slug}  schema: {schema}")
        print(f"admin:  {args.admin_email}")
        print(f"API key (shown once, store it now): {key}")
    else:
        try:
            res = provision(slug, args.name, args.admin_email, password)
        except ProvisionError as e:
            sys.exit(str(e))
        print(f"tenant: {res.slug}  schema: {res.schema}")
        print(f"admin:  {res.admin_email}")
        if not args.admin_password:
            print(f"admin password (shown once): {res.admin_password}")
        print(f"API key (shown once, store it now): {res.api_key}")


if __name__ == "__main__":
    main()

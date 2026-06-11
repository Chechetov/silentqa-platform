"""HTTP-plane tenant resolution: Host header → shared.tenants → contextvar.

Pure ASGI middleware (not BaseHTTPMiddleware): cheaper and contextvar
semantics are explicit. Resolution order (spec 3.2):
  1. exact match against custom_domains  → tenant  (legacy prod domain)
  2. <slug>.BASE_DOMAIN                  → tenant by slug
  3. apex / admin.BASE_DOMAIN            → platform (no tenant)
  4. anything else → DEFAULT_TENANT if set, else platform
Unknown slug / suspended tenant → 404.
"""
from __future__ import annotations

import time

from sqlalchemy import text
from starlette.responses import JSONResponse

from tenancy.context import reset_tenant_schema, set_tenant_schema


class TenantRegistry:
    """TTL-cached snapshot of shared.tenants (async engine)."""

    def __init__(self, ttl_seconds: float = 30.0):
        self._ttl = ttl_seconds
        self._rows: list[dict] = []
        self._loaded_at = 0.0

    async def all_tenants(self) -> list[dict]:
        if time.monotonic() - self._loaded_at > self._ttl:
            from app.database import async_session

            async with async_session() as db:
                res = await db.execute(
                    text(
                        "SELECT slug, schema_name, status, custom_domains "
                        "FROM shared.tenants"
                    )
                )
                self._rows = [
                    {
                        "slug": r.slug,
                        "schema_name": r.schema_name,
                        "status": r.status,
                        "custom_domains": list(r.custom_domains or []),
                    }
                    for r in res
                ]
            self._loaded_at = time.monotonic()
        return self._rows


class TenantResolutionMiddleware:
    def __init__(self, app, registry, base_domain: str, default_tenant: str = ""):
        self.app = app
        self.registry = registry
        self.base_domain = base_domain.lower()
        self.default_tenant = default_tenant

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        host = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value.decode("latin-1").split(":")[0].lower()
                break

        kind, row = await self._resolve(host)
        if kind == "notfound":
            resp = JSONResponse({"detail": "Unknown tenant"}, status_code=404)
            return await resp(scope, receive, send)

        token = set_tenant_schema(row["schema_name"] if row else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_tenant_schema(token)

    async def _resolve(self, host: str):
        rows = await self.registry.all_tenants()

        for r in rows:
            if host in r["custom_domains"]:
                return self._gate(r)

        if host == self.base_domain or host == f"admin.{self.base_domain}":
            return "platform", None

        suffix = f".{self.base_domain}"
        if host.endswith(suffix):
            slug = host[: -len(suffix)]
            if "." in slug:  # only single-label subdomains are tenants
                return "notfound", None
            for r in rows:
                if r["slug"] == slug:
                    return self._gate(r)
            return "notfound", None

        if self.default_tenant:
            for r in rows:
                if r["slug"] == self.default_tenant:
                    return self._gate(r)
            return "notfound", None
        return "platform", None

    @staticmethod
    def _gate(row):
        if row["status"] != "active":
            return "notfound", None
        return "tenant", row

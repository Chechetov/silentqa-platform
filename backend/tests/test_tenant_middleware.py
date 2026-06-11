"""Tenant resolution middleware: host → tenant schema contextvar."""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.tenancy_http import TenantResolutionMiddleware
from tenancy.context import get_tenant_schema


class StubRegistry:
    """In-memory stand-in for the shared.tenants lookup."""

    def __init__(self):
        self.rows = [
            {
                "slug": "realestate",
                "schema_name": "t_realestate",
                "status": "active",
                "custom_domains": ["rogov.automate-it.fun"],
                "api_key_hash": None,
                "api_key_required": False,
            },
            {
                "slug": "frozen",
                "schema_name": "t_frozen",
                "status": "suspended",
                "custom_domains": [],
                "api_key_hash": None,
                "api_key_required": False,
            },
        ]

    async def all_tenants(self):
        return self.rows


def _app(default_tenant=""):
    async def whoami(request):
        tenant = request.scope.get("state", {}).get("tenant")
        return JSONResponse(
            {
                "schema": get_tenant_schema(),
                "api_key_required": (tenant or {}).get("api_key_required"),
            }
        )

    app = Starlette(routes=[Route("/whoami", whoami)])
    return TestClient(
        app=TenantResolutionMiddleware(
            app,
            registry=StubRegistry(),
            base_domain="silentqa.com",
            default_tenant=default_tenant,
        ),
        base_url="http://testserver",
    )


def test_subdomain_resolves_tenant():
    c = _app()
    r = c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert r.json()["schema"] == "t_realestate"


def test_custom_domain_resolves_tenant():
    c = _app()
    r = c.get("/whoami", headers={"host": "rogov.automate-it.fun"})
    assert r.json()["schema"] == "t_realestate"


def test_unknown_subdomain_404():
    c = _app()
    r = c.get("/whoami", headers={"host": "ghost.silentqa.com"})
    assert r.status_code == 404


def test_suspended_tenant_404():
    c = _app()
    r = c.get("/whoami", headers={"host": "frozen.silentqa.com"})
    assert r.status_code == 404


def test_apex_and_admin_are_platform():
    c = _app()
    for host in ("silentqa.com", "admin.silentqa.com"):
        r = c.get("/whoami", headers={"host": host})
        assert r.json()["schema"] is None, host


def test_foreign_host_uses_default_tenant():
    c = _app(default_tenant="realestate")
    r = c.get("/whoami", headers={"host": "localhost:8002"})
    assert r.json()["schema"] == "t_realestate"


def test_foreign_host_without_default_is_platform():
    c = _app()
    r = c.get("/whoami", headers={"host": "localhost:8002"})
    assert r.json()["schema"] is None


def test_context_reset_after_request():
    c = _app()
    c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert get_tenant_schema() is None


def test_tenant_row_lands_in_request_state():
    c = _app()
    r = c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert r.status_code == 200
    assert r.json()["api_key_required"] is False


def test_platform_contour_state_tenant_is_none():
    c = _app()
    r = c.get("/whoami", headers={"host": "silentqa.com"})
    assert r.status_code == 200
    assert r.json()["api_key_required"] is None

"""Per-tenant module flags (universal dashboard).

Default semantics (spec §5.1): a flag missing from tenants.modules resolves to
its DEFAULT here — NOT to "off" and NOT to "all on". knowledge_base is generic
(ON for everyone); RE-specific modules are strict opt-in (OFF).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

MODULE_DEFAULTS: dict[str, bool] = {
    "knowledge_base": True,
    "complexes": False,
    "amocrm": False,
}


def module_enabled(modules: dict | None, name: str) -> bool:
    m = modules or {}
    if name in m:
        return bool(m[name])
    return MODULE_DEFAULTS.get(name, False)


def _tenant_modules(request: Request) -> dict:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        # platform contour — no tenant modules
        raise HTTPException(status_code=404, detail="Unknown tenant")
    return tenant.get("modules") or {}


def require_module(name: str):
    """Dependency factory: 403 module_disabled if `name` is off for this tenant."""

    async def _dep(request: Request) -> None:
        if not module_enabled(_tenant_modules(request), name):
            raise HTTPException(status_code=403, detail="module_disabled")

    return _dep

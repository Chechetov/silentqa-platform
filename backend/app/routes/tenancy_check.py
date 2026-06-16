"""GET /api/tenancy/domain-check — гейт для Caddy on_demand_tls ask.

Caddy перед выпуском сертификата спрашивает: 200 = хост наш (apex, admin,
поддомен активного тенанта или его custom_domain), 404 = серт не минтить.
Открытый эндпоинт без тенант-контекста (Caddy ходит на localhost); утечки
нет — список хостов и так публичен через DNS/CT-логи.

GET /api/tenancy/features — резолв module-флагов текущего тенанта для SPA.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.modules import MODULE_DEFAULTS, module_enabled
from app.tenancy_http import registry as _registry

router = APIRouter(prefix="/api/tenancy", tags=["tenancy"])


@router.get("/domain-check")
async def domain_check(domain: str = ""):
    host = (domain or "").strip().lower().rstrip(".")
    base = settings.BASE_DOMAIN.lower()
    if not host:
        raise HTTPException(status_code=404, detail="unknown domain")
    if host in (base, f"admin.{base}"):
        return {"ok": True}
    for r in await _registry.all_tenants():
        if r["status"] != "active":
            continue
        if host == f"{r['slug']}.{base}" or host in r["custom_domains"]:
            return {"ok": True}
    raise HTTPException(status_code=404, detail="unknown domain")


@router.get("/features")
async def get_features(request: Request):
    """Resolved module flags for the current tenant (used by the SPA to gate UI).

    Reads request.state.tenant — correct under impersonation (target tenant).
    """
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    modules = tenant.get("modules") or {}
    return {name: module_enabled(modules, name) for name in MODULE_DEFAULTS}

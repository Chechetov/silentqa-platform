import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.body_limit import BodySizeLimitMiddleware
from app.config import settings
from app.migrate import check as migrate_check
from app.routes import (
    chunks, sessions, companies, transcripts, analysis, managers, stats,
    amocrm, templates, complexes, knowledge, auth, user_auth, tenancy_check, platform_auth,
    platform_tenants, users, eval_profiles,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Миграции применяются на ДЕПЛОЕ (scripts/deploy_prod.sh, run.sh,
    # staging ExecStartPre), не на старте: упавшая миграция одного тенанта
    # не должна ронять бэкенд для всех. Здесь — только быстрая read-only
    # сверка head'ов с CRITICAL-логом.
    try:
        mismatched = await run_in_threadpool(migrate_check)
    except Exception:
        logging.getLogger(__name__).critical(
            "migrate check failed (БД недоступна?)", exc_info=True)
    else:
        if mismatched:
            logging.getLogger(__name__).critical(
                "Alembic heads расходятся: %s — запусти `python -m app.migrate`",
                ", ".join(mismatched))
    yield


app = FastAPI(
    title="Meeting Recorder", version="1.0.0", lifespan=lifespan,
    # C-3: schema-эндпоинты живут вне /api/* и не покрываются контур-гейтом —
    # наружу их не светим; включаются явным флагом (стейдж/локаль).
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

# Глобальный потолок тела (C-2). Добавлен ДО CORS: последний add_middleware —
# внешний, т.е. стек tenant → CORS → body-limit, и 413 уходит с CORS-заголовками.
app.add_middleware(
    BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_MB * 1024 * 1024)

# CORS: список origin'ов из настроек. Дефолт "*" — расширения и десктоп
# ходят с origin chrome-extension://… / file://; сужать только вместе
# с ревизией клиентов записи (план 2/3).
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant resolution — outermost: unknown hosts 404 before anything else runs
from app.tenancy_http import registry, TenantResolutionMiddleware

app.add_middleware(
    TenantResolutionMiddleware,
    registry=registry,
    base_domain=settings.BASE_DOMAIN,
    default_tenant=settings.DEFAULT_TENANT,
)

# Routes
app.include_router(sessions.router)
app.include_router(chunks.router)
app.include_router(transcripts.router)
app.include_router(companies.router)
app.include_router(analysis.router)
app.include_router(managers.router)
app.include_router(stats.router)
app.include_router(amocrm.router)
app.include_router(templates.router)
app.include_router(complexes.router)
app.include_router(knowledge.router)
app.include_router(auth.router)
app.include_router(user_auth.router)
app.include_router(tenancy_check.router)
app.include_router(platform_auth.router)
app.include_router(platform_tenants.router)
app.include_router(users.router)
app.include_router(eval_profiles.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


from sqlalchemy import text

from app.database import engine


async def _db_ping() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@app.get("/health/ready")
async def health_ready():
    # Readiness для deploy_prod.sh: процесс жив И БД доступна.
    # /health остаётся статическим liveness.
    try:
        await _db_ping()
    except Exception:
        logging.getLogger(__name__).warning("readiness: БД недоступна", exc_info=True)
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
    return {"status": "ready"}


from tenancy.context import get_tenant_slug as _get_tenant_slug


@app.get("/", include_in_schema=False)
async def root_page():
    # Платформенный контур (apex и admin.) — админ-SPA; тенант — дашборд
    page = "admin.html" if _get_tenant_slug() is None else "index.html"
    return FileResponse(f"static/{page}")


# Static files — must be LAST (after all API routers) so it doesn't intercept API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")

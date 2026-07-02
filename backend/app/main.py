import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import (
    chunks, sessions, companies, transcripts, analysis, managers,
    amocrm, templates, complexes, knowledge, auth, user_auth, tenancy_check, platform_auth,
    platform_tenants, users, eval_profiles,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the two-track migration runner (shared registry first, then the
    # tenant track per active schema). Fresh process: env.py's asyncio.run
    # would clash with the already-running loop here, hence subprocess.
    result = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        import logging
        logging.getLogger(__name__).error(f"Alembic migration failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
        raise RuntimeError(f"Alembic migration failed: {result.stderr}")
    yield


app = FastAPI(title="Meeting Recorder", version="1.0.0", lifespan=lifespan)

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


from tenancy.context import get_tenant_slug as _get_tenant_slug


@app.get("/", include_in_schema=False)
async def root_page():
    # Платформенный контур (apex и admin.) — админ-SPA; тенант — дашборд
    page = "admin.html" if _get_tenant_slug() is None else "index.html"
    return FileResponse(f"static/{page}")


# Static files — must be LAST (after all API routers) so it doesn't intercept API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")

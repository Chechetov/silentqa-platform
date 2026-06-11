import base64
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.routes import (
    chunks, sessions, companies, transcripts, analysis, managers,
    amocrm, templates, complexes, auth, user_auth,
)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth on UI routes only. API endpoints are open for the Chrome extension."""

    OPEN_PREFIXES = ("/api/", "/health")

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(self.OPEN_PREFIXES):
            return await call_next(request)

        # Allow CORS preflight through
        if request.method == "OPTIONS":
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if auth:
            try:
                scheme, credentials = auth.split(" ", 1)
                if scheme.lower() == "basic":
                    decoded = base64.b64decode(credentials).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    if (
                        secrets.compare_digest(username, settings.AUTH_USERNAME)
                        and secrets.compare_digest(password, settings.AUTH_PASSWORD)
                    ):
                        return await call_next(request)
            except Exception:
                pass

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Meeting Recorder"'},
            content="Unauthorized",
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

# CORS — must be added before BasicAuth so preflight responses include CORS headers
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Basic Auth
app.add_middleware(BasicAuthMiddleware)

# Tenant resolution — outermost: unknown hosts 404 before anything else runs
from app.tenancy_http import TenantRegistry, TenantResolutionMiddleware

app.add_middleware(
    TenantResolutionMiddleware,
    registry=TenantRegistry(),
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
app.include_router(auth.router)
app.include_router(user_auth.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/recorder")
async def recorder_page():
    return FileResponse("static/recorder.html")

# Static files — must be LAST (after all API routers) so it doesn't intercept API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")

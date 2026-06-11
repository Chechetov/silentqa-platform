import base64
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager

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
    webhooks, amocrm, templates, complexes, auth,
)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth on UI routes only. API endpoints are open for the Chrome extension."""

    OPEN_PREFIXES = ("/api/", "/health")
    PROTECTED_PREFIXES = ("/api/templates", "/api/complexes", "/api/extractions")
    PROTECTED_METHODS = {"POST", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        # Destructive calls on templates/complexes/extractions need the delete password
        if (request.method in self.PROTECTED_METHODS
                and any(request.url.path.startswith(p) for p in self.PROTECTED_PREFIXES)):
            expected = settings.DELETE_PASSWORD
            if not expected:
                return Response(
                    status_code=503,
                    content='{"detail":"Mutations disabled: DELETE_PASSWORD not configured"}',
                    media_type="application/json",
                )
            provided = request.headers.get("X-Delete-Password")
            if not provided or not secrets.compare_digest(provided, expected):
                return Response(
                    status_code=401,
                    content='{"detail":"Invalid delete password"}',
                    media_type="application/json",
                )
            return await call_next(request)

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

# Routes
app.include_router(sessions.router)
app.include_router(chunks.router)
app.include_router(transcripts.router)
app.include_router(companies.router)
app.include_router(analysis.router)
app.include_router(managers.router)
app.include_router(webhooks.router)
app.include_router(amocrm.router)
app.include_router(templates.router)
app.include_router(complexes.router)
app.include_router(auth.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/recorder")
async def recorder_page():
    return FileResponse("static/recorder.html")

# Static files — must be LAST (after all API routers) so it doesn't intercept API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")

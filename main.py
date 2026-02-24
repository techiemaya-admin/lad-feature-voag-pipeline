"""
V2 Main Entry Point - FastAPI Application.

Phase 19: Slim entry point that imports from modular v2 components.

This file contains:
- FastAPI app initialization
- Route mounting from v2/api/routes/
- Middleware setup from v2/api/middleware.py
- Health check endpoints

NO BUSINESS LOGIC - just wiring and setup.

Usage:
    uvicorn v2.main:app --reload
    python v2/main.py
"""

import os
import json
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

# Load environment variables with explicit path (works when run from any directory)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env_path)

# ============================================================================
# NON-BLOCKING LOGGING SETUP (MUST BE BEFORE OTHER IMPORTS)
# ============================================================================
from utils.logger_config import configure_non_blocking_logging

# MAIN_PY_LOG_LEVEL takes precedence, falls back to LOG_LEVEL
_main_log_level = os.getenv("MAIN_PY_LOG_LEVEL") or os.getenv("LOG_LEVEL")
_log_listener = configure_non_blocking_logging(level=_main_log_level)

# Import routes
from api.routes import (
    calls_router,
    batch_router,
    agents_router,
    knowledge_base_router,
    oauth_router,
    oauth_microsoft_router,
    recordings_router,
    rag_manager_router,
    analysis_router,
)

# Import middleware setup
from api.middleware import setup_middlewares, setup_request_logging

logger = logging.getLogger(__name__)


# =============================================================================
# LIFESPAN EVENTS
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("V2 Voice Agent API starting up...")
    yield
    logger.info("V2 Voice Agent API shutting down...")


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title="Vonage Voice Agent API (V2)",
    description="Multi-tenant voice agent platform with LiveKit integration",
    version="2.0.0",
    lifespan=lifespan,
)

# Setup middlewares (CORS, security, rate limiting)
setup_middlewares(app)
setup_request_logging(app)


# =============================================================================
# ROUTES
# =============================================================================

# Call management (router has /calls prefix)
app.include_router(calls_router, tags=["Calls"])

# Batch operations (router has /batch prefix)
app.include_router(batch_router, tags=["Batch"])

# Agent CRUD (router has /agents prefix)
app.include_router(agents_router, tags=["Agents"])

# Knowledge base (router has /knowledge-base prefix)
app.include_router(knowledge_base_router, tags=["Knowledge Base"])

# OAuth - Google (router has /auth prefix)
app.include_router(oauth_router, tags=["OAuth - Google"])

# OAuth - Microsoft (router has /auth/microsoft prefix)
app.include_router(oauth_microsoft_router, tags=["OAuth - Microsoft"])

# Recordings (router has /recordings prefix)
app.include_router(recordings_router, tags=["Recordings"])

# RAG Management UI (router has /rag prefix)
app.include_router(rag_manager_router, tags=["RAG Management"])

# Analysis (router has /analysis prefix)
app.include_router(analysis_router, tags=["Analysis"])



# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint - health check."""
    return {"status": "ok", "version": "2.0.0", "service": "voice-agent-api"}


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Kubernetes liveness probe."""
    return {"status": "healthy"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Kubernetes readiness probe."""
    return {"status": "ready"}


# =============================================================================
# ERROR HANDLERS
# =============================================================================

def _debug_body_for_log(body: dict | None, path: str) -> str:
    """Return body suitable for debug log; truncate batch entries to 2."""
    if not body:
        return "null"
    if "entries" in body and path.startswith("/batch/") and isinstance(body["entries"], list):
        truncated = {**body, "entries": body["entries"][:2]}
        if len(body["entries"]) > 2:
            truncated["entries"].append("...")
        return json.dumps(truncated, default=str)
    return json.dumps(body, default=str)


async def _read_body_safe(request: Request) -> dict | None:
    try:
        return await request.json()
    except Exception:
        return None


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    fid = request.headers.get("X-Frontend-ID") or "null"
    body = _debug_body_for_log(exc.body if isinstance(exc.body, dict) else None, request.url.path)
    logger.debug("[INVALID_BODY] %s %s | frontend_id=%s | body=%s", request.method, request.url.path, fid, body)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 400:
        fid = request.headers.get("X-Frontend-ID") or "null"
        body = _debug_body_for_log(await _read_body_safe(request), request.url.path)
        logger.debug("[BAD_REQUEST] %s %s | frontend_id=%s | body=%s", request.method, request.url.path, fid, body)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors."""
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    # Suppress health check access logs (Docker pings /healthz every 30s)
    class _HealthCheckFilter(logging.Filter):
        _SUPPRESSED = {"/healthz", "/readyz", "/"}
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(f'"{path} ' in msg or f" {path} " in msg for path in self._SUPPRESSED)
    
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())
    
    port = int(os.getenv("PORT", "8020"))
    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info").lower(),
    )

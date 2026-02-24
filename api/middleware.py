"""
API Security Module

Provides frontend authentication and rate limiting for the Vonage Voice Agent API.
This module implements:
- Frontend ID + API key validation
- Tiered rate limiting based on endpoint sensitivity
- Malformed request tracking and blocking
"""

import os
import time
import logging
import hashlib
import base64
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any, Callable, Mapping
from functools import wraps

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("vonage.api_security")

# =============================================================================
# Configuration from Environment
# =============================================================================

def _load_frontend_credentials() -> dict[str, str]:
    """
    Load frontend credentials from FRONTEND_API_KEYS environment variable.
    Format: {"frontend_id": "api_key", ...}
    """
    raw = os.getenv("FRONTEND_API_KEYS", "{}")
    try:
        import json
        credentials = json.loads(raw)
        if not isinstance(credentials, dict):
            logger.warning("FRONTEND_API_KEYS is not a valid JSON object; using empty config")
            return {}
        return {str(k): str(v) for k, v in credentials.items()}
    except Exception as exc:
        logger.warning("Failed to parse FRONTEND_API_KEYS: %s; using empty config", exc)
        return {}


_FRONTEND_CREDENTIALS: dict[str, str] = _load_frontend_credentials()


def reload_frontend_credentials() -> None:
    """Reload frontend credentials from environment (for testing or hot reload)."""
    global _FRONTEND_CREDENTIALS
    _FRONTEND_CREDENTIALS = _load_frontend_credentials()
    logger.info("Reloaded frontend credentials: %d frontends configured", len(_FRONTEND_CREDENTIALS))


def is_security_enabled() -> bool:
    """Check if API security is enabled (at least one frontend configured)."""
    return len(_FRONTEND_CREDENTIALS) > 0


# =============================================================================
# Rate Limiting Configuration
# =============================================================================

@dataclass
class RateLimitConfig:
    """Configuration for rate limiting a specific endpoint tier."""
    requests_per_minute: int
    burst_limit: int  # Max requests in a 5-second window
    block_duration_seconds: int = 60  # How long to block after limit exceeded


# Tiered rate limits - adjust based on endpoint sensitivity
RATE_LIMIT_TIERS: dict[str, RateLimitConfig] = {
    # High-frequency endpoints (signed URLs, status checks)
    "high": RateLimitConfig(requests_per_minute=120, burst_limit=30),
    # Standard endpoints (calls, sessions)
    "standard": RateLimitConfig(requests_per_minute=30, burst_limit=10),
    # Sensitive endpoints (auth, email, schedule)
    "sensitive": RateLimitConfig(requests_per_minute=10, burst_limit=5),
    # Malformed request limit (applies across all endpoints)
    "malformed": RateLimitConfig(requests_per_minute=5, burst_limit=2, block_duration_seconds=120),
}

# Endpoint to tier mapping
ENDPOINT_TIERS: dict[str, str] = {
    # Health and status - exempt from auth but rate limited
    "/": "high",
    "/healthz": "high",
    "/security/status": "high",
    # High frequency
    "/recordings/signed-url": "high",
    "/recordings/calls/": "high",  # Prefix match
    "/auth/status": "high",
    "/auth/microsoft/status": "high",
    # Standard
    "/calls": "standard",
    "/calls/batch": "standard",
    "/ui/sessions": "standard",
    "/auth/microsoft/list-businesses": "standard",
    "/auth/microsoft/list-services": "standard",
    # Sensitive - Google
    "/auth/google/start": "sensitive",
    "/auth/google/callback": "sensitive",  # Exempt from auth (OAuth callback)
    "/auth/revoke": "sensitive",
    "/agent/schedule": "sensitive",
    "/agent/email": "sensitive",
    # Sensitive - Microsoft
    "/auth/microsoft/start": "sensitive",
    "/auth/microsoft/callback": "sensitive",  # Exempt from auth (OAuth callback)
    "/auth/microsoft/revoke": "sensitive",
    "/auth/microsoft/save-config": "sensitive",
}

# Endpoints that don't require frontend authentication
AUTH_EXEMPT_ENDPOINTS: frozenset[str] = frozenset({
    "/",
    "/healthz",
    "/security/status",  # Monitoring endpoint
    "/auth/google/callback",  # OAuth callback comes from Google, not frontend
    "/auth/microsoft/callback",  # OAuth callback comes from Microsoft, not frontend
    "/auth/microsoft/configure",  # Booking wizard UI served after OAuth
    "/auth/microsoft/logo",  # Logo asset for booking wizard
    "/batch/entry-completed",  # Internal worker callback (not public)
    "/rag/manage",  # RAG management UI (has its own login)
})

# Documentation endpoints - protected via HTTP Basic Auth (browser login popup)
# Username = X-Frontend-ID, Password = X-API-Key
DOCS_ENDPOINTS: frozenset[str] = frozenset({
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",  # Swagger OAuth redirect
})

# Static file prefixes to skip entirely
SKIP_PREFIXES: tuple[str, ...] = ("/ui/", "/static/", "/favicon")


def _get_endpoint_tier(path: str) -> str:
    """Determine the rate limit tier for a given endpoint path."""
    # Check exact match first
    if path in ENDPOINT_TIERS:
        return ENDPOINT_TIERS[path]
    # Check prefix matches
    for prefix, tier in ENDPOINT_TIERS.items():
        if prefix.endswith("/") and path.startswith(prefix):
            return tier
    # Check if path starts with a known base
    for prefix, tier in ENDPOINT_TIERS.items():
        if path.startswith(prefix.rstrip("/")):
            return tier
    return "standard"


# =============================================================================
# Rate Limiter Implementation
# =============================================================================

@dataclass
class RequestWindow:
    """Tracks requests in a time window."""
    timestamps: list[float] = field(default_factory=list)
    blocked_until: float = 0.0


class RateLimiter:
    """
    In-memory rate limiter with per-frontend tracking.
    
    Tracks both normal requests and malformed requests separately.
    """
    
    def __init__(self) -> None:
        # Key: (frontend_id, tier) -> RequestWindow
        self._windows: dict[tuple[str, str], RequestWindow] = defaultdict(RequestWindow)
        # Key: client_identifier -> RequestWindow for malformed requests
        self._malformed_windows: dict[str, RequestWindow] = defaultdict(RequestWindow)
        self._lock_cleanup_interval = 60.0
        self._last_cleanup = time.monotonic()
    
    def _cleanup_old_entries(self, now: float) -> None:
        """Remove stale entries to prevent memory growth."""
        if now - self._last_cleanup < self._lock_cleanup_interval:
            return
        
        cutoff = now - 120  # Keep 2 minutes of history
        
        # Clean normal windows
        stale_keys = [
            key for key, window in self._windows.items()
            if window.blocked_until < now and (not window.timestamps or window.timestamps[-1] < cutoff)
        ]
        for key in stale_keys:
            del self._windows[key]
        
        # Clean malformed windows
        stale_malformed = [
            key for key, window in self._malformed_windows.items()
            if window.blocked_until < now and (not window.timestamps or window.timestamps[-1] < cutoff)
        ]
        for key in stale_malformed:
            del self._malformed_windows[key]
        
        self._last_cleanup = now
    
    def check_rate_limit(
        self,
        frontend_id: str,
        tier: str,
    ) -> tuple[bool, str | None]:
        """
        Check if a request is allowed under rate limits.
        
        Returns:
            (allowed, error_message) - allowed is True if request should proceed
        """
        now = time.monotonic()
        self._cleanup_old_entries(now)
        
        config = RATE_LIMIT_TIERS.get(tier, RATE_LIMIT_TIERS["standard"])
        key = (frontend_id, tier)
        window = self._windows[key]
        
        # Check if blocked
        if window.blocked_until > now:
            remaining = int(window.blocked_until - now)
            return False, f"Rate limit exceeded. Retry after {remaining} seconds."
        
        # Clean old timestamps
        minute_ago = now - 60
        five_seconds_ago = now - 5
        window.timestamps = [ts for ts in window.timestamps if ts > minute_ago]
        
        # Check per-minute limit
        if len(window.timestamps) >= config.requests_per_minute:
            window.blocked_until = now + config.block_duration_seconds
            return False, f"Rate limit exceeded ({config.requests_per_minute}/min). Retry after {config.block_duration_seconds} seconds."
        
        # Check burst limit
        recent_count = sum(1 for ts in window.timestamps if ts > five_seconds_ago)
        if recent_count >= config.burst_limit:
            return False, f"Burst limit exceeded ({config.burst_limit}/5s). Slow down."
        
        # Allow request
        window.timestamps.append(now)
        return True, None
    
    def record_malformed_request(self, client_id: str) -> tuple[bool, str | None]:
        """
        Record a malformed request and check if client should be blocked.
        
        Returns:
            (should_block, error_message)
        """
        now = time.monotonic()
        config = RATE_LIMIT_TIERS["malformed"]
        window = self._malformed_windows[client_id]
        
        # Check if already blocked
        if window.blocked_until > now:
            remaining = int(window.blocked_until - now)
            return True, f"Too many invalid requests. Blocked for {remaining} seconds."
        
        # Clean old timestamps
        minute_ago = now - 60
        window.timestamps = [ts for ts in window.timestamps if ts > minute_ago]
        window.timestamps.append(now)
        
        # Check limits
        if len(window.timestamps) >= config.requests_per_minute:
            window.blocked_until = now + config.block_duration_seconds
            return True, f"Too many invalid requests. Blocked for {config.block_duration_seconds} seconds."
        
        # Check burst (consecutive bad requests)
        five_seconds_ago = now - 5
        recent_count = sum(1 for ts in window.timestamps if ts > five_seconds_ago)
        if recent_count >= config.burst_limit:
            window.blocked_until = now + config.block_duration_seconds
            return True, f"Too many consecutive invalid requests. Blocked for {config.block_duration_seconds} seconds."
        
        return False, None


# Global rate limiter instance
_rate_limiter = RateLimiter()


# =============================================================================
# Authentication Helpers
# =============================================================================

def _hash_key(api_key: str) -> str:
    """Create a timing-safe hash of an API key for comparison."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def validate_frontend_credentials(frontend_id: str | None, api_key: str | None) -> tuple[bool, str | None]:
    """
    Validate frontend credentials.
    
    Returns:
        (valid, error_message) - valid is True if credentials are correct
    """
    if not is_security_enabled():
        # Security not configured, allow all (for development)
        return True, None
    
    if not frontend_id:
        return False, "Missing X-Frontend-ID header"
    
    if not api_key:
        return False, "Missing X-API-Key header"
    
    expected_key = _FRONTEND_CREDENTIALS.get(frontend_id)
    if not expected_key:
        return False, "Unknown frontend"
    
    # Timing-safe comparison
    if _hash_key(api_key) != _hash_key(expected_key):
        return False, "Invalid API key"
    
    return True, None


def _get_client_identifier(request: Request, frontend_id: str | None) -> str:
    """Get a unique identifier for the client (for rate limiting)."""
    if frontend_id:
        return f"frontend:{frontend_id}"
    
    # Fall back to IP for unauthenticated requests
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


# =============================================================================
# HTTP Basic Auth for Documentation Pages
# =============================================================================

def _extract_basic_auth(request: Request) -> tuple[str | None, str | None]:
    """
    Extract username and password from HTTP Basic Auth header.
    
    Returns:
        (username, password) or (None, None) if not present or invalid
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return None, None
    
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username or None, password or None
    except Exception:
        return None, None


# =============================================================================
# FastAPI Middleware
# =============================================================================

class APISecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces frontend authentication and rate limiting.
    
    Authentication methods:
    - API endpoints: X-Frontend-ID + X-API-Key headers
    - Documentation (/docs, /redoc, /openapi.json): HTTP Basic Auth
      (username = Frontend-ID, password = API-Key)
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        path = request.url.path
        
        # Skip static files
        if any(path.startswith(prefix) for prefix in SKIP_PREFIXES):
            return await call_next(request)
        
        # ── Documentation endpoints: HTTP Basic Auth (browser login popup) ──
        if path in DOCS_ENDPOINTS and is_security_enabled():
            basic_user, basic_pass = _extract_basic_auth(request)
            valid, error = validate_frontend_credentials(basic_user, basic_pass)
            if not valid:
                # Return 401 with Basic realm → browser shows login dialog
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Authentication required to view API documentation."},
                    headers={"WWW-Authenticate": 'Basic realm="API Documentation", charset="UTF-8"'},
                )
            # Credentials valid, allow through
            return await call_next(request)
        
        # ── Standard API endpoints: X-Frontend-ID + X-API-Key headers ──
        # Also accept HTTP Basic Auth as fallback (browser sends cached
        # credentials from docs login, making Swagger "Try it out" work)
        frontend_id = request.headers.get("X-Frontend-ID")
        api_key = request.headers.get("X-API-Key")
        
        # Fallback: if custom headers missing, try Basic Auth credentials
        if not frontend_id or not api_key:
            basic_user, basic_pass = _extract_basic_auth(request)
            if basic_user and basic_pass:
                frontend_id = frontend_id or basic_user
                api_key = api_key or basic_pass
        
        client_id = _get_client_identifier(request, frontend_id)
        
        # Check if endpoint requires authentication
        requires_auth = path not in AUTH_EXEMPT_ENDPOINTS
        
        # Validate credentials if required and security is enabled
        if requires_auth and is_security_enabled():
            valid, error = validate_frontend_credentials(frontend_id, api_key)
            if not valid:
                logger.debug(
                    "[SECURITY] Auth failed: %s %s | frontend_id=%s | %s",
                    request.method, path, frontend_id or "null", error,
                )
                # Record malformed/invalid request
                should_block, block_msg = _rate_limiter.record_malformed_request(client_id)
                if should_block:
                    return JSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={"detail": block_msg},
                    )
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": error},
                    headers={"WWW-Authenticate": "ApiKey"},
                )
        
        # Apply rate limiting
        tier = _get_endpoint_tier(path)
        rate_frontend = frontend_id or client_id
        allowed, rate_error = _rate_limiter.check_rate_limit(rate_frontend, tier)
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": rate_error},
                headers={"Retry-After": "60"},
            )
        
        # Process request
        response = await call_next(request)
        return response


# =============================================================================
# Dependency for Route-Level Access
# =============================================================================

async def get_frontend_id(request: Request) -> str | None:
    """FastAPI dependency to get the authenticated frontend ID."""
    return request.headers.get("X-Frontend-ID")


async def require_frontend_auth(request: Request) -> str:
    """
    FastAPI dependency that requires valid frontend authentication.
    
    Use this on routes that need explicit frontend validation beyond middleware.
    
    Returns:
        The validated frontend_id
        
    Raises:
        HTTPException: If authentication fails
    """
    if not is_security_enabled():
        return request.headers.get("X-Frontend-ID") or "default"
    
    frontend_id = request.headers.get("X-Frontend-ID")
    api_key = request.headers.get("X-API-Key")
    
    valid, error = validate_frontend_credentials(frontend_id, api_key)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error,
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    return frontend_id  # type: ignore


# =============================================================================
# Utility Functions
# =============================================================================

def get_rate_limit_status(frontend_id: str) -> dict[str, Any]:
    """Get rate limit status for a frontend (for debugging/monitoring)."""
    now = time.monotonic()
    status_info: dict[str, Any] = {"frontend_id": frontend_id, "tiers": {}}
    
    for tier_name in RATE_LIMIT_TIERS:
        if tier_name == "malformed":
            continue
        key = (frontend_id, tier_name)
        window = _rate_limiter._windows.get(key)
        if window:
            minute_ago = now - 60
            recent_count = sum(1 for ts in window.timestamps if ts > minute_ago)
            config = RATE_LIMIT_TIERS[tier_name]
            status_info["tiers"][tier_name] = {
                "requests_last_minute": recent_count,
                "limit_per_minute": config.requests_per_minute,
                "blocked": window.blocked_until > now,
                "blocked_remaining": max(0, int(window.blocked_until - now)) if window.blocked_until > now else 0,
            }
    
    return status_info


# =============================================================================
# Middleware Setup (Phase 15)
# =============================================================================

def setup_middlewares(app) -> None:
    """
    Configure all middlewares for the FastAPI application.
    
    Adds:
    - CORS middleware for cross-origin requests
    - API security middleware for auth + rate limiting
    - Request logging middleware
    
    Args:
        app: FastAPI application instance
    """
    from fastapi.middleware.cors import CORSMiddleware
    
    # CORS - Allow frontend origins
    cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # API Security (auth + rate limiting)
    app.add_middleware(APISecurityMiddleware)
    
    logger.info("Middlewares configured: CORS (origins=%s), APISecurityMiddleware", cors_origins)


def setup_request_logging(app) -> None:
    """
    Add request logging middleware for debugging.
    
    Logs incoming requests with method, path, and response time.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    
    class RequestLoggingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            start_time = time.time()
            response = await call_next(request)
            process_time = time.time() - start_time
            
            # Only log non-health endpoints
            if request.url.path not in ("/", "/healthz"):
                logger.debug(
                    "%s %s - %d (%.3fs)",
                    request.method,
                    request.url.path,
                    response.status_code,
                    process_time,
                )
            
            return response
    
    app.add_middleware(RequestLoggingMiddleware)
    logger.info("Request logging middleware configured")

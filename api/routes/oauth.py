"""
Google OAuth Routes Module (V2 API).

Handles Google OAuth flows:
- GET/POST /auth/google/start: Start Google OAuth flow
- GET /auth/google/callback: Handle OAuth callback
- GET /auth/status: Check OAuth status
- POST /auth/revoke: Revoke tokens

Note: Microsoft OAuth routes are in oauth_microsoft.py (already implemented).
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from api.models import OAuthStatusResponse, OAuthRevokeRequest
from db.storage import UserTokenStorage
from utils.google_oauth import (
    GoogleOAuthSettings,
    OAuthStateManager,
    TokenEncryptor,
    get_google_oauth_settings,
    credentials_to_dict,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# Lazy initialization of services
_oauth_settings: GoogleOAuthSettings | None = None
_state_manager: OAuthStateManager | None = None
_encryptor: TokenEncryptor | None = None
_token_storage: UserTokenStorage | None = None


def _get_oauth_settings() -> GoogleOAuthSettings:
    global _oauth_settings
    if _oauth_settings is None:
        _oauth_settings = get_google_oauth_settings()
    return _oauth_settings


def _get_state_manager() -> OAuthStateManager:
    global _state_manager
    if _state_manager is None:
        settings = _get_oauth_settings()
        _state_manager = OAuthStateManager(settings)
    return _state_manager


def _get_encryptor() -> TokenEncryptor:
    global _encryptor
    if _encryptor is None:
        settings = _get_oauth_settings()
        _encryptor = TokenEncryptor(settings.encryption_key)
    return _encryptor


def _get_token_storage() -> UserTokenStorage:
    global _token_storage
    if _token_storage is None:
        _token_storage = UserTokenStorage()
    return _token_storage


def _coerce_db_blob(value: Any) -> bytes | None:
    """Convert database blob to bytes."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    return None


async def _resolve_user_record(user_id: str) -> tuple[str, dict[str, Any]]:
    """Find user by ID and return (canonical_id, record)."""
    storage = _get_token_storage()
    clean = user_id.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="user_id is required")

    record = None
    if clean.isdigit():
        record = await storage.get_user_by_primary_id(int(clean))
    else:
        # Validate UUID format before database query
        import uuid
        try:
            uuid.UUID(clean)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user_id format - must be a valid UUID")
        record = await storage.get_user_by_user_id(clean)

    if not record:
        raise HTTPException(status_code=404, detail=f"User {clean} not found")

    canonical = str(record.get("id") or "").strip()  # lad_dev.users uses 'id' as primary key
    if not canonical:
        raise HTTPException(status_code=500, detail="User record missing identifier")

    return canonical, record


def _resolve_frontend_redirect(frontend_id: str | None, *, success: bool, message: str | None = None) -> str:
    """Build redirect URL for frontend after OAuth."""
    settings = _get_oauth_settings()
    redirect_map = settings.frontend_redirect_map

    if frontend_id and frontend_id in redirect_map:
        base = redirect_map[frontend_id]
    else:
        base = settings.success_fallback if success else settings.error_fallback

    from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["status"] = "success" if success else "error"
    query["provider"] = "google"
    if message:
        query["message"] = message
    if frontend_id:
        query["frontend_id"] = frontend_id
    encoded = urlencode(query)
    return urlunparse(parsed._replace(query=encoded))


def _build_google_flow(state: str):
    """Build Google OAuth flow object."""
    from google_auth_oauthlib.flow import Flow
    settings = _get_oauth_settings()
    
    client_config = {
        "web": {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.redirect_uri],
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=settings.scopes,
        state=state,
    )
    flow.redirect_uri = settings.redirect_uri
    return flow


def _merge_refresh_token(new_payload: dict, previous_payload: dict | None) -> dict:
    """Preserve refresh_token if not in new payload."""
    if not previous_payload:
        return new_payload
    if "refresh_token" not in new_payload and "refresh_token" in previous_payload:
        new_payload["refresh_token"] = previous_payload["refresh_token"]
    return new_payload


# =============================================================================
# GET/POST /auth/google/start - Start Google OAuth flow
# =============================================================================

@router.get("/google/start")
@router.post("/google/start")
async def start_google_auth(user_id: str, frontend_id: str) -> dict[str, str]:
    """
    Start Google OAuth flow.
    
    Query Params:
        user_id: User identifier (string or numeric)
        frontend_id: Frontend key for post-auth redirect
    
    Returns:
        {"url": "https://accounts.google.com/..."}
    """
    clean_frontend = frontend_id.strip()
    if not clean_frontend:
        raise HTTPException(status_code=400, detail="frontend_id is required")

    canonical_id, user_record = await _resolve_user_record(user_id)
    state_manager = _get_state_manager()
    state_token = state_manager.issue(user_id=canonical_id, frontend_id=clean_frontend)
    
    flow = _build_google_flow(state_token)
    blob = _coerce_db_blob(user_record.get("google_oauth_tokens"))
    prompt = "consent" if not blob else "select_account"
    
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt=prompt,
    )
    return {"url": authorization_url}


# =============================================================================
# GET /auth/google/callback - Handle OAuth callback
# =============================================================================

@router.get("/google/callback")
async def google_auth_callback(code: str | None = None, state: str | None = None) -> RedirectResponse:
    """
    Handle Google OAuth callback.
    
    Called by Google after user authorization.
    """
    frontend_id: str | None = None

    if not state:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="missing_state")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    try:
        state_manager = _get_state_manager()
        payload = state_manager.verify(state)
    except ValueError as exc:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message=str(exc))
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    frontend_id = payload.get("frontend_id")
    user_id = payload.get("user_id")
    
    if not user_id:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="missing_user_id")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    try:
        canonical_id, user_record = await _resolve_user_record(user_id)
    except HTTPException:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="user_not_found")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if not code:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="missing_code")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    flow = _build_google_flow(state)

    def _fetch_token(auth_code: str) -> None:
        flow.fetch_token(code=auth_code)

    try:
        await asyncio.to_thread(_fetch_token, code)
    except Exception as exc:
        logger.exception("Google OAuth token exchange failed", exc_info=exc)
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="token_exchange_failed")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Fetch user email from Google userinfo endpoint (using access token from credentials)
    connected_gmail = None
    access_token = flow.credentials.token
    if access_token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                userinfo_resp = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_resp.status_code == 200:
                    userinfo = userinfo_resp.json()
                    email = userinfo.get("email")
                    if email and isinstance(email, str):
                        connected_gmail = email.strip().lower()  # Normalize email
                        logger.info("Connected Gmail for user_id=%s: %s", canonical_id, connected_gmail)
                else:
                    logger.warning(
                        "Failed to fetch Google userinfo: status=%s body=%s",
                        userinfo_resp.status_code,
                        userinfo_resp.text[:200],
                    )
        except Exception as email_exc:
            logger.warning("Error fetching Google user email: %s", email_exc)

    new_payload = credentials_to_dict(flow.credentials)
    encryptor = _get_encryptor()
    
    previous_blob = _coerce_db_blob(user_record.get("google_oauth_tokens"))
    previous_payload = None
    if previous_blob:
        try:
            previous_payload = encryptor.decrypt_json(previous_blob)
        except ValueError:
            logger.warning(
                "Existing Google tokens for user_id=%s could not be decrypted; overwriting",
                user_id,
            )
    
    merged_payload = _merge_refresh_token(new_payload, previous_payload)
    encrypted = encryptor.encrypt_json(merged_payload)

    storage = _get_token_storage()
    await storage.store_token_blob(canonical_id, encrypted, connected_gmail=connected_gmail)
    logger.info("Stored Google OAuth tokens for user_id=%s (gmail=%s)", canonical_id, connected_gmail)
    
    redirect = _resolve_frontend_redirect(frontend_id, success=True, message="linked")
    return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# =============================================================================
# GET /auth/status - Check OAuth status
# =============================================================================

@router.get("/status", response_model=OAuthStatusResponse)
async def google_auth_status(user_id: str) -> OAuthStatusResponse:
    """
    Check Google OAuth connection status for a user.
    
    Query Params:
        user_id: User identifier
    
    Returns:
        Connection status with token info
    """
    canonical_id, user_record = await _resolve_user_record(user_id)
    
    # Tokens are stored in user_identities table, not users table
    storage = _get_token_storage()
    blob = await storage.get_google_token_blob(canonical_id)
    if not blob:
        return OAuthStatusResponse(connected=False)

    encryptor = _get_encryptor()
    try:
        payload = encryptor.decrypt_json(blob)
    except ValueError:
        logger.warning("Stored Google tokens for user_id=%s are invalid; clearing", canonical_id)
        await storage.remove_tokens(canonical_id)
        return OAuthStatusResponse(connected=False)

    if not payload:
        return OAuthStatusResponse(connected=False)

    expiry_raw = payload.get("expiry")
    expires_at = None
    if expiry_raw:
        try:
            expires_at = datetime.fromisoformat(str(expiry_raw))
        except ValueError:
            expires_at = None

    connected_gmail = await storage.get_connected_gmail(canonical_id)
    
    return OAuthStatusResponse(
        connected=True,
        expires_at=expires_at,
        scopes=list(payload.get("scopes") or []),
        has_refresh_token=bool(payload.get("refresh_token")),
        connected_gmail=connected_gmail if isinstance(connected_gmail, str) else None,
    )


# =============================================================================
# POST /auth/revoke - Revoke tokens
# =============================================================================

@router.post("/revoke", response_model=dict)
async def revoke_google_tokens(request: OAuthRevokeRequest) -> dict[str, str]:
    """
    Revoke Google OAuth tokens.
    
    Body:
        {"user_id": "..."}
    
    Returns:
        {"status": "ok", "message": "revoked"}
    """
    clean_user_id = request.user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    storage = _get_token_storage()
    await storage.remove_tokens(clean_user_id)
    
    return {"status": "ok", "message": "revoked"}


__all__ = ["router"]

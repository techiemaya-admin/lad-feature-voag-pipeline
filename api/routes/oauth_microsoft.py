"""Microsoft OAuth and Bookings API routes."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pathlib import Path
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from pydantic import BaseModel

from utils.google_oauth import (
    OAuthStateManager,
    TokenEncryptor,
    get_google_oauth_settings,
)
from utils.microsoft_oauth import (
    MicrosoftAuthService,
    get_microsoft_oauth_settings,
    token_response_to_storage_format,
)
from db.storage.tokens import UserTokenStorage
from db.db_config import get_db_config
from db.connection_pool import get_db_connection

logger = logging.getLogger(__name__)

# Initialize shared components
microsoft_router = APIRouter(prefix="/auth/microsoft")

# Lazy initialization of services
_ms_service: MicrosoftAuthService | None = None
_state_manager: OAuthStateManager | None = None
_encryptor: TokenEncryptor | None = None
_token_storage: UserTokenStorage | None = None


def _get_ms_service() -> MicrosoftAuthService:
    """Get or create Microsoft auth service."""
    global _ms_service
    if _ms_service is None:
        _ms_service = MicrosoftAuthService()
    return _ms_service


def _get_state_manager() -> OAuthStateManager:
    """Get or create OAuth state manager (shared with Google)."""
    global _state_manager
    if _state_manager is None:
        settings = get_google_oauth_settings()
        _state_manager = OAuthStateManager(settings)
    return _state_manager


def _get_encryptor() -> TokenEncryptor:
    """Get or create token encryptor (shared with Google)."""
    global _encryptor
    if _encryptor is None:
        settings = get_google_oauth_settings()
        _encryptor = TokenEncryptor(settings.encryption_key)
    return _encryptor


def _get_token_storage() -> UserTokenStorage:
    """Get or create token storage."""
    global _token_storage
    if _token_storage is None:
        _token_storage = UserTokenStorage()
    return _token_storage


def _get_frontend_redirect_map() -> dict[str, str]:
    """Get frontend redirect map from Google OAuth settings."""
    settings = get_google_oauth_settings()
    return dict(settings.frontend_redirect_map)


def _resolve_frontend_redirect(frontend_id: str | None, *, success: bool, message: str | None = None) -> str:
    """Build redirect URL for frontend after OAuth."""
    settings = get_google_oauth_settings()
    redirect_map = settings.frontend_redirect_map

    if frontend_id and frontend_id in redirect_map:
        base = redirect_map[frontend_id]
    else:
        base = settings.success_fallback if success else settings.error_fallback

    # Append query params
    from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["status"] = "success" if success else "error"
    query["provider"] = "microsoft"
    if message:
        query["message"] = message
    if frontend_id:
        query["frontend_id"] = frontend_id
    encoded = urlencode(query)
    return urlunparse(parsed._replace(query=encoded))


def _get_frontend_base_url(frontend_id: str | None) -> str:
    """Get the base frontend redirect URL (without query params)."""
    settings = get_google_oauth_settings()
    redirect_map = settings.frontend_redirect_map
    if frontend_id and frontend_id in redirect_map:
        return redirect_map[frontend_id]
    return settings.success_fallback


def _render_booking_wizard(user_id: str, frontend_id: str, api_key: str, base_url: str) -> str:
    """Render the booking wizard HTML template with injected configuration."""
    template_path = Path(__file__).parent.parent / "templates" / "booking_wizard.html"
    
    if not template_path.exists():
        logger.error("Booking wizard template not found at %s", template_path)
        raise HTTPException(status_code=500, detail="Wizard template not found")
    
    template = template_path.read_text(encoding="utf-8")
    frontend_redirect = _get_frontend_base_url(frontend_id)
    
    # Replace template placeholders
    html = template.replace("{{USER_ID}}", user_id)
    html = html.replace("{{FRONTEND_ID}}", frontend_id)
    html = html.replace("{{API_KEY}}", api_key)
    html = html.replace("{{BASE_URL}}", base_url)
    html = html.replace("{{FRONTEND_REDIRECT}}", frontend_redirect)
    
    return html


async def _resolve_user_record(user_id: str) -> tuple[str, dict[str, Any]]:
    """Find user by ID and return (canonical_id, record)."""
    storage = _get_token_storage()
    clean = user_id.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="user_id is required")

    # Validate UUID format before database query
    import uuid
    try:
        uuid.UUID(clean)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format - must be a valid UUID")

    # All IDs are now UUIDs in lad_dev.users
    record = await storage.get_user_by_user_id(clean)

    if not record:
        raise HTTPException(status_code=404, detail=f"User {clean} not found")

    # lad_dev.users uses 'id' as primary key (UUID), not 'user_id'
    canonical = str(record.get("id") or "").strip()
    if not canonical:
        raise HTTPException(status_code=500, detail="User record missing identifier")

    return canonical, record


async def _fetch_microsoft_user_info(access_token: str) -> dict[str, Any]:
    """Fetch user profile from Microsoft Graph API."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Failed to fetch Microsoft user info: %s", resp.text[:200])
        return {}


async def _fetch_booking_businesses(access_token: str) -> list[dict[str, Any]]:
    """Fetch all booking businesses for the user."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/solutions/bookingBusinesses",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("value", [])
        logger.warning("Failed to fetch booking businesses: %s", resp.text[:200])
        return []


async def _fetch_booking_services(access_token: str, business_id: str) -> list[dict[str, Any]]:
    """Fetch all services for a booking business."""
    from urllib.parse import quote
    encoded_business_id = quote(business_id, safe='')
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{encoded_business_id}/services",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch booking services: %s", resp.text[:200])
            return []
        return resp.json().get("value", [])


async def _fetch_booking_staff(access_token: str, business_id: str) -> list[dict[str, Any]]:
    """Fetch all staff members for a booking business."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/staffMembers",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch booking staff: %s", resp.text[:200])
            return []
        return resp.json().get("value", [])


async def _auto_configure_booking(
    user_id: str,
    access_token: str,
) -> dict[str, Any]:
    """
    Auto-configure booking business, service, and staff member.
    
    If only 1 business exists -> auto-save it
    If business is set and only 1 service exists -> auto-save it
    If service is set and only 1 staff exists -> auto-save it
    
    Returns dict with what was auto-configured.
    """
    result = {"business_auto_saved": False, "service_auto_saved": False, "staff_auto_saved": False}
    storage = _get_token_storage()
    
    # Fetch businesses
    businesses = await _fetch_booking_businesses(access_token)
    
    if len(businesses) == 1:
        # Only one business - auto-save it
        biz = businesses[0]
        business_id = biz.get("id", "")
        business_name = biz.get("displayName", "")
        
        # Check for services
        services = await _fetch_booking_services(access_token, business_id)
        service_id = None
        staff_member_id = None
        
        if len(services) == 1:
            service_id = services[0].get("id")
            result["service_auto_saved"] = True
            logger.info("Auto-configured single service for user %s: %s", user_id, service_id)
            
            # If service is set, check for staff
            staff = await _fetch_booking_staff(access_token, business_id)
            if len(staff) == 1:
                staff_member_id = staff[0].get("id")
                result["staff_auto_saved"] = True
                logger.info("Auto-configured single staff for user %s: %s", user_id, staff_member_id)
        
        await storage.store_booking_config(user_id, business_id, business_name, service_id, staff_member_id)
        result["business_auto_saved"] = True
        result["business_id"] = business_id
        result["business_name"] = business_name
        result["service_id"] = service_id
        result["staff_member_id"] = staff_member_id
        logger.info("Auto-configured single business for user %s: %s (%s)", user_id, business_name, business_id)
    
    return result


async def _auto_configure_service_if_single(
    user_id: str,
    access_token: str,
    business_id: str,
    current_service_id: str | None,
) -> str | None:
    """
    Check if business has only 1 service and auto-save it.
    Returns the service_id if auto-saved, else current_service_id.
    """
    if current_service_id:
        # Already has a service configured
        return current_service_id
    
    services = await _fetch_booking_services(access_token, business_id)
    
    if len(services) == 1:
        service_id = services[0].get("id")
        logger.info("Auto-configured single service for business %s: %s", business_id, service_id)
        return service_id
    
    return None


# =============================================================================
# Pydantic Models
# =============================================================================


class MicrosoftOAuthStatusResponse(BaseModel):
    """Response for Microsoft OAuth status check."""
    connected: bool
    connected_account: str | None = None
    bookings_accessible: bool = False  # True if Bookings API is accessible
    selected_business_id: str | None = None
    selected_business_name: str | None = None
    default_service_id: str | None = None
    default_staff_member_id: str | None = None


class MicrosoftRevokeRequest(BaseModel):
    """Request to revoke Microsoft tokens."""
    user_id: str


class SaveConfigRequest(BaseModel):
    """Request to save booking configuration."""
    user_id: str
    business_id: str
    business_name: str | None = None
    service_id: str | None = None
    staff_member_id: str | None = None


class SaveTenantToolConfigRequest(BaseModel):
    """Request to save MS Bookings defaults to tenant_features.config."""
    tenant_id: str
    business_id: str | None = None
    service_id: str | None = None
    staff_id: str | None = None


class BookingBusiness(BaseModel):
    """A Microsoft Booking business."""
    id: str
    display_name: str
    email: str | None = None
    phone: str | None = None


class BookingService(BaseModel):
    """A service within a booking business."""
    id: str
    display_name: str
    default_duration: str | None = None
    default_price: float | None = None


class BookingStaffMember(BaseModel):
    """A staff member within a booking business."""
    id: str
    display_name: str
    email: str | None = None
    role: str | None = None


# =============================================================================
# API Endpoints
# =============================================================================


@microsoft_router.get("/start")
@microsoft_router.post("/start")
async def start_microsoft_auth(user_id: str, frontend_id: str) -> dict[str, str]:
    """
    Start Microsoft OAuth flow.

    Query Params:
        user_id: User identifier (string or numeric)
        frontend_id: Frontend key for post-auth redirect

    Returns:
        {"url": "https://login.microsoftonline.com/..."}
    """
    clean_frontend = frontend_id.strip()
    if not clean_frontend:
        raise HTTPException(status_code=400, detail="frontend_id is required")

    canonical_id, _ = await _resolve_user_record(user_id)

    state_manager = _get_state_manager()
    state_token = state_manager.issue(user_id=canonical_id, frontend_id=clean_frontend)

    ms_service = _get_ms_service()
    auth_url = ms_service.get_auth_url(state=state_token)

    return {"url": auth_url}


@microsoft_router.get("/callback")
async def microsoft_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """
    Handle Microsoft OAuth callback.

    Called by Microsoft after user authorization.
    """
    frontend_id: str | None = None

    # Handle error from Microsoft
    if error:
        logger.warning("Microsoft OAuth error: %s - %s", error, error_description)
        redirect = _resolve_frontend_redirect(None, success=False, message=error)
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Verify state token
    if not state:
        redirect = _resolve_frontend_redirect(None, success=False, message="missing_state")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    try:
        state_manager = _get_state_manager()
        payload = state_manager.verify(state)
    except ValueError as exc:
        logger.warning("Invalid Microsoft OAuth state: %s", exc)
        redirect = _resolve_frontend_redirect(None, success=False, message="invalid_state")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    frontend_id = payload.get("frontend_id")
    user_id = payload.get("user_id")

    if not user_id:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="missing_user_id")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Resolve user
    try:
        canonical_id, _ = await _resolve_user_record(user_id)
    except HTTPException:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="user_not_found")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if not code:
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="missing_code")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Exchange code for tokens
    try:
        ms_service = _get_ms_service()
        token_result = ms_service.exchange_code_for_token(code)
    except ValueError as exc:
        logger.error("Microsoft token exchange failed: %s", exc)
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="token_exchange_failed")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Fetch user info
    access_token = token_result.get("access_token")
    connected_account = None
    if access_token:
        user_info = await _fetch_microsoft_user_info(access_token)
        connected_account = user_info.get("mail") or user_info.get("userPrincipalName")
        if connected_account:
            logger.info("Connected Microsoft account for user_id=%s: %s", canonical_id, connected_account)

    # Encrypt and store tokens
    token_payload = token_response_to_storage_format(token_result)
    encryptor = _get_encryptor()
    encrypted = encryptor.encrypt_json(token_payload)

    storage = _get_token_storage()
    await storage.store_microsoft_token_blob(canonical_id, encrypted, connected_account=connected_account)
    logger.info("Stored Microsoft OAuth tokens for user_id=%s", canonical_id)

    # Auto-configure booking if only one business/service exists
    auto_configured_fully = False
    if access_token:
        try:
            auto_result = await _auto_configure_booking(canonical_id, access_token)
            if auto_result.get("business_auto_saved"):
                logger.info(
                    "Auto-configured booking for user_id=%s: business=%s, service=%s",
                    canonical_id,
                    auto_result.get("business_name"),
                    auto_result.get("service_id"),
                )
                # Check if fully configured (business + service + staff all auto-saved)
                auto_configured_fully = (
                    auto_result.get("business_auto_saved") and
                    auto_result.get("service_auto_saved") and
                    auto_result.get("staff_auto_saved")
                )
        except Exception as exc:
            # Don't fail OAuth if auto-config fails
            logger.warning("Auto-configure booking failed for user_id=%s: %s", canonical_id, exc)

    # If not fully auto-configured, redirect to booking wizard for user to complete setup
    if not auto_configured_fully:
        from urllib.parse import urlencode
        configure_url = f"/auth/microsoft/configure?{urlencode({'user_id': canonical_id, 'frontend_id': frontend_id or ''})}"
        logger.info("Redirecting user_id=%s to booking wizard", canonical_id)
        return RedirectResponse(configure_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    redirect = _resolve_frontend_redirect(frontend_id, success=True, message="linked")
    return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@microsoft_router.get("/configure", response_class=HTMLResponse)
async def configure_booking_wizard(
    user_id: str,
    frontend_id: str = "",
) -> HTMLResponse:
    """
    Serve the booking configuration wizard HTML page.

    This page allows users to select their booking business, service, and staff
    after completing Microsoft OAuth. The wizard calls the existing API endpoints
    to fetch options and save the configuration.

    Query Params:
        user_id: User identifier
        frontend_id: Frontend key for post-configuration redirect

    Returns:
        HTML page with booking wizard
    """
    import os
    import json
    
    # Validate user exists and has Microsoft connection
    canonical_id, _ = await _resolve_user_record(user_id)
    
    storage = _get_token_storage()
    blob = await storage.get_microsoft_token_blob(canonical_id)
    if not blob:
        # No Microsoft connection - redirect to error
        redirect = _resolve_frontend_redirect(frontend_id, success=False, message="not_connected")
        return RedirectResponse(redirect, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    
    # Get API key for this frontend_id from environment
    api_key = ""
    try:
        api_keys_raw = os.getenv("FRONTEND_API_KEYS", "{}")
        api_keys = json.loads(api_keys_raw)
        api_key = api_keys.get(frontend_id, "")
    except Exception:
        logger.warning("Failed to load FRONTEND_API_KEYS for wizard")
    
    # Use relative base URL since wizard is served from the same domain
    base_url = ""
    
    html_content = _render_booking_wizard(
        user_id=canonical_id,
        frontend_id=frontend_id or "",
        api_key=api_key,
        base_url=base_url,
    )
    
    return HTMLResponse(content=html_content, status_code=200)


@microsoft_router.get("/logo")
async def serve_logo() -> Response:
    """Serve the TechieMaya logo for the booking wizard."""
    logo_path = Path(__file__).parent.parent / "templates" / "logo.png"
    if not logo_path.exists():
        raise HTTPException(status_code=404, detail="Logo not found")
    
    content = logo_path.read_bytes()
    return Response(content=content, media_type="image/png")


@microsoft_router.get("/status", response_model=MicrosoftOAuthStatusResponse)
async def microsoft_auth_status(user_id: str) -> MicrosoftOAuthStatusResponse:
    """
    Check Microsoft OAuth connection status for a user.

    Query Params:
        user_id: User identifier

    Returns:
        Connection status with account info and booking configuration
    """
    canonical_id, _ = await _resolve_user_record(user_id)
    storage = _get_token_storage()

    # Check for Microsoft identity in user_identities table
    identity = await storage.get_identity(canonical_id, "microsoft")
    if not identity:
        return MicrosoftOAuthStatusResponse(connected=False)

    # Extract booking config from provider_data
    provider_data = identity.get("provider_data") or {}
    booking_config = provider_data.get("booking_config") or {}
    
    # Test if Bookings API is accessible
    bookings_accessible = False
    try:
        # Load tokens to test Bookings API
        blob = await storage.get_microsoft_token_blob(canonical_id)
        if blob:
            encryptor = _get_encryptor()
            token_payload = encryptor.decrypt_json(blob)
            access_token = token_payload.get("access_token")
            if access_token:
                # Try fetching businesses - if it works, Bookings is accessible
                try:
                    businesses = await _fetch_booking_businesses(access_token)
                    bookings_accessible = businesses is not None  # Even empty list is OK
                except Exception:
                    bookings_accessible = False
    except Exception as e:
        logger.debug(f"Error checking Bookings access: {e}")
        bookings_accessible = False

    return MicrosoftOAuthStatusResponse(
        connected=True,
        connected_account=identity.get("provider_user_id") or provider_data.get("connected_account"),
        bookings_accessible=bookings_accessible,
        selected_business_id=booking_config.get("business_id"),
        selected_business_name=booking_config.get("business_name"),
        default_service_id=booking_config.get("service_id"),
        default_staff_member_id=booking_config.get("staff_member_id"),
    )


@microsoft_router.post("/revoke")
async def revoke_microsoft_tokens(request: MicrosoftRevokeRequest) -> dict[str, str]:
    """
    Revoke Microsoft OAuth tokens and clear from database.

    Body:
        {"user_id": "..."}

    Returns:
        {"status": "ok", "message": "revoked"}
    """
    clean_user_id = request.user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    canonical_id, _ = await _resolve_user_record(clean_user_id)

    storage = _get_token_storage()
    await storage.remove_microsoft_tokens(canonical_id)

    return {"status": "ok", "message": "revoked"}


@microsoft_router.get("/list-businesses", response_model=list[BookingBusiness])
async def list_booking_businesses(user_id: str) -> list[BookingBusiness]:
    """
    List Microsoft Booking businesses the user manages.

    Called by frontend after OAuth to let user select which business to use.

    Query Params:
        user_id: User identifier

    Returns:
        List of booking businesses
    """
    canonical_id, _ = await _resolve_user_record(user_id)
    storage = _get_token_storage()

    # Load tokens from user_identities table
    blob = await storage.get_microsoft_token_blob(canonical_id)
    if not blob:
        raise HTTPException(status_code=404, detail="User has not connected Microsoft account")

    encryptor = _get_encryptor()
    try:
        token_payload = encryptor.decrypt_json(blob)
    except ValueError as exc:
        logger.error("Failed to decrypt Microsoft tokens: %s", exc)
        raise HTTPException(status_code=500, detail="Invalid stored tokens") from exc

    access_token = token_payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="No access token available")

    # Call Microsoft Graph API
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/solutions/bookingBusinesses",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if resp.status_code == 401:
            # Token expired - try refresh
            refresh_token = token_payload.get("refresh_token")
            if refresh_token:
                try:
                    ms_service = _get_ms_service()
                    new_tokens = ms_service.refresh_token(refresh_token)
                    access_token = new_tokens.get("access_token")

                    # Store refreshed tokens
                    new_payload = token_response_to_storage_format(new_tokens)
                    encrypted = encryptor.encrypt_json(new_payload)
                    storage = _get_token_storage()
                    await storage.store_microsoft_token_blob(canonical_id, encrypted)

                    # Retry the request
                    resp = await client.get(
                        "https://graph.microsoft.com/v1.0/solutions/bookingBusinesses",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                except ValueError as exc:
                    logger.error("Token refresh failed: %s", exc)
                    raise HTTPException(
                        status_code=401,
                        detail="Microsoft tokens expired. Please reconnect your account."
                    ) from exc
            else:
                raise HTTPException(
                    status_code=401,
                    detail="Microsoft tokens expired. Please reconnect your account."
                )

        if resp.status_code != 200:
            logger.error("Failed to fetch booking businesses: %s", resp.text[:500])
            raise HTTPException(status_code=502, detail=f"Microsoft API error: {resp.status_code}")

        data = resp.json()
        businesses = data.get("value", [])

    return [
        BookingBusiness(
            id=b.get("id", ""),
            display_name=b.get("displayName", "Unknown"),
            email=b.get("email"),
            phone=b.get("phone"),
        )
        for b in businesses
    ]


@microsoft_router.get("/list-services", response_model=list[BookingService])
async def list_booking_services(user_id: str, business_id: str) -> list[BookingService]:
    """
    List services for a specific booking business.

    Query Params:
        user_id: User identifier
        business_id: Booking business ID

    Returns:
        List of services available for the business
    """
    canonical_id, _ = await _resolve_user_record(user_id)
    storage = _get_token_storage()

    blob = await storage.get_microsoft_token_blob(canonical_id)
    if not blob:
        raise HTTPException(status_code=404, detail="User has not connected Microsoft account")

    encryptor = _get_encryptor()
    try:
        token_payload = encryptor.decrypt_json(blob)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored tokens") from exc

    access_token = token_payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="No access token available")

    # URL-encode business_id (contains @ symbol)
    from urllib.parse import quote
    encoded_business_id = quote(business_id, safe='')
    
    # Call Microsoft Graph API
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{encoded_business_id}/services",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if resp.status_code != 200:
            logger.error("Failed to fetch booking services: %s", resp.text[:500])
            raise HTTPException(status_code=502, detail=f"Microsoft API error: {resp.status_code}")

        data = resp.json()
        services = data.get("value", [])

    return [
        BookingService(
            id=s.get("id", ""),
            display_name=s.get("displayName", "Unknown"),
            default_duration=s.get("defaultDuration"),
            default_price=s.get("defaultPrice", {}).get("amount") if isinstance(s.get("defaultPrice"), dict) else None,
        )
        for s in services
    ]


@microsoft_router.get("/staff", response_model=list[BookingStaffMember])
async def list_booking_staff(user_id: str, business_id: str) -> list[BookingStaffMember]:
    """
    List staff members for a specific booking business.

    Query Params:
        user_id: User identifier
        business_id: Booking business ID

    Returns:
        List of staff members available for the business
    """
    canonical_id, _ = await _resolve_user_record(user_id)
    storage = _get_token_storage()

    blob = await storage.get_microsoft_token_blob(canonical_id)
    if not blob:
        raise HTTPException(status_code=404, detail="User has not connected Microsoft account")

    encryptor = _get_encryptor()
    try:
        token_payload = encryptor.decrypt_json(blob)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored tokens") from exc

    access_token = token_payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="No access token available")

    # Call Microsoft Graph API
    staff = await _fetch_booking_staff(access_token, business_id)

    return [
        BookingStaffMember(
            id=s.get("id", ""),
            display_name=s.get("displayName", "Unknown"),
            email=s.get("emailAddress"),
            role=s.get("role"),
        )
        for s in staff
    ]


@microsoft_router.post("/save-config")
async def save_booking_config(request: SaveConfigRequest) -> dict[str, str]:
    """
    Save the selected booking business, service, and staff configuration.

    This is called after the user selects which business/calendar to use.
    If business_name is not provided, it will be auto-fetched from Microsoft.
    If service_id is not provided and only 1 service exists, it's auto-selected.
    If staff_member_id is not provided and only 1 staff exists, it's auto-selected.

    Body:
        {
            "user_id": "...",
            "business_id": "...",
            "business_name": "...",      (optional - auto-fetched if not provided)
            "service_id": "...",         (optional - auto-selected if single)
            "staff_member_id": "..."     (optional - auto-selected if single)
        }

    Returns:
        {"status": "ok", "message": "Configuration saved"}
    """
    clean_user_id = request.user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    business_id = request.business_id.strip()
    if not business_id:
        raise HTTPException(status_code=400, detail="business_id is required")

    canonical_id, _ = await _resolve_user_record(clean_user_id)
    storage = _get_token_storage()

    # Auto-fetch business name if not provided
    business_name = request.business_name.strip() if request.business_name else None
    
    # Get access token for auto-detection
    access_token = None
    blob = await storage.get_microsoft_token_blob(canonical_id)
    if blob:
        try:
            encryptor = _get_encryptor()
            token_payload = encryptor.decrypt_json(blob)
            access_token = token_payload.get("access_token")
        except Exception as exc:
            logger.warning("Failed to decrypt tokens: %s", exc)
    
    if not business_name and access_token:
        # Try to look up the business name from Microsoft Graph API
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    business_name = data.get("displayName")
                    logger.info("Auto-fetched business name: %s", business_name)
        except Exception as exc:
            logger.warning("Failed to auto-fetch business name: %s", exc)

    # Auto-configure service if only 1 service exists and none provided
    service_id = request.service_id.strip() if request.service_id else None
    staff_member_id = request.staff_member_id.strip() if request.staff_member_id else None
    
    if access_token:
        if not service_id:
            # Try to auto-detect single service
            try:
                service_id = await _auto_configure_service_if_single(
                    canonical_id, access_token, business_id, None
                )
                if service_id:
                    logger.info("Auto-detected single service: %s", service_id)
            except Exception as exc:
                logger.warning("Failed to auto-detect service: %s", exc)
        
        # Auto-configure staff if service is set and only 1 staff exists
        if service_id and not staff_member_id:
            try:
                staff = await _fetch_booking_staff(access_token, business_id)
                if len(staff) == 1:
                    staff_member_id = staff[0].get("id")
                    logger.info("Auto-detected single staff: %s", staff_member_id)
            except Exception as exc:
                logger.warning("Failed to auto-detect staff: %s", exc)

    storage = _get_token_storage()
    await storage.store_booking_config(
        canonical_id,
        business_id=business_id,
        business_name=business_name,
        service_id=service_id,
        staff_member_id=staff_member_id,
    )

    logger.info(
        "Saved booking config for user_id=%s: business=%s (%s), service=%s, staff=%s",
        canonical_id,
        business_id,
        business_name,
        service_id,
        staff_member_id,
    )

    return {"status": "ok", "message": "Configuration saved"}


@microsoft_router.post("/bookings/tenant-defaults")
async def save_tenant_booking_defaults(request: SaveTenantToolConfigRequest):
    """
    Save Microsoft Bookings defaults to tenant_features.config.
    
    This stores the business_id, service_id, staff_id in the config JSONB column
    for the 'voice-agent-tool-microsoft-bookings-auto' feature key.
    
    These defaults are used by the voice agent when auto-booking appointments.
    """
    import json
    
    config_data = {
        "business_id": request.business_id,
        "service_id": request.service_id,
        "staff_id": request.staff_id,
    }
    
    try:
        db_config = get_db_config()
        with get_db_connection(db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lad_dev.tenant_features (tenant_id, feature_key, enabled, config)
                    VALUES (%s, 'voice-agent-tool-microsoft-bookings-auto', true, %s::jsonb)
                    ON CONFLICT (tenant_id, feature_key)
                    DO UPDATE SET config = %s::jsonb, enabled = true
                """, (request.tenant_id, json.dumps(config_data), json.dumps(config_data)))
            conn.commit()
        
        logger.info(
            "Saved tenant booking defaults: tenant=%s, business=%s, service=%s, staff=%s",
            request.tenant_id, request.business_id, request.service_id, request.staff_id
        )
        
        return {"status": "ok", "message": "Tenant booking defaults saved"}
        
    except Exception as exc:
        logger.error("Failed to save tenant booking defaults: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

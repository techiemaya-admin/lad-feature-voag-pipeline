"""
Recording Routes Module (V2 API).

Handles recording signed URL generation with caching:
- POST /recordings/signed-url: Generate signed URL from gs:// URL
- GET /recordings/calls/{resource}/signed-url: Get signed URL for call recording

Caching System:
- Signed URLs are cached for 4 days (96 hours)
- Cache key is based on gs:// URL or call_log_id
- If same URL requested again, cached version is returned until expiry
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from db.storage import CallStorage
from storage.gcs import GCSStorageManager
from utils.signed_url_cache import SignedUrlCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings")

# Constants
CALL_RECORDING_SIGNED_URL_TTL_HOURS = 96  # 4 days
SIGNED_URL_CACHE_PREFIX_CALL = "call:"
SIGNED_URL_CACHE_PREFIX_GS = "gs:"

# Lazy initialization
_call_storage: CallStorage | None = None
_gcs_storage: GCSStorageManager | None = None
_signed_url_cache: SignedUrlCache | None = None


def _get_call_storage() -> CallStorage:
    global _call_storage
    if _call_storage is None:
        _call_storage = CallStorage()
    return _call_storage


def _get_gcs_storage() -> GCSStorageManager:
    global _gcs_storage
    if _gcs_storage is None:
        _gcs_storage = GCSStorageManager()
    return _gcs_storage


def _get_signed_url_cache() -> SignedUrlCache:
    global _signed_url_cache
    if _signed_url_cache is None:
        _signed_url_cache = SignedUrlCache(ttl=timedelta(hours=CALL_RECORDING_SIGNED_URL_TTL_HOURS))
    return _signed_url_cache


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class SignedUrlRequest(BaseModel):
    gs_url: str = Field(..., description="GCS URL in gs:// format")
    expiration_hours: int = Field(96, ge=1, le=168, description="Expiration time in hours (default 96 = 4 days)")


class SignedUrlResponse(BaseModel):
    signed_url: str
    gs_url: str
    expires_in_hours: int


class CallLogSignedUrlResponse(BaseModel):
    call_log_id: str | None = None
    signed_url: str
    gs_url: str
    expires_at: datetime
    expires_in_hours: int
    cached: bool = False


# =============================================================================
# POST /recordings/signed-url - Generate signed URL from gs:// URL
# =============================================================================

@router.post("/signed-url", response_model=SignedUrlResponse)
async def generate_signed_url(request: SignedUrlRequest) -> SignedUrlResponse:
    """
    Generate a signed URL for accessing a GCS recording.
    
    This endpoint takes a gs:// URL from the database and generates a time-limited
    signed URL that provides direct access to the audio recording.
    
    Args:
        request: SignedUrlRequest with gs_url and expiration_hours
    
    Returns:
        SignedUrlResponse with the signed URL and metadata
    
    Raises:
        HTTPException: If the URL is invalid or the recording doesn't exist
    """
    gcs_storage = _get_gcs_storage()
    
    try:
        signed_url = gcs_storage.generate_signed_url_from_gs(
            gs_url=request.gs_url,
            expiration_hours=request.expiration_hours
        )
        
        if not signed_url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recording not found or inaccessible: {request.gs_url}"
            )
        
        return SignedUrlResponse(
            signed_url=signed_url,
            gs_url=request.gs_url,
            expires_in_hours=request.expiration_hours
        )
        
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to generate signed URL: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate signed URL: {str(exc)}"
        ) from exc


# =============================================================================
# GET /recordings/calls/{resource}/signed-url - Get signed URL with caching
# =============================================================================

@router.get("/calls/{resource:path}/signed-url", response_model=CallLogSignedUrlResponse)
async def get_call_recording_signed_url(resource: str) -> CallLogSignedUrlResponse:
    """
    Get signed URL for a call recording with caching.
    
    This endpoint supports two modes:
    1. Pass a gs:// URL directly
    2. Pass a call_log_id - will fetch gs:// URL from database
    
    Caching:
    - URLs are cached for 96 hours (4 days)
    - If same URL requested again, cached version is returned
    - Cache is keyed by both gs:// URL and call_log_id
    
    Args:
        resource: Either gs:// URL or call_log_id UUID
    
    Returns:
        CallLogSignedUrlResponse with signed URL and cache status
    """
    if resource is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="identifier cannot be empty")

    decoded_identifier = unquote(resource).strip()
    if not decoded_identifier:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="identifier cannot be empty")

    call_storage = _get_call_storage()
    gcs_storage = _get_gcs_storage()
    signed_url_cache = _get_signed_url_cache()

    def _build_response(entry, cached_flag: bool, *, call_log_id_override: str | None = None) -> CallLogSignedUrlResponse:
        effective_call_log_id = call_log_id_override if call_log_id_override is not None else entry.call_log_id
        return CallLogSignedUrlResponse(
            call_log_id=effective_call_log_id,
            signed_url=entry.signed_url,
            gs_url=entry.gs_url,
            expires_at=entry.expires_at,
            expires_in_hours=CALL_RECORDING_SIGNED_URL_TTL_HOURS,
            cached=cached_flag,
        )

    # Case 1: Direct gs:// URL
    if decoded_identifier.startswith("gs://"):
        gs_url = decoded_identifier
        cache_key = f"{SIGNED_URL_CACHE_PREFIX_GS}{gs_url}"
        
        # Check cache first
        cached_entry = await signed_url_cache.get(cache_key)
        if cached_entry:
            return _build_response(cached_entry, True)

        # Generate new signed URL
        signed_url = gcs_storage.generate_signed_url_from_gs(
            gs_url,
            expiration_hours=CALL_RECORDING_SIGNED_URL_TTL_HOURS,
        )
        if not signed_url:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Recording not found or inaccessible")

        # Cache and return
        cache_entry = await signed_url_cache.set(cache_key, signed_url, gs_url)
        logger.info(
            "Generated signed URL for gs_url=%s with %d-hour TTL",
            gs_url[:60] + "..." if len(gs_url) > 60 else gs_url,
            CALL_RECORDING_SIGNED_URL_TTL_HOURS,
        )
        return _build_response(cache_entry, False)

    # Case 2: call_log_id - fetch gs:// URL from database
    normalized_id = decoded_identifier
    call_cache_key = f"{SIGNED_URL_CACHE_PREFIX_CALL}{normalized_id}"
    
    # Check cache by call_log_id first
    cached_entry = await signed_url_cache.get(call_cache_key)
    if cached_entry:
        return _build_response(cached_entry, True, call_log_id_override=normalized_id)

    # Fetch call record from database
    record = await call_storage.get_call_by_id(normalized_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call log not found")

    gs_url = record.get("call_recording_url")
    if not gs_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call recording URL not available")
    if not isinstance(gs_url, str) or not gs_url.startswith("gs://"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Stored recording URL is not a valid gs:// reference")

    # Check if we have a cached entry for the gs:// URL
    gs_cache_key = f"{SIGNED_URL_CACHE_PREFIX_GS}{gs_url}"
    gs_cached_entry = await signed_url_cache.get(gs_cache_key)
    
    if gs_cached_entry:
        # Cache the call_log_id -> URL mapping too
        cache_entry = await signed_url_cache.set(
            call_cache_key,
            gs_cached_entry.signed_url,
            gs_cached_entry.gs_url,
            call_log_id=normalized_id,
        )
        return _build_response(cache_entry, True, call_log_id_override=normalized_id)

    # Generate new signed URL
    signed_url = gcs_storage.generate_signed_url_from_gs(
        gs_url,
        expiration_hours=CALL_RECORDING_SIGNED_URL_TTL_HOURS,
    )
    if not signed_url:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Recording not found or inaccessible")

    # Cache by both gs:// URL and call_log_id
    cache_entry = await signed_url_cache.set(
        call_cache_key,
        signed_url,
        gs_url,
        call_log_id=normalized_id,
    )
    
    # Also cache by gs:// URL for future direct lookups
    await signed_url_cache.set(gs_cache_key, signed_url, gs_url)
    
    logger.info(
        "Generated signed URL for call_log_id=%s with %d-hour TTL",
        normalized_id,
        CALL_RECORDING_SIGNED_URL_TTL_HOURS,
    )
    return _build_response(cache_entry, False, call_log_id_override=normalized_id)


__all__ = ["router"]

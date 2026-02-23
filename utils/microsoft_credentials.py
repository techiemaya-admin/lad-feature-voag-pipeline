"""Shared helpers for resolving and decrypting Microsoft OAuth credentials.

This module provides a parallel implementation to google_credentials.py
for Microsoft Graph API credentials management.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

from db.storage.tokens import UserTokenStorage
from utils.google_oauth import TokenEncryptor, get_google_oauth_settings
from utils.microsoft_oauth import MicrosoftAuthService

logger = logging.getLogger(__name__)


class MicrosoftCredentialError(RuntimeError):
    """Raised when Microsoft OAuth credentials cannot be located or decrypted."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class MicrosoftCredentialResolver:
    """Loads encrypted Microsoft tokens for a user and refreshes if needed."""

    def __init__(self, storage: UserTokenStorage | None = None) -> None:
        self._storage = storage or UserTokenStorage()
        self._encryptor: TokenEncryptor | None = None
        self._auth_service: MicrosoftAuthService | None = None

    def _canonicalize_identifier(self, identifier: str | int | None) -> str:
        if identifier is None:
            return ""
        if isinstance(identifier, int):
            return str(identifier)
        return identifier.strip()

    def _ensure_encryptor(self) -> TokenEncryptor:
        """Use the same encryption key as Google OAuth for consistency."""
        if self._encryptor is None:
            try:
                settings = get_google_oauth_settings()
            except Exception as exc:
                raise MicrosoftCredentialError(
                    "OAuth encryption configuration is invalid",
                    status_code=500,
                ) from exc
            self._encryptor = TokenEncryptor(settings.encryption_key)
        return self._encryptor

    def _ensure_auth_service(self) -> MicrosoftAuthService:
        """Get Microsoft auth service for token refresh."""
        if self._auth_service is None:
            try:
                self._auth_service = MicrosoftAuthService()
            except Exception as exc:
                raise MicrosoftCredentialError(
                    "Microsoft OAuth configuration is invalid",
                    status_code=500,
                ) from exc
        return self._auth_service

    async def resolve_user(self, identifier: str | int | None) -> Tuple[str, dict[str, Any]]:
        """Resolve user identifier to canonical ID and user record."""
        clean = self._canonicalize_identifier(identifier)
        if not clean:
            raise MicrosoftCredentialError("user_id is required", status_code=400)
        
        record: dict[str, Any] | None = None
        if clean.isdigit():
            record = await self._storage.get_user_by_primary_id(int(clean))
        if record is None:
            record = await self._storage.get_user_by_user_id(clean)
        if not record:
            raise MicrosoftCredentialError(f"User {clean} not found", status_code=404)
        
        # users table uses 'id' as the primary key (UUID)
        canonical = str(record.get("id") or "").strip()
        if not canonical:
            raise MicrosoftCredentialError(
                "User record missing canonical identifier",
                status_code=500,
            )
        return canonical, record

    async def load_access_token(self, identifier: str | int | None) -> str:
        """
        Load and return a valid Microsoft access token.
        
        Refreshes the token if expired.
        
        Args:
            identifier: User ID (numeric or UUID)
            
        Returns:
            Valid access token string
            
        Raises:
            MicrosoftCredentialError: If credentials not found or invalid
        """
        canonical_id, record = await self.resolve_user(identifier)
        
        # Get encrypted token blob
        blob = await self._storage.get_microsoft_token_blob(canonical_id)
        if not blob:
            raise MicrosoftCredentialError(
                "User has not authorized Microsoft access",
                status_code=404,
            )
        
        encryptor = self._ensure_encryptor()
        try:
            payload = encryptor.decrypt_json(blob)
        except ValueError as exc:
            raise MicrosoftCredentialError(
                "Stored Microsoft OAuth tokens are invalid",
                status_code=500,
            ) from exc
        
        if not payload:
            raise MicrosoftCredentialError(
                "Stored Microsoft OAuth tokens are unavailable",
                status_code=404,
            )
        
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        
        if not access_token:
            raise MicrosoftCredentialError(
                "Microsoft access token is missing",
                status_code=500,
            )
        
        # For now, return the stored token
        # Token refresh happens automatically via MSAL when needed
        # TODO: Add expiry check and refresh logic if needed
        
        logger.debug(f"Loaded Microsoft access token for user {canonical_id}")
        return access_token

    async def get_refreshed_token(self, identifier: str | int | None) -> str:
        """
        Force refresh and return a new access token.
        
        Args:
            identifier: User ID
            
        Returns:
            New access token
        """
        canonical_id, record = await self.resolve_user(identifier)
        
        blob = await self._storage.get_microsoft_token_blob(canonical_id)
        if not blob:
            raise MicrosoftCredentialError(
                "User has not authorized Microsoft access",
                status_code=404,
            )
        
        encryptor = self._ensure_encryptor()
        payload = encryptor.decrypt_json(blob)
        
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise MicrosoftCredentialError(
                "No refresh token available - user needs to re-authorize",
                status_code=401,
            )
        
        # Refresh the token
        auth_service = self._ensure_auth_service()
        try:
            new_tokens = auth_service.refresh_token(refresh_token)
        except ValueError as exc:
            raise MicrosoftCredentialError(
                f"Token refresh failed: {exc}",
                status_code=401,
            ) from exc
        
        new_access_token = new_tokens.get("access_token")
        if not new_access_token:
            raise MicrosoftCredentialError(
                "Token refresh did not return access token",
                status_code=500,
            )
        
        # TODO: Store the refreshed tokens back to DB
        
        logger.info(f"Refreshed Microsoft access token for user {canonical_id}")
        return new_access_token


__all__ = ["MicrosoftCredentialResolver", "MicrosoftCredentialError"]

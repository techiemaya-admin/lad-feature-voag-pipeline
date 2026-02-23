"""Shared helpers for resolving and decrypting Google OAuth credentials."""

from __future__ import annotations

from typing import Any, Tuple

from google.oauth2.credentials import Credentials

from db.storage.tokens import UserTokenStorage
from utils.google_oauth import TokenEncryptor, credentials_from_dict, get_google_oauth_settings


class GoogleCredentialError(RuntimeError):
    """Raised when Google OAuth credentials cannot be located or decrypted."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _coerce_db_blob(blob: Any) -> bytes | None:
    if blob is None:
        return None
    if isinstance(blob, memoryview):
        return blob.tobytes()
    if isinstance(blob, bytearray):
        return bytes(blob)
    if isinstance(blob, bytes):
        return blob
    return None


class GoogleCredentialResolver:
    """Loads encrypted Google tokens for either numeric id or legacy user_id."""

    def __init__(self, storage: UserTokenStorage | None = None) -> None:
        self._storage = storage or UserTokenStorage()
        self._encryptor: TokenEncryptor | None = None

    def _canonicalize_identifier(self, identifier: str | int | None) -> str:
        if identifier is None:
            return ""
        if isinstance(identifier, int):
            return str(identifier)
        return identifier.strip()

    def _ensure_encryptor(self) -> TokenEncryptor:
        if self._encryptor is None:
            try:
                settings = get_google_oauth_settings()
            except Exception as exc:  # noqa: BLE001
                raise GoogleCredentialError(
                    "Google OAuth configuration is invalid",
                    status_code=500,
                ) from exc
            self._encryptor = TokenEncryptor(settings.encryption_key)
        return self._encryptor

    async def resolve_user(self, identifier: str | int | None) -> Tuple[str, dict[str, Any]]:
        clean = self._canonicalize_identifier(identifier)
        if not clean:
            raise GoogleCredentialError("user_id is required", status_code=400)
        record: dict[str, Any] | None = None
        if clean.isdigit():
            record = await self._storage.get_user_by_primary_id(int(clean))
        if record is None:
            record = await self._storage.get_user_by_user_id(clean)
        if not record:
            raise GoogleCredentialError(f"User {clean} not found", status_code=404)
        # users table uses 'id' as the primary key (UUID)
        canonical = str(record.get("id") or "").strip()
        if not canonical:
            raise GoogleCredentialError(
                "User record missing canonical identifier",
                status_code=500,
            )
        return canonical, record

    async def load_credentials(self, identifier: str | int | None) -> Credentials:
        canonical_id, record = await self.resolve_user(identifier)
        # Tokens are stored in user_identities table, not users table
        blob = await self._storage.get_google_token_blob(canonical_id)
        if not blob:
            raise GoogleCredentialError(
                "User has not authorized Google access",
                status_code=404,
            )
        encryptor = self._ensure_encryptor()
        try:
            payload = encryptor.decrypt_json(blob)
        except ValueError as exc:  # noqa: BLE001
            raise GoogleCredentialError(
                "Stored Google OAuth tokens are invalid",
                status_code=500,
            ) from exc
        if not payload:
            raise GoogleCredentialError(
                "Stored Google OAuth tokens are unavailable",
                status_code=404,
            )
        credentials = credentials_from_dict(payload)
        # Prevent timezone-aware/naive comparison issues inside google-auth.
        credentials.expiry = None
        # Ensure the credentials retain canonical user reference for auditing
        credentials._canonical_user_id = canonical_id  # type: ignore[attr-defined]
        return credentials


__all__ = ["GoogleCredentialResolver", "GoogleCredentialError"]

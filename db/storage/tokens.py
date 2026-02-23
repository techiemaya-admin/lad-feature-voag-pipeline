"""Utility helpers for reading/writing OAuth tokens via user_identities table.

Updated for dynamic schema where tokens are stored in user_identities table,
not directly on the users table.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Import connection pool manager
from db.connection_pool import get_db_connection, return_connection, USE_CONNECTION_POOLING
from db.schema_constants import USERS_FULL, USER_IDENTITIES_FULL

load_dotenv()

logger = logging.getLogger(__name__)


class UserTokenStorage:
    """Provides access to OAuth tokens via user_identities table."""

    def __init__(self) -> None:
        self.db_config = {
            "host": os.getenv("DB_HOST"),
            "port": int(os.getenv("DB_PORT", "5432")),
            "database": os.getenv("DB_NAME"),
            "user": os.getenv("DB_USER"),
            "password": os.getenv("DB_PASSWORD"),
        }
        required = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
        missing = [env for env in required if not os.getenv(env)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    def _get_connection(self) -> psycopg2.extensions.connection:
        """Get database connection (pooled or direct based on feature flag)"""
        return get_db_connection(self.db_config)
    
    def _return_connection(self, conn):
        """Return connection to pool if pooling is enabled"""
        if USE_CONNECTION_POOLING:
            return_connection(conn, self.db_config)

    # =========================================================================
    # User Lookup Methods
    # =========================================================================

    async def get_user_by_user_id(self, user_id: str) -> Optional[dict[str, Any]]:
        """Get user record by UUID from users table."""
        # Validate user_id - reject None, empty, or string 'None'/'null'
        if not user_id or str(user_id).strip().lower() in ('none', 'null', ''):
            return None
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"SELECT id, email, first_name, last_name FROM {USERS_FULL} WHERE id = %s",
                        (user_id,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch user %s: %s", user_id, exc, exc_info=True)
            raise

    async def get_user_by_primary_id(self, user_pk: str) -> Optional[dict[str, Any]]:
        """Alias for get_user_by_user_id (UUID is the primary key)."""
        return await self.get_user_by_user_id(user_pk)

    async def get_user_tenant_id(self, user_id: str) -> Optional[str]:
        """Get primary_tenant_id for a user.
        
        Args:
            user_id: User UUID
            
        Returns:
            primary_tenant_id if found, None otherwise
        """
        if not user_id:
            return None
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT primary_tenant_id FROM {USERS_FULL} WHERE id = %s",
                        (user_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0])
                    return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to get tenant for user %s: %s", user_id, exc, exc_info=True)
            return None

    # =========================================================================
    # Identity/Token Lookup Methods
    # =========================================================================

    async def get_identity(
        self, user_id: str, provider: str
    ) -> Optional[dict[str, Any]]:
        """Get OAuth identity record for a user and provider."""
        if not user_id or not provider:
            return None
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""SELECT id, user_id, provider, provider_user_id,
                                  access_token, refresh_token, token_expires_at,
                                  provider_data, created_at, updated_at
                           FROM {USER_IDENTITIES_FULL}
                           WHERE user_id = %s AND provider = %s""",
                        (user_id, provider),
                    )
                    row = cur.fetchone()
                    logger.debug("get_identity: user_id=%s, provider=%s, found=%s", user_id, provider, bool(row))
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to get identity for user=%s provider=%s: %s", user_id, provider, exc, exc_info=True)
            raise

    # =========================================================================
    # Google OAuth Methods
    # =========================================================================

    async def store_token_blob(
        self, user_id: str, blob: bytes, connected_gmail: str | None = None
    ) -> None:
        """Store encrypted Google OAuth token blob for user.
        
        The blob is stored in provider_data as base64, and connected_gmail
        is stored in provider_user_id.
        """
        import base64
        provider = "google"
        blob_b64 = base64.b64encode(blob).decode('utf-8')
        
        provider_data = {"token_blob": blob_b64}
        if connected_gmail:
            provider_data["connected_gmail"] = connected_gmail
        
        # provider_user_id cannot be null, use email or fallback
        provider_user_id = connected_gmail or f"google:{user_id}"
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # Upsert: insert or update on conflict
                    cur.execute(
                        f"""INSERT INTO {USER_IDENTITIES_FULL} 
                               (user_id, provider, provider_user_id, provider_data, updated_at)
                           VALUES (%s, %s, %s, %s, NOW())
                           ON CONFLICT (user_id, provider) DO UPDATE SET
                               provider_user_id = EXCLUDED.provider_user_id,
                               provider_data = EXCLUDED.provider_data,
                               updated_at = NOW()""",
                        (user_id, provider, provider_user_id, json.dumps(provider_data)),
                    )
                conn.commit()
                logger.info("Stored Google OAuth tokens for user_id=%s", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist Google tokens for %s: %s", user_id, exc, exc_info=True)
            raise

    async def get_google_token_blob(self, user_id: str) -> Optional[bytes]:
        """Get encrypted Google OAuth token blob for user."""
        import base64
        
        identity = await self.get_identity(user_id, "google")
        if not identity:
            return None
        
        provider_data = identity.get("provider_data") or {}
        blob_b64 = provider_data.get("token_blob")
        if not blob_b64:
            return None
        
        return base64.b64decode(blob_b64)

    async def get_connected_gmail(self, user_id: str) -> Optional[str]:
        """Get connected Gmail address for user."""
        identity = await self.get_identity(user_id, "google")
        if not identity:
            return None
        
        provider_data = identity.get("provider_data") or {}
        return provider_data.get("connected_gmail") or identity.get("provider_user_id")

    async def remove_tokens(self, user_id: str) -> None:
        """Clear Google OAuth tokens for the user."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {USER_IDENTITIES_FULL} WHERE user_id = %s AND provider = %s",
                        (user_id, "google"),
                    )
                conn.commit()
                logger.info("Cleared Google OAuth tokens for user_id=%s", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to clear Google tokens for %s: %s", user_id, exc, exc_info=True)
            raise

    # =========================================================================
    # Microsoft OAuth Methods
    # =========================================================================

    async def store_microsoft_token_blob(
        self,
        user_id: str,
        blob: bytes,
        connected_account: str | None = None,
    ) -> None:
        """Store encrypted Microsoft OAuth token blob for user.
        
        IMPORTANT: Uses JSONB merge (||) to preserve existing booking_config
        when only updating the token_blob.
        """
        import base64
        provider = "microsoft"
        blob_b64 = base64.b64encode(blob).decode('utf-8')
        
        provider_data = {"token_blob": blob_b64}
        if connected_account:
            provider_data["connected_account"] = connected_account
        
        # provider_user_id cannot be null, use email or fallback
        provider_user_id = connected_account or f"microsoft:{user_id}"
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # Use JSONB merge (||) to preserve existing data like booking_config
                    # New data takes precedence for overlapping keys
                    cur.execute(
                        f"""INSERT INTO {USER_IDENTITIES_FULL} 
                               (user_id, provider, provider_user_id, provider_data, updated_at)
                           VALUES (%s, %s, %s, %s::jsonb, NOW())
                           ON CONFLICT (user_id, provider) DO UPDATE SET
                               provider_user_id = EXCLUDED.provider_user_id,
                               provider_data = {USER_IDENTITIES_FULL}.provider_data || EXCLUDED.provider_data,
                               updated_at = NOW()""",
                        (user_id, provider, provider_user_id, json.dumps(provider_data)),
                    )
                conn.commit()
                logger.info("Stored Microsoft OAuth tokens for user_id=%s (merged with existing data)", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist Microsoft tokens for %s: %s", user_id, exc, exc_info=True)
            raise

    async def get_microsoft_token_blob(self, user_id: str) -> Optional[bytes]:
        """Get encrypted Microsoft OAuth token blob for user."""
        import base64
        
        identity = await self.get_identity(user_id, "microsoft")
        if not identity:
            return None
        
        provider_data = identity.get("provider_data") or {}
        blob_b64 = provider_data.get("token_blob")
        if not blob_b64:
            return None
        
        return base64.b64decode(blob_b64)

    async def remove_microsoft_tokens(self, user_id: str) -> None:
        """Clear Microsoft OAuth tokens for the user."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {USER_IDENTITIES_FULL} WHERE user_id = %s AND provider = %s",
                        (user_id, "microsoft"),
                    )
                conn.commit()
                logger.info("Cleared Microsoft OAuth tokens for user_id=%s", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to clear Microsoft tokens for %s: %s", user_id, exc, exc_info=True)
            raise

    async def store_booking_config(
        self,
        user_id: str,
        business_id: str,
        business_name: str | None = None,
        service_id: str | None = None,
        staff_member_id: str | None = None,
    ) -> None:
        """Store selected booking business, service, and staff configuration.
        
        Stored in Microsoft identity's provider_data.
        """
        identity = await self.get_identity(user_id, "microsoft")
        if not identity:
            raise ValueError(f"No Microsoft identity found for user {user_id}")
        
        provider_data = identity.get("provider_data") or {}
        provider_data["booking_config"] = {
            "business_id": business_id,
            "business_name": business_name,
            "service_id": service_id,
            "staff_member_id": staff_member_id,
        }
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""UPDATE {USER_IDENTITIES_FULL} 
                           SET provider_data = %s, updated_at = NOW()
                           WHERE user_id = %s AND provider = %s""",
                        (json.dumps(provider_data), user_id, "microsoft"),
                    )
                    if cur.rowcount == 0:
                        raise ValueError(f"User {user_id} not found when storing booking config")
                conn.commit()
                logger.info("Stored booking config for user_id=%s", user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to store booking config for %s: %s", user_id, exc, exc_info=True)
            raise

    async def get_booking_config(self, user_id: str) -> Optional[dict[str, Any]]:
        """Get booking configuration from Microsoft identity."""
        identity = await self.get_identity(user_id, "microsoft")
        logger.debug("get_booking_config: user_id=%s, identity_found=%s", user_id, bool(identity))
        if not identity:
            return None
        
        provider_data = identity.get("provider_data")
        logger.debug("get_booking_config: raw provider_data type=%s, value=%r", type(provider_data).__name__, str(provider_data)[:200] if provider_data else None)
        
        # Handle case where provider_data is a JSON string instead of dict
        if isinstance(provider_data, str):
            try:
                provider_data = json.loads(provider_data)
            except json.JSONDecodeError:
                logger.error("get_booking_config: Failed to parse provider_data as JSON")
                return None
        
        if not provider_data:
            return None
            
        booking_config = provider_data.get("booking_config")
        logger.debug("get_booking_config: booking_config=%r", booking_config)
        return booking_config

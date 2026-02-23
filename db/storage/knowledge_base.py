"""
Knowledge Base Storage - Database layer for Google File Search stores.

Provides CRUD operations for knowledge base stores and their associations
with agents and leads for the RAG (Retrieval-Augmented Generation) feature.
"""

import logging
import os
from datetime import datetime
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from db.connection_pool import get_db_connection, USE_CONNECTION_POOLING, return_connection

load_dotenv()

logger = logging.getLogger(__name__)


class KnowledgeBaseStorageError(Exception):
    """Exception raised for knowledge base storage errors."""
    pass


class KnowledgeBaseStorage:
    """
    Database storage for knowledge base (File Search) stores.
    
    Uses the knowledge_base_catalog table which associates KB stores
    with tenants rather than individual agents/leads.
    """

    SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")
    STORES_TABLE = "knowledge_base_catalog"
    # Note: Agent/lead links are not used in new schema - stores are linked via tenant_id

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

    def _get_connection(self):
        """Get database connection (pooled or direct based on feature flag)."""
        return get_db_connection(self.db_config)

    def _return_connection(self, conn):
        """Return connection to pool if pooling is enabled."""
        if USE_CONNECTION_POOLING:
            return_connection(conn, self.db_config)

    async def ensure_tables_exist(self) -> None:
        """Create knowledge base tables if they don't exist."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # Main stores table
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.STORES_TABLE} (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            gemini_store_name TEXT NOT NULL UNIQUE,
                            display_name TEXT NOT NULL,
                            description TEXT,
                            document_count INTEGER DEFAULT 0,
                            is_active BOOLEAN DEFAULT TRUE,
                            created_by_user_id INTEGER,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)

                    # Agent-to-store links
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.AGENT_LINKS_TABLE} (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            agent_id INTEGER NOT NULL,
                            store_id UUID NOT NULL REFERENCES {self.SCHEMA}.{self.STORES_TABLE}(id) ON DELETE CASCADE,
                            is_default BOOLEAN DEFAULT FALSE,
                            priority INTEGER DEFAULT 0,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(agent_id, store_id)
                        )
                    """)

                    # Lead-to-store links (for per-call customization)
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.LEAD_LINKS_TABLE} (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            lead_id INTEGER NOT NULL,
                            store_id UUID NOT NULL REFERENCES {self.SCHEMA}.{self.STORES_TABLE}(id) ON DELETE CASCADE,
                            priority INTEGER DEFAULT 0,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(lead_id, store_id)
                        )
                    """)

                    conn.commit()
                    logger.info("Knowledge base tables ensured")
        except Exception as exc:
            logger.error("Failed to create knowledge base tables: %s", exc, exc_info=True)
            raise KnowledgeBaseStorageError(f"Failed to create tables: {exc}") from exc

    # =========================================================================
    # STORE CRUD
    # =========================================================================

    async def create_store(
        self,
        tenant_id: str,  # Required - UUID
        gemini_store_name: str,
        display_name: str,
        description: Optional[str] = None,
        is_default: bool = False,
        priority: int = 0,
        created_by: Optional[str] = None,  # UUID
    ) -> str:
        """
        Create a new knowledge base store record.
        
        Args:
            tenant_id: Tenant UUID that owns this store
            gemini_store_name: The Gemini API store name (e.g., "fileSearchStores/xxx")
            display_name: Human-readable name
            description: Optional description
            is_default: Auto-attach to tenant's calls
            priority: Higher = preferred when multiple stores
            created_by: User UUID who created the store
            
        Returns:
            UUID of the created store record
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.SCHEMA}.{self.STORES_TABLE}
                            (tenant_id, gemini_store_name, display_name, description, 
                             is_default, priority, created_by)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::uuid)
                        RETURNING id::text
                        """,
                        (tenant_id, gemini_store_name, display_name, description,
                         is_default, priority, created_by),
                    )
                    result = cur.fetchone()
                    conn.commit()
                    store_id = result[0] if result else None
                    logger.info("Created knowledge base store: %s (%s) for tenant %s", 
                               display_name, store_id, tenant_id)
                    return store_id
        except psycopg2.IntegrityError as exc:
            logger.error("Store already exists: %s", gemini_store_name)
            raise KnowledgeBaseStorageError(f"Store already exists: {gemini_store_name}") from exc
        except Exception as exc:
            logger.error("Failed to create store: %s", exc, exc_info=True)
            raise KnowledgeBaseStorageError(f"Failed to create store: {exc}") from exc

    async def get_store_by_id(self, store_id: str) -> Optional[dict[str, Any]]:
        """Fetch a store by its database UUID."""
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id::text, tenant_id::text, gemini_store_name, display_name, 
                               description, is_default, is_active, priority, document_count,
                               created_by::text, created_at, updated_at
                        FROM {self.SCHEMA}.{self.STORES_TABLE}
                        WHERE id = %s::uuid
                        """,
                        (store_id,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:
            logger.error("Failed to get store %s: %s", store_id, exc, exc_info=True)
            return None

    async def get_store_by_gemini_name(self, gemini_store_name: str) -> Optional[dict[str, Any]]:
        """Fetch a store by its Gemini API name."""
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id::text, tenant_id::text, gemini_store_name, display_name,
                               description, is_default, is_active, priority, document_count,
                               created_by::text, created_at, updated_at
                        FROM {self.SCHEMA}.{self.STORES_TABLE}
                        WHERE gemini_store_name = %s
                        """,
                        (gemini_store_name,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:
            logger.error("Failed to get store by name %s: %s", gemini_store_name, exc, exc_info=True)
            return None

    async def list_stores(
        self,
        tenant_id: Optional[str] = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List knowledge base stores, optionally filtered by tenant."""
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    conditions = []
                    params = []

                    if active_only:
                        conditions.append("is_active = TRUE")
                    if tenant_id is not None:
                        conditions.append("tenant_id = %s::uuid")
                        params.append(tenant_id)

                    where_clause = " AND ".join(conditions) if conditions else "TRUE"

                    cur.execute(
                        f"""
                        SELECT id::text, tenant_id::text, gemini_store_name, display_name,
                               description, is_default, is_active, priority, document_count,
                               created_by::text, created_at, updated_at
                        FROM {self.SCHEMA}.{self.STORES_TABLE}
                        WHERE {where_clause}
                        ORDER BY priority DESC, created_at DESC
                        """,
                        params,
                    )
                    return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list stores: %s", exc, exc_info=True)
            return []

    async def update_store(
        self,
        store_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        document_count: Optional[int] = None,
        is_active: Optional[bool] = None,
        is_default: Optional[bool] = None,
        priority: Optional[int] = None,
    ) -> bool:
        """Update a store's metadata."""
        updates = []
        params = []

        if display_name is not None:
            updates.append("display_name = %s")
            params.append(display_name)
        if description is not None:
            updates.append("description = %s")
            params.append(description)
        if document_count is not None:
            updates.append("document_count = %s")
            params.append(document_count)
        if is_active is not None:
            updates.append("is_active = %s")
            params.append(is_active)
        if is_default is not None:
            updates.append("is_default = %s")
            params.append(is_default)
        if priority is not None:
            updates.append("priority = %s")
            params.append(priority)

        if not updates:
            return True

        updates.append("updated_at = NOW()")
        params.append(store_id)

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {self.SCHEMA}.{self.STORES_TABLE}
                        SET {", ".join(updates)}
                        WHERE id = %s::uuid
                        """,
                        params,
                    )
                    conn.commit()
                    return cur.rowcount > 0
        except Exception as exc:
            logger.error("Failed to update store %s: %s", store_id, exc, exc_info=True)
            return False

    async def delete_store(self, store_id: str) -> bool:
        """Delete a store and its associations."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {self.SCHEMA}.{self.STORES_TABLE} WHERE id = %s::uuid",
                        (store_id,),
                    )
                    conn.commit()
                    deleted = cur.rowcount > 0
                    if deleted:
                        logger.info("Deleted knowledge base store: %s", store_id)
                    return deleted
        except Exception as exc:
            logger.error("Failed to delete store %s: %s", store_id, exc, exc_info=True)
            return False

    async def update_document_count(self, store_id: str, delta: int) -> bool:
        """
        Update document count by a delta (positive to add, negative to remove).
        
        Args:
            store_id: Store UUID
            delta: Number to add to document_count (can be negative)
            
        Returns:
            True if updated successfully
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {self.SCHEMA}.{self.STORES_TABLE}
                        SET document_count = GREATEST(0, COALESCE(document_count, 0) + %s),
                            updated_at = NOW()
                        WHERE id = %s::uuid
                        """,
                        (delta, store_id),
                    )
                    conn.commit()
                    updated = cur.rowcount > 0
                    if updated:
                        logger.debug("Updated document count for store %s: delta=%d", store_id, delta)
                    return updated
        except Exception as exc:
            logger.error("Failed to update document count for %s: %s", store_id, exc, exc_info=True)
            return False

    # =========================================================================
    # TENANT-BASED STORE RESOLUTION (New Schema)
    # =========================================================================
    # Note: The new knowledge_base_catalog schema uses tenant_id
    # instead of agent/lead linking tables. Stores are associated directly
    # with tenants. The old agent/lead linking methods are deprecated.

    async def get_stores_for_tenant(
        self,
        tenant_id: str,
        default_only: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Get KB stores for a tenant, optionally filtered to defaults only.
        
        This is the primary method for call-time KB resolution.
        
        Args:
            tenant_id: Tenant UUID
            default_only: If True, only return stores where is_default=True
            
        Returns:
            List of store records ordered by priority
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    conditions = ["tenant_id = %s::uuid", "is_active = TRUE"]
                    if default_only:
                        conditions.append("is_default = TRUE")
                    
                    where_clause = " AND ".join(conditions)
                    
                    cur.execute(
                        f"""
                        SELECT id::text, tenant_id::text, gemini_store_name, display_name,
                               description, is_default, priority, document_count
                        FROM {self.SCHEMA}.{self.STORES_TABLE}
                        WHERE {where_clause}
                        ORDER BY priority DESC
                        """,
                        (tenant_id,),
                    )
                    return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to get stores for tenant %s: %s", tenant_id, exc, exc_info=True)
            return []

    async def get_gemini_store_names_for_tenant(self, tenant_id: str) -> list[str]:
        """
        Get Gemini store names for a tenant (for tool_builder integration).
        
        Only returns active default stores ordered by priority.
        
        Args:
            tenant_id: Tenant UUID
            
        Returns:
            List of Gemini store names (e.g., ["fileSearchStores/xxx"])
        """
        stores = await self.get_stores_for_tenant(tenant_id, default_only=True)
        return [s["gemini_store_name"] for s in stores]

    # =========================================================================
    # DEPRECATED: Agent/Lead Linking (Use tenant_id instead)
    # =========================================================================
    # These methods are kept for backward compatibility but log deprecation warnings.
    # In the new schema, stores are linked via tenant_id, not agent/lead tables.

    async def link_store_to_agent(self, agent_id: int, store_id: str, **kwargs) -> str:
        """Deprecated: Use tenant_id association instead."""
        logger.warning("link_store_to_agent is deprecated - stores are now linked via tenant_id")
        raise KnowledgeBaseStorageError("Agent-store linking is deprecated. Use tenant_id to associate KB stores.")

    async def unlink_store_from_agent(self, agent_id: int, store_id: str) -> bool:
        """Deprecated: Use tenant_id association instead."""
        logger.warning("unlink_store_from_agent is deprecated - stores are now linked via tenant_id")
        return False

    async def get_stores_for_agent(self, agent_id: int) -> list[dict[str, Any]]:
        """Deprecated: Use get_stores_for_tenant instead."""
        logger.warning("get_stores_for_agent is deprecated - use get_stores_for_tenant")
        return []

    async def link_store_to_lead(self, lead_id: str, store_id: str, **kwargs) -> str:
        """Deprecated: Use tenant_id association instead."""
        logger.warning("link_store_to_lead is deprecated - stores are now linked via tenant_id")
        raise KnowledgeBaseStorageError("Lead-store linking is deprecated. Use tenant_id to associate KB stores.")

    async def unlink_store_from_lead(self, lead_id: str, store_id: str) -> bool:
        """Deprecated: Use tenant_id association instead."""
        logger.warning("unlink_store_from_lead is deprecated - stores are now linked via tenant_id")
        return False

    async def get_stores_for_lead(self, lead_id: str) -> list[dict[str, Any]]:
        """Deprecated: Use get_stores_for_tenant instead."""
        logger.warning("get_stores_for_lead is deprecated - use get_stores_for_tenant")
        return []

    async def get_stores_for_call(
        self,
        tenant_id: Optional[str] = None,
        store_ids: Optional[list[str]] = None,
        **kwargs,  # Accept but ignore deprecated agent_id/lead_id
    ) -> list[str]:
        """
        Resolve Gemini store names for a call.
        
        Priority order:
        1. Explicit store_ids (if provided)
        2. Tenant's default stores (via tenant_id)
        
        Args:
            tenant_id: Tenant UUID for auto-resolution
            store_ids: Explicit store UUIDs from call parameters
            
        Returns:
            List of Gemini store names (e.g., ["fileSearchStores/xxx"])
        """
        # Priority 1: Explicit store IDs
        if store_ids:
            try:
                with self._get_connection() as conn:
                    with conn.cursor() as cur:
                        placeholders = ",".join(["%s::uuid"] * len(store_ids))
                        cur.execute(
                            f"""
                            SELECT gemini_store_name
                            FROM {self.SCHEMA}.{self.STORES_TABLE}
                            WHERE id IN ({placeholders}) AND is_active = TRUE
                            """,
                            store_ids,
                        )
                        gemini_names = [row[0] for row in cur.fetchall()]
                        if gemini_names:
                            logger.debug("Resolved %d explicit stores for call", len(gemini_names))
                            return gemini_names
            except Exception as exc:
                logger.error("Failed to resolve explicit stores: %s", exc)

        # Priority 2: Tenant's default stores
        if tenant_id:
            return await self.get_gemini_store_names_for_tenant(tenant_id)

        return []


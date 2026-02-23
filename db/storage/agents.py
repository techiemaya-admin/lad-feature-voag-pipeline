"""
Agent metadata storage helpers.

Updated for dynamic schema (Phase 12):
- Table: SCHEMA.voice_agents (was agents_voiceagent)
- Added: tenant_id for multi-tenancy
- Uses voice_permissions for org lookup (was org_permissions_voiceagent)
"""

import os
import logging
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Import connection pool manager
from db.connection_pool import get_db_connection, return_connection, USE_CONNECTION_POOLING
from db.db_config import get_db_config

load_dotenv()

logger = logging.getLogger(__name__)

# Schema and table constants
SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")
AGENTS_TABLE = "voice_agents"
PERMISSIONS_TABLE = "voice_permissions"
FULL_AGENTS_TABLE = f"{SCHEMA}.{AGENTS_TABLE}"
FULL_PERMISSIONS_TABLE = f"{SCHEMA}.{PERMISSIONS_TABLE}"


class AgentStorage:
    """
    Provides read access to agent prompt configuration.
    
    Uses voice_agents schema with:
    - tenant_id for multi-tenancy filtering
    - voice_permissions for organization lookup
    """

    def __init__(self) -> None:
        self.db_config = get_db_config()

    def _get_connection(self) -> psycopg2.extensions.connection:
        """Get database connection (pooled or direct based on feature flag)"""
        return get_db_connection(self.db_config)
    
    def _return_connection(self, conn):
        """Return connection to pool if pooling is enabled"""
        if USE_CONNECTION_POOLING:
            return_connection(conn, self.db_config)

    async def get_agent_by_id(
        self,
        agent_id: Any,
        tenant_id: str | None = None
    ) -> Optional[dict[str, Any]]:
        """
        Fetch an agent record by id.
        
        Args:
            agent_id: The agent's ID (UUID or int)
            tenant_id: Optional tenant ID for scoping
            
        Returns:
            Agent dict if found, None otherwise
        """
        if agent_id is None:
            logger.debug("get_agent_by_id called with None")
            return None

        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if tenant_id:
                        cur.execute(
                            f"""
                            SELECT id, tenant_id, name,
                                   agent_instructions, system_instructions,
                                   outbound_starter_prompt, inbound_starter_prompt,
                                   language, voice_id,
                                   created_at, updated_at
                            FROM {FULL_AGENTS_TABLE}
                            WHERE id = %s AND tenant_id = %s
                            LIMIT 1
                            """,
                            (agent_id, tenant_id),
                        )
                    else:
                        cur.execute(
                            f"""
                            SELECT id, tenant_id, name,
                                   agent_instructions, system_instructions,
                                   outbound_starter_prompt, inbound_starter_prompt,
                                   language, voice_id,
                                   created_at, updated_at
                            FROM {FULL_AGENTS_TABLE}
                            WHERE id = %s
                            LIMIT 1
                            """,
                            (agent_id,),
                        )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch agent %s: %s", agent_id, exc, exc_info=True)
            return None

    async def get_agent_tenant_id(self, agent_id: Any) -> Optional[str]:
        """
        Get the tenant_id for an agent.
        
        In the new schema, tenant_id is directly on the agent record.
        
        Args:
            agent_id: The agent's ID
            
        Returns:
            The tenant UUID as a string, or None if not found
        """
        if agent_id is None:
            return None
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT tenant_id::text
                        FROM {FULL_AGENTS_TABLE}
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (agent_id,),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to get tenant for agent %s: %s", agent_id, exc, exc_info=True)
            return None

    async def get_agent_organization_id(self, agent_id: Any) -> Optional[str]:
        """
        Get the organization ID for an agent.
        
        In the new schema, this is equivalent to tenant_id.
        Kept for backwards compatibility.
        
        Args:
            agent_id: The agent's ID
            
        Returns:
            The organization/tenant UUID as a string, or None if not found
        """
        # In new schema, org_id is now tenant_id
        return await self.get_agent_tenant_id(agent_id)

    async def is_education_agent(self, agent_id: Any) -> bool:
        """
        Check if an agent belongs to the Education vertical (e.g. Glinks).
        
        Args:
            agent_id: The agent's ID
            
        Returns:
            True if the agent belongs to Education vertical, False otherwise
        """
        from utils.tenant_utils import get_vertical_from_org_id
        
        org_id = await self.get_agent_organization_id(agent_id)
        vertical = get_vertical_from_org_id(org_id)
        return vertical == "education"

    async def get_agents_by_tenant(
        self,
        tenant_id: str,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Get all agents for a tenant.
        
        Args:
            tenant_id: Tenant UUID
            limit: Maximum number of agents to return
            
        Returns:
            List of agent dicts
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id, tenant_id, name,
                               agent_instructions, system_instructions,
                               language, voice_id,
                               created_at, updated_at
                        FROM {FULL_AGENTS_TABLE}
                        WHERE tenant_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (tenant_id, limit),
                    )
                    rows = cur.fetchall()
                    return [dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch agents for tenant %s: %s", tenant_id, exc, exc_info=True)
            return []

    async def list_agents(
        self,
        limit: int = 50,
        offset: int = 0,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """
        List all agents across all tenants.
        
        Args:
            limit: Maximum number of agents to return
            offset: Pagination offset
            active_only: Only return active agents (has is_active column)
            
        Returns:
            List of agent dicts
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Note: voice_agents table may not have is_active column
                    # So we just return all agents
                    cur.execute(
                        f"""
                        SELECT id, tenant_id, name,
                               agent_instructions AS instructions,
                               system_instructions,
                               outbound_starter_prompt, inbound_starter_prompt,
                               language, voice_id,
                               created_at, updated_at
                        FROM {FULL_AGENTS_TABLE}
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s
                        """,
                        (limit, offset),
                    )
                    rows = cur.fetchall()
                    return [dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list agents: %s", exc, exc_info=True)
            return []

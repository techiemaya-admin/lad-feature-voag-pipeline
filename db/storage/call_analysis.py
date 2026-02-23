"""
Call Analysis storage helpers.

Phase 13: Post-Call Analysis Refactor
- Table: SCHEMA.voice_call_analysis
- 1:1 relationship with voice_call_logs via call_log_id
"""

import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Union

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

from db.connection_pool import get_db_connection, return_connection, USE_CONNECTION_POOLING
from db.db_config import get_db_config

load_dotenv()

logger = logging.getLogger(__name__)

# Schema and table constants
SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")
TABLE = "voice_call_analysis"
FULL_TABLE = f"{SCHEMA}.{TABLE}"


class CallAnalysisStorage:
    """
    Handles database operations for call analysis records.
    
    Uses voice_call_analysis schema with:
    - tenant_id for multi-tenancy isolation
    - call_log_id as FK to voice_call_logs
    - summary, sentiment, key_points, lead_extraction
    - raw_analysis for full LLM response
    - analysis_cost for cost tracking
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

    def _prepare_jsonb(self, data: Union[str, dict, list, None]) -> Optional[Json]:
        """
        Prepare data for JSONB storage.
        
        Args:
            data: Can be a dict, list, JSON string, or None
            
        Returns:
            Json wrapper for psycopg2 JSONB, or None
        """
        if data is None:
            return None
        
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return Json(parsed)
            except (json.JSONDecodeError, TypeError):
                return Json({"raw": data})
        
        if isinstance(data, (dict, list)):
            return Json(data)
        
        return None

    # =========================================================================
    # CREATE
    # =========================================================================

    async def create_analysis(
        self,
        call_log_id: str,
        tenant_id: str,
        *,
        summary: Optional[str] = None,
        sentiment: Optional[str] = None,
        key_points: Optional[list] = None,
        lead_extraction: Optional[dict] = None,
        raw_analysis: Optional[dict] = None,
        analysis_cost: Optional[float] = None,
    ) -> Optional[str]:
        """
        Create a new call analysis record.
        
        Args:
            call_log_id: UUID of the call log (FK)
            tenant_id: UUID of the tenant for multi-tenancy
            summary: LLM-generated call summary
            sentiment: Sentiment category ('positive', 'negative', 'neutral')
            key_points: List of extracted key points
            lead_extraction: Extracted lead info (name, email, phone)
            raw_analysis: Full LLM response data
            analysis_cost: Cost of the analysis in USD
            
        Returns:
            analysis_id (UUID string) if successful, None otherwise
        """
        if not call_log_id or not tenant_id:
            logger.error("call_log_id and tenant_id are required for create_analysis")
            return None
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {FULL_TABLE}
                        (tenant_id, call_log_id, summary, sentiment,
                         key_points, lead_extraction, raw_analysis,
                         analysis_cost, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            tenant_id,
                            call_log_id,
                            summary,
                            sentiment,
                            self._prepare_jsonb(key_points),
                            self._prepare_jsonb(lead_extraction),
                            self._prepare_jsonb(raw_analysis),
                            analysis_cost,
                            datetime.utcnow(),
                            datetime.utcnow(),
                        )
                    )
                    analysis_id = cur.fetchone()[0]
                    conn.commit()
                    
                    logger.info(
                        "Created analysis: id=%s, call_log_id=%s",
                        analysis_id,
                        call_log_id
                    )
                    return str(analysis_id)

        except Exception as exc:
            logger.error(
                "Failed to create analysis for call %s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return None

    async def upsert_analysis(
        self,
        call_log_id: str,
        tenant_id: str,
        *,
        summary: Optional[str] = None,
        sentiment: Optional[str] = None,
        key_points: Optional[list] = None,
        lead_extraction: Optional[dict] = None,
        raw_analysis: Optional[dict] = None,
        analysis_cost: Optional[float] = None,
    ) -> Optional[str]:
        """
        Create or update analysis for a call.
        
        If analysis exists for call_log_id, updates it.
        Otherwise creates a new record.
        
        Returns:
            analysis_id (UUID string) if successful, None otherwise
        """
        # Check if exists
        existing = await self.get_analysis_by_call_id(call_log_id)
        
        if existing:
            # Update existing
            success = await self.update_analysis(
                existing["id"],
                summary=summary,
                sentiment=sentiment,
                key_points=key_points,
                lead_extraction=lead_extraction,
                raw_analysis=raw_analysis,
                analysis_cost=analysis_cost,
            )
            return str(existing["id"]) if success else None
        else:
            # Create new
            return await self.create_analysis(
                call_log_id=call_log_id,
                tenant_id=tenant_id,
                summary=summary,
                sentiment=sentiment,
                key_points=key_points,
                lead_extraction=lead_extraction,
                raw_analysis=raw_analysis,
                analysis_cost=analysis_cost,
            )

    # =========================================================================
    # READ
    # =========================================================================

    async def get_analysis_by_id(self, analysis_id: str) -> Optional[dict]:
        """
        Get analysis by primary key.
        
        Args:
            analysis_id: UUID of the analysis record
            
        Returns:
            Analysis dict if found, None otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id, tenant_id, call_log_id, summary, sentiment,
                               key_points, lead_extraction, raw_analysis,
                               analysis_cost, created_at, updated_at
                        FROM {FULL_TABLE}
                        WHERE id = %s
                        """,
                        (analysis_id,)
                    )
                    result = cur.fetchone()
                    return dict(result) if result else None

        except Exception as exc:
            logger.error(
                "Failed to get analysis by id=%s: %s",
                analysis_id,
                exc,
                exc_info=True
            )
            return None

    async def get_analysis_by_call_id(self, call_log_id: str) -> Optional[dict]:
        """
        Get analysis by call_log_id (1:1 relationship).
        
        Args:
            call_log_id: UUID of the call log
            
        Returns:
            Analysis dict if found, None otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id, tenant_id, call_log_id, summary, sentiment,
                               key_points, lead_extraction, raw_analysis,
                               analysis_cost, created_at, updated_at
                        FROM {FULL_TABLE}
                        WHERE call_log_id = %s
                        """,
                        (call_log_id,)
                    )
                    result = cur.fetchone()
                    return dict(result) if result else None

        except Exception as exc:
            logger.error(
                "Failed to get analysis for call %s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return None

    # =========================================================================
    # UPDATE
    # =========================================================================

    async def update_analysis(
        self,
        analysis_id: str,
        *,
        summary: Optional[str] = None,
        sentiment: Optional[str] = None,
        key_points: Optional[list] = None,
        lead_extraction: Optional[dict] = None,
        raw_analysis: Optional[dict] = None,
        analysis_cost: Optional[float] = None,
    ) -> bool:
        """
        Update an existing analysis record.
        
        Only provided fields are updated.
        
        Returns:
            True if successful, False otherwise
        """
        updates = []
        params = []
        
        if summary is not None:
            updates.append("summary = %s")
            params.append(summary)
        
        if sentiment is not None:
            updates.append("sentiment = %s")
            params.append(sentiment)
        
        if key_points is not None:
            updates.append("key_points = %s")
            params.append(self._prepare_jsonb(key_points))
        
        if lead_extraction is not None:
            updates.append("lead_extraction = %s")
            params.append(self._prepare_jsonb(lead_extraction))
        
        if raw_analysis is not None:
            updates.append("raw_analysis = %s")
            params.append(self._prepare_jsonb(raw_analysis))
        
        if analysis_cost is not None:
            updates.append("analysis_cost = %s")
            params.append(analysis_cost)
        
        if not updates:
            logger.warning("update_analysis called with no updates")
            return True
        
        updates.append("updated_at = %s")
        params.append(datetime.utcnow())
        params.append(analysis_id)
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {FULL_TABLE}
                        SET {', '.join(updates)}
                        WHERE id = %s
                        """,
                        tuple(params)
                    )
                    conn.commit()
                    return cur.rowcount > 0

        except Exception as exc:
            logger.error(
                "Failed to update analysis %s: %s",
                analysis_id,
                exc,
                exc_info=True
            )
            return False

    # =========================================================================
    # DELETE
    # =========================================================================

    async def delete_analysis_by_call_id(self, call_log_id: str) -> bool:
        """
        Delete analysis for a specific call.
        
        Used for re-running analysis.
        
        Returns:
            True if deleted, False otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        DELETE FROM {FULL_TABLE}
                        WHERE call_log_id = %s
                        """,
                        (call_log_id,)
                    )
                    conn.commit()
                    deleted = cur.rowcount > 0
                    if deleted:
                        logger.info("Deleted analysis for call %s", call_log_id)
                    return deleted

        except Exception as exc:
            logger.error(
                "Failed to delete analysis for call %s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return False

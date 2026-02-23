"""
Database storage for call recordings and transcriptions.

Updated for dynamic schema (Phase 12):
- Table: SCHEMA.voice_call_logs
- Column renames: target→lead_id, agent→agent_id, voice→voice_id
- transcriptions→transcripts, call_recording_url→recording_url
- call_duration→duration_seconds, call_type→direction
- Added: tenant_id (required), to_country_code, to_base_number
- Moved to metadata JSONB: job_id, room_name, added_context
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional, Union

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

# Import connection pool manager
from db.connection_pool import get_db_connection, return_connection, USE_CONNECTION_POOLING
# Import centralized DB config (respects USE_LOCAL_DB toggle)
from db.db_config import get_db_config, validate_db_config
# Import schema constants for configurable schema name
from db.schema_constants import SCHEMA, CALL_LOGS_FULL, BILLING_PRICING_CATALOG_FULL

load_dotenv()

logger = logging.getLogger(__name__)

# Table constants - use centralized schema
TABLE = "voice_call_logs"
FULL_TABLE = CALL_LOGS_FULL


class DatabaseError(Exception):
    """Raised when a database operation fails due to connection or query issues"""
    pass


def _split_phone_number(phone: str | None) -> tuple[str | None, int | None]:
    """
    Split phone number into country code and base number using E.164 rules.
    
    Implements expert logic:
    - Cleans formatting
    - Normalizes international exit codes (00, 011 → +)
    - Smart heuristics for bare local numbers
    
    Args:
        phone: Phone number string (e.g., "+919876543210", "919876543210")
        
    Returns:
        Tuple of (country_code, base_number) e.g., ("+91", 9876543210)
    """
    if not phone:
        return None, None
    
    # Clean the phone number - keep only digits and leading +
    cleaned = str(phone).strip()
    if cleaned.startswith('+'):
        cleaned = '+' + re.sub(r'[^\d]', '', cleaned[1:])
    else:
        cleaned = re.sub(r'[^\d]', '', cleaned)
    
    # Normalize international exit codes (00, 011 → +)
    cleaned = re.sub(r'^(00|011)', '+', cleaned)
    
    if not cleaned or cleaned == '+':
        return None, None
    
    # Known country code patterns (longer prefixes first to avoid false matches)
    # IMPORTANT: 971 must come before 91, and all before 1
    country_codes = [
        ('+971', 3),  # UAE (must be before India)
        ('+91', 2),   # India
        ('+44', 2),   # UK
        ('+65', 2),   # Singapore
        ('+61', 2),   # Australia
        ('+86', 2),   # China
        ('+1', 1),    # USA/Canada (LAST due to short prefix)
    ]
    
    # Handle +XX format - numbers starting with + are already normalized
    if cleaned.startswith('+'):
        for prefix, length in country_codes:
            if cleaned.startswith(prefix):
                base = cleaned[len(prefix):]
                if base.isdigit() and len(base) >= 6:
                    return prefix, int(base)
        
        # Generic: try +XX (2 digits) then +XXX (3 digits)
        for cc_len in [2, 3, 4]:
            if len(cleaned) > cc_len + 6:
                cc = cleaned[:cc_len + 1]  # includes +
                base = cleaned[cc_len + 1:]
                if base.isdigit():
                    return cc, int(base)
        
        # Fallback for + numbers
        return cleaned[:4], int(cleaned[4:]) if len(cleaned) > 4 and cleaned[4:].isdigit() else None
    
    # PRIORITY 1: Handle 0-prefix for India (11 digits: 0 + 10) and UAE (10 digits: 0 + 9)
    # This MUST come BEFORE country code matching to avoid false positives
    if cleaned.startswith('0'):
        if len(cleaned) == 11:
            # India: 0 + 10 digit number
            base = cleaned[1:]
            if base.isdigit():
                return '+91', int(base)
        elif len(cleaned) == 10:
            # UAE: 0 + 9 digit number
            base = cleaned[1:]
            if base.isdigit():
                return '+971', int(base)
    
    # PRIORITY 2: No + prefix - detect by country code digits (longer prefixes first)
    for prefix, length in country_codes:
        digits_prefix = prefix[1:]  # Remove + 
        if cleaned.startswith(digits_prefix) and len(cleaned) >= length + 8:
            base = cleaned[length:]
            # USA-specific validation: US numbers don't start with 0 after country code
            if prefix == '+1' and base.startswith('0'):
                continue  # Skip - not a valid US number
            return prefix, int(base)
    
    # PRIORITY 3: Smart heuristics for bare local numbers (Expert Logic)
    if cleaned.isdigit():
        if len(cleaned) == 9:
            # 9 digits → UAE national number
            return '+971', int(cleaned)
        elif len(cleaned) == 10:
            # 10 digits → ambiguous between India and US
            # Heuristic: India mobile numbers typically start with 6-9
            if cleaned[0] in '6789':
                return '+91', int(cleaned)
            else:
                # US/Canada
                return '+1', int(cleaned)
        else:
            # Short number - no country code detectable
            return None, int(cleaned)
    
    return None, None


class CallStorage:
    """
    Handles database operations for call recordings and transcriptions.
    
    Uses dynamic voice_call_logs schema with:
    - tenant_id (required) - multi-tenancy isolation
    - lead_id (UUID FK) - replaces old target/target_type
    - metadata (JSONB) - for job_id, room_name, added_context
    """

    def __init__(self):
        # Use centralized config (respects USE_LOCAL_DB toggle)
        self.db_config = get_db_config()
        
        # Validate required environment variables
        is_valid, error_message = validate_db_config()
        if not is_valid:
            raise ValueError(error_message)

    def _get_connection(self):
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
        
        if isinstance(data, (dict, list)):
            return Json(data)
        
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return Json(parsed)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON string, storing as raw text in object")
                return Json({"raw_text": data})
        
        return None

    def _build_metadata(
        self,
        job_id: str | None = None,
        room_name: str | None = None,
        added_context: str | None = None,
        **extra
    ) -> dict:
        """Build metadata JSONB object from individual fields."""
        metadata = {}
        if job_id:
            metadata["job_id"] = job_id
        if room_name:
            metadata["room_name"] = room_name
        if added_context:
            metadata["added_context"] = added_context
        metadata.update(extra)
        return metadata

    # =========================================================================
    # CREATE
    # =========================================================================

    async def create_call_log(
        self,
        tenant_id: str,
        to_number: str,
        *,
        lead_id: Optional[str] = None,
        agent_id: Optional[int] = None,
        voice_id: Optional[str] = None,
        direction: str = "outbound",
        from_number_id: Optional[str] = None,
        initiated_by_user_id: Optional[str] = None,
        job_id: Optional[str] = None,
        room_name: Optional[str] = None,
        added_context: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create a new call log entry with complete metadata.
        
        Args:
            tenant_id: Required tenant UUID for multi-tenancy
            to_number: Phone number being called
            lead_id: UUID of lead (optional, FK to leads)
            agent_id: Agent ID (bigint FK to voice_agents)
            voice_id: Voice UUID (FK to voice_agent_voices)
            direction: 'inbound' or 'outbound'
            from_number_id: UUID of calling number (FK to voice_agent_numbers)
            initiated_by_user_id: UUID of user who initiated
            job_id: Internal job tracking ID (stored in metadata)
            room_name: LiveKit room name (stored in metadata)
            added_context: Extra context (stored in metadata)
        
        Returns:
            call_log_id (UUID string) if successful, None otherwise
        """
        if not tenant_id:
            logger.error("tenant_id is required for create_call_log")
            return None
        
        try:
            # Split phone number into country code and base number
            country_code, base_number = _split_phone_number(to_number)
            
            if base_number is None:
                logger.error(f"Invalid phone number format: {to_number}")
                return None
            
            # Build metadata object - include voice_id here since column doesn't exist in voice_call_logs
            metadata = self._build_metadata(
                job_id=job_id,
                room_name=room_name,
                added_context=added_context,
            )
            # Store voice_id in metadata (column doesn't exist in voice_call_logs)
            if voice_id:
                metadata["voice_id"] = voice_id
            
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {FULL_TABLE}
                        (tenant_id, lead_id, to_country_code, to_base_number,
                         agent_id, direction, from_number_id,
                         initiated_by_user_id, status, started_at, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            tenant_id,
                            lead_id,
                            country_code,
                            base_number,
                            agent_id,
                            direction,
                            from_number_id,
                            initiated_by_user_id,
                            "in_queue",
                            datetime.utcnow(),
                            self._prepare_jsonb(metadata),
                        )
                    )
                    call_log_id = cur.fetchone()[0]
                    conn.commit()
                    
                    logger.info(
                        "Created call log: id=%s, tenant=%s, direction=%s",
                        call_log_id,
                        tenant_id,
                        direction
                    )
                    return str(call_log_id)

        except Exception as exc:
            logger.error(
                "Failed to create call log: %s",
                exc,
                exc_info=True
            )
            return None

    # =========================================================================
    # UPDATE
    # =========================================================================

    async def update_call_recording(
        self,
        call_log_id: str,
        recording_url: str,
        transcripts: Union[str, dict, None] = None,
    ) -> bool:
        """
        Update call with recording URL and transcripts.
        
        Args:
            call_log_id: UUID of the call log entry
            recording_url: Storage URL of the recording
            transcripts: Transcription as dict or JSON string (for JSONB)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {FULL_TABLE}
                        SET 
                            recording_url = %s,
                            transcripts = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            recording_url,
                            self._prepare_jsonb(transcripts),
                            datetime.utcnow(),
                            call_log_id
                        )
                    )
                    conn.commit()
                    
                    logger.info(
                        "Updated call recording: call_log_id=%s",
                        call_log_id
                    )
                    return True

        except Exception as exc:
            logger.error(
                "Failed to update call recording for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return False

    async def update_call_status(
        self,
        call_log_id: str,
        status: str,
        ended_at: Optional[datetime] = None,
        duration_seconds: Optional[int] = None,
        cost: Optional[float] = None,
        cost_breakdown: Optional[dict] = None,
    ) -> bool:
        """
        Update call status and completion data.
        
        Args:
            call_log_id: UUID of the call log entry
            status: New status (ongoing, ended, not reachable, declined, etc.)
            ended_at: When the call ended
            duration_seconds: Call duration in seconds (integer)
            cost: Call cost
            cost_breakdown: Detailed cost breakdown (JSONB)
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {FULL_TABLE}
                        SET 
                            status = %s,
                            ended_at = COALESCE(%s, ended_at),
                            duration_seconds = COALESCE(%s, duration_seconds),
                            cost = COALESCE(%s, cost),
                            cost_breakdown = COALESCE(%s, cost_breakdown),
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            status,
                            ended_at,
                            duration_seconds,
                            cost,
                            self._prepare_jsonb(cost_breakdown),
                            datetime.utcnow(),
                            call_log_id
                        )
                    )
                    conn.commit()
                    
                    logger.info(
                        "Updated call status: call_log_id=%s, status=%s",
                        call_log_id,
                        status
                    )
                    return True

        except Exception as exc:
            logger.error(
                "Failed to update call status for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return False

    async def update_call_metadata(
        self,
        call_log_id: str,
        **kwargs
    ) -> bool:
        """
        Update any call fields dynamically.
        
        Args:
            call_log_id: UUID of the call log entry
            **kwargs: Fields to update (e.g., status="ended", cost=0.05)
                      
        Supported fields:
            - status, recording_url, transcripts, duration_seconds
            - cost, cost_breakdown, ended_at, lead_id
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if not kwargs:
                logger.warning("No fields provided to update")
                return False
            
            # Map old column names to new for backwards compatibility
            column_map = {
                "transcriptions": "transcripts",
                "call_recording_url": "recording_url",
                "call_duration": "duration_seconds",
                "call_type": "direction",
                "agent": "agent_id",
                "voice": "voice_id",
            }
            
            # Build dynamic UPDATE query
            set_clauses = ["updated_at = %s"]
            params = [datetime.utcnow()]
            
            for key, value in kwargs.items():
                # Map old column name to new if needed
                column = column_map.get(key, key)
                
                set_clauses.append(f"{column} = %s")
                
                # Handle JSONB fields
                if column in ('transcripts', 'cost_breakdown', 'metadata'):
                    params.append(self._prepare_jsonb(value))
                else:
                    params.append(value)
            
            params.append(call_log_id)
            
            query = f"""
                UPDATE {FULL_TABLE}
                SET {', '.join(set_clauses)}
                WHERE id = %s
            """
            
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows_updated = cur.rowcount
                    conn.commit()
                    
                    if rows_updated > 0:
                        logger.info(
                            "Updated call metadata: call_log_id=%s, fields=%s",
                            call_log_id,
                            list(kwargs.keys())
                        )
                        return True
                    else:
                        logger.warning(f"No call found with id={call_log_id}")
                        return False

        except Exception as exc:
            logger.error(
                "Failed to update call metadata for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return False

    # =========================================================================
    # READ
    # =========================================================================

    async def get_call_by_id(self, call_log_id: str) -> Optional[dict]:
        """
        Get complete call log details by UUID.
        
        Args:
            call_log_id: The UUID of the call log record
            
        Returns:
            Call log dict if found, None otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT 
                            id, tenant_id, lead_id, to_country_code, to_base_number,
                            agent_id, direction, from_number_id,
                            initiated_by_user_id, status, recording_url, transcripts,
                            started_at, ended_at, duration_seconds,
                            cost, currency, cost_breakdown,
                            campaign_id, campaign_lead_id, campaign_step_id,
                            metadata, created_at, updated_at
                        FROM {FULL_TABLE}
                        WHERE id = %s
                        """,
                        (call_log_id,)
                    )
                    result = cur.fetchone()
                    return dict(result) if result else None

        except Exception as exc:
            logger.error(
                "Failed to get call by id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return None

    async def get_call_by_job_id(self, job_id: str) -> Optional[dict]:
        """
        Get call log by job_id (stored in metadata).
        
        Args:
            job_id: Job ID from API request
            
        Returns:
            Call log dict if found, None otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT 
                            id, tenant_id, lead_id, to_country_code, to_base_number,
                            agent_id, voice_id, direction, from_number_id,
                            initiated_by_user_id, status, recording_url, transcripts,
                            started_at, ended_at, duration_seconds,
                            cost, currency, cost_breakdown, metadata
                        FROM {FULL_TABLE}
                        WHERE metadata->>'job_id' = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (job_id,)
                    )
                    result = cur.fetchone()
                    
                    if not result:
                        logger.debug(f"No call found for job_id={job_id}")
                        return None
                    
                    return dict(result)

        except Exception as exc:
            logger.error(
                "Failed to get call by job_id=%s: %s",
                job_id,
                exc,
                exc_info=True
            )
            return None

    async def get_call_by_room_name(self, room_name: str) -> Optional[dict]:
        """
        Get call log by LiveKit room name (stored in metadata).
        
        Args:
            room_name: LiveKit room name
            
        Returns:
            Call log dict if found, None otherwise
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT 
                            id, tenant_id, lead_id, status, recording_url,
                            transcripts, started_at, ended_at, duration_seconds,
                            cost, metadata
                        FROM {FULL_TABLE}
                        WHERE metadata->>'room_name' = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (room_name,)
                    )
                    result = cur.fetchone()
                    return dict(result) if result else None

        except Exception as exc:
            logger.error(
                "Failed to get call by room_name=%s: %s",
                room_name,
                exc,
                exc_info=True
            )
            return None

    async def get_call_transcription(self, call_log_id: str) -> Optional[dict]:
        """Get transcripts JSONB for a call."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT transcripts
                        FROM {FULL_TABLE}
                        WHERE id = %s
                        """,
                        (call_log_id,)
                    )
                    result = cur.fetchone()
                    return result[0] if result else None

        except Exception as exc:
            logger.error(
                "Failed to get transcription for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return None

    async def get_call_recording_url(self, call_log_id: str) -> Optional[str]:
        """Get recording URL for a call."""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT recording_url
                        FROM {FULL_TABLE}
                        WHERE id = %s
                        """,
                        (call_log_id,)
                    )
                    result = cur.fetchone()
                    return result[0] if result else None

        except Exception as exc:
            logger.error(
                "Failed to get recording URL for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return None

    # =========================================================================
    # VALIDATION
    # =========================================================================

    async def validate_voice_id(self, voice_id: str) -> bool:
        """
        Validate that voice_id exists in voice_agent_voices table.
        
        Args:
            voice_id: UUID of the voice
            
        Returns:
            True if valid, False otherwise
        """
        if not voice_id:
            return False
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT 1 FROM {SCHEMA}.voice_agent_voices
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (voice_id,)
                    )
                    return cur.fetchone() is not None

        except Exception as exc:
            logger.error(
                "Failed to validate voice_id=%s: %s",
                voice_id,
                exc,
                exc_info=True
            )
            return False

    async def validate_lead_id(self, lead_id: str) -> bool:
        """
        Validate that lead_id exists in leads table.
        
        Args:
            lead_id: UUID of the lead
            
        Returns:
            True if valid, False otherwise
        """
        if not lead_id:
            return False
        
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT 1 FROM {SCHEMA}.leads
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (lead_id,)
                    )
                    return cur.fetchone() is not None

        except Exception as exc:
            logger.error(
                "Failed to validate lead_id=%s: %s",
                lead_id,
                exc,
                exc_info=True
            )
            return False

    # =========================================================================
    # USAGE TRACKING
    # =========================================================================

    async def save_call_usage(
        self, 
        call_log_id: str, 
        usage_records: list[dict],
        pricing_rates: list[dict] | None = None
    ) -> bool:
        """
        Save usage records for a call.
        
        Stores in voice_call_logs.cost_breakdown JSONB.
        
        Args:
            call_log_id: UUID of the call log
            usage_records: List of usage dicts (component, quantity, unit)
            pricing_rates: Optional pricing rates for cost calculation
            
        Returns:
            True if successful, False otherwise
        """
        # Guard: Validate call_log_id
        if not call_log_id or call_log_id == "None":
            logger.warning("save_call_usage called with invalid call_log_id: %s", call_log_id)
            return False
        
        try:
            # Calculate costs if pricing provided
            cost_breakdown = {
                "usage": usage_records,
                "rates": pricing_rates or [],
            }
            
            total_cost = 0.0
            if pricing_rates:
                for record in usage_records:
                    component = record.get("component", "")
                    quantity = record.get("quantity", 0)
                    
                    for rate in pricing_rates:
                        if rate.get("component") == component:
                            unit_cost = rate.get("cost_per_unit", 0)
                            total_cost += quantity * unit_cost
                            break
                
                cost_breakdown["total_calculated"] = total_cost
            
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {FULL_TABLE}
                        SET 
                            cost_breakdown = %s,
                            cost = COALESCE(cost, %s),
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            self._prepare_jsonb(cost_breakdown),
                            total_cost if total_cost > 0 else None,
                            datetime.utcnow(),
                            call_log_id
                        )
                    )
                    conn.commit()
                    
                    logger.info(
                        "Saved call usage: call_log_id=%s, records=%d",
                        call_log_id,
                        len(usage_records)
                    )
                    return True

        except Exception as exc:
            logger.error(
                "Failed to save usage for call_log_id=%s: %s",
                call_log_id,
                exc,
                exc_info=True
            )
            return False

    async def get_pricing_rates(self) -> list[dict]:
        """
        Fetch active pricing rates from billing_pricing_catalog table.
        
        Returns:
            List of pricing rate dicts with component, provider, model, unit, cost_per_unit
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # Use billing_pricing_catalog table
                    # Column mappings: category = component, unit_price = cost_per_unit
                    cur.execute(f"""
                        SELECT category, provider, model, unit, unit_price
                        FROM {BILLING_PRICING_CATALOG_FULL}
                        WHERE is_active = TRUE
                    """)
                    
                    rows = cur.fetchall()
                    return [
                        {
                            "component": row[0],  # category -> component
                            "provider": row[1],
                            "model": row[2],
                            "unit": row[3],
                            "cost_per_unit": float(row[4]) if row[4] else 0.0,
                        }
                        for row in rows
                    ]
                    
        except Exception as exc:
            logger.error("Failed to fetch pricing rates: %s", exc, exc_info=True)
            return []

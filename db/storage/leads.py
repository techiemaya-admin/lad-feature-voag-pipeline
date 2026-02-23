"""
Lead Storage Module.

Updated for dynamic schema (Phase 12):
- Table: leads
- ID is UUID (not bigint)
- Uses first_name/last_name instead of name
- Uses phone instead of lead_number
- Uses user_id instead of added_by
- Added: tenant_id (required), source, email, etc.
"""

import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

# Import connection pool manager (context manager pattern)
from db.connection_pool import get_db_connection
# Import centralized DB config (respects USE_LOCAL_DB toggle)
from db.db_config import get_db_config

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Schema and table constants
SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")
TABLE = "leads"
FULL_TABLE = f"{SCHEMA}.{TABLE}"


class LeadStorage:
    """
    Manages lead data in leads table.
    
    Key schema changes from old leads_voiceagent:
    - id: UUID (was bigint)
    - tenant_id: Required for multi-tenancy
    - first_name, last_name: Replaces single 'name' field
    - phone: Replaces 'lead_number'
    - user_id: Replaces 'added_by' (now UUID FK)
    """
    
    def __init__(self):
        """Initialize database connection using centralized config"""
        self.db_config = get_db_config()
    
    # Note: Uses get_db_connection() context manager for automatic cleanup
    # No manual _get_connection/_return_connection needed
    
    def _split_name(self, full_name: str | None) -> tuple[str | None, str | None]:
        """
        Split full name into first and last name.
        
        Args:
            full_name: Full name string
            
        Returns:
            Tuple of (first_name, last_name)
        """
        if not full_name:
            return None, None
        
        parts = full_name.strip().split(maxsplit=1)
        first_name = parts[0] if parts else None
        last_name = parts[1] if len(parts) > 1 else None
        return first_name, last_name
    
    def _combine_name(self, first_name: str | None, last_name: str | None) -> str | None:
        """Combine first and last name into full name."""
        parts = [n for n in [first_name, last_name] if n]
        return " ".join(parts) if parts else None

    def _normalize_phone(self, phone: str | None) -> str | None:
        """
        Normalize phone number to include country code using E.164 format.
        
        Implements deterministic rules matching expert reference:
        - Cleans formatting (parentheses, spaces, dashes)
        - Normalizes international exit codes (00, 011 → +)
        - Detects 0-prefix for UAE (10d) and India (11d)
        - Applies smart heuristics for bare local numbers
        
        Args:
            phone: Raw phone number string
            
        Returns:
            Normalized phone with country code (e.g., '+971506341191')
        """
        import re
        
        if not phone:
            return None
        
        # STEP 1: Clean the phone number - remove all non-digits except leading +
        cleaned = str(phone).strip()
        # Keep leading + if present, strip everything else
        if cleaned.startswith('+'):
            cleaned = '+' + re.sub(r'[^\d]', '', cleaned[1:])
        else:
            cleaned = re.sub(r'[^\d]', '', cleaned)
        
        # STEP 2: Normalize international exit codes (00, 011 → +)
        cleaned = re.sub(r'^(00|011)', '+', cleaned)
        
        if not cleaned or cleaned == '+':
            return None
        
        # STEP 3: If already has + prefix, validate and return
        if cleaned.startswith('+'):
            # Already normalized with country code
            return cleaned
        
        # Extract digits only for pattern matching
        digits = cleaned
        
        # PRIORITY 1: Handle 0-prefix for India (11 digits) and UAE (10 digits)
        # This MUST come before country code matching to avoid false positives
        if digits.startswith('0'):
            if len(digits) == 11:
                # India: 0 + 10 digit number
                return f"+91{digits[1:]}"
            elif len(digits) == 10:
                # UAE: 0 + 9 digit number
                return f"+971{digits[1:]}"
        
        # PRIORITY 2: Known country code patterns (longer prefixes first)
        # IMPORTANT: 971 must come before 91, which must come before 1
        country_codes = [
            ('971', '+971', 9),   # UAE: 971 + 9 digits = 12 total
            ('91', '+91', 10),    # India: 91 + 10 digits = 12 total
            ('44', '+44', 10),    # UK
            ('65', '+65', 8),     # Singapore
            ('61', '+61', 9),     # Australia
            ('1', '+1', 10),      # USA/Canada: 1 + 10 digits = 11 total (LAST due to short prefix)
        ]
        
        # Check if starts with country code (without +)
        for prefix, cc_with_plus, expected_base_len in country_codes:
            if digits.startswith(prefix):
                base = digits[len(prefix):]
                # STRICT check: base must be exactly the expected length
                if len(base) == expected_base_len:
                    return f"{cc_with_plus}{base}"
        
        # PRIORITY 3: Smart heuristics for bare local numbers (Expert Logic)
        # These are numbers without country code or trunk prefix
        if len(digits) == 9:
            # 9 digits → UAE national number
            return f"+971{digits}"
        elif len(digits) == 10:
            # 10 digits → ambiguous between India and US
            # Heuristic: India mobile numbers typically start with 6-9
            if digits[0] in '6789':
                return f"+91{digits}"
            else:
                # US/Canada or other India numbers
                return f"+1{digits}"
        
        # FALLBACK: If >= 10 digits but no pattern matched, add + prefix
        # (This handles edge cases but should rarely be hit with above logic)
        if len(digits) >= 10:
            return f"+{digits}"
        
        # Short number (< 10 digits) - return as-is (could be extension, short code, etc.)
        return phone

    # =========================================================================
    # READ
    # =========================================================================

    async def get_lead_by_phone(
        self,
        phone_number: str,
        tenant_id: str | None = None
    ) -> Optional[Dict]:
        """
        Get lead by phone number.
        
        Args:
            phone_number: Phone number to search for
            tenant_id: Optional tenant ID to scope search
            
        Returns:
            Lead dict if found, None otherwise
        """
        try:
            with get_db_connection(self.db_config) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    if tenant_id:
                        cursor.execute(
                            f"""
                            SELECT id, tenant_id, first_name, last_name, phone, email,
                                   user_id, source, status, notes, custom_fields,
                                   created_at, updated_at
                            FROM {FULL_TABLE}
                            WHERE phone = %s AND tenant_id = %s
                            LIMIT 1
                            """,
                            (phone_number, tenant_id)
                        )
                    else:
                        cursor.execute(
                            f"""
                            SELECT id, tenant_id, first_name, last_name, phone, email,
                                   user_id, source, status, notes, custom_fields,
                                   created_at, updated_at
                            FROM {FULL_TABLE}
                            WHERE phone = %s
                            LIMIT 1
                            """,
                            (phone_number,)
                        )
                    
                    result = cursor.fetchone()
                    
                    if result:
                        lead = dict(result)
                        # Add 'name' field for backwards compatibility
                        lead['name'] = self._combine_name(lead.get('first_name'), lead.get('last_name'))
                        # Add 'lead_number' alias for backwards compatibility
                        lead['lead_number'] = lead.get('phone')
                        logger.info(f"Found existing lead: id={lead['id']}, phone={phone_number}")
                        return lead
                    else:
                        logger.debug(f"No lead found for phone: {phone_number}")
                        return None
                
        except Exception as e:
            logger.error(f"Error getting lead by phone {phone_number}: {e}", exc_info=True)
            return None

    async def get_lead_by_id(self, lead_id: str) -> Optional[Dict]:
        """
        Get lead by UUID.
        
        Args:
            lead_id: UUID of the lead
            
        Returns:
            Lead dict if found, None otherwise
        """
        try:
            with get_db_connection(self.db_config) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        f"""
                        SELECT id, tenant_id, first_name, last_name, phone, email,
                               user_id, source, status, stage, priority, notes,
                               company_name, title, location, tags, custom_fields,
                               created_at, updated_at
                        FROM {FULL_TABLE}
                        WHERE id = %s
                        """,
                        (lead_id,)
                    )
                    
                    result = cursor.fetchone()
                    
                    if result:
                        lead = dict(result)
                        lead['name'] = self._combine_name(lead.get('first_name'), lead.get('last_name'))
                        return lead
                    return None
                
        except Exception as e:
            logger.error(f"Error getting lead by id {lead_id}: {e}", exc_info=True)
            return None

    # =========================================================================
    # CREATE
    # =========================================================================

    async def create_lead(
        self,
        tenant_id: str,
        phone_number: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        name: str | None = None,  # For backwards compatibility
        email: str | None = None,
        user_id: str | None = None,
        source: str | None = None,
        notes: str | None = None,
    ) -> Optional[Dict]:
        """
        Create a new lead.
        
        Args:
            tenant_id: Required tenant UUID
            phone_number: Phone number for the lead
            first_name: First name
            last_name: Last name
            name: Full name (split into first/last if provided)
            email: Email address
            user_id: UUID of user who created
            source: Lead source (e.g., 'voice_agent', 'manual')
            notes: Notes
            
        Returns:
            Lead record dict if successful, None otherwise
        """
        if not tenant_id:
            logger.error("tenant_id is required for create_lead")
            return None
        
        # Normalize phone number (0-prefix -> +971/+91)
        normalized_phone = self._normalize_phone(phone_number)
        if not normalized_phone:
            logger.error(f"Invalid phone number: {phone_number}")
            return None
        
        # Handle name splitting
        if name and not first_name:
            first_name, last_name = self._split_name(name)
        
        try:
            with get_db_connection(self.db_config) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        f"""
                        INSERT INTO {FULL_TABLE} 
                        (tenant_id, phone, first_name, last_name, email, user_id, source, notes, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, tenant_id, first_name, last_name, phone, email, user_id, source, notes
                        """,
                        (
                            tenant_id,
                            normalized_phone,
                            first_name,
                            last_name,
                            email,
                            user_id,
                            source or 'voice_agent',
                            notes,
                            'new'
                        )
                    )
                    
                    result = cursor.fetchone()
                    conn.commit()
                    
                    if result:
                        lead = dict(result)
                        lead['name'] = self._combine_name(lead.get('first_name'), lead.get('last_name'))
                        lead['lead_number'] = lead.get('phone')
                        logger.info(f"Created new lead: id={lead['id']}, phone={normalized_phone}")
                        return lead
                    
                    logger.warning(f"No lead returned when creating phone={phone_number}")
                    return None
            
        except psycopg2.IntegrityError as e:
            logger.warning(f"Lead with phone {phone_number} may already exist: {e}")
            # Try to get existing lead
            return await self.get_lead_by_phone(phone_number, tenant_id)
            
        except Exception as e:
            logger.error(f"Error creating lead for phone {phone_number}: {e}", exc_info=True)
            return None

    async def find_or_create_lead(
        self,
        tenant_id: str,
        phone_number: str,
        *,
        name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        user_id: str | None = None,
        source: str | None = None,
    ) -> Optional[Dict]:
        """
        Find existing lead by phone number or create new one.
        
        Args:
            tenant_id: Required tenant UUID
            phone_number: Phone number to search for or create
            name: Full name (for new leads or to update missing name)
            first_name: First name (takes precedence over name)
            last_name: Last name
            email: Email address
            user_id: UUID of user (for new leads)
            source: Lead source
            
        Returns:
            Lead record dict if successful, None otherwise
        """
        if not tenant_id:
            logger.error("tenant_id is required for find_or_create_lead")
            return None
        
        # Normalize phone number (0-prefix -> +971/+91) for consistent lookup
        normalized_phone = self._normalize_phone(phone_number)
        if not normalized_phone:
            logger.error(f"Invalid phone number: {phone_number}")
            return None
        
        try:
            # First try to find existing lead with normalized phone
            existing_lead = await self.get_lead_by_phone(normalized_phone, tenant_id)
            
            if existing_lead:
                logger.info(f"Using existing lead: id={existing_lead['id']}, phone={phone_number}")
                
                # Update name if missing
                has_name = bool(existing_lead.get('first_name') or existing_lead.get('last_name'))
                if (name or first_name) and not has_name:
                    fn = first_name
                    ln = last_name
                    if not fn and name:
                        fn, ln = self._split_name(name)
                    
                    if fn:
                        updated = await self._set_name_if_missing(
                            existing_lead['id'],
                            first_name=fn,
                            last_name=ln
                        )
                        if updated:
                            existing_lead['first_name'] = fn
                            existing_lead['last_name'] = ln
                            existing_lead['name'] = self._combine_name(fn, ln)
                
                return existing_lead
            
            # If not found, create new lead with normalized phone
            logger.info(f"Creating new lead for phone: {normalized_phone}")
            return await self.create_lead(
                tenant_id=tenant_id,
                phone_number=normalized_phone,
                first_name=first_name,
                last_name=last_name,
                name=name,
                email=email,
                user_id=user_id,
                source=source,
            )
            
        except Exception as e:
            logger.error(f"Error in find_or_create_lead for {phone_number}: {e}", exc_info=True)
            return None

    # =========================================================================
    # UPDATE
    # =========================================================================


    async def _set_name_if_missing(
        self,
        lead_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> bool:
        """Set the lead name only when it is currently null or blank."""
        if not first_name and not last_name:
            return False

        try:
            with get_db_connection(self.db_config) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {FULL_TABLE}
                        SET first_name = COALESCE(%s, first_name),
                            last_name = COALESCE(%s, last_name),
                            updated_at = %s
                        WHERE id = %s
                        AND (first_name IS NULL OR LENGTH(TRIM(first_name)) = 0)
                        AND (last_name IS NULL OR LENGTH(TRIM(last_name)) = 0)
                        RETURNING id
                        """,
                        (first_name, last_name, datetime.utcnow(), lead_id)
                    )
                    updated = cursor.fetchone() is not None
                    conn.commit()
                    if updated:
                        logger.info(f"Updated missing name for lead id={lead_id}")
                    return updated
        except Exception as e:
            logger.error(f"Error updating missing name for lead {lead_id}: {e}", exc_info=True)
            return False
    
    async def update_lead_info(
        self,
        lead_id: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        notes: str | None = None,
        status: str | None = None,
        custom_fields: dict | None = None,
    ) -> bool:
        """
        Update lead information.
        
        Args:
            lead_id: Lead UUID to update
            first_name: First name
            last_name: Last name
            name: Full name (split into first/last)
            email: Email address
            phone: Phone number
            notes: Notes
            status: Lead status
            custom_fields: Custom fields JSONB
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Handle name splitting
            if name and not first_name:
                first_name, last_name = self._split_name(name)
            
            # Build dynamic UPDATE
            updates = ["updated_at = %s"]
            params: list[Any] = [datetime.utcnow()]
            
            field_map = {
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'phone': phone,
                'notes': notes,
                'status': status,
            }
            
            for col, val in field_map.items():
                if val is not None:
                    updates.append(f"{col} = %s")
                    params.append(val)
            
            if custom_fields is not None:
                updates.append("custom_fields = %s")
                params.append(Json(custom_fields))
            
            if len(updates) == 1:
                logger.warning("No fields to update")
                return False
            
            params.append(lead_id)
            
            query = f"""
                UPDATE {FULL_TABLE}
                SET {', '.join(updates)}
                WHERE id = %s
            """
            
            with get_db_connection(self.db_config) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params)
                    rows_updated = cursor.rowcount
                    conn.commit()
                    
                    if rows_updated > 0:
                        logger.info(f"Updated lead: id={lead_id}")
                        return True
                    else:
                        logger.warning(f"No lead found with id={lead_id}")
                        return False
                
        except Exception as e:
            logger.error(f"Error updating lead {lead_id}: {e}", exc_info=True)
            return False
    
    def assign_lead_to_user_if_unassigned(
        self,
        lead_id: str,
        user_id: str,
    ) -> bool:
        """
        Assign a lead to a user if currently unassigned.
        
        Args:
            lead_id: Lead UUID to update
            user_id: User UUID to assign
        
        Returns:
            True if assigned (or already assigned), False on error
        """
        if not lead_id or not user_id:
            logger.warning("lead_id and user_id required for assignment")
            return False
        
        try:
            with get_db_connection(self.db_config) as conn:
                with conn.cursor() as cursor:
                    # Only update if assigned_user_id is NULL
                    cursor.execute(f"""
                        UPDATE {FULL_TABLE}
                        SET assigned_user_id = %s, assigned_at = NOW(), updated_at = NOW()
                        WHERE id = %s AND assigned_user_id IS NULL
                    """, (user_id, lead_id))
                    rows_updated = cursor.rowcount
                    conn.commit()
                    
                    if rows_updated > 0:
                        logger.info(f"Assigned lead {lead_id} to user {user_id}")
                        return True
                    else:
                        # Either already assigned or lead not found
                        logger.debug(f"Lead {lead_id} already assigned or not found")
                        return True  # Return True since it's not an error
        except Exception as e:
            logger.error(f"Error assigning lead {lead_id} to user: {e}", exc_info=True)
            return False

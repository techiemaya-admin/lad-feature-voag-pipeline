
"""
Vertical Routing Module
=======================

Routes extracted lead data to vertical-specific tables based on tenant slug.

Vertical Detection:
- Reads tenant slug from tenants table
- Parses slug format: "{vertical}_{client}" or "{vertical}-{client}"
- Routes to appropriate extractor/storage

Supported Verticals:
- education: Stores in education_students (uses analysis.lad_dev.StudentExtractor)
- realestate: (future) realestate_leads  
- general: Only stores in voice_call_analysis.lead_extraction

Usage:
    from utils.vertical_routing import route_lead_extraction
    
    # After main post-call analysis
    await route_lead_extraction(
        call_log_id=call_log_id,
        tenant_id=tenant_id,
        conversation=conversation,  # List of {"role": "...", "message": "..."}
        lead_id=lead_id,
        db_config=db_config
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from db.schema_constants import SCHEMA, STUDENTS_FULL, CALL_ANALYSIS_FULL

# Import vertical detection
try:
    from utils.tenant_utils import get_vertical_from_tenant_id_sync
    TENANT_UTILS_AVAILABLE = True
except ImportError:
    TENANT_UTILS_AVAILABLE = False
    logger.warning("tenant_utils not available - vertical routing disabled")

# Import student extractor for education vertical
try:
    from analysis.lad_dev import StudentExtractor
    STUDENT_EXTRACTOR_AVAILABLE = True
except ImportError:
    STUDENT_EXTRACTOR_AVAILABLE = False
    logger.debug("StudentExtractor not available - education vertical routing disabled")


class VerticalRoutingResult:
    """Result of vertical routing operation."""
    
    def __init__(
        self,
        vertical: str,
        routed: bool,
        target_table: Optional[str] = None,
        error: Optional[str] = None,
        extracted_data: Optional[Dict[str, Any]] = None
    ):
        self.vertical = vertical
        self.routed = routed
        self.target_table = target_table
        self.error = error
        self.extracted_data = extracted_data
    
    def __repr__(self):
        return f"VerticalRoutingResult(vertical={self.vertical}, routed={self.routed}, table={self.target_table})"


def _format_conversation_for_extraction(conversation: list[Dict[str, Any]]) -> str:
    """
    Format conversation list to text for extraction.
    
    Args:
        conversation: List of {"role": "agent/user", "message": "..."}
        
    Returns:
        Formatted text for LLM extraction
    """
    if not conversation:
        return ""
    
    lines = []
    for turn in conversation:
        role = turn.get("role", "unknown").capitalize()
        # Map roles to expected format
        if role.lower() == "agent":
            role = "Agent"
        elif role.lower() == "user":
            role = "User"
        
        message = turn.get("message", "")
        if message:
            lines.append(f"{role}: {message}")
    
    return "\n".join(lines)


async def route_lead_extraction(
    call_log_id: str,
    tenant_id: str,
    conversation: list[Dict[str, Any]],
    lead_id: Optional[str] = None,
    db_config: Optional[Dict[str, Any]] = None
) -> VerticalRoutingResult:
    """
    Route lead extraction to vertical-specific storage based on tenant.
    
    This function should be called AFTER the main post-call analysis has stored
    the generic lead_extraction in voice_call_analysis table.
    
    Args:
        call_log_id: UUID of the call log
        tenant_id: UUID of the tenant
        conversation: List of conversation turns [{"role": "agent/user", "message": "..."}]
        lead_id: Optional lead UUID to link with extracted data
        db_config: Database configuration dict
        
    Returns:
        VerticalRoutingResult with routing outcome
    """
    if not tenant_id:
        logger.debug("No tenant_id provided, skipping vertical routing")
        return VerticalRoutingResult(
            vertical="unknown",
            routed=False,
            error="No tenant_id provided"
        )
    
    if not TENANT_UTILS_AVAILABLE:
        logger.warning("tenant_utils not available, skipping vertical routing")
        return VerticalRoutingResult(
            vertical="unknown",
            routed=False,
            error="tenant_utils not available"
        )
    
    # Determine vertical from tenant
    vertical = get_vertical_from_tenant_id_sync(tenant_id)
    logger.info(f"Vertical routing: tenant_id={tenant_id}, vertical={vertical}")
    
    if vertical == "education":
        return await _route_education_vertical(
            call_log_id=call_log_id,
            tenant_id=tenant_id,
            conversation=conversation,
            lead_id=lead_id,
            db_config=db_config
        )
    
    elif vertical == "realestate":
        # Future: Implement realestate vertical routing
        logger.info(f"Realestate vertical detected but not yet implemented")
        return VerticalRoutingResult(
            vertical="realestate",
            routed=False,
            error="Realestate vertical not yet implemented"
        )
    
    else:
        # General vertical - extract general lead info and update analysis table
        return await _route_general_vertical(
            call_log_id=call_log_id,
            tenant_id=tenant_id,
            conversation=conversation,
            lead_id=lead_id,
            db_config=db_config
        )


async def _route_education_vertical(
    call_log_id: str,
    tenant_id: str,
    conversation: list[Dict[str, Any]],
    lead_id: Optional[str],
    db_config: Optional[Dict[str, Any]]
) -> VerticalRoutingResult:
    """
    Route to education vertical - extract student info and store in education_students.
    """
    if not STUDENT_EXTRACTOR_AVAILABLE:
        logger.warning("StudentExtractor not available for education vertical")
        return VerticalRoutingResult(
            vertical="education",
            routed=False,
            error="StudentExtractor not available"
        )
    
    if not db_config:
        logger.warning("No db_config provided for education vertical routing")
        return VerticalRoutingResult(
            vertical="education",
            routed=False,
            error="No db_config provided"
        )
    
    try:
        # Format conversation for extraction
        conversation_text = _format_conversation_for_extraction(conversation)
        
        if not conversation_text:
            logger.info("Empty conversation, skipping education extraction")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="Empty conversation"
            )
        
        # Extract student information using Gemini (async)
        extractor = StudentExtractor()
        student_info = await extractor.extract_student_information(conversation_text)
        
        logger.debug(f"student_info returned: {student_info}")
        
        # More explicit validation - check if it's None or an empty dict
        if student_info is None:
            logger.warning(f"student_info is None for call {call_log_id}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="extract_student_information returned None"
            )
        
        if not isinstance(student_info, dict):
            logger.warning(f"student_info is not a dict for call {call_log_id}, got: {type(student_info)}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="extract_student_information returned non-dict"
            )
            
        # Check if student_info is an empty dict or has no useful data
        has_main_fields = any(student_info.get(field) for field in ['student_parent_name', 'parent_designation', 'program_interested_in', 'country_interested', 'intake_year', 'intake_month'])
        metadata = student_info.get('metadata', {})
        has_metadata = bool(metadata)
        
        logger.debug(f"Validation: has_main_fields={has_main_fields}, has_metadata={has_metadata}")
        
        if not has_main_fields and not has_metadata:
            logger.info(f"No useful student info for call {call_log_id}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="No useful student information extracted"
            )
        
        logger.debug(f"Proceeding with database save for call_log_id: {call_log_id}")
        
        # Validate db_config before attempting save
        if not db_config or not isinstance(db_config, dict):
            logger.error(f"Invalid db_config for call {call_log_id}: {db_config}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="Invalid database configuration"
            )
        
        # Validate required parameters
        if not call_log_id or not tenant_id:
            logger.error(f"Missing required parameters - call_log_id: {call_log_id}, tenant_id: {tenant_id}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error="Missing required parameters for database save"
            )
        
        # Save to education_students table
        logger.debug(f"Calling save_to_database: call_log_id={call_log_id}, lead_id={lead_id}")
        
        saved = False
        error_details = None
        
        try:
            # Call the async save method
            saved = await extractor.save_to_database(
                student_info=student_info,
                call_log_id=call_log_id,
                lead_id=lead_id,
                tenant_id=tenant_id,
                db_config=db_config
            )
            logger.info(f"PRODUCTION: Database save completed with result: {saved}")
            
            # Double-check result
            if saved is not True:
                logger.warning(f"PRODUCTION: save_to_database returned unexpected value: {saved} (type: {type(saved)})")
                
        except Exception as e:
            error_details = f"{type(e).__name__}: {str(e)}"
            logger.error(f"PRODUCTION: Exception during save_to_database call: {error_details}", exc_info=True)
            saved = False
        
        if saved:
            logger.info(f"PRODUCTION SUCCESS: Education vertical saved student info to database for call {call_log_id}")
            return VerticalRoutingResult(
                vertical="education",
                routed=True,
                target_table=STUDENTS_FULL,
                extracted_data=student_info
            )
        else:
            error_msg = f"Database save failed for call {call_log_id}"
            if error_details:
                error_msg += f" - {error_details}"
            logger.error(f"PRODUCTION FAILURE: {error_msg}")
            return VerticalRoutingResult(
                vertical="education",
                routed=False,
                error=error_msg
            )
            
    except Exception as e:
        logger.error(f"Education vertical routing failed for call {call_log_id}: {e}", exc_info=True)
        return VerticalRoutingResult(
            vertical="education",
            routed=False,
            error=str(e)
        )

async def _route_general_vertical(
    call_log_id: str,
    tenant_id: str,
    conversation: list[Dict[str, Any]],
    lead_id: Optional[str],
    db_config: Optional[Dict[str, Any]]
) -> VerticalRoutingResult:
    """
    Extract general lead information and update voice_call_analysis.lead_extraction.
    
    Used for non-education verticals to capture general lead data like:
    name, email, phone, company, meeting time, etc.
    """
    import os
    import json
    import requests
    from db.storage.call_analysis import CallAnalysisStorage
    
    try:
        # Convert conversation to text
        conversation_text = _format_conversation_for_extraction(conversation)
        
        if not conversation_text or len(conversation_text) < 50:
            logger.debug(f"Insufficient conversation for general extraction: {len(conversation_text)} chars")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error="Empty or insufficient conversation"
            )
        
        # Extract general lead info using Gemini
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            logger.warning("GEMINI_API_KEY not available, skipping general extraction")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error="No API key for extraction"
            )
        
        prompt = f"""Extract ALL information about the lead from this sales conversation. Capture EVERYTHING the LEAD/USER mentioned (NOT the agent/bot).

CONVERSATION:
{conversation_text}

TASK: Extract ALL information that the LEAD provided during the conversation:

1. Personal Information:
   - Lead's name (first name, full name - the person speaking, NOT agent/bot names)
   - Contact details (email, phone, WhatsApp number)
   - Position/title/role
   - Company/business name

2. Contact Preferences & Meeting Scheduling:
   - Available time (when they prefer to be contacted)
   - Agreed meeting times (if agent suggests and user agrees)
   - Preferred contact method

3. Requirements/Interests:
   - What they are looking for
   - Budget if mentioned
   - Timeline/urgency

Respond in JSON format:

{{
    "first_name": "First name of the lead or null",
    "full_name": "Full name if mentioned or null",
    "email": "Email address if mentioned or null",
    "phone": "Phone number if mentioned or null",
    "position": "Job title/position if mentioned or null",
    "company": "Company/business name if mentioned or null",
    "available_time": "Scheduled/confirmed meeting time or null",
    "contact_preference": "Preferred contact method if mentioned or null",
    "location": "Location/city if mentioned or null",
    "requirements": "What they need or are looking for or null",
    "budget": "Budget if mentioned or null",
    "timeline": "Timeline/urgency if mentioned or null",
    "additional_notes": "Any other relevant information or null"
}}

CRITICAL: Only extract information PROVIDED BY THE LEAD/USER, NOT the agent/bot.
"""
        
        # Call Gemini API using google-genai library
        try:
            from google import genai
            from google.genai import types
            
            client = genai.Client(api_key=gemini_api_key)
            
            response = client.models.generate_content(
                model="gemini-flash-latest",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=2000,
                )
            )
            
            if not response.text:
                logger.warning("Empty response from Gemini API")
                return VerticalRoutingResult(
                    vertical="general",
                    routed=False,
                    error="Empty API response"
                )
            
            raw_text = response.text.strip()
            
        except Exception as api_error:
            logger.error(f"Gemini API error: {api_error}")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error=f"API error: {api_error}"
            )
        
        # Parse JSON from response
        lead_info = None
        try:
            # Try to find JSON in response
            json_match = raw_text.find("{")
            if json_match != -1:
                json_str = raw_text[json_match:]
                decoder = json.JSONDecoder()
                lead_info, _ = decoder.raw_decode(json_str)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from general extraction response")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error="Failed to parse extraction response"
            )
        
        if not lead_info:
            logger.debug("No lead info extracted for general vertical")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error="No info extracted"
            )
        
        # Clean the extracted data (remove nulls)
        cleaned_info = {}
        for key, value in lead_info.items():
            if value is not None:
                if isinstance(value, str):
                    cleaned = value.strip()
                    if cleaned and cleaned.lower() not in ['none', 'null', 'n/a']:
                        cleaned_info[key] = cleaned
                else:
                    cleaned_info[key] = value
        
        if not cleaned_info:
            logger.debug("All extracted fields were null/empty")
            return VerticalRoutingResult(
                vertical="general",
                routed=False,
                error="No valid data extracted"
            )
        
        # Update the voice_call_analysis.lead_extraction column
        storage = CallAnalysisStorage()
        existing = await storage.get_analysis_by_call_id(call_log_id)
        
        if existing:
            # Merge with existing lead_extraction data
            existing_extraction = existing.get("lead_extraction") or {}
            if isinstance(existing_extraction, str):
                try:
                    existing_extraction = json.loads(existing_extraction)
                except json.JSONDecodeError:
                    existing_extraction = {}
            
            merged = {**existing_extraction, **cleaned_info}
            
            success = await storage.update_analysis(
                existing["id"],
                lead_extraction=merged
            )
            
            if success:
                logger.info(f"General vertical: Updated lead_extraction for call {call_log_id} with {len(cleaned_info)} fields")
                return VerticalRoutingResult(
                    vertical="general",
                    routed=True,
                    target_table=CALL_ANALYSIS_FULL,
                    extracted_data=cleaned_info
                )
        
        logger.warning(f"No analysis record found to update for call {call_log_id}")
        return VerticalRoutingResult(
            vertical="general",
            routed=False,
            error="No analysis record to update"
        )
        
    except Exception as e:
        logger.error(f"General vertical routing failed for call {call_log_id}: {e}", exc_info=True)
        return VerticalRoutingResult(
            vertical="general",
            routed=False,
            error=str(e)
        )


# Convenience function for sync contexts
def route_lead_extraction_sync(
    call_log_id: str,
    tenant_id: str,
    conversation: list[Dict[str, Any]],
    lead_id: Optional[str] = None,
    db_config: Optional[Dict[str, Any]] = None
) -> VerticalRoutingResult:
    """Synchronous wrapper for route_lead_extraction."""
    import asyncio
    
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already in async context - create task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    asyncio.run,
                    route_lead_extraction(call_log_id, tenant_id, conversation, lead_id, db_config)
                )
                return future.result()
        else:
            return loop.run_until_complete(
                route_lead_extraction(call_log_id, tenant_id, conversation, lead_id, db_config)
            )
    except RuntimeError:
        # No event loop - create one
        return asyncio.run(
            route_lead_extraction(call_log_id, tenant_id, conversation, lead_id, db_config)
        )
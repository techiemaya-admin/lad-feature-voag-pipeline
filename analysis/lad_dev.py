"""
Student Information Extraction for LAD Development
Extracts student/parent information from call transcriptions and stores in lad_dev.education_students table

Usage:
    # List all calls from database
    python lad_dev.py --list-calls
    
    # Extract student info from database call (by row number)
    python lad_dev.py --db-id 123
    
    # Extract student info from database call (by UUID)
    python lad_dev.py --db-id bcc0402b-c290-4242-9873-3cd31052b84a
"""

import os
import json
import asyncio
import re
import logging
import argparse
import uuid as uuid_lib
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Schema configuration
SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")

# Structured output client for guaranteed JSON responses
from .gemini_client import generate_with_schema_async, LAD_STUDENT_INFO_SCHEMA

load_dotenv()

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"{SCHEMA}_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.errors
    from psycopg2.extras import Json
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    logger.warning("psycopg2 not installed. Database features disabled.")
    logger.warning("   Install with: pip install psycopg2-binary")


class StudentExtractor:
    """Student information extractor from call transcriptions"""
    
    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not self.gemini_api_key:
            logger.warning("GEMINI_API_KEY not found in .env file")
    
    async def _call_gemini_structured(self, prompt: str, temperature: float = 0.2, max_output_tokens: int = 8192, max_retries: int = 3) -> Optional[Dict]:
        """Call Gemini API with structured output schema - guarantees proper JSON response"""
        if not self.gemini_api_key:
            logger.warning("Gemini API key not available, skipping API call")
            return None
        
        logger.debug(f"Calling Gemini API with structured output - Max output: {max_output_tokens}, Temp: {temperature}")
        
        try:
            # Use structured output for guaranteed JSON response
            result = await generate_with_schema_async(
                prompt=prompt,
                schema=LAD_STUDENT_INFO_SCHEMA,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                max_retries=max_retries,
            )
            
            if result:
                # Extract and log usage metadata if present
                if '_usage_metadata' in result:
                    usage = result.pop('_usage_metadata')
                    logger.info(f"Gemini usage for student info extraction: {usage}")
                
                logger.debug(f"Student info structured response received")
                return result
            else:
                logger.warning("No result from structured generation")
                return None
            
        except Exception as e:
            logger.error(f"Gemini structured API exception: {str(e)}", exc_info=True)
            return None
    
    # NOTE: _parse_summary_json removed - no longer needed with structured output
    # The generate_with_schema_async function guarantees valid JSON responses
    
    async def extract_student_information(self, conversation_text: str) -> Optional[Dict]:
        """Extract student information from conversation transcription"""
        
        if not self.gemini_api_key:
            logger.warning("Gemini API key not available for student information extraction")
            return None
        
        try:
            # Validate conversation text
            if not conversation_text or len(conversation_text.strip()) < 10:
                logger.debug("Insufficient conversation text for student information extraction")
                return None
            
            logger.info(f"Extracting student information from conversation ({len(conversation_text)} chars)")
            
            # Get current date/time in GST for context
            from datetime import datetime
            import pytz
            GST = pytz.timezone('Asia/Dubai')
            current_time = datetime.now(GST)
            current_datetime_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p GST")
            
            prompt = f"""CURRENT DATE AND TIME: {current_datetime_str}

Extract information from USER RESPONSES ONLY in this phone call conversation.
The conversation below contains only what the user/prospect said (agent messages filtered out).

USER CONVERSATION:
{conversation_text}

INSTRUCTIONS - Analyze ONLY the user responses:

STEP 1 - Find parent name:
Search the user responses for these phrases:
- "My father name is"
- "father name is"  
- "My father is"
If you find any of these, extract the name that comes after. If the name appears in parts (e.g., "Suresh" then later "Harish Kumar"), combine them into one full name.

STEP 2 - Find parent profession:
Search user responses for phrases like:
- "He is doing business"
- "doing business"
- Any mention of parent's job/profession
If found, extract the profession.

STEP 3 - Extract other fields from user responses:
- Email addresses (even if spelled phonetically)
- Meeting times when confirmed
- Other information mentioned by the user

REQUIRED FIELDS TO EXTRACT:

1. student_parent_name: 
   - Search for "My father name is" or "father name is" in the user responses
   - Search for "My father name is" or "father name is" in the conversation
   - Extract the name that follows
   - Combine name parts if mentioned separately
   - Return null ONLY if absolutely no parent name is found

2. parent_designation:
   - Search for parent's profession/job mentions
   - Extract the profession (e.g., "Business", "Doctor")
   - Return null if not mentioned
3. Program Interested In: The educational program, course, or degree the student is interested in (e.g., "Engineering", "MBA", "Medicine", "Computer Science", etc.)
4. Country Interested: The country where the student wants to study (e.g., "USA", "UK", "Canada", "Australia", etc.)
5. Intake Year: The year when the student wants to start the program (e.g., 2025, 2026, etc.)
6. Intake Month: The month when the student wants to start (e.g., "January", "September", "Fall", "Spring", etc.)
7. Any other relevant information about the student, parent, or educational interests (email, phone, percentage, grades, class, school, etc.)

Respond in JSON format. IMPORTANT EXAMPLES:
- If user says "My father name is Suresh" and later "Harish Kumar", extract student_parent_name as "Suresh Harish Kumar"
- If user says "He is doing business", extract parent_designation as "Business"

Full JSON format:
{{
    "student_parent_name": "Extract parent name if you see phrases like 'My father name is X' or 'father name is X' in the conversation. Combine name parts if mentioned separately (e.g., 'Suresh' then 'Harish Kumar' = 'Suresh Harish Kumar'). Return null ONLY if no parent name is mentioned at all.",
    "parent_designation": "Extract parent profession if mentioned (e.g., 'He is doing business' = 'Business', 'My father is a doctor' = 'Doctor'). Return null if not mentioned.",
    "program_interested_in": "Educational program/course/degree student is interested in or null",
    "country_interested": "Country where student wants to study or null",
    "intake_year": "Year when student wants to start (integer like 2025) or null",
    "intake_month": "Month when student wants to start (e.g., 'January', 'September', 'Fall') or null",
    "metadata": {{
        "email": "Email address if mentioned or null",
        "phone": "Phone number if mentioned or null",
        "percentage": "Academic percentage/score if mentioned or null",
        "grades": "Academic grades if mentioned or null",
        "class": "Current class/grade if mentioned or null",
        "school_name": "Current school name if mentioned or null",
        "curriculum": "Curriculum/board type (e.g., 'CBSE', 'ICSE', 'IGCSE', 'IB', 'State Board') if mentioned or null",
        "address": "Address or location if mentioned or null",
        "budget": "Budget or financial information if mentioned or null",
        "preferred_university": "Preferred university or college if mentioned or null",
        "subject_interests": "Subject interests or specialization if mentioned or null",
        
        "followup_time": "**AUTO_FOLLOWUP ONLY** - Extract ONLY when agent CONFIRMS a CALLBACK/FOLLOWUP at a specific time. Look for phrases: 'I'll call you back in 30 minutes', 'calling you tomorrow at 3pm', 'I'll follow up next week', 'calling after 15 mins', 'calling within 1 hour'. Format: relative time like 'in 30 minutes', 'tomorrow at 3pm', 'after 15 mins', 'within 1 hour'. CRITICAL: If agent books a CONSULTATION/COUNSELING/MEETING, DO NOT fill this field - use slot_booked_for instead. This is ONLY for simple callbacks with NO consultation. Return null if this is a consultation booking or no callback confirmed.",
        
        "slot_booked_for": "**AUTO_CONSULTATION ONLY** - Extract ONLY when agent CONFIRMS and BOOKS a CONSULTATION/COUNSELING/MEETING session with specific time AND user ACCEPTS. MUST use FULL date format: 'Day_Name, Month Day, Year at Time' (e.g., 'Tuesday, January 13, 2026 at 1:30 PM', 'Sunday, January 12, 2026 at 11:00 AM'). Use CURRENT DATE ({current_datetime_str}) to calculate exact dates. Examples: Agent says 'I'll book your counseling for Sunday at 11 AM' + user agrees = 'Sunday, January 12, 2026 at 11:00 AM'. Agent says 'Tuesday at 1:30 PM for counseling' + user confirms = 'Tuesday, January 13, 2026 at 1:30 PM'. CRITICAL: Only for SCHEDULED CONSULTATIONS/COUNSELING/MEETINGS. Return null if no consultation booked.",
        
        "available_time": "**USER AVAILABILITY ONLY** - Extract ONLY when user MENTIONS when they will be available/free BUT no callback or consultation is confirmed by agent. Examples: 'I will be free after 10 mins', 'I'm free next Monday', 'available after 5pm', 'free tomorrow'. Format: relative time like 'after 10 mins', 'next Monday', 'tomorrow', 'after 5pm'. CRITICAL RULE: If followup_time OR slot_booked_for have values (meaning callback or consultation is confirmed), then available_time MUST be null. Only fill this when user mentions availability but agent hasn't confirmed any callback or consultation yet. Return null if followup_time or slot_booked_for are filled, or if user doesn't mention availability.",
        
        "summary_last_call": "A 1-2 sentence summary of what happened in this call - e.g., 'Discussed MBA programs in USA, parent interested but requested callback next week' or 'Student interested in engineering in UK, scheduled counseling for Monday 3pm' or null if not enough context",
        "additional_notes": "Any other relevant information provided by the lead"
    }}
}}

CRITICAL EXTRACTION RULES:
1. Extract EVERYTHING the STUDENT/PARENT/USER provides - be comprehensive and thorough
2. If a field is not mentioned, set it to null (not empty string, not "None")
3. Do NOT extract agent/bot names (like "Nithya", "Mira Singh")
4. Extract information in natural language - preserve exact details when provided
5. If information is provided in multiple parts (combine them into complete values - e.g., parent name split across messages)
6. Focus on education-related conversations only - return null for all fields if not education-related
"""

            logger.debug("Extracting student information using LLM with structured output...")
            # Using structured output - result is already a parsed dict
            parsed_data = await self._call_gemini_structured(prompt, temperature=0.2, max_output_tokens=8192)
            
            if not parsed_data:
                logger.warning("LLM did not return student information")
                return None
            
            logger.info(f"Parsed JSON from LLM: {json.dumps(parsed_data, indent=2)}")
            logger.info(f"Parent name from LLM: {parsed_data.get('student_parent_name')}")
            logger.info(f"Parent designation from LLM: {parsed_data.get('parent_designation')}")
            
            # Helper function to clean string values (convert "None" string to None)
            def clean_string(value):
                if value is None:
                    return None
                value_str = str(value).strip()
                if not value_str or value_str.lower() == 'none' or value_str.lower() == 'null':
                    return None
                return value_str
            
            # Clean and validate extracted data
            student_info = {
                'student_parent_name': clean_string(parsed_data.get('student_parent_name')),
                'parent_designation': clean_string(parsed_data.get('parent_designation')),
                'program_interested_in': clean_string(parsed_data.get('program_interested_in')),
                'country_interested': clean_string(parsed_data.get('country_interested')),
                'intake_year': parsed_data.get('intake_year'),
                'intake_month': clean_string(parsed_data.get('intake_month')),
                'metadata': parsed_data.get('metadata', {})
            }
            
            # Validate intake_year - should be integer or convert if possible
            if student_info['intake_year'] is not None:
                try:
                    # Try to convert to int if it's a string
                    if isinstance(student_info['intake_year'], str):
                        student_info['intake_year'] = int(student_info['intake_year'].strip())
                    elif not isinstance(student_info['intake_year'], int):
                        logger.warning(f"Invalid intake_year format: {student_info['intake_year']}")
                        student_info['intake_year'] = None
                except (ValueError, AttributeError):
                    logger.warning(f"Could not convert intake_year to integer: {student_info['intake_year']}")
                    student_info['intake_year'] = None
            
            # Validate metadata is a dict
            if not isinstance(student_info['metadata'], dict):
                student_info['metadata'] = {}
            
            # Clean metadata values (remove "None" strings)
            cleaned_metadata = {}
            if isinstance(student_info['metadata'], dict):
                for key, value in student_info['metadata'].items():
                    if value is not None:
                        value_str = str(value).strip()
                        if value_str and value_str.lower() not in ['none', 'null']:
                            cleaned_metadata[key] = value
            student_info['metadata'] = cleaned_metadata
            
            # Check if we have at least some information (either in main fields or metadata)
            has_info_main = any([
                student_info['student_parent_name'],
                student_info['parent_designation'],
                student_info['program_interested_in'],
                student_info['country_interested'],
                student_info['intake_year'],
                student_info['intake_month']
            ])
            
            has_info_metadata = len(student_info.get('metadata', {})) > 0
            
            if not has_info_main and not has_info_metadata:
                logger.warning("No student information found in conversation")
                logger.info(f"Parsed data was: {parsed_data}")
                logger.info(f"Main fields: student_parent_name={student_info['student_parent_name']}, parent_designation={student_info['parent_designation']}, etc.")
                logger.info(f"Metadata fields: {student_info.get('metadata', {})}")
                return None
            
            logger.debug(f"Student information extracted: {student_info}")
            return student_info
            
        except Exception as e:
            logger.error(f"Error extracting student information: {str(e)}", exc_info=True)
            return None
    
    def save_to_json(self, student_info: Dict, call_log_id, lead_id, tenant_id: Optional[str]) -> Optional[str]:
        """
        Save student information to JSON file locally
        
        Args:
            student_info: The extracted student information dictionary
            call_log_id: ID from voice_call_logs table (for reference in metadata)
            lead_id: Lead ID from voice_call_logs.lead_id (UUID to connect with education_students.lead_id)
            tenant_id: Tenant ID from {SCHEMA}.tenants.id (UUID string or None)
        
        Returns:
            Path to saved JSON file or None if failed
        """
        try:
            # Prepare metadata - ONLY store information provided by the lead
            # Do NOT include system-generated fields like call_log_id or extracted_at
            # Store all information that's not in the main columns (email, percentage, grades, etc.)
            metadata_raw = student_info.get('metadata', {})
            
            # Add any additional fields from LLM that aren't in main columns
            # These could include: email, percentage, grades, etc. (ONLY lead-provided information)
            for key, value in student_info.items():
                if key not in ['student_parent_name', 'parent_designation', 'program_interested_in', 
                              'country_interested', 'intake_year', 'intake_month', 'metadata']:
                    if value is not None:
                        metadata_raw[key] = value
            
            # Filter metadata to only include non-null, non-empty values
            metadata = {}
            if isinstance(metadata_raw, dict):
                for key, value in metadata_raw.items():
                    # Only include if value is not None, not empty string, and not "None"/"null" as string
                    if value is not None:
                        value_str = str(value).strip()
                        if value_str and value_str.lower() not in ['none', 'null']:
                            metadata[key] = value
            
            # Validate lead_id UUID (already UUID in voice_call_logs.lead_id column)
            lead_id_str = None
            if lead_id is not None:
                try:
                    # Validate as UUID string - voice_call_logs.lead_id is already UUID type
                    if isinstance(lead_id, str):
                        # Validate UUID format
                        uuid_lib.UUID(lead_id)
                        lead_id_str = lead_id
                    else:
                        # Convert UUID object to string
                        lead_id_str = str(lead_id)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid lead_id UUID format: {e}")
                    lead_id_str = None
            
            # Prepare output data structure (matches database structure)
            output_data = {
                'tenant_id': tenant_id,
                'lead_id': lead_id_str,  # UUID from voice_call_logs.lead_id
                'student_parent_name': student_info.get('student_parent_name'),
                'parent_designation': student_info.get('parent_designation'),
                'program_interested_in': student_info.get('program_interested_in'),
                'country_interested': student_info.get('country_interested'),
                'intake_year': student_info.get('intake_year'),
                'intake_month': student_info.get('intake_month'),
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat(),
                'is_deleted': False
            }
            
            # Only include metadata if it has actual values (filtered to exclude null/empty)
            if metadata:
                output_data['metadata'] = metadata
            
            # Generate filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            call_id_short = str(call_log_id)[:8] if call_log_id else 'unknown'
            filename = f"{SCHEMA}_{call_id_short}_{timestamp}.json"
            
            # Create json_exports directory if it doesn't exist
            json_dir = Path(__file__).parent / "json_exports"
            json_dir.mkdir(exist_ok=True)
            
            filepath = json_dir / filename
            
            # Save to JSON file
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Student information saved to JSON file: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Failed to save JSON file: {e}", exc_info=True)
            return None
    
    async def save_to_database(self, student_info: Dict, call_log_id, lead_id, tenant_id: Optional[str], db_config: Dict) -> bool:
        """
        Save student information to {SCHEMA}.education_students table (async with psycopg2 in thread)
        
        Args:
            student_info: The extracted student information dictionary
            call_log_id: ID from voice_call_logs table (for reference in metadata)
            lead_id: Lead ID from voice_call_logs.lead_id (UUID to connect with education_students.lead_id)
            tenant_id: Tenant ID from {SCHEMA}.tenants.id (UUID string or None)
            db_config: Dict with db connection parameters
        
        Returns:
            bool: True if saved successfully
        """
        logger.info(f"[SAVE_TO_DB] Function called - call_log_id: {call_log_id}, lead_id: {lead_id}, tenant_id: {tenant_id}")
        
        if not DB_AVAILABLE:
            logger.error("Database library not available. Install psycopg2-binary")
            return False
        
        conn = None
        cursor = None
        
        def _sync_save():
            """Synchronous database save wrapped for async execution"""
            conn = None
            cursor = None
            try:
                conn = psycopg2.connect(**db_config)
                cursor = conn.cursor()
            
                # Prepare metadata - ONLY store information provided by the lead
                # Do NOT include system-generated fields like call_log_id or extracted_at
                # Store all information that's not in the main columns (email, percentage, grades, etc.)
                metadata_raw = student_info.get('metadata', {})
                
                # Add any additional fields from LLM that aren't in main columns
                # These could include: email, percentage, grades, etc. (ONLY lead-provided information)
                for key, value in student_info.items():
                    if key not in ['student_parent_name', 'parent_designation', 'program_interested_in', 
                                  'country_interested', 'intake_year', 'intake_month', 'metadata']:
                        if value is not None:
                            metadata_raw[key] = value
                
                # Filter metadata to only include non-null, non-empty values
                metadata = {}
                if isinstance(metadata_raw, dict):
                    for key, value in metadata_raw.items():
                        # Only include if value is not None, not empty string, and not "None"/"null" as string
                        if value is not None:
                            value_str = str(value).strip()
                            if value_str and value_str.lower() not in ['none', 'null']:
                                metadata[key] = value
                
                # Convert lead_id to UUID format for education_students.lead_id column (UUID type)
                # NOTE: All IDs except agent_id should be UUIDs - no int fallback
                lead_id_uuid = None
                if lead_id is not None:
                    lead_id_str = str(lead_id).strip()
                    # Reject 'None' or 'null' string values
                    if lead_id_str.lower() in ('none', 'null', ''):
                        logger.warning(f"lead_id is invalid string value: '{lead_id}'")
                        lead_id_uuid = None
                    else:
                        try:
                            # Parse as UUID - this is the only valid format
                            parsed_uuid = uuid_lib.UUID(lead_id_str)
                            lead_id_uuid = str(parsed_uuid)
                            logger.debug(f"lead_id '{lead_id_str}' validated as UUID")
                        except ValueError as e:
                            logger.error(f"lead_id '{lead_id}' is not a valid UUID: {e}")
                            lead_id_uuid = None
                
                # Prepare INSERT query with tenant_id and lead_id
                query = """
                    INSERT INTO {SCHEMA}.education_students (
                        tenant_id,
                        lead_id,
                        student_parent_name,
                        parent_designation,
                        program_interested_in,
                        country_interested,
                        intake_year,
                        intake_month,
                        metadata,
                        created_at,
                        updated_at,
                        is_deleted
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, FALSE
                    )
                    ON CONFLICT (tenant_id, lead_id) DO UPDATE SET
                        student_parent_name = COALESCE(EXCLUDED.student_parent_name, {SCHEMA}.education_students.student_parent_name),
                        parent_designation = COALESCE(EXCLUDED.parent_designation, {SCHEMA}.education_students.parent_designation),
                        program_interested_in = COALESCE(EXCLUDED.program_interested_in, {SCHEMA}.education_students.program_interested_in),
                        country_interested = COALESCE(EXCLUDED.country_interested, {SCHEMA}.education_students.country_interested),
                        intake_year = COALESCE(EXCLUDED.intake_year, {SCHEMA}.education_students.intake_year),
                        intake_month = COALESCE(EXCLUDED.intake_month, {SCHEMA}.education_students.intake_month),
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                """.format(SCHEMA=SCHEMA)
                
                values = (
                    tenant_id,  # UUID from {SCHEMA}.tenants.id
                    lead_id_uuid,  # UUID from voice_call_logs.lead_id (already UUID in database)
                    student_info.get('student_parent_name'),
                    student_info.get('parent_designation'),
                    student_info.get('program_interested_in'),
                    student_info.get('country_interested'),
                    student_info.get('intake_year'),
                    student_info.get('intake_month'),
                    Json(metadata)
                )
                
                logger.info(f"Attempting to save/update - tenant_id: {tenant_id}, lead_id: {lead_id_uuid}")
                logger.info(f"ON CONFLICT will UPDATE existing record if (tenant_id={tenant_id}, lead_id={lead_id_uuid}) already exists")
            
                cursor.execute(query, values)
                conn.commit()
                
                logger.info(f"Student information saved to database (call_log_id: {call_log_id})")
                return True
                
            except Exception as e:
                logger.error(f"Database save failed - call_log_id: {call_log_id}, lead_id: {lead_id}, tenant_id: {tenant_id}")
                logger.error(f"Error details: {type(e).__name__}: {e}", exc_info=True)
                if conn:
                    conn.rollback()
                return False
                
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
        
        # Run the synchronous DB operation in a thread pool
        try:
            return await asyncio.to_thread(_sync_save)
        except Exception as e:
            logger.error(f"Async database save wrapper failed: {e}", exc_info=True)
            return False


async def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description="Extract student information from call transcriptions")
    parser.add_argument("--db-id", type=str, help="Call ID (UUID) or row number to process")
    parser.add_argument("--list-calls", action="store_true", help="List all available calls from database")
    
    args = parser.parse_args()
    
    if not DB_AVAILABLE:
        logger.error("psycopg2 not installed. Cannot connect to database.")
        logger.error("Install with: pip install psycopg2-binary")
        return
    
    # Get database configuration from environment
    db_config = {
        'host': os.getenv('DB_HOST'),
        'port': os.getenv('DB_PORT', '5432'),
        'database': os.getenv('DB_NAME'),
        'user': os.getenv('DB_USER'),
        'password': os.getenv('DB_PASSWORD')
    }
    
    # Validate database config
    if not all([db_config['host'], db_config['database'], db_config['user'], db_config['password']]):
        logger.error("Missing database configuration. Please set DB_HOST, DB_NAME, DB_USER, DB_PASSWORD environment variables.")
        return
    
    extractor = StudentExtractor()
    
    if args.list_calls:
        # List all calls from database
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**db_config)
            cursor = conn.cursor()
            
            cursor.execute(f"""
                SELECT id, lead_id, tenant_id, started_at, agent_id
                FROM {SCHEMA}.voice_call_logs
                ORDER BY started_at DESC
                LIMIT 100
            """)
            
            calls = cursor.fetchall()
            
            print(f"\nFound {len(calls)} calls:")
            print("=" * 120)
            print(f"{'Row':<6} {'Call ID':<40} {'Lead ID':<40} {'Started At':<25}")
            print("=" * 120)
            
            for idx, (call_id, lead_id, tenant_id, started_at, agent_id) in enumerate(calls, 1):
                print(f"{idx:<6} {call_id:<40} {lead_id:<40} {str(started_at):<25}")
            
        except Exception as e:
            logger.error(f"Failed to list calls: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        
        return
    
    if args.db_id:
        # Process specific call by UUID or row number
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**db_config)
            cursor = conn.cursor()
            
            # Check if db_id is a UUID or row number
            try:
                uuid_lib.UUID(args.db_id)
                # It's a UUID
                cursor.execute(f"""
                    SELECT id, lead_id, tenant_id, transcripts, started_at
                    FROM {SCHEMA}.voice_call_logs
                    WHERE id = %s
                """, (args.db_id,))
            except ValueError:
                # It's a row number
                cursor.execute(f"""
                    SELECT id, lead_id, tenant_id, transcripts, started_at
                    FROM {SCHEMA}.voice_call_logs
                    ORDER BY started_at DESC
                    LIMIT 1 OFFSET %s
                """, (int(args.db_id) - 1,))
            
            call_data = cursor.fetchone()
            
            if not call_data:
                logger.error(f"Call not found: {args.db_id}")
                return
            
            call_id, lead_id, tenant_id, transcripts, started_at = call_data
            
            logger.info(f"Processing call_id: {call_id}, lead_id: {lead_id}")
            
            # Parse transcripts
            if transcripts:
                if isinstance(transcripts, str):
                    transcripts = json.loads(transcripts)
                
                # Extract conversation text - use only 'text' field, ignore 'indented_text'
                conversation_text = ""
                if 'messages' in transcripts:
                    lines = []
                    for msg in transcripts['messages']:
                        # Only process user messages, ignore agent messages
                        role = msg.get('role', '').lower()
                        if role == 'user':
                            # Only use 'text' field, explicitly ignore 'indented_text'
                            text = msg.get('text', '')
                            if text:
                                lines.append(f"User: {text}")
                    conversation_text = "\n".join(lines)
                elif 'segments' in transcripts:
                    lines = []
                    for seg in transcripts['segments']:
                        # Only process user segments, ignore agent segments
                        speaker = seg.get('speaker', '').lower()
                        if speaker == 'user':
                            # Only use 'text' field, explicitly ignore 'indented_text'
                            text = seg.get('text', '')
                            if text:
                                lines.append(f"User: {text}")
                    conversation_text = "\n".join(lines)

                logger.info(f"Conversation length: {len(conversation_text)} chars")

                # Extract student information
                student_info = await extractor.extract_student_information(conversation_text)

                
                if student_info:
                    logger.info(f"Extracted student info: {json.dumps(student_info, indent=2)}")
                    
                    # Save to JSON
                    json_file = extractor.save_to_json(student_info, call_id, lead_id, tenant_id)
                    if json_file:
                        logger.info(f"Saved to JSON: {json_file}")
                    
                    # Save to database
                    db_saved = await extractor.save_to_database(student_info, call_id, lead_id, tenant_id, db_config)
                    if db_saved:
                        logger.info("Saved to database successfully")
                    else:
                        logger.warning("Failed to save to database")
                else:
                    logger.warning("No student information extracted")
            else:
                logger.warning("No transcriptions found for this call")
            
        except Exception as e:
            logger.error(f"Failed to process call: {e}", exc_info=True)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        
        return
    
    # No arguments provided
    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
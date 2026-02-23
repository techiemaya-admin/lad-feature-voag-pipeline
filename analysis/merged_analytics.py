"""
Simple Call Analytics for Pluto Travels
Uses LLM (Gemini) for sentiment analysis and All processing happens AFTER the call to avoid delays
"""

import os
import json
import asyncio
import re
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from dotenv import load_dotenv


# Structured output client for guaranteed JSON responses
from .gemini_client import (
    generate_with_schema_retry, 
    generate_text,
    SENTIMENT_SCHEMA, 
    DISPOSITION_SCHEMA
)

load_dotenv()

# Schema configuration
SCHEMA = os.getenv("DB_SCHEMA", "lad_dev")

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"analytics_{datetime.now().strftime('%Y%m%d')}.log"

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
    from psycopg2.extras import Json
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    logger.warning("psycopg2 not installed. Install with: pip install psycopg2-binary")

import openai

# Lead information extractor
try:
    from .lead_info_extractor import LeadInfoExtractor
    from .stage_detector import stage_detector
    LEAD_EXTRACTOR_AVAILABLE = True
except ImportError:
    LEAD_EXTRACTOR_AVAILABLE = False
    logger.debug("LeadInfoExtractor not available - lead extraction disabled")


# Phase 13: Import storage classes for database schema
try:
    from db.storage.call_analysis import CallAnalysisStorage
    from db.storage.calls import CallStorage
    STORAGE_CLASSES_AVAILABLE = True
except ImportError:
    STORAGE_CLASSES_AVAILABLE = False
    logger.warning("Storage classes not available - using legacy direct SQL")


class SentimentCategory(Enum):
    POSITIVE = "Positive"
    NEUTRAL = "Neutral"
    NEGATIVE = "Negative"
    VERY_INTERESTED = "Very Interested"


@dataclass
class CallAnalytics:
    """Simple call analytics with sentiment and summarization"""
    
    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not self.gemini_api_key:
            logger.warning("GEMINI_API_KEY not found in .env file")
        
        # Cost tracking for Gemini API calls
        self.cost_tracker = {
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'api_calls': 0
        }
        
        # Lead information extractor (optional)
        self.lead_extractor = None
        if LEAD_EXTRACTOR_AVAILABLE:
            # Pass cost_tracker reference so lead extraction costs are tracked together
            self.lead_extractor = LeadInfoExtractor(self.gemini_api_key, self.cost_tracker)

    @staticmethod
    def _ensure_list(value) -> List[str]:
        """Normalize summary list fields to a list of strings."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, (set, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return [str(value).strip()]

    def _normalize_summary_fields(self, summary_data: Dict | None) -> Dict:
        """Ensure the structured summary uses consistent field types."""
        summary_data = dict(summary_data or {})
        summary_data['call_summary'] = str(summary_data.get('call_summary', '') or '').strip()
        summary_data['key_discussion_points'] = self._ensure_list(summary_data.get('key_discussion_points'))
        summary_data['prospect_questions'] = self._ensure_list(summary_data.get('prospect_questions'))
        summary_data['prospect_concerns'] = self._ensure_list(summary_data.get('prospect_concerns'))
        summary_data['next_steps'] = self._ensure_list(summary_data.get('next_steps'))

        summary_data['business_name'] = str(summary_data.get('business_name') or '').strip() or None
        summary_data['contact_person'] = str(summary_data.get('contact_person') or '').strip() or None
        summary_data['phone_number'] = str(summary_data.get('phone_number') or '').strip() or None
        summary_data['recommendations'] = str(summary_data.get('recommendations', '') or '').strip()

        return summary_data

    def _parse_summary_json(self, raw_text: str | None) -> Optional[Dict]:
        """Attempt to extract a JSON object from Gemini output with code fences or trailing text."""
        if not raw_text:
            return None

        text = raw_text.strip()
        if not text:
            return None

        candidates = [text]

        # Extract fenced blocks (handles both ```json and ``)
        fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]+?)```", re.IGNORECASE)
        for match in fence_pattern.finditer(text):
            snippet = match.group(1).strip()
            if snippet:
                candidates.append(snippet)

        # Handle unterminated fences by slicing after the marker
        for marker in ("```json", "```"):
            marker_index = text.lower().find(marker)
            if marker_index != -1:
                snippet = text[marker_index + len(marker):].strip()
                if snippet:
                    candidates.append(snippet)

        # Always try substring starting from first brace
        brace_index = text.find("{")
        if brace_index != -1:
            json_candidate = text[brace_index:]
            if json_candidate:
                candidates.append(json_candidate)

        decoder = json.JSONDecoder()
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue

            try:
                return decoder.decode(candidate)
            except json.JSONDecodeError:
                pass

            try:
                obj, _ = decoder.raw_decode(candidate)
                return obj
            except json.JSONDecodeError:
                pass

            if "{" in candidate:
                start = candidate.find("{")
                try_candidate = candidate[start:]
                try:
                    obj, _ = decoder.raw_decode(try_candidate)
                    return obj
                except json.JSONDecodeError:
                    continue

        return None

    def _extract_call_summary_from_text(self, text: str) -> str:
        """Extract call summary section from raw LLM text response."""
        if not text:
            return ""
        
        # Look for "call_summary", "Call Summary", "Summary" sections
        patterns = [
            r'(?:call_summary|Call Summary|Summary)[:\-]?\s*(.+?)(?=\n\n|\n(?:Key|Recommendations|Next|Questions|Concerns|Business|Prospect)|$)',
            r'Summary[:\-]?\s*(.+?)(?=\n\n|Recommendations|Key|$)',
            r'The prospect.*?(?=\n\n|Recommendations|Key|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                summary = match.group(1).strip()
                if len(summary) > 50:  # Meaningful summary
                    return summary
        
        # If no pattern matches, return first paragraph or first 1000 chars
        paragraphs = text.split('\n\n')
        if paragraphs:
            first_para = paragraphs[0].strip()
            if len(first_para) > 50:
                return first_para[:2000]  # Limit to 2000 chars if no clear section
        
        return text[:1000]  # Fallback: first 1000 chars

    def _extract_recommendations_from_text(self, text: str) -> str:
        """Extract recommendations section from raw LLM text response."""
        if not text:
            return ""
        
        # Look for "recommendations", "Recommendations", "Recommendation" sections
        patterns = [
            r'(?:recommendations|Recommendations|Recommendation)[:\-]?\s*(.+?)(?=\n\n|\n(?:Key|Next|Questions|Concerns|Business|$)|$)',
            r'Recommendations?[:\-]?\s*(.+?)(?=\n\n|$)',
            r'Based on.*?(?=\n\n|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                recommendations = match.group(1).strip()
                if len(recommendations) > 30:  # Meaningful recommendations
                    return recommendations
        
        # If no pattern matches, look for sentences starting with recommendation keywords
        sentences = re.split(r'[.!?]+', text)
        rec_sentences = []
        rec_keywords = ['recommend', 'suggest', 'should', 'priority', 'next step', 'action', 'follow up']
        
        for sentence in sentences:
            if any(keyword in sentence.lower() for keyword in rec_keywords):
                rec_sentences.append(sentence.strip())
        
        if rec_sentences:
            return '. '.join(rec_sentences)  # Return all recommendation sentences
        
        return ""  # No recommendations found

    def _extract_simple_points_from_text(self, text: str) -> List[str]:
        """Extract ALL bullet points or numbered items from raw text - NO LIMITS."""
        points = []
        lines = text.split('\n')
        
        # Look for section headers first
        in_points_section = False
        points_section_keywords = ['key discussion', 'discussion points', 'key points', 'points']
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if we're entering a points section
            if any(keyword in line_lower for keyword in points_section_keywords):
                in_points_section = True
                continue
            
            # Check if we're leaving the points section
            if in_points_section and (line_lower.startswith('prospect') or line_lower.startswith('recommendations') or line_lower.startswith('next') or line_lower.startswith('business')):
                in_points_section = False
            
            # Extract bullet points
            if in_points_section or re.match(r'^[•\-\*]\s+', line) or re.match(r'^\d+[\.\)]\s+', line):
                point = re.sub(r'^([•\-\*]|\d+[\.\)])\s+', '', line).strip()
                if len(point) > 10:  # Only meaningful points
                    points.append(point)
            # Look for lines starting with keywords
            elif any(keyword in line.lower() for keyword in ['discussed', 'talked about', 'mentioned']):
                if len(line.strip()) > 15:
                    points.append(line.strip())
        
        return points  # NO LIMIT - Return ALL points found

    def _extract_questions_from_text(self, text: str) -> List[str]:
        """Extract ALL questions from raw text - NO LIMITS."""
        questions = []
        lines = text.split('\n')
        
        # Look for questions section
        in_questions_section = False
        questions_section_keywords = ['prospect questions', 'questions', 'question']
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if we're entering a questions section
            if any(keyword in line_lower for keyword in questions_section_keywords):
                in_questions_section = True
                continue
            
            # Check if we're leaving the questions section
            if in_questions_section and (line_lower.startswith('concerns') or line_lower.startswith('recommendations') or line_lower.startswith('next') or line_lower.startswith('business')):
                in_questions_section = False
            
            # Extract questions
            if in_questions_section or '?' in line:
                question = line.strip()
                if '?' in question and len(question) > 10:
                    # Clean up bullet points
                    question = re.sub(r'^([•\-\*]|\d+[\.\)])\s+', '', question).strip()
                    if len(question) > 10:
                        questions.append(question)
        
        # Also extract questions from sentences
        sentences = re.split(r'[.!?]+', text)
        for sentence in sentences:
            sentence = sentence.strip()
            if '?' in sentence and len(sentence) > 10:
                question = sentence.split('?')[0].strip()
                if len(question) > 10 and question not in questions:
                    questions.append(question + '?')
        
        return questions  # NO LIMIT - Return ALL questions found

    def _extract_concerns_from_text(self, text: str) -> List[str]:
        """Extract ALL concerns using keyword detection - NO LIMITS."""
        concerns = []
        lines = text.split('\n')
        
        # Look for concerns section
        in_concerns_section = False
        concerns_section_keywords = ['prospect concerns', 'concerns', 'concern']
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if we're entering a concerns section
            if any(keyword in line_lower for keyword in concerns_section_keywords):
                in_concerns_section = True
                continue
            
            # Check if we're leaving the concerns section
            if in_concerns_section and (line_lower.startswith('recommendations') or line_lower.startswith('next') or line_lower.startswith('business')):
                in_concerns_section = False
            
            # Extract concerns
            if in_concerns_section:
                concern = line.strip()
                concern = re.sub(r'^([•\-\*]|\d+[\.\)])\s+', '', concern).strip()
                if len(concern) > 10:
                    concerns.append(concern)
        
        # Also extract concerns from sentences using keywords
        concern_keywords = ['concern', 'worried', 'issue', 'problem', 'not sure', 'hesitant', 'anxious', 'apprehensive']
        sentences = re.split(r'[.!?]+', text.lower())
        for sentence in sentences:
            if any(keyword in sentence for keyword in concern_keywords):
                concern = sentence.strip().capitalize()
                if len(concern) > 15 and concern not in [c.lower() for c in concerns]:
                    concerns.append(concern)
        
        return concerns  # NO LIMIT - Return ALL concerns found

    def _extract_next_steps_from_text(self, text: str) -> List[str]:
        """Extract ALL next steps from raw text - NO LIMITS."""
        next_steps = []
        lines = text.split('\n')
        
        # Look for next steps section
        in_next_steps_section = False
        next_steps_section_keywords = ['next steps', 'next step', 'next actions', 'action items', 'follow up', 'follow-up']
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if we're entering a next steps section
            if any(keyword in line_lower for keyword in next_steps_section_keywords):
                in_next_steps_section = True
                continue
            
            # Check if we're leaving the next steps section
            if in_next_steps_section and (line_lower.startswith('recommendations') or line_lower.startswith('business') or line_lower.startswith('contact')):
                in_next_steps_section = False
            
            # Extract next steps (bullet points or numbered items)
            if in_next_steps_section or re.match(r'^[•\-\*]\s+', line) or re.match(r'^\d+[\.\)]\s+', line):
                step = re.sub(r'^([•\-\*]|\d+[\.\)])\s+', '', line).strip()
                if len(step) > 10:  # Only meaningful steps
                    next_steps.append(step)
            # Look for lines starting with next step keywords
            elif any(keyword in line.lower() for keyword in ['follow up', 'schedule', 'send', 'call back', 'meet', 'contact', 'reach out']):
                if len(line.strip()) > 15:
                    next_steps.append(line.strip())
        
        # Also extract from sentences with next step keywords
        sentences = re.split(r'[.!?]+', text)
        step_keywords = ['follow up', 'schedule', 'send', 'call back', 'meet', 'contact', 'reach out', 'next step', 'action']
        for sentence in sentences:
            sentence = sentence.strip()
            if any(keyword in sentence.lower() for keyword in step_keywords) and len(sentence) > 15:
                if sentence not in next_steps:
                    next_steps.append(sentence)
        
        return next_steps  # NO LIMIT - Return ALL next steps found

    def _extract_business_info_from_text(self, text: str) -> Dict:
        """Try to extract business name, contact person, phone from text."""
        business_name = None
        contact_person = None
        phone_number = None
        
        # Look for phone patterns
        phone_patterns = [
            r'\b\d{10}\b',
            r'\b\+?\d{1,3}[-.\s]?\d{3,4}[-.\s]?\d{3,4}[-.\s]?\d{4}\b',
            r'\b\d{5}[-.\s]?\d{5}\b'
        ]
        for pattern in phone_patterns:
            phone_match = re.search(pattern, text)
            if phone_match:
                phone_number = phone_match.group()
                break
        
        # Look for business name patterns
        business_patterns = [
            r'(?:business name|company|business)[:\-]?\s*([A-Z][A-Za-z\s&]+?)(?=\n|,|\.|$)',
            r'from\s+([A-Z][A-Za-z\s&]+?)(?=\s+expressed|\s+mentioned|\s+said|,|\.|$)',
        ]
        for pattern in business_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                potential_name = match.group(1).strip()
                if len(potential_name) > 3 and 'pluto' not in potential_name.lower() and 'travels' not in potential_name.lower():
                    business_name = potential_name
                    break
        
        # Look for contact person patterns
        contact_patterns = [
            r'(?:contact person|contact|person)[:\-]?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)',
            r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s+from',
            r'I\'m\s+([A-Z][a-z]+\s+[A-Z][a-z]+)',
        ]
        for pattern in contact_patterns:
            match = re.search(pattern, text)
            if match:
                potential_contact = match.group(1).strip()
                if len(potential_contact.split()) >= 2:
                    contact_person = potential_contact
                    break
        
        return {
            'business_name': business_name,
            'contact_person': contact_person,
            'phone_number': phone_number
        }
    
    def _parse_voiceagent_segments(self, segments: list) -> str:
        """
        Parse voiceagent transcription segments format.
        
        CRITICAL RULES:
        - Include BOTH agent and user messages for full context
        - ALWAYS use 'text' field (actual spoken), NEVER use 'intended_text'
        - Filter out phone system messages (voicemail prompts)
        - Analysis will focus on USER behavior with full conversation context
        """
        # Phone system messages to filter out
        SYSTEM_MESSAGES = [
            "call has been forwarded to voice mail",
            "the person you're trying to reach is not available",
            "at the tone",
            "please record your message",
            "when you",
            "to leave a callback number",
            "press",
            "hang up",
            "send a numeric page",
            "to repeat this message",
            "for more options"
        ]
        
        conversation_lines = []
        user_message_count = 0
        
        for segment in segments:
            speaker = segment.get('speaker', '').lower()
            text = segment.get('text', '').strip()
            
            # ALWAYS use 'text' field only, ignore 'intended_text'
            if not text:
                continue
            
            # Filter out phone system messages (usually from 'user' speaker in voicemail)
            is_system_message = any(sys_msg in text.lower() for sys_msg in SYSTEM_MESSAGES)
            if is_system_message:
                logger.debug(f"Filtered out system message: {text[:50]}...")
                continue
            
            # Filter out very short likely-system messages from 'user' speaker
            if speaker == 'user' and len(text.split()) <= 2 and text.lower() in ['hello', 'hello?', 'hi', 'when you', 'this', 'that']:
                logger.debug(f"Filtered out short system fragment: {text}")
                continue
            
            # Map speaker names
            if speaker == 'agent':
                role = 'Agent'
            elif speaker == 'user':
                role = 'User'
                user_message_count += 1
            else:
                role = speaker.title()
            
            conversation_lines.append(f"{role}: {text}")
        
        conversation_text = "\n".join(conversation_lines)
        logger.info(f"Parsed {len(conversation_lines)} total messages from voiceagent format ({user_message_count} user, {len(conversation_lines) - user_message_count} agent, filtered from {len(segments)} total segments)")
        
        return conversation_text
    


    
    def _extract_user_messages(self, conversation_text: str) -> str:
        """Extract only user messages from conversation (exclude bot/agent messages)"""
        lines = conversation_text.split('\n')
        user_messages = []
        
        for line in lines:
            line = line.strip()
            # Only include lines that start with "User:"
            if line.startswith("User:"):
                user_messages.append(line.replace("User:", "").strip())
            # Exclude agent/bot messages
            elif line.startswith("Bot:") or line.startswith("Agent:"):
                continue
            # Lines without prefix might be user messages (legacy format)
            elif line and not any(line.startswith(prefix) for prefix in ["Bot:", "Agent:", "User:"]):
                user_messages.append(line)
        
        return " ".join(user_messages)
    
    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (approximately 1 token = 4 characters)"""
        if not text:
            return 0
        return len(text) // 4
    
    def _call_gemini_structured(self, prompt: str, schema, temperature: float = 0.3, max_output_tokens: int = 8192) -> Optional[Dict]:
        """Helper function to call Gemini with structured output for guaranteed JSON response"""
        if not self.gemini_api_key:
            logger.warning("Gemini API key not available, skipping API call")
            return None
        
        logger.debug(f"Calling Gemini API with structured output - Max output: {max_output_tokens}, Temp: {temperature}")
        
        try:
            # Track API call
            self.cost_tracker['api_calls'] += 1
            
            # Use structured output for guaranteed JSON response
            result = generate_with_schema_retry(
                prompt=prompt,
                schema=schema,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            
            if result:
                # Extract and track usage metadata
                if '_usage_metadata' in result:
                    usage = result.pop('_usage_metadata')
                    self.cost_tracker['total_input_tokens'] += usage.get('prompt_token_count', 0)
                    self.cost_tracker['total_output_tokens'] += usage.get('candidates_token_count', 0)
                    logger.debug(f"Gemini usage: {usage}")
                
                logger.debug(f"Gemini structured response received (Total calls: {self.cost_tracker['api_calls']})")
                return result
            else:
                logger.warning("No result from structured generation")
                return None
            
        except Exception as e:
            logger.error(f"Gemini structured API exception: {str(e)}", exc_info=True)
            return None
    
    def _call_gemini_text(self, prompt: str, temperature: float = 0.3, max_output_tokens: int = 8192) -> Optional[str]:
        """Helper function to call Gemini for plain text generation (summaries, etc.)"""
        if not self.gemini_api_key:
            logger.warning("Gemini API key not available, skipping API call")
            return None
        
        logger.debug(f"Calling Gemini API for text - Max output: {max_output_tokens}, Temp: {temperature}")
        
        try:
            # Track API call
            self.cost_tracker['api_calls'] += 1
            
            # Use plain text generation with usage tracking
            result, usage = generate_text(
                prompt=prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                include_usage=True
            )
            
            if result:
                # Track usage
                if usage:
                    self.cost_tracker['total_input_tokens'] += usage.get('prompt_token_count', 0)
                    self.cost_tracker['total_output_tokens'] += usage.get('candidates_token_count', 0)
                    logger.debug(f"Gemini usage: {usage}")
                
                logger.debug(f"Gemini text response received (Total calls: {self.cost_tracker['api_calls']})")
                return result
            else:
                logger.warning("No result from text generation")
                return None
            
        except Exception as e:
            logger.error(f"Gemini text API exception: {str(e)}", exc_info=True)
            return None
    
    def _calculate_llm_cost(self) -> Dict:
        """Calculate total LLM cost (USD) and provide formatted values
        Note: Removed rounding and INR conversion as requested.
        """
        # Gemini 3 Flash pricing (as of Jan 2025)
        # Input: $0.50 per 1M tokens
        # Output: $3.00 per 1M tokens
        INPUT_COST_PER_1M_TOKENS = 0.50  # USD
        OUTPUT_COST_PER_1M_TOKENS = 3.00  # USD
        
        # Calculate costs
        input_cost_usd = (self.cost_tracker['total_input_tokens'] / 1_000_000) * INPUT_COST_PER_1M_TOKENS
        output_cost_usd = (self.cost_tracker['total_output_tokens'] / 1_000_000) * OUTPUT_COST_PER_1M_TOKENS
        total_cost_usd = input_cost_usd + output_cost_usd

         # No INR conversion or rounding as requested by user
        
        return {
            "total_api_calls": self.cost_tracker['api_calls'],
            "input_tokens": self.cost_tracker['total_input_tokens'],
            "output_tokens": self.cost_tracker['total_output_tokens'],
            "total_tokens": self.cost_tracker['total_input_tokens'] + self.cost_tracker['total_output_tokens'],
            "cost_usd": total_cost_usd,
            "cost_usd_formatted": f"${total_cost_usd:.5f}",
            "pricing_model": "Gemini 3 Flash Preview",
            "input_rate": "$0.50 per 1M tokens",
            "output_rate": "$3.00 per 1M tokens"
        }
    
   
    async def _calculate_sentiment_with_llm(self, user_text: str, conversation_text: str) -> Dict:
        """Calculate sentiment category using LLM"""
        
        logger.debug("Calling LLM for sentiment calculation with structured output...")
        if not self.gemini_api_key:
            return {"category": "Neutral"}
        
        try:
            prompt = f"""Analyze the sentiment of this sales conversation.
            
CONVERSATION:
{conversation_text[:1000]}

TASK: Classify the prospect's sentiment and provide a confidence score:
- category: One of ["Very Interested", "Positive", "Neutral", "Negative"]
- combined_score: Numeric confidence from 0.0 to 100.0 (where 60+ = positive, 40-59 = neutral, <40 = negative)"""
            
            # Schema with both category and score
            schema = {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["Very Interested", "Positive", "Neutral", "Negative"]},
                    "combined_score": {"type": "number", "minimum": 0, "maximum": 100}
                },
                "required": ["category", "combined_score"]
            }
            
            sentiment_data = self._call_gemini_structured(prompt, schema, temperature=0.1, max_output_tokens=500)
            
            if sentiment_data:
                # Convert numeric score to percentage string for consistency
                score = sentiment_data.get("combined_score", 50)
                combined_score_str = f"{score:.1f}%"
                return {
                    "category": sentiment_data.get("category", "Neutral"),
                    "combined_score": combined_score_str
                }
            
        except Exception as e:
            logger.error(f"LLM sentiment calculation error: {e}", exc_info=True)
        
        # Fallback
        return {"category": "Neutral", "combined_score": "50.0%"}
    

    

    
    async def analyze_sentiment(self, conversation_text: str, duration: int = 0, word_count: int = 0) -> Dict:
        """Analyze sentiment using LLM (TextBlob + VADER removed) - only on user messages"""
        
        logger.debug("Starting sentiment analysis...")
        user_text = self._extract_user_messages(conversation_text)

            
        logger.debug(f"Analyzing sentiment - User text length: {len(user_text)} chars, Word count: {word_count}")
        llm_sentiment_data = await self._calculate_sentiment_with_llm(user_text, conversation_text)
        
        logger.debug(f"Sentiment analysis complete - Category: {llm_sentiment_data.get('category', 'Unknown')}")
        
        llm_category = llm_sentiment_data.get("category", "Neutral")
        
        # Map LLM category to SentimentCategory enum
        if llm_category == "Very Interested":
            category = SentimentCategory.VERY_INTERESTED
        elif llm_category == "Positive":
            category = SentimentCategory.POSITIVE
        elif llm_category == "Negative":
            category = SentimentCategory.NEGATIVE
        else:
            category = SentimentCategory.NEUTRAL
        
        # Extract key phrases from user messages only
        key_phrases = self._extract_natural_phrases(user_text)
        
        # Generate natural sentiment description using LLM
        sentiment_description = self.generate_sentiment_description(category.value, user_text)
        
        result = {
            "category": category.value,
            "sentiment_description": sentiment_description,
            "key_phrases": key_phrases[:5],
            "combined_score": llm_sentiment_data.get("combined_score", "50.0%")
        }
        
        return result
    
    def _extract_natural_phrases(self, text: str) -> List[str]:
        """Extract key topics and phrases (not full sentences)"""
        
        # Common words to filter out
        stop_words = {
            'i', 'me', 'my', 'you', 'your', 'the', 'a', 'an', 'is', 'are', 'was', 'were',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
            'can', 'this', 'that', 'these', 'those', 'it', 'its', 'be', 'been', 'being',
            'at', 'by', 'for', 'with', 'about', 'to', 'of', 'in', 'on', 'and', 'or', 'but'
        }
        
        # Extract 2-3 word phrases that represent key topics
        words = text.lower().split()
        phrases = []
        
        # Extract important 2-3 word phrases
        for i in range(len(words) - 1):
            # 2-word phrases
            word1, word2 = words[i], words[i+1]
            if word1 not in stop_words and word2 not in stop_words:
                phrase = f"{word1} {word2}"
                if len(phrase) > 5 and phrase not in phrases:
                    phrases.append(phrase)
            
            # 3-word phrases
            if i < len(words) - 2:
                word3 = words[i+2]
                if word1 not in stop_words and word2 not in stop_words:
                    phrase = f"{word1} {word2} {word3}"
                    if len(phrase) > 8 and phrase not in phrases:
                        phrases.append(phrase)
        
        # Return top 5 unique key phrases
        return list(set(phrases))[:5]
    
    def analyze_advanced_emotions(self, conversation_text: str) -> Dict:
        """Analyze emotions - returns neutral emotion (Hugging Face models removed, using LLM-only approach)"""
        
        # Return default neutral emotion structure
        # LLM will infer emotion from context in sentiment_description
        return {
            "status": "llm_only",
            "message": "Emotion analysis using LLM inference from context",
            "emotion": "neutral",
            "confidence": 0.0
        }
    
    def _get_emotion_intensity(self, confidence: float) -> str:
        """Determine emotion intensity based on confidence score"""
        if confidence >= 0.85:
            return "Very High"
        elif confidence >= 0.75:
            return "High"
        elif confidence >= 0.60:
            return "Medium"
        elif confidence >= 0.45:
            return "Low"
        else:
            return "Very Low"
    

    
    def generate_sentiment_description(self, category: str, user_text: str) -> str:
        """Generate comprehensive behavioral and emotional analysis of the prospect"""
        
        if not self.gemini_api_key:
            return f"Prospect shows {category.lower()} sentiment"
        
        try:
            # Analyze conversation patterns
            word_count = len(user_text.split())
            question_count = user_text.count('?')
            
            # Create comprehensive prompt for detailed behavioral analysis
            prompt = f"""Analyze this B2B sales prospect's behavior and buying signals:

PROSPECT'S WORDS: "{user_text}"

METRICS: {word_count} words | {question_count} questions | Sentiment: {category}

Write 3 sentences covering:
1. Emotional state and engagement level
2. Communication style and buying signals
3. Likelihood to convert and recommended next steps

Be specific about their mindset, not generic."""
            
            description = self._call_gemini_text(prompt, temperature=0.3, max_output_tokens=2000)
            
            if description:
                # Remove quotes if LLM added them
                description = description.strip('"\'')
                return description
            else:
                # Fallback to simple description
                return f"Prospect shows {category.lower()} sentiment. Engagement level: {word_count} words spoken with {question_count} questions asked."
            
        except Exception as e:
            # Fallback to simple description
            return f"Prospect shows {category.lower()} sentiment with limited behavioral data available."
    

    async def generate_call_summary(self, call_id: str, sentiment: Dict, summary: Dict, duration_seconds: int, conversation_text: str) -> str:
        """Generate a comprehensive call summary"""
        
        try:
            # Basic call info
            duration_minutes = duration_seconds // 60
            duration_seconds_remainder = duration_seconds % 60
            word_count = len(conversation_text.split())
            
            # Sentiment info
            sentiment_description = sentiment.get('sentiment_description', 'No description available')
            
            # Summary info
            business_name = summary.get('business_name', 'Not provided')
            contact_person = summary.get('contact_person', 'Not provided')
            phone_number = summary.get('phone_number', 'Not provided')
            
            # Build comprehensive summary
            summary_parts = []
            
            # Call overview
            summary_parts.append(f"CALL OVERVIEW")
            summary_parts.append(f"Call ID: {call_id}")
            summary_parts.append(f"Duration: {duration_minutes}m {duration_seconds_remainder}s")
            summary_parts.append(f"Conversation: {word_count} words")
            summary_parts.append("")
            
            # Sentiment analysis
            summary_parts.append(f"PROSPECT SENTIMENT")
            summary_parts.append(f"Description: {sentiment_description}")
            summary_parts.append("")
            
            # Business information
            summary_parts.append(f"BUSINESS INFORMATION")
            summary_parts.append(f"Company: {business_name}")
            summary_parts.append(f"Contact: {contact_person}")
            summary_parts.append(f"Phone: {phone_number}")
            summary_parts.append("")
            
            # Call summary from LLM (before key discussion points)
            if 'call_summary' in summary and summary['call_summary']:
                summary_parts.append(f"CALL SUMMARY")
                # Split by sentences for vertical display
                import re
                call_summary_text = summary['call_summary']
                # Split on period followed by space or end of string
                sentences = re.split(r'\.\s+|\.\s*$', call_summary_text)
                for sentence in sentences:
                    sentence = sentence.strip()
                    if sentence:  # Only add non-empty sentences
                        # Add period if it doesn't end with punctuation
                        if not sentence.endswith(('.', '!', '?')):
                            sentence += '.'
                        summary_parts.append(sentence)
                summary_parts.append("")
            
            # Key discussion points
            if 'key_discussion_points' in summary and summary['key_discussion_points']:
                summary_parts.append(f"💬 KEY DISCUSSION POINTS")
                for i, point in enumerate(summary['key_discussion_points'], 1):
                    summary_parts.append(f"{i}. {point}")
                summary_parts.append("")
            
            # Prospect questions
            if 'prospect_questions' in summary and summary['prospect_questions']:
                summary_parts.append(f"PROSPECT QUESTIONS")
                for i, question in enumerate(summary['prospect_questions'], 1):
                    summary_parts.append(f"{i}. {question}")
                summary_parts.append("")
            
            # Prospect concerns
            if 'prospect_concerns' in summary and summary['prospect_concerns']:
                summary_parts.append(f"PROSPECT CONCERNS")
                for i, concern in enumerate(summary['prospect_concerns'], 1):
                    summary_parts.append(f"{i}. {concern}")
                summary_parts.append("")
            
            # Next steps
            if 'next_steps' in summary and summary['next_steps']:
                summary_parts.append(f"AGREED NEXT STEPS")
                for i, step in enumerate(summary['next_steps'], 1):
                    summary_parts.append(f"{i}. {step}")
                summary_parts.append("")
            
            
            return "\n".join(summary_parts)
            
        except Exception as e:
            return f"Error generating call summary: {str(e)}"
    
    def generate_full_summary(self, sentiment_category: str, sentiment_description: str, business_name: str, contact_person: str, summary: Dict, duration_minutes: int, word_count: int) -> str:
        """Generate a comprehensive full summary of the call"""
        
        try:
            # Extract key information
            key_points = summary.get('key_discussion_points', [])
            questions = summary.get('prospect_questions', [])
            concerns = summary.get('prospect_concerns', [])
            next_steps = summary.get('next_steps', [])
            recommendations = summary.get('recommendations', '')
            
            # Build comprehensive summary
            summary_parts = []
            
            # Opening
            summary_parts.append(f"This {duration_minutes}-minute call with {business_name} ({contact_person}) resulted in a {sentiment_category.lower()} outcome.")
            summary_parts.append(f"The prospect appeared {sentiment_description.lower()} throughout the conversation.")
            summary_parts.append("")
            
            # Main discussion
            if key_points:
                summary_parts.append("Key topics discussed included:")
                for point in key_points[:3]:  # Top 3 points
                    summary_parts.append(f"• {point}")
                summary_parts.append("")
            
            # Questions and concerns
            if questions:
                summary_parts.append("The prospect asked several questions:")
                for question in questions[:2]:  # Top 2 questions
                    summary_parts.append(f"• {question}")
                summary_parts.append("")
            
            if concerns:
                summary_parts.append("Main concerns raised:")
                for concern in concerns[:2]:  # Top 2 concerns
                    summary_parts.append(f"• {concern}")
                summary_parts.append("")
            
            # Next steps and outcome
            if next_steps:
                summary_parts.append("Agreed next steps:")
                for step in next_steps:
                    summary_parts.append(f"• {step}")
                summary_parts.append("")
            
            # Recommendations
            if recommendations:
                summary_parts.append("Recommended follow-up approach:")
                summary_parts.append(recommendations)
                summary_parts.append("")
            
            # Overall assessment
            if sentiment_category == "Very Interested":
                summary_parts.append("Overall Assessment: This is a high-priority prospect showing strong interest and engagement. Immediate follow-up is recommended to capitalize on their enthusiasm.")
            elif sentiment_category == "Positive":
                summary_parts.append("Overall Assessment: This prospect shows positive interest and should be followed up within 24-48 hours to maintain momentum.")
            elif sentiment_category == "Neutral":
                summary_parts.append("Overall Assessment: This prospect maintains a neutral stance and requires nurturing. Follow up in 1-2 weeks with additional value.")
            else:
                summary_parts.append("Overall Assessment: This prospect shows resistance or negative sentiment. Address their concerns before re-engaging.")
            
            return "\n".join(summary_parts)
            
        except Exception as e:
            return f"Error generating full summary: {str(e)}"
    
    async def generate_llm_full_summary(self, call_id: str, sentiment: Dict, summary: Dict, duration_seconds: int, conversation_text: str) -> str:
        """Generate comprehensive full summary using LLM (Gemini)"""
        
        if not self.gemini_api_key:
            return "Gemini API key not configured - using fallback summary"
        
        try:
            # Prepare context for LLM
            sentiment_category = sentiment.get('category', 'Unknown')
            sentiment_description = sentiment.get('sentiment_description', 'No description')
            business_name = summary.get('business_name', 'Not provided')
            contact_person = summary.get('contact_person', 'Not provided')
            duration_minutes = duration_seconds // 60
            
            # Create detailed prompt for LLM
            confidence_score = sentiment.get('confidence_score', 'N/A')
            
            prompt = f"""
            Create a simple, easy-to-understand summary of this sales call in 2-3 sentences that anyone can understand.
            Focus on what happened during the call and the outcome.
            
            CALL DETAILS:
            - Call ID: {call_id}
            - Duration: {duration_minutes} minutes
            - Company: {business_name}
            - Contact: {contact_person}
            - Prospect Sentiment: {sentiment_description}
            - Confidence: {confidence_score}
            
            CONVERSATION:
            {conversation_text}
            
            CALL SUMMARY DATA:
            Key Discussion Points: {summary.get('key_discussion_points', [])}
            Prospect Questions: {summary.get('prospect_questions', [])}
            Prospect Concerns: {summary.get('prospect_concerns', [])}
            Next Steps: {summary.get('next_steps', [])}
            Recommendations: {summary.get('recommendations', '')}
            
            Write a simple, clear summary that explains:
            1. What was discussed in the call
            2. How the prospect responded
            3. What happens next (if anything)
            
            Use simple language that anyone can understand. Keep it brief (2-3 sentences).
            """
            
            full_summary = self._call_gemini_text(prompt, temperature=0.3, max_output_tokens=2000)
            
            if full_summary:
                # Clean up the response
                if "```" in full_summary:
                    start = full_summary.find("```") + 3
                    end = full_summary.find("```", start)
                    if end != -1:
                        full_summary = full_summary[start:end].strip()
                return full_summary
            else:
                return "LLM API error - unable to generate summary"
            
        except Exception as e:
            return f"LLM full summary error: {str(e)}"
    
    async def _determine_lead_disposition_llm(self, sentiment: Dict, summary: Dict, stage_info: Dict = None) -> Dict:
        """Use LLM to determine lead disposition with stage-based restrictions"""
        
        logger.debug(f"Determining disposition from sentiment, summary, and stages")
        if not self.gemini_api_key:
            # Fallback to rule-based if no API key
            return self._determine_lead_disposition_fallback(sentiment, summary)
        
        # Check if stages 3 or 4 were reached - required for PROCEED IMMEDIATELY
        stages_reached = stage_info.get('stages_reached', []) if stage_info else []
        reached_conversion_stage = any(stage in stages_reached for stage in ['3_email_sent', '4_counseling_booked'])
        
        try:
            call_summary = str(summary.get('call_summary', ''))
            concerns = summary.get('prospect_concerns', [])
            next_steps = summary.get('next_steps', [])
            sentiment_category = sentiment.get('category', 'Neutral')
            
            # Build stage-aware prompt
            stage_restriction = ""
            if not reached_conversion_stage:
                stage_restriction = """
CRITICAL RESTRICTION: PROCEED IMMEDIATELY (A) is ONLY allowed if prospect reached Stage 3 (email provided) or Stage 4 (meeting booked). 
Since no conversion stage was reached, you MUST choose between B, C, or D only.
"""
            
            prompt = f"""Analyze this sales call and determine the lead disposition.

CALL DATA:
- Sentiment: {sentiment_category}
- Call Summary: {call_summary}
- Prospect Concerns: {', '.join(concerns) if concerns else 'None'}
- Next Steps: {', '.join(next_steps) if next_steps else 'None'}
- Stages Reached: {', '.join(stages_reached) if stages_reached else 'None'}

{stage_restriction}
TASK: Classify this lead as ONE of the following:

A. PROCEED IMMEDIATELY
   - AUTOMATICALLY selected if Stage 3 (email provided) or Stage 4 (meeting booked) reached
   - Prospect took concrete action (email, meeting booking) = ready to convert
   - Strong buying signals demonstrated through actions
   - Follow up within 24 hours for immediate conversion

B. FOLLOW UP IN 3 DAYS
   - Moderate interest, needs nurturing
   - Asked questions but not ready to decide
   - Said "busy right now" (timing issue, not rejection)
   - Some engagement but no concrete conversion actions
   - Warm lead, follow up in 2-3 days

C. NURTURE (7 DAYS)
   - Low engagement but not negative
   - Has existing partner but open to alternatives
   - Gatekeeper (can't share info, will forward internally)
   - No conversion actions taken
   - Long-term nurture, follow up in 1-2 weeks

D. DON'T PURSUE
   - Explicitly not interested ("don't want", "don't need", "not interested")
   - Very happy with current provider and NOT open to switch
   - Asked to stop calling or remove from list
   - Very negative responses throughout
   - No positive signals at all

CRITICAL RULES:
1. "Busy right now" = B (timing issue, NOT rejection)
2. "Already have + open to switch" = C (nurture opportunity)
3. "Already have + 100% happy + NOT open" = D (don't pursue)
4. Gatekeeper (can't share details) = C (nurture)
5. "Don't want/need" or "not interested" = D (don't pursue)
6. Asked about pricing/cost = B or A (if conversion stage reached)
7. Strong interest signals = A (only if conversion stage reached)
8. Negative sentiment with no positive signals = D
9. **CRITICAL: Stage 3 (email) or Stage 4 (meeting) reached = AUTOMATICALLY PROCEED IMMEDIATELY (A)**

Respond in this EXACT format:
DISPOSITION: [A/B/C/D]
ACTION: [One-line recommended action]
REASONING: [One sentence explaining why]
CONFIDENCE: [High/Medium/Low]"""

            llm_response = self._call_gemini_text(prompt, temperature=0.1, max_output_tokens=2000)
            
            if not llm_response:
                logger.warning("LLM disposition failed, using fallback")
                return self._determine_lead_disposition_fallback(sentiment, summary)
            
            # Parse LLM response
            disposition_map = {
                'A': 'PROCEED IMMEDIATELY',
                'B': 'FOLLOW UP IN 3 DAYS',
                'C': 'NURTURE (7 DAYS)',
                'D': 'DON\'T PURSUE'
            }
            
            action_map = {
                'A': 'Follow up within 24 hours',
                'B': 'Send info, follow up in 2-3 days',
                'C': 'Add to nurture campaign, follow up in 1-2 weeks',
                'D': 'Archive - Focus on other leads'
            }
            
            # Extract disposition letter (A/B/C/D)
            disposition_letter = None
            for line in llm_response.split('\n'):
                if 'DISPOSITION:' in line.upper():
                    for letter in ['A', 'B', 'C', 'D']:
                        if letter in line:
                            disposition_letter = letter
                            break
                    break
            
            if not disposition_letter:
                logger.warning("Could not parse LLM response, using fallback")
                return self._determine_lead_disposition_fallback(sentiment, summary)
            
            # Extract action and reasoning
            action = action_map.get(disposition_letter, "Review manually")
            reasoning = ""
            confidence = "Medium"
            
            for line in llm_response.split('\n'):
                if 'ACTION:' in line.upper():
                    action = line.split(':', 1)[1].strip()
                elif 'REASONING:' in line.upper():
                    reasoning = line.split(':', 1)[1].strip()
                elif 'CONFIDENCE:' in line.upper():
                    confidence = line.split(':', 1)[1].strip()
            
            if not reasoning:
                reasoning = f"LLM analysis based on call context and sentiment"
            
            return {
                "disposition": disposition_map[disposition_letter],
                "recommended_action": action,
                "reasoning": reasoning,
                "decision_confidence": confidence,
                "llm_powered": True
            }
            
        except Exception as e:
            logger.error(f"LLM disposition error: {str(e)}, using fallback", exc_info=True)
            return self._determine_lead_disposition_fallback(sentiment, summary)
    
    def _determine_lead_disposition_fallback(self, sentiment: Dict, summary: Dict) -> Dict:
        """Determine if lead is worth pursuing - clear YES/NO decision"""
        
        concerns = summary.get('prospect_concerns', [])
        next_steps = summary.get('next_steps', [])
        
        # Decision logic
        disposition = "UNKNOWN"
        action = "Review manually"
        reasoning = []
        
        # RED FLAGS - Don't pursue (check both concerns AND call summary)
        red_flags = [
            "already have",
            "not interested",
            "don't call",
            "remove my number",
            "satisfied with current",
            "100% happy",
            "completely satisfied",
            "very happy with",
            "please stop calling",
            "take me off your list",
            "don't contact me",
            "not looking for",
            "don't need this",
            "don't need",
            "didn't need",
            "don't want",
            "not for me",
            "no thanks",
            "not right now",
            "waste of time",
            "stop bothering me"
        ]
        
        # Check red flags in concerns
        has_red_flag_in_concerns = any(
            any(flag in str(concern).lower() for flag in red_flags)
            for concern in concerns
        )
        
        # Also check red flags in call summary (more comprehensive)
        call_summary = str(summary.get('call_summary', '')).lower()
        has_red_flag_in_summary = any(flag in call_summary for flag in red_flags)
        
        # Red flag if found in EITHER location
        has_red_flag = has_red_flag_in_concerns or has_red_flag_in_summary
        
        # GREEN FLAGS - Strong interest signals only (more strict)
        green_flags = [
            "very interested",
            "definitely interested",
            "tell me more about pricing",
            "how much does it cost",
            "what's the price",
            "send me pricing",
            "want to know more",
            "sounds good",
            "sounds interesting",
            "that's exactly what we need",
            "when can we start",
            "let's schedule a demo",
            "can we meet"
        ]
        
        summary_text = str(summary.get('call_summary', '')).lower()
        has_green_flag = any(flag in summary_text for flag in green_flags)
        
        # GATEKEEPER FLAGS - Not decision maker, can't help
        gatekeeper_flags = [
            "i can't share",
            "i'm not authorized",
            "i don't handle",
            "not the right person",
            "can't disclose",
            "don't have that information",
            "i'll forward this",
            "let me check with",
            "my manager handles",
            "my boss decides",
            "i'm just an assistant",
            "talk to hr",
            "contact our admin",
            "someone else manages"
        ]
        
        has_gatekeeper_flag = any(flag in summary_text for flag in gatekeeper_flags)
        
        # SPECIAL CASE: "Already have" (including variations) but open to switching
        already_have_variations = [
            "already have",
            "i have on business travel",
            "i have a travel",
            "i have someone",
            "we have a partner",
            "working with another",
            "currently using",
            "we use",
            "contracted with",
            "signed with",
            "partnered with"
        ]
        has_already_have = any(variation in call_summary for variation in already_have_variations)
        
        open_to_switch = any(phrase in call_summary for phrase in [
            "explore other options",
            "keeping options open",
            "open to exploring",
            "if they don't meet",
            "decide to leave",
            "if i leave",
            "switch",
            "secondary option",
            "backup option",
            "keep in mind",
            "for future reference",
            "if less discount",
            "if better price",
            "if cheaper",
            "i will prefer you",
            "consider switching",
            "might switch",
            "compare prices",
            "looking for alternatives",
            "contract expires",
            "contract renewal",
            "not locked in",
            "willing to change",
            "open to better deals",
            "if you offer more"
        ])
        
        # DECISION MATRIX
        if has_red_flag and not open_to_switch:
            # Hard red flag - not interested at all
            disposition = "DON'T PURSUE"
            action = "Archive - Focus on other leads"
            reasoning.append("Prospect not qualified or explicitly not interested")
            
        elif has_gatekeeper_flag:
            # Gatekeeper who can't/won't share decision maker info
            disposition = "NURTURE (7 DAYS)"
            action = "Send WhatsApp info, wait for internal referral"
            reasoning.append("Not decision maker, can't share details")
            
        elif has_already_have and not open_to_switch:
            # Has partner and NOT open to switching
            disposition = "DON'T PURSUE"
            action = "Archive - Satisfied with current provider"
            reasoning.append("Has existing partner and not open to alternatives")
            
        elif has_already_have and open_to_switch:
            # They have a partner BUT are open to switching - NURTURE lead!
            disposition = "NURTURE (7 DAYS)"
            action = "Send promised info, follow up in 1-2 weeks"
            reasoning.append("Has existing partner but open to alternatives")
            
        elif has_green_flag:
            # Strong interest signals
            disposition = "PROCEED IMMEDIATELY"
            action = "Follow up within 24 hours"
            reasoning.append("High interest with strong buying signals")
            
        elif next_steps and len(next_steps) > 0 and next_steps[0] != "None":
            # Has next steps - good engagement
            disposition = "FOLLOW UP IN 3 DAYS"
            action = "Send info, follow up in 2-3 days"
            reasoning.append("Good engagement with next steps agreed")
            
        else:
            # Default: moderate engagement, needs nurturing
            disposition = "FOLLOW UP IN 3 DAYS"
            action = "Send info, follow up in 2-3 days"
            reasoning.append("Moderate interest, needs nurturing")
        
        # Additional context
        if "busy" in str(concerns).lower() or "busy" in call_summary:
            reasoning.append("Prospect mentioned being busy - timing issue, not rejection")
            if disposition == "DON'T PURSUE":
                disposition = "FOLLOW UP IN 3 DAYS"
                action = "Try again when less busy"
        
        return {
            "disposition": disposition,
            "recommended_action": action,
            "reasoning": " | ".join(reasoning),
            "decision_confidence": "High" if has_red_flag or has_green_flag else "Medium"
        }
    
    def _calculate_lead_score_from_disposition(self, disposition: Dict) -> Dict:
        """
        Calculate lead_score and lead_category based on disposition.
        
        Mapping:
        - "PROCEED IMMEDIATELY" → lead_score = 10/10, lead_category = "Hot Lead" (immediate conversion)
        - "FOLLOW UP IN 3 DAYS" → lead_score = 8/10, lead_category = "Warm Lead"
        - "FOLLOW UP IN 7 DAYS" or "NURTURE (7 DAYS)" → lead_score = 6/10, lead_category = "Warm Lead"
        - "DON'T PURSUE" → lead_score = 4/10, lead_category = "Cold Lead"
        """
        disposition_str = disposition.get('disposition', '').upper()
        
        if disposition_str == 'PROCEED IMMEDIATELY':
            lead_score = 10.0  # Maximum score - immediate conversion
            lead_category = "Hot Lead"  # Immediate Hot Lead status
            priority = "High"
        elif disposition_str == 'FOLLOW UP IN 3 DAYS':
            lead_score = 8.0
            lead_category = "Warm Lead"
            priority = "High"
        elif disposition_str in ['FOLLOW UP IN 7 DAYS', 'NURTURE (7 DAYS)', 'NURTURE']:
            lead_score = 6.0
            lead_category = "Warm Lead"
            priority = "Medium"
        elif disposition_str == "DON'T PURSUE" or disposition_str == "DONT PURSUE":
            lead_score = 4.0
            lead_category = "Cold Lead"
            priority = "Low"
        else:
            # Default/Unknown disposition
            lead_score = 5.0
            lead_category = "Warm Lead"
            priority = "Medium"
        
        return {
            "lead_score": lead_score,
            "max_score": 10.0,
            "lead_category": lead_category,
            "priority": priority,
            "scoring_breakdown": {
                "disposition_based": f"Score calculated from disposition: {disposition_str}"
            }
        }
    
    def _update_lead_category_based_on_stages(self, lead_score: Dict, stage_info: Dict, tenant_id: str = None) -> Dict:
        """
        Update lead category based on stages reached.
        
        Logic:
        - GLINKS tenant: Upgrade to Hot Lead ONLY if reached Stage 3 (email_sent) or Stage 4 (counseling_booked)
        - Other tenants: Do NOT use stage-based logic (use sentiment/duration logic instead)
        - Keep all other categories unchanged (Warm Lead, Cold Lead, etc.)
        """
        GLINKS_TENANT_ID = os.getenv("GLINKS_TENANT_ID", "926070b5-189b-4682-9279-ea10ca090b84")
        
        stages_reached = stage_info.get('stages_reached', [])
        final_stage = stage_info.get('final_stage', '')
        
        # Check if GLINKS tenant and reached Stage 3 or 4
        is_glinks = tenant_id == GLINKS_TENANT_ID
        reached_hot_stage = any(stage in stages_reached for stage in ['3_email_sent', '4_counseling_booked'])
        
        if is_glinks and reached_hot_stage:
            # GLINKS tenant: Upgrade to Hot Lead based on stages
            lead_category = "Hot Lead"
            final_score = 10.0  # Boost to max score
            priority = "High"
            stage_bonus = "GLINKS: Reached Stage 3/4 - Upgraded to Hot Lead"
        else:
            # Non-GLINKS tenants or no hot stage: Keep original category and score
            lead_category = lead_score.get('lead_category', 'Warm Lead')
            final_score = lead_score.get('lead_score', 6.0)
            priority = lead_score.get('priority', 'Medium')
            stage_bonus = "No stage-based upgrade"
        
        # Update scoring breakdown
        scoring_breakdown = lead_score.get('scoring_breakdown', {})
        scoring_breakdown['stage_based'] = stage_bonus
        
        return {
            "lead_score": final_score,
            "max_score": 10.0,
            "lead_category": lead_category,
            "priority": priority,
            "scoring_breakdown": scoring_breakdown
        }
    
    def _update_lead_category_for_non_glinks(self, lead_score: Dict, sentiment: Dict, duration: int) -> Dict:
        """
        Update lead category for non-GLINKS tenants based on sentiment and duration.
        
        Logic:
        - High sentiment + Duration > 1 minute → Upgrade to Hot Lead
        - Keep all other categories unchanged
        """
        # Get sentiment category and score
        sentiment_category = sentiment.get('category', '').lower()
        combined_score_str = sentiment.get('combined_score', '0.0%')
        
        # Parse percentage string to float
        try:
            sentiment_score = float(combined_score_str.replace('%', '')) / 100.0
        except (ValueError, AttributeError):
            sentiment_score = 0.0
        
        # Check for high sentiment and duration > 1 minute
        is_high_sentiment = sentiment_category in ['positive', 'very positive'] and sentiment_score >= 0.6
        is_long_duration = duration > 60  # More than 1 minute
        
        if is_high_sentiment and is_long_duration:
            # Upgrade to Hot Lead for non-GLINKS tenants
            lead_category = "Hot Lead"
            final_score = 10.0  # Boost to max score
            priority = "High"
            sentiment_bonus = f"Non-GLINKS: High sentiment ({sentiment_category}) + {duration}s duration - Upgraded to Hot Lead"
        else:
            # Keep original category and score
            lead_category = lead_score.get('lead_category', 'Warm Lead')
            final_score = lead_score.get('lead_score', 6.0)
            priority = lead_score.get('priority', 'Medium')
            sentiment_bonus = f"Non-GLINKS: Sentiment={sentiment_category}, Duration={duration}s - No upgrade"
        
        # Update scoring breakdown
        scoring_breakdown = lead_score.get('scoring_breakdown', {})
        scoring_breakdown['sentiment_duration_based'] = sentiment_bonus
        
        return {
            "lead_score": final_score,
            "max_score": 10.0,
            "lead_category": lead_category,
            "priority": priority,
            "scoring_breakdown": scoring_breakdown
        }
    
    def _enhance_recommendations(self, base_rec: str, lead_score: Dict, sentiment: Dict, quality: Dict, stage: Dict, duration: int) -> str:
        """Enhance recommendations with complete analytics context"""
        
        # Extract key metrics
        score = lead_score.get('lead_score', 0)
        category = lead_score.get('lead_category', 'Unknown')
        engagement = quality.get('engagement_level', 'Unknown')
        quality_rating = quality.get('quality_rating', 'Unknown')
        emotion = sentiment.get('advanced_emotions', {}).get('emotion', 'neutral')
        completion = stage.get('stage_completion_percentage', '0%')
        
        # Build enhanced recommendation
        enhanced = f"{base_rec}\n\n"
        enhanced += f"Analytics Context:\n"
        enhanced += f"- Lead Score: {score}/10 ({category})\n"
        enhanced += f"- Engagement: {engagement} ({quality_rating} quality)\n"
        enhanced += f"- Emotion: {emotion.title()}\n"
        enhanced += f"- Stage Completion: {completion}\n"
        enhanced += f"- Duration: {duration}s\n\n"
        
        # Add priority-based action items
        if score >= 7:
            enhanced += "Priority: HIGH - Immediate follow-up required within 24 hours.\n"
            enhanced += "Action: Call back or send personalized WhatsApp with pricing details."
        elif score >= 5:
            enhanced += "Priority: MEDIUM - Follow up within 2-3 days.\n"
            enhanced += "Action: Send WhatsApp brochure, email summary of value proposition."
        elif score >= 3:
            enhanced += "Priority: LOW - Nurture campaign, follow up in 1 week.\n"
            enhanced += "Action: Add to email list, send monthly newsletter."
        else:
            enhanced += "Priority: VERY LOW - Not qualified, minimal follow-up.\n"
            enhanced += "Action: Archive, focus on higher-scoring leads."
        
        return enhanced
    
    async def generate_summary(self, conversation_text: str, sentiment_data: Dict = None, duration: int = 0) -> Dict:
        """Generate call summary with enhanced recommendations using all analytics data"""
        
        logger.info("Starting summary generation...")
        if not self.gemini_api_key:
            logger.error("Gemini API key not configured for summary generation")
            return {"error": "Gemini API key not configured"}
        
        try:
            # Build analytics context for smarter recommendations
            analytics_context = ""
            if sentiment_data:
                analytics_context = f"""
            
            ANALYTICS DATA (use this for smarter recommendations):
            - Sentiment: {sentiment_data.get('sentiment_description', 'N/A')[:100]}...
            - Confidence: {sentiment_data.get('confidence_score', 'N/A')}
            - Combined Score: {sentiment_data.get('combined_score', 'N/A')}
            - Emotion: {sentiment_data.get('advanced_emotions', {}).get('emotion', 'N/A')}
            - Call Duration: {duration} seconds
            """
            
            prompt = f"""
            Analyze this sales conversation and provide a structured summary:

            CONVERSATION:
            {conversation_text}
            {analytics_context}

            Please provide a JSON response with the following structure:
            {{
                "call_summary": "Complete detailed summary from prospect's perspective (3-5 sentences)",
                "key_discussion_points": ["point1", "point2", "point3"],
                "prospect_questions": ["question1", "question2"],
                "prospect_concerns": ["concern1", "concern2"],
                "next_steps": ["step1", "step2"],
                "business_name": "extracted PROSPECT's business name (NOT Pluto Travels) or null",
                "contact_person": "extracted PROSPECT's contact name (NOT the agent/caller from Pluto Travels) or null",
                "phone_number": "extracted PROSPECT's phone number or null",
                "recommendations": "Detailed, actionable recommendation based on sentiment, emotion, duration, and conversation quality. Include specific next steps, timing, and priority level."
            }}

            CRITICAL INSTRUCTIONS FOR "call_summary":
            - Write a COMPLETE, DETAILED summary (3-5 sentences minimum)
            - DO NOT mention "Nithya", "Pluto Travels", "agent", "caller", or "received a call from"
            - Focus ONLY on what the PROSPECT said, expressed, and their business needs
            - Start directly with the prospect's information: "The prospect from [company] expressed..."
            - Include: their company name (if mentioned), their specific needs, what they asked about, their interest level
            - Write as if describing what the prospect told us about their business situation
            
            Example GOOD call_summary:
            "The prospect, Pavan Kumar from Techie Maya, expressed a need for transportation services for his employees, specifically drivers to travel 15 kilometers from their host to the company. The prospect inquired about pricing for this service. The conversation was an introductory call to explore how we can support Techie Maya's travel needs."
            
            Example BAD call_summary (DO NOT DO THIS):
            "The prospect received a call from Nithya, a corporate travel specialist from Pluto Travels..."
            
            OTHER FIELD INSTRUCTIONS:
            - For "business_name": Extract the PROSPECT's company name, NOT "Pluto Travels"
            - For "contact_person": Extract the PROSPECT's name, NOT the agent's name
            - Focus on what the PROSPECT said, their needs, and their response
            
            Focus on:
            - Business travel/transport needs mentioned by the prospect
            - Pricing discussions
            - Timeline requirements
            - Decision-making process
            - Any commitments or next steps agreed upon
            """
            
            logger.debug(f"Generating summary - Conversation length: {len(conversation_text)} chars, Duration: {duration}s")
            summary_text = self._call_gemini_text(prompt, temperature=0.3, max_output_tokens=2000)
            
            if not summary_text:
                logger.error("Gemini API error - unable to generate summary")
                return {"error": f"Gemini API error - unable to generate summary"}
            
            logger.debug(f"Summary API response received - Length: {len(summary_text)} chars")

            # First try our internal JSON parser
            parsed_summary = self._parse_summary_json(summary_text)
            
            # Validate that parsed_summary is a proper dictionary
            if parsed_summary is not None:
                if not isinstance(parsed_summary, dict):
                    logger.error(f"Parsed summary is not a dictionary: {type(parsed_summary)} - falling back to text extraction")
                    parsed_summary = None
                elif 'call_summary' not in parsed_summary and not any(key in parsed_summary for key in ['key_discussion_points', 'prospect_questions', 'prospect_concerns', 'next_steps']):
                    logger.error(f"Parsed summary missing expected keys - falling back to text extraction")
                    parsed_summary = None

            # Fallback 1: try gemini_client-style JSON extraction to recover valid JSON
            if parsed_summary is None:
                try:
                    from gemini_client import extract_json_from_text as _ext_json  # script/run-relative
                except ImportError:
                    try:
                        from .gemini_client import extract_json_from_text as _ext_json  # package-relative
                    except ImportError:
                        _ext_json = None

                if _ext_json is not None:
                    cleaned = _ext_json(summary_text)
                    if cleaned:
                        try:
                            parsed_candidate = json.loads(cleaned)
                            if isinstance(parsed_candidate, dict):
                                parsed_summary = parsed_candidate
                        except json.JSONDecodeError:
                            # JSON parsing failed, will fall back to manual text extraction below
                            pass

            if parsed_summary is None:
                # Log detailed debug info to understand WHY JSON parsing failed
                text_sample = summary_text[:400]
                open_braces = summary_text.count("{")
                close_braces = summary_text.count("}")
                logger.error(
                    "Summary JSON parsing failed; falling back to text extraction. "
                    "Length=%d, open_braces=%d, close_braces=%d, startswith=%r, sample(first 400 chars)=%r",
                    len(summary_text),
                    open_braces,
                    close_braces,
                    summary_text.lstrip()[:1],
                    text_sample,
                )

                # HYBRID FALLBACK: Extract structured data from raw LLM text response
                full_text = summary_text.strip()
                
                logger.warning("JSON parsing failed - using hybrid fallback to extract data from text")
                
                # Extract specific sections from LLM text response
                extracted_call_summary = self._extract_call_summary_from_text(full_text)
                extracted_recommendations = self._extract_recommendations_from_text(full_text)
                
                # Extract structured data (NO LIMITS - all items extracted)
                extracted_points = self._extract_simple_points_from_text(full_text)
                extracted_questions = self._extract_questions_from_text(full_text)
                extracted_concerns = self._extract_concerns_from_text(full_text)
                extracted_next_steps = self._extract_next_steps_from_text(full_text)
                extracted_info = self._extract_business_info_from_text(full_text)
                
                # Build fallback response with extracted data
                # If we still don't have a clear call_summary, fall back to trimmed full LLM text
                call_summary_val = extracted_call_summary if extracted_call_summary else full_text[:2000]
                
                fallback_summary = {
                    "call_summary": call_summary_val,
                    "key_discussion_points": extracted_points if extracted_points else [],
                    "prospect_questions": extracted_questions if extracted_questions else [],
                    "prospect_concerns": extracted_concerns if extracted_concerns else [],
                    "next_steps": extracted_next_steps if extracted_next_steps else [],
                    "business_name": extracted_info.get('business_name'),
                    "contact_person": extracted_info.get('contact_person'),
                    "phone_number": extracted_info.get('phone_number'),
                    "recommendations": extracted_recommendations if extracted_recommendations else "",
                }
                
                logger.info(
                    f"Hybrid fallback extracted - Summary: {len(call_summary_val)} chars, "
                    f"Points: {len(extracted_points)}, Questions: {len(extracted_questions)}, "
                    f"Concerns: {len(extracted_concerns)}, Next Steps: {len(extracted_next_steps)}"
                )
                
                # Normalize types so all columns store clean, structured data
                return self._normalize_summary_fields(fallback_summary)

            normalized_summary = self._normalize_summary_fields(parsed_summary)
            logger.info(f"Summary generation complete - Key points: {len(normalized_summary.get('key_discussion_points', []))}, Questions: {len(normalized_summary.get('prospect_questions', []))}")
            return normalized_summary
            
        except Exception as e:
            logger.error(f"Summary generation error: {str(e)}", exc_info=True)
            return {"error": f"Gemini API error: {str(e)}"}
    
    def calculate_lead_score(self, conversation_text: str, sentiment: Dict, summary: Dict, duration_seconds: int) -> Dict:
        """
        Calculate lead score from 0-10 based on multiple factors
        Runs AFTER call - no performance impact
        """
        
        logger.debug("Calculating lead score...")
        score = 0.0
        max_score = 10.0
        scoring_breakdown = {}
        
        # 1. Sentiment Score (0-3 points)
        try:
            combined_score_str = sentiment.get('combined_score', '0.0%')
            combined_score = float(combined_score_str.replace('%', '')) / 100.0
            
            if combined_score >= 0.6:
                sentiment_points = 3.0
            elif combined_score >= 0.3:
                sentiment_points = 2.0
            elif combined_score >= 0:
                sentiment_points = 1.0
            else:
                sentiment_points = 0.0
            
            score += sentiment_points
            scoring_breakdown['sentiment'] = f"{sentiment_points}/3.0"
        except:
            scoring_breakdown['sentiment'] = "0/3.0"
        
        # 2. Engagement Level (0-2 points) - based on conversation length
        # BUT penalized if sentiment is negative (filters nuisance calls)
        word_count = len(conversation_text.split())
        
        if word_count >= 200:
            base_engagement = 2.0
        elif word_count >= 100:
            base_engagement = 1.5
        elif word_count >= 50:
            base_engagement = 1.0
        else:
            base_engagement = 0.5
        
        # Apply sentiment penalty for negative calls
        # If sentiment is negative, reduce engagement points by 50%
        if combined_score < 0:
            engagement_points = base_engagement * 0.5  # 50% penalty
        elif combined_score < 0.2:
            engagement_points = base_engagement * 0.75  # 25% penalty
        else:
            engagement_points = base_engagement
        
        score += engagement_points
        scoring_breakdown['engagement'] = f"{engagement_points}/2.0 ({word_count} words)"
        
        # 3. Duration Quality (0-1 point)
        # BUT penalized if sentiment is negative (filters long nuisance calls)
        if duration_seconds >= 180:  # 3+ minutes
            base_duration = 1.0
        elif duration_seconds >= 120:  # 2+ minutes
            base_duration = 0.7
        elif duration_seconds >= 60:  # 1+ minute
            base_duration = 0.4
        else:
            base_duration = 0.1
        
        # Apply sentiment penalty for negative calls
        # If sentiment is negative, reduce duration points by 50%
        if combined_score < 0:
            duration_points = base_duration * 0.5  # 50% penalty
        elif combined_score < 0.2:
            duration_points = base_duration * 0.75  # 25% penalty
        else:
            duration_points = base_duration
        
        score += duration_points
        scoring_breakdown['duration'] = f"{duration_points}/1.0 ({duration_seconds}s)"
        
        # 4. Information Provided (0-2 points)
        info_points = 0.0
        if summary.get('business_name'):
            info_points += 0.7
        if summary.get('contact_person'):
            info_points += 0.7
        if summary.get('phone_number'):
            info_points += 0.6
        
        score += info_points
        scoring_breakdown['information_provided'] = f"{info_points}/2.0"
        
        # 5. Next Steps Agreed (0-2 points)
        next_steps = summary.get('next_steps', [])
        if len(next_steps) >= 2:
            next_steps_points = 2.0
        elif len(next_steps) == 1:
            next_steps_points = 1.0
        else:
            next_steps_points = 0.0
        
        score += next_steps_points
        scoring_breakdown['next_steps'] = f"{next_steps_points}/2.0 ({len(next_steps)} steps)"
        
        final_score = round(min(score, max_score), 1)
        logger.debug(f"Lead score calculated: {final_score}/10 - Breakdown: {scoring_breakdown}")
        
        # Categorize lead quality
        if final_score >= 8.0:
            lead_category = "Hot Lead"
            priority = "High"
        elif final_score >= 6.0:
            lead_category = "Warm Lead"
            priority = "Medium"
        elif final_score >= 4.0:
            lead_category = "Warm Lead"
            priority = "Low"
        else:
            lead_category = "Cold Lead"
            priority = "Very Low"
        
        return {
            "lead_score": final_score,
            "max_score": max_score,
            "lead_category": lead_category,
            "priority": priority,
            "scoring_breakdown": scoring_breakdown
        }
    
    def calculate_conversation_quality(self, conversation_text: str, sentiment: Dict, duration_seconds: int) -> Dict:
        """
        Calculate conversation quality metrics
        Runs AFTER call - no performance impact
        """
        
        logger.debug("Calculating conversation quality metrics...")
        user_text = self._extract_user_messages(conversation_text)
        
        # Count turns (user responses)
        user_turns = len([line for line in conversation_text.split('\n') if line.strip().startswith('User:')])
        agent_turns = len([line for line in conversation_text.split('\n') if line.strip().startswith('Agent:')])
        
        # Calculate response rate
        if agent_turns > 0:
            response_rate = round((user_turns / agent_turns) * 100, 1)
        else:
            response_rate = 0.0
        
        # Average response length
        if user_turns > 0:
            avg_user_response_length = len(user_text.split()) / user_turns
        else:
            avg_user_response_length = 0
        
        # Engagement indicators
        questions_asked = user_text.count('?')
        
        # Quality assessment
        if user_turns >= 5 and avg_user_response_length >= 10:
            quality_rating = "Excellent"
        elif user_turns >= 3 and avg_user_response_length >= 5:
            quality_rating = "Good"
        elif user_turns >= 2:
            quality_rating = "Fair"
        else:
            quality_rating = "Poor"
        
        return {
            "quality_rating": quality_rating,
            "conversation_turns": {
                "user_turns": user_turns,
                "agent_turns": agent_turns,
                "total_turns": user_turns + agent_turns
            },
            "response_rate": f"{response_rate}%",
            "avg_user_response_length": round(avg_user_response_length, 1),
            "questions_asked_by_user": questions_asked,
            "engagement_level": "High" if user_turns >= 4 else "Medium" if user_turns >= 2 else "Low"
        }
    
    def parse_conversation_transcript(self, conversation_log: List, call_start_time) -> List[Dict]:
        """
        Parse conversation log into structured transcript with REAL timestamps
        
        Args:
            conversation_log: List of dicts with {role, message, timestamp}
            call_start_time: datetime when call started
        
        Returns:
            List of dicts with {timestamp, role, message} using real timestamps
        """
        transcript = []
        
        for entry in conversation_log:
            # Calculate elapsed time from call start
            message_time = entry.get('timestamp')
            if message_time and call_start_time:
                elapsed_seconds = (message_time - call_start_time).total_seconds()
                
                # Format as MM:SS
                minutes = int(elapsed_seconds // 60)
                seconds = int(elapsed_seconds % 60)
                timestamp = f"{minutes:02d}:{seconds:02d}"
            else:
                timestamp = "00:00"
            
            transcript.append({
                "timestamp": timestamp,
                "role": entry.get('role', 'unknown'),
                "message": entry.get('message', '')
            })
        
        return transcript
    
    def parse_conversation_transcript_legacy(self, conversation_text: str, duration_seconds: int) -> List[Dict]:
        """
        LEGACY: Parse conversation text (old format) into transcript with estimated timestamps
        Used for backwards compatibility with old string-based conversation logs
        """
        transcript = []
        lines = conversation_text.strip().split('\n')
        
        # Calculate time per message (distribute evenly)
        num_messages = len([l for l in lines if l.strip()])
        time_per_message = duration_seconds / num_messages if num_messages > 0 else 0
        
        current_time = 0
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse role and message
            if line.startswith("User:"):
                role = "user"
                message = line.replace("User:", "").strip()
            elif line.startswith("Agent:"):
                role = "agent"
                message = line.replace("Agent:", "").strip()
            else:
                # Skip lines that don't match expected format
                continue
            
            # Format timestamp as MM:SS
            minutes = int(current_time // 60)
            seconds = int(current_time % 60)
            timestamp = f"{minutes:02d}:{seconds:02d}"
            
            transcript.append({
                "timestamp": timestamp,
                "role": role,
                "message": message
            })
            
            current_time += time_per_message
        
        return transcript
    
    def extract_call_stages(self, conversation_text: str, summary: Dict, tenant_id: str = None) -> Dict:
        """
        Extract call stage information using the dedicated stage detector.
        
        Args:
            conversation_text: The formatted conversation transcript
            summary: Optional summary data
            tenant_id: Optional tenant ID for tenant-specific stage mapping
            
        Returns:
            Dict containing stage information
        """
        return stage_detector.extract_call_stages(conversation_text, summary, tenant_id)
    
    async def analyze_call(self, call_id: str, conversation_log, duration_seconds: int, call_start_time=None, tenant_id: str = None) -> Dict:
        """Complete call analysis - sentiment + summarization + scoring
        
        Args:
            call_id: Unique call identifier
            conversation_log: List of dicts with {role, message, timestamp} OR string (legacy)
            duration_seconds: Call duration
            call_start_time: datetime when call started (for real timestamps)
            tenant_id: Optional tenant ID for tenant-specific stage mapping
        """
        
        logger.info(f"Analyzing call {call_id}...")
        
        # Handle both new (structured) and old (string) formats
        if isinstance(conversation_log, dict):
            # Dict format - check for voiceagent 'segments' first
            if 'segments' in conversation_log:
                conversation_text = self._parse_voiceagent_segments(conversation_log['segments'])
            elif 'conversation' in conversation_log:
                conversation_text = conversation_log['conversation']
            elif 'messages' in conversation_log:
                # List of messages in dict
                messages = conversation_log['messages']
                if isinstance(messages, list):
                    conversation_text = "\n".join([f"{msg.get('role', 'Unknown').title()}: {msg.get('message', '')}" for msg in messages])
                else:
                    conversation_text = str(messages)
            else:
                # Try to convert entire dict to readable format
                conversation_text = str(conversation_log)
        elif isinstance(conversation_log, list) and len(conversation_log) > 0 and isinstance(conversation_log[0], dict):
            # Check if it's voiceagent segments format (has 'speaker' and 'text' fields)
            if 'speaker' in conversation_log[0] and 'text' in conversation_log[0]:
                conversation_text = self._parse_voiceagent_segments(conversation_log)
            else:
                # New format: List of dicts with timestamps
                # Convert to text for analytics
                conversation_text = "\n".join([f"{entry['role'].title()}: {entry['message']}" for entry in conversation_log])
        else:
            # Legacy format: String with "User:" and "Agent:" lines
            if isinstance(conversation_log, list):
                conversation_text = "\n".join(conversation_log)
            else:
                conversation_text = str(conversation_log)
        
        word_count = len(conversation_text.split())
        logger.info(f"Processing call - Word count: {word_count}, Duration: {duration_seconds}s, Text length: {len(conversation_text)} chars")

        
        # Check if conversation has any meaningful user speech
        has_user_speech = False
        
        # Check source log structure first (more reliable)
        if isinstance(conversation_log, list) and len(conversation_log) > 0 and isinstance(conversation_log[0], dict):
            # Check for any user message with actual text
            for msg in conversation_log:
                role = msg.get('role', '') or msg.get('speaker', '')
                text = msg.get('message', '') or msg.get('text', '')
                if role.lower() == 'user' and text and len(text.strip()) > 0:
                    has_user_speech = True
                    break
        elif isinstance(conversation_log, dict):
            # Dict format - check segments or messages
            if 'segments' in conversation_log:
                for seg in conversation_log['segments']:
                    if seg.get('speaker', '').lower() == 'user' and seg.get('text', ''):
                        has_user_speech = True
                        break
            elif 'messages' in conversation_log:
                for msg in conversation_log['messages']:
                    role = msg.get('role', '') or msg.get('speaker', '')
                    if role.lower() == 'user' and (msg.get('message') or msg.get('text')):
                        has_user_speech = True
                        break
        else:
            # Fallback to checking parsed text
            if "User:" in conversation_text:
                has_user_speech = True

        # If no user speech detected, return default values (SKIP LLM)
        if not has_user_speech:
            logger.info("No user speech detected - bypassing LLM analysis with default values")
            
            # Default values as requested
            default_summary = {
                "call_summary": "Prospect did not provide enough conversation to make the summary",
                "key_discussion_points": [],
                "prospect_questions": [],
                "prospect_concerns": [],
                "next_steps": [],
                "recommendations": "Prospect doesn't provide enough response to make the recommendations\n--- AI ANALYTICS RECOMMENDATION SUMMARY ---\nOverall Lead Status: Cold Lead (6.0/10)\nEngagement Level: Low\nFarthest Stage Reached: followup \nProspect Sentiment: Neutral",
                "business_name": None,
                "contact_person": None,
                "phone_number": None
            }
            
            default_sentiment = {
                "category": "Neutral",
                "sentiment_description": "Prospect did not provide enough conversation to make the sentiment",
                "key_phrases": []
            }
            
            default_disposition = {
                "disposition": "NURTURE (7 DAYS)",
                "recommended_action": "Attempt a follow-up call in 7 days to establish a two-way conversation .",
                "reasoning": "No user response recorded",
                "decision_confidence": "High"
            }
            
            default_lead_score = {
                "lead_score": 6.0, 
                "lead_category": "Cold Lead",
                "priority": "Low",
                "scoring_breakdown": {"reason": "No user speech detected"}
            }

            zero_cost = {
                "total_api_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_usd_formatted": "$0",
                "pricing_model": "Skipped (No User Speech)"
            }

            return {
                "call_id": call_id,
                "timestamp": datetime.now().isoformat(),
                "duration_seconds": duration_seconds,
                "lead_disposition": default_disposition,
                "sentiment": default_sentiment,
                "summary": default_summary,
                "lead_score": default_lead_score,
                "quality_metrics": {
                    "overall_score": 0,
                    "engagement_level": "Low",
                    "quality_rating": "Poor",
                    "clarity": "N/A",
                    "objection_handling": "N/A",
                    "conversation_turns": {"user_turns": 0, "bot_turns": 0},
                    "response_rate": "0%",
                    "avg_user_response_length": 0,
                    "questions_asked_by_user": 0,
                }, 
                "stage_info": stage_detector.get_default_stage_info(),
                "lead_info": None,
                "lead_info_path": None,
                "conversation_length": len(conversation_text),
                "word_count": word_count,
                "cost": zero_cost
            }

        
        # ===== MEANINGFUL USER TRANSCRIPTION CHECK =====
        logger.info("Checking for meaningful user transcription...")
        has_meaningful_transcription = stage_detector.has_meaningful_user_transcription(conversation_text)
        
        if not has_meaningful_transcription:
            logger.info("No meaningful user transcription - using same defaults as no user transcription")
            
            # Default values as requested (same as no user transcription)
            default_summary = {
                "call_summary": "Prospect did not provide enough conversation to make the summary",
                "key_discussion_points": [],
                "prospect_questions": [],
                "prospect_concerns": [],
                "next_steps": [],
                "recommendations": "Prospect doesn't provide enough response to make the recommendations\n--- AI ANALYTICS RECOMMENDATION SUMMARY ---\nOverall Lead Status: Cold Lead (6.0/10)\nEngagement Level: Low\nFarthest Stage Reached: followup \nProspect Sentiment: Neutral",
                "business_name": None,
                "contact_person": None,
                "phone_number": None
            }
            
            default_sentiment = {
                "category": "Neutral",
                "sentiment_description": "Prospect did not provide enough conversation to make the sentiment",
                "key_phrases": []
            }
            
            default_disposition = {
                "disposition": "NURTURE (7 DAYS)",
                "recommended_action": "Attempt a follow-up call in 7 days to establish a two-way conversation .",
                "reasoning": "No user response recorded",
                "decision_confidence": "High"
            }
            
            default_lead_score = {
                "lead_score": 6.0, 
                "lead_category": "Cold Lead",
                "priority": "Low",
                "scoring_breakdown": {"reason": "No user speech detected"}
            }

            zero_cost = {
                "total_api_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_usd_formatted": "$0",
                "pricing_model": "Skipped (No User Speech)"
            }

            # Return the complete analysis with default values
            return {
                "call_id": call_id,
                "timestamp": datetime.now().isoformat(),
                "duration_seconds": duration_seconds,
                "lead_disposition": default_disposition,
                "sentiment": default_sentiment,
                "summary": default_summary,
                "lead_score": default_lead_score,
                "quality_metrics": {
                    "overall_score": 0,
                    "engagement_level": "Low",
                    "quality_rating": "Poor",
                    "clarity": "N/A",
                    "objection_handling": "N/A",
                    "conversation_turns": {"user_turns": 0, "bot_turns": 0},
                    "response_rate": "0%",
                    "avg_user_response_length": 0,
                    "questions_asked_by_user": 0,
                }, 
                "stage_info": stage_detector.get_default_stage_info(),
                "lead_info": None,
                "lead_info_path": None,
                "conversation_length": len(conversation_text),
                "word_count": word_count,
                "cost": zero_cost
            }
        else:
            # ===== PARALLEL PHASE: Sentiment + Lead Info Extraction =====
            # These two are independent and can run in parallel
            logger.info("Step 1/7: Analyzing sentiment + Step 7/7: Extracting lead info (PARALLEL)...")
            
            async def extract_lead_info_safe():
                """Wrapper for lead info extraction with error handling"""
                logger.info(f"Lead extractor available: {self.lead_extractor is not None}")
                logger.info(f"LEAD_EXTRACTOR_AVAILABLE: {LEAD_EXTRACTOR_AVAILABLE}")
                if not self.lead_extractor:
                    logger.debug("Lead extractor not available - skipping lead info extraction")
                    return None, None
                try:
                    # Pass None for summary since we don't have it yet - lead extraction doesn't strictly need it
                    logger.info("Starting lead information extraction...")
                    lead_info = await self.lead_extractor.extract_lead_information(conversation_text, None)
                    lead_info_path = None
                    if lead_info:
                        lead_info_path = self.lead_extractor.save_to_json(lead_info, call_id)
                        logger.info(f"Lead info extracted: {len(lead_info)} fields, saved to: {lead_info_path}")
                    else:
                        logger.info("No lead information found in this call")
                    return lead_info, lead_info_path
                except Exception as e:
                    logger.error(f"Lead info extraction failed: {e}", exc_info=True)
                    return None, None
            
            # Run sentiment analysis and lead info extraction in parallel
            sentiment, (lead_info, lead_info_path) = await asyncio.gather(
                self.analyze_sentiment(conversation_text, duration=duration_seconds, word_count=word_count),
                extract_lead_info_safe()
            )
            
            # ===== SEQUENTIAL PHASE: Summary → Stages → Disposition (dependencies) =====
            logger.info("Step 2/7: Generating summary...")
            summary = await self.generate_summary(conversation_text, sentiment_data=sentiment, duration=duration_seconds)
            
            logger.info("Step 3/7: Extracting call stages...")
            stage_info = self.extract_call_stages(conversation_text, summary, tenant_id)
            
            logger.info("Step 4/7: Determining lead disposition...")
            lead_disposition = await self._determine_lead_disposition_llm(sentiment, summary, stage_info)
            logger.info(f"Lead disposition: {lead_disposition.get('disposition', 'Unknown')} - Action: {lead_disposition.get('recommended_action', 'N/A')}")

        # ===== NON-LLM STEPS (fast, no API calls) =====
        logger.info("Step 5/7: Calculating lead score from disposition...")
        lead_score = self._calculate_lead_score_from_disposition(lead_disposition)
        logger.info(f"Lead score: {lead_score.get('lead_score', 0)}/10 - Category: {lead_score.get('lead_category', 'Unknown')}")

        logger.info("Step 6/7: Calculating quality metrics...")
        quality_metrics = self.calculate_conversation_quality(conversation_text, sentiment, duration_seconds)

        # Update lead category based on tenant-specific logic
        GLINKS_TENANT_ID = os.getenv("GLINKS_TENANT_ID", "926070b5-189b-4682-9279-ea10ca090b84")
        
        if tenant_id == GLINKS_TENANT_ID:
            # GLINKS: Use stage-based logic
            lead_score = self._update_lead_category_based_on_stages(lead_score, stage_info, tenant_id)
            logger.info(f"GLINKS updated lead score: {lead_score.get('lead_score', 0)}/10 - Category: {lead_score.get('lead_category', 'Unknown')}")
        else:
            # Non-GLINKS: Use sentiment + duration logic
            lead_score = self._update_lead_category_for_non_glinks(lead_score, sentiment, duration_seconds)
            logger.info(f"Non-GLINKS updated lead score: {lead_score.get('lead_score', 0)}/10 - Category: {lead_score.get('lead_category', 'Unknown')}")

        # Lead info already extracted in parallel phase above

        # Enhance recommendations with complete analytics data
        if summary and 'recommendations' in summary and lead_score:
            enhanced_rec = self._enhance_recommendations(
                summary.get('recommendations', ''),
                lead_score,
                sentiment,
                quality_metrics,
                stage_info,
                duration_seconds
            )
            summary['recommendations'] = enhanced_rec

        cost_info = self._calculate_llm_cost()
        logger.info(f"LLM cost for this call: ${cost_info.get('cost_usd', 0):.6f} ({cost_info.get('total_tokens', 0)} tokens)")

        # Combine results (transcript removed - keeping only analytics)
        result = {
            "call_id": call_id,
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": duration_seconds,
            "lead_disposition": lead_disposition,  # Clear YES/NO decision for quick filtering
            "sentiment": sentiment,
            "summary": summary,
            "lead_score": lead_score,
            "quality_metrics": quality_metrics,
            "stage_info": stage_info,
            "lead_info": lead_info,  # Extracted lead information (may be None)
            "lead_info_path": lead_info_path,  # Path to saved JSON file (may be None)
            "conversation_length": len(conversation_text),
            "word_count": len(conversation_text.split()),
            "cost": cost_info
        }
        
        logger.info(f"Call analysis complete - Call ID: {call_id}, Lead Score: {lead_score.get('lead_score', 0)}/10, Disposition: {lead_disposition.get('disposition', 'Unknown')}")
        
        self.cost_tracker = {
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'api_calls': 0
        }
        
        return result
    
    def save_transcript_txt(self, analysis: Dict) -> str:
        """Save conversation transcript as a formatted TXT file"""
        
        call_id = analysis.get('call_id', 'UNKNOWN')
        timestamp = analysis.get('timestamp', datetime.now().isoformat())
        duration_seconds = analysis.get('duration_seconds', 0)
        transcript = analysis.get('conversation_transcript', [])
        sentiment = analysis.get('sentiment', {})
        advanced_emotions = sentiment.get('advanced_emotions', {})
        
        # Format duration
        duration_minutes = duration_seconds // 60
        duration_seconds_remainder = duration_seconds % 60
        
        # Parse timestamp
        try:
            dt = datetime.fromisoformat(timestamp)
            date_str = dt.strftime("%B %d, %Y")
            time_start_str = dt.strftime("%H:%M:%S")
        except:
            date_str = "Unknown Date"
            time_start_str = "Unknown Time"
        
        # Build TXT content
        lines = []
        lines.append("╔" + "═" * 62 + "╗")
        lines.append("║" + " " * 20 + "CALL TRANSCRIPT" + " " * 27 + "║")
        lines.append("╚" + "═" * 62 + "╝")
        lines.append("")
        lines.append(f"Call ID: {call_id}")
        lines.append(f"Date: {date_str}")
        lines.append(f"Time: {time_start_str}")
        lines.append(f"Duration: {duration_minutes} minutes {duration_seconds_remainder} seconds")
        lines.append("")
        lines.append("─" * 64)
        lines.append("")
        
        # Add conversation
        for entry in transcript:
            timestamp_str = entry.get('timestamp', '00:00')
            role = entry.get('role', 'unknown')
            message = entry.get('message', '')
            
            # Format role label
            if role == "agent":
                role_label = "Agent (Nithya):"
            elif role == "user":
                role_label = "Lead:"
            else:
                role_label = f"{role.title()}:"
            
            # Write timestamp and role
            lines.append(f"[{timestamp_str}] {role_label}")
            
            # Word wrap message to 60 characters
            words = message.split()
            current_line = ""
            for word in words:
                if len(current_line) + len(word) + 1 <= 60:
                    current_line += word + " "
                else:
                    lines.append(current_line.strip())
                    current_line = word + " "
            if current_line:
                lines.append(current_line.strip())
            
            lines.append("")  # Blank line between messages
        
        lines.append("─" * 64)
        lines.append("")
        lines.append("CALL SUMMARY")
        lines.append("")
        
        # Add sentiment info
        sentiment_desc = sentiment.get('sentiment_description', 'N/A')
        combined_score = sentiment.get('combined_score', 'N/A')
        
        lines.append(f"Sentiment: {combined_score}")
        
        # Add emotion info
        if advanced_emotions:
            emotion = advanced_emotions.get('emotion', 'N/A')
            emotion_conf = advanced_emotions.get('confidence', 0)
            lines.append(f"Main Emotion: {emotion.title()} ({emotion_conf:.0%} confidence)")
        
        lines.append("")
        
        # Add lead score
        lead_score = analysis.get('lead_score', {})
        if lead_score:
            score = lead_score.get('score', 'N/A')
            category = lead_score.get('category', 'N/A')
            lines.append(f"Lead Score: {score}/10 ({category})")
            lines.append("")
        
        # Add summary info
        summary = analysis.get('summary', {})
        if summary:
            next_steps = summary.get('next_steps', [])
            if next_steps:
                lines.append("Key Outcome:")
                for step in next_steps:
                    lines.append(f"• {step}")
                lines.append("")
        
        lines.append("─" * 64)
        lines.append("End of Transcript")
        lines.append("─" * 64)
        
        # Save to file
        filename = f"transcript_{call_id}.txt"
        filepath = os.path.join(os.path.dirname(__file__), filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        return filepath
    
    def save_analysis(self, analysis: Dict, filename: Optional[str] = None) -> str:
        """Save analysis to JSON file"""
        
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"call_analysis_{analysis['call_id']}_{timestamp}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        
        return filename
    
    
    def _determine_sentiment_category(self, combined_score: str) -> str:
        """
        Determine sentiment category from combined score
        
        Args:
            combined_score: Score as string with % (e.g., "61.0%")
            
        Returns:
            Sentiment category: "Positive", "Neutral", or "Negative"
        """
        try:
            score = float(combined_score.replace("%", ""))
            if score >= 60:
                return "Positive"
            elif score >= 40:
                return "Neutral"
            else:
                return "Negative"
        except:
            return "Neutral"
    
    def _calculate_quality_score(self, quality_metrics: Dict) -> float:
        """
        Calculate numeric quality score from quality_rating
        
        Args:
            quality_metrics: Quality metrics dictionary
            
        Returns:
            Quality score (0-10)
        """
        rating = quality_metrics.get("quality_rating", "Average")
        rating_map = {
            "Excellent": 9.0,
            "Good": 7.5,
            "Average": 5.0,
            "Poor": 3.0
        }
        return rating_map.get(rating, 5.0)
    
    def _extract_call_log_id(self, call_id: str) -> Optional[int]:
        """
        Extract call_log_id from call_id string
        
        Args:
            call_id: String like "DB_1_20251029_123045" or "MANUAL_20251029_123045"
            
        Returns:
            Extracted ID as integer, or None if cannot extract
        """
        try:
            # For DB calls: "DB_1_20251029_123045" -> extract "1"
            if call_id.startswith("DB_"):
                parts = call_id.split("_")
                if len(parts) >= 2:
                    return int(parts[1])
            
            # For other formats, return None (no call_log_id available)
            return None
        except:
            return None
    
    async def save_to_database(
        self,
        analysis: Dict,
        call_log_id: str,
        db_config: Dict,
    ) -> bool:
        """
        Legacy method for backward compatibility - calls save_to_db
        
        Args:
            analysis: The complete analytics dictionary
            call_log_id: UUID of the call log (from {SCHEMA}.voice_call_logs)
            db_config: Database configuration dictionary (not used, tenant_id extracted from analysis)
            
        Returns:
            bool: True if saved successfully
        """
        # Extract tenant_id from analysis for database save
        tenant_id = analysis.get("tenant_id")
        if not tenant_id:
            logger.warning("No tenant_id found in analysis - cannot save to database")
            return False
            
        return await self.save_to_db(analysis, call_log_id, tenant_id)
    
    async def save_to_db(
        self,
        analysis: Dict,
        call_log_id: str,
        tenant_id: str,
    ) -> bool:
        """
        Save analytics to {SCHEMA}.voice_call_analysis with full legacy-parity columns.
        
        Args:
            analysis: The complete analytics dictionary
            call_log_id: UUID of the call log (from {SCHEMA}.voice_call_logs)
            tenant_id: UUID of the tenant for multi-tenancy
            
        Returns:
            bool: True if saved successfully
        """
        if not STORAGE_CLASSES_AVAILABLE:
            logger.error("DB config helpers not available - cannot save to database")
            return False

        try:
            from db.db_config import get_db_config

            db_config = get_db_config()
            analysis = dict(analysis or {})
            analysis["tenant_id"] = tenant_id

            # Full-column persistence into {SCHEMA}.voice_call_analysis.
            # This updates: disposition, lead_category, recommendations, key_phrases, cost, etc.
            
            # Extract data for voice_call_analysis table
            summary = analysis.get("summary", {})
            sentiment = analysis.get("sentiment", {})
            lead = analysis.get("lead_score", {})
            quality = analysis.get("quality_metrics", {})
            stage = analysis.get("stage_info", {})
            disposition = analysis.get("lead_disposition", {})
            cost_data = analysis.get("cost", {})
            
            # Prepare INSERT query - matches FULL {SCHEMA}.voice_call_analysis schema
            query = f"""
                INSERT INTO {SCHEMA}.voice_call_analysis (
                    call_log_id,
                    tenant_id,
                    summary,
                    sentiment,
                    key_phrases,
                    key_discussion_points,
                    prospect_questions,
                    prospect_concerns,
                    recommendations,
                    lead_category,
                    engagement_level,
                    stages_reached,
                    disposition,
                    recommended_action,
                    cost,
                    key_points,
                    lead_extraction,
                    raw_analysis,
                    analysis_cost
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """
            
            # Build key_points as JSONB from summary data
            key_points_data = summary.get("key_discussion_points", [])
            if isinstance(key_points_data, str):
                key_points_data = [key_points_data] if key_points_data else []
            
            # Build lead_extraction from lead info (special column for extracted lead details)
            lead_extraction_data = analysis.get("lead_info", {})
            
            # Build raw_analysis with all detailed data for backup
            raw_analysis_data = {
                "sentiment_full": sentiment,
                "lead_score_full": lead,
                "quality_metrics_full": quality,
                "stage_info_full": stage,
                "disposition_full": disposition,
                "cost_full": cost_data,
            }
            
            # Json() parameters
            
            # Ensure all Json() parameters are proper dicts
            if not isinstance(key_points_data, (dict, list)):
                logger.warning(f"key_points_data is not dict/list, converting: {type(key_points_data)}")
                key_points_data = {"data": key_points_data}
            
            if not isinstance(lead_extraction_data, dict):
                logger.warning(f"lead_extraction_data is not dict, converting: {type(lead_extraction_data)}")
                lead_extraction_data = {"data": lead_extraction_data}
                
            if not isinstance(raw_analysis_data, dict):
                logger.warning(f"raw_analysis_data is not dict, converting: {type(raw_analysis_data)}")
                raw_analysis_data = {"data": raw_analysis_data}
            
            # Extract cost values - use USD directly (INR conversion removed)
            # Default to 0.00 for no-user-speech calls (not NULL)
            cost_numeric = 0.0
            analysis_cost_value = 0.0
            if cost_data:
                # Use cost_usd directly (INR was removed as legacy)
                cost_usd = cost_data.get("cost_usd", 0.0)
                if cost_usd is not None:
                    cost_numeric = round(float(cost_usd), 5)
                    analysis_cost_value = cost_numeric

            # Prepare text fields for old columns (convert lists/dicts to text)
            def to_text(val):
                if val is None:
                    return ""
                if isinstance(val, (list, dict)):
                    return json.dumps(val, ensure_ascii=False)
                return str(val)
            
            # stages_reached value
            stages_reached_value = stage.get("stages_reached", [])
            
            # Use detected stages_reached directly
            
            # Final stages_reached to save
            
            # Build values tuple for database INSERT
            
            # Create Json() objects
            
            try:
                key_points_json = Json(key_points_data)
            except Exception as json_error:
                logger.error(f"ERROR creating Json(key_points_data): {json_error}")
                raise json_error
                
            try:
                lead_extraction_json = Json(lead_extraction_data)
            except Exception as json_error:
                logger.error(f"ERROR creating Json(lead_extraction_data): {json_error}")
                raise json_error
                
            try:
                raw_analysis_json = Json(raw_analysis_data)
            except Exception as json_error:
                logger.error(f"ERROR creating Json(raw_analysis_data): {json_error}")
                raise json_error
            
            # All Json() objects created successfully
            
            values = (
                call_log_id,                                              # call_log_id
                tenant_id,                                                # tenant_id (NEW)
                summary.get("call_summary", ""),                          # summary
                sentiment.get("sentiment_description", ""),               # sentiment
                to_text(sentiment.get("key_phrases", [])),                # key_phrases (TEXT)
                to_text(summary.get("key_discussion_points", [])),        # key_discussion_points (TEXT)
                to_text(summary.get("prospect_questions", [])),           # prospect_questions (TEXT)
                to_text(summary.get("prospect_concerns", [])),            # prospect_concerns (TEXT)
                summary.get("recommendations", ""),                       # recommendations
                lead.get("lead_category", ""),                            # lead_category (TEXT)
                quality.get("engagement_level", ""),                      # engagement_level (TEXT)
                to_text(stages_reached_value),                            # stages_reached (TEXT) - FIXED: Use corrected value
                disposition.get("disposition", ""),                        # disposition (TEXT)
                disposition.get("recommended_action", ""),                 # recommended_action
                cost_numeric,                                             # cost (numeric)
                key_points_json,                                          # key_points (JSONB, NEW)
                lead_extraction_json,                                     # lead_extraction (JSONB, NEW)
                raw_analysis_json,                                         # raw_analysis (JSONB, NEW)
                analysis_cost_value                                      # analysis_cost (numeric, NEW)
            )
            
            # Values tuple created
            
            # Debug logging to check values before database operation

            # Execute the INSERT
            try:
                conn = psycopg2.connect(**db_config)
                cursor = conn.cursor()
                
                if call_log_id:
                    cursor.execute(
                        f"DELETE FROM {SCHEMA}.voice_call_analysis WHERE call_log_id = %s::uuid",
                        (call_log_id,),
                    )
                
                cursor.execute(query, values)
                conn.commit()
                logger.info(f"DEBUG: Database INSERT executed successfully")
                
            except Exception as db_error:
                logger.error(f"DATABASE INSERT ERROR: {db_error}")
                logger.error(f"Error type: {type(db_error).__name__}")
                
                # Log each value with its type to identify the problematic one
                for i, value in enumerate(values):
                    logger.error(f"Value {i}: type={type(value)}, repr={repr(value)}")
                
                # Try to identify which value is causing the issue
                for i, value in enumerate(values):
                    if isinstance(value, dict):
                        logger.error(f"FOUND DICT at position {i}: {value}")
                    elif isinstance(value, list):
                        logger.error(f"FOUND LIST at position {i}: {value}")
                
                raise db_error
            
            # Verify the data was actually saved
            try:
                cursor.execute(
                    f"SELECT id, call_log_id, disposition, cost FROM {SCHEMA}.voice_call_analysis WHERE call_log_id = %s::uuid ORDER BY created_at DESC LIMIT 1",
                    (call_log_id,)
                )
                saved_record = cursor.fetchone()
                if saved_record:
                    logger.info(f" VERIFIED: Data saved to voice_call_analysis - ID: {saved_record[0]}, Call Log: {saved_record[1]}, Disposition: {saved_record[2]}, Cost: {saved_record[3]}")
                else:
                    logger.warning(f"⚠️ WARNING: No record found in voice_call_analysis after save for call_log_id: {call_log_id}")
            except Exception as verify_error:
                logger.warning(f"Failed to verify save: {verify_error}")
            
            cursor.close()
            conn.close()
            
            logger.info(f"Analytics saved to {SCHEMA}.voice_call_analysis table successfully!")
            
            # Update tags column in leads table with lead_category value
            if analysis and "lead_score" in analysis:
                from db.db_config import get_db_config
                leads_db_config = get_db_config()
                
                try:
                    # Get lead_category from analysis
                    lead_score_data = analysis.get("lead_score", {})
                    lead_category_value = lead_score_data.get("lead_category", "")
                    
                    logger.info(f"DEBUG: lead_category_value type: {type(lead_category_value)}, value: {lead_category_value}")
                    
                    # Get lead_id from database using call_log_id
                    if lead_category_value and call_log_id:
                        conn = psycopg2.connect(**leads_db_config)
                        cursor = conn.cursor()
                        
                        # Get lead_id from voice_call_logs table
                        cursor.execute(f"""
                            SELECT lead_id 
                            FROM {SCHEMA}.voice_call_logs 
                            WHERE id = %s::uuid
                        """, (call_log_id,))
                        result = cursor.fetchone()
                        
                        if result and result[0]:
                            lead_id_from_call = result[0]
                            
                            # Ensure lead_category_value is a string, not a dict
                            if isinstance(lead_category_value, dict):
                                # Check for both 'category' and 'lead_category' keys
                                lead_category_value = (
                                    lead_category_value.get("category") or 
                                    lead_category_value.get("lead_category") or 
                                    str(lead_category_value)
                                )
                            elif not isinstance(lead_category_value, str):
                                lead_category_value = str(lead_category_value)
                            
                            # Create tags array and ensure it's properly serialized
                            tags_array = [lead_category_value]
                            logger.info(f"DEBUG: tags_array before serialization: {tags_array}, type: {type(tags_array)}")
                            
                            try:
                                tags_json = json.dumps(tags_array) if isinstance(tags_array, (list, dict)) else str(tags_array)
                                logger.info(f"DEBUG: tags_json after serialization: {tags_json}, type: {type(tags_json)}")
                            except Exception as json_error:
                                logger.error(f"JSON serialization error: {json_error}")
                                tags_json = str(tags_array)  # Fallback
                            
                            # Update tags column in leads table as JSON array
                            try:
                                cursor.execute(
                                    f"UPDATE {SCHEMA}.leads SET tags = %s, updated_at = %s WHERE id = %s::uuid",
                                    (tags_json, datetime.now(timezone.utc), lead_id_from_call)
                                )
                                conn.commit()  # Commit the tags update

                            except Exception as db_error:
                                logger.error(f"Database update error: {db_error}")
                                logger.error(f"Parameters: tags_json={tags_json} (type: {type(tags_json)}), lead_id={lead_id_from_call}")
                                raise db_error
                        else:
                            logger.warning(f"No lead_id found for call_log_id: {call_log_id}")
                        
                        cursor.close()
                        conn.close()
                        
                except Exception as tag_update_error:
                    logger.warning(f"Failed to update tags in leads table: {tag_update_error}")
                    # Don't fail the whole operation if tag update fails
            
            return True
        except Exception as e:
            logger.error(f"Database save failed: {e}", exc_info=True)
            return False
    def update_leads_for_user_transcription(self, call_log_id: str, lead_id: str, db_config: Dict, 
                                           conn=None, cursor=None, transcripts=None, tenant_id: str = None) -> bool:
        """
        Update leads table when user transcriptions are present in voice_call_logs.
        
        Updates the leads table columns:
        - source: 'voice_agent' 
        - status: 'success'
        - stage: [highest stage reached from conversation analysis]
        
        Only updates if user transcriptions are present (not just agent transcriptions).
        Will override existing values for the same lead_id.
        
        Args:
            call_log_id: UUID of the voice call log
            lead_id: UUID of the lead to update
            db_config: Database configuration dictionary
            conn: Optional existing database connection (for reusing transactions)
            cursor: Optional existing cursor (for reusing transactions)
            transcripts: Optional transcripts data (to avoid duplicate DB query)
            
        Returns:
            bool: True if update was successful, False otherwise
        """
        
        if not DB_AVAILABLE:
            logger.warning("Database not available - cannot update leads table")
            return False
            
        if not lead_id:
            logger.warning(f"No lead_id provided for call log: {call_log_id}")
            return False
        
        logger.info(f"DEBUG: DB_AVAILABLE={DB_AVAILABLE}, lead_id={lead_id}")
        
        # Validate call_log_id UUID format
        if isinstance(call_log_id, str):
            import uuid as uuid_lib
            try:
                # Validate UUID format
                uuid_lib.UUID(call_log_id)
                logger.info(f"DEBUG: call_log_id {call_log_id} is valid UUID")
            except ValueError:
                logger.error(f"Invalid call_log_id format: {call_log_id}")
                return False
        
        # System messages to filter out
        SYSTEM_MESSAGES = [
            "the person you're trying to reach is not available",
            "at the tone", "please record your message",
            "to leave a callback number", "press",
            "hang up", "send a numeric page", "to repeat this message"
        ]
            
        # Use existing connection or create new one
        should_close_connection = False
        local_conn = None
        local_cursor = None
        
        try:
            
            # Use provided connection/cursor or create new ones
            if conn is not None and cursor is not None:
                logger.info(f"DEBUG: Using provided connection and cursor")
                local_conn = conn
                local_cursor = cursor
                should_close_connection = False
            else:
                # Ensure all db_config values are strings (fix for JSON error)
                cleaned_db_config = {}
                for key, value in db_config.items():
                    if isinstance(value, dict):
                        cleaned_db_config[key] = json.dumps(value)
                    else:
                        cleaned_db_config[key] = str(value) if value is not None else value
                local_conn = psycopg2.connect(**cleaned_db_config)
                local_cursor = local_conn.cursor()
                should_close_connection = True
                
            logger.info(f"DEBUG: Database connection established, cursor created: {local_cursor is not None}")
            
            # Use provided transcripts or fetch from database
            if transcripts is not None:
                logger.info(f"DEBUG: Using provided transcripts, type: {type(transcripts)}")
                # Handle transcripts that might be passed as dict instead of JSON string
                if isinstance(transcripts, dict):
                    logger.info(f"DEBUG: Converting transcripts dict to JSON string")
                    transcripts = json.dumps(transcripts)
                    logger.info(f"DEBUG: Transcripts converted to JSON string, length: {len(transcripts)}")
                elif isinstance(transcripts, tuple):
                    # Handle case where transcripts is returned as a tuple from database query
                    logger.info(f"DEBUG: Converting transcripts tuple to JSON string")
                    if len(transcripts) > 0 and isinstance(transcripts[0], dict):
                        transcripts = json.dumps(transcripts[0])
                    else:
                        transcripts = json.dumps(transcripts)
            else:
                # Get transcripts from voice_call_logs
                try:
                    local_cursor.execute(f"""
                        SELECT transcripts 
                        FROM {SCHEMA}.voice_call_logs 
                        WHERE id = %s::uuid
                    """, (call_log_id,))
                    result = local_cursor.fetchone()
                    
                    if not result:
                        logger.warning(f"No transcripts found for call log: {call_log_id}")
                        return False
                    
                    transcripts = result[0]
                    
                    # Handle case where transcripts is a dict (from tuple result)
                    if isinstance(transcripts, dict):
                        transcripts = json.dumps(transcripts)
                        
                except Exception as db_query_error:
                    logger.error(f"Database query error: {db_query_error}")
                    raise db_query_error
            
            # Check if user transcriptions are present
            has_user_transcription = False
            conversation_text = ""
            
            if transcripts:
                try:
                    # Try to parse as JSON first
                    parsed_transcripts = json.loads(transcripts)
                    
                    if isinstance(parsed_transcripts, dict):
                        # JSON string with dict format - check if it has segments
                        if 'segments' in parsed_transcripts and isinstance(parsed_transcripts['segments'], list):
                            # Use the same parsing as main analysis
                            conversation_text = self._parse_voiceagent_segments(parsed_transcripts['segments'])
                            # Check if there are any user messages in the parsed conversation
                            has_user_transcription = any(line.startswith("User:") for line in conversation_text.split('\n') if line.strip())
                        else:
                            # Fallback for other dict formats
                            for key, value in parsed_transcripts.items():
                                if isinstance(value, str):
                                    # Check for user transcriptions
                                    if key.lower() == 'user' and value.strip():
                                        has_user_transcription = True
                                    # Build conversation text for stage analysis
                                    conversation_text += f"{key}: {value}\n"
                                elif isinstance(value, list):
                                    # Handle nested list of messages
                                    for item in value:
                                        if isinstance(item, dict):
                                            speaker = item.get('speaker', '').lower()
                                            text = item.get('text', '').strip()
                                            if speaker == 'user' and text:
                                                has_user_transcription = True
                                            conversation_text += f"{speaker}: {text}\n"
                    elif isinstance(parsed_transcripts, list):
                        # JSON string with list format - use same parsing as main analysis
                        conversation_text = self._parse_voiceagent_segments(parsed_transcripts)
                        # Check if there are any user messages in the parsed conversation
                        has_user_transcription = any(line.startswith("User:") for line in conversation_text.split('\n') if line.strip())
                    else:
                        # JSON but not a recognized format, treat as plain text
                        if parsed_transcripts and str(parsed_transcripts).strip():
                            # Non-empty text content, assume it's user transcription
                            # Filter out system messages
                            text_content = str(parsed_transcripts).strip()
                            is_system = any(sys_msg in text_content.lower() for sys_msg in SYSTEM_MESSAGES)
                            if not is_system:
                                has_user_transcription = True
                                conversation_text = text_content
                except (json.JSONDecodeError, ValueError) as json_error:
                    logger.warning(f"Failed to parse transcripts as JSON: {json_error}")
                    # Not valid JSON, treat as plain text
                    if transcripts.strip():
                        # Non-empty text content, assume it's user transcription
                        # Filter out system messages
                        text_content = transcripts.strip()
                        is_system = any(sys_msg in text_content.lower() for sys_msg in SYSTEM_MESSAGES)
                        if not is_system:
                            has_user_transcription = True
                            conversation_text = text_content
            else:
                logger.info(f"No transcripts provided")

            logger.info(f"Final result - has_user_transcription: {has_user_transcription}, conversation_text length: {len(conversation_text)}")

            if not has_user_transcription:
                logger.info("No user transcriptions found for call log: {call_log_id} - updating source and stage only")

                try:
                    stage_info = stage_detector.extract_call_stages(conversation_text, {}, tenant_id)
                    stages_reached = stage_info.get('stages_reached', [])

                    # For no user transcription, always set stage to followup and add 2_followup to stages_reached
                    stage_value = 'followup'
                    stages_reached.append("2_followup")  # Add followup stage for no user transcription

                    logger.info(f"No user transcription - setting stage to followup and adding 2_followup to stages_reached")
                    logger.info(f"Updated stages_reached: {stages_reached}")

                    try:
                        # Convert stages_reached to text for storage
                        stages_reached_text = json.dumps(stages_reached) if isinstance(stages_reached, list) else str(stages_reached)

                        local_cursor.execute(f"""
                            UPDATE {SCHEMA}.voice_call_analysis 
                            SET stages_reached = %s
                            WHERE call_log_id = %s::uuid
                        """, (
                            stages_reached_text,
                            call_log_id
                        ))
                        logger.info(f"DEBUG: Updated voice_call_analysis stages_reached with 2_followup")

                        # Commit this update
                        local_conn.commit()
                        logger.info(f"DEBUG: Committed voice_call_analysis update")

                    except Exception as analysis_update_error:
                        logger.error(f"Error updating voice_call_analysis stages_reached: {analysis_update_error}")
                        # Don't fail the whole operation if this update fails

                    # Ensure lead_id is a string
                    if not isinstance(lead_id, str):
                        lead_id = str(lead_id)

                    try:
                        logger.info(f"DEBUG: Executing no-user-transcription UPDATE with parameters: source=voice_agent, stage={stage_value}, lead_id={lead_id}")
                        logger.info(f"DEBUG: Parameter types - source: {type('voice_agent')}, stage: {type(stage_value)}, lead_id: {type(lead_id)}")

                        local_cursor.execute(f"""
                            UPDATE {SCHEMA}.leads 
                            SET source = %s, 
                                stage = %s,
                                updated_at = %s
                            WHERE id = %s::uuid
                        """, (
                            'voice_agent',  # Update source column
                            stage_value,    # Set stage as 'followup' or 'followup_scheduled'
                            datetime.now(timezone.utc),
                            lead_id
                        ))
                        logger.info(f"DEBUG: No-user-transcription UPDATE executed successfully")

                    except Exception as no_user_update_error:
                        logger.error(f"Error executing no-user-transcription leads table update: {no_user_update_error}")
                        logger.error(f"Parameters: source=voice_agent, stage={stage_value} (type: {type(stage_value)}), lead_id={lead_id} (type: {type(lead_id)})")
                        raise no_user_update_error

                except Exception as update_error:
                    logger.error(f"Error executing leads table update: {update_error}")
                    raise update_error

            else:
                # Has user transcriptions - process normally
                try:
                    # Process user transcription logic here
                    readable_stage = stage_detector.extract_call_stages(conversation_text, {}, tenant_id).get('final_stage', 'contacted')
                    stage_str = str(readable_stage) if readable_stage is not None else ""
                    lead_id_str = str(lead_id) if lead_id is not None else ""
                    
                    local_cursor.execute(f"""
                        UPDATE {SCHEMA}.leads 
                        SET source = %s, 
                            status = %s, 
                            stage = %s,
                            updated_at = %s
                        WHERE id = %s::uuid
                    """, ('voice_agent', 'success', stage_str, datetime.now(timezone.utc), lead_id_str))
                    
                    if should_close_connection:
                        local_conn.commit()
                    logger.info(f"Updated leads table for lead_id: {lead_id} (source=voice_agent, status=success, stage={readable_stage})")
                    return True
                    
                except Exception as update_error:
                    logger.error(f"Error executing leads table update: {update_error}")
                    if local_conn and should_close_connection:
                        local_conn.rollback()
                    return False
            
            # Always commit the transaction
            if should_close_connection:
                local_conn.commit()
            logger.info(f"Updated leads table for lead_id: {lead_id} (source=voice_agent, stage={stage_value})")
            return True
            
        except Exception as e:
            logger.error(f"Error updating leads table: {e}")
            if local_conn and should_close_connection:
                local_conn.rollback()
            return False
        finally:
            # Only close if we created the connection ourselves
            if should_close_connection:
                if local_cursor:
                    local_cursor.close()
                if local_conn:
                    local_conn.close()

analytics = CallAnalytics()

async def analyze_call_complete(call_id: str, conversation_log, duration_seconds: int, call_start_time=None):
    """Complete call analysis - call this AFTER the call ends
    
    Args:
        call_id: Unique call identifier
        conversation_log: List of dicts with {role, message, timestamp} OR string (legacy)
        duration_seconds: Call duration
        call_start_time: datetime when call started (optional, for real timestamps)
    """
    
    analysis = await analytics.analyze_call(call_id, conversation_log, duration_seconds, call_start_time)
    json_filename = analytics.save_analysis(analysis)
    
    return analysis

"""
Standalone Call Analytics Tool
Database input only for generating call analytics JSON

Usage:
    # List all calls from database
    python merged_analytics.py --list-calls
    
    # From database (by row number)
    python merged_analytics.py --db-id 123
    
    # From database (by UUID)
    python merged_analytics.py --db-id bcc0402b-c290-4242-9873-3cd31052b84a
"""

import argparse
import sys

try:
    import psycopg2
    import psycopg2.errors
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False


class StandaloneAnalytics:
    """Standalone analytics processor - database input only"""
    
    def __init__(self, db_config: Optional[Dict] = None):
        """
        Initialize standalone analytics
        
        Args:
            db_config: Database connection config (optional)
                      {'host': 'localhost', 'database': 'db', 'user': 'user', 'password': 'pass'}
        """
        self.analytics = CallAnalytics()
        self.db_config = db_config
    
    async def list_database_calls(self) -> None:
        """List all calls from database with row numbers matching the query order"""
        if not DB_AVAILABLE:
            raise ImportError("psycopg2 not installed. Install with: pip install psycopg2-binary")
        
        if not self.db_config:
            raise ValueError("Database config not provided. Use --db-host, --db-name, --db-user, --db-pass or .env file")
        
        logger.info("Listing calls from database...")
        
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()
        
        try:
            # Fetch calls in pgAdmin's default display order (physical storage order using ctid)
            # This matches exactly how pgAdmin shows rows when no ORDER BY is specified
            cursor.execute(f"""
                SELECT 
                    ROW_NUMBER() OVER (ORDER BY ctid) as row_num,
                    id,
                    started_at,
                    ended_at,
                    CASE 
                        WHEN transcripts IS NULL OR transcripts::text = '' THEN 'No transcript'
                        ELSE SUBSTRING(transcripts::text, 1, 50) || '...'
                    END as transcript_preview
                FROM {SCHEMA}.voice_call_logs
                ORDER BY ctid
                LIMIT 500000
            """)
            
            calls = cursor.fetchall()
            
            if not calls:
                logger.warning("No calls found in database.")
                return
            
            for row_num, call_id, started_at, ended_at, transcript_preview in calls:
                duration = ""
                if started_at and ended_at:
                    duration_seconds = int((ended_at - started_at).total_seconds())
                    duration = f"{duration_seconds}s"
                
                started_str = started_at.strftime('%Y-%m-%d %H:%M:%S') if started_at else 'N/A'
                print(f"{row_num}: {call_id} - {started_str} - {duration} - {transcript_preview}")
            
        finally:
            cursor.close()
            conn.close()

    async def analyze_from_database(self, call_log_id) -> Dict:
        """
        Analyze call from database call_logs table
        
        Args:
            call_log_id: ID from call_logs_voiceagent table
            
        Returns:
            Complete analytics JSON
        """
        if not DB_AVAILABLE:
            raise ImportError("psycopg2 not installed. Install with: pip install psycopg2-binary")
        
        if not self.db_config:
            raise ValueError("Database config not provided. Use --db-host, --db-name, --db-user, --db-pass")
        
        logger.info(f"Fetching call from database (ID: {call_log_id})")
        
        # Connect to database
        conn = psycopg2.connect(**self.db_config)
        cursor = conn.cursor()
        
        try:
            # Fetch call data from call_logs_voiceagent table (in voice_agent schema)
            # Handle both UUID and integer IDs
            # If call_log_id is integer and id column is UUID, use ROW_NUMBER() for sequential access
            # If call_log_id is UUID string, match directly
            
            # Try to parse as integer first, otherwise treat as UUID string
            try:
                call_log_id_int = int(call_log_id)
                call_log_id = call_log_id_int
            except (ValueError, TypeError):
                # Not an integer, treat as UUID string
                pass
            
            if isinstance(call_log_id, int):
                # Integer ID: Use ROW_NUMBER() to find the Nth record (ordered by ctid)
                cursor.execute(f"""
                    SELECT id, transcripts, started_at, ended_at, duration_seconds, recording_url
                    FROM (
                        SELECT 
                            id, 
                            transcripts, 
                            started_at, 
                            ended_at, 
                            duration_seconds,
                            recording_url,
                            ROW_NUMBER() OVER (ORDER BY ctid) as row_num
                        FROM {SCHEMA}.voice_call_logs
                    ) ranked
                    WHERE row_num = %s
                """, (call_log_id,))
            else:
                # UUID string: Try direct UUID match or text match
                try:
                    cursor.execute(f"""
                        SELECT id, transcripts, started_at, ended_at, duration_seconds, recording_url
                        FROM {SCHEMA}.voice_call_logs
                        WHERE id = %s::uuid
                    """, (str(call_log_id),))
                except (psycopg2.errors.InvalidTextRepresentation, psycopg2.errors.UndefinedFunction):
                    # Fallback: try text match
                    cursor.execute(f"""
                        SELECT id, transcripts, started_at, ended_at, duration_seconds, recording_url
                        FROM {SCHEMA}.voice_call_logs
                        WHERE id::text = %s
                    """, (str(call_log_id),))
            
            call_data = cursor.fetchone()
            
            if not call_data:
                raise ValueError(f"Call log {call_log_id} not found in database")
            
            db_call_id, transcripts, started_at, ended_at, duration_seconds_db = call_data[:5]
            recording_url = call_data[5] if len(call_data) > 5 else None
            
            # Get tenant_id and lead_id in single query
            cursor.execute(f"SELECT tenant_id, lead_id FROM {SCHEMA}.voice_call_logs WHERE id = '{db_call_id}'::uuid")
            result = cursor.fetchone()
            tenant_id = result[0] if result and result[0] else None
            lead_id = result[1] if result and result[1] else None
            
            # Get transcript from transcripts column
            # Handle both JSONB (dict/list) and text formats
            if transcripts:
                if isinstance(transcripts, dict):
                    # JSONB dict - check for voiceagent segments or other formats
                    if 'segments' in transcripts:
                        conversation_text = transcripts  # Pass dict to analyze_call for segment parsing
                    else:
                        # Other dict format
                        conversation_text = transcripts
                elif isinstance(transcripts, list):
                    # JSONB list - check if it's voiceagent segments
                    if transcripts and isinstance(transcripts[0], dict) and 'speaker' in transcripts[0]:
                        conversation_text = transcripts  # Pass list to analyze_call for segment parsing
                    else:
                        # Other list format
                        conversation_text = transcripts
                else:
                    # Plain text
                    conversation_text = str(transcripts)
            else:
                conversation_text = ""
            
            if not conversation_text:
                raise ValueError(f"No transcript found for call {call_log_id}")
            
            # Duration: only use duration_seconds column (if missing/invalid -> 0)
            try:
                duration = int(float(duration_seconds_db)) if duration_seconds_db is not None else 0
            except (ValueError, TypeError):
                duration = 0
            logger.info(f"Duration from duration_seconds column: {duration}s")
            
            call_id = f"DB_{call_log_id}_{started_at.strftime('%Y%m%d_%H%M%S') if started_at else 'unknown'}"
            
            logger.info(f"Call ID: {call_id}, Duration: {duration}s, Started: {started_at}, Ended: {ended_at}")
            
            # Run analytics
            result = await self.analytics.analyze_call(
                call_id=call_id,
                conversation_log=conversation_text,
                duration_seconds=duration,
                call_start_time=started_at,
                tenant_id=str(tenant_id) if tenant_id else None
            )
            
            # Add tenant_id to result for tracking
            if tenant_id:
                result['tenant_id'] = str(tenant_id)
            
                # Save to database
                if tenant_id and STORAGE_CLASSES_AVAILABLE:
                    logger.info("Saving analytics to {SCHEMA}.voice_call_analysis table...")
                    success = await self.analytics.save_to_db(result, str(db_call_id), str(tenant_id))
                    # Use the same db_config for leads update
                    from db.db_config import get_db_config
                    leads_db_config = get_db_config()
                else:
                    logger.info("Saving analytics to post_call_analysis_voiceagent table (legacy)...")
                    success = self.analytics.save_to_database(result, db_call_id, self.db_config)
                    leads_db_config = self.db_config
                
                if success:
                    logger.info("Analytics saved to database successfully!")
                    # Update leads table if user transcriptions are present
                    try:
                        if lead_id:
                            # Get the original transcripts from database to pass to leads update
                            cursor.execute(f"SELECT transcripts FROM {SCHEMA}.voice_call_logs WHERE id = %s::uuid", (str(db_call_id),))
                            transcripts_result = cursor.fetchone()
                            original_transcripts = transcripts_result[0] if transcripts_result else None
                            
                            self.analytics.update_leads_for_user_transcription(str(db_call_id), lead_id, leads_db_config, None, None, original_transcripts, str(tenant_id))
                    except Exception as leads_update_error:
                        logger.warning(f"Failed to update leads table for user transcription: {leads_update_error}")
                        # Don't fail the whole operation if leads update fails
                else:
                    logger.error("Failed to save analytics to database")
            
            return result
            
        finally:
            cursor.close()
            conn.close()
    
    def save_json(self, result: Dict, output_file: Optional[str] = None) -> str:
        """
        Save analytics result to JSON file
        
        Args:
            result: Analytics result dictionary
            output_file: Optional custom output filename
            
        Returns:
            Path to saved file
        """
        if not output_file:
            call_id = result.get('call_id', 'UNKNOWN')
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"call_analysis_{call_id}_{timestamp}.json"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Analysis saved to: {output_file}")
        return output_file
    

async def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description='Standalone Call Analytics Tool - Database input only',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # List all calls from database (shows row numbers matching --db-id usage)
    python merged_analytics.py --list-calls
    
    # From database (by row number - matches pgAdmin's default display order)
    python merged_analytics.py --db-id 23
    
    # From database (by UUID directly)
    python merged_analytics.py --db-id bcc0402b-c290-4242-9873-3cd31052b84a
        """
    )
    
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--db-id', help='Call log ID from database (integer for row number, or UUID string)')
    input_group.add_argument('--list-calls', action='store_true', help='List all calls with row numbers')
    
    parser.add_argument('--output', '-o', help='Output JSON file path')
    parser.add_argument('--db-host', help='Database host (default: from .env or localhost)')
    parser.add_argument('--db-name', help='Database name (default: from .env)')
    parser.add_argument('--db-user', help='Database user (default: from .env or postgres)')
    parser.add_argument('--db-pass', help='Database password (default: from .env)')
    
    args = parser.parse_args()
    
    db_config = {
        'host': args.db_host or os.getenv('DB_HOST', 'localhost'),
        'database': args.db_name or os.getenv('DB_NAME', 'salesmaya_agent'),
        'user': args.db_user or os.getenv('DB_USER', 'postgres'),
        'password': args.db_pass or os.getenv('DB_PASSWORD')
    }
    
    if not db_config['password']:
        parser.error("Database password required. Provide via --db-pass or DB_PASSWORD in .env file")
    
    analytics = StandaloneAnalytics(db_config=db_config)
    result = None
    
    try:
        if args.list_calls:
            await analytics.list_database_calls()
            return
        elif args.db_id:
            result = await analytics.analyze_from_database(args.db_id)
        
        if result:
            analytics.save_json(result, args.output)
    
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
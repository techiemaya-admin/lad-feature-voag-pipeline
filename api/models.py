"""
Shared Pydantic models and dataclasses for the V2 API.

Migrated from main.py to be shared across route modules.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# VALIDATION PATTERNS
# =============================================================================

# E.164 pattern - allows numbers starting with + or 0 (for local formats)
E164_PATTERN = re.compile(r"^(\+[1-9]\d{1,14}|0\d{9,14})$")


# =============================================================================
# ENUMS
# =============================================================================

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CallMode(str, Enum):
    SINGLE = "single"
    BATCH = "batch"


# =============================================================================
# DATACLASSES (Internal State)
# =============================================================================

@dataclass
class CallAttemptResult:
    to_number: str
    status: JobStatus = JobStatus.PENDING
    dispatch_id: str | None = None
    room_name: str | None = None
    error: str | None = None
    index: int | None = None
    lead_name: str | None = None
    context: str | None = None
    call_log_id: str | None = None


@dataclass
class CallJob:
    job_id: str
    mode: CallMode
    voice_id: str
    from_number: str | None
    base_context: str | None
    tts_voice_id: str | None = None
    voice_provider: str | None = None
    voice_name: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    initiated_by: str | None = None  # UUID
    agent_id: int | None = None
    status: JobStatus = JobStatus.PENDING
    error: str | None = None
    results: list[CallAttemptResult] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CallCompletionState:
    status: str
    artifacts_ready: bool
    recording_url: str | None = None
    transcriptions: Any | None = None
    raw_status: Any | None = None
    ended_at: datetime | None = None


# =============================================================================
# PYDANTIC MODELS (API Responses)
# =============================================================================

class CallAttemptModel(BaseModel):
    to_number: str
    status: JobStatus
    room_name: str | None = None
    dispatch_id: str | None = None
    error: str | None = None
    index: int | None = None
    lead_name: str | None = None
    context: str | None = None
    call_log_id: str | None = None

    class Config:
        use_enum_values = True


class CallJobResponse(BaseModel):
    job_id: str
    mode: CallMode
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    voice_id: str | None = None
    tts_voice_id: str | None = None
    voice_provider: str | None = None
    voice_name: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    from_number: str | None = None
    base_context: str | None = None
    initiated_by: str | None = None  # UUID
    agent_id: int | None = None
    results: list[CallAttemptModel] = Field(default_factory=list)
    error: str | None = None

    class Config:
        use_enum_values = True
        json_encoders = {datetime: lambda dt: dt.isoformat(timespec="seconds")}


# =============================================================================
# CALL REQUEST MODELS
# =============================================================================

class SingleCallPayload(BaseModel):
    voice_id: str = Field(..., description="Voice identifier or 'default' to use the agent's configured voice")
    from_number: str | None = Field(None, description="Caller ID in E.164 format")
    to_number: str = Field(..., description="Destination number in E.164 format")
    added_context: str | None = Field(None, description="Optional context to append to the agent instructions")
    llm_provider: str | None = Field(None, description="Optional LLM provider override (groq, google, openai)")
    llm_model: str | None = Field(None, description="Optional LLM model identifier for the selected provider")
    initiated_by: str | None = Field(None, description="Optional user UUID that initiated the call")
    agent_id: int | None = Field(None, description="Optional agent ID associated with the call")
    lead_name: str | None = Field(None, description="Optional display name to associate with the lead")
    lead_id: str | None = Field(None, description="Optional lead UUID for call tracking")
    knowledge_base_store_ids: list[str] | None = Field(None, description="Optional list of File Search store IDs for RAG")

    @field_validator("voice_id")
    @classmethod
    def _voice_id_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("voice_id cannot be empty")
        return value.strip()

    @field_validator("from_number", "to_number", mode="before")
    @classmethod
    def _validate_phone_number(cls, value: Optional[str], info: Any) -> Optional[str]:
        """
        Validate and normalize phone numbers to E.164 format.
        Uses shared logic from utils.call_routing.
        """
        if value is None:
            return None
        
        try:
            from utils.call_routing import normalize_phone_to_e164
            return normalize_phone_to_e164(value)
        except ValueError as e:
            raise ValueError(str(e))


    @field_validator("agent_id")
    @classmethod
    def _validate_positive_identifier(cls, value: Optional[int], info: Any) -> Optional[int]:
        if value is None:
            return None
        if value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value


# =============================================================================
# BATCH CALL MODELS
# =============================================================================

class BatchCallJsonEntry(BaseModel):
    to_number: str
    lead_name: str | None = None
    added_context: str | None = None
    lead_id: str | None = None  # UUID
    knowledge_base_store_ids: list[str] | None = None

    @field_validator("to_number", mode="before")
    @classmethod
    def _validate_to_number(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("to_number is required")
        text = value.strip()
        if not E164_PATTERN.match(text):
            raise ValueError("to_number must be an E.164 formatted number")
        return text


class BatchCallJsonRequest(BaseModel):
    voice_id: str
    from_number: str | None = None
    added_context: str | None = None
    initiated_by: str | None = None  # UUID
    agent_id: int | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    knowledge_base_store_ids: list[str] | None = None
    entries: list[BatchCallJsonEntry]

    @field_validator("voice_id")
    @classmethod
    def _validate_voice_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("voice_id cannot be empty")
        return value.strip()

    @field_validator("entries")
    @classmethod
    def _validate_entries(cls, value: list[BatchCallJsonEntry]) -> list[BatchCallJsonEntry]:
        if not value:
            raise ValueError("entries cannot be empty")
        return value


# =============================================================================
# CALL STATUS MODELS  
# =============================================================================

class CallStatusResponse(BaseModel):
    call_log_id: str
    status: str
    call_duration: float | None = None
    call_recording_url: str | None = None
    transcriptions: Any | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    batch_id: str | None = None
    is_batch_call: bool = False


class BatchEntryStatusModel(BaseModel):
    entry_index: int
    to_number: str
    lead_name: str | None = None
    status: str
    call_log_id: str | None = None
    call_status: str | None = None
    call_duration: float | None = None
    call_recording_url: str | None = None
    error_message: str | None = None


class BatchStatusResponse(BaseModel):
    batch_id: str
    job_id: str
    status: str
    total_calls: int
    completed_calls: int
    failed_calls: int
    cancelled_calls: int = 0
    pending_calls: int
    running_calls: int
    initiated_by: str | None = None  # UUID
    agent_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stopped_at: datetime | None = None
    completed_at: datetime | None = None
    entries: list[BatchEntryStatusModel] = Field(default_factory=list)


# =============================================================================
# CANCEL MODELS
# =============================================================================

class CancelRequest(BaseModel):
    resource_id: str | list[str] = Field(..., description="Single or multiple call log UUIDs / batch job_ids")
    force: bool = Field(False, description="If true, forcefully terminate running calls (not just pending)")


class CancelResultItem(BaseModel):
    resource_id: str
    resource_type: str  # "call" or "batch"
    status: str
    cancelled_count: int
    message: str


class CancelResponse(BaseModel):
    results: list[CancelResultItem]
    total_cancelled: int


# =============================================================================
# OAUTH MODELS
# =============================================================================

class OAuthStatusResponse(BaseModel):
    connected: bool
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    has_refresh_token: bool = False
    connected_gmail: str | None = Field(None, description="Gmail address of the connected Google account")


class OAuthRevokeRequest(BaseModel):
    user_id: str


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Enums
    "JobStatus",
    "CallMode",
    # Dataclasses
    "CallAttemptResult",
    "CallJob",
    "CallCompletionState",
    # Pydantic models
    "CallAttemptModel",
    "CallJobResponse",
    "SingleCallPayload",
    "BatchCallJsonEntry",
    "BatchCallJsonRequest",
    "CallStatusResponse",
    "BatchEntryStatusModel",
    "BatchStatusResponse",
    "CancelRequest",
    "CancelResponse",
    "OAuthStatusResponse",
    "OAuthRevokeRequest",
    # Patterns
    "E164_PATTERN",
]

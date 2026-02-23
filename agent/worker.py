"""
LiveKit Voice Agent Worker - V2 Entry Point.

This module implements the main LiveKit worker that handles inbound and outbound calls.
It uses the modular v2/agent components for:
- Pipeline (TTS/LLM configuration)
- Config (VAD/STT settings)
- Tool Builder (agent tools)
- Instruction Builder (prompt assembly)
- Cleanup Handler (post-call cleanup)

Key Features:
- Multi-provider TTS support (Cartesia, Google, Gemini, ElevenLabs, Rime, SmallestAI)
- Multi-provider LLM support (Gemini, Groq, OpenAI)
- Real-time call recording and transcription
- Silence detection and automatic hangup
- Background audio (office ambience, typing sounds)
- Post-call analysis and storage
- Configurable voice personalities and instructions
"""

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

from dotenv import load_dotenv

# Load environment variables with explicit path (works when run from any directory)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(_env_path)

# ============================================================================
# DISABLE OPENTELEMETRY EXPORTS (must be set before livekit imports)
# ============================================================================
# These prevent the 429 quota errors from LiveKit Cloud telemetry
# Set via .env: OTEL_TRACES_EXPORTER=none, OTEL_LOGS_EXPORTER=none
if os.getenv("OTEL_TRACES_EXPORTER", "").lower() == "none":
    os.environ["OTEL_TRACES_EXPORTER"] = "none"
if os.getenv("OTEL_LOGS_EXPORTER", "").lower() == "none":
    os.environ["OTEL_LOGS_EXPORTER"] = "none"
if os.getenv("OTEL_METRICS_EXPORTER", "").lower() == "none":
    os.environ["OTEL_METRICS_EXPORTER"] = "none"

# ============================================================================
# AGENT LOG LEVEL CONFIGURATION
# ============================================================================
# AGENT_LOG_LEVEL takes precedence, falls back to LOG_LEVEL
def _resolve_log_level(value):
    if value is None:
        return logging.INFO
    level_map = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, 
                 "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    return level_map.get(value.strip().upper(), logging.INFO)

_agent_log_level = _resolve_log_level(
    os.getenv("AGENT_LOG_LEVEL") or os.getenv("LOG_LEVEL")
)
logging.basicConfig(level=_agent_log_level, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

# Silence noisy third-party loggers
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
logging.getLogger("opentelemetry.exporter").setLevel(logging.CRITICAL)

# Suppress LiveKit session report upload errors (429 quota)
# These are harmless - calls still work, just no telemetry to LiveKit Cloud
_lk_agents_logger = logging.getLogger("livekit.agents")
_original_lk_error = _lk_agents_logger.error
def _filtered_lk_error(msg, *args, **kwargs):
    if "session report" in str(msg).lower():
        return  # Suppress session report errors
    _original_lk_error(msg, *args, **kwargs)
_lk_agents_logger.error = _filtered_lk_error

from livekit import agents, api
from livekit.agents import (
    Agent,
    AgentSession,
    RoomInputOptions,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip,
    MetricsCollectedEvent,
    RunContext,
)
from livekit.agents.llm import function_tool
from livekit.agents import StopResponse
from livekit.plugins import deepgram, silero, google  # noqa: F401 - plugins must register on main thread
try:
    from livekit.plugins import ultravox  # noqa: F401 - register on main thread
except ImportError:
    pass  # optional dependency

# Import V2 modular components
from agent.config import PipelineConfig, get_config
from agent.pipeline import (
    build_tts_engine,
    resolve_llm_configuration,
    create_llm_instance,
    derive_stt_language,
)
from agent.tool_builder import ToolConfig, get_enabled_tools, attach_tools
from agent.instruction_builder import build_instructions_async
from agent.cleanup_handler import CleanupContext, cleanup_and_save

# Storage (v2 internal)
from db.storage.calls import CallStorage
from db.storage.agents import AgentStorage

# Recording and transcription (v2 internal)
from recording.recorder import CallRecorder
from recording.transcription import attach_transcription_tracker

# Utilities (v2 internal)
from utils.usage_tracker import UsageCollector, is_component_tracking_enabled
from utils.logger_config import configure_non_blocking_logging, stop_logging

# Load environment variables
load_dotenv()

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

_log_listener = configure_non_blocking_logging()
logger = logging.getLogger(__name__)
ambient_logger = logging.getLogger(f"{__name__}.ambient")

# Agent version
AGENT_VERSION = os.getenv("AGENT_VERSION", "2.0.0")

# Max concurrent calls
_MAX_CONCURRENT_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "1"))
_call_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)

# Atomic counter for immediate load reporting (fixes race condition where
# LiveKit dispatches multiple jobs before worker.active_jobs updates).
# threading.Lock is used because load_fnc may be called from any thread;
# the lock is uncontended in practice so overhead is negligible.
_active_count = 0
_count_lock = threading.Lock()


# =============================================================================
# WORKER LOAD MANAGEMENT
# =============================================================================

def calculate_worker_load(worker: Any) -> float:
    """
    Calculate worker load to control job distribution.
    
    Returns a value between 0.0 and 1.0 based on active jobs vs max capacity.
    When load >= 1.0, the worker stops receiving new jobs from LiveKit.
    
    Uses _active_count (incremented immediately on entrypoint entry) instead
    of len(worker.active_jobs) which has a stale window during burst dispatch.
    """
    with _count_lock:
        count = _active_count
    if _MAX_CONCURRENT_CALLS <= 0:
        return 1.0 if count > 0 else 0.0
    return min(count / _MAX_CONCURRENT_CALLS, 1.0)


# =============================================================================
# SILENCE DETECTION AND MONITORING
# =============================================================================

@dataclass(frozen=True)
class SilenceMilestone:
    """Represents a milestone during silence detection."""
    seconds: float
    callback: Callable[[float], Awaitable[None] | None]
    label: str = ""


class SilenceMonitor:
    """
    Monitors call silence and triggers warning/hangup.
    
    SIMPLER DESIGN:
    - Timer always runs once started
    - Resets on user speech (deepgram transcript)
    - Resets on agent speech (LLM response) 
    - Warning prompt does NOT reset timer (uses skip_next_agent_reset flag)
    - Warning at 15s, hangup at 35s
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        on_timeout: Callable[[], Awaitable[None] | None],
        logger: logging.Logger,
        milestones: Sequence[SilenceMilestone] | None = None,
    ) -> None:
        self._timeout = max(timeout_seconds, 1.0)
        self._on_timeout = on_timeout
        self._logger = logger
        self._enabled = True
        self._triggered = False
        self._timer_task: asyncio.Task | None = None
        self._milestones = tuple(
            sorted(
                (m for m in (milestones or []) if m.seconds > 0.0),
                key=lambda item: item.seconds,
            )
        )
        self._milestones_fired: set[int] = set()
        self._elapsed: float = 0.0
        self._last_start: float | None = None
        self._pending_callbacks: set[asyncio.Task] = set()
        self._skip_next_agent_reset: bool = False  # Request flag
        self._skipping_current_turn: bool = False  # Active state flag

    def start(self) -> None:
        """Start the silence timer. Call once when call becomes ongoing."""
        if not self._enabled or self._timer_task is not None:
            return
        self._elapsed = 0.0
        self._milestones_fired.clear()
        try:
            loop = asyncio.get_running_loop()
            self._last_start = loop.time()
            self._timer_task = loop.create_task(self._run_timer())
            self._logger.info("[SilenceMonitor] Timer STARTED (always-on mode)")
        except RuntimeError:
            pass

    def reset_timer(self, source: str = "activity") -> None:
        """Reset the timer to 0. Called on user or agent activity."""
        if not self._enabled:
            return
        self._elapsed = 0.0
        self._milestones_fired.clear()
        try:
            loop = asyncio.get_running_loop()
            self._last_start = loop.time()
        except RuntimeError:
            pass
        self._logger.info(f"[SilenceMonitor] Timer reset ({source})")

    def notify_user_activity(self) -> None:
        """Reset timer when user speaks."""
        if not self._enabled:
            return
        self.reset_timer("user speech")

    def notify_agent_started(self) -> None:
        """Called when agent starts speaking. Resets timer unless skipped."""
        if not self._enabled:
            return
        if self._skip_next_agent_reset:
            self._skip_next_agent_reset = False
            self._skipping_current_turn = True
            self._logger.info("[SilenceMonitor] Agent speech STARTED - skipping reset (warning prompt)")
            return
        
        self._skipping_current_turn = False
        self.reset_timer("agent speech")

    def notify_agent_completed(self) -> None:
        """Called when agent finishes speaking. Reset timer to start fresh silence countdown."""
        if not self._enabled:
            return
        
        if self._skipping_current_turn:
            self._skipping_current_turn = False
            self._logger.info("[SilenceMonitor] Agent speech COMPLETED - skipping reset (warning prompt)")
            return

        self.reset_timer("agent completed")

    def skip_next_agent_reset(self) -> None:
        """Flag to skip the next agent speech reset (for warning prompt)."""
        self._skip_next_agent_reset = True
        self._logger.info("[SilenceMonitor] Next agent speech will NOT reset timer")

    def disable(self) -> None:
        """Permanently disable silence monitoring."""
        if not self._enabled:
            return
        self._enabled = False
        self._triggered = True
        self._cancel_timer()
        self._cancel_pending_callbacks()
        self._logger.info("[SilenceMonitor] DISABLED")

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _cancel_pending_callbacks(self) -> None:
        for task in tuple(self._pending_callbacks):
            if not task.done():
                task.cancel()
        self._pending_callbacks.clear()

    async def _fire_milestone(self, milestone: SilenceMilestone) -> None:
        """Execute a milestone callback."""
        try:
            result = milestone.callback(self._elapsed)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self._logger.error(f"Error executing milestone '{milestone.label}': {e}", exc_info=True)

    async def _run_timer(self) -> None:
        """Main timer loop - runs continuously until disabled or timeout."""
        try:
            loop = asyncio.get_running_loop()
            
            while self._enabled and not self._triggered:
                # Calculate elapsed since last reset
                now = loop.time()
                if self._last_start is not None:
                    self._elapsed = now - self._last_start
                
                # Check milestones
                for idx, milestone in enumerate(self._milestones):
                    if idx in self._milestones_fired:
                        continue
                    if self._elapsed >= milestone.seconds:
                        self._milestones_fired.add(idx)
                        self._logger.info(f"[SilenceMonitor] Milestone '{milestone.label}' at {self._elapsed:.1f}s")
                        try:
                            task = asyncio.create_task(self._fire_milestone(milestone))
                            self._pending_callbacks.add(task)
                            task.add_done_callback(self._pending_callbacks.discard)
                        except Exception as e:
                            self._logger.error(f"Failed to fire milestone: {e}")
                
                # Check timeout
                if self._elapsed >= self._timeout:
                    self._triggered = True
                    self._logger.info(f"[SilenceMonitor] TIMEOUT at {self._elapsed:.1f}s - triggering hangup")
                    try:
                        result = self._on_timeout()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        self._logger.error(f"Error in timeout callback: {e}", exc_info=True)
                    return
                
                # Sleep briefly and check again
                await asyncio.sleep(0.5)
                
        except asyncio.CancelledError:
            self._logger.info("[SilenceMonitor] Timer cancelled")
        except Exception as e:
            self._logger.error(f"[SilenceMonitor] Timer error: {e}", exc_info=True)

    # Compatibility methods (old API)
    def _reset_state(self) -> None:
        self._elapsed = 0.0
        self._milestones_fired.clear()



# =============================================================================
# VOICE ASSISTANT AGENT
# =============================================================================

class VoiceAssistant(Agent):
    """
    Main voice assistant agent that handles conversations.
    
    Extends LiveKit's Agent class with custom functionality:
    - Call recording integration
    - Transcription tracking
    - Silence monitoring
    - Hangup control
    - Custom instructions and context
    """
    
    GLINKS_ORG_ID = "f6de7991-df4f-43de-9f40-298fcda5f723"
    
    def __init__(
        self,
        call_recorder=None,
        job_context=None,
        *,
        instructions: str,
        tools: list | None = None,
        silence_monitor: SilenceMonitor | None = None,
        initiating_user_id: str | int | None = None,
        is_glinks_agent: bool = False,
        knowledge_base_store_ids: list[str] | None = None,
        audit_trail: Any = None,  # For logging tool calls
    ):
        self.call_recorder = call_recorder
        self.job_context = job_context
        self.silence_monitor = silence_monitor
        self._is_glinks_agent = is_glinks_agent
        self.audit_trail = audit_trail
        
        # Hangup control flags
        self._hangup_pending = False
        self._hangup_cancelled = False
        
        if isinstance(initiating_user_id, int):
            self._initiating_user_id: str | None = str(initiating_user_id)
        elif isinstance(initiating_user_id, str):
            self._initiating_user_id = initiating_user_id.strip() or None
        else:
            self._initiating_user_id = None
            
        self._google_workspace = None
        self._human_invite_pending = False
        self._human_joined = False
        self._agent_listening_only = False  # When True, agent only transcribes, no LLM/TTS
        self._knowledge_base_store_ids = knowledge_base_store_ids or []
        self._session = None  # AgentSession reference for pausing on human handoff
        
        # Register interruption callback to cancel pending hangup
        if self.call_recorder and hasattr(self.call_recorder, 'register_agent_speech_end_callback'):
            self.call_recorder.register_agent_speech_end_callback(self._on_agent_speech_end)
        
        # Pass tools from tool_builder to parent Agent
        # NOTE: @function_tool decorated methods (like hangup_call) are auto-registered by Agent base class
        super().__init__(instructions=instructions, tools=tools or [])
    
    def _on_agent_speech_end(self, was_interrupted: bool) -> None:
        """Callback when agent speech ends - cancels pending hangup if interrupted."""
        if was_interrupted and self._hangup_pending:
            logger.info("Agent speech interrupted during hangup - cancelling hangup")
            self._hangup_cancelled = True
    
    async def on_user_turn_completed(self, turn_ctx, new_message):
        """
        Handle user speech completion - this is the KEY for human handoff muting.
        
        When _agent_listening_only is True (after human support joins),
        this method raises StopResponse to prevent the LLM from generating any response.
        The transcription still runs, but the AI agent stays silent.
        
        Args:
            turn_ctx: Turn context from LiveKit
            new_message: Message object containing user's transcribed speech
        """
        from livekit.agents import StopResponse
        
        # In listening-only mode, don't let the LLM respond at all
        if self._agent_listening_only:
            text = getattr(new_message, "text_content", None)
            if text:
                logger.info(f"[HumanHandoff] Listening-only mode - captured transcript: {text[:50]}...")
            logger.debug("[HumanHandoff] Blocking LLM pipeline with StopResponse")
            raise StopResponse()
        
        # Normal mode - process as usual
        if self.silence_monitor:
            self.silence_monitor.notify_user_activity()

    @function_tool
    async def hangup_call(self, reason: str = "call_complete") -> str:
        """
        End the current call gracefully.
        
        Waits for TTS parting words to complete before hanging up.
        If human interrupts during parting words, hangup is cancelled.
        
        Args:
            reason: Reason for ending (e.g., "call_complete", "not_interested")
        
        Returns:
            Confirmation message
        """
        import asyncio
        
        # GUARD: Do NOT hang up if human agent has joined the call
        if getattr(self, '_human_joined', False):
            logger.info(f"hangup_call BLOCKED: Human agent has joined the call, refusing to hangup (reason: {reason})")
            return "Hangup blocked - a human specialist is now handling the call. Do not attempt to end the call."
        
        logger.info(f"Agent initiating graceful hangup (reason: {reason})")
        
        # Disable silence monitor to prevent timeout during goodbye
        if self.silence_monitor:
            self.silence_monitor.disable()
        
        # Mark hangup as pending (for interruption detection)
        self._hangup_pending = True
        self._hangup_cancelled = False
        
        try:
            # Wait for TTS parting words to complete (if recorder available)
            if self.call_recorder:
                try:
                    tts_completed = await self.call_recorder.wait_for_tts_playout(timeout=20.0)
                    if not tts_completed:
                        logger.warning("TTS playout wait timed out, proceeding with hangup")
                except Exception as e:
                    logger.warning(f"Error waiting for TTS playout: {e}")
            
            # Check if hangup was cancelled due to interruption
            if getattr(self, '_hangup_cancelled', False):
                logger.info("Hangup cancelled - human interrupted during parting words")
                self._hangup_pending = False
                if self.audit_trail:
                    self.audit_trail.log_agent_hangup(reason, status="cancelled_interrupted")
                return "Hangup cancelled - user is speaking"
            
            # Wait 1 second after TTS for natural pause
            await asyncio.sleep(1.0)
            
            # Final check for interruption during the pause
            if getattr(self, '_hangup_cancelled', False):
                logger.info("Hangup cancelled during post-TTS pause")
                self._hangup_pending = False
                if self.audit_trail:
                    self.audit_trail.log_agent_hangup(reason, status="cancelled_interrupted")
                return "Hangup cancelled - user started speaking"
            
            # Execute hangup
            if self.job_context and self.job_context.room:
                try:
                    await self.job_context.api.room.delete_room(
                        api.DeleteRoomRequest(room=self.job_context.room.name)
                    )
                    logger.info(f"Room {self.job_context.room.name} deleted after graceful goodbye")
                    if self.audit_trail:
                        self.audit_trail.log_agent_hangup(reason, status="completed")
                except Exception as e:
                    logger.error(f"Error deleting room: {e}")
                    if self.audit_trail:
                        self.audit_trail.log_agent_hangup(reason, status="error")
                    return f"Error ending call: {e}"
            
            return f"Call ended: {reason}"
            
        finally:
            self._hangup_pending = False
    
    def cancel_pending_hangup(self) -> None:
        """Cancel a pending hangup if human interrupts during parting words."""
        if getattr(self, '_hangup_pending', False):
            self._hangup_cancelled = True
            logger.info("Pending hangup marked for cancellation")

    def set_silence_monitor(self, monitor: SilenceMonitor | None) -> None:
        self.silence_monitor = monitor
    
    def set_session(self, session) -> None:
        """Set the AgentSession reference for pausing on human handoff."""
        self._session = session
    
    def mute_for_human_handoff(self) -> None:
        """
        Prepare for human handoff by FULLY muting AI agent.
        
        - Disables silence monitor (human agent will talk)
        - Sets _human_joined flag to prevent AI from interrupting
        - Sets _agent_listening_only flag to block LLM via StopResponse
        - hangup_call tool becomes a no-op (call ends when participants leave)
        """
        logger.info("[HumanHandoff] Muting AI agent COMPLETELY for human handoff")
        
        # 1. Disable silence monitoring - human agent will be talking
        if self.silence_monitor:
            self.silence_monitor.disable()
            logger.info("[HumanHandoff] Silence monitor disabled")
        
        # 2. Mark human as joined - prevents hangup_call tool from working
        self._human_joined = True
        self._human_invite_pending = False
        logger.info("[HumanHandoff] _human_joined=True, hangup_call is now blocked")
        
        # 3. CRITICAL: Set listening-only mode - blocks LLM from responding
        # The on_user_turn_completed() method will raise StopResponse when this is True
        self._agent_listening_only = True
        logger.info("[HumanHandoff] _agent_listening_only=True, LLM will be blocked via StopResponse")
        
        # 4. Interrupt any current speech so agent stops talking immediately
        if self._session:
            try:
                if hasattr(self._session, 'interrupt'):
                    self._session.interrupt()
                    logger.info("[HumanHandoff] Session interrupted - current speech cancelled")
            except Exception as e:
                logger.warning(f"[HumanHandoff] Error interrupting session: {e}")
        
        logger.info("[HumanHandoff] AI agent muted: silence=off, human_joined=True, listening_only=True")


# =============================================================================
# MAIN ENTRYPOINT
# =============================================================================

async def entrypoint(ctx: agents.JobContext):
    """
    Main entry point for the LiveKit voice agent.
    
    Handles the complete lifecycle of a call:
    1. Parse job metadata
    2. Initialize AI services (STT, LLM, TTS)
    3. Set up call recording and transcription
    4. Configure silence monitoring
    5. Connect to LiveKit room
    6. Start agent session
    7. Handle call flow
    8. Clean up on completion
    """
    logger.info(f"Agent v{AGENT_VERSION} starting job {getattr(ctx.job, 'id', 'unknown')}")
    
    # Immediately mark this slot as taken so load_fnc reports full to LiveKit.
    # This closes the race window where burst dispatches all see load=0.0.
    global _active_count
    with _count_lock:
        _active_count += 1
        logger.info("Concurrency slot claimed: %d/%d active", _active_count, _MAX_CONCURRENT_CALLS)
    
    # Acquire call slot (safety net — should never block if load_fnc works)
    await _call_semaphore.acquire()
    acquired_call_slot = True
    
    # Get pipeline config
    pipeline_config = get_config()
    
    # Initialize storage
    call_storage = CallStorage()
    agent_storage = AgentStorage()
    
    # Parse metadata
    phone_number = None
    voice_id = os.getenv("DEFAULT_CARTESIA_VOICE_ID", "95d51f79-c397-46f9-b49a-23763d3eaa2d")
    job_id = None
    call_log_id = None
    call_mode = "inbound"
    from_number = None
    to_number = None
    added_context = None
    agent_id: int | None = None
    tts_provider_override = None
    tts_overrides: dict[str, str] = {}
    voice_accent = None
    llm_provider_override = None
    llm_model_override = None
    initiating_user_id: str | None = None
    lead_id: str | None = None  # For vertical routing
    knowledge_base_store_ids: list[str] = []
    outbound_trunk_id: str | None = None  # SIP trunk ID from number rules
    batch_id: str | None = None  # Batch call tracking
    entry_id: str | None = None  # Batch entry tracking
    is_realtime: bool = False  # Realtime model mode (Ultravox, etc.)
    realtime_provider: str | None = None  # Realtime provider name
    
    try:
        if ctx.job.metadata:
            dial_info = json.loads(ctx.job.metadata)
            phone_number = dial_info.get("phone_number")
            to_number = phone_number
            job_id = dial_info.get("job_id")
            call_log_id = dial_info.get("call_log_id")
            lead_id = dial_info.get("lead_id")  # For vertical routing
            call_mode = dial_info.get("call_mode", "inbound")
            from_number = dial_info.get("from_number")
            added_context = dial_info.get("added_context")
            outbound_trunk_id = dial_info.get("outbound_trunk_id")
            batch_id = dial_info.get("batch_id")  # For batch completion tracking
            entry_id = dial_info.get("entry_id")  # For batch entry status update
            
            raw_agent_id = dial_info.get("agent_id")
            if isinstance(raw_agent_id, (int, str)):
                try:
                    agent_id = int(str(raw_agent_id).strip())
                except (ValueError, TypeError):
                    pass
            
            raw_kb_ids = dial_info.get("knowledge_base_store_ids")
            if isinstance(raw_kb_ids, list):
                knowledge_base_store_ids = [s.strip() for s in raw_kb_ids if isinstance(s, str) and s.strip()]
            
            voice_id = dial_info.get("voice_id", voice_id)
            # Prefer tts_voice_id (provider-specific) over voice_id (DB UUID)
            tts_voice_id = dial_info.get("tts_voice_id")
            if tts_voice_id:
                voice_id = tts_voice_id  # Use the Cartesia/ElevenLabs voice ID
            voice_accent = dial_info.get("voice_accent")
            # Check both tts_provider and voice_provider (call_service uses voice_provider)
            tts_provider_override = dial_info.get("tts_provider") or dial_info.get("voice_provider")
            llm_provider_override = dial_info.get("llm_provider")
            llm_model_override = dial_info.get("llm_model")
            
            # Parse TTS config overrides (speed/pitch/stability from provider_config)
            raw_tts_config = dial_info.get("tts_config")
            if isinstance(raw_tts_config, dict):
                for k, v in raw_tts_config.items():
                    if v is not None:
                        tts_overrides[k] = str(v)
                if tts_overrides:
                    logger.info(f"TTS config overrides from DB: {tts_overrides}")
            
            raw_initiated_by = dial_info.get("initiated_by")
            if isinstance(raw_initiated_by, (int, str)):
                initiating_user_id = str(raw_initiated_by).strip() or None
            
            # DEBUG: Log initiated_by parsing for OAuth tool debugging
            logger.info(
                "OAuth user context: raw_initiated_by=%s, initiating_user_id=%s",
                raw_initiated_by, initiating_user_id
            )
                
            logger.info(
                "Metadata: job_id=%s, call_log_id=%s, agent_id=%s, voice_id=%s",
                job_id, call_log_id, agent_id, voice_id
            )
            
            # Realtime model detection (e.g., ultravox)
            is_realtime = bool(dial_info.get("is_realtime", False))
            realtime_provider = dial_info.get("realtime_provider")
            if is_realtime:
                logger.info(
                    "REALTIME MODE detected: provider=%s, voice=%s",
                    realtime_provider, voice_id
                )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to parse metadata: {e}")
    
    # Warn if call_log_id is missing - this should never happen for outbound calls
    if not call_log_id:
        logger.warning(
            "call_log_id is None after metadata parsing. "
            "job_id=%s, call_mode=%s, agent_id=%s, raw_metadata=%s",
            job_id, call_mode, agent_id, ctx.job.metadata[:200] if ctx.job.metadata else None
        )
    
    # =========================================================================
    # EARLY BATCH CANCELLATION CHECK
    # If this call belongs to a cancelled batch, skip it entirely.
    # This handles the case where all calls were dispatched to LiveKit Cloud
    # before the cancel request was processed — the worker checks DB before
    # connecting to the room and making the actual call.
    # =========================================================================
    if batch_id and call_log_id:
        from db.storage.batches import BatchStorage
        batch_storage_check = BatchStorage()
        is_cancelled = await batch_storage_check.is_batch_stopped(batch_id)
        if is_cancelled:
            logger.info(
                "Batch %s is cancelled — skipping call %s (job=%s)",
                batch_id, call_log_id, job_id
            )
            # Update call status so it's not stuck as "in_queue"
            await call_storage.update_call_metadata(call_log_id, status="cancelled")
            # Update batch entry status if entry_id is available
            if entry_id:
                await batch_storage_check.update_batch_entry_status(entry_id, "cancelled")
                await batch_storage_check.increment_batch_counters(batch_id, cancelled_delta=1)
            # Release call slot and exit
            _call_semaphore.release()
            with _count_lock:
                _active_count = max(_active_count - 1, 0)
                logger.info("Concurrency slot released (batch cancel): %d/%d active", _active_count, _MAX_CONCURRENT_CALLS)
            ctx.shutdown()
            return
    
    # Connect to room
    await ctx.connect()
    
    # Load agent configuration
    agent_record = None
    is_glinks_agent = False
    system_instructions = ""
    agent_instructions = ""
    
    if agent_id is not None:
        agent_record = await agent_storage.get_agent_by_id(agent_id)
        if agent_record:
            # Column names match the agents.py SELECT query
            system_instructions = agent_record.get("system_instructions", "") or ""
            agent_instructions = agent_record.get("agent_instructions", "") or ""
            
            # Check if Glinks agent
            org_id = agent_record.get("organization_id")
            if org_id and str(org_id) == VoiceAssistant.GLINKS_ORG_ID:
                is_glinks_agent = True
    
    # Phase 4: Get tenant_id for multi-tenancy (from USER's primary_tenant_id, not agent)
    tenant_id = None
    if initiating_user_id:
        from db.storage.tokens import UserTokenStorage
        token_storage = UserTokenStorage()
        tenant_id = await token_storage.get_user_tenant_id(initiating_user_id)
        if tenant_id:
            logger.debug(f"Resolved tenant_id={tenant_id} from user_id={initiating_user_id}")
    # Fallback: try agent's tenant if no user (inbound calls)
    if not tenant_id and agent_id:
        tenant_id = await agent_storage.get_agent_tenant_id(agent_id)
        if tenant_id:
            logger.debug(f"Resolved tenant_id={tenant_id} from agent_id={agent_id} (fallback)")
    # Resolve LLM configuration
    llm_provider, llm_model = resolve_llm_configuration(
        llm_provider_override, llm_model_override
    )
    
    # =========================================================================
    # BRANCH: Realtime Model vs Pipeline Mode
    # =========================================================================
    realtime_model = None
    tts_engine = None
    stt_engine = None
    vad = None
    llm_instance = None
    tts_details = None
    
    if is_realtime and realtime_provider:
        # ---- REALTIME MODE ----
        # Realtime models (Ultravox, etc.) handle STT + LLM + TTS internally.
        # We create a RealtimeModel and pass it as llm= to AgentSession.
        from agent.providers.realtime_builder import create_realtime_model
        
        # Build realtime overrides from tts_overrides (which are actually
        # provider_config values passed through the metadata pipeline)
        realtime_overrides = dict(tts_overrides)  # from provider_config JSONB
        
        # The voice_id in realtime mode is the provider voice name (e.g., "Mark")
        if voice_id and "voice" not in realtime_overrides:
            realtime_overrides["voice"] = voice_id
        
        logger.info(
            "Creating realtime model: provider=%s, overrides=%s",
            realtime_provider, realtime_overrides
        )
        realtime_model = create_realtime_model(
            provider=realtime_provider,
            overrides=realtime_overrides,
        )
        logger.info("Realtime model created: %s", type(realtime_model).__name__)
    else:
        # ---- PIPELINE MODE (existing behavior) ----
        # Build separate STT, LLM, TTS, and VAD components.
        tts_provider_final = tts_provider_override or os.getenv("TTS_PROVIDER", "cartesia")
        logger.info(f"TTS provider: {tts_provider_final} (override={tts_provider_override})")
        tts_engine, tts_details = build_tts_engine(
            tts_provider_final,
            default_voice_id=voice_id,
            overrides=tts_overrides,
            accent=voice_accent,
        )
        logger.info(f"TTS engine built: {tts_details}")
        
        # Create LLM instance
        file_search_stores = [s for s in knowledge_base_store_ids if s.startswith("fileSearchStores/")]
        llm_instance = create_llm_instance(llm_provider, llm_model, file_search_stores or None)
        
        # Build STT
        stt_language = derive_stt_language(voice_accent, pipeline_config.stt.language)
        stt_engine = deepgram.STT(
            model=pipeline_config.stt.model,
            language=stt_language,
        )
        
        # Build VAD
        vad = silero.VAD.load(
            min_silence_duration=pipeline_config.vad.min_silence_duration,
            min_speech_duration=pipeline_config.vad.min_speech_duration,
            activation_threshold=pipeline_config.vad.activation_threshold,
            force_cpu=pipeline_config.vad.force_cpu,
        )
    
    # Fetch tools BEFORE building instructions (Phase 17c: tool instructions require valid tool_config)
    # NOTE: This must happen before build_instructions_async() so hangup instruction is included!
    tool_config, tool_configs = await get_enabled_tools({}, tenant_id)
    
    # Holder for VoiceAssistant - populated after creation, used by human_support tool
    voice_assistant_holder: dict = {"assistant": None}
    
    # Create audit trail for tool usage tracking
    from utils.audit_trail import ToolAuditTrail
    audit_trail = ToolAuditTrail(tenant_id=tenant_id)
    
    tool_list = await attach_tools(
        None,  # Agent not needed here, just building tools
        tool_config,
        tool_configs,
        tenant_id=tenant_id,
        user_id=initiating_user_id,
        knowledge_base_store_ids=knowledge_base_store_ids,
        job_context=ctx,  # For human support SIP transfer
        sip_trunk_id=outbound_trunk_id,  # SIP trunk from call routing
        from_number=from_number,  # For number validation in human support
        voice_assistant_holder=voice_assistant_holder,  # For human handoff muting
        audit_trail=audit_trail,  # For tool call logging
    )
    
    # Record which tools were provided to LLM
    audit_trail.set_tools_provided([getattr(t, '__name__', str(t)) for t in tool_list])
    logger.info(f"Built {len(tool_list)} tools for tenant {tenant_id}, user_id={'set' if initiating_user_id else 'None'}")
    
    # Build instructions (now with valid tool_config that includes hangup instructions)
    instructions = await build_instructions_async(
        system_instructions=system_instructions,
        agent_instructions=agent_instructions,
        added_context=added_context,
        direction=call_mode,
        tool_config=tool_config,  # Actual tool config from get_enabled_tools()
        tenant_id=tenant_id,  # Pass actual tenant_id, not None
    )
    
    # Create call recorder
    call_recorder = None
    if os.getenv("ENABLE_RECORDING", "true").lower() == "true" and call_log_id:
        call_recorder = CallRecorder(
            room_name=ctx.room.name,
            call_id=call_log_id,
            livekit_api=ctx.api,
        )
    
    # Phase 1: Create usage collector for cost tracking
    usage_collector = None
    if is_component_tracking_enabled():
        usage_collector = UsageCollector()
        if is_realtime and realtime_provider:
            # Realtime mode: single model handles STT + LLM + TTS
            usage_collector.set_tts_config(realtime_provider, voice_id or "unknown")
            usage_collector.set_llm_config(realtime_provider, realtime_overrides.get("model", "unknown"))
            usage_collector.set_stt_config(realtime_provider, "realtime")
            logger.info(
                "UsageCollector enabled (REALTIME): provider=%s, voice=%s",
                realtime_provider, voice_id,
            )
        else:
            # Pipeline mode: separate TTS/LLM/STT providers
            tts_provider_final = tts_provider_override or os.getenv("TTS_PROVIDER", "cartesia")
            usage_collector.set_tts_config(tts_provider_final, voice_id or "unknown")
            usage_collector.set_llm_config(llm_provider, llm_model)
            usage_collector.set_stt_config("deepgram", pipeline_config.stt.model)
            logger.info(
                "UsageCollector enabled: TTS=%s/%s, LLM=%s/%s, STT=deepgram/%s",
                tts_provider_final, voice_id, llm_provider, llm_model, pipeline_config.stt.model
            )
    else:
        logger.debug("Component cost tracking disabled (ENABLE_COMPONENT_COST_TRACKING=false)")
    # Create silence monitor with warning milestone
    silence_timeout = pipeline_config.silence.silence_timeout_seconds
    
    # Session holder for callbacks (populated after session creation)
    session_holder: dict = {"session": None}

    async def on_silence_warning(elapsed: float):
        """Prompt user when silent for 15s."""
        logger.info(f"Silence warning at {elapsed:.1f}s - prompting user")
        
        # Log to audit trail
        audit_trail.log_silence_warning(elapsed)
        
        # IMPORTANT: Skip resetting timer when agent speaks the warning prompt
        # Otherwise we'd never reach the 35s timeout
        silence_monitor.skip_next_agent_reset()
        
        sess = session_holder.get("session")
        if sess:
            try:
                await sess.generate_reply(
                    instructions="The user has been silent for a while. "
                    "Ask if they are still there and if they need any help. "
                    "Keep it brief and friendly."
                )
            except Exception as e:
                logger.warning(f"Failed to send silence warning prompt: {e}")

    async def on_silence_timeout():
        """End call after full silence timeout."""
        logger.info("Silence timeout - ending call")
        
        # Log to audit trail
        audit_trail.log_silence_hangup(silence_timeout)
        if ctx.room:
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception as e:
                logger.error(f"Error ending call on timeout: {e}")

    # Warning and hangup from config
    silence_warning = pipeline_config.silence.silence_warning_seconds
    silence_milestones = [
        SilenceMilestone(
            seconds=silence_warning,
            callback=on_silence_warning,
            label="first_warning"
        ),
    ]

    silence_monitor = SilenceMonitor(
        timeout_seconds=silence_timeout,
        on_timeout=on_silence_timeout,
        logger=logger,
        milestones=silence_milestones,
    )
    
    # Create voice assistant with tools (tools already fetched above before instruction building)
    voice_assistant = VoiceAssistant(
        call_recorder=call_recorder,
        job_context=ctx,
        instructions=instructions,
        tools=tool_list,  # Pass dynamic tools
        silence_monitor=silence_monitor,
        initiating_user_id=initiating_user_id,
        is_glinks_agent=is_glinks_agent,
        knowledge_base_store_ids=knowledge_base_store_ids,
        audit_trail=audit_trail,  # For tool call logging
    )
    
    # Populate holder so human_support tool can access VoiceAssistant
    voice_assistant_holder["assistant"] = voice_assistant
    
    # Create agent session - branch on realtime vs pipeline mode
    if is_realtime and realtime_model:
        # REALTIME MODE: pass realtime model as llm=, no STT/TTS/VAD
        session = AgentSession(
            llm=realtime_model,
            allow_interruptions=pipeline_config.interruption.allow_interruptions,
            min_interruption_duration=pipeline_config.interruption.min_interruption_duration,
        )
        logger.info("AgentSession created in REALTIME mode (provider=%s)", realtime_provider)
    else:
        # PIPELINE MODE: pass separate LLM, TTS, STT, VAD
        session = AgentSession(
            llm=llm_instance,
            tts=tts_engine,
            stt=stt_engine,
            vad=vad,
            min_endpointing_delay=pipeline_config.endpointing.min_endpointing_delay,
            max_endpointing_delay=pipeline_config.endpointing.max_endpointing_delay,
            # Interruption settings
            allow_interruptions=pipeline_config.interruption.allow_interruptions,
            min_interruption_duration=pipeline_config.interruption.min_interruption_duration,
            min_interruption_words=pipeline_config.interruption.min_interruption_words,
            # False interruption handling (resume if backchannel)
            false_interruption_timeout=pipeline_config.interruption.false_interruption_timeout,
            resume_false_interruption=pipeline_config.interruption.resume_false_interruption,
        )
        logger.info("AgentSession created in PIPELINE mode (llm=%s, tts=%s)", llm_provider, tts_details)
    
    # Populate session reference for silence warning callback
    session_holder["session"] = session
    
    # Set session reference in HumanAwareAgent for muting on human handoff
    voice_assistant.set_session(session)
    logger.debug("Session reference set in voice_assistant for human handoff pause")
    
    # Set session reference in human support tool for background task
    for tool in tool_list:
        if hasattr(tool, 'set_session'):
            tool.set_session(session)
            logger.info("[HumanSupport] Session reference set in tool")
    
    # Track last agent state for state change detection
    last_agent_state = None
    
    # Hook up session events to silence monitor (using correct event names from old agent.py)
    # Event to track when agent first speaks (for background audio timing)
    agent_spoke_event_holder: dict = {"event": None}
    
    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev):
        nonlocal last_agent_state
        new_state = getattr(ev, "new_state", None)
        
        if new_state == "speaking":
            logger.info("[SilenceMonitor] Event: agent_state_changed → speaking, pausing timer")
            if voice_assistant.silence_monitor:
                voice_assistant.silence_monitor.notify_agent_started()
            # Signal that agent has spoken (for background audio timing)
            spoke_event = agent_spoke_event_holder.get("event")
            if spoke_event and not spoke_event.is_set():
                spoke_event.set()
        elif last_agent_state == "speaking":
            # Agent just stopped speaking
            logger.info("[SilenceMonitor] Event: agent_state_changed → stopped speaking, starting timer")
            if voice_assistant.silence_monitor:
                voice_assistant.silence_monitor.notify_agent_completed()
        
        last_agent_state = new_state
    
    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        new_state = getattr(ev, "new_state", None)
        if new_state == "speaking":
            logger.info("[SilenceMonitor] Event: user_state_changed → speaking, resetting timer")
            if voice_assistant.silence_monitor:
                voice_assistant.silence_monitor.notify_user_activity()
    
    # Human support participant event handler - just for logging
    # The actual handoff logic is in the background task in tool_builder.py
    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant):
        """Log when new participants join (human support handled by background task)."""
        identity = getattr(participant, 'identity', '') or ''
        if identity.startswith("support-"):
            logger.info(f"[HumanSupport] Participant connected event: {identity}")
    
    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        """
        Handle participant disconnects - end call if human support or client leaves.
        
        When in listening-only mode (after human handoff), the call should end
        automatically when either:
        - The human support agent disconnects
        - The client/prospect disconnects
        """
        identity = getattr(participant, 'identity', '') or ''
        
        # Check if we're in listening-only mode (human handoff active)
        if voice_assistant._agent_listening_only:
            if identity.startswith("support-"):
                # Human support agent left - end the call
                logger.info(f"[HumanHandoff] Human support disconnected: {identity} - ending call")
            else:
                # Client/prospect left - end the call  
                logger.info(f"[HumanHandoff] Client disconnected: {identity} - ending call")
            
            # Delete the room to end the call for all participants
            async def _end_call():
                try:
                    await ctx.api.room.delete_room(
                        api.DeleteRoomRequest(room=ctx.room.name)
                    )
                    logger.info(f"[HumanHandoff] Room {ctx.room.name} deleted - call ended")
                except Exception as e:
                    logger.warning(f"[HumanHandoff] Error deleting room: {e}")
            
            import asyncio
            asyncio.create_task(_end_call())
    
    # Phase 2: Attach usage collector to session for metrics tracking
    if usage_collector:
        from utils.usage_tracker import attach_usage_collector
        attach_usage_collector(session, usage_collector)
        logger.debug("UsageCollector attached to AgentSession")
    
    # Phase 3: Register cleanup callback with full context
    cleanup_ctx = CleanupContext(
        call_recorder=call_recorder,
        call_log_id=call_log_id,
        tenant_id=tenant_id,  # Phase 4: Now included
        lead_id=lead_id,  # For vertical routing
        call_storage=call_storage,
        silence_monitor=silence_monitor,
        usage_collector=usage_collector,  # Uses the configured one from Phase 1
        call_semaphore=_call_semaphore,
        acquired_call_slot=acquired_call_slot,
        job_id=job_id,
        batch_id=batch_id,  # For batch completion tracking
        entry_id=entry_id,  # For batch entry status update
        audit_trail=audit_trail,  # Tool usage audit trail
        from_number=from_number,  # For provider-based telephony costing
    )
    
    # Background audio cleanup holders (populated later)
    background_audio_cleanup: dict = {
        "latency_mask_player": None,
        "ambience_player": None,
        "background_audio_task": None,
    }
    
    async def shutdown_callback():
        logger.info("=" * 60)
        logger.info("CALL ENDED - Running cleanup_and_save")
        logger.info("=" * 60)
        
        # Stop background audio task
        bg_task = background_audio_cleanup.get("background_audio_task")
        if bg_task and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass
        
        # Stop both audio players
        for key in ["latency_mask_player", "ambience_player"]:
            player = background_audio_cleanup.get(key)
            if player:
                stop_callable = getattr(player, "stop", None)
                if callable(stop_callable):
                    try:
                        maybe_coro = stop_callable()
                        if asyncio.iscoroutine(maybe_coro):
                            await maybe_coro
                    except Exception:
                        ambient_logger.warning(f"Failed to stop {key} cleanly", exc_info=True)
        
        await cleanup_and_save(cleanup_ctx)
    
    ctx.add_shutdown_callback(shutdown_callback)
    
    # Start recording
    if call_recorder:
        await call_recorder.start_recording()
    
    # ==========================================================================
    # BACKGROUND AUDIO CONFIGURATION
    # ==========================================================================
    # Background audio adds realism (office sounds, typing) during calls.
    # We use TWO separate players:
    # 1. latency_mask_player - typing sounds ONLY, started IMMEDIATELY for latency masking
    # 2. ambience_player - office/call center sounds, started AFTER first speech
    latency_mask_player: BackgroundAudioPlayer | None = None
    ambience_player: BackgroundAudioPlayer | None = None
    background_audio_task: asyncio.Task | None = None
    agent_spoke_event = asyncio.Event()  # Tracks when agent first speaks
    
    # Connect to the holder so event handler (defined earlier) can signal this event
    agent_spoke_event_holder["event"] = agent_spoke_event
    
    # Configuration from pipeline config (with env override support)
    def _flag_enabled(env_key: str, config_default: bool) -> bool:
        env_val = os.getenv(env_key)
        if env_val is not None:
            return env_val.lower() in ("true", "1", "yes")
        return config_default
    
    typing_audio_enabled = _flag_enabled("ENABLE_TYPING_NOISE", pipeline_config.ambience.enable_typing_noise)
    ambient_audio_enabled = _flag_enabled("ENABLE_OFFICE_AMBIENCE", pipeline_config.ambience.enable_office_ambience)
    people_talking_enabled = _flag_enabled("ENABLE_PEOPLE_TALKING", pipeline_config.ambience.enable_people_talking)
    
    typing_noise_volume = pipeline_config.ambience.typing_volume
    office_ambience_volume = pipeline_config.ambience.ambience_volume
    people_talking_volume = pipeline_config.ambience.people_talking_volume
    
    # Use custom audio path for people talking (relative to v2 directory)
    people_talking_audio_path = pipeline_config.ambience.people_talking_audio_path
    if not people_talking_audio_path:
        # Default to our copied audio file
        people_talking_audio_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "audio", "office-ambience-6322.mp3"
        )
    
    async def _start_latency_masking() -> None:
        """Start latency mask player IMMEDIATELY with typing sounds."""
        nonlocal latency_mask_player
        
        if not typing_audio_enabled:
            ambient_logger.info("Typing audio (latency masking) disabled via configuration")
            return
        
        try:
            latency_mask_player = BackgroundAudioPlayer(
                thinking_sound=[
                    AudioConfig(
                        source=BuiltinAudioClip.KEYBOARD_TYPING,
                        volume=typing_noise_volume,
                        probability=1.0,
                    ),
                    AudioConfig(
                        source=BuiltinAudioClip.KEYBOARD_TYPING2,
                        volume=typing_noise_volume,
                        probability=1.0,
                    ),
                ]
            )
            await latency_mask_player.start(
                room=ctx.room,
                agent_session=session,
            )
            ambient_logger.info("Latency masking (typing sounds) started IMMEDIATELY")
        except Exception as e:
            ambient_logger.warning(f"Failed to start latency masking: {e}")
    
    async def _start_ambience_after_first_speech() -> None:
        """Start office/call center ambience AFTER first agent speech."""
        nonlocal ambience_player
        
        if not ambient_audio_enabled and not people_talking_enabled:
            ambient_logger.info("All ambience audio disabled via configuration")
            return
        
        # Wait for agent to speak first (set by agent_state_changed event)
        if not agent_spoke_event.is_set():
            await agent_spoke_event.wait()
        
        # Additional delay after first speech for clean audio
        await asyncio.sleep(pipeline_config.ambience.delay_after_first_speech)
        
        try:
            # Determine ambience source - use people talking path if enabled, else built-in
            if people_talking_enabled and people_talking_audio_path and os.path.exists(people_talking_audio_path):
                ambience_source = people_talking_audio_path
                ambience_vol = people_talking_volume
                ambient_logger.info(f"Using custom ambience: {people_talking_audio_path}")
            elif ambient_audio_enabled:
                ambience_source = BuiltinAudioClip.OFFICE_AMBIENCE
                ambience_vol = office_ambience_volume
                ambient_logger.info("Using built-in office ambience")
            else:
                ambient_logger.info("No ambience source configured")
                return
            
            ambience_player = BackgroundAudioPlayer(
                ambient_sound=AudioConfig(
                    source=ambience_source,
                    volume=ambience_vol,
                )
            )
            await ambience_player.start(
                room=ctx.room,
                agent_session=session,
            )
            ambient_logger.info(f"Ambience audio started after first agent speech")
        except Exception as e:
            ambient_logger.warning(f"Failed to start ambience audio: {e}")
    
    # Start session
    room_input_options = RoomInputOptions()
    session_start_task = asyncio.create_task(
        session.start(room=ctx.room, agent=voice_assistant, room_input_options=room_input_options)
    )
    
    # Start latency masking immediately
    await _start_latency_masking()
    
    # Start ambience task (will wait for first TTS before playing)
    background_audio_task = asyncio.create_task(_start_ambience_after_first_speech())
    
    # Connect to cleanup holders for proper shutdown
    background_audio_cleanup["latency_mask_player"] = latency_mask_player
    background_audio_cleanup["background_audio_task"] = background_audio_task
    # Note: ambience_player is set inside _start_ambience_after_first_speech via nonlocal
    
    # Update holder periodically since ambience_player is set asynchronously
    async def _update_ambience_cleanup():
        await asyncio.sleep(5)  # Wait for ambience to potentially start
        background_audio_cleanup["ambience_player"] = ambience_player
    asyncio.create_task(_update_ambience_cleanup())
    
    try:
        if call_mode == "outbound" and phone_number:
            # Outbound call - dial out, use trunk from metadata or env default
            trunk_id = outbound_trunk_id or os.getenv("OUTBOUND_TRUNK_ID")
            if trunk_id:
                try:
                    # Update status to ringing before dialing
                    if call_log_id:
                        await call_storage.update_call_status(
                            call_log_id=call_log_id,
                            status="ringing"
                        )
                        logger.info(f"Call {call_log_id} status updated to ringing")
                    
                    await ctx.api.sip.create_sip_participant(
                        api.CreateSIPParticipantRequest(
                            room_name=ctx.room.name,
                            sip_trunk_id=trunk_id,
                            sip_call_to=phone_number,
                            participant_identity=f"dial-{phone_number}",
                            wait_until_answered=True,
                            krisp_enabled=True,
                        )
                    )
                    # Call is now answered - update status to ongoing
                    if call_log_id:
                        await call_storage.update_call_status(
                            call_log_id=call_log_id,
                            status="ongoing"
                        )
                        logger.info(f"Call {call_log_id} status updated to ongoing")
                        
                        # Start silence monitor now that call is active
                        silence_monitor.start()
                    
                    await session_start_task
                    
                    if call_recorder:
                        attach_transcription_tracker(session, call_recorder)
                    
                    # Outbound greeting - try agent starter prompt, then added_context, then default
                    # Make initial greeting uninterruptible to prevent "hello collision"
                    # This allows AEC calibration and prevents 2-3 turn sync issues
                    try:
                        greeting_uninterruptible = pipeline_config.greeting.greeting_uninterruptible
                        greeting = agent_record.get("outbound_starter_prompt", "") if agent_record else ""
                        if greeting:
                            logger.info(f"Sending outbound greeting (uninterruptible={greeting_uninterruptible}): {greeting[:50]}...")
                            await session.generate_reply(instructions=greeting, allow_interruptions=not greeting_uninterruptible)
                        elif added_context:
                            # Use added_context as greeting instruction
                            logger.info(f"Using added_context as greeting (uninterruptible={greeting_uninterruptible}): {added_context[:50]}...")
                            await session.generate_reply(instructions=f"Greet the user. Context: {added_context}", allow_interruptions=not greeting_uninterruptible)
                        else:
                            # Default: just start the conversation
                            logger.info(f"No greeting set, starting conversation with default (uninterruptible={greeting_uninterruptible})")
                            await session.generate_reply(instructions="Greet the user warmly and ask how you can help them today.", allow_interruptions=not greeting_uninterruptible)
                    except RuntimeError as e:
                        logger.error(f"Failed to send greeting (session may have crashed): {e}")
                        
                except api.TwirpError as e:
                    logger.error(f"SIP dial error: {e.message}")
                    if call_log_id:
                        await call_storage.update_call_status(
                            call_log_id=call_log_id,
                            status="failed",
                            ended_at=datetime.now(timezone.utc)
                        )
                    session_start_task.cancel()
                    ctx.shutdown()
                    return
        else:
            # Inbound call
            await session_start_task
            
            # Inbound call is now active - update status to ongoing
            if call_log_id:
                await call_storage.update_call_status(
                    call_log_id=call_log_id,
                    status="ongoing"
                )
                logger.info(f"Inbound call {call_log_id} status updated to ongoing")
            
            # Start silence monitor now that call is active
            silence_monitor.start()
            
            if call_recorder:
                attach_transcription_tracker(session, call_recorder)
            
            # Make initial greeting uninterruptible to prevent "hello collision"
            # This allows AEC calibration and prevents 2-3 turn sync issues
            greeting_uninterruptible = pipeline_config.greeting.greeting_uninterruptible
            greeting = agent_record.get("inbound_starter_prompt", "") if agent_record else ""
            if greeting:
                logger.info(f"Sending inbound greeting (uninterruptible={greeting_uninterruptible}): {greeting[:50]}...")
                await session.generate_reply(instructions=greeting, allow_interruptions=not greeting_uninterruptible)
                
    finally:
        if acquired_call_slot:
            _call_semaphore.release()
            with _count_lock:
                _active_count = max(_active_count - 1, 0)
                logger.info("Concurrency slot released (call end): %d/%d active", _active_count, _MAX_CONCURRENT_CALLS)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    logger.info(f"Starting LiveKit agent worker - Version {AGENT_VERSION}")
    logger.info(f"Max concurrent calls: {_MAX_CONCURRENT_CALLS}")
    
    try:
        agents.cli.run_app(
            agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                agent_name=os.getenv("VOICE_AGENT_NAME", "inbound-agent"),
                # port=0 → auto-pick available port (avoids conflicts when scaling)
                port=0,
                initialize_process_timeout=60.0,
                shutdown_process_timeout=60.0,
                load_fnc=calculate_worker_load,
                load_threshold=1.0,
                num_idle_processes=0,
            )
        )
    finally:
        stop_logging()

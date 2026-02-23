"""
Tool Audit Trail Module.

Tracks all tool calls, responses, and async events during a call.
Stored as nested JSON in metadata column of voice_call_logs.

Events logged:
- Tools provided to LLM
- Each tool call (input, output, timestamp, status)
- Async follow-ups (KB search result, human joined/failed)
- Silence warnings and auto-hangup
- Agent hangup (reason, cancelled/completed)
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class ToolAuditTrail:
    """
    Collects tool usage events during a call for audit purposes.
    
    Thread-safe event logging with timestamps.
    Non-blocking - errors logged but don't affect call flow.
    """
    
    def __init__(self, tenant_id: str | None = None):
        self.tenant_id = tenant_id
        self.tools_provided: list[str] = []
        self.events: list[dict] = []
    
    def _timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    
    def _log_event(self, event: dict) -> None:
        """Add event with timestamp."""
        event["ts"] = self._timestamp()
        self.events.append(event)
        logger.debug(f"Audit event: {event.get('type')} - {event.get('tool', event.get('action', ''))}")
    
    def set_tools_provided(self, tools: list[str]) -> None:
        """Record list of tools provided to LLM."""
        self.tools_provided = tools
        logger.debug(f"Audit: tools_provided={tools}")
    
    def log_tool_call(
        self,
        tool_name: str,
        input_data: dict | None = None,
        output: str = "",
        status: str = "complete"
    ) -> None:
        """
        Log a tool call event.
        
        Args:
            tool_name: Name of the tool called
            input_data: Input arguments to the tool
            output: Brief output/response (truncated for large responses)
            status: "complete", "pending", "error"
        """
        self._log_event({
            "type": "tool_call",
            "tool": tool_name,
            "input": input_data or {},
            "output": output[:500] if output else "",  # Truncate large outputs
            "status": status,
        })
    
    def log_tool_result(
        self,
        tool_name: str,
        status: str,
        output: str = ""
    ) -> None:
        """
        Log async tool result (e.g., KB search completed, human joined).
        
        Args:
            tool_name: Name of the tool
            status: "complete", "failed", "timeout"
            output: Result description
        """
        self._log_event({
            "type": "tool_result",
            "tool": tool_name,
            "status": status,
            "output": output[:500] if output else "",
        })
    
    def log_silence_warning(self, elapsed_sec: float) -> None:
        """Log silence warning event (prompted user if still there)."""
        self._log_event({
            "type": "silence_warning",
            "elapsed_sec": round(elapsed_sec, 1),
            "action": "prompted_user",
        })
    
    def log_silence_hangup(self, elapsed_sec: float) -> None:
        """Log auto-hangup due to silence timeout."""
        self._log_event({
            "type": "silence_hangup",
            "elapsed_sec": round(elapsed_sec, 1),
            "action": "auto_hangup",
            "note": "No parting words - auto hangup after silence timeout",
        })
    
    def log_agent_hangup(
        self,
        reason: str,
        status: str = "completed"
    ) -> None:
        """
        Log agent-initiated hangup.
        
        Args:
            reason: Reason provided by agent (e.g., "call_complete", "not_interested")
            status: "completed", "cancelled_interrupted", "pending"
        """
        self._log_event({
            "type": "agent_hangup",
            "reason": reason,
            "status": status,
        })
    
    def log_human_handoff_started(self, dial_number: str) -> None:
        """Log human support dial initiated."""
        # Mask phone number for privacy
        masked = f"{dial_number[:4]}***" if dial_number and len(dial_number) > 4 else "***"
        self._log_event({
            "type": "human_handoff_started",
            "dial_number": masked,
            "status": "dialing",
        })
    
    def log_human_handoff_joined(self) -> None:
        """Log human support agent joined the call."""
        self._log_event({
            "type": "human_handoff_joined",
            "status": "joined",
            "action": "ai_muted",
        })
    
    def log_human_handoff_failed(self, error: str) -> None:
        """Log human support dial failed."""
        self._log_event({
            "type": "human_handoff_failed",
            "status": "failed",
            "error": error[:200] if error else "unknown",
        })
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return {
            "tenant_id": self.tenant_id,
            "tools_provided": self.tools_provided,
            "events": self.events,
        }


# Convenience function for non-blocking logging
def safe_log_event(audit_trail: ToolAuditTrail | None, log_method: str, *args, **kwargs) -> None:
    """
    Safely log an event without raising exceptions.
    
    Args:
        audit_trail: ToolAuditTrail instance (can be None)
        log_method: Method name to call (e.g., "log_tool_call")
        *args, **kwargs: Arguments to pass to the method
    """
    if audit_trail is None:
        return
    
    try:
        method = getattr(audit_trail, log_method, None)
        if method and callable(method):
            method(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Failed to log audit event {log_method}: {e}")

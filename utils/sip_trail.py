"""
SIP Trail Module.

Tracks the SIP call lifecycle for diagnostic purposes.
Stored as nested JSON in metadata column of voice_call_logs.

Events logged:
- Room created
- SIP dial started (trunk_id, phone_number)
- SIP answered (call_sid, sip_status_code)
- SIP failed (error details, sip_status_code, sip_status)
- SIP ended (disconnect_reason, duration)

Also resolves `status_reason` — a human-readable top-level field
explaining WHY the call ended or failed.
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# SIP status code → status_reason mapping
_SIP_CODE_TO_REASON = {
    401: "sip_trunk_auth_failure",
    403: "sip_carrier_rejected",
    404: "sip_number_not_found",
    407: "sip_trunk_auth_failure",
    408: "sip_carrier_timeout",
    480: "callee_unavailable",
    486: "callee_busy",
    500: "sip_carrier_error",
    502: "sip_carrier_error",
    503: "sip_carrier_unavailable",
    504: "sip_carrier_timeout",
    600: "callee_busy",
    603: "callee_rejected",
    604: "sip_number_not_found",
}


class SipTrailLogger:
    """
    Collects SIP lifecycle events during a call for diagnostics.

    Same pattern as ToolAuditTrail — event collection + to_dict().
    Non-blocking - errors logged but don't affect call flow.
    """

    def __init__(self):
        self.trunk_id: str | None = None
        self.trunk_resolution: str | None = None  # "database" or "environment"
        self.phone_number: str | None = None
        self.carrier_name: str | None = None
        self.events: list[dict] = []
        self._status_reason: str | None = None  # Set explicitly on failure

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _log(self, event: dict) -> None:
        event["ts"] = self._ts()
        self.events.append(event)

    # --- Event loggers ---

    def log_room_created(self) -> None:
        self._log({"event": "room_created"})

    def log_sip_dial_started(self, trunk_id: str | None, phone_number: str | None) -> None:
        self.trunk_id = trunk_id
        self.phone_number = phone_number
        self._log({
            "event": "sip_dial_started",
            "trunk_id": trunk_id,
            "phone_number": phone_number,
        })

    def log_sip_answered(self, call_sid: str | None = None) -> None:
        self._log({
            "event": "sip_answered",
            "call_sid": call_sid,
            "sip_status_code": 200,
        })

    def log_sip_failed(
        self,
        error_message: str,
        sip_status_code: int | None = None,
        sip_status: str | None = None,
    ) -> None:
        """Log SIP dial failure and resolve status_reason from SIP code."""
        self._log({
            "event": "sip_failed",
            "error_message": error_message[:300],
            "sip_status_code": sip_status_code,
            "sip_status": sip_status,
        })
        # Resolve status_reason from SIP code
        if sip_status_code and sip_status_code in _SIP_CODE_TO_REASON:
            self._status_reason = _SIP_CODE_TO_REASON[sip_status_code]
        elif sip_status_code:
            # Unknown SIP code — classify by range
            if 400 <= sip_status_code < 500:
                self._status_reason = "sip_carrier_rejected"
            elif 500 <= sip_status_code < 600:
                self._status_reason = "sip_carrier_error"
            elif sip_status_code >= 600:
                self._status_reason = "callee_rejected"
        else:
            # No SIP code — likely trunk not found or LiveKit error
            self._status_reason = "sip_trunk_not_found"

    def log_sip_ended(self, disconnect_reason: str | None = None, duration_seconds: float | None = None) -> None:
        self._log({
            "event": "sip_ended",
            "disconnect_reason": disconnect_reason,
            "duration_seconds": round(duration_seconds, 1) if duration_seconds else None,
        })

    def set_config(
        self,
        trunk_resolution: str | None = None,
        carrier_name: str | None = None,
    ) -> None:
        """Set config metadata from dispatch info."""
        if trunk_resolution:
            self.trunk_resolution = trunk_resolution
        if carrier_name:
            self.carrier_name = carrier_name

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage in metadata."""
        result = {}
        if self.trunk_id:
            result["trunk_id_used"] = self.trunk_id
        if self.trunk_resolution:
            result["trunk_resolution"] = self.trunk_resolution
        if self.phone_number:
            result["phone_number_dialed"] = self.phone_number
        if self.carrier_name:
            result["carrier_name"] = self.carrier_name
        if self.events:
            result["events"] = self.events
        return result

    # --- status_reason resolution ---

    def get_status_reason(self) -> str | None:
        """Return explicitly set status_reason (from SIP failure)."""
        return self._status_reason


def resolve_status_reason(
    audit_trail: Any = None,
    sip_trail: SipTrailLogger | None = None,
    existing_status: str | None = None,
    human_joined: bool = False,
) -> str:
    """
    Determine the top-level status_reason for a call.

    Decision tree:
    1. SIP failure reason (if set by sip_trail)
    2. Agent hangup (audit_trail has agent_hangup event)
    3. Silence timeout (audit_trail has silence_hangup event)
    4. Cancelled (status was cancelled before cleanup)
    5. Human handoff ended (human_joined flag)
    6. Default: receiver_hangup (callee ended the call)
    """
    # 1. SIP-level failure
    if sip_trail:
        sip_reason = sip_trail.get_status_reason()
        if sip_reason:
            return sip_reason

    # 2-3. Check audit trail events
    if audit_trail and hasattr(audit_trail, 'events'):
        for event in reversed(audit_trail.events):
            event_type = event.get("type")
            if event_type == "agent_hangup":
                return "agent_hangup"
            if event_type == "silence_hangup":
                return "silence_timeout"

    # 4. Cancelled
    if existing_status in ("cancelled", "canceled"):
        return "cancelled_by_api"

    # 5. Human handoff
    if human_joined:
        return "human_handoff_ended"

    # 6. Failed status with no specific reason
    if existing_status in ("failed", "error", "not_reachable"):
        return "worker_error"

    # 7. Default — the receiver hung up
    return "receiver_hangup"

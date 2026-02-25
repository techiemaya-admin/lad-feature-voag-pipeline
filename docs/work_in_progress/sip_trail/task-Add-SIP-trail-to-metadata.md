# Task: Add SIP Trail to Call Log Metadata

> **Status**: NOT STARTED — Context captured for future pickup  
> **Related**: `sip-call-lifecycle-analysis.md` in this folder  
> **Date**: 2026-02-25

---

## Problem

When a call fails, we have no idea WHY. The current system only sets `status="failed"` and logs the generic error message. We can't distinguish between:
- Carrier rejected the call (wrong trunk config)
- Number doesn't exist (404)
- Callee busy (486)
- Carrier down (503)
- LiveKit server unreachable
- Trunk ID doesn't exist in LiveKit
- Network timeout

We also don't know which worker handled the call, which LiveKit server was used, or which trunk was actually selected.

---

## `status_reason` — Top-Level Quick Diagnosis Field

A **human-readable, top-level** field in metadata that instantly tells us WHY the call ended or failed. This is the first thing anyone looks at when debugging.

### All Possible Values

#### For Failed Calls (status = `failed`)

| `status_reason` | Trigger | Code Location |
|-----------------|---------|---------------|
| `sip_trunk_not_found` | Trunk ID doesn't exist in LiveKit | `worker.py:1394` — TwirpError with no sip_status_code |
| `sip_trunk_auth_failure` | Trunk creds rejected by carrier | TwirpError, sip_status_code = 401/407 |
| `sip_carrier_rejected` | Carrier rejected (wrong config) | TwirpError, sip_status_code = 403 |
| `sip_number_not_found` | Number doesn't exist | TwirpError, sip_status_code = 404/604 |
| `sip_carrier_timeout` | Carrier didn't respond | TwirpError, sip_status_code = 408 |
| `sip_carrier_error` | Carrier internal error | TwirpError, sip_status_code = 500 |
| `sip_carrier_unavailable` | Carrier overloaded/down | TwirpError, sip_status_code = 503 |
| `livekit_server_error` | LiveKit server issue | Room creation or dispatch failure |
| `routing_validation_failed` | Number failed carrier rules check | `call_service.py:699` — routing_result.success=false |
| `worker_error` | Unexpected crash in worker | Generic exception in worker |

#### For Declined Calls (status = `declined`)

| `status_reason` | Trigger | Code Location |
|-----------------|---------|---------------|
| `callee_busy` | Recipient on another call | TwirpError, sip_status_code = 486/600 |
| `callee_rejected` | Recipient hit reject button | TwirpError, sip_status_code = 603 |
| `callee_unavailable` | Recipient DND/phone off | TwirpError, sip_status_code = 480 |
| `callee_no_answer` | Rang but nobody picked up | TwirpError, sip_status_code = 408 + context |

#### For Ended Calls (status = `ended`) — THIS IS THE KEY DISTINCTION

| `status_reason` | Trigger | Code Location | How We Know |
|-----------------|---------|---------------|-------------|
| `receiver_hangup` | **Receiver (callee) ended the call** | `cleanup_handler.py` | Room session ends + no internal hangup/silence flag was set + call was `ongoing`. LiveKit DisconnectReason = `CLIENT_INITIATED` from the SIP participant side |
| `agent_hangup` | Agent's LLM used the `hangup_call` tool | `worker.py:462-536` | `audit_trail` has `agent_hangup` event with reason (call_complete, not_interested, etc). Room deleted by our code via `delete_room()` |
| `silence_timeout` | User silent for 35s → auto-hangup | `worker.py:968-978` | `audit_trail` has `silence_hangup` event. Room deleted by our `on_silence_timeout()` |
| `cancelled_by_api` | Cancel API endpoint called | `api/routes/calls.py:417` | Status was set to `cancelled` before cleanup ran |
| `batch_cancelled` | Batch was stopped, this call was pending | `worker.py:744-762` | Status set to `cancelled` + batch check at entrypoint |
| `human_handoff_ended` | Human agent joined, conversation ended naturally | `worker.py:560-580` | `_human_joined=True` flag + room ended without our `delete_room()` |
| `network_disconnect` | Network issue killed the session | cleanup_handler.py | LiveKit DisconnectReason = `CONNECTION_TIMEOUT` or `STATE_MISMATCH` |
| `livekit_server_shutdown` | LiveKit server restarted mid-call | cleanup_handler.py | LiveKit DisconnectReason = `SERVER_SHUTDOWN` |

### How to Determine `receiver_hangup` vs Other Endings

This is the core challenge. Here's the decision tree:

```
Call ended (cleanup_handler runs)
├── Was audit_trail.agent_hangup logged? → "agent_hangup"
├── Was audit_trail.silence_hangup logged? → "silence_timeout"
├── Was status already "cancelled"?      → "cancelled_by_api" / "batch_cancelled"
├── Was _human_joined = True?            → "human_handoff_ended"
├── LiveKit DisconnectReason?
│   ├── CONNECTION_TIMEOUT               → "network_disconnect"
│   ├── SERVER_SHUTDOWN                  → "livekit_server_shutdown"
│   ├── ROOM_DELETED (by us)             → already caught above (agent/silence/cancel)
│   └── CLIENT_INITIATED (SIP side)      → "receiver_hangup" ← this is it!
└── No signal at all?                    → "receiver_hangup" (default for ongoing→ended)
```

**Key insight**: If the call was `ongoing`, no internal hangup flag was set, and the session just ended — it means the **receiver hung up**. This is the default/most common case.

---

## Proposed Metadata Schema

Add top-level `status_reason`, `sip_trail`, and `worker_info` fields to the `metadata` JSONB column:

```json
{
  "job_id": "108fbc5d...",
  "room_name": "call-108fbc5d...-232700d4",
  "added_context": "...",
  "voice_id": "aaa16c76-...",
  "status_reason": "receiver_hangup",
  "worker_info": {
    "worker_name": "voag-uae-workers",
    "worker_id": "WK_abc123",
    "livekit_url": "wss://voag-uae.livekit.cloud",
    "credential_source": "database",
    "livekit_config_name": "uae-production",
    "picked_up_at": "2026-02-25T05:20:00+00:00"
  },
  "sip_trail": {
    "trunk_id_used": "ST_xxxx",
    "trunk_resolution": "database",
    "phone_number_dialed": "+971501234567",
    "carrier_name": "vonage_uae",
    "events": [
      {
        "event": "room_created",
        "ts": "2026-02-25T05:20:01+00:00"
      },
      {
        "event": "sip_dial_started",
        "trunk_id": "ST_xxxx",
        "phone_number": "+971501234567",
        "ts": "2026-02-25T05:20:02+00:00"
      },
      {
        "event": "sip_answered",
        "call_sid": "call_sid_456",
        "sip_status_code": 200,
        "sip_status": "OK",
        "ts": "2026-02-25T05:20:08+00:00"
      },
      {
        "event": "sip_ended",
        "disconnect_reason": "CLIENT_INITIATED",
        "duration_seconds": 120.5,
        "ts": "2026-02-25T05:22:08+00:00"
      }
    ]
  },
  "audit_trail": { "..." }
}
```

### Failed Call Example (callee busy)

```json
{
  "status_reason": "callee_busy",
  "sip_trail": {
    "trunk_id_used": "ST_xxxx",
    "trunk_resolution": "database",
    "phone_number_dialed": "+971501234567",
    "carrier_name": "vonage_uae",
    "events": [
      {
        "event": "room_created",
        "ts": "2026-02-25T05:20:01+00:00"
      },
      {
        "event": "sip_dial_started",
        "trunk_id": "ST_xxxx",
        "phone_number": "+971501234567",
        "ts": "2026-02-25T05:20:02+00:00"
      },
      {
        "event": "sip_failed",
        "error_message": "SIP dial error: callee busy",
        "sip_status_code": 486,
        "sip_status": "Busy Here",
        "disconnect_reason": "USER_REJECTED",
        "ts": "2026-02-25T05:20:08+00:00"
      }
    ]
  }
}
```

### Configuration Error Example (bad trunk)

```json
{
  "status_reason": "sip_trunk_not_found",
  "sip_trail": {
    "trunk_id_used": "ST_invalid",
    "trunk_resolution": "environment",
    "phone_number_dialed": "+971501234567",
    "carrier_name": "vonage_uae",
    "events": [
      {
        "event": "room_created",
        "ts": "2026-02-25T05:20:01+00:00"
      },
      {
        "event": "sip_dial_started",
        "trunk_id": "ST_invalid",
        "phone_number": "+971501234567",
        "ts": "2026-02-25T05:20:02+00:00"
      },
      {
        "event": "sip_failed",
        "error_message": "trunk not found",
        "sip_status_code": null,
        "sip_status": null,
        "disconnect_reason": "SIP_TRUNK_FAILURE",
        "ts": "2026-02-25T05:20:02+00:00"
      }
    ]
  }
}
```

### Agent Hangup Example

```json
{
  "status_reason": "agent_hangup",
  "sip_trail": {
    "trunk_id_used": "ST_xxxx",
    "events": [
      { "event": "sip_answered", "ts": "..." },
      { "event": "sip_ended", "disconnect_reason": "ROOM_DELETED", "duration_seconds": 95.3, "ts": "..." }
    ]
  },
  "audit_trail": {
    "events": [
      { "type": "agent_hangup", "reason": "call_complete", "status": "completed", "ts": "..." }
    ]
  }
}
```

### Silence Timeout Example

```json
{
  "status_reason": "silence_timeout",
  "audit_trail": {
    "events": [
      { "type": "silence_warning", "elapsed_sec": 15, "ts": "..." },
      { "type": "silence_hangup", "timeout_seconds": 35, "ts": "..." }
    ]
  }
}
```

---

## SIP Status Code → Our Status Mapping

When we capture `sip_status_code`, map to our call statuses:

| SIP Code | Meaning | Our Status |
|----------|---------|------------|
| 200 | OK (answered) | `ongoing` → `ended` |
| 403 | Forbidden | `failed` (config issue) |
| 404 | Not Found | `failed` (number invalid) |
| 408 | Request Timeout | `failed` (carrier timeout) |
| 480 | Temporarily Unavailable | `declined` (DND/offline) |
| 486 | Busy Here | `declined` (busy) |
| 500 | Server Internal Error | `failed` (carrier error) |
| 503 | Service Unavailable | `failed` (carrier down) |
| 600 | Busy Everywhere | `declined` (busy all lines) |
| 603 | Decline | `declined` (rejected) |
| 604 | Does Not Exist Anywhere | `failed` (number invalid) |

---

## Implementation Steps

### Step 1: Create `SipTrailLogger` Utility

New file: `utils/sip_trail.py`

Similar pattern to `utils/audit_trail.py` — a class that collects events and produces a dict:

```python
class SipTrailLogger:
    def __init__(self):
        self.events = []
        self.trunk_id = None
        self.trunk_resolution = None
        self.phone_number = None
        self.carrier_name = None
    
    def log_room_created(self): ...
    def log_sip_dial_started(self, trunk_id, phone_number): ...
    def log_sip_answered(self, call_sid, sip_status_code): ...
    def log_sip_failed(self, error_message, sip_status_code, sip_status, disconnect_reason): ...
    def log_sip_ended(self, disconnect_reason, duration_seconds): ...
    
    def to_dict(self) -> dict: ...
```

### Step 2: Add `worker_info` to Metadata

In `worker.py` at entrypoint, after parsing dispatch metadata:

```python
worker_info = {
    "worker_name": os.getenv("VOICE_AGENT_NAME", "inbound-agent"),
    "worker_id": ctx.worker.id if hasattr(ctx, 'worker') else None,
    "livekit_url": os.getenv("LIVEKIT_URL", "unknown"),
    "picked_up_at": datetime.now(timezone.utc).isoformat(),
}

# Save to metadata immediately
await call_storage.update_call_metadata(call_log_id, metadata={"worker_info": worker_info})
```

### Step 3: Capture SIP Participant Response

In `worker.py:1348`, capture the return value:

```python
sip_result = await ctx.api.sip.create_sip_participant(
    api.CreateSIPParticipantRequest(...)
)

# Extract SIP info
if sip_result:
    sip_trail.log_sip_answered(
        call_sid=getattr(sip_result, 'call_sid', None),
        sip_status_code=200,
    )
```

### Step 4: Extract TwirpError SIP Details

In `worker.py:1394`, extract the full error info:

```python
except api.TwirpError as e:
    sip_status_code = None
    sip_status = None
    if hasattr(e, 'metadata') and e.metadata:
        sip_status_code = e.metadata.get('sip_status_code')
        sip_status = e.metadata.get('sip_status')
    
    sip_trail.log_sip_failed(
        error_message=e.message,
        sip_status_code=sip_status_code,
        sip_status=sip_status,
        disconnect_reason=None,  # Not available from TwirpError
    )
    
    logger.error(
        f"SIP dial error: {e.message}, "
        f"SIP status: {sip_status_code} {sip_status}"
    )
```

### Step 5: Save SIP Trail in Cleanup

In `cleanup_handler.py`, alongside `save_audit_trail`:

```python
# Merge sip_trail into metadata
existing_metadata["sip_trail"] = ctx.sip_trail.to_dict()
```

### Step 6: Pass Carrier/Trunk Info from Dispatch to Worker

The carrier_name and trunk resolution source need to be passed through dispatch metadata to the worker. Add to the SIP participant metadata in `call_service.py:755`:

```python
metadata["carrier_name"] = routing_result.carrier_name
metadata["trunk_resolution"] = livekit_creds.source  # "database" or "environment"
```

---

## Key Files to Touch

| File | Change |
|------|--------|
| `utils/sip_trail.py` | **NEW** — SipTrailLogger class |
| `agent/worker.py` | Capture SIP response, extract TwirpError details, add worker_info, init SipTrailLogger |
| `agent/cleanup_handler.py` | Save sip_trail to metadata alongside audit_trail |
| `api/services/call_service.py` | Add carrier_name and trunk_resolution to SIP metadata |

---

## Mapping: Where Each Data Point Comes From

| Data Point | Available At | Source |
|-----------|-------------|--------|
| `worker_name` | worker.py entrypoint | `os.getenv("VOICE_AGENT_NAME")` |
| `livekit_url` | worker.py entrypoint | `os.getenv("LIVEKIT_URL")` or from dispatch metadata |
| `credential_source` | call_service.py | `livekit_creds.source` ("database" or "environment") |
| `trunk_id_used` | worker.py before SIP dial | From dispatch metadata `outbound_trunk_id` |
| `carrier_name` | call_service.py | `routing_result.carrier_name` |
| `sip_status_code` | worker.py TwirpError | `e.metadata.get('sip_status_code')` |
| `sip_status` | worker.py TwirpError | `e.metadata.get('sip_status')` |
| `call_sid` | worker.py SIP response | `sip_result.sip_info.call_sid` |
| `disconnect_reason` | cleanup_handler.py | LiveKit participant disconnect event |

---

## Reference: LiveKit Disconnect Reasons

### SIP-Specific
- `USER_UNAVAILABLE` — Callee didn't answer (timeout/DND)
- `USER_REJECTED` — Callee rejected/busy
- `SIP_TRUNK_FAILURE` — Trunk config wrong or provider returned unexpected response

### General
- `CLIENT_INITIATED` — Normal hangup from our side
- `ROOM_DELETED` — Room deleted via cancel API
- `PARTICIPANT_REMOVED` — Participant explicitly removed
- `SERVER_SHUTDOWN` — LiveKit server restarting
- `JOIN_FAILURE` — Failed to connect to room
- `CONNECTION_TIMEOUT` — Connection timed out
- `STATE_MISMATCH` — Client/server state desync

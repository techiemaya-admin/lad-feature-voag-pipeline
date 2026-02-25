# SIP Call Lifecycle Analysis

> **Purpose**: Complete trace of how SIP calls flow through the system, what's tracked, and where visibility gaps exist.  
> **Date**: 2026-02-25

---

## End-to-End SIP Call Flow

```
API Request → call_routing.py → livekit_resolver.py → call_service.py → LiveKit Server → worker.py → cleanup_handler.py
```

### Step-by-Step Pipeline

| Step | File | What Happens | Status Set |
|------|------|-------------|------------|
| 1. Validate number | `utils/call_routing.py` | Parse phone, lookup carrier rules from `voice_agent_numbers.rules`, format for carrier | — |
| 2. Resolve LiveKit creds | `utils/livekit_resolver.py` | Check `USE_SELFHOST_ROUTING_TABLE` flag → try DB (`voice_agent_livekit` table) → fallback env vars | — |
| 3. Create call log | `call_service.py:734` | Insert into `voice_call_logs` with metadata (job_id, room_name, added_context, voice_id) | `in_queue` |
| 4. Create LiveKit room | `call_service.py:750` | `livekit_api.room.create_room()` | — |
| 5. Build SIP participant metadata | `call_service.py:755-798` | JSON with job_id, agent_id, voice config, trunk_id, phone_number, batch tracking fields | — |
| 6. Create agent dispatch | `call_service.py:807` | `livekit_api.agent_dispatch.create_dispatch()` with agent_name, room, metadata | — |
| 7. Worker picks up job | `worker.py` entrypoint | Parses dispatch metadata, acquires concurrency slot | — |
| 8. Update to ringing | `worker.py:1342` | `call_storage.update_call_status(status="ringing")` | `ringing` |
| 9. Create SIP participant | `worker.py:1348` | `ctx.api.sip.create_sip_participant()` with room, trunk_id, phone, identity, wait_until_answered=True | — |
| 10. Call answered | `worker.py:1360` | `call_storage.update_call_status(status="ongoing")` | `ongoing` |
| 11. Conversation | `worker.py` | AgentSession runs (STT → LLM → TTS), silence monitoring active | — |
| 12. Call ends | `cleanup_handler.py` | `determine_final_status()` maps existing → final status | `ended` / `declined` / `failed` |
| 13. Save audit trail | `cleanup_handler.py:192` | Merge `audit_trail.to_dict()` into call metadata | — |

---

## Trunk ID Resolution Chain

Priority order (first non-null wins):

```
1. livekit_creds.trunk_id   ← from voice_agent_livekit table (DB)
2. routing_result.outbound_trunk_id  ← from voice_agent_numbers.rules (carrier config)
3. os.getenv("OUTBOUND_TRUNK_ID")   ← environment variable fallback
```

Code location: `call_service.py:790`

```python
outbound_trunk = livekit_creds.trunk_id or routing_result.outbound_trunk_id or os.getenv("OUTBOUND_TRUNK_ID")
```

---

## LiveKit Credential Resolution

File: `utils/livekit_resolver.py`

| Source | Trigger | Fields |
|--------|---------|--------|
| **Database** | `USE_SELFHOST_ROUTING_TABLE=true` + from_number has `livekit_config_id` in routing result | url, api_key, api_secret (encrypted→decrypted), trunk_id, worker_name |
| **Environment** | Flag is false, or DB lookup fails | LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, OUTBOUND_TRUNK_ID, VOICE_AGENT_NAME |

DB table: `voice_agent_livekit` — Columns: id, name, description, livekit_url, livekit_api_key, livekit_api_secret, trunk_id, worker_name, created_at, updated_at

---

## Current Error Handling

### Worker SIP Dial (worker.py:1394-1404)

```python
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
```

**Problem**: Only `e.message` is logged. The `TwirpError` object contains critical SIP diagnostic info in `e.metadata`:
- `e.metadata.get('sip_status_code')` — SIP response code (e.g., 403, 486, 503)
- `e.metadata.get('sip_status')` — SIP status text (e.g., "Forbidden", "Busy Here")

These are **not captured** in the call log or metadata.

### Call Service Dispatch (call_service.py:748-815)

No error handling around `create_room()` or `create_dispatch()` — only the outer try/except in `_dispatch_and_update()` in `api/routes/calls.py` catches generic exceptions.

### Cleanup Handler Status Mapping

```
existing_status → final_status
------------------------------
declined, rejected, no_answer, busy → "declined"
failed, error, not_reachable       → "failed"
cancelled, canceled                → "cancelled"
ringing                            → "ended"
ongoing, in_progress, running      → "ended"
in_queue, pending, queued          → "ended"
```

---

## SIP Status Codes (RFC 3261)

These are the standard SIP response codes you'll see in `TwirpError.metadata['sip_status_code']`:

### 4xx — Client Failures

| Code | Name | What It Means |
|------|------|---------------|
| 400 | Bad Request | Malformed SIP message |
| 401 | Unauthorized | Authentication required |
| 403 | Forbidden | Call rejected by provider (common: wrong trunk config) |
| 404 | Not Found | Number doesn't exist at carrier |
| 407 | Proxy Auth Required | Proxy-level auth issue |
| 408 | Request Timeout | No response from carrier in time |
| 480 | Temporarily Unavailable | Callee offline or DND |
| 481 | Call Does Not Exist | No matching dialog/transaction |
| 486 | Busy Here | Callee is on another call |

### 5xx — Server Failures

| Code | Name | What It Means |
|------|------|---------------|
| 500 | Server Internal Error | Carrier-side crash |
| 502 | Bad Gateway | Invalid upstream carrier response |
| 503 | Service Unavailable | Carrier overloaded or down |
| 504 | Server Timeout | Carrier didn't respond in time |

### 6xx — Global Failures

| Code | Name | What It Means |
|------|------|---------------|
| 600 | Busy Everywhere | Callee busy on all devices |
| 603 | Decline | Callee explicitly rejected |
| 604 | Does Not Exist Anywhere | Number doesn't exist globally |

---

## LiveKit Disconnect Reasons

When a SIP participant disconnects, LiveKit provides a `DisconnectReason`:

### SIP-Specific

| Reason | Meaning |
|--------|---------|
| `USER_UNAVAILABLE` | Callee didn't answer in time |
| `USER_REJECTED` | Callee rejected/busy |
| `SIP_TRUNK_FAILURE` | Trunk config wrong or provider error |

### General (also applies to SIP calls)

| Reason | Meaning |
|--------|---------|
| `CLIENT_INITIATED` | Our code disconnected (normal hangup) |
| `ROOM_DELETED` | Room was deleted (cancel endpoint) |
| `PARTICIPANT_REMOVED` | Explicitly removed from room |
| `SERVER_SHUTDOWN` | LiveKit server going down |
| `JOIN_FAILURE` | Failed to connect to room |
| `CONNECTION_TIMEOUT` | Connection timed out |
| `STATE_MISMATCH` | Client/server state went out of sync |

---

## LiveKit SIP Response Data (Currently Not Captured)

### CreateSIPParticipant Response

The `create_sip_participant()` call returns a `SIPParticipantInfo` object with:

```json
{
  "id": "participant_id_123",
  "identity": "dial-+971501234567",
  "state": "connected",
  "sip_info": {
    "trunk_id": "ST_xxxx",
    "call_sid": "call_sid_456",
    "call_status": "active"
  }
}
```

**None of this data is captured**. The current code at `worker.py:1348` doesn't store the return value.

---

## Worker Identity

The worker identifies itself via `VOICE_AGENT_NAME` env var (default: `inbound-agent`):

```python
# worker.py:1451
agent_name=os.getenv("VOICE_AGENT_NAME", "inbound-agent")
```

This name is used for dispatch matching but is **not stored in call metadata**. There's no way to know which worker instance handled a specific call after the fact.

---

## What's NOT Tracked (the gaps)

| Gap | Impact | Where to Fix |
|-----|--------|-------------|
| SIP status code (403/486/503 etc) | Can't tell WHY a call failed | worker.py TwirpError handler |
| SIP status text | No human-readable error | worker.py TwirpError handler |
| SIP call_sid from carrier | Can't cross-reference with carrier logs | worker.py after create_sip_participant |
| Worker name/identity | Can't tell which worker handled the call | worker.py at entrypoint |
| Trunk ID actually used | Can't verify correct trunk was selected | worker.py before SIP dial |
| LiveKit server URL used | Can't tell which LK server was used | worker.py at entrypoint |
| Room creation timestamp | Can't measure dispatch-to-room latency | call_service.py |
| SIP participant creation result | Lost diagnostic data | worker.py |
| DisconnectReason | Can't distinguish hangup vs failure vs timeout | cleanup_handler.py |
| Credential source (DB vs env) | Can't debug trunk resolution issues | worker.py at entrypoint |
| Call duration per SIP leg | Only overall duration tracked | cleanup_handler.py |

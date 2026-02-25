# SIP in VOAG

> How our SIP subsystem works — from API request to phone ringing to call teardown.

---

## 1. Data Flow — Outbound Call

```
API Request (POST /calls)
    ↓
calls.py → dispatch_call()
    ↓
call_routing.py → validate_and_format_call()
    ↓  Returns: formatted number, carrier_name, outbound_trunk_id, livekit_config_id
    ↓
livekit_resolver.py → resolve_livekit_credentials()
    ↓  Returns: url, api_key, api_secret, trunk_id, worker_name, source
    ↓
call_service.py → create call log (DB) + LiveKit room + SIP participant metadata + agent dispatch
    ↓  Dispatch metadata JSON sent as job context
    ↓
LiveKit Cloud routes dispatch to matching worker
    ↓
worker.py → entrypoint() parses metadata → create_sip_participant() → phone rings
```

### What Data Reaches the Worker

The API creates a JSON metadata blob attached to the LiveKit agent dispatch. The worker parses this from `ctx.job.metadata`:

| Field | Source | Used For |
|-------|--------|----------|
| `job_id` | Generated UUID | Tracking |
| `call_log_id` | DB insert return | Status updates |
| `phone_number` | `call_routing.py` formatted | SIP dialing |
| `to_number` | Original user input | Display/logging |
| `from_number` | API request | Carrier rules lookup |
| `call_mode` | Always `"outbound"` for dispatch | Worker branching |
| `outbound_trunk_id` | `livekit_creds.trunk_id` / `routing_result` / env | SIP trunk selection |
| `agent_id` | API request | Agent config lookup |
| `voice_id` | Resolved from agent config | TTS voice |
| `carrier_name` | `routing_result.carrier_name` | SIP trail logging |
| `trunk_resolution` | `livekit_creds.source` (`"database"` / `"environment"`) | SIP trail logging |
| `batch_id`, `entry_id` | Batch system | Batch tracking |
| `initiated_by` | API request user UUID | OAuth tools |
| `added_context` | API request | Agent greeting |

### How the Worker Uses This Data

1. **Parses metadata** from `ctx.job.metadata` (JSON string → dict)
2. **Resolves trunk_id**: `outbound_trunk_id` from metadata, fallback `OUTBOUND_TRUNK_ID` env var
3. **Updates call status** to `ringing` in DB
4. **Calls `ctx.api.sip.create_sip_participant()`** with:
   - `room_name` = the LiveKit room
   - `sip_trunk_id` = resolved trunk ID
   - `sip_call_to` = formatted phone number
   - `participant_identity` = `"dial-{phone_number}"`
   - `wait_until_answered` = `True` (blocks until pickup or failure)
   - `krisp_enabled` = `True`
5. On success → status updated to `ongoing`, silence monitor started, greeting sent
6. On `TwirpError` → status set to `failed`, SIP error details extracted and logged

---

## 2. Number Routing & Trunk Resolution

### Phone Number Flow

```
Raw input: "0501234567"
    ↓ call_routing.py
Parse: country_code=+971, base=501234567, country=uae
    ↓
DB lookup: voice_agent_numbers WHERE phone = from_number
    ↓ Returns carrier rules JSON
Format for carrier: "+971501234567" (E.164) or "0501234567" (local) per rules
    ↓
Validate: is outbound allowed? is this country allowed?
    ↓
Return: CallRoutingResult(formatted_to_number, carrier_name, outbound_trunk_id, livekit_config_id)
```

### Trunk ID Resolution Chain

Three sources, first non-null wins:

```
1. LiveKit config from DB     ← voice_agent_livekit.trunk_id (via livekit_config_id)
2. Carrier rules from DB      ← voice_agent_numbers.rules.outbound_trunk_id
3. Environment variable       ← OUTBOUND_TRUNK_ID
```

### LiveKit Credential Resolution

Controlled by `USE_SELFHOST_ROUTING_TABLE` env var (default: `true`):

| Flag | Behavior |
|------|----------|
| `true` | Try DB lookup (`voice_agent_livekit` table via `routing_result.livekit_config_id`) → fallback to env |
| `false` | Use `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` env vars directly |

DB table `voice_agent_livekit` stores: `livekit_url`, `livekit_api_key`, `livekit_api_secret` (encrypted), `trunk_id`, `worker_name`.

---

## 3. A Perfect SIP Call — Step by Step

Everything that happens at the SIP/telephony layer for a successful outbound call:

| # | What Happens | LiveKit API | Status |
|---|-------------|-------------|--------|
| 1 | API creates LiveKit room | `room.create_room(name=room_name)` | — |
| 2 | API creates agent dispatch | `agent_dispatch.create_dispatch(agent_name, room, metadata)` | `in_queue` |
| 3 | LiveKit routes dispatch to worker matching `agent_name` | — | — |
| 4 | Worker acquires concurrency slot (semaphore) | — | — |
| 5 | Worker connects to room | `ctx.connect()` | — |
| 6 | Worker updates call status | DB write | `ringing` |
| 7 | Worker calls `create_sip_participant()` | `sip.create_sip_participant(trunk, number, room)` | — |
| 8 | LiveKit sends SIP INVITE to trunk provider | — | — |
| 9 | Provider authenticates and routes to carrier | — | — |
| 10 | Carrier delivers call to recipient's phone | — | — |
| 11 | Phone rings... recipient picks up | SIP 200 OK | — |
| 12 | `create_sip_participant()` returns (was blocking with `wait_until_answered=True`) | Returns `SIPParticipantInfo` | — |
| 13 | Worker updates call status | DB write | `ongoing` |
| 14 | SIP audio bridge established: recipient ↔ LiveKit room ↔ agent | — | — |
| 15 | Silence monitor starts | — | — |
| 16 | Conversation happens (STT → LLM → TTS loop) | — | — |
| 17 | Call ends (recipient hangs up / agent hangs up / silence timeout) | — | — |
| 18 | SIP BYE sent to carrier, participant removed from room | — | — |
| 19 | Cleanup handler runs: saves trails, calculates cost, updates status | DB write | `ended` |
| 20 | Concurrency slot released | — | — |

### SIP Protocol Exchange (Simplified)

```
LiveKit Server                    SIP Trunk Provider                Carrier/Phone
      │                                 │                               │
      ├── INVITE (trunk_id creds) ─────>│                               │
      │                                 ├── INVITE ────────────────────>│
      │                                 │                               │ Phone rings
      │                                 │<──────────── 180 Ringing ─────┤
      │<── 180 Ringing ─────────────────┤                               │
      │                                 │                               │ User picks up
      │                                 │<──────────── 200 OK ──────────┤
      │<── 200 OK ──────────────────────┤                               │
      ├── ACK ─────────────────────────>│── ACK ──────────────────────>│
      │                                 │                               │
      │<═══════════ RTP Audio Stream (bidirectional) ═════════════════>│
      │                                 │                               │
      │                                 │                               │ User hangs up
      │                                 │<──────────── BYE ─────────────┤
      │<── BYE ─────────────────────────┤                               │
      ├── 200 OK ──────────────────────>│── 200 OK ───────────────────>│
      │                                 │                               │
```

---

## 4. SIP Trail

Every call now captures a `sip_trail` object in the `metadata` JSONB column:

### Schema

```json
{
  "status_reason": "receiver_hangup",
  "worker_info": {
    "worker_name": "voag-uae-workers",
    "livekit_url": "wss://voag-uae.livekit.cloud",
    "picked_up_at": "2026-02-25T05:20:00+00:00"
  },
  "sip_trail": {
    "trunk_id_used": "ST_xxxx",
    "trunk_resolution": "database",
    "phone_number_dialed": "+971501234567",
    "carrier_name": "vonage_uae",
    "events": [
      { "event": "sip_dial_started", "trunk_id": "ST_xxxx", "phone_number": "+971501234567", "ts": "..." },
      { "event": "sip_answered", "call_sid": "call_sid_456", "sip_status_code": 200, "ts": "..." }
    ]
  }
}
```

### `status_reason` Values

| Category | Value | Meaning |
|----------|-------|---------|
| **Ended** | `receiver_hangup` | Callee hung up (default when no internal flag set) |
| | `agent_hangup` | Agent LLM used hangup_call tool |
| | `silence_timeout` | No speech for 35s, auto-hangup |
| | `cancelled_by_api` | Cancelled via /calls/cancel endpoint |
| **Failed** | `sip_trunk_not_found` | Trunk ID doesn't exist in LiveKit |
| | `sip_carrier_rejected` | SIP 403 — carrier rejected the call |
| | `sip_number_not_found` | SIP 404/604 — number doesn't exist |
| | `sip_carrier_unavailable` | SIP 503 — carrier down |
| | `sip_carrier_timeout` | SIP 408/504 — no response |
| **Declined** | `callee_busy` | SIP 486/600 — recipient busy |
| | `callee_rejected` | SIP 603 — recipient declined |
| | `callee_unavailable` | SIP 480 — DND / phone off |

### SIP Status Codes from TwirpError

When `create_sip_participant()` fails, LiveKit throws `TwirpError` with `e.metadata`:
- `sip_status_code` — standard SIP response code (403, 486, 503, etc.)
- `sip_status` — human-readable text ("Forbidden", "Busy Here", etc.)

---

## 5. SIP Failure Modes

| Failure | SIP Code | What Went Wrong |
|---------|----------|-----------------|
| Wrong trunk ID | — | Trunk doesn't exist in LiveKit config |
| Trunk auth failure | 401/407 | Trunk credentials rejected by provider |
| Carrier rejected | 403 | Provider refused the call (config/permissions) |
| Invalid number | 404/604 | Number doesn't exist at carrier level |
| Carrier timeout | 408/504 | Provider or carrier didn't respond in time |
| Recipient busy | 486/600 | Recipient on another call |
| Recipient declined | 603 | Recipient pressed reject |
| Recipient unavailable | 480 | DND, phone off, out of range |
| Carrier overloaded | 503 | Provider temporarily unavailable |

---

## 6. Code References

### API Layer (request entry points)

| File | Responsibility |
|------|---------------|
| [calls.py](file:///d:/vonage/vonage-voice-agent/v2/api/routes/calls.py) | `POST /calls` — single outbound call endpoint, calls `dispatch_call()` |
| [batch.py](file:///d:/vonage/vonage-voice-agent/v2/api/routes/batch.py) | `POST /batch` — batch call endpoint, iterates entries and calls `dispatch_call()` per entry |

### Call Service (orchestration)

| File | Responsibility |
|------|---------------|
| [call_service.py](file:///d:/vonage/vonage-voice-agent/v2/api/services/call_service.py) | `dispatch_call()` — resolves routing → creds → creates room → builds SIP metadata → dispatches agent. Also `terminate_call()` for force-ending calls |

**Key functions in call_service.py:**
- `dispatch_call()` (L541) — main entry point
- `create_call_log()` (L734) — inserts DB row with status `in_queue`
- SIP metadata assembly (L755-800) — builds JSON blob sent to worker
- Room + dispatch creation (L748-818) — LiveKit API calls

### Routing & Credentials

| File | Responsibility |
|------|---------------|
| [call_routing.py](file:///d:/vonage/vonage-voice-agent/v2/utils/call_routing.py) | Phone number parsing, E.164 normalization, carrier rules lookup from `voice_agent_numbers`, number formatting per carrier rules |
| [livekit_resolver.py](file:///d:/vonage/vonage-voice-agent/v2/utils/livekit_resolver.py) | LiveKit credential resolution: DB (`voice_agent_livekit` table) vs environment, controlled by `USE_SELFHOST_ROUTING_TABLE` flag |

**Key types:**
- `CallRoutingResult` — formatted_to_number, carrier_name, outbound_trunk_id, livekit_config_id
- `LiveKitCredentials` — url, api_key, api_secret, trunk_id, worker_name, source

### Worker (SIP execution)

| File | Responsibility |
|------|---------------|
| [worker.py](file:///d:/vonage/vonage-voice-agent/v2/agent/worker.py) | `entrypoint()` — parses dispatch metadata, resolves trunk_id, calls `create_sip_participant()`, handles `TwirpError`, manages call status transitions (ringing → ongoing → ended/failed) |

**Key locations in worker.py:**
- Metadata parsing: L654-724
- Trunk resolution: `outbound_trunk_id or os.getenv("OUTBOUND_TRUNK_ID")` (L1337)
- SIP participant creation: L1348-1370
- TwirpError handling + SIP code extraction: L1394-1425
- Worker name for dispatch matching: L1451 (`VOICE_AGENT_NAME` env var)

### SIP Trail & Diagnostics

| File | Responsibility |
|------|---------------|
| [sip_trail.py](file:///d:/vonage/vonage-voice-agent/v2/utils/sip_trail.py) | `SipTrailLogger` — collects SIP lifecycle events (dial, answer, fail, end). `resolve_status_reason()` — determines why a call ended/failed based on audit trail + SIP trail + status flags |

### Cleanup (post-call)

| File | Responsibility |
|------|---------------|
| [cleanup_handler.py](file:///d:/vonage/vonage-voice-agent/v2/agent/cleanup_handler.py) | `save_audit_trail()` — merges audit_trail + sip_trail + status_reason into metadata. `determine_final_status()` — maps DB status to normalized final (ringing → ended, failed → failed, etc.) |

### Database

| Table | SIP-Relevant Fields |
|-------|-------------------|
| `voice_call_logs` | status, metadata (JSONB with sip_trail, worker_info, status_reason) |
| `voice_agent_numbers` | rules (JSONB with outbound_trunk_id, carrier format rules) |
| `voice_agent_livekit` | livekit_url, livekit_api_key, livekit_api_secret, trunk_id, worker_name |

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `LIVEKIT_URL` | LiveKit server WebSocket URL | Required |
| `LIVEKIT_API_KEY` | LiveKit API key | Required |
| `LIVEKIT_API_SECRET` | LiveKit API secret | Required |
| `OUTBOUND_TRUNK_ID` | Fallback SIP trunk ID | — |
| `VOICE_AGENT_NAME` | Worker identity for dispatch matching | `inbound-agent` |
| `USE_SELFHOST_ROUTING_TABLE` | Use DB for LiveKit cred resolution | `true` |

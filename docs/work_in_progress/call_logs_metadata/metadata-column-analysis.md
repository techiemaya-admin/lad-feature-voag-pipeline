# Call Logs `metadata` Column Analysis

## Column Overview

The `metadata` column is a **JSONB** column in `voice_call_logs` table. It serves as a flexible JSON store for call-specific data that doesn't have its own dedicated column.

### Current JSON Structure

```json
{
  "job_id": "108fbc5d...",
  "voice_id": "aaa16c76-...",
  "room_name": "call-108fbc5d...-232700d4",
  "added_context": "Testing Single Call\nThe lead's name is \"Sahil\".",
  "audit_trail": {
    "tenant_id": "da9e2715-...",
    "tools_provided": [],
    "events": []
  }
}
```

---

## Where Metadata is Written

### 1. Initial Creation — `db/storage/calls.py` (L204-L220)

`_build_metadata()` constructs the initial metadata dict from:
- `job_id` — internal job tracking ID
- `room_name` — LiveKit room name
- `added_context` — extra context from the call request
- `voice_id` — stored here because `voice_call_logs` has no `voice_id` column

Called by `create_call_log()` (L226-L324).

### 2. Audit Trail Merge — `agent/cleanup_handler.py` (L192-L241)

`save_audit_trail()` runs during post-call cleanup (step 3.5 in cleanup flow). It:
1. Reads existing metadata via `get_call_by_id()`
2. Merges `audit_trail.to_dict()` into `existing_metadata["audit_trail"]`
3. Writes back via `update_call_metadata()`

This is the **only place** that adds `audit_trail` to metadata.

---

## Where Metadata is Read

### 3. Query by `job_id` — `db/storage/calls.py` (L574-L617)

`get_call_by_job_id()` uses `metadata->>'job_id' = %s` to look up a call by its job ID. Used by:
- `scripts/check_batches.py` — batch inspection script
- `scripts/cancel_batch.py` — batch cancellation script

### 4. Query by `room_name` — `db/storage/calls.py` (L619-L655)

`get_call_by_room_name()` uses `metadata->>'room_name' = %s` to look up a call. Used by the cancel endpoint.

### 5. Cancel Endpoint — `api/routes/calls.py` (L393-L419)

When cancelling a call, the API reads:
- `metadata.room_name` → to terminate the LiveKit room
- `metadata.batch_id` → to update batch status if it's a batch call

---

## The Audit Trail System

### 6. ToolAuditTrail Class — `utils/audit_trail.py`

Records events during a call. Current capabilities:

| Method | Event Type | When Logged |
|--------|-----------|-------------|
| `set_tools_provided()` | — | At call start, lists tool names given to LLM |
| `log_tool_call()` | `tool_call` | When a tool is invoked (input, output, status) |
| `log_tool_result()` | `tool_result` | Async results (KB search, human joined) |
| `log_silence_warning()` | `silence_warning` | User silent for N seconds |
| `log_silence_hangup()` | `silence_hangup` | Auto-hangup after silence timeout |
| `log_agent_hangup()` | `agent_hangup` | Agent ends call (reason + status) |
| `log_human_handoff_started()` | `human_handoff_started` | Human support dial initiated |
| `log_human_handoff_joined()` | `human_handoff_joined` | Human agent answered |
| `log_human_handoff_failed()` | `human_handoff_failed` | Human dial failed |

### 7. Where audit_trail is actually called in code

| File | What's Logged |
|------|--------------|
| `agent/worker.py:894` | `set_tools_provided()` — tool names from `tool_list` |
| `agent/worker.py:952` | `log_silence_warning()` — silence elapsed time |
| `agent/worker.py:973` | `log_silence_hangup()` — silence timeout |
| `agent/worker.py:506-533` | `log_agent_hangup()` — reason + status |
| `agent/tool_builder.py:1211` | `log_tool_call()` — only for `invite_human_agent` |
| `agent/tool_builder.py:1217` | `log_human_handoff_started()` — dial number |
| `agent/tool_builder.py:1149` | `log_human_handoff_joined()` |
| `agent/tool_builder.py:1180` | `log_human_handoff_failed()` — error |

---

## Key Gaps Identified

### `tools_provided` is always `[]`

Despite `set_tools_provided()` being called at `worker.py:894`, the sample showed `"tools_provided": []`. This means either:
- The tool list was empty for that call (no tools enabled for the tenant), **or**
- The tool names aren't being resolved correctly from the `tool_list`

### `log_tool_call()` only tracks human handoff

The `log_tool_call()` method exists and supports `input_data`, `output`, `status` — but it's **only called for `invite_human_agent`**. None of the other tools (Google Calendar, email templates, Microsoft Bookings, Knowledge Base, Outlook, hangup) log their calls to the audit trail.

### No tool call tracking for non-human-handoff tools

Tools built in `tool_builder.py` (calendar, bookings, email, KB) do **not** have `audit_trail` passed to them. Only `build_human_support_tools()` receives the `audit_trail` parameter.

### audit_trail is never exposed via API

The `audit_trail` data is saved into `metadata` but:
- No API endpoint reads or returns `metadata.audit_trail`
- The `CallStatusResponse` model doesn't include metadata or audit trail
- The frontend has no way to display tool usage history

---

## What Needs to Happen (for future work)

| Need | Current State | What's Missing |
|------|--------------|----------------|
| Track tools attached to agent | `set_tools_provided()` called | Verify it's working for all tenants |
| Track tool calls (input/output/errors) | Only `invite_human_agent` logged | All other tool wrappers need `audit_trail.log_tool_call()` |
| Show tool calls in UI | Data saved to DB | API endpoint to return audit_trail, frontend to render it |
| Track errors per tool | `log_tool_call(status="error")` exists | No tool currently logs errors this way |
| Aggregate tool usage across calls | Raw data in metadata JSONB | No analytics/reporting on tool usage |

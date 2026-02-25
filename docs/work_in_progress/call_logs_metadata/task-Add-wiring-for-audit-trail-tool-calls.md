# Task: Wire Audit Trail for All Tool Calls

> **Status**: NOT STARTED — Context captured for future pickup  
> **Related**: `metadata-column-analysis.md` in this folder  
> **Date**: 2026-02-25

---

## Problem

The `metadata.audit_trail` system in `voice_call_logs` is designed to track all tool usage during a call, but it's only partially wired. Currently **only `invite_human_agent` (human handoff)** logs tool calls. Every other tool (calendar, bookings, email, KB, outlook) runs silently with no audit record.

## What Should Be in `metadata.audit_trail`

The final shape of a fully-wired audit trail should look like:

```json
{
  "audit_trail": {
    "tenant_id": "da9e2715-...",
    "tools_provided": ["hangup_call", "check_calendar", "create_calendar_event", "send_email", "search_knowledge_base", "invite_human_agent"],
    "events": [
      {
        "type": "tool_call",
        "tool": "check_calendar",
        "input": {"date": "2026-03-01"},
        "output": "Available slots on 2026-03-01: 10:00, 11:00, 14:00",
        "status": "complete",
        "ts": "2026-02-25T05:21:33+00:00"
      },
      {
        "type": "tool_call",
        "tool": "create_calendar_event",
        "input": {"title": "Demo Call", "date": "2026-03-01", "start_time": "10:00"},
        "output": "Event created: Demo Call on 2026-03-01 at 10:00",
        "status": "complete",
        "ts": "2026-02-25T05:22:10+00:00"
      },
      {
        "type": "tool_call",
        "tool": "send_email",
        "input": {"to_email": "lead@example.com", "subject": "Meeting Confirmation"},
        "output": "Email sent successfully",
        "status": "complete",
        "ts": "2026-02-25T05:22:45+00:00"
      },
      {
        "type": "tool_call",
        "tool": "search_knowledge_base",
        "input": {"query": "refund policy"},
        "output": "Searching knowledge base for: refund policy",
        "status": "complete",
        "ts": "2026-02-25T05:23:00+00:00"
      },
      {
        "type": "tool_call",
        "tool": "send_outlook_email",
        "input": {"to_email": "client@corp.com", "subject": "Follow Up"},
        "output": "",
        "status": "error",
        "ts": "2026-02-25T05:24:00+00:00"
      },
      {
        "type": "silence_warning",
        "elapsed_sec": 15.0,
        "action": "prompted_user",
        "ts": "2026-02-25T05:25:30+00:00"
      },
      {
        "type": "agent_hangup",
        "reason": "call_complete",
        "status": "completed",
        "ts": "2026-02-25T05:26:00+00:00"
      }
    ]
  }
}
```

## What's Currently Missing (the wiring gaps)

### 1. `audit_trail` not passed to most tool builders

In `agent/tool_builder.py` → `attach_tools()` (L1313-L1480):

- `build_human_support_tools()` — receives `audit_trail` param
- `build_google_workspace_tools()` — does NOT receive `audit_trail`
- `build_microsoft_bookings_tools()` — does NOT receive `audit_trail`
- `build_microsoft_outlook_tools()` — does NOT receive `audit_trail`
- `build_knowledge_base_tools()` — does NOT receive `audit_trail`
- `build_email_template_tools()` — does NOT receive `audit_trail`
- `build_microsoft_email_template_tools()` — does NOT receive `audit_trail`

### 2. No `log_tool_call()` inside tool wrapper functions

Each tool wrapper function (e.g., `check_calendar`, `create_calendar_event`, `send_email`, `auto_book_appointment`, `send_outlook_email`, `search_knowledge_base`) needs:

```python
# At the start of each tool function:
if audit_trail:
    audit_trail.log_tool_call(
        tool_name="check_calendar",
        input_data={"date": date},
        output=result_string,
        status="complete"  # or "error" in except block
    )
```

### 3. No API endpoint exposes audit_trail

- `CallStatusResponse` in `api/models.py` doesn't include metadata or audit_trail
- No dedicated endpoint like `GET /calls/{id}/audit-trail`
- Frontend cannot display tool call history

### 4. `tools_provided` may be empty

`worker.py:894` calls `set_tools_provided()` with `[getattr(t, '__name__', str(t)) for t in tool_list]`. If `tool_list` is empty (tenant has no tools enabled), this works correctly. But verify that `__name__` resolves properly for `@function_tool` decorated functions — it should, but needs a live test.

---

## Implementation Steps (when picking this up)

### Step 1: Pass `audit_trail` to all tool builders

In `attach_tools()` in `agent/tool_builder.py`, add `audit_trail=audit_trail` to every `build_*_tools()` call, same as it's done for `build_human_support_tools()`.

### Step 2: Add `audit_trail` parameter to each builder function signature

Each `build_*_tools()` function needs `audit_trail: Any = None` as a parameter.

### Step 3: Add `log_tool_call()` inside each tool wrapper

Wrap the try/except in each tool function:
- On success: `audit_trail.log_tool_call(tool_name, input_data, output, status="complete")`
- On error: `audit_trail.log_tool_call(tool_name, input_data, str(error), status="error")`
- Use `safe_log_event()` helper from `utils/audit_trail.py` to avoid breaking the call flow

### Step 4: Expose via API (optional, separate task)

- Add `audit_trail` field to `CallStatusResponse` model
- Or create `GET /calls/{id}/tool-calls` endpoint
- Parse `metadata->>'audit_trail'` from the DB record

### Step 5: Frontend display (optional, separate task)

- Show tool call timeline in call details view
- Show which tools were available vs actually used
- Highlight errors

---

## Key Files to Touch

| File | Change |
|------|--------|
| `agent/tool_builder.py` | Pass `audit_trail` to all builders, add `log_tool_call()` |
| `utils/audit_trail.py` | No changes needed (already has all methods) |
| `agent/worker.py` | No changes needed (already passes `audit_trail` to `attach_tools`) |
| `agent/cleanup_handler.py` | No changes needed (already saves audit trail) |
| `api/models.py` | Add audit_trail to response model (Step 4) |
| `api/routes/calls.py` | Return audit_trail in status response (Step 4) |

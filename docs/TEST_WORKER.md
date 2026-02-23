# Batch Test Worker — Guide

> **Source file**: `agent/test_worker.py`  
> **Endpoint**: `POST /batch/trigger-test-batch`

---

## What Is It?

A minimal LiveKit agent that simulates call outcomes without making real SIP, LLM, TTS, or STT connections. It's used to test the batch pipeline (wave dispatch, timeout handling, status tracking, report generation) end-to-end with deterministic, controllable outcomes.

The test endpoint uses the **exact same** batch pipeline as production — the only difference is:
- Worker name is forced to `batch-test-worker`
- Phone numbers are fake (`+1555000XXXX`)
- Outcome probabilities are encoded in `added_context`

---

## Quick Start

### 1. Start the Test Worker

```bash
# In a separate terminal (from v2/ root)
uv run python -m agent.test_worker dev
```

The worker registers with LiveKit as `batch-test-worker` and waits for jobs.

### 2. Trigger a Test Batch

```bash
curl -X POST http://localhost:8000/batch/trigger-test-batch \
  -H "Content-Type: application/json" \
  -d '{
    "voice_id": "YOUR_VOICE_ID",
    "initiated_by": YOUR_USER_ID,
    "total": 20,
    "fail_count": 10,
    "stuck_count": 3,
    "dropped_count": 2
  }'
```

### 3. Monitor Progress

```bash
# Check batch status
curl http://localhost:8000/batch/batch-status/BATCH_ID
```

Or watch the worker terminal — it logs every job with its outcome.

---

## API Reference

### `POST /batch/trigger-test-batch`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `voice_id` | string | **required** | Must exist in DB (for `resolve_voice()`) |
| `initiated_by` | int | **required** | User ID (for `resolve_tenant_id`) |
| `agent_id` | int \| null | `null` | Agent ID (optional) |
| `from_number` | string \| null | `null` | From number (no SIP for tests, but needed for dispatch_call) |
| `total` | int | `149` | Total number of test calls |
| `fail_count` | int | `100` | Number of calls that should fail |
| `stuck_count` | int | `15` | Number of calls that get stuck (no callback sent) |
| `dropped_count` | int | `10` | Number of calls that are accepted but dropped immediately |

**Success count** is calculated automatically: `total - fail_count - stuck_count - dropped_count`

**Response**: Same as `POST /trigger-batch-call` — returns `batch_id`, `job_id`, `status: "accepted"`.

---

## Outcome Behaviors

| Outcome | Probability Weight | Simulated Behavior |
|---|---|---|
| **Completed** | `success_count` | Sleep 2-20s → `call_logs.status = 'ended'` → POST `/batch/entry-completed` |
| **Failed** | `fail_count` | Sleep 1-5s → `call_logs.status = 'failed'` → POST `/batch/entry-completed` |
| **Stuck** | `stuck_count` | Accept job, connect to room, sleep 1s, disconnect. **No status update, no callback.** Wave timeout catches it. |
| **Dropped** | `dropped_count` | Same as stuck — accept job, connect, disconnect immediately. Simulates lost requests. |

### How Outcomes Are Selected

The endpoint encodes probabilities in `added_context` as:

```
[TEST_BATCH_PROBABILITIES]{"completed":24,"failed":100,"stuck":15,"dropped":10}
```

The test worker parses this marker and uses `random.choices(weights=...)` to pick each call's outcome. Results are **weighted-random**, not sequential — so the exact counts won't match the input precisely, but the distribution will be correct over large batches.

---

## What Gets Tested

| Batch Pipeline Component | Covered? | Notes |
|---|---|---|
| Batch creation | ✅ | Same pipeline, same DB records |
| Wave dispatch | ✅ | Same `dispatch_call()` path |
| Wave polling | ✅ | `count_pending_entries` polls every 5s |
| Worker callback | ✅ | Test worker POSTs to `/batch/entry-completed` |
| Counter incrementing | ✅ | `completed_calls` / `failed_calls` counters |
| Timeout handling | ✅ | Stuck/dropped calls trigger `handle_wave_timeout` |
| Retry dispatch | ✅ | Dispatched-but-picked-up entries get re-queued |
| Status sync | ✅ | `sync_entry_statuses_from_call_logs` runs after each wave |
| Batch completion | ✅ | `check_and_complete_batch` triggers report |
| Report generation | ✅ | Same report pipeline (may have empty analysis data) |
| Cancellation | ✅ | Can cancel mid-batch via `/batch-cancel/BATCH_ID` |
| SIP / real telephony | ❌ | No real phone calls made |
| LLM / TTS / STT | ❌ | No AI processing |
| Analysis | ❌ | No transcript to analyze |

---

## Example Test Scenarios

### Scenario 1: Happy Path (all succeed)
```json
{"voice_id": "...", "initiated_by": 1, "total": 10, "fail_count": 0, "stuck_count": 0, "dropped_count": 0}
```
Expected: All 10 calls complete in ~20s. Report generated immediately.

### Scenario 2: All Fail
```json
{"voice_id": "...", "initiated_by": 1, "total": 10, "fail_count": 10, "stuck_count": 0, "dropped_count": 0}
```
Expected: All 10 calls fail in ~5s. Report generated with 0 completions.

### Scenario 3: Timeout Stress Test
```json
{"voice_id": "...", "initiated_by": 1, "total": 15, "fail_count": 0, "stuck_count": 15, "dropped_count": 0}
```
Expected: All 15 calls get stuck. Wave timeout fires at 20 min. Entries are re-queued (up to 2x), then failed. Total time: ~60 min.

### Scenario 4: Realistic Distribution
```json
{"voice_id": "...", "initiated_by": 1, "total": 149, "fail_count": 100, "stuck_count": 15, "dropped_count": 10}
```
Expected: Default distribution. Mix of immediate completions, failures, and timeout-resolved entries. Tests the full pipeline including retries.

---

## Architecture

```
┌────────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Client (curl)    │────▶│   Main API       │────▶│   PostgreSQL     │
│                    │     │   (FastAPI)       │     │                  │
│ trigger-test-batch │     │                  │     │ voice_call_batches│
└────────────────────┘     │ _execute_batch_  │     │ batch_entries    │
                           │  pipeline()      │     │ call_logs        │
                           │                  │     └──────────────────┘
                           │ dispatch_call()  │              ▲
                           └────────┬─────────┘              │
                                    │                        │
                                    ▼                        │
                           ┌──────────────────┐              │
                           │  LiveKit Cloud   │              │
                           │                  │              │
                           │ Routes to worker │              │
                           │ named            │              │
                           │ "batch-test-     │              │
                           │  worker"         │              │
                           └────────┬─────────┘              │
                                    │                        │
                                    ▼                        │
                           ┌──────────────────┐              │
                           │  Test Worker     │──────────────┘
                           │  (test_worker.py)│  Updates call_logs
                           │                  │  POSTs /entry-completed
                           │ Simulates:       │
                           │ ✅ completed     │
                           │ ❌ failed        │
                           │ 🔇 stuck/dropped │
                           └──────────────────┘
```

---

## Environment Variables

| Variable | Default | Used By |
|---|---|---|
| `LIVEKIT_URL` | — | Test worker connects to LiveKit |
| `LIVEKIT_API_KEY` | — | LiveKit authentication |
| `LIVEKIT_API_SECRET` | — | LiveKit authentication |
| `MAIN_API_BASE_URL` | `http://localhost:8000` | Test worker → Main API callback |

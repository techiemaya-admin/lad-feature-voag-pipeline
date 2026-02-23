# Call Cancellation API

All cancellation goes through a single unified endpoint. The system auto-detects whether you're cancelling a single call or a batch based on the `resource_id` format. Supports cancelling one or more resources in a single request.

## Authentication

All endpoints require the following headers:

| Header | Required | Description |
|--------|----------|-------------|
| `X-Frontend-ID` | ✅ | Your frontend identifier (provided during onboarding) |
| `X-API-Key` | ✅ | Secret API key associated with your frontend ID |
| `Content-Type` | ✅ | `application/json` |

Requests without valid credentials will receive a `401 Unauthorized` or `403 Forbidden` response.

---

## Endpoint

```
POST /calls/cancel
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `resource_id` | `string` or `string[]` | ✅ | One or more resource IDs (see below) |
| `force` | boolean | ❌ | Default `false`. When `true`, also terminates calls that are currently ringing/in-progress |

**What to pass as `resource_id`:**

- **For a single call**: The `id` (UUID) from `voice_call_logs`. This is the `call_log_id` returned by `POST /calls/start-call`.
- **For a batch**: The `job_id` from `voice_call_batches`. Always starts with `batch-` (e.g. `batch-abc123def456`). Returned by `POST /batch/trigger-batch-call`.
- **Multiple**: Pass an array to cancel several resources at once: `["batch-xxx", "uuid-yyy"]`

The endpoint auto-detects the type based on whether each value starts with `batch-` or not.

### Response

| Field | Type | Description |
|-------|------|-------------|
| `results` | array | Per-resource result items (see below) |
| `total_cancelled` | int | Sum of all cancelled items across all resources |

**Each item in `results`:**

| Field | Type | Description |
|-------|------|-------------|
| `resource_id` | string | Echo of the input |
| `resource_type` | string | `"call"` or `"batch"` |
| `status` | string | Final status (`cancelled`, `not_found`, or existing terminal status) |
| `cancelled_count` | int | Number of items cancelled for this resource |
| `message` | string | Human-readable summary |

---

## 1. Cancel a Single Call

```bash
curl -X POST https://YOUR_API_HOST/calls/cancel \
  -H "Content-Type: application/json" \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "resource_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }'
```

**Response:**
```json
{
  "results": [
    {
      "resource_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "resource_type": "call",
      "status": "cancelled",
      "cancelled_count": 1,
      "message": "Call cancelled. LiveKit room terminated."
    }
  ],
  "total_cancelled": 1
}
```

---

## 2. Stop a Batch (Graceful)

Pending calls are cancelled; running calls finish on their own.

```bash
curl -X POST https://YOUR_API_HOST/calls/cancel \
  -H "Content-Type: application/json" \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "resource_id": "batch-abc123def456",
    "force": false
  }'
```

**Response:**
```json
{
  "results": [
    {
      "resource_id": "batch-abc123def456",
      "resource_type": "batch",
      "status": "cancelled",
      "cancelled_count": 12,
      "message": "Batch cancelled. 12 pending entries cancelled. 3 active call(s) will complete naturally. Use force=true to terminate them."
    }
  ],
  "total_cancelled": 12
}
```

---

## 3. Stop a Batch (Force — Kill Running Calls)

Set `force: true` to immediately terminate all running calls as well.

```bash
curl -X POST https://YOUR_API_HOST/calls/cancel \
  -H "Content-Type: application/json" \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "resource_id": "batch-abc123def456",
    "force": true
  }'
```

**Response:**
```json
{
  "results": [
    {
      "resource_id": "batch-abc123def456",
      "resource_type": "batch",
      "status": "cancelled",
      "cancelled_count": 15,
      "message": "Batch cancelled. 12 pending entries cancelled. 3 running call(s) forcefully terminated."
    }
  ],
  "total_cancelled": 15
}
```

---

## 4. Cancel Multiple Resources at Once

Pass an array to cancel a batch and individual calls in one request.

```bash
curl -X POST https://YOUR_API_HOST/calls/cancel \
  -H "Content-Type: application/json" \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "resource_id": [
      "batch-abc123def456",
      "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "f9e8d7c6-b5a4-3210-fedc-ba9876543210"
    ],
    "force": true
  }'
```

**Response:**
```json
{
  "results": [
    {
      "resource_id": "batch-abc123def456",
      "resource_type": "batch",
      "status": "cancelled",
      "cancelled_count": 10,
      "message": "Batch cancelled. 8 pending entries cancelled. 2 running call(s) forcefully terminated."
    },
    {
      "resource_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "resource_type": "call",
      "status": "cancelled",
      "cancelled_count": 1,
      "message": "Call cancelled. LiveKit room terminated."
    },
    {
      "resource_id": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
      "resource_type": "call",
      "status": "ended",
      "cancelled_count": 0,
      "message": "Call already in terminal state: ended"
    }
  ],
  "total_cancelled": 11
}
```

> **Note:** Invalid or not-found resource IDs return `status: "not_found"` in their result item instead of failing the entire request.

---

## 5. Check Status After Cancellation

```
GET /calls/status/{resource_id}
```

Works for both call UUIDs and batch job_ids (`batch-xxx`).

**Single call:**
```bash
curl https://YOUR_API_HOST/calls/status/a1b2c3d4-e5f6-7890-abcd-ef1234567890 \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key"
```

**Batch:**
```bash
curl https://YOUR_API_HOST/calls/status/batch-abc123def456 \
  -H "X-Frontend-ID: your_frontend_id" \
  -H "X-API-Key: your_api_key"
```

The batch response includes an `entries` array — each entry has its own `status` field (`completed`, `cancelled`, `failed`, etc.).

---

## Quick Reference

| What you want | `resource_id` | `force` |
|---------------|--------------|---------|
| Cancel one active call | `<call_log_id>` (UUID) | omit |
| Cancel multiple calls | `["uuid-1", "uuid-2"]` | omit |
| Stop batch, let running calls finish | `"batch-xxx"` | `false` (default) |
| Stop batch + kill running calls | `"batch-xxx"` | `true` |
| Mix of batches and calls | `["batch-xxx", "uuid-1"]` | `true` or `false` |

## Error Codes

| Code | When |
|------|------|
| `200` | Request processed — check each `results[].status` for per-resource outcome |
| `503` | Database temporarily unavailable (retry) |

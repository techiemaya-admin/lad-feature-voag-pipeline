"""
Batch Test Worker — Minimal LiveKit agent for batch logic testing.

Registers as 'batch-test-worker' and simulates call outcomes
WITHOUT real SIP, LLM, TTS, or STT connections.

The test batch endpoint encodes probability distribution in added_context
as: [TEST_BATCH_PROBABILITIES]{"completed":24,"failed":100,"stuck":15,"dropped":10}

The worker reads these probabilities and uses random.choices with weights
to pick an outcome for each call.

Usage:
    uv run python -m agent.test_worker dev
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone

from dotenv import load_dotenv

# Disable OTEL to avoid 429 quota errors
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")

from livekit import agents, api

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("test_worker")

# Outcome constants
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_STUCK = "stuck"
OUTCOME_DROPPED = "dropped"

ALL_OUTCOMES = [OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_STUCK, OUTCOME_DROPPED]

# Marker prefix for probability distribution in added_context
PROB_MARKER = "[TEST_BATCH_PROBABILITIES]"


def _pick_outcome(metadata: dict) -> str:
    """
    Pick an outcome based on probability distribution from added_context.
    
    Format in added_context:
        [TEST_BATCH_PROBABILITIES]{"completed":24,"failed":100,"stuck":15,"dropped":10}
    
    The numbers are weights for random.choices (not percentages).
    Falls back to equal random distribution if no probabilities found.
    """
    added_context = metadata.get("added_context", "")
    
    if PROB_MARKER in added_context:
        try:
            json_str = added_context.split(PROB_MARKER, 1)[1]
            probs = json.loads(json_str)
            
            outcomes = []
            weights = []
            for outcome in ALL_OUTCOMES:
                weight = probs.get(outcome, 0)
                if weight > 0:
                    outcomes.append(outcome)
                    weights.append(weight)
            
            if outcomes:
                result = random.choices(outcomes, weights=weights, k=1)[0]
                return result
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.warning("Failed to parse probability distribution: %s", e)
    
    # Fallback: equal random distribution
    return random.choice(ALL_OUTCOMES)


async def _update_call_status(call_log_id: str, status: str, duration_seconds: int | None = None):
    """Update call_logs status directly via CallStorage."""
    try:
        from db.storage.calls import CallStorage
        storage = CallStorage()
        ended_at = datetime.now(timezone.utc)
        await storage.update_call_status(
            call_log_id=call_log_id,
            status=status,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
        )
        logger.info("Updated call_log %s → status=%s, duration=%s", call_log_id, status, duration_seconds)
    except Exception as e:
        logger.error("Failed to update call_log %s: %s", call_log_id, e, exc_info=True)


async def _notify_entry_completed(batch_id: str, entry_id: str, call_status: str):
    """POST to /batch/entry-completed on main API."""
    try:
        import httpx
        main_api_base = os.getenv("MAIN_API_BASE_URL", "http://localhost:8000")
        url = f"{main_api_base}/batch/entry-completed"
        payload = {
            "batch_id": batch_id,
            "entry_id": entry_id,
            "call_status": call_status,
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                result = response.json()
                logger.info(
                    "Entry %s completed → batch_completed=%s, report_triggered=%s",
                    entry_id[:8], result.get("batch_completed"), result.get("report_triggered"),
                )
            else:
                logger.error("Entry callback failed: status=%s, body=%s", response.status_code, response.text[:200])
    except Exception as e:
        logger.error("Failed to notify entry completion: %s", e, exc_info=True)


async def _delete_room(room_name: str):
    """Delete the LiveKit room to clean up resources."""
    try:
        url = os.getenv("LIVEKIT_URL", "")
        api_key = os.getenv("LIVEKIT_API_KEY", "")
        api_secret = os.getenv("LIVEKIT_API_SECRET", "")
        if not all([url, api_key, api_secret]):
            logger.warning("Missing LiveKit credentials, skipping room cleanup")
            return
        async with api.LiveKitAPI(url, api_key, api_secret) as livekit_api:
            await livekit_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        logger.info("  Deleted room %s", room_name)
    except Exception as e:
        logger.warning("  Failed to delete room %s: %s", room_name, e)


async def entrypoint(ctx: agents.JobContext):
    """
    Test worker entrypoint — simulate a call outcome.
    
    Reads probability distribution from added_context in job metadata,
    picks a weighted-random outcome, and simulates it.
    """
    job_id = getattr(ctx.job, "id", "unknown")
    logger.info("=" * 50)
    logger.info("TEST WORKER: Job %s received", job_id)
    logger.info("=" * 50)
    
    # Parse metadata
    metadata = {}
    call_log_id = None
    batch_id = None
    entry_id = None
    
    if ctx.job.metadata:
        try:
            metadata = json.loads(ctx.job.metadata)
        except json.JSONDecodeError:
            logger.warning("Failed to parse job metadata: %s", ctx.job.metadata[:100])
    
    call_log_id = metadata.get("call_log_id")
    batch_id = metadata.get("batch_id")
    entry_id = metadata.get("entry_id")
    to_number = metadata.get("to_number", "unknown")
    room_name = ctx.room.name if ctx.room else None
    
    logger.info(
        "  call_log_id=%s, batch_id=%s, entry_id=%s, to=%s",
        call_log_id, batch_id and batch_id[:8], entry_id and entry_id[:8], to_number,
    )
    
    # Pick outcome based on probability distribution
    outcome = _pick_outcome(metadata)
    logger.info("  OUTCOME: %s", outcome)
    
    # Connect to the room (required by LiveKit agent framework)
    await ctx.connect()
    
    if outcome in (OUTCOME_STUCK, OUTCOME_DROPPED):
        # Simulate a stuck/dropped call — disconnect without updating anything.
        # The wave timeout handler should catch this.
        logger.info("  Simulating %s call — disconnecting without status update", outcome.upper())
        await asyncio.sleep(1)
        # Clean up room even for stuck/dropped
        if room_name:
            await _delete_room(room_name)
        return
    
    # Simulate call duration
    if outcome == OUTCOME_COMPLETED:
        duration = random.randint(2, 20)
        final_status = "ended"
    else:  # OUTCOME_FAILED
        duration = random.randint(1, 5)
        final_status = "failed"
    
    logger.info("  Sleeping %ds to simulate call...", duration)
    await asyncio.sleep(duration)
    
    # Update call_logs status
    if call_log_id:
        await _update_call_status(
            call_log_id=call_log_id,
            status=final_status,
            duration_seconds=duration if outcome == OUTCOME_COMPLETED else 0,
        )
    
    # Notify main API of batch entry completion
    if batch_id and entry_id:
        await _notify_entry_completed(batch_id, entry_id, final_status)
    
    # Clean up LiveKit room
    if room_name:
        await _delete_room(room_name)
    
    logger.info("  Job %s finished — outcome=%s, status=%s", job_id, outcome, final_status)


if __name__ == "__main__":
    logger.info("Starting batch test worker (agent_name=batch-test-worker)")
    
    try:
        agents.cli.run_app(
            agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                agent_name="batch-test-worker",
                # Auto-pick port to avoid conflicts with real worker
                port=0,
                initialize_process_timeout=30.0,
                shutdown_process_timeout=30.0,
                num_idle_processes=0,
            )
        )
    except KeyboardInterrupt:
        logger.info("Test worker stopped")

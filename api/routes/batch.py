"""
Batch Routes Module (V2 API).

Handles batch call operations with kebab-case naming:
- POST /trigger-batch-call: Start batch call job
- POST /trigger-test-batch: Start test batch with simulated outcomes

Note: Batch status and cancel are handled by calls.py (unified API).
"""

import asyncio
import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from api.models import (
    CallJobResponse,
    BatchCallJsonRequest,
    BatchCallJsonEntry,
    JobStatus,
    CallMode,
    CallJob,
    CallAttemptResult,
    BatchStatusResponse,
    BatchEntryStatusModel,
)
from api.services.call_service import (
    get_call_service,
    BatchCallEntry,
    _normalize_llm_provider,
    _clean_optional_text,
    DEFAULT_GLINKS_KB_STORE_IDS,
)
from db.storage import BatchStorage, VoiceStorage
from db.connection_pool import get_db_connection
from db.db_config import get_db_config
from db.schema_constants import USERS_FULL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/batch", tags=["batch"])

# Lazy initialization
_batch_storage: BatchStorage | None = None
_voice_storage: VoiceStorage | None = None


def _get_batch_storage() -> BatchStorage:
    global _batch_storage
    if _batch_storage is None:
        _batch_storage = BatchStorage()
    return _batch_storage


def _get_voice_storage() -> VoiceStorage:
    global _voice_storage
    if _voice_storage is None:
        _voice_storage = VoiceStorage()
    return _voice_storage


# =============================================================================
# BATCH PACING CONFIGURATION
# =============================================================================
# Wave-based dispatch: process BATCH_WAVE_SIZE calls at a time, wait for completion
BATCH_WAVE_SIZE = int(os.getenv("BATCH_WAVE_SIZE", "12"))
BATCH_WAVE_POLL_INTERVAL = float(os.getenv("BATCH_WAVE_POLL_INTERVAL", "5.0"))  # seconds
BATCH_WAVE_TIMEOUT = float(os.getenv("BATCH_WAVE_TIMEOUT", "1200.0"))  # 20 min max per wave
BATCH_MAX_RETRIES = int(os.getenv("BATCH_MAX_RETRIES", "1"))  # Max retries for stuck/dropped calls
BATCH_ONGOING_TIMEOUT = int(os.getenv("BATCH_ONGOING_TIMEOUT", "15"))  # Minutes before ongoing call is considered stuck

async def _resolve_tenant_id_from_user(user_id: str | None) -> str | None:
    """
    Resolve tenant_id from user's primary_tenant_id in lad_dev.users table.
    
    Args:
        user_id: User UUID (initiated_by)
        
    Returns:
        Tenant UUID or None if not found
    """
    if not user_id:
        return None
    
    try:
        # Use standard connection pattern (same as old code)
        with get_db_connection(get_db_config()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT primary_tenant_id FROM {USERS_FULL} WHERE id = %s",
                    (user_id,)
                )
                result = cur.fetchone()
                if result and result[0]:
                    return str(result[0])
                return None
    except Exception as e:
        logger.error(f"Failed to resolve tenant_id for user {user_id}: {e}")
        return None


# E.164 pattern for validation
# E.164 pattern for validation (removed in favor of semantic validation)
from utils.call_routing import normalize_phone_to_e164


def _validate_optional_number(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return normalize_phone_to_e164(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} must be E.164 formatted")


def _validate_optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{field_name} must be an integer")
    if not isinstance(value, int):
        return None
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"{field_name} must be positive")
    return value


def _clean_optional_context(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    return text if text else None


def _parse_batch_csv(file_bytes: bytes) -> list[BatchCallEntry]:
    """Parse CSV file into batch call entries."""
    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = file_bytes.decode("latin-1")
    
    reader = csv.DictReader(io.StringIO(content))
    entries = []
    
    for row in reader:
        to_number = row.get("to_number") or row.get("phone") or row.get("number")
        if not to_number:
            continue
        
        to_number = to_number.strip()
        try:
            to_number = normalize_phone_to_e164(to_number)
        except ValueError:
            logger.warning("Skipping invalid number in CSV: %s", to_number[:5] + "...")
            continue
        
        entries.append(BatchCallEntry(
            to_number=to_number,
            context=row.get("context") or row.get("added_context"),
            lead_name=row.get("lead_name") or row.get("name"),
        ))
    
    return entries


# =============================================================================
# WAVE COMPLETION HELPER
# =============================================================================

async def _wait_for_wave_completion(
    batch_id: str, 
    entry_ids: list[str], 
    wave_num: int,
    job_id: str
) -> dict:
    """
    Wait for all entries in wave to reach terminal state or timeout.
    
    Uses the new status-aware timeout handling:
    - dispatched entries: reset to queued for retry (up to max_retries)
    - ringing entries: mark as failed (stuck in ringing)
    - ongoing entries: wait if < 15 min, fail if > 15 min
    
    Args:
        batch_id: Batch UUID
        entry_ids: List of entry UUIDs in this wave
        wave_num: Wave number for logging
        job_id: Job ID for logging
        
    Returns:
        Dict with timeout handling results
    """
    batch_storage = _get_batch_storage()
    start_time = asyncio.get_event_loop().time()
    
    while True:
        # Check if all entries are done (completed, failed, cancelled, declined, ended)
        pending = await batch_storage.count_pending_entries(batch_id, entry_ids)
        
        if pending == 0:
            # Sync batch_entries.status from call_logs (source of truth)
            # This catches any entries where the entry-completed callback failed
            await batch_storage.sync_entry_statuses_from_call_logs(batch_id, entry_ids)
            logger.info("Batch %s wave %d: All entries completed", job_id, wave_num)
            return {"completed": True, "timeout_results": None}
        
        # Check if batch was cancelled — exit early instead of waiting 20 min
        if await batch_storage.is_batch_stopped(batch_id):
            logger.info("Batch %s wave %d: Batch stopped/cancelled, exiting wave wait", job_id, wave_num)
            return {"completed": False, "timeout_results": None, "cancelled": True}
        
        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > BATCH_WAVE_TIMEOUT:
            logger.warning(
                "Batch %s wave %d: Timeout after %.1fs, %d entries still pending", 
                job_id, wave_num, elapsed, pending
            )
            
            # Use intelligent timeout handling
            timeout_results = await batch_storage.handle_wave_timeout(
                batch_id=batch_id,
                entry_ids=entry_ids,
                max_retries=BATCH_MAX_RETRIES,
                ongoing_timeout_minutes=BATCH_ONGOING_TIMEOUT,
            )
            
            # If there are still ongoing entries, extend wait
            if timeout_results["still_ongoing"]:
                logger.info(
                    "Batch %s wave %d: %d entries still ongoing, extending wait",
                    job_id, wave_num, len(timeout_results["still_ongoing"])
                )
                # Reset timer but with shorter timeout for ongoing entries
                start_time = asyncio.get_event_loop().time()
                # Continue waiting with reduced timeout (5 min for ongoing)
                continue
            
            return {"completed": False, "timeout_results": timeout_results}
        
        # Log progress periodically (every 30 seconds)
        if int(elapsed) % 30 == 0 and int(elapsed) > 0:
            if pending < 5:
                # Get details of pending entries for debugging
                pending_details = await batch_storage.get_pending_entry_details(batch_id, entry_ids)
                if pending_details:
                    detail_strs = [
                        f"{d.get('to_number', '?')} (call:{d.get('call_log_id', '?')[:8] if d.get('call_log_id') else '?'}, status:{d.get('status', '?')})"
                        for d in pending_details
                    ]
                    logger.info(
                        "Batch %s wave %d: Waiting for %d entries (%.0fs elapsed): %s",
                        job_id, wave_num, pending, elapsed, ", ".join(detail_strs)
                    )
                else:
                    logger.info(
                        "Batch %s wave %d: Waiting for %d entries (%.0fs elapsed)",
                        job_id, wave_num, pending, elapsed
                    )
            else:
                logger.info(
                    "Batch %s wave %d: Waiting for %d entries (%.0fs elapsed)",
                    job_id, wave_num, pending, elapsed
                )
        
        await asyncio.sleep(BATCH_WAVE_POLL_INTERVAL)


# =============================================================================
# SHARED BATCH PIPELINE
# =============================================================================

async def _execute_batch_pipeline(
    *,
    entries: list[BatchCallEntry],
    voice_id: str,
    clean_from_number: str | None,
    clean_context: str | None,
    initiator_id,
    assigned_agent_id: int | None,
    llm_provider_override: str | None = None,
    llm_model_override: str | None = None,
    frontend_id: str | None = None,
    worker_name_override: str | None = None,
) -> dict:
    """
    Shared batch creation and processing pipeline.
    
    Used by both trigger-batch-call (production) and trigger-test-batch (testing).
    For test batches, worker_name_override="batch-test-worker" is the only difference.
    The same wave dispatch, status tracking, and report generation logic runs.
    """
    call_service = get_call_service()
    batch_storage = _get_batch_storage()

    if not entries:
        raise HTTPException(status_code=400, detail="No valid entries found")

    if not voice_id or not voice_id.strip():
        raise HTTPException(status_code=400, detail="voice_id cannot be empty")

    # Resolve voice
    try:
        resolved_voice_id, voice_context = await call_service.resolve_voice(voice_id, assigned_agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Resolve KB store IDs
    batch_kb_store_ids: list[str] | None = None
    if await call_service.should_use_glinks_kb(assigned_agent_id, frontend_id, batch_kb_store_ids):
        batch_kb_store_ids = DEFAULT_GLINKS_KB_STORE_IDS
        logger.info(
            "Auto-assigned default Glinks KB for batch: agent_id=%s, frontend_id=%s",
            assigned_agent_id, frontend_id,
        )

    if batch_kb_store_ids:
        for entry in entries:
            if not entry.knowledge_base_store_ids:
                entry.knowledge_base_store_ids = batch_kb_store_ids

    # Generate job_id with batch- prefix
    job_id = f"batch-{uuid.uuid4().hex}"

    # Resolve tenant_id from initiator's primary_tenant_id
    tenant_id = await _resolve_tenant_id_from_user(initiator_id)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not resolve tenant_id for initiated_by user"
        )

    # Create batch record in database
    batch_id = await batch_storage.create_batch(
        tenant_id=tenant_id,
        total_calls=len(entries),
        job_id=job_id,
        initiated_by_user_id=initiator_id,
        agent_id=assigned_agent_id,
        voice_id=voice_context.db_voice_id,
        from_number_id=None,
        base_context=clean_context,
        llm_provider=llm_provider_override,
        llm_model=llm_model_override,
    )

    if not batch_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create batch record in database"
        )

    logger.info("Created batch record: batch_id=%s, job_id=%s, total_calls=%d", batch_id, job_id, len(entries))

    # Create batch entry records and track their IDs
    entry_ids: list[str] = []
    for i, entry in enumerate(entries):
        entry_metadata = {"entry_index": i}
        if entry.lead_name:
            entry_metadata["lead_name"] = entry.lead_name
        if entry.context:
            entry_metadata["added_context"] = entry.context

        entry_id = await batch_storage.create_batch_entry(
            tenant_id=tenant_id,
            batch_id=batch_id,
            to_phone=entry.to_number,
            lead_id=None,
            call_log_id=None,
            metadata=entry_metadata,
        )
        if entry_id:
            entry_ids.append(entry_id)
        else:
            logger.warning("Failed to create batch entry %d", i)
            entry_ids.append("")

    # Fire and forget the batch processing with wave-based pacing
    async def _process_batch():
        """
        Process batch in waves of BATCH_WAVE_SIZE calls.
        
        This prevents overwhelming the worker by waiting for each wave
        to complete before starting the next.
        """
        call_service = get_call_service()
        batch_storage = _get_batch_storage()
        
        total = len(entries)
        wave_num = 0
        
        # Mark batch as running BEFORE dispatching
        await batch_storage.update_batch_status(batch_id, "running")
        
        for wave_start in range(0, total, BATCH_WAVE_SIZE):
            wave_num += 1
            wave_end = min(wave_start + BATCH_WAVE_SIZE, total)
            wave_entries = entries[wave_start:wave_end]
            wave_entry_ids = entry_ids[wave_start:wave_end]
            
            logger.info(
                "Batch %s: Starting wave %d (%d-%d of %d)", 
                job_id, wave_num, wave_start + 1, wave_end, total
            )
            
            # Dispatch all calls in this wave
            dispatched_entry_ids = []
            for i, (entry, entry_id) in enumerate(zip(wave_entries, wave_entry_ids)):
                if not entry_id:
                    logger.warning("Skipping entry %d - no entry_id", wave_start + i)
                    continue
                
                try:
                    await batch_storage.update_batch_entry_status(entry_id, "running")
                    
                    result = await call_service.dispatch_call(
                        job_id=job_id,
                        voice_id=resolved_voice_id,
                        voice_context=voice_context,
                        from_number=clean_from_number,
                        to_number=entry.to_number,
                        context=entry.context or clean_context,
                        initiated_by=initiator_id,
                        agent_id=assigned_agent_id,
                        llm_provider=llm_provider_override,
                        llm_model=llm_model_override,
                        knowledge_base_store_ids=entry.knowledge_base_store_ids,
                        lead_name=entry.lead_name,
                        lead_id_override=entry.lead_id,
                        batch_id=str(batch_id),
                        entry_id=entry_id,
                        worker_name_override=worker_name_override,
                    )
                    
                    await batch_storage.update_batch_entry_call_log(batch_id, entry_id, result.call_log_id)
                    await batch_storage.update_batch_entry_status(entry_id, "dispatched")
                    dispatched_entry_ids.append(entry_id)
                    
                except Exception as exc:
                    logger.exception("Batch entry %d failed: %s", wave_start + i, exc)
                    await batch_storage.update_batch_entry_status(entry_id, "failed", error_message=str(exc)[:500])
                    await batch_storage.increment_batch_counters(batch_id, failed_delta=1)
            
            # Wait for this wave to complete before starting next
            if dispatched_entry_ids:
                wave_result = await _wait_for_wave_completion(batch_id, dispatched_entry_ids, wave_num, job_id)
                
                # If wave was cancelled, trigger report if any calls completed and exit
                if wave_result.get("cancelled"):
                    logger.info("Batch %s: Wave %d cancelled, checking for partial report", job_id, wave_num)
                    batch_record = await batch_storage.get_batch_by_id(str(batch_id))
                    completed_calls = batch_record.get("completed_calls", 0) if batch_record else 0
                    if completed_calls > 0:
                        logger.info("Batch %s: %d completed calls — triggering partial report", job_id, completed_calls)
                        asyncio.create_task(_generate_and_send_batch_report(str(batch_id)))
                    return
                
                timeout_results = wave_result.get("timeout_results")
                
                if timeout_results:
                    timeout_failed = (
                        timeout_results.get("failed_max_retries", 0)
                        + timeout_results.get("failed_ringing", 0)
                        + timeout_results.get("failed_ongoing_stuck", 0)
                        + timeout_results.get("recovered_failed", 0)
                    )
                    timeout_completed = timeout_results.get("recovered_completed", 0)
                    if timeout_failed > 0:
                        await batch_storage.increment_batch_counters(batch_id, failed_delta=timeout_failed)
                        logger.info("Batch %s wave %d: Incremented failed_calls by %d (timeout failures)", job_id, wave_num, timeout_failed)
                    if timeout_completed > 0:
                        await batch_storage.increment_batch_counters(batch_id, completed_delta=timeout_completed)
                        logger.info("Batch %s wave %d: Incremented completed_calls by %d (recovered from call_logs)", job_id, wave_num, timeout_completed)
                
                if timeout_results and timeout_results.get("reset_to_queued", 0) > 0:
                    retry_entries = await batch_storage.get_queued_entries_for_wave(
                        str(batch_id), wave_size=timeout_results["reset_to_queued"]
                    )
                    if retry_entries:
                        logger.info(
                            "Batch %s wave %d: Re-dispatching %d retried entries",
                            job_id, wave_num, len(retry_entries)
                        )
                        retry_entry_ids = []
                        for retry_entry in retry_entries:
                            rid = str(retry_entry["id"])
                            try:
                                await batch_storage.update_batch_entry_status(rid, "running")
                                result = await call_service.dispatch_call(
                                    job_id=job_id,
                                    voice_id=resolved_voice_id,
                                    voice_context=voice_context,
                                    from_number=clean_from_number,
                                    to_number=retry_entry["to_phone"],
                                    context=retry_entry.get("metadata", {}).get("context") or clean_context,
                                    initiated_by=initiator_id,
                                    agent_id=assigned_agent_id,
                                    llm_provider=llm_provider_override,
                                    llm_model=llm_model_override,
                                    knowledge_base_store_ids=retry_entry.get("metadata", {}).get("knowledge_base_store_ids"),
                                    lead_name=retry_entry.get("metadata", {}).get("lead_name"),
                                    lead_id_override=retry_entry.get("lead_id"),
                                    batch_id=str(batch_id),
                                    entry_id=rid,
                                    worker_name_override=worker_name_override,
                                )
                                await batch_storage.update_batch_entry_call_log(batch_id, rid, result.call_log_id)
                                await batch_storage.update_batch_entry_status(rid, "dispatched")
                                retry_entry_ids.append(rid)
                            except Exception as exc:
                                logger.exception("Retry dispatch failed for entry %s: %s", rid, exc)
                                await batch_storage.update_batch_entry_status(rid, "failed", error_message=str(exc)[:500])
                                await batch_storage.increment_batch_counters(batch_id, failed_delta=1)
                        
                        if retry_entry_ids:
                            await _wait_for_wave_completion(batch_id, retry_entry_ids, wave_num, job_id)
            
            logger.info("Batch %s: Wave %d complete", job_id, wave_num)
            
            # Check between waves if batch was cancelled
            if await batch_storage.is_batch_stopped(str(batch_id)):
                logger.info("Batch %s: Stopped/cancelled between waves, checking for partial report", job_id)
                batch_record = await batch_storage.get_batch_by_id(str(batch_id))
                completed_calls = batch_record.get("completed_calls", 0) if batch_record else 0
                if completed_calls > 0:
                    logger.info("Batch %s: %d completed calls — triggering partial report", job_id, completed_calls)
                    asyncio.create_task(_generate_and_send_batch_report(str(batch_id)))
                return
        
        logger.info("Batch %s dispatched all %d calls in %d waves", job_id, total, wave_num)
        
        # Final safety net: check if batch is complete and trigger report
        # This catches cases where entry_completed callbacks failed but
        # sync_entry_statuses_from_call_logs already synced all entries
        await asyncio.sleep(15)  # Allow any in-flight callbacks to land
        
        result = await batch_storage.check_and_complete_batch(str(batch_id))
        if result.get("should_report") or result.get("completed"):
            logger.info("Batch %s: All waves done, triggering report generation", job_id)
            asyncio.create_task(_generate_and_send_batch_report(str(batch_id)))
        else:
            logger.info(
                "Batch %s: All waves dispatched but batch not yet complete (report pending from callbacks)",
                job_id,
            )

    asyncio.create_task(_process_batch())
    
    return {
        "job_id": job_id,
        "batch_id": str(batch_id),
        "total_entries": len(entries),
        "status": "accepted",
        "message": f"Batch job started with {len(entries)} calls",
    }


# =============================================================================
# POST /trigger-batch-call - Start batch call job
# =============================================================================

@router.post("/trigger-batch-call", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
async def trigger_batch_call(request: Request) -> dict[str, Any]:
    """
    Start a batch call job.
    
    Supports two formats:
    1. JSON payload with entries array
    2. Form data with CSV file upload
    
    Returns batch job ID for status tracking.
    """
    
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    entries: list[BatchCallEntry]
    clean_from_number: str | None
    clean_context: str | None
    initiator_id: int | None
    assigned_agent_id: int | None
    llm_provider_override: str | None
    llm_model_override: str | None
    voice_id: str

    if content_type == "application/json":
        try:
            payload_dict = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc.msg}")

        try:
            json_payload = BatchCallJsonRequest(**payload_dict)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail={"message": "Invalid request body", "errors": exc.errors()})

        voice_id = json_payload.voice_id
        clean_from_number = json_payload.from_number
        clean_context = _clean_optional_context(json_payload.added_context)
        initiator_id = json_payload.initiated_by
        assigned_agent_id = json_payload.agent_id
        llm_provider_override = _normalize_llm_provider(json_payload.llm_provider)
        llm_model_override = _clean_optional_text(json_payload.llm_model)
        entries = [
            BatchCallEntry(
                to_number=item.to_number,
                context=item.added_context,
                lead_name=item.lead_name,
                lead_id=item.lead_id,
                knowledge_base_store_ids=item.knowledge_base_store_ids,
            )
            for item in json_payload.entries
        ]
    else:
        form = await request.form()
        voice_id_value = form.get("voice_id")
        if not voice_id_value or not str(voice_id_value).strip():
            raise HTTPException(status_code=400, detail="voice_id cannot be empty")
        voice_id = str(voice_id_value).strip()

        csv_upload = form.get("csv_file")
        if not isinstance(csv_upload, UploadFile):
            raise HTTPException(status_code=400, detail="csv_file is required")

        clean_from_number = _validate_optional_number(str(form.get("from_number")) if form.get("from_number") else None, "from_number")
        clean_context = _clean_optional_context(str(form.get("added_context")) if form.get("added_context") else None)
        initiator_id = _validate_optional_positive_int(form.get("initiated_by"), "initiated_by")
        assigned_agent_id = _validate_optional_positive_int(form.get("agent_id"), "agent_id")
        llm_provider_override = _normalize_llm_provider(str(form.get("llm_provider")) if form.get("llm_provider") else None)
        llm_model_override = _clean_optional_text(str(form.get("llm_model")) if form.get("llm_model") else None)

        file_bytes = await csv_upload.read()
        entries = _parse_batch_csv(file_bytes)

    frontend_id = request.headers.get("X-Frontend-ID")
    return await _execute_batch_pipeline(
        entries=entries,
        voice_id=voice_id,
        clean_from_number=clean_from_number,
        clean_context=clean_context,
        initiator_id=initiator_id,
        assigned_agent_id=assigned_agent_id,
        llm_provider_override=llm_provider_override,
        llm_model_override=llm_model_override,
        frontend_id=frontend_id,
    )


# =============================================================================
# GET /batch-status/{batch_id} - Get batch status
# =============================================================================

@router.get("/batch-status/{batch_id}", response_model=BatchStatusResponse)
async def get_batch_status(batch_id: str) -> BatchStatusResponse:
    """
    Get status of a batch call job.
    
    Args:
        batch_id: Job ID (batch-xxx format) or batch UUID
    
    Returns:
        BatchStatusResponse with status and entries
    """
    batch_storage = _get_batch_storage()
    
    # Try as job_id first
    if batch_id.startswith("batch-"):
        batch_record = await batch_storage.get_batch_by_job_id(batch_id)
    else:
        batch_record = await batch_storage.get_batch_by_id(batch_id)
    
    if not batch_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch not found: {batch_id}"
        )
    
    entries = await batch_storage.get_batch_entries(batch_record["id"])
    
    pending = sum(1 for e in entries if e["status"] in ("queued", "dispatched"))
    running = sum(1 for e in entries if e["status"] in ("running", "ongoing"))
    
    entry_models = [
        BatchEntryStatusModel(
            entry_index=e["entry_index"],
            to_number=e["to_number"],
            lead_name=e.get("lead_name"),
            status=e["status"],
            call_log_id=e.get("call_log_id"),
            call_status=e.get("call_status"),
            call_duration=e.get("call_duration"),
            call_recording_url=e.get("call_recording_url"),
            error_message=e.get("error_message"),
        )
        for e in entries
    ]
    
    return BatchStatusResponse(
        batch_id=batch_record["id"],
        job_id=batch_record["job_id"],
        status=batch_record["status"],
        total_calls=batch_record["total_calls"],
        completed_calls=batch_record["completed_calls"],
        failed_calls=batch_record["failed_calls"],
        cancelled_calls=batch_record.get("cancelled_calls", 0),
        pending_calls=pending,
        running_calls=running,
        initiated_by=batch_record.get("initiated_by"),
        agent_id=batch_record.get("agent_id"),
        created_at=batch_record.get("created_at"),
        updated_at=batch_record.get("updated_at"),
        stopped_at=batch_record.get("stopped_at"),
        completed_at=batch_record.get("completed_at"),
        entries=entry_models,
    )




# =============================================================================
# POST /batch/trigger-test-batch - Test batch with simulated outcomes
# =============================================================================

class TestBatchRequest(BaseModel):
    """Request body for test batch endpoint."""
    voice_id: str = Field(..., description="Voice ID (must exist in DB)")
    initiated_by: str = Field(..., description="User UUID (initiator)")
    agent_id: int | None = Field(None, description="Agent ID (optional)")
    from_number: str | None = Field(None, description="From number (optional, no SIP for tests)")
    total: int = Field(149, description="Total number of test calls")
    fail_count: int = Field(100, description="Number of calls that should fail")
    stuck_count: int = Field(15, description="Number of calls that get stuck (no status update)")
    dropped_count: int = Field(10, description="Number of calls that are dropped (request lost)")


@router.post("/trigger-test-batch", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
async def trigger_test_batch(body: TestBatchRequest) -> dict[str, Any]:
    """
    Start a test batch that uses batch-test-worker for simulated outcomes.
    
    Generates fake entries with test phone numbers and encodes the
    probability distribution in added_context for the test worker.
    The SAME batch pipeline runs — only the worker name is overridden.
    
    Defaults: 149 total, 100 fail, 15 stuck, 10 dropped, 24 success.
    """
    success_count = body.total - body.fail_count - body.stuck_count - body.dropped_count
    if success_count < 0:
        raise HTTPException(
            status_code=400,
            detail=f"Sum of fail({body.fail_count})+stuck({body.stuck_count})+dropped({body.dropped_count})={body.fail_count + body.stuck_count + body.dropped_count} exceeds total({body.total})",
        )
    
    # Generate fake entries with test phone numbers
    entries = [
        BatchCallEntry(to_number=f"+1555000{i:04d}")
        for i in range(body.total)
    ]
    
    # Encode probability distribution in added_context for test worker to read
    probabilities = {
        "completed": success_count,
        "failed": body.fail_count,
        "stuck": body.stuck_count,
        "dropped": body.dropped_count,
    }
    context = f"[TEST_BATCH_PROBABILITIES]{json.dumps(probabilities)}"
    
    logger.info(
        "Starting test batch: total=%d, success=%d, fail=%d, stuck=%d, dropped=%d",
        body.total, success_count, body.fail_count, body.stuck_count, body.dropped_count,
    )
    
    return await _execute_batch_pipeline(
        entries=entries,
        voice_id=body.voice_id,
        clean_from_number=body.from_number,
        clean_context=context,
        initiator_id=body.initiated_by,
        assigned_agent_id=body.agent_id,
        worker_name_override="batch-test-worker",
    )


# =============================================================================
# POST /batch/entry-completed - Worker callback for entry completion
# =============================================================================

class EntryCompletedRequest(BaseModel):
    """Request body for entry completion callback."""
    batch_id: str = Field(..., description="Batch UUID")
    entry_id: str = Field(..., description="Entry UUID")
    call_status: str = Field(..., description="Final call status (ended, failed, etc)")


@router.post("/entry-completed")
async def entry_completed_callback(request: EntryCompletedRequest) -> dict:
    """
    Internal endpoint for worker to notify that a batch entry call has completed.
    
    This runs in main.py (stable process) so report generation won't be killed.
    
    Args:
        batch_id: Batch UUID
        entry_id: Entry UUID  
        call_status: Final call status
    
    Returns:
        Status response with batch completion info
    """
    batch_storage = _get_batch_storage()
    
    # Map call status to entry status
    entry_status = "completed" if request.call_status in ("ended", "completed") else "failed"
    
    # Update entry status
    await batch_storage.update_batch_entry_status(request.entry_id, entry_status)
    logger.info(f"Updated batch entry {request.entry_id} to status={entry_status}")
    
    # Increment batch counters
    if entry_status == "completed":
        await batch_storage.increment_batch_counters(request.batch_id, completed_delta=1)
    else:
        await batch_storage.increment_batch_counters(request.batch_id, failed_delta=1)
    
    # Check if batch is complete
    result = await batch_storage.check_and_complete_batch(request.batch_id)
    
    if result.get("should_report"):
        logger.info(f"Batch {request.batch_id} fully complete - triggering report from main server")
        # Fire and forget - runs in main.py's event loop (stable process)
        asyncio.create_task(_generate_and_send_batch_report(request.batch_id))
    
    return {
        "status": "ok",
        "entry_id": request.entry_id,
        "entry_status": entry_status,
        "batch_completed": result.get("completed", False),
        "report_triggered": result.get("should_report", False),
    }


async def _generate_and_send_batch_report(batch_id: str) -> None:
    """Generate and send batch report email. Runs in main.py's stable event loop.
    
    Includes a 15s delay to ensure the last call's analysis has time to complete
    and save to DB before we start polling for analysis records.
    """
    try:
        # Wait 15s to give the final analysis time to complete and save to DB
        # This prevents the race condition where report starts before analysis finishes
        logger.info(f"Waiting 15s for analysis completion before generating report for batch_id={batch_id}")
        await asyncio.sleep(15)
        
        from analysis.batch_report import generate_batch_report
        logger.info(f"Starting batch report generation for batch_id={batch_id}")
        result = await generate_batch_report(batch_id, send_email=True)
        if result.get("status") == "success":
            logger.info(f"Batch report sent for batch_id={batch_id}")
        else:
            logger.warning(f"Batch report failed for batch_id={batch_id}: {result.get('message')}")
    except Exception as e:
        logger.error(f"Error generating batch report: {e}", exc_info=True)


__all__ = ["router"]

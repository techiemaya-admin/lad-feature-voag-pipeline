"""
Call Routes Module (V2 API).

Handles call initiation and management endpoints with kebab-case naming:
- POST /start-call: Initiate single outbound call (trigger_single_call)
- POST /cancel: Cancel call or stop batch
- GET /status/{resource_id}: Get call or batch status
- GET /job/{job_id}: Get job status

Note: This implements the full route logic migrated from main.py.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from api.models import (
    CallJobResponse,
    CallStatusResponse,
    BatchStatusResponse,
    BatchEntryStatusModel,
    CancelRequest,
    CancelResponse,
    CancelResultItem,
    SingleCallPayload,
    CallAttemptModel,
    JobStatus,
    CallMode,
    CallJob,
    CallAttemptResult,
)
from api.services.call_service import (
    get_call_service,
    _normalize_llm_provider,
    _clean_optional_text,
    DEFAULT_GLINKS_KB_STORE_IDS,
)
from db.storage import CallStorage, BatchStorage
from db.db_config import get_db_config
from utils.call_routing import validate_and_format_call

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])

# Lazy-initialized storage instances
_call_storage: CallStorage | None = None
_batch_storage: BatchStorage | None = None


def _get_call_storage() -> CallStorage:
    global _call_storage
    if _call_storage is None:
        _call_storage = CallStorage()
    return _call_storage


def _get_batch_storage() -> BatchStorage:
    global _batch_storage
    if _batch_storage is None:
        _batch_storage = BatchStorage()
    return _batch_storage


# =============================================================================
# JOB STORE (In-memory for now)
# =============================================================================

class JobStore:
    """Simple in-memory job store."""
    
    def __init__(self):
        self._jobs: dict[str, CallJob] = {}
        self._lock = asyncio.Lock()
    
    async def create(self, job: CallJob) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job
    
    async def get(self, job_id: str) -> CallJob | None:
        async with self._lock:
            return self._jobs.get(job_id)
    
    async def update(self, job_id: str, **updates) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for key, value in updates.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                job.updated_at = datetime.now(timezone.utc)


_job_store: JobStore | None = None


def _get_job_store() -> JobStore:
    global _job_store
    if _job_store is None:
        _job_store = JobStore()
    return _job_store


def job_to_response(job: CallJob) -> CallJobResponse:
    """Convert CallJob to CallJobResponse."""
    return CallJobResponse(
        job_id=job.job_id,
        mode=job.mode,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        voice_id=job.voice_id,
        tts_voice_id=job.tts_voice_id,
        voice_provider=job.voice_provider,
        voice_name=job.voice_name,
        llm_provider=job.llm_provider,
        llm_model=job.llm_model,
        from_number=job.from_number,
        base_context=job.base_context,
        initiated_by=job.initiated_by,
        agent_id=job.agent_id,
        results=[
            CallAttemptModel(
                to_number=r.to_number,
                status=r.status,
                room_name=r.room_name,
                dispatch_id=r.dispatch_id,
                error=r.error,
                index=r.index,
                lead_name=r.lead_name,
                context=r.context,
                call_log_id=r.call_log_id,
            )
            for r in job.results
        ],
        error=job.error,
    )



# =============================================================================
# GET /calls/status/{resource_id} - Get call or batch status
# =============================================================================

@router.get("/status/{resource_id}")
async def get_call_or_batch_status(resource_id: str) -> CallStatusResponse | BatchStatusResponse:
    """
    Get status of a call or batch.
    
    If resource_id starts with 'batch-', returns batch status with all entries.
    Otherwise, returns status for a single call from call_logs.
    
    Args:
        resource_id: Call log UUID or batch job_id (with batch- prefix)
    
    Returns:
        BatchStatusResponse for batches, CallStatusResponse for individual calls
    
    Raises:
        404: Resource not found
        503: Database temporarily unavailable
    """
    resource_id = resource_id.strip()
    call_storage = _get_call_storage()
    batch_storage = _get_batch_storage()
    
    if resource_id.startswith("batch-"):
        # Batch status request
        try:
            batch_record = await batch_storage.get_batch_by_job_id(resource_id)
        except Exception as exc:
            logger.error("Database error fetching batch %s: %s", resource_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable. Please retry."
            ) from exc
        
        if not batch_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Batch not found: {resource_id}"
            )
        
        try:
            entries = await batch_storage.get_batch_entries(batch_record["id"])
        except Exception as exc:
            logger.error("Database error fetching batch entries for %s: %s", resource_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable. Please retry."
            ) from exc
        
        # Count statuses
        pending = sum(1 for e in entries if e["status"] == "pending")
        running = sum(1 for e in entries if e["status"] == "running")
        
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
            cancelled_calls=batch_record["cancelled_calls"],
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
    else:
        # Individual call status request
        try:
            call_record = await call_storage.get_call_by_id(resource_id)
        except Exception as exc:
            logger.error("Database error fetching call %s: %s", resource_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable. Please retry."
            ) from exc
        
        if not call_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call not found: {resource_id}"
            )
        
        batch_id = call_record.get("batch_id")
        
        return CallStatusResponse(
            call_log_id=str(call_record["id"]),
            status=call_record.get("status") or "unknown",
            call_duration=float(call_record["call_duration"]) if call_record.get("call_duration") else None,
            call_recording_url=call_record.get("call_recording_url"),
            transcriptions=call_record.get("transcriptions"),
            started_at=call_record.get("started_at"),
            ended_at=call_record.get("ended_at"),
            batch_id=str(batch_id) if batch_id else None,
            is_batch_call=batch_id is not None,
        )


# =============================================================================
# POST /calls/cancel - Cancel call(s) or stop batch(es)
# =============================================================================

async def _cancel_single_resource(
    resource_id: str, force: bool,
    call_storage, batch_storage,
) -> CancelResultItem:
    """Cancel a single resource (call or batch). Returns a CancelResultItem."""

    resource_id = resource_id.strip()

    if resource_id.startswith("batch-"):
        # Batch stop request
        batch_record = await batch_storage.get_batch_by_job_id(resource_id)
        if not batch_record:
            return CancelResultItem(
                resource_id=resource_id,
                resource_type="batch",
                status="not_found",
                cancelled_count=0,
                message=f"Batch not found: {resource_id}",
            )

        batch_id = batch_record["id"]
        current_status = batch_record.get("status")

        if current_status in ("completed", "cancelled", "failed"):
            return CancelResultItem(
                resource_id=resource_id,
                resource_type="batch",
                status=current_status,
                cancelled_count=0,
                message=f"Batch already in terminal state: {current_status}",
            )

        # Mark batch as cancelled
        await batch_storage.update_batch_status(batch_id, "cancelled")

        # Get running/dispatched entries FIRST (before marking them cancelled)
        running_entries = await batch_storage.get_running_entries_with_call_logs(batch_id)

        # Mark pending/queued entries as cancelled
        cancelled_count = await batch_storage.mark_pending_entries_cancelled(batch_id)
        if cancelled_count > 0:
            await batch_storage.increment_batch_counters(batch_id, cancelled_delta=cancelled_count)

        terminated_count = 0

        # If force=true, also terminate running calls
        if force and running_entries:
            from api.services.call_service import get_call_service
            call_service = get_call_service()

            for entry in running_entries:
                call_log_id = entry.get("call_log_id")
                room_name = entry.get("room_name")

                if room_name:
                    terminated = await call_service.terminate_call(room_name)
                    if terminated:
                        terminated_count += 1

                if call_log_id:
                    await call_storage.update_call_metadata(call_log_id, status="cancelled")
                await batch_storage.update_batch_entry_status(
                    str(entry["id"]), "cancelled"
                )

            if terminated_count > 0:
                await batch_storage.increment_batch_counters(batch_id, cancelled_delta=terminated_count)

        message = f"Batch cancelled. {cancelled_count} pending entries cancelled."
        if force and terminated_count > 0:
            message += f" {terminated_count} running call(s) forcefully terminated."
        elif running_entries and not force:
            message += f" {len(running_entries)} active call(s) will complete naturally. Use force=true to terminate them."

        # Trigger report if any calls completed successfully
        # On cancellation, we want a report even if not all entries are terminal
        result = await batch_storage.check_and_complete_batch(batch_id)
        report_triggered = False
        
        if result.get("should_report") or result.get("completed"):
            # All entries terminal — standard report trigger
            import asyncio
            from api.routes.batch import _generate_and_send_batch_report
            logger.info("Batch %s fully done after cancellation — triggering report", resource_id)
            asyncio.create_task(_generate_and_send_batch_report(batch_id))
            report_triggered = True
        else:
            # Not all entries terminal, but check if any calls completed
            batch_record = await batch_storage.get_batch_by_id(batch_id)
            completed_calls = batch_record.get("completed_calls", 0) if batch_record else 0
            if completed_calls > 0:
                import asyncio
                from api.routes.batch import _generate_and_send_batch_report
                logger.info(
                    "Batch %s cancelled with %d completed calls — triggering partial report",
                    resource_id, completed_calls,
                )
                asyncio.create_task(_generate_and_send_batch_report(batch_id))
                report_triggered = True

        if report_triggered:
            message += " Report generation triggered."

        return CancelResultItem(
            resource_id=resource_id,
            resource_type="batch",
            status="cancelled",
            cancelled_count=cancelled_count + terminated_count,
            message=message,
        )

    else:
        # Individual call cancel request
        call_record = await call_storage.get_call_by_id(resource_id)
        if not call_record:
            return CancelResultItem(
                resource_id=resource_id,
                resource_type="call",
                status="not_found",
                cancelled_count=0,
                message=f"Call not found: {resource_id}",
            )

        current_status = call_record.get("status")
        call_metadata = call_record.get("metadata") or {}
        room_name = call_metadata.get("room_name")

        terminal_statuses = {
            "ended", "completed", "failed", "declined", "rejected",
            "not_reachable", "no_answer", "busy", "error", "cancelled",
        }

        if current_status in terminal_statuses:
            return CancelResultItem(
                resource_id=resource_id,
                resource_type="call",
                status=current_status,
                cancelled_count=0,
                message=f"Call already in terminal state: {current_status}",
            )

        room_deleted = False
        if room_name:
            from api.services.call_service import get_call_service
            call_service = get_call_service()
            room_deleted = await call_service.terminate_call(room_name)

        await call_storage.update_call_metadata(resource_id, status="cancelled")

        batch_id = call_metadata.get("batch_id")
        if batch_id:
            entries = await batch_storage.get_batch_entries(str(batch_id))
            for entry in entries:
                if str(entry.get("call_log_id")) == resource_id:
                    await batch_storage.update_batch_entry_status(
                        str(entry["id"]), "cancelled"
                    )
                    await batch_storage.increment_batch_counters(str(batch_id), cancelled_delta=1)
                    break

        message = "Call cancelled."
        if room_deleted:
            message += " LiveKit room terminated."
        elif room_name:
            message += " Room may have already ended."

        logger.info(
            "Call %s cancelled: room=%s, room_deleted=%s, batch_id=%s",
            resource_id, room_name, room_deleted, batch_id,
        )

        return CancelResultItem(
            resource_id=resource_id,
            resource_type="call",
            status="cancelled",
            cancelled_count=1,
            message=message,
        )


@router.post("/cancel", response_model=CancelResponse)
async def cancel_call_or_batch(request: CancelRequest) -> CancelResponse:
    """
    Cancel one or more running calls / batches.

    Accepts a single resource_id (string) or a list of resource_ids.
    Each resource_id is a call log UUID or a batch job_id (prefixed 'batch-').
    """
    # Normalize to list
    ids = request.resource_id if isinstance(request.resource_id, list) else [request.resource_id]

    call_storage = _get_call_storage()
    batch_storage = _get_batch_storage()

    results: list[CancelResultItem] = []
    for rid in ids:
        result = await _cancel_single_resource(rid, request.force, call_storage, batch_storage)
        results.append(result)

    total = sum(r.cancelled_count for r in results)

    return CancelResponse(results=results, total_cancelled=total)

# =============================================================================
# POST /start-call - Trigger single outbound call
# =============================================================================

@router.post("/start-call", response_model=CallJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_single_call(payload: SingleCallPayload, request: Request) -> CallJobResponse:
    """
    Initiate a single outbound call.
    
    This endpoint:
    1. Resolves voice settings (supports 'default' with agent_id)
    2. Auto-assigns Glinks KB if applicable
    3. Creates call job in memory
    4. Dispatches call via LiveKit SIP
    5. Returns job ID for status tracking
    
    Args:
        payload: SingleCallPayload with call configuration
        request: FastAPI request for header access
    
    Returns:
        CallJobResponse with job_id and initial status
    """
    call_service = get_call_service()
    job_store = _get_job_store()
    
    try:
        # Resolve voice
        resolved_voice_id, voice_context = await call_service.resolve_voice(
            payload.voice_id, payload.agent_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Resolve LLM overrides
    llm_provider_override = _normalize_llm_provider(payload.llm_provider)
    llm_model_override = _clean_optional_text(payload.llm_model)
    
    # Resolve knowledge base store IDs - auto-assign Glinks default if applicable
    frontend_id = request.headers.get("X-Frontend-ID")
    resolved_kb_store_ids = payload.knowledge_base_store_ids
    
    if await call_service.should_use_glinks_kb(
        payload.agent_id,
        frontend_id,
        payload.knowledge_base_store_ids,
    ):
        resolved_kb_store_ids = DEFAULT_GLINKS_KB_STORE_IDS
        logger.info(
            "Auto-assigned default Glinks KB for agent_id=%s, frontend_id=%s",
            payload.agent_id,
            frontend_id,
        )
    
    # ===== VALIDATION: Check call routing BEFORE accepting the request =====
    # This ensures invalid calls (e.g., UAE number with India-only carrier) are rejected
    # with 400 error instead of 202 ACCEPTED then silent failure
    # NOTE: tenant_id is resolved later in dispatch_call - this is just format validation
    routing_result = validate_and_format_call(
        from_number=payload.from_number,
        to_number=payload.to_number,
        db_config=get_db_config(),
        tenant_id=None,  # Pre-dispatch validation; tenant_id resolved in dispatch_call
    )
    
    if not routing_result.success:
        logger.error(f"Call routing validation failed: {routing_result.error_message}")
        raise HTTPException(
            status_code=400,
            detail=routing_result.error_message
        )
    
    # Use the formatted number for dialing (after validation)
    dial_number = routing_result.formatted_to_number or payload.to_number
    logger.info(f"Call routing validated: {payload.to_number} -> {dial_number} (carrier: {routing_result.carrier_name})")
    
    # Generate job and dispatch
    job_id = uuid.uuid4().hex
    job = CallJob(
        job_id=job_id,
        mode=CallMode.SINGLE,
        voice_id=voice_context.db_voice_id or resolved_voice_id,
        tts_voice_id=voice_context.tts_voice_id,
        voice_provider=voice_context.provider,
        voice_name=voice_context.voice_name,
        llm_provider=llm_provider_override,
        llm_model=llm_model_override,
        initiated_by=payload.initiated_by,
        agent_id=payload.agent_id,
        from_number=payload.from_number,
        base_context=payload.added_context,
        results=[
            CallAttemptResult(
                to_number=payload.to_number,
                status=JobStatus.PENDING,
                index=0,
            )
        ],
    )
    await job_store.create(job)
    
    # Dispatch call asynchronously
    async def _dispatch_and_update():
        try:
            result = await call_service.dispatch_call(
                job_id=job_id,
                voice_id=resolved_voice_id,
                voice_context=voice_context,
                from_number=payload.from_number,
                to_number=payload.to_number,
                context=payload.added_context,
                initiated_by=payload.initiated_by,
                agent_id=payload.agent_id,
                llm_provider=llm_provider_override,
                llm_model=llm_model_override,
                knowledge_base_store_ids=resolved_kb_store_ids,
                lead_name=payload.lead_name,  # For lead creation/update
                lead_id_override=payload.lead_id,
            )
            
            # Update job with dispatch result
            await job_store.update(
                job_id,
                status=JobStatus.RUNNING,
            )
            
            # Update the result in the job
            job_updated = await job_store.get(job_id)
            if job_updated and job_updated.results:
                job_updated.results[0].status = JobStatus.RUNNING
                job_updated.results[0].room_name = result.room_name
                job_updated.results[0].dispatch_id = result.dispatch_id
                job_updated.results[0].call_log_id = result.call_log_id
                job_updated.results[0].lead_name = result.lead_name
                
        except Exception as exc:
            logger.exception("Call dispatch failed for job %s: %s", job_id, exc)
            await job_store.update(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
            )
            job_updated = await job_store.get(job_id)
            if job_updated and job_updated.results:
                job_updated.results[0].status = JobStatus.FAILED
                job_updated.results[0].error = str(exc)
    
    # Fire and forget the dispatch
    asyncio.create_task(_dispatch_and_update())
    
    return job_to_response(job)


# =============================================================================
# GET /job/{job_id} - Get job status
# =============================================================================

@router.get("/job/{job_id}", response_model=CallJobResponse)
async def get_job_status(job_id: str) -> CallJobResponse:
    """Get the status of a call job by ID."""
    job_store = _get_job_store()
    job = await job_store.get(job_id)
    
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}"
        )
    
    return job_to_response(job)


__all__ = ["router"]


"""
Cleanup Handler Module.

Handles post-call cleanup operations after a voice agent call ends.
Extracted from entry.py for modular architecture.

Phase 16: Updated for lad_dev schema:
- Added tenant_id to CleanupContext
- Column names: duration_seconds (not call_duration), transcripts (not transcriptions)

Responsibilities:
1. Stop call recording and get recording URL
2. Save transcription to database
3. Calculate call cost (component-based or duration-based)
4. Update call status (ended, failed, etc.)
5. Trigger post-call analysis
6. Stop background audio players
7. Release call semaphore
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# CLEANUP CONTEXT
# =============================================================================

class CleanupContext:
    """
    Context object holding all resources needed for cleanup.
    
    Passed to cleanup functions to avoid global state.
    """
    
    def __init__(
        self,
        call_recorder: Any = None,
        call_log_id: str | None = None,
        tenant_id: str | None = None,  # Phase 16: Multi-tenancy
        lead_id: str | None = None,  # For vertical routing
        call_storage: Any = None,
        silence_monitor: Any = None,
        latency_mask_player: Any = None,
        ambience_player: Any = None,
        background_audio_task: asyncio.Task | None = None,
        call_semaphore: asyncio.Semaphore | None = None,
        acquired_call_slot: bool = False,
        usage_collector: Any = None,
        job_id: str | None = None,
        batch_id: str | None = None,  # Batch call tracking
        entry_id: str | None = None,  # Batch entry tracking
        audit_trail: Any = None,  # Tool audit trail for metadata
        sip_trail: Any = None,  # SIP lifecycle trail for metadata
        from_number: str | None = None,  # For provider-based telephony costing
    ):
        self.call_recorder = call_recorder
        self.call_log_id = call_log_id
        self.tenant_id = tenant_id
        self.lead_id = lead_id
        self.call_storage = call_storage
        self.silence_monitor = silence_monitor
        self.latency_mask_player = latency_mask_player
        self.ambience_player = ambience_player
        self.background_audio_task = background_audio_task
        self.call_semaphore = call_semaphore
        self.acquired_call_slot = acquired_call_slot
        self.usage_collector = usage_collector
        self.job_id = job_id
        self.batch_id = batch_id
        self.entry_id = entry_id
        self.audit_trail = audit_trail
        self.sip_trail = sip_trail
        self.from_number = from_number


# =============================================================================
# RECORDING CLEANUP
# =============================================================================

async def stop_and_save_recording(ctx: CleanupContext) -> tuple[str | None, dict | None, float | None]:
    """
    Stop recording and retrieve artifacts.
    
    Args:
        ctx: Cleanup context with call_recorder
        
    Returns:
        Tuple of (recording_url, transcription_data, trimmed_duration)
    """
    call_recorder = ctx.call_recorder
    call_log_id = ctx.call_log_id
    
    recording_url: str | None = None
    transcription_data: dict | None = None
    trimmed_duration: float | None = None
    
    if not call_recorder or not call_log_id:
        return recording_url, transcription_data, trimmed_duration
    
    # Stop recording
    try:
        await call_recorder.stop_recording()
    except Exception as exc:
        logger.error("Failed to stop recording for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    # Get trimmed duration
    try:
        trimmed_duration = call_recorder.get_trimmed_duration_seconds()
        if trimmed_duration is not None:
            logger.info("Call audio duration after trimming: %.2fs for call_log_id=%s", trimmed_duration, call_log_id)
    except Exception as exc:
        logger.error("Failed to get trimmed duration for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    # Get recording URL
    try:
        recording_url = call_recorder.get_recording_url()
    except Exception as exc:
        logger.error("Failed to resolve recording URL for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    # Get transcription
    try:
        transcription_data = call_recorder.get_transcription_dict()
    except Exception as exc:
        logger.error("Failed to serialize transcription for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
        transcription_data = None
    
    return recording_url, transcription_data, trimmed_duration


async def save_recording_to_db(
    ctx: CleanupContext,
    recording_url: str | None,
    transcription_data: dict | None,
    trimmed_duration: float | None,
) -> None:
    """
    Persist recording and transcription to database.
    
    Args:
        ctx: Cleanup context with call_storage
        recording_url: GCS URL of recording
        transcription_data: Transcription dict
        trimmed_duration: Duration in seconds after trimming
    """
    call_storage = ctx.call_storage
    call_log_id = ctx.call_log_id
    
    if not call_storage or not call_log_id:
        return
    
    if recording_url:
        try:
            await call_storage.update_call_recording(
                call_log_id=call_log_id,
                recording_url=recording_url,
                transcripts=transcription_data,
            )
            
            if trimmed_duration is not None:
                await call_storage.update_call_metadata(
                    call_log_id,
                    duration_seconds=trimmed_duration,  # Phase 16: new column name
                )
                logger.info("Saved duration_seconds=%.2fs for call_log_id=%s", trimmed_duration, call_log_id)
            
            logger.info("Saved recording and transcription for call_log_id=%s", call_log_id)
        except Exception as exc:
            logger.error("Failed to persist recording for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    elif transcription_data:
        try:
            await call_storage.update_call_metadata(
                call_log_id,
                transcripts=transcription_data,  # Phase 16: new column name
            )
            logger.info("Saved transcripts without recording for call_log_id=%s", call_log_id)
        except Exception as exc:
            logger.error("Failed to persist transcripts for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    else:
        logger.warning("No recording or transcription captured for call_log_id=%s", call_log_id)


# =============================================================================
# AUDIT TRAIL
# =============================================================================

async def save_audit_trail(ctx: CleanupContext) -> None:
    """
    Save audit trail, SIP trail, and status_reason to metadata JSONB.
    
    Merges all trail data into existing metadata in a single DB write.
    Errors are logged but don't break the cleanup flow.
    
    Args:
        ctx: Cleanup context with audit_trail and sip_trail
    """
    if not ctx.call_log_id or not ctx.call_storage:
        return
    if not ctx.audit_trail and not ctx.sip_trail:
        return
    
    try:
        # Get existing call data to merge metadata
        call = await ctx.call_storage.get_call_by_id(ctx.call_log_id)
        if not call:
            logger.warning("Cannot save trails - call not found: %s", ctx.call_log_id)
            return
        
        # Get existing metadata or empty dict
        existing_metadata = call.get("metadata") or {}
        if isinstance(existing_metadata, str):
            import json
            try:
                existing_metadata = json.loads(existing_metadata)
            except json.JSONDecodeError:
                existing_metadata = {}
        
        # 1. Merge audit_trail
        if ctx.audit_trail:
            existing_metadata["audit_trail"] = ctx.audit_trail.to_dict()
        
        # 2. Merge sip_trail
        if ctx.sip_trail:
            sip_data = ctx.sip_trail.to_dict()
            if sip_data:
                existing_metadata["sip_trail"] = sip_data
        
        # 3. Resolve and set status_reason
        from utils.sip_trail import resolve_status_reason
        existing_status = call.get("status")
        status_reason = resolve_status_reason(
            audit_trail=ctx.audit_trail,
            sip_trail=ctx.sip_trail,
            existing_status=existing_status,
        )
        existing_metadata["status_reason"] = status_reason
        
        # Update metadata (single write)
        await ctx.call_storage.update_call_metadata(
            ctx.call_log_id,
            metadata=existing_metadata
        )
        
        audit_count = len(ctx.audit_trail.events) if ctx.audit_trail else 0
        sip_count = len(ctx.sip_trail.events) if ctx.sip_trail else 0
        logger.info(
            "Trails saved: call_log_id=%s, audit_events=%d, sip_events=%d, status_reason=%s",
            ctx.call_log_id, audit_count, sip_count, status_reason
        )
    except Exception as exc:
        # Non-blocking - log error but don't raise
        logger.error(
            "Failed to save trails for call_log_id=%s: %s",
            ctx.call_log_id,
            exc,
            exc_info=True
        )

# =============================================================================
# COST CALCULATION
# =============================================================================

def _resolve_telephony_provider(
    from_number: str | None,
    tenant_id: str | None = None,
) -> str:
    """
    Resolve the telephony provider for a from_number by looking up
    voice_agent_numbers.provider and matching against billing_pricing_catalog.
    
    Falls back to 'vonage' if no match found.
    """
    if not from_number:
        logger.debug("No from_number provided, defaulting telephony provider to 'vonage'")
        return "vonage"
    
    try:
        from db.storage.numbers import NumberStorage
        number_storage = NumberStorage()
        provider = number_storage.get_provider_by_phone(from_number, tenant_id)
        
        if provider:
            # Normalize to lowercase for pricing catalog matching
            normalized = provider.strip().lower()
            logger.info(f"Resolved telephony provider for {from_number[:4]}***: raw='{provider}', normalized='{normalized}'")
            return normalized
        else:
            logger.info(f"No provider found for {from_number[:4]}***, defaulting to 'vonage'")
            return "vonage"
    except Exception as e:
        logger.warning(f"Error resolving telephony provider for {from_number[:4]}***: {e}, defaulting to 'vonage'")
        return "vonage"


async def calculate_and_save_cost(
    ctx: CleanupContext,
    duration_seconds: float | None,
) -> tuple[float | None, list | None]:
    """
    Calculate call cost using component-based or duration-based method.
    
    Args:
        ctx: Cleanup context with usage_collector
        duration_seconds: Call duration in seconds
        
    Returns:
        Tuple of (call_cost, cost_breakdown)
    """
    from utils.usage_tracker import calculate_call_cost
    
    call_cost: float | None = None
    cost_breakdown: list | None = None
    
    usage_collector = ctx.usage_collector
    call_storage = ctx.call_storage
    call_log_id = ctx.call_log_id
    
    duration_hours = duration_seconds / 3600.0 if duration_seconds else None
    
    # Mode 1: Component-based cost tracking
    if usage_collector and not usage_collector.is_empty():
        logger.info(f"UsageCollector state: is_empty={usage_collector.is_empty()}, summary={usage_collector.get_summary()}")
        
        if duration_seconds and duration_seconds > 0:
            # Resolve telephony provider from from_number -> voice_agent_numbers
            telephony_provider = _resolve_telephony_provider(ctx.from_number, ctx.tenant_id)
            usage_collector.add_telephony_seconds(duration_seconds, provider=telephony_provider)
            usage_collector.add_vm_infrastructure_seconds(duration_seconds, provider="digitalocean")
        
        try:
            usage_summary = usage_collector.get_summary()
            if usage_summary and call_storage and call_log_id:
                pricing_rates = await call_storage.get_pricing_rates()
                await call_storage.save_call_usage(
                    call_log_id=str(call_log_id),
                    usage_records=usage_summary,
                    pricing_rates=pricing_rates,
                )
                
                total_cost, cost_breakdown = await calculate_call_cost(
                    usage_records=usage_summary,
                    pricing_rates=pricing_rates,
                )
                
                logger.info("=" * 60)
                logger.info("COMPONENT-BASED COST BREAKDOWN")
                logger.info("=" * 60)
                
                for item in cost_breakdown:
                    component = item["component"]
                    provider = item["provider"]
                    model = item["model"]
                    amount = item["amount"]
                    unit = item["unit"]
                    cost = item["cost"]
                    rate = item["rate"]
                    
                    if rate is not None:
                        logger.info(
                            f"  {component.upper()} ({provider}/{model}): "
                            f"{amount:.4f} {unit} × ${rate:.8f} = ${cost:.6f}"
                        )
                    else:
                        logger.warning(
                            f"  {component.upper()} ({provider}/{model}): "
                            f"{amount:.4f} {unit} - NO PRICING RATE"
                        )
                
                if total_cost > 0:
                    call_cost = float(total_cost)
                    logger.info(f"TOTAL COST: ${call_cost:.6f}")
        except Exception as exc:
            logger.warning(f"Failed to calculate component-based cost: {exc}", exc_info=True)
    
    # Mode 2: Duration-based cost (fallback)
    if call_cost is None and duration_hours is not None:
        cost_per_hour = float(os.getenv("CALL_COST_PER_HOUR", "0.10"))
        call_cost = duration_hours * cost_per_hour
        logger.info(f"Duration-based cost: {duration_seconds:.2f}s = ${call_cost:.4f}")
    
    return call_cost, cost_breakdown


def determine_final_status(existing_status: str | None) -> str:
    """
    Determine final call status based on existing status.
    
    Args:
        existing_status: Current call status
        
    Returns:
        Final status string (standardized: in_queue, ringing, ongoing, ended, declined, cancelled, failed)
    """
    # Standardized status values:
    # - in_queue: call created, waiting to be dialed
    # - ringing: call is dialing/ringing recipient
    # - ongoing: call is active, conversation happening
    # - ended: call completed normally
    # - declined: call was rejected/declined by recipient (no_answer, busy, rejected)
    # - cancelled: call was cancelled via cancel endpoint
    # - failed: technical failure or unreachable
    
    # Map synonyms to standardized statuses
    declined_synonyms = {"declined", "rejected", "no_answer", "busy"}
    failed_synonyms = {"failed", "error", "not_reachable"}
    cancelled_synonyms = {"cancelled", "canceled"}  # Keep cancelled separate from failed
    ongoing_synonyms = {"ongoing", "in_progress", "running", "started"}
    ringing_synonyms = {"ringing"}
    in_queue_synonyms = {"in_queue", "pending", "queued"}
    
    if existing_status in declined_synonyms:
        return "declined"
    if existing_status in failed_synonyms:
        return "failed"
    if existing_status in cancelled_synonyms:
        return "cancelled"  # Keep as cancelled, not mapped to failed
    if existing_status in ringing_synonyms:
        return "ended"  # Was ringing, call ended (unanswered goes to declined above)
    if existing_status in ongoing_synonyms:
        return "ended"  # Was ongoing, now ending
    if existing_status in in_queue_synonyms:
        return "ended"  # Was queued, now ending
    return existing_status or "ended"


async def update_call_status(
    ctx: CleanupContext,
    ended_at: datetime,
    call_cost: float | None,
    cost_breakdown: list | None,
) -> None:
    """
    Update call status in database.
    
    Args:
        ctx: Cleanup context
        ended_at: When call ended
        call_cost: Calculated cost
        cost_breakdown: Cost details
    """
    call_storage = ctx.call_storage
    call_log_id = ctx.call_log_id
    
    if not call_storage or not call_log_id:
        return
    
    try:
        call_details = await call_storage.get_call_by_id(call_log_id)
    except Exception as exc:
        logger.error("Failed to load call details for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
        call_details = None
    
    existing_status = None
    if call_details:
        raw_status = call_details.get('status')
        if isinstance(raw_status, str) and raw_status.strip():
            existing_status = raw_status.strip().lower()
    
    final_status = determine_final_status(existing_status)
    
    try:
        await call_storage.update_call_status(
            call_log_id=call_log_id,
            status=final_status,
            ended_at=ended_at,
            cost=call_cost,
            cost_breakdown=cost_breakdown,
        )
    except Exception as exc:
        logger.error("Failed to update call status for call_log_id=%s: %s", call_log_id, exc, exc_info=True)


# =============================================================================
# POST-CALL ANALYSIS
# =============================================================================

async def trigger_post_call_analysis(
    ctx: CleanupContext,
    transcription_data: dict | None,
    duration_seconds: float | None,
    call_details: dict | None,
) -> None:
    """
    Run post-call analysis on transcription.
    
    Args:
        ctx: Cleanup context
        transcription_data: Transcription dict
        duration_seconds: Call duration
        call_details: Full call record
    """
    from analysis import run_post_call_analysis
    
    call_log_id = ctx.call_log_id
    call_storage = ctx.call_storage
    tenant_id = ctx.tenant_id  # Multi-tenancy support
    lead_id = ctx.lead_id  # For vertical routing
    
    if not transcription_data:
        logger.warning("Skipping post-call analysis; no transcript for call_log_id=%s", call_log_id)
        return
    
    try:
        await run_post_call_analysis(
            call_log_id=str(call_log_id),
            transcription_json=transcription_data,
            duration_seconds=duration_seconds,
            call_details=call_details or {},
            db_config=getattr(call_storage, "db_config", None),
            tenant_id=str(tenant_id) if tenant_id else None,  # Pass tenant_id
            lead_id=str(lead_id) if lead_id else None,  # Pass lead_id for vertical routing
        )
    except Exception as exc:
        logger.error("Post-call analysis failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)


# =============================================================================
# LEAD BOOKINGS EXTRACTION
# =============================================================================

async def trigger_lead_bookings_extraction(
    ctx: CleanupContext,
    transcription_data: dict | None,
    call_details: dict | None,
) -> None:
    """
    Extract and save lead bookings from call transcription.
    
    Args:
        ctx: Cleanup context
        transcription_data: Transcription dict
        call_details: Full call record
    """
    try:
        from analysis.lead_bookings_extractor import LeadBookingsExtractor
    except ImportError:
        logger.warning("Lead bookings extractor not available, skipping extraction")
        return
    
    call_log_id = ctx.call_log_id
    
    if not transcription_data:
        logger.warning("Skipping lead bookings extraction; no transcript for call_log_id=%s", call_log_id)
        return
    
    if not call_log_id:
        logger.warning("Skipping lead bookings extraction; no call_log_id")
        return
    
    try:
        extractor = LeadBookingsExtractor()
        try:
            booking_data = await extractor.process_call_log(str(call_log_id))
            if booking_data:
                save_results = await extractor.save_booking(booking_data)
                if save_results.get("db"):
                    logger.info("Lead booking extracted and saved for call_log_id=%s", call_log_id)
                elif save_results.get("errors"):
                    logger.warning("Lead booking extraction completed with errors for call_log_id=%s: %s",
                                 call_log_id, save_results["errors"])
            else:
                logger.debug("No booking data extracted for call_log_id=%s", call_log_id)
        finally:
            await extractor.close()
    except Exception as exc:
        logger.error("Lead bookings extraction failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)


# =============================================================================
# SUBMIT ANALYSIS TO MAIN API (STABLE PROCESS)
# =============================================================================

async def _submit_analysis_to_main_api(
    call_log_id: str,
    transcription_data: dict | None,
    duration_seconds: float | None,
    call_details: dict | None,
    tenant_id: str | None,
    lead_id: str | None,
) -> None:
    """
    Submit analysis request to the main API process.
    
    The main API runs as a stable systemctl service that doesn't exit after
    each call. By offloading analysis to that process, we ensure analysis
    completes even after the worker process exits.
    
    This is fire-and-forget from the worker's perspective - we don't wait
    for analysis to complete, just for the request to be accepted.
    
    Args:
        call_log_id: The call log ID to analyze
        transcription_data: Transcription dict (can be empty for failed calls)
        duration_seconds: Call duration
        call_details: Full call record
        tenant_id: Tenant ID for multi-tenancy
        lead_id: Lead ID for vertical routing
    """
    import os
    import json
    import httpx
    from datetime import datetime, date
    from decimal import Decimal
    from uuid import UUID
    
    def json_serial(obj):
        """JSON serializer for objects not serializable by default.
        
        Handles common types from database records:
        - datetime/date → ISO format string
        - Decimal → float
        - UUID → string
        - bytes → base64 string
        - set → list
        """
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, bytes):
            import base64
            return base64.b64encode(obj).decode('utf-8')
        if isinstance(obj, set):
            return list(obj)
        # For any other unknown type, convert to string as fallback
        try:
            return str(obj)
        except Exception:
            raise TypeError(f"Type {type(obj)} not serializable")
    
    # Get main API URL from environment (same as batch completion uses)
    main_api_url = os.getenv("MAIN_API_BASE_URL", "http://localhost:8000")
    endpoint = f"{main_api_url}/analysis/run-background-analysis"
    
    # Prepare request payload
    payload = {
        "call_log_id": str(call_log_id),
        "transcription_data": transcription_data or {},
        "duration_seconds": duration_seconds,
        "call_details": call_details,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "lead_id": str(lead_id) if lead_id else None,
    }
    
    # Await the request to ensure it's sent before worker exits
    # This fixes the issue where single call analysis was never triggered
    # because the event loop exited before the fire-and-forget task ran
    logger.info("Sending analysis request to endpoint: %s", endpoint)
    
    await _fire_analysis_request(
        endpoint=endpoint,
        payload_json=json.dumps(payload, default=json_serial),
        internal_frontend_id=os.getenv("INTERNAL_FRONTEND_ID", "dev"),
        internal_api_key=os.getenv("INTERNAL_API_KEY", os.getenv("DEV_API_KEY", "")),
        call_log_id=call_log_id,
    )


async def _fire_analysis_request(
    endpoint: str,
    payload_json: str,
    internal_frontend_id: str,
    internal_api_key: str,
    call_log_id: str,
) -> None:
    """
    Actually send the HTTP request to main API. This runs as a background task
    so the worker cleanup can continue without waiting.
    """
    import httpx
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                endpoint,
                content=payload_json,
                headers={
                    "Content-Type": "application/json",
                    "X-Frontend-ID": internal_frontend_id,
                    "X-API-Key": internal_api_key,
                }
            )
            
            if response.status_code == 200:
                logger.debug(
                    "Analysis request accepted by main API: call_log_id=%s",
                    call_log_id
                )
            else:
                logger.error(
                    "Main API rejected analysis request: call_log_id=%s, status=%d, response=%s",
                    call_log_id, response.status_code, response.text[:500]
                )
    except httpx.TimeoutException:
        # Timeout is acceptable - the request was likely received
        logger.warning(
            "Timeout submitting analysis to main API (request likely received): call_log_id=%s",
            call_log_id
        )
    except Exception as exc:
        # Log but don't fail - analysis is optional
        logger.error(
            "Failed to submit analysis to main API: call_log_id=%s, error=%s",
            call_log_id, exc, exc_info=True
        )


# =============================================================================
# BACKGROUND ANALYSIS RUNNER (FIRE-AND-FORGET)
# =============================================================================

async def _run_analysis_background(
    ctx: CleanupContext,
    transcription_data: dict | None,
    duration_seconds: float | None,
    call_details: dict | None,
) -> None:
    """
    Run all post-call analysis tasks in background.
    
    This is a fire-and-forget task that runs independently of cleanup.
    It handles its own errors and saves results to DB.
    
    Includes:
    1. Post-call sentiment/summary analysis (saves to voice_call_analysis)
    2. Lead bookings extraction (saves to lead_bookings)
    
    Args:
        ctx: Cleanup context (immutable after cleanup completes)
        transcription_data: Transcription dict
        duration_seconds: Call duration
        call_details: Full call record
    """
    call_log_id = ctx.call_log_id
    
    try:
        # 1. Run post-call analysis (sentiment, summary, lead score, etc.)
        await trigger_post_call_analysis(ctx, transcription_data, duration_seconds, call_details)
        logger.info("Post-call analysis completed for call_log_id=%s", call_log_id)
    except Exception as exc:
        logger.error("Background post-call analysis failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    try:
        # 2. Extract and save lead bookings
        await trigger_lead_bookings_extraction(ctx, transcription_data, call_details)
        logger.info("Lead bookings extraction completed for call_log_id=%s", call_log_id)
    except Exception as exc:
        logger.error("Background lead bookings extraction failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    logger.info("All background analysis tasks completed for call_log_id=%s", call_log_id)


# =============================================================================
# AUDIO CLEANUP
# =============================================================================

async def stop_background_audio(ctx: CleanupContext) -> None:
    """Stop all background audio players and tasks."""
    ambient_logger = logging.getLogger("ambient.audio")
    
    # Cancel background audio task
    if ctx.background_audio_task and not ctx.background_audio_task.done():
        ctx.background_audio_task.cancel()
        try:
            await ctx.background_audio_task
        except asyncio.CancelledError:
            pass
    
    # Stop audio players
    for player in (ctx.latency_mask_player, ctx.ambience_player):
        if player:
            stop_callable = getattr(player, "stop", None)
            if callable(stop_callable):
                try:
                    maybe_coro = stop_callable()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                except Exception:
                    ambient_logger.warning("Failed to stop background audio player", exc_info=True)


# =============================================================================
# MAIN CLEANUP FUNCTION
# =============================================================================

async def cleanup_and_save(ctx: CleanupContext) -> None:
    """
    Main cleanup handler called when call ends.
    
    Performs all cleanup operations in order:
    1. Disable silence monitoring
    2. Stop recording and get artifacts
    3. Save recording to database
    4. Calculate and save cost
    5. Update call status
    6. Run post-call analysis
    7. Stop background audio
    8. Release semaphore
    
    Args:
        ctx: Cleanup context with all resources
    """
    # 1. Disable silence monitoring
    if ctx.silence_monitor:
        ctx.silence_monitor.disable()
    
    # 2. Stop recording and get artifacts
    recording_url, transcription_data, trimmed_duration = await stop_and_save_recording(ctx)
    
    # 3. Save recording to database
    await save_recording_to_db(ctx, recording_url, transcription_data, trimmed_duration)
    
    # 3.5. Save audit trail to metadata (non-blocking)
    await save_audit_trail(ctx)
    
    # Get call details for cost calculation
    call_details = None
    duration_seconds = None
    ended_at = datetime.now(timezone.utc)
    
    if ctx.call_storage and ctx.call_log_id:
        try:
            call_details = await ctx.call_storage.get_call_by_id(ctx.call_log_id)
        except Exception as exc:
            logger.error("Failed to load call details: %s", exc, exc_info=True)
        
        if call_details and call_details.get('started_at'):
            started_at = call_details['started_at']
            duration_seconds = (ended_at - started_at).total_seconds()
    
    # 4. Calculate and save cost
    call_cost, cost_breakdown = await calculate_and_save_cost(ctx, duration_seconds)
    
    # 5. Update call status
    await update_call_status(ctx, ended_at, call_cost, cost_breakdown)
    
    # Log transcription
    if ctx.call_recorder:
        try:
            logger.info("Call transcription:\n%s", ctx.call_recorder.get_transcription_text())
        except Exception:
            pass
    
    # 6. Stop background audio (quick operation)
    await stop_background_audio(ctx)
    
    # 7. Release semaphore FIRST - worker can now accept new calls
    # Analysis submission will continue in parallel with any new calls
    if ctx.acquired_call_slot and ctx.call_semaphore:
        ctx.call_semaphore.release()
        logger.info("Released semaphore for job %s - worker now available", ctx.job_id)
    
    # 8. Run post-call analysis (via Main API)
    # Worker is already available for new calls - this runs in parallel
    # We await to ensure the HTTP request is sent before cleanup exits
    await _submit_analysis_to_main_api(
        call_log_id=ctx.call_log_id,
        transcription_data=transcription_data,
        duration_seconds=duration_seconds,
        call_details=call_details,
        tenant_id=ctx.tenant_id,
        lead_id=ctx.lead_id,
    )
    logger.info("Post-call analysis submitted to main API for call_log_id=%s", ctx.call_log_id)
    
    # 9. Update batch entry status and check for batch completion
    # Use the same final_status that was written to the call log
    call_final_status = "ended"  # default
    if ctx.call_storage and ctx.call_log_id:
        try:
            updated_call = await ctx.call_storage.get_call_by_id(ctx.call_log_id)
            if updated_call and updated_call.get('status'):
                call_final_status = updated_call['status']
        except Exception:
            pass
    await update_batch_on_call_complete(ctx, call_final_status)

# =============================================================================
# BATCH COMPLETION TRACKING
# =============================================================================

async def update_batch_on_call_complete(ctx: CleanupContext, final_status: str) -> None:
    """
    Notify main.py that a batch entry call has completed.
    
    Main.py handles the entry status update, batch completion check, and report generation.
    This architecture ensures report generation runs in main.py's stable event loop,
    not in the worker which terminates after call ends.
    
    Args:
        ctx: Cleanup context with batch_id and entry_id
        final_status: The final status of the call (ended, failed, etc.)
    """
    if not ctx.batch_id or not ctx.entry_id:
        # Not a batch call
        return
    
    try:
        import httpx
        import os
        
        # Get main.py URL (same host, different or same port)
        main_api_base = os.getenv("MAIN_API_BASE_URL", "http://localhost:8000")
        url = f"{main_api_base}/batch/entry-completed"
        
        payload = {
            "batch_id": ctx.batch_id,
            "entry_id": ctx.entry_id,
            "call_status": final_status,
        }
        
        logger.info(f"Notifying main server of batch entry completion: batch={ctx.batch_id}, entry={ctx.entry_id}")
        
        # Retry with exponential backoff (2s, 4s, 8s) to handle transient failures
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(url, json=payload)
                    
                    if response.status_code == 200:
                        result = response.json()
                        logger.info(
                            f"Batch entry {ctx.entry_id} completed. "
                            f"Batch completed: {result.get('batch_completed')}, "
                            f"Report triggered: {result.get('report_triggered')}"
                        )
                        return  # Success
                    else:
                        logger.error(
                            f"Failed to notify main server of batch completion: "
                            f"status={response.status_code}, body={response.text[:200]} "
                            f"(attempt {attempt}/{max_retries})"
                        )
            except Exception as e:
                logger.error(
                    f"Error notifying main server of batch completion (attempt {attempt}/{max_retries}): {e}",
                    exc_info=(attempt == max_retries),
                )
            
            if attempt < max_retries:
                backoff = 2 ** attempt  # 2s, 4s
                import asyncio
                await asyncio.sleep(backoff)
    
    except Exception as e:
        logger.error(f"Error in batch completion notification setup: {e}", exc_info=True)


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "CleanupContext",
    "cleanup_and_save",
    "stop_and_save_recording",
    "save_recording_to_db",
    "calculate_and_save_cost",
    "determine_final_status",
    "update_call_status",
    "trigger_post_call_analysis",
    "trigger_lead_bookings_extraction",
    "stop_background_audio",
    "update_batch_on_call_complete",
]



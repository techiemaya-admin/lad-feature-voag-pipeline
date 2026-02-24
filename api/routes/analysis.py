"""
Analysis Routes - Background analysis execution endpoint.

This endpoint allows worker processes to offload analysis execution to the
main API process, which is long-running and stable. This ensures analysis
survives worker process shutdown.
"""

import asyncio
import logging
import concurrent.futures
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis")

# Thread pool for running analysis without blocking the event loop
# This allows the endpoint to return immediately while analysis runs in background
_analysis_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="analysis")


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class RunAnalysisRequest(BaseModel):
    """Request to run post-call analysis in the main API process."""
    call_log_id: str
    transcription_data: dict
    duration_seconds: Optional[float] = None
    call_details: Optional[dict] = None
    tenant_id: Optional[str] = None
    lead_id: Optional[str] = None
    # Database config is fetched from env in main API, not passed
    

class RunAnalysisResponse(BaseModel):
    """Response confirming analysis was queued."""
    status: str
    call_log_id: str
    message: str


# =============================================================================
# BACKGROUND ANALYSIS RUNNER (in Main API process)
# =============================================================================

def _run_analysis_sync(
    call_log_id: str,
    transcription_data: dict,
    duration_seconds: Optional[float],
    call_details: Optional[dict],
    tenant_id: Optional[str],
    lead_id: Optional[str],
) -> None:
    """
    Run post-call analysis synchronously (called from thread pool).
    
    This runs in a separate thread to avoid blocking the main event loop.
    The analysis will complete even if the worker process that initiated it exits.
    """
    import asyncio
    
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(_run_analysis_async(
            call_log_id=call_log_id,
            transcription_data=transcription_data,
            duration_seconds=duration_seconds,
            call_details=call_details,
            tenant_id=tenant_id,
            lead_id=lead_id,
        ))
    finally:
        loop.close()


async def _run_analysis_async(
    call_log_id: str,
    transcription_data: dict,
    duration_seconds: Optional[float],
    call_details: Optional[dict],
    tenant_id: Optional[str],
    lead_id: Optional[str],
) -> None:
    """
    Run post-call analysis and lead bookings extraction.
    
    This runs in a thread pool's event loop, separate from the main API event loop.
    """
    logger.debug("Starting background analysis for call_log_id=%s", call_log_id)
    
    # Get database config from environment (main API has its own DB connection)
    import os
    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "database": os.getenv("DB_NAME", "salesmaya_agent"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD"),
    }
    
    # 1. Run post-call analysis
    try:
        from analysis import run_post_call_analysis
        
        if transcription_data:
            logger.debug("Running post-call analysis for call_log_id=%s", call_log_id)
            success = await run_post_call_analysis(
                call_log_id=str(call_log_id),
                transcription_json=transcription_data,
                duration_seconds=duration_seconds,
                call_details=call_details or {},
                db_config=db_config,
                tenant_id=str(tenant_id) if tenant_id else None,
                lead_id=str(lead_id) if lead_id else None,
            )
            if success:
                logger.debug("Post-call analysis completed successfully for call_log_id=%s", call_log_id)
            else:
                logger.warning("Post-call analysis returned False for call_log_id=%s", call_log_id)
        else:
            logger.debug("Skipping post-call analysis; no transcript for call_log_id=%s", call_log_id)
    except Exception as exc:
        logger.error("Post-call analysis failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    # 2. Run lead bookings extraction
    try:
        from analysis.lead_bookings_extractor import LeadBookingsExtractor
        
        extractor = LeadBookingsExtractor()
        try:
            logger.debug("Running lead bookings extraction for call_log_id=%s", call_log_id)
            booking_data = await extractor.process_call_log(str(call_log_id))
            if booking_data:
                save_results = await extractor.save_booking(booking_data)
                if save_results.get("db"):
                    logger.debug("Lead booking extracted and saved for call_log_id=%s", call_log_id)
                elif save_results.get("errors"):
                    logger.warning(
                        "Lead booking extraction completed with errors for call_log_id=%s: %s",
                        call_log_id, save_results["errors"]
                    )
            else:
                logger.debug("No booking data extracted for call_log_id=%s", call_log_id)
        finally:
            await extractor.close()
    except ImportError:
        logger.debug("Lead bookings extractor not available, skipping extraction")
    except Exception as exc:
        logger.error("Lead bookings extraction failed for call_log_id=%s: %s", call_log_id, exc, exc_info=True)
    
    logger.info("Analysis completed and saved to DB for call_log_id=%s", call_log_id)


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.post("/run-background-analysis", response_model=RunAnalysisResponse)
async def run_background_analysis(
    request: RunAnalysisRequest,
    background_tasks: BackgroundTasks,
) -> RunAnalysisResponse:
    """
    Queue post-call analysis to run in background thread pool.
    
    This endpoint returns IMMEDIATELY after queuing. Analysis runs in a
    separate thread pool, not blocking the main event loop.
    
    Args:
        request: Analysis request with call data and transcription
        background_tasks: FastAPI background tasks manager
        
    Returns:
        Confirmation that analysis was queued (returns in <100ms)
    """
    logger.info("Analysis request received for call_log_id=%s", request.call_log_id)
    
    # Submit to thread pool - returns immediately, doesn't block event loop
    _analysis_executor.submit(
        _run_analysis_sync,
        call_log_id=request.call_log_id,
        transcription_data=request.transcription_data,
        duration_seconds=request.duration_seconds,
        call_details=request.call_details,
        tenant_id=request.tenant_id,
        lead_id=request.lead_id,
    )
    
    return RunAnalysisResponse(
        status="queued",
        call_log_id=request.call_log_id,
        message="Analysis queued for background execution in thread pool",
    )

"""
Knowledge Base Routes Module (V2 API).

Handles knowledge base (RAG) endpoints:
- POST /knowledge-base/stores: Create store
- GET /knowledge-base/stores: List stores
- GET /knowledge-base/stores/{store_id}: Get store
- DELETE /knowledge-base/stores/{store_id}: Delete store
- POST /knowledge-base/stores/{store_id}/documents: Upload document
- GET /knowledge-base/stores/{store_id}/documents: List documents
- GET /knowledge-base/stores/{store_id}/stats: Get storage stats
- Agent-store linking endpoints
- Lead-store linking endpoints
"""

import logging
import os
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from db.storage import KnowledgeBaseStorage
from utils.kb_cache import kb_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-base")

# =============================================================================
# GEMINI FILE SEARCH STORAGE LIMITS
# =============================================================================
# Official limits: https://ai.google.dev/gemini-api/docs/file-search#rate-limits

# Maximum file size per document
MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024  # 100 MB

# Total storage limits by tier
STORAGE_LIMITS_GB = {
    "free": 1,       # 1 GB
    "tier_1": 10,    # 10 GB
    "tier_2": 100,   # 100 GB
    "tier_3": 1000,  # 1 TB
}

# Recommended store size for optimal latency
RECOMMENDED_STORE_SIZE_GB = 20

# Lazy initialization
_kb_storage: KnowledgeBaseStorage | None = None


def _get_kb_storage() -> KnowledgeBaseStorage:
    global _kb_storage
    if _kb_storage is None:
        _kb_storage = KnowledgeBaseStorage()
    return _kb_storage


def _check_file_search_enabled() -> None:
    """Check if file search feature is enabled."""
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Knowledge base feature not available - GOOGLE_API_KEY or GEMINI_API_KEY not configured"
        )


# =============================================================================
# STATUS ENDPOINT
# =============================================================================

@router.get("/status", response_model=dict)
async def knowledge_base_status() -> dict:
    """
    Check knowledge base (file search) feature status.
    
    Returns:
        Status info including whether feature is enabled
    """
    enabled = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    
    return {
        "enabled": enabled,
        "feature": "knowledge_base",
        "requires": "GOOGLE_API_KEY or GEMINI_API_KEY environment variable",
        "message": "Knowledge base is ready" if enabled else "Knowledge base is disabled",
    }


class GeminiStoreItem(BaseModel):
    """Individual store from Gemini API with optional DB metadata."""
    name: str
    display_name: str
    size_bytes: int = 0
    size_mb: float = 0
    active_documents: int = 0
    pending_documents: int = 0
    failed_documents: int = 0
    # DB metadata (if linked to our system)
    db_id: str | None = None  # Our DB UUID if linked
    tenant_id: str | None = None
    description: str | None = None  # Description from our DB
    is_default: bool = False
    is_linked: bool = False  # True if this Gemini store is in our DB
    priority: int = 0


class GeminiStoresListResponse(BaseModel):
    """Response for listing all Gemini stores."""
    total_stores: int
    total_size_bytes: int
    total_size_gb: float
    stores: list[GeminiStoreItem]
    storage_limits_gb: dict
    warnings: list[str] = []


# Document response models (needed by gemini-stores endpoints)
class KnowledgeBaseDocumentResponse(BaseModel):
    name: str
    display_name: str
    state: str | None = None


class KnowledgeBaseUploadResponse(BaseModel):
    document: KnowledgeBaseDocumentResponse
    message: str


@router.get("/gemini-stores", response_model=GeminiStoresListResponse)
async def list_all_gemini_stores() -> GeminiStoresListResponse:
    """
    List ALL File Search stores from Gemini API with DB metadata merged.
    
    Shows all stores in the Gemini account. For stores that are linked to our DB,
    includes tenant_id, is_default, priority, etc. Unlinked stores can be attached
    to tenants via the UI.
    """
    _check_file_search_enabled()
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        client = file_search._ensure_client()
        
        # Fetch all DB stores to merge metadata
        kb_storage = _get_kb_storage()
        db_stores = await kb_storage.list_stores(active_only=False)
        
        # Create lookup by gemini_store_name -> DB store
        db_lookup = {s["gemini_store_name"]: s for s in db_stores}
        
        stores_list = []
        total_size = 0
        
        for store in client.file_search_stores.list():
            size = int(getattr(store, 'size_bytes', 0) or 0)
            total_size += size
            
            # Get cached document count
            cache_key = store.name.replace("fileSearchStores/", "")
            cached = kb_cache.get_store_stats(cache_key)
            cached_doc_count = cached["document_count"] if cached else 0
            
            # Use cached count if available, otherwise fall back to API count
            active_docs = cached_doc_count if cached else int(getattr(store, 'active_documents_count', 0) or 0)
            
            # Check if this Gemini store is in our DB
            db_store = db_lookup.get(store.name)
            
            stores_list.append(GeminiStoreItem(
                name=store.name,
                display_name=getattr(store, 'display_name', store.name),
                size_bytes=size,
                size_mb=round(size / (1024 * 1024), 2),
                active_documents=active_docs,
                pending_documents=int(getattr(store, 'pending_documents_count', 0) or 0),
                failed_documents=int(getattr(store, 'failed_documents_count', 0) or 0),
                # DB metadata if linked
                db_id=str(db_store["id"]) if db_store else None,
                tenant_id=db_store.get("tenant_id") if db_store else None,
                description=db_store.get("description") if db_store else None,
                is_default=db_store.get("is_default", False) if db_store else False,
                is_linked=db_store is not None,
                priority=db_store.get("priority", 0) if db_store else 0,
            ))
        
        total_gb = total_size / (1024 * 1024 * 1024)
        
        # Generate warnings
        warnings = []
        if total_gb > 0.8:  # 80% of free tier
            warnings.append(f"Approaching free tier limit (1 GB). Current: {total_gb:.2f} GB")
        
        empty_stores = sum(1 for s in stores_list if s.size_bytes == 0)
        if empty_stores > 5:
            warnings.append(f"{empty_stores} empty stores detected. Consider cleanup.")
        
        unlinked = sum(1 for s in stores_list if not s.is_linked)
        if unlinked > 0:
            warnings.append(f"{unlinked} stores not linked to any tenant.")
        
        return GeminiStoresListResponse(
            total_stores=len(stores_list),
            total_size_bytes=total_size,
            total_size_gb=round(total_gb, 4),
            stores=stores_list,
            storage_limits_gb=STORAGE_LIMITS_GB,
            warnings=warnings,
        )
    except Exception as e:
        logger.error(f"Failed to list Gemini stores: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gemini-stores/{store_name}/documents", response_model=list[KnowledgeBaseDocumentResponse])
async def list_gemini_store_documents(store_name: str) -> list[KnowledgeBaseDocumentResponse]:
    """
    List all documents in a Gemini store by its short name.
    
    Args:
        store_name: The Gemini store short name (e.g., 'glinkscorrected-crmt20009kci')
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        docs = await file_search.list_documents(gemini_store_name)
        
        # Sync cache
        doc_names = [d.name for d in docs]
        kb_cache.set_store_stats(store_name, gemini_store_name, len(docs), doc_names)
        
        return [
            KnowledgeBaseDocumentResponse(
                name=d.name,
                display_name=d.display_name,
                state=d.state,
            )
            for d in docs
        ]
    except Exception as e:
        logger.error(f"Failed to list documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/gemini-stores/{store_name}/documents/{doc_id:path}")
async def delete_gemini_store_document(store_name: str, doc_id: str) -> dict:
    """
    Delete a document from a Gemini store by short name.
    
    Args:
        store_name: The Gemini store short name
        doc_id: Document ID (just the doc ID part, not full path)
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    # Handle both full path and just doc ID
    if "/documents/" in doc_id:
        doc_id = doc_id.split("/documents/")[-1]
    
    full_doc_name = f"{gemini_store_name}/documents/{doc_id}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        await file_search.delete_document(full_doc_name)
        
        # Update cache
        kb_cache.update_on_delete_doc(store_name, full_doc_name)
        
        logger.info(f"Deleted document {doc_id} from store {store_name}")
        
        return {
            "message": "Document deleted successfully",
            "document_name": doc_id,
            "store_name": store_name,
        }
    except Exception as e:
        logger.error(f"Failed to delete document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gemini-stores/{store_name}/documents", response_model=KnowledgeBaseUploadResponse)
async def upload_gemini_store_document(
    store_name: str,
    file: UploadFile = File(...),
    display_name: str | None = Form(None),
) -> KnowledgeBaseUploadResponse:
    """
    Upload a document to a Gemini store by its short name.
    
    Args:
        store_name: The Gemini store short name
        file: The file to upload
        display_name: Optional display name for the document
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        file_bytes = await file.read()
        
        # Check file size limit (100 MB)
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            file_size_mb = len(file_bytes) / (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"File too large: {file_size_mb:.1f} MB exceeds maximum {MAX_FILE_SIZE_MB} MB limit"
            )
        
        doc_info = await file_search.upload_document_from_bytes(
            store_name=gemini_store_name,
            file_bytes=file_bytes,
            filename=file.filename or "document",
            display_name=display_name or file.filename,
        )
        
        # Update cache
        kb_cache.update_on_upload(store_name, doc_info.name)
        
        logger.info(f"Uploaded document '{doc_info.display_name}' to store {store_name}")
        
        return KnowledgeBaseUploadResponse(
            document=KnowledgeBaseDocumentResponse(
                name=doc_info.name,
                display_name=doc_info.display_name,
                state=doc_info.state,
            ),
            message="Document uploaded successfully",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/gemini-stores/{store_name}")
async def delete_gemini_store(store_name: str) -> dict:
    """
    Delete a Gemini store by its short name.
    
    Args:
        store_name: The Gemini store short name
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        await file_search.delete_store(gemini_store_name)
        
        # Update cache
        kb_cache.update_on_delete_store(store_name)
        
        logger.info(f"Deleted Gemini store {store_name}")
        
        return {
            "message": "Store deleted successfully",
            "store_name": store_name,
        }
    except Exception as e:
        logger.error(f"Failed to delete store: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class LinkStoreRequest(BaseModel):
    """Request to link a Gemini store to a tenant."""
    tenant_id: str = Field(..., description="Tenant UUID to link this store to")
    display_name: str | None = Field(None, description="Display name for the store")
    description: str | None = Field(None, max_length=1000, description="Description of the KB")
    is_default: bool = Field(False, description="Set as default store for tenant")
    priority: int = Field(0, description="Priority when multiple stores")


@router.post("/gemini-stores/{store_name}/link")
async def link_gemini_store_to_tenant(
    store_name: str,
    payload: LinkStoreRequest,
) -> dict:
    """
    Link an existing Gemini store to a tenant in our database.
    
    This creates a DB record for an unlinked Gemini store, allowing it to be
    used with specific tenants, set as default, etc.
    
    Args:
        store_name: The Gemini store short name
        payload: Link configuration (tenant_id, is_default, priority)
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        client = file_search._ensure_client()
        
        # Verify store exists in Gemini
        gemini_store = None
        for store in client.file_search_stores.list():
            if store.name == gemini_store_name:
                gemini_store = store
                break
        
        if not gemini_store:
            raise HTTPException(status_code=404, detail=f"Gemini store not found: {store_name}")
        
        # Check if already linked
        kb_storage = _get_kb_storage()
        existing = await kb_storage.get_store_by_gemini_name(gemini_store_name)
        if existing:
            raise HTTPException(status_code=409, detail=f"Store already linked (ID: {existing['id']})")
        
        # Create DB record
        display_name = getattr(gemini_store, 'display_name', store_name)
        store_id = await kb_storage.create_store(
            tenant_id=payload.tenant_id,
            gemini_store_name=gemini_store_name,
            display_name=payload.display_name or display_name,
            description=payload.description,
            is_default=payload.is_default,
            priority=payload.priority,
            created_by=None,
        )
        
        logger.info(f"Linked Gemini store {store_name} to tenant {payload.tenant_id} (DB ID: {store_id})")
        
        return {
            "message": "Store linked successfully",
            "store_name": store_name,
            "db_id": str(store_id),
            "tenant_id": payload.tenant_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to link store: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class UpdateSettingsRequest(BaseModel):
    """Request to update KB settings."""
    tenant_id: str = Field(..., description="Tenant UUID")
    display_name: str | None = Field(None, description="Display name for the store")
    description: str | None = Field(None, max_length=1000)
    is_default: bool = Field(False, description="Set as default store for tenant")
    is_active: bool = Field(True, description="Whether the store is active")
    priority: int = Field(0, description="Priority when multiple stores")


@router.put("/gemini-stores/{store_name}/settings")
async def update_gemini_store_settings(
    store_name: str,
    payload: UpdateSettingsRequest,
) -> dict:
    """
    Update settings for a linked Gemini store.
    
    Args:
        store_name: The Gemini store short name
        payload: Updated settings
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        kb_storage = _get_kb_storage()
        existing = await kb_storage.get_store_by_gemini_name(gemini_store_name)
        
        if not existing:
            raise HTTPException(status_code=404, detail=f"Store not linked to database: {store_name}")
        
        # Update the store
        await kb_storage.update_store(
            store_id=existing["id"],
            display_name=payload.display_name or existing.get("display_name"),
            description=payload.description,
            is_active=payload.is_active,
            is_default=payload.is_default,
            priority=payload.priority,
        )
        
        
        logger.info(f"Updated settings for store {store_name}")
        
        return {
            "message": "Settings updated successfully",
            "store_name": store_name,
            "db_id": str(existing["id"]),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update store settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/gemini-stores/{store_name}/unlink")
async def unlink_gemini_store(store_name: str) -> dict:
    """
    Unlink a Gemini store from the database.
    
    This removes the DB record but keeps the Gemini store intact.
    
    Args:
        store_name: The Gemini store short name
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    try:
        kb_storage = _get_kb_storage()
        existing = await kb_storage.get_store_by_gemini_name(gemini_store_name)
        
        if not existing:
            raise HTTPException(status_code=404, detail=f"Store not linked: {store_name}")
        
        # Delete the DB record (soft delete by setting is_active=False, or hard delete)
        await kb_storage.delete_store(existing["id"])
        
        logger.info(f"Unlinked store {store_name} from database")
        
        return {
            "message": "Store unlinked successfully",
            "store_name": store_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to unlink store: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class CreateStoreRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant UUID that owns this store")
    display_name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)
    is_default: bool = Field(False, description="Auto-attach to tenant's calls")
    priority: int = Field(0, description="Higher = preferred when multiple stores")


class KnowledgeBaseStoreResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    gemini_store_name: str
    display_name: str
    description: str | None = None
    is_default: bool = False
    is_active: bool = True
    priority: int = 0
    document_count: int = 0
    created_at: Any = None
    updated_at: Any = None


# Note: AgentStoreLink and LeadStoreLink are deprecated - use tenant_id instead


# =============================================================================
# STORE CRUD ROUTES
# =============================================================================
@router.post("/stores", response_model=KnowledgeBaseStoreResponse)
async def create_knowledge_base_store(
    payload: CreateStoreRequest,
    request: Request,
) -> KnowledgeBaseStoreResponse:
    """Create a new knowledge base store linked to a tenant."""
    _check_file_search_enabled()
    
    # Get optional user_id for created_by
    created_by: str | None = None
    try:
        user_id_header = request.headers.get("X-User-ID")
        if user_id_header:
            created_by = user_id_header.strip()  # UUID string
    except (ValueError, TypeError):
        pass
    
    kb_storage = _get_kb_storage()
    
    try:
        # Create Gemini FileSearchStore first
        from tools.file_search_tool import FileSearchTool
        file_search = FileSearchTool()
        gemini_store = await file_search.create_store(payload.display_name)
        gemini_store_name = gemini_store.name
        
        # Create DB record
        store_id = await kb_storage.create_store(
            tenant_id=payload.tenant_id,
            gemini_store_name=gemini_store_name,
            display_name=payload.display_name,
            description=payload.description,
            is_default=payload.is_default,
            priority=payload.priority,
            created_by=created_by,
        )
        
        store_record = await kb_storage.get_store_by_id(store_id)
        if not store_record:
            raise HTTPException(status_code=500, detail="Failed to retrieve created store")
        
        # Update cache using gemini short-name as key
        cache_key = gemini_store_name.replace("fileSearchStores/", "")
        kb_cache.update_on_create_store(cache_key, gemini_store_name)
        
        logger.info(f"Created KB store '{payload.display_name}' for tenant {payload.tenant_id}")
        
        return KnowledgeBaseStoreResponse(
            id=str(store_record["id"]),
            tenant_id=store_record.get("tenant_id"),
            gemini_store_name=store_record["gemini_store_name"],
            display_name=store_record["display_name"],
            description=store_record.get("description"),
            is_default=store_record.get("is_default", False),
            is_active=store_record.get("is_active", True),
            priority=store_record.get("priority", 0),
            document_count=store_record.get("document_count", 0),
            created_at=store_record.get("created_at"),
            updated_at=store_record.get("updated_at"),
        )
    except Exception as e:
        logger.error(f"Failed to create store: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stores", response_model=list[KnowledgeBaseStoreResponse])
async def list_knowledge_base_stores(
    tenant_id: str | None = None,
    active_only: bool = True,
) -> list[KnowledgeBaseStoreResponse]:
    """List knowledge base stores, optionally filtered by tenant."""
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    stores = await kb_storage.list_stores(
        tenant_id=tenant_id,
        active_only=active_only,
    )
    
    result = []
    for s in stores:
        store_id = str(s["id"])
        gemini_store_name = s["gemini_store_name"]
        
        # Prefer cached document count if available (try both DB id and gemini name)
        cached = kb_cache.get_store_stats(store_id) or kb_cache.get_by_gemini_name(gemini_store_name)
        doc_count = cached["document_count"] if cached else s.get("document_count", 0)
        
        result.append(KnowledgeBaseStoreResponse(
            id=store_id,
            tenant_id=s.get("tenant_id"),
            gemini_store_name=s["gemini_store_name"],
            display_name=s["display_name"],
            description=s.get("description"),
            is_default=s.get("is_default", False),
            is_active=s.get("is_active", True),
            priority=s.get("priority", 0),
            document_count=doc_count,
            created_at=s.get("created_at"),
            updated_at=s.get("updated_at"),
        ))
    
    return result


@router.get("/stores/{store_id}", response_model=KnowledgeBaseStoreResponse)
async def get_knowledge_base_store(store_id: str) -> KnowledgeBaseStoreResponse:
    """Get a specific knowledge base store by ID."""
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    return KnowledgeBaseStoreResponse(
        id=str(store["id"]),
        tenant_id=store.get("tenant_id"),
        gemini_store_name=store["gemini_store_name"],
        display_name=store["display_name"],
        description=store.get("description"),
        is_default=store.get("is_default", False),
        is_active=store.get("is_active", True),
        priority=store.get("priority", 0),
        document_count=store.get("document_count", 0),
        created_at=store.get("created_at"),
        updated_at=store.get("updated_at"),
    )


@router.delete("/stores/{store_id}", response_model=dict)
async def delete_knowledge_base_store(
    store_id: str,
    delete_gemini_store: bool = True,
) -> dict:
    """Delete a knowledge base store."""
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    deleted = await kb_storage.delete_store(store_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete store record")
    
    # Update cache using gemini short-name as key
    gemini_store_name = store["gemini_store_name"]
    cache_key = gemini_store_name.replace("fileSearchStores/", "")
    kb_cache.update_on_delete_store(cache_key)
    
    return {"message": "Store deleted successfully", "store_id": store_id}


class StoreStatsResponse(BaseModel):
    """Storage statistics response model."""
    store_id: str
    gemini_store_name: str
    display_name: str
    size_bytes: int
    size_mb: float
    size_gb: float
    active_documents: int
    pending_documents: int
    failed_documents: int
    total_documents: int
    create_time: str | None = None
    update_time: str | None = None
    warnings: list[str] = []
    limits: dict = {}


@router.get("/stores/{store_id}/stats", response_model=StoreStatsResponse)
async def get_store_stats(store_id: str) -> StoreStatsResponse:
    """
    Get storage statistics for a knowledge base store.
    
    Returns size usage, document counts, and limit warnings from Gemini API.
    
    Limits:
    - Max file size: 100 MB per document
    - Total storage: 1GB (free) to 1TB (tier 3)
    - Recommended store size: 20 GB for optimal latency
    """
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    store = await kb_storage.get_store_by_id(store_id)
    
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    gemini_store_name = store["gemini_store_name"]
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        stats = await file_search.get_store_stats(gemini_store_name)
        
        if not stats:
            raise HTTPException(status_code=500, detail="Failed to get store stats from Gemini")
        
        # Calculate sizes
        size_mb = stats.size_bytes / (1024 * 1024)
        size_gb = stats.size_bytes / (1024 * 1024 * 1024)
        
        # Generate warnings
        warnings = []
        if size_gb > RECOMMENDED_STORE_SIZE_GB:
            warnings.append(f"Store size ({size_gb:.2f} GB) exceeds recommended {RECOMMENDED_STORE_SIZE_GB} GB for optimal latency")
        if stats.failed_documents_count > 0:
            warnings.append(f"{stats.failed_documents_count} document(s) failed processing")
        if stats.pending_documents_count > 0:
            warnings.append(f"{stats.pending_documents_count} document(s) still processing")
        
        return StoreStatsResponse(
            store_id=store_id,
            gemini_store_name=gemini_store_name,
            display_name=stats.display_name,
            size_bytes=stats.size_bytes,
            size_mb=round(size_mb, 2),
            size_gb=round(size_gb, 4),
            active_documents=stats.active_documents_count,
            pending_documents=stats.pending_documents_count,
            failed_documents=stats.failed_documents_count,
            total_documents=stats.active_documents_count + stats.pending_documents_count + stats.failed_documents_count,
            create_time=stats.create_time,
            update_time=stats.update_time,
            warnings=warnings,
            limits={
                "max_file_size_mb": MAX_FILE_SIZE_MB,
                "recommended_store_size_gb": RECOMMENDED_STORE_SIZE_GB,
                "storage_tiers_gb": STORAGE_LIMITS_GB,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get store stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# DOCUMENT UPLOAD ROUTES
# =============================================================================

@router.post("/stores/{store_id}/documents", response_model=KnowledgeBaseUploadResponse)
async def upload_document_to_store(
    store_id: str,
    file: UploadFile = File(...),
    display_name: str | None = Form(None),
) -> KnowledgeBaseUploadResponse:
    """
    Upload a document to a knowledge base store.
    
    Supported formats: PDF, TXT, HTML, MD, DOC, DOCX, PPT, PPTX, XLS, XLSX, 
    ODT, ODS, ODP, CSV, JSON, XML
    
    Limits:
    - Maximum file size: 100 MB
    """
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    
    # Verify store exists
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    gemini_store_name = store["gemini_store_name"]
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        
        # Read file content
        file_bytes = await file.read()
        
        # Check file size limit (100 MB)
        file_size_mb = len(file_bytes) / (1024 * 1024)
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large: {file_size_mb:.1f} MB exceeds maximum {MAX_FILE_SIZE_MB} MB limit"
            )
        
        # Upload to Gemini
        doc_info = await file_search.upload_document_from_bytes(
            store_name=gemini_store_name,
            file_bytes=file_bytes,
            filename=file.filename or "document",
            display_name=display_name or file.filename,
        )
        
        # Update document count in DB
        current_count = store.get("document_count", 0)
        await kb_storage.update_store(store_id, document_count=current_count + 1)
        
        # Update cache using gemini short-name as key
        cache_key = gemini_store_name.replace("fileSearchStores/", "")
        kb_cache.update_on_upload(cache_key, doc_info.name)
        
        logger.info(f"Uploaded document '{doc_info.display_name}' to store {store_id}")
        
        return KnowledgeBaseUploadResponse(
            document=KnowledgeBaseDocumentResponse(
                name=doc_info.name,
                display_name=doc_info.display_name,
                state=doc_info.state,
            ),
            message="Document uploaded successfully",
        )
    except Exception as e:
        logger.error(f"Failed to upload document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stores/{store_id}/documents", response_model=list[KnowledgeBaseDocumentResponse])
async def list_store_documents(store_id: str) -> list[KnowledgeBaseDocumentResponse]:
    """List all documents in a knowledge base store."""
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    gemini_store_name = store["gemini_store_name"]
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        docs = await file_search.list_documents(gemini_store_name)
        
        # Sync cache with actual document list from Gemini
        # Use gemini short name as cache key for consistency
        doc_names = [d.name for d in docs]
        cache_key = gemini_store_name.replace("fileSearchStores/", "")
        kb_cache.set_store_stats(cache_key, gemini_store_name, len(docs), doc_names)
        
        return [
            KnowledgeBaseDocumentResponse(
                name=d.name,
                display_name=d.display_name,
                state=d.state,
            )
            for d in docs
        ]
    except Exception as e:
        logger.error(f"Failed to list documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stores/{store_id}/documents/{doc_name:path}", response_model=KnowledgeBaseDocumentResponse)
async def get_store_document(store_id: str, doc_name: str) -> KnowledgeBaseDocumentResponse:
    """
    Get a specific document from a knowledge base store.
    
    Args:
        store_id: The store UUID
        doc_name: Document name (just the doc ID part, not full path)
    """
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    gemini_store_name = store["gemini_store_name"]
    
    # Handle both full path and just doc ID
    if "/documents/" in doc_name:
        doc_id = doc_name.split("/documents/")[-1]
    else:
        doc_id = doc_name
    
    # Build full document name
    full_doc_name = f"{gemini_store_name}/documents/{doc_id}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        doc = await file_search.get_document(full_doc_name)
        
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        
        return KnowledgeBaseDocumentResponse(
            name=doc.name,
            display_name=doc.display_name,
            state=doc.state,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/stores/{store_id}/documents/{doc_name:path}")
async def delete_store_document(store_id: str, doc_name: str) -> dict:
    """
    Delete a specific document from a knowledge base store.
    
    Args:
        store_id: The store UUID
        doc_name: Document name (just the doc ID part, not full path)
        
    Returns:
        Success message
    """
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    
    store = await kb_storage.get_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    
    gemini_store_name = store["gemini_store_name"]
    
    # Handle both full path and just doc ID
    # If doc_name is already full path like "fileSearchStores/.../documents/doc-id"
    # Extract just the doc-id part
    if "/documents/" in doc_name:
        # Already a full or partial path, extract just the doc ID
        doc_id = doc_name.split("/documents/")[-1]
    else:
        doc_id = doc_name
    
    # Build full document name
    full_doc_name = f"{gemini_store_name}/documents/{doc_id}"
    
    try:
        from tools.file_search_tool import FileSearchTool
        
        file_search = FileSearchTool()
        await file_search.delete_document(full_doc_name)
        
        # Update document count in DB
        try:
            current_count = store.get("document_count", 0)
            if current_count > 0:
                await kb_storage.update_document_count(store_id, -1)  # Decrement
        except Exception as e:
            logger.warning(f"Failed to update document count: {e}")
        
        # Update cache using gemini short-name as key
        cache_key = gemini_store_name.replace("fileSearchStores/", "")
        kb_cache.update_on_delete_doc(cache_key, full_doc_name)
        
        logger.info(f"Deleted document {doc_name} from store {store_id}")
        
        return {
            "message": "Document deleted successfully",
            "document_name": doc_name,
            "store_id": store_id,
        }
    except Exception as e:
        logger.error(f"Failed to delete document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# TENANT-BASED STORE RETRIEVAL
# =============================================================================

@router.get("/tenants/{tenant_id}/stores", response_model=list[KnowledgeBaseStoreResponse])
async def get_tenant_stores(
    tenant_id: str,
    default_only: bool = True,
) -> list[KnowledgeBaseStoreResponse]:
    """Get all knowledge base stores for a tenant."""
    _check_file_search_enabled()
    
    kb_storage = _get_kb_storage()
    stores = await kb_storage.get_stores_for_tenant(tenant_id, default_only=default_only)
    
    return [
        KnowledgeBaseStoreResponse(
            id=str(s["id"]),
            tenant_id=s.get("tenant_id"),
            gemini_store_name=s["gemini_store_name"],
            display_name=s["display_name"],
            description=s.get("description"),
            is_default=s.get("is_default", False),
            is_active=True,  # Only active stores are returned
            priority=s.get("priority", 0),
            document_count=s.get("document_count", 0),
        )
        for s in stores
    ]


# =============================================================================
# STORE CHAT ENDPOINT (For Chat Widget)
# =============================================================================

class StoreChatRequest(BaseModel):
    """Request body for store-specific chat."""
    question: str = Field(..., min_length=1, max_length=2000, description="Question to ask")
    model: str = Field("gemini-2.5-flash", description="Gemini model to use")


class StoreChatResponse(BaseModel):
    """Response from store chat."""
    answer: str
    sources: list[str] = []


@router.post("/gemini-stores/{store_name}/chat", response_model=StoreChatResponse)
async def chat_with_store(store_name: str, request: StoreChatRequest) -> StoreChatResponse:
    """
    Chat with a specific Gemini store.
    
    This endpoint queries a single store directly, useful for the chat widget.
    
    Args:
        store_name: The Gemini store short name (e.g., 'glinkscorrected-crmt20009kci')
        request: Contains question and optional model
        
    Returns:
        Answer text and sources cited
    """
    _check_file_search_enabled()
    
    gemini_store_name = f"fileSearchStores/{store_name}"
    
    logger.info(f"Chat query to store {store_name}: {request.question[:50]}...")
    
    try:
        from google import genai
        from google.genai import types
        
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)
        
        response = client.models.generate_content(
            model=request.model,
            contents=request.question,
            config=types.GenerateContentConfig(
                tools=[types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[gemini_store_name]
                    )
                )]
            )
        )
        
        # Extract answer
        answer = response.text if response.text else "(No response generated)"
        
        # Extract sources from grounding metadata
        sources = []
        if response.candidates and response.candidates[0].grounding_metadata:
            grounding = response.candidates[0].grounding_metadata
            if grounding.grounding_chunks:
                for chunk in grounding.grounding_chunks:
                    if hasattr(chunk, 'retrieved_context') and chunk.retrieved_context:
                        title = getattr(chunk.retrieved_context, 'title', None)
                        if title and title not in sources:
                            sources.append(title)
        
        return StoreChatResponse(
            answer=answer,
            sources=sources,
        )
        
    except Exception as e:
        logger.error(f"Store chat failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Chat query failed: {str(e)}"
        )


# =============================================================================
# QUERY ENDPOINT (Testing)
# =============================================================================

class KBQueryRequest(BaseModel):
    """Request body for KB query."""
    tenant_id: str = Field(..., description="Tenant UUID to query KB stores for")
    question: str = Field(..., min_length=1, max_length=2000, description="Question to ask")
    model: str = Field("gemini-2.5-flash", description="Gemini model to use")


class KBQueryResponse(BaseModel):
    """Response from KB query."""
    answer: str
    sources: list[str] = []
    store_names: list[str] = []


@router.post("/query", response_model=KBQueryResponse)
async def query_knowledge_base(request: KBQueryRequest) -> KBQueryResponse:
    """
    Query the knowledge base for a tenant.
    
    This endpoint retrieves default KB stores for the tenant and queries
    Gemini with file search grounding to get an answer.
    
    Args:
        request: Contains tenant_id, question, and optional model
        
    Returns:
        Answer text and sources cited
    """
    _check_file_search_enabled()
    
    # Get KB stores for tenant
    kb_storage = _get_kb_storage()
    stores = await kb_storage.get_stores_for_tenant(request.tenant_id, default_only=False)
    
    if not stores:
        raise HTTPException(
            status_code=404,
            detail=f"No knowledge base stores found for tenant {request.tenant_id}"
        )
    
    # Get Gemini store names
    gemini_store_names = [s["gemini_store_name"] for s in stores if s.get("gemini_store_name")]
    
    if not gemini_store_names:
        raise HTTPException(
            status_code=404,
            detail="No Gemini stores linked to tenant's KB stores"
        )
    
    logger.info(f"Querying KB with {len(gemini_store_names)} store(s) for tenant {request.tenant_id}")
    
    # Query Gemini with file search
    try:
        from google import genai
        from google.genai import types
        
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)
        
        response = client.models.generate_content(
            model=request.model,
            contents=request.question,
            config=types.GenerateContentConfig(
                tools=[types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=gemini_store_names
                    )
                )]
            )
        )
        
        # Extract answer
        answer = response.text if response.text else "(No response generated)"
        
        # Extract sources from grounding metadata
        sources = []
        if response.candidates and response.candidates[0].grounding_metadata:
            grounding = response.candidates[0].grounding_metadata
            if grounding.grounding_chunks:
                for chunk in grounding.grounding_chunks:
                    if hasattr(chunk, 'retrieved_context') and chunk.retrieved_context:
                        title = getattr(chunk.retrieved_context, 'title', None)
                        if title and title not in sources:
                            sources.append(title)
        
        return KBQueryResponse(
            answer=answer,
            sources=sources,
            store_names=gemini_store_names,
        )
        
    except Exception as e:
        logger.error(f"KB query failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )


# =============================================================================
# BULK UPLOAD ENDPOINT
# =============================================================================

class BulkUploadRequest(BaseModel):
    """Request body for bulk upload."""
    folder_path: str = Field(..., description="Absolute path to folder containing files to upload")


class BulkUploadResult(BaseModel):
    """Result of a single file upload."""
    filename: str
    success: bool
    display_name: str | None = None
    error: str | None = None


class BulkUploadResponse(BaseModel):
    """Response from bulk upload."""
    store_id: str
    total_files: int
    successful: int
    failed: int
    results: list[BulkUploadResult]


# Supported file extensions for KB upload
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".pdf", ".html", ".htm", ".json", ".xml",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".csv"
}


@router.post("/stores/{store_id}/bulk-upload", response_model=BulkUploadResponse)
async def bulk_upload_documents(
    store_id: str,
    request: BulkUploadRequest,
) -> BulkUploadResponse:
    """
    Bulk upload all supported files from a folder to a KB store.
    
    Uses wave-based processing (5 files per wave) to prevent API timeouts.
    
    Supported formats: txt, md, pdf, html, json, xml, xlsx, xls, docx, doc, pptx, ppt, csv
    
    Args:
        store_id: The KB store UUID
        request: Contains folder_path to upload from
        
    Returns:
        Summary of upload results
    """
    import asyncio
    
    _check_file_search_enabled()
    
    folder_path = request.folder_path
    WAVE_SIZE = 5  # Files per wave
    WAVE_DELAY = 2.0  # Seconds between waves
    
    # Validate folder exists
    if not os.path.exists(folder_path):
        raise HTTPException(
            status_code=400,
            detail=f"Folder not found: {folder_path}"
        )
    
    if not os.path.isdir(folder_path):
        raise HTTPException(
            status_code=400,
            detail=f"Path is not a directory: {folder_path}"
        )
    
    # Get store info
    kb_storage = _get_kb_storage()
    store = await kb_storage.get_store_by_id(store_id)
    
    if not store:
        raise HTTPException(
            status_code=404,
            detail=f"Store not found: {store_id}"
        )
    
    gemini_store_name = store.get("gemini_store_name")
    if not gemini_store_name:
        raise HTTPException(
            status_code=400,
            detail="Store has no Gemini store linked"
        )
    
    # Get all supported files
    files_to_upload = []
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            ext = os.path.splitext(filename)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                files_to_upload.append((filename, file_path))
    
    if not files_to_upload:
        raise HTTPException(
            status_code=400,
            detail=f"No supported files found in {folder_path}. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
    
    total_files = len(files_to_upload)
    total_waves = (total_files + WAVE_SIZE - 1) // WAVE_SIZE
    
    logger.info(f"Bulk uploading {total_files} files in {total_waves} waves (wave size: {WAVE_SIZE})")
    
    # Upload in waves
    from tools.file_search_tool import FileSearchTool
    file_search = FileSearchTool()
    
    results = []
    successful = 0
    failed = 0
    
    for wave_num in range(total_waves):
        wave_start = wave_num * WAVE_SIZE
        wave_end = min(wave_start + WAVE_SIZE, total_files)
        wave_files = files_to_upload[wave_start:wave_end]
        
        logger.info(f"Wave {wave_num + 1}/{total_waves}: uploading {len(wave_files)} files")
        
        for filename, file_path in wave_files:
            try:
                # Use filename without extension as display name
                display_name = os.path.splitext(filename)[0]
                
                doc_info = await file_search.upload_document(
                    store_name=gemini_store_name,
                    file_path=file_path,
                    display_name=display_name,
                    wait_for_completion=True,  # Wait for each file in wave
                    timeout=120.0,  # 2 min timeout per file
                )
                
                results.append(BulkUploadResult(
                    filename=filename,
                    success=True,
                    display_name=display_name,
                ))
                successful += 1
                logger.info(f"Uploaded: {filename}")
                
            except Exception as e:
                results.append(BulkUploadResult(
                    filename=filename,
                    success=False,
                    error=str(e)[:200],
                ))
                failed += 1
                logger.error(f"Failed to upload {filename}: {e}")
        
        # Delay between waves (except last wave)
        if wave_num < total_waves - 1:
            logger.debug(f"Wave {wave_num + 1} complete, waiting {WAVE_DELAY}s before next wave")
            await asyncio.sleep(WAVE_DELAY)
    
    logger.info(f"Bulk upload complete: {successful} succeeded, {failed} failed")
    
    # Update document count in DB
    try:
        await kb_storage.update_document_count(store_id, successful)
    except Exception as e:
        logger.warning(f"Failed to update document count: {e}")
    
    return BulkUploadResponse(
        store_id=store_id,
        total_files=total_files,
        successful=successful,
        failed=failed,
        results=results,
    )


__all__ = ["router"]


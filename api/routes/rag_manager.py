"""
RAG Management UI Route

Serves a single-page application for managing Knowledge Base stores and documents.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/rag")


@router.get("/manage", response_class=HTMLResponse)
async def rag_management_page():
    """Serve the RAG Management UI."""
    template_path = Path(__file__).parent.parent / "templates" / "rag_manager.html"
    return HTMLResponse(content=template_path.read_text(encoding="utf-8"))

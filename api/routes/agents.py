"""
Agent Routes Module (V2 API).

Handles voice agent CRUD endpoints:
- GET /voice-agents: List agents
- POST /voice-agents: Create agent
- GET /voice-agents/{agent_id}: Get agent
- PUT /voice-agents/{agent_id}: Update agent
- DELETE /voice-agents/{agent_id}: Delete agent
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from db.storage import AgentStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents")

# Lazy initialization
_agent_storage: AgentStorage | None = None


def _get_agent_storage() -> AgentStorage:
    global _agent_storage
    if _agent_storage is None:
        _agent_storage = AgentStorage()
    return _agent_storage


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class AgentCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)
    voice_id: str | None = None
    instructions: str | None = None
    is_active: bool = True


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)
    voice_id: str | None = None
    instructions: str | None = None
    is_active: bool | None = None


class AgentResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    voice_id: str | None = None
    instructions: str | None = None
    is_active: bool = True
    created_at: Any = None
    updated_at: Any = None


# =============================================================================
# CRUD ROUTES
# =============================================================================

@router.get("/voice-agents", response_model=list[AgentResponse])
async def list_agents(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(True),
) -> list[AgentResponse]:
    """
    List all voice agents.
    
    Query params:
    - limit: Max records (default 50, max 100)
    - offset: Pagination offset
    - active_only: Only return active agents
    """
    agent_storage = _get_agent_storage()
    
    agents = await agent_storage.list_agents(
        limit=limit,
        offset=offset,
        active_only=active_only,
    )
    
    return [
        AgentResponse(
            id=a["id"],
            name=a["name"],
            description=a.get("description"),
            voice_id=str(a["voice_id"]) if a.get("voice_id") else None,
            instructions=a.get("instructions"),
            is_active=a.get("is_active", True),
            created_at=a.get("created_at"),
            updated_at=a.get("updated_at"),
        )
        for a in agents
    ]


@router.get("/voice-agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: int) -> AgentResponse:
    """Get a specific voice agent by ID."""
    agent_storage = _get_agent_storage()
    
    agent = await agent_storage.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )
    
    return AgentResponse(
        id=agent["id"],
        name=agent["name"],
        description=agent.get("description"),
        voice_id=str(agent["voice_id"]) if agent.get("voice_id") else None,
        instructions=agent.get("instructions"),
        is_active=agent.get("is_active", True),
        created_at=agent.get("created_at"),
        updated_at=agent.get("updated_at"),
    )


@router.post("/voice-agents", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(payload: AgentCreateRequest) -> AgentResponse:
    """Create a new voice agent."""
    agent_storage = _get_agent_storage()
    
    try:
        agent_id = await agent_storage.create_agent(
            name=payload.name,
            description=payload.description,
            voice_id=payload.voice_id,
            instructions=payload.instructions,
            is_active=payload.is_active,
        )
    except Exception as e:
        logger.error("Failed to create agent: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    
    agent = await agent_storage.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve created agent"
        )
    
    return AgentResponse(
        id=agent["id"],
        name=agent["name"],
        description=agent.get("description"),
        voice_id=str(agent["voice_id"]) if agent.get("voice_id") else None,
        instructions=agent.get("instructions"),
        is_active=agent.get("is_active", True),
        created_at=agent.get("created_at"),
        updated_at=agent.get("updated_at"),
    )


@router.put("/voice-agents/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: int, payload: AgentUpdateRequest) -> AgentResponse:
    """Update a voice agent."""
    agent_storage = _get_agent_storage()
    
    # Check agent exists
    existing = await agent_storage.get_agent_by_id(agent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )
    
    # Build update dict from non-None fields
    updates = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.description is not None:
        updates["description"] = payload.description
    if payload.voice_id is not None:
        updates["voice_id"] = payload.voice_id
    if payload.instructions is not None:
        updates["instructions"] = payload.instructions
    if payload.is_active is not None:
        updates["is_active"] = payload.is_active
    
    if updates:
        await agent_storage.update_agent(agent_id, **updates)
    
    agent = await agent_storage.get_agent_by_id(agent_id)
    return AgentResponse(
        id=agent["id"],
        name=agent["name"],
        description=agent.get("description"),
        voice_id=str(agent["voice_id"]) if agent.get("voice_id") else None,
        instructions=agent.get("instructions"),
        is_active=agent.get("is_active", True),
        created_at=agent.get("created_at"),
        updated_at=agent.get("updated_at"),
    )


@router.delete("/voice-agents/{agent_id}", response_model=dict)
async def delete_agent(agent_id: int) -> dict[str, Any]:
    """Delete a voice agent (soft delete - sets is_active=False)."""
    agent_storage = _get_agent_storage()
    
    existing = await agent_storage.get_agent_by_id(agent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )
    
    await agent_storage.update_agent(agent_id, is_active=False)
    
    return {
        "message": "Agent deleted successfully",
        "agent_id": agent_id,
    }


__all__ = ["router"]

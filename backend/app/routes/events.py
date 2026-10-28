import asyncio
import json
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthenticatedUser,
    get_current_user,
    require_project_owner,
    require_project_owner_flexible,
)
from app.database import get_db
from app.event_manager import event_manager
from app.repositories import ArtifactEventLogRepository
from app.schemas import (
    ActionEventListResponse,
    ArtifactEventListResponse,
    EventListResponse,
    ProjectEventListResponse,
)

logger = logging.getLogger("app.routes.events")

router = APIRouter()


# SYNC 4.1 — Stream owner-scoped project notifications.
# Trigger: SYNC 2.5 EventSource connection/reconnection.
# Input: project, flexible JWT, optional Last-Event-ID.
# Calls: SYNC 8.1 subscribe/replay; then dequeue live frames.
# Writes: NONE durable; holds one process-local subscriber queue.
# Branches: initial connected frame; data with id; 15s timeout heartbeat.
# Failure: invalid project → 400; disconnect/error → unsubscribe and END.
@router.get("/api/project/{project_id}/sse", status_code=status.HTTP_200_OK)
async def stream_project_events(
    project_id: str,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    # SSE clients are browser EventSources, which cannot send an Authorization
    # header — they authenticate with the Bearer header when available or a
    # `?token=` query parameter (see get_current_user_flexible). The caller
    # must additionally OWN the project.
    current_user: AuthenticatedUser = Depends(require_project_owner_flexible),
):
    """
    Server-Sent Events (SSE) stream for a project.

    Subscribes to the in-memory EventManager pub/sub bus and pushes any state
    change event published for this project to connected clients as a
    `text/event-stream`. This replaces the frontend's 3s polling of
    `/api/project/{id}`: clients now refresh only when a change actually occurs.

    Each frame is an unnamed `data:` event carrying JSON of the form:
        {"event": "<event_type>", "data": {...}}
    Each frame also carries an SSE `id:` field (a project-monotonic sequence
    number) so a reconnecting browser `EventSource` automatically returns with a
    `Last-Event-ID` header; the bus then replays any buffered events the client
    missed while it was disconnected before resuming live delivery. Heartbeat
    comments (`: ping`) keep the connection alive through proxies.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # A browser EventSource re-sends the last `id:` it observed on every
    # reconnect. Use it to seed the queue so events written while the client was
    # away are replayed before live delivery resumes (SSE replay).
    last_seq: Optional[int] = None
    if isinstance(last_event_id, str) and last_event_id.strip().lstrip("-").isdigit():
        last_seq = int(last_event_id.strip())

    async def event_generator():
        queue = event_manager.subscribe(project_id, last_event_id=last_seq)
        try:
            # Initial handshake so the client knows the stream is open.
            yield f"data: {json.dumps({'event': 'connected', 'data': {'project_id': project_id}})}\n\n"

            while True:
                try:
                    seq, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    message = json.loads(payload)
                    event_type = message.get("event", "message")
                    event_data = message.get("data", {})
                    framed = json.dumps({"event": event_type, "data": event_data})
                    # `id:` makes reconnects recover missed events via Last-Event-ID.
                    yield f"id: {seq}\ndata: {framed}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat comment keeps the connection alive through proxies/load balancers.
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            # Client disconnected; stop the generator.
            logger.debug(f"SSE stream closed for project {project_id}.")
        except Exception as e:
            logger.error(f"SSE stream error for project {project_id}: {str(e)}")
        finally:
            event_manager.unsubscribe(project_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/events", response_model=EventListResponse, status_code=status.HTTP_200_OK)
async def get_recent_events(
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db)
) -> EventListResponse:
    """
    Retrieve the most recent artifact event log entries across all projects.

    Args:
        limit: Maximum number of events to return.
        offset: Pagination offset.
        session: Active asynchronous database session.

    Returns:
        EventListResponse: Newest-first events with pagination metadata.
    """
    events = await ArtifactEventLogRepository.get_recent(
        session, limit=limit, offset=offset, user_id=current_user.id
    )
    return {
        "events": events,
        "total": len(events),
        "limit": limit,
        "offset": offset
    }


@router.get("/api/events/{artifact_type}/{artifact_id}", response_model=ArtifactEventListResponse, status_code=status.HTTP_200_OK)
async def get_artifact_events(
    artifact_type: str,
    artifact_id: str,
    limit: int = 100,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db)
) -> ArtifactEventListResponse:
    """
    Retrieve all events for a specific artifact (e.g. a user story, requirement, epic).

    Args:
        artifact_type: Type of artifact queried.
        artifact_id: UUID string of the artifact.
        limit: Maximum number of events to return.
        offset: Pagination offset.
        session: Active asynchronous database session.

    Returns:
        ArtifactEventListResponse: Newest-first events for the artifact.
    """
    events = await ArtifactEventLogRepository.get_by_artifact(
        artifact_type, artifact_id, session, limit=limit, offset=offset,
        user_id=current_user.id,
    )
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "events": events,
        "total": len(events),
        "limit": limit,
        "offset": offset
    }


@router.get("/api/events/action/{action}", response_model=ActionEventListResponse, status_code=status.HTTP_200_OK)
async def get_events_by_action(
    action: str,
    limit: int = 100,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db)
) -> ActionEventListResponse:
    """
    Retrieve all events for a specific action type (CREATE, UPDATE, DELETE, ARCHIVE, LOCK, UNLOCK).

    Args:
        action: The action type to filter by.
        limit: Maximum number of events to return.
        offset: Pagination offset.
        session: Active asynchronous database session.

    Returns:
        ActionEventListResponse: Newest-first events for the action.

    Raises:
        HTTPException: 400 if ``action`` is not a valid event action.
    """
    action_upper = action.upper()
    if action_upper not in ArtifactEventLogRepository.VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{action}'. Must be one of: {', '.join(sorted(ArtifactEventLogRepository.VALID_ACTIONS))}"
        )
    events = await ArtifactEventLogRepository.get_by_action(
        action_upper, session, limit=limit, offset=offset, user_id=current_user.id
    )
    return {
        "action": action_upper,
        "events": events,
        "total": len(events),
        "limit": limit,
        "offset": offset
    }


@router.get("/api/project/{project_id}/events", response_model=ProjectEventListResponse, status_code=status.HTTP_200_OK)
async def get_project_events(
    project_id: str,
    artifact_type: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> ProjectEventListResponse:
    """
    Retrieve events for a project, optionally filtered by artifact_type and/or action.

    Queries events by known artifact IDs for the project.

    Args:
        project_id: Project UUID string.
        artifact_type: Optional artifact-type filter.
        action: Optional action-type filter.
        limit: Maximum number of events to return.
        offset: Pagination offset.
        session: Active asynchronous database session.

    Returns:
        ProjectEventListResponse: Newest-first events for the project.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID or ``action``
            is invalid.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    if action:
        action_upper = action.upper()
        if action_upper not in ArtifactEventLogRepository.VALID_ACTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid action '{action}'. Must be one of: {', '.join(sorted(ArtifactEventLogRepository.VALID_ACTIONS))}"
            )
        action = action_upper

    events = await ArtifactEventLogRepository.get_by_project(
        project_id, session, artifact_type=artifact_type, action=action, limit=limit, offset=offset
    )
    return {
        "project_id": project_id,
        "artifact_type": artifact_type,
        "action": action,
        "events": events,
        "total": len(events),
        "limit": limit,
        "offset": offset
    }

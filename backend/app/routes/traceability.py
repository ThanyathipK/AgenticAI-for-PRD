"""
Requirement Traceability route.

`GET /api/project/{project_id}/traceability` — derived, read-only matrix linking
Requirements <-> User Stories <-> Acceptance Criteria <-> PRD sections <-> diagrams.
See ``app.traceability_service`` for how the links are derived.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, require_project_owner
from app.database import get_db
from app.repositories import ProjectRepository
from app.schemas import TraceabilityResponse
from app.traceability_service import TraceabilityService

logger = logging.getLogger("app.routes.traceability")

router = APIRouter()


# TRACEABILITY 4.1 — GET matrix. Trigger: UI 1.0 → loader 2.1 → client 3.1.
# Input: owner-authorized project UUID. Calls: project existence read 7.4, then
# matrix orchestration 5.1. Failure: invalid UUID 400; missing project 404.
# Writes/effects: none—the response is derived from current authoritative rows.
@router.get(
    "/api/project/{project_id}/traceability",
    response_model=TraceabilityResponse,
    status_code=status.HTTP_200_OK,
)
async def get_traceability(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> TraceabilityResponse:
    """
    Build the Requirement Traceability Matrix for a project.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        TraceabilityResponse: One matrix row per requirement (stories, criteria,
        PRD sections, diagrams) plus a project-wide coverage/gap report.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if the
        project does not exist.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # TRACEABILITY 7.4 — Confirm the authoritative project exists before deriving rows.
    project = await ProjectRepository.get_by_id(project_id, session)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matrix = await TraceabilityService.build_traceability(project_id, session)
    return TraceabilityResponse(**matrix)

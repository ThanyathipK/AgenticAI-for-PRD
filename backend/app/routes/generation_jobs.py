"""Persistent background-job API for long-running agents and regeneration."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user, require_project_owner
from app.config import settings
from app.database import get_db
from app.generation_job_service import cancel_job, create_job, get_job, launch_job, serialize_job
from app.rate_limit import rate_limit_dependency
from app.repositories import RequirementStateRepository
from app.routes.operational import RegenerationExecuteRequest
from app.schemas import ProcessRequirementsRequest

router = APIRouter()


@router.post("/api/process-requirements/background", status_code=status.HTTP_202_ACCEPTED)
async def start_agent_job(
    payload: ProcessRequirementsRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW,
        )
    ),
) -> dict[str, Any]:
    from app.auth import verify_project_access

    await verify_project_access(str(payload.project_id), current_user, db)
    if payload.target_agent not in {"architect", "auditor"}:
        raise HTTPException(status_code=400, detail="Only Architect and Auditor runs support this background endpoint.")
    if payload.target_agent == "architect":
        requirement_state = await RequirementStateRepository.get_by_project_id(
            str(payload.project_id), db
        )
        if not requirement_state or not (
            (requirement_state.get("requirements") or [])
            or str(requirement_state.get("generated_prd") or "").strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="There are no requirements to generate.",
            )
    try:
        job = await create_job(
            str(payload.project_id), current_user.id, str(payload.target_agent), payload.model_dump(), db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    launch_job(job["id"])
    return job


@router.post("/api/projects/{project_id}/regeneration/background", status_code=status.HTTP_202_ACCEPTED)
async def start_regeneration_job(
    project_id: str,
    payload: RegenerationExecuteRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW,
        )
    ),
) -> dict[str, Any]:
    try:
        job = await create_job(project_id, current_user.id, "regeneration", payload.model_dump(), db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    launch_job(job["id"])
    return job


@router.get("/api/projects/{project_id}/generation-jobs/{job_id}")
async def read_generation_job(
    project_id: str,
    job_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    job = await get_job(job_id, project_id, db)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    return serialize_job(job)


@router.post("/api/projects/{project_id}/generation-jobs/{job_id}/cancel")
async def stop_generation_job(
    project_id: str,
    job_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    job = await get_job(job_id, project_id, db)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    cancelled = await cancel_job(job, db)
    return {**serialize_job(job), "cancelled": cancelled}

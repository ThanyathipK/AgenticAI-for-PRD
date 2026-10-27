"""Project health, immutable audit history, and waiver endpoints."""
import asyncio
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_checklist import load_active_audit_checklist
from app.audit_history_service import create_waiver, list_audit_runs, list_waivers, revoke_waiver
from app.auth import AuthenticatedUser, require_project_owner
from app.database import get_db
from app.dependency_graph_service import (
    build_regeneration_plan,
    execute_regeneration,
    rebuild_dependency_graph,
)
from app.project_health_service import build_project_health
from app.workflow_cancellation import workflow_cancellations

router = APIRouter()


class WaiverCreateRequest(BaseModel):
    rule_id: str
    target_requirement_id: Optional[str] = None
    reason: str = Field(min_length=3, max_length=4000)
    compensating_control: Optional[str] = Field(None, max_length=4000)
    owner: str = Field(min_length=1, max_length=255)
    expires_at: datetime


class WaiverRevokeRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=4000)


class ArtifactReferenceRequest(BaseModel):
    artifact_type: Literal[
        "requirement", "user_story", "acceptance_criterion", "prd_section", "diagram"
    ]
    artifact_key: str = Field(min_length=1, max_length=255)


class RegenerationPlanRequest(BaseModel):
    changed_artifacts: list[ArtifactReferenceRequest] = Field(min_length=1, max_length=100)


class RegenerationExecuteRequest(RegenerationPlanRequest):
    confirm: Literal[True]


@router.get("/api/projects/{project_id}/health")
async def get_project_health(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await build_project_health(project_id, db)
    except LookupError:
        raise HTTPException(status_code=404, detail="Project not found") from None


@router.get("/api/projects/{project_id}/audit-history")
async def get_audit_history(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await list_audit_runs(project_id, db)


@router.get("/api/projects/{project_id}/waivers")
async def get_waivers(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await list_waivers(project_id, db)


@router.post("/api/projects/{project_id}/waivers", status_code=status.HTTP_201_CREATED)
async def post_waiver(
    project_id: str,
    payload: WaiverCreateRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    checklist = load_active_audit_checklist()
    if payload.rule_id.upper() not in checklist.rule_ids | {"ADHOC"}:
        raise HTTPException(status_code=400, detail="Unknown audit rule_id")
    try:
        return await create_waiver(
            project_id, db,
            rule_id=payload.rule_id,
            target_requirement_id=payload.target_requirement_id,
            reason=payload.reason,
            compensating_control=payload.compensating_control,
            owner=payload.owner,
            expires_at=payload.expires_at,
            approver_id=current_user.id,
            approver_name=current_user.full_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/projects/{project_id}/waivers/{waiver_id}/revoke")
async def post_revoke_waiver(
    project_id: str,
    waiver_id: str,
    payload: WaiverRevokeRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    waiver = await revoke_waiver(project_id, waiver_id, payload.reason, db)
    if waiver is None:
        raise HTTPException(status_code=404, detail="Waiver not found")
    return waiver


@router.get("/api/projects/{project_id}/dependency-graph")
async def get_dependency_graph(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await rebuild_dependency_graph(project_id, db)
    except LookupError:
        raise HTTPException(status_code=404, detail="Project requirement state not found") from None


@router.post("/api/projects/{project_id}/regeneration/plan")
async def post_regeneration_plan(
    project_id: str,
    payload: RegenerationPlanRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await build_regeneration_plan(
            project_id,
            [item.model_dump() for item in payload.changed_artifacts],
            db,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Project requirement state not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/projects/{project_id}/regeneration")
async def post_regeneration(
    project_id: str,
    payload: RegenerationExecuteRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    current_task = asyncio.current_task()
    if current_task is not None:
        workflow_cancellations.register(str(project_id), current_task)
    try:
        result = await execute_regeneration(
            project_id,
            [item.model_dump() for item in payload.changed_artifacts],
            current_user.full_name,
            db,
        )
        await db.commit()
        return result
    except asyncio.CancelledError:
        await db.rollback()
        return {
            "project_id": str(project_id),
            "status": "cancelled",
            "message": "Partial regeneration was stopped before completion. No changes were saved.",
        }
    except LookupError:
        raise HTTPException(status_code=404, detail="Project requirement state not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        await db.rollback()
        raise
    finally:
        if current_task is not None:
            workflow_cancellations.unregister(str(project_id), current_task)

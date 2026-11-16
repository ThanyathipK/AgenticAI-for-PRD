"""User-selected project statuses and append-only review history."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditResultModel,
    PRDVersionModel,
    ProjectModel,
    ProjectReviewEventModel,
    RequirementModel,
)
from app.repositories.base import as_uuid


def _action_for(from_status: str, to_status: str) -> str:
    if to_status == "approved":
        return "approve"
    if from_status == "approved" and to_status == "revised":
        return "reopen"
    if to_status in {"draft", "revised"}:
        return "request_changes"
    return "submit_review"


async def transition_project(
    project_id: str,
    to_status: str,
    session: AsyncSession,
    *,
    actor_id: str,
    actor_name: str,
    actor_role: str,
    comment: Optional[str] = None,
) -> ProjectModel:
    pid = as_uuid(project_id)
    project = await session.scalar(
        select(ProjectModel).where(
            ProjectModel.id == pid,
            ProjectModel.user_id == as_uuid(actor_id),
        )
    )
    if project is None:
        raise LookupError("Project not found")
    current = project.status or "draft"

    prd_version = None
    audit_version = None
    if to_status == "approved":
        latest_prd = await session.scalar(
            select(PRDVersionModel)
            .where(PRDVersionModel.project_id == pid)
            .order_by(PRDVersionModel.version_number.desc(), PRDVersionModel.created_at.desc())
            .limit(1)
        )
        latest_audit = await session.scalar(
            select(AuditResultModel)
            .join(RequirementModel, AuditResultModel.requirement_id == RequirementModel.id)
            .where(RequirementModel.project_id == pid)
            .order_by(AuditResultModel.updated_at.desc(), AuditResultModel.created_at.desc())
            .limit(1)
        )
        prd_version = latest_prd.version_number if latest_prd else None
        audit_version = latest_audit.audit_version_reviewed if latest_audit else None
        project.last_approved_prd_version = prd_version
        project.last_approved_audit_version = audit_version
        project.last_approved_by = as_uuid(actor_id)
        project.last_approved_at = datetime.now(timezone.utc)

    project.status = to_status
    session.add(ProjectReviewEventModel(
        project_id=pid,
        from_status=current,
        to_status=to_status,
        action=_action_for(current, to_status),
        comment=(comment or "").strip() or None,
        actor_id=as_uuid(actor_id),
        actor_name=actor_name,
        actor_role=actor_role,
        prd_version_number=prd_version,
        audit_version_reviewed=audit_version,
    ))
    await session.flush()
    await session.refresh(project)
    return project


async def get_review_history(project_id: str, session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ProjectReviewEventModel)
            .where(ProjectReviewEventModel.project_id == as_uuid(project_id))
            .order_by(ProjectReviewEventModel.created_at.desc())
        )
    ).scalars().all()
    prd_versions = (
        await session.execute(
            select(PRDVersionModel.version_number, PRDVersionModel.semver).where(
                PRDVersionModel.project_id == as_uuid(project_id)
            )
        )
    ).all()
    semver_by_number = {number: semver for number, semver in prd_versions}
    return [{
        "id": str(row.id),
        "project_id": str(row.project_id),
        "from_status": row.from_status,
        "to_status": row.to_status,
        "action": row.action,
        "comment": row.comment,
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "actor_name": row.actor_name,
        "actor_role": row.actor_role,
        "prd_version_number": row.prd_version_number,
        "prd_semver": semver_by_number.get(row.prd_version_number),
        "audit_version_reviewed": row.audit_version_reviewed,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]

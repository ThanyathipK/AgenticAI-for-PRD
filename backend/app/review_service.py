"""Governed project review transitions and append-only review history."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcceptanceCriteriaModel,
    AuditResultModel,
    AuditWaiverModel,
    ClarificationQuestionModel,
    PRDVersionModel,
    ProjectModel,
    ProjectReviewEventModel,
    RequirementModel,
    UserStoryModel,
)
from app.repositories.base import as_uuid

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"in_review_hpo"},
    "in_review_hpo": {"draft", "in_review_po"},
    "in_review_po": {"revised", "approved"},
    "approved": {"revised"},
    "revised": {"in_review_hpo"},
}

GOVERNED_STATE_FIELDS = (
    "requirements",
    "business_goals",
    "actors",
    "user_stories",
    "acceptance_criteria",
)


def governed_state_changed(before: Optional[dict], proposed: Optional[dict]) -> bool:
    """Compare only requirement-source fields supplied by a state mutation."""
    before = before or {}
    proposed = proposed or {}
    return any(
        field in proposed and proposed.get(field) != before.get(field)
        for field in GOVERNED_STATE_FIELDS
    )


class ReviewTransitionError(ValueError):
    """A requested transition violates lifecycle or approval prerequisites."""


def _action_for(from_status: str, to_status: str) -> str:
    if to_status == "approved":
        return "approve"
    if from_status == "approved" and to_status == "revised":
        return "reopen"
    if to_status in {"draft", "revised"}:
        return "request_changes"
    return "submit_review"


async def _approval_evidence(project_id: UUID, session: AsyncSession) -> tuple[int, AuditResultModel]:
    latest_prd = await session.scalar(
        select(PRDVersionModel)
        .where(PRDVersionModel.project_id == project_id)
        .order_by(PRDVersionModel.version_number.desc(), PRDVersionModel.created_at.desc())
        .limit(1)
    )
    if latest_prd is None:
        raise ReviewTransitionError("Generate and save a PRD version before approval.")

    latest_audit = await session.scalar(
        select(AuditResultModel)
        .join(RequirementModel, AuditResultModel.requirement_id == RequirementModel.id)
        .where(RequirementModel.project_id == project_id)
        .order_by(AuditResultModel.updated_at.desc(), AuditResultModel.created_at.desc())
        .limit(1)
    )
    if latest_audit is None:
        raise ReviewTransitionError("Run and save an audit before approval.")
    if latest_audit.verdict == "needs_clarification":
        raise ReviewTransitionError("Resolve audit clarification questions before approval.")
    active_waivers = (
        await session.execute(
            select(AuditWaiverModel).where(
                AuditWaiverModel.project_id == project_id,
                AuditWaiverModel.status == "active",
                AuditWaiverModel.expires_at > datetime.now(timezone.utc),
            )
        )
    ).scalars().all()
    waived = {
        (row.rule_id.upper(), (row.target_requirement_id or "PROJECT").upper())
        for row in active_waivers
    }
    if any(
        finding.get("impact") == "blocking"
        and finding.get("applicability") == "applicable_required"
        and (
            str(finding.get("rule_id") or "ADHOC").upper(),
            str(finding.get("target_requirement_id") or "PROJECT").upper(),
        ) not in waived
        and (str(finding.get("rule_id") or "ADHOC").upper(), "PROJECT") not in waived
        for finding in (latest_audit.findings or [])
        if isinstance(finding, dict)
    ):
        raise ReviewTransitionError("Resolve blocking audit findings before approval.")

    unresolved = await session.scalar(
        select(func.count())
        .select_from(ClarificationQuestionModel)
        .where(
            ClarificationQuestionModel.audit_result_id == latest_audit.id,
            ClarificationQuestionModel.is_resolved.is_(False),
        )
    )
    if unresolved:
        raise ReviewTransitionError("Resolve all audit clarification questions before approval.")

    # Audit freshness is based on governed requirement artifacts, not PRD version
    # bookkeeping: generating a document does not by itself make its source audit stale.
    timestamps = []
    for model, condition in (
        (RequirementModel, RequirementModel.project_id == project_id),
        (
            UserStoryModel,
            UserStoryModel.requirement_id.in_(
                select(RequirementModel.id).where(RequirementModel.project_id == project_id)
            ),
        ),
        (
            AcceptanceCriteriaModel,
            AcceptanceCriteriaModel.user_story_id.in_(
                select(UserStoryModel.id).where(
                    UserStoryModel.requirement_id.in_(
                        select(RequirementModel.id).where(RequirementModel.project_id == project_id)
                    )
                )
            ),
        ),
    ):
        value = await session.scalar(select(func.max(model.updated_at)).where(condition))
        if value is not None:
            timestamps.append(value)
    if timestamps and latest_audit.updated_at < max(timestamps):
        raise ReviewTransitionError("The saved audit is stale; run Validate again before approval.")

    return latest_prd.version_number, latest_audit


async def transition_project(
    project_id: str,
    to_status: str,
    session: AsyncSession,
    *,
    actor_id: str,
    actor_name: str,
    actor_role: str,
    comment: Optional[str] = None,
    manual: bool = False,
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
    if not manual and to_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ReviewTransitionError(f"Transition from '{current}' to '{to_status}' is not allowed.")
    if not manual and to_status in {"draft", "revised"} and not (comment or "").strip():
        raise ReviewTransitionError("A reason is required when requesting changes or reopening an approval.")

    prd_version = None
    audit_version = None
    if to_status == "approved":
        if manual:
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
        else:
            prd_version, audit = await _approval_evidence(pid, session)
            audit_version = audit.audit_version_reviewed
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


async def mark_revised_if_approved(
    project_id: str,
    session: AsyncSession,
    *,
    reason: str,
    actor_id: Optional[str] = None,
    actor_name: str = "System",
    actor_role: str = "system",
) -> bool:
    """Invalidate current approval after a governed content change."""
    pid = as_uuid(project_id)
    project = await session.scalar(select(ProjectModel).where(ProjectModel.id == pid))
    if project is None or project.status != "approved":
        return False
    project.status = "revised"
    session.add(ProjectReviewEventModel(
        project_id=pid,
        from_status="approved",
        to_status="revised",
        action="content_changed",
        comment=reason,
        actor_id=as_uuid(actor_id) if actor_id else None,
        actor_name=actor_name,
        actor_role=actor_role,
        prd_version_number=project.last_approved_prd_version,
        audit_version_reviewed=project.last_approved_audit_version,
    ))
    await session.flush()
    return True


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

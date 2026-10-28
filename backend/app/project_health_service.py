"""Deterministic project-health rollup over existing governed artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_history_service import list_waivers
from app.models import (
    AcceptanceCriteriaModel,
    AuditResultModel,
    ClarificationQuestionModel,
    PRDSectionModel,
    ProjectModel,
    RequirementModel,
    UserStoryModel,
)
from app.repositories.base import as_uuid
from app.traceability_service import TraceabilityService


async def build_project_health(project_id: str, session: AsyncSession) -> dict[str, Any]:
    pid = as_uuid(project_id)
    project = await session.scalar(select(ProjectModel).where(ProjectModel.id == pid))
    if project is None:
        raise LookupError("Project not found")

    # TRACEABILITY 5.2 — Project health reuses the same read-only matrix/coverage
    # derivation; it does not maintain a second traceability calculation.
    traceability = await TraceabilityService.build_traceability(project_id, session)
    coverage = traceability["coverage"]
    audit = await session.scalar(
        select(AuditResultModel)
        .join(RequirementModel, AuditResultModel.requirement_id == RequirementModel.id)
        .where(RequirementModel.project_id == pid)
        .order_by(AuditResultModel.updated_at.desc(), AuditResultModel.created_at.desc())
        .limit(1)
    )
    unresolved = int(await session.scalar(
        select(func.count()).select_from(ClarificationQuestionModel)
        .join(AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id)
        .join(RequirementModel, AuditResultModel.requirement_id == RequirementModel.id)
        .where(RequirementModel.project_id == pid, ClarificationQuestionModel.is_resolved.is_(False))
    ) or 0)
    pending_sections = int(await session.scalar(
        select(func.count()).select_from(PRDSectionModel).where(
            PRDSectionModel.project_id == pid,
            PRDSectionModel.review_status != "approved",
        )
    ) or 0)
    active_waivers = await list_waivers(project_id, session, include_inactive=False)

    findings = [item for item in ((audit.findings if audit else None) or []) if isinstance(item, dict)]
    waived = {
        (item["rule_id"].upper(), (item.get("target_requirement_id") or "PROJECT").upper())
        for item in active_waivers
    }
    blocking = sum(
        1 for item in findings
        if item.get("impact") == "blocking"
        and (
            str(item.get("rule_id") or "ADHOC").upper(),
            str(item.get("target_requirement_id") or "PROJECT").upper(),
        ) not in waived
        and (str(item.get("rule_id") or "ADHOC").upper(), "PROJECT") not in waived
    )
    warnings = sum(1 for item in findings if item.get("impact") == "warning")
    suggestions = sum(1 for item in findings if item.get("impact") == "suggestion")

    latest_artifact_update = None
    for model, condition in (
        (RequirementModel, RequirementModel.project_id == pid),
        (UserStoryModel, UserStoryModel.requirement_id.in_(select(RequirementModel.id).where(RequirementModel.project_id == pid))),
        (AcceptanceCriteriaModel, AcceptanceCriteriaModel.user_story_id.in_(
            select(UserStoryModel.id).where(UserStoryModel.requirement_id.in_(
                select(RequirementModel.id).where(RequirementModel.project_id == pid)
            ))
        )),
    ):
        value = await session.scalar(select(func.max(model.updated_at)).where(condition))
        if value is not None and (latest_artifact_update is None or value > latest_artifact_update):
            latest_artifact_update = value
    audit_stale = bool(audit and latest_artifact_update and audit.updated_at < latest_artifact_update)

    total = int(coverage["total_requirements"] or 0)
    traced = int(coverage["traced_requirements"] or 0)
    coverage_percent = round((traced / total) * 100) if total else 0
    total_stories = int(coverage["total_user_stories"] or 0)
    story_covered = total - len(coverage["requirements_without_stories"])
    criteria_covered = total_stories - len(coverage["stories_without_criteria"])
    prd_covered = total - len(coverage["requirements_without_prd_sections"])
    diagram_covered = total - len(coverage["requirements_without_diagram"])

    def percent(covered: int, denominator: int) -> int:
        return round((covered / denominator) * 100) if denominator else 0

    coverage_breakdown = {
        # "Fully traced" means a requirement has a story, every story has
        # acceptance criteria, and the requirement/story is referenced by a
        # PRD section. Diagram linkage is reported separately by design.
        "requirement_traceability_percent": coverage_percent,
        "requirements_with_stories_percent": percent(story_covered, total),
        "stories_with_acceptance_criteria_percent": percent(criteria_covered, total_stories),
        "requirements_with_prd_reference_percent": percent(prd_covered, total),
        "requirements_with_diagram_reference_percent": percent(diagram_covered, total),
    }
    issues: list[dict[str, Any]] = []

    def issue(kind: str, label: str, count: int, destination: str, severity: str = "warning") -> None:
        if count:
            issues.append({"kind": kind, "label": label, "count": count, "destination": destination, "severity": severity})

    issue("blocking_findings", "Blocking audit findings", blocking, "audit", "blocking")
    issue("unresolved_questions", "Unresolved clarification questions", unresolved, "requirements", "blocking")
    issue("missing_stories", "Requirements without stories", len(coverage["requirements_without_stories"]), "traceability")
    issue("missing_criteria", "Stories without acceptance criteria", len(coverage["stories_without_criteria"]), "traceability")
    issue("criteria_review", "Acceptance criteria requiring review", len(coverage.get("criteria_needing_review", [])), "traceability")
    issue("missing_sections", "Requirements without PRD coverage", len(coverage["requirements_without_prd_sections"]), "traceability")
    issue("missing_diagrams", "Requirements without diagram coverage", len(coverage["requirements_without_diagram"]), "traceability")
    issue("stale_references", "Stale PRD or diagram references", len(coverage["stale_section_references"]) + len(coverage["stale_diagram_references"]), "traceability")
    issue("pending_sections", "PRD sections awaiting approval", pending_sections, "prd")
    if audit_stale:
        issue("stale_audit", "Audit is older than requirement changes", 1, "audit", "blocking")

    if project.status == "approved" and not issues:
        health_status = "approved"
    elif blocking or unresolved or audit_stale:
        health_status = "blocked"
    elif issues or warnings:
        health_status = "attention_required"
    else:
        health_status = "ready"

    return {
        "project_id": project_id,
        "status": health_status,
        "workflow_status": project.status or "draft",
        # Backward-compatible alias for requirement traceability. New clients
        # should use the explicitly named value in coverage_breakdown.
        "coverage_percent": coverage_percent,
        "coverage_breakdown": coverage_breakdown,
        "metrics": {
            "total_requirements": total,
            "total_user_stories": total_stories,
            "traced_requirements": traced,
            "blocking_findings": blocking,
            "warnings": warnings,
            "suggestions": suggestions,
            "unresolved_questions": unresolved,
            "pending_prd_sections": pending_sections,
            "requirements_without_diagram": len(coverage["requirements_without_diagram"]),
            "active_waivers": len(active_waivers),
        },
        "audit": {
            "verdict": audit.verdict if audit else None,
            "version_reviewed": audit.audit_version_reviewed if audit else None,
            "checklist_version": audit.checklist_version if audit else None,
            "stale": audit_stale,
            "updated_at": audit.updated_at.isoformat() if audit and audit.updated_at else None,
        },
        "approval": {
            "prd_version": project.last_approved_prd_version,
            "audit_version": project.last_approved_audit_version,
            "approved_at": project.last_approved_at.isoformat() if project.last_approved_at else None,
        },
        "issues": issues,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

"""Governed project lifecycle and immutable approval evidence tests."""
import pytest
from sqlalchemy import select

from app.models import (
    AuditResultModel,
    ProjectModel,
    ProjectReviewEventModel,
    PRDVersionModel,
    RequirementModel,
)
from app.review_service import ReviewTransitionError, mark_revised_if_approved, transition_project
from tests.test_documents import db_session, seeded_project


async def _actor(session, project_id):
    project = await session.scalar(select(ProjectModel).where(ProjectModel.id == project_id))
    return {
        "actor_id": str(project.user_id),
        "actor_name": "Project Owner",
        "actor_role": "Business Analyst",
    }


@pytest.mark.asyncio
async def test_illegal_transition_is_rejected(db_session, seeded_project):
    with pytest.raises(ReviewTransitionError, match="not allowed"):
        await transition_project(
            seeded_project, "approved", db_session,
            **await _actor(db_session, seeded_project),
        )


@pytest.mark.asyncio
async def test_manual_status_allows_direct_approval_and_records_event(db_session, seeded_project):
    actor = await _actor(db_session, seeded_project)

    project = await transition_project(
        seeded_project,
        "approved",
        db_session,
        manual=True,
        **actor,
    )

    assert project.status == "approved"
    assert project.last_approved_prd_version is None
    assert project.last_approved_audit_version is None
    assert str(project.last_approved_by) == actor["actor_id"]
    assert project.last_approved_at is not None
    event = await db_session.scalar(
        select(ProjectReviewEventModel).where(
            ProjectReviewEventModel.project_id == seeded_project
        )
    )
    assert event is not None
    assert event.from_status == "draft"
    assert event.to_status == "approved"
    assert event.action == "approve"


@pytest.mark.asyncio
async def test_request_changes_requires_reason(db_session, seeded_project):
    actor = await _actor(db_session, seeded_project)
    await transition_project(seeded_project, "in_review_hpo", db_session, **actor)
    with pytest.raises(ReviewTransitionError, match="reason is required"):
        await transition_project(seeded_project, "draft", db_session, **actor)


@pytest.mark.asyncio
async def test_approval_requires_prd_and_fresh_passing_audit(db_session, seeded_project):
    actor = await _actor(db_session, seeded_project)
    await transition_project(seeded_project, "in_review_hpo", db_session, **actor)
    await transition_project(seeded_project, "in_review_po", db_session, **actor)

    with pytest.raises(ReviewTransitionError, match="PRD"):
        await transition_project(seeded_project, "approved", db_session, **actor)

    requirement = RequirementModel(
        project_id=seeded_project,
        requirement_code="REQ-001",
        title="Payment approval",
        status="active",
    )
    db_session.add(requirement)
    db_session.add(PRDVersionModel(
        project_id=seeded_project,
        version_number=1,
        generated_prd="Approved candidate",
        generated_by="test",
        change_type="ai",
        semver="1.0.0",
    ))
    await db_session.flush()

    with pytest.raises(ReviewTransitionError, match="audit"):
        await transition_project(seeded_project, "approved", db_session, **actor)

    audit = AuditResultModel(
        requirement_id=requirement.id,
        audit_version_reviewed=1,
        is_valid=True,
        passed_checks=["Product readiness"],
        failed_checks=[],
        findings=[],
        source_references=[],
        verdict="pass",
        project_context={},
    )
    db_session.add(audit)
    await db_session.flush()

    project = await transition_project(
        seeded_project, "approved", db_session,
        comment="Approved for delivery.", **actor,
    )
    assert project.status == "approved"
    assert project.last_approved_prd_version == 1
    assert project.last_approved_audit_version == 1

    events = (
        await db_session.execute(
            select(ProjectReviewEventModel)
            .where(ProjectReviewEventModel.project_id == seeded_project)
            .order_by(ProjectReviewEventModel.created_at)
        )
    ).scalars().all()
    assert [event.to_status for event in events] == ["in_review_hpo", "in_review_po", "approved"]
    assert events[-1].comment == "Approved for delivery."
    assert events[-1].prd_version_number == 1


@pytest.mark.asyncio
async def test_content_change_reopens_approved_project(db_session, seeded_project):
    project = await db_session.scalar(select(ProjectModel).where(ProjectModel.id == seeded_project))
    project.status = "approved"
    project.last_approved_prd_version = 4
    project.last_approved_audit_version = 3
    await db_session.flush()

    changed = await mark_revised_if_approved(
        seeded_project,
        db_session,
        reason="Requirement changed.",
    )
    assert changed is True
    assert project.status == "revised"
    event = await db_session.scalar(
        select(ProjectReviewEventModel)
        .where(ProjectReviewEventModel.project_id == seeded_project)
        .order_by(ProjectReviewEventModel.created_at.desc())
    )
    assert event.action == "content_changed"
    assert event.prd_version_number == 4

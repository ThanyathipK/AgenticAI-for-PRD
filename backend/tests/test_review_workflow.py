"""User-selected project status and immutable status-history tests."""
import pytest
from sqlalchemy import select

from app.auth import AuthenticatedUser
from app.models import ProjectModel, ProjectReviewEventModel
from app.review_service import transition_project
from app.routes.projects import set_project_status
from app.schemas import ProjectStatusRequest
from app.version_service import record_prd_version
from tests.test_documents import db_session, seeded_project  # noqa: F401


async def _actor(session, project_id):
    project = await session.scalar(select(ProjectModel).where(ProjectModel.id == project_id))
    return {
        "actor_id": str(project.user_id),
        "actor_name": "Project Owner",
        "actor_role": "Business Analyst",
    }


@pytest.mark.asyncio
async def test_status_route_allows_direct_approval_without_review_or_evidence(
    db_session, seeded_project,
):
    actor = await _actor(db_session, seeded_project)
    current_user = AuthenticatedUser(
        id=actor["actor_id"],
        email="owner@example.com",
        full_name=actor["actor_name"],
        role=actor["actor_role"],
    )

    project = await set_project_status(
        seeded_project,
        ProjectStatusRequest(status="approved"),
        current_user=current_user,
        db=db_session,
    )

    assert project["status"] == "approved"
    assert project["last_approved_prd_version"] is None
    assert project["last_approved_audit_version"] is None
    event = await db_session.scalar(
        select(ProjectReviewEventModel).where(
            ProjectReviewEventModel.project_id == seeded_project
        )
    )
    assert event is not None
    assert event.from_status == "draft"
    assert event.to_status == "approved"


@pytest.mark.asyncio
async def test_status_route_allows_revised_without_a_reason(db_session, seeded_project):
    actor = await _actor(db_session, seeded_project)
    current_user = AuthenticatedUser(
        id=actor["actor_id"], email="owner@example.com",
        full_name=actor["actor_name"], role=actor["actor_role"],
    )

    result = await set_project_status(
        seeded_project,
        ProjectStatusRequest(status="revised"),
        current_user=current_user,
        db=db_session,
    )

    assert result["status"] == "revised"


@pytest.mark.asyncio
async def test_prd_change_does_not_override_user_selected_approved_status(
    db_session, seeded_project,
):
    actor = await _actor(db_session, seeded_project)
    await transition_project(seeded_project, "approved", db_session, **actor)

    await record_prd_version(
        seeded_project,
        db_session,
        generated_prd="### 1. Business Overview\n\nUpdated after approval.",
        generated_by="test",
        change_type="manual",
    )

    project = await db_session.scalar(
        select(ProjectModel).where(ProjectModel.id == seeded_project)
    )
    events = (
        await db_session.execute(
            select(ProjectReviewEventModel).where(
                ProjectReviewEventModel.project_id == seeded_project
            )
        )
    ).scalars().all()
    assert project.status == "approved"
    assert [event.to_status for event in events] == ["approved"]

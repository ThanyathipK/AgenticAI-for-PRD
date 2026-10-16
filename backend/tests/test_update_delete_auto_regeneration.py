"""Regression coverage for Save -> automatic PRD dependency regeneration.

Both an ordinary requirement update and an archive/delete must persist first,
then queue the same scoped regeneration job when an existing PRD section
depends on the changed requirement.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import AuthenticatedUser
from app.dependency_graph_service import rebuild_dependency_graph
from app.models import PendingActionModel, ProjectModel, UserModel
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.routes.lock import confirm_action


BASE_STATE = {
    "requirements": [{
        "requirement_code": "REQ-001",
        "title": "Transfer approval",
        "description": "A checker approves a pending transfer.",
        "user_stories": [{
            "ticket_code": "US-001",
            "story_title": "Approve transfer",
            "as_a": "checker",
            "i_want_to": "approve a pending transfer",
            "so_that": "funds are controlled",
            "acceptance_criteria": ["Given a pending transfer, approval releases it"],
        }],
    }],
    "user_stories": [],
    "acceptance_criteria": [],
    "generated_diagrams": "",
}


@pytest_asyncio.fixture
async def saved_project(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/update-delete-cascade.sqlite")
    async with engine.begin() as connection:
        from app.database import Base
        from app import models  # noqa: F401

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        user = UserModel(
            email="cascade-owner@bank.test",
            full_name="Cascade Owner",
            role="Product Owner",
        )
        session.add(user)
        await session.flush()
        project = ProjectModel(user_id=user.id, name="Cascade Test", industry_standard="Banking")
        session.add(project)
        await session.flush()
        project_id = str(project.id)

        await RequirementStateRepository.save_or_update(project_id, BASE_STATE, session)
        await PRDSectionRepository.create(project_id, {
            "section_key": "product_scope",
            "title": "2. Product Scope",
            "content": "## 2. Product Scope\nREQ-001 is delivered by US-001.",
            "section_order": 2,
        }, session)
        await rebuild_dependency_graph(project_id, session)
        await session.commit()

        yield session, project_id, AuthenticatedUser(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            role=user.role,
        )
    await engine.dispose()


async def _confirm_change(
    session: AsyncSession,
    project_id: str,
    current_user: AuthenticatedUser,
    monkeypatch,
    *,
    action_type: str,
    proposed_changes: dict,
):
    action = PendingActionModel(
        project_id=project_id,
        action_type=action_type,
        original_user_message="test change",
        proposed_changes=proposed_changes,
        affected_user_story_ids=["REQ-001", "US-001"],
        affected_acceptance_criteria_ids=[],
        workflow_stage="delete_requirement_node" if action_type == "ARCHIVE" else "gatherer_node",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(action)
    await session.commit()

    queued = []

    async def fake_create_job(pid, uid, job_type, payload, _session):
        queued.append((pid, uid, job_type, payload))
        return {"id": "cascade-job-001"}

    monkeypatch.setattr("app.generation_job_service.create_job", fake_create_job)
    monkeypatch.setattr("app.generation_job_service.launch_job", lambda job_id: None)

    result = await confirm_action(str(action.id), project_id, current_user, session)
    return result, queued, str(action.id)


@pytest.mark.asyncio
async def test_saved_update_queues_automatic_prd_regeneration(saved_project, monkeypatch):
    session, project_id, current_user = saved_project
    proposed = {**BASE_STATE}
    proposed["requirements"] = [{
        **BASE_STATE["requirements"][0],
        "description": "A checker approves or rejects a pending transfer.",
    }]

    result, queued, action_id = await _confirm_change(
        session, project_id, current_user, monkeypatch,
        action_type="MERGE", proposed_changes=proposed,
    )

    assert result["cascade_status"] == "queued"
    assert result["cascade_job_id"] == "cascade-job-001"
    assert queued[0][2] == "regeneration"
    assert queued[0][3]["automatic"] is True
    assert any(item["artifact_key"] == "REQ-001" for item in queued[0][3]["changed_artifacts"])
    assert await session.get(PendingActionModel, action_id) is None


@pytest.mark.asyncio
async def test_saved_delete_queues_automatic_prd_regeneration(saved_project, monkeypatch):
    session, project_id, current_user = saved_project
    remaining_requirement = {
        "requirement_code": "REQ-002",
        "title": "Transfer history",
        "description": "Customers can view completed transfers.",
        "user_stories": [],
    }
    proposed = {
        **BASE_STATE,
        "requirements": [remaining_requirement],
        "user_stories": [],
        "acceptance_criteria": [],
    }

    result, queued, action_id = await _confirm_change(
        session, project_id, current_user, monkeypatch,
        action_type="ARCHIVE", proposed_changes=proposed,
    )

    assert result["cascade_status"] == "queued"
    assert result["cascade_job_id"] == "cascade-job-001"
    assert queued[0][2] == "regeneration"
    assert queued[0][3]["automatic"] is True
    assert any(item["artifact_key"] == "REQ-001" for item in queued[0][3]["changed_artifacts"])
    assert await session.get(PendingActionModel, action_id) is None
    saved_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    assert [item["requirement_code"] for item in saved_state["requirements"]] == ["REQ-002"]


@pytest.mark.asyncio
async def test_deleting_last_requirement_queues_empty_prd_regeneration(saved_project, monkeypatch):
    session, project_id, current_user = saved_project
    proposed = {
        **BASE_STATE,
        "requirements": [],
        "user_stories": [],
        "acceptance_criteria": [],
    }

    result, queued, action_id = await _confirm_change(
        session, project_id, current_user, monkeypatch,
        action_type="ARCHIVE", proposed_changes=proposed,
    )

    assert result["status"] == "confirmed"
    assert result["cascade_status"] == "queued"
    assert result["cascade_job_id"] == "cascade-job-001"
    assert queued[0][2] == "regeneration"
    assert queued[0][3]["automatic"] is True
    assert await session.get(PendingActionModel, action_id) is None
    saved_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    assert saved_state["requirements"] == []


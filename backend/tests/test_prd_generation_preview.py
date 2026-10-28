"""PRD generation remains draft-only until explicit approval."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import AuthenticatedUser
from app.database import Base
from app.models import ConversationMessageModel, PendingActionModel, PRDVersionModel, ProjectModel, UserModel
from app.repositories.requirement_state import RequirementStateRepository
from app.routes.lock import cancel_action, confirm_action
from tests.test_prd_versions import _doc_v1, _doc_v2_business_updated

pytest_plugins = ("tests.test_documents",)


@pytest_asyncio.fixture
async def preview_project(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/prd-preview.sqlite")
    async with engine.begin() as connection:
        from app import models  # noqa: F401

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        user = UserModel(email="preview@bank.test", full_name="Preview Owner", role="Product Owner")
        session.add(user)
        await session.flush()
        project = ProjectModel(user_id=user.id, name="Preview Project", industry_standard="Banking")
        session.add(project)
        await session.flush()
        project_id = str(project.id)
        await RequirementStateRepository.save_or_update(
            project_id,
            {
                "requirements_revision": 3,
                "version_number": 1,
                "generated_prd": _doc_v1(),
                "generated_diagrams": "flowchart LR\nA --> B",
            },
            session,
        )
        await session.commit()
        actor = AuthenticatedUser(
            id=str(user.id), email=user.email, full_name=user.full_name, role=user.role,
        )
        yield session, project_id, actor
    await engine.dispose()


async def _stage(session, project_id, *, requirements_revision=3, base_version=1):
    action = PendingActionModel(
        project_id=project_id,
        action_type="GENERATE_PRD",
        original_user_message="Generate PRD",
        proposed_changes={
            "generated_prd": _doc_v2_business_updated(),
            "generated_diagrams": "flowchart LR\nA --> C",
            "source_requirements_revision": requirements_revision,
            "base_prd_version": base_version,
            "proposed_version": base_version + 1,
        },
        affected_user_story_ids=[],
        affected_acceptance_criteria_ids=[],
        workflow_stage="PRD_PREVIEW",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(action)
    await session.commit()
    return action


@pytest.mark.asyncio
async def test_architect_endpoint_stages_without_replacing_saved_prd(
    client, db_session, seeded_project, monkeypatch,
):
    import app.routes.requirements as requirement_routes
    import app.semantic_service as semantic_service

    saved = await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "requirements_revision": 4,
            "version_number": 1,
            "requirements": [{
                "requirement_code": "REQ-001",
                "title": "Transfer approval",
                "description": "A checker approves a transfer.",
                "user_stories": [],
            }],
            "generated_prd": _doc_v1(),
            "generated_diagrams": "flowchart LR\nA --> B",
        },
        db_session,
    )
    await db_session.commit()

    async def detected(*_args, **_kwargs):
        return {"intent": "GENERAL_CHAT", "confidence": 1.0, "reason": "explicit agent target"}

    async def generated(state):
        assert state["stage_artifacts_only"] is True
        generated_state = {
            **saved,
            "generated_prd": _doc_v2_business_updated(),
            "generated_diagrams": "flowchart LR\nA --> C",
            "current_workflow_state": "COMPLETED",
        }
        return {
            **state,
            "requirement_state": generated_state,
            "prd_markdown": generated_state["generated_prd"],
            "mermaid_diagram": generated_state["generated_diagrams"],
            "diagram_regenerated": False,
            "workflow_routing": {},
            "agent_message": "Preview ready.",
        }

    monkeypatch.setattr(semantic_service, "detect_requirement_intent", detected)
    monkeypatch.setattr(requirement_routes.prd_workflow, "ainvoke", generated)

    response = await client.post(
        "/api/process-requirements",
        json={
            "project_id": seeded_project,
            "raw_input": "",
            "target_agent": "architect",
            "force_prd_rebuild": True,
            "client_persists_timeline": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_merge"] is True
    assert body["pending_action_type"] == "GENERATE_PRD"
    assert body["pending_action_id"]
    assert body["diagram_regenerated"] is False
    persisted = await RequirementStateRepository.get_by_project_id(seeded_project, db_session)
    assert persisted["generated_prd"] == _doc_v1()
    assert persisted["generated_diagrams"] == "flowchart LR\nA --> B"
    assert await db_session.scalar(select(func.count()).select_from(PRDVersionModel)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ConversationMessageModel)) == 0
    draft = await db_session.scalar(
        select(PendingActionModel).where(PendingActionModel.id == body["pending_action_id"])
    )
    assert draft.action_type == "GENERATE_PRD"
    assert draft.proposed_changes["source_requirements_revision"] == 4
    assert "Real-time settlement" in draft.proposed_changes["generated_prd"]


@pytest.mark.asyncio
async def test_architect_rejects_generation_while_requirement_draft_is_pending(
    client, db_session, seeded_project, monkeypatch,
):
    import app.semantic_service as semantic_service

    async def must_not_detect(*_args, **_kwargs):
        raise AssertionError("intent/model processing must not start")

    monkeypatch.setattr(semantic_service, "detect_requirement_intent", must_not_detect)
    pending = PendingActionModel(
        project_id=seeded_project,
        action_type="INSERT_CHUNKED_REQUIREMENTS",
        original_user_message="Extract requirements from document",
        proposed_changes={"requirements_revision": 2},
        affected_user_story_ids=[],
        affected_acceptance_criteria_ids=[],
        workflow_stage="gatherer_node",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(pending)
    await db_session.commit()

    response = await client.post(
        "/api/process-requirements",
        json={
            "project_id": seeded_project,
            "raw_input": "",
            "target_agent": "architect",
            "force_prd_rebuild": True,
            "client_persists_timeline": True,
        },
    )

    assert response.status_code == 409
    assert "Save or discard" in response.json()["detail"]


@pytest.mark.asyncio
async def test_approve_materializes_preview_and_creates_one_version(preview_project):
    session, project_id, actor = preview_project
    action = await _stage(session, project_id)

    result = await confirm_action(str(action.id), project_id, actor, session)

    assert result["status"] == "confirmed"
    assert result["requirement_state"]["generated_diagrams"] == "flowchart LR\nA --> C"
    assert result["requirement_state"]["version_number"] == 1
    assert "Real-time settlement" in result["requirement_state"]["generated_prd"]
    assert await session.get(PendingActionModel, action.id) is None
    assert await session.scalar(select(func.count()).select_from(PRDVersionModel)) == 1


@pytest.mark.asyncio
async def test_stale_requirements_reject_approval_without_writes(preview_project):
    session, project_id, actor = preview_project
    action = await _stage(session, project_id, requirements_revision=2)

    with pytest.raises(HTTPException) as raised:
        await confirm_action(str(action.id), project_id, actor, session)

    assert raised.value.status_code == 409
    assert "requirements changed" in raised.value.detail
    assert await session.get(PendingActionModel, action.id) is not None
    assert await session.scalar(select(func.count()).select_from(PRDVersionModel)) == 0
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    assert state["generated_prd"] == _doc_v1()


@pytest.mark.asyncio
async def test_confirmed_requirement_change_discards_stale_prd_preview(preview_project):
    session, project_id, actor = preview_project
    stale_preview = await _stage(session, project_id)
    current = await RequirementStateRepository.get_by_project_id(project_id, session)
    merge = PendingActionModel(
        project_id=project_id,
        action_type="INSERT_CHUNKED_REQUIREMENTS",
        original_user_message="Extract requirements from document",
        proposed_changes={
            **current,
            "prd_narrative": {
                "problem_statement": {
                    "value": "The legacy runtime creates security risks.",
                    "confidence": 0.5,
                    "source": "explicit_user_input",
                },
            },
            "requirements_revision": 4,
            "_base_requirements_revision": 3,
        },
        affected_user_story_ids=[],
        affected_acceptance_criteria_ids=[],
        workflow_stage="REVIEWING",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(merge)
    await session.commit()

    result = await confirm_action(str(merge.id), project_id, actor, session)

    assert result["status"] == "confirmed"
    assert await session.get(PendingActionModel, stale_preview.id) is None
    persisted = await RequirementStateRepository.get_by_project_id(project_id, session)
    assert persisted["prd_narrative"]["problem_statement"]["value"].startswith(
        "The legacy runtime"
    )


@pytest.mark.asyncio
async def test_discard_deletes_only_the_preview(preview_project):
    session, project_id, actor = preview_project
    action = await _stage(session, project_id)

    result = await cancel_action(str(action.id), project_id, actor, session)

    assert result["status"] == "cancelled"
    assert await session.get(PendingActionModel, action.id) is None
    assert await session.scalar(select(func.count()).select_from(PRDVersionModel)) == 0
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    assert state["generated_prd"] == _doc_v1()
    assert state["generated_diagrams"] == "flowchart LR\nA --> B"


@pytest.mark.asyncio
async def test_expired_preview_cannot_be_approved(preview_project):
    session, project_id, actor = preview_project
    action = await _stage(session, project_id)
    action.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(HTTPException) as raised:
        await confirm_action(str(action.id), project_id, actor, session)

    assert raised.value.status_code == 410
    assert await session.get(PendingActionModel, action.id) is None
    assert await session.scalar(select(func.count()).select_from(PRDVersionModel)) == 0

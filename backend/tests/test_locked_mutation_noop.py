"""Locked update/delete requests stop before inference and proposal staging."""

import pytest
from sqlalchemy import func, select, update

from app.models import PendingActionModel, RequirementModel
from app.repositories.requirement_state import RequirementStateRepository
from tests.test_documents import client, db_session, seeded_project


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "raw_input"),
    [
        ("UPDATE_REQUIREMENT", "Update REQ-001 to require two approvers"),
        ("DELETE_REQUIREMENT", "Delete REQ-001"),
    ],
)
async def test_locked_requirement_returns_message_without_merge_or_workflow(
    client, db_session, seeded_project, monkeypatch, intent, raw_input
):
    import app.routes.requirements as requirement_routes
    import app.semantic_service as semantic_service

    await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "requirements": [{
                "requirement_code": "REQ-001",
                "title": "Transfer approval",
                "description": "Approval controls",
                "user_stories": [{
                    "ticket_code": "US-001",
                    "story_title": "Approve transfer",
                    "as_a": "checker",
                    "i_want_to": "approve a transfer",
                    "so_that": "it can be processed",
                    "acceptance_criteria": [
                        "Given a transfer, when approved, then it is processed"
                    ],
                }],
            }],
        },
        db_session,
    )
    await db_session.execute(
        update(RequirementModel)
        .where(RequirementModel.project_id == seeded_project)
        .values(is_locked=True, locked_by="product-owner")
    )
    await db_session.commit()

    async def detected(*_args, **_kwargs):
        return {"intent": intent, "confidence": 0.99, "reason": "explicit operation"}

    async def workflow_must_not_run(*_args, **_kwargs):
        raise AssertionError("locked operations must stop before LangGraph/LLM execution")

    monkeypatch.setattr(semantic_service, "detect_requirement_intent", detected)
    monkeypatch.setattr(requirement_routes.prd_workflow, "ainvoke", workflow_must_not_run)

    before = await db_session.scalar(select(func.count()).select_from(PendingActionModel))
    response = await client.post(
        "/api/process-requirements",
        json={
            "project_id": seeded_project,
            "raw_input": raw_input,
            "target_agent": "delete_requirement" if intent == "DELETE_REQUIREMENT" else "gatherer",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "locked"
    assert body["pending_merge"] is False
    assert body["pending_action_id"] is None
    assert body["no_changes"] is True
    assert "REQ-001 is locked" in body["message"]
    assert await db_session.scalar(
        select(func.count()).select_from(PendingActionModel)
    ) == before

    state = (await client.get(f"/api/project/{seeded_project}")).json()
    assert state["requirements"][0]["title"] == "Transfer approval"
    assert state["requirements"][0]["is_locked"] is True

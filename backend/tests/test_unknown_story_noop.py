"""Unknown explicit story IDs must not produce empty merge actions."""
import pytest
from sqlalchemy import func, select

from app.models import PendingActionModel
from app.repositories.requirement_state import RequirementStateRepository
from tests.test_documents import client, db_session, seeded_project


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_input", "missing_code", "available_code"),
    [
        ("Change US-999 so approval requires biometrics", "US-999", "US-001"),
        ("Change REQ-999 so approval requires biometrics", "REQ-999", "REQ-001"),
    ],
)
async def test_unknown_explicit_artifact_id_returns_no_change(
    client, db_session, seeded_project, monkeypatch, raw_input, missing_code, available_code
):
    import app.routes.requirements as requirement_routes
    import app.semantic_service as semantic_service

    await client.get(f"/api/project/{seeded_project}")
    await RequirementStateRepository.save_or_update(seeded_project, {
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Transfer approval",
            "description": "",
            "user_stories": [{
                "ticket_code": "US-001",
                "story_title": "Approve transfer",
                "as_a": "checker",
                "i_want_to": "approve a transfer",
                "so_that": "it can be processed",
                "acceptance_criteria": ["Given a pending transfer, when approved, then it is processed"],
            }],
        }],
    }, db_session)
    await db_session.commit()

    async def update_intent(*args, **kwargs):
        return {"intent": "UPDATE_REQUIREMENT", "confidence": 0.99, "reason": "explicit update"}

    async def workflow_must_not_run(*args, **kwargs):
        raise AssertionError("unknown explicit IDs must stop before the workflow")

    monkeypatch.setattr(semantic_service, "detect_requirement_intent", update_intent)
    monkeypatch.setattr(requirement_routes.prd_workflow, "ainvoke", workflow_must_not_run)

    before = await db_session.scalar(select(func.count()).select_from(PendingActionModel))
    response = await client.post("/api/process-requirements", json={
        "project_id": seeded_project,
        "raw_input": raw_input,
        "target_agent": "gatherer",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "no_change"
    assert body["no_changes"] is True
    assert body["pending_merge"] is False
    assert body["pending_action_id"] is None
    assert f"{missing_code} does not exist" in body["message"]
    assert available_code in body["message"]
    after = await db_session.scalar(select(func.count()).select_from(PendingActionModel))
    assert after == before


@pytest.mark.asyncio
async def test_matcher_clarification_is_persisted_without_merge_preview(
    client, db_session, seeded_project, monkeypatch
):
    """Ambiguous matcher output must ask first and never stage unchanged data."""
    import app.routes.requirements as requirement_routes
    import app.semantic_service as semantic_service

    await RequirementStateRepository.save_or_update(seeded_project, {
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Transfer approval",
            "description": "Approval controls",
            "user_stories": [],
        }],
    }, db_session)
    await db_session.commit()

    async def update_intent(*_args, **_kwargs):
        return {"intent": "UPDATE_REQUIREMENT", "confidence": 0.99}

    async def ambiguous_workflow(state):
        next_state = dict(state)
        requirement_state = dict(state["requirement_state"])
        requirement_state.update({
            "validation_status": "invalid",
            "clarification_questions": [{
                "checklist_category": "Requirement Matcher Ambiguity",
                "question_text": "Which requirement should be updated?",
                "is_resolved": False,
            }],
        })
        next_state.update({
            "requirement_state": requirement_state,
            "requirement_match": {
                "status": "AMBIGUOUS",
                "confidence": 0.4,
                "candidates": ["REQ-001"],
            },
            "detected_intent": "UPDATE_REQUIREMENT",
        })
        return next_state

    monkeypatch.setattr(semantic_service, "detect_requirement_intent", update_intent)
    monkeypatch.setattr(requirement_routes.prd_workflow, "ainvoke", ambiguous_workflow)

    before = await db_session.scalar(select(func.count()).select_from(PendingActionModel))
    response = await client.post("/api/process-requirements", json={
        "project_id": seeded_project,
        "raw_input": "Update the approval flow",
        "target_agent": "gatherer",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["no_changes"] is True
    assert body["pending_merge"] is False
    assert body["pending_action_id"] is None
    assert "No changes were made because I need clarification" in body["message"]
    assert body["audit_result"]["clarification_questions"][0]["id"]
    assert body["audit_result"]["clarification_questions"][0]["question_text"] == (
        "Which requirement should be updated?"
    )
    assert await db_session.scalar(
        select(func.count()).select_from(PendingActionModel)
    ) == before

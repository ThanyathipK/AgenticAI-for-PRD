"""End-to-end regression coverage for Auditor clarification resolution.

The suite protects the user-visible contract:

* questions retain transparent provenance and survive reloads;
* unanswered questions stay unresolved;
* answers reach the Gatherer with their original evidence and are processed in
  bounded, topic-aware batches;
* only material requirement changes produce a review proposal; and
* Gatherer follow-ups are deferred to the next explicit validation run; and
* submitting answers never invokes the Auditor automatically.
"""

import json

import pytest
from sqlalchemy import func, select, update

from app.models import ClarificationQuestionModel, PendingActionModel
from tests.test_documents import client, db_session, seeded_project


def _question(index, source_reference):
    return {
        "question_text": f"Clarification question {index}?",
        "checklist_category": "Beneficiary governance",
        "target_user_story_id": "PAY-001",
        "source_references": [source_reference] if source_reference else [],
    }


@pytest.mark.asyncio
async def test_answers_are_batched_without_empty_proposal_or_automatic_reaudit(
    client, db_session, seeded_project, monkeypatch
):
    import app.agents as agents
    import app.routes.requirements as requirements_route
    import app.semantic_service as semantic

    upload = await client.post(
        f"/api/project/{seeded_project}/documents/upload",
        files={
            "file": (
                "beneficiary-policy.md",
                b"# Verification\nMaker-checker approval is mandatory.",
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    source = {
        "document_id": document_id,
        "document_name": "beneficiary-policy.md",
        "section": "Verification",
        "excerpt": "Maker-checker approval is mandatory.",
    }

    async def intent(*_args, **_kwargs):
        return {"intent": "GENERAL_CHAT", "confidence": 1.0}

    audit_calls = 0

    async def auditor_inference(*_args, **_kwargs):
        nonlocal audit_calls
        audit_calls += 1
        return {
            "is_valid": False,
            "verdict": "needs_clarification",
            "findings": [],
            "passed_checks": [],
            "failed_checks": [],
            "clarification_questions": [
                _question(index, source if index < 3 else None)
                for index in range(6)
            ],
        }

    async def save_message(**_kwargs):
        return {}

    monkeypatch.setattr(semantic, "detect_requirement_intent", intent)
    monkeypatch.setattr(agents, "invoke_llm_structured", auditor_inference)
    monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", save_message)

    audit = await client.post(
        "/api/process-requirements",
        json={
            "project_id": seeded_project,
            "raw_input": "",
            "target_agent": "auditor",
        },
    )
    assert audit.status_code == 200, audit.text
    assert audit_calls == 1

    loaded = (await client.get(f"/api/project/{seeded_project}")).json()
    questions = loaded["clarification_questions"]
    assert len(questions) == 6
    assert all(not question["is_resolved"] for question in questions)
    assert {question["provenance"]["type"] for question in questions} == {
        "Project file",
        "System analysis",
    }
    project_question = next(
        question for question in questions
        if question["provenance"]["type"] == "Project file"
    )
    assert project_question["provenance"] == {
        "type": "Project file",
        "document": "beneficiary-policy.md",
        "section": "Verification",
    }

    gatherer_batches = []

    async def gatherer(items, requirement_context):
        gatherer_batches.append((items, requirement_context))
        return {
            "classifications": [
                {
                    "question_id": item["question_id"],
                    "classification": "UPDATE",
                    "target_requirement_id": item["target_requirement_id"],
                    "rationale": "Stakeholder supplied a requirement decision.",
                }
                for item in items
            ],
            "proposed_requirements": [],
        }

    monkeypatch.setattr(
        requirements_route, "gather_clarification_answer_batch", gatherer
    )

    # Submit five answers. The sixth question must remain unresolved after a
    # database reload, while the five answers are sent as batches of 4 and 1.
    first_answers = {
        question["id"]: f"Approved answer {index}"
        for index, question in enumerate(questions[:5])
    }
    submitted = await client.post(
        "/api/clarification/submit",
        json={"project_id": seeded_project, "answers": first_answers},
    )
    assert submitted.status_code == 200, submitted.text
    assert audit_calls == 1, "answer submission must not automatically run Auditor"
    assert [len(items) for items, _context in gatherer_batches] == [4, 1]
    assert all(len(items) <= 4 for items, _context in gatherer_batches)

    sent_items = [item for items, _context in gatherer_batches for item in items]
    assert len(sent_items) == 5
    for item in sent_items:
        original = next(question for question in questions if question["id"] == item["question_id"])
        assert item["question"] == original["question_text"]
        assert item["provenance"] == original["provenance"]
        assert item["target_requirement_id"] == "PAY-001"
        assert "requirement_context" in item

    reloaded = (await client.get(f"/api/project/{seeded_project}")).json()
    unresolved = [q for q in reloaded["clarification_questions"] if not q["is_resolved"]]
    assert [q["id"] for q in unresolved] == [questions[5]["id"]]
    assert all(
        q["user_answer"] for q in reloaded["clarification_questions"] if q["is_resolved"]
    )
    pending_count = await db_session.scalar(
        select(func.count()).select_from(PendingActionModel)
    )
    assert pending_count == 0
    assert submitted.json()["pending_merge"] is False
    assert "no requirement fields changed" in submitted.json()["proposal_no_change_reason"]

    # Answering the remaining question still creates no empty merge window and
    # does not trigger Auditor.
    gatherer_batches.clear()
    final_submit = await client.post(
        "/api/clarification/submit",
        json={
            "project_id": seeded_project,
            "answers": {questions[5]["id"]: "The checker role is mandatory."},
        },
    )
    assert final_submit.status_code == 200, final_submit.text
    assert audit_calls == 1
    assert [len(items) for items, _context in gatherer_batches] == [1]
    assert await db_session.scalar(
        select(func.count()).select_from(PendingActionModel)
    ) == 0

    final_state = (await client.get(f"/api/project/{seeded_project}")).json()
    assert all(q["is_resolved"] for q in final_state["clarification_questions"])
    assert final_state["current_workflow_state"] == "AUDIT_PENDING"
    assert final_state["validation_status"] == "invalid"


@pytest.mark.asyncio
async def test_gatherer_failure_saves_answers_and_stages_review_warning(
    client, db_session, seeded_project, monkeypatch
):
    """A malformed/offline LM Studio response must not discard user answers."""
    import app.routes.requirements as requirements_route
    from app.repositories import RequirementStateRepository

    await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "clarification_questions": [{
                "question_text": "Who verifies the beneficiary?",
                "checklist_category": "Maker-checker",
                "target_user_story_id": "PAY-001",
                "is_resolved": False,
                "source_references": [],
                "provenance": {"type": "System analysis"},
                "requirement_context": {"requirement_code": "REQ-001"},
            }],
            "validation_status": "invalid",
        },
        db_session,
    )
    await db_session.commit()
    state = (await client.get(f"/api/project/{seeded_project}")).json()
    question = state["clarification_questions"][0]

    async def broken_gatherer(*_args, **_kwargs):
        raise RuntimeError("LM Studio returned malformed JSON")

    monkeypatch.setattr(
        requirements_route, "gather_clarification_answer_batch", broken_gatherer
    )
    response = await client.post(
        "/api/clarification/submit",
        json={
            "project_id": seeded_project,
            "answers": {question["id"]: "Operations checker verifies it."},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "Answers were saved" in body["proposal_generation_warning"]
    assert body["clarification_answer_classifications"][0]["classification"] == "NO_CHANGE"
    reloaded = (await client.get(f"/api/project/{seeded_project}")).json()
    assert reloaded["clarification_questions"][0]["is_resolved"] is True
    assert reloaded["clarification_questions"][0]["user_answer"] == (
        "Operations checker verifies it."
    )
    assert body["pending_merge"] is False
    assert body["pending_action_id"] is None
    assert await db_session.scalar(
        select(func.count()).select_from(PendingActionModel)
    ) == 0


@pytest.mark.asyncio
async def test_gatherer_follow_up_does_not_replace_or_append_questions_on_submit(
    client, db_session, seeded_project, monkeypatch
):
    """Only a later Validate action may introduce a new clarification question."""
    import app.routes.requirements as requirements_route
    from app.repositories import RequirementStateRepository

    await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "clarification_questions": [
                {
                    "question_text": "Who approves the payment?",
                    "checklist_category": "Approval",
                    "target_user_story_id": "PAY-001",
                    "is_resolved": False,
                },
                {
                    "question_text": "What is the approval threshold?",
                    "checklist_category": "Approval",
                    "target_user_story_id": "PAY-001",
                    "is_resolved": False,
                },
            ],
            "validation_status": "invalid",
        },
        db_session,
    )
    await db_session.commit()
    original = (await client.get(f"/api/project/{seeded_project}")).json()[
        "clarification_questions"
    ]

    async def gatherer(items, _requirement_context):
        return {
            "classifications": [{
                "question_id": items[0]["question_id"],
                "classification": "FOLLOW_UP",
                "target_requirement_id": items[0]["target_requirement_id"],
                "rationale": "The named role needs more detail.",
                "follow_up_question": "Which operations role approves it?",
            }],
            "proposed_requirements": [],
        }

    monkeypatch.setattr(
        requirements_route, "gather_clarification_answer_batch", gatherer
    )
    response = await client.post(
        "/api/clarification/submit",
        json={
            "project_id": seeded_project,
            "answers": {original[0]["id"]: "An operations role approves it."},
        },
    )

    assert response.status_code == 200, response.text
    questions = response.json()["clarification_questions"]
    assert [question["id"] for question in questions] == [
        question["id"] for question in original
    ]
    assert questions[0]["is_resolved"] is True
    assert questions[0]["user_answer"] == "An operations role approves it."
    assert questions[1]["is_resolved"] is False
    assert questions[1]["question_text"] == original[1]["question_text"]
    assert all(
        question["question_text"] != "Which operations role approves it?"
        for question in questions
    )


@pytest.mark.asyncio
async def test_submitting_an_answer_collapses_legacy_duplicate_questions(
    client, db_session, seeded_project, monkeypatch
):
    import app.routes.requirements as requirements_route
    from app.repositories import RequirementStateRepository

    duplicate = {
        "question_text": "Who approves the payment?",
        "checklist_category": "Approval",
        "target_user_story_id": "PAY-001",
        "is_resolved": False,
    }
    await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "clarification_questions": [
                duplicate,
                {**duplicate, "target_user_story_id": "REQ-002"},
            ],
            "validation_status": "invalid",
        },
        db_session,
    )
    await db_session.commit()
    original = (await client.get(f"/api/project/{seeded_project}")).json()[
        "clarification_questions"
    ]
    assert len(original) == 2

    async def gatherer(_items, _requirement_context):
        return {"classifications": [], "proposed_requirements": []}

    monkeypatch.setattr(
        requirements_route, "gather_clarification_answer_batch", gatherer
    )
    response = await client.post(
        "/api/clarification/submit",
        json={
            "project_id": seeded_project,
            "answers": {original[0]["id"]: "The operations manager approves it."},
        },
    )

    assert response.status_code == 200, response.text
    questions = response.json()["clarification_questions"]
    assert len(questions) == 1, json.dumps(questions, indent=2)
    assert questions[0]["is_resolved"] is True
    assert questions[0]["user_answer"] == "The operations manager approves it."


@pytest.mark.asyncio
async def test_project_reload_compacts_legacy_oversized_question_context(
    client, db_session, seeded_project
):
    """Old full-requirement snapshots must not cause response validation 500s."""
    from app.repositories import RequirementStateRepository

    await RequirementStateRepository.save_or_update(
        seeded_project,
        {
            "clarification_questions": [{
                "question_text": "Which verification rule applies?",
                "checklist_category": "Verification",
                "target_user_story_id": "US-003",
                "is_resolved": False,
                "provenance": {"type": "System analysis"},
            }],
        },
        db_session,
    )
    await db_session.flush()
    question_id = await db_session.scalar(select(ClarificationQuestionModel.id))
    legacy_context = json.dumps({
        "requirement_code": "REQ-001",
        "title": "Beneficiary Management",
        "description": "Domestic beneficiary operations",
        "user_stories": [
            {
                "ticket_code": f"US-{index:03d}",
                "story_title": f"Story {index}",
                "as_a": "Corporate user",
                "i_want_to": "manage beneficiary details " + ("x" * 500),
                "so_that": "payments remain controlled",
                "acceptance_criteria": ["Given a user when an action occurs then it is controlled" * 10],
            }
            for index in range(1, 15)
        ],
    })
    assert len(legacy_context) > 4000
    await db_session.execute(
        update(ClarificationQuestionModel)
        .where(ClarificationQuestionModel.id == question_id)
        .values(requirement_context=legacy_context, target_requirement_id="US-003")
    )
    await db_session.commit()

    response = await client.get(f"/api/project/{seeded_project}")

    assert response.status_code == 200, response.text
    context = response.json()["clarification_questions"][0]["requirement_context"]
    assert len(context) <= 4000
    parsed = json.loads(context)
    assert parsed["requirement_code"] == "REQ-001"
    assert parsed["target_story"]["ticket_code"] == "US-003"
    assert "US-014" not in context

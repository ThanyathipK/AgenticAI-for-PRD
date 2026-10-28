"""Offline unit tests for app.agents.gatherer_node.

Locks in the extraction-quality fixes:
- ONE merge over the flattened LLM output -> no story duplication across
  requirement groups (the old code merged the whole backlog once PER group).
- Unmentioned existing stories are preserved under their ORIGINAL requirement.
- The LLM ``version`` echo is ignored; the version bumps exactly once on real
  changes and never regresses.
- No-op / GENERAL_CHAT invocations never touch the requirement state.
- Low-confidence semantic changes produce a user-visible clarification message.
- Requirement locks protect all stories under the requirement.
- A recycled requirement code for a different feature is minted a fresh code.
- The persisted epic name is kept unless the user explicitly renames it.
- Intent classification reaches the prompt truthfully: the real classifier
  confidence (propagated from the route/matcher node) is quoted and a low
  confidence makes the gatherer prefer preserving over inventing.
"""
import sys

import pytest

sys.path.insert(0, "/Users/thanyathip/Desktop/agenticdia/backend")

import copy

import app.agents as agents

US_001 = {
    "ticket_code": "US-001",
    "story_title": "Submit refund request online",
    "as_a": "Retail Customer",
    "i_want_to": "submit a refund request",
    "so_that": "I get my money back",
    "acceptance_criteria": [
        "Given a valid transaction ID, when the form is submitted, then the refund status is PENDING"
    ],
}
US_002 = {
    "ticket_code": "US-002",
    "story_title": "Track refund status",
    "as_a": "Retail Customer",
    "i_want_to": "track my refund status",
    "so_that": "I know when the money arrives",
    "acceptance_criteria": [
        "Given a submitted refund, when I open the tracking page, then I see the current status"
    ],
}
US_003 = {
    "ticket_code": "US-003",
    "story_title": "Email refund updates",
    "as_a": "Retail Customer",
    "i_want_to": "receive email updates",
    "so_that": "I stay informed without checking",
    "acceptance_criteria": [
        "Given a refund status change, when it happens, then an email notification is sent"
    ],
}

REQ_STATE = {
    "project_id": "p1",
    "project_name": "PromptPay Refund Portal",
    "requirements": [
        {
            "requirement_code": "REQ-001",
            "title": "Refund Request Submission",
            "description": "Submit and track refund requests",
            "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
        },
        {
            "requirement_code": "REQ-002",
            "title": "Refund Notifications",
            "description": "",
            "user_stories": [copy.deepcopy(US_003)],
        },
    ],
    "business_goals": [],
    "actors": [],
    "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002), copy.deepcopy(US_003)],
    "acceptance_criteria": [
        US_001["acceptance_criteria"][0],
        US_002["acceptance_criteria"][0],
        US_003["acceptance_criteria"][0],
    ],
    "clarification_questions": [],
    "validation_status": "pending",
    "generated_prd": "",
    "generated_diagrams": "",
    "current_workflow_state": "gatherer_node",
    "requirements_revision": 5,
    "version_number": 5,
    "updated_at": None,
}


def _patch(monkeypatch, req_state, llm_result=None, semantic_recs=None):
    """Patch DB/LLM collaborators; returns (saved_messages, llm_calls)."""
    saved = []
    llm_calls = []

    async def fake_state(project_id, session=None, current_version=1):
        return copy.deepcopy(req_state)

    async def fake_save(**kwargs):
        saved.append(kwargs)
        return None

    async def fake_semantic(raw_input, current_stories):
        return copy.deepcopy(semantic_recs or [])

    async def fake_intent(raw_input, current_stories):
        return {"intent": "UPDATE_REQUIREMENT", "confidence": 0.95, "reason": "test"}

    async def fake_llm(*args, **kwargs):
        llm_calls.append(kwargs)
        return copy.deepcopy(llm_result)

    monkeypatch.setattr(agents, "get_or_init_requirement_state", fake_state)
    monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", fake_save)
    monkeypatch.setattr(agents, "detect_semantic_changes", fake_semantic)
    monkeypatch.setattr(agents, "detect_requirement_intent", fake_intent)
    monkeypatch.setattr(agents, "invoke_llm_structured", fake_llm)
    return saved, llm_calls


def _story_codes_by_requirement(requirements):
    return {
        r["requirement_code"]: sorted(s["ticket_code"] for s in r["user_stories"])
        for r in requirements
    }


@pytest.mark.asyncio
async def test_structured_draft_without_raw_input_preserves_epic_and_skips_llm(monkeypatch):
    """Frontend draft re-sync must not reference epic_name before assignment."""
    _saved, llm_calls = _patch(monkeypatch, REQ_STATE, llm_result={})

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "",
        "current_version": 5,
        "structured_requirements": {
            "epic_name": "Refund Request Submission",
            "version": 5,
            "requirements": copy.deepcopy(REQ_STATE["requirements"]),
        },
    })

    assert result["requirement_state"]["epic_name"] == "Refund Request Submission"
    assert result["structured_requirements"]["epic_name"] == "Refund Request Submission"
    assert _story_codes_by_requirement(result["structured_requirements"]["requirements"]) == {
        "REQ-001": ["US-001", "US-002"],
        "REQ-002": ["US-003"],
    }
    assert llm_calls == []


@pytest.mark.asyncio
async def test_unknown_command_requests_clarification_instead_of_claiming_success(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def command_workflow(_raw_input):
        return {"workflow": "COMMAND", "confidence": 0.95, "reason": "test"}

    monkeypatch.setattr(agents, "classify_workflow", command_workflow)
    result = await agents.workflow_router_node({
        "project_id": "p1",
        "raw_input": "perform the operation",
        "target_agent": "gatherer",
    })

    assert result["target_agent"] == "gatherer"
    assert "couldn't identify a supported command" in result["agent_message"]
    assert "successfully" not in result["agent_message"]


@pytest.mark.asyncio
async def test_matcher_resolves_explicit_parent_requirement_without_story_guess(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def story_matcher_must_not_run(*_args, **_kwargs):
        raise AssertionError("explicit REQ IDs must bypass story-level inference")

    monkeypatch.setattr(agents, "match_requirement", story_matcher_must_not_run)
    result = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "Change req 1 to require manager approval",
        "detected_intent": "UPDATE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })

    match = result["requirement_match"]
    assert match["parent_requirement_code"] == "REQ-001"
    assert match["confidence"] == 1.0
    assert match["status"] == "MATCHED"


@pytest.mark.asyncio
async def test_delete_matcher_supports_explicit_multiple_stories_and_remove_all(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def story_matcher_must_not_run(*_args, **_kwargs):
        raise AssertionError("explicit delete scopes must bypass story-level inference")

    monkeypatch.setattr(agents, "match_requirement", story_matcher_must_not_run)
    explicit = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "Archive US-001 and US-003",
        "detected_intent": "DELETE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })
    assert explicit["requirement_match"]["matched_requirement_ids"] == ["US-001", "US-003"]

    all_items = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "Remove all requirements",
        "detected_intent": "DELETE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })
    assert all_items["requirement_match"]["scope"] == "ALL_REQUIREMENTS"
    assert all_items["requirement_match"]["parent_requirement_codes"] == ["REQ-001", "REQ-002"]


@pytest.mark.asyncio
async def test_update_matcher_resolves_explicit_story_without_llm(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def story_matcher_must_not_run(*_args, **_kwargs):
        raise AssertionError("explicit US IDs must bypass story-level inference")

    monkeypatch.setattr(agents, "match_requirement", story_matcher_must_not_run)
    result = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "Update US-002 and replace all acceptance criteria",
        "detected_intent": "UPDATE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })

    match = result["requirement_match"]
    assert match["matched_requirement_id"] == "US-002"
    assert match["action"] == "UPDATE"
    assert match["confidence"] == 1.0


@pytest.mark.asyncio
async def test_delete_matcher_does_not_guess_ambiguous_bare_number(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def story_matcher_must_not_run(*_args, **_kwargs):
        raise AssertionError("bare archive numbers must be resolved deterministically")

    monkeypatch.setattr(agents, "match_requirement", story_matcher_must_not_run)
    result = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "remove 001",
        "detected_intent": "DELETE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })

    match = result["requirement_match"]
    assert match["status"] == "AMBIGUOUS"
    assert match["candidates"] == ["US-001", "REQ-001"]
    assert result["requirement_state"]["validation_status"] == "invalid"


@pytest.mark.asyncio
async def test_delete_matcher_maps_story_scoped_acceptance_criterion(monkeypatch):
    _patch(monkeypatch, REQ_STATE, llm_result={})

    async def story_matcher_must_not_run(*_args, **_kwargs):
        raise AssertionError("explicit AC references must bypass story inference")

    monkeypatch.setattr(agents, "match_requirement", story_matcher_must_not_run)
    result = await agents.requirement_matcher_node({
        "project_id": "p1",
        "raw_input": "remove US-001-AC-001",
        "detected_intent": "DELETE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
    })

    match = result["requirement_match"]
    assert match["status"] == "MATCHED"
    assert match["acceptance_criteria_targets"][0]["label"] == "US-001-AC-001"


@pytest.mark.asyncio
async def test_delete_node_removes_only_target_acceptance_criterion(monkeypatch):
    _patch(monkeypatch, REQ_STATE)
    criterion = US_001["acceptance_criteria"][0]
    result = await agents.delete_requirement_node({
        "project_id": "p1",
        "current_version": 5,
        "requirement_match": {
            "matched_requirement_id": "US-001",
            "acceptance_criteria_targets": [{
                "story_code": "US-001",
                "ordinal": 1,
                "text": criterion,
                "label": "US-001-AC-001",
            }],
            "confidence": 1.0,
            "action": "DELETE",
            "status": "MATCHED",
        },
    })

    state = result["requirement_state"]
    assert [story["ticket_code"] for story in state["user_stories"]] == ["US-001", "US-002", "US-003"]
    assert state["user_stories"][0]["acceptance_criteria"] == []
    assert _story_codes_by_requirement(state["requirements"]) == {
        "REQ-001": ["US-001", "US-002"],
        "REQ-002": ["US-003"],
    }
    assert result["archive_story_codes"] == []
    assert "US-001-AC-001" in result["agent_message"]


@pytest.mark.asyncio
async def test_delete_node_stages_archive_by_omitting_story_until_confirmation(monkeypatch):
    saved, _ = _patch(monkeypatch, REQ_STATE)
    result = await agents.delete_requirement_node({
        "project_id": "p1",
        "current_version": 5,
        "requirement_match": {
            "matched_requirement_id": "US-002",
            "matched_requirement_ids": ["US-002"],
            "confidence": 1.0,
            "action": "DELETE",
            "status": "MATCHED",
        },
    })

    assert [story["ticket_code"] for story in result["requirement_state"]["user_stories"]] == ["US-001", "US-003"]
    assert _story_codes_by_requirement(result["requirement_state"]["requirements"]) == {
        "REQ-001": ["US-001"],
        "REQ-002": ["US-003"],
    }
    assert result["archive_story_codes"] == ["US-002"]
    assert "only after you select Save" in result["agent_message"]
    assert saved and "Archive confirmation required" in saved[0]["message"]


@pytest.mark.asyncio
async def test_delete_node_keeps_story_under_locked_requirement(monkeypatch):
    locked_state = copy.deepcopy(REQ_STATE)
    locked_state["requirements"][0]["is_locked"] = True
    locked_state["requirements"][0]["locked_by"] = "product-owner"

    _patch(monkeypatch, locked_state)
    result = await agents.delete_requirement_node({
        "project_id": "p1",
        "current_version": 5,
        "requirement_match": {
            "matched_requirement_id": "US-002",
            "confidence": 1.0,
            "action": "DELETE",
            "status": "MATCHED",
        },
    })

    assert result["requirement_state"]["user_stories"] == locked_state["user_stories"]
    assert "locked" in result["agent_message"].lower()
    assert "unlock" in result["agent_message"].lower()
    assert result["lock_blocked"] is True


@pytest.mark.asyncio
async def test_update_match_under_locked_requirement_stops_before_llm(monkeypatch):
    locked_state = copy.deepcopy(REQ_STATE)
    locked_state["requirements"][0]["is_locked"] = True

    _saved, llm_calls = _patch(monkeypatch, locked_state)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Make tracking show settlement time",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
        "requirement_match": {
            "matched_requirement_id": "US-002",
            "confidence": 0.98,
            "action": "UPDATE",
            "status": "MATCHED",
        },
    })

    assert result["lock_blocked"] is True
    assert result["requirement_state"]["user_stories"] == locked_state["user_stories"]
    assert "US-002 is locked" in result["agent_message"]
    assert llm_calls == []


@pytest.mark.asyncio
async def test_compiled_workflow_routes_delete_matcher_to_archive_node(monkeypatch):
    """The graph's branch table must include every value route_from_matcher returns."""
    _patch(monkeypatch, REQ_STATE)

    result = await agents.prd_workflow.ainvoke({
        "project_id": "p1",
        "raw_input": "Archive US-002",
        "target_agent": "delete_requirement",
        "detected_intent": "DELETE_REQUIREMENT",
        "intent_confidence": 0.99,
        "current_version": 5,
        "requirement_state": copy.deepcopy(REQ_STATE),
    })

    assert result["archive_story_codes"] == ["US-002"]
    assert result["requirement_state"]["current_workflow_state"] == "delete_requirement_node"


@pytest.mark.asyncio
async def test_delete_story_does_not_query_removed_child_locks(monkeypatch):
    class EmptyRows:
        def all(self):
            return []

    class CapturingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyRows()

    session = CapturingSession()
    _patch(monkeypatch, REQ_STATE)
    await agents.delete_requirement_node({
        "project_id": "11111111-1111-1111-1111-111111111111",
        "current_version": 5,
        "db_session": session,
        "requirement_match": {
            "matched_requirement_id": "US-002",
            "confidence": 1.0,
            "action": "DELETE",
            "status": "MATCHED",
        },
    })

    assert session.statements == []


@pytest.mark.asyncio
async def test_multi_requirement_output_does_not_duplicate_existing_stories(monkeypatch):
    """The old node ran one merge PER LLM requirement over the whole backlog,
    duplicating every existing story into every requirement group."""
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [
                    {**copy.deepcopy(US_001), "i_want_to": "submit a refund request with receipts"},
                    copy.deepcopy(US_002),
                ],
            },
            {
                "requirement_code": "REQ-002",
                "title": "Refund Notifications",
                "user_stories": [
                    copy.deepcopy(US_003),
                    {
                        "ticket_code": "US-006",
                        "story_title": "SMS refund alerts",
                        "as_a": "Retail Customer",
                        "i_want_to": "receive SMS refund alerts",
                        "so_that": "I react instantly",
                        "acceptance_criteria": [
                            "Given a refund status change, when it happens, then an SMS is sent"
                        ],
                    },
                ],
            },
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Add SMS refund alerts and make receipts mandatory on refund submission",
        "detected_intent": "CREATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    reqs = result["requirement_state"]["requirements"]
    assert _story_codes_by_requirement(reqs) == {
        "REQ-001": ["US-001", "US-002"],
        "REQ-002": ["US-003", "US-006"],
    }

    # Flat list has each story exactly once
    flat_codes = [s["ticket_code"] for s in result["requirement_state"]["user_stories"]]
    assert sorted(flat_codes) == ["US-001", "US-002", "US-003", "US-006"]
    assert len(flat_codes) == len(set(flat_codes))

    # The targeted story was updated in place
    us001 = next(s for s in result["requirement_state"]["user_stories"] if s["ticket_code"] == "US-001")
    assert us001["i_want_to"] == "submit a refund request with receipts"


@pytest.mark.asyncio
async def test_unmentioned_existing_stories_preserved_under_original_requirement(monkeypatch):
    """Stories the LLM does not re-emit must survive, still grouped under the
    requirement they belonged to (the old dead-code loop never did this)."""
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-002",
                "title": "Refund Notifications",
                "user_stories": [
                    {
                        "ticket_code": "US-006",
                        "story_title": "Push refund alerts",
                        "as_a": "Retail Customer",
                        "i_want_to": "receive push notifications",
                        "so_that": "I react instantly",
                        "acceptance_criteria": [
                            "Given a refund status change, when it happens, then a push notification is sent"
                        ],
                    }
                ],
            }
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Add push notifications for refunds",
        "detected_intent": "CREATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    reqs = result["requirement_state"]["requirements"]
    assert _story_codes_by_requirement(reqs) == {
        "REQ-001": ["US-001", "US-002"],
        "REQ-002": ["US-003", "US-006"],
    }
    # Content preserved verbatim for unmentioned stories
    us002 = next(s for s in result["requirement_state"]["user_stories"] if s["ticket_code"] == "US-002")
    assert us002["story_title"] == US_002["story_title"]
    assert us002["acceptance_criteria"] == US_002["acceptance_criteria"]



@pytest.mark.asyncio
async def test_version_bumps_once_on_change_and_ignores_llm_version_echo(monkeypatch):
    """The LLM schema defaults ``version`` to 1 and never knows the real one -
    trusting it reset the project version on every gather."""
    llm_result = {
        "epic_name": "Refund Portal",
        "version": 1,  # what the LLM echoes
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [
                    {**copy.deepcopy(US_001), "i_want_to": "submit a refund with receipt attached"}
                ],
            }
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Refunds must attach a receipt",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    assert result["current_version"] == 5
    assert result["requirement_state"]["version_number"] == 5
    assert result["requirement_state"]["requirements_revision"] == 6
    assert result["structured_requirements"]["version"] == 5


@pytest.mark.asyncio
async def test_version_stays_when_nothing_changed(monkeypatch):
    llm_result = {
        "epic_name": "Refund Portal",
        "version": 1,
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
            },
            {
                "requirement_code": "REQ-002",
                "title": "Refund Notifications",
                "user_stories": [copy.deepcopy(US_003)],
            },
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "ok",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    assert result["current_version"] == 5


@pytest.mark.asyncio
async def test_noop_guard_returns_state_unchanged(monkeypatch):
    """Empty input + empty draft used to flatten all requirements into one
    REQ-001 via the legacy fallback."""
    saved, llm_calls = _patch(monkeypatch, REQ_STATE)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "",
        "structured_requirements": {},
        "current_version": 5,
    })

    assert llm_calls == []
    assert result["detected_intent"] == "GENERAL_CHAT"
    assert result["requirement_state"]["requirements"] == REQ_STATE["requirements"]
    assert result["requirement_state"]["version_number"] == 5
    # The user is told why nothing happened
    assert saved and "didn't receive" in saved[0]["message"]
    assert saved[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_general_chat_input_is_never_extracted(monkeypatch):
    saved, llm_calls = _patch(monkeypatch, REQ_STATE)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "What is a refund window?",
        "detected_intent": "GENERAL_CHAT",
        "current_version": 5,
        "structured_requirements": {},
    })

    assert llm_calls == []
    assert result["requirement_state"]["requirements"] == REQ_STATE["requirements"]
    assert result["detected_intent"] == "GENERAL_CHAT"
    assert saved and "did not modify" in saved[0]["message"]



@pytest.mark.asyncio
async def test_low_confidence_change_asks_user_instead_of_guessing(monkeypatch):
    """The clarification branch used to save questions to state with no chat
    message, so the gather looked like a silent failure."""
    semantic_recs = [{
        "change_type": "NEW_REQUIREMENT",
        "target_requirement_id": None,
        "confidence": 0.4,
        "reason": "It is unclear whether this is a new story or an update to US-002",
        "recommended_action": "INSERT",
    }]
    saved, llm_calls = _patch(monkeypatch, REQ_STATE, semantic_recs=semantic_recs)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "maybe change that refund thing",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    assert llm_calls == []  # no extraction on ambiguity
    cqs = result["requirement_state"]["clarification_questions"]
    assert len(cqs) == 1 and cqs[0]["is_resolved"] is False
    assert result["requirement_state"]["validation_status"] == "invalid"
    assert saved and "Clarification Needed" in saved[0]["message"]


@pytest.mark.asyncio
async def test_legacy_story_lock_flag_is_ignored(monkeypatch):
    locked_state = copy.deepcopy(REQ_STATE)
    locked_state["user_stories"][1]["is_locked"] = True

    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [
                    {**copy.deepcopy(US_002), "i_want_to": "HACKED VALUE"},
                ],
            }
        ],
    }
    _patch(monkeypatch, locked_state, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "change the tracking story",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    us002 = next(
        s for s in result["requirement_state"]["user_stories"]
        if s["ticket_code"] == "US-002"
    )
    assert us002["i_want_to"] == "HACKED VALUE"
    # And still grouped under its original requirement
    grouping = _story_codes_by_requirement(result["requirement_state"]["requirements"])
    assert "US-002" in grouping["REQ-001"]



@pytest.mark.asyncio
async def test_recycled_requirement_code_gets_a_fresh_code(monkeypatch):
    """An LLM reusing REQ-001 for a totally different feature must not pollute
    the existing group - a fresh code is minted."""
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",  # recycled!
                "title": "Fraud Detection",
                "user_stories": [
                    {
                        "ticket_code": "",
                        "story_title": "Flag suspicious refunds",
                        "as_a": "Fraud Analyst",
                        "i_want_to": "flag suspicious refunds",
                        "so_that": "fraud is caught early",
                        "acceptance_criteria": [
                            "Given a refund above threshold, when scored, then it is flagged"
                        ],
                    }
                ],
            }
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "We need fraud detection for refunds",
        "detected_intent": "CREATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    grouping = _story_codes_by_requirement(result["requirement_state"]["requirements"])
    # Existing groups untouched + a NEW group minted after REQ-002
    assert grouping["REQ-001"] == ["US-001", "US-002"]
    assert grouping["REQ-002"] == ["US-003"]
    assert grouping.get("REQ-003") == ["US-004"]
    new_group = next(r for r in result["requirement_state"]["requirements"] if r["requirement_code"] == "REQ-003")
    assert new_group["title"] == "Fraud Detection"


@pytest.mark.asyncio
async def test_epic_name_kept_unless_explicit_rename(monkeypatch):
    no_change_result = {
        "epic_name": "Totally Different Epic",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=no_change_result)

    # No rename request -> persisted epic stays even though the LLM suggested one
    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "small wording tweak on US-001",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })
    assert result["structured_requirements"]["epic_name"] == "Refund Request Submission"
    assert result["requirement_state"]["epic_name"] == "Refund Request Submission"

    # Explicit rename phrasing (missed by the old substring check) -> LLM epic applied
    rename_result = {
        "epic_name": "Payments Hub",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    _patch(monkeypatch, REQ_STATE, llm_result=rename_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Please rename the epic to Payments Hub",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })
    assert result["structured_requirements"]["epic_name"] == "Payments Hub"
    assert result["requirement_state"]["epic_name"] == "Payments Hub"


@pytest.mark.asyncio
async def test_removed_story_archives_and_empties_its_requirement_group(monkeypatch):
    semantic_recs = [{
        "change_type": "REMOVE_REQUIREMENT",
        "target_requirement_id": "US-003",
        "confidence": 0.95,
        "reason": "User asked to remove email notifications",
        "recommended_action": "ARCHIVE",
    }]
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
            }
        ],
    }
    saved, _llm_calls = _patch(monkeypatch, REQ_STATE, llm_result=llm_result, semantic_recs=semantic_recs)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Remove the email notification feature",
        "detected_intent": "DELETE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    grouping = _story_codes_by_requirement(result["requirement_state"]["requirements"])
    assert grouping == {"REQ-001": ["US-001", "US-002"]}  # REQ-002 dropped (empty)
    flat_codes = [s["ticket_code"] for s in result["requirement_state"]["user_stories"]]
    assert "US-003" not in flat_codes
    # Archiving is a requirement change, not a PRD release.
    assert result["current_version"] == 5
    assert result["requirement_state"]["requirements_revision"] == 6
    # Summary message mentions the removal
    assert saved and "Would archive" in saved[0]["message"] and "US-003" in saved[0]["message"]



# ==========================================
# PURE HELPER TESTS
# ==========================================
def test_sanitize_incoming_story_normalizes_drafts():
    dirty = {
        "ticket_code": " us-007 ",
        "story_title": "  Export statements  ",
        "as_a": "Back Office User",
        "i_want_to": "export statements",
        "so_that": "",
        "acceptance_criteria": [
            "Given a period, when export runs, then a CSV downloads",
            "given a period, when export runs, then a csv downloads",  # dupe (case/space)
            "   ",
            None,
        ],
    }
    clean = agents._sanitize_incoming_story(dirty)
    assert clean["ticket_code"] == "US-007"
    assert clean["story_title"] == "Export statements"
    assert clean["acceptance_criteria"] == [
        "Given a period, when export runs, then a CSV downloads"
    ]


def test_sanitize_incoming_story_rejects_unusable_entries():
    assert agents._sanitize_incoming_story(None) is None
    assert agents._sanitize_incoming_story("not a dict") is None
    assert agents._sanitize_incoming_story({"story_title": "", "i_want_to": ""}) is None


def test_sanitize_incoming_story_coerces_string_criteria():
    story = agents._sanitize_incoming_story({
        "story_title": "T",
        "i_want_to": "W",
        "acceptance_criteria": "Given x, when y, then z",
    })
    assert story["acceptance_criteria"] == ["Given x, when y, then z"]


def test_normalize_and_generate_requirement_codes():
    assert agents._normalize_requirement_code("req-2") == "REQ-002"
    assert agents._normalize_requirement_code("REQ-012") == "REQ-012"
    assert agents._normalize_requirement_code("junk") == ""
    assert agents._normalize_requirement_code(None) == ""
    assert agents._generate_next_requirement_code({"REQ-001", "REQ-004"}) == "REQ-005"


def test_renumber_fresh_project_codes_starts_at_req_001():
    """A fresh board whose LLM draft starts at REQ-002 (copied verbatim from
    the old gatherer prompt example) is renumbered 1..N in declaration order —
    the user-visible bug 'requirements start at REQ-002'."""
    meta = {
        "REQ-002": {"title": "Refunds", "description": ""},
        "REQ-003": {"title": "Payments", "description": ""},
    }
    renumber = agents._renumber_fresh_project_codes([], meta)
    assert renumber == {"REQ-002": "REQ-001", "REQ-003": "REQ-002"}


def test_renumber_fresh_project_codes_noops_when_already_sequential():
    meta = {
        "REQ-001": {"title": "Refunds", "description": ""},
        "REQ-002": {"title": "Payments", "description": ""},
    }
    assert agents._renumber_fresh_project_codes([], meta) == {}


def test_renumber_fresh_project_codes_never_touches_an_existing_board():
    """Legacy codes must never be renumbered: the PRD text and traceability
    references match the codes already stored."""
    meta = {"REQ-005": {"title": "New group", "description": ""}}
    existing = [{"requirement_code": "REQ-001", "title": "Existing"}]
    assert agents._renumber_fresh_project_codes(existing, meta) == {}
    assert agents._renumber_fresh_project_codes(existing, {}) == {}
    assert agents._renumber_fresh_project_codes([], {}) == {}


@pytest.mark.asyncio
async def test_first_requirement_of_a_fresh_project_is_req_001(monkeypatch):
    """The reported bug: a brand-new project's first requirement was REQ-002.

    The route hands the gatherer a legacy payload (placeholder epic name + no
    stories + no ``requirements`` key) for every new project. The gatherer used
    to fabricate an empty ``REQ-001`` group from it, which made the board look
    non-empty, skipped the fresh-project renumbering and was then dropped for
    having no stories — leaving the user's first requirement as the LLM's
    REQ-002.
    """
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                # Copied verbatim from the gatherer prompt's "new requirement"
                # example — the reason the board used to start at REQ-002.
                "requirement_code": "REQ-002",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    fresh_state = {
        "project_id": "p1",
        "project_name": "",
        "requirements": [],
        "business_goals": [],
        "actors": [],
        "user_stories": [],
        "acceptance_criteria": [],
        "clarification_questions": [],
        "validation_status": "pending",
        "generated_prd": "",
        "generated_diagrams": "",
        "current_workflow_state": "gatherer_node",
        "version_number": 1,
        "updated_at": None,
    }
    _patch(monkeypatch, fresh_state, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Add refund request submission",
        "detected_intent": "CREATE_REQUIREMENT",
        "current_version": 1,
        # Exactly what the route builds for a brand-new project: the frontend's
        # placeholder epic name, no stories and no requirements key.
        "structured_requirements": {
            "epic_name": "Structured Requirements Draft",
            "version": 1,
            "user_stories": [],
        },
    })

    reqs = result["requirement_state"]["requirements"]
    assert _story_codes_by_requirement(reqs) == {"REQ-001": ["US-001"]}
    # The phantom group's placeholder title never reaches the board.
    assert [r["title"] for r in reqs] == ["Refund Request Submission"]


@pytest.mark.asyncio
async def test_legacy_payload_still_preserves_persisted_requirement_identity(monkeypatch):
    """The guard above must not break the case it exists for: a legacy payload
    that DOES carry stories still resolves to the persisted requirement code and
    title instead of minting a fresh/hardcoded group."""
    single_group_state = {
        **copy.deepcopy(REQ_STATE),
        "requirements": [copy.deepcopy(REQ_STATE["requirements"][0])],
        "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
    }
    llm_result = {
        "epic_name": "Refund Request Submission",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    _patch(monkeypatch, single_group_state, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Refund submission wording changed",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {
            "epic_name": "Refund Request Submission",
            "version": 5,
            "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
        },
    })

    reqs = result["requirement_state"]["requirements"]
    # The persisted code/title are reused — no fresh REQ-002 group is minted and
    # the unmentioned story stays under its requirement.
    assert _story_codes_by_requirement(reqs) == {"REQ-001": ["US-001", "US-002"]}
    assert reqs[0]["title"] == "Refund Request Submission"


def test_epic_rename_regex_understands_natural_phrasing():
    assert agents._user_requests_epic_rename("rename the epic to Payments Hub")
    assert agents._user_requests_epic_rename("The epic should be called Payments")
    assert agents._user_requests_epic_rename("Let's change the epic name please")
    assert not agents._user_requests_epic_rename("Add a refund epic story")
    assert not agents._user_requests_epic_rename("Update US-001 please")
    assert not agents._user_requests_epic_rename("")


def test_build_change_summary_lists_ticket_codes():
    report = {
        "US-006": {"action": "created"},
        "US-001": {"action": "updated"},
        "US-003": {"action": "archived"},
        "US-002": {"action": "unchanged"},
    }
    msg = agents._build_change_summary(report)
    assert msg.startswith("Requirements update preview ready.")
    assert "US-006" in msg and "Would add" in msg
    assert "US-001" in msg and "Would update" in msg
    assert "US-003" in msg and "Would archive" in msg
    assert "US-002" not in msg  # unchanged stories are not noise


def test_build_change_summary_reports_no_changes():
    msg = agents._build_change_summary({"US-001": {"action": "unchanged"}})
    assert "No changes were needed" in msg


def test_build_intent_guidance_covers_all_intents():
    guidance_by_intent = {
        "CREATE_REQUIREMENT": "ADD something NEW",
        "UPDATE_REQUIREMENT": "MODIFY",
        "DELETE_REQUIREMENT": "REMOVE",
        "CLARIFY_REQUIREMENT": "clarification",
    }
    for intent, marker in guidance_by_intent.items():
        guidance = agents._build_intent_guidance(intent, 0.9, has_existing_stories=True)
        assert marker in guidance, f"guidance for {intent} misses '{marker}': {guidance}"

    first = agents._build_intent_guidance("CREATE_REQUIREMENT", 0.9, has_existing_stories=False)
    assert "FIRST extraction" in first

    targeted = agents._build_intent_guidance(
        "UPDATE_REQUIREMENT", 0.9,
        requirement_match={"matched_requirement_id": "US-002"},
        has_existing_stories=True,
    )
    assert "US-002" in targeted


def test_format_story_context_lines_shared_helper():
    lines = agents.format_story_context_lines([US_001])
    assert len(lines) == 1
    assert "US-001" in lines[0]
    assert "Submit refund request online" in lines[0]
    assert "Given a valid transaction ID" in lines[0]


# ==========================================
# INTENT CONFIDENCE + LOCKED CONTEXT
# ==========================================
def test_build_intent_guidance_hedges_on_low_confidence():
    low = agents._build_intent_guidance("UPDATE_REQUIREMENT", 0.30, has_existing_stories=True)
    assert "CAUTION" in low and "0.30" in low
    # The hedge must also be present on a first extraction (nothing to preserve,
    # but the model still must not over-extract).
    low_first = agents._build_intent_guidance("UPDATE_REQUIREMENT", 0.30, has_existing_stories=False)
    assert "CAUTION" in low_first

    high = agents._build_intent_guidance("UPDATE_REQUIREMENT", 0.95, has_existing_stories=True)
    assert "CAUTION" not in high


@pytest.mark.asyncio
async def test_intent_confidence_from_state_reaches_the_prompt(monkeypatch):
    """The gatherer used to hardcode confidence 1.0, so the prompt always claimed
    a perfect classification even when the matcher node was genuinely unsure."""
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    _saved, llm_calls = _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "maybe tweak the refund form",
        "detected_intent": "UPDATE_REQUIREMENT",
        "intent_confidence": 0.42,
        "current_version": 5,
        "structured_requirements": {},
    })

    variables = llm_calls[0]["variables"]
    assert "0.42" in variables["intent_guidance"]
    assert "CAUTION" in variables["intent_guidance"]


@pytest.mark.asyncio
async def test_gatherer_requests_native_json_schema(monkeypatch):
    """The initial extraction is constrained, avoiding schema-echo repairs."""
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Refund Request Submission",
            "user_stories": [copy.deepcopy(US_001)],
        }],
    }
    _saved, llm_calls = _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "update the refund form",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    assert llm_calls[0]["native_json_schema"] is True


@pytest.mark.asyncio
async def test_legacy_story_lock_flag_is_not_rendered(monkeypatch):
    locked_state = copy.deepcopy(REQ_STATE)
    locked_state["user_stories"][1]["is_locked"] = True

    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [copy.deepcopy(US_001)],
            }
        ],
    }
    _saved, llm_calls = _patch(monkeypatch, locked_state, llm_result=llm_result)

    await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "add a refund export",
        "detected_intent": "CREATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    })

    context = llm_calls[0]["variables"]["current_context"]
    assert "LOCKED STORIES" not in context
    assert "Flags: LOCKED" not in context
    assert "US-002" in context
    assert context.count("US-002") == 1

# ==========================================
# SEMANTIC RECOMMENDATION MERGE (matcher + detection)
# ==========================================
def test_merge_semantic_recommendations_keeps_both_halves_of_a_request():
    """The matcher result used to REPLACE semantic detection entirely, dropping
    any brand-new requirement described in the same message."""
    detected = [{
        "change_type": "NEW_REQUIREMENT",
        "target_requirement_id": None,
        "confidence": 0.95,
        "reason": "Also asks for QR payments.",
        "recommended_action": "INSERT",
    }]
    matcher = [{
        "change_type": "MODIFY_REQUIREMENT",
        "target_requirement_id": "US-002",
        "confidence": 0.95,
        "reason": "Matched via Requirement Matcher Agent.",
        "recommended_action": "UPDATE",
    }]

    merged = agents._merge_semantic_recommendations(detected, matcher)

    actions = {(c["target_requirement_id"], c["recommended_action"]) for c in merged}
    assert ("US-002", "UPDATE") in actions
    assert (None, "INSERT") in actions
    # The matcher's authoritative recommendation comes first (intent guidance).
    assert merged[0]["target_requirement_id"] == "US-002"


def test_merge_semantic_recommendations_matcher_wins_for_its_target():
    detected = [{
        "change_type": "MODIFY_REQUIREMENT",
        "target_requirement_id": "us-2",
        "confidence": 0.9,
        "reason": "detected",
        "recommended_action": "UPDATE",
    }]
    matcher = [{
        "change_type": "REMOVE_REQUIREMENT",
        "target_requirement_id": "US-002",
        "confidence": 0.95,
        "reason": "matcher",
        "recommended_action": "ARCHIVE",
    }]

    merged = agents._merge_semantic_recommendations(detected, matcher)

    assert len(merged) == 1
    assert merged[0]["recommended_action"] == "ARCHIVE"
    assert merged[0]["reason"] == "matcher"


def test_merge_semantic_recommendations_drops_redundant_no_change_entries():
    no_change = [{
        "change_type": "NO_MEANINGFUL_CHANGE",
        "target_requirement_id": None,
        "confidence": 0.9,
        "reason": "nothing",
        "recommended_action": "NO_CHANGE",
    }]
    matcher = [{
        "change_type": "MODIFY_REQUIREMENT",
        "target_requirement_id": "US-002",
        "confidence": 0.95,
        "reason": "matcher",
        "recommended_action": "UPDATE",
    }]

    # Alone it is the only signal available, so it is preserved...
    assert agents._merge_semantic_recommendations(no_change, []) == no_change
    # ...but next to a real change it is noise.
    merged = agents._merge_semantic_recommendations(no_change, matcher)
    assert [c["change_type"] for c in merged] == ["MODIFY_REQUIREMENT"]


@pytest.mark.asyncio
async def test_matcher_and_detected_changes_reach_the_prompt_together(monkeypatch):
    semantic_recs = [{
        "change_type": "NEW_REQUIREMENT",
        "target_requirement_id": None,
        "confidence": 0.93,
        "reason": "Also asks for QR payments.",
        "recommended_action": "INSERT",
    }]
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Refund Request Submission",
            "user_stories": [copy.deepcopy(US_001), copy.deepcopy(US_002)],
        }],
    }
    _saved, llm_calls = _patch(monkeypatch, REQ_STATE, llm_result=llm_result, semantic_recs=semantic_recs)

    await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "change the refund tracking and add QR payments",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
        "requirement_match": {"matched_requirement_id": "US-002", "action": "UPDATE", "confidence": 0.95},
    })

    recommendations = llm_calls[0]["variables"]["recommendations"]
    assert '"US-002"' in recommendations        # matcher-derived UPDATE
    assert "NEW_REQUIREMENT" in recommendations  # detected INSERT survived


@pytest.mark.asyncio
async def test_explicit_parent_requirement_cascades_into_story_and_criteria(monkeypatch):
    revised_story = {
        **copy.deepcopy(US_001),
        "i_want_to": "submit a refund request with manager approval",
        "acceptance_criteria": [
            "Given a valid request, When it is submitted, Then manager approval is required",
            "Given approval is denied, When the customer checks, Then the rejection is displayed",
        ],
    }
    llm_result = {
        "epic_name": "Refund Portal",
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Manager-approved Refund Submission",
            "description": "Refund submissions require manager approval.",
            "user_stories": [revised_story, copy.deepcopy(US_002)],
        }],
    }
    _saved, llm_calls = _patch(monkeypatch, REQ_STATE, llm_result=llm_result)

    result = await agents.gatherer_node({
        "project_id": "p1",
        "raw_input": "Change REQ-001 so every refund needs manager approval",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
        "requirement_match": {
            "matched_requirement_id": None,
            "parent_requirement_code": "REQ-001",
            "action": "UPDATE",
            "confidence": 1.0,
        },
    })

    requirement = next(
        item for item in result["requirement_state"]["requirements"]
        if item["requirement_code"] == "REQ-001"
    )
    story = next(item for item in requirement["user_stories"] if item["ticket_code"] == "US-001")
    assert requirement["title"] == "Manager-approved Refund Submission"
    assert story["i_want_to"] == "submit a refund request with manager approval"
    assert story["acceptance_criteria"] == revised_story["acceptance_criteria"]
    assert US_001["acceptance_criteria"][0] not in story["acceptance_criteria"]
    assert "explicitly targeted parent requirement REQ-001" in llm_calls[0]["variables"]["intent_guidance"]


@pytest.mark.asyncio
async def test_repeated_low_confidence_question_is_not_asked_twice(monkeypatch):
    """Re-running the gather used to append the same clarification question (and
    re-post the same chat message) every single time."""
    semantic_recs = [{
        "change_type": "NEW_REQUIREMENT",
        "target_requirement_id": None,
        "confidence": 0.4,
        "reason": "It is unclear whether this is a new story or an update to US-002",
        "recommended_action": "INSERT",
    }]
    holder = {"state": copy.deepcopy(REQ_STATE)}
    saved = []

    async def fake_state(project_id, session=None, current_version=1):
        return copy.deepcopy(holder["state"])

    async def fake_save(**kwargs):
        saved.append(kwargs)
        return None

    async def fake_semantic(raw_input, current_stories):
        return copy.deepcopy(semantic_recs)

    async def fake_intent(raw_input, current_stories):
        return {"intent": "UPDATE_REQUIREMENT", "confidence": 0.95, "reason": "test"}

    monkeypatch.setattr(agents, "get_or_init_requirement_state", fake_state)
    monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", fake_save)
    monkeypatch.setattr(agents, "detect_semantic_changes", fake_semantic)
    monkeypatch.setattr(agents, "detect_requirement_intent", fake_intent)

    state = {
        "project_id": "p1",
        "raw_input": "maybe change that refund thing",
        "detected_intent": "UPDATE_REQUIREMENT",
        "current_version": 5,
        "structured_requirements": {},
    }
    first = await agents.gatherer_node(dict(state))
    holder["state"] = first["requirement_state"]
    second = await agents.gatherer_node(dict(state))

    questions = second["requirement_state"]["clarification_questions"]
    assert len(questions) == 1                       # no duplicate pile-up
    assert len(saved) == 1                           # only one chat message
    assert questions[0]["is_resolved"] is False




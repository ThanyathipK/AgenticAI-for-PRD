"""Unit tests for the semantic change detection feature (:mod:`app.semantic_service`).

The semantic-change agent asks the local LLM to classify what a new user message
changes about the backlog, then hands the result to the persistence layer. These
tests pin the deterministic guardrails that sit between the two, because a model
answer is untrusted input:

- classifications / actions are canonicalised (aliases and the prompt-only
  ``MERGE`` action are mapped onto the four actions the writers act on),
- ``confidence`` is clamped into ``[0.0, 1.0]``,
- a hallucinated ``target_requirement_id`` never drives a mutation: it is
  downgraded to a low-confidence ``NO_CHANGE`` (below the Gatherer's
  clarification threshold) so the user is asked instead,
- one recommendation per story, with conflicting actions resolved
  deterministically,
- an empty model answer becomes an explicit ``NO_MEANINGFUL_CHANGE``,
- every returned action is actually understood by
  :func:`app.merge_service.merge_user_stories_with_report`.

No network / LM Studio access: ``invoke_llm_structured`` is monkeypatched.

Run::

    cd backend && ../venv/bin/python -m pytest tests/test_semantic_service.py -v
"""
import copy
import sys

import pytest

sys.path.insert(0, "/Users/thanyathip/Desktop/agenticdia/backend")

import app.semantic_service as semantic_service
from app.merge_service import merge_user_stories_with_report
from app.semantic_service import (
    CONFLICTING_ACTIONS_CONFIDENCE,
    DEFAULT_ACTION_BY_CHANGE_TYPE,
    SEMANTIC_ACTIONS,
    SEMANTIC_CHANGE_TYPES,
    SEMANTIC_LOW_CONFIDENCE_THRESHOLD,
    SemanticChange,
    coerce_confidence,
    normalize_change_type,
    normalize_recommended_action,
    normalize_semantic_changes,
    normalize_target_reference,
    extract_explicit_story_references,
    extract_story_ticket_codes,
    resolve_target_story,
)

US_001 = {
    "ticket_code": "US-001",
    "story_title": "Submit refund request online",
    "as_a": "Retail Customer",
    "i_want_to": "submit a refund request",
    "so_that": "I get my money back",
    "acceptance_criteria": ["Given a valid transaction ID, when the form is submitted, then the refund status is PENDING"],
}
US_002 = {
    "ticket_code": "US-002",
    "story_title": "Track refund status",
    "as_a": "Retail Customer",
    "i_want_to": "track my refund status",
    "so_that": "I know when the money arrives",
    "acceptance_criteria": ["Given a submitted refund, when I open the tracking page, then I see the current status"],
}
US_003 = {
    "ticket_code": "US-003",
    "story_title": "Email refund updates",
    "as_a": "Retail Customer",
    "i_want_to": "receive email updates",
    "so_that": "I stay informed without checking",
    "acceptance_criteria": ["Given a refund status change, when it happens, then an email notification is sent"],
}

STORIES = [copy.deepcopy(US_001), copy.deepcopy(US_002), copy.deepcopy(US_003)]


def test_explicit_story_ids_are_resolved_from_common_chat_spellings():
    assert extract_explicit_story_references("change #us 1 please", STORIES) == ["US-001"]
    assert extract_explicit_story_references("change US-999 please", STORIES) == []
    assert extract_story_ticket_codes("change US-999 and us 1") == ["US-999", "US-001"]


def make_change(**overrides):
    """A well-formed raw change entry (as the LLM would emit it)."""
    change = {
        "change_type": "MODIFY_REQUIREMENT",
        "target_requirement_id": "US-001",
        "confidence": 0.9,
        "reason": "The user wants OTP on refund submission.",
        "recommended_action": "UPDATE",
    }
    change.update(overrides)
    return change


def _patch_llm(monkeypatch, payload, capture=None, honor_validate=True):
    """Patch ``invoke_llm_structured``; ``payload`` may also be an Exception."""
    async def fake_invoke(
        llm, prompt_template, schema, variables, *,
        description="", format_instructions="", validate=None,
    ):
        if capture is not None:
            capture.append({"variables": variables, "description": description, "validate": validate})
        if isinstance(payload, Exception):
            raise payload
        if honor_validate and validate is not None and not validate(payload):
            raise RuntimeError("result did not pass validation")
        return copy.deepcopy(payload)

    monkeypatch.setattr(semantic_service, "invoke_llm_structured", fake_invoke)



# ==========================================
# VOCABULARY NORMALIZATION
# ==========================================
def test_change_type_aliases_are_canonicalised():
    assert normalize_change_type("ADD") == "NEW_REQUIREMENT"
    assert normalize_change_type("add_requirement") == "NEW_REQUIREMENT"
    assert normalize_change_type("modify-requirement") == "MODIFY_REQUIREMENT"
    assert normalize_change_type("DELETE") == "REMOVE_REQUIREMENT"
    assert normalize_change_type("no change") == "NO_MEANINGFUL_CHANGE"
    assert normalize_change_type("Merge") == "MERGE_REQUIREMENTS"
    # Canonical values pass through untouched.
    for change_type in SEMANTIC_CHANGE_TYPES:
        assert normalize_change_type(change_type) == change_type
    # Unknown values are reported as unknown instead of being invented.
    assert normalize_change_type("SOMETHING_ELSE") == ""
    assert normalize_change_type(None) == ""


def test_actions_are_canonicalised_onto_writer_supported_values():
    assert normalize_recommended_action("ADD") == "INSERT"
    assert normalize_recommended_action("modify") == "UPDATE"
    assert normalize_recommended_action("delete") == "ARCHIVE"
    assert normalize_recommended_action("keep") == "NO_CHANGE"
    # The prompt used to recommend "MERGE", which no writer understands: it must
    # land on a supported action instead of becoming a silent no-op.
    assert normalize_recommended_action("MERGE") == "UPDATE"
    assert normalize_recommended_action("MERGE_REQUIREMENTS") == "UPDATE"
    for change_type, action in DEFAULT_ACTION_BY_CHANGE_TYPE.items():
        assert normalize_recommended_action(change_type, change_type) == action
        assert action in SEMANTIC_ACTIONS
    # Garbage with no usable classification still yields a safe action.
    assert normalize_recommended_action("???", "") == "NO_CHANGE"
    assert SEMANTIC_ACTIONS == ("INSERT", "UPDATE", "ARCHIVE", "NO_CHANGE")


def test_confidence_is_clamped():
    assert coerce_confidence(0.5) == 0.5
    assert coerce_confidence(5) == 1.0
    assert coerce_confidence(-2) == 0.0
    assert coerce_confidence("0.85") == 0.85
    assert coerce_confidence("85%") == 0.85
    assert coerce_confidence("certain") == 0.5
    assert coerce_confidence(None, default=0.3) == 0.3


def test_target_reference_is_normalized_or_null():
    assert normalize_target_reference("us-7") == "US-007"
    assert normalize_target_reference("  US-001 ") == "US-001"
    assert normalize_target_reference(None) is None
    for null_token in ["", "null", "None", "n/a", "-", "unknown"]:
        assert normalize_target_reference(null_token) is None
    # A title-shaped reference is preserved verbatim so it can be matched later.
    assert normalize_target_reference("Track refund status") == "Track refund status"


def test_resolve_target_story_handles_codes_titles_and_hallucinations():
    assert resolve_target_story("US-002", STORIES) == "US-002"
    assert resolve_target_story("us-2", STORIES) == "US-002"
    assert resolve_target_story("Track refund status", STORIES) == "US-002"
    # Fuzzy title (typo / re-wording) resolves like the merge service does.
    assert resolve_target_story("Submit refund requests online", STORIES) == "US-001"
    assert resolve_target_story("US-999", STORIES) is None
    assert resolve_target_story(None, STORIES) is None
    # The placeholder code is never a usable target.
    assert resolve_target_story("US-000", [{"ticket_code": "US-000", "story_title": "Placeholder"}]) is None


# ==========================================
# SCHEMA COERCION (parse-boundary repair)
# ==========================================
def test_schema_repairs_a_sloppy_change_entry():
    change = SemanticChange.model_validate({
        "change_type": "add",
        "target_requirement_id": "none",
        "confidence": 4.2,
        "reason": None,
        "recommended_action": "insert",
    })
    assert change.change_type == "NEW_REQUIREMENT"
    assert change.recommended_action == "INSERT"
    assert change.target_requirement_id is None
    assert change.confidence == 1.0
    assert change.reason == ""


def test_schema_derives_the_action_from_the_classification():
    # An unknown/omitted action must never leak through as-is.
    change = SemanticChange.model_validate(make_change(recommended_action="???", change_type="REMOVE_REQUIREMENT"))
    assert change.recommended_action == "ARCHIVE"

    change = SemanticChange.model_validate(make_change(recommended_action=None, change_type="merge"))
    assert change.change_type == "MERGE_REQUIREMENTS"
    assert change.recommended_action == "UPDATE"


def test_schema_normalizes_the_target_reference():
    change = SemanticChange.model_validate(make_change(target_requirement_id=" us-1 "))
    assert change.target_requirement_id == "US-001"


# ==========================================
# GUARDRAIL LAYER: normalize_semantic_changes
# ==========================================
def test_unknown_classification_is_dropped_and_malformed_entries_ignored():
    changes = normalize_semantic_changes(
        [make_change(change_type="SOMETHING_ELSE"), "not-a-dict", None], STORIES
    )
    # Nothing usable survived -> an explicit no-change signal, never an empty list.
    assert len(changes) == 1
    assert changes[0]["change_type"] == "NO_MEANINGFUL_CHANGE"
    assert changes[0]["recommended_action"] == "NO_CHANGE"


def test_hallucinated_target_is_downgraded_and_forces_clarification():
    changes = normalize_semantic_changes([make_change(target_requirement_id="US-999")], STORIES)

    assert len(changes) == 1
    change = changes[0]
    assert change["change_type"] == "NO_MEANINGFUL_CHANGE"
    assert change["recommended_action"] == "NO_CHANGE"
    # Below the gatherer's clarification threshold -> the user is asked.
    assert change["confidence"] < SEMANTIC_LOW_CONFIDENCE_THRESHOLD
    assert "does not exist in the current backlog" in change["reason"]


def test_missing_target_on_a_target_requiring_classification_is_downgraded():
    changes = normalize_semantic_changes([make_change(target_requirement_id=None)], STORIES)

    assert changes[0]["recommended_action"] == "NO_CHANGE"
    assert changes[0]["confidence"] < SEMANTIC_LOW_CONFIDENCE_THRESHOLD
    assert "requires an existing ticket code" in changes[0]["reason"]


def test_remove_without_target_never_archives_anything():
    changes = normalize_semantic_changes(
        [make_change(change_type="REMOVE_REQUIREMENT", target_requirement_id="null", recommended_action="ARCHIVE")],
        STORIES,
    )
    assert changes[0]["recommended_action"] != "ARCHIVE"


def test_new_requirement_target_is_dropped_and_forced_to_insert():
    changes = normalize_semantic_changes(
        [make_change(change_type="NEW_REQUIREMENT", target_requirement_id="US-001", recommended_action="UPDATE")],
        STORIES,
    )
    assert changes[0]["target_requirement_id"] is None
    assert changes[0]["recommended_action"] == "INSERT"
    assert changes[0]["confidence"] == 0.9  # valid classifications are untouched


def test_valid_targets_are_canonicalised_by_code_title_and_fuzzy_match():
    changes = normalize_semantic_changes(
        [
            make_change(target_requirement_id="us-2"),
            make_change(change_type="EXPAND_REQUIREMENT", target_requirement_id="Email refund updates"),
            make_change(change_type="RENAME_REQUIREMENT", target_requirement_id="Submit refund requests online"),
        ],
        STORIES,
    )
    assert [c["target_requirement_id"] for c in changes] == ["US-002", "US-003", "US-001"]
    assert all(c["confidence"] == 0.9 for c in changes)


def test_confidence_is_clamped_at_the_guardrail_layer():
    too_high, too_low = normalize_semantic_changes(
        [make_change(confidence=99), make_change(target_requirement_id="US-002", confidence=-3.0)],
        STORIES,
    )
    assert too_high["confidence"] == 1.0
    assert too_low["confidence"] == 0.0



def test_duplicate_recommendations_collapse_to_one_per_target():
    changes = normalize_semantic_changes(
        [
            make_change(confidence=0.8, reason="first"),
            make_change(confidence=0.95, reason="second"),
        ],
        STORIES,
    )
    assert len(changes) == 1
    # Same action twice -> the more confident wording wins.
    assert changes[0]["confidence"] == 0.95
    assert changes[0]["reason"] == "second"


def test_conflicting_actions_over_one_target_resolve_deterministically():
    changes = normalize_semantic_changes(
        [
            make_change(change_type="EXPAND_REQUIREMENT", recommended_action="UPDATE", confidence=0.95),
            make_change(change_type="REMOVE_REQUIREMENT", target_requirement_id="us-1",
                        recommended_action="ARCHIVE", confidence=0.55),
        ],
        STORIES,
    )
    assert len(changes) == 1
    change = changes[0]
    # An explicit removal outranks a rewrite.
    assert change["recommended_action"] == "ARCHIVE"
    assert change["target_requirement_id"] == "US-001"
    # ... but the contradiction is surfaced so the user confirms it.
    assert change["confidence"] <= CONFLICTING_ACTIONS_CONFIDENCE
    assert change["confidence"] < SEMANTIC_LOW_CONFIDENCE_THRESHOLD
    assert "Conflicting recommendations" in change["reason"]


def test_no_meaningful_change_is_filtered_out_when_real_changes_exist():
    changes = normalize_semantic_changes(
        [
            {"change_type": "NO_MEANINGFUL_CHANGE", "target_requirement_id": None,
             "confidence": 0.9, "reason": "nothing", "recommended_action": "NO_CHANGE"},
            make_change(),
        ],
        STORIES,
    )
    assert [c["change_type"] for c in changes] == ["MODIFY_REQUIREMENT"]


def test_empty_model_answer_becomes_an_explicit_no_change():
    changes = normalize_semantic_changes([], STORIES)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "NO_MEANINGFUL_CHANGE"
    assert changes[0]["recommended_action"] == "NO_CHANGE"
    # A confident "nothing changed" must NOT trigger a clarification question.
    assert changes[0]["confidence"] >= SEMANTIC_LOW_CONFIDENCE_THRESHOLD


def test_actions_are_always_understood_by_the_merge_writer():
    """Contract test: the writer only acts on INSERT/UPDATE/ARCHIVE/NO_CHANGE."""
    messy = [
        make_change(change_type="ADD", target_requirement_id="US-001"),
        make_change(change_type="remove", target_requirement_id="Track refund status",
                    recommended_action="delete"),
        make_change(change_type="MERGE_REQUIREMENTS", target_requirement_id="US-003",
                    recommended_action="MERGE"),
        make_change(change_type="DELETE_REQUIREMENT", target_requirement_id="US-404"),
    ]
    changes = normalize_semantic_changes(messy, STORIES)

    for change in changes:
        assert change["recommended_action"] in SEMANTIC_ACTIONS
        assert change["change_type"] in SEMANTIC_CHANGE_TYPES
        target = change["target_requirement_id"]
        if target is not None:
            assert target in {"US-001", "US-002", "US-003"}

    # The prompt-only MERGE action is translated, so the consolidated story is
    # actually rewritten instead of being silently skipped.
    merge_rec = next(c for c in changes if c["change_type"] == "MERGE_REQUIREMENTS")
    assert merge_rec["recommended_action"] == "UPDATE"
    # The title-resolved removal is archiveable.
    remove_rec = next(c for c in changes if c["change_type"] == "REMOVE_REQUIREMENT")
    assert remove_rec["recommended_action"] == "ARCHIVE"
    assert remove_rec["target_requirement_id"] == "US-002"


def test_normalized_recommendations_drive_the_merge_writer():
    """End-to-end proof that a normalized recommendation is not a silent no-op."""
    existing = [copy.deepcopy(US_001), copy.deepcopy(US_002), copy.deepcopy(US_003)]
    incoming = [
        {**copy.deepcopy(US_001), "i_want_to": "submit a refund request with OTP"},
        copy.deepcopy(US_002),
    ]
    recs = normalize_semantic_changes(
        [
            make_change(target_requirement_id="us-1"),                                     # UPDATE
            make_change(change_type="REMOVE_REQUIREMENT", target_requirement_id="Email refund updates",
                        recommended_action="ARCHIVE"),                                     # ARCHIVE
        ],
        existing,
    )

    merged, report = merge_user_stories_with_report(existing, incoming, semantic_recs=recs)

    assert report["US-001"]["action"] == "updated"
    assert report["US-003"]["action"] == "archived"
    archived = next(s for s in merged if s["ticket_code"] == "US-003")
    assert archived["status"] == "archived"


# ==========================================
# detect_semantic_changes (LLM monkeypatched)
# ==========================================
@pytest.mark.asyncio
async def test_detect_semantic_changes_normalizes_the_model_answer(monkeypatch):
    capture = []
    _patch_llm(monkeypatch, {"changes": [
        make_change(change_type="modify", target_requirement_id="us-3", recommended_action="edit", confidence=1.7),
        make_change(change_type="ADD", target_requirement_id="US-001", recommended_action="INSERT"),
        make_change(change_type="???", target_requirement_id="US-001", recommended_action="UPDATE"),
    ]}, capture)

    changes = await semantic_service.detect_semantic_changes("Change the email notifications", STORIES)

    # The unknown classification is dropped, aliases are canonicalised and the
    # NEW_REQUIREMENT target is stripped.
    assert [c["change_type"] for c in changes] == ["MODIFY_REQUIREMENT", "NEW_REQUIREMENT"]
    assert changes[0]["target_requirement_id"] == "US-003"
    assert changes[0]["recommended_action"] == "UPDATE"
    assert changes[0]["confidence"] == 1.0
    assert changes[1]["target_requirement_id"] is None
    assert changes[1]["recommended_action"] == "INSERT"

    # The prompt received the real backlog, the raw message and a validator.
    variables = capture[0]["variables"]
    assert capture[0]["description"] == "semantic change detection"
    assert capture[0]["validate"] is not None
    assert variables["new_message"] == "Change the email notifications"
    for story in STORIES:
        assert story["ticket_code"] in variables["current_context"]


@pytest.mark.asyncio
async def test_detect_semantic_changes_flags_a_hallucinated_target_for_clarification(monkeypatch):
    _patch_llm(monkeypatch, {"changes": [make_change(target_requirement_id="US-777", confidence=0.98)]})

    changes = await semantic_service.detect_semantic_changes("Tweak that story", STORIES)

    assert len(changes) == 1
    assert changes[0]["recommended_action"] == "NO_CHANGE"
    assert changes[0]["confidence"] < SEMANTIC_LOW_CONFIDENCE_THRESHOLD


@pytest.mark.asyncio
async def test_explicit_user_id_overrides_an_incorrect_inferred_target(monkeypatch):
    _patch_llm(monkeypatch, {"changes": [make_change(target_requirement_id="US-003")]})

    changes = await semantic_service.detect_semantic_changes(
        "Change us 1 so refunds require an OTP",
        STORIES,
    )

    assert changes[0]["target_requirement_id"] == "US-001"


@pytest.mark.asyncio
async def test_detect_semantic_changes_empty_answer_becomes_explicit_no_change(monkeypatch):
    _patch_llm(monkeypatch, {"changes": []})

    changes = await semantic_service.detect_semantic_changes("what does US-001 do?", STORIES)

    assert [c["change_type"] for c in changes] == ["NO_MEANINGFUL_CHANGE"]
    assert changes[0]["recommended_action"] == "NO_CHANGE"


@pytest.mark.asyncio
async def test_detect_semantic_changes_skips_the_llm_for_empty_input(monkeypatch):
    capture = []
    _patch_llm(monkeypatch, RuntimeError("should not be called"), capture)

    changes = await semantic_service.detect_semantic_changes("   ", STORIES)

    assert capture == []
    assert changes[0]["change_type"] == "NO_MEANINGFUL_CHANGE"


@pytest.mark.asyncio
async def test_detect_semantic_changes_rejects_a_payload_without_changes(monkeypatch):
    _patch_llm(monkeypatch, {"unexpected": "shape"})

    with pytest.raises(RuntimeError):
        await semantic_service.detect_semantic_changes("Add QR payments", STORIES)


@pytest.mark.asyncio
async def test_detect_semantic_changes_still_fails_loudly_when_the_llm_fails(monkeypatch):
    """A parse/LLM failure must never masquerade as a legitimate NEW_REQUIREMENT."""
    _patch_llm(monkeypatch, RuntimeError("LM Studio offline"))

    with pytest.raises(RuntimeError) as excinfo:
        await semantic_service.detect_semantic_changes("Add QR payments", STORIES)
    assert "Semantic change detection failed" in str(excinfo.value)


# ==========================================
# match_requirement (target verification)
# ==========================================
def _matcher_payload(**overrides):
    payload = {
        "matched_requirement_id": "US-001",
        "confidence": 0.9,
        "reason": "The message refers to the refund submission story.",
        "action": "UPDATE",
        "status": "MATCHED",
        "candidates": None,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_matcher_rejects_a_hallucinated_target(monkeypatch):
    _patch_llm(monkeypatch, _matcher_payload(matched_requirement_id="US-404"))

    result = await semantic_service.match_requirement("tweak that thing", "UPDATE_REQUIREMENT", STORIES)

    assert result["matched_requirement_id"] is None
    assert result["action"] == "NEW"
    assert result["status"] == "LOW_CONFIDENCE"
    assert result["confidence"] < 0.75
    assert "does not exist" in result["reason"]


@pytest.mark.asyncio
async def test_matcher_canonicalises_the_matched_ticket_code(monkeypatch):
    _patch_llm(monkeypatch, _matcher_payload(matched_requirement_id="us-2"))

    result = await semantic_service.match_requirement("track refunds", "UPDATE_REQUIREMENT", STORIES)

    assert result["matched_requirement_id"] == "US-002"
    assert result["action"] == "UPDATE"
    assert result["status"] == "MATCHED"


@pytest.mark.asyncio
async def test_matcher_update_without_a_target_forces_clarification(monkeypatch):
    _patch_llm(monkeypatch, _matcher_payload(matched_requirement_id=None, action="UPDATE"))

    result = await semantic_service.match_requirement("change the refund limits", "UPDATE_REQUIREMENT", STORIES)

    assert result["matched_requirement_id"] is None
    assert result["action"] == "NEW"
    assert result["status"] == "LOW_CONFIDENCE"
    assert result["confidence"] < 0.75


@pytest.mark.asyncio
async def test_matcher_canonicalises_the_candidates_list(monkeypatch):
    _patch_llm(monkeypatch, _matcher_payload(
        matched_requirement_id=None, action="CLARIFY", status="AMBIGUOUS",
        candidates=["us-1", "Email refund updates", "US-999"],
    ))

    result = await semantic_service.match_requirement("update it", "UPDATE_REQUIREMENT", STORIES)

    assert result["candidates"] == ["US-001", "US-003", "US-999"]


@pytest.mark.asyncio
async def test_matcher_uses_the_shared_backlog_renderer(monkeypatch):
    capture = []
    _patch_llm(monkeypatch, _matcher_payload(), capture)

    await semantic_service.match_requirement("track refunds", "UPDATE_REQUIREMENT", STORIES)

    context = capture[0]["variables"]["current_context"]
    for story in STORIES:
        assert f"- Story {story['ticket_code']}" in context
        assert story["story_title"] in context


# ==========================================
# PROMPT / CODE VOCABULARY SYNC
# ==========================================
def test_prompt_and_code_vocabularies_stay_in_sync():
    """The prompt used to recommend a "MERGE" action no writer understands."""
    import app.prompt_loader as prompt_loader

    prompt = prompt_loader.load_prompt("semantic")
    for change_type in SEMANTIC_CHANGE_TYPES:
        assert change_type in prompt
    for action in SEMANTIC_ACTIONS:
        assert f'"{action}"' in prompt
    assert '"MERGE"' not in prompt


@pytest.mark.asyncio
async def test_explicit_requirement_brief_routes_to_gatherer_without_calling_model(monkeypatch):
    async def should_not_call(*args, **kwargs):
        raise AssertionError("deterministic PRD-brief routing should bypass the model")

    monkeypatch.setattr(semantic_service, "invoke_llm_structured", should_not_call)
    message = """Requirement brief:
Build a secure mobile-banking refund service.
Customers submit refund requests using the original transaction ID and officers approve or reject them.
The API must support 100 TPS, p95 below 500 ms, 99.9% availability, encryption, and audit logging.
Leave uncertain facts TBD."""

    workflow = await semantic_service.classify_workflow(message)
    intent = await semantic_service.detect_requirement_intent(message, [])

    assert workflow["workflow"] == "REQUIREMENT"
    assert intent["intent"] == "CREATE_REQUIREMENT"


@pytest.mark.asyncio
async def test_create_prd_is_document_command_not_gatherer_trigger(monkeypatch):
    async def should_not_call(*args, **kwargs):
        raise AssertionError("deterministic PRD command routing should bypass the model")

    monkeypatch.setattr(semantic_service, "invoke_llm_structured", should_not_call)
    result = await semantic_service.classify_workflow("Create a complete PRD")
    assert result["workflow"] == "COMMAND"

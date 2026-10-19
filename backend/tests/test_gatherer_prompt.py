"""Contract tests for the Gatherer's response-budget discipline."""

from app.agents import _build_intent_guidance
from app.prompt_loader import load_prompt


def test_gatherer_requests_a_delta_instead_of_repeating_the_backlog():
    prompt = load_prompt("gatherer")

    assert "delta-only" in prompt
    assert "OMIT it from the response" in prompt
    assert "Never return locked stories" in prompt
    assert "the application preserves them during the merge" in prompt


def test_intent_guidance_never_requests_unchanged_backlog_output():
    create = _build_intent_guidance("CREATE_REQUIREMENT", 0.99, has_existing_stories=True)
    update = _build_intent_guidance(
        "UPDATE_REQUIREMENT",
        0.99,
        requirement_match={"matched_requirement_id": "US-001"},
        has_existing_stories=True,
    )

    assert "OMIT every existing user story" in create
    assert "OMIT every other existing story" in update
    assert "PRESERVE every other" not in update


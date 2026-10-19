"""The Architect must refresh the Mermaid flow diagram on every PRD generation.

When the PRD is (re)filled, ``architect_node`` asks the LLM with the project's
own dataset (business goals / actors / requirements / user stories / acceptance
criteria) and stores the returned Mermaid ``flowchart TD`` source in
``requirement_states.generated_diagrams``; the API surfaces it as
``mermaid_diagram``. A diagram failure must never fail the PRD generation.
"""
import sys

import pytest

sys.path.insert(0, "/Users/thanyathip/Desktop/agenticdia/backend")

import app.agents as agents
from app.prompt_loader import load_prd_latex_template_body

REQ_STATE = {
    "project_id": "p1",
    "project_name": "PromptPay Refund Portal",
    "requirements": [
        {
            "requirement_code": "REQ-001",
            "title": "Refund Request Submission",
            "user_stories": [
                {
                    "ticket_code": "US-001",
                    "story_title": "Submit refund request online",
                    "as_a": "Retail Customer",
                    "i_want_to": "submit a refund request",
                    "so_that": "I get my money back",
                    "is_locked": False,
                    "acceptance_criteria": [
                        "Given a valid transaction ID, when submitted, then status is PENDING"
                    ],
                }
            ],
        }
    ],
    "business_goals": [{"description": "Cut refund handling time by 60%"}],
    "actors": [{"name": "Retail Customer"}, {"name": "Bank Refund Officer"}],
    "user_stories": [],
    "acceptance_criteria": [],
    "generated_prd": "",
    "generated_diagrams": "",
    "current_workflow_state": "gatherer_node",
    "version_number": 2,
    "updated_at": None,
}


def _patch(monkeypatch, req_state, diagram=None):
    """Stub DB/message/diagram collaborators for offline architect_node runs."""
    async def fake_state(project_id, session=None, current_version=1):
        return dict(req_state)

    async def fake_save(**kwargs):
        return None

    calls: list = []

    async def default_diagram(project_id, req_state, project_data):
        calls.append({"project_id": project_id, "data": project_data})
        return "flowchart TD\n  A([Start]) --> B[Refund]"

    monkeypatch.setattr(agents, "get_or_init_requirement_state", fake_state)
    monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", fake_save)
    monkeypatch.setattr(agents, "_generate_flow_diagram", diagram or default_diagram)
    return calls


def _run_architect():
    return agents.architect_node({
        "project_id": "p1",
        "current_version": 2,
        "structured_requirements": {},
        "version_history_summaries": "No previous revision logs available.",
        "stage_artifacts_only": True,
    })


@pytest.mark.asyncio
async def test_architect_node_refreshes_flow_diagram_on_prd_generation(monkeypatch):
    """Generating the PRD also asks the LLM for the flow diagram with the
    project's own dataset, and the answer is stored + returned."""
    calls = _patch(monkeypatch, REQ_STATE)

    result = await _run_architect()

    # the diagram LLM was invoked with the project dataset
    assert len(calls) == 1
    assert calls[0]["project_id"] == "p1"
    assert calls[0]["data"]["epic_name"] == "Refund Request Submission"
    assert calls[0]["data"]["business_goals"] == [{"description": "Cut refund handling time by 60%"}]
    assert calls[0]["data"]["actors"][0]["name"] == "Retail Customer"

    # the returned mermaid code is the LLM's flowchart, persisted in state
    assert result["mermaid_diagram"].startswith("flowchart TD")
    assert result["requirement_state"]["generated_diagrams"] == result["mermaid_diagram"]


@pytest.mark.asyncio
async def test_architect_node_clears_stale_diagram_when_generation_fails(monkeypatch):
    """A diagram failure clears the older revision but never fails the PRD."""
    async def broken_diagram(project_id, req_state, project_data):
        return ""

    previous = dict(REQ_STATE)
    previous["generated_diagrams"] = "flowchart TD\n  OLD --> FLOW"
    _patch(monkeypatch, previous, diagram=broken_diagram)

    result = await _run_architect()

    # PRD is still generated ...
    assert "\\section*{Stakeholders}" in result["prd_markdown"]
    # ... and the previous revision cannot remain beside the new PRD.
    assert result["mermaid_diagram"] == ""
    assert result["requirement_state"]["generated_diagrams"] == ""


@pytest.mark.asyncio
async def test_generate_flow_diagram_invokes_llm_with_project_dataset_and_sanitizes(monkeypatch):
    """_generate_flow_diagram feeds the project info to the LLM and strips any
    markdown fences from the answer."""
    captured = {}

    async def fake_invoke(llm_, prompt_template, schema, variables=None, **kwargs):
        captured.update(variables or {})
        return {"mermaid_diagram": "```mermaid\nflowchart TD\n  A --> B\n```"}

    monkeypatch.setattr(agents, "invoke_llm_structured", fake_invoke)

    project_data = {
        "epic_name": "Refund Request Submission",
        "business_goals": [{"description": "Cut refund handling time by 60%"}],
        "actors": [{"name": "Retail Customer"}],
        "requirements": REQ_STATE["requirements"],
        "user_stories": [],
        "acceptance_criteria": [],
        "scope_in": ["Submit refund request online"],
        "scope_out": [],
    }
    diagram = await agents._generate_flow_diagram("p1", REQ_STATE, project_data)

    # the project's own info was passed to the LLM call
    assert captured["project_name"] == "PromptPay Refund Portal"
    assert captured["current_version"] == 2
    assert "Refund Request Submission" in captured["project_dataset"]
    assert "Cut refund handling time by 60%" in captured["project_dataset"]
    # fences are stripped from the answer
    assert diagram == "flowchart TD\n  A --> B"


@pytest.mark.asyncio
async def test_generate_flow_diagram_fails_open(monkeypatch):
    """An LLM/parse failure returns '' (caller keeps the previous diagram)."""
    async def boom(*args, **kwargs):
        raise RuntimeError("LM Studio down")

    monkeypatch.setattr(agents, "invoke_llm_structured", boom)
    assert await agents._generate_flow_diagram("p1", REQ_STATE, {"epic_name": "X"}) == ""


def test_sanitize_mermaid_diagram_strips_fences_and_validates_header():
    assert agents._sanitize_mermaid_diagram("```mermaid\nflowchart TD\n  A --> B\n```") == "flowchart TD\n  A --> B"
    assert agents._sanitize_mermaid_diagram("```\nflowchart TD\n  A --> B\n```") == "flowchart TD\n  A --> B"
    assert agents._sanitize_mermaid_diagram("flowchart TD\n  A --> B") == "flowchart TD\n  A --> B"
    assert agents._sanitize_mermaid_diagram("  graph TD\n  A --> B  ") == "graph TD\n  A --> B"
    # non-flowchart answers are rejected
    assert agents._sanitize_mermaid_diagram("sequenceDiagram\n  A->>B: hi") == ""
    assert agents._sanitize_mermaid_diagram("") == ""
    assert agents._sanitize_mermaid_diagram(None) == ""
    assert agents._sanitize_mermaid_diagram("just some prose") == ""


@pytest.mark.asyncio
async def test_architect_reuse_branch_skips_diagram_llm(monkeypatch):
    """When every story is unchanged and both PRD and diagram exist, the node
    reuses everything without any LLM round-trip."""
    calls: list = []

    async def unexpected_diagram(*args, **kwargs):
        calls.append(1)
        return "flowchart TD\n  NEW"

    krungsri = dict(REQ_STATE)
    krungsri["generated_prd"] = load_prd_latex_template_body()
    krungsri["generated_diagrams"] = "flowchart TD\n  OLD"
    stories = []
    for req in krungsri["requirements"]:
        for story in req["user_stories"]:
            story["change_type"] = "unchanged"
            stories.append(story)
    krungsri["user_stories"] = stories
    _patch(monkeypatch, krungsri, diagram=unexpected_diagram)

    result = await _run_architect()

    assert calls == []  # no diagram LLM call on the full-reuse path
    assert result["prd_markdown"] == load_prd_latex_template_body()
    assert result["mermaid_diagram"] == "flowchart TD\n  OLD"

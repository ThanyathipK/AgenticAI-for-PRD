"""Focused unit tests for previously unentered pure helper functions."""
from pathlib import Path

import pytest
from langgraph.graph import END
from pydantic import BaseModel

from app.agents import route_from_matcher, route_from_router
from app.latex_service import _save_failure_diagnostics, _tex_error_excerpt
from app.llm_utils import normalize_output


@pytest.mark.parametrize(("state", "expected"), [
    ({"workflow_routing": {"workflow": "REQUIREMENT"}}, "requirement_matcher_node"),
    ({"workflow_routing": {"workflow": "COMMAND"}, "target_agent": "auditor"}, "auditor_node"),
    ({"workflow_routing": {"workflow": "COMMAND"}, "target_agent": "architect"}, "architect_node"),
    ({"workflow_routing": {"workflow": "COMMAND"}, "target_agent": "unknown"}, END),
    ({"workflow_routing": {"workflow": "CHAT"}}, END),
    ({"workflow_routing": {"workflow": "QUESTION"}}, END),
])
def test_route_from_router_all_dispatches(state, expected):
    assert route_from_router(state) == expected


def test_route_from_matcher_blocks_invalid_only():
    assert route_from_matcher({"requirement_state": {"validation_status": "invalid"}}) == END
    assert route_from_matcher({"requirement_state": {"validation_status": "valid"}}) == "gatherer_node"
    assert route_from_matcher({}) == "gatherer_node"


def test_route_from_matcher_sends_confident_delete_to_archive_node():
    state = {
        "detected_intent": "DELETE_REQUIREMENT",
        "requirement_state": {"validation_status": "invalid"},
        "requirement_match": {"status": "MATCHED", "confidence": 1.0},
    }
    assert route_from_matcher(state) == "delete_requirement_node"


class _Payload(BaseModel):
    value: int


def test_normalize_output_supports_pydantic_mapping_and_pairs():
    assert normalize_output(_Payload(value=3)) == {"value": 3}
    original = {"value": 4}
    assert normalize_output(original) is original
    assert normalize_output([("value", 5)]) == {"value": 5}


def test_tex_error_excerpt_prefers_error_context_and_falls_back_to_tail():
    output = "note\n! Undefined control sequence\ncontext\nmore\nl.42 \\badcommand\nlast"
    excerpt = _tex_error_excerpt(output)
    assert "Undefined control sequence" in excerpt
    assert "l.42" in excerpt
    assert _tex_error_excerpt("one\ntwo\nthree", max_lines=2) == "two\nthree"
    assert _tex_error_excerpt("") == ""


def test_save_failure_diagnostics_copies_sources_and_engine_output(tmp_path, monkeypatch):
    work = tmp_path / "compile"
    work.mkdir()
    (work / "document.tex").write_text("broken tex", encoding="utf-8")
    (work / "document.log").write_text("broken log", encoding="utf-8")
    monkeypatch.setattr("app.latex_service.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("app.latex_service.time.strftime", lambda _format: "20261013-120000")

    target = _save_failure_diagnostics(work, "PDF Build", "engine failed")

    assert target == Path(tmp_path) / "agenticdia-latex-failures" / "20261013-120000-pdf-build"
    assert (target / "document.tex").read_text() == "broken tex"
    assert (target / "document.log").read_text() == "broken log"
    assert (target / "engine-output.txt").read_text() == "engine failed"

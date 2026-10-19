from pathlib import Path

from evaluation.evaluator import CANONICAL_SECTIONS, evaluate_snapshot
from evaluation.report import render_markdown


def _complete_snapshot(tmp_path: Path):
    export = tmp_path / "prd.md"
    export.write_text("# Product Requirements Document\n\nReadable content", encoding="utf-8")
    return {
        "project_id": "p1",
        "requirements": [
            {
                "id": "r1",
                "requirement_code": "REQ-001",
                "prd_sections": ["product_scope"],
                "user_stories": [
                    {
                        "ticket_code": "US-001",
                        "acceptance_criteria": ["Given a user, when they act, then it succeeds"],
                    }
                ],
            }
        ],
        "prd_sections": [
            {"section_key": key, "content": f"Content for {key} REQ-001 US-001"}
            for key in CANONICAL_SECTIONS
        ],
        "diagrams": [{"label": "v1", "source": "flowchart TD\n  A[REQ-001] --> B[US-001]"}],
        "prd_documents": [{"version": 1, "prd_markdown": "Generated PRD"}],
        "generated_files": [{"path": export.name}],
        "expert_reviews": [
            {
                "reviewer_id": reviewer,
                "scores": {
                    "requirements_complete": 4,
                    "traceability_correct": 5,
                    "acceptance_criteria_testable": 4,
                    "diagrams_match_requirements": 4,
                    "prd_and_exports_complete": 5,
                },
            }
            for reviewer in ("r1", "r2")
        ],
        "runs": [
            {"generation_seconds": 10, "acceptance_seconds": 100, "revisions": 1, "prompts": 2},
            {"generation_seconds": 20, "acceptance_seconds": 200, "revisions": 3, "prompts": 4},
        ],
    }


def test_complete_snapshot_passes_and_aggregates(tmp_path, monkeypatch):
    # Rendering is separately exercised only in environments that install a
    # working browser-backed Mermaid CLI. Keep this unit test deterministic.
    monkeypatch.setattr("evaluation.evaluator.shutil.which", lambda _name: None)
    report = evaluate_snapshot(_complete_snapshot(tmp_path), base_dir=tmp_path)

    assert report["automated"]["failed"] == 0
    assert report["automated"]["pass_rate"] == 100.0
    assert report["automated"]["passed_threshold"] is True
    assert report["expert_review"]["passed"] is True
    assert report["expert_review"]["overall_average"] == 4.4
    assert report["time_measurement"]["generation_seconds"]["median"] == 15.0
    assert "100.0%" in render_markdown(report)


def test_detects_duplicate_missing_links_stale_refs_and_bad_diagram(tmp_path):
    snapshot = {
        "project_id": "broken",
        "requirements": [
            {"id": "r1", "requirement_code": "REQ-1", "user_stories": []},
            {"id": "r2", "requirement_code": "REQ-1", "user_stories": []},
        ],
        "prd_sections": [{"section_key": "product_scope", "content": "See REQ-999"}],
        "diagrams": [{"label": "bad", "source": "sequenceDiagram\n A->>B: hi"}],
        "prd_documents": [{"version": 1, "prd_markdown": "Generated PRD"}],
        "generated_files": [{"path": "missing.pdf"}],
    }
    report = evaluate_snapshot(snapshot, base_dir=tmp_path)

    failed_ids = {row["check_id"] for row in report["checks"] if row["status"] == "fail"}
    assert "requirement_ids_unique" in failed_ids
    assert "all_references_resolve" in failed_ids
    assert "diagram_1_syntax" in failed_ids
    assert "file_1_exists" in failed_ids
    assert report["automated"]["critical_error_count"] >= 4
    assert report["automated"]["passed_threshold"] is False


def test_traceability_endpoint_shape_is_supported(tmp_path):
    snapshot = {
        "project_id": "p2",
        "rows": [
            {
                "requirement": {"id": "r1", "requirement_code": "REQ-001"},
                "user_stories": [
                    {"ticket_code": "US-001", "acceptance_criteria": ["Then it works"]}
                ],
                "prd_sections": ["product_scope"],
                "diagrams": ["PRD v1"],
            }
        ],
        "prd_sections": [],
        "diagrams": [{"source": "flowchart TD\n REQ-001 --> US-001"}],
        "prd_documents": [{"version": 1, "prd_markdown": "Generated PRD"}],
    }
    report = evaluate_snapshot(snapshot, base_dir=tmp_path)
    story_parent = next(row for row in report["checks"] if row["check_id"] == "US-001_parent_exists")
    assert story_parent["status"] == "pass"

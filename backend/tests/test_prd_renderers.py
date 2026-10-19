"""Contract tests for the canonical PRD -> renderer boundary."""

from app.prd.model import CanonicalPRD, PRDContent, PRDMetadata
from app.prd.renderers import render_latex, render_markdown
from app.prd_filler import fill_template_body


def _document() -> CanonicalPRD:
    return CanonicalPRD(
        metadata=PRDMetadata(
            project_id="PRJ-42",
            project_name="Canonical Payments",
            version=2,
            version_label="V2.1.0",
            author="Product Owner",
            status="In Review",
            pmo_no="PMO-42",
            stakeholders={"Business Owner": "Payments"},
            version_summary="Added refund processing.",
        ),
        content=PRDContent(
            epic_name="Refund Portal",
            business_goals=["Reduce manual refund handling"],
            actors=[{"name": "Operations", "role": "Reviewer"}],
            user_stories=[{
                "story_title": "Review refund",
                "as_a": "Operations user",
                "i_want_to": "review a refund",
                "so_that": "invalid refunds are stopped",
            }],
            scope_in=["Refund review"],
            scope_out=["Card issuance"],
            problem_statement=["Refunds are processed manually."],
        ),
        narrative={"exec_summary": "A controlled refund workflow."},
    )


def test_latex_renderer_preserves_legacy_filler_output():
    document = _document()

    assert render_latex(document) == fill_template_body(
        **document.template_filler_kwargs()
    )


def test_renderer_adapter_returns_detached_legacy_structures():
    document = _document()
    kwargs = document.template_filler_kwargs()

    kwargs["data"]["business_goals"].append("mutated")
    kwargs["narrative"]["exec_summary"] = "mutated"

    assert document.content.business_goals == ["Reduce manual refund handling"]
    assert document.narrative["exec_summary"] == "A controlled refund workflow."


def test_markdown_renderer_uses_canonical_document(monkeypatch):
    monkeypatch.setattr(
        "app.latex_service.prd_to_markdown",
        lambda source: f"markdown:{source[:12]}",
    )

    rendered = render_markdown(_document())

    assert rendered.startswith("markdown:")

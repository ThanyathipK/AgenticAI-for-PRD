"""Guards for the PRD generation contract.

The Architect agent must ALWAYS fill the official Krungsri Nimble LaTeX
template (template-krungsrinimble.tex) from the project's own data - it must
never generate from the legacy Markdown template (prompts/template.md) and
never merge its output with a previously generated PRD document.
"""
import sys

sys.path.insert(0, "/Users/thanyathip/Desktop/agenticdia/backend")

from app.prompt_loader import (
    load_prd_latex_template,
    load_prd_latex_template_body,
    load_prompt,
)


def test_architect_prompt_has_no_merge_instructions():
    """The old 'INCREMENTAL MERGE' / MIGRATION-RULE behaviour (which blended a
    previous PRD into the new one) must be gone from the prompt."""
    prompt = load_prompt("architect")
    assert "INCREMENTAL MERGE" not in prompt
    assert "existing_prd_content" not in prompt
    assert "MIGRATION RULE" not in prompt


def test_architect_prompt_requires_whole_template_fill():
    prompt = load_prompt("architect")
    assert "<prd_template>" in prompt
    assert "Whole-document rule" in prompt
    assert "fill" in prompt.lower()
    # the template is the entire output, composed as LaTeX (not markdown)
    assert "ENTIRE output" in prompt
    assert "NOT Markdown" in prompt


def test_architect_prompt_maps_full_project_dataset():
    """The prompt must map every project artifact into the template."""
    prompt = load_prompt("architect")
    for needle in (
        "business goals",
        "actors",
        "acceptance criteria",
        "Stakeholders",
        "Version History",
        "PMO",
    ):
        assert needle.lower() in prompt.lower(), needle


def test_gatherer_prompt_separates_fr_wording_and_uses_contextual_ac_coverage():
    prompt = load_prompt("gatherer")
    assert "functional_requirement" in prompt
    assert 'start with "The system must"' in prompt
    assert "used only in the PRD Functional Requirement column" in prompt
    assert "2-6 concise, testable" in prompt
    assert "Do not force irrelevant perspectives" in prompt
    assert "never invent missing detail" in prompt


def test_prd_export_prompt_targets_official_template_body():
    prompt = load_prompt("prd_export")
    assert "<prd_template>" in prompt
    assert "DOCUMENT BODY" in prompt
    assert "NOT Markdown" in prompt


def test_template_body_is_the_official_krungsri_latex():
    body = load_prd_latex_template_body()
    assert "\\section*{Stakeholders}" in body
    assert "\\section*{Version History}" in body
    assert "\\documentclass" not in body  # body only; preamble spliced server-side


def test_served_template_is_the_official_latex_document():
    full = load_prd_latex_template()
    assert "\\documentclass" in full
    assert "\\begin{document}" in full and "\\end{document}" in full

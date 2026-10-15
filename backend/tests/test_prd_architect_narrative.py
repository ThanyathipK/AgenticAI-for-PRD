"""Regression tests for audit-backed Technical and Appendix PRD content."""

from app.agents import (
    _extract_explicit_prd_narrative,
    _prd_needs_narrative_refresh,
    _prd_narrative_from_explicit_input,
    _prd_narrative_from_validated_audit,
)
from app.latex_service import prd_to_markdown
from app.prd_filler import fill_template_body


def test_supported_high_confidence_finding_populates_prd_tail_sections():
    state = {
        "audit_findings": [{
            "category": "Performance and security",
            "confidence": 0.91,
            "evidence_status": "supported",
            "impact": "warning",
            "description": "The refund API must enforce latency and encryption requirements.",
            "recommendation": {
                "summary": "Define p95 response time and encryption-at-rest controls.",
            },
        }],
    }

    body = fill_template_body(
        project_id="new-project-test",
        project_name="Fresh PRD test",
        narrative=_prd_narrative_from_validated_audit(state),
    )
    markdown = prd_to_markdown(body)

    technical = markdown[markdown.index("### 3. Technical & Operational Considerations"):]
    appendix = markdown[markdown.index("### 4. Appendix"):]
    expected = "Define p95 response time and encryption-at-rest controls."
    assert expected in technical
    assert expected in appendix


def test_low_confidence_or_unsupported_findings_do_not_invent_prd_content():
    state = {
        "audit_findings": [{
            "category": "Performance",
            "confidence": 0.45,
            "evidence_status": "insufficient",
            "impact": "warning",
            "description": "Unverified latency requirement.",
        }],
    }
    assert _prd_narrative_from_validated_audit(state) == {}


def test_explicit_scenario_populates_sections_08_and_09():
    state = {
        "prd_narrative": {
            "non_functional": {
                "value": "1. API p95 response time under 500 milliseconds.\n2. 99.9% monthly availability.\n3. TLS 1.2 and AES-256 encryption.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
            "tps_volume": {
                "value": "100 TPS at launch; 50,000 requests per month.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
            "launch_plan": {
                "value": "Two-week internal pilot, then 10%, 50%, and 100% customer rollout if errors remain below 1%.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
            "assumptions": {
                "value": "Core banking provides a refund API and retains searchable transaction IDs for 180 days.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
            "open_questions": {
                "value": "Core banking API rate limit and final refund approval limit remain unconfirmed.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
            "growth": {
                "value": "Unclear growth forecast",
                "confidence": 0.40,
                "source": "explicit_user_input",
            },
        },
    }
    narrative = _prd_narrative_from_explicit_input(state)
    assert "growth" not in narrative

    markdown = prd_to_markdown(fill_template_body(
        project_id="scenario-project",
        project_name="Secure Refund API",
        narrative=narrative,
    ))
    technical = markdown[markdown.index("### 3. Technical & Operational Considerations"):]
    appendix = markdown[markdown.index("### 4. Appendix"):]
    assert "100 TPS at launch" in technical
    assert "Two-week internal pilot" in technical
    assert "Core banking provides a refund API" in appendix
    assert "Core banking API rate limit" in appendix
    assert "Unclear growth forecast" not in markdown


def test_exact_user_scenario_is_extracted_without_model_cooperation():
    raw_input = """Technical requirements:
- API response time must be p95 under 500 milliseconds.
- Support 100 transactions per second at launch.
- Expected customer volume is 50,000 requests per month.
- Encrypt sensitive data in transit using TLS 1.2 or higher and at rest using AES-256.
- Maintain 99.9% monthly availability.

Rollout plan:
- Pilot with internal operations users for two weeks.
- Release to 10%, then 50%, then 100% of customers.

Assumptions:
- The core banking system provides a refund-processing API.
- Transaction IDs remain searchable for at least 180 days.

Open questions and risks:
- The core banking API rate limit has not been confirmed.
- A core banking outage may delay refund completion.
"""
    gathered = _extract_explicit_prd_narrative(raw_input)
    assert "p95 under 500 milliseconds" in gathered["non_functional"]["value"]
    assert "100 transactions per second" in gathered["tps_volume"]["value"]
    assert "Pilot with internal operations" in gathered["launch_plan"]["value"]
    assert "core banking system provides" in gathered["assumptions"]["value"]
    assert "rate limit has not been confirmed" in gathered["open_questions"]["value"]
    assert all(field["confidence"] == 1.0 for field in gathered.values())


def test_existing_tbd_prd_is_rebuilt_when_confirmed_narrative_arrives():
    state = {
        "prd_narrative": {
            "launch_plan": {
                "value": "Pilot internally for two weeks.",
                "confidence": 0.99,
                "source": "explicit_user_input",
            },
        },
    }
    old_prd = fill_template_body(project_id="p", project_name="P")
    assert _prd_needs_narrative_refresh(state, old_prd)

    updated_prd = fill_template_body(
        project_id="p",
        project_name="P",
        narrative=_prd_narrative_from_explicit_input(state),
    )
    assert not _prd_needs_narrative_refresh(state, updated_prd)

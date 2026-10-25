"""Pure rules for selecting confirmed narrative content for PRD generation."""

import re
from typing import Any

# Supporting facts for the Technical and Appendix PRD parts are useful even
# when the extractor is moderately confident.  Keep genuinely weak guesses
# out, but do not require the stricter requirement-mutation threshold.
PRD_NARRATIVE_CONFIDENCE_THRESHOLD = 0.50


def narrative_from_validated_audit(requirement_state: dict[str, Any]) -> dict[str, Any]:
    """Extract only supported, high-confidence audit detail for the PRD tail."""
    technical_terms = (
        "technical",
        "performance",
        "security",
        "availability",
        "reliability",
        "operational",
        "integration",
        "privacy",
    )
    non_functional: list[str] = []
    open_questions: list[str] = []

    for finding in requirement_state.get("audit_findings") or []:
        if not isinstance(finding, dict):
            continue
        try:
            confidence = float(finding.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            confidence < PRD_NARRATIVE_CONFIDENCE_THRESHOLD
            or finding.get("evidence_status") != "supported"
        ):
            continue
        recommendation = finding.get("recommendation") or {}
        summary = recommendation.get("summary") if isinstance(recommendation, dict) else ""
        text = str(summary or finding.get("description") or "").strip()
        if not text:
            continue
        if any(term in str(finding.get("category") or "").lower() for term in technical_terms):
            non_functional.append(text)
        if str(finding.get("impact") or "").lower() in {"blocking", "warning"}:
            open_questions.append(text)

    narrative: dict[str, Any] = {}
    if non_functional:
        narrative["non_functional"] = "\n".join(
            f"{index}. {item}" for index, item in enumerate(dict.fromkeys(non_functional), start=1)
        )
    if open_questions:
        narrative["open_questions"] = "\n".join(
            f"{index}. {item}" for index, item in enumerate(dict.fromkeys(open_questions), start=1)
        )
    return narrative


def narrative_from_explicit_input(requirement_state: dict[str, Any]) -> dict[str, Any]:
    """Flatten explicitly gathered PRD facts at moderate confidence or above."""
    narrative: dict[str, Any] = {}
    allowed = {
        "exec_summary",
        "problem_statement",
        "business_objectives",
        "expected_benefit",
        "success_metrics",
        "target_audience",
        "user_flow",
        "scope_in",
        "scope_out",
        "non_functional",
        "tps_volume",
        "growth",
        "launch_plan",
        "dependencies",
        "go_to_market",
        "open_questions",
        "assumptions",
        "glossary",
        "related_documents",
    }
    aliases = {"technical": "non_functional"}
    for raw_key, field in (requirement_state.get("prd_narrative") or {}).items():
        key = aliases.get(raw_key, raw_key)
        if key not in allowed or not isinstance(field, dict):
            continue
        try:
            confidence = float(field.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        value = field.get("value")
        if isinstance(value, list):
            value = "\n".join(
                f"{index}. {item}" for index, item in enumerate(value, start=1) if item
            )
        if key in {"scope_in", "scope_out"} and isinstance(value, str):
            kept = []
            for line in value.splitlines():
                plain = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip()
                if re.match(r"(?i)^no changes? to\s+(?:REQ|US)-\d+", plain):
                    continue
                if plain:
                    kept.append(plain)
            value = "\n".join(
                f"{index}. {item}" for index, item in enumerate(dict.fromkeys(kept), start=1)
            )
        if confidence >= PRD_NARRATIVE_CONFIDENCE_THRESHOLD and value:
            narrative[key] = value
    return narrative


def needs_narrative_refresh(requirement_state: dict[str, Any], existing_prd: str) -> bool:
    """Return whether confirmed section 08/09 facts are absent from the stored PRD."""
    confirmed = narrative_from_explicit_input(requirement_state)
    if not confirmed:
        return False
    try:
        from app.latex_service import prd_to_markdown
        from app.prd_filler import extract_manual_export_fields

        rendered = extract_manual_export_fields(prd_to_markdown(existing_prd)).get("narrative", {})
    except Exception:
        return True
    for key, value in confirmed.items():
        current = rendered.get(key)
        if not current or str(current).strip().upper() == "TBD":
            return True
        expected_text = re.sub(r"\s+", " ", str(value)).strip().lower()
        current_text = re.sub(r"\s+", " ", str(current)).strip().lower()
        if expected_text[:48] and expected_text[:48] not in current_text:
            return True
    return False

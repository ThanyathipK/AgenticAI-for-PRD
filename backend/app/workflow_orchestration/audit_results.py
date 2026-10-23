"""Pure preparation and deterministic merging for auditor results."""

import re
from typing import Any

DOCUMENT_INJECTION_RE = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?(?:previous|prior|auditor|system|developer)|"
    r"return\s+(?:a\s+)?(?:critical|fake|pass|fail)|"
    r"override\s+(?:the\s+)?(?:prompt|instructions)|"
    r"(?:system|developer)\s+(?:prompt|message))"
)


def dedupe_dicts(items: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Perform stable de-duplication for overlapping audit chunks."""
    seen = set()
    output = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = tuple(str(item.get(field) or "").strip().casefold() for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def prepare_audit_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Treat uploaded text as data and redact obvious prompt-injection lines."""
    prepared = []
    for document in documents:
        body = str(document.get("content_markdown") or "")
        suspicious = False
        safe_lines = []
        for line in body.splitlines():
            if DOCUMENT_INJECTION_RE.search(line):
                suspicious = True
                safe_lines.append("[Potential prompt-injection instruction redacted]")
            else:
                safe_lines.append(line)
        prepared.append(
            {
                **document,
                "source_type": document.get("source_type")
                or (
                    "Knowledge base"
                    if str(document.get("document_id", "")).startswith("banking:")
                    else "Project file"
                ),
                "content_markdown": "\n".join(safe_lines),
                "prompt_injection_redacted": suspicious,
            }
        )
    return prepared


def merge_audit_passes(
    results: list[dict[str, Any]],
    current_version: int,
    *,
    checklist_id: str,
    checklist_version: str,
) -> dict[str, Any]:
    """Merge chunk output and enforce the risk-based verdict deterministically."""
    passed = {str(check) for result in results for check in result.get("passed_checks", [])}
    findings = dedupe_dicts(
        [finding for result in results for finding in result.get("findings", [])],
        ("finding_type", "category", "target_requirement_id", "description"),
    )
    questions = dedupe_dicts(
        [question for result in results for question in result.get("clarification_questions", [])],
        ("checklist_category", "target_user_story_id", "question_text"),
    )
    sources = dedupe_dicts(
        [source for finding in findings for source in finding.get("source_references", [])]
        + [source for question in questions for source in question.get("source_references", [])],
        ("document_id", "document_name", "section", "excerpt"),
    )
    for finding in findings:
        finding["rule_id"] = str(finding.get("rule_id") or "ADHOC").upper()
        finding["applicability"] = str(finding.get("applicability") or "unknown").lower()
        finding["impact"] = str(finding.get("impact") or "warning").lower()
        try:
            finding["confidence"] = max(0.0, min(1.0, float(finding.get("confidence", 0.5))))
        except (TypeError, ValueError):
            finding["confidence"] = 0.5

    # A conflict must always give the stakeholder a way to resolve it. Models
    # sometimes return a valid conflicting finding without the companion
    # question, leaving the UI with a red card that cannot be answered. Add a
    # deterministic, evidence-preserving question only when no question already
    # targets the same requirement/story and category.
    for finding in findings:
        if str(finding.get("finding_type") or "").lower() != "conflicting":
            continue
        target = str(finding.get("target_requirement_id") or "").strip()
        category = str(finding.get("category") or "Requirement conflict").strip()
        already_covered = any(
            str(
                question.get("target_requirement_id")
                or question.get("target_user_story_id")
                or ""
            ).strip() == target
            and str(question.get("checklist_category") or "").strip().casefold()
            == category.casefold()
            for question in questions
        )
        if already_covered:
            continue
        target_label = target or "the affected requirement"
        description = str(finding.get("description") or "the identified contradiction").strip()
        questions.append({
            "checklist_category": category,
            "target_user_story_id": target or None,
            "target_requirement_id": target or None,
            "question_text": (
                f"What should {target_label} state to resolve this conflict: "
                f"{description.rstrip('.')}?"
            ),
            "is_resolved": False,
            "source_references": list(finding.get("source_references") or []),
        })
    questions = dedupe_dicts(
        questions,
        ("checklist_category", "target_user_story_id", "question_text"),
    )

    impact_rank = {"blocking": 0, "warning": 1, "suggestion": 2}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(
        key=lambda item: (
            impact_rank.get(item.get("impact"), 3),
            severity_rank.get(str(item.get("severity") or "").lower(), 4),
            -item.get("confidence", 0.0),
        )
    )

    blocking = [
        finding
        for finding in findings
        if finding["applicability"] == "applicable_required"
        and finding["impact"] == "blocking"
        and finding["confidence"] >= 0.7
        and finding.get("evidence_status") == "supported"
    ]
    failed = {str(finding.get("category") or "Mandatory control") for finding in blocking}
    contexts = [
        result.get("project_context")
        for result in results
        if isinstance(result.get("project_context"), dict)
    ]
    project_context = max(
        contexts,
        key=lambda item: float(item.get("confidence") or 0.0),
        default={},
    )
    if blocking:
        verdict = "fail"
    elif questions:
        verdict = "needs_clarification"
    elif findings:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"
    return {
        "is_valid": not blocking and not questions,
        "verdict": verdict,
        "project_context": project_context,
        "audit_version_reviewed": current_version,
        "passed_checks": sorted(passed - failed),
        "failed_checks": sorted(failed),
        "findings": findings,
        "source_references": sources,
        "clarification_questions": questions,
        "checklist_id": checklist_id,
        "checklist_version": checklist_version,
    }

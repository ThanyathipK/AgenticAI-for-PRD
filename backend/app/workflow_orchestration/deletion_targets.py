"""Pure parsing and resolution rules for requirement deletion commands."""

import re
from typing import Any

from app.merge_service import normalize_ticket_code
from app.semantic_service import extract_story_ticket_codes

AC_REFERENCE_RE = re.compile(r"\bAC[\s_-]*(\d{1,4})\b", re.IGNORECASE)
AC_WORDING_RE = re.compile(r"\b(?:AC|acceptance\s+criteri(?:on|a))\b", re.IGNORECASE)
BARE_ARCHIVE_NUMBER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remove|delete|archive|retire|discard|drop)\s+#?0*(\d{1,4})\s*[.!]?\s*$",
    re.IGNORECASE,
)


def bulk_archive_scope(raw_input: str) -> str | None:
    """Return an explicitly requested whole-project archive scope, if any."""
    text = " ".join(str(raw_input or "").casefold().split())
    if not re.search(r"\b(?:remove|delete|archive|retire|discard|drop)\b", text):
        return None
    if not re.search(r"\b(?:all|every|everything)\b", text):
        return None
    if re.search(r"\bacceptance\s+criteri(?:a|on)\b", text):
        return None
    if re.search(r"\b(?:requirements?|reqs?)\b", text):
        return "ALL_REQUIREMENTS"
    return "ALL_STORIES"


def acceptance_criterion_label(story_code: str, ordinal: int) -> str:
    """Return the stable, user-facing reference for a criterion in a story."""
    return f"{story_code}-AC-{ordinal:03d}"


def resolve_acceptance_criteria_targets(
    raw_input: str, existing_stories: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve explicit AC references without falling back to story-level deletion."""
    if not AC_WORDING_RE.search(str(raw_input or "")):
        return None

    stories_by_code = {
        normalize_ticket_code(story.get("ticket_code")): story
        for story in existing_stories
        if isinstance(story, dict) and normalize_ticket_code(story.get("ticket_code"))
    }
    requested_story_codes = [
        code for code in extract_story_ticket_codes(raw_input) if code in stories_by_code
    ]
    ordinals = [int(value) for value in AC_REFERENCE_RE.findall(raw_input)]
    candidates: list[dict[str, Any]] = []

    if ordinals:
        story_codes = requested_story_codes or sorted(stories_by_code)
        for story_code in story_codes:
            criteria = stories_by_code[story_code].get("acceptance_criteria") or []
            for ordinal in ordinals:
                if 1 <= ordinal <= len(criteria):
                    candidates.append(
                        {
                            "story_code": story_code,
                            "ordinal": ordinal,
                            "text": str(criteria[ordinal - 1]),
                            "label": acceptance_criterion_label(story_code, ordinal),
                        }
                    )

    if not candidates:
        folded_input = " ".join(str(raw_input or "").casefold().split())
        for story_code, story in stories_by_code.items():
            for ordinal, criterion in enumerate(story.get("acceptance_criteria") or [], start=1):
                folded_criterion = " ".join(str(criterion).casefold().split())
                if folded_criterion and folded_criterion in folded_input:
                    candidates.append(
                        {
                            "story_code": story_code,
                            "ordinal": ordinal,
                            "text": str(criterion),
                            "label": acceptance_criterion_label(story_code, ordinal),
                        }
                    )

    unique = {item["label"]: item for item in candidates}
    candidates = list(unique.values())
    if len(candidates) == 1:
        return {
            "matched_requirement_id": candidates[0]["story_code"],
            "acceptance_criteria_targets": candidates,
            "confidence": 1.0,
            "reason": f"The user explicitly targeted {candidates[0]['label']}.",
            "action": "DELETE",
            "status": "MATCHED",
            "candidates": [candidates[0]["label"]],
        }
    if len(candidates) > 1:
        labels = [item["label"] for item in candidates]
        return {
            "matched_requirement_id": None,
            "acceptance_criteria_targets": [],
            "confidence": 0.0,
            "reason": (
                "That acceptance-criterion number exists in more than one story. "
                f"Specify the story too, for example {labels[0]}."
            ),
            "action": "CLARIFY",
            "status": "AMBIGUOUS",
            "candidates": labels,
        }
    return {
        "matched_requirement_id": None,
        "acceptance_criteria_targets": [],
        "confidence": 0.0,
        "reason": (
            "I could not match that acceptance criterion. Use a reference such as "
            "US-001-AC-001, or include the full criterion text."
        ),
        "action": "CLARIFY",
        "status": "LOW_CONFIDENCE",
        "candidates": [],
    }

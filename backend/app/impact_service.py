"""
Requirement Impact Analysis service (derived, read-only).

Analyzes HOW a pending merge (a draft ``pending_action``) would change the
project and WHICH downstream artifacts are impacted — so the merge window can
show the user what changes and what is affected BEFORE they click Save:

  Requirements   diffed by ``requirement_code``        -> created / updated / removed / unchanged
  User stories   diffed by ticket code / fuzzy title   -> created / updated / removed / unchanged
                  (+ which fields changed, criteria added / removed counts)
  PRD sections   scanned for ``REQ-###`` / ``US-###`` codes referencing the
                 affected requirements / stories       -> impacted sections
  Diagrams       scanned (per PRD-version mermaid source) for affected codes
                                                       -> impacted diagrams

Nothing here WRITES: the analysis is computed against the stored state and the
draft's ``proposed_changes`` on every read. The same code-extraction contract
as the Traceability service applies (``extract_codes``), so the impact window
and the traceability matrix always agree on what references what.
"""
import logging
from typing import Any, Dict, List, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.merge_service import normalize_ticket_code, titles_match
from app.repositories.prd import PRDDocumentRepository
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.traceability_service import extract_codes

logger = logging.getLogger(__name__)

# Story narrative fields considered when detecting an update.
STORY_FIELDS = ("story_title", "as_a", "i_want_to", "so_that")
REQUIREMENT_FIELDS = ("title", "description")

# Change kinds surfaced to the UI.
CHANGE_CREATED = "created"
CHANGE_UPDATED = "updated"
CHANGE_REMOVED = "removed"
CHANGE_UNCHANGED = "unchanged"


def _norm_text(value: Any) -> str:
    """Whitespace/case-insensitive text comparison value."""
    return " ".join(str(value or "").strip().casefold().split())


def _norm_criteria(value: Any) -> Set[str]:
    """Normalized acceptance-criteria text set (order-insensitive compare)."""
    if not isinstance(value, list):
        return set()
    return {_norm_text(c) for c in value if _norm_text(c)}


def _as_story_list(value: Any) -> List[Dict[str, Any]]:
    return [s for s in value if isinstance(s, dict)] if isinstance(value, list) else []


# CONFIRM 4.5 — Shape helper: flattens a state payload (persisted OR draft) into
#             {requirement_code: {…stories…}} so 4.2/4.3 can diff like-for-like
#             regardless of whether the story list is nested per requirement or flat.
def _flatten_state_requirements(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{requirement_code: requirement-dict} from a requirement-state shape.

    Falls back to attaching the top-level flat ``user_stories`` list to the
    first requirement when a requirement carries no stories of its own (the
    legacy epic-only shape the gatherer still emits).
    """
    requirements: Dict[str, Dict[str, Any]] = {}
    flat_stories = _as_story_list(state.get("user_stories"))
    raw = state.get("requirements")
    req_list = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    for index, req in enumerate(req_list):
        code = (req.get("requirement_code") or "").strip().upper()
        if not code:
            continue
        stories = _as_story_list(req.get("user_stories"))
        if not stories and index == 0 and flat_stories:
            stories = flat_stories
        requirements[code] = {
            "requirement_code": code,
            "title": req.get("title") or "",
            "description": req.get("description") or "",
            "user_stories": stories,
        }
    return requirements


# CONFIRM 4.2 — Requirement-level diff (current vs proposed): created / updated /
#             removed groups with the fields that changed; returns the affected
#             REQ-codes used by 4.4.
def diff_requirements(
    current: Dict[str, Dict[str, Any]],
    proposed: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """Requirement-level diff. Returns (changes, affected_codes)."""
    changes: List[Dict[str, Any]] = []
    affected: Set[str] = set()

    for code, req in proposed.items():
        existing = current.get(code)
        if existing is None:
            affected.add(code)
            changes.append({
                "requirement_code": code,
                "title": req.get("title") or "",
                "change_kind": CHANGE_CREATED,
                "changed_fields": [],
            })
            continue
        changed_fields = [
            field for field in REQUIREMENT_FIELDS
            if _norm_text(existing.get(field)) != _norm_text(req.get(field))
        ]
        kind = CHANGE_UPDATED if changed_fields else CHANGE_UNCHANGED
        if changed_fields:
            affected.add(code)
        changes.append({
            "requirement_code": code,
            "title": req.get("title") or existing.get("title") or "",
            "change_kind": kind,
            "changed_fields": changed_fields,
        })

    for code, req in current.items():
        if code not in proposed:
            affected.add(code)
            changes.append({
                "requirement_code": code,
                "title": req.get("title") or "",
                "change_kind": CHANGE_REMOVED,
                "changed_fields": [],
            })

    # Deterministic, readable order: changed first, then by code.
    kind_order = {CHANGE_CREATED: 0, CHANGE_UPDATED: 1, CHANGE_REMOVED: 2, CHANGE_UNCHANGED: 3}
    changes.sort(key=lambda c: (kind_order.get(c["change_kind"], 9), c["requirement_code"]))
    return changes, affected


# CONFIRM 4.3 — Story-level diff: created / updated / removed user stories with their
#             acceptance-criteria deltas (criteria_added / criteria_removed); returns
#             the affected US-codes used by 4.4.
def diff_stories(
    current: Dict[str, Dict[str, Any]],
    proposed: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """User-story diff (progressive identity: ticket code -> title match).

    Mirrors the Gatherer's matching contract (see ``merge_service``) so the
    impact window predicts exactly what the merge will do. Returns
    (changes, affected_codes).
    """
    # Flat story maps: code -> (story, requirement_code, matched flag)
    current_flat: Dict[str, Dict[str, Any]] = {}
    for req_code, req in current.items():
        for story in _as_story_list(req.get("user_stories")):
            code = normalize_ticket_code(story.get("ticket_code"))
            if code:
                current_flat[code] = {**story, "_requirement_code": req_code}

    proposed_flat: Dict[str, Dict[str, Any]] = {}
    for req_code, req in proposed.items():
        for story in _as_story_list(req.get("user_stories")):
            code = normalize_ticket_code(story.get("ticket_code"))
            if code:
                proposed_flat[code] = {**story, "_requirement_code": req_code}

    changes: List[Dict[str, Any]] = []
    affected: Set[str] = set()
    matched_current: Set[str] = set()

    kind_order = {CHANGE_CREATED: 0, CHANGE_UPDATED: 1, CHANGE_REMOVED: 2, CHANGE_UNCHANGED: 3}

    def _record(
        story: Dict[str, Any],
        kind: str,
        changed_fields: List[str],
        added: int,
        removed: int,
        review_required: int = 0,
    ) -> None:
        code = normalize_ticket_code(story.get("ticket_code"))
        changes.append({
            "ticket_code": code,
            "story_title": story.get("story_title") or "",
            "requirement_code": story.get("_requirement_code") or "",
            "change_kind": kind,
            "changed_fields": changed_fields,
            "criteria_added": added,
            "criteria_removed": removed,
            "criteria_review_required": review_required,
        })

    # 1. Proposed stories matched by ticket code.
    for code, story in proposed_flat.items():
        status = (story.get("status") or "active").lower()
        existing = current_flat.get(code)
        if status == "archived":
            affected.add(code)
            _record(story, CHANGE_REMOVED, [], 0, 0)
            if existing is not None:
                matched_current.add(code)
            continue
        if existing is None:
            # Fall back to a title match against any not-yet-matched current story.
            title_match = next(
                (
                    cur_code
                    for cur_code, cur in current_flat.items()
                    if cur_code not in matched_current
                    and cur_code not in proposed_flat
                    and titles_match(cur.get("story_title"), story.get("story_title"))
                ),
                None,
            )
            if title_match is not None:
                existing = current_flat[title_match]
                matched_current.add(title_match)
            else:
                affected.add(code)
                _record(story, CHANGE_CREATED, [], 0, 0)
                continue
        matched_current.add(code)

        changed_fields = [
            field for field in STORY_FIELDS
            if _norm_text(existing.get(field)) != _norm_text(story.get(field))
        ]
        cur_ac = _norm_criteria(existing.get("acceptance_criteria"))
        prop_ac = _norm_criteria(story.get("acceptance_criteria"))
        criteria_added = len(prop_ac - cur_ac)
        criteria_removed = len(cur_ac - prop_ac)
        criteria_review_required = len(cur_ac & prop_ac) if changed_fields else 0
        if changed_fields or criteria_added or criteria_removed:
            affected.add(code)
            if criteria_added and "acceptance_criteria" not in changed_fields:
                changed_fields.append("acceptance_criteria")
            _record(
                story,
                CHANGE_UPDATED,
                changed_fields,
                criteria_added,
                criteria_removed,
                criteria_review_required,
            )
        else:
            _record(story, CHANGE_UNCHANGED, [], 0, 0)

    # 2. Current stories the merge does NOT carry forward -> archived (removed).
    for code, story in current_flat.items():
        if code in matched_current or code in proposed_flat:
            continue
        affected.add(code)
        _record(story, CHANGE_REMOVED, [], 0, 0)

    changes.sort(key=lambda c: (kind_order.get(c["change_kind"], 9), c.get("ticket_code") or ""))
    return changes, affected


# CONFIRM 4.4 — Downstream impact (DB READ ONLY): scans the stored prd_sections and
#             prd_documents for the affected codes, flags locked sections, and reports
#             codes that would become DANGLING when a requirement/story is removed —
#             the warning surface of the confirmation gate.
async def analyze_downstream(
    project_id: str,
    session: AsyncSession,
    affected_codes: Set[str],
    removed_codes: Set[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find PRD sections / diagrams whose content references affected codes.

    Reuses the Traceability service's code-extraction contract so the impact
    window and the traceability matrix always agree. A section whose ONLY
    referenced affected codes are being removed is flagged
    ``references_removed`` (it would be left with dangling references);
    everything else is ``references_changed``. Returns
    (impacted_sections, impacted_diagrams).
    """
    impacted_sections: List[Dict[str, Any]] = []
    impacted_diagrams: List[Dict[str, Any]] = []
    if not affected_codes:
        return impacted_sections, impacted_diagrams

    affected = {code.upper() for code in affected_codes}
    removed = {code.upper() for code in removed_codes}

    try:
        sections = await PRDSectionRepository.get_by_project(project_id, session)
    except Exception as sec_err:  # never fail the analysis on section reads
        logger.warning("[IMPACT] Could not read PRD sections (%s).", sec_err)
        sections = []
    for section in sections:
        hits = {c.upper() for c in extract_codes(section.get("content"))} & affected
        if not hits:
            continue
        impacted_sections.append({
            "section_key": section.get("section_key") or "",
            "title": section.get("title") or "",
            "referenced_codes": sorted(hits),
            "is_locked": bool(section.get("is_locked")),
            "impact_kind": (
                "references_removed" if hits and hits <= removed else "references_changed"
            ),
        })

    try:
        prds = await PRDDocumentRepository.get_by_project(project_id, session)
    except Exception as prd_err:  # never fail the analysis on document reads
        logger.warning("[IMPACT] Could not read PRD documents (%s).", prd_err)
        prds = []
    for prd in prds:
        mermaid = prd.get("mermaid_diagram") or ""
        if not mermaid:
            continue
        hits = {c.upper() for c in extract_codes(mermaid)} & affected
        if not hits:
            continue
        impacted_diagrams.append({
            "label": f"PRD v{prd.get('version')}",
            "version": prd.get("version") or 0,
            "referenced_codes": sorted(hits),
        })

    return impacted_sections, impacted_diagrams


# CONFIRM 4.x — Impact analysis (READ-ONLY sub-flow behind CONFIRM 3.2). It diffs the
# staged draft against the persisted state and derives what is affected downstream:
#   4.1 analyze_pending_action — orchestrator (this class)
#   4.2 diff_requirements      — requirement groups added/changed/removed + codes
#   4.3 diff_stories           — user stories + acceptance-criteria deltas + codes
#   4.4 analyze_downstream     — prd_sections / prd_documents that reference those
#                                codes (locked sections flagged, removed codes →
#                                dangling references)
#   4.5 _flatten_state_requirements (+ _norm_* / _as_story_list) — shape helpers
# Nothing here writes: it exists so the user can judge the merge before CONFIRM 3.3.
class ImpactService:
    """Builds the Requirement Impact Analysis for one pending action."""

    @staticmethod
    # CONFIRM 4.1 — Orchestrator: load the CURRENT persisted state (PROJECT 2.3.2) as
    #               the "version before", diff it against `proposed_changes`
    #               (4.2 + 4.3), then ask 4.4 which PRD sections/diagrams reference the
    #               affected codes. Returns the summary counters + per-item changes +
    #               impacted artifacts consumed by CONFIRM 1.6.
    async def analyze_pending_action(
        action: Dict[str, Any],
        project_id: str,
        session: AsyncSession,
    ) -> Dict[str, Any]:
        """Diff the draft's proposed state against the stored state and
        derive every downstream artifact impacted by the change. READ-ONLY."""
        proposed_changes = action.get("proposed_changes") or {}
        if not isinstance(proposed_changes, dict):
            proposed_changes = {}

        # Current persisted state (the "version before") from the relation
        # tables — the same shape the merge draft stores.
        current_state = await RequirementStateRepository.get_by_project_id(project_id, session) or {}

        current_reqs = _flatten_state_requirements(current_state)
        proposed_reqs = _flatten_state_requirements(proposed_changes)

        requirement_changes, affected_req_codes = diff_requirements(current_reqs, proposed_reqs)
        story_changes, affected_story_codes = diff_stories(current_reqs, proposed_reqs)

        removed_codes = (
            {c["requirement_code"] for c in requirement_changes if c["change_kind"] == CHANGE_REMOVED}
            | {s["ticket_code"] for s in story_changes if s["change_kind"] == CHANGE_REMOVED}
        )
        impacted_sections, impacted_diagrams = await analyze_downstream(
            project_id, session, affected_req_codes | affected_story_codes, removed_codes,
        )

        criteria_added = sum(s.get("criteria_added", 0) for s in story_changes)
        criteria_removed = sum(s.get("criteria_removed", 0) for s in story_changes)
        criteria_review_required = sum(s.get("criteria_review_required", 0) for s in story_changes)
        summary = {
            "requirements_created": sum(1 for r in requirement_changes if r["change_kind"] == CHANGE_CREATED),
            "requirements_updated": sum(1 for r in requirement_changes if r["change_kind"] == CHANGE_UPDATED),
            "requirements_removed": sum(1 for r in requirement_changes if r["change_kind"] == CHANGE_REMOVED),
            "stories_created": sum(1 for s in story_changes if s["change_kind"] == CHANGE_CREATED),
            "stories_updated": sum(1 for s in story_changes if s["change_kind"] == CHANGE_UPDATED),
            "stories_removed": sum(1 for s in story_changes if s["change_kind"] == CHANGE_REMOVED),
            "criteria_added": criteria_added,
            "criteria_removed": criteria_removed,
            "criteria_review_required": criteria_review_required,
            "sections_impacted": len(impacted_sections),
            "sections_impacted_locked": sum(1 for s in impacted_sections if s.get("is_locked")),
            "diagrams_impacted": len(impacted_diagrams),
        }
        has_changes = any([
            summary["requirements_created"], summary["requirements_updated"],
            summary["requirements_removed"], summary["stories_created"],
            summary["stories_updated"], summary["stories_removed"],
            criteria_added, criteria_removed,
        ])

        current_version = current_state.get("version_number")
        if not isinstance(current_version, int):
            current_version = 1
        proposed_version = proposed_changes.get("version_number")
        if not isinstance(proposed_version, int):
            proposed_version = None

        return {
            "action_id": action.get("id") or "",
            "project_id": str(project_id),
            "action_type": action.get("action_type") or "",
            "original_user_message": action.get("original_user_message") or "",
            "current_version": current_version,
            "proposed_version": proposed_version,
            "has_changes": has_changes,
            "summary": summary,
            "requirements": requirement_changes,
            "user_stories": story_changes,
            "impacted_prd_sections": impacted_sections,
            "impacted_diagrams": impacted_diagrams,
        }

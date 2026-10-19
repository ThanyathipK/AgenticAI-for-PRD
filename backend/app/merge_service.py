"""
User Story Merge Service.

Pure, dependency-free helpers (no langchain / DB / LLM imports) that power
the multi-agent Gatherer's user story reconciliation logic.

The core entrypoint is :func:`merge_user_stories` (a thin wrapper over
:func:`merge_user_stories_with_report`), which performs an INCREMENTAL
reconciliation of newly generated user stories against previously persisted
ones WITHOUT overwriting existing data:

- Keep unchanged stories        -> ``change_type="unchanged"``, ``status="active"``
- Update modified stories       -> ``change_type="updated"``,   ``status="active"``
  (field-level: empty incoming values keep the persisted data; when story
  behaviour changes, the incoming acceptance criteria are treated as the
  complete revalidated set so obsolete criteria do not survive)
- Insert brand-new stories      -> ``change_type="created"``,  ``status="active"``
- Archive deleted stories       -> ``change_type="archived"``, ``status="archived"``
- Protected stories (locked / human-edited, via ``protected_ids``) are never
  mutated; a pending archive on one is surfaced as ``change_type="conflict"``
  (status stays ``active``) instead of being silently applied.

Matching is progressive identity resolution — exact id -> exact ticket code ->
exact title -> fuzzy title (:func:`titles_match`) — so a re-worded story title
updates the existing story instead of creating a duplicate.

Exposing this logic in a dedicated module (instead of inside ``agents.py``)
keeps it unit-testable without bootstrapping an LLM client or the LangGraph
workflow.
"""
import difflib
import json
import re
from typing import Dict, Any, List, Optional, Set, Tuple

from app.application_types import RequirementMergeReport, SemanticChangePayload


# ==========================================
# NORMALIZATION / TICKET HELPERS
# ==========================================
def normalize_ticket_code(code: Optional[str]) -> str:
    if not code:
        return ""
    code = str(code).strip().upper()
    # Accept common chat spellings while requiring the whole value to be an
    # identifier. This keeps free-form titles out of the ID path but makes
    # ``us1``, ``US 1``, ``#US-001`` and ``us_001`` equivalent.
    match = re.fullmatch(r"#?\s*US[\s_-]*(\d+)\s*", code)
    if match:
        return f"US-{int(match.group(1)):03d}"
    return code


def normalize_title(title: Optional[str]) -> str:
    if not title:
        return ""
    return " ".join(str(title).lower().strip().split())


def generate_next_ticket_code(stories: List[Dict[str, Any]]) -> str:
    max_num = 0
    for s in stories:
        tc = s.get("ticket_code", "")
        if tc and str(tc).strip().upper().startswith("US-"):
            num_part = str(tc).strip().upper()[3:]
            if num_part.isdigit():
                max_num = max(max_num, int(num_part))
    return f"US-{max_num + 1:03d}"

# ==========================================
# FUZZY MATCHING HELPERS (stdlib only)
# ==========================================
FUZZY_TITLE_THRESHOLD = 0.85
FUZZY_AC_THRESHOLD = 0.85
MERGED_STORY_FIELDS = ("story_title", "as_a", "i_want_to", "so_that")


def text_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Normalized-text similarity in ``[0.0, 1.0]`` (difflib.SequenceMatcher)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


# GATHERING 6.1.1 — Fuzzy title comparison used by 6.1 (and by the change detectors):
#               similarity above FUZZY_TITLE_THRESHOLD counts as the same story.
def titles_match(a: Optional[str], b: Optional[str], threshold: float = FUZZY_TITLE_THRESHOLD) -> bool:
    """True when two story titles are similar enough to be the same story."""
    return text_similarity(a, b) >= threshold


# ==========================================
# ACCEPTANCE-CRITERIA UNION MERGE
# ==========================================
def _normalize_ac_text(ac: Any) -> str:
    s = str(ac or "").lower().strip()
    return " ".join(s.split())


# GATHERING 6.3 — Acceptance-criteria union for a matched story: criteria are ADDED or
#             updated, never wholesale replaced, so a re-gather cannot delete ACs the
#             model simply forgot to repeat.
def merge_acceptance_criteria(
    old_ac: Optional[List[Any]],
    new_ac: Optional[List[Any]],
    threshold: float = FUZZY_AC_THRESHOLD,
) -> Tuple[List[Any], Dict[str, int]]:
    """
    Union-merges acceptance criteria instead of replacing the whole list.

    Existing criteria are never silently dropped: every old item is matched
    against the incoming items (fuzzy). A match either keeps the text
    (identical) or adopts only the richer (longer) phrasing; unmatched old
    items are KEPT as-is, unmatched new items are APPENDED.

    Returns ``(merged_list, report)`` with report counts
    ``{"kept": int, "updated": int, "added": int}``.
    """
    old_items = list(old_ac or [])
    new_items = list(new_ac or [])
    report = {"kept": 0, "updated": 0, "added": 0}

    if not new_items:
        report["kept"] = len(old_items)
        return old_items, report
    if not old_items:
        report["added"] = len(new_items)
        return new_items, report

    used_new: Set[int] = set()
    merged: List[Any] = []
    for o in old_items:
        o_norm = _normalize_ac_text(o)
        best_idx: Optional[int] = None
        best_ratio = 0.0
        for i, n in enumerate(new_items):
            if i in used_new:
                continue
            ratio = difflib.SequenceMatcher(None, o_norm, _normalize_ac_text(n)).ratio()
            if ratio > best_ratio:
                best_ratio, best_idx = ratio, i
        if best_idx is not None and best_ratio >= threshold:
            n = new_items[best_idx]
            used_new.add(best_idx)
            if _normalize_ac_text(n) == o_norm:
                merged.append(o)          # identical -> untouched existing data
                report["kept"] += 1
            else:
                # matched re-wording -> keep only the richer (longer) phrasing
                merged.append(n if len(str(n)) > len(str(o)) else o)
                report["updated"] += 1
        else:
            merged.append(o)              # never silently drop existing data
            report["kept"] += 1

    for i, n in enumerate(new_items):
        if i not in used_new:
            merged.append(n)
            report["added"] += 1

    return merged, report


# ==========================================
# FIELD-LEVEL INCREMENTAL MERGE
# ==========================================
# GATHERING 6.2 — Field-level merge for a matched story: incoming EMPTY values keep the
#             persisted data (never blank out a story), and the result records
#             `fields_changed` for the merge report / change summary (4.4.1).
def merge_story_fields(
    matched: Dict[str, Any],
    inc: Dict[str, Any],
    *,
    replace_acceptance_criteria: bool = False,
) -> Tuple[Dict[str, Any], List[str], Dict[str, int]]:
    """
    Incremental FIELD-LEVEL merge for an already-matched story.

    The incoming story wins ONLY the fields it actually provides (non-empty);
    empty incoming values keep the persisted data. Acceptance criteria are
    union-merged for an unaffected story, but a behaviour-changing update uses
    the incoming list as the complete revalidated set. This is what allows the
    hierarchy cascade to remove criteria that no longer describe the story.

    Returns ``(updated_story, changed_fields, ac_report)`` where
    ``changed_fields`` lists the story fields whose persisted value changed.
    """
    updated = dict(matched)
    changed_fields: List[str] = []

    for field in MERGED_STORY_FIELDS:
        m_val = (matched.get(field) or "").strip()
        i_val = (inc.get(field) or "").strip()
        if i_val and i_val != m_val:
            updated[field] = i_val
            changed_fields.append(field)

    old_ac = list(matched.get("acceptance_criteria", []) or [])
    has_incoming_ac = "acceptance_criteria" in inc and inc.get("acceptance_criteria") is not None
    new_ac = list(inc.get("acceptance_criteria") or []) if has_incoming_ac else old_ac
    if replace_acceptance_criteria and has_incoming_ac and new_ac:
        merged_ac = new_ac
        old_normalized = {_normalize_ac_text(item) for item in old_ac}
        new_normalized = {_normalize_ac_text(item) for item in new_ac}
        ac_report = {
            "kept": len(old_normalized & new_normalized),
            "updated": 0,
            "added": len(new_normalized - old_normalized),
            "removed": len(old_normalized - new_normalized),
        }
    else:
        merged_ac, ac_report = merge_acceptance_criteria(old_ac, new_ac)
    if merged_ac != old_ac:
        updated["acceptance_criteria"] = merged_ac
        changed_fields.append("acceptance_criteria")

    return updated, changed_fields, ac_report


# ==========================================
# PROTECTED-STORY HELPERS
# ==========================================
# GATHERING 6.4 — Protected-id set (built from GATHERING 1.3's locked story UUIDs and
#             ticket codes): a pending update on a protected story is skipped and a
#             pending archive becomes `change_type="conflict"` with status staying
#             active — the mechanism behind the "⚠️ locked story(ies) … stayed active"
#             line in 4.4.1.
def build_protected_set(protected_ids: Optional[List[str]]) -> Set[str]:
    """
    Normalizes the ``protected_ids`` allow-list (story UUIDs and/or ticket
    codes, any casing) into a lookup set.
    """
    protected: Set[str] = set()
    for p in (protected_ids or []):
        if not p:
            continue
        p_str = str(p).strip()
        protected.add(p_str)
        code = normalize_ticket_code(p_str)
        if code:
            protected.add(code)
    return protected


def is_protected_story(story: Dict[str, Any], protected_set: Set[str]) -> bool:
    """True when the story id or ticket code is in the protected set."""
    if not protected_set:
        return False
    if story.get("id") and str(story["id"]) in protected_set:
        return True
    code = normalize_ticket_code(story.get("ticket_code"))
    return bool(code) and code in protected_set


# ==========================================
# STORY IDENTITY MATCHING
# ==========================================
# GATHERING 6.1 — Identity resolution for one incoming story (the heart of "update the
#             right story"): progressive matching by story UUID → ticket code →
#             normalized title → fuzzy title (6.1.1), and None when nothing matches
#             (the story is then treated as NEW, i.e. created).
def _match_incoming_story(
    inc: Dict[str, Any],
    existing_by_id: Dict[str, Dict[str, Any]],
    existing_by_code: Dict[str, Dict[str, Any]],
    existing_by_title: Dict[str, Dict[str, Any]],
    matched_ptrs: Set[int],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Progressive identity resolution for one incoming story:

    exact id -> exact ticket code -> exact title -> fuzzy title.

    The fuzzy-title stage prevents the worst failure mode of a naive merge:
    a re-worded story title turning into a DUPLICATE story. Returns
    ``(matched_story_or_None, matched_by_or_None)``.
    """
    inc_id = str(inc["id"]) if inc.get("id") else None
    inc_code = normalize_ticket_code(inc.get("ticket_code"))
    inc_title = normalize_title(inc.get("story_title"))

    if inc_id and inc_id in existing_by_id:
        return existing_by_id[inc_id], "id"
    if inc_code and inc_code != "US-000" and inc_code in existing_by_code:
        return existing_by_code[inc_code], "ticket_code"
    if inc_title and inc_title in existing_by_title:
        return existing_by_title[inc_title], "title"
    if inc_title:
        best_story: Optional[Dict[str, Any]] = None
        best_ratio = 0.0
        for title, story in existing_by_title.items():
            if id(story) in matched_ptrs:
                continue
            ratio = difflib.SequenceMatcher(None, inc_title, title).ratio()
            if ratio >= FUZZY_TITLE_THRESHOLD and ratio > best_ratio:
                best_story, best_ratio = story, ratio
        if best_story is not None:
            return best_story, "fuzzy_title"
    return None, None


# ==========================================
# CORE MERGE LOGIC
# ==========================================
# GATHERING 6.0 — THE merge entry point (called once per gather from GATHERING 4.1,
#             and by the document-extraction and impact paths). Incremental
#             reconciliation rules live in the docstring below: unchanged / updated /
#             created / archived / conflict, with protected ids never mutated.
def merge_user_stories_with_report(
    existing_stories: List[Dict[str, Any]],
    new_incoming_stories: List[Dict[str, Any]],
    semantic_recs: Optional[List[SemanticChangePayload]] = None,
    protected_ids: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], RequirementMergeReport]:
    """
    Performs an INCREMENTAL reconciliation of newly generated user stories
    against the persisted ones WITHOUT overwriting existing data.

    - Keep unchanged stories   -> ``change_type="unchanged"``, ``status="active"``
    - Update modified stories  -> ``change_type="updated"``,   ``status="active"``
      (field-level: empty incoming values keep persisted data; acceptance
      criteria are union-merged, never wholesale-replaced)
    - Insert brand-new stories -> ``change_type="created"``,   ``status="active"``
    - Archive deleted stories  -> ``change_type="archived"``,  ``status="archived"``
    - Protected stories (``protected_ids``: story UUIDs / ticket codes of
      locked or human-edited stories) are NEVER mutated; a pending archive on
      one is surfaced as ``change_type="conflict"`` (status stays ``active``)
      instead of being silently applied.

    ``semantic_recs`` is an optional list of semantic-change recommendations
    of the form ``{"target_requirement_id": ..., "recommended_action": ...}``
    where ``recommended_action`` is one of ``INSERT``, ``UPDATE``,
    ``ARCHIVE`` or ``NO_CHANGE``.

    Returns ``(merged_stories, merge_report)``. ``merge_report`` maps an
    affected story key (its ticket code or normalized title) to::

        {
            "action": "updated"|"unchanged"|"created"|"archived"|"conflict",
            "matched_by": "id"|"ticket_code"|"title"|"fuzzy_title"|None,
            "protected": bool,
            "fields_changed": [...],       # updated stories only
            "acceptance_criteria": {...},  # kept/updated/added counts
        }
    """
    semantic_recs = semantic_recs or []
    protected_set = build_protected_set(protected_ids)

    rec_by_code = {}
    rec_by_title = {}
    for rec in semantic_recs:
        target_id = rec.get("target_requirement_id")
        action = rec.get("recommended_action")
        if target_id and action:
            norm_target = normalize_ticket_code(target_id)
            if norm_target:
                rec_by_code[norm_target] = action
            else:
                rec_by_title[normalize_title(target_id)] = action

    existing_by_id = {}
    existing_by_code = {}
    existing_by_title = {}

    active_existing = [s for s in existing_stories if s.get("status", "active") == "active"]

    for s in active_existing:
        if s.get("id"):
            existing_by_id[str(s["id"])] = s
        code = normalize_ticket_code(s.get("ticket_code"))
        if code and code != "US-000":
            existing_by_code[code] = s
        title = normalize_title(s.get("story_title"))
        if title:
            existing_by_title[title] = s

    matched_existing_ptrs: Set[int] = set()
    merged_stories: List[Dict[str, Any]] = []
    merge_report: RequirementMergeReport = {}

    # 1. Process incoming stories
    for inc in new_incoming_stories:
        inc_code = normalize_ticket_code(inc.get("ticket_code"))
        inc_title = normalize_title(inc.get("story_title"))

        matched, matched_by = _match_incoming_story(
            inc, existing_by_id, existing_by_code, existing_by_title, matched_existing_ptrs
        )

        if matched:
            matched_existing_ptrs.add(id(matched))

            m_code = normalize_ticket_code(matched.get("ticket_code"))
            report_key = m_code or inc_code or inc_title or "story"
            rec_act = (
                rec_by_code.get(inc_code)
                or rec_by_code.get(m_code)
                or rec_by_title.get(inc_title)
            )
            protected = is_protected_story(matched, protected_set)

            if rec_act == "ARCHIVE" or inc.get("status") == "archived" or inc.get("change_type") == "archived":
                if protected:
                    # NEVER silently archive protected data -> surface a conflict
                    conflict_story = dict(matched)
                    conflict_story["status"] = "active"
                    conflict_story["change_type"] = "conflict"
                    conflict_story["merge_conflict"] = "archive_requested"
                    merged_stories.append(conflict_story)
                    merge_report[report_key] = {
                        "action": "conflict",
                        "matched_by": matched_by,
                        "protected": True,
                    }
                else:
                    archived_story = dict(matched)
                    archived_story["status"] = "archived"
                    archived_story["change_type"] = "archived"
                    merged_stories.append(archived_story)
                    merge_report[report_key] = {
                        "action": "archived",
                        "matched_by": matched_by,
                        "protected": False,
                    }
                continue

            if protected:
                # Protected story: existing persisted data wins, incoming ignored
                keep_story = dict(matched)
                keep_story["status"] = "active"
                keep_story["change_type"] = "unchanged"
                merged_stories.append(keep_story)
                merge_report[report_key] = {
                    "action": "unchanged",
                    "matched_by": matched_by,
                    "protected": True,
                }
                continue

            # Incremental field merge: only what the incoming story actually
            # says is applied; empty incoming values keep persisted data.
            core_behavior_changed = any(
                (inc.get(field) or "").strip()
                and (inc.get(field) or "").strip() != (matched.get(field) or "").strip()
                for field in MERGED_STORY_FIELDS
            )
            updated_story, changed_fields, ac_report = merge_story_fields(
                matched,
                inc,
                replace_acceptance_criteria=(rec_act == "UPDATE" or core_behavior_changed),
            )

            # Ticket-code policy: keep the matched story's code unless the
            # incoming one is a real code that is unclaimed (or claimed by the
            # matched story itself) — never create code collisions.
            claimed_by_other = (
                inc_code in existing_by_code and existing_by_code[inc_code] is not matched
            )
            if inc_code and inc_code != "US-000" and not claimed_by_other:
                updated_story["ticket_code"] = inc_code
            elif not updated_story.get("ticket_code"):
                updated_story["ticket_code"] = matched.get("ticket_code") or "US-001"

            updated_story["status"] = "active"
            # Semantic-rec overrides: UPDATE forces the "updated" label even
            # without field diffs; NO_CHANGE keeps the "unchanged" label even
            # when incoming values were applied (values win, label follows
            # the recommendation).
            if rec_act == "UPDATE":
                final_change = "updated"
            elif rec_act == "NO_CHANGE":
                final_change = "unchanged"
            else:
                final_change = "updated" if changed_fields else "unchanged"
            updated_story["change_type"] = final_change
            merged_stories.append(updated_story)
            merge_report[report_key] = {
                "action": final_change,
                "matched_by": matched_by,
                "protected": False,
                "fields_changed": changed_fields,
                "acceptance_criteria": ac_report,
            }
            continue

        # Unmatched incoming -> New story insertion
        rec_act = rec_by_code.get(inc_code) or rec_by_title.get(inc_title)
        if rec_act == "ARCHIVE" or inc.get("status") == "archived":
            continue

        ticket_code = inc_code
        if not ticket_code or ticket_code == "US-000" or ticket_code in existing_by_code:
            ticket_code = generate_next_ticket_code(active_existing + merged_stories)

        new_story = {
            "ticket_code": ticket_code,
            "story_title": inc.get("story_title", "Untitled Story"),
            "as_a": inc.get("as_a", ""),
            "i_want_to": inc.get("i_want_to", ""),
            "so_that": inc.get("so_that", ""),
            "acceptance_criteria": inc.get("acceptance_criteria", []),
            "status": "active",
            "change_type": "created",
        }
        if inc.get("id"):
            new_story["id"] = inc["id"]

        merged_stories.append(new_story)
        merge_report[ticket_code or inc_title or "story"] = {
            "action": "created",
            "matched_by": None,
            "protected": False,
        }

    # 2. Process existing active stories that were NOT matched by incoming
    for ex in active_existing:
        if id(ex) in matched_existing_ptrs:
            continue

        ex_code = normalize_ticket_code(ex.get("ticket_code"))
        ex_title = normalize_title(ex.get("story_title"))
        rec_act = rec_by_code.get(ex_code) or rec_by_title.get(ex_title)
        report_key = ex_code or ex_title or "story"
        protected = is_protected_story(ex, protected_set)

        if rec_act == "ARCHIVE":
            if protected:
                # NEVER silently archive protected data -> surface a conflict
                conflict_story = dict(ex)
                conflict_story["status"] = "active"
                conflict_story["change_type"] = "conflict"
                conflict_story["merge_conflict"] = "archive_requested"
                merged_stories.append(conflict_story)
                merge_report[report_key] = {
                    "action": "conflict",
                    "matched_by": None,
                    "protected": True,
                }
            else:
                archived_story = dict(ex)
                archived_story["status"] = "archived"
                archived_story["change_type"] = "archived"
                merged_stories.append(archived_story)
                merge_report[report_key] = {
                    "action": "archived",
                    "matched_by": None,
                    "protected": False,
                }
        else:
            # KEEP UNCHANGED!
            unchanged_story = dict(ex)
            unchanged_story["status"] = "active"
            unchanged_story["change_type"] = "unchanged"
            merged_stories.append(unchanged_story)
            merge_report[report_key] = {
                "action": "unchanged",
                "matched_by": None,
                "protected": protected,
            }

    return merged_stories, merge_report


def merge_user_stories(
    existing_stories: List[Dict[str, Any]],
    new_incoming_stories: List[Dict[str, Any]],
    semantic_recs: Optional[List[SemanticChangePayload]] = None,
    protected_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Backward-compatible wrapper around :func:`merge_user_stories_with_report`
    returning only the merged story list.
    """
    merged_stories, _merge_report = merge_user_stories_with_report(
        existing_stories=existing_stories,
        new_incoming_stories=new_incoming_stories,
        semantic_recs=semantic_recs,
        protected_ids=protected_ids,
    )
    return merged_stories


# GATHERING 6.5 — Projection helper: flattens the active stories' acceptance criteria
#             into the board-level `acceptance_criteria` list (used by the gatherer's
#             draft short-circuit 1.5.1 and by the final state assembly in 4.2).
def collect_acceptance_criteria(stories: List[Dict[str, Any]]) -> List[Any]:
    """Flattens the ``acceptance_criteria`` of every story into a single list."""
    all_ac: List[Any] = []
    for story in stories:
        all_ac.extend(story.get("acceptance_criteria", []))
    return all_ac


# GATHERING 6.6 — Shared prompt-context formatter for the backlog: the SAME rendering
#             is used by the gatherer prompt (2.6), the semantic detector (5.2), the
#             intent detector (INTENT 2.2) and the matcher (MATCHING 2.2), so every
#             agent describes the stories identically.
def format_story_context_lines(stories: List[Dict[str, Any]]) -> List[str]:
    """Render user stories as the canonical multi-line context block.

    Shared by the Gatherer prompt, semantic change detection, intent
    classification and requirement matching so every agent describes the
    backlog to the LLM in exactly the same shape (previously this formatting
    was copy-pasted in four places and could drift apart).
    """
    lines: List[str] = []
    for story in stories:
        lines.append(
            f"- Story {story.get('ticket_code', 'UNKNOWN')}: '{story.get('story_title', '')}'\n"
            f"  As a {story.get('as_a', '')}, I want to {story.get('i_want_to', '')}, So that {story.get('so_that', '')}\n"
            f"  Acceptance Criteria: {json.dumps(story.get('acceptance_criteria', []))}"
        )
    return lines


# ==========================================
# POST-MERGE HELPERS (shared by gatherer_node)
# ==========================================
# GATHERING 6.7 — Active-only filter used by the document-extraction draft builder and
#             the UI projections (archived stories stay in the board payload but are
#             not offered as current work).
def filter_active_stories(stories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Returns only the stories whose status is ``"active"`` (default if unset)."""
    return [s for s in stories if s.get("status", "active") == "active"]

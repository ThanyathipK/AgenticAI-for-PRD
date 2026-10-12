"""
PRD version ledger service.

Every change to the document — an AI regeneration ("Generate PRD") or a manual
part edit / revert — is captured as an immutable ``prd_versions`` snapshot so
the Version History tab can show exactly how many versions exist and what each
one changed::

  - ``record_prd_version``  computes the per-section change records for the new
    snapshot (created / updated / unchanged / locked_preserved), persists the
    version row, advances ``requirement_states.version_number`` and returns it.
  - ``diff_versions``       renders a section-level line diff between any two
    stored versions for the interactive Difference Analysis pane.

Lock contract: when a section is locked (or human-owned, ``ai_generatable``
False) and the incoming source would have changed it, the linkage is recorded
as ``locked_preserved`` — the snapshot still advances, but the ledger proves the
lock was honoured.
"""
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.prd_section_service import split_markdown_sections
from app.repositories.prd import PRDVersionRepository
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.review_service import mark_revised_if_approved

logger = logging.getLogger(__name__)

# change_kind values persisted in prd_versions.changed_sections.
CHANGE_CREATED = "created"
CHANGE_UPDATED = "updated"
CHANGE_UNCHANGED = "unchanged"
CHANGE_REMOVED = "removed"
CHANGE_LOCKED_PRESERVED = "locked_preserved"

_CHANGED_KINDS = {CHANGE_CREATED, CHANGE_UPDATED, CHANGE_REMOVED, CHANGE_LOCKED_PRESERVED}


# VERSION 3.2 — Split helper (used by 3.1 and 3.8): a full document → {key: content}
#             + {key: title} maps via the canonical splitter shared with the PRD
#             sections and the frontend (parity requirement).
def _split_to_map(document: Optional[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Split a full PRD document into {section_key: content} and
    {section_key: title}; empty/missing documents yield empty maps."""
    keys: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    for part in split_markdown_sections(document or ""):
        keys[part["section_key"]] = part["content"].strip()
        titles[part["section_key"]] = part["title"]
    return keys, titles


# VERSION 3.1 — Section-change computation (persisted in
#             prd_versions.changed_sections and shown in the timeline): one record per
#             section — created / unchanged / updated / removed, plus
#             `locked_preserved` when a LOCKED part would have changed. That last kind
#             proves the AI-generation lock contract while the version still advances.
def compute_section_changes(
    prev_document: Optional[str],
    new_document: Optional[str],
    *,
    locked_keys: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Compare two full PRD documents part-by-part and return one change record
    per section, in new-document order (previous-only parts appended last).

    ``locked_keys`` are the section keys of currently locked parts. When a
    locked part WOULD have changed (content differs between the documents) it
    is reported as ``locked_preserved`` instead of ``updated``, proving the
    AI-generation lock contract while the version still advances.
    """
    locked = locked_keys or set()
    prev_keys, prev_titles = _split_to_map(prev_document)
    new_keys, new_titles = _split_to_map(new_document)

    records: List[Dict[str, Any]] = []
    for key, new_content in new_keys.items():
        prev_content = prev_keys.get(key)
        title = new_titles.get(key, prev_titles.get(key, key))
        if prev_content is None:
            kind = CHANGE_CREATED
        elif prev_content == new_content:
            kind = CHANGE_UNCHANGED
        elif key in locked:
            kind = CHANGE_LOCKED_PRESERVED
        else:
            kind = CHANGE_UPDATED
        records.append({
            "section_key": key,
            "title": title,
            "change_kind": kind,
            "changed": kind in _CHANGED_KINDS,
        })

    # Parts present in the previous document but gone from the new one.
    for key in sorted(prev_keys.keys() - new_keys.keys()):
        records.append({
            "section_key": key,
            "title": prev_titles.get(key, key),
            "change_kind": CHANGE_REMOVED,
            "changed": True,
        })
    return records


# VERSION 3.3 — Fallback change summary (used by 3.6 when the caller passes none):
#             names the manual-edited sections, or defaults to the AI-regeneration
#             wording.
def _default_change_summary(change_type: str, changed: List[Dict[str, Any]]) -> str:
    """Human-readable fallback summary derived from the change records."""
    if change_type == "manual":
        titles = [c["title"] or c["section_key"] for c in changed
                  if c.get("change_kind") in (CHANGE_UPDATED, CHANGE_CREATED)]
        if titles:
            return "Manual edit of " + ", ".join(titles[:3]) + (
                f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""
            )
        return "Manual edit recorded (no section content changed)."
    return "Regenerated by the Architect agent."


# VERSION 3.4 — Semver parsing helper for 3.5 (malformed/absent → 0.0.0, which is what
#             makes the FIRST snapshot come out as 1.0.0).
def _parse_semver(semver: Optional[str]) -> Tuple[int, int, int]:
    """Parse 'MAJOR.MINOR.PATCH' into ints; malformed values default to 0.0.0."""
    try:
        parts = (semver or "").strip().split(".")
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return (0, 0, 0)


# VERSION 3.5 — Semver derivation from 3.1's records: MAJOR on any created/removed
#             section (document shape changed), MINOR on AI content updates, PATCH for
#             manual edits/reverts and for always-advancing no-content-change snapshots
#             (e.g. every changed part was locked).
def compute_next_semver(
    previous_semver: Optional[str],
    change_type: str,
    changed: List[Dict[str, Any]],
) -> str:
    """
    Derive the Semantic Version of a new snapshot from the previous one.

    Rules (first snapshot always becomes 1.0.0):

      - MAJOR  structural change — any section ``created`` or ``removed``
               (the document's shape changed, downstream consumers must re-check)
      - MINOR  AI regeneration that ``updated`` section content
               (new AI output in existing parts, backwards compatible)
      - PATCH  manual part edit / revert, or an always-advancing snapshot with
               no content change (e.g. every changed part was locked)
    """
    kinds = {c.get("change_kind") for c in changed}
    major, minor, patch = _parse_semver(previous_semver)
    if CHANGE_CREATED in kinds or CHANGE_REMOVED in kinds:
        return f"{major + 1}.0.0"
    if change_type != "manual" and CHANGE_UPDATED in kinds:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# VERSION 3.6 — THE ledger entry point of the VERSIONING flow. Called by
#             ARCHITECT 6.0 (Generate PRD), CONFIRM 3.3.7 (confirmed merge),
#             PROJECT 8.2.3 (legacy direct state write) and the PRD-SECTION
#             edit/revert/restore paths. Layering: 3.2 → 3.1 → 3.5 → 4.1 → state
#             version lock-step. The version ALWAYS advances (no "content unchanged"
#             short-circuit) so the ledger never collapses, and every row carries
#             change_type, a change summary and per-section records incl.
#             `locked_preserved` markers.
async def record_prd_version(
    project_id: str,
    session: AsyncSession,
    *,
    generated_prd: str,
    generated_by: str = "automated_agent",
    change_type: str = "ai",
    change_summary: Optional[str] = None,
    version_number: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Persist a NEW immutable PRD snapshot for the project and advance the
    project's ``version_number``.

    The version ALWAYS advances — there is no "content unchanged" short-circuit
    (the requirement is that Generate PRD / manual edits always produce a new
    version even when every changed section was locked). The snapshot stores:

      - the full final document,
      - ``change_type`` ('ai' | 'manual'),
      - a change summary (auto-derived when omitted),
      - per-section change records incl. ``locked_preserved`` markers.

    ``version_number`` optionally PINS the ledger row's number (e.g. a
    confirmed merge whose requirement-state version was already advanced
    exactly once) so the ledger and the requirement version stay in
    lock-step instead of bumping twice. When omitted the row simply takes
    ``max(existing ledger) + 1``.

    Returns the serialized version row.
    """
    previous = await PRDVersionRepository.get_latest(project_id, session)
    prev_document = previous["generated_prd"] if previous else ""

    # Current locked sections (post-merge) — the lock contract source of truth.
    locked_keys: Set[str] = set()
    try:
        sections = await PRDSectionRepository.get_by_project(project_id, session)
        locked_keys = {s["section_key"] for s in sections if s.get("is_locked")}
    except Exception as lock_err:  # never fail versioning on lock bookkeeping
        logger.warning("[PRD VERSIONS] Could not read section locks (%s); assuming none.", lock_err)

    changed = compute_section_changes(
        prev_document, generated_prd or "", locked_keys=locked_keys,
    )

    # Semantic Version: MAJOR (sections created/removed) / MINOR (AI content
    # updates) / PATCH (manual edits + no-change advances). The first snapshot
    # becomes 1.0.0.
    semver = compute_next_semver(
        (previous or {}).get("semver"), change_type, changed,
    )

    version = await PRDVersionRepository.create(project_id, {
        "generated_prd": generated_prd or "",
        "generated_by": generated_by,
        "change_type": change_type,
        "change_summary": change_summary
            or _default_change_summary(change_type, changed),
        "changed_sections": changed,
        "semver": semver,
    }, session, version_number=version_number)

    if any(item.get("change_kind") in {CHANGE_CREATED, CHANGE_UPDATED, CHANGE_REMOVED} for item in changed):
        await mark_revised_if_approved(
            project_id,
            session,
            reason="The approved PRD content changed and requires review again.",
            actor_name=generated_by,
            actor_role="automated_agent" if change_type == "ai" else "editor",
        )

    # Keep the project's current version number in lock-step with the ledger so
    # the stamped document label (cover + Version History show this row's
    # semver) and the UI's currentVersion always agree.
    try:
        await RequirementStateRepository.save_or_update(project_id, {
            "version_number": version["version_number"],
        }, session)
    except Exception as ver_err:  # state row is optional — never break versioning
        logger.warning(
            "[PRD VERSIONS] Could not advance requirement_states.version_number (%s).",
            ver_err,
        )

    logger.info(
        "[PRD VERSIONS] Recorded %s v%d for project %s (change_kinds=%s)",
        change_type, version["version_number"], project_id,
        [c["change_kind"] for c in changed],
    )
    return version
# VERSION 3.7 — Line-level diff engine (difflib SequenceMatcher) used by 3.8: returns
#             [["add"|"del"|"context", line], …] with a bounded context window plus
#             added/removed counters for the diff cards (1.3/1.4).
def _line_diff(was: Optional[str], now: Optional[str], max_context: int = 2) -> Dict[str, Any]:
    """Compact line-level diff between two section contents.

    Returns ``{diff_lines: [["add"|"del"|"context", line], ...],
    added, removed}``. ``max_context`` context lines surround each change hunk.
    """
    import difflib

    a = (was or "").splitlines()
    b = (now or "").splitlines()

    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    lines: List[List[Any]] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            idx = 0
            for line in a[i1:i2]:
                if idx < max_context or (i2 - i1 - idx) <= max_context:
                    lines.append(["context", line])
                idx += 1
        elif tag == "replace":
            removed += (i2 - i1)
            added += (j2 - j1)
            for line in a[i1:i2]:
                lines.append(["del", line])
            for line in b[j1:j2]:
                lines.append(["add", line])
        elif tag == "delete":
            removed += (i2 - i1)
            for line in a[i1:i2]:
                lines.append(["del", line])
        elif tag == "insert":
            added += (j2 - j1)
            for line in b[j1:j2]:
                lines.append(["add", line])
    return {"diff_lines": lines, "added": added, "removed": removed}


# VERSION 3.8 — Diff service behind VERSION 2.2 (route-called only): compares a stored
#             version against an earlier base (default = its immediate predecessor via
#             4.2) and returns per-section line diffs with their change kind —
#             locked-preserved parts are labelled explicitly and removed parts carry
#             their deleted lines. Returns None when the target or the base is missing
#             (the route turns that into 404).
async def diff_versions(
    project_id: str,
    to_version: int,
    session: AsyncSession,
    *,
    base_version: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Diff one stored PRD version against an earlier base version (default: the
    immediately preceding version). Returns the per-section line diff plus each
    section's change kind — locked-preserved sections are marked explicitly.

    The first snapshot is compared with an empty document so its initially
    created lines can be inspected. Returns None only when the target or an
    explicitly requested base does not exist.
    """
    target = await PRDVersionRepository.get_by_version_number(project_id, to_version, session)
    if not target:
        return None

    if base_version is None:
        prev = await PRDVersionRepository.get_previous(project_id, to_version, session)
        if not prev:
            base_version = 0
            base_doc = ""
        else:
            base_version = prev["version_number"]
            base_doc = prev["generated_prd"]
    else:
        if base_version >= to_version:
            return None
        base_rec = await PRDVersionRepository.get_by_version_number(project_id, base_version, session)
        if not base_rec:
            return None
        base_doc = base_rec["generated_prd"]

    locked_keys: Set[str] = set()
    try:
        sections = await PRDSectionRepository.get_by_project(project_id, session)
        locked_keys = {s["section_key"] for s in sections if s.get("is_locked")}
    except Exception as lock_err:
        logger.warning("[PRD VERSIONS] Could not read section locks for diff (%s).", lock_err)

    prev_map, prev_titles = _split_to_map(base_doc)
    now_map, now_titles = _split_to_map(target["generated_prd"])

    section_diffs: List[Dict[str, Any]] = []
    for key, new_content in now_map.items():
        old_content = prev_map.get(key)
        title = now_titles.get(key, prev_titles.get(key, key))
        if old_content is None:
            kind, changed = CHANGE_CREATED, True
        elif old_content == new_content:
            kind, changed = CHANGE_UNCHANGED, False
        elif key in locked_keys:
            kind, changed = CHANGE_LOCKED_PRESERVED, True
        else:
            kind, changed = CHANGE_UPDATED, True
        diff = _line_diff(old_content, new_content)
        section_diffs.append({
            "section_key": key,
            "title": title,
            "change_kind": kind,
            "changed": changed,
            **diff,
        })

    for key in sorted(prev_map.keys() - now_map.keys()):
        section_diffs.append({
            "section_key": key,
            "title": prev_titles.get(key, key),
            "change_kind": CHANGE_REMOVED,
            "changed": True,
            "diff_lines": [["del", line] for line in (prev_map.get(key) or "").splitlines()],
            "added": 0,
            "removed": len((prev_map.get(key) or "").splitlines()),
        })

    return {
        "project_id": project_id,
        "to_version": to_version,
        "base_version": base_version,
        "sections": section_diffs,
    }


# VERSION 3.9 — PRE-RECORD semver resolution for DOCUMENT STAMPING: computes the
#               EXACT semver record_prd_version (3.6) will assign to the NEXT snapshot,
#               from the same inputs (latest ledger row + current locks → 3.1 changes →
#               3.5 next semver), so the version printed ON the document (cover +
#               Version History section) is provably the value that lands in
#               prd_versions.semver — never a fabricated V{n}.0. Callers fill the
#               template with prd_filler.PENDING_VERSION_LABEL and stamp the returned
#               value in (prd_filler.stamp_version_label) BEFORE the document is
#               stored/exported. FAIL-SOFT by contract: any failure (no ledger yet is
#               NOT a failure — it just means 1.0.0; unreadable locks assume none;
#               pandoc/conversion trouble) returns None and the caller keeps its
#               legacy V{n}.0 fallback label.
async def resolve_document_semver(
    project_id: str,
    session: AsyncSession,
    *,
    document: str,
    change_type: str = "ai",
) -> Optional[str]:
    """The semver the NEXT ledger snapshot for ``project_id`` will receive.

    ``document`` is the not-yet-recorded document (LaTeX or markdown). It is
    normalized to markdown with the same ``prd_to_markdown`` step the section
    sync uses, so :func:`compute_section_changes` compares it against the
    stored snapshot exactly the way :func:`record_prd_version` later will
    (once human-owned/locked parts have been re-merged, those parts only ever
    move change records "updated → unchanged/locked_preserved", which keeps
    the derived semver identical).

    Returns ``None`` when the semver cannot be determined (fail-soft) — the
    caller then falls back to its legacy display label.
    """
    try:
        previous = await PRDVersionRepository.get_latest(project_id, session)
        prev_document = previous["generated_prd"] if previous else ""

        # Same lock source of truth as record_prd_version — a locked part that
        # would have changed is locked_preserved, never updated.
        locked_keys: Set[str] = set()
        try:
            sections = await PRDSectionRepository.get_by_project(project_id, session)
            locked_keys = {s["section_key"] for s in sections if s.get("is_locked")}
        except Exception as lock_err:
            logger.warning(
                "[PRD VERSIONS] Could not read section locks for semver pre-resolution (%s); "
                "assuming none.", lock_err,
            )

        try:
            from app.latex_service import prd_to_markdown
            new_document = prd_to_markdown(document or "")
        except Exception as conv_err:
            logger.warning(
                "[PRD VERSIONS] Could not normalize document for semver pre-resolution (%s).",
                conv_err,
            )
            return None

        changed = compute_section_changes(prev_document, new_document, locked_keys=locked_keys)
        semver = compute_next_semver((previous or {}).get("semver"), change_type, changed)
        logger.info(
            "[PRD VERSIONS] Pre-resolved semver %s for project %s (change_kinds=%s)",
            semver, project_id, [c["change_kind"] for c in changed],
        )
        return semver
    except Exception as err:
        logger.warning(
            "[PRD VERSIONS] Failed to pre-resolve semver for project %s (%s).",
            project_id, err,
        )
        return None

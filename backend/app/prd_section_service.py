"""
PRD section service — the PRD as a collection of editable, lockable, versioned
PARTS.

The official Krungsri Nimble PRD always renders as the SAME nine parts in the
preview (cover, stakeholders, version history, reviews, contents, 1. business
& strategic overview, 2. product scope, 3. technical & operational, 4.
appendix). This module:

  - defines those canonical parts (:data:`CANONICAL_PRD_SECTIONS`),
  - splits a full PRD markdown document into those parts
    (:func:`split_markdown_sections`) using the EXACT same rules as the
    frontend ``parsePRDToSections`` (``src/utils/markdown.ts``), so a
    ``section_key`` always identifies the same part the user sees,
  - seeds the ``prd_sections`` table for a project on first access
    (:func:`ensure_sections_seeded`),
  - syncs a freshly generated (LaTeX or markdown) PRD into the table while
    PRESERVING human-owned / locked sections (:func:`sync_sections_from_prd`),
  - stitches the stored parts back into the full document
    (:func:`assemble_document_markdown`).

Section content is always stored as MARKDOWN (the preview/human-editing
format). LaTeX sources are normalized via ``latex_service.prd_to_markdown``
before splitting; markdown sources pass through untouched.
"""
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.prd_section import PRDSectionRepository

logger = logging.getLogger(__name__)

# The nine canonical PRD parts, in document order. ``section_key`` values match
# the ids produced by the frontend ``parsePRDToSections`` one-to-one:
#   - the cover/front-matter block before the first heading gets id "title"
#   - 'contents' is born locked: it is derived structure nobody edits
#   - 'reviews' is human-only (signatures/dates) — the AI never generates it
# PRD-SECTION 3.1 — The nine canonical lockable parts (the contract shared with the
#             frontend splitter/stitcher and with the locks UI): key, title, order and
#             the two ownership flags — `content_source` (template | ai | human) and
#             `ai_generatable` (False = human-owned, never rewritten by regeneration).
#             `contents` ships locked by default so the generated table of contents is
#             not disturbed.
CANONICAL_PRD_SECTIONS: List[Dict[str, Any]] = [
    {"section_key": "title", "title": "Cover & Document Information", "section_order": 1,
     "content_source": "template", "ai_generatable": True, "is_locked": False},
    {"section_key": "stakeholders", "title": "Stakeholders", "section_order": 2,
     "content_source": "ai", "ai_generatable": True, "is_locked": False},
    {"section_key": "version_history", "title": "Version History", "section_order": 3,
     "content_source": "template", "ai_generatable": True, "is_locked": False},
    {"section_key": "reviews", "title": "Reviews", "section_order": 4,
     "content_source": "human", "ai_generatable": False, "is_locked": False},
    {"section_key": "contents", "title": "Contents", "section_order": 5,
     "content_source": "template", "ai_generatable": False, "is_locked": True},
    {"section_key": "business_overview", "title": "1. Business & Strategic Overview", "section_order": 6,
     "content_source": "ai", "ai_generatable": True, "is_locked": False},
    {"section_key": "product_scope", "title": "2. Product Scope & Functional Requirements", "section_order": 7,
     "content_source": "ai", "ai_generatable": True, "is_locked": False},
    {"section_key": "tech_ops", "title": "3. Technical & Operational Considerations", "section_order": 8,
     "content_source": "ai", "ai_generatable": True, "is_locked": False},
    {"section_key": "appendix", "title": "4. Appendix", "section_order": 9,
     "content_source": "ai", "ai_generatable": True, "is_locked": False},
]

CANONICAL_KEYS = {c["section_key"] for c in CANONICAL_PRD_SECTIONS}

# PRD-SECTION 3.1.1 — Accepted review_status values (validated at the route, PRD-SECTION
#               2.2): a piecewise review tracker on each part, independent of locking.
VALID_REVIEW_STATUSES = {"draft", "satisfied", "approved"}


def preview_prd_sections(project_id: str, source_markdown: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build the nine-section editor view without writing database rows.

    The former GET route called :func:`ensure_sections_seeded`, making a read
    mutate governance state and inflate pending-section health metrics. Write
    endpoints still seed explicitly before their first mutation.
    """
    source = (source_markdown or "").strip()
    if source:
        try:
            from app.latex_service import is_markdown_prd, prd_to_markdown
            if not is_markdown_prd(source):
                source = prd_to_markdown(source)
        except Exception as exc:
            logger.warning("Could not normalize PRD preview for %s: %s", project_id, exc)
            source = ""
    if not source:
        from app.prompt_loader import load_prd_template
        source = load_prd_template()

    parts = {part["section_key"]: part for part in split_markdown_sections(source)}
    namespace = uuid.UUID(str(project_id))
    preview: List[Dict[str, Any]] = []
    for defaults in CANONICAL_PRD_SECTIONS:
        part = parts.get(defaults["section_key"], {})
        preview.append({
            **defaults,
            "id": str(uuid.uuid5(namespace, f"prd-section:{defaults['section_key']}")),
            "project_id": str(project_id),
            "content": part.get("content", ""),
            "review_status": "draft",
            "locked_by": None,
            "locked_at": None,
            "version_number": None,
            "created_at": "",
            "updated_at": "",
            "is_preview": True,
        })
    return preview


# PRD-SECTION 3.2 — Heading → stable section key. This is a 1:1 PORT of the frontend's
#             keyword mapping (src/utils/markdown.ts::parsePRDToSections): both sides
#             must agree or a manually saved part would land on a different key than the
#             UI displays. Unmatched headings fall back to a slugified key.
def _derive_section_key(title: str) -> str:
    """Derive a stable section key from a heading — a 1:1 port of the keyword
    mapping in ``src/utils/markdown.ts::parsePRDToSections``."""
    t = title.lower()
    # Pandoc can strip the text from the centred Contents heading in the
    # official template and leave a bare ``###``.  It is the only intentionally
    # empty top-level heading and must remain its own lockable canonical part.
    if not t.strip():
        return "contents"
    # The markdown fallback template uses a level-two brand heading as part of
    # the cover. It is not an eleventh editable section: keep its PMO fields
    # together with the front matter in the canonical ``title`` part.
    if "nimble by krungsri" in t:
        return "title"
    if "executive summary" in t:
        return "exec_summary"
    if "technical architecture" in t or "technical infrastructure" in t or "technical constraints" in t:
        return "tech_arch"
    if "scope of requirements" in t or "user stories" in t:
        return "user_stories"
    if "stakeholder" in t:
        return "stakeholders"
    if "version history" in t:
        return "version_history"
    if "review" in t:
        return "reviews"
    if "contents" in t:
        return "contents"
    if "business & strategic" in t:
        return "business_overview"
    if "product scope" in t or "functional requirement" in t:
        return "product_scope"
    if "technical & operational" in t:
        return "tech_ops"
    if "appendix" in t:
        return "appendix"
    key = re.sub(r"\*", "", t)
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key or "section"


# PRD-SECTION 3.3 — Document splitter (parity with the frontend splitter): boundaries
#             are `## ` and `### ` headings, the block before the first heading is the
#             `title` (cover) part, and each part's content INCLUDES its heading line so
#             3.4 stitching is lossless. Used by VERSION 3.1/3.2/3.8 and by 3.6 seeding.
def split_markdown_sections(markdown: str) -> List[Dict[str, str]]:
    """Split a full PRD markdown document into its parts.

    Mirrors the frontend splitter: boundaries are '## ' and '### ' headings,
    the block before the first heading is the 'title' (cover) part, and each
    part's content INCLUDES its heading line so stitching is lossless.
    """
    if not markdown or not markdown.strip():
        return []

    parts: List[Dict[str, str]] = []
    current_key = "title"
    current_title = "Product Requirement Document"
    current_lines: List[str] = []

    for line in markdown.split("\n"):
        trimmed = line.strip()
        heading = re.match(r"^#{2,3}(?:\s+(.*)|$)", trimmed)
        if heading:
            title = (heading.group(1) or "").strip()
            if not title:
                title = "Contents"
            next_key = _derive_section_key(title)
            # ``## Nimble by Krungsri`` is a visual cover heading in the
            # markdown fallback, not a section boundary.  Folding it into the
            # preceding front matter prevents an empty ``title`` plus a stray
            # tenth ``nimble_by_krungsri`` section.
            if next_key == "title" and current_key == "title":
                current_lines.append(line)
                continue
            parts.append({
                "section_key": current_key,
                "title": current_title,
                "content": "\n".join(current_lines).strip(),
            })
            current_title = title
            current_key = next_key
            current_lines = [line]
        else:
            current_lines.append(line)

    parts.append({
        "section_key": current_key,
        "title": current_title,
        "content": "\n".join(current_lines).strip(),
    })

    return [p for p in parts if p["content"] != "" or p["section_key"] == "title"]


# PRD-SECTION 3.4 — Document stitcher: canonical-order assembly of the stored parts,
#             producing the single document that is persisted as generated_prd and as the
#             ledger snapshot (3.7 → VERSION 3.6). Because it emits every part
#             untouched, human and locked content survive every regeneration.
def stitch_markdown(sections: List[Dict[str, Any]]) -> str:
    """Assemble the full PRD markdown from ordered section rows (same join
    rule as the frontend ``stitchSectionsToPRD``)."""
    ordered = sorted(
        sections,
        key=lambda s: (s.get("section_order", 0) or 0, s.get("created_at") or ""),
    )
    return "\n\n".join((s.get("content") or "").strip() for s in ordered if s.get("content"))


def merge_sections_for_preview(
    prd_markdown: str,
    stored_sections: List[Dict[str, Any]],
    *,
    selected_section_keys: Optional[Set[str]] = None,
) -> Optional[str]:
    """Compose a read-only PRD candidate while preserving governed parts.

    Architect previews intentionally do not write ``prd_sections``.  They must
    nevertheless follow the same ownership rules as the eventual save: locked,
    human-owned, and out-of-scope parts come verbatim from storage, while only
    eligible generated parts come from the new candidate.
    """
    source = (prd_markdown or "").strip()
    if not source:
        return None
    try:
        from app.latex_service import prd_to_markdown

        markdown = prd_to_markdown(source)
    except Exception as exc:
        logger.warning("Could not normalize PRD preview for governed merge: %s", exc)
        return None

    generated_parts = split_markdown_sections(markdown)
    if not generated_parts:
        return None

    merged = {str(section.get("section_key")): dict(section) for section in stored_sections}
    selected = set(selected_section_keys) if selected_section_keys is not None else None
    defaults_by_key = {item["section_key"]: item for item in CANONICAL_PRD_SECTIONS}
    for order, part in enumerate(generated_parts, start=1):
        key = part["section_key"]
        existing = merged.get(key)
        may_replace = (
            existing is None
            or (
                not existing.get("is_locked")
                and (existing.get("ai_generatable", True) or key == "title")
                and (selected is None or key in selected)
            )
        )
        if may_replace:
            defaults = defaults_by_key.get(key, {})
            merged[key] = {
                **defaults,
                **(existing or {}),
                **part,
                "section_order": (existing or {}).get("section_order")
                or defaults.get("section_order")
                or order,
            }

    return stitch_markdown(list(merged.values()))


# PRD-SECTION 3.5 — THREE-WAY merge for a manual part edit (called by PRD-SECTION 2.2).
#             Inputs: `base_content` (what the editor was seeded from), `current_content`
#             (latest stored part — possibly advanced by an AI run during the edit) and
#             `edited_content` (the user's buffer).
#             Contract: the user's touched lines win, concurrent AI changes outside the
#             edit window survive, AI insertions are always kept, user deletions stick.
#             No base (legacy client) ⇒ verbatim store; base == current (the common case)
#             ⇒ byte-identical to the edit, so a manual save can never wipe content.
def merge_edited_section(
    base_content: Optional[str],
    current_content: Optional[str],
    edited_content: str,
) -> str:
    """Three-way merge for a manual section edit.

    ``base_content`` is the section text the user STARTED editing (the
    version the editor buffer was seeded from), ``current_content`` is the
    latest stored section text (possibly advanced by an AI regeneration while
    the user was editing), and ``edited_content`` is the user's full edited
    text.

    Merge rule per base line: the user's version wins wherever the user
    touched the line; everywhere else the current stored line wins. Lines
    inserted by either side are always kept; lines deleted by the user stay
    deleted.

    When there is no base (unknown/legacy client) the edit is stored
    verbatim. When base == current (no concurrent change — the common case)
    the result is exactly the user's edited text, byte-for-byte, so a manual
    save can NEVER wipe content in the common case.

    An EMPTY stored part (``current_content`` blank) is treated as "no stored
    version" rather than a concurrent deletion, so the edit is stored verbatim
    instead of collapsing every untouched line — a manual save must never
    merge a full section down against an empty/never-populated template.
    """
    edited = edited_content or ""
    if not base_content or not base_content.strip():
        return edited

    # A stored part that carries no text (never populated, or a placeholder row)
    # is NOT a "the AI deleted the whole section" signal — there is simply no
    # stored version to reconcile against. The editor's base is the latest text
    # the user actually saw, so store the edit verbatim instead of collapsing
    # every untouched line against the empty stored part.
    if not (current_content or "").strip():
        return edited

    import difflib

    base_lines = (base_content or "").splitlines()
    current_lines = (current_content or "").splitlines()
    edited_lines = edited.splitlines()

    # Fast path: nothing changed concurrently — store the edit verbatim.
    if base_lines == current_lines:
        return edited

    user_ops = difflib.SequenceMatcher(
        a=base_lines, b=edited_lines, autojunk=False
    ).get_opcodes()

    # 1. For every base line, record the user's replacement lines (None =
    #    untouched, [] = deleted by user).
    user_line_map: Dict[int, Optional[List[str]]] = {}
    for tag, ui1, ui2, uj1, uj2 in user_ops:
        if tag == "equal":
            for k in range(ui1, ui2):
                user_line_map[k] = None
        elif tag == "delete":
            for k in range(ui1, ui2):
                user_line_map[k] = []
        else:  # replace: map each base line to its share of edited lines
            span = uj2 - uj1
            width = ui2 - ui1
            for n, k in enumerate(range(ui1, ui2)):
                lo = uj1 + (span * n) // width
                hi = uj1 + (span * (n + 1)) // width
                user_line_map[k] = edited_lines[lo:hi]
    user_inserts: Dict[int, List[str]] = {}
    for tag, ui1, _ui2, uj1, uj2 in user_ops:
        if tag == "insert":
            user_inserts.setdefault(ui1, []).extend(edited_lines[uj1:uj2])

    # 2. Walk base-vs-current; decide each emitted line.
    out: List[str] = []
    cur_ops = difflib.SequenceMatcher(
        a=base_lines, b=current_lines, autojunk=False
    ).get_opcodes()
    for tag, i1, i2, j1, j2 in cur_ops:
        # User insertions anchored at this base offset come first.
        for pos in range(i1, i1 + 1):
            if pos in user_inserts:
                out.extend(user_inserts[pos])
        if tag == "equal":
            for k in range(i1, i2):
                rep = user_line_map.get(k, None)
                out.extend(current_lines[j1 + (k - i1): j1 + (k - i1) + 1] if rep is None else rep)
        elif tag == "delete":
            for k in range(i1, i2):
                rep = user_line_map.get(k, None)
                if rep is None:
                    continue  # AI deletion of untouched line stands
                out.extend(rep)  # user touched it — edit wins
        elif tag == "insert":
            out.extend(current_lines[j1:j2])  # AI insertions always kept
        else:  # replace — AI rewrote this base range
            for k in range(i1, i2):
                rep = user_line_map.get(k, None)
                if rep is None:
                    # Untouched by user: keep AI's corresponding line when the
                    # rewrite is 1:1, else keep the whole AI block once.
                    if i2 - i1 == j2 - j1:
                        out.append(current_lines[j1 + (k - i1)])
                    elif k == i1:
                        out.extend(current_lines[j1:j2])
                else:
                    out.extend(rep)  # user's edit wins

    # 3. User insertions anchored at EOF (or any anchor never visited above).
    visited = {i1 for tag, i1, _i2, _j1, _j2 in cur_ops if tag == "insert"}
    for pos in sorted(user_inserts):
        if pos not in visited:
            out.extend(user_inserts[pos])

    return "\n".join(out)


# ARCHITECT 2.6.2 — Alternative 2.6 reuse signal: recognizes a document that is
#               the stitched output of the canonical PRD parts (the shape the frontend's
#               splitter/stitcher produces), so a document that does not carry the
#               template marker but IS sectioned is still reused rather than regenerated.
def prd_is_sectioned_markdown(document: str) -> bool:
    """True when a stored PRD is the stitched markdown of the canonical parts.

    Used by the architect's early-exit guard: a document assembled from
    ``prd_sections`` is a valid current document even though it is no longer
    the raw LaTeX template body.
    """
    if not document:
        return False
    keys = {p["section_key"] for p in split_markdown_sections(document)}
    return len(keys & CANONICAL_KEYS) >= 7


def prd_has_renderable_operational_sections(document: str) -> bool:
    """Whether the two operational tail sections contain rendered content.

    Older section seeds could leave ``tech_ops`` and ``appendix`` as heading-only
    placeholders.  Such a document still looks "sectioned" to the architect's
    reuse guard, so it was returned forever instead of being rebuilt from the
    official template.  A template rebuild is safe here: it supplies only the
    labelled ``TBD`` fields and does not invent technical facts or risks.
    """
    source = document
    try:
        from app.latex_service import is_markdown_prd, prd_to_markdown
        if source and not is_markdown_prd(source):
            source = prd_to_markdown(source)
    except Exception:
        return False
    parts = {part["section_key"]: part.get("content", "") for part in split_markdown_sections(source)}
    for key in ("tech_ops", "appendix"):
        content = parts.get(key, "")
        # Remove the section heading, page-break marker and whitespace. Anything
        # left (normally the deterministic table) is visible user content.
        body = re.sub(r"^#{2,3}\s+.*?(?:\n|$)", "", content.strip(), count=1)
        body = re.sub(r"(?m)^\\newpage\s*$", "", body).strip()
        if not body:
            return False
    return True


# PRD-SECTION 3.7 — Assemble the full document from the stored parts (4.1 → 3.4). This
#             is the single source of truth used by a manual edit (2.2), a revert (2.3),
#             the whole-document restore (VERSION 2.5) and the export path.
async def assemble_document_markdown(project_id: str, session: AsyncSession) -> str:
    """Stitch the project's stored PRD sections into the full document."""
    sections = await PRDSectionRepository.get_by_project(project_id, session)
    return stitch_markdown(sections)


# PRD-SECTION 3.6 — Seeder: guarantees the nine lockable parts exist. Called on FIRST
#             ACCESS (PRD-SECTION 2.1) and by the whole-document restore (VERSION 2.5).
#             Seeds from the project's current markdown PRD when there is one, otherwise
#             from the official template skeleton; a no-op once parts exist. LaTeX
#             sources are normalized first (pandoc; failure falls back to the template).
async def ensure_sections_seeded(
    project_id: str,
    session: AsyncSession,
    source_markdown: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Guarantee the project has its nine lockable PRD parts.

    Seeds ``prd_sections`` from ``source_markdown`` when provided (typically
    the project's current markdown PRD), otherwise from the official Krungsri
    ``prompts/template.md`` skeleton. No-op when sections already exist.
    """
    # Several first-write requests can arrive together (for example, the new
    # project intake writes Cover, Stakeholders and Business Overview).  The
    # initial "does this project have sections?" read must be serialized on
    # PostgreSQL; otherwise each transaction can see no rows and attempt the
    # same unique (project_id, section_key) insert.  SQLite test databases do
    # not provide advisory locks, so they retain the existing idempotent path.
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"prd-sections:{project_id}"},
        )

    existing = await PRDSectionRepository.get_by_project(project_id, session)
    if existing:
        return existing

    source = (source_markdown or "").strip()
    if source:
        try:
            from app.latex_service import is_markdown_prd, prd_to_markdown
            if not is_markdown_prd(source):
                source = prd_to_markdown(source)
        except Exception as conv_err:  # pandoc missing etc. — fall back to the template
            logger.warning(
                "[PRD SECTIONS] Could not normalize seed source for project %s (%s); "
                "seeding from the official template instead.", project_id, conv_err,
            )
            source = ""

    if not source:
        # Seed from the SAME source the preview renders: the official LaTeX
        # template normalized to markdown (yields exactly the nine parts). If
        # the pandoc toolchain is unavailable, degrade to the markdown
        # skeleton (template.md).
        try:
            from app.latex_service import prd_to_markdown
            from app.prompt_loader import load_prd_latex_template
            source = prd_to_markdown(load_prd_latex_template())
        except Exception as tpl_err:
            logger.warning(
                "[PRD SECTIONS] LaTeX template normalization failed (%s); "
                "seeding from the markdown skeleton instead.", tpl_err,
            )
            from app.prompt_loader import load_prd_template
            source = load_prd_template()

    created: Dict[str, Dict[str, Any]] = {}
    for order, part in enumerate(split_markdown_sections(source), start=1):
        defaults = next(
            (c for c in CANONICAL_PRD_SECTIONS if c["section_key"] == part["section_key"]), None
        )
        row = await PRDSectionRepository.create(project_id, {
            "section_key": part["section_key"],
            "title": part["title"],
            "content": part["content"],
            "section_order": defaults["section_order"] if defaults else order,
            "content_source": defaults["content_source"] if defaults else "ai",
            "ai_generatable": defaults["ai_generatable"] if defaults else True,
            "is_locked": defaults["is_locked"] if defaults else False,
            "initial_changed_by": "system",
            "initial_change_summary": "Seeded from the official Krungsri template.",
        }, session)
        created[row["section_key"]] = row

    # Also materialize canonical parts the source document lacks, so the
    # caller always gets the full nine-part skeleton to edit and lock.
    for defaults in CANONICAL_PRD_SECTIONS:
        if defaults["section_key"] in created:
            continue
        row = await PRDSectionRepository.create(project_id, {
            **defaults,
            "content": "",
            "initial_changed_by": "system",
            "initial_change_summary": "Placeholder for a missing template part.",
        }, session)
        created[row["section_key"]] = row

    logger.info("[PRD SECTIONS] Seeded %d sections for project %s", len(created), project_id)
    return await PRDSectionRepository.get_by_project(project_id, session)


# ARCHITECT 5.1 — Direct-write part merge called from ARCHITECT 5.0 when preview mode
#             is disabled. Ownership contract:
#             missing sections are created; existing AI-generatable UNLOCKED sections are
#             updated; human-owned (ai_generatable=False) and locked sections are NEVER
#             touched; every real change appends a prd_section_versions row (append-only).
#             Returns the stitched markdown of ALL parts (which the node stores as
#             generated_prd), or None when the source cannot be normalized — in which
#             case the caller keeps the previous document untouched (5.2).
async def sync_sections_from_prd(
    project_id: str,
    prd_markdown: str,
    session: AsyncSession,
    *,
    changed_by: str = "automated_agent",
    change_summary: Optional[str] = None,
    selected_section_keys: Optional[Set[str]] = None,
) -> Optional[str]:
    """
    Merge a freshly generated PRD into the project's stored sections.

    Rules (the human/AI ownership contract):
      - a section that does not exist yet is created from the generated part;
      - an existing section is updated ONLY when it is NOT locked and
        ``ai_generatable`` is True — human-owned content and user-satisfied
        (locked) sections are NEVER touched by regeneration;
      - every actual content change appends a ``prd_section_versions`` row
        (append-only, nothing overwritten).

    ``change_summary`` overrides the default per-part summary ("Generated /
    Regenerated by the Architect agent.") so non-regeneration callers — e.g.
    reconciling stale parts with the latest PRD before a manual edit — record an
    honest history line.

    Returns the stitched full markdown of ALL stored sections (so the caller
    can persist it as the current ``generated_prd``), or ``None`` when the
    source could not be normalized (e.g. pandoc unavailable) — in which case
    the caller must keep the previous document untouched.
    """
    source = (prd_markdown or "").strip()
    if not source:
        return None

    try:
        from app.latex_service import prd_to_markdown
        markdown = prd_to_markdown(source)
    except Exception as conv_err:
        logger.warning(
            "[PRD SECTIONS] Could not normalize generated PRD for project %s (%s); "
            "sections left untouched.", project_id, conv_err,
        )
        return None

    parts = split_markdown_sections(markdown)
    if not parts:
        return None

    created_summary = change_summary or "Generated by the Architect agent."
    updated_summary = change_summary or "Regenerated by the Architect agent."

    existing = {
        s["section_key"]: s
        for s in await PRDSectionRepository.get_by_project(project_id, session)
    }

    selected = set(selected_section_keys) if selected_section_keys is not None else None
    for order, part in enumerate(parts, start=1):
        if selected is not None and part["section_key"] not in selected:
            continue
        row = existing.get(part["section_key"])
        if row is None:
            defaults = next(
                (c for c in CANONICAL_PRD_SECTIONS if c["section_key"] == part["section_key"]), None
            )
            created = await PRDSectionRepository.create(project_id, {
                "section_key": part["section_key"],
                "title": part["title"],
                "content": part["content"],
                "section_order": defaults["section_order"] if defaults else order,
                "content_source": defaults["content_source"] if defaults else "ai",
                "ai_generatable": defaults["ai_generatable"] if defaults else True,
                "is_locked": defaults["is_locked"] if defaults else False,
                "initial_changed_by": changed_by,
                "initial_change_summary": created_summary,
            }, session)
            existing[created["section_key"]] = created
        elif (
            not row.get("is_locked")
            # The cover is a hybrid section. Its PMO/Author values are read
            # from the saved document before generation, but Version, Status
            # and Last Update are system-managed and must refresh. Allow the
            # generated cover through even for projects whose intake save
            # previously marked the whole title section human-owned.
            and (row.get("ai_generatable", True) or part["section_key"] == "title")
            and (row.get("content") or "") != part["content"]
        ):
            updated = await PRDSectionRepository.update_content(
                row["id"], project_id, part["content"], session,
                changed_by=changed_by,
                change_summary=updated_summary,
            )
            if updated:
                existing[updated["section_key"]] = updated

    stitched = stitch_markdown(list(existing.values()))
    logger.info(
        "[PRD SECTIONS] Synced %d generated parts into %d stored sections for project %s",
        len(parts) if selected is None else len([p for p in parts if p["section_key"] in selected]),
        len(existing), project_id,
    )
    return stitched

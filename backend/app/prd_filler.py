"""Deterministic filler for the official Krungsri Nimble PRD template.

The previous approach asked the LLM to reproduce the WHOLE LaTeX body. Small
local models routinely corrupt it (drop ``\\\\`` row terminators, unbalance
braces, over-escape) which made PDF/DOCX export fail. This module instead
FILLS the official template's blank fields from the project's (AI-gathered
and AI-validated) structured data: the skeleton - every table, merged-cell
structure and ``\\newpage`` marker - is always the untouched official
template, so the result is PDF-exact and always compiles.

``fill_template_body`` is deterministic and data-driven. Optional narrative
snippets (AI-written prose) can be supplied per-section; anything absent is
left blank instead of displaying a fabricated value or placeholder.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.prompt_loader import load_prd_latex_template_body

# ---------------------------------------------------------------------------
# LaTeX escaping for inserted values
# ---------------------------------------------------------------------------
_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "_": r"\_",
    "$": r"\$",
    "#": r"\#",
    "{": r"\{",
    "}": r"\}",
    "^": r"\^{}",
    "~": r"\~{}",
}


def escape_latex(value: Any) -> str:
    """Escape a plain-text value for safe insertion into a LaTeX cell."""
    s = str(value or "").strip()
    return "".join(_ESCAPE_MAP.get(ch, ch) for ch in s)




def _clean(value: Any) -> str:
    """Squash newlines in a plain-text value so it fills exactly one cell.

    Values are deliberately NOT truncated: the exported tables use wrapping
    paragraph columns and ``longtable`` rows that grow (and break across
    pages) with the content, mirroring the DOCX export behaviour.
    """
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")


_OUTLINE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)+[.):]?|\d+[.):])\s*)+"
)
_FR_AC_PREFIX_RE = re.compile(r"^\s*(?:FR|AC)\s+\d+(?:\.\d+)*\s*:\s*", re.I)


def _plain_list_item(value: Any) -> str:
    """Remove presentation numbering before the template adds its own label."""
    text = str(value or "").strip()
    # Normalize LaTeX-style escapes that legacy generated states persisted as
    # literal product text before applying the one true export escaping pass.
    text = re.sub(r"\\(?=[{}_\.])", "", text)
    text = text.replace(r"\textbackslash{}", "\\")
    previous = None
    while text and text != previous:
        previous = text
        text = _FR_AC_PREFIX_RE.sub("", text)
        text = _OUTLINE_PREFIX_RE.sub("", text)
    return "" if text.upper() == "TBD" else text.strip()


def _numbered_text(value: Any) -> str:
    """Canonicalize a numbered multiline field, returning blank when unknown."""
    source = str(value or "").replace("<br>", "\n")
    items = [_plain_list_item(line) for line in source.splitlines()]
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1 and not re.match(r"^\s*(?:\d|FR\b|AC\b)", source, re.I):
        return items[0]
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _latex_multiline(value: Any, *, numbered: bool = True) -> str:
    """Escape a cell value and turn logical lines into visible line breaks.

    A literal newline inside a LaTeX table cell is only source whitespace. It
    does not produce a new line in the preview, PDF, or DOCX, so all generated
    multi-item fields use ``\\newline`` explicitly.
    """
    text = _numbered_text(value) if numbered else str(value or "")
    return r"\newline ".join(
        escape_latex(line.strip())
        for line in text.replace("<br>", "\n").splitlines()
        if line.strip()
    )


def _as_list(value: Any) -> List[Any]:
    """Normalize scalar-or-list PRD fields without iterating strings by character."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# PENDING DOCUMENT-VERSION LABEL — written into the cover's VERSION field and
# the Version History row when the caller does not know the semver YET (the
# semver of a snapshot can only be computed once the filled document exists,
# because the document's own changes feed compute_section_changes). The caller
# then resolves the semver (version_service.resolve_document_semver) and calls
# stamp_version_label() to swap this marker for the real label BEFORE the
# document is stored or exported. The marker is letters-only, so it survives
# escape_latex, pandoc and LaTeX compilation untouched when unstamped.
PENDING_VERSION_LABEL = "PENDINGSEMVER"


def format_project_status(value: Any) -> str:
    """Convert the stored workflow status into the cover-page label."""
    raw = str(value or "draft").strip().lower()
    labels = {
        "draft": "Draft",
        "in_review_hpo": "In Review (HPO)",
        "in_review_po": "In Review (PO)",
        "approved": "Approved",
        "revised": "Revised",
    }
    return labels.get(raw, raw.replace("_", " ").title() or "Draft")


def version_history_sentence(history_context: Optional[str]) -> str:
    """Return one short, natural sentence for the current PRD version.

    ``history_context`` is a multi-line digest of previous versions. It must
    never be copied into one table cell; version, date, and author already
    have dedicated columns in the document.
    """
    context = str(history_context or "").strip().lower()
    if not context or context.startswith("no previous"):
        return "Initial PRD created."
    return "PRD updated with the latest requirements."


def _one_sentence(value: Any, fallback: str = "PRD updated.") -> str:
    """Collapse a ledger description to one short, readable sentence."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    first = re.match(r"^.*?[.!?](?=\s|$)", text)
    sentence = first.group(0) if first else text
    if len(sentence) > 140:
        sentence = sentence[:137].rsplit(" ", 1)[0].rstrip(".,;:") + "..."
    elif sentence[-1] not in ".!?":
        sentence += "."
    return sentence


def _previous_version_rows(history_context: Optional[str]) -> List[tuple[str, str, str, str]]:
    """Parse the newest-first ledger digest into chronological table rows."""
    rows: List[tuple[str, str, str, str]] = []
    pattern = re.compile(r"^-\s*v([^\s]+)\s+\(([^,]+),\s*([^)]+)\):\s*(.*)$")
    for line in str(history_context or "").splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        version, timestamp, author, description = match.groups()
        display_author = {"automated_agent": "System", "user": "User"}.get(
            author.strip(), author.strip(),
        )
        rows.append((version, timestamp.strip()[:10], display_author, _one_sentence(description)))
    rows.reverse()
    return rows


def stamp_version_label(document: str, label: str) -> str:
    """Replace every :data:`PENDING_VERSION_LABEL` marker in ``document`` with
    ``label`` (LaTeX-escaped), returning the stamped document.

    Pure string replacement — it never fails; a document without the marker
    is returned unchanged. Callers use it to stamp the ledger's semver into
    the cover + Version History section after resolving it.
    """
    return (document or "").replace(PENDING_VERSION_LABEL, escape_latex(label))


# ---------------------------------------------------------------------------
# Section fillers - each replaces the blank/marker inside the SAME skeleton
# ---------------------------------------------------------------------------
def extract_cover_fields(markdown: str) -> Dict[str, str]:
    """Read user-owned cover values from a normalized Markdown PRD.

    Intake data is stored in ``prd_sections`` and stitched into
    ``requirement_states.generated_prd``.  Generation must read these values
    before rebuilding the official LaTeX template; otherwise the historical
    UUID-derived fallback silently replaces the user's PMO number.
    """
    source = str(markdown or "")
    fields: Dict[str, str] = {}
    for key, label in (("pmo_no", "PMO No"), ("pmo_name", "PMO Name"), ("author", "Author")):
        table_match = re.search(
            rf"\|\s*\*\*{re.escape(label)}:\*\*\s*\|\s*([^|\n]*)\|",
            source,
            re.IGNORECASE,
        )
        plain_match = re.search(
            rf"^{re.escape(label)}:\s*(.+?)\s*$",
            source,
            re.IGNORECASE | re.MULTILINE,
        )
        value = (table_match.group(1) if table_match else plain_match.group(1) if plain_match else "").strip()
        if value and value.upper() not in {"PMO_NO", "PMO_NAME", "AUTHOR", "TBD"}:
            fields[key] = value.replace("\\|", "|")
    return fields


STAKEHOLDER_ROLES = (
    "Product Owner", "Technical Product Owner", "Solution Architecture",
    "System Analyst", "Engineering Manager", "Project Manager",
    "Quality Assurance Lead", "Performance QA Lead",
)
BUSINESS_TOPICS = (
    "Introduction & Executive Summary", "Problem Statement",
    "Business Objectives", "Expected Benefit", "Success Metrics",
    "Target Audience & user Personas",
)

_PRD_SECTION_TITLES = (
    "business & strategic overview",
    "product scope & functional requirements",
    "technical & operational considerations",
    "appendix",
)

_TECHNICAL_COMPLETION_GUIDANCE = {
    "non_functional": (
        "Define measurable performance, availability, security, privacy, recovery, "
        "and observability requirements before implementation."
    ),
    "tps_volume": (
        "Confirm peak transactions per second, concurrent users, and monthly volume "
        "before capacity planning."
    ),
    "growth": (
        "Confirm the year-over-year growth forecast, planning horizon, and scaling thresholds."
    ),
    "launch_plan": (
        "Define pilot users, release stages, readiness criteria, monitoring, ownership, "
        "and rollback steps."
    ),
}
_OPEN_ITEMS_GUIDANCE = (
    "Confirm performance targets, integration limits, security controls, data retention, "
    "operational ownership, rollout criteria, and rollback conditions."
)
_ASSUMPTIONS_GUIDANCE = (
    "Confirm assumptions for external services, data availability, compliance ownership, "
    "support coverage, and deployment environments."
)
_RELATED_DOCUMENTS_GUIDANCE = (
    "Reference the source requirements, architecture decisions, API specifications, "
    "compliance policies, and operating procedures used by this PRD."
)
_GLOSSARY_GUIDANCE_TERM = "Project terminology"
_GLOSSARY_GUIDANCE_DEFINITION = (
    "Define domain terms, acronyms, system names, and business rules used in this PRD."
)

_EVIDENCE_GAP_TEXT = {
    *(value.lower() for value in _TECHNICAL_COMPLETION_GUIDANCE.values()),
    _OPEN_ITEMS_GUIDANCE.lower(),
    _ASSUMPTIONS_GUIDANCE.lower(),
    _RELATED_DOCUMENTS_GUIDANCE.lower(),
    _GLOSSARY_GUIDANCE_TERM.lower(),
    _GLOSSARY_GUIDANCE_DEFINITION.lower(),
}


def is_template_only_detail(value: Any, field: str = "") -> bool:
    """Return True when an extracted cell contains template structure, not data.

    Legacy previews can contain the document's section titles collapsed into
    one table cell.  The official template also includes ``Performance &
    Latency`` as an NFR category example.  Neither is a project requirement
    and neither should be restored when a saved document is loaded.
    """
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    # Empty generated list cells round-trip through LaTeX/Markdown as artifacts
    # such as ``1. 2.``. They are not manual user content and must not override
    # newly extracted narrative fields merely because the string is non-empty.
    without_empty_markers = re.sub(r"(?:^|\s)\d+[.)](?=\s|$)", " ", normalized)
    without_empty_markers = re.sub(r"[\s,;:.-]+", "", without_empty_markers)
    if not without_empty_markers:
        return True
    normalized = re.sub(r"^\d+[.)]?\s*", "", normalized)
    if not normalized or normalized == "tbd" or normalized in _EVIDENCE_GAP_TEXT:
        return True
    if sum(title in normalized for title in _PRD_SECTION_TITLES) >= 2:
        return True
    return field == "non_functional" and normalized == "performance & latency"


def strip_template_only_details(document: str) -> str:
    """Blank template examples/artifacts without changing real PRD content."""
    source = str(document or "")
    source = re.sub(
        r"(Non-Functional\\newline Requirements\s*&\s*)"
        r"1\.\s*Performance\s+\\&\s+Latency(\s*\\\\)",
        r"\1\\prdfield{}\2",
        source,
    )

    cleaned_lines = []
    for line in source.splitlines():
        if line.strip().startswith("|"):
            cells = line.strip().strip("|").split("|")
            if len(cells) >= 2:
                row_label = re.sub(
                    r"\s+", " ", cells[0].replace("<br>", " ").replace("**", "")
                ).strip().lower().replace("-", " ")
                field = "non_functional" if row_label == "non functional requirements" else ""
                if is_template_only_detail(cells[1], field):
                    cells[1] = ""
                    line = "|" + "|".join(cells) + "|"
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def extract_intake_fields(markdown: str) -> Dict[str, Any]:
    """Extract every new-project intake value from persisted PRD Markdown."""
    source = str(markdown or "")
    result: Dict[str, Any] = {
        **extract_cover_fields(source),
        "stakeholders": {},
        "business_topics": {},
    }
    current_section = ""
    for raw_line in source.splitlines():
        heading = re.match(r"^#{2,3}\s+(.+?)\s*$", raw_line.strip())
        if heading:
            title = heading.group(1).lower()
            current_section = (
                "stakeholders" if "stakeholder" in title
                else "business" if "business & strategic" in title
                else ""
            )
            continue
        if current_section not in {"stakeholders", "business"}:
            continue
        cells = [cell.strip().replace("\\|", "|").replace("<br>", "\n")
                 for cell in raw_line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = re.sub(r"\*\*", "", cells[0]).strip()
        value = cells[1].strip()
        if not value:
            continue
        choices = STAKEHOLDER_ROLES if current_section == "stakeholders" else BUSINESS_TOPICS
        canonical = next((choice for choice in choices if choice.lower() == label.lower()), None)
        if canonical and not is_template_only_detail(value):
            target = "stakeholders" if current_section == "stakeholders" else "business_topics"
            result[target][canonical] = value
    return result


def extract_manual_export_fields(markdown: str) -> Dict[str, Any]:
    """Recover editable preview values needed by the official export filler.

    The Markdown document is the saved user-owned representation, while the
    PDF/DOCX is rebuilt with the untouched LaTeX template.  This bridge keeps
    manual Scope, Technical and Appendix edits from being replaced by older
    normalized requirement records during that rebuild.
    """
    result: Dict[str, Any] = {
        "scope_in": [], "scope_out": [], "scope_present": False,
        "functional_rows": [], "functional_table_present": False,
        "narrative": {},
    }
    section = ""
    scope_headers = False
    glossary_headers = False
    narrative_labels = {
        "non functional requirements": "non_functional",
        "expected tps customer volume": "tps_volume",
        "growth prediction y0y%": "growth",
        "launch & rollout plan": "launch_plan",
        "open questions & risks": "open_questions",
        "assumptions": "assumptions",
        "related documents": "related_documents",
    }

    def clean_cell(value: str) -> str:
        value = re.sub(r"\*\*", "", value).replace("<br>", "\n")
        return value.strip()

    def label(value: str) -> str:
        normalized = clean_cell(value).replace("&\n", "& ").replace("\n", " ").replace("-", " ")
        return re.sub(r"\s+", " ", normalized).strip().lower()

    def list_items(value: str) -> List[str]:
        items = []
        for part in clean_cell(value).splitlines():
            item = re.sub(r"^\s*\d+[.)]\s*", "", part).strip()
            if item and item.upper() != "TBD":
                items.append(item)
        return items

    def launch_parts(value: str) -> Dict[str, str]:
        """Split the combined rollout cell without carrying its labels forward.

        Older exports re-imported the complete cell as ``launch_plan`` and then
        prefixed it with ``Release plan:`` again on every generation.  Parsing
        each labelled segment back to its canonical field makes regeneration
        idempotent and removes duplicate dependencies.
        """
        source = clean_cell(value)
        labels = list(re.finditer(
            r"(?i)(?:^|\n|\s)(Release plan|Dependencies|Go-to-market)\s*:\s*",
            source,
        ))
        if not labels:
            return {"launch_plan": "\n".join(list_items(source)) or source}

        key_for = {
            "release plan": "launch_plan",
            "dependencies": "dependencies",
            "go-to-market": "go_to_market",
        }
        collected: Dict[str, List[str]] = {}
        for index, match in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(source)
            segment = source[match.end():end].strip()
            items = list_items(segment)
            key = key_for[match.group(1).lower()]
            bucket = collected.setdefault(key, [])
            for item in items:
                if item not in bucket:
                    bucket.append(item)
        return {key: "\n".join(items) for key, items in collected.items() if items}

    glossary: List[tuple[str, str]] = []
    for raw_line in str(markdown or "").splitlines():
        heading = re.match(r"^#{2,4}\s+(.+?)\s*$", raw_line.strip())
        if heading:
            title = heading.group(1).lower()
            if "product scope" in title:
                section = "scope"
            elif "scope definition" in title:
                # Subheading inside Product Scope; keep the parent section.
                section = "scope"
            elif "technical & operational" in title:
                section = "tech"
            elif "appendix" in title:
                section = "appendix"
            elif len(heading.group(0)) - len(heading.group(0).lstrip("#")) <= 3:
                section = ""
            continue
        if not raw_line.strip().startswith("|"):
            continue
        cells = [clean_cell(cell) for cell in raw_line.strip().strip("|").split("|")]
        if not cells or all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
            continue

        if section == "scope" and len(cells) >= 2:
            joined_labels = " ".join(label(cell) for cell in cells)
            if "functional requirement (fr)" in joined_labels and "acceptance criteria" in joined_labels:
                result["functional_table_present"] = True
                continue
            if len(cells) >= 4 and cells[2].strip() and re.match(r"^FR\s+\d", cells[2].strip(), re.I):
                story_text = clean_cell(cells[1])

                def story_value(start: str, end: Optional[str] = None) -> str:
                    boundary = rf"(?=\n+\s*{end}\b|$)" if end else r"(?=$)"
                    match = re.search(
                        rf"(?is)(?:^|\n+)\s*{start}\s+(.+?){boundary}",
                        story_text,
                    )
                    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""

                fr_text = re.sub(r"^FR\s+[\d.]+:\s*", "", clean_cell(cells[2]), flags=re.I)
                code_match = re.match(r"([^:]+):\s*(.*)", fr_text)
                ticket_code = code_match.group(1).strip() if code_match else ""
                title = code_match.group(2).strip() if code_match else fr_text.strip()
                acs = [
                    re.sub(r"^AC\s+[\d.]+:\s*", "", item, flags=re.I).strip()
                    for item in clean_cell(cells[3]).splitlines()
                ]
                acs = [item for item in acs if item and item.upper() != "TBD"]
                functional_row = {
                    "ticket_code": ticket_code,
                    "story_title": title,
                    "acceptance_criteria": acs,
                }
                as_a = story_value(r"As a", r"I want to")
                i_want_to = story_value(r"I want to", r"So that")
                so_that = story_value(r"So that")
                if as_a or i_want_to or so_that:
                    functional_row.update({
                        "as_a": as_a,
                        "i_want_to": i_want_to,
                        "so_that": so_that,
                    })
                result["functional_rows"].append(functional_row)
                result["functional_table_present"] = True
                continue
            if "scope in" in label(cells[0]) and "scope out" in label(cells[1]):
                scope_headers = True
                result["scope_present"] = True
                continue
            if scope_headers:
                result["scope_in"].extend(list_items(cells[0]))
                result["scope_out"].extend(list_items(cells[1]))
                scope_headers = False
                continue

        if section in {"tech", "appendix"} and len(cells) >= 2:
            row_label = label(cells[0])
            key = narrative_labels.get(row_label)
            if key:
                if key == "launch_plan":
                    result["narrative"].update(launch_parts(cells[1]))
                    continue
                value = "\n".join(list_items(cells[1])) or clean_cell(cells[1])
                # Presence of the editable row is authoritative even when the
                # user cleared it. Template examples are also empty values,
                # not project details; use TBD rather than restoring them.
                result["narrative"][key] = (
                    value if not is_template_only_detail(value, key) else "TBD"
                )
                glossary_headers = row_label == "glossary"
                continue
            if row_label == "glossary":
                glossary_headers = True
                continue
            if glossary_headers and len(cells) >= 3:
                term, definition = cells[-2].strip(), cells[-1].strip()
                if label(term) == "term" and label(definition) == "definition":
                    continue
                if (
                    term
                    and definition
                    and term.upper() != "TBD"
                    and definition.upper() != "TBD"
                    and term.lower() not in _EVIDENCE_GAP_TEXT
                    and definition.lower() not in _EVIDENCE_GAP_TEXT
                ):
                    glossary.append((term, definition))

    if glossary:
        result["narrative"]["glossary"] = glossary
    return result


def _fill_cover(body: str, project_id: str, project_name: str, version_label: str,
                author: str, today: str, pmo_no: Optional[str] = None,
                status: str = "Draft") -> str:
    resolved_pmo_no = (pmo_no or "").strip() or project_id[:8].upper()
    body = body.replace(r"\prdfield{PMO\_NO}", escape_latex(resolved_pmo_no), 1)
    body = body.replace(r"\prdfield{PMO\_NAME}", escape_latex(project_name), 1)
    body = body.replace(r"\prdfield{VERSION}", escape_latex(version_label), 1)
    body = body.replace(r"\prdfield{STATUS}", escape_latex(status or "Draft"), 1)
    body = body.replace(r"\prdfield{LAST\_UPDATE}", escape_latex(today), 1)
    body = body.replace(r"\prdfield{AUTHOR}", escape_latex(author or "Product Owner"), 1)
    return body


def _fill_stakeholders(body: str, actors: List[Any], stakeholders: Optional[Dict[str, str]] = None) -> str:
    names = [str(a.get("name", a)) if isinstance(a, dict) else str(a)
             for a in (actors or [])]

    def _match(role: str) -> str:
        for n in names:
            if role.lower() in n.lower() or n.lower() in role.lower():
                return n
        return ""

    for role in STAKEHOLDER_ROLES:
        name = (stakeholders or {}).get(role) or _match(role)
        body = body.replace(f"{role} & \\\\", f"{role} & {escape_latex(name)} \\\\", 1)
    return body


def _fill_version_history(body: str, version_label: str, author: str, today: str,
                          version_summary: str,
                          history_context: Optional[str] = None,
                          version_author: Optional[str] = None) -> str:
    """Write one row for every prior version plus the current version."""
    rows = _previous_version_rows(history_context)
    rows.append((
        version_label,
        today,
        version_author or author or "Product Owner",
        _one_sentence(version_summary, "Initial PRD created."),
    ))
    rendered = "\n  \\hline\n".join(
        "  " + " & ".join(escape_latex(value) for value in row) + " \\\\"
        for row in rows
    )
    placeholder = "  V1.0 & & & \\\\\n  \\hline\n  & & & \\\\\n  \\hline"
    return body.replace(placeholder, rendered + "\n  \\hline", 1)


def _fill_reviews(body: str, actors: List[Any]) -> str:
    names = [str(a.get("name", a)) if isinstance(a, dict) else str(a)
             for a in (actors or [])]
    for role in ("Product Owner", "Head of Product Management"):
        name = next((n for n in names if role.lower() in n.lower()), "")
        body = body.replace(
            f"{role} & & & \\\\",
            f"{role} & {escape_latex(name)} & & \\\\",
            1,
        )
    return body


def _fill_business_overview(body: str, data: Dict[str, Any],
                            narrative: Optional[Dict[str, str]],
                            business_topics: Optional[Dict[str, str]] = None) -> str:
    business_topics = business_topics or {}
    goals = [g.get("description", g) if isinstance(g, dict) else g
             for g in _as_list(data.get("business_goals"))]
    if business_topics.get("Business Objectives"):
        goals = [business_topics["Business Objectives"]]
    elif narrative and narrative.get("business_objectives"):
        goals = [narrative["business_objectives"]]
    narrative = dict(narrative or {})
    if business_topics.get("Expected Benefit"):
        narrative["expected_benefit"] = business_topics["Expected Benefit"]
    if business_topics.get("Success Metrics"):
        narrative["success_metrics"] = business_topics["Success Metrics"]
    actors = data.get("actors") or []

    def _cell_text(value: Any) -> str:
        """Escape user text and preserve each intended line inside one cell."""
        return r"\newline ".join(
            escape_latex(line.strip())
            for line in str(value or "").splitlines()
            if line.strip()
        )

    def _narr(key: str, fallback: str) -> str:
        if narrative and narrative.get(key):
            return r"\prdfield{" + _cell_text(_numbered_text(narrative[key])) + "}"
        return fallback.replace("{TBD}", "{}")

    def _topic(name: str, fallback: str) -> str:
        value = business_topics.get(name)
        cleaned_value = _numbered_text(value)
        return r"\prdfield{" + _cell_text(cleaned_value) + "}" if cleaned_value else fallback

    epic = _topic("Introduction & Executive Summary", _narr("exec_summary", r"\prdfield{}"))

    problem_source = (
        [business_topics["Problem Statement"]]
        if business_topics.get("Problem Statement")
        else [narrative["problem_statement"]]
        if narrative and narrative.get("problem_statement")
        else _as_list(data.get("problem_statement"))
    )
    def _num(items: List[str]) -> str:
        # No caps: the longtable rows wrap and grow with the content.
        expanded = []
        for item in items:
            expanded.extend(str(item or "").replace("<br>", "\n").splitlines())
        cleaned = [escape_latex(_plain_list_item(item)) for item in expanded]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return r"\prdfield{}"
        parts = []
        for idx, item in enumerate(cleaned):
            parts.append(
                f"{idx + 1}. {item}"
                + (r"\newline " if idx < len(cleaned) - 1 else "")
            )
        return r"\prdfield{" + "".join(parts) + "}"

    problem_cell = _num(problem_source)

    target = [
        (a.get("name", a) if isinstance(a, dict) else a) for a in (actors or [])
    ]
    target = [n for n in target if str(n or "").strip()]
    target_text = _numbered_text(
        business_topics.get("Target Audience & user Personas")
        or ((narrative or {}).get("target_audience"))
        or ", ".join(str(n) for n in target)
    )
    target_cell = r"\prdfield{" + _cell_text(target_text) + "}"

    rows = [
        (
            r"^  Introduction",
            f"  Introduction \\&\\newline Executive Summary & {epic} \\\\",
        ),
        (
            r"^  Problem Statement",
            f"  Problem Statement & {problem_cell} \\\\",
        ),
        (
            r"^  Business Objectives",
            f"  Business Objectives & {_num(goals)} \\\\",
        ),
        (
            r"^  Expected Benefit",
            f"  Expected Benefit & {_narr('expected_benefit', r'\prdfield{TBD}')} \\\\",
        ),
        (
            r"^  Success Metrics",
            f"  Success Metrics & {_narr('success_metrics', r'\prdfield{TBD}')} \\\\",
        ),
        (
            r"^  Target Audience",
            f"  Target Audience \\&\\newline user Personas & {target_cell} \\\\",
        ),
    ]
    return _rewrite_rows(body, rows)


def _rewrite_rows(body: str, rows: List[tuple[str, str]]) -> str:
    """Replace tabular rows whose leading label matches a regex.

    The Krungsri template rows are one physical line each; rewriting by line
    keeps the exact template row structure while making the fill resilient to
    incidental whitespace/escaping drift in the label text.
    """
    lines = body.splitlines()
    for prefix, new_line in rows:
        done = False
        for idx, ln in enumerate(lines):
            if re.search(prefix, ln):
                lines[idx] = new_line
                done = True
                break
        if not done:
            raise ValueError(f"Template row not found for fill pattern {prefix!r}")
    return "\n".join(lines)



_SYSTEM_OBLIGATION_PREFIX_RE = re.compile(r"^(?:the\s+)?system\s+(?:must|shall)\s+", re.I)


def _functional_requirement_text(story: Dict[str, Any], code: str, title: str) -> str:
    """Return one canonical FR sentence without changing stored story wording.

    New requirement records supply ``_functional_requirement``. Older records
    remain exportable by deriving the FR from the story action at render time.
    The canonical phrase belongs only to the FR cell; user-story and AC cells
    are rendered independently and are never rewritten here.
    """
    source = (
        story.get("_functional_requirement")
        or story.get("functional_requirement")
        or story.get("i_want_to")
    )
    if not source:
        source = f"{code}: {title}"
    cleaned = _plain_list_item(source)
    cleaned = _SYSTEM_OBLIGATION_PREFIX_RE.sub("", cleaned).strip()
    return f"The system must {cleaned}" if cleaned else ""


def _fr_rows_for(stories: List[Dict[str, Any]]) -> str:
    """Build user-story/FR/AC rows that grow with the project data.

    Every persisted user story becomes one ``longtable`` row. Its dedicated
    user-story cell contains only the stored persona, desired action, and
    outcome. ``\\newline`` creates readable spacing in PDF/DOCX and is also
    normalized cleanly by the Markdown preview converter.
    """
    if not stories:
        return r"""    & & & \\"""
    rows = []
    for idx, story in enumerate(stories, start=1):
        code = story.get("ticket_code") or f"US-{idx:03d}"
        title = story.get("story_title") or story.get("i_want_to") or f"Story {idx}"
        story_lines = []
        for prefix, value in (("As a ", story.get("as_a")),
                              ("I want to ", story.get("i_want_to")),
                              ("So that ", story.get("so_that"))):
            cleaned_value = _plain_list_item(value)
            if cleaned_value:
                story_lines.append(prefix + escape_latex(cleaned_value))
        story_block = r"\newline\newline ".join(story_lines)
        functional_requirement = escape_latex(_functional_requirement_text(story, code, title))
        fr_block = f"FR {idx}.1: {functional_requirement}" if functional_requirement else ""
        acs = [escape_latex(_plain_list_item(a)) for a in (story.get("acceptance_criteria") or [])]
        acs = [ac for ac in acs if ac]
        # ``\\newline`` is an in-cell break. Bare ``\\\\`` is ambiguous to
        # the Markdown/PDF conversion pipeline and previously collapsed
        # separate criteria into colon-separated prose.
        ac_block = r"\newline ".join(
            f"AC {idx}.{j}: {ac}" for j, ac in enumerate(acs, start=1)
        )
        rows.append(f"    & {story_block} & \\rule{{0pt}}{{2.1cm}}{fr_block} & {ac_block} \\\\")
    # Each row already ends with a complete ``\\`` terminator; rows are
    # separated by a plain newline.  Joining with ``'\<newline>'`` used to
    # emit ``\\\<newline>`` between rows - the stray odd backslash leaked
    # into the next row's first cell in the DOCX export.
    return "\n".join(rows)



def _fill_product_scope(
    body: str,
    data: Dict[str, Any],
    narrative: Optional[Dict[str, Any]] = None,
) -> str:
    epic = data.get("epic_name") or (
        data.get("requirements") or [{}]
    )[0].get("title", "") or ""
    # The official LaTeX template stores ``Epic Name:`` inside the content
    # argument of a multicolumn cell; it is not a standalone ``{Epic Name:}``
    # placeholder. Replace the label itself so the value remains inside that
    # merged cell in the preview, PDF, and DOCX renderers.
    body = body.replace(
        "Epic Name:",
        "Epic Name: " + escape_latex(epic),
        1,
    )
    # Keep the complete label visible in the Markdown preview. Explicit row
    # breaks inside this shortstack were interpreted as table boundaries.
    body = body.replace(
        "\\shortstack[l]{User Story\\\\ Mapping \\&\\\\ Functional\\\\ Requirements}",
        "\\parbox{2.8cm}{User Story Mapping \\& Functional Requirements}",
        1,
    )

    stories: List[Dict[str, Any]] = []
    for req in (data.get("requirements") or []):
        if isinstance(req, dict):
            requirement_fr = str(req.get("functional_requirement") or "").strip()
            for story in (req.get("user_stories") or []):
                if not isinstance(story, dict):
                    continue
                document_story = dict(story)
                if requirement_fr:
                    document_story["_functional_requirement"] = requirement_fr
                stories.append(document_story)
    stories.extend(data.get("user_stories") or [])

    # The template's label column is a \multirow spanning the epic row, the
    # column-header row and the FR rows; grow the span with the story count
    # so the merged label covers every generated row.  (\multirow{3}{*} is
    # unique in the template - the Scope Definition label uses \multirow{2}.)
    span = max(1, len(stories)) + 2
    body = re.sub(
        r"\\multirow(?:\[t\])?\{3\}\{\*\}",
        lambda _m: "\\multirow{" + str(span) + "}{*}",
        body,
        count=1,
    )

    _ANCHOR_FR_ROW = (
        r"""    & & \rule{0pt}{2.1cm}\normalsize\sffamily\shortstack{FR 1.1: The system\\"""
        + "\n"
        + r"""must\ldots} & \normalsize\sffamily\shortstack{AC 1.1:\\"""
        + "\n"
        + r"""AC 1.2:} \\"""
    )
    rows = _fr_rows_for(stories)
    if _ANCHOR_FR_ROW in body:
        body = body.replace(_ANCHOR_FR_ROW, rows, 1)

    user_flow = _numbered_text(data.get("user_flow") or (narrative or {}).get("user_flow"))
    if user_flow:
        flow_anchor = r"""  \shortstack[l]{User Flow \&\\
Wireframes}
    & \multicolumn{3}{L{12.55cm}|}{\rule{0pt}{1.15cm}} \\"""
        flow_filled = (
            r"""  \shortstack[l]{User Flow \&\\
Wireframes}
    & \multicolumn{3}{L{12.55cm}|}{\rule{0pt}{1.15cm}\prdfield{"""
            + _latex_multiline(user_flow, numbered=False)
            + r"""}} \\"""
        )
        body = body.replace(flow_anchor, flow_filled, 1)
    body = body.replace(
        "\\shortstack[l]{User Flow \\&\\\\\nWireframes}",
        "User Flow \\& Wireframes",
        1,
    )

    def _items(value: Any) -> List[str]:
        if isinstance(value, str):
            return [
                _plain_list_item(line)
                for line in value.replace("<br>", "\n").splitlines()
            ]
        if isinstance(value, (list, tuple)):
            return [_plain_list_item(item) for item in value]
        return []

    scope_in_source = (narrative or {}).get("scope_in") or data.get("scope_in")
    scope_out_source = (narrative or {}).get("scope_out") or data.get("scope_out")
    scope_in = [escape_latex(item) for item in _items(scope_in_source)]
    scope_in = [item for item in scope_in if item]
    scope_out = [escape_latex(item) for item in _items(scope_out_source)]
    scope_out = [item for item in scope_out if item]

    BS = chr(92)  # backslash - plain concatenation avoids f-string brace bugs
    NL = "\n"

    def _nums(items: List[str], *, last_gap: bool) -> str:
        """Numbered lines mirroring the template's Scope cell:
        entries separated by '\\\\'; the LAST entry carries '[0.15cm]' only
        when ``last_gap`` and never a trailing '\\\\' (a row break directly
        before '}}}' makes \\shortstack die with 'Misplaced \\cr')."""
        if not items:
            return ""
        lines = []
        n = len(items)
        for i, item in enumerate(items):
            if i == n - 1:
                if last_gap:
                    lines.append(f"{i + 1}. {item}{BS}{BS}[0.15cm]")
                else:
                    lines.append(f"{i + 1}. {item}")
            else:
                lines.append(f"{i + 1}. {item}{BS}{BS}")
        return NL.join(lines)

    in_lines = _nums(scope_in, last_gap=True)
    out_lines = _nums(scope_out, last_gap=False)

    # Single-line cell content: joined with spaces (not newlines) so that,
    # after the \parbox unwrap in the preview converter, every in-cell "\\"
    # stays mid-line and latex_service._keep_in_cell_rowbreaks classifies it
    # as an in-cell break ("<br>") instead of a row terminator.
    inner = (
        BS + "rule{0pt}{4.2cm}" + BS + "prdlbl{Scope In}" + BS + BS + " "
        + in_lines + " "
        + BS + "prdlbl{Scope out}" + BS + BS + " "
        + out_lines
        + "}}"
    )
    scope_block = (
        BS + "multicolumn{3}{L{12.55cm}|}{" + BS + "parbox{12.15cm}{%" + NL
        + "        " + inner + " " + BS + BS
    )

    _SCOPE_ROW_RE = re.compile(
        r"\\multicolumn\{3\}\{L\{12\.55cm\}\|\}\{\\parbox\{12\.15cm\}\{%\s*\\rule\{0pt\}\{4\.2cm\}\\shortstack(?:\[l\])?\{.*?\}\}\} \\\\",
        re.S,
    )
    if _SCOPE_ROW_RE.search(body):
        body = _SCOPE_ROW_RE.sub(lambda _m: scope_block, body, count=1)
    return body


def _fill_tech_appendix(body: str, narrative: Optional[Dict[str, str]]) -> str:
    def _cell(key: str, _legacy_fallback: str = "") -> str:
        if narrative and narrative.get(key):
            return r"\prdfield{" + _latex_multiline(narrative[key]) + "}"
        # Missing evidence must stay visibly empty. Completion guidance used to
        # be written into the document itself, where it looked like extracted
        # project data and was subsequently eligible for re-import.
        return r"\prdfield{}"

    def _launch_cell() -> str:
        parts = []
        for key, label in (
            ("launch_plan", "Release plan"),
            ("dependencies", "Dependencies"),
            ("go_to_market", "Go-to-market"),
        ):
            value = _numbered_text((narrative or {}).get(key))
            if value:
                parts.append(
                    escape_latex(f"{label}:") + " " + _latex_multiline(value, numbered=False)
                )
        if not parts:
            return r"\prdfield{}"
        return r"\prdfield{" + r"\newline ".join(parts) + "}"

    def _appendix_cell(key: str, *, minimum_height: bool = False) -> str:
        """Number real Appendix entries without numbering empty cells."""
        source = str((narrative or {}).get(key) or "").replace("<br>", "\n")
        items = [_plain_list_item(item) for item in source.splitlines()]
        items = [item for item in items if item]
        height = r"\rule{0pt}{1cm}" if minimum_height else ""
        if not items:
            return height + r"\prdfield{}"
        numbered = r"\newline ".join(
            f"{index}. {escape_latex(item)}"
            for index, item in enumerate(items, start=1)
        )
        return height + r"\prdfield{" + numbered + "}"

    rows = [
        (
            r"^  Non-Functional",
            f"  Non-Functional\\newline Requirements & {_cell('non_functional', r'\prdfield{TBD}')} \\\\",
        ),
        (
            r"^  Expected\\",
            f"  Expected\\newline TPS/Customer\\newline volume & {_cell('tps_volume', r'\prdfield{TBD}')} \\\\",
        ),
        (
            r"^  Growth\\",
            f"  Growth\\newline prediction\\newline Y0Y\\% & {_cell('growth', r'\prdfield{TBD}')} \\\\",
        ),
        (
            r"^  Launch",
            f"  Launch \\&\\newline Rollout Plan & {_launch_cell()} \\\\",
        ),
        (
            r"^  Open",
            "  Open\\newline Questions \\&\\newline Risks & "
            f"\\multicolumn{{2}}{{L{{12.56cm}}|}}{{{_appendix_cell('open_questions')}}} \\\\",
        ),
        (
            r"^  Assumptions",
            "  Assumptions & "
            f"\\multicolumn{{2}}{{L{{12.56cm}}|}}{{{_appendix_cell('assumptions')}}} \\\\",
        ),
        (
            r"^  Related\\",
            "  Related\\newline Documents & "
            f"\\multicolumn{{2}}{{L{{12.56cm}}|}}{{{_appendix_cell('related_documents', minimum_height=True)}}} \\\\",
        ),
    ]
    body = _rewrite_rows(body, rows)
    return _fill_glossary_rows(body, narrative)


#: The appendix template carries a nested Term/Definition sub-grid (two TBD
#: placeholder rows under the Glossary header). The regex matches both
#: placeholder rows so they can be replaced by real glossary entries.
_GLOSSARY_GRID_RE = re.compile(
    r"(?:\n\s*\\cline\{2-3\}\n\s*&\s*\\prdfield\{TBD\}\s*&\s*\\prdfield\{TBD\}\s*\\\\)+",
    re.S,
)


def _normalize_glossary_entries(value: Any) -> List[tuple[str, str]]:
    """Accept model/manual glossary shapes without letting bad data break PRD generation."""
    if not value:
        return []
    if isinstance(value, dict):
        candidates = list(value.items())
    elif isinstance(value, str):
        candidates = [line for line in value.splitlines() if line.strip()]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return []

    entries: List[tuple[str, str]] = []
    for candidate in candidates:
        term = definition = ""
        if isinstance(candidate, dict):
            term = str(candidate.get("term") or candidate.get("name") or "").strip()
            definition = str(candidate.get("definition") or candidate.get("description") or "").strip()
        elif isinstance(candidate, (list, tuple)) and len(candidate) >= 2:
            term, definition = str(candidate[0]).strip(), str(candidate[1]).strip()
        elif isinstance(candidate, str):
            text = re.sub(r"^\s*\d+[.)]\s*", "", candidate).strip()
            match = re.match(r"^([^:–—]+?)\s*(?::|\s[-–—]\s)\s*(.+)$", text)
            if match:
                term, definition = match.group(1).strip(), match.group(2).strip()
        if term and definition and term.upper() != "TBD" and definition.upper() != "TBD":
            entries.append((term, definition))
    return entries


def _fill_glossary_rows(body: str, narrative: Optional[Dict[str, Any]]) -> str:
    entries = _normalize_glossary_entries((narrative or {}).get("glossary"))
    if not entries:
        blank = (
            "\n  \\cline{2-3}\n"
            "  & \\prdfield{} & \\prdfield{} \\\\"
        )
        return _GLOSSARY_GRID_RE.sub(lambda _match: blank, body, count=1)
    rows = []
    for term, definition in entries:
        rows.append(
            f"  \\cline{{2-3}}\n"
            f"  & {escape_latex(_clean(term))} & {escape_latex(_clean(definition))} \\\\"
        )
    block = "\n".join(rows)
    updated, count = _GLOSSARY_GRID_RE.subn(lambda _m: block, body, count=1)
    if count:
        return updated
    # No placeholder grid found: inject the block right after the glossary
    # header row so the terms still render as a nested Term/Definition table.
    glossary_line = re.search(r"^  Glossary[^\n]*\\\\$", body, re.M)
    if glossary_line:
        idx = glossary_line.end()
        return body[:idx] + "\n" + block + body[idx:]
    return body



# ARCHITECT 3.1 — Deterministic template filler (the ARCHITECT 3.3 write path, and the
#             same helper used by the PROJECT PRD-export endpoint): fills the official
#             LaTeX template BODY's blanks from the project dataset — cover,
#             stakeholders, version history, reviews, business overview, scope tables
#             (in/out), technical appendix and glossary. No LLM text enters the
#             structure, so the result always compiles and stays PDF-exact.
def fill_template_body(
    *,
    project_id: str = "",
    project_name: str = "",
    version: int = 1,
    author: str = "Product Owner",
    status: str = "Draft",
    pmo_no: Optional[str] = None,
    stakeholders: Optional[Dict[str, str]] = None,
    business_topics: Optional[Dict[str, str]] = None,
    today: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    narrative: Optional[Dict[str, str]] = None,
    version_summary: str = "",
    version_history: Optional[str] = None,
    version_label: Optional[str] = None,
    version_author: Optional[str] = None,
) -> str:
    """Return the official Krungsri template BODY with its blanks filled from
    the project dataset. ``data`` maps: ``business_goals``, ``actors``,
    ``requirements`` (with nested ``user_stories`` + ``acceptance_criteria``)
    or flat ``user_stories`` / ``acceptance_criteria``, ``epic_name``,
    ``scope_in``, ``scope_out``, ``problem_statement``.

    ``version_label`` is the display version stamped into the cover's VERSION
    field and the Version History row. When omitted it falls back to the
    legacy ``V{version}.0`` derived from the integer version; callers that
    must show the ledger's semver pass :data:`PENDING_VERSION_LABEL` here and
    stamp the resolved semver with :func:`stamp_version_label` afterwards.
    """
    data = data or {}
    today = today or datetime.now(ZoneInfo("Asia/Bangkok")).date().isoformat()
    version_label = (version_label or "").strip() or f"V{version}.0"

    body = load_prd_latex_template_body()
    body = _fill_cover(body, project_id, project_name, version_label, author, today, pmo_no, status)
    body = _fill_stakeholders(body, data.get("actors") or [], stakeholders)
    body = _fill_version_history(
        body, version_label, author, today, version_summary, version_history,
        version_author,
    )
    body = _fill_reviews(body, data.get("actors") or [])
    body = _fill_business_overview(body, data, narrative, business_topics)
    body = _fill_product_scope(body, data, narrative)
    body = _fill_tech_appendix(body, narrative)
    return body

"""
Canonical ``REQ-###`` requirement-code helpers — the SINGLE source of truth for
requirement numbering.

Nothing in the codebase should hardcode a literal code such as ``"REQ-001"``:

* A hardcoded code on a fresh board is what let a brand-new project start at
  ``REQ-002`` (a seeded/default requirement claimed ``REQ-001`` before the first
  requirement the user actually gathered) and it silently pinned numbering when
  the sequence legitimately needed to advance.
* The prefix, the number a fresh board starts at, the sentinel used for
  not-yet-numbered rows and the ``used codes -> next code`` rule all live here so
  every caller (agents, routes, document import, repositories) derives the same
  answer.
"""
# GATHERING 7.x — Requirement-code helpers used across this flow:
#   7.1 format_requirement_code — canonical REQ-NNN rendering
#   7.2 normalize_requirement_code — "req_1"/"REQ-1" → REQ-001 ("" when invalid); used
#       by GATHERING 3.6/3.7 and by every repository that stores requirement codes
#   7.3 default_requirement_code — deterministic code for the requirement at index N
#       (index 0 == the first code of the project); used by GATHERING 1.5/3.5
#   7.4 next_requirement_code — code after the highest used one; used by GATHERING
#       3.6/3.7 and by the document-extraction draft builder
import re
from typing import Any, Iterable, List

# ``REQ-001``: prefix + zero-padded 3-digit number.
REQUIREMENT_CODE_PREFIX = "REQ"
# Number of the FIRST requirement of any project — a fresh board always starts
# its sequence here instead of at an arbitrary hardcoded code.
FIRST_REQUIREMENT_NUMBER = 1

# A requirement code (``REQ-1`` / ``req_001`` / ``REQ-001``) — the canonical
# form is always rendered as ``REQ-NNN``.
_REQUIREMENT_CODE_RE = re.compile(r"^(?:REQ[-_ ]?)(\d+)$", re.IGNORECASE)


# GATHERING 7.1 — Canonical rendering (the only place the REQ prefix is formatted).
def format_requirement_code(number: int) -> str:
    """Render a requirement number as its canonical ``REQ-NNN`` code."""
    return f"{REQUIREMENT_CODE_PREFIX}-{number:03d}"


# The code every fresh board starts at (``REQ-001``).
FIRST_REQUIREMENT_CODE = format_requirement_code(FIRST_REQUIREMENT_NUMBER)
# Sentinel code for a requirement row that has no sequence number yet (legacy /
# anonymous rows created to satisfy a user-story or audit foreign key).
UNASSIGNED_REQUIREMENT_CODE = format_requirement_code(FIRST_REQUIREMENT_NUMBER - 1)


# GATHERING 7.2 — Lenient inbound normalization: any LLM/legacy spelling collapses to
#             the canonical form (or "" when it is not a requirement code at all).
def normalize_requirement_code(code: Any) -> str:
    """Canonicalize an LLM/DB requirement code to ``REQ-NNN`` (``''`` if invalid)."""
    match = _REQUIREMENT_CODE_RE.match(str(code or "").strip())
    return format_requirement_code(int(match.group(1))) if match else ""


def extract_requirement_codes(text: Any) -> List[str]:
    """Extract canonical ``REQ-###`` identifiers explicitly written in text."""
    found: List[str] = []
    for match in re.finditer(
        r"(?<![A-Z0-9])#?REQ[\s_-]*(\d{1,6})(?!\d)",
        str(text or ""),
        re.IGNORECASE,
    ):
        code = format_requirement_code(int(match.group(1)))
        if code not in found:
            found.append(code)
    return found


# GATHERING 7.3 — Index-based default: index 0 IS the first code of the project, which
#             is why no caller hardcodes "REQ-001" (GATHERING 1.5/3.5 rely on this).
def default_requirement_code(index: int = 0) -> str:
    """Deterministic code for the requirement at 0-based ``index``.

    Index 0 is always the first code of the project (``REQ-001``), so callers
    never hardcode the first code themselves.
    """
    return format_requirement_code(FIRST_REQUIREMENT_NUMBER + index)


# GATHERING 7.4 — Sequence advance: highest used code + 1 (an empty set yields the
#             first code), used when the LLM recycled a code (GATHERING 3.6) or a story
#             needs a parent group that does not exist yet (GATHERING 4.2).
def next_requirement_code(used_codes: Iterable[Any]) -> str:
    """Return the code immediately after the highest already-used code.

    An empty/None sequence (a fresh board) yields the FIRST requirement code, so
    a brand-new project always starts at ``REQ-001``.
    """
    max_num = FIRST_REQUIREMENT_NUMBER - 1
    for code in used_codes or []:
        match = _REQUIREMENT_CODE_RE.match(str(code or "").strip())
        if match:
            max_num = max(max_num, int(match.group(1)))
    return format_requirement_code(max_num + 1)

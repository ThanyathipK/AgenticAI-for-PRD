"""
Unit tests for :mod:`app.requirement_codes` — the single source of truth for
``REQ-###`` requirement numbering.

Covers the user-visible contract that a brand-new project's FIRST requirement is
``REQ-001`` (the reported bug was a first requirement numbered ``REQ-002``), and
that no module needs to hardcode a literal requirement code.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_requirement_codes.py -v
"""
from app.requirement_codes import (
    FIRST_REQUIREMENT_CODE,
    FIRST_REQUIREMENT_NUMBER,
    REQUIREMENT_CODE_PREFIX,
    UNASSIGNED_REQUIREMENT_CODE,
    default_requirement_code,
    extract_requirement_codes,
    format_requirement_code,
    next_requirement_code,
    normalize_requirement_code,
)


def test_extract_requirement_codes_normalizes_common_chat_spellings():
    assert extract_requirement_codes("Change req1 and #REQ_002, not REQ 1 twice") == [
        "REQ-001",
        "REQ-002",
    ]


def test_first_requirement_code_is_derived_not_hardcoded():
    """The first code of every board comes from the sequence, not a literal."""
    assert FIRST_REQUIREMENT_NUMBER == 1
    assert FIRST_REQUIREMENT_CODE == format_requirement_code(FIRST_REQUIREMENT_NUMBER)
    assert f"{REQUIREMENT_CODE_PREFIX}-001" == FIRST_REQUIREMENT_CODE


def test_default_requirement_code_indexes_from_one():
    assert default_requirement_code(0) == "REQ-001"
    assert default_requirement_code(1) == "REQ-002"
    assert default_requirement_code(12) == "REQ-013"


def test_next_requirement_code_starts_at_first_code_on_a_fresh_board():
    """An empty board (no used codes) starts the sequence at REQ-001."""
    assert next_requirement_code(set()) == "REQ-001"
    assert next_requirement_code([]) == "REQ-001"
    assert next_requirement_code(None) == "REQ-001"


def test_next_requirement_code_continues_after_the_highest_used_code():
    assert next_requirement_code({"REQ-001", "REQ-004"}) == "REQ-005"
    assert next_requirement_code(["REQ-002"]) == "REQ-003"
    # Non-requirement keys (e.g. skipped/legacy groups) never reset the counter.
    assert next_requirement_code({"junk", "REQ-007"}) == "REQ-008"


def test_normalize_requirement_code_canonicalizes_and_rejects_junk():
    assert normalize_requirement_code("REQ-001") == "REQ-001"
    assert normalize_requirement_code("req-2") == "REQ-002"
    assert normalize_requirement_code("req_012") == "REQ-012"
    assert normalize_requirement_code("  REQ 7 ") == "REQ-007"
    assert normalize_requirement_code("junk") == ""
    assert normalize_requirement_code(None) == ""


def test_unassigned_sentinel_is_below_the_first_requirement():
    """Legacy/anonymous rows use the sentinel, never a real sequence number."""
    assert UNASSIGNED_REQUIREMENT_CODE == "REQ-000"
    assert UNASSIGNED_REQUIREMENT_CODE != FIRST_REQUIREMENT_CODE

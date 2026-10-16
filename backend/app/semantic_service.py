"""Semantic change detection + the workflow/intent/matcher classification agents.

The semantic-change feature compares the Current Project Context (persisted user
stories) with a new user message and classifies every requested change
(``NEW_REQUIREMENT``, ``MODIFY_REQUIREMENT``, ...) into a writer action
(``INSERT`` / ``UPDATE`` / ``ARCHIVE`` / ``NO_CHANGE``).

Because that classification is produced by a local LLM, the raw answer is
**never** trusted verbatim: :func:`detect_semantic_changes` pipes it through the
deterministic guardrails in :func:`normalize_semantic_changes`, which

- canonicalises the classification + action vocabulary (aliases such as ``ADD``
  / ``MODIFY`` / ``REMOVE`` and the prompt-only ``MERGE`` action are translated
  onto the four actions the persistence layer actually understands),
- clamps ``confidence`` into ``[0.0, 1.0]``,
- resolves every ``target_requirement_id`` against the real backlog (exact
  ticket code -> normalized title -> fuzzy title) and refuses to act on a
  hallucinated target,
- de-duplicates recommendations and resolves conflicting actions over one target
  with a deterministic priority.

An ambiguous or malformed model answer therefore degrades into a low-confidence
recommendation (which the Gatherer turns into a user-visible clarification
question) instead of silently mutating — or silently skipping — the wrong
requirement.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field, field_validator

from app.llm_factory import llm
from app.schemas import RequirementIntentDetectionResult, WorkflowRoutingResult, RequirementMatcherResult
from app.prompt_loader import load_prompt
from app.llm_utils import invoke_llm_structured
from app.merge_service import (
    format_story_context_lines,
    normalize_ticket_code,
    normalize_title,
    titles_match,
)

# ==========================================
# SEMANTIC CHANGE VOCABULARY (single source of truth)
# ==========================================
# What the model is asked to reason with (see prompts/semantic.md).
SEMANTIC_CHANGE_TYPES: Tuple[str, ...] = (
    "NEW_REQUIREMENT",
    "MODIFY_REQUIREMENT",
    "REMOVE_REQUIREMENT",
    "RENAME_REQUIREMENT",
    "NO_MEANINGFUL_CHANGE",
    "EXPAND_REQUIREMENT",
    "SPLIT_REQUIREMENT",
    "MERGE_REQUIREMENTS",
)

# What the WRITERS understand. ``merge_service.merge_user_stories_with_report``
# and ``RequirementStateRepository`` only act on these four actions; any other
# recommendation is silently ignored by both, which used to turn a detected
# change into a no-op.
SEMANTIC_ACTIONS: Tuple[str, ...] = ("INSERT", "UPDATE", "ARCHIVE", "NO_CHANGE")

# Canonical action per classification — must stay in sync with prompts/semantic.md.
DEFAULT_ACTION_BY_CHANGE_TYPE: Dict[str, str] = {
    "NEW_REQUIREMENT": "INSERT",
    "MODIFY_REQUIREMENT": "UPDATE",
    "REMOVE_REQUIREMENT": "ARCHIVE",
    "RENAME_REQUIREMENT": "UPDATE",
    "NO_MEANINGFUL_CHANGE": "NO_CHANGE",
    "EXPAND_REQUIREMENT": "UPDATE",
    "SPLIT_REQUIREMENT": "UPDATE",
    # A consolidation still has to be rewritten onto its surviving story, so the
    # writer action is UPDATE ("MERGE" is not a supported writer action).
    "MERGE_REQUIREMENTS": "UPDATE",
}

# Classifications that are meaningless without a concrete target story.
TARGET_REQUIRED_CHANGE_TYPES: Tuple[str, ...] = (
    "MODIFY_REQUIREMENT",
    "REMOVE_REQUIREMENT",
    "RENAME_REQUIREMENT",
    "EXPAND_REQUIREMENT",
    "SPLIT_REQUIREMENT",
    "MERGE_REQUIREMENTS",
)

# Confidence ceilings that force the Gatherer's clarification branch.
SEMANTIC_LOW_CONFIDENCE_THRESHOLD = 0.70   # gatherer halts below this
UNRESOLVED_TARGET_CONFIDENCE = 0.50        # target could not be resolved
CONFLICTING_ACTIONS_CONFIDENCE = 0.60      # entries disagree over one target
DEFAULT_NO_CHANGE_CONFIDENCE = 0.90        # explicit "nothing changed" signal

# Placeholder ticket code that must never be treated as a resolvable target.
_PLACEHOLDER_TICKET_CODE = "US-000"

# Values that mean "no target" rather than a ticket code.
_NULL_TARGET_TOKENS = frozenset(
    {"", "null", "none", "n/a", "na", "nil", "unknown", "unset", "-", "--", "tbd"}
)

# Deterministic precedence when two recommendations fight over one target: an
# explicit removal outranks a rewrite, which outranks a fresh insert.
_ACTION_PRIORITY: Dict[str, int] = {
    "ARCHIVE": 3,
    "UPDATE": 2,
    "INSERT": 1,
    "NO_CHANGE": 0,
}

# Tolerated synonyms the model may emit instead of the canonical vocabulary.
_CHANGE_TYPE_ALIASES: Dict[str, str] = {
    "NEW": "NEW_REQUIREMENT",
    "ADD": "NEW_REQUIREMENT",
    "ADD_REQUIREMENT": "NEW_REQUIREMENT",
    "CREATE_REQUIREMENT": "NEW_REQUIREMENT",
    "MODIFY": "MODIFY_REQUIREMENT",
    "UPDATE": "MODIFY_REQUIREMENT",
    "UPDATE_REQUIREMENT": "MODIFY_REQUIREMENT",
    "CHANGE_REQUIREMENT": "MODIFY_REQUIREMENT",
    "REMOVE": "REMOVE_REQUIREMENT",
    "DELETE": "REMOVE_REQUIREMENT",
    "DELETE_REQUIREMENT": "REMOVE_REQUIREMENT",
    "DROP_REQUIREMENT": "REMOVE_REQUIREMENT",
    "ARCHIVE_REQUIREMENT": "REMOVE_REQUIREMENT",
    "RENAME": "RENAME_REQUIREMENT",
    "REWORD_REQUIREMENT": "RENAME_REQUIREMENT",
    "NO_CHANGE": "NO_MEANINGFUL_CHANGE",
    "NO CHANGE": "NO_MEANINGFUL_CHANGE",
    "UNCHANGED": "NO_MEANINGFUL_CHANGE",
    "NONE": "NO_MEANINGFUL_CHANGE",
    "EXPAND": "EXPAND_REQUIREMENT",
    "EXTEND_REQUIREMENT": "EXPAND_REQUIREMENT",
    "ENHANCE_REQUIREMENT": "EXPAND_REQUIREMENT",
    "SPLIT": "SPLIT_REQUIREMENT",
    "SPLIT_REQUIREMENT_INTO_STORIES": "SPLIT_REQUIREMENT",
    "MERGE": "MERGE_REQUIREMENTS",
    "MERGE_REQUIREMENT": "MERGE_REQUIREMENTS",
    "CONSOLIDATE_REQUIREMENTS": "MERGE_REQUIREMENTS",
}

_ACTION_ALIASES: Dict[str, str] = {
    "ADD": "INSERT",
    "CREATE": "INSERT",
    "NEW": "INSERT",
    "INSERT_NEW": "INSERT",
    "MODIFY": "UPDATE",
    "MODIFIED": "UPDATE",
    "EDIT": "UPDATE",
    "REVISE": "UPDATE",
    "CHANGE": "UPDATE",
    "REMOVE": "ARCHIVE",
    "DELETE": "ARCHIVE",
    "DELETED": "ARCHIVE",
    "DROP": "ARCHIVE",
    "RETIRE": "ARCHIVE",
    "NONE": "NO_CHANGE",
    "NOCHANGE": "NO_CHANGE",
    "UNCHANGED": "NO_CHANGE",
    "SKIP": "NO_CHANGE",
    "KEEP": "NO_CHANGE",
    # ``MERGE`` is a classification, not a writer action: the surviving story is
    # rewritten (UPDATE) while absorbed stories get their own ARCHIVE entry.
    "MERGE": "UPDATE",
    "CONSOLIDATE": "UPDATE",
}



logger = logging.getLogger("app.semantic_service")

class SemanticChange(BaseModel):
    change_type: str = Field(..., description="Classification: " + ", ".join(SEMANTIC_CHANGE_TYPES))
    target_requirement_id: Optional[str] = Field(None, description="The ticket_code (e.g. US-001) of the affected user story, if any. Return null if it does not apply.")
    confidence: float = Field(..., description="Confidence score from 0.0 to 1.0")
    reason: str = Field(..., description="Detailed reasoning explaining why this change is classified this way.")
    recommended_action: str = Field(..., description="Action recommendation: " + ", ".join(SEMANTIC_ACTIONS))

    # The validators COERCE instead of raising: a sloppy model reply should be
    # repaired at the parse boundary, not turned into a failed workflow turn.
    @field_validator("change_type", mode="before")
    @classmethod
    def _validate_change_type(cls, value: Any) -> str:
        return normalize_change_type(value) or _clean_str(value).upper()

    @field_validator("recommended_action", mode="before")
    @classmethod
    def _validate_action(cls, value: Any, info) -> str:
        # ``info.data`` already holds the validated ``change_type`` (declared
        # first), so an omitted/unknown action can still be derived correctly.
        change_type = (getattr(info, "data", None) or {}).get("change_type", "")
        return normalize_recommended_action(value, change_type)

    @field_validator("target_requirement_id", mode="before")
    @classmethod
    def _validate_target(cls, value: Any) -> Optional[str]:
        return normalize_target_reference(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> float:
        return coerce_confidence(value, default=0.5)

    @field_validator("reason", mode="before")
    @classmethod
    def _validate_reason(cls, value: Any) -> str:
        return _clean_str(value)


class SemanticChangeDetectionResult(BaseModel):
    changes: List[SemanticChange] = Field(
        ...,
        description=(
            "List of all detected changes: ONE entry per affected user story, plus a "
            "single NO_MEANINGFUL_CHANGE entry when the message changes nothing."
        ),
    )


# ==========================================
# NORMALIZATION / RESOLUTION GUARDRAILS
# ==========================================
def _clean_str(value: Any) -> str:
    """Stringify and strip a possibly-``None`` LLM value."""
    return str(value).strip() if value is not None else ""


def normalize_change_type(value: Any) -> str:
    """Map a raw classification onto the canonical vocabulary (``""`` if unknown)."""
    text = " ".join(_clean_str(value).upper().replace("-", "_").split())
    if text in SEMANTIC_CHANGE_TYPES:
        return text
    return _CHANGE_TYPE_ALIASES.get(text, "")


def normalize_recommended_action(value: Any, change_type: str = "") -> str:
    """Canonical writer action, derived from the classification when needed.

    Handles the three failure modes seen in practice: an alias instead of the
    canonical action (``ADD`` -> ``INSERT``), the classification echoed into the
    action field (``MERGE_REQUIREMENTS`` -> ``UPDATE``), and a missing/garbage
    action (derived from ``change_type``, finally ``NO_CHANGE``). The return value
    is therefore always one of :data:`SEMANTIC_ACTIONS`.
    """
    text = " ".join(_clean_str(value).upper().replace("-", "_").split())
    action = text if text in SEMANTIC_ACTIONS else _ACTION_ALIASES.get(text, "")
    if not action:
        # The model sometimes echoes the classification into the action field.
        action = DEFAULT_ACTION_BY_CHANGE_TYPE.get(normalize_change_type(text), "")
    if not action:
        action = DEFAULT_ACTION_BY_CHANGE_TYPE.get(normalize_change_type(change_type) or change_type, "")
    if not action:
        logger.warning(
            "Unknown semantic action '%s' (change_type='%s'); defaulting to NO_CHANGE.", value, change_type
        )
        return "NO_CHANGE"
    return action


def coerce_confidence(value: Any, default: float = 0.5) -> float:
    """Clamp a raw confidence into ``[0.0, 1.0]`` (accepts numbers and ``"85%"``)."""
    text = _clean_str(value)
    try:
        parsed = float(text.rstrip("%")) if text else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    if text.endswith("%"):
        parsed = parsed / 100.0
    if parsed != parsed:  # NaN
        parsed = float(default)
    return min(1.0, max(0.0, parsed))


def normalize_target_reference(value: Any) -> Optional[str]:
    """Normalize an LLM target reference, or ``None`` when it means "no target"."""
    text = _clean_str(value)
    if text.lower() in _NULL_TARGET_TOKENS:
        return None
    code = normalize_ticket_code(text)
    if code.upper().startswith("US-"):
        return code.upper()
    # Not ticket-code shaped: keep it verbatim so it can still be matched as a
    # story title against the backlog.
    return text


def extract_explicit_story_references(
    text: Any,
    stories: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[str]:
    """Return canonical, existing story IDs explicitly written in user text."""
    known = build_story_target_index(stories)
    found: List[str] = []
    for match in re.finditer(
        r"(?<![A-Z0-9])#?US[\s_-]*(\d{1,6})(?!\d)",
        str(text or ""),
        re.IGNORECASE,
    ):
        code = normalize_ticket_code(f"US-{match.group(1)}")
        if code in known and code not in found:
            found.append(code)
    return found


def extract_story_ticket_codes(text: Any) -> List[str]:
    """Extract every explicitly written story code, including unknown ones."""
    found: List[str] = []
    for match in re.finditer(
        r"(?<![A-Z0-9])#?US[\s_-]*(\d{1,6})(?!\d)",
        str(text or ""),
        re.IGNORECASE,
    ):
        code = normalize_ticket_code(f"US-{match.group(1)}")
        if code not in found:
            found.append(code)
    return found


def build_story_target_index(stories: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, str]:
    """Map every reasonable target reference to its canonical ticket code."""
    index: Dict[str, str] = {}
    for story in stories or []:
        if not isinstance(story, dict):
            continue
        code = normalize_ticket_code(story.get("ticket_code"))
        if not code or code == _PLACEHOLDER_TICKET_CODE:
            continue
        index[code] = code
        title = normalize_title(story.get("story_title"))
        if title:
            index.setdefault(title, code)
    return index


def resolve_target_story(
    target_reference: Any,
    stories: Optional[Iterable[Dict[str, Any]]] = None,
    index: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a reference to a canonical ticket code, or ``None`` when unknown.

    Mirrors the merge service's progressive identity resolution: exact ticket
    code -> normalized title -> fuzzy title (``titles_match``). A target that
    cannot be resolved is a hallucination and must never drive a mutation.
    """
    text = normalize_target_reference(target_reference) if isinstance(target_reference, str) else target_reference
    if not text:
        return None

    story_list = [s for s in (stories or []) if isinstance(s, dict)]
    lookup = index if index is not None else build_story_target_index(story_list)

    for key in (str(text), normalize_ticket_code(text), normalize_title(text)):
        if key and lookup.get(key):
            return lookup[key]

    for story in story_list:
        code = normalize_ticket_code(story.get("ticket_code"))
        if not code or code == _PLACEHOLDER_TICKET_CODE:
            continue
        if titles_match(text, story.get("story_title")):
            return code
    return None


def _coerce_change_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one raw change dict onto the canonical recommendation shape.

    Used on the raw-JSON parse path (``json.loads`` bypasses the Pydantic
    validators) as well as on already-validated payloads.
    """
    raw_change_type = _clean_str(raw.get("change_type"))
    change_type = normalize_change_type(raw_change_type) or raw_change_type.upper().replace("-", "_")
    return {
        "change_type": change_type,
        "target_requirement_id": normalize_target_reference(raw.get("target_requirement_id")),
        "confidence": coerce_confidence(raw.get("confidence"), default=0.5),
        "reason": _clean_str(raw.get("reason")),
        "recommended_action": normalize_recommended_action(raw.get("recommended_action"), change_type),
    }


# GATHERING 5.1.1 — Explicit NO_CHANGE signal builder: used by the empty-message
#               guard (5.1) and by the validators to express "no action needed" with
#               a reason, instead of returning an empty list that callers would have
#               to interpret.
def build_no_change_recommendation(reason: str, confidence: float = DEFAULT_NO_CHANGE_CONFIDENCE) -> Dict[str, Any]:
    """The explicit "nothing meaningful changed" recommendation."""
    return {
        "change_type": "NO_MEANINGFUL_CHANGE",
        "target_requirement_id": None,
        "confidence": coerce_confidence(confidence),
        "reason": reason,
        "recommended_action": "NO_CHANGE",
    }

# GATHERING 5.5.1 — Canonicalizer for the model's change list: per-entry coercion
#               (`_coerce_change_entry`), conflict resolution
#               (`_resolve_action_conflict`), target verification/downgrade
#               (`_downgrade_unverifiable_change` + `resolve_target_story`) and
#               de-duplication (`_dedupe_changes`). Output actions are always
#               INSERT / UPDATE / ARCHIVE / NO_CHANGE (never NO_MEANINGFUL_CHANGE).
def normalize_semantic_changes(
    raw_changes: Optional[Iterable[Any]],
    current_stories: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Deterministically repair/validate the LLM's detected semantic changes.

    Guarantees (each one covered by ``tests/test_semantic_service.py``):

    1. every returned ``change_type`` is canonical and every
       ``recommended_action`` is one the writers actually act on;
    2. ``confidence`` is clamped into ``[0.0, 1.0]`` and lowered whenever a
       recommendation could not be fully verified;
    3. an unresolved (or missing) target on a target-requiring classification is
       downgraded to ``NO_CHANGE`` below
       :data:`SEMANTIC_LOW_CONFIDENCE_THRESHOLD`, so the Gatherer asks the user
       instead of guessing;
    4. ``NEW_REQUIREMENT`` never carries a target story;
    5. there is at most one recommendation per target, with conflicting actions
       resolved by :data:`_ACTION_PRIORITY` and forced into the clarification
       band;
    6. the list always contains at least one entry (an empty model answer becomes
       an explicit ``NO_MEANINGFUL_CHANGE``).
    """
    stories = [s for s in (current_stories or []) if isinstance(s, dict)]
    index = build_story_target_index(stories)
    cleaned: List[Dict[str, Any]] = []

    for raw in raw_changes or []:
        if not isinstance(raw, dict):
            logger.warning("Ignoring malformed semantic change entry (not an object): %r", raw)
            continue

        entry = _coerce_change_entry(raw)
        change_type = entry["change_type"]
        if change_type not in SEMANTIC_CHANGE_TYPES:
            logger.warning("Ignoring semantic change with unknown classification: %r", raw.get("change_type"))
            continue

        target = entry["target_requirement_id"]
        if change_type == "NEW_REQUIREMENT":
            # A brand-new requirement cannot be attached to an existing story.
            if target is not None:
                logger.info(
                    "Dropping target '%s' from NEW_REQUIREMENT (a new requirement has no existing story).", target
                )
            entry["target_requirement_id"] = None
            entry["recommended_action"] = "INSERT"
        elif target is not None:
            resolved = resolve_target_story(target, stories, index)
            if resolved:
                entry["target_requirement_id"] = resolved
            else:
                entry = _downgrade_unverifiable_change(
                    entry, f"Target story '{target}' does not exist in the current backlog."
                )
        elif change_type in TARGET_REQUIRED_CHANGE_TYPES:
            entry = _downgrade_unverifiable_change(
                entry, f"'{change_type}' requires an existing ticket code but no target was provided."
            )

        cleaned.append(entry)

    deduped = _dedupe_changes(cleaned)
    meaningful = [c for c in deduped if c["change_type"] != "NO_MEANINGFUL_CHANGE"]
    if meaningful:
        # An explicit "nothing changed" alongside real changes is just noise.
        return meaningful
    if deduped:
        return deduped
    logger.warning("Semantic change detection produced no usable entries; emitting an explicit NO_CHANGE.")
    return [build_no_change_recommendation("The model reported no semantic change for this message.")]


def _downgrade_unverifiable_change(entry: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Turn an unverifiable recommendation into a low-confidence ``NO_CHANGE``.

    Keeping the (now un-actionable) recommendation would let the Gatherer invent
    a target; dropping it silently would hide the ambiguity. A ``NO_CHANGE``
    below the clarification threshold does neither: nothing is mutated and the
    user is asked which story was meant.
    """
    downgraded = dict(entry)
    downgraded["change_type"] = "NO_MEANINGFUL_CHANGE"
    downgraded["recommended_action"] = "NO_CHANGE"
    downgraded["confidence"] = min(entry["confidence"], UNRESOLVED_TARGET_CONFIDENCE)
    note = f"{reason} No change was applied; please confirm the ticket code."
    downgraded["reason"] = f"{entry['reason']} {note}".strip()
    logger.warning("Semantic change downgraded to NO_CHANGE: %s", note)
    return downgraded


def _resolve_action_conflict(first: Dict[str, Any], second: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Pick the winning recommendation out of two conflicting ones for one target."""
    first_rank = _ACTION_PRIORITY.get(first["recommended_action"], -1)
    second_rank = _ACTION_PRIORITY.get(second["recommended_action"], -1)
    if first_rank == second_rank:
        # Same priority (e.g. two classifications that both map to UPDATE)
        # -> the more confident one wins.
        return (first, second) if first["confidence"] >= second["confidence"] else (second, first)
    return (first, second) if first_rank > second_rank else (second, first)


def _dedupe_changes(changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One recommendation per target, with deterministic conflict resolution."""
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for entry in changes:
        key = entry.get("target_requirement_id") or "__project__"
        if key not in by_key:
            by_key[key] = entry
            order.append(key)
            continue

        kept = by_key[key]
        if kept["recommended_action"] == entry["recommended_action"]:
            # Same action twice: keep the more confident wording.
            if entry["confidence"] > kept["confidence"]:
                by_key[key] = entry
            continue

        winner, loser = _resolve_action_conflict(kept, entry)
        conflict_note = (
            f"Conflicting recommendations for {key}: "
            f"'{loser['change_type']}' ({loser['recommended_action']}) vs "
            f"'{winner['change_type']}' ({winner['recommended_action']}); "
            "the more conservative action was kept. Please confirm the intent."
        )
        logger.warning(conflict_note)
        winner = dict(winner)
        winner["confidence"] = min(winner["confidence"], CONFLICTING_ACTIONS_CONFIDENCE)
        winner["reason"] = f"{winner['reason']} {conflict_note}".strip()
        by_key[key] = winner

    return [by_key[key] for key in order]

# GATHERING 5.0 — Semantic change detection service (the deterministic target finder
#             that feeds GATHERING 2.2/2.3 and the merge recommendations). Chain:
#             5.1 empty guard → 5.2 prompt (prompts/semantic.md) → 5.3 structured LLM
#             call → 5.4 fail-loud on parse failure → 5.5 normalization/repair
#             (5.5.1 + `_downgrade_unverifiable_change` / `_resolve_action_conflict` /
#             `_dedupe_changes`). Pure service: no DB, no state writes.
async def detect_semantic_changes(
    raw_input: str,
    current_stories: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Compare existing user stories with ``raw_input`` and classify every change.

    Returns normalized recommendation dicts (see
    :func:`normalize_semantic_changes`) whose ``recommended_action`` is always one
    of ``INSERT`` / ``UPDATE`` / ``ARCHIVE`` / ``NO_CHANGE`` and whose
    ``target_requirement_id`` is always a real ticket code from
    ``current_stories`` (or ``None`` for a brand-new requirement).
    """
    stories = [s for s in (current_stories or []) if isinstance(s, dict)]
    logger.info(
        "Running semantic change detection on new message: '%s' against %d existing user story(ies)",
        str(raw_input)[:100],
        len(stories),
    )

    # GATHERING 5.1 — Empty-message guard: no LLM round-trip, but still an explicit
    #             signal (5.1.1) instead of an ambiguous empty list, so callers can see
    #             "nothing to compare" versus "nothing changed".
    if not str(raw_input or "").strip():
        # Nothing to compare: skip the LLM round-trip but still return an
        # explicit signal instead of an ambiguous empty list.
        return [build_no_change_recommendation("The message is empty; there is no semantic change to detect.")]

    # Format current stories into a clean visual summary for the prompt
    # (shared with the Gatherer/intent prompts via merge_service).
    context_lines = format_story_context_lines(stories)
    current_context = "\n".join(context_lines) if context_lines else "No existing user stories in this project."

    # GATHERING 5.2 — Prompt assembly from prompts/semantic.md: the backlog context
    #             (shared formatter, GATHERING 6.6) plus the new message. The markdown
    #             file is not annotated (its text is sent to the model verbatim).
    prompt_template = PromptTemplate(
        template=load_prompt("semantic"),
        input_variables=["current_context", "new_message", "format_instructions"]
    )

    pydantic_parser = JsonOutputParser(pydantic_object=SemanticChangeDetectionResult)
    format_instructions = pydantic_parser.get_format_instructions()

    def _is_valid_changes(result: Dict[str, Any]) -> bool:
        """Reject a payload without a usable ``changes`` list so the raw-JSON retry runs."""
        changes = result.get("changes")
        return isinstance(changes, list) and all(isinstance(c, dict) for c in changes)

    # Prefer .with_structured_output(), falling back to raw JSON parse
    try:
        # GATHERING 5.3 — Structured LLM call (llm_utils; LM Studio via llm_factory,
        #             CHAT 6.1) returning SemanticChangeDetectionResult. The validator
        #             above rejects payloads without a usable `changes` list so the
        #             raw-JSON retry runs before 5.4.
        result = await invoke_llm_structured(
            llm,
            prompt_template,
            SemanticChangeDetectionResult,
            variables={
                "current_context": current_context,
                "new_message": raw_input,
                "format_instructions": "Output ONLY raw JSON matching the schema.",
            },
            description="semantic change detection",
            format_instructions=format_instructions,
            validate=_is_valid_changes,
        )
        logger.info("Successfully obtained structured output for semantic changes.")
    # GATHERING 5.4 — FAIL LOUD: a parse/LLM failure raises RuntimeError to the caller
    #             (surfaced as HTTP 500). Defaulting to an "INSERT/NEW" recommendation
    #             would masquerade as a legitimate new requirement and drive a wrong
    #             workflow decision — the opposite of GATHERING 2.4's safe stop.
    except Exception as e2:
        logger.error(f"Semantic change detection parsing failed completely: {str(e2)}", exc_info=True)
        # FAIL LOUDLY instead of silently defaulting to a NEW_REQUIREMENT "INSERT"
        # recommendation. A parse/LLM failure would otherwise be masked as a
        # legitimate new requirement and drive a wrong workflow decision, so we
        # propagate the error to the caller (which surfaces it as HTTP 500).
        raise RuntimeError(f"Semantic change detection failed: {str(e2)}") from e2

    # GATHERING 5.5 — Deterministic repair/validation pass (5.5.1): coerces every
    #             entry to a canonical change_type/recommended_action, resolves
    #             targets against the REAL backlog (unverifiable targets are
    #             downgraded), resolves contradictory actions, de-duplicates and
    #             computes the confidence the caller gates on (GATHERING 2.4).
    raw_changes = [dict(item) for item in (result.get("changes") or []) if isinstance(item, dict)]
    explicit_targets = extract_explicit_story_references(raw_input, stories)
    if len(explicit_targets) == 1:
        # An ID written by the user is stronger evidence than an inferred LLM
        # target. Apply it to every target-requiring change in this single-ID
        # message before canonical validation. Multi-ID messages remain with
        # the model because each change still needs its own mapping.
        explicit_target = explicit_targets[0]
        for entry in raw_changes:
            if normalize_change_type(entry.get("change_type")) in TARGET_REQUIRED_CHANGE_TYPES:
                entry["target_requirement_id"] = explicit_target
    changes = normalize_semantic_changes(raw_changes, stories)
    logger.info(
        "Detected %d semantic change(s): %s",
        len(changes),
        [
            {
                "change_type": c["change_type"],
                "target": c["target_requirement_id"],
                "action": c["recommended_action"],
                "confidence": round(c["confidence"], 2),
            }
            for c in changes
        ],
    )
    return changes






# INTENT 2.0 — The detector: raw user text (+ optional story context) → one of five
#             intents with a confidence and a reason. Layer chain:
#               1.1/1.3, 3.1, 3.2, 4.1.3 (callers) → 2.1 guard → 2.2 context →
#               2.3 prompt (prompts/intent.md) → 2.4 parser/format →
#               2.6 shared structured LLM call (llm_utils) → 2.7 normalization;
#               any failure falls back to the deterministic ladder 2.8.
#             Pure service: no DB writes, no state mutation, no caching.
async def detect_requirement_intent(raw_input: str, current_stories: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Uses LLM for semantic intent classification on user message.
    Returns a dict with: intent, confidence, reason.
    Supports intents: GENERAL_CHAT, CREATE_REQUIREMENT, UPDATE_REQUIREMENT, DELETE_REQUIREMENT, CLARIFY_REQUIREMENT.
    """
    # INTENT 2.0.1 — Allowed vocabulary. DUPLICATION (documented, not changed):
    #               this literal list duplicates the intent values described in
    #               schemas.RequirementIntentDetectionResult (INTENT 5.1) and in
    #               prompts/intent.md; nothing keeps the three in sync at runtime.
    VALID_INTENTS = ["GENERAL_CHAT", "CREATE_REQUIREMENT", "UPDATE_REQUIREMENT", "DELETE_REQUIREMENT", "CLARIFY_REQUIREMENT"]
    
    # INTENT 2.1 — Input guard: blank input short-circuits to GENERAL_CHAT with full
    #             confidence (which is why an empty PRD/Audit request classifies as
    #             chat — hence the bypass rule documented at CHAT 2.3.0).
    if not raw_input or not str(raw_input).strip():
        return {
            "intent": "GENERAL_CHAT",
            "confidence": 1.0,
            "reason": "Empty input defaulted to GENERAL_CHAT."
        }

    normalized_input = str(raw_input).strip().lower()
    requirement_brief_markers = (
        "new project requirements:",
        "requirement brief:",
        "gather the following requirements:",
        "add the following requirements:",
    )
    if any(marker in normalized_input for marker in requirement_brief_markers):
        return {
            "intent": "CREATE_REQUIREMENT",
            "confidence": 0.99,
            "reason": "The message supplies new product specifications to gather before PRD generation.",
        }
        
    logger.info(f"Detecting requirement intent for message: '{raw_input[:100]}'")
    
    # INTENT 2.2 — Context build: the canonical backlog rendering (shared helper,
    #             same format as the gatherer/matcher prompts) or a placeholder when
    #             the project has no stories yet. This is what lets the classifier
    #             tell "update the transfer limit" (existing) from "add transfers".
    context_lines = format_story_context_lines(current_stories)
    current_context = "\n".join(context_lines) if context_lines else "No existing user stories in this project."

    # INTENT 2.3 — Prompt assembly from prompts/intent.md (the intent taxonomy:
    #             GENERAL_CHAT / CREATE / UPDATE / DELETE / CLARIFY + the "ask a
    #             question ⇒ GENERAL_CHAT" rule). NOTE: no comment block is added to
    #             that markdown file on purpose — its text is sent verbatim to the
    #             model, so annotating it would change LLM behaviour.
    prompt_template = PromptTemplate(
        template=load_prompt("intent"),
        input_variables=["current_context", "user_message", "format_instructions"]
    )
    
    # INTENT 2.4 — JSON contract: the pydantic model doubles as the schema handed to
    #             the LLM (format instructions) and as the validation target in 2.6.
    pydantic_parser = JsonOutputParser(pydantic_object=RequirementIntentDetectionResult)
    format_instructions = pydantic_parser.get_format_instructions()
    
    # Prefer .with_structured_output(), falling back to raw JSON parse
    # INTENT 2.5 — Acceptance test for the model's answer: the intent must be one of
    #             INTENT 2.0.1 (case-insensitive), otherwise 2.6 retries and finally
    #             raises into the 2.8 fallback ladder.
    def _is_valid_intent(result: Dict[str, Any]) -> bool:
        return str(result.get("intent", "")).strip().upper() in VALID_INTENTS

    try:
        # INTENT 2.6 — Shared structured-output call (llm_utils.invoke_llm_structured):
        #             it owns the raw-JSON → with_structured_output retry, the
        #             markdown-fence stripping and the `reasoning_content` recovery.
        #             Model/transport: llm_factory → LM Studio (CHAT 6.1).
        result = await invoke_llm_structured(
            llm,
            prompt_template,
            RequirementIntentDetectionResult,
            variables={
                "current_context": current_context,
                "user_message": raw_input,
                "format_instructions": "Output ONLY raw JSON matching the schema.",
            },
            description="intent detection",
            format_instructions=format_instructions,
            validate=_is_valid_intent,
        )
        # INTENT 2.7 — Normalization: upper-cased intent, numeric confidence (default
        #             0.90 when the model omitted it), and `reason` preferred over the
        #             schema's `reasoning` alias. Returned verbatim to the callers.
        intent = str(result.get("intent", "")).strip().upper()
        confidence = float(result.get("confidence", 0.90))
        reason = str(result.get("reason") or result.get("reasoning") or "")
        logger.info(f"Successfully detected requirement intent via LLM: {intent} (confidence: {confidence})")
        return {
            "intent": intent,
            "confidence": confidence,
            "reason": reason
        }
    # INTENT 2.8 — LLM/parse failure: logged, then the deterministic keyword ladder
    #             below classifies instead. This is what keeps the workflow usable
    #             when the gateway is down (the pipeline still gets an intent).
    except Exception as e2:
        logger.error(f"Intent detection parsing failed completely: {str(e2)}")

    # Fallback: heuristic classification
    # INTENT 2.8.1 — Fallback ladder (ordered, first match wins): greeting → question
    #              → clarify/audit → delete → update → create → default GENERAL_CHAT
    #              at 0.80. Deliberately conservative: anything unrecognised stays a
    #              conversation rather than fabricating a requirement operation.
    lower_inp = raw_input.lower().strip()
    delete_command = bool(
        "no longer need" in lower_inp
        or re.match(
            r"^(?:please\s+)?(?:remove|delete|cancel|drop|discard|retire|archive)\b",
            lower_inp,
        )
        or re.match(
            r"^(?:can|could|would)\s+you\s+(?:please\s+)?(?:remove|delete|cancel|drop|discard|retire|archive)\b",
            lower_inp,
        )
    )
    if lower_inp in ["hello", "hi", "thank you", "thanks", "good morning", "good afternoon", "good evening", "ok", "okay", "got it", "awesome", "great"]:
        return {
            "intent": "GENERAL_CHAT",
            "confidence": 0.95,
            "reason": "The user is providing a conversational greeting or pleasantry."
        }
    elif delete_command:
        return {
            "intent": "DELETE_REQUIREMENT",
            "confidence": 0.85,
            "reason": "The user is requesting to archive an existing requirement."
        }
    elif lower_inp.startswith("what") or lower_inp.startswith("explain") or lower_inp.startswith("how") or lower_inp.startswith("can you") or lower_inp.endswith("?"):
        return {
            "intent": "GENERAL_CHAT",
            "confidence": 0.90,
            "reason": "The user is asking a question or seeking an explanation."
        }
    elif any(kw in lower_inp for kw in ["run audit", "run auditor", "audit", "validate", "clarify", "review requirement"]):
        return {
            "intent": "CLARIFY_REQUIREMENT",
            "confidence": 0.85,
            "reason": "The user is requesting validation, audit, or clarification of requirements."
        }
    elif any(kw in lower_inp for kw in ["update ", "change ", "modify ", "adjust ", "revise ", "edit "]):
        return {
            "intent": "UPDATE_REQUIREMENT",
            "confidence": 0.85,
            "reason": "The user is requesting to modify or update an existing requirement."
        }
    elif any(kw in lower_inp for kw in ["add ", "create ", "new ", "implement ", "introduce "]):
        return {
            "intent": "CREATE_REQUIREMENT",
            "confidence": 0.85,
            "reason": "The user is requesting to add a new requirement or feature."
        }
    else:
        return {
            "intent": "GENERAL_CHAT",
            "confidence": 0.80,
            "reason": "Fallback: could not determine intent from input."
        }


# CHAT 2.3.2 — Service layer of the conversation branch. Reads its system prompt
#             from prompts/general_chat.md, injects a compact project context
#             (stories/ACs/PRD preview + last 10 turns) and answers with the shared
#             LLM (via app/llm_factory). NEVER mutates project artifacts, and it
#             never raises: the caller gets the canned fallback below instead.
async def generate_general_chat_response(
    raw_input: str,
    project_context: Dict[str, Any],
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """
    Generates a conversational response for GENERAL_CHAT intents.
    Uses the same LLM client (no new agent or model).
    Provides full project context to the LLM for informed answers.
    Does NOT modify any project artifacts.
    """
    logger.info(f"Generating general chat response for: '{raw_input[:100]}'")
    
    # Build project context summary
    project_name = project_context.get("project_name", "Unnamed Project")
    reqs = project_context.get("requirements", {})
    if isinstance(reqs, list):
        epic_name = reqs[0].get("title", "") if reqs else ""
    elif isinstance(reqs, dict):
        epic_name = reqs.get("epic_name", "")
    else:
        epic_name = ""
    user_stories = project_context.get("user_stories", [])
    acceptance_criteria = project_context.get("acceptance_criteria", [])
    prd_markdown = project_context.get("generated_prd", "")
    
    context_parts = [f"Project Name: {project_name}"]
    if epic_name:
        context_parts.append(f"Epic: {epic_name}")
    
    if user_stories:
        story_lines = []
        for s in user_stories:
            ac_list = s.get("acceptance_criteria", [])
            ac_str = "; ".join(ac_list[:3])  # Limit to first 3 ACs to avoid token overflow
            story_lines.append(
                f"- {s.get('ticket_code', 'US')}: {s.get('story_title', '')}\n"
                f"  As a {s.get('as_a', '')}, I want to {s.get('i_want_to', '')}, So that {s.get('so_that', '')}\n"
                f"  Acceptance Criteria: {ac_str}"
            )
        context_parts.append("Current User Stories:\n" + "\n".join(story_lines))
    
    if acceptance_criteria:
        ac_summary = "\n".join([f"- {ac}" for ac in acceptance_criteria[:5]])
        context_parts.append(f"Acceptance Criteria:\n{ac_summary}")
    
    if prd_markdown:
        # Truncate PRD to avoid token overflow
        prd_preview = prd_markdown[:1500]
        context_parts.append(f"Current PRD (preview):\n{prd_preview}")
    
    # Build conversation history context (last 10 messages)
    if conversation_history:
        recent_msgs = conversation_history[-10:]
        history_lines = []
        for msg in recent_msgs:
            role = msg.get("role", "unknown")
            content = msg.get("message", msg.get("content", ""))
            if len(content) > 200:
                content = content[:200] + "..."
            history_lines.append(f"[{role}]: {content}")
        context_parts.append("Recent Conversation History:\n" + "\n".join(history_lines))
    
    full_context = "\n\n".join(context_parts)
    
    system_prompt = load_prompt("general_chat")
    
    try:
        from app.llm_factory import llm
        from langchain_core.messages import SystemMessage, HumanMessage
        
        messages = [
            SystemMessage(content=system_prompt + "\n\nCurrent Project Context:\n" + full_context),
            HumanMessage(content=raw_input)
        ]
        
        # CHAT 2.3.2 — LLM call: same shared ChatOpenAI client used by the agents
        #             (no separate model/gateway), so the offline behaviour is
        #             identical to every other flow (CHAT 6.x).
        response = await llm.ainvoke(messages)
        response_text = response.content if hasattr(response, "content") else str(response)
        logger.info(f"Generated general chat response ({len(response_text)} chars)")
        return response_text
    except Exception as e:
        logger.error(f"Failed to generate general chat response: {str(e)}")
        # CHAT 2.3.2 — Fail-soft fallback: an LLM failure yields a friendly reply
        #             instead of a 500, so a chat turn can never break the session.
        return f"I understand you're asking about: '{raw_input}'. However, I encountered an issue generating a detailed response. Please try rephrasing your question."


# MATCHING 2.0 — The matcher service: message + intent + backlog → the target story
#             (or an explicit "cannot tell"). Chain: 2.1 guard → 2.2 context →
#             2.3 prompt (prompts/matcher.md) + parser → 2.4 LLM call →
#             2.5.1–2.5.4 deterministic validation of the model's answer →
#             2.6 fallback dict. Pure service: no DB writes, no caching.
#             Return contract: {matched_requirement_id, confidence, reason, action,
#             status, candidates} — consumed by MATCHING 1.x and 3.x.
async def match_requirement(raw_input: str, detected_intent: str, current_stories: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Requirement Matcher Agent between Intent Detection and Gatherer.
    Determines which existing requirement(s) the user's message refers to before any modification occurs.
    """
    # MATCHING 2.1 — Guard: no text → nothing to match; returns MATCHED/NEW with
    #             confidence 1.0, i.e. "treat as brand new" (no clarification).
    if not raw_input or not str(raw_input).strip():
        return {
            "matched_requirement_id": None,
            "confidence": 1.0,
            "reason": "Empty input.",
            "action": "NEW",
            "status": "MATCHED",
            "candidates": None
        }

    logger.info(f"Running Requirement Matcher for input: '{raw_input[:100]}' with intent: {detected_intent}")

    # Same canonical backlog rendering as the semantic-change/intent/gatherer
    # prompts (previously this function kept its own copy-pasted format).
    # MATCHING 2.2 — Context build: the canonical backlog rendering (shared helper;
    #             identical format to the intent/gatherer prompts), filtered to real
    #             dict rows. Provides the ticket codes the model must choose from.
    context_lines = format_story_context_lines(
        [s for s in (current_stories or []) if isinstance(s, dict)]
    )
    current_context = "\n".join(context_lines) if context_lines else "No existing user stories in this project."

    # MATCHING 2.3 — Prompt assembly from prompts/matcher.md, parameterised by the
    #             already-detected intent so CREATE messages are not forced to name
    #             an existing story. Not annotated (prompt text reaches the model).
    prompt_template = PromptTemplate(
        template=load_prompt("matcher"),
        input_variables=["current_context", "detected_intent", "user_message", "format_instructions"]
    )

    # MATCHING 2.3.1 — JSON contract + format instructions (RequirementMatcherResult,
    #               MATCHING 5.2).
    pydantic_parser = JsonOutputParser(pydantic_object=RequirementMatcherResult)
    format_instructions = pydantic_parser.get_format_instructions()

    # Prefer .with_structured_output(), falling back to raw JSON parse
    # MATCHING 2.3.2 — Acceptance test (structural): an answer without both an
    #               action and a status is not actionable, so 2.4 retries and
    #               finally falls through to 2.6.
    def _is_valid_match(result: Dict[str, Any]) -> bool:
        """A match must carry a status and an action to be actionable."""
        return bool(_clean_str(result.get("action"))) and bool(_clean_str(result.get("status")))

    try:
        # MATCHING 2.4 — Shared structured-output call: raw-JSON attempt first, then
        #             with_structured_output (llm_utils). Model: llm_factory → LM
        #             Studio (CHAT 6.1). Output normalized right below.
        result = await invoke_llm_structured(
            llm,
            prompt_template,
            RequirementMatcherResult,
            variables={
                "current_context": current_context,
                "detected_intent": detected_intent,
                "user_message": raw_input,
                "format_instructions": "Output ONLY raw JSON matching the schema.",
            },
            description="requirement matcher",
            format_instructions=format_instructions,
            validate=_is_valid_match,
        )
        confidence = coerce_confidence(result.get("confidence", 0.85), default=0.85)
        status_val = _clean_str(result.get("status", "MATCHED")).upper() or "MATCHED"
        result["confidence"] = confidence
        result["status"] = status_val
        result["action"] = _clean_str(result.get("action")).upper() or "NEW"

        # Verify the reported target against the real backlog: a hallucinated or
        # mis-typed ticket code must not drive a mutation (same guardrail as the
        # semantic-change layer).
        # MATCHING 2.5.1 — Anti-hallucination guard (the decisive step): the returned
        #               ticket code is validated against the real backlog via
        #               resolve_target_story. A code that does not exist downgrades
        #               the result to LOW_CONFIDENCE/NEW with the confidence capped at
        #               UNRESOLVED_TARGET_CONFIDENCE, which forces the clarification
        #               branch in MATCHING 1.5 instead of mutating the wrong story.
        matched_id = normalize_target_reference(result.get("matched_requirement_id"))
        if matched_id:
            resolved = resolve_target_story(matched_id, current_stories)
            if resolved:
                result["matched_requirement_id"] = resolved
            else:
                logger.warning(
                    "Requirement matcher returned target '%s' which is not in the backlog; "
                    "forcing clarification.", matched_id,
                )
                result["matched_requirement_id"] = None
                result["action"] = "NEW"
                result["status"] = "LOW_CONFIDENCE"
                result["confidence"] = min(confidence, UNRESOLVED_TARGET_CONFIDENCE)
                result["reason"] = (
                    f"{_clean_str(result.get('reason'))} Target '{matched_id}' does not exist in the "
                    "current backlog. Please confirm the ticket code."
                ).strip()
        # MATCHING 2.5.2 — UPDATE/DELETE without a resolvable target is not
        #               actionable: downgrade to NEW + LOW_CONFIDENCE (never guess an
        #               existing story to modify or delete).
        elif str(result["action"]) in ("UPDATE", "DELETE") and current_stories:
            # UPDATE/DELETE without a resolvable target is not actionable.
            logger.warning("Requirement matcher returned action %s without a valid target.", result["action"])
            result["action"] = "NEW"
            result["status"] = "LOW_CONFIDENCE"
            result["confidence"] = min(confidence, UNRESOLVED_TARGET_CONFIDENCE)
            result["reason"] = (
                f"{_clean_str(result.get('reason'))} No existing story could be identified; "
                "please confirm the ticket code."
            ).strip()

        # MATCHING 2.5.3 — Candidate list hygiene: every candidate is resolved to a
        #               real ticket code (or normalized) and an empty list becomes
        #               None, so MATCHING 1.5 never renders an empty "could refer to".
        candidates = result.get("candidates")
        if isinstance(candidates, list):
            result["candidates"] = [
                resolve_target_story(c, current_stories) or normalize_target_reference(c)
                for c in candidates if _clean_str(c)
            ] or None

        # MATCHING 2.5.4 — Confidence floor: a MATCHED verdict below 0.75 is relabelled
        #               LOW_CONFIDENCE so MATCHING 1.5's single threshold rule holds
        #               regardless of which status the model reported.
        if result["confidence"] < 0.75 and result["status"] == "MATCHED":
            result["status"] = "LOW_CONFIDENCE"
            result["reason"] = f"Confidence {result['confidence']} is below threshold (0.75). Clarification required."
        logger.info(f"Requirement matcher result: {result}")
        return result
    # MATCHING 2.6 — Parse failure fallback: returns MATCHED/NEW at 0.80 (i.e. "treat
    #             as a new requirement") rather than MATCHING 1.5's clarification, so
    #             an unreachable model degrades to additive extraction instead of
    #             blocking the user with a question the model could not have answered.
    except Exception as e2:
        logger.error(f"Requirement matcher parsing failed completely: {str(e2)}")

    return {
        "matched_requirement_id": None,
        "confidence": 0.80,
        "reason": f"Fallback due to parsing failure",
        "action": "NEW",
        "status": "MATCHED",
        "candidates": None
    }



# ROUTING 3.0 — Workflow classifier (the LLM half of ROUTING 2.x). One of four
#             workflows (CHAT | QUESTION | COMMAND | REQUIREMENT) + confidence +
#             reason. Chain: 3.1 guard → 3.2 prompt (prompts/router.md) →
#             3.3 parser/validator → 3.4 shared structured call → 3.5 heuristic
#             ladder on any failure. Pure service: no DB, no state, no caching.
async def classify_workflow(raw_input: str) -> Dict[str, Any]:
    """
    Lightweight Workflow Router layer before the Gatherer Agent.
    Classifies every incoming user message into one of four workflow types:
    - CHAT
    - QUESTION
    - COMMAND
    - REQUIREMENT
    
    Returns structured JSON dict:
    {
        "workflow": "QUESTION",
        "confidence": 0.96,
        "reason": "The user is asking for an explanation rather than modifying project requirements."
    }
    """
    # ROUTING 3.1 — Guard: blank input → CHAT with full confidence (never reaches
    #             an agent). Note this is the OPPOSITE default to ROUTING 2.1's
    #             REQUIREMENT fallback: an empty message answers as chat, but an
    #             unparsable classification retries the requirement path.
    if not raw_input or not str(raw_input).strip():
        return {
            "workflow": "CHAT",
            "confidence": 1.0,
            "reason": "Empty input defaulted to CHAT."
        }

    clean_input = str(raw_input).strip()
    lower_input = clean_input.lower()
    requirement_brief_markers = (
        "new project requirements:",
        "requirement brief:",
        "gather the following requirements:",
        "add the following requirements:",
    )
    if any(marker in lower_input for marker in requirement_brief_markers):
        return {
            "workflow": "REQUIREMENT",
            "confidence": 0.99,
            "reason": "The message contains new product specifications that must be gathered before generating a PRD.",
        }
    if re.match(r"^(?:create|generate)(?:\s+a|\s+the)?(?:\s+complete)?\s+prd\b", lower_input):
        return {
            "workflow": "COMMAND",
            "confidence": 0.99,
            "reason": "The user explicitly requested PRD document generation from saved requirements.",
        }
    logger.info(f"Routing workflow classification for input: '{clean_input[:100]}'")

    # ROUTING 3.2 — Prompt assembly from prompts/router.md (workflow taxonomy:
    #             CHAT / QUESTION / COMMAND / REQUIREMENT). As with INTENT 2.3, no
    #             annotation is added to that markdown file: its text is sent to the
    #             model verbatim, so comments there would change LLM behaviour.
    prompt_template = PromptTemplate(
        template=load_prompt("router"),
        input_variables=["user_message", "format_instructions"]
    )

    # ROUTING 3.3 — JSON contract + format instructions for the prompt.
    pydantic_parser = JsonOutputParser(pydantic_object=WorkflowRoutingResult)
    format_instructions = pydantic_parser.get_format_instructions()

    # Prefer .with_structured_output(), falling back to raw JSON parse
    # ROUTING 3.3.1 — Allowed workflow vocabulary. DUPLICATION (documented, not
    #               changed): same three-way split as INTENT 2.0.1 — this list, the
    #               schema description (ROUTING 5.2) and prompts/router.md.
    VALID_WORKFLOWS = ["CHAT", "QUESTION", "COMMAND", "REQUIREMENT"]

    # ROUTING 3.3.2 — Acceptance test: the workflow must be in 3.3.1 (case-
    #               insensitive), else 3.4 retries and finally raises into 3.5.
    def _is_valid_workflow(result: Dict[str, Any]) -> bool:
        return str(result.get("workflow", "")).strip().upper() in VALID_WORKFLOWS

    try:
        # ROUTING 3.4 — Shared structured-output call (llm_utils.invoke_llm_structured:
        #             raw-JSON first, then with_structured_output, fence stripping and
        #             reasoning-content recovery). Transport: LM Studio (CHAT 6.1).
        result = await invoke_llm_structured(
            llm,
            prompt_template,
            WorkflowRoutingResult,
            variables={
                "user_message": clean_input,
                "format_instructions": "Output ONLY raw JSON matching the schema.",
            },
            description="workflow routing",
            format_instructions=format_instructions,
            validate=_is_valid_workflow,
        )
        wf = str(result.get("workflow", "")).strip().upper()
        result["workflow"] = wf
        result["confidence"] = float(result.get("confidence", 0.90))
        result["reason"] = str(result.get("reason", ""))
        logger.info(f"Workflow router classified input via LLM: {result}")
        return result
    # ROUTING 3.5 — LLM/parse failure: logged, then the keyword ladder below
    #             classifies deterministically so the graph still makes progress
    #             when the gateway is offline.
    except Exception as e2:
        logger.error(f"Workflow router raw parsing failed: {str(e2)}")

    # Heuristic Fallback
    # ROUTING 3.5.1 — Fallback ladder (ordered): greeting → question → COMMAND
    #              keywords ("run auditor", "generate prd", "generate diagram",
    #              "export docx", "audit requirements") → default REQUIREMENT at
    #              0.85. The default differs from INTENT 2.8.1's chat default: this
    #              ladder assumes a requirement rather than a conversation.
    lower_inp = clean_input.lower()
    if lower_inp in ["hello", "hi", "thank you", "thanks", "good morning", "good afternoon", "good evening"]:
        return {
            "workflow": "CHAT",
            "confidence": 0.95,
            "reason": "The user is providing a conversational greeting or pleasantry."
        }
    elif lower_inp.startswith("what") or lower_inp.startswith("explain") or lower_inp.startswith("how") or lower_inp.startswith("can you explain") or lower_inp.endswith("?"):
        return {
            "workflow": "QUESTION",
            "confidence": 0.90,
            "reason": "The user is asking a direct question or seeking an explanation."
        }
    elif any(cmd in lower_inp for cmd in ["run auditor", "generate prd", "generate diagram", "export docx", "audit requirements"]):
        return {
            "workflow": "COMMAND",
            "confidence": 0.90,
            "reason": "The user is requesting an automated backend command execution."
        }
    else:
        return {
            "workflow": "REQUIREMENT",
            "confidence": 0.85,
            "reason": "The user input describes a software requirement or functional modification."
        }


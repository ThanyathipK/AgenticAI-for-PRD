"""
Document processing helpers — token routing, markdown chunking and DRAFT-ONLY
requirements extraction for uploaded documents.

Separation of concerns enforced by the product intent (see ``routes/documents.py``):

* Storage is separate from LLM feeding. Uploads persist the FULL converted
  markdown regardless of token size (``MAX_UPLOAD_MB`` is the only size gate).
* Token budgets apply exclusively to the explicit "process this document"
  path. That path routes the document to the gatherer either in one call
  (``token_count <= DOCUMENT_BUDGET``) or across sequential chunks (each
  ~``DOCUMENT_CHUNK_SIZE`` tokens with ``DOCUMENT_CHUNK_OVERLAP`` overlap).
* Extraction is DRAFT-ONLY. Nothing in this module ever writes requirements,
  user stories or acceptance criteria; the merged draft is carried in a
  ``pending_action`` and only becomes DB state after explicit user confirmation
  through the existing confirm-action flow.

The gatherer LLM call reuses the same prompt + ``GatheredRequirements`` schema
that ``agents.gatherer_node`` uses, so document extraction stays consistent with
interactive requirement gathering.
"""
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate

from app.config import settings
from app.input_validation import _get_encoder, count_tokens
from app.llm_factory import llm
from app.llm_utils import invoke_llm_structured
from app.merge_service import (
    collect_acceptance_criteria,
    filter_active_stories,
    merge_user_stories,
    normalize_title,
    normalize_ticket_code,
)
from app.prompt_loader import load_prompt
from app.requirement_codes import next_requirement_code
from app.schemas import GatheredRequirements

logger = logging.getLogger("app.document_processor")

# Gatherer prompt input variables mirrored from agents.gatherer_node.
_GATHERER_INPUT_VARIABLES = [
    "raw_input",
    "detected_intent",
    "intent_guidance",
    "current_context",
    "recommendations",
    "format_instructions",
]

# Traceability marker stored inside a draft's ``proposed_changes`` so the
# confirm-action flow can audit-log which document produced the merge.
# DOC-UPLOAD 3.8 — Draft traceability markers, shared between FLOW 15 (which stamps them
#             into ``proposed_changes``) and DOC-UPLOAD 2.4.2 (which matches on them when
#             evicting the drafts of a deleted document). Keep both in sync.
DRAFT_DOCUMENT_REF_KEY = "_document_ref"
DRAFT_ACTION_TYPE = "INSERT_CHUNKED_REQUIREMENTS"


@dataclass
class DocumentRouting:
    """Decision about how a document feeds the LLM (full vs chunked)."""
    mode: str  # 'full' | 'chunked'
    chunk_count: int
    document_budget: int
    chunk_size: int
    overlap: int


def document_budget() -> int:
    """Return the token budget available for a single document extraction pass.

    A fraction of ``MAX_CONTEXT_TOKENS`` so the system prompt + gatherer
    instructions + tool schema + output slack still fit when the chunk joins
    the prompt (Option 2 math — never set the chunk near the full window).
    """
    return max(1, int(settings.MAX_CONTEXT_TOKENS * settings.DOCUMENT_BUDGET_FRACTION))


def decide_document_mode(token_count: int) -> str:
    """Return ``'full'`` (single pass) or ``'chunked'`` (sequential passes).

    The DOCUMENT_BUDGET boundary (``DOCUMENT_BUDGET_FRACTION * MAX_CONTEXT_TOKENS``)
    separates the two modes — a document at or below the budget always runs in
    one gatherer call; above the budget it is split into overlapping chunks.
    """
    return "full" if token_count <= document_budget() else "chunked"


def route_document_tokens(token_count: int) -> DocumentRouting:
    """Compute the routing decision (mode + predicted chunk count) for a document.

    Pure decision helper used by the process route and by tests at the
    DOCUMENT_BUDGET boundary. ``chunk_count`` is the number of gatherer passes
    expected (1 for full mode; for chunked mode a ceiling estimate based on the
    usable window size).
    """
    budget = document_budget()
    mode = decide_document_mode(token_count)
    if mode == "full":
        chunk_count = 1
    else:
        usable = max(1, settings.DOCUMENT_CHUNK_SIZE - settings.DOCUMENT_CHUNK_OVERLAP)
        chunk_count = max(1, (token_count + usable - 1) // usable)
    return DocumentRouting(
        mode=mode,
        chunk_count=chunk_count,
        document_budget=budget,
        chunk_size=settings.DOCUMENT_CHUNK_SIZE,
        overlap=settings.DOCUMENT_CHUNK_OVERLAP,
    )


def _tail_by_tokens(text: str, max_tokens: int) -> str:
    """Return the trailing ``max_tokens`` of ``text`` (word-boundary safe)."""
    if max_tokens <= 0 or not text:
        return ""
    encoder = _get_encoder()
    if encoder is None:
        # Heuristic fallback (chars/4) when tiktoken is unavailable.
        return text[-max_tokens * 4:].lstrip("\n")
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    tail_tokens = tokens[-max_tokens:]
    tail = encoder.decode(tail_tokens)
    # Trim leading whitespace/newlines so the carried-over tail is a clean
    # phrase rather than a fragment of the previous chunk's final word boundary.
    return tail.lstrip("\n \t")


def _split_long_line(line: str, chunk_size: int) -> List[str]:
    """Word-bucket splits a single unbreakable line so each piece fits budget.

    The naive approach re-encodes the whole growing candidate for EVERY word
    (O(n^2) tokenization on unbreakable blobs such as base64), which is a real
    slowdown on large documents.

    Fast path: a tokenizer token is always at least one UTF-8 byte long, so
    ``len(tokens) <= len(bytes)`` always holds. The exact ``count_tokens`` check
    is therefore needed only when a candidate's *byte* length already exceeds the
    budget — otherwise the token count is provably within budget and the check
    can be skipped. Output is byte-for-byte identical to the exact-only version.
    """
    pieces: List[str] = []
    current: List[str] = []
    space_bytes = len(" ".encode("utf-8"))
    current_bytes = 0  # utf-8 byte length of ``" ".join(current)``
    for word in line.split(" "):
        word_bytes = len(word.encode("utf-8"))
        candidate_bytes = current_bytes + word_bytes + (space_bytes if current else 0)
        if current and candidate_bytes > chunk_size:
            if count_tokens(" ".join(current + [word])) > chunk_size:
                pieces.append(" ".join(current))
                current = [word]
                current_bytes = word_bytes
                continue
        current.append(word)
        current_bytes = candidate_bytes
    if current:
        pieces.append(" ".join(current))
    return pieces


def split_markdown_into_chunks(
    text: str,
    chunk_size: Optional[int] = None,
    overlap: Optional[int] = None,
    token_count: Optional[int] = None,
) -> List[str]:
    """Split ``text`` into ordered, token-budgeted chunks with overlap.

    Behavior
    --------
    - A document already within ``chunk_size`` returns a single chunk.
    - Chunks are packed line-by-line so headings/paragraphs are never sliced
      mid-line, and the last ``overlap`` tokens of each chunk are carried into
      the next chunk so boundary context is not clipped.
    - No content is ever dropped: every source line appears in at least one
      chunk (overlap duplicates content — it never removes it).

    Args:
        text: Full converted-markdown document body.
        chunk_size: Target token budget per chunk (defaults to settings).
        overlap: Tokens of the previous chunk repeated at the next chunk start.
        token_count: Precomputed ``count_tokens(text)`` when the caller already
            measured it (routes do), skipping one redundant full-document
            tokenization pass on large documents.

    Returns:
        Ordered list of chunk strings (length >= 1 when ``text`` is non-empty).
    """
    if chunk_size is None:
        chunk_size = settings.DOCUMENT_CHUNK_SIZE
    if overlap is None:
        overlap = settings.DOCUMENT_CHUNK_OVERLAP

    if not text:
        return []
    if (token_count if token_count is not None else count_tokens(text)) <= chunk_size:
        return [text]

    chunks: List[str] = []
    current_lines: List[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current_lines, current_tokens
        if not current_lines:
            return
        chunk_text = "\n".join(current_lines)
        chunks.append(chunk_text)
        tail = _tail_by_tokens(chunk_text, overlap) if overlap > 0 else ""
        current_lines = [tail]
        current_tokens = count_tokens(tail) if tail else 0

    for line in text.split("\n"):
        line_tokens = count_tokens(line)
        if (
            line.strip()
            and current_lines
            and current_tokens > 0
            and current_tokens + line_tokens > chunk_size
        ):
            flush()
        if line_tokens > chunk_size:
            # A single unbreakable line (e.g. a base64 blob) overflows the
            # budget: split it word-wise, flushing between each piece.
            for piece in _split_long_line(line, chunk_size):
                if (
                    current_lines
                    and current_tokens > 0
                    and current_tokens + count_tokens(piece) > chunk_size
                ):
                    flush()
                current_lines.append(piece)
                current_tokens += count_tokens(piece)
        else:
            current_lines.append(line)
            current_tokens += line_tokens

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks


def accumulate_document_draft(chunk_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapse per-chunk ``GatheredRequirements`` outputs into ONE document draft.

    Chunk overlap means the same story can appear in consecutive chunk results
    with the same ticket code or title; duplicates are collapsed here across the
    WHOLE document before anything is committed.

    Returns a dict shaped like ``GatheredRequirements``:
    ``{"epic_name": ..., "requirements": [{requirement_code, title, description,
    user_stories: [...]}]}``.
    """
    seen_codes: set = set()
    seen_titles: set = set()
    req_map: Dict[str, Dict[str, Any]] = {}
    epic_name = ""

    for chunk in chunk_results:
        if not chunk:
            continue
        chunk_epic = (chunk.get("epic_name") or "").strip()
        if chunk_epic:
            epic_name = chunk_epic
        for req in chunk.get("requirements") or []:
            # A chunk that omits the code gets the next code of the sequence
            # being accumulated (REQ-001 for the first group) instead of a
            # hardcoded code that would collide with every other codeless group.
            code = (req.get("requirement_code") or "").strip() or next_requirement_code(req_map)
            if code not in req_map:
                req_map[code] = {
                    "requirement_code": code,
                    "title": (req.get("title") or f"Requirement {code}").strip(),
                    "description": req.get("description") or "",
                    "user_stories": [],
                }
            for story in req.get("user_stories") or []:
                ticket = normalize_ticket_code(story.get("ticket_code"))
                title_key = normalize_title(story.get("story_title"))
                if ticket:
                    if ticket in seen_codes:
                        continue  # duplicate across chunk overlap
                    seen_codes.add(ticket)
                elif title_key:
                    if title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)
                req_map[code]["user_stories"].append(
                    {
                        "ticket_code": ticket or story.get("ticket_code") or "",
                        "story_title": story.get("story_title") or "Untitled Story",
                        "as_a": story.get("as_a") or "",
                        "i_want_to": story.get("i_want_to") or "",
                        "so_that": story.get("so_that") or "",
                        "acceptance_criteria": list(story.get("acceptance_criteria") or []),
                    }
                )

    return {"epic_name": epic_name, "requirements": list(req_map.values())}


def build_merge_preview(
    project_id: str,
    current_state: Optional[Dict[str, Any]],
    doc_draft: Dict[str, Any],
    document_id: str,
    document_filename: str,
) -> Dict[str, Any]:
    """Assemble the SINGLE merged RequirementState draft for the whole document.

    This is the payload stored in ``pending_action.proposed_changes``. It
    preserves every existing requirement and related state (so a confirm never
    archives anything not mentioned) and merges the de-duplicated document
    stories against the current user-story list, collectively revisioned against
    the current requirement state (``requirements_revision + 1``). Purely in-memory —
    nothing here writes to the database.
    """
    current_state = current_state or {}
    existing_requirements = [dict(r) for r in (current_state.get("requirements") or [])]
    existing_stories = [
        dict(s)
        for s in (current_state.get("user_stories") or [])
        if s.get("status", "active") == "active"
    ]

    # Dedup incoming doc stories (flat), then reconcile against existing stories
    # with the same ticket/merge semantics the interactive gatherer uses
    # (renumbering colliding US-xxx codes into unique ones).
    doc_stories: List[Dict[str, Any]] = [
        s for req in doc_draft.get("requirements", []) for s in req.get("user_stories", [])
    ]
    merged_stories = merge_user_stories(
        existing_stories=existing_stories,
        new_incoming_stories=doc_stories,
    )
    active_stories = filter_active_stories(merged_stories)

    # Requirements: every existing requirement keeps its existing active stories;
    # the document's new groups (or the same group when a code collides) receive
    # the freshly extracted stories too.
    existing_by_code: Dict[str, Dict[str, Any]] = {}
    for r in existing_requirements:
        code = (r.get("requirement_code") or "").strip() or next_requirement_code(existing_by_code)
        existing_by_code.setdefault(code, dict(r))
        existing_by_code[code]["user_stories"] = list(
            existing_by_code[code].get("user_stories") or []
        )

    for doc_req in doc_draft.get("requirements", []):
        code = (doc_req.get("requirement_code") or "").strip() or next_requirement_code(existing_by_code)
        target = existing_by_code.get(code)
        if target is None:
            target = {
                "requirement_code": code,
                "title": doc_req.get("title") or f"Requirement {code}",
                "description": doc_req.get("description") or "",
                "user_stories": [],
            }
            existing_by_code[code] = target
        target["user_stories"].extend(doc_req.get("user_stories", []))

    next_revision = max(1, int((current_state.get("requirements_revision") or 1)) + 1)

    draft: Dict[str, Any] = {
        "project_id": project_id,
        "project_name": current_state.get("project_name") or "Default Project",
        "requirements": list(existing_by_code.values()),
        "user_stories": active_stories,
        "acceptance_criteria": collect_acceptance_criteria(active_stories),
        "business_goals": current_state.get("business_goals") or [],
        "actors": current_state.get("actors") or [],
        "clarification_questions": current_state.get("clarification_questions") or [],
        "validation_status": current_state.get("validation_status") or "pending",
        "generated_prd": current_state.get("generated_prd") or "",
        "generated_diagrams": current_state.get("generated_diagrams") or "",
        "current_workflow_state": "gatherer_node",
        "requirements_revision": next_revision,
    }
    if document_id:
        draft[DRAFT_DOCUMENT_REF_KEY] = {
            "document_id": document_id,
            "document_filename": document_filename,
        }
    return draft


async def extract_requirements_from_text(
    chunk_text: str,
    *,
    chunk_index: int = 1,
    chunk_count: int = 1,
) -> Dict[str, Any]:
    """Run the gatherer over one document chunk, returning ``GatheredRequirements``.

    Mirrors ``agents.gatherer_node``'s LLM invocation (same prompt file, same
    ``GatheredRequirements`` schema, same ``with_structured_output`` strategy)
    so document-driven extraction stays consistent with interactive gathering.
    """
    prompt_template = PromptTemplate(
        template=load_prompt("gatherer"),
        input_variables=_GATHERER_INPUT_VARIABLES,
    )
    parser = PydanticOutputParser(pydantic_object=GatheredRequirements)
    format_instructions = parser.get_format_instructions()
    logger.info(
        "Gatherer pass %d/%d: chunk length=%d chars.",
        chunk_index,
        chunk_count,
        len(chunk_text),
    )
    result = await invoke_llm_structured(
        llm,
        prompt_template,
        GatheredRequirements,
        variables={
            "raw_input": chunk_text,
            "detected_intent": "REQUIREMENT_REQUEST",
            "intent_guidance": (
                "Document upload mode: extract ALL requirements present in the "
                "document text as fresh user stories; there are no existing "
                "stories to preserve."
            ),
            # Document processing starts from the document alone; the current
            # requirement snapshot is merged afterwards, not fed into the LLM.
            "current_context": "No existing user stories in this project.",
            "recommendations": "[]",
            "format_instructions": "Output ONLY raw JSON. No markdown.",
        },
        description=f"document chunk {chunk_index}/{chunk_count}",
        format_instructions=format_instructions,
    )
    return result

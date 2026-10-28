# DOC-UPLOAD 4.0 — Document HTTP boundary. Upload = KNOWLEDGE ONLY.
#             Canonical converted markdown is stored without in-place content updates; the size
#             gate at save time is domain 6.1's MAX_UPLOAD_MB (disk abuse), NOT a token
#             check. Explicit extraction (4.5) updates tracking/transcript state and stages
#             a pending action; only CONFIRM applies its proposed requirement fields.
#             Auth on every endpoint below: AUTH 7.5 (project ownership).
"""
Document upload & extraction API.

Product intent enforced here (see also ``app/document_processor.py``):

1. **Knowledge base (RAG-ready).** Uploading a document ONLY adds source material
   to the project's knowledge store. The FULL converted markdown (plus metadata)
   is always persisted immutably — no token truncation at save time. The only
   size gate at save time is ``MAX_UPLOAD_MB`` (disk abuse), NOT a token check.
2. **Requirements source (opt-in, explicit).** Extraction happens only on the
   explicit ``POST .../documents/{id}/process`` action, produces a DRAFT merge
   preview stored in a ``pending_action``, and is NEVER written until the user
   confirms through the existing ``/api/confirm-action/{id}`` flow. Neither the
   upload nor the processing endpoints ever mutate requirements, user stories
   or acceptance criteria.

Alembic migration ``0004`` provisions the table; ``DocumentRepository`` owns
persistence; serialization lives in ``repositories/base.py``.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, require_project_owner
from app.config import settings
from app.database import get_db
from app.document_processor import (
    DRAFT_ACTION_TYPE,
    accumulate_document_draft,
    build_merge_preview,
    decide_document_mode,
    document_budget,
    extract_requirements_from_text,
    prepare_document_for_extraction,
    split_markdown_into_chunks,
)
from app.event_manager import event_manager
from app.input_validation import count_tokens
from app.rate_limit import rate_limit_dependency
from app.repositories import (
    ConversationMessageRepository,
    DocumentRepository,
    PendingActionRepository,
    RequirementStateRepository,
)
from app.schemas import (
    DocumentDeleteResponse,
    DocumentMarkdownResponse,
    DocumentProcessResponse,
    UploadedDocumentResponse,
)
from app.services.document_ingestion_service import (
    NEEDS_OCR_MESSAGE,
    DocumentIngestionService,
)
from app.workflow_cancellation import document_extraction_cancellations

logger = logging.getLogger("app.routes.documents")

router = APIRouter()


def _extraction_cancellation_key(project_id: str, document_id: str) -> str:
    return f"{project_id}:{document_id}"


async def _persist_conversion_message(
    project_id: str,
    message: str,
    *,
    status_value: str,
    session: AsyncSession,
) -> None:
    """DOC-UPLOAD 5.1 — Commit a document-conversion transcript milestone.

    Each milestone is committed immediately. A failed upload raises
    ``HTTPException``, which rolls back the request's current transaction; the
    explicit commit ensures prior conversion messages remain permanent.
    """
    await ConversationMessageRepository.save_message(
        project_id=project_id,
        role="assistant",
        message=message,
        workflow_state="document_conversion",
        intent=f"DOCUMENT_CONVERSION_{status_value.upper()}",
        session=session,
    )
    await session.commit()
    try:
        await event_manager.publish(
            project_id,
            "chat_reply",
            {
                "project_id": project_id,
                "intent": f"DOCUMENT_CONVERSION_{status_value.upper()}",
            },
        )
    except Exception:
        # Realtime delivery is only a refresh hint. The committed transcript is
        # authoritative and must not be reported as a failed conversion merely
        # because an SSE subscriber disappeared.
        logger.exception("Failed to publish conversion chat refresh for %s", project_id)


async def _persist_extraction_message(
    project_id: str,
    role: str,
    message: str,
    *,
    intent: str,
    session: AsyncSession,
) -> None:
    """DOC-UPLOAD 5.3 — Commit an extraction transcript milestone.

    The explicit commit also commits other changes already flushed in the same
    session—for example extraction status and a newly created pending action.
    """
    await ConversationMessageRepository.save_message(
        project_id=project_id,
        role=role,
        message=message,
        workflow_state="gatherer_node",
        intent=intent,
        session=session,
    )
    await session.commit()

# ==========================================
# ALLOWED FORMATS / MIME VALIDATION
# ==========================================
# DOC-UPLOAD 6.1.1 — The upload allow-list (DOC-UPLOAD 1.1's `accept` mirrors it):
#             only these four formats can enter the knowledge base, each with the MIME
#             types accepted for it. Anything else is a 400.
# Generic content-types are tolerated for supported extensions (curl uploads,
#             drag&drop, browsers that send octet-stream) once the extension itself has
#             validated.
# OCR is explicitly OUT OF SCOPE. A PDF with no extractable text is
#             surfaced with this clear message and, uniquely for this error, persisted as a
#             traceable `failed` record rather than aborting the upload.
# Extension + MIME validation: extension decides the
#             format; a declared MIME that contradicts the extension is a 400 (an empty or
#             generic MIME is tolerated). Returns the descriptor used for
#             conversion and for the stored `original_format`/`mime_type` columns.
def _resolve_upload_format(filename: str, content_type: str) -> Dict:
    """Validate extension + MIME and return the format descriptor.

    Raises:
        HTTPException: 400 for an unsupported extension or a mismatched MIME
            type.
    """
    return DocumentIngestionService.resolve_upload_format(filename, content_type)


def _validate_binary_signature(raw: bytes, original_format: str) -> None:
    """Reject binary uploads whose contents contradict their claimed format.

    This complements extension/MIME checks without changing the permissive
    UTF-8 handling used for Markdown and plain text. Full parsing remains the
    authoritative structural validation performed by the existing converters.
    """
    DocumentIngestionService.validate_binary_signature(raw, original_format)


# DOC-UPLOAD 6.1.3 — Streaming size cap: reads the upload in 1 MB slices and
#             aborts with 413 as soon as MAX_UPLOAD_MB is exceeded, so an oversized body is
#             never fully buffered. This is the ONLY size check at save time — it guards
#             disk abuse, NOT tokens (token_count is metadata and the full
#             markdown is stored regardless of size).
async def _read_upload_with_size_cap(file: UploadFile) -> bytes:
    """Read the whole upload, rejecting anything over ``MAX_UPLOAD_MB`` early.

    This is the ONLY size check applied at save time — it guards against disk
    abuse, NOT tokens. ``token_count`` is measured on the converted markdown and
    the full content is always persisted regardless of token size.
    """
    return await DocumentIngestionService.read_upload_with_size_cap(file)


# DOC-UPLOAD 6.1.4 — Canonical conversion, run off the event loop by route 4.1:
#               md/txt → UTF-8 passthrough (errors replaced, never raised);
#               docx   → mammoth → markdown (501 when mammoth is missing);
#               pdf    → pypdf per-page text join → 422 needs-OCR when nothing is
#                        extractable, 422 "could not read" on a parser failure.
def _convert_to_markdown(raw: bytes, original_format: str) -> str:
    """Convert an uploaded byte stream into canonical markdown server-side.

    - docx -> mammoth (best-maintained converter; ``convert_to_markdown``).
    - pdf  -> pypdf text extraction; scanned/no-text PDFs raise an explicit,
      clear 'needs-OCR' error (OCR is out of scope — never a silent stub).
    - md/txt -> passthrough as-is.
    """
    return DocumentIngestionService.convert_to_markdown(raw, original_format)


# ==========================================
# UPLOAD — KNOWLEDGE ONLY (never writes requirements)
# ==========================================

# DOC-UPLOAD 4.1 — POST upload (frontend 1.1 → hook 2.3 → client 3.1).
# Trigger: user selects a file. Input: multipart DOCX/PDF/Markdown/text.
# Calls: domain validation/conversion 6.1 → repository create 7.1 → milestone 5.1.
# Branches: invalid/oversized input fails; OCR-only PDF persists a failed row; other
# conversion failures create no row. Upload never changes requirement fields.
# Conversion runs OFF THE EVENT LOOP (asyncio.to_thread): PDF/DOCX
#                     parsing is CPU-heavy and previously froze every concurrent request
#                     (SSE heartbeats, chat, other uploads)
# Writes: canonical row + terminal transcript, then event 8.1. Token count is metadata,
# never a save gate. Returns 201 even for the traceable OCR-only failed row.
@router.post(
    "/api/project/{project_id}/documents/upload",
    response_model=UploadedDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    project_id: str,
    file: UploadFile = File(..., description="The document file (.docx/.pdf/.md/.txt)."),
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Upload a document into the project's knowledge base.

    This endpoint ONLY stores converted markdown as source material. It MUST NOT
    create any requirements, user stories, acceptance criteria, or pending
    actions for them. Extraction is a separate, explicitly-triggered action
    (``POST .../documents/{document_id}/process``).

    Args:
        project_id: Owning project UUID string.
        file: Multipart ``UploadFile`` (docx/pdf/md/txt).
        session: Active asynchronous database session.

    Returns:
        UploadedDocumentResponse: The persisted document record (status
            'processed' on success; a scanned-PDF failure returns status
            'failed' with a clear 'needs-OCR' message in ``result.message``).

    Raises:
        HTTPException: 400 bad extension/MIME/empty content, 413 oversized file,
            422 unreadable DOCX/PDF, or 501 missing converter. OCR-only PDFs are
            the exception: they return 201 with a persisted status=failed row.
    """
    # DOC-UPLOAD 4.1.1 — Identity gate: invalid project_id ⇒ 400 (ownership is already
    #                 enforced by AUTH 7.5 before the handler body runs).
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    filename = (file.filename or "unnamed").strip()
    # DOC-UPLOAD 4.1.2 — Domain validation 6.1: format/MIME, byte cap, then signature.
    try:
        fmt = _resolve_upload_format(file.filename or "", file.content_type or "")
        raw = await _read_upload_with_size_cap(file)
        _validate_binary_signature(raw, fmt["original_format"])
    except HTTPException as exc:
        await _persist_conversion_message(
            project_id,
            f"Document conversion failed: {filename}\nStatus: failed ({exc.status_code})\nError: {exc.detail}",
            status_value="failed",
            session=session,
        )
        raise

    status_value = "processed"
    error_message = None
    # CPU-heavy PDF/DOCX parsing (pypdf page extraction / mammoth XML walking)
    # plus the full-document tiktoken BPE count are blocking, potentially
    # multi-second operations for large uploads. Running them directly on the
    # event loop froze EVERY concurrent request (SSE heartbeats, chat, other
    # uploads) for the whole conversion, so they are offloaded to the default
    # thread pool like the LaTeX export pipeline already does.
    # DOC-UPLOAD 4.1.3 — Off-loop conversion; the needs-OCR branch below is the
    #                 ONLY conversion failure that still persists a record.
    try:
        markdown = await asyncio.to_thread(_convert_to_markdown, raw, fmt["original_format"])
    except HTTPException as exc:
        # A scanned/no-text PDF keeps a traceable 'failed' record and surfaces an
        # explicit, non-silent 'needs-OCR' message. Other conversion errors create
        # no document row, but their durable failure transcript was committed above.
        if (
            exc.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
            and exc.detail == NEEDS_OCR_MESSAGE
        ):
            status_value = "failed"
            error_message = exc.detail
            markdown = ""
        else:
            await _persist_conversion_message(
                project_id,
                f"Document conversion failed: {filename}\nStatus: failed ({exc.status_code})\nError: {exc.detail}",
                status_value="failed",
                session=session,
            )
            raise
    except Exception as exc:
        logger.exception("Unexpected document conversion failure for %s", filename)
        await _persist_conversion_message(
            project_id,
            f"Document conversion failed: {filename}\nStatus: failed (500)\nError: {str(exc) or type(exc).__name__}",
            status_value="failed",
            session=session,
        )
        raise

    # DOC-UPLOAD 4.1.4 — Token count off-thread (tiktoken BPE over the whole document):
    #                 measured for display/budget purposes only — it never truncates or
    #                 blocks the save.
    try:
        token_count = await asyncio.to_thread(count_tokens, markdown)
        # DOC-UPLOAD 4.1.5 — Persist the canonical knowledge record (7.1): full Markdown,
        #                 size, tokens, status, and extraction_status 'not_extracted'.
        doc = await DocumentRepository.create(
            str(project_id),
            {
                "original_filename": filename,
                "original_format": fmt["original_format"],
                "mime_type": fmt["mime_type"],
                "content_markdown": markdown,
                "file_size_bytes": len(raw),
                "token_count": token_count,
                "status": status_value,
                "uploaded_by": "user",
                "extraction_status": "not_extracted",
            },
            session,
        )
    except Exception as exc:
        await session.rollback()
        await _persist_conversion_message(
            project_id,
            f"Document conversion failed: {filename}\nStatus: failed (500)\nError: {str(exc) or type(exc).__name__}",
            status_value="failed",
            session=session,
        )
        raise

    # Commit the document and its terminal transcript together. The chat_reply
    # notification inside 5.1 is best-effort; the later document_uploaded event is not.
    if error_message:
        doc["message"] = error_message  # surfaced via extra=allow on the schema
        await _persist_conversion_message(
            project_id,
            f"Document conversion failed: {filename}\nStatus: failed\nError: {error_message}",
            status_value="failed",
            session=session,
        )
    else:
        await _persist_conversion_message(
            project_id,
            f"File uploaded: {filename}",
            status_value="processed",
            session=session,
        )

    # DOC-UPLOAD 8.1 — Publish document_uploaded after 5.1 committed the row/message.
    #                 Unlike the chat_reply hint inside 2.0.1, this publish is not caught;
    #                 a delivery failure can surface after the upload is already durable.
    await event_manager.publish(
        str(project_id),
        "document_uploaded",
        {
            "project_id": str(project_id),
            "document_id": doc["id"],
            "original_filename": doc["original_filename"],
            "status": doc["status"],
        },
    )

    logger.info(
        "Document uploaded for project %s: %s (%s, %d bytes, %d tokens) -> status=%s",
        project_id,
        doc["original_filename"],
        fmt["original_format"],
        len(raw),
        token_count,
        status_value,
    )
    return doc
# ==========================================
# LIST + DETAIL — READ-ONLY KNOWLEDGE ACCESS
# ==========================================

# DOC-UPLOAD 4.2 — GET list: metadata only, newest first (7.2), no Markdown body.
@router.get(
    "/api/project/{project_id}/documents",
    response_model=List[UploadedDocumentResponse],
    status_code=status.HTTP_200_OK,
)
async def list_documents(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List every uploaded document for a project (metadata only, newest first).

    Args:
        project_id: Owning project UUID string.
        session: Active asynchronous database session.

    Returns:
        List[UploadedDocumentResponse]: Document records (without the full
            markdown body; use the detail endpoint to fetch it).
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    return await DocumentRepository.get_by_project(project_id, session)


# DOC-UPLOAD 4.3 — GET detail: full canonical Markdown (7.3) for UI preview 1.3.
# Never truncated; token budgets apply only to extraction 4.5/6.x.
@router.get(
    "/api/project/{project_id}/documents/{document_id}",
    response_model=DocumentMarkdownResponse,
    status_code=status.HTTP_200_OK,
)
async def get_document_markdown(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Return the FULL canonical markdown for a document.

    The full converted markdown is always persisted, so this response is never
    truncated — token budgets apply only to the LLM-feeding path.

    Args:
        project_id: Owning project UUID string.
        document_id: Document UUID string.
        session: Active asynchronous database session.

    Raises:
        HTTPException: 400 on invalid UUIDs; 404 if the document does not exist.
    """
    try:
        UUID(project_id)
        UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or document_id format")

    doc = await DocumentRepository.get_by_id(document_id, project_id, session)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


# ==========================================
# DELETE — REMOVE A DOCUMENT FROM THE KNOWLEDGE BASE
# ==========================================

# DOC-UPLOAD 4.4 — DELETE document (UI 1.5 → hook 2.6 → client 3.6).
# Calls: hard-delete repository 7.4, then application cleanup 5.2 discards every
# DRAFT staged from THIS document: a DRAFT_ACTION_TYPE pending action whose document
# reference matches, so ConfirmationPanel cannot offer a draft whose source is gone.
# Writes: row/draft deletion plus document_deleted event 8.2. Requirements already
# confirmed from this document remain in the requirement tables / PRD ledger.
@router.delete(
    "/api/project/{project_id}/documents/{document_id}",
    response_model=DocumentDeleteResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_document(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Permanently remove an uploaded document from the project's knowledge base.

    Deletes the document record (including its canonical markdown).
    Any DRAFT extraction merge-preview staged from this document (the
    ``INSERT_CHUNKED_REQUIREMENTS`` pending_action whose
    ``proposed_changes._document_ref.document_id`` matches) is discarded too, so
    the ConfirmationPanel never offers a draft whose source is gone. Requirements
    that were ALREADY extracted and confirmed from this document are NOT touched
    — they live in the requirement tables and the PRD version ledger.

    Args:
        project_id: Owning project UUID string.
        document_id: Document UUID string.
        session: Active asynchronous database session.

    Returns:
        DocumentDeleteResponse: Confirmation payload with the removed file name.

    Raises:
        HTTPException: 400 on invalid UUIDs; 404 if the document does not exist.
    """
    try:
        UUID(project_id)
        UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or document_id format")

    # DOC-UPLOAD 4.4.1 — Hard-delete scoped to the project (7.4) → 404 when absent.
    doc = await DocumentRepository.delete(str(document_id), str(project_id), session)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # DOC-UPLOAD 5.2 — Draft eviction: drop every CURRENT merge-preview staged from this
    #                 document (and nothing else), so the confirmation UI never references
    #                 a deleted source. Only WAITING_CONFIRMATION actions are returned by
    #                 get_by_project and these expire within the hour anyway — this just
    #                 prevents stale drafts. This route does not cancel an extraction
    #                 already in flight; its later completion is a separate race.
    pending = await PendingActionRepository.get_by_project(str(project_id), session)
    removed_drafts = 0
    for action in pending:
        document_ref = (action.get("proposed_changes") or {}).get("_document_ref") or {}
        if (
            action.get("action_type") == DRAFT_ACTION_TYPE
            and document_ref.get("document_id") == str(document_id)
        ):
            await PendingActionRepository.delete(action["id"], str(project_id), session)
            removed_drafts += 1

    # DOC-UPLOAD 8.2 — Publish document_deleted before the request-level DB commit.
    #                 A publication failure therefore rolls back row/draft deletion.
    await event_manager.publish(
        str(project_id),
        "document_deleted",
        {
            "project_id": str(project_id),
            "document_id": str(document_id),
            "original_filename": doc.get("original_filename"),
        },
    )

    logger.info(
        "Document %s ('%s') deleted for project %s (discarded %d draft(s)).",
        document_id,
        doc.get("original_filename"),
        project_id,
        removed_drafts,
    )

    return {
        "status": "deleted",
        "document_id": str(document_id),
        "project_id": str(project_id),
        "original_filename": doc.get("original_filename") or "",
    }


# ==========================================
# PROCESS — EXPLICIT, DRAFT-ONLY REQUIREMENTS EXTRACTION
# ==========================================

async def _run_extraction_by_chunks(
    project_id: str,
    document_id: str,
    content_markdown: str,
    token_count: int,
) -> Dict[str, Any]:
    """DOC-UPLOAD 5.4 — Feed extraction-safe text through the Gatherer.

    Mode routing:
      - ``token_count <= DOCUMENT_BUDGET``   -> ONE gatherer pass over the full doc.
      - otherwise                            -> chunked passes of ``~DOCUMENT_CHUNK_SIZE``
        tokens with ``DOCUMENT_CHUNK_OVERLAP`` overlap. Passes run sequentially by
        default (``DOCUMENT_EXTRACTION_CONCURRENCY=1``) or with bounded parallelism
        when that setting is raised; per-chunk progress is streamed over the SSE
        event bus using the cumulative INPUT token count.

    The extracted artifacts stay in memory only (``chunk_results``); NOTHING is
    written to requirements/user_stories/acceptance_criteria until the user
    confirms the resulting pending_action.

    Returns:
        Dict with ``mode``, ``chunks`` list of ``GatheredRequirements`` dicts,
        ``chunk_count`` and ``processed_tokens``.
    """
    mode = decide_document_mode(token_count)
    if mode == "full":
        chunks = [content_markdown]
        chunk_input_tokens = [token_count]
    else:
        # Pass the already-measured token_count so the chunker does not re-tokenize
        # the whole document a second time (a real cost on large briefs).
        chunks = split_markdown_into_chunks(content_markdown, token_count=token_count)
        if len(chunks) > settings.MAX_DOCUMENT_CHUNKS:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    f"This document requires {len(chunks)} extraction chunks "
                    f"(~{token_count} tokens), exceeding the limit of "
                    f"{settings.MAX_DOCUMENT_CHUNKS}. Split the document into "
                    "smaller files and upload them separately."
                ),
            )
        # Measure each chunk's input tokens ONCE so the progress stream (and the
        # dedup step) never re-encodes outputs.
        chunk_input_tokens = [count_tokens(chunk) for chunk in chunks]

    chunk_count = len(chunks)
    concurrency = max(1, int(settings.DOCUMENT_EXTRACTION_CONCURRENCY))
    logger.info(
        "Document %s extraction mode=%s chunks=%d tokens=%d (budget=%d, concurrency=%d).",
        document_id,
        mode,
        chunk_count,
        token_count,
        document_budget(),
        concurrency,
    )

    processed_input_tokens = 0

    async def _extract_chunk(idx: int) -> Dict[str, Any]:
        """Run ONE gatherer pass; failures are loud and order-independent."""
        nonlocal processed_input_tokens
        chunk = chunks[idx]
        try:
            result = await extract_requirements_from_text(
                chunk, chunk_index=idx + 1, chunk_count=chunk_count
            )
        except Exception as exc:
            logger.error(
                "Document %s chunk %d/%d extraction failed: %s",
                document_id,
                idx + 1,
                chunk_count,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Extraction failed on chunk {idx + 1}/{chunk_count}. No changes "
                    f"were written. ({exc})"
                ),
            ) from exc

        # Monotonic cumulative INPUT tokens (previously re-encoded the gatherer
        # OUTPUT dicts, which count_tokens cannot tokenize -> a meaningless value).
        processed_input_tokens += chunk_input_tokens[idx]
        await event_manager.publish(
            project_id,
            "document_extraction_progress",
            {
                "project_id": project_id,
                "document_id": document_id,
                "mode": mode,
                "chunk_index": idx + 1,
                "chunk_count": chunk_count,
                "processed_tokens": processed_input_tokens,
            },
        )
        return result

    if mode == "full" or concurrency == 1:
        chunk_results: List[Dict[str, Any]] = []
        for idx in range(chunk_count):
            chunk_results.append(await _extract_chunk(idx))
    else:
        # Bounded parallel passes via a shared semaphore. asyncio.gather keeps the
        # result list in DOCUMENT order regardless of completion order, so the
        # accumulate/dedupe step is byte-for-byte identical to sequential mode.
        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(idx: int) -> Dict[str, Any]:
            async with semaphore:
                return await _extract_chunk(idx)

        chunk_results = list(await asyncio.gather(*[_bounded(i) for i in range(chunk_count)]))

    return {
        "mode": mode,
        "chunks": chunk_results,
        "chunk_count": chunk_count,
        "processed_tokens": token_count,
    }


@router.post(
    "/api/project/{project_id}/documents/{document_id}/process",
    response_model=DocumentProcessResponse,
    status_code=status.HTTP_200_OK,
)
async def process_document(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW
        )
    ),
) -> Dict[str, Any]:
    """DOC-UPLOAD 4.5 — Extract requirements from an uploaded document as a draft.

    This is the explicit, user-triggered extraction action. It NEVER writes
    requirements, user stories, or acceptance criteria directly. Instead it
    accumulates every chunk's gatherer output in memory, de-duplicates it across
    the whole document, builds a SINGLE merged merge preview against the current
    requirement state, and stores that run's draft in one pending_action (action_type
    ``INSERT_CHUNKED_REQUIREMENTS``). The draft is versioned
    (``requirements_revision + 1``) so confirming via ``/api/confirm-action/{id}``
    applies it as a single atomic merge without creating a PRD version. This route
    does not supersede an older extraction draft from the same document.

    Args:
        project_id: Owning project UUID string.
        document_id: Document UUID string.
        session: Active asynchronous database session.
        _rate_limit: Injected rate limiter (Finding #39 — this endpoint fans out
            into up to MAX_DOCUMENT_CHUNKS sequential LLM calls).

    Returns:
        DocumentProcessResponse: status/mode/chunk stats + the pending_action id
            the UI can offer the user to Confirm or Cancel.

    Raises:
        HTTPException: 400 invalid UUID or failed (OCR) document; 404 unknown
            document; 413 document too large to extract; 502 gatherer failure.
    """
    # DOC-UPLOAD 4.5.1 — Validate ids, load the project-scoped canonical document,
    #                   and reject failed conversions before any extraction milestone.
    try:
        UUID(project_id)
        UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or document_id format")

    doc = await DocumentRepository.get_by_id(document_id, project_id, session)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.get("status") != "processed":
        raise HTTPException(
            status_code=400,
            detail=(
                "This document was not processed successfully. Extraction is "
                "only available for successfully converted documents."
            ),
        )

    # DOC-UPLOAD 5.3.1 — Build an extraction-only copy (6.2 removes embedded image
    #                   bytes without changing stored markdown), count its tokens,
    #                   commit the request transcript, then mark tracking pending.
    content_markdown = doc.get("content_markdown") or ""
    extraction_markdown = prepare_document_for_extraction(content_markdown)
    token_count = count_tokens(extraction_markdown)
    filename = doc.get("original_filename") or document_id

    await _persist_extraction_message(
        project_id,
        "user",
        f'Extract requirements from attached document "{filename}".',
        intent="DOCUMENT_REQUIREMENTS_REQUEST",
        session=session,
    )

    # Explicit, loud tracking flag: the user has triggered extraction.
    await DocumentRepository.update_extraction_status(
        document_id, project_id, "extraction_pending", session
    )
    # The started event is a pre-commit progress hint; `extraction_pending` is flushed
    # but not committed until a later 5.3 milestone succeeds. A subscriber that
    # immediately reloads may therefore still observe the previous status.
    await event_manager.publish(
        project_id,
        "document_extraction_started",
        {
            "project_id": project_id,
            "document_id": document_id,
            "token_count": token_count,
            "budget": document_budget(),
        },
    )

    cancellation_key = _extraction_cancellation_key(project_id, document_id)
    current_task = asyncio.current_task()
    if current_task is not None:
        # DOC-UPLOAD extraction run → JOB 5.6 registration. The registry is
        # process-local; durable document status remains owned by this request.
        document_extraction_cancellations.register(cancellation_key, current_task)

    # DOC-UPLOAD 5.4 — Run full/chunked Gatherer passes and emit progress hints (8.4).
    try:
        run = await _run_extraction_by_chunks(
            project_id, document_id, extraction_markdown, token_count
        )
    # DOC-UPLOAD 5.5 — Cancellation/failure cleanup: discard partial in-memory output,
    #                   restore `not_extracted`, commit a transcript milestone, publish
    #                   the terminal event, and return cancelled or re-raise failure.
    except asyncio.CancelledError:
        logger.info(
            "Document %s extraction stopped by user; discarding partial results.",
            document_id,
        )
        await session.rollback()
        await DocumentRepository.update_extraction_status(
            document_id, project_id, "not_extracted", session
        )
        await _persist_extraction_message(
            project_id,
            "assistant",
            f'Document requirement extraction stopped for "{filename}". No changes were written.',
            intent="DOCUMENT_REQUIREMENTS_CANCELLED",
            session=session,
        )
        await event_manager.publish(
            project_id,
            "document_extraction_cancelled",
            {"project_id": project_id, "document_id": document_id},
        )
        return {
            "status": "cancelled",
            "mode": "cancelled",
            "chunk_count": 0,
            "processed_tokens": 0,
            "pending_action_id": None,
            "excluded_anything": False,
            "message": "Extraction stopped. No changes were written.",
            "document_id": document_id,
        }
    except Exception:
        # A failed/oversized LLM run must not leave the row looking as though a
        # reviewable draft exists. No pending action has been created yet, so
        # returning to knowledge-only is the truthful, retryable state.
        await DocumentRepository.update_extraction_status(
            document_id, project_id, "not_extracted", session
        )
        await _persist_extraction_message(
            project_id,
            "assistant",
            f'Document requirement extraction failed for "{filename}". No changes were written.',
            intent="DOCUMENT_REQUIREMENTS_FAILED",
            session=session,
        )
        # Persist before notifying clients: the SSE handler refreshes the
        # transcript and must always be able to observe the canonical message.
        await event_manager.publish(
            project_id,
            "document_extraction_failed",
            {"project_id": project_id, "document_id": document_id},
        )
        raise
    finally:
        if current_task is not None:
            # DOC-UPLOAD 8.3 / JOB 5.6 — Remove only this request's registration;
            # a replacement task under the same key must remain cancellable.
            document_extraction_cancellations.unregister(cancellation_key, current_task)

    # DOC-UPLOAD 5.6 — Collapse chunk output (6.5), build one revision-guarded merged
    #                   proposal (6.6), create one pending action for THIS run (without
    #                   deduplicating older drafts), then commit it together
    #                   with `extraction_pending` and the draft-ready transcript.
    doc_draft = accumulate_document_draft(run["chunks"])
    current_state = await RequirementStateRepository.get_by_project_id(project_id, session)

    preview = build_merge_preview(
        project_id,
        current_state,
        doc_draft,
        document_id,
        doc.get("original_filename") or "",
    )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    pending_action = await PendingActionRepository.create(
        project_id,
        {
            "action_type": DRAFT_ACTION_TYPE,
            "target_requirement_id": None,
            "original_user_message": (
                f"Extract requirements from uploaded document "
                f"'{doc.get('original_filename') or document_id}'."
            ),
            "proposed_changes": preview,
            "affected_user_story_ids": [],
            "affected_acceptance_criteria_ids": [],
            "workflow_stage": "gatherer_node",
        },
        expires_at=expires_at,
        session=session,
    )

    await _persist_extraction_message(
        project_id,
        "gatherer",
        (
            f'Merge preview ready. Requirements were gathered from the full content of '
            f'"{filename}" using {run["mode"]} mode ({run["chunk_count"]} chunk(s)). '
            "Nothing has been written yet; review the proposed changes and select Save or Cancel."
        ),
        intent="DOCUMENT_REQUIREMENTS_DRAFT_READY",
        session=session,
    )

    # DOC-UPLOAD 8.5 — After the draft commit, publish its id/stats for client refresh
    #                   and return the same handoff metadata to hook 2.4. This
    #                   publish is not caught, so delivery failure may surface even
    #                   though the pending action/status/transcript are already durable.
    await event_manager.publish(
        project_id,
        "document_extraction_draft",
        {
            "project_id": project_id,
            "document_id": document_id,
            "pending_action_id": pending_action["id"],
            "mode": run["mode"],
            "chunk_count": run["chunk_count"],
            "processed_tokens": run["processed_tokens"],
        },
    )

    logger.info(
        "Document %s processed (%s mode, %d chunks, %d tokens) -> draft pending_action %s.",
        document_id,
        run["mode"],
        run["chunk_count"],
        run["processed_tokens"],
        pending_action["id"],
    )

    return {
        "status": "draft_ready",
        "mode": run["mode"],
        "chunk_count": run["chunk_count"],
        "processed_tokens": run["processed_tokens"],
        "pending_action_id": pending_action["id"],
        "excluded_anything": False,
        "document_id": document_id,
        "message": (
            f"Extraction draft ready ({run['mode']} mode, {run['chunk_count']} "
            f"chunk(s), {run['processed_tokens']} tokens processed). Nothing has "
            "been written. Review the merge preview and confirm to apply."
        ),
    }


@router.post(
    "/api/project/{project_id}/documents/{document_id}/process/cancel",
    status_code=status.HTTP_200_OK,
)
async def cancel_document_extraction(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
) -> Dict[str, Any]:
    """DOC-UPLOAD 4.6 / JOB 4.6 — Signal a registered extraction task to stop.

    This endpoint changes no document row itself. The cancelled 4.5 request catches
    CancelledError, resets status, records the transcript, and publishes the event.
    `cancelled=false` means no live task matched the project/document key.
    """
    try:
        UUID(project_id)
        UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or document_id format")

    # JOB 5.6 — Cancel the process-local extraction task. The running DOC-UPLOAD
    # 4.5 request catches CancelledError and performs every durable cleanup write.
    cancelled = document_extraction_cancellations.cancel(
        _extraction_cancellation_key(project_id, document_id)
    )
    return {
        "project_id": project_id,
        "document_id": document_id,
        "cancelled": cancelled,
    }

# DOC-UPLOAD 2.0 — Module contract: upload = KNOWLEDGE ONLY (never writes requirements).
#             The whole converted markdown is always persisted immutably; the only size
#             gate at save time is config 3.5's MAX_UPLOAD_MB (disk abuse), NOT a token
#             check. Extraction is the separate explicit action (FLOW 15, 2.x `/process`),
#             which stages a DRAFT pending_action and still writes nothing until CONFIRM.
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

logger = logging.getLogger("app.routes.documents")

router = APIRouter()


async def _persist_conversion_message(
    project_id: str,
    message: str,
    *,
    status_value: str,
    session: AsyncSession,
) -> None:
    """Append a durable document-conversion turn to the project transcript.

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
    """Persist a document extraction turn as part of the normal chat timeline."""
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
# DOC-UPLOAD 3.1 — The upload allow-list (DOC-UPLOAD 1.1's `accept` attribute mirrors it):
#             only these four formats can enter the knowledge base, each with the MIME
#             types accepted for it. Anything else is a 400 at 3.4.
# DOC-UPLOAD 3.2 — Generic content-types tolerated for ANY supported format (curl uploads,
#             drag&drop, browsers that send octet-stream) once the extension itself has
#             validated in 3.4.
# DOC-UPLOAD 3.3 — OCR is explicitly OUT OF SCOPE. A PDF with no extractable text is
#             surfaced with this clear message and, uniquely for this error, persisted as a
#             traceable `failed` record (2.1.5) rather than aborting the upload.
# DOC-UPLOAD 3.4 — Extension + MIME validation (step 2.1.2). Extension decides the
#             format; a declared MIME that contradicts the extension is a 400 (an empty or
#             generic MIME is tolerated per 3.2). Returns the descriptor used for
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


# DOC-UPLOAD 3.5 — Streaming size cap (step 2.1.3): reads the upload in 1 MB slices and
#             aborts with 413 as soon as MAX_UPLOAD_MB is exceeded, so an oversized body is
#             never fully buffered. This is the ONLY size check at save time — it guards
#             disk abuse, NOT tokens (token_count is measured in 2.1.6 and the full
#             markdown is stored regardless of size).
async def _read_upload_with_size_cap(file: UploadFile) -> bytes:
    """Read the whole upload, rejecting anything over ``MAX_UPLOAD_MB`` early.

    This is the ONLY size check applied at save time — it guards against disk
    abuse, NOT tokens. ``token_count`` is measured on the converted markdown and
    the full content is always persisted regardless of token size.
    """
    return await DocumentIngestionService.read_upload_with_size_cap(file)


# DOC-UPLOAD 3.6 — Canonical conversion, run off the event loop by 2.1.4:
#               md/txt → UTF-8 passthrough (errors replaced, never raised);
#               docx   → mammoth → markdown (501 when mammoth is missing);
#               pdf    → pypdf per-page text join → 422 needs-OCR (3.3) when nothing is
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

# DOC-UPLOAD 2.1 — WRITE: POST upload (frontend 1.2). Ordered path:
#               2.1.1 project_id UUID check → 400
#               2.1.2 format/MIME validation (3.4) → 400
#               2.1.3 size-capped read (3.5) → 413
#               2.1.4 conversion OFF THE EVENT LOOP (asyncio.to_thread → 3.6): PDF/DOCX
#                     parsing is CPU-heavy and previously froze every concurrent request
#                     (SSE heartbeats, chat, other uploads)
#               2.1.5 needs-OCR PDF → a traceable `failed` row with the 3.3 message;
#                     every other conversion error aborts and persists NOTHING
#               2.1.6 token count off-thread (tiktoken) — metadata only, never a gate
#               2.1.7 repository create (4.1) with extraction_status 'not_extracted'
#               2.1.8 SSE "document_uploaded"
#             Returns 201 with the record; a needs-OCR failure carries `message` through
#             the schema's extra=allow (5.1).
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
        HTTPException: 400 bad extension/MIME, 413 oversized file, 422
            unreadable/OCR-only PDF.
    """
    # DOC-UPLOAD 2.1.1 — Identity gate: invalid project_id ⇒ 400 (ownership is already
    #                 enforced by AUTH 7.5 before the handler body runs).
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    filename = (file.filename or "unnamed").strip()
    # Persist the original conversion request before validation/conversion. It
    # remains in history even when a later step fails and the request-scoped
    # transaction is rolled back.
    await _persist_conversion_message(
        project_id,
        f"Convert uploaded document: {filename}",
        status_value="requested",
        session=session,
    )

    # DOC-UPLOAD 2.1.2 / 2.1.3 — Format allow-list (3.4), then the streaming size cap (3.5).
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
    # DOC-UPLOAD 2.1.4 — Off-loop conversion; 2.1.5 — the needs-OCR branch below is the
    #                 ONLY conversion failure that still persists a record.
    try:
        markdown = await asyncio.to_thread(_convert_to_markdown, raw, fmt["original_format"])
    except HTTPException as exc:
        # A scanned/no-text PDF keeps a traceable 'failed' record and surfaces an
        # explicit, non-silent 'needs-OCR' message. All other conversion errors
        # abort with the raised status code (nothing is persisted).
        if (
            exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
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

    # DOC-UPLOAD 2.1.6 — Token count off-thread (tiktoken BPE over the whole document):
    #                 measured for display/budget purposes only — it never truncates or
    #                 blocks the save.
    try:
        token_count = await asyncio.to_thread(count_tokens, markdown)
        # DOC-UPLOAD 2.1.7 — Persist the immutable knowledge record (4.1): full markdown,
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

    # Commit the document and its final transcript status together before any
    # best-effort realtime notification can fail.
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
            f"Document conversion completed: {filename}\nStatus: processed",
            status_value="processed",
            session=session,
        )

    # DOC-UPLOAD 2.1.8 — SSE "document_uploaded" (EVENTS flow) so other tabs/windows refresh
    #                 their knowledge lists.
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

# DOC-UPLOAD 2.2 — READ: list metadata only, newest first (4.2), no markdown body
#             (frontend 1.3/1.4). Invalid UUID ⇒ 400.
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


# DOC-UPLOAD 2.3 — READ: the FULL canonical markdown (4.3, include_markdown=True) for the
#             preview (frontend 1.5). Never truncated — token budgets apply only to the
#             LLM-feeding path (FLOW 15).
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

# DOC-UPLOAD 2.4 — WRITE: permanent removal (frontend 1.6). Order:
#               2.4.1 hard-delete the record (4.4) → 404 when absent
#               2.4.2 discard every DRAFT staged from THIS document: a pending_action of
#                     type DRAFT_ACTION_TYPE (FLOW 15's INSERT_CHUNKED_REQUIREMENTS) whose
#                     proposed_changes[DRAFT_DOCUMENT_REF_KEY].document_id matches — so the
#                     ConfirmationPanel can never offer a draft whose source is gone
#               2.4.3 SSE "document_deleted"
#             Requirements already confirm-extracted from this document are NOT touched
#             (they live in the requirement tables / PRD ledger).
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

    Deletes the immutable document record (full canonical markdown included).
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

    # DOC-UPLOAD 2.4.1 — Hard-delete scoped to the project (4.4) → 404 when it was absent.
    doc = await DocumentRepository.delete(str(document_id), str(project_id), session)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # DOC-UPLOAD 2.4.2 — Draft eviction: drop every DRAFT merge-preview staged from this
    #                 document (and nothing else), so the confirmation UI never references
    #                 a deleted source. Only WAITING_CONFIRMATION actions are returned by
    #                 get_by_project and these expire within the hour anyway — this just
    #                 prevents stale drafts.
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

    # DOC-UPLOAD 2.4.3 — SSE "document_deleted" (frontend 1.6 removes the preview locally).
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
    """Feed the whole document through the gatherer, accumulating a draft.

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
    """Extract requirements from an uploaded document — DRAFT ONLY.

    This is the explicit, user-triggered extraction action. It NEVER writes
    requirements, user stories, or acceptance criteria directly. Instead it
    accumulates every chunk's gatherer output in memory, de-duplicates it across
    the whole document, builds a SINGLE merged merge preview against the current
    requirement state, and stores that draft in ONE pending_action (action_type
    ``INSERT_CHUNKED_REQUIREMENTS``). The draft is versioned
    (``requirements_revision + 1``) so confirming via ``/api/confirm-action/{id}``
    applies it as a single atomic merge without creating a PRD version.

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

    content_markdown = doc.get("content_markdown") or ""
    token_count = count_tokens(content_markdown)
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

    try:
        run = await _run_extraction_by_chunks(
            project_id, document_id, content_markdown, token_count
        )
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

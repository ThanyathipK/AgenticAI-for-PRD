"""
Uploaded-document repository operations.

The ``uploaded_documents`` table is the project's knowledge/document store.
Canonical content is not edited in place, but extraction status is mutable and
rows can be deleted. Uploading ONLY adds source material — nothing here
ever mutates requirements, user stories, or acceptance criteria (that happens
exclusively through the explicit, user-confirmed extraction flow in
``app/routes/documents.py``).
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentModel
from app.repositories.base import as_uuid, serialize_document

logger = logging.getLogger(__name__)


# DOC-UPLOAD 7.0 — uploaded_documents persistence. Canonical content has no update
# path; extraction status is mutable and 7.4 permanently deletes the entire row.
#   7.1 create                   ← route 4.1 (the only INSERT path)
#   7.2 get_by_project           ← route 4.2 + delete-time draft scan context
#   7.3 get_by_id                ← routes 4.3 and 4.5 (with Markdown)
#   7.4 delete                   ← route 4.4 (hard delete, project-scoped)
#   7.5 update_extraction_status ← route 4.5 and CONFIRM
# Nothing here mutates requirements/user_stories/acceptance_criteria.
class DocumentRepository:
    """CRUD operations for uploaded project documents."""

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        # DOC-UPLOAD 7.1 — The only INSERT into uploaded_documents (route 4.1): stores the FULL
        #             converted markdown with its metadata; status defaults to 'processed' and
        #             extraction_status to 'not_extracted' (extraction is opt-in, route 4.5).
        """Persist a new document record with its full canonical markdown."""
        doc = DocumentModel(
            project_id=as_uuid(project_id),
            original_filename=data["original_filename"],
            original_format=data["original_format"],
            mime_type=data.get("mime_type"),
            content_markdown=data.get("content_markdown", ""),
            file_size_bytes=data.get("file_size_bytes", 0),
            token_count=data.get("token_count", 0),
            status=data.get("status", "processed"),
            uploaded_by=data.get("uploaded_by", "user"),
            extraction_status=data.get("extraction_status", "not_extracted"),
        )
        session.add(doc)
        await session.flush()
        await session.refresh(doc)
        return serialize_document(doc)

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        # DOC-UPLOAD 7.2 — List metadata, newest first (feeds route 4.2; serialization omits the
        #             Markdown body, so the UI calls route 4.3 to preview).
        """List every uploaded document for a project, newest first."""
        pid = as_uuid(project_id)
        stmt = (
            select(DocumentModel)
            .where(DocumentModel.project_id == pid)
            .order_by(DocumentModel.created_at.desc())
        )
        result = await session.execute(stmt)
        docs = result.scalars().all()
        return [serialize_document(d) for d in docs]

    @staticmethod
    async def get_validation_context(
        project_id: str, session: AsyncSession
    ) -> List[Dict[str, Any]]:
        """DOC-UPLOAD 7.2.1 — Return usable canonical text to the AUDIT flow.

        Unlike the lightweight document-list query, this deliberately includes
        the canonical markdown. Failed conversions (for example OCR-only PDFs)
        and empty bodies are excluded because they contain no text the auditor
        can validate.
        """
        pid = as_uuid(project_id)
        stmt = (
            select(DocumentModel)
            .where(
                DocumentModel.project_id == pid,
                DocumentModel.status == "processed",
                DocumentModel.content_markdown != "",
            )
            .order_by(DocumentModel.created_at.asc())
        )
        result = await session.execute(stmt)
        return [
            {
                "document_id": str(doc.id),
                "filename": doc.original_filename,
                "content_markdown": doc.content_markdown,
            }
            for doc in result.scalars().all()
        ]

    @staticmethod
    async def get_by_id(document_id: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        # DOC-UPLOAD 7.3 — Single record WITH Markdown for routes 4.3 and 4.5.
        """Fetch a single document by id, scoped to the project."""
        did = as_uuid(document_id)
        pid = as_uuid(project_id)
        stmt = select(DocumentModel).where(
            DocumentModel.id == did,
            DocumentModel.project_id == pid,
        )
        result = await session.execute(stmt)
        doc = result.scalar_one_or_none()
        if not doc:
            return None
        return serialize_document(doc, include_markdown=True)

    @staticmethod
    async def update_extraction_status(
        document_id: str,
        project_id: str,
        extraction_status: str,
        session: AsyncSession,
    ) -> Optional[Dict[str, Any]]:
        # DOC-UPLOAD 7.5 — Extraction tracking: `extraction_pending` during/after route 4.5,
        #             `not_extracted` on cancellation/failure/Discard, and
        #             `extraction_applied` after CONFIRM. Upload begins not_extracted.
        """Update a document's extraction-status tracking flag."""
        did = as_uuid(document_id)
        pid = as_uuid(project_id)
        stmt = select(DocumentModel).where(
            DocumentModel.id == did,
            DocumentModel.project_id == pid,
        )
        result = await session.execute(stmt)
        doc = result.scalar_one_or_none()
        if not doc:
            return None
        doc.extraction_status = extraction_status
        await session.flush()
        await session.refresh(doc)
        return serialize_document(doc)

    @staticmethod
    async def delete(document_id: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        # DOC-UPLOAD 7.4 — Hard delete, project-scoped (route 4.4). Returns the removed record for
        #             logging/audit or None when absent. Any DRAFT preview staged from this
        #             document is discarded by application step 5.2, not here.
        """Hard-delete an uploaded document, scoped to the owning project.

        Returns the removed record (for logging/audit) or ``None`` when the
        document does not exist. Watch out: any DRAFT merge preview already
        staged from this document (a pending_action whose
        ``proposed_changes._document_ref.document_id`` matches) is handled by
        the DELETE route, not here.
        """
        did = as_uuid(document_id)
        pid = as_uuid(project_id)
        stmt = select(DocumentModel).where(
            DocumentModel.id == did,
            DocumentModel.project_id == pid,
        )
        result = await session.execute(stmt)
        doc = result.scalar_one_or_none()
        if not doc:
            return None
        await session.delete(doc)
        await session.flush()
        return serialize_document(doc)

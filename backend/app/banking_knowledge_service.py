"""Reusable banking knowledge ingestion and relevance retrieval for audits."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import math
import re
from typing import Any
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.document_processor import split_markdown_into_chunks
from app.input_validation import count_tokens
from app.models import (
    BankingKnowledgeChunkModel,
    BankingKnowledgeDocumentModel,
    BankingKnowledgeRetrievalModel,
)
from app.semantic_memory import cosine_similarity, embed_texts

logger = logging.getLogger("app.banking_knowledge")

_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_STOP_WORDS = {
    "and", "the", "for", "with", "that", "this", "from", "into", "are", "was",
    "will", "shall", "must", "should", "user", "users", "requirement", "requirements",
}


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text or "") if token.lower() not in _STOP_WORDS}


def _lexical_score(query_tokens: set[str], content: str) -> float:
    content_tokens = _tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0
    overlap = len(query_tokens & content_tokens)
    return overlap / math.sqrt(len(query_tokens) * len(content_tokens))


def _serialize_document(row: BankingKnowledgeDocumentModel, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(row.id),
        "title": row.title,
        "original_filename": row.original_filename,
        "original_format": row.original_format,
        "mime_type": row.mime_type,
        "document_type": row.document_type,
        "jurisdiction": row.jurisdiction,
        "tags": row.tags or [],
        "version": row.version,
        "status": row.status,
        "approved_by": str(row.approved_by) if row.approved_by else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_content:
        payload["content_markdown"] = row.content_markdown
    return payload


async def create_knowledge_document(
    owner_user_id: str,
    session: AsyncSession,
    *,
    title: str,
    original_filename: str,
    original_format: str,
    mime_type: str | None,
    document_type: str,
    jurisdiction: str,
    tags: list[str],
    content_markdown: str,
) -> dict[str, Any]:
    body = content_markdown.strip()
    if not body:
        raise ValueError("Knowledge document contains no usable text")
    owner_id = uuid.UUID(str(owner_user_id))
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # The shared banking knowledge behaves like one owner-level SKILL.md: a
    # successful new upload replaces the previous source and all of its chunks.
    # Conversion happens before this function, and the surrounding transaction
    # rolls this deletion back if creation fails.
    existing_ids = select(BankingKnowledgeDocumentModel.id).where(
        BankingKnowledgeDocumentModel.owner_user_id == owner_id,
    )
    await session.execute(delete(BankingKnowledgeChunkModel).where(
        BankingKnowledgeChunkModel.document_id.in_(existing_ids),
    ))
    await session.execute(delete(BankingKnowledgeDocumentModel).where(
        BankingKnowledgeDocumentModel.owner_user_id == owner_id,
    ))
    await session.flush()

    document = BankingKnowledgeDocumentModel(
        owner_user_id=owner_id,
        title=title.strip(),
        original_filename=original_filename,
        original_format=original_format,
        mime_type=mime_type,
        document_type=document_type.strip() or "best_practice",
        jurisdiction=jurisdiction.strip() or "global",
        tags=sorted({tag.strip().lower() for tag in tags if tag.strip()}),
        content_markdown=body,
        content_checksum=checksum,
        status="draft",
    )
    session.add(document)
    await session.flush()

    chunks = split_markdown_into_chunks(
        body,
        chunk_size=settings.BANKING_RAG_CHUNK_TOKENS,
        overlap=settings.BANKING_RAG_CHUNK_OVERLAP,
        token_count=count_tokens(body),
    ) or [body]
    embeddings: list[list[float]] = [[] for _ in chunks]
    try:
        retrieval_prefix = " ".join([
            title.strip(), document_type.strip(), jurisdiction.strip(),
            " ".join(tag.strip() for tag in tags if tag.strip()),
        ]).strip()
        embeddings = await embed_texts([
            f"{retrieval_prefix}\n{chunk}" if retrieval_prefix else chunk
            for chunk in chunks
        ])
    except Exception as exc:
        # Knowledge remains usable through lexical retrieval while the local
        # embedding model is offline or not loaded.
        logger.warning("Banking knowledge embeddings unavailable; lexical fallback enabled: %s", exc)

    for index, chunk in enumerate(chunks):
        heading_match = _HEADING_RE.search(chunk)
        embedding = embeddings[index] if index < len(embeddings) else []
        session.add(BankingKnowledgeChunkModel(
            document_id=document.id,
            chunk_index=index,
            heading=heading_match.group(1).strip()[:500] if heading_match else None,
            content=chunk,
            token_count=count_tokens(chunk),
            embedding=embedding,
            embedding_model=settings.LM_STUDIO_EMBEDDING_MODEL if embedding else "",
        ))
    await session.flush()
    payload = _serialize_document(document, include_content=True)
    payload["chunk_count"] = len(chunks)
    payload["embedding_status"] = "ready" if any(embeddings) else "lexical_only"
    return payload


async def clear_knowledge_document(owner_user_id: str, session: AsyncSession) -> int:
    """Remove the owner's singleton shared knowledge source and its chunks."""
    owner_id = uuid.UUID(str(owner_user_id))
    document_ids = select(BankingKnowledgeDocumentModel.id).where(
        BankingKnowledgeDocumentModel.owner_user_id == owner_id,
    )
    await session.execute(delete(BankingKnowledgeChunkModel).where(
        BankingKnowledgeChunkModel.document_id.in_(document_ids),
    ))
    result = await session.execute(delete(BankingKnowledgeDocumentModel).where(
        BankingKnowledgeDocumentModel.owner_user_id == owner_id,
    ))
    await session.flush()
    return int(result.rowcount or 0)


async def list_knowledge_documents(owner_user_id: str, session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(BankingKnowledgeDocumentModel)
        .where(BankingKnowledgeDocumentModel.owner_user_id == uuid.UUID(str(owner_user_id)))
        .order_by(BankingKnowledgeDocumentModel.created_at.desc())
    )).scalars().all()
    return [_serialize_document(row) for row in rows]


async def get_knowledge_document(document_id: str, owner_user_id: str, session: AsyncSession) -> dict[str, Any] | None:
    row = await session.scalar(select(BankingKnowledgeDocumentModel).where(
        BankingKnowledgeDocumentModel.id == uuid.UUID(str(document_id)),
        BankingKnowledgeDocumentModel.owner_user_id == uuid.UUID(str(owner_user_id)),
    ))
    return _serialize_document(row, include_content=True) if row else None


async def set_knowledge_status(
    document_id: str,
    owner_user_id: str,
    status: str,
    session: AsyncSession,
) -> dict[str, Any] | None:
    row = await session.scalar(select(BankingKnowledgeDocumentModel).where(
        BankingKnowledgeDocumentModel.id == uuid.UUID(str(document_id)),
        BankingKnowledgeDocumentModel.owner_user_id == uuid.UUID(str(owner_user_id)),
    ))
    if row is None:
        return None
    row.status = status
    if status == "approved":
        row.approved_by = uuid.UUID(str(owner_user_id))
        row.approved_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(row)
    return _serialize_document(row)


def build_audit_retrieval_query(state: dict[str, Any]) -> str:
    """Build a compact semantic query from current requirements and stories."""
    pieces: list[str] = []
    for requirement in state.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        pieces.extend([str(requirement.get("title") or ""), str(requirement.get("description") or "")])
    for story in state.get("user_stories") or []:
        if not isinstance(story, dict) or story.get("status", "active") == "archived":
            continue
        pieces.extend([
            str(story.get("story_title") or ""), str(story.get("as_a") or ""),
            str(story.get("i_want_to") or ""), str(story.get("so_that") or ""),
            " ".join(str(item) for item in story.get("acceptance_criteria") or []),
        ])
    pieces.extend(str(item) for item in state.get("business_goals") or [])
    return "\n".join(piece.strip() for piece in pieces if piece.strip())[:12000]


async def retrieve_banking_knowledge(
    owner_user_id: str,
    project_id: str,
    query_text: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return top relevant approved chunks, with an auditable retrieval record."""
    owner_id = uuid.UUID(str(owner_user_id))
    rows = (await session.execute(
        select(BankingKnowledgeChunkModel, BankingKnowledgeDocumentModel)
        .join(BankingKnowledgeDocumentModel, BankingKnowledgeDocumentModel.id == BankingKnowledgeChunkModel.document_id)
        .where(
            BankingKnowledgeDocumentModel.owner_user_id == owner_id,
            BankingKnowledgeDocumentModel.status == "approved",
        )
    )).all()

    query = query_text.strip()
    query_tokens = _tokens(query)
    query_embedding: list[float] = []
    try:
        if query:
            query_embedding = (await embed_texts([query]))[0]
    except Exception as exc:
        logger.warning("Banking RAG query embedding unavailable; using lexical retrieval: %s", exc)

    scored: list[tuple[float, BankingKnowledgeChunkModel, BankingKnowledgeDocumentModel]] = []
    for chunk, document in rows:
        lexical = _lexical_score(
            query_tokens,
            " ".join([
                document.title,
                document.document_type,
                document.jurisdiction,
                " ".join(document.tags or []),
                chunk.content,
            ]),
        )
        vector = cosine_similarity(query_embedding, chunk.embedding or []) if query_embedding else 0.0
        score = (0.8 * max(0.0, vector) + 0.2 * lexical) if query_embedding and chunk.embedding else lexical
        if score > 0.0:
            scored.append((score, chunk, document))
    scored.sort(key=lambda item: (-item[0], item[2].title.casefold(), item[1].chunk_index))

    selected = []
    used_tokens = 0
    for score, chunk, document in scored:
        if len(selected) >= settings.BANKING_RAG_TOP_K:
            break
        if selected and used_tokens + chunk.token_count > settings.BANKING_RAG_MAX_CONTEXT_TOKENS:
            continue
        used_tokens += chunk.token_count
        selected.append({
            "document_id": f"banking:{document.id}:chunk:{chunk.chunk_index}",
            "filename": document.title,
            "content_markdown": chunk.content,
            "section": chunk.heading or f"Chunk {chunk.chunk_index + 1}",
            "knowledge_document_id": str(document.id),
            "document_type": document.document_type,
            "jurisdiction": document.jurisdiction,
            "version": document.version,
            "retrieval_score": round(score, 6),
        })

    session.add(BankingKnowledgeRetrievalModel(
        project_id=uuid.UUID(str(project_id)),
        owner_user_id=owner_id,
        query_text=query,
        retrieved_chunks=[{
            "document_id": item["knowledge_document_id"],
            "chunk_id": item["document_id"],
            "title": item["filename"],
            "section": item["section"],
            "score": item["retrieval_score"],
        } for item in selected],
        embedding_used=bool(query_embedding),
    ))
    await session.flush()
    return selected

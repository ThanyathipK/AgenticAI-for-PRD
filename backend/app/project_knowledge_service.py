"""Bounded, project-scoped knowledge retrieval for General Chat."""
from __future__ import annotations

import asyncio
import math
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.document_processor import prepare_document_for_extraction, split_markdown_into_chunks
from app.input_validation import count_tokens
from app.repositories import DocumentRepository

_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_STOP_WORDS = {
    "a", "an", "and", "are", "about", "base", "can", "could", "document",
    "does", "explain", "file", "for", "from", "how", "in", "is", "it",
    "knowledge", "me", "of", "on", "please", "say", "should", "summarize",
    "summary", "tell", "that", "the", "this", "to", "what", "with", "would",
}


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _WORD_RE.findall(text or "")
        if token.lower() not in _STOP_WORDS
    }


def _lexical_score(query_tokens: set[str], content: str) -> float:
    content_tokens = _tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0
    overlap = len(query_tokens & content_tokens)
    return overlap / math.sqrt(len(query_tokens) * len(content_tokens))


def _safe_label(value: str, fallback: str) -> str:
    """Keep source labels compact and prevent metadata from shaping the prompt."""
    cleaned = re.sub(r"[\r\n\[\]<>\"'&]+", " ", value or "").strip()
    return cleaned[:255] or fallback


def _safe_source_content(value: str) -> str:
    """Prevent document text from spoofing this prompt's source boundaries."""
    return re.sub(
        r"</?project_source\b",
        "[project-source boundary removed]",
        value or "",
        flags=re.IGNORECASE,
    )


def _rank_document_chunks(
    documents: list[dict[str, Any]], query_text: str,
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return []

    scored: list[tuple[float, str, int, dict[str, Any]]] = []
    for document in documents:
        body = prepare_document_for_extraction(str(document.get("content_markdown") or ""))
        if not body.strip():
            continue
        filename = _safe_label(str(document.get("filename") or ""), "Project document")
        chunks = split_markdown_into_chunks(
            body,
            chunk_size=settings.PROJECT_RAG_CHUNK_TOKENS,
            overlap=settings.PROJECT_RAG_CHUNK_OVERLAP,
            token_count=count_tokens(body),
        )
        filename_score = _lexical_score(query_tokens, filename)
        for index, chunk in enumerate(chunks):
            content_score = _lexical_score(query_tokens, chunk)
            # A filename match is useful evidence of user intent, but the body
            # still dominates ranking when both are available.
            score = content_score + (0.35 * filename_score)
            if score <= 0.0:
                continue
            heading_match = _HEADING_RE.search(chunk)
            section = _safe_label(
                heading_match.group(1) if heading_match else "",
                f"Chunk {index + 1}",
            )
            scored.append((score, filename.casefold(), index, {
                "document_id": str(document.get("document_id") or ""),
                "filename": filename,
                "section": section,
                "content_markdown": chunk,
                "token_count": count_tokens(chunk),
                "retrieval_score": round(score, 6),
            }))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in scored]


async def retrieve_project_knowledge(
    project_id: str,
    query_text: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return only relevant, token-bounded chunks from this project's files.

    Retrieval is deliberately read-only. Chunking and tokenization run off the
    event loop because converted documents can be large. No relevant lexical
    match returns an empty list rather than injecting unrelated material.
    """
    if not query_text.strip():
        return []
    documents = await DocumentRepository.get_validation_context(project_id, session)
    ranked = await asyncio.to_thread(_rank_document_chunks, documents, query_text)

    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for chunk in ranked:
        chunk_tokens = int(chunk.get("token_count") or 0)
        if chunk_tokens <= 0 or used_tokens + chunk_tokens > settings.PROJECT_RAG_MAX_CONTEXT_TOKENS:
            continue
        selected.append(chunk)
        used_tokens += chunk_tokens
        if len(selected) >= settings.PROJECT_RAG_TOP_K:
            break
    return selected


def build_project_knowledge_block(chunks: list[dict[str, Any]]) -> str:
    """Render retrieved chunks as clearly delimited, citation-ready evidence."""
    if not chunks:
        return ""
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        filename = _safe_label(str(chunk.get("filename") or ""), "Project document")
        section = _safe_label(str(chunk.get("section") or ""), f"Chunk {index}")
        content = _safe_source_content(str(chunk.get("content_markdown") or ""))
        parts.append(
            f"<project_source index=\"{index}\" citation=\"[{filename} — {section}]\">\n"
            f"{content}\n"
            "</project_source>"
        )
    return "\n\n".join(parts)

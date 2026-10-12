"""Reusable banking knowledge-base management endpoints."""
import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.banking_knowledge_service import (
    clear_knowledge_document,
    create_knowledge_document,
    get_knowledge_document,
    list_knowledge_documents,
    set_knowledge_status,
)
from app.database import get_db
from app.routes.documents import _convert_to_markdown, _read_upload_with_size_cap, _resolve_upload_format

router = APIRouter()


@router.get("/api/banking-knowledge")
async def get_banking_knowledge(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await list_knowledge_documents(current_user.id, db)


@router.delete("/api/banking-knowledge")
async def clear_banking_knowledge(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    deleted = await clear_knowledge_document(current_user.id, db)
    return {"status": "cleared", "deleted": deleted}


@router.get("/api/banking-knowledge/{document_id}")
async def get_banking_knowledge_document(
    document_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        document = await get_knowledge_document(document_id, current_user.id, db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document_id") from None
    if document is None:
        raise HTTPException(status_code=404, detail="Banking knowledge document not found")
    return document


@router.post("/api/banking-knowledge/upload", status_code=status.HTTP_201_CREATED)
async def upload_banking_knowledge(
    file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=255),
    document_type: str = Form("best_practice", max_length=100),
    jurisdiction: str = Form("global", max_length=100),
    tags: str = Form("", max_length=1000),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    descriptor = _resolve_upload_format(file.filename or "", file.content_type or "")
    raw = await _read_upload_with_size_cap(file)
    content = await asyncio.to_thread(_convert_to_markdown, raw, descriptor["original_format"])
    try:
        return await create_knowledge_document(
            current_user.id,
            db,
            title=title,
            original_filename=file.filename or title,
            original_format=descriptor["original_format"],
            mime_type=descriptor["mime_type"],
            document_type=document_type,
            jurisdiction=jurisdiction,
            tags=tags.split(","),
            content_markdown=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/api/banking-knowledge/{document_id}/approve")
async def approve_banking_knowledge(
    document_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        document = await set_knowledge_status(document_id, current_user.id, "approved", db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document_id") from None
    if document is None:
        raise HTTPException(status_code=404, detail="Banking knowledge document not found")
    return document


@router.post("/api/banking-knowledge/{document_id}/retire")
async def retire_banking_knowledge(
    document_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        document = await set_knowledge_status(document_id, current_user.id, "retired", db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document_id") from None
    if document is None:
        raise HTTPException(status_code=404, detail="Banking knowledge document not found")
    return document

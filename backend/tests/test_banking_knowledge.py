"""Reusable banking knowledge ingestion and RAG retrieval tests."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.banking_knowledge_service import (
    build_audit_retrieval_query,
    clear_knowledge_document,
    create_knowledge_document,
    list_knowledge_documents,
    retrieve_banking_knowledge,
    set_knowledge_status,
)
from app.models import BankingKnowledgeRetrievalModel, ProjectModel, UserModel


@pytest_asyncio.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/banking_knowledge.sqlite")
    async with engine.begin() as connection:
        from app.database import Base
        from app import models  # noqa: F401

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def owners(db_session):
    first = UserModel(email="first@bank.test", full_name="First Owner", role="Product Owner")
    second = UserModel(email="second@bank.test", full_name="Second Owner", role="Product Owner")
    db_session.add_all([first, second])
    await db_session.flush()
    project = ProjectModel(
        user_id=first.id,
        name="Transfer Project",
        industry_standard="Banking",
    )
    db_session.add(project)
    await db_session.flush()
    return str(first.id), str(second.id), str(project.id)


@pytest.mark.asyncio
async def test_only_relevant_approved_owner_knowledge_is_retrieved(db_session, owners, monkeypatch):
    first, second, project = owners

    async def embeddings_unavailable(_texts):
        raise RuntimeError("embedding model offline")

    monkeypatch.setattr("app.banking_knowledge_service.embed_texts", embeddings_unavailable)
    relevant = await create_knowledge_document(
        first,
        db_session,
        title="Maker Checker Standard",
        original_filename="maker-checker.md",
        original_format="md",
        mime_type="text/markdown",
        document_type="control_standard",
        jurisdiction="TH",
        tags=["payments", "approval"],
        content_markdown="# Transfer approvals\nHigh-value transfers require maker checker approval and an immutable audit log.",
    )
    await set_knowledge_status(relevant["id"], first, "approved", db_session)

    # Another owner's approved knowledge must never enter this owner's context.
    foreign = await create_knowledge_document(
        second,
        db_session,
        title="Other Tenant Guidance",
        original_filename="other.md",
        original_format="md",
        mime_type="text/markdown",
        document_type="standard",
        jurisdiction="TH",
        tags=[],
        content_markdown="Maker checker transfer approval from another tenant.",
    )
    await set_knowledge_status(foreign["id"], second, "approved", db_session)

    results = await retrieve_banking_knowledge(
        first,
        project,
        "high value transfer maker checker approval audit log",
        db_session,
    )
    assert len(results) == 1
    assert results[0]["filename"] == "Maker Checker Standard"
    assert results[0]["document_id"].startswith(f"banking:{relevant['id']}:chunk:")
    assert "immutable audit log" in results[0]["content_markdown"]

    retrieval = (await db_session.execute(select(BankingKnowledgeRetrievalModel))).scalar_one()
    assert retrieval.project_id == uuid.UUID(project)
    assert retrieval.embedding_used is False
    assert retrieval.retrieved_chunks[0]["document_id"] == relevant["id"]


@pytest.mark.asyncio
async def test_new_upload_replaces_previous_owner_file_and_clear_removes_it(db_session, owners, monkeypatch):
    first, _second, _project = owners
    async def no_embeddings(_texts):
        return []

    monkeypatch.setattr("app.banking_knowledge_service.embed_texts", no_embeddings)
    first_document = await create_knowledge_document(first, db_session, **dict(
        title="Old guidance",
        original_filename="old.md",
        original_format="md",
        mime_type="text/markdown",
        document_type="standard",
        jurisdiction="global",
        tags=[],
        content_markdown="The old reusable banking control.",
    ))
    replacement = await create_knowledge_document(first, db_session, **dict(
        title="Current guidance",
        original_filename="current.md",
        original_format="md",
        mime_type="text/markdown",
        document_type="standard",
        jurisdiction="global",
        tags=[],
        content_markdown="The current reusable banking control.",
    ))

    documents = await list_knowledge_documents(first, db_session)
    assert [document["id"] for document in documents] == [replacement["id"]]
    assert first_document["id"] != replacement["id"]
    assert await clear_knowledge_document(first, db_session) == 1
    assert await list_knowledge_documents(first, db_session) == []


def test_audit_query_contains_requirement_story_and_criteria():
    query = build_audit_retrieval_query({
        "requirements": [{"title": "Transfers", "description": "Approve high value transfers"}],
        "user_stories": [{
            "story_title": "Checker approval",
            "as_a": "checker",
            "i_want_to": "approve a payment",
            "so_that": "fraud is reduced",
            "acceptance_criteria": ["An audit record is immutable"],
        }],
    })
    assert "Approve high value transfers" in query
    assert "Checker approval" in query
    assert "audit record is immutable" in query

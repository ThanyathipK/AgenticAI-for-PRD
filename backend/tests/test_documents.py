"""
Unit tests for uploaded documents: token routing, chunking, upload validation,
and the DRAFT-ONLY extraction contract.

The critical guarantee exercised here: **uploading a document and even running
extraction writes NOTHING to requirements / user_stories / acceptance_criteria**
— only a draft ``pending_action`` is created — and confirming that draft applies
the merge exactly once with a single version bump.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_documents.py -v
"""
import io
import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import get_db as app_get_db
from app.document_processor import (
    DRAFT_ACTION_TYPE,
    accumulate_document_draft,
    decide_document_mode,
    document_budget,
    route_document_tokens,
    split_markdown_into_chunks,
    _split_long_line,
    _tail_by_tokens,
)
from app.event_manager import event_manager
from app.main import app as fastapi_app
from app.models import (
    AcceptanceCriteriaModel,
    ArtifactEventLogModel,
    ConversationMessageModel,
    DocumentModel,
    PendingActionModel,
    ProjectModel,
    RequirementModel,
    RequirementStateModel,
    UserModel,
    UserStoryModel,
)


# =====================================================================
# Isolated SQLite database + ASGI client fixtures
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_docs.db")
    async with engine.begin() as conn:
        from app.database import Base
        from app import models  # noqa: F401 - register all models

        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_project(db_session):
    """Seed the default system user + one project; returns the project id."""
    user = UserModel(
        email="docs-test@banking.com",
        full_name="Docs Tester",
        role="Developer",
    )
    db_session.add(user)
    await db_session.flush()  # materialise user.id before FK use
    project = ProjectModel(
        user_id=user.id,
        name="Document Test Project",
        industry_standard="Krungsri Nimble Baseline",
    )
    db_session.add(project)
    await db_session.commit()
    return str(project.id)


@pytest_asyncio.fixture
async def client(db_session):
    """ASGI client wired to the isolated SQLite session (no lifespan run)."""
    async def override_get_db():
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    fastapi_app.dependency_overrides[app_get_db] = override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(app_get_db, None)


# =====================================================================
# Shared assertion helpers
# =====================================================================
async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar() or 0)


def make_gathered(epic_name="Document Epic", stories=None):
    """Build a GatheredRequirements-shaped dict like the real gatherer emits."""
    return {
        "epic_name": epic_name,
        "version": 1,
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": epic_name or "Document Requirement",
                "description": "",
                "user_stories": stories or [],
            }
        ],
    }


def make_story(ticket, title, criteria=None):
    return {
        "ticket_code": ticket,
        "story_title": title,
        "as_a": "bank teller",
        "i_want_to": f"use {title.lower()}",
        "so_that": "work is faster",
        "acceptance_criteria": criteria or ["Given X when Y then Z"],
    }


# =====================================================================
# Token routing decision (full vs chunked) at the DOCUMENT_BUDGET boundary
# =====================================================================
class TestTokenRouting:
    def test_budget_is_fraction_of_context(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 8192)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        assert document_budget() == 4915

    def test_at_budget_boundary_runs_full(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 100)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        # 60 tokens == budget -> still a single full pass (inclusive boundary).
        assert decide_document_mode(60) == "full"
        assert decide_document_mode(document_budget()) == "full"

    def test_above_budget_switches_to_chunked(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 100)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        assert decide_document_mode(61) == "chunked"
        assert decide_document_mode(document_budget() + 1) == "chunked"

    def test_routing_reports_expected_chunk_count(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 100)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_SIZE", 30)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_OVERLAP", 5)
        routing = route_document_tokens(61)
        assert routing.mode == "chunked"
        # usable window = 30 - 5 = 25 tokens -> ceil(61 / 25) = 3 passes
        assert routing.chunk_count == 3
        full = route_document_tokens(60)
        assert full.mode == "full" and full.chunk_count == 1


# =====================================================================
# Chunker correctness: counts, overlap, no content loss across boundaries
# =====================================================================
class TestChunker:
    def test_empty_text_returns_no_chunks(self):
        assert split_markdown_into_chunks("") == []

    def test_small_text_single_chunk(self):
        text = "# Heading\nJust a couple of lines."
        chunks = split_markdown_into_chunks(text, chunk_size=1000, overlap=100)
        assert chunks == [text]

    @pytest.fixture
    def long_doc_lines(self):
        return [f"Line {i}: the quick brown fox jumps over the lazy dog." for i in range(200)]

    def test_multi_chunk_counts_within_budget(self, long_doc_lines):
        text = "\n".join(long_doc_lines)
        chunk_size, overlap = 120, 12
        chunks = split_markdown_into_chunks(text, chunk_size=chunk_size, overlap=overlap)
        assert len(chunks) > 1
        for chunk in chunks:
            # Chunks respect the budget with only single-line slack.
            assert count_tokens_local(chunk) <= chunk_size + 20

    def test_no_content_loss_across_boundaries(self, long_doc_lines):
        text = "\n".join(long_doc_lines)
        chunk_size, overlap = 120, 12
        chunks = split_markdown_into_chunks(text, chunk_size=chunk_size, overlap=overlap)
        for line in long_doc_lines:
            assert any(line in chunk for chunk in chunks), f"lost line: {line}"

    def test_overlap_carries_tail_into_next_chunk(self, long_doc_lines):
        text = "\n".join(long_doc_lines)
        chunk_size, overlap = 120, 12
        chunks = split_markdown_into_chunks(text, chunk_size=chunk_size, overlap=overlap)
        for prev_chunk, next_chunk in zip(chunks, chunks[1:]):
            tail = _tail_by_tokens(prev_chunk, overlap)
            assert tail, "expected non-empty overlap tail"
            assert next_chunk.startswith(tail)

    def test_unbreakable_line_is_word_split(self):
        blob = " ".join(["supercalifragilistic"] * 80)
        chunks = split_markdown_into_chunks(blob, chunk_size=50, overlap=0)
        assert len(chunks) > 1
        for word in blob.split(" "):
            assert any(word in chunk for chunk in chunks)

    def test_precomputed_token_count_skips_redundant_encode(self, long_doc_lines):
        """Supplying the measured token_count must not change the chunk output
        (the route already counted the whole doc once; the chunker reusing that
        number skips a full extra tokenization on large documents)."""
        text = "\n".join(long_doc_lines)
        chunk_size, overlap = 120, 12
        reference = split_markdown_into_chunks(text, chunk_size=chunk_size, overlap=overlap)
        assert len(reference) > 1
        precomputed = split_markdown_into_chunks(
            text,
            chunk_size=chunk_size,
            overlap=overlap,
            token_count=count_tokens_local(text),
        )
        assert precomputed == reference

    def test_split_long_line_fast_path_stays_within_budget(self):
        """The estimate-first fast path must flush at the same word boundaries as
        the exact checker — every piece stays within budget for a big blob."""
        blob = " ".join(["supercalifragilistic"] * 200)
        pieces = _split_long_line(blob, chunk_size=40)
        assert len(pieces) > 1
        for piece in pieces:
            assert count_tokens_local(piece) <= 40
        # No content was lost between the pieces.
        assert " ".join(pieces) == blob

    def test_split_long_line_matches_exact_reference(self):
        """The fast path output must be identical to the original per-word exact
        algorithm (reference reimplemented inline for proof of equivalence)."""
        blob = " ".join(["supercalifragilistic"] * 200)

        def exact_reference(line, chunk_size):
            pieces_out = []
            current = []
            for word in line.split(" "):
                candidate = " ".join(current + [word])
                if current and count_tokens_local(candidate) > chunk_size:
                    pieces_out.append(" ".join(current))
                    current = [word]
                else:
                    current.append(word)
            if current:
                pieces_out.append(" ".join(current))
            return pieces_out

        for chunk_size in (30, 40, 60, 120):
            assert _split_long_line(blob, chunk_size) == exact_reference(
                blob, chunk_size
            )


def count_tokens_local(text: str) -> int:
    from app.input_validation import count_tokens

    return count_tokens(text)


# =====================================================================
# Upload validation: bad extension, MIME mismatch, oversized file, happy path
# =====================================================================
class TestUploadValidation:
    @pytest.mark.asyncio
    async def test_bad_extension_rejected(self, seeded_project, client):
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("malware.exe", b"MZ...", "application/octet-stream")},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "Supported formats" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_mime_mismatch_rejected(self, seeded_project, client):
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("notes.md", b"# hi", "video/mp4")},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "does not match" in resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filename", "content_type", "payload", "label"),
        [
            ("fake.pdf", "application/pdf", b"not a pdf", "PDF"),
            (
                "fake.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                b"not a zip",
                "DOCX",
            ),
        ],
    )
    async def test_binary_signature_mismatch_rejected(
        self, seeded_project, client, filename, content_type, payload, label
    ):
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": (filename, payload, content_type)},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert label in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_empty_file_rejected(self, seeded_project, client):
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "empty" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_oversized_file_rejected_with_413(self, seeded_project, client, monkeypatch):
        # MAX_UPLOAD_MB is the ONLY size gate at save time; shrink it so the cap
        # triggers on a tiny payload.
        monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("big.md", b"x" * 64, "text/markdown")},
        )
        assert resp.status_code == status.HTTP_413_CONTENT_TOO_LARGE
        assert "maximum upload size" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_markdown_upload_stores_full_content(self, seeded_project, client, db_session):
        markdown = "# Requirements Brief\n\n- Payments\n- Ledger integrity\n"
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("brief.md", markdown.encode(), "text/markdown")},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        body = resp.json()
        assert body["status"] == "processed"
        assert body["original_format"] == "md"
        assert body["token_count"] > 0

        # Knowledge base only: requirement tables remain untouched.
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0
        assert await _count(db_session, AcceptanceCriteriaModel) == 0
        assert await _count(db_session, PendingActionModel) == 0

        # Full markdown retrievable via the detail endpoint.
        detail = await client.get(f"/api/project/{seeded_project}/documents/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["content_markdown"] == markdown

        listing = await client.get(f"/api/project/{seeded_project}/documents")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

        history = await client.get(f"/api/project/{seeded_project}/conversations")
        conversion_messages = [
            item for item in history.json()
            if item["workflow_state"] == "document_conversion"
        ]
        assert [item["intent"] for item in conversion_messages] == [
            "DOCUMENT_CONVERSION_PROCESSED",
        ]
        assert conversion_messages[0]["message"] == "File uploaded: brief.md"

    @pytest.mark.asyncio
    async def test_scanned_pdf_fails_loudly_needs_ocr(self, seeded_project, client, db_session, monkeypatch):
        """Image-only PDFs surface an explicit needs-OCR failure (OCR out of scope)."""
        class _FakePage:
            def extract_text(self):
                return ""  # scanned page: no text layer

        class _FakeReader:
            def __init__(self, stream):
                self.pages = [_FakePage(), _FakePage()]

        import sys
        import types

        fake_pypdf = types.ModuleType("pypdf")
        fake_pypdf.PdfReader = _FakeReader
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        minimal_pdf = b"%PDF-1.4 fake scanned bytes"
        resp = await client.post(
            f"/api/project/{seeded_project}/documents/upload",
            files={"file": ("scan.pdf", minimal_pdf, "application/pdf")},
        )
        assert resp.status_code == status.HTTP_201_CREATED  # record kept for traceability
        body = resp.json()
        assert body["status"] == "failed"
        assert "OCR is out of scope" in body["message"]

        history = await client.get(f"/api/project/{seeded_project}/conversations")
        conversion_messages = [
            item for item in history.json()
            if item["workflow_state"] == "document_conversion"
        ]
        assert conversion_messages[-1]["intent"] == "DOCUMENT_CONVERSION_FAILED"
        assert "OCR is out of scope" in conversion_messages[-1]["message"]

        # The failed document cannot be processed into requirements.
        process = await client.post(
            f"/api/project/{seeded_project}/documents/{body['id']}/process"
        )
        assert process.status_code == status.HTTP_400_BAD_REQUEST


# =====================================================================
# CRITICAL: DRAFT-ONLY accumulation — nothing written until confirmation
# =====================================================================
async def _upload_doc(client, project_id: str, name="spec.md", body="# Spec\n\n" + "Detail line.\n" * 40) -> dict:
    resp = await client.post(
        f"/api/project/{project_id}/documents/upload",
        files={"file": (name, body.encode(), "text/markdown")},
    )
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()


@pytest.fixture
def fake_gatherer(monkeypatch):
    """Replace the LLM gatherer with deterministic per-chunk output.

    Chunk overlap means consecutive chunks re-report the same story; the
    accumulator must collapse those duplicates across the whole document.
    """
    calls = {"chunks": []}

    async def _fake_extract(chunk_text, *, chunk_index=1, chunk_count=1):
        calls["chunks"].append(chunk_text)
        return make_gathered(
            stories=[
                make_story("US-001", "Duplicate From Overlap"),
                make_story(f"US-{chunk_index + 1:03d}", f"Unique Story {chunk_index}"),
            ]
        )

    import app.routes.documents as documents_module
    monkeypatch.setattr(documents_module, "extract_requirements_from_text", _fake_extract)
    return calls


class TestDraftOnlyExtraction:

    @pytest.mark.asyncio
    async def test_failed_extraction_restores_knowledge_only_status(
        self, seeded_project, client, db_session, monkeypatch
    ):
        """An LLM failure creates no draft and must not leave a false pending chip."""
        async def fail_extract(*_args, **_kwargs):
            raise RuntimeError("model offline")

        import app.routes.documents as documents_module
        monkeypatch.setattr(documents_module, "extract_requirements_from_text", fail_extract)

        doc = await _upload_doc(client, seeded_project)
        response = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "No changes were written" in response.json()["detail"]
        assert await _count(db_session, PendingActionModel) == 0
        row = (
            await db_session.execute(
                select(DocumentModel).where(DocumentModel.id == doc["id"])
            )
        ).scalar_one()
        assert row.extraction_status == "not_extracted"
        messages = (
            await db_session.execute(
                select(ConversationMessageModel)
                .where(ConversationMessageModel.project_id == seeded_project)
                .order_by(ConversationMessageModel.created_at)
            )
        ).scalars().all()
        extraction_messages = [
            message for message in messages
            if message.intent.startswith("DOCUMENT_REQUIREMENTS_")
        ]
        assert [message.intent for message in extraction_messages] == [
            "DOCUMENT_REQUIREMENTS_REQUEST",
            "DOCUMENT_REQUIREMENTS_FAILED",
        ]
        assert sum(
            "extraction failed" in message.message for message in extraction_messages
        ) == 1

    @pytest.mark.asyncio
    async def test_process_creates_draft_only_and_confirm_applies_once(
        self, seeded_project, client, db_session, monkeypatch, fake_gatherer
    ):
        # Small budget => the doc routes to CHUNKED mode with several chunks.
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 60)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_SIZE", 30)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_OVERLAP", 5)

        doc = await _upload_doc(client, seeded_project)
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0
        assert await _count(db_session, AcceptanceCriteriaModel) == 0
        assert await _count(db_session, PendingActionModel) == 0

        # --- Explicit processing: DRAFT ONLY -----------------------------
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200
        payload = proc.json()
        assert payload["status"] == "draft_ready"
        assert payload["mode"] == "chunked"
        assert payload["chunk_count"] > 1  # sequential chunked passes ran
        assert payload["pending_action_id"]

        # The whole document went through the gatherer chunk-by-chunk...
        assert len(fake_gatherer["chunks"]) == payload["chunk_count"]

        action_rows = (
            (await db_session.execute(select(PendingActionModel))).scalars().all()
        )
        assert len(action_rows) == 1
        action = action_rows[0]
        assert action.action_type == DRAFT_ACTION_TYPE
        proposed = action.proposed_changes
        flat_titles = [
            s["story_title"]
            for req in proposed["requirements"]
            for s in req.get("user_stories", [])
        ]
        assert len(flat_titles) == len(set(flat_titles)), "duplicates leaked into draft"
        draft_story_count = len(flat_titles)
        assert draft_story_count > 2  # overlap duplicates were really collapsed

        # NOTHING was persisted to requirement tables — draft only!
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0
        assert await _count(db_session, AcceptanceCriteriaModel) == 0

        doc_row = (
            (await db_session.execute(select(DocumentModel))).scalars().one()
        )
        assert doc_row.extraction_status == "extraction_pending"

        # --- Confirm: single atomic merge, no PRD version bump -----------
        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == 200

        states = (
            (await db_session.execute(select(RequirementStateModel))).scalars().all()
        )
        assert len(states) == 1
        assert states[0].version_number == 1
        assert states[0].requirements_revision == 2

        story_rows = (
            (await db_session.execute(select(UserStoryModel))).scalars().all()
        )
        # Every deduped draft story lands EXACTLY once — the merged draft is
        # applied as one atomic insert, never per-chunk.
        assert len(story_rows) == draft_story_count
        assert await _count(db_session, AcceptanceCriteriaModel) >= draft_story_count

        # Pending action consumed; confirming again is impossible (404).
        assert await _count(db_session, PendingActionModel) == 0
        replay = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert replay.status_code == status.HTTP_404_NOT_FOUND

        # Audit trail entry for the confirmed document merge.
        events = (
            (await db_session.execute(select(ArtifactEventLogModel))).scalars().all()
        )
        doc_events = [e for e in events if e.artifact_type == "uploaded_document"]
        assert len(doc_events) == 1
        assert doc_events[0].action == "CREATE"

        doc_row = (
            (await db_session.execute(select(DocumentModel))).scalars().one()
        )
        assert doc_row.extraction_status == "extraction_applied"
        doc_row = (
            (await db_session.execute(select(DocumentModel))).scalars().one()
        )
        assert doc_row.extraction_status == "extraction_applied"

    @pytest.mark.asyncio
    async def test_cancel_discards_draft_writes_nothing(
        self, seeded_project, client, db_session, monkeypatch, fake_gatherer
    ):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 60)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_SIZE", 30)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_OVERLAP", 5)

        doc = await _upload_doc(client, seeded_project)
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200
        pending_id = proc.json()["pending_action_id"]

        cancel = await client.post(
            f"/api/cancel-action/{pending_id}", params={"project_id": seeded_project}
        )
        assert cancel.status_code == 200

        # Draft discarded — nothing was EVER written.
        assert await _count(db_session, PendingActionModel) == 0
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0
        assert await _count(db_session, AcceptanceCriteriaModel) == 0
        assert await _count(db_session, RequirementStateModel) == 0
        doc_row = (
            await db_session.execute(
                select(DocumentModel).where(DocumentModel.id == doc["id"])
            )
        ).scalar_one()
        assert doc_row.extraction_status == "not_extracted"

    @pytest.mark.asyncio
    async def test_full_mode_single_pass_when_under_budget(
        self, seeded_project, client, monkeypatch, fake_gatherer
    ):
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 100000)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)

        doc = await _upload_doc(client, seeded_project)
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200
        payload = proc.json()
        assert payload["mode"] == "full"
        assert payload["chunk_count"] == 1
        assert len(fake_gatherer["chunks"]) == 1

    # =====================================================================
    # Extraction performance/correctness guards
    # =====================================================================
    async def _progress_frames(self, project_id: str) -> list:
        """Return every document_extraction_progress frame for `project_id` from
        the in-process event bus (ordered by publish seq)."""
        frames = []
        for _, payload in event_manager._history.get(project_id, ()):
            message = json.loads(payload)
            if message["event"] == "document_extraction_progress":
                frames.append(message["data"])
        return frames

    @pytest.mark.asyncio
    async def test_progress_reports_input_chunk_tokens_not_output_dicts(
        self, seeded_project, client, monkeypatch, fake_gatherer
    ):
        """The SSE progress frame must carry the cumulative INPUT chunk tokens
        (previously it re-encoded the gatherer OUTPUT dicts, degrading to a
        meaningless len(dict)//4 heuristic)."""
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 60)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_SIZE", 30)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_OVERLAP", 5)

        body = "# Spec\n\n" + "Detail line.\n" * 40
        doc = await _upload_doc(client, seeded_project, body=body)
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200

        frames = await self._progress_frames(seeded_project)
        assert frames, "expected per-chunk progress frames"
        assert [f["chunk_index"] for f in frames] == list(range(1, len(frames) + 1))

        # Real cumulative input tokens: monotonic and finishing at the sum of the
        # document's chunk token counts (duplicates the route's own math).
        expected_chunks = split_markdown_into_chunks(
            body,
            chunk_size=30,
            overlap=5,
            token_count=count_tokens_local(body),
        )
        expected_total = sum(count_tokens_local(c) for c in expected_chunks)
        assert expected_total > 10, "sanity: the chunk metric must be meaningful"
        values = [f["processed_tokens"] for f in frames]
        assert all(b >= a for a, b in zip(values, values[1:])), "must be monotonic"
        assert values[-1] == expected_total

    @pytest.mark.asyncio
    async def test_concurrent_chunked_extraction_preserves_results_and_draft(
        self, seeded_project, client, db_session, monkeypatch, fake_gatherer
    ):
        """DOCUMENT_EXTRACTION_CONCURRENCY > 1 must run the same gatherer passes,
        keep the merged draft deduplicated, and still write NOTHING until confirm."""
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 60)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_SIZE", 30)
        monkeypatch.setattr(settings, "DOCUMENT_CHUNK_OVERLAP", 5)
        monkeypatch.setattr(settings, "DOCUMENT_EXTRACTION_CONCURRENCY", 3)

        doc = await _upload_doc(client, seeded_project)
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200
        payload = proc.json()
        assert payload["mode"] == "chunked"
        assert payload["chunk_count"] > 1

        # Every chunk was handed to the (fake) gatherer, one pass per chunk.
        assert len(fake_gatherer["chunks"]) == payload["chunk_count"]

        action_rows = (
            (await db_session.execute(select(PendingActionModel))).scalars().all()
        )
        assert len(action_rows) == 1
        proposed = action_rows[0].proposed_changes
        flat_titles = [
            s["story_title"]
            for req in proposed["requirements"]
            for s in req.get("user_stories", [])
        ]
        assert len(flat_titles) == len(set(flat_titles)), "duplicates leaked into draft"

        # NOTHING was persisted to requirement tables — draft only!
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0
        assert await _count(db_session, AcceptanceCriteriaModel) == 0
# =====================================================================
# DELETE — permanent removal from the knowledge base
# =====================================================================
class TestDeleteDocument:
    @pytest.mark.asyncio
    async def test_delete_removes_document_and_no_drafts_yet(
        self, seeded_project, client, db_session
    ):
        """Deleting a document without a draft just removes the stored record."""
        doc = await _upload_doc(client, seeded_project)
        assert await _count(db_session, DocumentModel) == 1

        resp = await client.delete(f"/api/project/{seeded_project}/documents/{doc['id']}")
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["status"] == "deleted"
        assert body["document_id"] == doc["id"]
        assert body["original_filename"] == doc["original_filename"]

        assert await _count(db_session, DocumentModel) == 0
        listing = await client.get(f"/api/project/{seeded_project}/documents")
        assert listing.json() == []

        # Second delete -> 404 (already gone).
        again = await client.delete(f"/api/project/{seeded_project}/documents/{doc['id']}")
        assert again.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_delete_discards_staged_draft_but_not_confirmations(
        self, seeded_project, client, db_session, monkeypatch, fake_gatherer
    ):
        """Deleting a processed document drops its DRAFT pending_action."""
        monkeypatch.setattr(settings, "MAX_CONTEXT_TOKENS", 600)
        monkeypatch.setattr(settings, "DOCUMENT_BUDGET_FRACTION", 0.6)

        doc = await _upload_doc(client, seeded_project)
        proc = await client.post(
            f"/api/project/{seeded_project}/documents/{doc['id']}/process"
        )
        assert proc.status_code == 200
        assert await _count(db_session, PendingActionModel) == 1

        resp = await client.delete(f"/api/project/{seeded_project}/documents/{doc['id']}")
        assert resp.status_code == status.HTTP_200_OK

        # Document AND its draft are both gone.
        assert await _count(db_session, DocumentModel) == 0
        assert await _count(db_session, PendingActionModel) == 0
        # Nothing was ever written to requirement tables (draft-only contract).
        assert await _count(db_session, RequirementModel) == 0
        assert await _count(db_session, UserStoryModel) == 0

    @pytest.mark.asyncio
    async def test_delete_invalid_uuid_and_missing_document(
        self, seeded_project, client
    ):
        bad = await client.delete(f"/api/project/{seeded_project}/documents/not-a-uuid")
        assert bad.status_code == status.HTTP_400_BAD_REQUEST

        also_bad = await client.delete(
            f"/api/project/not-a-uuid/documents/not-a-uuid"
        )
        assert also_bad.status_code == status.HTTP_400_BAD_REQUEST

        ghost = await client.delete(
            f"/api/project/{seeded_project}/documents/00000000-0000-0000-0000-000000000000"
        )
        assert ghost.status_code == status.HTTP_404_NOT_FOUND

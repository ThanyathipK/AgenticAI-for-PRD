"""
Tests for the derived Requirement Traceability Matrix
(:mod:`app.traceability_service` + ``GET /api/project/{id}/traceability``).

Covers:
  - extract_codes: REQ-/US- code extraction (case-insensitive, word-bounded)
  - the matrix links: requirement -> user stories -> acceptance criteria via FKs
  - PRD section links derived from REQ-/US- codes inside ``prd_sections.content``
  - diagram links derived from codes inside ``prd_documents.mermaid_diagram``
  - ``traced`` flag semantics (stories + criteria + at least one section)
  - coverage gaps: stories without criteria, requirements without diagrams,
    and stale code references pointing at non-existent artifacts
  - the HTTP route: 200 with a full matrix, 400 invalid UUID, 404 unknown project

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_traceability.py -v
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.models import (
    AcceptanceCriteriaModel,
    EpicModel,
    PRDDocumentModel,
    PRDSectionModel,
    ProjectModel,
    RequirementModel,
    UserModel,
    UserStoryModel,
)
from app.traceability_service import TraceabilityService, extract_codes


# =====================================================================
# Fixtures: isolated SQLite database + seeded artifacts (same pattern
# as tests/test_latex_service.py so no live Supabase is touched).
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_trace.db")
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
    """Seed user + project; returns the project id string."""
    user = UserModel(
        email="trace-test@banking.com",
        full_name="Trace Tester",
        role="Developer",
    )
    db_session.add(user)
    await db_session.flush()
    project = ProjectModel(
        user_id=user.id,
        name="Traceability Test Project",
        industry_standard="Krungsri Nimble Baseline",
    )
    db_session.add(project)
    await db_session.flush()
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
# Seed helper: a small but complete project:
#   REQ-001 (epic A) -> US-001 (2 criteria) + US-002 (no criteria)
#   REQ-002 (no epic) -> US-003 (1 criterion)
#   PRD section 'business_overview' mentions REQ-001 + US-002
#   PRD section 'product_scope' mentions REQ-002 + stale REQ-999
#   PRD v1 diagram mentions REQ-001
#   PRD v2 diagram mentions US-001 + stale REQ-777
# =====================================================================
async def seed_full_project(db_session, project_id: str):
    pid = uuid.UUID(project_id)
    user = (await db_session.execute(
        select(ProjectModel).where(ProjectModel.id == pid)
    )).scalar_one()
    epic = EpicModel(project_id=pid, epic_name="PromptPay Refunds", version=1)
    db_session.add(epic)
    await db_session.flush()

    req1 = RequirementModel(
        project_id=pid, epic_id=epic.id, requirement_code="REQ-001",
        title="Refund initiation", description="Users can start refunds.",
    )
    req2 = RequirementModel(
        project_id=pid, requirement_code="REQ-002",
        title="Refund status tracking", description="Track refund progress.",
    )
    db_session.add_all([req1, req2])
    await db_session.flush()

    us1 = UserStoryModel(
        project_id=pid, requirement_id=req1.id, ticket_code="US-001",
        story_title="Start refund", as_a="customer",
        i_want_to="start a refund", so_that="I get my money back",
    )
    us2 = UserStoryModel(
        project_id=pid, requirement_id=req1.id, ticket_code="US-002",
        story_title="Attach receipt", as_a="customer",
        i_want_to="attach a receipt", so_that="the refund is verified",
    )
    us3 = UserStoryModel(
        project_id=pid, requirement_id=req2.id, ticket_code="US-003",
        story_title="View status", as_a="customer",
        i_want_to="view refund status", so_that="I know the progress",
    )
    db_session.add_all([us1, us2, us3])
    await db_session.flush()

    db_session.add_all(
        [
            AcceptanceCriteriaModel(user_story_id=us1.id, criteria_text="Given a valid QR, when scanning, then refund opens"),
            AcceptanceCriteriaModel(user_story_id=us1.id, criteria_text="Given an invalid QR, when scanning, then error shown"),
            AcceptanceCriteriaModel(user_story_id=us3.id, criteria_text="Given an open refund, when opening status, then progress is displayed"),
        ]
    )

    db_session.add_all(
        [
            PRDSectionModel(
                project_id=pid, section_key="business_overview", title="Business Overview",
                content="### Business Overview\nRefund initiation is REQ-001 (see US-002).",
                section_order=1,
            ),
            PRDSectionModel(
                project_id=pid, section_key="product_scope", title="Product Scope",
                content="### Product Scope\nTrack progress REQ-002. Legacy ref REQ-999.",
                section_order=2,
            ),
        ]
    )

    db_session.add_all(
        [
            PRDDocumentModel(
                project_id=pid, version=1, prd_markdown="v1 body",
                mermaid_diagram="sequenceDiagram\n  Note over REQ-001: refund flow",
            ),
            PRDDocumentModel(
                project_id=pid, version=2, prd_markdown="v2 body",
                mermaid_diagram="sequenceDiagram\n  Note over US-001: scan QR\n  Note over REQ-777: legacy",
            ),
        ]
    )


# =====================================================================
# Service-level tests
# =====================================================================
class TestExtractCodes:
    def test_finds_req_and_us_codes_case_insensitive(self):
        assert extract_codes("see REQ-001 and us-002 please") == {"REQ-001", "US-002"}

    def test_word_bounded_no_prefix_bleed(self):
        # "REQ-12345" must not match (5 digits > 4); "XREQ-001" is not a code.
        assert extract_codes("REQ-12345 XREQ-001") == set()

    def test_empty_text(self):
        assert extract_codes("") == set()
        assert extract_codes(None) == set()


@pytest.mark.asyncio
async def test_matrix_links_requirements_stories_criteria(db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert matrix["project_id"] == seeded_project
    assert len(matrix["rows"]) == 2

    row1 = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-001")
    assert row1["requirement"]["epic_name"] == "PromptPay Refunds"
    assert [s["ticket_code"] for s in row1["user_stories"]] == ["US-001", "US-002"]
    us1 = row1["user_stories"][0]
    assert len(us1["acceptance_criteria"]) == 2
    us2 = row1["user_stories"][1]
    assert us2["acceptance_criteria"] == []
    # REQ-001 fails the "traced" gate: US-002 has no acceptance criteria.
    assert row1["traced"] is False

    row2 = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-002")
    assert row2["requirement"]["epic_name"] is None
    assert len(row2["user_stories"]) == 1
    assert row2["traced"] is True


@pytest.mark.asyncio
async def test_matrix_surfaces_criteria_invalidated_by_story_change(db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)
    criterion = (await db_session.execute(
        select(AcceptanceCriteriaModel).where(AcceptanceCriteriaModel.criteria_text.like("Given a valid QR%"))
    )).scalar_one()
    criterion.change_type = "review_required"
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    row = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-001")
    story = next(s for s in row["user_stories"] if s["ticket_code"] == "US-001")
    assert story["criteria_needing_review"] == [criterion.criteria_text]
    assert matrix["coverage"]["criteria_needing_review"] == ["US-001"]


@pytest.mark.asyncio
async def test_prd_section_and_diagram_links(db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    row1 = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-001")
    # 'business_overview' mentions REQ-001 directly.
    assert "business_overview" in row1["prd_sections"]
    # PRD v1 diagram mentions REQ-001 directly; PRD v2 mentions US-001 (story ref).
    assert row1["diagrams"] == ["PRD v1", "PRD v2"]

    row2 = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-002")
    assert row2["prd_sections"] == ["product_scope"]
    assert row2["diagrams"] == []

    assert [d["label"] for d in matrix["diagrams"]] == ["PRD v1", "PRD v2"]


@pytest.mark.asyncio
async def test_coverage_gaps_and_stale_references(db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    coverage = matrix["coverage"]

    assert coverage["total_requirements"] == 2
    assert coverage["total_user_stories"] == 3
    assert coverage["total_acceptance_criteria"] == 3
    assert coverage["traced_requirements"] == 1

    # REQ-001 *is* referenced by a section (via REQ-001 + US-002 mentions)...
    assert "REQ-001" not in coverage["requirements_without_prd_sections"]
    # ...but REQ-001's US-002 lacks criteria, and REQ-002 has no diagram ref.
    assert coverage["stories_without_criteria"] == ["US-002"]
    assert coverage["requirements_without_diagram"] == ["REQ-002"]
    assert coverage["requirements_without_stories"] == []

    # Codes that reference artifacts which do not exist in the project.
    stale_sections = {ref["code"]: ref["section_keys"] for ref in coverage["stale_section_references"]}
    assert stale_sections == {"REQ-999": ["product_scope"]}
    stale_diagrams = {ref["code"]: ref["diagrams"] for ref in coverage["stale_diagram_references"]}
    assert stale_diagrams == {"REQ-777": ["PRD v2"]}


@pytest.mark.asyncio
async def test_empty_project_yields_empty_matrix(db_session, seeded_project):
    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert matrix["rows"] == []
    assert matrix["coverage"]["total_requirements"] == 0
    assert matrix["coverage"]["traced_requirements"] == 0
    assert matrix["coverage"]["stale_section_references"] == []

    await db_session.commit()


# =====================================================================
# Route-level tests (GET /api/project/{id}/traceability)
# =====================================================================
@pytest.mark.asyncio
async def test_route_returns_matrix(client, db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)

    resp = await client.get(f"/api/project/{seeded_project}/traceability")
    assert resp.status_code == 200
    body = resp.json()

    assert body["project_id"] == seeded_project
    assert len(body["rows"]) == 2
    codes = {r["requirement"]["requirement_code"] for r in body["rows"]}
    assert codes == {"REQ-001", "REQ-002"}

    coverage = body["coverage"]
    assert coverage["total_requirements"] == 2
    assert coverage["traced_requirements"] == 1
    assert coverage["stories_without_criteria"] == ["US-002"]


@pytest.mark.asyncio
async def test_route_rejects_invalid_uuid(client):
    resp = await client.get("/api/project/not-a-uuid/traceability")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid project_id format"


@pytest.mark.asyncio
async def test_route_unknown_project_returns_404(client, db_session):
    resp = await client.get(f"/api/project/{uuid.uuid4()}/traceability")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"



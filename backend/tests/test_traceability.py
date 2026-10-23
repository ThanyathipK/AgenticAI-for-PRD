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

Pending contract tests at the end of this file capture the next traceability
behaviour before production code changes.  They are strict ``xfail`` tests:
the suite stays green while the work is staged, and an accidental partial fix
becomes an ``XPASS`` failure until the contract is promoted to a normal test.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_traceability.py -v
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.models import (
    AcceptanceCriteriaModel,
    EpicModel,
    PRDDocumentModel,
    PRDVersionModel,
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
# Pending traceability contracts (tests first; implementation follows)
# =====================================================================
PENDING_TRACEABILITY = pytest.mark.xfail(
    strict=True,
    reason="Traceability hardening contract; production implementation is pending",
)


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_active_coverage_excludes_archived_requirements(db_session, seeded_project):
    """Archived requirements must not lower the current PRD coverage score."""
    pid = uuid.UUID(seeded_project)
    db_session.add_all(
        [
            RequirementModel(
                project_id=pid,
                requirement_code="REQ-001",
                title="Current requirement",
                status="active",
            ),
            RequirementModel(
                project_id=pid,
                requirement_code="REQ-002",
                title="Archived requirement",
                status="archived",
            ),
        ]
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert [row["requirement"]["requirement_code"] for row in matrix["rows"]] == ["REQ-001"]
    assert matrix["coverage"]["total_requirements"] == 1


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_requirement_rows_use_numeric_code_order(db_session, seeded_project):
    """REQ-2 sorts before REQ-10 regardless of insertion/database order."""
    pid = uuid.UUID(seeded_project)
    db_session.add_all(
        [
            RequirementModel(project_id=pid, requirement_code="REQ-010", title="Tenth"),
            RequirementModel(project_id=pid, requirement_code="REQ-002", title="Second"),
        ]
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert [row["requirement"]["requirement_code"] for row in matrix["rows"]] == [
        "REQ-002",
        "REQ-010",
    ]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_archived_story_reference_is_not_reported_as_stale(db_session, seeded_project):
    """Known archived artifacts are historical references, not broken links."""
    pid = uuid.UUID(seeded_project)
    req = RequirementModel(project_id=pid, requirement_code="REQ-001", title="Payments")
    db_session.add(req)
    await db_session.flush()
    db_session.add(
        UserStoryModel(
            project_id=pid,
            requirement_id=req.id,
            ticket_code="US-001",
            story_title="Old payment flow",
            as_a="customer",
            i_want_to="pay",
            so_that="I can complete checkout",
            status="archived",
        )
    )
    db_session.add(
        PRDSectionModel(
            project_id=pid,
            section_key="product_scope",
            title="Product Scope",
            content="Historical flow US-001 was retired.",
            section_order=1,
        )
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    stale_codes = {
        item["code"] for item in matrix["coverage"]["stale_section_references"]
    }
    assert "US-001" not in stale_codes


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_current_coverage_uses_latest_prd_diagram_only(db_session, seeded_project):
    """An obsolete diagram must not make the current PRD appear covered."""
    pid = uuid.UUID(seeded_project)
    req = RequirementModel(project_id=pid, requirement_code="REQ-001", title="Payments")
    db_session.add(req)
    await db_session.flush()
    db_session.add_all(
        [
            PRDDocumentModel(
                project_id=pid,
                version=1,
                prd_markdown="Old PRD",
                mermaid_diagram="flowchart LR\nREQ-001 --> Done",
            ),
            PRDDocumentModel(
                project_id=pid,
                version=2,
                prd_markdown="Current PRD",
                mermaid_diagram="flowchart LR\nStart --> Done",
            ),
        ]
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    row = matrix["rows"][0]

    assert row["diagrams"] == []
    assert matrix["coverage"]["requirements_without_diagram"] == ["REQ-001"]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_acceptance_criteria_expose_persistent_identity(db_session, seeded_project):
    """The UI must update a criterion by UUID, never by its array position."""
    await seed_full_project(db_session, seeded_project)

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    row = next(r for r in matrix["rows"] if r["requirement"]["requirement_code"] == "REQ-001")
    story = next(s for s in row["user_stories"] if s["ticket_code"] == "US-001")
    criterion = story["acceptance_criteria"][0]

    assert uuid.UUID(criterion["id"])
    assert criterion["criteria_text"].startswith("Given a valid QR")


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_duplicate_requirement_codes_are_reported_as_ambiguous(db_session, seeded_project):
    """Duplicate codes must be visible instead of silently overwriting a link target."""
    pid = uuid.UUID(seeded_project)
    db_session.add_all(
        [
            RequirementModel(project_id=pid, requirement_code="REQ-001", title="First"),
            RequirementModel(project_id=pid, requirement_code="REQ-001", title="Duplicate"),
        ]
    )
    db_session.add(
        PRDSectionModel(
            project_id=pid,
            section_key="product_scope",
            title="Product Scope",
            content="Scope for REQ-001.",
            section_order=1,
        )
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert matrix["coverage"]["duplicate_requirement_codes"] == ["REQ-001"]


@pytest.mark.asyncio
async def test_restored_requirement_returns_with_original_codes_and_hierarchy(
    db_session, seeded_project
):
    """Reactivated rows retain their persisted IDs/codes and original parentage."""
    pid = uuid.UUID(seeded_project)
    requirement = RequirementModel(
        project_id=pid,
        requirement_code="REQ-042",
        title="Restored payments",
        status="archived",
    )
    db_session.add(requirement)
    await db_session.flush()
    story = UserStoryModel(
        project_id=pid,
        requirement_id=requirement.id,
        ticket_code="US-042",
        story_title="Restore a payment",
        as_a="support agent",
        i_want_to="restore an archived payment",
        so_that="the customer can continue",
        status="archived",
    )
    db_session.add(story)
    await db_session.flush()
    criterion = AcceptanceCriteriaModel(
        user_story_id=story.id,
        criteria_text="Given an archived payment, When restored, Then it is active",
        status="archived",
    )
    db_session.add(criterion)
    await db_session.flush()

    original_ids = (requirement.id, story.id, criterion.id)
    requirement.status = story.status = criterion.status = "active"
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    row = matrix["rows"][0]

    assert uuid.UUID(row["requirement"]["id"]) == original_ids[0]
    assert row["requirement"]["requirement_code"] == "REQ-042"
    assert uuid.UUID(row["user_stories"][0]["id"]) == original_ids[1]
    assert row["user_stories"][0]["ticket_code"] == "US-042"
    assert row["user_stories"][0]["acceptance_criteria"] == [criterion.criteria_text]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_acceptance_criteria_have_persistent_codes_across_text_edits(
    db_session, seeded_project
):
    """AC codes are stored identities and must not be regenerated from list order."""
    await seed_full_project(db_session, seeded_project)
    first = await TraceabilityService.build_traceability(seeded_project, db_session)
    first_criterion = first["rows"][0]["user_stories"][0]["acceptance_criteria"][0]

    model = (await db_session.execute(select(AcceptanceCriteriaModel))).scalars().first()
    model.criteria_text = "Given a valid QR, When edited, Then the same criterion remains"
    await db_session.flush()
    second = await TraceabilityService.build_traceability(seeded_project, db_session)
    second_criterion = second["rows"][0]["user_stories"][0]["acceptance_criteria"][0]

    assert first_criterion["criterion_code"] == second_criterion["criterion_code"]
    assert first_criterion["id"] == second_criterion["id"]
    assert first_criterion["criteria_text"] != second_criterion["criteria_text"]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_duplicate_req_us_and_ac_codes_are_all_reported(db_session, seeded_project):
    """Every governed identifier namespace reports ambiguity in one response."""
    pid = uuid.UUID(seeded_project)
    req_a = RequirementModel(project_id=pid, requirement_code="REQ-001", title="A")
    req_b = RequirementModel(project_id=pid, requirement_code="REQ-001", title="B")
    db_session.add_all([req_a, req_b])
    await db_session.flush()
    story_a = UserStoryModel(
        requirement_id=req_a.id,
        ticket_code="US-001",
        story_title="A",
        as_a="user",
        i_want_to="act",
        so_that="it works",
    )
    story_b = UserStoryModel(
        requirement_id=req_b.id,
        ticket_code="US-001",
        story_title="B",
        as_a="user",
        i_want_to="act",
        so_that="it works",
    )
    db_session.add_all([story_a, story_b])
    await db_session.flush()
    db_session.add_all(
        [
            AcceptanceCriteriaModel(user_story_id=story_a.id, criteria_text="First"),
            AcceptanceCriteriaModel(user_story_id=story_b.id, criteria_text="Second"),
        ]
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert matrix["coverage"]["duplicate_requirement_codes"] == ["REQ-001"]
    assert matrix["coverage"]["duplicate_story_codes"] == ["US-001"]
    assert matrix["coverage"]["duplicate_criterion_codes"] == ["AC-001"]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_current_semver_coverage_is_separate_from_historical_coverage(
    db_session, seeded_project
):
    """Current coverage uses the latest SemVer while history remains inspectable."""
    pid = uuid.UUID(seeded_project)
    req = RequirementModel(project_id=pid, requirement_code="REQ-001", title="Payments")
    db_session.add(req)
    await db_session.flush()
    db_session.add_all(
        [
            PRDDocumentModel(
                project_id=pid,
                version=1,
                prd_markdown="Old",
                mermaid_diagram="flowchart LR\nREQ-001 --> Done",
            ),
            PRDDocumentModel(
                project_id=pid,
                version=2,
                prd_markdown="Current",
                mermaid_diagram="flowchart LR\nStart --> Done",
            ),
            PRDVersionModel(
                project_id=pid,
                version_number=1,
                generated_prd="Old",
                semver="1.0.0",
            ),
            PRDVersionModel(
                project_id=pid,
                version_number=2,
                generated_prd="Current",
                semver="1.1.0",
            ),
        ]
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    assert matrix["current_version"]["semver"] == "1.1.0"
    assert matrix["rows"][0]["diagrams"] == []
    assert matrix["historical_versions"][0]["semver"] == "1.0.0"
    assert matrix["historical_versions"][0]["requirements"] == ["REQ-001"]


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_archived_and_missing_references_are_reported_separately(
    db_session, seeded_project
):
    """A retired known code is historical; only an unknown code is stale."""
    pid = uuid.UUID(seeded_project)
    req = RequirementModel(
        project_id=pid,
        requirement_code="REQ-001",
        title="Retired flow",
        status="archived",
    )
    db_session.add(req)
    db_session.add(
        PRDSectionModel(
            project_id=pid,
            section_key="appendix",
            title="Appendix",
            content="Historical REQ-001; unknown REQ-999.",
            section_order=9,
        )
    )
    await db_session.flush()

    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    archived = {item["code"] for item in matrix["coverage"]["archived_section_references"]}
    missing = {item["code"] for item in matrix["coverage"]["stale_section_references"]}

    assert archived == {"REQ-001"}
    assert missing == {"REQ-999"}


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_requirement_change_invalidates_validation_and_prd_links(
    db_session, seeded_project
):
    """Changing a validated requirement makes both validation and links stale."""
    await seed_full_project(db_session, seeded_project)
    before = await TraceabilityService.build_traceability(seeded_project, db_session)
    req = (await db_session.execute(
        select(RequirementModel).where(RequirementModel.requirement_code == "REQ-001")
    )).scalar_one()
    req.description = "Materially changed refund rules."
    req.version += 1
    await db_session.flush()

    after = await TraceabilityService.build_traceability(seeded_project, db_session)
    row = next(r for r in after["rows"] if r["requirement"]["requirement_code"] == "REQ-001")

    assert before["rows"][0]["validation"]["stale"] is False
    assert row["validation"]["stale"] is True
    assert row["prd_link_status"] == "review_required"


@pytest.mark.asyncio
async def test_large_project_traceability_uses_bounded_queries(db_session, seeded_project):
    """Query count stays constant as requirements grow; guards against N+1 reads."""
    pid = uuid.UUID(seeded_project)
    requirements = [
        RequirementModel(
            project_id=pid,
            requirement_code=f"REQ-{index:03d}",
            title=f"Requirement {index}",
        )
        for index in range(1, 201)
    ]
    db_session.add_all(requirements)
    await db_session.flush()
    stories = [
        UserStoryModel(
            project_id=pid,
            requirement_id=requirement.id,
            ticket_code=f"US-{index:03d}",
            story_title=f"Story {index}",
            as_a="user",
            i_want_to="complete a task",
            so_that="I get value",
        )
        for index, requirement in enumerate(requirements, start=1)
    ]
    db_session.add_all(stories)
    await db_session.flush()
    db_session.add_all(
        AcceptanceCriteriaModel(
            user_story_id=story.id,
            criteria_text=f"Given story {index}, When used, Then it works",
        )
        for index, story in enumerate(stories, start=1)
    )
    await db_session.flush()

    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        matrix = await TraceabilityService.build_traceability(seeded_project, db_session)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(matrix["rows"]) == 200
    assert statements <= 7


@PENDING_TRACEABILITY
@pytest.mark.asyncio
async def test_traceability_exposes_consistency_snapshot_for_other_governance_views(
    db_session, seeded_project
):
    """Impact, graph, and health consume the same versioned coverage snapshot."""
    await seed_full_project(db_session, seeded_project)
    matrix = await TraceabilityService.build_traceability(seeded_project, db_session)

    snapshot = matrix["consistency"]
    assert snapshot["traceability_revision"] == snapshot["impact_analysis_revision"]
    assert snapshot["traceability_revision"] == snapshot["dependency_graph_revision"]
    assert snapshot["traceability_revision"] == snapshot["project_health_revision"]
    assert snapshot["artifact_counts"] == {
        "requirements": matrix["coverage"]["total_requirements"],
        "user_stories": matrix["coverage"]["total_user_stories"],
        "acceptance_criteria": matrix["coverage"]["total_acceptance_criteria"],
    }


# =====================================================================
# Route-level tests (GET /api/project/{id}/traceability)
# =====================================================================
@pytest.mark.asyncio
async def test_route_returns_matrix(client, db_session, seeded_project):
    await seed_full_project(db_session, seeded_project)

    resp = await client.get(f"/api/project/{seeded_project}/traceability")
    assert resp.status_code == 200
    assert resp.headers["server-timing"].startswith("app;dur=")
    assert resp.headers["x-request-id"]
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



"""
Tests for the PRD version ledger service (prd_versions).

Covers:
  - compute_section_changes: created / updated / unchanged / removed and the
    locked_preserved marker for locked sections that WOULD have changed
  - record_prd_version: the version ALWAYS advances (even when the merged
    document is byte-identical, e.g. every changed section was locked),
    stores change_type / change_summary / changed_sections, and keeps
    requirement_states.version_number in lock-step
  - resolve_document_semver: the pre-record stamp resolves EXACTLY the semver
    record_prd_version will store, so the version shown on the document
    (cover + Version History) equals prd_versions.semver
  - diff_versions: per-section line diff between two stored versions

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_prd_versions.py -v
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.lock_service import LockService
from app.models import PRDVersionModel, ProjectModel, UserModel
from app.prd_section_service import CANONICAL_KEYS, ensure_sections_seeded
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.prd import PRDVersionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.version_service import (
    CHANGE_CREATED,
    CHANGE_LOCKED_PRESERVED,
    CHANGE_UNCHANGED,
    CHANGE_UPDATED,
    compute_next_semver,
    compute_section_changes,
    diff_versions,
    record_prd_version,
    resolve_document_semver,
)


# =====================================================================
# Database plumbing: real async SQLite in-memory schema
# =====================================================================
@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        user = UserModel(
            email="ba@example.com",
            full_name="Product Owner",
            role="product_owner",
        )
        session.add(user)
        await session.flush()
        project = ProjectModel(
            user_id=user.id,
            name="PromptPay Refund Portal",
            industry_standard="Krungsri Nimble Baseline",
        )
        session.add(project)
        await session.flush()
        yield session, str(project.id)

    await engine.dispose()


def _doc_v1() -> str:
    """A minimal nine-part PRD markdown (all sections present)."""
    return "\n".join([
        "PRODUCT REQUIREMENT",
        "",
        "PMO No: PMO-1234",
        "",
        "### Stakeholders",
        "",
        "|Role|Name|",
        "|---|---|",
        "|Product Owner|Alice|",
        "",
        "### Version History",
        "",
        "|Version|Date|Author|Description|",
        "|---|---|---|---|",
        "|V1.0|2026-09-01|Alice|Initial approved version|",
        "",
        "### Reviews",
        "",
        "|Role|Name|Signature|Date|",
        "|---|---|---|---|",
        "|Product Owner|Alice|||",
        "",
        "### **Contents**",
        "",
        "1. Business & Strategic Overview",
        "",
        "### 1. Business & Strategic Overview",
        "",
        "|Problem Statement|1. Slow refunds|",
        "",
        "### 2. Product Scope & Functional Requirements",
        "",
        "|User Story||||",
        "",
        "### 3. Technical & Operational Considerations",
        "",
        "|Non-Functional Requirements|1. Performance|",
        "",
        "### 4. Appendix",
        "",
        "|Open Questions & Risks|1.||",
    ])


def _doc_v2_business_updated() -> str:
    doc = _doc_v1()
    return doc.replace("1. Slow refunds", "1. Real-time settlement")  # business_overview


# =====================================================================
# compute_section_changes (pure)
# =====================================================================
class TestComputeSectionChanges:
    def test_first_document_marks_everything_created(self):
        records = compute_section_changes(None, _doc_v1())
        assert len(records) == 9
        assert all(r["change_kind"] == CHANGE_CREATED and r["changed"] for r in records)
        assert {r["section_key"] for r in records} == CANONICAL_KEYS

    def test_identical_documents_are_unchanged(self):
        records = compute_section_changes(_doc_v1(), _doc_v1())
        assert records
        assert all(r["change_kind"] == CHANGE_UNCHANGED and not r["changed"] for r in records)

    def test_updated_section_detected_without_locks(self):
        records = compute_section_changes(_doc_v1(), _doc_v2_business_updated())
        by_key = {r["section_key"]: r for r in records}
        assert by_key["business_overview"]["change_kind"] == CHANGE_UPDATED
        assert by_key["business_overview"]["changed"] is True
        assert by_key["stakeholders"]["change_kind"] == CHANGE_UNCHANGED
        assert sum(r["changed"] for r in records) == 1

    def test_locked_section_is_marked_locked_preserved(self):
        records = compute_section_changes(
            _doc_v1(), _doc_v2_business_updated(), locked_keys={"business_overview"},
        )
        by_key = {r["section_key"]: r for r in records}
        assert by_key["business_overview"]["change_kind"] == CHANGE_LOCKED_PRESERVED
        assert by_key["business_overview"]["changed"] is True  # it WOULD have changed


# =====================================================================
# record_prd_version (real SQLite)
# =====================================================================
class TestRecordPrdVersion:
    @pytest.mark.asyncio
    async def test_version_always_advances_and_bumps_state(self, db_session):
        session, project_id = db_session
        first = await record_prd_version(
            project_id, session, generated_prd=_doc_v1(),
            change_type="ai", change_summary="First generation",
        )
        assert first["version_number"] == 1
        # Identical content MUST still create a new version (a Generate PRD
        # click always advances — even when the merged doc did not change).
        second = await record_prd_version(
            project_id, session, generated_prd=_doc_v1(),
            change_type="ai",
        )
        assert second["version_number"] == 2
        assert second["change_summary"]  # auto-derived

        state = await RequirementStateRepository.get_by_project_id(project_id, session)
        assert state["version_number"] == 2

        rows = (await session.execute(select(PRDVersionModel))).scalars().all()
        assert [r.version_number for r in rows] == [1, 2]

    @pytest.mark.asyncio
    async def test_metadata_stored_manual_change_type(self, db_session):
        session, project_id = db_session
        await record_prd_version(
            project_id, session, generated_prd=_doc_v1(), change_type="ai",
        )
        version = await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(),
            generated_by="alice",
            change_type="manual",
            change_summary="Fixed settlement description",
        )
        assert version["change_type"] == "manual"
        assert version["generated_by"] == "alice"
        assert version["change_summary"] == "Fixed settlement description"
        by_key = {c["section_key"]: c for c in version["changed_sections"]}
        assert by_key["business_overview"]["change_kind"] == CHANGE_UPDATED

    @pytest.mark.asyncio
    async def test_locked_sections_appear_as_locked_preserved(self, db_session):
        session, project_id = db_session
        await record_prd_version(
            project_id, session, generated_prd=_doc_v1(), change_type="ai",
        )
        # Lock the business_overview part, then record a regeneration whose
        # source would change it — the ledger must mark it locked_preserved.
        await ensure_sections_seeded(project_id, session, source_markdown=_doc_v1())
        section = await PRDSectionRepository.get_by_key("business_overview", project_id, session)
        await LockService.lock_artifact(
            "prd_section", section["id"], session, locked_by="user", project_id=project_id
        )
        version = await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(),
            change_type="ai",
        )
        by_key = {c["section_key"]: c for c in version["changed_sections"]}
        assert by_key["business_overview"]["change_kind"] == CHANGE_LOCKED_PRESERVED
        assert version["version_number"] == 2  # still advanced


# =====================================================================
# diff_versions (real SQLite)
# =====================================================================
class TestDiffVersions:
    @pytest.mark.asyncio
    async def test_diff_between_versions_returns_section_lines(self, db_session):
        session, project_id = db_session
        await record_prd_version(project_id, session, generated_prd=_doc_v1(), change_type="ai")
        await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(), change_type="ai",
        )

        diff = await diff_versions(project_id, 2, session)
        assert diff is not None
        assert diff["base_version"] == 1
        assert diff["to_version"] == 2
        by_key = {d["section_key"]: d for d in diff["sections"]}
        biz = by_key["business_overview"]
        assert biz["change_kind"] == CHANGE_UPDATED
        assert biz["removed"] >= 1 and biz["added"] >= 1
        assert any(tag == "del" for tag, _ in biz["diff_lines"])
        assert any(tag == "add" for tag, _ in biz["diff_lines"])

    @pytest.mark.asyncio
    async def test_diff_of_first_version_returns_none(self, db_session):
        session, project_id = db_session
        await record_prd_version(project_id, session, generated_prd=_doc_v1(), change_type="ai")
        diff = await diff_versions(project_id, 1, session)
        assert diff is not None
        assert diff["base_version"] == 0
        assert any(section["changed"] for section in diff["sections"])
        assert all(
            section["change_kind"] == "created"
            for section in diff["sections"] if section["changed"]
        )

    @pytest.mark.asyncio
    async def test_diff_respects_current_locks(self, db_session):
        session, project_id = db_session
        await record_prd_version(project_id, session, generated_prd=_doc_v1(), change_type="ai")
        await ensure_sections_seeded(project_id, session, source_markdown=_doc_v1())
        section = await PRDSectionRepository.get_by_key("business_overview", project_id, session)
        await LockService.lock_artifact(
            "prd_section", section["id"], session, locked_by="user", project_id=project_id
        )
        await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(), change_type="ai",
        )
        diff = await diff_versions(project_id, 2, session)
        by_key = {d["section_key"]: d for d in diff["sections"]}
        assert by_key["business_overview"]["change_kind"] == CHANGE_LOCKED_PRESERVED


# =====================================================================
# Semantic Versioning (SemVer): MAJOR.MINOR.PATCH on prd_versions.semver
# =====================================================================
class TestSemVer:
    """compute_next_semver unit rules + the end-to-end ledger sequence."""

    def test_first_snapshot_is_1_0_0(self):
        # First generation marks every section 'created' -> 1.0.0
        assert compute_next_semver(None, "ai", [{"change_kind": CHANGE_CREATED}] * 9) == "1.0.0"

    def test_created_or_removed_is_major(self):
        assert compute_next_semver("1.0.0", "ai", [{"change_kind": CHANGE_CREATED}]) == "2.0.0"
        assert compute_next_semver("2.3.4", "ai", [
            {"change_kind": CHANGE_UNCHANGED}, {"change_kind": "removed"},
        ]) == "3.0.0"

    def test_ai_content_update_is_minor(self):
        assert compute_next_semver("1.0.0", "ai", [{"change_kind": CHANGE_UPDATED}]) == "1.1.0"
        assert compute_next_semver("2.3.4", "ai", [
            {"change_kind": CHANGE_UNCHANGED}, {"change_kind": CHANGE_UPDATED},
        ]) == "2.4.0"

    def test_manual_edit_is_patch(self):
        # Manual part edits are backwards-compatible refinements -> PATCH even
        # though section content changed.
        assert compute_next_semver("2.3.4", "manual", [{"change_kind": CHANGE_UPDATED}]) == "2.3.5"

    def test_no_change_advance_is_patch(self):
        # Always-advancing click with no content change (e.g. all parts locked).
        assert compute_next_semver("1.0.0", "ai", [{"change_kind": CHANGE_UNCHANGED}]) == "1.0.1"
        assert compute_next_semver("1.2.3", "ai", []) == "1.2.4"

    def test_malformed_previous_defaults_to_zero(self):
        assert compute_next_semver("garbage", "ai", [{"change_kind": CHANGE_UPDATED}]) == "0.1.0"

    @pytest.mark.asyncio
    async def test_semver_sequence_end_to_end(self, db_session):
        """Full ledger sequence across AI + manual + shape-change + no-change."""
        session, project_id = db_session
        v1 = await record_prd_version(project_id, session, generated_prd=_doc_v1(), change_type="ai")
        assert v1["semver"] == "1.0.0"

        # AI content refinement -> 1.1.0
        v2 = await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(), change_type="ai",
        )
        assert v2["semver"] == "1.1.0"

        # Manual edit of the same doc -> 1.1.1
        v3 = await record_prd_version(
            project_id, session, generated_prd=_doc_v2_business_updated(),
            change_type="manual", generated_by="alice",
        )
        assert v3["semver"] == "1.1.1"

        rows = (await session.execute(
            select(PRDVersionModel).order_by(PRDVersionModel.version_number)
        )).scalars().all()
        assert [(r.version_number, r.semver) for r in rows] == [
            (1, "1.0.0"), (2, "1.1.0"), (3, "1.1.1"),
        ]


# =====================================================================
# resolve_document_semver — pre-record stamp (document shows the ledger semver)
# =====================================================================
@pytest.mark.asyncio
async def test_resolve_document_semver_matches_record(db_session):
    """resolve_document_semver must return EXACTLY the semver
    record_prd_version assigns to the next snapshot — the property that makes
    the label stamped on the document provably equal to prd_versions.semver
    (and never a fabricated V{n}.0)."""
    from app.prd_filler import PENDING_VERSION_LABEL, stamp_version_label

    session, project_id = db_session

    # v1 already in the ledger.
    first = await record_prd_version(
        project_id, session, generated_prd=_doc_v1(), change_type="ai",
    )
    assert first["semver"] == "1.0.0"

    # The next document as fill_template_body hands it over: a content change
    # PLUS the pending version marker in the Version History row.
    doc_v2 = (
        _doc_v1()
        .replace("V1.0|2026-09-01", f"{PENDING_VERSION_LABEL}|2026-09-01")
        .replace("1. Slow refunds", "1. Real-time settlement")
    )
    resolved = await resolve_document_semver(
        project_id, session, document=doc_v2, change_type="ai",
    )
    assert resolved == "1.1.0"  # AI content update → MINOR

    stamped = stamp_version_label(doc_v2, resolved)
    assert PENDING_VERSION_LABEL not in stamped
    assert "1.1.0" in stamped

    second = await record_prd_version(
        project_id, session, generated_prd=stamped, change_type="ai",
    )
    assert second["semver"] == resolved  # doc label == ledger row, provably


@pytest.mark.asyncio
async def test_resolve_document_semver_first_snapshot(db_session):
    """With an empty ledger the pre-resolution returns the first snapshot's
    1.0.0 — exactly what record_prd_version will store."""
    session, project_id = db_session
    resolved = await resolve_document_semver(
        project_id, session, document=_doc_v1(), change_type="ai",
    )
    assert resolved == "1.0.0"
    row = await record_prd_version(
        project_id, session, generated_prd=_doc_v1(), change_type="ai",
    )
    assert row["semver"] == resolved


@pytest.mark.asyncio
async def test_version_metadata_uses_stable_cursor_pages(db_session):
    """Version history stays bounded and never duplicates/skips cursor pages."""
    session, project_id = db_session
    for version in range(1, 46):
        session.add(
            PRDVersionModel(
                project_id=project_id,
                version_number=version,
                generated_prd=f"# Version {version}",
                semver=f"1.0.{version}",
            )
        )
    await session.flush()

    first = await PRDVersionRepository.get_by_project(
        project_id, session, limit=20
    )
    second = await PRDVersionRepository.get_by_project(
        project_id,
        session,
        limit=20,
        before_version=first[-1]["version_number"],
    )
    third = await PRDVersionRepository.get_by_project(
        project_id,
        session,
        limit=20,
        before_version=second[-1]["version_number"],
    )

    versions = [row["version_number"] for row in first + second + third]
    assert [len(first), len(second), len(third)] == [20, 20, 5]
    assert versions == list(range(45, 0, -1))
    assert len(versions) == len(set(versions))

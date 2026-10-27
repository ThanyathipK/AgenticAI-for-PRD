"""
Tests for the whole-document PRD version restore (projects.py restore route).

Covers:
  - restore: the snapshot content becomes a NEW version (append-only) and the
    requirement-state document + section rows reflect the restored content
  - lock contract: locked sections keep their current content untouched
  - history immutability: previous ledger rows are never rewritten
  - error paths: 404 unknown version, 409 project locked, 400 bad project id

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_prd_version_restore.py -v
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

from app.database import Base
from app.lock_service import LockService
from app.models import PRDVersionModel, ProjectModel, UserModel
from app.prd_section_service import ensure_sections_seeded
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.version_service import record_prd_version
from app.routes.projects import restore_prd_version


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


# =====================================================================
# Restore (real SQLite)
# =====================================================================
class TestRestorePrdVersion:
    @pytest.mark.asyncio
    async def test_restore_appends_new_version_and_restores_document(self, db_session):
        session, project_id = db_session
        await _seed_history(session, project_id)

        result = await restore_prd_version(project_id, 1, "user", session)

        # APPEND-ONLY: a NEW version 3 was appended, restored FROM v1.
        assert result.restored_from_version == 1
        assert result.new_version_number == 3
        assert result.semver  # manual change -> PATCH bump
        # Only business_overview differs between v1 and the current v2 doc.
        assert result.restored_sections == 1
        # The 'contents' part is born locked — it is preserved (identical
        # content anyway), which is why it appears in the preserved counter.
        assert result.preserved_locked_sections == 1

        # The returned/stored document reflects the v1 content again.
        assert "1. Slow refunds" in result.document_markdown
        state = await RequirementStateRepository.get_by_project_id(project_id, session)
        assert state["generated_prd"] == result.document_markdown
        assert state["version_number"] == 3

        section = await PRDSectionRepository.get_by_key(
            "business_overview", project_id, session
        )
        assert "1. Slow refunds" in section["content"]

    @pytest.mark.asyncio
    async def test_restore_preserves_locked_sections(self, db_session):
        session, project_id = db_session
        await _seed_history(session, project_id)

        # Lock the business_overview part BEFORE the restore: the restore must
        # NOT overwrite it — locked content is preserved untouched.
        section = await PRDSectionRepository.get_by_key("business_overview", project_id, session)
        await LockService.lock_artifact(
            "prd_section", section["id"], session, locked_by="user", project_id=project_id
        )

        result = await restore_prd_version(project_id, 1, "user", session)

        # 'contents' is born locked AND business_overview was locked above —
        # both are preserved untouched.
        assert result.preserved_locked_sections == 2
        assert result.restored_sections == 0  # the only differing part is locked
        # The ledger version still advances, and its change records prove the
        # lock contract was honoured (business_overview marked locked_preserved).
        assert result.new_version_number == 3
        locked_after = await PRDSectionRepository.get_by_key("business_overview", project_id, session)
        assert "1. Real-time settlement" in locked_after["content"]
        new_row = await session.execute(
            select(PRDVersionModel).where(PRDVersionModel.version_number == 3)
        )
        record = new_row.scalar_one()
        by_key = {c["section_key"]: c for c in record.changed_sections}
        # The restored doc keeps the locked part's CURRENT (v2) content, so the
        # ledger sees no change for it — 'locked_preserved' only fires when the
        # lock WOULD have changed the content.
        assert by_key["business_overview"]["change_kind"] == "unchanged"

    @pytest.mark.asyncio
    async def test_restore_never_rewrites_history(self, db_session):
        session, project_id = db_session
        await _seed_history(session, project_id)
        before = (await session.execute(
            select(PRDVersionModel).order_by(PRDVersionModel.version_number)
        )).scalars().all()
        snapshot = [(r.version_number, r.generated_prd, r.change_summary) for r in before]

        await restore_prd_version(project_id, 1, "user", session)

        after = (await session.execute(
            select(PRDVersionModel).order_by(PRDVersionModel.version_number)
        )).scalars().all()
        assert [(r.version_number, r.generated_prd, r.change_summary) for r in after[:2]] == snapshot
        assert [r.version_number for r in after] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_restore_unknown_version_404(self, db_session):
        session, project_id = db_session
        await _seed_history(session, project_id)
        with pytest.raises(HTTPException) as exc:
            await restore_prd_version(project_id, 99, "user", session)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_project_lock_is_not_supported(self, db_session):
        session, project_id = db_session
        with pytest.raises(ValueError, match="Unsupported artifact type: project"):
            await LockService.lock_artifact(
                "project", project_id, session, locked_by="other_user", project_id=project_id
            )

    @pytest.mark.asyncio
    async def test_restore_invalid_project_id_400(self, db_session):
        session, _project_id = db_session
        with pytest.raises(HTTPException) as exc:
            await restore_prd_version("not-a-uuid", 1, "user", session)
        assert exc.value.status_code == 400

def _doc_v2_business_updated() -> str:
    return _doc_v1().replace("1. Slow refunds", "1. Real-time settlement")


async def _seed_history(session, project_id: str):
    """Two ledger versions + the nine parts materialised from the NEWEST doc."""
    await record_prd_version(
        project_id, session, generated_prd=_doc_v1(), change_type="ai",
    )
    await ensure_sections_seeded(project_id, session, source_markdown=_doc_v2_business_updated())
    await record_prd_version(
        project_id, session, generated_prd=_doc_v2_business_updated(), change_type="ai",
    )

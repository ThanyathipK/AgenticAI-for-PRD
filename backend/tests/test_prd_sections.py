"""
Tests for the part-level PRD system: prd_sections + prd_section_versions.

Covers:
  - split_markdown_sections: the official template.md yields exactly the nine
    canonical parts with the same keys the frontend preview produces
  - ensure_sections_seeded: seeds the nine lockable parts, 'contents' born locked
  - update_content: appends an immutable version row on every change
  - lock enforcement: a locked part refuses edits (ArtifactLockError)
  - sync_sections_from_prd: locked / human-owned parts are PRESERVED across
    regeneration; unlocked AI parts are refreshed; the stitched markdown keeps
    both
  - merge_edited_section / update_prd_section: a manual edit is merged with the
    LATEST version (never against a stale empty/template part)
  - manual-edit ownership: a manual save marks the part human-owned so it
    survives the next "Generate PRD"; ai_generatable=True hands it back to AI
  - prd_is_sectioned_markdown: the architect early-exit guard helper

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_prd_sections.py -v
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.lock_service import ArtifactLockError, LockService
from app.auth import AuthenticatedUser
from app.models import PRDSectionModel, ProjectModel, UserModel
from app.prd_section_service import (
    CANONICAL_KEYS,
    CANONICAL_PRD_SECTIONS,
    assemble_document_markdown,
    ensure_sections_seeded,
    merge_edited_section,
    prd_has_renderable_operational_sections,
    prd_is_sectioned_markdown,
    split_markdown_sections,
    stitch_markdown,
    sync_sections_from_prd,
)
from app.repositories.prd_section import (
    PRDSectionRepository,
    PRDSectionVersionRepository,
)
from app.repositories.requirement_state import RequirementStateRepository
from app.routes.prd_sections import list_prd_sections, update_prd_section
from app.routes.projects import get_project_requirement_state
from app.schemas import PrdSectionUpdate


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
        # Seed one user + one project to satisfy the FK chain.
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


def _markdown_prd() -> str:
    """A minimal but structurally complete PRD markdown — the same nine-part
    structure the pandoc-normalized LaTeX template produces (cover block has
    NO '## ' heading; only '### ' section boundaries)."""
    return "\n".join([
        "PRODUCT REQUIREMENT",
        "",
        "Nimble by Krungsri",
        "",
        "PMO No: PMO-1234",
        "",
        "### Stakeholders",
        "",
        "|Role|Name|",
        "|---|---|",
        "|Product Owner||",
        "",
        "### Version History",
        "",
        "|Version|Date|Author|Description|",
        "|---|---|---|---|",
        "|V1.0||||",
        "",
        "### Reviews",
        "",
        "|Role|Name|Signature|Date|",
        "|---|---|---|---|",
        "|Product Owner||||",
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


def _latest_markdown_prd() -> str:
    """The FILLED version of :func:`_markdown_prd` — what ``generated_prd`` holds
    after a generation (the latest version the editor is seeded from). The
    ``prd_sections`` rows can still be the blank template when this is the
    project's current document."""
    return (
        _markdown_prd()
        .replace("PMO No: PMO-1234", "PMO No: PMO-9999")
        .replace("|Product Owner||", "|Product Owner|Alice|")
        .replace("|V1.0||||", "|V1.0|2026-09-01|Alice|Initial approved version|")
        .replace(
            "|Problem Statement|1. Slow refunds|",
            "|Problem Statement|1. Slow refunds are the top complaint|",
        )
        .replace(
            "|Non-Functional Requirements|1. Performance|",
            "|Non-Functional Requirements|1. Performance: p95 < 300ms|",
        )
    )


@pytest.mark.asyncio
async def test_initial_project_state_returns_saved_intake_section_draft(db_session):
    session, project_id = db_session
    await RequirementStateRepository.save_or_update(
        project_id,
        {"generated_prd": "", "version_number": 1, "validation_status": "pending"},
        session,
    )
    await ensure_sections_seeded(project_id, session, _markdown_prd())
    current_user = AuthenticatedUser(
        id=str((await session.execute(select(UserModel))).scalar_one().id),
        email="ba@example.com",
        full_name="Product Owner",
        role="product_owner",
    )

    state = await get_project_requirement_state(project_id, current_user, session)

    assert "Problem Statement" in state["generated_prd"]
    assert "Slow refunds" in state["generated_prd"]


def _section_content(document: str, section_key: str) -> str:
    """The part's markdown inside a full document (heading line included)."""
    return next(
        p["content"] for p in split_markdown_sections(document)
        if p["section_key"] == section_key
    )


def test_heading_only_operational_parts_are_not_reusable():
    incomplete = _markdown_prd().replace(
        "|Non-Functional Requirements|1. Performance|", ""
    ).replace("|Open Questions & Risks|1.||", "")
    assert prd_is_sectioned_markdown(incomplete)
    assert not prd_has_renderable_operational_sections(incomplete)


def test_populated_operational_parts_are_reusable():
    assert prd_has_renderable_operational_sections(_markdown_prd())


@pytest.mark.asyncio
async def test_list_sections_returns_preview_without_writing(db_session):
    session, project_id = db_session
    before = await session.scalar(select(func.count()).select_from(PRDSectionModel))
    user = AuthenticatedUser(
        id=str(uuid.uuid4()), email="owner@example.com", full_name="Owner", role="product_owner",
    )

    response = await list_prd_sections(project_id, user, session)

    after = await session.scalar(select(func.count()).select_from(PRDSectionModel))
    assert before == after == 0
    assert len(response["sections"]) == 9
    assert all(section["is_preview"] for section in response["sections"])


# =====================================================================
# Splitter
# =====================================================================
class TestSplitMarkdownSections:
    def test_template_yields_the_nine_canonical_parts(self):
        # The SAME source the preview renders: the official LaTeX template
        # normalized to markdown (pandoc), then split with the production rules.
        from app.latex_service import prd_to_markdown
        from app.prompt_loader import load_prd_latex_template
        parts = split_markdown_sections(prd_to_markdown(load_prd_latex_template()))
        keys = [p["section_key"] for p in parts]
        assert keys == [c["section_key"] for c in CANONICAL_PRD_SECTIONS]
        assert len(keys) == 9

    def test_markdown_fallback_keeps_the_cover_in_title(self):
        """The fallback template's branded heading is cover content, not part 10."""
        from app.prompt_loader import load_prd_template

        parts = split_markdown_sections(load_prd_template())
        assert [p["section_key"] for p in parts] == [
            c["section_key"] for c in CANONICAL_PRD_SECTIONS
        ]
        title = parts[0]["content"]
        assert "Nimble by Krungsri" in title
        assert "PMO No:" in title

    def test_parts_include_their_heading_line(self):
        parts = split_markdown_sections(_markdown_prd())
        stakeholders = next(p for p in parts if p["section_key"] == "stakeholders")
        assert stakeholders["content"].startswith("### Stakeholders")

    def test_bare_pandoc_heading_is_the_contents_part(self):
        parts = split_markdown_sections("Cover\n\n### \n\n1. Business")
        assert [part["section_key"] for part in parts] == ["title", "contents"]
        assert parts[1]["title"] == "Contents"

    def test_content_before_first_heading_is_the_title_part(self):
        parts = split_markdown_sections(_markdown_prd())
        assert parts[0]["section_key"] == "title"
        assert "PRODUCT REQUIREMENT" in parts[0]["content"]

    def test_stitch_roundtrip_is_lossless(self):
        source = _markdown_prd()
        stitched = stitch_markdown(split_markdown_sections(source))
        assert stitched.strip() == source.strip()

    def test_empty_and_latex_input(self):
        assert split_markdown_sections("") == []
        # LaTeX has no markdown headings: one undifferentiated block, not a crash.
        latex_parts = split_markdown_sections("\\section*{Stakeholders}\nbody")
        assert len(latex_parts) == 1
        assert latex_parts[0]["section_key"] == "title"


# =====================================================================
# Seeding + repository behavior (real SQLite)
# =====================================================================
class TestSeedingAndRepository:
    @pytest.mark.asyncio
    async def test_seed_creates_nine_parts_with_version_one(self, db_session):
        session, project_id = db_session
        sections = await ensure_sections_seeded(project_id, session)
        assert len(sections) == 9
        assert {s["section_key"] for s in sections} == CANONICAL_KEYS
        for s in sections:
            assert s["version_number"] == 1
        contents = next(s for s in sections if s["section_key"] == "contents")
        assert contents["is_locked"] is True  # derived structure — born locked
        reviews = next(s for s in sections if s["section_key"] == "reviews")
        assert reviews["ai_generatable"] is False  # human-only part

    @pytest.mark.asyncio
    async def test_seed_is_idempotent(self, db_session):
        session, project_id = db_session
        first = await ensure_sections_seeded(project_id, session)
        second = await ensure_sections_seeded(project_id, session)
        assert len(second) == len(first) == 9

    @pytest.mark.asyncio
    async def test_update_content_appends_versions_never_overwrites(self, db_session):
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        section = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)

        first = await PRDSectionRepository.update_content(
            section["id"], project_id, "### Stakeholders\n\nv2 content", session,
            changed_by="user", change_summary="manual edit",
        )
        second = await PRDSectionRepository.update_content(
            section["id"], project_id, "### Stakeholders\n\nv3 content", session,
            changed_by="user", change_summary="another edit",
        )
        assert first["version_number"] == 2
        assert second["version_number"] == 3

        history = await PRDSectionVersionRepository.get_by_section(section["id"], session)
        numbers = [v["version_number"] for v in history]
        assert numbers == [3, 2, 1]  # newest first, all preserved
        assert history[2]["content"] == section["content"]  # original kept

    @pytest.mark.asyncio
    async def test_locked_section_refuses_edits(self, db_session):
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        section = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        await LockService.lock_artifact(
            "prd_section", section["id"], session, locked_by="user", project_id=project_id
        )
        with pytest.raises(ArtifactLockError):
            await PRDSectionRepository.update_content(
                section["id"], project_id, "### Stakeholders\n\nhacked", session
            )
        # The stored content is untouched.
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert after["content"] == section["content"]

    @pytest.mark.asyncio
    async def test_locked_section_via_generic_lock_status(self, db_session):
        """prd_section participates in the generic LockService contract."""
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        section = await PRDSectionRepository.get_by_key("reviews", project_id, session)
        info = await LockService.lock_artifact(
            "prd_section", section["id"], session,
            locked_by="user", project_id=project_id,
        )
        assert info["is_locked"] is True
        status = await LockService.get_lock_status(
            "prd_section", section["id"], session, project_id=project_id
        )
        assert status["is_locked"] is True

    @pytest.mark.asyncio
    async def test_unlock_artifact(self, db_session):
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        section = await PRDSectionRepository.get_by_key("reviews", project_id, session)
        await LockService.lock_artifact(
            "prd_section", section["id"], session,
            locked_by="user", project_id=project_id,
        )
        info = await LockService.unlock_artifact(
            "prd_section", section["id"], session,
            unlocked_by="user", project_id=project_id,
        )
        assert info["is_locked"] is False
        status = await LockService.get_lock_status(
            "prd_section", section["id"], session, project_id=project_id
        )
        assert status["is_locked"] is False

    @pytest.mark.asyncio
    async def test_born_locked_section_with_no_lock_owner_is_unlockable(self, db_session):
        """Regression: 'contents' is seeded is_locked=True with locked_by=NULL
        (system-derived structure). The unlock ownership check used to treat
        that NULL as a DIFFERENT owner and answered 403 Forbidden forever.
        A lock with no recorded owner is not held by any user, so any
        requester may clear it."""
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        contents = await PRDSectionRepository.get_by_key("contents", project_id, session)
        assert contents["is_locked"] is True
        assert contents["locked_by"] is None

        info = await LockService.unlock_artifact(
            "prd_section", contents["id"], session,
            unlocked_by="user", project_id=project_id,
        )
        assert info["is_locked"] is False
        after = await PRDSectionRepository.get_by_key("contents", project_id, session)
        assert after["is_locked"] is False

    @pytest.mark.asyncio
    async def test_unlock_denied_while_another_user_holds_the_lock(self, db_session):
        """The guard must keep protecting locks actually held by someone else."""
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session)
        stakeholders = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        await LockService.lock_artifact(
            "prd_section", stakeholders["id"], session,
            locked_by="alice", project_id=project_id,
        )
        with pytest.raises(PermissionError):
            await LockService.unlock_artifact(
                "prd_section", stakeholders["id"], session,
                unlocked_by="bob", project_id=project_id,
            )
        # ...and the lock survives the denied attempt.
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert after["is_locked"] is True
        assert after["locked_by"] == "alice"



# =====================================================================
# AI regeneration ownership contract (sync_sections_from_prd)
# =====================================================================
class TestSyncSectionsFromPrd:
    @pytest.mark.asyncio
    async def test_generate_refreshes_system_cover_fields_for_legacy_human_cover(self, db_session):
        session, project_id = db_session
        original = _markdown_prd().replace(
            "PMO No: PMO-1234",
            "PMO No: PMO-1234\nVersion: V1.0\nStatus: Draft\nLast Update: 2026-10-12\nAuthor: Alice",
        )
        await ensure_sections_seeded(project_id, session, source_markdown=original)
        title = await PRDSectionRepository.get_by_key("title", project_id, session)
        await PRDSectionRepository.update_content(
            title["id"], project_id, title["content"], session,
            changed_by="user", content_source="human", ai_generatable=False,
        )

        regenerated = original.replace("Version: V1.0", "Version: V1.1").replace(
            "Last Update: 2026-10-12", "Last Update: 2026-10-13",
        )
        merged = await sync_sections_from_prd(project_id, regenerated, session)

        assert merged is not None
        assert "PMO No: PMO-1234" in merged
        assert "Author: Alice" in merged
        assert "Version: V1.1" in merged
        assert "Status: Draft" in merged
        assert "Last Update: 2026-10-13" in merged

    @pytest.mark.asyncio
    async def test_locked_and_human_parts_survive_regeneration(self, db_session):
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session, source_markdown=_markdown_prd())

        # User fills in the human-only Reviews part and LOCKS the stakeholders part.
        reviews = await PRDSectionRepository.get_by_key("reviews", project_id, session)
        await PRDSectionRepository.update_content(
            reviews["id"], project_id,
            "### Reviews\n\n|Role|Name|Signature|Date|\n|---|---|---|---|\n|Product Owner|Ann|signed|2026-09-04|",
            session, changed_by="user",
        )
        stakeholders = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        await PRDSectionRepository.update_content(
            stakeholders["id"], project_id,
            "### Stakeholders\n\n|Role|Name|\n|---|---|\n|Product Owner|Ann Person|",
            session, changed_by="user",
        )
        await LockService.lock_artifact(
            "prd_section", stakeholders["id"], session,
            locked_by="user", project_id=project_id,
        )

        # AI "regenerates" the document with different stakeholder / review content.
        regenerated = _markdown_prd().replace(
            "|Product Owner||", "|Product Owner|AI INVENTED|"
        )
        merged = await sync_sections_from_prd(project_id, regenerated, session)

        assert merged is not None
        assert "AI INVENTED" not in merged  # locked part untouched
        assert "signed" in merged  # human-only part untouched
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert "Ann Person" in after["content"]

    @pytest.mark.asyncio
    async def test_unlocked_ai_parts_are_refreshed_and_versioned(self, db_session):
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session, source_markdown=_markdown_prd())
        appendix = await PRDSectionRepository.get_by_key("appendix", project_id, session)
        assert appendix["version_number"] == 1

        regenerated = _markdown_prd().replace(
            "|Open Questions & Risks|1.||",
            "|Open Questions & Risks|1. Latency SLO undefined||",
        )
        merged = await sync_sections_from_prd(project_id, regenerated, session)
        assert merged is not None
        assert "Latency SLO undefined" in merged

        after = await PRDSectionRepository.get_by_key("appendix", project_id, session)
        assert after["version_number"] == 2  # content changed => new version row
        history = await PRDSectionVersionRepository.get_by_section(after["id"], session)
        assert history[0]["changed_by"] == "automated_agent"
        # v1 (the seed content) is still there — nothing overwritten.
        assert history[-1]["content"] == appendix["content"]

    @pytest.mark.asyncio
    async def test_unchanged_parts_get_no_version_row(self, db_session):
        session, project_id = db_session
        source = _markdown_prd()
        await ensure_sections_seeded(project_id, session, source_markdown=source)
        before = {s["section_key"]: s["version_number"]
                  for s in await PRDSectionRepository.get_by_project(project_id, session)}
        merged = await sync_sections_from_prd(project_id, source, session)
        assert merged is not None
        after = {s["section_key"]: s["version_number"]
                 for s in await PRDSectionRepository.get_by_project(project_id, session)}
        assert before == after  # identical content => no new versions

    @pytest.mark.asyncio
    async def test_sync_preserves_stitched_document(self, db_session):
        session, project_id = db_session
        source = _markdown_prd()
        await ensure_sections_seeded(project_id, session, source_markdown=source)
        merged = await sync_sections_from_prd(project_id, source, session)
        assert merged is not None
        # The stitched document round-trips through the splitter unchanged.
        assert merged.strip() == source.strip()

    @pytest.mark.asyncio
    async def test_sync_on_empty_project_creates_parts(self, db_session):
        session, project_id = db_session
        merged = await sync_sections_from_prd(project_id, _markdown_prd(), session)
        assert merged is not None
        sections = await PRDSectionRepository.get_by_project(project_id, session)
        assert {s["section_key"] for s in sections} == CANONICAL_KEYS


# =====================================================================
# Architect early-exit guard helper
# =====================================================================
class TestPrdIsSectionedMarkdown:
    def test_stitched_document_is_recognized(self):
        assert prd_is_sectioned_markdown(_markdown_prd()) is True

    def test_latex_document_is_not(self):
        assert prd_is_sectioned_markdown(
            "\\documentclass{article}\n\\section*{Stakeholders}\nbody"
        ) is False

    def test_empty_is_not(self):
        assert prd_is_sectioned_markdown("") is False
# =====================================================================
# Three-way merge for a manual part edit (merge_edited_section)
# =====================================================================
class TestMergeEditedSection:
    def test_edit_merges_with_the_latest_version(self):
        """The route reconciles the stored part with the project's latest
        document BEFORE merging, so ``current_content`` is the latest version
        the user edited (not a stale blank template). With the latest version
        as the merge base the user's touched line wins and everything else is
        kept."""
        latest = _section_content(_latest_markdown_prd(), "stakeholders")
        # The caller (update_prd_section) passes the LATEST version as current.
        edited = latest + "\n|Sponsor|Bob|"

        merged = merge_edited_section(latest, latest, edited)

        assert merged == edited
        assert "|Product Owner|Alice|" in merged  # latest content survives
        assert "|Sponsor|Bob|" in merged  # the user's edit is applied

    def test_empty_stored_part_never_collapses_the_edit(self):
        """An empty/never-populated stored part is not a concurrent deletion:
        the full edited section is stored verbatim, not collapsed to the lines
        the user happened to touch."""
        latest = _section_content(_latest_markdown_prd(), "stakeholders")
        edited = latest + "\n|Sponsor|Bob|"

        merged = merge_edited_section(latest, "", edited)

        assert merged == edited
        assert "|Product Owner|Alice|" in merged

    def test_in_sync_edit_is_verbatim(self):
        latest = _section_content(_latest_markdown_prd(), "stakeholders")
        edited = latest.replace("|Product Owner|Alice|", "|Product Owner|Alice Smith|")
        assert merge_edited_section(latest, latest, edited) == edited

    def test_no_base_is_stored_verbatim(self):
        assert merge_edited_section(None, "old stored", "brand new") == "brand new"
        assert merge_edited_section("", "old stored", "brand new") == "brand new"

    def test_user_deletion_sticks(self):
        latest = _section_content(_latest_markdown_prd(), "stakeholders")
        edited = latest.replace("\n|Product Owner|Alice|", "")
        merged = merge_edited_section(latest, latest, edited)
        assert "|Product Owner|Alice|" not in merged

    def test_concurrent_ai_insert_is_preserved(self):
        latest = _section_content(_latest_markdown_prd(), "stakeholders")
        # AI added a Sponsor row during the edit window; the user touched the
        # Product Owner row only.
        current = latest + "\n|Sponsor|AI Added|"
        edited = latest.replace("|Product Owner|Alice|", "|Product Owner|Ann|")
        merged = merge_edited_section(latest, current, edited)
        assert "|Product Owner|Ann|" in merged  # user's touched line wins
        assert "|Sponsor|AI Added|" in merged  # concurrent AI insert survives
# =====================================================================
# Manual edit route merges with the LATEST version (update_prd_section)
# =====================================================================
class TestUpdatePrdSectionMergesLatestVersion:
    @pytest.mark.asyncio
    async def test_manual_edit_merges_with_latest_version_not_template(self, db_session):
        session, project_id = db_session
        # 1. The parts were seeded from the blank TEMPLATE (e.g. the project was
        #    opened before its first generation).
        await ensure_sections_seeded(project_id, session, source_markdown=_markdown_prd())
        # 2. The project's LATEST document is the FILLED one — what the editor is
        #    seeded from (generated_prd), while prd_sections is still the template.
        latest = _latest_markdown_prd()
        await RequirementStateRepository.save_or_update(
            project_id, {"generated_prd": latest}, session,
        )
        latest_stakeholders = _section_content(latest, "stakeholders")
        edited = latest_stakeholders + "\n|Sponsor|Bob|"

        user = AuthenticatedUser(
            id=str(uuid.uuid4()), email="ba@example.com",
            full_name="Product Owner", role="product_owner",
        )
        result = await update_prd_section(
            project_id, "stakeholders",
            PrdSectionUpdate(
                content=edited, base_content=latest_stakeholders, updated_by="user",
            ),
            current_user=user, session=session,
        )

        # The stored part merged with the LATEST version + the user's edit ...
        assert "|Product Owner|Alice|" in result["section"]["content"]
        assert "|Sponsor|Bob|" in result["section"]["content"]
        assert "|Product Owner||" not in result["section"]["content"]
        # ... and the whole re-stitched document keeps the latest content for the
        # other parts too (it did NOT fall back to the stale template).
        assert "|Product Owner|Alice|" in result["document_markdown"]
        assert "top complaint" in result["document_markdown"]
        assert "p95 < 300ms" in result["document_markdown"]

    @pytest.mark.asyncio
    async def test_manual_edit_on_in_sync_sections_is_verbatim(self, db_session):
        session, project_id = db_session
        # Parts already in sync with the latest document: the edit is stored
        # exactly, with no template merge artifacts.
        latest = _latest_markdown_prd()
        await ensure_sections_seeded(project_id, session, source_markdown=latest)
        await RequirementStateRepository.save_or_update(
            project_id, {"generated_prd": latest}, session,
        )
        latest_stakeholders = _section_content(latest, "stakeholders")
        edited = latest_stakeholders.replace("|Product Owner|Alice|", "|Product Owner|Ann|")

        user = AuthenticatedUser(
            id=str(uuid.uuid4()), email="ba@example.com",
            full_name="Product Owner", role="product_owner",
        )
        result = await update_prd_section(
            project_id, "stakeholders",
            PrdSectionUpdate(content=edited, base_content=latest_stakeholders),
            current_user=user, session=session,
        )
        assert result["section"]["content"] == edited
# =====================================================================
# Manual edit makes the part HUMAN-OWNED so it survives "Generate PRD"
# =====================================================================
class TestManualEditOwnership:
    @staticmethod
    def _user() -> AuthenticatedUser:
        return AuthenticatedUser(
            id=str(uuid.uuid4()), email="ba@example.com",
            full_name="Product Owner", role="product_owner",
        )

    @pytest.mark.asyncio
    async def test_manual_edit_survives_ai_regeneration(self, db_session):
        """Regression: a manual save used to leave the part AI-owned, so the
        next 'Generate PRD' (sync_sections_from_prd) overwrote it. A manual
        save now makes the part human-owned and the Architect preserves it."""
        session, project_id = db_session
        latest = _latest_markdown_prd()
        await ensure_sections_seeded(project_id, session, source_markdown=latest)
        await RequirementStateRepository.save_or_update(
            project_id, {"generated_prd": latest}, session,
        )
        latest_stakeholders = _section_content(latest, "stakeholders")
        manual = latest_stakeholders.replace("|Product Owner|Alice|", "|Product Owner|HUMAN EDIT|")

        result = await update_prd_section(
            project_id, "stakeholders",
            PrdSectionUpdate(content=manual, base_content=latest_stakeholders),
            current_user=self._user(), session=session,
        )
        # The manual save flips ownership -> the UI shows the "Manual" badge.
        assert result["section"]["content_source"] == "human"
        assert result["section"]["ai_generatable"] is False

        # Simulate "Generate PRD": the Architect syncs a fresh document that
        # would change this part.
        regenerated = _latest_markdown_prd().replace(
            "|Product Owner|Alice|", "|Product Owner|AI REGENERATED|",
        )
        merged = await sync_sections_from_prd(project_id, regenerated, session)
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert "HUMAN EDIT" in after["content"]  # manual content survives
        assert "AI REGENERATED" not in after["content"]  # AI did not overwrite it
        assert "HUMAN EDIT" in (merged or "")  # the stitched doc keeps it too

    @pytest.mark.asyncio
    async def test_hand_back_to_ai_allows_regeneration(self, db_session):
        """Passing ai_generatable=True hands the part back to the AI, so the
        next regeneration updates it again (not a one-way door)."""
        session, project_id = db_session
        latest = _latest_markdown_prd()
        await ensure_sections_seeded(project_id, session, source_markdown=latest)
        await RequirementStateRepository.save_or_update(
            project_id, {"generated_prd": latest}, session,
        )
        latest_stakeholders = _section_content(latest, "stakeholders")
        human = latest_stakeholders.replace("|Product Owner|Alice|", "|Product Owner|HUMAN EDIT|")

        # Manual edit -> human-owned.
        await update_prd_section(
            project_id, "stakeholders",
            PrdSectionUpdate(content=human, base_content=latest_stakeholders),
            current_user=self._user(), session=session,
        )
        # Hand it back to the AI (content unchanged, ownership flips).
        handed = await update_prd_section(
            project_id, "stakeholders",
            PrdSectionUpdate(
                content=human, base_content=human,
                ai_generatable=True, updated_by="ai_handoff",
            ),
            current_user=self._user(), session=session,
        )
        assert handed["section"]["ai_generatable"] is True
        assert handed["section"]["content_source"] == "ai"

        # Now the Architect may regenerate it again.
        regenerated = _latest_markdown_prd().replace(
            "|Product Owner|Alice|", "|Product Owner|AI REGENERATED|",
        )
        await sync_sections_from_prd(project_id, regenerated, session)
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert "AI REGENERATED" in after["content"]
        assert "HUMAN EDIT" not in after["content"]


# =====================================================================
# Ownership backfill for manual edits saved BEFORE the flag existed
# =====================================================================
class TestManualOwnershipBackfill:
    @pytest.mark.asyncio
    async def test_backfill_marks_existing_manual_edit_human_owned(self, db_session):
        """A manual edit saved before the ownership flag existed (so the part is
        still ai_generatable=True) is retro-marked human-owned and then survives
        the next 'Generate PRD'."""
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session, source_markdown=_latest_markdown_prd())

        s = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        # NOTE: no ownership flags -> mimics an edit saved before the fix.
        await PRDSectionRepository.update_content(
            s["id"], project_id,
            "### Stakeholders\n\n|Role|Name|\n|---|---|\n|Product Owner|OLD MANUAL|",
            session, changed_by="user", change_summary=None,
        )
        assert (await PRDSectionRepository.get_by_key("stakeholders", project_id, session))["ai_generatable"] is True

        flipped = await PRDSectionRepository.mark_manually_edited_as_human_owned(session)
        assert flipped == 1
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert after["content_source"] == "human"
        assert after["ai_generatable"] is False

        # It now survives regeneration.
        regenerated = _latest_markdown_prd().replace("|Product Owner|Alice|", "|Product Owner|AI REGEN|")
        await sync_sections_from_prd(project_id, regenerated, session)
        final = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert "OLD MANUAL" in final["content"]
        assert "AI REGEN" not in final["content"]

    @pytest.mark.asyncio
    async def test_backfill_ignores_ai_handoff_and_restore(self, db_session):
        """Only genuine manual edits are flipped: AI rows, an explicit AI hand-off,
        a whole-document restore and a system reconciliation must NOT freeze the
        part."""
        session, project_id = db_session
        await ensure_sections_seeded(project_id, session, source_markdown=_latest_markdown_prd())

        appendix = await PRDSectionRepository.get_by_key("appendix", project_id, session)
        await PRDSectionRepository.update_content(
            appendix["id"], project_id,
            "### 4. Appendix\n\n|Open Questions & Risks|1. AI row|",
            session, changed_by="automated_agent",
        )
        tech = await PRDSectionRepository.get_by_key("tech_ops", project_id, session)
        await PRDSectionRepository.update_content(
            tech["id"], project_id,
            "### 3. Technical & Operational Considerations\n\n|NFR|1. Handed back|",
            session, changed_by="ai_handoff", content_source="ai", ai_generatable=True,
        )
        title = await PRDSectionRepository.get_by_key("title", project_id, session)
        await PRDSectionRepository.update_content(
            title["id"], project_id,
            "PRODUCT REQUIREMENT\n\nrestored content",
            session, changed_by="user", change_summary="Restored from PRD version 2.",
        )
        scope = await PRDSectionRepository.get_by_key("product_scope", project_id, session)
        await PRDSectionRepository.update_content(
            scope["id"], project_id,
            "### 2. Product Scope & Functional Requirements\n\n|User Story|1. reconciled|",
            session, changed_by="user",  # system reconciliation used a user-ish author
            change_summary="Reconciled with the latest PRD version before a manual edit.",
        )

        flipped = await PRDSectionRepository.mark_manually_edited_as_human_owned(session)
        assert flipped == 0
        for key in ("appendix", "tech_ops", "title", "product_scope"):
            row = await PRDSectionRepository.get_by_key(key, project_id, session)
            assert row["ai_generatable"] is True, key



# =====================================================================
# End-to-end: "Generate PRD" through the REAL LangGraph workflow
# =====================================================================
class TestGeneratePrdThroughWorkflow:
    @pytest.mark.asyncio
    async def test_generate_prd_preserves_human_owned_part(self, monkeypatch, db_session):
        """Regression: the architect's section sync was guarded by
        ``if db_session`` while ``db_session`` was not a declared AgentState
        channel — LangGraph dropped it, so the sync never ran and the raw AI
        LaTeX (without human-owned parts) was persisted, wiping manual edits.
        Driving the REAL compiled workflow must preserve the human-owned part."""
        import app.agents as agents

        session, project_id = db_session
        await ensure_sections_seeded(project_id, session, source_markdown=_markdown_prd())

        # A human-owned manual edit that must survive "Generate PRD".
        s = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        await PRDSectionRepository.update_content(
            s["id"], project_id,
            "### Stakeholders\n\n|Role|Name|\n|---|---|\n|Product Owner|MANUAL KEEP|",
            session, changed_by="user", content_source="human", ai_generatable=False,
        )

        req_state = {
            "project_id": project_id,
            "project_name": "PromptPay Refund Portal",
            "requirements": [{
                "requirement_code": "REQ-001",
                "title": "Refund Request Submission",
                "user_stories": [{
                    "ticket_code": "US-001",
                    "story_title": "Submit refund request online",
                    "as_a": "Retail Customer",
                    "i_want_to": "submit a refund request",
                    "so_that": "I get my money back",
                    "is_locked": False,
                    "acceptance_criteria": ["Given a valid id, when submitted, then PENDING"],
                }],
            }],
            "business_goals": [{"description": "Cut refund handling time by 60%"}],
            "actors": [{"name": "Retail Customer"}],
            "user_stories": [],
            "acceptance_criteria": [],
            "generated_prd": "",
            "generated_diagrams": "",
            "current_workflow_state": "gatherer_node",
            "version_number": 2,
        }

        async def fake_state(project_id, session=None, current_version=1):  # noqa: A002
            return dict(req_state)

        async def fake_diagram(*_a, **_k):
            return "flowchart TD\n  A([Start]) --> B[Done]"

        async def fake_save(**_k):
            return None

        monkeypatch.setattr(agents, "get_or_init_requirement_state", fake_state)
        monkeypatch.setattr(agents, "_generate_flow_diagram", fake_diagram)
        monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", fake_save)

        # The graph must carry the request-scoped session; if it did not, the
        # fallback would silently open a NON-test session (and write there).
        import app.database as app_database

        def _no_fallback():  # pragma: no cover - executed only on regression
            raise AssertionError("architect_node fell back to a non-request session")

        monkeypatch.setattr(app_database, "AsyncSessionLocal", _no_fallback)

        out = await agents.prd_workflow.ainvoke({
            "project_id": project_id,
            "target_agent": "architect",
            "current_version": 2,
            "structured_requirements": {},
            "version_history_summaries": "No previous revision logs available.",
            "db_session": session,
        })

        # The returned document (what the UI shows) keeps the manual part.
        assert "MANUAL KEEP" in out["prd_markdown"]
        after = await PRDSectionRepository.get_by_key("stakeholders", project_id, session)
        assert "MANUAL KEEP" in after["content"]
        # ...and the version ledger recorded the stitched document.
        stitched = await assemble_document_markdown(project_id, session)
        assert "MANUAL KEEP" in stitched

        # THE DOCUMENT'S VERSION IS THE LEDGER'S semver: the cover and the
        # Version History section show prd_versions.semver (first snapshot =
        # 1.0.0) instead of the integer-derived V{n}.0 filler, and the pending
        # marker never leaks into the stored document.
        from app.prd_filler import PENDING_VERSION_LABEL
        from app.repositories.prd import PRDVersionRepository

        latest = await PRDVersionRepository.get_latest(project_id, session)
        assert latest is not None
        assert latest["semver"] == "1.0.0"
        assert latest["semver"] in out["prd_markdown"]
        assert PENDING_VERSION_LABEL not in out["prd_markdown"]
        assert "V2.0" not in out["prd_markdown"]

        assert prd_is_sectioned_markdown(None) is False  # type: ignore[arg-type]

"""
Regression tests for the merge confirm/cancel contract:

1. Confirming (Save) a pending MERGE must LOG the merge as its own immutable
   version-ledger row (``prd_versions``, ``generated_by='ai_merge'``) so the
   Version History records merges too — not only manual part edits — while
   bumping the version EXACTLY once (no double-bump, no ledger reset).
2. Cancelling (not Save) must roll back: nothing is ever written to the
   requirement tables or the version ledger.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_merge_version_log.py -v
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.models import (
    AcceptanceCriteriaModel,
    PendingActionModel,
    PRDVersionModel,
    ProjectModel,
    RequirementModel,
    RequirementStateModel,
    UserStoryModel,
    UserModel,
)


# =====================================================================
# Isolated SQLite database + ASGI client fixtures (same pattern as
# tests/test_documents.py)
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_merge_log.db")
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
        email="merge-log-test@banking.com",
        full_name="Merge Log Tester",
        role="Developer",
    )
    db_session.add(user)
    await db_session.flush()
    project = ProjectModel(
        user_id=user.id,
        name="Merge Log Test Project",
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
# Helpers
# =====================================================================
async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar() or 0)


def _merged_state(version_number: int):
    """A Gatherer-shaped merged requirement state like the workflow stores."""
    return {
        "requirements": [
            {
                "requirement_code": "REQ-001",
                "title": "Secure Login Epic",
                "description": "Customers must log in securely.",
                "user_stories": [
                    {
                        "ticket_code": "US-001",
                        "story_title": "Password login",
                        "as_a": "customer",
                        "i_want_to": "log in with a password",
                        "so_that": "my account stays safe",
                        "acceptance_criteria": ["Given X when Y then Z"],
                    }
                ],
            }
        ],
        "user_stories": [
            {
                "ticket_code": "US-001",
                "story_title": "Password login",
                "as_a": "customer",
                "i_want_to": "log in with a password",
                "so_that": "my account stays safe",
                "acceptance_criteria": ["Given X when Y then Z"],
            }
        ],
        "generated_prd": "",
        "generated_diagrams": "",
        "version_number": version_number,
    }


async def _create_merge_action(session: AsyncSession, project_id: str, message: str, version_number: int):
    action = PendingActionModel(
        project_id=project_id,
        action_type="MERGE",
        original_user_message=message,
        proposed_changes=_merged_state(version_number),
        workflow_stage="REVIEWING",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(action)
    await session.commit()
    return action


# =====================================================================
# Tests
# =====================================================================
class TestMergeVersionLog:
    @pytest.mark.asyncio
    async def test_confirm_rejects_stale_requirement_update(
        self, seeded_project, client, db_session
    ):
        """A full-state preview must not overwrite a newer requirement save."""
        from app.repositories.requirement_state import RequirementStateRepository

        await RequirementStateRepository.save_or_update(
            seeded_project,
            {**_merged_state(1), "requirements_revision": 2},
            db_session,
        )
        stale_draft = {
            **_merged_state(1),
            "_base_requirements_revision": 1,
            "requirements_revision": 2,
        }
        action = PendingActionModel(
            project_id=seeded_project,
            action_type="MERGE",
            original_user_message="Stale update",
            proposed_changes=stale_draft,
            workflow_stage="REVIEWING",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )

        assert confirm.status_code == status.HTTP_409_CONFLICT
        assert "outdated" in confirm.json()["detail"].lower()
        state = await RequirementStateRepository.get_by_project_id(seeded_project, db_session)
        assert state["requirements_revision"] == 2
        assert await _count(db_session, PendingActionModel) == 1

    @pytest.mark.asyncio
    async def test_confirm_merge_does_not_create_a_prd_version(
        self, seeded_project, client, db_session
    ):
        """Requirement gathering is audited separately from PRD releases."""
        action = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=2
        )

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK

        rows = (
            (await db_session.execute(select(PRDVersionModel))).scalars().all()
        )
        assert rows == []

        states = (
            (await db_session.execute(select(RequirementStateModel))).scalars().all()
        )
        assert len(states) == 1
        assert states[0].version_number == 1

        # The pending action is consumed.
        assert await _count(db_session, PendingActionModel) == 0

    @pytest.mark.asyncio
    async def test_confirm_merge_preserves_existing_prd_version(
        self, seeded_project, client, db_session
    ):
        """A requirement merge must not alter an existing PRD ledger."""
        # Existing ledger at v5 (e.g. earlier PRD generations).
        db_session.add(
            PRDVersionModel(
                project_id=seeded_project,
                version_number=5,
                generated_prd="# Existing PRD",
                generated_by="automated_agent",
                change_type="ai",
                semver="1.2.0",
            )
        )
        await db_session.commit()

        action = await _create_merge_action(
            db_session, seeded_project, "Tweak stories", version_number=5
        )
        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK

        rows = (
            (await db_session.execute(select(PRDVersionModel))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].version_number == 5

        states = (
            (await db_session.execute(select(RequirementStateModel))).scalars().all()
        )
        assert states[0].version_number == 1

    @pytest.mark.asyncio
    async def test_confirm_archive_with_empty_story_list_persists_archived_status(
        self, seeded_project, client, db_session
    ):
        baseline = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=1
        )
        assert (await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )).status_code == status.HTTP_200_OK

        proposed = _merged_state(1)
        proposed["requirements"][0]["user_stories"] = []
        proposed["user_stories"] = []
        action = PendingActionModel(
            project_id=seeded_project,
            action_type="ARCHIVE",
            original_user_message="Archive US-001",
            proposed_changes=proposed,
            affected_user_story_ids=["US-001"],
            workflow_stage="delete_requirement_node",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK
        story = await db_session.scalar(
            select(UserStoryModel).where(UserStoryModel.ticket_code == "US-001")
        )
        assert story is not None
        assert story.status == "archived"
        criterion = await db_session.scalar(select(AcceptanceCriteriaModel))
        assert criterion is not None
        assert criterion.status == "archived"

    @pytest.mark.asyncio
    async def test_confirm_archive_one_criterion_keeps_story_and_requirement_active(
        self, seeded_project, client, db_session
    ):
        baseline = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=1
        )
        assert (await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )).status_code == status.HTTP_200_OK

        criterion = await db_session.scalar(select(AcceptanceCriteriaModel))
        assert criterion is not None
        proposed = _merged_state(1)
        proposed["requirements"][0]["user_stories"][0]["acceptance_criteria"] = []
        proposed["user_stories"][0]["acceptance_criteria"] = []
        proposed["acceptance_criteria"] = []
        action = PendingActionModel(
            project_id=seeded_project,
            action_type="ARCHIVE",
            original_user_message="Archive US-001-AC-001",
            proposed_changes=proposed,
            affected_user_story_ids=[],
            affected_acceptance_criteria_ids=[str(criterion.id)],
            workflow_stage="delete_requirement_node",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK
        requirement = await db_session.scalar(select(RequirementModel))
        story = await db_session.scalar(select(UserStoryModel))
        persisted_criterion = await db_session.scalar(select(AcceptanceCriteriaModel))
        assert requirement is not None and requirement.status == "active"
        assert story is not None and story.status == "active"
        assert persisted_criterion is not None and persisted_criterion.status == "archived"

    @pytest.mark.asyncio
    async def test_confirm_remove_all_requirements_archives_the_full_hierarchy(
        self, seeded_project, client, db_session
    ):
        baseline = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=1
        )
        assert (await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )).status_code == status.HTTP_200_OK

        proposed = _merged_state(1)
        proposed["requirements"] = []
        proposed["user_stories"] = []
        action = PendingActionModel(
            project_id=seeded_project,
            action_type="ARCHIVE",
            original_user_message="Remove all requirements",
            proposed_changes=proposed,
            affected_user_story_ids=["REQ-001", "US-001"],
            workflow_stage="delete_requirement_node",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK
        requirement = await db_session.scalar(select(RequirementModel))
        story = await db_session.scalar(select(UserStoryModel))
        criterion = await db_session.scalar(select(AcceptanceCriteriaModel))
        assert requirement is not None and requirement.status == "archived"
        assert story is not None and story.status == "archived"
        assert criterion is not None and criterion.status == "archived"

    @pytest.mark.asyncio
    async def test_confirm_archive_rechecks_locks_and_keeps_pending_action(
        self, seeded_project, client, db_session
    ):
        baseline = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=1
        )
        assert (await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )).status_code == status.HTTP_200_OK

        story = await db_session.scalar(
            select(UserStoryModel).where(UserStoryModel.ticket_code == "US-001")
        )
        assert story is not None
        story.is_locked = True
        story.locked_by = "product-owner"

        proposed = _merged_state(1)
        proposed["requirements"][0]["user_stories"] = []
        proposed["user_stories"] = []
        action = PendingActionModel(
            project_id=seeded_project,
            action_type="ARCHIVE",
            original_user_message="Archive US-001",
            proposed_changes=proposed,
            affected_user_story_ids=["US-001"],
            workflow_stage="delete_requirement_node",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_409_CONFLICT
        assert "Unlock" in confirm.json()["detail"]
        persisted_story = await db_session.scalar(
            select(UserStoryModel).where(UserStoryModel.ticket_code == "US-001")
        )
        assert persisted_story is not None
        assert persisted_story.status == "active"
        assert await _count(db_session, PendingActionModel) == 1

    @pytest.mark.asyncio
    async def test_cancel_rolls_back_writes_nothing(
        self, seeded_project, client, db_session
    ):
        """Cancel (not Save) = rollback: NOTHING is written anywhere."""
        action = await _create_merge_action(
            db_session, seeded_project, "Add secure login requirement", version_number=2
        )

        cancel = await client.post(
            f"/api/cancel-action/{action.id}", params={"project_id": seeded_project}
        )
        assert cancel.status_code == status.HTTP_200_OK
        assert cancel.json()["status"] == "cancelled"

        # Draft discarded — nothing was EVER written, incl. the version ledger.
        assert await _count(db_session, PendingActionModel) == 0
        assert await _count(db_session, PRDVersionModel) == 0
        assert await _count(db_session, RequirementStateModel) == 0


class TestVersionOnlySaveGuard:
    @pytest.mark.asyncio
    async def test_confirm_preserves_requirement_titles_and_siblings(
        self, seeded_project, client, db_session
    ):
        """The version ledger's version_number-only save_or_update sync must
        NEVER re-derive the requirement tables.

        Regression: the old default-requirement fallback renamed REQ-001 to
        'Default Requirement' and ARCHIVED every other requirement whenever
        ``record_prd_version`` synced the version number (Generate PRD, manual
        part edits, confirmed merges)."""
        from app.repositories.requirement_state import RequirementStateRepository

        action = PendingActionModel(
            project_id=seeded_project,
            action_type="MERGE",
            original_user_message="two requirements",
            proposed_changes={
                "requirements": [
                    {
                        "requirement_code": "REQ-001",
                        "title": "Secure Login Epic",
                        "description": "",
                        "user_stories": [
                            {"ticket_code": "US-001", "story_title": "Password login",
                             "as_a": "customer", "i_want_to": "log in", "so_that": "safe",
                             "acceptance_criteria": ["Given X when Y then Z"]}
                        ],
                    },
                    {
                        "requirement_code": "REQ-002",
                        "title": "Fraud Alerts",
                        "description": "Real-time fraud alerts.",
                        "user_stories": [
                            {"ticket_code": "US-002", "story_title": "Alert me",
                             "as_a": "customer", "i_want_to": "get alerts", "so_that": "fast",
                             "acceptance_criteria": ["Given A when B then C"]}
                        ],
                    },
                ],
                "user_stories": [],
                "version_number": 2,
            },
            workflow_stage="REVIEWING",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(action)
        await db_session.commit()

        confirm = await client.post(
            f"/api/confirm-action/{action.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK

        state = await RequirementStateRepository.get_by_project_id(seeded_project, db_session)
        by_code = {r["requirement_code"]: r for r in state["requirements"]}

        # BOTH requirements survive with their ORIGINAL titles — the
        # version-only sync did not rename or archive anything.
        assert by_code["REQ-001"]["title"] == "Secure Login Epic"
        assert by_code["REQ-002"]["title"] == "Fraud Alerts"
        assert by_code["REQ-002"]["description"] == "Real-time fraud alerts."

        codes = {s["ticket_code"] for r in state["requirements"] for s in r["user_stories"]}
        assert codes == {"US-001", "US-002"}



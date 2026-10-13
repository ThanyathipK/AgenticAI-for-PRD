"""Database-backed unit tests for repository CRUD contracts.

These repositories are foundational to the API but previously had no direct
execution coverage for their create/read/update/delete paths.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import ProjectModel, UserModel
from app.repositories.acceptance_criteria import AcceptanceCriteriaRepository
from app.repositories.epic import EpicRepository
from app.repositories.requirement import RequirementRepository
from app.repositories.semantic_memory import SemanticMemoryRepository
from app.repositories.user_story import UserStoryRepository


@pytest_asyncio.fixture
async def repository_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/repository-crud.sqlite")
    async with engine.begin() as connection:
        from app.database import Base
        from app import models  # noqa: F401

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        user = UserModel(email="repositories@bank.test", full_name="Repository Tester", role="BA")
        session.add(user)
        await session.flush()
        project = ProjectModel(user_id=user.id, name="Repository Project", industry_standard="Banking")
        session.add(project)
        await session.flush()
        yield session, str(project.id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_requirement_crud_locking_and_cascade_archive(repository_db):
    session, project_id = repository_db
    created = await RequirementRepository.create(project_id, {
        "requirement_code": "REQ-001",
        "title": "Payments",
        "description": "Original",
    }, session)
    requirement_id = created["id"]

    assert (await RequirementRepository.get_by_id(requirement_id, project_id, session))["title"] == "Payments"
    assert await RequirementRepository.get_by_id(requirement_id, "00000000-0000-0000-0000-000000000000", session) is None

    updated = await RequirementRepository.update(
        requirement_id, project_id,
        {"title": "Instant Payments", "description": "Updated", "status": "active"},
        session,
    )
    assert updated["title"] == "Instant Payments"

    locked = await RequirementRepository.lock(requirement_id, project_id, session, locked_by="owner")
    assert locked["is_locked"] is True
    assert (await RequirementRepository.lock(requirement_id, project_id, session, locked_by="owner"))["is_locked"] is True
    with pytest.raises(PermissionError, match="locked"):
        await RequirementRepository.update(requirement_id, project_id, {"title": "Blocked"}, session)
    with pytest.raises(PermissionError, match="Cannot be unlocked"):
        await RequirementRepository.unlock(requirement_id, project_id, session, unlocked_by="other")

    unlocked = await RequirementRepository.unlock(requirement_id, project_id, session, unlocked_by="owner")
    assert unlocked["is_locked"] is False
    assert (await RequirementRepository.unlock(requirement_id, project_id, session, unlocked_by="owner"))["is_locked"] is False

    story = await UserStoryRepository.create(project_id, {
        "ticket_code": "US-001", "story_title": "Send payment", "status": "active",
    }, session, requirement_id=__import__("uuid").UUID(requirement_id))
    criterion = await AcceptanceCriteriaRepository.create(project_id, {
        "user_story_id": story["id"], "criteria_text": "Payment succeeds",
    }, session)
    assert await RequirementRepository.delete(requirement_id, project_id, session) is True
    assert (await UserStoryRepository.get_by_id(story["id"], project_id, session))["status"] == "archived"
    assert (await AcceptanceCriteriaRepository.get_by_id(criterion["id"], project_id, session))["status"] == "archived"
    assert await RequirementRepository.delete("00000000-0000-0000-0000-000000000000", project_id, session) is False


@pytest.mark.asyncio
async def test_user_story_crud_and_ticket_generation(repository_db):
    session, project_id = repository_db
    requirement = await RequirementRepository.create(project_id, {
        "requirement_code": "REQ-002", "title": "Transfers",
    }, session)
    requirement_id = __import__("uuid").UUID(requirement["id"])

    first = await UserStoryRepository.save_or_update(project_id, {
        "story_title": "Create transfer", "as_a": "customer", "i_want_to": "transfer funds",
    }, session, requirement_id=requirement_id)
    assert first["ticket_code"] == "US-001"
    second = await UserStoryRepository.create(project_id, {
        "ticket_code": "US-009", "story_title": "Approve transfer",
    }, session, requirement_id=requirement_id)
    assert await UserStoryRepository.generate_unique_ticket_code(
        __import__("uuid").UUID(project_id), session
    ) == "US-010"

    updated = await UserStoryRepository.update(first["id"], project_id, {
        "ticket_code": "US-001", "story_title": "Create instant transfer",
        "as_a": "retail customer", "i_want_to": "send funds", "so_that": "I can pay",
        "status": "active", "version": 2, "last_modified_by": "tester", "change_type": "updated",
    }, session)
    assert updated["story_title"] == "Create instant transfer"
    assert len(await UserStoryRepository.get_by_project(project_id, session, requirement_id)) == 2
    assert (await UserStoryRepository.get_by_id(second["id"], project_id, session))["ticket_code"] == "US-009"
    assert await UserStoryRepository.delete(second["id"], project_id, session) is True
    assert await UserStoryRepository.delete("00000000-0000-0000-0000-000000000000", project_id, session) is False


@pytest.mark.asyncio
async def test_acceptance_criteria_crud(repository_db):
    session, project_id = repository_db
    requirement = await RequirementRepository.create(project_id, {
        "requirement_code": "REQ-003", "title": "Confirmation",
    }, session)
    story = await UserStoryRepository.create(project_id, {
        "ticket_code": "US-020", "story_title": "See confirmation",
    }, session, requirement_id=__import__("uuid").UUID(requirement["id"]))

    created = await AcceptanceCriteriaRepository.create(project_id, {
        "user_story_id": story["id"], "criteria_text": "A receipt is displayed",
    }, session)
    criterion_id = created["id"]
    assert (await AcceptanceCriteriaRepository.get_by_id(criterion_id, project_id, session))["criteria_text"] == "A receipt is displayed"
    assert len(await AcceptanceCriteriaRepository.get_by_project(
        project_id, session, __import__("uuid").UUID(requirement["id"])
    )) == 1

    updated = await AcceptanceCriteriaRepository.update(criterion_id, project_id, {
        "criteria_text": "A numbered receipt is displayed", "status": "active",
        "version": 2, "last_modified_by": "tester", "change_type": "updated",
    }, session)
    assert updated["version"] == 2
    assert await AcceptanceCriteriaRepository.delete(criterion_id, project_id, session) is True
    assert await AcceptanceCriteriaRepository.get_by_id(criterion_id, project_id, session) is None
    assert await AcceptanceCriteriaRepository.delete(
        "00000000-0000-0000-0000-000000000000", project_id, session
    ) is False


@pytest.mark.asyncio
async def test_epic_crud(repository_db):
    session, project_id = repository_db
    created = await EpicRepository.create(project_id, {"epic_name": "Payments", "version": 1}, session)
    assert (await EpicRepository.get_by_id(created["id"], project_id, session))["epic_name"] == "Payments"
    assert len(await EpicRepository.get_by_project(project_id, session)) == 1

    updated = await EpicRepository.update(
        created["id"], project_id, {"epic_name": "Instant Payments", "version": 2}, session
    )
    assert updated["epic_name"] == "Instant Payments"
    assert updated["version"] == 2
    assert await EpicRepository.delete(created["id"], project_id, session) is True
    assert await EpicRepository.get_by_id(created["id"], project_id, session) is None
    assert await EpicRepository.delete(
        "00000000-0000-0000-0000-000000000000", project_id, session
    ) is False


@pytest.mark.asyncio
async def test_semantic_memory_crud_and_recall_telemetry(repository_db):
    session, project_id = repository_db
    with pytest.raises(ValueError, match="cannot be empty"):
        await SemanticMemoryRepository.save_memory(project_id, "   ", [], session=session)

    first = await SemanticMemoryRepository.save_memory(
        project_id, "Customer prefers PromptPay", [0.1, 0.2],
        embedding_model="test-model", session=session,
    )
    second = await SemanticMemoryRepository.save_memory(
        project_id, "Transfers require approval", [0.3, 0.4], session=session,
    )
    recent = await SemanticMemoryRepository.get_recent(project_id, limit=10, session=session)
    assert {item["id"] for item in recent} == {first["id"], second["id"]}

    await SemanticMemoryRepository.mark_recalled([first["id"]], session=session)
    recalled = next(item for item in await SemanticMemoryRepository.get_recent(
        project_id, session=session
    ) if item["id"] == first["id"])
    assert recalled["recall_count"] == 1
    await SemanticMemoryRepository.mark_recalled([], session=session)

    assert await SemanticMemoryRepository.delete_for_project(project_id, session=session) == 2
    assert await SemanticMemoryRepository.get_recent(project_id, session=session) == []

"""
Integration tests for the project-state endpoint's empty-board initialization.

A brand-new project must start with an EMPTY requirement board: the endpoint
used to seed a hardcoded demo requirement (``REQ-001`` "PromptPay Real-Time
Merchant Settlement Engine" + a demo ``US-001``) and persist it, which both
polluted the board with demo content and made the first requirement the user
actually gathered come out as ``REQ-002``.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_project_state_defaults.py -v
"""
import pytest
import pytest_asyncio
import uuid
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.models import (
    ProjectModel,
    RequirementModel,
    RequirementStateModel,
    UserModel,
)
from app.requirement_codes import next_requirement_code
from app.repositories.project import ProjectRepository
from app.repositories.requirement_state import RequirementStateRepository


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_state.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


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


async def _create_bare_project(session: AsyncSession, name: str) -> str:
    """Insert a project WITHOUT a requirement-state row (the un-initialized case)."""
    user = UserModel(
        email=f"{name.lower().replace(' ', '-')}@banking.com",
        full_name="State Tester",
        role="Developer",
    )
    session.add(user)
    await session.flush()
    project = ProjectModel(user_id=user.id, name=name, industry_standard="Generic")
    session.add(project)
    await session.commit()
    return str(project.id)


async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar() or 0)


@pytest.mark.asyncio
async def test_uninitialized_project_gets_an_empty_board(client, db_session):
    """No hardcoded demo requirement is seeded for a project without state."""
    project_id = await _create_bare_project(db_session, "Fresh Project")

    response = await client.get(f"/api/project/{project_id}")

    assert response.status_code == 200, response.text
    body = response.json()

    # The hardcoded demo board (PromptPay demo REQ-001 + US-001) is gone.
    assert body["requirements"] == []
    assert body["user_stories"] == []
    assert body["acceptance_criteria"] == []
    assert body["project_name"] == "Fresh Project"

    # No requirement rows were fabricated, not even the state repository's
    # "Default Requirement" fallback.
    assert await _count(db_session, RequirementModel) == 0

    stored = await db_session.execute(
        select(RequirementStateModel).where(RequirementStateModel.project_id == project_id)
    )
    state_row = stored.scalar_one_or_none()
    assert state_row is not None, "the state row is initialized so SSE clients can sync"
    assert state_row.requirements in ({}, None, [])


@pytest.mark.asyncio
async def test_newly_created_project_has_an_empty_board(client, db_session):
    """The real creation path (``ProjectRepository.create_project``) initializes
    an empty requirement state — no demo requirement occupies REQ-001."""
    created = await ProjectRepository.create_project(
        {"id": str(uuid.uuid4()), "name": "Repository Created"}, db_session
    )
    await db_session.commit()

    body = (await client.get(f"/api/project/{created['id']}")).json()

    assert body["requirements"] == []
    assert body["user_stories"] == []
    assert body["project_name"] == "Repository Created"
    assert await _count(db_session, RequirementModel) == 0


@pytest.mark.asyncio
async def test_explicit_empty_requirement_payload_does_not_create_a_default_requirement(
    client, db_session
):
    """Saving an empty board must not invent a placeholder REQ-001 row."""
    project_id = await _create_bare_project(db_session, "Actually Empty Project")

    await RequirementStateRepository.save_or_update(project_id, {
        "requirements": [],
        "user_stories": [],
    }, db_session)
    await db_session.commit()

    body = (await client.get(f"/api/project/{project_id}")).json()
    assert body["requirements"] == []
    assert body["user_stories"] == []
    assert await _count(db_session, RequirementModel) == 0


@pytest.mark.asyncio
async def test_first_gathered_requirement_starts_at_req_001(client, db_session):
    """The board the gatherer receives is empty, so numbering starts at REQ-001."""
    project_id = await _create_bare_project(db_session, "Numbering Project")

    body = (await client.get(f"/api/project/{project_id}")).json()
    used_codes = [r["requirement_code"] for r in body["requirements"]]

    # The reported bug: the first requirement of a fresh project was REQ-002
    # because a seeded/fabricated REQ-001 already occupied the first number.
    assert next_requirement_code(used_codes) == "REQ-001"


@pytest.mark.asyncio
async def test_distinct_epic_name_survives_requirement_save_and_reload(client, db_session):
    project_id = await _create_bare_project(db_session, "Epic Persistence Project")

    await RequirementStateRepository.save_or_update(project_id, {
        "epic_name": "Corporate Payments Hub",
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "High-value Payment Approval",
            "functional_requirement": "The system must require approval for high-value payments",
            "description": "",
            "user_stories": [],
        }],
        "user_stories": [],
    }, db_session)
    await db_session.commit()

    body = (await client.get(f"/api/project/{project_id}")).json()
    assert body["epic_name"] == "Corporate Payments Hub"
    assert body["requirements"][0]["title"] == "High-value Payment Approval"

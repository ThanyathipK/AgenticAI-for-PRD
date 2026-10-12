"""
Tests for duplicate project-name validation.

Project names must be unique across the workspace: names are compared
case-insensitively and whitespace-trimmed. Creating a project with an existing
name and renaming a project onto another project's name must both fail with
HTTP 409 Conflict (with a user-facing detail message), while renaming a
project to its own name stays allowed. The repository helper
``ProjectRepository.name_exists`` carries the same semantics for the routes.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_project_name_uniqueness.py -v
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import create_access_token, get_password_hash
from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.repositories.project import ProjectRepository
from app.repositories.user import UserRepository
from app.repositories.requirement_state import RequirementStateRepository


# =====================================================================
# Fixtures: isolated SQLite database + ASGI client (same pattern as
# tests/test_latex_service.py so no live Supabase connection is touched).
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_project_names.db")
    async with engine.begin() as conn:
        from app.database import Base
        from app import models  # noqa: F401 - register all models

        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_headers(db_session):
    """A loginable account in the isolated DB plus its Bearer header.

    Project routes now require authentication (GET /api/projects scopes the
    listing to the JWT subject), so every API-level test in this module acts
    as this signed-in user by default.
    """
    user = await UserRepository.create_user(
        session=db_session,
        email="ba@bank.com",
        full_name="Banking BA",
        role="Business Analyst",
        password_hash=get_password_hash("Correct-Horse-1"),
    )
    await db_session.commit()
    token = create_access_token(user_id=str(user.id), email=user.email, role=user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(db_session, auth_headers):
    """ASGI client wired to the isolated SQLite session (no lifespan run).

    Every request carries the signed-in user's Bearer token by default; tests
    that need anonymous access can drop the header per-call.
    """

    async def override_get_db():
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    fastapi_app.dependency_overrides[app_get_db] = override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(app_get_db, None)


# =====================================================================
# Repository-level: ProjectRepository.name_exists
# =====================================================================
@pytest.mark.asyncio
async def test_name_exists_false_without_projects(db_session):
    assert await ProjectRepository.name_exists("Anything", db_session) is False


@pytest.mark.asyncio
async def test_name_exists_true_after_create(db_session):
    await ProjectRepository.create_project({"id": str(uuid.uuid4()), "name": "Payment Hub"}, db_session)

    assert await ProjectRepository.name_exists("Payment Hub", db_session) is True


@pytest.mark.asyncio
async def test_name_exists_is_case_insensitive(db_session):
    await ProjectRepository.create_project({"id": str(uuid.uuid4()), "name": "Payment Hub"}, db_session)

    assert await ProjectRepository.name_exists("payment hub", db_session) is True
    assert await ProjectRepository.name_exists("PAYMENT HUB", db_session) is True


@pytest.mark.asyncio
async def test_name_exists_ignores_surrounding_whitespace(db_session):
    await ProjectRepository.create_project({"id": str(uuid.uuid4()), "name": "Payment Hub"}, db_session)

    assert await ProjectRepository.name_exists("  Payment Hub  ", db_session) is True


@pytest.mark.asyncio
async def test_name_exists_matches_legacy_untrimmed_rows(db_session):
    # Rows saved before canonicalization may carry surrounding whitespace; the
    # check trims the stored value too so those still count as duplicates.
    await ProjectRepository.create_project({"id": str(uuid.uuid4()), "name": "  Payment Hub "}, db_session)

    assert await ProjectRepository.name_exists("Payment Hub", db_session) is True


@pytest.mark.asyncio
async def test_name_exists_exclude_project_id_ignores_self(db_session):
    created = await ProjectRepository.create_project({"id": str(uuid.uuid4()), "name": "Payment Hub"}, db_session)

    assert await ProjectRepository.name_exists("payment hub", db_session, exclude_project_id=created["id"]) is False


@pytest.mark.asyncio
async def test_name_exists_blank_name_is_never_taken(db_session):
    assert await ProjectRepository.name_exists("   ", db_session) is False


# =====================================================================
# API-level: POST /api/projects
# =====================================================================
async def _create_via_api(client: AsyncClient, name: str):
    return await client.post("/api/projects", json={"name": name})


@pytest.mark.asyncio
async def test_create_with_unique_name_succeeds(client):
    resp = await _create_via_api(client, "Payment Hub")

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Payment Hub"


@pytest.mark.asyncio
async def test_create_duplicate_name_conflicts(client):
    await _create_via_api(client, "Payment Hub")

    resp = await _create_via_api(client, "Payment Hub")

    assert resp.status_code == 409
    assert "Payment Hub" in resp.json()["detail"]
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_duplicate_name_case_insensitive(client):
    await _create_via_api(client, "Payment Hub")

    resp = await _create_via_api(client, "payment hub")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_duplicate_name_after_trim(client):
    await _create_via_api(client, "Payment Hub")

    resp = await _create_via_api(client, "   Payment Hub   ")

    assert resp.status_code == 409
    # The stored name stays canonical (trimmed) even though the request wasn't.
    listing = (await client.get("/api/projects")).json()
    assert sorted(p["name"] for p in listing) == ["Payment Hub"]


@pytest.mark.asyncio
async def test_create_whitespace_only_name_rejected(client):
    resp = await _create_via_api(client, "   ")

    assert resp.status_code == 422
    assert "must not be empty" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_create_name_over_50_chars_rejected(client):
    resp = await _create_via_api(client, "A" * 51)

    assert resp.status_code == 422
    assert "50 characters or fewer" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_create_name_padded_over_50_chars_rejected(client):
    # The cap is applied AFTER trimming, so surrounding whitespace must not
    # let an over-long name slip through.
    resp = await _create_via_api(client, "  " + "A" * 51 + " ")

    assert resp.status_code == 422
    assert "50 characters or fewer" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_create_name_exactly_50_chars_succeeds(client):
    resp = await _create_via_api(client, "A" * 50)

    assert resp.status_code == 201
    assert resp.json()["name"] == "A" * 50


@pytest.mark.asyncio
async def test_create_distinct_names_both_persist(client):
    first = await _create_via_api(client, "Payment Hub")
    second = await _create_via_api(client, "Lending Suite")

    assert first.status_code == 201
    assert second.status_code == 201


# =====================================================================
# API-level: PUT /api/projects/{project_id} (rename)
# =====================================================================
async def _seed_two_projects(client: AsyncClient, name_a: str, name_b: str):
    await _create_via_api(client, name_a)
    await _create_via_api(client, name_b)
    listing = (await client.get("/api/projects")).json()
    by_name = {p["name"]: p["id"] for p in listing}
    return by_name[name_a], by_name[name_b]


@pytest.mark.asyncio
async def test_rename_onto_other_project_name_conflicts(client):
    id_a, _ = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    resp = await client.put(f"/api/projects/{id_a}", json={"name": "Lending Suite"})

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rename_onto_other_project_name_case_insensitive(client):
    id_a, _ = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    resp = await client.put(f"/api/projects/{id_a}", json={"name": "lending suite"})

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_failed_rename_leaves_name_unchanged(client):
    id_a, _ = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    await client.put(f"/api/projects/{id_a}", json={"name": "Lending Suite"})

    listing = (await client.get("/api/projects")).json()
    assert sorted(p["name"] for p in listing) == ["Lending Suite", "Payment Hub"]


@pytest.mark.asyncio
async def test_rename_to_own_name_allowed(client):
    id_a, _ = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    resp = await client.put(f"/api/projects/{id_a}", json={"name": "Payment Hub"})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rename_is_reflected_in_requirement_state(client, db_session):
    """The state response resolves its name from projects, not a stale copy."""
    created = await _create_via_api(client, "Payment Hub")
    project_id = created.json()["id"]

    renamed = await client.put(f"/api/projects/{project_id}", json={"name": "Payments Platform"})

    assert renamed.status_code == 200
    state = await RequirementStateRepository.get_by_project_id(project_id, db_session)
    assert state is not None
    assert state["project_name"] == "Payments Platform"


@pytest.mark.asyncio
async def test_rename_over_50_chars_rejected(client):
    id_a, _ = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    resp = await client.put(f"/api/projects/{id_a}", json={"name": "B" * 51})

    assert resp.status_code == 422
    assert "50 characters or fewer" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_rename_to_new_unique_name_succeeds(client):
    _, id_b = await _seed_two_projects(client, "Payment Hub", "Lending Suite")

    resp = await client.put(f"/api/projects/{id_b}", json={"name": "Cards Platform"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "Cards Platform"


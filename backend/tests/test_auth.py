"""
Integration tests for the authentication surface — real database, not fakes.

Covers the full credential lifecycle against a genuine SQLAlchemy engine
(isolated SQLite file per test, same pattern as tests/test_project_name_uniqueness.py
so no live Supabase connection is touched):

* :class:`app.repositories.user.UserRepository` — email canonicalization,
  case-insensitive lookup, UUID coercion safety on ``get_by_id``;
* :mod:`app.auth` — hashing, credential verification, JWT round-trip, and the
  bootstrap account's "no password provisioned" diagnosis;
* ``/api/auth/*`` routes — login, ``/me``, config, register, change-password;
* :func:`app.migrations.seed_default_user` — the startup seeder must leave the
  system account LOGINABLE (bcrypt hash written / back-filled), which is what
  previously made every login attempt return 401.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_auth.py -v
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.migrations as migrations
from app.auth import (
    authenticate_user,
    create_access_token,
    get_password_hash,
    password_hash_is_usable,
    verify_password,
    verify_token,
)
from app.config import settings
from app.database import Base, get_db as app_get_db
from app.main import app as fastapi_app
from app.repositories.user import UserRepository, normalize_email

# A valid bcrypt hash of "Correct-Horse-1" is built per test through
# get_password_hash() rather than hard-coded, so the cost factor stays in sync
# with app.auth.pwd_context.
PASSWORD = "Correct-Horse-1"


# =====================================================================
# Fixtures: isolated SQLite database + ASGI client
# =====================================================================
@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_auth.db")
    async with engine.begin() as conn:
        from app import models  # noqa: F401 - register all models

        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(session_factory):
    """A session bound to the isolated database."""
    async with session_factory() as session:
        yield session


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


async def _create_user(session: AsyncSession, email: str = "ba@bank.com", **overrides):
    """Insert a loginable account and return the model."""
    fields = {
        "email": email,
        "full_name": "Banking BA",
        "role": "Business Analyst",
        "password_hash": get_password_hash(PASSWORD),
    }
    fields.update(overrides)
    user = await UserRepository.create_user(session=session, **fields)
    await session.commit()
    return user


# =====================================================================
# Repository: the real database read/write path
# =====================================================================
def test_normalize_email_canonicalizes_case_and_whitespace():
    assert normalize_email("  User@Bank.COM ") == "user@bank.com"
    assert normalize_email(None) == ""
    assert normalize_email("") == ""


@pytest.mark.asyncio
async def test_create_user_stores_canonical_lowercase_email(db_session):
    user = await _create_user(db_session, email=" Mixed.Case@Bank.com ")

    assert user.email == "mixed.case@bank.com"
    # The password is never stored in plaintext.
    assert user.password_hash != PASSWORD
    assert password_hash_is_usable(user.password_hash) is True


@pytest.mark.asyncio
async def test_get_by_email_is_case_insensitive(db_session):
    await _create_user(db_session, email="ba@bank.com")

    for candidate in ("ba@bank.com", "BA@BANK.COM", " Ba@Bank.com "):
        assert await UserRepository.get_by_email(candidate, db_session) is not None


@pytest.mark.asyncio
async def test_email_exists_is_case_insensitive(db_session):
    await _create_user(db_session, email="ba@bank.com")

    assert await UserRepository.email_exists("BA@bank.com", db_session) is True
    assert await UserRepository.email_exists("other@bank.com", db_session) is False


@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_non_uuid_without_raising(db_session):
    # A JWT ``sub`` is attacker-influenced: a non-UUID must degrade to a clean
    # "not found" (-> 401) instead of exploding in the GUID bind processor.
    assert await UserRepository.get_by_id("not-a-uuid", db_session) is None


@pytest.mark.asyncio
async def test_get_by_id_and_by_email_agree(db_session):
    created = await _create_user(db_session, email="ba@bank.com")

    by_id = await UserRepository.get_by_id(str(created.id), db_session)
    by_id_uuid = await UserRepository.get_by_id(created.id, db_session)

    assert by_id is not None and by_id.email == "ba@bank.com"
    assert by_id_uuid is not None and by_id_uuid.email == "ba@bank.com"


# =====================================================================
# app.auth: hashing, verification, tokens
# =====================================================================
def test_verify_password_round_trip():
    assert verify_password(PASSWORD, get_password_hash(PASSWORD)) is True
    assert verify_password("wrong-password", get_password_hash(PASSWORD)) is False


def test_verify_password_rejects_empty_and_malformed_hashes():
    # Empty string is the pre-0013 seeded shape; malformed values must not raise.
    assert verify_password(PASSWORD, "") is False
    assert verify_password(PASSWORD, "not-a-bcrypt-hash") is False


def test_password_hash_is_usable_detects_unprovisioned_accounts():
    assert password_hash_is_usable("") is False
    assert password_hash_is_usable("   ") is False
    assert password_hash_is_usable(None) is False
    assert password_hash_is_usable("$2b$12$abcdefghijklmnopqrstuv") is True


def test_access_token_round_trip():
    user_id = str(uuid.uuid4())

    token_data = verify_token(create_access_token(user_id, "ba@bank.com", "Business Analyst"))

    assert token_data.user_id == user_id
    assert token_data.email == "ba@bank.com"
    assert token_data.role == "Business Analyst"


def test_verify_token_rejects_garbage_and_wrongly_signed_tokens():
    from fastapi import HTTPException
    from jose import jwt

    for bad_token in ("not-a-jwt", jwt.encode({"sub": "x"}, "other-secret", algorithm="HS256")):
        with pytest.raises(HTTPException) as exc:
            verify_token(bad_token)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_user_resolves_the_database_row(db_session):
    created = await _create_user(db_session, email="ba@bank.com")

    # Mixed-case login still resolves the same row.
    user = await authenticate_user("BA@Bank.COM", PASSWORD, db_session)

    assert str(user.id) == str(created.id)
    assert user.email == "ba@bank.com"


@pytest.mark.asyncio
async def test_authenticate_user_rejects_unknown_account(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await authenticate_user("nobody@bank.com", PASSWORD, db_session)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid credentials"


@pytest.mark.asyncio
async def test_authenticate_user_rejects_account_without_password_hash(db_session):
    from fastapi import HTTPException

    await _create_user(db_session, email="no-password@bank.com", password_hash="")

    with pytest.raises(HTTPException) as exc:
        await authenticate_user("no-password@bank.com", PASSWORD, db_session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_user_rejects_wrong_password(db_session):
    from fastapi import HTTPException

    await _create_user(db_session, email="ba@bank.com")

    with pytest.raises(HTTPException) as exc:
        await authenticate_user("ba@bank.com", "wrong-password", db_session)

    assert exc.value.status_code == 401


# =====================================================================
# Routes: /api/auth/*
# =====================================================================
@pytest.mark.asyncio
async def test_login_returns_token_for_valid_credentials(client, db_session):
    await _create_user(db_session, email="ba@bank.com")

    resp = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": PASSWORD}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "ba@bank.com"
    assert body["user"]["role"] == "Business Analyst"
    # The issued token is real and resolvable.
    assert verify_token(body["access_token"]).email == "ba@bank.com"


@pytest.mark.asyncio
async def test_login_is_case_insensitive_on_email(client, db_session):
    await _create_user(db_session, email="ba@bank.com")

    resp = await client.post(
        "/api/auth/login", data={"username": "BA@Bank.com", "password": PASSWORD}
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_and_unknown_email(client, db_session):
    await _create_user(db_session, email="ba@bank.com")

    wrong_password = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": "nope"}
    )
    unknown_email = await client.post(
        "/api/auth/login", data={"username": "nobody@bank.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_profile_for_valid_token_and_401_for_stale_token(client, db_session):
    user = await _create_user(db_session, email="ba@bank.com")
    login = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": PASSWORD}
    )
    token = login.json()["access_token"]

    ok = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.json()["id"] == str(user.id)

    # A correctly-signed token for an id that no longer exists must not be
    # trusted on its claims alone — the DB is the source of truth.
    ghost = create_access_token(str(uuid.uuid4()), "ghost@bank.com", "Business Analyst")
    stale = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ghost}"})
    assert stale.status_code == 401

    # Malformed sub (not a UUID) must not 500.
    bad_sub = create_access_token("not-a-uuid", "bad@bank.com", "Business Analyst")
    malformed = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad_sub}"})
    assert malformed.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_a_bearer_token(client):
    resp = await client.get("/api/auth/me")

    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_auth_config_reports_signup_state(client):
    resp = await client.get("/api/auth/config")

    assert resp.status_code == 200
    assert resp.json()["signup_enabled"] is settings.SIGNUP_ENABLED


@pytest.mark.asyncio
async def test_request_lifecycle_uses_safe_correlation_id(client, caplog):
    caplog.set_level("INFO", logger="app.main")

    resp = await client.get(
        "/api/auth/config",
        headers={"X-Request-ID": "browser trace/123"},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "browser_trace_123"
    messages = [record.getMessage() for record in caplog.records]
    assert any("[REQUEST][browser_trace_123][START]" in message for message in messages)
    assert any("[REQUEST][browser_trace_123][DONE]" in message for message in messages)


@pytest.mark.asyncio
async def test_register_creates_a_loginable_row(client, db_session):
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": "New.BA@Bank.com",
            "password": PASSWORD,
            "full_name": "New BA",
            "role": "Business Analyst",
        },
    )

    assert resp.status_code == 201, resp.text
    # Stored canonicalized, so the login below (different case) resolves it.
    assert resp.json()["user"]["email"] == "new.ba@bank.com"
    assert await UserRepository.email_exists("new.ba@bank.com", db_session) is True

    login = await client.post(
        "/api/auth/login", data={"username": "NEW.BA@bank.com", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email_regardless_of_case(client, db_session):
    await _create_user(db_session, email="ba@bank.com")

    resp = await client.post(
        "/api/auth/register",
        json={"email": "BA@BANK.com", "password": PASSWORD, "full_name": "Dup"},
    )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_register_rejects_short_password(client):
    resp = await client.post(
        "/api/auth/register",
        json={"email": "short@bank.com", "password": "short", "full_name": "Short"},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_accepts_every_canonical_role(client, db_session):
    """All seven shipped roles register successfully and are stored verbatim."""
    roles = [
        "Business Analyst",
        "System Analyst",
        "Product Owner",
        "Technical Product Owner",
        "Project Manager",
        "Developer",
        "QA",
    ]
    for i, role in enumerate(roles):
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": f"role{i}@bank.com",
                "password": PASSWORD,
                "full_name": f"Role {role}",
                "role": role,
            },
        )
        assert resp.status_code == 201, (role, resp.text)
        assert resp.json()["user"]["role"] == role


@pytest.mark.asyncio
async def test_register_rejects_removed_and_unknown_roles(client):
    """'Auditor' was retired; anything off-list (or blank) is a 400, never a row."""
    for bad_role in ["Auditor", "auditor", "Admin", "", "   ", "Super Admin"]:
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": f"bad-{abs(hash(bad_role))}@bank.com",
                "password": PASSWORD,
                "full_name": "Bad Role",
                "role": bad_role,
            },
        )
        assert resp.status_code == 400, (bad_role, resp.text)
        assert "Invalid role" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_register_surrounding_whitespace_on_role_is_tolerated(client, db_session):
    """Input '  QA  ' is stored as the exact canonical 'QA'."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": "ws@bank.com",
            "password": PASSWORD,
            "full_name": "Whitespace Role",
            "role": "  QA  ",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["role"] == "QA"


@pytest.mark.asyncio
async def test_register_is_gated_by_signup_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "SIGNUP_ENABLED", False)

    resp = await client.post(
        "/api/auth/register",
        json={"email": "closed@bank.com", "password": PASSWORD, "full_name": "Closed"},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_change_password_persists_the_new_hash(client, db_session):
    await _create_user(db_session, email="ba@bank.com")
    login = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    changed = await client.post(
        "/api/auth/change-password",
        json={"old_password": PASSWORD, "new_password": "Rotated-Pass-2"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text

    # The write reached the database: the old password no longer works and the
    # new one does.
    old_login = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": PASSWORD}
    )
    new_login = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": "Rotated-Pass-2"}
    )
    assert old_login.status_code == 401
    assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_old_password(client, db_session):
    await _create_user(db_session, email="ba@bank.com")
    login = await client.post(
        "/api/auth/login", data={"username": "ba@bank.com", "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post(
        "/api/auth/change-password",
        json={"old_password": "wrong", "new_password": "Rotated-Pass-2"},
        headers=headers,
    )

    assert resp.status_code == 400


# =====================================================================
# Startup seeder: DISABLED — no account may be auto-created
# =====================================================================
# The bootstrap seeder was retired when the app moved to Supabase-only user
# storage: accounts exist exclusively through POST /api/auth/register. These
# tests pin that contract so the auto-seeder cannot silently return.
@pytest.mark.asyncio
async def test_seed_default_user_is_disabled_and_creates_nothing(session_factory):
    await migrations.seed_default_user()  # must be a no-op, not an insert

    async with session_factory() as reader:
        user = await UserRepository.get_by_email(settings.SYSTEM_USER_EMAIL, reader)

    assert user is None, "seed_default_user must not create any account"


# =====================================================================
# Per-user project boundary — nothing before login, own projects after
# =====================================================================
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_project_routes_reject_anonymous_callers(client):
    """Before login the sidebar can show nothing: project reads AND writes
    require a valid Bearer token (401 before any data leaves the DB)."""
    assert (await client.get("/api/projects")).status_code == 401
    assert (await client.post("/api/projects", json={"name": "Ghost"})).status_code == 401
    assert (await client.get("/api/projects/search?q=Ghost")).status_code == 401


@pytest.mark.asyncio
async def test_projects_are_scoped_to_their_owner(client, db_session):
    """Each signed-in user sees exactly their own projects and never another
    user's — the backend rule the sidebar depends on."""
    user_a = await _create_user(db_session, email="a@bank.com")
    user_b = await _create_user(db_session, email="b@bank.com")
    await db_session.commit()
    token_a = create_access_token(user_id=str(user_a.id), email=user_a.email, role=user_a.role)
    token_b = create_access_token(user_id=str(user_b.id), email=user_b.email, role=user_b.role)

    resp_a = await client.post("/api/projects", json={"name": "A Project"}, headers=_bearer(token_a))
    resp_b = await client.post("/api/projects", json={"name": "B Project"}, headers=_bearer(token_b))
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201

    names_a = [p["name"] for p in (await client.get("/api/projects", headers=_bearer(token_a))).json()]
    names_b = [p["name"] for p in (await client.get("/api/projects", headers=_bearer(token_b))).json()]

    assert names_a == ["A Project"]
    assert names_b == ["B Project"]

    # Search is scoped the same way: A cannot surface B's project by name.
    found = await client.get("/api/projects/search?q=B Project", headers=_bearer(token_a))
    assert [p["name"] for p in found.json()] == []


@pytest.mark.asyncio
async def test_other_users_project_mutations_answer_404(client, db_session):
    """Rename/delete/pin/flag/status on someone else's project answer the same
    404 a missing project would — no existence leak, no mutation."""
    owner = await _create_user(db_session, email="owner@bank.com")
    other = await _create_user(db_session, email="other@bank.com")
    await db_session.commit()
    token_owner = create_access_token(user_id=str(owner.id), email=owner.email, role=owner.role)
    token_other = create_access_token(user_id=str(other.id), email=other.email, role=other.role)

    created = await client.post("/api/projects", json={"name": "Owner Only"}, headers=_bearer(token_owner))
    pid = created.json()["id"]

    rename = await client.put(f"/api/projects/{pid}", json={"name": "Hijacked"}, headers=_bearer(token_other))
    pin = await client.put(f"/api/projects/{pid}/pin", json={"is_pinned": True}, headers=_bearer(token_other))
    flag = await client.put(f"/api/projects/{pid}/flag", json={"is_flagged": True}, headers=_bearer(token_other))
    status_change = await client.put(f"/api/projects/{pid}/status", json={"status": "approved"}, headers=_bearer(token_other))
    delete = await client.delete(f"/api/projects/{pid}", headers=_bearer(token_other))

    for resp in (rename, pin, flag, status_change, delete):
        assert resp.status_code == 404, str(resp.request.url)

    # The owner still sees the project untouched under its original name.
    listing = (await client.get("/api/projects", headers=_bearer(token_owner))).json()
    assert [p["name"] for p in listing] == ["Owner Only"]


@pytest.mark.asyncio
async def test_owner_can_manage_their_own_project(client, db_session):
    """The positive path: the legitimate owner's mutations all succeed."""
    owner = await _create_user(db_session, email="solo@bank.com")
    await db_session.commit()
    headers = _bearer(create_access_token(user_id=str(owner.id), email=owner.email, role=owner.role))

    pid = (await client.post("/api/projects", json={"name": "Mine"}, headers=headers)).json()["id"]

    assert (await client.put(f"/api/projects/{pid}", json={"name": "Mine 2"}, headers=headers)).status_code == 200
    assert (await client.put(f"/api/projects/{pid}/pin", json={"is_pinned": True}, headers=headers)).status_code == 200
    assert (await client.put(f"/api/projects/{pid}/status", json={"status": "in_review_hpo"}, headers=headers)).status_code == 200
    assert (await client.delete(f"/api/projects/{pid}", headers=headers)).status_code == 200
    assert (await client.get("/api/projects", headers=headers)).json() == []

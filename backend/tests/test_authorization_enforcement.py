"""Integration tests: authentication/ownership is actually WIRED to the routes.

tests/conftest.py bypasses authentication for every other test module so they
can exercise business logic. This module is EXEMPT from that bypass (it is
listed in REAL_AUTH_MODULES) and asserts the enforcement behavior end-to-end
through the ASGI app:

- 401 for unauthenticated callers on previously-public endpoints,
- 403 for authenticated non-owners (the IDOR hole found in the review),
- 404 for nonexistent projects (never leaked as 403),
- 200 for the legitimate owner.

No test here ever reaches the LLM: workflow hits assert only the auth failure
paths (403/404), which short-circuit before any agent runs.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db as app_get_db
from app.main import app as fastapi_app

PASSWORD = "Correct-Horse-1"
MISSING_PROJECT_ID = "00000000-0000-0000-0000-000000000099"


# =====================================================================
# Fixtures: isolated SQLite database + ASGI client (same pattern as
# tests/test_auth.py, but WITHOUT any auth bypass).
# =====================================================================
@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_authz.db")
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
    """ASGI client wired to the isolated SQLite session; REAL auth deps active."""

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


async def _register_and_login(client: AsyncClient, email: str) -> str:
    """Create a loginable account through the API and return its JWT."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "AuthZ Tester",
            "role": "Business Analyst",
        },
    )
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/auth/login", data={"username": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _create_project(client: AsyncClient, token: str, name: str) -> str:
    resp = await client.post(
        "/api/projects",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_clarification_question(
    db_session: AsyncSession,
    project_id: str,
) -> str:
    """Attach a Requirement → AuditResult → ClarificationQuestion to a project.

    ``POST /api/audit/respond/{question_id}`` receives ONLY a question id, so
    the endpoint has to derive the owning project itself (question → audit
    result → requirement) before it can gate on ownership. This seeds that
    chain and returns the question id under test.

    """
    from app.models import (
        AuditResultModel,
        ClarificationQuestionModel,
        RequirementModel,
    )

    requirement = RequirementModel(
        project_id=uuid.UUID(project_id),
        requirement_code="REQ-001",
        title="AuthZ Requirement",
        status="active",
        version=1,
    )
    db_session.add(requirement)
    await db_session.flush()

    audit_result = AuditResultModel(
        requirement_id=requirement.id,
        audit_version_reviewed=requirement.version,
        is_valid=False,
        passed_checks=[],
        failed_checks=[],
    )
    db_session.add(audit_result)
    await db_session.flush()

    question = ClarificationQuestionModel(
        audit_result_id=audit_result.id,
        checklist_category="Regulatory Compliance",
        question_text="Who is the accountable approver for this change?",
        is_resolved=False,
    )
    db_session.add(question)
    await db_session.flush()
    await db_session.commit()
    return str(question.id)


async def _read_question_state(db_session: AsyncSession, question_id: str):
    """Read a clarification question's persisted state (column-level select).

    Column-level select returns the row straight from the database, so the
    assertions do not depend on the session's identity map.
    """
    from app.models import ClarificationQuestionModel

    row = (
        await db_session.execute(
            select(
                ClarificationQuestionModel.user_answer,
                ClarificationQuestionModel.is_resolved,
            ).where(ClarificationQuestionModel.id == uuid.UUID(question_id))
        )
    ).one()
    return row.user_answer, row.is_resolved


# =====================================================================
# Authentication enforcement (401)
# =====================================================================
@pytest.mark.asyncio
async def test_project_state_requires_authentication(client):
    resp = await client.get(f"/api/project/{MISSING_PROJECT_ID}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_process_requirements_requires_authentication(client):
    resp = await client.post(
        "/api/process-requirements",
        json={"project_id": MISSING_PROJECT_ID, "raw_input": "hi"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_artifact_lock_requires_authentication(client, db_session):
    token = await _register_and_login(client, "locker-authz@bank.com")
    project_id = await _create_project(client, token, "AuthZ Lock Project")

    unauth = await client.post(
        f"/api/project/{project_id}/artifacts/project/{project_id}/lock",
        json={"locked_by": "attacker"},
    )
    assert unauth.status_code == 401

    owner = await client.post(
        f"/api/project/{project_id}/artifacts/project/{project_id}/lock",
        json={"locked_by": "owner"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert owner.status_code == 400, owner.text
    assert "requirement, prd_section" in owner.json()["detail"]


@pytest.mark.asyncio
async def test_document_upload_requires_authentication(client):
    token = await _register_and_login(client, "doc-owner-authz@bank.com")
    project_id = await _create_project(client, token, "AuthZ Docs Project")

    unauth = await client.post(
        f"/api/project/{project_id}/documents/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_traceability_and_events_require_authentication(client):
    token = await _register_and_login(client, "trace-authz@bank.com")
    project_id = await _create_project(client, token, "AuthZ Trace Project")

    assert (await client.get(f"/api/project/{project_id}/traceability")).status_code == 401
    assert (await client.get(f"/api/project/{project_id}/events")).status_code == 401
    assert (await client.get("/api/events")).status_code == 401
    assert (await client.get(f"/api/project/{project_id}/conversations")).status_code == 401


# =====================================================================
# Ownership enforcement (403) + existence semantics (404)
# =====================================================================
@pytest.mark.asyncio
async def test_nonowner_cannot_read_project_state(client, db_session):
    owner_token = await _register_and_login(client, "owner-authz@bank.com")
    attacker_token = await _register_and_login(client, "attacker-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Secret Project")

    owner_view = await client.get(
        f"/api/project/{project_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert owner_view.status_code == 200

    attacker_view = await client.get(
        f"/api/project/{project_id}",
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert attacker_view.status_code == 403


@pytest.mark.asyncio
async def test_missing_project_is_404_not_403(client):
    token = await _register_and_login(client, "owner-404-authz@bank.com")
    resp = await client.get(
        f"/api/project/{MISSING_PROJECT_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_process_requirements_rejects_nonowner_and_missing_project(client):
    owner_token = await _register_and_login(client, "wf-owner-authz@bank.com")
    attacker_token = await _register_and_login(client, "wf-attacker-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Workflow Project")

    attacker_run = await client.post(
        "/api/process-requirements",
        json={"project_id": project_id, "raw_input": "Build a login"},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert attacker_run.status_code == 403

    missing_run = await client.post(
        "/api/process-requirements",
        json={"project_id": MISSING_PROJECT_ID, "raw_input": "Build a login"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert missing_run.status_code == 404


# =====================================================================
# SSE: query-token authentication + ownership
# =====================================================================
@pytest.mark.asyncio
async def test_sse_requires_token_and_ownership(client):
    owner_token = await _register_and_login(client, "sse-owner-authz@bank.com")
    attacker_token = await _register_and_login(client, "sse-attacker-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ SSE Project")

    unauth = await client.get(f"/api/project/{project_id}/sse")
    assert unauth.status_code == 401

    wrong_owner = await client.get(
        f"/api/project/{project_id}/sse", params={"token": attacker_token}
    )
    assert wrong_owner.status_code == 403
    # NOTE: the owner-receives-the-stream case (200 + `connected` handshake
    # frame) is verified in the live smoke test against a real uvicorn server:
    # httpx's ASGI transport buffers a StreamingResponse's body until the app
    # finishes, so an infinite SSE stream cannot be sampled inside a unit test.


# =====================================================================
# IDOR: POST /api/audit/respond/{question_id}
# The path carries only a question id, so ownership cannot be a Depends —
# the handler must resolve the question's OWNING project and gate on it.
# =====================================================================
MISSING_QUESTION_ID = "00000000-0000-0000-0000-000000000088"
ANSWER = "Approved by the Head of Compliance on 2026-01-01."


@pytest.mark.asyncio
async def test_audit_respond_requires_authentication(client):
    resp = await client.post(
        f"/api/audit/respond/{MISSING_QUESTION_ID}",
        json={"answer_text": ANSWER},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_audit_respond_rejects_nonowner_and_leaves_question_untouched(
    client, db_session
):
    owner_token = await _register_and_login(client, "audit-owner-authz@bank.com")
    attacker_token = await _register_and_login(client, "audit-attacker-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Audit Project")
    question_id = await _seed_clarification_question(db_session, project_id)

    attacker = await client.post(
        f"/api/audit/respond/{question_id}",
        json={"answer_text": "Sneaky attacker answer."},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert attacker.status_code == 403, attacker.text

    # The rejected write must not have landed: no answer, still unresolved.
    user_answer, is_resolved = await _read_question_state(db_session, question_id)
    assert user_answer is None
    assert is_resolved is False


@pytest.mark.asyncio
async def test_audit_respond_missing_question_is_404(client):
    token = await _register_and_login(client, "audit-404-authz@bank.com")
    resp = await client.post(
        f"/api/audit/respond/{MISSING_QUESTION_ID}",
        json={"answer_text": ANSWER},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_audit_respond_owner_can_resolve_question(client, db_session):
    owner_token = await _register_and_login(client, "audit-resolve-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Resolve Project")
    question_id = await _seed_clarification_question(db_session, project_id)

    resp = await client.post(
        f"/api/audit/respond/{question_id}",
        json={"answer_text": ANSWER},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["is_resolved"] is True
    assert body["resolved_at"]

    user_answer, is_resolved = await _read_question_state(db_session, question_id)
    assert user_answer == ANSWER
    assert is_resolved is True


@pytest.mark.asyncio
async def test_clarification_questions_are_not_lockable(client, db_session):
    """Clarifications use resolved/status logic and are not lockable artifacts."""
    owner_token = await _register_and_login(client, "audit-locked-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Locked Q Project")
    question_id = await _seed_clarification_question(db_session, project_id)

    resp = await client.post(
        f"/api/audit/respond/{question_id}",
        json={"answer_text": ANSWER},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    user_answer, is_resolved = await _read_question_state(db_session, question_id)
    assert user_answer == ANSWER
    assert is_resolved is True


# =====================================================================
# IDOR (read): /api/intent-detector and /api/requirement-matcher load the
# project's user stories into the LLM context when a project_id is supplied.
# Both must gate on ownership BEFORE loading any tenant data.
# =====================================================================
@pytest.mark.asyncio
async def test_intent_detector_requires_authentication(client):
    resp = await client.post(
        "/api/intent-detector",
        json={"message": "Add SSO to the login flow"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_intent_detector_and_matcher_reject_nonowner(client):
    owner_token = await _register_and_login(client, "intent-owner-authz@bank.com")
    attacker_token = await _register_and_login(client, "intent-attacker-authz@bank.com")
    project_id = await _create_project(client, owner_token, "AuthZ Intent Project")

    intents = await client.post(
        "/api/intent-detector",
        json={"message": "Add SSO to the login flow", "project_id": project_id},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert intents.status_code == 403, intents.text

    matcher = await client.post(
        "/api/requirement-matcher",
        json={"message": "Update the login flow", "project_id": project_id},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert matcher.status_code == 403, matcher.text

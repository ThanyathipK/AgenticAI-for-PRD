"""HTTP-handler boundary tests for persistent generation jobs."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.auth as auth_module
from app.auth import AuthenticatedUser
from app.routes import generation_jobs as routes
from app.schemas import ProcessRequirementsRequest


@pytest.fixture
def current_user():
    return AuthenticatedUser(
        id=str(uuid.uuid4()),
        email="jobs-route@example.com",
        full_name="Job Route User",
        role="Business Analyst",
    )


@pytest.mark.asyncio
async def test_background_agent_route_rejects_non_long_running_agent(monkeypatch, current_user):
    async def allow_access(*_args):
        return None

    monkeypatch.setattr(auth_module, "verify_project_access", allow_access)
    payload = ProcessRequirementsRequest(
        project_id=str(uuid.uuid4()), raw_input="Add transfer", target_agent="gatherer",
    )

    with pytest.raises(HTTPException) as exc:
        await routes.start_agent_job(
            payload, current_user=current_user, db=object(), _rate_limit=None,
        )

    assert exc.value.status_code == 400
    assert "Architect and Auditor" in exc.value.detail


@pytest.mark.asyncio
async def test_background_agent_route_maps_active_job_conflict(monkeypatch, current_user):
    async def allow_access(*_args):
        return None

    async def duplicate(*_args):
        raise ValueError("Project already has an active job.")

    async def active_requirements(*_args):
        return {"requirements": [{"requirement_code": "REQ-001"}]}

    monkeypatch.setattr(auth_module, "verify_project_access", allow_access)
    monkeypatch.setattr(routes, "create_job", duplicate)
    monkeypatch.setattr(
        routes.RequirementStateRepository,
        "get_by_project_id",
        active_requirements,
    )
    payload = ProcessRequirementsRequest(
        project_id=str(uuid.uuid4()), target_agent="architect",
    )

    with pytest.raises(HTTPException) as exc:
        await routes.start_agent_job(
            payload, current_user=current_user, db=object(), _rate_limit=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "Project already has an active job."


@pytest.mark.asyncio
async def test_background_architect_rejects_project_with_only_archived_requirements(
    monkeypatch, current_user
):
    async def allow_access(*_args):
        return None

    async def no_active_requirements(*_args):
        return {"requirements": [], "user_stories": []}

    monkeypatch.setattr(auth_module, "verify_project_access", allow_access)
    monkeypatch.setattr(
        routes.RequirementStateRepository,
        "get_by_project_id",
        no_active_requirements,
    )
    payload = ProcessRequirementsRequest(
        project_id=str(uuid.uuid4()), target_agent="architect",
    )

    with pytest.raises(HTTPException) as exc:
        await routes.start_agent_job(
            payload, current_user=current_user, db=object(), _rate_limit=None,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "There are no requirements to generate."


@pytest.mark.asyncio
async def test_background_architect_allows_empty_board_when_prd_already_exists(
    monkeypatch, current_user
):
    calls = []

    class Session:
        async def commit(self):
            calls.append("commit")

    async def allow_access(*_args):
        return None

    async def existing_prd(*_args):
        return {"requirements": [], "generated_prd": "# Existing PRD"}

    async def create_job(*_args):
        return {"id": "job-empty-prd", "status": "queued"}

    monkeypatch.setattr(auth_module, "verify_project_access", allow_access)
    monkeypatch.setattr(
        routes.RequirementStateRepository,
        "get_by_project_id",
        existing_prd,
    )
    monkeypatch.setattr(routes, "create_job", create_job)
    monkeypatch.setattr(routes, "launch_job", lambda job_id: calls.append(f"launch:{job_id}"))
    payload = ProcessRequirementsRequest(
        project_id=str(uuid.uuid4()), target_agent="architect",
    )

    result = await routes.start_agent_job(
        payload, current_user=current_user, db=Session(), _rate_limit=None,
    )

    assert result["id"] == "job-empty-prd"
    assert calls == ["commit", "launch:job-empty-prd"]


@pytest.mark.asyncio
async def test_background_agent_route_commits_before_launch(monkeypatch, current_user):
    calls = []

    class Session:
        async def commit(self):
            calls.append("commit")

    async def allow_access(*_args):
        return None

    async def create_job(*_args):
        return {"id": "job-123", "status": "queued"}

    def launch(job_id):
        calls.append(f"launch:{job_id}")

    monkeypatch.setattr(auth_module, "verify_project_access", allow_access)
    monkeypatch.setattr(routes, "create_job", create_job)
    monkeypatch.setattr(routes, "launch_job", launch)
    payload = ProcessRequirementsRequest(
        project_id=str(uuid.uuid4()), target_agent="auditor",
    )

    result = await routes.start_agent_job(
        payload, current_user=current_user, db=Session(), _rate_limit=None,
    )

    assert result == {"id": "job-123", "status": "queued"}
    assert calls == ["commit", "launch:job-123"]


@pytest.mark.asyncio
async def test_read_generation_job_returns_404_for_wrong_project(monkeypatch, current_user):
    async def missing(*_args):
        return None

    monkeypatch.setattr(routes, "get_job", missing)

    with pytest.raises(HTTPException) as exc:
        await routes.read_generation_job(
            str(uuid.uuid4()), str(uuid.uuid4()), current_user=current_user, db=object(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_generation_job_reports_terminal_job_without_cancelling(monkeypatch, current_user):
    job = SimpleNamespace(id=uuid.uuid4())

    async def get_job(*_args):
        return job

    async def cancel_job(found, _db):
        assert found is job
        return False

    monkeypatch.setattr(routes, "get_job", get_job)
    monkeypatch.setattr(routes, "cancel_job", cancel_job)
    monkeypatch.setattr(routes, "serialize_job", lambda found: {"id": str(found.id), "status": "completed"})

    result = await routes.stop_generation_job(
        str(uuid.uuid4()), str(job.id), current_user=current_user, db=object(),
    )

    assert result == {"id": str(job.id), "status": "completed", "cancelled": False}

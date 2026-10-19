"""Application-service tests that do not construct a FastAPI client."""

import asyncio

import pytest
from fastapi.responses import JSONResponse

from app.schemas import ProcessRequirementsRequest
from app.services.requirement_processing_service import RequirementProcessingService
from app.workflow_cancellation import workflow_cancellations


class TrackingSession:
    """Small session double that makes rollback behavior observable."""

    def __init__(self):
        self.rollback_calls = 0

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_processing_service_authorizes_before_running_pipeline():
    calls = []
    request = ProcessRequirementsRequest(project_id="project-1", raw_input="Add refunds")
    session = object()
    user = object()

    async def verify(project_id, current_user, current_session):
        calls.append(("verify", project_id, current_user, current_session))

    async def pipeline(current_request, current_session, current_user):
        calls.append(("pipeline", current_request, current_session, current_user))
        return {"status": "ok"}

    service = RequirementProcessingService(pipeline=pipeline, verify_access=verify)
    result = await service.process(request, session, user)

    assert result == {"status": "ok"}
    assert calls[0] == ("verify", "project-1", user, session)
    assert calls[1] == ("pipeline", request, session, user)


@pytest.mark.asyncio
async def test_processing_service_does_not_run_pipeline_when_access_fails():
    request = ProcessRequirementsRequest(project_id="project-1", raw_input="Add refunds")
    pipeline_called = False

    async def verify(*_args):
        raise PermissionError("not owner")

    async def pipeline(*_args):
        nonlocal pipeline_called
        pipeline_called = True

    service = RequirementProcessingService(pipeline=pipeline, verify_access=verify)
    with pytest.raises(PermissionError, match="not owner"):
        await service.process(request, object(), object())

    assert not pipeline_called


@pytest.mark.asyncio
async def test_processing_service_cancellation_rolls_back_and_cleans_registry(monkeypatch):
    monkeypatch.setattr(workflow_cancellations, "_tasks", {})
    request = ProcessRequirementsRequest(project_id="project-1", raw_input="Add refunds")
    session = TrackingSession()

    async def verify(*_args):
        return None

    async def cancelled_pipeline(*_args):
        assert workflow_cancellations.is_running("project-1") is True
        raise asyncio.CancelledError

    service = RequirementProcessingService(
        pipeline=cancelled_pipeline,
        verify_access=verify,
    )
    result = await service.process(request, session, object())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
    assert b'"status":"cancelled"' in result.body
    assert session.rollback_calls == 1
    assert workflow_cancellations.is_running("project-1") is False


@pytest.mark.asyncio
async def test_processing_service_pipeline_error_propagates_and_cleans_registry(monkeypatch):
    monkeypatch.setattr(workflow_cancellations, "_tasks", {})
    request = ProcessRequirementsRequest(project_id="project-1", raw_input="Add refunds")
    session = TrackingSession()

    async def verify(*_args):
        return None

    async def failing_pipeline(*_args):
        assert workflow_cancellations.is_running("project-1") is True
        raise RuntimeError("pipeline failed")

    service = RequirementProcessingService(
        pipeline=failing_pipeline,
        verify_access=verify,
    )
    with pytest.raises(RuntimeError, match="pipeline failed"):
        await service.process(request, session, object())

    assert session.rollback_calls == 0
    assert workflow_cancellations.is_running("project-1") is False


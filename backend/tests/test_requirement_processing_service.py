"""Application-service tests that do not construct a FastAPI client."""

import pytest

from app.schemas import ProcessRequirementsRequest
from app.services.requirement_processing_service import RequirementProcessingService


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


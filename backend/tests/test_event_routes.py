"""Route-level coverage for event history and SSE replay boundaries."""

import json
import uuid

import pytest
from fastapi import HTTPException

from app.auth import AuthenticatedUser
from app.event_manager import EventManager
from app.routes import events as event_routes


@pytest.fixture
def current_user():
    return AuthenticatedUser(
        id=str(uuid.uuid4()),
        email="events@example.com",
        full_name="Event Reader",
        role="Business Analyst",
    )


@pytest.mark.asyncio
async def test_action_event_route_rejects_unknown_action(current_user):
    with pytest.raises(HTTPException) as exc:
        await event_routes.get_events_by_action(
            "overwrite", current_user=current_user, session=object(),
        )

    assert exc.value.status_code == 400
    assert "Invalid action" in exc.value.detail


@pytest.mark.asyncio
async def test_action_event_route_normalizes_action_and_scopes_user(monkeypatch, current_user):
    captured = {}

    async def get_by_action(action, session, **kwargs):
        captured.update(action=action, session=session, **kwargs)
        return [{"id": "event-1"}]

    monkeypatch.setattr(event_routes.ArtifactEventLogRepository, "get_by_action", get_by_action)
    session = object()
    result = await event_routes.get_events_by_action(
        "update", limit=7, offset=3, current_user=current_user, session=session,
    )

    assert result == {
        "action": "UPDATE",
        "events": [{"id": "event-1"}],
        "total": 1,
        "limit": 7,
        "offset": 3,
    }
    assert captured == {
        "action": "UPDATE",
        "session": session,
        "limit": 7,
        "offset": 3,
        "user_id": current_user.id,
    }


@pytest.mark.asyncio
async def test_project_event_route_validates_id_and_action(current_user):
    with pytest.raises(HTTPException) as bad_id:
        await event_routes.get_project_events(
            "not-a-uuid", current_user=current_user, session=object(),
        )
    assert bad_id.value.status_code == 400

    with pytest.raises(HTTPException) as bad_action:
        await event_routes.get_project_events(
            str(uuid.uuid4()), action="overwrite",
            current_user=current_user, session=object(),
        )
    assert bad_action.value.status_code == 400


@pytest.mark.asyncio
async def test_project_event_route_forwards_normalized_filters(monkeypatch, current_user):
    captured = {}

    async def get_by_project(project_id, session, **kwargs):
        captured.update(project_id=project_id, session=session, **kwargs)
        return []

    monkeypatch.setattr(event_routes.ArtifactEventLogRepository, "get_by_project", get_by_project)
    project_id = str(uuid.uuid4())
    session = object()
    result = await event_routes.get_project_events(
        project_id,
        artifact_type="requirement",
        action="create",
        limit=12,
        offset=4,
        current_user=current_user,
        session=session,
    )

    assert result["action"] == "CREATE"
    assert result["total"] == 0
    assert captured == {
        "project_id": project_id,
        "session": session,
        "artifact_type": "requirement",
        "action": "CREATE",
        "limit": 12,
        "offset": 4,
    }


@pytest.mark.asyncio
async def test_sse_route_replays_after_last_event_and_unsubscribes(monkeypatch, current_user):
    manager = EventManager(history_size=10)
    project_id = str(uuid.uuid4())
    await manager.publish(project_id, "old", {"step": 1})
    await manager.publish(project_id, "missed", {"step": 2})
    monkeypatch.setattr(event_routes, "event_manager", manager)

    response = await event_routes.stream_project_events(
        project_id, last_event_id="1", current_user=current_user,
    )
    iterator = response.body_iterator
    connected = await anext(iterator)
    replay = await anext(iterator)

    assert json.loads(connected.removeprefix("data: "))["event"] == "connected"
    assert replay.startswith("id: 2\n")
    assert '"event": "missed"' in replay

    await iterator.aclose()
    assert project_id not in manager._subscribers


@pytest.mark.asyncio
async def test_sse_route_rejects_invalid_project_id(current_user):
    with pytest.raises(HTTPException) as exc:
        await event_routes.stream_project_events(
            "bad-project", last_event_id=None, current_user=current_user,
        )

    assert exc.value.status_code == 400

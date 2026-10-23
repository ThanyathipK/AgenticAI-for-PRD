"""Durable, idempotent, chronologically stable conversation timeline tests."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import ConversationMessageModel

pytest_plugins = ("tests.test_documents",)


@pytest.mark.asyncio
async def test_append_message_is_idempotent_and_keeps_rapid_order(
    client, db_session, seeded_project,
):
    first_id = str(uuid4())
    second_id = str(uuid4())
    first = {
        "id": first_id,
        "role": "user",
        "message": "Explain the outage",
        "workflow_state": "gatherer",
        "intent": "PENDING_DETECTION",
    }
    response = await client.post(
        f"/api/project/{seeded_project}/conversations", json=first,
    )
    assert response.status_code == 201, response.text
    duplicate = await client.post(
        f"/api/project/{seeded_project}/conversations", json=first,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == first_id

    response = await client.post(
        f"/api/project/{seeded_project}/conversations",
        json={
            "id": second_id,
            "role": "assistant",
            "message": "LM Studio is unavailable.",
            "workflow_state": "gatherer",
            "intent": "TIMELINE_EVENT",
        },
    )
    assert response.status_code == 201, response.text

    history = (await client.get(
        f"/api/project/{seeded_project}/conversations"
    )).json()
    assert [item["id"] for item in history] == [first_id, second_id]
    assert await db_session.scalar(
        select(func.count()).select_from(ConversationMessageModel)
    ) == 2


@pytest.mark.asyncio
async def test_project_state_returns_latest_five_and_endpoint_supports_cursor_pages(
    client, seeded_project,
):
    for index in range(105):
        response = await client.post(
            f"/api/project/{seeded_project}/conversations",
            json={"role": "system", "message": f"timeline-{index:03d}"},
        )
        assert response.status_code == 201

    state = (await client.get(f"/api/project/{seeded_project}")).json()
    assert len(state["conversation_history"]) == 5
    assert state["conversation_history"][0]["message"] == "timeline-100"
    assert state["conversation_history"][-1]["message"] == "timeline-104"

    complete = (await client.get(
        f"/api/project/{seeded_project}/conversations"
    )).json()
    assert len(complete) == 105
    assert complete[0]["message"] == "timeline-000"
    assert complete[-1]["message"] == "timeline-104"

    previous = (await client.get(
        f"/api/project/{seeded_project}/conversations",
        params={"limit": 5, "before": state["conversation_history"][0]["id"]},
    )).json()
    assert [message["message"] for message in previous] == [
        "timeline-095", "timeline-096", "timeline-097", "timeline-098", "timeline-099",
    ]

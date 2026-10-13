"""
Regression tests for :mod:`app.repositories.requirement_state`.

These tests guard the N+1 query that previously lived in
``RequirementStateRepository._get_by_project_id_impl``: for *every* active
requirement the old code issued a dedicated User Story query plus a dedicated
Acceptance Criteria query (O(requirements) round-trips).

A fake async session records each executed statement and replays canned model
instances keyed by SQLAlchemy entity. The assertions below verify that the
whole project loads user stories with **one** query and acceptance criteria with
**one** query regardless of how many requirements exist.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_requirement_state_repository.py -v
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models import (
    AcceptanceCriteriaModel,
    ClarificationQuestionModel,
    EpicModel,
    PRDDocumentModel,
    RequirementModel,
    RequirementStateModel,
    UserStoryModel,
)
from app.repositories.requirement_state import RequirementStateRepository


# ======================================================================
# Fake AsyncSession plumbing
# ======================================================================
class _Scalars:
    """Mimics the scalar access helpers used across these repositories."""

    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    """Mimics the small slice of sqlalchemy.Result used in the repo layer."""

    def __init__(self, scalar_items=None, tuple_rows=None):
        self._scalar_items = list(scalar_items or [])
        self._tuple_rows = list(tuple_rows or [])

    def scalars(self):
        return _Scalars(self._scalar_items)

    def all(self):
        return list(self._tuple_rows)

    def first(self):
        return self._scalar_items[0] if self._scalar_items else None

    def scalar_one_or_none(self):
        return self._scalar_items[0] if self._scalar_items else None

    def one_or_none(self):
        return self._tuple_rows[0] if self._tuple_rows else None


class FakeAsyncSession:
    """Pre-seeds model instances and counts how often each entity is queried."""

    def __init__(self, *, state, reqs, stories, ac_rows=None, epic=None, cqs=None, prds=None):
        self._state = state
        self._reqs = reqs
        self._stories = stories
        self._ac_rows = ac_rows or []
        self._active_epic = epic
        self._cqs = cqs or []
        self._prds = prds or []

        # entity-group name -> number of statements that resolved to it
        self.dispatch_counts = {}

    async def execute(self, stmt):
        entities = [c["entity"] for c in stmt.column_descriptions]

        # The acceptance-criteria load is a join over UserStory, so it must be
        # matched before the plain UserStory branch below.
        if AcceptanceCriteriaModel in entities:
            return self._dispatch("acceptance_criteria", _Result(tuple_rows=self._ac_rows))

        if UserStoryModel in entities:
            return self._dispatch("user_story", _Result(scalar_items=self._stories))

        if RequirementStateModel in entities:
            return self._dispatch(
                "requirement_state",
                _Result(tuple_rows=[(self._state, "Acme", "draft")]),
            )

        if EpicModel in entities:
            items = [self._active_epic] if self._active_epic else []
            return self._dispatch("epic", _Result(scalar_items=items))

        if RequirementModel in entities:
            return self._dispatch("requirement", _Result(scalar_items=self._reqs))

        if ClarificationQuestionModel in entities:
            return self._dispatch("clarification", _Result(scalar_items=self._cqs))

        if PRDDocumentModel in entities:
            return self._dispatch("prd", _Result(scalar_items=self._prds))

        return _Result(scalar_items=[])

    def _dispatch(self, name, result):
        self.dispatch_counts[name] = self.dispatch_counts.get(name, 0) + 1
        return result
# ======================================================================
# Model builders + shared project fixture
# ======================================================================
def _user(project_id, seq):
    return RequirementModel(
        id=uuid.UUID(f"00000000-0000-0000-0000-{seq:012d}"),
        project_id=project_id,
        epic_id=None,
        requirement_code=f"REQ-{seq:03d}",
        title=f"Requirement {seq}",
        description="desc",
        is_locked=False,
        locked_by=None,
        locked_at=None,
    )


def _story(project_id, requirement_id, stub, *, status, ticket_code):
    return UserStoryModel(
        id=uuid.UUID(stub),
        project_id=project_id,
        requirement_id=requirement_id,
        ticket_code=ticket_code,
        story_title=f"Story {ticket_code}",
        as_a="user",
        i_want_to="do a thing",
        so_that="value is delivered",
        status=status,
        version=1,
        last_modified_by="automated_agent",
        change_type="created",
        is_locked=False,
        locked_by=None,
        locked_at=None,
    )


def _ac(story_id, stub, *, criteria_text):
    return AcceptanceCriteriaModel(
        id=uuid.UUID(stub),
        user_story_id=story_id,
        criteria_text=criteria_text,
        status="active",
        version=1,
        last_modified_by="automated_agent",
        change_type="created",
    )


def _make_state(project_id):
    return RequirementStateModel(
        project_id=project_id,
        requirements={},
        business_goals=[],
        actors=[],
        user_stories=[],
        acceptance_criteria=[],
        clarification_questions=[],
        validation_status="pending",
        generated_prd="",
        generated_diagrams="",
        current_workflow_state="gatherer_node",
        version_number=1,
        updated_at=datetime.now(timezone.utc),
    )


PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def build_project_session():
    """Three active requirements, active + archived stories, and their criteria."""
    project_uuid = uuid.UUID(PROJECT_ID)
    r1, r2, r3 = _user(project_uuid, 1), _user(project_uuid, 2), _user(project_uuid, 3)

    s1 = _story(project_uuid, r1.id, "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1",
                status="active", ticket_code="US-001")
    s2 = _story(project_uuid, r1.id, "a2a2a2a2-a2a2-a2a2-a2a2-a2a2a2a2a2a2",
                status="archived", ticket_code="US-002")
    s3 = _story(project_uuid, r2.id, "a3a3a3a3-a3a3-a3a3-a3a3-a3a3a3a3a3a3",
                status="active", ticket_code="US-003")

    state = _make_state(project_uuid)
    active_epic = EpicModel(id=uuid.UUID("e0000000-0000-0000-0000-00000000000e"),
                            epic_name="E-commerce Platform")

    # (ac_model, ticket_code_of_its_story, requirement_id)
    ac_rows = [
        # Both criteria attach to the ACTIVE story s1 on r1.
        (_ac(s1.id, "bbbbbbbb-1111-2222-3333-000000000001", criteria_text="AC-Login"), "US-001", r1.id),
        (_ac(s1.id, "bbbbbbbb-1111-2222-3333-000000000002", criteria_text="AC-Logout"), "US-001", r1.id),
        # AC attached to the ARCHIVED story s2 -> must be excluded.
        (_ac(s2.id, "bbbbbbbb-1111-2222-3333-000000000003", criteria_text="AC-Archived"), "US-002", r1.id),
        # AC attached to the ACTIVE story s3 on r2.
        (_ac(s3.id, "bbbbbbbb-1111-2222-3333-000000000004", criteria_text="AC-Transfer"), "US-003", r2.id),
    ]

    return FakeAsyncSession(
        state=state,
        reqs=[r1, r2, r3],
        stories=[s1, s2, s3],
        ac_rows=ac_rows,
        epic=active_epic,
    )
# ======================================================================
# Tests
# ======================================================================
class TestGetByProjectIdBatches:
    @pytest.mark.asyncio
    async def test_user_stories_and_acceptance_criteria_use_one_query_each(self):
        session = build_project_session()

        result = await RequirementStateRepository.get_by_project_id(PROJECT_ID, session)

        # The whole point of the fix: with 3 requirements, the old code issued
        # 3 story queries and 3 acceptance-criteria queries. Now it is 1 each.
        assert session.dispatch_counts.get("user_story", 0) == 1, (
            "expected a single batched user-story load, got "
            f"{session.dispatch_counts.get('user_story', 0)}"
        )
        assert session.dispatch_counts.get("acceptance_criteria", 0) == 1, (
            "expected a single batched acceptance-criteria load, got "
            f"{session.dispatch_counts.get('acceptance_criteria', 0)}"
        )

        assert result is not None
        assert result["project_status"] == "draft"
        assert len(result["requirements"]) == 3

    @pytest.mark.asyncio
    async def test_requirements_are_still_aggregated_correctly(self):
        session = build_project_session()
        result = await RequirementStateRepository.get_by_project_id(PROJECT_ID, session)

        first_req = result["requirements"][0]
        # Only the ACTIVE story survives; the AC attached to the archived story
        # is filtered out alongside it.
        assert [s["ticket_code"] for s in first_req["user_stories"]] == ["US-001"]
        active_story = first_req["user_stories"][0]
        assert sorted(active_story["acceptance_criteria"]) == ["AC-Login", "AC-Logout"]

        requirements_with_stories = [r for r in result["requirements"] if r["user_stories"]]
        assert len(requirements_with_stories) == 2  # r3 has no stories

        # Flat rollups match the original behavior: every ACTIVE-status AC is
        # rolled up (AC-Archived is active itself, only its *story* is archived
        # so it never appears on any requirement's story mapping).
        assert [s["ticket_code"] for s in result["user_stories"]] == ["US-001", "US-003"]
        assert sorted(result["acceptance_criteria"]) == [
            "AC-Archived",
            "AC-Login",
            "AC-Logout",
            "AC-Transfer",
        ]

    @pytest.mark.asyncio
    async def test_no_active_requirements_returns_empty_requirements(self):
        # Same shape as build_project_session() but with zero requirements.
        project_uuid = uuid.UUID(PROJECT_ID)
        session = FakeAsyncSession(
            state=_make_state(project_uuid),
            reqs=[],
            stories=[],
            ac_rows=[],
            epic=EpicModel(id=uuid.uuid4(), epic_name="E-commerce Platform"),
        )
        result = await RequirementStateRepository.get_by_project_id(PROJECT_ID, session)

        assert result is not None
        assert result["requirements"] == []
        # Batch helpers short-circuit on an empty ID set -> no story/AC query.
        assert session.dispatch_counts.get("user_story", 0) == 0
        assert session.dispatch_counts.get("acceptance_criteria", 0) == 0
        return result

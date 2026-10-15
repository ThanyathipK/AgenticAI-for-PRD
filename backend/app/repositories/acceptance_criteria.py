"""
Acceptance Criteria repository operations.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.lock_service import LockService
from app.models import AcceptanceCriteriaModel, RequirementModel, UserStoryModel
from app.repositories.base import as_uuid, serialize_acceptance_criteria

logger = logging.getLogger(__name__)


class AcceptanceCriteriaRepository:
    """Handles Agile Acceptance Criteria of the project."""

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)
        story_id = data.get("user_story_id")
        if story_id and isinstance(story_id, str):
            story_id = uuid.UUID(story_id)
        # LOCK ENFORCEMENT: Cannot add acceptance criteria to a locked user story
        if story_id:
            stmt_story = select(UserStoryModel).where(UserStoryModel.id == story_id)
            res_story = await session.execute(stmt_story)
            story = res_story.scalar_one_or_none()
            if story:
                LockService.raise_if_locked_model(
                    "user_story",
                    story,
                    message=f"User Story {story.ticket_code} is locked by {story.locked_by or 'unknown'}. Unlock it before adding acceptance criteria."
                )
        ac = AcceptanceCriteriaModel(
            user_story_id=story_id,
            criteria_text=data.get("criteria_text", ""),
            status=data.get("status", "active"),
            version=data.get("version", 1),
            last_modified_by=data.get("last_modified_by", "automated_agent"),
            change_type=data.get("change_type", "created")
        )
        session.add(ac)
        await session.flush()
        await session.refresh(ac)
        return serialize_acceptance_criteria(ac, pid, data.get("ticket_code"))

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        acid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AcceptanceCriteriaModel, UserStoryModel.ticket_code).join(
            UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
        ).join(
            RequirementModel, UserStoryModel.requirement_id == RequirementModel.id
        ).where(AcceptanceCriteriaModel.id == acid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        record = result.first()
        if record:
            ac, ticket_code = record
            return serialize_acceptance_criteria(ac, pid, ticket_code)
        return None

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        if requirement_id is None:
            stmt_req = select(RequirementModel).where(RequirementModel.project_id == pid).order_by(RequirementModel.version.desc())
            res_req = await session.execute(stmt_req)
            req = res_req.scalars().first()
            if req:
                requirement_id = req.id

        if requirement_id:
            stmt = select(AcceptanceCriteriaModel, UserStoryModel.ticket_code).join(
                UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
            ).where(UserStoryModel.requirement_id == requirement_id)
        else:
            stmt = select(AcceptanceCriteriaModel, UserStoryModel.ticket_code).join(
                UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
            ).join(
                RequirementModel, UserStoryModel.requirement_id == RequirementModel.id
            ).where(RequirementModel.project_id == pid)

        result = await session.execute(stmt)
        records = result.all()
        return [
            serialize_acceptance_criteria(ac, pid, ticket_code)
            for ac, ticket_code in records
        ]

    @staticmethod
    async def get_by_requirement_ids(
        requirement_ids: List[uuid.UUID],
        project_id: str,
        session: AsyncSession,
    ) -> Dict[uuid.UUID, List[Dict[str, Any]]]:
        """
        Batch-load acceptance criteria (with their user-story ticket codes) for
        many requirements in a single query.

        Returns criteria grouped by ``requirement_id``. This avoids the
        per-requirement ``get_by_project(session, requirement_id=...)`` calls
        that caused an N+1 round-trip cascade when building a RequirementState.
        """
        grouped: Dict[uuid.UUID, List[Dict[str, Any]]] = {}
        pid = as_uuid(project_id)
        if not requirement_ids:
            return grouped

        stmt = select(
            AcceptanceCriteriaModel,
            UserStoryModel.ticket_code,
            UserStoryModel.requirement_id,
        ).join(
            UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
        ).where(UserStoryModel.requirement_id.in_(requirement_ids))

        result = await session.execute(stmt)
        for ac, ticket_code, requirement_id in result.all():
            grouped.setdefault(requirement_id, []).append(
                serialize_acceptance_criteria(ac, pid, ticket_code)
            )
        return grouped

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        acid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AcceptanceCriteriaModel).join(
            UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
        ).join(
            RequirementModel, UserStoryModel.requirement_id == RequirementModel.id
        ).where(AcceptanceCriteriaModel.id == acid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        ac = result.scalar_one_or_none()
        if ac:
            # LOCK ENFORCEMENT: Cannot update a locked acceptance criteria
            LockService.raise_if_locked_model(
                "acceptance_criteria",
                ac,
                message=f"Acceptance Criteria is locked by {ac.locked_by or 'unknown'}. Unlock it before modifying."
            )
            criteria_changed = "criteria_text" in updates and ac.criteria_text != updates["criteria_text"]
            if "criteria_text" in updates:
                ac.criteria_text = updates["criteria_text"]
            if "user_story_id" in updates:
                story_id = updates["user_story_id"]
                ac.user_story_id = uuid.UUID(story_id) if story_id else None
            if "status" in updates:
                ac.status = updates["status"]
            if "version" in updates:
                ac.version = updates["version"]
            if "last_modified_by" in updates:
                ac.last_modified_by = updates["last_modified_by"]
            if "change_type" in updates:
                ac.change_type = updates["change_type"]
            elif criteria_changed:
                # Editing the criterion is the act that resolves a dependency
                # review raised by a parent-story change.
                ac.change_type = "updated"
            await session.flush()
            await session.refresh(ac)

            stmt_code = select(UserStoryModel.ticket_code).where(UserStoryModel.id == ac.user_story_id)
            res_code = await session.execute(stmt_code)
            ticket_code = res_code.scalar_one_or_none()

            return serialize_acceptance_criteria(ac, pid, ticket_code)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        acid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AcceptanceCriteriaModel).join(
            UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id
        ).join(
            RequirementModel, UserStoryModel.requirement_id == RequirementModel.id
        ).where(AcceptanceCriteriaModel.id == acid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        ac = result.scalar_one_or_none()
        if ac:
            # LOCK ENFORCEMENT: Cannot delete a locked acceptance criteria
            LockService.raise_if_locked_model(
                "acceptance_criteria",
                ac,
                message=f"Acceptance Criteria is locked by {ac.locked_by or 'unknown'}. Unlock it before deleting."
            )
            await session.delete(ac)
            await session.flush()
            return True
        return False

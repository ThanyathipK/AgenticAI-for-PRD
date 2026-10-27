"""
User Story repository operations.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AcceptanceCriteriaModel, RequirementModel, UserStoryModel
from app.repositories.base import as_uuid, serialize_user_story
from app.requirement_codes import UNASSIGNED_REQUIREMENT_CODE

logger = logging.getLogger(__name__)


class UserStoryRepository:
    """Handles Agile User Stories of the project."""

    @staticmethod
    async def _mark_criteria_for_review(story_id: uuid.UUID, session: AsyncSession) -> None:
        """Invalidate unchanged child criteria after parent behaviour changes."""
        result = await session.execute(
            select(AcceptanceCriteriaModel).where(
                AcceptanceCriteriaModel.user_story_id == story_id,
                AcceptanceCriteriaModel.status == "active",
            )
        )
        for criterion in result.scalars().all():
            if criterion.change_type != "review_required":
                criterion.version += 1
            criterion.change_type = "review_required"
            criterion.last_modified_by = "dependency_propagation"

    @staticmethod
    async def generate_unique_ticket_code(project_id: uuid.UUID, session: AsyncSession) -> str:
        stmt = select(UserStoryModel.ticket_code).where(UserStoryModel.project_id == project_id)
        res = await session.execute(stmt)
        codes = res.scalars().all()

        max_num = 0
        for code in codes:
            if code and code.startswith("US-"):
                try:
                    num = int(code.split("-")[1])
                    if num > max_num:
                        max_num = num
                except (ValueError, IndexError):
                    pass

        new_num = max_num + 1
        return f"US-{new_num:03d}"

    @staticmethod
    async def save_or_update(project_id: str, data: Dict[str, Any], session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
        pid = as_uuid(project_id)

        if requirement_id is None:
            # 1. Resolve requirement_id for this project
            stmt = select(RequirementModel).where(RequirementModel.project_id == pid).order_by(RequirementModel.version.desc())
            res = await session.execute(stmt)
            req = res.scalars().first()
            if not req:
                req = RequirementModel(
                    project_id=pid,
                    requirement_code=UNASSIGNED_REQUIREMENT_CODE,
                    title="Untitled Requirement",
                    status="active",
                    is_locked=False,
                    locked_by=None,
                    locked_at=None
                )
                session.add(req)
                await session.flush()
            requirement_id = req.id

        ticket_code = data.get("ticket_code")
        story_title = data.get("story_title", "Untitled Story")

        # Check if story already exists by (requirement_id and ticket_code) OR (requirement_id and story_title)
        story = None
        if ticket_code and ticket_code != "US-000":
            stmt = select(UserStoryModel).where(
                UserStoryModel.requirement_id == requirement_id,
                UserStoryModel.ticket_code == ticket_code
            )
            res = await session.execute(stmt)
            story = res.scalars().first()

        if not story:
            stmt = select(UserStoryModel).where(
                UserStoryModel.requirement_id == requirement_id,
                UserStoryModel.story_title == story_title
            )
            res = await session.execute(stmt)
            story = res.scalars().first()

        # If no ticket_code is provided or it is a placeholder/invalid, generate a unique one!
        if not ticket_code or ticket_code == "US-000":
            if story and story.ticket_code and story.ticket_code != "US-000":
                ticket_code = story.ticket_code
            else:
                ticket_code = await UserStoryRepository.generate_unique_ticket_code(pid, session)

        if story:
            content_changed = any([
                story.ticket_code != ticket_code,
                story.story_title != story_title,
                "as_a" in data and story.as_a != data.get("as_a"),
                "i_want_to" in data and story.i_want_to != data.get("i_want_to"),
                "so_that" in data and story.so_that != data.get("so_that"),
            ])
            # Update existing story
            story.ticket_code = ticket_code
            story.story_title = story_title
            story.as_a = data.get("as_a", story.as_a)
            story.i_want_to = data.get("i_want_to", story.i_want_to)
            story.so_that = data.get("so_that", story.so_that)
            story.project_id = pid
            story.status = data.get("status", story.status)
            story.version = data.get("version", story.version)
            story.last_modified_by = data.get("last_modified_by", story.last_modified_by)
            story.change_type = data.get("change_type", story.change_type)
            if content_changed:
                await UserStoryRepository._mark_criteria_for_review(story.id, session)
            await session.flush()
        else:
            # Create a brand new story
            story = UserStoryModel(
                project_id=pid,
                requirement_id=requirement_id,
                ticket_code=ticket_code,
                story_title=story_title,
                as_a=data.get("as_a", ""),
                i_want_to=data.get("i_want_to", ""),
                so_that=data.get("so_that", ""),
                status=data.get("status", "active"),
                version=data.get("version", 1),
                last_modified_by=data.get("last_modified_by", "automated_agent"),
                change_type=data.get("change_type", "created")
            )
            session.add(story)
            await session.flush()

        await session.refresh(story)
        return serialize_user_story(story, pid)

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
        return await UserStoryRepository.save_or_update(project_id, data, session, requirement_id=requirement_id)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        sid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(UserStoryModel).join(RequirementModel).where(
            UserStoryModel.id == sid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if story:
            return serialize_user_story(story, pid)
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
            stmt = select(UserStoryModel).where(UserStoryModel.requirement_id == requirement_id)
        else:
            stmt = select(UserStoryModel).join(RequirementModel).where(RequirementModel.project_id == pid)

        result = await session.execute(stmt)
        stories = result.scalars().all()
        return [serialize_user_story(s, pid) for s in stories]

    @staticmethod
    async def get_by_requirement_ids(
        requirement_ids: List[uuid.UUID],
        session: AsyncSession,
    ) -> Dict[uuid.UUID, List[Dict[str, Any]]]:
        """
        Batch-load user stories for many requirements in a single query.

        Returns stories grouped by ``requirement_id``. This replaces per-requirement
        ``get_by_project(session, requirement_id=...)`` calls that caused an N+1
        round-trip cascade when assembling a project's full RequirementState.
        """
        grouped: Dict[uuid.UUID, List[Dict[str, Any]]] = {}
        if not requirement_ids:
            return grouped

        stmt = select(UserStoryModel).where(UserStoryModel.requirement_id.in_(requirement_ids))
        result = await session.execute(stmt)
        for story in result.scalars().all():
            grouped.setdefault(story.requirement_id, []).append(
                serialize_user_story(story, story.project_id)
            )
        return grouped

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        sid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(UserStoryModel).join(RequirementModel).where(
            UserStoryModel.id == sid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if story:
            content_changed = any(
                field in updates and getattr(story, field) != updates[field]
                for field in ("ticket_code", "story_title", "as_a", "i_want_to", "so_that")
            )
            if "ticket_code" in updates:
                story.ticket_code = updates["ticket_code"]
            if "story_title" in updates:
                story.story_title = updates["story_title"]
            if "as_a" in updates:
                story.as_a = updates["as_a"]
            if "i_want_to" in updates:
                story.i_want_to = updates["i_want_to"]
            if "so_that" in updates:
                story.so_that = updates["so_that"]
            if "status" in updates:
                story.status = updates["status"]
            if "version" in updates:
                story.version = updates["version"]
            if "last_modified_by" in updates:
                story.last_modified_by = updates["last_modified_by"]
            if "change_type" in updates:
                story.change_type = updates["change_type"]
            if content_changed:
                await UserStoryRepository._mark_criteria_for_review(story.id, session)
            await session.flush()
            await session.refresh(story)
            return serialize_user_story(story, pid)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        sid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(UserStoryModel).join(RequirementModel).where(
            UserStoryModel.id == sid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if story:
            await session.delete(story)
            await session.flush()
            return True
        return False

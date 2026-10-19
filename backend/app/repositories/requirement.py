"""
Requirement repository operations.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from app.models import (
    AcceptanceCriteriaModel,
    RequirementModel,
    UserStoryModel,
)
from app.repositories.base import as_uuid, serialize_requirement
from app.repositories.event_log import ArtifactEventLogRepository
from app.requirement_codes import UNASSIGNED_REQUIREMENT_CODE

logger = logging.getLogger(__name__)


class RequirementRepository:
    """Handles atomic requirements of the project."""

    @staticmethod
    def _serialize(req: RequirementModel) -> Dict[str, Any]:
        """Serialize a RequirementModel to a dict including lock fields."""
        return serialize_requirement(req)

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)
        req = RequirementModel(
            project_id=pid,
            epic_id=uuid.UUID(data["epic_id"]) if data.get("epic_id") else None,
            requirement_code=data.get("requirement_code", UNASSIGNED_REQUIREMENT_CODE),
            title=data.get("title", "Untitled Requirement"),
            functional_requirement=data.get("functional_requirement"),
            description=data.get("description"),
            status=data.get("status", "active"),
            is_locked=False,
            locked_by=None,
            locked_at=None
        )
        session.add(req)
        await session.flush()
        await session.refresh(req)
        return RequirementRepository._serialize(req)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        rid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.id == rid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        req = result.scalar_one_or_none()
        if req:
            return RequirementRepository._serialize(req)
        return None

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        """Load every requirement of a project with its ACTIVE user stories nested.

        Deliberately applies NO status filter: the Requirements tab's traceability
        matrix lists archived requirements as well, and the lock list must mirror
        it so a Product Owner can see (and lock) every requirement record that
        exists. Nesting the active stories lets the UI show a per-requirement
        story count without a second round-trip.
        """
        # Imported lazily: user_story.py imports this module at import time, so a
        # module-level import here would close a circular dependency.
        from app.repositories.user_story import UserStoryRepository

        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        reqs = result.scalars().all()

        stories_by_req = await UserStoryRepository.get_by_requirement_ids(
            [r.id for r in reqs], session
        )

        serialized: List[Dict[str, Any]] = []
        for req in reqs:
            data = RequirementRepository._serialize(req)
            data["user_stories"] = [
                story
                for story in stories_by_req.get(req.id, [])
                if (story.get("status") or "active") == "active"
            ]
            serialized.append(data)
        return serialized

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        rid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.id == rid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        req = result.scalar_one_or_none()
        if req:
            # LOCK ENFORCEMENT: Cannot update a locked requirement
            if req.is_locked:
                raise PermissionError(
                    f"Requirement {req.requirement_code} is locked by {req.locked_by or 'unknown'} "
                    f"at {req.locked_at.isoformat() if req.locked_at else 'unknown time'}. "
                    f"Unlock it before modifying."
                )
            if "title" in updates:
                req.title = updates["title"]
            if "functional_requirement" in updates:
                req.functional_requirement = updates["functional_requirement"]
            if "description" in updates:
                req.description = updates["description"]
            if "status" in updates:
                req.status = updates["status"]
            await session.flush()
            await session.refresh(req)
            return RequirementRepository._serialize(req)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        rid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.id == rid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        req = result.scalar_one_or_none()
        if req:
            # LOCK ENFORCEMENT: Cannot delete a locked requirement
            if req.is_locked:
                raise PermissionError(
                    f"Requirement {req.requirement_code} is locked by {req.locked_by or 'unknown'} "
                    f"at {req.locked_at.isoformat() if req.locked_at else 'unknown time'}. "
                    f"Unlock it before deleting."
                )
            req.status = "deleted"
            # Archive User Stories and Acceptance Criteria related to that Requirement
            stmt_stories = select(UserStoryModel).where(UserStoryModel.requirement_id == rid)
            res_stories = await session.execute(stmt_stories)
            stories = res_stories.scalars().all()
            for story in stories:
                story.status = "archived"
                story.change_type = "archived"
                story.version = story.version + 1

                stmt_ac = select(AcceptanceCriteriaModel).where(AcceptanceCriteriaModel.user_story_id == story.id)
                res_ac = await session.execute(stmt_ac)
                ac_list = res_ac.scalars().all()
                for ac in ac_list:
                    ac.status = "archived"
                    ac.change_type = "archived"
                    ac.version = ac.version + 1

            await session.flush()
            return True
        return False

    @staticmethod
    async def lock(id_val: str, project_id: str, session: AsyncSession, locked_by: str = "user") -> Optional[Dict[str, Any]]:
        rid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.id == rid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        req = result.scalar_one_or_none()
        if req:
            if req.is_locked:
                return RequirementRepository._serialize(req)
            req.is_locked = True
            req.locked_by = locked_by
            req.locked_at = datetime.now(timezone.utc)
            await session.flush()
            await session.refresh(req)
            # Log LOCK event
            try:
                await ArtifactEventLogRepository.log_event(
                    artifact_type="requirement",
                    artifact_id=str(req.id),
                    action="LOCK",
                    session=session,
                    old_value={"is_locked": False},
                    new_value={"is_locked": True, "locked_by": locked_by},
                    performed_by=locked_by,
                    project_id=str(req.project_id),
                )
            except Exception as log_err:
                logger.warning(f"[EVENT LOG] Failed to log requirement lock: {log_err}")
            return RequirementRepository._serialize(req)
        return None

    @staticmethod
    async def unlock(id_val: str, project_id: str, session: AsyncSession, unlocked_by: str = "user") -> Optional[Dict[str, Any]]:
        """
        Unlock a requirement so it can be modified again.

        Raises:
            PermissionError: If the requirement is locked by a different user
                than the one attempting to unlock it
        """
        rid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(RequirementModel).where(RequirementModel.id == rid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        req = result.scalar_one_or_none()
        if req:
            if not req.is_locked:
                return RequirementRepository._serialize(req)
            # Authorization check: only the lock owner can unlock the requirement
            if req.locked_by != unlocked_by:
                logger.warning(
                    f"[UNLOCK] Denied unlock of requirement {req.id}: "
                    f"locked by {req.locked_by}, attempted by {unlocked_by}"
                )
                raise PermissionError(
                    f"Requirement is locked by {req.locked_by}. "
                    f"Cannot be unlocked by {unlocked_by}."
                )
            old_locked_by = req.locked_by
            req.is_locked = False
            req.locked_by = None
            req.locked_at = None
            await session.flush()
            await session.refresh(req)
            # Log UNLOCK event
            try:
                await ArtifactEventLogRepository.log_event(
                    artifact_type="requirement",
                    artifact_id=str(req.id),
                    action="UNLOCK",
                    session=session,
                    old_value={"is_locked": True, "locked_by": old_locked_by},
                    new_value={"is_locked": False},
                    performed_by=unlocked_by,
                    project_id=str(req.project_id),
                )
            except Exception as log_err:
                logger.warning(f"[EVENT LOG] Failed to log requirement unlock: {log_err}")
            return RequirementRepository._serialize(req)
        return None

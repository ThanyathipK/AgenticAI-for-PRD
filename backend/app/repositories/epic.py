"""
Epic repository operations.
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EpicModel
from app.repositories.base import as_uuid, serialize_epic
from app.repositories.event_log import ArtifactEventLogRepository

logger = logging.getLogger(__name__)


class EpicRepository:
    """Handles Epics of the project."""

    @staticmethod
    async def get_active_by_project(project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.project_id == pid, EpicModel.status == "active").order_by(EpicModel.updated_at.desc())
        result = await session.execute(stmt)
        epic = result.scalars().first()
        if epic:
            return serialize_epic(epic, with_status=True)
        return None

    @staticmethod
    async def save_or_update_epic(project_id: str, epic_name: str, session: AsyncSession, version: int = 1, performed_by: str = "automated_agent") -> Dict[str, Any]:
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.project_id == pid, EpicModel.status == "active").order_by(EpicModel.updated_at.desc())
        result = await session.execute(stmt)
        epic = result.scalars().first()

        name_to_use = epic_name.strip() if (epic_name and epic_name.strip()) else "Untitled Epic"
        if epic:
            old_name = epic.epic_name
            old_version = epic.version
            if epic_name and epic_name.strip():
                epic.epic_name = name_to_use
            epic.version = version
            await session.flush()
            await session.refresh(epic)
            # Log UPDATE event
            try:
                await ArtifactEventLogRepository.log_event(
                    artifact_type="epic",
                    artifact_id=str(epic.id),
                    action="UPDATE",
                    session=session,
                    old_value={"epic_name": old_name, "version": old_version},
                    new_value={"epic_name": epic.epic_name, "version": epic.version},
                    performed_by=performed_by,
                    project_id=str(epic.project_id),
                )
            except Exception as log_err:
                logger.warning(f"[EVENT LOG] Failed to log epic update: {log_err}")
        else:
            epic = EpicModel(
                project_id=pid,
                epic_name=name_to_use,
                version=version,
                status="active"
            )
            session.add(epic)
            await session.flush()
            await session.refresh(epic)
            # Log CREATE event
            try:
                await ArtifactEventLogRepository.log_event(
                    artifact_type="epic",
                    artifact_id=str(epic.id),
                    action="CREATE",
                    session=session,
                    old_value=None,
                    new_value={"epic_name": epic.epic_name, "version": epic.version, "status": epic.status},
                    performed_by=performed_by,
                    project_id=str(epic.project_id),
                )
            except Exception as log_err:
                logger.warning(f"[EVENT LOG] Failed to log epic create: {log_err}")

        return serialize_epic(epic, with_status=True)

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)
        epic = EpicModel(
            project_id=pid,
            epic_name=data.get("epic_name", "Untitled Epic"),
            version=data.get("version", 1)
        )
        session.add(epic)
        await session.flush()
        await session.refresh(epic)
        return serialize_epic(epic)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        eid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.id == eid, EpicModel.project_id == pid)
        result = await session.execute(stmt)
        epic = result.scalar_one_or_none()
        if epic:
            return serialize_epic(epic)
        return None

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.project_id == pid)
        result = await session.execute(stmt)
        epics = result.scalars().all()
        return [serialize_epic(e) for e in epics]

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        eid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.id == eid, EpicModel.project_id == pid)
        result = await session.execute(stmt)
        epic = result.scalar_one_or_none()
        if epic:
            if "epic_name" in updates:
                epic.epic_name = updates["epic_name"]
            if "version" in updates:
                epic.version = updates["version"]
            await session.flush()
            await session.refresh(epic)
            return serialize_epic(epic)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        eid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(EpicModel).where(EpicModel.id == eid, EpicModel.project_id == pid)
        result = await session.execute(stmt)
        epic = result.scalar_one_or_none()
        if epic:
            await session.delete(epic)
            await session.flush()
            return True
        return False

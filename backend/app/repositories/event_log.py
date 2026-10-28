"""
Append-only immutable event log repository.

Stores historical records of CREATE, UPDATE, DELETE, ARCHIVE, LOCK, UNLOCK
events. Never overwrites or deletes existing records — purely historical
traceability.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactEventLogModel, ProjectModel
from app.repositories.base import serialize_event_log

logger = logging.getLogger(__name__)


class ArtifactEventLogRepository:
    """Append-only immutable event log for tracking all artifact modifications."""

    VALID_ACTIONS = {"CREATE", "UPDATE", "DELETE", "ARCHIVE", "LOCK", "UNLOCK"}

    @staticmethod
    async def log_event(
        artifact_type: str,
        artifact_id: str,
        action: str,
        session: AsyncSession,
        old_value: Optional[Dict[str, Any]] = None,
        new_value: Optional[Dict[str, Any]] = None,
        performed_by: str = "automated_agent",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Append a single event to the artifact event log.
        This is the ONLY write operation — logs are never updated or deleted.

        Args:
            artifact_type: Type of artifact (e.g. 'epic', 'requirement',
                'user_story', 'acceptance_criteria', 'prd')
            artifact_id: UUID string of the modified artifact
            action: One of CREATE, UPDATE, DELETE, ARCHIVE, LOCK, UNLOCK
            session: Active database session
            old_value: Snapshot of the artifact before the change (optional)
            new_value: Snapshot of the artifact after the change (optional)
            performed_by: Identifier of who performed the action

        Returns:
            The created event log entry
        """
        action_upper = action.upper()
        if action_upper not in ArtifactEventLogRepository.VALID_ACTIONS:
            raise ValueError(
                f"Invalid action '{action}'. Must be one of: "
                f"{', '.join(sorted(ArtifactEventLogRepository.VALID_ACTIONS))}"
            )

        owner_id = uuid.UUID(str(user_id)) if user_id else None
        project_uuid = uuid.UUID(str(project_id)) if project_id else None
        if owner_id is None and project_uuid is not None:
            owner_id = await session.scalar(
                select(ProjectModel.user_id).where(ProjectModel.id == project_uuid)
            )

        log_entry = ArtifactEventLogModel(
            event_id=uuid.uuid4(),
            project_id=project_uuid,
            user_id=owner_id,
            artifact_type=artifact_type,
            artifact_id=str(artifact_id),
            action=action_upper,
            old_value=old_value,
            new_value=new_value,
            performed_by=performed_by
        )
        session.add(log_entry)
        await session.flush()
        await session.refresh(log_entry)

        logger.info(f"[EVENT LOG] {action_upper} on {artifact_type} ({artifact_id}) by {performed_by}")

        return serialize_event_log(log_entry)

    @staticmethod
    async def get_by_artifact(
        artifact_type: str,
        artifact_id: str,
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """EVENT 7.2 — Read one artifact's owner-scoped history, newest first."""
        stmt = (
            select(ArtifactEventLogModel)
            .where(
                ArtifactEventLogModel.artifact_type == artifact_type,
                ArtifactEventLogModel.artifact_id == str(artifact_id)
            )
        )
        if user_id is not None:
            stmt = stmt.where(ArtifactEventLogModel.user_id == uuid.UUID(str(user_id)))
        stmt = stmt.order_by(ArtifactEventLogModel.timestamp.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        events = result.scalars().all()
        return [serialize_event_log(e) for e in events]

    @staticmethod
    async def get_by_project(
        project_id: str,
        session: AsyncSession,
        artifact_type: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """EVENT 7.4 — Read project history with optional artifact/action filters.

        Retrieve all events for a project, optionally filtered by artifact_type
        and/or action.

        Since artifact_event_logs has no project_id column, this queries by
        artifact_type and artifact_id patterns. For project-scoped queries, use
        the dedicated get_by_project_artifact_types method with known artifact IDs.

        This method is a convenience wrapper — for production use, consider
        querying by known artifact IDs from the project.
        """
        conditions = [ArtifactEventLogModel.project_id == uuid.UUID(str(project_id))]
        if artifact_type:
            conditions.append(ArtifactEventLogModel.artifact_type == artifact_type)
        if action:
            conditions.append(ArtifactEventLogModel.action == action.upper())

        stmt = select(ArtifactEventLogModel)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(ArtifactEventLogModel.timestamp.desc()).limit(limit).offset(offset)

        result = await session.execute(stmt)
        events = result.scalars().all()
        return [serialize_event_log(e) for e in events]

    @staticmethod
    async def get_by_action(
        action: str,
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """EVENT 7.3 — Read owner-scoped history for one action, newest first."""
        action_upper = action.upper()
        stmt = (
            select(ArtifactEventLogModel)
            .where(ArtifactEventLogModel.action == action_upper)
        )
        if user_id is not None:
            stmt = stmt.where(ArtifactEventLogModel.user_id == uuid.UUID(str(user_id)))
        stmt = stmt.order_by(ArtifactEventLogModel.timestamp.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        events = result.scalars().all()
        return [serialize_event_log(e) for e in events]

    @staticmethod
    async def get_recent(
        session: AsyncSession,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """EVENT 7.1 — Read recent owner-scoped history across artifacts."""
        stmt = (
            select(ArtifactEventLogModel)
        )
        if user_id is not None:
            stmt = stmt.where(ArtifactEventLogModel.user_id == uuid.UUID(str(user_id)))
        stmt = stmt.order_by(ArtifactEventLogModel.timestamp.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        events = result.scalars().all()
        return [serialize_event_log(e) for e in events]

"""LOCK 5.0 — Generic locking for requirements and PRD sections only.

The service verifies artifact/project association, mutates lock columns, and
best-effort appends an artifact event. Lock ownership is the supplied string,
not an authenticated user id. Reads are not row-locked, so concurrent lock or
unlock requests can race; database transaction commit is owned by the route
dependency.
"""
import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PRDSectionModel, RequirementModel

logger = logging.getLogger("app.lock_service")


class ArtifactLockError(Exception):
    """Raised when an artifact is locked and cannot be modified."""
    def __init__(self, artifact_type: str, artifact_id: str, locked_by: str, locked_at: str, message: str = ""):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.locked_by = locked_by
        self.locked_at = locked_at
        self.message = message or f"{artifact_type.capitalize()} is locked by {locked_by} at {locked_at}. Unlock it before modifying."
        super().__init__(self.message)


class LockService:
    """
    Lock and unlock requirements and PRD sections.
    """

    # LOCK 5.0.1 — The complete supported-type map. `project` and `document` are
    #              unsupported even though some older comments/callers name them.
    ARTIFACT_CONFIG: ClassVar[Dict[str, Dict[str, Any]]] = {
        "requirement": {
            "model": RequirementModel,
            "id_field": "id",
            "lock_field": "is_locked",
            "locked_by_field": "locked_by",
            "locked_at_field": "locked_at",
        },
        "prd_section": {
            "model": PRDSectionModel,
            "id_field": "id",
            "lock_field": "is_locked",
            "locked_by_field": "locked_by",
            "locked_at_field": "locked_at",
        },
    }

    @staticmethod
    async def _resolve_artifact_project_id(
        artifact_type: str,
        artifact: Any,
        session: AsyncSession
    ) -> Optional[str]:
        """
        Resolve the project_id that owns the given artifact.

        Returns:
            The owning project_id as a string, or None if the artifact type
            has no project association.
        """
        try:
            if artifact_type in ("requirement", "prd_section"):
                pid = getattr(artifact, "project_id", None)
                return str(pid) if pid is not None else None
        except Exception:
            logger.exception(f"[LOCK] Failed to resolve project ownership for {artifact_type} {getattr(artifact, 'id', 'unknown')}")

        return None

    @staticmethod
    async def _verify_project_ownership(
        artifact_type: str,
        project_id: Optional[str],
        artifact: Any,
        session: AsyncSession
    ) -> None:
        """
        Verify that the artifact belongs to the given project.

        Args:
            artifact_type: Type of artifact
            project_id: Project UUID to verify ownership against
            artifact: The fetched artifact model instance
            session: Database session

        Raises:
            ValueError: If the artifact does not belong to the project
        """
        if project_id is None:
            return

        owning_project_id = await LockService._resolve_artifact_project_id(artifact_type, artifact, session)
        if owning_project_id is None or str(owning_project_id) != str(project_id):
            # Do not leak whether the artifact exists in another project.
            raise ValueError(f"{artifact_type.capitalize()} not found in project")

    @staticmethod
    async def lock_artifact(
        artifact_type: str,
        artifact_id: str,
        session: AsyncSession,
        locked_by: str = "user",
        project_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """LOCK 5.1 — Apply a generic artifact lock.

        Trigger: LOCK 4.1 or LOCK 4.6.
        Input: supported type, artifact UUID, project id and supplied owner label.
        Calls: ownership resolution and ArtifactEventLogRepository.
        Branches: same owner -> no-op; different owner -> ArtifactLockError.
        Writes: lock columns plus best-effort event log; caller owns commit.
        Failure: invalid/absent/wrong-project artifact fails closed.

        Lock an artifact to prevent modifications.
        
        Args:
            artifact_type: Either requirement or prd_section
            artifact_id: UUID of the artifact
            session: Database session
            locked_by: Who is locking the artifact
            project_id: UUID of the project that must own the artifact. If provided,
                verification fails with ValueError when the artifact does not belong
                to the project.
        
        Returns:
            Dict with lock metadata
        
        Raises:
            ValueError: If artifact_type is not supported, if the artifact is not found,
                or if the artifact does not belong to the given project
            ArtifactLockError: If the artifact is already locked by a different user
        """
        if artifact_type not in LockService.ARTIFACT_CONFIG:
            raise ValueError(f"Unsupported artifact type: {artifact_type}")

        config = LockService.ARTIFACT_CONFIG[artifact_type]
        model = config["model"]
        id_field = config["id_field"]
        lock_field = config["lock_field"]
        locked_by_field = config["locked_by_field"]
        locked_at_field = config["locked_at_field"]

        # Query the artifact
        stmt = select(model).where(getattr(model, id_field) == artifact_id)
        result = await session.execute(stmt)
        artifact = result.scalar_one_or_none()

        if not artifact:
            raise ValueError(f"{artifact_type.capitalize()} not found")

        # Verify the artifact belongs to the specified project
        await LockService._verify_project_ownership(artifact_type, project_id, artifact, session)

        # Check if already locked
        if getattr(artifact, lock_field):
            lock_holder = getattr(artifact, locked_by_field)
            locked_at_value = getattr(artifact, locked_at_field)

            # If the same user attempts to re-lock, treat it as an idempotent no-op.
            if lock_holder == locked_by:
                logger.info(f"[LOCK] {artifact_type} {artifact_id} is already locked by {locked_by}. Treating as re-lock no-op.")
                return LockService._serialize_lock_metadata(artifact, config, artifact_type)

            # A different user holds the lock; raise an error so the caller is
            # explicitly notified that someone else owns the lock.
            logger.warning(
                f"[LOCK] Denied lock of {artifact_type} {artifact_id}: "
                f"already locked by {lock_holder}, attempted by {locked_by}"
            )
            raise ArtifactLockError(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                locked_by=lock_holder or "unknown",
                locked_at=locked_at_value.isoformat() if locked_at_value else "unknown time",
                message=(
                    f"{artifact_type.capitalize()} is already locked by {lock_holder} "
                    f"at {locked_at_value.isoformat() if locked_at_value else 'unknown time'}. "
                    f"Unlock it before modifying."
                )
            )

        # Lock the artifact
        setattr(artifact, lock_field, True)
        setattr(artifact, locked_by_field, locked_by)
        setattr(artifact, locked_at_field, datetime.now(timezone.utc))

        await session.flush()
        await session.refresh(artifact)

        # Log the LOCK event
        try:
            from app.repositories import ArtifactEventLogRepository
            await ArtifactEventLogRepository.log_event(
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                action="LOCK",
                session=session,
                old_value={"is_locked": False},
                new_value={
                    "is_locked": True,
                    "locked_by": locked_by,
                    "locked_at": datetime.now(timezone.utc).isoformat(),
                },
                performed_by=locked_by,
                project_id=project_id,
            )
        except Exception as log_err:
            logger.warning(f"[EVENT LOG] Failed to log lock event for {artifact_type} {artifact_id}: {log_err}")

        logger.info(f"[LOCK] {artifact_type} {artifact_id} locked by {locked_by}")
        return LockService._serialize_lock_metadata(artifact, config, artifact_type)

    @staticmethod
    async def unlock_artifact(
        artifact_type: str,
        artifact_id: str,
        session: AsyncSession,
        unlocked_by: str = "user",
        project_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """LOCK 5.2 — Clear a generic artifact lock.

        Trigger: LOCK 4.2 or LOCK 4.7.
        Branches: unlocked -> no-op; different nonnull owner -> PermissionError;
            null owner -> clearable by any supplied label.
        Writes: clears lock columns plus best-effort event log; caller owns commit.
        Failure: invalid/absent/wrong-project artifact fails closed.

        Unlock an artifact to allow modifications.
        
        Args:
            artifact_type: Type of artifact
            artifact_id: UUID of the artifact
            session: Database session
            unlocked_by: Who is unlocking the artifact
            project_id: UUID of the project that must own the artifact. If provided,
                verification fails with ValueError when the artifact does not belong
                to the project.
        
        Returns:
            Dict with lock metadata
        
        Raises:
            ValueError: If artifact_type is not supported, if the artifact is not found,
                or if the artifact does not belong to the given project
            PermissionError: If the artifact is locked by a different user
                than the one attempting to unlock it
        """
        if artifact_type not in LockService.ARTIFACT_CONFIG:
            raise ValueError(f"Unsupported artifact type: {artifact_type}")

        config = LockService.ARTIFACT_CONFIG[artifact_type]
        model = config["model"]
        id_field = config["id_field"]
        lock_field = config["lock_field"]
        locked_by_field = config["locked_by_field"]
        locked_at_field = config["locked_at_field"]

        # Query the artifact
        stmt = select(model).where(getattr(model, id_field) == artifact_id)
        result = await session.execute(stmt)
        artifact = result.scalar_one_or_none()

        if not artifact:
            raise ValueError(f"{artifact_type.capitalize()} not found")

        # Verify the artifact belongs to the specified project
        await LockService._verify_project_ownership(artifact_type, project_id, artifact, session)

        # Check if already unlocked
        if not getattr(artifact, lock_field):
            logger.info(f"[UNLOCK] {artifact_type} {artifact_id} is already unlocked.")
            return LockService._serialize_lock_metadata(artifact, config, artifact_type)

        # Authorization check: only the lock owner can unlock the artifact.
        # A lock with NO recorded owner (e.g. the born-locked ``contents``
        # section seeded by the system with ``locked_by = NULL``) is not held
        # by any user, so any requester may clear it.
        locked_by_value = getattr(artifact, locked_by_field)
        if locked_by_value and locked_by_value != unlocked_by:
            logger.warning(
                f"[UNLOCK] Denied unlock of {artifact_type} {artifact_id}: "
                f"locked by {locked_by_value}, attempted by {unlocked_by}"
            )
            raise PermissionError(
                f"{artifact_type.capitalize()} is locked by {locked_by_value}. "
                f"Cannot be unlocked by {unlocked_by}."
            )

        # Store old values for logging
        old_locked_by = getattr(artifact, locked_by_field)
        old_locked_at = getattr(artifact, locked_at_field)

        # Unlock the artifact
        setattr(artifact, lock_field, False)
        setattr(artifact, locked_by_field, None)
        setattr(artifact, locked_at_field, None)

        await session.flush()
        await session.refresh(artifact)

        # Log the UNLOCK event
        try:
            from app.repositories import ArtifactEventLogRepository
            await ArtifactEventLogRepository.log_event(
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                action="UNLOCK",
                session=session,
                old_value={
                    "is_locked": True,
                    "locked_by": old_locked_by,
                    "locked_at": old_locked_at.isoformat() if old_locked_at else None,
                },
                new_value={"is_locked": False},
                performed_by=unlocked_by,
                project_id=project_id,
            )
        except Exception as log_err:
            logger.warning(f"[EVENT LOG] Failed to log unlock event for {artifact_type} {artifact_id}: {log_err}")

        logger.info(f"[UNLOCK] {artifact_type} {artifact_id} unlocked by {unlocked_by}")
        return LockService._serialize_lock_metadata(artifact, config, artifact_type)

    @staticmethod
    async def get_lock_status(artifact_type: str, artifact_id: str, session: AsyncSession, project_id: Optional[str] = None) -> Dict[str, Any]:
        """LOCK 5.3 — Read serialized lock metadata after project association check.

        Get the lock status of an artifact.
        
        Args:
            artifact_type: Type of artifact
            artifact_id: UUID of the artifact
            session: Database session
            project_id: UUID of the project that must own the artifact. If provided,
                verification fails with ValueError when the artifact does not belong
                to the project.
        
        Returns:
        
        Raises:
            ValueError: If artifact_type is not supported, if the artifact is not found,
                or if the artifact does not belong to the given project
        """
        if artifact_type not in LockService.ARTIFACT_CONFIG:
            raise ValueError(f"Unsupported artifact type: {artifact_type}")

        config = LockService.ARTIFACT_CONFIG[artifact_type]
        model = config["model"]
        id_field = config["id_field"]

        stmt = select(model).where(getattr(model, id_field) == artifact_id)
        result = await session.execute(stmt)
        artifact = result.scalar_one_or_none()

        if not artifact:
            raise ValueError(f"{artifact_type.capitalize()} not found")

        # Verify the artifact belongs to the specified project
        await LockService._verify_project_ownership(artifact_type, project_id, artifact, session)

        return LockService._serialize_lock_metadata(artifact, config, artifact_type)

    @staticmethod
    def _serialize_lock_metadata(artifact: Any, config: Dict[str, str], artifact_type: str) -> Dict[str, Any]:
        """Serialize lock metadata from an artifact model."""
        return {
            "artifact_type": artifact_type,
            "artifact_id": str(getattr(artifact, config["id_field"])),
            "is_locked": bool(getattr(artifact, config["lock_field"])) if getattr(artifact, config["lock_field"]) is not None else False,
            "locked_by": getattr(artifact, config["locked_by_field"]),
            "locked_at": getattr(artifact, config["locked_at_field"]).isoformat() if getattr(artifact, config["locked_at_field"]) else None,
        }

    @staticmethod
    def raise_if_locked(artifact_type: str, artifact_id: str, lock_info: Dict[str, Any], message: str = ""):
        """
        Raise ArtifactLockError if the artifact is locked.
        
        Args:
            artifact_type: Type of artifact
            artifact_id: UUID of the artifact
            lock_info: Lock metadata dict from get_lock_status or _serialize_lock_metadata
            message: Optional custom error message. If empty, a default message is used.
        
        Raises:
            ArtifactLockError: If artifact is locked
        """
        if lock_info.get("is_locked"):
            raise ArtifactLockError(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                locked_by=lock_info.get("locked_by", "unknown"),
                locked_at=lock_info.get("locked_at", "unknown time"),
                message=message
            )

    @staticmethod
    def raise_if_locked_model(artifact_type: str, artifact: Any, message: str = ""):
        """
        Raise ArtifactLockError if the given artifact model is locked.
        
        Args:
            artifact_type: Type of artifact
            artifact: The fetched artifact model instance
            message: Optional custom error message. If empty, a default message is used.
        
        Raises:
            ValueError: If artifact_type is not supported
            ArtifactLockError: If artifact is locked
        """
        if artifact_type not in LockService.ARTIFACT_CONFIG:
            raise ValueError(f"Unsupported artifact type: {artifact_type}")

        config = LockService.ARTIFACT_CONFIG[artifact_type]
        lock_field = config["lock_field"]

        if getattr(artifact, lock_field):
            lock_info = LockService._serialize_lock_metadata(artifact, config, artifact_type)
            LockService.raise_if_locked(
                artifact_type,
                str(getattr(artifact, config["id_field"])),
                lock_info,
                message=message
            )

"""
PRD Section and PRD Section Version repository operations.

A PRD is stored as a COLLECTION of lockable, versioned sections
(``prd_sections``) instead of a single document blob. This module owns the
per-section CRUD, the append-only per-section version history, and the lock
enforcement for section edits.
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.lock_service import LockService
from app.models import PRDSectionModel, PRDSectionVersionModel
from app.repositories.base import as_uuid, serialize_prd_section, serialize_prd_section_version

logger = logging.getLogger(__name__)


# PRD-SECTION 7.x — prd_sections + prd_section_versions persistence.
#   7.1 get_by_project         ← route 4.1, domain 6.6/6.7, VERSION 6.1/6.8
#   7.2 get_by_key             ← route lookups 4.2–4.6
#   7.3 create                 ← domain seeding 6.7 and ARCHITECT 5.1 sync
#   7.4 update_content         ← orchestration 5.2/5.3 (append-only + lock-gated)
#   7.5 version-history reads  ← routes 4.7/4.3
# The per-part history table is append-only: there is no update/delete for
# prd_section_versions.
class PRDSectionRepository:
    """Handles the editable / lockable PARTS of a project's PRD document."""

    @staticmethod
    # PRD-SECTION 7.1 — SELECT the project's parts in document order (the order 6.4
    #             stitching depends on, so this ordering is a document contract).
    async def get_by_project(project_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        """List all sections of the project in document order, with their
        current version number attached."""
        pid = as_uuid(project_id)
        stmt = select(PRDSectionModel).where(
            PRDSectionModel.project_id == pid
        ).order_by(PRDSectionModel.section_order.asc(), PRDSectionModel.created_at.asc())
        result = await session.execute(stmt)
        rows = result.scalars().all()

        # One grouped query for the latest version number of every section.
        latest: Dict[Any, int] = {}
        if rows:
            stmt_ver = (
                select(
                    PRDSectionVersionModel.section_id,
                    func.max(PRDSectionVersionModel.version_number),
                )
                .where(PRDSectionVersionModel.section_id.in_([r.id for r in rows]))
                .group_by(PRDSectionVersionModel.section_id)
            )
            res_ver = await session.execute(stmt_ver)
            latest = {sid: num for sid, num in res_ver.all()}

        return [
            serialize_prd_section(row, current_version=latest.get(row.id))
            for row in rows
        ]

    @staticmethod
    async def get_by_key(section_key: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        """Fetch a single section by its stable key within a project."""
        row = await PRDSectionRepository.get_model_by_key(section_key, project_id, session)
        if not row:
            return None
        stmt_ver = select(func.max(PRDSectionVersionModel.version_number)).where(
            PRDSectionVersionModel.section_id == row.id
        )
        res_ver = await session.execute(stmt_ver)
        return serialize_prd_section(row, current_version=res_ver.scalar() or 1)

    @staticmethod
    async def get_model_by_key(section_key: str, project_id: str, session: AsyncSession) -> Optional[PRDSectionModel]:
        """Fetch the raw model instance (for lock checks / mutations)."""
        pid = as_uuid(project_id)
        stmt = select(PRDSectionModel).where(
            PRDSectionModel.section_key == section_key,
            PRDSectionModel.project_id == pid,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        """Create a section row (and its initial version-history row)."""
        pid = as_uuid(project_id)
        section = PRDSectionModel(
            project_id=pid,
            section_key=data.get("section_key", "section"),
            title=data.get("title", "Untitled Section"),
            content=data.get("content", ""),
            section_order=data.get("section_order", 0),
            content_source=data.get("content_source", "ai"),
            ai_generatable=bool(data.get("ai_generatable", True)),
            review_status=data.get("review_status", "draft"),
            is_locked=bool(data.get("is_locked", False)),
            locked_by=data.get("locked_by"),
            locked_at=data.get("locked_at"),
        )
        session.add(section)
        await session.flush()
        await session.refresh(section)

        # The initial content is version 1 of the section history.
        await PRDSectionVersionRepository.create(
            str(section.id),
            {
                "content": section.content,
                "changed_by": data.get("initial_changed_by", "system"),
                "change_summary": data.get("initial_change_summary", "Section created."),
            },
            session,
        )
        return serialize_prd_section(section, current_version=1)

    @staticmethod
    # PRD-SECTION 7.4 — THE part-content write. APPEND-ONLY guarantee: the previous
    #             content is snapshotted as a prd_section_versions row BEFORE the
    #             materialized `content` is replaced, so nothing is silently overwritten.
    #             Lock enforcement (the finest gate) refuses a locked part, and the
    #             returned payload carries the new per-part version number.
    async def update_content(
        section_id: str,
        project_id: str,
        content: str,
        session: AsyncSession,
        *,
        changed_by: str = "user",
        change_summary: Optional[str] = None,
        review_status: Optional[str] = None,
        content_source: Optional[str] = None,
        ai_generatable: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Replace the current content of one section.

        APPEND-ONLY GUARANTEE: the previous content is never overwritten
        silently — a new ``prd_section_versions`` row is inserted first, then
        the materialized ``content`` is updated. Lock enforcement refuses the
        update when the section is locked.

        ``content_source`` records who last wrote the part. ``ai_generatable``
        is changed only when explicitly supplied; an ordinary manual edit must
        not become an implicit permanent regeneration lock.
        """
        sid = as_uuid(section_id)
        pid = as_uuid(project_id)
        stmt = select(PRDSectionModel).where(
            PRDSectionModel.id == sid, PRDSectionModel.project_id == pid
        )
        result = await session.execute(stmt)
        section = result.scalar_one_or_none()
        if not section:
            return None

        # LOCK ENFORCEMENT: cannot modify a locked PRD section.
        LockService.raise_if_locked_model(
            "prd_section",
            section,
            message=f"PRD section '{section.section_key}' is locked by "
                    f"{section.locked_by or 'unknown'}. Unlock it before modifying.",
        )

        await PRDSectionVersionRepository.create(section.id, {
            "content": content,
            "changed_by": changed_by,
            "change_summary": change_summary,
        }, session)

        section.content = content
        if content_source:
            section.content_source = content_source
        if ai_generatable is not None:
            section.ai_generatable = bool(ai_generatable)
        if review_status:
            section.review_status = review_status
        await session.flush()
        await session.refresh(section)

        stmt_ver = select(func.max(PRDSectionVersionModel.version_number)).where(
            PRDSectionVersionModel.section_id == section.id
        )
        res_ver = await session.execute(stmt_ver)
        current_version = res_ver.scalar() or 1
        return serialize_prd_section(section, current_version=current_version)

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        """Delete a section row (used by tests; production flows never delete)."""
        sid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(PRDSectionModel).where(
            PRDSectionModel.id == sid, PRDSectionModel.project_id == pid
        )
        result = await session.execute(stmt)
        section = result.scalar_one_or_none()
        if section:
            LockService.raise_if_locked_model("prd_section", section)
            await session.delete(section)
            await session.flush()
            return True
        return False


class PRDSectionVersionRepository:
    """
    Append-only immutable per-section version history.
    Every content change inserts a new row; nothing is updated or deleted.
    """

    @staticmethod
    async def create(section_id: Any, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        sid = as_uuid(section_id)
        stmt = select(func.max(PRDSectionVersionModel.version_number)).where(
            PRDSectionVersionModel.section_id == sid
        )
        result = await session.execute(stmt)
        max_version = result.scalar() or 0

        version = PRDSectionVersionModel(
            section_id=sid,
            version_number=max_version + 1,
            content=data.get("content", ""),
            changed_by=data.get("changed_by", "user"),
            change_summary=data.get("change_summary"),
        )
        session.add(version)
        await session.flush()
        await session.refresh(version)
        return serialize_prd_section_version(version)

    @staticmethod
    # PRD-SECTION 7.5 — Per-part history reads: all versions of one part (route 4.7,
    #             API/test-only) and one specific version (the revert target of 4.3,
    #             also UI-unreachable). Both still back the
    #             append-only guarantee: the rows they read are what 4.4 appends.
    async def get_by_section(section_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        """List the version history of one section, newest first."""
        sid = as_uuid(section_id)
        stmt = select(PRDSectionVersionModel).where(
            PRDSectionVersionModel.section_id == sid
        ).order_by(PRDSectionVersionModel.version_number.desc())
        result = await session.execute(stmt)
        return [serialize_prd_section_version(v) for v in result.scalars().all()]

    @staticmethod
    async def get_by_version_number(
        section_id: str, version_number: int, session: AsyncSession
    ) -> Optional[Dict[str, Any]]:
        sid = as_uuid(section_id)
        stmt = select(PRDSectionVersionModel).where(
            PRDSectionVersionModel.section_id == sid,
            PRDSectionVersionModel.version_number == version_number,
        )
        result = await session.execute(stmt)
        v = result.scalar_one_or_none()
        return serialize_prd_section_version(v) if v else None

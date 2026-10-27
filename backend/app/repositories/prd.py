"""
PRD Document and PRD Version repository operations.
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PRDDocumentModel, PRDVersionModel
from app.repositories.base import as_uuid, serialize_prd_document, serialize_prd_version

logger = logging.getLogger(__name__)


class PRDDocumentRepository:
    """Handles generated PRD Documents of the project."""

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)
        prd = PRDDocumentModel(
            project_id=pid,
            version=data.get("version", 1),
            prd_markdown=data.get("prd_markdown", ""),
            mermaid_diagram=data.get("mermaid_diagram", "")
        )
        session.add(prd)
        await session.flush()
        await session.refresh(prd)
        return serialize_prd_document(prd)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        prdid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(PRDDocumentModel).where(PRDDocumentModel.id == prdid, PRDDocumentModel.project_id == pid)
        result = await session.execute(stmt)
        prd = result.scalar_one_or_none()
        if prd:
            return serialize_prd_document(prd)
        return None

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession, version: Optional[int] = None) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        if version is not None:
            stmt = select(PRDDocumentModel).where(PRDDocumentModel.project_id == pid, PRDDocumentModel.version == version)
        else:
            stmt = select(PRDDocumentModel).where(PRDDocumentModel.project_id == pid).order_by(PRDDocumentModel.version.desc())
        result = await session.execute(stmt)
        records = result.scalars().all()
        return [serialize_prd_document(prd) for prd in records]

    @staticmethod
    async def get_latest_for_project(
        project_id: str,
        session: AsyncSession,
        *,
        max_version: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """The NEWEST stored document row — optionally capped at ``max_version``.

        The project-state endpoint resolves the preview document through this
        helper instead of exact-matching the current version number: version
        counters advance independently of document writes (requirement merges,
        confirmed AI generations pin the ledger ahead of the document rows), so
        an exact match can find no row at all and the UI would fall back to the
        empty template skeleton instead of the real document. Ordering by
        ``version DESC`` with a ``<=`` cap returns the newest snapshot at or
        before the current version; when every stored row is newer than the cap
        the absolute newest snapshot is returned so the preview never goes
        blank.
        """
        pid = as_uuid(project_id)
        stmt = select(PRDDocumentModel).where(PRDDocumentModel.project_id == pid)
        if max_version is not None:
            stmt = stmt.where(PRDDocumentModel.version <= max_version)
        stmt = stmt.order_by(PRDDocumentModel.version.desc()).limit(1)
        result = await session.execute(stmt)
        prd = result.scalar_one_or_none()
        if prd:
            return serialize_prd_document(prd)
        if max_version is None:
            return None
        # Every stored row is newer than the cap — still surface the newest
        # document rather than nothing.
        return await PRDDocumentRepository.get_latest_for_project(project_id, session)

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        prdid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(PRDDocumentModel).where(PRDDocumentModel.id == prdid, PRDDocumentModel.project_id == pid)
        result = await session.execute(stmt)
        prd = result.scalar_one_or_none()
        if prd:
            if "version" in updates:
                prd.version = updates["version"]
            if "prd_markdown" in updates:
                prd.prd_markdown = updates["prd_markdown"]
            if "mermaid_diagram" in updates:
                prd.mermaid_diagram = updates["mermaid_diagram"]
            await session.flush()
            await session.refresh(prd)
            return serialize_prd_document(prd)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        prdid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(PRDDocumentModel).where(PRDDocumentModel.id == prdid, PRDDocumentModel.project_id == pid)
        result = await session.execute(stmt)
        prd = result.scalar_one_or_none()
        if prd:
            await session.delete(prd)
            await session.flush()
            return True
        return False


# VERSION 4.x — Immutable PRD-version ledger (prd_versions table). APPEND-ONLY:
#   there is no UPDATE/DELETE path — history is never rewritten.
#   4.1 create                ← VERSION 3.6 (every AI/manual/revert/restore snapshot)
#   4.2 get_previous          ← VERSION 3.8 (default diff base)
#   4.3 get_by_project        ← VERSION 2.1 (newest-first timeline)
#   4.4 get_by_version_number ← VERSION 2.4 and the restore (2.5) lookup
#   4.5 get_latest            ← VERSION 2.3, PROJECT 8.2.3 and CONFIRM 3.3.7 (pinned version)
class PRDVersionRepository:
    """
    Dedicated immutable PRD version repository.
    Each PRD generation creates a new version record.
    Never overwrites previous versions.
    Does NOT store version history inside requirement_states.
    """

    @staticmethod
    # VERSION 4.1 — INSERT one snapshot. Version numbering: `max(existing)+1` by
        #             default, or the caller's PINNED number (used by CONFIRM 3.3.7 so a
        #             merge that already advanced the requirement version does not
        #             double-bump). There is deliberately NO "same content" short-circuit.
    async def create(
        project_id: str,
        data: Dict[str, Any],
        session: AsyncSession,
        version_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        pid = as_uuid(project_id)

        # Determine the next version number for this project. The version ALWAYS
        # advances on every snapshot (AI generation, manual edit, revert) —
        # there is intentionally no "same content" short-circuit.
        #
        # ``version_number`` may be pinned by the caller (e.g. a confirmed
        # merge whose requirement-state version was already advanced exactly
        # once): the ledger row then adopts THAT number so the requirement
        # version and the ledger stay in lock-step instead of double-bumping.
        if version_number is not None:
            next_version = int(version_number)
        else:
            stmt = select(func.max(PRDVersionModel.version_number)).where(PRDVersionModel.project_id == pid)
            result = await session.execute(stmt)
            max_version = result.scalar() or 0
            next_version = max_version + 1

        version = PRDVersionModel(
            project_id=pid,
            version_number=next_version,
            generated_prd=data.get("generated_prd", ""),
            generated_by=data.get("generated_by", "automated_agent"),
            change_type=data.get("change_type", "ai"),
            change_summary=data.get("change_summary"),
            changed_sections=data.get("changed_sections"),
            semver=data.get("semver") or "1.0.0",
        )
        session.add(version)
        await session.flush()
        await session.refresh(version)
        return serialize_prd_version(version)

    @staticmethod
    # VERSION 4.2 — The snapshot IMMEDIATELY before a version number (highest number
    #             below it, or None). This is the default base for VERSION 3.8's diff and
    #             therefore for every "what changed in vN?" question in the UI.
    async def get_previous(project_id: str, before_version: int, session: AsyncSession) -> Optional[Dict[str, Any]]:
        """The version IMMEDIATELY before ``before_version`` (or None when none).

        Used for change/diff computation: the snapshot at ``before_version`` is
        compared against this predecessor's content.
        """
        pid = as_uuid(project_id)
        stmt = (
            select(PRDVersionModel)
            .where(
                PRDVersionModel.project_id == pid,
                PRDVersionModel.version_number < before_version,
            )
            .order_by(PRDVersionModel.version_number.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        v = result.scalar_one_or_none()
        return serialize_prd_version(v) if v else None

    @staticmethod
    # VERSION 4.3 — SELECT the project's snapshots NEWEST FIRST (the timeline order
    #             VERSION 2.1 promises to the UI).
    async def get_by_project(
        project_id: str,
        session: AsyncSession,
        *,
        limit: Optional[int] = None,
        before_version: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        conditions = [PRDVersionModel.project_id == pid]
        if before_version is not None:
            conditions.append(PRDVersionModel.version_number < before_version)
        stmt = (
            select(PRDVersionModel)
            .where(*conditions)
            .order_by(PRDVersionModel.version_number.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        records = result.scalars().all()
        return [serialize_prd_version(v) for v in records]

    @staticmethod
    # VERSION 4.4 — SELECT one snapshot by number (None when absent) — the lookup the
    #             diff (3.8) and the restore (2.5) both start from.
    async def get_by_version_number(project_id: str, version_number: int, session: AsyncSession) -> Optional[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(PRDVersionModel).where(
            PRDVersionModel.project_id == pid,
            PRDVersionModel.version_number == version_number
        )
        result = await session.execute(stmt)
        v = result.scalar_one_or_none()
        if v:
            return serialize_prd_version(v)
        return None

    @staticmethod
    # VERSION 4.5 — SELECT the newest snapshot. The two callers use it for VERSION
    #             BOOKKEEPING rather than display: 3.6 compares the new document against
    #             it, and CONFIRM 3.3.7 reads its number to pin the next ledger row.
    async def get_latest(project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(PRDVersionModel).where(PRDVersionModel.project_id == pid).order_by(PRDVersionModel.version_number.desc()).limit(1)
        result = await session.execute(stmt)
        v = result.scalar_one_or_none()
        if v:
            return serialize_prd_version(v)
        return None

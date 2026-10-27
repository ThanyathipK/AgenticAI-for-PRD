"""
Audit Result repository operations.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditResultModel, RequirementModel
from app.repositories.base import as_uuid, serialize_audit_result
from app.requirement_codes import UNASSIGNED_REQUIREMENT_CODE

logger = logging.getLogger(__name__)


class AuditResultRepository:
    """Handles Audit Results of the project."""

    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
        pid = as_uuid(project_id)

        if requirement_id is None:
            stmt = select(RequirementModel).where(RequirementModel.project_id == pid).order_by(RequirementModel.version.desc())
            res = await session.execute(stmt)
            req = res.scalars().first()
            if not req:
                req = RequirementModel(
                    project_id=pid,
                    requirement_code=UNASSIGNED_REQUIREMENT_CODE,
                    title="Untitled Requirement",
                    # Internal anchor for document-only audits. Archived keeps
                    # it out of the visible requirement board.
                    status="archived",
                )
                session.add(req)
                await session.flush()
            requirement_id = req.id

        ar = AuditResultModel(
            requirement_id=requirement_id,
            is_valid=data.get("is_valid", False),
            audit_version_reviewed=data.get("audit_version_reviewed", 1),
            passed_checks=data.get("passed_checks", []),
            failed_checks=data.get("failed_checks", []),
            findings=data.get("findings", []),
            source_references=data.get("source_references", []),
            verdict=data.get("verdict", "needs_clarification"),
            project_context=data.get("project_context", {}),
            checklist_id=data.get("checklist_id", "banking-core"),
            checklist_version=data.get("checklist_version", "1.0.0"),
        )
        session.add(ar)
        await session.flush()
        await session.refresh(ar)
        return serialize_audit_result(ar, pid)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        arid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AuditResultModel).join(RequirementModel).where(
            AuditResultModel.id == arid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        ar = result.scalar_one_or_none()
        if ar:
            return serialize_audit_result(ar, pid)
        return None

    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(AuditResultModel).join(RequirementModel).where(RequirementModel.project_id == pid)
        if requirement_id:
            stmt = stmt.where(AuditResultModel.requirement_id == requirement_id)
        stmt = stmt.order_by(AuditResultModel.updated_at.desc(), AuditResultModel.created_at.desc())
        result = await session.execute(stmt)
        return [serialize_audit_result(ar, pid) for ar in result.scalars().all()]

    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        arid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AuditResultModel).join(RequirementModel).where(
            AuditResultModel.id == arid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        ar = result.scalar_one_or_none()
        if ar:
            if "is_valid" in updates:
                ar.is_valid = updates["is_valid"]
            if "audit_version_reviewed" in updates:
                ar.audit_version_reviewed = updates["audit_version_reviewed"]
            if "passed_checks" in updates:
                ar.passed_checks = updates["passed_checks"]
            if "failed_checks" in updates:
                ar.failed_checks = updates["failed_checks"]
            if "findings" in updates:
                ar.findings = updates["findings"]
            if "source_references" in updates:
                ar.source_references = updates["source_references"]
            if "verdict" in updates:
                ar.verdict = updates["verdict"]
            if "project_context" in updates:
                ar.project_context = updates["project_context"]
            if "checklist_id" in updates:
                ar.checklist_id = updates["checklist_id"]
            if "checklist_version" in updates:
                ar.checklist_version = updates["checklist_version"]
            await session.flush()
            await session.refresh(ar)
            return serialize_audit_result(ar, pid)
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        arid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(AuditResultModel).join(RequirementModel).where(
            AuditResultModel.id == arid,
            RequirementModel.project_id == pid
        )
        result = await session.execute(stmt)
        ar = result.scalar_one_or_none()
        if ar:
            await session.delete(ar)
            await session.flush()
            return True
        return False

"""
Clarification Question repository operations.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditResultModel,
    ClarificationQuestionModel,
    RequirementModel,
    UserStoryModel,
)
from app.repositories.base import (
    as_uuid,
    compact_requirement_context,
    dt_iso,
    serialize_clarification_question,
)
from app.requirement_codes import UNASSIGNED_REQUIREMENT_CODE

logger = logging.getLogger(__name__)


def _serialize_requirement_context(value: Any) -> Optional[str]:
    """Normalize structured context for the database Text column."""
    return compact_requirement_context(value)


# AUDIT 4.x — Clarification-question persistence (clarification_questions table):
#   4.1 update         — the answer path (AUDIT 3.2); LOCK-GATED
#   4.2 create         — used by the requirement-state save (CONFIRMATION flow) when
#                        the staged clarification_questions list is persisted
#   4.3 get_project_id — ownership resolution for AUDIT 3.1
#   4.4 _resolve_story_id / _resolve_story_code — map a TICKET CODE to the story UUID
#                        (the helpers MATCHING 1.5 / AUDIT 2.6 bypass when they store a
#                        code or the sentinel "ALL" as target_user_story_id)
class ClarificationQuestionRepository:
    """Handles Clarification Questions of the project."""

    # CLARIFY 4.3 — Serialization helper used by the reads above: maps the stored
    #             target_user_story_id back to a human-readable TICKET CODE for the UI.
    #             This is the counterpart of AUDIT 4.4 (code → UUID), and it is why a
    #             target stored as a raw code (MATCHING 1.5) or the sentinel "ALL"
    #             (AUDIT 2.6) still renders sensibly in the clarification forms.
    @staticmethod
    async def _resolve_story_code(session: AsyncSession, story_id) -> Optional[str]:
        """Resolve a target user story UUID to its ticket code (US-xxx)."""
        if not story_id:
            return None
        stmt_code = select(UserStoryModel.ticket_code).where(UserStoryModel.id == story_id)
        res_code = await session.execute(stmt_code)
        return res_code.scalar_one_or_none()

    @staticmethod
    # AUDIT 4.4 — Ticket code → story UUID resolution, inserted at the persistence
    #             boundary. Callers that store a raw code (MATCHING 1.5) or a sentinel
    #             such as "ALL" (AUDIT 2.6) as `target_user_story_id` cannot be
    #             resolved here, so the question survives with a NULL target link.
    async def _resolve_story_id(pid, session: AsyncSession, target_us: str) -> Optional[uuid.UUID]:
        """Resolve ``target_user_story_id`` exactly like the legacy logic.

        A string that parses as a UUID is used as-is (with an existence check);
        anything that is NOT a valid UUID is resolved as a ticket code (US-xxx).
        """
        if not target_us:
            return None
        try:
            story_uuid = uuid.UUID(str(target_us))
            stmt_us = select(UserStoryModel).where(UserStoryModel.id == story_uuid)
            res_us = await session.execute(stmt_us)
            us = res_us.scalar_one_or_none()
            if us:
                return us.id
            return None
        except ValueError:
            stmt_us = select(UserStoryModel).join(RequirementModel).where(
                UserStoryModel.ticket_code == str(target_us),
                RequirementModel.project_id == pid
            ).order_by(RequirementModel.version.desc())
            res_us = await session.execute(stmt_us)
            us = res_us.scalars().first()
            if us:
                return us.id
            return None

    # AUDIT 4.2 — INSERT a clarification question (used when a confirmed merge
    #             persists the auditor's / matcher's / gatherer's questions, and by the
    #             audit-result writer). Resolves the target ticket code to a story UUID
    #             through 4.4 when one is available.
    @staticmethod
    async def create(project_id: str, data: Dict[str, Any], session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
        pid = as_uuid(project_id)

        # 1. Resolve requirement_id
        if requirement_id is None:
            stmt_req = select(RequirementModel).where(RequirementModel.project_id == pid).order_by(RequirementModel.version.desc())
            res_req = await session.execute(stmt_req)
            req = res_req.scalars().first()
            if not req:
                req = RequirementModel(
                    project_id=pid,
                    requirement_code=UNASSIGNED_REQUIREMENT_CODE,
                    title="Untitled Requirement",
                    # Internal anchor for document-only audits; never surface
                    # it as a product requirement.
                    status="archived",
                )
                session.add(req)
                await session.flush()
            requirement_id = req.id

        # 2. Find or create an AuditResultModel for this requirement
        stmt_ar = select(AuditResultModel).where(AuditResultModel.requirement_id == requirement_id)
        res_ar = await session.execute(stmt_ar)
        ar = res_ar.scalars().first()
        if not ar:
            ar = AuditResultModel(
                requirement_id=requirement_id,
                is_valid=False,
                audit_version_reviewed=1,
                passed_checks=[],
                failed_checks=[]
            )
            session.add(ar)
            await session.flush()

        # 3. Resolve target_user_story_id UUID
        target_us = data.get("target_user_story_id")
        story_id_val = await ClarificationQuestionRepository._resolve_story_id(pid, session, target_us)

        # Clarification questions are audit findings, not edits to the target
        # story. They may reference locked artifacts and retain their normal
        # resolved/status lifecycle.

        cq = ClarificationQuestionModel(
            audit_result_id=ar.id,
            checklist_category=data.get("checklist_category", "General"),
            target_user_story_id=story_id_val,
            question_text=data.get("question_text", ""),
            user_answer=data.get("user_answer"),
            is_resolved=data.get("is_resolved", False),
            source_references=data.get("source_references", []),
            provenance=data.get("provenance") or {"type": "System analysis"},
            target_requirement_id=data.get("target_requirement_id") or target_us,
            requirement_context=_serialize_requirement_context(data.get("requirement_context")),
        )
        session.add(cq)
        await session.flush()
        await session.refresh(cq)

        ret_story_id = await ClarificationQuestionRepository._resolve_story_code(session, cq.target_user_story_id)
        if not ret_story_id:
            ret_story_id = target_us

        return serialize_clarification_question(cq, pid, ret_story_id)

    @staticmethod
    async def get_by_id(id_val: str, project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        cqid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(ClarificationQuestionModel).join(
            AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id
        ).join(
            RequirementModel, AuditResultModel.requirement_id == RequirementModel.id
        ).where(ClarificationQuestionModel.id == cqid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        cq = result.scalar_one_or_none()
        if cq:
            ret_story_id = await ClarificationQuestionRepository._resolve_story_code(session, cq.target_user_story_id)
            return serialize_clarification_question(cq, pid, ret_story_id)
        return None

    # CLARIFY 4.2 — Repository read of a project's clarification questions (called by
    #             CLARIFY 4.1 inside the state load). NOTE the join route: questions
    #             are reached through audit_results → requirements, and when no
    #             requirement row matches it falls back to the project's newest
    #             requirement — i.e. the query never trusts
    #             user_stories.project_id (the documented schema-drift workaround).
    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession, requirement_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        if requirement_id:
            stmt = select(ClarificationQuestionModel).join(
                AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id
            ).where(AuditResultModel.requirement_id == requirement_id)
        else:
            # Project state must include every question regardless of which
            # requirement became the persistence anchor for a particular audit
            # run. Limiting this to the newest requirement made answered rows
            # disappear while older, unresolved duplicates remained visible.
            stmt = select(ClarificationQuestionModel).join(
                AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id
            ).join(
                RequirementModel, AuditResultModel.requirement_id == RequirementModel.id
            ).where(RequirementModel.project_id == pid).order_by(
                ClarificationQuestionModel.created_at.asc()
            )

        result = await session.execute(stmt)
        records = result.scalars().all()

        # Repair only exact legacy duplicates: if the stakeholder has answered
        # one copy of the same question, that answer applies to the same issue
        # wherever an old audit anchor stored it. Distinct questions remain
        # independently unresolved.
        answered_duplicates: Dict[tuple, str] = {}
        for cq in records:
            fingerprint = (
                cq.checklist_category,
                str(cq.question_text).strip(),
                str(cq.target_requirement_id or cq.target_user_story_id or ""),
            )
            if cq.is_resolved and cq.user_answer:
                answered_duplicates[fingerprint] = cq.user_answer
        for cq in records:
            fingerprint = (
                cq.checklist_category,
                str(cq.question_text).strip(),
                str(cq.target_requirement_id or cq.target_user_story_id or ""),
            )
            if not cq.is_resolved and fingerprint in answered_duplicates:
                cq.user_answer = answered_duplicates[fingerprint]
                cq.is_resolved = True

        out = []
        for cq in records:
            ret_story_id = await ClarificationQuestionRepository._resolve_story_code(session, cq.target_user_story_id)
            out.append(serialize_clarification_question(cq, pid, ret_story_id))
        return out

    # AUDIT 4.3 — Ownership lookup for the answer route (AUDIT 3.1): returns the
    #             question's project id WITHOUT exposing the question itself, so a
    #             cross-tenant caller gets 404 after the ownership check instead of a
    #             confirmation that the id exists.
    @staticmethod
    async def get_project_id(question_id: str, session: AsyncSession) -> Optional[str]:
        """
        Resolve the project that owns a clarification question.

        Routes that receive ONLY a ``question_id`` (e.g.
        ``POST /api/audit/respond/{question_id}``) cannot express ownership as a
        ``Depends`` — the project id is not in the path. They use this lookup to
        find the owning project and then gate on it. The join runs
        ``clarification_questions -> audit_results -> requirements``, so a
        question is only ever reachable through its own project.

        Args:
            question_id: Clarification-question UUID (string or UUID).
            session: Active database session.

        Returns:
            The owning project id as a string, or None when the question does
            not exist (also covers a dangling audit/requirement row).
        """
        cqid = as_uuid(question_id)
        stmt = select(RequirementModel.project_id).join(
            AuditResultModel, AuditResultModel.requirement_id == RequirementModel.id
        ).join(
            ClarificationQuestionModel,
            ClarificationQuestionModel.audit_result_id == AuditResultModel.id,
        ).where(ClarificationQuestionModel.id == cqid)
        result = await session.execute(stmt)
        project_id = result.scalars().first()
        return str(project_id) if project_id else None

    # AUDIT 4.1 — Project-scoped, lock-gated UPDATE (called by AUDIT 3.2): the row is
    #             located through its project (audit_result → requirement), a locked
    #             question raises ArtifactLockError, and the response is serialized.
    @staticmethod
    async def update(id_val: str, project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
        cqid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(ClarificationQuestionModel).join(
            AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id
        ).join(
            RequirementModel, AuditResultModel.requirement_id == RequirementModel.id
        ).where(ClarificationQuestionModel.id == cqid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        cq = result.scalar_one_or_none()
        if cq:
            if "checklist_category" in updates:
                cq.checklist_category = updates["checklist_category"]
            if "question_text" in updates:
                cq.question_text = updates["question_text"]
            if "user_answer" in updates:
                cq.user_answer = updates["user_answer"]
            if "is_resolved" in updates:
                cq.is_resolved = updates["is_resolved"]
            if "target_user_story_id" in updates:
                target_us = updates["target_user_story_id"]
                story_id_val = None
                if target_us:
                    try:
                        story_uuid = uuid.UUID(str(target_us))
                        story_id_val = story_uuid
                    except ValueError:
                        stmt_us = select(UserStoryModel).join(RequirementModel).where(
                            UserStoryModel.ticket_code == str(target_us),
                            RequirementModel.project_id == pid
                        ).order_by(RequirementModel.version.desc())
                        res_us = await session.execute(stmt_us)
                        us = res_us.scalars().first()
                        if us:
                            story_id_val = us.id
                cq.target_user_story_id = story_id_val

            await session.flush()
            await session.refresh(cq)

            ret_story_id = await ClarificationQuestionRepository._resolve_story_code(session, cq.target_user_story_id)

            record = serialize_clarification_question(cq, pid, ret_story_id)
            # The write timestamp is reported by the caller (POST
            # /api/audit/respond returns ``resolved_at``). ``serialize_...`` is
            # shared with the read paths, so it is attached here instead of
            # leaking into every clarification-question payload.
            record["updated_at"] = dt_iso(cq.updated_at)
            return record
        return None

    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        cqid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(ClarificationQuestionModel).join(
            AuditResultModel, ClarificationQuestionModel.audit_result_id == AuditResultModel.id
        ).join(
            RequirementModel, AuditResultModel.requirement_id == RequirementModel.id
        ).where(ClarificationQuestionModel.id == cqid, RequirementModel.project_id == pid)
        result = await session.execute(stmt)
        cq = result.scalar_one_or_none()
        if cq:
            await session.delete(cq)
            await session.flush()
            return True
        return False

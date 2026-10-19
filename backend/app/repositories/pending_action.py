"""
Pending Action repository operations.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PendingActionModel
from app.application_types import PendingMutation
from app.repositories.base import as_uuid, serialize_pending_action

logger = logging.getLogger(__name__)


# CONFIRM 5.x — Pending actions = the staging table of the human-in-the-loop gate
# (pending_actions). Lifecycle: created by GATHERING 8.0 (MERGE) or the document
# extraction (INSERT_CHUNKED_REQUIREMENTS) → read by CONFIRM 3.1/3.2 → removed by
# CONFIRM 3.3.8 (applied) or 3.4 (discarded). `expires_at` is written but never
# queried — there is no expiry sweeper (analysis §9 item 2).
class PendingActionRepository:
    """Handles Pending Actions for Human-in-the-Loop confirmation."""

    # CONFIRM 5.1 — INSERT a staged action (called by GATHERING 8.0 and the document
    #             extraction). Defaults: action_type "UPDATE", workflow_stage
    #             "gatherer_node", status WAITING_CONFIRMATION (model default); the
    #             caller supplies expires_at (never enforced — see the class note).
    @staticmethod
    async def create(project_id: str, data: PendingMutation, expires_at: datetime, session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)
        action = PendingActionModel(
            project_id=pid,
            action_type=data.get("action_type", "UPDATE"),
            target_requirement_id=data.get("target_requirement_id"),
            original_user_message=data.get("original_user_message", ""),
            proposed_changes=data.get("proposed_changes", {}),
            affected_user_story_ids=data.get("affected_user_story_ids", []),
            affected_acceptance_criteria_ids=data.get("affected_acceptance_criteria_ids", []),
            workflow_stage=data.get("workflow_stage", "gatherer_node"),
            expires_at=expires_at
        )
        session.add(action)
        await session.flush()
        await session.refresh(action)
        return serialize_pending_action(action)

    # CONFIRM 5.2 — SELECT the project's staged actions, filtered to
    #             status == WAITING_CONFIRMATION and serialized with the FULL
    #             proposed_changes payload (that payload is the draft itself).
    @staticmethod
    async def get_by_project(project_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(PendingActionModel).where(PendingActionModel.project_id == pid, PendingActionModel.status == "WAITING_CONFIRMATION")
        result = await session.execute(stmt)
        actions = result.scalars().all()
        return [serialize_pending_action(a, full=True) for a in actions]

    @staticmethod
    async def discard_clarification_review_drafts(
        project_id: str,
        session: AsyncSession,
        *,
        keep_latest: bool,
    ) -> List[str]:
        """Discard redundant combined clarification-review drafts only.

        These drafts are regenerated whenever a stakeholder submits answers;
        retaining multiple copies creates duplicate confirmation windows and
        makes it unclear which combined proposal is authoritative. Normal
        Gatherer and document-extraction drafts are intentionally untouched.
        """
        pid = as_uuid(project_id)
        rows = (await session.execute(
            select(PendingActionModel).where(
                PendingActionModel.project_id == pid,
                PendingActionModel.status == "WAITING_CONFIRMATION",
                PendingActionModel.workflow_stage == "clarification_gatherer_review",
            ).order_by(PendingActionModel.created_at.desc())
        )).scalars().all()
        redundant = rows[1:] if keep_latest else rows
        removed = []
        for action in redundant:
            removed.append(str(action.id))
            await session.delete(action)
        if redundant:
            await session.flush()
        return removed

    # CONFIRM 5.3 — DELETE one action, scoped by BOTH action id and project id (a
    #             cross-project id can never be removed). Used by 3.3.8 (applied),
    #             3.4 (discarded) and 3.3.4 (draft with no changes).
    @staticmethod
    async def delete(id_val: str, project_id: str, session: AsyncSession) -> bool:
        aid = as_uuid(id_val)
        pid = as_uuid(project_id)
        stmt = select(PendingActionModel).where(PendingActionModel.id == aid, PendingActionModel.project_id == pid)
        result = await session.execute(stmt)
        action = result.scalar_one_or_none()
        if action:
            await session.delete(action)
            await session.flush()
            return True
        return False

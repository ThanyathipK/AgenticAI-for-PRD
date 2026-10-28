"""
Conversation message repository operations.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import ConversationMessageModel
from app.repositories.base import as_uuid, serialize_conversation_message

logger = logging.getLogger(__name__)


# CHAT 7.x — Shared conversation transcript persistence (conversation_messages).
# Call sites across flows:
#   7.1 save_message             ← CHAT 2.3.3 (pipeline assistant turn),
#                                  CHAT 5.3 / 5.5 (/api/chat turns), and every
#                                  agent node in the GATHERING/AUDITOR flows
#   7.2 get_conversation_history ← CHAT 2.3.1 (chat context), PROJECT 2.3.4
#                                  (state response), and agent prompt builders
# Session ownership: callers may pass the request-scoped session (participating in
# the caller's transaction) or omit it, in which case the repository opens its own
# AsyncSessionLocal with commit/rollback.
class ConversationMessageRepository:
    """
    Handles conversation message persistence in a dedicated table.
    Independent from requirement_states storage.
    """

    # CHAT 7.1 — Persist one idempotent conversation turn.
    # Trigger: CHAT 1.2, CHAT 2.3.3, CHAT 5.3/5.5, or an agent workflow.
    # Input: project, role, message, optional metadata/id, and optional session.
    # Writes: conversation_messages INSERT and flush; owns commit only when it
    #         creates its own session.
    # Branches: blank message → {}; existing same-project id → existing row.
    # Failure: cross-project id collision or database errors propagate.
    @staticmethod
    async def save_message(
        project_id: str,
        role: str,
        message: str,
        session: Optional[AsyncSession] = None,
        workflow_state: Optional[str] = None,
        intent: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not message or not message.strip():
            return {}

        pid = as_uuid(project_id)

        async def _save(s: AsyncSession):
            resolved_id = as_uuid(message_id) if message_id else uuid.uuid4()
            existing = await s.get(ConversationMessageModel, resolved_id)
            if existing is not None:
                if str(existing.project_id) != str(pid):
                    raise ValueError("Conversation message id belongs to another project")
                return serialize_conversation_message(existing)
            msg_obj = ConversationMessageModel(
                id=resolved_id,
                project_id=pid,
                role=role,
                message=message,
                workflow_state=workflow_state or "",
                intent=intent or "",
                # Database CURRENT_TIMESTAMP is commonly second-precision. Use
                # application UTC time so rapid user/error turns retain order.
                created_at=datetime.now(timezone.utc),
            )
            s.add(msg_obj)
            await s.flush()
            return serialize_conversation_message(msg_obj)

        if session:
            # Session is managed by the caller (e.g. get_db dependency) - just flush
            return await _save(session)
        async with AsyncSessionLocal() as db_session:
            try:
                result = await _save(db_session)
                await db_session.commit()
                return result
            except Exception:
                await db_session.rollback()
                raise

    # CHAT 7.2 — Read a project transcript in chronological order.
    # Trigger: CHAT 2.3.1, project hydration, or conversation APIs.
    # Input: project id plus optional limit/cursor/session.
    # Writes: NONE.
    # Ordering detail: SELECT the full transcript oldest-first (the ORDER BY created_at
    # asc is what keeps prompt context and the restored UI timeline consistent).
    # PERF (project switch): ``limit`` keeps only the NEWEST ``limit`` messages
    # (returned oldest-first, so ordering semantics are unchanged). The project-
    # state response uses it because it embeds the whole transcript on EVERY
    # project open — an unbounded chat easily dwarfs every other payload. Callers
    # that need the complete history (LLM context, the conversations endpoint)
    # omit ``limit`` and behave exactly as before.
    @staticmethod
    async def get_conversation_history(
        project_id: str,
        session: Optional[AsyncSession] = None,
        limit: Optional[int] = None,
        before_message_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        pid = as_uuid(project_id)

        async def _fetch(s: AsyncSession):
            if limit is not None and limit > 0:
                filters = [ConversationMessageModel.project_id == pid]
                if before_message_id:
                    cursor = await s.get(ConversationMessageModel, as_uuid(before_message_id))
                    if cursor is None or str(cursor.project_id) != str(pid):
                        return []
                    filters.append(or_(
                        ConversationMessageModel.created_at < cursor.created_at,
                        and_(
                            ConversationMessageModel.created_at == cursor.created_at,
                            ConversationMessageModel.id < cursor.id,
                        ),
                    ))
                # Newest N first, then reversed back to chronological order.
                stmt = (
                    select(ConversationMessageModel)
                    .where(*filters)
                    .order_by(
                        ConversationMessageModel.created_at.desc(),
                        ConversationMessageModel.id.desc(),
                    )
                    .limit(limit)
                )
                res = await s.execute(stmt)
                messages = list(res.scalars().all())[::-1]
            else:
                stmt = select(ConversationMessageModel).where(
                    ConversationMessageModel.project_id == pid
                ).order_by(
                    ConversationMessageModel.created_at.asc(),
                    ConversationMessageModel.id.asc(),
                )
                res = await s.execute(stmt)
                messages = res.scalars().all()
            return [serialize_conversation_message(m) for m in messages]

        if session:
            return await _fetch(session)
        async with AsyncSessionLocal() as db_session:
            return await _fetch(db_session)

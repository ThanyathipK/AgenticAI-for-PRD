"""
Project-level repository operations.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationMessageModel, ProjectModel, RequirementStateModel
from app.repositories.base import as_uuid, serialize_project
from app.repositories.event_log import ArtifactEventLogRepository

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _make_snippet(message: str, query: str, window: int = 60) -> str:
    # PROJECT 3.4.2 — Snippet builder for the search hit shown in the sidebar row:
    #                 windowed around the first case-insensitive match, ellipsized
    #                 at the trimmed edges, "" when there is nothing to show.
    """Build a compact excerpt of a conversation message centered on the first
    case-insensitive occurrence of ``query``.

    The snippet is bounded so the sidebar row stays short; an ellipsis marks the
    trimmed edges. Returns an empty string when there is nothing meaningful to
    show (no message text or no occurrence), in which case callers skip the
    ``match_snippet`` field entirely.
    """
    text = (message or "").strip()
    needle = query.strip().lower()
    if not text or not needle:
        return ""
    lower = text.lower()
    idx = lower.find(needle)
    if idx == -1:
        return text[:160].rstrip() + ("…" if len(text) > 160 else "")
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window * 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


# PROJECT repository map — the projects-table access point for the whole flow.
# Call sites (routes in app/routes/projects.py unless noted):
#   1.4.1  list_all       ← get_projects                 (SELECT, owner-scoped)
#   3.4.1  search         ← search_projects              (name OR messages ILIKE)
#   4.4.1  name_exists    ← create_project / update_project (global, not per-owner)
#   4.4.2  create_project ← create_project               (INSERT projects + state)
#   2.3.3.1 get_by_id     ← state init branch / PRD export / traceability
#   5.3.2  update         ← update_project               (lock-gated UPDATE)
#   6.3.1  delete         ← delete_project               (lock-gated DELETE + event log)
#   7.3.1  toggle_pinned  ← pin_project                  (UPDATE, no lock gate)
#   7.4.4  toggle_flagged ← flag_project                 (UPDATE, no lock gate)
#   7.5.4  update_status  ← set_project_status           (UPDATE, no lock gate)
class ProjectRepository:
    """Handles project-level operations."""

    # PROJECT 1.4.1 (helper) — Sidebar ordering: pinned first, then most recently
    # updated. Applied by list_all and search so both share the same order.
    @staticmethod
    def _sort_projects(projects: List[ProjectModel]) -> None:
        """Sidebar ordering: pinned projects float to the top, then most recently updated first."""
        projects.sort(key=lambda p: (not (p.is_pinned or False), -(p.updated_at or p.created_at).timestamp() if (p.updated_at or p.created_at) else 0))

    # PROJECT 1.4.1 — SELECT projects (WHERE user_id = :owner when scoped), sorted
    # and serialized. DB READ ONLY: no writes, no lock checks.
    @staticmethod
    async def list_all(
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> List[Dict[str, Any]]:
        """List projects, optionally scoped to a single owner.

        When ``user_id`` is given only that user's projects are returned — the
        per-user data boundary behind the sidebar (nothing before login, only
        the signed-in user's projects after). ``None`` lists everything and is
        reserved for internal/admin callers.
        """
        stmt = select(ProjectModel)
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        projects = list(result.scalars().all())
        ProjectRepository._sort_projects(projects)
        return [serialize_project(p) for p in projects]

    # PROJECT 3.4.1 — Search read: outer-joins conversation_messages and matches
    # projects.name OR message text (ILIKE, owner-scoped), then de-duplicates per
    # project and attaches one _make_snippet (PROJECT 3.4.2) per match. READ ONLY.
    @staticmethod
    async def search(
        session: AsyncSession,
        query: str,
        user_id: Optional[uuid.UUID] = None,
    ) -> List[Dict[str, Any]]:
        """Search projects by name OR by their persisted conversation messages.

        A project is returned when its name contains ``query`` (case-insensitive)
        or when at least one of its conversation messages contains ``query``
        (case-insensitive). Results use the same pinned-first ordering as
        :meth:`list_all`. When a project matched purely via message content, a
        ``match_snippet`` excerpt centered on ``query`` is attached so the UI can
        show *why* the project matched.

        Args:
            session: Active asynchronous database session.
            query: Non-empty search text (caller validates; stripped here).

        Returns:
            List[Dict[str, Any]]: Matching project summary records.
        """
        pattern = f"%{query.strip()}%"
        msg_col = ConversationMessageModel.message
        stmt = (
            select(ProjectModel, msg_col)
            .outerjoin(ConversationMessageModel, ConversationMessageModel.project_id == ProjectModel.id)
            .where(or_(
                ProjectModel.name.ilike(pattern),
                msg_col.ilike(pattern),
            ))
        )
        if user_id is not None:
            # Same per-owner boundary as list_all: a user only ever searches
            # their own projects (name or conversation content).
            stmt = stmt.where(ProjectModel.user_id == user_id)
        rows = (await session.execute(stmt)).all()

        # De-duplicate by project while capturing one snippet per project from
        # the first matching message (replaces the old ``.distinct()``).
        project_by_id: Dict[str, ProjectModel] = {}
        snippets: Dict[str, str] = {}
        for proj, message in rows:
            key = str(proj.id)
            if key not in project_by_id:
                project_by_id[key] = proj
            if message and key not in snippets:
                snippet = _make_snippet(message, query)
                if snippet:
                    snippets[key] = snippet

        projects = list(project_by_id.values())
        ProjectRepository._sort_projects(projects)
        return [
            serialize_project(p, match_snippet=snippets.get(str(p.id)))
            for p in projects
        ]

    # PROJECT 4.4.1 / 5.3.1 — Duplicate-name probe: COUNT over trim(lower(name)).
    # The count is NOT user-scoped (no owner filter is applied here or by the
    # callers), so names are unique across the whole workspace and a 409 can
    # disclose another tenant's project name (analysis §9 item 3; unchanged).
    @staticmethod
    async def name_exists(
        name: str,
        session: AsyncSession,
        exclude_project_id: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """Return ``True`` when a project already uses ``name``.

        The comparison is case-insensitive and whitespace-trimmed on BOTH
        sides, so ``"Payment Hub"``, ``"payment hub "`` and legacy rows saved
        without trimming are all treated as the same name. Routes call this
        before create/rename and translate ``True`` into HTTP 409 Conflict.

        Args:
            name: Candidate project name (stripped + lower-cased here).
            session: Active asynchronous database session.
            exclude_project_id: Optional project UUID string to ignore — used
                by rename so a project keeps its own name.

        Returns:
            bool: ``True`` when the normalized name is already taken.
        """
        normalized = (name or "").strip().lower()
        if not normalized:
            return False
        stmt = (
            select(func.count())
            .select_from(ProjectModel)
            .where(func.trim(func.lower(ProjectModel.name)) == normalized)
        )
        if exclude_project_id is not None:
            stmt = stmt.where(ProjectModel.id != as_uuid(exclude_project_id))
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        return bool((result.scalar_one() or 0) > 0)

    # PROJECT 4.4.2 — WRITE path of create: INSERT projects, then INSERT the empty
    # requirement_states row (bookkeeping only) in the same session/transaction.
    # Falls back to DEFAULT_SYSTEM_USER_ID when no owner is supplied — a legacy
    # path that would violate the projects.user_id FK if that seeded user is
    # absent (see analysis §9).
    @staticmethod
    async def create_project(project_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        user_id = project_data.get("user_id")
        if not user_id:
            # Resolve to standard seeded default system user
            user_id = DEFAULT_SYSTEM_USER_ID
        else:
            user_id = uuid.UUID(str(user_id))

        project = ProjectModel(
            id=uuid.UUID(str(project_data["id"])) if "id" in project_data else uuid.uuid4(),
            user_id=user_id,
            name=project_data["name"],
            description=project_data.get("description"),
            industry_standard=project_data.get("industry_standard", "Generic")
        )
        session.add(project)
        await session.flush()

        # Initialize requirement state
        req_state = RequirementStateModel(
            project_id=project.id,
        )
        session.add(req_state)

        await session.flush()
        await session.refresh(project)
        return {"id": str(project.id), "name": project.name}

    # PROJECT 2.3.3.1 / export / traceability — plain owner-agnostic READ of one
    # project by id (callers that need the owner boundary use `update`/`delete`,
    # which apply the user_id filter, or the AUTH 7.4 gate upstream).
    @staticmethod
    async def get_by_id(project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if p:
            return serialize_project(p)
        return None

    # PROJECT 5.3.2 — WRITE path of rename/field update: owner-scoped SELECT (a
    # non-owner simply gets None → route 404), then the artifact-lock gate
    # (LockService.raise_if_locked_model) before any assignment, then UPDATE of the
    # supplied fields + flush/refresh.  The project name is stored exclusively
    # in projects, so a rename has no denormalized copies to synchronize.
    @staticmethod
    async def update(
        project_id: str,
        updates: Dict[str, Any],
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[Dict[str, Any]]:
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        if user_id is not None:
            # Owner boundary: a non-owner sees the same None a missing project
            # produces, so the route answers 404 either way (no existence leak).
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if p:
            if "name" in updates:
                p.name = updates["name"]
            if "description" in updates:
                p.description = updates["description"]
            if "industry_standard" in updates:
                p.industry_standard = updates["industry_standard"]
            if "is_pinned" in updates:
                p.is_pinned = bool(updates["is_pinned"])
            if "is_flagged" in updates:
                p.is_flagged = bool(updates["is_flagged"])
            if "status" in updates:
                p.status = str(updates["status"])
            await session.flush()
            await session.refresh(p)
            return serialize_project(p)
        return None

    # PROJECT 7.5.4 — UPDATE of projects.status only. Deliberately NOT lock-gated:
    # locking protects document content, not review bookkeeping (same rationale as
    # toggle_pinned / toggle_flagged).
    @staticmethod
    async def update_status(
        project_id: str,
        new_status: str,
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[Dict[str, Any]]:
        """Set a project's user-editable workflow status (dashboard table).

        Mirrors :meth:`toggle_pinned`: a UI metadata update, so it is NOT gated
        by the artifact lock (locking protects document content, not review
        bookkeeping).

        Args:
            project_id: Project UUID string.
            new_status: Workflow status to apply ('draft' | 'in_review_hpo' |
                'in_review_po' | 'approved' | 'revised').
            session: Active asynchronous database session.
            user_id: When given, only a project owned by this user qualifies.

        Returns:
            Optional[Dict[str, Any]]: The updated project record, or None if the
                project does not exist (or is not owned by ``user_id``).
        """
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if not p:
            return None
        p.status = str(new_status)
        await session.flush()
        await session.refresh(p)
        return serialize_project(p)

    # PROJECT 7.3.1 — UPDATE of projects.is_pinned only (owner-scoped, no lock
    # gate). Returns None when the project is missing or owned by someone else.
    @staticmethod
    async def toggle_pinned(
        project_id: str,
        is_pinned: bool,
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pin or unpin a project (chat) so it floats to the top of the sidebar.

        Args:
            project_id: Project UUID string.
            is_pinned: New pinned state to apply.
            session: Active asynchronous database session.
            user_id: When given, only a project owned by this user qualifies.

        Returns:
            Optional[Dict[str, Any]]: The updated project record, or None if the
                project does not exist (or is not owned by ``user_id``).
        """
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if not p:
            return None
        p.is_pinned = bool(is_pinned)
        await session.flush()
        await session.refresh(p)
        return serialize_project(p)

    # PROJECT 7.4.4 — UPDATE of projects.is_flagged only (owner-scoped, no lock
    # gate). Never touches is_pinned, so sidebar ordering is unaffected.
    @staticmethod
    async def toggle_flagged(
        project_id: str,
        is_flagged: bool,
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[Dict[str, Any]]:
        """Flag or unflag a project (dashboard ★ marker).

        Deliberately independent of :meth:`toggle_pinned`: flagging marks a
        project for attention in the projects overview table and NEVER affects
        the sidebar's pinned-first ordering.

        Args:
            project_id: Project UUID string.
            is_flagged: New flagged state to apply.
            session: Active asynchronous database session.
            user_id: When given, only a project owned by this user qualifies.

        Returns:
            Optional[Dict[str, Any]]: The updated project record, or None if the
                project does not exist (or is not owned by ``user_id``).
        """
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if not p:
            return None
        p.is_flagged = bool(is_flagged)
        await session.flush()
        await session.refresh(p)
        return serialize_project(p)

    # PROJECT 6.3.1 — DELETE path: owner-scoped SELECT → artifact-lock gate →
    # session.delete (FK CASCADE removes projects' children) → append an
    # artifact_event_logs row. The event log is fail-open: a logging failure is
    # warned about and the deletion still succeeds.
    @staticmethod
    async def delete(
        project_id: str,
        session: AsyncSession,
        user_id: Optional[uuid.UUID] = None,
    ) -> bool:
        pid = as_uuid(project_id)
        stmt = select(ProjectModel).where(ProjectModel.id == pid)
        if user_id is not None:
            stmt = stmt.where(ProjectModel.user_id == user_id)
        result = await session.execute(stmt)
        p = result.scalar_one_or_none()
        if p:
            # Log while the parent row still exists. The previous order deleted
            # and flushed the project first, then inserted an event whose
            # project_id FK referenced that deleted row. PostgreSQL rejected
            # the insert and poisoned the outer transaction, so get_db's commit
            # rolled the deletion back even though the endpoint had returned
            # success. A savepoint makes audit logging genuinely fail-open.
            try:
                async with session.begin_nested():
                    await ArtifactEventLogRepository.log_event(
                        artifact_type="project",
                        artifact_id=str(project_id),
                        action="DELETE",
                        session=session,
                        old_value={"name": p.name, "description": p.description},
                        new_value=None,
                        performed_by="automated_agent",
                        project_id=str(project_id),
                        user_id=str(p.user_id),
                    )
            except Exception as log_err:
                logger.warning(f"[EVENT LOG] Failed to log project delete: {log_err}")
            await session.delete(p)
            await session.flush()
            return True
        return False

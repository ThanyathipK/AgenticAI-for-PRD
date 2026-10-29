import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user, require_project_owner
from app.database import get_db
from app.event_manager import event_manager
from app.latex_service import (
    compile_latex_to_pdf,
    convert_latex_to_docx,
    convert_markdown_to_docx,
    convert_markdown_to_pdf,
    docx_to_pdf,
    has_markdown_structure,
    is_markdown_prd,
    prd_to_markdown,
    soffice_available,
)
from app.prd_section_service import assemble_document_markdown, ensure_sections_seeded
from app.prompt_loader import load_prd_latex_template, load_prd_template
from app.repositories import (
    ConversationMessageRepository,
    PRDDocumentRepository,
    PRDVersionRepository,
    ProjectRepository,
    RequirementStateRepository,
)
from app.repositories.base import serialize_project
from app.repositories.prd_section import PRDSectionRepository
from app.review_service import (
    ReviewTransitionError,
    get_review_history,
    governed_state_changed,
    mark_revised_if_approved,
    transition_project,
)
from app.schemas import (
    ConversationMessageCreate,
    ConversationMessageResponse,
    PRDExportResponse,
    PrdVersionDiffResponse,
    PRDVersionResponse,
    PrdVersionRestoreResponse,
    ProjectCreate,
    ProjectCreated,
    ProjectDeleteResponse,
    ProjectFlagRequest,
    ProjectPinRequest,
    ProjectReviewEventResponse,
    ProjectStatusRequest,
    ProjectSummary,
    RequirementStateResponse,
)

logger = logging.getLogger("app.routes.projects")

router = APIRouter()

# Keep ordinary project refreshes bounded. The dedicated conversations endpoint
# remains the durable, complete audit trail and can support paginated history UI.
PROJECT_STATE_HISTORY_LIMIT = 5


def _duplicate_project_name_detail(name: str) -> str:
    """User-facing detail message for a duplicate project-name conflict (409)."""
    return f"A project named '{name}' already exists. Please choose a different name."


# PROJECT 1.4 — Route: GET /api/projects (the sidebar list).
#              Auth: Depends(get_current_user) → AUTH 7.1 (Bearer → users row);
#              the query itself is owner-scoped (1.4.1), so an anonymous caller is
#              rejected before any DB work and a signed-in caller only ever sees
#              their own projects — the same boundary the frontend applies in
#              PROJECT 1.2.
#              Response: List[ProjectSummary], pinned-first (PROJECT 1.5).
@router.get("/api/projects", response_model=List[ProjectSummary], status_code=status.HTTP_200_OK)
async def get_projects(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProjectSummary]:
    """
    List the signed-in user's projects.

    Per-user data boundary: only projects whose ``user_id`` matches the JWT's
    subject are returned. An anonymous caller is rejected with 401 by
    ``get_current_user`` before any query runs, so nothing is exposed before
    login.

    Args:
        current_user: Authenticated caller resolved from the Bearer token.
        db: Active asynchronous database session.

    Returns:
        List[ProjectSummary]: That user's project summary records (pinned first).
    """
    # PROJECT 1.4.1 — Repository read: SELECT projects (owner-scoped when user_id
    #                is given) + pinned-first sort + serialization. DB: projects.
    return await ProjectRepository.list_all(db, user_id=UUID(current_user.id))


# PROJECT 3.4 — Route: GET /api/projects/search (the debounced sidebar search).
#              Auth: AUTH 7.1 + owner scoping inside the repository (PROJECT 3.4.1).
#              Validation: `q` is required (min_length=1) and re-checked after
#              trimming → 400 "Search query must not be empty".
#              Response: matching ProjectSummary rows; a row matched through
#              conversation content carries `match_snippet`.
@router.get("/api/projects/search", response_model=List[ProjectSummary], status_code=status.HTTP_200_OK)
async def search_projects(
    q: str = Query(..., min_length=1, description="Text matched case-insensitively against project names and conversation message content."),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProjectSummary]:
    """
    Search the signed-in user's projects by name or conversation content.

    Same per-user boundary as GET /api/projects: results can only ever include
    projects owned by the authenticated caller.

    Args:
        q: Search text; matched case-insensitively against project names and
           any persisted conversation message belonging to the project.
        current_user: Authenticated caller resolved from the Bearer token.
        db: Active asynchronous database session.

    Returns:
        List[ProjectSummary]: Matching project summary records (pinned first).

    Raises:
        HTTPException: 400 if the query is empty after trimming.
    """
    # PROJECT 3.4.0 — Validation branch: an all-whitespace query is rejected even
    #                though the schema already enforces min_length=1.
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query must not be empty")
    # PROJECT 3.4.1 — Repository search: projects.name ILIKE OR the project's
    #                conversation_messages.message ILIKE, owner-scoped, de-duplicated
    #                per project with a bounded `match_snippet` excerpt.
    return await ProjectRepository.search(db, query, user_id=UUID(current_user.id))


# PROJECT 4.4 — Route: POST /api/projects (create).
#              Auth: AUTH 7.1 — the JWT subject becomes projects.user_id, which
#              is exactly what authorizes PROJECT 2.x / 5.x / 6.x / 8.x later.
#              Branches: 4.4.1 duplicate-name 409 (the check is deliberately NOT
#              user-scoped, so names are workspace-global and a 409 can reveal
#              another tenant's project name — documented in the analysis, not
#              changed here) → 4.4.2 INSERT projects + requirement_states →
#              4.4.3 SSE publish "project_created".
#              Response: 201 ProjectCreated; the UI re-reads the list (PROJECT 4.5).
@router.post("/api/projects", response_model=ProjectCreated, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectCreated:
    """
    Create a new project owned by the signed-in user and initialize its
    requirement state.

    Args:
        payload: Project creation payload.
        current_user: Authenticated caller resolved from the Bearer token —
            becomes the project's owner (``projects.user_id``).
        db: Active asynchronous database session.

    Returns:
        ProjectCreated: The new project's UUID string and name.

    Raises:
        HTTPException: 409 if a project with the same name already exists
            (names are compared case-insensitively, ignoring surrounding
            whitespace).
    """
    # DUPLICATE-NAME VALIDATION: project names must be unique across the
    # workspace. ``payload.name`` is already stripped by the ProjectCreate
    # schema validator; a conflict surfaces as 409 so the UI can render an
    # inline "name already exists" error.
    # PROJECT 4.4.1 — Duplicate-name branch (409). NOTE: this check is not
    #                user-scoped, so names are globally unique and the 409 can
    #                disclose another tenant's project name (analysis §9 item 3).
    if await ProjectRepository.name_exists(payload.name, db, user_id=UUID(current_user.id)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_project_name_detail(payload.name),
        )
    project_data = payload.model_dump()
    project_data["id"] = str(uuid.uuid4())
    # OWNERSHIP: the JWT subject is the owner. New projects are visible only to
    # their creator (GET /api/projects scopes to this same id).
    project_data["user_id"] = current_user.id
    # PROJECT 4.4.2 — Repository write: INSERT projects + INSERT requirement_states
    #                (empty board; only the project name is seeded).
    created_project = await ProjectRepository.create_project(project_data, db)
    # PROJECT 4.4.3 — Real-time fan-out: other tabs viewports refresh their sidebar
    #                (EVENTS flow consumes this frame).
    await event_manager.publish(str(project_data["id"]), "project_created", {"project_id": project_data["id"]})
    return created_project


# PROJECT 5.3 — Route: PUT /api/projects/{project_id} (rename / field update).
#              Auth: AUTH 7.1 + an owner filter inside the repository (5.3.2), so a
#              non-owner receives the same 404 as a missing project (no leak).
#              Branches: UUID format 400 → 5.3.1 duplicate-name 409 (excluding the
#              project itself) → 5.3.2 UPDATE (artifact lock gate inside) →
#              404 when unowned/missing → 5.3.4 SSE publish "project_updated".
#              Response: ProjectSummary; the hook applies it optimistically (5.4).
@router.put("/api/projects/{project_id}", response_model=ProjectSummary, status_code=status.HTTP_200_OK)
async def update_project(
    project_id: str,
    payload: ProjectCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectSummary:
    """
    Update an existing project (rename).

    Args:
        project_id: Project UUID string.
        payload: Updated project fields.
        db: Active asynchronous database session.

    Returns:
        ProjectSummary: The updated project record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if not
            found; 409 if another project already uses the new name
            (case-insensitive, whitespace-trimmed; renaming to the project's
            own name is allowed).
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    updates = payload.model_dump()
    # DUPLICATE-NAME VALIDATION: the new name must not collide with any OTHER
    # project (the project may keep its own name). Same 409 contract as create.
    # PROJECT 5.3.1 — Duplicate-name branch (409) excluding this project, so a
    #                project may keep or re-adopt its own name.
    if await ProjectRepository.name_exists(
        updates["name"], db, exclude_project_id=project_id,
        user_id=UUID(current_user.id),
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_project_name_detail(updates["name"]),
        )
    # PROJECT 5.3.2 — Repository UPDATE (owner-scoped SELECT → artifact-lock check →
    #                field assignment → flush/refresh). DB: projects.
    updated = await ProjectRepository.update(project_id, updates, db, user_id=UUID(current_user.id))
    if not updated:
        # Same answer for "missing" and "owned by someone else" — no existence leak.
        raise HTTPException(status_code=404, detail="Project not found")
    # PROJECT 5.3.4 — Real-time fan-out with the new name.
    await event_manager.publish(project_id, "project_updated", {
        "project_id": project_id,
        "name": updates.get("name")
    })
    return updated


# PROJECT 6.3 — Route: DELETE /api/projects/{project_id}.
#              Auth: AUTH 7.1 + owner filter in the repository (6.3.1).
#              Branches: UUID format 400 → 6.3.1 DELETE (lock-gated; also appends
#              an artifact_event_logs row) → 404 when unowned/missing → 6.3.3 SSE
#              publish "project_deleted". FK CASCADE removes every child artifact.
#              Response: {status:"deleted", project_id}; the hook re-selects the
#              first remaining project (PROJECT 6.4).
@router.delete("/api/projects/{project_id}", response_model=ProjectDeleteResponse, status_code=status.HTTP_200_OK)
async def delete_project(
    project_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectDeleteResponse:
    """
    Delete an existing project.

    Args:
        project_id: Project UUID string.
        db: Active asynchronous database session.

    Returns:
        ProjectDeleteResponse: Confirmation containing the deleted project ID.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if not found.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # PROJECT 6.3.1 — Repository DELETE (owner-scoped SELECT → artifact-lock check →
    #                session.delete → artifact_event_logs append, fail-open).
    #                DB: projects (FK CASCADE clears all child artifacts).
    deleted = await ProjectRepository.delete(project_id, db, user_id=UUID(current_user.id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    # PROJECT 6.3.3 — Real-time fan-out. Note the stream is keyed by the deleted
    #                project id, so only clients still subscribed to it see this.
    await event_manager.publish(project_id, "project_deleted", {"project_id": project_id})
    return {"status": "deleted", "project_id": project_id}


# PROJECT 7.3 — Route: PUT /api/projects/{project_id}/pin (sidebar pin/unpin).
#              Auth: AUTH 7.1 + owner filter inside 7.3.1.
#              Metadata only — deliberately NOT lock-gated (locking protects
#              document content, not review bookkeeping).
#              Branches: UUID format 400 → 7.3.1 UPDATE is_pinned → 404 when
#              unowned/missing → 7.3.2 SSE publish "project_updated".
@router.put("/api/projects/{project_id}/pin", response_model=ProjectSummary, status_code=status.HTTP_200_OK)
async def pin_project(
    project_id: str,
    payload: ProjectPinRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectSummary:
    """Pin or unpin a project (chat) so it floats to the top of the sidebar.

    Args:
        project_id: Project UUID string.
        payload: Desired pinned state.
        db: Active asynchronous database session.

    Returns:
        ProjectSummary: The updated project record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if not found.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # PROJECT 7.3.1 — Repository UPDATE of is_pinned only (owner-scoped). No lock
    #                gate by design: pinning is view metadata, not content.
    updated = await ProjectRepository.toggle_pinned(project_id, payload.is_pinned, db, user_id=UUID(current_user.id))
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    await event_manager.publish(project_id, "project_updated", {
        "project_id": project_id,
        "is_pinned": payload.is_pinned,
    })
    return updated


# PROJECT 7.4.3 — Route: PUT /api/projects/{project_id}/flag (dashboard ★).
#                Auth: AUTH 7.1 + owner filter in 7.4.4.
#                Independent of pinning by design (never affects sidebar order).
#                Branches: UUID format 400 → 7.4.4 UPDATE is_flagged → 404 when
#                unowned/missing → 7.4.5 SSE publish "project_updated".
@router.put("/api/projects/{project_id}/flag", response_model=ProjectSummary, status_code=status.HTTP_200_OK)
async def flag_project(
    project_id: str,
    payload: ProjectFlagRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectSummary:
    """Flag or unflag a project (dashboard ★ marker).

    Independent of pinning: flagging only marks the project for attention in
    the projects overview table and never affects the sidebar ordering.

    Args:
        project_id: Project UUID string.
        payload: Desired flagged state.
        db: Active asynchronous database session.

    Returns:
        ProjectSummary: The updated project record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if not found.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # PROJECT 7.4.4 — Repository UPDATE of is_flagged only (owner-scoped), by the
    #                same no-lock rationale as pinning.
    updated = await ProjectRepository.toggle_flagged(project_id, payload.is_flagged, db, user_id=UUID(current_user.id))
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    await event_manager.publish(project_id, "project_updated", {
        "project_id": project_id,
        "is_flagged": payload.is_flagged,
    })
    return updated


# PROJECT 7.5.3 — Route: PUT /api/projects/{project_id}/status (workflow status).
#                Auth: AUTH 7.1 + owner filter in 7.5.4.
#                Branches: UUID format 400 → 7.5.3.1 allowed-status validation 400
#                ({draft, in_review_hpo, in_review_po, approved, revised}) →
#                7.5.4 UPDATE status → 404 when unowned/missing → 7.5.5 SSE publish.
#                Also metadata-only: no artifact lock, no PRD version entry.
@router.put("/api/projects/{project_id}/status", response_model=ProjectSummary, status_code=status.HTTP_200_OK)
async def set_project_status(
    project_id: str,
    payload: ProjectStatusRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectSummary:
    """Set a project's user-editable workflow status (dashboard table).

    Args:
        project_id: Project UUID string.
        payload: Desired workflow status.
        db: Active asynchronous database session.

    Returns:
        ProjectSummary: The updated project record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID or the status
            value is not one of the allowed workflow statuses; 404 if not found.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # PROJECT 7.5.3.1 — Validation branch: only the canonical workflow statuses are
    #                accepted → 400 with the allowed set. (Free-form values may
    #                still reach the column via the generic PUT /api/projects/{id}.)
    allowed = {"draft", "in_review_hpo", "in_review_po", "approved", "revised"}
    if payload.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{payload.status}'. Allowed: {', '.join(sorted(allowed))}",
        )

    # Use the governed transition path so every dashboard status change creates
    # an append-only review event and approval records capture their PRD/audit
    # evidence instead of leaving the project approval columns permanently NULL.
    try:
        project = await transition_project(
            project_id,
            payload.status,
            db,
            actor_id=current_user.id,
            actor_name=current_user.full_name,
            actor_role=current_user.role,
            comment=payload.comment,
            manual=True,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except ReviewTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    updated = serialize_project(project)
    await event_manager.publish(project_id, "project_updated", {
        "project_id": project_id,
        "status": payload.status,
    })
    return updated


@router.get(
    "/api/projects/{project_id}/review-history",
    response_model=List[ProjectReviewEventResponse],
    status_code=status.HTTP_200_OK,
)
async def project_review_history(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> List[ProjectReviewEventResponse]:
    """Return the append-only lifecycle history for the caller's project."""
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format") from None
    return await get_review_history(project_id, db)


# PROJECT 2.3 — Route: GET /api/project/{project_id} (opening a project).
#              Auth: Depends(require_project_owner) → AUTH 7.5 → AUTH 7.4
#              (404 malformed/missing, 403 not-owner) — stricter than the
#              /api/projects* group above, which scopes by query instead.
#              Branches: 2.3.1 UUID format 400 → 2.3.2 repository read → 2.3.3
#              first-open initialization (bookkeeping fields only, so the board
#              stays empty and requirement numbering starts at REQ-001) → 2.3.4
#              attach the conversation history.
#              Response: RequirementStateResponse → PROJECT 2.4 (store mapping).
@router.get("/api/project/{project_id}", response_model=RequirementStateResponse, status_code=status.HTTP_200_OK)
async def get_project_requirement_state(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> RequirementStateResponse:
    """
    Retrieves the centralized RequirementState for a project from Supabase.

    If it doesn't exist, returns default initialized values. Includes the
    persisted conversation history for the project.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        RequirementStateResponse: The flattened requirement state.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # PROJECT 2.3.2 — Repository read (see the get_by_project_id branch comment):
    #                SELECT requirement_states by project_id, then rebuild the
    #                flattened board from epics / requirements / user_stories /
    #                acceptance_criteria / audit_results / clarification_questions.
    logger.info(f"[DB LOG] Loading RequirementState for project {project_id}")
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    logger.info(f"[DB LOG] Loading RequirementState for project {project_id} complete. Found: {state is not None}")
    if not state:
        # A brand-new project starts with an EMPTY requirement board.
        # This endpoint used to seed a HARDCODED demo requirement (``REQ-001``
        # "PromptPay Real-Time Merchant Settlement Engine" plus a demo ``US-001``)
        # and persist it. The phantom requirement owned ``REQ-001``, so the first
        # requirement the user actually gathered was numbered ``REQ-002`` and the
        # demo content polluted the board, the PRD and the traceability matrix.
        # The authoritative project name lives in the projects table, so it is
        # read from there instead of a fabricated demo label.
        # PROJECT 2.3.3.1 — Repository read of the authoritative project name
        #                  (projects row) instead of a fabricated demo label.
        logger.info(f"[DB LOG] Initializing empty state for project {project_id}...")
        # Persist BOOKKEEPING fields only: passing the empty
        # ``requirements``/``user_stories`` lists are unnecessary for this
        # bookkeeping write. Requirement rows are created by the first real
        # merge/gather save, so the requirement sequence starts at ``REQ-001``.
        # PROJECT 2.3.3.2 — Repository write: bookkeeping fields ONLY (no
        #                  requirements/user_stories lists), which is what keeps the
        #                  board empty so the first real capture starts at REQ-001.
        state = await RequirementStateRepository.save_or_update(project_id, {
            "validation_status": "pending",
            "current_workflow_state": "gatherer_node",
            "version_number": 1,
        }, session)
        logger.info(f"[DB LOG] Initializing empty state for project {project_id} complete.")
        # PROJECT 2.3.3.3 — Real-time fan-out for the first-open initialization.
        await event_manager.publish(project_id, "state_initialized", {"project_id": project_id})

    # A new project's intake is stored immediately in the canonical PRD
    # sections, before the first compiled PRD/version exists. Return that saved
    # draft as the initial document instead of making the client fall back to a
    # blank static template. This keeps Problem Statement and the other intake
    # fields visible from the first project load.
    if isinstance(state, dict) and not str(state.get("generated_prd") or "").strip():
        section_draft = await assemble_document_markdown(project_id, session)
        if section_draft.strip():
            state = dict(state)
            state["generated_prd"] = section_draft

    # Reuse the request's AsyncSession instead of opening a second pooled
    # connection just for the conversation history read (one fewer network
    # round-trip to the database on every project-state load).
    # PROJECT 2.3.4 — Repository read of the persisted conversation history, using
    #                the SAME request session (one fewer pooled connection); the
    #                result is attached to the response below so the UI restores
    #                the chat transcript in PROJECT 2.4.
    # PERF (project switch) — only the NEWEST N messages are embedded: this
    # response is fetched on every project click and an unbounded transcript is
    # the single largest field after the PRD itself. The timeline still renders
    # chronologically; older turns are simply not restored (the dedicated
    # /conversations endpoint still returns the complete transcript).
    conv_history = await ConversationMessageRepository.get_conversation_history(
        project_id, session, limit=PROJECT_STATE_HISTORY_LIMIT
    )
    if isinstance(state, dict):
        state = dict(state)
        state["conversation_history"] = conv_history
    return state


# PROJECT 9.1 — Route: GET /api/project/{project_id}/conversations.
#              Auth: AUTH 7.5 ownership gate (no explicit session dependency —
#              the repository opens its own read via the shared session factory).
#              DISCREPANCY vs. the expected flow: no frontend caller exists
#              (src/api/client.ts exposes no method for it) because the UI reads
#              history through PROJECT 2.3.4, which embeds `conversation_history`
#              in the state response. Documented as-is, not removed.
@router.get("/api/project/{project_id}/conversations", response_model=List[ConversationMessageResponse], status_code=status.HTTP_200_OK)
async def get_project_conversations(
    project_id: str,
    limit: Optional[int] = Query(None, ge=1, le=50),
    before: Optional[str] = Query(None),
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> List[ConversationMessageResponse]:
    """
    Retrieve the persisted conversation history for a project.

    Args:
        project_id: Project UUID string.

    Returns:
        List[ConversationMessageResponse]: All conversation messages, oldest first.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID.
    """
    try:
        UUID(project_id)
        if before:
            UUID(before)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    return await ConversationMessageRepository.get_conversation_history(
        project_id,
        session=session,
        limit=limit,
        before_message_id=before,
    )


@router.post(
    "/api/project/{project_id}/conversations",
    response_model=ConversationMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def append_project_conversation(
    project_id: str,
    payload: ConversationMessageCreate,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> ConversationMessageResponse:
    """Persist one UI-visible timeline bubble, idempotently when an id is supplied."""
    try:
        UUID(project_id)
        if payload.id:
            UUID(payload.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project or conversation message id")
    try:
        return await ConversationMessageRepository.save_message(
            project_id=project_id,
            role=payload.role,
            message=payload.message,
            workflow_state=payload.workflow_state,
            intent=payload.intent,
            message_id=payload.id,
            session=session,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# PROJECT 8.2 — Route: PUT /api/project/{project_id} (DIRECT requirement-state write).
#              Auth: AUTH 7.5 ownership gate.
#              Branches: 8.2.1 UUID format 400 → project artifact lock check
#              (409 locked / 404 unresolvable, via LockService) → 8.2.2
#              RequirementStateRepository.save_or_update → 8.2.3 optional PRD
#              version entry when `generated_prd` is present → 8.2.4 SSE publish
#              "state_updated".
#              This is the manual/legacy fallback; the AI write path is the
#              CONFIRMATION flow (confirm-action).
@router.put("/api/project/{project_id}", response_model=RequirementStateResponse, status_code=status.HTTP_200_OK)
async def update_project_requirement_state(
    project_id: str,
    updates: Dict[str, Any],
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> RequirementStateResponse:
    """
    Directly updates the centralized RequirementState for a project in the database.

    Useful for saving manual PRD edits and synchronizing sections. Refuses to
    mutate a locked project.

    Args:
        project_id: Project UUID string.
        updates: Partial requirement-state payload to apply.
        session: Active asynchronous database session.

    Returns:
        RequirementStateResponse: The updated, flattened requirement state.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    logger.info(f"[DB LOG] Updating RequirementState directly for project {project_id}")
    # PROJECT 8.2.2 — Repository write: RequirementStateRepository.save_or_update
    #                (create-if-missing, else partial field update; child tables
    #                re-derived when requirements/stories/AC lists are supplied).
    previous_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    state = await RequirementStateRepository.save_or_update(project_id, updates, session)
    from app.dependency_graph_service import detect_changed_artifacts, mark_dependents_for_review

    changed_artifacts = detect_changed_artifacts(previous_state, state)
    if changed_artifacts:
        state = await RequirementStateRepository.save_or_update(
            project_id,
            {"validation_status": "pending"},
            session,
        )
        await mark_dependents_for_review(project_id, changed_artifacts, session)
    if governed_state_changed(previous_state, updates):
        await mark_revised_if_approved(
            project_id,
            session,
            reason="Approved requirements changed through a direct edit.",
            actor_id=current_user.id,
            actor_name=current_user.full_name,
            actor_role=current_user.role,
        )

    # If the update includes a new generated_prd (e.g. legacy manual section edit
    # fallback), record an immutable PRD version so the version ledger never
    # collapses — even when the primary section endpoint is unavailable.
    # PROJECT 8.2.3 — Version ledger: a manual/legacy state write that carries a
    #                new generated_prd appends an immutable prd_versions row
    #                (VERSIONING flow) so the ledger never collapses. Fail-open:
    #                a ledger failure is logged and does not undo the state write.
    if "generated_prd" in updates and updates["generated_prd"]:
        try:
            from app.version_service import record_prd_version
            await record_prd_version(
                project_id, session,
                generated_prd=updates["generated_prd"],
                generated_by="user",
                change_type="manual",
                change_summary=updates.get("change_summary"),
            )
        except Exception as ver_err:
            logger.warning("[DB LOG] Failed to record PRD version for legacy update: %s", ver_err)

    # PROJECT 8.2.4 — Real-time fan-out; the frontend's SSE handler answers with a
    #                debounced full state re-read (PROJECT 2.2).
    await event_manager.publish(project_id, "state_updated", {"project_id": project_id})
    return state


@router.get("/api/prd/template", status_code=status.HTTP_200_OK)
def get_prd_template(current_user: AuthenticatedUser = Depends(get_current_user)) -> Dict[str, str]:
    """
    Serves the authoritative Krungsri Nimble PRD templates.

    - "template_latex": prompts/template-krungsrinimble.tex - the PDF-exact
      LaTeX template injected as the <prd_template> block into every PRD
      generation prompt. The running header/footer/page numbers are applied by
      fancyhdr at compile time rather than stored as lines.
    - "template_markdown": prompts/template.md - the markdown skeleton kept for
      the ongoing on-screen preview, so no frontend rendering breaks.

    Both expose the SAME document structure that the Architect fills in.
    """
    from app.prd_filler import strip_template_only_details

    return {
        "template_latex": strip_template_only_details(load_prd_latex_template()),
        "template_markdown": strip_template_only_details(load_prd_template()),
    }


# PRD-EXPORT 4.0 — POST snapshot-producing export. Deterministically fills and stamps a
#               document, then writes directly through PRD-EXPORT 7.2 / VERSION 7.1
#               (not VERSION 6.6): it stores no VERSION 6.1 changed_sections and does not
#               synchronize the state
#               counter. SemVer is predicted by 3.9; prediction failure stamps the
#               legacy label while 4.1 defaults the row semver to 1.0.0. Insert failure
#               is fatal (500), so this endpoint never reports an unpersisted version.
@router.post("/api/prd/export/{project_id}", response_model=PRDExportResponse, status_code=status.HTTP_200_OK)
async def post_prd_export_generation(
    project_id: UUID,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> PRDExportResponse:
    """
    Gathers linked user stories and acceptance criteria from the database,
    dispatches them to the local LLM, and returns a clean, finalized
    Production PRD document structured in markdown.

    A new immutable PRD version record is persisted on success.

    Args:
        project_id: Project UUID.
        db: Active asynchronous database session.

    Returns:
        PRDExportResponse: The generated PRD markdown, diagram, version and
            version record ID.

    Raises:
        HTTPException: 404 if the project or its requirement state is missing.
    """
    logger.info(f"Triggering automated compliance PRD synthesis for project {project_id}.")

    # Validate project existence
    project = await ProjectRepository.get_by_id(str(project_id), db)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Fetch the real requirement state: requirements, user stories, and acceptance criteria
    req_state = await RequirementStateRepository.get_by_project_id(str(project_id), db)
    if not req_state:
        raise HTTPException(
            status_code=404,
            detail=f"No requirement state found for project {project_id}. Gather requirements before exporting a PRD."
        )

    requirements = req_state.get("requirements", []) or []
    user_stories = req_state.get("user_stories", []) or []
    acceptance_criteria = req_state.get("acceptance_criteria", []) or []
    business_goals = req_state.get("business_goals", []) or []
    actors = req_state.get("actors", []) or []
    version_number = req_state.get("version_number", 1)

    if not user_stories and not requirements:
        raise HTTPException(
            status_code=404,
            detail=f"No user stories or requirements found for project {project_id}. Gather requirements before exporting a PRD."
        )

    # DETERMINISTIC TEMPLATE FILL: build the PRD by filling the official
    # PRD-EXPORT 5.0 — Assemble the Krungsri Nimble template from persisted project
    # artifacts, resolve/stamp the predicted SemVer, and require ledger insertion.
    # The
    # skeleton - every table, merged-cell structure and \newpage marker - is
    # always the untouched official template, so the export is PDF-exact and
    # compiles every time.
    from app.prd_filler import (
        PENDING_VERSION_LABEL,
        extract_intake_fields,
        fill_template_body,
        format_project_status,
        stamp_version_label,
    )

    nested = any(
        isinstance(r, dict) and r.get("user_stories") for r in (requirements or [])
    )
    all_stories = (
        [us for r in requirements for us in (r.get("user_stories") or [])]
        if nested else user_stories
    )
    data = {
        "epic_name": (
            req_state.get("epic_name")
            or (requirements[0].get("title", "") if requirements else "")
            or str(project.get("name", ""))
        ),
        "business_goals": business_goals,
        "actors": actors,
        "requirements": requirements if nested else [],
        "user_stories": [] if nested else user_stories,
        "acceptance_criteria": [] if nested else acceptance_criteria,
        "scope_in": [
            s.get("story_title", "") for s in all_stories
            if isinstance(s, dict) and s.get("story_title")
        ],
    }

    saved_intake = extract_intake_fields(req_state.get("generated_prd", ""))
    markdown_content = fill_template_body(
        project_id=str(project_id),
        project_name=str(project.get("name", "")),
        pmo_no=saved_intake.get("pmo_no"),
        author=saved_intake.get("author") or "Product Owner",
        status=format_project_status(project.get("status")),
        stakeholders=saved_intake.get("stakeholders"),
        business_topics=saved_intake.get("business_topics"),
        version=version_number,
        data=data,
        version_summary="Initial approved version",
        version_author="AI Assistance",
        # EXPORTED DOCUMENT VERSION = THE LEDGER'S semver: fill with the pending
        # marker, resolve the semver this snapshot will get (same inputs as
        # record_prd_version) and stamp it in below, so the cover and the
        # Version History section show prd_versions.semver — never V{n}.0.
        version_label=PENDING_VERSION_LABEL,
    )
    semver = None
    try:
        from app.version_service import resolve_document_semver
        semver = await resolve_document_semver(
            str(project_id), db, document=markdown_content, change_type="ai",
        )
    except Exception as resolve_err:
        # FAIL SOFT: an unstamped-pending document must still export — keep the
        # legacy V{n}.0 label and let the row fall back to the repo default.
        logger.error(f"[PRD EXPORT] Failed to resolve PRD semver: {resolve_err}", exc_info=True)
    markdown_content = stamp_version_label(
        markdown_content, semver or f"V{version_number}.0",
    )
    # Persist the newly generated PRD as an immutable version record
    version_record = None
    try:
        # PRD-EXPORT 7.2 — Optional export snapshot insertion. Unlike ordinary
        # VERSION 6.6 recording, this direct insert is mandatory for this endpoint.
        version_record = await PRDVersionRepository.create(str(project_id), {
            "generated_prd": markdown_content,
            "generated_by": "prd_export_endpoint",
            # On successful prediction the stamp and row share the SemVer. If
            # prediction failed, the document used legacy V{n}.0 while 4.1
            # independently falls back to row SemVer 1.0.0.
            "semver": semver,
        }, db)
        logger.info(f"[PRD EXPORT] Created PRD version {version_record['version_number']} for project {project_id}.")
    except Exception as version_err:
        # FAIL LOUDLY: a failed PRD version persistence must not be masked as a
        # successful export (the caller would otherwise receive a version_number that
        # was never actually persisted to the DB).
        logger.error(f"[PRD EXPORT] Failed to create PRD version: {str(version_err)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PRD generated but failed to persist version record: {str(version_err)}",
        ) from version_err

    # PRD-EXPORT 8.2 — Publish only after the snapshot row exists; publication
    # failure remains request-fatal and can roll back the transaction.
    await event_manager.publish(str(project_id), "prd_exported", {
        "project_id": str(project_id),
        "version": version_record["version_number"] if version_record else version_number,
    })

    return {
        "project_id": project_id,
        "version": version_record["version_number"] if version_record else version_number,
        "version_id": version_record["version_id"] if version_record else None,
        "prd_markdown": markdown_content,
        "mermaid_diagram": ""
    }


# ==========================================
# PRD FILE EXPORT API (LaTeX -> PDF / DOCX)
# ==========================================
# The generated PRD is a LaTeX document that follows the authoritative
# template-krungsrinimble.tex template, so both file exports are built from
# THAT LaTeX server-side:
#   DOCX -> native python-docx renderer (Pandoc fallback)
#   PDF  -> render the template-faithful DOCX through LibreOffice, with direct
#           LaTeX compilation as a fallback when LibreOffice is unavailable.
# Legacy Markdown documents are first filled into the same official template.
# The browser never parses the LaTeX as markdown again.


class LatexExportPayload(BaseModel):
    """Body for the compiled-file export endpoints.

    ``latex_source`` is the PRD document as held by the frontend store (a
    standalone LaTeX document). It is only a FALLBACK: the export endpoints
    resolve the LATEST stored document server-side (see
    ``_latest_stored_prd_source``) so both PDF and DOCX always carry the
    newest version. ``project_name`` shapes the suggested download filename;
    ``version`` is only a legacy fallback when no PRD version snapshot exists.
    """

    latex_source: str = Field(..., description="Full LaTeX PRD document source.")
    version: Optional[int] = Field(None, ge=1, description="PRD version for the filename.")
    project_name: Optional[str] = Field(None, description="Project name for the filename.")


def _timestamp_stamp(now: Optional[datetime] = None) -> str:
    """``YYYY-MM-DD_HHMM_AM/PM`` wall-clock stamp used in PRD filenames."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d_%I%M_%p")


def _safe_filename_stem(
    project_id: UUID,
    payload: LatexExportPayload,
    now: Optional[datetime] = None,
    semver: Optional[str] = None,
) -> str:
    """PRD-EXPORT 5.4 — Build the authoritative download filename.

    ``PRD_<project>_V<version>_<YYYY-MM-DD>_<HHMM>_<AM/PM>`` download name.

    Underscore-separated so the browser/default OS download name matches the
    shared ``PRD_projectname_version_date_time`` convention on both PDF and
    DOCX exports. ``now`` is injectable for deterministic tests.
    """
    name = (payload.project_name or "").strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:48].strip("_") if name else str(project_id)[:8]
    # A downloaded document is a PRD release, so its immutable ledger SemVer
    # identifies it more accurately than the internal monotonic row number.
    # Keep the payload integer only for pre-ledger/legacy projects.
    version_label = (semver or "").strip()
    if version_label:
        version_label = re.sub(r"[^0-9A-Za-z.+-]+", "_", version_label).strip("_")
        version_label = version_label.removeprefix("v").removeprefix("V")
    elif payload.version:
        version_label = str(payload.version)
    version_part = f"_V{version_label}" if version_label else ""
    return f"PRD_{stem}{version_part}_{_timestamp_stamp(now)}"


async def _latest_stored_prd_source(project_id: str, db: AsyncSession) -> str:
    """PRD-EXPORT 7.1 — Read latest saved PRD: state/document first, ledger fallback.

    Mirrors the project-state read path (``RequirementStateRepository``): the
    on-screen preview and BOTH file exports must resolve to the SAME latest
    version, so the export endpoints resolve the document server-side instead
    of trusting the frontend-posted copy — which can briefly lag a live sync
    (a generation/confirm/section-edit that just landed) and would otherwise
    compile an OLDER version than the preview shows. Empty string when the
    project has no stored document yet.
    """
    # Resolve through the same repository used by the preview. It compares the
    # materialized document with the immutable version ledger and returns the
    # newest saved PRD, including manual section edits.
    state = await RequirementStateRepository.get_by_project_id(project_id, db)
    source = ((state or {}).get("generated_prd") or "").strip()
    if source:
        return source

    # Compatibility fallback for old projects created before a requirement
    # state row existed. These sources still pass through the unchanged
    # Krungsri template renderer below.
    doc = await PRDDocumentRepository.get_latest_for_project(project_id, db)
    source = ((doc or {}).get("prd_markdown") or "").strip()
    if source:
        return source
    ledger = await PRDVersionRepository.get_latest(project_id, db)
    return ((ledger or {}).get("generated_prd") or "").strip()


def _markdown_export_narrative(source: str) -> Dict[str, Any]:
    """Recover useful prose from a legacy Markdown PRD for template fields.

    Older projects pre-date the LaTeX template and therefore only have a
    Markdown snapshot.  Exporting that snapshot with Pandoc reproduces the old
    generic layout.  We instead retain its prose while placing it in the
    authoritative Krungsri template.
    """
    sections: Dict[str, List[str]] = {}
    current = ""
    title = ""
    for raw_line in (source or "").splitlines():
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", raw_line)
        if heading:
            label = re.sub(r"^\d+(?:\.\d+)*[.)]?\s*", "", heading.group(2)).strip()
            if len(heading.group(1)) == 1 and not title:
                title = re.sub(r"^PRD\s*:\s*", "", label, flags=re.I).strip()
            current = label.lower()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(raw_line)

    from app.prd_filler import is_template_only_detail

    def prose(*names: str, field: str = "") -> str:
        for name in names:
            for heading, lines in sections.items():
                if name in heading:
                    cleaned = []
                    for line in lines:
                        value = line.strip()
                        if not value or value.startswith("|") or re.fullmatch(r"[-:| ]+", value):
                            continue
                        value = re.sub(r"^[-*+]\s+", "", value)
                        value = re.sub(r"^\d+[.)]\s+", "", value)
                        cleaned.append(value)
                    if cleaned:
                        value = " ".join(cleaned)
                        return "" if is_template_only_detail(value, field) else value
        return ""

    summary = prose("executive summary", "introduction", "overview")
    if not summary:
        summary = prose(*tuple(sections.keys()))
    return {
        "epic_name": title,
        "problem_statement": prose("problem statement", "problem"),
        "business_goals": prose("business objectives", "business goals", "objectives"),
        "narrative": {
            "exec_summary": summary,
            "expected_benefit": prose("expected benefit", "benefits"),
            "success_metrics": prose("success metrics", "metrics"),
            "non_functional": prose("non-functional", "non functional", field="non_functional"),
            "tps_volume": prose("tps", "customer volume"),
            "growth": prose("growth"),
            "launch_plan": prose("launch", "rollout"),
            "open_questions": prose("open questions", "risks"),
            "assumptions": prose("assumptions"),
        },
    }


async def _fill_official_export_template(
    project_id: str,
    project: Dict[str, Any],
    markdown_source: str,
    payload: LatexExportPayload,
    db: AsyncSession,
) -> str:
    """PRD-EXPORT 5.2 — Put legacy prose/project data into the official template."""
    from app.prd.model import CanonicalPRD, PRDContent, PRDMetadata
    from app.prd.renderers import render_latex
    from app.prd_filler import (
        extract_intake_fields,
        extract_manual_export_fields,
        format_project_status,
    )

    req_state = await RequirementStateRepository.get_by_project_id(project_id, db) or {}
    requirements = req_state.get("requirements") or []
    user_stories = req_state.get("user_stories") or []
    nested = any(
        isinstance(requirement, dict) and requirement.get("user_stories")
        for requirement in requirements
    )
    all_stories = (
        [
            story
            for requirement in requirements
            if isinstance(requirement, dict)
            for story in (requirement.get("user_stories") or [])
        ]
        if nested else user_stories
    )

    recovered = _markdown_export_narrative(markdown_source)
    goals = req_state.get("business_goals") or []
    if not goals and recovered["business_goals"]:
        goals = [recovered["business_goals"]]
    problem_statement = recovered["problem_statement"]

    version_number = req_state.get("version_number") or payload.version or 1
    try:
        version_number = int(version_number)
    except (TypeError, ValueError):
        version_number = 1
    versions = await PRDVersionRepository.get_by_project(project_id, db)
    latest_version = versions[0] if versions else None
    version_label = ((latest_version or {}).get("semver") or "").strip() or f"V{version_number}.0"

    # Rebuild the same complete Version History shown by the preview. The
    # newest ledger row is the current export row; all older rows are supplied
    # as history and the filler writes one concise sentence per version.
    history_author = extract_intake_fields(markdown_source).get("author") or "Product Owner"
    previous_version_history = "\n".join(
        f"- v{version.get('semver') or version.get('version_number')} "
        f"({version.get('created_at') or ''}, "
        f"{'AI Assistance' if version.get('change_type') == 'ai' else history_author}): "
        f"{version.get('change_summary') or 'PRD updated.'}"
        for version in versions[1:]
    )
    current_version_summary = (
        (latest_version or {}).get("change_summary")
        or ("Initial PRD created." if not versions else "PRD updated with the latest requirements.")
    )

    epic_name = req_state.get("epic_name") or recovered["epic_name"]
    if not epic_name and requirements and isinstance(requirements[0], dict):
        epic_name = requirements[0].get("title") or ""
    epic_name = epic_name or str(project.get("name") or "")

    data = {
        "epic_name": epic_name,
        "business_goals": goals,
        "actors": req_state.get("actors") or [],
        "requirements": requirements if nested else [],
        "user_stories": [] if nested else user_stories,
        "acceptance_criteria": [] if nested else (req_state.get("acceptance_criteria") or []),
        "scope_in": [
            story.get("story_title") or story.get("i_want_to") or ""
            for story in all_stories if isinstance(story, dict)
        ],
        "problem_statement": [problem_statement] if problem_statement else [],
    }
    narrative = {key: value for key, value in recovered["narrative"].items() if value}
    manual_fields = extract_manual_export_fields(markdown_source)
    # Saved preview edits are newer and explicitly user-owned, so they take
    # precedence over reconstructed requirement/narrative values.
    narrative.update(manual_fields.get("narrative") or {})
    if manual_fields.get("scope_present"):
        data["scope_in"] = manual_fields["scope_in"]
        data["scope_out"] = manual_fields["scope_out"]
    if manual_fields.get("epic_name_present"):
        data["epic_name"] = manual_fields["epic_name"]
    if manual_fields.get("functional_table_present"):
        # The Product Scope section was manually edited; export those saved
        # FR/AC rows instead of reconstructing older rows from requirements.
        data["requirements"] = []
        data["user_stories"] = manual_fields["functional_rows"]
        data["acceptance_criteria"] = []
    saved_intake = extract_intake_fields(markdown_source)
    document = CanonicalPRD(
        metadata=PRDMetadata(
            project_id=project_id,
            project_name=(
                saved_intake.get("pmo_name")
                or str(project.get("name") or payload.project_name or "")
            ),
            pmo_no=saved_intake.get("pmo_no"),
            author=saved_intake.get("author") or "Product Owner",
            status=format_project_status(project.get("status")),
            stakeholders=saved_intake.get("stakeholders") or {},
            business_topics=saved_intake.get("business_topics") or {},
            reviews=(
                manual_fields["reviews"]
                if manual_fields.get("reviews_present")
                else None
            ),
            version=version_number,
            version_summary=current_version_summary,
            version_history=previous_version_history,
            version_label=version_label,
            version_author=(
                "AI Assistance"
                if (latest_version or {}).get("change_type") == "ai"
                else history_author
            ),
        ),
        content=PRDContent.model_validate(data),
        narrative=narrative,
    )
    return render_latex(document)


async def _export_prd_bytes(
    project_id: str,
    payload: LatexExportPayload,
    db: AsyncSession,
    fmt: str,
) -> Response:
    """PRD-EXPORT 5.1–5.4 — Resolve source, normalize, compile, and respond.

    Blocking subprocess work runs in a worker thread so the event loop (and the
    SSE stream other tabs hold open) is never stalled by a slow TeX compile.
    """
    project = await ProjectRepository.get_by_id(str(project_id), db)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from app.prd_filler import strip_template_only_details

    source = strip_template_only_details(payload.latex_source).strip()

    # PRD-EXPORT 5.1 — Resolve the newest stored source server-side; the posted
    # source is only a pre-generation fallback. EXPORTS ALWAYS CARRY THE LATEST VERSION.
    # document server-side (the same resolution the project-state/preview
    # read path uses) and keep the frontend-posted copy only as a fallback
    # for exports BEFORE the first generation, where the store holds the
    # blank template skeleton and nothing is stored yet. Without this, an
    # export issued while the frontend store briefly lagged a live sync
    # compiled an OLDER version than the one on screen.
    stored_source = await _latest_stored_prd_source(project_id, db)
    if stored_source:
        source = strip_template_only_details(stored_source).strip()

    if not source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No PRD content was supplied to export.",
        )

    # Legacy and on-screen PRDs are Markdown.  Sending them directly to Pandoc
    # was the reason exports continued to use the old generic formatting even
    # after template-krungsrinimble.tex was updated.  Rebuild those snapshots
    # into the official template first, preserving their prose and filling the
    # structured requirements from the database.  Newer stored LaTeX documents
    # are already template-shaped and remain untouched.
    # PRD-EXPORT 5.2 — Normalize legacy Markdown into the official LaTeX template.
    if is_markdown_prd(source):
        source = await _fill_official_export_template(
            project_id, project, source, payload, db,
        )
    is_markdown = False

    # PRD-EXPORT 5.3 — Compiler selection: native DOCX; PDF via that DOCX and
    # LibreOffice when available, with direct PDF compilation as fallback.
    def _pdf_export(src: str, markdown_reader: bool) -> bytes:
        """Render PDF from the same reference-shaped document as DOCX.

        The supplied reference PDF originated from a Word-style layout (Aptos,
        US Letter and nested grids), so LibreOffice rendering of the native
        DOCX is the closest match. Direct LaTeX remains a reliable fallback.
        """
        if soffice_available():
            build = convert_markdown_to_docx if markdown_reader else convert_latex_to_docx
            try:
                return docx_to_pdf(build(src))
            except RuntimeError as soffice_err:
                logger.warning(
                    "DOCX->PDF conversion failed for project %s (%s); "
                    "falling back to direct PDF rendering.",
                    project_id, soffice_err,
                )
        return convert_markdown_to_pdf(src) if markdown_reader else compile_latex_to_pdf(src)

    try:
        if fmt == "pdf":
            data = await asyncio.to_thread(_pdf_export, source, is_markdown)
        elif is_markdown:
            data = await asyncio.to_thread(convert_markdown_to_docx, source)
        else:
            data = await asyncio.to_thread(convert_latex_to_docx, source)
    except RuntimeError as compile_err:
        # Defense in depth: a source classified as LaTeX (it carried strong
        # \begin{...}/\documentclass markers) can still be MOSTLY Markdown —
        # e.g. a legacy PRD containing a fenced ```latex block. One retry
        # through the Markdown pipeline beats an opaque 502 for the user.
        if not is_markdown and has_markdown_structure(source):
            logger.warning(
                "LaTeX export failed for project %s (%s) (%s); "
                "retrying via the Markdown pipeline.",
                project_id, fmt, compile_err,
            )
            if fmt == "pdf":
                retry_fn = lambda: _pdf_export(source, True)  # noqa: E731
            else:
                retry_fn = lambda: convert_markdown_to_docx(source)  # noqa: E731
            try:
                data = await asyncio.to_thread(retry_fn)
            except FileNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(err),
                ) from err
            except ValueError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(err),
                ) from err
            except RuntimeError as err:
                logger.error("Markdown retry export failed for project %s (%s): %s", project_id, fmt, err)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err),
                ) from err
        else:
            logger.error("LaTeX export failed for project %s (%s): %s", project_id, fmt, compile_err)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(compile_err),
            ) from compile_err
    except FileNotFoundError as err:
        # Toolchain not installed/reachable on this host.
        logger.error("LaTeX export toolchain missing: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        ) from err
    except ValueError as err:
        # Empty / unusable source.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(err),
        ) from err

    media_type = "application/pdf" if fmt == "pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    latest_prd_version = await PRDVersionRepository.get_latest(project_id, db)
    semver = str((latest_prd_version or {}).get("semver") or "").strip()
    stem = _safe_filename_stem(UUID(project_id), payload, semver=semver or None)
    # PRD-EXPORT 8.1 — Binary download response with authoritative filename,
    # media type, template identity, and resolved SemVer headers.
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.{fmt}"',
            "X-PRD-Template": "krungsrinimble-reference-v2",
            "X-PRD-SemVer": semver,
        },
    )


@router.post("/api/project/{project_id}/export/pdf", status_code=status.HTTP_200_OK)
async def export_prd_pdf(
    project_id: str,
    payload: LatexExportPayload,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """PRD-EXPORT 4.1 — Compile and return the latest saved PRD as PDF.

    Renders the supplied PRD into a downloadable PDF using the same native
    Word layout as DOCX (direct LaTeX compilation is the fallback).

    The PRD source follows prompts/template-krungsrinimble.tex - this endpoint
    renders THAT LaTeX, it does not re-parse Markdown.

    Returns:
        Response: application/pdf attachment.

    Raises:
        HTTPException: 404 unknown project; 422 empty/legacy-Markdown source;
            502 TeX compile failure; 503 toolchain unavailable.
    """
    return await _export_prd_bytes(project_id, payload, db, "pdf")


@router.post("/api/project/{project_id}/export/docx", status_code=status.HTTP_200_OK)
async def export_prd_docx(
    project_id: str,
    payload: LatexExportPayload,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """PRD-EXPORT 4.2 — Compile and return the latest saved PRD as DOCX.

    Converts the supplied LaTeX PRD into a downloadable Word document with the
    native Krungsri template renderer (Pandoc is a defensive fallback).

    Returns:
        Response: .docx attachment converted from the Krungsri Nimble LaTeX.

    Raises:
        HTTPException: same contract as the PDF endpoint.
    """
    return await _export_prd_bytes(project_id, payload, db, "docx")


@router.post("/api/prd/convert", status_code=status.HTTP_200_OK)
async def convert_prd_to_markdown(
    payload: LatexExportPayload,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, str]:
    """
    Normalize a stored PRD into GFM Markdown for the on-screen preview.

    Accepts either the Krungsri LaTeX body produced by the Architect agent or a
    legacy Markdown PRD and returns clean GFM Markdown (pipe tables, ``### ``
    section headings) that the frontend markdown renderer can display directly,
    so raw LaTeX never leaks into the PRD panel.

    Returns:
        Dict[str, str]: ``{"markdown": <converted document>, "source_kind":
        "latex" | "markdown"}``.

    Raises:
        HTTPException: 422 when no usable content was supplied.
    """
    from app.prd_filler import strip_template_only_details

    source = strip_template_only_details(payload.latex_source).strip()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No PRD content was supplied to convert.",
        )
    source_kind = "markdown" if is_markdown_prd(source) else "latex"
    try:
        markdown = await asyncio.to_thread(prd_to_markdown, source)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(err),
        ) from err
    except RuntimeError as err:
        logger.error("PRD preview conversion failed: %s", err)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(err),
        ) from err
    return {"markdown": markdown, "source_kind": source_kind}


# ==========================================
# PRD VERSION HISTORY API
# ==========================================

# VERSION 4.1 — GET ledger metadata (UI 1.0 → hook 2.1 → client 3.1 → repository 7.3), newest first. `limit` bounds a
#               page and `before_version` seeks older rows. Full generated_prd bodies
#               are blanked below; single-snapshot/diff routes retain content access.
@router.get("/api/project/{project_id}/prd-versions", response_model=List[PRDVersionResponse], status_code=status.HTTP_200_OK)
async def get_prd_versions(
    project_id: str,
    limit: Optional[int] = Query(None, ge=1, le=100),
    before_version: Optional[int] = Query(None, ge=1),
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> List[PRDVersionResponse]:
    """
    Retrieves one newest-first page of PRD-version metadata for a project.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        List[PRDVersionResponse]: Immutable PRD version records, newest first.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    versions = await PRDVersionRepository.get_by_project(
        project_id,
        session,
        limit=limit,
        before_version=before_version,
    )
    # PERF (project switch) — the timeline only needs METADATA: semver, author,
    # change summary and the per-section change records. Serializing every
    # snapshot's full PRD body made this list the largest payload in the app
    # (N versions x whole document), downloaded on EVERY project open even
    # though no consumer reads it (toVersionHistory ignores generated_prd).
    # Content stays reachable through the dedicated single-version endpoints
    # (VERSION 4.3/4.4) and the diff endpoint (VERSION 4.2).
    return [{**v, "generated_prd": ""} for v in versions]


# VERSION 4.2 — GET section/line diff (UI 1.2 → client 3.2 → domain 6.8). Omitted base resolves to
#               the immediate predecessor or empty base 0 for the first snapshot.
#               Invalid UUID is 400; missing target/base or base >= target is 404.
@router.get(
    "/api/project/{project_id}/prd-versions/{version_number}/diff",
    response_model=PrdVersionDiffResponse,
    status_code=status.HTTP_200_OK,
)
async def get_prd_version_diff(
    project_id: str,
    version_number: int,
    base_version: Optional[int] = Query(default=None, description="Older base version; defaults to the immediate predecessor."),
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PrdVersionDiffResponse:
    """Section-level diff of one PRD version against an earlier base version.

    A differing section locked at request time is labelled
    ``locked_preserved``. This is a current-state display classification, not
    proof of the lock state when either historical snapshot was recorded.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    from app.version_service import diff_versions
    diff = await diff_versions(
        project_id, version_number, session,
        base_version=base_version,
    )
    if diff is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Version {version_number} of project {project_id} does not exist"
                " or has no base version to compare against."
            ),
        )
    return diff


# VERSION 4.3 — GET latest full snapshot through 7.5; 404 when no row exists.
#               No application caller was found: the SPA gets its newest metadata from
#               4.1, while server-side paths call 7.5 directly.
@router.get("/api/project/{project_id}/prd-versions/latest", response_model=PRDVersionResponse, status_code=status.HTTP_200_OK)
async def get_latest_prd_version(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PRDVersionResponse:
    """
    Retrieves the latest PRD version for a project.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        PRDVersionResponse: The most recent immutable PRD version record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if no
            PRD versions exist for the project.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    version = await PRDVersionRepository.get_latest(project_id, session)
    if not version:
        raise HTTPException(status_code=404, detail="No PRD versions found for this project")
    return version


# VERSION 4.4 — GET one snapshot by number (repository 7.4) → 404 when absent.
#             DISCREPANCY (documented, not changed): no frontend caller either (see 4.3);
#             the diff endpoint (4.2) returns the section contents the UI needs.
@router.get("/api/project/{project_id}/prd-versions/{version_number}", response_model=PRDVersionResponse, status_code=status.HTTP_200_OK)
async def get_prd_version_by_number(
    project_id: str,
    version_number: int,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PRDVersionResponse:
    """
    Retrieves a specific PRD version by version number.

    Args:
        project_id: Project UUID string.
        version_number: Monotonic PRD version number.
        session: Active asynchronous database session.

    Returns:
        PRDVersionResponse: The requested immutable PRD version record.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if the
            requested version does not exist for the project.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    version = await PRDVersionRepository.get_by_version_number(project_id, version_number, session)
    if not version:
        raise HTTPException(status_code=404, detail=f"PRD version {version_number} not found for this project")
    return version


# VERSION 4.5 — POST whole-document restore (UI 1.5 → hook 2.2 → client 3.3).
#               Application orchestration 5.1 seeds parts from the current document
#               (target only when no current document); visit target-present sections;
#               skip currently locked and identical sections; write changed unlocked
#               sections; then stitch. Current-only sections remain. Recording through
#               6.6 normally appends history, but its exception is caught: the document
#               still persists and the response falls back to source number/blank
#               semver. `preserved_locked_sections` counts all locked target sections
#               encountered, including identical ones. Publication precedes request
#               commit; an uncaught later failure rolls the transaction back. The
#               owner-supplied `restored_by` query value becomes the recorded author.
@router.post(
    "/api/project/{project_id}/prd-versions/{version_number}/restore",
    response_model=PrdVersionRestoreResponse,
    status_code=status.HTTP_200_OK,
)
async def restore_prd_version(
    project_id: str,
    version_number: int,
    restored_by: str = "user",
    session: AsyncSession = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_project_owner),
) -> PrdVersionRestoreResponse:
    """Restore target-present unlocked sections into the current document.

    A successful ledger write appends a snapshot. If that write fails, the
    section/document restore still completes and the response uses its fallback
    version fields.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    target = await PRDVersionRepository.get_by_version_number(project_id, version_number, session)
    if not target:
        raise HTTPException(status_code=404, detail=f"PRD version {version_number} not found for this project")

    # VERSION 5.1 — Seed the nine lockable parts from the CURRENT document first so the
    # restore always has rows to write into (no-op when they already exist).
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    current_document = (state or {}).get("generated_prd") or target["generated_prd"]
    sections = await ensure_sections_seeded(project_id, session, source_markdown=current_document)

    # Which stored part holds which content + which are currently locked.
    # Locked target parts are skipped regardless of whether their content differs.
    current_map = {s["section_key"]: (s.get("content") or "") for s in sections}
    locked_map = {s["section_key"]: bool(s.get("is_locked")) for s in sections}
    id_map = {s["section_key"]: s["id"] for s in sections}

    from app.prd_section_service import split_markdown_sections
    from app.version_service import record_prd_version

    restored_sections = 0
    preserved_locked = 0
    for part in split_markdown_sections(target["generated_prd"] or ""):
        key = part["section_key"]
        target_content = part["content"].strip()
        if locked_map.get(key):
            preserved_locked += 1
            continue
        stored = current_map.get(key)
        if stored is not None and stored.strip() == target_content:
            continue  # identical — no section version churn for unchanged parts
        updated = await PRDSectionRepository.update_content(
            id_map[key], project_id, target_content, session,
            changed_by=restored_by or "user",
            change_summary=f"Restored from PRD version {version_number}.",
        )
        if updated is not None:
            restored_sections += 1

    # VERSION 5.2 — Re-stitch from every current part (including current-only and locked parts),
    # then attempt to append an immutable ledger version.
    document_markdown = await assemble_document_markdown(project_id, session)
    try:
        new_version = await record_prd_version(
            project_id, session,
            generated_prd=document_markdown,
            generated_by=restored_by or "user",
            change_type="manual",
            change_summary=f"Restored document from version {version_number}.",
        )
    except Exception as ver_err:  # never block a valid restore on ledger bookkeeping
        logger.warning("[PRD VERSIONS] Failed to record restore version: %s", ver_err)
        new_version = {"version_number": version_number, "semver": ""}

    # Persist the restored document as the requirement state's current PRD.
    await RequirementStateRepository.save_or_update(project_id, {
        "generated_prd": document_markdown,
    }, session)

    # VERSION 8.1 — Publish after restored state is flushed. Request-level commit
    # still follows publication, so a later uncaught failure rolls everything back.
    await event_manager.publish(project_id, "prd_version_restored", {
        "project_id": project_id,
        "restored_from_version": version_number,
        "new_version_number": new_version.get("version_number"),
    })

    logger.info(
        "[PRD VERSIONS] Restored project %s from v%d -> new v%s (restored=%d preserved_locked=%d)",
        project_id, version_number, new_version.get("version_number"),
        restored_sections, preserved_locked,
    )

    return PrdVersionRestoreResponse(
        restored_from_version=version_number,
        new_version_number=new_version.get("version_number") or version_number,
        semver=new_version.get("semver") or "",
        document_markdown=document_markdown,
        restored_sections=restored_sections,
        preserved_locked_sections=preserved_locked,
    )

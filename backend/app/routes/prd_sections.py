"""
PRD Section routes — part-level PRD editing, locking, and versioning.

A PRD is a collection of NINE editable parts (the same parts the preview
renders). Each part can be:
  - edited independently           -> PATCH  .../prd/sections/{section_key}
  - locked / unlocked individually -> POST   .../prd/sections/{section_key}/lock|unlock
  - inspected per-version          -> GET    .../prd/sections/{section_key}/versions
  - restored from history          -> POST   .../prd/sections/{section_key}/revert/{version}

Only the section lock is enforced before a part mutation. Restores are APPEND-ONLY:
reverting copies old content into a NEW version, history is never rewritten.
"""
import logging
from typing import Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, require_project_owner
from app.database import get_db
from app.event_manager import event_manager
from app.prd_section_service import (
    VALID_REVIEW_STATUSES,
    assemble_document_markdown,
    ensure_sections_seeded,
    merge_edited_section,
    preview_prd_sections,
    sync_sections_from_prd,
)
from app.repositories import (
    ArtifactEventLogRepository,
    PRDSectionRepository,
    PRDSectionVersionRepository,
    RequirementStateRepository,
)
from app.schemas import (
    ArtifactLockRequest,
    PrdSectionListResponse,
    PrdSectionResponse,
    PrdSectionRevertResponse,
    PrdSectionUpdate,
    PrdSectionUpdateResponse,
    PrdSectionVersionResponse,
)

logger = logging.getLogger("app.routes.prd_sections")

router = APIRouter()


async def _validate_project(project_id: str) -> None:
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")


async def _get_section_or_404(section_key: str, project_id: str, session: AsyncSession) -> Dict:
    section = await PRDSectionRepository.get_by_key(section_key, project_id, session)
    if not section:
        raise HTTPException(status_code=404, detail=f"PRD section '{section_key}' not found")
    return section


async def _ensure_sections_for_write(project_id: str, session: AsyncSession) -> None:
    """Materialize preview-only sections at an explicit mutation boundary."""
    existing = await PRDSectionRepository.get_by_project(project_id, session)
    if existing:
        return
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    await ensure_sections_seeded(
        project_id,
        session,
        source_markdown=(state or {}).get("generated_prd") or "",
    )


# PRD-SECTION 2.1 — READ: all nine parts in document order (PRD-SECTION 1.1). Auth:
#             AUTH 7.5. This endpoint is strictly read-only: when no stored parts
#             exist it returns deterministic preview rows without inserting them.
@router.get("/api/project/{project_id}/prd/sections", response_model=PrdSectionListResponse, status_code=status.HTTP_200_OK)
async def list_prd_sections(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
    materialize: bool = False,
) -> PrdSectionListResponse:
    """List all PRD parts in document order.

    If no stored parts exist, deterministic preview rows are derived from the
    current PRD (or the official template). ``materialize=true`` is an
    explicit first-write preparation step for callers that need to patch a
    template field without losing the template's surrounding content.
    """
    await _validate_project(project_id)
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    source = (state or {}).get("generated_prd") or ""
    sections = await PRDSectionRepository.get_by_project(project_id, session)
    if not sections:
        if materialize:
            sections = await ensure_sections_seeded(project_id, session, source_markdown=source)
        else:
            sections = preview_prd_sections(project_id, source)
    return {"project_id": project_id, "sections": sections}


# PRD-SECTION 2.2 — WRITE: edit ONE part (PRD-SECTION 1.3). The most guarded write in
#             the app, in this order:
#               2.2.0 locks: project (2.0.1) → document (2.0.2) → (section, in 4.4)
#               2.2.1 review_status validation → 422 (PRD-SECTION 3.1.1)
#               2.2.2 section lookup → 404
#               2.2.3 reconcile with the latest document (so the merge base is the
#                     current version, never a stale/blank template part)
#               2.2.4 THREE-WAY merge (PRD-SECTION 3.5) so a concurrent AI change is
#                     not clobbered by the manual save
#               2.2.5 repository update_content (4.4: appends a prd_section_versions
#                     row FIRST, then updates the materialized content + OWNERSHIP;
#                     lock-gated). A manual save marks the part human-owned
#                     (ai_generatable=false) so "Generate PRD" preserves it.
#               2.2.6 artifact_event_logs append (fail-soft)
#               2.2.7 re-stitch the whole document (PRD-SECTION 3.7)
#               2.2.8 VERSION 3.6 ledger row FIRST (so version_number advances), then
#                     PROJECT 8.2.2 persists the document — ORDER MATTERS: recording the
#                     version first makes the state write create a NEW prd_documents row
#                     for version N+1 instead of overwriting version N's row
#               2.2.9 SSE "prd_section_updated"
@router.patch("/api/project/{project_id}/prd/sections/{section_key}", response_model=PrdSectionUpdateResponse, status_code=status.HTTP_200_OK)
async def update_prd_section(
    project_id: str,
    section_key: str,
    payload: PrdSectionUpdate,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PrdSectionUpdateResponse:
    """
    Edit ONE part of the PRD.

    Enforces project > document > section locks, appends an immutable version
    row for the part, then re-stitches and persists the full document.
    """
    await _validate_project(project_id)
    await _ensure_sections_for_write(project_id, session)

    if payload.review_status and payload.review_status not in VALID_REVIEW_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"review_status must be one of: {', '.join(sorted(VALID_REVIEW_STATUSES))}",
        )

    section = await _get_section_or_404(section_key, project_id, session)

    # MERGE-WITH-LATEST-VERSION: the editor is seeded from the project's CURRENT
    # document (``generated_prd``) — the latest version the user sees. The
    # ``prd_sections`` row can lag behind it (parts seeded from the template
    # before the first generation, or a generation whose part-sync failed), and
    # merging against that stale template would drop the latest content. So
    # reconcile the stored parts with the latest document FIRST — reusing the
    # same ownership contract as regeneration (locked / human-owned parts are
    # preserved verbatim) — then merge the edit on top of the freshly-synced
    # part. When the parts are already in sync this is a no-op.
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    latest_document = (state or {}).get("generated_prd") or ""
    if latest_document.strip():
        synced = await sync_sections_from_prd(
            project_id, latest_document, session,
            # A SYSTEM reconciliation, not a user edit — use a distinct author
            # so the manual-ownership backfill never mistakes it for one.
            changed_by="reconcile",
            change_summary="Reconciled with the latest PRD version before a manual edit.",
        )
        if synced is not None:
            # Re-read so ``section.content`` is the LATEST text, not the stale row.
            section = await _get_section_or_404(section_key, project_id, session)

    # MERGE-WITH-LAST-VERSION (three-way): the UI sends BOTH the full edited
    # text (``content``) AND the text it started editing from
    # (``base_content``). ``section.content`` is the CURRENT stored text —
    # possibly advanced by an AI regeneration while the user was editing.
    # ``merge_edited_section`` keeps the user's touched lines and preserves
    # concurrent changes outside the edit window, so a manual save always
    # produces "last version + this edit" instead of overwriting. Legacy
    # clients without ``base_content`` fall back to verbatim storage.
    # PRD-SECTION 2.2.3 — Three-way merge with the CURRENT stored text (3.5): keeps the
    #                 user's touched lines and preserves concurrent AI changes; legacy
    #                 clients without base_content are stored verbatim.
    edited_content = merge_edited_section(
        payload.base_content,
        section.get("content"),
        payload.content or "",
    )
    requested_actor = (payload.updated_by or "user").strip()
    if requested_actor.lower() in {"user", "you"}:
        from app.prd_filler import extract_intake_fields
        project_author = str(
            extract_intake_fields(latest_document).get("author") or ""
        ).strip()
    else:
        project_author = ""
    actor_name = (
        project_author or "User"
        if requested_actor.lower() in {"user", "you"}
        else requested_actor
    )

    try:
        # PRD-SECTION 2.2.4 — Repository write (4.4): append the previous content as a
        #                 prd_section_versions row, then materialize the new content.
        #                 ArtifactLockError surfaces here as 409.
        # A manual save makes the part HUMAN-OWNED (content_source='human',
        # ai_generatable=False) so the next "Generate PRD" PRESERVES it — the
        # Architect only regenerates ai_generatable parts. Passing
        # ai_generatable=True hands the part back to the AI.
        updated = await PRDSectionRepository.update_content(
            section["id"], project_id, edited_content, session,
            changed_by=actor_name,
            change_summary=payload.change_summary,
            review_status=payload.review_status,
            content_source="ai" if payload.ai_generatable else "human",
            ai_generatable=payload.ai_generatable,
        )
    except Exception as lock_err:
        # ArtifactLockError from the repository lock enforcement
        if type(lock_err).__name__ == "ArtifactLockError":
            raise HTTPException(status_code=409, detail=str(lock_err))
        raise

    if not updated:
        raise HTTPException(status_code=404, detail=f"PRD section '{section_key}' not found")

    try:
        await ArtifactEventLogRepository.log_event(
            artifact_type="prd_section",
            artifact_id=updated["id"],
            action="UPDATE",
            session=session,
            old_value={"content": section.get("content"), "section_key": section_key},
            new_value={"content": payload.content, "version_number": updated.get("version_number")},
            performed_by=actor_name,
            project_id=project_id,
        )
    except Exception as log_err:  # never block an edit on event logging
        logger.warning(f"[PRD SECTIONS] Failed to log update event: {log_err}")

    # PRD-SECTION 2.2.6 — Re-stitch ALL parts (3.7): the parts table is the single
    #                 source of truth for the full document.
    document_markdown = await assemble_document_markdown(project_id, session)

    # ORDER MATTERS: record the version FIRST so requirement_states
    # .version_number advances to N+1; the subsequent save_or_update then
    # propagates the merged document into a NEW prd_documents row for version
    # N+1 instead of overwriting the previous version's row in place. This is
    # what makes a manual edit "merge with the old one and save as a NEW
    # version" — the old version's document row is never touched.
    try:
        from app.version_service import record_prd_version
        await record_prd_version(
            project_id, session,
            generated_prd=document_markdown,
            generated_by=actor_name,
            change_type="manual",
            change_summary=payload.change_summary,
        )
    except Exception as ver_err:  # never block a valid edit on ledger bookkeeping
        logger.warning("[PRD SECTIONS] Failed to record manual PRD version: %s", ver_err)

    await RequirementStateRepository.save_or_update(project_id, {
        "generated_prd": document_markdown,
    }, session)

    await event_manager.publish(project_id, "prd_section_updated", {
        "project_id": project_id,
        "section_key": section_key,
        "version_number": updated.get("version_number"),
    })

    return {"section": updated, "document_markdown": document_markdown}


# PRD-SECTION 2.7 — READ the per-part history (repository 4.5). Callers: this module's
#             revert path (2.3) resolves its target here; the route itself is reached only
#             by API clients/tests — verified: src/api/client.ts defines no
#             getPrdSectionVersions helper and nothing in src/ calls `/versions`, so there
#             is NO section-version viewer in the SPA today (per-part history is inspected
#             through the whole-document VERSION 2.1/2.2 views instead).
@router.get("/api/project/{project_id}/prd/sections/{section_key}/versions", response_model=list[PrdSectionVersionResponse], status_code=status.HTTP_200_OK)
async def list_prd_section_versions(
    project_id: str,
    section_key: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> list[PrdSectionVersionResponse]:
    """Version history of one PRD part, newest first (append-only)."""
    await _validate_project(project_id)
    section = await _get_section_or_404(section_key, project_id, session)
    return await PRDSectionVersionRepository.get_by_section(section["id"], session)


# PRD-SECTION 2.3 — WRITE: revert ONE part to an older version. APPEND-ONLY: the old
#             content becomes a NEW prd_section_versions row — history is never rewritten.
#             Same lock order as 2.2 (project → document → section in 4.4), then the 3.7
#             re-stitch → VERSION 3.6 ledger → PROJECT 8.2.2 document write (same
#             ORDER-MATTERS rationale as 2.2.7) → SSE.
#             DISCREPANCY (documented, not changed): UNREACHABLE FROM THE UI — verified
#             src/api/client.ts exposes no revert helper and nothing in src/ targets
#             `/revert`, so per-part rollback is API/test-only; users restore through the
#             whole-document path instead (VERSION 2.5).
@router.post("/api/project/{project_id}/prd/sections/{section_key}/revert/{version_number}", response_model=PrdSectionRevertResponse, status_code=status.HTTP_200_OK)
async def revert_prd_section(
    project_id: str,
    section_key: str,
    version_number: int,
    updated_by: str = "user",
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PrdSectionRevertResponse:
    """
    Restore an old version of one PRD part.

    APPEND-ONLY: the old content becomes a NEW version row — history is never
    rewritten. Enforces project > document > section locks.
    """
    await _validate_project(project_id)
    await _ensure_sections_for_write(project_id, session)

    section = await _get_section_or_404(section_key, project_id, session)
    version = await PRDSectionVersionRepository.get_by_version_number(
        section["id"], version_number, session
    )
    if not version:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_number} of PRD section '{section_key}' not found",
        )

    # PRD-SECTION 2.3.1 — Repository write (4.4) with the OLD version's content: appends
    #                 a new prd_section_versions row (append-only revert) and updates the
    #                 materialized part; lock-gated like any other write.
    updated = await PRDSectionRepository.update_content(
        section["id"], project_id, version["content"], session,
        changed_by=updated_by or "user",
        change_summary=f"Reverted to version {version_number}.",
    )
    # PRD-SECTION 2.3.2 — Re-stitch all parts with the restored part applied (3.7), then
    #                 the same ORDER-MATTERS ledger → state sequence as 2.2.7.
    document_markdown = await assemble_document_markdown(project_id, session)

    # ORDER MATTERS: record the version first (advances requirement_states
    # .version_number) so the document propagation below creates a NEW
    # prd_documents row for the new version instead of overwriting the old
    # one. A revert must also preserve every previous version's document.
    try:
        from app.version_service import record_prd_version
        await record_prd_version(
            project_id, session,
            generated_prd=document_markdown,
            generated_by=updated_by or "user",
            change_type="manual",
            change_summary=f"Reverted '{section_key}' to version {version_number}.",
        )
    except Exception as ver_err:  # never block a valid revert on ledger bookkeeping
        logger.warning("[PRD SECTIONS] Failed to record revert PRD version: %s", ver_err)

    await RequirementStateRepository.save_or_update(project_id, {
        "generated_prd": document_markdown,
    }, session)

    await event_manager.publish(project_id, "prd_section_reverted", {
        "project_id": project_id,
        "section_key": section_key,
        "restored_from_version": version_number,
    })

    return {
        "section": updated,
        "document_markdown": document_markdown,
        "restored_from_version": version_number,
    }


# PRD-SECTION 2.4 — Per-part LOCK (PRD-SECTION 1.4): blocks 2.2/2.3 AND excludes the part
#             from AI regeneration (ARCHITECT 5.1's sync). Auth: AUTH 7.5; branches: 400
#             UUID → 404 section → 409 already locked → SSE "artifact_locked".
@router.post("/api/project/{project_id}/prd/sections/{section_key}/lock", response_model=Dict, status_code=status.HTTP_200_OK)
async def lock_prd_section(
    project_id: str,
    section_key: str,
    payload: ArtifactLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> Dict:
    """Lock ONE PRD part: blocks edits AND excludes it from AI regeneration."""
    await _validate_project(project_id)
    from app.lock_service import ArtifactLockError, LockService

    await _ensure_sections_for_write(project_id, session)
    section = await _get_section_or_404(section_key, project_id, session)
    try:
        lock_info = await LockService.lock_artifact(
            artifact_type="prd_section",
            artifact_id=section["id"],
            session=session,
            locked_by=payload.locked_by or "user",
            project_id=project_id,
        )
    except ArtifactLockError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await event_manager.publish(project_id, "artifact_locked", {
        "project_id": project_id,
        "artifact_type": "prd_section",
        "artifact_id": section["id"],
        "section_key": section_key,
    })
    return {"status": "locked", "artifact": lock_info}


# PRD-SECTION 2.5 — Per-part UNLOCK: re-enables editing and AI regeneration. Branches:
#             403 when the caller may not release the lock (PermissionError from the lock
#             service), 404 section → SSE "artifact_unlocked".
@router.post("/api/project/{project_id}/prd/sections/{section_key}/unlock", response_model=Dict, status_code=status.HTTP_200_OK)
async def unlock_prd_section(
    project_id: str,
    section_key: str,
    payload: ArtifactLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> Dict:
    """Unlock ONE PRD part so it can be edited and AI-regenerated again."""
    await _validate_project(project_id)
    from app.lock_service import LockService

    await _ensure_sections_for_write(project_id, session)
    section = await _get_section_or_404(section_key, project_id, session)
    try:
        lock_info = await LockService.unlock_artifact(
            artifact_type="prd_section",
            artifact_id=section["id"],
            session=session,
            unlocked_by=payload.locked_by or "user",
            project_id=project_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await event_manager.publish(project_id, "artifact_unlocked", {
        "project_id": project_id,
        "artifact_type": "prd_section",
        "artifact_id": section["id"],
        "section_key": section_key,
    })
    return {"status": "unlocked", "artifact": lock_info}


# PRD-SECTION 2.6 — READ one part incl. its lock + review metadata.
@router.get("/api/project/{project_id}/prd/sections/{section_key}", response_model=PrdSectionResponse, status_code=status.HTTP_200_OK)
async def get_prd_section(
    project_id: str,
    section_key: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> PrdSectionResponse:
    """Fetch one PRD part (including its lock + review metadata)."""
    await _validate_project(project_id)
    section = await PRDSectionRepository.get_by_key(section_key, project_id, session)
    if section:
        return section
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    preview = preview_prd_sections(project_id, (state or {}).get("generated_prd") or "")
    match = next((item for item in preview if item["section_key"] == section_key), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"PRD section '{section_key}' not found")
    return match

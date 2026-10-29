import copy
import logging
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, require_project_owner
from app.database import get_db
from app.event_manager import event_manager
from app.models import PendingActionModel
from app.repositories import (
    ConversationMessageRepository,
    DocumentRepository,
    PendingActionRepository,
    RequirementStateRepository,
)
from app.review_service import governed_state_changed, mark_revised_if_approved
from app.schemas import (
    ActionStatusResponse,
    ArtifactLockRequest,
    ArtifactLockResponse,
    ConfirmActionResponse,
    ImpactAnalysisResponse,
    LockStatusResponse,
    PendingActionResponse,
)

logger = logging.getLogger("app.routes.lock")

router = APIRouter()

# Valid artifact types for the generic lock/unlock endpoints
VALID_ARTIFACT_TYPES = [
    "requirement",
    "prd_section",
]


# ==========================================
# GENERIC ARTIFACT LOCK / UNLOCK API
# ==========================================

# LOCK 4.1 — Lock one generic supported artifact.
# Trigger: LOCK 3.1 client call.
# Input: owner-scoped project, artifact type/UUID, caller owner label.
# Calls: LOCK 5.1 service → SYNC 8.2 publication.
# Branches: same-label re-lock → 200; different label → 409.
# Writes: artifact lock columns and best-effort event log.
# Failure: invalid IDs/type → 400; absent/wrong project → 404.
# Next: publish before request commit, so SYNC refresh may race persistence.
@router.post("/api/project/{project_id}/artifacts/{artifact_type}/{artifact_id}/lock", response_model=ArtifactLockResponse, status_code=status.HTTP_200_OK)
async def lock_artifact(
    project_id: str,
    artifact_type: str,
    artifact_id: str,
    payload: ArtifactLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> ArtifactLockResponse:
    """
    Generic lock endpoint for any artifact type.

    Supported types: requirement and prd_section.

    Args:
        project_id: Owning project UUID string.
        artifact_type: One of the supported artifact types.
        artifact_id: Artifact UUID string.
        payload: Optional locking identity and reason.
        session: Active asynchronous database session.

    Returns:
        ArtifactLockResponse: Confirmation with the artifact's lock metadata.

    Raises:
        HTTPException: 400 on invalid IDs or unsupported type; 409 when a
            different label holds the lock; 404 if absent/wrong project.
    """
    from app.lock_service import ArtifactLockError, LockService

    try:
        UUID(project_id)
        UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or artifact_id format")

    # Validate artifact type
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact_type. Must be one of: {', '.join(VALID_ARTIFACT_TYPES)}"
        )

    try:
        lock_info = await LockService.lock_artifact(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            session=session,
            locked_by=payload.locked_by or "user",
            project_id=project_id
        )
        await event_manager.publish(project_id, "artifact_locked", {
            "project_id": project_id,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
        })
        return {
            "status": "locked",
            "artifact": lock_info
        }
    except ArtifactLockError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# LOCK 4.2 — Unlock one generic supported artifact.
# Trigger: LOCK 3.1 client call; Calls: LOCK 5.2 → SYNC 8.2.
# Branches: already unlocked → 200; different nonempty label → 403;
#           ownerless lock → any supplied label may clear it.
# Writes: clears lock columns; publishes before request commit.
# Failure: invalid IDs/type → 400; absent/wrong project → 404.
@router.post("/api/project/{project_id}/artifacts/{artifact_type}/{artifact_id}/unlock", response_model=ArtifactLockResponse, status_code=status.HTTP_200_OK)
async def unlock_artifact(
    project_id: str,
    artifact_type: str,
    artifact_id: str,
    payload: ArtifactLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> ArtifactLockResponse:
    """
    Generic unlock endpoint for any artifact type.

    Args:
        project_id: Owning project UUID string.
        artifact_type: One of the supported artifact types.
        artifact_id: Artifact UUID string.
        payload: Optional unlocking identity.
        session: Active asynchronous database session.

    Returns:
        ArtifactLockResponse: Confirmation with the artifact's lock metadata.

    Raises:
        HTTPException: 400 on invalid IDs or unsupported artifact type; 409 if
            locked by a different user; 404 if the artifact is not found.
    """
    from app.lock_service import LockService

    try:
        UUID(project_id)
        UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or artifact_id format")

    # Validate artifact type
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact_type. Must be one of: {', '.join(VALID_ARTIFACT_TYPES)}"
        )

    try:
        lock_info = await LockService.unlock_artifact(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            session=session,
            unlocked_by=payload.locked_by or "user",
            project_id=project_id
        )
        await event_manager.publish(project_id, "artifact_unlocked", {
            "project_id": project_id,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
        })
        return {
            "status": "unlocked",
            "artifact": lock_info
        }
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# LOCK 4.3 — Read generic lock status through LOCK 5.3.
# Trigger: API consumer; no current SPA caller found.
# Writes: NONE. Failure: invalid/type → 400; absent/wrong project → 404.
@router.get("/api/project/{project_id}/artifacts/{artifact_type}/{artifact_id}/lock-status", response_model=LockStatusResponse, status_code=status.HTTP_200_OK)
async def get_artifact_lock_status(
    project_id: str,
    artifact_type: str,
    artifact_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> LockStatusResponse:
    """
    Get the lock status of any artifact.

    Args:
        project_id: Owning project UUID string.
        artifact_type: One of the supported artifact types.
        artifact_id: Artifact UUID string.
        session: Active asynchronous database session.

    Returns:
        LockStatusResponse: The artifact's lock metadata.

    Raises:
        HTTPException: 400 on invalid IDs or unsupported artifact type; 404 if
            the artifact is not found.
    """
    from app.lock_service import LockService

    try:
        UUID(project_id)
        UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or artifact_id format")

    # Validate artifact type
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact_type. Must be one of: {', '.join(VALID_ARTIFACT_TYPES)}"
        )

    try:
        lock_info = await LockService.get_lock_status(artifact_type, artifact_id, session, project_id=project_id)
        return lock_info
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==========================================
# PENDING ACTIONS API
# ==========================================

# CONFIRM 3.1 — LIST staged actions (CONFIRM 2.1 / 1.7). Auth: AUTH 7.5 ownership.
#             Only rows still WAITING_CONFIRMATION are returned (repository 5.2); the
#             response carries `full=True` proposed_changes so the panel can render
#             the draft without a second call. Before returning, 3.1.1 removes older
#             duplicate clarification-review drafts and retains the newest one.
@router.get("/api/pending-actions/{project_id}", response_model=List[PendingActionResponse], status_code=status.HTTP_200_OK)
async def get_pending_actions(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> List[PendingActionResponse]:
    """
    Retrieve all pending (awaiting-confirmation) actions for a project.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        List[PendingActionResponse]: Pending actions for the project.
    """
    # CONFIRM 3.1.1 — Older builds could create one clarification-review draft per answer/batch.
    # Only the newest combined draft is meaningful; remove stale duplicates as
    # part of the pending-review lifecycle while leaving every other draft type.
    removed = await PendingActionRepository.discard_clarification_review_drafts(
        project_id, session, keep_latest=True,
    )
    if removed:
        logger.info("Discarded %d superseded clarification review draft(s) for project %s", len(removed), project_id)
    actions = await PendingActionRepository.get_by_project(project_id, session)
    return actions


# IMPACT 4.1 / CONFIRM 3.2 cross-reference — GET impact preview for one staged action
# (IMPACT 2.0 → 3.1), then return to the CONFIRM decision.
#             Auth: AUTH 7.5. Branches: 400 invalid ids → 404 action not in this
#             project → CONFIRM expiry cleanup → ImpactService 5.1.
#             The analysis is read-only, but an expired row is deleted and committed
#             before 410. Cross-project action ids remain invisible and return 404.
@router.get("/api/pending-actions/{action_id}/impact", response_model=ImpactAnalysisResponse, status_code=status.HTTP_200_OK)
async def get_action_impact(
    action_id: str,
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> ImpactAnalysisResponse:
    """
    Requirement Impact Analysis for a pending merge action (READ-ONLY).

    Diffs the draft's proposed requirement state against the stored state and
    derives every downstream artifact impacted by the change — user stories,
    acceptance criteria, PRD sections and diagrams that reference the affected
    ``REQ-``/``US-`` codes — so the merge window can show the user what
    changes and what is impacted BEFORE they decide to Save or Cancel.

    Args:
        action_id: Pending-action UUID string.
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        ImpactAnalysisResponse: Change predictions + impacted artifacts.

    Raises:
        HTTPException: 400 on invalid IDs; 404 if the action does not exist.
    """
    from app.impact_service import ImpactService

    try:
        UUID(project_id)
        UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or action_id format")

    # IMPACT 7.1 — Read the project-scoped pending-action collection; a foreign
    # action id stays indistinguishable from a missing one.
    actions = await PendingActionRepository.get_by_project(project_id, session)
    action = next((a for a in actions if a["id"] == action_id), None)
    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")

    expires_at = action.get("expires_at")
    # CONFIRM expiry cleanup at IMPACT 4.1 — consume the unusable pending row,
    #                 commit that deletion immediately, and return 410 without diffing.
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                await PendingActionRepository.delete(action_id, project_id, session)
                await session.commit()
                raise HTTPException(
                    status_code=410,
                    detail="This preview expired. Generate a new preview before saving.",
                )
        except HTTPException:
            raise
        except (TypeError, ValueError):
            logger.warning("Pending action %s has an invalid expires_at value: %r", action_id, expires_at)

    analysis = await ImpactService.analyze_pending_action(action, project_id, session)
    return analysis


# CONFIRM 3.3 — Save boundary for a pending action (CONFIRM 2.3 / UI 1.2 or 1.8).
#             The row itself is already persisted; this route decides whether its
#             proposed project changes may become authoritative.
#             Branch map:
#               3.3.1 UUID validation + row-locked, project-scoped action lookup
#               3.3.2 expiry gate → delete + commit + 410
#               3.3.3 ARCHIVE parent-lock recheck → 409 while draft remains
#               3.3.4 empty proposal → 400; attempted deletion rolls back with error
#               3.3.5a GENERATE_PRD → ARCHITECT 9.0 commit/version/event branch
#               3.3.5b other actions → stale-revision guard + requirement-state save
#               3.3.6 post-save invalidation, legacy section sync, governance status
#               3.3.7 document-extraction bookkeeping when applicable
#               3.3.8 consume action; 3.3.9 publish merge_confirmed; 3.3.10 respond
@router.post("/api/confirm-action/{action_id}", response_model=ConfirmActionResponse, status_code=status.HTTP_200_OK)
async def confirm_action(
    action_id: str,
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> ConfirmActionResponse:
    """
    Confirm a pending merge action, applying its proposed changes to the database.

    Args:
        action_id: Pending-action UUID string.
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        ConfirmActionResponse: The persisted, merged requirement state.

    Raises:
        HTTPException: 400 on invalid project ID or missing proposed changes;
            404 if the action does not exist.
    """
    try:
        project_uuid = UUID(project_id)
        action_uuid = UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or action_id format")
    # CONFIRM 3.3.1 — Validate both identifiers, then serialize confirmation of this
    # exact project-scoped WAITING_CONFIRMATION row. On PostgreSQL, a second
    # request waits here and then observes that the first transaction consumed
    # the row, so one preview can create at most one immutable PRD version.
    action_model = (await session.execute(
        select(PendingActionModel)
        .where(
            PendingActionModel.id == action_uuid,
            PendingActionModel.project_id == project_uuid,
            PendingActionModel.status == "WAITING_CONFIRMATION",
        )
        .with_for_update()
    )).scalar_one_or_none()
    if action_model is None:
        raise HTTPException(status_code=404, detail="Pending action not found")
    from app.repositories.base import serialize_pending_action

    action = serialize_pending_action(action_model, full=True)

    expires_at = action.get("expires_at")
    # CONFIRM 3.3.2 — An expired preview cannot be saved. Delete and commit its row,
    #                 then return 410 so the client must generate a fresh preview.
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                await PendingActionRepository.delete(action_id, project_id, session)
                await session.commit()
                raise HTTPException(
                    status_code=410,
                    detail="This preview expired. Generate a new preview before saving.",
                )
        except HTTPException:
            raise
        except (TypeError, ValueError):
            logger.warning("Pending action %s has an invalid expires_at value: %r", action_id, expires_at)

    # CONFIRM 3.3.3 — Archive previews are checked again here because an artifact may have been
    # locked after the preview was created. Never silently skip a protected
    # target while reporting the confirmation as successful.
    if action.get("action_type") == "ARCHIVE":
        from app.models import AcceptanceCriteriaModel, RequirementModel, UserStoryModel

        affected_codes = {
            str(code).strip().upper()
            for code in (action.get("affected_user_story_ids") or [])
            if str(code).strip()
        }
        requirement_codes = {code for code in affected_codes if code.startswith("REQ-")}
        story_codes = {code for code in affected_codes if code.startswith("US-")}
        criterion_ids = {
            str(criterion_id).strip()
            for criterion_id in (action.get("affected_acceptance_criteria_ids") or [])
            if str(criterion_id).strip()
        }
        locked_items: set[str] = set()

        if requirement_codes:
            locked_requirements = await session.execute(
                select(RequirementModel.requirement_code).where(
                    cast(RequirementModel.project_id, String) == str(project_id),
                    RequirementModel.requirement_code.in_(requirement_codes),
                    RequirementModel.is_locked.is_(True),
                )
            )
            locked_items.update(str(code) for code in locked_requirements.scalars().all())

        if story_codes:
            story_lock_rows = await session.execute(
                select(
                    RequirementModel.requirement_code,
                )
                .select_from(UserStoryModel)
                .join(
                    RequirementModel,
                    cast(UserStoryModel.requirement_id, String)
                    == cast(RequirementModel.id, String),
                )
                .where(
                    cast(RequirementModel.project_id, String) == str(project_id),
                    UserStoryModel.ticket_code.in_(story_codes),
                    RequirementModel.is_locked.is_(True),
                )
            )
            locked_items.update(str(code) for code in story_lock_rows.scalars().all())

        if criterion_ids:
            criterion_parent_locks = await session.execute(
                select(RequirementModel.requirement_code)
                .join(
                    UserStoryModel,
                    cast(UserStoryModel.requirement_id, String)
                    == cast(RequirementModel.id, String),
                )
                .join(
                    AcceptanceCriteriaModel,
                    cast(AcceptanceCriteriaModel.user_story_id, String)
                    == cast(UserStoryModel.id, String),
                )
                .where(
                    cast(RequirementModel.project_id, String) == str(project_id),
                    cast(AcceptanceCriteriaModel.id, String).in_(criterion_ids),
                    RequirementModel.is_locked.is_(True),
                )
            )
            locked_items.update(
                str(code) for code in criterion_parent_locks.scalars().all()
            )

        if locked_items:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Archive blocked because these items are locked: "
                    f"{', '.join(sorted(locked_items))}. Unlock them before confirming."
                ),
            )

    # CONFIRM 3.3.4 — Extract the proposal. Empty content is rejected with 400. The
    #                 delete below is flushed, but get_db rolls the transaction back
    #                 with the HTTPException, so the pending row remains in practice.
    proposed_changes = action.get("proposed_changes", {})
    if not proposed_changes:
        await PendingActionRepository.delete(action_id, project_id, session)
        raise HTTPException(status_code=400, detail="No proposed changes found in pending action")

    # CONFIRM 3.3.5a / ARCHITECT 9.0 — Confirm a staged GENERATE_PRD preview.
    # Guards: compare source requirements_revision and base PRD version with current
    # state (409 when stale), then require nonempty candidate content. As in 3.3.4,
    # the attempted deletion for empty content rolls back with the 400 response.
    # Writes: ARCHITECT 5.1 section synchronization, immutable PRD version, canonical
    # document/diagram/narrative/current workflow state, pending-action deletion, and
    # prd_confirmed event. This is the first governed write in the explicit UI flow.
    if action.get("action_type") == "GENERATE_PRD":
        latest_state = await RequirementStateRepository.get_by_project_id(project_id, session) or {}
        expected_requirements_revision = int(
            proposed_changes.get("source_requirements_revision") or 1
        )
        expected_prd_version = int(proposed_changes.get("base_prd_version") or 1)
        actual_requirements_revision = int(latest_state.get("requirements_revision") or 1)
        actual_prd_version = int(latest_state.get("version_number") or 1)
        if actual_requirements_revision != expected_requirements_revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This PRD preview is outdated because the requirements changed. "
                    "Discard it and generate a new preview."
                ),
            )
        if actual_prd_version != expected_prd_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This PRD preview is outdated because the saved PRD changed. "
                    "Discard it and generate a new preview."
                ),
            )

        generated_prd = str(proposed_changes.get("generated_prd") or "").strip()
        if not generated_prd:
            await PendingActionRepository.delete(action_id, project_id, session)
            raise HTTPException(status_code=400, detail="The PRD preview has no document content")

        from app.prd_section_service import sync_sections_from_prd
        from app.version_service import record_prd_version

        stitched_prd = await sync_sections_from_prd(
            project_id,
            generated_prd,
            session,
            changed_by="automated_agent",
        )
        final_prd = stitched_prd or generated_prd
        version_record = await record_prd_version(
            project_id,
            session,
            generated_prd=final_prd,
            generated_by="AI Assistance",
            change_type="ai",
            change_summary="Approved generated PRD preview.",
        )
        prd_narrative = copy.deepcopy(proposed_changes.get("prd_narrative") or {})
        prd_narrative["_generation"] = {
            "source_requirements_revision": expected_requirements_revision,
        }
        persisted_state = await RequirementStateRepository.save_or_update(
            project_id,
            {
                "generated_prd": final_prd,
                "generated_diagrams": proposed_changes.get("generated_diagrams") or "",
                "prd_narrative": prd_narrative,
                "current_workflow_state": "COMPLETED",
                "version_number": version_record["version_number"],
            },
            session,
        )
        await PendingActionRepository.delete(action_id, project_id, session)
        await event_manager.publish(project_id, "prd_confirmed", {
            "project_id": project_id,
            "action_id": action_id,
            "version_number": version_record["version_number"],
        })
        logger.info(
            "[PRD CONFIRM] Approved preview %s as version %s for project %s",
            action_id,
            version_record["version_number"],
            project_id,
        )
        # CONFIRM 3.3.10 — Return the saved state. The PRD branch additionally returns
        #                  its immutable ledger SemVer; no cascade job is required.
        return {
            "status": "confirmed",
            "requirement_state": persisted_state,
            "prd_semver": version_record.get("semver"),
            "cascade_job_id": None,
            "cascade_status": "not_required",
        }

    # CONFIRM 3.3.5b — Apply a non-GENERATE_PRD proposal to requirement state.
    logger.info(f"[MERGE CONFIRM] Persisting merged state for project {project_id}...")
    # Reuses the PROJECT 8.2.2 repository step: create-if-missing,
    #               else partial update that rebuilds the child tables from the lists
    #               supplied in the draft. Committed by the request's get_db on success.
    previous_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    requirement_merge = action.get("workflow_stage") != "COMPLETED"
    clarification_review_merge = action.get("workflow_stage") == "clarification_gatherer_review"
    cascade_job_id = None
    cascade_status = "not_required"
    updates_to_persist = dict(proposed_changes)
    if requirement_merge:
        # New requirement drafts remember the revision they were built from.
        # Rejecting a stale full-state draft prevents it from silently replacing
        # requirements that were confirmed in another tab after this preview.
        # The conditional keeps older, already-created pending actions usable.
        base_revision = updates_to_persist.pop("_base_requirements_revision", None)
        previous_revision = int((previous_state or {}).get("requirements_revision") or 1)
        if base_revision is not None and int(base_revision) != previous_revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This requirement update is outdated because the requirements changed. "
                    "Discard it and create a new update preview."
                ),
            )
        # Older pending actions contain ``version_number`` from the time that
        # requirements and PRD releases shared one counter.  Never let that
        # legacy field advance the PRD version during a requirements-only save.
        updates_to_persist.pop("version_number", None)
        updates_to_persist["requirements_revision"] = max(
            int(updates_to_persist.get("requirements_revision") or 0),
            previous_revision + 1,
        )
    persisted_state = await RequirementStateRepository.save_or_update(project_id, updates_to_persist, session)
    # CONFIRM 3.3.6 — Post-save reconciliation for the non-GENERATE_PRD branch.
    # CONFIRM 3.3.6.1 — For ordinary requirement merges, invalidate changed dependents
    #                   and remove every stale GENERATE_PRD preview. Clarification-
    #                   review merges intentionally skip this dependency pass.
    if requirement_merge and not clarification_review_merge:
        from app.dependency_graph_service import (
            detect_changed_artifacts,
            mark_dependents_for_review,
        )

        changed_artifacts = detect_changed_artifacts(previous_state, persisted_state)
        if changed_artifacts:
            # A changed source invalidates its previous audit verdict and every
            # linked PRD section. Sections remain in draft/review state until
            # the user explicitly selects Generate PRD and approves its preview.
            persisted_state = await RequirementStateRepository.save_or_update(
                project_id,
                {"validation_status": "pending"},
                session,
            )
            await mark_dependents_for_review(project_id, changed_artifacts, session)

        # Every confirmed requirement merge advances requirements_revision,
        # including narrative-only document extraction. Therefore every older
        # PRD preview is obsolete even when dependency detection finds no story
        # or criterion change. Remove it now so the UI cannot keep displaying
        # stale blank narrative rows.
        for pending in await PendingActionRepository.get_by_project(project_id, session):
            if pending.get("action_type") == "GENERATE_PRD":
                await PendingActionRepository.delete(pending["id"], project_id, session)

    # CONFIRM 3.3.6.2 — Legacy COMPLETED-stage proposals may carry generated PRD
    # content outside the dedicated GENERATE_PRD action. Materialize their sections
    # fail-soft; this compatibility branch does not append a PRD version.
    # Architect output is staged without touching the section/version stores.
    # Materialize its sections only at this confirmation boundary so Cancel
    # truly leaves all governed PRD artifacts unchanged.
    if (
        action.get("workflow_stage") == "COMPLETED"
        and isinstance(proposed_changes, dict)
        and (proposed_changes.get("generated_prd") or "").strip()
    ):
        try:
            from app.prd_section_service import sync_sections_from_prd

            stitched_prd = await sync_sections_from_prd(
                project_id,
                proposed_changes["generated_prd"],
                session,
                changed_by="automated_agent",
            )
            if stitched_prd:
                proposed_changes = {**proposed_changes, "generated_prd": stitched_prd}
                persisted_state = await RequirementStateRepository.save_or_update(
                    project_id,
                    {"generated_prd": stitched_prd},
                    session,
                )
        except Exception as section_err:
            # Keep the existing fail-soft behavior: a section bookkeeping
            # problem must not discard a confirmed, exportable document.
            logger.error(
                "[MERGE CONFIRM] Failed to materialize staged PRD sections for project %s: %s",
                project_id,
                section_err,
            )
    # CONFIRM 3.3.6.3 — If governed content changed on an approved project, move its
    #                   governance status back to revised for a new review cycle.
    if governed_state_changed(previous_state, proposed_changes):
        await mark_revised_if_approved(
            project_id,
            session,
            reason="Approved requirements changed through a confirmed merge.",
            actor_id=current_user.id,
            actor_name=current_user.full_name,
            actor_role=current_user.role,
        )

    # 3b. Document-extraction merges (INSERT_CHUNKED_REQUIREMENTS): flip the
    # source document's extraction flag and append the audit-log entry. The
    # draft was created by POST .../documents/{id}/process; THIS is the only
    # point at which document-derived requirements ever reach the DB.
    # CONFIRM 3.3.7 — Document-extraction bookkeeping (only for drafts staged by the
    #               DOCUMENT EXTRACTION flow, identified by `_document_ref` in the
    #               draft): flip uploaded_documents.extraction_status to
    #               "extraction_applied" and append an artifact_event_logs entry.
    #               Fail-soft: a logging failure never blocks the confirmed merge.
    document_ref = proposed_changes.get("_document_ref") or {}
    if action.get("action_type") == "INSERT_CHUNKED_REQUIREMENTS" and document_ref.get("document_id"):
        from app.repositories import ArtifactEventLogRepository, DocumentRepository

        await DocumentRepository.update_extraction_status(
            str(document_ref["document_id"]), project_id, "extraction_applied", session
        )
        try:
            await ArtifactEventLogRepository.log_event(
                artifact_type="uploaded_document",
                artifact_id=str(document_ref["document_id"]),
                action="CREATE",
                session=session,
                old_value=None,
                new_value={
                    "document_filename": document_ref.get("document_filename"),
                    "pending_action_id": action_id,
                    "applied_version": persisted_state.get("version_number"),
                },
                performed_by="user",
                project_id=project_id,
            )
        except Exception as log_err:  # never block a confirmed merge on logging
            logger.warning(f"[MERGE CONFIRM] Failed to log document merge event: {log_err}")

    # A confirmed requirements merge is recorded by the requirement/audit
    # history, but it is not a PRD release.  Do not append a prd_versions row
    # or change SemVer until PRD content is generated or edited.

    # CONFIRM 3.3.8 — The action row is DELETED, never marked "applied": the pending
    #               action carries the only copy of the draft, and its absence is what
    #               tells the UI the gate is closed (3.4 does the same on Cancel).
    # 4. Delete the pending action
    await PendingActionRepository.delete(action_id, project_id, session)

    # Saving requirements deliberately stops here. It invalidates affected
    # downstream artifacts but never starts PRD generation; the explicit
    # Generate PRD action owns generation, preview, and approval.

    # CONFIRM 3.3.9 — Real-time fan-out: other clients (and this one, debounced)
    #               re-read the project state (EVENTS flow).
    await event_manager.publish(project_id, "merge_confirmed", {
        "project_id": project_id,
        "action_id": action_id,
        "cascade_job_id": cascade_job_id,
        "cascade_status": cascade_status,
    })

    logger.info(f"[MERGE CONFIRM] Merge confirmed and persisted for project {project_id}")
    # CONFIRM 3.3.10 — Return the authoritative requirement state and cascade fields
    #                  consumed by the Dashboard completion callback (CONFIRM 1.3).
    return {
        "status": "confirmed",
        "requirement_state": persisted_state,
        "cascade_job_id": cascade_job_id,
        "cascade_status": cascade_status,
    }


# CONFIRM 3.4 — Discard path (CONFIRM 2.4 / UI 1.4 or 1.8): find the project-scoped
#             waiting action, reset a document-extraction source to `not_extracted`
#             when applicable, delete the draft, then publish `action_cancelled`.
#             Proposed project changes are never applied, so no artifact-lock gate is
#             needed. A missing/cross-project action returns 404.
@router.post("/api/cancel-action/{action_id}", response_model=ActionStatusResponse, status_code=status.HTTP_200_OK)
async def cancel_action(
    action_id: str,
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> ActionStatusResponse:
    """
    Cancel (delete) a pending action without applying its proposed changes.

    Args:
        action_id: Pending-action UUID string.
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        ActionStatusResponse: Confirmation with status 'cancelled'.
    """
    actions = await PendingActionRepository.get_by_project(project_id, session)
    action = next((item for item in actions if item.get("id") == action_id), None)
    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")

    # Cancelling a document extraction removes its only draft, so its status
    # returns to knowledge-only. Confirming instead flips it to
    # extraction_applied in the confirmation branch above.
    document_ref = (action.get("proposed_changes") or {}).get("_document_ref") or {}
    if (
        action.get("action_type") == "INSERT_CHUNKED_REQUIREMENTS"
        and document_ref.get("document_id")
    ):
        await DocumentRepository.update_extraction_status(
            str(document_ref["document_id"]), project_id, "not_extracted", session
        )

    await PendingActionRepository.delete(action_id, project_id, session)
    # Persist the discard acknowledgement so an authoritative project reload (or
    # another tab receiving the event below) does not erase the message that the
    # frontend just displayed locally.
    await ConversationMessageRepository.save_message(
        project_id=project_id,
        role="system",
        message="Requirements update cancelled. No requirement changes were saved.",
        session=session,
        workflow_state="gatherer_node",
        intent="CANCEL_REQUIREMENT_UPDATE",
    )
    await event_manager.publish(project_id, "action_cancelled", {
        "project_id": project_id,
        "action_id": action_id,
    })
    return {"status": "cancelled"}

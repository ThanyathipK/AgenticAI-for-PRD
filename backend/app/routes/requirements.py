import copy
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import gather_clarification_answer_batch, prd_workflow
from app.auth import (
    AuthenticatedUser,
    get_current_user,
    require_project_owner,
    verify_project_access,
)
from app.config import settings
from app.database import get_db
from app.errors import classify_exception
from app.event_manager import event_manager
from app.impact_service import build_change_preview
from app.input_validation import validate_body_budget, validate_text_budget
from app.llm_client import (
    LMStudioGatewayError,
    LMStudioOutputParsingError,
    LMStudioUnavailableError,
)
from app.observability import LogContext, log_event
from app.rate_limit import rate_limit_dependency
from app.repositories import (
    AcceptanceCriteriaRepository,
    ClarificationQuestionRepository,
    ConversationMessageRepository,
    DocumentRepository,
    PendingActionRepository,
    PRDVersionRepository,
    RequirementRepository,
    RequirementStateRepository,
    UserStoryRepository,
)
from app.requirement_codes import (
    default_requirement_code,
    extract_requirement_codes,
    normalize_requirement_code,
)
from app.schemas import (
    AuditRespondResponse,
    IntentDetectorRequest,
    ProcessRequirementsRequest,
    ProcessRequirementsResponse,
    RequirementDetail,
    RequirementIntentDetectionResult,
    RequirementLockRequest,
    RequirementLockResponse,
    RequirementMatcherRequest,
    RequirementMatcherResult,
    RequirementStateResponse,
    UserAnswerSubmit,
    WorkflowRouterRequest,
    WorkflowRoutingResult,
)
from app.semantic_memory import extract_and_remember
from app.services.requirement_processing_service import RequirementProcessingService
from app.workflow_cancellation import workflow_cancellations

logger = logging.getLogger("app.routes.requirements")

router = APIRouter()


def _is_requirement_restore_command(value: str) -> bool:
    """Recognise an explicit governed reactivation request, never a discussion."""
    return bool(re.match(
        r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
        r"(?:restore|reopen|reactivate|unarchive)\b",
        value or "",
        flags=re.IGNORECASE,
    ))


def _ensure_requirement_ids(reqs: Any) -> List[Dict[str, Any]]:
    """Guarantee every requirement entry satisfies ``RequirementDetail``.

    The multi-agent workflow (and the legacy ``epic_name`` conversion) rebuild
    requirement dicts from the frontend's ``structured_requirements`` payload,
    which does not carry database identity. ``RequirementDetail.id`` (and
    ``requirement_code``/``title``) are REQUIRED response fields, so a missing
    ``id`` failed FastAPI response validation and surfaced as an opaque HTTP
    500 ("PRD Generation Failed — No Document Saved") in the UI. Falling back
    to ``requirement_code`` (or a fresh UUID) keeps the API contract intact.
    """
    if not isinstance(reqs, list):
        return []
    safe: List[Dict[str, Any]] = []
    for entry in reqs:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict requirement entry in process-requirements response: %r", entry)
            continue
        item = dict(entry)
        if not item.get("id"):
            item["id"] = item.get("requirement_code") or str(uuid4())
        if not item.get("requirement_code"):
            item["requirement_code"] = default_requirement_code(len(safe))
        if item.get("title") is None:
            item["title"] = ""
        safe.append(item)
    return safe


async def _stage_requirement_reactivation(
    project_id: str,
    raw_input: str,
    req_state: Dict[str, Any],
    session: AsyncSession,
) -> Dict[str, Any]:
    """Stage archived REQ records and their stored hierarchy for confirmation."""
    requested_codes = extract_requirement_codes(raw_input)
    if not requested_codes:
        return {
            "status": "no_change",
            "detected_intent": "UPDATE_REQUIREMENT",
            "message": "Specify the archived requirement code to restore, for example: Restore REQ-001.",
            "pending_merge": False,
            "pending_action_id": None,
            "no_changes": True,
        }

    all_requirements = await RequirementRepository.get_by_project(project_id, session)
    by_code = {
        normalize_requirement_code(item.get("requirement_code")): item
        for item in all_requirements
        if item.get("requirement_code")
    }
    missing = [code for code in requested_codes if code not in by_code]
    if missing:
        return {
            "status": "no_change",
            "detected_intent": "UPDATE_REQUIREMENT",
            "message": f"I couldn't restore {', '.join(missing)} because it does not exist in this project. No changes were made.",
            "pending_merge": False,
            "pending_action_id": None,
            "no_changes": True,
        }

    active = [code for code in requested_codes if str(by_code[code].get("status") or "active").lower() == "active"]
    archived = [code for code in requested_codes if str(by_code[code].get("status") or "active").lower() == "archived"]
    unavailable = [code for code in requested_codes if code not in active and code not in archived]
    if active or unavailable:
        details = []
        if active:
            details.append(f"already active: {', '.join(active)}")
        if unavailable:
            details.append(f"not restorable from its current status: {', '.join(unavailable)}")
        return {
            "status": "no_change",
            "detected_intent": "UPDATE_REQUIREMENT",
            "message": f"No changes were made ({'; '.join(details)}).",
            "pending_merge": False,
            "pending_action_id": None,
            "no_changes": True,
        }

    targets = [by_code[code] for code in archived]
    locked = [item["requirement_code"] for item in targets if item.get("is_locked")]
    target_ids = [UUID(str(item["id"])) for item in targets]
    stories_by_requirement = await UserStoryRepository.get_by_requirement_ids(target_ids, session)
    criteria_by_requirement = await AcceptanceCriteriaRepository.get_by_requirement_ids(
        target_ids, project_id, session,
    )

    restored_requirements: List[Dict[str, Any]] = []
    for requirement in targets:
        requirement_id = UUID(str(requirement["id"]))
        stories = stories_by_requirement.get(requirement_id, [])
        criteria = criteria_by_requirement.get(requirement_id, [])
        criteria_by_story: Dict[str, List[str]] = {}
        for criterion in criteria:
            criteria_by_story.setdefault(str(criterion.get("ticket_code") or ""), []).append(
                str(criterion.get("criteria_text") or "")
            )
        restored_stories = []
        for story in stories:
            restored_story = {**story, "status": "active"}
            restored_story["acceptance_criteria"] = criteria_by_story.get(
                str(story.get("ticket_code") or ""), []
            )
            restored_stories.append(restored_story)
        restored_requirements.append({
            **requirement,
            "status": "active",
            "user_stories": restored_stories,
        })

    if locked:
        return {
            "status": "locked",
            "detected_intent": "UPDATE_REQUIREMENT",
            "message": f"No changes were made because {', '.join(sorted(set(locked)))} or one of its child stories is locked. Unlock it before restoring it.",
            "pending_merge": False,
            "pending_action_id": None,
            "no_changes": True,
        }

    proposal = copy.deepcopy(req_state)
    proposal["requirements"] = [
        *copy.deepcopy(req_state.get("requirements") or []),
        *restored_requirements,
    ]
    proposal["user_stories"] = [
        story
        for requirement in proposal["requirements"]
        for story in (requirement.get("user_stories") or [])
    ]
    proposal["acceptance_criteria"] = [
        criterion
        for story in proposal["user_stories"]
        for criterion in (story.get("acceptance_criteria") or [])
    ]
    proposal["validation_status"] = "pending"
    proposal["current_workflow_state"] = "REACTIVATION_REVIEW"
    proposal["_base_requirements_revision"] = int(req_state.get("requirements_revision") or 1)

    pending_action = await PendingActionRepository.create(
        project_id=project_id,
        data={
            "action_type": "MERGE",
            "original_user_message": raw_input,
            "proposed_changes": proposal,
            "workflow_stage": "reactivation_review",
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        session=session,
    )
    codes = ", ".join(archived)
    return {
        "status": "draft_ready",
        "detected_intent": "UPDATE_REQUIREMENT",
        "message": f"Restore preview ready for {codes}. Its original code and stored requirement hierarchy will be reactivated after you select Save. Validation will need to run again.",
        "structured_requirements": {
            "epic_name": proposal.get("epic_name") or "",
            "version": proposal.get("version_number") or 1,
            "user_stories": proposal["user_stories"],
            "requirements": _ensure_requirement_ids(proposal["requirements"]),
        },
        "pending_merge": True,
        "pending_action_id": pending_action.get("id"),
        "no_changes": False,
    }


# ==========================================
# REQUIREMENT CRUD API
# ==========================================

@router.get("/api/project/{project_id}/requirements", response_model=List[RequirementDetail], status_code=status.HTTP_200_OK)
async def get_project_requirements(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> List[RequirementDetail]:
    """
    Retrieve all requirements for a project, including lock status.

    Args:
        project_id: Project UUID string.
        session: Active asynchronous database session.

    Returns:
        List[RequirementDetail]: All requirement records for the project.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID.
    """
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    requirements = await RequirementRepository.get_by_project(project_id, session)
    return requirements


@router.get("/api/project/{project_id}/requirements/{requirement_id}", response_model=RequirementDetail, status_code=status.HTTP_200_OK)
async def get_requirement(
    project_id: str,
    requirement_id: str,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db),
) -> RequirementDetail:
    """
    Retrieve a single requirement by ID, including lock status.

    Args:
        project_id: Project UUID string.
        requirement_id: Requirement UUID string.
        session: Active asynchronous database session.

    Returns:
        RequirementDetail: The requested requirement record.

    Raises:
        HTTPException: 400 if either ID is not a valid UUID; 404 if not found.
    """
    try:
        UUID(project_id)
        UUID(requirement_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or requirement_id format")

    requirement = await RequirementRepository.get_by_id(requirement_id, project_id, session)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return requirement


# ==========================================
# REQUIREMENT LOCK / UNLOCK API
# ==========================================

# LOCK 4.4 — Legacy requirement-specific lock route. It bypasses LOCK 5.x and
#            calls repository 7.1. Re-lock returns 200 even when another label
#            already owns the row; the existing metadata remains unchanged.
#            No SPA caller was found (LOCK 2.x uses generic route 4.1).
@router.post("/api/project/{project_id}/requirements/{requirement_id}/lock", response_model=RequirementLockResponse, status_code=status.HTTP_200_OK)
async def lock_requirement(
    project_id: str,
    requirement_id: str,
    payload: RequirementLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> RequirementLockResponse:
    """
    Lock a requirement so it cannot be updated, deleted, merged, or modified by AI.

    Args:
        project_id: Project UUID string.
        requirement_id: Requirement UUID string.
        payload: Optional locking identity and reason.
        session: Active asynchronous database session.

    Returns:
        RequirementLockResponse: Confirmation with the locked requirement record.

    Raises:
        HTTPException: 400 if either ID is not a valid UUID; 404 if the
            requirement does not exist.
    """
    try:
        UUID(project_id)
        UUID(requirement_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or requirement_id format")

    locked = await RequirementRepository.lock(
        requirement_id, project_id, session, locked_by=payload.locked_by or "user"
    )
    if not locked:
        raise HTTPException(status_code=404, detail="Requirement not found")
    await event_manager.publish(project_id, "requirement_locked", {
        "project_id": project_id,
        "requirement_id": requirement_id,
        "locked_by": payload.locked_by or "user",
    })
    return {
        "status": "locked",
        "requirement": locked
    }


# LOCK 4.5 — Legacy requirement-specific unlock through repository 7.2. It
#            requires exact supplied-label equality; unlike generic 3.2, a null
#            stored owner is not freely clearable. No SPA caller was found.
@router.post("/api/project/{project_id}/requirements/{requirement_id}/unlock", response_model=RequirementLockResponse, status_code=status.HTTP_200_OK)
async def unlock_requirement(
    project_id: str,
    requirement_id: str,
    payload: RequirementLockRequest,
    current_user: AuthenticatedUser = Depends(require_project_owner),
    session: AsyncSession = Depends(get_db)
) -> RequirementLockResponse:
    """
    Unlock a requirement so it can be modified again.

    Args:
        project_id: Project UUID string.
        requirement_id: Requirement UUID string.
        payload: Optional unlocking identity.
        session: Active asynchronous database session.

    Returns:
        RequirementLockResponse: Confirmation with the unlocked requirement record.

    Raises:
        HTTPException: 400 if either ID is not a valid UUID; 403 if locked by a
            different user; 404 if the requirement does not exist.
    """
    try:
        UUID(project_id)
        UUID(requirement_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or requirement_id format")

    try:
        unlocked = await RequirementRepository.unlock(
            requirement_id, project_id, session, unlocked_by=payload.locked_by or "user"
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not unlocked:
        raise HTTPException(status_code=404, detail="Requirement not found")
    await event_manager.publish(project_id, "requirement_unlocked", {
        "project_id": project_id,
        "requirement_id": requirement_id,
        "unlocked_by": payload.locked_by or "user",
    })
    return {
        "status": "unlocked",
        "requirement": unlocked
    }


# ==========================================
# AUDIT / CLARIFICATION API
# ==========================================

# AUDIT 3.0 — Answer a persisted compliance clarification raised by AUDIT 2.x.
#             Successful current audits are auto-persisted at AUDIT 2.8, so their
#             questions already have stable row IDs and require no Save/Merge step.
#             Branch map:
#               3.1 resolve the owning project from the question id and verify
#                   ownership (IDOR fix — writing by PK alone let any authenticated
#                   user answer any project's question; unknown id → 404, never 403)
#               3.2 project-scoped, LOCK-GATED repository update
#                   (user_answer + is_resolved=True; ArtifactLockError → 409)
#               3.3 session commit + SSE "clarification_answered"
#             PRECONDITION: question_id must identify a persisted, unlocked row owned
#             by the authenticated user's project.
@router.post("/api/audit/respond/{question_id}", response_model=AuditRespondResponse, status_code=status.HTTP_200_OK)
async def post_audit_resolution_reply(
    question_id: UUID,
    payload: UserAnswerSubmit,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AuditRespondResponse:
    """
    Submits official BA/PO answer to close an active compliance query roadblock.

    Persists the resolution answer and marks the clarification question as resolved.

    Args:
        question_id: Clarification-question UUID.
        payload: The official stakeholder resolution text.
        session: Active asynchronous database session.

    Returns:
        AuditRespondResponse: Confirmation with the resolution timestamp.

    Raises:
        HTTPException: 404 if the clarification question does not exist; 403 if
            the caller does not own the question's project; 409 if the question
            is locked.
    """
    from datetime import datetime, timezone

    logger.info(f"Updating clarification question {question_id} state with resolution.")

    # AUTHZ: the path carries ONLY the question id, so the owning project is
    # resolved first and the caller must own it. Writing by primary key alone
    # let any authenticated user resolve — and overwrite the answer of — any
    # other project's compliance question (the same IDOR class fixed on the
    # project-scoped routes). An unknown id is a 404, never a 403, so the
    # endpoint does not leak which question ids exist.
    # AUDIT 3.1 — Project resolution + ownership check (AUDIT 3.0 branch note).
    project_id = await ClarificationQuestionRepository.get_project_id(str(question_id), session)
    if project_id is None:
        raise HTTPException(status_code=404, detail=f"Clarification question {question_id} not found")
    await verify_project_access(str(project_id), current_user, session)

    # AUDIT 3.2 — Resolve the question through the project-scoped lock-gated writer.
    # Writes: user_answer and is_resolved=True; locked → 409, vanished → 404.
    updated = await ClarificationQuestionRepository.update(
        str(question_id),
        str(project_id),
        {"user_answer": payload.answer_text, "is_resolved": True},
        session,
    )
    if updated is None:
        # The question vanished between the ownership lookup and the write.
        raise HTTPException(status_code=404, detail=f"Clarification question {question_id} not found")

    # A direct answer is still audit evidence, not a fresh verdict. Keep the
    # validation invalid until Validate runs again, but do not leave a project
    # stuck in WAITING_CLARIFICATION after its final open question was answered.
    project_questions = await ClarificationQuestionRepository.get_by_project(
        str(project_id), session
    )
    unresolved = [
        question for question in project_questions
        if not question.get("is_resolved", False)
    ]
    await RequirementStateRepository.save_or_update(
        str(project_id),
        {
            "validation_status": "invalid",
            "current_workflow_state": (
                "WAITING_CLARIFICATION" if unresolved else "AUDIT_PENDING"
            ),
        },
        session,
    )

    # AUDIT 3.3 — Commit the answer, then publish clarification_answered after success.
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # Real-time fan-out removes the resolved question from other clients' forms.
    await event_manager.publish(str(project_id), "clarification_answered", {
        "project_id": str(project_id),
        "question_id": str(question_id),
        "is_resolved": True,
    })

    logger.info(f"Clarification question {question_id} resolved successfully.")

    return {
        "status": "success",
        "message": f"Answer to clarification question {question_id} has been logged and registered.",
        "resolved_at": updated.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "is_resolved": bool(updated.get("is_resolved")),
    }


def _append_clarification_evidence(
    proposed_requirements: List[Dict[str, Any]],
    submitted_envelopes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Preserve every stakeholder answer in its proposed requirement group.

    The Gatherer remains responsible for producing readable requirement edits,
    but a valid answer may never disappear merely because the model omitted it
    from ``proposed_requirements``.  A compact, deterministic decision ledger in
    the requirement description gives the Auditor and Architect authoritative
    evidence on the next run and makes the review proposal complete.
    """
    requirements = copy.deepcopy(proposed_requirements)
    if not requirements:
        return requirements

    def normalize(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    by_requirement: Dict[str, Dict[str, Any]] = {}
    story_to_requirement: Dict[str, Dict[str, Any]] = {}
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        for key in (requirement.get("requirement_code"), requirement.get("id")):
            if key:
                by_requirement[normalize(key)] = requirement
        for story in requirement.get("user_stories") or []:
            if not isinstance(story, dict):
                continue
            for key in (story.get("ticket_code"), story.get("id")):
                if key:
                    story_to_requirement[normalize(key)] = requirement

    fallback = next((item for item in requirements if isinstance(item, dict)), None)
    for item in submitted_envelopes:
        answer = str(item.get("answer") or "").strip()
        question = str(item.get("question") or "Clarification decision").strip()
        if not answer or not fallback:
            continue
        target_key = normalize(
            item.get("target_requirement_id") or item.get("target_user_story_id")
        )
        target = (
            by_requirement.get(target_key)
            or story_to_requirement.get(target_key)
            or fallback
        )
        description = str(target.get("description") or "").strip()
        # The exact answer is the durable idempotency key. Re-submitting an
        # already incorporated decision must not append duplicate paragraphs.
        if normalize(answer) in normalize(description):
            continue
        decision = f"Clarification decision — {question}: {answer}"
        target["description"] = f"{description}\n\n{decision}".strip()
    return requirements


# CLARIFY 3.0 — Answer intake for persisted Auditor/Matcher/Gatherer questions.
#             Reached from CLARIFY 1.3 via 2.1. Branch map:
#               3.1 validate project id + verify owner before project-state access
#               3.2 load state/questions; 404 when no requirement state exists
#               3.3 map stable-id (or legacy q-index) answers, resolve/dedupe questions
#               3.4 group answers and ask CLARIFY 5.1 Gatherer for a review proposal
#               3.5 write answer evidence and invalid/pending workflow status in-session
#               3.6 replace old review drafts; stage MERGE only for material changes
#               3.7 publish clarification_submitted; 3.8 return saved state + metadata
#             This route never runs the Auditor and has no artifact-lock gate. Answer
#             evidence and any pending proposal commit together when the request succeeds;
#             proposed requirement edits remain behind explicit CONFIRM approval.
#             Scope: only questions returned by CLARIFY 4.1 from persisted rows can be
#             answered here; request-local/transcript-only questions have no stable row.
@router.post("/api/clarification/submit", response_model=RequirementStateResponse, status_code=status.HTTP_200_OK)
async def post_clarification_submit(payload: Dict[str, Any], current_user: AuthenticatedUser = Depends(get_current_user), session: AsyncSession = Depends(get_db)) -> RequirementStateResponse:
    """
    Saves clarification answers and asks the Gatherer for one combined,
    reviewable requirement-change proposal. It never runs the Auditor.

    Args:
        payload: Dict containing ``project_id`` and an answer mapping keyed by
            stable question UUID (preferred) or legacy ``q-<idx>``.
        session: Active asynchronous database session.

    Returns:
        RequirementStateResponse: The updated requirement state.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 404 if the
            project does not exist.
    """
    project_id = payload.get("project_id")
    answers = payload.get("answers", {})

    # CLARIFY 3.1 — Validate the untyped payload's project id, then enforce ownership
    #               before reading questions or writing stakeholder evidence.
    if not isinstance(project_id, str):
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # AUTHZ: the caller must own the project before anything is read/written.
    await verify_project_access(str(project_id), current_user, session)

    # CLARIFY 3.2 — Load the authoritative state and its flattened persisted questions
    #               through CLARIFY 4.1/4.2. A project without state cannot be answered.
    req_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    if not req_state:
        raise HTTPException(status_code=404, detail="Project not found")

    # CLARIFY 3.3 — Stable question UUIDs are the primary contract. Keep q-<index> support for
    # older clients, but never depend on list position when an id is available.
    questions = req_state.get("clarification_questions", [])
    submitted_envelopes = []
    answered_by_fingerprint: Dict[tuple, str] = {}

    def question_fingerprint(question: Dict[str, Any]) -> tuple:
        return (
            " ".join(str(question.get("checklist_category") or "").lower().split()),
            " ".join(str(question.get("question_text") or "").lower().split()),
        )

    for idx, q in enumerate(questions):
        stable_key = str(q.get("id") or "")
        legacy_key = f"q-{idx}"
        answer = answers.get(stable_key) if stable_key else None
        if answer is None:
            answer = answers.get(legacy_key)
        if isinstance(answer, str) and answer.strip():
            q["user_answer"] = answer.strip()
            q["is_resolved"] = True
            answered_by_fingerprint[question_fingerprint(q)] = answer.strip()
            submitted_envelopes.append({
                "question_id": stable_key or legacy_key,
                "question": q.get("question_text"),
                "answer": answer.strip(),
                "provenance": q.get("provenance") or {"type": "System analysis"},
                "source_references": q.get("source_references") or [],
                "target_requirement_id": q.get("target_requirement_id") or q.get("target_user_story_id"),
                "target_user_story_id": q.get("target_user_story_id"),
                "requirement_context": q.get("requirement_context"),
            })

    # Older releases could persist the same Auditor question under more than
    # one audit anchor. A user answer explicitly resolves that same issue.
    # Merge duplicates by their user-visible category and wording. Auditor runs
    # can attach the same question to different internal targets; those anchors
    # must not make the user answer an identical question twice.
    for q in questions:
        fingerprint = question_fingerprint(q)
        if not q.get("is_resolved", False) and fingerprint in answered_by_fingerprint:
            q["user_answer"] = answered_by_fingerprint[fingerprint]
            q["is_resolved"] = True

    unique_questions: List[Dict[str, Any]] = []
    question_index_by_fingerprint: Dict[tuple, int] = {}
    duplicate_question_ids: List[str] = []
    for question in questions:
        fingerprint = question_fingerprint(question)
        existing_index = question_index_by_fingerprint.get(fingerprint)
        if existing_index is None:
            question_index_by_fingerprint[fingerprint] = len(unique_questions)
            unique_questions.append(question)
            continue
        existing = unique_questions[existing_index]
        if question.get("is_resolved") and not existing.get("is_resolved"):
            existing["is_resolved"] = True
            existing["user_answer"] = question.get("user_answer")
        if not (existing.get("target_requirement_id") or existing.get("target_user_story_id")):
            existing["target_requirement_id"] = question.get("target_requirement_id")
            existing["target_user_story_id"] = question.get("target_user_story_id")
        if not existing.get("source_references") and question.get("source_references"):
            existing["source_references"] = question.get("source_references")
            existing["provenance"] = question.get("provenance")
        if question.get("id"):
            duplicate_question_ids.append(str(question["id"]))
    questions = unique_questions
    for duplicate_question_id in duplicate_question_ids:
        await ClarificationQuestionRepository.delete(
            duplicate_question_id, str(project_id), session
        )

    if not submitted_envelopes:
        raise HTTPException(status_code=400, detail="At least one non-empty clarification answer is required")

    # Rebuild the deterministic evidence set from every resolved row, not only
    # this request's inputs. A later partial submission replaces the older
    # review draft, so omitting earlier answers here would silently remove them
    # from the replacement proposal before they were approved.
    decision_envelopes = [
        {
            "question_id": str(question.get("id") or ""),
            "question": question.get("question_text"),
            "answer": str(question.get("user_answer") or "").strip(),
            "target_requirement_id": (
                question.get("target_requirement_id")
                or question.get("target_user_story_id")
            ),
            "target_user_story_id": question.get("target_user_story_id"),
        }
        for question in questions
        if question.get("is_resolved") and str(question.get("user_answer") or "").strip()
    ]

    # CLARIFY 3.4 — Keep LM Studio calls small and coherent: group by requirement/story/topic,
    # then cap every call at four answer envelopes. Results are folded into one
    # draft and therefore one explicit confirmation action.
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in submitted_envelopes:
        group_key = str(item.get("target_requirement_id") or item.get("target_user_story_id") or "project")
        grouped.setdefault(group_key, []).append(item)

    current_requirements = copy.deepcopy(req_state.get("requirements") or [])
    proposed_by_code = {
        str(req.get("requirement_code")): req for req in current_requirements if isinstance(req, dict)
    }
    classifications: List[Dict[str, Any]] = []
    proposal_generation_warning = None
    for group_items in grouped.values():
        for start in range(0, len(group_items), 4):
            batch = group_items[start:start + 4]
            targets = {str(item.get("target_requirement_id") or "") for item in batch}
            context = [
                req for req in current_requirements
                if str(req.get("requirement_code")) in targets
                or any(str(story.get("ticket_code")) in targets for story in (req.get("user_stories") or []))
            ] or current_requirements
            try:
                logger.info(
                    "[CLARIFICATION][GATHERER][START] project=%s target=%s answers=%d",
                    project_id, sorted(targets), len(batch),
                )
                batch_result = await gather_clarification_answer_batch(batch, context)
                logger.info(
                    "[CLARIFICATION][GATHERER][DONE] project=%s classifications=%d proposed_requirements=%d",
                    project_id,
                    len(batch_result.get("classifications") or []),
                    len(batch_result.get("proposed_requirements") or []),
                )
            except Exception:
                # A local-model formatting failure must never discard stakeholder
                # answers. Save them as audit evidence and stage a clearly marked
                # no-change review rather than making the form look nonfunctional.
                logger.exception("Clarification Gatherer batch failed; retaining submitted answers")
                proposal_generation_warning = (
                    "Answers were saved in a deterministic requirement-change draft, but the Gatherer "
                    "could not improve its wording. Review the complete saved-answer proposal before approval."
                )
                classifications.extend({
                    "question_id": str(item["question_id"]),
                    "classification": "NO_CHANGE",
                    "target_requirement_id": item.get("target_requirement_id"),
                    "rationale": "No requirement change was proposed because the Gatherer response was unavailable.",
                } for item in batch)
                continue
            classifications.extend(batch_result.get("classifications") or [])
            for req in batch_result.get("proposed_requirements") or []:
                code = str(req.get("requirement_code") or "")
                if code:
                    proposed_by_code[code] = req
    # A Gatherer FOLLOW_UP classification is advisory for the review proposal.
    # Do not turn it into a clarification row during answer submission: answered
    # questions are complete, unanswered original questions stay untouched, and
    # only the next explicit Validate action may create new audit questions.
    req_state["clarification_questions"] = questions

    # CLARIFY 3.5 — Answers are evidence for a future audit, not proof that blocking findings
    # are fixed. Keep the project pending/invalid until a fresh Auditor run.
    unresolved = [q for q in req_state["clarification_questions"] if not q.get("is_resolved", False)]
    req_state["validation_status"] = "invalid"
    req_state["current_workflow_state"] = "WAITING_CLARIFICATION" if unresolved else "AUDIT_PENDING"

    # Write the answered rows through PROJECT 8.2.2 using the loaded full-state shape;
    # this may re-derive relational child rows, but carries the unchanged requirement
    # board. The request transaction commits only after draft staging/event publication
    # also succeed. This evidence write is not acceptance of Gatherer-proposed edits.
    saved_state = await RequirementStateRepository.save_or_update(project_id, req_state, session)

    from datetime import datetime, timedelta, timezone
    proposal_state = copy.deepcopy(saved_state)
    proposal_state["_base_requirements_revision"] = int(
        saved_state.get("requirements_revision") or 1
    )
    proposal_state["requirements"] = _append_clarification_evidence(
        list(proposed_by_code.values()), decision_envelopes
    )
    proposal_narrative = copy.deepcopy(saved_state.get("prd_narrative") or {})
    existing_decisions = list(proposal_narrative.get("clarification_decisions") or [])
    known_decisions = {
        (
            " ".join(str(item.get("question") or "").casefold().split()),
            " ".join(str(item.get("answer") or "").casefold().split()),
        )
        for item in existing_decisions if isinstance(item, dict)
    }
    for item in decision_envelopes:
        decision = {
            "question": item.get("question"),
            "answer": item.get("answer"),
            "target_requirement_id": (
                item.get("target_requirement_id")
                or item.get("target_user_story_id")
            ),
        }
        key = (
            " ".join(str(decision["question"] or "").casefold().split()),
            " ".join(str(decision["answer"] or "").casefold().split()),
        )
        if key not in known_decisions:
            known_decisions.add(key)
            existing_decisions.append(decision)
    proposal_narrative["clarification_decisions"] = existing_decisions
    proposal_state["prd_narrative"] = proposal_narrative
    proposal_state["user_stories"] = [
        story for req in proposal_state["requirements"] for story in (req.get("user_stories") or [])
    ]
    proposal_state["acceptance_criteria"] = [
        criterion for story in proposal_state["user_stories"]
        for criterion in (story.get("acceptance_criteria") or [])
    ]
    proposal_state["clarification_answer_classifications"] = classifications
    # CLARIFY 3.6 — Replace every older clarification-review draft. Create one MERGE
    #               pending action only when the Gatherer's full requirement proposal
    #               differs from the pre-answer requirements; otherwise no CONFIRM gate.
    removed_drafts = await PendingActionRepository.discard_clarification_review_drafts(
        project_id, session, keep_latest=False,
    )
    if removed_drafts:
        logger.info(
            "[CLARIFICATION][REVIEW] replaced %d older combined clarification draft(s) for project=%s",
            len(removed_drafts), project_id,
        )
    has_requirement_changes = (
        proposal_state["requirements"] != current_requirements
        or proposal_narrative != (saved_state.get("prd_narrative") or {})
    )
    pending_action = None
    if has_requirement_changes:
        pending_action = await PendingActionRepository.create(
            project_id=project_id,
            data={
                "action_type": "MERGE",
                "original_user_message": "Clarification answers",
                "proposed_changes": proposal_state,
                "workflow_stage": "clarification_gatherer_review",
            },
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            session=session,
        )
    saved_state["pending_action_id"] = pending_action.get("id") if pending_action else None
    saved_state["pending_merge"] = pending_action is not None
    saved_state["clarification_answer_classifications"] = classifications
    if proposal_generation_warning:
        saved_state["proposal_generation_warning"] = proposal_generation_warning
    elif not has_requirement_changes:
        rationales = [
            str(item.get("rationale") or "").strip()
            for item in classifications
            if str(item.get("rationale") or "").strip()
        ]
        saved_state["proposal_no_change_reason"] = (
            "Answers were saved, but no requirement fields changed. "
            + (" ".join(dict.fromkeys(rationales)) if rationales else
               "The Gatherer found no actionable requirement update in the submitted answers.")
        )
    # CLARIFY 3.7 — Publish saved workflow status and proposal metadata so other clients
    #               refresh their clarification/pending-action views (EVENTS flow).
    #               Publication occurs before get_db commits the request transaction.
    await event_manager.publish(project_id, "clarification_submitted", {
        "project_id": project_id,
        "validation_status": req_state.get("validation_status"),
        "current_workflow_state": req_state.get("current_workflow_state"),
        "pending_action_id": pending_action.get("id") if pending_action else None,
        "proposal_generation_warning": proposal_generation_warning,
    })
    # CLARIFY 3.8 — Response = saved evidence plus proposal metadata; CLARIFY 1.4/1.5
    #               reset resolved inputs, refresh question state, reload the project,
    #               and mirror the optional pending action. It is not a fresh verdict.
    return saved_state


# ==========================================
# GENERATION CANCELLATION API (Stop button)
# ==========================================

# JOB 4.5 — Cancel a legacy direct workflow task.
# Trigger: JOB 3.4 when no durable job id is known.
# Calls: JOB 5.5.2 registry cancellation.
# Writes: process-local task cancellation only; no durable cancellation row.
# Branches: live task → true; missing/completed task → false.
@router.post("/api/process-requirements/cancel", status_code=status.HTTP_200_OK)
def post_cancel_process_requirements(
    project_id: str = Query(..., description="Project whose in-flight generation should be terminated."),
    current_user: AuthenticatedUser = Depends(require_project_owner),
) -> Dict[str, Any]:
    """
    Cancel an in-flight multi-agent generation for a project (Stop button).

    The long-running ``/api/process-requirements`` handler registers its asyncio
    task in the cancellation registry at request start. This endpoint cancels
    that task, which raises ``asyncio.CancelledError`` at its current await
    point (including mid-LLM-call, aborting the outbound LM Studio request).
    The handler's cancellation path then skips every persistence step, so a
    stopped run saves NOTHING (no pending actions, no assistant messages, no
    SSE publishes).

    Returns ``{"cancelled": true}`` when a running generation was terminated,
    ``{"cancelled": false}`` when nothing was in flight (e.g. Stop pressed just
    after the run already completed).

    Deliberately NOT rate limited: stopping a runaway generation must always
    succeed.
    """
    cancelled = workflow_cancellations.cancel(str(project_id))
    if cancelled:
        logger.info(f"[CANCELLED] Termination requested for in-flight generation, project {project_id}")
    else:
        logger.info(f"[CANCELLED] No in-flight generation to cancel for project {project_id}")
    return {"project_id": str(project_id), "cancelled": cancelled}


# ==========================================
# MULTI-AGENT PROCESS REQUIREMENTS API
# ==========================================

# CHAT 2.1 — Accept the product's shared chat/agent processing request.
# Trigger: CHAT 1.3 POST /api/process-requirements.
# Input: processing payload, AUTH 7.1 identity, database session, and rate limit.
# Calls: RequirementProcessingService → _process_requirements_pipeline.
# Branches: CHAT 2.2 chooses CHAT 2.3 direct conversation or LangGraph processing.
# Writes: registers cancellation state and may persist conversation/agent results.
# Failure: authorization, rate-limit, budget, and workflow errors map to HTTP responses.
@router.post("/api/process-requirements", response_model=ProcessRequirementsResponse, status_code=status.HTTP_200_OK)
async def post_process_requirements(
    request: ProcessRequirementsRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW
        )
    ),
) -> ProcessRequirementsResponse:
    """
    Public entrypoint for the multi-agent workflow, with Stop-button support.

    Registers the running asyncio task under the project ID so a concurrent
    ``POST /api/process-requirements/cancel`` can hard-cancel it. On
    cancellation the pipeline's ``asyncio.CancelledError`` is converted into a
    ``{"status": "cancelled"}`` response and NO results are persisted. See
    :func:`_process_requirements_pipeline` for the full pipeline behavior.
    """
    service = RequirementProcessingService(
        pipeline=_process_requirements_pipeline,
        verify_access=verify_project_access,
    )
    response = await service.process(request, session, current_user)

    # CHAT 2.5 — Distill durable memory after completed product processing.
    # Trigger: RequirementProcessingService returns a non-cancelled response.
    # Input: project id, original user message, and returned assistant message.
    # Calls: CHAT 4.1 extract_and_remember.
    # Branches: blank input/reply or cancelled response → no memory call.
    # Writes: semantic-memory facts and embeddings when enabled and novel.
    # Failure: fail-open inside CHAT 4.1; memory cannot block the completed response.
    # Next: return ProcessRequirementsResponse to CHAT 1.4.
    reply = getattr(response, "message", None)
    user_message = (request.raw_input or "").strip()
    if user_message and reply and getattr(response, "status", None) != "cancelled":
        await extract_and_remember(
            project_id=str(request.project_id),
            user_message=user_message,
            assistant_reply=str(reply),
        )
    return response


async def _process_requirements_pipeline(
    request: ProcessRequirementsRequest,
    session: AsyncSession,
    current_user: AuthenticatedUser,
) -> ProcessRequirementsResponse:
    """
    Asynchronously invokes the LangGraph multi-agent workflow (prd_workflow)
    to process raw requirements, audit compliance, or build PRD & architectural diagrams.
    Loads and updates the shared RequirementState from Supabase (as single source of truth).

    Pipeline:
    1. Persist every user message before intent detection
    2. Detect intent (GENERAL_CHAT, REQUIREMENT_REQUEST)
    3. If GENERAL_CHAT: generate response with project context, persist, return (no LangGraph)
    4. If REQUIREMENT_REQUEST: proceed to LangGraph workflow
    5. Persist every assistant response

    Args:
        request: Processing payload (project ID, raw input, target agent,
            structured requirements, version context).
        session: Active asynchronous database session.

    Returns:
        ProcessRequirementsResponse: The workflow outcome, structured
            requirements, audit result, PRD/diagram content, and pending merge
            metadata.

    Raises:
        HTTPException: 413 if the request payload (raw input + structured
            requirements) exceeds ``MAX_CONTEXT_TOKENS``; 500 if the
            multi-agent workflow execution fails.
    """
    from datetime import datetime, timedelta, timezone

    run_id = uuid4().hex[:12]
    pipeline_started = time.perf_counter()
    log_context = LogContext(
        project_id=str(request.project_id),
        user_id=str(current_user.id),
        workflow=request.target_agent or "gatherer",
        run_id=run_id,
    )
    logger.info(
        "[WORKFLOW][%s][START] project=%s requested_agent=%s",
        run_id, request.project_id, request.target_agent or "gatherer",
    )

    # Finding #39: enforce MAX_CONTEXT_TOKENS on the incoming processing payload
    # (raw_input + structured_requirements) before spending any LLM budget.
    # Runs BEFORE the "Triggering on-demand agent" log so a rejected request
    # never claims an agent run was started.
    validate_body_budget(request.model_dump(), "Process-requirements request")
    logger.info("[WORKFLOW][%s][VALIDATE_INPUT][DONE]", run_id)

    logger.info(f"Triggering on-demand {request.target_agent} agent for project {request.project_id}")

    # ==========================================
    # STEP 1: Persist every user message before any intent detection or agent routing
    # ==========================================
    if (
        not request.client_persists_timeline
        and request.project_id
        and request.raw_input
        and request.raw_input.strip()
    ):
        await ConversationMessageRepository.save_message(
            project_id=str(request.project_id),
            role="user",
            message=request.raw_input.strip(),
            session=session,
            workflow_state=request.target_agent or "gatherer",
            intent="PENDING_DETECTION"
        )

    # ==========================================
    # INTENT 1.0 — Integrate intent detection into the shared processing pipeline.
    # Trigger: CHAT 2.1 pipeline after user-turn persistence and request validation.
    # Input: raw user text and confirmed project RequirementState.
    # Calls: INTENT 1.1 context phase → 1.2 service import → 1.3 detector call.
    # Branches: GENERAL_CHAT → CHAT 2.3; requirement intent → INTENT 1.4 mapping.
    # Writes: in-memory intent/confidence and target_agent; no intent-specific DB row.
    # Failure: INTENT 2.8 supplies a deterministic fallback classification.
    # ==========================================
    # INTENT 1.1 — Establish the pipeline detection phase before LangGraph.
    # Calls: RequirementState read supplies story context for INTENT 1.3.
    # Next: INTENT 1.2 service import and INTENT 1.3 invocation.
    # INTENT 1.2 — Import detector services locally to avoid eager LLM dependency loading.
    from app.semantic_service import detect_requirement_intent, generate_general_chat_response

    # Load the centralized RequirementState from Supabase (single source of truth)
    logger.info(f"[DB LOG] Loading RequirementState for processing project {request.project_id}")
    req_state = await RequirementStateRepository.get_by_project_id(request.project_id, session)
    persisted_version_at_start = req_state.get("version_number") if req_state else None
    persisted_requirements_revision_at_start = (
        int(req_state.get("requirements_revision") or 1) if req_state else 1
    )
    logger.info(f"[DB LOG] Loading RequirementState for processing project {request.project_id} complete. Found: {req_state is not None}")
    logger.info(
        "[WORKFLOW][%s][LOAD_STATE][DONE] found=%s version=%s",
        run_id, req_state is not None,
        req_state.get("version_number") if req_state else "none",
    )

    # Architect output must be built only from confirmed canonical state. A
    # document/gatherer draft is intentionally absent from req_state until the
    # user saves it; allowing Generate PRD here produced a plausible document
    # containing older fields while newly extracted fields (notably Problem
    # Statement) were missing. Enforce this on the server as well as the UI so
    # direct/API callers cannot generate from an incomplete revision.
    if request.target_agent == "architect":
        pending_requirement_actions = [
            action
            for action in await PendingActionRepository.get_by_project(
                request.project_id, session
            )
            if action.get("action_type") != "GENERATE_PRD"
        ]
        if pending_requirement_actions:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Save or discard the pending requirement changes before "
                    "generating a PRD. PRD generation uses confirmed project state only."
                ),
            )
        if not req_state or not (
            (req_state.get("requirements") or [])
            or str(req_state.get("generated_prd") or "").strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="There are no requirements to generate.",
            )

    # INTENT 1.3 — Classify pipeline input with current story context.
    # Trigger: confirmed RequirementState has been loaded and preflight guards pass.
    # Input: raw user text and current user stories.
    # Calls: INTENT 2.0 detect_requirement_intent.
    # Returns: intent, confidence, and reason for CHAT 2.2 and INTENT 1.4.
    # Writes: NONE; result remains in pipeline memory.
    detected_intent_result = await detect_requirement_intent(
        request.raw_input or "",
        req_state.get("user_stories", []) if req_state else []
    )
    # CHAT 2.2 — Select direct conversation or requirement processing.
    # Trigger: pipeline state and story context have been loaded.
    # Input: normalized result from the INTENT 2.x detector.
    # Branches: GENERAL_CHAT → CHAT 2.3; other intents → target-agent/LangGraph path.
    # Writes: in-memory detected_intent and confidence only.
    detected_intent = detected_intent_result.get("intent", "GENERAL_CHAT")
    # Propagated into the workflow so the Gatherer prompt can quote the real
    # classifier confidence (and hedge when the classification was uncertain)
    # instead of assuming a perfect 1.0 confidence.
    intent_confidence = float(detected_intent_result.get("confidence", 1.0) or 1.0)
    logger.info(f"[INTENT DETECTION] Detected intent: {detected_intent} (confidence: {detected_intent_result.get('confidence', 0.0)})")
    logger.info(
        "[WORKFLOW][%s][DETECT_INTENT][DONE] intent=%s confidence=%.3f",
        run_id, detected_intent, intent_confidence,
    )

    # Explicit reactivation is deterministic and must not be reconstructed by
    # the LLM: archived records are absent from normal Gatherer context. Stage
    # the original stored hierarchy through the same confirmation boundary as
    # every other governed requirement update.
    if _is_requirement_restore_command(request.raw_input or ""):
        if not req_state:
            return {
                "status": "no_change",
                "detected_intent": "UPDATE_REQUIREMENT",
                "message": "This project has no requirement state to restore.",
                "pending_merge": False,
                "pending_action_id": None,
                "no_changes": True,
            }
        restore_result = await _stage_requirement_reactivation(
            str(request.project_id), request.raw_input or "", req_state, session,
        )
        message = str(restore_result.get("message") or "")
        if not request.client_persists_timeline and message:
            await ConversationMessageRepository.save_message(
                project_id=str(request.project_id),
                role="assistant",
                message=message,
                session=session,
                workflow_state="reactivation_review",
                intent="UPDATE_REQUIREMENT",
            )
        await event_manager.publish(str(request.project_id), "workflow_update", {
            "project_id": str(request.project_id),
            "workflow_state": "REACTIVATION_REVIEW",
            "validation_status": req_state.get("validation_status"),
            "pending_action_id": restore_result.get("pending_action_id"),
            "target_agent": "gatherer",
        })
        return restore_result

    # An explicit story ID is authoritative. If the user asks to update/delete
    # an ID that does not exist, stop before the matcher/LLM and do not create a
    # meaningless pending merge containing the unchanged board.
    if detected_intent in {"UPDATE_REQUIREMENT", "DELETE_REQUIREMENT"}:
        from app.merge_service import normalize_ticket_code
        from app.requirement_codes import extract_requirement_codes, normalize_requirement_code
        from app.semantic_service import extract_story_ticket_codes

        requested_story_codes = extract_story_ticket_codes(request.raw_input or "")
        existing_story_codes = {
            normalize_ticket_code(story.get("ticket_code"))
            for story in ((req_state or {}).get("user_stories") or [])
            if isinstance(story, dict) and story.get("ticket_code")
        }
        requested_requirement_codes = extract_requirement_codes(request.raw_input or "")
        existing_requirement_codes = {
            normalize_requirement_code(requirement.get("requirement_code"))
            for requirement in ((req_state or {}).get("requirements") or [])
            if isinstance(requirement, dict) and requirement.get("requirement_code")
        }
        missing_story_codes = [
            code for code in requested_story_codes if code not in existing_story_codes
        ]
        missing_requirement_codes = [
            code for code in requested_requirement_codes if code not in existing_requirement_codes
        ]
        missing_codes = missing_story_codes + missing_requirement_codes
        if missing_codes:
            if missing_requirement_codes and not missing_story_codes:
                available = ", ".join(sorted(existing_requirement_codes)) or "none"
                available_label = "requirement IDs"
            elif missing_story_codes and not missing_requirement_codes:
                available = ", ".join(sorted(existing_story_codes)) or "none"
                available_label = "user story IDs"
            else:
                available = ", ".join(sorted(existing_requirement_codes | existing_story_codes)) or "none"
                available_label = "requirement and user story IDs"
            response_text = (
                f"I couldn't apply that change because {', '.join(missing_codes)} "
                f"does not exist in this project. Available {available_label}: {available}. "
                "No changes were made."
            )
            if not request.client_persists_timeline:
                await ConversationMessageRepository.save_message(
                    project_id=str(request.project_id),
                    role="assistant",
                    message=response_text,
                    session=session,
                    workflow_state="no_change",
                    intent=detected_intent,
                )
            return {
                "status": "no_change",
                "detected_intent": detected_intent,
                "message": response_text,
                "pending_merge": False,
                "pending_action_id": None,
                "no_changes": True,
            }

        # Locks are a precondition, not something discovered after an AI draft
        # has already opened a Save/Merge window. Explicit REQ/US targets can be
        # resolved deterministically here, before matcher or Gatherer work.
        requirements_by_code = {
            normalize_requirement_code(req.get("requirement_code")): req
            for req in ((req_state or {}).get("requirements") or [])
            if isinstance(req, dict) and req.get("requirement_code")
        }
        parent_by_story = {}
        for requirement in requirements_by_code.values():
            for story in requirement.get("user_stories") or []:
                if isinstance(story, dict) and story.get("ticket_code"):
                    code = normalize_ticket_code(story.get("ticket_code"))
                    parent_by_story[code] = requirement
        blocked = []
        for code in requested_requirement_codes:
            requirement = requirements_by_code.get(code) or {}
            if requirement.get("is_locked"):
                blocked.append(code)
        for code in requested_story_codes:
            if (parent_by_story.get(code) or {}).get("is_locked"):
                blocked.append(code)
        if blocked:
            blocked = sorted(set(blocked))
            response_text = (
                f"No changes were made because {', '.join(blocked)} is locked. "
                "Unlock it before trying to update or archive it."
            )
            if not request.client_persists_timeline:
                await ConversationMessageRepository.save_message(
                    project_id=str(request.project_id),
                    role="assistant",
                    message=response_text,
                    session=session,
                    workflow_state="locked",
                    intent=detected_intent,
                )
            return {
                "status": "locked",
                "detected_intent": detected_intent,
                "message": response_text,
                "pending_merge": False,
                "pending_action_id": None,
                "no_changes": True,
            }

    # ==========================================
    # CHAT 2.3 — Generate a direct conversational response outside LangGraph.
    # Trigger: CHAT 2.2 returns GENERAL_CHAT and CHAT 2.3.0 permits the branch.
    # Input: raw message, current project state, transcript, and knowledge chunks.
    # Calls: CHAT 2.3.1 history read → CHAT 2.3.2 generation → optional
    #        CHAT 2.3.3 persistence → CHAT 2.3.4 event publication.
    # Writes: conversation row/event only; no requirement, PRD, or version mutation.
    # Failure: history read fails loudly; knowledge/generation/persistence degrade
    #          according to their nested policies.
    # Next: CHAT 2.4 response → CHAT 1.4.
    # STEP 3: GENERAL_CHAT handling (bypass LangGraph entirely)
    # ==========================================
    # If the intent is GENERAL_CHAT (greeting, question, explanation request, concept query),
    # generate an LLM response directly without entering any agent workflow.
    # REQUIREMENT_REQUEST intents fall through to the LangGraph workflow below.
    #
    # IMPORTANT: Explicit on-demand agent requests (e.g. "Generate PRD" button sends
    # target_agent="architect" with empty raw_input) MUST bypass the GENERAL_CHAT
    # short-circuit. Otherwise the empty raw_input defaults to GENERAL_CHAT and the
    # architect/auditor workflow never runs.
    # CHAT 2.3.0 — Bypass rule: an explicit agent request (CHAT 1.1's PRD/Audit/Stop
    #              buttons send target_agent=architect|auditor|delete_requirement
    #              with empty raw_input) must NOT be swallowed by this branch, since
    #              an empty message always classifies as GENERAL_CHAT.
    on_demand_agents = ("architect", "auditor", "delete_requirement")
    if detected_intent == "GENERAL_CHAT" and request.target_agent not in on_demand_agents:
        logger.info("[WORKFLOW][%s][GENERAL_CHAT][START]", run_id)
        logger.info("[GENERAL_CHAT] Handling as general chat. Generating conversational response with project context.")

        # Load conversation history for context. This is a DB read that feeds the
        # chat's context — we fail loudly (HTTP 500) rather than silently degrade to
        # "no conversation history", which would mask a DB outage from the workflow.
        conv_history = []
        if request.project_id:
            try:
                # CHAT 2.3.1 — Repository READ: conversation_messages for context.
                #             Failure policy here is FAIL LOUD (500) on purpose —
                #             silently replying with "no history" would mask a DB
                #             outage (contrast with the fail-soft steps below).
                conv_history = await ConversationMessageRepository.get_conversation_history(request.project_id)
            except Exception as db_err:
                logger.exception("[GENERAL_CHAT] Failed to load conversation history from DB")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to load conversation history from database: {str(db_err)}",
                ) from db_err

        # Generate response with full project context
        knowledge_chunks = []
        try:
            from app.project_knowledge_service import retrieve_project_knowledge

            knowledge_chunks = await retrieve_project_knowledge(
                str(request.project_id), request.raw_input or "", session,
            )
        except Exception as retrieval_err:
            # Knowledge retrieval enriches chat but must not make ordinary chat
            # unavailable. Authorization already scoped the project before this
            # branch; any storage/tokenization failure falls back to prior behavior.
            logger.warning(
                "[GENERAL_CHAT] Project knowledge retrieval unavailable; continuing without it: %s",
                retrieval_err,
            )
        try:
            # CHAT 2.3.2 — LLM generation (service layer, CHAT 2.3.2 in
            #             app/semantic_service.py): project context + history →
            #             LangChain ChatOpenAI → text. No agent, no memory write.
            response_text = await generate_general_chat_response(
                raw_input=request.raw_input or "",
                project_context=req_state or {},
                conversation_history=conv_history,
                knowledge_chunks=knowledge_chunks,
            )
        except Exception as llm_err:
            # Intentional graceful degradation: when the LLM call fails we log the
            # error (with traceback) and return a canned user-facing fallback instead
            # of a 500. This does not corrupt state or alter the caller's data.
            logger.error(f"[GENERAL_CHAT] Failed to generate LLM response: {str(llm_err)}", exc_info=True)
            response_text = "I understand your question, but I'm currently experiencing some technical difficulties. Please try again in a moment."

        # Persist assistant response (with error handling for DB failures)
        if request.project_id and response_text and not request.client_persists_timeline:
            try:
                await ConversationMessageRepository.save_message(
                    project_id=str(request.project_id),
                    role="assistant",
                    message=response_text,
                    session=session,
                    # CHAT 2.3.3 — Repository WRITE of the assistant turn; FAIL SOFT
                    #             (the reply is already computed, so a failed insert
                    #             is logged loudly rather than losing the answer).
                    workflow_state="general_chat",
                    intent="GENERAL_CHAT"
                )
            except Exception as db_err:
                # The response is already computed and returned to the user, so a
                # persistence failure here cannot be undone. Keep the response flowing
                # but log the failed write loudly so the DB problem is not masked.
                logger.error(
                    f"[GENERAL_CHAT] Failed to save assistant response to DB (response delivered but not persisted): {str(db_err)}",
                    exc_info=True,
                )
            else:
                # Notify SSE subscribers so other connected clients refresh their conversation view.
                # CHAT 2.3.4 — Real-time fan-out: other connected clients refresh
                #             their transcript (EVENTS flow consumer).
                await event_manager.publish(str(request.project_id), "chat_reply", {
                    "project_id": str(request.project_id),
                    "intent": "GENERAL_CHAT",
                })

        # CHAT 2.4 — Return the direct-conversation response contract.
        # Trigger: CHAT 2.3 has produced a reply.
        # Input: reply text, knowledge sources, and unchanged current artifacts.
        # Writes: NONE; this return does not commit artifact state.
        # Next: CHAT 1.4 frontend response branch.
        # Return response (no project artifacts modified)
        logger.info(
            "[WORKFLOW][%s][GENERAL_CHAT][DONE] elapsed_ms=%.1f",
            run_id, (time.perf_counter() - pipeline_started) * 1000,
        )
        return {
            "status": "general_chat",
            "detected_intent": "GENERAL_CHAT",
            "message": response_text,
            "knowledge_sources": [
                {
                    "document_id": chunk.get("document_id"),
                    "filename": chunk.get("filename"),
                    "section": chunk.get("section"),
                    "retrieval_score": chunk.get("retrieval_score"),
                }
                for chunk in knowledge_chunks
            ],
            "structured_requirements": {
                "epic_name": req_state.get("epic_name") or (
                    req_state.get("requirements", [])[0].get("title", "")
                    if req_state and req_state.get("requirements")
                    else ""
                ),
                "version": req_state.get("version_number", 1) if req_state else 1,
                "user_stories": req_state.get("user_stories", []) if req_state else []
            } if req_state else {},
            "audit_result": {
                "is_valid": req_state.get("validation_status") == "valid" if req_state else False,
                "audit_version_reviewed": req_state.get("version_number", 1) if req_state else 1,
                "clarification_questions": req_state.get("clarification_questions", []) if req_state else [],
                "passed_checks": [],
                "failed_checks": []
            },
            "prd_markdown": req_state.get("generated_prd", "") if req_state else "",
            "mermaid_diagram": req_state.get("generated_diagrams", "") if req_state else "",
            "workflow_routing": {"workflow": "CHAT", "confidence": 1.0, "reason": "Routed as GENERAL_CHAT via intent detection."}
        }

    # ==========================================
    # STEP 4: Route based on detected intent
    # ==========================================
    # Map detected intent to target_agent for LangGraph workflow routing
    # INTENT 1.4 — Map a requirement intent to the LangGraph target agent.
    # Trigger: restore, lock/conflict, and CHAT 2.3 short-circuits did not return.
    # Input: detected intent and any explicit target_agent already on the request.
    # Branches: create/update → Gatherer; delete → delete flow; clarify → Auditor;
    #           unmapped explicit agent target remains unchanged.
    # Writes: request.target_agent in pipeline memory.
    # Next: workflow-state checks and LangGraph invocation.
    intent_to_target = {
        "CREATE_REQUIREMENT": "gatherer",
        "UPDATE_REQUIREMENT": "gatherer",
        "DELETE_REQUIREMENT": "delete_requirement",
        "CLARIFY_REQUIREMENT": "auditor",
    }
    if detected_intent in intent_to_target:
        request.target_agent = intent_to_target[detected_intent]
        logger.info(f"[INTENT ROUTING] Intent {detected_intent} -> target_agent={request.target_agent}")

    current_state = req_state.get("current_workflow_state", "IDLE") if req_state else "IDLE"
    logger.info(f"[WORKFLOW STATE MACHINE] Current workflow state: {current_state}")

    # Routing rules based on current_workflow_state
    if current_state == "IDLE":
        current_state = "GATHERING"
    elif current_state == "GATHERING":
        pass
    elif current_state == "REVIEWING":
        if request.target_agent == "gatherer" or (request.raw_input and ("update" in request.raw_input.lower() or "create" in request.raw_input.lower() or "add" in request.raw_input.lower())):
            current_state = "GATHERING"
        else:
            # Wait for user edits / normal chat, do not run gatherer automatically
            pass
    elif current_state == "AUDITING":
        if request.target_agent != "auditor":
            # Wait until user explicitly clicks Run Auditor
            pass
    elif current_state == "WAITING_CLARIFICATION":
        # Clarification answers now use the dedicated submit endpoint. Do not
        # hijack an explicit update or archive request and send it back through
        # Auditor: a failed audit otherwise traps the project in this state and
        # makes every subsequent mutation look like another validation run.
        if request.target_agent in {"gatherer", "delete_requirement"}:
            current_state = "GATHERING"
    elif current_state == "ARCHITECTING":
        if request.target_agent != "architect":
            # Wait until user explicitly clicks Generate PRD / Diagram
            pass
    elif current_state == "COMPLETED":
        if request.target_agent == "gatherer" or (request.raw_input and ("update" in request.raw_input.lower() or "create" in request.raw_input.lower())):
            current_state = "GATHERING"
        else:
            pass

    if not req_state:
        reqs = request.structured_requirements or {}
        all_ac = []
        for us in reqs.get("user_stories", []):
            all_ac.extend(us.get("acceptance_criteria", []))

        # Only synthesize a requirement when the payload actually carries user
        # stories. An epic-name-only payload (e.g. the frontend's placeholder
        # "Structured Requirements Draft" on a brand-new project) must leave the
        # board EMPTY so the requirement sequence starts at REQ-001 — a
        # fabricated requirement used to claim REQ-001 and push the first real
        # requirement to REQ-002.
        legacy_stories = reqs.get("user_stories", []) or []
        req_state = {
            "project_id": request.project_id,
            # The authoritative project name lives in the projects table;
            # keep this denormalized copy blank rather than a demo label.
            "project_name": "",
            "requirements": [
                {
                    "requirement_code": default_requirement_code(0),
                    "title": reqs.get("epic_name", ""),
                    "description": "",
                    "user_stories": legacy_stories
                }
            ] if legacy_stories else [],
            "business_goals": [],
            "actors": [],
            "user_stories": legacy_stories,
            "acceptance_criteria": all_ac,
            "clarification_questions": [],
            "validation_status": "pending",
            "generated_prd": "",
            "generated_diagrams": "",
            "current_workflow_state": current_state,
            "requirements_revision": 1,
            "version_number": 1,
            "updated_at": None
        }
    else:
        req_state["current_workflow_state"] = current_state
        # Gatherer requests may carry a newer client-side draft. Architect
        # generation is read-only with respect to requirements and must always
        # use the complete canonical server board; overlaying a flattened client
        # snapshot here previously collapsed multi-requirement projects to one.
        if request.structured_requirements and request.target_agent != "architect":
            reqs = request.structured_requirements
            all_ac = []
            for us in reqs.get("user_stories", []):
                all_ac.extend(us.get("acceptance_criteria", []))

            reqs_list = reqs.get("requirements", [])
            if isinstance(reqs_list, list) and reqs_list:
                # Use the requirements list from frontend
                req_state["requirements"] = reqs_list
            else:
                # Convert legacy epic_name to requirement format.
                # Preserve database identity (id/priority/status/...) from the
                # previous requirement record: a bare synthetic dict without
                # the REQUIRED ``id`` field previously failed RequirementDetail
                # response validation -> opaque HTTP 500 on PRD generation.
                #
                # Nothing is fabricated when there is neither a previous
                # requirement record nor any story to attach: an epic-name-only
                # payload must leave the board EMPTY so the requirement sequence
                # still starts at REQ-001 (a fabricated REQ-001 used to push the
                # first real requirement to REQ-002).
                previous_reqs = [r for r in (req_state.get("requirements", []) or []) if isinstance(r, dict)]
                prev_matched = previous_reqs[0] if previous_reqs else {}
                legacy_stories = reqs.get("user_stories", []) or []
                if prev_matched or legacy_stories:
                    req_state["requirements"] = [
                        {
                            **prev_matched,
                            # Preserve the stored requirement code; only synthesize
                            # one when no prior requirement record exists at all.
                            "requirement_code": prev_matched.get("requirement_code") or default_requirement_code(0),
                            "title": reqs.get("epic_name") or prev_matched.get("title", ""),
                            "description": prev_matched.get("description", ""),
                            "user_stories": legacy_stories
                        }
                    ]
            req_state["user_stories"] = reqs.get("user_stories", [])
            req_state["acceptance_criteria"] = all_ac

    # Snapshot only the requirement-source fields before the workflow. Agent
    # nodes mutate their state dictionaries in place, so a deep copy is needed
    # to distinguish a real proposal from a clarification/no-op result.
    requirement_source_fields = (
        "requirements", "business_goals", "actors", "user_stories",
        "acceptance_criteria", "prd_narrative",
    )
    requirement_state_before_workflow = {
        field: copy.deepcopy(req_state.get(field)) for field in requirement_source_fields
    }

    # Formulate initial state corresponding to AgentState TypedDict
    knowledge_documents = []
    if request.target_agent == "auditor":
        knowledge_documents = await DocumentRepository.get_validation_context(request.project_id, session)

    initial_state = {
        "project_id": request.project_id,
        "raw_input": request.raw_input or "",
        "structured_requirements": {
            "epic_name": req_state.get("epic_name") or (
                req_state.get("requirements", [])[0].get("title", "")
                if req_state and req_state.get("requirements")
                else ""
            ),
            "version": req_state.get("version_number", 1),
            "user_stories": req_state.get("user_stories", []),
            "requirements": req_state.get("requirements", []),
        },
        "audit_result": {},
        "prd_markdown": req_state.get("generated_prd", ""),
        "mermaid_diagram": req_state.get("generated_diagrams", ""),
        "current_version": req_state.get("version_number", 1),
        "version_history_summaries": request.version_history_summaries if request.version_history_summaries is not None else "No previous history.",
        "target_agent": request.target_agent or "gatherer",
        "force_prd_rebuild": request.force_prd_rebuild,
        "requirement_state": req_state,
        "detected_intent": detected_intent,
        "intent_confidence": intent_confidence,
        # Validate always uses the server-side persisted knowledge base. The
        # browser sends only document metadata, so loading the canonical text
        # here prevents stale/tampered client content and makes a document-only
        # project auditable without requiring extraction first.
        "knowledge_documents": knowledge_documents,
        "db_session": session,  # Pass the active session to workflow nodes
        # Architect output follows the same confirmation contract as gathered
        # requirements.  Section/version writes happen in confirm_action, not
        # while merely building the preview.
        "stage_artifacts_only": request.target_agent == "architect",
    }

    try:
        # End the read transaction before potentially minutes of local-model
        # inference. The same AsyncSession can safely autobegin a short write
        # transaction when the graph saves its message/result afterwards; this
        # avoids reserving a database connection for the entire generation.
        await session.commit()
        # Run state graph asynchronously (merges and generates in memory)
        logger.info(
            "[WORKFLOW][%s][AGENT][START] target=%s requirements=%d knowledge_documents=%d",
            run_id, request.target_agent or "gatherer",
            len(req_state.get("requirements", []) or []), len(knowledge_documents),
        )
        final_state = await prd_workflow.ainvoke(initial_state)
        logger.info(
            "[WORKFLOW][%s][AGENT][DONE] target=%s elapsed_ms=%.1f",
            run_id, request.target_agent or "gatherer",
            (time.perf_counter() - pipeline_started) * 1000,
        )

        # Read updated centralized RequirementState from final graph output
        req_state = final_state.get("requirement_state", req_state)
        requirement_content_changed = any(
            req_state.get(field) != requirement_state_before_workflow.get(field)
            for field in requirement_source_fields
        )
        # MATCHING 4.1 — Interpret the completed graph match at the API pipeline boundary.
        # Trigger: MATCHING 4.0 has ended the matcher/Gatherer/archive graph branch.
        # Input: final requirement_match plus the RequirementState returned by the graph.
        # Branches: unresolved match → MATCHING 4.1.1 clarification persistence;
        #           material archive preview → MATCHING 4.1.2 pending ARCHIVE action;
        #           no material change, lock, or stale run → no matcher mutation.
        # Writes: NONE in this step; it derives the persistence decision used below.
        requirement_match = final_state.get("requirement_match") or {}
        matcher_status = str(requirement_match.get("status") or "").upper()
        matcher_needs_clarification = (
            matcher_status in {"AMBIGUOUS", "LOW_CONFIDENCE"}
            or (
                requirement_match
                and float(requirement_match.get("confidence", 1.0) or 0.0) < 0.75
            )
        )

        # Update workflow state after run
        if matcher_needs_clarification:
            req_state["current_workflow_state"] = "WAITING_CLARIFICATION"
        elif request.target_agent == "gatherer" or current_state == "GATHERING":
            req_state["current_workflow_state"] = "REVIEWING"
        elif request.target_agent == "auditor":
            is_valid = req_state.get("validation_status") == "valid"
            req_state["current_workflow_state"] = "REVIEWING" if is_valid else "WAITING_CLARIFICATION"
        elif request.target_agent == "architect":
            req_state["current_workflow_state"] = "COMPLETED"

        workflow_routing = final_state.get("workflow_routing") or {}

        # A completed result remains useful even if the board changed during a
        # long model run, but it must be read-only and never merge into the newer
        # state. Return it with a stale marker instead of failing the whole job.
        stale_result = False
        if persisted_version_at_start is not None:
            latest_state = await RequirementStateRepository.get_by_project_id(request.project_id, session)
            latest_version = latest_state.get("version_number") if latest_state else None
            latest_requirements_revision = (
                int(latest_state.get("requirements_revision") or 1) if latest_state else 1
            )
            if (
                latest_version != persisted_version_at_start
                or latest_requirements_revision != persisted_requirements_revision_at_start
            ):
                stale_result = True
                logger.warning(
                    "AI result for project %s is stale (PRD version %s -> %s, requirements revision %s -> %s); returning without a pending merge",
                    request.project_id,
                    persisted_version_at_start,
                    latest_version,
                    persisted_requirements_revision_at_start,
                    latest_requirements_revision,
                )

        # ==========================================
        # IN-MEMORY MERGE: Store merged state as a pending action instead of directly persisting to DB.
        # The user must confirm before changes are committed to the database.
        # ==========================================

        audit_operational_error = bool(final_state.get("audit_operational_error"))
        audit_auto_persisted = False
        architect_draft_staged = False
        pending_action_id = None
        logger.info(f"[IN-MEMORY MERGE] Storing merged state as pending action for project {request.project_id}...")

        # ARCHITECT 8.1 — Stage an explicit architect result as a durable preview.
        # Trigger: target_agent="architect" and the run is not stale.
        # Writes: replaces any older GENERATE_PRD pending action with the candidate
        # document/diagram/narrative, source requirements revision, base PRD version,
        # proposed version/SemVer, and preserved-section list.
        # Persistence: no canonical document, section, diagram, or version ledger is
        # changed here. ARCHITECT 9.0 Save is the sole governed artifact writer;
        # Discard removes the pending action and leaves saved artifacts unchanged.
        if request.target_agent == "architect" and not stale_result:
            generated_prd = str(req_state.get("generated_prd") or "").strip()
            if not generated_prd:
                raise RuntimeError("Architect completed without generating a PRD document")

            # One project has at most one PRD preview. Regeneration replaces the
            # older draft but never touches the currently saved PRD.
            for existing_action in await PendingActionRepository.get_by_project(request.project_id, session):
                if existing_action.get("action_type") == "GENERATE_PRD":
                    await PendingActionRepository.delete(existing_action["id"], request.project_id, session)

            source_requirements_revision = persisted_requirements_revision_at_start
            base_prd_version = int(persisted_version_at_start or 1)
            latest_prd_version = await PRDVersionRepository.get_latest(request.project_id, session)
            proposed_version = (
                int(latest_prd_version.get("version_number") or 0) + 1
                if latest_prd_version
                else 1
            )
            skipped_sections = list(final_state.get("skipped_locked_prd_sections") or [])
            pending_action = await PendingActionRepository.create(
                project_id=request.project_id,
                data={
                    "action_type": "GENERATE_PRD",
                    "target_requirement_id": None,
                    "original_user_message": "Generate PRD",
                    "proposed_changes": {
                        "generated_prd": generated_prd,
                        "generated_diagrams": req_state.get("generated_diagrams", ""),
                        "prd_narrative": req_state.get("prd_narrative", {}),
                        "source_requirements_revision": source_requirements_revision,
                        "base_prd_version": base_prd_version,
                        "proposed_version": proposed_version,
                        "proposed_semver": req_state.get("proposed_prd_semver"),
                        "preserved_sections": skipped_sections,
                    },
                    "affected_user_story_ids": [],
                    "affected_acceptance_criteria_ids": [],
                    "workflow_stage": "PRD_PREVIEW",
                },
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                session=session,
            )
            pending_action_id = pending_action.get("id")
            architect_draft_staged = True
            logger.info(
                "[WORKFLOW][%s][STAGE_PRD][DONE] pending_action=%s project=%s",
                run_id, pending_action_id, request.project_id,
            )

        # GATHERING 8.0 — API-pipeline handoff to confirmation.
        # Input: final graph preview, pre-run state/revision, intent, stale/lock/error
        #        flags, and whether requirement source content materially changed.
        # Branches: matcher clarification persists only question state; audit and PRD
        #           use their own contracts; a current material Gatherer result creates
        #           MERGE (DELETE creates ARCHIVE); stale/locked/unchanged creates none.
        # Writes: pending_actions stores proposed_changes and its base revision, but
        #         does not apply final requirement/story rows. Save in CONFIRMATION is
        #         the final writer; the response exposes the action to GATHERING 8.1.
        # Create a pending action with the full merged state as proposed changes
        if matcher_needs_clarification and not stale_result:
            # MATCHING 4.1.1 — Persist unresolved matcher state without a preview.
            # Writes: validation/workflow status and clarification questions in
            #         RequirementState. MATCHING 1.5 already wrote the transcript.
            # Next: the chat renders the answer form; no MERGE/ARCHIVE confirmation
            #       exists until a later clarified request produces a material change.
            req_state = await RequirementStateRepository.save_or_update(
                request.project_id,
                {
                    "validation_status": "invalid",
                    "current_workflow_state": "WAITING_CLARIFICATION",
                    "clarification_questions": req_state.get("clarification_questions", []),
                },
                session,
            )
            final_state["requirement_state"] = req_state
            logger.info(
                "Requirement Matcher clarification persisted for project %s; no merge confirmation created",
                request.project_id,
            )
        elif request.target_agent == "auditor" and not audit_operational_error and not stale_result:
            # AUDIT 2.8 — Persist a successful current audit immediately.
            # Trigger: the original request explicitly selected target_agent="auditor"
            #          and completed without operational error or stale state. A
            #          router-classified audit COMMAND does not enter this branch.
            # Writes: validation/check fields, findings, sources, verdict/context,
            #         checklist identity, clarification-question rows, audit_results,
            #         and audit_runs history through RequirementStateRepository. On a
            #         document-only project, repositories may create one archived
            #         REQ-000-style internal requirement anchor for audit foreign keys;
            #         it is excluded from the visible active requirement board.
            # Next: questions are answerable at AUDIT 3.x by stable ID. No pending
            #       MERGE is created because requirement source content is unchanged.
            saved_audit_state = await RequirementStateRepository.save_or_update(
                request.project_id,
                {
                    "validation_status": req_state.get("validation_status", "invalid"),
                    "current_workflow_state": req_state.get("current_workflow_state", "WAITING_CLARIFICATION"),
                    "clarification_questions": req_state.get("clarification_questions", []),
                    "passed_checks": req_state.get("passed_checks", []),
                    "failed_checks": req_state.get("failed_checks", []),
                    "audit_findings": req_state.get("audit_findings", []),
                    "audit_source_references": req_state.get("audit_source_references", []),
                    "audit_verdict": req_state.get("audit_verdict", "needs_clarification"),
                    "audit_project_context": req_state.get("audit_project_context", {}),
                    "audit_checklist_id": req_state.get("audit_checklist_id", "banking-core"),
                    "audit_checklist_version": req_state.get("audit_checklist_version", "1.0.0"),
                    "_record_audit_run": True,
                },
                session,
            )
            req_state = saved_audit_state
            final_state["audit_result"]["clarification_questions"] = saved_audit_state.get("clarification_questions", [])
            audit_auto_persisted = True
            logger.info("Auditor result persisted directly for project %s; no merge confirmation required", request.project_id)
        elif (
            not architect_draft_staged
            and not audit_operational_error
            and not stale_result
            and not final_state.get("lock_blocked")
            and requirement_content_changed
        ):
            # MATCHING 4.1.2 — Stage material graph output for confirmation.
            # Matcher branch: when MATCHING 3.3 produced a resolved delete preview,
            #                 action_type is ARCHIVE; non-delete Gatherer output uses
            #                 the existing MERGE path through this shared block.
            # Writes: pending_actions with proposed state; ARCHIVE also carries
            #         canonical affected REQ/US codes and acceptance-criteria IDs for
            #         confirmation-time lock validation. No final row changes here.
            # Next: the CONFIRMATION flow applies or cancels the staged action.
            pending_action_type = "ARCHIVE" if detected_intent == "DELETE_REQUIREMENT" else "MERGE"
            pending_action = await PendingActionRepository.create(
                project_id=request.project_id,
                data={
                    "action_type": pending_action_type,
                    "target_requirement_id": None,
                    "original_user_message": request.raw_input or "",
                    "proposed_changes": {
                        **req_state,
                        "_base_requirements_revision": persisted_requirements_revision_at_start,
                        "_change_preview": build_change_preview(
                            requirement_state_before_workflow,
                            req_state,
                        ),
                    },
                    # Archive actions store canonical REQ/US codes here for a
                    # second lock check at confirmation time. Other merge types
                    # keep the established empty-list behavior.
                    "affected_user_story_ids": (
                        final_state.get("archive_requirement_codes", [])
                        + final_state.get("archive_story_codes", [])
                    ),
                    "affected_acceptance_criteria_ids": (
                        final_state.get("archive_acceptance_criteria_ids", [])
                        if detected_intent == "DELETE_REQUIREMENT" else []
                    ),
                    "workflow_stage": req_state.get("current_workflow_state", "gatherer_node")
                },
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                session=session
            )
            pending_action_id = pending_action.get("id")
            logger.info(
                "[WORKFLOW][%s][STAGE_MERGE][DONE] pending_action=%s",
                run_id, pending_action_id,
            )
        elif not architect_draft_staged and not audit_operational_error and not stale_result:
            logger.info(
                "[WORKFLOW][%s][NO_CHANGE] Requirement content is unchanged; no pending action created.",
                run_id,
            )
        elif audit_operational_error:
            logger.warning("Auditor operational failure: no pending action created for project %s", request.project_id)
        else:
            logger.warning("Stale AI result: no pending action created for project %s", request.project_id)

        # ==========================================
        # STEP 5: Persist assistant response for non-GENERAL_CHAT intents
        # ==========================================
        agent_message = final_state.get("agent_message", "")
        no_changes = (
            request.target_agent in {"gatherer", "delete_requirement"}
            and (not requirement_content_changed or bool(final_state.get("lock_blocked")))
            and pending_action_id is None
        )
        if no_changes and not agent_message:
            unresolved_questions = req_state.get("clarification_questions") or []
            latest_question = next(
                (str(item.get("question_text") or "").strip() for item in reversed(unresolved_questions) if isinstance(item, dict)),
                "",
            )
            agent_message = (
                f"No changes were made because I need clarification before updating the requirements.\n\n{latest_question}"
                if matcher_needs_clarification and latest_question
                else (
                    "I couldn't find an applicable requirement change in that request. "
                    "No changes were made. Please check the user story ID and try again."
                )
            )
        if (
            not request.client_persists_timeline
            and request.project_id
            and agent_message
            and not no_changes
        ):
            await ConversationMessageRepository.save_message(
                project_id=str(request.project_id),
                role="architect" if request.target_agent == "architect" else "assistant",
                message=agent_message,
                session=session,
                workflow_state=req_state.get("current_workflow_state", request.target_agent or "gatherer"),
                intent="PRD_GENERATION" if request.target_agent == "architect" else detected_intent,
            )

        # Determine status dynamically based on centralized validation status
        is_valid = req_state.get("validation_status") == "valid"
        status_str = "stale" if stale_result else ("completed" if is_valid else "audit_pending")

        # Format structures back to preserve frontend compatibility
        reqs_data = req_state.get("requirements", [])
        if isinstance(reqs_data, list):
            # Multi-requirement format
            epic_name = req_state.get("epic_name") or (
                reqs_data[0].get("title", "")
                if reqs_data and isinstance(reqs_data[0], dict)
                else ""
            )
            structured_out = {
                "epic_name": epic_name,
                "version": req_state.get("version_number", 1),
                "user_stories": req_state.get("user_stories", []),
                "requirements": _ensure_requirement_ids(reqs_data)
            }
        elif isinstance(reqs_data, dict):
            # Legacy dict format
            structured_out = {
                "epic_name": reqs_data.get("epic_name", ""),
                "version": req_state.get("version_number", 1),
                "user_stories": req_state.get("user_stories", [])
            }
        else:
            # Fallback for None or unexpected types
            structured_out = {
                "epic_name": "",
                "version": req_state.get("version_number", 1),
                "user_stories": req_state.get("user_stories", [])
            }

        # Ensure audit result fields are cleanly populated
        audit_out = {
            "is_valid": is_valid,
            "audit_version_reviewed": req_state.get("version_number", 1),
            "clarification_questions": req_state.get("clarification_questions", []),
            "passed_checks": final_state.get("audit_result", {}).get("passed_checks", []),
            "failed_checks": final_state.get("audit_result", {}).get("failed_checks", []),
            "findings": final_state.get("audit_result", {}).get("findings", []),
            "source_references": final_state.get("audit_result", {}).get("source_references", []),
            "verdict": final_state.get("audit_result", {}).get("verdict", req_state.get("audit_verdict", "needs_clarification")),
            "project_context": final_state.get("audit_result", {}).get("project_context", req_state.get("audit_project_context", {})),
            "checklist_id": final_state.get("audit_result", {}).get("checklist_id", req_state.get("audit_checklist_id", "banking-core")),
            "checklist_version": final_state.get("audit_result", {}).get("checklist_version", req_state.get("audit_checklist_version", "1.0.0")),
        }

        # Notify SSE subscribers that the workflow produced new state / a pending merge.
        await event_manager.publish(str(request.project_id), "workflow_update", {
            "project_id": str(request.project_id),
            "workflow_state": req_state.get("current_workflow_state"),
            "validation_status": req_state.get("validation_status"),
            "pending_action_id": pending_action_id,
            "target_agent": request.target_agent,
        })

        logger.info(
            "[WORKFLOW][%s][COMPLETE][DONE] status=%s pending_merge=%s elapsed_ms=%.1f",
            run_id, status_str,
            not audit_operational_error and not stale_result and not audit_auto_persisted and not architect_draft_staged,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        return {
            "status": status_str,
            "workflow_routing": workflow_routing,
            "detected_intent": final_state.get("detected_intent") or req_state.get("detected_intent", detected_intent),
            "structured_requirements": structured_out,
            "audit_result": audit_out,
            "prd_markdown": req_state.get("generated_prd", ""),
            "mermaid_diagram": req_state.get("generated_diagrams", ""),
            # The PRD may succeed while its LLM-backed diagram refresh fails.
            # Do not drop this explicit false value: the SPA uses it to show
            # the partial-success warning above the completed PRD preview.
            "diagram_regenerated": final_state.get("diagram_regenerated"),
            "diagram_generated_at": final_state.get("diagram_generated_at") or req_state.get("diagram_generated_at"),
            "message": agent_message,
            "pending_merge": pending_action_id is not None,
            "pending_action_id": pending_action_id,
            "pending_action_type": (
                "GENERATE_PRD" if pending_action_id is not None and request.target_agent == "architect" else
                "ARCHIVE" if pending_action_id is not None and detected_intent == "DELETE_REQUIREMENT" else
                "MERGE" if pending_action_id is not None else None
            ),
            "no_changes": no_changes,
            "stale_result": stale_result,
            "audit_auto_persisted": audit_auto_persisted,
            "skipped_locked_prd_sections": final_state.get("skipped_locked_prd_sections", []),
        }
    except HTTPException:
        # Preserve intentional 4xx responses (notably validation-context 413s)
        # instead of disguising them as a generic LangGraph 500.
        logger.warning(
            "[WORKFLOW][%s][FAILED] http_error elapsed_ms=%.1f",
            run_id, (time.perf_counter() - pipeline_started) * 1000,
        )
        raise
    except LMStudioUnavailableError as e:
        policy = classify_exception(e)
        log_event(logger, logging.ERROR, "workflow.failed", log_context, exc_info=True, kind=policy.kind.value, retryable=policy.retryable, rollback=policy.rollback)
        logger.exception(
            "[WORKFLOW][%s][FAILED] kind=lm_studio_unavailable project=%s elapsed_ms=%.1f",
            run_id, request.project_id,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The local inference gateway (LM Studio) is unreachable. Start LM Studio with a loaded model and retry the generation.",
        )
    except LMStudioGatewayError as e:
        policy = classify_exception(e)
        log_event(logger, logging.ERROR, "workflow.failed", log_context, exc_info=True, kind=policy.kind.value, retryable=policy.retryable, rollback=policy.rollback)
        logger.exception(
            "[WORKFLOW][%s][FAILED] kind=lm_studio_gateway project=%s elapsed_ms=%.1f",
            run_id, request.project_id,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The local inference gateway rejected the generation request. Retry once the model is responsive.",
        )
    except LMStudioOutputParsingError as e:
        policy = classify_exception(e)
        log_event(logger, logging.ERROR, "workflow.failed", log_context, exc_info=True, kind=policy.kind.value, retryable=policy.retryable, rollback=policy.rollback)
        logger.exception(
            "[WORKFLOW][%s][FAILED] kind=output_parsing project=%s elapsed_ms=%.1f",
            run_id, request.project_id,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The model returned an unreadable response. Please retry the generation.",
        )
    except RuntimeError as e:
        policy = classify_exception(e)
        log_event(logger, logging.ERROR, "workflow.failed", log_context, exc_info=True, kind=policy.kind.value, retryable=policy.retryable, rollback=policy.rollback)
        # invoke_llm_structured exhausts both of its parse attempts (e.g. a
        # reasoning model burning its whole token budget on thinking content).
        # That is a retryable model-output failure, not a server bug — 503, not 500.
        logger.exception(
            "[WORKFLOW][%s][FAILED] kind=structured_output project=%s elapsed_ms=%.1f",
            run_id, request.project_id,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The audit or generation could not complete. Please try again. Your previous saved results have not been replaced.",
        )
    except Exception as e:
        policy = classify_exception(e)
        log_event(logger, logging.ERROR, "workflow.failed", log_context, exc_info=True, kind=policy.kind.value, retryable=policy.retryable, rollback=policy.rollback)
        logger.exception(
            "[WORKFLOW][%s][FAILED] kind=unexpected project=%s elapsed_ms=%.1f",
            run_id, request.project_id,
            (time.perf_counter() - pipeline_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LangGraph multi-agent workflow execution failed: {str(e)}"
        )


# ==========================================
# WORKFLOW ROUTER / INTENT DETECTOR / MATCHER API
# ==========================================

# INTENT 4.0 — Authenticated API/debug integration for intent classification.
# Trigger: direct detector or matcher API request; the SPA uses INTENT 1.x instead.
# Calls: INTENT 4.1 detector endpoint, INTENT 4.2 matcher fallback, and shared
#        INTENT 4.1.1 context loading.
# Writes: NONE.

# INTENT 4.1.1 — Load authoritative story context for direct detector/matcher calls.
# Trigger: INTENT 4.1.2 or the project-scoped matcher endpoint after ownership.
# Input: project id and request-scoped database session.
# Calls: RequirementStateRepository.get_by_project_id.
# Writes: NONE.
# Failure: fails loudly with HTTP 500 rather than misclassifying against empty context.
async def _load_current_stories(project_id: str, db: AsyncSession) -> List[Dict[str, Any]]:
    """Load a project's current user stories, or fail loudly if the DB is unreachable.

    Deliberately does NOT swallow DB errors. Silently degrading to an empty story list
    here would hide a real DB outage and let downstream intent-detection /
    requirement-matching run without any project context, producing misleading output.
    A failure is surfaced as HTTP 500 instead.
    """
    try:
        req_state = await RequirementStateRepository.get_by_project_id(project_id, db)
    except Exception as e:
        logger.exception(f"Failed to load requirement state for project {project_id} from DB")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load project context from database: {str(e)}",
        ) from e
    return req_state.get("user_stories", []) if req_state else []

# ROUTING 5.1 — Expose the classifier for authenticated API/debug callers.
# Trigger: POST /api/workflow-router; the SPA reaches routing through ROUTING 2.1.
# Input: message, AUTH 7.1 identity, and workflow rate limit.
# Calls: ROUTING 5.1.1 budget guard → ROUTING 5.1.2 classifier invocation.
# Writes: NONE.
# Failure: rate limit returns 429; oversized text returns 413; classifier fallback
#          follows ROUTING 3.5.
@router.post("/api/workflow-router", response_model=WorkflowRoutingResult, status_code=status.HTTP_200_OK)
async def post_workflow_router(
    payload: WorkflowRouterRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW
        )
    ),
) -> WorkflowRoutingResult:
    """
    Direct endpoint for testing and executing the Workflow Router.

    Classifies incoming message into CHAT, QUESTION, COMMAND, or REQUIREMENT.

    Args:
        payload: Contains the user ``message`` to classify.
        _rate_limit: Injected rate limiter (Finding #39).

    Returns:
        WorkflowRoutingResult: The classified workflow type with confidence and reason.

    Raises:
        HTTPException: 413 if ``message`` exceeds ``MAX_CONTEXT_TOKENS``.
    """
    from app.semantic_service import classify_workflow

    # ROUTING 5.1.1 — Reject an oversized classifier message before LLM work.
    # Failure: validate_text_budget raises HTTP 413.
    validate_text_budget(payload.message, "Workflow-router message")
    # ROUTING 5.1.2 — Same classifier the graph uses (ROUTING 3.0); the result is
    #               returned verbatim, so this endpoint can be used to reproduce a
    #               routing decision without running the workflow.
    return await classify_workflow(payload.message)


# INTENT 4.1 — Expose intent classification to authenticated API/debug callers.
# Trigger: POST /api/intent-detector; no SPA client method calls this route.
# Input: message, optional project id, AUTH 7.1 identity, DB session, and rate limit.
# Calls: INTENT 4.1.0 budget guard → optional 4.1.2 ownership/context → 4.1.3 detector.
# Writes: NONE.
# Failure: rate limit 429, oversized text 413, ownership errors, or loud context-read 500.
@router.post("/api/intent-detector", response_model=RequirementIntentDetectionResult, status_code=status.HTTP_200_OK)
async def post_intent_detector(
    payload: IntentDetectorRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW
        )
    ),
) -> RequirementIntentDetectionResult:
    """
    Direct endpoint for testing and executing Requirement Intent Detection
    (GENERAL_CHAT, REQUIREMENT_REQUEST).

    Args:
        payload: Contains the user ``message`` and optional project context.
        db: Active asynchronous database session.
        _rate_limit: Injected rate limiter (Finding #39).

    Returns:
        RequirementIntentDetectionResult: The detected intent with confidence and reason.

    Raises:
        HTTPException: 413 if ``message`` exceeds ``MAX_CONTEXT_TOKENS``.
    """
    from app.semantic_service import detect_requirement_intent

    # INTENT 4.1.0 — Reject oversized direct-detector input before context or LLM work.
    # Failure: validate_text_budget raises HTTP 413.
    validate_text_budget(payload.message, "Intent-detector message")

    current_stories = []
    if payload.project_id:
        # INTENT 4.1.2 — Authorize project context and load its current stories.
        # Calls: AUTH 7.4 ownership gate → INTENT 4.1.1 context loader.
        # Writes: NONE.
        # AUTHZ: project stories are tenant data (titles/descriptions that get
        # injected into the LLM prompt). The caller must own the project before
        # they are loaded — otherwise any authenticated user could probe another
        # project's story content by passing its id here.
        await verify_project_access(str(payload.project_id), current_user, db)
        current_stories = await _load_current_stories(payload.project_id, db)

    # INTENT 4.1.3 — Invoke INTENT 2.0 and return its normalized result verbatim.
    result = await detect_requirement_intent(payload.message, current_stories)
    return result


# MATCHING 5.0 — Authenticated direct matcher API boundary.
# Purpose: expose MATCHING 2.0 for manual/API use without entering the graph.
# Writes: NONE; this endpoint reads authorized story context and returns a match.
#
# MATCHING 5.1 — POST /api/requirement-matcher.
# Trigger: an authenticated, workflow-rate-limited API request.
# Input: message, optional project_id, and optional pre-detected intent.
# Calls: MATCHING 5.1.0 budget guard → MATCHING 5.1.1 authorized context load →
#        optional INTENT 4.2 → MATCHING 5.1.2 canonical matcher.
# Failure: oversized text returns 413; missing/foreign project access is rejected
#          by AUTH; semantic/parse failures follow MATCHING 2.6.
# Note: no frontend client method currently calls this API/test-facing endpoint.
@router.post("/api/requirement-matcher", response_model=RequirementMatcherResult, status_code=status.HTTP_200_OK)
async def post_requirement_matcher(
    payload: RequirementMatcherRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "workflow", settings.RATE_LIMIT_WORKFLOW_LIMIT, settings.RATE_LIMIT_WORKFLOW_WINDOW
        )
    ),
) -> RequirementMatcherResult:
    """
    Direct endpoint for testing and executing the Requirement Matcher Agent.

    Determines which existing requirement(s) the user's message refers to
    before any modification occurs.

    Args:
        payload: Contains the user ``message``, optional project context and
            pre-detected intent.
        db: Active asynchronous database session.
        _rate_limit: Injected rate limiter (Finding #39).

    Returns:
        RequirementMatcherResult: The matched requirement, action recommendation
            and any ambiguous candidates.

    Raises:
        HTTPException: 413 if ``message`` exceeds ``MAX_CONTEXT_TOKENS``.
    """
    from app.semantic_service import detect_requirement_intent, match_requirement

    # MATCHING 5.1.0 — Reject an oversized message before context or LLM work.
    validate_text_budget(payload.message, "Requirement-matcher message")

    # MATCHING 5.1.1 — Load optional project context only after ownership succeeds.
    # Branches: no project_id → empty story context; authorized project → active
    #           current stories; unauthorized/missing project → AUTH error.
    current_stories = []
    if payload.project_id:
        # AUTHZ: same tenant-data gate as the intent detector — the caller must
        # own the project before its stories are loaded for matching.
        await verify_project_access(str(payload.project_id), current_user, db)
        current_stories = await _load_current_stories(payload.project_id, db)

    intent = payload.detected_intent
    if not intent:
        # INTENT 4.2 — Supply missing intent to the direct matcher endpoint.
        # Trigger: /api/requirement-matcher payload omits detected_intent.
        # Input: matcher message and optionally authorized current stories.
        # Calls: INTENT 2.0 canonical detector.
        # Branches: supplied intent → skip detection; missing intent → classify here.
        # Writes: local matcher input only; no project state mutation.
        # Failure: detector uses INTENT 2.8 fallback; absent result uses legacy UPDATE.
        intent_res = await detect_requirement_intent(payload.message, current_stories)
        intent = intent_res.get("intent", "UPDATE")

    # MATCHING 5.1.2 — Same matcher the graph uses (MATCHING 2.0), with the intent
    #               either supplied by the caller or detected here (INTENT 2.0).
    result = await match_requirement(payload.message, intent, current_stories)
    return result

import asyncio
import logging
from typing import Dict, Any, List
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthenticatedUser,
    get_current_user,
    require_project_owner,
    verify_project_access,
)
from app.config import settings
from app.database import get_db
from app.llm_client import (
    LMStudioGatewayError,
    LMStudioOutputParsingError,
    LMStudioUnavailableError,
)
from app.rate_limit import rate_limit_dependency
from app.workflow_cancellation import workflow_cancellations
from app.input_validation import validate_text_budget, validate_body_budget
from app.repositories import (
    RequirementRepository,
    RequirementStateRepository,
    ConversationMessageRepository,
    PendingActionRepository,
    ClarificationQuestionRepository,
    DocumentRepository,
)
from app.schemas import (
    ProcessRequirementsRequest,
    RequirementLockRequest,
    UserAnswerSubmit,
    WorkflowRouterRequest,
    IntentDetectorRequest,
    RequirementMatcherRequest,
    RequirementDetail,
    RequirementLockResponse,
    AuditRespondResponse,
    RequirementStateResponse,
    ProcessRequirementsResponse,
    WorkflowRoutingResult,
    RequirementIntentDetectionResult,
    RequirementMatcherResult,
)
from app.agents import prd_workflow
from app.event_manager import event_manager
from app.requirement_codes import default_requirement_code

logger = logging.getLogger("app.routes.requirements")

router = APIRouter()


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

# AUDIT 3.0 — Answer a compliance clarification question raised by the auditor
#             (AUDIT 2.x → staged → confirmed). Branch map:
#               3.1 resolve the owning project from the question id and verify
#                   ownership (IDOR fix — writing by PK alone let any authenticated
#                   user answer any project's question; unknown id → 404, never 403)
#               3.2 project-scoped, LOCK-GATED repository update
#                   (user_answer + is_resolved=True; ArtifactLockError → 409)
#               3.3 session commit + SSE "clarification_answered"
#             PRECONDITION (documented): the question must already exist as a ROW, i.e.
#             its merge must have been confirmed (CONFIRMATION flow) — a question that
#             is still only staged in a pending action is not resolvable yet.
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

    # Project-scoped write through the repository: it resolves the question
    # through its own project (audit_result -> requirement) and honours the
    # artifact lock, instead of a bare ORM update by PK.
    from app.lock_service import ArtifactLockError

    try:
        # AUDIT 3.2 — The write itself (AUDIT 4.1): resolves the question through its
        #            own project (audit_result → requirement) and honours the artifact
        #            lock, instead of a bare ORM update by primary key.
        updated = await ClarificationQuestionRepository.update(
            str(question_id),
            str(project_id),
            {"user_answer": payload.answer_text, "is_resolved": True},
            session,
        )
    except ArtifactLockError as e:
        # Locked questions must be unlocked before they can be answered. The
        # repository's lock gate raises a domain error, not an HTTP one.
        raise HTTPException(status_code=409, detail=str(e))
    if updated is None:
        # The question vanished between the ownership lookup and the write.
        raise HTTPException(status_code=404, detail=f"Clarification question {question_id} not found")

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # AUDIT 3.3 — Real-time fan-out: the resolved question disappears from other
    #            clients' pending-clarification forms (EVENTS flow).
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


# CLARIFY 3.0 — Answer intake for the auditor's / matcher's / gatherer's clarification
#             questions (FLOW 10). Reached from CLARIFY 1.3 via 2.1. Branch map:
#               3.1 manual validation (`payload` is an untyped Dict → UUID check 400)
#                   + AUTHZ ownership (AUTH 7.4) BEFORE any read/write
#               3.2 project artifact-lock gate → 409 (locked) / 404 (unresolvable)
#               3.3 state read (PROJECT 2.3.2) → 404 when the project has no state
#               3.4 positional answer mapping (`q-<index>` over the stored list)
#               3.5 validation recompute (unresolved ⇒ invalid + WAITING_CLARIFICATION)
#               3.6 RequirementStateRepository.save_or_update ← THE WRITE (PROJECT 8.2.2)
#               3.7 SSE "clarification_submitted"
#               3.8 return the saved state → CLARIFY 1.3 refreshes the audit view
#             DISCREPANCY (documented, not changed): the UI button says "Submit Answers
#             & Re-Audit", but no auditor/LLM call happens here — the verdict is
#             recomputed deterministically from the unresolved count (3.5); a real
#             re-audit requires a separate Validate action (AUDIT 5.0).
@router.post("/api/clarification/submit", response_model=RequirementStateResponse, status_code=status.HTTP_200_OK)
async def post_clarification_submit(payload: Dict[str, Any], current_user: AuthenticatedUser = Depends(get_current_user), session: AsyncSession = Depends(get_db)) -> RequirementStateResponse:
    """
    Submits answers to clarification questions, runs Auditor compliance check,
    and updates workflow state.

    Args:
        payload: Dict containing ``project_id`` and a mapping of answers
            (``answers[`q-<idx>`]``).
        session: Active asynchronous database session.

    Returns:
        RequirementStateResponse: The updated requirement state.

    Raises:
        HTTPException: 400 if ``project_id`` is not a valid UUID; 409 if the
            project is locked; 404 if the project does not exist.
    """
    project_id = payload.get("project_id")
    answers = payload.get("answers", {})

    # LOCK ENFORCEMENT: Cannot submit clarification answers on a locked project
    from app.lock_service import LockService, ArtifactLockError
    try:
        UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    # AUTHZ: the caller must own the project before anything is read/written.
    await verify_project_access(str(project_id), current_user, session)

    try:
        lock_info = await LockService.get_lock_status("project", project_id, session, project_id=project_id)
        LockService.raise_if_locked("project", project_id, lock_info)
    except ArtifactLockError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    req_state = await RequirementStateRepository.get_by_project_id(project_id, session)
    if not req_state:
        raise HTTPException(status_code=404, detail="Project not found")

    # Stable question UUIDs are the primary contract. Keep q-<index> support for
    # older clients, but never depend on list position when an id is available.
    questions = req_state.get("clarification_questions", [])
    for idx, q in enumerate(questions):
        stable_key = str(q.get("id") or "")
        legacy_key = f"q-{idx}"
        answer = answers.get(stable_key) if stable_key else None
        if answer is None:
            answer = answers.get(legacy_key)
        if isinstance(answer, str) and answer.strip():
            q["user_answer"] = answer.strip()
            q["is_resolved"] = True

    req_state["clarification_questions"] = questions

    # Answers are evidence for a future audit, not proof that blocking findings
    # are fixed. Keep the project pending/invalid until a fresh Auditor run.
    unresolved = [q for q in questions if not q.get("is_resolved", False)]
    req_state["validation_status"] = "invalid"
    req_state["current_workflow_state"] = "WAITING_CLARIFICATION" if unresolved else "AUDIT_PENDING"

    # CLARIFY 3.6 — THE WRITE (PROJECT 8.2.2): the whole state (with the answered
    #             questions) is persisted, which also re-derives the child rows. Note
    #             this is the second write path besides CONFIRM 3.3 — unlike the AI
    #             merge it is user-authored data, so no pending action is needed.
    saved_state = await RequirementStateRepository.save_or_update(project_id, req_state, session)
    # CLARIFY 3.7 — Real-time fan-out with the new verdict so other clients update
    #            their audit/clarification views (EVENTS flow).
    await event_manager.publish(project_id, "clarification_submitted", {
        "project_id": project_id,
        "validation_status": req_state.get("validation_status"),
        "current_workflow_state": req_state.get("current_workflow_state"),
    })
    # CLARIFY 3.8 — Response = the saved state; CLARIFY 1.3 applies it (answers reset,
    #            agent node + audit verdict refreshed, project state re-loaded).
    return saved_state


# ==========================================
# GENERATION CANCELLATION API (Stop button)
# ==========================================

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

# CHAT 2.1 — Backend entry for the CHAT flow (and for every other intent): the
#            shared pipeline below authenticates (AUTH 7.1/7.4), enforces the
#            context budget, persists the user turn and detects intent (CHAT 2.2)
#            before choosing conversation (CHAT 2.3) or an agent workflow.
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
    # AUTHZ: the caller must own the project (which must exist) before the
    # workflow registers, reads, or persists anything for it. This also closes
    # the orphaned-conversation-rows hole: a bogus project_id is a 404 here,
    # never a half-written conversation record.
    await verify_project_access(str(request.project_id), current_user, session)

    current_task = asyncio.current_task()
    if current_task is not None:
        workflow_cancellations.register(str(request.project_id), current_task)
    try:
        return await _process_requirements_pipeline(request, session, current_user)
    except asyncio.CancelledError:
        # Hard-cancel from the Stop button. CancelledError is a BaseException,
        # so the pipeline's `except Exception -> HTTP 500` guard never swallows
        # it. Skip ALL persistence and report the cancellation instead.
        logger.warning(
            f"[CANCELLED] Generation for project {request.project_id} was stopped by the user; "
            "skipping all result persistence."
        )
        await session.rollback()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "cancelled",
                "project_id": str(request.project_id),
                "message": "Generation was stopped by the user before completion. No results were saved.",
            },
        )
    finally:
        if current_task is not None:
            workflow_cancellations.unregister(str(request.project_id), current_task)


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

    # Finding #39: enforce MAX_CONTEXT_TOKENS on the incoming processing payload
    # (raw_input + structured_requirements) before spending any LLM budget.
    # Runs BEFORE the "Triggering on-demand agent" log so a rejected request
    # never claims an agent run was started.
    validate_body_budget(request.model_dump(), "Process-requirements request")

    logger.info(f"Triggering on-demand {request.target_agent} agent for project {request.project_id}")

    # ==========================================
    # STEP 1: Persist every user message before any intent detection or agent routing
    # ==========================================
    if request.project_id and request.raw_input and request.raw_input.strip():
        await ConversationMessageRepository.save_message(
            project_id=str(request.project_id),
            role="user",
            message=request.raw_input.strip(),
            workflow_state=request.target_agent or "gatherer",
            intent="PENDING_DETECTION"
        )

    # ==========================================
    # INTENT 1.1 — Primary call site of the intent detector (pipeline STEP 2).
    #              Runs BEFORE the LangGraph workflow so the pipeline can pick the
    #              conversation branch (CHAT 2.3) or map the intent onto an agent
    #              (INTENT 1.4). The detector itself is INTENT 2.x.
    #              DB: requirement_states read below supplies the story context.
    # STEP 2: Intent Detection (before LangGraph)
    # ==========================================
    # INTENT 1.2 — Service import (module-local to keep the route module's import
    #              graph light; semantic_service pulls in the LLM factory).
    from app.semantic_service import detect_requirement_intent, generate_general_chat_response

    # Load the centralized RequirementState from Supabase (single source of truth)
    logger.info(f"[DB LOG] Loading RequirementState for processing project {request.project_id}")
    req_state = await RequirementStateRepository.get_by_project_id(request.project_id, session)
    persisted_version_at_start = req_state.get("version_number") if req_state else None
    logger.info(f"[DB LOG] Loading RequirementState for processing project {request.project_id} complete. Found: {req_state is not None}")

    # Detect intent on the raw input
    # INTENT 1.3 — Detector invocation: raw user text + the project's current user
    #              stories (context disambiguates "add X" from "explain X").
    #              Returns {intent, confidence, reason} — INTENT 2.0; consumed
    #              immediately below by CHAT 2.2 and INTENT 1.4.
    detected_intent_result = await detect_requirement_intent(
        request.raw_input or "",
        req_state.get("user_stories", []) if req_state else []
    )
    # CHAT 2.2 — Intent detection (LLM): the single decision point that turns this
    #            request into either conversation (CHAT 2.3) or a requirement
    #            operation. The default keeps an unparsable reply on the chat path.
    detected_intent = detected_intent_result.get("intent", "GENERAL_CHAT")
    # Propagated into the workflow so the Gatherer prompt can quote the real
    # classifier confidence (and hedge when the classification was uncertain)
    # instead of assuming a perfect 1.0 confidence.
    intent_confidence = float(detected_intent_result.get("confidence", 1.0) or 1.0)
    logger.info(f"[INTENT DETECTION] Detected intent: {detected_intent} (confidence: {detected_intent_result.get('confidence', 0.0)})")

    # ==========================================
    # CHAT 2.3 — GENERAL_CHAT short-circuit: the chat flow's backend branch. Runs
    #            INSTEAD of the LangGraph workflow, performs no artifact/version
    #            writes, and returns a conversational reply (CHAT 2.4 → CHAT 1.4).
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
                logger.exception(f"[GENERAL_CHAT] Failed to load conversation history from DB")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to load conversation history from database: {str(db_err)}",
                ) from db_err

        # Generate response with full project context
        try:
            # CHAT 2.3.2 — LLM generation (service layer, CHAT 2.3.2 in
            #             app/semantic_service.py): project context + history →
            #             LangChain ChatOpenAI → text. No agent, no memory write.
            response_text = await generate_general_chat_response(
                raw_input=request.raw_input or "",
                project_context=req_state or {},
                conversation_history=conv_history
            )
        except Exception as llm_err:
            # Intentional graceful degradation: when the LLM call fails we log the
            # error (with traceback) and return a canned user-facing fallback instead
            # of a 500. This does not corrupt state or alter the caller's data.
            logger.error(f"[GENERAL_CHAT] Failed to generate LLM response: {str(llm_err)}", exc_info=True)
            response_text = "I understand your question, but I'm currently experiencing some technical difficulties. Please try again in a moment."

        # Persist assistant response (with error handling for DB failures)
        if request.project_id and response_text:
            try:
                await ConversationMessageRepository.save_message(
                    project_id=str(request.project_id),
                    role="assistant",
                    message=response_text,
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

        # CHAT 2.4 — Response contract for the conversation path: status
        #            "general_chat" + the reply text, with the project's CURRENT
        #            artifacts echoed back unchanged (nothing was written). The UI
        #            branches on `detected_intent` in CHAT 1.4.
        # Return response (no project artifacts modified)
        return {
            "status": "general_chat",
            "detected_intent": "GENERAL_CHAT",
            "message": response_text,
            "structured_requirements": {
                "epic_name": (
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
    # INTENT 1.4 — Intent → agent mapping (the second consumption point, after the
    #              GENERAL_CHAT branch). Only the four requirement intents appear
    #              here; GENERAL_CHAT never reaches this code (CHAT 2.3 returned
    #              already), and an explicit request.target_agent sent by the UI
    #              buttons survives because those intents are absent from the map.
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
        # User messages treated as clarification answers
        request.target_agent = "auditor"
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

    # Formulate initial state corresponding to AgentState TypedDict
    knowledge_documents = []
    if request.target_agent == "auditor":
        knowledge_documents = await DocumentRepository.get_validation_context(request.project_id, session)
        try:
            from app.banking_knowledge_service import build_audit_retrieval_query, retrieve_banking_knowledge

            banking_context = await retrieve_banking_knowledge(
                current_user.id,
                str(request.project_id),
                build_audit_retrieval_query(req_state or {}),
                session,
            )
            knowledge_documents.extend(banking_context)
            logger.info(
                "[BANKING RAG] Retrieved %d approved knowledge chunk(s) for project %s",
                len(banking_context), request.project_id,
            )
        except Exception as rag_err:
            # Project documents still provide the established audit evidence
            # path if the reusable corpus or embedding service is unavailable.
            logger.warning("[BANKING RAG] Retrieval unavailable; continuing without it: %s", rag_err)

    initial_state = {
        "project_id": request.project_id,
        "raw_input": request.raw_input or "",
        "structured_requirements": {
            "epic_name": (
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
        final_state = await prd_workflow.ainvoke(initial_state)

        # Read updated centralized RequirementState from final graph output
        req_state = final_state.get("requirement_state", req_state)

        # Update workflow state after run
        if request.target_agent == "gatherer" or current_state == "GATHERING":
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
            if latest_version != persisted_version_at_start:
                stale_result = True
                logger.warning(
                    "AI result for project %s is stale (started at version %s, latest is %s); returning without a pending merge",
                    request.project_id, persisted_version_at_start, latest_version,
                )

        # ==========================================
        # IN-MEMORY MERGE: Store merged state as a pending action instead of directly persisting to DB.
        # The user must confirm before changes are committed to the database.
        # ==========================================

        audit_operational_error = bool(final_state.get("audit_operational_error"))
        audit_auto_persisted = False
        architect_auto_persisted = False
        logger.info(f"[IN-MEMORY MERGE] Storing merged state as pending action for project {request.project_id}...")

        # Architect generation changes document artifacts, not the gathered
        # requirements. Persist the generated PRD/diagram immediately and append
        # its immutable version without sending the unchanged requirements
        # through the generic merge dialog (which previously made the first PRD
        # appear in preview but not in Version History).
        if request.target_agent == "architect" and not stale_result:
            generated_prd = str(req_state.get("generated_prd") or "").strip()
            if not generated_prd:
                raise RuntimeError("Architect completed without generating a PRD document")
            from app.prd_section_service import sync_sections_from_prd
            from app.version_service import record_prd_version

            merged_prd = await sync_sections_from_prd(
                request.project_id,
                generated_prd,
                session,
                changed_by="automated_agent",
            )
            final_prd = merged_prd or generated_prd
            version_record = await record_prd_version(
                request.project_id,
                session,
                generated_prd=final_prd,
                generated_by="automated_agent",
                change_type="ai",
            )
            req_state = await RequirementStateRepository.save_or_update(
                request.project_id,
                {
                    "generated_prd": final_prd,
                    "generated_diagrams": req_state.get("generated_diagrams", ""),
                    "current_workflow_state": "COMPLETED",
                },
                session,
            )
            final_state["requirement_state"] = req_state
            architect_auto_persisted = True
            logger.info(
                "Architect artifacts persisted as PRD version %s for project %s",
                version_record.get("version_number"), request.project_id,
            )

        # GATHERING 8.0 — Handoff to the CONFIRMATION flow: the merged state is NOT
        #             persisted. It is staged as a pending action carrying the whole
        #             `proposed_changes` payload (action_type="MERGE"); the user's Save
        #             (confirm-action, CONFIRMATION flow) is the only writer.
        #             DB: pending_actions INSERT. The response then triggers the
        #             debounced SSE refresh and the client's merge preview (8.1).
        # Create a pending action with the full merged state as proposed changes
        pending_action_id = None
        if request.target_agent == "auditor" and not audit_operational_error and not stale_result:
            # An audit changes only audit metadata and clarification questions;
            # it is not a proposed requirements edit. Persist those fields
            # immediately so questions are answerable by stable database IDs,
            # without forcing a redundant Save/Merge confirmation.
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
        elif not architect_auto_persisted and not audit_operational_error and not stale_result:
            pending_action = await PendingActionRepository.create(
                project_id=request.project_id,
                data={
                    "action_type": "MERGE",
                    "target_requirement_id": None,
                    "original_user_message": request.raw_input or "",
                    "proposed_changes": req_state,
                    "affected_user_story_ids": [],
                    "affected_acceptance_criteria_ids": [],
                    "workflow_stage": req_state.get("current_workflow_state", "gatherer_node")
                },
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                session=session
            )
            pending_action_id = pending_action.get("id")
            logger.info(f"[IN-MEMORY MERGE] Pending action {pending_action_id} created. Awaiting user confirmation.")
        elif audit_operational_error:
            logger.warning("Auditor operational failure: no pending action created for project %s", request.project_id)
        else:
            logger.warning("Stale AI result: no pending action created for project %s", request.project_id)

        # ==========================================
        # STEP 5: Persist assistant response for non-GENERAL_CHAT intents
        # ==========================================
        agent_message = final_state.get("agent_message", "")
        if request.project_id and agent_message:
            await ConversationMessageRepository.save_message(
                project_id=str(request.project_id),
                role="assistant",
                message=agent_message,
                workflow_state=req_state.get("current_workflow_state", request.target_agent or "gatherer"),
                intent=detected_intent
            )

        # Determine status dynamically based on centralized validation status
        is_valid = req_state.get("validation_status") == "valid"
        status_str = "stale" if stale_result else ("completed" if is_valid else "audit_pending")

        # Format structures back to preserve frontend compatibility
        reqs_data = req_state.get("requirements", [])
        if isinstance(reqs_data, list):
            # Multi-requirement format
            epic_name = reqs_data[0].get("title", "") if reqs_data and isinstance(reqs_data[0], dict) else ""
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

        return {
            "status": status_str,
            "workflow_routing": workflow_routing,
            "detected_intent": final_state.get("detected_intent") or req_state.get("detected_intent", detected_intent),
            "structured_requirements": structured_out,
            "audit_result": audit_out,
            "prd_markdown": req_state.get("generated_prd", ""),
            "mermaid_diagram": req_state.get("generated_diagrams", ""),
            "message": agent_message,
            "pending_merge": not audit_operational_error and not stale_result and not audit_auto_persisted and not architect_auto_persisted,
            "pending_action_id": pending_action_id,
            "stale_result": stale_result,
            "audit_auto_persisted": audit_auto_persisted,
        }
    except HTTPException:
        # Preserve intentional 4xx responses (notably validation-context 413s)
        # instead of disguising them as a generic LangGraph 500.
        raise
    except LMStudioUnavailableError as e:
        logger.error(f"LM Studio unreachable during workflow for project {request.project_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The local inference gateway (LM Studio) is unreachable. Start LM Studio with a loaded model and retry the generation.",
        )
    except LMStudioGatewayError as e:
        logger.error(f"LM Studio gateway error during workflow for project {request.project_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The local inference gateway rejected the generation request. Retry once the model is responsive.",
        )
    except LMStudioOutputParsingError as e:
        logger.error(f"LM Studio returned an unreadable payload for project {request.project_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The model returned an unreadable response. Please retry the generation.",
        )
    except RuntimeError as e:
        # invoke_llm_structured exhausts both of its parse attempts (e.g. a
        # reasoning model burning its whole token budget on thinking content).
        # That is a retryable model-output failure, not a server bug — 503, not 500.
        logger.error(f"Workflow produced no usable structured output for project {request.project_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The audit or generation could not complete. Please try again. Your previous saved results have not been replaced.",
        )
    except Exception as e:
        logger.error(f"Multi-agent workflow execution failed for project {request.project_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LangGraph multi-agent workflow execution failed: {str(e)}"
        )


# ==========================================
# WORKFLOW ROUTER / INTENT DETECTOR / MATCHER API
# ==========================================

# INTENT 4.1.1 — Shared context loader for the detector/matcher endpoints: one DB
#               read of the project's user stories that FAILS LOUD (HTTP 500) rather
#               than degrading to an empty list — an empty list would silently make
#               intent detection run without project context (misleading output).
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

# ROUTING 5.1 — Debug/manual endpoint for the classifier: POST /api/workflow-router.
#             Guards: rate limit ("workflow" scope, 429) → AUTH 7.1 identity →
#             validate_text_budget (413). Then ROUTING 5.1.2.
#             DISCREPANCY (documented, not changed): no frontend caller — the SPA
#             reaches the classifier only indirectly through the graph (ROUTING
#             2.1), and `workflow_routing` in the response is typed in
#             src/api/types.ts but never rendered. LLM-cost surface, API/tests only.
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

    # Finding #39: enforce MAX_CONTEXT_TOKENS on the incoming message.
    validate_text_budget(payload.message, "Workflow-router message")
    # ROUTING 5.1.2 — Same classifier the graph uses (ROUTING 3.0); the result is
    #               returned verbatim, so this endpoint can be used to reproduce a
    #               routing decision without running the workflow.
    return await classify_workflow(payload.message)


# INTENT 4.1 — Debug/manual endpoint for the detector: POST /api/intent-detector.
#              Guards: rate limit ("workflow" scope, 429) → AUTH 7.1 identity →
#              validate_text_budget (413) → optional project scoping (INTENT 4.1.1/
#              4.1.2). DISCREPANCY (documented, not changed): no frontend caller —
#              src/api/client.ts exposes no method for it, so this LLM-cost surface
#              is only reachable by API clients/tests. Kept for parity with the
#              pipeline path (INTENT 1.x).
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

    # Finding #39: enforce MAX_CONTEXT_TOKENS on the incoming message.
    validate_text_budget(payload.message, "Intent-detector message")

    current_stories = []
    if payload.project_id:
        # AUTHZ: project stories are tenant data (titles/descriptions that get
        # injected into the LLM prompt). The caller must own the project before
        # they are loaded — otherwise any authenticated user could probe another
        # project's story content by passing its id here.
        await verify_project_access(str(payload.project_id), current_user, db)
        # INTENT 4.1.2 — Repository read of the caller's own stories (AUTHZ above):
        #               this is prompt context, i.e. tenant data.
        current_stories = await _load_current_stories(payload.project_id, db)

    # INTENT 4.1.3 — Same detector as the pipeline (INTENT 2.0), returned verbatim.
    result = await detect_requirement_intent(payload.message, current_stories)
    return result


# MATCHING 5.1 — Debug/manual endpoint for the matcher: POST /api/requirement-matcher.
#             Guards: rate limit ("workflow" scope, 429) → AUTH 7.1 identity →
#             validate_text_budget (413) → optional project scoping (AUTH 7.4 +
#             _load_current_stories, INTENT 4.1.1/4.1.2) → intent reuse or INTENT
#             2.x detection when the payload omits it. Then MATCHING 5.1.2.
#             DISCREPANCY (documented, not changed): no frontend caller — src/api/
#             client.ts exposes no method for it, so this LLM-cost surface is API/
#             test-only, exactly like INTENT 4.1 and ROUTING 5.1.
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
    from app.semantic_service import match_requirement, detect_requirement_intent

    # Finding #39: enforce MAX_CONTEXT_TOKENS on the incoming message.
    validate_text_budget(payload.message, "Requirement-matcher message")

    current_stories = []
    if payload.project_id:
        # AUTHZ: same tenant-data gate as the intent detector — the caller must
        # own the project before its stories are loaded for matching.
        await verify_project_access(str(payload.project_id), current_user, db)
        current_stories = await _load_current_stories(payload.project_id, db)

    intent = payload.detected_intent
    if not intent:
        intent_res = await detect_requirement_intent(payload.message, current_stories)
        intent = intent_res.get("intent", "UPDATE")

    # MATCHING 5.1.2 — Same matcher the graph uses (MATCHING 2.0), with the intent
    #               either supplied by the caller or detected here (INTENT 2.0).
    result = await match_requirement(payload.message, intent, current_stories)
    return result

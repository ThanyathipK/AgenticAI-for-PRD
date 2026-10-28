import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user, verify_project_access
from app.config import settings
from app.database import get_db
from app.event_manager import event_manager
from app.input_validation import validate_messages_budget
from app.llm_client import (
    LMStudioGatewayError,
    LMStudioOutputParsingError,
    LMStudioUnavailableError,
    call_lm_studio,
    check_lm_studio_health,
)
from app.rate_limit import rate_limit_dependency
from app.repositories import ConversationMessageRepository
from app.schemas import ChatResponse, ChatSessionRequest, HealthResponse
from app.semantic_memory import build_memory_block, extract_and_remember, recall

logger = logging.getLogger("app.routes.chat")

router = APIRouter()


# CHAT 6.2.1 — Return backend and inference-gateway readiness.
# Trigger: CHAT 6.3.1 GET /api/health or an external health client.
# Calls: CHAT 6.2 check_lm_studio_health.
# Writes: NONE beyond the probe's in-process TTL cache.
# Failure: always returns HTTP 200 when the backend can answer; gateway failure is
#          represented in the response body.
@router.get("/api/health", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def get_health_status(force: bool = False) -> HealthResponse:
    """
    Readiness probe combining app liveness with a real LM Studio check (Finding #40).

    Runs a short ``GET /models`` round-trip against the configured local gateway
    (TTL-cached server-side) so callers can detect an offline LLM immediately and
    degrade gracefully. The app itself still answers ``200 online`` even when the
    LLM is down — connectivity is reported through the ``lm_studio_online`` /
    ``lm_studio_error`` fields instead of an error status.

    Returns:
        HealthResponse: Application identity, configured inference gateway and
        context-window budget, plus live LM Studio readiness information.
    """
    # CHAT 6.2 — LM Studio probe (TTL-cached server-side, see llm_client).
    lm_health = await check_lm_studio_health(force=force)
    return {
        "status": "online",
        "app_name": settings.APP_NAME,
        "local_inference_gateway": settings.LM_STUDIO_URL,
        "macbook_context_window_budget": f"{settings.MAX_CONTEXT_TOKENS} tokens",
        "lm_studio_online": lm_health["online"],
        "lm_studio_model": lm_health["target_model"],
        "lm_studio_model_loaded": lm_health["model_loaded"],
        "lm_studio_loaded_models": lm_health["loaded_models"],
        "lm_studio_latency_ms": lm_health["latency_ms"],
        "lm_studio_error": lm_health["error"],
        "lm_studio_last_checked": lm_health["checked_at"],
    }


# CHAT 5.1 — Serve the standalone memory-aware conversation API.
# Trigger: API/test client POST /api/chat; the SPA uses CHAT 1.3 instead.
# Input: conversation messages, optional project id, AUTH 7.1 identity, and DB session.
# Calls: guards → CHAT 5.2 memory recall → CHAT 5.3 prompt/user persistence →
#        CHAT 5.4 LLM → CHAT 5.5/5.6/5.7 downstream effects.
# Branches: project chat adds AUTH 7.4 ownership and memory/transcript operations.
# Writes: conversation rows, semantic memories, and SSE event when project-scoped.
# Failure: empty input 400, oversized context 413, LLM errors 422/502/503.
@router.post("/api/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def post_chat_query(
    request: ChatSessionRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    # CHAT 5.1 (defect note) — `AsyncSession` is used in this annotation but is NOT
    # imported in this module. It survives only because Python 3.14 defers
    # annotation evaluation (PEP 649); on Python <= 3.13 this line raises NameError
    # at import time. Reported in the analysis; deliberately NOT fixed here.
    session: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(
        rate_limit_dependency(
            "chat", settings.RATE_LIMIT_CHAT_LIMIT, settings.RATE_LIMIT_CHAT_WINDOW
        )
    ),
) -> ChatResponse:
    """
    Orchestrates requirements dialogue prompts with the local LLM.

    Acts as the entrypoint for LangGraph multi-agent context chains. Persists
    each user/assistant turn (when a ``project_id`` is provided) and notifies
    SSE subscribers of new assistant replies.

    Args:
        request: Conversation timeline plus optional project context.
        _rate_limit: Injected rate limiter (Finding #39) — rejects excess
            requests with HTTP 429 before any LLM work happens.

    Returns:
        ChatResponse: The assistant's generated reply text.

    Raises:
        HTTPException: 400 if the message list is empty; 413 if the combined
            message history exceeds ``MAX_CONTEXT_TOKENS``.
    """
    # CHAT 5.1.1 — Validation branch: an empty conversation is a client error.
    if not request.messages:
        raise HTTPException(status_code=400, detail="Conversation message list cannot be empty.")

    # AUTHZ: a project-bound chat may only read/write the caller's own project
    # (conversation rows are persisted below whenever project_id is set).
    if request.project_id:
        # CHAT 5.1.2 — AUTHZ: a project-bound chat may only touch its owner's data
        #             (both the memory read and the message writes below are
        #             project-scoped), so ownership is verified up front (AUTH 7.4).
        await verify_project_access(str(request.project_id), current_user, session)

    # Finding #39: enforce MAX_CONTEXT_TOKENS on incoming payload before any
    # token budget is consumed by the LLM gateway.
    # CHAT 5.1.3 — Budget gate: reject the payload before any LLM tokens are spent.
    validate_messages_budget(request.messages)

    # --- Semantic memory recall (fail-open) --------------------------------
    # Before the LLM call, retrieve the most relevant distilled memories for
    # this project (scored on the CURRENT user message) and inject them as a
    # hard-capped block into the system prompt. Any failure degrades to the
    # pre-memory behaviour — chat never breaks because memory is down.
    # CHAT 5.2 — Semantic-memory recall (fail-open): scored on the CURRENT user
    #            message and injected as a hard-capped prompt block. The nested
    #            try/except is belt-and-braces because recall itself never raises.
    memory_block = ""
    if request.project_id and settings.MEMORY_ENABLED:
        current_message = request.messages[-1].content
        try:
            # CHAT 5.2.1 — Read path (CHAT 3.1): embed the query, score the
            #             candidate window, drop zero-cosine hits, mark usage.
            # CHAT 5.2.2 — Render path (CHAT 3.2): token-capped bullet block; ""
            #             when nothing is relevant, so the append below is safe.
            memories = await recall(str(request.project_id), current_message)
            memory_block = build_memory_block(memories)
        except Exception as mem_err:  # belt & braces: recall is already fail-open
            logger.warning("Memory recall skipped (%s).", mem_err)

    # CHAT 5.3 — Assemble the LLM prompt and persist the newest user turn.
    # Trigger: CHAT 5.1 guards and CHAT 5.2 optional recall complete.
    # Input: system policy, bounded memory block, and request conversation.
    # Calls: CHAT 7.1 for the newest project-scoped user message.
    # Writes: one user conversation row when project_id and a user turn exist.
    # Next: CHAT 5.4 direct LLM transport.
    system_instruction = (
        "You are an expert Technical Product Owner Assistant specializing in core banking requirement designs. "
        "Adhere to Krungsri Nimble standards, ensure high compliance, security OTP mechanics, and double-entry general ledgers. "
        f"Strictly keep your replies inside a total contextual window budget of {settings.MAX_CONTEXT_TOKENS} tokens."
    )
    if memory_block:
        system_instruction = (
            f"{system_instruction}\n\n{memory_block}\n"
            "Use the remembered project context when it is relevant; never contradict a "
            "recorded user decision without saying so."
        )

    formatted_messages = [{"role": "system", "content": system_instruction}]
    for msg in request.messages:
        formatted_messages.append({"role": msg.role, "content": msg.content})

    # Persist only the newest user turn. Clients resend the timeline for LLM
    # context, so storing every supplied message would duplicate history on
    # every request.
    if request.project_id:
        newest_user = next((msg for msg in reversed(request.messages) if msg.role == "user"), None)
        if newest_user is not None:
            await ConversationMessageRepository.save_message(
                project_id=str(request.project_id), role="user", message=newest_user.content,
                workflow_state="chat", intent="GENERAL_CHAT", session=session,
            )

    logger.info("Initiating conversational analyst run.")
    try:
        # CHAT 5.4 — LLM transport (CHAT 6.1): direct LM Studio call, no LangGraph.
        #            Failures map onto the API contract below: unparsable JSON 422,
        #            gateway down 503, other gateway errors 502.
        response = await call_lm_studio(formatted_messages)
    except LMStudioOutputParsingError as err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(err))
    except LMStudioUnavailableError as err:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(err))
    except LMStudioGatewayError as err:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))
    reply_text = response.get("text", "")

    # CHAT 5.5 — Repository WRITE of the assistant turn (skipped for an empty reply).
    if request.project_id and reply_text:
        await ConversationMessageRepository.save_message(
            project_id=str(request.project_id),
            role="assistant",
            message=reply_text,
            workflow_state="chat",
            intent="GENERAL_CHAT",
            session=session,
        )
        # Semantic memory write path (fail-open): distil durable facts from
        # this finished turn so FUTURE conversations understand the project
        # better. Runs after the reply is persisted; failures are logged and
        # swallowed — the user's reply is already delivered either way.
        last_user_message = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        # CHAT 5.6 — Semantic-memory WRITE path (CHAT 4.1): distill durable facts
        #            from this finished turn and store them for future recalls.
        #            Fail-open — extract_and_remember returns 0 instead of raising,
        #            so the already-delivered reply can never be rolled back.
        if last_user_message and settings.MEMORY_FACT_EXTRACTION_ENABLED:
            await extract_and_remember(
                project_id=str(request.project_id),
                user_message=last_user_message,
                assistant_reply=reply_text,
            )
        # Notify SSE subscribers so other connected clients refresh their conversation view.
        # CHAT 5.7 — Real-time fan-out, mirroring the pipeline branch's SSE frame
        #            (CHAT 2.3.4) so both chat paths look identical to clients.
        await event_manager.publish(str(request.project_id), "chat_reply", {
            "project_id": str(request.project_id),
            "role": "assistant",
        })

    return {"message": reply_text}

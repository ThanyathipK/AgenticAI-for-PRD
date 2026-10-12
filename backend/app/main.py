import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.migrations import run_migrations, seed_default_user
from app.event_manager import verify_single_worker_guarantee as verify_sse_single_worker_guarantee
from app.rate_limit import verify_single_worker_guarantee
from app.routes import auth, chat, projects, requirements, lock, events, documents
from app.routes import prd_sections
from app.routes import traceability
from app.routes import operational
from app.routes import banking_knowledge
from app.routes import generation_jobs
from app.llm_client import check_lm_studio_health

# Logger initialization
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the application lifecycle.

    Runs versioned Alembic migrations and seeds the default user on startup,
    replacing the deprecated ``@app.on_event("startup")`` handler. Also performs
    a graceful LM Studio reachability probe (Finding #40): the app still boots
    when the local LLM is offline, but a prominent warning is logged so operators
    and the UI (which surfaces the same signal via ``GET /api/health``) can react.
    """
    logger.info("Application startup: running migrations and seeding data...")

    # Finding #39 hardening — fail loud instead of letting 429 protection
    # silently disappear. Refuses to boot when the in-process limiter is paired
    # with multiple uvicorn workers (each worker would keep its own counter),
    # unless the operator explicitly opts into that weaker posture.
    verify_single_worker_guarantee()

    # SSE event-bus hardening — same single-process contract as the rate limiter.
    # The in-process subscriber queues + replay buffer are process-local; with
    # multiple workers events published on one worker would never reach SSE
    # clients connected to another (missing/duplicate streams). This guard is
    # active even when rate limiting is disabled, so the SSE single-process
    # assumption is never silently violated.
    verify_sse_single_worker_guarantee()

    await run_migrations()
    await seed_default_user()

    # Persistent background work survives browser disconnects. Jobs left in a
    # queued/running state by a process restart are safely requeued because the
    # actual workflow writes are transaction-bound and idempotently staged.
    from app.generation_job_service import recover_generation_jobs
    recovered_jobs = await recover_generation_jobs()
    if recovered_jobs:
        logger.info("Recovered %d queued generation job(s) after restart.", recovered_jobs)

    # Backfill: parts whose newest history row is a human manual edit become
    # human-owned, so manual edits saved before the ownership flag existed are
    # not overwritten by the next "Generate PRD". Idempotent + fail-soft: a
    # bookkeeping failure must never block the app from booting.
    try:
        from app.database import AsyncSessionLocal
        from app.repositories.prd_section import PRDSectionRepository
        async with AsyncSessionLocal() as session:
            flipped = await PRDSectionRepository.mark_manually_edited_as_human_owned(session)
            if flipped:
                await session.commit()
                logger.info(
                    "[PRD SECTIONS] Marked %d manually-edited part(s) as human-owned "
                    "(protected from AI regeneration).", flipped,
                )
    except Exception as backfill_err:  # pragma: no cover - defensive
        logger.warning("PRD section ownership backfill failed: %s", backfill_err)

    # Finding #40 — startup check: probe LM Studio without crashing the app.
    # The health probe never raises; this extra guard is belt-and-braces.
    try:
        llm_health = await check_lm_studio_health()
        if llm_health["online"]:
            logger.info(
                f"LM Studio startup check passed: gateway={settings.LM_STUDIO_URL} "
                f"model={llm_health['loaded_models'] or 'none loaded'} "
                f"({llm_health['latency_ms']} ms)"
            )
        else:
            logger.warning(
                "LM Studio is OFFLINE at startup (%s). The UI will show an 'LM "
                "Studio offline' banner and inference calls will return HTTP 503 "
                "until the local server (localhost:1234) is started and the model "
                "'%s' is loaded.",
                llm_health["error"] or "unreachable",
                llm_health["target_model"],
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("LM Studio startup health check failed unexpectedly: %s", exc)

    yield
    # Any graceful-shutdown / cleanup logic belongs here (after ``yield``).
    from app.generation_job_service import shutdown_generation_jobs
    await shutdown_generation_jobs()
    logger.info("Application shutdown complete.")


# ==========================================
# OPENAPI / DOCS METADATA
# ==========================================
# Tags group endpoints into logical domains within the auto-generated /docs
# and /openapi.json documentation.
openapi_tags = [
    {
        "name": "System",
        "description": "Liveness, health checks and general chat with the local LLM.",
    },
    {
        "name": "Projects",
        "description": "Project CRUD, requirement-state access, conversations, PRD export and PRD version history.",
    },
    {
        "name": "Requirements & Workflow",
        "description": "Requirement CRUD/locking, the multi-agent processing pipeline, and semantic test endpoints (workflow routing, intent detection, requirement matching).",
    },
    {
        "name": "Locking & Pending Actions",
        "description": "Generic artifact locking, lock-status, and human-in-the-loop pending merge actions.",
    },
    {
        "name": "Events & SSE",
        "description": "Artifact event log queries and the Server-Sent Events (SSE) realtime push stream.",
    },
    {
        "name": "Traceability",
        "description": "Derived Requirement Traceability Matrix linking requirements, user stories, acceptance criteria, PRD sections and diagrams.",
    },
    {
        "name": "Operational Governance",
        "description": "Project health, audit history, dependency graphs, and scoped regeneration.",
    },
    {
        "name": "Banking Knowledge",
        "description": "Reusable approved banking guidance and relevance retrieval for audits.",
    },
]

# Instantiate FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Asynchronous FastAPI Core for Banking-grade Local LLM (LM Studio) & "
        "Multi-Agent orchestration.\n\n"
        "This service drives the **agenticdia** product: it orchestrates a "
        "LangGraph multi-agent workflow (Gatherer, Auditor, Architect) that "
        "parses raw banking requirements into structured user stories, runs "
        "Krungsri Nimble compliance audits, generates PRD documents and "
        "architecture diagrams, and enforces artifact-level locking with "
        "human-in-the-loop merge confirmation.\n\n"
        "Interactive API documentation is generated below. The raw OpenAPI "
        "specification is available at `/openapi.json`."
    ),
    version="1.2.0",
    openapi_tags=openapi_tags,
    contact={
        "name": "agenticdia",
        "url": "https://github.com/",
    },
    license_info={
        "name": "Proprietary",  # Adjust to the project's actual license.
    },
    lifespan=lifespan,
)


# Enterprise CORS middleware with explicit allowed origins.
# allow_credentials is intentionally omitted (False) because the frontend
# does not send cookies/auth headers, and "*" + credentials is invalid per spec.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-PRD-SemVer"],
)

# GZip response compression. The biggest payloads are the project-state reads,
# which ship the FULL PRD markdown and the entire conversation history on every
# request; compressing responses over 1KB typically cuts transfer size 5-10x
# for markdown-heavy JSON. This is a pure wire-level win — no client changes.
# (SSE `text/event-stream` frames are tiny and are flushed per chunk, so live
# update latency is unaffected.)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ==========================================
# ROUTER REGISTRATION
# ==========================================
# Each router module handles a distinct domain:
#   - chat:        /api/health, /api/chat
#   - projects:    /api/projects, /api/project/{id}, PRD export & versions
#   - requirements:/api/project/{id}/requirements, /api/process-requirements,
#                  /api/workflow-router, /api/intent-detector, /api/requirement-matcher,
#                  /api/audit/respond, /api/clarification/submit
#   - lock:        /api/project/{id}/artifacts/*/lock|unlock|lock-status,
#                  /api/pending-actions, /api/confirm-action, /api/cancel-action
#   - events:      /api/events, /api/project/{id}/events, /api/project/{id}/sse (SSE push stream)
#   - documents:   /api/project/{id}/documents upload/list/get + DRAFT-only process endpoint
#   - traceability:/api/project/{id}/traceability (derived matrix)
# AUTH 1.7.0 — Router mount: this single include registers EVERY /api/auth/* route
#              documented below (AUTH 1.7 login, 2.3 me, 3.3 config, 4.4 logout,
#              5.1 change-password, 6.3 register). Anonymous here on purpose —
#              per-route dependencies (AUTH 7.1) do the enforcement.
app.include_router(auth.router, tags=["Authentication"])
app.include_router(chat.router, tags=["System"])
app.include_router(projects.router, tags=["Projects"])
app.include_router(requirements.router, tags=["Requirements & Workflow"])
app.include_router(lock.router, tags=["Locking & Pending Actions"])
app.include_router(events.router, tags=["Events & SSE"])
app.include_router(documents.router, tags=["Documents"])
# PRD part-level editing / locking / versioning (the nine preview parts)
app.include_router(prd_sections.router, tags=["PRD Sections"])
# Derived Requirement Traceability Matrix (requirements <-> stories <-> criteria
# <-> PRD sections <-> diagrams)
app.include_router(traceability.router, tags=["Traceability"])
app.include_router(operational.router, tags=["Operational Governance"])
app.include_router(banking_knowledge.router, tags=["Banking Knowledge"])
app.include_router(generation_jobs.router, tags=["Background Generation Jobs"])


# ==========================================
# CENTRALIZED ERROR HANDLERS
# ==========================================
# Standardize how client errors (405 Method Not Allowed, 415 Unsupported Media Type,
# 429 Too Many Requests, 404, etc.) and body-validation failures are surfaced. Every
# handler returns a consistent JSON ``{"detail": ...}`` body and, critically, logs the
# failure loudly instead of swallowing it — so transient/outage errors remain visible
# to operators via the application logs rather than being silently masked.
# (Review finding: "silent error swallowing".)

@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Log server errors loudly, then return a structured JSON error body.

    All ``HTTPException``-derived errors (including 404, 405, 415 and the 429 raised
    by the rate limiter) flow through here. ``status_code``/``detail``/``headers`` are
    preserved so, for example, the rate limiter's ``Retry-After`` header and message
    are not lost.
    """
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error(
            "Unhandled HTTP error on %s %s (status=%s)",
            request.method,
            request.url.path,
            exc.status_code,
            exc_info=True,
        )
    elif exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        logger.warning("Rate limited on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a structured 422 for malformed bodies / unsupported content types."""
    errors = exc.errors()
    logger.warning(
        "Request validation failed on %s %s: %s",
        request.method,
        request.url.path,
        errors,
    )
    # Pydantic v2 embeds the original exception object in each issue's ``ctx``
    # (e.g. ``{'error': ValueError(...)}`` when a custom ``field_validator``
    # raises), which is NOT JSON serializable — returning ``exc.errors()``
    # verbatim would crash this response with a 500. Render each issue's
    # ``ctx`` values down to plain strings so the body is always JSON-safe.
    safe_errors = [
        {**error, "ctx": {key: str(value) for key, value in error["ctx"].items()}}
        if error.get("ctx")
        else error
        for error in errors
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": safe_errors},
    )


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting server on http://0.0.0.0:8000")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)

"""JOB 5.0 — Persistent, cancellable single-project background execution."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.application_types import BackgroundJobState
from app.auth import AuthenticatedUser
from app.config import settings
from app.database import AsyncSessionLocal
from app.errors import classify_exception
from app.models import GenerationJobModel, UserModel
from app.observability import LogContext, log_event
from app.schemas import ProcessRequirementsRequest

logger = logging.getLogger("app.generation_jobs")

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
_tasks: dict[str, asyncio.Task] = {}
_heavy_inference_semaphore = asyncio.Semaphore(
    max(1, settings.LLM_MAX_CONCURRENT_HEAVY_JOBS)
)


def _public_failure(exc: Exception) -> str:
    """Return a useful category without leaking parser prompts or DB details."""
    message = str(exc).lower()
    if "there are no requirements to generate" in message:
        return "There are no requirements to generate."
    if isinstance(exc, asyncio.TimeoutError) or "timed out" in message or "timeout" in message:
        return "timeout: The local model did not finish within the job deadline."
    if "connect" in message or "lm studio" in message:
        return "llm_unavailable: LM Studio is not reachable or has no responsive model loaded."
    if "above the configured safe limit" in message or "context" in message and "limit" in message:
        return "context_limit: The audit input is too large for a reliable local-model run. Narrow or split the knowledge documents."
    if "parse" in message or "structured" in message or "auditor could not complete" in message:
        return "invalid_model_output: The local model did not return a valid structured result."
    return "workflow_error: The background workflow failed. Check the server log for details."


# Public row projection used by every JOB 4.x response (local helper; unnumbered).
def serialize_job(job: GenerationJobModel) -> BackgroundJobState:
    return {
        "id": str(job.id),
        "project_id": str(job.project_id),
        "job_type": job.job_type,
        "status": job.status,
        "progress_stage": job.progress_stage,
        "result": job.result_payload,
        "error_message": job.error_message,
        "cancel_requested": bool(job.cancel_requested),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# JOB 5.1 — Create one queued durable job.
# Trigger: JOB 4.1 or JOB 4.2 after authorization/preconditions.
# Input: project/user/type plus JSON-encodable request payload.
# Calls: JOB 7.1 persistence constraint.
# Branches: narrow interrupted automatic regeneration → supersede; other active → 409.
# Writes: generation_jobs row; route commits before launch.
# Failure: uniqueness IntegrityError rolls back the caller session and becomes 409.
async def create_job(project_id: str, user_id: str, job_type: str, payload: dict[str, Any], session) -> BackgroundJobState:
    existing = await session.scalar(
        select(GenerationJobModel).where(
            GenerationJobModel.project_id == uuid.UUID(str(project_id)),
            GenerationJobModel.status.in_(ACTIVE_STATUSES),
        ).limit(1)
    )
    if existing is not None:
        existing_payload = existing.request_payload if isinstance(existing.request_payload, dict) else {}
        interrupted_automatic_cascade = (
            existing.job_type == "regeneration"
            and bool(existing_payload.get("automatic"))
            and existing.status == "queued"
            and existing.progress_stage == "interrupted_by_shutdown"
            and not bool(payload.get("automatic"))
        )
        if interrupted_automatic_cascade:
            # A restart race can occur when the new process performs its startup
            # recovery scan just before the old process marks an interrupted
            # automatic cascade as queued. That orphan must not permanently
            # block an explicit user action. Supersede only this narrow stale
            # state; genuinely queued/running user jobs retain the 409 guard.
            existing.status = "cancelled"
            existing.progress_stage = "superseded_by_user_request"
            existing.cancel_requested = True
            existing.completed_at = datetime.now(timezone.utc)
            await session.flush()
            logger.info(
                "[JOB][%s][SUPERSEDED] Interrupted automatic cascade replaced by user-requested %s job",
                existing.id,
                job_type,
            )
        else:
            raise ValueError(f"Project already has an active {existing.job_type} job ({existing.id}).")
    job = GenerationJobModel(
        project_id=uuid.UUID(str(project_id)),
        requested_by_user_id=uuid.UUID(str(user_id)),
        job_type=job_type,
        request_payload=jsonable_encoder(payload),
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ValueError("Project already has an active generation job.") from None
    await session.refresh(job)
    return serialize_job(job)


async def get_job(job_id: str, project_id: str, session) -> GenerationJobModel | None:
    try:
        jid = uuid.UUID(str(job_id))
        pid = uuid.UUID(str(project_id))
    except ValueError:
        return None
    return await session.scalar(select(GenerationJobModel).where(
        GenerationJobModel.id == jid,
        GenerationJobModel.project_id == pid,
    ))


# JOB 5.2 — Dispatch a persisted job payload.
# Trigger: JOB 5.3 after model capacity is acquired.
# Calls: Architect/Auditor → requirements pipeline; regeneration → dependency service.
# Branches: missing requesting user/unknown type → failure.
# Writes: domain writes remain in JOB 5.3's transaction.
# Failure: regeneration first asks LOCK 5.3 for unsupported type `project`, so it
#          currently fails before dependency execution (documented, not fixed).
async def _execute(job: GenerationJobModel, session) -> dict[str, Any]:
    user = await session.get(UserModel, job.requested_by_user_id)
    if user is None:
        raise RuntimeError("The user who requested this job no longer exists.")
    if job.job_type in {"architect", "auditor"}:
        from app.routes.requirements import _process_requirements_pipeline

        request = ProcessRequirementsRequest.model_validate(job.request_payload)
        request.target_agent = job.job_type
        current_user = AuthenticatedUser(
            id=str(user.id), email=user.email, full_name=user.full_name, role=user.role,
        )
        return jsonable_encoder(await _process_requirements_pipeline(request, session, current_user))
    if job.job_type == "regeneration":
        from app.dependency_graph_service import execute_regeneration
        from app.lock_service import LockService

        lock_info = await LockService.get_lock_status(
            "project", str(job.project_id), session, project_id=str(job.project_id),
        )
        LockService.raise_if_locked("project", str(job.project_id), lock_info)

        result = await execute_regeneration(
            str(job.project_id),
            list(job.request_payload.get("changed_artifacts") or []),
            user.full_name,
            session,
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error_message") or "Partial regeneration failed.")
        return jsonable_encoder(result)
    raise RuntimeError(f"Unsupported generation job type: {job.job_type}")


# JOB 5.3 — Execute and finalize a durable job.
# Trigger: JOB 5.3.1 launch or JOB 8.1 recovery.
# Calls: heavy-inference semaphore → timed JOB 5.2 dispatch.
# Branches: success → completed; user cancel → cancelled; shutdown → queued;
#           exception/timeout → failed with sanitized public error.
# Writes: progress/result/error/timestamps and transactional domain work.
# Failure: rolls back domain work before committing terminal failure state.
async def _run_job(job_id: str) -> None:
    started = time.perf_counter()
    log_context = LogContext(job_id=job_id, workflow="background_generation")
    logger.info("[JOB][%s][LOAD][START] Loading queued generation job", job_id)
    async with AsyncSessionLocal() as session:
        job = await session.get(GenerationJobModel, uuid.UUID(job_id))
        if job is None or job.status in TERMINAL_STATUSES:
            logger.info("[JOB][%s][LOAD][SKIP] Job is missing or already terminal", job_id)
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.progress_stage = "cancelled"
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info("[JOB][%s][CANCEL][DONE] Cancelled before execution", job_id)
            return
        job.status = "running"
        log_context = LogContext(
            project_id=str(job.project_id),
            user_id=str(job.requested_by_user_id),
            workflow=str(job.job_type),
            job_id=job_id,
        )
        job.progress_stage = "model_processing"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        await session.commit()
        logger.info(
            "[JOB][%s][EXECUTE][START] type=%s project=%s",
            job_id, job.job_type, job.project_id,
        )
        try:
            if job.job_type in {"architect", "auditor", "regeneration"}:
                job.progress_stage = "waiting_for_local_model"
                await session.commit()
                logger.info("[JOB][%s][MODEL_SLOT][WAIT] Waiting for local-model capacity", job_id)
                async with _heavy_inference_semaphore:
                    job.progress_stage = "model_processing"
                    await session.commit()
                    logger.info("[JOB][%s][MODEL][START] Local-model processing started", job_id)
                    result = await asyncio.wait_for(
                        _execute(job, session),
                        timeout=settings.LLM_BACKGROUND_JOB_TIMEOUT_SECONDS,
                    )
            else:
                result = await _execute(job, session)
            job.status = "completed"
            job.progress_stage = "completed"
            job.result_payload = result
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()
            if job.job_type == "regeneration":
                from app.event_manager import event_manager

                await event_manager.publish(str(job.project_id), "cascade_regeneration_completed", {
                    "project_id": str(job.project_id),
                    "job_id": job_id,
                    "status": "completed",
                })
            logger.info(
                "[JOB][%s][COMPLETE][DONE] type=%s elapsed_ms=%.1f",
                job_id, job.job_type, (time.perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            await session.rollback()
            job = await session.get(GenerationJobModel, uuid.UUID(job_id))
            if job is not None:
                # The cancellation request is committed by a different API
                # session. Refresh the identity-map object before deciding
                # whether this was a user stop or process shutdown.
                await session.refresh(job)
                # Explicit API cancellation sets cancel_requested before it
                # cancels the task. A process shutdown does not: leave that
                # work queued so startup recovery can safely resume it.
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.progress_stage = "cancelled"
                    job.completed_at = datetime.now(timezone.utc)
                else:
                    job.status = "queued"
                    job.progress_stage = "interrupted_by_shutdown"
                    job.completed_at = None
                await session.commit()
            logger.info(
                "[JOB][%s][INTERRUPTED] user_cancelled=%s elapsed_ms=%.1f",
                job_id, bool(job and job.cancel_requested),
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            failed_stage = job.progress_stage
            policy = classify_exception(exc)
            await session.rollback()
            job = await session.get(GenerationJobModel, uuid.UUID(job_id))
            if job is not None:
                job.status = "failed"
                job.progress_stage = "failed"
                job.error_message = _public_failure(exc)
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()
            logger.exception(
                "[JOB][%s][FAILED] stage=%s elapsed_ms=%.1f",
                job_id, failed_stage,
                (time.perf_counter() - started) * 1000,
            )
            log_event(
                logger,
                logging.ERROR,
                "background_job.failed",
                log_context,
                exc_info=True,
                kind=policy.kind.value,
                retryable=policy.retryable,
                rollback=policy.rollback,
                stage=failed_stage,
            )


# JOB 5.3.1 — Idempotently attach one process-local asyncio task to a persisted id.
def launch_job(job_id: str) -> None:
    current = _tasks.get(job_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(_run_job(job_id), name=f"generation-job-{job_id}")
    _tasks[job_id] = task
    task.add_done_callback(lambda finished: _tasks.pop(job_id, None) if _tasks.get(job_id) is finished else None)


# JOB 5.4 — Cancel a durable job.
# Trigger: JOB 4.4.
# Writes: cancel_requested + cancelling commit before process-local task cancellation.
# Branches: terminal → false; live task → task.cancel; no task → immediate cancelled.
# Failure: database errors propagate to the route.
async def cancel_job(job: GenerationJobModel, session) -> bool:
    if job.status in TERMINAL_STATUSES:
        return False
    job.cancel_requested = True
    job.status = "cancelling"
    job.progress_stage = "cancelling"
    await session.commit()
    task = _tasks.get(str(job.id))
    if task is not None and not task.done():
        task.cancel()
        return True
    # A queued job not yet attached to this process can be cancelled directly.
    job.status = "cancelled"
    job.progress_stage = "cancelled"
    job.completed_at = datetime.now(timezone.utc)
    await session.commit()
    return True


# JOB 8.1 — Recover durable work at application startup.
# Trigger: lifespan startup after migrations/user seed.
# Branches: cancel_requested → cancelled; otherwise → queued and JOB 5.3.1.
# Writes: recovery status/stage/timestamps.
async def recover_generation_jobs() -> int:
    """Requeue work interrupted by a restart, then launch every queued job."""
    async with AsyncSessionLocal() as session:
        jobs = list((await session.scalars(select(GenerationJobModel).where(
            GenerationJobModel.status.in_(ACTIVE_STATUSES)
        ))).all())
        for job in jobs:
            if job.cancel_requested:
                job.status = "cancelled"
                job.progress_stage = "cancelled"
                job.completed_at = datetime.now(timezone.utc)
            else:
                job.status = "queued"
                job.progress_stage = "recovered_after_restart"
        await session.commit()
        queued = [str(job.id) for job in jobs if job.status == "queued"]
    for job_id in queued:
        launch_job(job_id)
    return len(queued)


# JOB 8.2 — Interrupt local tasks for process shutdown.
# Trigger: lifespan shutdown.
# Calls: asyncio task cancellation → JOB 5.3 CancelledError branch.
# Writes: no cancel_requested flag; workers record queued/interrupted for JOB 8.1.
async def shutdown_generation_jobs() -> None:
    tasks = [task for task in _tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

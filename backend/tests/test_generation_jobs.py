"""Persistent background-job completion and cancellation semantics."""
import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import GenerationJobModel, ProjectModel, UserModel
import app.generation_job_service as jobs


@pytest_asyncio.fixture
async def job_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/jobs.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "AsyncSessionLocal", factory)
    async with factory() as session:
        user = UserModel(email="jobs@example.com", full_name="Job Owner", role="product_owner")
        session.add(user)
        await session.flush()
        project = ProjectModel(user_id=user.id, name="Background Work", industry_standard="Banking")
        session.add(project)
        await session.commit()
        yield factory, str(user.id), str(project.id)
    await jobs.shutdown_generation_jobs()
    await engine.dispose()


async def _new_job(factory, user_id, project_id):
    async with factory() as session:
        payload = await jobs.create_job(project_id, user_id, "architect", {"project_id": project_id}, session)
        await session.commit()
        return payload["id"]


@pytest.mark.asyncio
async def test_background_job_persists_completed_result(job_db, monkeypatch):
    factory, user_id, project_id = job_db
    job_id = await _new_job(factory, user_id, project_id)

    async def execute(_job, _session):
        return {"prd_markdown": "# Complete"}

    monkeypatch.setattr(jobs, "_execute", execute)
    jobs.launch_job(job_id)
    await asyncio.wait_for(jobs._tasks[job_id], timeout=1)

    async with factory() as session:
        job = await session.get(GenerationJobModel, job_id)
        assert job.status == "completed"
        assert job.result_payload == {"prd_markdown": "# Complete"}


@pytest.mark.asyncio
async def test_background_job_has_one_global_timeout(job_db, monkeypatch):
    factory, user_id, project_id = job_db
    job_id = await _new_job(factory, user_id, project_id)

    async def execute(_job, _session):
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "_execute", execute)
    monkeypatch.setattr(jobs.settings, "LLM_BACKGROUND_JOB_TIMEOUT_SECONDS", 0.02)
    jobs.launch_job(job_id)
    await asyncio.wait_for(jobs._tasks[job_id], timeout=1)

    async with factory() as session:
        job = await session.get(GenerationJobModel, job_id)
        assert job.status == "failed"
        assert job.error_message.startswith("timeout:")


@pytest.mark.asyncio
async def test_user_cancel_is_terminal_but_shutdown_is_recoverable(job_db, monkeypatch):
    factory, user_id, project_id = job_db
    started = asyncio.Event()

    async def execute(_job, _session):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "_execute", execute)
    cancelled_id = await _new_job(factory, user_id, project_id)
    jobs.launch_job(cancelled_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    task = jobs._tasks[cancelled_id]
    async with factory() as session:
        job = await session.get(GenerationJobModel, cancelled_id)
        assert await jobs.cancel_job(job, session)
    await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        cancelled = await session.get(GenerationJobModel, cancelled_id)
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested is True

        # A terminal job no longer blocks a new project job.
        second = await jobs.create_job(project_id, user_id, "architect", {"project_id": project_id}, session)
        await session.commit()
    started.clear()
    jobs.launch_job(second["id"])
    await asyncio.wait_for(started.wait(), timeout=1)
    await jobs.shutdown_generation_jobs()
    async with factory() as session:
        interrupted = await session.get(GenerationJobModel, second["id"])
        assert interrupted.status == "queued"
        assert interrupted.progress_stage == "interrupted_by_shutdown"
        assert interrupted.cancel_requested is False


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (asyncio.TimeoutError(), "timeout:"),
        (RuntimeError("Could not connect to LM Studio"), "llm_unavailable:"),
        (RuntimeError("Input is above the configured safe limit"), "context_limit:"),
        (RuntimeError("Structured response parse failed"), "invalid_model_output:"),
        (RuntimeError("There are no requirements to generate."), "There are no requirements to generate."),
        (RuntimeError("internal database detail"), "workflow_error:"),
    ],
)
def test_public_failure_returns_safe_actionable_categories(error, category):
    public_message = jobs._public_failure(error)

    assert public_message.startswith(category)
    assert "database detail" not in public_message


@pytest.mark.asyncio
async def test_get_job_rejects_invalid_and_cross_project_ids(job_db):
    factory, user_id, project_id = job_db
    job_id = await _new_job(factory, user_id, project_id)

    async with factory() as session:
        assert await jobs.get_job("not-a-uuid", project_id, session) is None
        assert await jobs.get_job(job_id, "not-a-uuid", session) is None
        assert await jobs.get_job(job_id, str(uuid.uuid4()), session) is None
        assert (await jobs.get_job(job_id, project_id, session)).id == uuid.UUID(job_id)


@pytest.mark.asyncio
async def test_create_job_rejects_second_active_job_for_project(job_db):
    factory, user_id, project_id = job_db
    first_id = await _new_job(factory, user_id, project_id)

    async with factory() as session:
        with pytest.raises(ValueError, match="already has an active architect job"):
            await jobs.create_job(
                project_id, user_id, "auditor", {"project_id": project_id}, session,
            )

        persisted = await session.get(GenerationJobModel, uuid.UUID(first_id))
        assert persisted.status == "queued"


@pytest.mark.asyncio
async def test_user_job_supersedes_interrupted_automatic_cascade(job_db):
    factory, user_id, project_id = job_db
    async with factory() as session:
        automatic = await jobs.create_job(
            project_id,
            user_id,
            "regeneration",
            {"changed_artifacts": [], "confirm": True, "automatic": True},
            session,
        )
        await session.commit()

        interrupted = await session.get(GenerationJobModel, uuid.UUID(automatic["id"]))
        interrupted.status = "queued"
        interrupted.progress_stage = "interrupted_by_shutdown"
        await session.commit()

        replacement = await jobs.create_job(
            project_id,
            user_id,
            "architect",
            {"project_id": project_id},
            session,
        )
        await session.commit()

        await session.refresh(interrupted)
        assert interrupted.status == "cancelled"
        assert interrupted.cancel_requested is True
        assert interrupted.progress_stage == "superseded_by_user_request"
        assert interrupted.completed_at is not None
        assert replacement["job_type"] == "architect"
        assert replacement["status"] == "queued"


@pytest.mark.asyncio
async def test_automatic_job_does_not_supersede_interrupted_automatic_cascade(job_db):
    factory, user_id, project_id = job_db
    async with factory() as session:
        first = await jobs.create_job(
            project_id,
            user_id,
            "regeneration",
            {"changed_artifacts": [], "confirm": True, "automatic": True},
            session,
        )
        await session.commit()
        interrupted = await session.get(GenerationJobModel, uuid.UUID(first["id"]))
        interrupted.progress_stage = "interrupted_by_shutdown"
        await session.commit()

        with pytest.raises(ValueError, match="already has an active regeneration job"):
            await jobs.create_job(
                project_id,
                user_id,
                "regeneration",
                {"changed_artifacts": [], "confirm": True, "automatic": True},
                session,
            )


@pytest.mark.asyncio
async def test_run_job_honours_cancellation_requested_before_execution(job_db, monkeypatch):
    factory, user_id, project_id = job_db
    job_id = await _new_job(factory, user_id, project_id)
    execute_called = False

    async def execute(_job, _session):
        nonlocal execute_called
        execute_called = True
        return {}

    monkeypatch.setattr(jobs, "_execute", execute)
    async with factory() as session:
        job = await session.get(GenerationJobModel, uuid.UUID(job_id))
        job.cancel_requested = True
        await session.commit()

    await jobs._run_job(job_id)

    async with factory() as session:
        job = await session.get(GenerationJobModel, uuid.UUID(job_id))
        assert job.status == "cancelled"
        assert job.progress_stage == "cancelled"
        assert job.completed_at is not None
    assert execute_called is False


@pytest.mark.asyncio
async def test_recovery_requeues_work_and_finalizes_previously_cancelled_job(job_db, monkeypatch):
    factory, user_id, project_id = job_db
    queued_id = await _new_job(factory, user_id, project_id)

    async with factory() as session:
        queued = await session.get(GenerationJobModel, uuid.UUID(queued_id))
        queued.status = "running"
        second_project = ProjectModel(
            user_id=uuid.UUID(user_id),
            name="Cancelled Background Work",
            industry_standard="Banking",
        )
        session.add(second_project)
        await session.commit()

        cancelled = GenerationJobModel(
            project_id=second_project.id,
            requested_by_user_id=uuid.UUID(user_id),
            job_type="auditor",
            request_payload={"project_id": project_id},
            status="cancelling",
            cancel_requested=True,
        )
        session.add(cancelled)
        await session.commit()
        cancelled_id = str(cancelled.id)

    launched = []
    monkeypatch.setattr(jobs, "launch_job", launched.append)

    assert await jobs.recover_generation_jobs() == 1
    assert launched == [queued_id]

    async with factory() as session:
        queued = await session.get(GenerationJobModel, uuid.UUID(queued_id))
        cancelled = await session.get(GenerationJobModel, uuid.UUID(cancelled_id))
        assert (queued.status, queued.progress_stage) == ("queued", "recovered_after_restart")
        assert (cancelled.status, cancelled.progress_stage) == ("cancelled", "cancelled")
        assert cancelled.completed_at is not None

import logging

from app.errors import (
    ErrorKind,
    ExpectedDomainError,
    OptionalFeatureError,
    PersistenceError,
    TransientInfrastructureError,
    classify_exception,
)
from app.observability import LogContext, log_event


def test_failure_policies_make_rollback_and_fail_open_explicit():
    assert classify_exception(ExpectedDomainError()).kind is ErrorKind.DOMAIN
    assert classify_exception(PersistenceError()).rollback is True
    assert classify_exception(TransientInfrastructureError()).retryable is True
    assert classify_exception(OptionalFeatureError()).fail_open is True
    assert classify_exception(RuntimeError("bad model JSON")).http_status == 503


def test_structured_log_context_carries_cross_boundary_identifiers(caplog):
    context = LogContext(
        project_id="project-1",
        user_id="user-1",
        workflow="auditor",
        job_id="job-1",
        run_id="run-1",
    )
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("test.observability"), logging.INFO, "workflow.started", context)

    record = caplog.records[-1]
    assert record.event == "workflow.started"
    assert record.project_id == "project-1"
    assert record.user_id == "user-1"
    assert record.workflow == "auditor"
    assert record.job_id == "job-1"
    assert record.run_id == "run-1"


"""Contract tests for application-boundary type annotations."""

from typing import get_type_hints

from app import agents
from app.application_types import (
    AuditFailure,
    BackgroundJobState,
    PendingMutation,
    RequirementMergeReport,
    WorkflowNodeOutcome,
)
from app.generation_job_service import serialize_job
from app.merge_service import merge_user_stories_with_report
from app.repositories.pending_action import PendingActionRepository


def test_major_boundaries_expose_explicit_result_types():
    assert get_type_hints(agents.gatherer_node)["return"] is WorkflowNodeOutcome
    assert get_type_hints(agents.auditor_node)["return"] is WorkflowNodeOutcome
    assert get_type_hints(agents.architect_node)["return"] is WorkflowNodeOutcome
    assert get_type_hints(agents._auditor_failure_details)["return"] is AuditFailure
    assert get_type_hints(serialize_job)["return"] is BackgroundJobState
    assert get_type_hints(PendingActionRepository.create)["data"] is PendingMutation


def test_merge_boundary_names_its_report_contract():
    return_type = get_type_hints(merge_user_stories_with_report)["return"]
    assert RequirementMergeReport in return_type.__args__


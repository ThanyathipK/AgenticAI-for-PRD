"""Typed contracts for dictionaries crossing application-layer boundaries.

These definitions add static safety without changing serialization, database
payloads, or public API response shapes.
"""

from typing import Any, Dict, List, Literal, Optional, TypeAlias, TypedDict

MergeAction: TypeAlias = Literal["updated", "unchanged", "created", "archived", "conflict"]
JobStatus: TypeAlias = Literal[
    "queued", "running", "cancelling", "completed", "failed", "cancelled"
]


class AuditFailure(TypedDict):
    code: str
    message: str
    description: str
    recommendation: str


class AuditResultPayload(TypedDict, total=False):
    id: str
    project_id: str
    is_valid: bool
    audit_version_reviewed: int
    passed_checks: List[str]
    failed_checks: List[str]
    findings: List[Dict[str, Any]]
    source_references: List[Dict[str, Any]]
    clarification_questions: List[Dict[str, Any]]
    verdict: str
    project_context: Dict[str, Any]
    checklist_id: str
    checklist_version: str
    operational_error: bool
    failure: AuditFailure


class SemanticChangePayload(TypedDict, total=False):
    change_type: str
    target_requirement_id: Optional[str]
    confidence: float
    reason: str
    recommended_action: str


class AcceptanceCriteriaMergeSummary(TypedDict, total=False):
    kept: int
    updated: int
    added: int
    removed: int


class MergeReportEntry(TypedDict, total=False):
    action: MergeAction
    matched_by: Optional[str]
    protected: bool
    fields_changed: List[str]
    acceptance_criteria: AcceptanceCriteriaMergeSummary


RequirementMergeReport: TypeAlias = Dict[str, MergeReportEntry]


class PendingMutation(TypedDict, total=False):
    action_type: str
    target_requirement_id: Optional[str]
    original_user_message: str
    proposed_changes: Dict[str, Any]
    affected_user_story_ids: List[str]
    affected_acceptance_criteria_ids: List[str]
    workflow_stage: str


class PRDGenerationResult(TypedDict, total=False):
    requirement_state: Dict[str, Any]
    prd_markdown: str
    mermaid_diagram: str
    current_version: int
    skipped_locked_prd_sections: List[str]
    agent_message: str


class WorkflowNodeOutcome(PRDGenerationResult, total=False):
    structured_requirements: Dict[str, Any]
    audit_result: AuditResultPayload
    audit_operational_error: bool
    detected_intent: Optional[str]
    workflow_routing: Dict[str, Any]
    requirement_match: Dict[str, Any]
    lock_blocked: bool


class BackgroundJobState(TypedDict, total=False):
    id: str
    project_id: str
    job_type: str
    status: JobStatus
    progress_stage: str
    result: Optional[Dict[str, Any]]
    error_message: Optional[str]
    cancel_requested: bool
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]

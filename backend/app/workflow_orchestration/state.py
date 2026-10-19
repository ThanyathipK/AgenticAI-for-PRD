"""LangGraph state contracts shared by all workflow nodes."""

from typing import Any, Dict, List, Optional, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.application_types import AuditResultPayload


class RequirementState(TypedDict):
    """Persisted, project-scoped requirements and generated artifacts."""

    project_id: str
    project_name: str
    requirements: dict
    business_goals: list
    actors: list
    user_stories: list
    acceptance_criteria: list
    prd_narrative: dict
    clarification_questions: list
    validation_status: str
    generated_prd: str
    generated_diagrams: str
    current_workflow_state: str
    version_number: int
    updated_at: Optional[str]
    detected_intent: Optional[str]


class AgentState(TypedDict, total=False):
    """Request-scoped state carried through the single-pass LangGraph."""

    project_id: str
    raw_input: str
    structured_requirements: dict
    audit_result: AuditResultPayload
    audit_operational_error: bool
    prd_markdown: str
    mermaid_diagram: str
    current_version: int
    version_history_summaries: str
    target_agent: str
    force_prd_rebuild: bool
    requirement_state: RequirementState
    detected_intent: Optional[str]
    lock_blocked: bool
    skipped_locked_prd_sections: List[str]
    intent_confidence: Optional[float]
    requirement_match: Optional[Dict[str, Any]]
    archive_story_codes: List[str]
    archive_requirement_codes: List[str]
    archive_acceptance_criteria_ids: List[str]
    workflow_routing: Optional[dict]
    agent_message: Optional[str]
    knowledge_documents: List[Dict[str, Any]]
    db_session: Optional[AsyncSession]
    regeneration_scope: Optional[Dict[str, Any]]
    stage_artifacts_only: bool

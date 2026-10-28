"""
Shared helpers and serializers for the repository layer.

These functions centralize the hand-written ``model -> dict`` serializers
that used to be duplicated across every repository class in the old
``app.repository`` monolith. Keeping them in one place means the dict
shapes stay consistent and any future column additions only need to be
updated here.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Union

from app.models import (
    AcceptanceCriteriaModel,
    ArtifactEventLogModel,
    AuditResultModel,
    ClarificationQuestionModel,
    ConversationMessageModel,
    DocumentModel,
    EpicModel,
    PendingActionModel,
    PRDDocumentModel,
    PRDSectionModel,
    PRDSectionVersionModel,
    PRDVersionModel,
    ProjectModel,
    RequirementModel,
    SemanticMemoryModel,
    UserStoryModel,
)


def compact_requirement_context(value: Any, target: Optional[str] = None) -> Optional[str]:
    """Return bounded, target-relevant JSON for a clarification question.

    Older audits stored the complete requirement and every story on every
    question. Besides wasting LLM context, those snapshots exceeded the API's
    4,000-character response contract. This also repairs those legacy rows at
    read time, without changing or dropping the question itself.
    """
    if value is None:
        return None
    parsed = value
    if isinstance(value, str):
        if len(value) <= 4000:
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value[:3997] + "..."
    if not isinstance(parsed, dict):
        text = json.dumps(parsed, ensure_ascii=False, default=str)
        return text if len(text) <= 4000 else text[:3997] + "..."

    compact = {
        key: parsed.get(key)
        for key in ("requirement_code", "title", "functional_requirement", "description")
        if parsed.get(key) is not None
    }
    description = compact.get("description")
    if isinstance(description, str):
        compact["description"] = description[:1000]
    stories = [story for story in (parsed.get("user_stories") or []) if isinstance(story, dict)]
    selected = next(
        (story for story in stories if str(story.get("ticket_code")) == str(target)),
        None,
    )
    if selected:
        target_story = {
            key: selected.get(key)
            for key in ("ticket_code", "story_title", "as_a", "i_want_to", "so_that")
            if selected.get(key) is not None
        }
        target_story["acceptance_criteria"] = [
            str(item)[:500] for item in (selected.get("acceptance_criteria") or [])[:5]
        ]
        compact["target_story"] = target_story
    elif stories:
        compact["story_index"] = [
            {
                "ticket_code": story.get("ticket_code"),
                "story_title": story.get("story_title"),
            }
            for story in stories[:20]
        ]
    text = json.dumps(compact, ensure_ascii=False, default=str)
    if len(text) <= 4000:
        return text
    # Defensive fallback for unusually long titles/actors. Preserve valid JSON.
    return json.dumps({
        "requirement_code": compact.get("requirement_code"),
        "title": str(compact.get("title") or "")[:500],
        "context_summary": text[:3000],
    }, ensure_ascii=False)


def as_uuid(value: Union[str, uuid.UUID]) -> uuid.UUID:
    """Coerce a string or UUID into a UUID, passing UUIDs through untouched."""
    return uuid.UUID(value) if isinstance(value, str) else value


def uid(value) -> Optional[str]:
    """Render a value as a string, passing None through untouched."""
    return str(value) if value is not None else None


def dt_iso(value: Optional[datetime]) -> str:
    """Render a datetime as an ISO string, or ``""`` when missing/None."""
    return value.isoformat() if value else ""


def dt_iso_or_none(value: Optional[datetime]) -> Optional[str]:
    """Render a datetime as an ISO string, or None when missing."""
    return value.isoformat() if value else None


def serialize_project(p: ProjectModel, *, match_snippet: Optional[str] = None) -> Dict[str, Any]:
    """Serialize a ProjectModel into its public dict representation.

    ``match_snippet`` (an excerpt of a conversation message that caused the
    project to match a sidebar search) is only emitted when provided, so the
    ordinary list/detail payloads stay unchanged.
    """
    data = {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "industry_standard": p.industry_standard,
        "is_pinned": bool(p.is_pinned) if p.is_pinned is not None else False,
        "is_flagged": bool(p.is_flagged) if p.is_flagged is not None else False,
        "status": p.status or "draft",
        "last_approved_prd_version": p.last_approved_prd_version,
        "last_approved_audit_version": p.last_approved_audit_version,
        "last_approved_by": uid(p.last_approved_by),
        "last_approved_at": dt_iso_or_none(p.last_approved_at),
        "updated_at": dt_iso_or_none(p.updated_at),
    }
    if match_snippet:
        data["match_snippet"] = match_snippet
    return data


def serialize_epic(epic: EpicModel, *, with_status: bool = False) -> Dict[str, Any]:
    """Serialize an EpicModel into its public dict representation."""
    data = {
        "id": str(epic.id),
        "project_id": str(epic.project_id),
        "epic_name": epic.epic_name,
        "version": epic.version,
    }
    if with_status:
        data["status"] = epic.status
    return data


def serialize_requirement(req: RequirementModel) -> Dict[str, Any]:
    """Serialize a RequirementModel to a dict including lock fields."""
    return {
        "id": str(req.id),
        "project_id": str(req.project_id),
        "epic_id": uid(req.epic_id),
        "requirement_code": req.requirement_code,
        "title": req.title,
        "functional_requirement": req.functional_requirement,
        "description": req.description,
        "status": req.status,
        "is_locked": bool(req.is_locked) if req.is_locked is not None else False,
        "locked_by": req.locked_by,
        "locked_at": dt_iso_or_none(req.locked_at),
    }


def serialize_user_story(
    story: UserStoryModel,
    project_id,
) -> Dict[str, Any]:
    """Serialize a UserStoryModel into its public dict representation.

    ``project_id`` is accepted explicitly (instead of deriving it from the
    model) because the legacy callers thread the stringified project UUID
    through the whole call stack.
    """
    data = {
        "id": str(story.id),
        "project_id": str(project_id),
        "ticket_code": story.ticket_code,
        "story_title": story.story_title,
        "as_a": story.as_a,
        "i_want_to": story.i_want_to,
        "so_that": story.so_that,
        "status": story.status,
        "version": story.version,
        "last_modified_by": story.last_modified_by,
        "change_type": story.change_type,
    }
    return data


def serialize_acceptance_criteria(
    ac: AcceptanceCriteriaModel,
    project_id,
    ticket_code,
) -> Dict[str, Any]:
    """Serialize an AcceptanceCriteriaModel into its public dict repr."""
    return {
        "id": str(ac.id),
        "project_id": str(project_id),
        "ticket_code": ticket_code,
        "user_story_id": uid(ac.user_story_id),
        "criteria_text": ac.criteria_text,
        "status": ac.status,
        "version": ac.version,
        "last_modified_by": ac.last_modified_by,
        "change_type": ac.change_type,
    }


def serialize_clarification_question(
    cq: ClarificationQuestionModel,
    project_id,
    target_story_id,
) -> Dict[str, Any]:
    """Serialize a ClarificationQuestionModel into its public dict repr.

    ``target_story_id`` is the *ticket code* of the linked user story (the
    legacy API surfaces the ticket code instead of the raw UUID).
    """
    return {
        "id": str(cq.id),
        "project_id": str(project_id),
        "checklist_category": cq.checklist_category,
        "target_user_story_id": target_story_id,
        "question_text": cq.question_text,
        "user_answer": cq.user_answer,
        "is_resolved": cq.is_resolved,
        "source_references": cq.source_references or [],
        "provenance": cq.provenance or {"type": "System analysis"},
        "target_requirement_id": cq.target_requirement_id,
        "requirement_context": compact_requirement_context(
            cq.requirement_context,
            cq.target_requirement_id or target_story_id,
        ),
    }


def serialize_audit_result(ar: AuditResultModel, project_id) -> Dict[str, Any]:
    """Serialize an AuditResultModel into its public dict representation."""
    return {
        "id": str(ar.id),
        "project_id": str(project_id),
        "is_valid": ar.is_valid,
        "audit_version_reviewed": ar.audit_version_reviewed,
        "passed_checks": ar.passed_checks,
        "failed_checks": ar.failed_checks,
        "findings": ar.findings or [],
        "source_references": ar.source_references or [],
        "verdict": ar.verdict or "needs_clarification",
        "project_context": ar.project_context or {},
        "checklist_id": ar.checklist_id or "banking-core",
        "checklist_version": ar.checklist_version or "1.0.0",
    }


def serialize_prd_document(prd: PRDDocumentModel) -> Dict[str, Any]:
    """Serialize a PRDDocumentModel into its public dict representation."""
    return {
        "id": str(prd.id),
        "project_id": str(prd.project_id),
        "version": prd.version,
        "prd_markdown": prd.prd_markdown,
        "mermaid_diagram": prd.mermaid_diagram,
        "created_at": prd.created_at.isoformat() if prd.created_at else None,
        "updated_at": prd.updated_at.isoformat() if prd.updated_at else None,
    }


def serialize_prd_version(v: PRDVersionModel) -> Dict[str, Any]:
    """Serialize a PRDVersionModel into its public dict representation."""
    return {
        "version_id": str(v.id),
        "project_id": str(v.project_id),
        "version_number": v.version_number,
        "semver": v.semver or "1.0.0",
        "generated_prd": v.generated_prd,
        "generated_by": v.generated_by,
        "change_type": v.change_type or "ai",
        "change_summary": v.change_summary,
        "changed_sections": v.changed_sections,
        "created_at": dt_iso(v.created_at),
    }


def serialize_prd_section(s: PRDSectionModel, *, current_version: Optional[int] = None) -> Dict[str, Any]:
    """Serialize a PRDSectionModel into its public dict representation.

    ``current_version`` is the latest ``prd_section_versions.version_number``
    for this section (attached by the repository when known).
    """
    data = {
        "id": str(s.id),
        "project_id": str(s.project_id),
        "section_key": s.section_key,
        "title": s.title,
        "content": s.content,
        "section_order": s.section_order,
        "content_source": s.content_source,
        "ai_generatable": bool(s.ai_generatable),
        "review_status": s.review_status,
        "is_locked": bool(s.is_locked) if s.is_locked is not None else False,
        "locked_by": s.locked_by,
        "locked_at": dt_iso_or_none(s.locked_at),
        "created_at": dt_iso(s.created_at),
        "updated_at": dt_iso(s.updated_at),
    }
    if current_version is not None:
        data["version_number"] = current_version
    return data


def serialize_prd_section_version(v: PRDSectionVersionModel) -> Dict[str, Any]:
    """Serialize a PRDSectionVersionModel into its public dict representation."""
    return {
        "id": str(v.id),
        "section_id": str(v.section_id),
        "version_number": v.version_number,
        "content": v.content,
        "changed_by": v.changed_by,
        "change_summary": v.change_summary,
        "created_at": dt_iso(v.created_at),
    }


def serialize_conversation_message(m: ConversationMessageModel) -> Dict[str, Any]:
    """Serialize a ConversationMessageModel into its public dict repr."""
    return {
        "id": str(m.id),
        "project_id": str(m.project_id),
        "role": m.role,
        "message": m.message,
        "content": m.message,
        "workflow_state": m.workflow_state,
        "intent": m.intent,
        "created_at": dt_iso(m.created_at),
    }


# DOC-UPLOAD 7.6 — Serialization shared by every document read/write (7.1/7.2/7.3): the
#             markdown body is included ONLY when include_markdown=True, which is the
#             difference between the list endpoint (4.2) and the preview endpoint (4.3).
def serialize_document(doc: DocumentModel, *, include_markdown: bool = False) -> Dict[str, Any]:
    """Serialize a DocumentModel into its public dict representation.

    The canonical converted markdown is always persisted in full; it is only
    included in the payload when ``include_markdown=True`` (the dedicated
    detail/markdown endpoint) so list responses stay lean.
    """
    data = {
        "id": str(doc.id),
        "project_id": str(doc.project_id),
        "original_filename": doc.original_filename,
        "original_format": doc.original_format,
        "mime_type": doc.mime_type,
        "file_size_bytes": doc.file_size_bytes,
        "token_count": doc.token_count,
        "status": doc.status,
        "uploaded_by": doc.uploaded_by,
        "extraction_status": doc.extraction_status,
        "created_at": dt_iso(doc.created_at),
        "updated_at": dt_iso(doc.updated_at),
    }
    if include_markdown:
        data["content_markdown"] = doc.content_markdown
    return data


def serialize_pending_action(a: PendingActionModel, *, full: bool = False) -> Dict[str, Any]:
    """Serialize a PendingActionModel into its public dict representation."""
    if full:
        return {
            "id": str(a.id),
            "project_id": str(a.project_id),
            "action_type": a.action_type,
            "target_requirement_id": a.target_requirement_id,
            "original_user_message": a.original_user_message,
            "proposed_changes": a.proposed_changes,
            "affected_user_story_ids": a.affected_user_story_ids,
            "affected_acceptance_criteria_ids": a.affected_acceptance_criteria_ids,
            "workflow_stage": a.workflow_stage,
            "status": a.status,
            "expires_at": dt_iso(a.expires_at),
        }
    return {
        "id": str(a.id),
        "project_id": str(a.project_id),
        "action_type": a.action_type,
        "target_requirement_id": a.target_requirement_id,
        "status": a.status,
    }


def serialize_semantic_memory(m: SemanticMemoryModel, *, score: Optional[float] = None) -> Dict[str, Any]:
    """Serialize a SemanticMemoryModel into its public dict representation.

    ``score`` (blended relevance+recency from the recall path) is only emitted
    when provided, so plain persistence payloads stay unchanged.
    """
    data = {
        "id": str(m.id),
        "project_id": str(m.project_id),
        "kind": m.kind,
        "content": m.content,
        "embedding_model": m.embedding_model,
        "source_message_id": str(m.source_message_id) if m.source_message_id else None,
        "recall_count": m.recall_count,
        "last_recalled_at": dt_iso_or_none(m.last_recalled_at),
        "created_at": dt_iso(m.created_at),
    }
    if score is not None:
        data["score"] = round(float(score), 4)
    return data


def serialize_event_log(e: ArtifactEventLogModel) -> Dict[str, Any]:
    """Serialize an ArtifactEventLogModel into its public dict repr."""
    return {
        "event_id": str(e.event_id),
        "artifact_type": e.artifact_type,
        "artifact_id": e.artifact_id,
        "action": e.action,
        "old_value": e.old_value,
        "new_value": e.new_value,
        "performed_by": e.performed_by,
        "timestamp": dt_iso(e.timestamp),
    }

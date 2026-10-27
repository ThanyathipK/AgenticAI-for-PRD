from sqlalchemy import Column, String, Integer, DateTime, JSON, Text, func, ForeignKey, Boolean, UniqueConstraint, Index, text
from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
import uuid
from app.database import Base

class GUID(TypeDecorator):
    """Platform-independent GUID type.
    Uses PostgreSQL's UUID type, otherwise CHAR(36), keeping a string on other databases.
    """
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == 'postgresql':
            return value
        else:
            if isinstance(value, uuid.UUID):
                return str(value)
            else:
                return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value)
            return value

class UserModel(Base):
    __tablename__ = "users"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False, server_default="")
    role = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class ProjectModel(Base):
    __tablename__ = "projects"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    industry_standard = Column(String(100), nullable=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    # Dashboard-only ★ flag marker — deliberately independent of is_pinned:
    # flagging marks a project for attention in the projects overview table and
    # NEVER affects the sidebar's pinned-first ordering.
    is_flagged = Column(Boolean, nullable=False, default=False)
    # User-editable workflow status shown on the dashboard table
    # ('draft' | 'in_review_hpo' | 'in_review_po' | 'approved' | 'revised').
    status = Column(String(50), nullable=False, default="draft", server_default="draft")
    last_approved_prd_version = Column(Integer, nullable=True)
    last_approved_audit_version = Column(Integer, nullable=True)
    last_approved_by = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ProjectReviewEventModel(Base):
    """Append-only lifecycle transition history for a project."""
    __tablename__ = "project_review_events"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(50), nullable=False)
    to_status = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)
    comment = Column(Text, nullable=True)
    actor_id = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_name = Column(String(255), nullable=False)
    actor_role = Column(String(100), nullable=False)
    prd_version_number = Column(Integer, nullable=True)
    audit_version_reviewed = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class EpicModel(Base):
    __tablename__ = "epics"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    epic_name = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    status = Column(String(50), nullable=False, default="active", server_default="active")

class RequirementModel(Base):
    __tablename__ = "requirements"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    epic_id = Column(GUID, ForeignKey("epics.id", ondelete="CASCADE"), nullable=True, index=True)
    requirement_code = Column(String(50), nullable=False) # e.g. REQ-001
    title = Column(String(255), nullable=False)
    functional_requirement = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="active", server_default="active")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    is_locked = Column(Boolean, nullable=False, default=False, server_default="false")
    locked_by = Column(String(100), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class UserStoryModel(Base):
    __tablename__ = "user_stories"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    requirement_id = Column(GUID, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    ticket_code = Column(String(50), nullable=False)
    story_title = Column(String(255), nullable=False)
    as_a = Column(Text, nullable=False)
    i_want_to = Column(Text, nullable=False)
    so_that = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    status = Column(String(50), nullable=False, default="active", server_default="active")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    last_modified_by = Column(String(100), nullable=False, default="automated_agent", server_default="automated_agent")
    change_type = Column(String(50), nullable=False, default="created", server_default="created")

    __table_args__ = (
        UniqueConstraint("project_id", "ticket_code", name="uq_user_stories_project_id_ticket_code"),
    )

class AcceptanceCriteriaModel(Base):
    __tablename__ = "acceptance_criteria"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    user_story_id = Column(GUID, ForeignKey("user_stories.id", ondelete="CASCADE"), nullable=False, index=True)
    criteria_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    status = Column(String(50), nullable=False, default="active", server_default="active")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    last_modified_by = Column(String(100), nullable=False, default="automated_agent", server_default="automated_agent")
    change_type = Column(String(50), nullable=False, default="created", server_default="created")

class AuditResultModel(Base):
    __tablename__ = "audit_results"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    requirement_id = Column(GUID, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    audit_version_reviewed = Column(Integer, nullable=False)
    is_valid = Column(Boolean, nullable=False, default=False)
    passed_checks = Column(JSON, nullable=False, default=list)
    failed_checks = Column(JSON, nullable=False, default=list)
    findings = Column(JSON, nullable=False, default=list)
    source_references = Column(JSON, nullable=False, default=list)
    verdict = Column(String(50), nullable=False, default="needs_clarification")
    project_context = Column(JSON, nullable=False, default=dict)
    checklist_id = Column(String(100), nullable=False, default="banking-core", server_default="banking-core")
    checklist_version = Column(String(30), nullable=False, default="1.0.0", server_default="1.0.0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AuditRunModel(Base):
    """Immutable project-level snapshot of one confirmed Auditor execution."""
    __tablename__ = "audit_runs"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    audit_result_id = Column(GUID, ForeignKey("audit_results.id", ondelete="SET NULL"), nullable=True)
    run_number = Column(Integer, nullable=False)
    audit_version_reviewed = Column(Integer, nullable=False)
    is_valid = Column(Boolean, nullable=False, default=False)
    verdict = Column(String(50), nullable=False)
    findings = Column(JSON, nullable=False, default=list)
    passed_checks = Column(JSON, nullable=False, default=list)
    failed_checks = Column(JSON, nullable=False, default=list)
    source_references = Column(JSON, nullable=False, default=list)
    project_context = Column(JSON, nullable=False, default=dict)
    checklist_id = Column(String(100), nullable=False)
    checklist_version = Column(String(30), nullable=False)
    comparison = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("project_id", "run_number", name="uq_audit_runs_project_number"),)


class AuditWaiverModel(Base):
    """Governed exception for one stable audit rule and optional target artifact."""
    __tablename__ = "audit_waivers"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = Column(String(100), nullable=False)
    target_requirement_id = Column(String(100), nullable=True)
    reason = Column(Text, nullable=False)
    compensating_control = Column(Text, nullable=True)
    owner = Column(String(255), nullable=False)
    approved_by = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_name = Column(String(255), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(30), nullable=False, default="active", server_default="active")
    revoked_reason = Column(Text, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ArtifactDependencyModel(Base):
    """Persisted directed edge in a project's artifact dependency graph."""
    __tablename__ = "artifact_dependencies"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(50), nullable=False)
    source_key = Column(String(255), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_key = Column(String(255), nullable=False)
    relationship = Column(String(50), nullable=False)
    evidence = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_type", "source_key", "target_type", "target_key", "relationship",
            name="uq_artifact_dependencies_edge",
        ),
        Index("idx_artifact_dependencies_source", "project_id", "source_type", "source_key"),
        Index("idx_artifact_dependencies_target", "project_id", "target_type", "target_key"),
    )


class RegenerationRunModel(Base):
    """Audit record for one dependency-scoped regeneration request."""
    __tablename__ = "regeneration_runs"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by = Column(String(255), nullable=False)
    trigger_artifacts = Column(JSON, nullable=False, default=list)
    plan = Column(JSON, nullable=False, default=dict)
    status = Column(String(30), nullable=False, default="planned", server_default="planned")
    regenerated_sections = Column(JSON, nullable=False, default=list)
    skipped_locked_sections = Column(JSON, nullable=False, default=list)
    diagram_regenerated = Column(Boolean, nullable=False, default=False, server_default="false")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class GenerationJobModel(Base):
    """Persistent background execution record for long-running AI work."""
    __tablename__ = "generation_jobs"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default="queued", server_default="queued", index=True)
    progress_stage = Column(String(100), nullable=False, default="queued", server_default="queued")
    request_payload = Column(JSON, nullable=False, default=dict)
    result_payload = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_generation_jobs_project_status", "project_id", "status"),
        Index(
            "uq_generation_jobs_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
            sqlite_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
    )


# Legacy compatibility mappings only. The former cross-project Banking
# Knowledge feature has no routes, UI, or retrieval path. Keeping these models
# in metadata prevents a future Alembic autogeneration from proposing an
# accidental destructive drop of installations that still contain old data.
class BankingKnowledgeDocumentModel(Base):
    """Inactive legacy banking-knowledge document row."""
    __tablename__ = "banking_knowledge_documents"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=False)
    original_format = Column(String(20), nullable=False)
    mime_type = Column(String(100), nullable=True)
    document_type = Column(String(100), nullable=False, default="best_practice", server_default="best_practice")
    jurisdiction = Column(String(100), nullable=False, default="global", server_default="global")
    tags = Column(JSON, nullable=False, default=list)
    content_markdown = Column(Text, nullable=False)
    content_checksum = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(30), nullable=False, default="draft", server_default="draft")
    approved_by = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("owner_user_id", "content_checksum", name="uq_banking_knowledge_owner_checksum"),
    )


class BankingKnowledgeChunkModel(Base):
    """Inactive legacy banking-knowledge chunk row."""
    __tablename__ = "banking_knowledge_chunks"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    document_id = Column(GUID, ForeignKey("banking_knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    heading = Column(String(500), nullable=True)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, nullable=False, default=0)
    embedding = Column(JSON, nullable=False, default=list)
    embedding_model = Column(String(100), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_banking_knowledge_chunk_index"),
    )


class BankingKnowledgeRetrievalModel(Base):
    """Inactive legacy retrieval audit row retained for data compatibility."""
    __tablename__ = "banking_knowledge_retrievals"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    query_text = Column(Text, nullable=False)
    retrieved_chunks = Column(JSON, nullable=False, default=list)
    embedding_used = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class ClarificationQuestionModel(Base):
    __tablename__ = "clarification_questions"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    audit_result_id = Column(GUID, ForeignKey("audit_results.id", ondelete="CASCADE"), nullable=False, index=True)
    checklist_category = Column(String(100), nullable=False)
    target_user_story_id = Column(GUID, ForeignKey("user_stories.id", ondelete="SET NULL"), nullable=True, index=True)
    question_text = Column(Text, nullable=False)
    user_answer = Column(Text, nullable=True)
    is_resolved = Column(Boolean, nullable=False, default=False)
    source_references = Column(JSON, nullable=False, default=list)
    provenance = Column(JSON, nullable=False, default=dict)
    target_requirement_id = Column(String(100), nullable=True)
    requirement_context = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class PRDDocumentModel(Base):
    __tablename__ = "prd_documents"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    prd_markdown = Column(Text, nullable=False)
    mermaid_diagram = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class PRDVersionModel(Base):
    """
    Dedicated immutable PRD version repository.
    EVERY change to the document — an AI regeneration ("Generate PRD") or a
    manual part edit / revert — creates a new version record, whether or not
    the content actually changed (a click always advances the version so the
    audit trail never collapses). Never overwrites previous versions.
    """
    __tablename__ = "prd_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_prd_versions_project_version",
        ),
    )

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    generated_prd = Column(Text, nullable=False)
    generated_by = Column(String(100), nullable=False, default="automated_agent")
    # 'ai' | 'manual' — who/what created this snapshot (the version ALWAYS
    # advances for both; this only records the origin for the Version ledger).
    change_type = Column(String(20), nullable=False, default="ai", server_default="ai")
    # Human-readable change description surfaced in the Version History tab.
    change_summary = Column(Text, nullable=True)
    # JSON list of per-section change records computed at snapshot time:
    #   [{"section_key": "business_overview", "title": "...", "change_kind":
    #    "created"|"updated"|"unchanged"|"locked_preserved", "changed": bool}]
    # Locked sections appear as "locked_preserved" so the ledger proves the
    # lock contract was honoured while the version still advanced.
    changed_sections = Column(JSON, nullable=True)
    # Semantic Version (MAJOR.MINOR.PATCH) for this snapshot, derived from the
    # per-section change records:
    #   MAJOR — structural change (sections created or removed)
    #   MINOR — AI regeneration with section content updates
    #   PATCH — manual edit, or an advance with no content change
    semver = Column(String(20), nullable=False, default="1.0.0", server_default="1.0.0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PRDSectionModel(Base):
    """
    One editable, lockable, versioned PART of the project's PRD document.

    The PRD is no longer only a single ``markdown_content`` blob: each part
    (cover, stakeholders, version history, reviews, contents, business
    overview, product scope, tech ops, appendix) lives in its own row so it
    can be edited part-by-part in the preview, locked individually, and
    excluded from AI regeneration.

    ``content`` stores the CURRENT markdown fragment of the section (heading
    line included, matching the frontend ``parsePRDToSections`` output). The
    full document is assembled by stitching rows in ``section_order``.

    Lock columns follow the exact same pattern as every other artifact table
    so ``LockService`` can manage ``prd_section`` generically.
    """
    __tablename__ = "prd_sections"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    section_key = Column(String(100), nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False, default="")
    section_order = Column(Integer, nullable=False, default=0)
    # Who owns the content: 'template' | 'ai' | 'human'
    content_source = Column(String(20), nullable=False, default="ai", server_default="ai")
    # When False the Architect agent NEVER regenerates this section.
    ai_generatable = Column(Boolean, nullable=False, default=True)
    # 'draft' | 'satisfied' | 'approved' — the "user satisfies it" gate.
    review_status = Column(String(30), nullable=False, default="draft", server_default="draft")
    is_locked = Column(Boolean, nullable=False, default=False)
    locked_by = Column(String(100), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "section_key", name="uq_prd_sections_project_key"),
    )


class PRDSectionVersionModel(Base):
    """
    Append-only per-section version history. A new row is inserted on EVERY
    content change (manual edit, revert, or an AI regeneration that actually
    changed the content) — the current text is materialized on
    ``prd_sections.content`` for fast reads, but it is never modified without
    a corresponding history row here. Nothing is ever overwritten or deleted.
    """
    __tablename__ = "prd_section_versions"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    section_id = Column(GUID, ForeignKey("prd_sections.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    changed_by = Column(String(100), nullable=False, default="user", server_default="user")
    change_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("section_id", "version_number", name="uq_prd_section_versions_section_version"),
    )

class RequirementStateModel(Base):
    """
    SQLAlchemy model for centralizing the RequirementState in Supabase.
    Isolates agent fields and persists specifications cleanly.
    """
    __tablename__ = "requirement_states"
    
    # The primary key already creates an index; a separate project_id index is
    # redundant because this table has exactly one state row per project.
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    # The project name deliberately belongs only to ``projects.name``.  Keeping
    # a second copy here made a rename dependent on every state write path and
    # allowed generated artifacts to use an obsolete name.
    business_goals = Column(JSON, nullable=False, default=list)
    actors = Column(JSON, nullable=False, default=list)
    prd_narrative = Column(JSON, nullable=False, default=dict)
    validation_status = Column(String(50), nullable=False, default="pending")
    generated_prd = Column(Text, nullable=False, default="")
    generated_diagrams = Column(Text, nullable=False, default="")
    current_workflow_state = Column(String(50), nullable=False, default="gatherer_node")
    # Requirement work must not advance the PRD release version or SemVer.
    requirements_revision = Column(Integer, nullable=False, default=1, server_default="1")
    version_number = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class ConversationMessageModel(Base):
    """
    SQLAlchemy model for persisting conversation messages per project.
    Uses a dedicated table independent from requirement_states.
    """
    __tablename__ = "conversation_messages"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    workflow_state = Column(String(50), nullable=True, default="")
    intent = Column(String(50), nullable=True, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "idx_conversation_messages_project_created_id",
            "project_id",
            "created_at",
            "id",
        ),
    )

class PendingActionModel(Base):
    __tablename__ = "pending_actions"
    
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)
    target_requirement_id = Column(String(50), nullable=True)
    original_user_message = Column(Text, nullable=False)
    proposed_changes = Column(JSON, nullable=False, default=dict)
    affected_user_story_ids = Column(JSON, nullable=False, default=list)
    affected_acceptance_criteria_ids = Column(JSON, nullable=False, default=list)
    workflow_stage = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="WAITING_CONFIRMATION")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


class DocumentModel(Base):
    """
    Uploaded project document — the immutable knowledge/document store.

    Uploading a document ONLY adds source material to the knowledge base: it
    never mutates requirements, user stories, or acceptance criteria. The full
    converted markdown is ALWAYS persisted (no token truncation at save time —
    token limits apply only to the explicit LLM extraction path). Extracted
    requirements are never stored here; they live only in the draft merge state
    (a pending_action) until the user confirms.
    """
    __tablename__ = "uploaded_documents"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    original_filename = Column(String(255), nullable=False)
    original_format = Column(String(20), nullable=False)  # 'docx' | 'pdf' | 'md' | 'txt'
    mime_type = Column(String(100), nullable=True)
    content_markdown = Column(Text, nullable=False, default="")  # canonical converted markdown, ALWAYS full
    file_size_bytes = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=False, default=0)  # measured at ingest via count_tokens
    status = Column(String(20), nullable=False, default="processed")  # 'processed' | 'failed'
    uploaded_by = Column(String(100), nullable=False, default="user", server_default="user")
    # Tracks whether the user has explicitly triggered extraction for this
    # document: 'not_extracted' | 'extraction_pending' | 'extraction_applied'.
    # Extracted requirements are NEVER stored on this table.
    extraction_status = Column(String(30), nullable=False, default="not_extracted", server_default="not_extracted")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ArtifactEventLogModel(Base):
    """
    Append-only immutable event log for tracking all artifact modifications.
    Stores historical records of CREATE, UPDATE, DELETE, ARCHIVE, LOCK, UNLOCK events.
    Never overwrites or deletes existing records.
    No foreign keys — purely historical traceability, not production data.
    """
    __tablename__ = "artifact_event_logs"

    event_id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    artifact_type = Column(String(50), nullable=False, index=True)
    artifact_id = Column(String(36), nullable=False, index=True)
    action = Column(String(20), nullable=False, index=True)
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    performed_by = Column(String(100), nullable=False, default="automated_agent")
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Composite index for querying events by artifact
    __table_args__ = (
        Index("idx_artifact_event_logs_artifact", "artifact_type", "artifact_id"),
        Index("idx_artifact_event_logs_timestamp", "timestamp"),
    )


class SemanticMemoryModel(Base):
    """
    One distilled, project-scoped semantic memory (Option A memory layer).

    After each chat turn a small LLM pass extracts DURABLE facts/decisions
    ("Project requires SSO", "User rejected option B") from the conversation.
    Each fact is embedded and persisted here so later turns can recall the
    most relevant memories by cosine similarity + recency.

    Embeddings are stored as a JSON float array instead of a pgvector column:
    the app also runs against the SQLite dev fallback (DATABASE_URL default),
    which cannot host a ``vector`` type, and per-project memory volumes are
    small enough that the bounded in-process cosine pass
    (MEMORY_CANDIDATE_LIMIT) is fast. Upgrading to pgvector + an HNSW index is
    a pure storage change (same 768-dim unit-normalised vectors).
    """
    __tablename__ = "semantic_memories"

    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # 'semantic' (LLM-extracted fact) — room for future kinds ('preference',
    # 'decision', 'constraint') without a schema change.
    kind = Column(String(30), nullable=False, default="semantic", server_default="semantic")
    content = Column(Text, nullable=False)
    # Unit-normalised float vector from the LM Studio embedding model.
    embedding = Column(JSON, nullable=False, default=list)
    # Embedding model id — lets the recall path detect a model/dimension change.
    embedding_model = Column(String(100), nullable=False, default="")
    source_message_id = Column(GUID, nullable=True, index=True)  # conversation_messages.id, no FK (chat may run standalone)
    recall_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_recalled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_semantic_memories_project_created", "project_id", "created_at"),
    )

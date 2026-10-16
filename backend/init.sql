-- ==========================================
-- Enterprise Banking Requirements Engineering System
-- PostgreSQL DDL Script (Optimized for Supabase Cloud)
-- ==========================================

-- Enable modern UUID generation extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ==========================================
-- 0. AUTOMATED AUDIT TRIGGER FUNCTION
-- ==========================================
-- Automatically updates the updated_at timestamp when a row is modified.
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ==========================================
-- 1. USERS TABLE
-- ==========================================
-- Stores information on banking stakeholders, product owners, and business analysts.
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL DEFAULT '',
    role VARCHAR(50) NOT NULL, -- e.g., 'Business Analyst', 'System Analyst', 'Product Owner', 'Technical Product Owner', 'Project Manager', 'Developer', 'QA'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_users
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 2. PROJECTS TABLE
-- ==========================================
-- Stores banking systems, digital products, and core-banking project contexts.
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, -- RESTRICT: Prevent user deletion if active projects exist for auditing
    name VARCHAR(255) NOT NULL,
    description TEXT,
    industry_standard VARCHAR(100) NOT NULL, -- e.g., 'Krungsri Nimble', 'ISO-20022', 'PCI-DSS'
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE, -- Pinned chats/projects float to the top of the sidebar
    is_flagged BOOLEAN NOT NULL DEFAULT FALSE, -- Dashboard ★ flag marker; independent of sidebar pinning
    status VARCHAR(50) NOT NULL DEFAULT 'draft', -- Dashboard workflow status: 'draft' | 'in_review_hpo' | 'in_review_po' | 'approved' | 'revised'
    last_approved_prd_version INTEGER,
    last_approved_audit_version INTEGER,
    last_approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    last_approved_at TIMESTAMPTZ,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_projects
    BEFORE UPDATE ON projects
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE TABLE project_review_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_status VARCHAR(50) NOT NULL,
    to_status VARCHAR(50) NOT NULL,
    action VARCHAR(50) NOT NULL,
    comment TEXT,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_name VARCHAR(255) NOT NULL,
    actor_role VARCHAR(100) NOT NULL,
    prd_version_number INTEGER,
    audit_version_reviewed INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_review_events_project_id ON project_review_events(project_id);

-- ==========================================
-- 3. EPICS TABLE
-- ==========================================
DROP TABLE IF EXISTS epics CASCADE;
CREATE TABLE epics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    epic_name VARCHAR(255) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(50) NOT NULL DEFAULT 'active'
);

CREATE TRIGGER handle_updated_at_epics
    BEFORE UPDATE ON epics
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 3b. REQUIREMENTS TABLE
-- ==========================================
DROP TABLE IF EXISTS requirements CASCADE;
CREATE TABLE requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    epic_id UUID REFERENCES epics(id) ON DELETE CASCADE,
    requirement_code VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_requirements
    BEFORE UPDATE ON requirements
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 4. USER STORIES TABLE
-- ==========================================
-- Captures Agile user stories formatted according to banking compliance requirements.
CREATE TABLE user_stories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    requirement_id UUID NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    ticket_code VARCHAR(50) NOT NULL, -- e.g., 'US-001', 'LN-402'
    story_title VARCHAR(255) NOT NULL,
    as_a TEXT NOT NULL,
    i_want_to TEXT NOT NULL,
    so_that TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    last_modified_by VARCHAR(100) NOT NULL DEFAULT 'automated_agent',
    change_type VARCHAR(50) NOT NULL DEFAULT 'created',
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Composite unique constraint for project-level ticket_code uniqueness
ALTER TABLE user_stories ADD CONSTRAINT uq_user_stories_project_id_ticket_code UNIQUE (project_id, ticket_code);

CREATE TRIGGER handle_updated_at_user_stories
    BEFORE UPDATE ON user_stories
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 5. ACCEPTANCE CRITERIA TABLE
-- ==========================================
-- Formatted criteria using the standard Given-When-Then specification for banking behavior.
CREATE TABLE acceptance_criteria (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_story_id UUID NOT NULL REFERENCES user_stories(id) ON DELETE CASCADE,
    criteria_text TEXT NOT NULL, -- Standardised Given-When-Then criteria block
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    last_modified_by VARCHAR(100) NOT NULL DEFAULT 'automated_agent',
    change_type VARCHAR(50) NOT NULL DEFAULT 'created',
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_acceptance_criteria
    BEFORE UPDATE ON acceptance_criteria
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 6. AUDIT RESULTS TABLE
-- ==========================================
-- Tracks checking runs on requirement drafts, validating banking logic and compliance rules.
CREATE TABLE audit_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requirement_id UUID NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    audit_version_reviewed INT NOT NULL,
    is_valid BOOLEAN NOT NULL DEFAULT FALSE,
    passed_checks JSONB NOT NULL DEFAULT '[]'::jsonb, -- Structured passed rule details
    failed_checks JSONB NOT NULL DEFAULT '[]'::jsonb, -- Structured failed rule details
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    verdict VARCHAR(50) NOT NULL DEFAULT 'needs_clarification',
    project_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    checklist_id VARCHAR(100) NOT NULL DEFAULT 'banking-core',
    checklist_version VARCHAR(30) NOT NULL DEFAULT '1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_audit_results
    BEFORE UPDATE ON audit_results
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE TABLE audit_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    audit_result_id UUID REFERENCES audit_results(id) ON DELETE SET NULL,
    run_number INTEGER NOT NULL,
    audit_version_reviewed INTEGER NOT NULL,
    is_valid BOOLEAN NOT NULL DEFAULT FALSE,
    verdict VARCHAR(50) NOT NULL,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    passed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    failed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    project_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    checklist_id VARCHAR(100) NOT NULL,
    checklist_version VARCHAR(30) NOT NULL,
    comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_audit_runs_project_number UNIQUE (project_id, run_number)
);

CREATE INDEX idx_audit_runs_project_id ON audit_runs(project_id);

CREATE TABLE audit_waivers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule_id VARCHAR(100) NOT NULL,
    target_requirement_id VARCHAR(100),
    reason TEXT NOT NULL,
    compensating_control TEXT,
    owner VARCHAR(255) NOT NULL,
    approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_by_name VARCHAR(255) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'active',
    revoked_reason TEXT,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_waivers_project_id ON audit_waivers(project_id);

-- ==========================================
-- 7. CLARIFICATION QUESTIONS TABLE
-- ==========================================
-- Stores clarification, query, and compliance verification dialogues regarding requirements.
CREATE TABLE clarification_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_result_id UUID NOT NULL REFERENCES audit_results(id) ON DELETE CASCADE,
    checklist_category VARCHAR(100) NOT NULL, -- e.g., 'Security', 'Regulatory Compliance', 'Edge Case'
    target_user_story_id UUID REFERENCES user_stories(id) ON DELETE SET NULL, -- Nullable FK for loose coupling
    question_text TEXT NOT NULL,
    user_answer TEXT, -- Nullable until answered by Business Stakeholder
    is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    source_references JSONB NOT NULL DEFAULT '[]'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_requirement_id VARCHAR(100),
    requirement_context TEXT,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_clarification_questions
    BEFORE UPDATE ON clarification_questions
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 8. PRD DOCUMENTS TABLE
-- ==========================================
-- Compiled final Production PRDs with markdown explanations and flow visualisations.
CREATE TABLE prd_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INT NOT NULL,
    prd_markdown TEXT NOT NULL,
    mermaid_diagram TEXT, -- System architecture flowcharts or state diagrams
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER handle_updated_at_prd_documents
    BEFORE UPDATE ON prd_documents
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 9b. PRD VERSIONS TABLE (IMMUTABLE VERSION HISTORY)
-- ==========================================
-- Dedicated immutable PRD version repository.
-- Each PRD generation creates a new version record.
-- Never overwrites previous versions.
-- Does NOT store version history inside requirement_states.
CREATE TABLE prd_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    generated_prd TEXT NOT NULL,
    generated_by VARCHAR(100) NOT NULL DEFAULT 'automated_agent',
    change_type VARCHAR(20) NOT NULL DEFAULT 'ai',
    change_summary TEXT,
    changed_sections JSONB,
    semver VARCHAR(20) NOT NULL DEFAULT '1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_prd_versions_project_id ON prd_versions(project_id);
CREATE INDEX idx_prd_versions_version_number ON prd_versions(project_id, version_number);

-- ==========================================
-- 9c. PRD SECTIONS TABLE (PART-LEVEL PRD EDITING / LOCKING / VERSIONING)
-- ==========================================
-- The PRD as a COLLECTION of editable, lockable, versioned PARTS (the nine
-- preview parts). section_key matches the frontend parsePRDToSections ids
-- ('title', 'stakeholders', 'version_history', 'reviews', 'contents',
-- 'business_overview', 'product_scope', 'tech_ops', 'appendix').
-- content_source: 'template' | 'ai' | 'human' — who owns the content.
-- ai_generatable FALSE => the Architect agent never regenerates the part.
-- review_status: 'draft' | 'satisfied' | 'approved'.
-- Lock columns follow the same pattern as every other artifact table.
CREATE TABLE prd_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    section_key VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    section_order INT NOT NULL DEFAULT 0,
    content_source VARCHAR(20) NOT NULL DEFAULT 'ai',
    ai_generatable BOOLEAN NOT NULL DEFAULT TRUE,
    review_status VARCHAR(30) NOT NULL DEFAULT 'draft',
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_by VARCHAR(100),
    locked_at TIMESTAMPTZ,
    lock_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_prd_sections_project_key UNIQUE (project_id, section_key)
);

CREATE INDEX idx_prd_sections_project_id ON prd_sections(project_id);

CREATE TRIGGER handle_updated_at_prd_sections
    BEFORE UPDATE ON prd_sections
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- ==========================================
-- 9d. PRD SECTION VERSIONS TABLE (APPEND-ONLY PER-PART HISTORY)
-- ==========================================
-- Immutable per-section version history. Every content change (manual edit,
-- revert, or an AI regeneration that actually changed the content) inserts a
-- new row. Nothing is ever overwritten or deleted. The current text is
-- materialized on prd_sections.content for fast reads.
CREATE TABLE prd_section_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id UUID NOT NULL REFERENCES prd_sections(id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    content TEXT NOT NULL,
    changed_by VARCHAR(100) NOT NULL DEFAULT 'user',
    change_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_prd_section_versions_section_version UNIQUE (section_id, version_number)
);

CREATE INDEX idx_prd_section_versions_section_id ON prd_section_versions(section_id);

-- ==========================================
-- 10. CONVERSATION MESSAGES TABLE
-- ==========================================
-- Dedicated table for persisting every conversation message per project.
-- Independent from requirement_states storage.
CREATE TABLE conversation_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    workflow_state VARCHAR(50) DEFAULT '',
    intent VARCHAR(50) DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversation_messages_project_id ON conversation_messages(project_id);
CREATE INDEX idx_conversation_messages_created_at ON conversation_messages(created_at);

-- ==========================================
-- 11. INDEXES FOR LOOK-UP OPTIMISATION & CONSTRAINT SPEED
-- ==========================================

-- Primary key lookups are indexed automatically.
-- Foreign Key Indexes (Required to prevent table scans on joins and deletes)
CREATE INDEX idx_projects_user_id ON projects(user_id);
CREATE INDEX idx_epics_project_id ON epics(project_id);
CREATE INDEX idx_requirements_project_id ON requirements(project_id);
CREATE INDEX idx_user_stories_requirement_id ON user_stories(requirement_id);
CREATE INDEX idx_acceptance_criteria_user_story_id ON acceptance_criteria(user_story_id);
CREATE INDEX idx_audit_results_requirement_id ON audit_results(requirement_id);
CREATE INDEX idx_clarification_questions_audit_result_id ON clarification_questions(audit_result_id);
CREATE INDEX idx_clarification_questions_target_user_story_id ON clarification_questions(target_user_story_id);
CREATE INDEX idx_prd_documents_project_id ON prd_documents(project_id);

-- Frequently Queried Status & Categories Indexes (To accelerate dashboard metrics and filter runs)
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_epics_is_locked ON epics(is_locked);
CREATE INDEX idx_audit_results_is_valid ON audit_results(is_valid);
CREATE INDEX idx_clarification_questions_is_resolved ON clarification_questions(is_resolved);
CREATE INDEX idx_clarification_questions_category ON clarification_questions(checklist_category);

-- ==========================================
-- 12. ARTIFACT EVENT LOGS TABLE (APPEND-ONLY)
-- ==========================================
-- Immutable, append-only event log for tracking all artifact modifications.
-- No foreign keys — purely historical traceability, not production data.
-- Never overwrites or deletes existing records.
CREATE TABLE artifact_event_logs (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    artifact_type VARCHAR(50) NOT NULL,       -- e.g. 'epic', 'requirement', 'user_story', 'acceptance_criteria', 'prd'
    artifact_id VARCHAR(36) NOT NULL,          -- UUID of the modified artifact
    action VARCHAR(20) NOT NULL,               -- CREATE, UPDATE, DELETE, ARCHIVE, LOCK, UNLOCK
    old_value JSONB,                           -- Snapshot of the artifact before the change
    new_value JSONB,                           -- Snapshot of the artifact after the change
    performed_by VARCHAR(100) NOT NULL DEFAULT 'automated_agent',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX idx_artifact_event_logs_artifact ON artifact_event_logs(artifact_type, artifact_id);
CREATE INDEX idx_artifact_event_logs_project_id ON artifact_event_logs(project_id);
CREATE INDEX idx_artifact_event_logs_user_id ON artifact_event_logs(user_id);
CREATE INDEX idx_artifact_event_logs_action ON artifact_event_logs(action);
CREATE INDEX idx_artifact_event_logs_timestamp ON artifact_event_logs(timestamp);

-- ==========================================
-- 13. SEMANTIC MEMORIES TABLE
-- ==========================================
-- Project-scoped distilled facts (Option A memory layer) extracted from chat
-- turns by a small LLM pass, embedded with the local LM Studio embedding model
-- and recalled at prompt-build time by blended cosine-relevance + recency
-- scoring. Mirrors alembic/versions/0008_add_semantic_memories.py.
-- Embeddings are stored as a JSON float array (not pgvector) so the schema
-- works on both Supabase Postgres and the SQLite dev fallback.
CREATE TABLE semantic_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind VARCHAR(30) NOT NULL DEFAULT 'semantic',   -- 'semantic'; room for future kinds ('preference', 'decision', ...)
    content TEXT NOT NULL,
    embedding JSONB NOT NULL,                        -- unit-normalised float vector (JSON array)
    embedding_model VARCHAR(100) NOT NULL DEFAULT '',
    source_message_id UUID,                          -- conversation_messages.id, no FK (chat may run standalone)
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_semantic_memories_project_id ON semantic_memories(project_id);
CREATE INDEX idx_semantic_memories_source_message_id ON semantic_memories(source_message_id);

-- ==========================================
-- 14. ARTIFACT DEPENDENCY GRAPH
-- ==========================================
CREATE TABLE artifact_dependencies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_type VARCHAR(50) NOT NULL,
    source_key VARCHAR(255) NOT NULL,
    target_type VARCHAR(50) NOT NULL,
    target_key VARCHAR(255) NOT NULL,
    relationship VARCHAR(50) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_artifact_dependencies_edge UNIQUE (project_id, source_type, source_key, target_type, target_key, relationship)
);

CREATE INDEX idx_artifact_dependencies_project_id ON artifact_dependencies(project_id);
CREATE INDEX idx_artifact_dependencies_source ON artifact_dependencies(project_id, source_type, source_key);
CREATE INDEX idx_artifact_dependencies_target ON artifact_dependencies(project_id, target_type, target_key);

CREATE TABLE regeneration_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    requested_by VARCHAR(255) NOT NULL,
    trigger_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(30) NOT NULL DEFAULT 'planned',
    regenerated_sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    skipped_locked_sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagram_regenerated BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_regeneration_runs_project_id ON regeneration_runs(project_id);

-- ==========================================
-- 15. REUSABLE BANKING KNOWLEDGE + RAG
-- ==========================================
CREATE TABLE banking_knowledge_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    original_format VARCHAR(20) NOT NULL,
    mime_type VARCHAR(100),
    document_type VARCHAR(100) NOT NULL DEFAULT 'best_practice',
    jurisdiction VARCHAR(100) NOT NULL DEFAULT 'global',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_markdown TEXT NOT NULL,
    content_checksum VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(30) NOT NULL DEFAULT 'draft',
    approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_banking_knowledge_owner_checksum UNIQUE (owner_user_id, content_checksum)
);

CREATE INDEX idx_banking_knowledge_documents_owner_user_id ON banking_knowledge_documents(owner_user_id);

CREATE TABLE banking_knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES banking_knowledge_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading VARCHAR(500),
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    embedding JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding_model VARCHAR(100) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_banking_knowledge_chunk_index UNIQUE (document_id, chunk_index)
);

CREATE INDEX idx_banking_knowledge_chunks_document_id ON banking_knowledge_chunks(document_id);

CREATE TABLE banking_knowledge_retrievals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query_text TEXT NOT NULL,
    retrieved_chunks JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding_used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_banking_knowledge_retrievals_project_id ON banking_knowledge_retrievals(project_id);
CREATE INDEX idx_banking_knowledge_retrievals_owner_user_id ON banking_knowledge_retrievals(owner_user_id);

-- ==========================================
-- 16. PERSISTENT BACKGROUND GENERATION JOBS
-- ==========================================
CREATE TABLE generation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    requested_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_type VARCHAR(30) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    progress_stage VARCHAR(100) NOT NULL DEFAULT 'queued',
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB,
    error_message TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_generation_jobs_project_id ON generation_jobs(project_id);
CREATE INDEX idx_generation_jobs_requested_by_user_id ON generation_jobs(requested_by_user_id);
CREATE INDEX idx_generation_jobs_status ON generation_jobs(status);
CREATE INDEX idx_generation_jobs_project_status ON generation_jobs(project_id, status);
CREATE UNIQUE INDEX uq_generation_jobs_active_project ON generation_jobs(project_id)
    WHERE status IN ('queued', 'running', 'cancelling');

// ============================================================================
// API wire types — the typed contract between the React frontend and the
// FastAPI backend (`backend/app/schemas.py`).
//
// These mirror the Pydantic response models so API payloads are never handled
// as `any`. The domain state shapes consumed by components live in
// `src/components/types.ts`; `src/api/transforms.ts` converts wire payloads
// into those domain shapes at the hook boundary.
// ============================================================================

// ----------------------------------------------------------------------------
// Projects
// ----------------------------------------------------------------------------

export interface ProjectSummary {
  id: string;
  name: string;
  description: string | null;
  industry_standard: string;
  is_pinned?: boolean;
  /** Dashboard ★ flag marker; independent of the sidebar pin. */
  is_flagged?: boolean;
  /** User-editable workflow status ('draft' | 'in_review_hpo' | 'in_review_po' | 'approved' | 'revised') shown on the dashboard table. */
  status?: string;
  last_approved_prd_version?: number | null;
  last_approved_audit_version?: number | null;
  last_approved_by?: string | null;
  last_approved_at?: string | null;
  /** ISO-8601 timestamp of the last project update (dashboard 'Updated' column). */
  updated_at?: string | null;
  /** Short excerpt of a matching conversation message, present only when the project matched via message content. */
  match_snippet?: string | null;
}

/** Request body for setting a project's user-editable workflow status (dashboard table). */
export interface ProjectStatusRequest {
  status: ProjectStatus;
  comment?: string;
}

/** Workflow statuses editable from the dashboard projects table. */
export type ProjectStatus =
  "draft" | "in_review_hpo" | "in_review_po" | "approved" | "revised";

export interface ProjectReviewEventPayload {
  id: string;
  project_id: string;
  from_status: ProjectStatus;
  to_status: ProjectStatus;
  action: string;
  comment?: string | null;
  actor_id?: string | null;
  actor_name: string;
  actor_role: string;
  prd_version_number?: number | null;
  prd_semver?: string | null;
  audit_version_reviewed?: number | null;
  created_at?: string | null;
}

export interface ProjectHealthPayload {
  project_id: string;
  status: "ready" | "attention_required" | "blocked" | "approved";
  workflow_status: ProjectStatus;
  /** @deprecated Use coverage_breakdown.requirement_traceability_percent. */
  coverage_percent: number;
  coverage_breakdown: {
    requirement_traceability_percent: number;
    requirements_with_stories_percent: number;
    stories_with_acceptance_criteria_percent: number;
    requirements_with_prd_reference_percent: number;
    requirements_with_diagram_reference_percent: number;
  };
  metrics: {
    total_requirements: number;
    total_user_stories: number;
    traced_requirements: number;
    blocking_findings: number;
    warnings: number;
    suggestions: number;
    unresolved_questions: number;
    pending_prd_sections: number;
    requirements_without_diagram: number;
    active_waivers: number;
  };
  audit: {
    verdict?: string | null;
    version_reviewed?: number | null;
    checklist_version?: string | null;
    stale: boolean;
    updated_at?: string | null;
  };
  approval: {
    prd_version?: number | null;
    audit_version?: number | null;
    approved_at?: string | null;
  };
  issues: Array<{
    kind: string;
    label: string;
    count: number;
    destination: string;
    severity: string;
  }>;
  generated_at: string;
}

export interface AuditWaiverPayload {
  id: string;
  project_id: string;
  rule_id: string;
  target_requirement_id?: string | null;
  reason: string;
  compensating_control?: string | null;
  owner: string;
  approved_by?: string | null;
  approved_by_name: string;
  expires_at: string;
  status: "active" | "expired" | "revoked" | string;
  revoked_reason?: string | null;
  revoked_at?: string | null;
  created_at?: string | null;
}

export interface AuditRunPayload {
  id: string;
  project_id: string;
  run_number: number;
  audit_version_reviewed: number;
  is_valid: boolean;
  verdict: string;
  findings: Array<
    AuditFindingPayload & {
      finding_key: string;
      lifecycle: "new" | "reopened" | "unchanged";
      waiver?: AuditWaiverPayload | null;
    }
  >;
  passed_checks: string[];
  failed_checks: string[];
  checklist_id: string;
  checklist_version: string;
  comparison: {
    new: string[];
    reopened: string[];
    unchanged: string[];
    resolved: string[];
  };
  created_at?: string | null;
}

export interface ProjectCreated {
  id: string;
  name: string;
}

export interface ProjectDeleteResponse {
  status: string;
  project_id: string;
}

// ----------------------------------------------------------------------------
// User stories (as serialized by backend `serialize_user_story` + enrichment)
// ----------------------------------------------------------------------------

export interface UserStoryPayload {
  id?: string;
  project_id?: string;
  ticket_code: string;
  story_title: string;
  as_a: string;
  i_want_to: string;
  so_that: string;
  acceptance_criteria?: string[];
  status?: string;
  version?: number;
  last_modified_by?: string | null;
  change_type?: string | null;
  is_locked?: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
}

// ----------------------------------------------------------------------------
// Requirements
// ----------------------------------------------------------------------------

export interface RequirementPayload {
  id?: string;
  project_id?: string;
  epic_id?: string | null;
  requirement_code: string;
  title: string;
  functional_requirement?: string;
  description?: string;
  status?: string;
  is_locked?: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
  user_stories?: UserStoryPayload[];
}

// ----------------------------------------------------------------------------
// Clarification questions / audit results
// ----------------------------------------------------------------------------

export interface ClarificationQuestionPayload {
  id?: string;
  project_id?: string;
  checklist_category: string;
  target_user_story_id?: string | null;
  question_text: string;
  user_answer?: string | null;
  is_resolved?: boolean;
  source_references?: AuditSourceReferencePayload[];
  provenance?: {
    type: "Project file" | "Knowledge base" | "System analysis";
    document?: string | null;
    section?: string | null;
    analyzed_target?: string | null;
  };
  target_requirement_id?: string | null;
  requirement_context?: string | null;
}

export interface AuditSourceReferencePayload {
  document_id?: string | null;
  document_name: string;
  section?: string | null;
  excerpt?: string | null;
  source_type?: "Project file" | "Knowledge base" | null;
}

export interface AuditFindingPayload {
  rule_id: string;
  finding_type: string;
  severity: string;
  category: string;
  target_requirement_id?: string | null;
  description: string;
  source_references: AuditSourceReferencePayload[];
  evidence_status: string;
  applicability:
    | "applicable_required"
    | "applicable_recommended"
    | "not_applicable"
    | "unknown";
  impact: "blocking" | "warning" | "suggestion";
  confidence: number;
  rationale?: string | null;
  recommendation?: {
    summary: string;
    proposed_requirement_text?: string | null;
    proposed_acceptance_criteria: string[];
    expected_benefit?: string | null;
  } | null;
}

export interface AuditProjectContextPayload {
  business_segment: string;
  product_domain: string;
  solution_type: string;
  financial_transaction?: boolean | null;
  external_integration?: boolean | null;
  sensitive_data?: boolean | null;
  delivery_stage: string;
  confidence: number;
}

export interface AuditResultPayload {
  id?: string;
  project_id?: string;
  is_valid: boolean;
  audit_version_reviewed?: number;
  passed_checks: string[];
  failed_checks: string[];
  findings?: AuditFindingPayload[];
  source_references?: AuditSourceReferencePayload[];
  clarification_questions?: ClarificationQuestionPayload[];
  verdict?: "pass" | "pass_with_warnings" | "needs_clarification" | "fail";
  project_context?: AuditProjectContextPayload;
  checklist_id?: string;
  checklist_version?: string;
}

// ----------------------------------------------------------------------------
// Conversation history
// ----------------------------------------------------------------------------

export interface ConversationMessagePayload {
  id: string;
  project_id?: string;
  role: string;
  message?: string;
  content?: string;
  workflow_state?: string;
  intent?: string;
  created_at: string;
  /** Legacy display timestamp used by the chat UI when created_at is missing. */
  timestamp?: string;
}

// ----------------------------------------------------------------------------
// Pending (merge) actions awaiting human confirmation
// ----------------------------------------------------------------------------

export interface PendingActionPayload {
  id: string;
  project_id: string;
  action_type: string;
  target_requirement_id?: string | null;
  original_user_message: string;
  /** Proposed merged requirement state. */
  proposed_changes?: Record<string, unknown>;
  affected_user_story_ids?: string[];
  affected_acceptance_criteria_ids?: string[];
  workflow_stage?: string;
  status?: string;
  expires_at?: string;
}

// ----------------------------------------------------------------------------
// Requirement Impact Analysis (merge preview window)
// ----------------------------------------------------------------------------

/** One requirement-level change predicted by the pending merge. */
export interface RequirementImpact {
  requirement_code: string;
  title: string;
  /** 'created' | 'updated' | 'removed' | 'unchanged'. */
  change_kind: string;
  changed_fields: string[];
}

/** One user-story-level change predicted by the pending merge. */
export interface StoryImpact {
  ticket_code: string;
  story_title: string;
  requirement_code: string;
  /** 'created' | 'updated' | 'removed' | 'unchanged'. */
  change_kind: string;
  changed_fields: string[];
  criteria_added: number;
  criteria_removed: number;
  criteria_review_required: number;
}

/** A PRD section whose content references an affected code. */
export interface ImpactedSection {
  section_key: string;
  title: string;
  referenced_codes: string[];
  is_locked: boolean;
  /** 'references_changed' | 'references_removed' (dangling after merge). */
  impact_kind: string;
}

/** A stored PRD-version diagram referencing an affected code. */
export interface ImpactedDiagram {
  label: string;
  version: number;
  referenced_codes: string[];
}

/** Rollup counts for the impact window header chips. */
export interface ImpactSummary {
  requirements_created: number;
  requirements_updated: number;
  requirements_removed: number;
  stories_created: number;
  stories_updated: number;
  stories_removed: number;
  criteria_added: number;
  criteria_removed: number;
  criteria_review_required: number;
  sections_impacted: number;
  sections_impacted_locked: number;
  diagrams_impacted: number;
}

/** READ-ONLY impact analysis for one pending merge action. */
export interface ImpactAnalysisPayload {
  action_id: string;
  project_id: string;
  action_type: string;
  original_user_message: string;
  current_version: number;
  proposed_version?: number | null;
  has_changes: boolean;
  summary: ImpactSummary;
  requirements: RequirementImpact[];
  user_stories: StoryImpact[];
  impacted_prd_sections: ImpactedSection[];
  impacted_diagrams: ImpactedDiagram[];
}

// ----------------------------------------------------------------------------
// Flattened requirement-state payload (`GET/PUT /api/project/{id}`,
// `POST /api/clarification/submit`)
// ----------------------------------------------------------------------------

/**
 * `requirements` is normally an array, but a few legacy code paths still emit a
 * single `{ epic_name }` object — the field type reflects that defensive shape.
 */
export type RequirementsField =
  RequirementPayload[] | { epic_name?: string } | null | undefined;

export interface RequirementStatePayload {
  passed_checks?: string[];
  failed_checks?: string[];
  audit_findings?: AuditFindingPayload[];
  audit_source_references?: AuditSourceReferencePayload[];
  audit_verdict?: AuditResultPayload["verdict"];
  audit_project_context?: AuditProjectContextPayload;
  audit_checklist_id?: string;
  audit_checklist_version?: string;
  project_id?: string;
  project_name?: string | null;
  epic_name?: string | null;
  requirements?: RequirementsField;
  business_goals?: Array<string | Record<string, unknown>>;
  actors?: Array<string | Record<string, unknown>>;
  user_stories?: UserStoryPayload[];
  acceptance_criteria?: Array<string | Record<string, unknown>>;
  clarification_questions?: ClarificationQuestionPayload[];
  validation_status?: string | null;
  generated_prd?: string;
  generated_diagrams?: string;
  diagram_generated_at?: string | null;
  current_workflow_state?: string | null;
  version_number?: number;
  updated_at?: string | null;
  conversation_history?: ConversationMessagePayload[];
  pending_action_id?: string | null;
  pending_merge?: boolean;
  clarification_answer_classifications?: Array<{
    question_id: string;
    classification: "UPDATE" | "ADD" | "REMOVE" | "NO_CHANGE" | "FOLLOW_UP";
    target_requirement_id?: string | null;
    rationale?: string;
    follow_up_question?: string | null;
  }>;
  proposal_generation_warning?: string | null;
  proposal_no_change_reason?: string | null;
}

// ----------------------------------------------------------------------------
// `POST /api/process-requirements`
// ----------------------------------------------------------------------------

export interface ProcessRequirementsRequest {
  project_id: string;
  raw_input?: string;
  current_version?: number;
  version_history_summaries?: string;
  target_agent?: string;
  /** Rebuild the PRD from persisted artifacts even when their change flags are unchanged. */
  force_prd_rebuild?: boolean;
  structured_requirements?: Record<string, unknown>;
  /** The SPA persists exactly the bubbles it renders through the conversation API. */
  client_persists_timeline?: boolean;
}

export interface ConversationMessageCreatePayload {
  id?: string;
  role: string;
  message: string;
  workflow_state?: string;
  intent?: string;
}

export interface GatheredRequirementsPayload {
  epic_name?: string;
  version?: number;
  user_stories?: UserStoryPayload[];
  requirements?: RequirementPayload[];
  [key: string]: unknown;
}

export interface ProcessRequirementsResponse {
  status: string;
  detected_intent?: string | null;
  workflow_routing?: Record<string, unknown> | null;
  structured_requirements?: GatheredRequirementsPayload | null;
  audit_result?: AuditResultPayload | null;
  prd_markdown?: string | null;
  mermaid_diagram?: string | null;
  diagram_generated_at?: string | null;
  message?: string | null;
  pending_merge?: boolean | null;
  pending_action_id?: string | null;
  no_changes?: boolean;
  stale_result?: boolean | null;
  skipped_locked_prd_sections?: string[];
  conversation_history?: ConversationMessagePayload[];
  [key: string]: unknown;
}

// ----------------------------------------------------------------------------
// Locks
// ----------------------------------------------------------------------------

export interface ArtifactLockMetadata {
  id?: string;
  artifact_type?: string;
  artifact_id?: string;
  is_locked: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
}

export interface ArtifactLockResponse {
  status: string;
  artifact: ArtifactLockMetadata | Record<string, unknown>;
}

// ----------------------------------------------------------------------------
// Health probe (`GET /api/health`)
// ----------------------------------------------------------------------------

export interface HealthPayload {
  status: string;
  app_name: string;
  local_inference_gateway: string;
  macbook_context_window_budget: string;
  lm_studio_online: boolean;
  lm_studio_model?: string | null;
  lm_studio_model_loaded: boolean;
  lm_studio_loaded_models: string[];
  lm_studio_latency_ms?: number | null;
  lm_studio_error?: string | null;
  lm_studio_last_checked?: string | null;
}

// ----------------------------------------------------------------------------
// Action confirmation responses
// ----------------------------------------------------------------------------

export interface ConfirmActionResponse {
  status: string;
  requirement_state?: RequirementStatePayload;
  /** Semantic version of an approved PRD snapshot. */
  prd_semver?: string | null;
  cascade_job_id?: string | null;
  cascade_status?: "queued" | "not_required" | "deferred" | string | null;
}

export interface ActionStatusResponse {
  status: string;
}

// ----------------------------------------------------------------------------
// Uploaded documents (`/api/project/{id}/documents/*`)
//
// Uploading ONLY adds knowledge-base source material; extraction is a separate,
// explicit action whose output stays a DRAFT pending_action until confirmed.
// ----------------------------------------------------------------------------

export type DocumentFormat = "docx" | "pdf" | "md" | "txt";

export interface UploadedDocumentPayload {
  id: string;
  project_id: string;
  original_filename: string;
  original_format: DocumentFormat | string;
  mime_type?: string | null;
  content_markdown?: string | null;
  file_size_bytes: number;
  token_count: number;
  status: "processed" | "failed" | string;
  uploaded_by: string;
  extraction_status:
    "not_extracted" | "extraction_pending" | "extraction_applied" | string;
  created_at: string;
  updated_at: string;
  /** Present on scanned-PDF failures: explicit needs-OCR message. */
  message?: string | null;
}

export interface DocumentMarkdownPayload extends UploadedDocumentPayload {
  content_markdown: string;
}

export interface ProcessDocumentResponse {
  status: "draft_ready" | "failed" | string;
  mode: "full" | "chunked" | string;
  chunk_count: number;
  processed_tokens: number;
  pending_action_id?: string | null;
  excluded_anything: boolean;
  document_id: string;
  message?: string | null;
}

export interface DocumentDeleteResponse {
  status: string;
  document_id: string;
  project_id: string;
  original_filename: string;
}

// ----------------------------------------------------------------------------
// PRD preview normalization (`POST /api/prd/convert`)
//
// Generated PRDs are Krungsri LaTeX bodies; this endpoint turns them (or a
// legacy Markdown PRD) into clean GFM markdown so the on-screen preview never
// renders raw LaTeX.
// ----------------------------------------------------------------------------

export interface ConvertedPrdMarkdownPayload {
  markdown: string;
  source_kind: "latex" | "markdown" | string;
}

// ----------------------------------------------------------------------------
// PRD sections (`/api/project/{id}/prd/sections/*`)
//
// The PRD is a COLLECTION of editable, lockable, versioned PARTS (the nine
// preview parts). `section_key` matches the preview section ids produced by
// `parsePRDToSections` ('title', 'stakeholders', 'version_history', 'reviews',
// 'contents', 'business_overview', 'product_scope', 'tech_ops', 'appendix').
// ----------------------------------------------------------------------------

export interface PrdSectionPayload {
  id: string;
  project_id: string;
  section_key: string;
  title: string;
  content: string;
  section_order: number;
  /** 'template' | 'ai' | 'human' — who owns the content. */
  content_source: "template" | "ai" | "human" | string;
  /** False => the Architect agent never regenerates this part. */
  ai_generatable: boolean;
  /** 'draft' | 'satisfied' | 'approved'. */
  review_status: string;
  is_locked: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  /** Latest version number in prd_section_versions. */
  version_number?: number | null;
  created_at: string;
  updated_at: string;
}

export interface PrdSectionListPayload {
  project_id: string;
  sections: PrdSectionPayload[];
}

export interface PrdSectionUpdateResponse {
  section: PrdSectionPayload;
  /** Full PRD markdown stitched from ALL parts after the edit. */
  document_markdown: string;
}

// ----------------------------------------------------------------------------
// PRD version ledger (`/api/project/{id}/prd-versions*`)
//
// EVERY document change — an AI regeneration or a manual part edit/revert —
// appends an immutable `prd_versions` snapshot. The version ALWAYS advances,
// and each snapshot carries `change_type` ('ai' | 'manual'), a change summary
// and the per-section change records (incl. `locked_preserved` markers proving
// the lock contract was honoured).
// ----------------------------------------------------------------------------

export interface PrdVersionChangedSection {
  section_key: string;
  title: string;
  /** 'created' | 'updated' | 'unchanged' | 'removed' | 'locked_preserved'. */
  change_kind: string;
  changed: boolean;
}

export interface PrdVersionPayload {
  version_id: string;
  project_id: string;
  version_number: number;
  /** Semantic Version (MAJOR.MINOR.PATCH): MAJOR = sections created/removed,
   *  MINOR = AI content updates, PATCH = manual edits / no-change advances. */
  semver: string;
  /** Full PRD markdown snapshot at this version (used for diffing / restore). */
  generated_prd: string;
  generated_by: string;
  /** 'ai' | 'manual' — origin of the snapshot. */
  change_type: string;
  change_summary: string | null;
  changed_sections: PrdVersionChangedSection[] | null;
  created_at: string;
}

export interface PrdVersionDiffSectionPayload {
  section_key: string;
  title: string;
  change_kind: string;
  changed: boolean;
  added: number;
  removed: number;
  /** Diff lines: [['add'|'del'|'context', line], ...]. */
  diff_lines: [string, string][];
}

export interface PrdVersionDiffPayload {
  project_id: string;
  to_version: number;
  base_version: number;
  sections: PrdVersionDiffSectionPayload[];
}

export interface PrdVersionRestorePayload {
  /** Ledger version number the document was restored from. */
  restored_from_version: number;
  /** The NEW version number appended for this restore (append-only ledger). */
  new_version_number: number;
  /** Semantic version of the NEW appended snapshot. */
  semver: string;
  /** Full PRD markdown after the restore (locked sections preserved). */
  document_markdown: string;
  restored_sections: number;
  preserved_locked_sections: number;
}

// ============================================================================
// Requirement Traceability Matrix (derived, read-only)
// GET /api/project/{id}/traceability — links Requirements <-> User Stories
// <-> Acceptance Criteria <-> PRD sections <-> diagrams via FKs + REQ-/US- code
// references inside section content and mermaid source. See
// backend/app/traceability_service.py for how links are derived.
// ============================================================================
export interface TraceabilityRequirementInfo {
  id: string;
  requirement_code: string;
  title: string;
  description: string;
  epic_name?: string | null;
  status?: string | null;
  version?: number | null;
  is_locked: boolean;
}

export interface TraceabilityStory {
  id?: string | null;
  ticket_code?: string | null;
  story_title?: string | null;
  as_a?: string | null;
  i_want_to?: string | null;
  so_that?: string | null;
  acceptance_criteria: string[];
  criteria_needing_review: string[];
  is_locked: boolean;
}

export interface TraceabilityRow {
  requirement: TraceabilityRequirementInfo;
  user_stories: TraceabilityStory[];
  /** PRD section keys referencing this requirement (or its stories). */
  prd_sections: string[];
  /** Diagram labels referencing this requirement (e.g. 'PRD v3'). */
  diagrams: string[];
  /** True when the requirement has stories with criteria AND a section reference. */
  traced: boolean;
}

export interface TraceabilityDiagram {
  label: string;
  version: number;
}

export interface StaleCodeReference {
  code: string;
  section_keys?: string[];
  diagrams?: string[];
}

export interface TraceabilityCoverage {
  total_requirements: number;
  total_user_stories: number;
  total_acceptance_criteria: number;
  traced_requirements: number;
  requirements_without_stories: string[];
  requirements_without_prd_sections: string[];
  requirements_without_diagram: string[];
  stories_without_criteria: string[];
  stories_without_prd_sections: string[];
  criteria_needing_review: string[];
  stale_section_references: StaleCodeReference[];
  stale_diagram_references: StaleCodeReference[];
}

export interface TraceabilityPayload {
  project_id: string;
  rows: TraceabilityRow[];
  diagrams: TraceabilityDiagram[];
  coverage: TraceabilityCoverage;
}

export type DependencyArtifactType =
  | "requirement"
  | "user_story"
  | "acceptance_criterion"
  | "prd_section"
  | "diagram";

export interface ArtifactReferencePayload {
  artifact_type: DependencyArtifactType;
  artifact_key: string;
}

export interface DependencyGraphPayload {
  project_id: string;
  nodes: Array<{
    id: string;
    artifact_type: DependencyArtifactType;
    artifact_key: string;
    label: string;
    metadata: Record<string, unknown>;
  }>;
  edges: Array<{
    source: string;
    target: string;
    relationship: string;
    evidence: Record<string, unknown>;
  }>;
  summary: {
    nodes: number;
    edges: number;
    requirements: number;
    stories: number;
    sections: number;
    diagrams: number;
  };
  rebuilt_at: string;
}

export interface RegenerationPlanPayload {
  project_id: string;
  changed_artifacts: ArtifactReferencePayload[];
  affected_user_stories: string[];
  regenerate_prd_sections: string[];
  skipped_prd_sections: string[];
  regenerate_diagrams: string[];
  affected_node_ids: string[];
  summary: {
    stories: number;
    sections: number;
    skipped_sections: number;
    diagrams: number;
  };
}

export interface RegenerationResultPayload {
  run_id: string;
  status: "completed" | "no_changes" | "failed" | string;
  plan: RegenerationPlanPayload;
  generated_prd?: string;
  generated_diagrams?: string;
  error_message?: string;
}

export interface GenerationJobPayload<T = unknown> {
  id: string;
  project_id: string;
  job_type: "architect" | "regeneration";
  status:
    "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled";
  progress_stage: string;
  result?: T | null;
  error_message?: string | null;
  cancel_requested: boolean;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  cancelled?: boolean;
}

export interface BankingKnowledgeDocumentPayload {
  id: string;
  title: string;
  original_filename: string;
  original_format: string;
  mime_type?: string | null;
  document_type: string;
  jurisdiction: string;
  tags: string[];
  version: number;
  status: "draft" | "approved" | "retired" | string;
  approved_by?: string | null;
  approved_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  content_markdown?: string;
  chunk_count?: number;
  embedding_status?: "ready" | "lexical_only";
}

// ----------------------------------------------------------------------------
// Authentication (backend/app/routes/auth.py)
// ----------------------------------------------------------------------------

/** Public user projection returned by login / register / me (`UserResponse`). */
export interface AuthUserPayload {
  id: string;
  email: string;
  full_name: string;
  role: string;
  /** Users table has no is_active column yet — always true for now. */
  is_active: boolean;
  created_at?: string | null;
}

/** Response from POST /api/auth/login (`LoginResponse`). */
export interface AuthSessionPayload {
  access_token: string;
  token_type: string;
  user: AuthUserPayload;
}

/** Response from POST /api/auth/register (`RegisterResponse`). */
export interface AuthRegisterResponse {
  message: string;
  user: AuthUserPayload;
}

/** Request body for POST /api/auth/register (`RegisterRequest`). */
export interface AuthRegisterRequest {
  email: string;
  password: string;
  full_name: string;
  role?: string;
}

/**
 * Response from GET /api/auth/config — an unauthenticated capabilities probe so
 * the UI knows whether to offer a sign-up form before it holds a token.
 */
export interface AuthConfigPayload {
  signup_enabled: boolean;
  token_expiration_minutes: number;
}

/** Response from POST /api/auth/logout and /api/auth/change-password. */
export interface AuthMessageResponse {
  message: string;
}

export interface AuthAccountDeleteResponse extends AuthMessageResponse {
  deleted_projects: number;
}

/** Roles offered by the sign-up form (mirrors the roles documented in init.sql
 *  and enforced server-side by POST /api/auth/register). */
export const AUTH_ROLES = [
  "Technical Product Owner",
  "Business Analyst",
  "Product Owner",
] as const;

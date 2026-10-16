// Shared domain types used across the Requirements Engine workspace.
// Extracted from the former monolithic Dashboard.tsx so every split module
// (ChatPanel, PRDEditor, VersionHistory, MarkdownRenderer,
// useProjectState) can share one source of truth.

export interface UserStory {
  ticket_code: string;
  story_title: string;
  as_a: string;
  i_want_to: string;
  so_that: string;
  acceptance_criteria: string[];
}

export interface RequirementItem {
  id?: string;
  requirement_code: string;
  title: string;
  description?: string;
  user_stories: UserStory[];
  /** 'active' | 'archived' | 'deleted' — mirrors the stored record status. */
  status?: string;
  is_locked?: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
}

export interface StructuredRequirements {
  epic_name: string;
  version: number;
  user_stories: UserStory[];
  requirements?: RequirementItem[];
}

export interface ClarificationQuestion {
  /** Persisted question UUID when the audit result has been saved. */
  id?: string;
  checklist_category: string;
  target_user_story_id?: string | null;
  question_text: string;
  user_answer?: string | null;
  is_resolved?: boolean;
  source_references?: import('../api/types').AuditSourceReferencePayload[];
  provenance?: {
    type: 'Project file' | 'Knowledge base' | 'System analysis';
    document?: string | null;
    section?: string | null;
    analyzed_target?: string | null;
  };
  target_requirement_id?: string | null;
  requirement_context?: string | null;
}

export interface AuditResult {
  is_valid: boolean;
  audit_version_reviewed: number;
  passed_checks: string[];
  failed_checks: string[];
  clarification_questions?: ClarificationQuestion[];
  findings?: import('../api/types').AuditFindingPayload[];
  source_references?: import('../api/types').AuditSourceReferencePayload[];
  verdict?: 'pass' | 'pass_with_warnings' | 'needs_clarification' | 'fail';
  project_context?: import('../api/types').AuditProjectContextPayload;
  checklist_id?: string;
  checklist_version?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'gatherer' | 'auditor' | 'architect' | string;
  content: string;
  timestamp: string;
  isPendingClarifications?: boolean;
  auditResultSnapshot?: AuditResult;
}

export interface VersionHistory {
  version: number;
  /** Semantic Version (MAJOR.MINOR.PATCH) of this snapshot.
   *  MAJOR = sections created/removed, MINOR = AI content updates,
   *  PATCH = manual edits / no-change advances. */
  semVersion?: string;
  timestamp: string;
  author: string;
  description: string;
  /** 'ai' | 'manual' — origin of the snapshot (Generate PRD vs manual edit). */
  changeType?: 'ai' | 'manual' | string;
  /** Per-section change records incl. 'locked_preserved' markers. */
  changedSections?: VersionSectionChange[];
  requirementsSnapshot?: StructuredRequirements;
}

/** One per-section change record attached to a PRD version snapshot. */
export interface VersionSectionChange {
  section_key: string;
  title: string;
  /** 'created' | 'updated' | 'unchanged' | 'removed' | 'locked_preserved'. */
  change_kind: string;
  changed: boolean;
}

// PRD Section Types and helper utilities
export interface PRDSection {
  id: string;
  title: string;
  content: string;
}

/** Per-part PRD section lock/ownership state mirrored from the backend. */
export interface PrdSectionLockState {
  is_locked: boolean;
  locked_by?: string | null;
  lock_reason?: string | null;
  review_status: string;
  content_source: string;
  ai_generatable: boolean;
  version_number?: number | null;
}

// Optimistic lock state tracked in the UI for generic artifacts.
export interface ArtifactLockState {
  is_locked: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
}

// Optimistic lock state tracked in the UI keyed by requirement code.
export interface RequirementLockState {
  is_locked: boolean;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_reason?: string | null;
  artifact_id?: string;
}

// ============================================================================
// Payload → domain transforms.
//
// The backend wire payloads (`src/api/types.ts`) are flattened/denormalized
// (e.g. `version_number`, lock flags on requirements). These pure functions
// convert them into the domain shapes that `src/components/types.ts` defines
// and that components render. They replace the inline `(r: any) => ...` casts
// that used to live inside `useProjectState`.
// ============================================================================

import type {
  ArtifactLockState,
  AuditResult,
  ChatMessage,
  RequirementItem,
  RequirementLockState,
  StructuredRequirements,
  UserStory,
  VersionHistory,
} from '../components/types';
import type {
  AuditResultPayload,
  ClarificationQuestionPayload,
  GatheredRequirementsPayload,
  PrdVersionPayload,
  RequirementsField,
  RequirementPayload,
  RequirementStatePayload,
  UserStoryPayload,
} from './types';

export const DEFAULT_EPIC_FALLBACK = 'Structured Requirements Draft';

export const DEFAULT_PASSED_CHECKS = [
  'Financial Regulatory Compliance',
  'Security & Data Masking',
];

export const DEFAULT_FAILED_CHECKS = [
  'Idempotency & De-duplication',
  'Network Timeouts & Retry Strategies',
];

const normalizeClarificationText = (value: string | null | undefined) =>
  (value || '').toLocaleLowerCase().replace(/\s+/g, ' ').trim();

const clarificationFingerprint = (question: ClarificationQuestionPayload) =>
  `${normalizeClarificationText(question.checklist_category)}\u0000${normalizeClarificationText(question.question_text)}`;

/** CLARIFY 1.0.1 — Collapse identical user-visible questions from multiple audit
 * anchors before CLARIFY 1.1 renders them; preserve resolved evidence and metadata. */
export function dedupeClarificationQuestions(
  questions: ClarificationQuestionPayload[] | null | undefined,
): ClarificationQuestionPayload[] {
  if (!Array.isArray(questions)) return [];
  const unique = new Map<string, ClarificationQuestionPayload>();
  for (const question of questions) {
    const fingerprint = clarificationFingerprint(question);
    const existing = unique.get(fingerprint);
    if (!existing) {
      unique.set(fingerprint, { ...question });
      continue;
    }
    if (question.is_resolved && !existing.is_resolved) {
      existing.is_resolved = true;
      existing.user_answer = question.user_answer;
    }
    existing.target_requirement_id ||= question.target_requirement_id;
    existing.target_user_story_id ||= question.target_user_story_id;
    if (!existing.source_references?.length && question.source_references?.length) {
      existing.source_references = question.source_references;
    }
    existing.provenance ||= question.provenance;
    existing.requirement_context ||= question.requirement_context;
  }
  return [...unique.values()];
}

// ----------------------------------------------------------------------------
// User stories
// ----------------------------------------------------------------------------

export function toUserStory(s: UserStoryPayload): UserStory {
  return {
    ticket_code: s.ticket_code,
    story_title: s.story_title,
    as_a: s.as_a,
    i_want_to: s.i_want_to,
    so_that: s.so_that,
    acceptance_criteria: s.acceptance_criteria || [],
  };
}

export function toUserStories(stories: UserStoryPayload[] | null | undefined): UserStory[] {
  if (!Array.isArray(stories)) return [];
  return stories.map(toUserStory);
}

// ----------------------------------------------------------------------------
// Requirements
// ----------------------------------------------------------------------------

export function toRequirementItem(r: RequirementPayload): RequirementItem {
  return {
    id: r.id,
    requirement_code: r.requirement_code,
    title: r.title,
    functional_requirement: r.functional_requirement || '',
    description: r.description || '',
    user_stories: toUserStories(r.user_stories),
    status: r.status || 'active',
    is_locked: r.is_locked || false,
    locked_by: r.locked_by,
    locked_at: r.locked_at,
  };
}

/** Returns `undefined` when the payload carries no requirements array. */
export function toRequirementItems(requirements: RequirementsField): RequirementItem[] | undefined {
  if (!Array.isArray(requirements)) return undefined;
  return requirements.map(toRequirementItem);
}

export function epicNameFromRequirements(
  requirements: RequirementsField,
  fallback = DEFAULT_EPIC_FALLBACK
): string {
  if (Array.isArray(requirements)) {
    return requirements[0]?.title || fallback;
  }
  if (requirements && typeof requirements === 'object' && 'epic_name' in requirements) {
    const epic = (requirements as { epic_name?: string }).epic_name;
    if (epic) return epic;
  }
  return fallback;
}

// ----------------------------------------------------------------------------
/**
 * Normalizes the `structured_requirements` block returned by
 * `POST /api/process-requirements` (or its local-fallback variant) into the
 * domain shape. Falls back to the caller-supplied version when the payload
 * omits one.
 */
export function toStructuredRequirementsFromGathered(
  gathered: GatheredRequirementsPayload | null | undefined,
  fallbackVersion = 1
): StructuredRequirements {
  const version = typeof gathered?.version === 'number' ? gathered.version : fallbackVersion;
  return {
    epic_name: gathered?.epic_name || '',
    version,
    user_stories: toUserStories(gathered?.user_stories),
    requirements: toRequirementItems(gathered?.requirements),
  };
}

export function toAuditResult(payload: RequirementStatePayload): AuditResult {
  const isPassed = payload.validation_status === 'valid';
  return {
    is_valid: isPassed,
    audit_version_reviewed: payload.version_number || 1,
    clarification_questions: dedupeClarificationQuestions(payload.clarification_questions),
    passed_checks: payload.passed_checks || [],
    failed_checks: payload.failed_checks || [],
    findings: payload.audit_findings || [],
    source_references: payload.audit_source_references || [],
    verdict: payload.audit_verdict || (isPassed ? 'pass' : 'needs_clarification'),
    project_context: payload.audit_project_context,
    checklist_id: payload.audit_checklist_id,
    checklist_version: payload.audit_checklist_version,
  };
}

/** Normalizes a raw `audit_result` response block (may be `{}`). */
export function toAuditResultFromPayload(
  audit: AuditResultPayload | Record<string, unknown> | null | undefined
): AuditResult {
  const a = (audit || {}) as Partial<AuditResultPayload>;
  return {
    is_valid: Boolean(a.is_valid),
    audit_version_reviewed: typeof a.audit_version_reviewed === 'number' ? a.audit_version_reviewed : 1,
    passed_checks: Array.isArray(a.passed_checks) ? a.passed_checks : [],
    failed_checks: Array.isArray(a.failed_checks) ? a.failed_checks : [],
    findings: Array.isArray(a.findings) ? a.findings : [],
    source_references: Array.isArray(a.source_references) ? a.source_references : [],
    verdict: a.verdict || (a.is_valid ? 'pass' : 'fail'),
    project_context: a.project_context,
    checklist_id: a.checklist_id,
    checklist_version: a.checklist_version,
    clarification_questions: dedupeClarificationQuestions(a.clarification_questions),
  };
}

// ----------------------------------------------------------------------------
// Locks
// ----------------------------------------------------------------------------

export function toLockMaps(requirements: RequirementsField): {
  lockedArtifacts: Record<string, ArtifactLockState>;
  lockedRequirements: Record<string, RequirementLockState>;
} {
  const lockedArtifacts: Record<string, ArtifactLockState> = {};
  const lockedRequirements: Record<string, RequirementLockState> = {};

  if (Array.isArray(requirements)) {
    for (const req of requirements) {
      if (req.is_locked) {
        lockedArtifacts[req.requirement_code] = {
          is_locked: true,
          locked_by: req.locked_by,
          locked_at: req.locked_at,
        };
        lockedRequirements[req.requirement_code] = {
          is_locked: true,
          locked_by: req.locked_by,
          locked_at: req.locked_at,
          artifact_id: req.id,
        };
      }
    }
  }

  return { lockedArtifacts, lockedRequirements };
}

// ----------------------------------------------------------------------------
// Conversation history
// ----------------------------------------------------------------------------

/**
 * Merge a bounded server refresh into the timeline already visible for the
 * same project. Project-state responses intentionally contain only the newest
 * messages, so replacing state here would make older loaded messages vanish.
 */
export function mergeChatMessages(
  current: ChatMessage[],
  incoming: ChatMessage[],
): ChatMessage[] {
  const byId = new Map(incoming.map(message => [message.id, message]));
  const merged = current.map(message => {
    const refreshed = byId.get(message.id);
    if (!refreshed) return message;
    byId.delete(message.id);
    return {
      ...message,
      ...refreshed,
      auditResultSnapshot: refreshed.auditResultSnapshot ?? message.auditResultSnapshot,
    };
  });
  return [...merged, ...byId.values()];
}

export function toChatMessages(
  payload: RequirementStatePayload,
  attachLatestAuditSnapshot = true,
): ChatMessage[] {
  const rawHistory = payload.conversation_history;
  const loaded: ChatMessage[] = [];

  if (Array.isArray(rawHistory)) {
    const history = rawHistory;
    const latestClarificationIndex = history.reduce(
      (latest, message, index) =>
        message.intent === 'CLARIFICATION_REQUEST' ||
        message.role === 'auditor' ||
        message.intent === 'AUDIT_RESULT'
          ? index
          : latest,
      -1,
    );
    history.forEach((m, idx) => {
      const dateObj = m.created_at ? new Date(m.created_at) : null;
      const timeStr =
        dateObj && !isNaN(dateObj.getTime())
          ? dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : m.timestamp || '';

      const ownsClarifications = attachLatestAuditSnapshot && idx === latestClarificationIndex;
      const hasClarifications = (payload.clarification_questions?.length ?? 0) > 0;

      loaded.push({
        id: m.id || `hist-${idx}`,
        role: m.role || 'assistant',
        content: m.content || m.message || '',
        timestamp: timeStr,
        // Keep the latest audit card reviewable after submission/reload. The
        // renderer decides which questions are editable from is_resolved.
        isPendingClarifications: ownsClarifications && hasClarifications,
        auditResultSnapshot: ownsClarifications ? toAuditResult(payload) : undefined,
      });
    });
  }

  return loaded;
}
// Requirement state
// ----------------------------------------------------------------------------

export function toStructuredRequirements(
  payload: RequirementStatePayload,
  fallbackEpic = DEFAULT_EPIC_FALLBACK
): StructuredRequirements {
  return {
    epic_name: payload.epic_name || epicNameFromRequirements(payload.requirements, fallbackEpic),
    version: payload.version_number || 1,
    user_stories: toUserStories(payload.user_stories),
    requirements: toRequirementItems(payload.requirements),
  };
}

/**
 * Serializes the domain `StructuredRequirements` back into the wire shape the
 * `POST /api/process-requirements` endpoint expects.
 */
export function toGatheredRequirementsPayload(
  sr: StructuredRequirements
): GatheredRequirementsPayload {
  return {
    epic_name: sr.epic_name,
    version: sr.version,
    user_stories: sr.user_stories,
    requirements: sr.requirements,
  };
}

// ----------------------------------------------------------------------------
// PRD version ledger
// ----------------------------------------------------------------------------

// VERSION 3.1.1 — Map one unnumbered ledger DTO for the timeline. Missing SemVer gets a display
//                 fallback based on the numeric version (not a derived SemVer), and
//                 every non-`manual` origin is collapsed to the UI's `ai` category.
export function toVersionHistory(v: PrdVersionPayload): VersionHistory {
  return {
    version: v.version_number,
    semVersion: v.semver || `${v.version_number}.0.0`,
    timestamp: v.created_at || '',
    author: v.generated_by || 'automated_agent',
    description: v.change_summary ?? (v.change_type === 'manual'
      ? 'Manual edit recorded.'
      : 'Regenerated by the Architect agent.'),
    changeType: v.change_type === 'manual' ? 'manual' : 'ai',
    changedSections: v.changed_sections ?? undefined,
  };
}

export function toVersionHistoryList(versions: PrdVersionPayload[] | null | undefined): VersionHistory[] {
  if (!Array.isArray(versions)) return [];
  return versions
    .map(toVersionHistory)
    .sort((a, b) => b.version - a.version);
}

// useChat — the conversational / multi-agent handlers extracted from the former
// useProjectState god-hook. Owns `rawInput` and the agent actions
// (send, validate, generate PRD, clarifications). Domain state lives in the
// shared `RequirementStore`; project info and pending-actions are injected
// through `deps`.
import { useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import axios from 'axios';
import { api } from '../api/client';
import type { PendingActionPayload, ProcessRequirementsResponse, ProjectSummary } from '../api/types';
import {
  toAuditResultFromPayload,
  toGatheredRequirementsPayload,
  toStructuredRequirementsFromGathered,
} from '../api/transforms';
import type { VersionHistory } from '../components/types';
import { handleError } from '../components/Toast';
import type { RequirementStore } from './useRequirementStore';
import type { WorkspaceTab } from './useWorkspaceUi';

export interface ChatDeps {
  projectId: string | null;
  projects: ProjectSummary[];
  currentVersion: number;
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>;
  setActiveTab: (tab: WorkspaceTab) => void;
  loadProjectState: (projId: string, retries?: number, delay?: number) => Promise<void>;
  /**
   * Gate shared with the ChatPanel action buttons: false while the project has
   * neither knowledge documents nor gathered requirements, so the agent
   * handlers refuse to run on an empty project (defense-in-depth parity with
   * the disabled Validate / Generate PRD buttons).
   */
  canRunAgentActions: boolean;
}

export interface UseChatResult {
  messages: RequirementStore['messages'];
  setMessages: RequirementStore['setMessages'];
  rawInput: string;
  setRawInput: Dispatch<SetStateAction<string>>;
  isProcessing: boolean;
  isLoading: boolean;
  currentAgentNode: string | null;
  syncStatus: string;
  chatEndRef: RequirementStore['chatEndRef'];
  clarificationAnswers: Record<string, string>;
  handleSendMessage: (textToSend?: string, projectIdOverride?: string) => Promise<void>;
  handleValidateRequirements: () => Promise<void>;
  handleGeneratePRD: () => Promise<void>;
  /** Aborts the in-flight agent request (Stop button). No-op when idle. */
  handleStopGeneration: () => void;
  handleSubmitClarifications: (e: React.FormEvent) => Promise<void>;
  handleUpdateAnswerValue: (key: string, value: string) => void;
}

const nowTime = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

const activeJobKey = (projectId: string, jobType: 'auditor' | 'architect') =>
  `agenticdia:active-job:${projectId}:${jobType}`;

const questionAnswerKey = (question: { id?: string }, index: number) =>
  question.id || `q-${index}`;

const waitForPoll = (signal: AbortSignal, delayMs = 1000): Promise<void> =>
  new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Generation cancelled', 'AbortError'));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener('abort', onAbort, { once: true });
  });

// --- Rate-limit (HTTP 429) awareness ---------------------------------------
// A 429 from the backend is a transient "slow down" signal (Finding #39), NOT a
// backend outage. These helpers let callers distinguish the two so they surface
// a "please wait" notice instead of the automatic local-sandbox fallback.

const isRateLimitError = (err: unknown): boolean => {
  if (!err || typeof err !== 'object') return false;
  return (err as { response?: { status?: number } }).response?.status === 429;
};

const extractRetryAfter = (err: unknown): number | null => {
  if (!err || typeof err !== 'object') return null;
  const headers = (err as { response?: { headers?: Record<string, unknown> & { get?: (k: string) => unknown } } }).response?.headers;
  const raw =
    headers?.['retry-after'] ??
    headers?.['Retry-After'] ??
    (typeof headers?.get === 'function' ? headers.get('retry-after') : undefined);
  if (raw === undefined || raw === null) return null;
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : null;
};

const rateLimitedContent = (err: unknown): string => {
  const wait = extractRetryAfter(err);
  const suffix = wait === null
    ? 'A short time window will restore access.'
    : `Please wait about ${wait} second(s) before continuing.`;
  return `Rate limit reached. Too many requests in a short window. ${suffix}`;
};

// --- Payload-too-large (HTTP 413) awareness ---------------------------------
// A 413 from the backend means the request payload — the project's full
// structured requirements, user stories and acceptance criteria serialized to
// JSON — exceeded MAX_CONTEXT_TOKENS before any agent ran (Finding #39 guard).
// It is a *content size* problem, not a crash: nothing was saved or changed.
// These helpers surface an actionable notice instead of the generic
// "generation failed" message.
const isPayloadTooLargeError = (err: unknown): boolean => {
  if (!err || typeof err !== 'object') return false;
  return (err as { response?: { status?: number } }).response?.status === 413;
};

const isUnauthorizedError = (err: unknown): boolean => {
  if (!err || typeof err !== 'object') return false;
  return (err as { response?: { status?: number } }).response?.status === 401;
};

const extractErrorDetail = (err: unknown): string | null => {
  if (!err || typeof err !== 'object') return null;
  const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  return typeof detail === 'string' ? detail : null;
};

const payloadTooLargeContent = (err: unknown): string => {
  const measured = extractErrorDetail(err);
  return [
    'Project too large for the current context budget.',
    'The project\'s requirements, user stories and acceptance criteria exceed the LLM context budget (`MAX_CONTEXT_TOKENS`). The request was rejected before any agent ran. Nothing was saved or changed.',
    '',
    'How to resolve:',
    '- Trim the project: remove or merge redundant user stories or acceptance criteria, or',
    '- Raise `MAX_CONTEXT_TOKENS` in `backend/.env` (e.g. `16384`) if the loaded LM Studio model supports a larger context window.',
    measured ? `\n${measured}` : '',
  ].filter(Boolean).join('\n');
};

// --- Stop-button (request abort) awareness ----------------------------------
// When the user presses Stop we cancel the axios request. Axios then rejects
// with a CanceledError — that is an intentional user action, NOT a failure, so
// every handler surfaces a neutral "stopped" notice instead of an error toast.
const isAbortError = (err: unknown): boolean =>
  axios.isCancel(err) || (err as { code?: string; name?: string })?.code === 'ERR_CANCELED'
  || (err as { name?: string })?.name === 'AbortError';

export function useChat(store: RequirementStore, deps: ChatDeps): UseChatResult {
  const [rawInput, setRawInput] = useState<string>('');

  // Abort controller for whichever agent request is currently in flight.
  // The Stop button aborts it; each handler creates a fresh controller per run.
  const abortRef = useRef<AbortController | null>(null);
  const backgroundJobRef = useRef<string | null>(null);

  // (Chat auto-scroll moved into ChatPanel, which owns the scroll container:
  // it only follows the timeline while the user is near the bottom and shows a
  // "jump to latest" pill otherwise.)

  // Build a truthful change-history digest for the backend prompt context.
  // Every agent call used to hardcode 'No previous history.', which defeated
  // the Architect node's incremental-regeneration logic (it could never see
  // what had already been approved). Returning undefined lets the backend's
  // documented default ("No previous history.") apply for genuinely new
  // projects instead of duplicating that string here.
  const buildVersionHistorySummaries = (): string | undefined => {
    const history = store.versionHistory || [];
    if (history.length === 0) return undefined;
    return history
      .map(h => `- v${h.semVersion} (${h.timestamp}, ${h.author}): ${h.description}`)
      .join('\n');
  };

  // Handle Raw Conversational Input Submission
  // CHAT 1.2 — Frontend chat entry: one handler serves the composer, the send
  // button and the empty-state suggestion chips (CHAT 1.1). Layer chain:
  //   1.2 → 1.3 (api.processRequirements) → route 2.1 → intent 2.2 →
  //   GENERAL_CHAT branch 2.3 → 1.4 (UI branch on the returned intent).
  // Local guards: no selected project → inline warning bubble (no request);
  // blank input → no-op. The optimistic user bubble is appended BEFORE the
  // request, and the AbortController is what the Stop button cancels.
  const handleSendMessage = async (textToSend?: string, projectIdOverride?: string) => {
    const targetProjectId = projectIdOverride ?? deps.projectId;
    if (!targetProjectId) {
      store.setMessages(prev => [...prev, {
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: 'No project selected. Please select or create a project before interacting.',
        timestamp: nowTime(),
      }]);
      return;
    }
    const projectId = targetProjectId;
    const inputMsg = textToSend || rawInput;
    if (!inputMsg.trim()) return;

    // Clear main input if sent from main input box
    if (!textToSend) setRawInput('');

    // Append User message to UI
    store.setMessages(prev => [...prev, {
      id: `user-${Date.now()}`,
      role: 'user',
      content: inputMsg,
      timestamp: nowTime(),
    }]);

    // Set Loading State - show neutral loading indicator until intent is determined
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus('Detecting intent...');
    store.setCurrentAgentNode(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      // The backend handles intent detection internally and routes accordingly:
      //   - GENERAL_CHAT: returns conversational response directly (no agent workflow)
      //   - REQUIREMENT_REQUEST: proceeds to Gatherer workflow
      // CHAT 1.3 — API boundary: POST /api/process-requirements, the SAME endpoint
      //            the agent flows use. Intent detection server-side decides whether
      //            this remains a conversation (CHAT 2.3) or enters the agent
      //            workflow (GATHERING / AUDITOR / ARCHITECT flows).
      const data = await api.processRequirements({
        project_id: projectId,
        raw_input: inputMsg,
        // An intake is dispatched just after project creation, before the
        // hook closure has necessarily observed the newly selected project.
        // It must never carry the prior project's version or requirements.
        current_version: projectIdOverride ? 1 : deps.currentVersion,
        // CHAT 1.3 — DISCREPANCY (documented, unchanged): the chat path hardcodes
        // this string even though buildVersionHistorySummaries() above exists and
        // the agent actions use it; the backend's default is the same text.
        version_history_summaries: 'No previous history.',
        target_agent: 'gatherer',
        ...(projectIdOverride
          ? {}
          : { structured_requirements: toGatheredRequirementsPayload(store.structuredRequirements) }),
      }, controller.signal);

      // CHAT 1.4 — Response branch. GENERAL_CHAT (below) is the pure-conversation
      //            outcome: the assistant text is appended and NO requirement,
      //            story, version or PRD state is modified. Every other intent
      //            falls into the agent branches instead.
      const detectedIntent = data.detected_intent || 'GENERAL_CHAT';

      if (detectedIntent === 'GENERAL_CHAT') {
        // Do NOT modify requirements, user stories, version history, or any project artifacts
        store.setSyncStatus('Synced with Supabase Cloud');
        store.setCurrentAgentNode(null);
        const chatResponse = data.message || 'I understood your message. How can I help you further?';
        store.setMessages(prev => [...prev, {
          id: `general-chat-${Date.now()}`,
          role: 'assistant',
          content: chatResponse,
          timestamp: nowTime(),
        }]);
      } else {
        store.setSyncStatus('Gathering specifications...');
        store.setCurrentAgentNode('gatherer_node');

        const receivedReqs = data.structured_requirements || null;
        const pendingActionId = data.pending_action_id;
        const isPendingMerge = data.pending_merge === true;

        // GATHERING 8.1 — Frontend side of the handoff (UI layer): the staged merge is
        //            pushed into `pendingActions`, which renders the ConfirmationPanel
        //            ("Merge Preview Ready" bubble below). Nothing is written until the
        //            user confirms (CONFIRMATION flow) — Cancel discards the action,
        //            which is why the state above is reported as "unconfirmed".
        if (isPendingMerge && pendingActionId) {
          // Store the pending merge for confirmation
          deps.setPendingActions(prev => [...prev, {
            id: pendingActionId,
            project_id: projectId,
            action_type: 'MERGE',
            original_user_message: inputMsg,
            proposed_changes: (receivedReqs ?? undefined) as Record<string, unknown> | undefined,
          }]);

          store.setMessages(prev => [...prev, {
            id: `merge-preview-${Date.now()}`,
            role: 'assistant',
            content: `Merge preview ready.\nI have analyzed your input and prepared the merged requirements. Please review the changes below and select Save or Cancel.\n\n"${inputMsg}"`,
            timestamp: nowTime(),
          }]);
        } else if (receivedReqs && receivedReqs.epic_name) {
          // Fallback: if no pending merge (e.g. direct update), apply immediately
          const nextVer = deps.currentVersion + 1;
          const nextStructured = toStructuredRequirementsFromGathered(receivedReqs, nextVer);
          store.setStructuredRequirements(nextStructured);
          store.setCurrentVersion(nextVer);

          const newHist: VersionHistory = {
            version: nextVer,
            timestamp: new Date().toISOString().replace('T', ' ').substring(0, 16),
            author: 'Product Owner',
            description: inputMsg.substring(0, 70) + (inputMsg.length > 70 ? '...' : ''),
            requirementsSnapshot: nextStructured,
          };
          store.setVersionHistory(prev => [newHist, ...prev]);
          store.setSyncStatus(`State updated to Version ${nextVer}.0`);
        }

        if (!isPendingMerge) {
          store.setMessages(prev => [...prev, {
            id: `gatherer-passed-${Date.now()}`,
            role: 'assistant',
            content: 'Requirements gathered and updated.\nI have structured your input into the Agile Requirements board.\n\nTo run compliance validation on these updated specifications, select Validate Requirements. Or select Generate PRD to build the technical documentation.',
            timestamp: nowTime(),
          }]);
        }
      }
    } catch (err) {
      if (isAbortError(err)) {
        store.setMessages(prev => [...prev, {
          id: `stopped-${Date.now()}`,
          role: 'assistant',
          content: 'Stopped.\nYou cancelled this request. The server-side run was terminated and no requirement changes were applied.',
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Request stopped by user.');
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages(prev => [...prev, {
          id: `rate-limited-${Date.now()}`,
          role: 'assistant',
          content: rateLimitedContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Rate limited. Please wait before continuing.');
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages(prev => [...prev, {
          id: `payload-too-large-${Date.now()}`,
          role: 'assistant',
          content: payloadTooLargeContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Request rejected: project exceeds the LLM context budget. Nothing was saved.');
        return;
      }
      handleError('The requirement engine could not complete your request.', err);
      const errorContent = `Request failed. Nothing was saved.\n\nThe requirement engine could not complete your message (${(err as Error)?.message || err}).\n\nYour input was not recorded as a user story, and no requirement state was changed.`;
      store.setMessages(prev => [...prev, {
        id: `api-error-${Date.now()}`,
        role: 'assistant',
        content: errorContent,
        timestamp: nowTime(),
      }]);
      store.setSyncStatus('Request failed. Nothing was saved.');
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      store.setIsLoading(false);
      store.setIsProcessing(false);
      store.setCurrentAgentNode(null);
    }
  };

  // Handle On-Demand Audit Execution
  // AUDIT 5.0 — Frontend entry of the AUDITOR flow (ChatPanel's "Validate
  //             Requirements" button). Layer chain: 5.0 → 5.1 (on-demand agent request)
  //             → AUDIT 1.0-2.x → 5.2/5.3/5.4 (UI verdict) → answers via AUDIT 3.x or
  //             the FLOW 10 clarification endpoint.
  //             Guarded by canRunAgentActions so an empty project cannot be audited.
  const handleValidateRequirements = async () => {
    if (!deps.projectId) return;
    if (!deps.canRunAgentActions) {
      // Nothing to audit yet — mirror the disabled button instead of running
      // the auditor agent against an empty project.
      store.setSyncStatus('Nothing to validate yet. Add requirements or upload a knowledge document.');
      return;
    }
    // prepare the UI for the audit run / clear any prior audit state
    const projectId = deps.projectId;
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus('Running Technical Audit...');
    store.setCurrentAgentNode('auditor_node');

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const request = {
        project_id: projectId,
        raw_input: '',
        // AUDIT 5.1 — The on-demand trigger: empty raw_input + target_agent="auditor"
        //            is exactly why the pipeline must bypass the GENERAL_CHAT
        //            short-circuit (see CHAT 2.3.0). On the backend this enters
        //            ROUTING 1.2 → auditor_node.
        target_agent: 'auditor',
        structured_requirements: toGatheredRequirementsPayload(store.structuredRequirements),
        current_version: deps.currentVersion,
        version_history_summaries: 'No previous history.',
      };
      const storageKey = activeJobKey(projectId, 'auditor');
      const savedJobId = window.localStorage.getItem(storageKey);
      let job;
      if (savedJobId) {
        try {
          job = await api.getGenerationJob<ProcessRequirementsResponse>(projectId, savedJobId);
        } catch (resumeError) {
          if (isUnauthorizedError(resumeError)) throw resumeError;
          window.localStorage.removeItem(storageKey);
          job = await api.startAgentJob(request);
        }
      } else {
        job = await api.startAgentJob(request);
      }
      window.localStorage.setItem(storageKey, job.id);
      backgroundJobRef.current = job.id;
      let pollDelayMs = 1000;
      while (!['completed', 'failed', 'cancelled'].includes(job.status)) {
        await waitForPoll(controller.signal, pollDelayMs);
        job = await api.getGenerationJob<ProcessRequirementsResponse>(projectId, job.id);
        store.setSyncStatus(`Running Technical Audit… ${job.progress_stage.replaceAll('_', ' ')}`);
        pollDelayMs = Math.min(5000, pollDelayMs + 500);
      }
      window.localStorage.removeItem(storageKey);
      if (job.status === 'cancelled') throw new DOMException('Generation cancelled', 'AbortError');
      if (job.status === 'failed' || !job.result) {
        throw new Error(job.error_message || 'Background audit failed.');
      }
      const data = job.result;

      // AUDIT 5.2 — Verdict mapping (api/transforms): audit_result → UI model
      //            (is_valid, passed/failed checks, clarification questions).
      //            Audits are also staged as a pending merge (GATHERING 8.1 pattern),
      //            so nothing is persisted until the user saves.
      const receivedAudit = toAuditResultFromPayload(data.audit_result);
      const pendingActionId = data.pending_action_id;
      const isPendingMerge = data.pending_merge === true;
      const isStaleResult = data.stale_result === true;

      // Handle pending merge for audit results (if applicable)
      if (isPendingMerge && pendingActionId) {
        deps.setPendingActions(prev => [...prev, {
          id: pendingActionId,
          project_id: projectId,
          action_type: 'MERGE',
          original_user_message: 'Audit validation',
          proposed_changes: (data.audit_result ?? {}) as Record<string, unknown>,
        }]);
      }

      const verdict = receivedAudit.verdict || (receivedAudit.is_valid ? 'pass' : 'fail');
      const questions = receivedAudit.clarification_questions || [];
      const questionTexts = questions.map(q => `- ${q.question_text}`).join('\n');
      const findingTexts = (receivedAudit.findings || []).map(finding => {
        const sources = finding.source_references
          .map(source => `${source.document_name}${source.section ? `: ${source.section}` : ''}`)
          .join(', ');
        const recommendation = finding.recommendation?.summary
          ? `\n  Suggested improvement: ${finding.recommendation.summary}`
          : '';
        return `- ${finding.impact}, ${finding.category} (${finding.rule_id || 'ADHOC'}): ${finding.description}${sources ? ` (Source: ${sources})` : ''}${recommendation}`;
      }).join('\n');
      const context = receivedAudit.project_context;
      const contextText = context
        ? `Inferred context: ${context.business_segment}, ${context.product_domain}, ${context.solution_type}, ${context.delivery_stage} (${Math.round(context.confidence * 100)}% confidence)`
        : '';
      const auditCouldNotComplete = receivedAudit.failed_checks.includes('AUDIT_PARSE_ERROR');
      if (isStaleResult) {
        store.setMessages(prev => [...prev, {
          id: `audit-stale-${Date.now()}`,
          role: 'assistant',
          content: `The audit completed, but requirements changed while it was running. This result is read-only and was not applied.\n${contextText}\n\nFindings from the older version:\n${findingTexts || 'No findings were returned.'}\n\nRun Validate Requirements again for the latest version.`,
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Audit completed on an older version. Run Validate again.');
        return;
      }
      // AUDIT 5.3 — Failure branch: the assistant bubble carries the questions and
      //            `isPendingClarifications` + `auditResultSnapshot`, which is what
      //            renders the embedded clarification form (AUDIT 5.3.1). The sync
      //            status tells the user the audit is pending, not failed hard.
      if (verdict === 'fail' || verdict === 'needs_clarification') {
        const title = auditCouldNotComplete
          ? 'Requirements audit could not complete'
          : verdict === 'fail' ? 'Requirements audit failed' : 'Requirements audit needs clarification';
        const warningContent = auditCouldNotComplete
          ? `${title}\n\n${findingTexts || 'Please run Validate again.'}`
          : `${title}\n${contextText}\n\nFindings:\n${findingTexts || 'No blocking finding was established.'}\n\nPending clarifications:\n${questionTexts || 'None specified'}`;

        store.setMessages(prev => [...prev, {
          id: `audit-failed-${Date.now()}`,
          role: 'assistant',
          content: warningContent,
          timestamp: nowTime(),
          isPendingClarifications: questions.length > 0,
          auditResultSnapshot: receivedAudit,
        }]);

        store.setSyncStatus(auditCouldNotComplete
          ? 'Audit could not complete. Please retry.'
          : verdict === 'fail' ? 'Audit failed: mandatory blockers found.' : 'Audit needs clarification.');
      } else {
        // AUDIT 5.4 — Success branch: a positive verdict bubble (no clarification
        //            form). The audit is still only STAGED — the user's Save in the
        //            ConfirmationPanel is what persists the verdict (CONFIRM 3.3).
        const successContent = `${verdict === 'pass_with_warnings' ? 'Requirements audit passed with warnings.' : 'Requirements audit passed.'}\n${contextText}\n\n${findingTexts || 'All clearly applicable mandatory controls are covered.'}`;
        store.setMessages(prev => [...prev, {
          id: `audit-passed-${Date.now()}`,
          role: 'assistant',
          content: successContent,
          timestamp: nowTime(),
        }]);

        store.setSyncStatus(verdict === 'pass_with_warnings' ? 'Passed with non-blocking warnings.' : 'Audit passed.');
      }

      store.setAuditResult(receivedAudit);

      if (receivedAudit.clarification_questions) {
        const initialAnswers: Record<string, string> = {};
        receivedAudit.clarification_questions.forEach((q, idx) => {
          initialAnswers[questionAnswerKey(q, idx)] = '';
        });
        store.setClarificationAnswers(initialAnswers);
      }
    } catch (err) {
      if (isAbortError(err)) {
        store.setMessages(prev => [...prev, {
          id: `stopped-${Date.now()}`,
          role: 'assistant',
          content: 'Stopped.\nYou cancelled this request. The audit was terminated and no validation verdict was applied.',
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Validation stopped by user.');
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages(prev => [...prev, {
          id: `rate-limited-${Date.now()}`,
          role: 'assistant',
          content: rateLimitedContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Rate limited. Please wait before continuing.');
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages(prev => [...prev, {
          id: `payload-too-large-${Date.now()}`,
          role: 'assistant',
          content: payloadTooLargeContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Audit skipped: project exceeds the LLM context budget.');
        return;
      }
      if (isUnauthorizedError(err)) {
        store.setMessages(prev => [...prev, {
          id: `audit-auth-expired-${Date.now()}`,
          role: 'assistant',
          content: 'The audit job is still stored, but your sign-in session expired while checking its result. Sign in again, then click Validate Requirements to resume this same job.',
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Sign-in expired. Please sign in again.');
        return;
      }
      const detail = extractErrorDetail(err);
      handleError(detail ?? 'Compliance audit did not complete.', err);
      store.setMessages(prev => [...prev, {
        id: `audit-error-${Date.now()}`,
        role: 'assistant',
        content: `Audit failed. No validation was saved.\n\n${detail ?? `The audit could not complete against the requirement engine (${(err as Error)?.message || err}).`}\n\nNo validation verdict was applied and no requirements were changed.`,
        timestamp: nowTime(),
      }]);
      store.setSyncStatus('Audit failed. No changes were saved.');
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      backgroundJobRef.current = null;
      store.setIsLoading(false);
      store.setIsProcessing(false);
      store.setCurrentAgentNode(null);
    }
  };

  // Handle On-Demand PRD & Architecture Diagram Generation
  // ARCHITECT 1.0 — Frontend entry of the PRD-generation flow (ChatPanel's "Generate
  //             PRD" button). Chain: 1.0 → 1.2 (on-demand architect request) →
  //             ARCHITECT 2.x-8.x → 1.5 (store + PRD tab) / 1.6 (abort-error branch).
  //             Guarded by canRunAgentActions so an empty project cannot be compiled.
  const handleGeneratePRD = async () => {
    if (!deps.projectId) return;
    if (!deps.canRunAgentActions) {
      // Nothing to compile yet — mirror the disabled button instead of asking
      // the architect agent to build a PRD from an empty project.
      store.setSyncStatus('Nothing to compile yet. Add requirements or upload a knowledge document.');
      return;
    }
    const projectId = deps.projectId;
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus('Compiling enterprise PRD document...');
    store.setCurrentAgentNode('architect_node');

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const request = {
        project_id: projectId,
        raw_input: '',
        // ARCHITECT 1.2 — The on-demand trigger: empty raw_input + target_agent
        //            "architect" is exactly why the pipeline bypasses the GENERAL_CHAT
        //            short-circuit (CHAT 2.3.0); on the backend it enters ROUTING 1.2 →
        //            architect_node. `version_history_summaries` is the real digest
        //            (1.3 helper), which is what makes the reuse guard (2.2) and the
        //            version-history table in the template meaningful.
        target_agent: 'architect',
        structured_requirements: toGatheredRequirementsPayload(store.structuredRequirements),
        current_version: deps.currentVersion,
        version_history_summaries: buildVersionHistorySummaries(),
      };
      const storageKey = activeJobKey(projectId, 'architect');
      const savedJobId = window.localStorage.getItem(storageKey);
      let job;
      if (savedJobId) {
        try {
          job = await api.getGenerationJob<ProcessRequirementsResponse>(projectId, savedJobId);
        } catch (resumeError) {
          if (isUnauthorizedError(resumeError)) throw resumeError;
          window.localStorage.removeItem(storageKey);
          job = await api.startAgentJob(request);
        }
      } else {
        job = await api.startAgentJob(request);
      }
      window.localStorage.setItem(storageKey, job.id);
      backgroundJobRef.current = job.id;
      let pollDelayMs = 1000;
      while (!['completed', 'failed', 'cancelled'].includes(job.status)) {
        await waitForPoll(controller.signal, pollDelayMs);
        job = await api.getGenerationJob<ProcessRequirementsResponse>(projectId, job.id);
        store.setSyncStatus(`Compiling enterprise PRD document… ${job.progress_stage.replaceAll('_', ' ')}`);
        pollDelayMs = Math.min(5000, pollDelayMs + 500);
      }
      window.localStorage.removeItem(storageKey);
      if (job.status === 'cancelled') throw new DOMException('Generation cancelled', 'AbortError');
      if (job.status === 'failed' || !job.result) {
        throw new Error(job.error_message || 'Background PRD generation failed.');
      }
      const data = job.result;

      // ARCHITECT 1.5 — Response applied to UI state: the filled PRD markdown and the
      //            refreshed Mermaid diagram replace the stored ones, a success bubble
      //            is appended, and the workspace switches to the PRD tab. The
      //            requirement rows are still unconfirmed (CONFIRMATION flow) — only the
      //            document/version writes already happened server-side (5.1/6.0).
      const generatedPrd = data.prd_markdown || '';
      const generatedMermaid = data.mermaid_diagram || '';

      if (generatedPrd) store.setPrdMarkdown(generatedPrd);
      if (generatedMermaid) store.setMermaidDiagram(generatedMermaid);
      if (data.structured_requirements?.version) {
        store.setCurrentVersion(data.structured_requirements.version);
      }
      // Architect generation now appends the immutable snapshot immediately;
      // reload so the first AI-generated PRD appears in Versions without a
      // project refresh or an unrelated merge confirmation.
      await store.loadVersionHistory(projectId);

      store.setMessages(prev => [...prev, {
        id: `prd-generated-${Date.now()}`,
        role: 'assistant',
        content: 'Enterprise PRD compiled successfully.\nThe Technical Product Owner Assistant has generated the formal PRD and interactive system flowchart in the preview panel.',
        timestamp: nowTime(),
      }]);

      store.setSyncStatus('PRD and flowchart diagram updated.');
      deps.setActiveTab('prd'); // switch tab automatically to PRD
    } catch (err) {
      // ARCHITECT 1.6 — Abort branch (Stop pressed): the generation was terminated
      //            server-side by the CANCELLATION flow, so no document was produced.
      if (isAbortError(err)) {
        store.setMessages(prev => [...prev, {
          id: `stopped-${Date.now()}`,
          role: 'assistant',
          content: 'Stopped.\nYou cancelled this request. PRD generation was terminated server-side; no document was produced.',
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Generation stopped by user.');
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages(prev => [...prev, {
          id: `rate-limited-${Date.now()}`,
          role: 'assistant',
          content: rateLimitedContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Rate limited. Please wait before continuing.');
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages(prev => [...prev, {
          id: `payload-too-large-${Date.now()}`,
          role: 'assistant',
          content: payloadTooLargeContent(err),
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('PRD generation blocked: project exceeds the LLM context budget.');
        return;
      }
      if (isUnauthorizedError(err)) {
        store.setMessages(prev => [...prev, {
          id: `prd-auth-expired-${Date.now()}`,
          role: 'assistant',
          content: 'The PRD job is still stored, but your sign-in session expired while checking it. Sign in again and click Generate PRD to resume the same job.',
          timestamp: nowTime(),
        }]);
        store.setSyncStatus('Sign-in expired. The PRD job can be resumed after signing in.');
        return;
      }
      handleError('PRD generation did not complete.', err);
      store.setMessages(prev => [...prev, {
        id: `prd-error-${Date.now()}`,
        role: 'assistant',
        content: `PRD generation failed. No document was saved.\n\nThe document could not be generated (${(err as Error)?.message || err}).\n\nNo PRD or diagram was produced.`,
        timestamp: nowTime(),
      }]);
      store.setSyncStatus('PRD generation failed. No document saved.');
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      backgroundJobRef.current = null;
      store.setIsLoading(false);
      store.setIsProcessing(false);
      store.setCurrentAgentNode(null);
    }
  };

  // Stop button — terminates generation on BOTH sides: asks the backend to
  // hard-cancel the running workflow task (best-effort), then aborts the local
  // axios request so the UI unlocks immediately instead of waiting for the LLM
  // workflow to finish.
  const handleStopGeneration = () => {
    const controller = abortRef.current;
    if (!controller) return;
    const projectId = deps.projectId;
    if (projectId) {
      const jobId = backgroundJobRef.current;
      const cancellation = jobId
        ? api.cancelGenerationJob(projectId, jobId)
        : api.cancelProcessRequirements(projectId);
      void cancellation.catch(() => {
        // Best-effort only: if this loses a race with completion (or fails),
        // the local abort below still unblocks the UI instantly.
      });
    }
    controller.abort();
    abortRef.current = null;
    backgroundJobRef.current = null;
    store.setSyncStatus('Stopping generation...');
  };

  // (Removed) The former `simulateAgentWorkflowFallback` fabricated a fake user
  // story + version bump locally whenever any backend call failed. It silently
  // "saved" data the backend never saw and drove the misleading "(Local
  // Fallback)" messages. All failure paths now surface a truthful error instead.

  // Submit Answer to Clarifications Form
  // CLARIFY 1.3 — Frontend submit handler for BOTH clarification forms (1.1 / 1.2).
  //             Chain: 1.3 → 1.5 (api.submitClarifications) → CLARIFY 3.x → the saved
  //             state back here, where the audit view is refreshed:
  //               • answers cleared, current agent node set from current_workflow_state
  //               • auditResult.is_valid + clarification_questions updated
  //               • full project state re-loaded (1.3 → PROJECT 2.x)
  //             The empty-answer guard makes the button a no-op until something is typed.
  const handleSubmitClarifications = async (e: React.FormEvent) => {
    e.preventDefault();
    const submittedAnswers = Object.fromEntries(
      Object.entries(store.clarificationAnswers)
        .map(([key, value]) => [key, value.trim()])
        .filter(([, value]) => value.length > 0)
    );
    if (Object.keys(submittedAnswers).length === 0) {
      store.setSyncStatus('Enter at least one answer before saving.');
      return;
    }

    try {
      store.setSyncStatus('Submitting clarifications...');
      // CLARIFY 1.5 — API boundary: POST /api/clarification/submit (CLARIFY 2.1 /
      //             3.0) carrying the position-keyed answers (1.4). A failure leaves
      //             the typed answers in place so the user can retry.
      const res = await api.submitClarifications(deps.projectId ?? '', submittedAnswers);
      if (res) {
        const refreshedQuestions = res.clarification_questions || [];
        const unresolvedQuestions = refreshedQuestions.filter(question => !question.is_resolved);
        const remainingAnswers: Record<string, string> = {};
        unresolvedQuestions.forEach((question, index) => {
          remainingAnswers[questionAnswerKey(question, index)] = '';
        });
        store.setClarificationAnswers(remainingAnswers);
        // Update the inline chat form in place: answered questions disappear,
        // while unanswered questions remain available for a later submission.
        store.setMessages(prev => prev.map(message =>
          message.isPendingClarifications
            ? {
                ...message,
                isPendingClarifications: unresolvedQuestions.length > 0,
                auditResultSnapshot: message.auditResultSnapshot
                  ? { ...message.auditResultSnapshot, clarification_questions: refreshedQuestions }
                  : message.auditResultSnapshot,
              }
            : message
        ));
        if (res.current_workflow_state) {
          store.setCurrentAgentNode(res.current_workflow_state);
        }
        if (res.clarification_questions) {
          store.setAuditResult(prev => ({
            ...prev,
            is_valid: res.validation_status === 'valid',
            clarification_questions: res.clarification_questions,
          }));
        }
        if (deps.projectId) await deps.loadProjectState(deps.projectId);
        if (unresolvedQuestions.length > 0) {
          store.setSyncStatus(`${unresolvedQuestions.length} clarification question(s) still unanswered.`);
        } else {
          store.setSyncStatus('Clarifications saved. Re-running the technical audit...');
          await handleValidateRequirements();
        }
      }
    } catch (err) {
      handleError('Failed to submit clarifications.', err);
      store.setSyncStatus('Failed to submit clarifications');
    }
  };

  // Stable persisted question IDs are used when available. The q-<index>
  // fallback preserves compatibility for newly staged, not-yet-persisted audits.
  const handleUpdateAnswerValue = (key: string, value: string) => {
    store.setClarificationAnswers(prev => ({
      ...prev,
      [key]: value,
    }));
  };

  return {
    messages: store.messages,
    setMessages: store.setMessages,
    rawInput,
    setRawInput,
    isProcessing: store.isProcessing,
    isLoading: store.isLoading,
    currentAgentNode: store.currentAgentNode,
    syncStatus: store.syncStatus,
    chatEndRef: store.chatEndRef,
    clarificationAnswers: store.clarificationAnswers,
    handleSendMessage,
    handleValidateRequirements,
    handleGeneratePRD,
    handleStopGeneration,
    handleSubmitClarifications,
    handleUpdateAnswerValue,
  };
}

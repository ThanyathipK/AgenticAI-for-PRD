// ChatPanel — left conversational panel extracted from the former Dashboard.tsx.
// Renders the message timeline (with embedded human-in-the-loop clarification
// forms), the agent execution ticker, and the fixed input + action bar.
import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import {
  Bot,
  Send,
  RefreshCw,
  Paperclip,
  AlertTriangle,
  CheckCircle2,
  FileText,
  Loader2,
  Square,
  ArrowDown,
} from 'lucide-react';
import { ProjectState } from '../hooks/useProjectState';
import { Tooltip } from './Tooltip';
import { highlightMatch } from '../utils/highlight';
import { displayUserName } from '../utils/actorName';
import { renderInlineFormatting } from './MarkdownRenderer';
import type { AuditFindingPayload, AuditSourceReferencePayload } from '../api/types';
import type { AuditResult, ClarificationQuestion, RequirementItem } from './types';
import { api } from '../api/client';
import { toChatMessages } from '../api/transforms';

const CHAT_INPUT_MAX_LENGTH = 4000;
const OLDER_MESSAGE_PAGE_SIZE = 10;

const sourceNames = (
  references: AuditSourceReferencePayload[] | undefined,
  provenanceDocument?: string | null,
): string[] => Array.from(new Set([
  ...(references || []).map(reference => reference.document_name?.trim()).filter(Boolean),
  provenanceDocument?.trim(),
].filter((name): name is string => Boolean(name))));

const sourceLabels = (
  references: AuditSourceReferencePayload[] | undefined,
  provenanceDocument?: string | null,
  provenanceSection?: string | null,
): string[] => {
  const labels = (references || []).map(reference =>
    [reference.document_name?.trim(), reference.section?.trim()].filter(Boolean).join(' · '),
  );
  if (provenanceDocument?.trim()) {
    labels.push([provenanceDocument.trim(), provenanceSection?.trim()].filter(Boolean).join(' · '));
  }
  return Array.from(new Set(labels.filter(Boolean)));
};

const readableCode = (value: string): string => value
  .replace(/[_-]+/g, ' ')
  .replace(/\b\w/g, character => character.toUpperCase());

const readableAuditRule = (value: string): string => readableCode(
  value.replace(/(?:[_-]|\s)\d+$/i, ''),
);

const contextRecord = (value: string | null | undefined): Record<string, unknown> | null => {
  const context = value?.trim();
  if (!context?.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(context) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
};

const readableContext = (value: string | null | undefined): string | null => {
  const context = value?.trim();
  if (!context) return null;
  if (!context.startsWith('{') && !context.startsWith('[')) return context;
  try {
    const parsed = contextRecord(context);
    if (!parsed) return null;
    const story = parsed.target_story && typeof parsed.target_story === 'object'
      ? parsed.target_story as Record<string, unknown>
      : null;
    const criterion = parsed.target_acceptance_criterion && typeof parsed.target_acceptance_criterion === 'object'
      ? parsed.target_acceptance_criterion as Record<string, unknown>
      : null;
    const parts = [
      parsed.requirement_code,
      parsed.title,
      story?.ticket_code,
      story?.story_title,
      criterion?.criterion_code,
      criterion?.title,
    ].filter((part): part is string => typeof part === 'string' && part.trim().length > 0);
    return parts.length ? parts.join(' · ') : null;
  } catch {
    // Never expose a large serialized payload as a heading. The identifiers
    // below still provide a useful target when legacy context is malformed.
    return null;
  }
};

const clarificationTarget = (question: ClarificationQuestion): string => {
  const context = readableContext(question.requirement_context);
  if (question.target_requirement_id) return context || `Requirement ${question.target_requirement_id}`;
  if (question.target_user_story_id) return context || `User story ${question.target_user_story_id}`;
  if (/acceptance|criteria/i.test(question.checklist_category)) return context || 'Acceptance criteria';
  return context || readableCode(question.checklist_category || 'Requirement detail');
};

const findingTarget = (finding: AuditFindingPayload): string => {
  if (finding.target_requirement_id) return `Requirement ${finding.target_requirement_id}`;
  return readableCode(finding.category || finding.rule_id || 'Validation finding');
};

function SourceFiles({ names }: { names: string[] }) {
  if (!names.length) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-on-surface-variant">
      <FileText className="h-3 w-3 shrink-0" />
      <span className="font-semibold">Source:</span>
      {names.map(name => (
        <span key={name} className="max-w-full truncate rounded-md border border-outline bg-white px-1.5 py-0.5 font-mono" title={name}>
          {name}
        </span>
      ))}
    </div>
  );
}

function ValidationCard({
  audit,
  requirements,
  isPending,
  isProcessing,
  clarificationAnswers,
  onAnswerChange,
}: {
  audit: AuditResult;
  requirements: RequirementItem[];
  isPending: boolean;
  isProcessing: boolean;
  clarificationAnswers: Record<string, string>;
  onAnswerChange: (key: string, value: string) => void;
}) {
  // CLARIFY 1.1 — Clarification form renderer inside the persisted audit card.
  // Input: saved questions plus local answer drafts. Resolved questions are read-only;
  // unresolved questions use their stable database id as the answer key, with q-<index>
  // only for legacy/id-less payloads. The form submits through CLARIFY 1.3.
  const questions = audit.clarification_questions || [];
  const findings = audit.findings || [];
  const currentRequirements = requirements.filter(requirement =>
    !requirement.status || requirement.status.toLowerCase() === 'active',
  );
  const conflicts = findings.filter(finding => finding.finding_type === 'conflicting');
  const blockingIssues = findings.filter(finding =>
    finding.impact === 'blocking' && finding.finding_type !== 'conflicting',
  );
  const reviews = findings.filter(finding => finding.impact !== 'blocking');
  const findingGroups = Array.from(findings.reduce((groups, finding, index) => {
    const key = finding.target_requirement_id || findingTarget(finding);
    const existing = groups.get(key) || { key, target: findingTarget(finding), findings: [] as Array<{ finding: AuditFindingPayload; index: number }> };
    existing.findings.push({ finding, index });
    groups.set(key, existing);
    return groups;
  }, new Map<string, { key: string; target: string; findings: Array<{ finding: AuditFindingPayload; index: number }> }>()).values())
    .map(group => ({
      ...group,
      findings: [...group.findings].sort((left, right) =>
        (left.finding.rule_id || left.finding.description).localeCompare(
          right.finding.rule_id || right.finding.description,
          undefined,
          { numeric: true },
        ),
      ),
    }))
    .sort((left, right) => left.target.localeCompare(right.target, undefined, { numeric: true }));
  const unresolvedQuestions = questions.filter(question => !question.is_resolved);
  const affectedRequirementKeys = new Set<string>();
  findings.forEach(finding => {
    if (finding.target_requirement_id) affectedRequirementKeys.add(finding.target_requirement_id);
  });
  questions.forEach(question => {
    if (question.target_requirement_id) affectedRequirementKeys.add(question.target_requirement_id);
    const parsed = contextRecord(question.requirement_context);
    if (typeof parsed?.requirement_code === 'string') affectedRequirementKeys.add(parsed.requirement_code);
    if (question.target_user_story_id) {
      const owner = currentRequirements.find(requirement =>
        requirement.user_stories.some(story => story.ticket_code === question.target_user_story_id),
      );
      if (owner?.id) affectedRequirementKeys.add(owner.id);
      if (owner?.requirement_code) affectedRequirementKeys.add(owner.requirement_code);
    }
  });
  const passedRequirements = currentRequirements
    .filter(requirement =>
      !affectedRequirementKeys.has(requirement.id || '') &&
      !affectedRequirementKeys.has(requirement.requirement_code),
    )
    .sort((left, right) => left.requirement_code.localeCompare(
      right.requirement_code,
      undefined,
      { numeric: true },
    ));
  const orderedQuestions = questions
    .map((question, originalIndex) => ({ question, originalIndex }))
    .sort((left, right) => clarificationTarget(left.question).localeCompare(
      clarificationTarget(right.question),
      undefined,
      { numeric: true },
    ));
  const requirementOrder = (target: string | null | undefined): number => {
    const matchedRequirement = currentRequirements.find(requirement =>
      requirement.id === target || requirement.requirement_code === target,
    );
    const sortable = matchedRequirement?.requirement_code || target || '';
    const numericCode = sortable.match(/\bREQ\D*0*(\d+)\b/i);
    return numericCode ? Number(numericCode[1]) : Number.MAX_SAFE_INTEGER;
  };
  const passedCount = passedRequirements.length;
  const passedRules = Array.from(new Set(
    (audit.passed_checks || []).map(readableAuditRule).filter(Boolean),
  ));
  const total = currentRequirements.length || passedCount + findings.length + questions.length;
  const allSources = Array.from(new Set([
    ...sourceNames([
      ...(audit.source_references || []),
      ...findings.flatMap(finding => finding.source_references || []),
      ...questions.flatMap(question => question.source_references || []),
    ]),
    ...questions
      .map(question => question.provenance?.document?.trim())
      .filter((name): name is string => Boolean(name)),
  ]));
  const hasDraftAnswer = Object.values(clarificationAnswers).some(value => value.trim());

  return (
    <div className="w-full min-w-0">
      <p className="text-[13px] leading-relaxed text-on-surface">
        Validated <strong>{total} requirement{total === 1 ? '' : 's'}</strong>
        {allSources.length ? ' against the project knowledge base.' : '.'}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700">✓ {passedCount} Passed</span>
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700">⚠ {reviews.length + unresolvedQuestions.length} Needs review</span>
        <span className="rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-[10px] font-semibold text-red-700">! {blockingIssues.length} Blocking</span>
        <span className="rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-[10px] font-semibold text-red-700">× {conflicts.length} Conflict{conflicts.length === 1 ? '' : 's'}</span>
      </div>

      {passedRules.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-semibold text-on-surface-variant">Passed checks:</span>
          {passedRules.map(rule => (
            <span key={rule} className="rounded-md border border-emerald-200 bg-white px-2 py-0.5 text-[10px] font-medium text-emerald-700">
              {rule}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex flex-col gap-2">
        {passedRequirements.map(requirement => (
          <div
            key={requirement.id || requirement.requirement_code}
            style={{ order: requirementOrder(requirement.requirement_code) * 10 }}
            className="flex items-center gap-2 rounded-xl border border-emerald-300 bg-white px-3 py-2.5"
          >
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-700" />
            <span className="min-w-0 flex-1 break-words text-xs">
              <strong>{requirement.requirement_code}</strong>{requirement.title ? ` · ${requirement.title}` : ''}
            </span>
            <span className="text-[10px] font-semibold text-emerald-700">Passed</span>
          </div>
        ))}

        {findingGroups.map(group => {
          const conflict = group.findings.some(({ finding }) => finding.finding_type === 'conflicting');
          const blocking = group.findings.some(({ finding }) => finding.impact === 'blocking');
          const requirement = currentRequirements.find(item =>
            item.id === group.key || item.requirement_code === group.key,
          );
          const target = requirement
            ? `Requirement ${requirement.requirement_code}${requirement.title ? ` · ${requirement.title}` : ''}`
            : group.target;
          return (
            <div
              key={group.key}
              style={{ order: requirementOrder(requirement?.requirement_code || group.target) * 10 + 1 }}
              className={`rounded-xl border bg-white px-3 py-3 ${blocking ? 'border-red-300' : 'border-amber-300'}`}
            >
              <div className="flex items-start gap-2">
                <AlertTriangle className={`mt-0.5 h-4 w-4 shrink-0 ${blocking ? 'text-red-600' : 'text-amber-600'}`} />
                <div className="min-w-0 flex-1">
                  <p className={`break-words text-xs font-semibold ${blocking ? 'text-red-800' : 'text-amber-800'}`}>{target}</p>
                </div>
                <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[9px] font-bold ${blocking ? 'border-red-200 bg-red-100 text-red-700' : 'border-amber-200 bg-amber-100 text-amber-700'}`}>
                  {conflict ? 'Conflict' : blocking ? 'Blocking issue' : 'Needs review'}
                </span>
              </div>
              <div className="mt-2 divide-y divide-outline">
                {group.findings.map(({ finding, index }, issueIndex) => (
                  <div key={`${finding.rule_id}-${index}`} className="flex gap-2 py-2 first:pt-0 last:pb-0">
                    <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold ${blocking ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'}`}>
                      {issueIndex + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="break-words text-[11px] leading-relaxed text-on-surface-variant">{finding.description}</p>
                      <SourceFiles names={sourceLabels(finding.source_references)} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}

        {orderedQuestions.map(({ question, originalIndex }) => {
          const answerKey = question.id || `q-${originalIndex}`;
          const answered = Boolean(question.is_resolved);
          const names = sourceLabels(
            question.source_references,
            question.provenance?.document,
            question.provenance?.section,
          );
          const targetParts = clarificationTarget(question).split(' · ').filter(Boolean);
          return (
            <div
              key={answerKey}
              style={{ order: requirementOrder(question.target_requirement_id || clarificationTarget(question)) * 10 + 2 }}
              className={`rounded-xl border bg-white px-3 py-3 ${answered ? 'border-emerald-300' : 'border-amber-300'}`}
            >
              <div className="flex items-center gap-1.5 text-[10px] font-semibold">
                <span className={`h-2 w-2 rounded-full ${answered ? 'bg-emerald-500' : 'bg-amber-500'}`} aria-hidden="true" />
                <span className={answered ? 'text-emerald-700' : 'text-amber-700'}>{answered ? 'Answered' : 'Needs review'}</span>
              </div>
              <div className="mt-2 space-y-0.5">
                {targetParts.map((part, partIndex) => (
                  <p key={`${part}-${partIndex}`} className={partIndex === 0 ? 'break-words text-xs font-bold text-on-surface' : 'break-words text-[11px] font-medium text-on-surface-variant'}>
                    {part}
                  </p>
                ))}
              </div>
              <p className="mt-2 break-words text-[13px] font-medium leading-relaxed text-on-surface">{question.question_text}</p>
              <SourceFiles names={names} />
              {answered ? (
                <div className="mt-2 rounded-lg border border-outline bg-white px-3 py-2">
                  <p className="text-[10px] font-semibold uppercase tracking-wide text-emerald-700">Answer</p>
                  <p className="mt-0.5 whitespace-pre-wrap break-words text-xs leading-relaxed text-on-surface">{question.user_answer || 'Answered'}</p>
                </div>
              ) : (
                <textarea
                  rows={1}
                  value={clarificationAnswers[answerKey] || ''}
                  onChange={event => onAnswerChange(answerKey, event.target.value)}
                  onInput={event => {
                    const element = event.currentTarget;
                    element.style.height = 'auto';
                    element.style.height = `${Math.min(element.scrollHeight, 160)}px`;
                  }}
                  disabled={isProcessing || !isPending}
                  placeholder={isPending ? 'Enter the missing detail…' : 'This clarification is no longer active'}
                  className="mt-2 min-h-10 max-h-40 w-full resize-none rounded-lg border border-outline bg-white px-3 py-2.5 text-xs leading-relaxed text-on-surface outline-none placeholder:text-on-surface-variant/40 focus:border-primary focus:ring-1 focus:ring-primary/30 disabled:bg-slate-50"
                />
              )}
            </div>
          );
        })}
      </div>

      {isPending && hasDraftAnswer && (
        <button type="submit" disabled={isProcessing} className="mt-3 flex w-full items-center justify-center gap-1.5 rounded-xl bg-primary py-2.5 text-xs font-bold text-on-primary transition hover:brightness-110 disabled:cursor-wait disabled:opacity-60">
          {isProcessing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          {isProcessing ? 'Preparing update…' : 'Submit answers & prepare update'}
        </button>
      )}
    </div>
  );
}

export function ChatPanel({ state, userName }: { state: ProjectState; userName: string }) {
  const {
    messages,
    chatEndRef,
    isProcessing,
    isLoading,
    currentAgentNode,
    rawInput,
    setRawInput,
    projectSearchQuery,
    handleSendMessage,
    handleStopGeneration,
    clarificationAnswers,
    structuredRequirements,
    prdMarkdown,
    lockableRequirements,
    handleUpdateAnswerValue,
    handleSubmitClarifications,
    handleValidateRequirements,
    handleGeneratePRD,
    projects,
    projectId,
    lmStudioOnline,
    lmStudioModelLoaded,
    documents,
    pendingActions,
    canRunAgentActions,
    agentActionsBlockedReason,
    isCascadeGenerating,
  } = state;

  // Clip-icon attach: uploads straight into the project knowledge base
  // (same KNOWLEDGE-ONLY contract as the DocumentLibrary upload control).
  // DOC-UPLOAD 1.1 — Upload ENTRY POINT of the whole flow: the paperclip is the ONLY way a
  //             document enters a project. It calls hook 2.3 directly; extraction stays a
  //             separate, explicit action (DOC-UPLOAD 1.4 → 2.4 → 4.5).
  const fileInputRef = useRef<HTMLInputElement>(null);

  // DOC-UPLOAD 1.1.1 — Guarded click: no project ⇒ no upload (the button is also disabled
  //                 while uploading/streaming).
  const handleClipClick = (): void => {
    if (!projectId) return;
    fileInputRef.current?.click();
  };

  // DOC-UPLOAD 1.1.2 — Pick handler: clears the input first so the SAME file can be attached
  //                 again later, then hands the File to 2.3 (which owns the POST + status
  //                 branching). No local state is mutated here — the list reload in 2.2 is
  //                 what makes the new row appear.
  const handleFilePicked = async (
    event: ChangeEvent<HTMLInputElement>,
  ): Promise<void> => {
    const file = event.target.files?.[0];
    // Allow re-picking the same file later.
    event.target.value = '';
    if (!file || !projectId) return;
    await documents.handleUploadDocument(file);
  };

  // Agent-action button gating: a brand-new project with no knowledge
  // documents and no gathered requirements has nothing for the Auditor /
  // Architect agents to work with, so Validate Requirements / Generate PRD
  // stay disabled until real content exists.
  const llmUnavailable = lmStudioOnline === false || !lmStudioModelLoaded;
  const isDocumentExtracting = Boolean(documents.processingDocId);
  const isActiveProcessing = isProcessing || isCascadeGenerating || isDocumentExtracting;
  const hasPendingRequirementChanges = pendingActions.some(
    action => action.action_type !== 'GENERATE_PRD',
  );
  const hasActiveRequirements =
    (structuredRequirements.requirements?.length ?? 0) > 0 ||
    (structuredRequirements.user_stories?.length ?? 0) > 0;
  const hasExistingPrd = Boolean(prdMarkdown.trim());
  const agentActionsDisabled = isLoading || isActiveProcessing || !canRunAgentActions || llmUnavailable;
  const generatePrdDisabled =
    isLoading ||
    isActiveProcessing ||
    llmUnavailable ||
    hasPendingRequirementChanges ||
    (!hasActiveRequirements && !hasExistingPrd);
  const agentActionHint = !projectId
    ? 'Create or select a project first'
    : agentActionsBlockedReason
      ? agentActionsBlockedReason
    : !canRunAgentActions
      ? 'Add requirements via chat or upload a knowledge document first'
      : llmUnavailable
        ? 'Start LM Studio and load the configured model before running AI actions'
        : '';

  // ---- Search-driven chat autoscroll --------------------------------------
  // The-sidebar search also matches conversation message content. When a query
  // is active, the chat highlight lights up matching bubbles and this brings
  // the FIRST matching bubble into the center of the chat viewport so the user
  // doesn't have to hunt for it.
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const lastMatchedBubbleIdRef = useRef<string | null>(null);

  useEffect(() => {
    const query = projectSearchQuery.trim().toLowerCase();
    if (!query) {
      lastMatchedBubbleIdRef.current = null;
      return;
    }
    const matchIndex = messages.findIndex(m => (m.content || '').toLowerCase().includes(query));
    if (matchIndex === -1) {
      lastMatchedBubbleIdRef.current = null;
      return;
    }
    const matchId = messages[matchIndex]?.id;
    // Don't re-jump to a bubble that is already the search target: keeps live
    // chat auto-scrolling to the bottom without fighting the search focus.
    if (matchId && lastMatchedBubbleIdRef.current === matchId) return;

    // Deferred so it lands AFTER the store's auto-scroll-to-bottom effect for
    // the same commit (parent effects run last) — the search focus wins.
    const timer = setTimeout(() => {
      const container = chatScrollRef.current;
      if (!container) return;
      const el = container.querySelector(`[data-msg-index="${matchIndex}"]`);
      if (!el) return;
      const containerRect = container.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const top =
        elRect.top - containerRect.top + container.scrollTop -
        (container.clientHeight / 2) + (elRect.height / 2);
      container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
      lastMatchedBubbleIdRef.current = matchId;
    }, 60);
    return () => clearTimeout(timer);
  }, [projectSearchQuery, messages]);

  // ---- Smart auto-scroll + "jump to latest" pill ---------------------------
  // The timeline only follows new output while the user is already reading near
  // the bottom. If they scrolled up during an agent run, new messages never
  // yank the viewport back down; a floating pill offers a one-click way back.
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const isNearBottomRef = useRef(true);
  const [showJumpPill, setShowJumpPill] = useState<boolean>(false);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  const [hasOlderMessages, setHasOlderMessages] = useState(true);
  const loadingOlderRef = useRef(false);
  const olderMessagesSentinelRef = useRef<HTMLDivElement>(null);
  const loadOlderMessagesRef = useRef<() => Promise<void>>(async () => {});
  const activeProjectRef = useRef(projectId);
  activeProjectRef.current = projectId;

  useEffect(() => {
    setHasOlderMessages(true);
    setIsLoadingOlder(false);
    loadingOlderRef.current = false;
  }, [projectId]);

  const loadOlderMessages = async (): Promise<void> => {
    const container = chatScrollRef.current;
    const oldestId = messages[0]?.id;
    if (!projectId || !oldestId || !container || loadingOlderRef.current || !hasOlderMessages) return;
    loadingOlderRef.current = true;
    setIsLoadingOlder(true);
    const previousHeight = container.scrollHeight;
    try {
      const rows = await api.listConversationMessages(projectId, {
        limit: OLDER_MESSAGE_PAGE_SIZE,
        before: oldestId,
      });
      if (activeProjectRef.current !== projectId) return;
      const older = toChatMessages({ conversation_history: rows }, false);
      if (rows.length < OLDER_MESSAGE_PAGE_SIZE) setHasOlderMessages(false);
      if (older.length > 0) {
        state.setMessages(current => {
          const existingIds = new Set(current.map(message => message.id));
          const uniqueOlder = older.filter(message => !existingIds.has(message.id));
          if (uniqueOlder.length > 0) {
          requestAnimationFrame(() => {
            const currentContainer = chatScrollRef.current;
            if (currentContainer) currentContainer.scrollTop += currentContainer.scrollHeight - previousHeight;
          });
          }
          return uniqueOlder.length > 0 ? [...uniqueOlder, ...current] : current;
        });
      }
    } catch {
      // Keep the existing timeline usable; another upward scroll can retry.
    } finally {
      loadingOlderRef.current = false;
      setIsLoadingOlder(false);
    }
  };
  loadOlderMessagesRef.current = loadOlderMessages;

  // Observe a real element at the top of the scroll viewport instead of
  // depending exclusively on an exact scrollTop value. This also works when
  // the first page is too short to create a scrollbar.
  useEffect(() => {
    const root = chatScrollRef.current;
    const target = olderMessagesSentinelRef.current;
    if (!root || !target || !projectId || !hasOlderMessages) return;
    const observer = new IntersectionObserver(
      entries => {
        if (entries.some(entry => entry.isIntersecting)) {
          void loadOlderMessagesRef.current();
        }
      },
      { root, rootMargin: '120px 0px 0px 0px', threshold: 0.01 },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [projectId, hasOlderMessages]);

  const handleChatScroll = (): void => {
    const container = chatScrollRef.current;
    if (!container) return;
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    isNearBottomRef.current = distance <= 96;
    setShowJumpPill(distance > 200);
    if (container.scrollTop <= 48) void loadOlderMessages();
  };

  const scrollToLatest = (): void => {
    isNearBottomRef.current = true;
    setShowJumpPill(false);
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    if (messages.length === 0) return;
    // Always chase the user's own sends; otherwise follow only when near bottom.
    const lastRole = messages[messages.length - 1]?.role;
    if (lastRole !== 'user' && !isNearBottomRef.current) return;
    isNearBottomRef.current = true;
    setShowJumpPill(false);
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isProcessing, chatEndRef]);

  useEffect(() => {
    if (!isCascadeGenerating || !isNearBottomRef.current) return;
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [isCascadeGenerating, chatEndRef]);

  // Auto-grow composer: expands with content up to a readable cap, and
  // collapses back after a message is sent (rawInput is cleared).
  useEffect(() => {
    const el = composerRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [rawInput]);

  return (
    <section className="chat-panel flex flex-col bg-white relative z-10" style={{ flexGrow: 0, flexShrink: 0, flexBasis: `${state.splitPct}%` }}>
      {/* Section Header */}
      <div id="chat-panel-header" className="h-[50px] px-5 border-b border-outline flex items-center bg-white shrink-0">
        <h2 className="text-[13px] font-bold text-on-surface truncate">
          {projects.find(p => p.id === projectId)?.name || "New Project"}
        </h2>
      </div>

      {/* Chat Logs scroll list — extra bottom padding on <sm screens because
          the action buttons stack into two rows and the composer grows taller. */}
      <div ref={chatScrollRef} onScroll={handleChatScroll} className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-5 custom-scrollbar pb-44 sm:pb-32 bg-white">
        <div ref={olderMessagesSentinelRef} className="flex min-h-6 items-center justify-center">
          {isLoadingOlder ? (
            <div className="flex items-center justify-center gap-1.5 text-[10px] text-on-surface-variant">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading earlier messages…
            </div>
          ) : hasOlderMessages && messages.length > 0 ? (
            <button
              type="button"
              onClick={() => void loadOlderMessages()}
              className="rounded-full border border-outline bg-white px-3 py-1 text-[10px] font-semibold text-on-surface-variant transition-colors hover:border-primary/40 hover:text-primary"
            >
              Load 10 earlier messages
            </button>
          ) : !hasOlderMessages && messages.length > 0 ? (
            <span className="text-[10px] text-on-surface-variant/70">Beginning of conversation</span>
          ) : null}
        </div>
        <AnimatePresence initial={false}>
          {messages.map((msg, msgIndex) => {
            const isUser = msg.role === 'user';
            const isSystem = msg.role === 'system';
            // User bubbles sit on the dark brand background, so the highlight
            // needs a light translucent wash instead of the brand-tinted one.
            const bubbleHighlightClass = isUser
              ? 'bg-white/30 text-on-primary font-semibold rounded-[2px] px-0.5'
              : '';

            if (isSystem) {
              return (
                <motion.div 
                  key={msg.id}
                  data-msg-index={msgIndex}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="mx-auto max-w-sm text-center py-2"
                >
                  <span className="inline-block max-w-full px-3 py-1 bg-black/5 rounded-full text-[10.5px] font-mono text-on-surface-variant border border-black/5 break-words">
                    {highlightMatch(msg.content, projectSearchQuery)}
                  </span>
                </motion.div>
              );
            }

            return (
              <motion.div
                key={msg.id}
                data-msg-index={msgIndex}
                initial={{ opacity: 0, y: 15 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.3 }}
                className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}
              >
                {/* Bot avatar */}
                {!isUser && (
                  <div className="w-7 h-7 rounded-full bg-primary flex items-center justify-center shrink-0">
                    <Bot className="text-on-primary w-3.5 h-3.5" />
                  </div>
                )}

                <div className={`flex flex-col gap-1 ${msg.auditResultSnapshot?.clarification_questions?.length ? 'max-w-[96%]' : 'max-w-[88%]'} ${isUser ? 'items-end' : 'items-start'}`}>
                  {/* Name card */}
                  <span className="text-[10px] text-[#a89b91] font-semibold tracking-[0.02em]">
                    {isUser ? displayUserName(userName) : 'AI'}, {msg.timestamp}
                  </span>

                  {/* Chat text box */}
                  <div className={`${msg.auditResultSnapshot ? 'w-full' : ''} px-4 py-3 rounded-[16px] border text-[13.5px] leading-[1.6] ${
                    isUser 
                      ? 'bg-[#f4ece4] text-on-surface border-[#e8e2d9] rounded-tr-[4px]' 
                      : 'bg-white text-on-surface border-[#e8e2d9] rounded-tl-[4px] shadow-[0_2px_3px_rgba(0,0,0,0.02)]'
                  }`}>
                    {/* CLARIFY 1.0 / AUDIT 5.3.1 — Mount the clarification form inside
                        the persisted audit snapshot. Only its newest pending message is
                        editable; submission follows CLARIFY 1.3 → 2.1 → 3.0. */}
                    {msg.auditResultSnapshot ? (
                      <form onSubmit={handleSubmitClarifications}>
                        <ValidationCard
                          audit={msg.auditResultSnapshot}
                          requirements={(structuredRequirements.requirements?.length ?? 0) > 0
                            ? structuredRequirements.requirements || []
                            : lockableRequirements}
                          isPending={Boolean(msg.isPendingClarifications)}
                          isProcessing={isProcessing}
                          clarificationAnswers={clarificationAnswers}
                          onAnswerChange={handleUpdateAnswerValue}
                        />
                      </form>
                    ) : (
                      <p className="whitespace-pre-line break-words">
                        {renderInlineFormatting(msg.content, projectSearchQuery, bubbleHighlightClass)}
                      </p>
                    )}
                  </div>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>

        {isActiveProcessing && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className="flex items-center gap-3 pl-12 text-xs text-on-surface-variant/80 italic font-medium"
            role="status"
            aria-live="polite"
          >
            <RefreshCw className="w-3.5 h-3.5 shrink-0 animate-spin text-primary" />
            <span>
              {isDocumentExtracting
                ? 'Extracting requirements from the document with LM Studio…'
                : isCascadeGenerating
                ? 'Updating the PRD with your saved requirements…'
                : currentAgentNode === 'gatherer_node'
                  ? 'Structuring your requirements with LM Studio…'
                  : currentAgentNode === 'auditor_node'
                    ? 'Validating your requirements with LM Studio…'
                    : currentAgentNode === 'architect_node'
                      ? 'Generating your PRD with LM Studio…'
                      : 'Working on your request with LM Studio…'}
            </span>
          </motion.div>
        )}

        <div ref={chatEndRef} />
      </div>

      {/* Floating "jump to latest" pill (shown while scrolled away from the bottom) */}
      <AnimatePresence>
        {showJumpPill && (
          <motion.button
            type="button"
            key="jump-to-latest"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.15 }}
            onClick={scrollToLatest}
            className="absolute left-1/2 -translate-x-1/2 bottom-[190px] sm:bottom-[150px] z-30 flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-surface border border-outline shadow-lg text-[11px] font-bold text-on-surface hover:border-primary/40 hover:text-primary transition-colors cursor-pointer"
          >
            <ArrowDown className="w-3.5 h-3.5" />
            <span>Jump to latest</span>
          </motion.button>
        )}
      </AnimatePresence>

      {/* Conversational Fixed Input Container */}
      <div className="absolute bottom-0 w-full px-3.5 py-3 border-t border-outline bg-white z-20 space-y-2">
        {/* Agent Actions Row — buttons stack on very narrow panels instead of
            overflowing; the min-width keeps each label on one line. */}
        <div className="grid grid-cols-1 min-[460px]:grid-cols-2 gap-2">
          {/* AUDIT 5.0 (UI trigger) — "Validate Requirements" button: on-demand
              auditor run (empty raw_input + target_agent=auditor). Disabled via
              agentActionsDisabled when there is nothing to audit. */}
          <button
            type="button"
            onClick={handleValidateRequirements}
            disabled={agentActionsDisabled}
            title={agentActionsDisabled ? agentActionHint : 'Validate current requirements and knowledge-base document content'}
            className="min-w-0 w-full h-[42px] px-3 rounded-[10px] border border-primary hover:bg-[#faf8f4] font-label-md text-xs text-primary font-semibold transition-all flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <CheckCircle2 className="w-3.5 h-3.5 text-primary" />
            <span>Validate Requirements</span>
          </button>
          {/* ARCHITECT 1.0 (UI trigger) — "Generate PRD" button: on-demand architect
              run (empty raw_input + target_agent=architect). Disabled via
              agentActionsDisabled when there is nothing to compile. */}
          <button
            type="button"
            onClick={handleGeneratePRD}
            disabled={generatePrdDisabled}
            title={hasPendingRequirementChanges
              ? 'Save or discard the pending requirement changes before generating the PRD'
              : !hasActiveRequirements && !hasExistingPrd
                ? 'There are no requirements to generate.'
              : llmUnavailable
                ? 'Start LM Studio and load the configured model before running AI actions'
              : isLoading || isActiveProcessing
                ? 'Please wait for the current operation to finish'
                : 'Compile the formal PRD and architecture diagram'}
            className="min-w-0 w-full h-[42px] px-3 rounded-[10px] bg-primary text-on-primary hover:brightness-110 font-label-md text-xs font-semibold transition-all flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <FileText className="w-3.5 h-3.5" />
            <span>Generate PRD</span>
          </button>
        </div>

        <div className="flex items-end gap-2 bg-white rounded-[16px] pl-2 pr-1.5 py-1.5 border border-[#e8e2d9] shadow-[0_2px_4px_rgba(0,0,0,0.02)] hover:border-primary focus-within:border-primary focus-within:ring-1 focus-within:ring-primary/20 transition-all">
          <Tooltip
            label={projectId ? 'Attach file (.docx / .pdf / .md / .txt)' : 'Create or select a project first'}
            side="top"
            align="start"
          >
            <button
              type="button"
              aria-label="Attach file"
              onClick={handleClipClick}
              disabled={!projectId || isLoading || isActiveProcessing || documents.isUploading}
              className="w-8 h-8 rounded-full bg-[#f4ece4] flex items-center justify-center text-primary hover:bg-[#eadfd5] transition-colors shrink-0 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {documents.isUploading ? (
                <Loader2 className="w-4.5 h-4.5 animate-spin" />
              ) : (
                <Paperclip className="w-4.5 h-4.5" />
              )}
            </button>
          </Tooltip>
          {/* DOC-UPLOAD 1.1 — hidden file input; `accept` mirrors domain allow-list 6.1. */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".docx,.pdf,.md,.txt"
            className="hidden"
            onChange={(e) => void handleFilePicked(e)}
          />
          {/* CHAT 1.1 — Collect and submit a conversational message.
              Trigger: user presses Enter or the Send button.
              Input: rawInput, bounded by CHAT_INPUT_MAX_LENGTH.
              Calls: CHAT 1.2 handleSendMessage.
              Branches: Shift+Enter inserts a line; IME composition does not submit.
              Writes: NONE here; useChat owns message and request state. */}
          <textarea
            ref={composerRef}
            rows={1}
            value={rawInput}
            maxLength={CHAT_INPUT_MAX_LENGTH}
            aria-describedby={rawInput.length >= CHAT_INPUT_MAX_LENGTH ? 'chat-input-character-warning' : undefined}
            disabled={isLoading || isActiveProcessing}
            onChange={(e) => setRawInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter inserts a newline. The isComposing
              // guard keeps IME composition (e.g. Thai keyboard) from sending.
              // CHAT 1.1 (composer send) — Enter sends, Shift+Enter inserts a
              // newline, and the isComposing guard keeps IME composition (e.g. a
              // Thai keyboard) from submitting mid-word.
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                handleSendMessage();
              }
            }}
            placeholder={isActiveProcessing ? 'Processing...' : isLoading ? 'Loading...' : 'Add !!!, !!, or ! before a request to set priority…'}
            className="flex-1 min-w-0 bg-transparent border-none focus:ring-0 font-body-sm text-sm placeholder:text-on-surface-variant/55 text-on-surface outline-none resize-none max-h-40 leading-relaxed py-1 custom-scrollbar"
          />
          {isActiveProcessing ? (
            <Tooltip label={isDocumentExtracting ? 'Stop document extraction' : 'Stop generating'} side="top">
              <button
                type="button"
                aria-label={isDocumentExtracting ? 'Stop document extraction' : 'Stop generating'}
                onClick={handleStopGeneration}
                className="w-8.5 h-8.5 rounded-full flex items-center justify-center transition-transform shrink-0 shadow-sm bg-red-600 text-white hover:bg-red-700 hover:scale-105 cursor-pointer"
              >
                <Square className="w-3.5 h-3.5 fill-current" />
              </button>
            </Tooltip>
          ) : isLoading ? (
            <button
              type="button"
              aria-label="Loading project"
              disabled
              className="w-8.5 h-8.5 rounded-full flex items-center justify-center shrink-0 bg-slate-200 text-slate-500 cursor-wait"
            >
              <Loader2 className="w-4 h-4 animate-spin" />
            </button>
          ) : (
            // CHAT 1.1 (send button) — same entry point as Enter; stays disabled
            // while the composer is blank and swaps to Stop while a run is active
            // (the Stop path is the CANCELLATION flow).
            <Tooltip label="Send message" side="top">
              <button
                onClick={() => handleSendMessage()}
                aria-label="Send message"
                disabled={!rawInput.trim()}
                className={`w-8.5 h-8.5 rounded-full flex items-center justify-center transition-transform shrink-0 shadow-sm ${
                  rawInput.trim()
                    ? 'bg-primary text-on-primary hover:scale-105 cursor-pointer'
                    : 'bg-black/10 text-on-surface-variant/40 cursor-not-allowed'
                }`}
              >
                <Send className="w-4 h-4" />
              </button>
            </Tooltip>
          )}
        </div>
        {rawInput.length >= CHAT_INPUT_MAX_LENGTH && (
          <div
            id="chat-input-character-warning"
            className="flex justify-end px-1 text-[11px] font-semibold text-red-600"
            role="alert"
          >
            Maximum 4,000 characters reached.
          </div>
        )}
      </div>
    </section>
  );
}

// useChat — the conversational / multi-agent handlers extracted from the former
// useProjectState god-hook. Owns `rawInput` and the agent actions
// (send, validate, generate PRD, clarifications). Domain state lives in the
// shared `RequirementStore`; project info and pending-actions are injected
// through `deps`.
import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api/client";
import type {
  PendingActionPayload,
  ProcessRequirementsResponse,
  ProjectSummary,
} from "../api/types";
import {
  isLegacyPrdPreviewMessage,
  toAuditResultFromPayload,
  toGatheredRequirementsPayload,
  toStructuredRequirementsFromGathered,
} from "../api/transforms";
import type { VersionHistory } from "../components/types";
import type { ChatMessage } from "../components/types";
import { handleError } from "../components/Toast";
import type { RequirementStore } from "./useRequirementStore";
import type { WorkspaceTab } from "./useWorkspaceUi";
import {
  extractErrorDetail,
  isAbortError,
  isPayloadTooLargeError,
  isRateLimitError,
  isUnauthorizedError,
  payloadTooLargeContent,
  rateLimitedContent,
} from "./chat/agentErrors";
import { runBackgroundAgentJob } from "./chat/backgroundJobs";
import { useAgentCancellation } from "./chat/useAgentCancellation";
import { formatPrdVersionLabel } from "../utils/version";
import { displayVersionAuthor, extractProjectAuthor } from "../utils/actorName";

export interface ChatDeps {
  projectId: string | null;
  projects: ProjectSummary[];
  currentVersion: number;
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>;
  setActiveTab: (tab: WorkspaceTab) => void;
  loadProjectState: (
    projId: string,
    retries?: number,
    delay?: number,
  ) => Promise<void>;
  /**
   * Gate shared with the ChatPanel action buttons: false while the project has
   * neither knowledge documents nor gathered requirements, so the agent
   * handlers refuse to run on an empty project (defense-in-depth parity with
   * the disabled Validate / Generate PRD buttons).
   */
  canRunAgentActions: boolean;
  agentActionsBlockedReason: string;
}

export interface UseChatResult {
  messages: RequirementStore["messages"];
  setMessages: RequirementStore["setMessages"];
  rawInput: string;
  setRawInput: Dispatch<SetStateAction<string>>;
  isProcessing: boolean;
  isLoading: boolean;
  currentAgentNode: string | null;
  syncStatus: string;
  chatEndRef: RequirementStore["chatEndRef"];
  clarificationAnswers: Record<string, string>;
  handleSendMessage: (
    textToSend?: string,
    projectIdOverride?: string,
  ) => Promise<void>;
  handleValidateRequirements: () => Promise<void>;
  handleGeneratePRD: () => Promise<void>;
  /** Aborts the in-flight agent request (Stop button). No-op when idle. */
  handleStopGeneration: () => void;
  handleSubmitClarifications: (e: React.FormEvent) => Promise<void>;
  handleUpdateAnswerValue: (key: string, value: string) => void;
}

const nowTime = () =>
  new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const questionAnswerKey = (question: { id?: string }, index: number) =>
  question.id || `q-${index}`;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const timelineMetadata = (message: ChatMessage) => {
  if (message.isPendingClarifications) {
    return {
      workflow_state: message.role === "auditor" ? "auditor" : "gatherer",
      intent: "CLARIFICATION_REQUEST",
    };
  }
  if (
    message.id.startsWith("audit-failed-") ||
    message.id.startsWith("audit-passed-") ||
    message.id.startsWith("audit-stale-")
  ) {
    return { workflow_state: "auditor", intent: "AUDIT_RESULT" };
  }
  if (message.id.startsWith("audit-")) {
    return { workflow_state: "auditor", intent: "AUDIT_EVENT" };
  }
  if (message.id.startsWith("prd-")) {
    return { workflow_state: "architect", intent: "PRD_GENERATION" };
  }
  if (message.id.startsWith("clarification-")) {
    return { workflow_state: "gatherer", intent: "CLARIFICATION_ANSWER" };
  }
  return { workflow_state: message.role, intent: "TIMELINE_EVENT" };
};

export function useChat(
  store: RequirementStore,
  deps: ChatDeps,
): UseChatResult {
  const [rawInput, setRawInput] = useState<string>("");
  const persistingMessageIdsRef = useRef<Set<string>>(new Set());
  const durableMessageIdsRef = useRef<Map<string, string>>(new Map());
  const clarificationSubmitInFlightRef = useRef(false);

  useEffect(() => {
    persistingMessageIdsRef.current.clear();
    durableMessageIdsRef.current.clear();
  }, [deps.projectId]);

  // Persist every locally-created non-user bubble in display order, then swap
  // its temporary id for the database UUID. This covers validation/generation
  // errors and system decisions that previously disappeared on reload.
  useEffect(() => {
    const projectId = deps.projectId;
    if (!projectId) return;
    const pending = store.messages.filter(
      (message) =>
        !message.id.startsWith("init-") &&
        !UUID_PATTERN.test(message.id) &&
        !persistingMessageIdsRef.current.has(message.id),
    );
    const message = pending[0];
    if (!message) return;
    persistingMessageIdsRef.current.add(message.id);
    const durableId = durableMessageIdsRef.current.get(message.id) || crypto.randomUUID();
    durableMessageIdsRef.current.set(message.id, durableId);
    void api.appendConversationMessage(projectId, {
      id: durableId,
      role: message.role,
      message: message.content,
      ...timelineMetadata(message),
    }).then((saved) => {
      store.setMessages((current) =>
        current.map((item) =>
          item.id === message.id ? { ...item, id: saved.id } : item,
        ),
      );
    }).catch((error) => {
      persistingMessageIdsRef.current.delete(message.id);
      console.warn("Could not persist conversation timeline message.", error);
    });
  }, [deps.projectId, store.messages, store.setMessages]);

  const cancellation = useAgentCancellation(
    () => deps.projectId,
    store.setSyncStatus,
  );

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
    const projectAuthor = extractProjectAuthor(store.prdMarkdown);
    return history
      .map(
        (h) =>
          `- ${formatPrdVersionLabel(h.semVersion, h.version)} (${h.timestamp}, ${displayVersionAuthor(h.author, projectAuthor, h.changeType)}): ${h.description}`,
      )
      .join("\n");
  };

  // Handle Raw Conversational Input Submission
  // CHAT 1.2 — Frontend chat entry: one handler serves the composer, the send
  // button and the empty-state suggestion chips (CHAT 1.1). Layer chain:
  //   1.2 → 1.3 (api.processRequirements) → route 2.1 → intent 2.2 →
  //   GENERAL_CHAT branch 2.3 → 1.4 (UI branch on the returned intent).
  // Local guards: no selected project → inline warning bubble (no request);
  // blank input → no-op. The optimistic user bubble is appended BEFORE the
  // request, and the AbortController is what the Stop button cancels.
  const handleSendMessage = async (
    textToSend?: string,
    projectIdOverride?: string,
  ) => {
    const targetProjectId = projectIdOverride ?? deps.projectId;
    if (!targetProjectId) {
      store.setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: "assistant",
          content:
            "No project selected. Please select or create a project before interacting.",
          timestamp: nowTime(),
        },
      ]);
      return;
    }
    const projectId = targetProjectId;
    const inputMsg = textToSend || rawInput;
    if (!inputMsg.trim()) return;

    // Clear main input if sent from main input box
    if (!textToSend) setRawInput("");

    // Persist the user turn before starting LM work. This guarantees the input
    // survives gateway failures and sorts before the resulting error bubble.
    const durableUserId = crypto.randomUUID();
    const localUserId = `user-${durableUserId}`;
    persistingMessageIdsRef.current.add(localUserId);
    store.setMessages((prev) => [
      ...prev,
      {
        id: localUserId,
        role: "user",
        content: inputMsg,
        timestamp: nowTime(),
      },
    ]);
    try {
      await api.appendConversationMessage(projectId, {
        id: durableUserId,
        role: "user",
        message: inputMsg,
        workflow_state: "gatherer",
        intent: "PENDING_DETECTION",
      });
      store.setMessages((current) =>
        current.map((message) =>
          message.id === localUserId ? { ...message, id: durableUserId } : message,
        ),
      );
    } catch (error) {
      persistingMessageIdsRef.current.delete(localUserId);
      store.setMessages((current) => [...current]);
      console.warn("The user message will be retried by timeline persistence.", error);
    }

    // Set Loading State - show neutral loading indicator until intent is determined
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus("Detecting intent...");
    store.setCurrentAgentNode(null);

    const controller = cancellation.beginRequest();

    try {
      // The backend handles intent detection internally and routes accordingly:
      //   - GENERAL_CHAT: returns conversational response directly (no agent workflow)
      //   - REQUIREMENT_REQUEST: proceeds to Gatherer workflow
      // CHAT 1.3 — API boundary: POST /api/process-requirements, the SAME endpoint
      //            the agent flows use. Intent detection server-side decides whether
      //            this remains a conversation (CHAT 2.3) or enters the agent
      //            workflow (GATHERING / AUDITOR / ARCHITECT flows).
      const data = await api.processRequirements(
        {
          project_id: projectId,
          raw_input: inputMsg,
          // An intake is dispatched just after project creation, before the
          // hook closure has necessarily observed the newly selected project.
          // It must never carry the prior project's version or requirements.
          current_version: projectIdOverride ? 1 : deps.currentVersion,
          // CHAT 1.3 — DISCREPANCY (documented, unchanged): the chat path hardcodes
          // this string even though buildVersionHistorySummaries() above exists and
          // the agent actions use it; the backend's default is the same text.
          version_history_summaries: "No previous history.",
          target_agent: "gatherer",
          client_persists_timeline: true,
          ...(projectIdOverride
            ? {}
            : {
                structured_requirements: toGatheredRequirementsPayload(
                  store.structuredRequirements,
                ),
              }),
        },
        controller.signal,
      );

      // CHAT 1.4 — Response branch. GENERAL_CHAT (below) is the pure-conversation
      //            outcome: the assistant text is appended and NO requirement,
      //            story, version or PRD state is modified. Every other intent
      //            falls into the agent branches instead.
      const detectedIntent = data.detected_intent || "GENERAL_CHAT";

      if (data.no_changes === true) {
        const clarificationQuestions = (
          data.audit_result?.clarification_questions || []
        ).filter((question) => !question.is_resolved);
        if (clarificationQuestions.length > 0) {
          const initialAnswers: Record<string, string> = {};
          clarificationQuestions.forEach((question, index) => {
            initialAnswers[questionAnswerKey(question, index)] = "";
          });
          store.setClarificationAnswers(initialAnswers);
        }
        store.setSyncStatus("No changes were made");
        store.setCurrentAgentNode(null);
        store.setMessages((prev) => [
          ...prev.map((message) =>
            message.isPendingClarifications
              ? { ...message, isPendingClarifications: false }
              : message,
          ),
          {
            id: `no-change-${Date.now()}`,
            role: "assistant",
            content:
              data.message ||
              "The requested item was not found. No changes were made.",
            timestamp: nowTime(),
            isPendingClarifications: clarificationQuestions.length > 0,
            auditResultSnapshot:
              clarificationQuestions.length > 0 && data.audit_result
                ? toAuditResultFromPayload(data.audit_result)
                : undefined,
          },
        ]);
      } else if (detectedIntent === "GENERAL_CHAT") {
        // Do NOT modify requirements, user stories, version history, or any project artifacts
        store.setSyncStatus("Synced with Supabase Cloud");
        store.setCurrentAgentNode(null);
        const chatResponse =
          data.message ||
          "I understood your message. How can I help you further?";
        store.setMessages((prev) => [
          ...prev,
          {
            id: `general-chat-${Date.now()}`,
            role: "assistant",
            content: chatResponse,
            timestamp: nowTime(),
          },
        ]);
      } else {
        store.setSyncStatus("Gathering specifications...");
        store.setCurrentAgentNode("gatherer_node");

        const receivedReqs = data.structured_requirements || null;
        const pendingActionId = data.pending_action_id;
        const isPendingMerge = data.pending_merge === true;
        const pendingActionType =
          detectedIntent === "DELETE_REQUIREMENT" ? "ARCHIVE" : "MERGE";

        // GATHERING 8.1 — Frontend side of the handoff (UI layer): the staged merge is
        //            pushed into `pendingActions`, which renders the ConfirmationPanel
        //            ("Merge Preview Ready" bubble below). Nothing is written until the
        //            user confirms (CONFIRMATION flow) — Cancel discards the action,
        //            which is why the state above is reported as "unconfirmed".
        if (isPendingMerge && pendingActionId) {
          // Store the pending merge for confirmation
          deps.setPendingActions((prev) => [
            ...prev,
            {
              id: pendingActionId,
              project_id: projectId,
              action_type: pendingActionType,
              original_user_message: inputMsg,
              proposed_changes: (receivedReqs ?? undefined) as
                Record<string, unknown> | undefined,
            },
          ]);

          store.setMessages((prev) => [
            ...prev,
            {
              id: `merge-preview-${Date.now()}`,
              role: "assistant",
              content:
                detectedIntent === "DELETE_REQUIREMENT"
                  ? `${data.message || "Archive preview ready."}\n\nNothing has been archived yet. Review the alert below and select Confirm archive or Cancel.\n\n"${inputMsg}"`
                  : `Merge preview ready.\nI have analyzed your input and prepared the merged requirements. Please review the changes below and select Save or Cancel.\n\n"${inputMsg}"`,
              timestamp: nowTime(),
            },
          ]);
        } else if (receivedReqs && receivedReqs.epic_name) {
          // Fallback: if no pending merge (e.g. direct update), apply immediately
          const nextVer = deps.currentVersion + 1;
          const nextStructured = toStructuredRequirementsFromGathered(
            receivedReqs,
            nextVer,
          );
          store.setStructuredRequirements(nextStructured);
          store.setCurrentVersion(nextVer);

          const newHist: VersionHistory = {
            version: nextVer,
            timestamp: new Date()
              .toISOString()
              .replace("T", " ")
              .substring(0, 16),
            author: "Product Owner",
            description:
              inputMsg.substring(0, 70) + (inputMsg.length > 70 ? "..." : ""),
            requirementsSnapshot: nextStructured,
          };
          store.setVersionHistory((prev) => [newHist, ...prev]);
          store.setSyncStatus(`Requirements updated to revision ${nextVer}.`);
        }

        if (!isPendingMerge) {
          store.setMessages((prev) => [
            ...prev,
            {
              id: `gatherer-passed-${Date.now()}`,
              role: "assistant",
              content:
                "Requirements gathered and updated.\nI have structured your input into the Agile Requirements board.\n\nTo run compliance validation on these updated specifications, select Validate Requirements. Or select Generate PRD to build the technical documentation.",
              timestamp: nowTime(),
            },
          ]);
        }
      }
    } catch (err) {
      if (isAbortError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `stopped-${Date.now()}`,
            role: "assistant",
            content:
              "Stopped.\nYou cancelled this request. The server-side run was terminated and no requirement changes were applied.",
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Request stopped by user.");
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `rate-limited-${Date.now()}`,
            role: "assistant",
            content: rateLimitedContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Rate limited. Please wait before continuing.");
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `payload-too-large-${Date.now()}`,
            role: "assistant",
            content: payloadTooLargeContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus(
          "Request rejected: project exceeds the LLM context budget. Nothing was saved.",
        );
        return;
      }
      handleError(
        "The requirement engine could not complete your request.",
        err,
      );
      const errorContent = `Request failed. Nothing was saved.\n\nThe requirement engine could not complete your message (${(err as Error)?.message || err}).\n\nYour input was not recorded as a user story, and no requirement state was changed.`;
      store.setMessages((prev) => [
        ...prev,
        {
          id: `api-error-${Date.now()}`,
          role: "assistant",
          content: errorContent,
          timestamp: nowTime(),
        },
      ]);
      store.setSyncStatus("Request failed. Nothing was saved.");
    } finally {
      cancellation.finishRequest(controller);
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
      if (deps.agentActionsBlockedReason) {
        store.setSyncStatus(deps.agentActionsBlockedReason);
        return;
      }
      // Nothing to audit yet — mirror the disabled button instead of running
      // the auditor agent against an empty project.
      store.setSyncStatus(
        "Nothing to validate yet. Add requirements or upload a knowledge document.",
      );
      return;
    }
    // prepare the UI for the audit run / clear any prior audit state
    const projectId = deps.projectId;
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus("Running Technical Audit...");
    store.setCurrentAgentNode("auditor_node");

    const controller = cancellation.beginRequest();

    try {
      const request = {
        project_id: projectId,
        raw_input: "",
        // AUDIT 5.1 — The on-demand trigger: empty raw_input + target_agent="auditor"
        //            is exactly why the pipeline must bypass the GENERAL_CHAT
        //            short-circuit (see CHAT 2.3.0). On the backend this enters
        //            ROUTING 1.2 → auditor_node.
        target_agent: "auditor",
        client_persists_timeline: true,
        structured_requirements: toGatheredRequirementsPayload(
          store.structuredRequirements,
        ),
        current_version: deps.currentVersion,
        version_history_summaries: "No previous history.",
      };
      const data = await runBackgroundAgentJob<ProcessRequirementsResponse>({
        projectId,
        jobType: "auditor",
        request,
        signal: controller.signal,
        onJobId: cancellation.setBackgroundJob,
        onProgress: (stage) =>
          store.setSyncStatus(`Running Technical Audit… ${stage}`),
      });

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
        deps.setPendingActions((prev) => [
          ...prev,
          {
            id: pendingActionId,
            project_id: projectId,
            action_type: "MERGE",
            original_user_message: "Audit validation",
            proposed_changes: (data.audit_result ?? {}) as Record<
              string,
              unknown
            >,
          },
        ]);
      }

      const verdict =
        receivedAudit.verdict || (receivedAudit.is_valid ? "pass" : "fail");
      const questions = receivedAudit.clarification_questions || [];
      const questionTexts = questions
        .map((q) => `- ${q.question_text}`)
        .join("\n");
      const findingTexts = (receivedAudit.findings || [])
        .map((finding) => {
          const sources = finding.source_references
            .map(
              (source) =>
                `${source.document_name}${source.section ? `: ${source.section}` : ""}`,
            )
            .join(", ");
          const recommendation = finding.recommendation?.summary
            ? `\n  Suggested improvement: ${finding.recommendation.summary}`
            : "";
          return `- ${finding.impact}, ${finding.category} (${finding.rule_id || "ADHOC"}): ${finding.description}${sources ? ` (Source: ${sources})` : ""}${recommendation}`;
        })
        .join("\n");
      const context = receivedAudit.project_context;
      const contextText = context
        ? `Inferred context: ${context.business_segment}, ${context.product_domain}, ${context.solution_type}, ${context.delivery_stage} (${Math.round(context.confidence * 100)}% confidence)`
        : "";
      const operationalAuditCodes = new Set([
        "AUDIT_PARSE_ERROR",
        "AUDIT_CONTEXT_LIMIT",
        "AUDIT_OUTPUT_LIMIT",
        "AUDIT_INCOMPLETE",
      ]);
      const auditCouldNotComplete = receivedAudit.failed_checks.some((code) =>
        operationalAuditCodes.has(code),
      );
      if (isStaleResult) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `audit-stale-${Date.now()}`,
            role: "assistant",
            content: `The audit completed, but requirements changed while it was running. This result is read-only and was not applied.\n${contextText}\n\nFindings from the older version:\n${findingTexts || "No findings were returned."}\n\nRun Validate Requirements again for the latest version.`,
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus(
          "Audit completed on an older version. Run Validate again.",
        );
        return;
      }
      // AUDIT 5.3 — Failure branch: the assistant bubble carries the questions and
      //            `isPendingClarifications` + `auditResultSnapshot`, which is what
      //            renders the embedded clarification form (AUDIT 5.3.1). The sync
      //            status tells the user the audit is pending, not failed hard.
      if (verdict === "fail" || verdict === "needs_clarification") {
        const title = auditCouldNotComplete
          ? "Requirements audit could not complete"
          : verdict === "fail"
            ? "Requirements audit failed"
            : "Requirements audit needs clarification";
        const warningContent = auditCouldNotComplete
          ? `${title}\n\n${data.message || findingTexts || "Please run Validate again."}`
          : `${title}\n${contextText}\n\nFindings:\n${findingTexts || "No blocking finding was established."}\n\nPending clarifications:\n${questionTexts || "None specified"}`;

        store.setMessages((prev) => [
          ...prev.map((message) =>
            message.isPendingClarifications
              ? { ...message, isPendingClarifications: false }
              : message,
          ),
          {
            id: `audit-failed-${Date.now()}`,
            role: "assistant",
            content: warningContent,
            timestamp: nowTime(),
            isPendingClarifications: questions.length > 0,
            auditResultSnapshot: receivedAudit,
          },
        ]);

        store.setSyncStatus(
          auditCouldNotComplete
            ? "Audit could not complete. Please retry."
            : verdict === "fail"
              ? "Audit failed: mandatory blockers found."
              : "Audit needs clarification.",
        );
      } else {
        // AUDIT 5.4 — Success branch: a positive verdict bubble (no clarification
        //            form). The audit is still only STAGED — the user's Save in the
        //            ConfirmationPanel is what persists the verdict (CONFIRM 3.3).
        const successContent = `${verdict === "pass_with_warnings" ? "Requirements audit passed with warnings." : "Requirements audit passed."}\n${contextText}\n\n${findingTexts || "All clearly applicable mandatory controls are covered."}`;
        store.setMessages((prev) => [
          ...prev,
          {
            id: `audit-passed-${Date.now()}`,
            role: "assistant",
            content: successContent,
            timestamp: nowTime(),
          },
        ]);

        store.setSyncStatus(
          verdict === "pass_with_warnings"
            ? "Passed with non-blocking warnings."
            : "Audit passed.",
        );
      }

      store.setAuditResult(receivedAudit);

      if (receivedAudit.clarification_questions) {
        const initialAnswers: Record<string, string> = {};
        receivedAudit.clarification_questions.forEach((q, idx) => {
          initialAnswers[questionAnswerKey(q, idx)] = "";
        });
        store.setClarificationAnswers(initialAnswers);
      }
    } catch (err) {
      if (isAbortError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `stopped-${Date.now()}`,
            role: "assistant",
            content:
              "Stopped.\nYou cancelled this request. The audit was terminated and no validation verdict was applied.",
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Validation stopped by user.");
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `rate-limited-${Date.now()}`,
            role: "assistant",
            content: rateLimitedContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Rate limited. Please wait before continuing.");
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `payload-too-large-${Date.now()}`,
            role: "assistant",
            content: payloadTooLargeContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus(
          "Audit skipped: project exceeds the LLM context budget.",
        );
        return;
      }
      if (isUnauthorizedError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `audit-auth-expired-${Date.now()}`,
            role: "assistant",
            content:
              "The audit job is still stored, but your sign-in session expired while checking its result. Sign in again, then click Validate Requirements to resume this same job.",
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Sign-in expired. Please sign in again.");
        return;
      }
      const detail = extractErrorDetail(err);
      handleError(detail ?? "Compliance audit did not complete.", err);
      store.setMessages((prev) => [
        ...prev,
        {
          id: `audit-error-${Date.now()}`,
          role: "assistant",
          content: `Audit failed. No validation was saved.\n\n${detail ?? `The audit could not complete against the requirement engine (${(err as Error)?.message || err}).`}\n\nNo validation verdict was applied and no requirements were changed.`,
          timestamp: nowTime(),
        },
      ]);
      store.setSyncStatus("Audit failed. No changes were saved.");
    } finally {
      cancellation.finishRequest(controller);
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
      if (deps.agentActionsBlockedReason) {
        store.setSyncStatus(deps.agentActionsBlockedReason);
        return;
      }
      // Nothing to compile yet — mirror the disabled button instead of asking
      // the architect agent to build a PRD from an empty project.
      store.setSyncStatus(
        "Nothing to compile yet. Add requirements or upload a knowledge document.",
      );
      return;
    }
    const projectId = deps.projectId;
    store.setIsLoading(true);
    store.setIsProcessing(true);
    store.setSyncStatus("Compiling enterprise PRD document...");
    store.setCurrentAgentNode("architect_node");

    const controller = cancellation.beginRequest();

    try {
      const request = {
        project_id: projectId,
        raw_input: "",
        // ARCHITECT 1.2 — The on-demand trigger: empty raw_input + target_agent
        //            "architect" is exactly why the pipeline bypasses the GENERAL_CHAT
        //            short-circuit (CHAT 2.3.0); on the backend it enters ROUTING 1.2 →
        //            architect_node. `version_history_summaries` is the real digest
        //            (1.3 helper), which is what makes the reuse guard (2.2) and the
        //            version-history table in the template meaningful.
        target_agent: "architect",
        client_persists_timeline: true,
        // An explicit Generate PRD click must rebuild the document from the
        // persisted user-story records. This applies template-rendering
        // improvements even when no requirement fields have changed.
        force_prd_rebuild: true,
        structured_requirements: toGatheredRequirementsPayload(
          store.structuredRequirements,
        ),
        current_version: deps.currentVersion,
        version_history_summaries: buildVersionHistorySummaries(),
      };
      const data = await runBackgroundAgentJob<ProcessRequirementsResponse>({
        projectId,
        jobType: "architect",
        request,
        signal: controller.signal,
        onJobId: cancellation.setBackgroundJob,
        onProgress: (stage) =>
          store.setSyncStatus(`Compiling enterprise PRD document… ${stage}`),
      });

      // ARCHITECT 1.5 — The generated document is preview-only. Keep the saved
      // PRD/diagram untouched and expose the draft through the same durable
      // pending-action channel used by requirement review. Approval is the
      // only operation that may create a PRD version.
      const generatedPrd = data.prd_markdown || "";
      if (!data.pending_action_id || !generatedPrd) {
        throw new Error("The architect did not return a reviewable PRD preview.");
      }
      const preservedSections = data.skipped_locked_prd_sections || [];
      // Re-read the server-owned draft instead of reconstructing it locally;
      // this carries the exact source revision and next ledger version used by
      // the approval conflict checks.
      try {
        deps.setPendingActions(await api.listPendingActions(projectId));
      } catch {
        // The generation itself succeeded and the durable action exists. Keep
        // the preview usable even if this secondary refresh briefly fails;
        // approval still reads and validates the server-owned action by id.
        deps.setPendingActions((prev) => [
          ...prev.filter((action) => action.action_type !== "GENERATE_PRD"),
          {
            id: data.pending_action_id!,
            project_id: projectId,
            action_type: "GENERATE_PRD",
            original_user_message: "Generate PRD",
            workflow_stage: "PRD_PREVIEW",
            proposed_changes: {
              generated_prd: generatedPrd,
              preserved_sections: preservedSections,
            },
          },
        ]);
      }

      store.setMessages((prev) => [
        ...prev.filter((message) => !isLegacyPrdPreviewMessage(message.content)),
        {
          id: `prd-generated-${Date.now()}`,
          role: "assistant",
          content: preservedSections.length > 0
            ? `PRD draft generated. It has not been saved yet. Review it in the preview panel, then select Save PRD or Discard preview.\n\nLocked sections: ${preservedSections.join(", ")}.\nNo changes were made to these locked sections; their existing content was preserved exactly.`
            : "PRD draft generated. It has not been saved yet. Review it in the preview panel, then select Save PRD or Discard preview.",
          timestamp: nowTime(),
        },
      ]);

      store.setSyncStatus(
        preservedSections.length > 0
          ? `Draft preview ready. It has not been saved. Protected sections preserved: ${preservedSections.join(", ")}`
          : "Draft preview ready. It has not been saved.",
      );
      deps.setActiveTab("prd"); // switch tab automatically to PRD
    } catch (err) {
      // ARCHITECT 1.6 — Abort branch (Stop pressed): the generation was terminated
      //            server-side by the CANCELLATION flow, so no document was produced.
      if (isAbortError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `stopped-${Date.now()}`,
            role: "assistant",
            content:
              "Stopped.\nYou cancelled this request. PRD generation was terminated server-side; no document was produced.",
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Generation stopped by user.");
        return;
      }
      if (isRateLimitError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `rate-limited-${Date.now()}`,
            role: "assistant",
            content: rateLimitedContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus("Rate limited. Please wait before continuing.");
        return;
      }
      if (isPayloadTooLargeError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `payload-too-large-${Date.now()}`,
            role: "assistant",
            content: payloadTooLargeContent(err),
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus(
          "PRD generation blocked: project exceeds the LLM context budget.",
        );
        return;
      }
      if (isUnauthorizedError(err)) {
        store.setMessages((prev) => [
          ...prev,
          {
            id: `prd-auth-expired-${Date.now()}`,
            role: "assistant",
            content:
              "The PRD job is still stored, but your sign-in session expired while checking it. Sign in again and click Generate PRD to resume the same job.",
            timestamp: nowTime(),
          },
        ]);
        store.setSyncStatus(
          "Sign-in expired. The PRD job can be resumed after signing in.",
        );
        return;
      }
      handleError("PRD generation did not complete.", err);
      store.setMessages((prev) => [
        ...prev,
        {
          id: `prd-error-${Date.now()}`,
          role: "assistant",
          content: `PRD generation failed. No document was saved.\n\nThe document could not be generated (${(err as Error)?.message || err}).\n\nNo PRD or diagram was produced.`,
          timestamp: nowTime(),
        },
      ]);
      store.setSyncStatus("PRD generation failed. No document saved.");
    } finally {
      cancellation.finishRequest(controller);
      store.setIsLoading(false);
      store.setIsProcessing(false);
      store.setCurrentAgentNode(null);
    }
  };

  // Stop button — terminates generation on BOTH sides: asks the backend to
  // hard-cancel the running workflow task (best-effort), then aborts the local
  // axios request so the UI unlocks immediately instead of waiting for the LLM
  // workflow to finish.
  const handleStopGeneration = cancellation.stop;

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
    if (clarificationSubmitInFlightRef.current) return;
    const submittedAnswers = Object.fromEntries(
      Object.entries(store.clarificationAnswers)
        .map(([key, value]) => [key, value.trim()])
        .filter(([, value]) => value.length > 0),
    );
    if (Object.keys(submittedAnswers).length === 0) {
      store.setSyncStatus("Enter at least one answer before saving.");
      return;
    }

    try {
      clarificationSubmitInFlightRef.current = true;
      // Answer submission has its own Gatherer handoff. Expose it through the
      // shared processing state so the inline form cannot look inert or submit
      // the same answers twice while LM Studio is working.
      store.setIsProcessing(true);
      store.setCurrentAgentNode("gatherer_node");
      store.setSyncStatus("Submitting clarifications...");
      // CLARIFY 1.5 — API boundary: POST /api/clarification/submit (CLARIFY 2.1 /
      //             3.0) carrying the position-keyed answers (1.4). A failure leaves
      //             the typed answers in place so the user can retry.
      const res = await api.submitClarifications(
        deps.projectId ?? "",
        submittedAnswers,
      );
      if (res) {
        const refreshedQuestions = res.clarification_questions || [];
        const unresolvedQuestions = refreshedQuestions.filter(
          (question) => !question.is_resolved,
        );
        const remainingAnswers: Record<string, string> = {};
        unresolvedQuestions.forEach((question, index) => {
          remainingAnswers[questionAnswerKey(question, index)] = "";
        });
        store.setClarificationAnswers(remainingAnswers);
        // Update only the newest clarification card in place. Answered questions
        // remain read-only for review, while unanswered questions stay editable.
        store.setMessages((prev) => {
          let ownerIndex = -1;
          for (let index = prev.length - 1; index >= 0; index -= 1) {
            if (prev[index].isPendingClarifications) {
              ownerIndex = index;
              break;
            }
          }
          return prev.map((message, index) => {
            if (!message.isPendingClarifications) return message;
            if (index !== ownerIndex) {
              return { ...message, isPendingClarifications: false };
            }
            return {
              ...message,
              isPendingClarifications: refreshedQuestions.length > 0,
              auditResultSnapshot: message.auditResultSnapshot
                ? {
                    ...message.auditResultSnapshot,
                    clarification_questions: refreshedQuestions,
                  }
                : message.auditResultSnapshot,
            };
          });
        });
        if (res.current_workflow_state) {
          store.setCurrentAgentNode(res.current_workflow_state);
        }
        if (res.clarification_questions) {
          store.setAuditResult((prev) => ({
            ...prev,
            is_valid: res.validation_status === "valid",
            clarification_questions: res.clarification_questions,
          }));
        }
        if (deps.projectId) await deps.loadProjectState(deps.projectId);
        if (res.pending_action_id) {
          deps.setPendingActions((prev) => {
            const unrelated = prev.filter(
              (action) =>
                action.workflow_stage !== "clarification_gatherer_review",
            );
            return [
              ...unrelated,
              {
                id: res.pending_action_id!,
                project_id: deps.projectId ?? "",
                action_type: "MERGE",
                status: "WAITING_CONFIRMATION",
                proposed_changes: {},
                original_user_message: "Clarification answers",
                workflow_stage: "clarification_gatherer_review",
              },
            ];
          });
        } else {
          // The server discards stale clarification previews when the latest
          // answers produce no material requirement change.
          deps.setPendingActions((prev) =>
            prev.filter(
              (action) =>
                action.workflow_stage !== "clarification_gatherer_review",
            ),
          );
        }
        const classifications = res.clarification_answer_classifications || [];
        const classificationSummary = classifications.length
          ? classifications
              .map((item) => `${item.question_id}: ${item.classification}`)
              .join(", ")
          : "No requirement change was proposed.";
        store.setMessages((prev) => [
          ...prev,
          {
            id: `clarification-gatherer-${Date.now()}`,
            role: "gatherer",
            content: res.proposal_generation_warning
              ? `Clarification answers were saved as audit evidence.\n\n${res.proposal_generation_warning}`
              : res.proposal_no_change_reason
                ? res.proposal_no_change_reason
                : `Clarification answers were sent to the Gatherer. A combined requirement-change proposal is ready for review and confirmation.\n\nClassifications: ${classificationSummary}`,
            timestamp: nowTime(),
          },
        ]);
        if (res.proposal_generation_warning) {
          store.setSyncStatus(res.proposal_generation_warning);
        } else if (res.proposal_no_change_reason) {
          store.setSyncStatus(
            "Answers saved. No requirement changes were proposed.",
          );
        } else if (unresolvedQuestions.length > 0) {
          store.setSyncStatus(
            `${unresolvedQuestions.length} clarification question(s) remain open. Review the combined proposed changes.`,
          );
        } else {
          store.setSyncStatus(
            "Answers saved. Review and confirm the proposed requirement changes; validation runs only when you click Validate Requirements.",
          );
        }
      }
    } catch (err) {
      handleError("Failed to submit clarifications.", err);
      store.setSyncStatus("Failed to submit clarifications");
    } finally {
      clarificationSubmitInFlightRef.current = false;
      store.setIsProcessing(false);
      store.setCurrentAgentNode(null);
    }
  };

  // Stable persisted question IDs are used when available. The q-<index>
  // fallback preserves compatibility for newly staged, not-yet-persisted audits.
  const handleUpdateAnswerValue = (key: string, value: string) => {
    store.setClarificationAnswers((prev) => ({
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

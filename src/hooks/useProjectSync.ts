// useProjectSync — project-state loading + real-time SSE sync, extracted from
// the former useProjectState god-hook. Writes through the shared
// `RequirementStore` and the typed `api` client.
import { useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api/client";
import type {
  PendingActionPayload,
  RequirementStatePayload,
} from "../api/types";
import {
  epicNameFromRequirements,
  toAuditResult,
  toChatMessages,
  toLockMaps,
  toRequirementItems,
  toStructuredRequirements,
  toUserStories,
} from "../api/transforms";
import { getSafeSectionContent } from "../utils/markdown";
import { handleWarning } from "../components/Toast";
import { readStoredToken } from "./useAuth";
import { getPrdTemplateMarkdown } from "./useRequirementStore";
import type { RequirementStore } from "./useRequirementStore";
import type { WorkspaceTab } from "./useWorkspaceUi";

export interface UseProjectSyncResult {
  loadProjectState: (
    projId: string,
    retries?: number,
    delay?: number,
  ) => Promise<void>;
}

/**
 * Optional SSE callbacks. The UI layer uses these to show LIVE extraction chunk
 * progress (chunk X/N) instead of a full-state refresh during long runs.
 */
export interface ProjectSyncOptions {
  onDocumentExtractionStarted?: (data: Record<string, unknown>) => void;
  onDocumentExtractionProgress?: (data: Record<string, unknown>) => void;
}

export function useProjectSync(
  store: RequirementStore,
  projectId: string | null,
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>,
  setActiveTab: (tab: WorkspaceTab) => void,
  options?: ProjectSyncOptions,
): UseProjectSyncResult {
  // Live refs so the SSE handler always reads the freshest values without
  // having to reconnect the EventSource every time these change.
  const prdMarkdownRef = useRef(store.prdMarkdown);
  const mermaidDiagramRef = useRef(store.mermaidDiagram);
  const currentVersionRef = useRef(store.currentVersion);
  const editingSectionIdRef = useRef(store.editingSectionId);
  const activeProjectIdRef = useRef(projectId);
  activeProjectIdRef.current = projectId;
  useEffect(() => {
    prdMarkdownRef.current = store.prdMarkdown;
    mermaidDiagramRef.current = store.mermaidDiagram;
    currentVersionRef.current = store.currentVersion;
    editingSectionIdRef.current = store.editingSectionId;
  });

  // Preview catch-up bookkeeping — the PRD preview must ALWAYS end up on the
  // latest stored version. A document that arrives while the preview is on
  // hold (the user is editing a section, or a freshly generated draft is
  // staged for confirmation) is never dropped: the flag records "a newer
  // server document arrived during the hold" and the catch-up effect below
  // re-fetches and applies it as soon as the hold clears.
  const prdSyncPendingRef = useRef(false);
  const prdSyncHoldRef = useRef(false);
  // Points at the SSE effect's full-state refresh so non-SSE code paths (an
  // edit finished, the hold cleared) can pull the latest document on demand.
  const refreshFromServerRef = useRef<
    ((force?: boolean) => Promise<void>) | null
  >(null);

  // Apply queued PRD updates the moment the hold clears. The hold flags only
  // change through renders, so without this the latest version would sit until
  // an unrelated SSE event happened to arrive.
  useEffect(() => {
    if (editingSectionIdRef.current !== null || prdSyncHoldRef.current) return;
    if (!prdSyncPendingRef.current) return;
    prdSyncPendingRef.current = false;
    void refreshFromServerRef.current?.(true);
  });

  // PROJECT 2.4 — Requirement-state load (opening a project). Layer chain:
  //              2.1 trigger → 2.2 (api.getProjectState) → route 2.3 →
  //              repository 2.3.2 / init branch 2.3.3 → 2.4 store mapping.
  //              Reused verbatim by the SSE full refresh and the PRD preview
  //              catch-up, so PROJECT 2.x is also the read path of EVENTS.
  const loadProjectState = async (
    projId: string,
    retries = 5,
    delay = 1000,
  ) => {
    let retryScheduled = false;
    // Fresh load — drop any queued PRD update / hold left over from a previous
    // project so the catch-up logic can never leak across projects.
    prdSyncPendingRef.current = false;
    prdSyncHoldRef.current = false;
    store.setIsLoading(true);
    store.setSyncStatus("Loading project state from Supabase...");

    // Clear lock states when loading a new project to prevent state pollution
    store.setLockedArtifacts({});
    store.setLockedRequirements({});

    try {
      // PERF (project switch) — fire the independent reads BEFORE awaiting the
      // main state fetch so they overlap on the wire instead of queueing behind
      // it. Previously pending actions were awaited after the state response and
      // the section-lock/version-ledger reads only started after BOTH, turning
      // every project click into a strictly sequential 4-request chain (each also
      // paying the ownership-check round trip to the remote database).
      const pendingActionsPromise = api
        .listPendingActions(projId)
        .then((actions) => actions || [])
        .catch((err) => {
          handleWarning("Could not load pending actions.", err);
          return null; // null = failed; distinguish from an intentionally empty list
        });
      // Load per-part PRD section lock/ownership metadata (non-blocking).
      store.loadSectionLocks(projId);
      // Load the immutable PRD version ledger (AI + manual snapshots)
      store.loadVersionHistory(projId);

      // PROJECT 2.2 — API boundary → GET /api/project/{id} (route PROJECT 2.3).
      const payload = await api.getProjectState(projId);
      if (activeProjectIdRef.current !== projId) return;
      if (payload) {
        // PROJECT 2.4 — Response applied to the UI store: structured
        //              requirements/stories, lock maps, version number, audit
        //              result, PRD markdown, Mermaid diagram, workflow node and
        //              the embedded conversation history (via api/transforms).
        // Always load from Supabase — no frontend cache fallback.
        store.setStructuredRequirements(toStructuredRequirements(payload));

        // Update locked artifacts from requirements - ONLY use backend data for new project
        const { lockedArtifacts, lockedRequirements } = toLockMaps(
          payload.requirements,
        );
        store.setLockedArtifacts(lockedArtifacts);
        store.setLockedRequirements(lockedRequirements);

        store.setCurrentVersion(payload.version_number || 1);
        store.setAuditResult(toAuditResult(payload));
        const loadedPrd = getSafeSectionContent(payload.generated_prd || "");
        store.setPrdMarkdown(loadedPrd);
        store.setMermaidDiagram(payload.generated_diagrams || "");
        store.setDiagramGeneratedAt(payload.diagram_generated_at || null);
        store.setCurrentAgentNode(payload.current_workflow_state || null);
        setActiveTab("prd");

        // No generated PRD yet -> show the official Krungsri Nimble template
        // skeleton in the right panel; Generate PRD fills it in afterwards.
        if (!loadedPrd) {
          const tpl = await getPrdTemplateMarkdown();
          if (tpl) store.setPrdMarkdown((prev) => prev || tpl);
        }

        // Load conversation history — always from Supabase, in chronological order
        store.setMessages(toChatMessages(payload));

        // CONFIRM 1.7 — Pending-action load (initial project open, non-blocking): the
        //              first paint already knows whether a draft is staged, so no extra
        //              state fetch is needed. The SSE path refreshes it again (1.7.1).
        //              The request was STARTED at the top of this function (PERF note)
        //              so it already ran concurrently with the state fetch — only its
        //              result is awaited here. A failure is already reported by the
        //              .catch above (returns null) and must not clear staged drafts.
        // Pending actions are secondary UI metadata. Do not block the core
        // project payload (and therefore every workspace tab) while this
        // independent request is slow; still discard stale project responses.
        void pendingActionsPromise.then((actions) => {
          if (activeProjectIdRef.current === projId && actions !== null) {
            setPendingActions(actions);
          }
        });

        store.setSyncStatus("Synced with Supabase Cloud");
      }
    } catch (err) {
      if (activeProjectIdRef.current !== projId) return;
      if (retries > 0 && activeProjectIdRef.current === projId) {
        retryScheduled = true;
        // PROJECT 2.5 — Error branch: bounded retry (5 × 1 s) before degrading
        // to the "working locally" warning. The server stays the only source of
        // truth — there is deliberately no offline cache fallback.
        setTimeout(() => {
          if (activeProjectIdRef.current === projId) {
            void loadProjectState(projId, retries - 1, delay);
          }
        }, delay);
      } else {
        handleWarning(
          "Failed to load project state from the server. Working locally.",
          err,
        );
        store.setSyncStatus("Failed to sync with Supabase. Working locally.");
      }
    } finally {
      if (activeProjectIdRef.current === projId && !retryScheduled) {
        store.setIsLoading(false);
      }
    }
  };

  // Load project state whenever the selected project changes.
  useEffect(() => {
    if (projectId && projectId !== "null") {
      // PROJECT 2.1 — Trigger: the selected project changed (PROJECT 1.5
      //              auto-select, sidebar row click, or dashboard row open)
      //              → PROJECT 2.2. Effect depends on projectId only.
      loadProjectState(projectId);
    }
    // `loadProjectState` intentionally omitted: it is recreated every render and
    // we only want to (re)load when the selected project id actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Real-time synchronization via Server-Sent Events (SSE).
  // Replaces the previous 3s polling: the backend pushes a change event through
  // the `/api/project/{id}/sse` stream and we refresh state once per real change.
  useEffect(() => {
    if (!projectId || projectId === "null") return;

    let isSubscribed = true;
    let refreshInFlight = false;
    // Fresh subscription — no queued PRD update or hold from a previous project.
    prdSyncPendingRef.current = false;
    prdSyncHoldRef.current = false;

    const refreshFromServer = async (force = false) => {
      if (refreshInFlight && !force) return;
      refreshInFlight = true;
      try {
        const payload: RequirementStatePayload =
          await api.getProjectState(projectId);
        if (!isSubscribed) return;

        if (payload) {
          // 1. Sync structured requirements & user stories if different
          if (payload.requirements && payload.user_stories) {
            store.setStructuredRequirements((prev) => {
              const currentStoriesStr = JSON.stringify(prev.user_stories);
              const newStoriesStr = JSON.stringify(payload.user_stories);

              // Build requirements list preserving lock info
              const newRequirements = toRequirementItems(payload.requirements);

              const currentReqsStr = JSON.stringify(prev.requirements);
              const newReqsStr = JSON.stringify(newRequirements);
              const epicName =
                payload.epic_name ||
                epicNameFromRequirements(payload.requirements, "");

              // Always update if stories/epic/requirements changed, OR if lock states changed
              const hasLockChanges =
                newRequirements && prev.requirements
                  ? newRequirements.some((nr, i) => {
                      const pr = prev.requirements?.[i];
                      return (
                        pr &&
                        (nr.is_locked !== pr.is_locked ||
                          nr.locked_by !== pr.locked_by ||
                          nr.locked_at !== pr.locked_at)
                      );
                    })
                  : false;

              if (
                currentStoriesStr !== newStoriesStr ||
                prev.epic_name !== epicName ||
                currentReqsStr !== newReqsStr ||
                hasLockChanges
              ) {
                return {
                  epic_name: epicName,
                  version: payload.version_number || 1,
                  user_stories: toUserStories(payload.user_stories),
                  requirements: newRequirements || prev.requirements,
                };
              }
              return prev;
            });
          }

          // 2. Sync version number if changed
          if (
            payload.version_number !== undefined &&
            payload.version_number !== currentVersionRef.current
          ) {
            store.setCurrentVersion(payload.version_number);
          }

          // 3. Sync audit validation result
          if (payload.validation_status) {
            store.setAuditResult((prev) => {
              const currentQsStr = JSON.stringify(
                prev.clarification_questions || [],
              );
              const newQsStr = JSON.stringify(
                payload.clarification_questions || [],
              );
              const currentFindingsStr = JSON.stringify(prev.findings || []);
              const newFindingsStr = JSON.stringify(
                payload.audit_findings || [],
              );
              const isPassed = payload.validation_status === "valid";

              const newPassed = payload.passed_checks || [];
              const newFailed = payload.failed_checks || [];

              if (
                prev.is_valid !== isPassed ||
                currentQsStr !== newQsStr ||
                currentFindingsStr !== newFindingsStr ||
                prev.checklist_version !== payload.audit_checklist_version
              ) {
                return {
                  is_valid: isPassed,
                  audit_version_reviewed: payload.version_number || 1,
                  clarification_questions:
                    payload.clarification_questions || [],
                  passed_checks: newPassed,
                  failed_checks: newFailed,
                  findings: payload.audit_findings || [],
                  source_references: payload.audit_source_references || [],
                  verdict:
                    payload.audit_verdict ||
                    (isPassed ? "pass" : "needs_clarification"),
                  project_context: payload.audit_project_context,
                  checklist_id: payload.audit_checklist_id,
                  checklist_version: payload.audit_checklist_version,
                };
              }
              return prev;
            });
          }

          // CONFIRM 1.7.1 — SSE-driven refresh: pending actions are re-read FIRST
          //               because the PRD sync below needs to know whether a freshly
          //               generated document is still staged for confirmation.
          // 4. Sync pending actions FIRST — the PRD sync below must know
          // whether a freshly generated document is still staged for
          // confirmation (see the hold computation).
          let pendingActionsList: PendingActionPayload[] = [];
          try {
            pendingActionsList =
              (await api.listPendingActions(projectId)) || [];
            if (isSubscribed) setPendingActions(pendingActionsList);
          } catch (err) {
            handleWarning("Could not load pending actions.", err);
          }

          // A staged MERGE whose PRD draft differs from the persisted document
          // means a freshly generated version is awaiting confirmation: the
          // persisted copy is the PREVIOUS document, so hold the preview on the
          // newer staged draft instead of reverting it. The hold clears when
          // the action is confirmed or discarded (the pending action then
          // disappears from the list).
          prdSyncHoldRef.current = pendingActionsList.some((action) => {
            if (action.action_type !== "MERGE") return false;
            const staged = (action.proposed_changes ?? {})["generated_prd"];
            return (
              typeof staged === "string" &&
              staged !== "" &&
              staged !== payload.generated_prd
            );
          });

          // 5–6. Apply the PRD and diagram as one artifact snapshot. Previously
          // the PRD waited during section editing / staged-preview holds while
          // the diagram updated immediately, which could display two different
          // revisions. Empty server values are authoritative too, so a removed
          // diagram cannot remain stale in the UI.
          const safeGeneratedPRD = getSafeSectionContent(
            payload.generated_prd || "",
          );
          const diagram = payload.generated_diagrams || "";
          const artifactsChanged =
            safeGeneratedPRD !== prdMarkdownRef.current ||
            diagram !== mermaidDiagramRef.current;
          const artifactsHeld =
            editingSectionIdRef.current !== null || prdSyncHoldRef.current;

          if (artifactsChanged && artifactsHeld) {
            prdSyncPendingRef.current = true;
          } else if (!artifactsHeld) {
            prdSyncPendingRef.current = false;
            // An empty persisted PRD means the project has no generated
            // document. Keep the official blank skeleton as the visual
            // fallback, while clearing the paired diagram from the same state.
            const nextPrd =
              safeGeneratedPRD || (await getPrdTemplateMarkdown());
            if (!isSubscribed || activeProjectIdRef.current !== projectId)
              return;
            if (nextPrd !== prdMarkdownRef.current) {
              store.setPrdMarkdown(nextPrd);
            }
            if (diagram !== mermaidDiagramRef.current) {
              store.setMermaidDiagram(diagram);
            }
            store.setDiagramGeneratedAt(payload.diagram_generated_at || null);
          }

          // 7. Sync active agent node
          const workflowState = payload.current_workflow_state;
          if (workflowState) {
            store.setCurrentAgentNode((prev) =>
              prev !== workflowState ? workflowState : prev,
            );
          }

          // 8. Sync per-part PRD section lock/ownership metadata (non-blocking)
          if (isSubscribed) store.loadSectionLocks(projectId);
          // 9. Sync the immutable PRD version ledger (non-blocking)
          if (isSubscribed) store.loadVersionHistory(projectId);

          store.setSyncStatus("Synced via live updates");
        }
      } catch (err) {
        handleWarning("Live sync encountered an error and will retry.", err);
      } finally {
        refreshInFlight = false;
      }
    };

    // Expose the refresh to the preview catch-up effect, which runs outside
    // this subscription (an edit finishing, the staged-draft hold clearing).
    refreshFromServerRef.current = refreshFromServer;

    // Open the Server-Sent Events stream. The backend publishes a change event
    // whenever the project state is mutated, so no periodic polling is needed.
    // EventSource cannot send an Authorization header, so the JWT travels as a
    // `?token=` query parameter — the backend's SSE dependency accepts either
    // the Bearer header or this query token, then enforces project ownership.
    const token = readStoredToken();
    const sseUrl = token
      ? `/api/project/${projectId}/sse?token=${encodeURIComponent(token)}`
      : `/api/project/${projectId}/sse`;
    const source = new EventSource(sseUrl);

    // Debounce window for event-triggered refreshes. A multi-agent run publishes
    // bursts of events (per-step saves, PRD section updates, progress frames) in
    // a few hundred milliseconds; coalescing them into ONE full-state fetch
    // keeps the UI live without re-downloading the whole project payload (full
    // PRD markdown + conversation history) once per event.
    const REFRESH_DEBOUNCE_MS = 350;
    let refreshTimer: number | undefined;

    const scheduleRefresh = () => {
      if (!isSubscribed) return;
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        void refreshFromServer();
      }, REFRESH_DEBOUNCE_MS);
    };

    source.onopen = () => {
      if (isSubscribed) store.setSyncStatus("Live updates connected");
    };
    source.onmessage = (event) => {
      if (!isSubscribed) return;
      try {
        const message = JSON.parse(event.data);
        if (!message || !message.event) return;

        // The stream's first frame is a `connected` handshake, not a state
        // change — `loadProjectState` already fetched the full state when the
        // project opened, so it must NOT trigger a redundant re-download.
        if (message.event === "connected") return;

        // High-frequency document-extraction frames: forward them to the
        // DocumentLibrary so users see live chunk progress (chunk X/N) on big
        // documents, and do NOT trigger a full project-state GET. A many-chunk
        // extraction would otherwise fire one heavy state download (PRD markdown
        // + chat history) per chunk while the backend is already busy serving
        // the local LLM.
        if (message.event === "document_extraction_started") {
          options?.onDocumentExtractionStarted?.(message.data);
          return;
        }
        if (message.event === "document_extraction_progress") {
          options?.onDocumentExtractionProgress?.(message.data);
          return;
        }
        scheduleRefresh();
      } catch (err) {
        // Ignore malformed or heartbeat payloads.
      }
    };
    source.onerror = () => {
      // EventSource reconnects automatically; surface a subtle status while down.
      if (isSubscribed) store.setSyncStatus("Live updates reconnecting...");
    };

    return () => {
      isSubscribed = false;
      refreshFromServerRef.current = null;
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      source.close();
    };
    // `store` is recreated every render; only re-subscribe when the project changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  return { loadProjectState };
}

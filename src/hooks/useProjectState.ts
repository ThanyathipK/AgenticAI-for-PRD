// useProjectState — slim composition root.
//
// The former 1,421-line "god-hook" has been decomposed into focused modules,
// each owning one concern and flowing through the typed `src/api` client so no
// payload is ever handled as `any`:
//
//   - src/api/{types,client,transforms}  -> typed backend contract + conversions
//   - useProjects                        -> project list, selection, CRUD, pending actions
//   - useWorkspaceUi                     -> split pane, tabs, diagram/version UI state
//   - useRequirementStore                -> requirement/PRD domain state + typed setters
//   - useArtifactLocks                   -> optimistic lock/unlock actions
//   - useProjectSync                     -> loadProjectState + SSE live sync
//   - useChat                            -> conversational / multi-agent handlers
//   - useLmStudioHealth                  -> LLM connectivity health polling (unchanged)
//
// This hook keeps exposing the same `ProjectState` interface so the presentational
// components (Dashboard, ChatPanel, PRDEditor, ArchitectureFlows, VersionHistory)
// are unaffected.
import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type {
  AuditResult,
  ChatMessage,
  PRDSection,
  PrdSectionLockState,
  RequirementItem,
  RequirementLockState,
  StructuredRequirements,
  VersionHistory,
} from "../components/types";
import type {
  PendingActionPayload,
  ProjectStatus,
  ProjectSummary,
} from "../api/types";
import { api, detailFromBlobError } from "../api/client";
import { handleError } from "../components/Toast";
import type {
  UseDocumentsResult,
  DocumentExtractionProgress,
} from "./useDocuments";
import type { CreateProjectOptions, CreateProjectResult } from "./useProjects";
import type { AppView, WorkspaceTab } from "./useWorkspaceUi";
import { useArtifactLocks } from "./useArtifactLocks";
import { useChat } from "./useChat";
import { useDocuments } from "./useDocuments";
import { useLmStudioHealth } from "./useLmStudioHealth";
import { useProjectSync } from "./useProjectSync";
import { useProjects } from "./useProjects";
import { useRequirementStore } from "./useRequirementStore";
import { useWorkspaceUi } from "./useWorkspaceUi";

export interface ProjectState {
  // Project/chrome state
  projectId: string | null;
  setProjectId: Dispatch<SetStateAction<string | null>>;
  projects: ProjectSummary[];
  setProjects: Dispatch<SetStateAction<ProjectSummary[]>>;
  currentVersion: number;
  pendingActions: PendingActionPayload[];
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>;
  historyCollapsed: boolean;
  setHistoryCollapsed: Dispatch<SetStateAction<boolean>>;
  projectContextMenu: { projectId: string; x: number; y: number } | null;
  setProjectContextMenu: Dispatch<
    SetStateAction<{ projectId: string; x: number; y: number } | null>
  >;
  renameProjectId: string | null;
  setRenameProjectId: Dispatch<SetStateAction<string | null>>;
  renameProjectName: string;
  setRenameProjectName: Dispatch<SetStateAction<string>>;
  handleRenameProject: (projectId: string, newName: string) => Promise<void>;
  /** Deletes a project; resolves to true on success. */
  handleDeleteProject: (projIdToDelete: string) => Promise<boolean>;
  /**
   * Creates a project with the given name. Resolves to null on success (the
   * caller may close its dialog), or to a user-facing error message — e.g. a
   * duplicate-name conflict from the backend — for inline display.
   */
  handleCreateProject: (
    name: string,
    options?: CreateProjectOptions,
  ) => Promise<CreateProjectResult>;
  handleTogglePin: (projectId: string) => Promise<void>;
  /** Flags/unflags a project (dashboard ★ marker) — independent of pinning. */
  handleToggleFlag: (projectId: string) => Promise<void>;
  /** Sets the project's user-editable workflow status (dashboard table), optimistically. */
  handleUpdateProjectStatus: (
    projectId: string,
    status: ProjectStatus,
    comment?: string,
  ) => Promise<boolean>;
  /** Whether the styled "New Project" modal is shown (replaces native prompt()). */
  isCreateModalOpen: boolean;
  setIsCreateModalOpen: Dispatch<SetStateAction<boolean>>;
  /** Project awaiting delete confirmation in the styled ConfirmModal (replaces native confirm()). */
  projectPendingDelete: string | null;
  setProjectPendingDelete: Dispatch<SetStateAction<string | null>>;
  /** Sidebar search filter — case-insensitively filters visible projects by name. */
  projectSearchQuery: string;
  setProjectSearchQuery: Dispatch<SetStateAction<string>>;

  // Chat / split layout
  messages: ChatMessage[];
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  rawInput: string;
  setRawInput: Dispatch<SetStateAction<string>>;
  splitPct: number;
  setSplitPct: Dispatch<SetStateAction<number>>;
  isDraggingSplit: boolean;
  setIsDraggingSplit: Dispatch<SetStateAction<boolean>>;
  splitContainerRef: React.RefObject<HTMLDivElement | null>;
  chatEndRef: React.RefObject<HTMLDivElement | null>;
  isProcessing: boolean;
  isLoading: boolean;
  currentAgentNode: string | null;
  syncStatus: string;
  /** True when the most recent PRD generation ended before a complete preview existed. */
  prdGenerationFailed: boolean;
  /** True when PRD preview succeeded but its LLM-backed flow refresh did not. */
  prdDiagramGenerationFailed: boolean;
  clearPrdGenerationNotices: () => void;
  diagramGeneratedAt: string | null;
  // Finding #40: LM Studio connectivity health (polled via GET /api/health)
  backendOnline: boolean | null;
  lmStudioOnline: boolean | null;
  lmStudioModel: string | null;
  lmStudioModelLoaded: boolean;
  lmStudioLoadedModels: string[];
  lmStudioLatencyMs: number | null;
  lmStudioLastCheckedAt: string | null;
  lmStudioError: string | null;
  checkLmStudioHealth: () => Promise<void>;
  clarificationAnswers: Record<string, string>;
  handleUpdateAnswerValue: (key: string, value: string) => void;
  handleSubmitClarifications: (e: React.FormEvent) => Promise<void>;
  /** Optional project id is used by the new-project intake before React has re-rendered the selection. */
  handleSendMessage: (
    textToSend?: string,
    projectIdOverride?: string,
  ) => Promise<void>;
  handleValidateRequirements: () => Promise<void>;
  handleGeneratePRD: () => Promise<void>;
  /**
   * Gate shared by the agent action buttons (Validate Requirements / Generate
   * PRD): true only when the active project exists AND holds real content —
   * at least one uploaded knowledge document, user story, or requirement item.
   */
  canRunAgentActions: boolean;
  /** User-facing explanation when Validate / Generate PRD must wait. */
  agentActionsBlockedReason: string;
  /** True while a previously queued legacy/manual regeneration is running. */
  isCascadeGenerating: boolean;
  /** Aborts the in-flight agent request (Stop button). No-op when idle. */
  handleStopGeneration: () => void;
  handleDownloadDocx: () => Promise<void>;
  handlePrintPDF: () => void;
  loadProjectState: (
    projId: string,
    retries?: number,
    delay?: number,
  ) => Promise<void>;
  // Uploaded-document knowledge base + DRAFT-only extraction handlers
  documents: UseDocumentsResult;

  // PRD / requirements state
  structuredRequirements: StructuredRequirements;
  auditResult: AuditResult;
  prdMarkdown: string;
  sections: PRDSection[];
  editingSectionId: string | null;
  setEditingSectionId: Dispatch<SetStateAction<string | null>>;
  editBuffer: string;
  setEditBuffer: Dispatch<SetStateAction<string>>;
  handleSaveSection: (
    sectionId: string,
    newContent: string,
    baseContent?: string,
  ) => Promise<void>;
  lockedRequirements: Record<string, RequirementLockState>;
  handleLockRequirement: (requirementCode: string) => Promise<void>;
  handleUnlockRequirement: (requirementCode: string) => Promise<void>;
  /** Authoritative lockable requirement rows for the project (real DB ids +
   *  lock metadata) — the Requirements tab's "Requirement Lock Status" list. */
  lockableRequirements: RequirementItem[];
  /** Re-read that list from the backend (e.g. after saving a pending merge). */
  refreshLockableRequirements: () => Promise<void>;
  /** Per-part PRD section lock/ownership metadata (the 9 preview parts). */
  sectionLocks: Record<string, PrdSectionLockState>;
  /** Lock/unlock ONE PRD part — blocks edits + AI regeneration when locked. */
  handleToggleSectionLock: (sectionId: string) => Promise<void>;
  /** Hand a MANUAL (human-owned) PRD part back to AI regeneration. */
  handleHandSectionToAi: (sectionId: string) => Promise<void>;
  /** Restore target-present unlocked parts; current-only/locked parts remain.
   *  Resolves to true for any successful route response, including ledger fallback. */
  handleRestoreVersion: (versionNumber: number) => Promise<boolean>;
  loadVersionHistory: (
    projectId: string,
    options?: { append?: boolean; beforeVersion?: number; limit?: number },
  ) => Promise<number>;

  // Tabs / flows / history (document library lives inside the history tab)
  activeTab: WorkspaceTab;
  setActiveTab: (tab: WorkspaceTab) => void;
  /** Top-level view next to the project sidebar: workspace, dashboard, or help. */
  activeView: AppView;
  setActiveView: (view: AppView) => void;
  mermaidDiagram: string;
  versionHistory: VersionHistory[];
  selectedHistVersion: number;
  setSelectedHistVersion: Dispatch<SetStateAction<number>>;
  diffBaseVersion: number | null;
  setDiffBaseVersion: Dispatch<SetStateAction<number | null>>;
  diagramZoom: number;
  setDiagramZoom: Dispatch<SetStateAction<number>>;
  hoverNode: string | null;
  setHoverNode: Dispatch<SetStateAction<string | null>>;
  activePacketFlow: boolean;
  setActivePacketFlow: Dispatch<SetStateAction<boolean>>;
}

// ----------------------------------------------------------------------------
// Hook — composes the focused sub-hooks back into one `ProjectState`.
// ----------------------------------------------------------------------------

export function useProjectState(
  isAuthenticated: boolean = false,
  authUserId: string | null = null,
): ProjectState {
  // PROJECT 1.x entry (frontend) — the whole PROJECT flow group is provided by
  // useProjects here and re-exposed through this hook's public surface:
  //   AUTH 1.6 (signed in) → PROJECT 1.2 list load → PROJECT 1.5 selection.
  // The state-load half (PROJECT 2.x) is wired at PROJECT 2.1 below.
  const projectsApi = useProjects(isAuthenticated, authUserId);
  const ui = useWorkspaceUi();
  const store = useRequirementStore(projectsApi.projectId);
  const locks = useArtifactLocks(store, projectsApi.projectId);
  const [activeCascadeJobId, setActiveCascadeJobId] = useState<string | null>(
    null,
  );
  const cascadeStorageKey = projectsApi.projectId
    ? `agenticdia:active-cascade:${projectsApi.projectId}`
    : null;

  // Authentication owns the visibility boundary for every project-backed
  // state slice. `useProjects` clears the list and selected id on logout; also
  // clear the composed workspace store so the last project's chat, PRD,
  // requirements and pending confirmations cannot remain on screen.
  useEffect(() => {
    if (isAuthenticated) return;
    store.resetProjectState();
    projectsApi.setPendingActions([]);
    // Reset only when the authentication boundary changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  useEffect(() => {
    setActiveCascadeJobId(
      cascadeStorageKey ? window.localStorage.getItem(cascadeStorageKey) : null,
    );
  }, [cascadeStorageKey]);

  // SSE bridge for LIVE extraction progress. useProjectSync receives the
  // document_extraction_* frames, maps them to a DocumentExtractionProgress,
  // and forwards them into useDocuments' setter (registered below) so the
  // DocumentLibrary "chunk X/N" readout moves in real time during long runs.
  const liveExtractionProgressRef = useRef<
    (p: DocumentExtractionProgress | null) => void
  >(() => {});

  // PROJECT 2.1 — Trigger: `projectsApi.projectId` (set by PROJECT 1.5 when the
  //              list loads, or by a sidebar/dashboard click) is handed to
  //              useProjectSync, whose effect fires loadProjectState → PROJECT 2.2.
  //              The SSE half of useProjectSync belongs to the EVENTS flow; the
  //              callbacks below only forward document_extraction_* frames.
  const { loadProjectState } = useProjectSync(
    store,
    projectsApi.projectId,
    projectsApi.setPendingActions,
    ui.setActiveTab,
    {
      onDocumentExtractionStarted: (data) => {
        // Optimistically pick the mode from the measured token_count vs budget.
        const documentId = String(data?.document_id ?? "");
        if (!documentId) return;
        const tokenCount = Number(data?.token_count ?? 0);
        const budget = Number(data?.budget ?? 0);
        liveExtractionProgressRef.current({
          documentId,
          mode: tokenCount > 0 && tokenCount > budget ? "chunked" : "full",
          chunkIndex: 0,
          chunkCount: 0,
        });
      },
      onDocumentExtractionProgress: (data) => {
        const documentId = String(data?.document_id ?? "");
        const chunkIndex = Number(data?.chunk_index ?? 0);
        const chunkCount = Number(data?.chunk_count ?? 0);
        if (!documentId || chunkIndex < 1) return;
        liveExtractionProgressRef.current({
          documentId,
          mode: String(data?.mode ?? "chunked"),
          chunkIndex,
          chunkCount,
        });
      },
    },
  );
  const loadProjectStateRef = useRef(loadProjectState);
  loadProjectStateRef.current = loadProjectState;
  const lm = useLmStudioHealth();
  // Documents must be composed BEFORE useChat so the agent-action gate below
  // can consider the knowledge base when it is injected into the chat deps.
  const documents = useDocuments({
    projectId: projectsApi.projectId,
    pendingActions: projectsApi.pendingActions,
    setPendingActions: projectsApi.setPendingActions,
    setMessages: store.setMessages,
    setSyncStatus: store.setSyncStatus,
    setCurrentAgentNode: store.setCurrentAgentNode,
    registerLiveProgress: (fn) => {
      liveExtractionProgressRef.current = fn;
    },
  });

  // Resume any legacy/manual downstream refresh already recorded for this
  // project. New requirement saves do not create this automatically; the
  // explicit Generate PRD action owns generation now.
  useEffect(() => {
    const projectId = projectsApi.projectId;
    const jobId = activeCascadeJobId;
    if (!projectId || !jobId) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const job = await api.getGenerationJob(projectId, jobId);
        if (cancelled) return;
        if (["completed", "failed", "cancelled"].includes(job.status)) {
          setActiveCascadeJobId(null);
          if (cascadeStorageKey)
            window.localStorage.removeItem(cascadeStorageKey);
          store.setSyncStatus(
            job.status === "completed"
              ? "Requirements update finished. Validate and Generate PRD are available."
              : "Requirements update stopped. Validate and Generate PRD are available.",
          );
          await loadProjectStateRef.current(projectId);
          return;
        }
        store.setSyncStatus("Applying the confirmed requirements update…");
        timer = window.setTimeout(poll, 1200);
      } catch {
        if (!cancelled) {
          // A missing/expired job no longer owns the backend generation slot.
          setActiveCascadeJobId(null);
          if (cascadeStorageKey)
            window.localStorage.removeItem(cascadeStorageKey);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // Depend on stable scalar/setter members, not the reconstructed store.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeCascadeJobId,
    cascadeStorageKey,
    projectsApi.projectId,
    store.setSyncStatus,
  ]);

  // Agent-action gating (Validate Requirements / Generate PRD): a brand-new
  // project with NO knowledge documents and NO gathered requirements has
  // nothing for the Auditor/Architect agents to work with, so those actions
  // stay blocked until real content exists (uploaded docs, user stories, or
  // requirement items).
  const hasRequirementContent =
    (store.structuredRequirements.user_stories?.length ?? 0) > 0 ||
    (store.structuredRequirements.requirements?.length ?? 0) > 0;
  // Failed conversions (notably OCR-only PDFs) have no usable text and must
  // not enable Validate merely because their metadata row is visible.
  const hasKnowledgeContent = documents.documents.some(
    (document) => document.status === "processed" && document.token_count > 0,
  );
  const hasPendingRequirementUpdate = projectsApi.pendingActions.some(
    (action) => action.action_type !== "GENERATE_PRD",
  );
  const agentActionsBlockedReason = hasPendingRequirementUpdate
    ? "Save or cancel the pending requirements update first"
    : activeCascadeJobId
      ? "Please wait while the confirmed requirements update finishes"
      : "";
  const canRunAgentActions =
    Boolean(projectsApi.projectId) &&
    (hasRequirementContent || hasKnowledgeContent) &&
    !hasPendingRequirementUpdate &&
    !activeCascadeJobId;

  const chat = useChat(store, {
    projectId: projectsApi.projectId,
    projects: projectsApi.projects,
    currentVersion: store.currentVersion,
    setPendingActions: projectsApi.setPendingActions,
    setActiveTab: ui.setActiveTab,
    loadProjectState,
    canRunAgentActions,
    agentActionsBlockedReason,
  });

  // PRD-EXPORT 2.0 — Shared PDF/DOCX export coordinator. Trigger: menu 1.x.
  // It selects the project/current fallback source, calls client 3.1/3.2,
  // creates the browser download at 2.1, and decodes Blob failures at 2.2.
  // The generated PRD is LaTeX
  // (template-krungsrinimble.tex), so the finished PDF/DOCX is COMPILED
  // server-side (Tectonic / Pandoc) — the browser never re-parses it as
  // markdown, which is what previously made exports follow template.md.
  const exportCompiledFile = async (fmt: "pdf" | "docx") => {
    const pid = projectsApi.projectId;
    if (!pid) {
      store.setSyncStatus("Select a project before exporting.");
      return;
    }
    const projectName =
      projectsApi.projects.find((p) => p.id === pid)?.name ||
      "Krungsri Nimble PRD";

    store.setSyncStatus(
      fmt === "pdf"
        ? "Compiling the Krungsri Nimble LaTeX PRD to PDF (Tectonic)..."
        : "Converting the Krungsri Nimble LaTeX PRD to Word (Pandoc)...",
    );

    try {
      const download =
        fmt === "pdf"
          ? await api.exportPrdPdf(
              pid,
              store.prdMarkdown,
              store.currentVersion,
              projectName,
            )
          : await api.exportPrdDocx(
              pid,
              store.prdMarkdown,
              store.currentVersion,
              projectName,
            );

      // The server resolves the latest immutable PRD snapshot and supplies its
      // SemVer filename. This local name is only a defensive fallback.
      const now = new Date();
      const pad = (n: number) => String(n).padStart(2, "0");
      const dateStamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
      const timeStamp = `${pad(now.getHours())}${pad(now.getMinutes())}`;
      const stem = `PRD_${projectName
        .replace(/[^A-Za-z0-9._-]+/g, "_")
        .slice(0, 48)
        .replace(/^_+|_+$/g, "")}`;
      // PRD-EXPORT 2.1 — Browser download effect. Prefer the authoritative
      // Content-Disposition filename; construct a legacy fallback only when absent.
      const link = document.createElement("a");
      link.href = URL.createObjectURL(download.blob);
      const latestSemVer = store.versionHistory[0]?.semVersion;
      const fallbackVersion = latestSemVer || String(store.currentVersion);
      link.download =
        download.filename ||
        `${stem}_V${fallbackVersion}_${dateStamp}_${timeStamp}.${fmt}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(link.href);

      store.setSyncStatus(
        fmt === "pdf"
          ? "PRD PDF exported from the .tex template."
          : "PRD Word Document (.docx) exported from the .tex template.",
      );
    } catch (err) {
      // PRD-EXPORT 2.2 — Binary endpoints return error JSON inside a Blob;
      // decode it before falling back to format-specific generic messaging.
      const detail = await detailFromBlobError(err);
      if (detail) {
        handleError(detail, err);
        store.setSyncStatus(
          `Failed to generate ${fmt.toUpperCase()} document.`,
        );
      } else if (fmt === "pdf") {
        handleError("Failed to generate the PDF document.", err);
        store.setSyncStatus("Failed to generate PDF document.");
      } else {
        handleError("Failed to generate the DOCX document.", err);
        store.setSyncStatus("Failed to generate DOCX document.");
      }
    }
  };

  const handleDownloadDocx = async () => {
    await exportCompiledFile("docx");
  };

  const handlePrintPDF = async () => {
    await exportCompiledFile("pdf");
  };

  return {
    projectId: projectsApi.projectId,
    setProjectId: projectsApi.setProjectId,
    projects: projectsApi.projects,
    setProjects: projectsApi.setProjects,
    pendingActions: projectsApi.pendingActions,
    setPendingActions: projectsApi.setPendingActions,
    currentVersion: store.currentVersion,
    rawInput: chat.rawInput,
    setRawInput: chat.setRawInput,
    splitPct: ui.splitPct,
    setSplitPct: ui.setSplitPct,
    isDraggingSplit: ui.isDraggingSplit,
    setIsDraggingSplit: ui.setIsDraggingSplit,
    historyCollapsed: ui.historyCollapsed,
    setHistoryCollapsed: ui.setHistoryCollapsed,
    splitContainerRef: ui.splitContainerRef,
    messages: chat.messages,
    setMessages: chat.setMessages,
    projectContextMenu: projectsApi.projectContextMenu,
    setProjectContextMenu: projectsApi.setProjectContextMenu,
    renameProjectId: projectsApi.renameProjectId,
    setRenameProjectId: projectsApi.setRenameProjectId,
    renameProjectName: projectsApi.renameProjectName,
    setRenameProjectName: projectsApi.setRenameProjectName,
    handleRenameProject: projectsApi.handleRenameProject,
    handleDeleteProject: projectsApi.handleDeleteProject,
    handleCreateProject: projectsApi.handleCreateProject,
    handleTogglePin: projectsApi.handleTogglePin,
    handleToggleFlag: projectsApi.handleToggleFlag,
    handleUpdateProjectStatus: projectsApi.handleUpdateProjectStatus,
    isCreateModalOpen: projectsApi.isCreateModalOpen,
    setIsCreateModalOpen: projectsApi.setIsCreateModalOpen,
    projectPendingDelete: projectsApi.projectPendingDelete,
    setProjectPendingDelete: projectsApi.setProjectPendingDelete,
    projectSearchQuery: projectsApi.projectSearchQuery,
    setProjectSearchQuery: projectsApi.setProjectSearchQuery,
    structuredRequirements: store.structuredRequirements,
    auditResult: store.auditResult,
    prdMarkdown: store.prdMarkdown,
    sections: store.sections,
    editingSectionId: store.editingSectionId,
    setEditingSectionId: store.setEditingSectionId,
    editBuffer: store.editBuffer,
    setEditBuffer: store.setEditBuffer,
    handleSaveSection: store.handleSaveSection,
    lockedRequirements: locks.lockedRequirements,
    handleLockRequirement: locks.handleLockRequirement,
    handleUnlockRequirement: locks.handleUnlockRequirement,
    lockableRequirements: locks.requirementList,
    refreshLockableRequirements: locks.refreshRequirementList,
    sectionLocks: store.sectionLocks,
    handleToggleSectionLock: store.handleToggleSectionLock,
    handleHandSectionToAi: store.handleHandSectionToAi,
    handleRestoreVersion: store.handleRestoreVersion,
    loadVersionHistory: store.loadVersionHistory,
    activeTab: ui.activeTab,
    setActiveTab: ui.setActiveTab,
    activeView: ui.activeView,
    setActiveView: ui.setActiveView,
    mermaidDiagram: store.mermaidDiagram,
    diagramGeneratedAt: store.diagramGeneratedAt,
    versionHistory: store.versionHistory,
    selectedHistVersion: ui.selectedHistVersion,
    setSelectedHistVersion: ui.setSelectedHistVersion,
    diffBaseVersion: ui.diffBaseVersion,
    setDiffBaseVersion: ui.setDiffBaseVersion,
    diagramZoom: ui.diagramZoom,
    setDiagramZoom: ui.setDiagramZoom,
    hoverNode: ui.hoverNode,
    setHoverNode: ui.setHoverNode,
    activePacketFlow: ui.activePacketFlow,
    setActivePacketFlow: ui.setActivePacketFlow,
    clarificationAnswers: chat.clarificationAnswers,
    handleUpdateAnswerValue: chat.handleUpdateAnswerValue,
    handleSubmitClarifications: chat.handleSubmitClarifications,
    isProcessing: chat.isProcessing,
    isLoading: chat.isLoading,
    currentAgentNode: chat.currentAgentNode,
    syncStatus: chat.syncStatus,
    prdGenerationFailed: chat.prdGenerationFailed,
    prdDiagramGenerationFailed: chat.prdDiagramGenerationFailed,
    clearPrdGenerationNotices: chat.clearPrdGenerationNotices,
    chatEndRef: chat.chatEndRef,
    handleSendMessage: chat.handleSendMessage,
    handleValidateRequirements: chat.handleValidateRequirements,
    handleGeneratePRD: chat.handleGeneratePRD,
    canRunAgentActions,
    agentActionsBlockedReason,
    isCascadeGenerating: Boolean(activeCascadeJobId),
    handleStopGeneration: () => {
      if (documents.processingDocId) {
        // JOB 2.3 — Global Stop prioritizes the active document-extraction
        // coordinator; other durable/direct generation uses JOB 2.2.
        void documents.handleStopDocumentExtraction();
        return;
      }
      if (activeCascadeJobId && projectsApi.projectId) {
        store.setSyncStatus("Stopping PRD generation…");
        void api
          .cancelGenerationJob(projectsApi.projectId, activeCascadeJobId)
          .catch((error) => {
            handleError("Could not stop PRD generation.", error);
            store.setSyncStatus("PRD generation is still running.");
          });
        return;
      }
      chat.handleStopGeneration();
    },
    handleDownloadDocx,
    handlePrintPDF,
    loadProjectState,
    documents,
    // Finding #40: LM Studio connectivity health
    backendOnline: lm.backendOnline,
    lmStudioOnline: lm.lmStudioOnline,
    lmStudioModel: lm.lmStudioModel,
    lmStudioModelLoaded: lm.lmStudioModelLoaded,
    lmStudioLoadedModels: lm.lmStudioLoadedModels,
    lmStudioLatencyMs: lm.lmStudioLatencyMs,
    lmStudioLastCheckedAt: lm.lmStudioLastCheckedAt,
    lmStudioError: lm.lmStudioError,
    checkLmStudioHealth: lm.checkLmStudioHealth,
  };
}

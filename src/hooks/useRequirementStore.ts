// useRequirementStore — the requirement-engine domain store extracted from the
// former useProjectState god-hook.
//
// Owns every piece of requirement/PRD state (structured requirements, audit
// result, PRD markdown, sections, diagram source, version ledger, locks, chat
// messages, workflow/agent status) plus their typed setters. It is intentionally
// a "store", not a god-hook: cross-cutting concerns (SSE sync, chat handlers,
// lock actions) live in dedicated hooks that receive this store as a parameter,
// and `useProjectState` composes everything.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api/client";
import { toVersionHistoryList } from "../api/transforms";
import type {
  ArtifactLockState,
  AuditResult,
  ChatMessage,
  PRDSection,
  PrdSectionLockState,
  RequirementLockState,
  StructuredRequirements,
  VersionHistory,
} from "../components/types";
import { handleError } from "../components/Toast";
import { formatPrdVersionLabel } from "../utils/version";
import {
  formatUserStoriesToMarkdown,
  getSafeSectionContent,
  parsePRDToSections,
} from "../utils/markdown";

// ----------------------------------------------------------------------------
// Initial demo state (preserved from the former Dashboard monolith)
// ----------------------------------------------------------------------------

// The two boot messages are rendered with the actual wall-clock time instead of
// the previously hard-coded fake "10:24 AM" (display-only; no logic depends on
// this value — see scripts/unit.test.ts, which never asserts on a timestamp).
const BOOT_TIME = new Date().toLocaleTimeString([], {
  hour: "2-digit",
  minute: "2-digit",
});

const INITIAL_MESSAGES: ChatMessage[] = [
  {
    id: "init-1",
    role: "system",
    content:
      "Requirements Engine Initialized. Multi-Agent workflow is ready to ingest raw Product Owner specifications.",
    timestamp: BOOT_TIME,
  },
  {
    id: "init-2",
    role: "assistant",
    content:
      "Hello! I am your Technical Product Owner Assistant. Share your raw, conversational, or messy requirement text and I will structure it, run a compliance audit, and compile a formal PRD for you.",
    timestamp: BOOT_TIME,
  },
];

// Neutral starting state: the workspace boots EMPTY until real data is loaded
// from the backend or produced by an agent run. Seeding fabricated demo PRDs /
// diagrams here made every fresh project display fake "generated" documents
// before any LLM invocation had ever happened.
const INITIAL_STRUCTURED_REQUIREMENTS: StructuredRequirements = {
  epic_name: "",
  version: 1,
  user_stories: [],
};

const INITIAL_AUDIT_RESULT: AuditResult = {
  is_valid: false,
  audit_version_reviewed: 1,
  passed_checks: [],
  failed_checks: [],
  clarification_questions: [],
};

const INITIAL_PRD_MARKDOWN = "";

const INITIAL_MERMAID = "";

// ----------------------------------------------------------------------------
// Official Krungsri Nimble PRD template
// ----------------------------------------------------------------------------

// One-session cache for the official Krungsri Nimble PRD template served by
// GET /api/prd/template. This is the LATEX template (template-krungsrinimble.tex)
// - the exact structure the Architect agent fills in from project data. The
// right panel shows its normalized preview whenever the project has no
// generated PRD yet, and exports compile this same LaTeX.
let cachedPrdTemplate: string | null = null;

export async function getPrdTemplateMarkdown(): Promise<string> {
  if (cachedPrdTemplate !== null) return cachedPrdTemplate;
  try {
    const resp = await api.getPrdTemplate();
    cachedPrdTemplate = resp.template_latex || "";
  } catch {
    // The template is cosmetic — an unreachable backend surfaces its own errors.
    cachedPrdTemplate = "";
  }
  return cachedPrdTemplate;
}

const INITIAL_VERSION_HISTORY: VersionHistory[] = [];

// ----------------------------------------------------------------------------
// Store interface
// ----------------------------------------------------------------------------

export interface RequirementStore {
  // state
  structuredRequirements: StructuredRequirements;
  auditResult: AuditResult;
  prdMarkdown: string;
  /**
   * Preview copy of the PRD shown in the PRD panel. Generated PRDs are
   * Krungsri LaTeX bodies; this holds their server-normalized GFM form so the
   * markdown renderer never shows raw LaTeX. Markdown documents pass through.
   * Exports keep using the raw `prdMarkdown` so the PDF is still compiled from
   * the original .tex source.
   */
  prdMarkdownDisplay: string;
  sections: PRDSection[];
  editingSectionId: string | null;
  editBuffer: string;
  mermaidDiagram: string;
  diagramGeneratedAt: string | null;
  versionHistory: VersionHistory[];
  currentVersion: number;
  lockedArtifacts: Record<string, ArtifactLockState>;
  lockedRequirements: Record<string, RequirementLockState>;
  syncStatus: string;
  isProcessing: boolean;
  isLoading: boolean;
  currentAgentNode: string | null;
  clarificationAnswers: Record<string, string>;
  messages: ChatMessage[];
  chatEndRef: React.RefObject<HTMLDivElement | null>;

  // setters
  setStructuredRequirements: Dispatch<SetStateAction<StructuredRequirements>>;
  setAuditResult: Dispatch<SetStateAction<AuditResult>>;
  setPrdMarkdown: Dispatch<SetStateAction<string>>;
  setSections: Dispatch<SetStateAction<PRDSection[]>>;
  setEditingSectionId: Dispatch<SetStateAction<string | null>>;
  setEditBuffer: Dispatch<SetStateAction<string>>;
  setMermaidDiagram: Dispatch<SetStateAction<string>>;
  setDiagramGeneratedAt: Dispatch<SetStateAction<string | null>>;
  setVersionHistory: Dispatch<SetStateAction<VersionHistory[]>>;
  setCurrentVersion: Dispatch<SetStateAction<number>>;
  setLockedArtifacts: Dispatch<
    SetStateAction<Record<string, ArtifactLockState>>
  >;
  setLockedRequirements: Dispatch<
    SetStateAction<Record<string, RequirementLockState>>
  >;
  setSyncStatus: Dispatch<SetStateAction<string>>;
  setIsProcessing: Dispatch<SetStateAction<boolean>>;
  setIsLoading: Dispatch<SetStateAction<boolean>>;
  setCurrentAgentNode: Dispatch<SetStateAction<string | null>>;
  setClarificationAnswers: Dispatch<SetStateAction<Record<string, string>>>;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;

  // actions
  resetProjectState: () => void;
  handleSaveSection: (
    sectionId: string,
    newContent: string,
    baseContent?: string,
  ) => Promise<void>;
  /** Fetch the per-part PRD section lock/ownership map from the backend. */
  loadSectionLocks: (projId: string) => Promise<void>;
  /** Fetch the immutable PRD version ledger (AI + manual snapshots). */
  loadVersionHistory: (
    projId: string,
    options?: { append?: boolean; beforeVersion?: number; limit?: number },
  ) => Promise<number>;
  /** Restore the whole PRD document from a ledger version (append-only;
   *  locked parts are preserved). Resolves to true on success. */
  handleRestoreVersion: (versionNumber: number) => Promise<boolean>;
  /** Lock/unlock ONE PRD part (blocks edits + AI regeneration when locked). */
  handleToggleSectionLock: (sectionId: string) => Promise<void>;
  /** Hand a MANUAL (human-owned) part back to AI regeneration (content kept). */
  handleHandSectionToAi: (sectionId: string) => Promise<void>;
  sectionLocks: Record<string, PrdSectionLockState>;
}

// ----------------------------------------------------------------------------
// Hook
// ----------------------------------------------------------------------------

export function useRequirementStore(
  projectId: string | null,
): RequirementStore {
  const [structuredRequirements, setStructuredRequirements] =
    useState<StructuredRequirements>(INITIAL_STRUCTURED_REQUIREMENTS);
  const [auditResult, setAuditResult] =
    useState<AuditResult>(INITIAL_AUDIT_RESULT);
  const [prdMarkdown, setPrdMarkdown] = useState<string>(INITIAL_PRD_MARKDOWN);
  const [prdMarkdownDisplay, setPrdMarkdownDisplay] =
    useState<string>(INITIAL_PRD_MARKDOWN);
  // PRD-SECTION 1.1 — Parts load (trigger: project open via useProjectSync). The response
  //             carries the nine parts in document order; `editingSectionId` (declared
  //             just below) is what stops an incoming SSE-driven document from
  //             overwriting the part the user is currently typing in (EVENTS flow).
  const [sections, setSections] = useState<PRDSection[]>([]);
  const [editingSectionId, setEditingSectionId] = useState<string | null>(null);
  const [editBuffer, setEditBuffer] = useState<string>("");
  const [mermaidDiagram, setMermaidDiagram] = useState<string>(INITIAL_MERMAID);
  const [diagramGeneratedAt, setDiagramGeneratedAt] = useState<string | null>(
    null,
  );
  const [versionHistory, setVersionHistory] = useState<VersionHistory[]>(
    INITIAL_VERSION_HISTORY,
  );
  const [currentVersion, setCurrentVersion] = useState<number>(1);
  const [lockedArtifacts, setLockedArtifacts] = useState<
    Record<string, ArtifactLockState>
  >({});
  const [lockedRequirements, setLockedRequirements] = useState<
    Record<string, RequirementLockState>
  >({});
  // Per-part PRD section lock/ownership state (mirrors prd_sections rows).
  const [sectionLocks, setSectionLocks] = useState<
    Record<string, PrdSectionLockState>
  >({});
  const [syncStatus, setSyncStatus] = useState<string>("Synced with Local LLM");
  const [isProcessing, setIsProcessing] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [currentAgentNode, setCurrentAgentNode] = useState<string | null>(null);
  const [clarificationAnswers, setClarificationAnswers] = useState<
    Record<string, string>
  >({});
  const [messages, setMessages] = useState<ChatMessage[]>(INITIAL_MESSAGES);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const activeProjectIdRef = useRef(projectId);
  activeProjectIdRef.current = projectId;

  // Preload the official Krungsri Nimble template once per session so the PRD
  // panel shows the document skeleton immediately. The updater form guarantees
  // a concurrently arriving generated PRD is never clobbered by the skeleton.
  useEffect(() => {
    let cancelled = false;
    getPrdTemplateMarkdown().then((tpl) => {
      if (!cancelled && tpl) setPrdMarkdown((prev) => prev || tpl);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep the PREVIEW copy of the PRD in sync with the stored document.
  // Generated PRDs are Krungsri LaTeX bodies: the backend normalizes them into
  // clean GFM (POST /api/prd/convert) so the markdown renderer never shows raw
  // LaTeX. Markdown documents pass through untouched. Exports keep using the
  // raw `prdMarkdown`, so PDFs are still compiled from the original .tex.
  useEffect(() => {
    const src = prdMarkdown;
    if (!src) {
      setPrdMarkdownDisplay("");
      return;
    }
    let cancelled = false;
    // Normalize both LaTeX and stored Markdown. The backend also repairs
    // Pandoc's merged-table artifacts in legacy/current Markdown snapshots,
    // so sections 07–09 render and edit with the same semantic rows.
    api
      .convertPrdToMarkdown(src)
      .then((res) => {
        if (!cancelled && res.markdown) setPrdMarkdownDisplay(res.markdown);
      })
      .catch(() => {
        // Preview-only concern: fall back to the raw document, never blank out.
        if (!cancelled) setPrdMarkdownDisplay(src);
      });
    return () => {
      cancelled = true;
    };
  }, [prdMarkdown]);

  // Sync sections whenever the PRD preview or structuredRequirements.user_stories changes
  useEffect(() => {
    setSections((prevSections) => {
      const remoteRaw = getSafeSectionContent(prdMarkdownDisplay);
      const remoteSections = parsePRDToSections(remoteRaw);

      if (prevSections.length === 0) {
        return remoteSections.map((sec) => {
          if (
            sec.id === "user_stories" &&
            structuredRequirements.user_stories &&
            structuredRequirements.user_stories.length > 0
          ) {
            return {
              ...sec,
              content: formatUserStoriesToMarkdown(
                structuredRequirements.user_stories,
              ),
            };
          }
          return sec;
        });
      }

      let changed = false;
      const updated = prevSections.map((sec) => {
        // If this is the active editing section, do not overwrite it with remote updates
        if (editingSectionId === sec.id) {
          return sec;
        }

        // Specially format Section 3 if we have structured user stories
        if (
          sec.id === "user_stories" &&
          structuredRequirements.user_stories &&
          structuredRequirements.user_stories.length > 0
        ) {
          const formattedStories = formatUserStoriesToMarkdown(
            structuredRequirements.user_stories,
          );
          if (sec.content !== formattedStories) {
            changed = true;
            return { ...sec, content: formattedStories };
          }
          return sec;
        }

        const remoteSec = remoteSections.find((rs) => rs.id === sec.id);
        if (remoteSec) {
          const safeRemoteContent = getSafeSectionContent(remoteSec.content);
          if (sec.content !== safeRemoteContent) {
            changed = true;
            return { ...sec, content: safeRemoteContent };
          }
        }

        return sec;
      });

      // If any new sections were added remotely that weren't in prevSections, append them
      const missingSections = remoteSections.filter(
        (rs) => !prevSections.some((ps) => ps.id === rs.id),
      );
      if (missingSections.length > 0) {
        changed = true;
        return [...updated, ...missingSections];
      }

      return changed ? updated : prevSections;
    });
  }, [
    prdMarkdownDisplay,
    structuredRequirements.user_stories,
    editingSectionId,
  ]);

  // (Chat auto-scroll moved into ChatPanel — see the note in useChat.ts.)

  // PRD-SECTION 1.3 — Part save handler. Optimistically replaces the part locally, then
  //             PATCHes ONLY that part with the edit base so the server can three-way
  //             merge (PRD-SECTION 3.5).
  const handleSaveSection = async (
    sectionId: string,
    newContent: string,
    baseContent?: string,
  ) => {
    // 1. Update the local sections state
    const updatedSections = sections.map((s) =>
      s.id === sectionId ? { ...s, content: newContent } : s,
    );
    setSections(updatedSections);

    // 2. Save THE PART to the backend (PATCH one section). baseContent is the
    // text the editor started from — the server three-way merges it onto the
    // latest stored section, so a manual save is always "last version + this
    // edit" and never clobbers concurrent AI updates. Falls back to the
    // legacy whole-document save if the section endpoint is unavailable.
    if (projectId) {
      setSyncStatus("Saving section edit...");
      try {
        const res = await api.updatePrdSection(
          projectId,
          sectionId,
          newContent,
          { base_content: baseContent },
        );
        setPrdMarkdown(res.document_markdown);
        setPrdMarkdownDisplay(res.document_markdown);
        // Refresh per-part lock/version/ownership metadata (version_number
        // advanced; a manual save flips the part to human-owned -> "Manual").
        setSectionLocks((prev) => ({
          ...prev,
          [sectionId]: {
            ...(prev[sectionId] ?? {
              review_status: "draft",
              content_source: "ai",
              ai_generatable: true,
            }),
            ...(res.section.is_locked !== undefined
              ? { is_locked: res.section.is_locked }
              : {}),
            ...(res.section.review_status
              ? { review_status: res.section.review_status }
              : {}),
            ...(res.section.content_source
              ? { content_source: res.section.content_source }
              : {}),
            ...(res.section.ai_generatable !== undefined
              ? { ai_generatable: res.section.ai_generatable }
              : {}),
            version_number:
              res.section.version_number ??
              prev[sectionId]?.version_number ??
              null,
          },
        }));
        setSyncStatus(
          res.section.ai_generatable === false
            ? "Section saved. Marked Manual; Generate PRD will preserve it."
            : "Section saved & versioned.",
        );
      } catch (err) {
        // The section endpoint is authoritative. Falling back to a whole-document
        // write on lock/conflict/network errors can overwrite newer remote edits.
        setSections(sections);
        handleError("Failed to save manual edits to the database.", err);
        setSyncStatus("Save failed. The server copy was not changed.");
        return;
      }

      // Refresh the version ledger — a manual section edit always creates a new
      // immutable PRD version on the server, so reload the history to reflect the
      // new version number / change summary.
      await loadVersionHistory(projectId);
    }

    // 3. Reset editing state
    setEditingSectionId(null);
  };

  // PRD-SECTION 1.2 — Part metadata load (locks + review status + ownership flags +
  //             per-part version). Reads the SAME list endpoint as 1.1 (PRD-SECTION
  //             2.1); purely cosmetic bookkeeping, so a failure is ignored and never
  //             blocks project loading.
  const loadSectionLocks = async (projId: string) => {
    try {
      const payload = await api.getPrdSections(projId);
      const map: Record<string, PrdSectionLockState> = {};
      for (const s of payload.sections) {
        map[s.section_key] = {
          is_locked: s.is_locked,
          locked_by: s.locked_by,
          review_status: s.review_status,
          content_source: s.content_source,
          ai_generatable: s.ai_generatable,
          version_number: s.version_number ?? null,
        };
      }
      if (activeProjectIdRef.current !== projId) return;
      setSectionLocks(map);
    } catch {
      // Cosmetic metadata — never block project loading on it.
    }
  };

  // VERSION 1.1 — Ledger load (trigger: project open / after restore / after any
  //             snapshot-creating action). Failure is non-fatal: the timeline simply
  //             keeps its previous contents (the PRD panel is unaffected).
  const loadVersionHistory = useCallback(async (
    projId: string,
    options?: { append?: boolean; beforeVersion?: number; limit?: number },
  ): Promise<number> => {
    try {
      const payload = await api.getPrdVersions(projId, {
        limit: options?.limit ?? 20,
        beforeVersion: options?.beforeVersion,
      });
      if (activeProjectIdRef.current !== projId) return 0;
      console.log(
        "[VersionHistory] Loaded",
        payload?.length,
        "versions for project",
        projId,
      );
      const incoming = toVersionHistoryList(payload);
      setVersionHistory((current) => {
        if (!options?.append) return incoming;
        const byVersion = new Map(
          [...current, ...incoming].map((item) => [item.version, item]),
        );
        return [...byVersion.values()].sort((a, b) => b.version - a.version);
      });
      return incoming.length;
    } catch (err) {
      console.warn("[VersionHistory] Failed to load:", err);
      return 0;
    }
  }, []);

  // Restore the WHOLE PRD document from a ledger version. The backend is
  // append-only: the restored content becomes a NEW version (history is never
  // rewritten) and locked parts keep their current content. On success the
  // store document, preview copy, current version and ledger all refresh.
  // Resolves to true only on success so ConfirmModal closes on success.
  // VERSION 1.5 — Restore handler (VERSION 2.5 → 3.6). APPEND-ONLY semantics are
  //             visible here: the preview/document are set to the RESTORED content while
  //             `currentVersion` moves to the NEW number, and the ledger + section
  //             locks are reloaded so the timeline shows the appended row. Locked parts
  //             keep their current content (the backend enforces the lock contract).
  //             Resolves true only on success so the ConfirmModal closes.
  const handleRestoreVersion = async (
    versionNumber: number,
  ): Promise<boolean> => {
    if (!projectId) return false;
    const sourceSemVer = versionHistory.find(version => version.version === versionNumber)?.semVersion;
    setSyncStatus(`Restoring ${formatPrdVersionLabel(sourceSemVer, versionNumber)}...`);
    try {
      const res = await api.restorePrdVersion(projectId, versionNumber);
      setPrdMarkdown(res.document_markdown);
      setPrdMarkdownDisplay(res.document_markdown);
      setCurrentVersion(res.new_version_number);
      // Reload the ledger + per-part metadata so the timeline shows the NEW
      // appended version and section lock/version info stays accurate.
      await Promise.all([
        loadVersionHistory(projectId),
        loadSectionLocks(projectId),
      ]);
      setSyncStatus(
        `Restored from ${formatPrdVersionLabel(sourceSemVer, versionNumber)}. New version ${formatPrdVersionLabel(res.semver, res.new_version_number)} created.`,
      );
      return true;
    } catch (err) {
      handleError(`Failed to restore ${formatPrdVersionLabel(sourceSemVer, versionNumber)}.`, err);
      setSyncStatus("Restore failed.");
      return false;
    }
  };

  // PRD-SECTION 1.4 — Lock toggle. Optimistic: the local lock flag flips first, then
  //             the server lock/unlock call reconciles (PRD-SECTION 2.4/2.5) and a
  //             failure rolls the flip back. Locking a part is what excludes it from
  //             AI regeneration (ARCHITECT 5.1) and from part edits (4.4).
  const handleToggleSectionLock = async (sectionId: string) => {
    if (!projectId) return;
    const current = sectionLocks[sectionId];
    const wasLocked = current?.is_locked ?? false;
    // Optimistic update
    setSectionLocks((prev) => ({
      ...prev,
      [sectionId]: {
        ...(prev[sectionId] ?? {
          review_status: "draft",
          content_source: "ai",
          ai_generatable: true,
        }),
        is_locked: !wasLocked,
        locked_by: wasLocked ? null : "user",
      },
    }));
    setSyncStatus(wasLocked ? "Unlocking PRD part..." : "Locking PRD part...");
    try {
      if (wasLocked) {
        await api.unlockPrdSection(projectId, sectionId);
        setSyncStatus("PRD part unlocked. AI can regenerate it again.");
      } else {
        await api.lockPrdSection(projectId, sectionId);
        setSyncStatus(
          "PRD part locked. Protected from edits and AI regeneration.",
        );
      }
    } catch (err) {
      // Revert on failure
      setSectionLocks((prev) => ({
        ...prev,
        [sectionId]: {
          ...(prev[sectionId] as PrdSectionLockState),
          is_locked: wasLocked,
        },
      }));
      handleError(
        wasLocked
          ? "Failed to unlock the PRD part."
          : "Failed to lock the PRD part.",
        err,
      );
      setSyncStatus("Lock action failed.");
    }
  };

  // PRD-SECTION 1.6 — Hand a MANUAL part back to the AI. A manual save marks the
  //             part human-owned so "Generate PRD" preserves it; this action
  //             flips that flag back (content unchanged) so the Architect may
  //             regenerate the part again on the next run.
  const handleHandSectionToAi = async (sectionId: string) => {
    if (!projectId) return;
    const section = sections.find((s) => s.id === sectionId);
    if (!section) return;
    setSyncStatus("Handing PRD part back to the AI...");
    try {
      const res = await api.updatePrdSection(
        projectId,
        sectionId,
        section.content,
        {
          base_content: section.content,
          ai_generatable: true,
          // Distinct author so this hand-off is not mistaken for a manual edit
          // by the ownership backfill (it must stay AI-generatable).
          updated_by: "ai_handoff",
          change_summary: "Handed the part back to AI regeneration.",
        },
      );
      setPrdMarkdown(res.document_markdown);
      setPrdMarkdownDisplay(res.document_markdown);
      setSectionLocks((prev) => ({
        ...prev,
        [sectionId]: {
          ...(prev[sectionId] ?? { review_status: "draft" }),
          content_source: res.section.content_source,
          ai_generatable: res.section.ai_generatable,
          version_number:
            res.section.version_number ??
            prev[sectionId]?.version_number ??
            null,
        },
      }));
      setSyncStatus(
        "PRD part handed back to the AI. Generate PRD will refresh it.",
      );
      await loadVersionHistory(projectId);
    } catch (err) {
      handleError("Failed to hand the PRD part back to the AI.", err);
      setSyncStatus("Could not hand the part back to the AI.");
    }
  };

  // Legacy helper (kept for parity): clears the requirement-engine domain state.
  // Note: pending-actions reset lives in `useProjects`, which owns that slice.
  const resetProjectState = () => {
    setStructuredRequirements({ epic_name: "", version: 0, user_stories: [] });
    setAuditResult({
      is_valid: false,
      audit_version_reviewed: 0,
      passed_checks: [],
      failed_checks: [],
      clarification_questions: [],
    });
    setPrdMarkdown("");
    setPrdMarkdownDisplay("");
    setMermaidDiagram("");
    setDiagramGeneratedAt(null);
    setCurrentVersion(1);
    setCurrentAgentNode(null);
    setMessages([]);
    setVersionHistory([]);
    setSyncStatus("Switched project. Loading...");
    setSections([]);
    setSectionLocks({});
    setClarificationAnswers({});
  };

  return {
    structuredRequirements,
    auditResult,
    prdMarkdown,
    prdMarkdownDisplay,
    sections,
    editingSectionId,
    editBuffer,
    mermaidDiagram,
    diagramGeneratedAt,
    versionHistory,
    currentVersion,
    lockedArtifacts,
    lockedRequirements,
    syncStatus,
    isProcessing,
    isLoading,
    currentAgentNode,
    clarificationAnswers,
    messages,
    chatEndRef,
    setStructuredRequirements,
    setAuditResult,
    setPrdMarkdown,
    setSections,
    setEditingSectionId,
    setEditBuffer,
    setMermaidDiagram,
    setDiagramGeneratedAt,
    setVersionHistory,
    setCurrentVersion,
    setLockedArtifacts,
    setLockedRequirements,
    setSyncStatus,
    setIsProcessing,
    setIsLoading,
    setCurrentAgentNode,
    setClarificationAnswers,
    setMessages,
    resetProjectState,
    handleSaveSection,
    loadSectionLocks,
    loadVersionHistory,
    handleRestoreVersion,
    handleToggleSectionLock,
    handleHandSectionToAi,
    sectionLocks,
  };
}

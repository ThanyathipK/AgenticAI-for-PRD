// Dashboard — workspace shell (formerly a 3000+ line monolith).
// Now composes dedicated modules:
//   - useProjectState   -> all state/effects/handlers (src/hooks/useProjectState.ts)
//   - ChatPanel         -> left conversational panel
//   - PRDEditor         -> PRD Documents tab
//   - ArchitectureFlows -> System Flows tab
//   - VersionHistory    -> Version Ledger tab
//   - MarkdownRenderer  -> markdown rendering (via PRDEditor)
//   - File exports (PDF/DOCX) are compiled server-side in useProjectState.ts
//     (the browser never re-parses the LaTeX PRD into markdown).
import { useEffect, useRef, useState } from 'react';
import {
  PanelLeft,
  Plus,
  Search,
  MessageSquare,
  Check,
  XCircle,
  Pencil,
  LayoutGrid,
  CircleHelp,
  Lock,
  Pin,
  PinOff,
} from 'lucide-react';
import { useProjectState } from '../hooks/useProjectState';
import { isLockableRequirementId } from '../hooks/useArtifactLocks';
import { ChatPanel } from './ChatPanel';
import { ConfirmationPanel } from './ConfirmationPanel';
import { BUSINESS_TOPICS, NewProjectModal, STAKEHOLDER_ROLES } from './NewProjectModal';
import { ConfirmModal } from './ConfirmModal';
import { AuthPanel } from './AuthPanel';
import { AuthModal } from './AuthModal';
import { useAuth } from '../hooks/useAuth';
import { ProjectsDashboard } from './ProjectsDashboard';
import { Tooltip, TooltipBubble } from './Tooltip';
import { highlightMatch } from '../utils/highlight';
import { api } from '../api/client';
import type { ProjectIntake } from './NewProjectModal';
import { WorkspaceNavigation } from './WorkspaceNavigation';
import { MainArtifactPanel } from './MainArtifactPanel';
import { PRDGenerationPreview } from './PRDGenerationPreview';
import { HelpGuidelines } from './HelpGuidelines';
import { formatPrdVersionLabel } from '../utils/version';

const escapeTableCell = (value: string) => value.replaceAll('|', '\\|').replaceAll('\n', '<br>');

const replaceCoverField = (content: string, label: string, value: string) => {
  // The cover is a Markdown table. Replace only its second cell; replacing a
  // whole line would collapse label and value into the first cell.
  const tableField = new RegExp(`(\\|\\s*\\*\\*${label}:\\*\\*\\s*\\|\\s*)[^|]*(\\|)`, 'i');
  if (tableField.test(content)) return content.replace(tableField, `$1${escapeTableCell(value)}$2`);
  return content.replace(new RegExp(`(${label}:\\s*)[^\\n]*`, 'i'), `$1${value}`);
};

const stakeholderSectionContent = (stakeholders: Record<string, string>) =>
  ['### Stakeholders', '', '|Role|Name|', '|---|---|', ...STAKEHOLDER_ROLES.map(role => `|${role}|${escapeTableCell(stakeholders[role] ?? '')}|`)].join('\n');

const businessOverviewContent = (topics: Record<string, string>) =>
  ['### 1. Business & Strategic Overview', '', '|Topic|Details|', '|---|---|', ...BUSINESS_TOPICS.map(topic => `|${topic}|${escapeTableCell(topics[topic] ?? '')}|`)].join('\n');

const seedPrdIntake = async (projectId: string, projectName: string, intake: ProjectIntake): Promise<void> => {
  // Materialize the official template first. A preview-only response has
  // placeholder content, so patching it would replace the cover with blank.
  const { sections } = await api.getPrdSections(projectId, true);
  const title = sections.find(section => section.section_key === 'title');
  const today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Bangkok',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
  const titleContent = title
    ? [
      ['PMO No', intake.pmoNo || 'TBD'],
      ['PMO Name', projectName],
      ['Version', 'V1.0.0'],
      ['Status', 'Draft'],
      ['Last Update', today],
      ['Author', intake.author || 'Product Owner'],
    ].reduce(
      (content, [label, value]) => replaceCoverField(content, label, value),
      title.content,
    )
    : '';
  const hasStakeholderInput = Object.values(intake.stakeholders).some(value => value.trim());
  const hasBusinessInput = Object.values(intake.businessTopics).some(value => value.trim());
  // These writes deliberately stay sequential. A first edit seeds the nine
  // PRD rows server-side; concurrent PATCH requests can otherwise race while
  // both requests see the unseeded project and attempt the same INSERT.
  if (title) {
    await api.updatePrdSection(projectId, 'title', titleContent, {
      base_content: title.content,
      change_summary: 'Set PMO number from project intake.',
      // The cover is hybrid: PMO/Author are reused by the deterministic
      // generator, while Version/Status/Last Update must refresh on Generate.
      ai_generatable: true,
    });
  }
  if (hasStakeholderInput) {
    await api.updatePrdSection(projectId, 'stakeholders', stakeholderSectionContent(intake.stakeholders), {
      change_summary: 'Set stakeholders from project intake.',
    });
  }
  if (hasBusinessInput) {
    await api.updatePrdSection(projectId, 'business_overview', businessOverviewContent(intake.businessTopics), {
      change_summary: 'Set business and strategic overview from project intake.',
    });
  }
};

export default function Dashboard() {
  // AUTH 1.3 — Frontend session owner instantiated once for the whole app
  //            (src/hooks/useAuth.ts). It provides the LOGIN callbacks wired to
  //            AUTH 1.1.1 below and the signed-in user every authenticated UI
  //            branch reads (AUTH 1.6).
  // useAuth must resolve BEFORE useProjectState: the project list is scoped to
  // the signed-in user (empty until login, that user's projects after).
  const {
    user: authUser,
    isAuthenticating,
    signupEnabled,
    login: handleLogin,
    register: handleRegister,
    logout: handleLogout,
    deleteAccount: handleDeleteAccount,
    isAuthModalOpen,
    setIsAuthModalOpen,
  } = useAuth();
  const userDisplayName = authUser?.full_name || authUser?.email || 'User';
  // AUTH 1.6 — Authenticated application state: because AUTH 1.5 flipped
  //            authUser, this hook re-scopes the project list to that user
  //            (`GET /api/project …`). Client-side gating only — every one of
  //            those requests is re-authorized server-side by AUTH 7.4.
  const state = useProjectState(!!authUser, authUser?.id ?? null);
  const {
    projectId,
    setProjectId,
    projects,
    historyCollapsed,
    setHistoryCollapsed,
    projectContextMenu,
    setProjectContextMenu,
    renameProjectId,
    setRenameProjectId,
    renameProjectName,
    setRenameProjectName,
    handleRenameProject,
    handleDeleteProject,
    handleCreateProject,
    handleSendMessage,
    handleTogglePin,
    isCreateModalOpen,
    setIsCreateModalOpen,
    projectPendingDelete,
    setProjectPendingDelete,
    projectSearchQuery,
    setProjectSearchQuery,
    pendingActions,
    setPendingActions,
    loadProjectState,
    setMessages,
    currentVersion,
    versionHistory,
    isDraggingSplit,
    setIsDraggingSplit,
    splitContainerRef,
    activeTab,
    setActiveTab,
    activeView,
    setActiveView,
    handleDownloadDocx,
    handlePrintPDF,
    structuredRequirements,
    lockedRequirements,
    handleLockRequirement,
    handleUnlockRequirement,
    lockableRequirements,
    refreshLockableRequirements,
    handleGeneratePRD,
  } = state;

  // ---- Sidebar project search (pure presentation state) --------------------
  // The query itself lives in useProjects; this only controls whether the
  // search input row is revealed and keeps it focused when opened.
  const [isSearchOpen, setIsSearchOpen] = useState<boolean>(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const previewScrollRef = useRef<HTMLDivElement>(null);
  const previousPendingActionIdsRef = useRef<Set<string>>(new Set());

  // Use one sidebar state at every viewport size. Previously narrow screens
  // switched to a separate overlay drawer, so the same control had different
  // behavior depending on the window width.
  const collapsed = historyCollapsed;
  const expanded = !collapsed;

  // ---- Requirements tab: lockable requirement rows -------------------------
  // The "Requirement Lock Status" panel lists EVERY requirement of the project:
  // the authoritative DB rows (real UUIDs + lock metadata) when they are loaded,
  // otherwise the requirement groups held in memory. Previously it required an
  // in-memory `requirements` array to be present, so the whole panel silently
  // disappeared whenever the state came from a flat/legacy payload.
  const lockTargets =
    lockableRequirements.length > 0
      ? lockableRequirements
      : structuredRequirements.requirements ?? [];

  useEffect(() => {
    if (isSearchOpen) {
      searchInputRef.current?.focus();
    }
  }, [isSearchOpen]);

  // A newly gathered requirement merge is displayed at the very top of the
  // preview window. If the user was reading farther down, reveal the new merge
  // card immediately so the Save / Cancel decision cannot go unnoticed.
  useEffect(() => {
    const previousIds = previousPendingActionIdsRef.current;
    const hasNewPendingAction = pendingActions.some(action => !previousIds.has(action.id));
    previousPendingActionIdsRef.current = new Set(pendingActions.map(action => action.id));

    if (hasNewPendingAction) {
      previewScrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, [pendingActions]);

  const handleToggleProjectSearch = () => {
    // Search is a per-user, server-scoped query — meaningless (and 401) before
    // login, so the toggle is a no-op for signed-out visitors.
    if (!authUser) return;
    // Expanding the sidebar first guarantees the input row is visible.
    if (historyCollapsed) setHistoryCollapsed(false);
    setIsSearchOpen((open) => !open);
  };

  const handleCloseProjectSearch = () => {
    setIsSearchOpen(false);
    setProjectSearchQuery('');
  };

  // Sidebar projects to render. Matching (project names AND conversation
  // message content) happens server-side via GET /api/projects/search — the
  // hook's debounced search effect swaps `projects` for the matching subset.
  // Pinned chats sort first; the list below renders them as two clearly
  // separated sections ("Pinned" vs "Recent Projects") instead of one blend.
  const visibleProjects = [...projects].sort((a, b) => Number(!!b.is_pinned) - Number(!!a.is_pinned));

  return (
    <div className="flex-1 flex overflow-hidden h-full">
      {/* Background radial soft light gradient */}
      <div className="absolute inset-0 pointer-events-none z-[-1] overflow-hidden">
        <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-primary/3 blur-[140px] rounded-full"></div>
        <div className="absolute bottom-[10%] right-[-5%] w-[40%] h-[40%] bg-primary/2 blur-[120px] rounded-full"></div>
      </div>

      {/* PROJECT HISTORY SIDEBAR */}
      <aside
        id="project-sidebar"
        className={`flex flex-col bg-white border-r border-outline shrink-0 transition-[width] duration-200 ease-out ${historyCollapsed ? 'w-[52px]' : 'w-[280px]'}`}
      >
        <div className={`py-4 flex items-center ${collapsed ? 'justify-center px-2' : 'justify-between px-5'} border-b border-outline`}>
          <div className={`flex items-center gap-2 overflow-hidden ${collapsed ? 'w-0 opacity-0' : 'opacity-100'}`}>
            <div className="w-6.5 h-6.5 rounded-lg bg-primary flex items-center justify-center text-on-primary text-[10px] font-bold shrink-0">Ai</div>
            <span className="text-[13px] font-bold text-on-surface whitespace-nowrap">Agentic-AI</span>
          </div>
          <Tooltip label={expanded ? 'Hide sidebar' : 'Show sidebar'} side="right">
            <button
              className="w-8 h-8 rounded-lg flex items-center justify-center text-on-surface-variant hover:bg-[#f6f1ea] hover:text-primary transition-colors shrink-0"
              onClick={() => setHistoryCollapsed((v: boolean) => !v)}
              aria-label={expanded ? 'Hide sidebar' : 'Show sidebar'}
            >
              <PanelLeft className="w-4 h-4" />
            </button>
          </Tooltip>
        </div>

        <div className="px-1.5 pt-2">
          {/* AUTH 1.6 — Signed-out visitors are pushed into the LOGIN entry
              point (AUTH 1.1) instead of the create-project modal; project
              creation itself is authorized server-side in AUTH 7.4. */}
          <Tooltip label={authUser ? 'New Project' : 'Sign in to create a project'} side="right" className="w-full">
            <button
              className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] font-semibold text-on-surface hover:bg-primary/10 hover:text-primary transition-colors ${collapsed ? 'justify-center' : ''}`}
              aria-label="New Project"
              onClick={() => (authUser ? setIsCreateModalOpen(true) : setIsAuthModalOpen(true))}
            >
              <Plus className="w-4 h-4 shrink-0" />
              {expanded && <span>New Project</span>}
            </button>
          </Tooltip>
          {/* View switcher — the sidebar doubles as the app's navigation: the
              chat/PRD workspace and the projects overview dashboard are the
              two pages, and the active one is highlighted like a nav item. */}
          <Tooltip label="Workspace" side="right" className="w-full">
            <button
              className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] font-semibold transition-colors ${collapsed ? 'justify-center' : ''} ${
                activeView === 'workspace'
                  ? 'bg-primary/10 text-primary'
                  : 'text-on-surface-variant hover:bg-primary/10 hover:text-primary'
              }`}
              aria-label="Workspace view"
              aria-current={activeView === 'workspace' ? 'page' : undefined}
              onClick={() => setActiveView('workspace')}
            >
              <MessageSquare className="w-4 h-4 shrink-0" />
              {expanded && <span>Workspace</span>}
            </button>
          </Tooltip>
          <Tooltip label="Dashboard" side="right" className="w-full">
            <button
              className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] font-semibold transition-colors ${collapsed ? 'justify-center' : ''} ${
                activeView === 'dashboard'
                  ? 'bg-primary/10 text-primary'
                  : 'text-on-surface-variant hover:bg-primary/10 hover:text-primary'
              }`}
              aria-label="Dashboard view"
              aria-current={activeView === 'dashboard' ? 'page' : undefined}
              onClick={() => setActiveView('dashboard')}
            >
              <LayoutGrid className="w-4 h-4 shrink-0" />
              {expanded && <span>Dashboard</span>}
            </button>
          </Tooltip>
          <Tooltip label="Search Projects" side="right" className="w-full">
            <button
              className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] font-semibold transition-colors ${collapsed ? 'justify-center' : ''} ${
                isSearchOpen || projectSearchQuery
                  ? 'bg-primary/10 text-primary'
                  : 'text-on-surface-variant hover:bg-primary/10 hover:text-primary'
              }`}
              aria-label="Search Projects"
              onClick={handleToggleProjectSearch}
            >
              <Search className="w-4 h-4 shrink-0" />
              {expanded && <span>Search Projects</span>}
            </button>
          </Tooltip>
          {authUser && isSearchOpen && expanded && (
            <div className="relative flex items-center mt-1">
              <Search className="absolute left-2.5 w-3.5 h-3.5 text-on-surface-variant pointer-events-none" />
              {/* PROJECT 3.1 — Sidebar search UI entry: each keystroke feeds the
                  debounced hook effect (PROJECT 3.2 → api 3.3 → route 3.4) which
                  replaces `projects`; rows then render `match_snippet`. */}
              <input
                ref={searchInputRef}
                type="text"
                value={projectSearchQuery}
                onChange={(e) => setProjectSearchQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.stopPropagation();
                    handleCloseProjectSearch();
                  }
                }}
                placeholder="Search projects or messages..."
                className="w-full bg-white border border-outline rounded-xl pl-8 pr-7 py-1.5 text-xs text-on-surface focus:outline-none focus:border-primary"
              />
              {projectSearchQuery && (
                <button
                  onClick={() => {
                    setProjectSearchQuery('');
                    searchInputRef.current?.focus();
                  }}
                  className="group/tt absolute right-1.5 p-0.5 text-slate-400 hover:text-slate-600 rounded"
                  aria-label="Clear search"
                >
                  <XCircle className="w-3.5 h-3.5" />
                  <TooltipBubble label="Clear search" side="bottom" />
                </button>
              )}
            </div>
          )}
          <Tooltip label="Help & Guidelines" side="right" className="w-full">
            <button
              className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] font-semibold transition-colors ${collapsed ? 'justify-center' : ''} ${
                activeView === 'help'
                  ? 'bg-primary/10 text-primary'
                  : 'text-on-surface-variant hover:bg-primary/10 hover:text-primary'
              }`}
              aria-label="Help and Guidelines"
              aria-current={activeView === 'help' ? 'page' : undefined}
              onClick={() => setActiveView('help')}
            >
              <CircleHelp className="w-4 h-4 shrink-0" />
              {expanded && <span>Help &amp; Guidelines</span>}
            </button>
          </Tooltip>
        </div>

        {/* Section headers ("Pinned" / "Recent Projects") render inside the
            scrollable list below, directly above each group of chats. */}
        {expanded && (
          <div className="flex-1 overflow-y-auto custom-scrollbar px-2 pb-2">
          {!authUser && expanded && (
            // Signed out: no project data exists on screen at all (the hook
            // never fetches without a session) — prompt for sign-in instead.
            // The Sign in button lives in AuthPanel at the bottom of the rail.
            <div className="px-2.5 py-2 text-[12.5px] text-on-surface-variant">
              Sign in to see your projects
            </div>
          )}
          {authUser && projects.length === 0 && expanded && (
            <div className="px-2.5 py-2 text-[12.5px] text-on-surface-variant">No projects yet</div>
          )}
          {projects.length > 0 && visibleProjects.length === 0 && expanded && (
            <div className="px-2.5 py-2 text-[12.5px] text-on-surface-variant">
              No projects or messages matching &quot;{projectSearchQuery.trim()}&quot;
            </div>
          )}
          {/* PROJECT 1.1 — Sidebar project list (UI layer of PROJECT 1.x).
              Data comes from PROJECT 1.5 (`projects`); each row is a selection
              trigger (PROJECT 2.1), a pin button (PROJECT 7.1) and a context
              menu (rename PROJECT 5.1 / delete PROJECT 6.1). */}
          {visibleProjects.map((p, index) => {
            const isActive = projectId === p.id;
            // Group boundaries: the first pinned chat opens the "Pinned"
            // section and the first unpinned chat opens "Recent Projects", so
            // pinned and unpinned chats stay visually separated instead of
            // blending into a single list. Collapsed mode (icons only) falls
            // back to a thin divider between the two groups.
            const prev = index > 0 ? visibleProjects[index - 1] : null;
            const startsPinnedGroup = !!p.is_pinned && (!prev || !prev.is_pinned);
            const startsUnpinnedGroup = !p.is_pinned && (!prev || !!prev.is_pinned);
            return [
              expanded && startsPinnedGroup && (
                <div
                  key={`${p.id}-pinned-header`}
                  className="px-2.5 pt-4 pb-1.5 flex items-center gap-1.5 text-[11px] font-bold tracking-wide uppercase text-on-surface-variant/70"
                >
                  <Pin className="w-3 h-3 text-primary shrink-0" />
                  Pinned
                </div>
              ),
              startsUnpinnedGroup && index > 0 && (
                <div
                  key={`${p.id}-group-divider`}
                  className="mx-1 mt-2 border-t border-outline/70"
                  aria-hidden="true"
                />
              ),
              expanded && startsUnpinnedGroup && (
                <div
                  key={`${p.id}-recent-header`}
                  className="px-2.5 pt-2 pb-1.5 flex items-center gap-1.5 text-[11px] font-bold tracking-wide uppercase text-on-surface-variant/70"
                >
                  <MessageSquare className="w-3 h-3 shrink-0" />
                  Recent Projects
                </div>
              ),
              <div key={p.id} className="relative group">
                {renameProjectId === p.id ? (
                  <div className="flex items-center gap-1 px-2 py-1">
                    <input
                      type="text"
                      value={renameProjectName}
                      onChange={(e) => setRenameProjectName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleRenameProject(p.id, renameProjectName);
                        if (e.key === 'Escape') setRenameProjectId(null);
                      }}
                      maxLength={50}
                      className="flex-1 bg-white border border-primary/30 rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-primary"
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                    />
                    <Tooltip label="Confirm rename" side="left">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleRenameProject(p.id, renameProjectName); }}
                        className="p-1 text-emerald-600 hover:bg-emerald-50 rounded"
                        aria-label="Confirm rename"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                    </Tooltip>
                    <Tooltip label="Cancel rename" side="left">
                      <button
                        onClick={(e) => { e.stopPropagation(); setRenameProjectId(null); }}
                        className="p-1 text-slate-400 hover:bg-slate-100 rounded"
                        aria-label="Cancel rename"
                      >
                        <XCircle className="w-3.5 h-3.5" />
                      </button>
                    </Tooltip>
                  </div>
                ) : (
                  <button
                    onClick={() => {
                      // PROJECT 2.1 — Selection trigger: switching the active
                      // project id re-fires the state load (PROJECT 2.2 → 2.4)
                      // because useProjectSync's effect depends on projectId.
                      setProjectId(p.id);
                      // Selecting a project from the sidebar always lands the
                      // user back in the chat/PRD workspace, even if they were
                      // browsing the dashboard view.
                      setActiveView('workspace');
                    }}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setProjectContextMenu({ projectId: p.id, x: e.clientX, y: e.clientY });
                    }}
                    className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-[13px] mb-0.5 transition-colors truncate ${collapsed ? 'justify-center' : ''} ${
                      isActive ? 'bg-primary/10 text-primary font-semibold' : 'text-on-surface hover:bg-surface'
                    }`}
                    title={p.name}
                  >
                    <div className="relative shrink-0">
                      <MessageSquare className={`w-4 h-4 ${isActive ? 'text-primary' : ''}`} />
                      {isActive && (
                        <span className="absolute -top-1 -right-1 w-2.5 h-2.5 bg-primary rounded-full border-2 border-background"></span>
                      )}
                    </div>
                    {expanded && (
                      <>
                        <span className="truncate flex-1 text-left">{highlightMatch(p.name, projectSearchQuery)}</span>
                        <div className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-0.5 shrink-0">
                          <Tooltip label={p.is_pinned ? 'Unpin' : 'Pin'} side="left">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                // PROJECT 7.1 (UI trigger) — sidebar pin button:
                                // handler 7.1 → api 7.2 → route 7.3 → repo 7.3.1.
                                handleTogglePin(p.id);
                              }}
                              className={`p-1 rounded-lg transition-colors ${p.is_pinned ? 'text-primary hover:bg-primary/10' : 'text-slate-400 hover:text-primary hover:bg-primary/10'}`}
                              aria-label={p.is_pinned ? 'Unpin' : 'Pin'}
                            >
                              {p.is_pinned
                                ? <PinOff className="w-3 h-3" />
                                : <Pin className="w-3 h-3" />}
                            </button>
                          </Tooltip>
                          <Tooltip label="Rename" side="left">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setRenameProjectId(p.id);
                                setRenameProjectName(p.name);
                              }}
                              className="p-1 text-slate-400 hover:text-primary hover:bg-primary/10 rounded-lg transition-colors"
                              aria-label="Rename"
                            >
                              <Pencil className="w-3 h-3" />
                            </button>
                          </Tooltip>
                          <Tooltip label="Delete" side="left">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setProjectPendingDelete(p.id);
                              }}
                              className="p-1 text-slate-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                              aria-label="Delete"
                            >
                              <XCircle className="w-3 h-3" />
                            </button>
                          </Tooltip>
                        </div>
                      </>
                    )}
                  </button>
                )}
                {expanded && renameProjectId !== p.id && p.match_snippet && (
                  <div className="mt-1 px-2.5 flex items-center gap-1.5 text-[11px] leading-snug text-on-surface-variant min-w-0">
                    <MessageSquare className="w-3 h-3 shrink-0 text-on-surface-variant/70" />
                    <span className="truncate flex-1 text-left">
                      {highlightMatch(p.match_snippet, projectSearchQuery)}
                    </span>
                  </div>
                )}
              </div>,
            ];
          })}
          </div>
        )}

        {/* Context Menu */}
        {projectContextMenu && (
          <div
            className="fixed z-50 bg-white border border-slate-200 rounded-xl shadow-xl py-1 min-w-[160px] max-w-[calc(100vw-1rem)]"
            style={{
              left: Math.min(projectContextMenu.x, Math.max(8, window.innerWidth - 176)),
              top: Math.min(projectContextMenu.y, Math.max(8, window.innerHeight - 150)),
            }}
          >
            <button
              className="w-full flex items-center gap-2 px-3 py-2 text-xs text-slate-700 hover:bg-slate-50 transition-colors"
              onClick={() => {
                // PROJECT 7.1 (context-menu trigger) — same chain as the sidebar
                // pin button: 7.1 → api 7.2 → route 7.3 → repo 7.3.1.
                handleTogglePin(projectContextMenu.projectId);
              }}
            >
              {(() => {
                const p = projects.find((pr) => pr.id === projectContextMenu.projectId);
                return p && p.is_pinned ? (
                  <>
                    <PinOff className="w-3.5 h-3.5 text-primary" />
                    <span>Unpin</span>
                  </>
                ) : (
                  <>
                    <Pin className="w-3.5 h-3.5" />
                    <span>Pin</span>
                  </>
                );
              })()}
            </button>
            <button
              className="w-full flex items-center gap-2 px-3 py-2 text-xs text-slate-700 hover:bg-slate-50 transition-colors"
              onClick={() => {
                const p = projects.find((pr) => pr.id === projectContextMenu.projectId);
                if (p) {
                  setRenameProjectId(p.id);
                  setRenameProjectName(p.name);
                }
                setProjectContextMenu(null);
              }}
            >
              <Pencil className="w-3.5 h-3.5" />
              <span>Rename</span>
            </button>
            <button
              className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-600 hover:bg-red-50 transition-colors"
              onClick={() => {
                const pid = projectContextMenu.projectId;
                setProjectContextMenu(null);
                setProjectPendingDelete(pid);
              }}
            >
              <XCircle className="w-3.5 h-3.5" />
              <span>Delete</span>
            </button>
          </div>
        )}

        {/* AUTH — sign-in button / signed-in user chip, pinned to the sidebar's
            BOTTOM-LEFT. It is the last child of the sidebar column and the
            project list above it is `flex-1`, so it stays glued to the bottom
            while the list scrolls. */}
        {/* AUTH 1.1 / AUTH 1.6 — Signed out: the "Sign in" trigger. Signed in:
            the user chip whose Log out button starts AUTH 4.1. */}
        <div className={collapsed ? 'mt-auto' : undefined}>
          <AuthPanel
            user={authUser}
            isAuthenticating={isAuthenticating}
            collapsed={collapsed}
            onOpenAuth={() => setIsAuthModalOpen(true)}
            onLogout={handleLogout}
            onDeleteAccount={handleDeleteAccount}
          />
        </div>
      </aside>

      {/* PAGE CONTENT — the workspace (chat + PRD tabs) or the projects
          overview dashboard, switched from the sidebar's view nav. Only the
          content area swaps; the sidebar and its shared project state stay
          mounted so both views always agree. */}
      {activeView === 'dashboard' ? (
        // PROJECT 1.5 / 7.4.1 / 7.5.1 — Projects overview table: consumes the SAME
        // project list state, so flag (7.4.x), workflow status (7.5.x) and row
        // open (PROJECT 2.1) all write through the hooks documented above.
        <ProjectsDashboard state={state} />
      ) : activeView === 'help' ? (
        <HelpGuidelines />
      ) : (
      <div id="split-container" className="flex-1 min-w-0 min-h-0 flex flex-col md:flex-row overflow-hidden bg-white" ref={splitContainerRef}>
        
        {/* LEFT PANEL: Conversational Timeline & Human-In-The-Loop */}
        <ChatPanel state={state} userName={userDisplayName} />

        {/* RESIZABLE DIVIDER */}
        <div
          className={`resize-divider ${isDraggingSplit ? 'is-dragging' : ''}`}
          onMouseDown={() => setIsDraggingSplit(true)}
        />

        {/* RIGHT PANEL: Live Workspace Previews (Tabbed System) */}
        <section className="flex-[1_1_0%] min-w-0 min-h-0 flex flex-col bg-background relative overflow-hidden">
          {/* Tabs bar — warm pill navigation (PRD Documents / Requirements / Flows /
              Versions) modeled on the PRD nav reference; each tab is wired to its
              live workspace panel, and the DOCX/PDF exports sit at the pill's right
              edge (inside it) like the reference's Export button. */}
          <WorkspaceNavigation
            activeTab={activeTab}
            onTabChange={setActiveTab}
            onDownloadDocx={handleDownloadDocx}
            onPrintPdf={handlePrintPDF}
          />

          {/* RIGHT VIEW WINDOW */}
          <div
            id="printable-document"
            ref={previewScrollRef}
            className="flex-1 min-w-0 overflow-y-auto p-3 md:p-4 custom-scrollbar"
          >
{/* CONFIRM 1.0 / 1.1 — Human-in-the-loop gate mount: one ConfirmationPanel per staged
    action. onConfirm drops the card and re-reads the authoritative project state +
    lock list; onCancel just closes it (nothing was written). */}
            {pendingActions.filter(action => action.action_type !== 'GENERATE_PRD').map(action => (
                <ConfirmationPanel 
                    key={action.id} 
                    action={action} 
                    onConfirm={() => {
                        setPendingActions(prev => prev.filter(a => a.id !== action.id));
                        if(projectId) loadProjectState(projectId);
                        // The merge just created/renamed requirement records, so
                        // re-read the authoritative lock list (new rows must be
                        // listed and lockable immediately, with their real ids).
                        void refreshLockableRequirements();
                        setMessages(prev => [...prev, {
                            id: `merge-saved-${Date.now()}`,
                            role: 'system',
                            content: 'Requirements saved. Review or validate them, then select Generate PRD when you are ready to create a PRD preview.',
                            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                        }]);
                    }}
                    onCancel={() => {
                        setPendingActions(prev => prev.filter(a => a.id !== action.id));
                        // ROLLBACK: the merge was only ever a draft — re-fetch the
                        // stored state so the workspace shows exactly the version
                        // from BEFORE the preview was shown. Nothing was saved.
                        if(projectId) loadProjectState(projectId);
                        setMessages(prev => [...prev, {
                            id: `merge-cancelled-${Date.now()}`,
                            role: 'system',
                            content: `Merge discarded. Rolled back to the previous PRD version (${formatPrdVersionLabel(versionHistory.find(version => version.version === currentVersion)?.semVersion, currentVersion)}). Nothing was saved.`,
                            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                        }]);
                    }}
                />
            ))}

            {activeTab === 'prd' && pendingActions.filter(action => action.action_type === 'GENERATE_PRD').map(action => (
              <PRDGenerationPreview
                key={action.id}
                action={action}
                onApproved={(result) => {
                  setPendingActions(prev => prev.filter(item => item.id !== action.id));
                  if (projectId) void loadProjectState(projectId);
                  setMessages(prev => [...prev, {
                    id: `prd-saved-${Date.now()}`,
                    role: 'system',
                    content: `PRD saved successfully${result.requirement_state?.version_number ? ` as ${formatPrdVersionLabel(result.prd_semver, result.requirement_state.version_number)}` : ''}. This approved document is now the current PRD.`,
                    timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                  }]);
                }}
                onDiscarded={() => {
                  setPendingActions(prev => prev.filter(item => item.id !== action.id));
                  setMessages(prev => [...prev, {
                    id: `prd-discarded-${Date.now()}`,
                    role: 'system',
                    content: 'PRD preview discarded. The saved PRD was not changed.',
                    timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                  }]);
                }}
                onRegenerate={handleGeneratePRD}
                onBackToRequirements={() => setActiveTab('trace')}
              />
            ))}

            {/* Requirement Lock Status Panel — only shown on the Requirements tab */}
            {activeTab === 'trace' && lockTargets.length > 0 && (
              <div className="max-w-4xl mx-auto mb-3 px-1 py-2">
                <div className="flex items-center gap-1.5 mb-3 text-[10px] font-bold tracking-[0.08em] text-[#716860] uppercase">
                  <span>{lockTargets.length} requirements</span>
                  <span>·</span>
                  <span>0 manual</span>
                  <span>·</span>
                  <span>{lockTargets.filter((req) => Boolean(req.is_locked || lockedRequirements[req.requirement_code]?.is_locked)).length} locked</span>
                </div>
                <div className="space-y-2">
                  {lockTargets.map((req) => {
                    const isLocked = Boolean(req.is_locked || lockedRequirements[req.requirement_code]?.is_locked);
                    // A requirement created by a gather run has no database UUID
                    // until its merge is saved; the lock API would reject it, so
                    // the row is flagged instead of offering a button that fails.
                    const canLock =
                      isLockableRequirementId(req.id) ||
                      isLockableRequirementId(lockedRequirements[req.requirement_code]?.artifact_id);
                    const isArchived = req.status?.toLowerCase() === 'archived';
                    return (
                      <div key={req.requirement_code} className={`flex items-center justify-between gap-2 sm:gap-3 px-3 py-2.5 rounded-[10px] border ${isLocked ? 'bg-[#f0f8f2] border-[#cfe7d4]' : 'bg-white border-outline'}`}>
                        <div className="flex items-center gap-2.5 min-w-0 flex-1">
                          {isLocked && <Lock className="w-3 h-3 shrink-0 text-emerald-600" />}
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0 flex-1">
                              <span className={`text-[11px] font-mono font-bold shrink-0 ${isLocked ? 'text-emerald-700' : 'text-[#a89b91]'}`}>{req.requirement_code}</span>
                              <span className="text-xs font-semibold text-on-surface break-words min-w-0 flex-1">{req.title}</span>
                              <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600 border border-indigo-200 font-semibold">AI</span>
                              {isArchived && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">Archived</span>
                              )}
                          </div>
                        </div>
                        {canLock ? (
                          <button
                            onClick={() => isLocked ? handleUnlockRequirement(req.requirement_code) : handleLockRequirement(req.requirement_code)}
                            className={`min-w-[70px] px-3 py-1.5 rounded-full text-[10px] font-bold transition-all flex items-center justify-center gap-1.5 shrink-0 cursor-pointer ${
                              isLocked 
                                ? 'bg-emerald-100 text-emerald-800 border border-emerald-200 hover:bg-emerald-200' 
                                : 'bg-white text-on-surface border border-outline hover:bg-[#faf8f4]'
                            }`}
                          >
                            {isLocked ? (
                              <>
                                <Lock className="w-3 h-3" />
                                <span>Locked</span>
                              </>
                            ) : (
                              <>
                                <Lock className="w-3 h-3" />
                                <span>Lock</span>
                              </>
                            )}
                          </button>
                        ) : (
                          <Tooltip label="This requirement has no saved record yet. Save the pending merge preview first, then lock it.">
                            <button
                              disabled
                              className="px-3 py-1.5 rounded-xl text-[11px] font-bold flex items-center gap-1.5 shrink-0 cursor-not-allowed bg-slate-100 text-slate-500 border border-slate-200"
                            >
                              <Lock className="w-3.5 h-3.5" />
                              <span>Save first</span>
                            </button>
                          </Tooltip>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {activeTab === 'trace' && lockTargets.length === 0 && (
              <div className="max-w-4xl mx-auto mb-3 p-4 bg-white border border-dashed border-outline rounded-[10px]">
                <div className="flex items-start gap-3 min-w-0">
                  <Lock className="text-on-surface-variant w-4 h-4 shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <h3 className="font-bold text-sm text-on-surface">Requirement Lock Status</h3>
                    <p className="text-xs text-on-surface-variant mt-0.5 break-words">
                      {(structuredRequirements.user_stories?.length ?? 0) > 0
                        ? 'No requirement records are stored for this project yet, so there is nothing to lock. Save the pending merge preview to create the requirement records; they become lockable immediately afterwards.'
                        : 'No requirements yet. Describe what you need in the chat and the Gatherer will create the requirement records you can lock here.'}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {activeTab === 'prd' && pendingActions.some(action => action.action_type === 'GENERATE_PRD') && (
              <div className="max-w-4xl mx-auto mb-2 px-1 text-[10px] font-bold uppercase tracking-[0.08em] text-on-surface-variant">
                Current saved PRD (unchanged until preview approval)
              </div>
            )}
            <MainArtifactPanel state={state} />

          </div>
        </section>

      </div>
      )}

      {/* PROJECT 4.1 — Create UI trigger: opens NewProjectModal, whose submit is
          handleCreateProject (PROJECT 4.2 → api 4.3 → route 4.4). */}
      {/* NEW PROJECT MODAL (styled replacement for native prompt()) */}
      <NewProjectModal
        isOpen={isCreateModalOpen}
        onCreate={async (name, intake, skipIntake = false) => {
          // Do not select yet: selecting starts an async project load, which
          // previously raced the intake writes and could repaint the editor
          // with the empty template after the intake had saved.
          const result = await handleCreateProject(name, { select: false });
          if (result.error || !result.projectId) return result.error || 'Failed to create the project.';
          try {
            await seedPrdIntake(result.projectId, name, intake);
          } catch {
            return 'Project was created, but the PRD intake could not be saved. Please open the PRD sections and try again.';
          }
          setProjectId(result.projectId);
          setActiveView('workspace');
          if (skipIntake) return null;
          // Selection now starts the one authoritative project load. Defer the
          // Gatherer until that load has read the same stitched version that
          // the PRD preview renders.
          requestAnimationFrame(() => requestAnimationFrame(() => {
            void (async () => {
              await loadProjectState(result.projectId!);
              await handleSendMessage(intake.gathererBrief, result.projectId);
            })();
          }));
          return null;
        }}
        onClose={() => setIsCreateModalOpen(false)}
      />

      {/* PROJECT 6.1 — Delete UI trigger: ConfirmModal submit is
          handleDeleteProject (6.1 → api 6.2 → route 6.3); a false result keeps
          the modal open when the server refuses. */}
      {/* DELETE PROJECT CONFIRM MODAL (styled replacement for native confirm()) */}
      <ConfirmModal
        isOpen={!!projectPendingDelete}
        title="Delete Project"
        description={`Are you sure you want to delete "${projects.find(p => p.id === projectPendingDelete)?.name ?? 'this project'}"? This action cannot be undone.`}
        confirmLabel="Delete"
        pendingLabel="Deleting..."
        danger
        onConfirm={() => handleDeleteProject(projectPendingDelete as string)}
        onClose={() => setProjectPendingDelete(null)}
      />

      {/* AUTH 1.1.1 — AuthModal mount point (isOpen = isAuthModalOpen set by
          AUTH 1.1). On success useAuth stores the JWT in AUTH 1.5, so the
          sidebar chip flips to the user's name (AUTH 1.6).
          AUTH MODAL — sign in (POST /api/auth/login, AUTH 1.7) / create account
          (POST /api/auth/register, AUTH 6.3, only when the backend allows it —
          AUTH 3.3). */}
      <AuthModal
        isOpen={isAuthModalOpen}
        signupEnabled={signupEnabled}
        isAuthenticating={isAuthenticating}
        onSignIn={handleLogin}
        onSignUp={handleRegister}
        onClose={() => setIsAuthModalOpen(false)}
      />
    </div>
  );
}

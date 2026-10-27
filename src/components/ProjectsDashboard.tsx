// ProjectsDashboard — projects overview page (the "Dashboard" view).
//
// Rendered next to the project sidebar when the user switches views from the
// sidebar nav (Workspace ⇄ Dashboard). Adapts the approved dashboard mock into
// the app's existing design system (Tailwind theme tokens, lucide icons,
// Tooltip) and reuses the shared project state so sidebar and dashboard never
// disagree:
//
//   - KPI cards   -> Total projects / Pending in review / Revised or rework /
//                    Approved, all derived from the user-selected project status
//   - Tab pills   -> All / In review with HPO / In review with PO / Draft /
//                    Approved / Revised / ★ Flagged (dashboard-only marker —
//                    independent of the sidebar pin)
//   - Table rows  -> star = flag toggle (separate is_flagged field), user-editable
//                    workflow status badge (PUT /api/projects/{id}/status),
//                    relative "Updated" time, and an Open action that selects
//                    the project and returns to the workspace view.
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  History,
  LayoutGrid,
  RotateCcw,
  Search,
  Star,
  X,
  XCircle,
} from 'lucide-react';
import type { ProjectState } from '../hooks/useProjectState';
import type { AuditRunPayload, AuditWaiverPayload, ProjectHealthPayload, ProjectReviewEventPayload, ProjectStatus } from '../api/types';
import { api } from '../api/client';
import { Tooltip } from './Tooltip';
import { handleError } from './Toast';
import { formatPrdVersionLabel } from '../utils/version';
import { formatRelativeUpdated } from '../utils/time';

// ----------------------------------------------------------------------------
// Status metadata (kept in one place so tabs, dropdowns, and badges agree)
// ----------------------------------------------------------------------------

const STATUS_OPTIONS: ReadonlyArray<{ value: ProjectStatus; label: string }> = [
  { value: 'draft', label: 'Draft' },
  { value: 'in_review_hpo', label: 'In review: HPO' },
  { value: 'in_review_po', label: 'In review: PO' },
  { value: 'approved', label: 'Approved' },
  { value: 'revised', label: 'Revised' },
];

const STATUS_LABEL: Record<ProjectStatus, string> = Object.fromEntries(
  STATUS_OPTIONS.map((s) => [s.value, s.label]),
) as Record<ProjectStatus, string>;

/** Badge color classes per status (amber = in review, green = approved, navy = revised). */
const STATUS_BADGE: Record<ProjectStatus, string> = {
  draft: 'bg-black/5 text-on-surface-variant',
  in_review_hpo: 'bg-amber-100 text-amber-800',
  in_review_po: 'bg-amber-100 text-amber-800',
  approved: 'bg-emerald-100 text-emerald-800',
  revised: 'bg-navy-100 text-navy-700',
};

/** Projects created before the status column default to 'draft' at the UI layer. */
const normalizeStatus = (status: string | undefined): ProjectStatus =>
  (STATUS_OPTIONS.some((s) => s.value === status) ? (status as ProjectStatus) : 'draft');

function ApprovalTime({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  const approvedAt = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));

  return (
    <span className="mt-1 block text-[11px] text-on-surface-variant">Approved {approvedAt}</span>
  );
}

// Tab pills: filter shortcuts over the same dimensions as the dropdowns.
type TableTab = 'all' | ProjectStatus | 'flagged';

const TABLE_TABS: ReadonlyArray<{ key: TableTab; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'in_review_hpo', label: 'In review with HPO' },
  { key: 'in_review_po', label: 'In review with PO' },
  { key: 'draft', label: 'Draft' },
  { key: 'approved', label: 'Approved' },
  { key: 'revised', label: 'Revised' },
  { key: 'flagged', label: '★ Flagged' },
];

type SortKey = 'updated' | 'name';

// ----------------------------------------------------------------------------
// Dropdown — minimal styled select matching the mock's pill triggers.
// Closes on outside click / Escape; owns only its open flag.
// ----------------------------------------------------------------------------

interface DropdownProps<T extends string> {
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (value: T) => void;
  ariaLabel: string;
}

function Dropdown<T extends string>({ value, options, onChange, ariaLabel }: DropdownProps<T>) {
  const [open, setOpen] = useState<boolean>(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handlePointer = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handlePointer);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handlePointer);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  const current = options.find((o) => o.value === value);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={ariaLabel}
        aria-expanded={open}
        className="h-11 flex items-center gap-2 px-4 bg-surface border border-outline rounded-[10px] text-[13px] font-semibold text-on-surface hover:border-primary/40 transition-colors whitespace-nowrap"
      >
        <span>{current?.label}</span>
        <ChevronDown className={`w-3.5 h-3.5 text-on-surface-variant transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1.5 z-30 min-w-[190px] bg-surface border border-outline rounded-xl shadow-lg py-1">
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
              className={`w-full text-left px-3.5 py-2 text-[13px] transition-colors flex items-center justify-between gap-3 ${
                o.value === value ? 'text-primary font-semibold bg-primary/5' : 'text-on-surface hover:bg-black/5'
              }`}
            >
              <span>{o.label}</span>
              {o.value === value && <Check className="w-3.5 h-3.5 shrink-0" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Pagination — flat "10 per page" over the current filtered/sorted rows. Prev /
// Next arrows frame a windowed number list (first, last, current ±1, ellipsis
// for gaps) so a large list never floods the footer with buttons.
// ----------------------------------------------------------------------------

const PAGE_SIZE = 10;

function paginationItems(
  current: number,
  total: number,
): Array<number | 'ellipsis'> {
  // Few pages: show every number. Otherwise collapse the middle into a window
  // of current ±1 framed by the first/last page, with ellipsis for the gap.
  if (total <= 7) return Array.from({ length: total }, (_, i) => i);
  if (current <= 2) return [0, 1, 2, 3, 'ellipsis', total - 1];
  if (current >= total - 3) return [0, 'ellipsis', total - 4, total - 3, total - 2, total - 1];
  return [0, 'ellipsis', current - 1, current, current + 1, 'ellipsis', total - 1];
}

function PaginationControls({
  totalRows,
  page,
  onPageChange,
}: {
  totalRows: number;
  page: number;
  onPageChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(totalRows / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const from = safePage * PAGE_SIZE + 1;
  const to = Math.min(totalRows, (safePage + 1) * PAGE_SIZE);

  return (
    <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-5 py-3.5 border-t border-outline">
      <span className="text-[12.5px] text-on-surface-variant whitespace-nowrap">
        Showing {from} to {to} of {totalRows} {totalRows === 1 ? 'project' : 'projects'}
      </span>
      <nav className="flex items-center gap-1" aria-label="Pagination">
        <button
          type="button"
          onClick={() => onPageChange(safePage - 1)}
          disabled={safePage === 0}
          aria-label="Previous page"
          className={`w-8 h-8 inline-flex items-center justify-center rounded-md transition-colors ${
            safePage === 0 ? 'text-on-surface-variant opacity-40 cursor-not-allowed' : 'text-on-surface hover:bg-black/5'
          }`}
        >
          <ChevronLeft className="w-3.5 h-3.5" />
        </button>
        {paginationItems(safePage, totalPages).map((item, idx) =>
          item === 'ellipsis' ? (
            <span
              key={`ellipsis-${idx}`}
              className="w-8 h-8 inline-flex items-center justify-center text-[12.5px] text-on-surface-variant"
            >
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              onClick={() => onPageChange(item)}
              aria-label={`Page ${item + 1}`}
              aria-current={safePage === item ? 'page' : undefined}
              className={`w-8 h-8 inline-flex items-center justify-center rounded-md text-[12.5px] font-semibold transition-colors ${
                safePage === item ? 'bg-primary text-on-primary' : 'text-on-surface hover:bg-black/5'
              }`}
            >
              {item + 1}
            </button>
          ),
        )}
        <button
          type="button"
          onClick={() => onPageChange(safePage + 1)}
          disabled={safePage >= totalPages - 1}
          aria-label="Next page"
          className={`w-8 h-8 inline-flex items-center justify-center rounded-md transition-colors ${
            safePage >= totalPages - 1 ? 'text-on-surface-variant opacity-40 cursor-not-allowed' : 'text-on-surface hover:bg-black/5'
          }`}
        >
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </nav>
    </div>
  );
}

// ----------------------------------------------------------------------------
// ProjectsDashboard
// ----------------------------------------------------------------------------

export function ProjectsDashboard({ state }: { state: ProjectState }) {
  const {
    projects,
    projectId,
    setProjectId,
    handleToggleFlag,
    handleUpdateProjectStatus,
    setActiveView,
    setActiveTab,
  } = state;

  // ---- Table filters (pure presentation state) -----------------------------
  const [query, setQuery] = useState<string>('');
  const [sortKey, setSortKey] = useState<SortKey>('updated');
  const [activeTab, setDashboardTab] = useState<TableTab>('all');

  // ---- KPI cards -------------------------------------------------------------
  const kpis = useMemo(() => {
    const inReview = projects.filter((p) => {
      const s = normalizeStatus(p.status);
      return s === 'in_review_hpo' || s === 'in_review_po';
    }).length;
    const revised = projects.filter((p) => normalizeStatus(p.status) === 'revised').length;
    const approved = projects.filter((p) => normalizeStatus(p.status) === 'approved').length;
    return [
      { icon: LayoutGrid, value: String(projects.length), label: 'Total Projects', iconClass: 'bg-blue-50 text-blue-600' },
      { icon: AlertTriangle, value: String(inReview), label: 'Pending in Review', iconClass: 'bg-amber-50 text-amber-600' },
      { icon: RotateCcw, value: String(revised), label: 'Revised / Rework', iconClass: 'bg-orange-50 text-orange-600' },
      { icon: CheckCircle2, value: String(approved), label: 'Approved', iconClass: 'bg-emerald-50 text-emerald-600' },
    ];
  }, [projects]);

  // ---- Filter + sort pipeline -------------------------------------------------
  const visibleRows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = projects.filter((p) => {
      if (q && !p.name.toLowerCase().includes(q) && !(p.description ?? '').toLowerCase().includes(q)) return false;
      if (activeTab === 'flagged' && !p.is_flagged) return false;
      if (activeTab !== 'all' && activeTab !== 'flagged' && normalizeStatus(p.status) !== activeTab) return false;
      return true;
    });
    return [...rows].sort((a, b) => {
      if (sortKey === 'name') return a.name.localeCompare(b.name);
      const ta = new Date(a.updated_at ?? 0).getTime();
      const tb = new Date(b.updated_at ?? 0).getTime();
      return tb - ta;
    });
  }, [projects, query, activeTab, sortKey]);

  // ---- Pagination (10 per page) ---------------------------------------------
  // `safePage` clamps the raw page state to the last valid page so the slice is
  // never out of range; the effects below reset to page 1 when the filter/sort
  // inputs change and re-clamp when the list shrinks (e.g. a project is deleted).
  const [page, setPage] = useState<number>(0);
  const totalPages = Math.max(1, Math.ceil(visibleRows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageRows = useMemo(
    () => visibleRows.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE),
    [visibleRows, safePage],
  );

  useEffect(() => {
    setPage(0);
  }, [query, activeTab, sortKey]);

  useEffect(() => {
    setPage((cur) => Math.min(cur, totalPages - 1));
  }, [totalPages]);

  // Open a project from the table: select it and jump back into the workspace.
  // PROJECT 2.1 — Dashboard row open: same selection trigger as the sidebar
  //              (setProjectId → useProjectSync effect → PROJECT 2.2).
  const handleOpenProject = (id: string) => {
    setProjectId(id);
    setActiveView('workspace');
  };

  // ---- Per-row status menu (user-editable workflow status) -------------------
  // PROJECT 7.5.1 / 7.4.1 — Per-row metadata menu (UI triggers): the status
  // dropdown calls handleUpdateProjectStatus (7.5.1 → api 7.5.2 → route 7.5.3)
  // and the ★ button calls handleToggleFlag (7.4.1 → api 7.4.2 → route 7.4.3).
  const [statusMenuFor, setStatusMenuFor] = useState<string | null>(null);
  const [historyProject, setHistoryProject] = useState<{ id: string; name: string } | null>(null);
  const [reviewHistory, setReviewHistory] = useState<ProjectReviewEventPayload[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [healthProject, setHealthProject] = useState<{ id: string; name: string } | null>(null);
  const [healthDetail, setHealthDetail] = useState<ProjectHealthPayload | null>(null);
  const [auditHistory, setAuditHistory] = useState<AuditRunPayload[]>([]);
  const [waivers, setWaivers] = useState<AuditWaiverPayload[]>([]);
  const [healthLoading, setHealthLoading] = useState(false);
  const [waiverFinding, setWaiverFinding] = useState<AuditRunPayload['findings'][number] | null>(null);
  const [waiverReason, setWaiverReason] = useState('');
  const [waiverControl, setWaiverControl] = useState('');
  const [waiverOwner, setWaiverOwner] = useState('Project owner');

  const openHealth = async (id: string, name: string) => {
    setHealthProject({ id, name });
    setHealthLoading(true);
    const [health, history, waiverRows] = await Promise.all([
      api.getProjectHealth(id).catch(() => null),
      api.getAuditHistory(id).catch(() => []),
      api.getAuditWaivers(id).catch(() => []),
    ]);
    setHealthDetail(health);
    setAuditHistory(history);
    setWaivers(waiverRows);
    setHealthLoading(false);
  };

  const openReviewHistory = async (id: string, name: string) => {
    setStatusMenuFor(null);
    setHistoryProject({ id, name });
    setHistoryLoading(true);
    try {
      setReviewHistory(await api.getProjectReviewHistory(id));
    } catch {
      setReviewHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    if (!statusMenuFor) return;
    const handlePointer = (e: MouseEvent) => {
      const target = e.target;
      if (target instanceof Element && target.closest('[data-status-menu]')) return;
      setStatusMenuFor(null);
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setStatusMenuFor(null);
    };
    document.addEventListener('mousedown', handlePointer);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handlePointer);
      document.removeEventListener('keydown', handleKey);
    };
  }, [statusMenuFor]);

  return (
    <section className="flex-1 min-w-0 min-h-0 flex flex-col bg-background overflow-hidden">
      <div className="flex-1 overflow-y-auto custom-scrollbar">
        <div id="projects-dashboard-content" className="w-full max-w-6xl mx-auto px-4 sm:px-6 md:px-10 py-8">
          <h1 className="text-[24px] font-bold text-on-surface mb-7">Dashboard</h1>

          {/* TOP SEARCH AND FILTERS */}
          <div className="flex flex-wrap items-center gap-3 mb-6">
            <div className="flex-1 min-w-[220px] flex items-center gap-2.5 bg-surface border border-outline rounded-[10px] px-4 h-11 focus-within:border-primary/50 transition-colors">
              <Search className="w-4 h-4 text-on-surface-variant shrink-0" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') setQuery('');
                }}
                placeholder="Search projects or document hash..."
                className="flex-1 bg-transparent text-[14px] text-on-surface placeholder:text-on-surface-variant focus:outline-none min-w-0"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery('')}
                  className="text-slate-400 hover:text-slate-600 transition-colors shrink-0"
                  aria-label="Clear search"
                >
                  <XCircle className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <Dropdown<SortKey>
              ariaLabel="Sort projects"
              value={sortKey}
              onChange={setSortKey}
              options={[
                { value: 'updated' as const, label: 'Sort by: Updated' },
                { value: 'name' as const, label: 'Sort by: Name' },
              ]}
            />
          </div>

          {/* KPI METRICS GRID */}
          <div className="flex flex-wrap sm:flex-nowrap items-stretch gap-3 sm:gap-4 mb-9">
            {kpis.map(({ icon: Icon, value, label, iconClass }) => (
              <div
                key={label}
                className="basis-[calc(50%_-_0.375rem)] sm:basis-0 sm:flex-1 min-w-0 bg-surface border border-outline rounded-[12px] p-4 sm:p-5 flex flex-col items-start"
              >
                <div className={`w-9 h-9 rounded-lg flex items-center justify-center mb-3 sm:mb-4 ${iconClass}`}>
                  <Icon className="w-4.5 h-4.5" />
                </div>
                <div className="text-[24px] sm:text-[28px] font-bold leading-tight text-on-surface">{value}</div>
                <div className="text-[13px] text-on-surface-variant mt-1 leading-snug">{label}</div>
              </div>
            ))}
          </div>

          {/* PROJECTS TABLE CONTAINER */}
          <div className="bg-surface border border-outline rounded-[12px] overflow-hidden">
            {/* HEADER TABS */}
            <div className="flex flex-wrap items-center gap-2 px-5 py-3 border-b border-outline">
              <span className="text-[15px] font-bold text-on-surface mr-2">Projects</span>
              <div className="flex flex-wrap items-center gap-0.5 bg-black/5 rounded-lg p-1">
                {TABLE_TABS.map((tab) => (
                  <button
                    key={tab.key}
                    type="button"
                    onClick={() => setDashboardTab(tab.key)}
                    className={`px-3 py-1.5 rounded-md text-[12.5px] font-semibold transition-all whitespace-nowrap ${
                      activeTab === tab.key
                        ? 'bg-surface text-on-surface shadow-sm'
                        : 'text-on-surface-variant hover:text-on-surface'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
            </div>

            {/* TABLE CONTENT — md and up: the table scrolls horizontally inside
                this container only, never the page. Below md the same rows are
                rendered as stacked cards (see below) so no control is clipped. */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr>
                    <th className="w-12 px-5 py-3 text-[12px] font-semibold text-on-surface-variant border-b border-outline">
                      <Star className="w-3.5 h-3.5" aria-label="Flagged" />
                    </th>
                    <th className="px-5 py-3 text-[12px] font-semibold text-on-surface-variant border-b border-outline">
                      Project
                    </th>
                    <th className="px-5 py-3 text-[12px] font-semibold text-on-surface-variant border-b border-outline">
                      Status
                    </th>
                    <th className="px-5 py-3 text-[12px] font-semibold text-on-surface-variant border-b border-outline">
                      Updated
                    </th>
                    <th className="px-5 py-3 border-b border-outline" aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map((p) => {
                    const status = normalizeStatus(p.status);
                    return (
                      <tr
                        key={p.id}
                        className={`transition-colors hover:bg-black/[0.02] ${p.id === projectId ? 'bg-primary/[0.04]' : ''}`}
                      >
                        <td className="px-5 py-4 border-b border-outline">
                          <Tooltip label={p.is_flagged ? 'Unflag' : 'Flag'}>
                            <button
                              type="button"
                              onClick={() => handleToggleFlag(p.id)}
                              aria-label={p.is_flagged ? 'Unflag project' : 'Flag project'}
                              className={`transition-colors ${p.is_flagged ? 'text-amber-500' : 'text-slate-300 hover:text-amber-500'}`}
                            >
                              <Star className="w-4 h-4" fill={p.is_flagged ? 'currentColor' : 'none'} />
                            </button>
                          </Tooltip>
                        </td>
                        <td className="px-5 py-4 border-b border-outline max-w-[280px]">
                          <span className="font-bold text-[14px] text-on-surface block truncate" title={p.name}>
                            {p.name}
                          </span>
                          {p.description && (
                            <span className="mt-0.5 block truncate text-[12px] text-on-surface-variant" title={p.description}>
                              {p.description}
                            </span>
                          )}
                          <ApprovalTime value={p.last_approved_at} />
                        </td>
                        <td className="px-5 py-4 border-b border-outline">
                          <div className="relative" data-status-menu>
                            <button
                              type="button"
                              onClick={() => setStatusMenuFor((cur) => (cur === p.id ? null : p.id))}
                              aria-label={`Change status for ${p.name}`}
                              aria-expanded={statusMenuFor === p.id}
                              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] font-semibold transition-all hover:brightness-95 ${STATUS_BADGE[status]}`}
                            >
                              {STATUS_LABEL[status]}
                              <ChevronDown className="w-3 h-3" />
                            </button>
                            {statusMenuFor === p.id && (
                              <div className="absolute left-0 top-full mt-1.5 z-30 min-w-[180px] bg-surface border border-outline rounded-xl shadow-lg py-1">
                                {STATUS_OPTIONS.map((o) => {
                                  const isCurrent = o.value === status;
                                  return (
                                  <button
                                    key={o.value}
                                    type="button"
                                    disabled={isCurrent}
                                    onClick={() => {
                                      setStatusMenuFor(null);
                                      if (!isCurrent) void handleUpdateProjectStatus(p.id, o.value);
                                    }}
                                    className={`w-full text-left px-3.5 py-2 text-[13px] transition-colors flex items-center justify-between gap-3 ${
                                      isCurrent
                                        ? 'text-primary font-semibold bg-primary/5'
                                        : 'text-on-surface hover:bg-black/5'
                                    }`}
                                  >
                                    <span>{o.label}</span>
                                    {isCurrent && <Check className="w-3.5 h-3.5 shrink-0" />}
                                  </button>
                                  );
                                })}
                                <button
                                  type="button"
                                  onClick={() => void openReviewHistory(p.id, p.name)}
                                  className="w-full border-t border-outline px-3.5 py-2 text-left text-[13px] text-on-surface hover:bg-black/5 flex items-center gap-2"
                                >
                                  <History className="w-3.5 h-3.5" /> Review history
                                </button>
                              </div>
                            )}
                          </div>
                        </td>
                        <td className="px-5 py-4 border-b border-outline">
                          <span className="text-[13px] text-on-surface-variant whitespace-nowrap">
                            {formatRelativeUpdated(p.updated_at)}
                          </span>
                        </td>
                        <td className="px-5 py-4 border-b border-outline text-right">
                          <button
                            type="button"
                            onClick={() => handleOpenProject(p.id)}
                            className="inline-flex items-center gap-1 bg-surface border border-outline rounded-lg px-3.5 py-1.5 text-[13px] font-semibold text-on-surface hover:bg-black/5 transition-colors whitespace-nowrap"
                          >
                            Open
                            <ChevronRight className="w-3 h-3" />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* MOBILE PROJECT CARDS (< md): identical data to the table, stacked
                vertically so the flag, status, updated, and Open controls all
                remain reachable without horizontal scrolling. */}
            <div className="md:hidden divide-y divide-outline">
              {pageRows.map((p) => {
                const status = normalizeStatus(p.status);
                return (
                  <div key={p.id} className={`p-4 ${p.id === projectId ? 'bg-primary/[0.04]' : ''}`}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5 min-w-0">
                        <Tooltip label={p.is_flagged ? 'Unflag' : 'Flag'}>
                          <button
                            type="button"
                            onClick={() => handleToggleFlag(p.id)}
                            aria-label={p.is_flagged ? 'Unflag project' : 'Flag project'}
                            className={`mt-0.5 transition-colors ${p.is_flagged ? 'text-amber-500' : 'text-slate-300 hover:text-amber-500'}`}
                          >
                            <Star className="w-4 h-4" fill={p.is_flagged ? 'currentColor' : 'none'} />
                          </button>
                        </Tooltip>
                        <div className="min-w-0">
                          <span className="font-bold text-[14px] text-on-surface block break-words">{p.name}</span>
                          {p.description && <span className="mt-0.5 block text-[12px] text-on-surface-variant break-words">{p.description}</span>}
                          <ApprovalTime value={p.last_approved_at} />
                          <span className="text-[12px] text-on-surface-variant">
                            {formatRelativeUpdated(p.updated_at)}
                          </span>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleOpenProject(p.id)}
                        className="shrink-0 inline-flex items-center gap-1 bg-surface border border-outline rounded-lg px-3 py-1.5 text-[12px] font-semibold text-on-surface hover:bg-black/5 transition-colors whitespace-nowrap"
                      >
                        Open
                        <ChevronRight className="w-3 h-3" />
                      </button>
                    </div>
                    <div className="relative mt-3 inline-block max-w-full" data-status-menu>
                      <button
                        type="button"
                        onClick={() => setStatusMenuFor((cur) => (cur === p.id ? null : p.id))}
                        aria-label={`Change status for ${p.name}`}
                        aria-expanded={statusMenuFor === p.id}
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] font-semibold transition-all hover:brightness-95 max-w-full ${STATUS_BADGE[status]}`}
                      >
                        <span className="truncate">{STATUS_LABEL[status]}</span>
                        <ChevronDown className="w-3 h-3 shrink-0" />
                      </button>
                      {statusMenuFor === p.id && (
                        <div className="absolute left-0 top-full mt-1.5 z-30 min-w-[180px] bg-surface border border-outline rounded-xl shadow-lg py-1">
                          {STATUS_OPTIONS.map((o) => {
                            const isCurrent = o.value === status;
                            return (
                            <button
                              key={o.value}
                              type="button"
                              disabled={isCurrent}
                              onClick={() => {
                                setStatusMenuFor(null);
                                if (!isCurrent) void handleUpdateProjectStatus(p.id, o.value);
                              }}
                              className={`w-full text-left px-3.5 py-2 text-[13px] transition-colors flex items-center justify-between gap-3 ${
                                isCurrent
                                  ? 'text-primary font-semibold bg-primary/5'
                                  : 'text-on-surface hover:bg-black/5'
                              }`}
                            >
                              <span>{o.label}</span>
                              {isCurrent && <Check className="w-3.5 h-3.5 shrink-0" />}
                            </button>
                            );
                          })}
                          <button
                            type="button"
                            onClick={() => void openReviewHistory(p.id, p.name)}
                            className="w-full border-t border-outline px-3.5 py-2 text-left text-[13px] text-on-surface hover:bg-black/5 flex items-center gap-2"
                          >
                            <History className="w-3.5 h-3.5" /> Review history
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {visibleRows.length === 0 && (
              <div className="px-5 py-12 text-center text-[13px] text-on-surface-variant">
                {projects.length === 0
                  ? 'No projects yet. Create one from the sidebar to see it here.'
                  : 'No projects match the current filters.'}
              </div>
            )}

            {visibleRows.length > 0 && (
              <PaginationControls totalRows={visibleRows.length} page={safePage} onPageChange={setPage} />
            )}
          </div>

        </div>
      </div>
      {historyProject && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true">
          <div className="w-full max-w-xl rounded-2xl border border-outline bg-surface shadow-xl">
            <div className="flex items-start justify-between border-b border-outline px-5 py-4">
              <div>
                <h2 className="font-bold text-on-surface">Review history</h2>
                <p className="text-xs text-on-surface-variant">{historyProject.name}</p>
              </div>
              <button type="button" aria-label="Close review history" onClick={() => setHistoryProject(null)} className="p-1 text-on-surface-variant hover:text-on-surface">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="max-h-[60vh] overflow-y-auto p-5 space-y-3">
              {historyLoading ? (
                <p className="text-sm text-on-surface-variant">Loading review history…</p>
              ) : reviewHistory.length === 0 ? (
                <p className="text-sm text-on-surface-variant">No review transitions have been recorded yet.</p>
              ) : reviewHistory.map((event) => (
                <div key={event.id} className="rounded-xl border border-outline p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-on-surface">
                      {STATUS_LABEL[event.from_status]} to {STATUS_LABEL[event.to_status]}
                    </span>
                    <span className="text-[11px] text-on-surface-variant">
                      {event.created_at ? new Date(event.created_at).toLocaleString() : ''}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-on-surface-variant">
                    {event.actor_name}, {event.actor_role}
                    {event.prd_version_number ? `, PRD ${formatPrdVersionLabel(event.prd_semver, event.prd_version_number)}` : ''}
                    {event.audit_version_reviewed ? `, Audit revision ${event.audit_version_reviewed}` : ''}
                  </p>
                  {event.comment && <p className="mt-2 text-sm text-on-surface">{event.comment}</p>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      {healthProject && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
          <div className="w-full max-w-3xl rounded-2xl border border-outline bg-surface shadow-xl">
            <div className="flex items-start justify-between border-b border-outline px-5 py-4">
              <div>
                <h2 className="font-bold text-on-surface">Project health</h2>
                <p className="text-xs text-on-surface-variant">{healthProject.name}</p>
              </div>
              <button type="button" aria-label="Close project health" onClick={() => setHealthProject(null)}><X className="h-4 w-4" /></button>
            </div>
            <div className="max-h-[75vh] overflow-y-auto p-5">
              {healthLoading || !healthDetail ? <p className="text-sm text-on-surface-variant">Loading project health…</p> : (
                <div className="space-y-5">
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    {[
                      ['Fully traced requirements', `${healthDetail.coverage_breakdown.requirement_traceability_percent}%`],
                      ['Blocking', healthDetail.metrics.blocking_findings],
                      ['Open questions', healthDetail.metrics.unresolved_questions],
                      ['Active waivers', healthDetail.metrics.active_waivers],
                    ].map(([label, value]) => (
                      <div key={label} className="rounded-xl border border-outline p-3">
                        <p className="text-[11px] text-on-surface-variant">{label}</p>
                        <p className="mt-1 text-xl font-bold text-on-surface">{value}</p>
                      </div>
                    ))}
                  </div>

                  <div>
                    <h3 className="text-sm font-bold text-on-surface">Outstanding issues</h3>
                    {healthDetail.issues.length === 0 ? <p className="mt-2 text-sm text-emerald-700">No outstanding project-health issue.</p> : (
                      <div className="mt-2 space-y-2">{healthDetail.issues.map((issue) => (
                        <button
                          key={issue.kind}
                          type="button"
                          onClick={() => {
                            setProjectId(healthProject.id);
                            if (issue.destination === 'traceability') setActiveTab('trace');
                            else if (issue.destination === 'prd') setActiveTab('prd');
                            setActiveView('workspace');
                            setHealthProject(null);
                          }}
                          className="flex w-full items-center justify-between rounded-xl border border-outline p-3 text-left hover:bg-black/[0.02]"
                        >
                          <span className="text-sm text-on-surface">{issue.label}</span>
                          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-bold text-amber-700">{issue.count}</span>
                        </button>
                      ))}</div>
                    )}
                  </div>

                  <div>
                    <h3 className="text-sm font-bold text-on-surface">Audit history</h3>
                    {auditHistory.length === 0 ? <p className="mt-2 text-sm text-on-surface-variant">No confirmed audit run yet.</p> : auditHistory.slice(0, 5).map((run) => (
                      <div key={run.id} className="mt-2 rounded-xl border border-outline p-3">
                        <div className="flex flex-wrap justify-between gap-2 text-xs">
                          <span className="font-bold text-on-surface">Audit revision {run.audit_version_reviewed}: {run.verdict}</span>
                          <span className="text-on-surface-variant">{run.created_at ? new Date(run.created_at).toLocaleString() : ''}</span>
                        </div>
                        <p className="mt-1 text-[11px] text-on-surface-variant">
                          {run.comparison.new.length} new, {run.comparison.reopened.length} reopened, {run.comparison.resolved.length} resolved
                        </p>
                        {run.findings.map((finding) => (
                          <div key={finding.finding_key} className="mt-2 border-t border-outline pt-2 text-xs">
                            <div className="flex items-start justify-between gap-3">
                              <p><span className="font-bold uppercase">{finding.lifecycle}</span>, {finding.rule_id}: {finding.description}</p>
                              {!finding.waiver && (
                                <button type="button" onClick={() => { setWaiverFinding(finding); setWaiverReason(''); setWaiverControl(''); }} className="shrink-0 text-primary font-semibold">Waive</button>
                              )}
                            </div>
                            {finding.waiver && <p className="mt-1 text-emerald-700">Waived until {new Date(finding.waiver.expires_at).toLocaleDateString()}</p>}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>

                  {waivers.length > 0 && (
                    <div>
                      <h3 className="text-sm font-bold text-on-surface">Waivers</h3>
                      {waivers.map((waiver) => <p key={waiver.id} className="mt-1 text-xs text-on-surface-variant">{waiver.rule_id}, {waiver.status}, {waiver.owner}</p>)}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      {waiverFinding && healthProject && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/45 p-4" role="dialog" aria-modal="true">
          <div className="w-full max-w-md rounded-2xl bg-surface p-5 shadow-xl">
            <h2 className="font-bold text-on-surface">Create finding waiver</h2>
            <p className="mt-1 text-xs text-on-surface-variant">{waiverFinding.rule_id}: {waiverFinding.target_requirement_id || 'Project level'}</p>
            <input value={waiverOwner} onChange={(e) => setWaiverOwner(e.target.value)} placeholder="Waiver owner" className="mt-4 w-full rounded-xl border border-outline p-3 text-sm" />
            <textarea value={waiverReason} onChange={(e) => setWaiverReason(e.target.value)} placeholder="Business reason" rows={3} className="mt-2 w-full rounded-xl border border-outline p-3 text-sm" />
            <textarea value={waiverControl} onChange={(e) => setWaiverControl(e.target.value)} placeholder="Compensating control (optional)" rows={2} className="mt-2 w-full rounded-xl border border-outline p-3 text-sm" />
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={() => setWaiverFinding(null)} className="px-3 py-2 text-xs font-semibold">Cancel</button>
              <button
                type="button"
                disabled={waiverReason.trim().length < 3 || !waiverOwner.trim()}
                onClick={async () => {
                  const expiry = new Date();
                  expiry.setDate(expiry.getDate() + 30);
                  try {
                    await api.createAuditWaiver(healthProject.id, {
                      rule_id: waiverFinding.rule_id,
                      target_requirement_id: waiverFinding.target_requirement_id,
                      reason: waiverReason.trim(),
                      compensating_control: waiverControl.trim() || undefined,
                      owner: waiverOwner.trim(),
                      expires_at: expiry.toISOString(),
                    });
                    setWaiverFinding(null);
                    await openHealth(healthProject.id, healthProject.name);
                  } catch (error) {
                    handleError('Could not create the audit waiver.', error);
                  }
                }}
                className="rounded-xl bg-primary px-4 py-2 text-xs font-bold text-on-primary disabled:opacity-50"
              >Create 30-day waiver</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

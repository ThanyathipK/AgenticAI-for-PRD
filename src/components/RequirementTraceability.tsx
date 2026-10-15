// RequirementTraceability — the derived Requirement Traceability Matrix tab.
//
// Fetches GET /api/project/{id}/traceability and renders:
//   - a coverage summary (traced vs total requirements, stories, criteria)
//   - an explicit coverage-gap panel (the actionable part of traceability)
//   - the matrix: one row per Requirement tracing to its User Stories,
//     Acceptance Criteria, PRD sections and diagrams
// All links are computed server-side (backend/app/traceability_service.py);
// this tab is read-only.
import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircle2,
  GitBranch,
  Loader2,
  Network,
  RefreshCw,
  ScrollText,
  ShieldAlert,
  Sparkles,
  UserCircle2,
} from 'lucide-react';
import { api, detailFromJsonError } from '../api/client';
import type {
  StaleCodeReference,
  TraceabilityPayload,
  TraceabilityRow,
  DependencyGraphPayload,
  RegenerationPlanPayload,
} from '../api/types';
import { handleError, notify } from './Toast';

interface RequirementTraceabilityProps {
  projectId: string | null;
}

const chip = (label: string, className: string): React.ReactNode => (
  <span className={`px-2.5 py-0.5 rounded border font-bold font-mono text-[10.5px] ${className}`}>
    {label}
  </span>
);

const tracedChip = (traced: boolean): React.ReactNode =>
  traced
    ? chip('traced', 'bg-emerald-100 text-emerald-800 border-emerald-300')
    : chip('gaps', 'bg-amber-100 text-amber-800 border-amber-300');

const gapChips = (codes: string[]): React.ReactNode =>
  codes.map((code) => chip(code, 'bg-slate-100 text-slate-700 border-slate-300'));

const staleChips = (refs: StaleCodeReference[]): React.ReactNode =>
  refs.map((ref) => (
    <span
      key={ref.code}
      className="px-2.5 py-0.5 rounded bg-rose-50 text-rose-700 border border-rose-200 font-bold font-mono text-[10.5px]"
    >
      {ref.code} maps to {[...(ref.section_keys ?? []), ...(ref.diagrams ?? [])].join(', ')}
    </span>
  ));


const RowCard: React.FC<{ row: TraceabilityRow }> = ({ row }) => {
  const { requirement: req } = row;
  return (
    <div className="bg-white border border-outline rounded-[10px] p-3 space-y-3">
      {/* Requirement node */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2.5 min-w-0">
          <GitBranch className="w-4 h-4 text-primary shrink-0" />
          <span className="text-xs font-mono font-bold text-on-surface">{req.requirement_code}</span>
          <span className="text-sm font-semibold text-on-surface truncate">{req.title}</span>
          {req.epic_name && (
            <span className="text-[10.5px] text-on-surface-variant truncate">
              ({req.epic_name})
            </span>
          )}
        </div>
        {tracedChip(row.traced)}
      </div>

      {/* Traced artifacts */}
      <div className="grid md:grid-cols-3 gap-3">
        {/* User stories + acceptance criteria */}
        <div className="bg-[#faf8f4] border border-outline rounded-[8px] p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-[10.5px] font-bold uppercase tracking-wider text-on-surface-variant">
            <UserCircle2 className="w-3.5 h-3.5" /> User Stories ({row.user_stories.length})
          </div>
          {row.user_stories.length === 0 && (
            <p className="text-xs text-rose-600 font-medium">No user story implements this requirement.</p>
          )}
          {row.user_stories.map((story) => (
            <div key={story.ticket_code} className="space-y-1">
              <div className="flex items-center gap-1.5">
                <span className="text-[10.5px] font-mono font-bold text-primary">{story.ticket_code}</span>
                <span className="text-xs text-on-surface-variant truncate">{story.story_title}</span>
              </div>
              {story.acceptance_criteria.length === 0 ? (
                <p className="text-[10.5px] text-amber-700 pl-2">No acceptance criteria</p>
              ) : (
                <ul className="pl-2 space-y-0.5">
                  {story.acceptance_criteria.map((ac, idx) => (
                    <li key={idx} className="text-[10.5px] text-on-surface-variant leading-snug list-disc ml-2">
                      {ac}
                      {story.criteria_needing_review?.includes(ac) && (
                        <span className="ml-1.5 rounded border border-amber-300 bg-amber-50 px-1 py-0.5 text-[9px] font-bold uppercase text-amber-700">
                          Review required
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>

        {/* PRD sections */}
        <div className="bg-[#faf8f4] border border-outline rounded-[8px] p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-[10.5px] font-bold uppercase tracking-wider text-on-surface-variant">
            <ScrollText className="w-3.5 h-3.5" /> PRD Sections ({row.prd_sections.length})
          </div>
          {row.prd_sections.length === 0 ? (
            <p className="text-xs text-amber-700 font-medium">No PRD section references this requirement.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {row.prd_sections.map((key) => chip(key, 'bg-slate-100 text-slate-700 border-slate-300'))}
            </div>
          )}
        </div>

        {/* Diagrams */}
        <div className="bg-[#faf8f4] border border-outline rounded-[8px] p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-[10.5px] font-bold uppercase tracking-wider text-on-surface-variant">
            <Network className="w-3.5 h-3.5" /> Diagrams ({row.diagrams.length})
          </div>
          {row.diagrams.length === 0 ? (
            <p className="text-xs text-on-surface-variant italic">No diagram references this requirement.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {row.diagrams.map((label) => chip(label, 'bg-blue-50 text-blue-800 border-blue-200'))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};


export const RequirementTraceability: React.FC<RequirementTraceabilityProps> = ({ projectId }) => {
  const [matrix, setMatrix] = useState<TraceabilityPayload | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dependencyGraph, setDependencyGraph] = useState<DependencyGraphPayload | null>(null);
  const [selectedArtifacts, setSelectedArtifacts] = useState<Set<string>>(new Set());
  const [regenerationPlan, setRegenerationPlan] = useState<RegenerationPlanPayload | null>(null);
  const [isPlanning, setIsPlanning] = useState(false);
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [regenerationJobId, setRegenerationJobId] = useState<string | null>(null);

  const loadMatrix = useCallback(async (id: string): Promise<void> => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const [payload, graph] = await Promise.all([
        api.getTraceability(id),
        api.getDependencyGraph(id),
      ]);
      setMatrix(payload);
      setDependencyGraph(graph);
    } catch (err) {
      setMatrix(null);
      setDependencyGraph(null);
      // Surface the REAL reason: a stale backend (404 — route not registered),
      // a server error (500 — detail carries the message), or a network issue.
      const statusCode = (err as { response?: { status?: unknown } } | null)?.response?.status;
      const statusSuffix = typeof statusCode === 'number' ? ` (HTTP ${statusCode})` : '';
      const reason = detailFromJsonError(err);
      const message =
        `Could not build the traceability matrix.${statusSuffix}` +
        (reason ? ` ${reason}` : ' Check that the backend is running with the latest code, then press Rebuild.');
      setLoadError(message);
      handleError('Could not load the traceability matrix.', err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const selectableNodes = (dependencyGraph?.nodes ?? []).filter(
    (node) => node.artifact_type === 'requirement' || node.artifact_type === 'user_story',
  );

  const artifactRefs = () => selectableNodes
    .filter((node) => selectedArtifacts.has(node.id))
    .map((node) => ({ artifact_type: node.artifact_type, artifact_key: node.artifact_key }));

  const planRegeneration = async (): Promise<void> => {
    const refs = artifactRefs();
    if (!projectId || refs.length === 0) return;
    setIsPlanning(true);
    try {
      setRegenerationPlan(await api.planRegeneration(projectId, refs));
    } catch (error) {
      handleError('Could not calculate the regeneration scope.', error);
    } finally {
      setIsPlanning(false);
    }
  };

  const executeRegeneration = async (): Promise<void> => {
    const refs = artifactRefs();
    if (!projectId || refs.length === 0 || !regenerationPlan) return;
    const confirmed = window.confirm(
      `Regenerate ${regenerationPlan.summary.sections} PRD section(s)` +
      `${regenerationPlan.summary.diagrams ? ' and the architecture diagram' : ''}? Locked and human-owned sections will be preserved.`,
    );
    if (!confirmed) return;
    setIsRegenerating(true);
    try {
      let job = await api.startRegenerationJob(projectId, refs);
      setRegenerationJobId(job.id);
      while (!['completed', 'failed', 'cancelled'].includes(job.status)) {
        await new Promise(resolve => window.setTimeout(resolve, 1000));
        job = await api.getGenerationJob(projectId, job.id);
      }
      if (job.status === 'cancelled') throw new Error('Scoped regeneration was cancelled.');
      if (job.status === 'failed' || !job.result) {
        throw new Error(job.error_message || 'Scoped regeneration failed.');
      }
      const result = job.result;
      if (result.status === 'failed') {
        throw new Error(result.error_message || 'Scoped regeneration failed.');
      }
      notify(
        result.status === 'no_changes'
          ? 'No downstream artifact required regeneration.'
          : 'Affected artifacts regenerated successfully.',
        'success',
      );
      setSelectedArtifacts(new Set());
      setRegenerationPlan(null);
      await loadMatrix(projectId);
    } catch (error) {
      handleError('Could not regenerate the affected artifacts.', error);
    } finally {
      setRegenerationJobId(null);
      setIsRegenerating(false);
    }
  };

  const cancelRegeneration = async (): Promise<void> => {
    if (!projectId || !regenerationJobId) return;
    try {
      await api.cancelGenerationJob(projectId, regenerationJobId);
    } catch (error) {
      handleError('Could not cancel regeneration.', error);
    }
  };

  useEffect(() => {
    if (!projectId) return;
    void loadMatrix(projectId);
  }, [projectId, loadMatrix]);

  if (!projectId) {
    return (
      <div className="p-8 text-sm text-on-surface-variant italic">Select a project to trace requirements.</div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 p-12 text-sm text-on-surface-variant">
        <Loader2 className="w-4.5 h-4.5 animate-spin text-primary" />
        <span>Building traceability matrix…</span>
      </div>
    );
  }

  if (loadError || !matrix) {
    return (
      <div className="max-w-4xl mx-auto p-6 bg-white border border-rose-200 rounded-2xl shadow-sm space-y-2.5">
        <div className="flex items-center gap-2 text-sm font-semibold text-rose-700">
          <ShieldAlert className="w-4.5 h-4.5 shrink-0" />
          Requirement Traceability unavailable
        </div>
        <p className="text-xs text-on-surface-variant leading-relaxed">{loadError}</p>
        <button
          onClick={() => void loadMatrix(projectId)}
          aria-label="Retry loading traceability matrix"
          className="px-3 py-1.5 rounded-xl bg-white border border-outline hover:bg-black/5 font-label-md text-xs text-on-surface font-semibold shadow-sm flex items-center gap-1.5 cursor-pointer transition-all whitespace-nowrap"
        >
          <RefreshCw className="w-3.5 h-3.5 text-primary" />
          Rebuild
        </button>
      </div>
    );
  }

  const { coverage } = matrix;
  const gapCount =
    coverage.requirements_without_stories.length +
    coverage.stories_without_criteria.length +
    coverage.criteria_needing_review.length +
    coverage.requirements_without_prd_sections.length +
    coverage.requirements_without_diagram.length +
    coverage.stale_section_references.length +
    coverage.stale_diagram_references.length;


  return (
    <div className="space-y-3 mb-4">
      {/* Coverage summary + rebuild */}
      <div className="bg-white border border-outline rounded-[10px] p-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="bg-primary/15 text-primary text-[10.5px] px-2.5 py-1 rounded border border-primary/25 font-bold uppercase tracking-wider font-mono">
            Traceability Matrix
          </span>
          {chip(`${coverage.traced_requirements}/${coverage.total_requirements} traced`, 'bg-emerald-100 text-emerald-800 border-emerald-300')}
          {chip(`${coverage.total_user_stories} stories`, 'bg-slate-100 text-slate-700 border-slate-300')}
          {chip(`${coverage.total_acceptance_criteria} criteria`, 'bg-slate-100 text-slate-700 border-slate-300')}
        </div>
        <button
          onClick={() => void loadMatrix(projectId)}
          aria-label="Rebuild traceability matrix"
          className="px-3 py-1.5 rounded-xl bg-white border border-outline hover:bg-black/5 font-label-md text-xs text-on-surface font-semibold shadow-sm flex items-center gap-1.5 cursor-pointer transition-all whitespace-nowrap"
        >
          <RefreshCw className="w-3.5 h-3.5 text-primary" />
          Rebuild
        </button>
      </div>

      {/* Persisted dependency graph + partial-regeneration controls */}
      {dependencyGraph && (
        <div className="bg-white border border-outline rounded-[10px] p-3 space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-bold text-on-surface">
                <Network className="w-4 h-4 text-primary" /> Artifact dependency graph
              </div>
              <p className="mt-1 text-xs text-on-surface-variant">
                {dependencyGraph.summary.nodes} artifacts, {dependencyGraph.summary.edges} relationships. Select changed inputs to calculate their downstream write scope.
              </p>
            </div>
            <button
              type="button"
              disabled={selectedArtifacts.size === 0 || isPlanning || isRegenerating}
              onClick={() => void planRegeneration()}
              className="inline-flex items-center gap-1.5 rounded-xl bg-primary px-3 py-2 text-xs font-bold text-on-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isPlanning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <GitBranch className="h-3.5 w-3.5" />}
              Preview affected artifacts
            </button>
          </div>

          <div className="max-h-44 overflow-y-auto rounded-xl border border-outline bg-slate-50 p-2.5">
            <div className="grid gap-1.5 sm:grid-cols-2">
              {selectableNodes.map((node) => (
                <label key={node.id} className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-2 text-xs hover:bg-primary/[0.04]">
                  <input
                    type="checkbox"
                    checked={selectedArtifacts.has(node.id)}
                    onChange={() => {
                      setSelectedArtifacts((current) => {
                        const next = new Set(current);
                        if (next.has(node.id)) next.delete(node.id); else next.add(node.id);
                        return next;
                      });
                      setRegenerationPlan(null);
                    }}
                    className="mt-0.5 accent-primary"
                  />
                  <span className="min-w-0">
                    <span className="font-mono font-bold text-primary">{node.artifact_key}</span>
                    <span className="ml-1 text-on-surface-variant">{node.label}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {regenerationPlan && (
            <div className="rounded-xl border border-primary/25 bg-primary/[0.04] p-3 text-xs space-y-2">
              <div className="flex flex-wrap gap-1.5">
                {chip(`${regenerationPlan.summary.stories} affected stories`, 'bg-blue-50 text-blue-800 border-blue-200')}
                {chip(`${regenerationPlan.summary.sections} PRD sections`, 'bg-violet-50 text-violet-800 border-violet-200')}
                {chip(`${regenerationPlan.summary.diagrams} diagrams`, 'bg-cyan-50 text-cyan-800 border-cyan-200')}
                {regenerationPlan.summary.skipped_sections > 0 && chip(`${regenerationPlan.summary.skipped_sections} protected`, 'bg-amber-50 text-amber-800 border-amber-200')}
              </div>
              {regenerationPlan.regenerate_prd_sections.length > 0 && (
                <p><span className="font-semibold">Will update:</span> {regenerationPlan.regenerate_prd_sections.join(', ')}</p>
              )}
              {regenerationPlan.skipped_prd_sections.length > 0 && (
                <p className="text-amber-800"><span className="font-semibold">Preserved:</span> {regenerationPlan.skipped_prd_sections.join(', ')}</p>
              )}
              <div className="flex justify-end">
                {isRegenerating && regenerationJobId && (
                  <button
                    type="button"
                    onClick={() => void cancelRegeneration()}
                    className="mr-2 rounded-xl border border-error/40 px-3 py-2 text-xs font-bold text-error"
                  >
                    Stop regeneration
                  </button>
                )}
                <button
                  type="button"
                  disabled={isRegenerating || (regenerationPlan.summary.sections === 0 && regenerationPlan.summary.diagrams === 0)}
                  onClick={() => void executeRegeneration()}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-primary px-3 py-2 text-xs font-bold text-on-primary disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isRegenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  Regenerate affected artifacts
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Coverage-gap panel */}
      {gapCount > 0 && (
        <div className="bg-white border border-amber-200 rounded-2xl p-4.5 shadow-sm space-y-2.5">
          <div className="flex items-center gap-1.5 text-xs font-bold text-amber-700">
            <ShieldAlert className="w-4 h-4" />
            Coverage Gaps ({gapCount})
          </div>
          <div className="grid md:grid-cols-2 gap-2.5 text-xs">
            {coverage.requirements_without_stories.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Requirements with no user story:</span>
                <div className="flex flex-wrap gap-1.5">{gapChips(coverage.requirements_without_stories)}</div>
              </div>
            )}
            {coverage.stories_without_criteria.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Stories without acceptance criteria:</span>
                <div className="flex flex-wrap gap-1.5">{gapChips(coverage.stories_without_criteria)}</div>
              </div>
            )}
            {coverage.criteria_needing_review.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Stories with criteria requiring review:</span>
                <div className="flex flex-wrap gap-1.5">{gapChips(coverage.criteria_needing_review)}</div>
              </div>
            )}
            {coverage.requirements_without_prd_sections.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Not referenced by any PRD section:</span>
                <div className="flex flex-wrap gap-1.5">{gapChips(coverage.requirements_without_prd_sections)}</div>
              </div>
            )}
            {coverage.requirements_without_diagram.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Not referenced by any diagram:</span>
                <div className="flex flex-wrap gap-1.5">{gapChips(coverage.requirements_without_diagram)}</div>
              </div>
            )}
            {coverage.stale_section_references.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Unknown codes in PRD sections:</span>
                <div className="flex flex-wrap gap-1.5">{staleChips(coverage.stale_section_references)}</div>
              </div>
            )}
            {coverage.stale_diagram_references.length > 0 && (
              <div className="space-y-1">
                <span className="font-semibold text-on-surface">Unknown codes in diagrams:</span>
                <div className="flex flex-wrap gap-1.5">{staleChips(coverage.stale_diagram_references)}</div>
              </div>
            )}
          </div>
        </div>
      )}


      {/* Matrix rows */}
      {matrix.rows.length === 0 ? (
        <div className="p-8 text-sm text-on-surface-variant italic">
          No requirements to trace yet. Process banking requirements to populate the matrix.
        </div>
      ) : (
        <div className="space-y-3">
          {matrix.rows.map((row) => (
            <RowCard key={row.requirement.id} row={row} />
          ))}
        </div>
      )}

      {/* End-to-end confirmation badge */}
      {coverage.total_requirements > 0 && coverage.traced_requirements === coverage.total_requirements && (
        <div className="flex items-center gap-2 p-4.5 bg-emerald-50 border border-emerald-200 rounded-2xl text-sm font-semibold text-emerald-800">
          <CheckCircle2 className="w-4.5 h-4.5" />
          All requirements are traced end-to-end: story, criteria, and PRD section.
        </div>
      )}
    </div>
  );
};

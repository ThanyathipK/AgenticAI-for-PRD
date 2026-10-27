// A concise, read-only hierarchy for finding the item a user wants to change.
import React, { useCallback, useEffect, useState } from "react";
import {
  Loader2,
  RefreshCw,
} from "lucide-react";
import { api, detailFromJsonError } from "../api/client";
import type { TraceabilityPayload, TraceabilityRow } from "../api/types";
import { handleError } from "./Toast";

interface RequirementTraceabilityProps {
  projectId: string | null;
  refreshToken?: string;
}

export interface TraceabilityRefreshState {
  requirements: unknown;
  clarificationAnswers: unknown;
  auditResult: unknown;
  prdMarkdown: string;
  mermaidDiagram: string;
}

/**
 * Changes whenever an operation can alter the derived traceability matrix.
 * Keeping this pure makes the refresh contract independently testable.
 */
export const traceabilityRefreshToken = (
  state: TraceabilityRefreshState,
): string => JSON.stringify(state);

const idChip = (id: string): React.ReactNode => (
  <span className="rounded-md border border-outline bg-slate-50 px-2 py-1 font-mono text-[10.5px] font-semibold text-on-surface-variant">
    {id}
  </span>
);

const lockChip = (isLocked: boolean): React.ReactNode =>
  isLocked ? (
    <span className="rounded-md bg-amber-50 px-2 py-1 text-[9px] font-semibold uppercase tracking-wide text-amber-800">
      Locked
    </span>
  ) : null;

export const RequirementTree: React.FC<{ row: TraceabilityRow }> = ({
  row,
}) => (
  <article className="overflow-hidden rounded-[10px] border border-outline bg-white">
    <header className="border-b border-outline bg-slate-50/60 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
          {idChip(row.requirement.requirement_code)}
          <span className="text-sm font-bold text-on-surface">
            {row.requirement.title}
          </span>
          {lockChip(row.requirement.is_locked)}
        </div>
      {row.requirement.epic_name && (
        <p className="mt-1.5 text-xs text-on-surface-variant">
          {row.requirement.epic_name}
        </p>
      )}
    </header>
    <div className="p-4">
      <div className="mb-4 flex flex-wrap gap-1.5">
        {row.prd_sections.map((section) => (
          <span
            key={section}
            className="rounded-md bg-violet-50 px-2 py-1 text-[9px] font-semibold text-violet-800"
          >
            PRD: {section}
          </span>
        ))}
        {row.diagrams.map((diagram) => (
          <span
            key={diagram}
            className="rounded-md bg-cyan-50 px-2 py-1 text-[9px] font-semibold text-cyan-800"
          >
            Diagram: {diagram}
          </span>
        ))}
        {!row.traced && (
          <span className="rounded-md bg-rose-50 px-2 py-1 text-[9px] font-semibold uppercase tracking-wide text-rose-800">
            Coverage gap
          </span>
        )}
      </div>
      {row.user_stories.length === 0 ? (
        <p className="py-1 text-xs text-on-surface-variant">
          No user story yet.
        </p>
      ) : (
        <div className="space-y-2.5">
          {row.user_stories.map((story) => {
            const storyId = story.ticket_code || "Unassigned story";
            return (
              <section key={story.id || storyId} className="rounded-lg border border-outline bg-white px-3 py-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {idChip(storyId)}
                      <span className="text-xs font-semibold text-on-surface">
                        {story.story_title || "Untitled user story"}
                      </span>
                    </div>
                    <div className="mt-3">
                      {story.acceptance_criteria.length === 0 ? (
                        <p className="py-1 text-xs text-on-surface-variant">
                          No acceptance criteria yet.
                        </p>
                      ) : (
                        <div className="divide-y divide-outline">
                          {story.acceptance_criteria.map((criterion, index) => {
                            const criterionId =
                              storyId +
                              "-AC-" +
                              String(index + 1).padStart(3, "0");
                            return (
                              <div
                                key={criterionId + "-" + criterion}
                                className="grid gap-1.5 py-2.5 sm:grid-cols-[auto_1fr] sm:gap-3"
                              >
                                <div className="flex flex-wrap items-start gap-1.5">
                                    {idChip(criterionId)}
                                    {story.criteria_needing_review?.includes(
                                      criterion,
                                    ) && (
                                      <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold uppercase text-amber-800">
                                        Review
                                      </span>
                                    )}
                                </div>
                                  <p className="text-xs leading-relaxed text-on-surface-variant sm:pt-1">
                                    {criterion}
                                  </p>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  </article>
);

export const RequirementTraceability: React.FC<
  RequirementTraceabilityProps
> = ({ projectId, refreshToken }) => {
  const [matrix, setMatrix] = useState<TraceabilityPayload | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadHierarchy = useCallback(async (id: string): Promise<void> => {
    setIsLoading(true);
    setLoadError(null);
    try {
      setMatrix(await api.getTraceability(id));
    } catch (error) {
      setMatrix(null);
      const status = (error as { response?: { status?: unknown } } | null)
        ?.response?.status;
      const reason = detailFromJsonError(error);
      const suffix =
        typeof status === "number" ? " (HTTP " + status + ")." : ".";
      setLoadError(
        "Could not load the requirement hierarchy" +
          suffix +
          (reason ? " " + reason : " Please refresh and try again."),
      );
      handleError("Could not load the requirement hierarchy.", error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (projectId) void loadHierarchy(projectId);
  }, [projectId, refreshToken, loadHierarchy]);

  if (!projectId)
    return (
      <div className="p-8 text-sm italic text-on-surface-variant">
        Select a project to view its requirements.
      </div>
    );
  if (isLoading)
    return (
      <div className="flex items-center justify-center gap-2 p-12 text-sm text-on-surface-variant">
        <Loader2 className="h-4.5 w-4.5 animate-spin text-primary" /> Loading
        requirement hierarchy…
      </div>
    );
  if (loadError || !matrix)
    return (
      <div className="space-y-3 rounded-xl border border-rose-200 bg-white p-5">
        <p className="text-sm font-semibold text-rose-700">
          Requirement hierarchy unavailable
        </p>
        <p className="text-xs text-on-surface-variant">{loadError}</p>
        <button
          type="button"
          onClick={() => void loadHierarchy(projectId)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-outline px-3 py-1.5 text-xs font-semibold text-on-surface hover:bg-slate-50"
        >
          <RefreshCw className="h-3.5 w-3.5 text-primary" /> Refresh
        </button>
      </div>
    );

  const storyCount = matrix.rows.reduce(
    (total, row) => total + row.user_stories.length,
    0,
  );
  const criterionCount = matrix.rows.reduce(
    (total, row) =>
      total +
      row.user_stories.reduce(
        (count, story) => count + story.acceptance_criteria.length,
        0,
      ),
    0,
  );
  return (
    <div className="space-y-3 pb-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-outline bg-white p-4">
        <div>
          <h2 className="text-sm font-bold text-on-surface">
            Requirement hierarchy
          </h2>
          <p className="mt-1 text-xs text-on-surface-variant">
            Find the ID you want to change: requirement → user story →
            acceptance criterion.
          </p>
          <p className="mt-1 text-xs text-on-surface-variant">
            Use an ID in chat, for example: “edit US-001” or “update
            US-001-AC-002”.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded border border-outline bg-slate-50 px-2 py-1 text-[10.5px] font-mono font-bold text-on-surface-variant">
            {matrix.rows.length} REQ
          </span>
          <span className="rounded border border-outline bg-slate-50 px-2 py-1 text-[10.5px] font-mono font-bold text-on-surface-variant">
            {storyCount} US
          </span>
          <span className="rounded border border-outline bg-slate-50 px-2 py-1 text-[10.5px] font-mono font-bold text-on-surface-variant">
            {criterionCount} AC
          </span>
          <button
            type="button"
            onClick={() => void loadHierarchy(projectId)}
            aria-label="Refresh requirement hierarchy"
            className="rounded-lg border border-outline p-1.5 text-primary hover:bg-slate-50"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      {matrix.rows.length === 0 ? (
        <div className="rounded-xl border border-outline bg-white p-8 text-sm italic text-on-surface-variant">
          No requirements yet.
        </div>
      ) : (
        <div className="space-y-3">
          {matrix.rows.map((row) => (
            <RequirementTree key={row.requirement.id} row={row} />
          ))}
        </div>
      )}
    </div>
  );
};

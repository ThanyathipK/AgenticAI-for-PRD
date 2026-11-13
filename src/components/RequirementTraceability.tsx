// TRACEABILITY 1.0 — Read-only matrix/hierarchy UI. It renders requirement →
// story → criterion rows plus PRD/diagram references and coverage gaps; it
// never mutates an artifact.
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

export const RequirementTree: React.FC<{ row: TraceabilityRow }> = ({
  row,
}) => (
  <article className="overflow-hidden rounded-[10px] border border-outline bg-white">
    <header className="border-b border-outline bg-slate-50/60 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[10.5px] font-semibold text-on-surface-variant">
            {row.requirement.requirement_code}
          </span>
          <span className="text-sm font-bold text-on-surface">
            {row.requirement.title}
          </span>
        </div>
      {row.requirement.epic_name && (
        <p className="mt-1.5 text-xs text-on-surface-variant">
          {row.requirement.epic_name}
        </p>
      )}
    </header>
    <div className="p-4">
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
                      <span className="font-mono text-[10px] text-on-surface-variant">{story.ticket_code || "Unassigned story"}</span>
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
                        <ul className="list-disc space-y-2 pl-5">
                          {story.acceptance_criteria.map((criterion, index) => (
                            <li key={`${storyId}-AC-${index + 1}-${criterion}`} className="text-xs leading-relaxed text-on-surface-variant">
                              <span className="mr-2 font-mono text-[10px]">{storyId}-AC-{String(index + 1).padStart(3, "0")}</span>
                              {criterion}
                            </li>
                          ))}
                        </ul>
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
  // TRACEABILITY 2.0 — Colocated frontend state/load coordinator. Project or
  // derived-artifact token changes trigger 2.1; explicit Refresh reuses it.
  const [matrix, setMatrix] = useState<TraceabilityPayload | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // TRACEABILITY 2.1 — Load client 3.1, replace matrix state atomically, and
  // expose decoded read failures without retaining stale rows.
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

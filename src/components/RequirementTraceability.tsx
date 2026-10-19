// A concise, read-only hierarchy for finding the item a user wants to change.
import React, { useCallback, useEffect, useState } from "react";
import {
  ChevronRight,
  GitBranch,
  ListChecks,
  Loader2,
  LockKeyhole,
  RefreshCw,
  UserCircle2,
} from "lucide-react";
import { api, detailFromJsonError } from "../api/client";
import type { TraceabilityPayload, TraceabilityRow } from "../api/types";
import { handleError } from "./Toast";

interface RequirementTraceabilityProps {
  projectId: string | null;
}

const idChip = (id: string): React.ReactNode => (
  <span className="rounded border border-primary/25 bg-primary/[0.08] px-2 py-0.5 font-mono text-[10.5px] font-bold text-primary">
    {id}
  </span>
);

const lockChip = (isLocked: boolean): React.ReactNode =>
  isLocked ? (
    <span className="inline-flex items-center gap-1 rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold uppercase text-amber-800">
      <LockKeyhole className="h-2.5 w-2.5" /> Locked
    </span>
  ) : null;

const RequirementTree: React.FC<{ row: TraceabilityRow }> = ({ row }) => (
  <article className="rounded-xl border border-outline bg-white p-3">
    <div className="flex items-start gap-2">
      <GitBranch className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {idChip(row.requirement.requirement_code)}
          <span className="text-sm font-bold text-on-surface">
            {row.requirement.title}
          </span>
          {lockChip(row.requirement.is_locked)}
        </div>
        {row.requirement.epic_name && (
          <p className="mt-1 text-xs text-on-surface-variant">
            {row.requirement.epic_name}
          </p>
        )}
      </div>
    </div>
    <div className="ml-2 mt-3 border-l border-outline pl-4">
      {row.user_stories.length === 0 ? (
        <p className="py-1 text-xs text-on-surface-variant">
          No user story yet.
        </p>
      ) : (
        <div className="space-y-3">
          {row.user_stories.map((story) => {
            const storyId = story.ticket_code || "Unassigned story";
            return (
              <section key={story.id || storyId} className="relative">
                <ChevronRight className="absolute -left-[21px] top-1 h-3.5 w-3.5 text-on-surface-variant" />
                <div className="flex items-start gap-2">
                  <UserCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-blue-700" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {idChip(storyId)}
                      <span className="text-xs font-semibold text-on-surface">
                        {story.story_title || "Untitled user story"}
                      </span>
                      {lockChip(story.is_locked)}
                    </div>
                    <div className="mt-2 border-l border-outline pl-4">
                      {story.acceptance_criteria.length === 0 ? (
                        <p className="py-1 text-xs text-on-surface-variant">
                          No acceptance criteria yet.
                        </p>
                      ) : (
                        <div className="space-y-1.5">
                          {story.acceptance_criteria.map((criterion, index) => {
                            const criterionId =
                              storyId +
                              "-AC-" +
                              String(index + 1).padStart(3, "0");
                            return (
                              <div
                                key={criterionId + "-" + criterion}
                                className="relative flex items-start gap-2 py-0.5"
                              >
                                <ChevronRight className="absolute -left-[21px] top-1 h-3.5 w-3.5 text-on-surface-variant" />
                                <ListChecks className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-700" />
                                <div className="min-w-0">
                                  <div className="flex flex-wrap items-center gap-1.5">
                                    {idChip(criterionId)}
                                    {story.criteria_needing_review?.includes(
                                      criterion,
                                    ) && (
                                      <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold uppercase text-amber-800">
                                        Review
                                      </span>
                                    )}
                                  </div>
                                  <p className="mt-0.5 text-xs leading-relaxed text-on-surface-variant">
                                    {criterion}
                                  </p>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
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
> = ({ projectId }) => {
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
  }, [projectId, loadHierarchy]);

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

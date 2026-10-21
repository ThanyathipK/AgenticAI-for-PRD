import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, FileText, Loader2, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { ConfirmActionResponse, PendingActionPayload } from "../api/types";
import { parseAndRenderMarkdown } from "./MarkdownRenderer";
import { handleError } from "./Toast";
import { formatPrdVersionLabel } from "../utils/version";

interface PRDGenerationPreviewProps {
  action: PendingActionPayload;
  onApproved: (result: ConfirmActionResponse) => void;
  onDiscarded: () => void;
  onRegenerate: () => Promise<void>;
  onBackToRequirements: () => void;
}

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function PRDGenerationPreview({
  action,
  onApproved,
  onDiscarded,
  onRegenerate,
  onBackToRequirements,
}: PRDGenerationPreviewProps) {
  const proposed = action.proposed_changes ?? {};
  const source = typeof proposed.generated_prd === "string" ? proposed.generated_prd : "";
  const proposedVersion = typeof proposed.proposed_version === "number" ? proposed.proposed_version : undefined;
  const proposedSemVer = typeof proposed.proposed_semver === "string" ? proposed.proposed_semver : undefined;
  const diagramWillUpdate = typeof proposed.generated_diagrams === "string" && proposed.generated_diagrams.trim().length > 0;
  const preservedSections = useMemo(
    () => asStringArray(proposed.preserved_sections),
    [proposed.preserved_sections],
  );
  const [previewMarkdown, setPreviewMarkdown] = useState(source);
  const [isNormalizing, setIsNormalizing] = useState(Boolean(source));
  const [workingAction, setWorkingAction] = useState<"approve" | "discard" | "regenerate" | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPreviewMarkdown(source);
    setIsNormalizing(Boolean(source));
    if (!source) return () => { cancelled = true; };
    api.convertPrdToMarkdown(source)
      .then((result) => {
        if (!cancelled) setPreviewMarkdown(result.markdown || source);
      })
      .catch(() => {
        if (!cancelled) setPreviewMarkdown(source);
      })
      .finally(() => {
        if (!cancelled) setIsNormalizing(false);
      });
    return () => { cancelled = true; };
  }, [source]);

  const approve = async () => {
    if (workingAction) return;
    setWorkingAction("approve");
    try {
      onApproved(await api.confirmAction(action.id, action.project_id));
    } catch (error) {
      handleError("The PRD preview could not be approved.", error);
    } finally {
      setWorkingAction(null);
    }
  };

  const discard = async () => {
    if (workingAction) return;
    setWorkingAction("discard");
    try {
      await api.cancelAction(action.id, action.project_id);
      onDiscarded();
    } catch (error) {
      handleError("The PRD preview could not be discarded.", error);
    } finally {
      setWorkingAction(null);
    }
  };

  const regenerate = async () => {
    if (workingAction) return;
    setWorkingAction("regenerate");
    try {
      await onRegenerate();
    } finally {
      setWorkingAction(null);
    }
  };

  return (
    <section className="max-w-4xl mx-auto mb-4 overflow-hidden rounded-[12px] border-2 border-primary/35 bg-white shadow-sm">
      <header className="border-b border-primary/20 bg-[#fffaf6] px-4 py-3">
        <div className="flex flex-wrap items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <FileText className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-bold text-on-surface">PRD preview</h2>
              <span className="rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-700">
                Not saved
              </span>
              {proposedVersion !== undefined && (
                <span className="text-[10px] font-mono text-on-surface-variant">
                  Approval creates {formatPrdVersionLabel(proposedSemVer, proposedVersion)}
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-on-surface-variant">
              Review the generated document below. Your currently saved PRD remains unchanged until approval.
            </p>
            {preservedSections.length > 0 && (
              <div className="mt-2 flex items-center gap-1.5 text-[11px] text-emerald-700">
                <ShieldCheck className="h-3.5 w-3.5" />
                Protected sections preserved: {preservedSections.join(", ")}
              </div>
            )}
            {diagramWillUpdate && (
              <p className="mt-1 text-[11px] text-on-surface-variant">
                Approval will also update the architecture flow.
              </p>
            )}
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={approve}
            disabled={Boolean(workingAction) || !source}
            className="flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3.5 text-xs font-semibold text-on-primary hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {workingAction === "approve" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            Approve & save PRD
          </button>
          <button
            type="button"
            onClick={regenerate}
            disabled={Boolean(workingAction)}
            className="flex h-9 items-center gap-1.5 rounded-lg border border-outline bg-white px-3.5 text-xs font-semibold text-on-surface hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
          >
            {workingAction === "regenerate" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Regenerate
          </button>
          <button
            type="button"
            onClick={discard}
            disabled={Boolean(workingAction)}
            className="flex h-9 items-center gap-1.5 rounded-lg border border-red-200 bg-white px-3.5 text-xs font-semibold text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {workingAction === "discard" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            Discard draft
          </button>
          <button
            type="button"
            onClick={onBackToRequirements}
            disabled={Boolean(workingAction)}
            className="ml-auto flex h-9 items-center gap-1.5 rounded-lg px-2 text-xs font-semibold text-on-surface-variant hover:text-primary disabled:opacity-50"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to requirements
          </button>
        </div>
      </header>

      <div className="p-4 sm:p-5">
        {isNormalizing ? (
          <div className="flex items-center justify-center gap-2 py-16 text-xs text-on-surface-variant">
            <Loader2 className="h-4 w-4 animate-spin text-primary" /> Preparing document preview…
          </div>
        ) : previewMarkdown ? (
          <article className="prose prose-slate max-w-none min-w-0 break-words">
            {parseAndRenderMarkdown(previewMarkdown)}
          </article>
        ) : (
          <p className="py-16 text-center text-sm text-red-600">This draft has no previewable PRD content.</p>
        )}
      </div>
    </section>
  );
}

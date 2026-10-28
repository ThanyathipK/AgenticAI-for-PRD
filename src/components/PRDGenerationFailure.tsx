import { AlertTriangle, RefreshCw } from "lucide-react";

export function PRDGenerationFailure({
  onRetry,
  previousDraftRetained = false,
  diagramOnly = false,
}: {
  onRetry: () => Promise<void>;
  previousDraftRetained?: boolean;
  diagramOnly?: boolean;
}) {
  return (
    <div
      role={diagramOnly ? "status" : "alert"}
      className={`mx-auto mb-3 flex max-w-4xl flex-wrap items-center gap-2 rounded-lg border px-3 py-2 ${
        diagramOnly
          ? "border-amber-200 bg-amber-50 text-amber-800"
          : "border-red-200 bg-red-50 text-red-800"
      }`}
    >
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <p className="min-w-0 flex-1 text-xs">
        {diagramOnly ? (
          <><strong>Generation may be incomplete.</strong>{" "}The LLM became unavailable before the generation process fully finished. Review the preview carefully, or retry when the LLM is available.</>
        ) : (
          <><strong>PRD generation failed.</strong>{" "}The LLM connection was lost before completion. No partial document was saved.{' '}{previousDraftRetained ? 'The previous draft shown below was not replaced.' : 'Your current PRD is unchanged.'}</>
        )}
      </p>
      <button
        type="button"
        onClick={() => void onRetry()}
        className={`inline-flex h-7 shrink-0 items-center gap-1 rounded-md border bg-white px-2.5 text-[11px] font-semibold ${diagramOnly ? 'border-amber-300 text-amber-800 hover:bg-amber-100' : 'border-red-300 text-red-700 hover:bg-red-100'}`}
      >
        <RefreshCw className="h-3.5 w-3.5" />
        Retry
      </button>
    </div>
  );
}

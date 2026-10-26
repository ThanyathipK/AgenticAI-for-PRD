import { lazy, Suspense } from "react";
import type { useProjectState } from "../hooks/useProjectState";
import { DocumentLibrary } from "./DocumentLibrary";
import { PRDEditor } from "./PRDEditor";
import {
  RequirementTraceability,
  traceabilityRefreshToken,
} from "./RequirementTraceability";
import { VersionHistory } from "./VersionHistory";
import { LoaderCircle } from "lucide-react";

const ArchitectureFlows = lazy(() =>
  import("./ArchitectureFlows").then(({ ArchitectureFlows }) => ({
    default: ArchitectureFlows,
  })),
);

interface MainArtifactPanelProps {
  state: ReturnType<typeof useProjectState>;
}

export function MainArtifactPanel({ state }: MainArtifactPanelProps) {
  const { activeTab, projectId, documents } = state;
  const traceabilityToken = traceabilityRefreshToken({
    requirements: state.structuredRequirements,
    clarificationAnswers: state.clarificationAnswers,
    auditResult: state.auditResult,
    prdMarkdown: state.prdMarkdown,
    mermaidDiagram: state.mermaidDiagram,
  });

  return (
    <div className="max-w-4xl mx-auto">
      {activeTab === "prd" && <PRDEditor state={state} />}
      {activeTab === "flows" && (
        <Suspense
          fallback={
            <div
              className="flex min-h-72 items-center justify-center gap-2 rounded-[12px] border border-outline bg-white text-sm text-on-surface-variant"
              role="status"
            >
              <LoaderCircle className="h-5 w-5 animate-spin text-primary" />
              Loading flowchart view…
            </div>
          }
        >
          <ArchitectureFlows state={state} />
        </Suspense>
      )}
      {activeTab === "trace" && (
        <>
          <DocumentLibrary projectId={projectId} docs={documents} />
          <details className="mt-3 rounded-[10px] border border-outline bg-white">
            <summary className="cursor-pointer list-none px-4 py-3 text-xs font-semibold text-on-surface flex items-center justify-between">
              Requirement hierarchy
            </summary>
            <div className="border-t border-outline p-3">
              <RequirementTraceability
                projectId={projectId}
                refreshToken={traceabilityToken}
              />
            </div>
          </details>
        </>
      )}
      {activeTab === "history" && <VersionHistory state={state} />}
    </div>
  );
}

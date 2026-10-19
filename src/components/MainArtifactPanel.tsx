import type { useProjectState } from "../hooks/useProjectState";
import { ArchitectureFlows } from "./ArchitectureFlows";
import { DocumentLibrary } from "./DocumentLibrary";
import { PRDEditor } from "./PRDEditor";
import { RequirementTraceability } from "./RequirementTraceability";
import { VersionHistory } from "./VersionHistory";
import { ChevronDown } from "lucide-react";

interface MainArtifactPanelProps {
  state: ReturnType<typeof useProjectState>;
  userName: string;
}

export function MainArtifactPanel({ state, userName }: MainArtifactPanelProps) {
  const { activeTab, projectId, documents } = state;

  return (
    <div className="max-w-4xl mx-auto">
      {activeTab === "prd" && <PRDEditor state={state} />}
      {activeTab === "flows" && <ArchitectureFlows state={state} />}
      {activeTab === "trace" && (
        <>
          <DocumentLibrary projectId={projectId} docs={documents} />
          <details className="mt-3 rounded-[10px] border border-outline bg-white">
            <summary className="cursor-pointer list-none px-4 py-3 text-xs font-semibold text-on-surface flex items-center justify-between">
              Requirement hierarchy
              <ChevronDown className="w-4 h-4 text-on-surface-variant" />
            </summary>
            <div className="border-t border-outline p-3">
              <RequirementTraceability projectId={projectId} />
            </div>
          </details>
        </>
      )}
      {activeTab === "history" && (
        <>
          <VersionHistory state={state} userName={userName} />
          <DocumentLibrary projectId={projectId} docs={documents} />
        </>
      )}
    </div>
  );
}

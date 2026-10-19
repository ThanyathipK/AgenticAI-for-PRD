import { useRef } from "react";
import { api } from "../../api/client";

interface CancelActiveAgentOptions {
  controller: AbortController;
  projectId: string | null;
  jobId: string | null;
  cancelJob: (projectId: string, jobId: string) => Promise<unknown>;
  cancelProcess: (projectId: string) => Promise<unknown>;
  setSyncStatus: (status: string) => void;
}

export function cancelActiveAgent({
  controller,
  projectId,
  jobId,
  cancelJob,
  cancelProcess,
  setSyncStatus,
}: CancelActiveAgentOptions) {
  if (projectId) {
    const cancellation = jobId
      ? cancelJob(projectId, jobId)
      : cancelProcess(projectId);
    void cancellation.catch(() => undefined);
  }
  controller.abort();
  setSyncStatus("Stopping generation...");
}

export function useAgentCancellation(
  getProjectId: () => string | null,
  setSyncStatus: (status: string) => void,
) {
  const abortRef = useRef<AbortController | null>(null);
  const backgroundJobRef = useRef<string | null>(null);

  const beginRequest = (): AbortController => {
    const controller = new AbortController();
    abortRef.current = controller;
    return controller;
  };

  const setBackgroundJob = (jobId: string | null) => {
    backgroundJobRef.current = jobId;
  };

  const finishRequest = (controller: AbortController) => {
    if (abortRef.current === controller) abortRef.current = null;
    backgroundJobRef.current = null;
  };

  const stop = () => {
    const controller = abortRef.current;
    if (!controller) return;
    cancelActiveAgent({
      controller,
      projectId: getProjectId(),
      jobId: backgroundJobRef.current,
      cancelJob: api.cancelGenerationJob,
      cancelProcess: api.cancelProcessRequirements,
      setSyncStatus,
    });
    abortRef.current = null;
    backgroundJobRef.current = null;
  };

  return { beginRequest, finishRequest, setBackgroundJob, stop };
}

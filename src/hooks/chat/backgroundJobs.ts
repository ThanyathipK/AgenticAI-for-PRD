import { api } from "../../api/client";
import type {
  GenerationJobPayload,
  ProcessRequirementsRequest,
} from "../../api/types";
import { isUnauthorizedError } from "./agentErrors";

export type AgentJobType = "auditor" | "architect";

const activeJobKey = (projectId: string, jobType: AgentJobType) =>
  `agenticdia:active-job:${projectId}:${jobType}`;

const waitForPoll = (signal: AbortSignal, delayMs: number): Promise<void> =>
  new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Generation cancelled", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    if (signal.aborted) return onAbort();
    signal.addEventListener("abort", onAbort, { once: true });
  });

interface RunBackgroundJobOptions {
  projectId: string;
  jobType: AgentJobType;
  request: ProcessRequirementsRequest;
  signal: AbortSignal;
  onJobId: (jobId: string | null) => void;
  onProgress: (stage: string) => void;
  initialPollDelayMs?: number;
}

export async function runBackgroundAgentJob<T>({
  projectId,
  jobType,
  request,
  signal,
  onJobId,
  onProgress,
  initialPollDelayMs = 1000,
}: RunBackgroundJobOptions): Promise<T> {
  const storageKey = activeJobKey(projectId, jobType);
  const savedJobId = window.localStorage.getItem(storageKey);
  let job: GenerationJobPayload<T>;
  if (savedJobId) {
    try {
      job = await api.getGenerationJob<T>(projectId, savedJobId);
    } catch (resumeError) {
      if (isUnauthorizedError(resumeError)) throw resumeError;
      window.localStorage.removeItem(storageKey);
      job = (await api.startAgentJob(request)) as GenerationJobPayload<T>;
    }
  } else {
    job = (await api.startAgentJob(request)) as GenerationJobPayload<T>;
  }

  window.localStorage.setItem(storageKey, job.id);
  onJobId(job.id);
  let pollDelayMs = initialPollDelayMs;
  while (!["completed", "failed", "cancelled"].includes(job.status)) {
    await waitForPoll(signal, pollDelayMs);
    job = await api.getGenerationJob<T>(projectId, job.id);
    onProgress(job.progress_stage.replaceAll("_", " "));
    pollDelayMs = Math.min(5000, pollDelayMs + 500);
  }
  window.localStorage.removeItem(storageKey);
  onJobId(null);
  if (job.status === "cancelled") {
    throw new DOMException("Generation cancelled", "AbortError");
  }
  if (job.status === "failed" || !job.result) {
    throw new Error(job.error_message || `Background ${jobType} job failed.`);
  }
  return job.result;
}

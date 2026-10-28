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

// JOB 2.1 — Run/resume and poll durable Architect/Auditor work.
// Trigger: Validate Requirements or Generate PRD frontend action.
// Input: project, job type, request, AbortSignal and progress callbacks.
// Calls: JOB 3.1 create/resume read → repeated JOB 3.2 polls.
// Branches: completed → result; failed/cancelled → error; saved id → resume.
// Writes: localStorage active id and caller progress/job-id state.
// Failure: 401 preserves resumability; other resume-read failure starts a new job.
// Details: Resume the localStorage job id
// first; except for 401, any read failure discards that id and attempts a new job.
// Polling backs off from 1s to 5s. Local abort stops polling only—the cancellation
// hook must separately call JOB 3.3. Terminal rows clear localStorage; auth expiry
// during polling intentionally leaves it for a later resume.
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

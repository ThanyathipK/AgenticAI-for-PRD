import assert from "node:assert/strict";
import { test } from "node:test";

import { api } from "../src/api/client";
import type {
  GenerationJobPayload,
  ProcessRequirementsResponse,
} from "../src/api/types";
import {
  extractErrorDetail,
  isAbortError,
  isPayloadTooLargeError,
  isRateLimitError,
  isUnauthorizedError,
  payloadTooLargeContent,
  rateLimitedContent,
} from "../src/hooks/chat/agentErrors";
import { runBackgroundAgentJob } from "../src/hooks/chat/backgroundJobs";
import { cancelActiveAgent } from "../src/hooks/chat/useAgentCancellation";

class MemoryStorage {
  private values = new Map<string, string>();
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
  removeItem(key: string) {
    this.values.delete(key);
  }
}

const installWindow = () => {
  const storage = new MemoryStorage();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: storage,
      setTimeout,
      clearTimeout,
    },
  });
  return storage;
};

test("agent errors distinguish abort, rate limit, payload size, and auth expiry", () => {
  assert.equal(isAbortError(new DOMException("cancelled", "AbortError")), true);
  assert.equal(isRateLimitError({ response: { status: 429 } }), true);
  assert.equal(isPayloadTooLargeError({ response: { status: 413 } }), true);
  assert.equal(isUnauthorizedError({ response: { status: 401 } }), true);
  assert.equal(
    extractErrorDetail({ response: { data: { detail: "too large" } } }),
    "too large",
  );
  assert.match(
    rateLimitedContent({ response: { headers: { "retry-after": "7" } } }),
    /7 second/,
  );
  assert.match(
    payloadTooLargeContent({ response: { data: { detail: "9000 tokens" } } }),
    /9000 tokens/,
  );
});

test("background polling resumes a saved job and removes it after completion", async () => {
  const storage = installWindow();
  const key = "agenticdia:active-job:project-1:auditor";
  storage.setItem(key, "job-1");
  const originalGet = api.getGenerationJob;
  const originalStart = api.startAgentJob;
  const stages: string[] = [];
  const ids: Array<string | null> = [];
  let polls = 0;
  api.getGenerationJob = (async () => {
    polls += 1;
    if (polls === 1)
      return {
        id: "job-1",
        project_id: "project-1",
        job_type: "architect",
        status: "running",
        progress_stage: "waiting_for_local_model",
        cancel_requested: false,
      };
    return {
      id: "job-1",
      project_id: "project-1",
      job_type: "architect",
      status: "completed",
      progress_stage: "completed",
      cancel_requested: false,
      result: { status: "completed" },
    };
  }) as typeof api.getGenerationJob;
  api.startAgentJob = (async () => {
    throw new Error("must resume");
  }) as typeof api.startAgentJob;
  try {
    const result = await runBackgroundAgentJob<ProcessRequirementsResponse>({
      projectId: "project-1",
      jobType: "auditor",
      request: { project_id: "project-1", target_agent: "auditor" },
      signal: new AbortController().signal,
      onJobId: (id) => ids.push(id),
      onProgress: (stage) => stages.push(stage),
      initialPollDelayMs: 0,
    });
    assert.equal(result.status, "completed");
    assert.deepEqual(stages, ["completed"]);
    assert.deepEqual(ids, ["job-1", null]);
    assert.equal(storage.getItem(key), null);
  } finally {
    api.getGenerationJob = originalGet;
    api.startAgentJob = originalStart;
  }
});

test("expired authentication does not replace a resumable background job", async () => {
  const storage = installWindow();
  const key = "agenticdia:active-job:project-1:architect";
  storage.setItem(key, "job-auth");
  const originalGet = api.getGenerationJob;
  const originalStart = api.startAgentJob;
  let started = false;
  api.getGenerationJob = (async () => {
    throw { response: { status: 401 } };
  }) as typeof api.getGenerationJob;
  api.startAgentJob = (async () => {
    started = true;
    return {} as GenerationJobPayload<ProcessRequirementsResponse>;
  }) as typeof api.startAgentJob;
  try {
    await assert.rejects(
      runBackgroundAgentJob({
        projectId: "project-1",
        jobType: "architect",
        request: { project_id: "project-1", target_agent: "architect" },
        signal: new AbortController().signal,
        onJobId: () => undefined,
        onProgress: () => undefined,
        initialPollDelayMs: 0,
      }),
    );
    assert.equal(started, false);
    assert.equal(storage.getItem(key), "job-auth");
  } finally {
    api.getGenerationJob = originalGet;
    api.startAgentJob = originalStart;
  }
});

test("a stale saved job is replaced and the replacement result is returned", async () => {
  const storage = installWindow();
  const key = "agenticdia:active-job:project-1:architect";
  storage.setItem(key, "stale-job");
  const originalGet = api.getGenerationJob;
  const originalStart = api.startAgentJob;
  const ids: Array<string | null> = [];
  let started = 0;
  api.getGenerationJob = (async () => {
    throw { response: { status: 404 } };
  }) as typeof api.getGenerationJob;
  api.startAgentJob = (async () => {
    started += 1;
    return {
      id: "replacement-job",
      project_id: "project-1",
      job_type: "architect",
      status: "completed",
      progress_stage: "completed",
      cancel_requested: false,
      result: { status: "completed", prd_markdown: "# Replacement" },
    };
  }) as typeof api.startAgentJob;
  try {
    const result = await runBackgroundAgentJob<ProcessRequirementsResponse>({
      projectId: "project-1",
      jobType: "architect",
      request: { project_id: "project-1", target_agent: "architect" },
      signal: new AbortController().signal,
      onJobId: (id) => ids.push(id),
      onProgress: () => undefined,
      initialPollDelayMs: 0,
    });

    assert.equal(started, 1);
    assert.equal(result.prd_markdown, "# Replacement");
    assert.deepEqual(ids, ["replacement-job", null]);
    assert.equal(storage.getItem(key), null);
  } finally {
    api.getGenerationJob = originalGet;
    api.startAgentJob = originalStart;
  }
});

test("terminal failed and cancelled jobs clear persisted resumable state", async () => {
  const terminalCases = [
    {
      status: "failed",
      error_message: "invalid_model_output: malformed response",
      expected: /invalid_model_output/,
    },
    {
      status: "cancelled",
      error_message: null,
      expected: { name: "AbortError" },
    },
  ] as const;

  for (const terminal of terminalCases) {
    const storage = installWindow();
    const key = "agenticdia:active-job:project-1:architect";
    const originalStart = api.startAgentJob;
    api.startAgentJob = (async () => ({
      id: `job-${terminal.status}`,
      project_id: "project-1",
      job_type: "architect",
      status: terminal.status,
      progress_stage: terminal.status,
      cancel_requested: terminal.status === "cancelled",
      error_message: terminal.error_message,
    })) as typeof api.startAgentJob;
    try {
      await assert.rejects(
        runBackgroundAgentJob({
          projectId: "project-1",
          jobType: "architect",
          request: { project_id: "project-1", target_agent: "architect" },
          signal: new AbortController().signal,
          onJobId: () => undefined,
          onProgress: () => undefined,
          initialPollDelayMs: 0,
        }),
        terminal.expected,
      );
      assert.equal(storage.getItem(key), null);
    } finally {
      api.startAgentJob = originalStart;
    }
  }
});

test("cancelling a background job aborts locally and requests server cancellation", async () => {
  const controller = new AbortController();
  const calls: string[] = [];
  cancelActiveAgent({
    controller,
    projectId: "project-1",
    jobId: "job-1",
    cancelJob: async (projectId, jobId) => {
      calls.push(`${projectId}:${jobId}`);
    },
    cancelProcess: async () => {
      calls.push("process");
    },
    setSyncStatus: (status) => calls.push(status),
  });
  await Promise.resolve();
  assert.equal(controller.signal.aborted, true);
  assert.deepEqual(calls, ["project-1:job-1", "Stopping generation..."]);
});

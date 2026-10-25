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
import { isOlderProjectSnapshot } from "../src/hooks/useProjectSync";
import { renderToStaticMarkup } from "react-dom/server";
import React from "react";
import {
  RequirementTree,
  traceabilityRefreshToken,
  type TraceabilityRefreshState,
} from "../src/components/RequirementTraceability";
import type { TraceabilityRow } from "../src/api/types";

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

test("project refresh rejects only older snapshots and accepts equal/new revisions", () => {
  assert.equal(
    isOlderProjectSnapshot("2026-10-23T10:00:00Z", "2026-10-24T10:00:00Z"),
    true,
  );
  assert.equal(
    isOlderProjectSnapshot("2026-10-24T10:00:00Z", "2026-10-24T10:00:00Z"),
    false,
  );
  assert.equal(
    isOlderProjectSnapshot("2026-10-25T10:00:00Z", "2026-10-24T10:00:00Z"),
    false,
  );
  assert.equal(isOlderProjectSnapshot(null, "2026-10-24T10:00:00Z"), false);
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

test("traceability refreshes automatically after create, update, archive, restore, lock, unlock, clarification, validation, PRD generation, and PRD restore mutations", () => {
  const base: TraceabilityRefreshState = {
    requirements: { requirements: [], user_stories: [] },
    clarificationAnswers: {},
    auditResult: null,
    prdMarkdown: "",
    mermaidDiagram: "",
  };
  const initial = traceabilityRefreshToken(base);
  const mutations: TraceabilityRefreshState[] = [
    { ...base, requirements: { requirements: [{ id: "created" }] } },
    {
      ...base,
      requirements: { requirements: [{ id: "r1", title: "updated" }] },
    },
    {
      ...base,
      requirements: { requirements: [{ id: "r1", status: "archived" }] },
    },
    {
      ...base,
      requirements: { requirements: [{ id: "r1", status: "active" }] },
    },
    {
      ...base,
      requirements: { requirements: [{ id: "r1", is_locked: true }] },
    },
    {
      ...base,
      requirements: { requirements: [{ id: "r1", is_locked: false }] },
    },
    { ...base, clarificationAnswers: { q1: "confirmed" } },
    { ...base, auditResult: { status: "completed", findings: [] } },
    { ...base, prdMarkdown: "# Generated PRD", mermaidDiagram: "flowchart LR" },
    { ...base, prdMarkdown: "# Restored PRD" },
  ];

  for (const mutation of mutations) {
    assert.notEqual(traceabilityRefreshToken(mutation), initial);
  }
});

test("traceability renders PRD sections, current and historical diagrams, coverage gaps, validation freshness, and source references for each requirement", () => {
  const row: TraceabilityRow = {
    requirement: {
      id: "req-1",
      requirement_code: "REQ-001",
      title: "Verified transfer",
      description: "Source requirement",
      epic_name: "Payments",
      is_locked: false,
    },
    user_stories: [
      {
        id: "story-1",
        ticket_code: "US-001",
        story_title: "Submit a transfer",
        acceptance_criteria: ["Transfer requires approval"],
        criteria_needing_review: ["Transfer requires approval"],
        is_locked: false,
      },
    ],
    prd_sections: ["functional_requirements"],
    diagrams: ["PRD v1", "PRD v2"],
    traced: false,
  };

  const html = renderToStaticMarkup(
    React.createElement(RequirementTree, { row }),
  );
  assert.match(html, /REQ-001/);
  assert.match(html, /US-001-AC-001/);
  assert.match(html, /PRD: functional_requirements/);
  assert.match(html, /Diagram: PRD v1/);
  assert.match(html, /Diagram: PRD v2/);
  assert.match(html, /Coverage gap/);
  assert.match(html, /Review/);
  assert.match(html, /Transfer requires approval/);
});

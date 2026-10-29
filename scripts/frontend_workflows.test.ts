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
import {
  parseStructuredTextBlocks,
  StructuredSectionEditor,
} from "../src/components/StructuredSectionEditor";
import {
  buildDiffRows,
  readableDiffLine,
  resolveLoadedVersionSelection,
} from "../src/components/VersionHistory";
import { PRDGenerationFailure } from "../src/components/PRDGenerationFailure";

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

test("version diff creates readable paired rows with line positions and gaps", () => {
  const rows = buildDiffRows([
    ["context", "# Scope"],
    ["del", "| **Status** | Draft<br>Review |"],
    ["add", "| **Status** | Approved<br>Ready |"],
    ["skip", "4"],
    ["context", "End"],
  ]);
  assert.deepEqual(rows, [
    { kind: "context", before: "# Scope", oldLine: 1, newLine: 1 },
    {
      kind: "pair",
      before: "| **Status** | Draft<br>Review |",
      after: "| **Status** | Approved<br>Ready |",
      oldLine: 2,
      newLine: 2,
    },
    { kind: "skip", skipped: 4 },
    { kind: "context", before: "End", oldLine: 7, newLine: 7 },
  ]);
  assert.equal(
    readableDiffLine("| **Status** | Draft<br>Review |"),
    "Status  |  Draft · Review",
  );
});

test("unfinished LLM generation shows only a compact PRD failure message", () => {
  const markup = renderToStaticMarkup(
    React.createElement(PRDGenerationFailure, { onRetry: async () => {} }),
  );
  assert.match(markup, /PRD generation failed/);
  assert.match(markup, /LLM connection was lost before completion/);
  assert.match(markup, /No partial document was saved/);
  assert.match(markup, />Retry</);
  assert.doesNotMatch(markup, /Generating PRD preview/);
});

test("failed regeneration explains that the previous PRD draft remains visible", () => {
  const markup = renderToStaticMarkup(
    React.createElement(PRDGenerationFailure, {
      onRetry: async () => {},
      previousDraftRetained: true,
    }),
  );
  assert.match(markup, /previous draft shown below was not replaced/);
});

test("LLM interruption shows a generic incomplete-generation warning above the preview", () => {
  const markup = renderToStaticMarkup(
    React.createElement(PRDGenerationFailure, {
      onRetry: async () => {},
      previousDraftRetained: true,
      diagramOnly: true,
    }),
  );
  assert.match(markup, /Generation may be incomplete/);
  assert.match(markup, /LLM became unavailable/);
  assert.match(markup, /Review the preview carefully/);
  assert.doesNotMatch(markup, /architecture flow/);
});

test("structured section editor separates headings, fields, lists, and narrative", () => {
  const blocks = parseStructuredTextBlocks([
    "### Delivery plan",
    "**Owner:** Product team",
    "",
    "- Pilot launch",
    "2. General release",
    "",
    "Customers migrate in controlled waves.",
    "Support remains available during rollout.",
    "\\newpage",
  ]);

  assert.deepEqual(
    blocks.map(({ kind, label, value, prefix }) => ({
      kind,
      label,
      value,
      prefix,
    })),
    [
      { kind: "heading", label: "Delivery plan", value: "", prefix: undefined },
      {
        kind: "field",
        label: "Owner",
        value: "Product team",
        prefix: "**Owner:** ",
      },
      {
        kind: "list-item",
        label: "Item 1",
        value: "Pilot launch",
        prefix: "- ",
      },
      {
        kind: "list-item",
        label: "Item 2",
        value: "General release",
        prefix: "2. ",
      },
      {
        kind: "paragraph",
        label: "Paragraph 1",
        value:
          "Customers migrate in controlled waves.\nSupport remains available during rollout.",
        prefix: undefined,
      },
    ],
  );
});

test("structured section editor exposes official-template lead fields without exposing real headers", () => {
  const business = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      value: [
        "### 1. Business & Strategic Overview",
        "|Introduction &<br>Executive Summary|Customer onboarding summary|",
        "|---|---|",
        "|Problem Statement|Manual onboarding is slow|",
      ].join("\n"),
      onChange: () => {},
    }),
  );
  assert.match(business, /Customer onboarding summary/);
  assert.match(business, /Manual onboarding is slow/);

  const stakeholders = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      value: [
        "### Stakeholders",
        "|Role|Name|",
        "|---|---|",
        "|Product Owner|Nina|",
      ].join("\n"),
      onChange: () => {},
    }),
  );
  assert.match(stakeholders, /Product Owner/);
  assert.match(stakeholders, /Nina/);
  assert.doesNotMatch(stakeholders, /value="Role"/);
});

test("all nine PRD sections render structured edit layouts", () => {
  const fixtures: Array<[string, string, string]> = [
    [
      "title",
      '<div class="flushright">\n\nNimble by Krungsri\n\n</div>\n\n**KRUNGSRI NIMBLE**\\\n**Product Requirement Document**\\\n\n|                  |             |\n|:-----------------|:------------|\n| **PMO No:**      | PMO-28      |\n| **PMO Name:**    | Mobile Banking |',
      "Mobile Banking",
    ],
    [
      "stakeholders",
      "### Stakeholders\n| | |\n|:---|:---|\n| **Role** | **Name** |\n| Product Owner | Nina |",
      "Nina",
    ],
    [
      "version_history",
      "### Version History\n| | | | |\n|:---|:---|:---|:---|\n| **Version** | **Date** | **Author** | **Description** |\n|V1.0|2026-10-28|Nina|Initial|",
      "Initial",
    ],
    [
      "reviews",
      "### Reviews\n| | | | |\n|:---|:---|:---|:---|\n| **Role** | **Name** | **Signature** | **Date** |\n|Product Owner|Nina|Approved|2026-10-28|",
      "Approved",
    ],
    [
      "contents",
      "### Contents\n1. Business Overview\n2. Product Scope",
      "Business Overview",
    ],
    [
      "business_overview",
      "### 1. Business & Strategic Overview\n|Topic|Details|\n|---|---|\n|Introduction &<br>Executive Summary|Summary|\n|Problem Statement|Problem|",
      "Summary",
    ],
    [
      "product_scope",
      "### 2. Product Scope & Functional Requirements\n| | | | |\n|---|---|---|---|\n|Mapping & Functional Requirements|Epic Name: Payments|||\n||**User Story**|**Functional Requirement**|**Acceptance Criteria**|\n||Send money|Transfer funds|Given balance|",
      "Payments",
    ],
    [
      "tech_ops",
      "### 3. Technical & Operational Considerations\n|Consideration|Details|\n|---|---|\n|Non-Functional<br>Requirements|Availability|\n|Expected<br>TPS/Customer<br>volume|100 TPS|",
      "100 TPS",
    ],
    [
      "appendix",
      "### 4. Appendix\n|Appendix Item|Details|Reference|\n|---|---|---|\n|Open<br>Questions &<br>Risks|Fraud risk||\n|Glossary|**Term**|**Definition**|\n||OTP|One-time password|",
      "One-time password",
    ],
  ];

  for (const [sectionId, value, expected] of fixtures) {
    const markup = renderToStaticMarkup(
      React.createElement(StructuredSectionEditor, {
        value,
        sectionId,
        onChange: () => {},
      }),
    );
    assert.match(
      markup,
      new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
    );
    assert.doesNotMatch(markup, /\|---\|/);
  }

  const contentsMarkup = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      value: "### Contents\n1. Business Overview\n2. Product Scope",
      sectionId: "contents",
      onChange: () => {},
    }),
  );
  assert.doesNotMatch(contentsMarkup, /<textarea/);
});

test("product scope keeps Scope In and Scope Out as separate editable fields", () => {
  const markup = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      sectionId: "product_scope",
      value: [
        "### 2. Product Scope & Functional Requirements",
        "#### Scope Definition (In vs. Out)",
        "| Scope In (Included) | Scope Out (Excluded) |",
        "| --- | --- |",
        "| Mobile application | Branch counter workflow |",
      ].join("\n"),
      onChange: () => {},
    }),
  );

  assert.match(markup, /Scope In \(Included\)/);
  assert.match(markup, /Scope Out \(Excluded\)/);
  assert.match(markup, /Mobile application/);
  assert.match(markup, /Branch counter workflow/);
  assert.equal((markup.match(/<textarea/g) ?? []).length, 2);
  assert.doesNotMatch(markup, /\| --- \|/);
});

test("blank product scope exposes wireframe and both scope inputs", () => {
  const markup = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      sectionId: "product_scope",
      value: [
        "### 2. Product Scope & Functional Requirements",
        "| User Story Mapping & Functional Requirements | Epic Name: |  |  |",
        "| --- | --- | --- | --- |",
        "|  | **User Story (System Focus)** | **Functional Requirement (FR)** | **Acceptance Criteria** |",
        "| User Flow & Wireframes |  |  |  |",
        "#### Scope Definition (In vs. Out)",
        "| Scope In (Included) | Scope Out (Excluded) |",
        "| --- | --- |",
        "|  |  |",
      ].join("\n"),
      onChange: () => {},
    }),
  );

  assert.match(markup, /aria-label="User Flow &amp; Wireframes details"/);
  assert.match(markup, /aria-label="Scope In \(Included\)"/);
  assert.match(markup, /aria-label="Scope Out \(Excluded\)"/);
});

test("imported product scope repairs split Wireframes and a collapsed Scope Out column", () => {
  const markup = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      sectionId: "product_scope",
      value: [
        "### 2. Product Scope & Functional Requirements",
        "| Wireframes |",
        "| --- |",
        "|  |",
        "#### Scope Definition (In vs. Out)",
        "| Scope In (Included) |",
        "| --- |",
        "| Existing scope in |",
      ].join("\n"),
      onChange: () => {},
    }),
  );

  assert.match(markup, /aria-label="Wireframes details"/);
  assert.match(markup, /aria-label="Scope In \(Included\)"/);
  assert.match(markup, /aria-label="Scope Out \(Excluded\)"/);
  assert.equal((markup.match(/<textarea/g) ?? []).length, 3);
});

test("blank appendix template rows still expose an editable value field", () => {
  const markup = renderToStaticMarkup(
    React.createElement(StructuredSectionEditor, {
      sectionId: "appendix",
      value: [
        "### 4. Appendix",
        "|Open<br>Questions &<br>Risks|",
        "|---|",
        "|Assumptions|||",
        "|Glossary|||",
        "||Term|Definition|",
        "||TBD|TBD|",
        "|Related<br>Documents|||",
      ].join("\n"),
      onChange: () => {},
    }),
  );

  assert.match(
    markup,
    /Open Questions &amp; Risks[\s\S]*aria-label="Open Questions &amp; Risks details"[\s\S]*Assumptions/,
  );
  assert.match(markup, /aria-label="Assumptions details"/);
  assert.match(markup, /aria-label="Related Documents details"/);
  assert.match(markup, /aria-label="Glossary entry 1 term"/);
  assert.match(markup, /aria-label="Glossary entry 1 definition"/);
  assert.ok((markup.match(/<textarea/g) ?? []).length >= 4);
});

test("version history preserves an older row selected by the user", () => {
  const loaded = [7, 6, 5, 4];
  assert.equal(resolveLoadedVersionSelection(loaded, 5, false), 5);
  assert.equal(resolveLoadedVersionSelection(loaded, 0, false), 0);
  assert.equal(resolveLoadedVersionSelection(loaded, 1, false), 7);
  assert.equal(resolveLoadedVersionSelection(loaded, 5, true), 7);
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

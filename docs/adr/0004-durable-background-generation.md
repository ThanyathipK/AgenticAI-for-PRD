# ADR 0004: Run long generation as durable background jobs

- Status: Accepted
- Date: 2026-10-19

## Context

Architect and scoped-regeneration runs can outlive an HTTP connection. Tying
their lifecycle to a browser tab causes lost progress and ambiguous state after
disconnects or server restarts.

## Decision

Long generation is represented by persisted jobs with explicit queued,
running, completed, failed, and cancelled outcomes. Clients poll and may resume
a saved job. Startup recovery requeues eligible interrupted work.

## Invariants

- Job identity and state are persisted before detached work is relied upon.
- A browser disconnect does not itself cancel server work.
- Cancellation is explicit, idempotent, and checked during execution.
- Failure or cancellation rolls back partial governed mutations.
- Recovery cannot create two active executions for the same job.
- Authentication expiry does not discard a resumable client-side job reference.

## Consequences

The system accepts lifecycle and recovery complexity in exchange for durable
user-visible progress. Job transitions require tests, not only endpoint tests.

## Characterization

See `test_generation_jobs.py`, `test_generation_job_routes.py`,
`test_workflow_cancellation.py`, and `scripts/frontend_workflows.test.ts`.

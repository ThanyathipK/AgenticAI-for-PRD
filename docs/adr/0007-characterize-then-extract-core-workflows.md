# ADR 0007: Characterize behavior before extracting core workflows

- Status: Accepted
- Date: 2026-10-19

## Context

Workflow orchestration accumulated in `agents.py` and requirement routes. These
areas encode persistence, locks, retries, routing, and failure policy, so a
structural rewrite can change behavior without obvious API breakage.

## Decision

Refactor incrementally: first add characterization tests around observable
workflow behavior, then move one responsibility at a time behind compatibility
facades and application services. Preserve public node functions and HTTP
contracts until callers have migrated and regression tests pass.

## Invariants

- Structural changes do not alter workflow routing, persistence, error mapping,
  cancellation, or response shapes unless separately approved.
- Public node functions remain import-compatible during extraction.
- Routes authenticate and validate, then delegate to application services.
- Each extraction has focused characterization tests plus the full regression
  suite before the next boundary moves.
- State-management, database-abstraction, and framework migrations are deferred
  until core workflow boundaries are clean and independently testable.

## Consequences

The migration temporarily contains adapters and facades. This duplication is
accepted to reduce behavioral risk and should be removed only after all callers
use the new boundary.

## Characterization

See `test_requirement_processing_service.py`, the Router/Gatherer/Auditor/
Architect node suites, `test_workflow_cancellation.py`, and route-level tests.

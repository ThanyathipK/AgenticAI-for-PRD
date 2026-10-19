# ADR 0005: Stage agent-proposed mutations before committing them

- Status: Accepted
- Date: 2026-10-19

## Context

LLM output is probabilistic and may propose broad requirement changes. Applying
it immediately would make review, cancellation, concurrency control, and lock
enforcement unreliable.

## Decision

Material agent changes are stored as pending actions. The user sees the semantic
delta and affected artifacts, then explicitly confirms or cancels it. Confirm is
the transaction boundary that revalidates current state and applies the change.

## Invariants

- Creating a proposal does not mutate governed requirement tables or versions.
- Cancel discards the proposal without domain writes.
- Confirm validates project ownership, proposal state, and current locks.
- Confirm is atomic: domain changes, version records, and pending-action state
  succeed together or roll back together.
- Repeated confirm/cancel requests have defined idempotent or conflict behavior.
- A failed confirmation remains inspectable/retryable when safe.

## Consequences

The workflow has an extra interaction step, but users retain control and the
system gains a clear transactional boundary around uncertain agent output.

## Characterization

See `test_merge_version_log.py`, `test_merge_service.py`, and
`scripts/frontend_workflows.test.ts`.

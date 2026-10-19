# Architecture decision records

These short records explain architectural choices that must survive routine
refactoring. They document intent and invariants; implementation details remain
in code and tests.

## Status

- **Proposed** — under discussion.
- **Accepted** — current policy.
- **Superseded** — replaced by a later ADR, which must link back here.

## Decisions

| ADR | Decision |
| --- | --- |
| [0001](0001-append-only-version-history.md) | Preserve version history as append-only snapshots |
| [0002](0002-semantic-memory-fails-open.md) | Keep semantic memory optional and fail-open |
| [0003](0003-server-enforced-artifact-locks.md) | Enforce artifact locks on the server |
| [0004](0004-durable-background-generation.md) | Run long generation as durable background jobs |
| [0005](0005-stage-agent-mutations-before-commit.md) | Stage agent-proposed mutations before committing them |
| [0006](0006-support-postgresql-and-sqlite.md) | Support PostgreSQL and a controlled SQLite fallback |
| [0007](0007-characterize-then-extract-core-workflows.md) | Characterize behavior before extracting core workflows |

## Adding a record

Use the next four-digit number. Keep the record short and include: context,
decision, invariants, consequences, and the tests that characterize it. Do not
edit an accepted decision to reverse its meaning; add a superseding ADR.

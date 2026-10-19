# ADR 0001: Preserve version history as append-only snapshots

- Status: Accepted
- Date: 2026-10-19

## Context

PRDs and their independently editable sections require an auditable history.
Replacing an old row during restore would erase provenance and make lock and
review behavior difficult to prove.

## Decision

Every generated, edited, regenerated, or restored PRD state creates a new
immutable version. Restoring an older version copies its content into a new
head version; it never rewinds or overwrites the ledger. Section history follows
the same rule.

## Invariants

- Existing version rows are never updated to represent newer content.
- Version numbers increase monotonically within their owning document/section.
- A restore records its source and produces a new current version.
- Locked content preserved during a change is represented in version details.
- Persistence failure must not be reported as a successful versioned operation.

## Consequences

History consumes more storage, but auditability and deterministic comparison
take priority. Cleanup, if introduced, must be an explicit retention policy and
must not silently rewrite active history.

## Characterization

See `test_prd_versions.py`, `test_prd_version_restore.py`,
`test_prd_sections.py`, and `test_merge_version_log.py`.

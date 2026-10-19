# ADR 0003: Enforce artifact locks on the server

- Status: Accepted
- Date: 2026-10-19

## Context

Users can protect projects, requirements, stories, criteria, questions, PRDs,
and PRD sections from human or agent changes. UI-only locks would be bypassed by
concurrent requests, background work, or direct API clients.

## Decision

The backend is authoritative for lock state and rechecks locks at mutation time.
The frontend may update optimistically, but must roll back when the server
rejects the operation.

## Invariants

- Mutating a locked artifact returns `409 Conflict` unless an explicit,
  authorized unlock occurs first.
- Confirmation rechecks locks; proposal-time checks are insufficient.
- Agents exclude locked artifacts from prompts, merges, and regeneration.
- Locked and human-owned PRD sections survive generation unchanged.
- Unlock ownership rules are enforced when a lock has an owner.

## Consequences

Lock checks occur at several write boundaries and must remain consistent.
Extracting services must not move enforcement exclusively into routes or UI.

## Characterization

See `test_prd_sections.py`, `test_merge_version_log.py`,
`test_architect_node.py`, and `scripts/requirement_locks.test.ts`.

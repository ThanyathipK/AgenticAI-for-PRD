# ADR 0002: Keep semantic memory optional and fail-open

- Status: Accepted
- Date: 2026-10-19

## Context

Semantic memory improves project-bound standalone chat, but depends on an
embedding model and vector operations that may be unavailable. Memory is an
enhancement, not a prerequisite for core requirements processing.

## Decision

Memory extraction, embedding, persistence, and recall fail open. When disabled
or unhealthy, the request continues without recalled memory and the failure is
logged with request context.

## Invariants

- A memory failure cannot fail an otherwise valid chat request.
- Missing embeddings are treated as absence of optional context.
- Recall remains project-scoped and bounded by configured result/token limits.
- Memory failures may not corrupt the primary conversation transaction.
- Fail-open behavior must not be generalized to required persistence or auth.

## Consequences

Responses may lose personalization during an outage, but core availability is
preserved. Operators need structured logs because users may see no direct error.

## Characterization

See `test_semantic_memory.py` and the failure classification tests in
`test_error_policy.py`.

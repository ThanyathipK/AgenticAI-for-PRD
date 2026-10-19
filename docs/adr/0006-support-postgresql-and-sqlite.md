# ADR 0006: Support PostgreSQL and a controlled SQLite fallback

- Status: Accepted
- Date: 2026-10-19

## Context

Production targets Supabase PostgreSQL, while local development and isolated
tests need a zero-service database. Maintaining separate persistence stacks
would cause behavior and schema drift.

## Decision

Use one async SQLAlchemy repository/model layer for both PostgreSQL and SQLite.
PostgreSQL remains the production target. SQLite is an explicit configurable
fallback for local use and tests, and may be disabled in production.

## Invariants

- Alembic migrations define schema evolution for both supported databases.
- Repository contracts and transaction boundaries remain database-neutral.
- PostgreSQL-specific types have tested SQLite representations.
- Empty or placeholder database URLs only fall back when
  `ALLOW_SQLITE_FALLBACK` permits it.
- Production can fail fast rather than silently selecting SQLite.
- Concurrency-sensitive code accounts for SQLite's single-writer behavior.

## Consequences

Features must stay within the shared SQLAlchemy contract or provide explicit,
tested dialect handling. SQLite passing is necessary but not proof of production
PostgreSQL behavior.

## Characterization

See `test_runtime_database_policy.py`, repository tests using in-memory SQLite,
and PostgreSQL schema-drift validation in CI.

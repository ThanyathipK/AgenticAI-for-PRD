"""Alembic migration runner for Agentic AI.

Replaces the previous ad-hoc startup migrations (routes/migrations.py) with
proper versioned Alembic migrations. Migrations are stored in
backend/alembic/versions/ and tracked in the alembic_version table.

The Alembic env.py module handles the async engine connection lifecycle,
so this module simply delegates to Alembic's command API.
"""
import asyncio
import logging
from pathlib import Path

from alembic.config import Config

from alembic import command

# NOTE: app.auth / app.repositories.user imports were removed together with
# the retired bootstrap seeder — user rows are created exclusively through
# POST /api/auth/register now.

logger = logging.getLogger("app.migrations")

# Path to the alembic.ini file relative to this module.
# backend/app/migrations.py -> backend/alembic.ini
BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


async def run_migrations() -> None:
    """Run all pending Alembic migrations.

    Delegates to Alembic's command API. The env.py module reads the
    application's DATABASE_URL from app.config and manages its own async
    engine lifecycle (including ``asyncio.run(run_async_migrations())``).

    ``asyncio.run()`` cannot be called from inside a running event loop, so
    when this startup coroutine is executed on uvicorn's loop we must not
    invoke Alembic directly. Instead the blocking Alembic upgrade is
    offloaded to a worker thread, where a fresh event loop can be created
    safely and the migration connection is fully isolated from the app engine.
    """
    logger.info("Running Alembic migrations...")

    def _upgrade() -> None:
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        command.upgrade(cfg, "head")

    await asyncio.to_thread(_upgrade)

    logger.info("Alembic migrations completed successfully.")


async def seed_default_user() -> None:
    """DISABLED — users are self-service registrations only.

    This bootstrap seeder used to auto-create ``system@banking.com`` (with a
    bcrypt hash from ``SYSTEM_USER_PASSWORD``) on every startup. Per the
    product decision to run exclusively off Supabase with real, registered
    users only, the seeder is retired: it no longer inserts anything, and the
    ``users`` table starts empty. The function is kept as a no-op so existing
    call sites (``app.main`` lifespan) do not break.

    The project/requirement agent workflows that previously resolved this
    account via ``DEFAULT_SYSTEM_USER_ID`` must use an authenticated user's id
    instead.
    """
    logger.info(
        "Skipping seed_default_user: bootstrap account seeding is disabled "
        "(users are created exclusively through POST /api/auth/register)."
    )

import logging
import re
from typing import Any, AsyncGenerator
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

logger = logging.getLogger("app.database")

# Declarative base class for SQLAlchemy database models
Base = declarative_base()


def _mask_db_url(url: str) -> str:
    """Credentials-safe rendering of the database URL for startup logs.

    Keeps the host/port/database so operators can immediately tell WHICH
    database the engine is pointed at (Supabase vs local SQLite fallback)
    without ever exposing the username/password.
    """
    if url.startswith("sqlite"):
        return url
    return re.sub(r"//[^@/?]+@", "//<credentials>@", url)


# Async connection engine optimized for Supabase Cloud postgres connectivity
# Transaction pooler (port 6543) handles connection pooling at infrastructure level
# so we use minimal SQLAlchemy pooling to avoid double-pooling conflicts
try:
    db_url = settings.async_database_url
    is_sqlite = db_url.startswith("sqlite")
    
    if is_sqlite:
        engine_kwargs: dict[str, Any] = {
            "echo": settings.DEBUG
        }
    else:
        # Supabase Postgres REQUIRES TLS; asyncpg does not enable SSL by
        # default, so force it unless the DSN already carries an explicit
        # ssl/sslmode option (e.g. ?sslmode=require or ?sslmode=disable for a
        # local dev Postgres, which then wins).
        _query_keys = {k for k, _ in parse_qsl(urlsplit(db_url).query)}
        _connect_args = {
            "server_settings": {
                "application_name": "agenticdia-backend"
            },
            "statement_cache_size": 0  # Required for PgBouncer compatibility
        }
        if not (_query_keys & {"ssl", "sslmode"}):
            _connect_args["ssl"] = "require"

        # For transaction pooler (Supabase pooler on port 6543), use minimal pool
        # The pooler itself manages connections, so SQLAlchemy should not maintain a large pool
        engine_kwargs = {
            "pool_size": 5,  # Reduced for transaction pooler
            "max_overflow": 5,  # Reduced for transaction pooler
            "pool_timeout": 10,  # Faster timeout for pooler connections
            "pool_recycle": 300,  # 5 minutes - shorter recycle for pooler
            "pool_pre_ping": True,  # Verify connections before use
            "echo": settings.DEBUG,
            "connect_args": _connect_args
        }
        
    engine = create_async_engine(
        db_url,
        **engine_kwargs
    )
    logger.info(
        f"Async SQLAlchemy Database Engine initialized successfully "
        f"(SQLite={is_sqlite}, Pooler mode). Target -> {_mask_db_url(db_url)}"
    )
except Exception as e:
    logger.error(f"Critical error initializing Database Engine: {str(e)}")
    raise e

# Session maker wrapping the async connection engine
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI Dependency yield mechanism.
    Provides isolated asynchronous transaction sessions for single requests or multi-agent transactions.
    Gracefully disposes and commits/rolls back connection states.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except HTTPException as http_exc:
            # HTTP 4xx/5xx raised by route logic (e.g. the 413 payload-budget
            # guard firing BEFORE any DB work) is NOT a database failure.
            # Logging it as a "Database transaction error" misled operators:
            # the session was never dirty. Roll back defensively (a
            # mid-transaction HTTPException may still hold uncommitted writes)
            # and re-raise so FastAPI returns the intended status code.
            await session.rollback()
            raise http_exc
        except Exception as e:
            await session.rollback()
            logger.error(f"Database transaction error encountered. Session rolled back: {str(e)}")
            raise e
        finally:
            await session.close()

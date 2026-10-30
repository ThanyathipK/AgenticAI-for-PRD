"""
User account repository operations.

Owns every ``users``-table read/write used by the authentication layer
(:mod:`app.auth` and :mod:`app.routes.auth`). Centralizing them here keeps the
UUID coercion (``as_uuid``) and the email canonicalization in one place, so the
login, token-verification, and registration paths can never
drift apart — a drift that previously made an account registerable but not
loginable.

Email handling
--------------
Emails are stored lowercase/trimmed via :func:`normalize_email`, and lookups
match case-insensitively (``func.lower(email)``) so rows written before this
canonicalization — e.g. the seeded ``system@banking.com`` account — still
resolve for a user who types ``System@Banking.com``.
"""
import logging
import uuid
from typing import Optional, Union

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectModel, UserModel
from app.repositories.base import as_uuid

logger = logging.getLogger(__name__)


# AUTH repository layer — Read and mutate authentication-owned user records.
# Trigger: AUTH services and routes; repository methods are not HTTP entry points.
# Input: canonical user identifiers, email addresses, or account fields.
# Writes: create_user flushes changes; its caller commits.
# Failure: database errors propagate to the calling service or route.
# Call map:
#   AUTH 1.7.1.1 — authenticate_user → get_by_email   (LOGIN read)
#   AUTH 6.3.4   — register route    → email_exists   (uniqueness read)
#   AUTH 6.3.5   — register route    → create_user    (INSERT)
#   AUTH 7.1.2   — get_current_user  → get_by_id      (token → row, reused by
#                                                       AUTH 7.2.1)
#   AUTH 8.5     — delete-account route → delete_account (project/user DELETE)
def normalize_email(email: Optional[str]) -> str:
    """Return the canonical form of an email address.

    Surrounding whitespace is stripped and the value lower-cased so that
    ``" User@Bank.com "``, ``"user@bank.com"`` and ``"USER@BANK.COM"`` all
    address the same account. The ``users.email`` column is ``UNIQUE`` but the
    index itself is case-sensitive on both SQLite and Postgres, so the
    canonicalization has to happen in the application layer.
    """
    return (email or "").strip().lower()


class UserRepository:
    """Database access for user accounts (authentication surface)."""

    # AUTH 7.1.2 — Resolve a user by primary key.
    # Trigger: request authentication.
    # Writes: NONE; missing rows return None for the caller to classify.
    @staticmethod
    async def get_by_id(
        user_id: Union[str, uuid.UUID], session: AsyncSession
    ) -> Optional[UserModel]:
        """Fetch a user by primary key.

        Returns ``None`` — instead of raising — when ``user_id`` is not a
        parseable UUID (a JWT ``sub`` is attacker-influenced even though it is
        signature-checked), so the caller can surface a clean 401 instead of a
        500 from the GUID bind processor.
        """
        try:
            key = as_uuid(user_id)
        except (AttributeError, TypeError, ValueError):
            logger.warning("Rejected non-UUID user id lookup: %r", user_id)
            return None

        result = await session.execute(
            select(UserModel).where(UserModel.id == key)
        )
        return result.scalar_one_or_none()

    # AUTH 1.7.1.1 — Resolve a user by canonical, case-insensitive email.
    # Trigger: AUTH 1.7.1 login or AUTH 6.3.4 uniqueness check.
    # Writes: NONE; missing rows return None.
    @staticmethod
    async def get_by_email(
        email: Optional[str], session: AsyncSession
    ) -> Optional[UserModel]:
        """Fetch a user by email, matching case-insensitively."""
        normalized = normalize_email(email)
        if not normalized:
            return None

        result = await session.execute(
            select(UserModel).where(func.lower(UserModel.email) == normalized)
        )
        return result.scalar_one_or_none()

    # AUTH 6.3.4 — Check registration email uniqueness.
    # Trigger: AUTH 6.3 registration route.
    # Calls: AUTH 1.7.1.1 get_by_email.
    # Writes: NONE; duplicate handling remains in the route.
    @staticmethod
    async def email_exists(email: Optional[str], session: AsyncSession) -> bool:
        """Return True when an account already owns *email* (case-insensitive)."""
        return await UserRepository.get_by_email(email, session) is not None

    # AUTH 6.3.5 — Stage insertion of a validated user account.
    # Trigger: AUTH 6.3 after validation, uniqueness, and password hashing.
    # Writes: INSERT plus flush; the route owns transaction commit.
    # Failure: database errors propagate and prevent AUTH 6.3.6 success.
    @staticmethod
    async def create_user(
        *,
        email: str,
        password_hash: str,
        full_name: str,
        role: str,
        session: AsyncSession,
        user_id: Optional[Union[str, uuid.UUID]] = None,
    ) -> UserModel:
        """Insert a user row and return the refreshed model.

        A caller-supplied ``user_id`` is honored so the bootstrap account can
        keep its fixed ``00000000-0000-0000-0000-000000000000`` id that
        ``ProjectRepository.DEFAULT_SYSTEM_USER_ID`` depends on.
        """
        user = UserModel(
            id=as_uuid(user_id) if user_id is not None else uuid.uuid4(),
            email=normalize_email(email),
            full_name=full_name,
            role=role,
            password_hash=password_hash,
        )
        session.add(user)
        await session.flush()
        return user

    # AUTH 8.5 — Stage deletion of an account and all projects it owns.
    # Trigger: AUTH 8.4 after confirmation, password, and system-account guards.
    # Input: authoritative user row and request-scoped database session.
    # Writes: explicit project DELETE, cascading owned-data deletes, user DELETE,
    #         and flush; AUTH 8.6 owns the transaction commit.
    # Failure: database errors propagate and leave commit unavailable.
    @staticmethod
    async def delete_account(
        user: UserModel,
        session: AsyncSession,
    ) -> int:
        """Delete an account and all of its owned projects in one transaction.

        ``projects.user_id`` is intentionally RESTRICT, so projects must be
        removed first. Their complete requirement/document/chat/PRD hierarchy
        is database-cascaded; user-owned knowledge and jobs cascade when the
        user row is deleted. The route owns the final commit.
        """
        project_ids = list((await session.execute(
            select(ProjectModel.id).where(ProjectModel.user_id == user.id)
        )).scalars().all())
        if project_ids:
            await session.execute(delete(ProjectModel).where(ProjectModel.id.in_(project_ids)))
        await session.delete(user)
        await session.flush()
        return len(project_ids)

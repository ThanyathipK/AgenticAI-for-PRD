"""
User account repository operations.

Owns every ``users``-table read/write used by the authentication layer
(:mod:`app.auth` and :mod:`app.routes.auth`). Centralizing them here keeps the
UUID coercion (``as_uuid``) and the email canonicalization in one place, so the
login, token-verification, registration and password-change paths can never
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
from typing import Any, Dict, Optional, Union

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProjectModel, UserModel
from app.repositories.base import as_uuid, dt_iso_or_none

logger = logging.getLogger(__name__)


# AUTH repository layer — the users-table access point for the whole AUTH
# namespace. Call sites (all in app/auth.py or app/routes/auth.py):
#   AUTH 1.7.1.1 — authenticate_user → get_by_email   (LOGIN read,      line 243)
#   AUTH 5.2.1   — change_password   → get_by_id      (CHANGE PASSWORD read)
#   AUTH 5.2.3   — change_password   → set_password_hash (UPDATE)
#   AUTH 6.3.4   — register route    → email_exists   (uniqueness read)
#   AUTH 6.3.5   — register route    → create_user    (INSERT)
#   AUTH 7.1.2   — get_current_user  → get_by_id      (token → row, reused by
#                                                       AUTH 7.2.1 / 7.3)
def normalize_email(email: Optional[str]) -> str:
    """Return the canonical form of an email address.

    Surrounding whitespace is stripped and the value lower-cased so that
    ``" User@Bank.com "``, ``"user@bank.com"`` and ``"USER@BANK.COM"`` all
    address the same account. The ``users.email`` column is ``UNIQUE`` but the
    index itself is case-sensitive on both SQLite and Postgres, so the
    canonicalization has to happen in the application layer.
    """
    return (email or "").strip().lower()


# NOTE (flow trace): serialize_user has no caller — the AUTH flows project rows
# via app.auth.to_authenticated_user (AUTH 7.1.3) / routes.auth.UserResponse
# (AUTH 1.7.4, 2.3, 6.3.6) instead. Left in place, not part of any AUTH flow.
def serialize_user(user: UserModel) -> Dict[str, Any]:
    """Serialize a :class:`UserModel` into its public dict representation."""
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        # The users table has no ``is_active`` column yet; the flag stays for
        # schema stability until account suspension is actually introduced.
        "is_active": True,
        "created_at": dt_iso_or_none(user.created_at),
    }


class UserRepository:
    """Database access for user accounts (authentication surface)."""

    # AUTH 7.1.2 / AUTH 5.2.1 — SELECT users WHERE id = :key.
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

    # AUTH 1.7.1.1 — SELECT users WHERE lower(email) = :normalized. The single LOGIN
    #                read; also reused by AUTH 6.3.4 via email_exists below.
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

    # AUTH 6.3.4 — duplicate-email check for POST /api/auth/register (409 upstream).
    @staticmethod
    async def email_exists(email: Optional[str], session: AsyncSession) -> bool:
        """Return True when an account already owns *email* (case-insensitive)."""
        return await UserRepository.get_by_email(email, session) is not None

    # AUTH 6.3.5 — INSERT users row (flush only; the route commits in AUTH 6.3.5).
    #             The bcrypt hash is produced by the route, never here.
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

    # AUTH 5.2.3 — UPDATE users.password_hash (flush only; the commit happens in AUTH 5.2.3).
    @staticmethod
    async def set_password_hash(
        user: UserModel, password_hash: str, session: AsyncSession
    ) -> None:
        """Store a new bcrypt hash on *user* and flush the change."""
        user.password_hash = password_hash
        await session.flush()

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

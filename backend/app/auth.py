"""
Authentication and Authorization Module.

Provides JWT-based authentication with:
- Token creation and verification
- User dependency injection for routes
- Project ownership validation
- Role-based access control helpers
"""
import logging
import warnings
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from passlib.context import CryptContext
from jose import JWTError, jwt

from app.config import settings
from app.database import get_db
from app.models import ProjectModel, UserModel
from app.repositories.base import as_uuid
from app.repositories.user import UserRepository, normalize_email

logger = logging.getLogger("app.auth")

# ---------------------------------------------------------------------------
# AUTH flow index (the matching tags live across src/ and backend/app/)
# ---------------------------------------------------------------------------
#   AUTH 1.x  LOGIN             AuthPanel 1.1 → AuthModal 1.1.1/1.2 →
#                                useAuth 1.3/1.3.1 → api client 1.4 →
#                                route 1.7 → authenticate_user 1.7.1 (repo
#                                1.7.1.1, provisioning 1.7.1.2, bcrypt 1.7.1.3)
#                                → JWT 1.7.3 → response 1.7.4 → persist 1.5
#                                (setAuthToken 1.5.1) → UI state 1.6
#   AUTH 2.x  SESSION RESTORE   GET  /api/auth/me        (2.1 → 2.2 → 2.3 → 2.4)
#   AUTH 3.x  CAPABILITIES      GET  /api/auth/config    (3.1 → 3.2 → 3.3 → 3.4)
#   AUTH 4.x  LOGOUT            AuthPanel 4.1 → useAuth.logout → 4.2 → 4.3 → 4.4
#   AUTH 5.x  CHANGE PASSWORD   POST /api/auth/change-password (service 5.1/5.2;
#                                no frontend caller — see the note at AUTH 5.1)
#   AUTH 6.x  REGISTER          useAuth 6.1 → api 6.2 → route 6.3.1…6.3.6 → 6.4
#   AUTH 7.x  AUTHORIZATION     get_current_user 7.1 (verify_token 7.1.1, repo
#                                7.1.2, projection 7.1.3) · flexible 7.2 (+7.2.1)
#                                · optional 7.3 · project ownership 7.4 →
#                                require_project_owner 7.5/7.6
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Development-only fallback secret (never used once backend/.env sets a real
# JWT_SECRET_KEY — unfilled placeholders count as unset; see resolver below).
DEV_JWT_SECRET = "dev-secret-change-in-production-2026-agenticdia"


# AUTH 1.7.3 / AUTH 7.1.1 — Signing-key resolution, shared by JWT issuance
#                            (create_access_token) and verification (verify_token).
#                            Placeholder or weak secrets are warned about loudly
#                            rather than silently accepted.
def _resolve_jwt_secret() -> str:
    """
    Resolve the JWT signing secret from settings (backend/.env).

    Empty values and unfilled template placeholders fall back to the
    development secret with a loud warning so local development still boots,
    while a real (but weak) secret is honored with a hardening warning.
    """
    configured = (settings.JWT_SECRET_KEY or "").strip()
    if not configured or configured.startswith("[YOUR_"):
        warnings.warn(
            "JWT_SECRET_KEY is unset (or still a template placeholder). "
            "Falling back to the development secret — set a strong "
            "JWT_SECRET_KEY in backend/.env for anything beyond local dev.",
            RuntimeWarning,
            stacklevel=2,
        )
        logger.warning(
            "JWT_SECRET_KEY is unset or a placeholder. Set a strong "
            "JWT_SECRET_KEY in backend/.env for production."
        )
        return DEV_JWT_SECRET
    if len(configured) < 32:
        warnings.warn(
            "JWT_SECRET_KEY is shorter than 32 characters. "
            "Use e.g. `openssl rand -hex 32` for production.",
            RuntimeWarning,
            stacklevel=2,
        )
        logger.warning(
            "JWT_SECRET_KEY is weak (< 32 chars). Strengthen it for production."
        )
    return configured


# AUTH 1.7.3 — The key create_access_token signs with; AUTH 7.1.1 verifies with
#              this same value, so rotating it invalidates every stored session
#              (the client then clears it in AUTH 2.4).
JWT_SECRET_KEY = _resolve_jwt_secret()
JWT_ALGORITHM = "HS256"
# backend/.env exposes the TTL as JWT_EXPIRATION_MINUTES (the previously-read
# ACCESS_TOKEN_EXPIRE_MINUTES name was never wired to .env).
ACCESS_TOKEN_EXPIRE_MINUTES = settings.JWT_EXPIRATION_MINUTES

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)

security_scheme = HTTPBearer()

# ---------------------------------------------------------------------------
# Password Handling
# ---------------------------------------------------------------------------


# AUTH 1.7.1.3 — Bcrypt verification step of the LOGIN flow (called by AUTH 1.7.1);
#                reused as AUTH 5.2.2 (old-password check on change-password).
#                Returns False — never raises — for an empty or malformed stored
#                hash so a bad row produces a 401 instead of a 500.
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Returns ``False`` (instead of raising) when the stored hash is empty or
    not a recognizable bcrypt hash — e.g. rows seeded before the
    ``password_hash`` column existed carry an empty string, and passlib raises
    ``UnknownHashError``/``ValueError`` for anything malformed.
    """
    if not hashed_password:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        logger.warning("Stored password hash is not a valid bcrypt hash")
        return False


# AUTH 6.3.5 (hashing step) — bcrypt hashing for registration (route AUTH 6.3);
#                reused as AUTH 5.2.3 (new hash on change-password). Cost factor
#                comes from pwd_context above (bcrypt__rounds=12).
def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


# AUTH 1.7.1.2 — "Password not provisioned" branch of the LOGIN flow: a users row
#                with an empty hash (the pre-0013 seeded shape) is reported as a
#                provisioning problem (401 + actionable operator log) instead of a
#                wrong-password attempt.
def password_hash_is_usable(hashed_password: Optional[str]) -> bool:
    """Return True when *hashed_password* is a non-empty stored hash.

    Used by the bootstrap seeder and the login route to tell "this account has
    no password yet" (unusable, must be provisioned) apart from "the supplied
    password is wrong" (a real 401), so operators get an actionable log line
    instead of a permanent, unexplainable 401.
    """
    return bool((hashed_password or "").strip())


# AUTH 5.2 — Service layer for POST /api/auth/change-password (route AUTH 5.1).
#            No frontend caller exists (see the AUTH 5.1 discrepancy note).
#              AUTH 5.2.1 — repository READ: UserRepository.get_by_id → 400 when
#                           the row is gone (app.repositories.user)
#              AUTH 5.2.2 — verify_password (AUTH 1.7.1.3) on the OLD password
#              AUTH 5.2.3 — get_password_hash + UserRepository.set_password_hash
#                           + commit → users table UPDATE
async def change_password(
    user_id: str,
    old_password: str,
    new_password: str,
    session: AsyncSession,
) -> None:
    """
    Change a user's password after verifying the old password.

    Args:
        user_id: ID of the user whose password is being changed.
        old_password: Current plaintext password (must verify).
        new_password: New plaintext password (stored bcrypt-hashed).
        session: Active database session (committed on success).

    Raises:
        HTTPException 400: If the user does not exist or the old
            password is incorrect.
    """
    # AUTH 5.2.1 — Repository READ (users table) via UserRepository.get_by_id;
    #              a non-UUID id or missing row becomes a 400 below.
    user = await UserRepository.get_by_id(user_id, session)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found",
        )

    # AUTH 5.2.2 — Bcrypt check of the OLD password (AUTH 1.7.1.3 helper) → 400.
    if not verify_password(old_password, user.password_hash):
        logger.warning(
            "Failed password change (wrong old password) for user %s", user_id
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Old password is incorrect",
        )

    # AUTH 5.2.3 — WRITE: re-hash (AUTH 6.3.5 helper), repository UPDATE +
    #              commit (users.password_hash).
    await UserRepository.set_password_hash(user, get_password_hash(new_password), session)
    await session.commit()
    logger.info("Password changed for user: %s", user.email)


# AUTH 1.7.1 — SERVICE layer of the LOGIN flow: the single credential-verification
#              path behind POST /api/auth/login (route AUTH 1.7) and the only
#              place that decides 200-vs-401 for a login attempt.
#                AUTH 1.7.1.1 — repository READ (email → users row, AUTH 7.1.2
#                               shares the same helper module)
#                AUTH 1.7.1.2 — provisioning branch
#                AUTH 1.7.1.3 — bcrypt branch
#              All three failure branches return the identical 401 "Invalid
#              credentials" so the endpoint cannot be used to enumerate accounts.
async def authenticate_user(
    email: str,
    password: str,
    session: AsyncSession,
) -> UserModel:
    """Verify credentials against the ``users`` table.

    The single login path: resolves the account (case-insensitive,
    whitespace-trimmed email) and verifies the bcrypt hash. Raises a generic
    401 for both "unknown account" and "wrong password" so the endpoint never
    confirms which emails exist, while logging the distinction for operators.

    A row with an EMPTY ``password_hash`` (the pre-0013 seeded shape) is
    reported as a provisioning problem rather than a bad password — otherwise
    the bootstrap account would be permanently un-loginable with no clue why.

    Raises:
        HTTPException 401: If the account does not exist, has no password
            provisioned, or the password does not verify.
    """
    normalized = normalize_email(email)
    # AUTH 1.7.1.1 — Repository READ: the ONLY database access in the LOGIN flow
    #                (users table, case-insensitive email match).
    user = await UserRepository.get_by_email(normalized, session)

    if user is None:
        # AUTH 1.7.1.1 — Unknown account: same 401 body as a wrong password
        #                (below) so login cannot be used to probe for emails.
        logger.warning("Login attempt for non-existent user: %s", normalized)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not password_hash_is_usable(user.password_hash):
        # AUTH 1.7.1.2 — No hash provisioned: 401 for the caller, actionable
        #                advice for the operator (see the logger.error below).
        logger.error(
            "Account %s has no password hash provisioned in the database. "
            "Set SYSTEM_USER_PASSWORD in backend/.env (the startup seeder "
            "back-fills the bootstrap account) or register/rotate a password "
            "for this account.",
            user.email,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(password, user.password_hash):
        # AUTH 1.7.1.3 — Wrong password: identical 401, and the account is NOT
        #                locked (no attempt counter/lockout exists in this flow).
        logger.warning("Failed login attempt for user: %s", user.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


# ---------------------------------------------------------------------------
# JWT Token Handling
# ---------------------------------------------------------------------------


class TokenData(BaseModel):
    """Data embedded in a JWT token."""
    user_id: str
    email: str
    role: str
    exp: Optional[datetime] = None


class AuthenticatedUser(BaseModel):
    """Represents an authenticated user derived from a JWT token."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: str

# AUTH 1.7.3 — JWT creation for the LOGIN flow (called by route AUTH 1.7 only).
#              Claims: sub (user id) / email / role + exp + iat; signed HS256 with
#              JWT_SECRET_KEY (resolver above) and TTL ACCESS_TOKEN_EXPIRE_MINUTES.
#              No server-side session row and no jti, so the token cannot be
#              revoked — the reason AUTH 4.x logout is client-side only.
def create_access_token(
    user_id: str,
    email: str,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a new JWT access token for a user."""
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


# ---------------------------------------------------------------------------
# Token Verification
# ---------------------------------------------------------------------------


# AUTH 7.1.1 — JWT verification gate for every authenticated request: decodes and
#              validates signature + expiry (401 "Could not validate credentials")
#              and requires sub/email/role (401 "Invalid token payload"). Shared by
#              AUTH 7.1, 7.2 and 7.3. Claims are never trusted for identity —
#              AUTH 7.1.2 re-reads the users table afterwards.
def verify_token(token: str) -> TokenData:
    """Verify and decode a JWT token, returning the TokenData payload."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        email: str = payload.get("email")
        role: str = payload.get("role")
        if user_id is None or email is None or role is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return TokenData(user_id=user_id, email=email, role=role)
    except JWTError as e:
        logger.warning(f"JWT verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Dependency Injection — Get Current User
# ---------------------------------------------------------------------------


# AUTH 7.1.3 — Row → public identity projection, shared by AUTH 7.1, 7.2 and 7.3.
#              Single conversion point so password_hash (and future sensitive
#              columns) can never leak into the route handlers.
def to_authenticated_user(user: UserModel) -> AuthenticatedUser:
    """Project a :class:`UserModel` row onto the public auth identity.

    Single conversion point so ``password_hash`` (and any future sensitive
    column) can never leak into the token-derived identity used by routes.
    """
    return AuthenticatedUser(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role,
    )


# AUTH 7.1 — AUTHORIZATION dependency for every Bearer-protected route. This is
#            where an authenticated request "begins" from the backend's point of
#            view: AUTH 2.3 (auth/me) and every project route depend on it. Chain:
#              7.1.1 verify_token → 7.1.2 repository READ (users) → 7.1.3 projection
#            Note: no is_active/disabled-account check exists yet (the users table
#            has no such column), so revocation currently means deleting the row.
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Dependency that extracts and validates the JWT Bearer token from the
    Authorization header, then fetches the corresponding user from the DB.

    Returns an :class:`AuthenticatedUser` — the safe public projection of
    the user row — for the authenticated caller.

    The database is the source of truth: a token whose signature is valid but
    whose ``sub`` no longer resolves to a row (deleted account, wrong
    environment's secret, truncated ``sub``) is rejected with 401 rather than
    trusted on the token's own claims.

    Note: the users table currently has no ``is_active`` column, so no
    disabled-account check is performed here. Re-add one (plus a migration)
    if account suspension is introduced.
    """
    # AUTH 7.1.1 — Signature/expiry gate (shared helper). Raises 401 before any
    #              database work, so a garbage/expired token costs no query.
    token_data = verify_token(credentials.credentials)

    # AUTH 7.1.2 — Repository READ: the DB is the source of truth, so the
    #              token's own claims are never trusted for identity.
    user = await UserRepository.get_by_id(token_data.user_id, session)

    # AUTH 7.1.2 (error branch) — Signature valid but the row is gone (deleted
    #              account, or a token signed with another environment's secret):
    #              401 so the client clears its stale session in AUTH 2.4.
    if user is None:
        logger.warning(
            "Rejected token for unknown user id: %s", token_data.user_id
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # AUTH 7.1.3 — Projection handed to the route handler: identity only, never
    #              password_hash.
    return to_authenticated_user(user)


# ---------------------------------------------------------------------------
# Optional Auth (for routes that allow anonymous access but support auth)
# ---------------------------------------------------------------------------


# AUTH 7.3 — OPTIONAL variant (no 401): routes that work anonymously and merely
#            personalize when a token is present. Reuses the same 7.1.x steps, but
#            a bad token yields None instead of an error.
async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    session: AsyncSession = Depends(get_db),
) -> Optional[AuthenticatedUser]:
    """
    Optional authentication dependency. Returns an :class:`AuthenticatedUser`
    if a valid token is provided, or None if no token/invalid token.
    """
    if credentials is None:
        return None

    try:
        token_data = verify_token(credentials.credentials)
    except HTTPException:
        return None

    user = await UserRepository.get_by_id(token_data.user_id, session)
    if user is None:
        return None

    # AUTH 7.3 — Success path: same projection as AUTH 7.1.3.
    return to_authenticated_user(user)


# ---------------------------------------------------------------------------
# User Response Schemas
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """Public user information returned by auth endpoints."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    role: str
    # The users table has no ``is_active`` column yet; default keeps the
    # schema stable until account suspension is actually introduced.
    is_active: bool = True
    created_at: Optional[datetime] = None
    
class UserMinimalResponse(BaseModel):
    """Minimal user info (e.g., for project ownership references)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    
# ---------------------------------------------------------------------------
# Project Ownership Helpers
# ---------------------------------------------------------------------------


# AUTH 7.4 — PROJECT-OWNERSHIP gate. Called directly by routes whose project id
#            arrives in the request BODY (a FastAPI dependency cannot read it) and
#            wrapped by AUTH 7.5 / 7.6 for path- or query-scoped routes.
#              AUTH 7.4.1 — malformed id → 404 (never 400, so ids are not probed)
#              AUTH 7.4.2 — repository READ: SELECT projects WHERE id=…
#              AUTH 7.4.3 — missing row → 404; different owner → 403
async def verify_project_access(
    project_id: str,
    current_user: AuthenticatedUser,
    session: AsyncSession,
) -> None:
    """
    Core ownership gate shared by every project-scoped route.

    Raises 404 when ``project_id`` is malformed or the project does not exist
    (never leaks whether an id belongs to someone else), and 403 when the
    authenticated user does not own the project. Routes whose project id comes
    from the request BODY (which a FastAPI ``Depends`` cannot reach) call this
    directly; path/query-scoped routes use :func:`require_project_owner`.
    """
    # AUTH 7.4.1 — Malformed/absent project id → 404 before touching the DB.
    try:
        project_key = as_uuid(project_id)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    # AUTH 7.4.2 — Repository READ: the projects table (ownership column user_id).
    result = await session.execute(
        select(ProjectModel).where(ProjectModel.id == project_key)
    )
    project = result.scalar_one_or_none()

    # AUTH 7.4.3 — 404 for an unknown project, 403 for someone else's: the two are
    #              distinct so a client can tell "gone" from "not yours", but neither
    #              reveals the other owner's existence beyond the 403 itself.
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    if str(project.user_id) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this project",
        )


# AUTH 7.5 — Path/query-scoped wrapper: AUTH 7.1 authentication + AUTH 7.4
#            ownership in one `Depends`, returning the caller for handler use.
async def require_project_owner(
    project_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Dependency that verifies the current user owns the specified project.
    ``project_id`` is resolved from the route path (or query string) by name.
    Raises 403 if the user does not own the project; 404 if it does not exist.
    """
    await verify_project_access(project_id, current_user, session)
    return current_user


# AUTH 7.2.1 — Shared token→user resolution for the header and query variants of
#              AUTH 7.2: reuses AUTH 7.1.1 (verify_token), AUTH 7.1.2 (repository
#              READ) and AUTH 7.1.3 (projection); 401 when the row is gone.
async def _resolve_user_from_raw_token(
    raw_token: str, session: AsyncSession
) -> AuthenticatedUser:
    """Verify a raw JWT string and load its user (401 on any failure)."""
    token_data = verify_token(raw_token)
    user = await UserRepository.get_by_id(token_data.user_id, session)
    if user is None:
        logger.warning("Accepted token for unknown user id: %s", token_data.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return to_authenticated_user(user)


# AUTH 7.2 — FLEXIBLE variant used by the SSE stream: accepts the JWT either as a
#            Bearer header (default) or as ?token=…, because the browser
#            EventSource API cannot set headers. Authorization semantics identical
#            to AUTH 7.1; only the token source differs.
async def get_current_user_flexible(
    token: Optional[str] = Query(
        default=None,
        description="JWT for clients that cannot send headers (browser EventSource).",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """
    Like :func:`get_current_user`, but additionally accepts the JWT as a
    ``?token=`` query parameter. Browser ``EventSource`` cannot attach an
    Authorization header, so the SSE stream authenticates through the query
    string; every other client keeps using the standard Bearer header.
    """
    if credentials is not None:
        return await _resolve_user_from_raw_token(credentials.credentials, session)
    if token:
        return await _resolve_user_from_raw_token(token, session)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


# AUTH 7.6 — SSE-scoped ownership wrapper: AUTH 7.2 authentication + AUTH 7.4
#            ownership, so the push stream is authorized exactly like REST routes.
async def require_project_owner_flexible(
    project_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user_flexible),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """Project-ownership gate for the SSE route (Bearer header OR ?token=)."""
    await verify_project_access(project_id, current_user, session)
    return current_user

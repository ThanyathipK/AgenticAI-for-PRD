"""
Authentication Routes.

Endpoints for user login, logout, token management, and password changes.

Every handler resolves accounts through
:class:`app.repositories.user.UserRepository` and verifies credentials through
:func:`app.auth.authenticate_user`, so the database is the single source of
truth for identity (no in-memory users, no token-claim-only trust).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthenticatedUser,
    UserResponse,
    authenticate_user,
    create_access_token,
    get_current_user,
    get_password_hash,
    verify_password,
)
from app.auth import (
    change_password as auth_change_password,
)
from app.config import settings
from app.database import get_db
from app.rate_limit import rate_limit_dependency
from app.repositories.user import UserRepository, normalize_email

logger = logging.getLogger("app.routes.auth")

router = APIRouter()

# Minimum password length enforced by registration and password change. Kept in
# one constant so the two paths (and their error messages) cannot drift apart.
MIN_PASSWORD_LENGTH = 8

# Canonical user roles for the register endpoint (and the role dropdown in the
# frontend's sign-up form, src/api/types.ts AUTH_ROLES — keep the two lists in
# step). Registration rejects anything else so free-form strings like
# "auditor" or "" can never enter the users table.
VALID_ROLES = frozenset({
    "Business Analyst",
    "System Analyst",
    "Product Owner",
    "Technical Product Owner",
    "Project Manager",
    "Developer",
    "QA",
})


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class LoginResponse(BaseModel):
    """Response from successful login."""
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class PasswordChangeRequest(BaseModel):
    """Request to change password."""
    old_password: str
    new_password: str


class PasswordChangeResponse(BaseModel):
    """Response from successful password change."""
    message: str = "Password changed successfully"


class AccountDeleteRequest(BaseModel):
    """Re-authentication and deliberate confirmation for permanent deletion."""
    password: str
    confirmation: str


class AccountDeleteResponse(BaseModel):
    message: str = "Account deleted permanently"
    deleted_projects: int = 0


class RegisterRequest(BaseModel):
    """Request to register a new user."""
    email: EmailStr
    password: str
    full_name: str
    role: str = "Technical Product Owner"


class RegisterResponse(BaseModel):
    """Response from successful registration."""
    message: str = "User registered successfully"
    user: UserResponse


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


# AUTH 1.7 — Authenticate credentials and issue a JWT.
# Trigger: AUTH 1.4 POST /api/auth/login.
# Input: OAuth2 username/password form and request-scoped database session.
# Calls: AUTH 1.7.1 authenticate_user → AUTH 1.7.3 create_access_token.
# Writes: no database state; tokens are stateless.
# Failure: credential failures return the same 401 response.
# Next: AUTH 1.7.4 response → AUTH 1.5 frontend persistence.
@router.post("/api/auth/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    _rate: None = Depends(
        rate_limit_dependency(
            "auth-login", settings.RATE_LIMIT_LOGIN_LIMIT, settings.RATE_LIMIT_LOGIN_WINDOW
        )
    ),
    session: AsyncSession = Depends(get_db),
):
    """
    Authenticate a user and return a JWT access token.
    
    Uses OAuth2 password flow. The username field is treated as email and is
    matched case-insensitively after trimming. Credential verification (and the
    "account has no password provisioned" diagnosis) lives in
    :func:`app.auth.authenticate_user`; this handler only issues the token.

    Raises:
        HTTPException 401: If credentials are invalid
    """
    # AUTH 1.7.1 — Service layer (app/auth.py authenticate_user): users-table
    #              lookup + credential check. Every failure raises 401 "Invalid
    #              credentials" — unknown account (1.7.1.1), unprovisioned
    #              password (1.7.1.2) and wrong password (1.7.1.3) are
    #              indistinguishable to the caller by design.
    user = await authenticate_user(form_data.username, form_data.password, session)

    # AUTH 1.7.2 — Hand the verified user identity to token issuance.
    # Trigger: AUTH 1.7.1 returns successfully.
    # Input: authoritative UserModel loaded by email and verified by bcrypt.
    # Writes: NONE; this step marks the successful authentication boundary.
    # Next: AUTH 1.7.3 JWT creation.

    # AUTH 1.7.3 — JWT issuance (app/auth.py create_access_token): HS256, signed
    #              with JWT_SECRET_KEY, TTL = JWT_EXPIRATION_MINUTES. Stateless —
    #              nothing is written to the DB, which is why AUTH 4.4 cannot
    #              revoke it server-side.
    access_token = create_access_token(
        user_id=str(user.id),
        email=user.email,
        role=user.role,
    )
    
    logger.info(f"User logged in: {user.email} (role: {user.role})")
    
    # AUTH 1.7.4 — Response: access_token + public user projection (no
    #              password_hash) → parsed by AUTH 1.4, persisted by AUTH 1.5.
    return LoginResponse(
        access_token=access_token,
        user=UserResponse(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            role=user.role,
        )
    )


# ---------------------------------------------------------------------------
# Current User / Auth Capabilities
# ---------------------------------------------------------------------------


# AUTH 2.3 — Resolve the current public user for session restoration.
# Trigger: AUTH 2.2 GET /api/auth/me during frontend mount.
# Input: authenticated identity supplied by AUTH 7.1.
# Calls: AUTH 7.1 Bearer verification and users-table resolution occur as a dependency.
# Writes: NONE.
# Failure: invalid token or missing user returns 401 → AUTH 2.4 local clear.
@router.get("/api/auth/me", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def read_current_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Return the profile of the caller identified by the Bearer token.

    The token is re-resolved against the ``users`` table, so this doubles as the
    client-side "is my session still valid?" probe (401 once the account is gone
    or the token has expired).
    """
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
    )


# AUTH 3.3 — Return public authentication capabilities.
# Trigger: AUTH 3.2 GET /api/auth/config before login.
# Input: server authentication settings; no bearer token is required.
# Writes: NONE.
# Next: AUTH 3.1 gates the sign-up UI; AUTH 6.3.1 independently enforces signup.
@router.get("/api/auth/config", status_code=status.HTTP_200_OK)
async def auth_config():
    """Public auth capabilities, so the UI can hide the sign-up form.

    Deliberately unauthenticated: the client must know whether registration is
    available BEFORE it holds a token.
    """
    return {
        "signup_enabled": settings.SIGNUP_ENABLED,
        "token_expiration_minutes": settings.JWT_EXPIRATION_MINUTES,
    }


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


# AUTH 4.4 — Record an authenticated logout notification.
# Trigger: AUTH 4.2 POST /api/auth/logout after AUTH 4.3 local clear.
# Input: identity resolved by AUTH 7.1.
# Writes: no session or token state; the route emits an audit log only.
# Failure: a client may ignore this route's failure because local sign-out is authoritative.
# Next: END.
@router.post("/api/auth/logout", status_code=status.HTTP_200_OK)
async def logout(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Logout the current user.
    
    Note: JWT tokens are stateless, so logout is primarily client-side.
    Server logs the event for audit purposes.
    """
    logger.info(f"User logged out: {current_user.email}")
    return {"message": "Successfully logged out"}


# ---------------------------------------------------------------------------
# Change Password
# ---------------------------------------------------------------------------


# AUTH 5.1 — FastAPI route: POST /api/auth/change-password. Authenticated through
#            AUTH 7.1, and the target account is `current_user.id` from the
#            verified token — never a body field — so a caller can only change
#            its OWN password.
#            DISCREPANCY vs. the expected end-to-end flow: there is NO frontend
#            caller. src/api/client.ts exposes no authChangePassword helper and no
#            component invokes this route (src/api/types.ts only carries the
#            response type), so the flow ends at AUTH 5.1.3 for API/test clients
#            and has no UI surface yet.
@router.post(
    "/api/auth/change-password",
    response_model=PasswordChangeResponse,
    status_code=status.HTTP_200_OK,
)
async def change_password_endpoint(
    password_request: PasswordChangeRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Change the current user's password with old password verification."""
    # AUTH 5.1.1 — Validation branch: 400 before any hashing/DB work (mirrors the
    #              MIN_PASSWORD_LENGTH check the register route does in AUTH 6.3.2).
    if len(password_request.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    # AUTH 5.1.2 — Service layer: app/auth.py change_password (AUTH 5.2) re-verifies
    #              the old password and rewrites the bcrypt hash; it raises 400 for
    #              a wrong old password, so no new token is issued on success.
    await auth_change_password(
        user_id=current_user.id,
        old_password=password_request.old_password,
        new_password=password_request.new_password,
        session=session,
    )
    
    logger.info(f"Password changed for user: {current_user.email}")
    
    # AUTH 5.1.3 — Response: message only (never a token), so already-issued JWTs
    #              stay valid until they expire.
    return PasswordChangeResponse(message="Password changed successfully")


# AUTH 8.4 — Permanently delete the authenticated account and its owned data.
# Trigger: AUTH 8.3 DELETE /api/auth/account.
# Input: password, literal DELETE confirmation, AUTH 7.1 identity, and DB session.
# Calls: AUTH 8.4.1 confirmation guard → AUTH 8.4.2 password verification →
#        AUTH 8.4.3 system-account guard → AUTH 8.5 repository deletion.
# Branches: invalid confirmation → 400; bad password → 401; system account → 403.
# Writes: AUTH 8.6 commits deletion of owned projects and the user row.
# Failure: fails closed before deletion; transaction errors propagate.
# Next: AUTH 8.6 response → AUTH 8.7 frontend session clear.
@router.delete(
    "/api/auth/account",
    response_model=AccountDeleteResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_account(
    request: AccountDeleteRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Permanently delete the caller's account after password verification."""
    # AUTH 8.4.1 — Require the destructive-action confirmation literal.
    if request.confirmation != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Type "DELETE" exactly to confirm permanent account deletion',
        )

    # AUTH 8.4.2 — Re-read the user and verify the current password.
    user = await UserRepository.get_by_id(current_user.id, session)
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    # AUTH 8.4.3 — Protect the configured bootstrap/system account.
    if str(user.id) == str(settings.SYSTEM_USER_ID):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The protected system account cannot be deleted",
        )

    # AUTH 8.6 — Commit the repository's staged account/project deletion.
    deleted_projects = await UserRepository.delete_account(user, session)
    await session.commit()
    logger.info(
        "User permanently deleted own account: user_id=%s deleted_projects=%d",
        current_user.id,
        deleted_projects,
    )
    return AccountDeleteResponse(deleted_projects=deleted_projects)


# ---------------------------------------------------------------------------
# Register New User
# ---------------------------------------------------------------------------


# AUTH 6.3 — Validate and create a self-service user account.
# Trigger: AUTH 6.2 POST /api/auth/register.
# Input: registration payload and request-scoped database session.
# Calls: AUTH 6.3.4 uniqueness read → AUTH 6.3.5 hash and repository insert.
# Branches: signup disabled → 403; invalid password/role → 400; duplicate → 409.
# Writes: one users row and transaction commit on success.
# Next: AUTH 6.3.6 response → AUTH 6.4 normal login flow.
@router.post(
    "/api/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    register_request: RegisterRequest,
    _rate: None = Depends(
        rate_limit_dependency(
            "auth-register",
            settings.RATE_LIMIT_REGISTER_LIMIT,
            settings.RATE_LIMIT_REGISTER_WINDOW,
        )
    ),
    session: AsyncSession = Depends(get_db),
):
    """
    Register a new user account.

    Gated by ``SIGNUP_ENABLED`` (backend/.env): when false this endpoint is not
    exposed at all (403), so a shared/production deployment cannot be given new
    accounts by an anonymous caller. Uniqueness is enforced case-insensitively
    against the ``users`` table.

    Raises:
        HTTPException 403: If self-service signup is disabled.
        HTTPException 409: If an account with this email already exists.
    """
    # AUTH 6.3.1 — Deployment gate: 403 when SIGNUP_ENABLED=false, so a shared or
    #              production instance cannot be given new accounts anonymously.
    if not settings.SIGNUP_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-service registration is disabled",
        )

    # AUTH 6.3.2 — Payload validation branch: minimum password length → 400 (the
    #              client mirrors it in AUTH 1.1.1, but the server is the gate).
    if len(register_request.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    # AUTH 6.3.3 — Role validation branch: only the canonical VALID_ROLES are
    #              accepted → 400; free-form strings never reach the users table.
    # ROLE VALIDATION: only the canonical roles are accepted. Whitespace is
    # tolerated on input but the stored value is the exact canonical string.
    requested_role = register_request.role.strip()
    if requested_role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role. Allowed roles: " + ", ".join(sorted(VALID_ROLES)),
        )

    # AUTH 6.3.4 — Repository READ: canonicalize the email and check uniqueness
    #              case-insensitively (UserRepository.email_exists →
    #              get_by_email, AUTH 1.7.1.1 helper) → 409 on a duplicate.
    email = normalize_email(str(register_request.email))

    if await UserRepository.email_exists(email, session):
        logger.warning("Registration rejected — duplicate email: %s", email)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    # AUTH 6.3.5 — WRITE path: bcrypt hash (app/auth.py get_password_hash) then
    #              UserRepository.create_user (INSERT) + commit — the only place
    #              in the AUTH namespace that adds a users row.
    new_user = await UserRepository.create_user(
        email=email,
        full_name=register_request.full_name.strip(),
        role=requested_role,
        password_hash=get_password_hash(register_request.password),
        session=session,
    )
    await session.commit()
    await session.refresh(new_user)

    logger.info(f"New user registered: {new_user.email}")
    
    # AUTH 6.3.6 — Response: 201 + the created user, and deliberately NO token —
    #              which is exactly why the frontend chains into the LOGIN flow
    #              (AUTH 6.4 → AUTH 1.3.1) instead of trusting this response.
    return RegisterResponse(
        message="User registered successfully",
        user=UserResponse(
            id=str(new_user.id),
            email=new_user.email,
            full_name=new_user.full_name,
            role=new_user.role,
        )
    )

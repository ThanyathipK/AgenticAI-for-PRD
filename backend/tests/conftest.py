"""Central pytest fixtures shared across the backend suite.

The API routes enforce authentication (JWT bearer) and per-project ownership.
Most route-level tests exercise business logic, not the auth layer — the auth
layer has its own dedicated suites (tests/test_auth.py and
tests/test_authorization_enforcement.py, which see the REAL dependencies).

So by default every test runs with:

1. ``get_current_user`` / ``require_project_owner`` (and the SSE ``*_flexible``
   variants) overridden to return a synthetic authenticated user, and
2. the ``verify_project_access`` calls that routes make directly (body/query
   project ids) patched to a no-op via monkeypatch.
"""

import uuid

import pytest

from app.auth import (
    AuthenticatedUser,
    get_current_user,
    get_current_user_flexible,
    require_project_owner,
    require_project_owner_flexible,
    verify_project_access,
)
from app.main import app as fastapi_app
from app.rate_limit import limiter

AUTH_DEPENDENCIES = (
    get_current_user,
    get_current_user_flexible,
    require_project_owner,
    require_project_owner_flexible,
)

# Test modules that must exercise the REAL auth/ownership dependencies because
# they assert the actual 401/403/404 enforcement behavior.
REAL_AUTH_MODULES = {"tests.test_auth", "tests.test_authorization_enforcement"}

# Route modules that call verify_project_access() directly (project ids that
# arrive in the request body/query and therefore cannot be reached with a
# Depends()). Their module-global name is patched so business-logic tests
# bypass ownership checks exactly like the dependency-based routes do.
DIRECT_CHECK_MODULES = ("app.routes.requirements", "app.routes.chat")


@pytest.fixture(autouse=True)
def isolate_auth_rate_limit_buckets():
    """Keep process-global authentication counters isolated per test case."""
    limiter.reset("auth-login:127.0.0.1")
    limiter.reset("auth-register:127.0.0.1")


@pytest.fixture(autouse=True)
def bypass_route_auth(monkeypatch, request):
    """Give every non-auth test a synthetic authenticated user."""
    if request.module.__name__ in REAL_AUTH_MODULES:
        # A generator fixture must always yield, even when it does nothing.
        yield
        return

    synthetic = AuthenticatedUser(
        id=str(uuid.uuid4()),
        email="route-test@bank.com",
        full_name="Route Test User",
        role="Business Analyst",
    )

    async def fake_current_user():
        return synthetic

    async def fake_verify_project_access(project_id, current_user, session):
        return None

    for dep in AUTH_DEPENDENCIES:
        fastapi_app.dependency_overrides[dep] = fake_current_user
    for module in DIRECT_CHECK_MODULES:
        monkeypatch.setattr(f"{module}.verify_project_access", fake_verify_project_access)

    yield

    for dep in AUTH_DEPENDENCIES:
        fastapi_app.dependency_overrides.pop(dep, None)

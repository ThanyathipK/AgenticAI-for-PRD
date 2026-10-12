"""
Tests for the Requirement Impact Analysis endpoint
(``GET /api/pending-actions/{id}/impact``).

The impact window must tell the user — BEFORE they click Save — exactly what a
pending merge changes (requirements / user stories / acceptance criteria) and
which downstream artifacts are impacted (PRD sections and diagrams that
reference the affected ``REQ-``/``US-`` codes), including dangling-reference
warnings and locked-section flags.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_impact_analysis.py -v
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db as app_get_db
from app.main import app as fastapi_app
from app.models import (
    PRDDocumentModel,
    PRDSectionModel,
    PendingActionModel,
    ProjectModel,
    UserModel,
)


# =====================================================================
# Isolated SQLite database + ASGI client fixtures (same pattern as
# tests/test_documents.py)
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_impact.db")
    async with engine.begin() as conn:
        from app.database import Base
        from app import models  # noqa: F401 - register all models

        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_project(db_session):
    """Seed the default system user + one project; returns the project id."""
    user = UserModel(
        email="impact-test@banking.com",
        full_name="Impact Tester",
        role="Developer",
    )
    db_session.add(user)
    await db_session.flush()
    project = ProjectModel(
        user_id=user.id,
        name="Impact Test Project",
        industry_standard="Krungsri Nimble Baseline",
    )
    db_session.add(project)
    await db_session.commit()
    return str(project.id)


@pytest_asyncio.fixture
async def client(db_session):
    """ASGI client wired to the isolated SQLite session (no lifespan run)."""
    async def override_get_db():
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    fastapi_app.dependency_overrides[app_get_db] = override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(app_get_db, None)


# =====================================================================
# Helpers
# =====================================================================
def _story(ticket: str, title: str, so_that: str = "my account stays safe", criteria=None):
    return {
        "ticket_code": ticket,
        "story_title": title,
        "as_a": "customer",
        "i_want_to": f"use {title.lower()}",
        "so_that": so_that,
        "acceptance_criteria": criteria if criteria is not None else ["Given X when Y then Z"],
    }


def _draft(requirements: list, version_number: int, message: str = "chat input") -> PendingActionModel:
    return PendingActionModel(
        project_id="",  # set by the caller
        action_type="MERGE",
        original_user_message=message,
        proposed_changes={
            "requirements": requirements,
            "user_stories": [s for r in requirements for s in r.get("user_stories", [])],
            "generated_prd": "",
            "generated_diagrams": "",
            "version_number": version_number,
        },
        workflow_stage="REVIEWING",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _create_action(session: AsyncSession, project_id: str, draft: PendingActionModel):
    draft.project_id = project_id
    session.add(draft)
    await session.commit()
    return draft


async def _seed_downstream(session: AsyncSession, project_id: str, *, content: str, mermaid: str, locked: bool = False):
    """Seed one PRD section + one PRD-version diagram referencing codes."""
    session.add(
        PRDSectionModel(
            project_id=project_id,
            section_key="product_scope",
            title="Product Scope",
            content=content,
            section_order=5,
            is_locked=locked,
        )
    )
    session.add(
        PRDDocumentModel(
            project_id=project_id,
            version=1,
            prd_markdown="# PRD",
            mermaid_diagram=mermaid,
        )
    )
    await session.commit()


async def _get_impact(client, project_id: str, action_id: str):
    return await client.get(
        f"/api/pending-actions/{action_id}/impact", params={"project_id": project_id}
    )


# =====================================================================
# Tests
# =====================================================================
class TestImpactAnalysis:
    @pytest.mark.asyncio
    async def test_reports_changes_and_downstream_impact(
        self, seeded_project, client, db_session
    ):
        """Updated + created stories, criteria deltas, impacted section + diagram."""
        # --- Establish the CURRENT state: REQ-001 + US-001 (via confirm) ---
        baseline = await _create_action(
            db_session, seeded_project,
            _draft(
                [{"requirement_code": "REQ-001", "title": "Secure Login Epic",
                  "description": "", "user_stories": [_story("US-001", "Password login")]}],
                version_number=2,
            ),
        )
        confirm = await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK

        # Downstream artifacts referencing the baseline codes.
        await _seed_downstream(
            db_session, seeded_project,
            content="# Product Scope\nREQ-001 covers secure login; see US-001 for the password story.",
            mermaid="graph TD\n  US-001 --> Login",
        )

        # --- The DRAFT: US-001 updated (+1 criteria), US-002 created -------
        draft = await _create_action(
            db_session, seeded_project,
            _draft(
                [{
                    "requirement_code": "REQ-001",
                    "title": "Secure Login Epic",
                    "description": "",
                    "user_stories": [
                        _story(
                            "US-001", "Password login",
                            so_that="my account stays protected",
                            criteria=["Given X when Y then Z", "And rate limiting applies"],
                        ),
                        _story("US-002", "Biometric login"),
                    ],
                }],
                version_number=3,
                message="add biometric login",
            ),
        )

        resp = await _get_impact(client, seeded_project, draft.id)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        # Confirming requirements no longer advances the PRD release counter.
        assert data["current_version"] == 1
        assert data["proposed_version"] == 3
        assert data["has_changes"] is True

        # Summary rollup.
        s = data["summary"]
        assert s["stories_updated"] == 1
        assert s["stories_created"] == 1
        assert s["criteria_added"] == 1
        assert s["requirements_created"] == 0
        assert s["sections_impacted"] == 1
        assert s["diagrams_impacted"] == 1

        # Requirement unchanged (same code, same title/description).
        req_changes = {r["requirement_code"]: r for r in data["requirements"]}
        assert req_changes["REQ-001"]["change_kind"] == "unchanged"

        # Story changes.
        story_changes = {st["ticket_code"]: st for st in data["user_stories"]}
        assert story_changes["US-001"]["change_kind"] == "updated"
        assert "so_that" in story_changes["US-001"]["changed_fields"]
        assert story_changes["US-001"]["criteria_added"] == 1
        assert story_changes["US-002"]["change_kind"] == "created"

        # Downstream artifacts: the section references REQ-001 (unchanged) and
        # US-001 (updated) — only the CHANGED code is reported as the reason.
        section = data["impacted_prd_sections"][0]
        assert section["section_key"] == "product_scope"
        assert section["referenced_codes"] == ["US-001"]
        assert section["is_locked"] is False
        assert section["impact_kind"] == "references_changed"

        diagram = data["impacted_diagrams"][0]
        assert diagram["label"] == "PRD v1"
        assert diagram["referenced_codes"] == ["US-001"]

    @pytest.mark.asyncio
    async def test_removed_story_flags_dangling_section(
        self, seeded_project, client, db_session
    ):
        """A section referencing ONLY a removed code is a dangling reference."""
        baseline = await _create_action(
            db_session, seeded_project,
            _draft(
                [{"requirement_code": "REQ-001", "title": "Secure Login Epic",
                  "description": "", "user_stories": [_story("US-001", "Password login")]}],
                version_number=2,
            ),
        )
        confirm = await client.post(
            f"/api/confirm-action/{baseline.id}", params={"project_id": seeded_project}
        )
        assert confirm.status_code == status.HTTP_200_OK

        await _seed_downstream(
            db_session, seeded_project,
            content="# Product Scope\nSee US-001 for the password story.",
            mermaid="",
        )

        # Draft drops US-001 entirely.
        draft = await _create_action(
            db_session, seeded_project,
            _draft(
                [{"requirement_code": "REQ-001", "title": "Secure Login Epic",
                  "description": "", "user_stories": []}],
                version_number=3,
                message="drop password login",
            ),
        )

        resp = await _get_impact(client, seeded_project, draft.id)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        story_changes = {st["ticket_code"]: st for st in data["user_stories"]}
        assert story_changes["US-001"]["change_kind"] == "removed"
        assert data["summary"]["stories_removed"] == 1

        section = data["impacted_prd_sections"][0]
        assert section["impact_kind"] == "references_removed"

    @pytest.mark.asyncio
    async def test_locked_section_flagged(self, seeded_project, client, db_session):
        """Locked sections are flagged so the user knows AI cannot update them."""
        draft = await _create_action(
            db_session, seeded_project,
            _draft(
                [{"requirement_code": "REQ-001", "title": "Epic",
                  "description": "", "user_stories": [_story("US-001", "Story")]}],
                version_number=2,
            ),
        )
        await _seed_downstream(
            db_session, seeded_project,
            content="References REQ-001.",
            mermaid="",
            locked=True,
        )

        resp = await _get_impact(client, seeded_project, draft.id)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        assert data["summary"]["sections_impacted_locked"] == 1
        section = data["impacted_prd_sections"][0]
        assert section["is_locked"] is True

    @pytest.mark.asyncio
    async def test_unknown_action_returns_404(self, seeded_project, client):
        resp = await _get_impact(client, seeded_project, "00000000-0000-0000-0000-000000000000")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_invalid_ids_return_400(self, seeded_project, client):
        resp = await _get_impact(client, "not-a-uuid", "also-not")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


"""Dependency graph and scoped PRD regeneration tests."""
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.dependency_graph_service import build_regeneration_plan, execute_regeneration, rebuild_dependency_graph
from app.models import PRDDocumentModel, ProjectModel, RegenerationRunModel, RequirementStateModel, UserModel
from app.prd_section_service import sync_sections_from_prd
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.version_service import record_prd_version
from app.auth import AuthenticatedUser
from app.routes.operational import ArtifactReferenceRequest, RegenerationExecuteRequest, post_regeneration
from app.workflow_cancellation import workflow_cancellations


@pytest_asyncio.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/dependency.sqlite")
    async with engine.begin() as connection:
        from app.database import Base
        from app import models  # noqa: F401

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def graph_project(db_session):
    user = UserModel(email="graph@bank.test", full_name="Graph Owner", role="Product Owner")
    db_session.add(user)
    await db_session.flush()
    project = ProjectModel(
        user_id=user.id,
        name="Dependency Project",
        industry_standard="Banking",
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(RequirementStateModel(project_id=project.id))
    await db_session.flush()
    await RequirementStateRepository.save_or_update(str(project.id), {
        "requirements": [{
            "requirement_code": "REQ-001",
            "title": "Approve transfer",
            "description": "A maker-checker transfer approval.",
            "user_stories": [{
                "ticket_code": "US-001",
                "story_title": "Approve a transfer",
                "as_a": "checker",
                "i_want_to": "approve a transfer",
                "so_that": "funds are controlled",
                "acceptance_criteria": ["Given a pending transfer, approval releases it"],
            }],
        }],
        "user_stories": [],
        "generated_diagrams": "flowchart TD\n  REQ001[REQ-001] --> US001[US-001]",
    }, db_session)
    await db_session.flush()
    await PRDSectionRepository.create(str(project.id), {
        "section_key": "product_scope",
        "title": "2. Product Scope",
        "content": "## 2. Product Scope\nREQ-001 is delivered by US-001.",
        "section_order": 7,
    }, db_session)
    await PRDSectionRepository.create(str(project.id), {
        "section_key": "tech_ops",
        "title": "3. Technical Operations",
        "content": "## 3. Technical Operations\nREQ-001 requires signed audit logs.",
        "section_order": 8,
        "is_locked": True,
    }, db_session)
    await db_session.flush()
    return str(project.id)


@pytest.mark.asyncio
async def test_graph_persists_edges_and_plan_is_transitive(db_session, graph_project):
    graph = await rebuild_dependency_graph(graph_project, db_session)
    assert graph["summary"]["requirements"] == 1
    assert graph["summary"]["stories"] == 1
    assert any(
        edge["source"] == "requirement:REQ-001"
        and edge["target"] == "user_story:US-001"
        and edge["relationship"] == "contains"
        for edge in graph["edges"]
    )

    plan = await build_regeneration_plan(
        graph_project,
        [{"artifact_type": "requirement", "artifact_key": "req-001"}],
        db_session,
    )
    assert plan["affected_user_stories"] == ["US-001"]
    assert plan["regenerate_prd_sections"] == ["product_scope"]
    assert plan["skipped_prd_sections"] == ["tech_ops"]
    assert plan["regenerate_diagrams"] == ["current"]


@pytest.mark.asyncio
async def test_plan_rejects_unknown_artifact_type(db_session, graph_project):
    with pytest.raises(ValueError, match="Invalid artifact reference"):
        await build_regeneration_plan(
            graph_project,
            [{"artifact_type": "database", "artifact_key": "payments"}],
            db_session,
        )


@pytest.mark.asyncio
async def test_section_sync_updates_only_selected_parts(db_session, graph_project, monkeypatch):
    monkeypatch.setattr("app.latex_service.prd_to_markdown", lambda value: value)
    product = await PRDSectionRepository.get_by_key("product_scope", graph_project, db_session)
    tech = await PRDSectionRepository.get_by_key("tech_ops", graph_project, db_session)
    assert product and tech

    document = (
        "## 2. Product Scope & Functional Requirements\nUpdated product content\n\n"
        "## 3. Technical & Operational Considerations\nUpdated technical content"
    )
    await sync_sections_from_prd(
        graph_project,
        document,
        db_session,
        selected_section_keys={"product_scope"},
    )

    updated_product = await PRDSectionRepository.get_by_key("product_scope", graph_project, db_session)
    unchanged_tech = await PRDSectionRepository.get_by_key("tech_ops", graph_project, db_session)
    assert "Updated product content" in updated_product["content"]
    assert unchanged_tech["content"] == tech["content"]


@pytest.mark.asyncio
async def test_failed_regeneration_is_recorded_without_partial_writes(db_session, graph_project, monkeypatch):
    product = await PRDSectionRepository.get_by_key("product_scope", graph_project, db_session)

    async def fail_architect(_state):
        row = await PRDSectionRepository.get_model_by_key("product_scope", graph_project, db_session)
        row.content = "partial write that must roll back"
        await db_session.flush()
        raise RuntimeError("generation unavailable")

    monkeypatch.setattr("app.agents.architect_node", fail_architect)
    result = await execute_regeneration(
        graph_project,
        [{"artifact_type": "requirement", "artifact_key": "REQ-001"}],
        "Test Owner",
        db_session,
    )

    assert result["status"] == "failed"
    unchanged = await PRDSectionRepository.get_by_key("product_scope", graph_project, db_session)
    assert unchanged["content"] == product["content"]
    run = (await db_session.execute(select(RegenerationRunModel))).scalar_one()
    assert run.status == "failed"
    assert run.error_message == "generation unavailable"


@pytest.mark.asyncio
async def test_reload_prefers_new_nonempty_ledger_and_materialized_diagram(db_session, graph_project):
    """An older empty prd_documents row must not blank a later regeneration."""
    state_model = await db_session.get(RequirementStateModel, graph_project)
    state_model.generated_prd = "# Materialized PRD"
    state_model.generated_diagrams = "flowchart TD\n  REQ001 --> US001"
    db_session.add(PRDDocumentModel(
        project_id=state_model.project_id,
        version=2,
        prd_markdown="",
        mermaid_diagram="",
    ))
    await db_session.flush()
    await record_prd_version(
        graph_project,
        db_session,
        generated_prd="# Latest immutable PRD",
        change_type="ai",
        version_number=3,
    )

    reloaded = await RequirementStateRepository.get_by_project_id(graph_project, db_session)

    assert reloaded["generated_prd"] == "# Latest immutable PRD"
    assert reloaded["generated_diagrams"] == "flowchart TD\n  REQ001 --> US001"


@pytest.mark.asyncio
async def test_partial_regeneration_can_be_cancelled_and_rolls_back(
    db_session, graph_project, monkeypatch
):
    await db_session.commit()
    entered = asyncio.Event()

    async def slow_regeneration(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.routes.operational.execute_regeneration", slow_regeneration)
    task = asyncio.create_task(post_regeneration(
        graph_project,
        RegenerationExecuteRequest(
            changed_artifacts=[ArtifactReferenceRequest(
                artifact_type="requirement", artifact_key="REQ-001"
            )],
            confirm=True,
        ),
        AuthenticatedUser(
            id="11111111-1111-1111-1111-111111111111",
            email="owner@example.com",
            full_name="Test Owner",
            role="Product Owner",
        ),
        db_session,
    ))
    await entered.wait()

    assert workflow_cancellations.cancel(graph_project) is True
    result = await task

    assert result["status"] == "cancelled"
    assert workflow_cancellations.is_running(graph_project) is False

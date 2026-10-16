"""Persisted artifact dependency graph and dependency-scoped regeneration."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactDependencyModel, PRDSectionModel, RegenerationRunModel
from app.merge_service import normalize_ticket_code
from app.repositories.prd_section import PRDSectionRepository
from app.repositories.requirement_state import RequirementStateRepository
from app.traceability_service import extract_codes


ALLOWED_ARTIFACT_TYPES = {
    "requirement", "user_story", "acceptance_criterion", "prd_section", "diagram",
}


def _node_id(artifact_type: str, key: str) -> str:
    return f"{artifact_type}:{key}"


def _artifact_ref(artifact_type: str, key: str) -> dict[str, str]:
    return {"artifact_type": artifact_type, "artifact_key": key}


def _active_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("status", "active") != "archived"]


def detect_changed_artifacts(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Find changed requirement/story identities between two saved-state shapes."""
    before = before or {}
    after = after or {}

    def artifact_maps(state: dict[str, Any]) -> tuple[dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]]:
        requirements: dict[str, tuple[Any, ...]] = {}
        stories: dict[str, tuple[Any, ...]] = {}
        nested_story_codes: set[str] = set()
        for requirement in _active_items(state.get("requirements")):
            req_code = str(requirement.get("requirement_code") or "").strip().upper()
            if req_code:
                requirements[req_code] = (
                    str(requirement.get("title") or "").strip(),
                    str(requirement.get("description") or "").strip(),
                )
            for story in _active_items(requirement.get("user_stories")):
                story_code = normalize_ticket_code(story.get("ticket_code"))
                if not story_code:
                    continue
                nested_story_codes.add(story_code)
                stories[story_code] = (
                    str(story.get("story_title") or "").strip(),
                    str(story.get("as_a") or "").strip(),
                    str(story.get("i_want_to") or "").strip(),
                    str(story.get("so_that") or "").strip(),
                    tuple(str(item).strip() for item in (story.get("acceptance_criteria") or [])),
                    req_code,
                )
        for story in _active_items(state.get("user_stories")):
            story_code = normalize_ticket_code(story.get("ticket_code"))
            if not story_code or story_code in nested_story_codes:
                continue
            stories[story_code] = (
                str(story.get("story_title") or "").strip(),
                str(story.get("as_a") or "").strip(),
                str(story.get("i_want_to") or "").strip(),
                str(story.get("so_that") or "").strip(),
                tuple(str(item).strip() for item in (story.get("acceptance_criteria") or [])),
                "",
            )
        return requirements, stories

    before_requirements, before_stories = artifact_maps(before)
    after_requirements, after_stories = artifact_maps(after)
    changed: list[dict[str, str]] = []
    for code in sorted(set(before_requirements) | set(after_requirements)):
        if before_requirements.get(code) != after_requirements.get(code):
            changed.append(_artifact_ref("requirement", code))
    for code in sorted(set(before_stories) | set(after_stories)):
        if before_stories.get(code) != after_stories.get(code):
            changed.append(_artifact_ref("user_story", code))
    return changed


async def mark_dependents_for_review(
    project_id: str,
    changed_artifacts: list[dict[str, str]],
    session: AsyncSession,
) -> dict[str, Any] | None:
    """Reset linked PRD sections to draft and persist the refreshed graph.

    Content is not guessed or silently rewritten. The dependency plan records
    the complete downstream scope, while each linked section is made visibly
    reviewable (including locked/human-owned sections).
    """
    if not changed_artifacts:
        return None
    plan = await build_regeneration_plan(project_id, changed_artifacts, session)
    section_keys = set(plan["regenerate_prd_sections"]) | set(plan["skipped_prd_sections"])
    if section_keys:
        result = await session.execute(
            select(PRDSectionModel).where(
                PRDSectionModel.project_id == uuid.UUID(str(project_id)),
                PRDSectionModel.section_key.in_(section_keys),
            )
        )
        for section in result.scalars().all():
            section.review_status = "draft"
        await session.flush()
    return plan


async def rebuild_dependency_graph(project_id: str, session: AsyncSession) -> dict[str, Any]:
    """Rebuild persistent edges from current normalized artifacts and references."""
    state = await RequirementStateRepository.get_by_project_id(project_id, session)
    if state is None:
        raise LookupError("Project requirement state not found")

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    code_nodes: dict[str, tuple[str, str]] = {}

    def add_node(artifact_type: str, key: Any, label: Any, **metadata: Any) -> str:
        clean_key = str(key or "").strip()
        if not clean_key:
            return ""
        node_id = _node_id(artifact_type, clean_key)
        nodes[node_id] = {
            "id": node_id,
            "artifact_type": artifact_type,
            "artifact_key": clean_key,
            "label": str(label or clean_key),
            "metadata": metadata,
        }
        return node_id

    def add_edge(
        source_type: str, source_key: str, target_type: str, target_key: str,
        relationship: str, evidence: dict[str, Any] | None = None,
    ) -> None:
        if not source_key or not target_key:
            return
        edge_key = (source_type, source_key, target_type, target_key, relationship)
        edges[edge_key] = evidence or {}

    requirements = _active_items(state.get("requirements"))
    flat_stories = _active_items(state.get("user_stories"))
    seen_story_codes: set[str] = set()

    for req_index, requirement in enumerate(requirements):
        req_code = str(requirement.get("requirement_code") or f"REQ-{req_index + 1:03d}").upper()
        add_node("requirement", req_code, requirement.get("title") or req_code)
        code_nodes[req_code] = ("requirement", req_code)
        stories = _active_items(requirement.get("user_stories"))
        if not stories and req_index == 0:
            stories = flat_stories
        for story_index, story in enumerate(stories):
            story_code = str(story.get("ticket_code") or f"{req_code}-US-{story_index + 1:03d}").upper()
            seen_story_codes.add(story_code)
            add_node("user_story", story_code, story.get("story_title") or story_code, requirement_code=req_code)
            code_nodes[story_code] = ("user_story", story_code)
            add_edge("requirement", req_code, "user_story", story_code, "contains")
            for criterion_index, criterion in enumerate(story.get("acceptance_criteria") or []):
                text = criterion.get("criteria_text") if isinstance(criterion, dict) else criterion
                criterion_key = str((criterion.get("id") if isinstance(criterion, dict) else "") or f"{story_code}:AC-{criterion_index + 1:03d}")
                add_node("acceptance_criterion", criterion_key, text or criterion_key, user_story=story_code)
                add_edge("user_story", story_code, "acceptance_criterion", criterion_key, "contains")

    # Include flat stories that were not nested under a requirement.
    for story_index, story in enumerate(flat_stories):
        story_code = str(story.get("ticket_code") or f"US-{story_index + 1:03d}").upper()
        if story_code in seen_story_codes:
            continue
        add_node("user_story", story_code, story.get("story_title") or story_code)
        code_nodes[story_code] = ("user_story", story_code)

    sections = await PRDSectionRepository.get_by_project(project_id, session)
    for section in sections:
        section_key = section["section_key"]
        add_node(
            "prd_section", section_key, section.get("title") or section_key,
            is_locked=bool(section.get("is_locked")),
            ai_generatable=bool(section.get("ai_generatable", True)),
            review_status=section.get("review_status") or "draft",
        )
        for code in extract_codes(section.get("content") or ""):
            source = code_nodes.get(code.upper())
            if source:
                add_edge(*source, "prd_section", section_key, "referenced_by", {"code": code.upper()})

    # Product scope is the deterministic template destination for requirements,
    # stories and criteria, including newly-created artifacts not yet mentioned
    # in the old document. These inferred edges close that important gap in a
    # reference-only scan.
    if any(section.get("section_key") == "product_scope" for section in sections):
        for node in list(nodes.values()):
            if node["artifact_type"] in {"requirement", "user_story", "acceptance_criterion"}:
                add_edge(
                    node["artifact_type"], node["artifact_key"],
                    "prd_section", "product_scope", "renders_in", {"derived": True},
                )

    diagram = (state.get("generated_diagrams") or "").strip()
    # The current diagram is a dependency target even before its first source
    # exists, allowing a scoped run to create a missing diagram.
    add_node("diagram", "current", "Current architecture flow", exists=bool(diagram))
    referenced = {code.upper() for code in extract_codes(diagram)}
    for code, source in code_nodes.items():
        relationship = "referenced_by" if code in referenced else "renders_in"
        add_edge(*source, "diagram", "current", relationship, {"derived": code not in referenced})

    await session.execute(
        delete(ArtifactDependencyModel).where(
            ArtifactDependencyModel.project_id == uuid.UUID(str(project_id))
        )
    )
    for (source_type, source_key, target_type, target_key, relationship), evidence in edges.items():
        session.add(ArtifactDependencyModel(
            project_id=uuid.UUID(str(project_id)),
            source_type=source_type,
            source_key=source_key,
            target_type=target_type,
            target_key=target_key,
            relationship=relationship,
            evidence=evidence,
        ))
    await session.flush()

    return {
        "project_id": str(project_id),
        "nodes": sorted(nodes.values(), key=lambda item: (item["artifact_type"], item["artifact_key"])),
        "edges": [
            {
                "source": _node_id(source_type, source_key),
                "target": _node_id(target_type, target_key),
                "relationship": relationship,
                "evidence": evidence,
            }
            for (source_type, source_key, target_type, target_key, relationship), evidence in sorted(edges.items())
        ],
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "requirements": sum(n["artifact_type"] == "requirement" for n in nodes.values()),
            "stories": sum(n["artifact_type"] == "user_story" for n in nodes.values()),
            "sections": sum(n["artifact_type"] == "prd_section" for n in nodes.values()),
            "diagrams": sum(n["artifact_type"] == "diagram" for n in nodes.values()),
        },
        "rebuilt_at": datetime.now(timezone.utc).isoformat(),
    }


async def build_regeneration_plan(
    project_id: str,
    changed_artifacts: Iterable[dict[str, str]],
    session: AsyncSession,
) -> dict[str, Any]:
    """Traverse the current graph and return the minimal downstream write set."""
    graph = await rebuild_dependency_graph(project_id, session)
    nodes = {node["id"]: node for node in graph["nodes"]}
    adjacency: dict[str, set[str]] = {}
    for edge in graph["edges"]:
        adjacency.setdefault(edge["source"], set()).add(edge["target"])

    triggers: list[dict[str, str]] = []
    queue: deque[str] = deque()
    visited: set[str] = set()
    for raw in changed_artifacts:
        artifact_type = str(raw.get("artifact_type") or "").strip().lower()
        artifact_key = str(raw.get("artifact_key") or "").strip()
        if artifact_type not in ALLOWED_ARTIFACT_TYPES or not artifact_key:
            raise ValueError(f"Invalid artifact reference: {artifact_type}:{artifact_key}")
        if artifact_type in {"requirement", "user_story"}:
            artifact_key = artifact_key.upper()
        ref = _artifact_ref(artifact_type, artifact_key)
        if ref not in triggers:
            triggers.append(ref)
            node_id = _node_id(artifact_type, artifact_key)
            queue.append(node_id)
            visited.add(node_id)

    while queue:
        current = queue.popleft()
        for target in adjacency.get(current, set()):
            if target not in visited:
                visited.add(target)
                queue.append(target)

    # Removed artifacts are intentionally absent from the freshly rebuilt
    # graph. They still invalidate the deterministic product scope and current
    # architecture flow, so retain a safe minimal fallback for those triggers.
    for trigger in triggers:
        trigger_id = _node_id(trigger["artifact_type"], trigger["artifact_key"])
        if trigger_id in nodes or trigger["artifact_type"] not in {"requirement", "user_story"}:
            continue
        for fallback_id in ("prd_section:product_scope", "diagram:current"):
            if fallback_id in nodes:
                visited.add(fallback_id)

    affected = [nodes[node_id] for node_id in visited if node_id in nodes]
    stories = sorted({n["artifact_key"] for n in affected if n["artifact_type"] == "user_story"})
    section_nodes = [n for n in affected if n["artifact_type"] == "prd_section"]
    writable_sections = sorted({
        n["artifact_key"] for n in section_nodes
        if not n["metadata"].get("is_locked") and n["metadata"].get("ai_generatable", True)
    })
    skipped_sections = sorted({
        n["artifact_key"] for n in section_nodes
        if n["metadata"].get("is_locked") or not n["metadata"].get("ai_generatable", True)
    })
    diagrams = sorted({n["artifact_key"] for n in affected if n["artifact_type"] == "diagram"})

    return {
        "project_id": str(project_id),
        "changed_artifacts": triggers,
        "affected_user_stories": stories,
        "regenerate_prd_sections": writable_sections,
        "skipped_prd_sections": skipped_sections,
        "regenerate_diagrams": diagrams,
        "affected_node_ids": sorted(visited),
        "summary": {
            "stories": len(stories),
            "sections": len(writable_sections),
            "skipped_sections": len(skipped_sections),
            "diagrams": len(diagrams),
        },
    }


async def execute_regeneration(
    project_id: str,
    changed_artifacts: list[dict[str, str]],
    requested_by: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """Execute a plan using the existing Architect with a strict write scope."""
    plan = await build_regeneration_plan(project_id, changed_artifacts, session)
    run = RegenerationRunModel(
        project_id=uuid.UUID(str(project_id)),
        requested_by=requested_by,
        trigger_artifacts=plan["changed_artifacts"],
        plan=plan,
        status="running",
        skipped_locked_sections=plan["skipped_prd_sections"],
    )
    session.add(run)
    await session.flush()

    if not plan["regenerate_prd_sections"] and not plan["regenerate_diagrams"]:
        run.status = "no_changes"
        run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return {"run_id": str(run.id), "status": run.status, "plan": plan}

    try:
        from app.agents import architect_node

        # Keep every generated artifact/version/message inside a savepoint. A
        # failed Architect run rolls back its partial writes while the outer
        # transaction can still retain the failed run record for diagnosis.
        async with session.begin_nested():
            current = await RequirementStateRepository.get_by_project_id(project_id, session) or {}
            result = await architect_node({
                "project_id": project_id,
                "db_session": session,
                "current_version": current.get("version_number", 1),
                "version_history_summaries": "Dependency-scoped regeneration.",
                "regeneration_scope": {
                    "section_keys": plan["regenerate_prd_sections"],
                    "regenerate_diagram": bool(plan["regenerate_diagrams"]),
                },
            })
            generated_state = result.get("requirement_state") or {}
            artifact_updates: dict[str, Any] = {"current_workflow_state": "COMPLETED"}
            if generated_state.get("generated_prd"):
                artifact_updates["generated_prd"] = generated_state["generated_prd"]
            if "generated_diagrams" in generated_state:
                # The repository treats an explicit empty string as a real clear.
                artifact_updates["generated_diagrams"] = generated_state.get("generated_diagrams") or ""
            await RequirementStateRepository.save_or_update(project_id, artifact_updates, session)
            await rebuild_dependency_graph(project_id, session)
            await session.flush()
        run.status = "completed"
        run.regenerated_sections = plan["regenerate_prd_sections"]
        run.diagram_regenerated = bool(result.get("diagram_regenerated"))
        run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return {
            "run_id": str(run.id),
            "status": run.status,
            "plan": plan,
            "generated_prd": generated_state.get("generated_prd") or "",
            "generated_diagrams": generated_state.get("generated_diagrams") or "",
        }
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)[:4000]
        run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return {
            "run_id": str(run.id),
            "status": run.status,
            "plan": plan,
            "error_message": run.error_message,
        }

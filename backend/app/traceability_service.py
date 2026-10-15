"""
Requirement Traceability service (derived, read-only).

Builds the Requirement Traceability Matrix for a project by DERIVING the links
between artifacts — no new linkage tables:

  Requirement  ->  User Stories            (existing FK ``user_stories.requirement_id``)
  User Story   ->  Acceptance Criteria     (existing FK ``acceptance_criteria.user_story_id``)
  Requirement  <-> PRD Sections            (code matching: ``REQ-###`` / ``US-###`` found
                                            in ``prd_sections.content``)
  Requirement  <-> Diagrams                (code matching inside each
                                            ``prd_documents.mermaid_diagram`` source)

Because PRD sections and diagrams are stored per-project (not per-requirement),
the code references found inside their markdown/mermaid source ARE the link.
Anything NOT linked is reported as an explicit coverage gap so BA teams can act
on it — the classic purpose of traceability.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AcceptanceCriteriaModel, EpicModel, RequirementModel, UserStoryModel
from app.repositories.base import as_uuid
from app.repositories.prd import PRDDocumentRepository
from app.repositories.prd_section import PRDSectionRepository

logger = logging.getLogger(__name__)

# A requirement code ("REQ-001") or user-story ticket code ("US-001").
CODE_PATTERN = re.compile(r"\b(REQ-\d{1,4}|US-\d{1,4})\b", re.IGNORECASE)


def extract_codes(text: Optional[str]) -> Set[str]:
    """Extract normalized artifact codes (uppercased) from free text."""
    if not text:
        return set()
    return {match.upper() for match in CODE_PATTERN.findall(text)}


class TraceabilityService:
    """Builds the derived Requirement Traceability Matrix for a project."""

    @staticmethod
    async def build_traceability(project_id: str, session: AsyncSession) -> Dict[str, Any]:
        pid = as_uuid(project_id)

        # ------------------------------------------------------------------
        # 1. Load every artifact of the project with a handful of queries.
        # ------------------------------------------------------------------
        req_res = await session.execute(
            select(RequirementModel).where(RequirementModel.project_id == pid)
        )
        requirements = req_res.scalars().all()

        epic_res = await session.execute(select(EpicModel).where(EpicModel.project_id == pid))
        epic_names: Dict[Any, str] = {e.id: e.epic_name for e in epic_res.scalars().all()}

        story_res = await session.execute(
            # NOTE: filter through the `requirements` join, NEVER on
            # ``UserStoryModel.project_id`` directly. The live Postgres schema
            # has ``user_stories.project_id`` as VARCHAR while the model types
            # it as GUID/UUID — ``varchar = uuid`` fails with UndefinedFunction.
            # This mirrors UserStoryRepository.get_by_project (line ~162).
            select(UserStoryModel)
            .join(RequirementModel, UserStoryModel.requirement_id == RequirementModel.id)
            .where(RequirementModel.project_id == pid)
        )
        active_stories = [s for s in story_res.scalars().all() if (s.status or "active") == "active"]

        ac_by_story: Dict[Any, List[AcceptanceCriteriaModel]] = {}
        if requirements:
            ac_res = await session.execute(
                select(AcceptanceCriteriaModel, UserStoryModel.id)
                .join(UserStoryModel, AcceptanceCriteriaModel.user_story_id == UserStoryModel.id)
                .where(UserStoryModel.requirement_id.in_([r.id for r in requirements]))
            )
            for ac, story_id in ac_res.all():
                if (ac.status or "active") == "active":
                    ac_by_story.setdefault(story_id, []).append(ac)

        sections = await PRDSectionRepository.get_by_project(project_id, session)
        prds = await PRDDocumentRepository.get_by_project(project_id, session)
        return _assemble(
            project_id,
            requirements,
            epic_names,
            active_stories,
            ac_by_story,
            sections,
            prds,
        )


def _assemble(
    project_id: str,
    requirements: List[RequirementModel],
    epic_names: Dict[Any, str],
    stories: List[UserStoryModel],
    ac_by_story: Dict[Any, List[AcceptanceCriteriaModel]],
    sections: List[Dict[str, Any]],
    prds: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the traceability matrix + coverage gaps from loaded artifacts."""
    requirement_codes = {(r.requirement_code or "").upper(): r for r in requirements}
    all_story_codes = {(s.ticket_code or "").upper() for s in stories if s.ticket_code}

    stories_by_requirement: Dict[Any, List[UserStoryModel]] = {}
    for story in stories:
        stories_by_requirement.setdefault(story.requirement_id, []).append(story)

    # -- Index PRD sections by the codes they reference ------------------------
    section_links: Dict[str, Set[str]] = {}
    stale_section_refs: Dict[str, Set[str]] = {}
    for section in sections:
        for code in extract_codes(section.get("content")):
            if code in requirement_codes or code in all_story_codes:
                section_links.setdefault(code, set()).add(section["section_key"])
            else:
                stale_section_refs.setdefault(code, set()).add(section["section_key"])

    # -- Index diagrams (one per PRD version with mermaid source) --------------
    diagram_links: Dict[str, Set[str]] = {}
    stale_diagram_refs: Dict[str, Set[str]] = {}
    diagrams: List[Dict[str, Any]] = []
    for prd in prds:
        mermaid = prd.get("mermaid_diagram") or ""
        if not mermaid:
            continue
        label = f"PRD v{prd['version']}"
        diagrams.append({"label": label, "version": prd["version"]})
        for code in extract_codes(mermaid):
            if code in requirement_codes or code in all_story_codes:
                diagram_links.setdefault(code, set()).add(label)
            else:
                stale_diagram_refs.setdefault(code, set()).add(label)

    rows: List[Dict[str, Any]] = []
    requirements_without_stories: List[str] = []
    requirements_without_sections: List[str] = []
    requirements_without_diagram: List[str] = []
    stories_without_criteria: List[str] = []
    stories_without_sections: List[str] = []
    criteria_needing_review: List[str] = []

    for req in requirements:
        code = (req.requirement_code or "").upper()
        req_stories = sorted(stories_by_requirement.get(req.id, []), key=lambda s: s.ticket_code or "")

        story_entries: List[Dict[str, Any]] = []
        for story in req_stories:
            story_code = (story.ticket_code or "").upper()
            criterion_rows = ac_by_story.get(story.id, [])
            criteria = [criterion.criteria_text for criterion in criterion_rows]
            needs_review = [
                criterion.criteria_text
                for criterion in criterion_rows
                if criterion.change_type == "review_required"
                or (
                    bool(criterion.is_locked)
                    and story.updated_at is not None
                    and criterion.updated_at is not None
                    and story.updated_at > criterion.updated_at
                )
            ]
            if needs_review:
                criteria_needing_review.append(story_code)
            if not criteria:
                stories_without_criteria.append(story_code)
            if not (section_links.get(story_code) or section_links.get(code)):
                stories_without_sections.append(story_code)
            story_entries.append(
                {
                    "id": str(story.id),
                    "ticket_code": story.ticket_code,
                    "story_title": story.story_title,
                    "as_a": story.as_a,
                    "i_want_to": story.i_want_to,
                    "so_that": story.so_that,
                    "acceptance_criteria": criteria,
                    "criteria_needing_review": needs_review,
                    "is_locked": bool(story.is_locked),
                }
            )
        # Sections/diagrams traced to this requirement directly, plus indirectly
        # via any of its stories' ticket codes.
        req_sections = set(section_links.get(code, set()))
        req_diagrams = set(diagram_links.get(code, set()))
        for story in story_entries:
            story_code = (story["ticket_code"] or "").upper()
            req_sections |= section_links.get(story_code, set())
            req_diagrams |= diagram_links.get(story_code, set())

        if not story_entries:
            requirements_without_stories.append(code)
        if not req_sections:
            requirements_without_sections.append(code)
        if not req_diagrams:
            requirements_without_diagram.append(code)

        rows.append(
            {
                "requirement": {
                    "id": str(req.id),
                    "requirement_code": req.requirement_code,
                    "title": req.title,
                    "description": req.description or "",
                    "epic_name": epic_names.get(req.epic_id),
                    "status": req.status,
                    "version": req.version,
                    "is_locked": bool(req.is_locked),
                },
                "user_stories": story_entries,
                "prd_sections": sorted(req_sections),
                "diagrams": sorted(req_diagrams),
                # Fully traced = has stories AND criteria AND is referenced by
                # at least one PRD section. Diagram linkage is reported but NOT
                # required for "traced" (valid PRDs often embed one shared flow).
                "traced": bool(story_entries)
                and all(bool(s["acceptance_criteria"]) for s in story_entries)
                and bool(req_sections),
            }
        )


    coverage = {
        "total_requirements": len(rows),
        "total_user_stories": len(stories),
        "total_acceptance_criteria": sum(len(v) for v in ac_by_story.values()),
        "traced_requirements": sum(1 for r in rows if r["traced"]),
        "requirements_without_stories": requirements_without_stories,
        "requirements_without_prd_sections": requirements_without_sections,
        "requirements_without_diagram": requirements_without_diagram,
        "stories_without_criteria": stories_without_criteria,
        "stories_without_prd_sections": stories_without_sections,
        "criteria_needing_review": sorted(set(criteria_needing_review)),
        "stale_section_references": [
            {"code": code, "section_keys": sorted(keys)}
            for code, keys in sorted(stale_section_refs.items())
        ],
        "stale_diagram_references": [
            {"code": code, "diagrams": sorted(keys)}
            for code, keys in sorted(stale_diagram_refs.items())
        ],
    }

    return {
        "project_id": str(project_id),
        "rows": rows,
        # Deterministic order: oldest PRD version first.
        "diagrams": sorted(diagrams, key=lambda d: d["version"]),
        "coverage": coverage,
    }


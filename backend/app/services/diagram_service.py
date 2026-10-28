"""Mermaid validation and generation for Architect workflow runs."""

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate

from app.prompt_loader import load_prompt
from app.schemas import MermaidDiagramResult

logger = logging.getLogger("app.agents")
_MERMAID_HEADER_RE = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|LR|RL)\b", re.IGNORECASE)


# ARCHITECT 4.2 — Strip optional code fences and accept only supported Mermaid
# flowchart/graph headers and directions; invalid output becomes an empty string.
def sanitize_mermaid_diagram(text: Any) -> str:
    """Return renderable flowchart source, or an empty string when invalid."""
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean).strip()
    if not clean or not _MERMAID_HEADER_RE.match(clean):
        return ""
    return clean


# ARCHITECT 4.3 — Execute the diagram prompt and fail open.
# Returns validated Mermaid from 4.2, or "" for model, parse, or validation failure so
# the PRD candidate can still succeed without retaining a stale requested diagram.
async def generate_flow_diagram(
    project_id: str,
    req_state: Dict[str, Any],
    project_data: Dict[str, Any],
    *,
    llm: Any,
    invoke_structured: Callable[..., Awaitable[Dict[str, Any]]],
) -> str:
    """Generate a validated flowchart; diagram failures intentionally fail open."""
    try:
        dataset = {
            "project_name": req_state.get("project_name", "") or "",
            "version": req_state.get("version_number", 1),
            **(project_data or {}),
        }
        prompt = PromptTemplate(
            template=load_prompt("architect_diagram"),
            input_variables=[
                "project_name",
                "current_version",
                "project_dataset",
                "format_instructions",
            ],
        )
        instructions = PydanticOutputParser(
            pydantic_object=MermaidDiagramResult
        ).get_format_instructions()

        def is_flowchart(result: Dict[str, Any]) -> bool:
            return bool(sanitize_mermaid_diagram(result.get("mermaid_diagram", "")))

        result = await invoke_structured(
            llm,
            prompt,
            MermaidDiagramResult,
            variables={
                "project_name": dataset.get("project_name") or "Untitled Project",
                "current_version": dataset.get("version", 1),
                "project_dataset": json.dumps(dataset, ensure_ascii=False),
                "format_instructions": "Output ONLY raw JSON. No markdown.",
            },
            description="architect flow diagram",
            format_instructions=instructions,
            validate=is_flowchart,
        )
        diagram = sanitize_mermaid_diagram(result.get("mermaid_diagram", ""))
        if diagram:
            logger.info(
                "[ARCHITECT] Generated flow diagram for project %s (%s chars).",
                project_id,
                len(diagram),
            )
        else:
            logger.warning("[ARCHITECT] Flow diagram for project %s was invalid.", project_id)
        return diagram
    except Exception as exc:
        logger.error(
            "[ARCHITECT] Flow diagram generation failed for project %s: %s", project_id, exc
        )
        return ""

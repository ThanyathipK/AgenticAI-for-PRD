"""Format-neutral Product Requirements Document models.

These models deliberately describe the boundary between application/domain
code and output formatting.  They contain no Markdown, LaTeX, DOCX, or PDF
logic.  The permissive nested item types are intentional for the first phase
of the migration: requirement and story schemas are still shared with older
projects, while the document-level contract is now explicit and validated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class PRDMetadata(BaseModel):
    """Cover, ownership, and version information for a PRD."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = ""
    project_name: str = ""
    version: int = Field(default=1, ge=1)
    version_label: Optional[str] = None
    author: str = "Product Owner"
    status: str = "Draft"
    pmo_no: Optional[str] = None
    today: Optional[str] = None
    version_summary: str = ""
    version_history: Optional[str] = None
    version_author: Optional[str] = None
    stakeholders: Dict[str, str] = Field(default_factory=dict)
    business_topics: Dict[str, str] = Field(default_factory=dict)
    reviews: Optional[List[Dict[str, str]]] = None


class PRDContent(BaseModel):
    """Canonical, renderer-independent PRD content.

    Nested records remain dictionaries during the staged migration so legacy
    snapshots retain every field.  Unlike the old top-level ``data`` mapping,
    the supported document fields are discoverable and typo-checked.
    """

    model_config = ConfigDict(extra="forbid")

    epic_name: str = ""
    business_goals: List[Any] = Field(default_factory=list)
    actors: List[Any] = Field(default_factory=list)
    requirements: List[Any] = Field(default_factory=list)
    user_stories: List[Any] = Field(default_factory=list)
    acceptance_criteria: List[Any] = Field(default_factory=list)
    scope_in: List[Any] = Field(default_factory=list)
    scope_out: List[Any] = Field(default_factory=list)
    problem_statement: List[Any] = Field(default_factory=list)
    user_flow: Any = None


class CanonicalPRD(BaseModel):
    """The application-level PRD passed to all output renderers."""

    model_config = ConfigDict(extra="forbid")

    metadata: PRDMetadata = Field(default_factory=PRDMetadata)
    content: PRDContent = Field(default_factory=PRDContent)
    narrative: Dict[str, Any] = Field(default_factory=dict)

    def template_filler_kwargs(self) -> Dict[str, Any]:
        """Compatibility adapter for the existing deterministic LaTeX filler.

        Keeping this adapter on the boundary lets renderers migrate one at a
        time without making domain code understand the old formatter API.
        Pydantic returns fresh dictionaries, preventing a renderer from
        mutating the canonical document.
        """

        metadata = self.metadata
        return {
            "project_id": metadata.project_id,
            "project_name": metadata.project_name,
            "version": metadata.version,
            "author": metadata.author,
            "status": metadata.status,
            "pmo_no": metadata.pmo_no,
            "stakeholders": dict(metadata.stakeholders),
            "business_topics": dict(metadata.business_topics),
            "reviews": (
                [dict(review) for review in metadata.reviews]
                if metadata.reviews is not None
                else None
            ),
            "today": metadata.today,
            "data": self.content.model_dump(),
            "narrative": self.narrative.copy(),
            "version_summary": metadata.version_summary,
            "version_history": metadata.version_history,
            "version_label": metadata.version_label,
            "version_author": metadata.version_author,
        }

"""Canonical PRD domain model and format-specific renderers.

New code should pass :class:`CanonicalPRD` across the domain/rendering boundary
instead of passing the argument bundle accepted by the legacy template filler.
"""

from app.prd.model import CanonicalPRD, PRDContent, PRDMetadata

__all__ = ["CanonicalPRD", "PRDContent", "PRDMetadata"]

"""Format-specific renderers for :mod:`app.prd` domain documents.

The LaTeX renderer is the compatibility bridge to the proven official
template filler.  Markdown and DOCX derive from that same rendered source, so
no output formatter needs to reconstruct domain data from another format.
"""

from app.prd.model import CanonicalPRD


def render_latex(document: CanonicalPRD) -> str:
    """Render the official deterministic LaTeX template."""
    from app.prd_filler import fill_template_body

    return fill_template_body(**document.template_filler_kwargs())


def render_markdown(document: CanonicalPRD) -> str:
    """Render a Markdown projection of the canonical document."""
    from app.latex_service import prd_to_markdown

    return prd_to_markdown(render_latex(document))


def render_docx(document: CanonicalPRD) -> bytes:
    """Render a DOCX projection of the canonical document."""
    from app.latex_service import convert_latex_to_docx

    return convert_latex_to_docx(render_latex(document))

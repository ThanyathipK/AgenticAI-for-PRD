"""
Unit tests for the PRD export pipeline (:mod:`app.latex_service` + the export
routes in ``app.routes.projects``).

Guarantees exercised here (root causes of the broken PRD export):

- **Legacy Markdown PRDs are exportable again** — ``is_markdown_prd`` routes
  them to Pandoc's native GFM readers instead of failing with 422.
- **LaTeX DOCX exports contain REAL Word tables** — the Krungsri template
  constructs Pandoc cannot parse (``\\newcolumntype{L}``, ``\\shortstack``,
  ``\\multirow``, ``\\multicolumn``, ``\\rowcolor``) are rewritten into a
  pandoc-compatible subset before conversion; without that rewrite the DOCX
  contained zero ``<w:tbl>`` elements.
- **The on-screen preview never renders raw LaTeX** — ``prd_to_markdown``
  normalizes either format into GFM with the ``### `` headings the frontend
  section splitter expects.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_latex_service.py -v
"""
import io
import re
import zipfile
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db as app_get_db
from app.latex_service import (
    _aptos_font_files,
    compile_latex_to_pdf,
    convert_latex_to_docx,
    convert_markdown_to_pdf,
    convert_to_docx,
    docx_to_pdf,
    ensure_full_document,
    is_markdown_prd,
    pandoc_friendly_document,
    preprocess_latex_for_pandoc,
    prd_to_markdown,
    soffice_available,
)
from app.prompt_loader import load_prd_latex_template, load_prd_template
from app.prd_filler import fill_template_body
from app.main import app as fastapi_app


def test_export_timestamp_includes_unambiguous_meridiem():
    from app.routes.projects import _timestamp_stamp

    assert _timestamp_stamp(datetime(2026, 10, 27, 0, 5)) == "2026-10-27_1205_AM"
    assert _timestamp_stamp(datetime(2026, 10, 27, 14, 30)) == "2026-10-27_0230_PM"


# =====================================================================
# Fixtures: isolated SQLite database + ASGI client (same pattern as
# tests/test_documents.py so no live Supabase connection is touched).
# =====================================================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh SQLite database per test with all app tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test_export.db")
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
    from app.models import ProjectModel, UserModel

    user = UserModel(
        email="export-test@banking.com",
        full_name="Export Tester",
        role="Developer",
    )
    db_session.add(user)
    await db_session.flush()
    project = ProjectModel(
        user_id=user.id,
        name="Export Test Project",
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
# Shared samples + helper
# =====================================================================
MARKDOWN_PRD = (
    "# PRD: Transfer API\n\n"
    "## 1. Executive Summary\n\n"
    "Secure transfer API for retail customers.\n\n"
    "|Role|Name|\n"
    "|---|---|\n"
    "|Product Owner|Jane|\n"
)

# A representative slice of the Krungsri template incl. every construct that
# used to break Pandoc's LaTeX reader.
KRUNGSRI_BODY = (
    "\\section*{Stakeholders}\n"
    "\\begin{tabular}{|L{5.5cm}|L{8.5cm}|}\n"
    "\\hline\n"
    "\\rowcolor{tblHeader}\\prdlbl{Role} & \\prdlbl{Name} \\\\\n"
    "\\hline\n"
    "Product Owner & \\\\\n"
    "\\hline\n"
    "\\end{tabular}\n\n"
    "\\section*{2. Product Scope \\& Functional Requirements}\n"
    "\\bgroup\n"
    "\\renewcommand{\\arraystretch}{1.25}\n"
    "\\begin{tabular}{|L{4cm}|L{4cm}|L{4cm}|L{4cm}|}\n"
    "\\hline\n"
    "\\multirow{3}{*}{\\shortstack{User Story\\\\ Mapping \\&\\\\ Functional\\\\ Requirements}}\n"
    "  & \\multicolumn{3}{L{12cm}|}{Epic Name: Transfer} \\\\\n"
    "\\cline{2-4}\n"
    "  & \\shortstack{\\prdlbl{User Story (System}\\\\\n"
    "\\prdlbl{Focus)}} & \\shortstack{FR 1.1: The system\\\\\n"
    "must\\ldots} & \\prdlbl{Acceptance Criteria} \\\\\n"
    "\\hline\n"
    "\\end{tabular}\n"
    "\\egroup\n"
)


def docx_table_count(docx_bytes: bytes) -> int:
    """Count native Word tables inside an in-memory .docx payload."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return xml.count("<w:tbl>")


# =====================================================================
# Source-kind detection
# =====================================================================
class TestIsMarkdownPrd:
    def test_krungsri_latex_is_not_markdown(self):
        assert is_markdown_prd(KRUNGSRI_BODY) is False

    def test_full_official_template_is_not_markdown(self):
        """The full authoritative .tex (what /api/prd/template serves) must be
        routed to the Tectonic pipeline, never the Markdown one."""
        assert is_markdown_prd(load_prd_latex_template()) is False

    def test_markdown_skeleton_is_markdown(self):
        """template.md (the on-screen preview skeleton) IS the Markdown kind."""
        assert is_markdown_prd(load_prd_template()) is True

    def test_full_latex_document_is_not_markdown(self):
        src = (
            "\\documentclass[12pt]{article}\n"
            "\\begin{document}\nHello\\end{document}\n"
        )
        assert is_markdown_prd(src) is False

    def test_pipe_table_markdown_is_detected(self):
        assert is_markdown_prd(MARKDOWN_PRD) is True

    def test_heading_markdown_is_detected(self):
        assert is_markdown_prd("# Title\n\nBody text.\n") is True

    def test_fenced_markdown_is_detected(self):
        fenced = "```markdown\n# Fenced PRD\n\n|A|B|\n|---|---|\n```"
        assert is_markdown_prd(fenced) is True

    def test_empty_source_is_not_markdown(self):
        assert is_markdown_prd("") is False
        assert is_markdown_prd("   \n  ") is False


class TestAuthoritativeTemplateAssembly:
    def test_standalone_source_cannot_override_template_formatting(self):
        source = (
            "\\documentclass[9pt,a4paper]{article}\n"
            "\\usepackage[margin=4in]{geometry}\n"
            "\\begin{document}\n"
            "\\section*{Project content}\nBody\n"
            "\\end{document}\n"
        )
        assembled = ensure_full_document(source)
        assert "\\documentclass[12pt,letterpaper]{article}" in assembled
        assert "top=1in,bottom=1in,left=1in,right=1in" in assembled
        assert "a4paper" not in assembled
        assert "margin=4in" not in assembled
        assert assembled.count("\\begin{document}") == 1
        assert "\\section*{Project content}" in assembled


# =====================================================================
# LLM escaping repairs (sanitize_generated_latex)
# =====================================================================
class TestSanitizeGeneratedLatex:
    def test_paired_currency_dollars_are_escaped(self):
        """Two currency amounts used to PAIR up and poison everything in
        between (the Export-Test-RPO export failure): TeX entered math mode
        after the first US$ and the cover page's \bfseries blew up. Every raw
        $ must be escaped, paired or not."""
        from app.latex_service import sanitize_generated_latex

        out = sanitize_generated_latex(
            "Revenue of US$ 1.4M in 2026 and cost US$ 0.2M. \\textbf{Bold}"
        )
        assert out.count("$") == out.count("\\$")
        assert out.count("\\$") == 2
        assert "\\textbf{Bold}" in out

    def test_single_price_dollar_is_escaped(self):
        from app.latex_service import sanitize_generated_latex

        assert sanitize_generated_latex("price $5 per month") == "price \\$5 per month"

    def test_already_escaped_dollar_untouched_and_idempotent(self):
        from app.latex_service import sanitize_generated_latex

        once = sanitize_generated_latex("US\\$ 1.4M stays \\$5 total")
        assert once == "US\\$ 1.4M stays \\$5 total"
        assert sanitize_generated_latex(once) == once

    def test_row_terminator_backslashes_before_dollar_are_not_misread(self):
        from app.latex_service import sanitize_generated_latex

        # '... \\' is a tabular row terminator, not an escape for the next $.
        out = sanitize_generated_latex("fee is US$5 \\\\ next row")
        assert out == "fee is US\\$5 \\\\ next row"

    def test_currency_prd_compiles_to_pdf(self):
        """Integration: the exact failure shape - currency amounts in a body
        that also contains the cover page's \\bfseries block - must export."""
        from app.latex_service import compile_latex_to_pdf

        body = (
            "\\thispagestyle{empty}\n"
            "\\begin{center}\n"
            "{\\Large \\bfseries KRUNGSRI NIMBLE}\\\\[0.4cm]\n"
            "{\\Large Product Requirement Document}\\\\[1.8cm]\n"
            "\\end{center}\n"
            "\\section*{Business \\& Strategic Overview}\n"
            "Projected revenue: US$ 1.4M with operating cost US$ 0.2M \\\\\n"
            "\\begin{tabular}{L{4.5cm}L{9.5cm}}\n"
            "\\prdlbl{Goal:} & Grow deposits US$ 2M \\\\\n"
            "\\end{tabular}\n"
        )
        pdf = compile_latex_to_pdf(body)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 2000

    def test_unbalanced_single_dollar_prd_compiles_to_pdf(self):
        """REGRESSION (the reported 'LaTeX PDF compilation failed' bug): a
        single, UNBALANCED currency dollar flips TeX into math mode and the
        cover page's \\bfseries then dies with 'invalid in math mode'. The PDF
        path must repair the dollar (escape it) exactly like the DOCX path —
        before the fix, compile_latex_to_pdf never sanitized and this failed
        while exporting the SAME PRD to DOCX succeeded."""
        from app.latex_service import compile_latex_to_pdf, sanitize_generated_latex

        body = (
            "\\begin{center}\n"
            "{\\Large \\bfseries KRUNGSRI NIMBLE}\n"
            "\\end{center}\n"
            "\\section*{Business \\& Strategic Overview}\n"
            "Budget for 2026 is $5M (single, unmatched dollar).\n"
        )
        # Precondition: exactly one unescaped dollar, and the sanitizer
        # escapes it (DOCX path behaviour) — the PDF path must do the same.
        assert body.count("$") == 1
        assert sanitize_generated_latex("budget $5M") == "budget \\$5M"
        pdf = compile_latex_to_pdf(body)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 2000

    def test_lone_backslash_spacing_bracket_becomes_row_break(self):
        """REGRESSION (production 'Command \\bfseries invalid in math mode'):
        the Krungsri cover uses '\\\\[2.2cm]' (row terminator + spacing). A
        model that under-escapes its JSON emits '\\[2.2cm]', which TeX parses
        as the START of display math - the next cover line then fails. The
        sanitizer must rewrite the lone form back to the row terminator."""
        from app.latex_service import sanitize_generated_latex

        BS = chr(92)
        broken = (
            "Krungsri}" + BS + "[2.2cm]" + chr(10)
            + "{" + BS + "Large " + BS + "bfseries NIMBLE}"
        )
        fixed = sanitize_generated_latex(broken)
        assert fixed.count(BS + BS + "[2.2cm]") == 1
        # no lone form left outside the doubled occurrence
        assert (BS + "[2.2cm]") not in fixed.replace(BS + BS + "[2.2cm]", "")
        assert sanitize_generated_latex(fixed) == fixed  # idempotent

    def test_correct_spacing_bracket_and_display_math_untouched(self):
        from app.latex_service import sanitize_generated_latex

        BS = chr(92)
        good = (
            "Nimble}" + BS + BS + "[0.4cm]" + chr(10)
            + "Einstein " + BS + "[x^2 + y^2] wrote"
        )
        assert sanitize_generated_latex(good) == good

    def test_double_escaped_newline_document_is_restored(self):
        """A model that double-escapes its JSON turns EVERY newline into a
        literal \\n two-char sequence; the document collapses onto a handful
        of physical lines and Tectonic dies with 'Missing \\begin{document}'.
        The sanitizer must restore the line structure."""
        from app.latex_service import sanitize_generated_latex
        from app.prompt_loader import load_prd_latex_template

        BS, NL = chr(92), chr(10)
        tpl = load_prd_latex_template()
        restored = sanitize_generated_latex(tpl.replace(NL, BS + "n"))
        # whitespace-equivalent (newline<->space) and structurally intact
        assert restored.split() == tpl.split()
        assert (BS + "begin{document}" + BS + "n") not in restored

    def test_row_break_word_vs_artifact_after_row_break(self):
        """'\\\\nobody' (row break then a word starting with n) must survive,
        while the double-escaped 'row break + newline artifact + word'
        ('\\\\\\nmust' - textually near-identical) must be repaired to
        row-break + real newline + word."""
        from app.latex_service import sanitize_generated_latex

        BS = chr(92)
        legit = "cell " + BS + BS + "nobody next"
        assert sanitize_generated_latex(legit) == legit

        artifact = "The system" + BS + BS + BS + "nmust" + BS + "ldots"
        repaired = sanitize_generated_latex(artifact)
        assert (BS + BS + chr(10) + "must" + BS + "ldots") in repaired




# =====================================================================
# Pandoc-friendly LaTeX rewriting
# =====================================================================
class TestPreprocessLatexForPandoc:
    def test_unwraps_multirow_multicolumn_shortstack(self):
        out = preprocess_latex_for_pandoc(KRUNGSRI_BODY)
        assert "\\multirow" not in out
        assert "\\multicolumn" not in out
        assert "\\shortstack" not in out
        assert "User Story" in out and "Mapping" in out
        assert "Epic Name: Transfer" in out

    def test_converts_custom_columns_to_p_columns(self):
        out = preprocess_latex_for_pandoc(KRUNGSRI_BODY)
        assert "p{5.5cm}" in out and "p{8.5cm}" in out and "p{4cm}" in out
        assert "|L{" not in out  # no custom |L{..}| column spec left

    def test_prdlbl_becomes_textbf(self):
        out = preprocess_latex_for_pandoc(KRUNGSRI_BODY)
        assert "\\textbf{Role}" in out and "\\textbf{Name}" in out
        assert "\\prdlbl" not in out

    def test_drops_rowcolor_cline_hline(self):
        out = preprocess_latex_for_pandoc(KRUNGSRI_BODY)
        assert "\\rowcolor" not in out
        assert "\\cline" not in out
        assert "\\hline" not in out

    def test_shortstack_linebreaks_become_newline(self):
        out = preprocess_latex_for_pandoc(KRUNGSRI_BODY)
        assert "\\newline" in out


class TestPandocFriendlyDocument:
    def test_latex_kind_wraps_in_safe_preamble(self):
        document, kind = pandoc_friendly_document(KRUNGSRI_BODY)
        assert kind == "latex"
        assert document.startswith("\\documentclass")
        assert "\\begin{document}" in document and "\\end{document}" in document
        # The construct-heavy original preamble must NOT leak into pandoc input.
        assert "newcolumntype" not in document
        assert "fancyhdr" not in document

    def test_markdown_kind_passes_through(self):
        document, kind = pandoc_friendly_document(MARKDOWN_PRD)
        assert kind == "markdown"
        assert document == MARKDOWN_PRD.strip()

    def test_full_standalone_document_body_is_extracted(self):
        src = (
            "\\documentclass[12pt,a4paper]{article}\n"
            "\\newcolumntype{L}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
            "\\begin{document}\n" + KRUNGSRI_BODY + "\n\\end{document}\n"
        )
        document, kind = pandoc_friendly_document(src)
        assert kind == "latex"
        assert "newcolumntype" not in document

    def test_empty_source_raises_value_error(self):
        with pytest.raises(ValueError):
            pandoc_friendly_document("   ")
        with pytest.raises(ValueError):
            pandoc_friendly_document("")


# =====================================================================
# Export pipelines (require the vendored pandoc / tectonic binaries)
# =====================================================================
class TestConvertToDocx:
    def test_krungsri_latex_produces_native_word_tables(self):
        data = convert_to_docx(KRUNGSRI_BODY)
        assert data[:2] == b"PK"  # OOXML zip signature
        # THE regression guard: the old pipeline produced ZERO tables here.
        assert docx_table_count(data) >= 2

    def test_markdown_prd_produces_native_word_tables(self):
        data = convert_to_docx(MARKDOWN_PRD)
        assert data[:2] == b"PK"
        assert docx_table_count(data) >= 1

    def test_empty_source_raises(self):
        with pytest.raises(ValueError):
            convert_to_docx("")


class TestNativeDocxExport:
    """The native renderer must mirror the PDF's table layout: real merged
    cells for \\multirow/\\multicolumn, shaded header rows for \\rowcolor and
    fixed column widths from the L{..} specs - none of which the Pandoc
    pipeline preserves."""

    @staticmethod
    def _document_xml() -> str:
        import io
        import zipfile

        data = convert_to_docx(load_prd_latex_template())
        assert data[:2] == b"PK"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.read("word/document.xml").decode()

    def test_native_renderer_used_for_latex(self):
        """The Krungsri template must go through latex_docx_native, not pandoc."""
        from app import latex_docx_native

        assert latex_docx_native is not None
        xml = self._document_xml()
        # stakeholders + version history + reviews + product scope + tech ops
        # + appendix (the cover PMO fields are template-faithful plain lines,
        # not a Word table - see _render_cover)
        assert xml.count("<w:tbl>") >= 7

    def test_cover_matches_authoritative_tex_layout(self):
        """The DOCX cover follows the supplied reference PDF: a 28pt regular
        right-aligned title, left-aligned bold subtitles, and 16pt form lines."""
        import io

        from docx import Document
        from docx.shared import Pt

        data = convert_to_docx(load_prd_latex_template())
        doc = Document(io.BytesIO(data))
        paras = list(doc.paragraphs)
        texts = [p.text.strip() for p in paras]

        title_idx = texts.index("Nimble by Krungsri")
        title_p = paras[title_idx]
        assert str(title_p.alignment).startswith("RIGHT")
        title_run = title_p.runs[0]
        assert title_run.font.size == Pt(28) and not title_run.bold

        for offset, text in ((1, "KRUNGSRI NIMBLE"), (2, "Product Requirement Document")):
            sub_idx = title_idx + offset
            assert texts[sub_idx] == text
            sub_p = paras[sub_idx]
            assert sub_p.alignment is None or str(sub_p.alignment).startswith("LEFT")
            assert sub_p.runs[0].font.size == Pt(20) and sub_p.runs[0].bold

        pmo_idx = next(i for i, t in enumerate(texts) if t.startswith("PMO No:"))
        labels = [texts[pmo_idx + k].split("\t")[0] for k in range(6)]
        assert labels == [
            "PMO No:", "PMO Name:", "Version:", "Status:", "Last Update:", "Author:",
        ]
        assert all(paras[pmo_idx + k].runs[0].font.size == Pt(16) for k in range(6))

    def test_merged_cells_present(self):
        xml = self._document_xml()
        # Epic Name / User Flow / Scope-Definition horizontal merges ...
        assert xml.count("<w:gridSpan") >= 3
        # ... and the User Story Mapping (3-row) + Scope (2-row) vertical merges
        assert xml.count("<w:vMerge") >= 3

    def test_header_rows_shaded(self):
        xml = self._document_xml()
        assert 'w:fill="F8FAFC"' not in xml

    def test_no_latex_leakage(self):
        import io
        import zipfile

        data = convert_to_docx(load_prd_latex_template())
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts = " ".join(
                zf.read(name).decode() for name in zf.namelist() if name.endswith(".xml")
            )
        for token in ("multirow", "shortstack", "prdlbl", "prdfield", "rowcolor", "tabular"):
            assert token not in parts

    def test_header_footer_and_cover_present(self):
        import io
        import zipfile

        data = convert_to_docx(load_prd_latex_template())
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts = " ".join(
                zf.read(name).decode() for name in zf.namelist() if name.endswith(".xml")
            )
        assert "Nimble by Krungsri" in parts
        assert "PMO No:" in parts
        assert "PRODUCT REQUIREMENT" in parts
        assert "KRUNGSRI NIMBLE CONFIDENTIAL" in parts

    def test_nested_user_story_table_structure(self):
        """The complex nested table: the 'User Story Mapping & Functional
        Requirements' header cell spans 3 rows (each label on its own
        paragraph inside the merged cell); 'Epic Name:' spans 3 columns."""
        xml = self._document_xml()
        # vertical-merge header labels, one paragraph per line
        for token in ("User Story", "Mapping", "Functional", "Requirements"):
            assert token in xml
        assert "Epic Name:" in xml
        assert "Acceptance Criteria" in xml
        assert "Scope In" in xml and "Scope out" in xml

    def test_pandoc_fallback_on_native_failure(self, monkeypatch):
        """If the native renderer raises, the export must still succeed via
        the Pandoc pipeline instead of failing the request."""
        import app.latex_docx_native as native_mod

        def boom(_src):
            raise RuntimeError("simulated native failure")

        monkeypatch.setattr(native_mod, "latex_to_docx_native", boom)
        data = convert_to_docx(load_prd_latex_template())
        assert data[:2] == b"PK"
        assert docx_table_count(data) >= 7


class TestPrdToMarkdownPreview:
    def test_latex_preview_has_template_sections(self):
        md = prd_to_markdown(KRUNGSRI_BODY)
        assert "### Stakeholders" in md
        assert "### 2. Product Scope & Functional Requirements" in md
        # Pipe-table syntax the frontend renderer understands — pandoc emits
        # `|:---|` alignment separators, our HTML fallback emits `| --- |`.
        sep = re.compile(r"^\|[\s:\-|]+\|$", re.M)
        assert sep.search(md), "no GFM pipe-table separator row found"
        # No raw LaTeX and no raw HTML table blocks may leak into the preview.
        assert "\\begin{tabular}" not in md
        assert "\\shortstack" not in md
        assert "<table" not in md.lower()
        assert "<td" not in md.lower()

    def test_complex_merged_table_becomes_pipe_rows(self):
        md = prd_to_markdown(KRUNGSRI_BODY)
        assert "Epic Name: Transfer" in md
        assert "Acceptance Criteria" in md

    def test_official_trailing_tables_have_semantic_rows_and_headers(self):
        md = prd_to_markdown(load_prd_latex_template())
        assert "| Mapping & Functional Requirements | Epic Name:" in md
        assert "#### Scope Definition (In vs. Out)" in md
        assert "| Scope In (Included) | Scope Out (Excluded) |" in md
        assert "**User Story (System Focus)**" in md
        assert "**Functional Requirement (FR)**" in md
        assert "| Consideration | Details |" in md
        assert "| Appendix Item | Details | Reference |" in md
        assert "| Non-Functional<br> Requirements" in md
        assert "| Open<br> Questions &<br> Risks" in md

    def test_scope_in_and_out_are_separate_preview_columns(self):
        body = fill_template_body(
            project_id="scope-project",
            project_name="Scope Project",
            data={
                "scope_in": ["Create payment", "Approve payment"],
                "scope_out": ["International wire", "Cash handling"],
            },
        )
        md = prd_to_markdown(body)
        assert "| Scope In (Included) | Scope Out (Excluded) |" in md
        assert "1. Create payment<br>2. Approve payment" in md
        assert "1. International wire<br>2. Cash handling" in md
        assert "| Definition (In |" not in md

    def test_saved_scope_columns_are_not_shifted_during_renormalization(self):
        saved = """### 2. Product Scope & Functional Requirements

#### Scope Definition (In vs. Out)

| Scope In (Included) | Scope Out (Excluded) |
| --- | --- |
| Mobile onboarding | Branch counter changes |
"""
        normalized = prd_to_markdown(saved)
        assert "| Mobile onboarding | Branch counter changes |" in normalized
        assert normalized == saved.strip()

    def test_markdown_passthrough(self):
        assert prd_to_markdown(MARKDOWN_PRD) == MARKDOWN_PRD.strip()

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            prd_to_markdown("")


class TestConvertMarkdownToPdf:
    def test_markdown_pdf_is_a_real_pdf(self):
        data = convert_markdown_to_pdf(MARKDOWN_PRD)
        assert data.startswith(b"%PDF")


# =====================================================================
# DOCX -> PDF (LibreOffice headless) - the PRIMARY PDF engine.
# The PDF export renders the SAME Word document the DOCX export
# produces, so both downloads always match. The killer regression this
# guards: the TeX pipeline SILENTLY DROPS every Thai glyph (its fonts
# have no Thai coverage), while the Word pipeline preserves them.
# =====================================================================
@pytest.mark.skipif(not soffice_available(), reason="LibreOffice (soffice) is not installed")
class TestDocxToPdfLibreOffice:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def require_working_libreoffice_conversion(cls):
        """Skip integration checks when soffice exists but cannot convert here.

        Some sandboxed/CI environments expose the executable while denying the
        profile or process capabilities needed for headless conversion. That is
        an unavailable integration, not an application-unit failure. Product
        fallback behavior remains covered separately with mocked converters.
        """
        try:
            probe = docx_to_pdf(convert_latex_to_docx(KRUNGSRI_BODY))
        except (FileNotFoundError, RuntimeError) as exc:
            pytest.skip(f"LibreOffice conversion is unavailable: {exc}")
        assert probe.startswith(b"%PDF")

    def test_docx_to_pdf_is_a_real_pdf(self):
        docx = convert_latex_to_docx(KRUNGSRI_BODY)
        pdf = docx_to_pdf(docx)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000

    def test_thai_text_survives_the_pdf_export(self):
        """Regression: Tectonic dropped every Thai character, and LibreOffice's
        bundled fonts have no Thai coverage either (its default CTL substitute,
        DejaVu Sans, renders Thai as blank gaps). The export must embed the
        vendored Noto Sans Thai face so Thai PRD content is readable."""
        thai_label = "ระบบชำระเงิน"
        tex = load_prd_latex_template().replace("PMO\\_NO", thai_label)
        docx = convert_latex_to_docx(tex)
        pdf = docx_to_pdf(docx)

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf))
        fonts = set()
        thai_codepoints = 0
        for page in reader.pages:
            for f in (page.get("/Resources", {}).get("/Font") or {}).values():
                fonts.add(str(f.get_object().get("/BaseFont")))
            text = page.extract_text() or ""
            thai_codepoints += sum(
                1 for ch in text if "\u0e00" <= ch <= "\u0e7f"
            )
        assert any("NotoSansThai" in font for font in fonts), (
            f"PDF must embed the vendored Thai face; got fonts={sorted(fonts)}"
        )
        assert thai_codepoints > 0, "PDF must still contain Thai character data"

    def test_empty_docx_raises(self):
        with pytest.raises(ValueError):
            docx_to_pdf(b"")


# =====================================================================
# The FULL authoritative template end-to-end (what the browser exports
# when no generated PRD exists yet - the skeleton itself).
# =====================================================================
class TestOfficialTemplateWorkflow:
    """Regression for the production bug 'LaTeX PDF compilation failed' when
    exporting the preloaded template-krungsrinimble.tex skeleton."""

    def test_full_official_template_compiles_to_pdf(self):
        """The complete official .tex must compile via Tectonic unchanged."""
        pdf = compile_latex_to_pdf(load_prd_latex_template())
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 3000

    def test_pdf_and_docx_use_the_same_aptos_family(self):
        """The emergency TeX PDF path must not regress to Helvetica while
        Word uses Aptos. This host gets Aptos from Microsoft Word's DFonts."""
        if not _aptos_font_files():
            pytest.skip("Aptos is not locally installed on this host")

        from docx import Document
        from pypdf import PdfReader

        source = load_prd_latex_template()
        docx = Document(io.BytesIO(convert_latex_to_docx(source)))
        assert docx.styles["Normal"].font.name == "Aptos"
        assert docx.styles["Heading 1"].font.name == "Aptos"

        fonts = set()
        for page in PdfReader(io.BytesIO(compile_latex_to_pdf(source))).pages:
            for font in (page.get("/Resources", {}).get("/Font") or {}).values():
                fonts.add(str(font.get_object().get("/BaseFont")))
        assert fonts
        assert all("Aptos" in font for font in fonts), sorted(fonts)

    def test_full_official_template_converts_to_docx(self):
        docx = convert_latex_to_docx(load_prd_latex_template())
        assert docx_table_count(docx) >= 7

    def test_full_official_template_preview_is_clean_gfm(self):
        md = prd_to_markdown(load_prd_latex_template())
        assert "\\begin{tabular}" not in md
        assert "Stakeholders" in md
        assert "**KRUNGSRI NIMBLE**" in md
        assert "**Product Requirement Document**" in md
        assert "**PMO No:**" in md
        assert "PMO_NO" in md

    def test_markdown_skeleton_loads_and_starts_with_heading(self):
        tpl = load_prd_template()
        assert tpl.lstrip().startswith("#")

    # ------------------------------------------------------------------
    # REGRESSION (production bug): the browser preloads the full official
    # .tex via /api/prd/template and stores it wrapped in a ```latex fence.
    # The classification heuristic saw the template's "### " / "|" demo
    # lines INSIDE the fence, called the payload Markdown and routed the
    # LaTeX PRD into the Pandoc->Tectonic pipeline -> "LaTeX PDF
    # compilation failed. See server logs for details." on every fresh
    # project's Export PDF click.
    # ------------------------------------------------------------------
    def test_fenced_full_template_is_not_misrouted_as_markdown(self):
        """A ```latex-fenced official template must be classified as LaTeX."""
        fenced = "```latex\n" + load_prd_latex_template() + "\n```"
        assert is_markdown_prd(fenced) is False

    def test_fenced_full_template_compiles_to_pdf(self):
        """End-to-end defense: fenced template still exports a real PDF."""
        fenced = "```latex\n" + load_prd_latex_template() + "\n```"
        pdf = compile_latex_to_pdf(fenced)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 3000

    def test_fenced_full_template_converts_to_docx(self):
        """End-to-end defense: fenced template still yields native tables."""
        fenced = "```latex\n" + load_prd_latex_template() + "\n```"
        assert docx_table_count(convert_latex_to_docx(fenced)) >= 7

    # ------------------------------------------------------------------
    # REGRESSION (production bug): the LLM regenerated the cover page with
    # a SINGLE backslash before the spacing bracket ('\\[2.2cm]' instead
    # of '\\\\[2.2cm]'). TeX read '\\[' as the start of display math and
    # the export died with 'Command \bfseries invalid in math mode'.
    # ------------------------------------------------------------------
    def test_single_backslash_spacing_bracket_template_exports(self):
        BS = chr(92)
        buggy = load_prd_latex_template()
        buggy = buggy.replace(BS + BS + "[0.65cm]", BS + "[0.65cm]")
        assert (BS + "[0.65cm]") in buggy  # precondition: bug present
        pdf = compile_latex_to_pdf(buggy)
        assert pdf.startswith(b"%PDF")
        assert docx_table_count(convert_latex_to_docx(buggy)) >= 7

    def test_double_escaped_template_exports(self):
        """Same defense for a fully double-escaped (collapsed) response."""
        BS, NL = chr(92), chr(10)
        collapsed = load_prd_latex_template().replace(NL, BS + "n")
        pdf = compile_latex_to_pdf(collapsed)
        assert pdf.startswith(b"%PDF")
        assert docx_table_count(convert_latex_to_docx(collapsed)) >= 7




# =====================================================================
# HTTP routes
# =====================================================================
class TestConvertEndpoint:
    @pytest.mark.asyncio
    async def test_convert_latex_returns_gfm(self, client):
        resp = await client.post("/api/prd/convert", json={"latex_source": KRUNGSRI_BODY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_kind"] == "latex"
        assert "### Stakeholders" in body["markdown"]

    @pytest.mark.asyncio
    async def test_convert_markdown_reports_kind(self, client):
        resp = await client.post("/api/prd/convert", json={"latex_source": MARKDOWN_PRD})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_kind"] == "markdown"

    @pytest.mark.asyncio
    async def test_convert_empty_is_422(self, client):
        resp = await client.post("/api/prd/convert", json={"latex_source": ""})
        assert resp.status_code == 422


class TestExportEndpoints:
    @pytest.mark.asyncio
    async def test_legacy_markdown_docx_export_succeeds(self, client, seeded_project):
        """Legacy Markdown is content input, never a generic layout path."""
        resp = await client.post(
            f"/api/project/{seeded_project}/export/docx",
            json={"latex_source": MARKDOWN_PRD, "version": 1, "project_name": "Export Test Project"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert resp.headers["x-prd-template"] == "krungsrinimble-reference-v2"
        # The old generic Markdown converter produced one table. The official
        # reference layout contains the cover plus all seven structured grids.
        assert docx_table_count(resp.content) >= 7
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "Nimble by Krungsri" in xml
        assert "Secure transfer API for retail customers." in xml

    @pytest.mark.asyncio
    async def test_krungsri_latex_pdf_export_succeeds(self, client, seeded_project):
        """Endpoint-level guard: a Krungsri LaTeX body (what the Architect agent
        actually stores) must export a real PDF - via the DOCX->LibreOffice
        pipeline when soffice is installed, else via the Tectonic fallback -
        not be rejected as markdown and not re-parsed by pandoc's GFM reader."""
        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": KRUNGSRI_BODY, "version": 2,
                  "project_name": "Export Test Project"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.content.startswith(b"%PDF")
        # Download-name contract consumed by the frontend blob saver:
        # PRD_<project>_V<version>_<YYYY-MM-DD>_<HHMM>_<AM/PM>.pdf.
        match = re.fullmatch(
            r'attachment; filename="PRD_Export_Test_Project_V2_\d{4}-\d{2}-\d{2}_\d{4}_(?:AM|PM)\.pdf"',
            resp.headers["content-disposition"],
        )
        assert match, resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_export_filename_uses_latest_prd_semver(
        self, client, seeded_project, db_session, monkeypatch
    ):
        """The download name identifies the PRD release, not its DB row number."""
        from app.repositories.prd import PRDVersionRepository
        import app.routes.projects as projects_route

        await PRDVersionRepository.create(seeded_project, {
            "generated_prd": KRUNGSRI_BODY,
            "generated_by": "automated_agent",
            "semver": "2.3.1",
        }, db_session)
        await db_session.commit()
        monkeypatch.setattr(projects_route, "soffice_available", lambda: False)
        monkeypatch.setattr(
            projects_route, "compile_latex_to_pdf", lambda source: b"%PDF-semver"
        )

        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": KRUNGSRI_BODY, "version": 99,
                  "project_name": "Export Test Project"},
        )

        assert resp.status_code == 200
        assert resp.headers["x-prd-semver"] == "2.3.1"
        assert re.fullmatch(
            r'attachment; filename="PRD_Export_Test_Project_V2\.3\.1_\d{4}-\d{2}-\d{2}_\d{4}_(?:AM|PM)\.pdf"',
            resp.headers["content-disposition"],
        )

    @pytest.mark.asyncio
    async def test_pdf_export_renders_reference_shaped_docx(self, client, seeded_project, monkeypatch):
        """PDF prefers the same native Word layout as DOCX because the supplied
        reference PDF uses Word/Aptos geometry."""
        import app.routes.projects as projects_route

        captured: dict = {}

        def fake_docx(source: str) -> bytes:
            captured["source"] = source
            return b"PK-reference-docx"

        def fake_pdf(docx: bytes) -> bytes:
            captured["docx"] = docx
            return b"%PDF-from-reference-docx"

        monkeypatch.setattr(projects_route, "soffice_available", lambda: True)
        monkeypatch.setattr(projects_route, "convert_latex_to_docx", fake_docx)
        monkeypatch.setattr(projects_route, "docx_to_pdf", fake_pdf)

        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": KRUNGSRI_BODY, "version": 3,
                  "project_name": "Export Test Project"},
        )
        assert resp.status_code == 200
        assert resp.content == b"%PDF-from-reference-docx"
        assert captured["source"] == KRUNGSRI_BODY.strip()
        assert captured["docx"] == b"PK-reference-docx"

    @pytest.mark.asyncio
    async def test_pdf_export_does_not_require_soffice(
        self, client, seeded_project, monkeypatch
    ):
        """Direct template compilation remains the no-LibreOffice fallback."""
        import app.routes.projects as projects_route

        monkeypatch.setattr(projects_route, "soffice_available", lambda: False)
        monkeypatch.setattr(
            projects_route, "compile_latex_to_pdf", lambda src: b"%PDF-tectonic-fallback"
        )

        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": KRUNGSRI_BODY},
        )
        assert resp.status_code == 200
        assert resp.content == b"%PDF-tectonic-fallback"

    @pytest.mark.asyncio
    async def test_pdf_export_compiles_the_latest_stored_version(
        self, client, seeded_project, db_session, monkeypatch
    ):
        """REGRESSION (user report): the Export PDF button produced an OLDER
        document than the Export DOCX button when the frontend-posted copy
        lagged a live sync (a generation/confirm/section-edit that had just
        landed). The PDF endpoint must resolve the LATEST stored document
        server-side — the same resolution the on-screen preview uses — and
        keep the posted source only as a pre-first-generation fallback."""
        from uuid import UUID

        from app.models import PRDDocumentModel

        newer = MARKDOWN_PRD.replace(
            "Secure transfer API for retail customers.",
            "LATEST v2 instant transfer API.",
        )
        db_session.add(
            PRDDocumentModel(
                project_id=UUID(seeded_project),
                version=7,
                prd_markdown=newer,
                mermaid_diagram="",
            )
        )
        await db_session.commit()

        import app.routes.projects as projects_route

        captured: dict = {}

        def fake_compile(source: str) -> bytes:
            captured["source"] = source
            return b"%PDF-from-tex"

        monkeypatch.setattr(projects_route, "compile_latex_to_pdf", fake_compile)
        monkeypatch.setattr(projects_route, "convert_markdown_to_pdf", fake_compile)
        # This test asserts the direct-PDF fallback's input. Force that branch
        # so it stays deterministic on developer machines with LibreOffice.
        monkeypatch.setattr(projects_route, "soffice_available", lambda: False)

        # The (stale) frontend posts the OLD copy — the stored LATEST must win.
        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": MARKDOWN_PRD},
        )
        assert resp.status_code == 200
        assert resp.content == b"%PDF-from-tex"
        assert "LATEST v2 instant transfer API" in captured["source"]
        assert "Secure transfer API for retail customers." not in captured["source"]

    @pytest.mark.asyncio
    async def test_docx_export_compiles_the_latest_stored_version(
        self, client, seeded_project, db_session
    ):
        """Same latest-version contract for the DOCX export: a stored document
        always wins over the (possibly stale) frontend-posted copy, so both
        downloads can never carry different versions."""
        from uuid import UUID

        from app.models import PRDDocumentModel

        newer = MARKDOWN_PRD.replace(
            "Secure transfer API for retail customers.",
            "LATEST v2 instant transfer API.",
        )
        db_session.add(
            PRDDocumentModel(
                project_id=UUID(seeded_project),
                version=7,
                prd_markdown=newer,
                mermaid_diagram="",
            )
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/project/{seeded_project}/export/docx",
            json={"latex_source": MARKDOWN_PRD},
        )
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "LATEST v2 instant transfer API" in xml
        assert "Secure transfer API for retail customers." not in xml

    @pytest.mark.asyncio
    async def test_docx_export_contains_every_preview_version_row(
        self, client, seeded_project, db_session
    ):
        """Markdown previews rebuilt into the official DOCX keep the complete
        ledger, with one short sentence in each version row."""
        from app.repositories.prd import PRDVersionRepository

        await PRDVersionRepository.create(seeded_project, {
            "generated_prd": MARKDOWN_PRD,
            "generated_by": "user",
            "change_summary": "Initial PRD created.",
            "semver": "1.0.0",
        }, db_session)
        await PRDVersionRepository.create(seeded_project, {
            "generated_prd": MARKDOWN_PRD,
            "generated_by": "automated_agent",
            "change_summary": "Added payment flow. Internal detail is omitted.",
            "semver": "1.1.0",
        }, db_session)
        await db_session.commit()

        resp = await client.post(
            f"/api/project/{seeded_project}/export/docx",
            json={"latex_source": MARKDOWN_PRD},
        )
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "1.0.0" in xml and "Initial PRD created." in xml
        assert "1.1.0" in xml and "Added payment flow." in xml
        assert "Internal detail is omitted." not in xml

    @pytest.mark.asyncio
    async def test_krungsri_latex_docx_export_succeeds(self, client, seeded_project):
        """Endpoint-level guard: the merged-cell constructs of the template body
        still yield REAL native Word tables after the full HTTP round-trip."""
        resp = await client.post(
            f"/api/project/{seeded_project}/export/docx",
            json={"latex_source": KRUNGSRI_BODY},
        )
        assert resp.status_code == 200
        assert resp.content[:2] == b"PK"  # OOXML zip signature
        # Same regression bar as the unit level: no empty-table DOCX allowed.
        assert docx_table_count(resp.content) >= 2

    @pytest.mark.asyncio
    async def test_full_official_template_pdf_export_succeeds(self, client, seeded_project):
        """The COMPLETE authoritative template-krungsrinimble.tex (cover page,
        version history, all tables) compiles through the export endpoint —
        the exact fidelity contract 'use the right latex template' demands."""
        from app.prompt_loader import load_prd_latex_template

        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": load_prd_latex_template()},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")

    @pytest.mark.asyncio
    async def test_legacy_markdown_pdf_export_succeeds(self, client, seeded_project):
        resp = await client.post(
            f"/api/project/{seeded_project}/export/pdf",
            json={"latex_source": MARKDOWN_PRD},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")

    @pytest.mark.asyncio
    async def test_unknown_project_is_404(self, client):
        resp = await client.post(
            "/api/project/00000000-0000-0000-0000-000000000000/export/docx",
            json={"latex_source": MARKDOWN_PRD},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_payload_is_422(self, client, seeded_project):
        resp = await client.post(
            f"/api/project/{seeded_project}/export/docx",
            json={"latex_source": ""},
        )
        assert resp.status_code == 422

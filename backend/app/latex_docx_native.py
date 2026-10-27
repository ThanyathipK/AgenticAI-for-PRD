"""Native LaTeX -> DOCX renderer for the Krungsri Nimble PRD.

The Pandoc pipeline flattens the template's nested tables (``\\multirow``,
``\\multicolumn`` and ``\\rowcolor`` shading are unparsable for it), so
exported Word documents lost the merged-cell layout that makes the PDF
readable. This module renders the SAME LaTeX subset directly into a Word
document via python-docx:

- ``\\multirow{n}...`` / ``\\multicolumn{n}...`` become real merged cells,
- ``\\rowcolor{tblHeader}`` becomes shaded header rows (fill F8FAFC),
- custom ``L{..}``/``R{..}`` column widths become fixed Word column widths,
- ``\\shortstack`` / ``\\parbox`` stacks become multi-paragraph cells,
- ``\\section*`` headings, ``\\newpage`` breaks, enumerate lists, A4 page
  geometry, typography, cover, and the PRODUCT REQUIREMENT / CONFIDENTIAL
  header-footer follow ``prompts/template-krungsrinimble.tex``.

Anything unexpected degrades to plain text; :func:`app.latex_service.convert_to_docx`
falls back to the Pandoc pipeline if this module raises unexpectedly.
"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.latex_service import (
    _extract_latex_body,
    _strip_code_fence,
    sanitize_generated_latex,
)

#: Header-row fill taken from the template's ``\\definecolor{tblHeader}``.
HEADER_FILL = "FFFFFF"
LATIN_FONT = "Aptos"

#: Complex-script (CTL) face declared on the Normal/Heading styles so Thai
#: text renders in the headless LibreOffice PDF conversion (and in Word).
#: This EXACT family is vendored under ``backend/.tools/fonts/`` and staged
#: into the LibreOffice profile by :func:`app.latex_service.docx_to_pdf` -
#: the macOS headless build cannot see system fonts (Tahoma/Thonburi are
#: invisible to it) and its bundled set has no Thai coverage. python-docx
#: falls back gracefully in Word when a declared face is missing.
CTL_FONT = "Noto Sans Thai"

#: Marker for grid slots consumed by a neighbouring merged cell.
_CONT = "\x00CONT"


# ---------------------------------------------------------------------------
# TeX text decoding
# ---------------------------------------------------------------------------
_TEX_TEXT_REPLACEMENTS = (
    ("\\ldots", "…"),
    ("\\&", "&"),
    ("\\_", "_"),
    ("\\%", "%"),
    ("\\$", "$"),
    ("\\#", "#"),
    ("~", " "),
)


def _tex_to_text(fragment: str) -> str:
    """Decode escaped TeX characters into plain text."""
    fragment = re.sub(
        r"\\(?:normal(?:font|size)|large|Large|LARGE|sffamily|mdseries|bfseries|selectfont|noindent|hfill)\b",
        "",
        fragment,
    )
    fragment = re.sub(r"\\(?:hspace|vspace)\*?\{[^{}]*\}", "", fragment)
    fragment = re.sub(r"\\(?:rule|fontfamily|fontsize)\{[^{}]*\}(?:\{[^{}]*\})?", "", fragment)
    for old, new in _TEX_TEXT_REPLACEMENTS:
        fragment = fragment.replace(old, new)
    return fragment.replace("{", "").replace("}", "").strip()


# ---------------------------------------------------------------------------
# Brace-aware tokenization
# ---------------------------------------------------------------------------
def _split_top(text: str, on: str) -> List[str]:
    """Split *text* on a top-level separator.

    ``on='&'``  -> split on unescaped ampersands at brace depth 0.
    ``on='\\\\'`` -> split on row terminators ``\\\\`` (an optional
    ``[..spacing..]`` after the terminator is consumed).
    Backslash escape pairs (``\\&``, ``\\_`` ...) and anything inside
    ``{...}`` groups (``\\shortstack{a\\\\b}``) are never split.
    """
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            if (
                on == "\\\\"
                and depth == 0
                and i + 1 < n
                and text[i + 1] == "\\"
            ):
                parts.append("".join(buf))
                buf = []
                i += 2
                if i < n and text[i] == "[":
                    while i < n and text[i] != "]":
                        i += 1
                    i += 1
                continue
            if i + 1 < n:
                buf.append(text[i : i + 2])
                i += 2
            else:
                buf.append(ch)
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "&" and on == "&" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


#: How many brace arguments each recognised command consumes.
_COMMAND_ARGC: Dict[str, int] = {
    "multirow": 3,
    "multicolumn": 3,
    "shortstack": 1,
    "parbox": 2,
    "prdlbl": 1,
    "prdfield": 1,
    "fontsize": 2,
    "textbf": 1,
    "rowcolor": 1,
    "coverfont": 1,
}


def _read_command(text: str, start: int) -> Optional[Tuple[str, List[str], int]]:
    """Read ``\\name{arg1}{arg2}...`` at *start*.

    Returns ``(name, args, end_offset)`` or ``None``. The argument count is
    capped by ``_COMMAND_ARGC`` so trailing groups that belong to the
    surrounding markup are not swallowed.
    """
    if start >= len(text) or text[start] != "\\":
        return None
    m = re.match(r"\\([a-zA-Z]+)\*?", text[start:])
    if not m:
        return None
    name = m.group(1)
    argc = _COMMAND_ARGC.get(name)
    if argc is None:
        return None
    i = start + m.end()
    # Commands such as ``\shortstack[l]{...}`` carry an optional alignment
    # argument before their braced payload. It affects layout only; the Word
    # renderer already uses left-aligned cell paragraphs by default.
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text) and text[i] == "[":
        closer = text.find("]", i + 1)
        if closer != -1:
            i = closer + 1
    args: List[str] = []
    while len(args) < argc and i < len(text):
        j = i
        while j < len(text) and text[j] in " \t\r\n%":
            j += 1
        if j < len(text) and text[j] == "{":
            depth = 0
            k = j + 1
            while k < len(text):
                if text[k] == "\\":
                    k += 2
                    continue
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            args.append(text[j + 1 : k])
            i = k + 1
            continue
        break
    return name, args, i


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------
_RUN = Tuple[str, bool]  # one (text, bold) run


def _unwrap_stacks(src: str) -> str:
    """Unwrap ``\\shortstack{..}``, ``\\parbox{..}{..}``, ``\\coverfont{..}``
    and ``\\prdfield{..}``
    so their inner content (and ``\\\\`` line breaks) becomes top-level."""
    while True:
        m = re.search(r"\\(shortstack|parbox|prdfield|coverfont)\b", src)
        if not m:
            return src
        parsed = _read_command(src, m.start())
        if not parsed:
            return src
        _name, args, end = parsed
        inner = args[-1] if args else ""
        src = src[: m.start()] + inner + src[end:]


def _line_runs(line: str) -> List[_RUN]:
    """Split one cell line into (text, bold) runs at ``\\prdlbl``/``\\textbf``."""
    runs: List[_RUN] = []
    pos = 0
    for m in re.finditer(r"\\(?:prdlbl|textbf)\s*\{([^{}]*)\}", line):
        before = _tex_to_text(line[pos : m.start()])
        if before:
            runs.append((before, False))
        label = _tex_to_text(m.group(1))
        if label:
            runs.append((label, True))
        pos = m.end()
    tail = _tex_to_text(line[pos:])
    if tail:
        runs.append((tail, False))
    return runs


def _parse_cell(src: str) -> Dict:
    """Parse one tabular cell into spans, shading and paragraph runs."""
    meta: Dict = {"vspan": 1, "hspan": 1, "shading": None, "paras": []}
    src = re.sub(r"(?m)^\s*%.*$", "", src).strip()
    if src.startswith("\\rowcolor"):
        parsed = _read_command(src, 0)
        if parsed:
            _name, args, end = parsed
            fill = (args[0] if args else "").strip()
            meta["shading"] = fill if re.fullmatch(r"[0-9A-Fa-f]{6}", fill) else HEADER_FILL
            src = src[end:].strip()
    while True:
        m = re.match(r"\\(multirow|multicolumn)\b", src)
        if not m:
            break
        parsed = _read_command(src, 0)
        if not parsed:
            break
        name, args, end = parsed
        count = re.sub(r"[^0-9]", "", args[0] if args else "") or "1"
        if name == "multirow":
            meta["vspan"] = max(1, int(count))
        else:
            meta["hspan"] = max(1, int(count))
        src = (args[-1] if args else "").strip()
    src = _unwrap_stacks(src).replace("\\newline", "\\\\")
    for line in _split_top(src, "\\\\"):
        line = line.strip()
        if not line:
            continue
        runs = _line_runs(line)
        if runs:
            meta["paras"].append(runs)
    return meta


def _parse_colspec(spec: str) -> Tuple[List[float], bool]:
    """``{|L{5.5cm}|L{8.5cm}|}`` -> ([5.5, 8.5], has_vertical_lines)."""
    has_vlines = "|" in spec
    widths = [float(m.group(1)) for m in re.finditer(r"[LR]\{([0-9.]+)(?:cm)?\}", spec)]
    if not widths:
        widths = [3.0] * max(1, len(re.findall(r"[lrc]", spec)))
    return widths, has_vlines


# ---------------------------------------------------------------------------
# Table rendering (real merged cells)
# ---------------------------------------------------------------------------
def _split_tabular_rows(body: str, ncols: int) -> List[str]:
    """Split a ``tabular`` BODY into ROW segments, not cells.

    The Krungsri template uses a bare ``\\`` INSIDE a cell as a *line break*
    (``Introduction \\\\ Executive Summary``, ``1. \\\\[0.15cm] 2.``) just as
    often as it uses ``\\`` to END a row, so naively splitting on every
    top-level ``\\`` tears each multi-line cell into its own table row.  A
    top-level ``\\`` is only a row terminator when, after optional
    ``[spacing]`` and whitespace, ONE of the following holds:

    - the next content starts a NEW row - ``&`` (filler cell) or a backslash
      command (cover table rows are followed directly by ``\\prdlbl{...}``
      with no ``\\hline`` between them);
    - the row is already complete (``ncols`` cells seen) and the plain text
      that follows runs on to another cell separator - the stakeholders and
      reviews tables repeat data rows WITHOUT ``\\hline`` between them, and
      this is what tells a row terminator from a mid-cell line break;
    - a structural marker follows: ``\\hline``, ``\\cline{n-m}``,
      ``\\noalign``, ``\\rowcolor``, ``\\end{tabular}`` or end of input.

    Any other top-level ``\\`` is a line break inside the current cell and is
    kept as a literal ``\\\\`` marker that :func:`_parse_cell` later turns
    into separate paragraphs.  ``\\`` inside ``{...}`` groups (``\\shortstack``
    / ``\\parbox`` / ``\\multirow`` content) is preserved untouched - those
    groups are unwrapped and split only after the row/cell split has happened.
    """
    body = re.sub(r"(?<!\\)%.*$", "", body, flags=re.M)  # comments-to-EOL
    rows: List[str] = []
    buf: List[str] = []
    depth = 0
    seps = 0  # top-level cell separators (``&``) seen in the current row
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt == "\\":
                if depth == 0:
                    j = i + 2
                    if j < n and body[j] == "[":
                        k = body.find("]", j)
                        j = n if k == -1 else k + 1
                    k = j
                    while k < n and body[k] in " \t\r\n":
                        k += 1
                    if k == n:
                        rows.append("".join(buf))
                        buf = []
                        i = k
                        continue
                    tail = body[k:]
                    if (
                        tail.startswith("\\hline")
                        or tail.startswith("\\cline")
                        or tail.startswith("\\noalign")
                        or tail.startswith("\\rowcolor")
                    ):
                        rows.append("".join(buf))
                        buf = []
                        seps = 0
                        i = k
                        continue
                    if tail.startswith("\\end"):
                        rows.append("".join(buf))
                        return rows
                    is_next_row = body[k] == "&" or body[k] == "\\"
                    if (
                        not is_next_row
                        and seps == ncols - 1
                        and _segment_has_cell_separator(body, k)
                    ):
                        is_next_row = True
                    if is_next_row:
                        rows.append("".join(buf))
                        buf = []
                        seps = 0
                        i = k
                        continue
                    buf.append("\\\\")  # line break inside the current cell
                    i = j
                    continue
                buf.append("\\\\")
                i += 2
                continue
            if nxt == "{":
                buf.append("\\{")
                depth += 1
                i += 2
                continue
            if nxt == "}":
                buf.append("\\}")
                depth = max(0, depth - 1)
                i += 2
                continue
            if nxt in "&%$#_~^":
                buf.append(body[i : i + 2])
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "&" and depth == 0:
            seps += 1
        buf.append(ch)
        i += 1
    if buf:
        rows.append("".join(buf))
    return rows


def _segment_has_cell_separator(body: str, k: int) -> bool:
    """Does the text from *k* to the next row break contain a top-level ``&``?

    Recognises a ``\\`` that closes a COMPLETE row whose successor opens with
    plain text (stakeholders/reviews repeat data rows with no ``\\hline``):
    the successor carries its own cell separators on the way to its own row
    break.  A match means the ``\\`` at *k* was not a mid-cell line break.
    """
    depth = 0
    i = k
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            nx = body[i + 1] if i + 1 < n else ""
            if nx == "\\":
                return False  # next row break: no ``&`` in between
            if nx == "{":
                depth += 1
                i += 2
                continue
            if nx == "}":
                depth = max(0, depth - 1)
                i += 2
                continue
            if nx in "&%$#_~^":
                i += 2
                continue
            if (
                body.startswith("\\hline", i)
                or body.startswith("\\cline", i)
                or body.startswith("\\end", i)
            ):
                return False
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == "&" and depth == 0:
            return True
        i += 1
    return False


def _shade(cell, fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _fill_cell(cell, paras: List[List[_RUN]]) -> None:
    for i, runs in enumerate(paras):
        p = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        for text, bold in runs:
            run = p.add_run(text)
            run.bold = bold


def _render_table(doc: "Document", spec: str, rows_src: str, centered: bool) -> None:
    widths, has_vlines = _parse_colspec(spec)
    rows: List[List[Dict]] = []
    for raw in _split_tabular_rows(rows_src, len(widths)):
        raw = re.sub(r"\\(?:hline|cline\{\d+-\d+\})", "", raw)
        raw = re.sub(r"\\noalign\{\\vskip\s+[^{}]+\}", "", raw)
        if not raw.strip():
            continue
        rows.append([_parse_cell(c) for c in _split_top(raw, "&")])
    if not rows or not widths:
        return

    ncols = len(widths)
    nrows = len(rows)
    grid: List[List[Optional[Dict] | str]] = [[None] * ncols for _ in range(nrows)]
    merges: List[Tuple[int, int, int, int]] = []
    header_rows = set()

    for r, cells in enumerate(rows):
        c = 0
        for cell in cells:
            if c >= ncols:
                break
            # A filler cell ("&" with no content) for an active vertical
            # merge is consumed silently - the merge already owns the slot.
            if grid[r][c] is _CONT and not cell["paras"]:
                c += 1
                continue
            while c < ncols and grid[r][c] is _CONT:
                c += 1
            if c >= ncols:
                break
            vspan = max(1, min(cell["vspan"], nrows - r))
            hspan = max(1, min(cell["hspan"], ncols - c))
            grid[r][c] = cell
            if vspan > 1 or hspan > 1:
                merges.append((r, c, r + vspan - 1, c + hspan - 1))
                for rr in range(r, r + vspan):
                    for cc in range(c, c + hspan):
                        if (rr, cc) != (r, c):
                            grid[rr][cc] = _CONT
            if cell["shading"]:
                header_rows.add(r)
            c += hspan

    table = doc.add_table(rows=nrows, cols=ncols)
    table.autofit = False
    if has_vlines:
        table.style = "Table Grid"
    if centered:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    table._tbl.tblPr.append(layout)
    for i, w in enumerate(widths):
        table.columns[i].width = Cm(w)

    for (r0, c0, r1, c1) in merges:
        try:
            table.cell(r0, c0).merge(table.cell(r1, c1))
        except Exception:
            pass  # a broken merge must never fail the whole export

    for r in range(nrows):
        for c in range(ncols):
            rendered_cell = grid[r][c]
            if not isinstance(rendered_cell, dict):
                continue
            docx_cell = table.cell(r, c)
            docx_cell.width = Cm(sum(widths[c : c + rendered_cell["hspan"]]))
            if rendered_cell["vspan"] > 1:
                docx_cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            _fill_cell(docx_cell, rendered_cell["paras"])

    for r in header_rows:
        for c in range(ncols):
            try:
                _shade(table.cell(r, c), HEADER_FILL)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------
_BODY_TOKEN_RE = re.compile(
    r"\\begin\{(?:longtable|tabular)\}\s*\{(?P<spec>(?:[^{}]|\{[^{}]*\})*)\}(?P<tbl>.*?)\\end\{(?:longtable|tabular)\}"
    r"|(?P<contents>\\section\*\{\\makebox(?:\[[^\]]*\])*\{Contents\}\})"
    r"|\\section\*\{(?P<sec>[^{}]*)\}"
    r"|\\begin\{enumerate\}(?P<enum>.*?)\\end\{enumerate\}"
    r"|\\begin\{flushright\}(?P<flush>.*?)\\end\{flushright\}"
    r"|\\begin\{center\}(?P<center>.*?)\\end\{center\}"
    r"|(?P<page>\\newpage\b)"
    r"|(?P<space>\\vspace\*?\{[^{}]*\})"
    r"|(?P<skip>\\thispagestyle\{[^{}]*\})",
    re.S,
)


_VSPACE_RE = re.compile(r"\\vspace\*?\{(?P<value>[0-9.]+)\s*(?P<unit>cm|pt|em|ex)?\}")

#: TeX length units -> centimetres (em/ex approximated for the 12pt body font).
_UNIT_TO_CM = {"cm": 1.0, "pt": 2.54 / 72.0, "em": 0.42, "ex": 0.21}


def _spacer(doc: "Document", amount_cm: float = 0.0) -> None:
    """Empty paragraph standing in for ``\\vspace``.

    When the LaTeX source specifies a length (the cover's ``\\vspace*{6cm}``)
    the spacer grows to that height so the title block sits at the same
    mid-page position as in the compiled PDF - the collapsed 6pt spacer used
    before squeezed every cover title against the top of the Word page.
    """
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = Pt(2)  # keep the empty line itself negligible
    p.paragraph_format.space_after = Cm(min(max(amount_cm, 0.0), 24.0))


def _add_heading(doc: "Document", title: str) -> None:
    if title:
        if "Contents" in title:
            title = "Contents"
        title = _tex_to_text(title)
        p = doc.add_heading(title, level=1)
        if title == "Contents":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif title.startswith("1."):
            p.paragraph_format.left_indent = Cm(1.27)
        elif re.match(r"^[234]\.\s", title):
            p.paragraph_format.left_indent = Cm(0.64)


def _render_enum(doc: "Document", enum_src: str) -> None:
    for item in re.split(r"\\item\b", enum_src)[1:]:
        text = _tex_to_text(item)
        if text:
            doc.add_paragraph(text, style="List Number")


def _render_cover_titles(doc: "Document", flush_src: str) -> None:
    """Render the reference PDF's 28pt regular right-aligned cover title."""
    segments = re.split(r"\\\\(?:\[([0-9.]+)(?:cm|pt|em|ex)?\])?", flush_src)
    title_index = 0
    for part in segments:
        if part is None or re.fullmatch(r"[0-9.]+", part):
            continue  # pure spacing terminator - gaps come from the template
        seg = part.strip()
        if not seg:
            continue
        text = _tex_to_text(
            re.sub(r"\\(?:fontsize\{[\d.]+\}\{[\d.]+\}|selectfont|bfseries|Large|large)", "", seg)
        )
        if not text:
            continue
        if title_index == 0:
            size, bold, align, before_cm, after_cm = 28.0, False, WD_ALIGN_PARAGRAPH.RIGHT, 1.65, 2.15
        else:
            size, bold, align, before_cm, after_cm = 20.0, True, WD_ALIGN_PARAGRAPH.LEFT, 0.0, 0.4
        p = doc.add_paragraph()
        p.alignment = align
        p.paragraph_format.space_before = Cm(before_cm)
        p.paragraph_format.space_after = Cm(after_cm)
        run = p.add_run(text)
        run.bold = bold
        run.font.size = Pt(size)
        title_index += 1


def _render_cover(doc: "Document", cover_src: str) -> bool:
    """Render page 1 from the supplied PDF's measured cover layout.

    Returns True when the cover was rendered from the template structure;
    False makes the caller fall back to the generic LaTeX body renderer.
    """
    flush_m = re.search(r"\\begin\{flushright\}(.*?)\\end\{flushright\}", cover_src, re.S)
    tbl_m = re.search(
        r"\\begin\{tabular\}\{[^\n]*\}\s*(?P<tbl>.*?)\\end\{tabular\}",
        cover_src,
        re.S,
    )
    if not flush_m or not tbl_m:
        return False

    _render_cover_titles(doc, flush_m.group(1))
    for text, after_cm in (
        ("KRUNGSRI NIMBLE", 0.25),
        ("Product Requirement Document", 0.20),
    ):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Cm(after_cm)
        run = p.add_run(text)
        run.bold = True
        run.font.size = Pt(20)

    prev_p = None
    for raw in _split_top(tbl_m.group("tbl"), "\\\\"):
        raw = re.sub(r"\\(?:hline|cline\{\d+-\d+\})", "", raw).strip()
        raw = re.sub(r"\\noalign\{\\vskip\s+[^{}]+\}", "", raw).strip()
        if not raw:
            continue
        cells = _split_top(raw, "&")
        if len(cells) < 2:
            continue
        label = "".join(t for t, _b in _line_runs(_unwrap_stacks(cells[0]))).strip()
        value = "".join(t for t, _b in _line_runs(_unwrap_stacks(cells[1]))).strip()
        if not label:
            continue
        p = doc.add_paragraph()
        p.paragraph_format.tab_stops.add_tab_stop(Cm(4.5))
        p.paragraph_format.space_after = Cm(0.35)
        run = p.add_run(label)
        run.font.size = Pt(16)
        if value:
            value_run = p.add_run("\t" + value)
            value_run.font.size = Pt(16)
        if label.lower().startswith("version") and prev_p is not None:
            prev_p.paragraph_format.space_after = Cm(1.65)
        prev_p = p
    return True


def _render_text(doc: "Document", chunk: str, centered: bool) -> None:
    for line in chunk.split("\n"):
        line = line.strip()
        if not line or line.startswith("\\") or line.startswith("%"):
            continue  # unknown/stray commands degrade silently
        text = _tex_to_text(line)
        if not text:
            continue
        p = doc.add_paragraph()
        if centered:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(text)


def _render_body(doc: "Document", body: str, centered: bool = False) -> None:
    body = re.sub(r"(?m)^\s*%.*$", "", body)  # comment lines
    body = re.sub(r"\\(?:bgroup|egroup)\b", "", body)
    body = re.sub(r"\\renewcommand\{[^{}]*\}\{[^{}]*\}", "", body)
    body = re.sub(r"\\setcounter\{[^{}]*\}\{[^{}]*\}", "", body)
    pos = 0
    while pos < len(body):
        m = _BODY_TOKEN_RE.search(body, pos)
        if not m:
            _render_text(doc, body[pos:], centered)
            break
        if m.start() > pos:
            _render_text(doc, body[pos : m.start()], centered)
        if m.group("tbl") is not None:
            _render_table(doc, m.group("spec"), m.group("tbl"), centered)
        elif m.group("contents") is not None:
            _add_heading(doc, "Contents")
        elif m.group("sec") is not None:
            _add_heading(doc, _tex_to_text(m.group("sec")))
        elif m.group("enum") is not None:
            _render_enum(doc, m.group("enum"))
        elif m.group("flush") is not None:
            _render_cover_titles(doc, m.group("flush"))
        elif m.group("center") is not None:
            _render_body(doc, m.group("center"), centered=True)
        elif m.group("page") is not None:
            doc.add_page_break()
        elif m.group("space") is not None:
            vs = _VSPACE_RE.match(m.group("space"))
            amount = 0.0
            if vs:
                amount = float(vs.group("value")) * _UNIT_TO_CM.get(vs.group("unit") or "pt", 0.0)
            _spacer(doc, amount)
        pos = m.end()


def _add_page_field(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def _add_cs_font(style, latin: str, size_pt: float) -> None:
    """Declare the complex-script (CTL) face + size on a style.

    ``style.font.name`` only writes ``w:rFonts w:ascii``/``w:hAnsi`` - Thai,
    Arabic and other complex scripts are shaped with the ``w:cs`` face, which
    python-docx leaves UNSET. A bare profile of headless LibreOffice then
    substitutes its bundled DejaVu Sans for CTL runs - which has NO Thai
    glyphs - and the PDF shows blank gaps where Thai text should be. The
    ``CTL_FONT`` family is vendored and staged into the LibreOffice profile
    by :func:`app.latex_service.docx_to_pdf` (the macOS headless build cannot
    see system fonts). ``w:szCs`` mirrors ``font.size`` for CTL runs
    (complex-script text ignores ``w:sz``).
    """
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), latin)
    rFonts.set(qn("w:hAnsi"), latin)
    rFonts.set(qn("w:cs"), CTL_FONT)
    sz_cs = rPr.find(qn("w:szCs"))
    if sz_cs is None:
        sz_cs = OxmlElement("w:szCs")
        rPr.append(sz_cs)
    sz_cs.set(qn("w:val"), str(int(size_pt * 2)))


def _new_document() -> Document:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = LATIN_FONT
    normal.font.size = Pt(14)
    normal.paragraph_format.space_after = Pt(0)
    _add_cs_font(normal, LATIN_FONT, 14)
    heading = doc.styles["Heading 1"]
    heading.font.name = LATIN_FONT
    heading.font.size = Pt(16)
    heading.font.bold = False
    heading.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
    heading.paragraph_format.space_after = Cm(0.35)
    _add_cs_font(heading, LATIN_FONT, 16)

    section = doc.sections[0]
    section.page_width = Cm(21.59)
    section.page_height = Cm(27.94)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    # The official template shows the running header/footer on every page,
    # including the cover, so do not suppress first-page furniture.
    section.different_first_page_header_footer = False

    header_run = section.header.paragraphs[0].add_run("PRODUCT REQUIREMENT")
    header_run.font.name = LATIN_FONT
    header_run.font.size = Pt(12)

    # Footer mirrors the compiled PDF: page number bottom-LEFT, company
    # "KRUNGSRI NIMBLE CONFIDENTIAL" centered across the page.
    footer_p = section.footer.paragraphs[0]
    footer_p.paragraph_format.tab_stops.add_tab_stop(Cm(8.255), WD_TAB_ALIGNMENT.CENTER)
    _add_page_field(footer_p)
    footer_run = footer_p.add_run("\tKRUNGSRI NIMBLE CONFIDENTIAL")
    footer_run.font.name = LATIN_FONT
    footer_run.font.size = Pt(12)

    doc.core_properties.title = "Krungsri Nimble - Product Requirement Document"
    return doc


def latex_to_docx_native(latex_source: str) -> bytes:
    """Render the Krungsri PRD LaTeX into DOCX bytes - merged cells, shaded
    header rows and fixed column widths, mirroring the compiled PDF."""
    src = _strip_code_fence(latex_source or "")
    src = sanitize_generated_latex(src)
    if "\\begin{document}" in src:
        src = _extract_latex_body(src)
    body = src.strip()
    if not body:
        raise ValueError("Cannot export an empty PRD.")
    doc = _new_document()
    # The cover page (everything before the first \newpage) is rendered from
    # the explicit structure in template-krungsrinimble.tex; anything that does
    # not match that structure falls back to the generic renderer.
    cover_end = body.find("\\newpage")
    cover_rendered = False
    if cover_end != -1:
        cover_rendered = _render_cover(doc, body[:cover_end])
    if cover_rendered:
        _render_body(doc, body[cover_end:])
    else:
        _render_body(doc, body)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

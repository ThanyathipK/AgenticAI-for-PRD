"""
LaTeX compile & conversion service.

The generated PRD is now a LaTeX document (following the authoritative
template-krungsrinimble.tex template), so the PDF and DOCX exports must be
built by compiling/converting that LaTeX - NOT by parsing markdown.

Toolchain (self-contained, no TeX Live install required):
  - tectonic  -> LaTeX  -> PDF (fallback PDF engine)
  - pandoc    -> LaTeX  -> DOCX (fallback + Markdown -> DOCX)
  - soffice   -> DOCX   -> PDF (LibreOffice headless; PRIMARY PDF engine so the
                PDF is rendered from the SAME Word document the DOCX export
                produces - identical layout, and full Unicode/Thai support,
                which the LaTeX fonts silently drop)

Binary discovery order:
  1. $TECTONIC_BIN / $PANDOC_BIN environment variables
  2. The vendored `bin/` under `<repo>/.tools/` used during development
  3. Any `tectonic` / `pandoc` on the system PATH

Each compile runs in a fresh temporary working directory with a strict timeout,
so a malformed LLM-generated fragment does not wedge the server and never
touches the application source tree.
"""
import functools
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("app.latex_service")


class LaTeXCompileError(RuntimeError):
    """Raised when a TeX toolchain invocation fails.

    Carries the engine's error excerpt (and the diagnostics directory) in the
    message so API error details tell the caller exactly what was wrong with
    the generated LaTeX instead of a generic "see server logs" note.
    """


# Repository root = backend/app/../.. (i.e. <repo>/backend -> <repo>)
_LATEX_SERVICE_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _LATEX_SERVICE_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent

_COMPILE_TIMEOUT_S = 120

_DEFAULT_TECTONIC_CANDIDATES = (_REPO_ROOT / ".tools" / "tectonic",)
_DEFAULT_PANDOC_CANDIDATES = tuple(
    (_REPO_ROOT / ".tools" / "pandoc" / d / "bin" / "pandoc")
    for d in ("pandoc-3.10.2-arm64",)
)
_DEFAULT_SOFFICE_CANDIDATES = (
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/usr/local/bin/soffice"),
    Path("/usr/bin/soffice"),
    Path("/opt/libreoffice/program/soffice"),
)

# Thai-capable faces staged into every isolated LibreOffice profile before a
# DOCX->PDF conversion (<profile>/user/fonts). The macOS headless build sees
# ONLY the fonts inside its own bundle + profile - never the system's - and
# its bundled set has no Thai coverage, so without these the PDF drops every
# Thai glyph. Same vendored-binary pattern as .tools/tectonic and .tools/pandoc.
_VENDORED_FONTS: tuple = tuple(
    p
    for p in sorted((_BACKEND_DIR / ".tools" / "fonts").glob("*.tt[fc]"))
    if p.is_file()
)

# The required reference PDF uses Aptos. Microsoft Office on macOS keeps its
# fonts inside the Word application bundle rather than the normal system font
# directories, so neither headless LibreOffice nor Tectonic discovers them on
# its own. We do not redistribute these proprietary files; when Word is
# installed, stage its local regular/bold faces into each isolated conversion
# directory. DOCX files already declare Aptos and will use the same family when
# opened in Word even on hosts where these paths do not exist.
_APTOS_FONT_CANDIDATES = {
    "regular": (
        Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts/Aptos.ttf"),
        Path("/Library/Fonts/Aptos.ttf"),
        Path.home() / "Library" / "Fonts" / "Aptos.ttf",
    ),
    "bold": (
        Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts/Aptos-Bold.ttf"),
        Path("/Library/Fonts/Aptos-Bold.ttf"),
        Path.home() / "Library" / "Fonts" / "Aptos-Bold.ttf",
    ),
}


def _aptos_font_files() -> dict[str, Path]:
    """Return locally installed Aptos regular/bold files when both exist."""
    found = {
        weight: next((path for path in candidates if path.is_file()), None)
        for weight, candidates in _APTOS_FONT_CANDIDATES.items()
    }
    return found if all(found.values()) else {}


def _enable_aptos_for_tectonic(document: str) -> str:
    """Switch the portable Helvetica preamble to staged Aptos font files."""
    portable = "\\usepackage[T1]{fontenc}\n\\usepackage{helvet}"
    aptos = r"""\usepackage{fontspec}
\setmainfont[
  Path=./,
  UprightFont=Aptos.ttf,
  BoldFont=Aptos-Bold.ttf
]{Aptos}
\setsansfont[
  Path=./,
  UprightFont=Aptos.ttf,
  BoldFont=Aptos-Bold.ttf
]{Aptos}"""
    return document.replace(portable, aptos, 1)


def _find_binary(env_name: str, default_candidates: tuple, name: str) -> str:
    explicit = os.environ.get(env_name)
    if explicit and Path(explicit).exists():
        return explicit
    for cand in default_candidates:
        if cand.exists():
            return str(cand)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    raise FileNotFoundError(
        f"LaTeX export needs the '{name}' binary. Set {env_name} or install it "
        f"('{name}' was not found at any known candidate location)."
    )


def tectonic_bin() -> str:
    return _find_binary("TECTONIC_BIN", _DEFAULT_TECTONIC_CANDIDATES, "tectonic")


def pandoc_bin() -> str:
    return _find_binary("PANDOC_BIN", _DEFAULT_PANDOC_CANDIDATES, "pandoc")


# ---------------------------------------------------------------------------
# Source-kind detection
# ---------------------------------------------------------------------------
# LaTeX control sequences that UNAMBIGUOUSLY mark a document as LaTeX (as
# opposed to the legacy Markdown PRDs persisted before the LaTeX switch).
#
# NOTE: page-furniture commands like \newpage / \vspace / bare \section are
# deliberately NOT signals here. The Markdown PRD skeleton served to the UI
# (prompts/template.md, preloaded into the export payload when no PRD has been
# generated yet) legitimately uses \newpage as a page-break marker — treating
# it as LaTeX evidence misrouted that skeleton into the TeX engine and crashed
# every PDF export made before the first Generate-PRD run.
_STRONG_LATEX_SIGNALS = (
    "\\documentclass",
    "\\begin{",
    "\\end{",
    "\\thispagestyle",
    "\\usepackage",
    "\\newcolumntype",
    "\\prdlbl{",
    "\\prdfield{",
    "\\multirow",
    "\\multicolumn",
    "\\shortstack",
    "\\rowcolor",
    "\\hline",
    "\\cline",
)


def _strip_code_fence(source: str) -> str:
    """Remove a leading/trailing ``` fence an LLM may wrap output in."""

    src = (source or "").strip()
    if not src.startswith("```"):
        return src
    first_nl = src.find("\n")
    src = src[first_nl + 1:] if first_nl != -1 else ""
    if src.rstrip().endswith("```"):
        src = src.rstrip()[:-3]
    return src.strip()


def has_markdown_structure(source: str) -> bool:
    """True when the source shows Markdown structure: headings, pipe tables,
    bullet or numbered lists. LaTeX bodies have none of these line shapes."""

    src = _strip_code_fence(source)
    for line in src.split("\n"):
        t = line.strip()
        if not t:
            continue
        if t.startswith("#") or t.startswith("|") or t.startswith("- ") or t.startswith("* "):
            return True
        if re.match(r"^\d+\.\s", t):
            return True
    return False


# ---------------------------------------------------------------------------
# LLM-output sanitation
# ---------------------------------------------------------------------------
# Small local models (qwen3.5-9B et al.) routinely DOUBLE-ESCAPE the JSON
# string they return, so every intended newline arrives as a LITERAL
# backslash-n two-character sequence and the whole document collapses onto a
# single physical line. TeX then halts on the first one with
# "! Undefined control sequence. l.N \n" and every PDF export of that PRD
# fails with HTTP 502 (the DOCX path only "worked" because pandoc silently
# dropped the unknown macro, producing a badly-formatted document).
#
# The repair rule: a backslash followed by 'n'/'r'/'t' is an escaping
# ARTIFACT only when the next character is NOT a letter -- a real control
# sequence's name continues with letters. Therefore `\newpage`, `\newline`,
# `\noindent`, `\neq`, `\nu` and friends are never touched, and a row
# terminator followed by a word starting with 'n' (`\\nobody`) is preserved
# as well; bare `\n\vspace`, `\n  \hline` and trailing `\n` are repaired.
_LITERAL_NEWLINE_ARTIFACT_RE = re.compile(r"\\n([a-zA-Z]*)")
_LITERAL_CR_ARTIFACT_RE = re.compile(r"\\r(?![a-zA-Z])")
_LITERAL_TAB_ARTIFACT_RE = re.compile(r"\\t(?![a-zA-Z])")

# Real LaTeX control sequences that legitimately start with \\n
# Anything spelled \\n<letters> cannot be a command the PRD intends.
_KNOWN_N_LEADING_COMMANDS = frozenset({
    "newline", "newpage", "newcolumntype", "newcommand", "newenvironment", "newcounter",
    "newtheorem", "newif", "newcount", "newdimen", "newlength", "newbox",
    "newtok", "newfont", "newskip", "newread", "newwrite", "newfam",
    "newlanguage", "noindent", "nobreak", "nolinebreak", "nopagebreak", "nonumber",
    "noalign", "noexpand", "nolimits", "nonscript", "nointerlineskip", "nu",
    "neq", "nabla", "nearrow", "node", "normalfont", "normalsize", "null",
})


def _repair_newline_artifact(match):
    """Decide whether \\n<letters> is an escape artifact or a real token."""
    name = match.group(1)
    if ("n" + name) in _KNOWN_N_LEADING_COMMANDS:
        return match.group(0)
    # Count the consecutive backslashes immediately BEFORE this match so a
    # row terminator (\\) can be told apart from the artifact's own slash:
    # 'row break' + word ('\\nobody') has ONE preceding backslash, while the
    # double-escaped 'row break' + newline-artifact + word ('\\\nmust') has
    # TWO - and those two texts are otherwise indistinguishable.
    start = match.start()
    run = 0
    i = start - 1
    while i >= 0 and match.string[i] == "\\":
        run += 1
        i -= 1
    if not name:
        if run % 2 == 1:
            return match.group(0)        # row break + 'n' word ('\\n 2.')
        return "\n"                      # bare artifact -> real newline
    if run == 1:
        return match.group(0)            # row break + word ('\\nobody')
    if run >= 2:
        return "\n" + name               # row break + artifact + word
    return " " + name                    # in-cell 'system\\nis' -> 'system is'



def _escape_raw_dollars(text: str) -> str:
    r"""Escape every unescaped "$" so TeX never enters math mode.

    A PRD is a business document - it contains no legitimate math, but it
    routinely contains currency amounts. A SINGLE unescaped "$" (a price
    "$5" the model wrote into a cell) flips TeX into math mode for the rest
    of the document, and TWO of them ("US$ 1.4M" ... "US$2M") silently
    pair up and poison everything in between - the compile then dies far away
    from the typo with "! Missing $ inserted" or "! Command \bfseries invalid
    in math mode" on the cover page, and every PDF/DOCX export of that PRD
    fails. Pair-based "balancing" cannot fix that class of document, so every
    unescaped "$" is simply escaped to \\$" (rendering a literal dollar
    sign). Already-escaped \\$" passes through untouched and \\\\" directly
    before "$" is recognised as a tabular row terminator rather than an
    escape, which together make the function idempotent.
    """
    if "$" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch == "$":
            out.append("\\$")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# "\\[2.2cm]" written with a SINGLE leading backslash: TeX parses "\\[" as
# the start of display math, so every font/series command after it dies
# with "Command \\bfseries invalid in math mode". Only dimension-shaped
# brackets are repaired so genuine display math is never touched.
_LONE_BACKSLASH_SPACING_BRACKET_RE = re.compile(
    r"(?<!\\)\\\[((?:\d+(?:\.\d+)?)(?:cm|mm|pt|em|ex|in))\]"
)


# "\\[2.2cm]" written with a SINGLE leading backslash: TeX parses "\\[" as
# the start of display math, so every font/series command after it dies
# with "Command \\bfseries invalid in math mode". Only dimension-shaped
# brackets are repaired so genuine display math is never touched.
_LONE_BACKSLASH_SPACING_BRACKET_RE = re.compile(
    r"(?<!\\)\\\[((?:\d+(?:\.\d+)?)(?:cm|mm|pt|em|ex|in))\]"
)


# A row terminator (with optional trailing "[spacing]") at end of line.
_ROW_TERMINATOR_LINE_RE = re.compile(
    r"\\\\(?:\[[0-9.]*(?:cm|mm|pt|em|ex|in)?\])?\s*$"
)

_TABULAR_BLOCK_RE = re.compile(
    r"\\begin\{tabular\}\s*\{(?P<spec>(?:[^{}]|\{[^{}]*\})*)\}"
    r"(?P<rows>.*?)\\end\{tabular\}",
    re.S,
)


def _repair_missing_row_terminators(text: str) -> str:
    r"""Fix ``Misplaced \noalign`` caused by an LLM dropping a row terminator.

    Inside every ``\begin{tabular}...\end{tabular}`` block, a ``\hline`` or
    ``\cline`` that follows a POPULATED row which does not end with ``\\``
    gets a ``\\`` inserted - turning::

        ...content}
        \hline

    into::

        ...content}\\
        \hline

    The FIRST rule of a table (right after the column spec) is legal without
    a terminator and is left untouched, as are any rules that already follow
    a terminated row. Run on every line of the block; only the flag
    "does the current row already end with \\" drives the decision.
    """
    def _fix_block(match) -> str:
        spec = match.group("spec")
        body = match.group("rows")
        out: list[str] = []
        seen_first_rule = False
        row_terminated = False
        for line in body.split("\n"):
            stripped = line.strip()
            if re.match(r"\\(?:hline|cline)", stripped):
                if not seen_first_rule:
                    seen_first_rule = True
                    out.append(line)
                    row_terminated = False
                    continue
                if not row_terminated:
                    indent = re.match(r"^\s*", line).group(0)
                    out.append(indent + "\\\\")
                out.append(line)
                row_terminated = False
                continue
            if _ROW_TERMINATOR_LINE_RE.search(stripped):
                row_terminated = True
            out.append(line)
        return (
            r"\begin{tabular}{" + spec + "}" + "\n".join(out) + r"\end{tabular}"
        )

    return _TABULAR_BLOCK_RE.sub(_fix_block, text)


def sanitize_generated_latex(latex_source: str) -> str:
    r"""Repair common LLM escaping mistakes in a generated LaTeX document.

    Repairs:

    - literal CR / tab escape artifacts,
    - literal newline escape artifacts (\\n): both the bare form and
      the backslash-n a model embeds INSIDE a cell before a word
      ("The system\\nis" -> "The system is") which used to
      crash Tectonic with '! Undefined control sequence' on the unknown
      \\nXYZ token while pandoc silently dropped it. Real commands
      (\\newpage, \\newline, \\noindent, \\nu ...) and
      row-break-then-word sequences (\\\nobody) are preserved,
    - every unescaped "$" -> \\$" (see _escape_raw_dollars),
    - row-break spacing brackets written with a single leading backslash
      ("\\[2.2cm]" -> "\\\\[2.2cm]"; see _LONE_BACKSLASH_SPACING_BRACKET_RE),
    - a tabular row whose terminator "\\\\" was dropped before a "\\hline" /
      "\\cline" (see _repair_missing_row_terminators) - this fixes the real
      '! Misplaced \\noalign' compile error on LLM-filled templates.

    Idempotent; a no-op for correctly-escaped sources (including the
    authoritative template)."""
    s = latex_source or ""
    if "\\n" in s or "\\r" in s or "\\t" in s:
        s = _LITERAL_CR_ARTIFACT_RE.sub("", s)
        s = _LITERAL_TAB_ARTIFACT_RE.sub(" ", s)
        s = _LITERAL_NEWLINE_ARTIFACT_RE.sub(_repair_newline_artifact, s)
    if "\\[" in s:
        s = _LONE_BACKSLASH_SPACING_BRACKET_RE.sub(r"\\\\[\1]", s)
    if "\\hline" in s or "\\cline" in s:
        s = _repair_missing_row_terminators(s)
    return _escape_raw_dollars(s)


def is_markdown_prd(source: str) -> bool:
    """Heuristic: is this stored PRD the legacy Markdown format?

    LaTeX PRDs contain control sequences (``\\begin{tabular}``, ``\\thispagestyle``,
    ...); Markdown PRDs contain pipe tables / hash headings / bullet lists.
    Decision rule:

    1. Any STRONG LaTeX signal (``\\begin{``, ``\\documentclass``, ...) wins
       immediately -> LaTeX.
    2. Otherwise the presence of Markdown structure decides -> Markdown.
    3. Plain prose with neither -> LaTeX (the historical default; the TeX
       wrapper tolerates prose better than the GFM reader tolerates TeX).

    Used only to pick the correct export pipeline (Tectonic vs Pandoc) — never
    to validate content.
    """

    src = _strip_code_fence(source).strip()
    if not src:
        return False
    # Repair LLM escaping artifacts (literal \n sequences) BEFORE classifying:
    # a double-escaped document collapses onto one physical line, which would
    # otherwise defeat the line-based markdown heuristics below.
    src = sanitize_generated_latex(src)
    if not src:
        return False
    if any(sig in src for sig in _STRONG_LATEX_SIGNALS):
        return False
    return has_markdown_structure(src)


# ---------------------------------------------------------------------------
# Full-document assembly
# ---------------------------------------------------------------------------
def _load_template_preamble_and_footer() -> tuple[str, str]:
    """Returns (preamble, footer) split around the body of the authoritative
    template-krungsrinimble.tex so partial LLM output can be wrapped in the
    correct, PDF-exact LaTeX environment."""
    tex_path = _LATEX_SERVICE_DIR / "prompts" / "template-krungsrinimble.tex"
    tex = tex_path.read_text(encoding="utf-8")
    and_head = "\\begin{document}"
    idx = tex.find(and_head)
    if idx == -1:
        raise RuntimeError("Authoritative LaTeX template is missing \\begin{document}.")
    preamble = tex[: idx + len(and_head)]
    footer = "\\end{document}\n"
    return preamble, footer


def ensure_full_document(latex_source: str) -> str:
    r"""Return a compilable standalone LaTeX document.

    The document body is always placed inside the preamble from
    ``prompts/template-krungsrinimble.tex``.  This is intentional even when a
    stored/LLM-produced source is already standalone: accepting its preamble
    would allow page size, margins, fonts, colours, and running furniture to
    drift away from the required export format.
    """
    src = (latex_source or "").strip()
    if not src:
        raise ValueError("Cannot export an empty PRD.")

    # Defensive: an LLM may wrap the document in a fenced code block even when
    # told not to. Strip a leading/trailing fence before anything else.
    if src.startswith("```"):
        src = src.split("\n", 1)[1] if "\n" in src else ""
        if src.rstrip().endswith("```"):
            src = src.rstrip()[:-3]
        src = src.strip()
        if not src:
            raise ValueError("Cannot export an empty PRD.")

    # Defensive: repair LLM escaping artifacts (literal \n sequences from a
    # double-escaped JSON response) before any structural inspection - without
    # this the whole document is one physical line and TeX halts with
    # "! Undefined control sequence \n".
    src = sanitize_generated_latex(src)

    # A full standalone source may carry formatting invented by the model or
    # copied from an older template.  Keep only its body so the authoritative
    # template remains the single source of export formatting.
    begin = "\\begin{document}"
    end = "\\end{document}"
    if begin in src:
        inner_start = src.find(begin) + len(begin)
        inner_end = src.rfind(end)
        src = (
            src[inner_start:inner_end]
            if inner_end > inner_start
            else src[inner_start:]
        ).strip()

    # Legacy documents produced before the LaTeX switch are Markdown; wrapping
    # those in a LaTeX preamble would only produce confusing TeX errors, so
    # fail with actionable guidance instead.
    markdown_hints = ("\n# ", "\n|---", "\n| ---", "\n- **")
    if any(hint in src for hint in markdown_hints):
        raise ValueError(
            "This PRD is stored as Markdown (generated before the LaTeX switch). "
            "Regenerate the PRD to export it from the Krungsri Nimble .tex template."
        )

    preamble, footer = _load_template_preamble_and_footer()
    return f"{preamble}\n{src}\n{footer}"


# ---------------------------------------------------------------------------
# Pandoc-friendly LaTeX conversion
# ---------------------------------------------------------------------------
# The generated PRD follows template-krungsrinimble.tex, which relies on
# constructs pandoc's LaTeX reader cannot handle: custom `\newcolumntype` column
# specs (`L{..}`/`R{..}`), `\shortstack`, `\multirow`, `\multicolumn`, `\parbox`
# and `\rowcolor`. When pandoc parses those it emits the raw column spec as
# text and produces NO real tables in the DOCX. So, before calling pandoc we
# rewrite the (deterministic) template constructs into a pandoc-compatible
# subset — native `p{..}` columns, plain cells and `\\`/`\newline` row/line
# breaks. The transform is template-shaped and tested in backend/tests.

# Minimal preamble pandoc needs to compile the rewritten body. The full Krungsri
# preamble (fancyhdr, xcolor, custom column types, ...) is purposefully dropped:
# page furniture is applied by Word itself and none of those packages are
# understood by pandoc anyway.
_PANDOC_SAFE_PREAMBLE = r"""\documentclass[12pt,letterpaper]{article}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\begin{document}
"""

_PANDOC_SAFE_FOOTER = r"""
\end{document}
"""


def _take_braced_group(text: str, i: int) -> tuple[str, int]:
    """``text[i]`` must be ``{``; return ``(contents, index_after_closing)``."""

    depth = 0
    k = i
    while k < len(text):
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:k], k + 1
        k += 1
    return text[i + 1:], len(text)


def _rewrite_command(text: str, command: str, handler) -> str:
    r"""Rewrite every ``\command{arg1}{arg2}...`` using a callable handler.

    ``handler(args)`` receives the list of braced argument strings (balanced
    braces respected, so ``\shortstack{\prdlbl{a}}`` arrives as one group) and
    returns the replacement text. Anything that is not an exact ``\command{g}``
    series is copied through untouched.
    """

    token = "\\" + command
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(token, i):
            j = i + len(token)
            while j < n and text[j] in " \t\n\r*":
                j += 1
            if j < n and text[j] == "{":
                args: list[str] = []
                while j < n and text[j] == "{":
                    content, end = _take_braced_group(text, j)
                    args.append(content)
                    j = end
                if args:
                    out.append(handler(args))
                    i = j
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)
_TABULAR_ENV_RE = re.compile(
    r"\\begin\{(tabular|longtable)\}.*?\\end\{\1\}",
    re.S,
)


def _keep_in_cell_rowbreaks(block: str) -> str:
    r"""Rewrite top-level in-cell ``\\`` line breaks so pandoc keeps one
    preview row per template row.

    The Krungsri tables use ``p{}``-style paragraph columns where a ``\\``
    in the MIDDLE of a row's cell content (``Introduction ... \\
    Executive Summary & ...``) is a line break inside the cell, while every
    row-terminating ``\\`` sits at the end of a source line (or is
    followed by ``\hline`` / ``\cline`` / ``\end`` / ``&``).
    Pandoc's LaTeX reader treats every top-level ``\\`` as a row
    terminator, splitting one template row into two broken preview rows, so
    mid-line ``\\`` must be rewritten to ``\newline`` (pandoc renders
    it as a ``<br>`` line break inside the cell). Breaks nested in braces
    (``\shortstack{...}``) are untouched here - the unwrap above rewrites
    those itself.
    """

    hline_rules = (chr(92) + "hline", chr(92) + "cline")
    end_rule = chr(92) + "end{"
    newline_cmd = chr(92) + "newline"
    out: list[str] = []
    i = 0
    n = len(block)
    depth = 0
    while i < n:
        ch = block[i]
        if ch == "{":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            depth -= 1
            out.append(ch)
            i += 1
            continue
        if depth == 0 and block.startswith(chr(92) * 2, i):
            j = i + 2
            if j < n and block[j] == "*":
                j += 1
            if j < n and block[j] == "[":
                closer = block.find("]", j + 1)
                if closer != -1:
                    j = closer + 1
            k = j
            while k < n and block[k] in " \t":
                k += 1
            rest = block[k:]
            at_line_end = k >= n or block[k] in "\r\n"
            if (
                at_line_end
                or rest.startswith(hline_rules)
                or rest.startswith(end_rule)
                or rest.startswith("&")
            ):
                out.append(block[i:j])  # real row terminator: keep verbatim
            else:
                out.append(newline_cmd)  # in-cell break -> <br> in preview
            i = j
            continue
        if ch == chr(92):  # single backslash: control word or escaped char
            j = i + 1
            if j < n and block[j].isalpha():
                while j < n and block[j].isalpha():
                    j += 1
                out.append(block[i:j])
                i = j
                continue
            if j < n:
                out.append(block[i:j + 1])
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def preprocess_latex_for_pandoc(latex_body: str) -> str:
    """Rewrite Krungsri-template LaTeX into a pandoc-compatible subset.

    - ``\\multirow{n}{w}{content}`` / ``\\multicolumn{n}{spec}{content}`` keep
      only their content (merged-cell layout is handled by Word later).
    - ``\\shortstack{...}`` and ``\\parbox{w}{...}`` are unwrapped; inner line
      breaks become ``\\newline`` which pandoc renders as real line breaks.
    - Top-level in-cell ``\\\\`` row breaks (the template's multi-line label
      cells, e.g. ``Introduction \\&\\\\ Executive Summary``) become
      ``\\newline`` too, so pandoc keeps one preview row per template row.
    - ``\\prdlbl{...}`` becomes ``\\textbf{...}``, ``\\prdfield{...}`` is unwrapped.
    - Custom ``L{..}``/``R{..}`` columns become ``p{..}``; the definitions and
      conflicts that confuse pandoc (``\\rowcolor``, ``\\cline``, ``\\newcolumntype``,
      ``\\renewcommand``, ``\\bgroup``/``\\egroup``) are dropped.
    """

    s = latex_body or ""

    def _unwrap_last(args: list[str]) -> str:  # keep the final argument (the content)
        return args[-1] if args else ""

    def _unwrap_shortstack(args: list[str]) -> str:
        inner = args[-1] if args else ""
        return inner.replace(r"\\", r"\newline")

    # Outer wrappers first so the inner content can be further rewritten.
    s = _rewrite_command(s, "multirow", _unwrap_last)
    s = _rewrite_command(s, "multicolumn", _unwrap_last)
    s = _rewrite_command(s, "parbox", _unwrap_last)
    s = _rewrite_command(s, "shortstack", _unwrap_shortstack)
    s = _rewrite_command(s, "coverfont", _unwrap_last)

    # Simple labelled fields are used all over the template.
    s = re.sub(r"\\prdlbl\{([^{}]*)\}", r"\\textbf{\1}", s)
    s = re.sub(r"\\prdfield\{([^{}]*)\}", r"\1", s)

    # Constructs that do not survive pandoc's reader and would otherwise leak
    # literal text into the DOCX.
    s = s.replace(r"\bgroup", "").replace(r"\egroup", "")
    s = s.replace(r"\thispagestyle{empty}", "")
    s = s.replace(r"\arraybackslash", "")

    # Custom column types -> native p{} columns (pandoc reads `|p{..}|` fine).
    s = re.sub(r"L\{(\d*\.?\d*cm)\}", r"p{\1}", s)
    s = re.sub(r"R\{(\d*\.?\d*cm)\}", r"p{\1}", s)
    s = re.sub(r"(?m)^\s*\\newcolumntype\s*\{.*?$", "", s)
    s = re.sub(r"(?m)^\s*\\renewcommand\s*\{.*?$", "", s)

    # Multi-line template cells: the unwraps above promote ``\\`` line breaks
    # that lived inside \parbox/\multirow cells to the top level, where
    # pandoc's reader would mistake them for row terminators and split one
    # template row into two broken preview rows. Rescan now that the cell
    # contents sit at the top level (this must run while \hline/\cline are
    # still present - they mark the real row terminators below).
    s = _TABULAR_ENV_RE.sub(lambda m: _keep_in_cell_rowbreaks(m.group(0)), s)

    # Row colors / partial rules / row-spanning artefacts confuse pandoc.
    s = re.sub(r"\\rowcolor\s*\{[^{}]*\}", "", s)
    s = re.sub(r"\\cline\s*\{[^{}]*\}", "", s)
    s = re.sub(r"\\hline", "", s)
    s = re.sub(r"(?<!\\)(?<!\\)\\\[[\d.]*(?:cm|pt|em|ex)?\]", "", s)

    # Layout-only space / font glue that pandoc's reader complains about.
    s = re.sub(r"\\(?:vspace|hspace)\*?\s*\{[^{}]*\}", "", s)
    s = re.sub(r"\\fontsize\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\\selectfont", "", s)
    # Font/layout declarations around the cover text are meaningful to the PDF
    # renderer but cause pandoc to preserve raw HTML or discard the wrapped
    # words. The preview supplies its own typography, so retain the text only.
    s = re.sub(r"\\(?:sffamily|large|normalsize|mdseries|noindent|par)\b", "", s)
    s = re.sub(r"\{\\Large\s*", "", s)
    s = re.sub(r"\{\\bfseries\s*", r"\\textbf{", s)
    s = s.replace(r"\\\[2.2cm]", "").replace(r"\\\[0.4cm]", "").replace(r"\\\[1.8cm]", "")

    return s


def _extract_latex_body(source: str) -> str:
    """Return only the document body (between ``\\begin{document}`` and
    ``\\end{document}``) when the source is a full standalone document;
    otherwise return the (fence-stripped) source unchanged."""

    src = _strip_code_fence(source).strip()
    begin = "\\begin{document}"
    end = "\\end{document}"
    if "\\documentclass" in src or begin in src:
        inner_start = src.find(begin)
        if inner_start != -1:
            inner_end = src.rfind(end)
            inner_start += len(begin)
            if inner_end > inner_start:
                return src[inner_start:inner_end]
            return src[inner_start:]
    return src


def pandoc_friendly_document(source: str) -> tuple[str, str]:
    """Return ``(document, kind)`` ready for pandoc.

    ``kind`` is ``"markdown"`` for legacy Markdown PRDs (passed through so
    pandoc parses the GFM pipe tables natively) or ``"latex"`` for the
    Krungsri LaTeX output (rewritten into a pandoc-compatible subset and
    wrapped in a minimal preamble).
    """

    src = _strip_code_fence(source).strip()
    if not src:
        raise ValueError("Cannot export an empty PRD.")

    # Repair LLM escaping artifacts (literal \n sequences) first so the line
    # structure - and therefore the markdown/latex classification below - is
    # judged on the document the model actually intended to emit.
    src = sanitize_generated_latex(src)

    if is_markdown_prd(src):
        return src, "markdown"

    body = _extract_latex_body(src)
    if not body.strip():
        raise ValueError("Cannot export an empty PRD.")
    processed = preprocess_latex_for_pandoc(body)
    return (
        f"{_PANDOC_SAFE_PREAMBLE}\n{processed}\n\n{_PANDOC_SAFE_FOOTER}",
        "latex",
    )
# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
def _tex_error_excerpt(output: str, max_lines: int = 10) -> str:
    """Extract the actionable lines from a TeX engine's output.

    TeX reports real errors as ``! <message>`` blocks followed by an
    ``l.<lineno>`` source-context line. Tectonic also emits a wall of
    informational notes/warnings, so everything else is noise for the caller.
    Returns a short excerpt (empty string when nothing useful is found).
    """
    lines = [ln.rstrip() for ln in (output or "").splitlines()]
    excerpt: list[str] = []
    for i, ln in enumerate(lines):
        if ln.startswith("!"):
            excerpt.extend(lines[i:i + 3])
            for j in range(i + 1, min(i + 8, len(lines))):
                if re.match(r"l\.\d+", lines[j]):
                    excerpt.append(lines[j])
                    break
            if len(excerpt) >= max_lines:
                break
    if not excerpt:
        excerpt = [ln for ln in lines if ln.strip()][-max_lines:]
    return "\n".join(excerpt[:max_lines]).strip()


def _save_failure_diagnostics(workdir: Path, label: str, engine_output: str) -> Path:
    """Persist the failing .tex/.log + engine output for post-mortem debugging.

    The temporary compile directory is deleted right after a failure, which
    used to destroy the only evidence of WHY the LLM-produced LaTeX broke.
    Returns the diagnostics directory that now holds the artifacts.
    """
    diag_dir = Path(tempfile.gettempdir()) / "agenticdia-latex-failures"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = diag_dir / f"{stamp}-{label.lower().replace(' ', '-')}"
    target.mkdir(parents=True, exist_ok=True)
    try:
        for pattern in ("*.tex", "*.log"):
            for src in workdir.glob(pattern):
                shutil.copy2(src, target / src.name)
        (target / "engine-output.txt").write_text(engine_output or "", encoding="utf-8")
    except OSError:
        logger.warning("Could not persist LaTeX failure diagnostics to %s", target)
    return target


def _run(cmd: list[str], workdir: Path, label: str) -> None:
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("LaTeX %s compile timed out after %ss: %s", label, _COMPILE_TIMEOUT_S, cmd)
        raise RuntimeError(
            f"LaTeX {label} compilation timed out after {_COMPILE_TIMEOUT_S}s."
        ) from exc
    duration = time.time() - start
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        logger.error(
            "LaTeX %s failed (rc=%s, %.1fs):\nstdout:\n%s\nstderr:\n%s",
            label, result.returncode, duration, result.stdout[-1500:], result.stderr[-1500:],
        )
        diag_dir = _save_failure_diagnostics(workdir, label, combined)
        excerpt = _tex_error_excerpt(combined)
        detail = f" at line context: {excerpt.splitlines()[-1]}" if excerpt else ""
        logger.error("LaTeX %s failure diagnostics saved to %s%s", label, diag_dir, detail)
        raise LaTeXCompileError(
            f"LaTeX {label} compilation failed."
            + (f" TeX reported:\n{excerpt}" if excerpt else "")
            + f"\nDiagnostics saved to: {diag_dir}"
        )


def compile_latex_to_pdf(latex_source: str) -> bytes:
    """Compile a LaTeX PRD to PDF bytes via Tectonic."""
    # Repair LLM escaping artifacts (literal \\n/\\r/\\t tokens and unescaped $)
    # BEFORE compiling, mirroring the DOCX path (pandoc_friendly_document
    # sanitizes the same way). Leaving this out made PDF -- but not DOCX --
    # export of the same generated PRD fail when the model emitted a
    # deliberately-escaped newline or a raw currency dollar.
    full = ensure_full_document(sanitize_generated_latex(latex_source))
    with tempfile.TemporaryDirectory(prefix="prd-pdf-") as tmp:
        workdir = Path(tmp)
        # Keep the emergency direct-PDF path typographically identical to the
        # DOCX/LibreOffice path. Without this, Tectonic used Helvetica while
        # the downloaded Word document used Aptos.
        aptos_fonts = _aptos_font_files()
        if aptos_fonts:
            shutil.copy2(aptos_fonts["regular"], workdir / "Aptos.ttf")
            shutil.copy2(aptos_fonts["bold"], workdir / "Aptos-Bold.ttf")
            full = _enable_aptos_for_tectonic(full)
        src_path = workdir / "prd.tex"
        src_path.write_text(full, encoding="utf-8")
        _run([tectonic_bin(), "prd.tex"], workdir, "PDF")
        pdf_path = workdir / "prd.pdf"
        if not pdf_path.exists():
            raise RuntimeError("Tectonic finished but produced no prd.pdf.")
        return pdf_path.read_bytes()


def convert_markdown_to_pdf(markdown: str) -> bytes:
    """Render a legacy Markdown PRD to PDF via Pandoc -> stand-alone LaTeX -> Tectonic.

    Pandoc turns the GFM pipe tables into ``longtable`` environments and
    Tectonic compiles them; this keeps a single PDF engine (Tectonic) for every
    export instead of reaching for a second TeX toolchain.
    """

    src = _strip_code_fence(markdown).strip()
    if not src:
        raise ValueError("Cannot export an empty PRD.")
    with tempfile.TemporaryDirectory(prefix="prd-md-pdf-") as tmp:
        workdir = Path(tmp)
        md_path = workdir / "prd.md"
        md_path.write_text(src, encoding="utf-8")
        _run(
            [pandoc_bin(), "prd.md", "-f", "gfm", "-t", "latex", "--standalone", "-o", "prd.tex"],
            workdir,
            "PDF (Markdown)",
        )
        _run([tectonic_bin(), "prd.tex"], workdir, "PDF (Markdown)")
        pdf_path = workdir / "prd.pdf"
        if not pdf_path.exists():
            raise RuntimeError("Tectonic finished but produced no prd.pdf.")
        return pdf_path.read_bytes()


def convert_latex_to_docx(latex_source: str) -> bytes:
    """Convert a LaTeX PRD to DOCX bytes.

    The Krungsri-native renderer preserves template geometry, merged cells,
    shading, headers, and footers. Pandoc remains the fallback for unexpected
    input that the native renderer cannot process.
    """
    return convert_to_docx(latex_source)


def convert_markdown_to_docx(markdown: str) -> bytes:
    """Convert a legacy Markdown PRD to DOCX bytes via Pandoc's GFM reader."""
    return convert_to_docx(markdown)


def convert_to_docx(source: str) -> bytes:
    """Shared DOCX pipeline.

    LaTeX PRDs are rendered by the NATIVE renderer
    (:mod:`app.latex_docx_native`) so the Word document carries real
    ``\\multirow``/``\\multicolumn`` merged cells, shaded header
    rows and fixed column widths, all of which Pandoc's table flattening
    destroys. The PDF export renders THIS document via LibreOffice
    (:func:`docx_to_pdf`), so both downloads always match. The Pandoc
    pipeline remains the fallback if the native renderer fails
    unexpectedly, and the only path for legacy Markdown PRDs.
    """

    if not is_markdown_prd(source):
        try:
            from app.latex_docx_native import latex_to_docx_native

            return latex_to_docx_native(source)
        except Exception:
            logger.warning(
                "Native DOCX render failed; falling back to the Pandoc pipeline.",
                exc_info=True,
            )

    document, kind = pandoc_friendly_document(source)

    with tempfile.TemporaryDirectory(prefix="prd-docx-") as tmp:
        workdir = Path(tmp)
        src_path = workdir / ("prd.md" if kind == "markdown" else "prd.tex")
        src_path.write_text(document, encoding="utf-8")
        out_path = workdir / "prd.docx"
        if kind == "markdown":
            _run(
                [pandoc_bin(), "prd.md", "-f", "gfm", "-t", "docx", "-o", "prd.docx"],
                workdir,
                "DOCX",
            )
        else:
            _run(
                [pandoc_bin(), "prd.tex", "-o", "prd.docx", "--standalone"],
                workdir,
                "DOCX",
            )
        if not out_path.exists():
            raise RuntimeError("Pandoc finished but produced no prd.docx.")
        return out_path.read_bytes()


# ---------------------------------------------------------------------------
# DOCX -> PDF (LibreOffice headless) - the PRIMARY PDF engine
# ---------------------------------------------------------------------------
# The PDF export renders the SAME Word document the DOCX export produces, so
# both downloads always match. This matters because the TeX pipeline drops
# every Thai glyph (NimbusSanL/lmodern have no Thai coverage) and fails hard
# on LLM LaTeX mistakes, while the Word renderer handles full Unicode and
# degrades gracefully.
def soffice_bin() -> str:
    """Locate the LibreOffice headless converter.

    Same discovery order as the TeX toolchain: $SOFFICE_BIN, the well-known
    install locations, then anything on PATH.
    """
    return _find_binary("SOFFICE_BIN", _DEFAULT_SOFFICE_CANDIDATES, "soffice")


def soffice_available() -> bool:
    """True when LibreOffice is reachable (PDF exports go through it)."""
    try:
        return bool(soffice_bin())
    except FileNotFoundError:
        return False


def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Render DOCX bytes to PDF bytes via headless LibreOffice.

    The Noto Sans Thai faces vendored under ``.tools/fonts/`` are copied into
    the isolated LibreOffice profile's ``user/fonts`` directory first: the
    macOS headless build enumerates ONLY the fonts inside its own profile and
    bundle (system fonts such as Tahoma/Thonburi are invisible to it), so
    without this step every Thai run degrades to a glyph-less font and the
    PDF shows blank gaps.

    Raises:
        ValueError: empty payload.
        FileNotFoundError: soffice not installed (caller decides the fallback).
        RuntimeError: soffice ran but produced nothing / failed.
    """
    if not docx_bytes:
        raise ValueError("Cannot convert an empty DOCX document.")
    soffice = soffice_bin()  # FileNotFoundError propagates to the fallback
    with tempfile.TemporaryDirectory(prefix="prd-docx-pdf-") as tmp:
        workdir = Path(tmp)
        src_path = workdir / "prd.docx"
        src_path.write_bytes(docx_bytes)
        outdir = workdir / "out"
        outdir.mkdir()
        # An isolated user profile is REQUIRED: a concurrently running
        # interactive LibreOffice (or a stale profile lock) otherwise aborts
        # the headless conversion with "could not establish a connection...".
        profile = workdir / "lo_profile"
        profile.mkdir()
        # Vendored Thai-capable faces -> <profile>/user/fonts (see docstring).
        user_fonts = profile / "user" / "fonts"
        user_fonts.mkdir(parents=True)
        for font in _VENDORED_FONTS:
            try:
                shutil.copy2(font, user_fonts / font.name)
            except OSError:
                logger.warning("Could not stage vendored font %s for LibreOffice.", font)
        # Aptos is the Latin face in both the supplied reference PDF and the
        # native DOCX. LibreOffice cannot discover Office's private DFonts
        # directory, so explicitly expose the local Word copies to this
        # conversion profile and prevent a visually different substitution.
        for font in _aptos_font_files().values():
            try:
                shutil.copy2(font, user_fonts / font.name)
            except OSError:
                logger.warning("Could not stage Aptos font %s for LibreOffice.", font)
        start = time.time()
        result = subprocess.run(
            [
                soffice, "--headless", "--norestore", "--nolockcheck",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to", "pdf:writer_pdf_Export",
                "--outdir", str(outdir), str(src_path),
            ],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_S,
        )
        pdf_path = outdir / "prd.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            logger.error(
                "DOCX->PDF conversion failed (rc=%s, %.1fs):\nstdout:\n%s\nstderr:\n%s",
                result.returncode,
                time.time() - start,
                result.stdout[-1500:],
                result.stderr[-1500:],
            )
            raise RuntimeError(
                "LibreOffice DOCX->PDF conversion failed"
                + (f": {result.stderr.strip()[:300]}" if result.stderr.strip() else ".")
            )
        return pdf_path.read_bytes()


def _demote_headings(markdown: str, levels: int = 2) -> str:
    """GFM headings from pandoc are H1 (``# Stakeholders``); the frontend section
    splitter keys off the template's ``### `` level, so demote every heading."""

    lines = markdown.split("\n")
    out_lines = []
    for line in lines:
        m = re.match(r"^(#{1,6})(\s+.*)$", line)
        if m:
            out_lines.append("#" * (len(m.group(1)) + levels) + m.group(2))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


# Pandoc's gfm writer falls back to a raw ``<table>`` block whenever a table is
# too complex for pipe syntax (e.g. the merged-cell Product Scope grid whose
# unwrapped rows have ragged cell counts). The frontend markdown renderer does
# not understand raw HTML, so convert those blocks back into pipe tables.
_HTML_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_HTML_ENTITIES = (
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
    ("&nbsp;", " "),
    ("&ldots;", "..."),
    ("&hellip;", "..."),
)


def _clean_html_cell(html: str) -> str:
    """Normalise one ``<td>/<th>`` payload into single-line GFM cell text."""
    text = re.sub(r"(?i)<br\s*/?>", "<br>", html)
    # Keep emphasis semantics the renderer understands.
    text = re.sub(r"(?is)<strong>(.*?)</strong>", r"**\1**", text)
    text = re.sub(r"(?is)<em>(.*?)</em>", r"*\1*", text)
    text = re.sub(r"(?i)</?(?!br\b)[a-z][^>]*>", "", text)
    for entity, char in _HTML_ENTITIES:
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip().replace("|", "\\|")


def _html_table_to_gfm(table_html: str) -> str:
    """Convert one pandoc ``<table>`` block into a GFM pipe table."""
    rows: list[list[str]] = []
    for tr in _TR_RE.findall(table_html):
        cells = [_clean_html_cell(c) for c in _TD_RE.findall(tr)]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _normalize_pandoc_gfm(markdown: str) -> str:
    """Post-process pandoc GFM output: demote headings and inline any leftover
    HTML tables so the frontend renders pure markdown."""
    normalized = _demote_headings(markdown)
    # The official template centres Contents with \makebox. Pandoc preserves
    # the section boundary but loses that wrapped title, producing a bare
    # ``###``. Restore the visible/canonical title before the document is split
    # into independently editable and lockable sections.
    normalized = re.sub(r"(?m)^###\s*$", "### Contents", normalized)

    def _sub(match: re.Match) -> str:
        converted = _html_table_to_gfm(match.group(0))
        return f"\n{converted}\n" if converted else ""

    normalized = _HTML_TABLE_RE.sub(_sub, normalized)
    return _normalize_template_table_semantics(normalized)


def _normalize_template_table_semantics(markdown: str) -> str:
    """Repair Pandoc's interpretation of the three complex trailing tables.

    The LaTeX template uses merged/multirow cells. GFM has no equivalent, so
    Pandoc can split one label across several physical rows and can promote the
    first real data row to a Markdown header. Keep the visual document and the
    structured editor faithful by joining split labels and adding explicit
    semantic headers where the source table has none.
    """
    source = markdown or ""

    def _section(title: str, transform) -> None:
        nonlocal source
        pattern = re.compile(
            rf"(?ms)(^###\s+{re.escape(title)}\s*$)(.*?)(?=^###\s+|\Z)"
        )
        source = pattern.sub(lambda match: match.group(1) + transform(match.group(2)), source)

    def _product(body: str) -> str:
        # Wrapped words are one semantic label, not separate requirements.
        body = re.sub(
            r"\|\s*Mapping\s*&\s*\|\s*\|\s*\|\s*\|\s*\n"
            r"\|\s*Functional\s*\|\s*\|\s*\|\s*\|\s*\n"
            r"\|\s*Requirements\s*\|\s*([^|]*)\|\s*\|\s*\|",
            r"| Mapping & Functional Requirements | \1 |  |  |",
            body,
        )
        body = re.sub(
            r"\|\s*Definition \(In\s*\|\s*\|\s*\|\s*\|\s*\n"
            r"\|\s*vs\. Out\)\s*\|\s*\|\s*\|\s*\|",
            "| Scope Definition (In vs. Out) |  |  |  |",
            body,
        )
        # Pandoc inserts hard breaks inside short headings. Let CSS wrap only
        # when the viewport actually requires it.
        body = re.sub(r"(?i)<br\s*/?>\s*", " ", body)
        body = body.replace("**User Story (System** **Focus)**", "**User Story (System Focus)**")
        body = body.replace("**Functional** **Requirement (FR)**", "**Functional Requirement (FR)**")

        # Keep the three persisted user-story fields readable inside their
        # Markdown table cell. Pandoc emits the LaTeX ``\\newline`` markers as
        # hard breaks, while the heading cleanup above intentionally flattens
        # hard breaks. Re-instate paragraph spacing only for story data rows.
        readable_lines = []
        for line in body.splitlines():
            if (
                line.lstrip().startswith("|")
                and "As a " in line
                and "I want to " in line
                and "So that " in line
            ):
                line = re.sub(r"\s+I want to\s+", "<br><br>I want to ", line, count=1)
                line = re.sub(r"\s+So that\s+", "<br><br>So that ", line, count=1)
            readable_lines.append(line)
        body = "\n".join(readable_lines)

        # The nested LaTeX scope block has no direct GFM equivalent. Pandoc
        # flattens it into unrelated rows, so rebuild it as one clear table.
        # A stored Markdown document may already contain that table after a
        # user edit; never parse it a second time or its header/value cells
        # would be mistaken for flattened LaTeX content and shifted columns.
        if "| Scope In (Included) | Scope Out (Excluded) |" in body:
            return body
        lines = body.splitlines()
        scope_start = next((
            index for index, line in enumerate(lines)
            if "Definition (In" in line or "Scope Definition (In vs. Out)" in line
        ), None)
        if scope_start is None:
            return body
        scope_text = " ".join(
            " ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
            for line in lines[scope_start:] if line.strip().startswith("|")
        ).replace("**", " ")
        scope_text = re.sub(r"Scope Definition \(In vs\. Out\)", "", scope_text, flags=re.I)
        scope_text = re.sub(r"Definition \(In\s+vs\. Out\)", "", scope_text, flags=re.I)
        split_out = re.split(r"\bScope\s+out\b", scope_text, maxsplit=1, flags=re.I)
        in_text = re.sub(r"^.*?\bScope\s+In\b", "", split_out[0], flags=re.I).strip()
        out_text = split_out[1].strip() if len(split_out) > 1 else ""

        def _items(text: str) -> str:
            matches = re.findall(r"\d+\.\s*.*?(?=(?:\s+\d+\.)|$)", text.strip())
            values = [re.sub(r"\s+", " ", item).strip() for item in matches]
            if not values and text.strip():
                values = [text.strip()]
            return "<br>".join(values)

        prefix = lines[:scope_start]
        while prefix and not prefix[-1].strip():
            prefix.pop()
        return "\n".join(prefix + [
            "", "#### Scope Definition (In vs. Out)", "",
            "| Scope In (Included) | Scope Out (Excluded) |", "| --- | --- |",
            f"| {_items(in_text)} | {_items(out_text)} |", "",
        ])

    def _technical(body: str) -> str:
        lines = body.splitlines()
        first = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
        if first is not None and first + 1 < len(lines) and is_markdown_table_separator(lines[first + 1]):
            if "Non-Functional" in lines[first]:
                data_row = lines[first]
                lines[first] = "| Consideration | Details |"
                lines.insert(first + 2, data_row)
        return "\n".join(lines)

    def _appendix(body: str) -> str:
        lines = body.splitlines()
        first = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
        if first is not None and first + 1 < len(lines) and is_markdown_table_separator(lines[first + 1]):
            if "Questions" in lines[first] and "Risks" in lines[first]:
                data_row = lines[first]
                lines[first] = "| Appendix Item | Details | Reference |"
                lines.insert(first + 2, data_row)
        return "\n".join(lines)

    _section("2. Product Scope & Functional Requirements", _product)
    _section("3. Technical & Operational Considerations", _technical)
    _section("4. Appendix", _appendix)
    return source


def is_markdown_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def prd_to_markdown(source: str) -> str:
    """Normalize a stored PRD (LaTeX or legacy Markdown) into GFM Markdown.

    Used by the ``/api/prd/convert`` endpoint and the on-screen preview so the
    frontend never renders raw LaTeX: the Krungsri body is rewritten to the
    pandoc-compatible subset and pandoc converts that to clean GFM pipe tables
    (headings demoted to the ``### `` level the section parser expects).
    Markdown input is returned unchanged.

    PERF (project switch): the preview conversion runs on EVERY project open
    for every LaTeX document, and each run spawns a pandoc subprocess
    (~0.3-1s). Results are therefore memoized per normalized source body
    (see ``_prd_to_markdown_cached``) so repeat conversions are free.
    """

    src = _strip_code_fence(source).strip()
    if not src:
        raise ValueError("Cannot convert an empty PRD.")
    if is_markdown_prd(src):
        return _normalize_template_table_semantics(src)

    document, kind = pandoc_friendly_document(src)
    if kind != "latex":
        return document
    return _prd_to_markdown_cached(document)


@functools.lru_cache(maxsize=32)
def _prd_to_markdown_cached(document: str) -> str:
    """Pandoc LaTeX -> GFM for one pandoc-ready body, memoized.

    Keyed on the full normalized source, so a document that changed is
    converted again (never serves stale output) while unchanged documents —
    the common case when switching back and forth between projects — hit the
    cache. ``maxsize=32`` bounds memory; entries are a few hundred KB at most.
    """
    with tempfile.TemporaryDirectory(prefix="prd-md-") as tmp:
        workdir = Path(tmp)
        src_path = workdir / "prd.tex"
        src_path.write_text(document, encoding="utf-8")
        out_path = workdir / "prd.gfm"
        _run(
            [pandoc_bin(), "prd.tex", "-f", "latex", "-t", "gfm", "--wrap=none", "-o", "prd.gfm"],
            workdir,
            "PRD preview",
        )
        if not out_path.exists():
            raise RuntimeError("Pandoc finished but produced no markdown preview.")
        return _normalize_pandoc_gfm(out_path.read_text(encoding="utf-8"))

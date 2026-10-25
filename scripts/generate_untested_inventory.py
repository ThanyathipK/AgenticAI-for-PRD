"""Generate the static direct-test-reference inventory.

This is deliberately a lightweight name audit, not code coverage. A production
function is considered directly referenced when its leaf name occurs in a test
file. Run from the repository root:

    ./venv/bin/python scripts/generate_untested_inventory.py
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend" / "app"
FRONTEND_ROOT = ROOT / "src"
OUTPUT = ROOT / "UNTESTED_FUNCTION_INVENTORY.md"

TS_FUNCTION_PATTERNS = (
    re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    re.compile(
        r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s*)?\([^\n]*\)\s*=>",
        re.MULTILINE,
    ),
    re.compile(
        r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>",
        re.MULTILINE,
    ),
)

PRIORITY_AREAS = {
    "Authentication": (
        "backend/app/auth.py",
        "backend/app/routes/auth.py",
        "backend/app/repositories/user.py",
        "src/hooks/useAuth.ts",
    ),
    "Background jobs": (
        "backend/app/generation_job_service.py",
        "backend/app/routes/generation_jobs.py",
        "src/hooks/chat/backgroundJobs.ts",
    ),
    "Locking": (
        "backend/app/lock_service.py",
        "backend/app/routes/lock.py",
        "src/hooks/useArtifactLocks.ts",
        "src/hooks/useRequirementStore.ts",
    ),
    "Impact analysis": (
        "backend/app/impact_service.py",
        "src/components/ConfirmationPanel.tsx",
    ),
    "Exports": (
        "backend/app/latex_service.py",
        "backend/app/latex_docx_native.py",
        "backend/app/routes/projects.py",
        "src/hooks/useProjectState.ts",
    ),
}


@dataclass(frozen=True)
class Function:
    path: str
    qualified_name: str
    line: int
    side: str

    @property
    def leaf_name(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


class PythonFunctionVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.parents: list[str] = []
        self.functions: list[Function] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not (node.name.startswith("__") and node.name.endswith("__")):
            name = ".".join((*self.parents, node.name))
            self.functions.append(Function(self.path, name, node.lineno, "Backend"))
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def python_functions(path: Path) -> list[Function]:
    visitor = PythonFunctionVisitor(relative(path))
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.functions


def typescript_functions(path: Path) -> list[Function]:
    text = path.read_text(encoding="utf-8")
    found: dict[tuple[str, int], Function] = {}
    for pattern in TS_FUNCTION_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            found[(name, line)] = Function(relative(path), name, line, "Frontend")
    return sorted(found.values(), key=lambda item: item.line)


def production_functions() -> list[Function]:
    functions: list[Function] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        functions.extend(python_functions(path))
    for path in sorted(FRONTEND_ROOT.rglob("*")):
        if path.suffix in {".ts", ".tsx"} and not path.name.endswith(".d.ts"):
            functions.extend(typescript_functions(path))
    return functions


def test_text() -> str:
    paths = sorted((ROOT / "backend" / "tests").glob("test_*.py"))
    paths.extend(sorted((ROOT / "scripts").glob("*.test.ts")))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def is_referenced(function: Function, tests: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(function.leaf_name)}(?![\w$])", tests) is not None


def priority_section(untested: list[Function]) -> list[str]:
    lines = [
        "## Coverage priorities",
        "",
        "These are the highest-risk candidates from the static audit. Start with public",
        "entry points and failure/rollback paths; private helpers may already be covered",
        "indirectly and should be confirmed with runtime coverage before adding tests.",
        "",
        "| Priority | Area | Direct-reference gaps | Suggested next coverage |",
        "|---:|---|---:|---|",
    ]
    suggestions = {
        "Authentication": "expired/invalid tokens, password change, flexible auth, ownership denial",
        "Background jobs": "recovery, cancellation races, terminal failure serialization",
        "Locking": "ownership checks, optimistic rollback, locked mutation rejection",
        "Impact analysis": "requirement/story diffs, dangling references, downstream summaries",
        "Exports": "DOCX/PDF endpoint failures, conversion fallback, download error handling",
    }
    for priority, (area, paths) in enumerate(PRIORITY_AREAS.items(), start=1):
        count = sum(item.path in paths for item in untested)
        lines.append(f"| {priority} | {area} | {count} | {suggestions[area]} |")
    lines.append("")
    for area, paths in PRIORITY_AREAS.items():
        candidates = [item for item in untested if item.path in paths]
        lines.extend((f"### {area}", ""))
        if not candidates:
            lines.append("- No direct-reference gaps detected.")
        else:
            for item in candidates:
                lines.append(f"- `{item.path}:{item.line}` — `{item.qualified_name}`")
        lines.append("")
    return lines


def render(functions: list[Function], untested: list[Function]) -> str:
    backend = [item for item in untested if item.side == "Backend"]
    frontend = [item for item in untested if item.side == "Frontend"]
    lines = [
        "# Functions With No Direct Unit-Test Reference",
        "",
        "> Generated by `./venv/bin/python scripts/generate_untested_inventory.py`.",
        "> This is a static name audit only: an entry may still execute indirectly through",
        "> an endpoint, component, hook, callback, or another tested function. Use runtime",
        "> coverage to determine whether a listed function is actually unexecuted.",
        "",
        f"- Production named functions detected: **{len(functions)}**",
        f"- No direct test reference found: **{len(untested)}**",
        f"- Backend candidates: **{len(backend)}**",
        f"- Frontend candidates: **{len(frontend)}**",
        "",
        "Anonymous callbacks and Python dunder methods are excluded.",
        "",
        *priority_section(untested),
    ]
    for heading, candidates in (("Backend Python", backend), ("Frontend TypeScript", frontend)):
        lines.extend((f"## {heading}", ""))
        current_path = ""
        for item in candidates:
            if item.path != current_path:
                if current_path:
                    lines.append("")
                current_path = item.path
                lines.extend((f"### `{current_path}`", ""))
            lines.append(f"- `{item.qualified_name}` — line {item.line}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    functions = production_functions()
    tests = test_text()
    untested = [item for item in functions if not is_referenced(item, tests)]
    OUTPUT.write_text(render(functions, untested), encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}: {len(untested)}/{len(functions)} candidates")


if __name__ == "__main__":
    main()

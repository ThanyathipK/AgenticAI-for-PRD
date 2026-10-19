"""CLI for archived JSON snapshots or live Agentic AI projects."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.evaluator import evaluate_snapshot
from evaluation.report import render_markdown


async def _load_live_project(project_id: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    backend_dir = repo_root / "backend"
    sys.path.insert(0, str(backend_dir))

    # The development DATABASE_URL fallback is ``sqlite:///app.db``. The
    # backend is normally launched with ``backend/`` as its working directory,
    # so reproduce that here; otherwise running this CLI from the repository
    # root silently creates/opens a different, empty ``./app.db``.
    os.chdir(backend_dir)
    from app.database import AsyncSessionLocal
    from app.repositories import ProjectRepository
    from app.repositories.prd import PRDDocumentRepository
    from app.repositories.prd_section import PRDSectionRepository
    from app.traceability_service import TraceabilityService

    async with AsyncSessionLocal() as session:
        project = await ProjectRepository.get_by_id(project_id, session)
        if not project:
            raise ValueError(
                f"Project {project_id} was not found in the configured database."
            )
        matrix = await TraceabilityService.build_traceability(project_id, session)
        sections = await PRDSectionRepository.get_by_project(project_id, session)
        documents = await PRDDocumentRepository.get_by_project(project_id, session)
    return {**matrix, "prd_sections": sections, "prd_documents": documents}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate traceability and generated artifacts.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON snapshot to evaluate.")
    source.add_argument("--project-id", help="Project UUID to load from the configured database.")
    parser.add_argument("--json-output", type=Path, help="Write the detailed JSON report.")
    parser.add_argument("--markdown-output", type=Path, help="Write the Markdown summary.")
    parser.add_argument("--fail-under", type=float, default=95.0, help="Required automated pass rate.")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.input:
        snapshot = json.loads(args.input.read_text(encoding="utf-8"))
        base_dir = args.input.resolve().parent
    else:
        original_dir = Path.cwd()
        try:
            snapshot = asyncio.run(_load_live_project(args.project_id))
        except Exception as exc:
            print(f"Evaluation could not load the project: {exc}", file=sys.stderr)
            return 2
        finally:
            os.chdir(original_dir)
        base_dir = original_dir
    report = evaluate_snapshot(snapshot, base_dir=base_dir, automated_threshold=args.fail_under)
    markdown = render_markdown(report)
    if args.json_output:
        args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(markdown, encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(markdown)
    return 0 if report["automated"]["passed_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

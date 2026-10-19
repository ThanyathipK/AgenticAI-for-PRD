# Automated project evaluation

This folder implements the parts of the evaluation that can be performed
deterministically by code. Expert judgments still come from reviewers, but the
tool validates and aggregates their 1–5 scores. Timing data is likewise recorded
externally and summarized here.

## Checks

- `REQ-###` and `US-###` format and uniqueness
- Requirement → user-story and story → acceptance-criteria links
- Story parent references
- Canonical nine-section PRD structure and required generated content
- References in PRD sections and Mermaid sources
- Mermaid flowchart structure and real rendering when `mmdc` is installed
- TXT, Markdown, LaTeX, DOCX, and PDF existence/open/readability, placeholder
  detection, and optional required-heading checks
- Two-reviewer completeness, per-dimension averages, and 1–5 validation
- Average and median generation time, acceptance time, prompts, and revisions

Skipped checks are reported separately and are never included in the pass-rate
denominator. A missing Mermaid renderer therefore cannot inflate or reduce the
score, but remains visible in the report.

## Run against the configured database

From the repository root:

```bash
./venv/bin/python -m evaluation.run \
  --project-id PROJECT_UUID \
  --json-output evaluation-report.json \
  --markdown-output evaluation-report.md
```

The command exits with status 1 when the automated result is below 95% or has a
critical error, which makes it suitable for CI.

The live-project command deliberately resolves the SQLite fallback relative to
`backend/`, matching the normal application startup. Exit status 2 means the
project or configured database could not be loaded.

## Run against an archived snapshot

```bash
./venv/bin/python -m evaluation.run --input snapshot.json
```

Minimal snapshot shape:

```json
{
  "project_id": "example",
  "requirements": [
    {
      "id": "r1",
      "requirement_code": "REQ-001",
      "user_stories": [
        {
          "ticket_code": "US-001",
          "acceptance_criteria": ["Given X, when Y, then Z"]
        }
      ]
    }
  ],
  "prd_sections": [
    {"section_key": "title", "content": "Example PRD"}
  ],
  "diagrams": [
    {"label": "current", "source": "flowchart TD\n  A[REQ-001] --> B[US-001]"}
  ],
  "generated_files": [
    {
      "path": "exports/example.docx",
      "required_headings": ["Business Overview", "Product Scope"]
    }
  ],
  "expert_reviews": [
    {
      "reviewer_id": "reviewer-1",
      "scores": {
        "requirements_complete": 4,
        "traceability_correct": 4,
        "acceptance_criteria_testable": 4,
        "diagrams_match_requirements": 4,
        "prd_and_exports_complete": 4
      }
    }
  ],
  "runs": [
    {
      "generation_seconds": 90,
      "acceptance_seconds": 900,
      "revisions": 1,
      "prompts": 2
    }
  ]
}
```

For a complete expert result, provide two distinct reviewers and all five score
dimensions. The thresholds in the report are explicitly project-defined:
automated checks ≥95%, every expert dimension ≥4/5, and no critical errors.

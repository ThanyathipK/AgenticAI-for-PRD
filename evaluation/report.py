"""Human-readable rendering for evaluation results."""

from __future__ import annotations

from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    automated = report["automated"]
    lines = [
        "# Project Evaluation Report",
        "",
        f"Project: `{report.get('project_id') or 'unspecified'}`",
        "",
        "## Automated checks",
        "",
        f"**{automated['passed']} of {automated['applicable']} checks passed "
        f"({automated['pass_rate'] if automated['pass_rate'] is not None else 'N/A'}%).**",
        "",
        "| Category | Passed | Failed | Skipped | Pass rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, result in report["categories"].items():
        rate = "N/A" if result["pass_rate"] is None else f"{result['pass_rate']}%"
        lines.append(f"| {name} | {result['passed']} | {result['failed']} | {result['skipped']} | {rate} |")
    lines.extend(["", f"Critical errors: **{automated['critical_error_count']}**", ""])
    failures = [row for row in report["checks"] if row["status"] == "fail"]
    if failures:
        lines.extend(["### Failed checks", ""])
        for row in failures:
            marker = " **CRITICAL**" if row["critical"] else ""
            lines.append(f"- `{row['check_id']}`{marker}: {row['detail']}")
        lines.append("")

    expert = report["expert_review"]
    lines.extend(["## Expert review", ""])
    if not expert["complete"]:
        lines.append("Incomplete: at least two reviewers must score every dimension from 1 to 5.")
    else:
        lines.append(f"Overall dimension average: **{expert['overall_average']}/5**")
    lines.extend(["", "## Time measurement", ""])
    timing = report["time_measurement"]
    if not timing["run_count"]:
        lines.append("No timing runs supplied.")
    else:
        lines.append(f"Runs: **{timing['run_count']}**")
        for key in ("generation_seconds", "acceptance_seconds", "revisions", "prompts"):
            value = timing[key]
            lines.append(f"- {key}: median {value['median']}, average {value['average']} (n={value['count']})")
    lines.extend(
        [
            "",
            "## Project-defined decision",
            "",
            f"- Automated threshold met: **{'yes' if automated['passed_threshold'] else 'no'}**",
            f"- Expert threshold met: **{'yes' if expert['passed'] else 'no/incomplete'}**",
            "- Required thresholds: automated ≥95%, every expert dimension ≥4/5, and no critical errors.",
            "",
        ]
    )
    return "\n".join(lines)

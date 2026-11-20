"""Workflow reports never include raw context packets or provider responses."""

import html
import json
from dataclasses import fields, is_dataclass

from pydantic import BaseModel

from reviewflow.report import safe_text
from reviewflow.review.workflow_models import RunReport

NOTICE = (
    "Offline simulated workflow: no live model was called. "
    "Candidates and verdicts are demonstration data, not a code-review conclusion."
)


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def render_review(report: RunReport, fmt: str = "text") -> str:
    live = report.execution == "live_development"
    notice = (
        "Live development review: cost is unknown. "
        "A model verdict is not test verification or a release-safety guarantee."
        if live
        else NOTICE
    )
    if fmt == "json":
        return json.dumps(
            {"report_type": "review_workflow", **_plain(report), "notice": notice},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    lines = [
        "ReviewFlow · live development review" if live else "ReviewFlow · simulated review workflow",
        "",
        notice,
        "",
        f"Mode      {report.mode}",
        f"Status    {report.status} (not a release-safety conclusion)",
        f"Snapshot  {report.snapshot_id}",
        (
            f"Workflow attempts {report.calls} / {report.limits.max_calls}; "
            "see the run accounting for network usage"
            if live
            else f"Simulated calls {report.calls} / {report.limits.max_calls}; network model calls 0"
        ),
        f"Files     included {len(report.included_paths)}; "
        f"context supplied {len(report.supplied_paths)}; "
        + ("model-reported checks " if live else "simulated self-reported checks ")
        + f"{len(report.reported_reviewed_paths)}; not fully covered {len(report.unreviewed_paths)}",
        f"Tasks     {len(report.tasks)}; candidates {len(report.findings)}",
        "",
    ]
    for item in report.findings:
        f = item.finding
        verdict = item.verification.verdict if item.verification else "not independently verified"
        lines += [
            f"[{f.severity}] {f.title}",
            f"  Source: {item.role} / {f.task_id}; " + ("verdict: " if live else "simulated verdict: ") + verdict,
            f"  Cause: {f.root_cause}",
            f"  Trigger: {f.trigger}",
            f"  Impact: {f.impact}",
        ]
        for ref in f.evidence:
            lines.append(
                f"  Evidence: {ref.path}:{ref.start_line}-{ref.end_line} SHA256={ref.content_sha256}"
            )
        if item.verification:
            lines.append(f"  Verification: {item.verification.rationale}")
            for ref in item.verification.evidence:
                lines.append(f"  Verification evidence: {ref.path}:{ref.start_line}-{ref.end_line}")
        if item.error:
            lines.append(f"  Verification incomplete: {item.error}")
        lines.append("")
    if not report.findings:
        lines.append(
            "No candidate findings; this does not mean the code has no defects."
            if live
            else "No demonstration candidates; this does not mean the code has no defects."
        )
    for outcome in report.tasks:
        lines.append(
            f"Task {outcome.task.task_id}: {outcome.status}"
            + (f" ({outcome.error})" if outcome.error else "")
        )
    for path, reason in report.omissions:
        lines.append(f"Context omitted: {path} ({reason})")
    for path in report.unreviewed_paths:
        lines.append(f"Not fully covered: {path}")
    for error in report.errors:
        lines.append(f"Workflow error: {error}")
    for warning in report.inventory_warnings:
        lines.append(f"Inventory note: {warning}")
    lines.append(
        "Target tests were not run and no files were modified. "
        "A static model verdict is not a dynamic reproduction."
        if live
        else "Target tests were not run and no files were modified. "
        "Simulated results cannot be used to assess accuracy or repair effectiveness."
    )
    text = "\n".join(safe_text(line) for line in lines) + "\n"
    if fmt == "markdown":
        title = (
            "# ReviewFlow live development review report"
            if live
            else "# ReviewFlow simulated workflow report"
        )
        return title + "\n\n<pre>" + html.escape(text) + "</pre>\n"
    return text

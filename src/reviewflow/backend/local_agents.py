"""Same review chain as the course workflow, reading and staging a local directory."""

import json
import sys
from dataclasses import replace
from pathlib import Path

from reviewflow.backend.repair import RepairError
from reviewflow.backend.responses_adapter import build_body
from reviewflow.backend.transport import TransportError
from reviewflow.models import ScanPolicy
from reviewflow.review.context import MAX_EXCERPT_BYTES, MAX_EXCERPT_LINES, ReviewContext
from reviewflow.review.workflow import run_review
from reviewflow.review.workflow_models import ModelRequest, RunLimits
from reviewflow.snapshot import build_inventory, compute_snapshot_id
from reviewflow.sources.git import open_source

_LIMITS = RunLimits(max_calls=100, max_context_files=20, max_context_bytes=1_048_576)
_POLICY = ScanPolicy(10000, 262144, 64 * 1024 * 1024)


def _note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _step_label(stage: str, payload: dict, secret: str) -> str:
    title = ""
    finding = payload.get("finding")
    if isinstance(finding, dict):
        title = str(finding.get("title") or "")
    if secret and secret in title:
        title = ""
    title = " ".join(title.split())[:40]
    if stage == "reviewer":
        role = payload.get("task", {}).get("role")
        return {"correctness": "correctness review", "security": "security review"}.get(role, "review")
    if stage == "verifier":
        return f"verify {title}" if title else "verify"
    if stage == "patch":
        return f"propose edit {title}" if title else "propose edit"
    return {"planner": "plan"}.get(stage, stage)


class _AgentBackend:
    """One model adapter for planner, both reviewers, verifier and patch."""

    def __init__(self, model, key: str):
        self.model = model
        self.key = key
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.batch = 0
        self.total = 0

    def complete(self, request):
        from reviewflow.backend.local_fix import _worker_call

        if isinstance(request, ModelRequest):
            stage, system, payload, schema = (
                request.stage,
                request.system,
                json.loads(request.payload),
                request.response_schema,
            )
        else:
            stage = request["stage"]
            system = request["system"]
            schema = request["response_schema"]
            payload = {
                key: value
                for key, value in request.items()
                if key not in {"stage", "system", "response_schema"}
            }
        label = _step_label(stage, payload, self.key)
        prefix = f"[{self.batch}/{self.total}]" if self.total else "[-]"
        _note(f"{prefix} {label}, waiting for the model")
        wire = json.dumps(
            {"stage": stage, "system": system, "payload": payload, "response_schema": schema},
            ensure_ascii=True,
        )
        try:
            body = build_body(self.model, wire, 8192)
            answer = _worker_call(self.model.base_url, self.key, body, 8192, 300)
        except Exception as exc:
            _note(f"{prefix} {label} failed")
            raise TransportError("responses_request_rejected") from exc
        text = answer["text"]
        if self.key in text:
            _note(f"{prefix} {label} failed")
            raise TransportError("response_text_invalid_or_unsafe")
        self.calls += 1
        self.input_tokens += answer["usage"]["input_tokens"]
        self.output_tokens += answer["usage"]["output_tokens"]
        _note(f"{prefix} {label} done")
        return text


def run_local_agents(root: Path, model, key: str, names: list[str]) -> dict:
    source = open_source(root, "directory")
    inventory = build_inventory(source, _POLICY)
    included = {
        record.path: record
        for record in inventory.snapshot.records
        if record.status == "included"
    }
    pending: dict[str, str] = {}
    summaries: list[str] = []
    items: list[dict] = []
    failures: list[dict[str, str]] = []
    reviewed: list[str] = []
    backend = _AgentBackend(model, key)
    batches = 0
    for name in names:
        if name not in included:
            failures.append({"files": name, "error": "file was not included in the local snapshot"})
    chunks, skipped = _chunks(root, [included[name] for name in names if name in included])
    failures.extend(skipped)
    backend.total = len(chunks)
    _note(
        f"Preparing to review {sum(len(chunk) for chunk in chunks)} files "
        f"in {len(chunks)} batches."
    )
    if skipped:
        _note(f"{len(skipped)} files were not included in this review.")
    for chunk in chunks:
        batches += 1
        backend.batch = batches
        paths = [record.path for record in chunk]
        reviewed.extend(paths)
        _note(f"[{batches}/{len(chunks)}] start, {len(paths)} files in this batch")
        narrowed = _select(inventory, set(paths))
        report = run_review(source, narrowed, backend, mode="multi", limits=_LIMITS)
        context = ReviewContext(source, narrowed)
        tasks = {item.task.task_id: item.task for item in report.tasks}
        for error in report.errors:
            failures.append({"files": ",".join(paths), "error": error})
        for path, reason in report.omissions:
            failures.append({"files": path, "error": reason})
        for outcome in report.findings:
            if outcome.error or outcome.verification is None or outcome.verification.verdict != "accepted":
                continue
            task = tasks.get(outcome.finding.task_id)
            if task is None:
                failures.append({"files": outcome.finding.finding_id, "error": "finding task missing"})
                continue
            try:
                checked = propose_patch(context, task, outcome.finding, backend)
            except RepairError as exc:
                failures.append({"files": outcome.finding.finding_id, "error": str(exc)})
                continue
            accepted = []
            for replacement in checked.proposal.replacements:
                if replacement.path in pending:
                    failures.append(
                        {"files": replacement.path, "error": "another accepted patch already targets this file"}
                    )
                    continue
                pending[replacement.path] = replacement.content
                accepted.append({"path": replacement.path, "content": replacement.content})
            if not accepted:
                continue
            items.append(
                {
                    "id": len(items) + 1,
                    "title": outcome.finding.title,
                    "explanation": checked.proposal.explanation,
                    "files": accepted,
                }
            )
            summaries.append(f"{outcome.finding.title}: {checked.proposal.explanation}")
            shown = " ".join(str(outcome.finding.title).split())
            if backend.key and backend.key in shown:
                shown = "hidden"
            _note(f"Pending edit {len(items)}: {shown[:40]}")
    return {
        "pending": pending,
        "summaries": summaries,
        "items": items,
        "failures": failures,
        "reviewed": reviewed,
        "batches": batches,
        "calls": backend.calls,
        "input_tokens": backend.input_tokens,
        "output_tokens": backend.output_tokens,
    }


def _chunks(root: Path, records):
    failures = []
    chunks = []
    current = []
    used = 0
    for record in records:
        try:
            text = _text(root, record.path)
        except UnicodeError:
            failures.append({"files": record.path, "error": "file is not UTF-8"})
            continue
        lines = text.count("\n") + (not text.endswith("\n"))
        if record.size_bytes > MAX_EXCERPT_BYTES or lines > MAX_EXCERPT_LINES:
            failures.append({"files": record.path, "error": "file exceeds one review excerpt"})
            continue
        if current and (len(current) >= 20 or used + record.size_bytes > _LIMITS.max_context_bytes):
            chunks.append(current)
            current, used = [], 0
        current.append(record)
        used += record.size_bytes
    if current:
        chunks.append(current)
    return chunks, failures


def _text(root: Path, relative: str) -> str:
    return (root.joinpath(*relative.split("/"))).read_text(encoding="utf-8")


def _select(inventory, selected: set[str]):
    available = {record.path for record in inventory.snapshot.records if record.status == "included"}
    if not selected <= available:
        raise ValueError("Invalid selected scope")
    records = tuple(
        replace(record, status="skipped", reason="not_selected_for_upload")
        if record.status == "included" and record.path not in selected
        else record
        for record in inventory.snapshot.records
    )
    snapshot = inventory.snapshot
    return replace(
        inventory,
        snapshot=replace(
            snapshot,
            records=records,
            snapshot_id=compute_snapshot_id(snapshot.source, snapshot.policy, records),
        ),
    )


def propose_patch(context, task, finding, backend):
    from reviewflow.backend.patch_agent import propose_patch as _propose

    return _propose(context, task, finding, backend)

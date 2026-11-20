"""Exclusive, source-bound simulated review sessions. Disk state is never authority.

Reopen validates the selected root/snapshot and all evidence. Resume starts a new
bounded simulated review with fresh grants; it does not deserialize credentials,
execution receipts, live billing state, Python objects or an active repair workspace.
"""

import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.config import Money, parse_config, read_bounded
from reviewflow.backend.gateway import WorkflowGateway
from reviewflow.backend.transport import GuardedTransport, Reply, Usage
from reviewflow.errors import CollectionError
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import Digest, EvidenceRef, Identifier, ReviewMessage
from reviewflow.review.interactive import DuplicateGroup, SessionDemoBackend, run_interactive_review
from reviewflow.review.workflow_models import ModelRequest, RunLimits, RunReport


class RetrievalEvent(ReviewMessage):
    stage: Literal["reviewer", "verifier"]
    task_id: Identifier
    round: Annotated[int, Field(ge=1, le=2)]
    evidence: Annotated[tuple[EvidenceRef, ...], Field(max_length=300)]


class SimulatedAccounting(ReviewMessage):
    calls: Annotated[int, Field(ge=0, le=100)]
    charged_or_reserved_usd: Money
    notice: Literal["Simulated byte counts and prices; not actual tokens or money."] = (
        "Simulated byte counts and prices; not actual tokens or money."
    )


class SessionDocument(ReviewMessage):
    version: Literal[1] = 1
    revision: Annotated[int, Field(ge=1, le=100)]
    root: Annotated[str, Field(min_length=1, max_length=8192)]
    source_mode: Literal["head", "directory"]
    snapshot_id: Digest
    report: RunReport
    retrievals: Annotated[tuple[RetrievalEvent, ...], Field(max_length=100)]
    duplicate_groups: Annotated[tuple[DuplicateGroup, ...], Field(max_length=20)]
    accounting: SimulatedAccounting
    network_calls: Literal[0] = 0
    commit_ready: Literal[False] = False
    notice: Literal["Untrusted saved report, not permission or validation evidence."] = (
        "Untrusted saved report, not permission or validation evidence."
    )

    @field_validator("commit_ready", "network_calls", mode="before")
    @classmethod
    def strict_flags(cls, value, info):
        if type(value) is not (bool if info.field_name == "commit_ready" else int):
            raise ValueError("Invalid flags")
        return value

    @model_validator(mode="after")
    def simulated_only(self) -> Self:
        if (
            self.report.execution != "simulated"
            or self.report.semantic_review_performed
            or self.report.target_tests_executed
            or self.report.snapshot_id != self.snapshot_id
            or self.report.mode not in {"single", "multi", "multi-no-verifier"}
            or self.report.status not in {"complete", "partial", "failed"}
        ):
            raise ValueError("Invalid saved simulated report")
        return self


def _location(folder, source, *, new):
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError("Session symlink rejected")
    path = folder.parent.resolve(strict=True) / folder.name
    root = source.info.root.resolve()
    if path == root or root in path.parents:
        raise ValueError("Session storage must be outside target")
    if new and os.path.lexists(path):
        raise ValueError("New session destination required")
    if not new and not path.is_dir():
        raise ValueError("Session directory required")
    return path


def _run(source, inventory, *, mode, limits, revision):
    paths = [r.path for r in inventory.snapshot.records if r.status == "included"]
    if not paths:
        raise ValueError("No included source files")
    gate = ApprovalGate()
    fixture = SessionDemoBackend()
    config = parse_config(
        {
            "model": {
                "provider": "offline-fixture",
                "api_type": "in-process",
                "model": "fixture",
                "base_url": "https://offline.example.invalid",
                "api_key_file": "secrets.local.toml",
            },
            "budget": {
                "max_run_cost_usd": "200",
                "max_call_cost_usd": "2",
                "input_price_per_million_usd": "1",
                "output_price_per_million_usd": "1",
                "max_input_tokens": 1000000,
                "max_output_tokens": 100000,
                "timeout_seconds": 30,
                "max_retries": 0,
                "max_calls": limits.max_calls,
            },
            "privacy": {
                "allowed_project_root": str(source.info.root.resolve()),
                "allowed_paths": paths,
                "allow_code_upload": True,
                "allow_telemetry": False,
            },
        }
    )

    def adapter(wire, **kwargs):
        data = json.loads(wire)
        request = ModelRequest(
            data["stage"], data["system"], json.dumps(data["payload"]), data["response_schema"]
        )
        response = fixture.complete(request)
        return Reply(response, Usage(len(wire.encode()), len(response.encode())))

    transport = GuardedTransport(config, adapter, gate)
    gateway = WorkflowGateway(
        source,
        inventory,
        transport,
        lambda wire: len(wire.encode()),
        lambda intent: gate.approve(intent, confirmed=True),
    )
    # Internal grants apply ONLY to the hardcoded in-process fixture; never real sends.
    result = run_interactive_review(source, inventory, gateway, mode=mode, limits=limits)
    return SessionDocument(
        revision=revision,
        root=str(source.info.root.resolve()),
        source_mode=source.info.mode,
        snapshot_id=inventory.snapshot.snapshot_id,
        report=result.report,
        retrievals=tuple(
            RetrievalEvent.model_validate_json(json.dumps(e)) for e in result.retrievals
        ),
        duplicate_groups=result.duplicate_groups,
        accounting=SimulatedAccounting(
            calls=transport.calls, charged_or_reserved_usd=str(transport.spent)
        ),
    )


def _save(folder, document):
    data = document.model_dump_json(indent=2).encode()
    if len(data) > 2097152:
        raise ValueError("Session report too large")
    path = folder / f"run-{document.revision:04d}.json"
    from reviewflow.backend.nofollow import open_exclusive

    fd = open_exclusive(path)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def start_session(folder, source, inventory, *, mode="multi", limits=None):
    path = _location(folder, source, new=True)
    doc = _run(source, inventory, mode=mode, limits=limits or RunLimits(), revision=1)
    path.mkdir(mode=0o700)
    _save(path, doc)
    return doc


def load_session(folder, source, inventory):
    try:
        path = _location(folder, source, new=False)
        names = sorted(p.name for p in path.glob("run-*.json"))
        if (
            not names
            or len(names) > 100
            or any(not re.fullmatch(r"run-\d{4}\.json", n) for n in names)
        ):
            raise ValueError()
        if names != [f"run-{i:04d}.json" for i in range(1, len(names) + 1)]:
            raise ValueError()
        doc = SessionDocument.model_validate_json(read_bounded(path / names[-1], 2097152))
        if (
            doc.revision != len(names)
            or doc.root != str(source.info.root.resolve())
            or doc.source_mode != source.info.mode
            or doc.snapshot_id != inventory.snapshot.snapshot_id
        ):
            raise ValueError()
        context = ReviewContext(source, inventory)
        # Validate all actual current bytes, not only paths that happen to be cited.
        for record in inventory.snapshot.records:
            if record.status == "included":
                context.read_file(record.path)
        tasks = {t.task.task_id: t.task for t in doc.report.tasks}
        for task in tasks.values():
            context.validate_task(task)
        for outcome in doc.report.findings:
            task = tasks[outcome.finding.task_id]
            context.validate_finding(task, outcome.finding)
            if outcome.verification:
                context.validate_verification(task, outcome.finding, outcome.verification)
        for event in doc.retrievals:
            for ref in event.evidence:
                context._validate_evidence(ref)
        return doc
    except (ValueError, OSError, KeyError, CollectionError):
        raise ValueError("Session rejected: schema, source or evidence mismatch") from None


def resume_session(folder, source, inventory):
    old = load_session(folder, source, inventory)
    if old.revision >= 100:
        raise ValueError("Session revision limit")
    doc = _run(
        source, inventory, mode=old.report.mode, limits=old.report.limits, revision=old.revision + 1
    )
    _save(_location(folder, source, new=False), doc)
    return doc


def export_finding(folder, source, inventory, finding_id, destination):
    doc = load_session(folder, source, inventory)
    matches = [o.finding for o in doc.report.findings if o.finding.finding_id == finding_id]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous finding")
    finding = matches[0]
    tasks = [t.task for t in doc.report.tasks if t.task.task_id == finding.task_id]
    if len(tasks) != 1:
        raise ValueError("Unknown or ambiguous task")
    path = _location(destination, source, new=True)
    path.mkdir(mode=0o700)
    for name, value in [("task.json", tasks[0]), ("finding.json", finding)]:
        from reviewflow.backend.nofollow import open_exclusive

        fd = open_exclusive(path / name)
        with os.fdopen(fd, "w") as stream:
            stream.write(value.model_dump_json(indent=2))
    return {
        "directory": str(path),
        "commit_ready": False,
        "notice": "Exported simulated candidate, not verified defect or repair approval.",
    }

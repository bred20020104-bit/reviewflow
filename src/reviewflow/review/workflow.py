"""Offline LangGraph orchestration. No live transport or target execution."""

import json
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.diagnostics import safe_error_code
from reviewflow.backend.transport import TransportError
from reviewflow.errors import CollectionError
from reviewflow.models import Inventory, SourceReader
from reviewflow.review.context import MAX_EXCERPT_BYTES, MAX_EXCERPT_LINES, ReviewContext
from reviewflow.review.contracts import EvidenceRef, ReviewTask, VerificationResult
from reviewflow.review.evidence_errors import evidence_error_code
from reviewflow.review.workflow_models import (
    CodePacket,
    FindingOutcome,
    ModelRequest,
    Plan,
    ReviewBackend,
    ReviewOutput,
    RunLimits,
    RunReport,
    TaskOutcome,
)

MAX_RESPONSE_BYTES = 131072


class WorkflowFailure(Exception):
    """Only fixed diagnostic codes cross this boundary."""


class GraphState(TypedDict):
    phase: str
    report: RunReport | None


class _Run:
    def __init__(self, source, inventory, backend, mode, limits, *, profiles=None):
        self.profiles = load_profiles() if profiles is None else profiles
        self.inventory = inventory
        self.context = ReviewContext(source, inventory)
        self.backend = backend
        self.mode = mode
        self.limits = limits
        self.calls = 0
        self.packets: dict[str, CodePacket] = {}
        self.omissions = []
        self.errors = []
        self.tasks = ()
        self.outcomes = []
        self.findings = []

    def call(self, stage, payload, schema, *, response_schema=None):
        if self.calls >= self.limits.max_calls:
            raise WorkflowFailure("call_budget")
        if response_schema is None:
            response_schema = schema.model_json_schema()
        if schema is Plan:
            response_schema["properties"]["tasks"]["maxItems"] = self.limits.max_tasks
        request = ModelRequest(
            stage,
            self.profiles.system_for(stage, payload.get("task", {}).get("role")),
            json.dumps(payload, ensure_ascii=True),
            response_schema,
        )
        self.calls += 1
        try:
            response = self.backend.complete(request)
        except TimeoutError:
            raise WorkflowFailure("timeout") from None
        except TransportError as exc:
            raise WorkflowFailure(safe_error_code(exc)) from None
        except Exception:
            raise WorkflowFailure("backend_error") from None
        if not isinstance(response, str):
            raise WorkflowFailure("invalid_output")
        try:
            if len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise WorkflowFailure("output_limit")
            return schema.model_validate_json(response)
        except (ValidationError, ValueError, RecursionError):
            raise WorkflowFailure("invalid_output") from None

    def prepare(self):
        used = 0
        for record in self.inventory.snapshot.records:
            if record.status != "included":
                continue
            if record.size_bytes == 0:
                self.omissions.append((record.path, "empty_file"))
                continue
            if record.size_bytes > MAX_EXCERPT_BYTES:
                self.omissions.append((record.path, "file_context_limit"))
                continue
            if (
                len(self.packets) >= self.limits.max_context_files
                or used + record.size_bytes > self.limits.max_context_bytes
            ):
                self.omissions.append((record.path, "context_budget"))
                continue
            try:
                text = self.context.read_file(record.path)
            except CollectionError:
                self.errors.append("prepare: source_changed")
                self.packets.clear()
                return
            lines = text.count("\n") + (not text.endswith("\n"))
            if lines > MAX_EXCERPT_LINES:
                self.omissions.append((record.path, "file_context_limit"))
                continue
            ref = EvidenceRef(
                snapshot_id=self.inventory.snapshot.snapshot_id,
                path=record.path,
                content_sha256=record.sha256,
                start_line=1,
                end_line=lines,
            )
            self.packets[record.path] = CodePacket(ref, text)
            used += record.size_bytes
        if not self.packets:
            self.errors.append("prepare: no_context")

    def packet_data(self, paths):
        # Check again before every request; never silently use changed directory content.
        result = []
        for path in paths:
            packet = self.packets[path]
            if self.context.read_file(path) != packet.text:
                raise CollectionError("Context changed.")
            result.append(
                {"evidence": packet.evidence.model_dump(mode="json"), "text": packet.text}
            )
        return result

    def plan(self):
        if not self.packets:
            return
        snapshot = self.inventory.snapshot.snapshot_id
        if self.mode == "single":
            self.tasks = (
                ReviewTask(
                    task_id="baseline",
                    snapshot_id=snapshot,
                    role="general",
                    paths=tuple(self.packets),
                    objective="Review correctness and security independently of other agents.",
                ),
            )
            return
        try:
            plan = self.call(
                "planner",
                {
                    "snapshot_id": snapshot,
                    "max_tasks": self.limits.max_tasks,
                    "files": [p.evidence.model_dump(mode="json") for p in self.packets.values()],
                },
                Plan,
            )
            if len(plan.tasks) > self.limits.max_tasks:
                raise WorkflowFailure("task_budget")
            if {t.role for t in plan.tasks} != {"correctness", "security"}:
                raise WorkflowFailure("invalid_plan")
            for task in plan.tasks:
                self.context.validate_task(task)
                if not set(task.paths) <= self.packets.keys():
                    raise WorkflowFailure("invalid_plan")
            self.tasks = plan.tasks
        except (WorkflowFailure, CollectionError) as exc:
            self.errors.append(
                "planner: " + (str(exc) if isinstance(exc, WorkflowFailure) else evidence_error_code(exc))
            )

    def review(self):
        known_ids = set()
        for task in self.tasks:
            try:
                output = self.call(
                    "reviewer",
                    {"task": task.model_dump(mode="json"), "files": self.packet_data(task.paths)},
                    ReviewOutput,
                )
                if not set(output.reviewed_paths) <= set(task.paths):
                    raise WorkflowFailure("invalid_coverage")
                if len(self.findings) + len(output.findings) > self.limits.max_findings:
                    raise WorkflowFailure("finding_budget")
                for finding in output.findings:
                    self.context.validate_finding(task, finding)
                    if finding.finding_id in known_ids:
                        raise WorkflowFailure("duplicate_finding_id")
                    if not {r.path for r in finding.evidence} <= set(output.reviewed_paths):
                        raise WorkflowFailure("finding_evidence_not_reviewed")
                known_ids.update(f.finding_id for f in output.findings)
                self.findings.extend(FindingOutcome(f, task.role) for f in output.findings)
                self.outcomes.append(TaskOutcome(task, "completed", output.reviewed_paths))
            except (WorkflowFailure, CollectionError) as exc:
                code = str(exc) if isinstance(exc, WorkflowFailure) else evidence_error_code(exc)
                self.errors.append(f"reviewer/{task.task_id}: {code}")
                self.outcomes.append(TaskOutcome(task, "failed", error=code))

    def verify(self):
        if self.mode in {"single", "multi-no-verifier"}:
            return
        tasks = {t.task_id: t for t in self.tasks}
        for i, outcome in enumerate(self.findings):
            task = tasks[outcome.finding.task_id]
            try:
                result = self.call(
                    "verifier",
                    {
                        "task": task.model_dump(mode="json"),
                        "finding": outcome.finding.model_dump(mode="json"),
                        "files": self.packet_data(self.packets),
                    },
                    VerificationResult,
                )
                if any(ref.path not in self.packets for ref in result.evidence):
                    raise WorkflowFailure("invalid_evidence")
                self.context.validate_verification(task, outcome.finding, result)
                self.findings[i] = replace(outcome, verification=result)
            except (WorkflowFailure, CollectionError) as exc:
                code = str(exc) if isinstance(exc, WorkflowFailure) else evidence_error_code(exc)
                self.errors.append(f"verifier/{outcome.finding.finding_id}: {code}")
                self.findings[i] = replace(outcome, error=code)

    def report(self):
        included = tuple(r.path for r in self.inventory.snapshot.records if r.status == "included")
        reviewed = {p for t in self.outcomes for p in t.reviewed_paths}
        roles = ("general",) if self.mode == "single" else ("correctness", "security")
        complete_paths = set(included)
        for role in roles:
            complete_paths &= {
                p for t in self.outcomes if t.task.role == role for p in t.reviewed_paths
            }
        unreviewed = tuple(p for p in included if p not in complete_paths)
        success = any(t.status == "completed" for t in self.outcomes)
        incomplete = bool(
            self.errors
            or unreviewed
            or self.inventory.coverage.skipped_files
            or self.inventory.coverage.excluded_directories
            or any(f.verification and f.verification.verdict == "unresolved" for f in self.findings)
        )
        status = "failed" if not success else "partial" if incomplete else "complete"
        return RunReport(
            1,
            self.inventory.snapshot.snapshot_id,
            self.mode,
            "simulated",
            status,
            self.calls,
            self.limits,
            included,
            tuple(self.packets),
            tuple(sorted(reviewed)),
            unreviewed,
            tuple(self.omissions),
            tuple(self.outcomes),
            tuple(self.findings),
            tuple(self.errors),
            self.inventory.snapshot.warnings,
        )


def run_review(
    source: SourceReader,
    inventory: Inventory,
    backend: ReviewBackend,
    *,
    mode: str = "multi",
    limits: RunLimits | None = None,
) -> RunReport:
    """Run an explicitly simulated workflow with a trusted injected offline backend.

    Not an API for authorizing live transports; no supplied credentials are resolved.
    """
    if mode not in ("single", "multi", "multi-no-verifier"):
        raise ValueError("Invalid review mode")
    limits = RunLimits.model_validate(limits or RunLimits())
    run = _Run(source, inventory, backend, mode, limits)
    with tracing_context(enabled=False):
        graph = StateGraph(GraphState)

        def node(method, name):
            def execute(state):
                method()
                return {"phase": name}

            return execute

        for name in ("prepare", "plan", "review", "verify"):
            graph.add_node(name, node(getattr(run, name), name))
        graph.add_node("report", lambda state: {"phase": "report", "report": run.report()})
        for left, right in zip(
            (START, "prepare", "plan", "review", "verify"),
            ("prepare", "plan", "review", "verify", "report"),
        ):
            graph.add_edge(left, right)
        graph.add_edge("report", END)
        result = graph.compile().invoke(
            {"phase": "start", "report": None}, config={"recursion_limit": 10, "callbacks": []}
        )
        return result["report"]

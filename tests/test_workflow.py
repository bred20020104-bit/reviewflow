import json
import socket

import pytest

from reviewflow.models import ScanPolicy
from reviewflow.review.context import MAX_EXCERPT_LINES
from reviewflow.review.workflow import run_review
from reviewflow.review.workflow_models import RunLimits
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


class SyntheticBackend:
    def __init__(self, mutate=None):
        self.requests = []
        self.mutate = mutate

    def complete(self, request):
        self.requests.append(request)
        payload = json.loads(request.payload)
        if request.stage == "planner":
            result = {
                "tasks": [
                    {
                        "task_id": role,
                        "snapshot_id": payload["snapshot_id"],
                        "role": role,
                        "paths": [p["path"] for p in payload["files"]],
                        "objective": "Synthetic test",
                    }
                    for role in ("correctness", "security")
                ]
            }
        elif request.stage == "reviewer":
            task = payload["task"]
            ref = payload["files"][0]["evidence"]
            result = {
                "reviewed_paths": task["paths"],
                "findings": [
                    {
                        "finding_id": task["task_id"] + "-f",
                        "task_id": task["task_id"],
                        "title": "Synthetic candidate",
                        "root_cause": "Synthetic",
                        "trigger": "Synthetic",
                        "impact": "Synthetic",
                        "severity": "low",
                        "evidence": [ref],
                    }
                ],
            }
        else:
            result = {
                "finding_id": payload["finding"]["finding_id"],
                "verdict": "accepted",
                "rationale": "Synthetic verification",
                "evidence": payload["finding"]["evidence"],
            }
        if self.mutate:
            result = self.mutate(request.stage, result)
        return result if isinstance(result, str) else json.dumps(result)


def fixture_run(tmp_path, backend=None, **kwargs):
    (tmp_path / "a.py").write_text("answer = 42\n")
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    backend = backend or SyntheticBackend()
    return run_review(source, inventory, backend, **kwargs), backend


def test_multi_valid_flow_is_simulated_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "placeholder-not-a-key")
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network attempted"))
    report, backend = fixture_run(tmp_path)
    assert report.status == "complete"
    assert report.calls == 5
    assert report.execution == "simulated"
    assert report.semantic_review_performed is False
    assert len(report.findings) == 2
    assert all(f.verification.verdict == "accepted" for f in report.findings)
    assert report.unreviewed_paths == ()
    reviews = [json.loads(r.payload) for r in backend.requests if r.stage == "reviewer"]
    assert all(set(r) == {"task", "files"} for r in reviews)
    assert [r["task"]["role"] for r in reviews] == ["correctness", "security"]


@pytest.mark.parametrize("task_cap", [2, 4])
def test_planner_receives_runtime_task_budget_in_schema_and_payload(tmp_path, task_cap):
    report, backend = fixture_run(tmp_path, limits=RunLimits(max_tasks=task_cap))
    assert report.status == "complete"
    request = backend.requests[0]
    assert request.stage == "planner"
    assert request.response_schema["properties"]["tasks"]["maxItems"] == task_cap
    assert json.loads(request.payload)["max_tasks"] == task_cap


def test_model_ignoring_task_budget_is_still_rejected_without_retry(tmp_path):
    def mutate(stage, result):
        if stage == "planner":
            result["tasks"].append({**result["tasks"][0], "task_id": "extra"})
        return result

    report, backend = fixture_run(tmp_path, SyntheticBackend(mutate), limits=RunLimits(max_tasks=2))
    assert report.status == "failed"
    assert report.errors == ("planner: task_budget",)
    assert len(backend.requests) == 1


def test_single_baseline_has_no_verifier(tmp_path):
    report, backend = fixture_run(tmp_path, mode="single")
    assert report.calls == 1
    assert [r.stage for r in backend.requests] == ["reviewer"]
    assert report.findings[0].verification is None
    assert report.tasks[0].task.role == "general"


@pytest.mark.parametrize(
    "stage,kind",
    [("planner", "invalid_output"), ("reviewer", "invalid_output"), ("verifier", "invalid_output")],
)
def test_invalid_json_is_visible_not_zero_issue_success(tmp_path, stage, kind):
    backend = SyntheticBackend(lambda s, out: "SECRET not JSON" if s == stage else out)
    report, _ = fixture_run(tmp_path, backend)
    assert report.status != "complete"
    assert any(kind in e for e in report.errors)
    assert "SECRET" not in repr(report)


@pytest.mark.parametrize("failure", [TimeoutError("SECRET"), RuntimeError("SECRET")])
def test_backend_errors_are_sanitized(tmp_path, failure):
    def fail(stage, result):
        raise failure

    report, _ = fixture_run(tmp_path, SyntheticBackend(fail))
    assert report.status == "failed"
    assert "SECRET" not in repr(report)
    assert report.calls == 1


def test_budget_stops_calls_and_preserves_unverified_candidates(tmp_path):
    report, backend = fixture_run(tmp_path, limits=RunLimits(max_calls=3))
    assert report.calls == len(backend.requests) == 3
    assert report.status == "partial"
    assert len(report.findings) == 2
    assert all(f.verification is None and f.error == "call_budget" for f in report.findings)


@pytest.mark.parametrize("corruption", ["scope", "snapshot", "duplicate", "role"])
def test_bad_plan_never_reaches_reviewer(tmp_path, corruption):
    def mutate(stage, result):
        if stage == "planner":
            t = result["tasks"][0]
            if corruption == "scope":
                t["paths"] = [".env"]
            elif corruption == "snapshot":
                t["snapshot_id"] = "0" * 64
            elif corruption == "duplicate":
                result["tasks"][1]["task_id"] = t["task_id"]
            else:
                t["role"] = "general"
        return result

    report, backend = fixture_run(tmp_path, SyntheticBackend(mutate))
    assert report.status == "failed"
    assert len(backend.requests) == 1


@pytest.mark.parametrize("corruption", ["hash", "line", "task", "duplicate", "coverage"])
def test_invalid_candidate_batch_rejected(tmp_path, corruption):
    def mutate(stage, result):
        if stage == "reviewer":
            f = result["findings"][0]
            if corruption == "hash":
                f["evidence"][0]["content_sha256"] = "0" * 64
            elif corruption == "line":
                f["evidence"][0]["end_line"] = 999
            elif corruption == "task":
                f["task_id"] = "wrong"
            elif corruption == "duplicate":
                result["findings"].append(f)
            else:
                result["reviewed_paths"] = ["not-supplied.py"]
        return result

    report, _ = fixture_run(tmp_path, SyntheticBackend(mutate))
    assert report.status == "failed"
    assert not report.findings
    assert report.unreviewed_paths == ("a.py",)


def test_context_omissions_and_reported_coverage(tmp_path):
    (tmp_path / "long.py").write_text("x\n" * (MAX_EXCERPT_LINES + 1))
    (tmp_path / "empty.py").write_text("")
    (tmp_path / ".env").write_text("secret")
    report, _ = fixture_run(tmp_path)
    assert report.status == "partial"
    assert report.unreviewed_paths == ("empty.py", "long.py")
    assert {reason for _, reason in report.omissions} == {"empty_file", "file_context_limit"}
    assert ".env" not in report.included_paths


def test_partial_reviewer_failure_keeps_other_results(tmp_path):
    def mutate(stage, result):
        if stage == "reviewer" and result["findings"][0]["task_id"] == "security":
            return "bad"
        return result

    report, _ = fixture_run(tmp_path, SyntheticBackend(mutate))
    assert report.status == "partial"
    assert len(report.findings) == 1
    assert report.reported_reviewed_paths == ("a.py",)
    assert report.unreviewed_paths == ("a.py",)  # missing security coverage


def test_excessive_response_rejected(tmp_path):
    report, _ = fixture_run(tmp_path, SyntheticBackend(lambda *a: " " * 131073))
    assert report.errors == ("planner: output_limit",)


def test_verifier_counterevidence_allowed(tmp_path):
    (tmp_path / "b.py").write_text("counterexample\n")
    backend = SyntheticBackend()
    original = backend.complete

    def complete(request):
        out = json.loads(original(request))
        if request.stage == "verifier":
            out["verdict"] = "rejected"
            out["evidence"] = [json.loads(request.payload)["files"][-1]["evidence"]]
        return json.dumps(out)

    backend.complete = complete
    report, _ = fixture_run(tmp_path, backend)
    assert all(f.verification.evidence[0].path == "b.py" for f in report.findings)


def test_empty_directory_no_backend_calls(tmp_path):
    source = DirectorySource(tmp_path)
    backend = SyntheticBackend()
    report = run_review(source, build_inventory(source, ScanPolicy()), backend)
    assert report.status == "failed"
    assert report.calls == 0


def test_total_context_and_findings_budget(tmp_path):
    (tmp_path / "b.py").write_text("x\n")
    report, _ = fixture_run(tmp_path, limits=RunLimits(max_context_bytes=12, max_findings=1))
    assert report.status == "partial"
    assert len(report.findings) <= 1
    assert any(reason == "context_budget" for _, reason in report.omissions)

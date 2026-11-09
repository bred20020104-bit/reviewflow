"""Synthetic controller tests only; not real-model, Docker or user-target acceptance."""

import json

import pytest
from test_repair_budget import Adapter, setup_budget

from reviewflow.backend.transport import Reply, Usage
from reviewflow.review.interactive import SessionDemoBackend
from reviewflow.review.workflow_models import ModelRequest


class RepairFixtureAdapter(Adapter):
    def __init__(self, model, behavior="success"):
        super().__init__(model)
        self.behavior, self.patch_calls = behavior, 0

    def __call__(self, wire, **kwargs):
        self.sent.append(wire)
        request = json.loads(wire)
        data, stage = request["payload"], request["stage"]
        text = None
        if self.behavior == "unknown_usage":
            return Reply("{}", None)
        if stage == "reviewer" and "refresh" in data:
            assessments = []
            for old in data["refresh"]["historical_findings"]:
                packet = data["files"][int(old["finding_id"].split("-")[-1])] if self.behavior == "two_files" else data["files"][0]
                assessments.append({"finding_id": old["finding_id"],
                    "status": "present" if packet["text"] == "old\n" else "resolved",
                    "evidence": [packet["evidence"]], "rationale": "Synthetic current-code assessment"})
            text = json.dumps({"assessments": assessments, "new_findings": [],
                               "reviewed_paths": data["task"]["paths"]})
        elif stage == "patch":
            self.patch_calls += 1
            if self.behavior == "invalid_patch" or (self.behavior == "retry" and self.patch_calls == 1):
                text = "invalid json"
            else:
                ref = data["finding"]["evidence"][0]
                text = json.dumps({"snapshot_id": ref["snapshot_id"], "finding_id": data["finding"]["finding_id"],
                    "edits": [{"path": ref["path"], "before_sha256": ref["content_sha256"],
                               "old_text": "old\n", "new_text": "fixed\n"}],
                    "explanation": "Synthetic fixture correction"})
        elif stage == "reviewer" and data["task"]["role"] == "correctness":
            findings = []
            if self.behavior != "no_findings" and (self.behavior == "final_finding" or any(p["text"] == "old\n" for p in data["files"])):
                ref = next(p["evidence"] for p in data["files"] if p["evidence"]["path"] == "a.py")
                findings = [{"finding_id": "synthetic-bug", "task_id": data["task"]["task_id"],
                    "title": "Synthetic wrong value", "root_cause": "Synthetic value is old",
                    "trigger": "Fixture read", "impact": "Synthetic wrong output", "severity": "high",
                    "evidence": [ref]}]
                if self.behavior == "overlap":
                    findings.append({**findings[0], "finding_id": "other-bug", "root_cause": "Separate synthetic defect"})
                elif self.behavior == "two_files":
                    findings = [{**findings[0], "finding_id": "bug-" + str(i), "evidence": [p["evidence"]]}
                                for i, p in enumerate(data["files"]) if p["text"] == "old\n"]
            text = json.dumps({"result": {"findings": findings, "reviewed_paths": data["task"]["paths"]}, "requests": []})
        elif stage == "verifier":
            result = {"finding_id": data["finding"]["finding_id"], "verdict": "accepted",
                      "rationale": "Synthetic fixture verification", "evidence": data["finding"]["evidence"]}
            if data.get("refresh_verification"):
                path = data["finding"]["evidence"][0]["path"]
                packet = next(p for p in data["files"] if p["evidence"]["path"] == path)
                result["verdict"] = "accepted" if packet["text"] == "old\n" else "rejected"
            text = json.dumps({"result": result, "requests": []} if request["response_schema"].get("title") == "VerifierTurn" else result)
        if text is None:
            message = ModelRequest(stage, request["system"], json.dumps(data), request["response_schema"])
            text = SessionDemoBackend().complete(message)
        return Reply(text, Usage(10, 5))


@pytest.mark.parametrize("behavior,status,patches", [
    ("success", "applied", 1), ("no_findings", "no_changes", 0),
    ("retry", "applied", 2), ("invalid_patch", "blocked", 2),
    ("unknown_usage", "blocked", 0), ("final_finding", "blocked", 1),
    ("overlap", "applied", 1),
])
def test_real_controller_composes_all_gates_on_synthetic_fixtures(git_repo, tmp_path, behavior, status, patches):
    from reviewflow.backend.live_repair import run_repair_session

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = RepairFixtureAdapter(config.model, behavior)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == status, result
    assert adapter.patch_calls == patches
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)
    assert result["accounting"]["cost_usd"] is None
    assert result["commit_ready"] is False
    assert (git_repo / "a.py").read_bytes() == (b"fixed\n" if status == "applied" else b"old\n")
    if status == "applied":
        assert result["application"]["applied_files"] == ["a.py"]
        assert "-old\n+fixed\n" in result["candidate_diff"]
        assert result["final_reviews"] and result["test_runs"][-1]["status"] == "passed"
        assert result["final_reviews"][0]["status"] == "partial"
    if behavior == "unknown_usage":
        assert len(adapter.sent) == 1
        assert not result["accounting"]["usage_complete"]


def test_baseline_infrastructure_failure_sends_no_model_requests(git_repo, tmp_path):
    from test_validation_publication import process

    from reviewflow.backend.live_repair import run_repair_session

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    runner._execute = lambda command, **kwargs: process(3 if "run" in command else 0)
    adapter = RepairFixtureAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.sent
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("codes,status,attempts", [
    ([1, 0, 0], "applied", 1), ([1, 1, 1], "blocked", 1),
    ([0, 1, 0, 0], "applied", 2), ([0, 1, 1], "blocked", 2),
    ([0, 3], "blocked", 1),
])
def test_candidate_failures_never_become_success(git_repo, tmp_path, codes, status, attempts):
    from test_validation_publication import process

    from reviewflow.backend.live_repair import run_repair_session

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    outcomes = iter(codes)
    runner._execute = lambda command, **kwargs: process(next(outcomes) if "run" in command else 0)
    adapter = RepairFixtureAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == status, result
    assert adapter.patch_calls == attempts
    assert [r["exit_code"] for r in result["test_runs"]] == codes
    assert (git_repo / "a.py").read_bytes() == (b"fixed\n" if status == "applied" else b"old\n")


def test_later_unchanged_evidence_is_independently_reverified(git_repo, tmp_path):
    from conftest import git

    from reviewflow.backend.live_repair import run_repair_session

    (git_repo / "src/b.py").write_text("old\n")
    git(git_repo, "add", "src/b.py")
    git(git_repo, "commit", "-qm", "Synthetic second defect")
    plan, config, profiles, runner, _, authority = setup_budget(
        git_repo, tmp_path, cap=32, paths=("a.py", "src/b.py"))
    adapter = RepairFixtureAdapter(config.model, "two_files")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert result["application"]["applied_files"] == ["a.py", "src/b.py"]
    rechecks = [json.loads(w) for w in adapter.sent if json.loads(w)["response_schema"].get("title") == "VerificationResult"]
    assert len(rechecks) == 1
    assert rechecks[0]["stage"] == "verifier"
    assert rechecks[0]["payload"]["finding"]["evidence"][0]["path"] == "src/b.py"
    assert (git_repo / "src/b.py").read_bytes() == b"fixed\n"


def test_parent_exhaustion_never_writes_back(git_repo, tmp_path):
    from reviewflow.backend.live_repair import run_repair_session

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=3)
    adapter = RepairFixtureAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.sent) == result["accounting"]["requests_attempted"] == 3
    assert (git_repo / "a.py").read_bytes() == b"old\n"

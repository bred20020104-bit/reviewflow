"""Owned synthetic fixtures; no real-model or target acceptance."""

import json

import pytest
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.transport import Reply, Usage


class RefreshAdapter(RepairFixtureAdapter):
    def __init__(self, model, mode="resolved"):
        super().__init__(model, "overlap")
        self.mode = mode

    def __call__(self, wire, **kwargs):
        request = json.loads(wire)
        data, stage = request["payload"], request["stage"]
        if stage == "reviewer" and "refresh" in data:
            self.sent.append(wire)
            ref = data["files"][0]["evidence"]
            remaining = self.mode in {"remaining", "cycle"}
            assessments = [{"finding_id": old["finding_id"],
                "status": "present" if remaining else "resolved",
                "evidence": [ref], "rationale": "Current fixture inspected"}
                for old in data["refresh"]["historical_findings"]]
            if self.mode == "unresolved" and assessments:
                assessments[0]["status"] = "unresolved"
                assessments[0]["evidence"] = []
            if self.mode == "bad_hash" and assessments:
                assessments[0]["evidence"][0]["content_sha256"] = "0" * 64
            if self.mode == "omitted":
                assessments = []
            new = []
            if self.mode in {"new", "cycle"} and data["task"]["role"] == "security":
                new = [{"finding_id": "new-issue" if self.mode == "new" else f"cycle-{self.patch_calls}", "task_id": data["task"]["task_id"],
                    "title": "New fixture defect", "root_cause": "Intermediate fixture value",
                    "trigger": "Fixture read", "impact": "Fixture failure", "severity": "high",
                    "evidence": [ref]}]
            return Reply(json.dumps({"assessments": assessments, "new_findings": new,
                "reviewed_paths": data["task"]["paths"]}), Usage(10, 5))
        if stage == "verifier" and data.get("refresh_verification"):
            self.sent.append(wire)
            findings = data["findings"] if "findings" in data else [data["finding"]]
            verdicts = [{"finding_id": f["finding_id"], "verdict": (
                "accepted" if self.mode in {"remaining", "cycle", "disagree"}
                or f["finding_id"] == "new-issue" else "rejected"),
                "rationale": "Independent fixture recheck", "evidence": f["evidence"]}
                for f in findings]
            return Reply(json.dumps({"verdicts": verdicts} if "findings" in data else verdicts[0]),
                         Usage(10, 5))
        reply = super().__call__(wire, **kwargs)
        if stage == "patch" and self.mode in {"remaining", "new", "cycle"}:
            proposal = json.loads(reply.text)
            edit = proposal["edits"][0]
            edit["old_text"] = data["files"][0]["text"]
            edit["new_text"] = "part\n" if self.patch_calls == 1 else "fixed\n"
            if self.mode == "cycle":
                edit["new_text"] = "part\n" if edit["old_text"] != "part\n" else "old\n"
            return Reply(json.dumps(proposal), Usage(10, 5))
        return reply


def run_fixture(git_repo, tmp_path, mode="resolved", cap=48):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=cap)
    adapter = RefreshAdapter(config.model, mode)
    return run_repair_session(plan, config, authority, profiles, runner, adapter), adapter


def test_owned_patch_resolves_overlapping_issue_with_independent_current_evidence(git_repo, tmp_path):
    result, adapter = run_fixture(git_repo, tmp_path)
    assert result["status"] == "applied", json.dumps((result.get("failure_stage"), result.get("finding_refreshes")))
    assert (git_repo / "a.py").read_text() == "fixed\n"
    assert len(result["patch_attempts"]) == 1
    refresh, = result["finding_refreshes"]
    assert refresh["status"] == "complete"
    issue, = refresh["issues"]
    assert issue["finding_id"] == "other-bug" and issue["status"] == "resolved"
    assert issue["verification"]["verdict"] == "rejected"
    assert issue["verification"]["evidence"][0]["content_sha256"] != result["initial_reviews"][0]["findings"][1]["finding"]["evidence"][0]["content_sha256"]
    assert result["final_reviews"] and result["application"]
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)


def test_distinct_remaining_issue_uses_new_evidence_before_second_patch(git_repo, tmp_path):
    result, adapter = run_fixture(git_repo, tmp_path, "remaining")
    assert result["status"] == "applied", json.dumps((result.get("failure_stage"), result.get("finding_refreshes")))
    assert len(result["patch_attempts"]) == 2
    assert result["finding_refreshes"][0]["issues"][0]["status"] == "present"
    patches = [json.loads(w)["payload"] for w in adapter.sent if json.loads(w)["stage"] == "patch"]
    assert patches[1]["files"][0]["text"] == "part\n"
    assert patches[0]["finding"]["evidence"][0]["content_sha256"] != patches[1]["finding"]["evidence"][0]["content_sha256"]
    assert (git_repo / "a.py").read_text() == "fixed\n"


@pytest.mark.parametrize("mode", ["unresolved", "bad_hash", "omitted", "disagree"])
def test_uncertain_or_invalid_refresh_never_skips_issue_or_writes_back(git_repo, tmp_path, mode):
    result, _ = run_fixture(git_repo, tmp_path, mode)
    assert result["status"] == "blocked"
    assert result["failure_stage"] == "finding_refresh", result
    assert result["finding_refreshes"][0]["status"] == "failed"
    assert result["finding_refreshes"][0]["diagnostic_code"]
    assert result["finding_refreshes"][0]["patch_sha256"] == result["patch_attempts"][0]["patch_sha256"]
    assert result["finding_refreshes"][0]["pending_findings"][0]["finding_id"] == "other-bug"
    assert len(result["patch_attempts"]) == 1
    assert result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_refresh_can_discover_and_queue_a_new_independently_verified_issue(git_repo, tmp_path):
    result, _ = run_fixture(git_repo, tmp_path, "new")
    assert result["status"] == "applied", json.dumps((result.get("failure_stage"), result.get("finding_refreshes")))
    assert [p["finding_id"] for p in result["patch_attempts"]] == ["synthetic-bug", "new-issue"]
    assert (git_repo / "a.py").read_text() == "fixed\n"


def test_refresh_exhaustion_retains_audit_and_obeys_parent_budget(git_repo, tmp_path):
    result, adapter = run_fixture(git_repo, tmp_path, cap=15)  # Seven repair calls + eight held.
    assert result["status"] == "blocked"
    assert result["failure_stage"] == "finding_refresh", result
    assert result["finding_refreshes"][0]["status"] == "failed"
    assert len(adapter.sent) <= 7
    assert result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_repeated_new_issues_stop_at_refresh_bound_without_writeback(git_repo, tmp_path):
    result, adapter = run_fixture(git_repo, tmp_path, "cycle")
    assert result["status"] == "blocked"
    assert result["finding_refreshes"][-1]["diagnostic_code"] == "refresh_limit_exhausted"
    assert len(result["finding_refreshes"]) == 5
    assert len(result["patch_attempts"]) <= 8 and len(adapter.sent) <= 48
    assert result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_context_growth_cannot_claim_full_refresh_with_omitted_source(git_repo, tmp_path):
    from conftest import git
    (git_repo / "src/b.py").write_text("old\n")
    git(git_repo, "add", "src/b.py")
    git(git_repo, "commit", "-qm", "Fixture second defect")
    plan, config, profiles, runner, _, authority = setup_budget(
        git_repo, tmp_path, cap=48, paths=("a.py", "src/b.py"))

    class Growing(RepairFixtureAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["stage"] == "patch":
                value = json.loads(reply.text)
                value["edits"][0]["new_text"] = "fixed\n" + "#" * 32760
                return Reply(json.dumps(value), Usage(10, 5))
            return reply

    result = run_repair_session(plan, config, authority, profiles, runner,
                               Growing(config.model, "two_files"))
    assert result["status"] == "blocked"
    assert result["finding_refreshes"][0]["diagnostic_code"] == "refresh_context_incomplete"
    assert result["finding_refreshes"][0]["requests_attempted"] == 0
    assert result["application"] is None


def test_refresh_policy_cannot_be_expanded_even_with_new_approval_digest(git_repo, tmp_path):
    from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
    from reviewflow.backend.repair_budget import RepairRunAuthority
    _, _, _, _, preview, _ = setup_budget(git_repo, tmp_path)
    assert "finding_refresh" in preview
    preview["finding_refresh"]["max_rounds"] = 100
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_two_roles_same_issue_are_reassessed_not_deduplicated_by_title(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)

    class TwoRoles(RefreshAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            req = json.loads(wire)
            data = req["payload"]
            if req["stage"] != "reviewer" or "refresh" in data:
                return reply
            value = json.loads(reply.text)
            if data["task"]["role"] == "correctness":
                value["result"]["findings"] = value["result"]["findings"][:1]
            elif any(p["text"] == "old\n" for p in data["files"]):
                value["result"]["findings"] = [{"finding_id": "security-overlap",
                    "task_id": data["task"]["task_id"], "title": "Different security title",
                    "root_cause": "Same synthetic value", "trigger": "Read", "impact": "Wrong value",
                    "severity": "high", "evidence": [data["files"][0]["evidence"]]}]
            return Reply(json.dumps(value), Usage(10, 5))

    result = run_repair_session(plan, config, authority, profiles, runner, TwoRoles(config.model))
    assert result["status"] == "applied", result
    assert len(result["patch_attempts"]) == 1
    issue, = result["finding_refreshes"][0]["issues"]
    assert issue["role"] == "security" and issue["status"] == "resolved"


def test_external_change_during_refresh_stops_before_verification(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)

    class Changed(RefreshAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if "refresh" in json.loads(wire)["payload"]:
                (plan.destination / "a.py").write_text("external change\n")
            return reply

    adapter = Changed(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["application"] is None
    assert not any("refresh_verification" in json.loads(w)["payload"] for w in adapter.sent)
    assert (git_repo / "a.py").read_text() == "old\n"


def test_refresh_markdown_explains_safe_stop_reason(git_repo, tmp_path):
    from reviewflow.backend.live_repair_cli import _markdown
    result, _ = run_fixture(git_repo, tmp_path, "unresolved")
    result.update(preview={"max_calls": 48}, report_directory="fixture")
    assert "refresh_unresolved" in _markdown(result)
    result["finding_refreshes"][0]["diagnostic_code"] = "SECRET unknown error"
    assert "SECRET" not in _markdown(result)

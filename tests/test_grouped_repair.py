"""Grouped scheduling fixtures never constitute real target acceptance."""
import json

import pytest
from test_finding_refresh import RefreshAdapter
from test_repair_budget import setup_budget
from test_validation_publication import process

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.transport import Reply, Usage


def setup_group(repo, tmp_path, cap=48):
    plan, config, profiles, runner, _, _ = setup_budget(repo, tmp_path, cap=cap)
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", cap,
                                  patch_scheduling="grouped")
    return plan, config, profiles, runner, preview, RepairRunAuthority(
        preview, preview["approval_digest"], ApprovalGate())


class GroupAdapter(RefreshAdapter):
    def __init__(self, model, mode="valid"):
        super().__init__(model, "remaining" if mode == "present" else "resolved")
        self.group_mode = mode
        self.groups = []

    def __call__(self, wire, **kwargs):
        data = json.loads(wire)["payload"]
        if not data.get("group_patch"):
            return super().__call__(wire, **kwargs)
        self.sent.append(wire)
        self.groups.append(data)
        self.patch_calls += 1
        packet = data["files"][0]
        patches = [{"task_id": m["task"]["task_id"], "finding_id": m["finding"]["finding_id"],
            "edits": [{"path": packet["evidence"]["path"],
                       "before_sha256": packet["evidence"]["content_sha256"],
                       "old_text": packet["text"], "new_text": "fixed\n"}]}
            for m in data["members"]]
        if self.group_mode == "missing":
            patches.pop()
        elif self.group_mode == "foreign":
            patches[-1]["finding_id"] = "foreign"
        elif self.group_mode == "conflict":
            patches[-1]["edits"][0]["new_text"] = "other\n"
        return Reply(json.dumps({"snapshot_id": data["members"][0]["task"]["snapshot_id"],
                                "patches": patches, "explanation": "Synthetic grouped repair"}),
                     None if self.group_mode == "unknown_usage" else Usage(10, 5))


def test_group_uses_one_request_test_and_refresh_for_two_owned_claims(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", json.dumps(result)
    assert len(adapter.groups) == 1
    assert len(adapter.groups[0]["members"]) == 2
    assert len(result["patch_attempts"]) == 2
    assert len(result["patch_groups"]) == 1
    assert [r["stage"] for r in result["test_runs"]] == ["baseline", "candidate", "final"]
    assert len(result["finding_refreshes"]) == 1
    issues = result["finding_refreshes"][0]["issues"]
    assert len(issues) == 2 and all(i["status"] == "resolved" for i in issues)
    assert result["final_reviews"] and result["accounting"]["final_review_started"]
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)


@pytest.mark.parametrize("mode", ["missing", "foreign", "conflict", "unknown_usage", "present"])
def test_group_never_accepts_invalid_output_unknown_usage_or_unresolved_claims(git_repo, tmp_path, mode):
    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    adapter = GroupAdapter(config.model, mode)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"
    assert len(result["patch_attempts"]) <= 8
    if mode == "unknown_usage":
        assert len(adapter.groups) == 1 and not result["accounting"]["usage_complete"]
    if mode == "present":
        assert all(i["status"] == "present" for i in result["finding_refreshes"][0]["issues"])
    else:
        assert len(result["test_runs"]) == 1 and not result["finding_refreshes"]


def test_group_regression_restores_all_members_before_retry(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    codes = iter([0, 1, 0, 0])
    runner._execute = lambda command, **kw: process(next(codes) if "run" in command else 0)
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", json.dumps(result)
    assert len(adapter.groups) == 2 and len(result["patch_attempts"]) == 4
    assert all(g["files"][0]["text"] == "old\n" for g in adapter.groups)
    assert result["patch_groups"][0]["status"] == "reverted"


def test_grouping_policy_cannot_be_expanded_with_new_digest(git_repo, tmp_path):
    *_, preview, _ = setup_group(git_repo, tmp_path)
    preview["patch_scheduling"]["max_findings"] = 8
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_group_cli_requires_fresh_approval(git_repo, tmp_path, capsys):
    from test_live_repair_cli import cli_fixture

    from reviewflow.quick import main

    args, output, _ = cli_fixture(git_repo, tmp_path)
    assert main(args) == 0
    original = json.loads(capsys.readouterr().out)
    grouped = args + ["--patch-scheduling", "grouped"]
    assert main(grouped) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["patch_scheduling"]["max_findings"] == 4
    assert preview["approval_digest"] != original["approval_digest"]
    assert main(grouped + ["--approve", original["approval_digest"]]) == 2
    assert not output.exists()


def test_grouped_tests_only_never_calls_model_or_writes_source(git_repo, tmp_path):
    plan, config, profiles, runner, _, _ = setup_group(git_repo, tmp_path)
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", 48,
                                  tests_only=True, patch_scheduling="grouped")
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "tests_passed"
    assert not adapter.sent and not result["patch_groups"] and result["application"] is None
    assert preview["patch_scheduling"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_group_cycles_stop_at_eight_member_attempts_not_eight_groups(git_repo, tmp_path):
    class Cycling(GroupAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["payload"].get("group_patch"):
                value = json.loads(reply.text)
                for patch in value["patches"]:
                    edit = patch["edits"][0]
                    edit["new_text"] = "part\n" if edit["old_text"] != "part\n" else "old\n"
                return Reply(json.dumps(value), Usage(10, 5))
            return reply

    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    adapter = Cycling(config.model, "present")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["failure_stage"] == "patch_budget", json.dumps(result)
    assert len(result["patch_attempts"]) == 8 and len(adapter.groups) == 4
    assert len(result["finding_refreshes"]) == 4
    assert result["accounting"]["final_review_calls_held"] == 8
    assert not result["final_reviews"] and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("mode", ["unresolved", "disagree", "bad_hash", "omitted"])
def test_group_refresh_uncertainty_blocks_even_when_candidate_tests_pass(git_repo, tmp_path, mode):
    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    adapter = GroupAdapter(config.model)
    adapter.mode = mode
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["failure_stage"] == "finding_refresh"
    assert result["test_runs"][-1]["status"] == "passed"
    assert len(result["finding_refreshes"][0]["pending_findings"]) == 2
    assert result["application"] is None and not result["final_reviews"]
    assert (git_repo / "a.py").read_text() == "old\n"

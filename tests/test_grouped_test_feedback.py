"""Owned synthetic diagnostics drive bounded retries, never authorize findings."""
import json

import pytest
from conftest import git
from test_grouped_repair import GroupAdapter, setup_group
from test_validation_publication import process

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.transport import Reply


def failure(definition=1, line=2):
    return {"project_index": 0, "file_index": 0, "test_line": definition,
            "line": line, "phase": "call", "category": "assertion_error"}


def setup_observations(repo, tmp_path, runs):
    path = repo / "test_PRIVATE_TEST_SECRET.py"
    path.write_text("def test_old():\n    assert False\n    assert False\n\n"
                    "def test_new():\n    assert False\n")
    git(repo, "add", path.name)
    git(repo, "commit", "-qm", "Synthetic diagnostic definitions")
    plan, config, profiles, runner, preview, authority = setup_group(repo, tmp_path)
    observations = iter(runs)

    def execute(command, **kwargs):
        if "run" not in command:
            return process(0)
        failures, incomplete = next(observations)
        kwargs["stdout"].write(json.dumps({"version": 1, "failures": failures,
            "truncated": False, "incomplete": incomplete}).encode())
        return process(1 if failures else 0)

    runner._execute = execute
    return plan, config, profiles, runner, preview, authority


def test_failed_baseline_new_definition_restores_and_retries_with_bound_feedback(git_repo, tmp_path):
    values = setup_observations(git_repo, tmp_path, [([failure()], False),
        ([failure(), failure(5, 6)], False), ([], False), ([], False)])
    plan, config, profiles, runner, _, authority = values
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert len(adapter.groups) == 2
    assert all(g["files"][0]["text"] == "old\n" for g in adapter.groups)
    first, second = adapter.groups
    assert first["test_feedback"]["current"]["content_digest"] == result["test_runs"][0]["content_digest"]
    feedback = second["test_feedback"]
    assert feedback["current"] == first["test_feedback"]["current"]
    rejected = feedback["rejected_attempt"]
    assert rejected["observation"]["content_digest"] == result["test_runs"][1]["content_digest"]
    assert rejected["patch_sha256"] == result["patch_groups"][0]["patch_sha256"]
    assert rejected["new_failing_definitions"] == 1
    assert rejected["proposal"]["patches"][0]["edits"][0]["new_text"] == "fixed\n"
    assert result["patch_groups"][0]["status"] == "reverted"
    assert "PRIVATE_TEST_SECRET" not in "\n".join(adapter.sent)
    refreshes = [json.loads(w)["payload"] for w in adapter.sent if "refresh" in json.loads(w)["payload"]]
    assert refreshes
    assert all(p["test_observation"]["content_digest"] == result["test_runs"][2]["content_digest"]
               for p in refreshes)
    assert all("rejected_attempt" not in p for p in refreshes)


@pytest.mark.parametrize("failures,incomplete", [([failure(1, 3)], False),
    ([failure(), failure(5, 6)], True)])
def test_old_failure_location_change_or_incomplete_diagnostics_remains_provisional(
        git_repo, tmp_path, failures, incomplete):
    plan, config, profiles, runner, _, authority = setup_observations(git_repo, tmp_path,
        [([failure()], False), (failures, incomplete), ([], False)])
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert len(adapter.groups) == 1
    assert result["patch_groups"][0]["status"] == "provisional"
    assert adapter.groups[0]["test_feedback"]["current"]["status"] == "failed"


def test_repeated_new_definition_failure_stops_without_writeback(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_observations(git_repo, tmp_path,
        [([failure()], False), ([failure(5, 6)], False), ([failure(5, 6)], False)])
    adapter = GroupAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["application"] is None
    assert len(adapter.groups) == 2
    assert all(g["status"] == "reverted" for g in result["patch_groups"])
    assert len(result["patch_attempts"]) == 4 and not result["finding_refreshes"]
    assert (git_repo / "a.py").read_text() == "old\n"


def test_new_failure_is_compared_to_immediately_preceding_retained_candidate(git_repo, tmp_path):
    class Incremental(GroupAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["payload"].get("group_patch"):
                value = json.loads(reply.text)
                for patch in value["patches"]:
                    patch["edits"][0]["new_text"] = "part\n" if self.patch_calls == 1 else "fixed\n"
                return Reply(json.dumps(value), reply.usage)
            return reply

    plan, config, profiles, runner, _, authority = setup_observations(git_repo, tmp_path,
        [([failure(), failure(5, 6)], False), ([failure()], False),
         ([failure(), failure(5, 6)], False), ([failure(), failure(5, 6)], False)])
    adapter = Incremental(config.model, "present")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["application"] is None
    assert [g["status"] for g in result["patch_groups"]] == ["provisional", "reverted", "reverted"]
    assert [g["files"][0]["text"] for g in adapter.groups] == ["old\n", "part\n", "part\n"]
    assert adapter.groups[-1]["test_feedback"]["current"]["content_digest"] == result["test_runs"][1]["content_digest"]
    assert (git_repo / "a.py").read_text() == "old\n"


def test_observation_rejects_other_candidate_digest(git_repo, tmp_path):
    from test_repair_candidate import create, plan_for

    from reviewflow.backend.repair import RepairError
    from reviewflow.backend.test_feedback import current_observation

    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    with pytest.raises(RepairError, match="different candidate content"):
        current_observation(candidate, {"content_digest": "0" * 64})


def test_feedback_policy_cannot_be_disabled_with_fresh_digest(git_repo, tmp_path):
    from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
    from reviewflow.backend.repair_budget import RepairRunAuthority

    *_, preview, _ = setup_group(git_repo, tmp_path)
    preview["patch_scheduling"]["regression_guard"] = "none"
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_test_feedback_does_not_resolve_independent_uncertainty(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_observations(git_repo, tmp_path,
        [([failure()], False), ([], False)])
    adapter = GroupAdapter(config.model)
    adapter.mode = "unresolved"
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["failure_stage"] == "finding_refresh"
    assert result["application"] is None
    refresh = next(json.loads(w)["payload"] for w in adapter.sent if "refresh" in json.loads(w)["payload"])
    assert refresh["test_observation"]["status"] == "passed"
    assert (git_repo / "a.py").read_text() == "old\n"

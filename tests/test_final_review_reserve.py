"""Final-review reservation protects the existing total, never adds calls."""
import json

import pytest
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import Adapter, request, setup_budget
from test_repair_candidate import create

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority
from reviewflow.backend.transport import Reply, TransportError


def test_reserved_calls_are_unavailable_until_final_transition(git_repo, tmp_path):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path, cap=11)
    candidate = create(plan)
    adapter = Adapter(config.model)
    authority.reserve_final_review(candidate)
    assert authority.model_call_limit == 3
    for _ in range(3):
        phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
        phase.gateway.complete(request(phase))
        authority.record_phase(phase)
    with pytest.raises(Denied):
        authority.start_phase(candidate, plan.selected, adapter, profiles)
    assert len(adapter.sent) == 3
    assert authority.accounting()["final_review_calls_held"] == 8
    authority.begin_final_review(candidate)
    assert authority.model_call_limit == 11
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    for _ in range(8):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    with pytest.raises(Denied):
        authority.start_phase(candidate, plan.selected, adapter, profiles)
    assert authority.issued == len(adapter.sent) == 11
    assert authority.accounting()["final_review_started"] is True


def test_active_phase_cannot_spend_held_calls(git_repo, tmp_path):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path, cap=10)
    candidate = create(plan)
    adapter = Adapter(config.model)
    authority.reserve_final_review(candidate)
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    for _ in range(2):
        phase.gateway.complete(request(phase))
    with pytest.raises((Denied, TransportError)):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    assert len(adapter.sent) == 2
    assert authority.accounting()["final_review_calls_held"] == 8


def test_unknown_usage_cannot_release_reserve(git_repo, tmp_path):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path, cap=12)
    candidate = create(plan)
    adapter = Adapter(config.model, Reply("{}", None))
    authority.reserve_final_review(candidate)
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    with pytest.raises(TransportError):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    with pytest.raises(Denied):
        authority.begin_final_review(candidate)
    assert not authority.accounting()["usage_complete"]
    assert authority.accounting()["final_review_calls_held"] == 8


def test_reservation_policy_is_approval_bound_and_not_expandable(git_repo, tmp_path):
    _, _, _, _, preview, _ = setup_budget(git_repo, tmp_path, cap=48)
    assert preview["final_review_budget"] == {
        "version": 1, "calls_per_batch": 8, "activation": "before_first_patch",
        "release": "after_final_tests_pass"}
    preview["final_review_budget"]["calls_per_batch"] = 0
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_insufficient_reserve_stops_before_patch_without_dropping_findings(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=10)
    adapter = RepairFixtureAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert result["failure_stage"] == "final_review_budget"
    assert result["initial_reviews"][0]["findings"]
    assert adapter.patch_calls == 0 and result["patch_attempts"] == []
    assert result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_success_releases_reserve_only_for_final_review(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class Observed(RepairFixtureAdapter):
        def __call__(self, wire, **kwargs):
            data = json.loads(wire)
            if data["stage"] == "patch":
                assert authority.accounting()["final_review_calls_held"] == 8
                assert not authority.accounting()["final_review_started"]
            if data["stage"] == "planner" and self.patch_calls:
                assert authority.accounting()["final_review_calls_held"] == 0
                assert authority.accounting()["final_review_started"]
            return super().__call__(wire, **kwargs)

    result = run_repair_session(plan, config, authority, profiles, runner, Observed(config.model))
    assert result["status"] == "applied", result
    assert result["final_reviews"] and result["test_runs"][-1]["status"] == "passed"
    assert result["accounting"]["final_review_started"] is True


def test_failed_final_tests_never_release_held_budget(git_repo, tmp_path):
    from test_validation_publication import process

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    runner._execute = lambda command, **kwargs: process(1 if "run" in command else 0)
    result = run_repair_session(plan, config, authority, profiles, runner,
                               RepairFixtureAdapter(config.model))
    assert result["failure_stage"] == "final_tests"
    assert result["accounting"]["final_review_calls_held"] == 8
    assert result["accounting"]["final_review_started"] is False
    assert not result["final_reviews"] and result["application"] is None


def test_reserve_scales_with_actual_semantic_batches(git_repo, tmp_path):
    from conftest import git

    (git_repo / "src/b.py").write_text("#" * 32765 + "\n")
    git(git_repo, "add", "src/b.py")
    git(git_repo, "commit", "-qm", "Synthetic second batch")
    plan, _, _, _, _, authority = setup_budget(
        git_repo, tmp_path, cap=48, paths=("a.py", "src/b.py"))
    authority.reserve_final_review(create(plan))
    assert authority.model_call_limit == 32
    assert authority.accounting()["final_review_calls_held"] == 16

"""Explicit call-count exemption only; synthetic fixtures are not target acceptance."""

import json

import pytest
from test_dependency_verification import DependencyAdapter, setup_cross_batch
from test_finding_refresh import RefreshAdapter
from test_grouped_repair import GroupAdapter
from test_live_repair import RepairFixtureAdapter
from test_live_repair_cli import cli_fixture
from test_refresh_reviewer_dependencies import RecoveringReviewer
from test_repair_budget import Adapter, request, setup_budget
from test_repair_candidate import create
from test_validation_publication import process
from test_verification_recovery import EvidenceAdapter

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.transport import Reply, TransportError, Usage
from reviewflow.quick import main


def uncapped(values, tmp_path, *, grouped=False):
    plan, config, profiles, runner, _, _ = values
    preview = make_repair_preview(
        plan, config, profiles, runner, tmp_path.parent / "report", 48,
        no_call_limit=True, patch_scheduling="grouped" if grouped else "serial")
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    return plan, config, profiles, runner, preview, authority


def test_more_than_48_actual_requests_and_final_review_transition(git_repo, tmp_path):
    values = setup_budget(git_repo, tmp_path, cap=48)
    plan, config, profiles, _, preview, authority = uncapped(values, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model)
    authority.reserve_final_review(candidate)
    for _ in range(7):
        phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
        for _ in range(8):
            assert phase.gateway.complete(request(phase)) == "{}"
        authority.record_phase(phase)
    assert preview["max_calls"] is None and preview["no_call_limit"] is True
    assert authority.model_call_limit is None
    assert authority.accounting()["requests_attempted"] == len(adapter.sent) == 56
    assert authority.accounting()["input_tokens_reported"] == 672
    assert authority.accounting()["final_review_calls_held"] == 0
    assert not authority.accounting()["final_review_started"]
    authority.begin_final_review(candidate)
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    assert authority.accounting()["requests_attempted"] == 57
    assert authority.accounting()["final_review_started"]
    assert authority.accounting()["cost_usd"] is None
    with pytest.raises(Denied):
        authority.begin_final_review(candidate)


@pytest.mark.parametrize("kind", ["phase_limit", "unknown_usage", "expired", "no_reservation"])
def test_uncapped_does_not_disable_other_authorization_stops(git_repo, tmp_path, kind):
    values = setup_budget(git_repo, tmp_path, cap=48)
    plan, config, profiles, runner, _, authority = uncapped(values, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model, Reply("{}", None) if kind == "unknown_usage"
                      else Reply("{}", Usage(12, 3)))
    if kind == "no_reservation":
        with pytest.raises(Denied):
            authority.begin_final_review(candidate)
        assert authority.accounting()["requests_attempted"] == 0
        return
    authority.reserve_final_review(candidate)
    if kind == "expired":
        authority._deadline = 0
        with pytest.raises(Denied):
            authority.start_phase(candidate, plan.selected, adapter, profiles)
    else:
        phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
        if kind == "phase_limit":
            for _ in range(8):
                phase.gateway.complete(request(phase))
        with pytest.raises((Denied, TransportError)):
            phase.gateway.complete(request(phase))
        authority.record_phase(phase)
        if kind == "phase_limit":
            # A pre-send phase denial is not a failed/unknown transport; another
            # independently authorized phase remains available, as in bounded mode.
            assert len(adapter.sent) == authority.accounting()["requests_attempted"] == 8
            phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
            phase.gateway.complete(request(phase))
            authority.record_phase(phase)
            assert authority.accounting()["requests_attempted"] == 9
            return
    with pytest.raises(Denied):
        authority.authorize_tests(candidate, runner)
    with pytest.raises(Denied):
        authority.begin_final_review(candidate)
    assert len(adapter.sent) == {"phase_limit": 8, "unknown_usage": 1, "expired": 0}[kind]


@pytest.mark.parametrize("changes", [
    {"max_calls": None}, {"no_call_limit": True},
    {"max_calls": None, "no_call_limit": 1},
    {"max_calls": None, "no_call_limit": "true"},
    {"max_calls": 49}, {"max_calls": True}, {"max_calls": 0},
    {"max_calls": None, "no_call_limit": True, "tests_only": True},
    {"max_calls": "missing", "no_call_limit": True},
])
def test_malformed_budget_pair_rejected_even_with_recomputed_approval(git_repo, tmp_path, changes):
    plan, _, _, _, preview, _ = setup_budget(git_repo, tmp_path, cap=48)
    preview.update(changes)
    if preview.get("max_calls") == "missing":
        del preview["max_calls"]
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    assert not plan.destination.exists()


@pytest.mark.parametrize("kwargs", [
    {"no_call_limit": 1}, {"no_call_limit": "true"},
    {"no_call_limit": True, "tests_only": True},
    {"no_call_limit": True, "seed": object()},
])
def test_preview_rejects_ambiguous_or_incompatible_exemption(git_repo, tmp_path, kwargs):
    plan, config, profiles, runner, _, _ = setup_budget(git_repo, tmp_path, cap=48)
    with pytest.raises(ValueError):
        make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", 48, **kwargs)
    assert not plan.destination.exists()


def test_cli_flag_is_explicit_and_both_directions_need_new_approval(git_repo, tmp_path, capsys):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    args = args[:-2]  # fixture's explicit --max-calls 16; omission must still default to48
    assert main(args) == 0
    bounded = json.loads(capsys.readouterr().out)
    assert bounded["max_calls"] == 48
    assert main(args + ["--no-call-limit"]) == 0
    unlimited = json.loads(capsys.readouterr().out)
    assert unlimited["max_calls"] is None and unlimited["no_call_limit"] is True
    assert unlimited["network_requests"] == 0
    assert "no aggregate model-call limit" in unlimited["notice"]
    assert bounded["approval_digest"] != unlimited["approval_digest"]
    assert main(args + ["--no-call-limit", "--approve", bounded["approval_digest"]]) == 2
    assert main(args + ["--approve", unlimited["approval_digest"]]) == 2
    assert not output.exists() and not output.with_name(output.name + "-candidate").exists()


@pytest.mark.parametrize("flags", [
    ["--no-call-limit", "--max-calls", "48"],
    ["--max-calls", "48", "--no-call-limit"],
    ["--no-call-limit", "--tests-only"],
])
def test_cli_rejects_conflicting_budget_flags_before_output(git_repo, tmp_path, flags):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    assert main(args[:-2] + flags) == 2
    assert not output.exists() and not output.with_name(output.name + "-candidate").exists()


@pytest.mark.parametrize("scenario", ["success", "failed_final_tests", "final_finding", "unknown_usage"])
def test_uncapped_full_session_still_requires_final_tests_and_review(git_repo, tmp_path, scenario):
    values = setup_budget(git_repo, tmp_path, cap=48)
    plan, config, profiles, runner, _, authority = uncapped(values, tmp_path)
    if scenario == "failed_final_tests":
        runner._execute = lambda command, **kwargs: process(1 if "run" in command else 0)
    adapter = RepairFixtureAdapter(config.model, "final_finding" if scenario == "final_finding" else "success")
    if scenario == "unknown_usage":
        adapter = Adapter(config.model, Reply("{}", None))
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    if scenario == "success":
        assert result["status"] == "applied", result
        assert result["final_reviews"] and result["accounting"]["final_review_started"]
        assert result["test_runs"][-1]["status"] == "passed"
        assert (git_repo / "a.py").read_text() == "fixed\n"
    else:
        assert result["status"] == "blocked" and result["application"] is None
        assert (git_repo / "a.py").read_text() == "old\n"
        if scenario == "failed_final_tests":
            assert result["failure_stage"] == "final_tests"
            assert not result["final_reviews"] and not result["accounting"]["final_review_started"]
        if scenario == "final_finding":
            assert result["failure_stage"] == "final_review" and result["final_reviews"]
        if scenario == "unknown_usage":
            assert result["accounting"]["requests_attempted"] == 1
            assert not result["accounting"]["usage_complete"]


@pytest.mark.parametrize("scenario", ["evidence", "refresh", "dependency", "reassessment"])
def test_uncapped_recovery_paths_use_optional_parent_limit(git_repo, tmp_path, scenario):
    values = (setup_cross_batch(git_repo, tmp_path) if scenario in {"dependency", "reassessment"}
              else setup_budget(git_repo, tmp_path, cap=48))
    plan, config, profiles, runner, _, authority = uncapped(values, tmp_path, grouped=scenario == "reassessment")
    adapter = {"evidence": EvidenceAdapter, "refresh": RefreshAdapter,
               "dependency": DependencyAdapter, "reassessment": RecoveringReviewer}[scenario](config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert result["final_reviews"] and result["application"]
    if scenario == "evidence":
        assert result["verification_recoveries"][0]["status"] == "recovered"
    elif scenario == "refresh":
        assert result["finding_refreshes"][0]["status"] == "complete"
    elif scenario == "dependency":
        assert result["dependency_verifications"][0]["requests_attempted"] == 2
    else:
        assert result["finding_refreshes"][0]["reviewer_reassessments"][0]["status"] == "complete"
        assert result["dependency_verifications"][0]["verification"]["verdict"] == "rejected"


def test_uncapped_group_cycle_stops_at_member_bound_not_call_count(git_repo, tmp_path):
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

    values = setup_budget(git_repo, tmp_path, cap=48)
    plan, config, profiles, runner, _, authority = uncapped(values, tmp_path, grouped=True)
    result = run_repair_session(plan, config, authority, profiles, runner, Cycling(config.model, "present"))
    assert result["failure_stage"] == "patch_budget", result
    assert len(result["patch_attempts"]) == 8 and len(result["patch_groups"]) == 4
    assert result["application"] is None and not result["final_reviews"]
    assert (git_repo / "a.py").read_text() == "old\n"


def test_uncapped_cli_report_preserves_unknown_usage_and_safe_http_status(git_repo, tmp_path, capsys, monkeypatch):
    from test_responses_adapter import api_server

    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, secret = cli_fixture(git_repo, tmp_path)
    args = args[:-2] + ["--no-call-limit"]
    original = VolumeDockerRunner.__init__
    monkeypatch.setattr(VolumeDockerRunner, "__init__", lambda self, image, **kw:
                        original(self, image, **kw, execute=lambda *a, **k: process()))
    with api_server(status=422, raw=b"PRIVATE_BODY synthetic-secret") as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert main(args + ["--approve", preview["approval_digest"]]) == 1
        document = json.loads(capsys.readouterr().out)
        assert len(calls) == document["accounting"]["requests_attempted"] == 1
    markdown = (output / "report.md").read_text()
    assert "不设总模型调用次数上限" in markdown
    assert "PRIVATE_BODY" not in markdown and "synthetic-secret" not in markdown
    assert document["accounting"]["records"][0]["error_code"] == "http_status_422"
    assert not document["accounting"]["usage_complete"] and document["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"

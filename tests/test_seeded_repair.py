"""New seed approvals require new tests/reviews, never historical receipts."""
import json

import pytest
from test_candidate_seed import seed_input as seed_input
from test_live_repair import RepairFixtureAdapter
from test_validation_publication import process

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.candidate_seed import SeedPlan
from reviewflow.backend.development import DevelopmentConfig, DevelopmentModel
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.volume_runner import VolumeDockerRunner


def setup_seed(values, codes=(1, 0, 0)):
    plan, _, path, _ = values
    seed = SeedPlan.load(plan, path)
    profiles = load_profiles()
    config = DevelopmentConfig(model=DevelopmentModel(provider="openai", api_type="responses",
        model="fixture", base_url="http://127.0.0.1:9999/v1", api_key_file="secrets.local.toml"),
        privacy={"allowed_project_root": str(plan.root), "allowed_paths": plan.selected})
    results = iter(codes)
    runner = VolumeDockerRunner("sha256:" + "a" * 64,
        execute=lambda command, **kwargs: process(next(results) if "run" in command else 0))
    preview = make_repair_preview(plan, config, profiles, runner, plan.destination.parent / "report",
                                  48, patch_scheduling="grouped", seed=seed)
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    return plan, seed, config, profiles, runner, preview, authority


def test_seed_without_new_findings_still_needs_fresh_baseline_final_tests_and_review(seed_input):
    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input)
    adapter = RepairFixtureAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter, seed=seed)
    assert result["status"] == "applied", result
    assert [t["stage"] for t in result["test_runs"]] == ["baseline", "seed", "final"]
    assert [t["exit_code"] for t in result["test_runs"]] == [1, 0, 0]
    assert result["test_runs"][0]["content_digest"] != result["test_runs"][1]["content_digest"]
    assert len(result["initial_reviews"]) == len(result["final_reviews"]) == 1
    assert not result["patch_attempts"] and not adapter.patch_calls
    assert result["seed"]["validation"] == "unverified_input"
    assert result["cumulative_reported_accounting"]["requests_attempted"] == 12 + result["accounting"]["requests_attempted"]
    assert result["cumulative_reported_accounting"]["patch_attempts"] == 1
    assert (plan.root / "a.py").read_text() == "fixed\n"


@pytest.mark.parametrize("codes", [(1, 1, 1), (1, 0, 1), (1, 3)])
def test_seeded_failed_tests_never_writeback(seed_input, codes):
    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input, codes)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                RepairFixtureAdapter(config.model), seed=seed)
    assert result["status"] == "blocked" and result["application"] is None
    assert not result["final_reviews"]
    assert (plan.root / "a.py").read_text() == "old\n"


def test_seed_approval_does_not_authorize_missing_seed(seed_input):
    plan, _, config, profiles, runner, _, authority = setup_seed(seed_input)
    result = run_repair_session(plan, config, authority, profiles, runner, RepairFixtureAdapter(config.model))
    assert result["status"] == "blocked" and not plan.destination.exists()
    assert result["accounting"]["requests_attempted"] == 0


def test_seed_changed_after_approval_blocks_before_candidate_creation(seed_input):
    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input)
    (seed.candidate_path / "a.py").write_text("changed after approval\n")
    result = run_repair_session(plan, config, authority, profiles, runner,
                                RepairFixtureAdapter(config.model), seed=seed)
    assert result["status"] == "blocked" and not plan.destination.exists()
    assert result["accounting"]["requests_attempted"] == 0


def test_seed_policy_cannot_expand_with_recomputed_approval(seed_input):
    _, _, _, _, _, preview, _ = setup_seed(seed_input)
    preview["seed_policy"]["max_depth"] = 99
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_seed_report_renders_stage_and_report_supplied_accounting(seed_input):
    from reviewflow.backend.live_repair_cli import _markdown

    plan, seed, config, profiles, runner, preview, authority = setup_seed(seed_input)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                RepairFixtureAdapter(config.model), seed=seed)
    result.update(preview=preview, report_directory=str(plan.destination.parent / "report"))
    text = _markdown(result)
    assert "/ seed：" in text
    assert "历史用量由旧报告提供，未经认证" in text
    cumulative = result["cumulative_reported_accounting"]
    assert f"累计请求尝试：{cumulative['requests_attempted']}" in text
    assert "累计补丁成员尝试：1" in text


@pytest.mark.parametrize("verdict", ["accepted", "unresolved"])
def test_seed_final_review_cannot_be_replaced_by_passing_tests(seed_input, verdict):
    from reviewflow.backend.transport import Reply

    class FinalFinding(RepairFixtureAdapter):
        correctness_reviews = 0

        def __call__(self, wire, **kwargs):
            request = json.loads(wire)
            if request["stage"] == "reviewer" and request["payload"]["task"]["role"] == "correctness":
                self.correctness_reviews += 1
                if self.correctness_reviews == 2:
                    self.behavior = "final_finding"
            reply = super().__call__(wire, **kwargs)
            if request["stage"] == "verifier":
                body = json.loads(reply.text)
                body.get("result", body)["verdict"] = verdict
                return Reply(json.dumps(body), reply.usage)
            return reply

    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input)
    result = run_repair_session(plan, config, authority, profiles, runner, FinalFinding(config.model), seed=seed)
    assert result["status"] == "blocked" and result["application"] is None
    assert result["test_runs"][-1]["status"] == "passed"
    assert result["final_reviews"]
    assert (plan.root / "a.py").read_text() == "old\n"


def test_seed_remaining_findings_use_fresh_source_and_grouped_repair(seed_input):
    from test_grouped_repair import GroupAdapter

    from reviewflow.backend.validation import content_digest

    plan, old, path, report = seed_input
    partial = "old\n# synthetic seed progress\n"
    (old.path / "a.py").write_text(partial)
    report["test_runs"][-1]["content_digest"] = content_digest(dict(plan.baseline) | {"a.py": partial.encode()})
    path.write_text(json.dumps(report))

    class Remaining(GroupAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["payload"].get("group_patch"):
                self.behavior = "success"
            return reply

    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input, (1, 1, 0, 0))
    adapter = Remaining(config.model)
    adapter.behavior = "final_finding"
    result = run_repair_session(plan, config, authority, profiles, runner, adapter, seed=seed)
    assert result["status"] == "applied", result
    assert [t["stage"] for t in result["test_runs"]] == ["baseline", "seed", "candidate", "final"]
    assert adapter.groups[0]["files"][0]["text"] == partial
    assert result["finding_refreshes"][0]["status"] == "complete"
    assert result["cumulative_reported_accounting"]["patch_attempts"] == 2
    assert (plan.root / "a.py").read_text() == "fixed\n"


def test_seed_source_changed_during_baseline_never_imported(seed_input):
    plan, seed, config, profiles, runner, _, authority = setup_seed(seed_input)
    execute = runner._execute

    def changed(command, **kwargs):
        result = execute(command, **kwargs)
        if "run" in command:
            (seed.candidate_path / "a.py").write_text("concurrent change\n")
        return result

    runner._execute = changed
    result = run_repair_session(plan, config, authority, profiles, runner,
                                RepairFixtureAdapter(config.model), seed=seed)
    assert result["status"] == "blocked" and result["failure_stage"] == "seed_application"
    assert result["accounting"]["requests_attempted"] == 0
    assert (plan.destination / "a.py").read_text() == "old\n"
    assert (plan.root / "a.py").read_text() == "old\n"


def test_seed_cli_rejects_output_inside_old_candidate_without_mutating_it(seed_input, tmp_path, capsys):
    from test_live_repair_cli import cli_fixture

    from reviewflow.quick import main

    plan, old, report_path, _ = seed_input
    args, _, _ = cli_fixture(plan.root, tmp_path)
    output = old.path / "next-report"
    args[args.index("--output") + 1] = str(output)
    args += ["--seed-report", str(report_path)]
    before = sorted(str(p.relative_to(old.path)) for p in old.path.rglob("*"))
    assert main(args) == 2
    assert not output.exists()
    assert before == sorted(str(p.relative_to(old.path)) for p in old.path.rglob("*"))
    assert (old.path / "a.py").read_text() == "fixed\n"


def test_seed_cli_preview_requires_new_approval_and_disallows_tests_only(seed_input, tmp_path, capsys):
    from test_live_repair_cli import cli_fixture

    from reviewflow.quick import main

    plan, _, report_path, _ = seed_input
    args, output, _ = cli_fixture(plan.root, tmp_path)
    assert main(args) == 0
    original = json.loads(capsys.readouterr().out)
    seeded = args + ["--seed-report", str(report_path)]
    assert main(seeded) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["seed"]["validation"] == "unverified_input"
    assert preview["seed"]["reported_prior_calls"] == 12
    assert preview["approval_digest"] != original["approval_digest"]
    assert main(seeded + ["--approve", original["approval_digest"]]) == 2
    assert main(seeded + ["--tests-only"]) == 2
    assert not output.exists()
    assert (plan.root / "a.py").read_text() == "old\n"

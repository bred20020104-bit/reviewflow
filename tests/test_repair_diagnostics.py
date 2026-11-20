"""Report integration with synthetic model and container boundaries."""

import json

from conftest import git
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import setup_budget
from test_test_diagnostics import envelope
from test_volume_runner import process

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.live_repair_cli import _markdown


def test_each_run_preserves_diagnostics_and_candidate_association_when_blocked(git_repo, tmp_path):
    (git_repo / "test_example.py").write_bytes(b"def test_example(): assert False\n")
    git(git_repo, "add", "test_example.py")
    git(git_repo, "commit", "-qm", "Synthetic failing test")
    plan, config, profiles, runner, preview, authority = setup_budget(git_repo, tmp_path, cap=32)
    categories = iter(["assertion_error", "permission_error", "permission_error"])
    def execute(command, **kwargs):
        if command[1] == "run":
            value = json.loads(envelope())
            value["failures"][0]["category"] = next(categories)
            kwargs["stdout"].write(json.dumps(value).encode())
            return process(1)
        return process()
    runner._execute = execute
    result = run_repair_session(plan, config, authority, profiles, runner,
                               RepairFixtureAdapter(config.model))
    assert result["status"] == "blocked" and result["failure_stage"] == "final_tests"
    runs = result["test_runs"]
    assert [r.get("stage") for r in runs] == ["baseline", "candidate", "final"]
    assert [r["sequence"] for r in runs] == [1, 2, 3]
    assert runs[0]["finding_id"] is None and runs[0]["patch_sha256"] is None
    assert runs[1]["patch_sha256"] == result["patch_attempts"][0]["patch_sha256"]
    assert runs[1]["finding_id"] == "synthetic-bug"
    assert [r["test_failures"][0]["category"] for r in runs] == [
        "assertion_error", "permission_error", "permission_error"]
    assert runs[1]["comparison"]["comparable"]
    assert runs[1]["comparison"]["new_failures"][0]["category"] == "permission_error"
    assert runs[1]["comparison"]["no_longer_reported"][0]["category"] == "assertion_error"
    assert runs[2]["comparison"]["new_failures"] == []
    assert result["application"] is None
    result.update(preview=preview, report_directory="synthetic-report")
    text = _markdown(result)
    assert "baseline" in text and "candidate" in text and "final" in text
    assert "test_example.py" in text and "permission_error" in text
    assert "does not mean the business defect is fixed" in text
    assert (git_repo / "a.py").read_text() == "old\n"


def test_missing_details_never_claim_failure_improvement(git_repo, tmp_path):
    plan, config, profiles, runner, preview, authority = setup_budget(git_repo, tmp_path, cap=32)
    result = run_repair_session(plan, config, authority, profiles, runner,
                               RepairFixtureAdapter(config.model))
    assert result["status"] == "applied"
    comparison = result["test_runs"][1].get("comparison")
    assert comparison and comparison["comparable"] is False
    result.update(preview=preview, report_directory="synthetic-report")
    assert "cannot be compared reliably" in _markdown(result)


def test_markdown_ignores_unknown_target_diagnostic_fields(git_repo, tmp_path):
    plan, config, profiles, runner, preview, authority = setup_budget(git_repo, tmp_path, cap=32)
    result = run_repair_session(plan, config, authority, profiles, runner,
                               RepairFixtureAdapter(config.model))
    result.update(preview=preview, report_directory="synthetic-report")
    result["test_runs"][0].update(diagnostic_code="SECRET", diagnostics_status="SECRET",
        test_failures=[{"path": "SECRET", "category": "SECRET", "phase": "SECRET", "line": 1}])
    assert "SECRET" not in _markdown(result)

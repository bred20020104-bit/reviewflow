"""Seed reports are untrusted inputs, never reopened validation authority."""

import json
from pathlib import Path

import pytest
from test_repair_candidate import create, plan_for
from test_repair_workspace import proposal_for

from reviewflow.backend.repair import RepairError, validate_patch
from reviewflow.review.context import ReviewContext


@pytest.fixture
def seed_input(git_repo, tmp_path):
    old = create(plan_for(git_repo, tmp_path / "old-storage"))
    source, inventory = old.source_inventory(old.plan.selected)
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    old.apply(validate_patch(context, task, finding, proposal), context, task, finding)
    report = {
        "report_type": "live_development_repair",
        "status": "blocked",
        "candidate_path": str(old.path),
        "preserve_candidate": True,
        "candidate_diff": "synthetic nonempty diff",
        "application": None,
        "preview": {"candidate": old.plan.preview()},
        "accounting": {
            "requests_attempted": 12, "usage_complete": True,
            "records": [{"status": "complete", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "error_code": None} for _ in range(12)],
        },
        "patch_attempts": [{"status": "provisional"}],
        "test_runs": [
            {
                "content_digest": old.current_digest(),
                "status": "failed",
                "execution": "container",
                "suite": "repository",
            }
        ],
    }
    report_path = tmp_path / "seed-report.json"
    report_path.write_text(json.dumps(report))
    plan = plan_for(git_repo, tmp_path / "new-storage")
    return plan, old, report_path, report


def test_seed_captures_only_selected_bytes_and_explicit_reported_accounting(seed_input):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, old, path, _ = seed_input
    seed = SeedPlan.load(plan, path)
    assert [(r.path, r.content) for r in seed.replacements] == [("a.py", "fixed\n")]
    preview = seed.preview()
    assert preview["depth"] == 1 and preview["reported_prior_calls"] == 12
    assert preview["reported_prior_patch_attempts"] == 1
    assert preview["content_digest"] == old.current_digest()
    assert preview["validation"] == "unverified_input"
    assert "fixed" not in json.dumps(preview)
    assert (plan.root / "a.py").read_text() == "old\n"


@pytest.mark.parametrize(
    "change",
    [
        "usage",
        "status",
        "application",
        "test_status",
        "test_execution",
        "digest",
        "base",
        "branch",
        "selection",
        "origin",
        "path",
        "calls",
        "attempts",
        "depth",
    ],
)
def test_invalid_historical_report_cannot_seed_candidate(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, _, path, report = seed_input
    if change == "usage":
        report["accounting"]["usage_complete"] = False
    elif change == "status":
        report["status"] = "partial_application"
    elif change == "application":
        report["application"] = {"claimed": "applied"}
    elif change == "test_status":
        report["test_runs"][-1]["status"] = "timeout"
    elif change == "test_execution":
        report["test_runs"][-1]["execution"] = "host"
    elif change == "digest":
        report["test_runs"][-1]["content_digest"] = "0" * 64
    elif change == "calls":
        report["accounting"]["requests_attempted"] = 49
    elif change == "attempts":
        report["patch_attempts"] *= 9
    elif change == "depth":
        report["seed"] = {
            "depth": 2,
            "reported_prior_calls": 20,
            "reported_prior_patch_attempts": 2,
        }
    else:
        field = {
            "selection": "selected_paths",
            "origin": "original_state",
            "path": "destination",
        }.get(change, change)
        report["preview"]["candidate"][field] = [] if change == "selection" else "different"
    path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        SeedPlan.load(plan, path)
    assert not plan.destination.exists()


@pytest.mark.parametrize("change", ["selected", "unselected", "mode", "extra", "symlink"])
def test_changed_candidate_or_unselected_content_rejected(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan
    from reviewflow.backend.validation import content_digest

    plan, old, path, report = seed_input
    if change == "selected":
        (old.path / "a.py").write_text("tampered\n")
    elif change == "unselected":
        (old.path / "src/b.py").write_text("unexpected\n")
        files = dict(plan.baseline)
        files.update({"a.py": b"fixed\n", "src/b.py": b"unexpected\n"})
        report["test_runs"][-1]["content_digest"] = content_digest(files)
        path.write_text(json.dumps(report))
    elif change == "mode":
        (old.path / "a.py").chmod(0o755)
    elif change == "extra":
        (old.path / "extra.py").write_text("unexpected\n")
    else:
        target = old.path / "a.py"
        target.unlink()
        target.symlink_to(plan.root / "a.py")
    with pytest.raises(RepairError):
        SeedPlan.load(plan, path)
    assert not plan.destination.exists()


@pytest.mark.parametrize("change", ["report", "source"])
def test_seed_is_rechecked_after_preview(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, old, path, report = seed_input
    seed = SeedPlan.load(plan, path)
    if change == "report":
        report["accounting"]["requests_attempted"] = 13
        path.write_text(json.dumps(report))
    else:
        (old.path / "a.py").write_text("changed after approval\n")
    with pytest.raises(RepairError):
        seed.check(plan)
    assert not plan.destination.exists()


@pytest.mark.parametrize("change", ["empty_directory", "malformed_lineage"])
def test_seed_rejects_unexpected_tree_and_malformed_lineage(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, old, path, report = seed_input
    if change == "empty_directory":
        (old.path / "unexpected-directory").mkdir()
    else:
        report["seed"] = ["not a manifest"]
        path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        SeedPlan.load(plan, path)


@pytest.mark.parametrize("change", ["duplicate_keys", "report_size", "source_size", "dirty_selected",
                                    "calls_bool", "prior_calls", "prior_attempts"])
def test_seed_rejects_input_and_accounting_limits(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan
    from reviewflow.backend.validation import content_digest

    plan, old, path, report = seed_input
    if change == "duplicate_keys":
        path.write_text('{"status":"blocked",' + json.dumps(report)[1:])
    elif change == "report_size":
        path.write_bytes(b" " * 262145)
    elif change == "source_size":
        data = b"x" * 65537
        (old.path / "a.py").write_bytes(data)
        report["test_runs"][-1]["content_digest"] = content_digest(dict(plan.baseline) | {"a.py": data})
        path.write_text(json.dumps(report))
    elif change == "dirty_selected":
        (plan.root / "a.py").write_text("user work\n")
    else:
        if change == "calls_bool":
            report["accounting"]["requests_attempted"] = True
        else:
            report["seed"] = {"depth": 1, "reported_prior_calls": 49 if change == "prior_calls" else 48,
                              "reported_prior_patch_attempts": 9 if change == "prior_attempts" else 8}
        path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        SeedPlan.load(plan, path)
    assert not plan.destination.exists()


@pytest.mark.parametrize("change", ["failed_with_usage", "missing_usage", "missing_records", "count_mismatch"])
def test_seed_requires_complete_transport_records_not_only_usage_flag(seed_input, change):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, _, path, report = seed_input
    if change == "failed_with_usage":
        report["accounting"]["records"][-1].update(status="transport_or_usage_failure", error_code="output_limit")
    elif change == "missing_usage":
        report["accounting"]["records"][-1]["usage"] = None
    elif change == "missing_records":
        del report["accounting"]["records"]
    else:
        report["accounting"]["records"].pop()
    assert report["accounting"]["usage_complete"] is True
    path.write_text(json.dumps(report))
    with pytest.raises(RepairError):
        SeedPlan.load(plan, path)
    assert not plan.destination.exists()


def test_second_seed_depth_preserves_reported_history(seed_input):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, _, path, report = seed_input
    report["seed"] = {"depth": 1, "reported_prior_calls": 48, "reported_prior_patch_attempts": 8}
    report["accounting"]["requests_attempted"] = 48
    report["accounting"]["records"] *= 4
    report["patch_attempts"] *= 8
    path.write_text(json.dumps(report))
    seed = SeedPlan.load(plan, path)
    assert seed.depth == 2
    assert seed.reported_prior_calls == 96
    assert seed.reported_prior_patch_attempts == 16


def test_seed_report_symlink_rejected(seed_input):
    from reviewflow.backend.candidate_seed import SeedPlan

    plan, _, path, _ = seed_input
    link = Path(str(path) + ".link")
    link.symlink_to(path)
    with pytest.raises(RepairError):
        SeedPlan.load(plan, link)

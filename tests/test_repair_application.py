"""Unit fixtures for application gating; no genuine model/target acceptance here."""

from dataclasses import replace

import pytest
from test_repair_candidate import create, plan_for
from test_repair_workspace import proposal_for
from test_validation_publication import process

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.repair import RepairError, validate_patch
from reviewflow.backend.volume_runner import VolumeDockerRunner
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import ReviewTask
from reviewflow.review.workflow_models import RunLimits, RunReport, TaskOutcome

IMAGE = "sha256:" + "a" * 64


def review_fixture(candidate):
    _, inventory = candidate.source_inventory(candidate.plan.selected)
    paths = candidate.plan.selected
    tasks = tuple(TaskOutcome(ReviewTask(task_id=role, snapshot_id=inventory.snapshot.snapshot_id,
                    role=role, paths=paths, objective="Synthetic gate test"), "completed", paths)
                  for role in ("correctness", "security"))
    return RunReport(1, inventory.snapshot.snapshot_id, "multi", "live_development", "partial",
                     3, RunLimits(), paths, paths, paths, (), (), tasks, (), (), (),
                     semantic_review_performed=True)


def ready_candidate(git_repo, tmp_path, paths=("a.py",)):
    candidate = create(plan_for(git_repo, tmp_path / "storage", paths))
    runner = VolumeDockerRunner(IMAGE, execute=lambda *args, **kwargs: process())
    gate = ApprovalGate()

    def run(files):
        return runner.run(files, "repository", gate,
                          gate.approve(runner.intent(files), confirmed=True))

    baseline = run(candidate.files())
    source, inventory = candidate.source_inventory(candidate.plan.selected)
    task, finding, proposal = proposal_for(source, inventory)
    for path in paths[1:]:
        _, extra_finding, extra = proposal_for(source, inventory, path=path)
        task = task.model_copy(update={"paths": paths})
        finding = finding.model_copy(update={"evidence": finding.evidence + extra_finding.evidence})
        proposal = proposal.model_copy(update={"replacements": proposal.replacements + extra.replacements})
    context = ReviewContext(source, inventory)
    candidate.apply(validate_patch(context, task, finding, proposal), context, task, finding)
    result = run(candidate.files())
    return candidate, runner, baseline, result, review_fixture(candidate)


def test_only_process_owned_tests_and_review_allow_selected_worktree_write(git_repo, tmp_path):
    from conftest import git

    from reviewflow.backend.repair_authority import ApplicationAuthority

    # These existing user changes are outside the selected source.
    (git_repo / "notes.txt").write_text("user-owned untracked\n")
    (git_repo / "src/b.py").write_text("user-owned dirty\n")
    index_before = git(git_repo, "ls-files", "--stage", "-z")
    head_before = git(git_repo, "rev-parse", "HEAD")
    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    authority = ApplicationAuthority(runner)
    permit = authority.finish(candidate, baseline, result, (review,))
    outcome = candidate.writeback(authority, permit)
    assert outcome["applied_files"] == ["a.py"]
    assert outcome["commit_ready"] is False
    assert (git_repo / "a.py").read_bytes() == b"fixed\n"
    assert (git_repo / "src/b.py").read_text() == "user-owned dirty\n"
    assert (git_repo / "notes.txt").read_text() == "user-owned untracked\n"
    assert git(git_repo, "ls-files", "--stage", "-z") == index_before
    assert git(git_repo, "rev-parse", "HEAD") == head_before
    with pytest.raises(RepairError):
        candidate.writeback(authority, permit)


@pytest.mark.parametrize("change", ["simulated", "wrong_snapshot", "errors", "omission",
    "missing_security", "unreviewed", "incomplete_supply", "failed"])
def test_incomplete_or_nonreal_review_cannot_authorize_writeback(git_repo, tmp_path, change):
    from reviewflow.backend.repair_authority import ApplicationAuthority

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    changes = {
        "simulated": {"execution": "simulated"},
        "wrong_snapshot": {"snapshot_id": "0" * 64},
        "errors": {"errors": ("fixture_error",)},
        "omission": {"omissions": (("a.py", "context_budget"),)},
        "missing_security": {"tasks": review.tasks[:1]},
        "unreviewed": {"unreviewed_paths": ("a.py",)},
        "incomplete_supply": {"supplied_paths": ()},
        "failed": {"status": "failed"},
    }
    with pytest.raises(RepairError):
        ApplicationAuthority(runner).finish(candidate, baseline, result,
                                             (replace(review, **changes[change]),))
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("change", ["copied_result", "copied_baseline", "wrong_image", "original_dirty", "candidate_changed"])
def test_foreign_or_stale_validation_cannot_authorize_application(git_repo, tmp_path, change):
    from reviewflow.backend.repair_authority import ApplicationAuthority

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    if change == "copied_result":
        result = replace(result)
    elif change == "copied_baseline":
        baseline = replace(baseline)
    elif change == "wrong_image":
        result = replace(result, image="sha256:" + "b" * 64)
    elif change == "original_dirty":
        (git_repo / "a.py").write_text("user edit\n")
    else:
        (candidate.path / "a.py").write_text("changed candidate\n")
    with pytest.raises(RepairError):
        ApplicationAuthority(runner).finish(candidate, baseline, result, (review,))


def test_changed_target_after_validation_is_not_overwritten(git_repo, tmp_path):
    from reviewflow.backend.repair_authority import ApplicationAuthority

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    authority = ApplicationAuthority(runner)
    permit = authority.finish(candidate, baseline, result, (review,))
    (git_repo / "a.py").write_text("late user edit\n")
    with pytest.raises(RepairError):
        candidate.writeback(authority, permit)
    assert (git_repo / "a.py").read_text() == "late user edit\n"


def test_manually_constructed_or_copied_permit_has_no_authority(git_repo, tmp_path):
    from reviewflow.backend.repair_authority import ApplicationAuthority, ApplicationPermit

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    authority = ApplicationAuthority(runner)
    permit = authority.finish(candidate, baseline, result, (review,))
    for foreign in (ApplicationPermit("made-up"), replace(permit)):
        with pytest.raises(RepairError):
            candidate.writeback(authority, foreign)
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("phase,exit_code,allowed", [
    ("baseline", 1, True), ("baseline", 3, False), ("baseline", 5, False),
    ("candidate", 1, False), ("candidate", 3, False), ("candidate", 5, False),
])
def test_owned_test_outcomes_are_not_all_success(git_repo, tmp_path, phase, exit_code, allowed):
    from reviewflow.backend.repair_authority import ApplicationAuthority

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    runner._execute = lambda command, **kwargs: process(exit_code if "run" in command else 0)
    files = dict(candidate.plan.baseline) if phase == "baseline" else candidate.files()
    gate = ApprovalGate()
    run = runner.run(files, "repository", gate, gate.approve(runner.intent(files), confirmed=True))
    if phase == "baseline":
        baseline = run
    else:
        result = run
    authority = ApplicationAuthority(runner)
    if allowed:
        assert authority.finish(candidate, baseline, result, (review,))
    else:
        with pytest.raises(RepairError):
            authority.finish(candidate, baseline, result, (review,))


def test_expired_permit_cannot_write(git_repo, tmp_path, monkeypatch):
    from reviewflow.backend import repair_authority

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    authority = repair_authority.ApplicationAuthority(runner)
    now = repair_authority.time.monotonic()
    permit = authority.finish(candidate, baseline, result, (review,))
    monkeypatch.setattr(repair_authority.time, "monotonic", lambda: now + 1000)
    with pytest.raises(RepairError):
        candidate.writeback(authority, permit)
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("defect", ["task_error", "task_scope", "wrong_verification", "stale_evidence", "accepted", "unresolved"])
def test_review_provenance_and_findings_gate(git_repo, tmp_path, defect):
    from reviewflow.backend.repair_authority import ApplicationAuthority
    from reviewflow.review.contracts import VerificationResult
    from reviewflow.review.workflow_models import FindingOutcome

    candidate, runner, baseline, result, review = ready_candidate(git_repo, tmp_path)
    source, inventory = candidate.source_inventory(candidate.plan.selected)
    _, finding, _ = proposal_for(source, inventory)
    finding = finding.model_copy(update={"task_id": "correctness"})
    verification = VerificationResult(finding_id=finding.finding_id, verdict="rejected",
                                      rationale="Synthetic review", evidence=finding.evidence)
    if defect == "task_error":
        review = replace(review, tasks=(replace(review.tasks[0], error="failed"), review.tasks[1]))
    elif defect == "task_scope":
        task = review.tasks[0].task.model_copy(update={"paths": ("src/b.py",)})
        review = replace(review, tasks=(replace(review.tasks[0], task=task), review.tasks[1]))
    else:
        if defect == "wrong_verification":
            verification = verification.model_copy(update={"finding_id": "foreign"})
        elif defect == "stale_evidence":
            ref = verification.evidence[0].model_copy(update={"content_sha256": "0" * 64})
            verification = verification.model_copy(update={"evidence": (ref,)})
        else:
            verification = verification.model_copy(update={"verdict": defect})
        review = replace(review, findings=(FindingOutcome(finding, "correctness", verification),))
    with pytest.raises(RepairError):
        ApplicationAuthority(runner).finish(candidate, baseline, result, (review,))
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("failure", [None, "second_write", "user_race", "index_change"])
def test_multifile_writeback_and_owned_rollback(git_repo, tmp_path, monkeypatch, failure):
    from conftest import git

    from reviewflow.backend import repair_candidate
    from reviewflow.backend.repair import PartialApplicationError
    from reviewflow.backend.repair_authority import ApplicationAuthority

    candidate, runner, baseline, result, review = ready_candidate(
        git_repo, tmp_path, ("a.py", "src/b.py"))
    authority = ApplicationAuthority(runner)
    permit = authority.finish(candidate, baseline, result, (review,))
    original_replace = repair_candidate._replace
    calls = 0

    def injected(root, path, before, after):
        nonlocal calls
        calls += 1
        if calls == 2 and failure in {"second_write", "user_race"}:
            if failure == "user_race":
                (git_repo / "a.py").write_text("concurrent user change\n")
            raise RepairError("Injected second write failure")
        original_replace(root, path, before, after)
        if calls == 1 and failure == "index_change":
            git(git_repo, "add", "a.py")

    monkeypatch.setattr(repair_candidate, "_replace", injected)
    if failure:
        with pytest.raises(PartialApplicationError if failure in {"user_race", "index_change"} else RepairError):
            candidate.writeback(authority, permit)
        assert (git_repo / "src/b.py").read_bytes() == b"b = 1\n"
        if failure == "user_race":
            assert (git_repo / "a.py").read_bytes() == b"concurrent user change\n"
        elif failure == "index_change":
            assert (git_repo / "a.py").read_bytes() == b"fixed\n"
            assert git(git_repo, "show", ":a.py") == b"fixed\n"
        else:
            assert (git_repo / "a.py").read_bytes() == b"old\n"
    else:
        outcome = candidate.writeback(authority, permit)
        assert outcome["applied_files"] == ["a.py", "src/b.py"]
        assert (git_repo / "a.py").read_bytes() == b"fixed\n"
        assert (git_repo / "src/b.py").read_bytes() == b"fixed\n"
    with pytest.raises(RepairError):
        candidate.writeback(authority, permit)

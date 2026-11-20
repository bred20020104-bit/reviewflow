"""Owned candidate tests use temporary synthetic Git repositories only."""

import os

import pytest
from conftest import git
from test_repair_workspace import proposal_for

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.repair import RepairError, validate_patch
from reviewflow.review.context import ReviewContext
from reviewflow.sources.git import GitHeadSource


def plan_for(repo, storage, paths=("a.py",)):
    from reviewflow.backend.repair_candidate import CandidatePlan

    # git_repo itself is tmp_path; managed storage must be its sibling.
    storage = repo.parent / (repo.name + "-" + storage.name)
    storage.mkdir(exist_ok=True)
    return CandidatePlan.from_head(GitHeadSource(repo), paths, storage / "candidate")


def create(plan):
    from reviewflow.backend.repair_candidate import RepairCandidate

    gate = ApprovalGate()
    return RepairCandidate.create(plan, gate, gate.approve(plan.preview(), confirmed=True))


def test_candidate_preview_no_effects_and_unapproved_creation_rejected(git_repo, tmp_path):
    from reviewflow.backend.repair_candidate import RepairCandidate

    plan = plan_for(git_repo, tmp_path / "storage")
    before = git(git_repo, "worktree", "list", "--porcelain")
    preview = plan.preview()
    assert preview["base"] == git(git_repo, "rev-parse", "HEAD").decode().strip()
    assert preview["selected_paths"] == ["a.py"]
    assert not plan.destination.exists()
    with pytest.raises(Denied):
        RepairCandidate.create(plan, ApprovalGate(), "not-approved")
    assert git(git_repo, "worktree", "list", "--porcelain") == before
    assert not plan.destination.exists()


def test_detached_candidate_contains_head_not_dirty_work_and_preserves_original(dirty_repo, tmp_path):
    plan = plan_for(dirty_repo, tmp_path / "storage")
    before = git(dirty_repo, "status", "--porcelain=v1", "-z")
    index = git(dirty_repo, "ls-files", "--stage", "-z")
    branch = git(dirty_repo, "symbolic-ref", "HEAD")
    candidate = create(plan)
    assert candidate.files()["a.py"] == b"old\n"
    assert (dirty_repo / "a.py").read_bytes() == b"new\n"
    assert git(candidate.path, "rev-parse", "HEAD") == git(dirty_repo, "rev-parse", "HEAD")
    assert git(dirty_repo, "status", "--porcelain=v1", "-z") == before
    assert git(dirty_repo, "ls-files", "--stage", "-z") == index
    assert git(dirty_repo, "symbolic-ref", "HEAD") == branch
    assert plan.preview()["dirty_selected_paths"] == ["a.py"]


def test_script_and_example_export_do_not_relax_model_inventory(git_repo, tmp_path):
    (git_repo / "run.sh").write_text("#!/bin/sh\nprintf 'not executed'\n")
    (git_repo / "run.sh").chmod(0o755)
    (git_repo / ".env.example").write_bytes(b"VALUE=placeholder\n")
    git(git_repo, "add", "run.sh", ".env.example")
    git(git_repo, "commit", "-m", "synthetic local test inputs")
    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    assert candidate.files()[".env.example"] == b"VALUE=placeholder\n"
    assert "run.sh" in candidate.files()
    _, inventory = candidate.source_inventory(("a.py",))
    included = {r.path for r in inventory.snapshot.records if r.status == "included"}
    assert included == {"a.py"}
    assert all(r.status == "skipped" for r in inventory.snapshot.records
               if r.path in ("run.sh", ".env.example"))


@pytest.mark.parametrize("kind", ["secret", "symlink", "binary", "oversized"])
def test_unsafe_head_tree_rejected_before_candidate_creation(git_repo, tmp_path, kind):
    if kind == "secret":
        path = git_repo / ".env"
        path.write_text("KEY=private\n")
    elif kind == "symlink":
        path = git_repo / "link.py"
        path.symlink_to("a.py")
    else:
        path = git_repo / "bad.py"
        path.write_bytes(b"\x00" if kind == "binary" else b"x" * 262145)
    git(git_repo, "add", "-f", path.name)
    git(git_repo, "commit", "-m", "synthetic rejected entry")
    with pytest.raises(RepairError):
        plan_for(git_repo, tmp_path / "storage")
    assert not (tmp_path / "storage/candidate").exists()


def test_current_candidate_patch_snapshot_and_rollback(git_repo, tmp_path):
    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    source, inventory = candidate.source_inventory(("a.py",))
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    patch = validate_patch(context, task, finding, proposal)
    baseline = candidate.current_digest()
    candidate.apply(patch, context, task, finding)
    assert candidate.files()["a.py"] == b"fixed\n"
    assert candidate.current_digest() != baseline
    _, updated = candidate.source_inventory(("a.py",))
    assert updated.snapshot.snapshot_id != inventory.snapshot.snapshot_id
    with pytest.raises(RepairError):
        candidate.apply(patch, context, task, finding)
    assert (git_repo / "a.py").read_bytes() == b"old\n"
    candidate.restore_attempt()
    assert candidate.files()["a.py"] == b"old\n"
    assert candidate.current_digest() == baseline


@pytest.mark.parametrize("change", ["source", "parent", "index", "branch"])
def test_original_state_change_stops_candidate_application(git_repo, tmp_path, change):
    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    source, inventory = candidate.source_inventory(("a.py",))
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    patch = validate_patch(context, task, finding, proposal)
    if change == "source":
        (git_repo / "a.py").write_text("user edit\n")
    elif change == "parent":
        original = git_repo.with_name(git_repo.name + "-moved")
        git_repo.rename(original)
        git_repo.symlink_to(original, target_is_directory=True)
    elif change == "index":
        git(git_repo, "update-index", "--force-remove", "src/b.py")
    else:
        git(git_repo, "switch", "-c", "user-branch")
    with pytest.raises(RepairError):
        candidate.apply(patch, context, task, finding)
    assert (candidate.path / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("change", ["modified", "symlink", "unknown", "git-pointer", "candidate-index", "candidate-branch"])
def test_unknown_candidate_changes_block_reads_and_rollback(git_repo, tmp_path, change):
    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    if change == "modified":
        (candidate.path / "a.py").write_text("user-owned\n")
    elif change == "symlink":
        (candidate.path / "a.py").unlink()
        (candidate.path / "a.py").symlink_to(git_repo / "a.py")
    elif change == "unknown":
        (candidate.path / "unknown.py").write_text("user-owned\n")
    elif change == "candidate-index":
        git(candidate.path, "update-index", "--force-remove", "src/b.py")
    elif change == "candidate-branch":
        git(candidate.path, "switch", "-c", "unexpected-candidate-branch")
    else:
        pointer = candidate.path / ".git"
        fd = os.open(pointer, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0))
        os.write(fd, f"gitdir: {git_repo / '.git'}\n".encode())
        os.close(fd)
    with pytest.raises(RepairError):
        candidate.files()
    with pytest.raises(RepairError):
        candidate.restore_attempt()
    assert (git_repo / "a.py").read_bytes() == b"old\n"


def test_candidate_application_rechecks_live_protected_path_rules(git_repo, tmp_path):
    (git_repo / "config.py").write_bytes(b"value = 1\n")
    git(git_repo, "add", "config.py")
    git(git_repo, "commit", "-m", "synthetic configuration")
    candidate = create(plan_for(git_repo, tmp_path / "storage", ("config.py",)))
    source, inventory = candidate.source_inventory(("config.py",))
    task, finding, proposal = proposal_for(source, inventory, path="config.py", content="value = 2\n")
    context = ReviewContext(source, inventory)
    patch = validate_patch(context, task, finding, proposal)
    with pytest.raises(RepairError):
        candidate.apply(patch, context, task, finding)
    assert (candidate.path / "config.py").read_bytes() == b"value = 1\n"


def test_candidate_replacement_preserves_modes_under_private_umask(git_repo, tmp_path):
    import os
    import stat

    candidate = create(plan_for(git_repo, tmp_path / "storage"))
    source, inventory = candidate.source_inventory(("a.py",))
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    patch = validate_patch(context, task, finding, proposal)
    previous = os.umask(0o077)
    try:
        candidate.apply(patch, context, task, finding)
        assert candidate.files()["a.py"] == b"fixed\n"
        if os.name == "posix":
            assert stat.S_IMODE((candidate.path / "a.py").stat().st_mode) == 0o644
    finally:
        os.umask(previous)


@pytest.mark.parametrize("user_race", [False, True])
def test_failed_multifile_candidate_change_rolls_back_only_owned_bytes(git_repo, tmp_path, monkeypatch, user_race):
    from reviewflow.backend import repair_candidate
    from reviewflow.backend.repair import PartialApplicationError

    candidate = create(plan_for(git_repo, tmp_path / "storage", ("a.py", "src/b.py")))
    source, inventory = candidate.source_inventory(("a.py", "src/b.py"))
    task, finding, first = proposal_for(source, inventory)
    _, second_finding, second = proposal_for(source, inventory, path="src/b.py")
    task = task.model_copy(update={"paths": ("a.py", "src/b.py")})
    finding = finding.model_copy(update={"evidence": finding.evidence + second_finding.evidence})
    proposal = first.model_copy(update={"replacements": first.replacements + second.replacements})
    context = ReviewContext(source, inventory)
    patch = validate_patch(context, task, finding, proposal)
    original_replace = repair_candidate._replace
    calls = 0

    def fail_second(root, path, before, after):
        nonlocal calls
        calls += 1
        if calls == 2:
            if user_race:
                (candidate.path / "a.py").write_bytes(b"user edit during rollback\n")
            raise RepairError("Injected filesystem failure")
        return original_replace(root, path, before, after)

    monkeypatch.setattr(repair_candidate, "_replace", fail_second)
    with pytest.raises(PartialApplicationError if user_race else RepairError):
        candidate.apply(patch, context, task, finding)
    assert (candidate.path / "a.py").read_bytes() == (
        b"user edit during rollback\n" if user_race else b"old\n")
    assert (candidate.path / "src/b.py").read_bytes() == b"b = 1\n"
    assert (git_repo / "a.py").read_bytes() == b"old\n"


def test_candidate_destination_inside_target_or_existing_is_rejected(git_repo, tmp_path):
    from reviewflow.backend.repair_candidate import CandidatePlan

    source = GitHeadSource(git_repo)
    with pytest.raises(RepairError):
        CandidatePlan.from_head(source, ("a.py",), git_repo / "candidate")
    existing = tmp_path.parent / (tmp_path.name + "-existing")
    existing.mkdir()
    (existing / "keep").write_text("user-owned")
    with pytest.raises(RepairError):
        CandidatePlan.from_head(source, ("a.py",), existing)
    assert (existing / "keep").read_text() == "user-owned"

import hashlib

import pytest
from conftest import git

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.repair import PatchProposal, RepairError, validate_patch
from reviewflow.backend.workspace import Workspace
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import EvidenceRef, Finding, ReviewTask
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import GitHeadSource


def proposal_for(source, inventory, *, content="fixed\n", path="a.py"):
    record = next(r for r in inventory.snapshot.records if r.path == path)
    task = ReviewTask(
        task_id="task",
        snapshot_id=inventory.snapshot.snapshot_id,
        role="correctness",
        paths=(path,),
        objective="Check correctness",
    )
    finding = Finding(
        finding_id="bug",
        task_id="task",
        title="Bug",
        root_cause="Wrong value",
        trigger="Call",
        impact="Wrong output",
        severity="medium",
        evidence=(
            EvidenceRef(
                snapshot_id=inventory.snapshot.snapshot_id,
                path=path,
                content_sha256=record.sha256,
                start_line=1,
                end_line=1,
            ),
        ),
    )
    proposal = PatchProposal.model_validate_json(
        __import__("json").dumps(
            {
                "snapshot_id": inventory.snapshot.snapshot_id,
                "finding_id": "bug",
                "replacements": [
                    {"path": path, "before_sha256": record.sha256, "content": content}
                ],
                "explanation": "Replace wrong value",
            }
        )
    )
    return task, finding, proposal


def make_workspace(repo, tmp_path):
    source = GitHeadSource(repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory)
    patch = validate_patch(ReviewContext(source, inventory), task, finding, proposal)
    storage = tmp_path / "managed"
    storage.mkdir()
    gate = ApprovalGate()
    intent = Workspace.creation_intent(source, inventory, storage, "test-run")
    workspace = Workspace.create(
        source, inventory, storage, "test-run", gate, gate.approve(intent, confirmed=True)
    )
    return workspace, gate, patch


def test_actual_worktree_patch_preserves_dirty_source_and_index(dirty_repo, tmp_path):
    # Storage is outside the target repository, not a child of its dirty tree.
    storage_parent = tmp_path.parent / (tmp_path.name + "-storage")
    storage_parent.mkdir()
    before = git(dirty_repo, "diff", "--cached")
    ws, gate, patch = make_workspace(dirty_repo, storage_parent)
    assert (ws.path / "a.py").read_text() == "old\n"
    with pytest.raises(Denied):
        ws.apply(patch, gate, "made-up")
    intent = ws.patch_intent(patch)
    ws.apply(patch, gate, gate.approve(intent, confirmed=True))
    assert (ws.path / "a.py").read_text() == "fixed\n"
    assert (dirty_repo / "a.py").read_text() == "new\n"
    assert git(dirty_repo, "diff", "--cached") == before
    assert ws.attempts["bug"] == 1
    assert "-old" in patch.diff and "+fixed" in patch.diff
    assert ws.current_digest() != ws.baseline_digest
    ws.rollback_attempt()
    assert (ws.path / "a.py").read_text() == "old\n"


def test_workspace_refuses_symlink_unknown_file_and_modified_base(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    (ws.path / "a.py").unlink()
    (ws.path / "a.py").symlink_to(git_repo / "a.py")
    with pytest.raises(RepairError):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    assert (git_repo / "a.py").read_text() == "old\n"


def test_patch_schema_scope_hash_and_protected_paths(git_repo):
    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    for changes in ({"snapshot_id": "b" * 64}, {"finding_id": "other"}):
        with pytest.raises(RepairError):
            validate_patch(context, task, finding, proposal.model_copy(update=changes))
    changed = proposal.replacements[0].model_copy(update={"before_sha256": "b" * 64})
    with pytest.raises(RepairError):
        validate_patch(
            context, task, finding, proposal.model_copy(update={"replacements": (changed,)})
        )
    with pytest.raises(Exception):
        PatchProposal.model_validate_json(proposal.model_dump_json().replace('"a.py"', '"../a.py"'))
    (git_repo / "test_app.py").write_text("assert True\n")
    git(git_repo, "add", "test_app.py")
    git(git_repo, "commit", "-m", "test fixture")
    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory, path="test_app.py")
    with pytest.raises(RepairError, match="protected"):
        validate_patch(ReviewContext(source, inventory), task, finding, proposal)


def test_worktree_creation_never_runs_filters_or_hooks(git_repo, tmp_path):
    tripwire = tmp_path / "EXECUTED"
    hook = git_repo / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{tripwire}'\n")
    hook.chmod(0o755)
    git(git_repo, "config", "filter.evil.smudge", f"touch '{tripwire}'")
    (git_repo / ".gitattributes").write_text("*.py filter=evil\n")
    # Attributes are untracked and never used during manual materialization.
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, _, _ = make_workspace(git_repo, parent)
    assert (ws.path / "a.py").read_text() == "old\n"
    assert not tripwire.exists()


def test_two_attempt_limit_and_rollback_preserves_user_changes(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    for _ in range(2):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
        ws.rollback_attempt()
    with pytest.raises(RepairError, match="attempt"):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    (ws.path / "src/b.py").write_text("user edit\n")
    with pytest.raises(RepairError, match="changed"):
        ws.current_digest()
    assert (ws.path / "src/b.py").read_text() == "user edit\n"


def test_noop_and_untracked_unknown_files_are_rejected(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    (ws.path / "unexpected.py").write_text("personal work\n")
    with pytest.raises(RepairError):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    assert (ws.path / "unexpected.py").exists()
    assert patch.sha256 == hashlib.sha256(patch.proposal.model_dump_json().encode()).hexdigest()

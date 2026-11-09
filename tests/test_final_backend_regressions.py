"""Regressions for the independent offline-backend review; never run target Python."""

import json
import os
import subprocess

import pytest
from conftest import git
from test_repair_workspace import make_workspace, proposal_for
from test_validation_publication import IMAGE, process

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.delivery import package_source
from reviewflow.backend.repair import RepairError, validate_patch
from reviewflow.backend.repair_loop import run_repair
from reviewflow.backend.validation import DockerRunner
from reviewflow.backend.workspace import Workspace
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import GitHeadSource


def multi_patch(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, _ = make_workspace(git_repo, parent)
    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    context = ReviewContext(source, inventory)
    task, finding, proposal = proposal_for(source, inventory)
    _, second_finding, second_proposal = proposal_for(source, inventory, path="src/b.py")
    task = task.model_copy(update={"paths": ("a.py", "src/b.py")})
    finding = finding.model_copy(update={"evidence": finding.evidence + second_finding.evidence})
    proposal = proposal.model_copy(
        update={
            "replacements": proposal.replacements + second_proposal.replacements,
        }
    )
    patch = validate_patch(context, task, finding, proposal)
    return ws, gate, context, task, finding, patch


def fail_second_replace(monkeypatch, ws, *, user_edit=False):
    original = os.replace

    def replace(src, dst, **kwargs):
        if dst == "b.py":
            if user_edit:
                (ws.path / "a.py").write_text("user content\n")
            raise PermissionError("synthetic disk failure")
        return original(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_direct_apply_restores_successful_writes_after_later_failure(
    git_repo, tmp_path, monkeypatch
):
    ws, gate, _, _, _, patch = multi_patch(git_repo, tmp_path)
    fail_second_replace(monkeypatch, ws)
    with pytest.raises(RepairError):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    assert (ws.path / "a.py").read_bytes() == ws._baseline["a.py"]
    assert (ws.path / "src/b.py").read_bytes() == ws._baseline["src/b.py"]
    assert ws._previous is None
    assert ws.current_digest() == ws.baseline_digest
    assert ws.attempts["bug"] == 1


@pytest.mark.parametrize("user_edit", [False, True])
def test_loop_partial_write_restores_or_stops_explicitly(
    git_repo, tmp_path, monkeypatch, user_edit
):
    ws, gate, context, task, finding, patch = multi_patch(git_repo, tmp_path)
    fail_second_replace(monkeypatch, ws, user_edit=user_edit)
    requests = []

    class Backend:
        def complete(self, request):
            requests.append(request)
            return patch.proposal.model_dump_json()

    def validate(workspace):
        pytest.fail("Partially applied patch must never be validated")

    report = run_repair(
        ws,
        context,
        task,
        finding,
        Backend(),
        gate,
        lambda intent: gate.approve(intent, confirmed=True),
        None,
        validate,
    )
    assert report["commit_ready"] is False
    if user_edit:
        assert report["status"] == "blocked_partial_application"
        assert len(requests) == 1
        assert (ws.path / "a.py").read_text() == "user content\n"
    else:
        assert report["status"] == "unresolved"
        assert ws.current_digest() == ws.baseline_digest
        assert ws._previous is None
        assert ws.attempts["bug"] == 2
    assert (git_repo / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_container_removal_is_not_reported_as_clean_exit(timeout):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == "run" and timeout:
            raise subprocess.TimeoutExpired(command, 1)
        return process(1 if command[1] == "rm" else 0)

    runner, gate = DockerRunner(IMAGE, execute=execute), ApprovalGate()
    files = {"a.py": b"x=1\n"}
    result = runner.run(
        files, "regression", gate, gate.approve(runner.intent(files, "regression"), confirmed=True)
    )
    assert result.status == "cleanup_failed"
    assert result.run_id.startswith("reviewflow-")
    assert next(c for c in calls if c[1] == "rm")[-1] == result.run_id
    # Explicit removal is the termination evidence; auto-remove would turn success
    # into an indistinguishable 'no such container' cleanup error.
    assert "--rm" not in next(c for c in calls if c[1] == "run")


def test_executable_git_entry_rejected_before_worktree_creation(git_repo, tmp_path):
    git(git_repo, "update-index", "--chmod=+x", "a.py")
    git(
        git_repo,
        "commit",
        "-m",
        "Executable fixture\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>",
    )
    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    gate = ApprovalGate()
    intent = Workspace.creation_intent(source, inventory, parent, "executable")
    grant = gate.approve(intent, confirmed=True)
    with pytest.raises(RepairError, match="executable"):
        Workspace.create(source, inventory, parent, "executable", gate, grant)
    assert not (parent / "executable").exists()
    assert git(git_repo, "branch", "--list", "reviewfix/executable") == b""
    gate.consume(grant, intent)  # Rejection precedes any authorization consumption.


def test_workspace_executable_permission_change_invalidates_digest(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, _, _ = make_workspace(git_repo, parent)
    (ws.path / "a.py").chmod(0o755)
    with pytest.raises(RepairError, match="executable"):
        ws.current_digest()


@pytest.mark.parametrize(
    "filename", ["credentials.json", "CREDENTIALS.JSON", "id_rsa", "private.pem"]
)
def test_package_rejects_known_credential_filenames_before_output(tmp_path, filename):
    root = tmp_path / "project"
    (root / "src/reviewflow").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="reviewflow"\n')
    (root / "examples").mkdir()
    (root / "examples" / filename).write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_secret": "synthetic-secret",
                "refresh_token": "synthetic-refresh",
            }
        )
    )
    output = tmp_path / "source.zip"
    with pytest.raises(ValueError, match="credential"):
        package_source(root, output)
    assert not output.exists()

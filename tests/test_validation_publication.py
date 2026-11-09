import subprocess
from dataclasses import replace

import pytest
from conftest import git
from test_repair_workspace import make_workspace

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.publication import Publisher
from reviewflow.backend.repair import RepairError
from reviewflow.backend.validation import DockerRunner, ValidationAuthority
from reviewflow.review.workflow_models import RunLimits, RunReport

IMAGE = "python@sha256:" + "a" * 64


def process(returncode=0):
    return subprocess.CompletedProcess([], returncode, b"", b"")


def test_container_command_limits_and_no_implicit_execution(tmp_path):
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["env"] == {"PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin")}
        return process()

    gate = ApprovalGate()
    runner = DockerRunner(IMAGE, execute=execute)
    files = {"a.py": b"x = 1\n", "tests/test_a.py": b"assert True\n"}
    intent = runner.intent(files, "regression")
    with pytest.raises(Denied):
        runner.run(files, "regression", gate, "fake")
    assert not calls
    result = runner.run(files, "regression", gate, gate.approve(intent, confirmed=True))
    assert result.status == "passed" and result.execution == "container"
    command = next(c for c, _ in calls if "run" in c)
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--user=65534:65534",
        "--pull=never",
    ):
        assert flag in command
    assert not any("docker.sock" in item for item in command)
    assert "--env" in command and "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert runner.owns(result)
    assert not runner.owns(replace(result))


@pytest.mark.parametrize(
    "kind,status",
    [
        ("timeout", "timeout"),
        ("missing", "blocked"),
        ("failure", "failed"),
        ("docker-failure", "blocked"),
    ],
)
def test_container_failures_do_not_become_success(kind, status):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if "image" in command:
            if kind == "missing":
                raise FileNotFoundError()
            return process()
        if "run" in command:
            if kind == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            return process(125 if kind == "docker-failure" else 1)
        return process()

    gate = ApprovalGate()
    runner = DockerRunner(IMAGE, execute=execute)
    files = {"a.py": b"x=1\n"}
    result = runner.run(
        files, "regression", gate, gate.approve(runner.intent(files, "regression"), confirmed=True)
    )
    assert result.status == status
    if kind == "timeout":
        assert any(c[1:3] == ["rm", "-f"] for c in calls)


def test_unpinned_image_and_unsafe_export_rejected():
    with pytest.raises(ValueError):
        DockerRunner("python:latest")
    runner = DockerRunner(IMAGE)
    for files in ({"../a.py": b"x"}, {".git/config": b"x"}, {".env": b"SECRET"}):
        with pytest.raises(RepairError):
            runner.intent(files, "regression")


def re_review(ws, *, simulated=False):
    _, inventory = ws.inventory()
    paths = tuple(ws._expected)
    return RunReport(
        1,
        inventory.snapshot.snapshot_id,
        "multi",
        "simulated" if simulated else "real",
        "complete",
        1,
        RunLimits(),
        paths,
        paths,
        paths,
        (),
        (),
        (),
        (),
        (),
        (),
        semantic_review_performed=not simulated,
    )


def validated_workspace(repo, parent, *, simulated=False):
    ws, gate, patch = make_workspace(repo, parent)
    codes = iter([0, 1, 0, 0])

    def execute(command, **kwargs):
        return process(next(codes) if "run" in command else 0)

    runner = DockerRunner(IMAGE, execute=execute)

    def run(files, suite):
        return runner.run(
            files, suite, gate, gate.approve(runner.intent(files, suite), confirmed=True)
        )

    baseline_regression = run(ws._baseline, "regression")
    baseline_reproducer = run(ws._baseline, "reproducer")
    ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    candidate_regression = run(ws._expected, "regression")
    candidate_reproducer = run(ws._expected, "reproducer")
    authority = ValidationAuthority(runner)
    receipt = authority.finish(
        ws,
        baseline_regression,
        baseline_reproducer,
        candidate_regression,
        candidate_reproducer,
        re_review(ws, simulated=simulated),
    )
    return ws, gate, authority, receipt


def test_unvalidated_or_simulated_repair_cannot_commit(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    with pytest.raises(RepairError, match="review"):
        validated_workspace(git_repo, parent, simulated=True)


def test_exact_approved_commit_in_owned_branch_only(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    original = git(git_repo, "rev-parse", "HEAD")
    ws, gate, authority, receipt = validated_workspace(git_repo, parent)
    publisher = Publisher(ws, authority)
    intent = publisher.commit_intent(receipt, "Test Person", "test@example.invalid")
    with pytest.raises(Denied):
        publisher.commit(receipt, "Test Person", "test@example.invalid", gate, "fake")
    oid = publisher.commit(
        receipt, "Test Person", "test@example.invalid", gate, gate.approve(intent, confirmed=True)
    )
    assert git(git_repo, "rev-parse", "HEAD") == original
    assert git(ws.path, "show", f"{oid}:a.py") == b"fixed\n"
    assert git(ws.path, "show", f"{oid}:src/b.py") == b"b = 1\n"
    assert (
        git(ws.path, "show", "-s", "--format=%B", oid)
        .decode()
        .endswith("Co-Authored-By: Claude Code <noreply@anthropic.com>\n\n")
    )
    assert ws.branch.startswith("reviewfix/")


def test_changed_index_and_git_pointer_cannot_escape_commit(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, authority, receipt = validated_workspace(git_repo, parent)
    publisher = Publisher(ws, authority)
    intent = publisher.commit_intent(receipt, "Test", "test@example.invalid")
    git(ws.path, "update-index", "--force-remove", "src/b.py")
    with pytest.raises(RepairError, match="index"):
        publisher.commit(
            receipt, "Test", "test@example.invalid", gate, gate.approve(intent, confirmed=True)
        )
    (ws.path / ".git").write_text(f"gitdir: {git_repo / '.git'}\n")
    with pytest.raises(RepairError, match="metadata"):
        ws.current_digest()


def test_push_requires_new_approval_and_binds_full_history_destination(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, authority, receipt = validated_workspace(git_repo, parent)
    publisher = Publisher(ws, authority)
    intent = publisher.commit_intent(receipt, "Test", "test@example.invalid")
    publisher.commit(
        receipt, "Test", "test@example.invalid", gate, gate.approve(intent, confirmed=True)
    )
    push = publisher.push_intent("https://example.invalid/repo.git")
    assert push["history"] == "commit and all reachable ancestors"
    assert push["branch"] == ws.branch
    assert "--force" not in publisher.push_command(push)
    assert publisher.push_command(push)[-1].endswith(":refs/heads/" + ws.branch)
    with pytest.raises(Denied):
        publisher.push(
            "https://example.invalid/repo.git", gate, gate.approve(intent, confirmed=True)
        )
    for url in ("ext::evil", "ssh://example.invalid/a", "https://user:SECRET@example.invalid/a"):
        with pytest.raises(RepairError):
            publisher.push_intent(url)

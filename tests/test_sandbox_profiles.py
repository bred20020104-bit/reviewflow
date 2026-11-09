"""Executable scratch is explicit and approval-bound; strict stays unchanged."""
import json
import subprocess

import pytest
from test_live_repair_cli import cli_fixture
from test_volume_runner import IMAGE, approved_run, process

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.volume_runner import VolumeDockerRunner
from reviewflow.quick import main


@pytest.mark.parametrize("profile", ["strict", "exec-scratch"])
def test_profile_controls_only_container_scratch(profile):
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        return process()

    runner = VolumeDockerRunner(IMAGE, sandbox_profile=profile, execute=execute)
    result = approved_run(runner)
    assert result.status == "passed"
    run = next(c for c in commands if c[1] == "run")
    assert "/tmp:rw,noexec,nosuid,size=32m" in run
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                 "--cpus=1", "--user=65534:65534", "--log-driver=none"):
        assert flag in run
    assert any(x.endswith(",target=/workspace,readonly") for x in run)
    assert not any("type=bind" in x or "docker.sock" in x for c in commands for x in c)
    assert ("/scratch:rw,exec,nosuid,nodev,size=32m,mode=1777" in run) == (profile == "exec-scratch")
    assert ("TMPDIR=/scratch" in run) == (profile == "exec-scratch")
    assert commands[-1][1:3] == ["volume", "rm"]


def test_default_profile_is_strict_and_has_no_executable_scratch():
    runner = VolumeDockerRunner(IMAGE)
    policy = runner.intent({"a.py": b"x=1\n"}).get("sandbox")
    assert policy == {"version": 1, "profile": "strict",
                      "tmpfs": ["/tmp:rw,noexec,nosuid,size=32m"], "environment": []}


@pytest.mark.parametrize("profile", ["exec", "", "../scratch", None, True, []])
def test_invalid_profiles_reject_before_docker(profile):
    with pytest.raises(ValueError):
        VolumeDockerRunner(IMAGE, sandbox_profile=profile,
                           execute=lambda *a, **kw: pytest.fail("Unexpected Docker call"))


@pytest.mark.parametrize("before,after", [("strict", "exec-scratch"), ("exec-scratch", "strict")])
def test_profile_change_cannot_reuse_execution_grant(before, after):
    runner = VolumeDockerRunner(IMAGE, sandbox_profile=before,
                               execute=lambda *a, **kw: pytest.fail("Unauthorized Docker call"))
    files = {"tests/test_a.py": b"def test_a(): assert True\n"}
    gate = ApprovalGate()
    grant = gate.approve(runner.intent(files), confirmed=True)
    runner.sandbox_profile = after
    with pytest.raises(Denied):
        runner.run(files, "repository", gate, grant)


@pytest.mark.parametrize("tests_only", [False, True])
def test_cli_profile_preview_invalidates_old_approval(git_repo, tmp_path, capsys, tests_only):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    if tests_only:
        args += ["--tests-only"]
    assert main(args) == 0
    strict = json.loads(capsys.readouterr().out)
    assert strict["test_policy"].get("sandbox", {}).get("profile") == "strict"
    changed = args + ["--test-sandbox", "exec-scratch"]
    assert main(changed) == 0
    scratch = json.loads(capsys.readouterr().out)
    assert scratch["test_policy"]["sandbox"] == {
        "version": 1, "profile": "exec-scratch",
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=32m",
                  "/scratch:rw,exec,nosuid,nodev,size=32m,mode=1777"],
        "environment": ["TMPDIR=/scratch"]}
    assert "exec-scratch" in scratch["notice"]
    assert strict["approval_digest"] != scratch["approval_digest"]
    assert main(changed + ["--approve", strict["approval_digest"]]) == 2
    assert not output.exists()
    assert not output.with_name(output.name + "-candidate").exists()


def test_tests_only_exec_scratch_never_constructs_model(git_repo, tmp_path, capsys, monkeypatch):
    from reviewflow.backend import live_repair_cli

    args, _, _ = cli_fixture(git_repo, tmp_path)
    args += ["--tests-only", "--test-sandbox", "exec-scratch"]
    monkeypatch.setattr(live_repair_cli, "ResponsesAdapter",
                        lambda *a, **kw: pytest.fail("Zero-model test constructed adapter"))
    original = VolumeDockerRunner.__init__
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        return process()

    monkeypatch.setattr(VolumeDockerRunner, "__init__",
                        lambda self, image, **kw: original(self, image, **kw, execute=execute))
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert main(args + ["--approve", preview["approval_digest"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["accounting"]["requests_attempted"] == 0
    assert report["application"] is None
    assert report["patch_attempts"] == []
    assert "TMPDIR=/scratch" in next(c for c in commands if c[1] == "run")
    assert (git_repo / "a.py").read_bytes() == b"old\n"


def test_exec_scratch_timeout_still_cleans_container_and_volume():
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 1)
        return process()

    result = approved_run(VolumeDockerRunner(IMAGE, sandbox_profile="exec-scratch", execute=execute))
    assert result.status == "timeout"
    assert sum(c[1:3] == ["rm", "-f"] for c in commands) == 2
    assert commands[-1][1:3] == ["volume", "rm"]

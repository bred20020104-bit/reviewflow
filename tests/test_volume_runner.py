"""Product sandbox tests; never import or execute the user's target on the host."""

import subprocess

import pytest

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.validation import DockerRunner

IMAGE = "sha256:" + "a" * 64


def process(code=0):
    return subprocess.CompletedProcess([], code)


def test_immutable_local_image_can_execute_approved_check():
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        return process()

    runner = DockerRunner(IMAGE, execute=execute)
    gate = ApprovalGate()
    files = {"tests/test_one.py": b"def test_one(): assert True\n"}
    intent = runner.intent(files, "regression")
    result = runner.run(files, "regression", gate, gate.approve(intent, confirmed=True))
    assert result.status == "passed"
    assert runner.owns(result)
    command = next(c for c in commands if c[1] == "run")
    assert command[command.index("python") + 1] == IMAGE


@pytest.mark.parametrize("image", [
    "python:latest", "sha256:" + "a" * 63, "sha256:" + "A" * 64,
    "sha256:" + "g" * 64, "sha256:" + "a" * 64 + "\n", "-evil@sha256:" + "a" * 64,
])
def test_mutable_or_malformed_image_is_rejected(image):
    with pytest.raises(ValueError):
        DockerRunner(image)


def volume_runner(**kwargs):
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    return VolumeDockerRunner(IMAGE, **kwargs)


def approved_run(runner, files=None):
    gate = ApprovalGate()
    files = files or {"pkg/tests/test_ok.py": b"def test_ok(): assert True\n"}
    return runner.run(files, "repository", gate,
                      gate.approve(runner.intent(files, "repository"), confirmed=True))


def test_volume_copy_preserves_explicit_committed_executable_modes():
    import stat
    from pathlib import Path

    def execute(command, **kwargs):
        if command[1] == "cp":
            root = Path(command[2])
            assert stat.S_IMODE((root / "run.sh").stat().st_mode) == 0o755
            assert stat.S_IMODE((root / "a.py").stat().st_mode) == 0o644
        return process()

    runner = volume_runner(execute=execute, executable_paths=("run.sh",))
    files = {"run.sh": b"#!/bin/sh\nexit 0\n", "a.py": b"x = 1\n"}
    assert runner.intent(files)["executable_paths"] == ["run.sh"]
    assert approved_run(runner, files).status == "passed"


@pytest.mark.parametrize("paths", [("missing.sh",), ("../outside.sh",), ("run.sh", "run.sh")])
def test_invalid_executable_manifest_never_reaches_docker(paths):
    from reviewflow.backend.repair import RepairError

    with pytest.raises((RepairError, ValueError)):
        runner = volume_runner(executable_paths=paths,
                               execute=lambda *args, **kwargs: pytest.fail("Unexpected Docker operation"))
        approved_run(runner, {"run.sh": b"exit 0\n"})


def test_volume_export_never_exposes_host_mount_and_preserves_test_bytes():
    from pathlib import Path

    from reviewflow.backend.approval import Denied

    commands = []
    source = b"def test_ok(): assert True\n"

    def execute(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            assert hasattr(kwargs["stdout"], "write")  # bounded structured channel only
        else:
            assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        if command[1] == "cp":
            assert (Path(command[2]) / "pkg/tests/test_ok.py").read_bytes() == source
        return process()

    runner = volume_runner(execute=execute)
    files = {"pkg/tests/test_ok.py": source, "pkg/run.sh": b"#!/bin/sh\nexit 0\n",
             "pkg/.env.example": b"EXAMPLE=placeholder\n"}
    gate = ApprovalGate()
    with pytest.raises(Denied):
        runner.run(files, "repository", gate, "invalid")
    assert commands == []
    result = approved_run(runner, files)
    assert result.status == "passed"
    assert result.execution == "container" and runner.owns(result)
    assert not runner.owns(__import__("dataclasses").replace(result))
    run = next(c for c in commands if c[1] == "run")
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                 "--cpus=1", "--user=65534:65534", "--pull=never"):
        assert flag in run
    mount = run[run.index("--mount") + 1]
    assert mount.startswith("type=volume,") and mount.endswith(",target=/workspace,readonly")
    assert not any("type=bind" in arg or "docker.sock" in arg for c in commands for arg in c)
    assert commands[-1][1:3] == ["volume", "rm"]
    assert sum(c[1] == "run" for c in commands) == 1


@pytest.mark.parametrize("files", [
    {"../escape.py": b"x"}, {".git/config": b"x"}, {".env": b"secret"},
    {"pkg/.env.production": b"secret"}, {"secrets.local.toml": b"secret"},
    {".git/.env.example": b"x"}, {"x.py": b"\x00"}, {"x.py": b"\xff"},
    {"a.py": b"x", "A.py": b"y"}, {"a.py": b"x", "a.py/b.py": b"y"},
    {"x.py": b"x" * 262145}, {f"x{i}.py": b"" for i in range(201)},
    {"x.py": "not bytes"}, {"x.exe": b"x"}, {"bad\n.py": b"x"},
])
def test_volume_invalid_exports_reject_before_any_docker_operation(files):
    from reviewflow.backend.repair import RepairError

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid export reached Docker")

    runner = volume_runner(execute=forbidden)
    with pytest.raises(RepairError):
        runner.intent(files, "repository")


@pytest.mark.parametrize("stage", ["inspect", "volume", "create", "cp"])
def test_volume_preparation_failure_never_executes_tests(stage):
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        failed = ((stage == "inspect" and command[1:3] == ["image", "inspect"])
                  or (stage == "volume" and command[1:3] == ["volume", "create"])
                  or command[1] == stage)
        return process(1 if failed else 0)

    result = approved_run(volume_runner(execute=execute))
    assert result.status != "passed"
    assert not any(c[1] == "run" for c in commands)


@pytest.mark.parametrize("code,status", [(0, "passed"), (1, "failed"), (2, "blocked"),
    (3, "blocked"), (4, "blocked"), (5, "blocked"), (125, "blocked"), (137, "blocked")])
def test_repository_exit_codes_cannot_confuse_setup_errors_with_bug_failures(code, status):
    result = approved_run(volume_runner(execute=lambda c, **kw: process(code if c[1] == "run" else 0)))
    assert result.status == status and result.exit_code == code


def test_volume_timeout_attempts_container_and_volume_cleanup():
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 1)
        return process()

    result = approved_run(volume_runner(execute=execute))
    assert result.status == "timeout"
    assert len([c for c in commands if c[1:3] == ["rm", "-f"]]) == 2
    assert commands[-1][1:3] == ["volume", "rm"]


def test_volume_export_remains_readable_to_container_under_private_host_umask():
    import os
    import stat
    from pathlib import Path

    def execute(command, **kwargs):
        if command[1] == "cp":
            root = Path(command[2])
            assert stat.S_IMODE(root.stat().st_mode) == 0o755
            assert stat.S_IMODE((root / "pkg/tests").stat().st_mode) == 0o755
        return process()

    original = os.umask(0o077)
    try:
        assert approved_run(volume_runner(execute=execute)).status == "passed"
    finally:
        os.umask(original)


def test_large_controller_is_streamed_without_exec_argument_limit():
    observed = []
    def execute(command, **kwargs):
        if command[1] == "run":
            assert command[-1] == "-"
            assert "--interactive" in command
            script = kwargs["input"]
            assert isinstance(script, bytes) and b"project109" in script
            observed.append(True)
        return process()
    files = {f"project{i:03d}/tests/test_ok.py": b"def test_ok(): pass\n" for i in range(110)}
    assert approved_run(volume_runner(execute=execute), files).status == "passed"
    assert observed == [True]


def test_preview_binds_project_layout_and_rejects_changed_scope():
    from reviewflow.backend.approval import Denied

    runner = volume_runner(execute=lambda *a, **kw: pytest.fail("Unapproved Docker call"))
    files = {"a/tests/test_one.py": b"def test_one(): pass\n"}
    intent = runner.intent(files)
    assert intent["test_projects"] == [{"root": "a", "files": ["a/tests/test_one.py"]}]
    gate = ApprovalGate()
    grant = gate.approve(intent, confirmed=True)
    with pytest.raises(Denied):
        runner.run({"b/tests/test_one.py": files["a/tests/test_one.py"]}, "repository", gate, grant)


@pytest.mark.parametrize("code,diagnostic", [
    (0, "tests_passed"), (1, "test_assertions_failed"), (5, "no_tests_executed"),
    (20, "test_collection_error"), (21, "test_setup_error"),
    (22, "test_teardown_error"), (23, "test_internal_error"), (24, "test_usage_error"),
    (137, "container_execution_error"),
])
def test_volume_results_carry_fixed_safe_diagnostic(code, diagnostic):
    runner = volume_runner(execute=lambda c, **kw: process(code if c[1] == "run" else 0))
    result = approved_run(runner)
    assert result.diagnostic_code == diagnostic
    assert result.status == ("passed" if code == 0 else "failed" if code == 1 else "blocked")


def test_volume_cleanup_failure_overrides_test_success():
    def execute(command, **kwargs):
        return process(1 if command[1:3] == ["volume", "rm"] else 0)

    result = approved_run(volume_runner(execute=execute))
    assert result.status == "cleanup_failed" and result.exit_code == 0


@pytest.mark.parametrize("body,expected", [
    ("def test_one(): assert True\n", 0),
    ("def test_one(): assert False\n", 1),
    ("import pytest\n@pytest.mark.skip(reason='fixture')\ndef test_one(): pass\n", 5),
    ("import pytest\n@pytest.mark.xfail\ndef test_one(): assert False\n", 5),
    ("import pytest\n@pytest.fixture\ndef broken(): raise RuntimeError('fixture')\ndef test_one(broken): pass\n", 3),
    ("raise ImportError('fixture')\n", 3),
    ("x = 1\n", 5),
])
def test_fixed_pytest_wrapper_classifies_real_product_owned_fixtures(tmp_path, body, expected):
    import os
    import sys

    from reviewflow.backend.volume_runner import repository_script

    nested = tmp_path / "pkg" / "tests"
    nested.mkdir(parents=True)
    (nested / "test_fixture.py").write_text(body)
    # A target config cannot silently skip the selected recursive discovery root.
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --ignore=pkg\ntestpaths = nonexistent\n")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "DYLD_LIBRARY_PATH")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", repository_script(str(tmp_path))],
                            env=env, capture_output=True, timeout=20)
    assert result.returncode == expected, result.stderr.decode(errors="replace")

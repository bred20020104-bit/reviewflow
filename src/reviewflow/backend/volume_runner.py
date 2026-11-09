"""Approved recursive pytest in Docker-owned storage, without host directory shares.

The export helper is never started. Only the fixed test container executes code.
Results attest process outcomes, not the honesty or completeness of target tests.
Raw output and individual test counts are deliberately not retained.
"""

import os
import secrets
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path, PurePosixPath

from pydantic import TypeAdapter

from reviewflow.backend.bounded_output import BoundedOutput
from reviewflow.backend.repair import RepairError
from reviewflow.backend.test_diagnostics import DIAGNOSTIC_POLICY, parse_diagnostics
from reviewflow.backend.test_projects import project_script, test_projects
from reviewflow.backend.validation import DockerRunner, ToolRun, content_digest
from reviewflow.policy import path_reason
from reviewflow.review.contracts import RelativePath


def repository_script(root="/workspace"):
    """Fixed pytest policy; root is a trusted controller path, never model code."""
    arguments = [
        "-q", "-p", "no:cacheprovider", "-c", "/dev/null",
        f"--rootdir={root}", f"--confcutdir={root}",
        "-o", "addopts=", "-o", "python_files=test_*.py *_test.py", root,
    ]
    return f"""import sys, pytest
sys.path.insert(0, {root!r})
class Outcomes:
    def __init__(self):
        self.collected = 0
        self.executed = 0
        self.skipped = 0
        self.phase_errors = 0
    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
    def pytest_runtest_logreport(self, report):
        if report.skipped:
            self.skipped += 1
        if report.when == 'call' and not report.skipped:
            self.executed += 1
        elif report.when != 'call' and report.failed:
            self.phase_errors += 1
outcomes = Outcomes()
code = int(pytest.main({arguments!r}, plugins=[outcomes]))
if code in (2, 3, 4) or outcomes.phase_errors:
    code = 3
elif not outcomes.collected or not outcomes.executed:
    code = 5
raise SystemExit(code)
"""


def _export_manifest(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 200:
        raise RepairError("Invalid sandbox export scope")
    total, normalized = 0, set()
    try:
        for path, data in files.items():
            TypeAdapter(RelativePath).validate_python(path, strict=True)
            if any(ord(char) < 32 or ord(char) == 127 for char in path):
                raise ValueError()
            reason = path_reason(path, "file")
            parsed = PurePosixPath(path.lower())
            permitted_local = (
                reason == "sensitive_path" and parsed.name == ".env.example"
            ) or (reason == "unsupported_type" and parsed.suffix == ".sh")
            if (reason and not permitted_local) or not isinstance(data, bytes):
                raise ValueError()
            total += len(data)
            if len(data) > 262144 or total > 8388608 or b"\0" in data:
                raise ValueError()
            data.decode("utf-8")
            key = unicodedata.normalize("NFC", path).casefold()
            if key in normalized:
                raise ValueError()
            normalized.add(key)
        for key in normalized:
            if any(str(parent) in normalized for parent in PurePosixPath(key).parents):
                raise ValueError()
    except ValueError:
        raise RepairError("Invalid sandbox export scope") from None
    return content_digest(files)


def sandbox_policy(profile):
    """Fresh fixed policy, never model/user-provided mount flags or host paths."""
    if type(profile) is not str or profile not in ("strict", "exec-scratch"):
        raise RepairError("Invalid test sandbox profile")
    tmpfs = ["/tmp:rw,noexec,nosuid,size=32m"]
    environment = []
    if profile == "exec-scratch":
        tmpfs.append("/scratch:rw,exec,nosuid,nodev,size=32m,mode=1777")
        environment.append("TMPDIR=/scratch")
    return {"version": 1, "profile": profile, "tmpfs": tmpfs, "environment": environment}


class _PreparationFailed(Exception):
    pass


class VolumeDockerRunner(DockerRunner):
    """A separate repository suite; never mints old publication receipts."""

    def __init__(self, image, *, executable_paths=(), timeout_seconds=60,
                 sandbox_profile="strict", execute=subprocess.run):
        super().__init__(image, timeout_seconds=timeout_seconds, execute=execute)
        sandbox_policy(sandbox_profile)
        self.sandbox_profile = sandbox_profile
        if (not isinstance(executable_paths, tuple) or any(type(p) is not str for p in executable_paths)
                or len(set(executable_paths)) != len(executable_paths)):
            raise RepairError("Invalid executable manifest")
        self.executable_paths = tuple(sorted(executable_paths))

    def intent(self, files, suite="repository"):
        if not set(self.executable_paths) <= set(files):
            raise RepairError("Executable manifest outside approved export")
        if suite != "repository":
            raise RepairError("Invalid sandbox suite")
        digest = _export_manifest(files)
        return {
            "action": "execute-tests",
            "digest": digest,
            "suite": suite,
            "image": self.image,
            "timeout_seconds": self.timeout,
            "network": "none",
            "sandbox": sandbox_policy(self.sandbox_profile),
            "command": "fixed-project-pytest-v3",
            "diagnostics": dict(DIAGNOSTIC_POLICY),
            "test_projects": test_projects(files),
            "transfer": "docker-volume-copy",
            "logs": "discarded",
            "test_counts": "not-retained",
            "executable_paths": list(self.executable_paths),
            "local_only_paths": sorted(
                path for path in files if path_reason(path, "file")
            ),
        }

    def run(self, files, suite, gate, grant):
        files = dict(files)
        intent = self.intent(files, suite)
        gate.consume(grant, intent)
        started = time.monotonic()
        name = "reviewflow-" + secrets.token_hex(16)
        helper, volume = name + "-export", name + "-data"
        status, code = "blocked", None
        failures, diagnostic_state = (), "not_collected"
        cleanup = []
        kwargs = {
            "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "check": False,
        }

        def prepare(command):
            if self._execute(command, timeout=self.timeout, **kwargs).returncode != 0:
                raise _PreparationFailed()

        try:
            with tempfile.TemporaryDirectory(prefix="reviewflow-export-") as temporary:
                root = Path(temporary) / "export"
                root.mkdir(mode=0o755)
                root.chmod(0o755)
                for path, data in files.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o755 if path in intent["executable_paths"] else 0o644)
                    for parent in target.parents:
                        if parent == root:
                            break
                        parent.chmod(0o755)
                prepare(["docker", "image", "inspect", self.image])
                cleanup.append(["docker", "volume", "rm", volume])
                prepare(["docker", "volume", "create", volume])
                cleanup.append(["docker", "rm", "-f", helper])
                prepare([
                    "docker", "create", "--name", helper, "--pull=never",
                    "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--mount", f"type=volume,source={volume},target=/workspace,volume-nocopy",
                    "--entrypoint", "python", self.image, "-I", "-B", "-c", "pass",
                ])
                prepare(["docker", "cp", str(root) + "/.", helper + ":/workspace"])
                cleanup.append(["docker", "rm", "-f", name])
                command = [
                    "docker", "run", "--name", name, "--pull=never", "--interactive",
                    "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=64",
                    "--memory=256m", "--cpus=1", "--user=65534:65534",
                    "--ulimit", "nofile=128:128", "--log-driver=none",
                    *[arg for mount in intent["sandbox"]["tmpfs"] for arg in ("--tmpfs", mount)],
                    *[arg for value in intent["sandbox"]["environment"] for arg in ("--env", value)],
                    "--mount", f"type=volume,source={volume},target=/workspace,readonly",
                    "--workdir", "/workspace",
                    "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "HOME=/tmp",
                    "--entrypoint", "python", self.image, "-I", "-B", "-",
                ]
                script = project_script(intent["test_projects"]).encode()
                with BoundedOutput(DIAGNOSTIC_POLICY["max_bytes"]) as capture:
                    code = self._execute(command, input=script, timeout=self.timeout,
                        **{**kwargs, "stdout": capture.writer}).returncode
                if capture.overflow:
                    diagnostic_state = "oversized"
                elif capture.incomplete:
                    diagnostic_state = "partial"
                else:
                    failures, diagnostic_state = parse_diagnostics(bytes(capture.data), files)
                if code == 0 and failures:
                    failures, diagnostic_state = (), "invalid"
                elif code == 1 and not failures and diagnostic_state == "complete":
                    diagnostic_state = "partial"
                status = "passed" if code == 0 else "failed" if code == 1 else "blocked"
        except subprocess.TimeoutExpired:
            status = "timeout"
        except (OSError, _PreparationFailed):
            status = "blocked"
        finally:
            for command in reversed(cleanup):
                try:
                    if self._execute(command, timeout=10, **kwargs).returncode != 0:
                        status = "cleanup_failed"
                except (OSError, subprocess.TimeoutExpired):
                    status = "cleanup_failed"
        diagnostic = {
            0: "tests_passed", 1: "test_assertions_failed", 5: "no_tests_executed",
            20: "test_collection_error", 21: "test_setup_error", 22: "test_teardown_error",
            23: "test_internal_error", 24: "test_usage_error",
        }.get(code, "container_execution_error" if code is not None else "sandbox_preparation_error")
        if status in {"timeout", "cleanup_failed"}:
            diagnostic = "sandbox_" + status
        result = ToolRun(
            name, intent["digest"], self.image, suite, status, code,
            "container", time.monotonic() - started, diagnostic, failures, diagnostic_state,
        )
        self._records[name] = result
        return result

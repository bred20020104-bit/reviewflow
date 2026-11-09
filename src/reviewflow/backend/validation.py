"""Fixed, explicitly approved Docker checks and process-local validation receipts.

No shell strings from targets, dependency installation, image pulls, host fallback,
raw target logs or model-supplied exit codes. Container isolation requires separate
platform validation; a worktree alone is never an execution sandbox.
"""

import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.repair import RepairError
from reviewflow.backend.test_diagnostics import TestFailure
from reviewflow.policy import path_reason
from reviewflow.review.contracts import RelativePath
from reviewflow.review.workflow_models import RunReport


def content_digest(files):
    import hashlib

    return fingerprint({p: hashlib.sha256(data).hexdigest() for p, data in files.items()})


@dataclass(frozen=True)
class ToolRun:
    run_id: str
    content_digest: str
    image: str
    suite: str
    status: str
    exit_code: int | None
    execution: str
    seconds: float
    diagnostic_code: str | None = None
    test_failures: tuple[TestFailure, ...] = ()
    diagnostics_status: str = "not_collected"


class DockerRunner:
    def __init__(self, image: str, *, timeout_seconds=60, execute=subprocess.run):
        if not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[a-f0-9]{64}", image):
            raise ValueError("Sandbox image requires an explicit immutable digest")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("Invalid sandbox timeout")
        self.image = image
        self.timeout = timeout_seconds
        self._execute = execute
        self._records = {}

    def intent(self, files, suite):
        if suite not in {"regression", "reproducer"} or not 1 <= len(files) <= 200:
            raise RepairError("Invalid sandbox suite or file count")
        total = 0
        try:
            for path, data in files.items():
                TypeAdapter(RelativePath).validate_python(path, strict=True)
                if path_reason(path, "file") or not isinstance(data, bytes):
                    raise ValueError()
                total += len(data)
                if len(data) > 262144 or total > 8388608:
                    raise ValueError()
        except ValueError:
            raise RepairError("Invalid sandbox export scope") from None
        return {
            "action": "execute-tests",
            "digest": content_digest(files),
            "suite": suite,
            "image": self.image,
            "timeout_seconds": self.timeout,
            "network": "none",
            "command": "fixed-pytest",
            "logs": "discarded",
        }

    def run(self, files, suite, gate, grant):
        files = dict(files)
        intent = self.intent(files, suite)
        gate.consume(grant, intent)
        started = time.monotonic()
        name = "reviewflow-" + secrets.token_hex(16)
        status, code = "blocked", None
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        # No raw target output is retained: it may contain credentials or flood logs.
        kwargs = {
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "check": False,
        }
        with tempfile.TemporaryDirectory(prefix="reviewflow-check-") as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            for path, data in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o644)
            target = (
                "/workspace/tests"
                if suite == "regression"
                else "/workspace/tests/test_reviewflow_reproducer.py"
            )
            arguments = ["-q", "-p", "no:cacheprovider", target]
            if suite == "regression":
                arguments += ["--ignore=/workspace/tests/test_reviewflow_reproducer.py"]
            script = (
                "import sys, pytest; sys.path.insert(0, '/workspace'); "
                f"raise SystemExit(pytest.main({arguments!r}))"
            )
            command = [
                "docker",
                "run",
                "--name",
                name,
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--user=65534:65534",
                "--ulimit",
                "nofile=128:128",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=32m",
                "--mount",
                f"type=bind,source={root},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                "--env",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "HOME=/tmp",
                "--entrypoint",
                "python",
                self.image,
                "-I",
                "-B",
                "-c",
                script,
            ]
            launched = False
            try:
                if "," in str(root):
                    raise OSError("Unsupported temporary directory")
                image = self._execute(
                    ["docker", "image", "inspect", self.image], timeout=10, **kwargs
                )
                if image.returncode == 0:
                    launched = True
                    result = self._execute(command, timeout=self.timeout, **kwargs)
                    code = result.returncode
                    status = (
                        "passed"
                        if code == 0
                        else "blocked"
                        if code in {125, 126, 127}
                        else "failed"
                    )
            except subprocess.TimeoutExpired:
                status = "timeout"
            except OSError:
                status = "blocked"
            finally:
                if launched:
                    try:
                        removal = self._execute(["docker", "rm", "-f", name], timeout=10, **kwargs)
                        if removal.returncode != 0:
                            status = "cleanup_failed"
                    except (OSError, subprocess.TimeoutExpired):
                        status = "cleanup_failed"
        result = ToolRun(
            name,
            intent["digest"],
            self.image,
            suite,
            status,
            code,
            "container",
            time.monotonic() - started,
        )
        self._records[name] = result
        return result

    def owns(self, result):
        return isinstance(result, ToolRun) and self._records.get(result.run_id) is result


class ValidationAuthority:
    """Only results minted by this runner plus a new trusted real review can authorize commit."""

    def __init__(self, runner):
        self.runner = runner
        self._receipts = {}

    def finish(
        self,
        ws,
        baseline_regression,
        baseline_reproducer,
        candidate_regression,
        candidate_reproducer,
        review,
    ):
        digest = ws.current_digest()
        checks = (
            baseline_regression,
            baseline_reproducer,
            candidate_regression,
            candidate_reproducer,
        )
        expected = (
            (ws.baseline_digest, "regression", "passed"),
            (ws.baseline_digest, "reproducer", "failed"),
            (digest, "regression", "passed"),
            (digest, "reproducer", "passed"),
        )
        if digest == ws.baseline_digest:
            raise RepairError("No changed candidate to validate")
        for result, (bound, suite, status) in zip(checks, expected):
            if (
                not self.runner.owns(result)
                or result.execution != "container"
                or result.content_digest != bound
                or result.suite != suite
                or result.status != status
            ):
                raise RepairError("Validation incomplete or mismatched")
        if baseline_reproducer.exit_code != 1:
            raise RepairError("Baseline did not reproduce an assertion failure")
        _, inventory = ws.inventory()
        if (
            not isinstance(review, RunReport)
            or review.execution != "real"
            or not review.semantic_review_performed
            or review.mode != "multi"
            or review.snapshot_id != inventory.snapshot.snapshot_id
            or review.status != "complete"
            or review.errors
            or review.unreviewed_paths
            or set(review.supplied_paths) != set(ws._expected)
            or set(review.reported_reviewed_paths) != set(ws._expected)
            or any(
                not f.verification or f.verification.verdict != "rejected" for f in review.findings
            )
        ):
            raise RepairError("Independent real re-review required for final snapshot")
        token = secrets.token_hex(32)
        self._receipts[token] = (ws, digest)
        return token

    def verify(self, ws, token):
        if self._receipts.get(token) != (ws, ws.current_digest()):
            raise RepairError("Final validation receipt missing or stale")

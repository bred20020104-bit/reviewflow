"""Owned Git worktrees with manual blob materialization (no checkout filters).

This is not an OS sandbox. The tool installation, workspace parent and invoking
Python process are trusted. No automatic cleanup of potentially user-edited files.
"""

import hashlib
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.nofollow import (
    directory_descriptors_available,
    open_exclusive,
    replace_regular,
)
from reviewflow.backend.repair import (
    CheckedPatch,
    PartialApplicationError,
    PatchProposal,
    RepairError,
    editable,
)
from reviewflow.models import ScanPolicy
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def _git(root, args, *, data=None, timeout=30):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    command = [
        "git",
        "--no-pager",
        "-C",
        str(root),
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "submodule.recurse=false",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.quotePath=false",
        *args,
    ]
    try:
        result = subprocess.run(
            command, input=data, capture_output=True, env=env, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RepairError("Git operation unavailable or timed out; preserve workspace") from None
    if result.returncode:
        raise RepairError("Git operation failed; preserve workspace")
    return result.stdout


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _replace(root: Path, path: str, expected: bytes, content: bytes):
    """No-follow traversal and exclusive temporary file; never write through links."""
    if not directory_descriptors_available():
        try:
            replace_regular(root, path, expected, content)
        except OSError:
            raise RepairError("Workspace changed or inaccessible") from None
        return
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".reviewflow-" + secrets.token_hex(12)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise RepairError("Workspace changed")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                current = stream.read(len(expected) + 1)
            if current != expected:
                raise RepairError("Workspace changed")
        finally:
            os.close(file_fd)
        out = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(info.st_mode),
            dir_fd=fd,
        )
        with os.fdopen(out, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(content)
        check = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if (check.st_ino, check.st_mtime_ns, check.st_size) != (
            info.st_ino,
            info.st_mtime_ns,
            info.st_size,
        ):
            raise RepairError("Workspace changed")
        os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
    except OSError:
        raise RepairError("Workspace changed or inaccessible") from None
    finally:
        # Only a randomly named file created by this operation may be removed.
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


class Workspace:
    @staticmethod
    def creation_intent(source, inventory, storage: Path, run_id: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", run_id):
            raise RepairError("Invalid run ID")
        if source.info.mode != "head" or not source.info.commit:
            raise RepairError("Repair requires committed Git HEAD")
        if storage.is_symlink() or not storage.is_dir():
            raise RepairError("Trusted existing workspace parent required")
        parent = storage.resolve()
        root = source.info.root.resolve()
        if parent == root or root in parent.parents:
            raise RepairError("Workspace parent must be outside the target repository")
        return {
            "action": "create-worktree",
            "root": str(root),
            "base": source.info.commit,
            "snapshot": inventory.snapshot.snapshot_id,
            "destination": str(parent / run_id),
            "branch": "reviewfix/" + run_id,
        }

    @classmethod
    def create(cls, source, inventory, storage, run_id, gate, grant):
        from reviewflow.review.context import ReviewContext

        intent = cls.creation_intent(source, inventory, storage, run_id)
        context = ReviewContext(source, inventory)
        entries = tuple(source.entries())
        included = {r.path for r in inventory.snapshot.records if r.status == "included"}
        if {e.path for e in entries} != included or any(e.entry_type != "file" for e in entries):
            raise RepairError("Repair requires a fully included regular-text Git tree")
        tree = _git(source.info.root, ["ls-tree", "-r", "-z", source.info.commit])
        if any(entry.split(b" ", 1)[0] != b"100644" for entry in tree.split(b"\0") if entry):
            raise RepairError("Repair does not support executable Git entries")
        contents = {path: context.read_file(path).encode("utf-8") for path in sorted(included)}
        path = Path(intent["destination"])
        audit_path = path.with_name(path.name + ".audit.jsonl")
        if os.path.lexists(path) or os.path.lexists(audit_path):
            raise RepairError("Workspace destination or audit already exists")
        gate.consume(grant, intent)
        _git(
            source.info.root,
            [
                "worktree",
                "add",
                "--no-checkout",
                "-b",
                intent["branch"],
                str(path),
                source.info.commit,
            ],
        )
        # No checkout, sparse-checkout, filter, hook, target command or symlink materialization.
        _git(path, ["read-tree", source.info.commit])
        for relative, data in contents.items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        ws = cls()
        ws.path = path
        ws.root = source.info.root
        ws.base = source.info.commit
        ws.branch = intent["branch"]
        ws.snapshot = inventory.snapshot.snapshot_id
        ws.policy = inventory.snapshot.policy
        ws._baseline = contents.copy()
        ws._expected = contents.copy()
        ws._previous = None
        ws._identity = (path.stat().st_dev, path.stat().st_ino)
        ws._git_pointer = (path / ".git").read_bytes()
        ws.attempts = {}
        ws._receipt = None
        ws._committed = None
        ws.baseline_digest = ws.current_digest()
        ws.audit_path = audit_path
        fd = open_exclusive(audit_path)
        info = os.fstat(fd)
        os.close(fd)
        ws._audit_identity = (info.st_dev, info.st_ino)
        ws._audit_size = 0
        ws.record_event(
            {
                "event": "created",
                "base": ws.base,
                "snapshot": ws.snapshot,
                "branch": ws.branch,
                "baseline_digest": ws.baseline_digest,
            }
        )
        return ws

    def record_event(self, event):
        import json

        try:
            fd = open_exclusive(self.audit_path, append=True)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != self._audit_identity
                    or info.st_size != self._audit_size
                ):
                    raise RepairError("Workspace audit changed")
                data = (json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n").encode()
                with os.fdopen(fd, "ab", closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(fd)
                self._audit_size += len(data)
            finally:
                os.close(fd)
        except OSError:
            raise RepairError("Workspace audit changed or unavailable") from None

    def current_digest(self):
        from reviewflow.backend.config import read_bounded

        try:
            if read_bounded(self.path / ".git", 4096) != self._git_pointer:
                raise RepairError("Workspace Git metadata changed")
        except (OSError, ValueError):
            raise RepairError("Workspace Git metadata changed") from None
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._identity:
            raise RepairError("Workspace changed")
        source = DirectorySource(self.path)
        entries = [e for e in source.entries() if e.path != ".git" and e.entry_type != "directory"]
        if {e.path for e in entries} != self._expected.keys():
            raise RepairError("Workspace changed: unknown or missing files")
        for entry in entries:
            if entry.entry_type != "file" or entry.size_bytes != len(self._expected[entry.path]):
                raise RepairError("Workspace changed: nonregular file or size mismatch")
            if (self.path / entry.path).lstat().st_mode & 0o111:
                raise RepairError("Workspace executable permissions changed")
            data = source.read_bytes(entry, max_bytes=max(len(self._expected[entry.path]), 1))
            if data != self._expected[entry.path]:
                raise RepairError("Workspace changed")
        if _git(self.path, ["rev-parse", "HEAD"]).decode().strip() != (
            self._committed or self.base
        ):
            raise RepairError("Workspace HEAD changed")
        return fingerprint({p: _sha(v) for p, v in self._expected.items()})

    def patch_intent(self, patch):
        return {
            "action": "repair",
            "workspace": str(self.path),
            "base": self.base,
            "snapshot": self.snapshot,
            "patch": patch.sha256,
            "finding": patch.proposal.finding_id,
        }

    def apply(self, patch: CheckedPatch, gate, grant):
        if self._committed or self._previous is not None:
            raise RepairError("Finish current attempt before applying another")
        proposal = PatchProposal.model_validate(patch.proposal)
        if (
            proposal.snapshot_id != self.snapshot
            or _sha(proposal.model_dump_json().encode()) != patch.sha256
        ):
            raise RepairError("Invalid patch binding")
        if self.attempts.get(proposal.finding_id, 0) >= 2:
            raise RepairError("Repair attempt limit")
        self.current_digest()
        seen = set()
        for r in proposal.replacements:
            if (
                r.path in seen
                or not editable(r.path)
                or r.path not in self._expected
                or _sha(self._expected[r.path]) != r.before_sha256
                or len(r.content.encode()) > 65536
                or "\0" in r.content
            ):
                raise RepairError("Invalid replacement or changed base")
            seen.add(r.path)
        gate.consume(grant, self.patch_intent(patch))
        attempt = self.attempts.get(proposal.finding_id, 0) + 1
        self.record_event(
            {
                "event": "attempt",
                "finding_id": proposal.finding_id,
                "attempt": attempt,
                "patch_sha256": patch.sha256,
            }
        )
        self.attempts[proposal.finding_id] = attempt
        self._previous = self._expected.copy()
        self._receipt = None
        try:
            for r in proposal.replacements:
                content = r.content.encode()
                _replace(self.path, r.path, self._expected[r.path], content)
                self._expected[r.path] = content
        except (RepairError, OSError):
            try:
                self.rollback_attempt()
            except Exception:
                raise PartialApplicationError(
                    "Partial application could not be restored safely; preserve workspace"
                ) from None
            raise RepairError("Application failed; owned changes restored") from None

    def rollback_attempt(self):
        if self._previous is None:
            raise RepairError("No active attempt")
        self.current_digest()  # User edits are never overwritten during rollback.
        self.record_event({"event": "rollback", "candidate_digest": self.current_digest()})
        for path, content in self._previous.items():
            if content != self._expected[path]:
                _replace(self.path, path, self._expected[path], content)
        self._expected = self._previous
        self._previous = None
        self._receipt = None

    def inventory(self):
        self.current_digest()
        workspace = self

        class TrackedWorktreeSource:
            def __init__(self):
                self.reader = DirectorySource(workspace.path)
                self.info = self.reader.info

            def entries(self):
                workspace.current_digest()
                # The owned .git pointer is tool metadata, not part of the source tree.
                return tuple(e for e in self.reader.entries() if e.path in workspace._expected)

            def read_bytes(self, entry, *, max_bytes):
                workspace.current_digest()
                if entry.path not in workspace._expected:
                    raise RepairError("Unknown worktree source path")
                return self.reader.read_bytes(entry, max_bytes=max_bytes)

        source = TrackedWorktreeSource()
        return source, build_inventory(
            source,
            ScanPolicy(
                max_files=self.policy.max_files,
                max_file_bytes=max(self.policy.max_file_bytes, 65536),
                max_total_bytes=max(self.policy.max_total_bytes, 131072),
            ),
        )

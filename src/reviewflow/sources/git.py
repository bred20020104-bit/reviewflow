"""Immutable HEAD blobs via an allowlisted, noninteractive Git subprocess boundary."""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from reviewflow.errors import (
    CollectionError,
    GitCommandError,
    GitUnavailableError,
    InvalidInputError,
)
from reviewflow.models import SourceEntry, SourceInfo, SourceReader
from reviewflow.sources.directory import DirectorySource, normalize_root

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PROBE = ("rev-parse", "--is-inside-work-tree")
_TOP = ("rev-parse", "--show-toplevel")
_HEAD = ("rev-parse", "--verify", "HEAD^{commit}")
_BRANCH = ("symbolic-ref", "--quiet", "--short", "HEAD")
_STATUS = ("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all")
_FILTER_KEYS = (
    "config",
    "--null",
    "--name-only",
    "--get-regexp",
    r"^filter\..*\.(clean|smudge|process|required)$",
)
_FIXED = {_PROBE, _TOP, _HEAD, _BRANCH, _STATUS, _FILTER_KEYS}


class GitRunner:
    def run(self, root: Path, args: tuple[str, ...]) -> bytes:
        tree = (
            len(args) == 6
            and args[:5] == ("ls-tree", "-r", "-l", "-z", "--full-tree")
            and _OID.fullmatch(args[5])
        )
        blob = len(args) == 3 and args[:2] == ("cat-file", "blob") and _OID.fullmatch(args[2])
        if args not in _FIXED and not tree and not blob:
            raise GitCommandError("Git operation is not allowlisted.")
        overrides: list[str] = []
        if args == _STATUS:
            # Even `git status` can run configured clean/process filters. Neutralize
            # every configured driver before it inspects the working tree.
            keys = self._execute(root, _FILTER_KEYS)
            try:
                drivers = {key.rsplit(".", 1)[0] for key in keys.decode("utf-8").split("\0") if key}
            except UnicodeDecodeError as exc:
                raise GitCommandError("Invalid Git filter configuration encoding.") from exc
            for driver in sorted(drivers):
                for setting in ("clean=", "smudge=", "process=", "required=false"):
                    overrides.extend(("-c", f"{driver}.{setting}"))
        return self._execute(root, args, overrides)

    def _execute(
        self, root: Path, args: tuple[str, ...], overrides: list[str] | None = None
    ) -> bytes:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_PAGER": "cat",
                "GIT_ATTR_NOSYSTEM": "1",
                "LC_ALL": "C",
            }
        )
        command = [
            "git",
            "--no-pager",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.quotePath=false",
            "-c",
            "color.ui=false",
            *(overrides or []),
            *args,
        ]
        try:
            result = subprocess.run(command, env=env, capture_output=True, timeout=10, check=False)
        except FileNotFoundError as exc:
            raise GitUnavailableError("Git is not installed or not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError("Git inspection timed out after 10 seconds.") from exc
        except OSError as exc:
            raise GitCommandError("Git inspection could not start.") from exc
        if result.returncode == 1 and args in {_BRANCH, _FILTER_KEYS}:
            return b""
        if result.returncode:
            if (
                args == _PROBE
                and result.returncode == 128
                and b"not a git repository" in result.stderr
            ):
                raise InvalidInputError("Input is not a Git working tree.")
            raise GitCommandError(
                f"Read-only Git {args[0]} failed (exit {result.returncode}).",
                returncode=result.returncode,
            )
        return result.stdout


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitCommandError("Git metadata is not valid UTF-8.") from exc


def _has_git_marker(path: Path, *, include_bare_hint: bool = False) -> bool:
    for directory in (path, *path.parents):
        if os.path.lexists(directory / ".git"):
            return True
        # Only use a conservative bare-layout hint when Git cannot probe. Two
        # ordinary names (HEAD and objects) alone do not identify a repository.
        if (
            include_bare_hint
            and (directory / "HEAD").is_file()
            and (directory / "objects").is_dir()
            and (directory / "refs").is_dir()
        ):
            return True
    return False


class GitHeadSource:
    def __init__(self, root: Path, *, runner: GitRunner | None = None):
        requested = normalize_root(root)
        self._runner = runner or GitRunner()
        if self._runner.run(requested, _PROBE) != b"true\n":
            raise InvalidInputError(
                "Bare repositories are not supported; provide a Git working tree."
            )
        root = Path(_decode(self._runner.run(requested, _TOP)).removesuffix("\n")).resolve()
        try:
            commit = _decode(self._runner.run(root, _HEAD)).strip()
        except GitCommandError as exc:
            if exc.returncode == 128:
                raise InvalidInputError(
                    "Repository has no valid HEAD; use --source directory for current files."
                ) from exc
            raise
        if not _OID.fullmatch(commit):
            raise GitCommandError("Git returned an invalid HEAD object ID.")
        branch = _decode(self._runner.run(root, _BRANCH)).removesuffix("\n") or None
        dirty = bool(self._runner.run(root, _STATUS))
        warnings: list[str] = []
        if requested != root:
            warnings.append("Input subdirectory expanded to the Git repository root.")
        if dirty:
            warnings.append(
                "Uncommitted, staged and untracked changes are excluded from this HEAD snapshot."
            )
        self.info = SourceInfo("head", root, commit, branch, dirty, tuple(warnings))
        self._records: dict[str, SourceEntry] | None = None

    def entries(self) -> tuple[SourceEntry, ...]:
        if self._records is not None:
            return tuple(self._records.values())
        data = self._runner.run(
            self.info.root, ("ls-tree", "-r", "-l", "-z", "--full-tree", self.info.commit)
        )
        records: dict[str, SourceEntry] = {}
        try:
            if data and not data.endswith(b"\0"):
                raise ValueError("unterminated tree")
            for record in data.split(b"\0"):
                if not record:
                    continue
                metadata, name = record.split(b"\t", 1)
                mode, object_type, oid, size = metadata.split()
                path = _decode(name)
                parsed = PurePosixPath(path)
                if not path or parsed.is_absolute() or ".." in parsed.parts or path in records:
                    raise ValueError("invalid path")
                object_id = oid.decode("ascii")
                if not _OID.fullmatch(object_id):
                    raise ValueError("invalid object")
                if mode == b"160000" and object_type == b"commit":
                    entry_type, size_bytes = "submodule", None
                elif mode == b"120000" and object_type == b"blob":
                    entry_type, size_bytes = "symlink", int(size)
                elif mode in {b"100644", b"100755"} and object_type == b"blob":
                    entry_type, size_bytes = "file", int(size)
                else:
                    raise ValueError("unsupported tree mode")
                if size_bytes is not None and size_bytes < 0:
                    raise ValueError("negative size")
                records[path] = SourceEntry(path, entry_type, size_bytes, object_id)
        except (ValueError, UnicodeError) as exc:
            raise GitCommandError("Malformed Git tree metadata.") from exc
        self._records = records
        return tuple(records.values())

    def read_bytes(self, entry: SourceEntry, *, max_bytes: int) -> bytes:
        if self._records is None or self._records.get(entry.path) != entry:
            raise CollectionError("Only enumerated Git blobs can be read.")
        if (
            entry.entry_type != "file"
            or entry.size_bytes is None
            or max_bytes <= 0
            or entry.size_bytes > max_bytes
        ):
            raise CollectionError("Entry is not a bounded regular Git blob.")
        data = self._runner.run(self.info.root, ("cat-file", "blob", entry.object_id))
        if len(data) != entry.size_bytes or len(data) > max_bytes:
            raise CollectionError("Git blob size does not match the fixed tree.")
        return data


def open_source(path: Path, mode: Literal["auto", "head", "directory"]) -> SourceReader:
    root = normalize_root(path)
    if mode == "directory":
        return DirectorySource(root)
    if mode not in {"auto", "head"}:
        raise InvalidInputError("Unknown source mode.")
    runner = GitRunner()
    try:
        inside = runner.run(root, _PROBE)
    except GitUnavailableError:
        if mode == "auto" and not _has_git_marker(root, include_bare_hint=True):
            return DirectorySource(root)
        raise
    except InvalidInputError:
        if mode == "auto" and not _has_git_marker(root):
            return DirectorySource(root)
        raise
    if inside != b"true\n":
        raise InvalidInputError("Bare repositories are not supported.")
    return GitHeadSource(root, runner=runner)

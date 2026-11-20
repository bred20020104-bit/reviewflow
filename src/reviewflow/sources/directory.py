"""Bounded local reads, with no-follow descriptors for each path component."""

import os
import stat
from pathlib import Path, PurePosixPath

from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import SourceEntry, SourceInfo
from reviewflow.policy import is_excluded_directory

# POSIX walks use no-follow directory descriptors. Windows has neither
# O_DIRECTORY nor dir_fd, so those trees are walked by path and lstat.
_NOFOLLOW_DIR_FD = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
)


def normalize_root(root: Path) -> Path:
    try:
        root = root.expanduser()
        if root.is_symlink() or not root.is_dir():
            raise InvalidInputError("Input must be an existing directory, not a symlink.")
        return root.resolve(strict=True)
    except PermissionError as exc:
        raise CollectionError("Cannot resolve input directory.") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise InvalidInputError("Cannot resolve input directory.") from exc


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _windows_same_file(info: os.stat_result, stored: tuple[int, ...]) -> bool:
    """Ignore creation time. Windows lstat and fstat can disagree by about 1ms."""
    return _signature(info)[:5] == stored[:5]


class DirectorySource:
    def __init__(self, root: Path):
        root = normalize_root(root)
        self.info = SourceInfo("directory", root, None, None, None, ())
        self._root_identity = _signature(root.stat())[:2]
        self._records: dict[str, SourceEntry] | None = None
        self._signatures: dict[str, tuple[int, ...]] = {}

    def _open_root(self) -> int:
        fd = os.open(self.info.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if _signature(os.fstat(fd))[:2] != self._root_identity:
            os.close(fd)
            raise CollectionError("Input directory changed during collection.")
        return fd

    def entries(self) -> tuple[SourceEntry, ...]:
        if self._records is not None:
            return tuple(self._records.values())
        if not _NOFOLLOW_DIR_FD:
            return self._entries_by_path()
        records: dict[str, SourceEntry] = {}

        def visit(fd: int, prefix: str) -> None:
            with os.scandir(fd) as iterator:
                children = sorted(iterator, key=lambda e: e.name)
            for child in children:
                path = prefix + child.name
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    records[path] = SourceEntry(path, "symlink", info.st_size)
                elif stat.S_ISDIR(info.st_mode):
                    if is_excluded_directory(child.name):
                        records[path + "/"] = SourceEntry(path + "/", "directory", None)
                    else:
                        child_fd = os.open(
                            child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                        )
                        try:
                            visit(child_fd, path + "/")
                        finally:
                            os.close(child_fd)
                else:
                    size = info.st_size if stat.S_ISREG(info.st_mode) else None
                    records[path] = SourceEntry(path, "file", size)
                    self._signatures[path] = _signature(info)

        try:
            fd = self._open_root()
            try:
                visit(fd, "")
            finally:
                os.close(fd)
        except OSError as exc:
            raise CollectionError("Cannot enumerate directory safely.") from exc
        self._records = records
        return tuple(records.values())

    def _entries_by_path(self) -> tuple[SourceEntry, ...]:
        records: dict[str, SourceEntry] = {}
        try:
            if _signature(self.info.root.stat())[:2] != self._root_identity:
                raise CollectionError("Input directory changed during collection.")
            self._visit_path(self.info.root, "", records)
        except OSError as exc:
            raise CollectionError("Cannot enumerate directory safely.") from exc
        self._records = records
        return tuple(records.values())

    def _visit_path(self, directory: Path, prefix: str, records: dict[str, SourceEntry]) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            path = prefix + child.name
            # DirEntry.stat(follow_symlinks=False) reports st_dev/st_ino as 0 on
            # Windows, which would not match the later lstat/fstat identity.
            info = os.lstat(child.path)
            if stat.S_ISLNK(info.st_mode):
                records[path] = SourceEntry(path, "symlink", info.st_size)
            elif stat.S_ISDIR(info.st_mode):
                if is_excluded_directory(child.name):
                    records[path + "/"] = SourceEntry(path + "/", "directory", None)
                else:
                    self._visit_path(directory / child.name, path + "/", records)
            else:
                size = info.st_size if stat.S_ISREG(info.st_mode) else None
                records[path] = SourceEntry(path, "file", size)
                self._signatures[path] = _signature(info)

    def _read_by_path(self, entry: SourceEntry, path: PurePosixPath, *, max_bytes: int) -> bytes:
        fd: int | None = None
        try:
            current = self.info.root
            for part in path.parts[:-1]:
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise CollectionError(
                        "Cannot read file safely; it may have changed or be inaccessible."
                    )
            current = current / path.name
            info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not _windows_same_file(info, self._signatures[entry.path])
            ):
                raise CollectionError("File changed since enumeration.")
            # Windows os.open defaults to text mode and strips CR from CRLF.
            fd = os.open(os.fspath(current), os.O_RDONLY | getattr(os, "O_BINARY", 0))
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not _windows_same_file(
                before, self._signatures[entry.path]
            ):
                raise CollectionError("File changed since enumeration.")
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if (
                not _windows_same_file(os.fstat(fd), _signature(before))
                or len(data) != entry.size_bytes
            ):
                raise CollectionError("File changed during reading.")
            return data
        except CollectionError:
            raise
        except OSError as exc:
            raise CollectionError(
                "Cannot read file safely; it may have changed or be inaccessible."
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)

    def read_bytes(self, entry: SourceEntry, *, max_bytes: int) -> bytes:
        path = PurePosixPath(entry.path)
        if (
            max_bytes <= 0
            or "\0" in entry.path
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
        ):
            raise CollectionError("Invalid bounded file read.")
        if self._records is None or self._records.get(entry.path) != entry:
            raise CollectionError("Only enumerated entries can be read.")
        if entry.entry_type != "file" or entry.size_bytes is None or entry.size_bytes > max_bytes:
            raise CollectionError("Entry is not a bounded regular file.")
        if not _NOFOLLOW_DIR_FD:
            return self._read_by_path(entry, path, max_bytes=max_bytes)
        fds: list[int] = []
        try:
            fds.append(self._open_root())
            for part in path.parts[:-1]:
                fds.append(
                    os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
                )
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
            fds.append(fd)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or _signature(before) != self._signatures[entry.path]
            ):
                raise CollectionError("File changed since enumeration.")
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if _signature(os.fstat(fd)) != _signature(before) or len(data) != entry.size_bytes:
                raise CollectionError("File changed during reading.")
            return data
        except OSError as exc:
            raise CollectionError(
                "Cannot read file safely; it may have changed or be inaccessible."
            ) from exc
        finally:
            for fd in reversed(fds):
                os.close(fd)

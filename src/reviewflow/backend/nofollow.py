"""No-follow file access. POSIX keeps directory descriptors; Windows walks by lstat."""

import os
import stat
from pathlib import Path


def mode_matches(actual_mode: int, expected: int) -> bool:
    # Windows reports a fixed permission mask, so a Unix 0644 bit cannot be stored.
    if os.name != "posix":
        return True
    return stat.S_IMODE(actual_mode) == expected


def directory_descriptors_available() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )


def _binary(flags: int) -> int:
    if hasattr(os, "O_BINARY"):
        return flags | os.O_BINARY
    return flags


def _reject_symlink(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise OSError("Refusing to follow a symlink")
    return info


def resolve_regular(root: Path, relative: str) -> Path:
    """Return a regular file inside root. Any symlink component is refused."""
    if not relative or "\\" in relative or relative.startswith("/") or ".." in relative.split("/"):
        raise OSError("Path escapes the root")
    current = Path(root)
    info = _reject_symlink(current)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("Root is not a directory")
    parts = relative.split("/")
    for part in parts[:-1]:
        current = current / part
        info = _reject_symlink(current)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("Parent is not a directory")
    target = current / parts[-1]
    info = _reject_symlink(target)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Target is not a regular file")
    return target


def read_regular(root: Path, relative: str, max_bytes: int) -> tuple[bytes, tuple, tuple]:
    target = resolve_regular(root, relative)
    parents = []
    current = Path(root)
    for part in relative.split("/")[:-1]:
        current = current / part
        info = current.lstat()
        parents.append((info.st_dev, info.st_ino))
    root_info = Path(root).lstat()
    parents.insert(0, (root_info.st_dev, root_info.st_ino))
    fd = os.open(target, _binary(os.O_RDONLY))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise OSError("File changed or unsupported")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    if signature(before) != signature(after) or len(data) != before.st_size:
        raise OSError("File changed during read")
    return data, tuple(parents), signature(before)


def replace_regular(root: Path, relative: str, expected: bytes, content: bytes) -> None:
    target = resolve_regular(root, relative)
    fd = os.open(target, _binary(os.O_RDONLY))
    try:
        info = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            current = stream.read(len(expected) + 1)
        if not stat.S_ISREG(info.st_mode) or current != expected:
            raise OSError("File changed")
    finally:
        os.close(fd)
    temporary = target.with_name(".reviewflow-" + os.urandom(12).hex())
    out = os.open(temporary, _binary(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o644)
    try:
        with os.fdopen(out, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def open_exclusive(path: Path, *, append: bool = False):
    path = Path(path)
    if path.is_symlink():
        raise OSError("Refusing to follow a symlink")
    flags = os.O_WRONLY | (os.O_APPEND if append else os.O_CREAT | os.O_EXCL)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if append:
        return os.open(path, _binary(flags))
    return os.open(path, _binary(flags), 0o600)


def open_directory(path: Path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise OSError("Not an owned directory")
    if directory_descriptors_available():
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    return path.resolve()


def close_directory(handle) -> None:
    if isinstance(handle, int):
        os.close(handle)

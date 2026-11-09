"""Shared metadata contracts. Source contents never live in inventory records."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SourceMode = Literal["head", "directory"]
EntryType = Literal["file", "directory", "symlink", "submodule"]
FileKind = Literal["source", "test", "documentation", "config", "other"]


@dataclass(frozen=True)
class ScanPolicy:
    max_files: int = 200
    max_file_bytes: int = 262144
    max_total_bytes: int = 8388608

    def __post_init__(self):
        for name in ("max_files", "max_file_bytes", "max_total_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SourceInfo:
    mode: SourceMode
    root: Path
    commit: str | None
    branch: str | None
    dirty: bool | None
    warnings: tuple[str, ...]

    @property
    def git_repair_eligible(self) -> bool:
        return self.mode == "head" and self.commit is not None


@dataclass(frozen=True)
class SourceEntry:
    path: str
    entry_type: EntryType
    size_bytes: int | None
    object_id: str | None = None


@dataclass(frozen=True)
class FileRecord:
    path: str
    status: Literal["included", "skipped"]
    reason: str | None
    size_bytes: int | None
    sha256: str | None
    kind: FileKind


@dataclass(frozen=True)
class Coverage:
    included_files: int
    included_bytes: int
    skipped_files: int
    excluded_directories: int
    skip_counts: dict[str, int]
    budget_limited: bool
    semantic_review_performed: bool = False
    target_tests_executed: bool = False


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    source: SourceInfo
    policy: ScanPolicy
    records: tuple[FileRecord, ...]
    snapshot_id: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Inventory:
    snapshot: Snapshot
    coverage: Coverage


class SourceReader(Protocol):
    info: SourceInfo

    def entries(self) -> Iterable[SourceEntry]: ...

    def read_bytes(self, entry: SourceEntry, *, max_bytes: int) -> bytes: ...

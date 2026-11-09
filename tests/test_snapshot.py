from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from reviewflow.errors import CollectionError
from reviewflow.models import ScanPolicy, SourceEntry, SourceInfo
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


class FakeSource:
    """In-memory fixture that rejects forbidden reads before returning any data."""

    def __init__(self, files, *, forbidden=()):
        self.files = files
        self.forbidden = set(forbidden)
        self.read_paths = []
        self.info = SourceInfo("directory", Path("/fixture"), None, None, None, ())

    def entries(self):
        return [SourceEntry(path, "file", len(data)) for path, data in self.files.items()]

    def read_bytes(self, entry, *, max_bytes):
        if entry.path in self.forbidden:
            raise AssertionError(f"Must not read {entry.path}")
        self.read_paths.append(entry.path)
        assert len(self.files[entry.path]) <= max_bytes
        return self.files[entry.path]


def records(inventory):
    return {record.path: record for record in inventory.snapshot.records}


def test_sensitive_and_large_files_not_read():
    source = FakeSource(
        {".env": b"secret", "large.py": b"x" * 11, "a.py": b"ok"}, forbidden=[".env", "large.py"]
    )
    inventory = build_inventory(source, ScanPolicy(max_file_bytes=10))
    assert source.read_paths == ["a.py"]
    assert records(inventory)[".env"].reason == "sensitive_path"
    assert records(inventory)["large.py"].reason == "file_too_large"
    assert inventory.coverage.semantic_review_performed is False
    assert inventory.coverage.target_tests_executed is False
    assert inventory.coverage.included_bytes == 2
    assert records(inventory)["a.py"].sha256 == sha256(b"ok").hexdigest()


def test_sorted_deterministic_selection():
    first = FakeSource({"z.py": b"z", "a.py": b"a", "b.py": b"b"})
    second = FakeSource({"b.py": b"b", "z.py": b"z", "a.py": b"a"})
    left = build_inventory(first, ScanPolicy(max_files=2))
    right = build_inventory(second, ScanPolicy(max_files=2))
    assert [r.path for r in left.snapshot.records] == ["a.py", "b.py", "z.py"]
    assert first.read_paths == ["a.py", "b.py"]
    assert records(left)["z.py"].reason == "file_limit"
    assert left.snapshot.snapshot_id == right.snapshot.snapshot_id
    assert left.coverage.budget_limited
    assert left.snapshot.warnings


def test_count_limit_only_counts_included():
    source = FakeSource({"a.py": b"\x00", "b.py": b"\xff", "c.py": b"ok", "d.py": b"no"})
    inventory = build_inventory(source, ScanPolicy(max_files=1))
    assert records(inventory)["a.py"].reason == "binary"
    assert records(inventory)["b.py"].reason == "invalid_utf8"
    assert records(inventory)["c.py"].status == "included"
    assert records(inventory)["d.py"].reason == "file_limit"
    assert inventory.coverage.included_files == 1
    assert inventory.coverage.included_bytes == 2


def test_total_budget_allows_later_smaller_file():
    source = FakeSource({"a.py": b"1234", "b.py": b"1234567", "c.py": b"12"}, forbidden=["b.py"])
    inventory = build_inventory(source, ScanPolicy(max_total_bytes=6))
    assert inventory.coverage.included_bytes == 6
    assert inventory.coverage.included_files == 2
    assert records(inventory)["b.py"].reason == "total_bytes_limit"
    assert records(inventory)["c.py"].status == "included"


@pytest.mark.parametrize(
    "size,status,reason",
    [(3, "included", None), (4, "included", None), (5, "skipped", "file_too_large")],
)
def test_exact_limit_inclusive(size, status, reason):
    inventory = build_inventory(FakeSource({"x.py": b"a" * size}), ScanPolicy(max_file_bytes=4))
    assert records(inventory)["x.py"].status == status
    assert records(inventory)["x.py"].reason == reason


@pytest.mark.parametrize("size,status", [(3, "included"), (4, "included"), (5, "skipped")])
def test_total_exact_limit(size, status):
    inventory = build_inventory(FakeSource({"x.py": b"a" * size}), ScanPolicy(max_total_bytes=4))
    assert records(inventory)["x.py"].status == status


def test_binary_and_invalid_utf8():
    source = FakeSource({"binary.py": b"\x00", "encoding.txt": b"\xfe", "empty.py": b""})
    inventory = build_inventory(source, ScanPolicy())
    assert inventory.coverage.skip_counts == {"binary": 1, "invalid_utf8": 1}
    assert inventory.coverage.included_files == 1
    assert inventory.coverage.included_bytes == 0
    assert records(inventory)["encoding.txt"].sha256 is None


def test_excluded_directory_count_not_file_count(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/hidden.py").write_bytes(b"do not read")
    (tmp_path / "image.png").write_bytes(b"no")
    (tmp_path / "link.py").symlink_to(tmp_path / "image.png")
    inventory = build_inventory(DirectorySource(tmp_path), ScanPolicy())
    assert inventory.coverage.excluded_directories == 1
    assert inventory.coverage.skipped_files == 2
    assert "node_modules/hidden.py" not in records(inventory)
    assert records(inventory)["link.py"].reason == "symlink"


def test_tracked_file_under_excluded_directory_is_file():
    source = FakeSource({"node_modules/a.py": b"secret"}, forbidden=["node_modules/a.py"])
    source.info = replace(source.info, mode="head", commit="a" * 40)
    inventory = build_inventory(source, ScanPolicy())
    assert inventory.coverage.excluded_directories == 0
    assert inventory.coverage.skipped_files == 1


def test_empty_inventory():
    inventory = build_inventory(FakeSource({}), ScanPolicy())
    assert inventory.snapshot.records == ()
    assert inventory.coverage.included_files == 0
    assert inventory.snapshot.warnings


def test_same_contents_different_roots_same_id():
    first = FakeSource({"a.py": b"hello"})
    second = FakeSource({"a.py": b"hello"})
    second.info = replace(second.info, root=Path("/elsewhere"), dirty=True, branch="different")
    assert (
        build_inventory(first, ScanPolicy()).snapshot.snapshot_id
        == build_inventory(second, ScanPolicy()).snapshot.snapshot_id
    )


def test_changed_included_content_changes_id():
    first = build_inventory(FakeSource({"a.py": b"old"}), ScanPolicy())
    second = build_inventory(FakeSource({"a.py": b"new"}), ScanPolicy())
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id


def test_read_error_aborts_without_partial_inventory():
    class FailingSource(FakeSource):
        def read_bytes(self, entry, *, max_bytes):
            if entry.path == "b.py":
                raise CollectionError("read failed")
            return super().read_bytes(entry, max_bytes=max_bytes)

    with pytest.raises(CollectionError):
        build_inventory(FailingSource({"a.py": b"ok", "b.py": b"fail"}), ScanPolicy())


def test_fifo_not_included(tmp_path):
    import os

    os.mkfifo(tmp_path / "special.py")
    inventory = build_inventory(DirectorySource(tmp_path), ScanPolicy())
    assert records(inventory)["special.py"].reason == "unsupported_type"


def test_duplicate_entry_rejected():
    class DuplicateSource(FakeSource):
        def entries(self):
            return super().entries() * 2

    with pytest.raises(CollectionError):
        build_inventory(DuplicateSource({"a.py": b"a"}), ScanPolicy())

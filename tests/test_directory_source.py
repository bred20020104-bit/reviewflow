import os
from dataclasses import replace
from pathlib import Path

import pytest

from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.sources.directory import DirectorySource


@pytest.fixture
def sample_tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_bytes(b"x = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/a.js").write_bytes(b"ignored")
    (tmp_path / ".env").write_bytes(b"SECRET=placeholder")
    (tmp_path / "outside_link").symlink_to(tmp_path.parent, target_is_directory=True)
    return tmp_path


def test_directory_prunes_and_does_not_follow_links(sample_tree):
    source = DirectorySource(sample_tree)
    entries = {e.path: e for e in source.entries()}
    assert "node_modules/" in entries
    assert "node_modules/a.js" not in entries
    assert entries["outside_link"].entry_type == "symlink"
    assert source.info.mode == "directory"
    assert source.info.dirty is None
    assert source.info.git_repair_eligible is False
    assert source.read_bytes(entries["src/main.py"], max_bytes=7) == b"x = 1\n"
    assert (sample_tree / "src/main.py").read_bytes() == b"x = 1\n"


def test_root_symlink_rejected(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InvalidInputError):
        DirectorySource(link)


@pytest.mark.parametrize("name", ["missing", "file"])
def test_non_directory_rejected(tmp_path, name):
    (tmp_path / "file").write_text("x")
    with pytest.raises(InvalidInputError):
        DirectorySource(tmp_path / name)


def test_spaces_unicode_and_newline_names(tmp_path):
    names = ["a space.py", "中文.py"]
    if os.name == "posix":
        names.append("new\nline.py")
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    source = DirectorySource(tmp_path)
    assert {e.path for e in source.entries()} == set(names)
    assert all(source.read_bytes(e, max_bytes=2) == b"x" for e in source.entries())


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "a/../outside.py", "bad\0.py"])
def test_read_rejects_escape(tmp_path, path):
    (tmp_path / "a.py").write_bytes(b"safe")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    with pytest.raises(CollectionError):
        source.read_bytes(replace(entry, path=path), max_bytes=10)


def test_read_rejects_symlink_replacement(tmp_path):
    target = tmp_path / "a.py"
    target.write_bytes(b"safe")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    target.unlink()
    target.symlink_to("/etc/passwd")
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=100)


def test_parent_symlink_replacement(tmp_path):
    directory = tmp_path / "src"
    directory.mkdir()
    (directory / "a.py").write_bytes(b"safe")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    directory.rename(tmp_path / "moved")
    directory.symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=100)


@pytest.mark.parametrize("action", ["delete", "change"])
def test_deleted_or_changed_file_fails(tmp_path, action):
    target = tmp_path / "a.py"
    target.write_bytes(b"old")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    if action == "delete":
        target.unlink()
    else:
        target.write_bytes(b"new content")
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=100)


def test_read_is_bounded(tmp_path):
    (tmp_path / "a.py").write_bytes(b"12345")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=4)
    assert source.read_bytes(entry, max_bytes=5) == b"12345"


def test_file_changed_during_read(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_bytes(b"old")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    original = os.read

    def change_after_read(fd, count):
        data = original(fd, count)
        target.write_bytes(b"changed")
        return data

    monkeypatch.setattr(os, "read", change_after_read)
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=100)


@pytest.mark.skipif(os.name != "posix", reason="mkfifo is not available on Windows")
def test_fifo_is_not_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe.py")
    source = DirectorySource(tmp_path)
    entry = next(iter(source.entries()))
    assert entry.size_bytes is None
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=10)


def test_permission_failure_is_not_an_empty_directory(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError("fixture denial")

    source = DirectorySource(tmp_path)
    monkeypatch.setattr(os, "scandir", deny)
    with pytest.raises(CollectionError):
        list(source.entries())


def test_root_resolve_permission_denied_is_collection_error(tmp_path, monkeypatch):
    from reviewflow.sources.directory import normalize_root

    target = tmp_path / "locked"
    target.mkdir()
    original = Path.resolve

    def deny(self, *args, **kwargs):
        if os.path.normcase(os.path.abspath(self)) == os.path.normcase(os.path.abspath(target)):
            raise PermissionError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", deny)
    with pytest.raises(CollectionError, match="Cannot resolve input directory."):
        normalize_root(target)

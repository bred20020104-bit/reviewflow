import subprocess
from dataclasses import replace

import pytest
from conftest import git

from reviewflow.errors import (
    CollectionError,
    GitCommandError,
    GitUnavailableError,
    InvalidInputError,
)
from reviewflow.sources.git import GitHeadSource, GitRunner, open_source


def test_head_ignores_working_changes(dirty_repo):
    before = {
        p: (dirty_repo / p).read_bytes() for p in [".git/HEAD", ".git/index", "a.py", "src/b.py"]
    }
    source = GitHeadSource(dirty_repo)
    entries = {e.path: e for e in source.entries()}
    assert source.read_bytes(entries["a.py"], max_bytes=262145) == b"old\n"
    assert source.read_bytes(entries["src/b.py"], max_bytes=100) == b"b = 1\n"
    assert source.info.dirty is True
    assert source.info.git_repair_eligible is True
    assert "untracked.py" not in entries
    assert source.info.warnings
    assert {p: (dirty_repo / p).read_bytes() for p in before} == before


def test_clean_head(git_repo):
    assert GitHeadSource(git_repo).info.dirty is False


def test_head_stays_fixed_after_branch_moves(git_repo):
    source = GitHeadSource(git_repo)
    old_commit = source.info.commit
    (git_repo / "a.py").write_bytes(b"new commit\n")
    git(git_repo, "add", "a.py")
    git(git_repo, "commit", "-m", "later")
    entry = next(e for e in source.entries() if e.path == "a.py")
    assert source.read_bytes(entry, max_bytes=100) == b"old\n"
    assert source.info.commit == old_commit
    assert git(git_repo, "rev-parse", "HEAD").decode().strip() != old_commit


def test_subdirectory_expands_with_warning(git_repo):
    source = open_source(git_repo / "src", "auto")
    assert source.info.root == git_repo.resolve()
    assert any("root" in warning for warning in source.info.warnings)
    direct = open_source(git_repo / "src", "directory")
    assert direct.info.root == (git_repo / "src").resolve()
    assert direct.info.git_repair_eligible is False


def test_detached_head(git_repo):
    git(git_repo, "checkout", "--detach")
    assert GitHeadSource(git_repo).info.branch is None


@pytest.mark.parametrize("mode", ["head", "auto"])
def test_empty_repo_head_fails(tmp_path, mode):
    git(tmp_path, "init")
    with pytest.raises(InvalidInputError, match="HEAD"):
        open_source(tmp_path, mode)


def test_empty_repo_explicit_directory_works(tmp_path):
    git(tmp_path, "init")
    assert open_source(tmp_path, "directory").info.mode == "directory"


def test_bare_repo_rejected(tmp_path):
    git(tmp_path, "init", "--bare")
    with pytest.raises(InvalidInputError):
        open_source(tmp_path, "auto")


def test_missing_git_plain_directory_works(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert open_source(tmp_path, "auto").info.mode == "directory"


@pytest.mark.parametrize("mode", ["auto", "head"])
def test_missing_git_git_input_fails(git_repo, monkeypatch, mode):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GitUnavailableError):
        open_source(git_repo / "src", mode)


@pytest.mark.parametrize("git_available", [True, False])
def test_auto_ordinary_directory_with_git_like_names(tmp_path, monkeypatch, git_available):
    (tmp_path / "HEAD").write_text("ordinary application data\n")
    (tmp_path / "objects").mkdir()
    (tmp_path / "app.py").write_bytes(b"value = 1\n")
    if not git_available:
        monkeypatch.setenv("PATH", "")
    source = open_source(tmp_path, "auto")
    assert source.info.mode == "directory"
    entry = next(entry for entry in source.entries() if entry.path == "app.py")
    assert source.read_bytes(entry, max_bytes=100) == b"value = 1\n"


def test_git_rejection_overrides_bare_name_heuristic(tmp_path):
    (tmp_path / "HEAD").write_text("not a git HEAD\n")
    (tmp_path / "objects").mkdir()
    (tmp_path / "refs").mkdir()
    assert open_source(tmp_path, "auto").info.mode == "directory"


def test_missing_git_bare_input_does_not_fall_back(tmp_path, monkeypatch):
    git(tmp_path, "init", "--bare")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GitUnavailableError):
        open_source(tmp_path, "auto")


def test_git_timeout_not_directory_fallback(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitCommandError, match="timed out"):
        open_source(tmp_path, "auto")


def test_nul_delimited_names(git_repo):
    names = ["with space.py", "中文.py", "new\nline.py", "tab\tname.py"]
    for name in names:
        (git_repo / name).write_bytes(b"value\n")
    git(git_repo, "add", "--", *names)
    git(git_repo, "commit", "-m", "names")
    source = GitHeadSource(git_repo)
    entries = {e.path: e for e in source.entries()}
    for name in names:
        assert source.read_bytes(entries[name], max_bytes=100) == b"value\n"


def test_submodule_and_symlink_not_read(git_repo):
    (git_repo / "link.py").symlink_to("a.py")
    git(git_repo, "add", "link.py")
    commit = git(git_repo, "rev-parse", "HEAD").decode().strip()
    git(git_repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/sub")
    git(git_repo, "commit", "-m", "special entries")
    source = GitHeadSource(git_repo)
    entries = {e.path: e for e in source.entries()}
    assert entries["link.py"].entry_type == "symlink"
    assert entries["vendor/sub"].entry_type == "submodule"
    for name in ["link.py", "vendor/sub"]:
        with pytest.raises(CollectionError):
            source.read_bytes(entries[name], max_bytes=100)


def test_inherited_git_environment_does_not_redirect_repo(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_WORK_TREE", "/")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    assert GitHeadSource(git_repo).info.root == git_repo.resolve()


def test_runner_rejects_unapproved_command(git_repo):
    with pytest.raises(GitCommandError):
        GitRunner().run(git_repo, ("checkout", "main"))
    with pytest.raises(GitCommandError):
        GitRunner().run(git_repo, ("cat-file", "blob", "HEAD:a.py"))


def test_arbitrary_object_and_oversize_read_rejected(git_repo):
    source = GitHeadSource(git_repo)
    entry = next(iter(source.entries()))
    with pytest.raises(CollectionError):
        source.read_bytes(replace(entry, object_id="0" * 40), max_bytes=100)
    with pytest.raises(CollectionError):
        source.read_bytes(entry, max_bytes=1)


def test_no_configured_filters_or_fsmonitor_execute(git_repo):
    marker = git_repo / "EXECUTED"
    # Configure after committing so only the scanner could execute these commands.
    (git_repo / ".gitattributes").write_text("*.py filter=tripwire\n")
    git(git_repo, "add", ".gitattributes")
    git(git_repo, "commit", "-m", "attributes without executable config")
    command = f"touch '{marker}'; cat"
    git(git_repo, "config", "filter.tripwire.clean", command)
    git(git_repo, "config", "filter.tripwire.process", command)
    git(git_repo, "config", "filter.tripwire.required", "true")
    git(git_repo, "config", "core.fsmonitor", f"touch '{marker}'")
    (git_repo / "a.py").write_bytes(b"changed\n")
    source = GitHeadSource(git_repo)
    for entry in source.entries():
        if entry.path == "a.py":
            assert source.read_bytes(entry, max_bytes=100) == b"old\n"
    assert not marker.exists()


def test_auto_ordinary_directory(tmp_path):
    assert open_source(tmp_path, "auto").info.mode == "directory"


def test_malformed_tree_output_is_error(git_repo):
    class MalformedRunner(GitRunner):
        def run(self, root, args):
            if args[0] == "ls-tree":
                return b"broken\0"
            return super().run(root, args)

    with pytest.raises(GitCommandError):
        list(GitHeadSource(git_repo, runner=MalformedRunner()).entries())

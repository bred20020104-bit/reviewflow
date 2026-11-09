"""Fixtures operate only on pytest-owned temporary repositories."""

import os
import subprocess

import pytest


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    )
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=ReviewFlow Test",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            *args,
        ],
        env=env,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def git_repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "a.py").write_bytes(b"old\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/b.py").write_bytes(b"b = 1\n")
    git(tmp_path, "add", "a.py", "src/b.py")
    git(tmp_path, "commit", "-m", "fixture")
    return tmp_path


@pytest.fixture
def dirty_repo(git_repo):
    (git_repo / "a.py").write_bytes(b"new\n")
    (git_repo / "src/b.py").write_bytes(b"staged\n")
    git(git_repo, "add", "src/b.py")
    (git_repo / "untracked.py").write_bytes(b"untracked\n")
    return git_repo

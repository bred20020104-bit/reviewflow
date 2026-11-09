from pathlib import Path

import pytest

from reviewflow.models import ScanPolicy, SourceInfo
from reviewflow.policy import classify_kind, is_excluded_directory, path_reason


def test_policy_defaults():
    assert ScanPolicy() == ScanPolicy(200, 262144, 8388608)


@pytest.mark.parametrize("field", ["max_files", "max_file_bytes", "max_total_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_policy_rejects_invalid_limits(field, value):
    with pytest.raises(ValueError):
        ScanPolicy(**{field: value})


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV.local",
        ".env.example",
        "KEY.PEM",
        "x.key",
        "x.p12",
        "x.pfx",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    ],
)
def test_sensitive_paths(name):
    assert path_reason(f"src/{name}", "file") == "sensitive_path"


def test_link_precedes_sensitive_name():
    assert path_reason(".env", "symlink") == "symlink"
    assert path_reason(".env", "submodule") == "submodule"


@pytest.mark.parametrize(
    "name",
    [
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".reviewflow",
        ".worktrees",
        "workspaces",
        "target-repos",
        ".superpowers",
        ".claude",
        ".eggs",
        "reviewflow.egg-info",
    ],
)
def test_directory_policy(name):
    assert is_excluded_directory(name.upper())
    assert path_reason(f"{name}/", "directory") == "excluded_directory"
    assert path_reason(f"src/{name}/a.py", "file") == "excluded_directory"
    assert path_reason(f"{name}/.env", "file") == "excluded_directory"


@pytest.mark.parametrize(
    "suffix", ["py", "pyi", "md", "rst", "txt", "toml", "yaml", "yml", "json", "ini", "cfg", "sql"]
)
def test_text_suffixes_allowed(suffix):
    assert path_reason(f"src/file.{suffix.upper()}", "file") is None


@pytest.mark.parametrize("name", ["Dockerfile", "Makefile", ".gitignore"])
def test_special_text_names_allowed(name):
    assert path_reason(name, "file") is None


def test_unsupported_types():
    assert path_reason("image.png", "file") == "unsupported_type"
    assert path_reason("extensionless", "file") == "unsupported_type"
    assert not is_excluded_directory("build_tools")


@pytest.mark.parametrize(
    "path,kind",
    [
        ("src/a.py", "source"),
        ("src/a.pyi", "source"),
        ("tests/a.py", "test"),
        ("test_a.py", "test"),
        ("README.md", "documentation"),
        ("a.rst", "documentation"),
        ("a.txt", "documentation"),
        ("pyproject.toml", "config"),
        ("Dockerfile", "config"),
        ("x.sql", "config"),
        ("x.png", "other"),
    ],
)
def test_classification(path, kind):
    assert classify_kind(path) == kind


@pytest.mark.parametrize(
    "mode,commit,eligible",
    [("head", "a" * 40, True), ("head", None, False), ("directory", None, False)],
)
def test_repair_eligibility_requires_head_commit(mode, commit, eligible):
    source = SourceInfo(mode, Path("/example"), commit, None, None, ())
    assert source.git_repair_eligible is eligible

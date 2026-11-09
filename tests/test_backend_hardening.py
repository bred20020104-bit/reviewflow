import json
import subprocess
import sys

import pytest
from conftest import git
from test_backend_controls import config_data
from test_repair_workspace import make_workspace

from reviewflow.backend.config import ConfigError, parse_config
from reviewflow.backend.delivery import package_source
from reviewflow.backend.repair import RepairError
from reviewflow.quick import main


def test_worktree_rereview_excludes_only_owned_git_metadata(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, _, _ = make_workspace(git_repo, parent)
    source, inventory = ws.inventory()
    assert inventory.coverage.skipped_files == 0
    assert inventory.coverage.excluded_directories == 0
    assert {e.path for e in source.entries()} == {"a.py", "src/b.py"}


def test_packaging_rejects_symlink_in_src_ancestor(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    other = tmp_path / "outside"
    (other / "reviewflow").mkdir(parents=True)
    (other / "reviewflow/key.py").write_text("outside\n")
    (root / "src").symlink_to(other, target_is_directory=True)
    (root / "pyproject.toml").write_text('[project]\nname="reviewflow"\n')
    with pytest.raises(ValueError):
        package_source(root, tmp_path / "bad.zip")


@pytest.mark.parametrize(
    "section,key,value", [("budget", "max_retries", False), ("privacy", "allow_telemetry", 0)]
)
def test_literal_config_fields_do_not_coerce_boolean_numbers(tmp_path, section, key, value):
    data = config_data(tmp_path)
    data[section][key] = value
    with pytest.raises(ConfigError):
        parse_config(data)


def test_prepare_enforces_configured_root_and_scope(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x=1\n")
    config = config_data(tmp_path)
    # TOML fixture serialization is explicit, not a production parser mirror.
    path = tmp_path / "tool.toml"
    text = ""
    for section, values in config.items():
        text += f"[{section}]\n"
        for key, value in values.items():
            text += f"{key} = {json.dumps(value)}\n"
    path.write_text(text)
    assert (
        main(["prepare", "-d", str(tmp_path), "--source", "directory", "--config", str(path)]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["network_calls"] == 0 and result["credentials_read"] is False
    assert set(result["allowed_files"]) == {"a.py"}
    path.write_text(text.replace('allowed_paths = ["a.py"]', 'allowed_paths = ["missing.py"]'))
    assert (
        main(["prepare", "-d", str(tmp_path), "--source", "directory", "--config", str(path)]) == 2
    )


def test_backend_help_remains_optional_dependency_free_in_subprocess():
    script = """
import builtins
original = builtins.__import__
def blocked(name, *a, **kw):
    if name.split('.')[0] in {'pydantic', 'langgraph', 'langsmith'}:
        raise AssertionError('optional import')
    return original(name, *a, **kw)
builtins.__import__ = blocked
from reviewflow.quick import main
assert main(['repair-apply', '--help']) == 0
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_push_rejects_worktree_config_rewrites(git_repo, tmp_path):
    from test_validation_publication import validated_workspace

    from reviewflow.backend.publication import Publisher

    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, authority, receipt = validated_workspace(git_repo, parent)
    publisher = Publisher(ws, authority)
    intent = publisher.commit_intent(receipt, "Test", "test@example.invalid")
    publisher.commit(
        receipt, "Test", "test@example.invalid", gate, gate.approve(intent, confirmed=True)
    )
    git(git_repo, "config", "extensions.worktreeConfig", "true")
    git(
        ws.path,
        "config",
        "--worktree",
        "url.https://evil.invalid/.insteadOf",
        "https://example.invalid/",
    )
    with pytest.raises(RepairError, match="transport"):
        # A missing grant must not be the reason for this rejection: config must fail first.
        publisher.push("https://example.invalid/repo.git", gate, "fake")

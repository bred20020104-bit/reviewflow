import json
import os

import pytest
from test_backend_controls import config_data

from reviewflow.backend.config import ConfigError, parse_config
from reviewflow.backend.credentials import read_key_file, resolve_key_file
from reviewflow.backend.delivery import package_source
from reviewflow.models import ScanPolicy
from reviewflow.quick import main
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def private_file(path, value='api_key = "synthetic-local-test-key"\n'):
    path.write_text(value)
    path.chmod(0o600)
    return path


def test_file_config_exclusive_and_relative_to_config_not_cwd(tmp_path, monkeypatch):
    data = config_data(tmp_path)
    data["model"].pop("api_key_env")
    data["model"]["api_key_file"] = "secrets.local.toml"
    model = parse_config(data).model
    private_file(tmp_path / "secrets.local.toml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    secret = resolve_key_file(tmp_path / "config.local.toml", model)
    assert secret.get_secret_value() == "synthetic-local-test-key"
    assert "synthetic-local" not in repr(secret)
    data["model"]["api_key_env"] = "REVIEWFLOW_API_KEY"
    with pytest.raises(ConfigError):
        parse_config(data)
    data["model"].pop("api_key_file")
    data["model"].pop("api_key_env")
    with pytest.raises(ConfigError):
        parse_config(data)


@pytest.mark.parametrize(
    "value",
    [
        'api_key = "TODO_REPLACE_LOCALLY"',
        'api_key = ""',
        "api_key = 7",
        'api_key = "secret with spaces"',
        'api_key = "synthetic-local-test-key"\nextra = true',
        'api_key = "' + "x" * 17000 + '"',
    ],
)
def test_bad_key_file_never_echoes_values(tmp_path, value):
    path = private_file(tmp_path / "secrets.local.toml", value)
    with pytest.raises(ConfigError) as exc:
        read_key_file(path)
    assert "synthetic" not in str(exc.value) and value not in str(exc.value)


def test_key_symlink_and_public_permissions_rejected(tmp_path):
    path = private_file(tmp_path / "secrets.local.toml")
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ConfigError):
            read_key_file(path)
        path.chmod(0o600)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(path)
    with pytest.raises(ConfigError):
        read_key_file(alias)


def test_credential_check_reports_only_presence_without_network(tmp_path, capsys):
    path = private_file(tmp_path / "secrets.local.toml")
    assert main(["credentials-check", "--file", str(path)]) == 0
    out = capsys.readouterr()
    assert "synthetic" not in out.out + out.err
    result = json.loads(out.out)
    assert result["status"] == "configured" and result["network_calls"] == 0
    assert result["key_validated_with_provider"] is False


def test_key_file_is_excluded_from_inventory_and_rejected_in_package_tree(tmp_path):
    root = tmp_path / "project"
    (root / "src/reviewflow").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="reviewflow"\n')
    private_file(root / "src/reviewflow/secrets.local.toml")
    inventory = build_inventory(DirectorySource(root), ScanPolicy())
    record = next(r for r in inventory.snapshot.records if r.path.endswith("secrets.local.toml"))
    assert record.status == "skipped" and record.reason == "sensitive_path"
    with pytest.raises(ValueError):
        package_source(root, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


@pytest.mark.parametrize("value", ["../secrets.local.toml", "/tmp/secrets.local.toml", "a/b.toml"])
def test_key_reference_is_a_local_filename_not_target_path(tmp_path, value):
    data = config_data(tmp_path)
    data["model"].pop("api_key_env")
    data["model"]["api_key_file"] = value
    with pytest.raises(ConfigError):
        parse_config(data)

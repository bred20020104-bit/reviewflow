import json

import pytest

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.config import ConfigError, LocalSettings, load_config
from reviewflow.backend.credentials import read_key_file
from reviewflow.backend.transport import GuardedTransport, TransportError
from reviewflow.models import ScanPolicy
from reviewflow.quick import main
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource

PRIVATE = """provider = "example"
api_type = "official"
model = "example-model"
base_url = "https://api.example.invalid/v1"
api_key = "synthetic-private-value"
"""


def configured(tmp_path):
    config = tmp_path / "config.local.toml"
    config.write_text('secrets_file = "secrets.local.toml"\n')
    secret = tmp_path / "secrets.local.toml"
    secret.write_text(PRIVATE)
    secret.chmod(0o600)
    return config, secret


def test_all_model_settings_resolve_from_private_file_with_safe_defaults(tmp_path, monkeypatch):
    config, secret = configured(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = load_config(config)
    assert isinstance(settings, LocalSettings)
    assert settings.model.provider == "example"
    assert settings.model.model == "example-model"
    assert settings.model.base_url == "https://api.example.invalid/v1"
    assert settings.model.api_key_file == "secrets.local.toml"
    assert "synthetic-private-value" not in repr(settings)
    assert "synthetic-private-value" not in settings.model.model_dump_json()
    assert read_key_file(secret).get_secret_value() == "synthetic-private-value"
    budget = settings.budget
    assert budget.max_run_cost_usd == "1.00"
    assert budget.max_call_cost_usd == "0.25"
    assert (budget.max_calls, budget.max_retries, budget.timeout_seconds) == (12, 0, 60)
    assert (budget.max_input_tokens, budget.max_output_tokens) == (16000, 4000)
    assert budget.input_price_per_million_usd is None
    assert budget.output_price_per_million_usd is None


def test_prepare_derives_only_selected_inventory_scope_without_upload_permission(tmp_path, capsys):
    config, _ = configured(tmp_path)
    root = tmp_path / "target"
    root.mkdir()
    (root / "a.py").write_text("x=1\n")
    (root / ".env").write_text("SYNTHETIC_SECRET=yes\n")
    assert main(["prepare", "--config", str(config), "-d", str(root), "--source", "directory"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["root"] == str(root.resolve())
    assert set(result["allowed_files"]) == {"a.py"}
    assert result["credentials_read"] is True
    assert result["upload_enabled_in_config"] is False
    assert result["budget"]["max_calls"] == 12
    assert result["pricing_configured"] is False
    assert result["network_calls"] == 0
    assert "synthetic-private-value" not in captured.out + captured.err
    source = DirectorySource(root)
    bound = load_config(config).bind(source, build_inventory(source, ScanPolicy()))
    assert bound.privacy.allowed_paths == ("a.py",)
    assert not bound.privacy.allow_code_upload and not bound.privacy.allow_telemetry


def test_missing_prices_block_even_explicit_upload_and_grant(tmp_path):
    config, _ = configured(tmp_path)
    root = tmp_path / "target"
    root.mkdir()
    (root / "a.py").write_text("x=1\n")
    source = DirectorySource(root)
    bound = load_config(config).bind(source, build_inventory(source, ScanPolicy()))
    bound = bound.model_copy(
        update={"privacy": bound.privacy.model_copy(update={"allow_code_upload": True})}
    )
    gate = ApprovalGate()

    def forbidden(*args, **kwargs):
        pytest.fail("Unknown provider prices must not allow a send")

    client = GuardedTransport(bound, forbidden, gate)
    intent = client.intent("snapshot", {"a.py": "hash"}, "payload", 100)
    grant = gate.approve(intent, confirmed=True)
    with pytest.raises(TransportError, match="pricing_unconfigured"):
        client.call("snapshot", {"a.py": "hash"}, "payload", 100, grant)
    assert client.calls == 0 and client.spent == 0
    gate.consume(grant, intent)


def test_config_check_reads_private_file_but_never_prints_key(tmp_path, capsys):
    config, secret = configured(tmp_path)
    assert main(["config-check", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["credentials_read"] is True
    assert report["upload_enabled_in_config"] is False
    assert report["pricing_configured"] is False
    assert report["budget"]["max_run_cost_usd"] == "1.00"
    assert "synthetic-private-value" not in captured.out + captured.err
    # Config metadata may be checked before a Key is supplied.
    secret.write_text(PRIVATE.replace("synthetic-private-value", "TODO_REPLACE_LOCALLY"))
    assert main(["config-check", "--config", str(config)]) == 0
    assert main(["credentials-check", "--file", str(secret)]) == 2


@pytest.mark.parametrize(
    "change", ["permissions", "symlink", "unknown", "bad_endpoint", "missing_model"]
)
def test_private_model_settings_fail_closed_without_disclosing_key(tmp_path, change):
    config, secret = configured(tmp_path)
    if change == "permissions":
        secret.chmod(0o644)
    elif change == "symlink":
        other = tmp_path / "other.toml"
        secret.rename(other)
        secret.symlink_to(other)
    elif change == "unknown":
        secret.write_text(PRIVATE + 'unexpected = "synthetic-private-value"\n')
    elif change == "bad_endpoint":
        secret.write_text(
            PRIVATE.replace(
                "https://api.example.invalid/v1",
                "https://api.example.invalid/?key=synthetic-private-value",
            )
        )
    else:
        secret.write_text('api_key = "synthetic-private-value"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(config)
    assert "synthetic-private-value" not in str(exc.value)


@pytest.mark.parametrize(
    "reference", ["../secrets.local.toml", "/tmp/secrets.local.toml", "sub/key.toml"]
)
def test_simplified_reference_cannot_escape_selected_config_directory(tmp_path, reference):
    config, _ = configured(tmp_path)
    config.write_text("secrets_file = " + json.dumps(reference) + "\n")
    with pytest.raises(ConfigError):
        load_config(config)


def test_simplified_file_rejects_mixed_settings_and_empty_target(tmp_path):
    config, _ = configured(tmp_path)
    config.write_text(config.read_text() + '\n[model]\nprovider = "override"\n')
    with pytest.raises(ConfigError):
        load_config(config)
    config.write_text('secrets_file = "secrets.local.toml"\n')
    empty = tmp_path / "empty"
    empty.mkdir()
    source = DirectorySource(empty)
    with pytest.raises(ConfigError):
        load_config(config).bind(source, build_inventory(source, ScanPolicy()))

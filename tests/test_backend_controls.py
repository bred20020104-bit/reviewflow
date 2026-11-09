import json
from decimal import Decimal

import pytest

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.config import ConfigError, load_config, parse_config
from reviewflow.backend.transport import GuardedTransport, Reply, TransportError, Usage


def config_data(root):
    return {
        "model": {
            "provider": "example",
            "api_type": "official",
            "model": "model-1",
            "base_url": "https://api.example.invalid/v1",
            "api_key_env": "REVIEWFLOW_API_KEY",
        },
        "budget": {
            "max_run_cost_usd": "0.001",
            "max_call_cost_usd": "0.001",
            "input_price_per_million_usd": "1",
            "output_price_per_million_usd": "2",
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "timeout_seconds": 30,
            "max_retries": 0,
            "max_calls": 3,
        },
        "privacy": {
            "allowed_project_root": str(root),
            "allowed_paths": ["a.py"],
            "allow_code_upload": False,
            "allow_telemetry": False,
        },
    }


def test_config_validates_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFLOW_API_KEY", "SECRET")
    config = parse_config(config_data(tmp_path))
    assert config.budget.max_calls == 3
    assert "SECRET" not in config.model_dump_json()
    assert config.privacy.allowed_paths == ("a.py",)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("model", "provider", "TODO_PROVIDER"),
        ("model", "base_url", "http://api.example.invalid"),
        ("model", "base_url", "https://user:SECRET@example.invalid"),
        ("model", "base_url", "https://example.invalid/?key=SECRET"),
        ("model", "api_key_env", "bad-name"),
        ("budget", "max_calls", True),
        ("budget", "max_retries", 1),
        ("budget", "max_run_cost_usd", "NaN"),
        ("budget", "max_run_cost_usd", 0.1),
        ("privacy", "allowed_paths", ["../a.py"]),
        ("privacy", "allow_telemetry", True),
    ],
)
def test_invalid_config_sanitizes_errors(tmp_path, section, key, value):
    data = config_data(tmp_path)
    data[section][key] = value
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "SECRET" not in str(exc.value)


def test_config_rejects_unknown_fields_and_symlinks(tmp_path):
    data = config_data(tmp_path)
    data["api_key"] = "SECRET"
    with pytest.raises(ConfigError):
        parse_config(data)
    path = tmp_path / "config.toml"
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ConfigError):
        load_config(path)
    # Resolve the template next to no private credentials, never the developer's Key file.
    template = tmp_path / "config.example.toml"
    template.write_text(__import__("pathlib").Path("config.example.toml").read_text())
    with pytest.raises(ConfigError):
        load_config(template)


def test_grants_bind_operation_and_are_one_shot():
    gate = ApprovalGate()
    request = {"action": "repair", "snapshot": "a", "paths": ["a.py"]}
    with pytest.raises(Denied):
        gate.approve(request, confirmed=False)
    grant = gate.approve(request, confirmed=True)
    with pytest.raises(Denied):
        gate.consume(grant, {**request, "action": "commit"})
    gate.consume(grant, request)
    with pytest.raises(Denied):
        gate.consume(grant, request)
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_grants_cannot_cross_issuers_or_survive_expiry():
    clock = [10.0]
    gate = ApprovalGate(clock=lambda: clock[0], ttl_seconds=2)
    request = {"action": "push"}
    grant = gate.approve(request, confirmed=True)
    with pytest.raises(Denied):
        ApprovalGate().consume(grant, request)
    clock[0] = 13
    with pytest.raises(Denied):
        gate.consume(grant, request)


def guarded(tmp_path, transport):
    config = parse_config(config_data(tmp_path))
    gate = ApprovalGate()
    client = GuardedTransport(config, transport, gate)
    request = client.intent("snapshot", {"a.py": "hash"}, "payload", 50)
    return client, gate, request


def test_transport_requires_upload_and_exact_approval_before_call(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized transport called")

    client, gate, request = guarded(tmp_path, forbidden)
    grant = gate.approve(request, confirmed=True)
    with pytest.raises(TransportError, match="upload_disabled"):
        client.call("snapshot", {"a.py": "hash"}, "payload", 50, grant)


def test_transport_reserves_and_reconciles_usage(tmp_path):
    data = config_data(tmp_path)
    data["privacy"]["allow_code_upload"] = True
    config = parse_config(data)
    gate = ApprovalGate()
    client = GuardedTransport(config, lambda *a, **kw: Reply("ok", Usage(40, 10)), gate)
    request = client.intent("snapshot", {"a.py": "hash"}, "payload", 50)
    grant = gate.approve(request, confirmed=True)
    assert client.call("snapshot", {"a.py": "hash"}, "payload", 50, grant) == "ok"
    assert client.spent == Decimal("0.000060")
    assert client.calls == 1
    assert client.records[0]["status"] == "complete"
    assert "payload" not in json.dumps(client.records)
    with pytest.raises(Denied):
        client.call("snapshot", {"a.py": "hash"}, "payload", 50, grant)


@pytest.mark.parametrize("reply", [None, Reply("ok", Usage(1000, 10)), Reply("ok", None)])
def test_failed_unknown_or_excess_usage_blocks_followups(tmp_path, reply):
    def transport(*a, **kw):
        if reply is None:
            raise TimeoutError("SECRET")
        return reply

    data = config_data(tmp_path)
    data["privacy"]["allow_code_upload"] = True
    gate = ApprovalGate()
    client = GuardedTransport(parse_config(data), transport, gate)
    intent = client.intent("s", {"a.py": "h"}, "p", 50)
    with pytest.raises(TransportError) as exc:
        client.call("s", {"a.py": "h"}, "p", 50, gate.approve(intent, confirmed=True))
    assert "SECRET" not in str(exc.value)
    assert client.spent >= Decimal("0.000250")
    assert client.blocked
    assert client.calls == 1
    with pytest.raises(TransportError, match="blocked"):
        client.call("s", {"a.py": "h"}, "p", 50, gate.approve(intent, confirmed=True))


def test_budget_and_scope_reject_before_transport(tmp_path):
    data = config_data(tmp_path)
    data["privacy"]["allow_code_upload"] = True
    data["budget"]["max_run_cost_usd"] = "0.00001"
    data["budget"]["max_call_cost_usd"] = "0.00001"
    gate = ApprovalGate()
    client = GuardedTransport(parse_config(data), lambda *a, **k: pytest.fail("called"), gate)
    with pytest.raises(TransportError, match="scope"):
        client.intent("s", {"secret.py": "h"}, "p", 10)
    intent = client.intent("s", {"a.py": "h"}, "p", 10)
    with pytest.raises(TransportError, match="budget"):
        client.call("s", {"a.py": "h"}, "p", 10, gate.approve(intent, confirmed=True))
    assert client.calls == 0

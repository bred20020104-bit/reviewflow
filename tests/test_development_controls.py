import json

import pytest

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.config import ConfigError, load_config
from reviewflow.backend.development import DevelopmentTransport, load_development_config
from reviewflow.backend.transport import Reply, TransportError, Usage
from reviewflow.models import ScanPolicy
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def development_fixture(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    config = tmp_path / "config.local.toml"
    config.write_text('secrets_file = "secrets.local.toml"\n')
    secret = tmp_path / "secrets.local.toml"
    secret.write_text(
        'provider="openai"\napi_type="responses"\nmodel="fixture"\n'
        'base_url="http://127.0.0.1:9999/v1"\napi_key="synthetic-secret"\n'
    )
    secret.chmod(0o600)
    source = DirectorySource(root)
    inventory = build_inventory(source, ScanPolicy())
    return config, source, inventory


def test_development_config_does_not_relax_normal_https_or_expose_key(tmp_path):
    path, source, inventory = development_fixture(tmp_path)
    config = load_development_config(path, source, inventory)
    assert config.model.base_url == "http://127.0.0.1:9999/v1"
    assert config.privacy.allowed_paths == ("a.py",)
    assert not config.privacy.allow_code_upload
    assert not config.privacy.allow_telemetry
    assert "synthetic-secret" not in config.model_dump_json()
    with pytest.raises(ConfigError):
        load_config(path)


class FixtureAdapter:
    def __init__(self, reply=Reply("{}", Usage(12, 3))):
        self.reply = reply
        self.sent = []

    def body_size(self, payload):
        return len(payload.encode())

    def __call__(self, payload, **kwargs):
        self.sent.append((payload, kwargs))
        return self.reply


def make_transport(tmp_path, reply=Reply("{}", Usage(12, 3))):
    path, source, inventory = development_fixture(tmp_path)
    config = load_development_config(path, source, inventory)
    config = config.model_copy(
        update={"privacy": config.privacy.model_copy(update={"allow_code_upload": True})}
    )
    adapter = FixtureAdapter(reply)
    gate = ApprovalGate()
    transport = DevelopmentTransport(config, adapter, gate)
    return transport, adapter, gate


def send(transport, gate, payload="{}"):
    intent = transport.intent("snapshot", {"a.py": "hash"}, payload, len(payload.encode()))
    return transport.call(
        "snapshot",
        {"a.py": "hash"},
        payload,
        len(payload.encode()),
        gate.approve(intent, confirmed=True),
    )


def test_development_counts_real_calls_and_usage_without_invented_money(tmp_path):
    transport, adapter, gate = make_transport(tmp_path)
    for _ in range(8):
        assert send(transport, gate) == "{}"
    with pytest.raises(TransportError, match="call_budget"):
        send(transport, gate)
    assert len(adapter.sent) == 8
    assert all(
        item[1] == {"max_output_tokens": 2048, "timeout_seconds": 60} for item in adapter.sent
    )
    assert transport.calls == 8
    assert transport.records[-1]["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert transport.cost_usd is None
    assert "synthetic-secret" not in json.dumps(transport.records)


def test_scope_size_and_body_bound_checked_before_grant_consumption(tmp_path):
    transport, adapter, gate = make_transport(tmp_path)
    with pytest.raises(TransportError, match="scope"):
        transport.intent("s", {"outside.py": "hash"}, "{}", 2)
    with pytest.raises(TransportError, match="request_bytes"):
        transport.intent("s", {"a.py": "hash"}, "x" * 65537, 65537)
    with pytest.raises(TransportError, match="request_bytes"):
        transport.intent("s", {"a.py": "hash"}, "{}", 1)
    intent = transport.intent("s", {"a.py": "hash"}, "{}", 2)
    grant = gate.approve(intent, confirmed=True)
    with pytest.raises(Denied):
        transport.call("s", {"a.py": "hash"}, "[]", 2, grant)
    assert adapter.sent == []
    assert transport.call("s", {"a.py": "hash"}, "{}", 2, grant) == "{}"
    with pytest.raises(Denied):
        transport.call("s", {"a.py": "hash"}, "{}", 2, grant)


@pytest.mark.parametrize(
    "reply",
    [
        Reply("{}", None),
        Reply("{}", Usage(True, 1)),
        Reply("{}", Usage(-1, 1)),
        Reply("{}", Usage(10, 2049)),
        Reply("x" * 131073, Usage(10, 1)),
    ],
)
def test_unknown_or_invalid_usage_and_response_block_followups(tmp_path, reply):
    transport, adapter, gate = make_transport(tmp_path, reply)
    with pytest.raises(TransportError):
        send(transport, gate)
    with pytest.raises(TransportError, match="blocked"):
        send(transport, gate)
    assert transport.calls == len(adapter.sent) == 1
    assert transport.blocked


def test_transport_exception_is_redacted_and_no_retry(tmp_path):
    transport, adapter, gate = make_transport(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic-secret")

    # Use a dedicated callable boundary, not a mock of the guard itself.
    class Broken(FixtureAdapter):
        __call__ = staticmethod(explode)

    transport.adapter = Broken()
    with pytest.raises(TransportError) as error:
        send(transport, gate)
    assert "synthetic-secret" not in str(error.value)
    assert transport.blocked and transport.calls == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (TransportError("http_rate_limit"), "http_rate_limit"),
        (
            TransportError("response_incomplete_max_output_tokens"),
            "response_incomplete_max_output_tokens",
        ),
        (TransportError("synthetic-secret"), "backend_error"),
        (TransportError("http_rate_limit", "synthetic-secret"), "backend_error"),
        (RuntimeError("synthetic-secret"), "backend_error"),
    ],
)
def test_accounting_preserves_only_safe_failure_codes_and_blocks_followups(tmp_path, error, code):
    transport, adapter, gate = make_transport(tmp_path)

    class Broken(FixtureAdapter):
        def __call__(self, payload, **kwargs):
            self.sent.append((payload, kwargs))
            raise error

    transport.adapter = adapter = Broken()
    with pytest.raises(TransportError) as caught:
        send(transport, gate)
    assert caught.value.args == (code,)
    assert transport.records[-1]["error_code"] == code
    assert transport.records[-1]["usage"] is None
    assert transport.records[-1]["status"] == "transport_or_usage_failure"
    assert "synthetic-secret" not in json.dumps(transport.records)
    with pytest.raises(TransportError, match="run_blocked"):
        send(transport, gate)
    assert transport.calls == len(adapter.sent) == 1
    from reviewflow.backend.diagnostics import SafeTransportError

    assert isinstance(caught.value, SafeTransportError)


def test_successful_accounting_has_no_error_code(tmp_path):
    transport, adapter, gate = make_transport(tmp_path)
    send(transport, gate)
    assert transport.records[-1]["error_code"] is None


def test_upload_stays_disabled_without_controller_approval(tmp_path):
    path, source, inventory = development_fixture(tmp_path)
    adapter, gate = FixtureAdapter(), ApprovalGate()
    transport = DevelopmentTransport(
        load_development_config(path, source, inventory), adapter, gate
    )
    with pytest.raises(TransportError, match="upload_disabled"):
        send(transport, gate)
    assert not adapter.sent

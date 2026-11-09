import json
from decimal import Decimal

import pytest
from test_backend_controls import config_data

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.config import parse_config
from reviewflow.backend.gateway import WorkflowGateway
from reviewflow.backend.transport import GuardedTransport, Reply, TransportError, Usage
from reviewflow.models import ScanPolicy
from reviewflow.review.workflow_models import ModelRequest
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def gateway_fixture(tmp_path, *, authorize=None):
    (tmp_path / "a.py").write_text("a = 1\n")
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    data = config_data(tmp_path)
    data["privacy"]["allow_code_upload"] = True
    data["budget"].update(
        max_input_tokens=10000, max_output_tokens=100, max_run_cost_usd="1", max_call_cost_usd="1"
    )
    gate = ApprovalGate()
    seen = []

    def adapter(wire, **kwargs):
        seen.append(json.loads(wire))
        return Reply('{"ok":true}', Usage(10, 1))

    transport = GuardedTransport(parse_config(data), adapter, gate)
    gateway = WorkflowGateway(
        source,
        inventory,
        transport,
        lambda wire: len(wire.encode()),
        authorize or (lambda intent: gate.approve(intent, confirmed=True)),
    )
    ref = {
        "snapshot_id": inventory.snapshot.snapshot_id,
        "path": "a.py",
        "content_sha256": inventory.snapshot.records[0].sha256,
        "start_line": 1,
        "end_line": 1,
    }
    request = ModelRequest(
        "reviewer",
        "trusted system",
        json.dumps({"files": [{"evidence": ref, "text": "a = 1\n"}]}),
        {"type": "object"},
    )
    return gateway, transport, request, seen


def test_gateway_binds_full_request_and_reserves_before_adapter(tmp_path):
    gateway, transport, request, seen = gateway_fixture(tmp_path)
    assert gateway.complete(request) == '{"ok":true}'
    assert seen[0]["system"] == "trusted system"
    assert seen[0]["response_schema"] == {"type": "object"}
    assert seen[0]["payload"]["files"][0]["text"] == "a = 1\n"
    assert transport.calls == 1 and transport.spent == Decimal("0.000012")
    assert len(transport.records[0]["intent"]) == 64


def test_gateway_denial_happens_before_adapter(tmp_path):
    gateway, transport, request, seen = gateway_fixture(tmp_path, authorize=lambda intent: "fake")
    with pytest.raises(Denied):
        gateway.complete(request)
    assert not seen and transport.calls == 0


@pytest.mark.parametrize("tamper", ["text", "path", "hash", "source"])
def test_gateway_rejects_changed_or_unbound_context(tmp_path, tamper):
    gateway, transport, request, seen = gateway_fixture(tmp_path)
    data = json.loads(request.payload)
    packet = data["files"][0]
    if tamper == "text":
        packet["text"] = "outside source text"
    elif tamper == "path":
        packet["evidence"]["path"] = "../private.py"
    elif tamper == "hash":
        packet["evidence"]["content_sha256"] = "0" * 64
    else:
        (tmp_path / "a.py").write_text("a = 2\n")
    request = ModelRequest(request.stage, request.system, json.dumps(data), request.response_schema)
    with pytest.raises(TransportError):
        gateway.complete(request)
    assert not seen and transport.calls == 0


def test_gateway_keeps_body_limit_diagnostic_before_approval_or_send(tmp_path):
    from dataclasses import replace

    from test_responses_adapter import model

    from reviewflow.backend.responses_adapter import ResponsesAdapter

    gateway, transport, request, seen = gateway_fixture(tmp_path)
    adapter = ResponsesAdapter(model("http://127.0.0.1:1/v1"), lambda: pytest.fail("no key read"))
    gateway.input_upper_bound = adapter.body_size
    request = replace(request, response_schema={"type": "object", "description": "x" * 65536})
    with pytest.raises(TransportError, match="^responses_request_rejected$"):
        gateway.complete(request)
    assert transport.calls == 0 and seen == []


def test_gateway_enforces_cumulative_call_budget(tmp_path):
    gateway, transport, request, seen = gateway_fixture(tmp_path)
    for _ in range(3):
        gateway.complete(request)
    with pytest.raises(TransportError):
        gateway.complete(request)
    assert len(seen) == 3


def test_gateway_rejects_root_mismatch_before_approval(tmp_path):
    gateway, transport, request, seen = gateway_fixture(tmp_path)
    transport.config = transport.config.model_copy(
        update={
            "privacy": transport.config.privacy.model_copy(
                update={"allowed_project_root": str(tmp_path.parent)}
            )
        }
    )
    with pytest.raises(TransportError):
        gateway.complete(request)
    assert not seen

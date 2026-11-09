import json

import pytest
from pydantic import SecretStr
from test_responses_adapter import WIRE, api_server, model, response

from reviewflow.backend.responses_adapter import ResponsesAdapter


@pytest.mark.parametrize("stage", ["planner", "reviewer", "verifier", "patch"])
def test_real_worker_explicitly_disables_native_tools_for_every_stage(stage):
    wire = json.loads(WIRE)
    wire["stage"] = stage
    with api_server(response()) as (url, calls):
        reply = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-key"))(
            json.dumps(wire), max_output_tokens=2048, timeout_seconds=5)
        assert len(calls) == 1
        sent = json.loads(calls[0][2])
        assert sent.get("tools") == []
        assert sent.get("tool_choice") == "none"
        assert "read/search requests are JSON data" in sent["instructions"]
        assert sent["text"]["format"]["type"] == "json_schema"
        assert sent["text"]["format"]["strict"] is True
        assert sent["max_output_tokens"] == 2048
        assert sent["stream"] is False and sent["store"] is False
        assert reply.text == '{"result":"ok"}'

"""Reject unsupported envelopes with fixed type classes, never raw type values."""
import pytest
from pydantic import SecretStr
from test_responses_adapter import WIRE, api_server, model, response

from reviewflow.backend.responses_adapter import ResponsesAdapter
from reviewflow.backend.transport import TransportError


@pytest.mark.parametrize("kind", [
    "custom_tool_call", "custom_tool_call_output", "function_call_output",
    "web_search_call", "file_search_call", "computer_call", "code_interpreter_call",
    "mcp_call", "mcp_list_tools", "mcp_approval_request", "local_shell_call",
    "shell_call", "shell_call_output", "apply_patch_call", "apply_patch_call_output",
    "compaction", "tool_call", "tool_use", "text", "output_text", "refusal",
])
def test_known_unsupported_type_is_classified_but_never_accepted(kind):
    data = response(output=[{"type": kind, "arguments": "synthetic-private-key",
                             "content": [{"type": "output_text", "text": '{"ok":true}'}]}])
    with api_server(data) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert error.value.args == (f"response_type_{kind}_unsupported",)
        assert len(calls) == 1
        assert error.value.progress["events"][-1]["stage"] == "response_body"
        assert "synthetic-private-key" not in str(error.value)


@pytest.mark.parametrize("kind", ["synthetic-private-key", ["synthetic-private-key"],
                                 {"type": "synthetic-private-key"}, 3, True])
def test_unknown_types_remain_one_fixed_code(kind):
    with api_server(response(output=[{"type": kind}])) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert error.value.args == ("response_output_type_unsupported",)
        assert len(calls) == 1

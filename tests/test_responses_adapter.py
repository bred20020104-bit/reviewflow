import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from reviewflow.backend.development import DevelopmentModel
from reviewflow.backend.responses_adapter import (
    MAX_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    ResponsesAdapter,
    build_body,
)
from reviewflow.backend.transport import TransportError

WIRE = json.dumps(
    {
        "stage": "planner",
        "system": "Read only.",
        "payload": {"text": "中文"},
        "response_schema": {
            "type": "object",
            "properties": {"result": {"type": "string", "default": ""}},
            "required": [],
        },
    }
)


def response(text='{"result":"ok"}', **changes):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 17, "output_tokens": 4, "total_tokens": 21},
        **changes,
    }


@contextmanager
def api_server(result=None, status=200, raw=None, delay=0):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, dict(self.headers), body))
            if delay:
                threading.Event().wait(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", "/not-authorized")
                self.end_headers()
                value = result(body) if callable(result) else result or response()
                self.wfile.write(raw if raw is not None else json.dumps(value).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def model(base_url):
    return DevelopmentModel(
        provider="openai",
        api_type="responses",
        model="fixture",
        base_url=base_url,
        api_key_file="secrets.local.toml",
    )


@pytest.mark.parametrize("content", [None, [], [{"type": "reasoning_text", "text": "synthetic-private-key"}]])
def test_reasoning_content_is_discarded_not_concatenated_into_structured_answer(content):
    data = response()
    data["output"].insert(0, {"type": "reasoning", "id": "synthetic", "summary": [], "content": content})
    with api_server(data) as (url, calls):
        reply = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
            WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert reply.text == '{"result":"ok"}'
    assert reply.usage.input_tokens == 17 and len(calls) == 1


@pytest.mark.parametrize("content", ["synthetic-private-key", [{"type": "output_text", "text": '{}'}],
                                     [{"type": "reasoning_text", "text": None}]])
def test_malformed_reasoning_is_not_silently_accepted(content):
    data = response()
    data["output"].insert(0, {"type": "reasoning", "content": content})
    with api_server(data) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert error.value.args == ("response_reasoning_invalid",)
    assert len(calls) == 1


def test_reasoning_without_a_structured_answer_still_fails():
    data = response(output=[{"type": "reasoning", "content": [{"type": "reasoning_text", "text": '{}'}]}])
    with api_server(data) as (url, calls):
        with pytest.raises(TransportError):
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert len(calls) == 1


def test_mapping_counts_exact_body_and_preserves_local_schema():
    original = json.loads(WIRE)
    body = build_body(model("http://127.0.0.1/v1"), WIRE, 2048)
    data = json.loads(body)
    assert data["model"] == "fixture"
    assert data["store"] is False and data["stream"] is False
    assert data["max_output_tokens"] == 2048
    assert json.loads(data["input"]) == {"text": "中文"}
    assert data["text"]["format"]["schema"]["required"] == ["result"]
    assert data["text"]["format"]["schema"]["additionalProperties"] is False
    assert "default" not in data["text"]["format"]["schema"]["properties"]["result"]
    assert json.loads(WIRE) == original
    assert data["tools"] == [] and data["tool_choice"] == "none"
    assert "previous_response_id" not in data


def test_real_worker_sends_one_bounded_request_without_env_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with api_server() as (url, calls):
        adapter = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))
        reply = adapter(WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert reply.text == '{"result":"ok"}'
        assert (reply.usage.input_tokens, reply.usage.output_tokens) == (17, 4)
        assert len(calls) == 1 and calls[0][0] == "/v1/responses"
        assert len(calls[0][2]) == adapter.body_size(WIRE)
        assert b"synthetic-private-key" not in calls[0][2]
        assert calls[0][1]["Authorization"] == "Bearer synthetic-private-key"


@pytest.mark.parametrize(
    "data",
    [
        response(status="incomplete"),
        response(usage=None),
        response(usage={"input_tokens": True, "output_tokens": 1}),
        response(usage={"input_tokens": 1, "output_tokens": 2049}),
        response(output=[{"content": [{"type": "refusal", "refusal": "synthetic-private-key"}]}]),
        response(text="synthetic-private-key"),
        response(text='{"result":[{"nested":"\\u0073ynthetic-private-key"}]}'),
        response(text='{"\\u0073ynthetic-private-key":"value"}'),
        response(text='{"result":"\\u0073ynthetic-private-\\u006bey"}'),
        response(output=[]),
        {"error": "synthetic-private-key"},
    ],
)
def test_invalid_incomplete_refusal_or_secret_output_fails_without_raw_error(data):
    with api_server(data) as (url, calls):
        adapter = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))
        with pytest.raises(TransportError) as error:
            adapter(WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert "synthetic-private-key" not in str(error.value)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "status,raw",
    [
        (302, b"synthetic-private-key"),
        (429, b"synthetic-private-key"),
        (500, b"synthetic-private-key"),
        (200, b"x" * 262145),
        (200, b"not-json"),
    ],
)
def test_redirect_errors_and_oversized_body_never_retry(status, raw):
    with api_server(status=status, raw=raw) as (url, calls):
        with pytest.raises(TransportError):
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5
            )
        assert len(calls) == 1


def test_parent_deadline_terminates_worker_without_background_request_loop():
    with api_server(delay=2) as (url, calls):
        adapter = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))
        start = time.monotonic()
        with pytest.raises(TransportError):
            adapter(WIRE, max_output_tokens=2048, timeout_seconds=0.3)
        assert time.monotonic() - start < 1.5
        assert len(calls) <= 1


@pytest.mark.parametrize(
    "status,raw,code",
    [
        (302, b"synthetic-private-key", "http_redirect"),
        (401, b"synthetic-private-key", "http_auth_error"),
        (403, b"synthetic-private-key", "http_auth_error"),
        (429, b"synthetic-private-key", "http_rate_limit"),
        (400, b"synthetic-private-key", "http_status_400"),
        (404, b"synthetic-private-key", "http_status_404"),
        (405, b"synthetic-private-key", "http_status_405"),
        (408, b"synthetic-private-key", "http_status_408"),
        (409, b"synthetic-private-key", "http_status_409"),
        (413, b"synthetic-private-key", "http_status_413"),
        (415, b"synthetic-private-key", "http_status_415"),
        (422, b"synthetic-private-key", "http_status_422"),
        (451, b"synthetic-private-key", "http_status_451"),
        (499, b"synthetic-private-key", "http_status_499"),
        (503, b"synthetic-private-key", "http_server_error"),
        (201, b"synthetic-private-key", "http_error"),
        (200, b"not-json synthetic-private-key", "response_json_invalid"),
        (200, b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
    ],
)
def test_real_worker_preserves_safe_http_and_body_diagnostics(status, raw, code):
    with api_server(status=status, raw=raw) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5
            )
        assert error.value.args == (code,)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "data,code",
    [
        (
            response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            "response_incomplete_max_output_tokens",
        ),
        (
            response(status="incomplete", incomplete_details={"reason": "content_filter"}),
            "response_incomplete_content_filter",
        ),
        (
            response(status="incomplete", incomplete_details={"reason": "synthetic-private-key"}),
            "response_incomplete",
        ),
        (response(status="synthetic-private-key"), "response_status_invalid"),
        (response(usage=None), "usage_missing"),
        (response(usage={"input_tokens": True, "output_tokens": 1}), "usage_invalid"),
        (response(usage={"input_tokens": 1}), "usage_invalid"),
        (response(usage={"input_tokens": 1, "output_tokens": 2049}), "usage_output_limit"),
        (
            response(
                output=[{"content": [{"type": "refusal", "refusal": "synthetic-private-key"}]}]
            ),
            "response_refusal",
        ),
        (
            response(output=[{"type": "function_call", "arguments": "synthetic-private-key"}]),
            "response_function_call_unsupported",
        ),
        (response(output="synthetic-private-key"), "response_output_not_list"),
        (response(output=["synthetic-private-key"]), "response_output_item_invalid"),
        (response(output=[{"type": "synthetic-private-key"}]), "response_output_type_unsupported"),
        (response(output=[{"type": ["synthetic-private-key"]}]), "response_output_type_unsupported"),
        (response(output=[{"content": "synthetic-private-key"}]), "response_message_content_invalid"),
        (response(output=[{"content": ["synthetic-private-key"]}]), "response_content_item_invalid"),
        (response(output=[{"content": [{"type": "synthetic-private-key"}]}]),
         "response_content_type_unsupported"),
        (response(text="not-json"), "response_text_invalid_or_unsafe"),
        (
            response(text='{"result":"\\u0073ynthetic-private-key"}'),
            "response_text_invalid_or_unsafe",
        ),
        (response(output=[]), "response_text_invalid_or_unsafe"),
    ],
)
def test_real_worker_preserves_safe_response_diagnostics(data, code):
    with api_server(data) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=5
            )
        assert error.value.args == (code,)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "returncode,stdout,code",
    [
        (1, b'{"ok":false,"error_code":"http_rate_limit"}', "http_rate_limit"),
        (0, b'{"ok":false,"error_code":"http_rate_limit"}', "worker_protocol_error"),
        (1, b'{"ok":false,"error_code":"run_blocked"}', "worker_protocol_error"),
        (1, b'{"ok":false,"error_code":"synthetic-private-key"}', "worker_protocol_error"),
        (1, b'{"ok":false,"error_code":["http_rate_limit"]}', "worker_protocol_error"),
        (
            1,
            b'{"ok":false,"error_code":"http_rate_limit","message":"synthetic-private-key"}',
            "worker_protocol_error",
        ),
        (1, b"synthetic-private-key", "worker_protocol_error"),
        (
            1,
            b'{"ok":false,"error_code":"synthetic-private-key","error_code":"http_rate_limit"}',
            "worker_protocol_error",
        ),
        (0, b"null", "worker_protocol_error"),
        (0, b"[]", "worker_protocol_error"),
        (1, b"", "worker_failed"),
        (-9, b'{"ok":false,"error_code":"http_rate_limit"}', "worker_failed"),
        (0, b'{"ok":true,"text":"{}","usage":null}', "worker_protocol_error"),
        (0, b"x" * 524289, "worker_protocol_error"),
    ],
)
def test_untrusted_worker_output_never_becomes_diagnostics(monkeypatch, returncode, stdout, code):
    def worker(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode, stdout, b"synthetic-private-key")

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(TransportError) as error:
        ResponsesAdapter(
            model("http://127.0.0.1:1/v1"), lambda: SecretStr("synthetic-private-key")
        )(WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert error.value.args == (code,)


def test_worker_deadline_is_distinct_and_discards_timeout_streams(monkeypatch):
    def worker(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=b"synthetic-private-key",
            stderr=b"synthetic-private-key",
        )

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(TransportError) as error:
        ResponsesAdapter(
            model("http://127.0.0.1:1/v1"), lambda: SecretStr("synthetic-private-key")
        )(WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert error.value.args == ("worker_timeout",)


def test_real_worker_socket_timeout_emits_safe_envelope_without_retry():
    with api_server(delay=0.2) as (url, calls):
        request = {
            "base_url": url,
            "api_key": "synthetic-private-key",
            "body": build_body(model(url), WIRE, 2048).decode(),
            "max_output_tokens": 2048,
            "timeout": 0.05,
        }
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "reviewflow.backend.responses_worker"],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 1
        assert json.loads(completed.stdout) == {"ok": False, "error_code": "response_timeout"}
        assert completed.stderr == b""
        assert len(calls) == 1


def test_real_worker_rejects_invalid_stdin_without_echo():
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "reviewflow.backend.responses_worker"],
        input=b"synthetic-private-key",
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {"ok": False, "error_code": "worker_protocol_error"}
    assert completed.stderr == b""


def test_real_worker_network_failure_has_safe_code():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # Close the reserved loopback port so connection refusal, not an idle socket, is tested.
    with pytest.raises(TransportError) as error:
        ResponsesAdapter(
            model(f"http://127.0.0.1:{port}/v1"), lambda: SecretStr("synthetic-private-key")
        )(WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert error.value.args == ("network_error",)


def test_real_worker_tls_failure_has_safe_code():
    with api_server() as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(
                model(url.replace("http:", "https:")), lambda: SecretStr("synthetic-private-key")
            )(WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert error.value.args == ("tls_error",)
        assert not calls


def test_oversized_request_rejected_before_key_read_or_network():
    wire = json.loads(WIRE)
    wire["payload"]["text"] = "x" * (MAX_BODY_BYTES + 1)

    def forbidden():
        pytest.fail("must not read Key for oversized request")

    adapter = ResponsesAdapter(model("http://127.0.0.1:1/v1"), forbidden)
    with pytest.raises(TransportError):
        adapter(json.dumps(wire), max_output_tokens=2048, timeout_seconds=5)

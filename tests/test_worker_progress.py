"""Fixed transport milestones, never provider text or a substitute for usage."""
import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr
from test_responses_adapter import WIRE, model, response

from reviewflow.backend.responses_adapter import ResponsesAdapter
from reviewflow.backend.transport import TransportError

STAGES = ["worker_started", "connection_established", "request_sent", "response_headers",
          "response_body", "response_validated"]


@contextmanager
def stalled_server(stage):
    calls = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            raw = json.dumps(response()).encode()
            if stage == "headers":
                release.wait(5)
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.flush()
                if stage == "body":
                    release.wait(5)
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def assert_progress(progress, stages):
    assert progress is not None
    assert set(progress) == {"version", "events"}
    assert progress["version"] == 1
    assert [event["stage"] for event in progress["events"]] == stages
    times = [event["elapsed_ms"] for event in progress["events"]]
    assert times == sorted(times)
    assert all(type(value) is int and 0 <= value <= 60000 for value in times)
    assert all(set(event) == {"stage", "elapsed_ms"} for event in progress["events"])


@pytest.mark.parametrize("stage,count", [("headers", 3), ("body", 4)])
def test_parent_timeout_retains_only_completed_milestones(stage, count):
    with stalled_server(stage) as (url, calls):
        with pytest.raises(TransportError) as error:
            ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
                WIRE, max_output_tokens=2048, timeout_seconds=1)
        assert error.value.args == ("worker_timeout",)
        assert_progress(getattr(error.value, "progress", None), STAGES[:count])
        assert len(calls) == 1


def test_successful_worker_reports_all_milestones():
    with stalled_server(None) as (url, calls):
        reply = ResponsesAdapter(model(url), lambda: SecretStr("synthetic-private-key"))(
            WIRE, max_output_tokens=2048, timeout_seconds=5)
        assert_progress(getattr(reply, "progress", None), STAGES)
        assert reply.text == '{"result":"ok"}'
        assert len(calls) == 1


def test_connection_failure_reports_only_worker_start():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(TransportError) as error:
        ResponsesAdapter(model(f"http://127.0.0.1:{port}/v1"), lambda: SecretStr("synthetic-key"))(
            WIRE, max_output_tokens=2048, timeout_seconds=5)
    assert error.value.args == ("network_error",)
    assert_progress(getattr(error.value, "progress", None), STAGES[:1])


def test_timeout_streams_cannot_forge_milestones(monkeypatch):
    def worker(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"],
            output=b"synthetic-private-key", stderr=b'worker_started 1 synthetic-private-key')
    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(TransportError) as error:
        ResponsesAdapter(model("http://127.0.0.1:1/v1"), lambda: SecretStr("synthetic-private-key"))(
            WIRE, max_output_tokens=2048, timeout_seconds=1)
    assert error.value.args == ("worker_timeout",)
    assert getattr(error.value, "progress", None) is None


@pytest.mark.parametrize("raw", [b"", b"synthetic-private-key", b"x" * 31,
    b"\x00\x00", b"\x06\x00\x00\x00\x00",
    b"\x00\x00\x01\x00\x00", b"\x01\x00\x00\x00\x00",
    b"\x00\x00\x00\x00\x02\x01\x00\x00\x00\x01"])
def test_invalid_progress_bytes_are_discarded(raw):
    from reviewflow.backend.worker_progress import decode_progress
    assert decode_progress(raw) is None


@pytest.mark.parametrize("value", [None, {"version": True, "events": []},
    {"version": 1, "events": [{"stage": "synthetic-private-key", "elapsed_ms": 0}]},
    {"version": 1, "events": [{"stage": "worker_started", "elapsed_ms": True}]},
    {"version": 1, "events": [{"stage": "worker_started", "elapsed_ms": 0, "raw": "secret"}]},
    {"version": 1, "events": [{"stage": "worker_started", "elapsed_ms": 0}], "raw": "secret"}])
def test_untrusted_structured_progress_is_discarded(value):
    from reviewflow.backend.worker_progress import safe_progress
    assert safe_progress(value) is None


def test_development_accounting_keeps_progress_but_unknown_usage_still_blocks(tmp_path):
    from test_development_controls import make_transport

    from reviewflow.backend.approval import fingerprint
    from reviewflow.backend.diagnostics import SafeTransportError

    transport, adapter, gate = make_transport(tmp_path)
    progress = {"version": 1, "events": [{"stage": "worker_started", "elapsed_ms": 0}]}

    class Failure:
        body_size = adapter.body_size

        def __call__(self, *args, **kwargs):
            error = SafeTransportError("worker_timeout")
            error.progress = progress
            raise error

    transport.adapter = Failure()
    payload = "{}"
    intent = transport.intent("snapshot", {"a.py": "hash"}, payload, 2)
    grant = gate.approve(intent, confirmed=True)
    with pytest.raises(TransportError):
        transport.call("snapshot", {"a.py": "hash"}, payload, 2, grant)
    assert transport.blocked and transport.calls == 1
    record = transport.records[0]
    assert record["usage"] is None
    assert record["status"] == "transport_or_usage_failure"
    assert record["error_code"] == "worker_timeout"
    assert record["intent"] == fingerprint(intent)
    assert record.get("transport_progress") == progress
    with pytest.raises(TransportError, match="run_blocked"):
        transport.call("snapshot", {"a.py": "hash"}, payload, 2, grant)

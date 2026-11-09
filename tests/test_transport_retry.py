"""Retry control/accounting on owned fixtures, never real-target acceptance."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError
from test_live_repair import RepairFixtureAdapter
from test_live_repair_cli import cli_fixture
from test_repair_budget import Adapter, request, setup_budget
from test_repair_candidate import create
from test_responses_adapter import model, response
from test_validation_publication import process

from reviewflow.backend import development
from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.development import DevelopmentLimits
from reviewflow.backend.diagnostics import SafeTransportError
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.transport import Reply, TransportError, Usage
from reviewflow.quick import main


@pytest.fixture
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr(development, "sleep", delays.append, raising=False)
    return delays


def retry_setup(repo, tmp_path, *, retries=2, cap=48, unbounded=False):
    plan, config, profiles, runner, _, _ = setup_budget(repo, tmp_path, cap=cap)
    config = config.model_copy(update={"limits": config.limits.model_copy(update={"max_retries": retries})})
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", cap,
                                  no_call_limit=unbounded)
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    return plan, config, profiles, runner, preview, authority


class SequenceAdapter(Adapter):
    def __init__(self, config, values):
        super().__init__(config.model)
        self.values = iter(values)

    def __call__(self, payload, **kwargs):
        assert kwargs == {"max_output_tokens": 2048, "timeout_seconds": 60}
        self.sent.append(payload)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.parametrize("code", ["http_status_408", "response_timeout", "worker_timeout"])
def test_retry_success_counts_all_attempts_and_preserves_unknown_usage(git_repo, tmp_path, waits, code):
    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path)
    candidate = create(plan)
    adapter = SequenceAdapter(config, [SafeTransportError(code), SafeTransportError(code), Reply("{}", Usage(12, 3))])
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    assert phase.gateway.complete(request(phase)) == "{}"
    authority.record_phase(phase)
    data = authority.accounting()
    assert waits == [1, 2]
    assert len(adapter.sent) == phase.issued == data["requests_reserved"] == data["requests_attempted"] == 3
    assert adapter.sent[0] == adapter.sent[1] == adapter.sent[2]
    assert data["input_tokens_reported"] == 12 and data["output_tokens_reported"] == 3
    assert data["usage_complete"] is False and data["cost_usd"] is None
    for record in data["records"][:2]:
        assert record["status"] == "transport_or_usage_failure" and record["usage"] is None
        assert record["error_code"] == code and record["retry_status"] == "recovered"
        assert record["recovered_by_call"] == 3
    assert data["records"][-1]["status"] == "complete"
    assert data["records"][-1]["retry_attempt"] == 2
    grant = authority.authorize_tests(candidate, runner)
    assert runner.run(candidate.files(), "repository", authority.gate, grant).status == "passed"


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_repeated_timeout_stops_exactly_at_authorized_attempt_count(git_repo, tmp_path, waits, retries):
    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path, retries=retries)
    candidate = create(plan)
    adapter = SequenceAdapter(config, [SafeTransportError("http_status_408")] * 4)
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    with pytest.raises(TransportError, match="http_status_408"):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    assert len(adapter.sent) == authority.accounting()["requests_attempted"] == retries + 1
    assert waits == [1, 2][:retries]
    assert all(r["status"] != "complete" for r in authority.accounting()["records"])
    with pytest.raises(Denied):
        authority.authorize_tests(candidate, runner)
    with pytest.raises(Denied):
        authority.start_phase(candidate, plan.selected, adapter, profiles)


@pytest.mark.parametrize("failure", [
    SafeTransportError("http_auth_error"), SafeTransportError("http_status_400"),
    SafeTransportError("http_status_413"), SafeTransportError("http_status_422"),
    SafeTransportError("http_rate_limit"), SafeTransportError("http_server_error"),
    SafeTransportError("network_error"), SafeTransportError("tls_error"),
    SafeTransportError("response_json_invalid"), SafeTransportError("worker_protocol_error"),
    RuntimeError("PRIVATE_ERROR"), Reply("{}", None), Reply("{}", Usage(12, 2049)),
])
def test_other_errors_never_retry_or_become_recovered(git_repo, tmp_path, waits, failure):
    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path)
    candidate = create(plan)
    adapter = SequenceAdapter(config, [failure, Reply("{}", Usage(12, 3))])
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    with pytest.raises(TransportError):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    assert waits == [] and len(adapter.sent) == 1
    assert "PRIVATE_ERROR" not in json.dumps(authority.accounting())
    assert authority.accounting()["records"][0].get("retry_status") != "recovered"
    with pytest.raises(Denied):
        authority.authorize_tests(candidate, runner)


@pytest.mark.parametrize("boundary", ["total", "reserve", "phase", "expiry", "candidate", "origin"])
def test_retry_rechecks_caps_expiry_and_source_before_second_send(git_repo, tmp_path, waits, monkeypatch, boundary):
    cap = 1 if boundary == "total" else 9 if boundary == "reserve" else 48
    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path, cap=cap)
    candidate = create(plan)
    if boundary == "reserve":
        authority.reserve_final_review(candidate)
    adapter = SequenceAdapter(config, [Reply("{}", Usage(1, 1))] * (7 if boundary == "phase" else 0)
                              + [SafeTransportError("http_status_408"), Reply("{}", Usage(1, 1))])
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    if boundary == "phase":
        for _ in range(7):
            phase.gateway.complete(request(phase))
    if boundary in {"expiry", "candidate", "origin"}:
        def change_after_backoff(seconds):
            waits.append(seconds)
            if boundary == "expiry":
                authority._deadline = 0
            else:
                root = candidate.path if boundary == "candidate" else git_repo
                (root / "a.py").write_text("external edit\n")
        monkeypatch.setattr(development, "sleep", change_after_backoff)
    with pytest.raises((TransportError, ValueError)):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    assert len(adapter.sent) == authority.accounting()["requests_attempted"] == (8 if boundary == "phase" else 1)
    assert authority.accounting()["records"][-1]["usage"] is None
    assert not authority.accounting()["usage_complete"]
    with pytest.raises(Denied):
        authority.authorize_tests(candidate, runner)
    if boundary == "reserve":
        assert authority.accounting()["final_review_calls_held"] == 8
        with pytest.raises(Denied):
            authority.begin_final_review(candidate)


def test_recovery_permission_is_owned_and_not_mutable_report_metadata(git_repo, tmp_path, waits):
    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path)
    candidate = create(plan)
    adapter = SequenceAdapter(config, [SafeTransportError("http_status_408"), Reply("{}", Usage(12, 3))])
    phase = authority.start_phase(candidate, plan.selected, adapter, profiles)
    assert phase.gateway.complete(request(phase)) == "{}"
    phase.transport.records[0]["recovered_by_call"] = 99
    authority.record_phase(phase)
    with pytest.raises(Denied):
        authority.authorize_tests(candidate, runner)


@pytest.mark.parametrize("value", [True, False, -1, 3, 2.0, "2"])
def test_retry_limit_rejects_noninteger_or_out_of_range(value):
    with pytest.raises(ValidationError):
        DevelopmentLimits(max_retries=value)


def test_policy_cannot_be_widened_with_recomputed_digest(git_repo, tmp_path, waits):
    plan, _, _, _, preview, _ = retry_setup(git_repo, tmp_path)
    preview["transport_retry"]["retryable_codes"].append("http_auth_error")
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    assert not plan.destination.exists()


@pytest.mark.parametrize("final_fails", [False, True])
def test_recovered_patch_is_applied_once_and_still_requires_final_gates(git_repo, tmp_path, waits, final_fails):
    class RetryPatch(RepairFixtureAdapter):
        transient_sent = False

        def __call__(self, wire, **kwargs):
            if json.loads(wire)["stage"] == "patch" and not self.transient_sent:
                self.transient_sent = True
                self.sent.append(wire)
                raise SafeTransportError("http_status_408")
            return super().__call__(wire, **kwargs)

    plan, config, profiles, runner, _, authority = retry_setup(git_repo, tmp_path, unbounded=True)
    if final_fails:
        codes = iter([1, 1, 1])
        runner._execute = lambda command, **kwargs: process(next(codes) if "run" in command else 0)
    adapter = RetryPatch(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert len(result["patch_attempts"]) == adapter.patch_calls == 1
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)
    assert not result["accounting"]["usage_complete"]
    assert [r["stage"] for r in result["test_runs"]] == ["baseline", "candidate", "final"]
    if final_fails:
        assert result["status"] == "blocked" and result["failure_stage"] == "final_tests"
        assert result["application"] is None and not result["final_reviews"]
        assert (git_repo / "a.py").read_text() == "old\n"
    else:
        assert result["status"] == "applied", result
        assert result["final_reviews"] and result["application"]
        assert (git_repo / "a.py").read_text() == "fixed\n"


@contextmanager
def retry_server(failures):
    calls = []
    fixture = RepairFixtureAdapter(model("http://127.0.0.1:9999/v1"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(body)
            status = 408 if len(calls) <= failures else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if status == 408:
                value = b"PRIVATE_BODY synthetic-secret"
            else:
                data = json.loads(body)
                wire = json.dumps({"stage": data["text"]["format"]["name"].removeprefix("reviewflow_"),
                                   "payload": json.loads(data["input"]),
                                   "system": data["instructions"],
                                   "response_schema": data["text"]["format"]["schema"]})
                value = json.dumps(response(fixture(wire).text)).encode()
            self.wfile.write(value)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("retries,failures,expected", [(2, 1, "applied"), (2, 3, "blocked"), (0, 1, "blocked")])
def test_actual_cli_and_worker_retry_safe_http_without_hiding_billing(
        git_repo, tmp_path, capsys, monkeypatch, waits, retries, failures, expected):
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, secret = cli_fixture(git_repo, tmp_path)
    if retries == 0:
        args += ["--transport-retries", "0"]
    original = VolumeDockerRunner.__init__
    monkeypatch.setattr(VolumeDockerRunner, "__init__", lambda self, image, **kw:
                        original(self, image, **kw, execute=lambda *a, **k: process()))
    with retry_server(failures) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert preview["config"]["limits"]["max_retries"] == retries
        assert not calls
        assert main(args + ["--approve", preview["approval_digest"]]) == (0 if expected == "applied" else 1)
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == expected, result
        assert len(calls) == result["accounting"]["requests_attempted"]
        if expected == "applied":
            assert calls[0] == calls[1]
            assert result["final_reviews"] and result["application"]
            assert result["accounting"]["records"][0]["retry_status"] == "recovered"
        else:
            assert len(calls) == retries + 1
            assert result["application"] is None
    markdown = (output / "report.md").read_text()
    assert not result["accounting"]["usage_complete"]
    assert result["accounting"]["records"][0]["error_code"] == "http_status_408"
    assert "PRIVATE_BODY" not in markdown and "synthetic-secret" not in json.dumps(result)
    assert "重试" in markdown and "未知" in markdown


@pytest.mark.parametrize("kind,cap", [("evidence_correction", 1), ("dependency_verification", 3),
                                    ("reviewer_dependency_reassessment", 3)])
def test_retries_respect_narrow_recovery_physical_request_caps(git_repo, tmp_path, waits, kind, cap):
    from test_dependency_verification import DependencyAdapter, setup_cross_batch
    from test_refresh_reviewer_dependencies import RecoveringReviewer
    from test_verification_recovery import EvidenceAdapter

    base = {"evidence_correction": EvidenceAdapter, "dependency_verification": DependencyAdapter,
            "reviewer_dependency_reassessment": RecoveringReviewer}[kind]

    class TransientRecovery(base):
        attempts = 0

        def __call__(self, wire, **kwargs):
            if kind in json.loads(wire)["payload"]:
                self.attempts += 1
                if self.attempts <= 2:
                    self.sent.append(wire)
                    raise SafeTransportError("http_status_408")
            return super().__call__(wire, **kwargs)

    values = (setup_budget(git_repo, tmp_path) if cap == 1 else setup_cross_batch(git_repo, tmp_path))
    plan, config, profiles, runner, _, _ = values
    config = config.model_copy(update={"limits": DevelopmentLimits(max_retries=2)})
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", 48,
        no_call_limit=True, patch_scheduling="grouped" if kind == "reviewer_dependency_reassessment" else "serial")
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    adapter = TransientRecovery(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert adapter.attempts == cap
    assert result["status"] == "blocked" and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)
    assert not result["accounting"]["usage_complete"]


def test_cli_retry_count_changes_require_new_approval(git_repo, tmp_path, capsys):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    assert main(args + ["--transport-retries", "0"]) == 0
    old = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    new = json.loads(capsys.readouterr().out)
    assert new["config"]["limits"]["max_retries"] == 2
    assert old["approval_digest"] != new["approval_digest"]
    assert main(args + ["--approve", old["approval_digest"]]) == 2
    assert not output.exists()

import pytest

from reviewflow.backend.transport import TransportError


@pytest.mark.parametrize(
    "error,expected",
    [
        (TransportError("run_blocked"), "run_blocked"),
        (TransportError("call_budget"), "call_budget"),
        (TransportError("request_bytes"), "request_bytes"),
        (TransportError("responses_request_rejected"), "responses_request_rejected"),
        (TransportError("request_evidence_or_scope"), "request_evidence_or_scope"),
        (TransportError("http_rate_limit"), "http_rate_limit"),
        (TransportError("synthetic-secret"), "backend_error"),
        (TransportError("run_blocked", "synthetic-secret"), "backend_error"),
        (TransportError(["run_blocked"]), "backend_error"),
        (RuntimeError("run_blocked"), "backend_error"),
    ],
)
def test_only_single_allowlisted_transport_args_are_public(error, expected):
    from reviewflow.backend.diagnostics import safe_error_code

    assert safe_error_code(error) == expected


def test_safe_errors_normalize_unknown_codes_and_fallbacks():
    from reviewflow.backend.diagnostics import SafeTransportError, safe_error_code

    error = SafeTransportError("synthetic-secret")
    assert isinstance(error, TransportError)
    assert error.args == ("backend_error",)
    assert safe_error_code(RuntimeError("synthetic-secret"), "worker_failed") == "worker_failed"
    assert safe_error_code(RuntimeError("synthetic-secret"), "synthetic-secret") == "backend_error"


def test_diagnostics_do_not_format_exception_or_accept_string_subclasses():
    from reviewflow.backend.diagnostics import SafeTransportError, safe_error_code

    class Hostile(TransportError):
        def __str__(self):
            pytest.fail("must not format an untrusted exception")

    class PretendCode(str):
        def __hash__(self):
            pytest.fail("must not hash an untrusted string subclass")

    assert safe_error_code(Hostile("synthetic-secret")) == "backend_error"
    assert safe_error_code(TransportError(PretendCode("run_blocked"))) == "backend_error"
    assert SafeTransportError(PretendCode("run_blocked")).args == ("backend_error",)

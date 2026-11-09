"""Fixed public diagnostic vocabulary; never format untrusted exceptions or streams."""

from reviewflow.backend.transport import TransportError

# A finite vocabulary for rejection diagnostics, not supported tool capabilities.
# Never interpolate an unrecognized provider value into a diagnostic.
UNSUPPORTED_OUTPUT_TYPES = {
    kind: f"response_type_{kind}_unsupported" for kind in (
        "custom_tool_call", "custom_tool_call_output", "function_call_output",
        "web_search_call", "file_search_call", "computer_call", "code_interpreter_call",
        "mcp_call", "mcp_list_tools", "mcp_approval_request", "local_shell_call",
        "shell_call", "shell_call_output", "apply_patch_call", "apply_patch_call_output",
        "compaction", "tool_call", "tool_use", "text", "output_text", "refusal",
    )
}

# Finite numeric HTTP metadata, never provider messages, headers or error bodies.
# Keep existing authentication/rate-limit codes for compatibility.
HTTP_CLIENT_STATUS_CODES = {
    status: f"http_status_{status}" for status in range(400, 500)
    if status not in (401, 403, 429)
}

# Only these codes may cross the isolated worker's failure-envelope boundary.
WORKER_ERROR_CODES = frozenset(
    {
        *UNSUPPORTED_OUTPUT_TYPES.values(),
        *HTTP_CLIENT_STATUS_CODES.values(),
        "http_redirect",
        "http_auth_error",
        "http_rate_limit",
        "http_client_error",
        "http_server_error",
        "http_error",
        "response_timeout",
        "network_error",
        "tls_error",
        "response_json_invalid",
        "response_incomplete_max_output_tokens",
        "response_incomplete_content_filter",
        "response_incomplete",
        "response_status_invalid",
        "usage_missing",
        "usage_invalid",
        "usage_output_limit",
        "response_refusal",
        "response_unsupported_output",
        "response_output_not_list",
        "response_output_item_invalid",
        "response_output_type_unsupported",
        "response_function_call_unsupported",
        "response_message_content_invalid",
        "response_content_item_invalid",
        "response_content_type_unsupported",
        "response_reasoning_invalid",
        "response_text_invalid_or_unsafe",
        "response_too_large",
        "worker_protocol_error",
        "worker_failed",
    }
)

SAFE_ERROR_CODES = WORKER_ERROR_CODES | frozenset(
    {
        "backend_error",
        "worker_timeout",
        "responses_request_rejected",
        "run_blocked",
        "call_budget",
        "request_bytes",
        "request_evidence_or_scope",
        "scope",
        "upload_disabled",
        "token_budget",
        "payload_limit",
        "pricing_unconfigured",
        "cost_or_call_budget",
        "unknown_usage",
        "invalid_usage",
        "output_limit",
        "usage_exceeded_reservation",
        "response_limit",
        "transport_failure",
        "transport_or_usage_failure",
    }
)


def _allowed(code):
    return type(code) is str and code in SAFE_ERROR_CODES


class SafeTransportError(TransportError):
    """A transport failure whose sole argument is a fixed, safe diagnostic code."""

    def __init__(self, code="backend_error", *, progress=None):
        from reviewflow.backend.worker_progress import safe_progress

        super().__init__(code if _allowed(code) else "backend_error")
        self.progress = safe_progress(progress)


def safe_error_code(error, fallback="backend_error"):
    """Accept only one exact allowlisted string argument from a TransportError."""
    if isinstance(error, TransportError) and len(error.args) == 1 and _allowed(error.args[0]):
        return error.args[0]
    return fallback if _allowed(fallback) else "backend_error"

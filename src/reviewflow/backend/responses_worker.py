"""Single isolated HTTP request; bounded stdin/stdout and no target imports.

Parent owns the hard wall-clock deadline and kills/waits this process on timeout.
This module is an implementation detail, not an independently authorized CLI.
"""

import http.client
import json
import ssl
import sys
from urllib.parse import urlsplit

from reviewflow.backend.development import development_endpoint
from reviewflow.backend.diagnostics import (
    HTTP_CLIENT_STATUS_CODES,
    UNSUPPORTED_OUTPUT_TYPES,
    SafeTransportError,
    safe_error_code,
)
from reviewflow.backend.response_safety import validate_response_text
from reviewflow.backend.worker_progress import ProgressWriter


def _http_error(status):
    if 300 <= status < 400:
        return "http_redirect"
    if status in (401, 403):
        return "http_auth_error"
    if status == 429:
        return "http_rate_limit"
    if 400 <= status < 500:
        return HTTP_CLIENT_STATUS_CODES.get(status, "http_client_error")
    if 500 <= status < 600:
        return "http_server_error"
    return "http_error"


def _response_result(raw, cap, key):
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise SafeTransportError("response_json_invalid") from None
    if not isinstance(data, dict):
        raise SafeTransportError("response_json_invalid")
    if data.get("status") == "incomplete":
        details = data.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            raise SafeTransportError("response_incomplete_max_output_tokens")
        if reason == "content_filter":
            raise SafeTransportError("response_incomplete_content_filter")
        raise SafeTransportError("response_incomplete")
    if data.get("status") != "completed":
        raise SafeTransportError("response_status_invalid")
    usage = data.get("usage")
    if usage is None:
        raise SafeTransportError("usage_missing")
    if not isinstance(usage, dict) or any(
        type(usage.get(name)) is not int or usage[name] < 0
        for name in ("input_tokens", "output_tokens")
    ):
        raise SafeTransportError("usage_invalid")
    if usage["output_tokens"] > cap:
        raise SafeTransportError("usage_output_limit")
    chunks = []
    output = data.get("output")
    if not isinstance(output, list):
        raise SafeTransportError("response_output_not_list")
    for item in output:
        if not isinstance(item, dict):
            raise SafeTransportError("response_output_item_invalid")
        if item.get("type") == "function_call":
            raise SafeTransportError("response_function_call_unsupported")
        kind = item.get("type")
        if kind not in (None, "message", "reasoning"):
            code = (UNSUPPORTED_OUTPUT_TYPES.get(kind, "response_output_type_unsupported")
                    if type(kind) is str else "response_output_type_unsupported")
            raise SafeTransportError(code)
        if item.get("type") == "reasoning":
            # Responses reasoning content is optional/null or reasoning_text[];
            # it is not the structured answer and must never be forwarded/stored.
            reasoning = item.get("content")
            if reasoning is not None and (not isinstance(reasoning, list) or any(
                not isinstance(part, dict) or part.get("type") != "reasoning_text"
                or not isinstance(part.get("text"), str) for part in reasoning
            )):
                raise SafeTransportError("response_reasoning_invalid")
            continue
        contents = item.get("content", [])
        if not isinstance(contents, list):
            raise SafeTransportError("response_message_content_invalid")
        for content in contents:
            if not isinstance(content, dict):
                raise SafeTransportError("response_content_item_invalid")
            if content.get("type") == "refusal":
                raise SafeTransportError("response_refusal")
            if content.get("type") != "output_text":
                raise SafeTransportError("response_content_type_unsupported")
            if not isinstance(content.get("text"), str):
                raise SafeTransportError("response_text_invalid_or_unsafe")
            chunks.append(content["text"])
    text = "".join(chunks)
    try:
        validate_response_text(text, key)
    except (ValueError, TypeError, RecursionError):
        raise SafeTransportError("response_text_invalid_or_unsafe") from None
    return {
        "ok": True,
        "text": text,
        "usage": {name: usage[name] for name in ("input_tokens", "output_tokens")},
    }


def main(progress_fd=None):
    progress = ProgressWriter(progress_fd)
    progress.mark()  # Worker main entered; excludes interpreter/import startup time.
    connection = None
    failure = "worker_protocol_error"
    try:
        raw = sys.stdin.buffer.read(524289)
        if len(raw) > 524288:
            raise ValueError()
        request = json.loads(raw)
        parsed = urlsplit(development_endpoint(request["base_url"]))
        key = request["api_key"]
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 4096
            or any(c.isspace() or ord(c) < 32 for c in key)
        ):
            raise ValueError()
        key.encode("ascii")
        body = request["body"].encode("utf-8")
        cap, timeout = request["max_output_tokens"], request["timeout"]
        if (
            len(body) > 65536
            or type(cap) is not int
            or not 1 <= cap <= 2048
            or type(timeout) not in (int, float)
            or not 0 < timeout <= 60
        ):
            raise ValueError()
        path = parsed.path.rstrip("/")
        if not path.endswith("/responses"):
            path += "/responses"
        failure = "worker_failed"
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        connection.connect()
        progress.mark()  # DNS/TCP and (for HTTPS) TLS completed.
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "ReviewFlow-development/0.1",
            },
        )
        progress.mark()  # Request bytes handed to the local socket, not provider acceptance.
        response = connection.getresponse()
        progress.mark()
        if response.status != 200:
            # No redirects, retries, or reads of provider error bodies/headers.
            raise SafeTransportError(_http_error(response.status))
        raw_response = response.read(262145)
        if len(raw_response) > 262144:
            raise SafeTransportError("response_too_large")
        progress.mark()
        result = _response_result(raw_response, cap, key)
        progress.mark()
        sys.stdout.write(json.dumps(result, ensure_ascii=True))
        return 0
    except SafeTransportError as error:
        failure = safe_error_code(error, "worker_failed")
    except TimeoutError:
        failure = "response_timeout"
    except ssl.SSLError:
        failure = "tls_error"
    except (OSError, http.client.HTTPException):
        failure = "network_error"
    except Exception:
        pass  # Unrecognized exceptions never contribute text to the envelope.
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    sys.stdout.write(json.dumps({"ok": False, "error_code": failure}))
    return 1


if __name__ == "__main__":
    fd = int(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "--progress-fd" else None
    raise SystemExit(main(fd))

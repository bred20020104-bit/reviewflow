"""Explicit, bounded development traffic. No claimed monetary or input-token bound.

This is deliberately separate from the fail-closed priced GuardedTransport.
Only the live development controller enables uploads and grants bounded requests.
"""

import tomllib
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, field_validator

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.config import (
    ConfigError,
    LocalReference,
    ModelConfig,
    PrivacyConfig,
    Text,
    read_bounded,
)
from reviewflow.backend.credentials import MODEL_FIELDS, PRIVATE_FIELDS, _read_private_file
from reviewflow.backend.diagnostics import SafeTransportError, safe_error_code
from reviewflow.backend.transport import Reply, TransportError, Usage
from reviewflow.review.contracts import ReviewMessage


def development_endpoint(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(c.isspace() for c in value)
        or (parsed.scheme == "http" and parsed.hostname.lower() == "api.openai.com")
    ):
        raise ValueError("Invalid development endpoint")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid port")
    return value


class DevelopmentModel(ModelConfig):
    base_url: Annotated[Text, AfterValidator(development_endpoint)]
    api_type: Literal["responses"]


class DevelopmentLimits(ReviewMessage):
    max_calls: Literal[8] = 8
    max_request_bytes: Literal[65536] = 65536
    max_output_tokens: Literal[2048] = 2048
    timeout_seconds: Literal[60] = 60
    max_retries: Literal[0, 1, 2] = 0

    @field_validator("max_retries", mode="before")
    @classmethod
    def strict_retries(cls, value):
        if type(value) is not int:
            raise ValueError("Retry count must be an integer")
        return value


RETRYABLE_CODES = frozenset({"http_status_408", "response_timeout", "worker_timeout"})
RETRY_DELAYS = (1, 2)


def transport_retry_policy(retries):
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError("Invalid retry count")
    if retries == 0:
        return None
    return {"version": 1, "max_retries": retries,
            "retryable_codes": sorted(RETRYABLE_CODES),
            "backoff_seconds": list(RETRY_DELAYS[:retries]),
            "authorization": "fresh_per_attempt_same_intent",
            "accounting": "all_attempts_unknown_usage_retained",
            "recovery": "owned_success_same_request_only"}


class DevelopmentConfig(ReviewMessage):
    model: DevelopmentModel
    privacy: PrivacyConfig
    limits: DevelopmentLimits = DevelopmentLimits()
    accounting: Literal["development_unknown_price"] = "development_unknown_price"


def load_development_config(path, source, inventory):
    """Explicit simplified config only. Does not enable upload or retain a Key."""
    try:
        reference = LocalReference.model_validate(tomllib.loads(read_bounded(path).decode("utf-8")))
        private = Path(path).parent.resolve(strict=True) / reference.secrets_file
        values = _read_private_file(private)
        if set(values) != PRIVATE_FIELDS:
            raise ValueError()
        return DevelopmentConfig(
            model=DevelopmentModel.model_validate(
                {
                    **{name: values[name] for name in MODEL_FIELDS},
                    "api_key_file": private.name,
                }
            ),
            privacy=PrivacyConfig(
                allowed_project_root=str(source.info.root.resolve()),
                allowed_paths=tuple(
                    r.path for r in inventory.snapshot.records if r.status == "included"
                ),
            ),
        )
    except (ValueError, OSError):
        raise ConfigError("Invalid development configuration") from None


class DevelopmentTransport:
    """Gateway-compatible request control; byte size is explicitly not token usage."""

    def __init__(self, config, adapter, approvals, *, retry_authorize=None):
        self.config = DevelopmentConfig.model_validate(config)
        if self.config.limits.max_retries and not callable(retry_authorize):
            raise ValueError("Retries require fresh per-attempt controller authorization")
        self.adapter = adapter
        self.approvals = approvals
        self.calls = 0
        self.blocked = False
        self.records = []
        self.cost_usd = None
        self._retry_authorize = retry_authorize
        self._recovered_failures = {}
        self._lock = Lock()

    def is_recovered_failure(self, record):
        """Only this live transport's sealed successful retry can recover a failure."""
        return (type(record.get("call")) is int
                and self._recovered_failures.get(record["call"]) == fingerprint(record))

    def intent(self, snapshot, files, payload, input_upper_bound):
        if not files or not set(files) <= set(self.config.privacy.allowed_paths):
            raise TransportError("scope")
        if (
            not isinstance(payload, str)
            or len(payload.encode("utf-8")) > 2097152
            or type(input_upper_bound) is not int
            or not 0 < input_upper_bound <= self.config.limits.max_request_bytes
            or input_upper_bound != self.adapter.body_size(payload)
        ):
            raise TransportError("request_bytes")
        return {
            "action": "development_send",
            "snapshot": snapshot,
            "files": dict(files),
            "root": self.config.privacy.allowed_project_root,
            "config": self.config.model_dump(mode="json"),
            "payload_sha256": fingerprint({"text": payload}),
            "request_bytes": input_upper_bound,
        }

    def _attempt(self, payload, record):
        from reviewflow.backend.worker_progress import safe_progress

        reply = self.adapter(payload, max_output_tokens=2048, timeout_seconds=60)
        if not isinstance(reply, Reply) or not isinstance(reply.usage, Usage):
            raise TransportError("unknown_usage")
        progress = safe_progress(reply.progress)
        if progress is not None:
            record["transport_progress"] = progress
        usage = reply.usage
        if any(type(n) is not int or n < 0 for n in (usage.input_tokens, usage.output_tokens)):
            raise TransportError("invalid_usage")
        record["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if usage.output_tokens > 2048:
            raise TransportError("output_limit")
        if not isinstance(reply.text, str) or len(reply.text.encode("utf-8")) > 131072:
            raise TransportError("response_limit")
        record["status"] = "complete"
        return reply.text

    def call(self, snapshot, files, payload, input_upper_bound, grant):
        with self._lock:
            if self.blocked:
                raise TransportError("run_blocked")
            if not self.config.privacy.allow_code_upload:
                raise TransportError("upload_disabled")
            intent = self.intent(snapshot, files, payload, input_upper_bound)
            if self.calls >= self.config.limits.max_calls:
                raise TransportError("call_budget")
            pending = []
            request_call = self.calls + 1
            for attempt in range(self.config.limits.max_retries + 1):
                if attempt:
                    previous = pending[-1]
                    try:
                        if self.calls >= self.config.limits.max_calls:
                            previous["retry_status"] = "budget_exhausted"
                            raise TransportError("call_budget")
                        previous["retry_status"] = "waiting"
                        sleep(RETRY_DELAYS[attempt - 1])
                        if self.intent(snapshot, files, payload, input_upper_bound) != intent:
                            raise TransportError("request_evidence_or_scope")
                        # Parent rechecks origin/candidate, expiry, phase and total
                        # allowance AFTER backoff, then issues a NEW consumed grant.
                        grant = self._retry_authorize(intent)
                        self.approvals.consume(grant, intent)
                    except Exception:
                        self.blocked = True
                        for old in pending[:-1]:
                            old["retry_status"] = "failed"
                        if previous["retry_status"] != "budget_exhausted":
                            previous["retry_status"] = "authorization_stopped"
                        raise SafeTransportError(previous["error_code"]) from None
                else:
                    self.approvals.consume(grant, intent)
                self.calls += 1
                record = {"call": self.calls, "intent": fingerprint(intent),
                          "status": "unknown_usage", "cost_usd": None, "usage": None,
                          "error_code": None, "request_call": request_call, "retry_attempt": attempt}
                try:
                    text = self._attempt(payload, record)
                    for old in pending:
                        old.update(retry_status="recovered", recovered_by_call=self.calls)
                        self._recovered_failures[old["call"]] = fingerprint(old)
                    return text
                except Exception as error:
                    from reviewflow.backend.worker_progress import safe_progress

                    progress = safe_progress(getattr(error, "progress", None))
                    if progress is not None:
                        record["transport_progress"] = progress
                    record["status"] = "transport_or_usage_failure"
                    record["error_code"] = safe_error_code(error)
                    if (record["error_code"] in RETRYABLE_CODES
                            and attempt < self.config.limits.max_retries):
                        record["retry_status"] = "pending"
                        pending.append(record)
                        continue
                    self.blocked = True
                    for old in pending:
                        old["retry_status"] = "failed"
                    record["retry_status"] = (
                        "disabled" if not self.config.limits.max_retries else
                        "exhausted" if record["error_code"] in RETRYABLE_CODES else "not_retryable")
                    raise SafeTransportError(record["error_code"]) from None
                finally:
                    self.records.append(record)

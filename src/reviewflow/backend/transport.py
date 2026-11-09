"""Budget/approval boundary for a future trusted SDK adapter; no live SDK here.

Input counts must be an adapter-proven upper bound for the complete serialized request.
The adapter must enforce timeout/output caps and disable its internal retries. Monetary
caps rely on supplied prices and provider accounting; unknown usage stops the run.
"""

from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from reviewflow.backend.approval import fingerprint


class TransportError(ValueError):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage | None
    progress: dict | None = None


class GuardedTransport:
    def __init__(self, config, transport, approvals):
        self.config = config
        self.transport = transport
        self.approvals = approvals
        self.spent = Decimal(0)
        self.calls = 0
        self.blocked = False
        self.records = []
        self._lock = Lock()

    def intent(self, snapshot, files, payload, input_upper_bound):
        if not files or not set(files) <= set(self.config.privacy.allowed_paths):
            raise TransportError("scope")
        if (
            type(input_upper_bound) is not int
            or not 0 < input_upper_bound <= self.config.budget.max_input_tokens
        ):
            raise TransportError("token_budget")
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > 2097152:
            raise TransportError("payload_limit")
        return {
            "action": "send",
            "snapshot": snapshot,
            "files": dict(files),
            "root": self.config.privacy.allowed_project_root,
            "config": self.config.model_dump(mode="json"),
            "payload_sha256": fingerprint({"text": payload}),
            "input_upper_bound": input_upper_bound,
        }

    def _cost(self, input_tokens, output_tokens):
        budget = self.config.budget
        if (
            budget.input_price_per_million_usd is None
            or budget.output_price_per_million_usd is None
        ):
            raise TransportError("pricing_unconfigured")
        return (
            Decimal(input_tokens) * Decimal(budget.input_price_per_million_usd)
            + Decimal(output_tokens) * Decimal(budget.output_price_per_million_usd)
        ) / 1000000

    def call(self, snapshot, files, payload, input_upper_bound, grant):
        # Serial execution makes reservation/settlement atomic across callers.
        with self._lock:
            if self.blocked:
                raise TransportError("run_blocked")
            if not self.config.privacy.allow_code_upload:
                raise TransportError("upload_disabled")
            intent = self.intent(snapshot, files, payload, input_upper_bound)
            budget = self.config.budget
            reserve = self._cost(input_upper_bound, budget.max_output_tokens)
            if (
                self.calls >= budget.max_calls
                or reserve > Decimal(budget.max_call_cost_usd)
                or self.spent + reserve > Decimal(budget.max_run_cost_usd)
            ):
                raise TransportError("cost_or_call_budget")
            self.approvals.consume(grant, intent)
            self.spent += reserve
            self.calls += 1
            status = "unknown_usage"
            try:
                reply = self.transport(
                    payload,
                    max_output_tokens=budget.max_output_tokens,
                    timeout_seconds=budget.timeout_seconds,
                )
                if not isinstance(reply, Reply) or not isinstance(reply.usage, Usage):
                    raise TransportError("unknown_usage")
                usage = reply.usage
                if any(
                    type(n) is not int or n < 0 for n in (usage.input_tokens, usage.output_tokens)
                ):
                    raise TransportError("invalid_usage")
                actual = self._cost(usage.input_tokens, usage.output_tokens)
                if (
                    usage.input_tokens > input_upper_bound
                    or usage.output_tokens > budget.max_output_tokens
                ):
                    self.spent += max(Decimal(0), actual - reserve)
                    raise TransportError("usage_exceeded_reservation")
                self.spent += actual - reserve
                if not isinstance(reply.text, str) or len(reply.text.encode("utf-8")) > 131072:
                    raise TransportError("response_limit")
                status = "complete"
                return reply.text
            except Exception as exc:
                self.blocked = True
                status = str(exc) if isinstance(exc, TransportError) else "transport_failure"
                raise TransportError(status) from None
            finally:
                self.records.append(
                    {
                        "call": self.calls,
                        "intent": fingerprint(intent),
                        "status": status,
                        "charged_or_reserved_usd": str(self.spent),
                    }
                )

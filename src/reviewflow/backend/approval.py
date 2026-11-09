"""Process-local, expiring, one-shot grants. Not a sandbox against trusted Python callers."""

import hashlib
import json
import secrets
import threading
import time


class Denied(ValueError):
    pass


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


class ApprovalGate:
    """Only a trusted UI may translate an explicit user action into confirmed=True.

    Grants are never loaded from files, backend responses or model-generated JSON.
    Restarting a process requires new approval; audit digests do not grant authority.
    """

    def __init__(self, *, clock=time.monotonic, ttl_seconds=900):
        if not 0 < ttl_seconds <= 3600:
            raise ValueError("Invalid approval lifetime")
        self._clock = clock
        self._ttl = ttl_seconds
        self._grants = {}
        self._lock = threading.Lock()

    def approve(self, intent: dict, *, confirmed: bool) -> str:
        if confirmed is not True:
            raise Denied("Explicit approval required")
        token = secrets.token_hex(32)
        with self._lock:
            now = self._clock()
            self._grants = {k: v for k, v in self._grants.items() if v[1] > now}
            self._grants[token] = (fingerprint(intent), now + self._ttl)
        return token

    def consume(self, token: str, intent: dict) -> None:
        with self._lock:
            record = self._grants.get(token)
            if not record or record[1] <= self._clock() or record[0] != fingerprint(intent):
                raise Denied("Approval missing, expired or mismatched")
            del self._grants[token]

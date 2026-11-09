"""Six fixed milestones on a private inherited descriptor, never stdout/stderr.

The parent reads at most 31 bytes after the worker exits/is killed. Each event is
one stage byte plus a uint32 elapsed millisecond count, relative to worker main.
Missing/invalid telemetry is discarded; it cannot authorize a response or retry.
"""
import os
import struct
import time

STAGES = ("worker_started", "connection_established", "request_sent", "response_headers",
          "response_body", "response_validated")
_RECORD = struct.Struct("!BI")
MAX_PROGRESS_BYTES = len(STAGES) * _RECORD.size


def safe_progress(value):
    if (type(value) is not dict or set(value) != {"version", "events"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["events"]) is not list
            or not 1 <= len(value["events"]) <= len(STAGES)):
        return None
    events = []
    previous = 0
    for index, event in enumerate(value["events"]):
        if (type(event) is not dict or set(event) != {"stage", "elapsed_ms"}
                or type(event["stage"]) is not str or event["stage"] != STAGES[index]
                or type(event["elapsed_ms"]) is not int
                or not previous <= event["elapsed_ms"] <= 60000):
            return None
        previous = event["elapsed_ms"]
        events.append({"stage": STAGES[index], "elapsed_ms": previous})
    return {"version": 1, "events": events}


def decode_progress(raw):
    if (type(raw) is not bytes or not raw or len(raw) > MAX_PROGRESS_BYTES
            or len(raw) % _RECORD.size):
        return None
    events = []
    for expected, (index, elapsed) in enumerate(_RECORD.iter_unpack(raw)):
        if index != expected:
            return None
        events.append({"stage": STAGES[index], "elapsed_ms": elapsed})
    return safe_progress({"version": 1, "events": events})


class ProgressWriter:
    def __init__(self, fd=None):
        self.fd = fd
        self.start = time.monotonic_ns()
        self.index = 0

    def mark(self):
        if self.fd is not None and self.index < len(STAGES):
            elapsed = min(60000, (time.monotonic_ns() - self.start) // 1000000)
            try:
                os.write(self.fd, _RECORD.pack(self.index, elapsed))
            except OSError:
                pass  # Diagnostics never change transport acceptance or retry behavior.
        self.index += 1

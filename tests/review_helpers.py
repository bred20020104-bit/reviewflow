"""Synthetic protocol payloads, never real model output."""


def evidence(**changes):
    return {
        "snapshot_id": "a" * 64,
        "path": "src/main.py",
        "content_sha256": "b" * 64,
        "start_line": 1,
        "end_line": 2,
        **changes,
    }


def task(**changes):
    return {
        "task_id": "task-1",
        "snapshot_id": "a" * 64,
        "role": "correctness",
        "paths": ("src/main.py",),
        "objective": "Check boundary handling",
        **changes,
    }


def finding(**changes):
    return {
        "finding_id": "finding-1",
        "task_id": "task-1",
        "title": "Boundary issue",
        "root_cause": "Missing guard",
        "trigger": "Empty input",
        "impact": "Raises unexpectedly",
        "severity": "medium",
        "evidence": (evidence(),),
        **changes,
    }

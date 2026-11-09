"""Bounded observations from owned tests, with no test paths or source uploaded."""

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.repair import RepairError
from reviewflow.backend.test_diagnostics import CATEGORIES, DIAGNOSTIC_STATES, PHASES, TEST_CODES


def _identity(failure):
    # Assertion location/category and parameter cases are not stable test identities.
    return (failure["project"], failure["path"], failure["test_line"])


def new_failing_definitions(previous, current):
    """None means unknown; a positive count is a conservative rollback signal, not causality."""
    if any(r.get("diagnostics_status") != "complete" or r.get("status") not in {"passed", "failed"}
           for r in (previous, current)):
        return None
    before = {_identity(f) for f in previous["test_failures"]}
    # Collection locations have no test definition; infrastructure handling remains separate.
    after = {_identity(f) for f in current["test_failures"] if f["test_line"] is not None}
    return len(after - before)


def observation(record):
    """Project only fixed categories, numeric locations and opaque test identities."""
    if (record["status"] not in {"passed", "failed"}
            or record["diagnostics_status"] not in DIAGNOSTIC_STATES
            or len(record["test_failures"]) > 50):
        raise RepairError("Invalid test observation")
    failures = []
    for failure in record["test_failures"]:
        if (failure["category"] not in CATEGORIES or failure["phase"] not in PHASES
                or type(failure["line"]) is not int or failure["line"] < 1
                or (failure["test_line"] is not None
                    and (type(failure["test_line"]) is not int or failure["test_line"] < 1))):
            raise RepairError("Invalid test observation")
        failures.append({"definition_id": fingerprint(_identity(failure)),
            "test_line": failure["test_line"], "line": failure["line"],
            "phase": failure["phase"], "category": failure["category"]})
    return {"content_digest": record["content_digest"], "status": record["status"],
            "diagnostics_status": record["diagnostics_status"],
            "diagnostic_code": record.get("diagnostic_code")
                if record.get("diagnostic_code") in TEST_CODES else None,
            "failures": failures}


def current_observation(candidate, record):
    if record["content_digest"] != candidate.current_digest():
        raise RepairError("Test observation belongs to different candidate content")
    return observation(record)

"""Test-run correlation and safe, observation-only repair diagnostics."""

from reviewflow.backend.test_diagnostics import CATEGORIES, DIAGNOSTIC_STATES, PHASES, TEST_CODES
from reviewflow.report import _markdown as escape_markdown


def compare_runs(previous, current):
    """Compare complete diagnostic sets, not inferred business correctness or counts."""
    result = {"comparable": False, "new_failures": [], "no_longer_reported": []}
    if (previous is None or any(r.get("diagnostics_status") != "complete"
            or r.get("status") not in {"passed", "failed"} for r in (previous, current))):
        return result
    before, after = previous["test_failures"], current["test_failures"]
    result.update(comparable=True,
                  new_failures=[r for r in after if r not in before],
                  no_longer_reported=[r for r in before if r not in after])
    return result


def render_test_runs(runs):
    lines = ["\n## Test diagnostics by round\n"]
    for number, run in enumerate(runs, 1):
        stage = run.get("stage")
        stage = stage if stage in {"baseline", "seed", "candidate", "final"} else "unknown"
        code = run.get("diagnostic_code")
        code = code if code in TEST_CODES else "unknown"
        state = run.get("diagnostics_status")
        state = state if state in DIAGNOSTIC_STATES else "invalid"
        lines.append(f"- Round {number} / {stage}: {code}; detail status: {state}")
        if code == "test_assertions_failed":
            lines.append(
                "  - Test execution failed. This may be an assertion or a runtime exception; "
                "do not attribute it from the exit code alone."
            )
        for failure in run.get("test_failures", ())[:50]:
            if (failure.get("category") not in CATEGORIES or failure.get("phase") not in PHASES
                    or type(failure.get("path")) is not str
                    or type(failure.get("line")) is not int):
                continue
            path = escape_markdown(failure["path"])
            location = failure.get("test_line")
            location = location if type(location) is int else failure["line"]
            lines.append(
                f"  - {path}:{location} — {failure['phase']} / {failure['category']}"
                f"; error location: {failure['line']}"
            )
        comparison = run.get("comparison", {})
        if number > 1:
            if comparison.get("comparable") is True:
                lines.append(
                    f"  - Compared with the previous round: "
                    f"{len(comparison['new_failures'])} new diagnostics; "
                    f"{len(comparison['no_longer_reported'])} no longer reported."
                )
            else:
                lines.append(
                    "  - Diagnostics are missing, incomplete, or blocked, "
                    "so failure changes cannot be compared reliably."
                )
    lines.append(
        "\nCases are identified by the test definition location. "
        "Parameterized instances are not expanded. Error summaries keep only the fixed category "
        "and location, not raw logs, exception messages, or assertion values. "
        "A diagnostic change does not mean the business defect is fixed, "
        "and an error category alone does not prove that an environment limit is the root cause.\n"
    )
    return "\n".join(lines)

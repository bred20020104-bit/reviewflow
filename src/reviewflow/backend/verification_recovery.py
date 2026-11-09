"""One independent evidence correction per failed finding, under parent authority."""

import json
from dataclasses import replace

from pydantic import ValidationError

from reviewflow.backend.approval import Denied
from reviewflow.backend.context_index import ContextIndex
from reviewflow.backend.repair import RepairError
from reviewflow.backend.repair_authority import validate_review_provenance, validate_scoped_review
from reviewflow.backend.transport import TransportError
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import VerificationResult
from reviewflow.review.evidence_errors import (
    RECOVERABLE_EVIDENCE_CODES,
    EvidenceValidationError,
)
from reviewflow.review.workflow_models import ModelRequest

RECOVERY_POLICY = {
    "version": 1,
    "max_attempts_per_finding": 1,
    "requests_per_recovery_phase": 1,
    "recoverable_codes": sorted(RECOVERABLE_EVIDENCE_CODES),
}


def recover_verification(candidate, report, authority, profiles, adapter, audit, *, stage):
    """Return a new effective report, never erase the original report or retry a send."""
    failed = [o for o in report.findings if o.error and o.verification is None]
    expected = [f"verifier/{o.finding.finding_id}: {o.error}" for o in failed]
    if (not failed or any(o.error not in RECOVERABLE_EVIDENCE_CODES for o in failed)
            or sorted(expected) != sorted(report.errors)):
        return report
    # Check all unaffected outcomes, both-role coverage and current provenance first.
    # The temporary filtered report is never returned, stored as success or applied.
    cross_batch = bool(set(candidate.plan.selected) - set(report.included_paths))
    validate = validate_review_provenance if cross_batch else validate_scoped_review
    validate(candidate, replace(report, errors=(), findings=tuple(
        o for o in report.findings if not o.error)))
    source, inventory = candidate.source_inventory(report.included_paths)
    context = ReviewContext(source, inventory)
    tasks = {t.task.task_id: t.task for t in report.tasks}
    for outcome in failed:
        context.validate_finding(tasks[outcome.finding.task_id], outcome.finding)
    index = ContextIndex(source, inventory, max_bytes=32768, max_chunks=1000)
    files = [{"evidence": p.evidence.model_dump(mode="json"), "text": p.text}
             for p in index.packets]
    effective = list(report.findings)
    for outcome in failed:
        identity = (stage, report.snapshot_id, outcome.finding.finding_id)
        if any((a["stage"], a["snapshot_id"], a["finding_id"]) == identity for a in audit):
            raise RepairError("Evidence recovery already attempted")
        entry = {"stage": stage, "snapshot_id": report.snapshot_id,
                 "finding_id": outcome.finding.finding_id, "attempt": 1,
                 "original_error": outcome.error, "status": "failed",
                 "diagnostic_code": None, "requests_attempted": 0}
        audit.append(entry)
        phase = None
        try:
            if authority.call_budget_exhausted:
                entry["diagnostic_code"] = "recovery_budget_exhausted"
                raise RepairError("No parent budget for evidence correction")
            phase = authority.start_phase(candidate, report.included_paths, adapter, profiles,
                max_requests=RECOVERY_POLICY["requests_per_recovery_phase"])
            payload = {"task": tasks[outcome.finding.task_id].model_dump(mode="json"),
                       "finding": outcome.finding.model_dump(mode="json"), "files": files,
                       "evidence_correction": {
                           "reason": outcome.error,
                           "expected_finding_id": outcome.finding.finding_id,
                           "attempt": 1,
                           "instruction": "Independently verify this finding again. The prior "
                           "response failed evidence validation. Use the exact finding ID and "
                           "supplied file/snapshot/hash metadata and valid line ranges. Do not "
                           "assume acceptance; accepted, rejected or unresolved are permitted. "
                           "No tools or further correction requests are available.",
                       }}
            text = phase.gateway.complete(ModelRequest(
                "verifier", profiles.system_for("verifier"), json.dumps(payload),
                VerificationResult.model_json_schema()))
            result = VerificationResult.model_validate_json(text)
            # Recheck ownership/source after the response, before accepting any metadata.
            authority.check(candidate)
            context.validate_verification(tasks[outcome.finding.task_id], outcome.finding, result)
            if result.verdict == "unresolved" and not cross_batch:
                entry["diagnostic_code"] = "verification_unresolved"
                raise RepairError("Evidence correction is unresolved")
            effective[effective.index(outcome)] = replace(outcome, error=None, verification=result)
            # Persist this validated result even if a later correction fails.
            entry.update(status="recovered", verdict=result.verdict,
                         verification=result.model_dump(mode="json"))
        except Exception as exc:
            if entry["diagnostic_code"] is None:
                entry["diagnostic_code"] = (
                    exc.code if isinstance(exc, EvidenceValidationError) else
                    "invalid_output" if isinstance(exc, ValidationError) else
                    "recovery_authorization_stopped" if isinstance(exc, Denied) else
                    "recovery_transport_stopped" if isinstance(exc, TransportError) else
                    "recovery_stopped")
            raise
        finally:
            if phase is not None:
                entry["requests_attempted"] = phase.transport.calls
                authority.record_phase(phase)
    recovered = replace(report, findings=tuple(effective), errors=())
    # A corrected but semantically unknown cross-batch verdict is NOT accepted.
    # The controller must resolve dependencies and then pass the strict gate.
    validate(candidate, recovered)
    return recovered

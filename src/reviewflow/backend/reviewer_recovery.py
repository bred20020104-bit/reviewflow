"""Bounded reissue of a reviewer task whose evidence metadata failed validation."""

import json
from dataclasses import replace

from reviewflow.backend.approval import Denied
from reviewflow.backend.repair import RepairError
from reviewflow.backend.repair_authority import validate_review_provenance
from reviewflow.backend.transport import TransportError
from reviewflow.review.context import ReviewContext
from reviewflow.review.evidence_errors import RECOVERABLE_EVIDENCE_CODES, EvidenceValidationError
from reviewflow.review.interactive import run_reviewer_correction
from reviewflow.review.report import render_review

REVIEWER_CODES = (RECOVERABLE_EVIDENCE_CODES - {"verification_finding_mismatch"}) | {
    "finding_evidence_not_reviewed",
}
REVIEWER_RECOVERY_POLICY = {
    "version": 2,
    "max_attempts_per_task": 1,
    "max_attempts_per_run": 8,
    "max_requests_per_attempt": 8,
    "recoverable_codes": sorted(REVIEWER_CODES),
}


def recover_reviewers(candidate, report, authority, profiles, adapter, audit, *, stage):
    """Keep the original report; do not repair hashes or accept unverified findings."""
    failed = [t for t in report.tasks if t.status == "failed"]
    if not failed or any(t.error not in REVIEWER_CODES for t in failed):
        return report
    expected = [f"reviewer/{t.task.task_id}: {t.error}" for t in failed]
    expected += [f"verifier/{o.finding.finding_id}: {o.error}" for o in report.findings
                 if o.error in RECOVERABLE_EVIDENCE_CODES and o.verification is None]
    if sorted(report.errors) != sorted(expected):
        return report
    authority.check(candidate)
    source, inventory = candidate.source_inventory(report.included_paths)
    context = ReviewContext(source, inventory)
    if (report.execution != "live_development" or report.mode != "multi"
            or report.snapshot_id != inventory.snapshot.snapshot_id or report.omissions
            or set(report.supplied_paths) != set(report.included_paths)
            or len({t.task.task_id for t in report.tasks}) != len(report.tasks)
            or {t.task.role for t in report.tasks} != {"correctness", "security"}):
        raise RepairError("Reviewer correction requires current complete task scope")
    tasks = {t.task.task_id: t for t in report.tasks}
    for outcome in report.tasks:
        context.validate_task(outcome.task)
        if set(outcome.task.paths) != set(report.included_paths):
            raise RepairError("Reviewer correction cannot enlarge task scope")
        if outcome not in failed and (outcome.status != "completed" or outcome.error
                or set(outcome.reviewed_paths) != set(outcome.task.paths)):
            raise RepairError("Unaffected reviewer coverage incomplete")
    for outcome in report.findings:
        owner = tasks[outcome.finding.task_id]
        if owner in failed or owner.task.role != outcome.role:
            raise RepairError("Ambiguous reviewer finding ownership")
        context.validate_finding(owner.task, outcome.finding)
        if outcome.verification is not None:
            context.validate_verification(owner.task, outcome.finding, outcome.verification)

    effective = report
    for failed_task in failed:
        task = failed_task.task
        identity = (stage, report.snapshot_id, tuple(sorted(report.included_paths)), task.task_id)
        if len(audit) >= REVIEWER_RECOVERY_POLICY["max_attempts_per_run"] or any(
            (a["stage"], a["snapshot_id"], tuple(sorted(a["review_paths"])), a["task_id"]) == identity
            for a in audit
        ):
            raise RepairError("Reviewer correction attempt limit")
        entry = {"stage": stage, "snapshot_id": report.snapshot_id,
                 "review_paths": list(report.included_paths), "task_id": task.task_id,
                 "original_error": failed_task.error, "attempt": 1, "status": "failed",
                 "diagnostic_code": None, "requests_attempted": 0}
        audit.append(entry)
        phase = None
        try:
            phase = authority.start_phase(candidate, report.included_paths, adapter, profiles)
            corrected = run_reviewer_correction(phase.source, phase.inventory, phase.gateway,
                task, failed_task.error, profiles=profiles)
            authority.check(candidate)
            corrected = replace(corrected, execution="live_development",
                semantic_review_performed=any(t.status == "completed" for t in corrected.tasks))
            entry["report"] = json.loads(render_review(corrected, "json"))
            # A correction is a complete re-review, not another recovery chain.
            if (corrected.errors or corrected.omissions or len(corrected.tasks) != 1
                    or corrected.tasks[0].status != "completed"
                    or corrected.tasks[0].task != task
                    or set(corrected.tasks[0].reviewed_paths) != set(task.paths)
                    or any(o.error or o.verification is None for o in corrected.findings)):
                raise RepairError("Reviewer correction failed validation")
            findings = effective.findings + corrected.findings
            if (len({o.finding.finding_id for o in findings}) != len(findings)
                    or len(findings) > report.limits.max_findings):
                raise RepairError("Reviewer correction finding identity or limit")
            outcomes = tuple(corrected.tasks[0] if t.task.task_id == task.task_id else t
                             for t in effective.tasks)
            reviewed = {p for t in outcomes for p in t.reviewed_paths}
            covered = set(report.included_paths)
            for role in ("correctness", "security"):
                covered &= {p for t in outcomes if t.task.role == role and t.status == "completed"
                            for p in t.reviewed_paths}
            effective = replace(effective, tasks=outcomes, findings=findings,
                calls=effective.calls + corrected.calls,
                limits=effective.limits.model_copy(update={"max_calls": effective.limits.max_calls + 8}),
                errors=tuple(e for e in effective.errors if e != f"reviewer/{task.task_id}: {failed_task.error}"),
                reported_reviewed_paths=tuple(sorted(reviewed)),
                unreviewed_paths=tuple(p for p in report.included_paths if p not in covered),
                status="partial", semantic_review_performed=True)
            entry["status"] = "recovered"
        except Exception as exc:
            entry["diagnostic_code"] = (
                exc.code if isinstance(exc, EvidenceValidationError) else
                "reviewer_recovery_authorization_stopped" if isinstance(exc, Denied) else
                "reviewer_recovery_transport_stopped" if isinstance(exc, TransportError) else
                "reviewer_recovery_stopped")
            raise
        finally:
            if phase is not None:
                entry["requests_attempted"] = phase.transport.calls
                authority.record_phase(phase)
    # Existing verifier metadata errors may proceed only to the independent correction
    # gate. Semantic unknowns proceed only to dependency resolution, never to patching.
    if not effective.errors:
        validate_review_provenance(candidate, effective)
    return effective

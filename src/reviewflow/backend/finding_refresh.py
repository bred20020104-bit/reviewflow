"""Bounded post-patch review. Historical claims never serve as current evidence."""

import json
from dataclasses import replace
from typing import Annotated

from pydantic import Field, ValidationError

from reviewflow.backend.approval import Denied
from reviewflow.backend.context_index import ContextIndex
from reviewflow.backend.dependency_verification import (
    DEPENDENCY_CODES,
    DEPENDENCY_POLICY,
    DependencyError,
    verify_dependencies,
)
from reviewflow.backend.repair import RepairError
from reviewflow.backend.repair_authority import validate_scoped_review
from reviewflow.backend.reviewer_reassessment import (
    REASSESSMENT_POLICY,
    Reassessment,
    ReassessmentError,
    reassess_reviewer,
)
from reviewflow.backend.transport import TransportError
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import (
    Evidence,
    Finding,
    LongText,
    RelativePath,
    ReviewMessage,
    VerificationResult,
)
from reviewflow.review.evidence_errors import RECOVERABLE_EVIDENCE_CODES, EvidenceValidationError
from reviewflow.review.report import render_review
from reviewflow.review.workflow_models import FindingOutcome, ModelRequest, RunLimits, TaskOutcome

REFRESH_POLICY = {
    "version": 4,
    "reviewer_dependency_recovery": dict(REASSESSMENT_POLICY),
    "max_verifier_batch_size": 2,
    "max_batch_evidence_per_verdict": 3,
    "max_batch_rationale_chars": 200,
    "max_verifier_corrections_per_finding": 1,
    "max_rounds": 4,
    "max_findings_per_round": 8,
    "max_total_patch_attempts": 8,
    "roles": ["correctness", "security"],
}

REFRESH_CODES = frozenset({
    "refresh_limit_exhausted", "refresh_context_incomplete", "refresh_invalid_queue",
    "refresh_budget_exhausted", "refresh_role_coverage_invalid", "refresh_incomplete_assessment",
    "refresh_unresolved", "refresh_findings_exhausted", "refresh_duplicate_identity",
    "refresh_verdict_disagreement", "refresh_invalid_output", "refresh_authorization_stopped",
    "refresh_transport_stopped", "refresh_stopped", "refresh_verification_correction_limit",
    "refresh_reviewer_limit_exhausted", "refresh_reviewer_already_attempted",
    "refresh_reviewer_context_exhausted", "refresh_reviewer_evidence_not_supplied",
    "refresh_reviewer_dependency_not_read", "refresh_reviewer_read_limit",
}) | DEPENDENCY_CODES


class RefreshReview(ReviewMessage):
    assessments: Annotated[tuple[Reassessment, ...], Field(max_length=8)]
    new_findings: Annotated[tuple[Finding, ...], Field(max_length=8)]
    reviewed_paths: Annotated[tuple[RelativePath, ...], Field(max_length=20)]


class CompactVerification(VerificationResult):
    rationale: Annotated[LongText, Field(max_length=200)]
    evidence: Annotated[Evidence, Field(max_length=3)]


class RefreshVerdicts(ReviewMessage):
    verdicts: Annotated[tuple[CompactVerification, ...], Field(min_length=1, max_length=2)]


def _verify_batch(complete, context, task, files, findings):
    payload = {"task": task.model_dump(mode="json"), "files": files,
        "refresh_verification": "Independently check each issue against current supplied code. "
        "Accept only if it exists, reject if it does not, otherwise return unresolved. "
        "Never assume an earlier patch is correct. Return exactly one verdict per finding ID. "
        "For a batch, use at most3 evidence references and200 rationale characters per verdict; "
        "if these cannot establish a decisive verdict, return unresolved."}
    if len(findings) == 1:
        payload["finding"] = findings[0].model_dump(mode="json")
        verdicts = (VerificationResult.model_validate_json(complete(
            "verifier", payload, VerificationResult.model_json_schema())),)
    else:
        payload["findings"] = [f.model_dump(mode="json") for f in findings]
        output = RefreshVerdicts.model_validate_json(complete(
            "verifier", payload, RefreshVerdicts.model_json_schema()))
        # Convert bounded wire results back to the ordinary immutable contract.
        verdicts = tuple(VerificationResult.model_validate_json(v.model_dump_json())
                         for v in output.verdicts)
    # Scope across the WHOLE batch dominates metadata errors/correction on an earlier item.
    for verdict in verdicts:
        for ref in verdict.evidence:
            context._record(ref.path)
    if len(findings) > 1:
        ids = [v.finding_id for v in verdicts]
        if len(ids) != len(set(ids)) or set(ids) != {f.finding_id for f in findings}:
            raise RefreshError("refresh_incomplete_assessment")
        by_id = {v.finding_id: v for v in verdicts}
        return tuple(by_id[f.finding_id] for f in findings)
    return verdicts  # Single-result ID errors retain their once-only correction policy.


class RefreshError(RepairError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def refresh_findings(candidate, report, outcomes, authority, profiles, adapter, audit, *,
                     dependency_audit=None, test_record=None):
    """Return a current both-role report; explicit independently checked dispositions.

    Each old claim retains its identity and semantics, but a reviewer must supply
    fresh evidence. A separate verifier must agree it remains or no longer holds.
    Omissions, disagreement and unknown verdicts never remove a pending issue.
    """
    entry = {"old_snapshot_id": report.snapshot_id, "snapshot_id": None,
             "paths": list(report.included_paths), "status": "failed",
             "diagnostic_code": None, "issues": [], "requests_attempted": 0,
             "reviewer_assessments": [], "reviewer_reassessments": [],
             "pending_findings": [{"finding_id": o.finding.finding_id,
                 "task_id": o.finding.task_id, "role": o.role} for o in outcomes]}
    previous_rounds = len(audit)
    audit.append(entry)
    try:
        if previous_rounds >= REFRESH_POLICY["max_rounds"]:
            raise RefreshError("refresh_limit_exhausted")
        authority.check(candidate)
        source, inventory = candidate.source_inventory(report.included_paths)
        context = ReviewContext(source, inventory)
        snapshot = inventory.snapshot.snapshot_id
        entry["snapshot_id"] = snapshot
        index = ContextIndex(source, inventory, max_bytes=32768, max_chunks=1000)
        if index.omissions:
            raise RefreshError("refresh_context_incomplete")
        files = [{"evidence": p.evidence.model_dump(mode="json"), "text": p.text}
                 for p in index.packets]
        pending = {(o.finding.task_id, o.finding.finding_id): o for o in outcomes}
        if not pending or len(pending) != len(outcomes):
            raise RefreshError("refresh_invalid_queue")
        tasks, reviewed = [], []
        used_ids = set()
        total = 0

        def complete(role, payload, schema):
            if authority.call_budget_exhausted:
                raise RefreshError("refresh_budget_exhausted")
            phase = authority.start_phase(candidate, report.included_paths, adapter, profiles)
            try:
                text = phase.gateway.complete(ModelRequest(
                    "reviewer" if role != "verifier" else "verifier",
                    profiles.system_for("verifier" if role == "verifier" else "reviewer", role),
                    json.dumps(payload), schema))
                authority.check(candidate)
                return text
            finally:
                entry["requests_attempted"] += phase.transport.calls
                authority.record_phase(phase)

        for role in REFRESH_POLICY["roles"]:
            originals = [t.task for t in report.tasks if t.task.role == role]
            if len(originals) != 1:
                raise RefreshError("refresh_role_coverage_invalid")
            task = originals[0].model_copy(update={"snapshot_id": snapshot})
            context.validate_task(task)
            old = {o.finding.finding_id: o.finding for o in outcomes
                   if o.finding.task_id == task.task_id and o.role == role}
            # Never send stale EvidenceRef metadata through the current-source gateway.
            history = [f.model_dump(mode="json", exclude={"evidence"}) for f in old.values()]
            payload = {"task": task.model_dump(mode="json"), "files": files,
                "refresh": {"historical_findings": history,
                    "instruction": "Review the entire supplied current scope. Explicitly assess "
                    "every historical issue by ID, preserving its meaning: present, resolved or "
                    "unresolved. Historical descriptions are claims, not current evidence. "
                    "Supply exact CURRENT evidence supporting each decisive assessment; do not "
                    "assume earlier patches fixed anything. Report additional distinct defects "
                    "in new_findings, not duplicates of historical issues. Report reviewed_paths. "
                    "No tools are available; use only supplied current files."}}
            if test_record is not None:
                from reviewflow.backend.test_feedback import current_observation

                payload["test_observation"] = current_observation(candidate, test_record)
                payload["refresh"]["instruction"] += (
                    " test_observation is a bounded current-candidate test observation, not "
                    "evidence for a finding or resolution. It contains no test source or expected "
                    "values. Consider compatibility regressions, but derive all claims from "
                    "current supplied source; never invent expectations from opaque test IDs.")
            output = RefreshReview.model_validate_json(complete(
                role, payload, RefreshReview.model_json_schema()))
            ids = [a.finding_id for a in output.assessments]
            if (len(ids) != len(set(ids)) or set(ids) != set(old)
                    or len(output.reviewed_paths) != len(set(output.reviewed_paths))
                    or set(output.reviewed_paths) != set(task.paths)):
                raise RefreshError("refresh_incomplete_assessment")
            entry["reviewer_assessments"].extend({"task_id": task.task_id, "role": role,
                "finding_id": a.finding_id, "status": a.status, "rationale": a.rationale}
                for a in output.assessments)
            # Preflight the whole response before extra reads; unresolved cannot mask bad evidence.
            for item in (*output.assessments, *output.new_findings):
                for ref in item.evidence:
                    context._record(ref.path)
                    if ref.path not in task.paths:
                        raise RefreshError("refresh_incomplete_assessment")
                    context._validate_evidence(ref)
            tasks.append(TaskOutcome(task, "completed", output.reviewed_paths))
            assessments, recovered_ids = [], set()
            for assessment in output.assessments:
                if assessment.status == "unresolved":
                    if dependency_audit is None or not set(candidate.plan.selected) - set(report.included_paths):
                        raise RefreshError("refresh_unresolved")
                    before_calls = authority.issued
                    try:
                        assessment = reassess_reviewer(candidate, report.included_paths, task,
                            old[assessment.finding_id], assessment, authority, profiles, adapter, audit)
                        recovered_ids.add(assessment.finding_id)
                    finally:
                        entry["requests_attempted"] += authority.issued - before_calls
                # New evidence comes from an explicit source review, not hash substitution.
                current = old[assessment.finding_id].model_copy(update={
                    "evidence": assessment.evidence})
                current = Finding.model_validate(current)
                context.validate_finding(task, current)
                assessments.append((current, assessment.status, assessment.rationale))
            assessments.extend((f, "new", None) for f in output.new_findings)
            total += len(assessments)
            if total > REFRESH_POLICY["max_findings_per_round"]:
                raise RefreshError("refresh_findings_exhausted")
            for finding, status, rationale in assessments:
                identity = (task.task_id, finding.finding_id)
                if identity in used_ids or (status == "new" and identity in pending):
                    raise RefreshError("refresh_duplicate_identity")
                used_ids.add(identity)
                context.validate_finding(task, finding)
            batch_size = REFRESH_POLICY["max_verifier_batch_size"]
            for position, (finding, status, rationale) in enumerate(assessments):
                identity = (task.task_id, finding.finding_id)
                if position % batch_size == 0:
                    verdicts = _verify_batch(complete, context, task, files,
                        [item[0] for item in assessments[position:position + batch_size]])
                issue = {"finding_id": finding.finding_id, "task_id": task.task_id,
                         "role": role, "status": "unresolved", "assessment": status}
                entry["issues"].append(issue)
                # Do not send the reviewer's desired verdict; this is independent judgment.
                verification_payload = {
                    "task": task.model_dump(mode="json"), "files": files,
                    "finding": finding.model_dump(mode="json"),
                    "refresh_verification": "Independently check whether this issue exists in "
                    "the supplied current code. Accept only if it exists, reject if it does not, "
                    "or return unresolved. Do not assume an earlier patch was correct.",
                }
                for attempt in range(2):
                    verdict = (verdicts[position % batch_size] if not attempt else
                        VerificationResult.model_validate_json(complete("verifier",
                            verification_payload, VerificationResult.model_json_schema())))
                    # Scope violations dominate ID/hash/range errors across the whole result.
                    for ref in verdict.evidence:
                        context._record(ref.path)
                    try:
                        context.validate_verification(task, finding, verdict)
                    except EvidenceValidationError as exc:
                        if attempt or exc.code not in RECOVERABLE_EVIDENCE_CODES:
                            raise
                        if any(prior_round.get("snapshot_id") == snapshot and
                               set(prior_round["paths"]) == set(report.included_paths) and
                               any(old_issue["task_id"] == task.task_id and
                                   old_issue["finding_id"] == finding.finding_id and
                                   old_issue.get("verification_correction")
                                   for old_issue in prior_round["issues"])
                               for prior_round in audit[:-1]):
                            raise RefreshError("refresh_verification_correction_limit")
                        issue["verification_correction"] = {
                            "original_error": exc.code, "attempt": 1, "status": "failed"}
                        verification_payload["evidence_correction"] = {
                            "reason": exc.code, "attempt": 1,
                            "instruction": "Independently verify again using the exact current "
                            "finding ID, snapshot and supplied file hashes and valid line ranges. "
                            "Do not assume acceptance or rejection. No further correction is allowed.",
                        }
                    else:
                        if attempt:
                            issue["verification_correction"]["status"] = "recovered"
                        break
                verified = FindingOutcome(finding, role, verdict)
                prior = pending.get(identity)
                if (dependency_audit is not None
                        and set(candidate.plan.selected) - set(report.included_paths)
                        and (verdict.verdict == "unresolved" or (prior and prior.verification_scope)
                             or finding.finding_id in recovered_ids)):
                    before_calls = authority.issued
                    try:
                        verified = verify_dependencies(candidate, report.included_paths, task,
                            verified, authority, profiles, adapter, dependency_audit, stage="finding_refresh")
                        verdict = verified.verification
                    finally:
                        entry["requests_attempted"] += authority.issued - before_calls
                issue["verification"] = verdict.model_dump(mode="json")
                issue["verification_scope"] = list(verified.verification_scope)
                if verdict.verdict == "unresolved":
                    raise RefreshError("refresh_unresolved")
                expected = "rejected" if status == "resolved" else "accepted"
                if status != "new" and verdict.verdict != expected:
                    raise RefreshError("refresh_verdict_disagreement")
                issue.update(status=("resolved" if verdict.verdict == "rejected" else "present"),
                             rationale=rationale)
                reviewed.append(verified)
        if not set(pending) <= used_ids:
            raise RefreshError("refresh_incomplete_assessment")
        refreshed = replace(report, snapshot_id=snapshot, tasks=tuple(tasks),
            findings=tuple(reviewed), errors=(), calls=entry["requests_attempted"],
            limits=RunLimits(max_calls=18 + 8 * DEPENDENCY_POLICY["max_requests_per_attempt"],
                             max_tasks=2, max_findings=8, max_context_bytes=32768),
            reported_reviewed_paths=report.included_paths)
        validate_scoped_review(candidate, refreshed)
        entry.update(status="complete", report=json.loads(render_review(refreshed, "json")))
        return refreshed
    except Exception as exc:
        entry["diagnostic_code"] = (
            exc.code if isinstance(exc, (RefreshError, ReassessmentError, DependencyError,
                                        EvidenceValidationError)) else
            "refresh_invalid_output" if isinstance(exc, ValidationError) else
            "refresh_authorization_stopped" if isinstance(exc, Denied) else
            "refresh_transport_stopped" if isinstance(exc, TransportError) else
            "refresh_stopped")
        raise

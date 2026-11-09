"""Bounded real repair orchestration; all target execution stays in Docker.

The CLI supplies the real Responses adapter. Injected adapters/runners in unit
tests are trusted fixtures and never constitute user-target acceptance.
"""

import difflib
import json
from dataclasses import asdict, replace

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.dependency_verification import resolve_dependencies, verify_dependencies
from reviewflow.backend.finding_refresh import REFRESH_POLICY, refresh_findings
from reviewflow.backend.live_patch import propose_live_patch
from reviewflow.backend.repair import PartialApplicationError, RepairError
from reviewflow.backend.repair_authority import ApplicationAuthority, validate_scoped_review
from reviewflow.backend.repair_candidate import RepairCandidate
from reviewflow.backend.repair_diagnostics import compare_runs
from reviewflow.backend.reviewer_recovery import recover_reviewers
from reviewflow.backend.transport import TransportError
from reviewflow.backend.verification_recovery import recover_verification
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import EvidenceRef, VerificationResult
from reviewflow.review.interactive import run_interactive_review
from reviewflow.review.report import render_review
from reviewflow.review.workflow_models import ModelRequest, RunLimits


def repair_batches(plan):
    files = dict(plan.baseline)
    batches, current, size = [], [], 0
    for path in plan.selected:
        length = len(files[path])
        if length > 32768:
            raise RepairError("Selected file exceeds live repair context limit")
        if current and size + length > 32768:
            batches.append(tuple(current))
            current, size = [], 0
        current.append(path)
        size += length
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _review(candidate, paths, authority, profiles, adapter):
    phase = authority.start_phase(candidate, paths, adapter, profiles)
    try:
        result = run_interactive_review(phase.source, phase.inventory, phase.gateway,
            mode="multi", limits=RunLimits(max_calls=8, max_tasks=2, max_findings=8,
                                           max_context_bytes=32768),
            live_policy=True, profiles=profiles)
        return replace(result.report, execution="live_development",
                       semantic_review_performed=any(t.status == "completed" for t in result.report.tasks))
    finally:
        authority.record_phase(phase)


def _current_finding(candidate, report, outcome):
    source, inventory = candidate.source_inventory(report.included_paths)
    context = ReviewContext(source, inventory)
    records = {r.path: r for r in inventory.snapshot.records if r.status == "included"}
    finding = outcome.finding
    if any(records[ref.path].sha256 != ref.content_sha256 for ref in finding.evidence):
        # Never silently re-anchor a finding to changed source lines.
        raise RepairError("Finding evidence changed; a new review is required")
    snapshot = inventory.snapshot.snapshot_id
    task = next(t.task for t in report.tasks if t.task.task_id == finding.task_id)
    task = task.model_copy(update={"snapshot_id": snapshot})
    finding = finding.model_copy(update={"evidence": tuple(
        ref.model_copy(update={"snapshot_id": snapshot}) for ref in finding.evidence)})
    context.validate_finding(task, finding)
    return context, task, finding


def _reverify(candidate, report, context, task, finding, authority, profiles, adapter):
    """New independent verification when only the containing snapshot changed."""
    packets, total = [], 0
    for record in context._records.values():
        text = context.read_file(record.path)
        total += len(text.encode("utf-8"))
        if total > 32768:
            raise RepairError("Reverification context exceeds budget")
        count = text.count("\n") + (not text.endswith("\n"))
        for start in range(1, count + 1, 200):
            ref = EvidenceRef(snapshot_id=task.snapshot_id, path=record.path,
                              content_sha256=record.sha256, start_line=start,
                              end_line=min(count, start + 199))
            packets.append({"evidence": ref.model_dump(mode="json"),
                            "text": context.read_lines(record.path, start, ref.end_line)})
    phase = authority.start_phase(candidate, report.included_paths, adapter, profiles)
    try:
        output = phase.gateway.complete(ModelRequest("verifier", profiles.system_for("verifier"),
            json.dumps({"task": task.model_dump(mode="json"), "finding": finding.model_dump(mode="json"),
                        "files": packets}), VerificationResult.model_json_schema()))
        verdict = VerificationResult.model_validate_json(output)
        context.validate_verification(task, finding, verdict)
        authority.check(candidate)
        return verdict
    finally:
        authority.record_phase(phase)


def run_repair_session(plan, config, authority, profiles, runner, adapter, *, seed=None):
    document = {
        "report_type": "live_development_repair", "status": "blocked",
        "tests_only": authority.preview.get("tests_only", False),
        "candidate_path": str(plan.destination), "original_root": str(plan.root),
        "initial_reviews": [], "final_reviews": [], "test_runs": [], "patch_attempts": [],
        "verification_recoveries": [], "recovered_reviews": [], "finding_refreshes": [],
        "dependency_verifications": [], "dependency_reviews": [], "reviewer_recoveries": [],
        "patch_groups": [], "application": None, "commit_ready": False,
        "test_counts_retained": False, "uncommitted_changes_tested": False,
        "dynamic_reproduction_proven": False,
        "review_scope": list(plan.selected), "test_scope": "all discoverable committed HEAD tests",
    }
    stage = "authorization"
    candidate = None
    try:
        authority.check()
        if fingerprint(seed.preview() if seed is not None else None) != fingerprint(authority.preview.get("seed")):
            raise RepairError("Seed differs from fresh approval")
        if seed is not None:
            seed.check(plan)
            document["seed"] = seed.preview()
        if config.model_dump(mode="json") != authority.preview["config"]:
            raise RepairError("Repair configuration changed")
        if profiles.manifest() != authority.preview["agent_profiles"]:
            raise RepairError("Repair profiles changed")
        batches = () if document["tests_only"] else repair_batches(plan)
        candidate = RepairCandidate.create(plan, authority.gate, authority.authorize_candidate(plan))

        def test(test_stage, *, finding_id=None, patch_sha256=None):
            grant = authority.authorize_tests(candidate, runner)
            result = runner.run(candidate.files(), "repository", authority.gate, grant)
            record = asdict(result)
            record.update(stage=test_stage, sequence=len(document["test_runs"]) + 1,
                          finding_id=finding_id, patch_sha256=patch_sha256)
            previous = document["test_runs"][-1] if document["test_runs"] else None
            record["comparison"] = compare_runs(previous, record)
            document["test_runs"].append(record)
            return result

        def reviewer_recovery(report, review_stage):
            nonlocal stage
            if any(t.status == "failed" for t in report.tasks):
                stage = review_stage + "_reviewer_recovery"
            recovered = recover_reviewers(candidate, report, authority, profiles, adapter,
                document["reviewer_recoveries"], stage=review_stage)
            if recovered is not report:
                document["recovered_reviews"].append({"stage": review_stage,
                    "report": json.loads(render_review(recovered, "json"))})
            stage = review_stage
            return recovered

        def dependencies(report, review_stage):
            nonlocal stage
            if any(o.verification and o.verification.verdict == "unresolved" for o in report.findings):
                stage = review_stage + "_dependencies"
            effective = resolve_dependencies(candidate, report, authority, profiles, adapter,
                document["dependency_verifications"], stage=review_stage)
            if effective is not report:
                document["dependency_reviews"].append({"stage": review_stage,
                    "report": json.loads(render_review(effective, "json"))})
            stage = review_stage
            return effective

        stage = "baseline_tests"
        baseline = test("baseline")
        if baseline.status not in {"passed", "failed"}:
            raise RepairError("Baseline infrastructure unavailable")
        if document["tests_only"]:
            document["status"] = "tests_passed" if baseline.status == "passed" else "tests_failed"
            return document
        if seed is not None:
            stage = "seed_application"
            seed.apply(candidate, authority)
            stage = "seed_tests"
            seeded = test("seed")
            if seeded.status not in {"passed", "failed"}:
                raise RepairError("Seed test infrastructure unavailable")
        initial = []
        stage = "initial_review"
        for paths in batches:
            report = _review(candidate, paths, authority, profiles, adapter)
            document["initial_reviews"].append(json.loads(render_review(report, "json")))
            report = reviewer_recovery(report, "initial_review")
            if report.errors:
                stage = "initial_review_recovery"
                recovered = recover_verification(candidate, report, authority, profiles, adapter,
                    document["verification_recoveries"], stage="initial_review")
                if recovered is not report:
                    document["recovered_reviews"].append({"stage": "initial_review",
                        "report": json.loads(render_review(recovered, "json"))})
                report = recovered
                stage = "initial_review"
            report = dependencies(report, "initial_review")
            validate_scoped_review(candidate, report)
            initial.append(report)
        selected = [(report, outcome) for report in initial for outcome in report.findings
                    if outcome.verification.verdict == "accepted"]
        if not selected and seed is None:
            if baseline.status != "passed":
                raise RepairError("Tests fail without accepted repair findings")
            document["status"] = "no_changes"
            return document
        stage = "final_review_budget"
        authority.reserve_final_review(candidate)
        pending = list(selected)
        if authority.preview["patch_scheduling"]["mode"] == "grouped":
            from reviewflow.backend.grouped_repair import run_groups

            def set_stage(value):
                nonlocal stage
                stage = value

            run_groups(candidate, selected, authority, profiles, adapter, baseline,
                       document, test, set_stage)
            pending = []
        while pending:
            report, outcome = pending.pop(0)
            stage = "current_finding_provenance"
            context, task, finding = _current_finding(candidate, report, outcome)
            if task.snapshot_id != report.snapshot_id:
                stage = "current_finding_verification"
                verdict = outcome.verification if outcome.verification_scope else _reverify(
                    candidate, report, context, task, finding, authority, profiles, adapter)
                if outcome.verification_scope or (verdict.verdict == "unresolved"
                        and set(plan.selected) - set(report.included_paths)):
                    current = verify_dependencies(candidate, report.included_paths, task,
                        replace(outcome, finding=finding, verification=verdict), authority, profiles, adapter,
                        document["dependency_verifications"], stage="current_finding_verification")
                    verdict = current.verification
                    if verdict.verdict == "rejected":
                        continue  # Explicit independent current-source rejection, not omission.
                if verdict.verdict != "accepted":
                    raise RepairError("Current finding not independently accepted")
            for attempt in (1, 2):
                if len(document["patch_attempts"]) >= REFRESH_POLICY["max_total_patch_attempts"]:
                    stage = "patch_budget"
                    raise RepairError("Total patch attempt limit exhausted")
                entry = {"finding_id": finding.finding_id, "snapshot_id": task.snapshot_id,
                         "attempt": attempt, "status": "failed"}
                document["patch_attempts"].append(entry)
                stage = "patch_generation"
                phase = authority.start_phase(candidate, report.included_paths, adapter, profiles)
                try:
                    patch = propose_live_patch(context, task, finding, phase.gateway, profiles,
                                               feedback=attempt > 1)
                except TransportError:
                    raise
                except RepairError:
                    if attempt == 2:
                        raise
                    continue
                finally:
                    authority.record_phase(phase)
                stage = "candidate_patch_application"
                candidate.apply(patch, context, task, finding)
                entry["patch_sha256"] = patch.sha256
                stage = "candidate_tests"
                result = test("candidate", finding_id=finding.finding_id, patch_sha256=patch.sha256)
                if result.status not in {"passed", "failed"}:
                    raise RepairError("Candidate test infrastructure blocked")
                if baseline.status == "passed" and result.status != "passed":
                    candidate.restore_attempt()
                    context, task, finding = _current_finding(candidate, report, outcome)
                    if attempt == 2:
                        raise RepairError("Patch introduced regression")
                    continue
                # With a failing baseline, assertion failures may be other unfixed bugs.
                # Retain provisionally; final all-tests pass is still mandatory.
                entry["status"] = "provisional"
                break
            changed_paths = {r.path for r in patch.proposal.replacements}
            # Refresh whole affected review scopes, not titles or overlapping lines.
            # Unchanged scopes retain the existing independent snapshot recheck.
            affected = []
            for queued_report, _ in pending:
                if (changed_paths.intersection(queued_report.included_paths)
                        and not any(queued_report is r for r in affected)):
                    affected.append(queued_report)
            for old_report in affected:
                stage = "finding_refresh"
                old_outcomes = [o for r, o in pending if r is old_report]
                try:
                    refreshed = refresh_findings(candidate, old_report, old_outcomes, authority,
                        profiles, adapter, document["finding_refreshes"],
                        dependency_audit=document["dependency_verifications"])
                finally:
                    document["finding_refreshes"][-1]["patch_sha256"] = patch.sha256
                replacement = [(refreshed, o) for o in refreshed.findings
                               if o.verification.verdict == "accepted"]
                insertion = next(i for i, (r, _) in enumerate(pending) if r is old_report)
                pending = [(r, o) for r, o in pending if r is not old_report]
                pending[insertion:insertion] = replacement
        stage = "final_tests"
        result = test("final")
        if result.status != "passed":
            raise RepairError("Final repository tests failed")
        stage = "final_review"
        authority.begin_final_review(candidate)
        reviews = []
        for paths in batches:
            report = _review(candidate, paths, authority, profiles, adapter)
            document["final_reviews"].append(json.loads(render_review(report, "json")))
            report = reviewer_recovery(report, "final_review")
            if report.errors:
                stage = "final_review_recovery"
                recovered = recover_verification(candidate, report, authority, profiles, adapter,
                    document["verification_recoveries"], stage="final_review")
                if recovered is not report:
                    document["recovered_reviews"].append({"stage": "final_review",
                        "report": json.loads(render_review(recovered, "json"))})
                report = recovered
                stage = "final_review"
            report = dependencies(report, "final_review")
            validate_scoped_review(candidate, report, final=True)
            reviews.append(report)
        stage = "writeback"
        authority.check(candidate)
        application = ApplicationAuthority(runner)
        permit = application.finish(candidate, baseline, result, reviews)
        document["application"] = candidate.writeback(application, permit)
        document["status"] = "applied"
    except PartialApplicationError:
        document["status"] = "partial_application"
        document["failure_stage"] = stage
    except Exception:
        # Never serialize arbitrary backend, source or filesystem exception text.
        document["failure_stage"] = stage
    finally:
        document["accounting"] = authority.accounting()
        if seed is not None:
            document["cumulative_reported_accounting"] = {
                "historical_accounting": "report_supplied", "cost_usd": None,
                "requests_attempted": seed.reported_prior_calls + document["accounting"]["requests_attempted"],
                "patch_attempts": seed.reported_prior_patch_attempts + len(document["patch_attempts"])}
        document["preserve_candidate"] = plan.destination.exists()
        document["candidate_diff"] = None
        if candidate is not None:
            try:
                files = candidate.files()
                lines = []
                for path, before in plan.baseline:
                    if before != files[path]:
                        lines.extend(difflib.unified_diff(before.decode("utf-8").splitlines(keepends=True),
                            files[path].decode("utf-8").splitlines(keepends=True),
                            fromfile="a/" + path, tofile="b/" + path))
                document["candidate_diff"] = "".join(
                    line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
            except Exception:
                pass  # Unknown externally changed bytes are never exported as our diff.
    return document

"""Bounded group scheduling; acceptance remains the common final controller gate."""
from dataclasses import replace

from reviewflow.backend.dependency_verification import verify_dependencies
from reviewflow.backend.finding_refresh import REFRESH_POLICY, refresh_findings
from reviewflow.backend.grouped_patch import GROUP_POLICY, propose_group
from reviewflow.backend.repair import RepairError
from reviewflow.backend.test_feedback import (
    current_observation,
    new_failing_definitions,
    observation,
)
from reviewflow.backend.transport import TransportError


def run_groups(candidate, selected, authority, profiles, adapter, baseline, document, test, stage):
    # Imported here to share the serial controller's provenance checks without an import cycle.
    from reviewflow.backend.live_repair import _current_finding, _reverify

    pending = list(selected)
    while pending:
        report = pending[0][0]
        stage("current_finding_provenance")
        members, chosen, retired, paths = [], [], [], set()
        remaining = REFRESH_POLICY["max_total_patch_attempts"] - len(document["patch_attempts"])
        if not remaining:
            stage("patch_budget")
            raise RepairError("Total patch attempt limit exhausted")
        for queued_report, outcome in pending:
            if queued_report is not report:
                continue
            context, task, finding = _current_finding(candidate, report, outcome)
            if task.snapshot_id != report.snapshot_id:
                stage("current_finding_verification")
                verdict = outcome.verification if outcome.verification_scope else _reverify(
                    candidate, report, context, task, finding, authority, profiles, adapter)
                if outcome.verification_scope or (verdict.verdict == "unresolved"
                        and set(candidate.plan.selected) - set(report.included_paths)):
                    current = verify_dependencies(candidate, report.included_paths, task,
                        replace(outcome, finding=finding, verification=verdict), authority, profiles,
                        adapter, document["dependency_verifications"], stage="current_finding_verification")
                    verdict = current.verification
                document.setdefault("current_finding_verifications", []).append({
                    "task_id": task.task_id, "finding_id": finding.finding_id,
                    "snapshot_id": task.snapshot_id, "verification": verdict.model_dump(mode="json")})
                if verdict.verdict == "rejected":
                    retired.append(outcome)
                    continue  # Explicit current-source rejection, never a generated patch claim.
                if verdict.verdict != "accepted":
                    raise RepairError("Current group member not independently accepted")
            union = paths | {ref.path for ref in finding.evidence}
            if sum(len(context.read_file(p).encode("utf-8")) for p in union) > 32768:
                if members:
                    break
                raise RepairError("Group member context exceeds budget")
            paths = union
            members.append((task, finding))
            chosen.append(outcome)
            if len(members) == min(GROUP_POLICY["max_findings"], remaining):
                break
        pending = [(r, o) for r, o in pending
                   if not (r is report and any(o is old for old in retired))]
        if not members:
            continue
        digest = candidate.current_digest()
        tested = next(r for r in reversed(document["test_runs"]) if r["content_digest"] == digest)
        feedback = {"current": current_observation(candidate, tested), "rejected_attempt": None}
        for attempt in range(1, GROUP_POLICY["max_attempts_per_group"] + 1):
            if len(document["patch_attempts"]) + len(members) > REFRESH_POLICY["max_total_patch_attempts"]:
                stage("patch_budget")
                raise RepairError("Total patch attempt limit exhausted")
            entries = [{"task_id": t.task_id, "finding_id": f.finding_id,
                        "snapshot_id": t.snapshot_id, "attempt": attempt, "status": "failed"}
                       for t, f in members]
            group = {"sequence": len(document["patch_groups"]) + 1, "attempt": attempt,
                     "members": [{"task_id": t.task_id, "finding_id": f.finding_id} for t, f in members],
                     "snapshot_id": members[0][0].snapshot_id, "status": "failed"}
            document["patch_attempts"].extend(entries)
            document["patch_groups"].append(group)
            stage("patch_generation")
            phase = authority.start_phase(candidate, report.included_paths, adapter, profiles)
            try:
                current_observation(candidate, tested)
                patch = propose_group(context, members, phase.gateway, profiles, feedback=attempt > 1,
                                      test_feedback=feedback)
            except TransportError:
                raise
            except RepairError:
                if attempt == GROUP_POLICY["max_attempts_per_group"]:
                    raise
                continue
            finally:
                authority.record_phase(phase)
            stage("candidate_patch_application")
            candidate.apply_group(patch, context, members)
            group["patch_sha256"] = patch.sha256
            for entry in entries:
                entry["patch_sha256"] = patch.sha256
            stage("candidate_tests")
            result = test("candidate", patch_sha256=patch.sha256)
            if result.status not in {"passed", "failed"}:
                raise RepairError("Candidate test infrastructure blocked")
            tested_after = document["test_runs"][-1]
            current_observation(candidate, tested_after)
            added = new_failing_definitions(tested, tested_after)
            group["new_failing_definitions"] = added
            if (tested["status"] == "passed" and result.status != "passed") or added:
                feedback["rejected_attempt"] = {
                    "patch_sha256": patch.sha256,
                    "proposal": patch.proposal.model_dump(mode="json", exclude={"explanation"}),
                    "observation": observation(tested_after), "new_failing_definitions": added}
                candidate.restore_attempt()
                # Owned restoration changes file identities; rebuild checked readers, not hashes.
                current = [_current_finding(candidate, report, o) for o in chosen]
                context = current[0][0]
                members = [(t, f) for _, t, f in current]
                group["status"] = "reverted"
                for entry in entries:
                    entry["status"] = "reverted"
                if attempt == GROUP_POLICY["max_attempts_per_group"]:
                    raise RepairError("Group introduced regression")
                continue
            group["status"] = "provisional"
            for entry in entries:
                entry["status"] = "provisional"
            break
        changed = {r.path for r in patch.replacements}
        affected = []
        for queued_report, outcome in pending:
            scope = set(queued_report.included_paths) | set(outcome.verification_scope)
            if (queued_report is report or changed.intersection(scope)) and not any(
                    queued_report is old for old in affected):
                affected.append(queued_report)
        # Keep ALL group members in pending until an independent refreshed verdict exists.
        # Neither successful generation nor a passing candidate test resolves a claim.
        for old_report in affected:
            stage("finding_refresh")
            old_outcomes = [o for r, o in pending if r is old_report]
            before = len(document["finding_refreshes"])
            try:
                refreshed = refresh_findings(candidate, old_report, old_outcomes, authority,
                    profiles, adapter, document["finding_refreshes"],
                    dependency_audit=document["dependency_verifications"],
                    test_record=document["test_runs"][-1])
            finally:
                if len(document["finding_refreshes"]) > before:
                    document["finding_refreshes"][-1]["patch_sha256"] = patch.sha256
            pending = [(r, o) for r, o in pending if r is not old_report]
            pending.extend((refreshed, o) for o in refreshed.findings
                           if o.verification.verdict == "accepted")

"""Synthetic cross-batch controller tests; no paid requests or target execution."""

import json

import pytest
from conftest import git
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.live_repair import repair_batches, run_repair_session
from reviewflow.backend.transport import Reply, Usage


class DependencyAdapter(RepairFixtureAdapter):
    def __init__(self, model, behavior="accepted"):
        super().__init__(model)
        self.dependency_behavior = behavior

    def __call__(self, wire, **kwargs):
        request = json.loads(wire)
        data = request["payload"]
        if request["stage"] != "verifier":
            self.behavior = ("final_finding" if self.dependency_behavior.startswith("final_")
                and request["stage"] == "reviewer" and "a.py" in data["task"]["paths"] else "success")
            return super().__call__(wire, **kwargs)
        self.sent.append(wire)
        result = {"finding_id": data["finding"]["finding_id"],
                  "verdict": "unresolved", "rationale": "Need dependency implementation",
                  "evidence": data["finding"]["evidence"]}
        requests = []
        if data.get("dependency_verification"):
            supplied = [p for p in data["files"] if p["evidence"]["path"] == "src/b.py"]
            if not supplied:
                path = "private.py" if self.dependency_behavior == "outside" else "src/b.py"
                requests = [{"kind": "read", "path": path, "start_line": 1,
                             "end_line": 2 if self.dependency_behavior == "context" else 1}]
                result = None
            else:
                result["verdict"] = self.dependency_behavior if self.dependency_behavior in {
                    "accepted", "rejected", "unresolved"} else "accepted"
                result["evidence"] = [supplied[0]["evidence"]]
                if self.dependency_behavior.startswith("final_") and self.patch_calls:
                    result["verdict"] = self.dependency_behavior.removeprefix("final_")
                if self.dependency_behavior == "unsupplied":
                    result["evidence"] = [{**supplied[0]["evidence"], "start_line": 2, "end_line": 2}]
        return Reply(json.dumps({"result": result, "requests": requests}), Usage(10, 5))


def setup_cross_batch(git_repo, tmp_path, *, cap=48, dependency_source=None):
    # b.py cannot fit with a.py in the existing 32768-byte batch. It is never
    # executed, and its first line is enough for this synthetic verifier.
    (git_repo / "src/b.py").write_text(dependency_source if dependency_source is not None
        else "#" + "x" * 16382 + "\n" + "#" + "y" * 16381 + "\n")
    git(git_repo, "add", "src/b.py")
    git(git_repo, "commit", "-qm", "Synthetic cross-batch dependency")
    values = setup_budget(git_repo, tmp_path, cap=cap, paths=("a.py", "src/b.py"))
    assert repair_batches(values[0]) == (("a.py",), ("src/b.py",))
    return values


def test_approved_later_batch_dependency_is_verified_without_claiming_review_coverage(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    adapter = DependencyAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert (git_repo / "a.py").read_text() == "fixed\n"
    assert len(result["initial_reviews"]) == len(result["final_reviews"]) == 2
    original = result["initial_reviews"][0]
    assert original["findings"][0]["verification"]["verdict"] == "unresolved"
    assert original["included_paths"] == ["a.py"]
    attempt = result["dependency_verifications"][0]
    assert attempt["status"] == "complete"
    assert attempt["requests_attempted"] == 2
    assert attempt["verification"]["verdict"] == "accepted"
    effective = result["dependency_reviews"][0]["report"]
    assert effective["included_paths"] == effective["reported_reviewed_paths"] == ["a.py"]
    assert effective["findings"][0]["verification_scope"] == ["a.py", "src/b.py"]


@pytest.mark.parametrize("behavior", ["outside", "unsupplied", "unresolved"])
def test_dependency_unknown_or_invalid_never_allows_patch(git_repo, tmp_path, behavior):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    adapter = DependencyAdapter(config.model, behavior)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not result["patch_attempts"] and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"
    assert result["dependency_verifications"][0]["status"] == "failed"


def test_dependency_rejection_is_not_a_patch_or_omitted_review(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                DependencyAdapter(config.model, "rejected"))
    assert result["status"] == "no_changes", result
    assert len(result["initial_reviews"]) == 2
    assert not result["patch_attempts"] and result["application"] is None
    assert result["dependency_verifications"][0]["verification"]["verdict"] == "rejected"


def test_dependency_reads_share_parent_budget(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path, cap=6)
    adapter = DependencyAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert result["accounting"]["requests_attempted"] == 6
    assert not result["patch_attempts"]
    assert result["dependency_verifications"][0]["diagnostic_code"] == "dependency_budget_exhausted"


@pytest.mark.parametrize("verdict,status", [("rejected", "applied"), ("accepted", "blocked"), ("unresolved", "blocked")])
def test_final_dependency_verdict_still_controls_writeback(git_repo, tmp_path, verdict, status):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                DependencyAdapter(config.model, "final_" + verdict))
    assert result["status"] == status, result
    assert result["dependency_verifications"][-1]["stage"] == "final_review"
    assert (git_repo / "a.py").read_text() == ("fixed\n" if status == "applied" else "old\n")


def test_dependency_context_limit_applies_across_all_reads(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                DependencyAdapter(config.model, "context"))
    assert result["status"] == "blocked" and not result["patch_attempts"]
    attempt = result["dependency_verifications"][0]
    assert attempt["diagnostic_code"] == "dependency_context_exhausted"
    assert attempt["requests_attempted"] == 1


def test_dependency_policy_cannot_be_expanded_with_a_recomputed_digest(git_repo, tmp_path):
    from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
    from reviewflow.backend.repair_budget import RepairRunAuthority

    _, _, _, _, preview, _ = setup_cross_batch(git_repo, tmp_path)
    preview["dependency_verification"]["max_requests_per_attempt"] = 8
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


@pytest.mark.parametrize("dependency_verdict", ["rejected", "unresolved"])
def test_refresh_verifier_can_read_dependency_without_skipping_reassessment(git_repo, tmp_path, dependency_verdict):
    from test_finding_refresh import RefreshAdapter

    class RefreshDependency(RefreshAdapter):
        def __init__(self, model):
            super().__init__(model)
            self.reader = DependencyAdapter(model, dependency_verdict)

        def __call__(self, wire, **kwargs):
            request = json.loads(wire)
            if request["payload"].get("dependency_verification"):
                return self.reader(wire, **kwargs)
            reply = super().__call__(wire, **kwargs)
            if request["payload"].get("refresh_verification"):
                verdict = json.loads(reply.text)
                verdict["verdict"] = "unresolved"
                return Reply(json.dumps(verdict), reply.usage)
            return reply

    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner, RefreshDependency(config.model))
    if dependency_verdict == "unresolved":
        assert result["status"] == "blocked" and result["application"] is None
        assert result["finding_refreshes"][0]["diagnostic_code"] == "dependency_unresolved"
        assert (git_repo / "a.py").read_text() == "old\n"
        return
    assert result["status"] == "applied", result
    assert len(result["patch_attempts"]) == 1
    assert result["finding_refreshes"][0]["issues"][0]["status"] == "resolved"
    assert result["dependency_verifications"][0]["stage"] == "finding_refresh"


@pytest.mark.parametrize("scheduling", ["serial", "grouped"])
@pytest.mark.parametrize("initial_verdict", ["unresolved", "accepted"])
def test_dependency_change_requires_fresh_verification_before_next_patch(git_repo, tmp_path, initial_verdict, scheduling):
    class TwoBatch(RepairFixtureAdapter):
        def __call__(self, wire, **kwargs):
            request = json.loads(wire)
            data, stage = request["payload"], request["stage"]
            if data.get("group_patch"):
                self.sent.append(wire)
                self.patch_calls += 1
                patches = []
                for member in data["members"]:
                    ref = member["finding"]["evidence"][0]
                    patches.append({"task_id": member["task"]["task_id"],
                        "finding_id": member["finding"]["finding_id"], "edits": [{
                            "path": ref["path"], "before_sha256": ref["content_sha256"],
                            "old_text": "old\n", "new_text": "fixed\n"}]})
                return Reply(json.dumps({"snapshot_id": data["members"][0]["task"]["snapshot_id"],
                    "patches": patches, "explanation": "Synthetic dependency repair"}), Usage(10, 5))
            if stage == "reviewer" and "refresh" in data and data["task"]["paths"] == ["src/b.py"]:
                self.sent.append(wire)
                # Reanchor to the relevant first line, not the whole near-limit padding file.
                ref = {**data["files"][0]["evidence"], "start_line": 1, "end_line": 1}
                return Reply(json.dumps({"assessments": [{"finding_id": f["finding_id"],
                    "status": "resolved", "evidence": [ref], "rationale": "Dependency repaired"}
                    for f in data["refresh"]["historical_findings"]], "new_findings": [],
                    "reviewed_paths": data["task"]["paths"]}), Usage(10, 5))
            if stage == "reviewer" and "refresh" not in data and data["task"]["paths"] == ["src/b.py"]:
                self.sent.append(wire)
                findings = []
                if data["task"]["role"] == "correctness" and not self.patch_calls:
                    ref = {**data["files"][0]["evidence"], "start_line": 1, "end_line": 1}
                    findings = [{"finding_id": "b-dependent", "task_id": data["task"]["task_id"],
                        "title": "Synthetic dependency", "root_cause": "Depends on a.py old value",
                        "trigger": "Fixture read", "impact": "Wrong fixture value", "severity": "high",
                        "evidence": [ref]}]
                return Reply(json.dumps({"result": {"findings": findings,
                    "reviewed_paths": data["task"]["paths"]}, "requests": []}), Usage(10, 5))
            if stage == "verifier" and data["finding"]["finding_id"] == "b-dependent":
                self.sent.append(wire)
                result = {"finding_id": "b-dependent", "verdict": "unresolved" if self.patch_calls else initial_verdict,
                          "rationale": "Check a.py", "evidence": data["finding"]["evidence"]}
                requests = []
                if data.get("dependency_verification"):
                    supplied = [p for p in data["files"] if p["evidence"]["path"] == "a.py"]
                    if supplied:
                        result.update(verdict="rejected" if supplied[0]["text"] == "fixed\n" else "accepted",
                                      evidence=[supplied[0]["evidence"]])
                    else:
                        result = None
                        requests = [{"kind": "read", "path": "a.py", "start_line": 1, "end_line": 1}]
                value = result if request["response_schema"].get("title") == "VerificationResult" else {
                    "result": result, "requests": requests}
                return Reply(json.dumps(value), Usage(10, 5))
            return super().__call__(wire, **kwargs)

    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    if scheduling == "grouped":
        from reviewflow.backend.approval import ApprovalGate
        from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview

        preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", 48,
                                      patch_scheduling="grouped")
        authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    before = (git_repo / "src/b.py").read_bytes()
    result = run_repair_session(plan, config, authority, profiles, runner, TwoBatch(config.model))
    assert result["status"] == "applied", json.dumps(result)
    assert len(result["patch_attempts"]) == 1
    assert (git_repo / "src/b.py").read_bytes() == before
    checks = result["dependency_verifications"]
    assert [e["verification"]["verdict"] for e in checks] == (
        ["accepted", "rejected"] if initial_verdict == "unresolved" else ["rejected"])
    if initial_verdict == "unresolved":
        assert checks[0]["snapshot_id"] != checks[1]["snapshot_id"]


@pytest.mark.parametrize("mode,code,calls", [
    ("repeat", "dependency_read_limit", 3),
    ("guess", "dependency_not_read", 1),
    ("bad_hash", "evidence_hash_mismatch", 2),
    ("bad_snapshot", "evidence_snapshot_mismatch", 2),
    ("unknown_usage", "dependency_transport_stopped", 1),
    ("mutation", "dependency_stopped", 1),
])
def test_dependency_attempt_boundaries_fail_closed(git_repo, tmp_path, mode, code, calls):
    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)

    class Invalid(DependencyAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            payload = json.loads(wire)["payload"]
            if not payload.get("dependency_verification"):
                return reply
            if mode == "mutation":
                (plan.destination / "a.py").write_text("external mutation\n")
            if mode == "unknown_usage":
                return Reply("{}", None)
            data = json.loads(reply.text)
            if mode == "repeat":
                data = {"result": None, "requests": [{"kind": "read", "path": "src/b.py",
                                                       "start_line": 1, "end_line": 1}]}
            if mode == "guess":
                data = {"result": {"finding_id": payload["finding"]["finding_id"], "verdict": "accepted",
                    "rationale": "No dependency read", "evidence": payload["finding"]["evidence"]}, "requests": []}
            if data["result"] and mode in {"bad_hash", "bad_snapshot"}:
                data["result"]["evidence"][0]["content_sha256" if mode == "bad_hash" else "snapshot_id"] = "0" * 64
            return Reply(json.dumps(data), reply.usage)

    result = run_repair_session(plan, config, authority, profiles, runner, Invalid(config.model))
    assert result["status"] == "blocked" and result["application"] is None
    assert not result["patch_attempts"]
    attempt = result["dependency_verifications"][0]
    assert attempt["diagnostic_code"] == code
    assert attempt["requests_attempted"] == calls
    assert (git_repo / "a.py").read_text() == "old\n"


def test_duplicate_attempt_and_global_attempt_limit_do_not_send(git_repo, tmp_path):
    from reviewflow.backend.dependency_verification import DependencyError, verify_dependencies
    from reviewflow.backend.live_repair import _review
    from reviewflow.backend.repair_candidate import RepairCandidate

    plan, config, profiles, _, _, authority = setup_cross_batch(git_repo, tmp_path)
    candidate = RepairCandidate.create(plan, authority.gate, authority.authorize_candidate(plan))
    adapter = DependencyAdapter(config.model)
    report = _review(candidate, ("a.py",), authority, profiles, adapter)
    outcome = report.findings[0]
    task = next(t.task for t in report.tasks if t.task.task_id == outcome.finding.task_id)
    audit = []
    verify_dependencies(candidate, report.included_paths, task, outcome, authority, profiles, adapter,
                        audit, stage="initial_review")
    count = authority.issued
    with pytest.raises(DependencyError, match="dependency_already_attempted"):
        verify_dependencies(candidate, report.included_paths, task, outcome, authority, profiles, adapter,
                            audit, stage="initial_review")
    assert authority.issued == count
    audit = [{**audit[0], "finding_id": f"previous-{i}"} for i in range(8)]
    with pytest.raises(DependencyError, match="dependency_limit_exhausted"):
        verify_dependencies(candidate, report.included_paths, task, outcome, authority, profiles, adapter,
                            audit, stage="initial_review")
    assert authority.issued == count


def test_dependency_scope_and_original_finding_provenance_remain_mandatory(git_repo, tmp_path):
    from dataclasses import replace

    from reviewflow.backend.dependency_verification import resolve_dependencies
    from reviewflow.backend.live_repair import _review
    from reviewflow.backend.repair_authority import validate_scoped_review
    from reviewflow.backend.repair_candidate import RepairCandidate

    plan, config, profiles, _, _, authority = setup_cross_batch(git_repo, tmp_path)
    candidate = RepairCandidate.create(plan, authority.gate, authority.authorize_candidate(plan))
    adapter = DependencyAdapter(config.model, "rejected")
    original = _review(candidate, ("a.py",), authority, profiles, adapter)
    report = resolve_dependencies(candidate, original, authority, profiles, adapter, [], stage="final_review")
    validate_scoped_review(candidate, report, final=True)
    outcome = report.findings[0]
    invalid = replace(outcome, verification_scope=("a.py", "private.py"))
    with pytest.raises(ValueError):
        validate_scoped_review(candidate, replace(report, findings=(invalid,)), final=True)
    ref = outcome.finding.evidence[0].model_copy(update={"content_sha256": "0" * 64})
    invalid = replace(outcome, finding=outcome.finding.model_copy(update={"evidence": (ref,)}))
    with pytest.raises(ValueError):
        validate_scoped_review(candidate, replace(report, findings=(invalid,)), final=True)


def test_markdown_reports_dependency_results_without_unknown_diagnostic_text(git_repo, tmp_path):
    from reviewflow.backend.live_repair_cli import _markdown

    plan, config, profiles, runner, preview, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner,
                                DependencyAdapter(config.model, "unresolved"))
    result.update(preview=preview, report_directory="synthetic")
    text = _markdown(result)
    assert "跨批依赖复核" in text and "dependency_unresolved" in text
    result["dependency_verifications"][0]["diagnostic_code"] = "SECRET-RAW-ERROR"
    assert "SECRET-RAW-ERROR" not in _markdown(result)


@pytest.mark.parametrize("mode,attempts", [("four", 4), ("mixed", 3), ("corrected_unknown", 1)])
def test_full_phase_and_evidence_correction_can_precede_dependency_verification(git_repo, tmp_path, mode, attempts):
    class Combined(DependencyAdapter):
        def __call__(self, wire, **kwargs):
            request = json.loads(wire)
            data, stage = request["payload"], request["stage"]
            if stage == "reviewer" and mode != "corrected_unknown" and data["task"]["paths"] == ["a.py"]:
                self.sent.append(wire)
                role = data["task"]["role"]
                findings = [{"finding_id": f"{role}-{i}", "task_id": data["task"]["task_id"],
                    "title": "Synthetic claim", "root_cause": f"Separate synthetic {role}-{i}",
                    "trigger": "Fixture read", "impact": "Wrong fixture value", "severity": "high",
                    "evidence": [data["files"][0]["evidence"]]} for i in range(2)]
                return Reply(json.dumps({"result": {"findings": findings,
                    "reviewed_paths": data["task"]["paths"]}, "requests": []}), Usage(10, 5))
            if data.get("evidence_correction"):
                self.sent.append(wire)
                return Reply(json.dumps({"finding_id": data["finding"]["finding_id"],
                    "verdict": "unresolved" if mode == "corrected_unknown" else "rejected",
                    "rationale": "Corrected metadata, independent result",
                    "evidence": data["finding"]["evidence"]}), Usage(10, 5))
            reply = super().__call__(wire, **kwargs)
            if (stage == "verifier" and not data.get("dependency_verification")
                    and (mode == "corrected_unknown" or (mode == "mixed"
                         and data["finding"]["finding_id"] == "correctness-0"))):
                value = json.loads(reply.text)
                value["result"]["evidence"][0]["content_sha256"] = "0" * 64
                return Reply(json.dumps(value), reply.usage)
            return reply

    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner, Combined(config.model, "rejected"))
    assert result["status"] == "no_changes", result
    assert len(result["dependency_verifications"]) == attempts
    assert not result["patch_attempts"]
    if mode != "corrected_unknown":
        assert result["initial_reviews"][0]["calls"] == 8
    if mode != "four":
        assert result["verification_recoveries"][0]["status"] == "recovered"
        assert result["initial_reviews"][0]["errors"]


def test_same_local_finding_ids_in_distinct_batches_are_not_duplicate_attempts(git_repo, tmp_path):
    class ReusedIDs(RepairFixtureAdapter):
        def __call__(self, wire, **kwargs):
            request = json.loads(wire)
            data, stage = request["payload"], request["stage"]
            if stage == "reviewer" and data["task"]["role"] == "correctness":
                self.sent.append(wire)
                finding = {"finding_id": "synthetic-bug", "task_id": data["task"]["task_id"],
                    "title": "Batch-local candidate", "root_cause": "Distinct local claim",
                    "trigger": "Fixture read", "impact": "Wrong fixture value", "severity": "high",
                    "evidence": [{**data["files"][0]["evidence"], "start_line": 1, "end_line": 1}]}
                return Reply(json.dumps({"result": {"findings": [finding],
                    "reviewed_paths": data["task"]["paths"]}, "requests": []}), Usage(10, 5))
            if stage == "verifier":
                self.sent.append(wire)
                result = {"finding_id": "synthetic-bug", "verdict": "unresolved",
                    "rationale": "Check cross-batch dependency", "evidence": data["finding"]["evidence"]}
                requests = []
                if data.get("dependency_verification"):
                    dependency = "src/b.py" if data["task"]["paths"] == ["a.py"] else "a.py"
                    supplied = [p for p in data["files"] if p["evidence"]["path"] == dependency]
                    if supplied:
                        result.update(verdict="rejected", evidence=[supplied[0]["evidence"]])
                    else:
                        result = None
                        requests = [{"kind": "read", "path": dependency, "start_line": 1, "end_line": 1}]
                return Reply(json.dumps({"result": result, "requests": requests}), Usage(10, 5))
            return super().__call__(wire, **kwargs)

    plan, config, profiles, runner, _, authority = setup_cross_batch(git_repo, tmp_path)
    result = run_repair_session(plan, config, authority, profiles, runner, ReusedIDs(config.model))
    assert result["status"] == "no_changes", result.get("dependency_verifications")
    checks = result["dependency_verifications"]
    assert len(checks) == 2 and all(c["status"] == "complete" for c in checks)
    assert checks[0]["task_id"] == checks[1]["task_id"]
    assert checks[0]["finding_id"] == checks[1]["finding_id"]
    assert checks[0]["snapshot_id"] == checks[1]["snapshot_id"]
    assert checks[0]["review_paths"] != checks[1]["review_paths"]
    assert not result["patch_attempts"]


@pytest.mark.parametrize("ranges,citation,status", [
    (((1, 1), (2, 2)), (1, 2), "no_changes"),
    (((2, 3), (1, 2)), (1, 3), "no_changes"),
    (((1, 1), (3, 3)), (1, 3), "blocked"),
])
def test_dependency_citation_uses_gap_free_union_of_supplied_ranges(git_repo, tmp_path, ranges, citation, status):
    class SplitReads(DependencyAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            data = json.loads(wire)["payload"]
            if not data.get("dependency_verification"):
                return reply
            value = json.loads(reply.text)
            if value["result"] is None:
                value["requests"] = [{"kind": "read", "path": "src/b.py",
                    "start_line": start, "end_line": end} for start, end in ranges]
            else:
                value["result"]["evidence"][0].update(start_line=citation[0], end_line=citation[1])
            return Reply(json.dumps(value), reply.usage)

    # Keep the dependency in a later real byte-sized batch; reads themselves are tiny.
    source = "x=1\nx=2\nx=3\n#" + "p" * (32767 - 13)
    plan, config, profiles, runner, _, authority = setup_cross_batch(
        git_repo, tmp_path, dependency_source=source)
    result = run_repair_session(plan, config, authority, profiles, runner, SplitReads(config.model, "rejected"))
    assert result["status"] == status, result.get("dependency_verifications")
    attempt = result["dependency_verifications"][0]
    assert attempt["requests_attempted"] == 2
    if status == "blocked":
        assert attempt["diagnostic_code"] == "dependency_evidence_not_supplied"
    else:
        assert attempt["verification"]["evidence"][0]["end_line"] == citation[1]
    assert not result["patch_attempts"]

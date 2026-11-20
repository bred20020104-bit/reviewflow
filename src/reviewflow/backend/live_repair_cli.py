"""Explicit CLI approval/report boundary for live development repair."""

import json
import sys

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.approval import ApprovalGate, fingerprint
from reviewflow.backend.candidate_seed import SeedPlan
from reviewflow.backend.credentials import resolve_key_file
from reviewflow.backend.dependency_verification import DEPENDENCY_CODES
from reviewflow.backend.development import DevelopmentLimits, load_development_config
from reviewflow.backend.diagnostics import SAFE_ERROR_CODES
from reviewflow.backend.finding_refresh import REFRESH_CODES
from reviewflow.backend.live_repair import repair_batches, run_repair_session
from reviewflow.backend.live_review import output_location, save_report, selected_inventory
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.repair_candidate import CandidatePlan
from reviewflow.backend.repair_diagnostics import render_test_runs
from reviewflow.backend.responses_adapter import ResponsesAdapter
from reviewflow.backend.volume_runner import VolumeDockerRunner
from reviewflow.backend.worker_progress import safe_progress
from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import ScanPolicy
from reviewflow.review.evidence_errors import EVIDENCE_CODES
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import open_source


def _markdown(document):
    recoveries = document.get("verification_recoveries", [])
    codes = {f.get("error") for report in
             document.get("initial_reviews", []) + document.get("final_reviews", [])
             for f in report.get("findings", [])}
    reviewer_recoveries = document.get("reviewer_recoveries", [])
    codes.update(t.get("error") for report in
                 document.get("initial_reviews", []) + document.get("final_reviews", [])
                 for t in report.get("tasks", []))
    codes.update(r.get("diagnostic_code") for r in recoveries + reviewer_recoveries)
    safe_codes = EVIDENCE_CODES | {
        "invalid_evidence", "unsupplied_evidence", "invalid_output", "verification_unresolved",
        "recovery_budget_exhausted", "recovery_authorization_stopped",
        "recovery_transport_stopped", "recovery_stopped",
        "reviewer_recovery_authorization_stopped", "reviewer_recovery_transport_stopped",
        "reviewer_recovery_stopped", "finding_evidence_not_reviewed",
    }
    diagnostics = ", ".join(sorted(c for c in codes if c in safe_codes)) or "none"
    recovered = sum(r["status"] == "recovered" for r in recoveries)
    refreshes = document.get("finding_refreshes", [])
    refresh_codes = ", ".join(sorted({r["diagnostic_code"] for r in refreshes
        if r.get("diagnostic_code") in REFRESH_CODES | EVIDENCE_CODES})) or "none"
    resolved = sum(issue.get("status") == "resolved" for r in refreshes for issue in r["issues"])
    dependencies = document.get("dependency_verifications", [])
    dependency_codes = ", ".join(sorted({r["diagnostic_code"] for r in dependencies
        if r.get("diagnostic_code") in DEPENDENCY_CODES | EVIDENCE_CODES})) or "none"
    decided = sum(r.get("status") == "complete" for r in dependencies)
    transport_diagnostics = []
    for record in document["accounting"].get("records", []):
        code = record.get("error_code")
        if type(code) is not str or code not in SAFE_ERROR_CODES:
            continue
        progress = safe_progress(record.get("transport_progress"))
        detail = "stage diagnostics unavailable"
        if progress is not None:
            event = progress["events"][-1]
            detail = f"last completed stage {event['stage']} ({event['elapsed_ms']} ms inside the worker)"
        recovery = "a later retry recovered" if record.get("retry_status") == "recovered" else "not recovered"
        transport_diagnostics.append(f"- Transport attempt failed: {code}; {recovery}; {detail}\n")
    seed_accounting = ""
    cumulative = document.get("cumulative_reported_accounting")
    if cumulative is not None:
        seed_accounting = (
            "\n## Explicit candidate continuation\n"
            "The old candidate is unverified source input only. "
            "Tests, review, and write-back authority are created again for this run.\n"
            "Historical usage comes from the old report and is not certified. "
            "It does not mean old calls were free or that old permissions were reopened.\n"
            f"- Cumulative requests attempted: {cumulative['requests_attempted']}\n"
            f"- Cumulative patch-member attempts: {cumulative['patch_attempts']}\n"
            "- Cumulative cost: unknown.\n"
        )
    records = document["accounting"].get("records", [])
    retry_count = sum(type(r.get("retry_attempt")) is int and r["retry_attempt"] > 0 for r in records)
    retry_limit = (document["preview"].get("transport_retry") or {}).get("max_retries", 0)
    unknown_usage = sum(r.get("usage") is None for r in records)
    cap = document["preview"]["max_calls"]
    cap_label = "no total model-call cap (other limits remain)" if cap is None else str(cap)
    return (
        "# ReviewFlow automated repair report\n\n"
        f"- Status: {document['status']}\n"
        f"- Requests attempted: {document['accounting']['requests_attempted']} / {cap_label}\n"
        "- Cost: unknown. Nothing was committed, pushed, or merged.\n"
        f"- Transport retries: at most {retry_limit} per request; "
        f"{retry_count} extra attempts were made, and each counts as a call.\n"
        f"- Attempts with unknown usage: {unknown_usage}. "
        "A successful retry does not make the earlier request free or its usage complete.\n"
        f"- Report directory: `{document['report_directory']}`\n"
        f"- Candidate directory: `{document['candidate_path']}`\n"
        f"- Failure stage: {document.get('failure_stage', 'none')}\n"
        f"- Tests only: {document.get('tests_only', False)}\n"
        f"- Evidence diagnostics (including recovered original errors): {diagnostics}\n"
        f"- Verification recovery: {len(recoveries)} records; recovered: {recovered}\n"
        f"- Reviewer recovery: {len(reviewer_recoveries)} records; recovered: "
        f"{sum(r.get('status') == 'recovered' for r in reviewer_recoveries)}\n"
        f"- Cross-batch dependency checks: {len(dependencies)} attempts; "
        f"independent decisions: {decided}; diagnostics: {dependency_codes}\n"
        "Reading a dependency does not widen review coverage or edit permission. "
        "The original unresolved result and the effective recheck stay in report.json.\n"
        f"- Review after patches: {len(refreshes)} rounds; "
        f"statically confirmed removed: {resolved}; diagnostics: {refresh_codes}\n"
        "An old finding leaves the queue only after a new evidence review and an independent check. "
        "Static removal does not mean the whole repair succeeded.\n\n"
        "Validation covers HEAD plus the candidate patch, not uncommitted content in the original workspace. "
        "Semantic review covers only the approved source. Tests cover every discoverable committed test. "
        "Individual test counts and raw logs are not retained, and no claim is made that every defect was reproduced dynamically.\n\n"
        "`report.json` stores the initial review, independent verification, patch attempts, "
        "container exit status, final review, and write-back result. "
        "`candidate.diff` is the ownership-checked candidate diff. "
        "A blocked status does not mean it was written back to the original repository. "
        "On partial_application both workspaces are kept for manual inspection and are not cleaned up automatically.\n"
        + "".join(transport_diagnostics)
        + seed_accounting
        + render_test_runs(document["test_runs"])
    )


def run_live_repair(args):
    directory_fd = None
    try:
        if not args.development:
            raise ValueError("Explicit development mode required")
        source = open_source(args.path, "head")
        root = source.info.root.resolve()
        output = output_location(args.output, root)
        destination = output.with_name(output.name + "-candidate")
        plan = CandidatePlan.from_head(source, tuple(args.include), destination)
        if args.no_call_limit and (args.tests_only or args.seed_report is not None):
            raise ValueError("Uncapped calls require fresh repair mode without seed import")
        if args.seed_report is not None and args.tests_only:
            raise ValueError("Tests-only cannot import a candidate seed")
        seed = SeedPlan.load(plan, args.seed_report) if args.seed_report is not None else None
        inventory = selected_inventory(build_inventory(source, ScanPolicy()), tuple(args.include))
        config = load_development_config(args.config, source, inventory)
        config = config.model_copy(update={"limits": DevelopmentLimits(
            max_retries=0 if args.tests_only else args.transport_retries)})
        profiles = load_profiles()
        runner = VolumeDockerRunner(args.image, timeout_seconds=args.test_timeout,
            sandbox_profile=args.test_sandbox,
            executable_paths=tuple(path for path, mode in plan.modes if mode == 0o755))
        preview = make_repair_preview(plan, config, profiles, runner, output, args.max_calls,
                                      tests_only=args.tests_only,
                                      patch_scheduling=args.patch_scheduling, seed=seed,
                                      no_call_limit=args.no_call_limit)
        preview.pop("approval_digest")
        preview["semantic_batches"] = ([] if args.tests_only else
                                       [list(batch) for batch in repair_batches(plan)])
        preview["approval_digest"] = fingerprint(preview)
        if not args.approve:
            print(json.dumps(preview, ensure_ascii=True, indent=2, sort_keys=True))
            return 0
        gate = ApprovalGate(ttl_seconds=3600)
        authority = RepairRunAuthority(preview, args.approve, gate)
        plan.check_origin()
        parent = output.parent.stat()
        if (parent.st_dev, parent.st_ino) != plan.storage_identity:
            raise ValueError("Report parent changed")
        output.mkdir(mode=0o700)
        from reviewflow.backend.nofollow import open_directory

        directory_fd = open_directory(output)
        adapter = (None if args.tests_only else
                   ResponsesAdapter(config.model, lambda: resolve_key_file(args.config, config.model)))
        document = run_repair_session(plan, config, authority, profiles, runner, adapter, seed=seed)
        document["preview"] = preview
        document["report_directory"] = str(output)
        document["report_files"] = {name: str(output / name) for name in
                                    ("report.json", "report.md", "candidate.diff")}
        encoded = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        save_report(directory_fd, "report.json", encoded)
        save_report(directory_fd, "report.md", _markdown(document))
        save_report(directory_fd, "candidate.diff", document.get("candidate_diff") or "")
        print(encoded, end="")
        return 0 if document["status"] in {"applied", "no_changes", "tests_passed"} else 1
    except (ValueError, OSError, CollectionError, InvalidInputError):
        print("review: repair input/configuration/approval/output rejected; no automatic retry "
              "or cleanup. Preserve existing report and candidate directories.", file=sys.stderr)
        return 2
    finally:
        if directory_fd is not None:
            from reviewflow.backend.nofollow import close_directory

            close_directory(directory_fd)

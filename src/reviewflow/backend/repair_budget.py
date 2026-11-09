"""Parent repair authorization; transport ceilings remain independently enforced."""

import json
import re
import time
from dataclasses import dataclass

from reviewflow.backend.approval import Denied, fingerprint
from reviewflow.backend.candidate_seed import SEED_POLICY
from reviewflow.backend.dependency_verification import DEPENDENCY_POLICY
from reviewflow.backend.development import (
    DevelopmentConfig,
    DevelopmentTransport,
    transport_retry_policy,
)
from reviewflow.backend.finding_refresh import REFRESH_POLICY
from reviewflow.backend.gateway import WorkflowGateway
from reviewflow.backend.grouped_patch import GROUP_POLICY
from reviewflow.backend.responses_adapter import ADAPTER_VERSION
from reviewflow.backend.reviewer_recovery import REVIEWER_RECOVERY_POLICY
from reviewflow.backend.verification_recovery import RECOVERY_POLICY
from reviewflow.review.interactive import LIVE_REVIEW_POLICY, TOOL_INSTRUCTIONS

FINAL_REVIEW_POLICY = {
    "version": 1, "calls_per_batch": 8, "activation": "before_first_patch",
    "release": "after_final_tests_pass",
}


def make_repair_preview(plan, config, profiles, runner, output, max_calls, *, tests_only=False,
                        patch_scheduling="serial", seed=None, no_call_limit=False):
    config = DevelopmentConfig.model_validate(config)
    if tests_only and config.limits.max_retries:
        raise ValueError("Tests-only cannot authorize model retries")
    if (type(no_call_limit) is not bool
            or (no_call_limit and (tests_only or seed is not None))):
        raise ValueError("Uncapped calls require explicit repair mode without seed import")
    if seed is not None:
        if tests_only:
            raise ValueError("Tests-only cannot import a candidate seed")
        seed.check(plan)
    if patch_scheduling not in ("serial", "grouped"):
        raise ValueError("Invalid patch scheduling policy")
    if type(max_calls) is not int or not 1 <= max_calls <= 48 or type(tests_only) is not bool:
        raise ValueError("Repair call cap must be 1..48 and mode must be boolean")
    preview = {
        "operation": "live_development_repair", "adapter_version": ADAPTER_VERSION,
        "candidate": plan.preview(), "config": config.model_dump(mode="json"),
        "agent_profiles": profiles.manifest(), "tool_instructions": TOOL_INSTRUCTIONS,
        "review_policy": dict(LIVE_REVIEW_POLICY),
        "test_policy": runner.intent(dict(plan.baseline)),
        "output": str(output), "max_calls": 0 if tests_only else None if no_call_limit else max_calls,
        "no_call_limit": no_call_limit,
        "tests_only": tests_only, "max_patch_attempts": 2,
        "seed": seed.preview() if seed is not None else None,
        "seed_policy": dict(SEED_POLICY) if seed is not None else None,
        "patch_scheduling": (None if tests_only else dict(GROUP_POLICY)
                             if patch_scheduling == "grouped" else {"version": 1, "mode": "serial"}),
        "verification_recovery": None if tests_only else json.loads(json.dumps(RECOVERY_POLICY)),
        "reviewer_recovery": None if tests_only else json.loads(json.dumps(REVIEWER_RECOVERY_POLICY)),
        "finding_refresh": None if tests_only else json.loads(json.dumps(REFRESH_POLICY)),
        "dependency_verification": None if tests_only else dict(DEPENDENCY_POLICY),
        "final_review_budget": None if tests_only else dict(FINAL_REVIEW_POLICY),
        "transport_retry": transport_retry_policy(config.limits.max_retries),
        "expires_after_seconds": 3600,
        "cost_usd": None, "network_requests": 0,
        "notice": "HEAD-derived tests only; existing dirty work is not tested. "
                  "Selected-source writeback only after tests and independent review. "
                  "One evidence correction per failed finding/review may use a separate "
                  "single-request phase within the same total model budget. "
                  "A reviewer task with invalid evidence metadata or in-scope evidence omitted "
                  "from its reviewed-path declaration may be reissued once with "
                  "independent verification, at most eight tasks per run and eight requests per "
                  "attempt, all within the same total budget. "
                  "Owned patches trigger bounded both-role reassessment of affected pending "
                  "issues within the same total model budget; unknown outcomes block writeback. "
                  "A refreshed verifier result may receive one independent evidence correction "
                  "per finding and current snapshot, still within the same total model budget. "
                  "Independent unresolved findings may read approved cross-batch dependencies in "
                  "bounded verification phases within the same total budget, without widening "
                  "reviewer coverage or editable scope. Refreshed verifier claims may be batched "
                  "in pairs with complete per-ID evidence validation. An unresolved refreshed "
                  "reviewer assessment may read approved dependencies once per current claim, "
                  "at most eight recovery attempts with three requests each and 32768 unique source "
                  "bytes. Current evidence inside original finding paths and independent dependency "
                  "verification are required; uncertainty or disagreement still blocks. "
                  "Before the first patch, "
                  "eight calls per semantic batch are held for final review; repair phases cannot "
                  "spend them. They are released only after final tests pass. Insufficient budget "
                  "blocks; reservation does not guarantee recovery capacity. "
                  "No commits or pushes. Unknown price; "
                  "HTTP exposes credentials/source. "
                  "Do not edit selected files concurrently.",
    }
    if no_call_limit:
        preview["notice"] = preview["notice"].replace(
            "Before the first patch, eight calls per semantic batch are held for final review; "
            "repair phases cannot spend them. They are released only after final tests pass. "
            "Insufficient budget blocks; reservation does not guarantee recovery capacity. ",
            "Explicit no aggregate model-call limit for this approved run; cost may increase. "
            "No numeric final-call reserve is needed, but final tests and independent review "
            "remain mandatory. Per-phase eight-call, request, repair/recovery and one-hour "
            "authority lifetime limits remain unchanged; unrecovered transport failure stops. "
        )
    if config.limits.max_retries:
        preview["notice"] += (
            f" Transient transport retries enabled: at most {config.limits.max_retries} retries "
            "per logical request, only HTTP408/response_timeout/worker_timeout, with 1s then2s "
            "backoff. Every physical attempt needs a fresh grant, counts toward phase/parent "
            "limits and retains its own usage record. Source/expiry/budget rechecked after waiting. "
            "Only a validated success for the same owned request recovers prior transient failures; "
            "unknown usage and possible duplicate provider charges remain unknown, not free. "
            "Final tests and review still required; no automatic replay of a completed run."
        )
    if patch_scheduling == "grouped" and not tests_only:
        preview["notice"] += (
            " Grouped scheduling combines at most four individually owned findings from one "
            "current review scope; conflicting edits reject the whole group. Each member uses "
            "one of eight patch-attempt slots, including retries. Whole-group tests and "
            "independent refresh of patched claims are mandatory; refreshed scopes queue last. "
            "Complete diagnostics exposing newly failing test definitions relative to the retained "
            "pre-patch candidate trigger whole-group rollback and one bounded retry; assertion-line "
            "changes alone do not. Model feedback includes bounded test statuses, fixed categories, "
            "numeric locations and opaque definition IDs, never test paths/source, raw logs, "
            "assertion values or parameter IDs. A rejected proposal and its tested content digest "
            "may inform the retry; observations do not prove causality or resolve findings."
        )
    if seed is not None:
        preview["notice"] += (
            " Explicit seed import: selected bytes from a prior candidate are UNVERIFIED INPUT "
            "under this NEW approval, never restored authority or reusable test/review receipts. "
            "Fresh HEAD baseline, seeded candidate tests and full independent reviews are required. "
            "Historical accounting is report-supplied, not authenticated or a zero-cost claim; "
            "it is displayed cumulatively without resetting this run's independent ceilings. "
            "At most two seed continuations (three runs, reported total144 calls/24 member attempts)."
        )
    if tests_only:
        preview["notice"] = ("Tests-only: no model requests and no source writeback. "
                             "HEAD-derived tests in retained candidate; dirty work is not tested. "
                             "No commits or pushes.")
    if preview["test_policy"].get("sandbox", {}).get("profile") == "exec-scratch":
        preview["notice"] += (
            " Sandbox exec-scratch explicitly permits temporary executable programs in "
            "a container-only 32MiB scratch tmpfs; TMPDIR=/scratch. This enlarges execution "
            "permission compared with strict. /tmp remains noexec; source/root stay readonly, "
            "network stays disabled, and nonroot/resource restrictions remain in force."
        )
    preview["approval_digest"] = fingerprint(preview)
    return preview


@dataclass
class RepairPhase:
    source: object
    inventory: object
    transport: DevelopmentTransport
    gateway: WorkflowGateway | None = None
    issued: int = 0
    max_requests: int = 8


class RepairRunAuthority:
    def __init__(self, preview, approved_digest, gate):
        bound = {k: v for k, v in preview.items() if k != "approval_digest"}
        no_call_limit = preview.get("no_call_limit", False)
        if (type(no_call_limit) is not bool or "max_calls" not in preview
                or (no_call_limit and (preview["max_calls"] is not None
                    or preview.get("tests_only", False) or preview.get("seed") is not None))):
            raise Denied("Invalid aggregate call-limit policy")
        if (not approved_digest or approved_digest != preview.get("approval_digest")
                or fingerprint(bound) != approved_digest
                or (not no_call_limit and type(preview["max_calls"]) is not int)
                or type(preview.get("tests_only", False)) is not bool
                or (not no_call_limit and (preview["max_calls"] != 0 if preview.get("tests_only", False)
                    else not 1 <= preview["max_calls"] <= 48))
                or preview.get("operation") != "live_development_repair"
                or (preview.get("seed") is not None and preview.get("tests_only", False))
                or fingerprint(preview.get("seed_policy")) != fingerprint(
                    SEED_POLICY if preview.get("seed") is not None else None)
                or preview.get("max_patch_attempts") != 2
                or fingerprint(preview.get("patch_scheduling")) not in (
                    {fingerprint(None)} if preview.get("tests_only", False) else
                    {fingerprint(GROUP_POLICY), fingerprint({"version": 1, "mode": "serial"})})
                or fingerprint(preview.get("verification_recovery")) != fingerprint(
                    None if preview.get("tests_only", False) else RECOVERY_POLICY)
                or fingerprint(preview.get("reviewer_recovery")) != fingerprint(
                    None if preview.get("tests_only", False) else REVIEWER_RECOVERY_POLICY)
                or fingerprint(preview.get("finding_refresh")) != fingerprint(
                    None if preview.get("tests_only", False) else REFRESH_POLICY)
                or fingerprint(preview.get("dependency_verification")) != fingerprint(
                    None if preview.get("tests_only", False) else DEPENDENCY_POLICY)
                or fingerprint(preview.get("final_review_budget")) != fingerprint(
                    None if preview.get("tests_only", False) else FINAL_REVIEW_POLICY)
                or preview.get("expires_after_seconds") != 3600
                or preview.get("adapter_version") != ADAPTER_VERSION):
            raise Denied("Repair approval missing or mismatched")
        self.preview = json.loads(json.dumps(preview))
        self.config = DevelopmentConfig.model_validate_json(json.dumps(preview["config"]))
        if ((preview.get("tests_only", False) and self.config.limits.max_retries)
                or fingerprint(preview.get("transport_retry")) != fingerprint(
                    transport_retry_policy(self.config.limits.max_retries))):
            raise Denied("Retry policy differs from approved fixed limits")
        self.gate = gate
        self.issued = 0
        self._final_calls_held = 0
        self._final_reserved = False
        self._final_started = False
        self.blocked = False
        self._active = None
        self._records = []
        self._deadline = time.monotonic() + 3600
        intent = {"action": "repair_run", "digest": approved_digest}
        gate.consume(gate.approve(intent, confirmed=True), intent)

    def check(self, candidate=None):
        if self.blocked or time.monotonic() >= self._deadline:
            self.blocked = True
            raise Denied("Repair run stopped or expired")
        if self._active and self._active.transport.blocked:
            self.blocked = True
            raise Denied("Repair transport failed")
        if candidate is not None:
            if fingerprint(candidate.plan.preview()) != fingerprint(self.preview["candidate"]):
                raise Denied("Candidate outside approved repair")
            candidate.plan.check_origin()
            candidate.files()

    def authorize_candidate(self, plan):
        self.check()
        if fingerprint(plan.preview()) != fingerprint(self.preview["candidate"]):
            raise Denied("Candidate plan changed")
        plan.check_origin()
        return self.gate.approve(plan.preview(), confirmed=True)

    @property
    def model_call_limit(self):
        cap = self.preview["max_calls"]
        return None if cap is None else cap - self._final_calls_held

    @property
    def call_budget_exhausted(self):
        limit = self.model_call_limit
        return limit is not None and self.issued >= limit

    def reserve_final_review(self, candidate):
        from reviewflow.backend.live_repair import repair_batches

        self.check(candidate)
        if self._active or self._final_reserved or self.preview.get("tests_only", False):
            raise Denied("Invalid final review reservation")
        count = len(repair_batches(candidate.plan)) * FINAL_REVIEW_POLICY["calls_per_batch"]
        cap = self.preview["max_calls"]
        if cap is not None and self.issued + count >= cap:
            raise Denied("Insufficient budget for repair and final review")
        self._final_calls_held = 0 if cap is None else count
        self._final_reserved = True

    def begin_final_review(self, candidate):
        """Trusted controller transition, only after its final full-test gate."""
        self.check(candidate)
        if self._active or not self._final_reserved or self._final_started:
            raise Denied("Invalid final review transition")
        self._final_calls_held = 0
        self._final_started = True

    def start_phase(self, candidate, paths, adapter, profiles, *, max_requests=8):
        self.check(candidate)
        if (type(max_requests) is not int or not 1 <= max_requests <= 8
                or self._active is not None or self.call_budget_exhausted
                or adapter.model != self.config.model
                or profiles.manifest() != self.preview["agent_profiles"]):
            raise Denied("Repair phase exceeds bound policy")
        source, inventory = candidate.source_inventory(paths)
        config = self.config.model_copy(update={"privacy": self.config.privacy.model_copy(update={
            "allowed_project_root": str(candidate.path), "allowed_paths": tuple(paths),
            "allow_code_upload": True,
        })})
        transport = DevelopmentTransport(config, adapter, self.gate,
            retry_authorize=lambda intent: self.authorize_model(candidate, phase, intent))
        phase = RepairPhase(source, inventory, transport, max_requests=max_requests)
        phase.gateway = WorkflowGateway(source, inventory, transport, adapter.body_size,
            lambda intent: self.authorize_model(candidate, phase, intent))
        self._active = phase
        return phase

    def authorize_model(self, candidate, phase, intent):
        self.check(candidate)
        allowed = {r.path: r.sha256 for r in phase.inventory.snapshot.records if r.status == "included"}
        files = intent.get("files")
        if (self._active is not phase or phase.issued >= phase.max_requests
                or self.call_budget_exhausted
                or set(intent) != {"action", "snapshot", "files", "root", "config", "payload_sha256", "request_bytes"}
                or intent.get("action") != "development_send"
                or intent.get("snapshot") != phase.inventory.snapshot.snapshot_id
                or intent.get("root") != str(candidate.path)
                or intent.get("config") != phase.transport.config.model_dump(mode="json")
                or phase.transport.config.model != self.config.model
                or not isinstance(files, dict) or not files
                or any(allowed.get(p) != digest for p, digest in files.items())
                or type(intent.get("request_bytes")) is not int
                or not 0 < intent["request_bytes"] <= 65536
                or not isinstance(intent.get("payload_sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", intent["payload_sha256"])):
            raise Denied("Model request exceeds approved repair phase")
        token = self.gate.approve(intent, confirmed=True)
        self.issued += 1
        phase.issued += 1
        return token

    def record_phase(self, phase):
        if self._active is not phase:
            raise Denied("Foreign or already closed repair phase")
        transport = phase.transport
        self._records.extend(json.loads(json.dumps(transport.records)))
        if (transport.blocked or transport.calls != len(transport.records)
                or transport.calls > phase.issued
                or any((r["status"] != "complete" or r["usage"] is None)
                       and not transport.is_recovered_failure(r) for r in transport.records)):
            self.blocked = True
        self._active = None

    def authorize_tests(self, candidate, runner):
        self.check(candidate)
        if self._active:
            raise Denied("Finish model phase before executing tests")
        intent = runner.intent(candidate.files())
        policy = dict(self.preview["test_policy"])
        policy["digest"] = intent["digest"]
        if intent != policy:
            raise Denied("Test policy differs from approved repair")
        return self.gate.approve(intent, confirmed=True)

    def accounting(self):
        records = self._records + (self._active.transport.records if self._active else [])
        usages = [r["usage"] for r in records if r["usage"] is not None]
        return {
            "requests_attempted": len(records), "requests_reserved": self.issued,
            "final_review_calls_held": self._final_calls_held,
            "final_review_started": self._final_started,
            "input_tokens_reported": sum(u["input_tokens"] for u in usages),
            "output_tokens_reported": sum(u["output_tokens"] for u in usages),
            "usage_complete": all(r["usage"] is not None for r in records),
            "cost_usd": None, "records": json.loads(json.dumps(records)),
        }

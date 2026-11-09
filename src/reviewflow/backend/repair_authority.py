"""Process-local working-tree application permits, never publication permission.

Review reports must come from the trusted controller's actual live execution, not
from target/model JSON or a saved report. This is not a security boundary against
arbitrary Python code in the trusted application process.
"""

import secrets
import time
from dataclasses import dataclass

from reviewflow.backend.dependency_verification import validate_dependency_outcome
from reviewflow.backend.live_patch import live_editable
from reviewflow.backend.repair import RepairError
from reviewflow.errors import CollectionError
from reviewflow.review.context import ReviewContext
from reviewflow.review.workflow_models import RunReport


def validate_review_provenance(candidate, review):
    """Structural preflight only; never grants permission to patch or write back."""
    if (
        not isinstance(review, RunReport)
        or review.execution != "live_development"
        or not review.semantic_review_performed
        or review.mode != "multi"
        or review.status not in {"complete", "partial"}
        or review.errors or review.omissions or review.unreviewed_paths
        or not review.included_paths
        or set(review.supplied_paths) != set(review.included_paths)
        or set(review.reported_reviewed_paths) != set(review.included_paths)
    ):
        raise RepairError("Complete selected-scope real review required")
    source, inventory = candidate.source_inventory(review.included_paths)
    if review.snapshot_id != inventory.snapshot.snapshot_id:
        raise RepairError("Review is not for current candidate snapshot")
    context = ReviewContext(source, inventory)
    tasks_by_id = {t.task.task_id: t for t in review.tasks}
    if len(tasks_by_id) != len(review.tasks):
        raise RepairError("Ambiguous review task identity")
    try:
        for outcome in review.tasks:
            context.validate_task(outcome.task)
            if outcome.error or not set(outcome.reviewed_paths) <= set(outcome.task.paths):
                raise RepairError("Task failed or reported paths outside its scope")
        for outcome in review.findings:
            task = tasks_by_id[outcome.finding.task_id].task
            if task.role != outcome.role or outcome.verification is None:
                raise RepairError("Finding role or verification missing")
            if outcome.verification_scope:
                validate_dependency_outcome(candidate, review.included_paths, task, outcome)
            else:
                context.validate_verification(task, outcome.finding, outcome.verification)
    except (CollectionError, KeyError, ValueError):
        raise RepairError("Final review provenance invalid") from None
    for role in ("correctness", "security"):
        tasks = [t for t in review.tasks if t.task.role == role]
        if (
            not tasks or any(t.status != "completed" for t in tasks)
            or {p for t in tasks for p in t.reviewed_paths} != set(review.included_paths)
            or any(t.task.snapshot_id != review.snapshot_id for t in tasks)
        ):
            raise RepairError("Independent role coverage incomplete")


def validate_scoped_review(candidate, review, *, final=False):
    validate_review_provenance(candidate, review)
    allowed = {"rejected"} if final else {"accepted", "rejected"}
    if any(outcome.error or not outcome.verification
           or outcome.verification.verdict not in allowed for outcome in review.findings):
        raise RepairError("Review findings remain unresolved")


@dataclass(frozen=True)
class ApplicationPermit:
    nonce: str


class ApplicationAuthority:
    def __init__(self, runner):
        self.runner = runner
        self._permits = {}

    def finish(self, candidate, baseline, result, reviews):
        candidate.plan.check_origin()
        digest = candidate.current_digest()
        if digest == candidate.baseline_digest:
            raise RepairError("No changed candidate to apply")
        for check, expected_digest in ((baseline, candidate.baseline_digest), (result, digest)):
            if (
                not self.runner.owns(check)
                or check.execution != "container"
                or check.image != self.runner.image
                or check.suite != "repository"
                or check.content_digest != expected_digest
            ):
                raise RepairError("Owned complete repository test results required")
        if (baseline.status, baseline.exit_code) not in {("passed", 0), ("failed", 1)}:
            raise RepairError("Baseline test environment blocked")
        if (result.status, result.exit_code) != ("passed", 0):
            raise RepairError("Candidate repository tests did not pass")
        covered = set()
        for review in reviews:
            validate_scoped_review(candidate, review, final=True)
            covered.update(review.included_paths)
        if covered != set(candidate.plan.selected):
            raise RepairError("Final review does not cover entire approved selection")
        baseline_files, original_files = dict(candidate.plan.baseline), dict(candidate.plan.originals)
        for path, data in candidate.files().items():
            if data != baseline_files[path] and (
                path not in candidate.plan.selected or not live_editable(path)
                or original_files.get(path) != baseline_files[path]
            ):
                raise RepairError("Changed path is protected or originally dirty")
        permit = ApplicationPermit(secrets.token_hex(32))
        self._permits[permit.nonce] = (permit, candidate, digest, time.monotonic() + 300)
        return permit

    def consume(self, candidate, permit):
        if not isinstance(permit, ApplicationPermit):
            raise RepairError("Application permit missing")
        record = self._permits.get(permit.nonce)
        if (not record or record[0] is not permit or record[1] is not candidate
                or record[3] <= time.monotonic()):
            raise RepairError("Application permit missing or expired")
        candidate.plan.check_origin()
        if record[2] != candidate.current_digest():
            raise RepairError("Application permit snapshot changed")
        del self._permits[permit.nonce]
        return record[2]

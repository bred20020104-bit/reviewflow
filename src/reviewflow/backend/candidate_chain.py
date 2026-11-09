"""In-process, explicitly approved candidate composition; never validation evidence.

Owns a fresh Workspace's attempt state. Do not mix with direct Workspace.apply or
run_repair calls. No deserialization/reopening, execution, commit or push API.
"""

from reviewflow.backend.repair import PartialApplicationError, RepairError, validate_patch
from reviewflow.review.context import ReviewContext


class CandidateChain:
    def __init__(self, workspace):
        if workspace._previous is not None or workspace._committed or workspace.attempts:
            raise RepairError("Composition requires a fresh owned workspace")
        self.workspace = workspace
        self._digest = workspace.current_digest()
        self._checkpoints = []
        self._blocked = False
        self._attempts = 0

    def _check(self):
        if self._blocked or self.workspace._committed:
            raise RepairError("Composition blocked; preserve workspace")
        if self.workspace.current_digest() != self._digest:
            raise RepairError("Candidate changed outside composition")

    def _prepare(self, task, finding, proposal):
        self._check()
        if self._attempts >= 80 or len(self._checkpoints) >= 40:
            raise RepairError("Composition limit")
        source, inventory = self.workspace.inventory()
        patch = validate_patch(ReviewContext(source, inventory), task, finding, proposal)
        intent = {
            "action": "compose-candidate",
            "workspace": str(self.workspace.path),
            "base": self.workspace.base,
            "candidate_digest": self._digest,
            "snapshot": inventory.snapshot.snapshot_id,
            "step": len(self._checkpoints) + 1,
            "attempt": self._attempts + 1,
            "patch": patch.sha256,
            "finding": finding.finding_id,
            "validation": "not_run",
        }
        return patch, intent

    def intent(self, task, finding, proposal):
        """Preview binding for a separately reviewed diff, not a permission token."""
        return self._prepare(task, finding, proposal)[1]

    def apply(self, task, finding, proposal, gate, grant):
        patch, intent = self._prepare(task, finding, proposal)
        gate.consume(grant, intent)
        ws = self.workspace
        checkpoint = (ws.snapshot, ws._previous, self._digest)
        self._attempts += 1
        ws._previous = None
        ws.snapshot = intent["snapshot"]
        try:
            # The consumed composition grant authorizes this exact inner write only.
            ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
        except PartialApplicationError:
            self._blocked = True
            raise
        except Exception:
            # Workspace.apply either did not write, or restored the previous candidate.
            if ws.current_digest() != checkpoint[2]:
                self._blocked = True
                raise PartialApplicationError(
                    "Candidate restoration failed; preserve workspace"
                ) from None
            ws.snapshot, ws._previous, self._digest = checkpoint
            raise
        self._checkpoints.append(checkpoint)
        self._digest = ws.current_digest()

    def rollback_last(self):
        """Undo only our latest step; user edits block rather than get overwritten."""
        self._check()
        if not self._checkpoints:
            raise RepairError("No candidate to roll back")
        try:
            self.workspace.rollback_attempt()
        except Exception:
            self._blocked = True
            raise PartialApplicationError("Rollback incomplete; preserve workspace") from None
        snapshot, previous, digest = self._checkpoints.pop()
        self.workspace.snapshot = snapshot
        self.workspace._previous = previous
        self._digest = digest
        self._check()

    def summary(self):
        self._check()
        return {
            "workspace": str(self.workspace.path),
            "candidate_digest": self._digest,
            "steps": len(self._checkpoints),
            "attempts": self._attempts,
            "validation": "not_run",
            "commit_ready": False,
            "target_tests_executed": False,
            "notice": "Unverified candidate composition, not a validated repair chain.",
        }

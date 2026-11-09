"""One selected finding, at most two proposals, no automatic publication.

Callbacks are trusted application services, never loaded from target configuration.
Restart deliberately requires a new session/approval; existing workspaces are not
silently reopened and attempt/audit files are never treated as permission grants.
"""

from reviewflow.backend.approval import Denied
from reviewflow.backend.patch_agent import propose_patch
from reviewflow.backend.repair import PartialApplicationError, RepairError


def run_repair(
    ws, context, task, finding, backend, gate, authorize, authority, validate, *, max_attempts=2
):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
        raise RepairError("Invalid attempt limit")
    failures = []
    for _ in range(max_attempts):
        if ws.attempts.get(finding.finding_id, 0) >= 2:
            break
        try:
            patch = propose_patch(context, task, finding, backend, feedback=bool(failures))
            ws.apply(patch, gate, authorize(ws.patch_intent(patch)))
        except Denied:
            return {"status": "approval_required", "attempts": len(failures), "commit_ready": False}
        except PartialApplicationError:
            return {
                "status": "blocked_partial_application",
                "attempts": ws.attempts.get(finding.finding_id, 0),
                "commit_ready": False,
                "errors": [*failures, "application_rollback_failed"],
            }
        except RepairError:
            failures.append("proposal_failed")
            continue
        try:
            receipt = validate(ws)
            authority.verify(ws, receipt)
        except Exception:
            failures.append("verification_failed")
            try:
                ws.rollback_attempt()
            except Exception:
                return {
                    "status": "blocked_workspace_changed",
                    "attempts": len(failures),
                    "commit_ready": False,
                    "errors": failures,
                }
            continue
        ws.record_event({"event": "validated", "candidate_digest": ws.current_digest()})
        return {
            "status": "validated",
            "attempts": ws.attempts[finding.finding_id],
            "commit_ready": True,
            "receipt": receipt,
            "errors": failures,
        }
    return {
        "status": "unresolved",
        "attempts": len(failures),
        "commit_ready": False,
        "errors": failures,
    }

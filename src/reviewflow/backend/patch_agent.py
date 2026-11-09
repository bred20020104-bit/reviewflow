"""Patch-role request builder for trusted offline backends; no live transport."""

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.repair import PatchProposal, RepairError, validate_patch


def propose_patch(context, task, finding, backend, *, feedback=None):
    context.validate_finding(task, finding)
    request = {
        "stage": "patch",
        "feedback": "verification_failed" if feedback else None,
        "system": load_profiles().system_for("patch"),
        "task": task.model_dump(mode="json"),
        "finding": finding.model_dump(mode="json"),
        "files": {ref.path: context.read_file(ref.path) for ref in finding.evidence},
        "response_schema": PatchProposal.model_json_schema(),
    }
    if sum(len(value.encode()) for value in request["files"].values()) > 131072:
        raise RepairError("Patch context budget exceeded")
    try:
        output = backend.complete(request)
        if not isinstance(output, str) or len(output.encode()) > 131072:
            raise ValueError()
        proposal = PatchProposal.model_validate_json(output)
    except Exception:
        raise RepairError("Patch backend failed or returned invalid output") from None
    return validate_patch(context, task, finding, proposal)

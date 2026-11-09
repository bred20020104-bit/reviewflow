"""Exact-edit model proposals, translated into the existing checked patch contract.

No writes, tests, subprocesses or grants here. The trusted caller provides a frozen
profile bundle and an evidence-checking gateway; the response remains untrusted.
"""

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import Field

from reviewflow.backend.diagnostics import SafeTransportError, safe_error_code
from reviewflow.backend.repair import (
    CheckedPatch,
    PatchProposal,
    RepairError,
    Replacement,
    editable,
    validate_patch,
)
from reviewflow.backend.transport import TransportError
from reviewflow.errors import CollectionError
from reviewflow.review.context import MAX_EXCERPT_LINES
from reviewflow.review.contracts import Digest, EvidenceRef, Identifier, RelativePath, ReviewMessage
from reviewflow.review.workflow_models import ModelRequest


class ExactEdit(ReviewMessage):
    path: RelativePath
    before_sha256: Digest
    old_text: Annotated[str, Field(min_length=1, max_length=32768)]
    new_text: Annotated[str, Field(max_length=32768)]


class EditProposal(ReviewMessage):
    snapshot_id: Digest
    finding_id: Identifier
    edits: Annotated[tuple[ExactEdit, ...], Field(min_length=1, max_length=20)]
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]


def live_editable(path):
    name = PurePosixPath(path.lower()).name
    return (
        editable(path)
        and name not in {"config.py", "settings.py", "wsgi.py", "asgi.py", "__main__.py"}
        and not name.endswith(("_config.py", "_settings.py"))
    )


def translate_edits(context, task, finding, proposal) -> CheckedPatch:
    try:
        proposal = EditProposal.model_validate(proposal)
        context.validate_finding(task, finding)
        if proposal.snapshot_id != task.snapshot_id or proposal.finding_id != finding.finding_id:
            raise ValueError()
        cited = {ref.path for ref in finding.evidence}
        grouped = {}
        for edit in proposal.edits:
            if edit.path not in cited or not live_editable(edit.path):
                raise ValueError()
            if edit.old_text == edit.new_text or "\0" in edit.new_text:
                raise ValueError()
            grouped.setdefault(edit.path, []).append(edit)
        if len(grouped) > 5:
            raise ValueError()
        replacements = []
        for path, edits in grouped.items():
            before = context.read_file(path)
            digest = hashlib.sha256(before.encode("utf-8")).hexdigest()
            ranges = []
            for edit in edits:
                if edit.before_sha256 != digest:
                    raise ValueError()
                start = before.find(edit.old_text)
                if start < 0 or before.find(edit.old_text, start + 1) >= 0:
                    raise ValueError()
                ranges.append((start, start + len(edit.old_text), edit.new_text))
            ranges.sort()
            if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
                raise ValueError()
            after = before
            for start, end, replacement in reversed(ranges):
                after = after[:start] + replacement + after[end:]
            replacements.append(Replacement(path=path, before_sha256=digest, content=after))
        full = PatchProposal(
            snapshot_id=proposal.snapshot_id,
            finding_id=proposal.finding_id,
            replacements=tuple(replacements),
            explanation=proposal.explanation,
        )
        return validate_patch(context, task, finding, full)
    except (ValueError, CollectionError):
        raise RepairError("Invalid exact-edit patch or evidence") from None


def propose_live_patch(context, task, finding, gateway, profiles, *, feedback=None) -> CheckedPatch:
    try:
        context.validate_finding(task, finding)
        refs = {ref.path: ref for ref in finding.evidence}
        packets, total = [], 0
        for path, ref in refs.items():
            text = context.read_file(path)
            total += len(text.encode("utf-8"))
            if total > 32768:
                raise ValueError()
            lines = text.split("\n")
            if lines[-1] == "":
                lines.pop()
            for start in range(1, len(lines) + 1, MAX_EXCERPT_LINES):
                end = min(start + MAX_EXCERPT_LINES - 1, len(lines))
                current = EvidenceRef(
                    snapshot_id=ref.snapshot_id, path=path,
                    content_sha256=ref.content_sha256, start_line=start, end_line=end,
                )
                packets.append({
                    "evidence": current.model_dump(mode="json"),
                    "text": context.read_lines(path, start, end),
                })
        request = ModelRequest(
            "patch", profiles.system_for("patch"),
            json.dumps({
                "task": task.model_dump(mode="json"),
                "finding": finding.model_dump(mode="json"),
                "files": packets,
                "feedback": "verification_failed" if feedback else None,
            }, ensure_ascii=True),
            EditProposal.model_json_schema(),
        )
    except (ValueError, CollectionError):
        raise RepairError("Invalid or excessive live patch context") from None
    try:
        output = gateway.complete(request)
        if not isinstance(output, str) or len(output.encode("utf-8")) > 131072:
            raise ValueError()
        proposal = EditProposal.model_validate_json(output)
    except TransportError as exc:
        raise SafeTransportError(safe_error_code(exc)) from None
    except Exception:
        raise RepairError("Patch backend failed or returned invalid output") from None
    return translate_edits(context, task, finding, proposal)

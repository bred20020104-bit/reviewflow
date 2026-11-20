"""Untrusted Patch Agent output becomes a bounded, provenance-checked replacement."""

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import Field

from reviewflow.errors import CollectionError
from reviewflow.review.contracts import Digest, Identifier, RelativePath, ReviewMessage


class RepairError(ValueError):
    pass


class PartialApplicationError(RepairError):
    """Owned writes could not be safely restored; preserve and stop the workspace."""


class Replacement(ReviewMessage):
    path: RelativePath
    before_sha256: Digest
    content: Annotated[str, Field(max_length=65536)]


class PatchProposal(ReviewMessage):
    snapshot_id: Digest
    finding_id: Identifier
    replacements: Annotated[tuple[Replacement, ...], Field(min_length=1, max_length=5)]
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]


@dataclass(frozen=True)
class CheckedPatch:
    proposal: PatchProposal
    sha256: str
    diff: str


def editable(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    source = name.endswith(".py") or (name.endswith(".java") and not name.endswith(("test.java", "tests.java")))
    return (
        source
        and not any(p.startswith(".") for p in parts)
        and not any(p in {"test", "tests", "testing", "migrations"} for p in parts[:-1])
        and not name.startswith("test_")
        and not name.endswith("_test.py")
        and name not in {"conftest.py", "setup.py", "sitecustomize.py", "usercustomize.py"}
    )


def validate_patch(context, task, finding, proposal) -> CheckedPatch:
    try:
        proposal = PatchProposal.model_validate(proposal)
        context.validate_finding(task, finding)
        if proposal.snapshot_id != finding.evidence[0].snapshot_id:
            raise RepairError("Patch snapshot mismatch")
        if proposal.finding_id != finding.finding_id:
            raise RepairError("Patch finding mismatch")
        paths = [r.path for r in proposal.replacements]
        if len(set(paths)) != len(paths) or not set(paths) <= {e.path for e in finding.evidence}:
            raise RepairError("Patch scope mismatch")
        total, diffs = 0, []
        for replacement in proposal.replacements:
            if not editable(replacement.path):
                raise RepairError("Patch path protected")
            before = context.read_file(replacement.path)
            if hashlib.sha256(before.encode()).hexdigest() != replacement.before_sha256:
                raise RepairError("Patch base changed")
            after = replacement.content
            size = len(after.encode("utf-8"))
            total += size
            if size > 65536 or total > 131072 or "\0" in after or after == before:
                raise RepairError("Invalid replacement size, encoding or no-op")
            diffs.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile="a/" + replacement.path,
                    tofile="b/" + replacement.path,
                )
            )
        digest = hashlib.sha256(proposal.model_dump_json().encode()).hexdigest()
        return CheckedPatch(proposal, digest, "".join(diffs))
    except (ValueError, CollectionError) as exc:
        if isinstance(exc, RepairError):
            raise
        raise RepairError("Invalid patch structure or evidence") from None

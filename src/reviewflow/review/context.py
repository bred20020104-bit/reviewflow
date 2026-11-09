"""Deterministic scope and provenance checks, not semantic verification."""

from hashlib import sha256
from typing import TypeVar

from pydantic import ValidationError

from reviewflow.errors import CollectionError
from reviewflow.models import Inventory, SourceInfo, SourceReader
from reviewflow.review.contracts import (
    EvidenceRef,
    Finding,
    ReviewMessage,
    ReviewTask,
    VerificationResult,
)
from reviewflow.review.evidence_errors import EvidenceValidationError
from reviewflow.snapshot import compute_snapshot_id

MAX_EXCERPT_LINES = 200
MAX_EXCERPT_BYTES = 32768
Message = TypeVar("Message", bound=ReviewMessage)


def _validated(cls: type[Message], value: Message) -> Message:
    try:
        return cls.model_validate(value)
    except ValidationError:
        # Pydantic diagnostics can contain arbitrary model-supplied text.
        raise CollectionError("Invalid review message structure.") from None


def _identity(info: SourceInfo) -> tuple:
    return info.mode, info.root, info.commit


class ReviewContext:
    def __init__(self, source: SourceReader, inventory: Inventory):
        snapshot = inventory.snapshot
        self._identity = _identity(snapshot.source)
        if _identity(source.info) != self._identity:
            raise CollectionError("Source identity does not match snapshot.")
        if (
            snapshot.schema_version != 1
            or compute_snapshot_id(snapshot.source, snapshot.policy, snapshot.records)
            != snapshot.snapshot_id
        ):
            raise CollectionError("Invalid inventory snapshot digest.")
        self._source = source
        self._snapshot_id = snapshot.snapshot_id
        self._max_bytes = snapshot.policy.max_file_bytes
        self._records = {}
        seen = set()
        for record in snapshot.records:
            if record.path in seen:
                raise CollectionError("Duplicate inventory path.")
            seen.add(record.path)
            if record.status == "included":
                if (
                    type(record.size_bytes) is not int
                    or not 0 <= record.size_bytes <= self._max_bytes
                    or record.sha256 is None
                ):
                    raise CollectionError("Invalid included file metadata.")
                # Validate paths and hashes even for manually assembled inventories.
                _validated(
                    EvidenceRef,
                    {
                        "snapshot_id": self._snapshot_id,
                        "path": record.path,
                        "content_sha256": record.sha256,
                        "start_line": 1,
                        "end_line": 1,
                    },
                )
                self._records[record.path] = record
        self._entries = {}
        for entry in source.entries():
            if entry.path in self._records:
                record = self._records[entry.path]
                if (
                    entry.path in self._entries
                    or entry.entry_type != "file"
                    or entry.size_bytes != record.size_bytes
                ):
                    raise CollectionError("Source entries do not match inventory.")
                self._entries[entry.path] = entry
        if self._entries.keys() != self._records.keys():
            raise CollectionError("Included file missing from source.")

    def _record(self, path: str):
        if type(path) is not str or path not in self._records:
            raise EvidenceValidationError("evidence_path_outside_scope")
        return self._records[path]

    def read_file(self, path: str) -> str:
        """Read one full file within the original inventory's per-file limit."""
        record = self._record(path)
        if _identity(self._source.info) != self._identity:
            raise EvidenceValidationError("source_changed")
        try:
            data = self._source.read_bytes(self._entries[path], max_bytes=self._max_bytes)
        except CollectionError:
            raise EvidenceValidationError("source_changed") from None
        if len(data) != record.size_bytes or sha256(data).hexdigest() != record.sha256:
            raise EvidenceValidationError("source_changed")
        if b"\0" in data:
            raise CollectionError("Evidence content contains NUL.")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CollectionError("Evidence content is not UTF-8.") from None
        return text

    def read_lines(self, path: str, start_line: int, end_line: int) -> str:
        self._record(path)
        if (
            type(start_line) is not int
            or type(end_line) is not int
            or start_line < 1
            or end_line < start_line
            or end_line - start_line + 1 > MAX_EXCERPT_LINES
        ):
            raise EvidenceValidationError("evidence_line_range_invalid")
        text = self.read_file(path)
        # splitlines would treat CR, VT and Unicode separators as extra lines.
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        if end_line > len(lines):
            raise EvidenceValidationError("evidence_line_range_invalid")
        excerpt = "\n".join(lines[start_line - 1 : end_line])
        if end_line < len(lines) or text.endswith("\n"):
            excerpt += "\n"
        if len(excerpt.encode("utf-8")) > MAX_EXCERPT_BYTES:
            raise CollectionError("Evidence excerpt exceeds byte limit.")
        return excerpt

    def validate_task(self, task: ReviewTask) -> None:
        task = _validated(ReviewTask, task)
        if task.snapshot_id != self._snapshot_id:
            raise CollectionError("Task snapshot mismatch.")
        for path in task.paths:
            self._record(path)

    def _validate_evidence(self, ref: EvidenceRef) -> None:
        record = self._record(ref.path)
        if ref.snapshot_id != self._snapshot_id:
            raise EvidenceValidationError("evidence_snapshot_mismatch")
        if ref.content_sha256 != record.sha256:
            raise EvidenceValidationError("evidence_hash_mismatch")
        self.read_lines(ref.path, ref.start_line, ref.end_line)

    def validate_finding(self, task: ReviewTask, finding: Finding) -> None:
        task = _validated(ReviewTask, task)
        self.validate_task(task)
        finding = _validated(Finding, finding)
        if finding.task_id != task.task_id:
            raise CollectionError("Finding task mismatch.")
        for ref in finding.evidence:
            if ref.path not in task.paths:
                raise CollectionError("Finding evidence is outside task scope.")
            self._validate_evidence(ref)

    def validate_verification(
        self, task: ReviewTask, finding: Finding, result: VerificationResult
    ) -> None:
        finding = _validated(Finding, finding)
        self.validate_finding(task, finding)
        result = _validated(VerificationResult, result)
        if result.finding_id != finding.finding_id:
            raise EvidenceValidationError("verification_finding_mismatch")
        for ref in result.evidence:
            self._validate_evidence(ref)

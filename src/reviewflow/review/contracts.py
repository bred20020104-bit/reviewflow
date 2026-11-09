"""Bounded, immutable review messages. Structure alone does not prove evidence."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must contain a non-whitespace character")
    return value


def _relative_path(value: str) -> str:
    if "\0" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Expected a canonical relative POSIX path")
    return value


def _unique(values: tuple) -> tuple:
    if len(set(values)) != len(values):
        raise ValueError("Duplicate entries are not allowed")
    return values


Identifier = Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")]
RelativePath = Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(_relative_path)]
ShortText = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_nonblank)]
LongText = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_nonblank)]


class ReviewMessage(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", revalidate_instances="always"
    )


class ReviewTask(ReviewMessage):
    task_id: Identifier
    snapshot_id: Digest
    role: Literal["correctness", "security", "general"]
    paths: Annotated[
        tuple[RelativePath, ...], Field(min_length=1, max_length=20), AfterValidator(_unique)
    ]
    objective: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_nonblank)]


class EvidenceRef(ReviewMessage):
    snapshot_id: Digest
    path: RelativePath
    content_sha256: Digest
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("End line precedes start line")
        return self


Evidence = Annotated[tuple[EvidenceRef, ...], Field(max_length=20), AfterValidator(_unique)]


class Finding(ReviewMessage):
    finding_id: Identifier
    task_id: Identifier
    title: ShortText
    root_cause: LongText
    trigger: LongText
    impact: LongText
    severity: Literal["critical", "high", "medium", "low"]
    evidence: Annotated[Evidence, Field(min_length=1)]


class VerificationResult(ReviewMessage):
    finding_id: Identifier
    verdict: Literal["accepted", "rejected", "unresolved"]
    rationale: LongText
    evidence: Evidence

    @model_validator(mode="after")
    def decisive_evidence(self) -> Self:
        if self.verdict != "unresolved" and not self.evidence:
            raise ValueError("A decisive verdict requires evidence")
        return self

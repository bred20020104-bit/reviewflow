"""Bounded workflow messages; runtime facts are never supplied by a model."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self

from pydantic import AfterValidator, Field, model_validator

from reviewflow.review.contracts import (
    EvidenceRef,
    Finding,
    RelativePath,
    ReviewMessage,
    ReviewTask,
    VerificationResult,
    _unique,
)


class Plan(ReviewMessage):
    tasks: Annotated[tuple[ReviewTask, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        _unique(tuple(t.task_id for t in self.tasks))
        return self


class ReviewOutput(ReviewMessage):
    findings: Annotated[tuple[Finding, ...], Field(max_length=20)]
    reviewed_paths: Annotated[
        tuple[RelativePath, ...], Field(max_length=20), AfterValidator(_unique)
    ]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        _unique(tuple(f.finding_id for f in self.findings))
        return self


class RunLimits(ReviewMessage):
    max_calls: Annotated[int, Field(ge=1, le=100)] = 12
    max_tasks: Annotated[int, Field(ge=1, le=8)] = 8
    max_findings: Annotated[int, Field(ge=1, le=100)] = 40
    max_context_files: Annotated[int, Field(ge=1, le=20)] = 20
    max_context_bytes: Annotated[int, Field(ge=1, le=1048576)] = 131072


@dataclass(frozen=True)
class CodePacket:
    evidence: EvidenceRef
    text: str


@dataclass(frozen=True)
class ModelRequest:
    stage: Literal["planner", "reviewer", "verifier", "patch"]
    system: str
    payload: str
    response_schema: dict


class ReviewBackend(Protocol):
    """Trusted local implementation. No dynamic plugin loading or live adapter yet."""

    def complete(self, request: ModelRequest) -> str: ...


@dataclass(frozen=True)
class TaskOutcome:
    task: ReviewTask
    status: str
    reviewed_paths: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class FindingOutcome:
    finding: Finding
    role: str
    verification: VerificationResult | None = None
    error: str | None = None
    # Independent verifier inventory, not reviewer coverage or editable paths.
    verification_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunReport:
    schema_version: int
    snapshot_id: str
    mode: str
    execution: str
    status: str
    calls: int
    limits: RunLimits
    included_paths: tuple[str, ...]
    supplied_paths: tuple[str, ...]
    reported_reviewed_paths: tuple[str, ...]
    unreviewed_paths: tuple[str, ...]
    omissions: tuple[tuple[str, str], ...]
    tasks: tuple[TaskOutcome, ...]
    findings: tuple[FindingOutcome, ...]
    errors: tuple[str, ...]
    inventory_warnings: tuple[str, ...]
    semantic_review_performed: bool = False
    target_tests_executed: bool = False

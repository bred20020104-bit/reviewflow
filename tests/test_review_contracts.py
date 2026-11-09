import pytest
from pydantic import ValidationError
from review_helpers import evidence, finding, task

from reviewflow.review.contracts import EvidenceRef, Finding, ReviewTask, VerificationResult


@pytest.mark.parametrize(
    "cls,payload",
    [
        (ReviewTask, task()),
        (EvidenceRef, evidence()),
        (Finding, finding()),
        (
            VerificationResult,
            {
                "finding_id": "finding-1",
                "verdict": "accepted",
                "rationale": "Checked code",
                "evidence": (evidence(),),
            },
        ),
    ],
)
def test_strict_schema_roundtrip_and_extra_fields(cls, payload):
    model = cls.model_validate(payload)
    assert cls.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        cls.model_validate({**payload, "tests_passed": True})
    with pytest.raises(ValidationError):
        model.extra = "no"


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../a.py", "src/../a.py", "./a.py", "a//b.py", "a/", "a\0.py", "a" * 4097],
)
def test_invalid_evidence_paths(path):
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(evidence(path=path))


def test_unicode_newline_paths_and_tuple_immutability():
    path = "模块/new\nfile.py"
    model = ReviewTask.model_validate(task(paths=(path,)))
    assert model.paths == (path,)
    assert EvidenceRef.model_validate(evidence(path=path)).path == path
    with pytest.raises(ValidationError):
        ReviewTask.model_validate(task(paths=[path]))


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "x" * 65},
        {"task_id": "bad id"},
        {"snapshot_id": "a"},
        {"role": "patch"},
        {"paths": ()},
        {"paths": ("src/main.py", "src/main.py")},
        {"paths": tuple(f"a{i}.py" for i in range(21))},
        {"objective": " "},
        {"objective": "x" * 2001},
    ],
)
def test_invalid_task_fields(changes):
    with pytest.raises(ValidationError):
        ReviewTask.model_validate(task(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"start_line": 0},
        {"start_line": True},
        {"start_line": "1"},
        {"start_line": 3, "end_line": 2},
        {"content_sha256": "G" * 64},
    ],
)
def test_invalid_evidence_fields(changes):
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(evidence(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence": ()},
        {"evidence": (evidence(), evidence())},
        {"severity": "urgent"},
        {"title": " "},
        {"trigger": "x" * 4001},
        {"evidence": tuple(evidence(start_line=i, end_line=i) for i in range(1, 22))},
    ],
)
def test_invalid_findings(changes):
    with pytest.raises(ValidationError):
        Finding.model_validate(finding(**changes))


@pytest.mark.parametrize("verdict", ["accepted", "rejected"])
def test_decisive_verdict_requires_evidence(verdict):
    with pytest.raises(ValidationError):
        VerificationResult(
            finding_id="finding-1", verdict=verdict, rationale="No proof", evidence=()
        )


def test_unresolved_can_have_no_evidence():
    result = VerificationResult(
        finding_id="finding-1", verdict="unresolved", rationale="Missing context", evidence=()
    )
    assert result.evidence == ()


def test_modified_instance_is_revalidated():
    forged = ReviewTask.model_validate(task()).model_copy(update={"paths": ("../secret",)})
    with pytest.raises(ValidationError):
        ReviewTask.model_validate(forged)

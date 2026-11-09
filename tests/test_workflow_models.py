import pytest
from pydantic import ValidationError
from review_helpers import task

from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.review.workflow_models import Plan, ReviewOutput, RunLimits
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def test_read_file_preserves_full_verified_content(tmp_path):
    content = "a\r\n" * 250
    (tmp_path / "a.py").write_bytes(content.encode())
    source = DirectorySource(tmp_path)
    ctx = ReviewContext(source, build_inventory(source, ScanPolicy()))
    assert ctx.read_file("a.py") == content
    (tmp_path / "a.py").write_text("changed")
    with pytest.raises(Exception, match="changed"):
        ctx.read_file("a.py")


@pytest.mark.parametrize(
    "payload",
    [
        {"tasks": []},
        {"tasks": [task(), task()]},
        {"tasks": [task(task_id=f"t{i}") for i in range(9)]},
    ],
)
def test_invalid_plans(payload):
    import json

    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(payload))


def test_plan_json_roundtrip():
    model = Plan.model_validate({"tasks": (task(),)})
    assert Plan.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    "payload",
    [
        {"findings": (), "reviewed_paths": ("a.py", "a.py")},
        {"findings": (), "reviewed_paths": ("../a.py",)},
        {"findings": (), "reviewed_paths": (), "tests_passed": True},
    ],
)
def test_bad_review_output(payload):
    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(payload)


@pytest.mark.parametrize(
    "payload", [{"max_calls": 0}, {"max_tasks": True}, {"max_context_bytes": "1"}, {"max_tasks": 9}]
)
def test_invalid_limits(payload):
    with pytest.raises(ValidationError):
        RunLimits(**payload)

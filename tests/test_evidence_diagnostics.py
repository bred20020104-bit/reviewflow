"""Fixed diagnostics must preserve rejection, without exposing arbitrary values."""
import pytest
from test_review_context import messages, setup_context

from reviewflow.errors import CollectionError
from reviewflow.review.contracts import VerificationResult


@pytest.mark.parametrize("field,value,code", [
    ("finding_id", "wrong", "verification_finding_mismatch"),
    ("snapshot_id", "0" * 64, "evidence_snapshot_mismatch"),
    ("content_sha256", "0" * 64, "evidence_hash_mismatch"),
    ("end_line", 4, "evidence_line_range_invalid"),
    ("path", "SECRET-not-in-scope.py", "evidence_path_outside_scope"),
])
def test_invalid_verification_has_fixed_diagnostic(tmp_path, field, value, code):
    _, inventory, context = setup_context(tmp_path)
    task, finding = messages(inventory)
    result = VerificationResult(finding_id=finding.finding_id, verdict="accepted",
                                rationale="Fixture", evidence=finding.evidence)
    if field == "finding_id":
        result = result.model_copy(update={field: value})
    else:
        result = result.model_copy(update={"evidence": (
            result.evidence[0].model_copy(update={field: value}),)})
    with pytest.raises(CollectionError) as caught:
        context.validate_verification(task, finding, result)
    assert getattr(caught.value, "code", None) == code
    assert "SECRET" not in str(caught.value)


def test_source_mutation_is_not_mislabeled_as_bad_model_hash(tmp_path):
    _, inventory, context = setup_context(tmp_path)
    task, finding = messages(inventory)
    (tmp_path / "main.py").write_text("SECRET changed source")
    result = VerificationResult(finding_id=finding.finding_id, verdict="accepted",
                                rationale="Fixture", evidence=finding.evidence)
    with pytest.raises(CollectionError) as caught:
        context.validate_verification(task, finding, result)
    assert getattr(caught.value, "code", None) == "source_changed"
    assert "SECRET" not in str(caught.value)

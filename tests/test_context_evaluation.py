import pytest

from reviewflow.backend.context_index import ContextIndex
from reviewflow.backend.evaluation import EvaluationError, score
from reviewflow.models import ScanPolicy
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def test_chunks_cover_long_crlf_file_with_real_evidence(tmp_path):
    from reviewflow.review.context import ReviewContext

    (tmp_path / "a.py").write_bytes(("value = 1\r\n" * 450).encode())
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    index = ContextIndex(source, inventory)
    assert [(p.evidence.start_line, p.evidence.end_line) for p in index.packets] == [
        (1, 200),
        (201, 400),
        (401, 450),
    ]
    context = ReviewContext(source, inventory)
    for packet in index.packets:
        assert (
            context.read_lines("a.py", packet.evidence.start_line, packet.evidence.end_line)
            == packet.text
        )
    assert index.omissions == ()
    assert len(index.search("value", limit=2)) == 2


def test_chunk_budget_and_long_single_line_report_omissions(tmp_path):
    (tmp_path / "a.py").write_text("x\n" * 450)
    (tmp_path / "b.py").write_text("x" * 40000)
    source = DirectorySource(tmp_path)
    index = ContextIndex(source, build_inventory(source, ScanPolicy()), max_bytes=500)
    assert len(index.packets) == 1
    assert ("a.py", "context_budget") in index.omissions
    assert ("b.py", "line_too_large") in index.omissions
    with pytest.raises(ValueError):
        index.search("", limit=2)
    (tmp_path / "a.py").write_text("changed")
    with pytest.raises(Exception, match="changed"):
        index.search("xxx")


def dataset():
    return {
        "cases": [
            {"case_id": "bug", "group": "g1", "split": "test", "truth_ids": ["off-by-one"]},
            {"case_id": "clean", "group": "g2", "split": "test", "truth_ids": []},
        ]
    }


def results():
    return {
        "execution": "real",
        "mode": "multi",
        "split": "test",
        "runs": [
            {
                "case_id": "bug",
                "candidates": [
                    {"finding_id": "f1", "truth_id": "off-by-one", "verdict": "accepted"},
                    {"finding_id": "f2", "truth_id": None, "verdict": "rejected"},
                ],
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": "0.01",
                "seconds": 2,
            },
            {
                "case_id": "clean",
                "candidates": [{"finding_id": "f3", "truth_id": None, "verdict": "accepted"}],
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": "0.01",
                "seconds": 2,
            },
        ],
    }


def test_human_matching_metrics_include_clean_cases_and_rejections():
    report = score(dataset(), results())
    assert report["precision"] == 0.5
    assert report["recall"] == 1.0
    assert report["f1"] == pytest.approx(2 / 3)
    assert report["clean_case_false_positive_rate"] == 1.0
    assert report["verifier_correct_rejections"] == 1
    assert report["cost_usd"] == "0.02"
    assert report["input_tokens"] == 200


def test_duplicate_root_causes_cannot_inflate_recall():
    data = results()
    data["runs"][0]["candidates"].append(
        {"finding_id": "duplicate", "truth_id": "off-by-one", "verdict": "accepted"}
    )
    report = score(dataset(), data)
    assert report["true_positives"] == 1
    assert report["duplicate_findings"] == 1
    assert report["precision"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    "mutation", ["simulated", "missing", "unknown", "leak", "nan", "fake-match"]
)
def test_evaluation_rejects_invalid_or_misleading_experiments(mutation):
    manifest, data = dataset(), results()
    if mutation == "simulated":
        data["execution"] = "simulated"
    elif mutation == "missing":
        data["runs"].pop()
    elif mutation == "unknown":
        data["runs"][0]["case_id"] = "other"
    elif mutation == "leak":
        manifest["cases"].append(
            {"case_id": "variant", "group": "g1", "split": "dev", "truth_ids": []}
        )
    elif mutation == "nan":
        data["runs"][0]["seconds"] = float("nan")
    else:
        data["runs"][0]["candidates"][0]["truth_id"] = "made-up"
    with pytest.raises(EvaluationError):
        score(manifest, data)


def test_ablation_runs_reviewers_without_verifier(tmp_path):
    from reviewflow.review.demo import DemoBackend
    from reviewflow.review.workflow import run_review

    (tmp_path / "a.py").write_text("# REVIEWFLOW_DEMO_FINDING\nx = 1\n")
    source = DirectorySource(tmp_path)
    report = run_review(
        source, build_inventory(source, ScanPolicy()), DemoBackend(), mode="multi-no-verifier"
    )
    assert report.calls == 3
    assert report.status == "complete"
    assert report.findings[0].verification is None
    assert report.execution == "simulated"

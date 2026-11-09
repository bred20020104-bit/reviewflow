import json
from pathlib import Path

import pytest

from reviewflow.backend.benchmark import run_benchmark
from reviewflow.backend.evaluation import EvaluationError, score
from reviewflow.quick import main


def test_offline_benchmark_runs_all_cases_without_sending_answers():
    report = run_benchmark(Path("benchmarks/corpus"), split="dev", mode="single")
    assert report["execution"] == "simulated"
    assert len(report["runs"]) == 10
    assert report["total_calls"] == 10
    assert report["semantic_review_performed"] is False
    assert all(run["status"] == "complete" for run in report["runs"])
    assert not any("truth_ids" in run for run in report["runs"])
    manifest = json.loads(Path("benchmarks/corpus/manifest.json").read_text())
    with pytest.raises(EvaluationError):
        score(manifest, report)


def test_benchmark_cli_reports_simulated_ablation(capsys):
    assert (
        main(
            [
                "benchmark-demo",
                "--corpus",
                "benchmarks/corpus",
                "--split",
                "dev",
                "--mode",
                "multi-no-verifier",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["total_calls"] == 30
    assert result["network_calls"] == 0

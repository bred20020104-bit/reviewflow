from reviewflow.backend.live_repair_cli import _markdown


def document(record):
    return {"status": "blocked", "accounting": {"requests_attempted": 1, "records": [record]},
            "preview": {"max_calls": 48}, "report_directory": "report",
            "candidate_path": "candidate", "test_runs": []}


def test_markdown_exposes_fixed_timeout_stage_and_time():
    result = _markdown(document({"error_code": "worker_timeout", "transport_progress": {
        "version": 1, "events": [
            {"stage": "worker_started", "elapsed_ms": 0},
            {"stage": "connection_established", "elapsed_ms": 80},
            {"stage": "request_sent", "elapsed_ms": 90}]}}))
    assert "worker_timeout" in result
    assert "request_sent" in result
    assert "90 ms" in result


def test_markdown_drops_arbitrary_progress_and_error_text():
    result = _markdown(document({"error_code": "synthetic-private-key", "transport_progress": {
        "version": 1, "events": [{"stage": "synthetic-private-key", "elapsed_ms": 0}]}}))
    assert "synthetic-private-key" not in result

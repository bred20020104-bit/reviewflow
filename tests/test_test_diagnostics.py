"""Structured diagnostics must never expose target-controlled free text."""

import json

import pytest
from test_test_projects import run_projects
from test_volume_runner import approved_run, process, volume_runner


def envelope(**updates):
    value = {"version": 1, "failures": [{"project_index": 0, "file_index": 0,
        "line": 1, "test_line": 1, "phase": "call", "category": "assertion_error"}],
        "truncated": False, "incomplete": False}
    value.update(updates)
    return json.dumps(value).encode()


@pytest.mark.parametrize("body,code,phase,category", [
    ("def test_bad(): assert False\n", 1, "call", "assertion_error"),
    ("def test_bad(): raise PermissionError('SECRET_VALUE')\n", 1, "call", "permission_error"),
    ("def test_bad(): raise ConnectionError('SECRET_VALUE')\n", 1, "call", "connection_error"),
    ("raise ImportError('SECRET_VALUE')\n", 20, "collection", "collection_error"),
    ("import pytest\n@pytest.fixture\ndef fixture(): raise RuntimeError('SECRET_VALUE')\n"
     "def test_bad(fixture): pass\n", 21, "setup", "runtime_error"),
    ("import pytest\n@pytest.fixture\ndef fixture():\n    yield\n    raise RuntimeError('SECRET_VALUE')\n"
     "def test_bad(fixture): pass\n", 22, "teardown", "runtime_error"),
])
def test_real_wrapper_retains_safe_failure_locations(tmp_path, body, code, phase, category):
    files = {"pkg/tests/test_bad.py": body}
    result = run_projects(tmp_path, files)
    assert result.returncode == code
    assert result.stdout, "Structured diagnostics were discarded"
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert state == "complete"
    failure, = failures
    assert failure.path == "pkg/tests/test_bad.py"
    assert (failure.phase, failure.category) == (phase, category)
    assert failure.line >= 1
    assert b"SECRET" not in result.stdout + result.stderr


def test_parameter_ids_messages_assert_values_and_logs_are_not_exposed(tmp_path):
    files = {"tests/test_secret.py": "import pytest\n"
        "@pytest.mark.parametrize('value', ['SECRET_VALUE'], ids=['SECRET_PARAM'])\n"
        "def test_bad(value):\n    print('SECRET_STDOUT' * 10000)\n    assert value == 'other'\n"}
    result = run_projects(tmp_path, files)
    assert result.returncode == 1
    assert result.stdout, "Missing safe diagnostic channel"
    assert b"SECRET" not in result.stdout + result.stderr
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert state == "complete" and len(failures) == 1
    assert failures[0].test_line == 3


def test_runner_attaches_host_validated_diagnostics_without_changing_verdict():
    def execute(command, **kwargs):
        if command[1] == "run":
            output = kwargs["stdout"]
            assert hasattr(output, "write"), "No bounded structured output channel"
            output.write(envelope())
            return process(1)
        return process()
    result = approved_run(volume_runner(execute=execute))
    assert result.status == "failed" and result.exit_code == 1
    assert result.diagnostics_status == "complete"
    assert result.test_failures[0].path == "pkg/tests/test_ok.py"
    assert result.test_failures[0].category == "assertion_error"


@pytest.mark.parametrize("change", ["extra", "path", "line", "boolean", "category", "phase", "many"])
def test_invalid_diagnostics_never_publish_arbitrary_fields_or_claim_success(change):
    value = json.loads(envelope())
    failure = value["failures"][0]
    if change == "extra":
        failure["message"] = "SECRET"
    elif change == "path":
        failure["file_index"] = 999
    elif change == "line":
        failure["line"] = 999
    elif change == "boolean":
        failure["test_line"] = True
    elif change == "category":
        failure["category"] = "SECRET"
    elif change == "phase":
        failure["phase"] = "SECRET"
    else:
        value["failures"] *= 51
    wire = json.dumps(value).encode()
    def execute(command, **kwargs):
        if command[1] == "run":
            assert hasattr(kwargs["stdout"], "write"), "Missing bounded output channel"
            kwargs["stdout"].write(wire)
            return process(1)
        return process()
    result = approved_run(volume_runner(execute=execute))
    assert result.status == "failed"
    assert result.diagnostics_status == ("partial" if change == "line" else "invalid")
    assert result.test_failures == ()
    assert "SECRET" not in repr(result)


def test_output_flood_is_drained_but_not_retained_or_reported():
    def execute(command, **kwargs):
        if command[1] == "run":
            assert hasattr(kwargs["stdout"], "write"), "Missing bounded output channel"
            kwargs["stdout"].write(b"SECRET" * 100000)
            return process(1)
        return process()
    result = approved_run(volume_runner(execute=execute))
    assert result.status == "failed"
    assert result.diagnostics_status == "oversized" and result.test_failures == ()
    assert "SECRET" not in repr(result)


def test_complete_diagnostics_cannot_override_cleanup_failure():
    def execute(command, **kwargs):
        if command[1] == "run":
            assert hasattr(kwargs["stdout"], "write"), "Missing bounded output channel"
            kwargs["stdout"].write(envelope(failures=[]))
        return process(1 if command[1:3] == ["volume", "rm"] else 0)
    result = approved_run(volume_runner(execute=execute))
    assert result.status == "cleanup_failed"
    assert result.diagnostics_status == "complete"


def test_many_failures_are_explicitly_truncated_not_reported_as_complete(tmp_path):
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    files = {"tests/test_many.py": "".join(f"def test_{i}(): assert False\n" for i in range(60))}
    result = run_projects(tmp_path, files)
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert result.returncode == 1
    assert len(failures) == 50 and state == "truncated"
    assert len(result.stdout) < 65536


def test_project_failure_locations_remain_distinct_and_missing_packets_are_partial(tmp_path):
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    files = {"a/tests/test_same.py": "import os\ndef test_bad(): os._exit(1)\n",
             "b/tests/test_same.py": "def test_bad(): assert False\n"}
    result = run_projects(tmp_path, files)
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert result.returncode == 1 and state == "partial"
    assert len(failures) == 1
    assert (failures[0].project, failures[0].path) == ("b", "b/tests/test_same.py")


@pytest.mark.parametrize("code,failures,state", [(1, [], "partial"), (0, None, "invalid")])
def test_exit_code_and_diagnostic_conflicts_cannot_support_comparison(code, failures, state):
    def execute(command, **kwargs):
        if command[1] == "run":
            kwargs["stdout"].write(envelope() if failures is None else envelope(failures=failures))
            return process(code)
        return process()
    result = approved_run(volume_runner(execute=execute))
    assert result.exit_code == code
    assert result.status == ("passed" if code == 0 else "failed")
    assert result.diagnostics_status == state
    assert result.test_failures == ()


def test_inherited_definition_uses_its_own_file_and_preserves_direct_failures(tmp_path):
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    files = {
        "tests/test_base.py": "# fixture\n\n\nclass Base:\n    def test_inherited(self): assert False\n",
        "tests/test_child.py": "from test_base import Base\nclass TestChild(Base):\n    pass\ndef test_direct(): assert False\n",
    }
    result = run_projects(tmp_path, files)
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert result.returncode == 1
    assert state == "complete"
    assert {(f.path, f.test_line) for f in failures} == {
        ("tests/test_base.py", 5), ("tests/test_child.py", 4)}


def test_imported_non_test_definition_is_partial_without_losing_direct_failure(tmp_path):
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    files = {
        "helper.py": "# fixture\n\n\n\ndef test_imported(): assert False\n",
        "tests/test_child.py": "from helper import test_imported\ndef test_direct(): assert False\n",
    }
    result = run_projects(tmp_path, files)
    failures, state = parse_diagnostics(result.stdout, {p: t.encode() for p, t in files.items()})
    assert result.returncode == 1 and state == "partial"
    assert [(f.path, f.test_line) for f in failures] == [("tests/test_child.py", 2)]


def test_unsupported_location_is_partial_without_poisoning_valid_records():
    from reviewflow.backend.test_diagnostics import parse_diagnostics
    value = json.loads(envelope())
    value["failures"].append({**value["failures"][0], "line": 999, "test_line": 999})
    failures, state = parse_diagnostics(json.dumps(value).encode(),
                                       {"test_one.py": b"def test_one(): assert False\n"})
    assert state == "partial" and len(failures) == 1
    assert (failures[0].path, failures[0].test_line) == ("test_one.py", 1)

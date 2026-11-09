"""Host-validated, finite test diagnostics. No target-controlled error text."""

import ast
import json
from dataclasses import dataclass

# Keep wire limits/version explicit in approval previews.
DIAGNOSTIC_POLICY = {"version": 1, "max_bytes": 65536, "max_failures": 50,
                     "raw_logs": "discarded", "parameters": "omitted"}
PHASES = frozenset({"collection", "setup", "call", "teardown"})
CATEGORIES = frozenset({"assertion_error", "import_error", "permission_error", "connection_error",
                        "timeout_error", "os_error", "runtime_error", "other_error", "collection_error"})
DIAGNOSTIC_STATES = frozenset({"complete", "truncated", "partial", "missing", "invalid",
                             "oversized", "not_collected"})
TEST_CODES = frozenset({"tests_passed", "test_assertions_failed", "no_tests_executed",
    "test_collection_error", "test_setup_error", "test_teardown_error", "test_internal_error",
    "test_usage_error", "container_execution_error", "sandbox_preparation_error",
    "sandbox_timeout", "sandbox_cleanup_failed"})


@dataclass(frozen=True)
class TestFailure:
    __test__ = False
    project: str
    path: str
    test_line: int | None
    line: int
    phase: str
    category: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate diagnostic field")
        result[key] = value
    return result


def parse_diagnostics(raw, files):
    """Map numeric references back to approved test source; discard invalid envelopes.

    Locations identify a test definition, not a parameter case. No arbitrary node
    IDs, exception strings or log excerpts cross this boundary. These diagnostics
    remain target-reported observations, not independent proof of business defects.
    """
    from reviewflow.backend.test_projects import test_projects

    if not raw:
        return (), "missing"
    if len(raw) > DIAGNOSTIC_POLICY["max_bytes"]:
        return (), "oversized"
    try:
        data = json.loads(raw, object_pairs_hook=_object)
        if (type(data) is not dict or set(data) != {"version", "failures", "truncated", "incomplete"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["truncated"]) is not bool or type(data["incomplete"]) is not bool
                or type(data["failures"]) is not list or len(data["failures"]) > 50):
            raise ValueError()
        projects = test_projects(files)
        results, definitions = [], {}
        incomplete = data["incomplete"]
        for item in data["failures"]:
            if (type(item) is not dict or set(item) != {
                    "project_index", "file_index", "line", "test_line", "phase", "category"}
                    or any(type(item[k]) is not int for k in ("project_index", "file_index", "line"))
                    or item["project_index"] < 0 or item["file_index"] < 0
                    or item["phase"] not in PHASES or item["category"] not in CATEGORIES):
                raise ValueError()
            project = projects[item["project_index"]]
            path = project["files"][item["file_index"]]
            source = files[path].decode("utf-8")
            count = max(1, len(source.splitlines()))
            test_line = item["test_line"]
            if test_line is not None and type(test_line) is not int:
                raise ValueError()
            if (not 1 <= item["line"] <= count
                    or (test_line is not None and not 1 <= test_line <= count)):
                incomplete = True
                continue
            if test_line is not None:
                if path not in definitions:
                    tree = ast.parse(source)
                    definitions[path] = {}
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            definitions[path][node.lineno] = node.lineno
                            if node.decorator_list:
                                definitions[path][min(d.lineno for d in node.decorator_list)] = node.lineno
                if test_line not in definitions[path]:
                    incomplete = True
                    continue
                test_line = definitions[path][test_line]
            elif item["phase"] != "collection":
                raise ValueError()
            record = TestFailure(project["root"], path, test_line, item["line"],
                                 item["phase"], item["category"])
            if record not in results:
                results.append(record)
        state = "partial" if incomplete else "truncated" if data["truncated"] else "complete"
        return tuple(results), state
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError, SyntaxError):
        return (), "invalid"

"""Static test layout plus fixed per-project subprocesses inside the sandbox.

Input paths come from the validated export, not target configuration or commands.
Raw pytest output is discarded. Exit codes are fixed diagnostics, not test counts.
"""

import inspect
from pathlib import PurePosixPath

from reviewflow.backend.bounded_output import BoundedOutput


def test_projects(files):
    groups = {}
    for path in sorted(files):
        parsed = PurePosixPath(path)
        if not (parsed.name.endswith(".py") and
                (parsed.name.startswith("test_") or parsed.name.endswith("_test.py"))):
            continue
        parents = parsed.parts[:-1]
        boundaries = [i for i, name in enumerate(parents) if name in {"test", "tests"}]
        root = str(PurePosixPath(*parents[:boundaries[-1]])) if boundaries else str(parsed.parent)
        groups.setdefault(root, []).append(path)
    return [{"root": root, "files": paths} for root, paths in sorted(groups.items())]


_PYTEST_SCRIPT = '''import json, os, sys, pytest
config = json.load(sys.stdin)
root = config['root']
arguments = ['-q', '-p', 'no:cacheprovider', '-c', os.devnull,
             '--rootdir=' + root, '--confcutdir=' + root, '-o', 'addopts=']
arguments += [os.path.join(root, path) for path in config['files']]
sys.path.insert(0, root)
sys.path.insert(0, config['project'])
class Outcomes:
    def __init__(self):
        self.executed = 0
        self.collection_errors = 0
        self.setup_errors = 0
        self.teardown_errors = 0
        self.failures = []
        self.truncated = False
        self.incomplete = False
        self.detail = None
    def record(self, path, line, test_line, phase, category):
        if path not in config['files']:
            self.incomplete = True
            return
        item = dict(file_index=config['files'].index(path), line=line,
                    test_line=test_line, phase=phase, category=category)
        if item in self.failures:
            return
        if len(self.failures) >= 50:
            self.truncated = True
        else:
            self.failures.append(item)
    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1
            self.record(report.nodeid.split('::', 1)[0], 1, None,
                        'collection', 'collection_error')
    def pytest_runtest_makereport(self, item, call):
        self.detail = None
        if call.excinfo is None:
            return
        category = 'other_error'
        for kind, name in ((AssertionError, 'assertion_error'), (ImportError, 'import_error'),
                           (PermissionError, 'permission_error'), (ConnectionError, 'connection_error'),
                           (TimeoutError, 'timeout_error'), (OSError, 'os_error'),
                           (RuntimeError, 'runtime_error')):
            if issubclass(call.excinfo.type, kind):
                category = name
                break
        # Pytest locations describe the definition, which can be imported/inherited
        # from a different file than item.path (the collection file).
        definition_path, definition_line, _ = item.location

        def relative(value):
            try:
                text = os.path.relpath(value, root) if os.path.isabs(value) else value
            except ValueError:
                return None
            return text.replace(chr(92), '/')

        path = relative(definition_path)
        if path is None:
            self.incomplete = True
            return
        test_line = definition_line + 1
        line = test_line
        for frame in call.excinfo.traceback:
            if relative(str(frame.path)) == path:
                line = frame.lineno + 1
        self.detail = (path, line, test_line, call.when, category)
    def pytest_runtest_logreport(self, report):
        if report.failed:
            if self.detail is None:
                self.incomplete = True
            else:
                self.record(*self.detail)
        self.detail = None
        if report.when == 'call' and not report.skipped:
            self.executed += 1
        if report.when == 'setup' and report.failed:
            self.setup_errors += 1
        if report.when == 'teardown' and report.failed:
            self.teardown_errors += 1
outcomes = Outcomes()
code = int(pytest.main(arguments, plugins=[outcomes]))
if outcomes.collection_errors:
    code = 20
elif outcomes.setup_errors:
    code = 21
elif outcomes.teardown_errors:
    code = 22
elif code in (2, 3):
    code = 23
elif code == 4:
    code = 24
elif not outcomes.executed:
    code = 5
packet = dict(failures=outcomes.failures, truncated=outcomes.truncated,
              incomplete=outcomes.incomplete)
encoded = json.dumps(packet, separators=(',', ':')).encode()
if config.get('diagnostic_path'):
    with open(config['diagnostic_path'], 'wb') as output:
        output.write(encoded)
else:
    with os.fdopen(config['diagnostic_fd'], 'wb') as output:
        output.write(encoded)
raise SystemExit(code)
'''


def project_script(projects, root="/workspace"):
    """Controller script: fresh interpreter/cwd per group, one outer timeout."""
    return f'''import json, os, subprocess, sys
{inspect.getsource(BoundedOutput)}
codes, failures = [], []
truncated, incomplete = False, False
root = {root!r}
script = {_PYTEST_SCRIPT!r}
for index, project in enumerate({projects!r}):
    directory = os.path.join(root, project['root'])
    diagnostic_path = None
    if os.name == 'posix':
        with BoundedOutput() as capture:
            fd = capture.writer.fileno()
            config = {{'root': root, 'project': directory, 'files': project['files'], 'diagnostic_fd': fd}}
            result = subprocess.run([sys.executable, '-I', '-B', '-c', script], cwd=directory,
                                    input=json.dumps(config).encode(), pass_fds=(fd,),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raw, overflow = bytes(capture.data), capture.overflow or capture.incomplete
    else:
        diagnostic_path = os.path.join(directory, '.reviewflow-diagnostic')
        config = {{'root': root, 'project': directory, 'files': project['files'],
                   'diagnostic_path': diagnostic_path}}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script], cwd=directory,
                                input=json.dumps(config).encode(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with open(diagnostic_path, 'rb') as diagnostic:
                raw = diagnostic.read(65537)
            overflow = len(raw) > 65536
        except OSError:
            raw, overflow = b'', True
        finally:
            try:
                os.remove(diagnostic_path)
            except OSError:
                pass
    try:
        if overflow:
            raise ValueError()
        packet = json.loads(raw)
        if (type(packet) is not dict or set(packet) != {{'failures', 'truncated', 'incomplete'}}
                or type(packet['failures']) is not list or len(packet['failures']) > 50
                or type(packet['truncated']) is not bool or type(packet['incomplete']) is not bool):
            raise ValueError()
        records = []
        for record in packet['failures']:
            if type(record) is not dict or 'project_index' in record:
                raise ValueError()
            records.append(dict(record, project_index=index))
        available = 50 - len(failures)
        failures.extend(records[:available])
        truncated |= packet['truncated'] or len(records) > available
        incomplete |= packet['incomplete']
    except (ValueError, TypeError, KeyError, RecursionError):
        incomplete = True
    code = result.returncode
    codes.append(code if code in (0, 1, 5, 20, 21, 22, 23, 24) else 23)
packet = dict(version=1, failures=failures, truncated=truncated, incomplete=incomplete)
encoded = json.dumps(packet, separators=(',', ':')).encode()
if len(encoded) > 65536:
    encoded = b'{{"version":1,"failures":[],"truncated":true,"incomplete":true}}'
sys.stdout.buffer.write(encoded)
blocked = [code for code in codes if code not in (0, 1, 5)]
raise SystemExit(blocked[0] if blocked else 1 if 1 in codes else 0 if 0 in codes else 5)
'''

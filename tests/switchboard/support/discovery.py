"""Self-registration of test classes (#2206): the runner finds every `Test*` class of tests/switchboard/test_*.py itself.

Before, each test file needed one loader line in bin/test-switchboard that bound its classes, so two pull requests that each added a test file edited the same region and conflicted.
Now `bind_discovered(namespace, tests_dir)` binds, by its own name, every module-level unittest.TestCase class named Test* that a test file defines and the runner does not already hold.
A base class whose tests run through a derived class that the file defines or a loader line binds is left alone.
A loader line that binds a class (by its name or under another name) still wins: a class already in the namespace, by name or by identity, is never bound twice.
Standard library only.
"""
import importlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path


def discoverable(cls_name, cls, module_name):
    """True for a class that the runner binds on its own: defined by the file itself, a TestCase, named Test*."""
    return (cls_name.startswith("Test") and isinstance(cls, type) and issubclass(cls, unittest.TestCase)
            and cls.__module__ == module_name)


def discovered_classes(tests_dir):
    """{class name: class} of every discoverable class of the test files of `tests_dir`, files in name order."""
    found = {}
    for path in sorted(Path(tests_dir).glob("test_*.py")):
        module = importlib.import_module(path.stem)
        for name, cls in sorted(vars(module).items()):
            if discoverable(name, cls, module.__name__):
                found.setdefault(name, cls)
    return found


def bind_discovered(namespace, tests_dir):
    """Bind into `namespace` the discoverable classes it does not hold yet; return the sorted names bound.
    `namespace` is a dict, or the NAME of a module (`__name__`): the runner hands its name, so no call there takes the module itself (the rebinding guard);
    only names that are not held are ever written, so a moved name is never rebound."""
    if isinstance(namespace, str):
        namespace = vars(sys.modules[namespace])
    found = discovered_classes(tests_dir)
    held = [v for v in namespace.values() if isinstance(v, type)]
    bound = []
    for name, cls in found.items():
        # a base class is not bound: its tests run through the class that derives from it (the rule of the wiring check)
        derived = any(other is not cls and issubclass(other, cls) for other in held + list(found.values()))
        if name not in namespace and not any(v is cls for v in held) and not derived:
            namespace[name] = cls
            bound.append(name)
    return sorted(bound)


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


class _Combined:
    def __init__(self):
        self.testsRun, self.failures, self.errors, self.skipped = 0, [], [], []

    def wasSuccessful(self):
        return not self.failures and not self.errors


def run_suite_parallel(suite, scenario_reports=None):
    """Run the test classes of `suite` in forked workers (TEST_SWITCHBOARD_WORKERS, default cpu count); one combined result."""
    order = {}
    for case in _cases(suite):
        order.setdefault(type(case), []).append(case)
    groups = [(cls, unittest.TestSuite(cases)) for cls, cases in order.items()]
    workers = int(os.environ.get("TEST_SWITCHBOARD_WORKERS") or (os.cpu_count() or 1))
    if len(groups) <= 1 or workers <= 1 or not hasattr(os, "fork"):
        return unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    started = time.time()
    payloads = [None] * len(groups)
    running = {}   # pid -> (index, read fd)

    def reap():
        pid, _ = os.wait()
        index, fh = running.pop(pid)
        with fh:
            fh.seek(0)
            data = fh.read()
        try:
            payloads[index] = json.loads(data.decode("utf-8"))
        except ValueError:
            payloads[index] = {"testsRun": 0, "failures": [], "errors": [("worker %d" % index, "worker produced no result")],
                               "skipped": 0, "scenarios": [], "out": ""}

    for index, (cls, cls_suite) in enumerate(groups):
        while len(running) >= min(len(groups), workers):
            reap()
        fh = tempfile.TemporaryFile()   # a file, not a pipe: a large payload never blocks the child
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                buf = io.StringIO()
                sys.stdout = sys.stderr = buf
                if scenario_reports is not None:
                    scenario_reports[:] = []
                res = unittest.TextTestRunner(verbosity=2, stream=buf).run(cls_suite)
                out = json.dumps({"testsRun": res.testsRun, "failures": [(str(t), tb) for t, tb in res.failures],
                                  "errors": [(str(t), tb) for t, tb in res.errors], "skipped": len(res.skipped),
                                  "scenarios": list(scenario_reports or ()), "out": buf.getvalue()}, default=str)
                fh.write(out.encode("utf-8"))
                fh.flush()
            except BaseException:
                code = 1
            os._exit(code)
        running[pid] = (index, fh)
    while running:
        reap()
    result = _Combined()
    for payload in payloads:
        sys.stdout.write(re.sub(r"^Ran \d+ tests? in [0-9.]+s\n", "", payload["out"], flags=re.M))
        result.testsRun += payload["testsRun"]
        result.failures += [tuple(x) for x in payload["failures"]]
        result.errors += [tuple(x) for x in payload["errors"]]
        result.skipped += [None] * payload["skipped"]
        if scenario_reports is not None:
            scenario_reports.extend(payload["scenarios"])
    sys.stdout.write("Ran %d tests in %.3fs\n" % (result.testsRun, time.time() - started))
    sys.stdout.flush()
    return result

"""Self-registration of test classes (#2206): the runner finds every `Test*` class of tests/switchboard/test_*.py itself.

Before, each test file needed one loader line in bin/test-switchboard that bound its classes, so two pull requests that each added a test file edited the same region and conflicted.
Now `bind_discovered(namespace, tests_dir)` binds, by its own name, every module-level unittest.TestCase class named Test* that a test file defines and the runner does not already hold.
A base class whose tests run through a derived class that the file defines or a loader line binds is left alone.
A loader line that binds a class (by its name or under another name) still wins: a class already in the namespace, by name or by identity, is never bound twice.
Standard library only.
"""
import importlib
import sys
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

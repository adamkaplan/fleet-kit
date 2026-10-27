#!/usr/bin/env python3
"""selector_resolves of bin/cloud_quick.py (ask #1299): a selector that matches nothing still fails; a bin/test-switchboard
class that lives only in tests/switchboard/test_cases_NN.py resolves. A mutant that always says true is caught."""
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SPEC = importlib.util.spec_from_file_location("cq_selector", os.path.join(ROOT, "bin", "cloud_quick.py"))
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)
SB = "bin/test-switchboard"
GOOD = ("TestGitShim.test_s7_u1_no_file_of_this_change_writes_the_global_git_configuration")


def unresolved_all_fail(fn):
    return [p for p in (("TestNope.test_x"), ("TestGitShim.test_nope"), ("TestNope",)) if fn(ROOT, (SB, p))]


class Selector(unittest.TestCase):
    def test_unresolved_selectors_fail_and_the_moved_class_resolves(self):
        self.assertEqual(unresolved_all_fail(Q.selector_resolves), [])
        self.assertTrue(Q.selector_resolves(ROOT, (SB, GOOD)))
        self.assertTrue(Q.selector_resolves(ROOT, (SB, "TestGitShim")))

    def test_class_only_in_a_cases_file_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "bin")); os.makedirs(os.path.join(d, "tests", "switchboard"))
            open(os.path.join(d, "bin", "test-switchboard"), "w").write("# no classes here\n")
            open(os.path.join(d, "tests", "switchboard", "test_cases_07.py"), "w").write("class TestMoved:\n    def test_a(self):\n        pass\n")
            self.assertTrue(Q.selector_resolves(d, (SB, "TestMoved.test_a")))
            self.assertFalse(Q.selector_resolves(d, (SB, "TestMoved.test_b")))
            self.assertFalse(Q.selector_resolves(d, (SB, "TestOther")))

    def test_mutant_always_true_is_caught(self):
        real = Q.selector_resolves
        Q.selector_resolves = lambda root, pair: True
        try:
            self.assertIs(Q.selector_resolves(ROOT, (SB, "TestNope.test_x")), True, "the mutation applied")
            self.assertNotEqual(unresolved_all_fail(Q.selector_resolves), [], "the always-true mutant is not caught")
        finally:
            Q.selector_resolves = real
        self.assertEqual(unresolved_all_fail(Q.selector_resolves), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The two documents state the cause first, the two preflight modes, the variable and the measured times (ask #1299)."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DOCS = ("cloud/README.md", "docs/cloud-operator-quickstart.md")


def section(rel):
    with open(os.path.join(ROOT, rel)) as f:
        text = f.read()
    i = text.find("<!-- preflight-modes:begin -->")
    j = text.find("<!-- preflight-modes:end -->")
    assert 0 <= i < j, rel + ": no preflight-modes block"
    return text[i:j]


class DocsPreflightModes(unittest.TestCase):
    def test_s1_the_cause_is_stated_first(self):
        for rel in DOCS:
            s = section(rel)
            self.assertIn("could not run the preflight", s, rel)
            self.assertIn("300 seconds", s, rel)
            self.assertLess(s.find("could not run the preflight"), s.find("quick mode"), rel)

    def test_s11_both_modes_the_variable_and_a_measured_time(self):
        for rel in DOCS:
            s = section(rel)
            self.assertIn("quick mode", s, rel)
            self.assertIn("full mode", s, rel)
            self.assertIn("CLOUD_PREPUSH_PREFLIGHT", s, rel)
            self.assertRegex(s, r"quick mode[^\n]*measured[^\n]*\d+ ?(seconds|s)\b", rel)
            self.assertRegex(s, r"full mode[^\n]*measured[^\n]*\d+ ?(seconds|s)\b", rel)


if __name__ == "__main__":
    unittest.main()

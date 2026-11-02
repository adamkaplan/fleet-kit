"""Ask #1876: the author's About is read from an issue body or a report text. Neutral names (acme)."""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import decision_core as core  # noqa: E402


class TestAboutParsing(unittest.TestCase):
    def test_issue_bodies(self):   # S8
        cases = [
            ("## Plan\nx\n\n## About\nAcme sells\n  widgets.\n\n## Options\nA", "Acme sells widgets."),
            ("intro\n## About\nAt the\nend", "At the end"),
            ("## About\n\n## Options\nA", None),
            ("## Plan\nno about here", None),
            ("## About\nfirst one\n## Other\nz\n## About\nsecond", "first one"),
        ]
        got = [core.parse_about(body) for body, _ in cases]
        self.assertEqual(sum(1 for g, (_, want) in zip(got, cases) if g == want), 5, msg="about of five bodies: %r" % got)

    def test_report_paragraph(self):
        self.assertEqual(core.parse_about_paragraph("Ask?\n\nAbout: Acme\nneeds it.\n\nMore"), "Acme needs it.")
        self.assertIsNone(core.parse_about_paragraph("no paragraph"))
        self.assertIsNone(core.parse_about(None))


if __name__ == "__main__":
    unittest.main()

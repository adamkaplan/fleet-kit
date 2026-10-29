"""The call-site table is complete (#1515 S2) and shown in the docs (S10)."""
import glob
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "bin"))
import call_sites  # noqa: E402

PATHS = [os.path.join(REPO, "bin", "fleet-switchboard")] + sorted(glob.glob(os.path.join(REPO, "bin", "switchboard", "*.py")))


class CallSites(unittest.TestCase):
    def test_every_site_of_the_source_is_a_row(self):
        missing, stale = call_sites.missing(PATHS)
        self.assertEqual(missing, [], "call sites in no row of bin/call_sites.py")
        self.assertEqual(stale, [], "rows with no call site")

    def test_a_planted_site_is_named(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("class Planted:\n    def new_read(self):\n        return self.gh_api('repos/o/r/x')\n")
        try:
            missing, _ = call_sites.missing(PATHS + [fh.name])
        finally:
            os.unlink(fh.name)
        self.assertEqual([m[1] for m in missing], ["Planted.new_read"])

    def test_the_docs_show_the_table_and_the_counting_rule(self):
        text = open(os.path.join(REPO, "docs", "switchboard.md"), encoding="utf-8").read()
        section = text.split("## The daemon's GitHub reads: call sites and the counting rule", 1)[1]
        self.assertEqual(len(re.findall(r"^\*\*Counting rule\.\*\*", section, re.M)), 1)
        self.assertEqual(len(re.findall(r"^\| Call site ", section, re.M)), 1)
        for key in call_sites.SITES:
            self.assertIn("`%s`" % key[1], section)
        total = call_sites.hourly({"repos": 4, "pulls": 10, "pages": 2})[1]
        self.assertIn("**%d requests per hour**" % total, section)


if __name__ == "__main__":
    unittest.main()

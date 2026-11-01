"""The pure decision of bin/ci_fresh.py: a green run counts only on the current tip (ask #1755, S5)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
import ci_fresh  # noqa: E402

TIP = "a" * 40
OLD = "b" * 40


class TestCiStateDecision(unittest.TestCase):
    def test_a_ci_that_is_not_green_is_not_judged(self):
        for ci in ("failing", "pending", "none"):
            self.assertEqual(ci_fresh.ci_state(ci, OLD, TIP, 3), (None, ""), ci)

    def test_a_green_run_at_the_tip_is_fresh(self):
        state, line = ci_fresh.ci_state("green", TIP, TIP, None)
        self.assertEqual(state, "fresh")
        self.assertTrue(line.startswith("fresh: "), line)

    def test_a_green_run_behind_is_stale_with_the_count_and_the_8_character_bases(self):
        self.assertEqual(ci_fresh.ci_state("green", OLD, TIP, 3),
                         ("stale", "stale: 3 commits behind (CI ran on bbbbbbbb, the tip is aaaaaaaa)"))

    def test_the_commits_behind_not_counted_is_still_stale(self):
        state, line = ci_fresh.ci_state("green", OLD, TIP, None)
        self.assertEqual(state, "stale")
        self.assertTrue(line.startswith("stale: the commits behind were not counted"), line)

    def test_a_green_run_with_no_recorded_base_is_unknown(self):
        for base in (None, ""):
            self.assertEqual(ci_fresh.ci_state("green", base, TIP, None),
                             ("unknown", "unknown: the base of the last run is not recorded"))

    def test_a_green_run_with_an_unreadable_tip_is_unknown(self):
        for tip in (None, ""):
            self.assertEqual(ci_fresh.ci_state("green", OLD, tip, None),
                             ("unknown", "unknown: the tip could not be read"))


if __name__ == "__main__":
    unittest.main()

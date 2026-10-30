"""The README's section on the in-cluster reaper names the five things (ask #1582, S10)."""
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(os.path.dirname(HERE), "README.md")
PHRASES = ["runs as one restarting workload in the cluster namespace", "It holds exactly these rights", "To turn the Mac's job off",
           "To roll back", "The owner's steps are",
           "could reach the token service: a known limit, low risk", "A lost lease ends the pass", "the renewal thread yields to the lease"]


def missing(text):
    return [p for p in PHRASES if p not in text]


class TestDocs(unittest.TestCase):
    def test_five_of_five(self):
        with open(README) as f:
            text = f.read()
        self.assertEqual(missing(text), [])

    def test_mutant_without_rollback_counts_four(self):
        with open(README) as f:
            text = f.read().replace("To roll back", "x")
        self.assertEqual(len(missing(text)), 1)


if __name__ == "__main__":
    unittest.main()

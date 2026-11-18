"""#279: pass timing. Run: python3 -m unittest tests.test_pass_timing"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import pass_timing as pt  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Audit:
    def __init__(self):
        self.lines = []

    def append(self, actor, event, subject, detail):
        self.lines.append((event, detail))


def run(state, audit, stages, extra=0.0):
    clock = Clock()
    timer = pt.Timer(clock)
    for name, seconds in stages:
        with timer.stage(name):
            clock.t += seconds
    clock.t += extra
    pt.finish(timer, state, audit, "daemon", "2026-01-01T00:00:00Z")


class PassTiming(unittest.TestCase):
    def test_slow_stage_one_line(self):
        with tempfile.TemporaryDirectory() as d:
            audit = Audit()
            run(d, audit, [("discover", 1), ("facts", 31)])
            self.assertEqual([e for e, _ in audit.lines], ["pass.slow"])
            detail = audit.lines[0][1]
            self.assertEqual((detail["stage"], detail["seconds"]), ("facts", 31))
            self.assertEqual(set(detail["stages"]), {"discover", "facts"})

    def test_only_total_over_names_pass(self):
        with tempfile.TemporaryDirectory() as d:
            audit = Audit()
            run(d, audit, [("discover", 20), ("facts", 20)])
            self.assertEqual(audit.lines[0][1]["stage"], "pass")

    def test_exactly_threshold_is_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            audit = Audit()
            run(d, audit, [("facts", 30)])
            self.assertEqual(audit.lines, [])

    def test_status_line(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(pt.status_line(d))
            run(d, None, [("facts", 40)])
            run(d, None, [("render", 2)])
            line = pt.status_line(d)
            self.assertIn("40.0s", line)
            self.assertIn("facts", line)
            self.assertIn("last 2.0s", line)
            self.assertIn("1 over", line)

    def test_keeps_last_twenty_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(25):
                run(d, None, [("render", 1)])
            self.assertEqual(len(pt.load(d)), pt.KEEP)
        run("/nonexistent/dir", Audit(), [("facts", 99)])   # must not raise


if __name__ == "__main__":
    unittest.main()

"""Diagnosis only: a RUNNING pod idle 15+ minutes whose idle limit is longer leaves ONE 'idle-nudge-skipped' audit line per quiet period."""
import contextlib
import datetime as dt
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_idle_nudge as tn  # noqa: E402

cw, NOW, Stub, run = tn.cw, tn.NOW, tn.Stub, tn.run


def mkpod(n, idle="3600", marked=False):
    p = tn.pod(idle=idle, marked=marked)
    p["metadata"]["name"], p["metadata"]["uid"] = "p%d" % n, "u%d" % n
    p["status"]["startTime"] = (NOW - dt.timedelta(hours=1)).isoformat()
    return p


class Multi(Stub):
    """A stub that answers per pod name: the activity and the results probe."""
    def __init__(self, plan):
        super().__init__()
        self.plan, self.cur = plan, None

    def audit(self, *a):
        self.audits.append(a)

    def activity(self, name):
        self.cur = name
        return self.plan[name][0]

    def _exec(self, name, *cmd, **kw):
        self.execs.append(cmd)
        r = self.plan[self.cur][1]
        if r == "fail":
            raise cw.CloudError("exec failed")
        return r + "\n"


def six():
    late = NOW - dt.timedelta(minutes=20)
    plan = {"p1": ((True, late), "no"), "p2": ((False, None), "no"), "p3": ((False, late), "no"),
            "p4": ((False, late), "yes"), "p5": (None, "no"), "p6": ((False, late), "fail")}
    pods = [mkpod(1), mkpod(2), mkpod(3, marked=True), mkpod(4), mkpod(5), mkpod(6)]
    return plan, pods


def passes(fn, count=2):
    plan, pods = six()
    s = Multi(plan)
    out = []
    for _ in range(count):
        before = len(s.audits)
        with contextlib.redirect_stderr(io.StringIO()):
            fn(s, pods, NOW)   # the whole list in one pass: a pod missing from the list is forgotten
        out.append([a for a in s.audits[before:] if a[2] == "idle-nudge-skipped"])
    return out


class TestSkipped(unittest.TestCase):
    fn = staticmethod(cw.Launcher.send_idle_nudges)

    def test_s2_one_line_each_then_none(self):
        one, two = passes(self.fn)
        got = {a[1]: a[5] for a in one}
        self.assertEqual(got, {"pod/p1": "busy (idle unknown)", "pod/p2": "no-activity-time (idle unknown)", "pod/p3": "already-nudged",
                               "pod/p4": "results-finished", "pod/p5": "probe-unreadable (idle unknown)", "pod/p6": "exec-failed"})
        self.assertEqual(len(one), 6)
        self.assertEqual(two, [])

    def test_s3_short_idle_or_limit_no_line(self):
        s = Stub(idle_minutes=10)
        run(self.fn, s, mkpod(1))
        s2 = Stub(busy=True)
        run(self.fn, s2, mkpod(2, idle="900"))
        self.assertEqual([a for a in s.audits + s2.audits + getattr(s, 'skips', []) + getattr(s2, 'skips', []) if a[2] == "idle-nudge-skipped"], [])

    def test_young_pod_no_line(self):
        s = Stub(busy=True)
        p = mkpod(1)
        p["status"]["startTime"] = (NOW - dt.timedelta(minutes=10)).isoformat()
        run(self.fn, s, p)
        self.assertEqual(s.__dict__.get("skips", []), [])

    def test_busy_rate_limited_fake_clock(self):
        s = Stub(busy=True)
        p = mkpod(1)
        p["status"]["startTime"] = (NOW - dt.timedelta(minutes=30)).isoformat()
        lines = lambda: len(s.__dict__.get("skips", []))
        for minutes, want in ((0, 1), (5, 1), (14, 1), (15, 2), (20, 2)):
            with contextlib.redirect_stderr(io.StringIO()):
                self.fn(s, [p], NOW + dt.timedelta(minutes=minutes))
            self.assertEqual(lines(), want, minutes)

    def test_s4_nudge_unchanged(self):
        s = Stub()
        run(self.fn, s, mkpod(1))
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])
        f = Stub(results="yes")
        run(self.fn, f, mkpod(2))
        self.assertEqual(f.sent, [])

    def test_s6_gone_pods_pruned(self):
        s = Stub()
        a, b = mkpod(1), mkpod(2)
        a["status"]["startTime"] = b["status"]["startTime"] = (NOW - dt.timedelta(hours=1)).isoformat()
        s.activity = lambda name: (False, None)   # idle unknown -> _nudge_unknown
        with contextlib.redirect_stderr(io.StringIO()):
            self.fn(s, [a, b], NOW)
        self.assertEqual(set(s._nudge_unknown), {"u1", "u2"})
        s.__dict__.setdefault("_nudge_skipped", {}).update({("u2", "x"): True, ("u1", "x"): True})
        # b is in the full list but not in `live`: keeps its entries
        cw.nudge_prune(s, [a, b])
        self.assertEqual(set(s._nudge_unknown), {"u1", "u2"})
        self.assertEqual(set(k[0] for k in s._nudge_skipped), {"u1", "u2"})
        cw.nudge_prune(s, [a])
        self.assertEqual(set(s._nudge_unknown), {"u1"})
        self.assertEqual(set(k[0] for k in s._nudge_skipped), {"u1"})

    def test_s7_cap(self):
        s = Stub()
        s._nudge_unknown = {"u%d" % i: NOW for i in range(2005)}
        cw.nudge_prune(s, [{"metadata": {"uid": "u%d" % i}} for i in range(2005)])
        self.assertEqual(len(s._nudge_unknown), 2000)
        self.assertNotIn("u0", s._nudge_unknown)


class TestMutant(unittest.TestCase):
    def test_s5_control_then_mutant(self):
        one, _ = passes(cw.Launcher.send_idle_nudges)
        self.assertEqual(len(one), 6)   # control first
        m = tn.mutant("nudge_skip(self, ", "(lambda *a: None)(")
        one, _ = passes(m)
        self.assertEqual(len(one), 0)
        with self.assertRaises(AssertionError):
            self.assertTrue(one, "a nudge skip left no line")


if __name__ == "__main__":
    unittest.main()

"""The fresh-node race waits between attempts (#1023): the pause, the window and the attempt rule. No cloud: kubectl is the
fake beside this file, and time is a fake clock that only the launcher's own sleeps advance.

The retry has NO attempt count. After a race (or a vanish) the launcher pauses STACK_RACE_PAUSE_SECONDS, then deletes the pod and
creates it again, for as long as one more pause plus one poll fits in STACK_RACE_WINDOW_SECONDS. The mutants of the loop (no pause,
the count restored) are run by hand against a copy of the launcher; each of these tests names the mutant it kills."""
import io
import json
import os
import re
import sys
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base, T0  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
RUN_ID = "abcdef0123456789"
PAUSE, WINDOW, POLL = cw.STACK_RACE_PAUSE_SECONDS, cw.STACK_RACE_WINDOW_SECONDS, cw.WAIT_POLL_SECONDS
KINDS = ("race", "vanish")   # the two outcomes the loop retries: the handler-not-known event, and a pod that vanishes while pending
ALLOWED_RESOURCES = {"pod", "pods", "secret", "secrets", "events"}   # the launcher's role in the namespace (PC-1)


def expected_attempts(kind):
    """The rule, written out apart from the launcher: a failed attempt at time t is retried only if window - t >= pause + poll.
    A race is read at the first poll (an attempt costs no time); a vanish is read at the second (an attempt costs one poll)."""
    cost = POLL if kind == "vanish" else 0
    n, t = 1, cost
    while WINDOW - t >= PAUSE + POLL:
        t += PAUSE + cost
        n += 1
    return n


class Wait(Base):
    def run_stack(self, *extra):
        out, err = io.StringIO(), io.StringIO()

        def sleep(s):
            with open(self.env["FAKE_KUBE_CALLS"], "a") as f:   # the sleeps share the kubectl call log, so their ORDER is checked
                f.write(json.dumps(["SLEEP", s]) + "\n")
            self.clock.advance(seconds=s)
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", RUN_ID, "--stack", *extra],
                           self.env, self.clock, sleep)
        return code, out.getvalue(), err.getvalue()

    def elapsed(self):
        return (self.clock() - T0).total_seconds()

    def attempts(self):
        return [l for l in self.audit_lines() if l["action"] == "create-attempt"]

    def calls(self):
        """Every kubectl call without its login flags (the part from the verb on), and the launcher's sleeps as ["SLEEP", s]."""
        out = []
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            for c in map(json.loads, f):
                out.append(c if c[0] == "SLEEP" else (c[c.index("-n") + 2:] if "-n" in c else c))
        return out

    def fresh(self, kind, plan_steps):
        self.setUp()
        self.patch_state(plan=[kind] * plan_steps + ["ok"])


class Slow(Wait):
    def test_a_runtime_ready_after_a_long_delay_succeeds_on_a_later_attempt(self):   # S2; kills: the attempt count restored (gives up at 3)
        for kind in KINDS:
            with self.subTest(kind=kind):
                n = 4   # 4 rejections: ready on attempt 5; a longer pause (patched) keeps it at least 200 s in with few kubectl calls
                self.fresh(kind, n)
                with mock.patch.object(cw, "STACK_RACE_PAUSE_SECONDS", 60):
                    code, out, err = self.run_stack()
                self.assertEqual(code, 0, err)
                a = self.attempts()
                self.assertEqual(len(a), n + 1)
                self.assertGreater(len(a), 3)
                self.assertGreaterEqual(self.elapsed(), 200)
                self.assertEqual([l["ok"] for l in a], [False] * n + [True])
                self.assertTrue(a[-1]["reason"].endswith("ready"))
                self.assertEqual(len([l for l in a if l["reason"].endswith("ready")]), 1)

    def test_attempts_are_spaced_by_the_pause_and_the_pod_goes_only_after_it(self):   # S3; kills: no pause between the delete and the next create
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.fresh(kind, 14)
                code, out, err = self.run_stack()
                self.assertEqual(code, 0, err)
                a = self.attempts()
                stamps = [cw.parse_iso(l["ts"]) for l in a]
                gaps = [(b - x).total_seconds() for x, b in zip(stamps, stamps[1:])]
                self.assertEqual(len(gaps), 14)
                self.assertEqual([g for g in gaps if g < PAUSE], [])
                seq = self.calls()
                deletes = [i for i, c in enumerate(seq) if c[:2] == ["delete", "pod"] and "--wait=true" in c]
                self.assertEqual(len(deletes), 14)
                for i in deletes:   # the call just before each delete (the Secret checks aside) is the pause
                    before = [c for c in seq[:i] if not (c[:2] == ["get", "secret"] or c[:2] == ["get", "pod"] or c[:2] == ["get", "events"])]
                    self.assertEqual(before[-1], ["SLEEP", PAUSE])


class Frozen(Wait):
    """A clock that never advances (a fake clock, or a sleep that does not move it) must still end every waiting loop in the stack
    create path: _start_stack_pod's retry loop and _wait_ready (the only two; the rest of the file never sleeps on the clock in create).
    The second bound is the seconds the launcher itself slept. Mutant: that sum removed from a loop (the test aborts at the cap)."""
    CAP = 2000

    def frozen(self, kind, plan_steps):
        self.fresh(kind, plan_steps)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) > self.CAP:
                raise AssertionError("looped past %d sleeps: no end" % self.CAP)   # the clock is never advanced
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", RUN_ID, "--stack"],
                           self.env, self.clock, sleep)
        return code, err.getvalue(), sleeps

    def test_the_retry_loop_ends_with_one_refusal_on_a_frozen_clock(self):   # kills: the slept sum removed from the retry loop
        for kind in KINDS:
            with self.subTest(kind=kind):
                code, err, sleeps = self.frozen(kind, 500)
                self.assertEqual(code, cw.EXIT_CLOUD, err)
                self.assertLessEqual(sum(sleeps), WINDOW + POLL)
                self.assertEqual(len([l for l in err.splitlines() if "gave up" in l]), 1, err)

    def test_wait_ready_ends_on_a_frozen_clock(self):   # kills: the slept sum removed from _wait_ready (pod never ready, no race)
        self.setUp()
        self.patch_state(never_ready=True)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            self.assertLessEqual(len(sleeps), self.CAP, "no end on a frozen clock")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", RUN_ID, "--stack", "--wait", "30"],
                           self.env, self.clock, sleep)
        self.assertEqual(code, cw.EXIT_TIMEOUT, err.getvalue())
        self.assertLessEqual(sum(sleeps), 30 + POLL)


class Never(Wait):
    def test_a_runtime_that_never_comes_ends_at_the_window_with_one_message(self):   # S4, S6, U3; kills: the count restored; no end at the window
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.fresh(kind, 300)
                code, out, err = self.run_stack()
                self.assertEqual(code, cw.EXIT_CLOUD, err)
                n = expected_attempts(kind)
                self.assertEqual(len(self.attempts()), n)
                self.assertLessEqual(self.elapsed(), WINDOW + POLL)
                self.assertLess(self.elapsed(), 900)   # the switchboard's LAUNCH_SECONDS
                lines = [l for l in err.splitlines() if "gave up" in l]
                self.assertEqual(len(lines), 1, err)   # one refusal, not one per attempt
                self.assertIn("gave up after %d attempt(s) in %d s" % (n, self.elapsed()), lines[0])
                self.assertEqual(self.st()["pods"], {})   # no pod and no Secret left
                self.assertEqual(self.st()["secrets"], {})

    def test_a_retry_never_waits_past_the_window(self):   # U3; kills: the last attempt keeps its full --wait
        self.fresh("race", 300)
        code, out, err = self.run_stack("--wait", "300")
        self.assertEqual(code, cw.EXIT_CLOUD, err)
        self.assertLess(self.elapsed(), 900)

    def test_no_kubectl_call_leaves_the_launchers_role(self):   # U2; kills: a read of a node or a RuntimeClass to decide readiness
        self.fresh("race", 14)
        self.run_stack()
        for c in self.calls():
            if c[0] == "SLEEP":
                continue
            words = [w.split("/")[0].split(",")[0].lower() for w in c if not w.startswith("-") and "=" not in w]
            self.assertEqual([w for w in words if w in ("node", "nodes", "runtimeclass", "runtimeclasses", "no")], [], c)
        verbs = {c[c.index(v) + 1] for c in self.calls() if c[0] != "SLEEP" for v in ("get", "delete") if v in c and c.index(v) + 1 < len(c)}
        self.assertLessEqual({v.split("/")[0] for v in verbs} - {"secret", "secrets", "pod", "pods", "events", "event"}, set())


class OnePlace(unittest.TestCase):
    OLD = "STACK" + "_ATTEMPTS"
    OLD_TEXT = "3 " + "attempts"
    DOCS = ("cloud/README.md", "cloud/launcher/README.md", "docs/cloud-worker-interface.md", "docs/cloud-operator-quickstart.md")

    def text(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return f.read()

    def test_the_constants_are_defined_once_and_the_count_is_gone(self):   # S5; kills: a count constant back, a literal 15 beside the constant
        src = self.text("cloud/launcher/cloud_worker.py")
        for name in ("STACK_RACE_WINDOW_SECONDS", "STACK_RACE_PAUSE_SECONDS", "WAIT_POLL_SECONDS"):
            self.assertEqual(len(re.findall(r"^%s = " % name, src, re.M)), 1, name)
        self.assertFalse(hasattr(cw, self.OLD))
        self.assertEqual(re.findall(r"sleep\(\s*(?:15|2)\s*\)", src), [])   # no literal pause beside the constants
        for rel in ("cloud/launcher/cloud_worker.py", *self.DOCS, "cloud/launcher/tests/test_cloud_stack.py", "cloud/launcher/tests/test_cloud_stack_wait.py"):
            t = self.text(rel)
            self.assertNotIn(self.OLD, t, rel)
            self.assertNotIn(self.OLD_TEXT, t, rel)
            self.assertIsNone(re.search(r"attempt (?:\d+|N) of 3", t), rel)

    def test_every_doc_page_states_the_window_and_the_pause(self):   # S5; kills: a page left with the old numbers
        for rel in self.DOCS:
            t = self.text(rel)
            self.assertIn("STACK_RACE_WINDOW_SECONDS", t, rel)
            self.assertIn("STACK_RACE_PAUSE_SECONDS", t, rel)
            self.assertIn("%d seconds" % WINDOW, t, rel)
            self.assertIn("%d seconds" % PAUSE, t, rel)
            self.assertNotIn("4 minutes", t.split("fresh-node")[-1][:600] if "fresh-node" in t else "", rel)


if __name__ == "__main__":
    unittest.main()

"""#1992: `merge-gate --pass --merge` reads the pull request once after a failing `gh pr merge` and says what it sees. Scenarios S1 to S5.
The fake is a subclass of FakeMergeGitHub; every test runs the REAL `pass_and_merge`. The mutant (S5) has its control first and asserts the mutation applied."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support.herdr_world import made_up_pid
from support.loader import sb
from support.paths import REPO
from support.service_and_merge import FakeMergeGitHub, MG_HEAD

STATE_VIEW = "state,headRefOid,mergeCommit"


class AnswerGitHub(FakeMergeGitHub):
    """`pr merge` answers the scenario (code, out, err); the state re-read answers `view`: a dict (JSON), a str (sent as is) or None (fails)."""

    def __init__(self):
        super().__init__()
        self.merge_answer = (1, "", "gh: boom\nsecond line")
        self.view = {"state": "MERGED", "headRefOid": MG_HEAD, "mergeCommit": {"oid": "f" * 40}}
        self.state_reads, self.events = [], []
        self.lock_probe, self.lock_held_at_read = None, []
        self.view_raises = False

    def __call__(self, argv, own=True):
        if argv[:2] == ["pr", "merge"]:
            self.merges.append(list(argv))
            self.merge_own.append(own)
            return self.merge_answer
        if argv[:2] == ["pr", "view"] and STATE_VIEW in argv:
            self.state_reads.append(list(argv))
            self.events.append("read")
            if self.lock_probe:
                self.lock_held_at_read.append(self.lock_probe() is not None)
            if self.view_raises:
                raise RuntimeError("view blew up")
            if self.view is None:
                return 1, "", "HTTP 502"
            return 0, self.view if isinstance(self.view, str) else json.dumps(self.view), ""
        return super().__call__(argv, own)


class AnswerBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.gate = sb.load_module("merge_gate")
        self.reading = sb.load_module("pr_reading")
        self.fake = self.fresh()
        self.repo, self.n = self.fake.repo, self.fake.number
        self.state = self.dir / "mg-state"
        self.state.mkdir()
        self.conf = self.gate.settings({"merge_gate": {"repos": {self.repo: {}}}})

    def fresh(self):
        """A new fake wired to the lock file of this test's state directory."""
        fake = AnswerGitHub()
        fake.lock_probe = lambda: self.gate.read_lock(self.state, fake.repo, "main")
        return fake

    def run_real(self, gate=None, clock=None):
        """The REAL pass_and_merge; the lock releases are logged into the fake's event list."""
        gate = gate or self.gate
        real = gate.release

        def release(*a, **k):
            self.fake.events.append("release")
            return real(*a, **k)
        gate.release = release
        try:
            return gate.pass_and_merge(self.reading, self.fake, self.conf, self.state, self.repo, self.n, "coder-a", made_up_pid(), clock or self.fake.clock, self.fake.sleep,
                                       alive=lambda pid: True)
        finally:
            gate.release = real

    def cases(self):
        other = {"state": "MERGED", "headRefOid": "c" * 40, "mergeCommit": None}
        return [{"state": "OPEN", "headRefOid": MG_HEAD, "mergeCommit": None}, other, None, "[1]"]

    def late(self, bad):
        """A clock that is the fake's until the merge was sent, then answers `bad`."""
        return lambda: bad if self.fake.merges else self.fake.clock()

    def each(self, views):
        """Yield with a fresh fake per view (no setUp repeat)."""
        for view in views:
            self.fake = self.fresh()
            self.fake.view = view
            yield view



class TestMergeGateAnswer(AnswerBase):
    def test_s1_a_failing_exit_with_the_pull_request_merged_is_reported_merged(self):
        code, lines, audit = self.run_real()
        self.assertEqual(code, 0, "a failing exit with the pull request merged is reported merged")
        self.assertTrue(lines[-1].startswith("merged "), lines)
        self.assertIn("gh exited 1: gh: boom", lines[-1])
        self.assertEqual((audit["outcome"], audit["merge_exit"], audit["merge_said"]), ("merged", 1, "gh: boom"))

    def test_s2_anything_else_is_state_unknown_and_never_merged(self):
        for view in self.each(self.cases() + [[1], {"state": 5, "headRefOid": MG_HEAD}]):
            with self.subTest(view=view):
                code, lines, audit = self.run_real()
                self.assertEqual(code, 1)
                self.assertIn("state is unknown at", lines[-1])
                self.assertIn("read the pull request: gh: boom", lines[-1])
                self.assertNotIn("merged ", lines[-1].replace("pull request state", ""))
                self.assertNotIn("nothing merged", lines[-1])
                self.assertEqual(audit["outcome"], "refused: merge exit 1")
                self.assertEqual(audit["merge_said"], "gh: boom")
                self.assertIn("merge_state", audit)

    def test_s2_exit_10_is_as_today_with_no_reread(self):
        self.fake.merge_answer = (10, "", "blocked by shim")
        code, lines, audit = self.run_real()
        self.assertEqual(code, 10)
        self.assertEqual(lines[-1], "merge-gate --pass --merge: the merge was refused or failed (exit 10), the lock is released: blocked by shim")
        self.assertEqual(self.fake.state_reads, [])

    def test_s3_one_reread_after_the_release(self):
        for view in self.each([self.fake.view] + self.cases()):
            with self.subTest(view=view):
                self.run_real()
                self.assertEqual(len(self.fake.state_reads), 1)
                self.assertEqual(self.fake.lock_held_at_read, [False], "the lock is released at the moment of the read")
                self.assertEqual(self.fake.events.index("release") < self.fake.events.index("read"), True, self.fake.events)

    def test_s4_the_lock_is_released_in_every_case(self):
        released = 0
        run = 0
        for view in self.each([self.fake.view] + self.cases()):
            self.run_real()
            run += 1
            released += self.fake.events.count("release")
            self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"))
        self.fake = self.fresh()
        self.fake.merge_answer = (10, "", "blocked by shim")
        self.run_real()
        run += 1
        released += self.fake.events.count("release")
        self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"))
        self.assertEqual((released, run), (6, 6), "the lock is released in 6 of 6 cases")

    def test_s6_the_reread_raising_is_reported_not_raised(self):
        self.fake.view_raises = True
        code, lines, audit = self.run_real()
        self.assertEqual(code, 1, msg="an exception of the re-read is reported, not raised")
        self.assertIn("could not be read", audit["merge_state"])
        self.assertEqual((audit["merge_exit"], audit["merge_said"]), (1, "gh: boom"))
        self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"))

    def test_s7_an_unusable_clock_is_an_unknown_time(self):
        for bad in (1e30, "x", float("nan"), None):
            with self.subTest(clock=bad):
                self.fake = self.fresh()
                self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}
                code, lines, audit = self.run_real(clock=self.late(bad))
                self.assertEqual(code, 1, msg="an unusable clock is reported as an unknown time")
                self.assertIn("unknown at an unknown time", lines[-1], msg="an unusable clock is reported as an unknown time")
                self.assertEqual(audit["merge_said"], "gh: boom")

    def test_s11_a_clock_that_raises_is_an_unknown_time(self):
        def boom():
            if self.fake.merges:
                raise RuntimeError("clock broke")
            return self.fake.clock()
        self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}
        code, lines, audit = self.run_real(clock=boom)
        self.assertEqual(code, 1, msg="a raising clock ends in the refusal, no traceback")
        self.assertIn("state is unknown at an unknown time", lines[-1])

    def test_s12_a_negative_clock_is_an_unknown_time(self):
        for bad in (-1, -1.5, -1e9):
            with self.subTest(clock=bad):
                self.fake = self.fresh()
                self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}
                code, lines, audit = self.run_real(clock=self.late(bad))
                self.assertEqual(code, 1)
                self.assertIn("unknown at an unknown time", lines[-1], msg="a negative clock gives no stamp")
                self.assertNotIn("23:59:59Z", lines[-1])

    def test_s8_merged_without_a_head_says_no_head_given(self):
        self.fake.view = {"state": "MERGED"}
        code, lines, audit = self.run_real()
        self.assertEqual(code, 1)
        self.assertEqual(audit["merge_state"], "MERGED (no head given)")
        self.assertIn("MERGED (no head given)", lines[-1])


class TestMergeGateAnswerMutant(AnswerBase):
    """S5: a copy of the module with the re-read skipped."""
    NEEDLE = 'mcode, mout, _ = gh(["pr", "view", str(number), "-R", repo, "--json", "state,headRefOid,mergeCommit"])'
    SKIPPED = 'mcode, mout = 1, ""'

    def copy(self, text):
        path = self.dir / "merge_gate.py"
        path.write_text(text)
        import importlib.util
        spec = importlib.util.spec_from_file_location("merge_gate_mutant", str(path))
        mod = importlib.util.module_from_spec(spec)
        import sys
        sys.path.insert(0, str(REPO / "bin"))
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(REPO / "bin"))
        return mod

    def s1_on(self, gate):
        code, lines, audit = self.run_real(gate)
        self.assertEqual(code, 0, "a failing exit with the pull request merged is reported merged")

    def test_s5_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        self.s1_on(self.copy(text))   # the unmutated copy passes first
        self.assertEqual(text.count(self.NEEDLE), 1)   # the mutation is applied: the replaced text occurs once
        self.fake = self.fresh()
        mutant = self.copy(text.replace(self.NEEDLE, self.SKIPPED))
        with self.assertRaises(AssertionError) as caught:
            self.s1_on(mutant)
        self.assertIn("a failing exit with the pull request merged is reported merged", str(caught.exception))

    def test_s5_head_check_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        needle = 'if mdata.get("state") == "MERGED" and mdata.get("headRefOid") == head:'
        self.assertEqual(text.count(needle), 1)

        def other_head(gate):
            self.fake = self.fresh()
            self.fake.view = {"state": "MERGED", "headRefOid": "c" * 40, "mergeCommit": None}
            code, lines, audit = self.run_real(gate)
            self.assertEqual(code, 1, msg="MERGED at another head is not reported merged")
        other_head(self.copy(text))   # the control passes first
        mutant = self.copy(text.replace(needle, 'if mdata.get("state") == "MERGED":'))
        with self.assertRaises(AssertionError) as caught:
            other_head(mutant)
        self.assertIn("MERGED at another head is not reported merged", str(caught.exception))

    def test_s9_reread_guard_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        needle = "        except Exception:\n            mcode, mout = 1, \"\""
        self.assertEqual(text.count(needle), 1)

        def raising(gate):
            self.fake = self.fresh()
            self.fake.view_raises = True
            code, lines, audit = self.run_real(gate)
            self.assertEqual(code, 1, msg="an exception of the re-read is reported, not raised")
        raising(self.copy(text))
        mutant = self.copy(text.replace(needle, "        except KeyboardInterrupt:\n            mcode, mout = 1, \"\""))
        with self.assertRaises(RuntimeError):
            raising(mutant)

    def test_s10_clock_guard_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        needle = "if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now) or now < 0:"
        self.assertEqual(text.count(needle), 1)

        def none_clock(gate):
            self.fake = self.fresh()
            self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}
            code, lines, audit = self.run_real(gate, clock=self.late(None))
            self.assertIn("unknown at an unknown time", lines[-1], msg="an unusable clock is reported as an unknown time")
        none_clock(self.copy(text))
        mutant = self.copy(text.replace(needle, "if False:"))
        with self.assertRaises(AssertionError) as caught:
            none_clock(mutant)
        self.assertIn("an unusable clock is reported as an unknown time", str(caught.exception))

    def test_s11_raising_clock_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        needle = "        except Exception:\n            stamp = \"an unknown time\""
        self.assertEqual(text.count(needle), 1)

        def raising(gate):
            self.fake = self.fresh()
            self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}

            def boom():
                if self.fake.merges:
                    raise RuntimeError("clock broke")
                return self.fake.clock()
            code, lines, audit = self.run_real(gate, clock=boom)
            self.assertIn("state is unknown at an unknown time", lines[-1], msg="a raising clock ends in the refusal")
        raising(self.copy(text))
        mutant = self.copy(text.replace(needle, needle.replace("Exception", "(OverflowError, TypeError, ValueError, OSError)")))
        with self.assertRaises(RuntimeError):   # REAL: the old tuple lets RuntimeError escape
            raising(mutant)

    def test_s12_negative_clock_control_then_mutant(self):
        text = (REPO / "bin" / "merge_gate.py").read_text()
        needle = " or not math.isfinite(now) or now < 0:"
        self.assertEqual(text.count(needle), 1)

        def negative(gate):
            self.fake = self.fresh()
            self.fake.view = {"state": "OPEN", "headRefOid": MG_HEAD}
            code, lines, audit = self.run_real(gate, clock=self.late(-1))
            self.assertIn("unknown at an unknown time", lines[-1], msg="a negative clock gives no stamp")
        negative(self.copy(text))
        mutant = self.copy(text.replace(needle, " or not math.isfinite(now):"))
        with self.assertRaises(AssertionError):   # REAL: gmtime(-1) gives 23:59:59Z
            negative(mutant)

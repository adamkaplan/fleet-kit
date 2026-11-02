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

    def run_real(self, gate=None):
        """The REAL pass_and_merge; the lock releases are logged into the fake's event list."""
        gate = gate or self.gate
        real = gate.release

        def release(*a, **k):
            self.fake.events.append("release")
            return real(*a, **k)
        gate.release = release
        try:
            return gate.pass_and_merge(self.reading, self.fake, self.conf, self.state, self.repo, self.n, "coder-a", made_up_pid(), self.fake.clock, self.fake.sleep,
                                       alive=lambda pid: True)
        finally:
            gate.release = real

    def cases(self):
        other = {"state": "MERGED", "headRefOid": "c" * 40, "mergeCommit": None}
        return [{"state": "OPEN", "headRefOid": MG_HEAD, "mergeCommit": None}, other, None, "[1]"]

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

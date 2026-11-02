"""#1970: `held-ci` lists the held pull requests' CI tip and refreshes ONE on request. build() is called by bin/test-switchboard, which holds HandoffCase."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from support.loader import captured, copy_entry, _load, sb
from support.paths import BIN
from support.scenario_roles import FAKE_GH, FAKE_REPO

TIP = "a" * 40
OLD = "b" * 40


def build(HandoffCase):
    class FakeHeld:
        """`gh api` over a list of open pull requests: reads by path, writes (PATCH) recorded."""

        def __init__(self, label, held=0, others=0, drafts=(), run_tip=TIP, behind=3):
            self.label, self.reads, self.writes, self.page_reads = label, [], [], 0
            self.run_tip, self.behind, self.endless = run_tip, behind, False
            self.prs = {}
            for n in range(1, held + 1):
                self.add(n, [label], draft=n in drafts)
            for n in range(held + 1, held + others + 1):
                self.add(n, [])
            self.after = None   # a function (pull) applied after the close, to drift the head or the labels
            self.fail_reopen = False

        def add(self, number, labels, draft=False):
            self.prs[number] = {"number": number, "state": "open", "draft": draft, "labels": [{"name": l} for l in labels],
                                "head": {"sha": "%040x" % number}, "base": {"ref": "main"}}

        def ok(self, argv, body):
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

        def __call__(self, argv, **kwargs):
            if "--method" in argv:
                state = json.loads(kwargs["input"])["state"]
                number = int(argv[2].rsplit("/", 1)[1])
                self.writes.append((number, state))
                if state == "open" and self.fail_reopen:
                    return subprocess.CompletedProcess(argv, 1, "", "boom (HTTP 500)")
                self.prs[number]["state"] = state
                if state == "closed" and self.after:
                    self.after(self.prs[number])
                return self.ok(argv, {})
            path = argv[-1]
            self.reads.append(path)
            if "/pulls?" in path:
                self.page_reads += 1
                page = int(path.rsplit("page=", 1)[1])
                if self.endless:
                    return self.ok(argv, [dict(self.prs[1], number=page * 1000 + i) for i in range(100)])
                everything = [self.prs[n] for n in sorted(self.prs)]
                return self.ok(argv, everything[(page - 1) * 100:page * 100])
            if "/pulls/" in path:
                number = int(path.rsplit("/", 1)[1])
                return self.ok(argv, self.prs[number]) if number in self.prs else subprocess.CompletedProcess(argv, 1, "", "Not Found (HTTP 404)")
            if "/actions/runs" in path:
                return self.ok(argv, {"workflow_runs": [{"pull_requests": [{"base": {"sha": self.run_tip}}]}]})
            if "/branches/" in path:
                return self.ok(argv, {"commit": {"sha": TIP}})
            if "/compare/" in path:
                return self.ok(argv, {"ahead_by": self.behind})
            raise AssertionError("unexpected read " + path)

    class TestHeldCi(HandoffCase):
        """#1970 S2-S8, U4, U5: the list reads to its end and writes nothing; a refresh is one pull request, checked before and after."""
        MODULE = sb

        def setUp(self):
            super().setUp()
            self.label = sb.label_names({"gh": FAKE_GH})["merge-captain"]
            self.audit_file = None

        def fake(self, **kw):
            return FakeHeld(self.label, **kw)

        def run_held(self, fake, *argv, mod=None):
            def route(args, **kwargs):
                return fake(args, **kwargs) if args[0] == FAKE_GH else self.world.route(args, **kwargs)
            with captured() as (out, err):
                code = (mod or self.MODULE).main(["held-ci", "--repo", FAKE_REPO] + list(argv), runner=route, now=self.world.clock.now)
            return code, out.getvalue(), err.getvalue()

        def audit_lines(self):
            found = [l for p in Path(self.state_dir).glob("audit*.jsonl") for l in p.read_text().splitlines() if "held-ci" in l]
            return found

        def test_s2_25_held_and_3_unlabelled_print_25_lines(self):
            fake = self.fake(held=25, others=3)
            code, out, _ = self.run_held(fake)
            lines = [l for l in out.splitlines() if "head " in l]
            self.assertEqual((code, len(lines)), (0, 25), out)
            self.assertIn("25 of 25", out)
            self.assertFalse(any("#26 " in l or "#27 " in l or "#28 " in l for l in lines))

        def test_s3_150_held_are_read_on_page_two(self):
            fake = self.fake(held=150)
            code, out, _ = self.run_held(fake)
            self.assertEqual((code, fake.page_reads), (0, 2), out)
            self.assertIn("150 of 150", out)

        def test_s3_a_list_that_never_ends_is_cut_and_named(self):
            fake = self.fake(held=1)
            fake.endless = True
            code, out, _ = self.run_held(fake)
            self.assertEqual(code, 1, out)
            self.assertIn("list cut at 100 pages", out)
            self.assertEqual(fake.page_reads, 100)

        def test_u4_600_held_are_read_to_their_end(self):
            fake = self.fake(held=600)
            code, out, _ = self.run_held(fake)
            self.assertEqual((code, fake.page_reads), (0, 7), out)   # six full pages, then the empty seventh that ends the read (the plan said 6)
            self.assertIn("600 of 600", out)

        def test_s4_current_and_stale_and_no_write(self):
            fake = self.fake(held=1)
            code, out, _ = self.run_held(fake)
            self.assertTrue(out.splitlines()[0].endswith("current"), out)
            self.assertEqual(fake.writes, [])
            fake = self.fake(held=1, run_tip=OLD, behind=3)
            code, out, _ = self.run_held(fake)
            self.assertTrue(out.splitlines()[0].endswith("stale: 3 commits behind"), out)
            self.assertEqual((fake.writes, self.audit_lines()), ([], []))

        def test_s5_a_refresh_closes_and_reopens_once_and_says_unchanged(self):
            fake = self.fake(held=2)
            code, out, _ = self.run_held(fake, "--refresh", "2")
            self.assertEqual((code, fake.writes), (0, [(2, "closed"), (2, "open")]), out)
            self.assertIn("head and labels unchanged", out)
            self.assertEqual(len(self.audit_lines()), 1)

        def test_s6_refusals_write_nothing(self):
            fake = self.fake(held=2, others=1, drafts=(2,))
            for argv, reason in (("2", "draft"), ("3", "not held"), ("1 2", "exactly one")):
                with self.subTest(reason=reason):
                    code, _, err = self.run_held(fake, "--refresh", *argv.split())
                    self.assertIn(code, (1, 2))
                    self.assertIn(reason, err)
            self.assertEqual((fake.writes, self.audit_lines()), ([], []))

        def test_s7_a_list_writes_no_audit_line_and_nothing_calls_the_refresh(self):
            self.run_held(self.fake(held=3))
            self.assertEqual(self.audit_lines(), [])
            text = "\n".join(p.read_text() for p in [BIN / "fleet-switchboard"] + sorted((BIN / "switchboard").glob("*.py")))
            self.assertEqual(text.count("held_ci_refresh("), 2, "its definition and its one call in cmd_held_ci")

        def drift(self, change):
            fake = self.fake(held=1)
            fake.after = change
            return fake, self.run_held(fake, "--refresh", "1")

        def test_s8_a_drift_of_a_label_or_the_head_is_named(self):
            for what, change in (("labels changed", lambda pr: pr.update(labels=[])), ("head changed", lambda pr: pr.update(head={"sha": "c" * 40}))):
                with self.subTest(what=what):
                    _, (code, out, _) = self.drift(change)
                    self.assertEqual(code, 1, out)
                    self.assertIn(what, out)
                    self.assertNotIn("head and labels unchanged", out)

        def test_u5_a_failed_reopen_says_closed_and_leaves_one_audit_line(self):
            fake = self.fake(held=1)
            fake.fail_reopen = True
            code, _, err = self.run_held(fake, "--refresh", "1")
            self.assertEqual(code, 1)
            self.assertIn("CLOSED", err)
            self.assertIn("1", err)
            self.assertEqual(len(self.audit_lines()), 1)

        # -- the mutants, each after its unmutated control

        def mutant(self, needle, replacement, tag):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            shutil.copytree(str(BIN), tmp, dirs_exist_ok=True)
            copy_entry(tmp)
            host = Path(tmp) / "switchboard" / "agents_verify.py"
            text = host.read_text()
            self.assertEqual(text.count(needle), 1)
            host.write_text(text.replace(needle, replacement, 1))
            return _load("t_held_ci_" + tag, Path(tmp) / "fleet-switchboard")

        def test_s9_the_mutant_without_the_check_fails_on_the_text_of_the_check(self):
            def check(mod):
                _, (code, out, _) = self.drift(lambda pr: pr.update(labels=[]))
                self.assertNotIn("head and labels unchanged", out)
                self.assertEqual(code, 1)
            def drift_with(mod):
                fake = self.fake(held=1)
                fake.after = lambda pr: pr.update(labels=[])
                code, out, _ = self.run_held(fake, "--refresh", "1", mod=mod)
                self.assertNotIn("head and labels unchanged", out)
                self.assertEqual(code, 1)
            drift_with(sb)   # the control: the unmutated copy passes
            mod = self.mutant('    if after["labels"] != before["labels"]:', "    if False:", "nocheck")
            with self.assertRaises(AssertionError) as caught:
                drift_with(mod)
            self.assertIn("head and labels unchanged", str(caught.exception))

        def test_s9_the_mutant_that_lists_the_first_100_fails_on_150_of_150(self):
            def check(mod):
                code, out, _ = self.run_held(self.fake(held=150), mod=mod)
                self.assertIn("150 of 150", out)
            check(sb)   # the control
            mod = self.mutant("cap=HELD_CI_PAGES", "cap=1", "first100")
            with self.assertRaises(AssertionError) as caught:
                check(mod)
            self.assertIn("150 of 150", str(caught.exception))

    return TestHeldCi

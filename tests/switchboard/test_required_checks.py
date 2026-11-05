"""#2203: a reader's brief names its required checks in one marked block; the launch keeps the list; a posted review comment is read against it
(S2, S3, U1). Properties, not exact sets or sizes. S4 (the strict-read count in `gates` and the merge gate) is HELD for the owner and is not built here.
build() is called by bin/test-switchboard, which holds TwoRepoCase."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import captured, sb
from support.paths import REPO
from support.installs_and_keys import ACME_WEB

CHECKS = ["production-mutant sample", "restart test", "docs paragraph"]
BRIEF = "LOCAL REVIEWER. PR #5.\n\nREQUIRED CHECKS:\n" + "".join("- %s\n" % c for c in CHECKS) + "\nNot a check: - outside line\n"
SHA = "3f2a9c1b7d4e" + "0" * 28


def module():
    return sb.load_module("required_checks")


def build(TwoRepoCase):
    class TestRequiredChecks(TwoRepoCase):
        """S2, S3 and U1 on fakes: the real launch, the real comparison and the real routing step."""

        def setUp(self):
            super().setUp()
            self.work = self.home / "checkout"
            self.work.mkdir(exist_ok=True)
            os.environ.pop("HERDR_PANE_ID", None)
            patcher = mock.patch.object(sb, "launch_memory_check", lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)

        def launch(self, brief, name):
            path = self.home / ("brief-%s.md" % name)
            path.write_text(brief)
            with captured() as (out, err):
                code = sb.main(["launch", name, "--agent", "coder", "--dir", str(self.work), "--workspace", "w1", "--role", "reviewer", "--repo", ACME_WEB,
                                "--issue", "20", "--brief-file", str(path)], runner=lambda argv, **kw: self.gh(argv, **kw) if argv[0] == sb_gh() else self.world.route(argv, **kw),
                               now=self.world.clock.now, wait=lambda sock, s: False, socket_call=self.world.herdr.socket)
            self.assertEqual(code, 0, err.getvalue())
            return [s["metadata"]["fleet"] for s in self.world.v2.sessions.values() if s["metadata"]["fleet"].get("name") == name][0]

        def route(self, fleet, body):
            """One review comment routed to its lane: the item as the daemon holds it afterwards."""
            self.audits = []
            agents = {"orch": types.SimpleNamespace(fleet={"role": "orchestrator"}), "rev-1": types.SimpleNamespace(fleet=fleet)}
            item = sb.GitHubItem("github.reviewcomment:1", "github.comment", "base summary", "o/r", [], [], "bob", 1.0, [10], True)
            item.review_signer, item.review_body = "rev-1", body
            hub = types.SimpleNamespace(queue={item.key: item}, lock=threading.RLock(), _unrouted=set(), _filtered=set(), _unrouted_batch=[], _filtered_batch=[],
                                        _routed_batch=[], _audit=lambda *a: self.audits.append(a), route_of=lambda *a: ("orch", "closing"), _fleet_login=lambda l: True,
                                        _unowned_seen=lambda k: False)
            hub._review_checks_note = lambda i, a: sb.GitHubHub._review_checks_note(hub, i, a)
            sb.GitHubHub._route(hub, item, types.SimpleNamespace(problems=[], agents=agents), 1.0)
            return item

        # S2
        def test_s2_the_launch_record_holds_every_check_of_the_block_and_none_from_outside_it(self):
            fleet = self.launch(BRIEF, "rev-a")
            kept = fleet.get("required_checks") or []
            self.assertTrue(all(c in kept for c in CHECKS), "the launch record holds every check line of the block")
            self.assertFalse([c for c in kept if "outside" in c], "no line from outside the block")

        def test_s2_a_brief_without_the_block_gives_no_list(self):
            fleet = self.launch("LOCAL REVIEWER. PR #5.\n- not a block\n", "rev-b")
            self.assertNotIn("required_checks", fleet)

        # S3
        def test_s3_the_notice_names_each_missing_check_and_none_of_the_answered_as_missing(self):
            body = ("Review of %s: APPROVE\nproduction-mutant sample: ran 4 mutants, all caught\nrestart test\nnothing else\n" % SHA)
            item = self.route({"required_checks": CHECKS}, body)
            note = item.summary.split("MISSING:", 1)[1]
            self.assertIn("restart test", note, "a bare mention is not an answer")
            self.assertIn("docs paragraph", note)
            self.assertNotIn("production-mutant sample", note, "an answered check is not named as missing")
            self.assertEqual(item.summary.count("required checks:"), 1, "one notice")

        def test_s3_a_full_answer_names_nothing_missing(self):
            body = "Review of %s: APPROVE\n%s\n" % (SHA, "\n".join("%s: done, passes" % c for c in CHECKS))
            self.assertIn("MISSING: none", self.route({"required_checks": CHECKS}, body).summary)

        def test_s3_not_done_with_a_reason_is_answered_and_shown_without_one_it_is_missing(self):
            with_reason = module().compare("restart test: not done, the pod was not available", ["restart test"])
            without = module().compare("restart test: not done", ["restart test"])
            self.assertEqual((with_reason["missing"], with_reason["not_done"]), ([], ["restart test"]))
            self.assertEqual(without["missing"], ["restart test"], "not done needs a reason")

        def test_review_note_scrubs_a_signed_link_in_a_check_name(self):
            name = "[x](http://evil/p?token=SECRETVALUE1234)"
            item = self.route({"required_checks": [name]}, "Review of %s: APPROVE\nnothing\n" % SHA)
            self.assertIn("required checks:", item.summary, "the note is on the line")
            self.assertNotIn("SECRETVALUE1234", item.summary, "a signed link never reaches the line")

        def test_prose_after_the_name_is_not_an_answer_but_a_separated_result_is(self):
            prose = module().compare("restart test was required but I did not run it\n", ["restart test"])
            self.assertIn("restart test", prose["missing"], "prose after the name is not a result")
            for line in ("restart test: ran, passes", "restart test - ran, passes", "**restart test**: passes"):
                self.assertEqual(module().compare(line, ["restart test"])["missing"], [], line)

        def test_parse_keeps_no_control_character_in_a_name(self):
            found = module().parse("REQUIRED CHECKS:\n- red\x1b[31m team\x07 check\n- plain\n")
            self.assertTrue(found, "the block is read")
            self.assertFalse([c for c in found if any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in c)], "no control character in a stored name")
            self.assertIn("plain", found)

        def test_a_failing_comparison_keeps_todays_notice_and_leaves_an_audit_line(self):
            with mock.patch.object(module(), "notice", side_effect=RuntimeError("boom-marker")):
                item = self.route({"required_checks": CHECKS}, "Review of %s: APPROVE\nx\n" % SHA)
            self.assertEqual(item.summary, "base summary", "the notice stays today's notice")
            failed = [a for a in self.audits if "checks_note" in str(a[0])]
            self.assertTrue(failed, "the failure is audited")
            self.assertIn("boom-marker", str(failed[0]), "the audit line names the failure")

        def test_the_name_matches_as_a_whole_phrase(self):
            inside = module().compare("latest: passes\nattestation: ok\n", ["test"])
            self.assertIn("test", inside["missing"], "a name inside a longer word is not a mention")
            self.assertEqual(module().compare("TEST: passes\n", ["test"])["missing"], [], "a whole phrase matches, any case")

        # U1
        def test_u1_a_reader_with_no_list_changes_nothing(self):
            body = "Review of %s: APPROVE\nanything\n" % SHA
            for fleet in ({}, {"required_checks": []}):
                self.assertEqual(self.route(fleet, body).summary, "base summary", "no list changes nothing")

        def test_u1_the_parse_is_bounded_and_reads_text_only(self):
            many = "REQUIRED CHECKS:\n" + "".join("- c%d %s\n" % (n, "x" * 500) for n in range(100))
            found = module().parse(many)
            self.assertTrue(0 < len(found) <= module().MAX_CHECKS)
            self.assertTrue(all(len(c) <= module().MAX_CHECK_CHARS for c in found))
            self.assertEqual(module().parse("no block here\n- a line\n"), [])
            self.assertEqual(module().parse(None), [])

    class TestRequiredChecksGuards(TwoRepoCase):
        """The guards the mutants must break: each asserts the check's own text FIRST."""

        def test_guard_launch_record(self):
            self.assertEqual(module().parse(BRIEF), CHECKS, "the block is kept in the launch record")

        def test_guard_fleet_record(self):
            self.assertEqual(sb.fleet_record("n", "reviewer", None, 20, required_checks=CHECKS).get("required_checks"), CHECKS, "the fleet record keeps the list")

        def test_guard_bare_mention(self):
            self.assertEqual(module().compare("restart test\n", ["restart test"])["missing"], ["restart test"], "a bare mention is not an answer")

        def test_guard_not_done_reason(self):
            self.assertEqual(module().compare("restart test: not done\n", ["restart test"])["missing"], ["restart test"], "not done needs a reason")

        def test_guard_no_list(self):
            self.assertEqual(module().notice("anything", []), "", "no list changes nothing")

    class TestRequiredChecksMutants(unittest.TestCase):
        """Mutants on a SOURCE COPY of the repository, each applied once, each run beside its control."""

        MUTANTS = (("drops the block", "test_guard_launch_record", "the block is kept in the launch record",
                    (("bin/required_checks.py", "if line.strip() == MARK:", "if False and line.strip() == MARK:"),)),
                   ("launch drops the list", "test_guard_fleet_record", "the fleet record keeps the list",
                    (("bin/fleet-switchboard", 'fleet["required_checks"] = list(required_checks)', 'fleet["required_checks"] = []'),)),
                   ("a bare mention answers", "test_guard_bare_mention", "a bare mention is not an answer",
                    (("bin/required_checks.py", 'return "result" if re.search(r"[A-Za-z0-9]", rest) else None', 'return "result"'),)),
                   ("not done needs no reason", "test_guard_not_done_reason", "not done needs a reason",
                    (("bin/required_checks.py", 'return "not_done" if re.search(r"[A-Za-z0-9]", rest[len("not done"):]) else "no_reason"', 'return "not_done"'),)),
                   ("an absent list is all missing", "test_guard_no_list", "no list changes nothing",
                    (("bin/required_checks.py", "    if not checks:\n        return \"\"\n", "    if False:\n        return \"\"\n"),)))

        def copy(self):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, True)
            shutil.copytree(REPO, tmp / "repo", ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
            return tmp / "repo"

        def run_guard(self, root, name):
            env = dict(os.environ, FLEET_HEAVY="off", FLEETKIT_MUTANT_COPY="1")
            proc = subprocess.run([sys.executable, str(root / "bin" / "test-switchboard"), "TestRequiredChecksGuards." + name], capture_output=True, text=True, env=env, cwd=str(root), timeout=600)
            return proc.returncode, proc.stdout + proc.stderr

        def test_each_mutant_is_applied_once_fails_its_named_guard_with_its_text_and_its_control_passes(self):
            if os.environ.get("FLEETKIT_MUTANT_COPY"):
                self.skipTest("inside a source copy")
            for label, guard, anchor, edits in self.MUTANTS:
                with self.subTest(mutant=label):
                    root = self.copy()
                    control_code, control_out = self.run_guard(root, guard)   # the control FIRST: the unmutated copy passes
                    self.assertEqual(control_code, 0, control_out[-1500:])
                    for path, old, new in edits:
                        source = (root / path).read_text()
                        self.assertEqual(source.count(old), 1, "%s: applied once" % path)
                        (root / path).write_text(source.replace(old, new))
                    code, out = self.run_guard(root, guard)
                    self.assertNotEqual(code, 0, "the mutant survived")
                    self.assertIn(guard, out)
                    self.assertIn(anchor, out)

    return TestRequiredChecks, TestRequiredChecksGuards, TestRequiredChecksMutants


def sb_gh():
    from support.scenario_roles import FAKE_GH
    return FAKE_GH

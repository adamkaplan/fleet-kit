"""The #1350 launch brief-check tests, moved out of bin/test-switchboard. build() is called by bin/test-switchboard, which holds TwoRepoCase."""
import json
import os
import subprocess
from unittest import mock
from support.loader import captured, sb
from support.paths import BIN
from support.installs_and_keys import ACME_WEB
from support.scenario_roles import FAKE_GH


def build(TwoRepoCase, RefusedCase):
    class TestLaunchBriefCheck(TwoRepoCase):
        """#1350: `launch --issue N` refuses a brief naming another issue or a stale PR head; nothing is started."""
        HEAD = "abc1234def5678abc1234def5678abc1234def56"

        def setUp(self):
            super().setUp()
            self.work = self.home / "checkout"
            self.work.mkdir(exist_ok=True)
            os.environ.pop("HERDR_PANE_ID", None)
            patcher = mock.patch.object(sb, "launch_memory_check", lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.reads = []
            self.pr_error = None

        def main(self, *argv):
            def route(argv, **kwargs):
                if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
                    self.reads.append(argv[3])
                    if self.pr_error:
                        return subprocess.CompletedProcess(argv, 1, "", self.pr_error)
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"headRefOid": self.HEAD, "state": "OPEN"}), "")
                return self.gh(argv, **kwargs) if argv[0] == FAKE_GH else self.world.route(argv, **kwargs)
            with captured() as (out, err):
                code = sb.main(list(argv), runner=route, now=self.world.clock.now, wait=lambda sock, s: False, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def launch(self, brief, *extra, name="web-new"):
            path = self.home / ("brief-%s.md" % len(self.reads) + name)
            path.write_text(brief)
            return self.main("launch", name, "--agent", "coder", "--dir", str(self.work), "--workspace", "w1", "--role", "coder", "--repo", ACME_WEB,
                             "--issue", "20", "--brief-file", str(path), *extra)

        def started(self):
            return len(self.world.v2.sessions)

        def brief(self, issue="20", head=None):
            return ("PLAN: https://github.com/acme/web/issues/%s#issuecomment-1\nISSUE: https://github.com/acme/web/issues/%s\n"
                    "LOCAL CODER. PR #21 at head %s.\n" % (issue, issue, head or self.HEAD[:7]))

        def test_s2_a_matching_brief_is_accepted(self):
            before = self.started()
            code, _, err = self.launch(self.brief())
            self.assertEqual((code, self.started() - before), (0, 1), err)

        def test_s3_a_stale_head_is_refused_naming_both(self):
            before = self.started()
            code, _, err = self.launch(self.brief(head="1111111"))
            self.assertEqual((code, self.started() - before), (2, 0), err)
            self.assertIn("head", err)
            self.assertIn("1111111", err)
            self.assertIn(self.HEAD, err)

        def test_s4_another_issue_is_refused_naming_both(self):
            before = self.started()
            code, _, err = self.launch(self.brief(issue="30"))
            self.assertEqual((code, self.started() - before), (2, 0), err)
            self.assertIn("30", err)
            self.assertIn("20", err)

        def test_s5_an_unreadable_pr_is_refused_as_unverified(self):
            self.pr_error = "HTTP 502 bad gateway"
            before = self.started()
            code, _, err = self.launch(self.brief())
            self.assertEqual((code, self.started() - before), (2, 0), err)
            self.assertIn("unverified", err)

        def test_s6_the_override_needs_a_reason_and_records_it(self):
            before = self.started()
            code, _, err = self.launch(self.brief(head="1111111"), "--allow-brief-mismatch", "head moved on purpose")
            self.assertEqual((code, self.started() - before), (0, 1), err)
            (entry,) = [e for e in self.world.audit() if e["event"] == "launch.brief_mismatch_allowed"]
            self.assertEqual(entry["detail"]["reason"], "head moved on purpose")
            code, _, err = self.launch(self.brief(head="1111111"), "--allow-brief-mismatch", " ", name="web-new2")
            self.assertEqual((code, self.started() - before), (2, 1), err)

        def test_s7_a_brief_with_no_such_line_is_accepted_and_reads_no_pr(self):
            before = self.started()
            code, _, err = self.launch("Add a health check\nSee issue 99 in prose.\n")
            self.assertEqual((code, self.started() - before, self.reads), (0, 1, []), err)

        def test_u1_a_launch_without_issue_reads_no_pr(self):
            path = self.home / "b.md"
            path.write_text(self.brief(issue="30", head="1111111"))
            code, _, err = self.main("launch", "web-new", "--agent", "coder", "--dir", str(self.work), "--workspace", "w1", "--role", "coder",
                                     "--repo", ACME_WEB, "--brief-file", str(path))
            self.assertEqual((code, self.reads), (0, []), err)

        def test_the_derived_exit_check_fails_without_the_mismatch_row(self):
            ident = " > mismatch|return EXIT_USAGE"
            case = RefusedCase("test_s6_every_refusal_exit_derived_from_the_source_has_a_row_and_sends_exactly_one_message")
            case.setUp()
            self.addCleanup(case.doCleanups)
            derived = sorted(i for i, _ in case.derived_exits())
            rows = case.rows()
            self.assertEqual(derived, sorted(rows))
            without = {k: v for k, v in rows.items() if k != ident}
            self.assertNotEqual(derived, sorted(without))
            argv, patches = rows[ident]
            text, _ = case.check_refused(argv, patches, ident)
            self.assertEqual(len(case.sent()), 1)

        def test_the_docs_say_it(self):
            text = (BIN.parent / "docs" / "switchboard.md").read_text()
            self.assertIn("--allow-brief-mismatch", text)
            self.assertIn("at head", text)

    return TestLaunchBriefCheck

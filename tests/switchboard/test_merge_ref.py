"""`fleet-switchboard merge-ref` (#2053): the first parent of a pull request's merge ref against the head of its base; reads only."""
import json
import re
import subprocess
import sys
from pathlib import Path

from support.loader import captured, sb

HOST = Path(__file__).resolve().parents[2] / "bin" / "switchboard" / "agents_verify.py"
OLD, NEW, SECOND = "a" * 40, "b" * 40, "c" * 40
PR = "o/r#5"


class FakeDone:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def parents(first, second=SECOND):
    return json.dumps([{"sha": first}, {"sha": second}])


def build(HandoffCase):
    class TestMergeRef(HandoffCase):
        """#2053: fresh when the first parent is the base head, STALE otherwise; --refresh is one query and one more read; conflict exits 3."""

        def setUp(self):
            super().setUp()
            self.calls = []
            self.refs = [parents(NEW)]
            self.base_head = NEW
            self.ref_error = None
            self.garbage = False

        def runner(self, argv, **kwargs):
            self.calls.append(argv)
            if argv[:2] == ["gh", "api"] and "/commits/refs/pull/" in argv[2]:
                if self.ref_error:
                    return FakeDone(1, "", self.ref_error)
                if self.garbage:
                    return FakeDone(0, "<html>")
                return FakeDone(0, self.refs.pop(0) if len(self.refs) > 1 else self.refs[0])
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/pulls/5"):
                return FakeDone(0, "integration\n")
            if argv[:2] == ["gh", "api"] and "/branches/integration" in argv[2]:
                return FakeDone(0, self.base_head + "\n")
            if argv[:3] == ["gh", "pr", "view"]:
                return FakeDone(0, '{"mergeStateStatus":"CLEAN"}')
            raise AssertionError("an unexpected call: %r" % (argv,))

        def run_cmd(self, *argv):
            with captured() as (out, err):
                code = sb.main(["merge-ref", PR] + list(argv), runner=self.runner, now=self.world.clock.now)
            return code, out.getvalue(), err.getvalue()

        def writes(self):
            return [c for c in self.calls if c[:3] == ["gh", "pr", "close"] or c[:3] == ["gh", "pr", "reopen"] or c[:3] == ["gh", "pr", "edit"]
                    or "--method" in c or "-X" in c]

        def test_s2_the_first_parent_of_the_merge_ref_is_the_base_is_fresh(self):
            code, out, _ = self.run_cmd()
            self.assertEqual(code, 0)
            self.assertEqual(len([l for l in out.splitlines() if "fresh" in l]), 1, "the first parent of the merge ref is the base")
            self.assertEqual(self.writes(), [])

        def test_s2_an_older_first_parent_is_stale_with_both_short_shas(self):
            self.refs = [parents(OLD)]
            code, out, _ = self.run_cmd()
            self.assertEqual(len([l for l in out.splitlines() if "STALE" in l and OLD[:8] in l and NEW[:8] in l]), 1)
            self.assertNotIn("fresh", out)

        def test_s2_a_second_parent_that_is_the_base_does_not_make_it_fresh(self):
            self.refs = [parents(OLD, NEW)]
            _, out, _ = self.run_cmd()
            self.assertIn("STALE", out, "the first parent of the merge ref is the base")

        def test_s3_refresh_makes_one_query_reads_the_ref_twice_and_writes_nothing(self):
            self.refs = [parents(OLD), parents(NEW)]
            code, out, _ = self.run_cmd("--refresh")
            self.assertEqual(code, 0)
            self.assertEqual(len([c for c in self.calls if c[:3] == ["gh", "pr", "view"] and "mergeStateStatus" in c]), 1)
            self.assertEqual(len([c for c in self.calls if "/commits/refs/pull/" in " ".join(c)]), 2)
            self.assertEqual(self.writes(), [])
            self.assertIn("fresh", out)

        def test_s3_refresh_that_stays_old_says_still_stale(self):
            self.refs = [parents(OLD)]
            _, out, _ = self.run_cmd("--refresh")
            self.assertIn("still STALE", out)

        def test_s4_a_conflict_has_no_merge_ref_one_line_exit_3(self):
            self.ref_error = "gh: Not Found (HTTP 404)"
            code, out, err = self.run_cmd()
            self.assertEqual(code, 3)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertIn("conflicts", out)
            self.assertNotIn("fresh", out)
            self.assertEqual(self.writes(), [])

        def absent_runner(self, pull):
            """A runner whose ref read is a 404 and whose pull read answers `pull` (a JSON text, or None for a 404)."""
            def run(argv, **kwargs):
                self.calls.append(argv)
                if argv[:2] == ["gh", "api"] and "/commits/refs/pull/" in argv[2]:
                    return FakeDone(1, "", "gh: Not Found (HTTP 404)")
                if argv[:2] == ["gh", "api"] and argv[2].endswith("/pulls/5"):
                    return FakeDone(0, pull) if pull is not None else FakeDone(1, "", "gh: Not Found (HTTP 404)")
                raise AssertionError("an unexpected call: %r" % (argv,))
            return run

        def run_absent(self, pull):
            with captured() as (out, err):
                code = sb.main(["merge-ref", PR], runner=self.absent_runner(pull), now=self.world.clock.now)
            return code, out.getvalue(), err.getvalue()

        def test_s4a_a_pull_request_that_does_not_exist_is_named_exit_1(self):
            code, out, err = self.run_absent(None)
            self.assertEqual(code, 1)
            self.assertNotIn("conflicts", out + err)
            self.assertIn("does not exist", out + err)
            self.assertEqual(len((out + err).strip().splitlines()), 1)

        def test_s4b_a_conflicting_pull_request_is_the_conflict_line_exit_3(self):
            for body in ('{"m":false,"s":"dirty"}', '{"m":false,"s":"unknown"}'):
                code, out, _ = self.run_absent(body)
                self.assertEqual(code, 3)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.assertIn("conflicts", out)

        def test_s4c_a_ref_not_built_yet_says_run_refresh_exit_1(self):
            code, out, err = self.run_absent('{"m":null,"s":"unknown"}')
            self.assertEqual(code, 1)
            self.assertNotIn("conflicts", out + err)
            self.assertIn("no merge ref yet (GitHub has not built it): run merge-ref --refresh", out + err)
            self.assertEqual(len((out + err).strip().splitlines()), 1)

        def test_s4_an_unreadable_answer_is_named_and_changes_nothing(self):
            self.garbage = True
            code, out, err = self.run_cmd()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("not a list of parents", err)
            self.assertEqual(self.writes(), [])
            self.ref_error = "HTTP 502 bad gateway"
            self.garbage = False
            code, out, err = self.run_cmd()
            self.assertEqual(code, 1)
            self.assertIn("502", err)

        def section_namespace(self, mutate):
            """The merge-ref section of the host, textually replaced when `mutate`, run in a namespace of its own (nothing on disk)."""
            text = HOST.read_text()
            section = text[text.index("# ---- merge-ref (#2053)"):text.index("# ---- rehearse (#1785)")]
            if mutate:
                self.assertEqual(section.count("first = parents[0]"), 1)
                section = section.replace("first = parents[0]", "first = parents[1]")
            space = {"re": re, "json": json, "sys": sys, "subprocess": subprocess, "_one_line": lambda t, n=300: " ".join(str(t).split())[:n],
                     "load_module": lambda name: type("M", (), {"parse_bounded": staticmethod(json.loads)}), "PROG": "x", "EXIT_OK": 0, "EXIT_ERROR": 1}
            exec(compile(section, "merge-ref-section", "exec"), space)
            return space

        def first_parent_is_base(self, space):
            """The test of S2: a ref whose first parent is the base head prints fresh."""
            args = type("A", (), {"pr": PR, "repo": None, "refresh": False})
            with captured() as (out, err):
                space["cmd_merge_ref"](args, self.runner, None)
            self.assertIn("fresh", out.getvalue(), "the first parent of the merge ref is the base")

        def test_s5_the_unmutated_control_passes_first_then_the_second_parent_mutant_fails(self):
            self.refs = [parents(NEW, OLD)]
            self.first_parent_is_base(self.section_namespace(False))   # the control
            with self.assertRaises(AssertionError) as caught:
                self.first_parent_is_base(self.section_namespace(True))
            self.assertIn("the first parent of the merge ref is the base", str(caught.exception))

        def test_u1_the_section_has_no_write_call(self):
            text = HOST.read_text()
            section = text[text.index("# ---- merge-ref (#2053)"):text.index("# ---- rehearse (#1785)")]
            for word in ('"close"', '"reopen"', '"edit"', '"--method"', '"comment"'):
                self.assertNotIn(word, section)

    return TestMergeRef

"""#1731: `charter set` for an orchestrator whose registry row repo is another organization's repo (S2 to S9). The REAL IntentReader.parent/issue over a fake _api."""
import contextlib
import inspect
import textwrap
import unittest
from unittest import mock

from support.loader import sb

WIDGET = "acme-org/widget-service"
OTHER = "acme-org/other-service"
import itertools
COUNTER = itertools.count(1)
LIVE = sb.IntentError("not_found", "gh: No parent issue found (HTTP 404)")


def build(CharterRig, Base):
    real_parent, real_issue = sb.IntentReader.parent, sb.IntentReader.issue

    class TestCharterOtherRepo(CharterRig, Base):
        def lane(self, name, repo=WIDGET):
            self.orchestrator(name, registry=None)
            self.registry.upsert(self.world.state, name, dict(self.row(name), charter=None, repo=repo))
            return name

        def labelled(self, **kw):
            return dict({"number": 7, "labels": [{"name": sb.orchestrator_label()}]}, **kw)

        def recorded(self, *lanes):
            return sum(1 for n in lanes if self.row(n)["charter"] is not None)

        def run_set(self, lane, value, parent, issue=None, mutant=None):
            """One `charter set` through the real reads; the fake _api answers only for the row's repo. Returns (code, err, parent read repos)."""
            seen = []
            if value.endswith("#7") or value == "7":   # one charter number per lane, so no lane holds another's
                value = value[:-1] + str(7 + next(COUNTER))
            issue = self.labelled() if issue is None else issue

            def api(reader, path, repo=None):
                if repo != WIDGET:
                    raise sb.IntentError("not_found", "wrong repo %r" % (repo,))
                if path.endswith("/parent"):
                    seen.append(repo)
                answer = parent if path.endswith("/parent") else issue
                if isinstance(answer, Exception):
                    raise answer
                return answer
            patches = [mock.patch.object(sb.IntentReader, "issue", real_issue), mock.patch.object(sb.IntentReader, "parent", real_parent), mock.patch.object(sb.IntentReader, "_api", api)]
            if mutant is not None:
                patches.append(mock.patch.object(sb, "cmd_charter", mutant))
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                code, out, err = self.run_as("cos", "charter", "set", lane, value)
            return code, err, seen

        def test_s2_the_row_repo_is_read_and_the_live_404_is_no_parent(self):
            lane = self.lane("orch-s2")
            code, err, seen = self.run_set(lane, "7", LIVE)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.recorded(lane), 1)
            self.assertEqual(seen, [WIDGET])

        def test_s3_ask_pr_and_unlabelled_are_refused(self):
            for name, parent, issue, text in (("a", {"number": 1}, None, "is an ask"), ("b", LIVE, self.labelled(pull_request={"url": "x"}), "is a pull request"),
                                              ("c", LIVE, {"number": 7, "labels": [{"name": "bug"}]}, "lacks the")):
                lane = self.lane("orch-s3" + name)
                code, err, _ = self.run_set(lane, "7", parent, issue)
                with self.subTest(name):
                    self.assertIn(text, err)
                    self.assertEqual(self.recorded(lane), 0)

        def test_s4_the_forms_of_the_value(self):
            a, b, c = self.lane("orch-s4a"), self.lane("orch-s4b"), self.lane("orch-s4c")
            code, err, _ = self.run_set(a, WIDGET + "#7", LIVE)
            self.assertEqual((code, self.recorded(a)), (0, 1), err)
            code, err, _ = self.run_set(b, OTHER + "#7", LIVE)
            self.assertIn(WIDGET, err)
            self.assertIn(OTHER, err)
            self.assertEqual(self.recorded(b), 0)
            code, err, _ = self.run_set(c, "7", LIVE)
            self.assertEqual((code, self.recorded(c)), (0, 1), err)

        def test_s5_a_reader_of_the_default_repo_is_caught(self):
            old = "engine.intents.parent(value, repo0, strict=True)"
            source = textwrap.dedent(inspect.getsource(sb.cmd_charter))
            self.assertEqual(source.count(old), 1)
            scope = dict(vars(sb))
            exec(compile(source.replace(old, "engine.intents.parent(value, None, strict=True)"), "<mutant>", "exec"), scope)
            scope["cmd_charter"].ensure_on_use = True
            control, mutated = self.lane("orch-s5a"), self.lane("orch-s5b")
            self.run_set(control, "7", LIVE)
            self.assertEqual(self.recorded(control), 1)   # the unmutated control first
            self.run_set(mutated, "7", LIVE, mutant=scope["cmd_charter"])
            with self.assertRaisesRegex(AssertionError, r"^0 != 1"):
                self.assertEqual(self.recorded(mutated), 1)

        def test_s6_the_live_text_and_an_odd_404(self):
            a, b = self.lane("orch-s6a"), self.lane("orch-s6b")
            self.run_set(a, "7", LIVE)
            self.assertEqual(self.recorded(a), 1)
            code, err, _ = self.run_set(b, "7", sb.IntentError("not_found", "gh: repository moved (HTTP 404)"))
            self.assertIn("that is not the no-parent answer".replace(" ", "?"), err)   # charter_safe turns every space of the reason into ?
            self.assertEqual(self.recorded(b), 0)

        def test_s8_a_missing_issue_is_refused_after_the_parent_read(self):
            miss = sb.IntentError("not_found", "gh: Not Found (HTTP 404)")
            a, b = self.lane("orch-s8a"), self.lane("orch-s8b")
            code, err, _ = self.run_set(a, "7", miss, miss)
            self.assertIn("cannot read", err)
            self.assertEqual(self.recorded(a), 0)
            self.run_set(b, "7", miss)
            self.assertEqual(self.recorded(b), 1)

        def test_s9_the_older_answers_still_count_as_no_parent(self):
            lanes = []
            for k, text in enumerate(("gh: Not Found (HTTP 404)", "Not Found", "HTTP 404 no parent")):
                lanes.append(self.lane("orch-s9%d" % k))
                self.run_set(lanes[-1], "7", sb.IntentError("not_found", text))
            self.assertEqual(self.recorded(*lanes), 3)

    return TestCharterOtherRepo

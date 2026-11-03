"""#1789: `fleet-switchboard plan apply` applies an approved plan edit and refuses anything that was not approved. Offline: a FAKE issue.
build(HandoffCase) takes the HandoffCase of bin/test-switchboard and returns the test classes."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from unittest import mock

from support.loader import captured, sb
from support.scenario_roles import FAKE_GH, FAKE_REPO


def build(HandoffCase):
    REPO_NAME, GH = FAKE_REPO, FAKE_GH
    mark = lambda name: "<!-- fleet-switchboard:from=%s -->" % name
    def plan_apply_text():
        text = (ROOT / "bin" / "switchboard" / "plan_input.py").read_text()
        return text[text.index("# ---- plan apply (#1789)"):]
    ROOT = Path(__file__).resolve().parents[2]
    PLAN = ("## Plan (scenarios)\n\nsize: S\n\nunchanged: none because this is a brand new endpoint with no callers\n\n```yaml\nclauses:\n  DW-1: \"the thing works\"\n```\n\nTier S. The thing is small.\n"
            "Alpha line. Beta line. Beta line.\n\n```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: Given a thing, when used, then it works\n"
            "    proof: test\n    pass: count equals 1 pass\n    discriminates: a build that does nothing shows count equals 0 and fails\n"
            "    evaluator: ci\n```\n\n" + mark("cos"))
    FORM = "===== OLD %d (verbatim) =====\n%s\n===== NEW %d (verbatim) =====\n%s\n"

    class FakeIssue:
        """A fake GitHub issue of comments, one login for every lane; records every request, writes and body requests."""

        def __init__(self):
            self.store, self.writes, self.calls, self.fail_patch, self.fail_read = {}, [], [], False, False

        def add(self, ident, body, issue=7):
            self.store[ident] = {"id": ident, "issue": issue, "body": body, "created_at": "2027-01-01T00:00:%02dZ" % ident,
                                 "user": {"login": "shared-account"}, "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (REPO_NAME, issue, ident)}

        def __call__(self, argv, **kwargs):
            path, method = argv[2], (argv[argv.index("--method") + 1] if "--method" in argv else "GET")
            self.calls.append((method, path))
            one = re.match(r"^repos/%s/issues/comments/(\d+)$" % re.escape(REPO_NAME), path)
            many = re.match(r"^repos/%s/issues/(\d+)/comments(\?.*)?$" % re.escape(REPO_NAME), path)
            if one and method == "GET":
                return self.answer(argv, self.store.get(int(one.group(1))))
            if one and method == "PATCH":
                if self.fail_patch:
                    return subprocess.CompletedProcess(argv, 1, "", "gh: Forbidden (HTTP 403)\n")
                self.writes.append(("PATCH", int(one.group(1))))
                self.store[int(one.group(1))]["body"] = json.loads(kwargs["input"])["body"]
                return self.answer(argv, self.store[int(one.group(1))])
            if many and method == "GET":
                if self.fail_read:
                    return subprocess.CompletedProcess(argv, 1, "", "gh: Server Error (HTTP 500)\n")
                return self.answer(argv, [c for c in self.store.values() if c["issue"] == int(many.group(1))])
            if many and method == "POST":
                self.writes.append(("POST", int(many.group(1))))
                ident = max(self.store, default=0) + 1
                self.add(ident, json.loads(kwargs["input"])["body"], int(many.group(1)))
                return self.answer(argv, self.store[ident])
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")

        def answer(self, argv, data):
            if data is None:
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

        def requests_to_body(self):
            return [c for c in self.calls if re.match(r"^repos/[^/]+/[^/]+/issues/\d+$", c[1])]

    class PlanApplyCase(HandoffCase):
        def setUp(self):
            super().setUp()
            self.gh = FakeIssue()
            self.gh.add(1, PLAN)
            self.ph = sb.plan_check.plan_hash

        def pairs(self):
            return FORM % (1, "Tier S. The thing is small.", 1, "Tier S. The thing is bigger.") + FORM % (2, "Alpha line.", 2, "Gamma line.\n\nWhy: because the Chief approved it.")

        def expected(self, old_texts):
            body = self.gh.store[1]["body"]
            for old, new in old_texts:
                body = body.replace(old, new)
            body = re.sub(r"<!--\s*fleet-switchboard:from=[A-Za-z0-9_-]+\s*-->", "", body).rstrip() + "\n\n" + mark("cos")
            return body

        def good(self):
            return self.expected([("Tier S. The thing is small.", "Tier S. The thing is bigger."), ("Alpha line.", "Gamma line.")])

        def run_apply(self, text=None, live=None, result=None, file=None, issue="7", extra=()):
            path = self.home / "edit.txt"
            path.write_text(self.pairs() if text is None else text)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            argv = ["plan", "apply", issue, "--file", str(path), "--expect-live", live or self.ph(self.gh.store[1]["body"]),
                    "--expect-result", result or self.ph(self.good()), "--expect-file", file or sha] + list(extra)
            with captured() as (out, err):
                code = sb.main(argv, runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def refused_once(self, **kw):
            before = list(self.gh.writes)
            code, out, err = self.run_apply(**kw)
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertEqual(self.gh.writes, before)
            return err

    class TestPlanApply(PlanApplyCase):
        def refusals(self):
            twice = FORM % (1, "Beta line.", 1, "Delta line.")
            return [("--expect-live", dict(live="sha256:" + "0" * 64)), ("--expect-file", dict(file="1" * 64)),
                    ("OLD 1 occurs 2 times", dict(text=twice, result="sha256:" + "2" * 64)), ("--expect-result", dict(result="sha256:" + "3" * 64))]

        def scenario_s2(self):
            """Four refusals, one per expectation, then the success; the count of lines and writes."""
            for name, kw in self.refusals():
                err = self.refused_once(**kw)
                if name not in err:
                    raise AssertionError("%s was not named in: %s" % (name, err))
            code, out, err = self.run_apply()
            if code != 0:
                raise AssertionError("the right run was refused: %s" % err)
            if self.gh.writes != [("PATCH", 1), ("POST", 7)]:
                raise AssertionError("writes %r" % (self.gh.writes,))

        def test_s2_four_refusals_one_per_expectation_and_one_success(self):
            self.scenario_s2()

        def test_s3_success_patches_once_signs_once_prints_the_minute_and_hash_and_posts_the_receipt(self):
            code, out, err = self.run_apply()
            self.assertEqual(code, 0, err)
            self.assertEqual(self.gh.writes, [("PATCH", 1), ("POST", 7)])
            live = self.gh.store[1]["body"]
            self.assertEqual(live, self.good())
            self.assertEqual(live.count("fleet-switchboard:from="), 1)
            self.assertIn(self.ph(live), out)
            self.assertEqual(len(re.findall(r"sha256:[0-9a-f]{64}", out)), 1)
            self.assertRegex(out, r"applied at \d{4}-\d\d-\d\dT\d\d:\d\dZ")
            self.assertEqual(out.count("plan check: PASS"), 1, out)
            receipt = self.gh.store[2]["body"]
            self.assertEqual(receipt.splitlines()[0], "<!-- plan-receipt -->")
            self.assertEqual(receipt.splitlines()[1], "plan hash: " + self.ph(live))
            self.assertIsNone(sb.plan_check.receipt_problem({"body": receipt}))
            self.assertIn("because the Chief approved it", receipt)
            self.assertNotIn("Gamma line", receipt)

        def test_s4_the_pairs_are_printed_by_the_run_that_applies(self):
            code, out, err = self.run_apply()
            self.assertEqual(code, 0, err)
            self.assertIn("OLD 1 (27 characters): Tier S. The thing is small.", out)
            self.assertIn("NEW 2 (11 characters): Gamma line.", out)
            self.assertEqual(len([l for l in out.splitlines() if re.match(r"^(OLD|NEW) \d \(\d+ characters\)", l)]), 4)

        def test_s4_other_forms_are_refused_with_the_standard_form_and_nothing_written(self):
            forms = ["EDIT 1\nOLD:\nAlpha line.\nNEW:\nX\n", "OLD:\nAlpha line.\nNEW:\nX\n", "=== OLD 1 ===\nAlpha line.\n=== NEW 1 ===\nX\n"]
            for text in forms:
                code, out, err = self.run_apply(text=text)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertIn("===== OLD <n> (verbatim) =====", err)
            self.assertEqual(self.gh.writes, [])

        def test_s5_a_changed_clauses_block_and_another_lanes_plan_are_refused_and_no_request_reaches_the_body(self):
            err = self.refused_once(text=FORM % (1, "DW-1: \"the thing works\"", 1, "DW-1: \"the thing works less\""), result="sha256:" + "4" * 64)
            self.assertIn("--expect-result", err)   # the hash of that result is not the one named: it is refused first
            changed = self.expected([("DW-1: \"the thing works\"", "DW-1: \"the thing works less\"")])
            err = self.refused_once(text=FORM % (1, "DW-1: \"the thing works\"", 1, "DW-1: \"the thing works less\""), result=self.ph(changed))
            self.assertIn("clauses", err)
            self.gh.store[1]["body"] = PLAN.replace(mark("cos"), mark("platform"))
            err = self.refused_once(live=self.ph(self.gh.store[1]["body"]))
            self.assertIn("signed platform", err)
            self.assertEqual(self.gh.requests_to_body(), [])
            self.assertEqual(self.gh.writes, [])

        def test_u1_each_cause_exits_1_with_one_line_nothing_written_and_no_traceback(self):
            for text in ("a note and no pair\n", FORM % (1, "Alpha line.", 1, "X") + FORM % (3, "Beta", 3, "Y")):
                code, out, err = self.run_apply(text=text)
                self.assertEqual((code, len(err.strip().splitlines())), (1, 1), err)
                self.assertNotIn("Traceback", err)
            self.gh.fail_read = True
            code, out, err = self.run_apply(live="sha256:" + "0" * 64)
            self.assertEqual((code, len(err.strip().splitlines())), (1, 1), err)
            self.assertNotIn("Traceback", err)
            self.gh.fail_read = False
            self.gh.fail_patch = True
            code, out, err = self.run_apply()
            self.assertEqual((code, len(err.strip().splitlines())), (1, 1), err)
            self.assertIn("nothing is changed", err)
            self.assertEqual(self.gh.writes, [])
            self.assertEqual(self.gh.store[1]["body"], PLAN)

        def test_u1_an_approved_plan_is_edited_with_the_approval_derived_and_the_old_hash_in_the_receipt(self):
            self.gh.add(5, "Approved at %s\n\n%s" % (self.ph(PLAN), mark("chief")))
            code, out, err = self.run_apply()
            self.assertEqual(code, 0, err)
            self.assertIn(self.ph(PLAN), self.gh.store[6]["body"])

        def test_u3_the_runs_make_one_read_one_patch_one_receipt_and_never_touch_the_body(self):
            self.assertEqual(self.run_apply()[0], 0)
            methods = [m for m, _ in self.gh.calls]
            self.assertEqual(methods.count("PATCH"), 1)
            self.assertEqual(methods.count("POST"), 1)
            self.assertEqual(self.gh.requests_to_body(), [])
            source = plan_apply_text()
            self.assertEqual(len(re.findall(r"""["']api["']""", source)), 0)
            self.assertNotIn("gh api", source.replace("`gh api` call", ""))

        def check_failed_plan_check_is_in_the_receipt(self):
            old, new = "    pass: count equals 1 pass\n", "    pass: it works\n"
            body = self.gh.store[1]["body"]
            result = re.sub(r"<!--\s*fleet-switchboard:from=[A-Za-z0-9_-]+\s*-->", "", body.replace(old, new)).rstrip() + "\n\n" + mark("cos")
            code, out, err = self.run_apply(text=FORM % (1, old.rstrip("\n"), 1, new.rstrip("\n")), result=self.ph(result))
            receipt = self.gh.store[2]["body"] if 2 in self.gh.store else ""
            counts = (self.gh.writes.count(("PATCH", 1)), self.gh.writes.count(("POST", 7)), (out + err).count("plan check: FAIL"), receipt.splitlines().count("plan check: FAIL"), code)
            if counts != (1, 1, 1, 1, 1):
                raise AssertionError("plan check: FAIL line missing: patch, receipt, printed, in receipt, exit = %r\n%s%s" % (counts, out, err))

        def test_u5_a_failing_plan_check_is_patched_receipted_with_the_line_and_exits_1(self):
            self.check_failed_plan_check_is_in_the_receipt()
            self.assertEqual(self.gh.store[1]["body"].count("pass: it works"), 1)

        def test_u5_the_passing_run_has_no_fail_line(self):
            code, out, err = self.run_apply()
            self.assertEqual(code, 0, err)
            self.assertEqual(out.count("plan check: FAIL"), 0)
            self.assertEqual(self.gh.store[2]["body"].count("plan check: FAIL"), 0)

        def test_u5_mutant_that_omits_the_line_fails_with_its_text(self):
            self.run_apply()
            self.gh = FakeIssue(); self.gh.add(1, PLAN)
            real = sb.receipt_text
            with mock.patch.object(sb, "receipt_text", lambda *a: real(*a[:8])):
                with self.assertRaisesRegex(AssertionError, r"plan check: FAIL line missing: patch, receipt, printed, in receipt, exit = \(1, 1, 1, 0, 1\)"):
                    self.check_failed_plan_check_is_in_the_receipt()

        def check_live_was_checked(self):
            code, out, err = self.run_apply(live="sha256:" + "0" * 64)
            if code == 0 or self.gh.writes:
                raise AssertionError("--expect-live was not checked")

        def check_file_was_checked(self):
            code, out, err = self.run_apply(file="1" * 64)
            if code == 0 or self.gh.writes:
                raise AssertionError("--expect-file was not checked")

        def test_s6_the_control_of_the_mutants_passes_first(self):
            self.check_live_was_checked()
            self.check_file_was_checked()

        def test_s6_mutant_that_skips_the_live_check_fails_with_its_text(self):
            self.run_apply()
            self.gh = FakeIssue(); self.gh.add(1, PLAN)
            with mock.patch.object(sb, "expect_live", lambda have, want: None):
                with self.assertRaisesRegex(AssertionError, "--expect-live was not checked"):
                    self.check_live_was_checked()

        def test_s6_mutant_that_skips_the_file_check_fails_with_its_text(self):
            self.run_apply()
            self.gh = FakeIssue(); self.gh.add(1, PLAN)
            with mock.patch.object(sb, "expect_file", lambda have, want: None):
                with self.assertRaisesRegex(AssertionError, "--expect-file was not checked"):
                    self.check_file_was_checked()

        def racing(self):
            """The plan body changes between the apply's read and the signed edit's read (before the PATCH)."""
            gh = self.gh
            real = type(gh).__call__
            state = {"done": False}
            def route(argv, **kwargs):
                if argv[2].endswith("/issues/comments/1") and "--method" not in argv and not state["done"]:
                    state["done"] = True
                    gh.store[1]["body"] = gh.store[1]["body"].replace("Beta line. Beta line.", "Beta line. Beta line. Edited by another hand.")
                return real(gh, argv, **kwargs)
            gh.__class__ = type("RacingIssue", (type(gh),), {"__call__": lambda self, argv, **kw: route(argv, **kw)})

        def check_race_was_refused(self):
            self.racing()
            code, out, err = self.run_apply()
            if code == 0 or "--expect-live" not in err or self.gh.writes:
                raise AssertionError("the changed live plan was not refused: %r %r writes %r" % (code, err, self.gh.writes))
            return err

        def test_race_a_body_changed_before_the_patch_is_refused_naming_expect_live_with_no_patch(self):
            self.gh.fail_read = False
            err = self.check_race_was_refused()
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertEqual([w for w in self.gh.writes if w[0] == "PATCH"], [])

        def test_race_an_unchanged_body_still_applies(self):
            code, out, err = self.run_apply()
            self.assertEqual(code, 0, err)
            self.assertEqual(self.gh.writes, [("PATCH", 1), ("POST", 7)])

    class TestPlanApplyDerivedPlaces(PlanApplyCase):
        HEADING = "Applying an approved plan edit"

        def places(self):
            help_text = self.plain_help()
            skill = (ROOT / "skills/fleet-planning/SKILL.md").read_text()
            template = (ROOT / "docs/plan-template.md").read_text()
            return {"template": template, "help": help_text}, sb.STANDARD_FORM, skill

        def plain_help(self):
            with captured() as (out, err):
                try:
                    sb.main(["plan", "apply", "-h"], runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
                except SystemExit:
                    pass
            return out.getvalue()

        def missing(self, places, form):
            return {name: [l for l in form if l not in text.splitlines()] for name, text in places.items()}

        def test_s7_two_places_carry_the_six_lines_word_for_word_and_the_skill_points_at_them(self):
            places, form, skill = self.places()
            found = [n for n, t in places.items() if self.HEADING in t]
            self.assertEqual(sorted(found), ["help", "template"])
            self.assertEqual(len(form), 6)
            self.assertEqual(self.missing(places, form), {"template": [], "help": []})
            self.assertEqual(skill.count("Apply an approved plan edit with `fleet-switchboard plan apply` (see plan-template)."), 1)
            self.assertTrue(skill.rstrip().endswith("(see plan-template)."))

        def test_s7_a_planted_place_with_one_changed_word_fails_with_the_line_named(self):
            places, form, skill = self.places()
            places["help"] = places["help"].replace("read by nobody", "read by somebody")
            missing = self.missing(places, form)
            self.assertEqual(missing["template"], [])
            self.assertEqual(missing["help"], [form[0]])

        def test_u2_no_second_hash_or_plan_selection_and_plan_check_output_is_the_one_renderer(self):
            source = plan_apply_text()
            self.assertEqual(len(re.findall(r"hashlib\.sha256\(", source)), 1)   # the file's sha256 only
            self.assertNotIn("def plan_hash", source)
            self.assertNotIn("def select_plan_text", source)
            self.assertNotIn("_is_plan_comment", source)
            for text in (PLAN, PLAN.replace("small", "tiny")):
                path = self.home / "p.md"
                path.write_text(text)
                with captured() as (out, err):
                    code = sb.main(["plan", "check", "--file", str(path), "--done-when", "(1) the thing works"], runner=self.route,
                                   now=self.world.clock.now, socket_call=self.world.herdr.socket)
                result = sb.plan_check.check(text, "(1) the thing works", None)
                self.assertEqual(out.getvalue().splitlines()[0], "plan check: " + result["result"].upper())
                self.assertIn("plan hash: " + sb.plan_check.plan_hash(text), out.getvalue())
                self.assertIn("plan check: PASS", out.getvalue())

    return TestPlanApply, TestPlanApplyDerivedPlaces

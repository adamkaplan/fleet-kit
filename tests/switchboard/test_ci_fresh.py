"""The pure decision of bin/ci_fresh.py: a green run counts only on the current tip (ask #1755, S5)."""
import inspect
import json
import os
import subprocess
import sys
import textwrap
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
import ci_fresh  # noqa: E402
import merge_gate  # noqa: E402
import plan_ledger  # noqa: E402
import pr_reading  # noqa: E402

TIP = "a" * 40
OLD = "b" * 40


class TestCiStateDecision(unittest.TestCase):
    def test_a_ci_that_is_not_green_is_not_judged(self):
        for ci in ("failing", "pending", "none"):
            self.assertEqual(ci_fresh.ci_state(ci, OLD, TIP, 3), (None, ""), ci)

    def test_a_green_run_at_the_tip_is_fresh(self):
        state, line = ci_fresh.ci_state("green", TIP, TIP, None)
        self.assertEqual(state, "fresh")
        self.assertTrue(line.startswith("fresh: "), line)

    def test_a_green_run_behind_is_stale_with_the_count_and_the_8_character_bases(self):
        self.assertEqual(ci_fresh.ci_state("green", OLD, TIP, 3),
                         ("stale", "stale: 3 commits behind (CI ran on bbbbbbbb, the tip is aaaaaaaa): close and reopen the pull request for a fresh run"))

    def test_the_commits_behind_not_counted_is_still_stale(self):
        state, line = ci_fresh.ci_state("green", OLD, TIP, None)
        self.assertEqual(state, "stale")
        self.assertTrue(line.startswith("stale: the commits behind were not counted"), line)

    def test_a_green_run_with_no_recorded_base_is_unknown(self):
        for base in (None, ""):
            self.assertEqual(ci_fresh.ci_state("green", base, TIP, None),
                             ("unknown", "unknown: the base of the last run is not recorded"))

    def test_a_green_run_with_an_unreadable_tip_is_unknown(self):
        for tip in (None, ""):
            self.assertEqual(ci_fresh.ci_state("green", OLD, tip, None),
                             ("unknown", "unknown: the tip could not be read"))


    def test_s5_the_decision_table_in_one_pass(self):
        way = ": close and reopen the pull request for a fresh run"
        table = [(("failing", OLD, TIP, 3), (None, "")), (("pending", OLD, TIP, 3), (None, "")), (("none", OLD, TIP, 3), (None, "")),
                 (("green", TIP, TIP, None), ("fresh", "fresh: CI ran on the tip " + TIP[:12])),
                 (("green", OLD, TIP, 3), ("stale", "stale: 3 commits behind (CI ran on bbbbbbbb, the tip is aaaaaaaa)" + way)),
                 (("green", OLD, TIP, None), ("stale", "stale: the commits behind were not counted (%s, %s)%s" % (OLD, TIP, way))),
                 (("green", None, TIP, None), ("unknown", "unknown: the base of the last run is not recorded")),
                 (("green", OLD, None, None), ("unknown", "unknown: the tip could not be read"))]
        for args, want in table:
            self.assertEqual(ci_fresh.ci_state(*args), want, args)


class TestCiFreshDocs(unittest.TestCase):
    """S9: the documentation says what stale means and how a lane gets a fresh run."""

    def test_s9_the_docs_say_what_stale_means_and_how_a_lane_gets_a_fresh_run(self):
        docs = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "switchboard.md")).read()
        start = docs.index("**A green run counts only on the current tip (#1755).**")
        sec = docs[start:docs.index("\n\n", docs.index("**How a lane gets a fresh run (#1755).**"))]
        for need in ("the last run was not made on the current tip", "commits behind", "gh pr close N", "gh pr reopen N",
                     "one full CI run", "`gh run rerun`", "`workflow_dispatch`", "`update-branch`", "on for every repository", "ci_current_tip: false",
                     "stale: N commits behind (CI ran on X, the tip is Y): close and reopen the pull request for a fresh run"):
            self.assertIn(need, sec)



HEAD = "c" * 40


def _host():
    return sys.modules["__main__"]


class CountingGh:
    """A fake of the merge gate's reader: gh(argv) -> (code, out, err), counting each kind of read."""

    def __init__(self, base=OLD, tip=TIP, behind=3, runs="ok", tip_ok=True, compare_ok=True, two_runs=False, newest_first=False):
        self.newest_first = newest_first
        self.base, self.tip, self.behind, self.runs, self.tip_ok, self.compare_ok, self.two_runs = base, tip, behind, runs, tip_ok, compare_ok, two_runs
        self.calls = []

    def __call__(self, argv, own=True):
        self.calls.append(argv)
        path = argv[1] if len(argv) > 1 else ""
        if "/actions/runs" in path:
            if self.runs == "fail":
                return 1, "", "boom"
            def run(i, created, base, wf):
                pr = {"number": 5, "base": {"sha": base}} if base else {"number": 5}
                return {"id": i, "name": "w%d" % wf, "workflow_id": wf, "head_sha": HEAD, "status": "completed", "conclusion": "success",
                        "created_at": created, "pull_requests": [pr]}
            runs = [run(1, "2026-11-01T10:00:00Z", None if self.runs == "nobase" else self.base, 1)]
            if self.two_runs:
                runs.append(run(2, "2026-11-01T11:00:00Z", self.tip, 2))
            return 0, json.dumps({"workflow_runs": runs[::-1] if self.newest_first else runs}), ""
        if "/git/ref/heads/" in path:
            return (0, json.dumps({"object": {"sha": self.tip}}), "") if self.tip_ok else (1, "", "boom")
        if "/compare/" in path:
            return (0, json.dumps({"ahead_by": self.behind}), "") if self.compare_ok else (1, "", "boom")
        raise AssertionError("an unexpected read: %r" % (argv,))

    def kinds(self):
        return [("runs" if "/actions/runs" in a[1] else "tip" if "/git/ref/heads/" in a[1] else "compare") for a in self.calls]


def judge(gh, setting=None, ci="green", base_ref="main", repo="acme/app", number=5, reading=pr_reading, module=ci_fresh, made=None, head=HEAD):
    facts = {"ci": ci, "head": head, "base_ref": base_ref}
    def make_gh():
        if made is not None:
            made.append(1)
        return gh
    return module.fresh_fact(facts, setting if callable(setting) else (lambda: setting), make_gh, reading, repo, number)


def clear_cache():
    """The memory of read_behind (`_seen`, module-global) is cleared, so a test that counts compare reads does not depend on the order of the tests."""
    for mod in (pr_reading, _host().sb.load_module("pr_reading")):
        mod.read_behind.__defaults__[0].clear()


class FreshCase(unittest.TestCase):
    def setUp(self):
        clear_cache()


def g4(facts):
    plan = {"result": "pass", "scenarios_parsed": [], "plan_hash": "sha256:x", "source": "x"}
    table = plan_ledger.evaluate_gates(plan, [], None, dict(facts, head=HEAD), "acme/app#5")
    return next(r for r in table["gates"] if r["gate"] == "G4")


def ci_record(facts):
    ctx = types.SimpleNamespace(unreadable=None, facts=dict(facts, state="OPEN"), plan={"result": "pass", "scenarios_parsed": []}, plan_problem=None,
                                engine=None, pr=5, config={}, repo="acme/app")
    return _host().sb.ci_record_core(ctx, lambda: 0, quiet=True)


def pr_state_text(facts):
    """The REAL `pr-state` text: the entry's own enrich (what the daemon stores of an examination), view and line, from the facts the rule returned."""
    sb = _host().sb
    ref = "acme/app#5"
    entry = {"state": "open", "head": HEAD, "labels": [], "at": 100.0, "listed_at": 100.0}
    state = types.SimpleNamespace(pr_state={ref: entry})
    ctx = types.SimpleNamespace(facts=dict(facts, state="open", checks=[], mergeable="MERGEABLE"), plan={"result": "pass", "scenarios_parsed": []}, entries=[], problem=None, pr=ref)
    sb.pr_state_enrich(state, "acme/app", 5, ctx, 100.0)
    view = sb.pr_state_view(ref, {"prs": state.pr_state, "written_at": 100}, [], 100.0)
    return sb.pr_state_line(view)


def mutant(test, module, old, new, tag):
    """A copy of the module with `old` replaced by `new`; the replaced text is asserted to occur exactly once (the mutation applied once)."""
    src = open(module.__file__).read()
    test.assertEqual(src.count(old), 1, "the mutation applies once: " + old)
    mod = types.ModuleType(tag)
    mod.__dict__["__file__"] = module.__file__
    exec(compile(src.replace(old, new), tag, "exec"), mod.__dict__)
    return mod


class TestCiFreshReaders(FreshCase):
    """S2, S3: the three readers over a stale and a fresh green run (a repo with no `ci_current_tip` setting: the rule is on)."""

    STALE_RUN = '    if run_base == tip:\n        return "fresh"'

    def check_stale(self, module):
        facts = judge(CountingGh(behind=3), module=module)
        row = g4(facts)
        self.assertEqual(row["status"], "missing", "G4 reads a stale run as missing")
        self.assertEqual(row["missing"][0][:23], "stale: 3 commits behind", "G4 says the stale line")
        done = ci_record(facts)
        self.assertEqual(done["status"], "not-green", "ci-record records nothing for a stale run")
        self.assertEqual(done["why"][:23], "stale: 3 commits behind", "ci-record says the stale line")
        text = pr_state_text(facts)
        self.assertIn("CI stale: 3 behind", text, "pr-state prints the stale count")
        self.assertNotIn("CI green", text, "pr-state does not print CI green")

    def test_s2_the_stale_run_is_missing_records_nothing_and_prints_stale(self):
        self.check_stale(ci_fresh)

    def test_s2_the_control_the_unmutated_copy_passes_the_same_check(self):
        self.check_stale(mutant(self, ci_fresh, self.STALE_RUN, self.STALE_RUN, "ci_fresh_control2"))

    def test_s2_the_mutant_that_reads_the_heads_run_only_is_caught(self):
        mut = mutant(self, ci_fresh, self.STALE_RUN, '    if True:\n        return "fresh"', "ci_fresh_mut2")
        with self.assertRaisesRegex(AssertionError, "G4 reads a stale run as missing"):
            self.check_stale(mut)

    def test_s3_a_run_at_the_tip_is_ok_names_the_tip_and_reads_green(self):
        facts = judge(CountingGh(base=TIP))
        row = g4(facts)
        self.assertEqual(row["status"], "ok")
        self.assertIn(TIP[:12], row["detail"])
        self.assertNotEqual(ci_record(facts)["status"], "not-green")
        self.assertIn("CI green", pr_state_text(facts))


class TestCiFreshUnreadable(unittest.TestCase):
    """S4: none of four unreadable states is green."""

    CASES = (("runs", dict(runs="fail"), "unknown", "unknown: the base of the last run is not recorded"),
             ("nobase", dict(runs="nobase"), "unknown", "unknown: the base of the last run is not recorded"),
             ("tip", dict(tip_ok=False), "unknown", "unknown: the tip could not be read"),
             ("compare", dict(compare_ok=False), "stale: the commits", "stale: the commits behind were not counted"))

    NOBASE = 'return "unknown", "unknown: the base of the last run is not recorded"'

    def check_nobase(self, module):
        facts = judge(CountingGh(runs="nobase"), module=module)
        self.assertNotEqual(facts["ci"], "green", "a run with no recorded base is not green")
        self.assertEqual(facts["ci"], "unknown", "a run with no recorded base is unknown")

    def test_s4_each_unreadable_state_is_not_green_with_its_line(self):
        for name, kw, ci, line in self.CASES:
            facts = judge(CountingGh(**kw))
            self.assertNotEqual(facts["ci"], "green", name)
            self.assertTrue(facts["ci_line"].startswith(line), (name, facts["ci_line"]))
            self.assertEqual(g4(facts)["status"], "missing", name)

    def test_s4_the_control_the_unmutated_copy_reads_no_base_as_unknown(self):
        self.check_nobase(mutant(self, ci_fresh, self.NOBASE, self.NOBASE, "ci_fresh_control4"))

    def test_s4_the_mutant_that_reads_an_unreadable_base_as_fresh_is_caught(self):
        mut = mutant(self, ci_fresh, self.NOBASE, 'return "fresh", "fresh: x"', "ci_fresh_mut4")
        with self.assertRaisesRegex(AssertionError, "a run with no recorded base is not green"):
            self.check_nobase(mut)


class TestCiFreshOptOut(FreshCase):
    """S6: only an explicit `ci_current_tip: false` turns the rule off."""

    OFF = '.get("ci_current_tip") is False'
    WHOLE = 'try: off = (setting() or {}).get("ci_current_tip") is False\n    except Exception: off = False'

    def test_s6_the_opted_out_repo_reads_nothing_and_the_others_judge(self):
        gh = CountingGh()
        facts = judge(gh, setting={"ci_current_tip": False})
        self.assertEqual((facts["ci"], gh.calls, "ci_line" in facts), ("green", [], False))
        for setting in (None, {}, {"ci_current_tip": True}, {"ci_current_tip": "false"}, {"ci_current_tip": 0}):
            clear_cache()
            self.assertEqual(judge(CountingGh(), setting=setting)["ci"], "stale: 3 behind", setting)

    def test_s6_an_unreadable_configuration_is_not_an_opt_out(self):
        def boom():
            raise ValueError("config")
        facts = {"ci": "green", "head": HEAD, "base_ref": "main"}
        ci_fresh.fresh_fact(facts, boom, lambda: CountingGh(), pr_reading, "acme/app", 5)
        self.assertEqual(facts["ci"], "stale: 3 behind")

    def check_opted_out(self, module):
        gh = CountingGh()
        judge(gh, setting={"ci_current_tip": False}, module=module)
        self.assertEqual(gh.calls, [], "an opted-out repo makes no read")

    def check_absent(self, module):
        facts = judge(CountingGh(), setting=None, module=module)
        self.assertEqual(facts["ci"], "stale: 3 behind", "an absent setting leaves the rule on")

    def test_s6_the_control_the_unmutated_copy_honours_the_opt_out_and_an_absent_setting(self):
        mod = mutant(self, ci_fresh, self.OFF, self.OFF, "ci_fresh_control6")
        self.check_opted_out(mod)
        self.check_absent(mod)

    def test_s6_the_mutant_that_ignores_the_opt_out_is_caught(self):
        mut = mutant(self, ci_fresh, self.OFF, '.get("ci_current_tip") == "never"', "ci_fresh_mut6a")
        with self.assertRaisesRegex(AssertionError, "an opted-out repo makes no read"):
            self.check_opted_out(mut)

    def test_s6_the_mutant_that_reads_an_absent_setting_as_off_is_caught(self):
        mut = mutant(self, ci_fresh, self.OFF, '.get("ci_current_tip") is not True', "ci_fresh_mut6b")
        with self.assertRaisesRegex(AssertionError, "an absent setting leaves the rule on"):
            self.check_absent(mut)

    def check_once(self, module):
        asked = []
        def setting():
            asked.append(1)
            return {}
        judge(CountingGh(two_runs=True), setting=setting, module=module)
        self.assertEqual(len(asked), 1, "the opt-out is checked once per examination")

    def test_the_opt_out_control_it_is_checked_once_per_examination(self):
        self.check_once(mutant(self, ci_fresh, self.WHOLE, self.WHOLE, "ci_fresh_control_once"))

    def test_a_mutant_that_checks_the_opt_out_per_run_is_caught(self):
        mut = mutant(self, ci_fresh, self.WHOLE, 'off = any((setting() or {}).get("ci_current_tip") is False for _ in range(2))', "ci_fresh_mut_per_run")
        with self.assertRaisesRegex(AssertionError, "the opt-out is checked once per examination"):
            self.check_once(mut)

    def test_a_mutant_that_never_checks_the_opt_out_is_caught(self):
        mut = mutant(self, ci_fresh, self.WHOLE, "off = False", "ci_fresh_mut_never")
        with self.assertRaisesRegex(AssertionError, "the opt-out is checked once per examination"):
            self.check_once(mut)


class TestCiFreshReads(FreshCase):
    """S7: the reads of the helper, counted."""

    def test_s7_a_run_at_the_tip_reads_two_and_a_run_behind_three_in_order(self):
        made = []
        at_tip = CountingGh(base=TIP)
        judge(at_tip, made=made)
        self.assertEqual(at_tip.kinds(), ["runs", "tip"])
        behind = CountingGh()
        judge(behind, made=made)
        self.assertEqual(behind.kinds(), ["runs", "tip", "compare"])
        self.assertEqual(len(made), 2)   # every read went through the reader the merge gate's factory made, none through a view

    def test_s7_a_rollup_that_is_not_green_reads_nothing(self):
        for ci in ("failing", "pending", "none"):
            gh = CountingGh()
            judge(gh, ci=ci)
            self.assertEqual(gh.calls, [], ci)

    def test_s7_the_base_is_the_earliest_created_deciding_run(self):
        gh = CountingGh(two_runs=True, newest_first=True)   # run 1 (earlier) on OLD, run 2 (later) on the tip, listed newest first
        facts = judge(gh)
        self.assertEqual(facts["ci"], "stale: 3 behind")
        self.assertIn("bbbbbbbb", facts["ci_line"])

    EARLIEST = 'min(runs, key=lambda r: r["created"] or 0)["base"]'

    def check_earliest(self, mod):
        facts = judge(CountingGh(two_runs=True, newest_first=True), reading=mod)
        self.assertEqual(facts["ci"], "stale: 3 behind", "the earliest-created run decides the base")

    def test_the_earliest_run_control_the_unmutated_copy_picks_the_earliest(self):
        self.check_earliest(mutant(self, pr_reading, self.EARLIEST, self.EARLIEST, "pr_reading_control_earliest"))

    def test_a_reader_that_picks_the_newest_run_is_caught(self):
        mod = mutant(self, pr_reading, self.EARLIEST, 'max(runs, key=lambda r: r["created"] or 0)["base"]', "pr_reading_newest")
        with self.assertRaisesRegex(AssertionError, "the earliest-created run decides the base"):
            self.check_earliest(mod)

    def test_a_reader_that_picks_the_first_listed_run_is_caught(self):
        mod = mutant(self, pr_reading, self.EARLIEST, 'runs[0]["base"]', "pr_reading_first")
        with self.assertRaisesRegex(AssertionError, "the earliest-created run decides the base"):
            self.check_earliest(mod)

    def test_a_pull_request_with_no_base_ref_or_no_head_reads_nothing_and_is_unknown(self):
        for kw in (dict(base_ref=None), dict(base_ref="")):
            gh = CountingGh()
            facts = judge(gh, **kw)
            self.assertEqual((gh.calls, facts["ci"]), ([], "unknown"), kw)
        gh = CountingGh()
        facts = judge(gh, head=None)
        self.assertEqual((gh.calls, facts["ci"]), ([], "unknown"), "no head with a valid base_ref makes 0 reads")
        control = CountingGh()
        judge(control)
        self.assertEqual(control.kinds()[:2], ["runs", "tip"])

    def test_a_reader_that_reads_the_tip_without_a_head_is_caught(self):
        def check(mod):
            gh = CountingGh()
            self.assertEqual(judge(gh, head=None, reading=mod)["ci"], "unknown")
            self.assertEqual(len(gh.calls), 0, "no head with a valid base_ref makes 0 reads")
        check(_copy_of(pr_reading, TestCiFreshReadMutants.TIP_NEW, None, "pr_reading_nohead_control"))
        mod = _copy_of(pr_reading, TestCiFreshReadMutants.TIP_NEW, TestCiFreshReadMutants.TIP_OLD, "pr_reading_nohead")
        with self.assertRaisesRegex(AssertionError, "makes 0 reads"):
            check(mod)

    def test_a_negative_boolean_or_string_ahead_by_is_not_counted(self):
        for bad in (-3, True, "-3"):
            clear_cache()
            facts = judge(CountingGh(behind=bad))
            self.assertIn("were not counted", facts["ci_line"], bad)
            self.assertNotIn("-3", facts["ci_line"])
            self.assertNotEqual(facts["ci"], "green")
        clear_cache()
        self.assertIn("3 commits behind", judge(CountingGh(behind=3))["ci_line"])

    UPPER = "not 0 <= n <= 1000000"

    def s14_count_check(self, mod):
        for bad in (2 ** 70, 1000001, True, -1, 3.0, "3", "absent"):
            mod.read_behind.__defaults__[0].clear()
            gh = (lambda argv, own=True: (0, "{}", "")) if bad == "absent" else CountingGh(behind=bad)
            try:
                mod.read_behind(gh, "acme/app", OLD, TIP)
            except mod.ReadError as e:
                self.assertEqual(str(e), "the commits behind were not counted", repr(bad))
            else:
                self.fail("a count above the bound of 1000000 or not an int is not counted: %r was counted" % (bad,))
        for good in (0, 3):
            mod.read_behind.__defaults__[0].clear()
            self.assertEqual(mod.read_behind(CountingGh(behind=good), "acme/app", OLD, TIP), good)

    def test_s14_the_count_is_an_int_from_0_to_1000000_else_not_counted(self):
        self.s14_count_check(pr_reading)

    def test_s14_the_control_the_unmutated_copy_refuses_the_seven_and_counts_two(self):
        self.s14_count_check(_copy_of(pr_reading, self.UPPER, None, "pr_reading_s14_control"))

    def s14_one(self, mod, bad):
        mod.read_behind.__defaults__[0].clear()
        try:
            mod.read_behind(CountingGh(behind=bad), "acme/app", OLD, TIP)
        except mod.ReadError:
            return
        self.fail("a count above the bound of 1000000 or not an int is not counted: %r was counted" % (bad,))

    def test_s14_the_mutant_with_the_old_count_check_is_caught_for_each_of_2_70_and_1000001(self):
        for bad in (2 ** 70, 1000001):
            control = _copy_of(pr_reading, self.UPPER, None, "pr_reading_s14_control_%d" % bad)
            self.s14_one(control, bad)      # control first: it passes
            mod = _copy_of(pr_reading, self.UPPER, "n < 0", "pr_reading_s14_nobound_%d" % bad)   # applied once (count == 1)
            with self.assertRaisesRegex(AssertionError, "a count above the bound of 1000000 or not an int is not counted: %d was counted" % bad):
                self.s14_one(mod, bad)

    RETURN = "        _seen[key] = n\n    return n\n"

    def s15_atomic_check(self, mod):
        class Evicted(dict):      # the lookup of a key it just stored fails, as when another thread evicted it
            def __getitem__(self, k):
                raise KeyError(k)

        class PopGone(dict):      # the eviction pop finds the oldest key already gone
            def pop(self, *a):
                super().pop(*a[:1], None)
                if len(a) < 2:
                    raise KeyError(a[0])
                return super().pop(*a)

        self.assertEqual(mod.read_behind(CountingGh(behind=3), "acme/app", OLD, TIP, Evicted()), 3, "the count answers when the stored key is gone")
        full = PopGone({("r", "a%d" % i, "b"): i for i in range(256)})
        self.assertEqual(mod.read_behind(CountingGh(behind=4), "acme/app", OLD, TIP, full), 4, "the count answers when the eviction finds its key gone")

    def test_s15_the_control_the_unmutated_copy_answers_when_the_key_is_gone(self):
        self.s15_atomic_check(_copy_of(pr_reading, self.RETURN, None, "pr_reading_s15_control"))
        self.s15_atomic_check(pr_reading)

    def test_s15_the_mutant_with_the_two_lookup_form_is_caught(self):
        mod = _copy_of(pr_reading, self.RETURN, "        _seen[key] = n\n    return _seen[key]\n", "pr_reading_s15_twolookups")
        with self.assertRaises(KeyError):
            self.s15_atomic_check(mod)

    def s14_memory_check(self, mod):
        seen = mod.read_behind.__defaults__[0]
        seen.clear()
        gh = CountingGh(behind=3)
        for i in range(300):
            mod.read_behind(gh, "acme/app", "%040d" % i, TIP)
        self.assertLessEqual(len(seen), 256, "the memory holds at most 256 pairs: it holds %d" % len(seen))
        before = len(gh.calls)
        self.assertEqual(mod.read_behind(gh, "acme/app", "%040d" % 299, TIP), 3)
        self.assertEqual(len(gh.calls), before, "a pair inside the memory costs no GET")
        self.assertEqual(mod.read_behind(gh, "acme/app", "%040d" % 0, TIP), 3)
        self.assertEqual(len(gh.calls), before + 1, "the first pair, dropped, costs one more GET and answers as before")

    def test_s14_300_pairs_the_memory_holds_256_and_a_dropped_pair_costs_one_get(self):
        self.s14_memory_check(pr_reading)

    MEM = "        if len(_seen) >= 256: _seen.pop(next(iter(_seen), None), None)\n"

    def test_s14_the_control_the_unmutated_copy_bounds_its_memory(self):
        self.s14_memory_check(_copy_of(pr_reading, self.MEM, None, "pr_reading_s14_mem_control"))

    def test_s14_the_mutant_with_no_bound_on_the_memory_is_caught(self):
        mod = _copy_of(pr_reading, self.MEM, "", "pr_reading_s14_mem")
        with self.assertRaisesRegex(AssertionError, "the memory holds at most 256 pairs"):
            self.s14_memory_check(mod)

    BEHIND_OLD = "if not isinstance(n, int): raise"

    def test_a_reader_that_restores_the_old_ahead_by_check_is_caught(self):
        def check(mod):
            clear_cache()
            self.assertIn("were not counted", judge(CountingGh(behind=-3), reading=mod)["ci_line"], "a negative ahead_by is not counted")
        check(_copy_of(pr_reading, "if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 1000000: raise", None, "pr_reading_behind_control"))
        mod = _copy_of(pr_reading, "if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 1000000: raise", self.BEHIND_OLD, "pr_reading_behind")
        with self.assertRaisesRegex(AssertionError, "a negative ahead_by is not counted"):
            check(mod)

    def test_s7_a_pair_is_read_once_per_pass(self):
        gh = CountingGh()
        judge(gh)
        judge(gh)
        self.assertEqual(gh.kinds().count("compare"), 1)

    def test_s7_the_real_examination_reads_once_per_pair_through_the_merge_gates_reader(self):
        sb = _host().sb
        raw = {"state": "OPEN", "headRefOid": HEAD, "baseRefName": "main", "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        gh = CountingGh()
        views, through_reader = [], []

        def runner(argv, **kw):
            if argv[1:3] == ["pr", "view"]:
                views.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
            code, out, err = gh(argv[1:])
            return subprocess.CompletedProcess(argv, code, out, err)

        real = sb.merge_gate_gh

        def counting(config, run, repo):
            inner = real(config, run, repo)

            def read(argv, own=True):
                through_reader.append(argv)
                return inner(argv, own)
            return read
        engine = types.SimpleNamespace(config=dict(sb.DEFAULTS, gh="gh"), gh_runner=runner, now=lambda: 0.0)
        sb.load_module("pr_reading").read_behind.__defaults__[0].clear()
        sb.merge_gate_gh = counting
        try:
            facts = sb.read_pr_facts(engine, "acme/app", 5)
            first = list(gh.kinds())
            sb.read_pr_facts(engine, "acme/app", 5)
        finally:
            sb.merge_gate_gh = real
        self.assertEqual(facts["ci"], "stale: 3 behind")
        self.assertEqual(first, ["runs", "tip", "compare"])
        self.assertEqual(gh.kinds(), ["runs", "tip", "compare", "runs", "tip"])   # the pair is counted once per pass
        self.assertEqual(len(views), 2)    # the examination's own view, one per examination, never a second one
        self.assertEqual(len(through_reader), len(gh.calls))   # every GitHub read of the rule went through the merge gate's reader


class TestCiFreshRefusalAndEvidence(FreshCase):
    """S8: the merge gate's refusal holds the number; the evidence names the tip."""

    FACTS = dict(problems=[], state="OPEN", gated=True, branch="main", branch_result="green", branch_runs=[], pr_result="green", failing=[],
                 head=HEAD, legs=(False, True, ""), run_base=OLD, tip=TIP)

    def test_s8_the_refusal_says_the_number_of_commits_behind_and_the_rest_is_unchanged(self):
        ok, why, need = merge_gate.decide(dict(self.FACTS, behind=17))
        self.assertEqual((ok, need), (False, "rerun"))
        self.assertEqual(why, "main moved after the last green run (tested on bbbbbbbb, tip now aaaaaaaa, 17 commits behind): its CI must run again on the current merge ref")
        _, bare, _ = merge_gate.decide(dict(self.FACTS, behind=None))
        self.assertEqual(bare, "main moved after the last green run (tested on bbbbbbbb, tip now aaaaaaaa): its CI must run again on the current merge ref")

    def test_s8_the_evidence_line_of_a_fresh_run_is_read_back_from_the_recorded_verdict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as state:
            facts = judge(CountingGh(base=TIP))
            self.assertEqual(facts["ci_line"], "fresh: CI ran on the tip " + TIP[:12])
            ctx = types.SimpleNamespace(unreadable=None, facts=dict(facts, state="OPEN", body=""), plan_problem=None, engine=types.SimpleNamespace(audit=None), pr="acme/app#5", config={},
                                        repo="acme/app", state=state, source="plan.md", entries=[],
                                        plan={"result": "pass", "plan_hash": "sha256:x", "scenarios_parsed": [{"id": "S1", "evaluator": "ci", "claim": "pr"}]})
            done = _host().sb.ci_record_core(ctx, lambda: 0, quiet=True)
            self.assertEqual((done["status"], done["recorded"]), ("recorded", ["S1"]))
            entries, problem = plan_ledger.read_ledger(state)
            self.assertIsNone(problem)
            (verdict,) = [e for e in entries if e.get("kind") == "verdict"]
            self.assertEqual(verdict["evidence"], "CI green at %s on the tip %s: every check run complete and passing" % (HEAD[:12], TIP[:12]))
            stale = judge(CountingGh())
            again = _host().sb.ci_record_core(types.SimpleNamespace(**dict(vars(ctx), facts=dict(stale, state="OPEN", body=""))), lambda: 0, quiet=True)
            self.assertEqual((again["status"], again["recorded"]), ("not-green", []))
            self.assertEqual(len([e for e in plan_ledger.read_ledger(state)[0] if e.get("kind") == "verdict"]), 1)   # nothing more was recorded


def _copy_of(module, old, new, tag):
    """A module built from a COPY of the source of `module`, with `old` replaced by `new` (None: unmutated); the mutation is asserted applied."""
    src = open(module.__file__).read()
    if new is not None:
        assert src.count(old) == 1, "the mutation did not apply: " + old
        src = src.replace(old, new)
    mod = types.ModuleType(tag)
    mod.__dict__["__file__"] = module.__file__
    exec(compile(src, tag, "exec"), mod.__dict__)
    return mod


class TestCiFreshReadMutants(FreshCase):
    """Mutants of `pr_reading.read_fresh` (one per GitHub read it makes) and of the merge gate's `N commits behind` text; each has a control."""

    RUNS = "runs = read_pr_runs(gh, repo, number, head) if head and isinstance(base_ref, str) and base_ref else []"
    TIP_NEW = "tip = read_tip(gh, repo, base_ref) if head and isinstance(base_ref, str) and base_ref else None"
    TIP = TIP_NEW
    TIP_OLD = "tip = read_tip(gh, repo, base_ref) if isinstance(base_ref, str) and base_ref else None"
    COMPARE = "behind = read_behind(gh, repo, base, tip) if base and base != tip else None"

    def read_all(self, mod):
        gh = CountingGh()
        facts = judge(gh, reading=mod)
        self.assertEqual(gh.kinds(), ["runs", "tip", "compare"], "the three reads are made: runs, tip, compare")
        self.assertEqual(facts["ci_line"], ci_fresh.ci_state("green", OLD, TIP, 3)[1], "the stale line holds the count of the compare")

    def test_the_control_the_unmutated_copy_makes_all_three_reads(self):
        self.read_all(_copy_of(pr_reading, self.RUNS, None, "pr_reading_control"))

    def test_a_reader_that_skips_the_run_list_is_caught(self):
        mod = _copy_of(pr_reading, self.RUNS, "runs = []", "pr_reading_noruns")
        with self.assertRaisesRegex(AssertionError, "the three reads are made: runs, tip, compare"):
            self.read_all(mod)

    def test_a_reader_that_skips_the_tip_is_caught(self):
        mod = _copy_of(pr_reading, self.TIP, "tip = None", "pr_reading_notip")
        with self.assertRaisesRegex(AssertionError, "the three reads are made: runs, tip, compare"):
            self.read_all(mod)

    def test_a_reader_that_skips_the_compare_is_caught(self):
        mod = _copy_of(pr_reading, self.COMPARE, "behind = None", "pr_reading_nocompare")
        with self.assertRaisesRegex(AssertionError, "the three reads are made: runs, tip, compare"):
            self.read_all(mod)

    def refusal(self, mod):
        facts = dict(TestCiFreshRefusalAndEvidence.FACTS, behind=17)
        why = mod.decide(facts)[1]
        self.assertIn("tip now aaaaaaaa, 17 commits behind)", why)

    GATE = '", %d commits behind" % facts["behind"] if facts.get("behind") is not None else ""'

    def test_the_control_the_unmutated_merge_gate_says_the_number(self):
        self.refusal(_copy_of(merge_gate, self.GATE, None, "merge_gate_control"))

    def test_a_merge_gate_that_drops_the_number_is_caught(self):
        mod = _copy_of(merge_gate, self.GATE, '""', "merge_gate_nonumber")
        with self.assertRaisesRegex(AssertionError, "17 commits behind"):
            self.refusal(mod)


class TestCiFreshSeamMutants(FreshCase):
    """The fakes of the existing fixtures answer a run list whose base is their `run_base` (default: the tip). A copy of each seam whose run list ignores `run_base` and answers the tip
    makes the stale test of its family fail with the anchored text of that test's assertion; the unmutated copy passes the same test (the control)."""

    SEAMS = (("LedgerBase", "route", '"base": {"sha": self.run_base}} for n in (9, 11)]}', '"base": {"sha": "t" * 40}} for n in (9, 11)]}',
              (("TestCiRecordPass", "test_a_stale_run_records_no_verdict_and_says_the_stale_line", "ci-record records no verdict for a stale run"),
               ("TestPlanGatesCheck", "test_a_stale_run_makes_g4_missing_with_the_stale_line", "the G4 row is missing for a stale run"))),
             ("TestCiRecordCarry", "route", '"base": {"sha": self.run_base}} for n in (9, 11)]}', '"base": {"sha": "t" * 40}} for n in (9, 11)]}',
              (("TestCiRecordCarry", "test_a_stale_run_carries_nothing_and_prints_the_stale_line", "a stale run carries no proof: exit code"),)),
             ("BaselineWorld", "route", '"base": {"sha": self.run_base}} for n in self.prs]}', '"base": {"sha": "t" * 40}} for n in self.prs]}',
              (("TestDecisionBaselineNotices", "test_a_stale_run_reads_stale_in_the_notice_facts", "the panel notice facts read stale: 3 behind"),)),
             ("FakeGhPr", "__call__", '"base": {"sha": self.run_base}} for n in (7, 8)]}', '"base": {"sha": "t" * 40}} for n in (7, 8)]}',
              (("TestPrNotices", "test_a_stale_run_reads_stale_in_the_pull_request_notice", "the pull request notice reads stale: 3 behind"),)))

    def seam(self, cls, name, old, new):
        """The method of the seam as a function built from its source with `old` replaced by `new` (new None: unmutated); the replaced text occurs once."""
        func = getattr(cls, name)
        src = textwrap.dedent("".join(inspect.getsourcelines(func)[0]))
        if new is not None:
            self.assertEqual(src.count(old), 1, "the mutation applies once: " + old)
            src = src.replace(old, new)
        scope = {}
        exec(compile(src.replace("super().", "super(__cls__, self)."), "seam_" + name, "exec"), dict(func.__globals__, __cls__=cls), scope)   # a copy has no __class__ cell: super is named
        return scope[name]

    def run_case(self, case, test, cls, name, fn):
        result = unittest.TestResult()
        with mock.patch.object(cls, name, fn):
            getattr(_host(), case)(test).run(result)
        return result

    def test_the_controls_the_unmutated_seams_pass_the_stale_tests(self):
        for cls_name, name, old, new, cases in self.SEAMS:
            cls = getattr(_host(), cls_name)
            fn = self.seam(cls, name, old, None)
            for case, test, text in cases:
                result = self.run_case(case, test, cls, name, fn)
                self.assertEqual((result.testsRun, result.failures, result.errors), (1, [], []), (case, test))

    def test_a_seam_that_answers_the_tip_for_every_run_fails_the_stale_test_of_each_family(self):
        for cls_name, name, old, new, cases in self.SEAMS:
            cls = getattr(_host(), cls_name)
            fn = self.seam(cls, name, old, new)
            for case, test, text in cases:
                result = self.run_case(case, test, cls, name, fn)
                self.assertEqual((result.testsRun, len(result.failures), result.errors), (1, 1, []), (case, test))
                with self.assertRaisesRegex(AssertionError, text):
                    raise AssertionError(result.failures[0][1])


if __name__ == "__main__":
    unittest.main()

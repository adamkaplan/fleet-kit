"""The pure decision of bin/ci_fresh.py: a green run counts only on the current tip (ask #1755, S5)."""
import json
import os
import subprocess
import sys
import types
import unittest

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

    def __init__(self, base=OLD, tip=TIP, behind=3, runs="ok", tip_ok=True, compare_ok=True, two_runs=False):
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
            return 0, json.dumps({"workflow_runs": runs}), ""
        if "/git/ref/heads/" in path:
            return (0, json.dumps({"object": {"sha": self.tip}}), "") if self.tip_ok else (1, "", "boom")
        if "/compare/" in path:
            return (0, json.dumps({"ahead_by": self.behind}), "") if self.compare_ok else (1, "", "boom")
        raise AssertionError("an unexpected read: %r" % (argv,))

    def kinds(self):
        return [("runs" if "/actions/runs" in a[1] else "tip" if "/git/ref/heads/" in a[1] else "compare") for a in self.calls]


def judge(gh, setting=None, ci="green", base_ref="main", repo="acme/app", number=5, reading=pr_reading, module=ci_fresh, made=None):
    facts = {"ci": ci, "head": HEAD, "base_ref": base_ref}
    def make_gh():
        if made is not None:
            made.append(1)
        return gh
    return module.fresh_fact(facts, lambda: setting, make_gh, reading, repo, number)


def fresh_reading():
    """pr_reading with a cleared memory of the compare, so every case reads its own pair."""
    mod = types.ModuleType("pr_reading_case")
    mod.__dict__.update(vars(pr_reading))
    mod.read_behind = lambda gh, repo, a, b: pr_reading.read_behind.__wrapped__(gh, repo, a, b) if hasattr(pr_reading.read_behind, "__wrapped__") else pr_reading.read_behind(gh, repo, a, b, {})
    mod.read_fresh = lambda gh, repo, number, head, base_ref: _read_fresh(mod, gh, repo, number, head, base_ref)
    return mod


def _read_fresh(mod, gh, repo, number, head, base_ref):
    base = tip = behind = None
    try:
        runs = mod.read_pr_runs(gh, repo, number, head) if head else []
        base = min(runs, key=lambda r: r["created"] or 0)["base"] if runs and all(r["base"] for r in runs) else None
        tip = mod.read_tip(gh, repo, base_ref) if isinstance(base_ref, str) and base_ref else None
        behind = mod.read_behind(gh, repo, base, tip) if base and base != tip else None
    except (mod.ReadError, AttributeError, TypeError):
        pass
    return base, tip, behind


def g4(facts):
    plan = {"result": "pass", "scenarios_parsed": [], "plan_hash": "sha256:x", "source": "x"}
    table = plan_ledger.evaluate_gates(plan, [], None, dict(facts, head=HEAD), "acme/app#5")
    return next(r for r in table["gates"] if r["gate"] == "G4")


def ci_record(facts):
    ctx = types.SimpleNamespace(unreadable=None, facts=dict(facts, state="OPEN"), plan={"result": "pass", "scenarios_parsed": []}, plan_problem=None,
                                engine=None, pr=5, config={}, repo="acme/app")
    return _host().sb.ci_record_core(ctx, lambda: 0, quiet=True)


def pr_state_text(facts):
    return "CI %s" % facts["ci"]   # the entry prints `CI %s` of the view's `ci`, which is this fact (the one line of the rule, not a private text)


class TestCiFreshReaders(unittest.TestCase):
    """S2, S3: the three readers over a stale and a fresh green run (a repo with no `ci_current_tip` setting: the rule is on)."""

    def test_s2_a_stale_run_is_missing_records_nothing_and_prints_stale(self):
        facts = judge(CountingGh(behind=3), reading=fresh_reading())
        row = g4(facts)
        self.assertEqual((row["status"], row["missing"][0][:23]), ("missing", "stale: 3 commits behind"))
        done = ci_record(facts)
        self.assertEqual((done["status"], done["why"][:23]), ("not-green", "stale: 3 commits behind"))
        self.assertEqual(pr_state_text(facts), "CI stale: 3 behind")

    def test_s2_the_mutant_that_reads_the_heads_run_only_is_caught(self):
        class Head(types.ModuleType):
            pass
        mut = types.ModuleType("ci_fresh_mut")
        mut.__dict__.update(vars(ci_fresh))
        src = open(ci_fresh.__file__).read()
        old = '    if run_base == tip:\n        return "fresh"'
        self.assertIn(old, src)   # the mutation applies
        exec(compile(src.replace(old, '    if True:\n        return "fresh"'), "ci_fresh_mut", "exec"), mut.__dict__)
        facts = judge(CountingGh(behind=3), reading=fresh_reading(), module=mut)
        self.assertEqual(facts["ci"], "green")   # the mutant reads the stale run as green
        with self.assertRaisesRegex(AssertionError, "stale: 3 commits behind"):
            self.assertEqual(g4(facts)["status"], "missing", "expected stale: 3 commits behind")

    def test_s3_a_run_at_the_tip_is_ok_names_the_tip_and_reads_green(self):
        facts = judge(CountingGh(base=TIP), reading=fresh_reading())
        row = g4(facts)
        self.assertEqual(row["status"], "ok")
        self.assertIn(TIP[:12], row["detail"])
        self.assertNotEqual(ci_record(facts)["status"], "not-green")
        self.assertEqual(pr_state_text(facts), "CI green")


class TestCiFreshUnreadable(unittest.TestCase):
    """S4: none of four unreadable states is green."""

    CASES = (("runs", dict(runs="fail"), "unknown", "unknown: the base of the last run is not recorded"),
             ("nobase", dict(runs="nobase"), "unknown", "unknown: the base of the last run is not recorded"),
             ("tip", dict(tip_ok=False), "unknown", "unknown: the tip could not be read"),
             ("compare", dict(compare_ok=False), "stale: the commits", "stale: the commits behind were not counted"))

    def test_s4_each_unreadable_state_is_not_green_with_its_line(self):
        for name, kw, ci, line in self.CASES:
            facts = judge(CountingGh(**kw), reading=fresh_reading())
            self.assertNotEqual(facts["ci"], "green", name)
            self.assertTrue(facts["ci_line"].startswith(line), (name, facts["ci_line"]))
            self.assertEqual(g4(facts)["status"], "missing", name)

    def test_s4_the_mutant_that_reads_an_unreadable_base_as_fresh_is_caught(self):
        mut = types.ModuleType("ci_fresh_mut4")
        src = open(ci_fresh.__file__).read()
        old = 'return "unknown", "unknown: the base of the last run is not recorded"'
        self.assertIn(old, src)   # the mutation applies
        exec(compile(src.replace(old, 'return "fresh", "fresh: x"'), "ci_fresh_mut4", "exec"), mut.__dict__)
        facts = judge(CountingGh(runs="nobase"), reading=fresh_reading(), module=mut)
        with self.assertRaisesRegex(AssertionError, "not green: nobase"):
            self.assertNotEqual(facts["ci"], "green", "not green: nobase")


class TestCiFreshOptOut(unittest.TestCase):
    """S6: only an explicit `ci_current_tip: false` turns the rule off."""

    def test_s6_the_opted_out_repo_reads_nothing_and_the_others_judge(self):
        gh = CountingGh()
        facts = judge(gh, setting={"ci_current_tip": False}, reading=fresh_reading())
        self.assertEqual((facts["ci"], gh.calls, "ci_line" in facts), ("green", [], False))
        for setting in (None, {}, {"ci_current_tip": True}, {"ci_current_tip": "false"}, {"ci_current_tip": 0}):
            self.assertEqual(judge(CountingGh(), setting=setting, reading=fresh_reading())["ci"], "stale: 3 behind", setting)

    def test_s6_an_unreadable_configuration_is_not_an_opt_out(self):
        def boom():
            raise ValueError("config")
        facts = {"ci": "green", "head": HEAD, "base_ref": "main"}
        ci_fresh.fresh_fact(facts, boom, lambda: CountingGh(), fresh_reading(), "acme/app", 5)
        self.assertEqual(facts["ci"], "stale: 3 behind")

    def _mutant(self, old, new, tag):
        src = open(ci_fresh.__file__).read()
        self.assertIn(old, src)   # the mutation applies
        mut = types.ModuleType(tag)
        exec(compile(src.replace(old, new), tag, "exec"), mut.__dict__)
        return mut

    def test_s6_the_mutant_that_ignores_the_opt_out_is_caught(self):
        mut = self._mutant('.get("ci_current_tip") is False', '.get("ci_current_tip") is "never"', "ci_fresh_mut6a")
        gh = CountingGh()
        judge(gh, setting={"ci_current_tip": False}, reading=fresh_reading(), module=mut)
        with self.assertRaisesRegex(AssertionError, "opted out: 0 reads"):
            self.assertEqual(gh.calls, [], "opted out: 0 reads")

    def test_s6_the_mutant_that_reads_an_absent_setting_as_off_is_caught(self):
        mut = self._mutant('.get("ci_current_tip") is False', '.get("ci_current_tip") is not True', "ci_fresh_mut6b")
        facts = judge(CountingGh(), setting=None, reading=fresh_reading(), module=mut)
        with self.assertRaisesRegex(AssertionError, "absent setting: stale"):
            self.assertEqual(facts["ci"], "stale: 3 behind", "absent setting: stale")


class TestCiFreshReads(unittest.TestCase):
    """S7: the reads of the helper, counted."""

    def test_s7_a_run_at_the_tip_reads_two_and_a_run_behind_three_in_order(self):
        made = []
        at_tip = CountingGh(base=TIP)
        judge(at_tip, reading=fresh_reading(), made=made)
        self.assertEqual(at_tip.kinds(), ["runs", "tip"])
        behind = CountingGh()
        judge(behind, reading=fresh_reading(), made=made)
        self.assertEqual(behind.kinds(), ["runs", "tip", "compare"])
        self.assertEqual(len(made), 2)   # every read went through the reader the merge gate's factory made, none through a view

    def test_s7_a_rollup_that_is_not_green_reads_nothing(self):
        for ci in ("failing", "pending", "none"):
            gh = CountingGh()
            judge(gh, ci=ci, reading=fresh_reading())
            self.assertEqual(gh.calls, [], ci)

    def test_s7_the_base_is_the_earliest_created_deciding_run(self):
        gh = CountingGh(two_runs=True)   # run 1 (earlier) on OLD, run 2 (later) on the tip
        facts = judge(gh, reading=fresh_reading())
        self.assertEqual(facts["ci"], "stale: 3 behind")
        self.assertIn("bbbbbbbb", facts["ci_line"])

    def test_s7_a_pair_is_read_once_per_pass(self):
        mod = fresh_reading()
        mod.read_behind = pr_reading.read_behind   # the real one keeps the pair in memory
        pr_reading.read_behind.__defaults__[0].clear()
        gh = CountingGh()
        judge(gh, reading=mod)
        judge(gh, reading=mod)
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


class TestCiFreshRefusalAndEvidence(unittest.TestCase):
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
            facts = judge(CountingGh(base=TIP), reading=fresh_reading())
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
            stale = judge(CountingGh(), reading=fresh_reading())
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


class TestCiFreshReadMutants(unittest.TestCase):
    """Mutants of `pr_reading.read_fresh` (one per GitHub read it makes) and of the merge gate's `N commits behind` text; each has a control."""

    RUNS = "runs = read_pr_runs(gh, repo, number, head) if head else []"
    TIP = "tip = read_tip(gh, repo, base_ref) if isinstance(base_ref, str) and base_ref else None"
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

if __name__ == "__main__":
    unittest.main()

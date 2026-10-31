"""The tests of #1241 (the per-repository owner hold), moved out of bin/test-switchboard (its line ceiling). Nothing here needs the entry file's own fixtures."""
import contextlib
import copy
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.loader import SWITCHBOARD, captured, sb
from support.paths import BIN
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE, SimClock

class TestOwnerHoldFloorConfig(Base):
    """Ask #1241 (plan S1 as amended by PC-11, and S28): the module's functions stay where the callers read them, and an impact_floor entry that cannot be read is refused whole, never dropped."""
    NAMES = {"owner_hold_rule": "impact_floor.py", "top_tier_hits": "impact_floor.py", "owner_decide": "impact_floor.py",
             "floor_for": "impact_floor.py", "floor_paths": "impact_floor.py", "pr_floor_report": "fleet-switchboard"}   # each in the file that defines it

    def defined(self, source):
        import ast
        return {n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}

    def missing(self, sources):
        """The names not defined as a module-level function in the file that owns each."""
        return sorted(n for n, f in self.NAMES.items() if n not in self.defined(sources[f]))

    def test_s1_the_six_named_functions_are_module_level_functions_of_their_own_file_and_a_missing_one_is_named(self):
        sources = {f: (BIN / f).read_text() for f in set(self.NAMES.values())}
        self.assertEqual(self.missing(sources), [], "0 missing")
        renamed = dict(sources, **{"impact_floor.py": sources["impact_floor.py"].replace("def owner_decide(", "def owner_decide_renamed(", 1)})
        self.assertEqual(self.missing(renamed), ["owner_decide"], "a rename in a copy is found")
        moved = dict(sources, **{"fleet-switchboard": sources["fleet-switchboard"].replace("def pr_floor_report(", "def pr_floor_report_x(", 1)})
        self.assertEqual(self.missing(moved), ["pr_floor_report"])
        with mock.patch.dict(self.NAMES, {"no_such_function_bogus": "impact_floor.py"}):
            self.assertEqual(self.missing(sources), ["no_such_function_bogus"])

    def good(self):
        return {"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 5,
                "impact_floor": {"repos": {"acme/widget-api": {"globs": ["shims/**"]}}}}

    def bad_configs(self):
        broken = self.good()
        broken["impact_floor"]["repos"]["acme/widget-api"]["hold"] = "{not json"   # a hold entry that cannot be parsed
        number = dict(self.good(), impact_floor=7)
        return {"hold entry unparseable": broken, "impact_floor a number": number}

    def refuses(self, text):
        """The assertions of S28 for one config text (so a mutant build can be run against the very same ones)."""
        self.config_file.write_text(text)
        with self.assertRaises(sb.ConfigError) as caught:
            sb.load_runnable_config()
        self.assertIn("impact_floor", str(caught.exception))
        with captured() as (out, err):
            code = sb.main(["status"])   # the command that reads the config and says why it is unusable
        out, err = out.getvalue(), err.getvalue()
        self.assertEqual(code, sb.EXIT_ERROR, err)
        self.assertIn("impact_floor", out + err)

    def test_s28_an_impact_floor_that_cannot_be_read_is_refused_naming_it_and_the_running_config_is_kept(self):
        for label, bad in self.bad_configs().items():
            with self.subTest(label):
                self.write_config(self.good())
                kept = sb.load_runnable_config()
                clock = SimClock()
                watch = sb.ConfigWatch(self.config_file, kept, clock.now)
                snapshot = json.loads(json.dumps(kept, sort_keys=True, default=str))
                self.refuses(json.dumps(bad))
                self.assertIsNone(watch.check())
                clock.t += sb.CONFIG_STABLE_SECONDS + 0.5
                self.assertIsNone(watch.check(), "an invalid file never reloads")
                self.assertIn("impact_floor", watch.invalid["reason"])
                self.assertEqual(json.loads(json.dumps(watch.config, sort_keys=True, default=str)), snapshot, "the last good config is what runs")
                self.assertIs(watch.config, kept)

    def test_s28_a_build_that_drops_the_invalid_entry_and_loads_on_fails_the_same_assertions(self):
        floor = sb.load_module("impact_floor")
        real = floor.validate
        def dropping(raw, source="config"):   # the mutant: the check of the key is gone, so the bad entry loads
            return None
        for label, bad in self.bad_configs().items():
            with self.subTest(label):
                self.write_config(self.good())
                self.refuses(json.dumps(bad))   # the real build passes it
                with mock.patch.object(floor, "validate", dropping):
                    with self.assertRaises(AssertionError):
                        self.refuses(json.dumps(bad))
                self.assertIs(floor.validate, real)


# ---- #1241: the per-repository owner hold (impact_floor.repos."<owner>/<repo>".hold / hold_exclude). Neutral names only.
HOLD_API = "acme-org/widget-api"
HOLD_WEB = "acme-org/widget-web"
HOLD_ENTRY = {"globs": ["src/**"], "hold": {"identity": ["auth/**"], "credentials": ["secrets/**"], "authority": ["perms/**"], "execution": ["deploy/**", ".ci/**"]},
              "hold_exclude": ["deploy/docs/**", "auth/fixtures/**"]}


def hold_config(entry=HOLD_ENTRY, name=HOLD_API):
    return {"impact_floor": {"repos": {name: copy.deepcopy(entry), HOLD_WEB: {"globs": ["src/**"]}}}}


def hold_decide(f, repo, paths, config=None, impacts=(), floor_hit=()):
    matrix = f.load_matrix()
    config = hold_config() if config is None else config
    return f.owner_decide(matrix, list(paths), list(impacts), None, floor_hit, {}, None, entry=f.entry_hold(config, repo, matrix))


def hold_kit(f):
    return sorted(f.default_floors())[0]


def _validation(f, entry):
    try:
        f.validate_config({"repos": {HOLD_API: entry}})
    except ValueError as err:
        return str(err)
    return None


class HoldChecks:
    """Every scenario of #1241 as a check on the module `f` (raises AssertionError). The tests run them on the real module; each mutant runs them on a broken
    one and must make the named checks fail."""

    def __init__(self, test):
        self.t = test

    def holds(self, f, repo, paths, *words, **kw):
        decision, reasons = hold_decide(f, repo, paths, **kw)
        self.t.assertEqual(decision, "hold", reasons)
        for word in words:
            self.t.assertTrue(any(word in r for r in reasons), (word, reasons))
        return reasons

    def not_held(self, f, repo, paths, **kw):
        decision, reasons = hold_decide(f, repo, paths, **kw)
        self.t.assertNotEqual(decision, "hold", reasons)
        return decision, reasons

    def c_validate_accepts(self, f):
        self.t.assertIsNone(_validation(f, HOLD_ENTRY))
        self.t.assertIsNone(_validation(f, {"hold": {"identity": ["auth/**"]}}))   # a hold entry needs no floor globs
        self.t.assertIsNone(_validation(f, {"hold": {}, "hold_exclude": ["docs/**"]}))   # docs/** is no machinery glob
        self.t.assertIsNone(_validation(f, {"hold": {"execution": []}}))

    def c_validate_refuses(self, f):
        t = self.t
        msg = _validation(f, {"hold": {"network": ["x/**"]}})
        t.assertTrue(msg and "hold" in msg and "network" in msg, msg)
        t.assertIn("hold_include", _validation(f, {"hold": {}, "hold_include": ["x"]}) or "")
        msg = _validation(f, {"hold": {"identity": "auth/**"}})
        t.assertTrue(msg and "hold.identity" in msg and "list" in msg, msg)
        for bad in ({}, [""], [7]):
            t.assertIn("hold_exclude", _validation(f, {"hold": {}, "hold_exclude": bad}) or "", bad)
        t.assertIn("hold", _validation(f, {"hold": ["identity"]}) or "")
        t.assertIn("Identity", _validation(f, {"hold": {"Identity": ["a/**"]}}) or "")
        t.assertIn("hold.identity", _validation(f, {"hold": {"identity": [""]}}) or "")

    def c_class_holds(self, f):
        for cls in f.HOLD_CLASSES:
            glob = HOLD_ENTRY["hold"][cls][0]
            path = glob.replace("**", "file.txt")
            self.holds(f, HOLD_API, [path], cls, glob, path)

    def c_exclude_wins(self, f):
        decision, reasons = self.not_held(f, HOLD_API, ["deploy/docs/readme.md"])
        self.t.assertFalse([r for r in reasons if "execution" in r], reasons)
        reasons = self.holds(f, HOLD_API, ["deploy/docs/readme.md", "deploy/run.sh"], "deploy/run.sh")
        self.t.assertFalse([r for r in reasons if "deploy/docs/readme.md" in r], reasons)

    def c_unlisted_and_floor_only(self, f):
        decision, reasons = self.not_held(f, HOLD_API, ["docs/guide.md", "src/util.py"], floor_hit=["src/util.py"])
        self.t.assertFalse([r for r in reasons if r.startswith("owner hold")], reasons)
        self.t.assertEqual(decision, "second-review")

    def c_rename_source(self, f):
        self.holds(f, HOLD_API, ["old/x.py", "auth/y.py"])
        self.holds(f, HOLD_API, ["auth/x.py", "old/y.py"], "auth/x.py")

    def c_kit_wide_paths_do_not_apply(self, f):
        decision, reasons = self.not_held(f, HOLD_API, ["cloud/launcher/token_mint.py", "bin/reassign.py"])   # kit-wide top tier paths that are not machinery
        self.t.assertFalse([r for r in reasons if "top tier path" in r], reasons)

    def c_rating_still_holds(self, f):
        self.holds(f, HOLD_API, ["docs/guide.md"], "rating:", impacts=[("Catastrophic", "Frequent", None)])
        decision, reasons = hold_decide(f, HOLD_API, ["docs/guide.md"], impacts=[("Catastrophic", "Frequent", "data")])
        self.t.assertEqual(decision, "second-review", reasons)   # a class the matrix names as not top tier asks, never holds

    def c_escape_holds(self, f):
        self.holds(f, HOLD_API, ["../x"], "outside the repository")

    def c_no_entry_as_before(self, f):
        matrix = f.load_matrix()
        for paths in (["shims/gh"], ["auth/login.py"], ["src/a.py"], ["docs/x.md"]):
            floor = ["src/a.py"] if paths == ["src/a.py"] else ()
            before = f.owner_decide(matrix, paths, [], None, floor, {}, None)
            self.t.assertEqual(hold_decide(f, HOLD_WEB, paths, floor_hit=floor), before, paths)
            self.t.assertEqual(hold_decide(f, "acme-org/other", paths, floor_hit=floor), before, paths)
        self.holds(f, HOLD_WEB, ["shims/gh"], "top tier path (credentials): shims/gh")
        self.t.assertEqual(hold_decide(f, HOLD_WEB, ["auth/login.py"])[0], "none")
        self.t.assertEqual(hold_decide(f, HOLD_WEB, ["src/a.py"], floor_hit=["src/a.py"])[0], "second-review")

    def c_machinery_excludes_refused(self, f):
        for glob in ("bin/impact_hold.py", "bin/**", "**", "docs/review-laws/**", "bin/impact_*.py", "./bin/impact_hold.py", "BIN/impact_hold.py"):
            msg = _validation(f, {"hold": {"execution": ["x/**"]}, "hold_exclude": [glob]})
            self.t.assertTrue(msg and "hold_exclude" in msg and "machinery" in msg, (glob, msg))
        self.t.assertIn("bin/impact_hold.py", _validation(f, {"hold": {"execution": ["x/**"]}, "hold_exclude": ["bin/impact_hold.py"]}))
        for glob in ("docs/**", "deploy/docs/**", "tests/**"):
            self.t.assertIsNone(_validation(f, {"hold": {"execution": ["x/**"]}, "hold_exclude": [glob]}), glob)

    def c_kit_machinery_stays_held(self, f):
        kit = hold_kit(f)
        config = hold_config({"hold": {"execution": ["x/**"]}, "hold_exclude": ["docs/**", "bin/**"]}, kit)   # written past the check on purpose
        matrix = f.load_matrix()
        for path in ("bin/impact_hold.py", "docs/review-laws/impact-matrix.json"):
            self.holds(f, kit, [path], "clear_code_paths", config=config)
            self.holds(f, kit, ["old/x.py", path], "clear_code_paths", config=config)   # as a rename source
        self.holds(f, kit, ["shims/gh"], "top tier path", config=config)
        closure = matrix["owner_hold"]["delegated_clear"]["closure_files"][1]
        self.t.assertEqual(hold_decide(f, kit, [closure], config=config), hold_decide(f, kit, [closure], config=hold_config({"globs": ["x/**"]}, kit)))   # the closure rule is the delegated clear's; the floor judges it as before

    def c_kit_top_tier_and_narrow_apply(self, f):
        kit = hold_kit(f)
        config = hold_config({"hold": {"execution": ["x/**"]}}, kit)
        decision, reasons = hold_decide(f, kit, ["agents/foo.md"], config=config)
        self.t.assertEqual(decision, "hold", reasons)
        self.t.assertTrue(any("top tier path (permissions)" in r for r in reasons), reasons)

    def c_invalid_falls_back(self, f):
        for bad in ({"hold": "auth/**"}, {"hold": {"identity": 7}}, {"hold": {}, "hold_exclude": "x"}):
            reasons = self.holds(f, HOLD_API, ["cloud/launcher/token_mint.py"], "kit-wide default applied", "top tier path (credentials): cloud/launcher/token_mint.py", config=hold_config(bad))
            self.t.assertTrue(any("invalid" in r for r in reasons), reasons)

    def c_empty_class_holds_nothing(self, f):
        self.holds(f, HOLD_API, ["src/a.py"], "owner hold", config=hold_config({"hold": {"identity": ["**"]}}))
        empty = hold_config({"hold": {"identity": [], "credentials": [], "authority": [], "execution": []}})
        self.t.assertEqual(hold_decide(f, HOLD_API, ["src/a.py"], config=empty), ("none", []))
        self.t.assertEqual(hold_decide(f, HOLD_API, ["src/a.py"], config=hold_config({"hold": {}}))[0], "none")

    def c_report_carries_the_hold(self, f):
        """S20: the real pr_floor_report (fake gh read) carries the entry's hold: the wiring, not only owner_decide."""
        engine = mock.Mock()
        engine.config, engine.state_dir, engine.audit = hold_config(), None, None
        raw = {"files": [{"path": "auth/login.py"}], "changedFiles": 1, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}
        with mock.patch.object(sb, "read_pr_json", return_value=raw), mock.patch.object(sb, "agent_block_states", return_value={}):
            report = sb.pr_floor_report(engine, HOLD_API, 5)
        self.t.assertEqual(report["owner_decision"], "hold", report["owner_reasons"])
        self.t.assertTrue(any("identity" in r and "auth/**" in r for r in report["owner_reasons"]), report["owner_reasons"])

    def names(self):
        return [n for n in dir(self) if n.startswith("c_")]


def _hold_mutants(f):
    """{mutant: (replacements for mock.patch.object(f, name, ...), the checks one of which must fail)}; the per-class mutants derive from HOLD_CLASSES."""
    real_hits, real_hold, real_decide = f.entry_hits, f.entry_hold, f.owner_decide
    out = {}

    def no_exclude(paths, hold, exclude):
        return real_hits(paths, hold, [])

    def empty_is_all(paths, hold, exclude):
        return real_hits(paths, {c: (g or ["**"]) for c, g in hold.items()}, exclude)

    def kit_wide_on_entry(config, repo, matrix=None):
        got = real_hold(config, repo, matrix)
        return got[:3] + (False,) if got and got[0] == "ok" else got

    def entry_loosens_kit(config, repo, matrix=None):
        got = real_hold(config, repo, matrix)
        return got[:3] + (True,) if got and got[0] == "ok" else got

    def entry_drops_clear_paths(matrix, *a, **kw):
        entry = kw.get("entry") if "entry" in kw else (a[7] if len(a) > 7 else None)
        if entry is None:
            return real_decide(matrix, *a, **kw)
        with mock.patch.object(f, "clear_code_paths", return_value=(None, None)):
            return real_decide(matrix, *a, **kw)

    def invalid_is_no_hold(matrix, *a, **kw):
        a = list(a)
        if isinstance(kw.get("entry"), tuple) and kw["entry"][0] == "invalid":
            kw = dict(kw, entry=("ok", {}, [], True))
        elif len(a) > 7 and isinstance(a[7], tuple) and a[7][0] == "invalid":
            a[7] = ("ok", {}, [], True)
        return real_decide(matrix, *a, **kw)

    def invalid_silently(matrix, *a, **kw):
        decision, reasons = real_decide(matrix, *a, **kw)
        return decision, [r for r in reasons if "kit-wide default applied" not in r]

    out["M1 ignore hold_exclude"] = ({"entry_hits": no_exclude}, {"c_exclude_wins"})
    out["M2 kit-wide paths apply to an entry repository"] = ({"entry_hold": kit_wide_on_entry}, {"c_kit_wide_paths_do_not_apply"})
    out["M3 empty class holds all"] = ({"entry_hits": empty_is_all}, {"c_empty_class_holds_nothing"})
    out["M4 machinery allowed in hold_exclude"] = ({"exclude_hits_machinery": lambda glob, machinery: []}, {"c_machinery_excludes_refused"})
    out["M4b only exact strings refused"] = ({"exclude_hits_machinery": lambda glob, machinery: [m for m in machinery if m == glob]}, {"c_machinery_excludes_refused"})
    out["M5 an entry replaces the kit repository's top tier lists"] = ({"entry_hold": entry_loosens_kit}, {"c_kit_top_tier_and_narrow_apply"})
    out["M5b an entry drops the clear code paths"] = ({"owner_decide": entry_drops_clear_paths}, {"c_kit_machinery_stays_held"})
    out["M6 an invalid entry means no hold"] = ({"owner_decide": invalid_is_no_hold}, {"c_invalid_falls_back"})
    out["M6b an invalid entry falls back silently"] = ({"owner_decide": invalid_silently}, {"c_invalid_falls_back"})
    for cls in f.HOLD_CLASSES:
        def skip(paths, hold, exclude, cls=cls):
            return real_hits(paths, {c: g for c, g in hold.items() if c != cls}, exclude)
        out["M7 class %s is not read" % cls] = ({"entry_hits": skip}, {"c_class_holds"})

    # M8..M14: the checks that had no mutant (the validation, S15, S16, S18, S19, S20)
    def entry_of(a, kw):
        return kw["entry"] if "entry" in kw else (a[7] if len(a) > 7 else None)

    def is_ok(entry):
        return isinstance(entry, tuple) and entry[0] == "ok"

    def floor_globs_hold(paths, hold, exclude):
        return real_hits(paths, hold, exclude) + [("identity", "src/**", p) for p in paths if f.matches(p, ["src/**"])]

    def new_path_only(paths, hold, exclude):
        return real_hits(list(paths)[-1:], hold, exclude)

    def rating_silenced(matrix, *a, **kw):
        decision, reasons = real_decide(matrix, *a, **kw)
        if not is_ok(entry_of(a, kw)):
            return decision, reasons
        kept = [r for r in reasons if not r.startswith("rating:")]
        return ("hold", kept) if kept else ("none", [])

    def escape_unjudged(matrix, *a, **kw):
        decision, reasons = real_decide(matrix, *a, **kw)
        if not is_ok(entry_of(a, kw)):
            return decision, reasons
        kept = [r for r in reasons if "outside the repository" not in r]
        return ("hold", kept) if kept else ("none", [])

    def refuse_all(raw):
        if raw and raw.get("repos"):
            raise ValueError("'impact_floor.repos' hold is refused")

    def entry_for_every_repo(config, repo, matrix=None):
        return real_hold(config, repo, matrix) or ("ok", {}, [], True)

    out["M15 a repository with no hold entry gets an empty one"] = ({"entry_hold": entry_for_every_repo}, {"c_no_entry_as_before"})
    out["M8 a valid hold entry is refused"] = ({"validate_config": refuse_all}, {"c_validate_accepts"})
    out["M9 an invalid hold entry is accepted"] = ({"validate_config": lambda raw: None}, {"c_validate_refuses"})
    out["M10 the floor globs hold an entry repository"] = ({"entry_hits": floor_globs_hold}, {"c_unlisted_and_floor_only"})
    out["M11 only a rename's new path is read"] = ({"entry_hits": new_path_only}, {"c_rename_source"})
    out["M12 an entry silences the rating"] = ({"owner_decide": rating_silenced}, {"c_rating_still_holds"})
    out["M13 an escaping path is not held under an entry"] = ({"owner_decide": escape_unjudged}, {"c_escape_holds"})
    out["M14 the report is not wired to the entry"] = ({"entry_hold": lambda config, repo, matrix=None: None}, {"c_report_carries_the_hold"})
    return out


class TestImpactHoldByEntry(unittest.TestCase):
    """#1241: a repository's own owner hold by class, with an exclude. Pure checks on the real module, mutants on broken ones, and the real report."""

    def setUp(self):
        self.f = sb.load_impact_floor()   # the module itself: a patch on the lazy proxy would not reach the functions
        self.checks = HoldChecks(self)

    def test_every_scenario_check_passes_on_the_real_code(self):
        for name in self.checks.names():
            with self.subTest(name):
                getattr(self.checks, name)(self.f)

    def test_the_four_classes_are_the_ones_the_docs_name(self):
        self.assertEqual(sorted(self.f.HOLD_CLASSES), ["authority", "credentials", "execution", "identity"])
        self.assertEqual(sorted(HOLD_ENTRY["hold"]), sorted(self.f.HOLD_CLASSES), "the fixture lists every class")

    def test_every_mutant_makes_its_named_check_fail(self):
        mutants = _hold_mutants(self.f)
        self.assertEqual(len([m for m in mutants if m.startswith("M7")]), len(self.f.HOLD_CLASSES), "one mutant per class")
        for name, (patches, expected) in mutants.items():
            with contextlib.ExitStack() as stack:
                for attr, replacement in patches.items():
                    stack.enter_context(mock.patch.object(self.f, attr, replacement))
                failed = set()
                for check in self.checks.names():
                    try:
                        getattr(self.checks, check)(self.f)
                    except AssertionError:
                        failed.add(check)
            self.assertTrue(failed & expected, "mutant %r survived: failed %s, expected one of %s" % (name, sorted(failed), sorted(expected)))

    def test_every_check_has_a_mutant_that_makes_that_check_fail(self):
        """Completeness, derived from the code: each c_* check is named by a mutant, and under that mutant that very check fails (a check added without a mutant fails here)."""
        mutants = _hold_mutants(self.f)
        named = {c for _, expected in mutants.values() for c in expected}
        self.assertEqual(sorted(set(self.checks.names()) - named), [], "checks with no mutant")
        self.assertEqual(sorted(named - set(self.checks.names())), [], "mutants naming no check")
        for name, (patches, expected) in mutants.items():
            for check in sorted(expected):
                with contextlib.ExitStack() as stack:
                    for attr, replacement in patches.items():
                        stack.enter_context(mock.patch.object(self.f, attr, replacement))
                    with self.assertRaises(AssertionError, msg="mutant %r does not fail its check %s" % (name, check)):
                        getattr(self.checks, check)(self.f)

    def test_the_kit_name_and_machinery_are_read_from_the_matrix_not_copied(self):
        text = Path(self.f.__file__).read_text()
        for name in self.f.default_floors():
            self.assertNotIn(name, text)
        for path in self.f.machinery_paths(self.f.load_matrix()):
            self.assertNotIn('"%s"' % path, text, path)

    def test_the_daemon_check_refuses_an_invalid_entry_naming_the_key(self):
        for entry in ({"hold": "auth/**"}, {"hold": {"identity": ["a/**"]}, "hold_exclude": ["bin/**"]}):
            with self.assertRaises(ValueError) as caught:
                self.f.validate({"repos": {HOLD_API: entry}}, "config")
            self.assertIn("hold", str(caught.exception))

    def test_the_real_report_carries_the_hold_and_a_rename_source_holds(self):
        engine = mock.Mock()
        engine.config, engine.state_dir, engine.audit = hold_config(), None, None

        def report(item, repo=HOLD_API):
            raw = {"files": [item], "changedFiles": 1, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}
            with mock.patch.object(sb, "read_pr_json", return_value=raw), mock.patch.object(sb, "agent_block_states", return_value={}):
                return sb.pr_floor_report(engine, repo, 5)
        for item, expect in (({"path": "auth/login.py"}, "auth/**"), ({"path": "old/y.py", "previous_filename": "auth/x.py"}, "auth/x.py")):
            got = report(item)
            self.assertEqual(got["owner_decision"], "hold", got["owner_reasons"])
            self.assertTrue(any(expect in r and "identity" in r for r in got["owner_reasons"]), got["owner_reasons"])
        self.assertEqual(report({"path": "cloud/launcher/token_mint.py"})["owner_decision"], "none")
        self.assertEqual(report({"path": "cloud/launcher/token_mint.py"}, HOLD_WEB)["owner_decision"], "hold")

    def test_u2_a_repository_hold_entry_never_lifts_the_delegated_clears_closure_and_new_file_refusals(self):
        """#1241 U2: the clear takes no entry; the entry reaches it only through the report. The same files, the same closure mapping, once with a hold entry that lists nothing for them and once with no entry: the clear refuses the same two things."""
        dc = sb.load_module("delegated_clear")
        rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
        closure_path, new_path = "bin/agent_presence.py", "bin/zz_acme_new_module.py"
        self.assertIn(closure_path, rule["closure_files"])
        self.assertTrue(sb.impact_floor.matches(new_path, rule["new_file_globs"]))
        files = [{"filename": closure_path, "status": "modified"}, {"filename": new_path, "status": "added"}]
        mapping = {closure_path: ["it changes a line of the closure (acme definition)"], new_path: []}
        engine = mock.Mock()
        engine.config, engine.state_dir, engine.audit = hold_config(dict(HOLD_ENTRY, hold={"identity": ["auth/**"]}, hold_exclude=[])), None, None   # the entry names nothing under bin/: the most permissive one
        raw = {"files": [{"path": f["filename"]} for f in files], "changedFiles": len(files), "headRefOid": "a" * 40, "baseRefOid": "b" * 40}

        def refused(module, repo):
            with mock.patch.object(sb, "read_pr_json", return_value=raw), mock.patch.object(sb, "agent_block_states", return_value={}):
                report = sb.pr_floor_report(engine, repo, 5)
            report = dict(report, review_action="none")   # an independent review is recorded: only the closure and the new file are left to refuse
            return module.refusals(rule, report, files, matches=sb.impact_floor.matches, closure=mapping)
        with_entry, without = refused(dc, HOLD_API), refused(dc, "acme-org/widget-none")
        self.assertEqual(with_entry, without, "the entry changes what the clear refuses")
        self.assertEqual(len(with_entry), 2, with_entry)
        self.assertTrue(any("acme definition" in r for r in with_entry), with_entry)
        self.assertTrue(any("adds, deletes or renames a file" in r for r in with_entry), with_entry)
        old = "if matches(path, rule[\"closure_files\"]) or matches(path, [\"bin/switchboard/*.py\"]):"
        source = (BIN / "delegated_clear.py").read_text()
        self.assertEqual(source.count(old), 1)
        import types
        mutant = types.ModuleType("delegated_clear_mutant")   # a build that lets an entry skip the closure rule: here, skipped for the repository that has one
        mutant.__file__ = str(BIN / "delegated_clear.py")
        exec(compile(source.replace(old, "if False:"), str(BIN / "delegated_clear.py"), "exec"), mutant.__dict__)
        self.assertNotEqual(refused(mutant, HOLD_API), refused(dc, "acme-org/widget-none"), "a build that skips the closure rule for an entry passes this test")
        self.assertEqual(len(refused(mutant, HOLD_API)), 1)


class TestOperatorHoldDocs(unittest.TestCase):
    """#1241 clause 7: the operator section of docs/switchboard.md."""

    def section(self):
        text = (SWITCHBOARD.parent.parent / "docs" / "switchboard.md").read_text()
        heads = list(re.finditer(r"^(#+) .*$", text, re.M))
        mine = [i for i, m in enumerate(heads) if "per-repository owner hold" in m.group(0).lower()]
        self.assertEqual(len(mine), 1, "exactly one section heading for the per-repository hold")
        first = heads[mine[0]]
        end = text.index("\n**", first.end())   # the section ends where the next bold paragraph starts
        return text[first.start():end]

    def test_the_section_states_the_key_the_classes_and_the_fallback(self):
        text = self.section()
        for word in ("impact_floor.repos", "hold_exclude", "database", "schema", "container", "independent impact rating", "config.json", "kit-wide default", "empty"):
            self.assertIn(word, text)
        for cls in sb.impact_floor.HOLD_CLASSES:
            self.assertIn(cls, text)

    def test_only_neutral_repositories_are_named(self):
        text = self.section()
        kit = {n.lower() for n in sb.impact_floor.default_floors()}
        slugs = re.findall(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=\"\s*:\s*\{|`\s*:|\"\s*:)", text) + re.findall(r"owner/repo|[a-z0-9-]+/[a-z0-9-]+(?=\.hold)", text)
        self.assertIn("acme-org/widget-api", text)
        for slug in slugs:
            self.assertTrue(slug.startswith("acme-org/") or slug.lower() in kit or slug == "owner/repo", slug)
        self.assertNotRegex(text, r"github\.com/(?!acme-org/)")



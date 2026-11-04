"""A definition or binding ADDED under a clear-closure name in the entry or any part is refused by the clear (#1339, follow-up of #1092 S13).

THE CLOSED RULE (`delegated_clear.bound_names`): the names a file binds at module level are every name bound by ANY statement that runs at import time, found by walking the module body through
if/try/for/while/with/match blocks and not into function or class bodies. An ADDED bound name (the head's set minus the base's) that is a closure name is refused, naming the name and the file, for
every changed file under bin/switchboard/ and for the entry, listed in the matrix or not. One test per binder form (a missing form shows as one line), the real glue (`impact_hold.delegated_clear_map`,
the function that calls the rule) with a mutant that removes the call, and the entry's reasons compared with the recorded baseline.

LIMITS, stated: `exec`/`eval`, writes through `globals()`/`vars()`/`setattr` on the module, bindings made by a call (`globals().update(...)`) and `from x import *` are not followed; the closure names are
the ones the matrix carries (`closure_roots`, `rating_roots`, `module_roots`), so a closure-read binding the matrix does not name is not caught by the clear (CI's tools/derive-closure --check lists the part
that holds it); `delegated_clear_inputs` (which asks for the map of an unlisted part) is not driven here, only the map it calls.
"""
import base64
import difflib
import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import impact_floor  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULE = json.loads((REPO / "docs/review-laws/impact-matrix.json").read_text())["owner_hold"]["delegated_clear"]
BASE = "import os\n\n\ndef keep(a):\n    return a\n"
NAME = "boss_name"

# every way a statement that runs at import time binds NAME: one row each, the added text appended to BASE
BINDERS = {
    "def": "\n\ndef boss_name(a, b):\n    return a\n",
    "async def": "\n\nasync def boss_name(a, b):\n    return a\n",
    "class": "\n\nclass boss_name:\n    pass\n",
    "assign": "\n\nboss_name = 1\n",
    "assign lambda": "\n\nboss_name = lambda a, b: a\n",
    "assign to a tuple": "\n\nboss_name, other = 1, 2\n",
    "assign to a nested tuple": "\n\n(first, (boss_name, other)) = (1, (2, 3))\n",
    "assign starred": "\n\nfirst, *boss_name = [1, 2, 3]\n",
    "chained assign": "\n\nother = boss_name = 1\n",
    "annotated assign": "\n\nboss_name: int = 1\n",
    "augmented assign": "\n\nboss_name += 1\n",
    "import as": "\n\nimport os as boss_name\n",
    "import dotted": "\n\nimport boss_name.sub\n",
    "import dotted as": "\n\nimport os.path as boss_name\n",
    "from import": "\n\nfrom os import boss_name\n",
    "from import as": "\n\nfrom os import path as boss_name\n",
    "for target": "\n\nfor boss_name in range(3):\n    pass\n",
    "for tuple target": "\n\nfor (i, boss_name) in enumerate([1]):\n    pass\n",
    "with as": "\n\nwith open('x') as boss_name:\n    pass\n",
    "with as tuple": "\n\nwith a as (boss_name, other):\n    pass\n",
    "except as": "\n\ntry:\n    pass\nexcept Exception as boss_name:\n    pass\n",
    "walrus in a condition": "\n\nif (boss_name := 1):\n    pass\n",
    "walrus in a decorator": "\n\n@(boss_name := dec)\ndef g():\n    pass\n",
    "walrus in a default": "\n\ndef g(a=(boss_name := 1)):\n    pass\n",
    "walrus in a comprehension": "\n\nxs = [y for y in z if (boss_name := y)]\n",
    "global declaration": "\n\ndef g():\n    global boss_name\n    boss_name = 1\n",
    "match capture": "\n\nmatch value:\n    case [boss_name]:\n        pass\n",
    "match star": "\n\nmatch value:\n    case [first, *boss_name]:\n        pass\n",
    "match as": "\n\nmatch value:\n    case 1 as boss_name:\n        pass\n",
    "match mapping rest": "\n\nmatch value:\n    case {'a': 1, **boss_name}:\n        pass\n",
    "del": "\n\ndel boss_name\n",
    "walrus in a lambda default": "\n\nh = lambda a=(boss_name := 1): a\n",
    "walrus in a return annotation": "\n\ndef g() -> (boss_name := int):\n    pass\n",
    "walrus in an argument annotation": "\n\ndef g(a: (boss_name := int)):\n    pass\n",
    "walrus in a keyword-only default": "\n\ndef g(*, a=(boss_name := 1)):\n    pass\n",
    "walrus in a class keyword": "\n\nclass K(Base, flag=(boss_name := 1)):\n    pass\n",
    "walrus in an except type": "\n\ntry:\n    pass\nexcept (boss_name := Exception):\n    pass\n",
    "except star as": "\n\ntry:\n    pass\nexcept* Exception as boss_name:\n    pass\n",
    "match class pattern capture": "\n\nmatch value:\n    case K(a=boss_name):\n        pass\n",
    "match guard walrus": "\n\nmatch value:\n    case _ if (boss_name := 1):\n        pass\n",
    "walrus in a with item": "\n\nwith (boss_name := a):\n    pass\n",
    "walrus in a for iterable": "\n\nfor i in (boss_name := [1]):\n    pass\n",
    "walrus in an assert": "\n\nassert (boss_name := 1)\n",
    "walrus in a call argument": "\n\nprint((boss_name := 1))\n",
    "def under if True": "\n\nif True:\n    def boss_name(a, b):\n        return a\n",
    "def under else": "\n\nif False:\n    pass\nelse:\n    def boss_name(a, b):\n        return a\n",
    "def under try": "\n\ntry:\n    def boss_name(a, b):\n        return a\nexcept Exception:\n    pass\n",
    "def under except": "\n\ntry:\n    pass\nexcept Exception:\n    def boss_name(a, b):\n        return a\n",
    "def under finally": "\n\ntry:\n    pass\nfinally:\n    def boss_name(a, b):\n        return a\n",
    "def under for": "\n\nfor i in range(2):\n    def boss_name(a, b):\n        return a\n",
    "def under while": "\n\nwhile False:\n    def boss_name(a, b):\n        return a\n",
    "def under with": "\n\nwith a:\n    def boss_name(a, b):\n        return a\n",
    "def under match": "\n\nmatch value:\n    case _:\n        def boss_name(a, b):\n            return a\n",
    "assign under three blocks": "\n\nif a:\n    try:\n        for i in b:\n            boss_name = 1\n    finally:\n        pass\n",
}
# what does NOT bind NAME at module level
NOT_BOUND = {
    "inside a function body": "\n\ndef g():\n    boss_name = 1\n    return boss_name\n",
    "inside a method": "\n\nclass K:\n    def m(self):\n        boss_name = 1\n",
    "a class attribute": "\n\nclass K:\n    boss_name = 1\n",
    "a parameter": "\n\ndef g(boss_name):\n    return boss_name\n",
    "a lambda parameter": "\n\nh = lambda boss_name: boss_name\n",
    "a comprehension variable": "\n\nxs = [boss_name for boss_name in range(2)]\n",
    "an attribute store": "\n\nobj.boss_name = 1\n",
    "a subscript store": "\n\ntable['boss_name'] = 1\n",
    "a read": "\n\nvalue = boss_name\n",
    "a string": "\n\ntext = 'boss_name'\n",
    "a walrus in a lambda body": "\n\nh = lambda: (boss_name := 1)\n",
    "an async for target in a function": "\n\nasync def g():\n    async for boss_name in h():\n        pass\n",
    "a walrus in a function body": "\n\ndef g():\n    return (boss_name := 1)\n",
    "a name outside the closure": "\n\ndef not_a_closure_name_zz(a):\n    return a\n",
}


def diff(base, head):
    return "".join(list(difflib.unified_diff(base.splitlines(True), head.splitlines(True), "a/x", "b/x", n=3))[2:])


class TestShadowRule(unittest.TestCase):
    def setUp(self):
        self.m = load("delegated_clear_shadow", REPO / "bin" / "delegated_clear.py")
        self.names = self.m.shadow_names(RULE)

    def test_boss_name_is_a_closure_name_and_the_names_include_the_rating_roots(self):
        self.assertIn(NAME, self.names)
        self.assertTrue(set(RULE["rating_roots"]) <= self.names, "rating_roots are closure names too")

    def test_every_binder_form_adds_the_name_and_is_refused_naming_it_and_the_file(self):
        for label, add in BINDERS.items():
            for path in ("bin/switchboard/judge.py", "bin/switchboard/audit.py"):   # not listed in the matrix, and listed
                with self.subTest(form=label, file=path.rsplit("/", 1)[1]):
                    got = self.m.shadow_refusals(BASE, BASE + add, self.names, path)
                    self.assertTrue(any(NAME in r and path in r for r in got), got)

    def test_what_does_not_bind_the_name_at_module_level_is_not_refused(self):
        for label, add in NOT_BOUND.items():
            with self.subTest(form=label):
                self.assertEqual(self.m.shadow_refusals(BASE, BASE + add, self.names, "bin/switchboard/judge.py"), [])

    def test_the_bypasses_the_reviews_found_are_named_regression_markers(self):
        for add in (BINDERS["import as"], BINDERS["def under if True"], BINDERS["walrus in a lambda default"], BINDERS["walrus in a return annotation"], BINDERS["walrus in an except type"]):
            self.assertTrue(self.m.shadow_refusals(BASE, BASE + add, self.names, "bin/switchboard/judge.py"), add)

    def test_a_name_already_bound_is_not_added_and_a_syntax_error_is_refused(self):
        had = BASE + "\ndef boss_name(a):\n    return a\n"
        self.assertEqual(self.m.shadow_refusals(had, had.replace("return a", "return 2"), self.names, "bin/switchboard/x.py"), [])
        self.assertEqual(self.m.shadow_refusals(had, had + "\nif True:\n    boss_name = 3\n", self.names, "bin/switchboard/x.py"), [], "bound before: a second binding is not an addition (stated: the first one was already there; a listed part and the entry are still judged by closure_refusals)")
        self.assertTrue(self.m.shadow_refusals(BASE, "def (", self.names, "bin/switchboard/x.py"))
        self.assertTrue(self.m.shadow_refusals("def (", BASE, self.names, "bin/switchboard/x.py"))

    def test_the_stated_limits_are_not_followed(self):
        for add in ("\n\nglobals().update(boss_name=1)\n", "\n\nexec('boss_name = 1')\n", "\n\nsetattr(sys.modules[__name__], 'boss_name', 1)\n", "\n\nfrom os import *\n"):
            with self.subTest(add=add.strip()[:30]):
                self.assertEqual(self.m.shadow_refusals(BASE, BASE + add, self.names, "bin/switchboard/judge.py"), [], "a stated limit")


class TestShadowGlue(unittest.TestCase):
    """impact_hold.delegated_clear_map, the function that really applies the rule to the entry, listed parts and unlisted parts."""

    def setUp(self):
        self.m = load("delegated_clear_glue", REPO / "bin" / "delegated_clear.py")
        self.hold = load("impact_hold_glue", REPO / "bin" / "impact_hold.py")
        self.hold.impact_floor = impact_floor
        self.hold.load_module = lambda name: self.m
        self.names = self.m.shadow_names(RULE)

    def run_map(self, filename, base, head, readable=True):
        blobs = {"BLOBBASE": base, "BLOBHEAD": head}

        def fake(engine, runner, repo, path):
            if not readable:
                raise RuntimeError("unreadable")
            if "/contents/" in path:
                return {"type": "file", "sha": "BLOBBASE"}
            return {"encoding": "base64", "content": base64.b64encode(blobs[path.rsplit("/", 1)[1]].encode()).decode()}
        with mock.patch.object(self.hold, "_gh_json", fake):
            return self.hold.delegated_clear_map(None, None, "o/r", {"filename": filename, "sha": "BLOBHEAD", "patch": diff(base, head)}, "BASESHA", RULE)

    def test_a_listed_part_and_an_unlisted_part_refuse_the_added_name_and_the_old_check_alone_did_not(self):
        add = BINDERS["def"]
        for path, listed in (("bin/switchboard/audit.py", True), ("bin/switchboard/judge.py", False)):
            with self.subTest(file=path):
                self.assertEqual(impact_floor.matches(path, RULE["closure_files"]), listed)
                got = self.run_map(path, BASE, BASE + add)
                self.assertTrue(any(NAME in r and path in r for r in got), got)
                alone = self.m.closure_refusals(BASE, BASE + add, diff(BASE, BASE + add), RULE["module_roots"].get(path, []), set(), True)
                self.assertFalse(any(NAME in r for r in alone), "the check as it was lets the shadow through")

    def test_the_glue_applies_the_rule_to_the_two_bypass_forms_in_an_unlisted_part(self):
        for add in (BINDERS["import as"], BINDERS["def under if True"]):
            self.assertTrue(any(NAME in r for r in self.run_map("bin/switchboard/judge.py", BASE, BASE + add)), add)

    def test_a_mutant_that_removes_the_call_leaves_the_plants_unrefused(self):
        with mock.patch.object(self.m, "shadow_refusals", lambda *args: []):
            self.assertEqual(self.run_map("bin/switchboard/judge.py", BASE, BASE + BINDERS["def"]), [], "without the call nothing refuses it")
            self.assertFalse(any(NAME in r for r in self.run_map("bin/switchboard/audit.py", BASE, BASE + BINDERS["def"])))

    def test_an_outside_name_and_an_edit_of_a_non_closure_function_are_not_refused(self):
        self.assertEqual(self.run_map("bin/switchboard/judge.py", BASE, BASE + NOT_BOUND["a name outside the closure"]), [])
        self.assertEqual(self.run_map("bin/switchboard/audit.py", BASE, BASE + NOT_BOUND["a name outside the closure"]), [])

    def test_a_missing_merge_base_is_an_unreadable_part_too(self):
        with mock.patch.object(self.hold, "_gh_json", side_effect=AssertionError("nothing is read without a merge-base")):
            got = self.hold.delegated_clear_map(None, None, "o/r", {"filename": "bin/switchboard/judge.py", "sha": "BLOBHEAD", "patch": "x"}, None, RULE)
        self.assertTrue(got and "cannot be read" in got[0], got)

    def test_a_real_move_between_two_existing_parts_is_refused_on_the_receiving_file_and_a_non_closure_move_is_not(self):
        import ast as _ast

        def move(giving, receiving, name):
            source, target = (REPO / giving).read_text(), (REPO / receiving).read_text()
            node = next(n for n in _ast.parse(source).body if getattr(n, "name", None) == name)
            lines = source.split("\n")
            first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            cut = "\n".join(lines[first:node.end_lineno])
            return (source, "\n".join(lines[:first] + lines[node.end_lineno:])), (target, target.rstrip("\n") + "\n\n\n" + cut + "\n")
        for giving, receiving, name in (("bin/switchboard/decisions_a.py", "bin/switchboard/decisions_b.py", "parse_bare_pr_ref"), ("bin/switchboard/audit.py", "bin/switchboard/decision.py", "redact")):
            with self.subTest(move=name):
                self.assertIn(name, self.names)
                (g0, g1), (r0, r1) = move(giving, receiving, name)
                self.assertTrue(any(name in r for r in self.run_map(giving, g0, g1)), "the giving file changes a closure definition")
                got = self.run_map(receiving, r0, r1)
                self.assertTrue(any("it adds %s in %s" % (name, receiving) in r for r in got), got)   # the stated over-refusal: it stays the owner's own clear
        (g0, g1), (r0, r1) = move("bin/switchboard/facts.py", "bin/switchboard/reminders.py", "disk_status_line")
        self.assertNotIn("disk_status_line", self.names)
        self.assertEqual((self.run_map("bin/switchboard/facts.py", g0, g1), self.run_map("bin/switchboard/reminders.py", r0, r1)), ([], []))

    def test_an_unreadable_unlisted_part_is_refused_and_a_listed_one_is_unmapped_as_before(self):
        got = self.run_map("bin/switchboard/judge.py", BASE, BASE, readable=False)
        self.assertTrue(got and "judge.py" in got[0] and "cannot be read" in got[0], got)
        self.assertIsNone(self.run_map("bin/switchboard/audit.py", BASE, BASE, readable=False), "a listed part that cannot be read stays unmapped (the clear refuses it as unreadable)")

    def test_the_entry_keeps_its_recorded_baseline_reasons_exactly(self):
        entry = RULE["daemon_file"]
        base = "def boss_name(a):\n    return a\n"
        head = "def boss_name(a):\n    return a + 1\n"   # an edit inside a closure definition
        roots = self.m.daemon_roots(RULE)
        baseline = self.m.closure_refusals(base, head, diff(base, head), roots, set(RULE["clear_entry_points"]), False, list(RULE.get("dynamic_actions") or []))
        self.assertTrue(baseline)
        self.assertEqual(self.run_map(entry, base, head), baseline)
        other = "redact"
        self.assertIn(other, self.names)
        added = base + "\n%s = 1\n" % other   # an added closure name: the baseline (computed here the same way) decides first
        recorded = self.m.closure_refusals(base, added, diff(base, added), roots, set(RULE["clear_entry_points"]), False, list(RULE.get("dynamic_actions") or []))
        expected = recorded or self.m.shadow_refusals(base, added, self.names, entry)
        self.assertEqual(self.run_map(entry, base, added), expected)
        with mock.patch.object(self.m, "closure_refusals", lambda *args, **kw: []):
            self.assertTrue(any(other in r for r in self.run_map(entry, base, added)), "when the old check says nothing the added name is what the entry says")


class FakeGitHub:
    """What `delegated_clear_inputs` reads, as plain data: the files of one pull request (a status each), the pull, the compare and the blobs. The real function runs on it."""

    def __init__(self, files):
        self.files, self.blobs = files, {}

    def get(self, path):
        if "/pulls/7/files" in path:
            return [{k: v for k, v in f.items() if k not in ("base", "head")} for f in self.files]
        if path.endswith("/pulls/7"):
            return {"head": {"sha": "H1"}, "base": {"sha": "B1"}, "changed_files": len(self.files)}
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": "MB"}}
        raise AssertionError("unexpected read: " + path)

    def blob(self, engine, runner, repo, path):   # `_gh_json`, the blobs API
        if "/contents/" in path:
            return {"type": "file", "sha": "BLOBBASE:" + path.split("/contents/")[1].split("?")[0]}
        key = path.split("/git/blobs/", 1)[1]
        kind, name = key.split(":", 1)
        text = next(f["base" if kind == "BLOBBASE" else "head"] for f in self.files if f["filename"] == name)
        return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def inputs_for(testcase, hold, module, files):
    """The REAL `delegated_clear_inputs` on a fake GitHub: (the refusals of the real `refusals` over what it returned, the inputs)."""
    github = FakeGitHub(files)
    state = Path(tempfile.mkdtemp(prefix="shadow-state-"))
    testcase.addCleanup(shutil.rmtree, str(state), True)
    engine = types.SimpleNamespace(state_dir=state)
    report = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "review_action": "go", "review_class": None, "registrar_reasons": [], "head": "H1", "changed_paths": [f["filename"] for f in files]}
    with mock.patch.object(hold, "impact_carry_get", lambda e, r, repo: github.get, create=True), mock.patch.object(hold, "_gh_json", github.blob):
        got, why = hold.delegated_clear_inputs(engine, None, "o/r", 7, False, report)
    testcase.assertIsNone(why, why)
    return module.refusals(got["rule"], got["report"], got["files"], impact_floor.matches, got["closure"]), got


def entry_pair():
    base = "def boss_name(a):\n    return a\n"
    return base, "def boss_name(a):\n    return a + 1\n"


def file_row(kind, status):
    if kind == "entry":
        base, head = entry_pair()
        path = RULE["daemon_file"]
    else:
        base, head = BASE, BASE + BINDERS["def"]
        path = {"listed": "bin/switchboard/audit.py", "unlisted": "bin/switchboard/judge.py"}[kind]
    row = {"filename": path, "base": base, "head": head, "patch": diff(base, head), "sha": "BLOBHEAD:" + path}
    if status is not None:
        row["status"] = status
    return row


class TestShadowStatuses(unittest.TestCase):
    """The REAL `delegated_clear_inputs` (on a fake GitHub) feeds the real `refusals`: a map is built only for a file whose status is `modified`. Any other status of a part or of the entry is unmapped and REFUSED
    ('cannot be mapped'), never skipped: a listed part, an unlisted part and the entry alike."""
    STATUSES = ("changed", "unchanged", None, "renamed", "copied", "removed", "added", "modified")

    def setUp(self):
        self.m = load("delegated_clear_statuses", REPO / "bin" / "delegated_clear.py")
        self.hold = load("impact_hold_statuses", REPO / "bin" / "impact_hold.py")
        self.hold.impact_floor = impact_floor
        self.hold.load_module = lambda name: self.m

    def test_every_status_of_a_listed_part_an_unlisted_part_and_the_entry(self):
        self.assertFalse(impact_floor.matches("bin/switchboard/judge.py", RULE["closure_files"]))
        for kind in ("listed", "unlisted", "entry"):
            for status in self.STATUSES:
                with self.subTest(file=kind, status=status):
                    got, inputs = inputs_for(self, self.hold, self.m, [file_row(kind, status)])
                    path = file_row(kind, status)["filename"]
                    if status == "modified":
                        self.assertIn(path, inputs["closure"], "the real function maps a modified file")
                        self.assertTrue(any(NAME in r for r in got), "a mapped file is judged: %s" % got)
                    else:
                        self.assertNotIn(path, inputs["closure"], "and maps nothing else")
                        self.assertTrue(any("cannot be mapped" in r for r in got), got)

    def test_a_mutant_that_restores_the_skip_lets_the_unmapped_unlisted_part_through(self):
        source = (REPO / "bin" / "delegated_clear.py").read_text()
        old = 'matches(path, rule["closure_files"]) or matches(path, ["bin/switchboard/*.py"]):'
        self.assertIn(old, source)
        mutant = source.replace(old, 'matches(path, rule["closure_files"]) or (matches(path, ["bin/switchboard/*.py"]) and (closure or {}).get(item["filename"]) is not None):', 1)
        spec = importlib.util.spec_from_loader("delegated_clear_mutant", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__file__"] = str(REPO / "bin" / "delegated_clear.py")
        exec(compile(mutant, "delegated_clear_mutant", "exec"), module.__dict__)
        for status in ("changed", "unchanged", None):
            with self.subTest(status=status):
                got, _ = inputs_for(self, self.hold, module, [file_row("unlisted", status)])
                self.assertEqual(got, [], "the old skip passes it")
                self.assertTrue(inputs_for(self, self.hold, self.m, [file_row("unlisted", status)])[0], "the rule refuses it")

    def test_the_part_glob_matches_a_dotfile_and_not_a_subdirectory(self):   # stated: the parts directory is flat (derive-closure fails closed on any other file there)
        self.assertTrue(impact_floor.matches("bin/switchboard/.x.py", ["bin/switchboard/*.py"]))
        self.assertFalse(impact_floor.matches("bin/switchboard/sub/x.py", ["bin/switchboard/*.py"]))


class TestReasonsAreOneCleanLine(unittest.TestCase):
    """A path comes from a pull request: a control character in it (an escape, a newline) must not reach ANY reason, so it cannot forge a line in a posted one. One place cleans (`_item`); one row per reason kind,
    through the real `refusals`."""
    DIRTY = "a\x1b[31m\nb.py"

    def setUp(self):
        self.m = load("delegated_clear_clean", REPO / "bin" / "delegated_clear.py")
        self.hold = load("impact_hold_clean", REPO / "bin" / "impact_hold.py")
        self.hold.impact_floor = impact_floor
        self.hold.load_module = lambda name: self.m

    def reasons(self, files, closure=None):
        report = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "review_action": "go", "review_class": None, "registrar_reasons": [], "changed_paths": [f["filename"] for f in files]}
        return self.m.refusals(RULE, report, files, impact_floor.matches, closure or {})

    def assertOneCleanLine(self, got, kind):
        self.assertTrue(got, kind)
        for reason in got:
            self.assertTrue(reason.isprintable(), "%s: %r" % (kind, reason))   # no escape, no newline
            self.assertIn("b.py", reason, kind)

    def test_every_reason_kind_that_names_a_path_is_one_clean_line(self):
        part, hook, newbin = "bin/switchboard/" + self.DIRTY, "plugins/fleet-hooks/" + self.DIRTY, "bin/" + self.DIRTY
        shadow_from_map = self.m.shadow_refusals("x = 1\n", NAME + " = 1\n", {NAME}, part)
        kinds = {
            "cannot be mapped": self.reasons([{"filename": part, "status": "changed"}]),
            "machinery": self.reasons([{"filename": hook, "status": "modified"}]),
            "new-file": self.reasons([{"filename": newbin, "status": "added"}]),
            "shadow": self.reasons([{"filename": part, "status": "modified"}], {part: shadow_from_map}),
            "closure (a reason handed in raw)": self.reasons([{"filename": part, "status": "modified"}], {part: ["it changes f in %s, part of the clear" % part]}),
        }
        for kind, got in kinds.items():
            with self.subTest(kind=kind):
                self.assertOneCleanLine(got, kind)

    def test_the_unread_reason_of_delegated_clear_map_is_clean_and_passes_through_refusals(self):
        part = "bin/switchboard/" + self.DIRTY
        item = {"filename": part, "sha": "BLOBHEAD", "patch": "x"}
        with mock.patch.object(self.hold, "_gh_json", side_effect=RuntimeError("unreadable")):
            unread = self.hold.delegated_clear_map(None, None, "o/r", item, "BASE", RULE)
        self.assertTrue(unread and all(r.isprintable() for r in unread), unread)
        self.assertOneCleanLine(self.reasons([{"filename": part, "status": "modified"}], {part: unread}), "unread")

    def test_a_mutant_that_cleans_nothing_fails_the_row(self):
        source = (REPO / "bin" / "delegated_clear.py").read_text()
        old = 'return "".join(c if c.isprintable() else "?" for c in text) if isinstance(text, str) else text'
        self.assertIn(old, source)
        spec = importlib.util.spec_from_loader("delegated_clear_dirty", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__file__"] = str(REPO / "bin" / "delegated_clear.py")
        exec(compile(source.replace(old, "return text", 1), "delegated_clear_dirty", "exec"), module.__dict__)
        report = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "review_action": "go", "review_class": None, "registrar_reasons": [], "changed_paths": []}
        got = module.refusals(RULE, report, [{"filename": "bin/switchboard/" + self.DIRTY, "status": "changed"}], impact_floor.matches, {})
        self.assertFalse(all(r.isprintable() for r in got), "without the cleaning the escape reaches the reason")


if __name__ == "__main__":
    unittest.main()

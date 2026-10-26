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
import sys
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


class TestShadowStatuses(unittest.TestCase):
    """The real `refusals` (the clear's one rule), fed as `delegated_clear_inputs` feeds it: a map only for a file whose status is `modified`. Any other status of a part or of the entry is unmapped and REFUSED
    ('cannot be mapped'), never skipped: a listed part, an unlisted part and the entry alike."""
    FILES = {"listed": "bin/switchboard/audit.py", "unlisted": "bin/switchboard/judge.py", "entry": RULE["daemon_file"]}
    STATUSES = ("changed", "unchanged", None, "renamed", "copied", "removed", "added", "modified")
    REPORT = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "review_action": "go", "review_class": None, "changed_paths": []}

    def setUp(self):
        self.m = load("delegated_clear_statuses", REPO / "bin" / "delegated_clear.py")

    def refused(self, module, path, status, closure_map):
        item = {"filename": path}
        if status is not None:
            item["status"] = status
        report = dict(self.REPORT, changed_paths=[path])
        closure = {path: closure_map} if status == "modified" else {}   # what delegated_clear_inputs maps
        return module.refusals(RULE, report, [item], impact_floor.matches, closure)

    def test_every_status_of_a_listed_part_an_unlisted_part_and_the_entry(self):
        self.assertFalse(impact_floor.matches(self.FILES["unlisted"], RULE["closure_files"]))
        for kind, path in self.FILES.items():
            for status in self.STATUSES:
                with self.subTest(file=kind, status=status):
                    got = self.refused(self.m, path, status, [])
                    if status == "modified":
                        self.assertEqual(got, [], "a mapped file with no reason is judged normally and passes this rule")
                        self.assertTrue(self.refused(self.m, path, status, ["it adds boss_name in %s, a name of the clear's closure: refused" % path]), "and a mapped reason is carried")
                    else:
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
                self.assertEqual(self.refused(module, self.FILES["unlisted"], status, []), [], "the old skip passes it")
                self.assertTrue(self.refused(self.m, self.FILES["unlisted"], status, []), "the rule refuses it")

    def test_a_control_character_in_a_path_is_not_carried_into_the_reason(self):
        got = self.m.shadow_refusals("x = 1\n", "boss_name = 1\n", {"boss_name"}, "bin/switchboard/a\x1b[31m\nb.py")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].isprintable(), repr(got[0]))
        self.assertIn("boss_name", got[0])

    def test_the_part_glob_matches_a_dotfile_and_not_a_subdirectory(self):   # stated: the parts directory is flat (derive-closure fails closed on any other file there)
        self.assertTrue(impact_floor.matches("bin/switchboard/.x.py", ["bin/switchboard/*.py"]))
        self.assertFalse(impact_floor.matches("bin/switchboard/sub/x.py", ["bin/switchboard/*.py"]))


if __name__ == "__main__":
    unittest.main()

"""Test classes moved out of bin/test-switchboard (text unchanged): cases_10."""
import argparse
import ast
import traceback
import base64
import calendar
import collections
import contextlib
import errno
import copy
import datetime as _dt
import fcntl
import gzip
import fnmatch
import hashlib
import hmac
import importlib.machinery
import importlib.util
import inspect
import http.client
import io
import itertools
import select
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import shlex
import shutil
import textwrap
import zipfile
import signal
import socket
import stat
import tarfile
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
import uuid
import urllib.parse
from unittest import mock
from support.core import Base, SAFE_PATH
from support.fake_jev import GIT_SHIM, SHIM
from support.installs_and_keys import GOOD_DONE_WHEN, GOOD_PLAN
from support.loader import SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, sb, switchboard_source
from support.paths import BIN, REPO


class TestWhoCanClear(unittest.TestCase):
    """#946: `who_can_clear`, the ONE answer to "who can clear this held pull request", over every outcome of the clear's own rule. Pure: plain dicts in, a plain dict out. Every set (the machinery globs, the
    new-file globs, the closure files, the floor classes, the never-delegated classes) is read from the matrix file, never listed here; each member is a subTest, and each has a mutant (the member removed from the rule) that must change its row."""

    H = "a" * 40
    # the refusal strings of the clear for 9 rows, stored from the code as it was before #946 moved the rule into `refusal_items` (each row: [no max_level, a max_level whose score says High])
    STORED_REFUSALS = json.loads("""{
 "closure": [
  [
   "it changes build_parser, part of the clear (head file)"
  ],
  [
   "it changes build_parser, part of the clear (head file)",
   "the score is High: High is the owner's"
  ]
 ],
 "floor-authority": [
  [],
  [
   "the score is High: High is the owner's"
  ]
 ],
 "floor-credentials": [
  [
   "it changes a credentials path (cloud/launcher/token_mint.py): that stays the owner's own clear"
  ],
  [
   "the score is High: High is the owner's"
  ]
 ],
 "floor-identity": [
  [
   "it changes a identity path (bin/switchboard-trial): that stays the owner's own clear"
  ],
  [
   "the score is High: High is the owner's"
  ]
 ],
 "machinery": [
  [
   "it changes a file that is the clear or hold machinery or that the daemon loads (bin/impact_hold.py): that stays the owner's own clear"
  ],
  [
   "it changes a file that is the clear or hold machinery or that the daemon loads (bin/impact_hold.py): that stays the owner's own clear",
   "the score is High: High is the owner's"
  ]
 ],
 "new-file": [
  [
   "it adds, deletes or renames a file under a directory the daemon loads at start (bin/new_tool.py): that stays the owner's own clear"
  ],
  [
   "it adds, deletes or renames a file under a directory the daemon loads at start (bin/new_tool.py): that stays the owner's own clear",
   "the score is High: High is the owner's"
  ]
 ],
 "no-hold": [
  [],
  [
   "the score is High: High is the owner's"
  ]
 ],
 "plain": [
  [],
  [
   "the score is High: High is the owner's"
  ]
 ],
 "unreadable": [
  [
   "the changed files cannot be read, so a change to the clear code cannot be ruled out"
  ],
  [
   "the score is High: High is the owner's",
   "the changed files cannot be read, so a change to the clear code cannot be ruled out"
  ]
 ]
}""")

    def setUp(self):
        self.floor = sb.load_module("impact_floor")
        self.module = sb.load_module("delegated_clear")
        self.matrix = self.floor.load_matrix()
        self.rule = self.matrix["owner_hold"]["delegated_clear"]
        self.block = self.rule["score"]
        self.on = {"threshold": "Serious"}

    def report(self, decision="hold", hits=None, paths=()):
        return {"pr": "acme/app#9", "head": self.H, "owner_decision": decision, "complete": True, "unknown": None, "renamed_no_source": [], "top_tier_hits": hits or {}, "changed_paths": list(paths),
                "review_action": "none", "review_class": None, "registrar_reasons": []}   # the lint's answer the real report always carries (#964)

    @staticmethod
    def file(name, status="modified", previous=None):
        return dict({"filename": name, "status": status, "sha": "e" * 40, "patch": "@@ -1 +1 @@\n-a\n+b"}, **({"previous_filename": previous} if previous else {}))

    @staticmethod
    def concrete(glob):
        return glob[:-3] + "/probe.py" if glob.endswith("/**") else glob.replace("**", "probe.py").replace("*", "probe")

    def who(self, report, files, closure=None, delegation="on", rule=None, block="shipped"):
        delegation = self.on if delegation == "on" else delegation
        return self.module.who_can_clear(rule or self.rule, report, files, self.floor.matches, closure or {}, self.block if block == "shipped" else block, delegation, pr="acme/app#9")

    def expect(self, **over):
        base = {"pr": "acme/app#9", "head": self.H, "hold": True, "floor_ids": [], "owner_only": [], "min_rating": None, "must_name": [], "who": "owner or chief of staff", "unreadable": None,
                "delegation": "on", "threshold": "Serious", "needs": []}
        return dict(base, **over)

    def rows(self):
        """The 9 rows of S2: name, report, files, closure, expected."""
        credentials = next(p for p in self.matrix["owner_hold"]["top_tier_paths"]["credentials"] if not self.floor.matches(p, self.rule["clear_code_paths"]) and "*" not in p)
        identity = next(p for p in self.matrix["owner_hold"]["top_tier_paths"]["identity"] if not self.floor.matches(p, self.rule["clear_code_paths"]) and "*" not in p)
        floor_rows = []
        for cls, path in (("credentials", credentials), ("identity", identity), ("authority", "migrations/0002.sql")):
            rid = next(r for r, c in self.block["floor"]["rules"].items() if c == cls)
            floor_rows.append(("floor-" + cls, self.report(hits={cls: [path]}, paths=[path]), [self.file(path)], {},
                               self.expect(floor_ids=[rid], must_name=[rid], min_rating=self.block["floor"]["lowest_level"], who="chief of staff with a rating")))
        return [
            ("closure", self.report(paths=["bin/fleet-switchboard"]), [self.file("bin/fleet-switchboard")], {"bin/fleet-switchboard": ["it changes build_parser, part of the clear (head file)"]},
             self.expect(who="owner only", owner_only=[{"kind": "closure", "why": "changes a definition reachable from the clear (bin/fleet-switchboard)"}])),
            ("new-file", self.report(paths=["bin/new_tool.py"]), [self.file("bin/new_tool.py", "added")], {},
             self.expect(who="owner only", owner_only=[{"kind": "new-file", "why": "adds a file under a directory the daemon loads at start (bin/new_tool.py)"}])),
            ("machinery", self.report(paths=["bin/impact_hold.py"]), [self.file("bin/impact_hold.py")], {},
             self.expect(who="owner only", owner_only=[{"kind": "machinery", "why": "changes the clear or hold machinery (bin/impact_hold.py)"}])),
        ] + floor_rows + [
            ("no-hold", self.report(decision="none", paths=["docs/x.md"]), [self.file("docs/x.md")], {}, self.expect(hold=False, who="no hold")),
            ("unreadable", self.report(paths=["src/app.py"]), None, {},
             self.expect(who="unknown", unreadable=self.module.UNREAD, owner_only=[{"kind": "unreadable", "why": "the changed files cannot be read"}])),
            ("plain", self.report(paths=["src/app.py"]), [self.file("src/app.py")], {}, self.expect()),
        ]

    def test_s2_the_table_has_9_rows_and_every_row_returns_its_exact_structure(self):
        rows = self.rows()
        self.assertEqual(len(rows), 9)
        for name, report, files, closure, expected in rows:
            with self.subTest(row=name):
                self.assertEqual(self.who(report, files, closure), expected)
        self.assertEqual([r[4]["floor_ids"] for r in rows if r[0].startswith("floor-")], [["floor-credentials"], ["floor-identity"], ["floor-authority"]])
        self.assertEqual({k for r in rows for o in r[4]["owner_only"] for k in [o["kind"]]}, {"closure", "new-file", "machinery", "unreadable"})

    def test_s2_a_build_that_answers_the_same_for_every_pull_request_fails_the_rows(self):
        same = self.who(self.report(paths=["src/app.py"]), [self.file("src/app.py")])
        failed = [name for name, report, files, closure, expected in self.rows() if same != expected]
        self.assertEqual(sorted(failed), sorted(n for n, *_ in self.rows() if n != "plain"))

    def test_every_who_value_is_reachable_and_the_values_are_one_table(self):
        seen = {r[4]["who"] for r in self.rows()}
        seen.add(self.who(self.report(paths=["src/app.py"]), [self.file("src/app.py")], delegation=None)["who"])
        self.assertEqual(seen, set(self.module.WHO))
        self.assertEqual(self.who(self.report(paths=["src/app.py"]), [self.file("src/app.py")], delegation=None)["delegation"], "off")

    def test_every_kind_the_rule_can_refuse_for_is_in_the_one_table(self):
        import ast
        tree = ast.parse(Path(self.module.__file__).read_text())
        kinds = {c.args[0].value for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_item" and c.args and isinstance(c.args[0], ast.Constant)}
        self.assertEqual(kinds, set(self.module._OWNER_WORDS) | {"unreadable", "rating", "review"}, "a kind the rule can give that the line has no words for (or the reverse)")

    def test_every_machinery_path_is_owner_only_with_its_own_reason_and_removing_it_from_the_rule_changes_the_row(self):
        for glob in self.rule["clear_code_paths"]:
            with self.subTest(glob=glob):
                path = self.concrete(glob)
                self.assertTrue(self.floor.matches(path, [glob]))
                got = self.who(self.report(paths=[path]), [self.file(path)])
                self.assertEqual((got["who"], [o["kind"] for o in got["owner_only"]]), ("owner only", ["machinery"]))
                self.assertIn("machinery", got["owner_only"][0]["why"])
                mutant = dict(self.rule, clear_code_paths=[g for g in self.rule["clear_code_paths"] if g != glob])
                if not self.floor.matches(path, mutant["clear_code_paths"]) and not self.floor.matches(path, self.rule["new_file_globs"]):
                    self.assertNotEqual(self.who(self.report(paths=[path]), [self.file(path)], rule=mutant)["who"], "owner only")

    def test_every_new_file_glob_and_every_status_is_owner_only_and_a_plain_edit_is_not(self):
        verbs = {"added": "adds a file", "removed": "deletes a file", "renamed": "renames a file"}
        for glob in self.rule["new_file_globs"]:
            path = self.concrete(glob)
            for status, verb in verbs.items():
                with self.subTest(glob=glob, status=status):
                    got = self.who(self.report(paths=[path]), [self.file(path, status)])
                    self.assertEqual([(o["kind"], verb in o["why"]) for o in got["owner_only"]], [("new-file", True)])
                    mutant = dict(self.rule, new_file_globs=[g for g in self.rule["new_file_globs"] if g != glob])
                    self.assertEqual(self.who(self.report(paths=[path]), [self.file(path, status)], rule=mutant)["owner_only"], [])
            self.assertEqual(self.who(self.report(paths=[path]), [self.file(path, "modified")])["owner_only"], [], "an edit of an existing file is not a new file")

    def test_every_closure_file_is_owner_only_for_a_mapped_change_unreadable_when_unmapped_and_free_when_nothing_is_reached(self):
        for path in self.rule["closure_files"]:
            if self.floor.matches(path, self.rule["clear_code_paths"]):
                continue
            with self.subTest(path=path):
                report, files = self.report(paths=[path]), [self.file(path)]
                self.assertEqual([o["kind"] for o in self.who(report, files, {path: ["it changes f, part of the clear (head file)"]})["owner_only"]], ["closure"])
                self.assertEqual(self.who(report, files, {path: ["it changes f"]})["owner_only"][0]["why"], "changes a definition reachable from the clear (%s)" % path)
                self.assertEqual(self.who(report, files, {path: None})["who"], "unknown")
                self.assertEqual(self.who(report, files, {path: []})["who"], "owner or chief of staff")
                mutant = dict(self.rule, closure_files=[g for g in self.rule["closure_files"] if g != path])
                if self.floor.matches(path, ["bin/switchboard/*.py"]):   # #1339: a part of the daemon is judged whether the matrix lists it or not (an unmapped one is refused)
                    self.assertEqual(self.who(report, files, {path: None}, rule=mutant)["who"], "unknown", "a part is judged even when the rule does not list it")
                else:
                    self.assertEqual(self.who(report, files, {path: None}, rule=mutant)["who"], "owner or chief of staff", "a path the rule does not judge is not mapped")

    def test_every_floor_class_gives_its_rule_id_and_the_lowest_rating_and_the_never_delegated_ones_are_owner_only_without_a_max_level(self):
        for rid, cls in self.block["floor"]["rules"].items():
            with self.subTest(rule=rid):
                report, files = self.report(hits={cls: ["x/p.py"]}, paths=["x/p.py"]), [self.file("x/p.py")]
                got = self.who(report, files)
                self.assertEqual((got["floor_ids"], got["must_name"], got["min_rating"], got["who"]), ([rid], [rid], self.block["floor"]["lowest_level"], "chief of staff with a rating"))
                bare = self.who(report, files, delegation={"threshold": None})
                if cls in self.rule["never_path_classes"]:
                    self.assertEqual(([o["kind"] for o in bare["owner_only"]], bare["who"]), (["class"], "owner only"))
                else:
                    self.assertEqual((bare["owner_only"], bare["who"]), ([], "owner or chief of staff"))
                self.assertEqual(self.who(report, files, delegation=None)["who"], "owner (delegation is off)" if cls not in self.rule["never_path_classes"] else "owner only")
                mutant = {"floor": {"lowest_level": "High", "rules": {k: v for k, v in self.block["floor"]["rules"].items() if k != rid}}}
                self.assertEqual(self.who(report, files, block=mutant)["floor_ids"], [], "the rule id comes from the matrix block")
        for cls in self.rule["never_path_classes"]:
            self.assertIn(cls, self.block["floor"]["rules"].values(), "every never-delegated class has a floor rule id")

    def test_the_hold_decisions_and_unreadable_inputs(self):
        for decision in ("none", "second-review"):
            self.assertEqual(self.who(self.report(decision=decision), [self.file("a")])["who"], "no hold")
        for bad in (None, {}, dict(self.report(), owner_decision="maybe")):
            self.assertEqual(self.module.who_can_clear(self.rule, bad, [], self.floor.matches, {}, self.block, self.on)["who"], "unknown")
        self.assertEqual(self.module.who_can_clear(None, None, None, None, None, self.block, self.on, pr="acme/app#9", unread="why")["unreadable"], self.module.UNREAD)
        cut = dict(self.report(), complete=False)
        self.assertEqual(self.who(cut, [self.file("a")])["who"], "unknown")
        self.assertEqual(self.who(self.report(), [self.file("a")], block=None)["who"], "unknown", "no score rule: never a confident answer")

    def test_u1_the_refusal_strings_of_the_nine_rows_are_byte_identical_to_the_stored_fixture(self):
        for name, report, files, closure, _ in self.rows():
            with self.subTest(row=name):
                got = [self.module.refusals(self.rule, report, files, self.floor.matches, closure, None),
                       self.module.refusals(self.rule, report, files, self.floor.matches, closure, {"level": "High", "reasons": ["the score is High: High is the owner's"]})]
                self.assertEqual(got, self.STORED_REFUSALS[name])

    def test_s4_the_line_names_the_level_the_matrix_gives(self):
        row = [r for r in self.rows() if r[0] == "floor-authority"][0]
        shipped = self.module.clear_line(self.who(row[1], row[2]))
        self.assertIn("at least Serious", shipped)
        medium = json.loads(json.dumps(self.block))
        medium["floor"]["lowest_level"] = "Medium"
        line = self.module.clear_line(self.who(row[1], row[2], block=medium))
        self.assertIn("at least Medium", line)
        self.assertNotIn("Serious", line.replace(" on an independent rating up to Serious", ""))
        self.assertIn("floor-authority", line)

    def test_s12_an_owner_only_line_starts_with_owner_only_and_gives_its_reason_in_words(self):
        want = {"new-file": "adds a file", "machinery": "machinery", "closure": "reachable from the clear"}
        for name, report, files, closure, _ in self.rows():
            if name in want:
                with self.subTest(row=name):
                    line = self.module.clear_line(self.who(report, files, closure))
                    self.assertTrue(line.startswith("Who can clear this: owner only: "), line)
                    self.assertIn(want[name], line)

    def test_s13_the_documented_samples_are_the_rendered_lines(self):
        text = (BIN.parent / "docs" / "switchboard.md").read_text()
        rows = {r[0]: r for r in self.rows()}
        for name in ("new-file", "floor-authority"):
            with self.subTest(sample=name):
                _, report, files, closure, _ = rows[name]
                line = self.module.clear_line(self.who(report, files, closure))
                self.assertIn("\n" + line + "\n", text, "the documentation's sample is the line the code renders")

    def test_s9_no_planted_token_and_no_home_path_reaches_the_line_and_a_reason_is_never_printed(self):
        token = "gh" + "p_" + "A1b2C3d4" * 5
        home = "/".join(["", "Us" + "ers", "alex", ".config", "fleet"])
        for planted in (token, home, "x/" + token + ".py", "../../" + home.lstrip("/"), "bin/" + home):
            with self.subTest(path=planted[:12]):
                paths = [".github/workflows/" + planted.lstrip("/")]
                got = self.who(self.report(paths=paths), [self.file(paths[0])], {})
                line = self.module.clear_line(got)
                self.assertNotIn(token, json.dumps(got) + line)
                self.assertNotIn("alex", json.dumps(got) + line)
        closure = {"bin/fleet-switchboard": ["it changes %s at %s" % (token, home)]}
        got = self.who(self.report(paths=["bin/fleet-switchboard"]), [self.file("bin/fleet-switchboard")], closure)
        self.assertNotIn(token, json.dumps(got) + self.module.clear_line(got))
        self.assertNotIn("alex", json.dumps(got) + self.module.clear_line(got))
        self.assertEqual(self.module.safe_path("bin/ok_name.py"), "bin/ok_name.py")

    def test_u4_every_token_of_the_line_over_the_rows_is_a_word_of_the_code_a_rule_id_a_level_or_a_changed_path(self):
        import ast
        tree = ast.parse(Path(self.module.__file__).read_text())
        fixed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("clear_line", "_why") or isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("_OWNER_WORDS", "UNREAD") for t in node.targets):
                fixed |= {w for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str) for w in re.findall(r"[A-Za-z0-9_./-]+", c.value)}
        allowed = fixed | set(self.block["floor"]["rules"]) | set(self.block["ladder"]) | set(self.matrix["owner_hold"]["top_tier_classes"]) | {w for r in self.rows() for p in r[1]["changed_paths"] for w in re.findall(r"[A-Za-z0-9_./-]+", p)}
        def outside(line):
            return sorted(set(re.findall(r"[A-Za-z0-9_./-]+", line)) - allowed)
        seen = 0
        for name, report, files, closure, _ in self.rows():
            for delegation in ("on", {"threshold": None}, None):
                line = self.module.clear_line(self.who(report, files, closure, delegation=delegation))
                if line is not None:
                    seen += 1
                    self.assertEqual(outside(line), [], "%s: %s" % (name, line))
        self.assertGreater(seen, 20)
        self.assertTrue(outside(line + " " + "/".join(["", "Us" + "ers", "x", ".local", "state", "fleet", "audit.log"])), "the check sees a path that is not allowed")

    def test_u5_who_can_clear_is_defined_once_and_the_daemons_line_calls_it_and_it_returns_plain_data(self):
        import ast
        count = 0
        for path in sorted(BIN.parent.rglob("*")):
            if path.is_file() and not path.name.startswith("test-") and path.suffix in ("", ".py") and ".git" not in path.parts and path.stat().st_size < 3_000_000:
                try:
                    count += len(re.findall(r"^\s*def who_can_clear\(", path.read_text(), re.M))
                except UnicodeDecodeError:
                    continue
        self.assertEqual(count, 1)
        self.assertTrue(callable(self.module.who_can_clear))
        daemon, hold = ast.parse(switchboard_source()), ast.parse((BIN / "impact_hold.py").read_text())
        calls = lambda tree, fn: {getattr(c.func, "attr", getattr(c.func, "id", "")) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == fn for c in ast.walk(f) if isinstance(c, ast.Call)}
        self.assertIn("clear_who", calls(daemon, "clear_line_post"))
        self.assertIn("who_can_clear", calls(hold, "clear_who"))
        got = self.who(self.report(paths=["src/app.py"]), [self.file("src/app.py")])
        self.assertEqual(json.loads(json.dumps(got)), got, "a plain structure: it survives a JSON round trip unchanged")

    def test_the_delegation_is_read_from_the_configuration_the_clear_reads(self):
        bound = lambda n, **extra: dict({"number": n, "sha256": "0" * 64}, **extra)
        on = lambda *orders: {"delegated_clear": {"orders": list(orders), "charter": 40}}
        self.assertIsNone(self.module.delegation_of({}, self.block))
        self.assertIsNone(self.module.delegation_of({"delegated_clear": {"orders": [], "charter": 40}}, self.block))
        self.assertEqual(self.module.delegation_of(on(bound(1)), self.block), {"threshold": None})
        self.assertEqual(self.module.delegation_of(on(bound(1, max_level="Medium"), bound(2, max_level="Serious"), bound(3)), self.block), {"threshold": "Serious"})

    def floor_row(self, cls="credentials"):
        path = next(p for p in self.matrix["owner_hold"]["top_tier_paths"][cls] if not self.floor.matches(p, self.rule["clear_code_paths"]) and "*" not in p)
        return self.report(hits={cls: [path]}, paths=[path]), [self.file(path)]

    def test_a_floor_above_every_order_is_owner_only_and_the_clear_refuses_every_rating_the_same_way(self):
        report, files = self.floor_row()
        floor = self.block["floor"]["lowest_level"]
        ladder = self.block["ladder"]
        for threshold in [l for l in self.block["delegable_levels"] if ladder.index(l) < ladder.index(floor)]:
            with self.subTest(threshold=threshold):
                got = self.who(report, files, delegation={"threshold": threshold})
                self.assertEqual((got["who"], [o["kind"] for o in got["owner_only"]]), ("owner only", ["score"]))
                line = self.module.clear_line(got)
                self.assertIn("owner only: the rating the floor needs (%s) is above what any order allows (%s)" % (floor, threshold), line)
                for level in ladder:   # the clear itself: no rating at the floor or above passes
                    self.assertTrue(self.module.level_refusal(self.block, level, threshold) or ladder.index(level) <= ladder.index(threshold))
                self.assertTrue(self.module.level_refusal(self.block, floor, threshold), "the same comparison the clear makes")
        self.assertEqual(self.who(report, files, delegation={"threshold": floor})["who"], "chief of staff with a rating")

    def test_a_mutant_that_leaves_the_floor_comparison_out_says_the_chief_of_staff_can(self):
        report, files = self.floor_row()
        source = Path(self.module.__file__).read_text()
        old = 'and level_refusal(block, out["min_rating"], out["threshold"]):'
        self.assertIn(old, source)
        scope = {"__name__": "mutant_delegated_clear", "__file__": self.module.__file__}
        exec(compile(source.replace(old, "and False:"), "mutant", "exec"), scope)
        medium = {"threshold": "Medium"}
        got = scope["who_can_clear"](self.rule, report, files, self.floor.matches, {}, self.block, medium, pr="acme/app#9")
        self.assertEqual(got["who"], "chief of staff with a rating", "the mutant says what the clear would refuse for every rating")
        self.assertEqual(self.who(report, files, delegation=medium)["who"], "owner only")

    def test_every_refusal_of_judge_is_a_row_the_line_knows_and_a_new_one_fails_here(self):
        import ast
        tree = ast.parse(Path(self.module.__file__).read_text())
        judge = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "judge")
        sites = [n for n in ast.walk(judge) if (isinstance(n, ast.Return) and isinstance(n.value, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "reasons" for k in n.value.keys)
                 and not (isinstance(n.value.values[-1], ast.List) and not n.value.values[-1].elts) and not (isinstance(n.value.values[-1], ast.Name)))
                 or (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "append" and getattr(n.func.value, "id", "") == "reasons")
                 or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "reasons" for t in n.targets))
]
        # each refusal of judge and what the line says of it: "static" is known from the pull request and the orders alone (the line says owner only), "rating" depends on a rating not yet made (the line says what the rating must be)
        known = {"the score rule or the rating is unknown": "unreadable (who: unknown)", "the threshold is unknown": "unreachable: the configuration refuses a max_level that is not delegable",
                 "a rating has no severity and probability the matrix knows": "rating", "no agent block": "rating", "incomplete verdict": "rating", "no independent rating": "rating",
                 "a higher uncounted rating": "rating", "floor below": "rating (the line says the least rating)", "rule not named": "rating (the line says which ids)", "append(over), the answer of level_refusal": "static (owner only, kind score)",
                 "#972 too few counted ratings": "rating", "#972 too few rater sessions": "rating", "#972 too few independent runs": "rating"}
        self.assertEqual(len(sites), len(known), "judge has a refusal this table does not: give it a row (and a line) before it ships")

    def test_s1_the_documented_cause_paragraph_names_every_recorded_refusal_kind_with_its_count_and_says_the_sample_is_thin(self):
        text = (BIN.parent / "docs" / "switchboard.md").read_text()
        start = text.index("#### Why a clear was refused (#946)")
        section = text[start:text.index("\n#", start + 10)]
        recorded = [("closure rule", 3), ("new-file rule", 1), ("credentials path, rating Medium below the floor Serious", 1), ("did not name the floor rule id", 1),
                    ("a hold label a person added", 2), ("not the Chief of Staff's own session", 1)]
        found = [name for name, count in recorded if any(name in row and re.search(r"\|\s*%d\s*\|?\s*$" % count, row) for row in section.split("\n"))]
        self.assertEqual(len(found), 6, "6 of 6 refusal kinds are named with their recorded count; found %s" % found)
        self.assertIn("thin sample", section)


class TestRecordJsonReaders(unittest.TestCase):
    """#1314: every reader of untrusted record JSON uses the shared bounded-depth parse (bin/record_json.py); the readers are DERIVED from the source."""
    BIN = REPO / "bin"
    # readers of record text and the shared helpers: each must call parse_bounded (the helpers ARE the parse)
    READERS = {"agent_presence.py:read_record": "agent_presence", "switchboard/reminders.py:read_reminders": "reminders",
               "switchboard/reminders.py:read_record_file": "reminders", "registry.py:_parse": "registry", "registry.py:_old_retired": "registry"}
    HELPERS = {"record_json.py:parse_bounded", "cloud_worker.py:parse_bounded"}
    # every other json.loads / json.load site in bin/: daemon-written state, command output, a socket body or a line of a log, with a reason
    EXEMPT_REASON = "not a record file another process writes: daemon-written state, command output, a request body or a log line"
    # path_suites.py:check_manual and load_manual read the repository's OWN checked-in manual (tools/path-suites.json), a trusted repo file, not untrusted record text
    EXEMPT = """
agent_calls.py:_read_json
agent_calls.py:_spool_lines
agent_calls.py:parse_usage
chat_clear.py:_load
chat_clear.py:read_seen
ci_runs.py:_read_run
cloud_channel_server.py:do_POST
cloud_cli.py:post
cloud_cost_report.py:read_lines
cloud_hub.py:_read_json
cloud_hub.py:_read_json_aged
cloud_hub.py:kube_http
cloud_relay.py:loads_strict
cloud_relay_daemon.py:_read
cloud_way_back_check.py:main
cloud_worker.py:run_launcher
cpu_guard.py:load_episode
decision_sync.py:_read_json
decisions_pull.py:held_body
decisions_pull.py:parse
delegated_clear.py:score_block
disk_janitor.py:_without_own_events
disk_janitor.py:read_cursor
external_adapter.py:attempt
external_adapter.py:call_tool
external_adapter.py:classify
external_adapter.py:load
external_adapter.py:parse_key_source
external_adapter.py:rpc
external_adapter.py:sse_messages
fleet-heartbeat:call
fleet-heartbeat:load_config
fleet-scenario:read
fleet-switchboard:__init__
fleet-switchboard:_api
fleet-switchboard:_audit_tail
fleet-switchboard:_call
fleet-switchboard:_docker_inspect
fleet-switchboard:_fresh
fleet-switchboard:_last
fleet-switchboard:_load_markers
fleet-switchboard:_read_json_object
fleet-switchboard:_review_api
fleet-switchboard:_write_gates_config
fleet-switchboard:anonymous_volumes
fleet-switchboard:catching_up_lines
fleet-switchboard:charter_pane_update
fleet-switchboard:ci_record_status_line
fleet-switchboard:cmd_review_measure
fleet-switchboard:daemon_record_of
fleet-switchboard:daemon_snapshot
fleet-switchboard:gates_state
fleet-switchboard:get
fleet-switchboard:github_calls_status
fleet-switchboard:github_status
fleet-switchboard:handle
fleet-switchboard:health_watched_repos
fleet-switchboard:janitor_agent_roots
fleet-switchboard:job_read
fleet-switchboard:json
fleet-switchboard:lab_facts
fleet-switchboard:launch_memory_check
fleet-switchboard:load_config
fleet-switchboard:pane_exit_audit_tail
fleet-switchboard:pr_state_read
fleet-switchboard:pull_state
fleet-switchboard:read_decisions_file
fleet-switchboard:read_friction_state
fleet-switchboard:read_janitor_record
fleet-switchboard:read_open_items
fleet-switchboard:read_open_overlap
fleet-switchboard:read_pane_health
fleet-switchboard:read_pr_json
fleet-switchboard:read_pr_notices
fleet-switchboard:read_ref_state
fleet-switchboard:read_report_riders
fleet-switchboard:receive
fleet-switchboard:stopped_news_summary
fleet-switchboard:sweep_summary
fleet-switchboard:unix_socket_call
fleet-switchboard:update_daemon_record
fleet-switchboard:v2_calls_status
fleet-switchboard:v2_link_status_line
fleet_health.py:_audit_events
fleet_health.py:_read_state
fleet_health.py:audit_moves
fleet_health.py:decision_items
fleet_health.py:held_prs
fleet_health.py:mark_stale
fleet_health.py:read_moves
fleet_health.py:stale_since
fleet_health.py:tick
health_records.py:read_tool_file
heartbeat-ack:atomic_write
heartbeat-ack:read_state_document
heavy_slots.py:_read
heavy_slots.py:_read_info
heavy_slots.py:_reported
impact_floor.py:load_matrix
impact_hold.py:_gh_json
impact_hold.py:_pending_read
impact_hold.py:read_impact_state
judge-eval:cmd_run
judge-eval:cmd_sweep
judge-eval:read_jsonl
judge-sample:read_verdicts
merge_gate.py:pass_and_merge
merge_gate.py:read_json
path_suites.py:check_manual
path_suites.py:load_manual
plan_ledger.py:_read_head
plan_ledger.py:cos_rules
plan_ledger.py:read_ledger
pr_reading.py:_json
proof-switchboard:api
proof-switchboard:audit
proof-switchboard:cmd_lab_status
proof-switchboard:cmd_session
proof-switchboard:do_POST
proof-switchboard:herdr
proof-switchboard:launch
proof-switchboard:model_requests
proof-switchboard:new_lab_pane
proof-switchboard:pane_text
proof-switchboard:parse_script
proof-switchboard:run_pass
proof-switchboard:settings
proof-switchboard:shim_log
queue_ejections.py:read_failure
reassign.py:read_overrides
reassign.py:read_overrides_strict
reds.py:get
reds.py:parse_artifact
registry.py:_small
registry.py:fold_old
repo_exclusion.py:_commands
repo_exclusion.py:az_without_subscription
review_checkout.py:remove
spotlight_guard.py:load
switchboard-trial:latest_session
switchboard-trial:launch
switchboard-trial:launched
switchboard-trial:observation
switchboard-trial:resume
switchboard-trial:settings
switchboard/agents_verify.py:agent_definition
switchboard/agents_verify.py:agents_found
switchboard/alert_labels.py:__init__
switchboard/answer_delivery.py:cmd_decision_batch
switchboard/decisions_a.py:read_batch_notes
switchboard/decisions_a.py:read_escalated
switchboard/decisions_b.py:read_order_notices
switchboard/decisions_b.py:sync_for
switchboard/engine.py:load
switchboard/facts.py:_load
switchboard/handoff.py:post_ask_comment
switchboard/import_v1.py:read_v1_export
switchboard/jev.py:_load
switchboard/jev.py:_post
switchboard/judge.py:ask_judge_daemon
switchboard/judge.py:looks_like_plugin_event
switchboard/judge.py:parse_plugin_event
switchboard/judge_request.py:_guess_configured
switchboard/judge_request.py:parse_policy_request
switchboard/triage.py:triage_summary
switchboard/v2_transport.py:_decode
switchboard/v2_transport.py:_read_file
triage-sample:read_findings
triage-sample:summarise
triage_digest.py:read_json
work_sync.py:_read_json
""".split()

    @staticmethod
    def scan(root):
        """{"file:function"} of every json.loads, json.load and `from json import loads` call in the python sources under root (tests excluded)."""
        import ast
        out = set()
        for f in sorted(Path(root).rglob("*")):
            if not f.is_file() or f.name.startswith(("test-", "test_")) or "__pycache__" in f.parts:
                continue
            try:
                text = f.read_text()
            except (OSError, UnicodeError):
                continue
            if not (f.suffix == ".py" or text.startswith(("#!/usr/bin/env python", "#!/usr/bin/python"))):
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            names = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "json" for a in n.names if a.name in ("loads", "load")}
            def walk(node, fn):
                for c in ast.iter_child_nodes(node):
                    nf = c.name if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                    if isinstance(c, ast.Call):
                        x = c.func
                        if (isinstance(x, ast.Attribute) and x.attr in ("loads", "load") and isinstance(x.value, ast.Name) and x.value.id in ("json", "_json")) \
                                or (isinstance(x, ast.Name) and x.id in names):
                            out.add("%s:%s" % (f.relative_to(root).as_posix(), fn or "<module>"))
                    walk(c, nf)
            walk(tree, None)
        return out

    def missing(self, root):
        known = set(self.EXEMPT) | self.HELPERS
        return sorted(self.scan(root) - known)

    def test_every_json_reader_in_bin_is_in_the_table(self):
        self.assertEqual(self.missing(self.BIN), [])

    def test_the_table_has_no_stale_entry(self):
        self.assertEqual(sorted((set(self.EXEMPT) | self.HELPERS) - self.scan(self.BIN)), [])

    def test_a_planted_third_reader_is_named(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        shutil.copytree(str(self.BIN), str(tmp / "bin"), ignore=shutil.ignore_patterns("test-*", "__pycache__"))
        (tmp / "bin" / "planted_reader.py").write_text("import json\n\ndef read_planted(path):\n    return json.loads(open(path).read())\n")
        self.assertEqual(self.missing(tmp / "bin"), ["planted_reader.py:read_planted"])

    def test_the_covered_readers_are_derived_and_call_the_shared_parse(self):
        import ast
        for key, module in self.READERS.items():
            path, fn = key.split(":")
            tree = ast.parse((self.BIN / path).read_text())
            node = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn][0]
            calls = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "") for c in ast.walk(node) if isinstance(c, ast.Call)}
            self.assertIn("parse_bounded", calls, key)
            self.assertNotIn("loads", calls, key)
        # and no raw json.loads stays in them (the scan finds none there)
        self.assertEqual(set(self.READERS) & self.scan(self.BIN), set())

    def deep(self, n):
        return "[" * n + "]" * n

    def nested(self, n):
        return '{"reminders": [], "k": %s}' % self.deep(n - 1)   # an object holding n-1 more levels: n in all

    def presence(self, state, text):
        ap = sb.load_agent_presence()
        d = Path(state) / ap.RECORD_DIR
        d.mkdir(parents=True, exist_ok=True)
        f = d / "w.json"
        f.write_text(text)
        os.chmod(str(f), 0o600)
        return ap.read_record(state, "w")

    def good_record(self, depth_extra):
        return '{"kind": "local", "worker": "w", "session": "ses_1", "x": %s}' % (self.deep(depth_extra) if depth_extra else "0")

    def reminders_read(self, state, text):
        (Path(state) / "reminders.json").write_text(text)
        try:
            return ("ok", sb.read_reminders(state))
        except sb.ReminderError as err:
            return ("refused", str(err))

    def with_limit(self, limit, fn):
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(limit)
        try:
            return fn()
        finally:
            sys.setrecursionlimit(old)

    def tmpdir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    # the record cap of agent_presence is 8192 bytes, so its deepest file is 4000 levels (the reminders reader takes 30000)
    def test_s2_deep_file_is_refused_by_load_record_without_raising(self):
        st = self.tmpdir()
        self.assertEqual(self.presence(st, self.good_record(4000)), (None, "is not readable JSON"))

    def test_s3_deep_file_is_refused_by_the_reminders_readers_without_raising(self):
        st = self.tmpdir()
        got = self.reminders_read(st, '{"reminders": %s}' % self.deep(30000))
        self.assertEqual(got[0], "refused")
        self.assertIn("nested deeper than", got[1])
        f = Path(st) / "reminder-repeats.json"
        f.write_text(self.deep(30000))
        self.assertIsNone(sb.read_record_file(f))

    def test_s4_the_refusal_is_the_same_at_a_low_and_the_default_recursion_limit(self):
        st = self.tmpdir()
        a = {self.with_limit(lim, lambda: self.presence(st, self.good_record(4000))) for lim in (200, sys.getrecursionlimit())}
        self.assertEqual(len(a), 1)
        b = {self.with_limit(lim, lambda: self.reminders_read(st, '{"reminders": %s}' % self.deep(30000))) for lim in (200, sys.getrecursionlimit())}
        self.assertEqual(len(b), 1)

    def test_s5_load_record_parses_at_the_limit_and_refuses_one_deeper(self):
        st = self.tmpdir()
        limit = sb.load_module("record_json").RECORD_MAX_DEPTH
        ok = self.presence(st, '{"kind": "local", "worker": "w", "session": "ses_1", "x": %s}' % self.deep(limit - 1))
        self.assertIsNone(ok[1])
        self.assertEqual(self.presence(st, '{"kind": "local", "worker": "w", "session": "ses_1", "x": %s}' % self.deep(limit)), (None, "is not readable JSON"))

    def test_s6_reminders_reader_parses_at_the_limit_and_refuses_one_deeper(self):
        st = self.tmpdir()
        limit = sb.load_module("record_json").RECORD_MAX_DEPTH
        self.assertEqual(self.reminders_read(st, '{"reminders": [], "x": %s}' % self.deep(limit - 1)), ("ok", []))
        self.assertEqual(self.reminders_read(st, '{"reminders": [], "x": %s}' % self.deep(limit))[0], "refused")
        f = Path(st) / "reminder-repeats.json"
        f.write_text(self.deep(limit))
        self.assertEqual(sb.read_record_file(f), json.loads(self.deep(limit)))
        f.write_text(self.deep(limit + 1))
        self.assertIsNone(sb.read_record_file(f))

    def test_the_limit_equals_the_cloud_readers_limit(self):
        self.assertEqual(sb.load_module("record_json").RECORD_MAX_DEPTH, sb.load_module("cloud_worker").WATCH_MAX_DEPTH)

    def mutate(self, rel, old, new):
        text = (self.BIN / rel).read_text()
        self.assertIn(old, text)
        return text.replace(old, new)

    def test_s9_mutants_the_scan_removed_from_each_reader_are_caught(self):
        """Run by hand too: replace parse_bounded by json.loads at the reader and the deep-file tests fail."""
        for rel, old in (("agent_presence.py", "_record_json.parse_bounded("), ("switchboard/reminders.py", 'load_module("record_json").parse_bounded('),
                         ("registry.py", "_record_json.parse_bounded(")):
            text = self.mutate(rel, old, "json.loads(")
            self.assertNotIn("parse_bounded", text.replace("def parse_bounded", "")) if False else None
            self.assertIn("json.loads(", text)
            # the derived scan sees the plain loads in the reader function and it is not in the table
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, str(tmp), True)
            shutil.copytree(str(self.BIN), str(tmp / "bin"), ignore=shutil.ignore_patterns("test-*", "__pycache__"))
            (tmp / "bin" / rel).write_text(text)
            self.assertTrue(self.missing(tmp / "bin"), rel)

    def test_docs_name_every_reader_and_the_bounded_depth_parse(self):
        docs = (REPO / "docs" / "switchboard.md").read_text()
        section = docs[docs.index("### Depth limit of untrusted record parses"):docs.index("## The assembler")]
        for name in list(self.READERS) + ["record_json.py", "RECORD_MAX_DEPTH", "bounded-depth", "TestRecordJsonReaders"]:
            self.assertIn(name.split(":")[-1], section)


class TestPlanCheckRules(unittest.TestCase):
    """#135: every validation, both ways."""

    def check(self, text=GOOD_PLAN, done_when=GOOD_DONE_WHEN):
        return sb.plan_check.check(text, done_when)

    def codes(self, text, done_when=GOOD_DONE_WHEN):
        return sorted(d["code"] for d in self.check(text, done_when)["defects"])

    def edit(self, old, new, text=GOOD_PLAN):
        self.assertIn(old, text)
        return text.replace(old, new, 1)

    def test_the_good_plan_passes_with_a_hash_and_no_defect(self):
        got = self.check()
        self.assertEqual((got["result"], got["defects"], got["scenarios"], len(got["clauses"])), ("pass", [], 3, 2))
        self.assertRegex(got["scenario_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(got["plan_hash"], r"^sha256:[0-9a-f]{64}$")

    def warn_codes(self, text):
        return [(w["id"], w["code"]) for w in self.check(text)["warnings"] if w["code"] == "live-no-claim"]

    def test_a_live_or_tester_scenario_with_no_claim_warns_and_never_fails_or_changes_the_hash(self):
        got = self.check()
        self.assertEqual(got["result"], "pass")
        self.assertEqual(self.warn_codes(GOOD_PLAN), [("S2", "live-no-claim")])
        message = [w["message"] for w in self.check(GOOD_PLAN)["warnings"] if w["code"] == "live-no-claim"][0]
        self.assertIn("`claim: epic`", message)
        self.assertIn("`claim: pr`", message)
        self.assertNotIn("is claimed by", message)
        tester = self.edit("    proof: live\n", "    proof: measure\n")
        self.assertEqual(self.warn_codes(tester), [("S2", "live-no-claim")], "evaluator tester alone warns")
        live = self.edit("    evaluator: tester", "    evaluator: owner")
        self.assertEqual(self.warn_codes(live), [("S2", "live-no-claim")], "proof live alone warns")
        claimed = self.edit("    evaluator: tester", "    evaluator: tester\n    claim: epic")
        self.assertEqual(self.warn_codes(claimed), [])
        self.assertEqual(self.check(claimed)["result"], "pass")
        self.assertEqual(self.warn_codes(self.edit("    evaluator: tester", "    evaluator: tester\n    claim: pr")), [])
        same = self.check(GOOD_PLAN)["plan_hash"]
        self.assertEqual(same, got["plan_hash"])
        self.assertEqual(same, "sha256:2ad49344a1bf3ea7fc5ceeef6f60013e02fa465dc04da8a3b5b389a82b686cdb", "the hash the checker gave this plan before the warning existed")
        self.assertEqual([w["code"] for w in got["warnings"] if w["code"] != "live-no-claim"], ["no-constraint-list"])

    def test_the_template_marks_every_stub_with_a_ready_claim_line_and_a_live_one_for_live_or_tester(self):
        lines = sb.plan_check.template(["one thing", "another"])
        want = "    # claim: epic   # " + sb.plan_check.CLAIM_WHY
        self.assertEqual(lines.count(want), 3, "one per stub: two clauses and the unchanged one")
        self.assertFalse([l for l in lines if l.strip().startswith("claim:")], "a test/ci stub is never claimed epic")
        for proof, evaluator in (("live", "tester"), ("test", "tester"), ("live", "ci")):
            self.assertEqual(sb.plan_check.claim_lines(proof, evaluator), ["    claim: epic   # " + sb.plan_check.CLAIM_WHY])
        self.assertEqual(sb.plan_check.claim_lines("test", "ci"), [want])

    def test_the_brief_template_carries_the_plan_posted_report_line_once(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertEqual(text.count("working: plan posted <hash> <url>"), 1)

    def test_clauses_are_the_numbered_parts_and_a_plain_line_is_one(self):
        c = sb.plan_check.clauses_of
        self.assertEqual(c("(1) one thing; (2) another (see (3) below)"), ["one thing", "another (see (3) below)"])
        self.assertEqual(c("(1) a; (2) b; (3) c"), ["a", "b", "c"])
        self.assertEqual(len(c("(1) a :( ; (2) b ; (3) c")), 3, "an unbalanced ( in a clause does not hide the next marker")
        self.assertEqual(len(c("(1) a (b ; (2) c ; (3) d ) ; (4) e")), 4)
        self.assertEqual(c("just one sentence, no numbers"), ["just one sentence, no numbers"])
        self.assertEqual(c("(2) starts at two"), ["(2) starts at two"])
        self.assertEqual(c(""), [])
        self.assertEqual(c(None), [])

    def test_a_vague_pass_rule_fails_and_a_concrete_one_passes(self):
        for vague in ("works", "it works", "ok", "non-empty", "passes", "Works.", "no errors", "short one",
                      "it works for 1 user", "works correctly after the change is applied", "output matches the expectations",
                      "'works'", '"works"', "result = ok and everything fine", "after the change, 1 thing is fine"):
            with self.subTest(vague=vague):
                self.assertIn("vague-pass", self.codes(self.edit('pass: the list call returns status 200 and exactly 1 agent named "alpha"',
                                                              "pass: %s" % vague)))
        for concrete in ("the exit code is 0 after the run finishes", "the page shows the word 'Ready' after loading",
                         "the count is greater than 0 in the result", "latency stays under 200 ms at p95"):
            with self.subTest(concrete=concrete):
                self.assertEqual(self.codes(self.edit('pass: the list call returns status 200 and exactly 1 agent named "alpha"',
                                                      "pass: %s" % concrete)), [])
        self.assertIn("vague-pass", self.codes(self.edit('pass: the list call returns status 200 and exactly 1 agent named "alpha"',
                                                         "pass: the thing behaves nicely for the user overall")))

    def test_a_missing_or_vacuous_discriminator_fails(self):
        base = "discriminates: a build that creates nothing returns an empty list and fails"
        for new in ("discriminates: n/a", "discriminates: none", "discriminates: tbd", "discriminates: short",
                    'discriminates: the list call returns status 200 and exactly 1 agent named "alpha"',
                    "discriminates: a wrong build would fail this", "discriminates: this would fail if it is wrong",
                    "discriminates: the list call gives status 200 and exactly one agent named alpha",
                    "discriminates: the scenario checks that things are right"):
            with self.subTest(new=new):
                self.assertIn("no-discriminator", self.codes(self.edit(base, new)))
        self.assertIn("missing-discriminates", self.codes(self.edit("    " + base + "\n", "")))

    def test_each_required_key_is_required(self):
        clauses = sb.plan_check.clauses_of(GOOD_DONE_WHEN)
        for key in sb.plan_check.REQUIRED:
            with self.subTest(key=key):
                scenarios = sb.plan_check.parse_scenarios(sb.plan_check.scenario_block(GOOD_PLAN))
                del scenarios[1][key]
                defects, _ = sb.plan_check.validate(scenarios, clauses, GOOD_PLAN)
                self.assertIn("missing-%s" % key, [d["code"] for d in defects])
        scenarios = sb.plan_check.parse_scenarios(sb.plan_check.scenario_block(GOOD_PLAN))
        scenarios[1]["text"] = "   "
        self.assertIn("missing-text", [d["code"] for d in sb.plan_check.validate(scenarios, clauses, GOOD_PLAN)[0]])

    def test_id_pattern_and_uniqueness(self):
        self.assertIn("bad-id", self.codes(self.edit("id: S2", "id: X2")))
        self.assertIn("bad-id", self.codes(self.edit("id: S2", "id: S")))
        self.assertIn("duplicate-id", self.codes(self.edit("id: S2", "id: S1")))
        self.assertEqual(self.codes(self.edit("id: S2", "id: S12")), [])

    def test_covers_pattern_and_range(self):
        self.assertIn("bad-covers", self.codes(self.edit("covers: DW-2", "covers: 2")))
        self.assertIn("bad-covers", self.codes(self.edit("covers: DW-2", "covers: DW-1, DW-2")))
        self.assertIn("covers-unknown-clause", self.codes(self.edit("covers: PC-1", "covers: DW-9")))
        self.assertEqual(self.codes(self.edit("covers: PC-1", "covers: PC-7")), [])

    def test_unknown_keys_are_rejected_and_the_optional_ones_allowed(self):
        self.assertIn("unknown-key", self.codes(self.edit("    evaluator: tester", "    evaluator: tester\n    owner: me")))
        self.assertEqual(self.codes(self.edit("    evaluator: tester", "    evaluator: tester\n    slice: one\n    phase: shadow\n    claim: epic")), [])

    def test_proof_evaluator_and_claim_values_and_consistency(self):
        self.assertIn("bad-proof", self.codes(self.edit("proof: live", "proof: manual")))
        self.assertIn("bad-evaluator", self.codes(self.edit("evaluator: tester", "evaluator: bob")))
        self.assertIn("bad-claim", self.codes(self.edit("evaluator: tester", "evaluator: tester\n    claim: both")))
        for proof, evaluator, ok in (("test", "ci", True), ("test", "tester", False), ("test", "owner", False),
                                     ("screenshot", "reviewer", True), ("screenshot", "owner", True),
                                     ("screenshot", "ci", False), ("live", "tester", True), ("live", "owner", True),
                                     ("live", "ci", False), ("live", "reviewer", False), ("measure", "tester", True),
                                     ("measure", "reviewer", False)):
            with self.subTest(proof=proof, evaluator=evaluator):
                text = self.edit("proof: live", "proof: %s" % proof).replace("evaluator: tester", "evaluator: %s" % evaluator, 1)
                self.assertEqual("evaluator-mismatch" in self.codes(text), not ok)

    def test_the_old_personal_evaluator_name_is_an_alias_of_owner_and_does_not_change_the_hash(self):
        base = self.check()
        old = self.check(self.edit("evaluator: tester", "evaluator: adam"))
        new = self.check(self.edit("evaluator: tester", "evaluator: owner"))
        self.assertEqual((old["result"], new["result"]), ("pass", "pass"))
        self.assertEqual([w["code"] for w in old["warnings"] if w["code"] == "evaluator-alias"], ["evaluator-alias"])
        self.assertEqual([w for w in new["warnings"] if w["code"] == "evaluator-alias"], [])
        self.assertNotEqual(old["scenario_hash"], new["scenario_hash"], "the hash covers the text as written")
        self.assertIn("evaluator-mismatch", self.codes(self.edit("proof: live", "proof: test").replace("evaluator: tester", "evaluator: adam", 1)))

    def test_every_done_when_clause_must_be_covered_by_an_s_scenario(self):
        self.assertIn("uncovered-clause", self.codes(self.edit("covers: DW-2", "covers: PC-2")))
        self.assertEqual(self.codes(GOOD_PLAN, "(1) one; (2) two; (3) three").count("uncovered-clause"), 1)
        self.assertIn("uncovered-clause", self.codes(self.edit("id: S2", "id: U2")))  # a U scenario does not cover a clause
        self.assertIn("no-done-when", self.codes(GOOD_PLAN, ""))

    def test_an_uncovered_clause_names_the_fix(self):
        fix = "add an S scenario that covers DW-2; never edit the clause"
        got = [d["message"] for d in self.check(self.edit("covers: DW-2", "covers: PC-2"))["defects"] if d["code"] == "uncovered-clause"]
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("DW-2 is not covered by any S scenario: "), got)
        self.assertIn(fix, got[0])

    def test_a_refused_evaluator_names_the_evaluators_that_fit_the_proof(self):
        for proof, evaluator, fitting in (("test", "reviewer", "ci"), ("live", "ci", "tester or owner"),
                                          ("measure", "ci", "tester or owner"), ("screenshot", "ci", "reviewer or owner")):
            with self.subTest(proof=proof):
                text = self.edit("proof: live", "proof: %s" % proof).replace("evaluator: tester", "evaluator: %s" % evaluator, 1)
                got = self.check(text)
                self.assertEqual(got["result"], "fail")
                messages = [d["message"] for d in got["defects"] if d["code"] == "evaluator-mismatch"]
                self.assertEqual(len(messages), 1)
                self.assertTrue(messages[0].endswith("the evaluator that fits proof %s is %s" % (proof, fitting)), messages[0])

    def test_the_no_done_when_message_names_the_intent_heading(self):
        got = self.check(GOOD_PLAN, "")
        messages = [d["message"] for d in got["defects"] if d["code"] == "no-done-when"]
        self.assertEqual(got["result"], "fail")
        self.assertEqual(len(messages), 1)
        self.assertIn("## Intent", messages[0])
        self.assertIn("Done when:", messages[0])

    def test_the_skills_carry_the_paper_cut_lines(self):
        skills = REPO / "skills"
        coordination = (skills / "fleet-coordination" / "SKILL.md").read_text()
        block = coordination.split("a push voids proofs):\n\n```text\n", 1)[1].split("```", 1)[0].strip("\n").splitlines()
        self.assertEqual(len(block), 2)
        self.assertIn("test_the_role_files_do_not_grow", block[0])
        self.assertIn("TestHygiene", block[1])
        planning = (skills / "fleet-planning" / "SKILL.md").read_text()
        self.assertEqual(planning.count("`## Intent` before `Done when:`"), 1)
        review = (skills / "fleet-review" / "SKILL.md").read_text()
        lines = [l for l in review.splitlines() if l.startswith("In a repository with no review laws installed, the ORCHESTRATOR")]
        self.assertEqual(len(lines), 1)
        self.assertIn("independent review", review.split(lines[0], 1)[1].split("\n\n", 1)[0])

    def test_the_mutation_run_rule_is_said_once_in_each_skill_and_in_no_reviewer_definition(self):
        words = ("mutation or fault-injection check", "fleet-switchboard heavy", "time limit", "process group", "background", "never a reviewer")
        for skill in ("fleet-review", "fleet-coordination"):
            text = " ".join((REPO / "skills" / skill / "SKILL.md").read_text().split())
            with self.subTest(skill=skill):
                self.assertEqual(text.count("An agent that is asked to run a mutation or fault-injection check"), 1)
                sentence = text.split("An agent that is asked to run a mutation or fault-injection check", 1)[1].split("can loop.", 1)[0]
                for word in words[1:] + ("named job", "job record", "the job-start rule"):
                    self.assertIn(word, sentence)
                for old in ("never runs in the background without one", "never leaves one running in the background"):
                    self.assertEqual(text.count(old), 0)
        definitions = sorted((REPO / "agents" / "reviewers").glob("*.md"))
        self.assertEqual(len(definitions), 5)
        for path in definitions:
            with self.subTest(definition=path.name):
                self.assertNotIn("fault-injection", path.read_text().lower())

    def test_a_plan_that_opts_in_needs_a_size_in_agent_time_and_one_that_does_not_is_left_alone(self):
        self.assertEqual(self.codes(GOOD_PLAN), [], "a plan written before the rule has no plan-format line and is not flagged")
        self.assertEqual(self.codes("plan-format: 1\n" + GOOD_PLAN), [])
        self.assertEqual(self.codes("plan-format: 2\nsize: S\n" + GOOD_PLAN), [], "a light plan may carry just `size: S`")
        self.assertIn("missing-size", self.codes("plan-format: 2\n" + GOOD_PLAN))
        for valid in ("S", "M: a few hours, one file and a test", "L: about a day, three areas", "XL: several days, five areas and a migration", "xl, 3 days"):
            with self.subTest(valid=valid):
                self.assertEqual(self.codes("plan-format: 2\nsize: %s\n%s" % (valid, GOOD_PLAN)), [])

    def test_the_skill_and_the_template_state_the_size_rule_and_the_opt_in(self):
        for path in (REPO / "skills" / "fleet-planning" / "SKILL.md", REPO / "docs" / "plan-template.md"):
            text = path.read_text()
            with self.subTest(path=path.name):
                for phrase in ("t-shirt", "agent time", "plan-format: 2", "size: s"):
                    self.assertIn(phrase, text.lower())
                self.assertRegex(text, r"never (?:prefers|human-days)")

    def test_the_opt_in_line_is_bounded_read_with_bold_keys_and_applies_from_format_2_up(self):
        self.assertIn("missing-size", self.codes("plan-format: 3\n" + GOOD_PLAN))
        self.assertEqual(self.codes("plan-format: 2\n**size:** S\n" + GOOD_PLAN), [])
        self.assertEqual(self.codes("**plan-format:** 2\nsize: S\n" + GOOD_PLAN), [])
        self.assertEqual(self.codes("plan-format: " + "9" * 5000 + "\n" + GOOD_PLAN), [], "a huge number is not read, and does not crash the checker")
        self.assertIn("size-in-days", self.codes("plan-format: 2\nsize: S, 1 week\n" + GOOD_PLAN))
        self.assertIn("size-in-days", self.codes("plan-format: 2\nsize: M, 2 wk\n" + GOOD_PLAN))

    def test_prose_that_starts_with_size_does_not_hide_the_real_line_and_a_code_fence_is_not_the_plan(self):
        prose = "Size: one PR, a few files\n"
        self.assertEqual(self.codes("plan-format: 2\n" + prose + "size: M\n" + GOOD_PLAN), [], "the repro: prose before the real line")
        self.assertEqual(self.codes("plan-format: 2\nsize: M: a few hours\n" + prose + GOOD_PLAN), [])
        self.assertIn("bad-size", self.codes("plan-format: 2\n" + prose + GOOD_PLAN), "only prose: the defect shows")
        fenced = "```text\nplan-format: 2\nsize: huge\n```\n"
        self.assertEqual(self.codes(fenced + GOOD_PLAN), [], "an example in a fence does not opt a plan in")
        self.assertIn("missing-size", self.codes("plan-format: 2\n" + fenced + GOOD_PLAN), "an example size in a fence is not the plan's size")
        self.assertEqual(self.codes("plan-format: 2\n~~~\nsize: huge\n~~~\nsize: S\n" + GOOD_PLAN), [])

    def test_a_size_in_days_is_refused_for_work_under_l_and_human_time_always(self):
        for bad, code in (("M, 3 days", "size-in-days"), ("S: 1 day", "size-in-days"), ("m (2-3 days)", "size-in-days"), ("S, two days", "size-in-days"),
                          ("L, 2 human-days", "size-in-days"), ("XL: 4 person-days", "size-in-days"), ("big", "bad-size"), ("3 days", "bad-size")):
            with self.subTest(bad=bad):
                self.assertEqual(self.codes("plan-format: 2\nsize: %s\n%s" % (bad, GOOD_PLAN)), [code])
        self.assertEqual(self.codes("size: M, 3 days\n" + GOOD_PLAN), [], "without the opt-in line the size is not read at all")

    def test_an_unchanged_scenario_is_needed_unless_the_plan_says_why_not(self):
        scen = GOOD_PLAN[:GOOD_PLAN.index("  - id: U1")] + "```\n"
        self.assertIn("no-unchanged-scenario", self.codes(scen))
        self.assertEqual(self.codes(scen + "\nunchanged: none because this is a brand new endpoint with no callers\n"), [])
        self.assertIn("no-unchanged-scenario", self.codes(scen + "\nunchanged: none because\n"))
        self.assertIn("no-unchanged-scenario", self.codes(scen + "\nunchanged: none because x\n"))
        self.assertIn("no-unchanged-scenario", self.codes(scen + "\nunchanged: none because n/a ok\n"))
        self.assertIn("no-unchanged-scenario", self.codes(scen + "\nunchanged: none because short reason\n"))

    def test_more_than_thirty_scenarios_warns_but_does_not_fail(self):
        extra = "".join("  - id: S%d\n    covers: PC-1\n    text: Another behaviour of the system under test\n"
                        "    proof: test\n    pass: the call returns status 200 and the count is %d\n"
                        "    discriminates: a build that ignores the input fails the count\n    evaluator: ci\n" % (n, n)
                        for n in range(10, 40))
        text = GOOD_PLAN.replace("  - id: U1", extra + "  - id: U1", 1)
        got = self.check(text)
        self.assertEqual((got["result"], got["scenarios"]), ("pass", 33))
        self.assertEqual(sorted(w["code"] for w in got["warnings"]), ["live-no-claim", "many-scenarios", "no-constraint-list"])

    def test_the_hash_ignores_whitespace_comments_quoting_order_and_defaults_but_not_content(self):
        base = self.check()["scenario_hash"]
        spaced = GOOD_PLAN.replace("    proof: test", "    proof:   test   # note").replace("evaluator: ci", "evaluator: 'ci'  ")
        self.assertEqual(self.check(spaced)["scenario_hash"], base)
        explicit = GOOD_PLAN.replace("evaluator: ci", "evaluator: ci\n    claim: pr")
        self.assertEqual(self.check(explicit)["scenario_hash"], base, "claim: pr is the default")
        scen = sb.plan_check.parse_scenarios(sb.plan_check.scenario_block(GOOD_PLAN))
        clauses = sb.plan_check.clauses_of(GOOD_DONE_WHEN)
        self.assertEqual(sb.plan_check.content_hash(list(reversed(scen)), clauses, GOOD_PLAN), base, "order is not content")
        for old, new in (("exactly 1 agent", "exactly 2 agents"), ("evaluator: tester", "evaluator: owner"),
                         ("proof: live", "proof: measure")):
            with self.subTest(change=new):
                self.assertNotEqual(self.check(self.edit(old, new))["scenario_hash"], base)
        self.assertNotEqual(self.check(GOOD_PLAN, "(1) an owner can commission an agent; (2) it answers a chat reply.")["scenario_hash"], base)

    def test_the_hash_is_the_same_for_the_same_plan_on_every_call(self):
        self.assertEqual(self.check()["scenario_hash"], self.check()["scenario_hash"])


class TestCloudLoad(Base):
    """#1097: the load record counts each lane's live cloud workers apart from the local busy and idle counts."""

    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.disc = types.SimpleNamespace(agents={"orch": sb.Observation("orch", "s", "w:p", "idle", {"name": "orch", "role": "orchestrator"})})

    def rec(self, name, **extra):
        directory = self.state_dir / "cloud"
        directory.mkdir(exist_ok=True)
        (directory / (name + ".json")).write_text(json.dumps(dict({"worker": name, "reports_to": "orch", "url": "u", "runtime_id": "r", "hard_deadline": "2999-01-01T00:00:00Z"}, **extra)))

    def count(self, fn=None, retired=None, last=None, audit=None):
        return (fn or sb.live_cloud_by_lane)(self.state_dir, self.disc, retired or {}, last or {}, audit)

    def mutant(self, old, new):
        import re as _re
        source = SWITCHBOARD.read_text()
        body = _re.search(r"def live_cloud_by_lane.*?\n\n\n", source, _re.S).group(0)
        helper = _re.search(r"def cloud_record_live.*?\n\n\n", source, _re.S).group(0)   # #1066: the shared liveness helper holds the liveness lines
        self.assertIn(old, body + helper)
        scope = dict(vars(sb))
        exec(helper.replace(old, new), scope)
        exec(body.replace(old, new), scope)
        return scope["live_cloud_by_lane"]

    def test_live_workers_counted_by_lane_apart_from_local(self):
        self.rec("c1"); self.rec("c2"); self.rec("other", reports_to="elsewhere")
        self.assertEqual(self.count(), {"orch": 2, "elsewhere": 1})

    def test_finished_retired_and_ended_are_not_counted(self):
        self.rec("fin"); self.rec("ret"); self.rec("old", hard_deadline="2001-01-01T00:00:00Z")
        self.assertEqual(self.count(retired={"ret": {"at": 1}}, last={"fin": {"state": "done"}}), {})

    def test_no_cloud_directory_is_zero_and_silent(self):
        self.assertEqual(self.count(), {})

    def test_unreadable_directory_one_audit_line(self):
        (self.state_dir / "cloud").write_text("not a directory")
        lines = []
        audit = types.SimpleNamespace(append=lambda *a: lines.append(a))
        original = sb.Path.is_dir
        sb.Path.is_dir = lambda self: True
        try:
            self.assertEqual(self.count(audit=audit), {})
        finally:
            sb.Path.is_dir = original
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][1], "load.cloud_unreadable")

    def test_read_is_bounded_and_makes_no_call(self):
        for i in range(sb.CLOUD_LOAD_MAX_ENTRIES + 5):
            self.rec("w%03d" % i)
        calls = []
        original = sb.subprocess.run
        sb.subprocess.run = lambda *a, **k: calls.append(a)
        try:
            self.assertEqual(self.count()["orch"], sb.CLOUD_LOAD_MAX_ENTRIES)
        finally:
            sb.subprocess.run = original
        self.assertEqual(calls, [])

    def test_mutant_counting_a_finished_worker_is_caught(self):
        self.rec("fin")
        broken = self.mutant('(last_reports.get(worker) or {}).get("state") in ("done", "failed")', 'False')
        self.assertEqual(broken(self.state_dir, self.disc, {}, {"fin": {"state": "done"}}, None), {"orch": 1})   # the mutant differs
        self.assertEqual(self.count(last={"fin": {"state": "done"}}), {})

    def test_no_boss_worker_counted_under_named_fallback_lane(self):
        self.rec("c1"); self.rec("lone"); 
        p = self.state_dir / "cloud" / "lone.json"
        record = json.loads(p.read_text()); record.pop("reports_to"); p.write_text(json.dumps(record))
        self.assertEqual(self.count(), {"orch": 1, sb.CLOUD_NO_BOSS_LANE: 1})

    def test_truncation_writes_one_audit_line_per_episode(self):
        for i in range(sb.CLOUD_LOAD_MAX_ENTRIES + 3):
            self.rec("w%03d" % i)
        lines = []
        audit = types.SimpleNamespace(append=lambda *a: lines.append(a))
        sb._CLOUD_TRUNCATED.clear()
        self.count(audit=audit); self.count(audit=audit)
        self.assertEqual([l[1] for l in lines], ["load.cloud_truncated"])
        self.assertEqual(lines[0][3]["name"], "CLOUD_LOAD_MAX_ENTRIES")
        for i in range(10):
            (self.state_dir / "cloud" / ("w%03d.json" % i)).unlink()
        self.count(audit=audit)            # the episode ends
        for i in range(10):
            self.rec("w%03d" % i)
        self.count(audit=audit)            # a new episode
        self.assertEqual(len(lines), 2)

    def test_mutant_silent_truncation_is_caught(self):
        for i in range(sb.CLOUD_LOAD_MAX_ENTRIES + 3):
            self.rec("w%03d" % i)
        broken = self.mutant('"load.cloud_truncated"', '"load.nothing"')
        lines = []
        audit = types.SimpleNamespace(append=lambda *a: lines.append(a))
        sb._CLOUD_TRUNCATED.clear()
        broken(self.state_dir, self.disc, {}, {}, audit)
        self.assertNotIn("load.cloud_truncated", [l[1] for l in lines])   # the mutant differs
        sb._CLOUD_TRUNCATED.clear()
        self.count(audit=audit)
        self.assertIn("load.cloud_truncated", [l[1] for l in lines])

    def _raw(self, name, data=b""):
        d = self.state_dir / "cloud"; d.mkdir(exist_ok=True)
        (d / name).write_bytes(data)

    def _alarm(self, fn):
        import signal as _sig
        def boom(*a):
            raise AssertionError("the read blocked")
        old = _sig.signal(_sig.SIGALRM, boom); _sig.alarm(3)
        try:
            return fn()
        finally:
            _sig.alarm(0); _sig.signal(_sig.SIGALRM, old)

    def _cw_mutant(self, old, new, *more):
        src = (BIN / "cloud_worker.py").read_text()
        pairs = [(old, new)] + [(more[i], more[i + 1]) for i in range(0, len(more), 2)]
        for a, b in pairs:
            self.assertIn(a, src)
            src = src.replace(a, b)
        mod = types.ModuleType("cw_mut")
        mod.__dict__["__file__"] = str(BIN / "cloud_worker.py")
        exec(compile(src, "cw_mut", "exec"), mod.__dict__)
        return mod

    def _with_reader(self, mod, fn):
        real = sb.load_module
        sb.load_module = lambda n: mod
        try:
            return fn()
        finally:
            sb.load_module = real

    def test_fifo_does_not_block(self):
        self.rec("ok")
        os.mkfifo(str(self.state_dir / "cloud" / "f.json"))
        self.assertEqual(self._alarm(self.count), {"orch": 1})

    def test_mutant_blocking_open_is_caught(self):
        self.rec("ok")
        os.mkfifo(str(self.state_dir / "cloud" / "f.json"))
        mod = self._cw_mutant("os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr", "os.O_RDONLY | os.O_NOFOLLOW | getattr")
        with self.assertRaises(AssertionError):
            self._with_reader(mod, lambda: self._alarm(self.count))   # the mutant hangs; the alarm fails it

    def test_symlink_is_not_followed(self):
        self.rec("real", reports_to="orch")
        d = self.state_dir / "cloud"
        os.symlink(str(d / "real.json"), str(d / "link.json"))
        (d / "real.json").write_text(json.dumps({"worker": "link", "reports_to": "orch", "hard_deadline": "2999-01-01T00:00:00Z"}))   # the target names the link's worker
        self.assertEqual(self.count(), {})   # real.json names "link", not "real": skipped; link.json is a symlink: skipped
        mod = self._cw_mutant("os.O_NONBLOCK | os.O_NOFOLLOW | getattr", "os.O_NONBLOCK | getattr")
        self.assertEqual(self._with_reader(mod, self.count), {"orch": 1})   # the mutant follows the link

    def test_hostile_files_are_skipped(self):
        self.rec("ok")
        self._raw("huge.json", json.dumps({"worker": "huge", "reports_to": "orch", "hard_deadline": "2999-01-01T00:00:00Z", "pad": "x" * (sb.CLOUD_LOAD_MAX_BYTES + 10)}).encode())
        self._raw("bin.json", b"\xff\xfe\x00")
        self._raw("bad.json", b"{not json")
        self._raw("list.json", b"[1]")
        (self.state_dir / "cloud" / "dir.json").mkdir()
        self.assertEqual(self._alarm(self.count), {"orch": 1})

    def test_mutant_unbounded_read_is_caught(self):
        self._raw("huge.json", json.dumps({"worker": "huge", "reports_to": "orch", "hard_deadline": "2999-01-01T00:00:00Z", "pad": "x" * (sb.CLOUD_LOAD_MAX_BYTES + 10)}).encode())
        mod = self._cw_mutant("    if len(raw) > max_bytes:\n        return None, \"too_large\"\n", "", "os.read(fd, max_bytes + 1 - len(raw))", "os.read(fd, 1 << 20)")
        self.assertEqual(self.count(), {})
        self.assertEqual(self._with_reader(mod, self.count), {"orch": 1})   # no size bound: the huge record is read and counted

    def test_mutant_non_dict_accepted_is_caught(self):
        self._raw("list.json", b"[1]")
        mod = self._cw_mutant("    if not isinstance(record, dict):\n        return None, \"wrong_shape\"\n", "")
        with self.assertRaises(Exception):
            self._with_reader(mod, self.count)   # the mutant passes a list on, and the caller breaks on it
        self.assertEqual(self.count(), {})

    def test_mutant_non_utf8_and_bad_json_and_directory_are_caught(self):
        self.rec("ok")
        self._raw("bin.json", b"\xff")
        (self.state_dir / "cloud" / "dir.json").mkdir()
        self._raw("bad.json", b"{")
        for pair in (("    except UnicodeDecodeError:\n        return None, \"not_utf8\"", "    except UnicodeDecodeError:\n        raise RuntimeError('x')"),
                         ("    except ValueError:\n        return None, \"not_json\"", "    except ZeroDivisionError:\n        return None, \"not_json\""),
                         ("    except OSError:\n        return None, \"not_regular\"\n    finally", "    except OSError:\n        raise\n    finally",
                          "        if not stat.S_ISREG(os.fstat(fd).st_mode):\n            return None, \"not_regular\"\n", "")):
            mod = self._cw_mutant(*pair)
            try:
                got = self._with_reader(mod, self.count)
            except Exception:
                continue   # the mutant breaks the pass: caught
            self.assertNotEqual(got, {"orch": 1}, pair[0][:40])   # or loses the good record: caught
        self.assertEqual(self.count(), {"orch": 1})

    def test_record_without_valid_hard_deadline_is_not_live(self):
        self.rec("a"); self.rec("b", hard_deadline="garbage"); self.rec("c", last_report_at="2001-01-01T00:00:00Z")
        d = self.state_dir / "cloud"
        r = json.loads((d / "a.json").read_text()); r.pop("hard_deadline"); (d / "a.json").write_text(json.dumps(r))
        self.assertEqual(self.count(), {})
        self.rec("fresh")
        self.assertEqual(self.count(), {"orch": 1})
        broken = self.mutant("if ends is None or ends < now:", "if ends is not None and ends < now:")
        self.assertEqual(broken(self.state_dir, self.disc, {}, {}, None), {"orch": 3})   # the mutant counts the evidence-free records a and b with the fresh one

    def test_scan_stops_at_the_files_bound_and_the_time_bound(self):
        for i in range(20):
            self.rec("w%02d" % i)
        lines = []
        audit = types.SimpleNamespace(append=lambda *a: lines.append(a))
        old = sb.CLOUD_LOAD_MAX_SCAN
        try:
            sb.CLOUD_LOAD_MAX_SCAN = 5; sb._CLOUD_TRUNCATED.clear()
            self.assertLessEqual(self.count(audit=audit).get("orch", 0), 5)
            self.assertEqual(lines[-1][3]["bound"], "files")
            sb.CLOUD_LOAD_MAX_SCAN = 10 ** 6; sb._CLOUD_TRUNCATED.clear()
            ticks = iter(range(0, 1000))
            out = sb.live_cloud_by_lane(self.state_dir, self.disc, {}, {}, audit, None, lambda: next(ticks) * 0.3)
            self.assertLess(out.get("orch", 0), 20)
            self.assertEqual(lines[-1][3]["bound"], "time")
            broken = self.mutant("if clock() > deadline:", "if False:")
            sb._CLOUD_TRUNCATED.clear(); ticks = iter(range(0, 1000))
            self.assertEqual(broken(self.state_dir, self.disc, {}, {}, audit, None, lambda: next(ticks) * 0.3), {"orch": 20})
        finally:
            sb.CLOUD_LOAD_MAX_SCAN = old

    def test_load_line_text_carries_the_cloud_mark(self):
        fh = sb.load_fleet_health()
        src = (BIN / "fleet_health.py").read_text()
        self.assertIn("\\u2601 cloud", src)
        self.assertIn('(" +%d\\u2601 cloud" % f["cloud"]) if f.get("cloud")', src)

    def test_a_boss_with_control_characters_is_cleaned_at_the_source_and_a_non_string_is_no_boss(self):
        self.rec("e", reports_to="a\x1b[31mb\nc"); self.rec("l", reports_to="x" * 500); self.rec("n", reports_to=["orch"]); self.rec("i", reports_to=7)
        out = self.count()
        self.assertEqual(out["a[31mbc"], 1)
        self.assertEqual(out["x" * sb.CLOUD_BOSS_MAX], 1)
        self.assertEqual(out[sb.CLOUD_NO_BOSS_LANE], 2)
        self.assertTrue(all(k.isprintable() and len(k) <= sb.CLOUD_BOSS_MAX for k in out))
        mutant = self.mutant("_cloud_boss(record.get(\"reports_to\"))", "str(record.get(\"reports_to\") or \"\")")   # the mutant: the raw string is the key
        self.assertNotEqual(self.count(mutant), out)

    def test_the_shared_helper_is_the_only_place_the_liveness_test_lives(self):
        source = SWITCHBOARD.read_text()
        for line in ('seen is None or seen > now or', '(last_reports.get(worker) or {}).get("state") in ("done", "failed")', 'if ends is None or ends < now:'):
            self.assertEqual(source.count(line), 1, line)   # once: inside cloud_record_live
        helper = source[source.index("def cloud_record_live"):source.index("def live_cloud_by_lane")]
        for line in ('seen is None or seen > now or', 'if ends is None or ends < now:'):
            self.assertIn(line, helper)
        self.assertEqual(source.count("cloud_record_live(record, "), 3)   # the def and two calls: the load count and the pane health repair each call it

    def test_a_future_dated_last_report_is_not_fresh(self):
        self.rec("f", last_report_at="2998-01-01T00:00:00Z")
        self.assertEqual(self.count(), {})
        mutant = self.mutant("seen is None or seen > now or", "seen is None or")
        self.assertEqual(mutant(self.state_dir, self.disc, {}, {}, None), {"orch": 1})

    def test_a_cloud_worker_with_no_orchestrator_lane_is_shown_in_status(self):
        fh = sb.load_fleet_health()
        src = (BIN / "fleet_health.py").read_text()
        self.assertIn('state.get("cloud_unplaced")', src)
        self.assertIn('state["cloud_unplaced"] = int(getattr(ctx, "cloud_unplaced", 0) or 0)', src)
        import tempfile
        d = tempfile.mkdtemp()
        (Path(d) / "load.json").write_text(json.dumps({"latest": {"o": {"open_asks": 0, "stalled": 0, "oldest_stall_hours": None, "moved_1h": 0, "messages": 0, "decisions": 0, "busy": 0, "idle": 0}}, "cloud_unplaced": 2}))
        lines = fh.status_lines(d, 1000)
        self.assertTrue(any("2 live cloud worker(s) with no orchestrator lane" in l for l in lines), lines)

    def test_now_is_injected(self):
        self.rec("w", hard_deadline="2030-01-01T00:00:00Z")
        self.assertEqual(self.disc and sb.live_cloud_by_lane(self.state_dir, self.disc, {}, {}, None, 1893456001.0 - 86400 * 365 * 5), {"orch": 1})
        self.assertEqual(sb.live_cloud_by_lane(self.state_dir, self.disc, {}, {}, None, 1893456001.0), {})   # 2030-01-01T00:00:01Z


class TestArchitectureSafetyNet(unittest.TestCase):
    """The tools that prove a move of switchboard code is text only (plan #1092, slice S0)."""
    TOOLS = REPO / "tools"
    BEFORE = "A = 1\n\n\ndef one():\n    return 1\n\n\ndef two():\n    return 2\n"

    def load(self, name):
        loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(self.TOOLS / name))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    def split_check(self, before, after):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        sides = []
        for label, parts in (("before", before), ("after", after)):
            paths = []
            for index, text in enumerate(parts):
                path = work / ("%s%d.py" % (label, index))
                path.write_text(text)
                paths.append(str(path))
            sides.append(paths)
        proc = subprocess.run([sys.executable, str(self.TOOLS / "arch-split-check"), "--before"] + sides[0] + ["--after"] + sides[1],
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout

    def test_a_move_across_files_and_a_reorder_pass(self):
        code, out = self.split_check([self.BEFORE], ["def two():\n    return 2\n", "A = 1\n\n\ndef one():\n    return 1\n"])
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)

    def test_one_edited_line_fails_and_names_the_statement(self):
        code, out = self.split_check([self.BEFORE], [self.BEFORE.replace("return 2", "return 3")])
        self.assertEqual(code, 1, out)
        self.assertIn("FunctionDef two", out)
        self.assertIn("RESULT: FAIL", out)

    def test_a_dropped_statement_fails_and_names_the_statement_and_the_name(self):
        code, out = self.split_check([self.BEFORE], [self.BEFORE.split("\n\n\ndef two")[0] + "\n"])
        self.assertEqual(code, 1, out)
        self.assertIn("FunctionDef two", out)
        self.assertIn("name only before: two", out)

    def test_a_duplicated_statement_fails(self):
        code, out = self.split_check([self.BEFORE], [self.BEFORE + "\n\ndef two():\n    return 2\n"])
        self.assertEqual(code, 1, out)
        self.assertIn("statement added", out)

    def test_the_overlap_tool_maps_changed_lines_to_symbols_classes_and_sections(self):
        tool = self.load("arch-overlap")
        source = "# ---------- the first\nX = 1\n\n\ndef f():\n    return 1\n\n\n# ---------- the second\ndef g():\n    return 2\n"
        table = tool.symbols(source)
        marks = tool.markers(source)
        lines = tool.changed_old_lines("@@ -6,1 +6,1 @@ def f\n-    return 1\n+    return 9\n@@ -10,0 +11,2 @@\n+x\n")
        self.assertEqual(lines, {6, 10})
        self.assertEqual(tool.touched(lines, table), ["f", "g"])
        self.assertEqual([tool.section_of(n, marks) for n in (2, 6, 10)], ["the first", "the first", "the second"])
        self.assertEqual(tool.touched({7}, table), [])
        self.assertEqual([n for _, _, n in tool.classes("class A:\n    pass\n\n\ndef f():\n    pass\n")], ["A"])

    def golden(self):
        return self.load("golden-cli")

    def test_the_command_list_covers_every_verb_and_the_recording_is_deterministic(self):
        golden = self.golden()
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        root = os.path.realpath(str(work))
        env = golden.world(root)
        entry = str(SWITCHBOARD)
        first = golden.record_all(entry, root, env)
        self.assertGreaterEqual(len(first), 120)
        verbs = golden.choices(golden.run(entry, ["--help"], root, env)["stdout"])
        self.assertGreater(len(verbs), 40)
        for verb in verbs:
            self.assertIn((verb, "--help"), first, "the command list has no --help of the verb %s" % verb)
        self.assertEqual(golden.differences(first, golden.record_all(entry, root, env)), [], "two recordings of one entry differ")

    def fake_repo(self, text):
        """A git repository whose bin/fleet-switchboard prints bin/out.txt: a stand-in for the daemon."""
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "bin").mkdir()
        (repo / "bin" / "fleet-switchboard").write_text(
            "import os\nprint(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out.txt')).read(), end='')\n")
        (repo / "bin" / "out.txt").write_text(text)
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a), check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        return repo

    def compare(self, repo):
        return subprocess.run([sys.executable, str(self.TOOLS / "golden-cli"), "compare", "--base", "HEAD", "--repo", str(repo)],
                              capture_output=True, text=True)

    def test_compare_passes_an_unchanged_tree_and_names_a_changed_output(self):
        repo = self.fake_repo("usage: tool\n")
        done = self.compare(repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("RESULT: PASS", done.stdout)
        (repo / "bin" / "out.txt").write_text("usage: tool [-x]\n")
        done = self.compare(repo)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("DIFF --help: stdout differs", done.stdout)
        self.assertIn("RESULT: FAIL", done.stdout)

    def test_compare_names_a_command_the_working_tree_lost(self):
        golden = self.golden()
        base = {("--help",): {"status": 0, "stdout": "a", "stderr": ""}, ("send", "--help"): {"status": 0, "stdout": "b", "stderr": ""}}
        work = {("--help",): {"status": 2, "stdout": "a", "stderr": ""}, ("plan", "--help"): {"status": 0, "stdout": "c", "stderr": ""}}
        self.assertEqual(golden.differences(base, work), ["DIFF --help: status differs", "DIFF plan --help: the command is new in the working tree",
                                                          "DIFF send --help: the command is gone from the working tree"])

    def test_the_recorder_writes_grouped_files(self):
        repo = self.fake_repo("usage: tool\n")
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        done = subprocess.run([sys.executable, str(self.TOOLS / "golden-cli"), "record", "--repo", str(repo), "--dir", str(out)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads((out / "root.json").read_text())[0]["argv"], ["--help"])
        self.assertLessEqual(len(list(out.glob("*.json"))), 7)

    def test_the_surface_snapshot_is_a_sorted_list_of_names(self):
        names = (REPO / "tests" / "golden" / "surface-dir-sb.txt").read_text().split()
        self.assertEqual(names, sorted(names))
        for name in ("main", "cmd_status", "HerdrClient"):
            self.assertIn(name, names)

    def test_the_surface_modes_write_check_and_name_a_changed_name(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        code = work / "m.py"
        code.write_text(self.BEFORE)
        snap = work / "snap.txt"
        tool = str(self.TOOLS / "arch-split-check")
        run = lambda *a: subprocess.run([sys.executable, tool] + list(a), capture_output=True, text=True)
        self.assertEqual(run("--write-surface", str(snap), "--after", str(code)).returncode, 0)
        self.assertIn("one", snap.read_text().split())
        done = run("--check-surface", str(snap), "--after", str(code))
        self.assertEqual((done.returncode, "RESULT: PASS" in done.stdout), (0, True), done.stdout)
        snap.write_text(snap.read_text() + "extra\n")
        done = run("--check-surface", str(snap), "--after", str(code))
        self.assertEqual(done.returncode, 1)
        self.assertIn("name only in the snapshot: extra", done.stdout)
        code.write_text(self.BEFORE + "\n\ndef three():\n    return 3\n")
        snap.write_text("\n".join(n for n in snap.read_text().split() if n != "extra") + "\n")
        done = run("--check-surface", str(snap), "--after", str(code))
        self.assertIn("name only in the code: three", done.stdout)

    def split_run(self, *args):
        return subprocess.run([sys.executable, str(self.TOOLS / "arch-split-check")] + list(args), capture_output=True, text=True)

    def test_split_check_no_surface_runs_nothing_and_a_missing_before_is_a_usage_error(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        bomb = work / "bomb.py"
        bomb.write_text("raise RuntimeError('loaded')\n")
        done = self.split_run("--before", str(bomb), "--after", str(bomb), "--no-surface")
        self.assertEqual((done.returncode, "RESULT: PASS" in done.stdout), (0, True), done.stdout + done.stderr)
        self.assertNotEqual(self.split_run("--before", str(bomb), "--after", str(bomb)).returncode, 0)   # loading it fails: the surface check runs it
        self.assertEqual(self.split_run("--after", str(bomb)).returncode, 2)

    def test_split_check_takes_a_multi_part_before_and_a_reorder_inside_one_side(self):
        code, out = self.split_check(["A = 1\n", "def one():\n    return 1\n"], ["def one():\n    return 1\n\n\nA = 1\n"])
        self.assertEqual(code, 0, out)

    def test_compare_fails_on_an_unknown_revision_and_names_a_lost_command(self):
        repo = self.fake_repo("usage: tool {a} ...\n\npositional arguments:\n  {a}\n")
        entry = repo / "bin" / "fleet-switchboard"   # only the top help lists a subcommand: the walk ends
        entry.write_text(entry.read_text().replace("print(", "print('usage: sub') if sys.argv[1:] != ['--help'] else print(", 1).replace("import os", "import os, sys"))
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-am", "entry"], check=True, capture_output=True)
        done = subprocess.run([sys.executable, str(self.TOOLS / "golden-cli"), "compare", "--base", "no-such-rev", "--repo", str(repo)],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("cannot read no-such-rev", done.stdout + done.stderr)
        (repo / "bin" / "out.txt").write_text("usage: tool\n")
        done = self.compare(repo)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("DIFF a --help: the command is gone from the working tree", done.stdout)

    def overlap_repo(self):
        """A clone-like repository: main, and a pull request head at refs/pull/1/head that edits one function and one test class."""
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a), check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "bin").mkdir()
        (repo / "bin" / "fleet-switchboard").write_text("# ------ the first\nX = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n")
        (repo / "bin" / "test-switchboard").write_text("class A:\n    x = 1\n\n\nclass B:\n    y = 2\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("remote", "add", "origin", str(repo))
        git("checkout", "-q", "-b", "pr")
        (repo / "bin" / "fleet-switchboard").write_text("# ------ the first\nX = 1\n\n\ndef f():\n    return 9\n\n\ndef g():\n    return 2\n")
        (repo / "bin" / "test-switchboard").write_text("class A:\n    x = 1\n\n\nclass B:\n    y = 3\n")
        git("commit", "-q", "-am", "pr")
        git("update-ref", "refs/pull/1/head", "HEAD")
        git("checkout", "-q", "main")
        return repo

    def run_overlap(self, repo, listing, *args, gh_exit=0):
        bindir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bindir, True)
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % (json.dumps(listing), gh_exit))
        gh.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
        return subprocess.run([sys.executable, str(self.TOOLS / "arch-overlap"), "--base", "main"] + list(args), cwd=repo, env=env, capture_output=True, text=True)

    def test_overlap_names_symbols_classes_and_sections_of_a_pull_request_head(self):
        repo = self.overlap_repo()
        files = [{"path": "bin/fleet-switchboard"}, {"path": "bin/test-switchboard"}]
        done = self.run_overlap(repo, [{"number": 1, "title": "edit f", "files": files}, {"number": 2, "title": "no head", "files": files},
                                       {"number": 3, "title": "other", "files": [{"path": "README.md"}]}])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("symbols: f", done.stdout)
        self.assertNotIn("symbols: f, g", done.stdout)
        self.assertIn("test classes: B", done.stdout)
        self.assertIn("the first: #1", done.stdout)
        self.assertIn("cannot fetch the pull request head", done.stdout)   # #2 has no head ref
        self.assertNotIn("#3", done.stdout)   # touches neither file
        self.assertIn("2 open pull requests touch", done.stdout)

    def test_overlap_names_an_insertion_between_statements_and_refuses_bad_input(self):
        repo = self.overlap_repo()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a), check=True, capture_output=True)
        git("checkout", "-q", "-b", "gap")
        path = repo / "bin" / "fleet-switchboard"
        path.write_text("NEW = 3\n" + path.read_text())   # a line above every statement
        git("commit", "-q", "-am", "gap")
        git("update-ref", "refs/pull/1/head", "HEAD")
        git("checkout", "-q", "main")
        done = self.run_overlap(repo, [{"number": 1, "title": "gap", "files": [{"path": "bin/fleet-switchboard"}]}])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("between statements near line", done.stdout)
        failed = self.run_overlap(repo, [], gh_exit=1)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("gh pr list failed", failed.stderr)
        refused = self.run_overlap(repo, [], "--daemon", "-x")
        self.assertEqual(refused.returncode, 2)
        long_list = self.run_overlap(repo, [{"number": 1000 + n, "title": "t", "files": []} for n in range(200)])
        self.assertIn("200 open pull requests listed", long_list.stderr)

    def test_the_hygiene_scan_reads_one_list_of_daemon_files(self):
        self.assertIn(SWITCHBOARD, SWITCHBOARD_SOURCE_FILES)
        self.assertEqual(switchboard_source(), "\n".join(p.read_text() for p in SWITCHBOARD_SOURCE_FILES))


class TestOwnMarkerNotHonoured(unittest.TestCase):
    """#1216: shims/gh reads FLEET_SWITCHBOARD_OWN_GH nowhere (derived scan), and each probe has a mutant that honours it again which the case above it fails."""
    NAME = "FLEET_SWITCHBOARD_OWN_GH"

    def test_the_shim_reads_the_marker_nowhere(self):
        tree = ast.parse(SHIM.read_text())
        reads = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == self.NAME]
        self.assertEqual(reads, [], "a string constant naming the marker in shims/gh (a read or a set), by line")

    def test_every_own_call_site_of_the_switchboard_carries_no_marker(self):
        text = switchboard_source()
        self.assertNotIn('{OWN_GH_ENV: "1"}', text)
        self.assertNotIn('setdefault(OWN_GH_ENV', text)

    # The rule: every Python source the daemon can load: the main script, every bin/switchboard/*.py and EVERY bin/*.py (each is a module of load_module(name));
    # the scripts for humans carry no .py name, so they are not in it.
    BARE_GH_SOURCES = (["bin/fleet-switchboard"] + sorted("bin/switchboard/" + n.name for n in (REPO / "bin" / "switchboard").glob("*.py"))
                       + sorted("bin/" + n.name for n in (REPO / "bin").glob("*.py")))
    SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "runner", "_runner", "run_cmd"}
    # THE ALLOW-LIST (#1330): the two agent-facing reads that name the bare "gh" ON PURPOSE. Keyed (path, enclosing function) -> (the argv the bare call carries after "gh", the reason).
    # LIMIT: an allow-list NARROWS what this guard checks on a credential path: inside these two functions a bare "gh" is no longer flagged, so the guard says nothing about any other
    # call they add; test_every_allowed_bare_gh_entry_exists pins each entry to a real function whose bare call is the one named, and a third site anywhere else still fails the scan.
    ALLOWED_BARE_GH = {
        ("bin/fleet-switchboard", "issue_is_ask"): (("api",), "agent-facing: the shim must see this call"),
        ("bin/fleet-switchboard", "preflight_body_warnings"): (("pr", "view"), "agent-facing: the shim must see this call"),
    }

    @staticmethod
    def enclosing_function(tree, line):
        """The name of the innermost function whose span holds `line`, or None."""
        spans = [(n.end_lineno - n.lineno, n.name) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= line <= n.end_lineno]
        return min(spans)[1] if spans else None

    @classmethod
    def bare_gh_sites(cls, sources):
        """(path, line) of every place a command is built whose first argv element is the literal "gh". CLOSED rule, by AST over `sources` ({path: text}):
        (1) a LIST literal (a tuple of tool names, as in `for tool in ("gh", "git")`, is not an argv) whose first element is the string constant "gh" (covers `runner(["gh", ...])`, `["gh"] + argv` and a name bound to such a list);
        (2) a call to run/Popen/call/check_call/check_output or a runner whose first positional argument is a string constant that starts with "gh " or is "gh".
        The ONE merge call of `merge-gate --pass --merge` is not an exception: it names `config.get("gh_by_name", ...)`, never the literal, so it is not seen and needs none.
        KNOWN LIMITS, what it cannot catch: a variable holding "gh" (`g = "gh"; [g, ...]`); a tuple (deliberate: `for tool in ("gh", "git")`); an f-string
        (JoinedStr) command; `config.get(..., "gh")` (a default); a string built at run time (`"g" + "h"`, an argv read from a file or the environment);
        `config["gh_by_name"]` itself; a shell string that is not a plain constant; and any source outside BARE_GH_SOURCES; the daemon's own calls are held by
        config["gh"] (own_gh_path) and by the behaviour tests around it, which this scan does not replace."""
        found = []
        for path, text in sources.items():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "gh":
                    if (path, cls.enclosing_function(tree, node.lineno)) in cls.ALLOWED_BARE_GH:
                        continue
                    found.append((path, node.lineno))
                elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    first = node.args[0].value
                    if fn in cls.SUBPROCESS_CALLS and (first == "gh" or first.startswith("gh ")):
                        found.append((path, node.lineno))
        return found

    def test_no_bare_gh_argv_remains_in_the_switchboard_sources(self):
        """#1216: a bare "gh" reaches the shim as an AGENT call (counted, read-scoped, refusable). Derived over the sources, not a hand list; see bare_gh_sites for the rule and its limits."""
        sources = {p: (REPO / p).read_text() for p in self.BARE_GH_SOURCES}
        self.assertGreater(len(sources), 5, "the scan must see bin/switchboard/*.py too")
        self.assertEqual(self.bare_gh_sites(sources), [], "a command whose first argv element is the literal 'gh' (path, line): use config['gh'] (the real gh by path)")

    def test_every_allowed_bare_gh_entry_exists(self):
        """Each allow-list entry names a function found in the scanned sources whose bare gh list is the one the entry names, with a reason; a rename or a deleted function fails here."""
        self.assertEqual(sorted(self.ALLOWED_BARE_GH), [("bin/fleet-switchboard", "issue_is_ask"), ("bin/fleet-switchboard", "preflight_body_warnings")])
        for (path, name), (argv, reason) in sorted(self.ALLOWED_BARE_GH.items()):
            self.assertIn(path, self.BARE_GH_SOURCES)
            self.assertTrue(reason)
            tree = ast.parse((REPO / path).read_text())
            funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
            self.assertEqual(len(funcs), 1, "the allowed function %s is not in %s" % (name, path))
            lists = [n for n in ast.walk(funcs[0]) if isinstance(n, ast.List) and n.elts and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == "gh"]
            self.assertEqual(len(lists), 1, "%s must hold exactly one bare gh call" % name)
            self.assertEqual(tuple(e.value for e in lists[0].elts[1:1 + len(argv)] if isinstance(e, ast.Constant)), argv)

    def test_a_third_bare_gh_site_still_fails_the_scan(self):
        """The hand mutant: a bare gh planted in a temp copy of a scanned source, outside the allow-list, is flagged (and one in an allowed function's neighbour too)."""
        sources = {p: (REPO / p).read_text() for p in self.BARE_GH_SOURCES}
        self.assertEqual(self.bare_gh_sites(sources), [])
        planted = dict(sources, **{"bin/switchboard/audit.py": sources["bin/switchboard/audit.py"] + '\n\ndef third_site(runner):\n    return runner(["gh", "api", "user"])\n'})
        self.assertEqual(len(self.bare_gh_sites(planted)), 1)
        neighbour = sources["bin/fleet-switchboard"] + '\n\ndef issue_is_ask_again(runner):\n    return runner(["gh", "api", "user"])\n'
        self.assertEqual(len(self.bare_gh_sites(dict(sources, **{"bin/fleet-switchboard": neighbour}))), 1)

    def test_a_mutant_that_re_adds_a_bare_gh_site_fails_the_scan(self):
        sources = {p: (REPO / p).read_text() for p in self.BARE_GH_SOURCES}
        for path, old, new in (("bin/fleet-switchboard", 'runner([ctx.config["gh"], "pr", "diff", str(ctx.number)', 'runner(["gh", "pr", "diff", str(ctx.number)'),
                               ("bin/fleet-switchboard", 'runner([real_gh, "pr", "view"', 'runner(["gh", "pr", "view"')):
            self.assertIn(old, sources[path])
            mutant = dict(sources, **{path: sources[path].replace(old, new, 1)})
            self.assertEqual(len(self.bare_gh_sites(mutant)), 1, old)
        self.assertIn("bin/ci_runs.py", sources)
        old = 'subprocess.run([GH, "api", "repos/%s/actions/runs/%d"'
        self.assertIn(old, sources["bin/ci_runs.py"])
        mutant = dict(sources, **{"bin/ci_runs.py": sources["bin/ci_runs.py"].replace(old, old.replace("[GH,", '["gh",'), 1)})
        self.assertEqual(len(self.bare_gh_sites(mutant)), 1, "a bare gh re-added in bin/ci_runs.py")
        extra = dict(sources, **{"bin/switchboard/audit.py": sources["bin/switchboard/audit.py"] + '\nsubprocess.run("gh pr list")\n'})
        self.assertEqual(len(self.bare_gh_sites(extra)), 1, "a string command in a module under bin/switchboard is seen too")

    def test_the_own_call_path_resolves_the_real_gh_beside_the_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = Path(tmp, "shim"), Path(tmp, "real")
            shimdir.mkdir(); realdir.mkdir()
            shimdir.joinpath("gh").write_text(SHIM.read_text()); shimdir.joinpath("gh").chmod(0o755)
            realdir.joinpath("gh").write_text("#!/bin/sh\nexit 0\n"); realdir.joinpath("gh").chmod(0o755)
            path = os.pathsep.join([str(shimdir), str(realdir)])
            self.assertEqual(sb.own_gh_path("gh", {"PATH": path}), str(realdir / "gh"))
            self.assertEqual(sb.own_gh_path("gh", {"PATH": str(realdir)}), str(realdir / "gh"), "the real gh first on PATH is named by its path")
            self.assertEqual(sb.own_gh_path("gh", {"PATH": path, "FLEET_REAL_GH": str(realdir / "gh")}), str(realdir / "gh"))
            self.assertEqual(sb.own_gh_path("/x/gh", {"PATH": path}), "/x/gh")

    def test_the_config_load_hands_the_resolved_gh_to_the_run_identity_reader(self):
        """#1216: bin/ci_runs.py reads a run through config['gh'] (set at the config load), so a daemon the hook started never reaches the shim by name."""
        ci = sb.load_module("ci_runs")
        self.addCleanup(setattr, ci, "GH", ci.GH)
        ci.GH = None
        cfg = sb.validate_config({})
        self.assertEqual(ci.GH, cfg["gh"])
        self.assertNotEqual(ci.GH, "gh")

    REAL_FAKE = "#!/bin/sh\necho \"real $* token=$GH_TOKEN\" >> \"$FAKE_LOG\"\necho '{\"ok\": 1}'\n"

    def layout(self, tmp, real=True):
        shimdir, realdir = Path(tmp, "shim"), Path(tmp, "real")
        shimdir.mkdir(); realdir.mkdir()
        for entry in SHIM.parent.iterdir():
            if entry.is_file():
                shutil.copy(str(entry), str(shimdir / entry.name))
        if real:
            realdir.joinpath("gh").write_text(self.REAL_FAKE); realdir.joinpath("gh").chmod(0o755)
        return shimdir, realdir

    def test_the_daemon_refuses_at_start_when_its_gh_is_the_shim(self):
        """No real gh behind the shim: PATH holding only the shim, or a configured absolute gh that IS the shim, is refused with the named reason (never run as an agent)."""
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = self.layout(tmp, real=False)
            with self.assertRaisesRegex(sb.ConfigError, "no real gh behind the shim"):
                sb.own_gh_path("gh", {"PATH": str(shimdir)})
            with self.assertRaisesRegex(sb.ConfigError, "no real gh behind the shim"):
                sb.own_gh_path("gh", {"PATH": str(shimdir), "FLEET_REAL_GH": str(shimdir / "gh")})
            with self.assertRaisesRegex(sb.ConfigError, "no real gh behind the shim"):
                sb.own_gh_path(str(shimdir / "gh"), {"PATH": str(shimdir)})
            done = subprocess.run([sys.executable, str(SWITCHBOARD), "run", "--once"], capture_output=True, text=True, timeout=60,
                                  env={"PATH": str(shimdir), "HOME": tmp, "XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp, "FLEET_SWITCHBOARD_STATE": tmp})
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("no real gh behind the shim", done.stderr)

    def test_an_absolute_real_gh_is_used_as_it_is_and_a_real_gh_behind_the_shim_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = self.layout(tmp)
            self.assertEqual(sb.own_gh_path(str(realdir / "gh"), {"PATH": str(shimdir)}), str(realdir / "gh"))
            self.assertEqual(sb.own_gh_path("gh", {"PATH": os.pathsep.join([str(shimdir), str(realdir)])}), str(realdir / "gh"))
            self.assertEqual(sb.own_gh_path("gh", {"PATH": os.pathsep.join([str(shimdir), str(realdir)]), "FLEET_REAL_GH": str(shimdir / "gh")}), str(realdir / "gh"),
                             "FLEET_REAL_GH naming the shim is ignored")
            config = {"github_repos": [], "github_events": ["issue_comment"], "gh": str(realdir / "gh"), "github_lookback_hours": 1, "github_port": None, "github_ignore_authors": []}
            with self.assertRaisesRegex(sb.ConfigError, "no real gh behind the shim"):
                sb._validate_github(dict(config, gh=str(shimdir / "gh")), "cfg")

    def test_the_resolved_path_prevents_re_identification_the_daemons_call_reaches_the_real_gh_by_path_never_the_shim(self):
        """The deleted regression, restored: with the marker gone, a daemon call through the SHIM would be re-identified (token swapped). The resolved path never reaches it."""
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = self.layout(tmp)
            log = Path(tmp, "calls.log")
            path = os.pathsep.join([str(shimdir), str(realdir)])
            resolved = sb.own_gh_path("gh", {"PATH": path})
            self.assertEqual(resolved, str(realdir / "gh"))
            environ = {"PATH": path, "HOME": tmp, "FAKE_LOG": str(log), "GH_TOKEN": "tok-daemon", "FLEET_SWITCHBOARD_STATE": tmp}
            done = subprocess.run([resolved, "api", "rate_limit"], capture_output=True, text=True, env=environ, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(log.read_text().splitlines(), ["real api rate_limit token=tok-daemon"], "the fake real gh ran, with the daemon's own token, unswapped")
            self.assertFalse(Path(tmp, "agent-calls.jsonl").exists(), "the shim never saw it: nothing counted")

    def test_the_validated_config_keeps_the_resolved_real_gh_and_a_daemon_call_never_runs_the_shim(self):
        """#1216 wiring: _validate_github must STORE the resolved path (a bare own_gh_path(...) that discards it leaves 'gh' and the daemon back behind the shim)."""
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = self.layout(tmp)
            log = Path(tmp, "calls.log")
            path = os.pathsep.join([str(shimdir), str(realdir)])
            base = {"github_repos": [], "github_events": ["issue_comment"], "gh": "gh", "github_lookback_hours": 1, "github_port": None, "github_ignore_authors": []}
            with mock.patch.dict(os.environ, {"PATH": path, "FAKE_LOG": str(log), "GH_TOKEN": "tok-daemon", "HOME": tmp, "FLEET_SWITCHBOARD_STATE": tmp}):
                os.environ.pop("FLEET_REAL_GH", None)
                config = sb.validate_config({"gh": "gh"})
                self.assertEqual(config["gh"], str(realdir / "gh"), "the config holds the RESOLVED real gh, not the bare name")
                done = subprocess.run([config["gh"], "api", "rate_limit"], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(log.read_text().splitlines(), ["real api rate_limit token=tok-daemon"])
                self.assertFalse(Path(tmp, "agent-calls.jsonl").exists())
                full = sb.validate_config({"gh": "gh"})
                self.assertEqual(full["gh"], str(realdir / "gh"))
            source = switchboard_source()
            line = 'config["gh"] = own_gh_path(config["gh"])'
            self.assertIn(line, source)
            mutant = source.replace(line, 'own_gh_path(config["gh"])', 1)   # validate, then DISCARD
            scope = {"__name__": "mutant_discard", "__file__": str(SWITCHBOARD)}
            exec(compile(mutant, "mutant", "exec"), scope)
            with mock.patch.dict(os.environ, {"PATH": path}):
                kept = scope["validate_config"]({"gh": "gh"})
            self.assertEqual(kept["gh"], "gh", "the discarding mutant leaves the bare name: the assertion above fails it")

    def test_a_relative_configured_gh_that_is_the_shim_is_refused_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            shimdir, realdir = self.layout(tmp, real=False)
            cwd = os.getcwd()
            os.chdir(str(shimdir))
            try:
                with self.assertRaisesRegex(sb.ConfigError, "no real gh behind the shim"):
                    sb.own_gh_path("./gh", {"PATH": str(shimdir)})
            finally:
                os.chdir(cwd)

    def test_the_shim_is_recognised_by_its_real_file_and_editing_the_docstring_fails_here(self):
        self.assertTrue(sb.is_shim_file(str(SHIM)), "own_gh_path must know the REAL shims/gh by its signature: update SHIM_HEAD together with the docstring")
        self.assertIn(sb.SHIM_HEAD, SHIM.read_bytes()[:400])
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp, "gh")
            other.write_text("#!/bin/sh\nexit 0\n"); other.chmod(0o755)
            self.assertFalse(sb.is_shim_file(str(other)))
            self.assertFalse(sb.is_shim_file(str(Path(tmp, "missing"))))

    def test_each_mutant_that_honours_the_marker_again_is_seen(self):
        """Each mutant puts a marker read back into one shim function; the derived scan fails for every one of them (and passes for the real code above)."""
        source = SHIM.read_text()
        for func in ("review_probe", "gate_probe", "merge_gate_probe", "read_scope", "audit_write", "read_account_env", "account_env", "count_call"):
            with self.subTest(func=func):
                self.assertIn("def %s(" % func, source)
                head = source[source.index("def %s(" % func):]
                body_at = head.index(":\n", head.index(")")) + 2
                mutant = source.replace(head, head[:body_at] + '    if os.environ.get("%s") == "1":\n        return None\n' % self.NAME + head[body_at:], 1)
                tree = ast.parse(mutant)
                self.assertTrue([n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == self.NAME], func)

"""Test classes moved out of bin/test-switchboard (text unchanged): cases_11."""
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
from support.core import Base
from support.fake_hubs import tri_issue
from support.loader import SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, _load, copy_entry, sb, switchboard_source
from support.paths import BIN, REPO
from support.wire_fakes import EPOCH, fact


class TestOwnerHoldRule(unittest.TestCase):
    """Ask #451, slice 1: the owner's own clear is for credentials, identity and who may do what only. The rule is data in the impact matrix file."""

    def setUp(self):
        self.floor = sb.load_module("impact_floor")
        self.matrix = self.floor.load_matrix()

    def decide(self, paths, impacts=(), unknown=None, matrix=None):
        matrix = matrix or self.matrix
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        hit = self.floor.floor_paths(paths, globs, exclude, force)
        return self.floor.owner_decide(matrix, paths, list(impacts), unknown, hit)

    def test_s1_the_decision_code_names_no_path_or_class_and_agrees_with_the_file(self):
        import ast
        tree = ast.parse((BIN / "impact_floor.py").read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                docstrings.add(id(node.body[0].value))
        constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings}
        block = self.matrix["owner_hold"]
        from_file = set(block["top_tier_classes"]) | set(block["other_classes"]) | {g for globs in block["top_tier_paths"].values() for g in globs}
        from_file |= {g for entry in block["floor"].values() for key in ("globs", "exclude", "force") for g in entry.get(key, [])}
        self.assertTrue(from_file)
        self.assertEqual(sorted(constants & from_file), [], "a path or class of the matrix file is written in the module's code")

    def test_every_top_tier_path_is_inside_the_floor_or_listed_as_outside_it(self):
        block = self.matrix["owner_hold"]
        outside = set(block["top_tier_outside_floor"])
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        for cls, patterns in block["top_tier_paths"].items():
            for pattern in patterns:
                sample = pattern.replace("**", "x").replace("*", "x")
                inside = bool(self.floor.floor_paths([sample], globs, exclude, force))
                self.assertTrue(inside != (pattern in outside), "%s (%s): inside the floor %s, listed as outside it %s" % (pattern, cls, inside, pattern in outside))

    def test_a_missing_or_malformed_block_holds_every_change_and_a_broken_file_is_never_read_as_no_floor(self):
        for broken in ({}, {"owner_hold": "x"}, {"owner_hold": {"top_tier_classes": ["credentials"], "other_classes": [], "top_tier_paths": {"credentials": "shims/gh"}}},
                       {"owner_hold": {"top_tier_classes": ["credentials"], "other_classes": [], "top_tier_paths": {}}}):
            decision, reasons = self.floor.owner_decide(broken, ["docs/notes.md"], [])
            self.assertEqual(decision, "hold", broken)
            self.assertIn("missing or malformed", reasons[0])
        bad = self.home_file("matrix.json", "{not json")
        with mock.patch.object(self.floor, "MATRIX_PATH", bad):
            self.floor._MATRIX_CACHE.clear()
            with self.assertRaises(self.floor.MatrixError):
                self.floor.floor_for("adamkaplan/fleet-kit")
        self.floor._MATRIX_CACHE.clear()
        self.assertTrue(self.floor.floor_for("adamkaplan/fleet-kit")[0], "the real file reads again")

    def test_an_odd_character_in_a_path_never_escapes_a_glob_and_a_suffix_never_makes_an_exact_file_match(self):
        for path in ("shims/a\nb", "shims/a\tb", "shims/a\x00b", "shims/a b ", "shims/" + "x" * 5000, "agents/x\nz.md", "agents/opencode/\x00x.md",
                     "plugins/fleet-hooks/a\nb", "SHIMS/A\nB"):
            self.assertEqual(self.decide([path])[0], "hold", repr(path[:40]))
            self.assertTrue(self.floor.matches(path, ["shims/**", "agents/**", "plugins/fleet-hooks/**"]), repr(path[:40]))
        for path in ("bin/plan_ledger.py\nx", "bin/plan_ledger.py\n", "bin/plan_ledger.py ", "bin/plan_ledger.pyx"):
            self.assertEqual(self.decide([path])[0], "none", "an exact file with a suffix is a different file: %r" % path)
        self.assertEqual(self.decide(["bin/plan_ledger.py"])[0], "hold")
        review = sb.load_module("plan_review")
        self.assertEqual(review.path_hits(["shims/a\nb", "docs/x.md"], ["shims/**"]), ["shims/a\nb"], "the held-path glob of the review tier has the same rule")

    def test_a_variant_spelling_of_a_top_tier_path_is_that_path_and_a_path_that_leaves_the_repository_holds(self):
        review = sb.load_module("plan_review")
        for path in ("Shims/GH", "shims//gh", "shims/./gh", "shims/x/../gh", "a/../shims/gh", "shims\\gh", "./shims/gh",
                     "agents/../AGENTS/opencode/../opencode/chief-of-staff.md", "BIN/Plan_Ledger.py", "bin//plan_ledger.py", "bin\\plan_ledger.py"):
            self.assertEqual(self.decide([path])[0], "hold", repr(path))
            self.assertEqual(review.path_hits([path], ["shims/**", "agents/**", "bin/plan_ledger.py"]), [path], "review tier: " + repr(path))
        for path in ("../x", "../../shims/gh", "a/../../x", "shims/../../x", "/etc/x", "/shims/gh", "~/x", "C:/x", "c:\\\\x"):
            decision, reasons = self.decide([path])
            self.assertEqual(decision, "hold", repr(path))
            self.assertTrue(any("outside the repository" in r for r in reasons), reasons)
            self.assertEqual(review.path_hits([path], ["docs/**"]), [path], "a path that cannot be read inside the repository is held by the review tier too")
            self.assertEqual(self.floor.floor_paths([path], ["docs/**"], ["docs/x/**"]), [path])
        for path in ("docs/notes.md", "docs/./notes.md", "Docs//Notes.md", "docs/a/../b.md"):
            self.assertEqual(self.decide([path])[0], "none", "a variant of a path outside the top tier stays outside: " + repr(path))

    def test_an_exclude_judges_only_the_named_file_a_force_path_and_the_two_tiers_share_one_normaliser(self):
        globs, exclude, force = ["shims/**"], ["shims/docs/**"], ["shims/docs/keep.md"]
        self.assertEqual(self.floor.floor_paths(["shims/docs/../gh"], globs, exclude), ["shims/docs/../gh"], "a spelling never moves a path into an exclude")
        self.assertEqual(self.floor.floor_paths(["shims/docs/x.md", "Shims//Docs//x.md"], globs, exclude), [], "a real excluded file stays excluded, in any spelling")
        self.assertEqual(self.floor.floor_paths(["shims/./docs/keep.md", "SHIMS/DOCS/KEEP.MD"], globs, exclude, force), ["SHIMS/DOCS/KEEP.MD", "shims/./docs/keep.md"],
                         "a force path is never excluded, in any spelling")
        review = sb.load_module("plan_review")
        for path in ("Shims/GH", "a/../x", "../x", "shims//./gh", "x\\y", "", "/", "..", "a/b/../../..", "bin\\..\\shims\\gh", "/etc/x", "~/x", "C:/x", "c:\\x", "a//b/./c/../d", "Ünï/../Ünï/x", "x\n/../y", "\u017fhims/gh", "\u212aeep/x", "ﬁle", "e\u0301", "\u00e9", "STRASSE/ß", "İ/i̇"):
            self.assertEqual(review._canonical(path), self.floor.canonical(path), repr(path))
            self.assertEqual(review._folds(path), self.floor.folds(path), "the fold copies agree: " + repr(path))

    def test_a_unicode_fold_of_a_top_tier_path_is_that_path_on_a_case_and_normalisation_insensitive_file_system(self):
        review = sb.load_module("plan_review")
        for path in ("\u017fhims/gh", "\u017fHIMS/Gh", ".github/wor\u212aflows/ci.yml", "agents/opencode/chi\u0065\u0301f.md", "BIN/PLAN_LEDGER.PY"):
            self.assertEqual(self.decide([path])[0], "hold", repr(path))
            self.assertEqual(review.path_hits([path], ["shims/**", ".github/workflows/**", "agents/**", "bin/plan_ledger.py"]), [path], "review tier: " + repr(path))
        self.assertTrue(self.floor.matches("e\u0301.txt", ["\u00e9.txt"]), "a decomposed accent is the composed one, in a glob too")
        self.assertTrue(self.floor.matches("\u00e9.txt", ["e\u0301.txt"]))
        self.assertEqual(self.floor.folds("\ufb01"), {"fi", "\ufb01"}, "a ligature folds to its letters, and its lower-case spelling is kept too")
        self.assertEqual(self.decide(["docs/\u017fhims.md"])[0], "none", "a fold of a name that matches no top tier glob stays outside")

    def test_the_fold_is_a_superset_of_the_old_lower_case_match(self):
        self.assertTrue(self.floor.matches("shims/\u00df", ["shims/?"]), "casefold turns a sharp s into ss, but the lower-case spelling is still tried")
        self.assertIn("\u00df", self.floor.folds("\u00df"))
        for text in ("SHIMS/Gh", "Agents/X.md", "\u00c9COLE"):
            self.assertTrue({text.lower()} <= self.floor.folds(text), text)

    def home_file(self, name, text):
        tmp = Path(tempfile.mkdtemp(prefix="ohr"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / name
        path.write_text(text)
        return path

    def test_a_deleted_path_a_renames_old_path_a_case_variant_and_a_cased_class_are_judged(self):
        self.assertEqual(self.decide(["docs/new-name.md", "shims/gh"])[0], "hold", "a rename out of a shim arrives as its old path")
        self.assertEqual(self.decide(["SHIMS/Git"])[0], "hold", "case-insensitive")
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", "AVAILABILITY ")])[0], "second-review", "a class name is compared case-insensitively")
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", "Credentials")])[0], "hold")
        self.assertEqual(self.decide(["x.py"], [("Marginal", "Remote", "credentials")])[0], "second-review", "a medium-band rating of a top tier class asks for a second review")

    def test_s2_a_change_to_the_shims_the_ledger_a_permission_block_or_the_matrix_holds_for_the_owner(self):
        for path in ("shims/gh", "shims/git", "bin/plan_ledger.py", "agents/opencode/chief-of-staff.md", "docs/review-laws/impact-matrix.json"):
            decision, reasons = self.decide([path, "docs/notes.md"])
            self.assertEqual(decision, "hold", path)
            self.assertTrue(any(path in reason for reason in reasons), (path, reasons))

    def test_s3_the_cloud_image_and_a_docs_file_do_not_hold_for_the_owner_and_the_launchd_template_does_since_it_is_on_clear_code_paths(self):
        paths = ["cloud/launcher/Dockerfile", "docs/switchboard.md"]
        decision, reasons = self.decide(paths)
        self.assertEqual((decision, reasons), ("none", []))
        for path in paths:
            self.assertNotEqual(self.decide([path])[0], "hold", path)
        decision, reasons = self.decide(["templates/com.fleet-kit.switchboard.plist"])   # #939: templates/** is on clear_code_paths, so it holds
        self.assertEqual(decision, "hold", reasons)

    def test_s4_only_a_top_tier_or_unknown_class_at_a_hold_level_holds(self):
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", "credentials")])[0], "hold")
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", "a-class-nobody-named")])[0], "hold")
        decision, reasons = self.decide(["x.py"], [("Critical", "Remote", "availability")])
        self.assertEqual(decision, "second-review", reasons)

    def test_s5_a_rating_with_no_class_is_top_tier(self):
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", None)])[0], "hold")
        self.assertEqual(self.decide(["x.py"], [("Negligible", "Improbable", None)])[0], "none", "a low rating asks for nothing, class or not")

    def test_s6_a_diff_that_cannot_be_read_in_full_holds(self):
        decision, reasons = self.decide([], unknown="the file list was cut off")
        self.assertEqual(decision, "hold")
        self.assertIn("floor unknown", reasons[0])

    def test_s7_this_slices_own_files_hold_for_the_owner(self):
        decision, reasons = self.decide(["bin/impact_floor.py", "docs/review-laws/impact-matrix.json", "bin/test-switchboard"])
        self.assertEqual(decision, "hold")
        joined = " ".join(reasons)
        self.assertIn("bin/impact_floor.py", joined)
        self.assertIn("docs/review-laws/impact-matrix.json", joined)

    def test_s11_a_change_to_the_files_list_changes_the_decision_with_no_code_change(self):
        before = self.decide(["tools/new-gate.sh"])
        edited = json.loads(json.dumps(self.matrix))
        edited["owner_hold"]["top_tier_paths"]["authority"].append("tools/new-gate.sh")
        after = self.decide(["tools/new-gate.sh"], matrix=edited)
        self.assertEqual((before[0], after[0]), ("none", "hold"))
        edited["owner_hold"]["other_classes"].append("a-class-nobody-named")
        self.assertEqual(self.decide(["x.py"], [("Critical", "Remote", "a-class-nobody-named")], matrix=edited)[0], "second-review")

    def test_a_matrix_with_no_owner_hold_block_changes_nothing_for_the_old_floor_and_the_old_floor_is_read_from_the_file(self):
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        self.assertIn("shims/**", globs)
        self.assertIn("bin/impact_floor.py", force)
        self.assertEqual(self.floor.floor_for("someone/else"), ([], [], []))
        self.assertIsNone(self.floor.owner_hold_rule({}))
        self.assertTrue(self.floor.class_is_top_tier({}, "availability"), "no block: everything is top tier (fail closed)")


class TestAgentRegistryInventory(unittest.TestCase):
    """docs/agent-registry.md (#906): every reader or writer of an agent list or record in the code is a row of its inventory, and the
    interface spec has its sections and its freeze line."""
    DOC = REPO / "docs" / "agent-registry.md"
    RECORDS = {
        "pane-health.json": r"pane-health\.json|PANE_HEALTH_FILE|PANE_FILE|read_pane_health|write_pane_health|pane_health_pass|pane_health_status",
        "retired.json": r"retired\.json|RETIRED_FILE|read_retired|known_agent",
        "last-reports.json": r"last-reports\.json|LAST_REPORT_FILE|record_last_report",
        "boss-overrides.json": r"boss-overrides\.json|\.overlay\(",
        "boss-resolution": r"resolve_boss\(|boss_name\(",
        "discovery.agents": r"discovery\.agents|\.discover\(",
        "health-records.json": r"health-records\.json|health_records|RECORDS_FILE",
        "tool-rate.json": r"tool-rate\.json|TALLY_FILE|TOOL_FILE",
        "load.json": r"load\.json|LOAD_FILE",
        "decisions.json": r"decisions\.json|DECISIONS_FILE",
        "cloud-records": r"CLOUD_DIR|cloud_dir",
        "heartbeat-agent-list": r"tabs_with_agents",
        "headless-records": r"headless-service\.json|headless-route\.json|HEADLESS_SERVICE_FILE|HEADLESS_ROUTE_FILE|headless/",
    }
    SECTIONS = ("### 1. Read API", "### 2. Write API", "### 3. The file", "### 4. The lock", "### 5. The atomic write", "### 6. The version rule")

    @staticmethod
    def sources(root=None):
        """Every tracked non-test source under bin/, plugins/ and cloud/ (from git, not globs): scripts with no extension, nested plugin files and the launcher included."""
        root = Path(root or REPO)
        listed = subprocess.run(["git", "ls-files", "bin", "plugins", "cloud"], cwd=str(root), capture_output=True, text=True, check=True).stdout.split("\n")
        found = []
        for name in listed:
            path = root / name
            parts = path.relative_to(root).parts
            if not name or not path.is_file() or path.is_symlink() or "node_modules" in parts or "tests" in parts:
                continue
            base = path.name
            if base.startswith("test-") or ".test." in base or base == "test-switchboard":
                continue
            if path.suffix in (".py", ".mjs", ".js", ".ts", ".tsx", ".sh") or (not path.suffix and path.read_bytes()[:2] == b"#!"):
                found.append(path)
        return found

    @classmethod
    def in_code(cls, root=None):
        pairs = set()
        for path in cls.sources(root):
            text = path.read_text(errors="replace")
            for record, regex in cls.RECORDS.items():
                if re.search(regex, text):
                    pairs.add((str(path.relative_to(root or REPO)), record))
        return pairs

    @classmethod
    def spec_gaps(cls, text):
        gaps = [h for h in cls.SECTIONS + ("## Migration rule",) if "\n" + h + "\n" not in text]
        if "FROZEN: when the product, chief-of-staff and daemon lanes have each commented agreement on #906 (no earlier)." not in text:
            gaps.append("the FROZEN line")
        return gaps

    @staticmethod
    def in_table(text):
        pairs = set()
        for line in text.splitlines():
            cells = [c.strip().strip("`") for c in line.split("|")[1:-1]]
            if len(cells) == 6 and ":" in cells[0] and not cells[0].startswith("file:"):
                pairs.add((cells[0].rsplit(":", 1)[0], cells[1]))
        return pairs

    def test_every_reader_and_writer_in_the_code_is_a_row_of_the_inventory(self):
        in_code = self.in_code()
        self.assertGreaterEqual(len(in_code), 25)   # the scan itself is alive
        missing = sorted(in_code - self.in_table(self.DOC.read_text()))
        self.assertEqual(missing, [], "readers or writers missing from docs/agent-registry.md")

    def test_the_scan_sees_every_kind_of_source_and_a_new_reader_in_each_is_found(self):
        """Blind spots closed as a class: an extensionless script, a nested plugin file and the cloud launcher each, with a mutant reader."""
        root = Path(tempfile.mkdtemp(prefix="registry-scan-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        wanted = {"bin/fleet-x": "#!/usr/bin/env python3\nopen('pane-health.json')\n", "plugins/p/src/deep/x.mjs": "const f = 'retired.json'\n",
                  "cloud/launcher/x.py": "F = 'last-reports.json'\n", "bin/test-x": "'pane-health.json'\n"}
        for name, body in wanted.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(body)
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        self.assertEqual(self.in_code(root), {("bin/fleet-x", "pane-health.json"), ("plugins/p/src/deep/x.mjs", "retired.json"), ("cloud/launcher/x.py", "last-reports.json")})

    def test_no_file_names_pane_health_outside_the_table(self):
        """The bare string `pane-health` in any scanned source is a row; a reader that builds the path from parts or lists the state directory
        is NOT found by a scan of text (the limit, stated in docs/agent-registry.md: slice 3's runtime test covers it)."""
        table = self.in_table(self.DOC.read_text())
        for path in self.sources():
            if re.search(r"pane-health", path.read_text(errors="replace")):
                with self.subTest(path=str(path.relative_to(REPO))):
                    self.assertIn((str(path.relative_to(REPO)), "pane-health.json"), table)

    @staticmethod
    def rows_without_the_record(pairs, root=None):
        """Rows whose file never touches their record (a made-up row)."""
        root = Path(root or REPO)
        return sorted((f, r) for f, r in pairs if r in TestAgentRegistryInventory.RECORDS and not re.search(TestAgentRegistryInventory.RECORDS[r], (root / f).read_text(errors="replace")))

    def test_every_row_names_a_record_its_file_touches(self):
        self.assertEqual(self.rows_without_the_record(self.in_table(self.DOC.read_text())), [])

    def test_a_made_up_row_is_found(self):
        root = Path(tempfile.mkdtemp(prefix="registry-rows-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "a.py").write_text("print('nothing here')\n")
        (root / "b.py").write_text("open('pane-health.json')\n")
        found = self.rows_without_the_record({("a.py", "pane-health.json"), ("b.py", "pane-health.json")}, root)
        self.assertEqual(found, [("a.py", "pane-health.json")])

    def test_every_cited_file_exists(self):
        for path, record in self.in_table(self.DOC.read_text()):
            with self.subTest(path=path, record=record):
                self.assertTrue((REPO / path).is_file(), path)

    def test_a_removed_row_is_found_missing(self):
        lines = [l for l in self.DOC.read_text().splitlines() if "plugins/fleet-decisions-tui/health.mjs" not in l or "`pane-health.json`" not in l]
        self.assertIn(("plugins/fleet-decisions-tui/health.mjs", "pane-health.json"), self.in_code() - self.in_table("\n".join(lines)))

    def test_the_spec_has_its_six_sections_and_the_freeze_line(self):
        self.assertEqual(self.spec_gaps(self.DOC.read_text()), [])

    def test_a_spec_without_the_version_rule_is_found_incomplete(self):
        text = self.DOC.read_text().replace("### 6. The version rule", "### 6. Other")
        self.assertEqual(self.spec_gaps(text), ["### 6. The version rule"])
        self.assertEqual(self.spec_gaps(self.DOC.read_text().replace("FROZEN:", "Frozen:")), ["the FROZEN line"])


class TestClearCodePathsAlwaysHeld(unittest.TestCase):
    """Ask #939: a path of the matrix's `clear_code_paths` is never delegated, so the floor also HOLDS it for the owner. One table, derived from the matrix list;
    the real floor and the real clear refusal are driven; the closure rule (modules and functions the clear merely loads) is NOT part of this (#940)."""

    def setUp(self):
        self.floor = sb.load_module("impact_floor")
        self.clear = sb.load_module("delegated_clear")
        self.matrix = self.floor.load_matrix()
        self.rule = self.matrix["owner_hold"]["delegated_clear"]
        self.listed = list(self.rule["clear_code_paths"])

    def decide(self, paths, matrix=None):
        matrix = matrix or self.matrix
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        return self.floor.owner_decide(matrix, paths, [], None, self.floor.floor_paths(paths, globs, exclude, force))

    @staticmethod
    def concrete(entry):
        return entry.replace("**", "sub/x.txt").replace("*", "x")

    def without(self, drop):
        matrix = json.loads(json.dumps(self.matrix))
        if drop is None:
            del matrix["owner_hold"]["delegated_clear"]
        else:
            matrix["owner_hold"]["delegated_clear"]["clear_code_paths"] = [e for e in self.listed if e != drop] or ["nothing/at/all"]
        return matrix

    def rows(self):
        return [(entry, self.concrete(entry)) for entry in self.listed]

    def reason_of(self, path, matrix=None):
        decision, reasons = self.decide([path], matrix)
        return decision, [r for r in reasons if r.startswith("clear_code_paths")]

    def test_s5_every_entry_of_the_list_is_held_with_the_reason_that_names_the_list(self):
        rows = self.rows()
        self.assertEqual(len(rows), len(self.listed))
        self.assertGreaterEqual(len(rows), 14)
        for entry, path in rows:
            decision, why = self.reason_of(path)
            self.assertEqual(decision, "hold", entry)
            self.assertEqual(len(why), 1, "%s: the reason names the list and the path" % entry)
            self.assertIn(path, why[0])

    def test_s5_a_mutant_that_drops_the_list_or_one_entry_dies(self):
        for entry, path in self.rows():
            decision, why = self.reason_of(path, self.without(entry))
            self.assertEqual(why, [], "dropping %s from the list must remove its reason" % entry)
        for entry, path in self.rows():
            with mock.patch.object(self.floor, "clear_code_paths", lambda matrix: (None, None)):
                self.assertEqual(self.reason_of(path)[1], [], entry)

    def test_s5_the_table_is_complete_for_a_new_entry(self):
        matrix = json.loads(json.dumps(self.matrix))
        matrix["owner_hold"]["delegated_clear"]["clear_code_paths"].append("brand/new/**")
        self.assertEqual(self.reason_of("brand/new/a.py", matrix)[0], "hold")   # held with no code edit: the list is the only source
        self.assertNotEqual(self.decide(["brand/new/a.py"])[0], "hold")
        self.assertEqual(len(self.rows()), len(self.rule["clear_code_paths"]), "a row per entry, derived, never hand-listed")

    def test_s2_a_rename_source_and_the_spellings_the_floor_normalises_are_held(self):
        self.assertEqual(self.reason_of("bin/plan_check.py")[0], "hold")
        for spelled in ("./bin/plan_check.py", "bin//plan_check.py", "BIN/PLAN_CHECK.PY"):
            self.assertEqual(self.reason_of(spelled)[0], "hold", spelled)
        self.assertEqual(self.decide(["bin/other_new_name.py", "bin/repo_exclusion.py"])[0], "hold", "a rename's old path is among the paths")

    def test_s2b_a_closure_module_that_is_not_on_the_list_is_decided_as_before(self):
        outside = [e for e in self.rule["closure_files"] if not self.floor.matches(e, self.listed)]
        self.assertTrue(outside)
        before = self.without(None)
        for path in outside:
            self.assertEqual(self.decide([path]), self.decide([path], before), path)
            self.assertEqual(self.reason_of(path)[1], [], path)

    def test_s3_a_pull_request_that_touches_no_listed_path_is_decided_as_before(self):
        before = self.without(None)
        for path in ("docs/notes.md", "README.md", "bin/fleet_health.py", "bin/decision_sync.py", "bin/triage_digest.py", "bin/fleet-switchboard", "cloud/launcher/app_token.py",
                     "agents/x/y.md", "skills/a/SKILL.md", "bin/test-switchboard", "docs/switchboard.md"):
            self.assertEqual(self.decide([path]), self.decide([path], before), path)

    def test_the_three_entries_the_floor_did_not_hold_before_are_held_now(self):
        before = self.without(None)
        for path in ("bin/plan_check.py", "bin/repo_exclusion.py", "templates/com.example.plist"):
            self.assertNotEqual(self.decide([path], before)[0], "hold", path)
            self.assertEqual(self.decide([path])[0], "hold", path)

    def test_a_malformed_list_holds_and_an_absent_block_does_not_widen_the_rule(self):
        for bad in ("x", [], [1], [""]):
            matrix = json.loads(json.dumps(self.matrix))
            matrix["owner_hold"]["delegated_clear"]["clear_code_paths"] = bad
            decision, reasons = self.decide(["docs/notes.md"], matrix)
            self.assertEqual(decision, "hold", bad)
            self.assertIn("clear_code_paths", reasons[0])

    def test_s4_the_clear_still_refuses_every_entry_with_its_current_reason(self):
        report = {"owner_decision": "hold", "complete": True, "unknown": None, "renamed_no_source": False, "top_tier_hits": {}, "review_action": "none", "review_class": None, "changed_paths": [], "registrar_reasons": []}
        for entry, path in self.rows():
            files = [{"filename": path, "previous_filename": None, "status": "modified"}]
            got = self.clear.refusals(self.rule, report, files, self.floor.matches, {}, {"reasons": [], "level": "Low"})
            self.assertEqual(got, ["it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path], entry)

    def test_s7_one_reader_serves_the_floor_and_the_clear_and_the_rule_names_no_path(self):
        self.assertEqual(self.floor.clear_code_paths(self.matrix), (self.listed, None))
        matrix = json.loads(json.dumps(self.matrix))
        matrix["owner_hold"]["delegated_clear"]["clear_code_paths"].append("brand/new/**")   # one entry added to the matrix: the floor holds it AND the clear refuses it
        self.assertEqual(self.reason_of("brand/new/a.py", matrix)[0], "hold")
        listed, problem = self.floor.clear_code_paths(matrix)
        self.assertIsNone(problem)
        report = {"owner_decision": "hold", "complete": True, "unknown": None, "renamed_no_source": False, "top_tier_hits": {}, "review_action": "none", "review_class": None, "changed_paths": [], "registrar_reasons": []}
        refused = self.clear.refusals(dict(matrix["owner_hold"]["delegated_clear"], clear_code_paths=listed), report,
                                      [{"filename": "brand/new/a.py", "previous_filename": None, "status": "modified"}], self.floor.matches, {}, {"reasons": [], "level": "Low"})
        self.assertEqual(len(refused), 1, refused)
        import ast
        tree = ast.parse((BIN / "impact_floor.py").read_text())
        constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(sorted(constants & set(self.listed)), [], "a listed path is written in the floor's code")

    def test_s6_the_docs_and_the_matrix_text_say_always_held_and_where_it_stops(self):
        docs = (BIN.parent / "docs" / "switchboard.md").read_text()
        meaning = self.rule["meaning"]
        for text in (docs, meaning):
            self.assertIn("always held", text.lower())
            self.assertIn("clear_code_paths", text)
            self.assertIn("closure", text)
            self.assertIn("#940", text)


class TestTriageStandingLine(unittest.TestCase):
    """#1349: a standing line with a date (or "awaiting batch") keeps T3 and T12 quiet until the date, through the one reader `standing_line`."""
    NOW = sb.parse_iso("2026-10-26T12:00:00Z")
    SLA = {"t1_days": 3, "t2_days": 3, "t3_ask_days": 2, "t3_other_days": 7, "t8_days": 5, "t9_days": 7, "t11_days": 30}
    CTX = {"chain": set(), "owned": set(), "unresolved": set(), "charters": set(), "asks": {10}}
    LINK = {130: {"pr": 1325, "url": "https://example.test/pull/1325", "merged_at": "2026-10-25T00:00:00Z", "by": "bot"}}
    # the line forms seen on two real issues, rewritten neutral
    LINE_BATCH = ("Standing line (26 Oct 07:55Z): the change is in batch 6 (pull request #1325: ready, waiting for the owner's clear). "
                  "It closes when that batch reaches the default branch, then the live line follows.")
    LINE_DATED = "Standing line (26 Oct 07:51Z): next check 28 Oct, the plan waits for the review of #1300."
    NEXT_STEP = "Next step (triage, 25 Oct): keep with the lane until 2026-10-30."

    def comment(self, body, at="2026-10-26T07:55:00Z"):
        return sb.triage_comment_facts([{"created_at": at, "user": {"login": "bot"}, "body": body}])

    def judge(self, number, comments, now=None, waiting=None):
        item = sb.triage_norm(tri_issue(self.NOW, number, created=20, updated=10, assignees=["bot"]))
        found, _ours = sb.triage_rules(item, comments, self.CTX, self.SLA, self.NOW if now is None else now, ["bot"], [], set(), merged=self.LINK, waiting=waiting)
        return found

    def test_the_reader_reads_the_real_shapes(self):
        posted = sb.parse_iso("2026-10-26T07:55:00Z")
        self.assertEqual(sb.standing_line(self.LINE_BATCH, posted), ("batch", None))
        self.assertEqual(sb.standing_line(self.LINE_DATED, posted), ("date", sb.parse_iso("2026-10-29T00:00:00Z")))
        self.assertEqual(sb.standing_line(self.NEXT_STEP, posted), ("date", sb.parse_iso("2026-10-31T00:00:00Z")))
        self.assertEqual(sb.standing_line("Next: do it", posted), ("none", None))
        self.assertEqual(sb.standing_line("Standing line (x): until 31 Feb", posted), ("none", None))
        self.assertIsNone(sb.standing_line("the change is in batch 6, awaiting batch", posted))   # only a heading line counts

    def test_the_first_date_not_past_decides_and_a_past_year_less_date_never_rolls_forward(self):
        at = sb.parse_iso("2026-10-26T07:55:00Z")
        day = lambda text: sb.standing_line(text, at)
        self.assertEqual(day("Standing line (26 Oct 07:55Z): merged 24 Oct, close by 29 Oct."), ("date", sb.parse_iso("2026-10-30T00:00:00Z")))
        self.assertEqual(day("Next step (triage, 25 Oct): keep with the lane. started 3 Oct ... Date: 28 Oct"), ("date", sb.parse_iso("2026-10-29T00:00:00Z")))
        self.assertEqual(day("Standing line (26 Oct 07:55Z): merged 24 Oct, closed 3 Oct."), ("none", None))   # all past: not next year
        self.assertEqual(day("Standing line (26 Oct 07:55Z): merged 24 Oct 2026, close by 2026-10-27."), ("date", sb.parse_iso("2026-10-28T00:00:00Z")))
        self.assertEqual(day("Standing line (26 Oct 07:55Z): next check 26 Oct."), ("date", sb.parse_iso("2026-10-27T00:00:00Z")))   # the comment's own day is not past
        self.assertEqual(day("Standing line (26 Oct 07:55Z): next check 30 Dec."), ("none", None))   # a year-less date over 60 days ahead
        december = sb.parse_iso("2026-12-28T10:00:00Z")   # year end: a January date is next year's
        self.assertEqual(sb.standing_line("Standing line (28 Dec): check 5 Jan.", december), ("date", sb.parse_iso("2027-01-06T00:00:00Z")))
        self.assertEqual(sb.standing_line("Standing line (28 Dec): check 3 Dec.", december), ("none", None))
        self.assertEqual(sb.standing_line("Standing line (28 Dec): check 5 Apr.", december), ("none", None))

    def test_the_real_lines_of_the_day_with_their_past_dates(self):
        batch = ("Standing line (26 Oct 07:55Z): the change is in batch 6 (pull request #1325: ready). It is merged 24 Oct and the live check follows, close by 28 Oct. "
                 "No further triage lines until then.")
        step = "Next step (triage, 25 Oct): keep with the lane while the plan is reviewed. Date: 28 Oct"
        posted = sb.parse_iso("2026-10-26T07:55:00Z")
        self.assertEqual(sb.standing_line(batch, posted), ("date", sb.parse_iso("2026-10-29T00:00:00Z")))
        self.assertEqual(sb.standing_line(step, sb.parse_iso("2026-10-25T09:00:00Z")), ("date", sb.parse_iso("2026-10-29T00:00:00Z")))
        for text in (batch, step):
            self.assertNotIn("T12", self.judge(130, self.comment(text, at="2026-10-26T07:55:00Z")))

    def test_a_quoted_old_line_is_not_read(self):
        self.assertIn("T12", self.judge(130, self.comment("> Standing line (20 Oct): check 28 Oct.\n\nnoted")))

    def test_only_a_fleet_login_or_the_issue_author_silences(self):
        def facts(user):
            return sb.triage_comment_facts([{"created_at": "2026-10-26T07:55:00Z", "user": {"login": user}, "body": self.LINE_DATED}])
        self.assertEqual(facts("Bot")[0]["author"], "bot")
        self.assertNotIn("T12", self.judge(130, facts("bot")))
        self.assertIn("T12", self.judge(130, facts("stranger")))
        self.assertIn("T3", self.judge(10, facts("stranger")))
        self.assertIn("T12", self.judge(130, sb.triage_comment_facts([{"created_at": "2026-10-26T07:55:00Z", "body": self.LINE_DATED}])))   # no author read: not trusted

    def test_an_unreadable_author_on_both_sides_does_not_silence(self):
        item = {"author": "", "standing": None}
        c = [{"author": "", "standing": ("date", sb.parse_iso("2026-10-29T00:00:00Z")), "at": sb.parse_iso("2026-10-26T07:55:00Z")}]
        self.assertFalse(sb.triage_standing_silent(item, c, self.NOW, trusted=frozenset({"bot"})))
        item["author"] = "writer"
        c[0]["author"] = "writer"
        self.assertTrue(sb.triage_standing_silent(item, c, self.NOW, trusted=frozenset({"bot"})))

    def test_s2_the_batch_shape_before_the_date_silences_t12(self):
        self.assertNotIn("T12", self.judge(130, self.comment(self.LINE_BATCH)))

    def test_s2_a_text_with_no_heading_is_not_read(self):
        self.assertIn("T12", self.judge(130, self.comment("the change is in batch 6, awaiting batch")))

    def test_s3_the_dated_shape_before_the_date_silences_t12(self):
        self.assertNotIn("T12", self.judge(130, self.comment(self.LINE_DATED)))
        self.assertNotIn("T12", self.judge(130, self.comment(self.NEXT_STEP)))

    def test_s4_the_ask_with_only_a_dated_line_is_not_t3(self):
        self.assertNotIn("T3", self.judge(10, self.comment(self.LINE_DATED)))
        self.assertIn("T3", self.judge(10, []))   # as before: no line, T3

    def test_s5_awaiting_batch_with_no_date_silences_both_until_a_newer_comment(self):
        line = "Standing line (26 Oct 08:00Z): awaiting batch 6."
        for number in (10, 130):
            self.assertEqual([r for r in self.judge(number, self.comment(line)) if r in ("T3", "T12")], [])
        later = self.comment(line) + self.comment("any other note", at="2026-10-26T09:00:00Z")
        self.assertIn("T3", self.judge(10, later))
        self.assertIn("T12", self.judge(130, later))
        self.assertIn("T12", self.judge(130, self.comment(line), waiting=set()))   # the batch reached the default branch

    def test_s6_a_date_years_ahead_or_impossible_silences_nothing(self):
        for date in ("2030-01-01", "31 Feb 2026", "2026-13-40"):
            comments = self.comment("Standing line (26 Oct 08:00Z): next check %s." % date)
            self.assertIn("T3", self.judge(10, comments), date)
            self.assertIn("T12", self.judge(130, comments), date)

    def test_s7_one_reader_serves_t3_and_t12(self):
        source = (REPO / "bin" / "switchboard" / "triage.py").read_text()
        self.assertEqual(len(re.findall(r"^def standing_line\(", source, re.M)), 1)
        self.assertEqual(len(re.findall(r"TRIAGE_STANDING_RE\.", source)), 1)   # the heading regex is applied in one place
        self.assertEqual(len(re.findall(r"(?<!def )triage_standing_silent\(item, comments, now", source)), 2)

    def test_s8_s9_after_the_date_both_fire_again_and_the_last_second_is_silent(self):
        comments = self.comment(self.LINE_DATED)   # names 28 Oct: silent through 28 Oct 23:59:59Z
        for rule, number in (("T3", 10), ("T12", 130)):
            self.assertNotIn(rule, self.judge(number, comments, now=sb.parse_iso("2026-10-28T23:59:59Z")))
            self.assertIn(rule, self.judge(number, comments, now=sb.parse_iso("2026-10-29T00:00:00Z")))
            self.assertIn(rule, self.judge(number, comments, now=sb.parse_iso("2026-10-30T12:00:00Z")))

    def test_u2_the_existing_next_forms_keep_their_effect(self):
        for body in ("Next: do it", "Next steps: do it", "- [ ] a task"):
            self.assertNotIn("T3", self.judge(10, self.comment(body)))

    def test_the_latest_line_wins(self):
        comments = self.comment(self.LINE_DATED) + self.comment("Standing line (27 Oct 09:00Z): nothing to say", at="2026-10-27T09:00:00Z")
        self.assertIn("T3", self.judge(10, comments))


class TestConfig(Base):
    def test_missing_file_means_every_default(self):
        self.assertFalse(self.config_file.exists())
        self.assertEqual(sb.load_config(), {
            "opencode": None, "opencode_env": {}, "opencode_executable": None, "launch_env": {},
            "herdr": "herdr", "batch_seconds": 90, "engaged_minutes": 10, "retire_idle_minutes": 10, "call_timeout_seconds": 20, "clear_remind_minutes": 15, "send_timeout_seconds": 60, "poll_seconds": 5,
            "herdr_log": "~/.config/herdr/herdr-server.log", "pane_exit_burst_seconds": 5, "pane_exit_burst_min": 3,  # #1030
            "worker_lookback_hours": 24, "stalled_working_hours": 2, "review_window_minutes": 60, "send_to_working": "steer", "steer_after_minutes": 0, "launch_default": "headless",
            "rider_max_age_seconds": 3600, "undelivered_wake_minutes": 15, "undelivered_wake_tell_minutes": None, "undelivered_wake_per_pass": 2, "report_triage": sb.REPORT_TRIAGE_DEFAULTS,  # PR 13
            "github_repos": [], "gh": sb.own_gh_path("gh"), "gh_by_name": "gh", "github_lookback_hours": 24, "github_port": None, "github_ignore_authors": [],   # #1216: the RESOLVED gh (a machine with a gh on PATH names it by path); `gh_by_name` is the gh as configured (PATH finds the shim), used by the one merge call of `merge-gate --pass --merge`
            "owner_logins": [], "github_bot_logins": [], "v2_light": False, "v2_cache_seconds": 120, "v2_reads_per_pass": 8, "v2_reads_per_minute": 90, "v2_events": "auto", "v2_plugin_events": "auto", "v2_transport": "auto", "cpu_guard": None, "disk_guard": None, "reds": None, "heavy": None, "no_multiplexer": False, "docker_reaper": True,
            "github_events": ["issues", "issue_comment", "sub_issues", "pull_request", "pull_request_review",
                              "check_suite", "workflow_run"],
            "awaiting_live_days": 14, "awaiting_label": None, "decisions_lookback_hours": 72,  # PR 14
            "awaiting_cos_label": None, "decisions_cos_grace_seconds": 300, "cos_orders_file": None,  # PR 18
            "pr_notice_hold_minutes": 0,  # PR notices
            "intent_repo": None, "intent_required": True,
            "gh_users": {}, "github_poll_seconds": 60, "intent_live_silence_minutes": 10,  # PR 15
            "classifier_mode": "shadow", "busy_poll_seconds": 1,  # PR 8
            "policy": sb.POLICY_DEFAULTS,  # PR 9
            **sb.V1_MOVE_DEFAULTS})  # PR 10: pinned in TestImportV1Config

    def test_path_follows_xdg_config_home(self):
        self.assertEqual(sb.config_path(), self.config_file)

    def test_default_path_is_under_home_when_xdg_is_unset_or_relative(self):
        for value in (None, "relative/dir"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = value
                self.assertEqual(sb.config_path(), self.home / ".config/fleet-switchboard/config.json")

    def test_overrides_replace_only_what_they_name(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode", "opencode_env": {"XDG_DATA_HOME": "/tmp/lab"},
                           "batch_seconds": 5, "call_timeout_seconds": 2.5})
        config = sb.load_config()
        self.assertEqual(config["opencode"], "/opt/lab/bin/opencode")
        self.assertEqual(config["opencode_env"], {"XDG_DATA_HOME": "/tmp/lab"})
        self.assertEqual(config["batch_seconds"], 5)
        self.assertEqual(config["call_timeout_seconds"], 2.5)
        self.assertEqual(config["herdr"], "herdr")
        self.assertEqual(config["engaged_minutes"], 10)

    def test_defaults_are_not_shared_between_loads(self):
        sb.load_config()["opencode_env"]["LEAK"] = "1"
        self.assertEqual(sb.load_config()["opencode_env"], {})
        self.assertEqual(sb.DEFAULTS["opencode_env"], {})

    def test_an_unknown_top_level_or_nested_key_is_a_warning_not_a_refusal_and_a_bad_type_still_refuses(self):
        for raw in ({"surprise": 1}, {"policy": {"surprise": 1}}, {"jev": {"surprise": 1}}, {"sweep": {"surprise": 1}}):
            with self.subTest(raw=raw):
                sb._WARNED_CONFIG.clear()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    config = sb.validate_config(raw)
                self.assertIn("warning:", err.getvalue())
                self.assertIn("surprise", err.getvalue())
                self.assertIn("this tool does not read", err.getvalue())
                self.assertIsInstance(config, dict)
        sb._WARNED_CONFIG.clear()
        with self.assertRaises(sb.ConfigError):
            with contextlib.redirect_stderr(io.StringIO()):
                sb.validate_config({"surprise": 1, "batch_seconds": "90"})   # a warning never hides a real error
        with self.assertRaises(sb.ConfigError):
            with contextlib.redirect_stderr(io.StringIO()):
                sb.validate_config({"policy": {"surprise": 1}, "opencode": 7})

    def test_the_same_unknown_key_is_warned_once_per_process(self):
        sb._WARNED_CONFIG.clear()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sb.validate_config({"surprise": 1})
            sb.validate_config({"surprise": 1})
        self.assertEqual(err.getvalue().count("surprise"), 1)

    def test_bad_values_are_refused_with_the_key_named(self):
        cases = [
            ({"opencode": 7}, "'opencode'"),
            ({"opencode": ""}, "'opencode'"),
            ({"opencode": "opencode"}, "'opencode'"),
            ({"opencode": "bin/opencode"}, "'opencode'"),
            ({"herdr": None}, "'herdr'"),
            ({"herdr": ["herdr"]}, "'herdr'"),
            ({"opencode_env": ["A=1"]}, "'opencode_env'"),
            ({"opencode_env": {"A": 1}}, "'opencode_env'"),
            ({"opencode_env": {"BAD NAME": "x"}}, "'opencode_env'"),
            ({"batch_seconds": "90"}, "'batch_seconds'"),
            ({"batch_seconds": True}, "'batch_seconds'"),
            ({"batch_seconds": 0}, "'batch_seconds'"),
            ({"engaged_minutes": -1}, "'engaged_minutes'"),
            ({"engaged_minutes": None}, "'engaged_minutes'"),
            ({"call_timeout_seconds": "20"}, "'call_timeout_seconds'"),
            ({"poll_seconds": 0}, "'poll_seconds'"),
        ]
        for raw, needle in cases:
            with self.subTest(raw=raw):
                self.write_config(raw)
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.load_config()
                self.assertIn(needle, str(caught.exception))

    def test_malformed_files_are_refused(self):
        for text, needle in (("{not json", "not valid JSON"), ("[1, 2]", "JSON object"), ('"x"', "JSON object")):
            with self.subTest(text=text):
                self.write_config(text)
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.load_config()
                self.assertIn(needle, str(caught.exception))


class TestAssembler(unittest.TestCase):
    """The entry builds its one namespace from the manifest bin/fleet-switchboard.parts (docs/switchboard.md, "The assembler")."""

    def _copy(self, lines):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        for sibling in BIN.glob("*.py"):   # the entry loads its sibling modules from its own directory
            shutil.copy(str(sibling), str(root / sibling.name))
        copy_entry(root)   # the shipped parts the copied entry needs
        (root / "fleet-switchboard.parts").write_text("\n".join(lines) + "\n")
        return root

    def test_the_shipped_manifest_names_the_entry_itself(self):
        lines = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.startswith("#")]
        self.assertEqual(lines[0], "fleet-switchboard")

    def test_a_second_part_puts_its_names_into_sb_in_the_order_given(self):
        root = self._copy(["part_a.py", "fleet-switchboard", "switchboard/v2_transport.py", "switchboard/facts.py", "switchboard/judge_request.py", "switchboard/judge.py", "switchboard/import_v1.py", "switchboard/handoff.py", "part_b.py"])
        (root / "part_a.py").write_text('ORDER = ["a"]\nA_SAW_MAIN = "main" in globals()\nA_FILE = __file__\n')
        (root / "part_b.py").write_text('ORDER.append("b")\nB_SAW_MAIN = "main" in globals()\n')
        module = _load("t_assembled", root / "fleet-switchboard")
        self.assertEqual(module.ORDER, ["a", "b"])
        self.assertFalse(module.A_SAW_MAIN)
        self.assertTrue(module.B_SAW_MAIN)
        self.assertEqual(module.A_FILE, str(root / "fleet-switchboard"))
        self.assertTrue(callable(module.main))
        self.assertFalse(hasattr(module, "_fleet_parts"))

    def test_a_missing_part_fails_at_start_with_one_line(self):
        root = self._copy(["part_missing.py", "fleet-switchboard"])
        done = subprocess.run([sys.executable, str(root / "fleet-switchboard"), "status", "--help"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(len(done.stderr.strip().splitlines()), 1)
        self.assertIn("part file not found", done.stderr)
        self.assertIn("part_missing.py", done.stderr)

    def test_an_unreadable_manifest_fails_at_start_with_one_line(self):
        root = self._copy(["fleet-switchboard"])
        manifest = root / "fleet-switchboard.parts"
        manifest.unlink()
        manifest.mkdir()   # exists, cannot be read as a file
        done = subprocess.run([sys.executable, str(root / "fleet-switchboard"), "status", "--help"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1)
        self.assertIn("cannot read the parts manifest", done.stderr)

    def test_a_missing_manifest_runs_the_entry_alone(self):
        text = SWITCHBOARD.read_text()
        block = text[:text.index("\n_fleet_parts()\n") + len("\n_fleet_parts()\n")]   # the assembler's own block, with nothing else
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "entry").write_text(block + "ENTRY_RAN = True\n")
        self.assertTrue(_load("t_no_manifest", root / "entry").ENTRY_RAN)

    def test_a_manifest_that_does_not_name_the_entry_runs_every_part_before_it(self):
        text = SWITCHBOARD.read_text()
        block = text[:text.index("\n_fleet_parts()\n") + len("\n_fleet_parts()\n")]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "entry").write_text(block + "ENTRY_SAW_A = A_RAN\n")
        (root / "entry.parts").write_text("part_a.py\n")
        (root / "part_a.py").write_text("A_RAN = True\n")
        self.assertTrue(_load("t_unnamed_entry", root / "entry").ENTRY_SAW_A)

    def test_comments_and_blank_lines_in_a_manifest_are_ignored(self):
        root = self._copy(["# the parts", "", "part_a.py", "  ", "fleet-switchboard", "switchboard/v2_transport.py", "switchboard/facts.py", "switchboard/judge_request.py", "switchboard/judge.py", "switchboard/import_v1.py", "switchboard/handoff.py"])
        (root / "part_a.py").write_text("FROM_A = 1\n")
        self.assertEqual(_load("t_commented", root / "fleet-switchboard").FROM_A, 1)

    def test_a_manifest_refuses_a_part_outside_the_entrys_directory_or_listed_twice(self):
        cases = (("a parent path", ["../part_a.py", "fleet-switchboard"], "must be relative"),
                 ("a nested parent path", ["sub/../../part_a.py", "fleet-switchboard"], "must be relative"),
                 ("an absolute path", ["/etc/hosts", "fleet-switchboard"], "must be relative"),
                 ("a duplicate", ["part_a.py", "fleet-switchboard", "part_a.py"], "listed twice"),
                 ("a duplicate by another spelling", ["part_a.py", "./part_a.py", "fleet-switchboard"], "listed twice"))
        for label, lines, message in cases:
            with self.subTest(label):
                root = self._copy(lines)
                (root / "part_a.py").write_text("X = 1\n")
                done = subprocess.run([sys.executable, str(root / "fleet-switchboard"), "status", "--help"], capture_output=True, text=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
                self.assertIn(message, done.stderr)

    def test_the_manifest_lists_exactly_the_part_files_in_bin_switchboard(self):
        """No part file is left out of the manifest (it would never run) and none is listed twice."""
        lines = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.startswith("#")]
        parts = [n for n in lines if n != "fleet-switchboard"]
        on_disk = sorted("switchboard/" + p.name for p in (BIN / "switchboard").glob("*.py"))
        self.assertEqual(sorted(parts), on_disk)
        self.assertEqual(len(parts), len(set(parts)))

    def test_the_shipped_manifest_lists_the_entry_once_and_every_part_exists_under_bin(self):
        lines = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.startswith("#")]
        self.assertEqual(lines.count("fleet-switchboard"), 1)
        self.assertEqual(len(lines), len(set(lines)))
        for name in lines:
            self.assertTrue((BIN / name).is_file(), name)
            if name != "fleet-switchboard":
                self.assertLessEqual(len((BIN / name).read_text().splitlines()), 1500, name)

    def test_a_symlinked_run_and_the_help_line_still_work(self):
        link = Path(tempfile.mkdtemp()) / "fsb"
        self.addCleanup(shutil.rmtree, str(link.parent), True)
        link.symlink_to(SWITCHBOARD)
        done = subprocess.run([sys.executable, "-B", str(link), "status", "--help"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: fleet-switchboard status", done.stdout)


class TestBigRegistrarFile(unittest.TestCase):
    """#964 S23: the daemon file is over 1 MB, so the JSON contents API serves no content (encoding "none"); the floor reads it through the raw media type and still holds a registrar change and still fails closed."""

    BASE, HEAD = "a" * 40, "b" * 40

    def setUp(self):
        self.dc = sb.load_module("delegated_clear")
        self.matrix = sb.impact_floor.load_matrix()
        self.rule = self.matrix["owner_hold"]["delegated_clear"]
        self.src = (BIN / "fleet-switchboard").read_text()
        self.calls, self.raw_fails = [], False

    def engine(self, heads):
        def runner(argv, **kw):
            self.calls.append(list(argv))
            path = argv[-1]
            ref = path.rsplit("ref=", 1)[1]
            if "Accept: application/vnd.github.raw" in argv:
                if self.raw_fails:
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 500")
                return subprocess.CompletedProcess(argv, 0, heads[ref], "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "file", "encoding": "none", "content": "", "size": 1589963}), "")
        reader = sb.IntentReader("gh", "acme/app", runner=runner)
        return types.SimpleNamespace(intents=reader)

    def states(self, head_src, **kw):
        engine = self.engine({self.BASE: self.src, self.HEAD: head_src})
        return sb.registrar_states(engine, "acme/app", self.matrix, ["bin/fleet-switchboard"], self.BASE, self.HEAD)

    def test_an_ordinary_edit_of_the_big_file_is_not_held_and_costs_four_calls(self):
        got = self.states(self.src + "\n\ndef helper_zzz():\n    return 1\n")
        self.assertIsNone(got["why"], got)
        self.assertEqual(got["findings"], [])
        self.assertEqual(len(self.calls), 4, "a JSON read and a raw read for the base and for the head")

    def test_a_registrar_change_in_the_big_file_is_held(self):
        got = self.states(self.src + "\n\ndef add_zzz_parsers(sub):\n    import os\n    os.system('x')\n")
        self.assertIsNone(got["why"], got)
        self.assertTrue(got["findings"], got)

    def test_a_read_that_really_fails_still_holds_with_the_same_reason(self):
        self.raw_fails = True
        got = self.states(self.src)
        self.assertIn("bin/fleet-switchboard is not a readable file at aaaaaaaa", got["why"])

    def test_a_file_over_the_ceiling_is_refused_unread(self):
        reader = sb.IntentReader("gh", "acme/app", runner=lambda argv, **kw: self.calls.append(argv) or subprocess.CompletedProcess(argv, 0, json.dumps({"type": "file", "encoding": "none", "content": "", "size": 6 * 1024 * 1024}), ""))
        with self.assertRaises(sb.IntentError) as raised:
            sb._file_text_at(types.SimpleNamespace(intents=reader), "acme/app", "bin/x", self.BASE)
        self.assertIn("is not a readable file at aaaaaaaa", str(raised.exception))
        self.assertEqual(len(self.calls), 1)

    def test_one_raw_read_for_each_path_and_ref(self):
        engine = self.engine({self.BASE: self.src, self.HEAD: self.src})
        for _ in range(3):
            sb._file_text_at(engine, "acme/app", "bin/fleet-switchboard", self.BASE)
        self.assertEqual(sum(1 for c in self.calls if "-H" in c), 1)

    def test_a_mutant_that_reads_only_the_json_contents_fails_these_tests(self):
        with mock.patch.object(sb.IntentReader, "_api_raw_text", side_effect=sb.IntentError("gh", "mutant: no raw read")):
            got = self.states(self.src)
        self.assertIsNotNone(got["why"], "with the raw read gone the big file is unreadable (the defect the tests above catch)")

    def test_a_big_file_that_is_not_utf8_holds_with_the_unreadable_text_for_both_callers(self):
        """A mutant that does not convert the decode error lets UnicodeDecodeError escape agent_block_states: this test fails it."""
        engine = self.engine({self.BASE: "x", self.HEAD: "x"})
        bad = b"\xff\xfe" + b"a" * 100
        orig = engine.intents.runner
        def runner(argv, **kw):
            if "Accept: application/vnd.github.raw" in argv:
                return subprocess.CompletedProcess(argv, 0, bad, "")
            return orig(argv, **kw)
        engine.intents.runner = runner
        got = sb.registrar_states(engine, "acme/app", self.matrix, ["bin/fleet-switchboard"], self.BASE, self.HEAD)
        self.assertIn("is not text at aaaaaaaa", got["why"])
        states = sb.agent_block_states(engine, "acme/app", self.matrix, ["agents/x.md"], self.BASE, self.HEAD)
        self.assertEqual(states.get("agents/x.md"), "unreadable", states)

    def test_a_size_zero_file_makes_no_raw_call(self):
        """A mutant that goes raw for size 0 makes a second call: this test fails it."""
        reader = sb.IntentReader("gh", "acme/app", runner=lambda argv, **kw: self.calls.append(argv) or subprocess.CompletedProcess(argv, 0, json.dumps({"type": "file", "encoding": "base64", "content": "", "size": 0}), ""))
        self.assertEqual(sb._file_text_at(types.SimpleNamespace(intents=reader), "acme/app", "bin/x", self.BASE), "")
        self.assertEqual(len(self.calls), 1, "one JSON read and no raw read")

    def test_a_raw_read_through_the_accounts_path_is_counted_and_not_conditional(self):
        accounts = sb.GhAccounts({"gh": "gh", "call_timeout_seconds": 5, "gh_users": {}}, lambda *a, **k: None, lambda: 1.0, environ={})
        accounts.conditional = True
        seen = []
        def runner(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "raw body", "")
        done = accounts.read(runner, "repos/acme/app/contents/x?ref=" + self.BASE, "acme/app", 5, extra_flags=["-H", "Accept: application/vnd.github.raw"])
        self.assertEqual(done.stdout, "raw body")
        self.assertEqual(seen, [["gh", "api", "-H", "Accept: application/vnd.github.raw", "repos/acme/app/contents/x?ref=" + self.BASE]])
        self.assertEqual(len(accounts.calls), 1, "the read is counted")

    def _failed_raw_read(self, stderr):
        """A raw read through accounts.read whose runner is honest: it must be asked for text=False and answers BYTES."""
        now = [1000.0]
        accounts = sb.GhAccounts({"gh": "gh", "call_timeout_seconds": 5, "gh_users": {}}, lambda *a, **k: None, lambda: now[0], environ={})
        asked = []
        def runner(argv, **kw):
            asked.append(kw.get("text"))
            return subprocess.CompletedProcess(argv, 1, b"", stderr)
        reader = sb.IntentReader("gh", "acme/app", runner=runner, accounts=accounts)
        with self.assertRaises(sb.IntentError) as raised:
            reader._api_raw_text("repos/acme/app/contents/x?ref=" + self.BASE, repo="acme/app")
        self.assertEqual(asked, [False], "the runner was asked for bytes")
        return raised.exception, accounts

    def test_a_failed_raw_read_with_bytes_stderr_is_an_intent_error_not_a_type_error(self):
        """A mutant that drops the decode before _says_rate_limited raises TypeError (bytes with a str pattern): these three tests fail it."""
        err, accounts = self._failed_raw_read(b"gh: Not Found (HTTP 404)")
        self.assertIn("404", str(err))
        self.assertEqual(accounts.paused, {})

    def test_a_failed_raw_read_that_says_rate_limit_in_bytes_pauses_the_account(self):
        err, accounts = self._failed_raw_read(b"gh: API rate limit exceeded for user ID 1 (HTTP 403)")
        self.assertIn("rate limit", str(err))
        self.assertTrue(accounts.paused, "the pause is recorded")

    def test_a_failed_raw_read_with_non_utf8_stderr_is_an_intent_error(self):
        err, accounts = self._failed_raw_read(b"gh: bad \xff\xfe bytes (HTTP 500)")
        self.assertIn("500", str(err))
        self.assertEqual(accounts.paused, {})

    def test_the_ceiling_counts_bytes_not_characters(self):
        """3 MiB of a 2-byte character is 3 Mi characters but 6 MiB: over the ceiling. A mutant that counts characters fails this test."""
        body = ("\u00e9" * (3 * 1024 * 1024)).encode("utf-8")
        engine = self.engine({self.BASE: ""})
        orig = engine.intents.runner
        engine.intents.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, body, "") if "Accept: application/vnd.github.raw" in argv else orig(argv, **kw)
        with self.assertRaises(sb.IntentError) as raised:
            sb._file_text_at(engine, "acme/app", "bin/fleet-switchboard", self.BASE)
        self.assertIn("is not a readable file at aaaaaaaa", str(raised.exception))


class TestStateDir(Base):
    def test_created_private_under_xdg_state_home_whatever_the_umask(self):
        # 0o277 strips owner bits from mkdir's mode, so only an explicit chmod
        # yields exactly 0700; 0 would let a looser mode through if one were asked for.
        for umask in (0, 0o022, 0o277):
            with self.subTest(umask=oct(umask)):
                if self.state_dir.exists():
                    self.state_dir.rmdir()
                self.state_dir.parent.mkdir(parents=True, exist_ok=True)
                old = os.umask(umask)
                try:
                    path = sb.ensure_state_dir()
                finally:
                    os.umask(old)
                self.assertEqual(path, self.state_dir)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_missing_parents_are_created_too(self):
        self.assertFalse(self.state_dir.parent.exists())
        self.assertEqual(stat.S_IMODE(sb.ensure_state_dir().stat().st_mode), 0o700)

    def test_default_is_under_home_when_xdg_is_unset(self):
        os.environ.pop("XDG_STATE_HOME", None)
        self.assertEqual(sb.state_dir_path(), self.home / ".local/state/fleet-switchboard")

    def test_existing_private_dir_is_accepted(self):
        self.state_dir.mkdir(parents=True, mode=0o700)
        os.chmod(str(self.state_dir), 0o700)
        self.assertEqual(sb.ensure_state_dir(), self.state_dir)

    def test_a_directory_made_by_another_process_between_the_look_and_the_mkdir_is_accepted(self):
        """#688: three `heavy` wrappers started together all saw no directory; the one that lost the mkdir got EEXIST and ran its command with NO slot."""
        self.state_dir.mkdir(parents=True, mode=0o700)
        os.chmod(str(self.state_dir), 0o700)
        real = sb.Path.exists
        sb.Path.exists = lambda self_: False if self_ == self.state_dir else real(self_)   # the look said "not there": the other process made it just after
        try:
            self.assertEqual(sb.ensure_state_dir(), self.state_dir)
        finally:
            sb.Path.exists = real

    def test_a_directory_made_in_that_gap_is_still_refused_when_it_is_not_private(self):
        self.state_dir.mkdir(parents=True)
        os.chmod(str(self.state_dir), 0o755)
        real = sb.Path.exists
        sb.Path.exists = lambda self_: False if self_ == self.state_dir else real(self_)
        try:
            with self.assertRaises(sb.ConfigError):
                sb.ensure_state_dir()
        finally:
            sb.Path.exists = real
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o755)

    def test_group_or_world_readable_dir_is_refused_not_repaired(self):
        for mode in (0o750, 0o705, 0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                self.state_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(str(self.state_dir), mode)
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.ensure_state_dir()
                self.assertIn("private", str(caught.exception))
                self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), mode)

    def test_a_file_in_its_place_is_refused(self):
        self.state_dir.parent.mkdir(parents=True)
        self.state_dir.write_text("")
        os.chmod(str(self.state_dir), 0o600)
        with self.assertRaises(sb.ConfigError):
            sb.ensure_state_dir()


class TestScanDriftGuard(unittest.TestCase):
    """#1314: bin/record_json.py copies the cloud_worker depth scan; both scans must give the SAME RESULT on the same inputs."""
    BATTERY = {
        "nested_strings": '{"a": "[[[{{{", "b": ["x", "{"]}',
        "escaped_quote": '{"a": "q\\"' + "[" * 70 + '", "b": [1]}',
        "escaped_backslash_deep": '"\\\\"' + "[" * 70,
        "escaped_backslash": '{"a": "path\\\\", "b": [[[1]]]}',
        "escaped_backslash_then_quote": '["\\\\"][[[[',
        "brackets_in_string": '"' + "[" * 200 + '"',
        "unbalanced_closers": "]]]}}}" + "[" * 64,
        "unbalanced_closers_then_deep": "}" * 10 + "[" * 70,
        "unbalanced_openers": "[" * 30,
        "unbalanced_openers_over": "{" * 100,
        "exactly_64": "[" * 64 + "]" * 64,
        "exactly_65": "[" * 65 + "]" * 65,
        "exactly_64_objects": '{"a":' * 64 + "1" + "}" * 64,
        "exactly_65_objects": '{"a":' * 65 + "1" + "}" * 65,
        "deep_30000": "[" * 30000 + "]" * 30000,
        "all_bracket_64k": "[" * 65536,
        "all_bracket_64k_mixed": "[{" * 32768,
        "empty": "",
        "whitespace": " \t\r\n  ",
        "scalar": "42",
        "scalar_string": '"x"',
        "object_in_arrays": "[" * 40 + '{"a":' * 30 + "1" + "}" * 30 + "]" * 40,
        "arrays_in_objects": '{"a":' * 40 + "[" * 30 + "]" * 30 + "}" * 40,
        "object_in_arrays_over": "[" * 40 + '{"a":' * 30 + "1" + "}" * 30 + "]" * 40 + "[" * 30,
        "non_ascii": '{"k\u00e9y": "\u2603[[[\U0001F600", "z": ["\u00fc"]}',
        "non_ascii_deep": "\u00e9" + "[" * 65,
    }

    @staticmethod
    def scans(record_src=None):
        rj = sb.load_module("record_json")
        cw = sb.load_module("cloud_worker")
        return rj.nesting_too_deep, cw.nesting_too_deep

    def compare(self, a, b):
        return {n: (a(t), b(t)) for n, t in self.BATTERY.items() if a(t) != b(t)}

    def test_both_scans_agree_on_the_battery(self):
        a, b = self.scans()
        for name, text in self.BATTERY.items():
            with self.subTest(name):
                self.assertEqual(a(text), b(text), name)

    def test_battery_expectations(self):
        a, b = self.scans()
        for name, want in (("exactly_64", False), ("exactly_65", True), ("deep_30000", True), ("empty", False),
                           ("whitespace", False), ("scalar", False), ("unbalanced_closers", False), ("brackets_in_string", False)):
            self.assertIs(a(self.BATTERY[name]), want, name)
            self.assertIs(b(self.BATTERY[name]), want, name)

    def test_mutants_make_the_guard_fail(self):
        src = (REPO / "bin" / "record_json.py").read_text()
        _, b = self.scans()
        for old, new in (('elif ch == "[" or ch == "{":', 'elif ch == "[":'),
                         ('elif ch == "\\\\":\n                escaped = True', 'elif False:\n                escaped = True'),
                         ('elif ch == "]" or ch == "}":\n            depth -= 1', 'elif False:\n            depth -= 1')):
            self.assertIn(old, src)
            ns = {}
            exec(compile(src.replace(old, new), "mutant", "exec"), ns)
            self.assertTrue(self.compare(ns["nesting_too_deep"], b), old)


class TestOwnerClearModule(unittest.TestCase):
    """Ask #532, slice 3: the code that decides who may clear, hold or release lives in bin/impact_hold.py (listed in the matrix), not in the daemon file. The names come from the
    module's own MOVED tuple and the paths from the matrix: this test has no list of its own."""

    def setUp(self):
        self.floor = sb.load_module("impact_floor")
        self.matrix = self.floor.load_matrix()
        self.module = sb.load_module("impact_hold")
        self.daemon = BIN / "fleet-switchboard"

    @staticmethod
    def defined(source):
        import ast
        return {n.name for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_the_moved_functions_are_defined_in_the_module_and_found_in_the_daemon_under_their_old_names(self):
        self.assertGreaterEqual(len(self.module.MOVED), 8)
        self.assertEqual(self.defined(Path(self.module.__file__).read_text()) & set(self.module.MOVED), set(self.module.MOVED))
        for name in self.module.MOVED:
            self.assertTrue(callable(getattr(sb, name)), name)
            self.assertIs(getattr(sb, name).__globals__, vars(sb), name + " runs in the daemon's own globals, as before the move")

    def test_s5_s7_none_of_the_moved_functions_is_defined_in_the_daemon_file_and_one_registration_names_the_module(self):
        source = switchboard_source()   # the entry and every part: a moved function defined in any of them is named
        self.assertEqual(self.defined(source) & set(self.module.MOVED), set())
        self.assertEqual(source.count('load_module("impact_hold").bind('), 1)

    def test_s7_a_moved_function_copied_back_into_the_daemon_is_named(self):
        name = self.module.MOVED[0]
        copied = switchboard_source() + "\n\ndef %s(*args):\n    return None\n" % name
        self.assertEqual(sorted(self.defined(copied) & set(self.module.MOVED)), [name])

    def test_s3_no_code_outside_the_matrix_names_a_top_tier_path_as_a_literal(self):
        import ast
        listed = {g for globs in self.matrix["owner_hold"]["top_tier_paths"].values() for g in globs}
        files = sorted(BIN.glob("*.py")) + list(SWITCHBOARD_SOURCE_FILES)   # the entry and every part, from the one list
        hits = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in listed:
                    hits.append("%s: %s" % (path.name, node.value))
        self.assertEqual(hits, [])

    def test_s6_a_change_to_the_module_holds_for_the_owner_and_naming_it_a_change_to_another_daemon_function_does_not(self):
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        floor_hit = lambda paths: self.floor.floor_paths(paths, globs, exclude, force)
        moved = self.floor.owner_decide(self.matrix, ["bin/impact_hold.py"], [], None, floor_hit(["bin/impact_hold.py"]))
        other = self.floor.owner_decide(self.matrix, ["bin/fleet-switchboard"], [], None, floor_hit(["bin/fleet-switchboard"]))
        self.assertEqual(moved[0], "hold")
        self.assertTrue(any("bin/impact_hold.py" in r for r in moved[1]), moved)
        self.assertNotEqual(other[0], "hold", other)

    def test_s8_without_the_module_the_daemon_refuses_loudly_and_names_it(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            for path in list(BIN.glob("*.py")) + [self.daemon]:
                if path.name != "impact_hold.py":
                    shutil.copy(path, Path(tmp) / path.name)
            done = subprocess.run([sys.executable, str(Path(tmp) / "fleet-switchboard"), "impact", "clear", "acme/api#1", "--reason", "x"], capture_output=True, text=True, timeout=60,
                                  env=dict(os.environ, FLEET_SWITCHBOARD_STATE_DIR=tmp))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("impact_hold", done.stderr)


class TestBuilderChecklist(unittest.TestCase):
    """#1283: the coordination skill carries the ten builder checklist items and the brief template names the checklist."""
    ITEMS = {
        "a": "every input and every outside call of the new code; each row names its test and its run-by-hand mutant",
        "b": "uses the one shared safe reader",
        "c": "derives its list from the code's syntax tree",
        "d": "is in the owner format from the first push",
        "e": "opens its pull request as a draft at its first push",
        "f": "pushes the merge commit the minute it exists",
        "g": "states its closed rule and its known-limits list in its first version",
        "h": "is built by a local job, never a cloud pod",
        "i": "Compare failure messages, not only test names",
        "j": "say which git version the tests ran on",
    }
    RULES = {
        "b": "(opens without blocking, refuses links, accepts only regular files, bounds the count, the size and the time)",
        "i": "and keep new strings and comments clear of the added-lines scans",
        "e": "the pod's git token expires after about an hour and a late open fails",
    }
    BRIEF = "Every builder brief carries the builder checklist below by default; a brief that leaves an item out says why in one line."

    def text(self):
        return " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())

    def problems(self, text):
        found = [k for k, v in self.ITEMS.items() if text.count("- (%s) " % k) != 1 or v not in text]
        found += ["rule " + k for k, v in self.RULES.items() if v not in text]
        if self.BRIEF not in text or "## Builder checklist" not in text:
            found.append("brief")
        return found

    def test_the_skill_carries_the_ten_items_and_the_brief_sentence(self):
        self.assertEqual(self.problems(self.text()), [])
        self.assertEqual(len(self.ITEMS), 10)   # 10 of 10 markers

    def test_a_mutant_that_removes_each_item_or_the_brief_sentence_fails(self):
        text = self.text()
        for key in self.ITEMS:
            with self.subTest(item=key):
                self.assertEqual(self.problems(text.replace(self.ITEMS[key], "")), [key])
        self.assertEqual(self.problems(text.replace(self.BRIEF, "")), ["brief"])
        for key, rule in self.RULES.items():   # three more mutants: strip the unpinned rule of (b), (i), (e)
            with self.subTest(rule=key):
                self.assertEqual(self.problems(text.replace(rule, "")), ["rule " + key])

    def test_item_b_names_no_invented_reader(self):
        import ast
        text = self.text()
        self.assertIn("`read_record_file(path, max_bytes)` in `bin/cloud_worker.py`", text)
        self.assertNotIn("has no such shared module", text)
        tree = ast.parse((REPO / "bin" / "cloud_worker.py").read_text())
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_record_file"]
        self.assertEqual(len(found), 1)
        self.assertEqual([a.arg for a in found[0].args.args], ["path", "max_bytes"])


class TestEveryReaderOfAPullRequestsFileList(unittest.TestCase):
    """#967 S12: the readers of a pull request's file list are DERIVED from the source and compared with the docs table."""

    FILES = ["bin/fleet-switchboard", "bin/impact_hold.py", "bin/delegated_clear.py", "bin/clear_carry.py"]
    PAT = re.compile(r"pulls/[^\"']*/files|--json.*\bfiles\b")
    FIELDS = re.compile(r"[A-Za-z,]*\bfiles\b[A-Za-z,]*")

    @classmethod
    def derive(cls, sources):
        found = set()
        for name, text in sources.items():
            def walk(node, stack):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stack = stack + [node.name]
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and stack and len(node.value) < 200:
                    v = node.value
                    if ("/" in v and cls.PAT.search(v)) or ("," in v and cls.FIELDS.fullmatch(v)):
                        found.add((name, stack[0]))
                for child in ast.iter_child_nodes(node):
                    walk(child, stack)
            walk(ast.parse(text), [])
        return found

    @classmethod
    def sources(cls):
        names = cls.FILES + sorted(str(p.relative_to(BIN.parent)) for p in (BIN / "switchboard").glob("*.py"))
        return {n: (BIN.parent / n).read_text() for n in names}

    @staticmethod
    def table():
        doc = (BIN.parent / "docs" / "switchboard.md").read_text()
        block = doc.split("**Every reader of a pull request's file list (#967)**", 1)[1].split("**Every branch", 1)[0]
        return {(m.group(2), m.group(1)) for m in re.finditer(r"^\| `([^`]+)` \| `([^`]+)` \|", block, re.M)}

    def check(self, sources):
        derived, table = self.derive(sources), self.table()
        self.assertEqual(sorted(derived - table), [], "a reader of the file list is not in the docs table")
        self.assertEqual(sorted(table - derived), [], "a table row names no reader")

    def test_967_every_reader_of_a_pull_requests_file_list_is_in_the_table(self):
        sources = self.sources()
        derived = self.derive(sources)
        for func in ("read_pr_files", "pr_floor_report", "own_change", "_review_pr"):
            self.assertIn(func, {f for _, f in derived})
        self.check(sources)
        # mutant: a spliced NEW reader of pulls/N/files must fail the check
        mutant = dict(sources)
        mutant["bin/fleet-switchboard"] += '\n\ndef spliced_new_reader(get, repo, n):\n    return get("repos/%s/pulls/%d/files" % (repo, n))\n'
        with self.assertRaises(AssertionError):
            self.check(mutant)


class TestGithubConfig(Base):
    def cfg(self, **raw):
        return sb.validate_config(raw)

    def test_defaults_watch_nothing_and_name_the_documented_events(self):
        config = self.cfg()
        self.assertEqual((config["github_repos"], config["gh"], config["github_lookback_hours"],
                          config["github_port"]), ([], sb.own_gh_path("gh"), 24, None))   # the RESOLVED gh: environment-independent
        self.assertEqual(config["github_events"], ["issues", "issue_comment", "sub_issues", "pull_request",
                                                   "pull_request_review", "check_suite", "workflow_run"])

    def test_a_config_gets_its_own_lists(self):
        first = self.cfg()
        first["github_repos"].append("a/b")
        first["github_events"].append("push")
        self.assertEqual((self.cfg()["github_repos"], len(self.cfg()["github_events"])), ([], 7))
        self.assertEqual(sb.DEFAULTS["github_repos"], [])

    def test_good_values(self):
        config = self.cfg(github_repos=["a/b", "c-d/e.f_g"], gh="/opt/gh", github_lookback_hours=0.5,
                          github_events=["issue_comment"], github_port=8123)
        self.assertEqual((config["github_repos"], config["gh"], config["github_port"]),
                         (["a/b", "c-d/e.f_g"], "/opt/gh", 8123))

    def test_bad_values_are_refused_by_name(self):
        for key, value in (("github_repos", "a/b"), ("github_repos", ["ab"]), ("github_repos", ["a/b/c"]),
                           ("github_repos", ["a/b", "a/b"]), ("github_repos", [3]), ("gh", ""), ("gh", 3),
                           ("github_lookback_hours", 0), ("github_lookback_hours", True),
                           ("github_events", []), ("github_events", ["Issues"]), ("github_events", "issues"),
                           ("github_port", 0), ("github_port", 70000), ("github_port", "80"), ("github_port", True)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(sb.ConfigError) as caught:
                    self.cfg(**{key: value})
                self.assertIn(key, str(caught.exception))

    def test_an_unknown_key_is_warned_about_not_refused(self):
        sb._WARNED_CONFIG.clear()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.cfg(github_repo=["a/b"])
        self.assertIn("github_repo", err.getvalue())


class TestPlanningSkill(unittest.TestCase):
    """skills/fleet-planning: short, installed like the other skills, and its own example passes `plan check`."""
    SKILL = REPO / "skills" / "fleet-planning" / "SKILL.md"

    def test_it_is_a_short_skill_with_the_frontmatter_the_others_have(self):
        text = self.SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: fleet-planning\ndescription: "))
        self.assertLessEqual(len(text.split()), 1200, "the skill must be cheap to load")
        for phrase in ("PLAN: <link>", "fleet-switchboard plan check", "plan review", "docs/plan-template.md", "discriminates", "What the reviewers attack"):
            self.assertIn(phrase, text)
        self.assertIn("not built yet", text)  # the review engine is part 2: said plainly
        self.assertNotIn("--tier", text)  # no command signature for a command that does not exist yet
        self.assertIn("computed from the ask or epic", text)
        self.assertIn("name the integration branch in the plan", text)

    def test_it_is_installed_with_the_kits_other_skills_and_named_in_the_install_guide(self):
        self.assertIn("fleet-planning", sb.kit_skill_names(REPO))
        self.assertIn('ln -s "$PWD/skills/fleet-planning"', (REPO / "INSTALL.md").read_text())

    def test_its_example_passes_the_check_it_teaches(self):
        text = self.SKILL.read_text()
        block = re.search(r"```yaml\n(scenarios:.*?)```", text, re.S).group(1)
        done = "(1) a project with no agents lists none, and after commissioning lists exactly one"
        plan = "## Plan\n\nDone when: %s\n\nPC-1: existing projects keep their listing\n\n```yaml\n%s```\n" % (done, block)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.md"
            path.write_text(plan)
            out = subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "plan", "check", "--file", str(path), "--done-when", done],
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("plan check: PASS", out.stdout)


class TestCompose(unittest.TestCase):
    def test_header_then_each_item_in_full(self):
        text, carried = sb.compose("platform", [
            fact("b", EPOCH + 1, summary="Second\nwith detail", kind="review", sender="coder", issue=7),
            fact("a", EPOCH, summary="First thing"),
        ])
        self.assertEqual(text.splitlines(), [
            "[switchboard] 2 for platform: Second with detail [newest first]",
            "- review from coder (issue #7): Second",
            "  with detail",
            "- lab.fact from the fleet: First thing",
        ])
        self.assertEqual([f.key for f in carried], ["b", "a"])   # #1275: newest first

    def test_the_cap_leaves_the_rest_pending_and_says_so(self):
        facts = [fact("k%02d" % n, EPOCH + n, summary="x" * 300) for n in range(30)]
        text, carried = sb.compose("platform", facts, cap=1000)
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(len(carried), 2)
        self.assertTrue(text.endswith("\u2026 and 28 more: run fleet-switchboard pending platform"))
        self.assertEqual([f.key for f in carried], ["k29", "k28"])

    def test_one_item_longer_than_the_cap_is_cut_and_says_so(self):
        text, carried = sb.compose("platform", [fact("big", summary="y" * 5000)], cap=500)
        self.assertEqual(len(text), 500)
        self.assertTrue(text.endswith("[cut at 500 characters]"))
        self.assertEqual(len(carried), 1)


class TestOwnMeasurementTester(unittest.TestCase):
    """An orchestrator that measured a scenario itself re-measures through a tester of its lane, never a waiver."""
    PLANNING = "**Own measurement:** launch a tester of your lane (`--role tester --repo <the pull request's repo> --reports-to <you>`, headless) to re-measure and record; never record as yourself."
    COORDINATION = "When a scenario's evidence is your own measurement, from your own pane launch a tester of your lane (`--role tester --repo <the pull request's repo> --reports-to <you>`, headless is enough) to re-measure and record the verdict: never record as yourself, and do not ask for a waiver."

    def test_each_text_is_in_its_skill_exactly_once(self):
        for skill, text in (("fleet-planning", self.PLANNING), ("fleet-coordination", self.COORDINATION)):
            with self.subTest(skill=skill):
                self.assertEqual((REPO / "skills" / skill / "SKILL.md").read_text().count(text), 1)

    def test_the_texts_name_roles_never_agents_or_people(self):
        for text in (self.PLANNING, self.COORDINATION):
            self.assertNotRegex(text, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")

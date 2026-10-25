"""The read of a pull request's whole file list, and the readers of it (ask #967, slice S12).

Moved out of bin/test-switchboard to keep that file under its ceiling (docs/architecture.md): the tests are the same, only their home is new.
They build on `LedgerBase` and the fakes of the suite's entry, so the entry is the host: `bin/test-switchboard` loads this file (CI runs it
there, with the rest), and run alone (`python3 -m unittest tests.switchboard.test_file_list`) this file loads the entry itself and takes
only the names it needs from it.
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin"
ENTRY = BIN / "test-switchboard"
NEEDED = ("FAKE_GH", "FLOOR_CONFIG", "LedgerBase", "captured", "sb")


def host_names():
    """the names this file needs from the suite's entry: from the entry that is loading this file, or from a copy of the entry loaded here"""
    for module in list(sys.modules.values()):
        if getattr(module, "_FILE_LIST_HOST", False):
            return {name: getattr(module, name) for name in NEEDED}
    sys.path.insert(0, str(REPO / "tests" / "switchboard"))
    loader = importlib.machinery.SourceFileLoader("test_switchboard_entry", str(ENTRY))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("test_switchboard_entry", loader))
    sys.modules["test_switchboard_entry"] = module
    loader.exec_module(module)
    return {name: getattr(module, name) for name in NEEDED}


globals().update(host_names())

class FakePullFiles:
    """GitHub's pull request answers for ONE pull request, no more generous than the real API: the files are served 100 a page whatever per_page asks, only the first 3000 are ever listed (a page past
    the end is empty), `changed_files` is the true count, and `gh pr view` lists 100 files at most with no source path for a rename. `faults` is {"pull" or page number: [fault, ...]}: one fault is used up
    by each call of that read, in order (a timeout, an HTTP 502, an answer that is not JSON, one over the size cap, an answer that is not a list). `heads` is what the pull request's head is at each read of it."""

    HEAD = "c" * 40
    LONG = "E" * 450 + " THE-END"   # an error text far over every old clip (80 to 600 characters), ending in a marker

    def __init__(self, files, number=9, heads=None, count=None):
        self.files, self.number = files, number
        self.count = len(files) if count is None else count
        self.faults, self.calls = {}, []
        self.heads = list(heads or [])

    @staticmethod
    def make(size, status="modified"):
        return [{"filename": "docs/f%04d.md" % i, "status": status, "sha": "e" * 40, "patch": "@@ -1 +1 @@\n-a\n+b", "changes": 2} for i in range(size)]

    def view(self):
        """what `gh pr view --json files,...` says: at most 100 files, a rename without its source"""
        shown = [{"path": f["filename"] if isinstance(f, dict) else "x", "additions": 1, "deletions": 1, "changeType": f["status"].upper() if isinstance(f, dict) else "MODIFIED"} for f in self.files[:100]]
        return {"headRefOid": self.HEAD, "baseRefOid": "b" * 40, "changedFiles": self.count, "files": shown}

    def fault(self, key):
        queue = self.faults.get(key) or []
        return queue.pop(0) if queue else None

    def __call__(self, argv, **kwargs):
        path = argv[2]
        self.calls.append(path)
        route, _, query = path.partition("?")
        query = dict(q.split("=", 1) for q in query.split("&") if q)
        tail = route.split("/", 3)[3]
        if tail == "pulls/%d" % self.number:
            key, body = "pull", None
            head = (self.heads.pop(0) if self.heads else self.HEAD)
            body = {"head": {"sha": head}, "base": {"sha": "b" * 40, "ref": "main"}, "changed_files": self.count}
        elif tail == "pulls/%d/files" % self.number:
            page, size = int(query.get("page", "1")), min(int(query.get("per_page", "30")), 100)
            key, body = page, (self.files[:3000])[(page - 1) * size:page * size]
        elif tail.startswith("compare/"):
            key, body = "compare", {"merge_base_commit": {"sha": "b" * 40}}
        else:
            raise AssertionError("the fake GitHub has no answer for %s" % path)
        fault = self.fault(key)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        if fault == "longerror":
            return subprocess.CompletedProcess(argv, 1, "", "gh: " + FakePullFiles.LONG)
        if fault == "http502":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)")
        if fault in ("http403", "http404", "http422"):
            return subprocess.CompletedProcess(argv, 1, "", "gh: Error (HTTP %s)" % fault[4:])
        if fault == "bigbody":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)\n" + "B" * (1 << 20))
        if fault == "bodystatus":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Validation Failed (HTTP 422)\n{\"message\": \"see HTTP 404 and HTTP 502\"}")
        if fault == "notjson":
            return subprocess.CompletedProcess(argv, 0, "<html>bad gateway</html>", "")
        if fault == "toolarge":
            return subprocess.CompletedProcess(argv, 0, "[" + "0," * (sb.CARRY_ANSWER_MAX // 2 + 1) + "0]", "")
        if fault == "notlist":
            return subprocess.CompletedProcess(argv, 0, json.dumps(["x"] if key in ("pull", "compare") else {"message": "Server Error"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    def pages_asked(self, page):
        return len([c for c in self.calls if c.endswith("&page=%d" % page)])


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


class TestPullFileListRead(LedgerBase):
    """#967: a pull request's changed files are read in full or refused with ONE named reason; a failed read is never called a cut list. The REAL `delegated_clear_pr_checks`, `pr_floor_report` and
    `read_pr_files` over a fake GitHub that serves pages of 100, errors on a chosen page, stops at 3000 and says nothing it would not say."""

    REPO = "acme/app"

    def setUp(self):
        super().setUp()
        self.extra_config = dict(FLOOR_CONFIG, plan_gates={"repos": {"acme/app": {"mode": "shadow"}}})
        self.configure()
        self.review = (("none", None), None)
        patcher = mock.patch.object(sb, "impact_review_record", lambda state, pr, head: self.review)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sb, "delegated_clear_map", lambda *a, **k: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, files, **kwargs):
        """a fake GitHub for these files, and the engine that reads `gh pr view` from it too"""
        self.github = FakePullFiles(files, **kwargs)

        def runner(argv, **kw):
            if argv[0] != FAKE_GH:
                return self.route(argv, **kw)
            if argv[1] == "pr":
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.github.view()), "")
            return self.github(argv, **kw)
        self.runner = runner
        engine = self.world.new_engine("daemon")
        engine.config, engine.state_dir, engine.gh_runner = sb.load_config(), self.world.state, runner
        self.engine = engine
        return self.github

    def check(self):
        return sb.delegated_clear_pr_checks(self.engine, self.runner, self.REPO, 9)

    # ---- (2) the whole list, whatever its size

    SIZES = (0, 1, 38, 99, 100, 101, 200, 201, 3000)

    def test_s3_every_size_is_read_in_full_in_order_with_one_page_more_than_the_full_ones(self):
        module = sb.load_module("delegated_clear")
        for size in self.SIZES:
            with self.subTest(size=size):
                api = self.serve(FakePullFiles.make(size))
                files, _ = module.read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
                self.assertEqual([f["filename"] for f in files], [f["filename"] for f in api.files])
                self.assertEqual(len(api.calls), 1 + size // 100 + 1, "the pull request, then pages until a short one (the page after the last full page is the empty one that says done)")

    def test_s3_the_clear_accepts_38_100_101_and_3000_files(self):
        for size in (38, 100, 101, 3000):
            with self.subTest(size=size):
                api = self.serve(FakePullFiles.make(size))
                report, why = self.check()
                self.assertEqual((why, report is not None), (None, True), size)

    def test_the_clear_reuses_the_floors_paged_list_each_page_asked_once_for_a_rename_pull_request(self):
        files = FakePullFiles.make(250)
        files[3] = dict(files[3], status="renamed", previous_filename="docs/old.md")
        api = self.serve(files)
        report, why = self.check()
        self.assertEqual(why, None)
        self.assertEqual([api.pages_asked(n) for n in (1, 2, 3, 4)], [1, 1, 1, 0], "the floor read the pages; the clear did not read them again")

    def count_words(self, count):
        api = self.serve(FakePullFiles.make(38))
        api.count = count
        return self.check()[1]

    ENTRY_SHAPES = (   # (label, the entry at position 51 of page 2, the detail the refusal gives)
        ("a string", "bad", "a str, not an object"),
        ("null", None, "a NoneType, not an object"),
        ("a list", ["a"], "a list, not an object"),
        ("an object with no filename", {"status": "modified"}, "no file name"),
        ("a filename that is a number", {"filename": 5, "status": "modified"}, "no file name"),
        ("an empty filename", {"filename": "", "status": "modified"}, "no file name"),
    )

    def entry_refusal(self, entry, floor=False):
        """the refusal for `entry` at position 51 of page 2 of 250 files, through the clear or through the floor"""
        api = self.serve(FakePullFiles.make(250))
        api.files[150] = entry
        if floor:
            report = sb.pr_floor_report(self.engine, self.REPO, 9)
            self.assertEqual(report["complete"], False)
            return report["list_problem"]
        return self.check()[1]

    def nameless_words(self, name, floor=False):
        return self.entry_refusal({"filename": name, "status": "modified"}, floor)

    def test_a_count_that_is_missing_not_a_number_or_negative_says_which(self):
        for count, words in ((None, "was missing"), ("12", "was not a number"), (12.5, "was not a number"), (True, "was not a number"), (-1, "was negative (-1)")):
            with self.subTest(count=count):
                why = self.count_words(count)
                self.assertIn(words, why)
                self.assertNotIn("cut", why)
                self.assertNotIn("after one retry", why)

    def test_every_shape_of_a_bad_entry_is_refused_with_its_kind_its_page_and_its_one_based_position_through_the_clear_and_the_floor(self):
        for label, entry, detail in self.ENTRY_SHAPES:
            for floor in (False, True):
                with self.subTest(shape=label, floor=floor):
                    why = self.entry_refusal(entry, floor)
                    self.assertIn("page 2, position 51 on the page (counting from 1) of the changed files is not a file entry (%s)" % detail, why)
                    self.assertNotIn("cut", why)

    def test_s9_a_mutant_for_each_branch_and_for_the_position_of_a_bad_entry_is_caught(self):
        def every_shape():
            for label, entry, detail in self.ENTRY_SHAPES:
                self.assertIn("page 2, position 51 on the page (counting from 1) of the changed files is not a file entry (%s)" % detail, self.entry_refusal(entry))
        check_line = '    if not isinstance(entry.get("filename"), str) or not entry["filename"]:'
        mutants = {
            "a non-object entry is skipped": ('            if not isinstance(entry, dict):\n', '            if False:\n'),
            "a missing or non-text name is kept": (check_line, '    if not entry.get("filename"):'),
            "an empty name is kept": (check_line, '    if not isinstance(entry.get("filename"), str):'),
            "the position counts from 0": ("enumerate(got, 1)", "enumerate(got)"),
        }
        for label, (old, new) in mutants.items():
            with self.subTest(mutant=label):
                self.assertFalse(self.survives(self.mutant(old, new), every_shape), "the mutant %s is not caught" % label)
        every_shape()

    def test_u4_a_view_that_vouches_for_its_list_makes_no_paged_read_for_the_floor(self):
        api = self.serve(FakePullFiles.make(38))
        report = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual((report["complete"], report["list_problem"], api.calls), (True, None, []), "not one REST call")

    def test_s5_3001_files_are_refused_naming_the_limit_before_any_page_is_read(self):
        api = self.serve(FakePullFiles.make(3001))
        report, why = self.check()
        self.assertIsNone(report)
        self.assertIn("more than 3000 changed files", why)
        module = sb.load_module("delegated_clear")
        api.calls.clear()
        with self.assertRaises(module.FileListError) as caught:
            module.read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
        self.assertEqual(caught.exception.kind, "over_limit")
        self.assertEqual([c for c in api.calls if "&page=" in c], [], "refused on the pull request's own count, no page is read")

    def test_a_top_tier_file_only_on_page_3_holds_and_a_duplicated_page_is_refused_naming_the_repeat(self):
        files = FakePullFiles.make(300)
        files[250]["filename"] = "bin/chat_clear.py"
        self.serve(files)
        report, why = self.check()
        self.assertTrue(report is None and why and "bin/chat_clear.py" in why, (report and "read", why))
        floor = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual(floor["complete"], True)
        self.assertTrue(floor["top_tier_hits"], floor["top_tier_hits"])
        why = self.drive("duplicate")
        self.assertIn("docs/f0000.md (page 3, position 1, already listed on page 1)", why)

    def test_the_paged_list_of_a_newer_head_than_the_floor_report_is_not_used(self):
        self.serve(FakePullFiles.make(150), heads=["d" * 40])
        report = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual(report["complete"], False)
        self.assertIn("head moved", report["list_problem"])

    def test_s5_exactly_3000_files_are_read_in_full(self):
        api = self.serve(FakePullFiles.make(3000))
        files, _ = sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
        self.assertEqual(len(files), 3000)

    # ---- #967 review fixes: only a transient error is retried; the reason is capped; the status is the structured one

    def read_page2(self, fault, times=5):
        api = self.serve(FakePullFiles.make(250))
        api.faults[2] = [fault] * times
        try:
            sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
        except Exception as err:
            return api, err
        return api, None

    def test_family_final_errors_are_asked_once(self):
        for fault in self.FINAL:
            with self.subTest(fault=fault):
                api, err = self.read_page2(fault)
                self.assertEqual(getattr(err, "kind", None), "read_failed")
                self.assertEqual(api.pages_asked(2), 1, "a 403, 404, 422 or over-size answer is not retried")

    def test_family_a_502_and_a_timeout_are_retried_once(self):
        for fault in ("http502", "timeout"):
            with self.subTest(fault=fault):
                api, err = self.read_page2(fault)
                self.assertEqual(api.pages_asked(2), 2)
                api, err = self.read_page2(fault, 1)
                self.assertIsNone(err)

    def test_family_a_1_mib_error_body_is_cut_to_the_cap_and_status_kept(self):
        api, err = self.read_page2("bigbody")
        text = str(err)
        self.assertLess(len(text), 700)
        self.assertIn("HTTP 502", text)
        self.assertIn("gh failed", text)
        self.assertIn("more characters not kept", text)

    def test_family_the_status_comes_from_the_first_line_never_from_the_body(self):
        api, err = self.read_page2("bodystatus")
        self.assertIn("HTTP 422", str(err))
        self.assertEqual(api.pages_asked(2), 1)
        module = sb.load_module("delegated_clear")
        self.assertNotIn("with HTTP", module.error_class(sb.IntentError("gh", "boom: HTTP 404")))

    def test_family_the_branches_are_derived_from_the_code_and_the_docs_table_lists_each(self):
        module = sb.load_module("delegated_clear")
        import ast as _ast
        src = Path(module.__file__).read_text()
        used = {n.args[0].value for n in _ast.walk(_ast.parse(src)) if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "FileListError" and n.args and isinstance(n.args[0], _ast.Constant)}
        self.assertEqual(used - set(module.FILE_REFUSALS), set())
        docs = (Path(module.__file__).parent.parent / "docs" / "switchboard.md").read_text()
        table = docs.split("**Every branch of the file-list read**")[1].split("**The audit keeps")[0]
        for kind in used | {"read_failed", "not_recorded"}:
            self.assertIn("`%s`" % kind, table, kind)
        for word in ("is_transient", "error_class", "read_with_retry"):
            self.assertIn(word, src)

    def test_family_mutants_retry_everything_retry_nothing_no_cap_body_status(self):
        def final():
            api, err = self.read_page2("http404")
            self.assertEqual(api.pages_asked(2), 1)

        def transient():
            api, err = self.read_page2("http502")
            self.assertEqual(api.pages_asked(2), 2)

        def cap():
            api, err = self.read_page2("bigbody")
            self.assertLess(len(str(err)), 700)

        def status():
            api, err = self.read_page2("bodystatus")
            self.assertEqual(api.pages_asked(2), 1)
            self.assertNotIn("with HTTP", sb.load_module("delegated_clear").error_class(sb.IntentError("gh", "x: see HTTP 404")))
        for label, old, new, check in (
            ("retries everything", "            if not is_transient(err):\n                break", "            if False:\n                break", final),
            ("retries nothing", "            if not is_transient(err):\n                break", "            if True:\n                break", transient),
            ("no cap", "    if len(detail) > REASON_MAX:", "    if False:", cap),
            ("status from the text", "status = getattr(err, \"status\", None)", "status = (lambda m: int(m.group(1)) if m else None)(re.search(r\"HTTP (\\d{3})\", detail))", status),
        ):
            with self.subTest(mutant=label):
                self.assertFalse(self.survives(self.mutant(old, new), check), label)
                check()

    # ---- one retry of a page

    TRANSIENT = ("timeout", "http502", "notjson", "notlist")
    FINAL = ("http403", "http404", "http422", "toolarge")

    def test_s4_a_transient_error_on_a_page_is_retried_once_and_the_list_is_whole(self):
        module = sb.load_module("delegated_clear")
        for fault in self.TRANSIENT:
            for where in (1, 2, 3, "pull"):
                with self.subTest(fault=fault, where=where):
                    api = self.serve(FakePullFiles.make(250))
                    api.faults[where] = [fault]
                    files, _ = module.read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
                    self.assertEqual(len(files), 250)
                    asked = len([c for c in api.calls if (c.endswith("/pulls/9") if where == "pull" else c.endswith("&page=%d" % where))])
                    self.assertEqual(asked, 2, "the page was asked twice: once failed, once answered")

    def test_s4_a_persistent_error_is_asked_exactly_twice_never_more(self):
        for fault in self.TRANSIENT:
            with self.subTest(fault=fault):
                api = self.serve(FakePullFiles.make(250))
                api.faults[2] = [fault] * 5
                report, why = self.check()
                self.assertIsNone(report)
                self.assertEqual(api.pages_asked(2), 2)

    # ---- (1) the cause, reproduced: a rename with no source path was called a cut list

    def test_s1_a_38_file_pull_request_with_a_rename_was_called_a_cut_list_and_now_says_what_it_is(self):
        files = FakePullFiles.make(38)
        files[3] = dict(files[3], status="renamed")   # `gh pr view` lists it as RENAMED with no source, in a repository with no floor globs
        api = self.serve(files)
        self.engine.config = dict(self.engine.config, impact_floor={"repos": {}})
        report = sb.pr_floor_report(self.engine, "acme/other", 9)
        self.assertEqual((report["files_total"], len(report["renamed_no_source"]), report["unknown"]), (38, 1, None), "no floor globs: nothing set `unknown`, so the old refusal fell back to its own words")
        old = "the pull request's changes cannot be read in full (%s)" % (report.get("unknown") or "the file list is cut")
        self.assertIn("the file list is cut", old, "what the recorded refusal said")
        text, why = self.check()[1], sb.load_module("delegated_clear").list_words(report)
        self.assertNotIn("cut", text)
        self.assertIn("no known source path", text)
        self.assertNotIn("cut", why)

    def test_s10_a_rename_reads_its_source_from_the_paged_list_and_a_floor_path_named_only_as_the_source_counts(self):
        files = FakePullFiles.make(38)
        files[3] = dict(files[3], status="renamed", previous_filename="migrations/0002.sql")
        self.serve(files)
        report = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual((report["complete"], report["renamed_no_source"], report["list_problem"]), (True, [], None))
        self.assertIn("migrations/0002.sql", report["floor_paths"])

    def test_s10_a_list_that_gh_cuts_at_100_is_completed_and_a_failed_completion_stays_held_with_its_reason(self):
        api = self.serve(FakePullFiles.make(250))
        report = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual((report["complete"], report["files_total"], report["list_problem"]), (True, 250, None))
        api = self.serve(FakePullFiles.make(250))
        api.faults[1] = ["http502"] * 3
        report = sb.pr_floor_report(self.engine, self.REPO, 9)
        self.assertEqual(report["complete"], False)
        self.assertIn("GitHub listed 100 of 250 changed files; the paged list could not be read", report["list_problem"])
        self.assertEqual(report["decision"], "hold", "an unknown list holds")

    def test_u1_u2_an_ordinary_list_is_accepted_and_a_change_to_the_clear_code_is_refused_with_the_old_words(self):
        self.serve(FakePullFiles.make(38))
        report, why = self.check()
        self.assertEqual((why, report["files_total"]), (None, 38))
        files = FakePullFiles.make(38)
        files[0] = dict(files[0], filename="bin/delegated_clear.py")
        self.serve(files)
        report, why = self.check()
        self.assertIsNone(report)
        self.assertEqual(why, "it changes a file that is the clear or hold machinery or that the daemon loads (bin/delegated_clear.py): that stays the owner's own clear")

    # ---- PC-4: the audit keeps the whole reason and lists the causes as records

    def refusal_audit(self):
        return [e for e in self.world.audit() if e["event"] == "gate.hold_clear_refused"][-1]["detail"]

    def test_s13_a_reason_of_forty_clear_code_files_is_in_the_audit_whole(self):
        files = FakePullFiles.make(40)
        for i, f in enumerate(files):
            f["filename"] = "shims/a-long-descriptive-name-for-the-clear-helper-number-%02d.sh" % i
        self.serve(files)
        code, out, err = self.run_clear()
        detail = self.refusal_audit()
        self.assertEqual(code, 2)
        self.assertGreater(len(detail["why"]), 1000)
        self.assertTrue(all(f["filename"] in detail["why"] for f in files), "every path is named in the audit")
        self.assertTrue(detail["why"].endswith("that stays the owner's own clear"), "the last sentence is there")
        self.assertNotIn("\u2026", detail["why"])
        self.assertIn(detail["why"], err, "and the printed line is whole too")
        self.assertEqual(len([c for c in detail["causes"] if c["kind"] == "clear_code"]), 40)

    def test_s13_a_control_character_in_a_file_name_never_reaches_the_audit_or_the_terminal(self):
        files = FakePullFiles.make(3)
        files[1]["filename"] = "shims/a\x1b[31m\x07.sh"
        self.serve(files)
        code, out, err = self.run_clear()
        detail = self.refusal_audit()
        self.assertEqual(code, 2)
        for text in [detail["why"], err] + [c["text"] + (c["path"] or "") for c in detail["causes"]]:
            self.assertNotRegex(text.replace("\n", ""), r"[\x00-\x1f\x7f-\x9f]")

    def test_s13_the_one_bound_is_what_an_audit_line_holds_and_it_says_how_much_was_left_out(self):
        module = sb.load_module("delegated_clear")
        long = "x" * (module.AUDIT_LINE_MAX + 50)
        line = module.whole_line(long)
        self.assertTrue(line.startswith("x" * module.AUDIT_LINE_MAX) and "50 more characters not kept" in line)
        self.assertEqual(module.whole_line("y" * module.AUDIT_LINE_MAX), "y" * module.AUDIT_LINE_MAX, "exactly at the bound nothing is cut")
        many = [module.cause("clear_code", "z" * 5000, "bin/p%d.py" % i) for i in range(1000)]
        parts = module.audit_parts("why", many)
        self.assertEqual(len(parts["causes"]) + parts["causes_omitted"], 1000)
        self.assertLessEqual(len(json.dumps(parts)), module.AUDIT_LINE_MAX + 5000)
        self.assertNotIn("causes_omitted", module.audit_parts("why", many[:5]))

    # NOTE: the writers are found by a scan of the source text, so a writer that builds the audit line another way (not `engine.audit.append(... "gate.hold_clear_refused" ...)` on one line) is not caught by the scan; the behavioural halves below drive the real writers.
    def test_s15_no_writer_of_a_refusal_on_the_clear_path_cuts_the_reason(self):
        text = (BIN / "impact_hold.py").read_text()
        appends = [m.group(0) for m in re.finditer(r'engine\.audit\.append\([^\n]*"gate\.hold_clear_refused"[^\n]*', text)]
        self.assertEqual(len(appends), 6, "the writers of a refusal on the clear path (the not-owner one carries a fixed line)")
        for line in appends:
            self.assertNotIn("_one_line(why", line)
            self.assertNotIn("300", line.replace('"gate.hold_clear_refused"', ""))
        for line in re.findall(r'sys\.stderr\.write\("%s: impact clear: refused: %s\\n"[^\n]*', text):
            self.assertNotIn("_one_line", line)
        self.serve(FakePullFiles.make(3))
        long = "the owner's standing-orders file cannot be read (" + "x " * 450 + "): refused"
        with mock.patch.object(sb, "delegated_clear_checks", lambda *a, **k: (None, long)):
            code, out, err = self.run_clear()
        self.assertEqual((code, self.refusal_audit()["why"]), (2, " ".join(long.split())))
        self.assertIn(" ".join(long.split()), err)

    # ---- the producers of a reason on the clear path: each is made to fail with a LONG error text, and the audit, the cause record and the printed line hold all of it

    def hold_mutant(self, old, new):
        """a patch that puts the moved clear functions of a copy of impact_hold.py (with `old` replaced by `new`) in the daemon's namespace"""
        source = (BIN / "impact_hold.py").read_text()
        self.assertEqual(source.count(old), 1, "the mutation point is there: %r" % old)
        module = types.ModuleType("impact_hold_mutant")
        exec(compile(source.replace(old, new), "impact_hold_mutant", "exec"), module.__dict__)
        bound = {}
        for name in module.MOVED:
            original = getattr(module, name)
            function = types.FunctionType(original.__code__, sb.__dict__, name, original.__defaults__, original.__closure__)
            function.__kwdefaults__ = original.__kwdefaults__
            function.__dict__.update(original.__dict__)
            bound[name] = function
        return mock.patch.dict(sb.__dict__, bound)

    def producers(self):
        """name -> (a function that makes the real read fail with the long text and runs the clear, the (old, new) mutation that puts the old clip back, where the whole text must be: 'audit' or 'stderr')"""
        LONG = FakePullFiles.LONG
        d = "delegated_clear.py"

        def view_fails():
            self.serve(FakePullFiles.make(38))
            with mock.patch.object(sb, "read_pr_json", lambda *a, **k: (_ for _ in ()).throw(sb.NoticeError(LONG))):
                return self.run_clear()

        def page_fails():
            self.serve(FakePullFiles.make(250)).faults[2] = ["longerror"] * 3
            return self.run_clear()

        def owner_file():
            self.serve(FakePullFiles.make(38))
            with mock.patch.object(sb, "read_cos_orders", lambda config: (_ for _ in ()).throw(sb.CosOrdersError(LONG))):
                return self.run_clear({"source": "owner-file"})

        def charter():
            self.serve(FakePullFiles.make(38))
            return self.run_clear(orders=lambda reader, charter, repo: (_ for _ in ()).throw(sb.IntentError("gh", LONG)))

        def score():
            self.serve(FakePullFiles.make(38))
            with mock.patch.object(sb, "rating_at", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(LONG))):
                return self.run_clear({"max_level": "Serious"})

        def label_delete():
            api = self.serve(FakePullFiles.make(38))
            inner = self.runner

            def runner(argv, **kw):
                return subprocess.CompletedProcess(argv, 1, "", "gh: " + LONG) if "DELETE" in argv else inner(argv, **kw)
            self.runner = self.engine.gh_runner = runner
            return self.run_clear()

        def state_aside():
            self.serve(FakePullFiles.make(38))
            (self.world.state / sb.IMPACT_STATE_FILE).write_text("{not json")
            with mock.patch.object(os, "rename", side_effect=OSError(13, LONG)):
                return self.run_clear()
        w = 'load_module("delegated_clear").whole_line(str(err))'
        return {
            "the floor or its rule cannot be read": (view_fails, ("hold", 'its rule cannot be read (%s): refused" % module.whole_line(str(err))', 'its rule cannot be read (%s): refused" % _one_line(str(err), 140)'), "audit"),
            "a page failed twice": (page_fails, ("clear", 'detail = " ".join(str(err).split())', 'detail = " ".join(str(err).split())[:100]'), "audit"),
            "the owner's standing-orders file": (owner_file, ("hold", '(%s): refused" % module.whole_line(str(err))\n        if line is None', '(%s): refused" % _one_line(str(err), 120)\n        if line is None'), "audit"),
            "the charter's orders": (charter, ("hold", '"the charter\'s orders cannot be read (%s): refused" % module.whole_line(str(err))', '"the charter\'s orders cannot be read (%s): refused" % _one_line(str(err), 120)'), "audit"),
            "the score": (score, ("hold", 'module.whole_line(str(err)))\n    if scored is None:', '_one_line(str(err), 80))\n    if scored is None:'), "audit"),
            "a failed label removal": (label_delete, ("hold", 'sys.stderr.write("%s: impact clear: %s\\n" % (PROG, ' + w + '))', 'sys.stderr.write("%s: impact clear: %s\\n" % (PROG, _one_line(str(err), 300)))'), "stderr"),
            "a state file that could not be set aside": (state_aside, ("hold", 'sys.stderr.write("%s: impact clear: refused: %s; nothing was changed\\n" % (PROG, ' + w + '))', 'sys.stderr.write("%s: impact clear: refused: %s; nothing was changed\\n" % (PROG, _one_line(str(err), 200)))'), "stderr"),
        }

    def test_s15_every_producer_of_a_reason_on_the_clear_path_keeps_a_long_error_whole_and_puts_the_old_clip_back_is_caught(self):
        for name, (run, (where, old, new), place) in self.producers().items():
            with self.subTest(producer=name):
                code, out, err = run()
                self.assertEqual(code in (1, 2), True, (code, err))
                if name == "a page failed twice":   # #967 review: a failed read's reason is the one capped text (REASON_MAX)
                    self.assertIn("more characters not kept", err)
                    self.assertNotIn("THE-END", err)
                    continue
                self.assertIn("THE-END", err, "the printed line holds all of it")
                if place == "audit":
                    detail = self.refusal_audit()
                    self.assertIn("THE-END", detail["why"])
                    self.assertTrue(any("THE-END" in c["text"] for c in detail["causes"]), "and so does a cause record")
                patcher = self.mutant(old, new) if where == "clear" else self.hold_mutant(old, new)
                with patcher:
                    if place == "audit":
                        self.audit_mark = len([e for e in self.world.audit() if e["event"] == "gate.hold_clear_refused"])
                    code, out, err = run()
                    held = "THE-END" in err and (place != "audit" or "THE-END" in self.refusal_audit()["why"])
                self.assertFalse(held, "the old clip is put back and the test still passes: %s" % name)

    def test_s15_the_clear_path_has_no_clip_left_but_the_two_named_ones(self):
        hold = ast.parse((BIN / "impact_hold.py").read_text())
        found = set()
        for node in hold.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("delegated_clear_checks", "delegated_clear_score", "delegated_clear_pr_checks", "delegated_clear_map", "impact_clear_core", "cmd_impact_clear"):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and getattr(call.func, "id", "") in ("_one_line", "clip") or isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "clip":
                        found.add((node.name, ast.get_source_segment((BIN / "impact_hold.py").read_text(), call)[:60]))
        allowed = {("cmd_impact_clear", 'plan_ledger.clip(redact_command_text(reason), 300)'), ("cmd_impact_clear", 'plan_ledger.clip(redact_command_text(reason), 300)'),
                   ("cmd_impact_clear", '_one_line(str(err), 200)'), ("impact_clear_core", 'plan_ledger.clip(redact_command_text(reason), 300)'), ("impact_clear_core", 'plan_ledger.clip(under_order["text"], 300)')}
        self.assertEqual(sorted(found - allowed), [], "a clip on the clear path that is not one of the named ones (a person's own typed reason; the revoke and lock lines, which are not refusals of a pull request)")
        text = (BIN / "delegated_clear.py").read_text()
        self.assertEqual([m for m in re.findall(r"\)\[:\d+\]|sorted\([^)]*\)\[:\d+\]|\.split\(\)\)\[:\d+\]", text) if m != ")[:8]"], [], "no slice of a reason's words in the module that makes them")

    def cause_drivers(self):
        """kind -> a function returning the cause records of a refusal for it, each through the real code that makes it"""
        module = sb.load_module("delegated_clear")
        rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
        matches = sb.impact_floor.matches

        def pure(report=None, files=(), closure=None, scored=None, rule_=None, m=matches):
            base = {"owner_decision": "none", "complete": True, "unknown": None, "renamed_no_source": [], "review_action": "none", "review_class": None, "top_tier_hits": {}, "changed_paths": []}
            base.update(report or {})
            return module.causes_of(rule if rule_ is None else rule_, base, list(files), m, closure, scored)

        def via_check(kind):
            def run():
                self.setup_kind(kind)
                info = {}
                self.assertIsNone(sb.delegated_clear_pr_checks(self.engine, self.runner, self.REPO, 9, None, info)[0])
                return info["causes"]
            return run

        def hold_label(key):
            def run():
                self.serve(FakePullFiles.make(3))
                names = sb.label_names(sb.load_config())
                self.pr["labels"] = [{"name": names[key]}]
                self.pr["headRefOid"] = FakePullFiles.HEAD
                with mock.patch.object(sb, "read_pr_json", lambda engine, repo, number, extra="": dict(self.pr, **self.github.view())):
                    with self.assertRaises(sb.NoticeError):
                        sb.impact_clear_core(self.engine, self.runner, self.REPO, 9, "cos", "a reason of enough length", head=FakePullFiles.HEAD, via="order:17", under_order={"number": 17, "text": "t", "threshold": None})
                return self.refusal_audit()["causes"]
            return run
        listing = lambda *names: [{"filename": n, "status": "modified"} for n in names]
        return {
            "rule_unreadable": lambda: pure(rule_=None, m=None),
            "rule_malformed": lambda: pure(rule_=dict(rule, clear_code_paths="x")),
            "decision_unreadable": lambda: pure({"owner_decision": "maybe"}),
            "file_list": via_check("cut"),
            "never_class": lambda: pure({"top_tier_hits": {"credentials": ["config/secrets.yml"]}}),
            "no_review": lambda: pure({"review_action": None}),
            "review_no_class": lambda: pure({"review_action": "hold", "review_class": None}),
            "review_top_class": lambda: pure({"review_class": "authority"}),
            "score": lambda: pure(scored={"level": "High", "reasons": ["the score is High: High is the owner's"]}),
            "files_unreadable": lambda: module.causes_of(rule, {"owner_decision": "none", "complete": True, "review_action": "none"}, None, matches),
            "file_unreadable": lambda: pure(files=[{"status": "modified"}]),
            "path_unreadable": lambda: pure(files=[{"filename": "a\x00b"}]),
            "clear_code": lambda: pure(files=listing("shims/x.sh")),
            "new_file": lambda: pure(files=[{"filename": "bin/brand_new.py", "status": "added"}]),
            "closure_unmapped": lambda: pure(files=listing("bin/reds.py"), closure={"bin/reds.py": None}),
            "closure": lambda: pure(files=listing("bin/reds.py"), closure={"bin/reds.py": ["it changes one_line, part of the clear (head file)"]}),
            "order": lambda: self.order_refused(),
            "owner_hold_label": hold_label(sb.OWNER_HOLD_KEY),
            "person_hold_label": hold_label("merge-captain"),
        }

    def order_refused(self):
        self.serve(FakePullFiles.make(3))
        with mock.patch.object(sb, "delegated_clear_checks", lambda *a, **k: (None, "order 99 is not listed as a delegated clear")):
            self.run_clear()
        return self.refusal_audit()["causes"]

    def test_s14_every_kind_of_the_cause_table_has_a_record_with_its_kind_its_rule_and_the_words_the_reason_holds(self):
        module = sb.load_module("delegated_clear")
        used = set()
        for name in ("delegated_clear.py", "impact_hold.py"):
            used |= {a or b for a, b in re.findall(r'\bcause\("([a-z_]+)"|\b_item\("[a-z-]+", "([a-z_]+)"', (BIN / name).read_text())}
        self.assertEqual(sorted(used), sorted(module.CAUSE_RULES), "every kind used is in the table and every kind of the table is used")
        docs = (Path(__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        for kind in module.CAUSE_RULES:
            self.assertIn("`%s`" % kind, docs, "the documentation names the cause kind %s" % kind)
        drivers = self.cause_drivers()
        self.assertEqual(sorted(drivers), sorted(module.CAUSE_RULES), "a driver for every kind")
        for kind, run in drivers.items():
            with self.subTest(kind=kind):
                found = run()
                mine = [c for c in found if c["kind"] == kind]
                self.assertTrue(mine, "%s: %r" % (kind, found))
                self.assertEqual({c["rule"] for c in mine}, {module.CAUSE_RULES[kind]} if kind != "file_list" else {"cut"})
                self.assertTrue(all(set(c) == {"kind", "path", "rule", "text"} and c["text"] for c in mine))

    def test_s14_the_reason_is_the_words_of_the_records_and_a_path_is_the_records_own_field(self):
        files = FakePullFiles.make(3)
        files[1]["filename"] = "bin/chat_clear.py"
        self.serve(files)
        info = {}
        report, why = sb.delegated_clear_pr_checks(self.engine, self.runner, self.REPO, 9, None, info)
        self.assertEqual(why, "; ".join(c["text"] for c in info["causes"]))
        mine = [c for c in info["causes"] if c["path"] == "bin/chat_clear.py"]
        self.assertEqual([(c["kind"], c["rule"]) for c in mine], [("clear_code", "clear_code_paths")], info["causes"])

    # ---- (3)(5) the table of refusal kinds: each kind, its own words, no other's

    def words(self, kind):
        return {"read_failed": "after one retry", "not_a_list": "was not a list", "over_limit": "more than 3000 changed files", "head_moved": "head moved",
                "cut": "the list was cut: a page was short", "duplicate": "the changed files repeat", "bad_entry": "is not a file entry", "no_count": "count of changed files was", "no_source": "no known source path", "not_recorded": "no reason was recorded"}[kind]

    def drive(self, kind, detail=None):
        """the refusal text the real clear gives for each kind of failure in the table (`detail` picks which failed read for read_failed)"""
        text = self.setup_kind(kind, detail)
        if text is not None:
            return text
        report, why = self.check()
        self.assertIsNone(report, kind)
        return why

    def setup_kind(self, kind, detail=None):
        """build the fake GitHub for the kind; the refusal text itself for the one kind no pull request can reach (a report with no reason recorded)"""
        if kind == "read_failed":
            api = self.serve(FakePullFiles.make(250))
            api.faults[2] = [detail or "http502"] * 3
        elif kind == "not_a_list":
            api = self.serve(FakePullFiles.make(250))
            api.faults[2] = ["notlist"] * 3
        elif kind == "over_limit":
            self.serve(FakePullFiles.make(3001))
        elif kind == "head_moved":
            self.serve(FakePullFiles.make(38), heads=[FakePullFiles.HEAD, "d" * 40])
        elif kind == "bad_entry":
            api = self.serve(FakePullFiles.make(150))
            api.files[0] = "bad"
        elif kind == "duplicate":
            api = self.serve(FakePullFiles.make(300))
            api.files[200:300] = [dict(f) for f in api.files[0:100]]   # page 3 served as a copy of page 1; the count still says 300
        elif kind == "no_count":
            api = self.serve(FakePullFiles.make(38))
            api.count = None
        elif kind == "cut":
            self.serve(FakePullFiles.make(38), count=40)
        elif kind == "no_source":
            files = FakePullFiles.make(38)
            files[3] = dict(files[3], status="renamed")   # GitHub's own list gives no previous_filename here: nothing says where it came from
            self.serve(files)
        elif kind == "not_recorded":
            module = sb.load_module("delegated_clear")
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            self.serve(FakePullFiles.make(1))
            report = dict(sb.pr_floor_report(self.engine, self.REPO, 9), complete=False, list_problem=None, unknown=None, renamed_no_source=[], files_read=None, files_total=None)
            return "; ".join(module.refusals(rule, report, [], sb.impact_floor.matches, {}, None))
        else:
            raise AssertionError(kind)
        return None

    def test_s6_each_failure_kind_names_exactly_its_own_reason_and_only_cut_says_cut(self):
        module = sb.load_module("delegated_clear")
        for kind in module.FILE_REFUSALS:
            with self.subTest(kind=kind):
                why = self.drive(kind)
                self.assertIn(self.words(kind), why)
                for other in module.FILE_REFUSALS:
                    if other != kind:
                        self.assertNotIn(self.words(other), why, "%s reads as %s" % (kind, other))
                self.assertEqual("list was cut" in why, kind == "cut", why)
                self.assertEqual(why.count("\n"), 0, "one line")

    def test_s6_a_read_failure_names_its_class_timeout_status_not_json_or_too_large(self):
        for fault, words in (("timeout", "gh unavailable"), ("http502", "HTTP 502"), ("notjson", "not JSON"), ("toolarge", "rejected")):
            with self.subTest(fault=fault):
                why = self.drive("read_failed", fault)
                self.assertIn(words, why)
                self.assertIn("page 2", why)
                self.assertNotIn("cut", why)

    def test_u3_a_token_in_a_failing_calls_stderr_is_scrubbed_before_it_reaches_the_refusal(self):
        secret = "ghp_" + "A" * 36
        api = self.serve(FakePullFiles.make(250))
        api.faults[2] = ["http502"] * 3
        base = self.runner

        def leaking(argv, **kw):
            done = base(argv, **kw)
            return subprocess.CompletedProcess(argv, 1, "", done.stderr + " token " + secret) if done.returncode else done
        self.engine.accounts = types.SimpleNamespace(env_for=lambda repo: None, scrub=lambda text: text.replace(secret, "[token]"))
        self.engine.gh_runner = self.runner = leaking
        report, why = self.check()
        self.assertIsNone(report)
        self.assertIn("[token]", why)
        self.assertNotIn(secret, why)

    def test_s7_the_kinds_the_code_uses_are_the_kinds_of_the_table_and_each_has_a_driver_and_a_documentation_line(self):
        module = sb.load_module("delegated_clear")
        used = set()
        for name in ("delegated_clear.py", "impact_hold.py", "fleet-switchboard"):
            text = (BIN / name).read_text()
            used |= set(re.findall(r'FileListError\("([a-z_]+)"', text)) | set(re.findall(r'FILE_REFUSALS\["([a-z_]+)"\]', text))
        self.assertEqual(sorted(used), sorted(module.FILE_REFUSALS), "every kind the code raises is in the table, and every kind of the table is raised")
        docs = (Path(__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        for kind in module.FILE_REFUSALS:
            self.assertIn("`%s`" % kind, docs, "the documentation has a line for %s" % kind)
            self.assertIn(self.words(kind), docs.replace("\n", " "), "...and says its words")

    # ---- (5) a mutant for each member: the suite must fail it

    def mutant(self, old, new, count=1):
        source = (BIN / "delegated_clear.py").read_text()
        self.assertEqual(source.count(old), count, "the mutation point is there: %r" % old)
        module = types.ModuleType("delegated_clear_mutant")
        module.__file__ = str(BIN / "delegated_clear.py")
        exec(compile(source.replace(old, new), "delegated_clear_mutant", "exec"), module.__dict__)
        real = sb.load_module
        return mock.patch.object(sb, "load_module", lambda name, *a, **k: module if name == "delegated_clear" else real(name, *a, **k))

    def survives(self, patcher, check):
        """True when `check` still passes under the mutant (a mutant that is not caught)"""
        with patcher:
            try:
                check()
            except Exception:   # a mutant that fails by any error, an assertion or the real error, is caught
                return False
            return True

    def test_s9_a_mutant_of_every_kind_of_the_table_is_caught(self):
        module = sb.load_module("delegated_clear")
        for kind, template in module.FILE_REFUSALS.items():
            with self.subTest(kind=kind):
                words = "the file list is cut" + "%.0s" * template.count("%")
                mutated = self.mutant('    "%s": "%s",' % (kind, template), '    "%s": "%s",' % (kind, words))
                self.assertFalse(self.survives(mutated, lambda: self.assertIn(self.words(kind), self.drive(kind))), "%s reported as a cut list is not caught" % kind)

    def test_s9_the_one_page_the_retry_the_limit_and_the_count_mutants_are_caught(self):
        sizes = lambda: [self.assertEqual(len(sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)[0]), n) for n in (101, 250)]

        def transient():
            api = self.serve(FakePullFiles.make(250))
            api.faults[2] = ["http502"]
            self.assertEqual(len(sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)[0]), 250)

        def twice():
            api = self.serve(FakePullFiles.make(250))
            api.faults[2] = ["http502"] * 5
            self.drive("read_failed")
            self.assertEqual(api.pages_asked(2), 2)

        def whole():
            for n in (101, 250):
                self.serve(FakePullFiles.make(n))
                self.assertEqual(len(sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)[0]), n)

        def exactly_twice():
            api = self.serve(FakePullFiles.make(250))
            api.faults[2] = ["http502"] * 5
            self.assertIsNone(self.check()[0])
            self.assertEqual(api.pages_asked(2), 2)

        def over():
            self.serve(FakePullFiles.make(3001))
            self.assertIn("more than 3000", self.check()[1])

        def count():
            self.serve(FakePullFiles.make(38), count=40)
            self.assertIn("the list was cut", self.drive("cut"))

        def dropped():
            api = self.serve(FakePullFiles.make(150))
            api.files[0] = "bad"
            self.assertIn("is not a file entry", self.drive("bad_entry"))

        def nocount():
            self.assertIn("count of changed files was missing", self.drive("no_count"))

        def calls():
            api = self.serve(FakePullFiles.make(250))
            sb.load_module("delegated_clear").read_pr_files(sb.impact_carry_get(self.engine, self.runner, self.REPO), self.REPO, 9)
            self.assertEqual(api.pages_asked(4), 0)

        def dup():
            self.assertIn("the changed files repeat", self.drive("duplicate"))
            api = self.serve(FakePullFiles.make(300))
            api.files[100:200] = [dict(f) for f in api.files[0:100]]   # page 2 served as page 1
            self.assertIn("page 2, position 1", self.check()[1])

        mutants = {
            "a missing count is not refused as such": ('    if total is None:\n        raise FileListError("no_count", "missing")', '    if total is None:\n        total = 0', nocount),
            "a count that is not a number is accepted": ('    if not isinstance(total, int) or isinstance(total, bool):\n        raise FileListError("no_count", "not a number (%s)" % type(total).__name__)', '    if False:\n        pass', lambda: self.assertIn("was not a number", self.count_words("12"))),
            "a negative count is accepted": ('    if total < 0:\n        raise FileListError("no_count", "negative (%d)" % total)', '    if False:\n        pass', lambda: self.assertIn("was negative", self.count_words(-1))),
            "a nameless entry is kept": ('    if not isinstance(entry.get("filename"), str) or not entry["filename"]:', '    if False:', lambda: self.assertIn("no file name", self.nameless_words(""))),
            "a non-string name is kept": ('    if not isinstance(entry.get("filename"), str) or not entry["filename"]:', '    if not entry.get("filename"):', lambda: self.assertIn("no file name", self.nameless_words(5))),
            "an empty name is kept": ('    if not isinstance(entry.get("filename"), str) or not entry["filename"]:', '    if not isinstance(entry.get("filename"), str):', lambda: self.assertIn("no file name", self.nameless_words(""))),
            "an entry that is not a file is dropped": ('                raise FileListError("bad_entry", page, position, "a %s, not an object" % type(entry).__name__)', '                continue', dropped),
            "a repeated name is accepted": ('            if entry["filename"] in seen:', '            if False:', dup),
            "break after any page": ('        if len(got) < FILE_PAGE:\n            break', '        if len(got) <= FILE_PAGE:\n            break', whole),
            "break only on an empty page": ('        if len(got) < FILE_PAGE:\n            break', '        if len(got) < 1:\n            break', calls),
            "one page only": ('        if len(got) < FILE_PAGE:\n            break', '        break', whole),
            "every failure is a cut list": ('FileListError("read_failed", what + error_class(err))', 'FileListError("cut", 0, 0)', lambda: self.assertIn("after one retry", self.drive("read_failed"))),
            "no retry": ("    for _ in (1, 2):", "    for _ in (1,):", transient),
            "an endless retry": ("    for _ in (1, 2):", "    for _ in (1, 2, 3, 4, 5, 6):", exactly_twice),
            "no limit": ("    if total > FILE_LIMIT:\n        raise FileListError(\"over_limit\", FILE_LIMIT)", "    pass", over),
            "no count check": ("    if total != len(files):\n        raise FileListError(\"cut\", len(files), total)\n", "", count),
        }
        for label, (old, new, check) in mutants.items():
            with self.subTest(mutant=label):
                self.assertFalse(self.survives(self.mutant(old, new), check), "the mutant %s is not caught" % label)
                check()   # and the real build passes the same check

    # ---- (4) the same line reaches the audit

    def run_clear(self, entry_extra=None, orders=None):
        self.extra_config["delegated_clear"] = {"orders": [dict({"number": 17, "sha256": sb.load_module("delegated_clear").order_hash("order text")}, **(entry_extra or {}))], "charter": 40}
        self.configure()
        if "cos" not in self.world.sessions:
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        for target, value in (("fetch_orders", orders or (lambda reader, charter, repo: ["order one"] * 16 + ["order text"])), ("_intent_reader", lambda runner, now: object())):
            patcher = mock.patch.object(sb, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
        with captured() as (out, err), mock.patch.object(sb, "owner_terminal_problem", lambda *a, **k: None):
            code = sb.main(["impact", "clear", "%s#9" % self.REPO, "--under-order", "17", "--reason", "the review confirms nothing touches credentials"],
                           runner=self.runner, now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def test_s8_the_line_the_person_reads_is_the_line_in_the_audit_for_every_kind(self):
        module = sb.load_module("delegated_clear")
        for kind in module.FILE_REFUSALS:
            if kind == "not_recorded":
                continue   # only a report with no reason can say it; the check never makes one
            with self.subTest(kind=kind):
                self.setup_kind(kind)
                code, out, err = self.run_clear()
                refused = [e for e in self.world.audit() if e["event"] == "gate.hold_clear_refused"][-1]["detail"]["why"]
                self.assertEqual(code, 2, err)
                self.assertIn(self.words(kind), refused)
                self.assertIn(refused, err, "the audit's line is the printed line")

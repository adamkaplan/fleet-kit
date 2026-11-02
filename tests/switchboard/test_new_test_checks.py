"""#1967: tools/new-test-checks reads a new tests/switchboard/test_*.py file before the push, and `preflight-pr` and `bin/cloud-pr-preflight` run it
(scenarios S2 to S8, S10, S13 to S16, U1, U2 of the plan). Run by bin/test-switchboard. Planted trees in temporary directories, real git; the heavy and
test runs of the preflights are faked."""
import contextlib
import functools
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb
from support.paths import BIN, REPO

NTC_TOOL = REPO / "tools" / "new-test-checks"
NTC_CLOUD = BIN / "cloud-pr-preflight"
NTC_ENTRY = BIN / "fleet-switchboard"
NTC_SKIPLINE = "SKIP new-test-checks: tools/new-test-checks is not in this tree"
NTC_SUPPORT = "MOVED_ONE = 1\n\n\ndef moved_two():\n    return 2\n"
NTC_SHEBANG = "#!/bin/sh\nEXCEPTIONS=\"\"\n"
NTC_PASS = "import sys\nsys.exit(0)\n"

NTC_BINDS = "import unittest\n\nMOVED_ONE = 5\n\n\nclass T(unittest.TestCase):\n    pass\n"
NTC_CLEAN = "import unittest\n\nOWN_NAME = 5\n\n\nclass T(unittest.TestCase):\n    pass\n"
NTC_CLASS_READ = "from pathlib import Path\nREPO = Path('.')\n\n\nclass T:\n    SHIM = (REPO / 'shims' / 'gh').read_text()\n"
NTC_METHOD_READ = "from pathlib import Path\nREPO = Path('.')\n\n\nclass T:\n    def test_x(self):\n        return (REPO / 'shims' / 'gh').read_text()\n"
NTC_DOCS_READ = "from pathlib import Path\nREPO = Path('.')\nTEXT = (REPO / 'docs' / 'x.md').read_text()\n"
NTC_TEXT_ONLY = ("# SHIM = (REPO / 'shims' / 'gh').read_text()\n# MOVED_ONE = 1\n# import plan_ledger\n"
                 "NOTE = \"SHIM = (REPO / 'shims' / 'gh').read_text()\\nMOVED_ONE = 1\\nimport plan_ledger\\nwith mock.patch.object(plan_ledger, 'x'): pass\\n\"\n"
                 "ALSO = 'class T:\\n    x = BIN.glob(\"*\")'\n")
NTC_PATCHES = "import unittest\nfrom unittest import mock\nimport plan_ledger\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        with mock.patch.object(plan_ledger, 'read', None):\n            pass\n"
NTC_EXCEPT = "import plan_ledger\n\n\ndef go():\n    try:\n        pass\n    except plan_ledger.LedgerError:\n        pass\n"
NTC_RAISES = "import unittest\nimport plan_ledger\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        with self.assertRaises(plan_ledger.LedgerError):\n            pass\n"
NTC_CONSTANT = "import plan_ledger\nimport relay\nfrom unittest import mock\n\nLIMIT = plan_ledger.LIMIT\n\n\ndef go():\n    with mock.patch.object(relay, 'send', None):\n        return plan_ledger.f()\n"
NTC_LISTS_BIN = "from support.paths import BIN\n\n\nclass TestListsIt:\n    def test_x(self):\n        return sorted(BIN.glob('*.py'))\n"
NTC_NAMES_PS = "from support.paths import BIN\n\nPS = BIN / 'path_suites.py'\n"
NTC_SCANDIR = "from support.paths import BIN\nimport os\n\n\nclass TestListsIt:\n    def test_x(self):\n        return os.scandir(BIN)\n"


@functools.lru_cache(maxsize=None)
def ntc_rule_source():
    """The class TestSupportRebinding of the real bin/test-switchboard, as the one source of a planted copy (read when a test needs it, never at import)."""
    import ast
    source = (BIN / "test-switchboard").read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "TestSupportRebinding")
    return "import ast\nimport unittest\nfrom pathlib import Path\nREPO = Path(__file__).resolve().parent.parent\n\n\n" + ast.get_source_segment(source, node) + "\n"


def ntc_write(root, rel, text, mode=None):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)
    return path


def ntc_plant(root, tool=True):
    """A minimal tree: the rule source, one support module, the entry binding plan_ledger, bin/plan_ledger.py, bin/relay.py, the table test, the rows of path-suites."""
    ntc_write(root, "bin/test-switchboard", ntc_rule_source())
    ntc_write(root, "tests/switchboard/support/mod.py", NTC_SUPPORT)
    ntc_write(root, "bin/fleet-switchboard", "import os\nplan_ledger = object()\n")
    ntc_write(root, "bin/plan_ledger.py", "LIMIT = 1\n")
    ntc_write(root, "bin/relay.py", "LIMIT = 1\n")
    shutil.copy(REPO / "tests" / "switchboard" / "test_bin_set_table.py", Path(root) / "tests/switchboard/test_bin_set_table.py")
    ntc_write(root, "tools/path-suites.json", '{"rows": [{"glob": "bin/path_suites.py", "suites": ["bin/test-path-suites"], "reason": "x"}]}\n')
    if tool:
        shutil.copy(NTC_TOOL, ntc_write(root, "tools/new-test-checks", "", 0o755))
    return Path(root)


def ntc_run(root, *args, tool=NTC_TOOL, cwd=None):
    done = subprocess.run([sys.executable, str(tool)] + [str(a) for a in args], cwd=str(cwd or root), capture_output=True, text=True)
    return done.returncode, [line for line in done.stdout.splitlines() if line]


def ntc_git(root, *args):
    subprocess.run(("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false") + args, cwd=str(root), check=True, capture_output=True)


def ntc_repo(root, tool=True):
    """A planted tree as a git repository on `main` with its tree committed, then the branch `feature` checked out."""
    ntc_plant(root, tool)
    ntc_git(root, "init", "-q", "-b", "main")
    ntc_git(root, "add", "-A")
    ntc_git(root, "commit", "-q", "-m", "base")
    ntc_git(root, "checkout", "-q", "-b", "feature")
    return Path(root)


def ntc_commit(root, message="work"):
    ntc_git(root, "add", "-A")
    ntc_git(root, "commit", "-q", "--allow-empty", "-m", message)


class NtcTree(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ntc-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.root = ntc_plant(self.tmp / "tree")

    def check(self, text, name="test_planted.py", *extra):
        path = ntc_write(self.root, "tests/switchboard/" + name, text)
        return ntc_run(self.root, path, *extra)


class TestNewTestChecksRules(NtcTree):
    """S2 to S6, S13: each rule on planted files."""

    def test_s2_a_moved_name_is_flagged_with_its_line_and_the_rule_is_read_not_copied(self):
        code, lines = self.check(NTC_BINDS)
        self.assertEqual((code, len(lines)), (1, 1), lines)
        self.assertRegex(lines[0], r"test_planted\.py:3: moved-name: ")
        code, lines = self.check(NTC_CLEAN, "test_clean.py")
        self.assertEqual((code, lines), (0, []))
        ntc_write(self.root, "tests/switchboard/support/mod.py", NTC_SUPPORT + "OWN_NAME = 3\n")
        code, lines = self.check(NTC_CLEAN, "test_clean.py")
        self.assertEqual((code, len(lines)), (1, 1), lines)
        self.assertRegex(lines[0], r":3: moved-name: ")

    def test_s3_only_a_read_at_import_outside_the_three_directories_is_flagged(self):
        code, lines = self.check(NTC_CLASS_READ, "test_a.py")
        self.assertEqual((code, len(lines)), (1, 1), lines)
        self.assertRegex(lines[0], r"test_a\.py:6: import-time-read: ")
        for name, text in (("test_b.py", NTC_METHOD_READ), ("test_c.py", NTC_DOCS_READ), ("test_d.py", NTC_TEXT_ONLY)):
            self.assertEqual(self.check(text, name), (0, []), name)

    def test_s4_a_part_module_patched_or_caught_through_is_flagged_as_sb_dot_name(self):
        total = []
        for name, text in (("test_p.py", NTC_PATCHES), ("test_e.py", NTC_EXCEPT), ("test_r.py", NTC_RAISES)):
            code, lines = self.check(text, name)
            self.assertEqual((code, len(lines)), (1, 1), (name, lines))
            self.assertIn("part-module-through-import", lines[0])
            self.assertIn("sb.plan_ledger", lines[0])
            total += lines
        self.assertEqual(len(total), 3)
        self.assertEqual(self.check(NTC_CONSTANT, "test_k.py"), (0, []))

    def test_s5_a_class_that_lists_bin_and_a_row_that_does_not_name_the_file_are_listed_not_flagged(self):
        code, lines = self.check(NTC_LISTS_BIN, "test_l.py")
        self.assertEqual(code, 0, lines)
        self.assertEqual([l for l in lines if l.startswith("table:")], ["table: class TestListsIt lists bin/: it is a row of NEW_BIN_GUARDS or exempt in tests/switchboard/test_bin_set_table.py"])
        self.assertEqual([l for l in lines if l.startswith("run first:")], ["run first: python3 bin/test-switchboard TestTheBinSetClassesAreAllInTheTable"])
        code, lines = self.check(NTC_NAMES_PS, "test_n.py")
        self.assertEqual(code, 0, lines)
        self.assertEqual([l for l in lines if l.startswith("table:")], ["table: tools/path-suites.json row bin/path_suites.py does not name this file"])
        self.assertEqual([l for l in lines if l.startswith("run first:")], ["run first: python3 bin/path_suites.py --check"])
        self.assertEqual(self.check(NTC_CLEAN, "test_o.py"), (0, []))

    def test_s6_text_in_a_comment_or_a_string_is_silent_and_a_missing_rule_exits_2(self):
        self.assertEqual(self.check(NTC_CLEAN, "test_clean.py"), (0, []))
        self.assertEqual(self.check(NTC_TEXT_ONLY, "test_text.py"), (0, []))
        source = (self.root / "bin" / "test-switchboard").read_text()
        (self.root / "bin" / "test-switchboard").write_text(source.replace("class TestSupportRebinding", "class TestSupportRebindingRenamed"))
        code, lines = self.check(NTC_BINDS, "test_planted.py")
        self.assertEqual((code, len(lines)), (2, 1), lines)
        self.assertIn("the rule of the moved names was not found", lines[0])

    def test_s13_the_patterns_are_read_from_the_table_test_not_copied(self):
        self.assertEqual([l for l in self.check(NTC_SCANDIR, "test_s.py")[1] if l.startswith("table:")], [])
        table = self.root / "tests" / "switchboard" / "test_bin_set_table.py"
        text = table.read_text()
        self.assertEqual(text.count("os\\.listdir\\([^)]*bin|"), 1)
        table.write_text(text.replace("os\\.listdir\\([^)]*bin|", "os\\.listdir\\([^)]*bin|os\\.scandir\\(|", 1))
        lines = self.check(NTC_SCANDIR, "test_s.py")[1]
        self.assertEqual(len([l for l in lines if l.startswith("table:")]), 1, lines)


class TestNewTestChecksFiles(unittest.TestCase):
    """S7, S8, U1."""

    def test_s7_with_no_file_it_reads_the_files_the_diff_adds_or_changes_under_the_directory(self):
        with tempfile.TemporaryDirectory(prefix="ntc-") as tmp:
            root = ntc_plant(Path(tmp) / "r")
            ntc_write(root, "tests/switchboard/test_changed.py", NTC_CLEAN)
            ntc_write(root, "tests/switchboard/test_deleted.py", NTC_BINDS)
            ntc_write(root, "tests/switchboard/test_kept.py", NTC_BINDS)
            ntc_git(root, "init", "-q", "-b", "main")
            ntc_commit(root, "base")
            ntc_git(root, "checkout", "-q", "-b", "feature")
            ntc_write(root, "tests/switchboard/test_new_one.py", NTC_BINDS)
            ntc_write(root, "tests/switchboard/test_new_two.py", NTC_BINDS)
            ntc_write(root, "tests/switchboard/test_changed.py", NTC_BINDS)
            ntc_write(root, "tests/other/test_outside.py", NTC_BINDS)
            ntc_write(root, "tests/switchboard/helper.py", NTC_BINDS)
            (root / "tests/switchboard/test_deleted.py").unlink()
            ntc_commit(root)
            code, lines = ntc_run(root, "--base", "main")
            self.assertEqual(code, 1, lines)
            self.assertEqual(sorted(l.split(":")[0] for l in lines), ["tests/switchboard/test_changed.py", "tests/switchboard/test_new_one.py", "tests/switchboard/test_new_two.py"])

    def test_s8_the_existing_test_files_and_this_one_give_no_finding(self):
        files = sorted((REPO / "tests" / "switchboard").glob("test_*.py"))
        self.assertGreaterEqual(len(files), 47)
        done = subprocess.run([sys.executable, str(NTC_TOOL)] + [str(f) for f in files], capture_output=True, text=True, cwd=str(REPO))
        findings = [l for l in done.stdout.splitlines() if not l.startswith(("table:", "run first:"))]
        self.assertEqual((done.returncode, findings), (0, []), done.stdout)
        done = subprocess.run([sys.executable, str(NTC_TOOL), str(Path(__file__).resolve())], capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_u1_it_writes_nothing_and_exits_0_1_or_2(self):
        with tempfile.TemporaryDirectory(prefix="ntc-") as tmp:
            root = ntc_repo(Path(tmp) / "r")
            for name, text in (("test_a.py", NTC_BINDS), ("test_b.py", NTC_CLEAN), ("test_c.py", NTC_LISTS_BIN)):
                ntc_write(root, "tests/switchboard/" + name, text)
            ntc_commit(root)
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
            codes = [ntc_run(root, "--base", "main")[0], ntc_run(root, root / "tests/switchboard/test_a.py")[0], ntc_run(root, root / "tests/switchboard/test_b.py")[0],
                     ntc_run(root, "--bogus")[0], ntc_run(root, "--base", "nothing-here")[0], ntc_run(root, root / "tests/switchboard/test_c.py")[0]]
            self.assertEqual(codes, [1, 1, 0, 2, 2, 0])
            self.assertEqual({str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}, before)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True).stdout
            self.assertEqual(status, "")


def ntc_load_entry(path):
    """The entry loaded from `path` (a copy) with the other files of bin/ beside it as links, so that its loader of part modules finds them."""
    directory = Path(path).parent
    for item in BIN.iterdir():
        if item.name != "fleet-switchboard" and not (directory / item.name).exists():
            (directory / item.name).symlink_to(item)
    loader = importlib.machinery.SourceFileLoader("ntc_entry_" + str(abs(hash(str(path)))), str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def ntc_preflight(entry, repo, merged=False):
    """`cmd_preflight_pr` of `entry` on a planted repository: git runs for real, the heavy run is the command itself, the test classes are faked. (code, stdout, stderr)."""
    def runner(argv, **kwargs):
        argv = list(argv)
        if "heavy" in argv[:3] and "--" in argv:
            argv = argv[argv.index("--") + 1:]
            kwargs.pop("env", None)
        elif argv[0] != "git":
            return mock.Mock(returncode=1, stdout="", stderr="")
        kwargs.setdefault("cwd", str(repo))
        return subprocess.run(argv, **kwargs)
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(str(repo))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(entry, "cmd_preflight_pr_in", return_value=0):
            code = entry.cmd_preflight_pr(types.SimpleNamespace(merged=merged), runner, __import__("time").time)
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class TestNewTestChecksInPreflightPr(unittest.TestCase):
    """S14, S15: `preflight-pr` and `preflight-pr --merged` run the command."""

    def planted(self, text=None, outside=None, tool=True):
        tmp = tempfile.mkdtemp(prefix="ntc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = ntc_repo(Path(tmp) / "r", tool)
        if text is not None:
            ntc_write(root, "tests/switchboard/test_head.py", text)
        if outside is not None:
            ntc_write(root, "lib/other.py", outside)
        ntc_commit(root)
        return root

    def test_s14_a_head_that_binds_a_moved_name_fails_and_a_clean_one_passes_in_both_forms(self):
        for merged in (False, True):
            code, out, err = ntc_preflight(sb, self.planted(NTC_BINDS), merged)
            self.assertEqual(code, 1, (merged, out, err))
            self.assertEqual(len(re.findall(r"^FAIL new-test-checks \(\d+s\)$", out, re.M)), 1, out)
            self.assertEqual(len(re.findall(r"moved-name", err)), 1, err)
            code, out, err = ntc_preflight(sb, self.planted(NTC_CLEAN), merged)
            self.assertEqual(code, 0, (merged, out, err))
            self.assertEqual(len(re.findall(r"^PASS new-test-checks \(\d+s\)$", out, re.M)), 1, out)

    def test_s15_no_test_file_prints_nothing_and_a_tree_without_the_tool_prints_the_skip_line(self):
        for merged in (False, True):
            for root in (self.planted(None, "X = 1\n"), self.planted(None, None)):
                code, out, err = ntc_preflight(sb, root, merged)
                self.assertEqual((code, "new-test-checks" in out + err), (0, False), (merged, out, err))
            code, out, err = ntc_preflight(sb, self.planted(NTC_BINDS, None, tool=False), merged)
            self.assertEqual((code, out.count(NTC_SKIPLINE)), (0, 1), (merged, out, err))


class TestNewTestChecksInCloudPreflight(unittest.TestCase):
    """S16: bin/cloud-pr-preflight, full and quick mode, its test files stubbed as passing."""

    def planted(self, text=None, tool=True):
        tmp = tempfile.mkdtemp(prefix="ntc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp + "/r"
        ntc_plant(root, tool)
        ntc_write(root, "bin/test-cloud-launcher", NTC_SHEBANG, 0o755)
        ntc_write(root, "bin/test-cloud-launcher.chain", "cloud/launcher/tests/test_a.py\ncloud/worker/tests/test_b.py\n")
        ntc_write(root, "cloud/launcher/tests/test_a.py", NTC_PASS)
        ntc_write(root, "cloud/worker/tests/test_b.py", NTC_PASS)
        ntc_git(root, "init", "-q", "-b", "main")
        ntc_commit(root, "base")
        ntc_git(root, "checkout", "-q", "-b", "feature")
        ntc_write(root, "lib/other.py", "X = 1\n")
        if text is not None:
            ntc_write(root, "tests/switchboard/test_head.py", text)
        ntc_commit(root)
        return Path(root)

    def run_script(self, root, quick, script=NTC_CLOUD):
        env = dict(os.environ, CLOUD_PREFLIGHT_TIMEOUT="20", CLOUD_PREFLIGHT_BASE="main")
        done = subprocess.run([sys.executable, str(script)] + (["--quick"] if quick else []), cwd=str(root), env=env, capture_output=True, text=True)
        return done.returncode, done.stdout

    def test_s16_the_block_is_printed_only_for_a_changed_switchboard_test_file_in_both_modes(self):
        for quick in (False, True):
            code, out = self.run_script(self.planted(NTC_BINDS), quick)
            self.assertEqual(code, 1, out)
            self.assertEqual(out.count("== new test file checks (tools/new-test-checks) =="), 1, out)
            self.assertEqual((out.count("new-test-checks: FAIL"), out.count("moved-name"), out.count("RESULT: FAIL")), (1, 1, 1), out)
            code, out = self.run_script(self.planted(NTC_CLEAN), quick)
            self.assertEqual((out.count("new-test-checks: ok"), out.count("new-test-checks: FAIL"), out.count("RESULT: FAIL")), (1, 0, 0), out)
            code, out = self.run_script(self.planted(None), quick)
            self.assertNotIn("new-test-checks", out)
            code, out = self.run_script(self.planted(NTC_CLEAN, tool=False), quick)
            self.assertEqual((out.count(NTC_SKIPLINE), len(re.findall("new-test-checks: (?:ok|FAIL)", out))), (1, 0), out)
            self.assertNotIn("RESULT: FAIL", out)

    def test_s16_a_branch_without_a_test_file_prints_what_the_script_printed_before_the_block(self):
        source = NTC_CLOUD.read_text()
        start, stop = source.index('        rc, out = git("diff", "--name-only", "--no-renames", "--diff-filter=AM", mb, "--", "tests/switchboard/"'), source.index("        sel_of = {}\n")
        self.assertEqual(source.count("        sel_of = {}\n"), 1)
        with tempfile.TemporaryDirectory(prefix="ntc-") as tmp:
            before = Path(tmp) / "before"
            before.write_text(source[:start] + source[stop:])
            root = self.planted(None)
            self.assertEqual(self.run_script(root, False, before), self.run_script(root, False))


class TestNewTestChecksMutants(unittest.TestCase):
    """S10: six mutants, each on a source copy and applied exactly once; each control (the unmutated copy) passes first and the mutant fails the check on its own text."""

    def mutate(self, path, old, new):
        source = Path(path).read_text()
        self.assertEqual(source.count(old), 1, old)
        directory = Path(tempfile.mkdtemp(prefix="ntc-"))
        self.addCleanup(shutil.rmtree, str(directory), True)
        target = directory / Path(path).name
        target.write_text(source.replace(old, new))
        target.chmod(0o755)
        return target

    def tool_case(self, text, rule, old, new):
        tmp = Path(tempfile.mkdtemp(prefix="ntc-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        root = ntc_plant(tmp / "t")
        planted = ntc_write(root, "tests/switchboard/test_m.py", text)
        control = ntc_run(root, planted, tool=NTC_TOOL)
        self.assertEqual(control[0], 1, control)
        self.assertTrue(any(": %s: " % rule in line for line in control[1]), control)
        mutant = ntc_run(root, planted, tool=self.mutate(NTC_TOOL, old, new))
        self.assertFalse(any(": %s: " % rule in line for line in mutant[1]), "the mutant still flags %s: %r" % (rule, mutant))
        self.assertEqual(mutant[0], 0, mutant)

    def test_mutant_moved_name_that_stops_flagging(self):
        self.tool_case(NTC_BINDS, "moved-name", "sorted(check_moved(source, root) + check_reads(tree)", "sorted([] + check_reads(tree)")

    def test_mutant_import_time_read_that_stops_flagging(self):
        self.tool_case(NTC_CLASS_READ, "import-time-read", "+ check_reads(tree) +", "+ [] +")

    def test_mutant_part_module_that_stops_flagging(self):
        self.tool_case(NTC_PATCHES, "part-module-through-import", "+ check_parts(tree, root))", "+ [])")

    def test_mutant_table_list_that_stops_listing(self):
        tmp = Path(tempfile.mkdtemp(prefix="ntc-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        root = ntc_plant(tmp / "t")
        planted = ntc_write(root, "tests/switchboard/test_m.py", NTC_LISTS_BIN)
        self.assertTrue(any(l.startswith("table: class TestListsIt") for l in ntc_run(root, planted)[1]))
        mutant = ntc_run(root, planted, tool=self.mutate(NTC_TOOL, "lines, first = table_lines(source, tree, root, rel)", "lines, first = [], []"))
        self.assertFalse(any(l.startswith("table:") for l in mutant[1]), mutant)

    def test_mutant_preflight_pr_without_the_call(self):
        old = "    code = EXIT_ERROR if run_new_test_checks(runner, root, now) and code == EXIT_OK else code\n"
        with tempfile.TemporaryDirectory(prefix="ntc-") as tmp:
            root = ntc_repo(Path(tmp) / "r")
            ntc_write(root, "tests/switchboard/test_head.py", NTC_BINDS)
            ntc_commit(root)
            control = ntc_preflight(sb, root)
            self.assertEqual(control[0], 1, control)
            self.assertIn("FAIL new-test-checks", control[1])
            entry = ntc_load_entry(self.mutate(NTC_ENTRY, old, "    pass\n"))
            code, out, err = ntc_preflight(entry, root)
            self.assertNotIn("FAIL new-test-checks", out, "the mutant still runs the check")
            self.assertEqual(code, 0)

    def test_mutant_cloud_preflight_without_the_call(self):
        scenario = TestNewTestChecksInCloudPreflight("test_s16_the_block_is_printed_only_for_a_changed_switchboard_test_file_in_both_modes")
        scenario.addCleanup = self.addCleanup
        root = scenario.planted(NTC_BINDS)
        code, out = scenario.run_script(root, False)
        self.assertEqual(code, 1, out)
        self.assertIn("new-test-checks: FAIL", out)
        old = '        if any(x.count("/") == 2 and os.path.basename(x).startswith("test_")'
        mutant = self.mutate(NTC_CLOUD, old, old.replace("if any(", "if False and any("))
        code, out = scenario.run_script(root, False, mutant)
        self.assertNotIn("new-test-checks: FAIL", out, "the mutant still runs the check")
        self.assertIn("RESULT: PASS", out)


class TestNewTestChecksDocs(unittest.TestCase):
    """S11 (the docs claim) and U2 (nothing else runs the command)."""
    OLD = "Before any push, run bin/cloud-pr-preflight at the repository root and paste its three sections in the pull request comment; "

    def test_s11_the_documentation_names_the_command_and_the_quickstart_keeps_its_sentence(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        for needle in ("tools/new-test-checks", "moved-name", "import-time-read", "part-module-through-import", "table:", "only reads", "before pushing"):
            self.assertIn(needle, text)
        quick = (REPO / "docs" / "cloud-operator-quickstart.md").read_text()
        self.assertEqual(quick.count(self.OLD), 1)
        self.assertEqual(quick.count("the checks of tools/new-test-checks, between the first and the second section"), 1)

    def test_u2_the_workflow_and_the_hooks_do_not_run_the_command(self):
        for folder in (REPO / ".github", REPO / "hooks", REPO / ".githooks"):
            for path in folder.rglob("*") if folder.is_dir() else []:
                if path.is_file():
                    self.assertNotIn("new-test-checks", path.read_text(errors="replace"), str(path))


if __name__ == "__main__":
    unittest.main()

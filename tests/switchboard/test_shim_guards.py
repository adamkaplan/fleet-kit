"""#1909: a new module under shims/ is seen by the new-module guards as a new module under bin/ is (scenarios S2-S9, U1-U5). Run by bin/test-switchboard.
Every test RUNS the real tool or function on a planted directory; only the documentation claim reads text."""
import ast
import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.paths import BIN, REPO

TOOL = REPO / "tools" / "new-module-demands"
MATRIX = REPO / "docs" / "review-laws" / "impact-matrix.json"
PATH_SUITES = BIN / "path_suites.py"
SWITCHBOARD_FILE = BIN / "fleet-switchboard"
PLAIN = "def format_line(x):\n    return x\n"
# The output of the tool for a planted bin/y.py, recorded from a clean worktree of the base branch before the change
# (the closure line is the live state of derive-closure, which this change does not touch).
RECORDED_BIN_BLOCK = """bin/y.py:
  guards: 9 classes run for a new file under bin/:
    - TestAuthorityModulesGuard: a module that decides who may clear, hold or merge is listed or exempted by name
    - TestAgentRegistryInventory: every module that reads or writes an agent record is in the registry inventory
    - TestImpactHold module-name guard: every module the clear runs is judged by name
    - bin/check-public-names: no private name in a tracked file
    - TestArchitectureDocument: every file under bin/ is a row of the Units table of docs/architecture.md
    - TestRecordJsonReaders: every module under bin/ that reads a record's JSON is in the table of readers
    - TestTheReaperOnlyInstall: every function the installer calls is classified (Linux and macOS)
    - TestRecordingCommands: every command that reaches a ledger append is a recording command or named exempt
    - TestDerivedClosure: the closure of the delegated clear is derived over the entry and every part (tests/switchboard/test_closure.py)
  authority code: no
  matrix: no matrix line is demanded
  units: add this row to the Units table of docs/architecture.md (header `| Unit | Layer | Job | Also may use |`, rows sorted by name), after the row of `work_sync.py`:
    | `y.py` | <layer 0-6> | <the one job of the module> | |
  closure: %s
"""


def shim_load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shim_plant(tmp, rel, source=PLAIN):
    path = Path(tmp) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return rel


def shim_run(tool, root, *paths):
    return subprocess.run([sys.executable, str(tool), "--root", str(root)] + list(paths), capture_output=True, text=True, cwd=str(root))


def shim_git(root, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"] + list(args), cwd=str(root), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done


def shim_repo_adding(*rels):
    """A planted repository: branch main, then a feature branch that ADDS the given files (committed)."""
    root = tempfile.mkdtemp(prefix="shim-guards-")
    shim_git(root, "init", "-q", "-b", "main")
    shim_plant(root, "README.md", "x\n")
    shim_git(root, "add", "-A")
    shim_git(root, "commit", "-qm", "base")
    shim_git(root, "checkout", "-q", "-b", "feature")
    for rel in rels:
        shim_plant(root, rel)
    shim_git(root, "add", "-A")
    shim_git(root, "commit", "-qm", "add", "--allow-empty")
    return root


def shim_live_closure():
    done = subprocess.run([sys.executable, str(REPO / "tools" / "derive-closure"), "--check"], capture_output=True, text=True, cwd=str(REPO))
    first = ((done.stdout or done.stderr).strip().splitlines() or ["(no output)"])[0]
    return "derive-closure --check: %s (exit %d): %s" % ({0: "ok", 1: "differs"}.get(done.returncode, "could not run"), done.returncode, first)


def shim_check_tool_accepts_shims(case, done):
    case.assertEqual(done.returncode, 0, "new-module-demands refused a path under shims/: " + done.stdout)
    case.assertIn("guards: 3 classes run for a new file under shims/", done.stdout)


def shim_check_added_and_rows(case, ps, root):
    added = ps.added_paths(root, "main")
    case.assertEqual(added, ["shims/x.py"], "added_paths drops a file under shims/ the head adds")
    case.assertEqual(len(ps.guard_rows(added)), 3, "guard_rows returns no row of NEW_SHIM_GUARDS for an added shims/ file")


class TestShimGuardsTool(unittest.TestCase):
    def test_s2_a_shim_a_bin_and_a_path_outside_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = shim_run(TOOL, tmp, shim_plant(tmp, "shims/x.py"))
            shim_check_tool_accepts_shims(self, shim)
            ps = shim_load(PATH_SUITES, "shim_ps_tool")
            self.assertEqual(len(ps.NEW_SHIM_GUARDS), 3)
            for row in ps.NEW_SHIM_GUARDS:
                self.assertIn("    - %s: %s" % (row[0], row[4]), shim.stdout)
            hold = json.loads(MATRIX.read_text())["owner_hold"]
            read = [g for v in hold["top_tier_paths"].values() for g in v if g == "shims/**"] + [g for r in hold["floor"].values() for g in r["globs"] if g == "shims/**"]
            self.assertEqual(len(read), 2)
            self.assertEqual((shim.stdout.count("identity: shims/**"), shim.stdout.count(".globs: shims/**")), (1, 1))
            for line in ("  authority code: no", "  matrix: no line is demanded: shims/** is a top tier identity path and a floor glob", "  units: none", "  closure: not in the closure of the daemon"):
                self.assertIn(line, shim.stdout)
            binned = shim_run(TOOL, tmp, shim_plant(tmp, "bin/y.py"))
            self.assertEqual((binned.returncode, binned.stdout), (0, RECORDED_BIN_BLOCK % shim_live_closure()))
            outside = shim_run(TOOL, tmp, shim_plant(tmp, "plugins/z.py"))
            self.assertEqual(outside.returncode, 2)
            self.assertIn("is not a file directly under bin/ or shims/", outside.stdout)

    def test_s6_the_six_existing_modules_demand_nothing(self):
        modules = sorted(p.name for p in (REPO / "shims").glob("*.py"))
        self.assertEqual(len(modules), 6)
        yes = {"gh_hold.py": 3, "stash_guard.py": 1}
        total = 0
        for name in modules:
            done = shim_run(TOOL, REPO, "shims/" + name)
            self.assertEqual(done.returncode, 0, (name, done.stdout))
            for needle in ("  units: none", "  matrix: no line is demanded", "closure: not in the closure"):
                self.assertIn(needle, done.stdout, name)
            self.assertNotIn("add this row", done.stdout, name)
            self.assertNotIn("add \"", done.stdout, name)
            line = [l for l in done.stdout.splitlines() if l.startswith("  authority code:")][0]
            if name in yes:
                self.assertTrue(line.startswith("  authority code: yes"), (name, line))
                self.assertEqual(len(re.search(r"functions: ([^;]*);", line).group(1).split(", ")), yes[name], name)
                total += yes[name]
            else:
                self.assertEqual(line, "  authority code: no", name)
        self.assertEqual(total, 4)

    def test_s8_the_globs_are_read_from_the_matrix_not_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("tools/new-module-demands", "bin/path_suites.py", "docs/architecture.md"):
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(REPO / rel), str(Path(tmp) / rel))
            matrix = json.loads(MATRIX.read_text())
            hold = matrix["owner_hold"]

            def strip(node):
                if isinstance(node, list):
                    return [strip(i) for i in node if i != "shims/**"]
                if isinstance(node, dict):
                    return {k: strip(v) for k, v in node.items()}
                return node
            (Path(tmp) / MATRIX.relative_to(REPO)).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / MATRIX.relative_to(REPO)).write_text(json.dumps(strip(matrix)))
            path = shim_plant(tmp, "shims/x.py")
            real = shim_run(Path(tmp) / "tools" / "new-module-demands", tmp, path)
            self.assertEqual(real.returncode, 0)
            self.assertIn("matrix: no glob holds this path", real.stdout)
            self.assertIn("owner_hold.top_tier_paths.<class>", real.stdout)
            self.assertIn("owner_hold.floor[\"<repo>\"].globs", real.stdout)
            self.assertNotIn("shims/**", real.stdout)
            with tempfile.TemporaryDirectory() as other:
                full = shim_run(TOOL, other, shim_plant(other, "shims/x.py"))
            repo_name = sorted(hold["floor"])[0]
            self.assertIn("owner_hold.top_tier_paths.identity: shims/**", full.stdout)
            self.assertIn("owner_hold.floor[\"%s\"].globs: shims/**" % repo_name, full.stdout)


class TestShimGuardRows(unittest.TestCase):
    ps = None

    @classmethod
    def setUpClass(cls):
        cls.ps = shim_load(PATH_SUITES, "shim_ps_rows")
        loader = importlib.machinery.SourceFileLoader("shim_sb_rows", str(SWITCHBOARD_FILE))
        cls.sb = importlib.util.module_from_spec(importlib.util.spec_from_loader("shim_sb_rows", loader))
        loader.exec_module(cls.sb)

    def test_s3_added_paths_and_guard_rows_for_four_planted_changes(self):
        ps, nb, ns = self.ps, len(self.ps.NEW_BIN_GUARDS), len(self.ps.NEW_SHIM_GUARDS)
        self.assertEqual(ns, 3)
        for rels, expected in ((["shims/x.py"], ns), (["bin/y.py"], nb), (["shims/x.py", "bin/y.py"], nb + ns), (["docs/n.md"], 0)):
            root = shim_repo_adding(*rels)
            added = ps.added_paths(root, "main")
            self.assertEqual(added, sorted(r for r in rels if not r.startswith("docs/")), rels)
            self.assertEqual(len(ps.guard_rows(added)), expected, rels)
        self.assertEqual(ps.guard_rows([]), [])

    def run_preflight(self, rels, code=0):
        root = shim_repo_adding(*rels)
        added = self.sb.preflight_added(subprocess.run, root)
        runner = mock.Mock(return_value=mock.Mock(returncode=code, stdout="", stderr=""))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(self.ps, "guard_missing", lambda r, g: ""):
            failed = self.sb.run_new_bin_guards(self.ps, runner, root, added, lambda: 0.0)
        return added, failed, out.getvalue()

    def test_s3_the_preflight_runs_the_shim_rows_for_a_change_that_adds_only_a_shim(self):
        added, failed, out = self.run_preflight(["shims/x.py"])
        self.assertEqual(added, ["shims/x.py"])
        verdicts = [l for l in out.splitlines() if l.split()[0] in ("PASS", "FAIL")]
        self.assertEqual((len(verdicts), failed), (3, 0))
        self.assertTrue(all(l.startswith("PASS") for l in verdicts), verdicts)
        self.assertIn("new file under bin/ or shims/ (shims/x.py): the 3 guards every new file there must pass", out)
        added, failed, out = self.run_preflight(["shims/x.py"], code=1)
        verdicts = [l for l in out.splitlines() if l.split()[0] in ("PASS", "FAIL")]
        self.assertEqual((len(verdicts), failed, {l.split()[0] for l in verdicts}), (3, 3, {"FAIL"}))

    def test_s3_a_change_that_adds_both_runs_every_row_once_each(self):
        added, failed, out = self.run_preflight(["shims/x.py", "bin/y.py"])
        verdicts = [l for l in out.splitlines() if l.split()[0] in ("PASS", "FAIL")]
        self.assertEqual(len(verdicts), len(self.ps.NEW_BIN_GUARDS) + len(self.ps.NEW_SHIM_GUARDS))
        self.assertEqual(self.sb.preflight_added(subprocess.run, shim_repo_adding("docs/n.md")), [])


class TestShimHygienePlanted(unittest.TestCase):
    @staticmethod
    def faults(directory):
        import test_cases_05
        return test_cases_05.TestShimHygiene.module_faults(directory)

    def test_s4_a_foreign_import_and_a_personal_name_are_refused_naming_module_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim_plant(tmp, "clean.py", "import json\nimport os.path\n")
            shim_plant(tmp, "foreign.py", "import json\nimport " + "requests\n")
            shim_plant(tmp, "named.py", "import json\n# " + "adk" + "aplan wrote this\n")
            self.assertEqual(self.faults(tmp), ["foreign.py:2: imports requests, not the standard library", "named.py:2: holds a personal name"])
        self.assertEqual(self.faults(REPO / "shims"), [])


class TestShimGuardMutants(unittest.TestCase):
    def mutate(self, tmp, rel, old, new, patch_root=False):
        text = (REPO / rel).read_text()
        self.assertEqual(text.count(old), 1, "the mutation applies exactly once")
        mutant = text.replace(old, new)
        if patch_root:
            root_line = "ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
            self.assertEqual(mutant.count(root_line), 1)
            mutant = mutant.replace(root_line, "ROOT = %r" % str(REPO))
        target = Path(tmp) / "mutant" / Path(rel).name
        target.parent.mkdir(exist_ok=True)
        target.write_text(mutant)
        target.chmod(0o755)
        return target

    def test_s5_the_bin_only_regex_of_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = shim_plant(tmp, "shims/x.py")
            shim_check_tool_accepts_shims(self, shim_run(TOOL, tmp, path))   # control
            mutant = self.mutate(tmp, "tools/new-module-demands", 'r"^(bin|shims)/[^/]+$"', 'r"^bin/[^/]+$"', True)
            with self.assertRaises(AssertionError) as caught:
                shim_check_tool_accepts_shims(self, shim_run(mutant, tmp, path))
            self.assertIn("new-module-demands refused a path under shims/", str(caught.exception))

    def test_s5_the_bin_only_filter_of_added_paths(self):
        root = shim_repo_adding("shims/x.py")
        shim_check_added_and_rows(self, shim_load(PATH_SUITES, "shim_ps_control_a"), root)   # control
        with tempfile.TemporaryDirectory() as tmp:
            mutant = self.mutate(tmp, "bin/path_suites.py", 'p.startswith(("bin/", "shims/"))', 'p.startswith("bin/")')
            with self.assertRaises(AssertionError) as caught:
                shim_check_added_and_rows(self, shim_load(mutant, "shim_ps_mutant_a"), root)
            self.assertIn("added_paths drops a file under shims/ the head adds", str(caught.exception))

    def test_s5_the_dropped_shims_branch_of_guard_rows(self):
        root = shim_repo_adding("shims/x.py")
        shim_check_added_and_rows(self, shim_load(PATH_SUITES, "shim_ps_control_g"), root)   # control
        with tempfile.TemporaryDirectory() as tmp:
            mutant = self.mutate(tmp, "bin/path_suites.py", " + [(g[1], [g[2]] if g[2] else [], guard_command(g), shims) for g in NEW_SHIM_GUARDS if shims]", "")
            with self.assertRaises(AssertionError) as caught:
                shim_check_added_and_rows(self, shim_load(mutant, "shim_ps_mutant_g"), root)
            self.assertIn("guard_rows returns no row of NEW_SHIM_GUARDS for an added shims/ file", str(caught.exception))


class TestShimGuardDocs(unittest.TestCase):
    def test_s7_the_documentation_and_the_matrix_line(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertEqual(len(re.findall(r"^A new module under `shims/` \(#1909\) is guarded as a new bin module is:.*the 3 rows of `NEW_SHIM_GUARDS`", text, re.M)), 1)
        self.assertEqual(len(re.findall(r"^Not put in scope, and why: the authority-modules guard.*the Units guard.*`TestDerivedClosure`", text, re.M)), 1)
        matrix = MATRIX.read_text()
        self.assertEqual(matrix.count("code that is not a .py file in bin/ (bin/fleet-switchboard, scripts, shims, plugins, agents)"), 1)

    def test_u5_the_new_tests_do_not_measure_a_pull_request(self):
        source = Path(__file__).read_text()
        forbidden = ["git" + " diff", "origin/" + "integration", "diff" + "stat", "num" + "stat", "changed_" + "files"]
        self.assertEqual([w for w in forbidden if w in source.replace('"git", "-c"', "")], [])

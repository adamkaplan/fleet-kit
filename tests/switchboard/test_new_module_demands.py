"""#1620: tools/new-module-demands prints what the guards will demand of a new file under bin/ (scenarios S2-S6 and U3). Run by bin/test-switchboard."""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from support.paths import BIN, REPO

TOOL = REPO / "tools" / "new-module-demands"
AUTHORITY = "def may_clear_hold(pr):\n    return pr == 'operator'\n"
PLAIN = "def format_line(x):\n    return x\n"


def load_tool():
    loader = SourceFileLoader("new_module_demands_under_test", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run(tool, root, *paths, env=None):
    return subprocess.run([sys.executable, str(tool), "--root", str(root)] + list(paths), capture_output=True, text=True, env=env, cwd=str(root))


def plant(tmp, name, source):
    (Path(tmp) / "bin").mkdir(exist_ok=True)
    (Path(tmp) / "bin" / name).write_text(source)
    return "bin/" + name


def guard_class():
    import test_cases_03
    return test_cases_03.TestAuthorityModulesGuard


def check_authority_output(case, done, path):
    """The S2 demands: 9 classes, both matrix entries with their JSON paths, the units row, exit 0."""
    case.assertEqual(done.returncode, 0, done.stderr)
    out = done.stdout
    ps = load_module_path_suites()
    case.assertEqual(len(ps.NEW_BIN_GUARDS), 9)
    for row in ps.NEW_BIN_GUARDS:
        case.assertIn("- %s:" % row[0], out)
    case.assertIn("authority code: yes", out)
    case.assertIn("owner_hold.top_tier_paths.authority", out)
    case.assertIn('owner_hold.floor["adamkaplan/fleet-kit"].globs', out)
    case.assertEqual(out.count('add "%s" to ' % path), 2)
    case.assertEqual(out.count("| `%s` |" % Path(path).name), 1)
    case.assertIn("closure: derive-closure --check", out)


def load_module_path_suites():
    spec = importlib.util.spec_from_file_location("path_suites_for_test", str(BIN / "path_suites.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNewModuleDemands(unittest.TestCase):
    def test_s2_a_planted_authority_module_gets_nine_classes_both_matrix_entries_and_the_units_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = plant(tmp, "new_gate.py", AUTHORITY)
            done = run(TOOL, tmp, path)
            check_authority_output(self, done, path)
            self.assertIn("may_clear_hold", done.stdout)
            self.assertNotRegex(done.stdout, r"add [^\n]*authority_modules")

    def test_s3_a_planted_plain_module_has_no_matrix_line_but_the_classes_and_the_units_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = plant(tmp, "plain_thing.py", PLAIN)
            done = run(TOOL, tmp, path)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("authority code: no", done.stdout)
            self.assertIn("no matrix line is demanded", done.stdout)
            self.assertNotIn("owner_hold.", done.stdout)
            self.assertEqual(len(re.findall(r"^    - ", done.stdout, re.M)), 9)
            self.assertEqual(done.stdout.count("| `plain_thing.py` |"), 1)

    def test_s4_a_path_outside_bin_and_a_missing_path_exit_2_with_one_line_and_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake:
            gh = Path(fake) / "gh"
            gh.write_text("#!/bin/sh\ntouch %s/gh-was-called\n" % fake)
            gh.chmod(0o755)
            env = {"PATH": fake + ":/usr/bin:/bin"}
            before = sorted(p.name for p in Path(tmp).rglob("*"))
            for args in (["docs/x.py"], ["bin/missing.py"]):
                done = run(TOOL, tmp, *args, env=env)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(len(done.stdout.strip().splitlines()), 1)
                self.assertEqual(done.stderr, "")
            self.assertEqual(sorted(p.name for p in Path(tmp).rglob("*")), before)
            self.assertFalse((Path(fake) / "gh-was-called").exists())

    def test_s5_the_tool_and_the_guard_name_the_same_modules(self):
        tool, guard = load_tool(), guard_class()
        matrix = json.loads((REPO / "docs" / "review-laws" / "impact-matrix.json").read_text())["owner_hold"]["authority_modules"]
        vocab, literals = matrix["vocabulary"], matrix["role_literals"]

        def both(directory):
            by_tool = set()
            for p in sorted(Path(directory).glob("*.py")):
                names, hits = tool.authority_code(p.read_text(), vocab, literals)
                if names or hits:
                    by_tool.add("bin/" + p.name)
            by_guard = set(guard.authority_functions(directory, vocab)) | set(guard.role_checks(directory, literals))
            return by_tool, by_guard
        by_tool, by_guard = both(BIN)
        self.assertEqual(by_tool ^ by_guard, set(), "modules named by one and not the other on the real bin/")
        self.assertTrue(by_tool, "the real bin/ has authority modules")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text(AUTHORITY)
            (Path(tmp) / "b.py").write_text(PLAIN)
            (Path(tmp) / "c.py").write_text("def f(r):\n    return r in ('chief-of-staff', 'x')\n")
            by_tool, by_guard = both(tmp)
            self.assertEqual(by_tool, by_guard)
            self.assertEqual(by_tool, {"bin/a.py", "bin/c.py"})

    def test_s6_a_mutant_that_drops_the_floor_glob_demand_fails_the_s2_check(self):
        text = TOOL.read_text()
        self.assertIn('out.append("    2. add', text)
        mutant = re.sub(r'\n        out\.append\("    2\. add[^\n]*', "", text)
        self.assertNotEqual(mutant, text)
        mutant = mutant.replace("ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))", "ROOT = %r" % str(REPO))
        with tempfile.TemporaryDirectory() as tmp:
            path = plant(tmp, "new_gate.py", AUTHORITY)
            (Path(tmp) / "mutant").write_text(mutant)
            check_authority_output(self, run(TOOL, tmp, path), path)
            with self.assertRaises(AssertionError):
                check_authority_output(self, run(Path(tmp) / "mutant", tmp, path), path)

    def test_u3_the_tool_holds_no_typed_class_name_or_vocabulary_and_runs_no_guard(self):
        source = TOOL.read_text()
        ps = load_module_path_suites()
        for row in ps.NEW_BIN_GUARDS:
            self.assertNotIn(row[0], source.replace("NEW_BIN_GUARDS", ""), row[0])
        matrix = json.loads((REPO / "docs" / "review-laws" / "impact-matrix.json").read_text())["owner_hold"]["authority_modules"]
        typed = [w for w in matrix["vocabulary"] + matrix["role_literals"] if re.search(r"""['"]%s['"]""" % re.escape(w), source)]
        self.assertEqual(set(typed) - {"authority"}, set(), "only the class key `authority` of top_tier_paths is named, not a list of the vocabulary")
        self.assertNotIn("test-switchboard", source.replace("tests/switchboard/test_new_module_demands.py", ""))
        self.assertNotIn("urllib", source)
        self.assertNotRegex(source, r"\bopen\([^)]*[\"']w")

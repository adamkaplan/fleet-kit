"""#1498: every bin/test-* file runs in CI from ONE list (bin/ci-test-files.txt); the files are derived from the directory (the tests of that change)."""
import ast
import re
import unittest

from support.paths import BIN, REPO

SWITCHBOARD = REPO / "bin" / "test-switchboard"


def _ci_list_problems(list_text, files, workflow_text):
    """The problems of the CI test list: a file in neither section, a `run` row with no workflow step, an `exempt` row without a reason of 20+ characters."""
    run, exempt, problems = set(), {}, []
    for line in list_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("run "):
            run.add(line[4:].strip())
        elif line.startswith("exempt "):
            path, _, reason = line[7:].partition("|")
            exempt[path.strip()] = reason.strip()
        else:
            problems.append("unreadable row: " + line)
    for path in sorted(files):
        if path not in run and path not in exempt:
            problems.append("not in the CI test list: " + path)
    for path in sorted(run):
        if not re.search(r"^\s+" + re.escape(path) + r"\s+2>&1", workflow_text, re.M):
            problems.append("no workflow step runs: " + path)
    for path, reason in sorted(exempt.items()):
        if len(reason) < 20:
            problems.append("exempt without a reason: " + path)
    return problems


class TestCiTestFileList(unittest.TestCase):
    LIST = (REPO / "bin" / "ci-test-files.txt")
    WORKFLOW = (REPO / ".github" / "workflows" / "test.yml")

    def real_files(self):
        return sorted("bin/" + p.name for p in BIN.glob("test-*"))

    def test_every_test_file_is_listed_and_run(self):
        self.assertEqual(_ci_list_problems(self.LIST.read_text(), self.real_files(), self.WORKFLOW.read_text()), [])

    def test_a_planted_unlisted_file_fails_naming_it(self):
        problems = _ci_list_problems(self.LIST.read_text(), self.real_files() + ["bin/test-newfile"], self.WORKFLOW.read_text())
        self.assertEqual(problems, ["not in the CI test list: bin/test-newfile"])

    def test_the_list_runs_path_suites_once_in_the_workflow(self):
        self.assertEqual(self.LIST.read_text().splitlines().count("run bin/test-path-suites"), 1)
        self.assertEqual(len(re.findall(r"^\s+bin/test-path-suites\s+2>&1", self.WORKFLOW.read_text(), re.M)), 1)

    def test_removing_a_known_file_from_the_list_fails_naming_it(self):
        for name in ("bin/test-path-suites", "bin/test-impact-brief"):
            text = "\n".join(l for l in self.LIST.read_text().splitlines() if name not in l)
            problems = _ci_list_problems(text, self.real_files(), self.WORKFLOW.read_text())
            self.assertEqual(problems, ["not in the CI test list: " + name])

    def test_a_run_row_without_a_step_and_a_bare_exempt_fail(self):
        text = self.LIST.read_text() + "run bin/test-ghost\nexempt bin/test-x | short\n"
        problems = _ci_list_problems(text, self.real_files(), self.WORKFLOW.read_text())
        self.assertEqual(problems, ["no workflow step runs: bin/test-ghost", "exempt without a reason: bin/test-x"])

    @staticmethod
    def _bound_names(source):
        """The names bound at the top level of bin/test-switchboard (the namespace its loader fills), read from its syntax tree."""
        names = set()
        for node in ast.parse(source).body:
            for t in (node.targets if isinstance(node, ast.Assign) else []):
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return names

    def _exempt_claim_problems(self, source, namespace):
        row = [l for l in self.LIST.read_text().splitlines() if l.startswith("exempt bin/test-impact-brief")]
        if not row:
            return []
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_brief_modules" for t in node.targets):
                names = {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        problems = []
        if "test-impact-brief" not in names:
            problems.append("exempt row claims bin/test-switchboard loads it, but _brief_modules does not: " + row[0])
        for cls in ("BriefTests", "MutantTests"):
            if "Brief_impact_brief_" + cls not in namespace:
                problems.append("exempt row claims " + cls + " is imported, but it is not: " + row[0])
        return problems

    def test_the_exempt_test_impact_brief_is_really_loaded_here(self):
        self.assertEqual(self._exempt_claim_problems(SWITCHBOARD.read_text(), self._bound_names(SWITCHBOARD.read_text())), [])

    def test_a_switchboard_that_stops_loading_test_impact_brief_fails_naming_the_row(self):
        original = SWITCHBOARD.read_text()
        source = original.replace('"test-impact-brief", ', '', 1)
        if source == original:
            source = original.replace(', "test-impact-brief")', ')', 1)
        self.assertNotEqual(source, original)
        problems = self._exempt_claim_problems(source, {k for k in self._bound_names(original) if not k.startswith("Brief_impact_brief_")})
        self.assertEqual(len(problems), 3)
        self.assertTrue(all("exempt bin/test-impact-brief" in p for p in problems))

    def test_the_documentation_names_the_list_file(self):
        self.assertEqual(sum(1 for l in (REPO / "docs" / "switchboard.md").read_text().splitlines() if "bin/ci-test-files.txt" in l), 1)

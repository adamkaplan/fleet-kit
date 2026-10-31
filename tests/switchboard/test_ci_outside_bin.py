"""#1502: every test file outside bin/ (cloud/, plugins/, tests/) is run by a CI step or chain, or is exempt with a reason, from ONE list (tests/ci-test-files.txt).

Closed rule: the files come from the directory tree (cloud/**/test_*.py, cloud/**/test-*, plugins/**/*.test.mjs, plugins/**/*.test.ts, tests/**/test_*.py), never from a list.
A file passes when a rule row covers it (a chain file lists it, or the runner names its module as a string) or a run/exempt row names it. Known limit: a runner that names a module but never runs it is not caught here.
"""
import ast
import re
import unittest

from support.paths import REPO

GLOBS = ("cloud/**/test_*.py", "cloud/**/test-*", "plugins/**/*.test.mjs", "plugins/**/*.test.ts", "tests/**/test_*.py")


def tree_files(repo, globs=GLOBS):
    """The test files of the trees, read from the directory (node_modules skipped)."""
    return sorted({p.relative_to(repo).as_posix() for g in globs for p in repo.glob(g) if p.is_file() and "node_modules" not in p.parts})


def problems(list_text, files, workflow_text, chain_texts, runner_sources):
    """One message per failure: a file covered by nothing, a row with no step, an exempt row with a reason under 20 characters."""
    chains, loaders, run, exempt, out = {}, [], {}, {}, []
    for line in list_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        path, _, detail = (s.strip() for s in rest.partition("|"))
        if kind == "chain":
            chains[path] = detail
        elif kind == "loader":
            loaders.append(path)
        elif kind == "run":
            run[path] = detail
        elif kind == "exempt":
            exempt[path] = detail
        else:
            out.append("unreadable row: " + line)
    chained = set()
    for chain, script in chains.items():
        if not re.search(r"^\s+" + re.escape(script) + r"\s+2>&1", workflow_text, re.M):
            out.append("no workflow step runs: " + script)
        chained |= {l.strip() for l in chain_texts.get(chain, "").splitlines() if l.strip()}
    named = set()
    for runner in loaders:
        named |= {n.value for n in ast.walk(ast.parse(runner_sources.get(runner, ""))) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for path, step in sorted(run.items()):
        if not re.search(r"^\s+(run: node --test .*" + re.escape(path) + r"(\s|$)|" + re.escape(path) + r"\s+2>&1)", workflow_text, re.M):
            out.append("no workflow step runs: " + path)
    for path, reason in sorted(exempt.items()):
        if len(reason) < 20:
            out.append("exempt without a reason: " + path)
    for f in files:
        stem = f.rsplit("/", 1)[-1]
        by_loader = f.endswith(".py") and (stem[:-3] in named or stem in named)
        if f not in chained and not by_loader and f not in run and f not in exempt:
            out.append("not in the CI outside-bin list: " + f)
    for path in list(run) + list(exempt):
        if path not in files:
            out.append("listed but not a test file of the trees: " + path)
    return out


class TestCiOutsideBin(unittest.TestCase):
    LIST = REPO / "tests" / "ci-test-files.txt"
    WORKFLOW = REPO / ".github" / "workflows" / "test.yml"

    def args(self, text=None, files=None):
        chains = {"bin/test-cloud-launcher.chain": (REPO / "bin" / "test-cloud-launcher.chain").read_text()}
        runners = {"bin/test-switchboard": (REPO / "bin" / "test-switchboard").read_text()}
        return (text if text is not None else self.LIST.read_text(), files if files is not None else tree_files(REPO), self.WORKFLOW.read_text(), chains, runners)

    def test_every_test_file_outside_bin_is_run_or_exempt(self):
        self.assertEqual(problems(*self.args()), [])

    def test_the_trees_are_really_read(self):
        files = tree_files(REPO)
        for prefix in ("cloud/launcher/tests/", "cloud/worker/tests/", "cloud/tests/", "plugins/fleet-hooks/", "plugins/opencode-todolist/", "tests/switchboard/", "tests/test_"):
            self.assertTrue(any(f.startswith(prefix) for f in files), prefix)

    def test_a_planted_unlisted_file_fails_naming_it(self):
        self.assertEqual(problems(*self.args(files=tree_files(REPO) + ["plugins/x/new.test.mjs"])), ["not in the CI outside-bin list: plugins/x/new.test.mjs"])

    def test_removing_a_listed_file_fails_naming_it(self):
        for name in ("plugins/fleet-hooks/index.test.mjs", "cloud/worker/tests/test_guard.py"):
            text = "\n".join(l for l in self.LIST.read_text().splitlines() if name not in l)
            self.assertEqual(problems(*self.args(text=text)), ["not in the CI outside-bin list: " + name])

    def test_a_mutant_that_reads_only_bin_fails(self):
        self.assertEqual(tree_files(REPO, ("bin/test-*",)) and [f for f in tree_files(REPO, ("bin/test-*",)) if not f.startswith("bin/")], [])
        self.assertNotEqual(tree_files(REPO, ("bin/test-*",)), tree_files(REPO))

    def test_a_chain_without_the_file_a_short_reason_and_a_ghost_row_fail(self):
        text = self.LIST.read_text() + "run plugins/x/ghost.test.mjs | node --test\nexempt tests/test_x.py | short\n"
        out = problems(*self.args(text=text, files=tree_files(REPO) + ["tests/test_x.py"]))
        self.assertIn("no workflow step runs: plugins/x/ghost.test.mjs", out)
        self.assertIn("exempt without a reason: tests/test_x.py", out)
        self.assertIn("listed but not a test file of the trees: plugins/x/ghost.test.mjs", out)
        chain = {"bin/test-cloud-launcher.chain": ""}
        out = problems(self.LIST.read_text(), tree_files(REPO), self.WORKFLOW.read_text(), chain, self.args()[4])
        self.assertIn("not in the CI outside-bin list: cloud/launcher/tests/test_cw_names.py", out)

    def test_a_workflow_without_the_node_step_fails(self):
        text = self.WORKFLOW.read_text().replace("plugins/fleet-hooks/index.test.mjs", "")
        out = problems(self.LIST.read_text(), tree_files(REPO), text, self.args()[3], self.args()[4])
        self.assertIn("no workflow step runs: plugins/fleet-hooks/index.test.mjs", out)

    def test_the_documentation_names_the_list_file(self):
        self.assertEqual(sum(1 for l in (REPO / "docs" / "switchboard.md").read_text().splitlines() if "tests/ci-test-files.txt" in l), 1)

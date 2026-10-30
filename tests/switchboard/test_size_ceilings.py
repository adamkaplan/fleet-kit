"""The size ceilings are checked on the merge result (#1605): bin/size_ceilings.py, the test workflow's step and the merge gate's call.

Every scenario runs on a REAL git repository built in a temporary directory (a file of 1,000 lines, a ceiling of 1,100). A mutant is the real source
with one line replaced; each of the three (the head alone, the stored merge ref, no added-lines condition) must fail the scenario written for it.
Run with `bin/test-switchboard` (the loader block of that file imports this module by name) or `python3 -m unittest tests/switchboard/test_size_ceilings.py`.
Standard library only.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
sys.path.insert(0, str(REPO / "tests" / "switchboard"))
import size_ceilings  # noqa: E402

SOURCE = (REPO / "bin" / "size_ceilings.py").read_text()
BIG = "bin/big"
ROW = "| `bin/big` | `-` | size | %s | a fixture file |\n"
DOC = "# Fixture\n\n## Units\n\n| Unit | Layer |\n|---|---|\n| `x.py` | 1 |\n\n## Known violations\n\n| From | To | Kind | Count | Why |\n|---|---|---|---|---|\n%s\n## Next\n\nText.\n"
_EMPTY_HOME = tempfile.mkdtemp(prefix="size-ceilings-home-")   # an empty home: the fixture's git reads no user configuration
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
           HOME=_EMPTY_HOME, XDG_CONFIG_HOME=_EMPTY_HOME, GIT_CONFIG_NOSYSTEM="1")


def git(root, *argv):
    done = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, env=ENV)
    if done.returncode:
        raise AssertionError("git %s: %s" % (" ".join(argv), done.stderr))
    return done.stdout.strip()


def git_show(root, ref, path):
    return subprocess.run(["git", "-C", str(root), "show", "%s:%s" % (ref, path)], capture_output=True, text=True, env=ENV, check=True).stdout


def mutant(old, new):
    """The module with the one line `old` replaced by `new` (the line must be there: a renamed line fails here, not silently)."""
    assert old in SOURCE, old
    module = types.ModuleType("size_ceilings_mutant")
    exec(compile(SOURCE.replace(old, new), "size_ceilings_mutant.py", "exec"), module.__dict__)
    return module


class Fixture:
    """`main` holds the doc and `bin/big` (1,000 lines); pr1 and pr2 each add 60 lines (pr1 at the top, pr2 at the bottom, so both merge cleanly)."""

    def __init__(self, ceiling="1,100", rows=None, lines=1000):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "docs").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "docs" / "architecture.md").write_text(DOC % (rows if rows is not None else ROW % ceiling))
        self.base_lines = ["line %d\n" % i for i in range(lines)]
        (self.root / BIG).write_text("".join(self.base_lines))
        self.commit("base")

    def commit(self, message):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def branch(self, name, top=0, bottom=0, from_ref="main"):
        git(self.root, "checkout", "-q", "-b", name, from_ref)
        text = "".join("%s top %d\n" % (name, i) for i in range(top)) + git_show(self.root, from_ref, BIG) + "".join("%s bottom %d\n" % (name, i) for i in range(bottom))
        (self.root / BIG).write_text(text)
        sha = self.commit(name)
        git(self.root, "checkout", "-q", "main")
        return sha

    def merge(self, name):
        git(self.root, "merge", "-q", "--no-ff", "-m", "merge " + name, name)
        return git(self.root, "rev-parse", "HEAD")

    def close(self):
        self.dir.cleanup()


class Cases(unittest.TestCase):
    def fixture(self, **more):
        fx = Fixture(**more)
        self.addCleanup(fx.close)
        return fx

    def refs(self, fx):
        return git(fx.root, "for-each-ref")

    def status(self, fx):
        return git(fx.root, "status", "--porcelain")


# ---- the scenarios, each a function of the module so that a mutant can be run through it

def two_pull_requests(module, fx):
    """S8: pr1 passes, then (merged) pr2 fails with 1120, 1100 and 60 (the head alone would be 1060)."""
    pr1 = fx.branch("pr1", top=60)
    pr2 = fx.branch("pr2", bottom=60)
    first = module.check_merge(fx.root, "main", pr1)
    fx.merge("pr1")
    second = module.check_merge(fx.root, "main", pr2)
    assert first == [], first
    assert len(second) == 1 and re.search(r"bin/big is 1,120 lines.*ceiling of 1,100.*adds 60", second[0]), second
    return first, second


def moved_tip(module, fx):
    """S2 and S6: the stored merge ref was built when the tip was old (it fits); the tip then moved; the same head now fails."""
    pr2 = fx.branch("pr2", bottom=60)
    old = module.check_merge(fx.root, "main", pr2)
    stored = git(fx.root, "merge-tree", "--write-tree", "main", pr2)
    git(fx.root, "update-ref", "refs/pull/2/merge", git(fx.root, "commit-tree", stored, "-p", "main", "-p", pr2, "-m", "stored merge ref"))
    fx.branch("pr1", top=60)
    fx.merge("pr1")
    new = module.check_merge(fx.root, "main", pr2)
    assert old == [], old
    assert len(new) == 1 and "bin/big is 1,120 lines" in new[0], new
    return old, new


def over_already(module, fx):
    """S9: a file already over its ceiling: a pull request that adds nothing passes, one that adds a line fails."""
    git(fx.root, "checkout", "-q", "-b", "nothing", "main")
    (fx.root / "other.txt").write_text("x\n")
    nothing = fx.commit("unrelated change")
    git(fx.root, "checkout", "-q", "main")
    adds = fx.branch("adds", bottom=1)
    assert module.check_merge(fx.root, "main", nothing) == []
    got = module.check_merge(fx.root, "main", adds)
    assert len(got) == 1 and "adds 1" in got[0], got


class TestTheCheck(Cases):
    def test_s8_two_pull_requests_that_each_add_60_lines_pass_then_fail_with_the_numbers(self):
        first, second = two_pull_requests(size_ceilings, self.fixture())
        self.assertEqual((first, len(second)), ([], 1))

    def test_s3_the_problem_line_holds_the_file_the_count_after_the_merge_the_ceiling_and_the_added_lines(self):
        fx = self.fixture()
        fx.branch("pr1", top=60)
        fx.merge("pr1")
        (line,) = size_ceilings.check_merge(fx.root, "main", fx.branch("pr2", bottom=60))
        numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", line)]
        self.assertEqual((line.split()[0], numbers), ("bin/big", [1120, 1100, 60]))

    def test_s2_and_s6_a_tip_that_moved_after_the_stored_merge_ref_was_built_fails_on_a_second_run_with_no_new_head(self):
        old, new = moved_tip(size_ceilings, self.fixture())
        self.assertEqual((old, len(new)), ([], 1))

    def test_s5_the_architecture_test_and_the_check_read_the_five_real_ceilings_from_the_one_function(self):
        import test_architecture
        text = (REPO / "docs" / "architecture.md").read_text()
        ceilings, problems = size_ceilings.size_rows(text)
        _units, violations = test_architecture.parse_document(text)
        by_arch = {("bin/" + k[0] if not k[0].startswith(("bin/", "tests/")) else k[0]): v[0] for k, v in violations.items() if k[2] == "size"}
        self.assertEqual((problems, len(ceilings), ceilings), ([], 5, by_arch))
        self.assertIs(test_architecture.size_ceilings, size_ceilings)
        self.assertEqual(test_architecture.line_count(REPO / "bin" / "size_ceilings.py"), size_ceilings.line_count(REPO / "bin" / "size_ceilings.py"))

    def test_s5_a_planted_unreadable_row_fails_the_check_and_the_architecture_test_the_same_way(self):
        import test_architecture
        row = "| `bin/big` | `-` | size | lots | planted |\n"
        _ceilings, problems = size_ceilings.size_rows(DOC % row)
        self.assertEqual(len(problems), 1)
        with self.assertRaises(ValueError):
            test_architecture.parse_document(DOC % row)

    def test_s9_a_file_over_its_ceiling_passes_when_the_pull_request_adds_nothing_and_fails_when_it_adds_a_line(self):
        over_already(size_ceilings, self.fixture(ceiling="900"))

    def test_s10_an_unreadable_size_row_fails_closed_and_names_the_row(self):
        for text, name in (("| `bin/big` | `-` | size | lots | planted |\n", "bin/big"), ("| `bin/big` | `-` |\n", "bin/big")):
            with self.subTest(row=text):
                fx = self.fixture(rows=text)
                got = size_ceilings.check_merge(fx.root, "main", fx.branch("pr", top=1))
                self.assertEqual(len(got), 1)
                self.assertIn("`%s`" % name, got[0])

    def test_a_file_that_is_not_a_size_row_and_a_file_within_its_ceiling_pass(self):
        fx = self.fixture(rows="| `x.py` | `y.py` | import | 3 | known |\n" + ROW % "1,100")
        self.assertEqual(size_ceilings.check_merge(fx.root, "main", fx.branch("pr", top=100)), [])

    def test_u4_a_conflicting_head_fails_closed_with_one_line_naming_the_files_and_writes_nothing(self):
        fx = self.fixture()
        fx.branch("a", top=3)
        fx.branch("b", top=3)
        fx.merge("a")
        before = (self.refs(fx), self.status(fx))
        got = size_ceilings.check_merge(fx.root, "main", "b")
        self.assertEqual(len(got), 1)
        self.assertIn("conflicts in bin/big", got[0])
        self.assertEqual((self.refs(fx), self.status(fx)), before, "no branch, tag or file is written")

    def test_a_check_writes_no_branch_tag_or_file_and_an_unknown_commit_fails_closed(self):
        fx = self.fixture()
        head = fx.branch("pr", top=5)
        before = (self.refs(fx), self.status(fx))
        self.assertEqual(size_ceilings.check_merge(fx.root, "main", head), [])
        self.assertEqual((self.refs(fx), self.status(fx)), before)
        self.assertEqual(len(size_ceilings.check_merge(fx.root, "main", "f" * 40)), 1)

    def test_the_command_exits_0_for_a_pass_and_1_with_the_lines_on_stdout(self):
        fx = self.fixture()
        fx.branch("pr1", top=60)
        bad_head = fx.branch("pr2", bottom=60)
        fx.merge("pr1")
        good_head = fx.branch("ok", bottom=1)
        run = lambda head: subprocess.run([sys.executable, str(REPO / "bin" / "size_ceilings.py"), "check", "--tip", "main", "--head", head, "--root", str(fx.root)],
                                          capture_output=True, text=True)
        good = run(good_head)
        bad = run(bad_head)
        self.assertEqual((good.returncode, good.stdout), (0, ""))
        self.assertEqual((bad.returncode, bad.stdout.count("\n")), (1, 1))
        self.assertEqual(bad.stdout.splitlines(), size_ceilings.check_merge(fx.root, "main", git(fx.root, "rev-parse", "pr2")))


class TestMutants(Cases):
    """S11: each mutant fails the scenario written for it, and the real module passes all of them."""

    MUTANTS = {
        "head alone": ('"merge-tree", "--write-tree", "--name-only", tip, head)', '"merge-tree", "--write-tree", "--name-only", head, head)'),
        "stored merge ref": ("tree = out[0].strip()", 'tree = _git(root, "rev-parse", "refs/pull/2/merge^{tree}").stdout.decode().strip()'),
        "ignores the added lines": ("after > ceiling and added > 0", "after > ceiling"),
    }
    SCENARIOS = {"head alone": (two_pull_requests, {}), "stored merge ref": (moved_tip, {}), "ignores the added lines": (over_already, {"ceiling": "900"})}

    def test_the_real_module_passes_every_scenario(self):
        for name, (scenario, more) in self.SCENARIOS.items():
            with self.subTest(name):
                scenario(size_ceilings, self.fixture(**more))

    def test_each_mutant_fails_its_scenario(self):
        self.assertEqual(set(self.MUTANTS), set(self.SCENARIOS))
        for name, (old, new) in self.MUTANTS.items():
            with self.subTest(name):
                module = mutant(old, new)
                scenario, more = self.SCENARIOS[name]
                with self.assertRaises(AssertionError):
                    scenario(module, self.fixture(**more))


class TestTheMergeGate(Cases):
    """S7: `merge_gate.check_merge` (what the shim asks before `gh pr merge`) refuses a merge that would put a file over its ceiling, with the lines of the step."""

    def setUp(self):
        from support.service_and_merge import FakeMergeGitHub
        spec = importlib.util.spec_from_file_location("merge_gate_t", str(REPO / "bin" / "merge_gate.py"))
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        spec = importlib.util.spec_from_file_location("pr_reading_t", str(REPO / "bin" / "pr_reading.py"))
        self.reading = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.reading)
        self.fake_class = FakeMergeGitHub
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def merge_attempt(self, fx, head, tip):
        fake = self.fake_class(repo="acme-org/widget-service")
        fake.head, fake.tip = head, tip
        fake.runs = [fake.make_run(1, "ci", 10, "completed", "success", tip, fake.now - 600)]
        fake.branch_runs[0]["head_sha"] = tip
        conf = self.gate.settings({"merge_gate": {"repos": {fake.repo: {}}}})
        ok, said = self.gate.gate_pass(self.reading, fake, conf, self.state, fake.repo, fake.number, "coder-a", os.getpid(), fake.clock, fake.sleep, alive=lambda pid: True)
        self.assertTrue(ok, said)
        os.chdir(fx.root)
        got = self.gate.check_merge(self.reading, fake, conf, self.state, fake.repo, fake.number, "coder-a", False, lambda h: False, fake.clock, alive=lambda pid: True)
        return got, fake

    def test_a_merge_over_the_ceiling_is_refused_with_the_problem_lines_of_the_step_and_one_that_fits_is_allowed(self):
        fx = self.fixture()
        pr1, pr2 = fx.branch("pr1", top=60), fx.branch("pr2", bottom=60)
        fx.merge("pr1")
        tip = git(fx.root, "rev-parse", "main")
        step = size_ceilings.check_merge(fx.root, tip, pr2)
        (allowed, why, head), fake = self.merge_attempt(fx, pr2, tip)
        self.assertEqual((allowed, head, len(step)), (False, None, 1))
        self.assertEqual(why.splitlines()[1:], step)
        self.assertEqual(fake.merges, [], "no `gh pr merge` call for the refused one")
        fx2 = self.fixture()
        ok_head = fx2.branch("pr", bottom=60)
        (allowed, why, head), _ = self.merge_attempt(fx2, ok_head, git(fx2.root, "rev-parse", "main"))
        self.assertEqual((allowed, head), (True, ok_head))
        self.assertTrue(why.startswith(self.gate.GATE_PASSED))

    def test_a_pull_request_whose_commits_are_not_in_this_repository_is_decided_as_before(self):
        fx = self.fixture()
        (allowed, _why, _head), _fake = self.merge_attempt(fx, "a" * 40, "1" * 40)
        self.assertTrue(allowed)


class TestDocumentation(unittest.TestCase):
    """S12: docs/switchboard.md says which files are checked, that the head is merged with the current tip, how to re-run, the limit and what the lines are."""

    def test_the_four_facts_and_the_message_are_documented(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        start = text.index("## The size ceilings on the merge result")
        section = text[start:text.index("\n## ", start + 5)] if "\n## " in text[start + 5:] else text[start:]
        for fact in ("`size` rows of the Known violations table", "merges the pull request head with the current tip", "plain re-run of the job",
                     "file with no `size` row", "stdout lines of `python3 bin/size_ceilings.py check`"):
            with self.subTest(fact=fact):
                self.assertIn(fact, section)


if __name__ == "__main__":
    unittest.main()

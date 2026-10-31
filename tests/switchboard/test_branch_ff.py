"""The `clear-head` and `branch-ff` tests of #1712: a pull request's branch moves to a head that was already read and cleared. Everything is faked: no real GitHub call."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock
from support.core import Base
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE
from support.herdr_world import World

REPO = "acme/widgets"
OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40
PR = "%s#7" % REPO


class FakeGitHub:
    """The fake `gh`: reads answer from tables, every call is counted, a write is any call with a --method."""

    def __init__(self):
        self.pull = {"state": "open", "merged": False, "head": {"ref": "feature", "sha": OLD, "repo": {"full_name": REPO}}, "base": {"ref": "integration"}}
        self.scratch, self.compare, self.default = NEW, "ahead", "main"
        self.calls, self.git_calls = [], []

    def __call__(self, argv, **kwargs):
        if os.path.basename(argv[0]) == "git":
            self.git_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "no git here")
        path = next(a for a in argv[2:] if a.startswith("repos/"))
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        body = json.loads(kwargs["input"]) if kwargs.get("input") else None
        self.calls.append((method, path, body))
        if method != "GET":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if path == "repos/%s/pulls/7" % REPO:
            answer = self.pull
        elif path == "repos/%s/git/ref/heads/scratch" % REPO:
            answer = {"object": {"sha": self.scratch}}
        elif path.startswith("repos/%s/compare/" % REPO):
            answer = {"status": self.compare}
        elif path == "repos/%s" % REPO:
            answer = {"default_branch": self.default}
        else:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class TestBranchFf(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.world.add_agent("lane", fleet={"role": "reviewer"})
        self.world.add_agent("orch", fleet={"role": "orchestrator"})
        self.gh = FakeGitHub()

    def runner(self, argv, **kwargs):
        return self.gh(argv, **kwargs) if os.path.basename(argv[0]) in ("gh", "git") else self.world.route(argv, **kwargs)

    def main(self, who, *argv):
        with mock.patch.dict(os.environ, {sb.SESSION_ENV: self.world.sessions[who]}):
            with captured() as (out, err):
                try:
                    code = sb.main(list(argv), runner=self.runner, now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
                except SystemExit as stop:
                    code = stop.code
        return code, out.getvalue(), err.getvalue()

    def ledger(self):
        path = self.world.state / sb.CLEAR_HEADS_FILE
        return json.loads(path.read_text())["lines"] if path.exists() else []

    def audited(self, event):
        return [x for x in self.world.audit() if x["event"] == event]

    def clear(self, pr=PR, sha=NEW, who="cos"):
        return self.main(who, "clear-head", pr, "--sha", sha)

    def one_refusal(self, result, text="refused"):
        code, out, err = result
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn(text, err)

    def test_s2_only_the_chief_of_staff_records_a_cleared_head(self):
        results = [self.clear(who=who) for who in ("cos", "lane", "orch")]
        self.assertEqual(results[0][0], 0, results[0])
        self.assertEqual(len(self.ledger()), 1, "ledger lines after three callers")
        refusals = [r for r in results if r[0] != 0]
        self.assertEqual(len(refusals), 2, "refusals of three callers")
        for refusal in refusals:
            self.assertEqual(len(refusal[2].strip().splitlines()), 1, refusal)
        self.assertEqual(self.ledger()[0]["sha"], NEW)
        self.assertEqual(self.ledger()[0]["pr"], PR)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.clear(sha=NEW[:8])[0], sb.EXIT_USAGE, "a short sha is refused")
        self.assertEqual(len(self.ledger()), 1)

    def test_s3_the_allowed_case_moves_the_branch_once_and_audits_once(self):
        self.assertEqual(self.clear()[0], 0)
        code, out, err = self.main("orch", "branch-ff", PR, "scratch")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gh.writes(), [("PATCH", "repos/%s/git/refs/heads/feature" % REPO, {"sha": NEW, "force": False})])   # U3: one write
        self.assertEqual(self.gh.git_calls, [], "no git call")
        lines = self.audited("branch.ff")
        self.assertEqual(len(lines), 1)
        detail = lines[0]["detail"]
        self.assertEqual((detail["by"], detail["from"], detail["to"], detail["clear_head"], lines[0]["subject"]), ("orch", OLD, NEW, 1, PR))
        self.assertIn("branch.ff", out)
        self.assertIn(NEW, out)

    def installed_tree(self):
        """A real git clone as the installed tree, recorded in this world's state dir; KIT_ROOT points at it."""
        import tempfile
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}
        patched = mock.patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        origin, tree = home / "origin", home / "installed"
        for argv in (["init", "-q", "-b", "main", str(origin)],):
            subprocess.run(["git"] + argv, check=True, capture_output=True)
        (origin / "one.txt").write_text("one\n")
        for argv in (["add", "one.txt"], ["commit", "-q", "-m", "one"]):
            subprocess.run(["git", "-C", str(origin)] + argv, check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(tree)], check=True, capture_output=True)
        (self.world.state / "installed-tree").write_text("%s\nmain\n" % tree)
        patch = mock.patch.object(sb, "KIT_ROOT", tree)
        patch.start()
        self.addCleanup(patch.stop)
        return tree

    def refused_when_unreadable(self):
        """On a trusted tree both commands run; on an unreadable one the count of those refused with nothing written (2 when both are marked)."""
        tree = self.installed_tree()
        self.assertEqual(self.clear()[0], 0, "control: clear-head on a trusted tree")
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.main("orch", "branch-ff", PR, "scratch")[0], 0, "control: branch-ff on a trusted tree")
        self.assertEqual(len(self.gh.writes()), 1)
        (tree / ".git" / "HEAD").write_text("garbage\n")
        ledger, writes, audit = len(self.ledger()), len(self.gh.writes()), len(self.world.audit())
        refused = 0
        for who, argv in (("cos", ("clear-head", "acme/widgets#8", "--sha", NEW)), ("orch", ("branch-ff", PR, "scratch"))):
            code, out, err = self.main(who, *argv)
            if code != 0 and "could not be read" in err and len(err.strip().splitlines()) == 1:
                refused += 1
        self.assertEqual(refused, 2, "commands refused")
        self.assertEqual((len(self.ledger()), len(self.gh.writes()), len(self.world.audit())), (ledger, writes, audit), "writes of a refused command")
        return refused

    def test_s9_both_commands_are_refused_on_an_unreadable_installed_tree(self):
        self.assertEqual(self.refused_when_unreadable(), 2)
        self.assertTrue(sb.command_records("clear-head", sb.build_parser().commands))
        self.assertTrue(sb.command_records("branch-ff", sb.build_parser().commands))

    def test_s9_a_command_without_its_mark_is_not_refused(self):
        for name in ("cmd_clear_head", "cmd_branch_ff"):
            with self.subTest(name):
                self.setUp()
                with mock.patch.object(getattr(sb, name), "guard_records", False):
                    with self.assertRaisesRegex(AssertionError, r"^1 != 2"):
                        self.refused_when_unreadable()

    def refusal_cases(self):
        """Each: (name, a function that arranges the world, the argv after `branch-ff`)."""
        def line(pr=PR, sha=NEW):
            self.clear(pr=pr, sha=sha)

        def diverged():
            line()
            self.gh.compare = "diverged"

        def merged():
            line()
            self.gh.pull.update(state="closed", merged=True)

        def closed():
            line()
            self.gh.pull.update(state="closed")

        def on(branch):
            def arrange():
                line()
                self.gh.pull["head"]["ref"] = branch
            return arrange
        def fork():
            line()
            self.gh.pull["head"]["repo"] = {"full_name": "forker/widgets"}
        return [("not a fast-forward", diverged, [PR, "scratch"]),
                ("no clear-head line", lambda: None, [PR, "scratch"]),
                ("a line for another pull request", lambda: line(pr="%s#8" % REPO), [PR, "scratch"]),
                ("a line for another sha", lambda: line(sha=OTHER), [PR, "scratch"]),
                ("a merged pull request", merged, [PR, "scratch"]),
                ("a closed pull request", closed, [PR, "scratch"]),
                ("the default branch", on("main"), [PR, "scratch"]),
                ("the integration branch", on("integration"), [PR, "scratch"]),
                ("a --force flag", lambda: line(), [PR, "scratch", "--force"]),
                ("a head from another repository", fork, [PR, "scratch"])]

    def run_case(self, index):
        name, arrange, argv = self.refusal_cases()[index]
        self.setUp()   # a fresh world for each case
        arrange()
        before = len(self.world.audit())
        code, out, err = self.main("orch", "branch-ff", *argv)
        self.assertNotEqual(code, 0, name)
        if name == "a head from another repository":
            self.assertIn("another repository", err)
        if name != "a --force flag":   # the parser's own refusal is usage text over several lines
            self.assertEqual(len(err.strip().splitlines()), 1, "%s: %r" % (name, err))
        self.assertEqual(len(self.gh.writes()), 0, "ref updates after a refused request")
        self.assertEqual(len(self.audited("branch.ff")), 0, "branch.ff lines after a refused request")
        self.assertEqual(len(self.world.audit()), before, "audit lines written by a refusal")
        return name

    def test_s4_ten_requests_are_each_refused_writing_nothing(self):
        names = [self.run_case(i) for i in range(10)]
        self.assertEqual(len(set(names)), 10)

    def test_s4_a_failed_read_is_a_refusal(self):
        self.assertEqual(self.clear()[0], 0)
        self.gh.pull = None
        self.gh.scratch = "not a sha"
        code, out, err = self.main("orch", "branch-ff", PR, "scratch")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.gh.writes()), 0)

    def test_s5_mutant_without_a_clear_head_line_is_caught_after_its_control(self):
        self.run_case(1)   # the control: the unmutated code refuses
        class Anything(str):
            def __eq__(self, other):
                return True
            __hash__ = str.__hash__
        mutant = mock.patch.object(sb, "clear_head_lines", lambda state: [{"id": 1, "pr": Anything("x"), "sha": Anything("y")}])
        with mutant:
            with self.assertRaisesRegex(AssertionError, r"^0 == 0 : no clear-head line$"):
                self.run_case(1)

    def test_s5_mutant_that_allows_a_non_fast_forward_is_caught_after_its_control(self):
        self.run_case(0)   # the control
        real = sb.branch_ff_read

        def lying(engine, runner, repo, path):
            got = real(engine, runner, repo, path)
            return dict(got, status="ahead") if "/compare/" in path else got
        with mock.patch.object(sb, "branch_ff_read", lying):
            with self.assertRaisesRegex(AssertionError, r"^0 == 0 : not a fast-forward$"):
                self.run_case(0)

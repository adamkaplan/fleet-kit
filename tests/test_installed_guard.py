"""Ask #1380: the installed tree is always the default branch, clean. Plan scenarios S2 to S11, U1, U2.

Real git repositories in a temporary directory (an origin and a clone that plays the installed tree), the real `main()` of the entry for the
commands, the real `run_daemon` loop for the daemon, and the real standalone scripts for the alert and the hook. Fakes: herdr and the desktop notifier
(two small scripts that log their arguments), the engine of the daemon (a counter of its passes), and the clock.
"""
import ast
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.loader import sb
from support.paths import REPO

BIN = REPO / "bin"
guard = sb.load_module("installed_guard")
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *argv):
    done = subprocess.run(["git", "-C", str(cwd)] + list(argv), capture_output=True, text=True, env=dict(os.environ, **GIT_ENV))
    assert done.returncode == 0, (argv, done.stderr)
    return done.stdout.strip()


def commit(tree, name):
    (Path(tree) / name).write_text(name + "\n")
    git(tree, "add", name)
    git(tree, "commit", "-q", "-m", name)
    return git(tree, "rev-parse", "HEAD")


class TreeCase(Base):
    """An origin with two commits on main and one more on a branch `integration`; `self.tree` is a clone of it on main, recorded by the install."""

    def setUp(self):
        super().setUp()
        os.environ.update(GIT_ENV)
        os.environ["PATH"] = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
        self.origin = self.home / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        self.first = commit(self.origin, "one.txt")
        self.second = commit(self.origin, "two.txt")
        git(self.origin, "switch", "-q", "-c", "integration")
        self.integration = commit(self.origin, "three.txt")
        git(self.origin, "switch", "-q", "main")
        self.tree = self.home / "installed"
        git(self.home, "clone", "-q", str(self.origin), str(self.tree))
        git(self.tree, "fetch", "-q", "origin")
        self.state = sb.ensure_state_dir()
        (self.state / "installed-tree.json").write_text(json.dumps({"root": str(self.tree), "default_branch": "main"}))
        self.patch = mock.patch.object(sb, "KIT_ROOT", self.tree)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def command(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = sb.main(list(argv))
        return code, err.getvalue()


class TestTheEntryGuard(TreeCase):
    def outcomes(self):
        """S2, S3, S4: the exit status of a command for each state of the tree."""
        found = {}
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        found["other branch"] = self.command("version")
        git(self.tree, "switch", "-q", "--detach", "origin/integration")
        found["detached integration"] = self.command("version")
        git(self.tree, "switch", "-q", "--detach", self.first)
        found["detached ancestor"] = self.command("version")
        git(self.tree, "switch", "-q", "main")
        (self.tree / "one.txt").write_text("changed\n")
        found["dirty"] = self.command("version")
        git(self.tree, "checkout", "-q", "--", "one.txt")
        found["clean"] = self.command("version")
        return found

    def test_s2_s3_s4_each_state_gives_the_stated_result(self):
        found = self.outcomes()
        code, err = found["other branch"]
        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("branch scratch", err)
        self.assertIn(self.integration[:12], err)
        code, err = found["detached integration"]
        self.assertEqual(code, 1)
        self.assertIn(self.integration[:12], err)
        self.assertIn("not an ancestor", err)
        self.assertEqual(found["detached ancestor"][0], 0)
        code, err = found["dirty"]
        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("dirty paths: one.txt", err)
        self.assertEqual(found["clean"], (0, ""))

    def test_s9_a_mutant_that_removes_the_check_fails_each_of_them(self):
        always_ok = lambda root, branch, run=subprocess.run: {"ok": True, "note": None, "branch": "main", "commit": None, "dirty": [], "remote": None, "why": None}
        with mock.patch.object(guard, "check", always_ok):
            found = self.outcomes()
        self.assertEqual({name: code for name, (code, _) in found.items()}, {"other branch": 0, "detached integration": 0, "detached ancestor": 0, "dirty": 0, "clean": 0})
        for name in ("other branch", "detached integration", "dirty"):
            self.assertEqual(found[name][0], 0, name)   # the real code exits 1 for each (test_s2_s3_s4...): the mutant is caught

    def test_s5_the_commands_that_move_the_tree_are_not_refused(self):
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 1)
        for name in guard.EXEMPT_COMMANDS:
            with self.subTest(name=name):
                self.assertIsNone(sb.installed_tree_refusal(name))
        for name in ("status", "send", "run", "daemon", "key"):
            with self.subTest(name=name):
                self.assertIsNotNone(sb.installed_tree_refusal(name))
        self.assertEqual(set(guard.EXEMPT_COMMANDS), {"install", "upgrade", "doctor"})

    def test_s5_install_itself_runs_on_a_bad_tree(self):
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        with mock.patch.object(sb, "dispatch", lambda *a, **k: 0):
            self.assertEqual(self.command("install", "--dry-run")[0], 0)
            self.assertEqual(self.command("status")[0], 1)

    def test_a_tree_the_install_never_recorded_is_not_judged(self):
        (self.state / "installed-tree.json").unlink()
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 0)

    def test_a_recorded_tree_that_is_not_this_one_is_not_judged(self):
        (self.state / "installed-tree.json").write_text(json.dumps({"root": str(self.home / "elsewhere"), "default_branch": "main"}))
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 0)

    def test_a_fresh_clone_without_the_remote_ref_is_allowed_with_a_note_and_a_detached_one_is_not(self):
        git(self.tree, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self.command("version"), (0, ""))
        self.assertIn("ancestry unverified", guard.note(self.state, self.tree))
        git(self.tree, "switch", "-q", "--detach", "HEAD")
        self.assertEqual(self.command("version")[0], 1)

    def test_the_guard_that_cannot_run_refuses_all_but_the_exempt(self):
        with mock.patch.object(sb, "load_module", side_effect=ImportError("stub")):
            self.assertIn("could not run", sb.installed_tree_refusal("status"))
            self.assertIsNone(sb.installed_tree_refusal("install"))

    def test_u1_a_clean_tree_costs_at_most_three_git_calls_and_no_network(self):
        calls = []
        real = subprocess.run

        def counting(argv, **kw):
            calls.append(list(argv))
            return real(argv, **kw)
        self.assertIsNone(guard.problem(self.state, self.tree, counting))
        self.assertLessEqual(len(calls), guard.GIT_CALLS)
        self.assertFalse([c for c in calls if {"fetch", "pull", "clone", "ls-remote", "push"} & set(c)])

    def snapshot(self):
        files = {str(p.relative_to(self.tree)): p.read_bytes() for p in sorted(self.tree.rglob("*")) if p.is_file() and ".git/objects" not in str(p)}
        return files

    def test_u2_the_guard_changes_nothing_in_any_state(self):
        calls = []
        real = subprocess.run
        for prepare in (lambda: None, lambda: git(self.tree, "switch", "-q", "-c", "x", "origin/integration"),
                        lambda: (self.tree / "one.txt").write_text("dirty\n"), lambda: git(self.tree, "switch", "-q", "--detach", self.first)):
            prepare()
            before = self.snapshot()
            head = git(self.tree, "rev-parse", "HEAD")
            guard.problem(self.state, self.tree, lambda argv, **kw: (calls.append(list(argv)), real(argv, **kw))[1])
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(git(self.tree, "rev-parse", "HEAD"), head)
        verbs = {c[c.index("-C") + 2] for c in calls}
        self.assertLessEqual(verbs, {"status", "rev-parse", "merge-base"})


class FakePrograms:
    """A fake herdr and a fake desktop notifier: scripts that log their arguments, one line per call."""

    def __init__(self, directory, herdr_ok=True):
        self.herdr, self.notifier = directory / "herdr", directory / "notify"
        self.herdr_log, self.notifier_log = directory / "herdr.log", directory / "notify.log"
        self.herdr.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit %d\n' % (self.herdr_log, 0 if herdr_ok else 1))
        self.notifier.write_text('#!/bin/sh\necho "$@" >> "%s"\n' % self.notifier_log)
        for path in (self.herdr, self.notifier):
            path.chmod(0o755)

    @staticmethod
    def count(path):
        return len(path.read_text().splitlines()) if path.exists() else 0


class FakeEngine:
    def __init__(self, clock, herdr):
        self.clock, self.passes, self.notes, self.config = clock, 0, [], {}
        self.herdr = mock.Mock(binary=str(herdr))

    def now(self):
        return self.clock[0]

    def run_pass(self):
        self.passes += 1

    def _note(self, event, subject, detail):
        self.notes.append((event, detail))

    def _failed(self, *args):
        raise AssertionError("a pass failed: %r" % (args,))


class TestTheDaemon(TreeCase):
    def setUp(self):
        super().setUp()
        self.fakes = FakePrograms(self.home)
        self.clock = [1_800_000_000.0]
        self.engine = FakeEngine(self.clock, self.fakes.herdr)
        os.environ[sb.INSTALLED_TREE_ALERT_ENV] = str(self.fakes.notifier)
        patch = mock.patch.object(sb, "load_module", wraps=sb.load_module)
        self.load = patch.start()
        self.addCleanup(patch.stop)
        modules = mock.patch.dict(sys.modules, {sb.__name__: sb})   # run_daemon hands its own module to the relay pass
        modules.start()
        self.addCleanup(modules.stop)
        relay = mock.patch.object(sb.load_module("cloud_relay_daemon"), "pass_if_configured", lambda *a, **k: None)
        relay.start()
        self.addCleanup(relay.stop)

    def run_passes(self, count, step=60.0, between=None):
        def wait(sock, timeout):
            self.clock[0] += step
            if between:
                between()
        with mock.patch.object(sb, "next_poll", lambda engine, poll: poll):
            return sb.run_daemon(self.engine, self.state, 60, wait=wait, passes=count)

    def audit_lines(self, event):
        path = self.state / "audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines() if json.loads(l)["event"] == event] if path.exists() else []

    def test_s6_the_first_alert_is_at_once_repeats_wait_a_day_and_nothing_runs(self):
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.run_passes(1)
        self.assertEqual(self.fakes.count(self.fakes.notifier_log), 1)
        self.run_passes(2)
        self.assertEqual(self.fakes.count(self.fakes.notifier_log), 1)
        self.assertEqual(self.engine.passes, 0)   # no verdict, merge or message was decided on the bad tree
        self.clock[0] += 24 * 3600
        self.run_passes(1)
        self.assertEqual(self.fakes.count(self.fakes.notifier_log), 2)
        self.assertEqual(self.fakes.count(self.fakes.herdr_log), 2)
        self.assertEqual(len(self.audit_lines("installed_tree.alert")), 2)

    def test_s6b_the_alert_needs_no_guarded_code_and_the_person_is_told_when_herdr_is_down(self):
        self.fakes = FakePrograms(self.home, herdr_ok=False)
        self.engine.herdr = mock.Mock(binary=str(self.fakes.herdr))
        os.environ[sb.INSTALLED_TREE_ALERT_ENV] = str(self.fakes.notifier)
        real = self.load.side_effect

        def stub(name):
            if name == "installed_guard":
                raise ImportError("a stub that raises on import")
            return sb.load_module.__wrapped__(name)
        self.load.side_effect = stub
        self.run_passes(1)
        self.assertEqual(self.fakes.count(self.fakes.herdr_log), 1)       # tried, and failed: it is down
        self.assertEqual(self.fakes.count(self.fakes.notifier_log), 1)    # the desktop notification fires on every alert
        self.assertEqual(len(self.audit_lines("installed_tree.alert")), 1)
        self.assertEqual(self.engine.passes, 0)
        self.assertIn("(for cos)", self.fakes.herdr_log.read_text())
        tree = ast.parse((BIN / "guard_alert.py").read_text())
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(imported & {"installed_guard", "fleet_switchboard", "importlib"}, imported)

    def test_s6b_the_alert_script_runs_with_no_other_file_of_the_kit_beside_it(self):
        lone = self.home / "lone"
        lone.mkdir()
        (lone / "guard_alert.py").write_text((BIN / "guard_alert.py").read_text())
        state = self.home / "alone-state"
        state.mkdir()
        done = subprocess.run([sys.executable, str(lone / "guard_alert.py"), "--state", str(state), "--text", "bad", "--herdr", str(self.fakes.herdr),
                               "--notifier", str(self.fakes.notifier)], capture_output=True, text=True, cwd=str(lone))
        self.assertEqual(json.loads(done.stdout)["sent"], True)
        self.assertTrue((state / "audit.jsonl").is_file())

    def test_s6c_the_daemon_resumes_by_itself_when_the_tree_is_good(self):
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.run_passes(1)
        self.assertEqual(self.engine.passes, 0)
        git(self.tree, "switch", "-q", "main")
        self.run_passes(2)
        self.assertEqual(self.engine.passes, 2)
        git(self.tree, "switch", "-q", "scratch")
        self.run_passes(1)   # a new bad time after a good one alerts at once, whatever the day
        self.assertEqual(self.fakes.count(self.fakes.notifier_log), 2)

    def test_a_good_tree_alerts_nobody(self):
        self.run_passes(3)
        self.assertEqual((self.engine.passes, self.fakes.count(self.fakes.notifier_log), self.fakes.count(self.fakes.herdr_log)), (3, 0, 0))


class TestTheHooks(TreeCase):
    def install(self):
        return guard.install(self.tree, self.state, BIN, sys.executable)

    def test_s7_every_move_of_head_adds_one_audit_line_with_the_ancestors(self):
        self.install()
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        git(self.tree, "switch", "-q", "main")
        git(self.tree, "merge", "-q", "--no-edit", "origin/integration")
        lines = [json.loads(l) for l in (self.state / "audit.jsonl").read_text().splitlines()]
        moves = [l for l in lines if l["event"] == "installed_tree.head_moved"]
        self.assertEqual([m["detail"]["hook"] for m in moves], ["post-checkout", "post-checkout", "post-merge"])
        for move in moves:
            self.assertGreaterEqual(len(move["detail"]["ancestors"]), 2)
            for ancestor in move["detail"]["ancestors"]:
                self.assertTrue(ancestor["pid"] and ancestor["command"] and "ppid" in ancestor)
        self.assertEqual(moves[0]["detail"]["old"], self.second)
        self.assertEqual(moves[0]["detail"]["new"], self.integration)
        self.assertEqual(moves[2]["detail"]["new"], self.integration)

    def test_s8_the_install_writes_both_hooks_executable_and_is_idempotent(self):
        first = self.install()
        hooks = Path(git(self.tree, "rev-parse", "--git-common-dir"))
        hooks = (hooks if hooks.is_absolute() else self.tree / hooks) / "hooks"
        for name in guard.HOOKS:
            self.assertTrue(os.access(str(hooks / name), os.X_OK), name)
            self.assertIn("guard_record_head_move.py", (hooks / name).read_text())
        self.assertEqual(git(self.tree, "status", "--porcelain"), "")   # the tree stays clean
        self.assertTrue(all(status == "done" for _, status, _ in first))
        self.assertTrue(all(status == "already done" for _, status, _ in self.install()))
        self.assertEqual(len({step for step, _, _ in first}), len(first))   # one step name each: the install test groups by it

    def test_a_hook_that_is_not_ours_is_left_alone(self):
        hooks = self.tree / ".git" / "hooks"
        (hooks / "post-merge").write_text("#!/bin/sh\necho mine\n")
        steps = {step: (status, detail) for step, status, detail in self.install()}
        self.assertEqual(steps["tree hook merge"][0], "skipped")
        self.assertEqual((hooks / "post-merge").read_text(), "#!/bin/sh\necho mine\n")

    def test_the_install_records_the_tree_and_the_default_branch(self):
        (self.state / "installed-tree.json").unlink()
        self.install()
        self.assertEqual(guard.read_marker(self.state), (os.path.realpath(str(self.tree)), "main"))

    def test_the_scripts_are_executable_with_the_portable_shebang_and_standard_library_only(self):
        roots = sb.sysconfig.get_paths()["stdlib"] if hasattr(sb, "sysconfig") else None
        for name in guard.SCRIPTS:
            path = BIN / name
            self.assertTrue(os.access(str(path), os.X_OK), name)
            self.assertEqual(path.read_text().splitlines()[0], "#!/usr/bin/env python3")
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for module in ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]):
                        self.assertIn(module.split(".")[0], sys.stdlib_module_names, "%s imports %s" % (name, module))


class TestTheDocument(unittest.TestCase):
    TEXT = (REPO / "docs" / "switchboard.md").read_text()

    def section(self):
        return self.TEXT.split("## The installed tree is the default branch, clean (#1380)", 1)[1].split("\n## ", 1)[0]

    def test_s10_the_document_states_the_three_things(self):
        body = " ".join(self.section().split())
        for statement in ("allowed when it is on the default branch, at a commit that is an ancestor of (or equal to) the remote default branch",
                          "`install`, `upgrade` and `doctor`", "How the operator moves the tree"):
            self.assertIn(statement, body)

    def test_the_cause_note_answers_the_three_questions(self):
        body = " ".join(self.section().split())
        for phrase in ("Who can run git there", "Which step could do it", "found by a floor verdict"):
            self.assertIn(phrase, body)


# ---- S11: no advice to move the installed tree's branch without saying where


GIT_ADVICE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|switch|merge|pull)\s+[^\s`]")
WORKTREE_WORDS = re.compile(r"worktree|clone", re.IGNORECASE)
NOT_ADVICE = re.compile(r"effect:\s*deny")   # a permission rule that FORBIDS the command


def advising_lines(text):
    """(line number, line) of every advising line of a markdown text that does not say it is done in a worktree or a clone.

    Closed rule: an advising line is a line of the file (prose, list item, table row or a line inside a code fence) that holds `git` followed by checkout,
    switch, merge or pull and an argument, and is not a permission rule that denies it. It passes when the line itself, or the paragraph it is in (the
    run of non-blank lines; for a line in a code fence, the fence and the paragraph just before it), says `worktree` or `clone`.
    Known limits: advice written in prose without the command words (\"check out integration\"), a command split over lines, advice inside an image,
    and a fence whose opening this scan cannot see (an indented or tilde fence) are not found.
    """
    lines = text.splitlines()
    found, fence_start, fence_before = [], None, None
    blocks, current = [], []
    for number, line in enumerate(lines):
        if line.strip().startswith("```"):
            fence_start = None if fence_start is not None else number
            if fence_start is not None:
                fence_before = number
        if line.strip():
            current.append(number)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    block_of = {n: i for i, block in enumerate(blocks) for n in block}
    inside = False
    for number, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if not GIT_ADVICE.search(line) or NOT_ADVICE.search(line):
            continue
        context = list(blocks[block_of[number]])
        if inside and block_of[number] > 0:
            context += blocks[block_of[number] - 1]
        if not any(WORKTREE_WORDS.search(lines[n]) for n in context):
            found.append((number + 1, line.strip()))
    return found


class TestAdviceNamesAWorktree(unittest.TestCase):
    ROOTS = ("docs", "skills", "agents")

    def files(self):
        return sorted(p for root in self.ROOTS for p in (REPO / root).rglob("*.md"))

    def test_s11_every_advising_line_says_it_is_done_in_a_worktree_or_a_clone(self):
        bad = ["%s:%d: %s" % (p.relative_to(REPO), n, t[:100]) for p in self.files() for n, t in advising_lines(p.read_text())]
        self.assertEqual(bad, [])

    def test_the_scan_finds_the_files_it_is_meant_to_read(self):
        names = {p.relative_to(REPO).as_posix() for p in self.files()}
        self.assertIn("docs/switchboard.md", names)
        self.assertIn("skills/fleet-coordination/SKILL.md", names)
        self.assertTrue([n for n in names if n.startswith("agents/")])

    def test_a_planted_advising_line_without_the_wording_is_found_and_with_it_is_not(self):
        plain = "3. After the merge: `git fetch origin`, `git checkout integration`, `git merge origin/main`, push.\n"
        self.assertEqual(len(advising_lines(plain)), 1)   # the batching step as it was written
        self.assertEqual(advising_lines(plain.replace("After the merge:", "After the merge, in a worktree of your own:")), [])
        self.assertEqual(len(advising_lines("```\ngit switch main\n```\n")), 1)
        self.assertEqual(advising_lines("Run this in a clone:\n\n```\ngit switch main\n```\n"), [])
        self.assertEqual(advising_lines("  - {action: shell, resource: \"git merge *\", effect: deny}\n"), [])
        self.assertEqual(advising_lines("The command `git merge` is walked.\n"), [])
        recorded = "3. After every direct merge to the default branch: `git fetch origin`, `git checkout integration`, `git merge origin/<default>` (never a rebase), push."
        self.assertEqual(len(advising_lines(recorded)), 1)

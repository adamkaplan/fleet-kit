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
guard = sb.installed_guard_module()
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
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
        (self.state / "installed-tree").write_text("%s\nmain\n" % self.tree)
        self.patch = mock.patch.object(sb, "KIT_ROOT", self.tree)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def command(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = sb.main(list(argv))
        return code, err.getvalue()


class UnreadableTreeTests:
    """Plan rev 7, rule 4: a git read that fails without raising leaves the tree UNREADABLE. A command that records a verdict, a gate, a clear, a merge or a
    proof refuses; a plain read command runs with a note; the daemon skips that pass with the alert. The recording commands are DERIVED from the registry
    (the flags on the command handlers), never listed by hand here."""

    def break_tree(self):
        (self.tree / ".git" / "HEAD").write_text("garbage\n")

    def mend_tree(self):
        (self.tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    def known(self):
        return sb.build_parser().commands

    def recording(self):
        return sorted(name for name in self.known() if sb.command_records(name, self.known()))

    def test_the_recording_commands_are_derived_from_the_registry_and_include_the_ones_that_matter(self):
        recording = set(self.recording())
        for name in ("prove", "verdict", "item-prove", "item-verdict", "waive", "gates", "gates-check", "merge-gate", "impact", "cloud-clear", "review", "run"):
            self.assertIn(name, recording)
        for name in ("status", "version", "intent", "pending", "send", "pr-state", "whoami"):
            self.assertNotIn(name, recording)
        flagged = {name for name in self.known() if getattr(sb.resolve_command(name, self.known()), "ledger", False)}
        self.assertTrue(flagged and flagged <= recording)   # every command of the evidence ledger records

    def test_every_command_whose_name_says_it_records_is_a_recording_command(self):
        """The completeness check: a command named for a verdict, a gate, a proof, a clear or a merge that no flag marks fails here."""
        self.assertEqual(guard.unflagged_recording_names(self.known(), set(self.recording())), [])

    def test_a_new_verdict_or_merge_command_is_covered_or_the_check_names_it(self):
        flagged = lambda args: 0
        flagged.ledger = True
        forgotten = lambda args: 0
        with mock.patch.dict(sb.__dict__, {"cmd_zap_verdict": flagged, "cmd_zap_merge": forgotten}):
            known = set(self.known()) | {"zap-verdict", "zap-merge"}
            self.assertTrue(sb.command_records("zap-verdict", known))                     # a flagged one is covered at once
            self.assertFalse(sb.command_records("zap-merge", known))
            recording = {name for name in known if sb.command_records(name, known)}
            self.assertEqual(guard.unflagged_recording_names(known, recording), ["zap-merge"])   # an unflagged one is named

    def test_every_recording_command_refuses_on_an_unreadable_tree_and_every_other_runs_with_a_note(self):
        self.break_tree()
        known = self.known()
        for name in sorted(known):
            if name in ("install", "run"):
                continue
            with self.subTest(name=name):
                line = sb.installed_tree_refusal(name, records=sb.command_records(name, known))
                if name in self.recording():
                    self.assertIsNotNone(line)
                    self.assertEqual(len(line.splitlines()), 1)
                    self.assertIn("could not be read", line)
                else:
                    self.assertIsNone(line)

    def test_end_to_end_a_merge_gate_is_refused_and_status_runs_with_a_note(self):
        self.break_tree()
        code, err = self.command("merge-gate", "acme/widget#1")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("could not be read", err)
        with mock.patch.object(sb, "dispatch", lambda *a, **k: 0):
            code, err = self.command("status")
        self.assertEqual(code, 0)
        self.assertIn("could not be read", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.mend_tree()
        self.assertEqual(self.command("version"), (0, ""))

    def test_each_recording_command_is_load_bearing_one_mutant_per_member(self):
        self.break_tree()
        known = self.known()
        for name in self.recording():
            if name in ("run",):
                continue
            with self.subTest(name=name):
                self.assertIsNotNone(sb.installed_tree_refusal(name, records=sb.command_records(name, known)))
                self.assertIsNone(sb.installed_tree_refusal(name, records=False))   # the mutant: this member read as a plain command runs

    def test_a_mutant_that_treats_unreadable_as_fine_is_caught(self):
        source = (BIN / "installed_guard.py").read_text()
        marker = "if records:   # the unreadable tree"
        self.assertIn(marker, source)
        namespace = {"__name__": "mutant_guard"}
        exec(compile(source.replace(marker, "if False:   # mutant"), "mutant_guard.py", "exec"), namespace)
        self.break_tree()
        mutant = namespace["entry_problem_line"]("merge-gate", self.state, self.tree, subprocess.run, True)
        self.assertEqual(mutant[0], None)                                             # the mutant lets the merge gate run
        self.assertIsNotNone(guard.entry_problem_line("merge-gate", self.state, self.tree, subprocess.run, True)[0])

    def test_the_daemon_skips_a_pass_on_an_unreadable_tree_alerts_and_resumes(self):
        fakes = FakePrograms(self.home)
        os.environ[sb.INSTALLED_TREE_ALERT_ENV] = str(fakes.notifier)
        engine = FakeEngine([1_800_000_000.0], fakes.herdr)

        def run(count):
            with mock.patch.object(sb, "next_poll", lambda e, p: p), mock.patch.dict(sys.modules, {sb.__name__: sb}), \
                    mock.patch.object(sb.load_module("cloud_relay_daemon"), "pass_if_configured", lambda *a, **k: None):
                sb.run_daemon(engine, self.state, 60, wait=lambda sock, timeout: None, passes=count)
        self.break_tree()
        run(2)
        self.assertEqual(engine.passes, 0)
        self.assertEqual(fakes.count(fakes.notifier_log), 1)
        self.assertIn("could not be read", (self.home / "notify.log").read_text())
        self.mend_tree()
        run(2)
        self.assertEqual(engine.passes, 2)


class TestTheEntryGuard(UnreadableTreeTests, TreeCase):
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
        always_ok = lambda root, branch, run=subprocess.run: {"ok": True, "note": None, "unreadable": False, "branch": "main", "commit": None, "dirty": [], "remote": None, "why": None}
        mutant = sb.installed_guard_module()
        mutant.check = always_ok   # the entry loads its own fresh copy of the module: the mutant is what it loads
        with mock.patch.object(sb, "installed_guard_module", return_value=mutant):
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
        self.assertEqual(set(guard.EXEMPT_COMMANDS), {"install", "upgrade", "doctor"})   # `run` (the daemon) is exempt in the entry only, and only without --once

    def test_s5_install_itself_runs_on_a_bad_tree(self):
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        with mock.patch.object(sb, "dispatch", lambda *a, **k: 0):
            self.assertEqual(self.command("install", "--dry-run")[0], 0)
            self.assertEqual(self.command("status")[0], 1)

    def test_a_tree_the_install_never_recorded_is_not_judged(self):
        (self.state / "installed-tree").unlink()
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 0)

    def test_a_recorded_tree_that_is_not_this_one_is_not_judged(self):
        (self.state / "installed-tree").write_text("%s\nmain\n" % (self.home / "elsewhere"))
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 0)

    def test_a_fresh_clone_without_the_remote_ref_is_allowed_with_a_note_and_a_detached_one_is_not(self):
        git(self.tree, "update-ref", "-d", "refs/remotes/origin/main")
        code, err = self.command("version")
        self.assertEqual(code, 0)
        self.assertIn("ancestry unverified", err)
        self.assertIn("ancestry unverified", guard.note(self.state, self.tree))
        git(self.tree, "switch", "-q", "--detach", "HEAD")
        self.assertEqual(self.command("version")[0], 1)

    def test_the_guard_that_cannot_run_refuses_all_but_the_exempt(self):
        with mock.patch.object(sb, "installed_guard_module", side_effect=ImportError("stub")):
            self.assertIn("could not run", sb.installed_tree_refusal("status"))
            self.assertIsNone(sb.installed_tree_refusal("install"))

    def test_a_failed_read_of_the_tree_is_no_evidence_so_the_command_runs(self):
        """A git failure, a timeout or a missing git: the guard allows and says so; only a read that succeeded and found something wrong refuses."""
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        self.assertEqual(self.command("version")[0], 1)
        for failure in (subprocess.TimeoutExpired("git", 20), FileNotFoundError("git"),
                        subprocess.CompletedProcess(["git"], 128, "", "fatal: another git process seems to be running")):
            def broken(argv, **kw):
                if isinstance(failure, BaseException):
                    raise failure
                return failure
            with self.subTest(failure=repr(failure)):
                self.assertIsNone(guard.problem(self.state, self.tree, broken))
                self.assertIn("unreadable", guard.note(self.state, self.tree, broken))

    def test_a_moved_tree_is_allowed_after_a_fetch_a_fast_forward_and_at_the_default_tip(self):
        commit(self.origin, "four.txt")
        git(self.tree, "fetch", "-q", "origin")
        self.assertEqual(self.command("version"), (0, ""))   # behind the remote: an ancestor
        git(self.tree, "pull", "-q", "--ff-only")
        self.assertEqual(self.command("version"), (0, ""))
        git(self.tree, "switch", "-q", "--detach", "origin/main")
        self.assertEqual(self.command("version")[0], 0)      # a detached head at the default tip
        git(self.tree, "switch", "-q", "main")
        commit(self.tree, "local.txt")                       # a local commit the remote does not hold
        self.assertEqual(self.command("version")[0], 1)


    # ---- the review round (rev-1): the daemon, the default branch, the caller's git environment, a failed rev-parse

    def test_a_restarted_daemon_on_a_bad_tree_is_not_refused_at_the_entry_and_alerts(self):
        """`run` (the daemon, which checks the tree itself at its start and on every pass) is not refused at the entry: a refusal there makes the service
        manager restart it for ever with no alert. `run --once` decides and delivers by itself, so it stays guarded."""
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        fakes = FakePrograms(self.home)
        os.environ[sb.INSTALLED_TREE_ALERT_ENV] = str(fakes.notifier)
        engine = FakeEngine([1_800_000_000.0], fakes.herdr)

        def daemon(args, parser_commands, **kw):
            with mock.patch.object(sb, "next_poll", lambda e, p: p), mock.patch.dict(sys.modules, {sb.__name__: sb}), \
                    mock.patch.object(sb.load_module("cloud_relay_daemon"), "pass_if_configured", lambda *a, **k: None):
                sb.run_daemon(engine, self.state, 60, wait=lambda sock, timeout: None, passes=1)
            return 0
        with mock.patch.object(sb, "dispatch", daemon):
            code, err = self.command("run")
        self.assertEqual((code, err), (0, ""))                      # not refused: the service manager has nothing to restart
        self.assertEqual(fakes.count(fakes.notifier_log), 1)        # and the alert went out
        self.assertEqual(engine.passes, 0)                          # with nothing decided
        self.assertEqual(self.command("run", "--once")[0], 1)       # the one-shot pass has no check of its own: the entry guards it

    def test_the_default_branch_is_not_assumed_to_be_main(self):
        git(self.origin, "branch", "-q", "-m", "main", "master")
        git(self.tree, "fetch", "-q", "origin", "--prune")
        git(self.tree, "switch", "-q", "-c", "master", "origin/master")
        git(self.tree, "branch", "-q", "-D", "main")
        git(self.tree, "remote", "set-head", "origin", "-d")       # origin/HEAD unknown
        (self.state / "installed-tree").unlink()
        steps = {step: (status, detail) for step, status, detail in guard.install(self.tree, self.state, BIN, sys.executable)}
        self.assertEqual(guard.read_marker(self.state), (os.path.realpath(str(self.tree)), "master"), steps)
        self.assertEqual(self.command("version"), (0, ""))

    def test_an_unknown_default_branch_is_not_recorded_and_the_install_says_so(self):
        git(self.origin, "branch", "-q", "master", "main")          # both main and master exist: which is the default is not known
        git(self.tree, "fetch", "-q", "origin")
        git(self.tree, "remote", "set-head", "origin", "-d")
        (self.state / "installed-tree").unlink()
        steps = {step: (status, detail) for step, status, detail in guard.install(self.tree, self.state, BIN, sys.executable)}
        self.assertIsNone(guard.read_marker(self.state))
        self.assertEqual(steps["installed tree"][0], "skipped")
        self.assertIn("git remote set-head origin", steps["installed tree"][1])

    def test_the_callers_git_environment_does_not_move_the_guard_to_another_repository(self):
        other = self.home / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "feature")
        commit(other, "x.txt")
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        for name, value in (("GIT_DIR", str(other / ".git")), ("GIT_WORK_TREE", str(other)), ("GIT_INDEX_FILE", str(other / ".git" / "index"))):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                line = guard.problem(self.state, self.tree)
                self.assertIsNotNone(line)
                self.assertIn("branch scratch", line)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            (self.state / "installed-tree").unlink()
            guard.install(self.tree, self.state, BIN, sys.executable)
        self.assertTrue((self.tree / ".git" / "hooks" / "post-checkout").is_file())
        self.assertFalse((other / ".git" / "hooks" / "post-checkout").exists())

    def rev_parse_fails(self, code=128, out=""):
        real = subprocess.run

        def run(argv, **kw):
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, code, out, "fatal: bad")
            return real(argv, **kw)
        return run

    def test_a_rev_parse_that_fails_without_raising_is_no_evidence_so_the_command_runs(self):
        run = self.rev_parse_fails()
        self.assertIsNone(guard.problem(self.state, self.tree, run))
        self.assertIn("unreadable", guard.note(self.state, self.tree, run))

    def test_a_mutant_that_reads_a_failed_rev_parse_as_no_commit_is_caught(self):
        source = (BIN / "installed_guard.py").read_text()
        fixed = 'result["ok"], result["unreadable"], result["note"] = True, True, "unreadable: git rev-parse failed"\n        return result'
        self.assertIn(fixed, source)
        namespace = {"__name__": "mutant_guard"}
        exec(compile(source.replace(fixed, 'result["why"] = "no commit"'), "mutant_guard.py", "exec"), namespace)
        run = self.rev_parse_fails()
        self.assertIsNotNone(namespace["problem"](self.state, self.tree, run))     # the mutant refuses on a read failure
        self.assertIsNone(guard.problem(self.state, self.tree, run))               # the real code does not

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
        stub = mock.patch.object(sb, "installed_guard_module", side_effect=ImportError("a stub that raises on import"))
        stub.start()
        self.addCleanup(stub.stop)
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
        (self.state / "installed-tree").unlink()   # setUp recorded the tree already
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

    def test_a_hook_that_cannot_record_never_fails_the_git_command(self):
        self.install()
        shutil_state = self.state / "guard"
        (shutil_state / "guard_record_head_move.py").write_text("import sys\nsys.exit(7)\n")
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")   # git asserts exit status 0
        self.assertIn("exit 0", (self.tree / ".git" / "hooks" / "post-checkout").read_text())

    def test_the_recorder_that_cannot_write_says_so_and_exits_zero(self):
        done = subprocess.run([sys.executable, str(BIN / "guard_record_head_move.py"), str(self.home / "no-such-dir"), "post-checkout", "a", "b", "1"],
                              capture_output=True, text=True, cwd=str(self.tree))
        self.assertEqual(done.returncode, 0)
        self.assertIn("could not record", done.stderr)

    def test_a_failed_git_directory_read_skips_the_hooks_and_still_records_the_tree(self):
        real = subprocess.run

        def runner(argv, **kw):
            return subprocess.CompletedProcess(argv, 128, "", "fatal") if "--git-common-dir" in argv else real(argv, **kw)
        (self.state / "installed-tree").unlink()
        steps = {step: status for step, status, _ in guard.install(self.tree, self.state, BIN, sys.executable, run=runner)}
        self.assertEqual((steps["tree hook checkout"], steps["tree hook merge"], steps["installed tree"]), ("skipped", "skipped", "done"))


    def test_the_record_keeps_executable_names_only_and_never_an_argument(self):
        self.install()
        git(self.tree, "switch", "-q", "-c", "scratch", "origin/integration")
        line = [json.loads(l) for l in (self.state / "audit.jsonl").read_text().splitlines() if "head_moved" in l][0]
        for ancestor in line["detail"]["ancestors"]:
            self.assertNotIn(" ", ancestor["command"], ancestor)
            self.assertNotIn("/", ancestor["command"], ancestor)
        self.assertEqual(sorted(line["detail"]["ancestors"][0]), ["command", "pid", "ppid"])
        self.assertLessEqual(len(line["detail"]["ancestors"]), 16)

    def test_the_recorder_takes_the_audits_append_lock(self):
        import fcntl
        import time
        lock = self.state / "audit.jsonl.append.lock"
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)           # a rotation holds it exclusively for its swap
        proc = subprocess.Popen([sys.executable, str(BIN / "guard_record_head_move.py"), str(self.state), "post-merge"], cwd=str(self.tree),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "the recorder wrote while the audit was locked")
            fcntl.flock(fd, fcntl.LOCK_UN)
            self.assertEqual(proc.wait(timeout=20), 0)
        finally:
            os.close(fd)
            if proc.poll() is None:
                proc.kill()
        self.assertIn("head_moved", (self.state / "audit.jsonl").read_text())

    def test_a_hook_that_is_not_ours_is_left_alone(self):
        hooks = self.tree / ".git" / "hooks"
        (hooks / "post-merge").write_text("#!/bin/sh\necho mine\n")
        steps = {step: (status, detail) for step, status, detail in self.install()}
        self.assertEqual(steps["tree hook merge"][0], "skipped")
        self.assertEqual((hooks / "post-merge").read_text(), "#!/bin/sh\necho mine\n")

    def test_the_install_records_the_tree_and_the_default_branch(self):
        (self.state / "installed-tree").unlink()
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


    def test_the_document_states_the_limits(self):
        body = " ".join(self.section().split())
        for phrase in ("reverses", "records a verdict, a gate, a clear, a merge or a proof", "could not be read", "lives in the tree it guards", "deleting `installed-tree`", "records the tree it is run from", "core.hooksPath",
                       "origin", "fileMode", "executable names"):
            self.assertIn(phrase, body)

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

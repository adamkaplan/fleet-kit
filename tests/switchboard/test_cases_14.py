"""Test classes moved out of bin/test-switchboard (text unchanged): cases_14."""
import argparse
import ast
import traceback
import base64
import calendar
import collections
import contextlib
import errno
import copy
import datetime as _dt
import fcntl
import gzip
import fnmatch
import hashlib
import hmac
import importlib.machinery
import importlib.util
import inspect
import http.client
import io
import itertools
import select
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import shlex
import shutil
import textwrap
import zipfile
import signal
import socket
import stat
import tarfile
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
import uuid
import urllib.parse
from unittest import mock
from support.core import Base
from support.loader import SWITCHBOARD, captured, sb, switchboard_source
from support.paths import REPO
from support.rebind import rebound
from support.rigs import World, made_up_pid
from support.service_and_merge import FakeMergeGitHub, MG_HEAD, MG_NEWTIP


class TestFocusedLocalRuns(unittest.TestCase):
    """#942: the documented default (focused runs, CI runs the whole suite), the refusal of a second full-suite job in a worktree and the preflight skip of a sub-check
    CI runs. Real slot files and queue in a temp state directory, a fake worktree function, a stub script as the command; nothing real is run. Each family is ONE table;
    each row has a test and a mutant (the defence swapped for a weaker one with mock.patch, and the attack then succeeds)."""
    DEFAULT = "Run the focused classes of what you changed through `heavy --short`; CI runs the whole suite."
    EXCEPTION = "Run the whole suite only for a change to the CI workflow itself or to a test CI cannot run; say which in the pull request."
    REFUSAL = "The heavy wrapper refuses a second full-suite job in a worktree while one is queued or running."
    FILES = ("skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/copilot/coder.agent.md", "docs/switchboard.md")
    A, B = "/work/a", "/work/b"

    def setUp(self):
        self.init()
        self.addCleanup(self.cleanup)

    def reset(self):
        self.cleanup()
        self.init()

    def init(self):
        self.hs = sb.load_heavy_slots()
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state")
        os.mkdir(self.state)
        self.script = os.path.join(self.dir, "test-fake")   # a program that classifies as full
        Path(self.script).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.script, 0o755)
        self.bin = os.path.join(self.dir, "bin")   # the only PATH the child gets: a fake `timeout` (macOS ships none, Ubuntu ships GNU's); no real program is needed
        os.mkdir(self.bin)
        os.symlink(shutil.which("sh"), os.path.join(self.bin, "sh"))   # `sh -c` is on both runners
        self.fake_timeout()
        self.children, self.fds = [], []

    def cleanup(self):
        for child in self.children:
            child.kill()
            child.wait()
        for fd in self.fds:
            os.close(fd)
        self.children, self.fds = [], []
        shutil.rmtree(self.dir, True)

    def fake_timeout(self):
        path = os.path.join(self.bin, "timeout")
        Path(path).write_text('#!/bin/sh\nshift\nexec "$@"\n')   # drops the duration, runs the rest
        os.chmod(path, 0o755)

    def environ(self):
        return {"PATH": self.bin, "FLEET_HEAVY_NAME": "second"}

    def start(self, command, worktree, short=False, wait_max=1):
        out = io.StringIO()
        code = self.hs.run(command, self.state, 3, wait_max, "second", environ=self.environ(), out=out, short=short, worktree=lambda cwd: worktree)
        return code, out.getvalue()

    def hold(self, worktree, kind="full"):
        """A job RUNNING in `worktree`: a slot whose lock is held, with the record a wrapper writes."""
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        self.hs._record(fd, "first", os.getpid(), "timeout", time.time(), {"kind": kind, "worktree": worktree, "program": "test-switchboard"})

    def queue(self, worktree, kind="full"):
        """A job QUEUED in `worktree`: an entry of a live process (a sleeping child) in the waiting line."""
        child = subprocess.Popen(["sleep", "60"])
        self.children.append(child)
        line = self.hs.Line(self.state, "first", pid=child.pid, meta={"kind": kind, "worktree": worktree, "program": "test-switchboard", "since": int(time.time())})
        line.place()
        # every slot busy, so the entry stays in the line (a ticket never outlives the slot its owner took)

    def fill(self):
        for _ in range(3):
            fd = self.hs._take(self.state, 3)[0]
            os.ftruncate(fd, 0)   # a busy slot that has no record (yet)
            self.fds.append(fd)

    # ---- THE refusal table: (id, how the first job exists, its worktree, the second command, the second's worktree, refused?)
    FULL, FOCUSED, WRAPPED = ["{s}"], ["{s}", "-k", "x"], ["timeout", "900", "{s}"]
    ROWS = (
        ("running-same-worktree", "run", "/work/a", FULL, "/work/a", True),
        ("queued-same-worktree", "queue", "/work/a", FULL, "/work/a", True),
        ("wrapped-in-timeout", "run", "/work/a", WRAPPED, "/work/a", True),
        ("focused-not-refused", "run", "/work/a", FOCUSED, "/work/a", False),
        ("other-worktree-not-refused", "run", "/work/a", FULL, "/work/b", False),
        ("first-ended-not-refused", "none", "/work/a", FULL, "/work/a", False),
        ("first-is-focused-not-refused", "run-focused", "/work/a", FULL, "/work/a", False),
        ("worktree-unknown-not-refused", "run", "/work/a", FULL, None, False),
    )

    def attempt(self, row):
        _id, how, first_tree, command, second_tree, _refused = row
        if how == "run":
            self.hold(first_tree)
        elif how == "run-focused":
            self.hold(first_tree, kind="focused")
        elif how == "queue":
            self.fill()
            self.queue(first_tree)
        command = [self.script if word == "{s}" else word for word in command]
        if how == "queue":   # the queue entry exists; the second run must be refused before it waits (a refusal needs no slot)
            return self.start(command, second_tree, wait_max=0)
        return self.start(command, second_tree)

    def test_every_row_of_the_refusal_table(self):
        for row in self.ROWS:
            with self.subTest(row=row[0]):
                self.reset()
                code, text = self.attempt(row)
                if row[5]:
                    self.assertEqual(code, 64)
                    self.assertIn("heavy: refused: a full-suite job is already queued or running in this worktree (first, test-switchboard, pid ", text)
                    self.assertIn(", since ", text)
                    if row[1] == "run":
                        self.assertIn("pid %d" % os.getpid(), text)
                else:
                    self.assertEqual(code, 0, text)
                    self.assertNotIn("refused", text)

    def test_a_mutant_without_the_fake_timeout_fails_the_wrapped_row_as_command_not_found_on_every_os(self):
        row = [r for r in self.ROWS if r[0] == "wrapped-in-timeout"][0]
        os.unlink(os.path.join(self.bin, "timeout"))
        self.hold(self.A)
        code, text = self.attempt(row)
        self.assertEqual(code, 127, text)

    def test_a_mutant_that_never_refuses_lets_every_refused_row_run(self):
        for row in self.ROWS:
            if row[5]:
                with self.subTest(row=row[0]), mock.patch.object(self.hs, "second_job_notice", lambda *a, **k: None):
                    self.reset()
                    self.assertNotEqual(self.attempt(row)[0], 64)

    def test_a_mutant_that_always_refuses_stops_every_allowed_row(self):
        for row in self.ROWS:
            if not row[5]:
                with self.subTest(row=row[0]), mock.patch.object(self.hs, "second_job_notice", lambda *a, **k: "heavy: refused: x"):
                    self.reset()
                    code = self.attempt(row)[0]
                    if row[4] is not None and row[0] != "focused-not-refused":   # a focused run or an unknown worktree never asks
                        self.assertEqual(code, 64)
                    else:
                        self.assertEqual(code, 0)

    def test_the_table_covers_every_kind_of_the_one_classifier(self):
        kinds = {kind for kind, _s, _m in self.hs.KINDS} - {"quick"}   # "quick" is a ceiling row taken by name (run_quick); the classifier never returns it
        probes = {"full": [], "focused": ["-k", "x"], "e2e": ["playwright", "test"], "other": ["ls"]}
        self.assertEqual(set(probes), kinds, "a kind with no probe")
        for kind, rest in probes.items():
            with self.subTest(kind=kind):
                self.reset()
                command = [self.script] + rest if kind in ("full", "focused") else rest
                self.hold(self.A)   # a full job is running in worktree A
                code, text = self.start(command, self.A) if kind in ("full", "focused") else (None, "")
                self.assertEqual(self.hs.classify(command), kind)
                self.assertEqual(code == 64, kind == "full") if code is not None else None

    def test_every_other_kind_runs_while_a_full_job_holds_the_worktree(self):
        for kind, rest in (("focused", ["-k", "x"]), ("other", None), ("e2e", None)):
            with self.subTest(kind=kind):
                self.reset()
                self.hold(self.A)
                if rest is None:
                    with mock.patch.object(self.hs, "classify", lambda command, short=False, kind=kind: kind):
                        code, text = self.start([self.script], self.A)
                else:
                    code, text = self.start([self.script] + rest, self.A)
                self.assertEqual(code, 0, text)

    def test_the_wrapper_is_a_visible_holder_before_it_leaves_the_line(self):
        seen = []
        real = self.hs.Line.leave

        def leave(line):
            seen.extend(i for i in self.hs._held_records(self.state) if i.get("worktree") == self.A and i.get("kind") == "full")
            return real(line)
        with mock.patch.object(self.hs.Line, "leave", leave):
            self.assertEqual(self.start([self.script], self.A)[0], 0)
        self.assertTrue(seen, "no record naming the worktree when the line was left")

    def test_the_marker_preflight_looks_for_is_in_the_text_the_wrapper_writes(self):
        hs = sb.load_heavy_slots()
        text = hs.second_job_notice(self.state_for_marker(), "/work/a", [{"kind": "full", "worktree": "/work/a", "name": "n", "pid": 1, "since": 1}])
        source = switchboard_source()
        literal = re.search(r'"(heavy: refused: [^"]+)" in \(done\.stderr', source).group(1)
        self.assertTrue(text.startswith(literal), text)

    def state_for_marker(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_a_test_that_itself_exits_64_is_a_plain_failure_not_a_refusal(self):
        def runner(argv, **kwargs):
            return mock.Mock(returncode=64 if "heavy" in argv else 0, stdout="/r\n", stderr="usage error\n" if "heavy" in argv else "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            self.assertEqual(sb.cmd_preflight_pr(None, runner, time.time), 1)
        self.assertIn("FAIL cli-surface (", out.getvalue())
        self.assertIn("exit 64:", out.getvalue())
        self.assertNotIn("refused", out.getvalue())

    def test_preflight_reports_a_refused_sub_check_as_a_fail_that_says_so(self):
        def runner(argv, **kwargs):
            done = mock.Mock(returncode=64 if "heavy" in argv else 0, stdout="/r\n", stderr="heavy: refused: a full-suite job is already queued or running in this worktree (first, x, pid 1, since t)\n" if "heavy" in argv else "")
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            self.assertEqual(sb.cmd_preflight_pr(None, runner, time.time), 1)
        self.assertIn("FAIL cli-surface (", out.getvalue())
        self.assertIn("refused: a full-suite job is already queued or running", out.getvalue())

    def test_u2_the_refusal_reads_the_kind_from_the_one_classifier(self):
        self.hold(self.A)
        with mock.patch.object(self.hs, "classify", lambda command, short=False: "focused"):
            self.assertEqual(self.start([self.script], self.A)[0], 0)
        with mock.patch.object(self.hs, "classify", lambda command, short=False: "full"):
            self.assertEqual(self.start(["sh", "-c", "true"], self.A)[0], 64)

    def test_u1_a_run_that_is_not_refused_prints_nothing_new_and_a_full_house_still_exits_75(self):
        code, text = self.start([self.script], self.A)
        self.assertEqual((code, text), (0, ""))
        self.fill()
        code, text = self.start([self.script], self.A, wait_max=0)
        self.assertEqual(code, 75)
        self.assertTrue(text.startswith("heavy: no slot after 0 s (3 of 3 busy: "), text)
        self.assertNotIn("refused", text)

    def test_s7_the_only_ceilings_are_the_per_kind_table(self):
        self.assertEqual(self.hs.KIND_DEFAULTS, {"e2e": 3600, "focused": 1800, "full": 5400, "quick": 600, "other": 3600})
        names = sorted(name for name in vars(self.hs) if re.search(r"(CEIL|SECONDS|MAX_SECONDS|HARD_MAX)", name) and name.isupper())
        self.assertEqual(names, ["DEFAULT_MAX_SECONDS", "EXPRESS_SECONDS", "HARD_MAX_SECONDS", "KILL_GRACE_SECONDS", "POLL_SECONDS", "SWEEP_SECONDS", "WAIT_REPORT_SECONDS", "WATCH_SECONDS"])

    def test_the_worktree_is_the_git_toplevel_by_real_path(self):
        def repo(name):
            path = os.path.join(self.dir, name)
            os.makedirs(os.path.join(path, "sub"))
            subprocess.run(["git", "init", "-q", path], check=True)
            return path
        one, two = repo("one"), repo("two")
        link = os.path.join(self.dir, "link")
        os.symlink(one, link)
        top = os.path.realpath(one)
        self.assertEqual(self.hs.worktree_of(one), top)
        self.assertEqual(self.hs.worktree_of(os.path.join(one, "sub")), top)
        self.assertEqual(self.hs.worktree_of(os.path.join(link, "sub")), top)
        self.assertNotEqual(self.hs.worktree_of(two), top)
        self.assertIsNone(self.hs.worktree_of(os.path.join(self.dir, "state")))   # no git tree here (a temp dir): nothing is refused
        self.assertIsNone(self.hs.worktree_of(os.path.join(self.dir, "missing")))

    # ---- the words, once each, the same in every file
    def test_s2_s8_each_sentence_is_in_each_file_once_and_the_same_words(self):
        for path in self.FILES:
            text = " ".join((REPO / path).read_text().split())
            for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL):
                with self.subTest(path=path, sentence=sentence[:30]):
                    self.assertEqual(text.count(sentence), 1)

    def sentences_once(self, text):
        text = " ".join(text.split())
        return all(text.count(sentence) == 1 for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL))

    def test_a_mutant_that_drops_the_exception_or_words_it_differently_fails_the_same_check(self):
        for path in self.FILES:
            text = (REPO / path).read_text()
            self.assertTrue(self.sentences_once(text), path)
            for name, mutant in (("dropped", text.replace(self.EXCEPTION, "")), ("reworded", text.replace("to a test CI cannot run", "to a slow test")),
                                 ("doubled", text + "\n" + self.EXCEPTION)):
                with self.subTest(path=path, mutant=name):
                    self.assertFalse(self.sentences_once(mutant))

    def write_record(self, record):
        """A slot held by this test with `record` written WHOLE (truncated first); read back as the dict meant, before anything is asserted of it."""
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
        self.assertEqual(self.hs._read_info(fd), record)
        return fd

    def dead_pid(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        return dead.pid

    LIVE_CASES = (   # (id, record fields, refused?)
        ("both-dead", {"pid": "DEAD", "wrapper": "DEAD"}, False),
        ("command-alive", {"pid": "LIVE", "wrapper": "DEAD"}, True),
        ("wrapper-alive", {"pid": "DEAD", "wrapper": "LIVE"}, True),
        ("pid-reused-by-another-process", {"pid": "LIVE", "cstart": "Mon Jan  1 00:00:00 1990", "wrapper": "DEAD"}, False),
        ("wrapper-pid-reused", {"pid": "DEAD", "wrapper": "LIVE", "wstart": "Mon Jan  1 00:00:00 1990"}, False),
        ("pid-with-its-real-start", {"pid": "LIVE", "cstart": "REAL", "wrapper": "DEAD"}, True),
    )

    def case(self, fields):
        live, dead = os.getpid(), self.dead_pid()
        record = {"kind": "full", "worktree": self.A, "name": "ghost", "program": "x", "since": 1}
        for key, value in fields.items():
            record[key] = live if value == "LIVE" else dead if value == "DEAD" else self.hs._start_of(live) if value == "REAL" else value
        return record

    def test_a_held_lock_is_refused_only_for_a_holder_that_is_really_running(self):
        for name, fields, refused in self.LIVE_CASES:
            with self.subTest(case=name):
                self.reset()
                self.write_record(self.case(fields))
                self.assertEqual(self.hs.second_job_notice(self.state, self.A, []) is not None, refused)

    def test_mutants_of_the_liveness_rule_flip_a_case(self):
        flips = {}
        for label, patch in (("always-live", mock.patch.object(self.hs, "_pid_alive", lambda pid, start=None: True)),
                             ("pid-only", mock.patch.object(self.hs, "LIVE_KEYS", (("pid", ""), ("wrapper", "")))),
                             ("wrapper-half-dropped", mock.patch.object(self.hs, "LIVE_KEYS", (("pid", "cstart"),))),
                             ("command-half-dropped", mock.patch.object(self.hs, "LIVE_KEYS", (("wrapper", "wstart"),)))):
            with patch:
                flips[label] = []
                for name, fields, refused in self.LIVE_CASES:
                    self.reset()
                    self.write_record(self.case(fields))
                    if (self.hs.second_job_notice(self.state, self.A, []) is not None) != refused:
                        flips[label].append(name)
        self.assertEqual(flips, {"always-live": ["both-dead", "pid-reused-by-another-process", "wrapper-pid-reused"],
                                 "pid-only": ["pid-reused-by-another-process", "wrapper-pid-reused"],
                                 "wrapper-half-dropped": ["wrapper-alive"], "command-half-dropped": ["command-alive", "pid-with-its-real-start"]})

    def test_a_malformed_record_is_read_without_error(self):
        self.assertIsNone(self.hs.second_job_notice(self.state, self.A, [{"kind": "full", "worktree": "/elsewhere"}]))
        text = self.hs.second_job_notice(self.state, self.A, [{"kind": "full", "worktree": self.A, "name": "\x1b[31mq", "pid": "x", "since": "y"}])
        self.assertIn("pid unknown, since unknown time", text)
        self.assertNotIn("\x1b", text)
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        os.ftruncate(fd, 0)
        os.pwrite(fd, b'{"kind": "full", "worktree"', 0)   # cut off: no record
        self.assertIsNone(self.hs.second_job_notice(self.state, self.A, []))

    def test_the_sentences_name_roles_never_agents_or_people(self):
        for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL):
            self.assertNotRegex(sentence, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")

    def test_s1_the_docs_state_the_measured_cause_before_the_default(self):
        text = " ".join((REPO / "docs" / "switchboard.md").read_text().split())
        section = text.split("**Focused by default, one full-suite job per worktree (#942).**", 1)[1]
        before = section.split(self.DEFAULT, 1)[0]
        for figure in ("19 full local runs", "14 worktrees", "Five of the 14 worktrees", "upper bound"):
            self.assertIn(figure, before)
        self.assertEqual(text.count("**Focused by default, one full-suite job per worktree (#942).**"), 1)

    # ---- preflight: THE table of workflow cases (name, workflow text, changed files, expected job or None)
    PR_ON = "on:\n  pull_request:\n  push:\n    branches: [main]\n"
    JOB = ("jobs:\n  test-cloud:\n    runs-on: ubuntu-latest\n    steps:\n      - name: launcher\n        if: ${{ !cancelled() }}\n        run: |\n"
           "          bin/test-cloud-launcher 2>&1 | tee out.txt\n")

    def cases(self):
        wf = self.PR_ON + self.JOB
        return (
            ("runs-on-pull-request", wf, ["cloud/x"], "test-cloud"),
            ("workflow-does-not-name-it", wf.replace("bin/test-cloud-launcher", "bin/test-other"), ["cloud/x"], None),
            ("workflow-unreadable", None, ["cloud/x"], None),
            ("branch-changes-the-workflow", wf, ["cloud/x", ".github/workflows/test.yml"], None),
            ("no-pull-request-trigger", wf.replace("pull_request:", "workflow_dispatch:"), ["cloud/x"], None),
            ("job-level-condition", wf.replace("    runs-on", "    if: github.ref == 'x'\n    runs-on"), ["cloud/x"], None),
            ("step-continue-on-error", wf.replace("        run: |", "        continue-on-error: true\n        run: |"), ["cloud/x"], None),
            ("step-condition-it-may-not-run", wf.replace("${{ !cancelled() }}", "${{ github.ref == 'x' }}"), ["cloud/x"], None),
            ("only-a-name-or-comment", wf.replace("          bin/test-cloud-launcher 2>&1 | tee out.txt", "          # bin/test-cloud-launcher\n          true"), ["cloud/x"], None),
            ("changed-files-unknown", wf, None, None),
            ("job-continue-on-error-true", wf.replace("    runs-on", "    continue-on-error: true\n    runs-on"), ["cloud/x"], None),
            ("job-continue-on-error-expression", wf.replace("    runs-on", "    continue-on-error: ${{ matrix.x }}\n    runs-on"), ["cloud/x"], None),
            ("job-continue-on-error-false-counts", wf.replace("    runs-on", "    continue-on-error: false\n    runs-on"), ["cloud/x"], "test-cloud"),
            ("step-continue-on-error-expression", wf.replace("        run: |", "        continue-on-error: ${{ true }}\n        run: |"), ["cloud/x"], None),
            ("run-only-echoes-the-name", wf.replace("          bin/test-cloud-launcher 2>&1 | tee out.txt", "          echo bin/test-cloud-launcher"), ["cloud/x"], None),
            ("single-line-run-echo", wf.replace("        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt", "        run: echo bin/test-cloud-launcher"), ["cloud/x"], None),
            ("single-line-run-counts", wf.replace("        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt", "        run: bin/test-cloud-launcher"), ["cloud/x"], "test-cloud"),
            ("failure-swallowed-with-or-true", wf.replace("tee out.txt", "tee out.txt || true"), ["cloud/x"], None),
            ("failure-swallowed-with-set-plus-e", wf.replace("          bin/test-cloud-launcher", "          set +e\n          bin/test-cloud-launcher"), ["cloud/x"], None),
            ("mentioned-after-another-command", wf.replace("          bin/test-cloud-launcher", "          true; bin/test-cloud-launcher"), ["cloud/x"], None),
            ("list-form-trigger", wf.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", "on: [push, pull_request]\n"), ["cloud/x"], "test-cloud"),
            ("trigger-with-paths-filter", wf.replace("  pull_request:\n", "  pull_request:\n    paths: ['docs/**']\n"), ["cloud/x"], None),
            ("trigger-with-branches-filter", wf.replace("  pull_request:\n", "  pull_request:\n    branches: [main]\n"), ["cloud/x"], None),
            ("pull-request-target-only", wf.replace("  pull_request:\n", "  pull_request_target:\n"), ["cloud/x"], None),
        )

    def preflight(self, workflow, changed):
        calls = []
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        if workflow is not None:
            os.makedirs(os.path.join(root, ".github", "workflows"))
            Path(root, ".github", "workflows", "test.yml").write_text(workflow)

        def runner(argv, **kwargs):
            calls.append(list(argv))
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = root + "\n"
            elif argv[:2] == ["git", "merge-base"]:
                done.stdout = "abc123\n" if changed is not None else ""
                done.returncode = 0 if changed is not None else 1
            elif argv[:2] == ["git", "diff"]:
                done.stdout = "\n".join(changed or [])
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual([c[:2] for c in calls if c[0] == "git"], [c[:2] for c in calls if c[0] == "git" and c[1] in ("rev-parse", "merge-base", "diff")])
        heavy = [" ".join(c[c.index("--") + 1:]) for c in calls if "heavy" in c]
        return code, out.getvalue(), heavy

    def test_s3_s4_every_workflow_case(self):
        for name, workflow, changed, job in self.cases():
            with self.subTest(case=name):
                code, out, heavy = self.preflight(workflow, changed)
                launcher = [c for c in heavy if "bin/test-cloud-launcher" in c]
                self.assertEqual(code, 0)
                if job:
                    self.assertEqual(launcher, [])
                    self.assertIn("preflight-pr: cloud launcher: skipped, CI runs it for this pull request (job %s)\n" % job, out)
                else:
                    self.assertEqual(len(launcher), 1)
                    self.assertNotIn("skipped", out)

    def test_every_job_level_key_that_can_hide_a_failure_is_refused_at_job_and_step_level(self):
        wf = self.PR_ON + self.JOB
        for key in sb.PREFLIGHT_JOB_KEYS_HIDING:
            value = "'x'" if key == "if" else "true"
            with self.subTest(key=key, level="job"):
                self.assertIsNone(sb.workflow_job_running(wf.replace("    runs-on", "    %s: %s\n    runs-on" % (key, value)), "bin/test-cloud-launcher"))
            with self.subTest(key=key, level="step"):
                text = wf.replace("        run: |", "        %s: %s\n        run: |" % (key, value)).replace("        if: ${{ !cancelled() }}\n", "", 1 if key == "if" else 0)
                self.assertIsNone(sb.workflow_job_running(text, "bin/test-cloud-launcher"))
        self.assertEqual(sb.workflow_job_running(wf, "bin/test-cloud-launcher"), "test-cloud")

    def test_a_mutant_that_forgets_a_job_level_key_skips_a_suite_ci_cannot_fail_on(self):
        wf = self.PR_ON + self.JOB.replace("    runs-on", "    continue-on-error: true\n    runs-on")
        with mock.patch.object(sb, "PREFLIGHT_JOB_KEYS_HIDING", ("if",)):
            self.assertEqual(sb.workflow_job_running(wf, "bin/test-cloud-launcher"), "test-cloud")   # the weakened rule would skip it
        self.assertIsNone(sb.workflow_job_running(wf, "bin/test-cloud-launcher"))

    def test_the_skip_leaves_the_other_cloud_suites_and_classes_as_they_were(self):
        code, out, heavy = self.preflight(self.PR_ON + self.JOB, ["cloud/x"])
        self.assertEqual([c for c in heavy if "cloud" in c], ["bin/test-cloud-worker", "cloud/tests/test-cloud"])
        self.assertEqual(len(heavy), 5)
        self.assertEqual([l.split()[:2] for l in out.splitlines() if l.startswith("PASS")], [["PASS", "cli-surface"], ["PASS", "hygiene"], ["PASS", "public-names"], ["PASS", "cloud"]])

    def test_every_sub_check_of_every_class_is_skipped_only_when_the_table_says_so(self):
        every = [" ".join(command) for _n, _c, commands, _u in sb.PREFLIGHT_CLASSES for command in commands]
        skippable = {command for command, _shown in sb.PREFLIGHT_CI_SKIPS}
        self.assertTrue(skippable <= set(every), "a skip row for a command no class runs")
        steps = "".join("      - run: %s\n" % command for command in every)
        workflow = self.PR_ON + "jobs:\n  everything:\n    steps:\n" + steps
        code, out, heavy = self.preflight(workflow, ["cloud/x"])
        for command in every:
            with self.subTest(command=command):
                self.assertEqual(command in heavy, command not in skippable)

    def test_a_mutant_that_always_runs_or_always_skips_the_sub_check_fails_a_case(self):
        with mock.patch.object(sb, "preflight_ci_runs", lambda *a, **k: None):
            self.assertIn("bin/test-cloud-launcher", self.preflight(self.PR_ON + self.JOB, ["cloud/x"])[2])
        with mock.patch.object(sb, "preflight_ci_runs", lambda *a, **k: "any-job"):
            self.assertNotIn("bin/test-cloud-launcher", self.preflight(None, ["cloud/x"])[2])


class TestItemVerdicts(unittest.TestCase):
    """#770 (plan fleet-kit#777): verdicts for a work item that has no pull request. Temp dirs and fakes only. Each attack has a test and a mutant: the
    mutant is the weakened defence swapped in with mock.patch, and the same attack then SUCCEEDS (the test would fail on it)."""

    REPO, N, ITEM = "acme-org/widget-service", 777, "acme-org/widget-service#777"
    PIN, MOVED = "a" * 40, "b" * 40
    T0 = 1_000_000

    def setUp(self):
        self.iv = sb.load_module("item_verdicts")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = Path(self.dir)
        self.clock = [self.T0]
        self.audit = []
        self.sessions = {"orch": "ses_o", "t1": "ses_t1", "t2": "ses_t2", "c1": "ses_c1", "n1": "ses_n1"}
        self.fleets = {"orch": {"role": "orchestrator", "repo": self.REPO, "charter": 587},
                       "t1": {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N},
                       "t2": {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 778},
                       "c1": {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N},
                       "n1": {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 700}}
        self.scenarios = [{"id": "S1", "evaluator": "tester", "proof": "live", "claim": "pr"},
                          {"id": "S2", "evaluator": "ci", "proof": "test", "claim": "pr"},
                          {"id": "S3", "evaluator": "tester", "proof": "measure", "claim": "epic"},
                          {"id": "S4", "evaluator": "tester", "proof": "measure"}]
        self.plan = {"result": "pass", "defects": [], "scenarios_parsed": self.scenarios, "plan_hash": "sha256:p", "text": "", "source": "plan"}
        self.pages = {}

    def now(self):
        return self.clock[0]

    def entries(self):
        return sb.plan_ledger.read_ledger(self.state)[0]

    def register(self, tester="t1", item=None, head=None, orch="orch", session=None):
        item = item or self.ITEM
        return sb.plan_ledger.append_entry(self.state, {"kind": "item_tester", "pr": item, "item": item, "tester": tester, "session": session or self.sessions.get(tester),
                                                         "orchestrator": orch, "head": head or self.PIN, "plan_ref": None, "by": orch}, self.now())

    def ctx(self, author="human", own=True, item=None):
        item = item or self.ITEM
        number = int(item.rsplit("#", 1)[1])

        class Audit:
            def append(inner, actor, event, subject=None, detail=None, **extra):
                self.audit.append((event, subject, detail))
        intents = types.SimpleNamespace(_api=lambda path, repo=None: self.pages[path] if path in self.pages else (_ for _ in ()).throw(sb.IntentError("not_found", path)))
        engine = types.SimpleNamespace(intents=intents, audit=Audit(), actor="t", herdr=None, v2=None)
        return types.SimpleNamespace(engine=engine, state=self.state, config={"plan_gates": {"repos": {self.REPO: {"item_checks": [{"name": "build", "app": "github-actions"}]}}}},
                                     repo=self.REPO, number=number, pr=item, item=item, entries=self.entries(), problem=None, facts={}, unreadable=None, source="plan",
                                     plan=self.plan, plan_problem=None, issue={"author": author, "body": "", "state": "open", "parent": None}, plan_own=own)

    def args(self, **kw):
        base = dict(item=self.ITEM, pr=None, plan=None, ask=None, head=None, scenario="S1", all_scenarios=False, evaluator="tester", result="pass", evidence="ran it", command=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def verdict(self, caller, retired=None, **kw):
        """Run the real cmd_item_verdict as `caller`; (exit, why) where why is the refusal sentence or None."""
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), retired or {})
        item = kw.get("item_key") or self.ITEM
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx(item=item)), mock.patch.object(sb, "identify_caller", lambda *a: caller), \
                mock.patch.object(sb, "item_agents", lambda c: (launch, self.sessions, self.fleets)):
            try:
                return sb.cmd_item_verdict(self.args(**{k: v for k, v in kw.items() if k != "item_key"}), None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def verdicts(self):
        return [e for e in self.entries() if e.get("kind") == "verdict"]

    # ---- S1 / DW-1: the registration and the verdict
    def test_s1_a_registered_tester_records_a_verdict_for_its_item(self):
        self.register()
        code, why = self.verdict("t1")
        self.assertEqual((code, why), (sb.EXIT_OK, None))
        got = self.verdicts()
        self.assertEqual([(v["pr"], v["by"], v["head"], v["evaluator"]) for v in got], [(self.ITEM, "t1", self.PIN, "tester")])

    def test_s1_launch_registers_the_tester_for_that_item_by_the_switchboard(self):
        self.pages.update({"repos/%s/issues/777" % self.REPO: {"user": {"login": "human"}, "body": "PLAN: https://github.com/acme-org/widget-service/issues/777#issuecomment-1",
                                                               "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"},
                           "repos/%s/issues/770" % self.REPO: {"parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/587"},
                           "repos/%s" % self.REPO: {"default_branch": "main"}, "repos/%s/commits/main" % self.REPO: {"sha": self.PIN}})
        agents = {n: types.SimpleNamespace(fleet=f) for n, f in self.fleets.items()}
        ctx = self.ctx()
        ctx.engine.discover = lambda now: types.SimpleNamespace(agents=agents)
        ctx.engine.now = lambda: self.now()
        ctx.engine.config = {}
        args = types.SimpleNamespace(role="tester", issue=777, name="t1", reports_to="orch")
        sb.register_item_tester(ctx.engine, self.state, args, self.REPO, "orch", {"session": "ses_t1"}, self.now)
        reg = self.iv.registrations(self.entries(), self.ITEM)
        self.assertEqual([(e["tester"], e["session"], e["orchestrator"], e["head"]) for e in reg], [("t1", "ses_t1", "orch", self.PIN)])
        self.assertEqual([a[0] for a in self.audit], ["plan.item.register"])
        # a launch run by a coder (not the orchestrator) registers nothing, and says why
        sb.register_item_tester(ctx.engine, self.state, args, self.REPO, "c1", {"session": "ses_t1"}, self.now)
        self.assertEqual(len(self.iv.registrations(self.entries(), self.ITEM)), 1)
        self.assertEqual(self.audit[-1][0], "plan.item.refused")

    def test_s2_a_tester_registered_for_item_a_cannot_write_for_item_b(self):
        self.register("t1")
        other = "acme-org/widget-service#778"
        code, why = self.verdict("t1", item_key=other)
        self.assertIsNone(code)
        self.assertIn("not registered", why)
        self.assertEqual(self.verdicts(), [])
        self.register("t2", item=other)
        self.assertEqual(self.verdict("t1", item_key=other)[0], None)     # t2 is registered for B; t1 still is not

    def test_s2_mutant_matching_on_role_and_repo_only_lets_it_through(self):
        self.register("t1")
        other = "acme-org/widget-service#778"
        with mock.patch.object(self.iv, "registration_problem", lambda *a: None), mock.patch.object(self.iv, "pinned", lambda e, i: self.PIN):
            code, why = self.verdict("t1", item_key=other)
        self.assertEqual(code, sb.EXIT_OK, "the mutant (no per-item registration) records a verdict for item B")

    # ---- S10: a forged or handmade launch record
    def test_s10_a_handmade_launch_record_with_no_switchboard_entry_registers_nothing(self):
        code, why = self.verdict("t1")          # the launch record is perfect (launched_by the orchestrator) but no ledger entry exists
        self.assertIsNone(code)
        self.assertIn("not registered", why)
        self.assertEqual(self.verdicts(), [])

    def test_s10_mutant_trusting_the_launch_record_alone_accepts_it(self):
        with mock.patch.object(self.iv, "registration_problem", lambda *a: None), mock.patch.object(self.iv, "pinned", lambda e, i: self.PIN):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)

    def test_s10_a_coder_launching_its_own_tester_is_refused_by_the_launch_record_and_writes_no_registration(self):
        self.fleets["t1"]["launched_by"] = "c1"; self.fleets["t1"]["reports_to"] = "c1"
        self.register("t1", orch="c1")      # even a ledger entry naming the coder as the orchestrator cannot make it a launched tester
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("not a tester launched", why)

    def test_s10_a_replayed_ledger_line_breaks_the_chain(self):
        self.register()
        self.verdict("t1")
        path = self.state / sb.plan_ledger.LEDGER_FILE
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines + [lines[-1]]) + "\n")      # the verdict line copied once more
        self.assertIsNotNone(sb.plan_ledger.read_ledger(self.state)[1])

    # ---- S5: the reference commit does not move under the verdict
    def test_s5_a_verdict_names_the_pinned_commit_and_no_other(self):
        self.register()
        code, why = self.verdict("t1", head=self.MOVED)
        self.assertIsNone(code)
        self.assertIn("reference commit", why)
        self.assertEqual(self.verdicts(), [])
        self.assertEqual(self.verdict("t1", head=self.PIN)[0], sb.EXIT_OK)
        self.assertEqual(self.verdicts()[0]["head"], self.PIN)

    def test_s5_mutant_reading_the_tip_at_verdict_time_accepts_a_moved_commit(self):
        self.register()
        with mock.patch.object(self.iv, "pinned", lambda e, i: self.MOVED):
            code, why = self.verdict("t1", head=self.MOVED)
        self.assertEqual(code, sb.EXIT_OK, "the mutant stamps whatever the tip is now")

    def test_s5_a_new_registration_voids_the_verdicts_at_the_old_commit(self):
        self.register()
        self.verdict("t1")
        self.clock[0] += 60
        self.register("t1", head=self.MOVED)
        report = sb.item_gates_report(self.ctx(), set())
        row = next(r for r in report["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertTrue(any("head moved" in v for v in row["void"]))

    # ---- S3/S4: CI verdicts of the repository's own checks
    RUN = {"name": "build", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}

    def ci(self, runs, on_branch=True, checks=True, workflows=None):
        self.register()
        self.pages.update({"repos/%s" % self.REPO: {"default_branch": "main"},
                           "repos/%s/compare/main...%s" % (self.REPO, self.PIN): {"status": "identical" if on_branch else "diverged"},
                           })
        for n in range(1, 6):
            self.pages["repos/%s/commits/%s/check-runs?per_page=100&page=%d" % (self.REPO, self.PIN, n)] = {"check_runs": runs[(n - 1) * 100:n * 100], "total_count": len(runs)}
        ctx = self.ctx()
        if not checks:
            ctx.config = {"plan_gates": {"repos": {self.REPO: {}}}}
        if workflows is not None:
            ctx.config["plan_gates"]["repos"][self.REPO]["ci_workflows"] = workflows
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: ctx):
            try:
                return sb.cmd_item_ci_record(types.SimpleNamespace(rest=[self.ITEM], plan=None, ask=None), None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def test_s3_every_listed_check_green_at_the_reference_commit_records_one_ci_verdict_by_the_recorder(self):
        self.assertEqual(self.ci([self.RUN])[0], sb.EXIT_OK)
        got = self.verdicts()
        self.assertEqual([(v["scenario"], v["by"], v["head"], v["evaluator"]) for v in got], [("S2", sb.plan_ledger.CI_RECORDER, self.PIN, "ci")])
        self.assertEqual([a[0] for a in self.audit if a[0] == "plan.item.ci_record"], ["plan.item.ci_record"])

    def test_s3_an_agent_cannot_type_a_ci_verdict_for_an_item(self):
        self.register()
        code, why = self.verdict("t1", scenario="S2", evaluator="ci")
        self.assertIsNone(code)
        self.assertIn("ci", why)
        self.assertEqual(self.verdicts(), [])

    def test_s4_nothing_configured_a_same_named_check_of_another_app_or_a_commit_off_the_branch_records_nothing(self):
        other = dict(self.RUN, app={"slug": "evil-workflow"})
        for runs, kwargs, reason in (([self.RUN], {"checks": False}, "no item_checks"), ([other], {}, "no run of build by github-actions"), ([self.RUN], {"on_branch": False}, "not on the default branch"),
                                     ([dict(self.RUN, conclusion="failure")], {}, "did not succeed"), ([dict(self.RUN, status="in_progress", conclusion=None)], {}, "not complete")):
            self.setUp()
            code, why = self.ci(runs, **kwargs)
            self.assertIsNone(code, (runs, kwargs))
            self.assertIn(reason, why)
            self.assertEqual(self.verdicts(), [], (runs, kwargs))

    def test_s4_mutant_matching_a_check_by_name_alone_records_the_faked_one(self):
        other = dict(self.RUN, app={"slug": "evil-workflow"})
        real = self.iv.ci_decision
        by_name = lambda runs, checks: real(runs, [dict(c, app=r["app"]["slug"]) for c in checks for r in runs[:1]] if runs else checks)
        with mock.patch.object(self.iv, "ci_decision", by_name):
            self.assertEqual(self.ci([other])[0], sb.EXIT_OK, "the mutant accepts the same-named run of another app")

    def _wf(self, run, concl, status="completed"):
        return dict(self.RUN, conclusion=concl, status=status, details_url="https://github.com/%s/actions/runs/%d/job/1" % (self.REPO, run))

    def _same_workflow(self):
        import sys
        ci = sb.load_module("ci_runs")
        ci._CACHE.clear(); ci._FAILED.clear()
        old = ci.READER
        ci.READER = lambda repo, run: (1, "push")
        self.addCleanup(lambda: (setattr(ci, "READER", old), ci._CACHE.clear(), ci._FAILED.clear()))

    def test_1237_plan_item_ci_record_and_the_gate_agree_on_a_cancelled_first_attempt(self):
        self._same_workflow()
        runs = [self._wf(100, "cancelled"), self._wf(101, "success")]
        self.assertEqual(self.ci(runs)[0], sb.EXIT_OK)
        rollup = [{"workflowName": "ci", "name": "build", "conclusion": "CANCELLED", "status": "COMPLETED", "detailsUrl": "https://github.com/%s/actions/runs/100/job/1" % self.REPO},
                  {"workflowName": "ci", "name": "build", "conclusion": "SUCCESS", "status": "COMPLETED", "detailsUrl": "https://github.com/%s/actions/runs/101/job/1" % self.REPO}]
        self.assertEqual(sb.pr_ci_of(rollup), "green")

    def test_1237_mutant_by_name_any_in_the_plan_item_path_refuses_the_green_head(self):
        self._same_workflow()
        with mock.patch.object(sb.load_module("ci_runs"), "latest_check_runs", lambda runs, repo: runs):
            code, why = self.ci([self._wf(100, "cancelled"), self._wf(101, "success")])
        self.assertIsNone(code)
        self.assertIn("did not succeed", why)

    # ---- #1237 round 3: ONE fixtures table of API shapes, run through BOTH the gate and the item path: the same verdict from both
    def _cr(self, run, concl, job=1, at="10", cid=1, name="build", status="completed"):
        return dict(self.RUN, name=name, conclusion=concl, status=status, id=cid, started_at="2026-10-15T%s:00:00Z" % at, completed_at="2026-10-15T%s:30:00Z" % at,
                    details_url="https://github.com/%s/actions/runs/%d/job/%d" % (self.REPO, run, job))

    def shape_rows(self):
        """(label, check runs in the Checks API shape, {run id: (workflow id, display name)} (a missing run is unreadable), expected workflow ids, green?)."""
        cr = self._cr
        many = [cr(100, "success", name="x%d" % i, cid=i) for i in range(1, 120)] + [cr(100, "success", cid=500), cr(101, "failure", cid=501)]
        return [
            ("green baseline", [cr(100, "success")], {100: (1, "ci")}, [], True),
            ("cancelled-then-green", [cr(100, "cancelled"), cr(101, "success")], {100: (1, "ci"), 101: (1, "ci")}, [], True),
            ("rerun inside one run", [cr(100, "cancelled", job=1, at="10", cid=1), cr(100, "success", job=2, at="11", cid=2)], {100: (1, "ci")}, [], True),
            ("rerun inside one run, the newest red", [cr(100, "success", job=1, at="10", cid=1), cr(100, "failure", job=2, at="11", cid=2)], {100: (1, "ci")}, [], False),
            ("two workflows with one display name, one red", [cr(100, "success"), cr(101, "failure")], {100: (1, "ci"), 101: (2, "ci")}, [], False),
            ("more than one page", many, {100: (1, "ci"), 101: (2, "ci")}, [], False),
            ("a missing expected workflow", [cr(100, "success")], {100: (1, "ci")}, ["2"], False),
            ("an expected workflow present", [cr(100, "success")], {100: (7, "anything")}, ["7"], True),
            ("an author-named workflow test", [cr(100, "success")], {100: (9, "test")}, ["7"], False),
            ("unreadable identity", [cr(100, "success")], {}, [], False),
        ]

    def gate_verdict(self, runs, wf, expected):
        names = {100: "ci", 101: "ci"}
        rollup = [{"workflowName": wf.get(int(r["details_url"].split("/runs/")[1].split("/")[0]), (0, "ci"))[1], "name": r["name"], "conclusion": r["conclusion"].upper(), "status": "COMPLETED",
                   "startedAt": r["started_at"], "completedAt": r["completed_at"], "detailsUrl": r["details_url"]} for r in runs[:99]]
        if len(runs) > 99:   # the gate reads one rollup page: more than it holds is never green (the item path pages and reads the red run)
            rollup = rollup[:99] + [{"workflowName": "ci", "name": "build", "conclusion": "FAILURE", "status": "COMPLETED", "detailsUrl": runs[-1]["details_url"]}]
        facts = sb.pr_facts_from({"state": "OPEN", "headRefOid": "a" * 40, "statusCheckRollup": rollup})
        cfg = {"plan_gates": {"repos": {self.REPO: {"ci_workflows": expected}}}}
        ctx = types.SimpleNamespace(unreadable=None, facts=facts, plan={"result": "pass", "scenarios_parsed": []}, plan_problem=None, engine=None, pr=1, config=cfg, repo=self.REPO)
        return sb.ci_record_core(ctx, lambda: 0, quiet=True)["status"] == "nothing"

    def run_shapes(self):
        ci = sb.load_module("ci_runs")
        got = {}
        for label, runs, wf, expected, want in self.shape_rows():
            self.setUp()
            ci._CACHE.clear(); ci._FAILED.clear(); ci._PATH.clear()
            old = ci.READER
            ci.READER = lambda repo, run, wf=wf: (wf[run][0], "push") if run in wf else None
            try:
                code, why = self.ci(runs, workflows=expected)
                item = code == sb.EXIT_OK
                ci._CACHE.clear(); ci._FAILED.clear()
                gate = self.gate_verdict(runs, wf, expected)
            finally:
                ci.READER = old
                ci._CACHE.clear(); ci._FAILED.clear()
            got[label] = (item, gate, want)
        return got

    def test_1237_every_api_shape_gives_the_same_verdict_from_the_gate_and_the_item_path(self):
        for label, (item, gate, want) in self.run_shapes().items():
            self.assertEqual((item, gate), (want, want), label)

    def test_1237_the_docs_list_exactly_the_shapes_of_the_table(self):
        import re as _re
        text = open(os.path.join(os.path.dirname(os.path.abspath(sb.__file__)), "..", "docs", "switchboard.md")).read()
        block = _re.search(r"<!-- ci-shapes:begin -->\n(.*?)<!-- ci-shapes:end -->", text, _re.S).group(1)
        listed = [line[2:].strip() for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual(listed, [row[0] for row in self.shape_rows()])

    def test_1237_mutant_by_name_only_in_the_item_path_fails_the_rerun_row(self):
        with mock.patch.object(sb.load_module("ci_runs"), "latest_check_runs", lambda runs, repo: [r for r in runs if isinstance(r, dict)]):
            got = self.run_shapes()
        self.assertNotEqual(got["rerun inside one run"][0], True)
        self.assertNotEqual(got["cancelled-then-green"][0], True)

    def test_1237_mutant_matching_the_display_name_lets_the_author_named_workflow_pass(self):
        ci = sb.load_module("ci_runs")
        real = ci.missing_workflows
        with mock.patch.object(ci, "missing_workflows_of_checks", lambda runs, expected, repo: []):
            got = self.run_shapes()
        self.assertTrue(got["a missing expected workflow"][0])   # the item path with the list ignored reads green: the table row catches it
        self.assertTrue(got["an author-named workflow test"][0])

    def test_1237_over_the_page_cap_is_refused_naming_the_cap_and_a_failed_page_too(self):
        runs = [self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(249)]
        code, why = self.ci(runs)
        self.assertEqual(code, sb.EXIT_OK)   # 250 runs are read in 3 pages
        self.setUp()
        code, why = self.ci([self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(599)])
        self.assertIsNone(code)
        self.assertIn("more than 500 check runs", why)
        self.setUp()
        self.register()
        real = self.ci
        code, why = real(runs[:150])
        self.assertEqual(code, sb.EXIT_OK)

    def test_1237_mutant_one_page_only_reads_a_long_list_as_whole(self):
        ci = sb.load_module("ci_runs")
        with mock.patch.object(ci, "CHECK_PAGES", 1):
            code, why = self.ci([self.RUN] + [dict(self.RUN, name="x%d" % i) for i in range(249)])
        self.assertIsNone(code)   # with the cap at one page it refuses at 100: the cap is the named constant, not a silent cut
        self.assertIn("more than 100 check runs", why)

    def test_s4_the_item_checks_key_is_validated_by_its_own_module_and_defaults_to_none(self):
        self.assertEqual(self.iv.validate_item_checks(None, "c"), [])
        self.assertEqual(self.iv.validate_item_checks([{"name": "build", "app": "github-actions"}], "c"), [{"name": "build", "app": "github-actions"}])
        for bad in ("build", [{"name": "build"}], [{"name": "", "app": "x"}], [{"name": "a", "app": "b", "x": 1}], [{"name": "a", "app": "b"}] * 2):
            with self.assertRaises(ValueError):
                self.iv.validate_item_checks(bad, "c")
        raw = {"repos": {self.REPO: {"item_checks": [{"name": "build", "app": "github-actions"}]}}}
        self.assertEqual(sb.plan_ledger.validate_plan_gates(raw, "c")["repos"][self.REPO]["item_checks"], raw["repos"][self.REPO]["item_checks"])
        self.assertNotIn("item_checks", sb.plan_ledger.validate_plan_gates({"repos": {self.REPO: {}}}, "c")["repos"][self.REPO])

    # ---- S6/S7: the item-level claim
    def test_s6_an_item_claims_only_pr_scenarios_never_an_ask_level_live_one(self):
        self.register()
        code, why = self.verdict("t1", all_scenarios=True, scenario=None, head=self.PIN)
        self.assertEqual(code, sb.EXIT_OK)
        self.assertEqual([v["scenario"] for v in self.verdicts()], ["S1"])        # S3 is claim: epic, S4 is tester + measure with no claim (read as epic)
        for sid in ("S3", "S4"):
            code, why = self.verdict("t1", scenario=sid)
            self.assertIsNone(code)
            self.assertIn("epic", why)
        self.assertEqual(len(self.verdicts()), 1)

    def test_s6_mutant_giving_the_item_every_scenario_claims_the_live_one(self):
        self.register()
        with mock.patch.object(sb.plan_ledger, "claim_of", lambda s: "pr"):
            self.verdict("t1", all_scenarios=True, scenario=None, head=self.PIN)
        self.assertIn("S3", [v["scenario"] for v in self.verdicts()])

    def test_s7_an_item_that_links_the_asks_plan_claims_only_what_is_sliced_to_it(self):
        self.register()
        self.scenarios[0]["slice"] = "#778"
        self.scenarios.append({"id": "S5", "evaluator": "tester", "proof": "live", "claim": "pr", "slice": "#777"})
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        results = {}
        for sid in ("S1", "S5"):
            with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx(own=False)), mock.patch.object(sb, "identify_caller", lambda *a: "t1"), \
                    mock.patch.object(sb, "item_agents", lambda c: (launch, self.sessions, self.fleets)):
                try:
                    results[sid] = sb.cmd_item_verdict(self.args(scenario=sid), None, self.now)
                except sb.LedgerCommandError as err:
                    results[sid] = str(err)
        self.assertIn("not sliced", results["S1"])
        self.assertEqual(results["S5"], sb.EXIT_OK)
        self.assertEqual([v["scenario"] for v in self.verdicts()], ["S5"])

    # ---- S8: independence
    def test_s8_the_author_notifier_orchestrator_the_orchestrators_coder_and_a_coders_session_are_refused(self):
        self.register()
        self.fleets["nt"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["nt"] = self.sessions["c1"]            # a tester running in the session of the item's coder
        self.register("nt")
        self.fleets["human"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["human"] = "ses_h"
        self.register("human")                                # a tester that is also the issue's GitHub author
        self.fleets["wk"] = {"role": "coder", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": 700}   # the orchestrator's coder on ANOTHER item (the notifier)
        self.sessions["wk"] = "ses_wk"
        reasons = {}
        for who in ("human", "c1", "wk", "nt"):
            if who in ("c1", "wk"):
                self.register(who)
            code, why = self.verdict(who)
            self.assertIsNone(code, who)
            reasons[who] = why
        self.assertIn("wrote or runs", reasons["human"])        # the issue's author, a launched tester
        self.assertIn("wrote or runs", reasons["nt"])           # a launched tester in the session of the item's coder
        self.assertIn("not a tester launched", reasons["c1"])   # the coders are no launched testers at all
        self.assertIn("not a tester launched", reasons["wk"])
        self.assertEqual(self.verdicts(), [])
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)
        self.assertTrue({"orch", "c1", "wk", "human", "nt"} <= writers)       # the exclusion itself, whatever else refuses first
        self.assertEqual(self.iv.verdict_problem(self.ITEM, "wk", self.entries(), writers, "ses_wk", self.now(), self.PIN)[:2], "wk")

    def test_s8_mutant_excluding_only_the_issue_author_records_the_others(self):
        for who in ("c1", "wk"):
            self.fleets[who] = dict(self.fleets.get(who, {}), role="tester", repo=self.REPO, reports_to="orch", launched_by="orch", issue=self.N)
            self.sessions.setdefault(who, "ses_" + who)
            self.register(who)
        with mock.patch.object(self.iv, "excluded", lambda item, number, ask, author, entries, fleets, sessions: {author}):
            codes = [self.verdict(who)[0] for who in ("c1",)]
        self.assertEqual(codes, [sb.EXIT_OK], "the mutant lets the orchestrator's other worker record")

    def test_s9_a_tester_that_later_proves_for_the_item_loses_its_verdicts_at_read_time(self):
        self.register()
        self.verdict("t1")
        before = sb.item_gates_report(self.ctx(), self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions))
        self.assertEqual(next(r for r in before["scenarios"] if r["id"] == "S1")["verdict"]["by"], "t1")
        self.assertEqual(self.prove("t1")[0], sb.EXIT_OK)
        after = sb.item_gates_report(self.ctx(), self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions))
        row = next(r for r in after["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertTrue(any("author" in m for m in row["missing"]))

    def test_s9_mutant_computing_the_writers_at_write_time_only_still_reads_ok(self):
        self.register()
        self.verdict("t1")
        self.assertEqual(self.prove("t1")[0], sb.EXIT_OK)
        report = sb.item_gates_report(self.ctx(), set())          # the mutant reads with no writers
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")

    def test_s17_a_registered_tester_that_reports_done_keeps_its_verdict_and_a_coders_report_excludes_the_coder(self):
        self.register()
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)   # a report by t1 adds nobody: reports are not read
        self.assertNotIn("t1", writers)
        self.assertIn("c1", writers)
        report = sb.item_gates_report(self.ctx(), writers)
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")
        self.register("c1")
        self.assertIsNone(self.verdict("c1")[0])

    def test_s18_a_registration_expires_after_24_hours_and_ends_with_the_session_or_retirement(self):
        self.register()
        self.clock[0] += 23 * 3600
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        self.clock[0] += 2 * 3600
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("registration expired", why)
        self.register()                                      # a fresh registration for ses_t1
        self.sessions["t1"] = "ses_other"                    # the tester now runs in another session
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("another session", why)
        self.sessions["t1"] = "ses_t1"
        code, why = self.verdict("t1", retired={"t1": {"session": None}})
        self.assertIsNone(code)
        self.assertIn("not a tester launched", why)

    def test_s18_mutant_never_expiring_records_at_25_hours(self):
        self.register()
        self.clock[0] += 25 * 3600
        with mock.patch.object(self.iv, "TTL", 10 ** 9):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)

    def test_a_replayed_verdict_at_an_old_commit_is_refused_after_a_repin(self):
        self.register()
        self.clock[0] += 60
        self.register("t1", head=self.MOVED)
        code, why = self.verdict("t1", head=self.PIN)
        self.assertIsNone(code)
        self.assertIn("reference commit", why)

    # ---- S12: audit
    def test_s12_a_registration_a_verdict_and_a_refusal_are_audited_with_the_item_the_writer_and_the_reference_commit(self):
        self.register()
        self.verdict("t1")
        self.verdict("c1")
        self.verdict("t1", head=self.MOVED)
        events = {e: d for e, s, d in self.audit}
        self.assertEqual(events["plan.item.verdict"]["by"], "t1")
        self.assertEqual((events["plan.item.verdict"]["item"], events["plan.item.verdict"]["reference_commit"]), (self.ITEM, self.PIN))
        refused = [d for e, s, d in self.audit if e == "plan.item.refused"]
        self.assertEqual([r["item"] for r in refused], [self.ITEM] * 2)
        self.assertEqual(sorted(r["by"] for r in refused), ["c1", "t1"])
        self.assertTrue(all(r["reference_commit"] == self.PIN for r in refused))

    # ---- S13 to S15: the draft line
    def g4(self, **facts):
        report = sb.plan_ledger.evaluate_gates(None, [], None, dict({"head": "a" * 40}, **facts), "acme-org/widget-service#9")
        return next(r for r in report["gates"] if r["gate"] == "G4")["missing"]

    def test_s13_a_draft_pr_with_no_ci_says_the_draft_line(self):
        self.assertEqual(self.g4(ci="none", draft=True), ["draft: no CI run seen; this repo's workflow may skip drafts; mark ready, then wait"])

    def test_s14_a_ready_pr_with_a_failing_check_or_an_unknown_draft_flag_keeps_the_failing_line(self):
        line = ["CI is failing (needs green at the head, or a second agent's re-run)"]
        self.assertEqual(self.g4(ci="failing", draft=False), line)
        self.assertEqual(self.g4(ci="failing", draft=True), line)      # a draft that HAS runs and a failing check: the failing line
        self.assertEqual(self.g4(ci="failing"), line)
        self.assertEqual(self.g4(ci="failing", draft=None), line)

    def test_s15_the_draft_flag_comes_from_isdraft_not_from_the_title(self):
        data = {"title": "draft: x", "state": "OPEN", "headRefOid": "a" * 40, "isDraft": False}
        self.assertIs(sb.pr_facts_from(data)["draft"], False)
        self.assertIs(sb.pr_facts_from(dict(data, title="x", isDraft=True))["draft"], True)
        self.assertIn("isDraft", sb.PR_VIEW_FIELDS.split(","))

    # ---- U2 / U3
    def test_u2_a_pull_request_number_is_refused_on_the_item_path_and_nothing_changes_for_pr(self):
        self.pages["repos/%s/issues/9" % self.REPO] = {"pull_request": {"url": "x"}, "user": {"login": "a"}}
        engine = self.ctx().engine
        with self.assertRaises(sb.LedgerCommandError) as raised:
            sb.item_issue(engine, self.REPO, 9)
        self.assertIn("is a pull request", str(raised.exception))
        self.assertEqual(sb.plan_ledger.KINDS[:13], ("proof", "verdict", "consensus", "waiver", "round", "lane_session", "lane", "response", "escalation", "decision", "abandoned", "reminder", "review_record"))

    def test_u3_a_ledger_with_item_entries_reads_with_the_chain_intact_and_read_ledger_never_looks_at_kinds(self):
        self.register()
        self.verdict("t1")
        self.assertEqual(sb.plan_ledger.read_ledger(self.state)[1], None)
        tree = ast.parse(Path(sb.plan_ledger.__file__).read_text())
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_ledger")
        names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"KINDS", "ITEM_KINDS", "EVIDENCE_KINDS"}, "read_ledger must not judge kinds: an older build must read a ledger with item entries")

    # ---- item-prove and the refusals of the registration
    def prove(self, caller, sid="S1", item_key=None):
        args = types.SimpleNamespace(item=item_key or self.ITEM, scenario=[sid], plan=None, ask=None, head=None, evidence="ran it", result="pass", command=None)
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: self.ctx()), mock.patch.object(sb, "identify_caller", lambda *a: caller):
            try:
                return sb.cmd_item_prove(args, None, self.now), None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def test_item_prove_records_at_the_reference_commit_audits_it_and_makes_its_author_an_excluded_writer(self):
        code, why = self.prove("c1")
        self.assertIsNone(code)
        self.assertIn("no reference commit", why)              # nothing is registered yet
        self.register()
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        proofs = [e for e in self.entries() if e.get("kind") == "proof"]
        self.assertEqual([(e["pr"], e["by"], e["head"], e["scenario"]) for e in proofs], [(self.ITEM, "c1", self.PIN, "S1")])
        self.assertIn("plan.item.proof", [a[0] for a in self.audit])
        self.assertEqual(self.prove("t1", sid="S2")[0], sb.EXIT_OK)      # a tester that proves becomes an author: its verdict is then refused
        code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("wrote or runs", why)
        self.assertEqual(self.prove("c1", sid="S3")[0], sb.EXIT_PARTIAL)   # an epic-claimed scenario is not claimed by an item
        self.assertEqual(len([e for e in self.entries() if e.get("kind") == "proof"]), 2)    # c1's S1 and t1's S2; S3 recorded nothing

    def register_world(self, **page_changes):
        self.pages.update({"repos/%s/issues/777" % self.REPO: {"user": {"login": "human"}, "body": "", "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"},
                           "repos/%s/issues/770" % self.REPO: {"parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/587"},
                           "repos/%s" % self.REPO: {"default_branch": "main"}, "repos/%s/commits/main" % self.REPO: {"sha": self.PIN}})
        self.pages.update(page_changes)
        agents = {n: types.SimpleNamespace(fleet=f) for n, f in self.fleets.items()}
        ctx = self.ctx()
        ctx.engine.discover = lambda now: types.SimpleNamespace(agents=agents)
        ctx.engine.now = lambda: self.now()
        ctx.engine.config = {}
        return ctx.engine

    def register_try(self, engine, launched_by="orch", reports_to="orch"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sb.register_item_tester(engine, self.state, types.SimpleNamespace(role="tester", issue=777, name="t1", reports_to=reports_to), self.REPO, launched_by,
                                    {"session": "ses_t1"}, self.now)
        return len(self.iv.registrations(self.entries(), self.ITEM)), [a for a in self.audit if a[0].startswith("plan.item.")]

    def test_every_refusal_of_a_registration_writes_no_entry_and_audits_why(self):
        issue = "repos/%s/issues/777" % self.REPO
        base = {"user": {"login": "human"}, "body": "", "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/770"}
        cases = (("is not open", dict(self.pages, **{issue: dict(base, state="closed")}), {}),
                 ("does not reach", {}, {"orch_charter": 999}),
                 ("could not be read", {"repos/%s/commits/main" % self.REPO: {"sha": "xyz"}}, {}),
                 ("not run by an orchestrator", {}, {"launched_by": "c1"}),
                 ("report to the orchestrator", {}, {"reports_to": "c1"}))
        for reason, changes, extra in cases:
            self.setUp()
            self.fleets["orch"]["charter"] = extra.get("orch_charter", 587)
            engine = self.register_world(**({issue: dict(base, state="closed")} if reason == "is not open" else changes))
            count, audit = self.register_try(engine, launched_by=extra.get("launched_by", "orch"), reports_to=extra.get("reports_to", "orch"))
            self.assertEqual(count, 0, reason)
            self.assertEqual([a[0] for a in audit], ["plan.item.refused"], reason)
            self.assertIn(reason, audit[0][2]["why"])
        self.setUp()                                           # a pull request number: this path does not apply, and says nothing
        engine = self.register_world(**{issue: {"pull_request": {"url": "x"}, "user": {"login": "a"}}})
        self.assertEqual(self.register_try(engine), (0, []))
        self.assertTrue(self.iv.lane_ok([777, 770, 587], 587))
        self.assertFalse(self.iv.lane_ok([777, 770], 587))
        self.assertFalse(self.iv.lane_ok([777], None))

    # ---- the pull request commands never write for an item; gates item reads only what the item path wrote; the read fails closed; the append re-checks
    def pr_path(self, command, caller="t2", picture=(None, "PLAN: acme-org/widget-service#777", [], "no pull request found"), **kw):
        """Run an ORDINARY pull request command (`prove`, `verdict`, `gates ci-record`, `waive`) with --pr naming the item's number."""
        args = types.SimpleNamespace(pr=self.ITEM, plan=None, ask=None, head=self.PIN, scenario="S1", all_scenarios=False, evaluator="tester", result="pass", evidence="x",
                                     command=None, gates="G1", reason="a long enough reason to waive")
        for k, v in kw.items():
            setattr(args, k, v)
        ctx = self.ctx()
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        with mock.patch.object(sb, "build_engine", lambda *a, **k: (ctx.engine, self.state, {"intent_repo": self.REPO})), mock.patch.object(sb, "pr_picture", lambda *a: picture), \
                mock.patch.object(sb, "identify_caller", lambda *a: caller), mock.patch.object(sb, "checked_plan", lambda *a, **k: self.plan), \
                mock.patch.object(sb, "launched_testers_for", lambda c: (launch, self.sessions, self.fleets)), mock.patch.object(sb, "gates_caller_kind", lambda *a: ("agent", "x", "chief-of-staff")):
            try:
                with captured() as (out, err):
                    code = getattr(sb, command)(args, None, self.now)
                if code == sb.EXIT_USAGE:   # #1049: a recording command ends `not recorded: <reason>` (exit 2) instead of raising
                    return None, err.getvalue()
                return code, None
            except sb.LedgerCommandError as err:
                return None, str(err)

    def issue_page(self):
        self.pages["repos/%s/issues/777" % self.REPO] = {"user": {"login": "human"}, "body": "", "state": "open"}

    def test_a_pull_request_command_refuses_an_issue_number_and_writes_nothing(self):
        self.issue_page()
        self.register("t1")
        self.register("t2", item="acme-org/widget-service#778", head=self.MOVED)
        before = len(self.entries())
        for command in ("cmd_prove", "cmd_verdict", "cmd_gates_ci_record", "cmd_waive"):
            for caller in ("t2", "t1"):         # a tester registered for ANOTHER item, and the tester registered for this one
                code, why = self.pr_path(command, caller=caller, scenario=["S1"] if command == "cmd_prove" else "S1", pr=self.ITEM)
                self.assertIsNone(code, (command, caller))
                self.assertTrue("work item" in why or "is an issue" in why, (command, caller, why))
        self.assertEqual(len(self.entries()), before)           # nothing was written by any of them

    def test_a_pull_request_command_refuses_an_issue_number_that_has_no_registration_too(self):
        self.issue_page()
        for command in ("cmd_verdict", "cmd_prove"):
            code, why = self.pr_path(command, scenario=["S1"] if command == "cmd_prove" else "S1")
            self.assertIsNone(code)
            self.assertIn("is an issue, not a pull request", why)
        self.assertEqual(self.entries(), [])

    def test_mutant_without_the_refusal_the_pull_request_command_writes_the_item_verdict_around_every_defence(self):
        self.issue_page()
        sb.plan_ledger.append_entry(self.state, {"kind": "proof", "pr": self.ITEM, "scenario": "S9", "plan_hash": "sha256:p", "head": self.PIN, "result": "pass", "by": "c1"}, self.now())
        with mock.patch.object(sb, "item_entry_refusal", lambda *a: None), mock.patch.object(sb, "ask_subject", lambda *a: None):
            code, why = self.pr_path("cmd_verdict")              # t2 is registered for nothing, and there is no registration for the item
        self.assertEqual(code, sb.EXIT_OK, why)
        forged = [e for e in self.entries() if e.get("kind") == "verdict"]
        self.assertEqual([(e["by"], e["pr"]) for e in forged], [("t2", self.ITEM)])
        # and with the refusal in place the same call is refused (the real guard), while gates item would ignore the entry anyway
        self.assertIsNone(self.pr_path("cmd_verdict")[0])

    def test_a_real_pull_request_is_unaffected_by_the_refusal(self):
        self.issue_page()
        facts = {"head": self.PIN, "ci": "green"}
        ctx = self.ctx()
        ctx.engine.intents = types.SimpleNamespace(_api=lambda *a, **k: (_ for _ in ()).throw(AssertionError("a readable pull request must not be looked up as an issue")))
        with mock.patch.object(sb, "pr_picture", lambda *a: (facts, "", [], None)):
            args = types.SimpleNamespace(pr="acme-org/widget-service#9", plan=None, ask=None)
            got = sb._ledger_context(args, None, self.now, True, parts=(ctx.engine, self.state, {"intent_repo": self.REPO}))
        self.assertEqual((got.pr, got.facts["head"]), ("acme-org/widget-service#9", self.PIN))

    def test_gates_item_ignores_an_entry_it_cannot_tie_to_the_item_path(self):
        self.register("t1")
        entry = {"plan_hash": "sha256:p", "head": self.PIN, "result": "pass", "pr": self.ITEM, "scenario": "S1"}
        sb.plan_ledger.append_entry(self.state, dict(entry, kind="proof", by="c9"), self.now())
        sb.plan_ledger.append_entry(self.state, dict(entry, kind="verdict", by="t2", evaluator="tester"), self.now())       # what the pull request path used to write
        writers = self.iv.excluded(self.ITEM, self.N, None, "human", self.entries(), self.fleets, self.sessions)
        report = sb.item_gates_report(self.ctx(), writers)
        row = next(r for r in report["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "missing")
        self.assertEqual((row["proof"], row["verdict"]), (None, None))
        with mock.patch.object(self.iv, "visible", lambda entries, item, recorder: entries):         # mutant: no tie to the item path
            row = next(r for r in sb.item_gates_report(self.ctx(), writers)["scenarios"] if r["id"] == "S1")
        self.assertEqual(row["status"], "ok")

    def test_a_tester_verdict_counts_only_for_a_tester_registered_when_it_wrote_it(self):
        self.register("t1")
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)
        good = next(e for e in self.entries() if e.get("kind") == "verdict")
        self.assertTrue(self.iv.tied(good, self.entries(), sb.plan_ledger.CI_RECORDER))
        self.clock[0] += 3 * 24 * 3600                                  # read long after the registration expired (a finished, retired tester keeps its verdicts)
        self.assertTrue(self.iv.tied(good, self.entries(), sb.plan_ledger.CI_RECORDER))
        for forged in (dict(good, session="ses_other"), dict(good, by="t2"), dict(good, via="pr"), dict(good, seq=1)):
            self.assertFalse(self.iv.tied(forged, self.entries(), sb.plan_ledger.CI_RECORDER), forged)
        stale = dict(good, ts=good["ts"] + self.iv.TTL + 1)             # written after the registration had expired
        self.assertFalse(self.iv.tied(stale, self.entries(), sb.plan_ledger.CI_RECORDER))

    def unreadable_world(self):
        self.register("t1")
        self.fleets["nt"] = {"role": "tester", "repo": self.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.N}
        self.sessions["nt"] = self.sessions["c1"]                       # a tester in the session of the item's coder
        self.register("nt")
        self.assertEqual(self.prove("c1")[0], sb.EXIT_OK)
        sb.plan_ledger.append_entry(self.state, {"kind": "verdict", "pr": self.ITEM, "scenario": "S1", "plan_ref": "plan", "plan_hash": "sha256:p", "head": self.PIN, "result": "pass",
                                                 "by": "nt", "evaluator": "tester", "evidence": "x", "via": "item", "session": self.sessions["c1"]}, self.now())

    def gates_item(self, agents):
        ctx = self.ctx()
        args = types.SimpleNamespace(rest=[self.ITEM], plan=None, ask=None, json=True, item=None)
        out = io.StringIO()
        with mock.patch.object(sb, "item_context", lambda a, r, n, w: ctx), mock.patch.object(sb, "item_agents", agents), contextlib.redirect_stdout(out):
            code = sb.cmd_item_gates(args, None, self.now)
        return code, json.loads(out.getvalue())

    def test_gates_item_fails_closed_when_the_running_agents_cannot_be_read(self):
        self.unreadable_world()
        launch = sb.load_launch_testers().launched(self.fleets, self.sessions, self.REPO, lambda f: f.get("repo"), {})
        code, report = self.gates_item(lambda c: (launch, self.sessions, self.fleets))
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "missing")      # agents read: the session-mate of a coder is excluded

        def broken(ctx):
            raise sb.ItemAgentsUnreadable("the running agents cannot be read (OSError: down)")
        code, report = self.gates_item(broken)
        self.assertEqual((code, report["ready"], report["scenarios"]), (sb.EXIT_ERROR, False, []))
        self.assertIn("NOT READY", report["rows"][0]["missing"][0])
        code, report = self.gates_item(lambda c: ({}, {}, {}))         # mutant: fail open, the writer set shrinks to the author, the proofs and the registrar
        self.assertEqual(next(r for r in report["scenarios"] if r["id"] == "S1")["status"], "ok")

    def test_item_agents_raises_when_discovery_or_the_retired_list_fails(self):
        ctx = self.ctx()
        ctx.engine.now = lambda: self.now()
        ctx.engine.discover = lambda now: (_ for _ in ()).throw(OSError("herdr down"))
        with self.assertRaises(sb.ItemAgentsUnreadable):
            sb.item_agents(ctx)

    def test_the_append_rechecks_the_ledger_under_the_lock_and_refuses_a_replaced_registration(self):
        self.register()
        real = sb.plan_ledger.append_many_checked

        def racing(state, build, now):      # between the reads and the append another registration (a new pin) lands
            sb.plan_ledger.append_entry(state, {"kind": "item_tester", "pr": self.ITEM, "item": self.ITEM, "tester": "t1", "session": "ses_t1", "orchestrator": "orch", "head": self.MOVED,
                                                "plan_ref": None, "by": "orch"}, self.now())
            return real(state, build, now)
        with mock.patch.object(sb.plan_ledger, "append_many_checked", racing):
            code, why = self.verdict("t1")
        self.assertIsNone(code)
        self.assertIn("reference commit", why)
        self.assertEqual(self.verdicts(), [])
        self.assertEqual([a for e, s, a in self.audit if e == "plan.item.refused"][-1]["stage"], "append")
        self.audit.clear()
        self.setUp()
        self.register()

        def unchecked(state, build, now):   # mutant: the check made on the snapshot from BEFORE the race (what the old append amounted to)
            snapshot = sb.plan_ledger.read_ledger(state)[0]
            sb.plan_ledger.append_entry(state, {"kind": "item_tester", "pr": self.ITEM, "item": self.ITEM, "tester": "t1", "session": "ses_t1", "orchestrator": "orch", "head": self.MOVED,
                                                "plan_ref": None, "by": "orch"}, self.now())
            return sb.plan_ledger.append_many(state, build(snapshot), now)
        with mock.patch.object(sb.plan_ledger, "append_many_checked", unchecked):
            self.assertEqual(self.verdict("t1")[0], sb.EXIT_OK)         # without the re-check the verdict at the old pin is written

    def test_append_many_checked_refuses_with_the_ledger_unchanged(self):
        self.register()
        before = self.entries()

        def refuse(entries):
            raise sb.plan_ledger.LedgerError("no")
        with self.assertRaises(sb.plan_ledger.LedgerError):
            sb.plan_ledger.append_many_checked(self.state, refuse, self.now())
        self.assertEqual(self.entries(), before)
        got = sb.plan_ledger.append_many_checked(self.state, lambda have: [{"kind": "verdict", "pr": self.ITEM, "scenario": "S1", "result": "pass"}] * 2, self.now())
        self.assertEqual([e["seq"] for e in got], [len(before) + 1, len(before) + 2])
        self.assertIsNone(sb.plan_ledger.read_ledger(self.state)[1])

    def test_u1_the_pull_request_rules_are_untouched(self):
        import subprocess as sp
        for name in ("may_record_verdict", "scenario_state", "authors_of"):
            self.assertTrue(callable(getattr(sb.plan_ledger, name)))
        text = switchboard_source()
        for name in ("ci_record_core", "launched_testers_for"):
            self.assertEqual(text.count("def %s(" % name), 1)


class TestBatchingDocs(unittest.TestCase):
    """#834 slice 1: the batching procedure, its body template and its owner decisions, each said once."""
    HEAD = "## Batching: the integration branch and the batch pull request (#834)"

    def section(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertEqual(text.count(self.HEAD), 1)
        return text.split(self.HEAD, 1)[1]  # the last section of the file; its template holds "## " lines, so no split on them

    def test_the_topics_are_each_a_heading_once(self):
        section = self.section()
        for topic in ("### What batches and what goes straight", "### The cadence and who keeps the batch", "### The freeze",
                      "### The sync", "### An ask whose change is only in `integration`", "### The steps a lane follows"):
            self.assertEqual(section.count(topic), 1, topic)

    def test_the_hold_rule_uses_the_matrix_terms_and_marks_the_delegation_a_target(self):
        section = self.section()
        for phrase in ("`owner_hold`", "Target, after #835 lands", "`delegated_clear`", "aggregated diff", "taken out of draft", "a planned follow-up (not built)",
                       "voids the owner's clear", "before the next change merges into `integration`", "delete head branch on merge"):
            self.assertEqual(section.count(phrase), 1, phrase)

    def test_the_batch_hold_sentence_is_in_the_rule_and_in_d1_and_in_the_skill(self):
        sentence = "until #835 lands, then only when a row is rated High"
        self.assertEqual(self.section().count(sentence), 2)  # the hold rule and decision D1
        self.assertEqual((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().count(sentence), 1)

    def test_no_label_and_no_existing_audit_report_are_claimed(self):
        section = self.section()
        self.assertNotIn("batch-ready", section)
        self.assertNotIn("batch-ready", (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text())
        self.assertNotIn("audit report", section)

    def test_the_lane_steps_are_numbered_and_each_carries_a_command(self):
        steps = self.section().split("### The steps a lane follows", 1)[1].split("###", 1)[0].strip().splitlines()
        numbered = [l for l in steps if l[:2] in ("1.", "2.", "3.", "4.", "5.", "6.")]
        self.assertEqual(len(numbered), 6)
        for line in numbered:
            self.assertIn("`", line)

    def test_the_skill_names_the_batching_rules_in_one_paragraph(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        paragraphs = [p for p in text.split("\n\n") if "`integration` branch" in p]
        self.assertEqual(len(paragraphs), 1)
        for word in ("batch pull request", "freeze", "held"):
            self.assertIn(word, paragraphs[0])

    def test_the_template_has_the_v2_sections_a_four_column_header_and_one_example_row(self):
        block = self.section().split("```markdown\n", 1)[1].split("```", 1)[0]
        for heading in ("## Why this needs you", "## What changes for you", "## Example", "## Questions for you"):
            self.assertEqual(block.count(heading), 1, heading)
        self.assertIn("Recommendation:", block)
        rows = [l for l in block.splitlines() if l.startswith("| Change |")]
        self.assertEqual(rows, ["| Change | Impact | Hold reasons and who cleared them | Evidence |"])
        after = block.split(rows[0], 1)[1].split("\n\n", 1)[0].strip().splitlines()
        self.assertEqual(len(after), 2)  # the separator and one example row
        self.assertEqual(len(after[1].strip().strip("|").split("|")), 4)

    def test_each_owner_decision_is_once_with_its_recommendation_or_decision(self):
        decisions = self.section().split("### Owner decisions and follow-up asks", 1)[1]
        for key, word in (("**D1**", "decided"), ("**D2**", "Recommendation"), ("**D3**", "Recommendation")):
            lines = [l for l in decisions.splitlines() if l.startswith("- " + key)]
            self.assertEqual(len(lines), 1, key)
            self.assertIn(word, lines[0])

    def test_the_three_prohibitions_are_said_once_each(self):
        section = self.section()
        for phrase in ("a force push", "a rebase of a shared branch", "an agent changing a ruleset"):
            self.assertEqual(section.count(phrase), 1, phrase)


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
        return dict({"headRefOid": self.HEAD, "baseRefOid": "b" * 40, "changedFiles": self.count, "files": shown}, **({"statusCheckRollup": self.rollup} if getattr(self, "rollup", None) else {}))   # `rollup` is set only by a clear that must show CI to its brief (#1215)

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


class MergeGateBase(Base):
    def setUp(self):
        super().setUp()
        self.gate = sb.load_module("merge_gate")
        self.reading = sb.load_module("pr_reading")
        self.fake = FakeMergeGitHub()
        self.repo, self.n = self.fake.repo, self.fake.number
        self.state = self.home / "mg-state"
        self.state.mkdir()
        self.conf = self.gate.settings({"merge_gate": {"repos": {self.repo: {}}}})

    def facts(self):
        return self.gate.read_facts(self.reading, self.fake, self.repo, self.n, self.conf)

    def run_pass(self, who="coder-a", gate=None, **more):
        gate = gate or self.gate
        return gate.gate_pass(self.reading, self.fake, self.conf, self.state, self.repo, self.n, who, made_up_pid(), self.fake.clock, self.fake.sleep,
                              alive=lambda pid: True, **more)

    def check(self, caller="coder-a", person=False, waived=False, gate=None):
        gate = gate or self.gate
        return gate.check_merge(self.reading, self.fake, self.conf, self.state, self.repo, self.n, caller, person, lambda head: waived, self.fake.clock,
                                alive=lambda pid: True)

    def moved(self):
        self.fake.tip = MG_NEWTIP


class TestMergeGateDecision(MergeGateBase):
    """The read-only block and the decision (#962 clauses 2, 3, 6, 7)."""

    def tree(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_a_branch_that_has_not_moved_is_allowed_and_the_block_says_six_facts_and_writes_nothing(self):
        before = self.tree()
        facts = self.facts()
        allowed, reason, need = self.gate.decide(facts)
        text = self.gate.block(facts, allowed, reason)
        self.assertEqual((allowed, need), (True, None))
        for label in ("branch tip now:", "CI computed on tip:", "equal:", "CI result at head", "latest run on branch:", "merge allowed now:"):
            self.assertEqual(sum(1 for line in text.splitlines() if label in line), 1, label)
        self.assertIn("equal:                    yes", text)
        self.assertEqual((self.fake.writes, self.tree()), ([], before), "reading writes no file, label, comment or branch")

    def test_a_branch_that_moved_is_not_allowed_and_names_the_rerun_as_the_way_forward(self):
        self.moved()
        facts = self.facts()
        allowed, reason, need = self.gate.decide(facts)
        self.assertEqual((allowed, need), (False, "rerun"))
        self.assertIn("moved after the last green run", reason)
        self.assertIn("equal:                    no", self.gate.block(facts, allowed, reason))
        self.assertEqual(self.fake.writes, [], "the read-only command never re-runs anything")

    def test_unreadable_unknown_and_pending_runs_each_block_with_a_reason(self):
        cases = []
        for broken in ("actions/runs?head_sha", "git/ref/heads"):
            fake = FakeMergeGitHub()
            fake.broken = {broken}
            cases.append(fake)
        pending = FakeMergeGitHub()
        pending.runs[0].update(status="in_progress", conclusion=None)
        cases.append(pending)
        unknown = FakeMergeGitHub()
        unknown.runs[0].update(conclusion="startup_failure")
        cases.append(unknown)
        for fake in cases:
            with self.subTest(broken=sorted(fake.broken), runs=[r["conclusion"] for r in fake.runs]):
                facts = self.gate.read_facts(self.reading, fake, self.repo, self.n, self.conf)
                allowed, reason, _ = self.gate.decide(facts)
                self.assertFalse(allowed)
                self.assertTrue(reason)

    def test_a_red_pull_request_run_names_the_failing_job(self):
        self.fake.runs[0].update(id=2, conclusion="failure")
        allowed, reason, _ = self.gate.decide(self.facts())
        self.assertFalse(allowed)
        self.assertIn("ci / unit-tests", reason)
        self.assertNotIn("lint", reason)

    def test_a_red_latest_run_on_the_branch_blocks_every_merge_into_it(self):
        self.fake.branch_runs[0]["conclusion"] = "failure"
        allowed, reason, _ = self.gate.decide(self.facts())
        self.assertFalse(allowed)
        self.assertIn("latest completed run on main is red", reason)

    def test_a_cancelled_branch_run_is_a_superseded_run_not_a_red_branch(self):
        self.fake.branch_runs[0]["conclusion"] = "cancelled"
        self.assertTrue(self.gate.decide(self.facts())[0])

    def test_gated_branches_default_to_the_default_branch_and_the_integration_branch_only_when_listed(self):
        gate = self.gate
        none = gate.settings({"merge_gate": {"repos": {self.repo: {}}}})
        listed = gate.settings({"merge_gate": {"repos": {self.repo: {"branches": ["main", "integration"]}}}})
        other = gate.settings({"merge_gate": {"repos": {self.repo: {"branches": ["release"]}}}})
        self.assertEqual([gate.is_gated(c, self.repo, b, "main") for c in (none, listed, other) for b in ("main", "integration")],
                         [True, False, True, True, False, False])
        self.assertFalse(gate.is_gated(gate.settings({}), self.repo, "main", "main"), "a repository not listed is not enforced")

    def test_the_integration_branch_is_allowed_without_a_gate_pass_when_the_key_does_not_list_it(self):
        self.fake.base = "integration"
        self.moved()
        self.assertEqual(self.check(), (True, "branch integration is not gated", MG_HEAD), "the head is returned: the shim binds the merge to it")

    def test_the_configuration_key_is_validated(self):
        gate = self.gate
        gate.validate({"repos": {"acme-org/widget-service": {"branches": ["main"]}}, "wait_minutes": 45})
        for bad in ({"repos": []}, {"repos": {"nope": {}}}, {"repos": {self.repo: {"branches": []}}}, {"wait_minutes": 0}, {"other": 1}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gate.validate(bad)
        self.assertEqual(gate.settings({"merge_gate": {}})["wait"], 45 * 60, "the wait defaults to 45 minutes")


class TestMergeGateRerun(MergeGateBase):
    """The re-run on the current merge ref (clause 3, PC-2): head and labels untouched, wait, green allows, red or timeout blocks."""

    def baseline(self):
        return (self.fake.head, list(self.fake.labels))

    def test_a_moved_branch_is_rerun_without_a_change_of_head_or_labels_and_allowed_on_green(self):
        self.moved()
        before = self.baseline()
        ok, lines = self.run_pass()
        self.assertTrue(ok, lines)
        self.assertEqual(self.baseline(), before, "the head and the labels are identical to the baseline")
        self.assertEqual([w[-1] for w in self.fake.writes], ["state=closed", "state=open"], "the only writes: close and reopen the pull request")
        self.assertEqual(self.gate.read_pass(self.state, self.repo, self.n)["tip"], MG_NEWTIP)
        self.assertTrue(any("gate passed" in l for l in lines))

    def test_a_red_rerun_blocks_and_names_the_failing_job(self):
        self.moved()
        self.fake.rerun_result = "failure"
        ok, lines = self.run_pass()
        self.assertFalse(ok)
        self.assertIn("unit-tests", lines[-1])
        self.assertIsNone(self.gate.read_pass(self.state, self.repo, self.n))
        self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"))

    def test_a_rerun_that_does_not_finish_in_time_blocks_and_says_the_limit(self):
        self.moved()
        self.fake.rerun_result = None
        ok, lines = self.run_pass(wait_seconds=300)
        self.assertFalse(ok)
        self.assertIn("within 5 minutes", lines[-1])
        self.assertEqual(self.fake.writes[-1][-1], "state=open", "the pull request is open again")

    def test_a_held_pull_request_is_not_closed_by_the_gate_unless_told_to(self):
        self.moved()
        self.fake.labels = ["fleet:merge-captain"]
        ok, lines = self.run_pass()
        self.assertFalse(ok)
        self.assertEqual(self.fake.writes, [])
        self.assertIn("--rerun-held", lines[-1])
        ok, lines = self.run_pass(allow_held=True)
        self.assertTrue(ok, lines)
        self.assertEqual(self.fake.labels, ["fleet:merge-captain"])

    def test_a_reopen_that_fails_says_how_to_reopen(self):
        self.moved()
        self.fake.reopen_fails = True
        ok, lines = self.run_pass()
        self.assertFalse(ok)
        self.assertIn("gh pr reopen 12", lines[-1])

    def test_a_branch_that_moves_while_the_lock_is_taken_is_not_passed(self):
        real = self.gate.acquire

        def acquire_then_move(*a, **k):
            got = real(*a, **k)
            self.fake.tip = MG_NEWTIP   # the branch moves after the facts were read and before the tip is read again
            return got
        with mock.patch.object(self.gate, "acquire", acquire_then_move):
            ok, lines = self.run_pass()
        self.assertFalse(ok)
        self.assertIn("moved while the gate ran", lines[-1])
        self.assertIsNone(self.gate.read_pass(self.state, self.repo, self.n))
        self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"), "the lock is let go")

    def test_an_unmoved_branch_is_not_rerun(self):
        ok, lines = self.run_pass()
        self.assertTrue(ok, lines)
        self.assertEqual(self.fake.writes, [])


class TestMergeGateLock(MergeGateBase):
    """One merge into a branch at a time (clause 4)."""

    def take(self, who, now, pid=None, lease=1200, alive=lambda p: True):
        return self.gate.acquire(self.state, self.repo, "main", who, pid if pid is not None else made_up_pid(), "%s#%d" % (self.repo, self.n), now, lease, alive)

    def test_a_second_merger_is_told_who_holds_the_lock_and_when_to_retry(self):
        self.assertTrue(self.take("coder-a", 1000)[0])
        ok, lock, said = self.take("coder-b", 1100)
        self.assertFalse(ok)
        self.assertEqual(lock["holder"], "coder-a")
        self.assertIn("coder-a holds the merge lock on %s main since" % self.repo, said)
        self.assertIn("retry in about", said)

    def test_a_lock_whose_lease_ran_out_is_recovered(self):
        self.take("coder-a", 1000)
        ok, lock, said = self.take("coder-b", 1000 + 1201)
        self.assertTrue(ok)
        self.assertIn("recovered a stale lock of coder-a", said)
        self.assertEqual(self.gate.read_lock(self.state, self.repo, "main")["holder"], "coder-b")

    def test_a_lock_whose_holder_process_is_gone_is_recovered_after_the_grace(self):
        self.take("coder-a", 1000, pid=made_up_pid())
        self.assertFalse(self.take("coder-b", 1000 + 60, alive=lambda p: False)[0], "inside the grace the merge of the holder may still follow")
        ok, _, said = self.take("coder-b", 1000 + self.gate.HOLDER_GRACE + 5, alive=lambda p: False)
        self.assertTrue(ok)
        self.assertIn("its holder is gone", said)

    def test_a_lock_whose_holder_process_is_alive_is_not_recovered_past_the_grace(self):
        self.take("coder-a", 1000, pid=made_up_pid())
        ok, lock, said = self.take("coder-b", 1000 + self.gate.HOLDER_GRACE + 300, alive=lambda p: True)
        self.assertFalse(ok)
        self.assertEqual(lock["holder"], "coder-a")

    def test_only_the_holder_releases_and_the_same_holder_may_take_it_again(self):
        self.take("coder-a", 1000)
        self.assertFalse(self.gate.release(self.state, self.repo, "main", "coder-b"))
        self.assertTrue(self.take("coder-a", 1010)[0])
        self.assertTrue(self.gate.release(self.state, self.repo, "main", "coder-a"))
        self.assertIsNone(self.gate.read_lock(self.state, self.repo, "main"))

    def test_the_gate_pass_of_a_second_merger_is_blocked_by_the_lock_of_the_first(self):
        self.assertTrue(self.run_pass("coder-a")[0])
        ok, lines = self.run_pass("coder-b")
        self.assertFalse(ok)
        self.assertIn("coder-a holds the merge lock", lines[-1])


class TestUndeliveredWake(Base):
    """#1337: an idle agent with a wake admitted with delivery queue that is absent from its transcript is resumed once, then the Chief of Staff is told once."""
    KEY = "lab.fact:wedged"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=(), config={})
        for name, fleet in (("cos", {"role": "chief-of-staff"}), ("coder", {"role": "coder", "reports_to": "cos"})):
            self.world.add_agent(name, fleet=fleet)
        self.pane = self.world.herdr.panes[self.world.panes["coder"]]
        self.sid = self.world.sessions["coder"]
        self.n = 0
        patch = mock.patch.object(self.world.v2, "_start")   # the wedged session: an item queued for it starts no turn, as in the recorded case
        patch.start()
        self.addCleanup(patch.stop)

    def wedge(self, key=None, age=0):
        """A queued wake in the inbox that no turn took (what the recorded case showed), created `age` seconds ago."""
        self.n += 1
        now = self.world.clock.now()
        self.world.v2.sessions[self.sid]["inbox"].append({
            "id": "inb_%d" % self.n, "sessionID": self.sid, "text": "wake", "metadata": {"fleet": {"keys": [key or self.KEY], "from": "switchboard"}},
            "resume": True, "delivery": "queue", "time": {"created": self.world.v2.clock.ms(now - age)}})

    def passes(self, minutes, count=1):
        self.world.tick(minutes * 60)
        for _ in range(count):
            self.world.engine.run_pass()

    def resumes(self):
        return [a for a in self.world.admissions("coder") if a["text"] == sb.RESUME_TEXT]

    def told(self):
        return [a for a in self.world.admissions("cos") if "Undelivered wake" in a["text"]]

    def lines(self, event="undelivered.resume"):
        return [e for e in self.world.audit() if e["event"] == event]

    def test_an_idle_agent_with_an_old_admitted_wake_is_resumed_once_with_one_audit_line(self):
        self.wedge(age=16 * 60)
        self.world.engine.run_pass()
        self.assertEqual(len(self.resumes()), 1)
        (line,) = self.lines()
        self.assertEqual((line["subject"], line["detail"]["keys"]), ("coder", [self.KEY]))
        self.assertGreaterEqual(line["detail"]["oldest_age_seconds"], 16 * 60)

    def test_a_second_pass_in_the_incident_does_not_resume_again(self):
        self.wedge(age=16 * 60)
        self.passes(0, 3)
        self.world.tick(11 * 60)
        self.world.engine.run_pass()
        self.assertEqual(len(self.resumes()), 1)

    def test_the_default_is_fifteen_minutes_and_the_key_sets_another(self):
        self.assertEqual(sb.validate_config({})["undelivered_wake_minutes"], 15)
        self.wedge(age=14 * 60)
        self.world.engine.run_pass()
        self.assertEqual(len(self.resumes()), 0)
        self.passes(2)
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(sb.validate_config({"undelivered_wake_minutes": 3})["undelivered_wake_minutes"], 3)

    def test_the_resume_refusals_hold(self):
        for case in ("permission", "question", "working", "recent"):
            with self.subTest(case=case):
                self.setUp()
                self.wedge(age=16 * 60)
                if case == "permission":
                    self.world.v2.ask_permission(self.sid)
                elif case == "question":
                    self.world.v2.ask_form(self.sid)
                elif case == "working":
                    self.pane["status"] = "working"
                else:
                    sb.blocked_update(self.world.engine.state_dir, sb.RESUME_STATE, lambda s: {"coder": {"at": self.world.clock.now() - 60}})
                self.world.engine.run_pass()
                self.assertEqual(self.resumes(), [])

    def test_a_blocked_agent_a_delivered_wake_and_a_young_wake_are_not_resumed(self):
        self.pane["status"] = "blocked"
        self.wedge(age=16 * 60)
        self.world.engine.run_pass()
        self.assertEqual(self.resumes(), [])
        self.setUp()
        self.wedge(age=60)   # admitted only a minute ago: not yet that old
        self.world.engine.run_pass()
        self.assertEqual(self.resumes(), [])
        self.setUp()
        self.wedge(age=16 * 60)
        self.world.v2.sessions[self.sid]["messages"].append({"id": "msg_k", "type": "synthetic", "text": "t", "time": {"created": self.world.v2.clock.ms(self.world.clock.now())},
                                                             "metadata": {"fleet": {"keys": [self.KEY], "from": "switchboard"}}})
        self.world.engine.run_pass()
        self.assertEqual(self.resumes(), [])

    def test_the_chief_of_staff_is_told_once_when_the_resume_did_not_deliver(self):
        self.wedge(age=16 * 60)
        self.world.engine.run_pass()
        self.passes(15, 3)
        self.world.engine.run_pass()
        self.assertEqual(len(self.told()), 1)
        self.assertIn("coder", self.told()[0]["text"])
        self.assertIn("1 wake", self.told()[0]["text"])

    def test_the_chief_of_staff_is_not_told_when_the_keys_appear(self):
        self.wedge(age=16 * 60)
        self.world.engine.run_pass()
        self.world.v2.sessions[self.sid]["inbox"][:] = []
        self.world.v2.sessions[self.sid]["messages"].append({"id": "msg_k", "type": "synthetic", "text": "t", "time": {"created": self.world.v2.clock.ms(self.world.clock.now())},
                                                             "metadata": {"fleet": {"keys": [self.KEY], "from": "switchboard"}}})
        self.passes(31, 3)
        self.assertEqual(self.told(), [])

    def test_a_manual_resume_blocks_the_rule(self):
        self.wedge(age=16 * 60)
        sb.resume_ended_turn(self.world.engine, "cos", "coder", None)
        self.world.engine.run_pass()
        self.assertEqual(len(self.resumes()), 1)

    def wedge_for(self, name, age):
        sid = self.world.sessions[name]
        self.n += 1
        self.world.v2.sessions[sid]["inbox"].append({
            "id": "inb_%s_%d" % (name, self.n), "sessionID": sid, "text": "wake", "metadata": {"fleet": {"keys": ["lab.fact:" + name], "from": "switchboard"}},
            "resume": True, "delivery": "queue", "time": {"created": self.world.v2.clock.ms(self.world.clock.now() - age)}})

    def test_a_wake_first_noticed_long_after_admission_is_not_told_before_the_resume_could_deliver(self):
        self.wedge(age=19 * 3600)
        self.world.engine.run_pass()
        self.assertEqual(len(self.resumes()), 1)
        self.passes(0.2, 3)   # seconds later: the oldest wake is 19 hours old, the resume only seconds
        self.assertEqual((self.told(), self.lines("undelivered.told")), ([], []))
        self.passes(16)
        self.passes(1, 3)
        self.assertEqual((len(self.told()), len(self.lines("undelivered.told"))), (1, 1))
        self.assertEqual(len(self.resumes()), 1)

    def test_the_tell_time_is_its_own_config_key_defaulting_to_the_limit(self):
        self.assertIsNone(sb.validate_config({})["undelivered_wake_tell_minutes"])
        self.assertEqual(sb.validate_config({"undelivered_wake_tell_minutes": 5})["undelivered_wake_tell_minutes"], 5)
        for bad in (0, -1, "x"):
            with self.assertRaises(sb.ConfigError):
                sb.validate_config({"undelivered_wake_tell_minutes": bad})
        self.world.engine.config["undelivered_wake_tell_minutes"] = 2
        self.wedge(age=16 * 60)
        self.world.engine.run_pass()
        self.passes(3, 3)
        self.assertEqual(len(self.told()), 1)

    def test_at_most_the_per_pass_cap_of_agents_is_resumed_in_a_pass_oldest_first(self):
        self.assertEqual(sb.validate_config({})["undelivered_wake_per_pass"], 2)
        for bad in (0, 21, -1, 1.5, "2", True):
            with self.assertRaises(sb.ConfigError):
                sb.validate_config({"undelivered_wake_per_pass": bad})
        names = ["w%d" % i for i in range(6)]
        for i, name in enumerate(names):
            self.world.add_agent(name, fleet={"role": "coder", "reports_to": "cos"})
            self.wedge_for(name, (20 + i) * 60)   # w5 the oldest
        order = []
        for _ in range(3):
            self.world.engine.run_pass()
            order.append(sorted(n for n in names if [a for a in self.world.admissions(n) if a["text"] == sb.RESUME_TEXT]))
        self.assertEqual([len(o) for o in order], [2, 4, 6])
        self.assertEqual(order[0], ["w4", "w5"])
        self.assertEqual(order[1], ["w2", "w3", "w4", "w5"])

    def test_a_delivered_key_beyond_the_first_page_is_not_counted_undelivered(self):
        self.wedge(age=16 * 60)
        now = self.world.clock.now()
        messages = self.world.v2.sessions[self.sid]["messages"]
        messages.append({"id": "msg_k", "type": "synthetic", "text": "t", "time": {"created": self.world.v2.clock.ms(now - 10 * 60)},
                         "metadata": {"fleet": {"keys": [self.KEY], "from": "switchboard"}}})
        for i in range(3):
            messages.append({"id": "msg_n%d" % i, "type": "synthetic", "text": "t", "time": {"created": self.world.v2.clock.ms(now - 60 + i)}, "metadata": {"fleet": {"keys": ["other%d" % i], "from": "switchboard"}}})
        with mock.patch.object(sb, "SYNTHETIC_PAGE", 2):   # a window shorter than the delivered key's position
            self.world.engine.run_pass()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.told(), [])

    def test_when_the_agent_is_the_chief_of_staff_the_incident_is_closed_with_an_audit_line(self):
        self.wedge_for("cos", 16 * 60)
        self.world.engine.run_pass()
        self.passes(16)
        self.passes(1, 2)
        lines = self.lines("undelivered.untold")
        self.assertEqual(len(lines), 1)
        self.assertIn("Chief of Staff", lines[0]["detail"]["reason"])
        self.assertEqual(len([a for a in self.world.admissions("cos") if a["text"] == sb.RESUME_TEXT]), 1)

    def test_each_idle_agent_costs_three_reads_a_pass_and_no_github_call(self):
        self.wedge(age=16 * 60)
        gh, window, mine, real_tick, real_popen = [], [], [], sb.undelivered_wake_tick, sb.subprocess.Popen

        def spy(argv, *a, **k):
            if isinstance(argv, (list, tuple)) and argv:
                if os.path.basename(str(argv[0])) == "gh":
                    gh.append(list(argv))
                window.append(list(argv))
            return real_popen(argv, *a, **k)

        def tick(engine, discovery):
            start = len(self.world.v2.calls)
            try:
                return real_tick(engine, discovery)
            finally:
                mine.extend(self.world.v2.calls[start:])   # only what the rule itself read
        with mock.patch.object(sb.subprocess, "Popen", spy), mock.patch.object(sb, "undelivered_wake_tick", tick):
            self.world.engine.run_pass()
        coder = [c[2] for c in mine if "sessionID=" + self.sid in c and c[2] in ("session.inbox.list", "session.message.list") and "type=user" not in c]   # the resume's own re-observation is not the rule's read
        self.assertEqual(coder, ["session.inbox.list", "session.inbox.list", "session.message.list"], mine)
        other = [c[2] for c in mine if "sessionID=" + self.world.sessions["cos"] in c]
        self.assertEqual(other, ["session.inbox.list"], "an idle agent with nothing queued costs one read")
        self.assertEqual(gh, [])

    # -- the delivery states, a row per state with the action and the test; `test_every_state_has_a_row` derives the rows from the code
    STATES = {"undelivered": "test_a_blocked_agent_a_delivered_wake_and_a_young_wake_are_not_resumed",
              "admitted-in-transcript": "test_a_blocked_agent_a_delivered_wake_and_a_young_wake_are_not_resumed",
              "admitted-absent": "test_an_idle_agent_with_an_old_admitted_wake_is_resumed_once_with_one_audit_line",
              "refused": "test_the_resume_refusals_hold",
              "resumed": "test_a_second_pass_in_the_incident_does_not_resume_again",
              "told": "test_the_chief_of_staff_is_told_once_when_the_resume_did_not_deliver"}

    def test_every_state_has_a_row(self):
        self.assertEqual(sorted(self.STATES), sorted(sb.UNDELIVERED_STATES))
        for test in self.STATES.values():
            self.assertTrue(callable(getattr(self, test, None)), test)

    # -- the family table: every input and external call of the new code, with its bound, test and mutant; derived from the code, never a hand copy
    # -- the family table of docs/switchboard.md ("An undelivered wake is recovered"): every config key, constant and refusal of the pass, derived by ast
    TABLE = {"undelivered_wake_minutes": ("test_the_default_is_fifteen_minutes_and_the_key_sets_another", "resumes on every pass"),
             "undelivered_wake_tell_minutes": ("test_the_tell_time_is_its_own_config_key_defaulting_to_the_limit", "tells from the admission"),
             "undelivered_wake_per_pass": ("test_at_most_the_per_pass_cap_of_agents_is_resumed_in_a_pass_oldest_first", "no per-pass cap"),
             "UNDELIVERED_STATE": ("test_a_second_pass_in_the_incident_does_not_resume_again", "resumes on every pass"),
             "UNDELIVERED_KEYS": ("test_an_idle_agent_with_an_old_admitted_wake_is_resumed_once_with_one_audit_line", "never resumes"),
             "UNDELIVERED_STATES": ("test_every_state_has_a_row", "never resumes"),
             "SYNTHETIC_PAGE": ("test_a_delivered_key_beyond_the_first_page_is_not_counted_undelivered", "reads one page"),
             "MAX_READ_PAGES": ("test_a_delivered_key_beyond_the_first_page_is_not_counted_undelivered", "reads one page"),
             "resume_ended_turn": ("test_the_resume_refusals_hold", "skips the refusals"),
             "session.inbox.list": ("test_each_idle_agent_costs_three_reads_a_pass_and_no_github_call", "resumes a blocked agent"),
             "session.message.list": ("test_each_idle_agent_costs_three_reads_a_pass_and_no_github_call", "reads one page")}

    def test_the_docs_table_lists_every_key_constant_and_refusal_derived_by_ast(self):
        text = SWITCHBOARD.read_text()
        tree = ast.parse(text)
        derived = {"SYNTHETIC_PAGE", "MAX_READ_PAGES", "resume_ended_turn", "session.inbox.list", "session.message.list"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                derived.update(t.id for t in node.targets if isinstance(t, ast.Name) and t.id.startswith("UNDELIVERED_"))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("undelivered_wake_") and node.value.isidentifier():
                derived.add(node.value)
        self.assertEqual(derived, set(self.TABLE))
        docs = (SWITCHBOARD.parent.parent / "docs" / "switchboard.md").read_text()
        section = docs[docs.index("### An undelivered wake is recovered"):docs.index("### A blocked agent is reported")]
        rows = {m.group(1): m.groups() for m in re.finditer(r"^\| `([^`]+)` \| (.*?) \| (.*?) \| `(test_\w+)` \| `([^`]+)` \|$", section, re.M)}
        self.assertEqual(set(rows), set(self.TABLE))
        for key, (test, mutant) in self.TABLE.items():
            self.assertEqual((rows[key][3], rows[key][4]), (test, mutant), key)
            self.assertTrue(callable(getattr(self, test, None)), test)
            self.assertIn(mutant, self.MUTANTS, key)

    MUTANTS = {
        "never resumes": ('                resume_ended_turn(engine, "switchboard", name, "undelivered wake:', '                (lambda *a: None)(engine, "switchboard", name, "undelivered wake:', "test_an_idle_agent_with_an_old_admitted_wake_is_resumed_once_with_one_audit_line"),
        "resumes a blocked agent": ('if seen.status != "idle" or hold_reason(seen) or seen.conflict', 'if seen.status not in ("idle", "blocked") or seen.conflict', "test_a_blocked_agent_a_delivered_wake_and_a_young_wake_are_not_resumed"),
        "tells from the admission": ('now - entry["resumed"] >= tell_after', 'age >= tell_after', "test_a_wake_first_noticed_long_after_admission_is_not_told_before_the_resume_could_deliver"),
        "no per-pass cap": ('if resumed_now >= cap:', 'if False:', "test_at_most_the_per_pass_cap_of_agents_is_resumed_in_a_pass_oldest_first"),
        "reads one page": ('SYNTHETIC_PAGE, "synthetic", MAX_READ_PAGES):\n        reached = False', 'SYNTHETIC_PAGE, "synthetic", 1):\n        reached = False', "test_a_delivered_key_beyond_the_first_page_is_not_counted_undelivered"),
        "resumes on every pass": ('if entry.get("resumed") is None and age >= limit:', 'if age >= limit:', "test_a_second_pass_in_the_incident_does_not_resume_again"),
        "skips the refusals": ('                resume_ended_turn(engine, "switchboard", name, "undelivered wake:', '                engine.v2.synthetic(seen.session_id, sb_text(), {"fleet": {"keys": ["r"], "from": "switchboard"}}, True, "queue", derive_message_id(seen.session_id, ["r%d" % now]), "x"); (lambda *a: None)(engine, "switchboard", name, "undelivered wake:', "test_the_resume_refusals_hold"),
    }

    def test_every_mutant_is_killed_by_its_named_test(self):
        text = SWITCHBOARD.read_text()
        for name, (old, new, test) in sorted(self.MUTANTS.items()):
            with self.subTest(mutant=name):
                self.assertEqual(text.count(old), 1, old)
                module = types.ModuleType("t_fleet_switchboard_undelivered_mutant")
                module.__file__ = str(SWITCHBOARD)
                sys.modules[module.__name__] = module
                self.addCleanup(sys.modules.pop, module.__name__, None)
                mutant = text.replace(old, new).replace("sb_text()", "RESUME_TEXT")
                exec(compile(mutant, str(SWITCHBOARD), "exec"), module.__dict__)
                with mock.patch.dict(globals(), {"sb": module}), rebound("sb", module):
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([TestUndeliveredWake(test)]))
                self.assertTrue(result.failures or result.errors, "the mutant %r survives %s" % (name, test))


class TestCloudWatchBounds(unittest.TestCase):
    """#1073 review: every entry kind and every bound of the cloud watch read has a test and a mutant (table in docs/switchboard.md, Cloud watch)."""
    SRC = (REPO / "bin" / "cloud_worker.py")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.dir = self.tmp / "cloud"
        self.dir.mkdir()
        self.cloud = sb.load_module("cloud_worker").bind(sb.__dict__)

    def module(self, old=None, new=None):
        text = self.SRC.read_text()
        if old is not None:
            self.assertIn(old, text)
            text = text.replace(old, new)
        mod = types.ModuleType("cloud_mutant")
        mod.__file__ = str(self.SRC)
        sys.modules["cloud_mutant"] = mod
        self.addCleanup(sys.modules.pop, "cloud_mutant", None)
        exec(compile(text, str(self.SRC), "exec"), mod.__dict__)
        return mod.bind(sb.__dict__)

    def guarded(self, fn, seconds=2):
        import signal
        def hung(*a):
            raise AssertionError("the read hung")
        old = signal.signal(signal.SIGALRM, hung)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def read(self, mod=None, **kw):
        mod = mod or self.cloud
        return self.guarded(lambda: mod.read_records_bounded(self.tmp, **kw))

    def put(self, name, data=None):
        (self.dir / name).write_bytes(json.dumps({"w": 1}).encode() if data is None else data)

    # each kind: (check on the real module, mutant text change that must make the check fail)
    def kind_cases(self):
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        def fifo():
            os.mkfifo(str(self.dir / "x.json"))
        def sym():
            os.symlink(str(outside), str(self.dir / "x.json"))
        def adir():
            (self.dir / "x.json").mkdir()
        return {
            "bad_name": (lambda: self.put("bad name!.json"), None, None),
            "directory": (adir, None, None),
            "not_regular": (fifo, "if not stat.S_ISREG(os.fstat(fd).st_mode):", "if False:"),
            "fifo": (fifo, "os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW", "os.O_RDONLY | os.O_NOFOLLOW"),
            "symlink": (sym, "os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW", "os.O_RDONLY | os.O_NONBLOCK"),
            "too_large": (lambda: self.put("x.json", b"{\"a\": \"" + b"a" * 70000 + b"\"}"), "if len(raw) > max_bytes:", "if False:"),
            "not_utf8": (lambda: self.put("x.json", b"\xff\xfe{}"), "except UnicodeDecodeError:", "except ZeroDivisionError:"),
            "not_json": (lambda: self.put("x.json", b"{nope"), "except ValueError:\n        return None, \"not_json\"", "except ValueError:\n        return None, \"too_deep\""),
            "wrong_shape": (lambda: self.put("x.json", b"[1]"), "if not isinstance(record, dict):", "if False:"),
            "too_deep": (lambda: self.put("x.json", b"[" * 30000 + b"]" * 30000), None, None),   # mutants: test_depth_* below
        }

    def test_each_kind_is_skipped_and_counted_under_its_own_name(self):
        for kind, (setup, _old, _new) in self.kind_cases().items():
            with self.subTest(kind=kind):
                for f in list(self.dir.iterdir()):
                    shutil.rmtree(str(f)) if f.is_dir() and not f.is_symlink() else f.unlink()
                setup()
                got = self.read()
                want = {"fifo": "not_regular", "directory": "not_regular"}.get(kind, kind)
                self.assertEqual(got["records"], [])
                self.assertEqual(got["skips"][want], 1, got)
                self.assertEqual(got["skipped"], 1)

    def test_each_kind_has_a_mutant_that_fails_its_check(self):
        for kind, (setup, old, new) in self.kind_cases().items():
            if old is None:
                continue
            with self.subTest(kind=kind):
                for f in list(self.dir.iterdir()):
                    shutil.rmtree(str(f)) if f.is_dir() and not f.is_symlink() else f.unlink()
                setup()
                want = {"fifo": "not_regular", "directory": "not_regular"}.get(kind, kind)
                try:
                    got = self.read(self.module(old, new))
                except (AssertionError, Exception):
                    continue   # the mutant hung or raised: caught
                self.assertNotEqual((got["records"], got["skips"][want]), ([], 1), "the mutant of %s survived" % kind)

    # ---- #1073 repair of #1155: a Python-independent depth limit (the scan never reads the interpreter's recursion limit)
    def nested(self, n):
        return "".join('{"a":' for _ in range(n)) + "1" + "}" * n

    def test_depth_exactly_at_the_limit_parses_and_one_over_is_too_deep(self):
        limit = self.cloud.WATCH_MAX_DEPTH
        self.assertEqual(limit, 64)
        self.put("x.json", self.nested(limit).encode())
        got = self.read()
        self.assertEqual((len(got["records"]), got["skipped"]), (1, 0))
        for f in self.dir.iterdir():
            f.unlink()
        self.put("x.json", self.nested(limit + 1).encode())
        got = self.read()
        self.assertEqual((got["records"], got["skips"]["too_deep"]), ([], 1))

    def test_brackets_in_strings_and_escaped_quotes_do_not_count(self):
        c = self.cloud
        self.assertFalse(c.nesting_too_deep(json.dumps({"a": "[" * 500 + "{" * 500})))
        self.assertFalse(c.nesting_too_deep(json.dumps({"a": 'x\\" ' + "[" * 500})))
        self.assertFalse(c.nesting_too_deep('{"a": "\\\\", "b": "[[[[[[[[[["}', 2))
        self.assertTrue(c.nesting_too_deep("[" * 65))
        self.assertTrue(c.nesting_too_deep('["\\"", ' + "[" * 70))

    def test_all_bracket_file_of_64_kib_is_too_deep_and_fast(self):
        import time
        self.put("x.json", b"[" * 65536)
        start = time.monotonic()
        got = self.read()
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(got["skips"]["too_deep"], 1)
        start = time.monotonic()
        self.assertTrue(self.cloud.nesting_too_deep("[" * 65536))
        self.assertLess(time.monotonic() - start, 1.0)

    def reader_checks(self):
        deep = self.nested(65)
        def watch(mod):
            self.put("x.json", deep.encode())
            return mod.read_records_bounded(self.tmp)["skips"]["too_deep"] == 1
        def shared(mod):
            f = self.dir / "y.json"
            f.write_text(deep)
            return mod.read_record_file(str(f), 65536) == (None, "too_deep")
        def load(mod):
            (self.dir / "z.json").write_text(deep)
            os.chmod(str(self.dir / "z.json"), 0o600)
            try:
                mod.load_record(self.tmp, "z")
            except mod.CloudError as err:
                return "nested deeper" in str(err)
            return False
        return {
            "read_records_bounded (_read_one)": (watch, "        record = parse_bounded(text)\n    except TooDeep:\n        return None, \"too_deep\"\n    except ValueError:\n        return None, \"not_json\"\n    if not isinstance(record, dict):\n        return None, \"wrong_shape\"\n    return record, None\n\n\ndef read_records_bounded", None),
            "read_record_file": (shared, None, None),
            "load_record": (load, "record = parse_bounded(_read_private(path, RECORD_MAX))", "record = json.loads(_read_private(path, RECORD_MAX))"),
        }

    def test_every_reader_refuses_too_deep_with_its_own_kind(self):
        for name, (check, _o, _n) in self.reader_checks().items():
            with self.subTest(reader=name):
                self.assertTrue(check(self.cloud))

    def test_a_limit_of_a_billion_fails_every_reader_on_every_python(self):
        mutant = self.module("WATCH_MAX_DEPTH = 64 ", "WATCH_MAX_DEPTH = 10**9 ")
        for name, (check, _o, _n) in self.reader_checks().items():
            with self.subTest(reader=name):
                for f in list(self.dir.iterdir()):
                    f.unlink()
                try:
                    self.assertFalse(check(mutant))
                except Exception:
                    pass

    def test_each_reader_mutant_that_drops_the_bounded_parse_is_caught(self):
        """Per-reader mutant: that reader parses with a plain json.loads and a 65-deep record is no longer refused."""
        checks = self.reader_checks()
        text = self.SRC.read_text()
        # one mutant per reader: swap that reader's parse_bounded call for json.loads (the first, second, third call site in file order)
        sites = [i for i in range(len(text)) if text.startswith("parse_bounded(", i) and not text.startswith("parse_bounded(text):", i)]
        self.assertEqual(len(sites), 3)
        for site, key in zip(sites, ("read_record_file", "load_record", "read_records_bounded (_read_one)")):
            with self.subTest(reader=key):
                mutated = text[:site] + "json.loads(" + text[site + len("parse_bounded("):]
                mod = types.ModuleType("cloud_mutant")
                mod.__file__ = str(self.SRC)
                sys.modules["cloud_mutant"] = mod
                self.addCleanup(sys.modules.pop, "cloud_mutant", None)
                exec(compile(mutated, str(self.SRC), "exec"), mod.__dict__)
                for f in list(self.dir.iterdir()):
                    f.unlink()
                check = checks[key][0]
                try:
                    ok = check(mod.bind(sb.__dict__))
                except Exception:
                    ok = False
                self.assertFalse(ok, "the mutant of %s survived" % key)

    def test_every_untrusted_record_parse_is_bounded_and_listed_in_the_docs(self):
        """Completeness: every json.loads in cloud_worker.py of a record file goes through parse_bounded, and the docs table names each reader."""
        text = self.SRC.read_text()
        loads = [l.strip() for l in text.splitlines() if "json.loads(" in l and not l.lstrip().startswith("#")]
        self.assertEqual(loads, ["return json.loads(text)", "body = json.loads(out) if (out or \"\").strip() else None"])
        import re
        readers = sorted(set(re.findall(r"^def (\w+)\(", text, re.M)) & {"read_record_file", "_read_one", "load_record"})
        sites = text.count("parse_bounded(")
        self.assertEqual((len(readers), sites), (3, 4))   # 3 call sites + the definition
        docs = (REPO / "docs" / "switchboard.md").read_text()
        section = docs[docs.index("## Cloud watch"):docs.index("## The assembler")]
        for name in readers + ["live_cloud_by_lane", "WATCH_MAX_DEPTH", "parse_bounded"]:
            self.assertIn(name, section)

    def test_mutant_plain_open_hangs_on_a_fifo(self):
        os.mkfifo(str(self.dir / "x.json"))
        plain = self.module("fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)", "fd = os.open(path, os.O_RDONLY)")
        with self.assertRaises(AssertionError):
            self.read(plain)
        self.assertEqual(self.read()["skips"]["not_regular"], 1)

    def test_files_examined_bound_with_more_bad_files_than_the_bound(self):
        for i in range(self.cloud.WATCH_MAX_FILES + 30):
            self.put("b%04d.json" % i, b"{nope")
        got = self.read()
        self.assertEqual(got["skipped"], self.cloud.WATCH_MAX_FILES)
        self.assertEqual(got["bound"], "files")
        mutant = self.module("elif examined >= max_files:", "elif False:")
        self.assertGreater(self.read(mutant)["skipped"], self.cloud.WATCH_MAX_FILES)

    def test_record_bound_names_records(self):
        for i in range(self.cloud.WATCH_MAX_RECORDS + 3):
            self.put("w%03d.json" % i)
        got = self.read()
        self.assertEqual((len(got["records"]), got["bound"]), (self.cloud.WATCH_MAX_RECORDS, "records"))
        mutant = self.module("if len(out[\"records\"]) >= max_records:", "if False:")
        self.assertGreater(len(self.read(mutant)["records"]), self.cloud.WATCH_MAX_RECORDS)

    def test_time_bound_stops_and_says_so(self):
        for i in range(10):
            self.put("w%d.json" % i)
        ticks = iter(range(0, 1000))
        got = self.read(clock=lambda: next(ticks) * 0.5, seconds=1.0)
        self.assertEqual(got["bound"], "time")
        self.assertLess(len(got["records"]), 10)
        mutant = self.module("elif clock() > deadline:", "elif False:")
        ticks = iter(range(0, 1000))
        self.assertEqual(len(self.read(mutant, clock=lambda: next(ticks) * 0.5, seconds=1.0)["records"]), 10)

    def test_rotation_gives_every_record_a_turn(self):
        for i in range(8):
            self.put("w%d.json" % i)
        seen, cursor = set(), None
        for _ in range(4):
            got = self.read(max_records=2, cursor=cursor)
            cursor = got["cursor"]
            seen |= {r["w"] for r in got["records"]}
            self.assertEqual(len(got["records"]), 2)
        names = set()
        cursor = None
        for _ in range(4):
            got = self.read(max_records=2, cursor=cursor)
            cursor = got["cursor"]
            names.add(cursor)
        self.assertEqual(len(names), 4)
        mutant = self.module("after = [n for n in names if n > cursor]", "after = []")
        cursor, last = None, set()
        for _ in range(4):
            got = self.read(mutant, max_records=2, cursor=cursor)
            cursor = got["cursor"]
            last.add(cursor)
        self.assertEqual(len(last), 1)

    def test_tick_makes_no_write_process_network_or_remove_call_and_audits_the_bound(self):
        for i in range(self.cloud.WATCH_MAX_FILES + 2):
            self.put("b%04d.json" % i, b"{nope")
        audit = []
        engine = types.SimpleNamespace(audit=types.SimpleNamespace(append=lambda *a: audit.append(a)), actor="daemon")
        def boom(*a, **k):
            raise AssertionError("the tick made a forbidden call")
        import socket
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "st")}):
            (sb.state_dir_path() / "cloud").mkdir(parents=True)
            for f in self.dir.iterdir():
                shutil.copy(str(f), str(sb.state_dir_path() / "cloud" / f.name))
            with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), mock.patch.object(os, "remove", boom), \
                    mock.patch.object(os, "unlink", boom), mock.patch.object(os, "rename", boom), mock.patch.object(os, "write", boom), \
                    mock.patch.object(socket, "socket", boom), mock.patch("builtins.open", boom):
                sb.cloud_watch_tick(engine, None)
                sb.cloud_watch_tick(engine, None)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][1], "cloud-watch.capped")
        self.assertEqual(audit[0][3]["bound"], "files")
        self.assertEqual(audit[0][3]["skipped"], {"not_json": self.cloud.WATCH_MAX_FILES})

    def test_mutants_write_call_and_no_audit_are_caught(self):
        # a tick that writes or never audits fails the recording test above: prove the audit one by a source mutant
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertIn('engine.audit.append(engine.actor, "cloud-watch.capped"', src)
        self.assertNotIn("os.unlink", src[src.index("def cloud_watch_tick"):src.index("def finish_pass")])
        self.assertNotIn("subprocess", src[src.index("def cloud_watch_tick"):src.index("def finish_pass")])

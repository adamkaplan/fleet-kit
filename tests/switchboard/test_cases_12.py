"""Test classes moved out of bin/test-switchboard (text unchanged): cases_12."""
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
from support.bases import StandInPids
from support.core import Base
from support.loader import SWITCHBOARD, captured, sb
from support.orders_and_adapters import BoundedList, BoundedOut, janitor_review
from support.paths import REPO
from support.rigs import World, _artifact_zip
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


class TestHeavyCeilings(StandInPids, unittest.TestCase):
    """Ask #930: a time ceiling per KIND of heavy run, read from the command. The clock, the process table, the signals, the sender and the audit are injected, so nothing
    waits for a real hour; the one real-process case (a wrapper killed with SIGKILL) is in TestHeavySlots."""

    # THE family table of the test: the kind, a command of that kind, the default ceiling. Derived completeness: a kind in the code with no row here fails.
    FAMILY = (
        ("full", ["bin/test-switchboard"], 5400),
        ("focused", ["bin/test-switchboard", "-k", "x"], 1800),
        ("e2e", ["playwright", "test"], 3600),
        ("other", ["ls"], 3600),
    )
    RUNNABLE = {"full": ["python3", "/nowhere/test-fake"], "focused": ["python3", "/nowhere/test-fake", "-k", "x"], "e2e": ["sh", "-c", "true", "e2e"], "other": ["sh", "-c", "true"]}   # commands that exist, for run()
    NAMED = {"quick": 600}   # kinds classify() never returns: run_quick takes the row by name (#1045); the quick slot's own 10 min ceiling is why it is below the 1800 floor of FAMILY
    CLASSIFY = (
        (["bin/test-switchboard"], "full"), (["bin/test-switchboard", "-k", "x"], "focused"), (["tests/test_a.py"], "focused"),
        (["python3", "-m", "unittest", "a.b"], "focused"), (["playwright", "test"], "e2e"), (["bin/run-e2e.sh"], "e2e"),
        (["timeout", "60", "bin/test-switchboard"], "full"), (["ls"], "other"), (["sh", "x"], "other"),
    )

    def setUp(self):
        self.hs = sb.load_heavy_slots()
        self.now = [1000.0]
        self.signals, self.out = BoundedList(), BoundedOut()
        self.exited = threading.Event()
        self.reads = BoundedList()

    def drive(self, command, schedule, environ_extra=None, ceiling=None, config=None, short=False, on_stop=None, name="holder"):
        """run() of `command` whose child is fake; the watchdog's reads move the clock to start + schedule[n] (seconds); returns (exit status, signals seen at each read)."""
        test, seen = self, []
        base = test.now[0]

        class FakeChild:
            pid = self.px[4242]

            def __init__(child, argv, **kwargs):
                pass

            def wait(child):
                if not test.exited.wait(30):
                    raise AssertionError("the fake command was never stopped")
                return -9

        def read_table():
            seen.append(len(test.signals))
            if test.exited.is_set() or len(seen) > len(schedule):
                test.exited.set()
                return []
            test.now[0] = base + schedule[len(seen) - 1]
            return [(self.px[4242], 1, os.getpgrp(), 1), (self.px[4243], self.px[4242], os.getpgrp(), 1), (self.px[777], 1, self.px[777], 1)]   # a child and a grandchild of the run, and a stranger

        def record(number, sig):
            test.signals.append((number, sig))
            if sig == signal.SIGKILL:
                test.exited.set()

        environ = {"PATH": os.environ["PATH"], "FLEET_HEAVY": "off"}
        environ.update(environ_extra or {})
        dog = {"read_table": read_table, "pause": lambda seconds: False, "killpg": record, "kill": record, "every": 0.0, "grace": 0.001, "sweep_pause": lambda s: None}
        with mock.patch.object(self.hs.subprocess, "Popen", FakeChild):
            code = self.hs.run(command, None, 3, 1, name, environ=environ, out=self.out, clock=lambda: test.now[0], sleep=lambda s: None, watchdog=dog,
                               short=short, ceiling=ceiling, ceiling_config=config, on_stop=on_stop)
        return code, seen

    def test_s2_every_command_reads_as_its_kind_and_every_kind_has_a_row(self):
        for command, kind in self.CLASSIFY:
            with self.subTest(command=command):
                self.assertEqual(self.hs.classify(command), kind)
        self.assertEqual(self.hs.classify(["bin/test-switchboard"], short=True), "focused", "--short declares a short run")
        every = {kind for kind, _s, _m in self.hs.KINDS}
        self.assertEqual({row[0] for row in self.FAMILY} | set(self.NAMED), every, "a kind with no row here, or a row with no kind")
        self.assertEqual({kind for _c, kind in self.CLASSIFY} | set(self.NAMED), every, "a kind no command above reads as (or takes by name)")
        for kind, seconds in self.NAMED.items():
            self.assertEqual(self.hs.KIND_DEFAULTS[kind], seconds, "the named kind's ceiling")
            self.assertNotIn(kind, {self.hs.classify(command) for command, _k in self.CLASSIFY}, "a named kind is never read from a command")
        for kind, command, _default in self.FAMILY:
            self.assertEqual(self.hs.classify(command), kind)
            self.assertEqual(self.hs.classify(self.RUNNABLE[kind]), kind)

    def test_m2_a_word_that_only_contains_a_kind_name_never_changes_the_kind(self):
        """One case per matcher: a path, a filter value and a shell string that contain each kind's word; the kind stays that of the PROGRAM and its whole option words."""
        words = [w for w in self.hs.E2E_WORDS if not w.startswith("-")]
        self.assertTrue(words)
        for word in words:
            with self.subTest(word=word):
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "-k", "test_%s_tabs" % word]), "focused")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "-k", word]), "focused", "a filter VALUE is not read")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "/work/repo-%s/file" % word]), "full", "a path argument")
                self.assertEqual(self.hs.classify(["sh", "-c", "cd /x/%s-app && bin/test-switchboard" % word]), "other", "a shell string is not read")
                self.assertEqual(self.hs.classify(["cat", "/tmp/%s.txt" % word]), "other")
                self.assertEqual(self.hs.classify(["python3", "/home/user/%s-notes/run.py" % word]), "other")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "--name", word]), "full", "the VALUE of an option the table does not list")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "--name=%s" % word]), "full")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "-v", "tests/test_x.py"]), "focused", "a test file after a flag still counts")
                self.assertEqual(self.hs.classify(["bin/test-switchboard", "tests/test_%s.py" % word]), "focused", "a test file argument is a focused run, whatever its name")
                self.assertEqual(self.hs.classify(["python3", "-m", word]), "e2e", "the program itself, or a whole word, is read")
                self.assertEqual(self.hs.classify(["npx", word, "test"]), "e2e")
                self.assertEqual(self.hs.classify(["timeout", "60", "bin/%s-runner" % word]), "e2e", "the program's own name")
        self.assertEqual(self.hs.classify(["bin/test-switchboard", "--browser"]), "e2e", "a whole option word")
        self.assertEqual(self.hs.classify(["python3", "-m", "unittest", "discover"]), "full", "a whole suite")
        self.assertEqual(self.hs.classify(["python3", "-m", "unittest"]), "full")
        self.assertEqual(self.hs.classify(["python3", "-m", "unittest", "-v", "a.b"]), "focused")
        self.assertEqual(self.hs.classify(["pytest", "a/b.py::t"]), "focused")
        self.assertEqual(self.hs.classify(["pytest"]), "full")
        for command in (["make", "test"], ["npm", "test"], ["go", "test"]):
            self.assertEqual(self.hs.classify(command), "full")
        self.assertEqual(self.hs.classify([]), "other")

    def test_the_configured_ceilings_never_pass_the_hard_maximum(self):
        for key in ("full", "focused", "e2e", "other", "max"):
            with self.subTest(key=key), self.assertRaises(self.hs.HeavyError):
                self.hs.settings({"ceilings": {key: 14401}})
        table = self.hs.ceiling_table({"ceilings": {"full": 99999999, "max": 99999999}})   # a config that was not validated reads as the default, never above the maximum
        self.assertEqual(table["max"], 14400)
        self.assertLessEqual(max(table.values()), 14400)
        self.assertEqual(self.hs.resolve_ceiling("full", {}, "99999", {"ceilings": {"full": 14401}}), 14400)

    def test_s3_each_kind_has_its_default_and_a_configured_value_replaces_only_its_own(self):
        for kind, command, default in self.FAMILY:
            with self.subTest(kind=kind):
                self.assertEqual(self.hs.resolve_ceiling(kind, {}), default)
                other = {"ceilings": {kind: 900}}
                self.assertEqual(self.hs.resolve_ceiling(kind, {}, config=other), 900)
                for rest, _c, rest_default in self.FAMILY:
                    if rest != kind:
                        self.assertEqual(self.hs.resolve_ceiling(rest, {}, config=other), rest_default)
        self.assertEqual(self.hs.ceiling_table(None)["max"], 14400)
        self.assertEqual(self.hs.resolve_ceiling("full", {}, "99999", {"ceilings": {"max": 7000}}), 7000, "the maximum is configurable")
        self.assertGreaterEqual(min(d for _k, _c, d in self.FAMILY), 1800, "no default is below the repository's longest CI step limit")

    def test_s4_an_unreadable_value_is_the_default_or_the_maximum_never_off(self):
        for kind, _command, default in self.FAMILY:
            for bad in ("lots", "-5", "1e3", "", " ", "5 s", None):
                with self.subTest(kind=kind, value=bad):
                    for got in (self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": bad} if bad is not None else {}), self.hs.resolve_ceiling(kind, {}, bad)):
                        self.assertEqual(got, default)
            self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": "99999999999999"}), 14400)
            for bad in ("lots", 0, -1, True, 1.5, None):   # an unreadable configured value is the default
                self.assertEqual(self.hs.resolve_ceiling(kind, {}, config={"ceilings": {kind: bad}}), default)

    def test_s5_a_higher_request_is_honoured_up_to_the_maximum_and_zero_is_the_maximum(self):
        for kind, _command, _default in self.FAMILY:
            with self.subTest(kind=kind):
                self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": "10800"}), 10800)
                self.assertEqual(self.hs.resolve_ceiling(kind, {}, "10800"), 10800)
                self.assertEqual(self.hs.resolve_ceiling(kind, {}, "10800", None), 10800)
                for big in ("16200", "99999999"):
                    self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": big}), 14400)
                self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": "0"}), 14400)
                self.assertEqual(self.hs.resolve_ceiling(kind, {}, "0"), 14400)
                self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": "60"}), 60, "a request may be lower (preflight-pr)")
                self.assertEqual(self.hs.resolve_ceiling(kind, {"FLEET_HEAVY_MAX_SECONDS": "60"}, "120"), 120, "--ceiling wins over the environment")

    def test_s6_each_kind_runs_on_one_second_before_its_ceiling_and_is_stopped_past_it_with_124(self):
        for kind, command, default in self.FAMILY:
            with self.subTest(kind=kind, at="ceiling-1"):
                self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
                code, seen = self.drive(self.RUNNABLE[kind], [default - 1, default + 1])
                self.assertEqual(seen[:2], [0, 0], "before the ceiling nothing is signalled (the second read is the one at ceiling-1)")
                self.assertEqual(code, 124)
                self.assertEqual({p for p, _s in self.signals}, {self.px[4242], self.px[4243]}, "the child and the grandchild go, the stranger does not")
                self.assertIn("time ceiling of %d s" % default, self.out.getvalue())
            with self.subTest(kind=kind, at="exactly the ceiling"):
                self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
                code, seen = self.drive(self.RUNNABLE[kind], [default, default + 1])
                self.assertEqual(seen[1], 0, "at the ceiling itself the run still goes on (stopped past it)")
                self.assertEqual(code, 124)

    def test_u1_a_run_under_its_ceiling_prints_nothing_new_and_keeps_its_exit_status(self):
        for status in (0, 3):
            with self.subTest(status=status):
                code = self.hs.run(["sh", "-c", "exit %d" % status], None, 3, 1, "who", environ={"PATH": os.environ["PATH"], "FLEET_HEAVY": "off"}, out=(out := BoundedOut()))
                self.assertEqual((code, out.getvalue()), (status, ""))
        out = BoundedOut()
        self.assertEqual(self.hs.run(["no-such-command-930"], None, 3, 1, "who", environ={"PATH": os.environ["PATH"]}, out=out), 127)
        self.assertEqual(out.getvalue(), "heavy: no-such-command-930: command not found\n")

    def test_u2_the_old_configuration_stays_valid_and_unknown_keys_are_refused(self):
        self.assertEqual(self.hs.settings(None), 3)
        self.assertEqual(self.hs.settings({"slots": 2}), 2)
        self.assertEqual(self.hs.settings({"slots": 2, "ceilings": {"full": 3600, "max": 7200}}), 2)
        for bad in ({"slotz": 2}, {"ceilings": {"huge": 5}}, {"ceilings": {"full": 0}}, {"ceilings": {"full": "x"}}, {"ceilings": 5}):
            with self.subTest(bad=bad), self.assertRaises(self.hs.HeavyError):
                self.hs.settings(bad)

    def canary(self):
        return "".join(["sk", "-", "canary", "-", "930", "-", "abcdef"])   # built at run time: no secret-shaped literal in the file

    def hooks(self, sent, audited, answer="sent", audit_fails=False, order=None):
        """The two-phase on_stop of the product, built from the REAL record_stop and tell_lane with a recording sender and audit."""
        def send(lane, text):
            sent.append((lane, text))
            if isinstance(answer, Exception):
                raise answer
            return answer

        def audit(*a):
            if order is not None:
                order.append(("audit", len(self.signals)))   # how many signals had been sent when the line was written
            if audit_fails:
                raise OSError("the audit sink is full")
            audited.append(a)
        note = self.out.write

        def on_stop(phase, info, holder, lane):
            if phase == "decided":
                self.hs.record_stop(info, holder, audit, note)
            else:
                self.hs.tell_lane(info, holder, lane, send, audit, 14400, note)
        return on_stop

    def reported(self, command, **kw):
        """A time stop through the real hooks; returns (exit status, sent, audited, secret, order)."""
        sent, audited, order = BoundedList(), BoundedList(), BoundedList()
        stop = self.hooks(sent, audited, kw.pop("answer", "sent"), kw.pop("audit_fails", False), order)
        secret = self.canary()
        code, _seen = self.drive(command + [secret], [5401], environ_extra={"FLEET_HEAVY_SECRET_930": secret, "FLEET_HEAVY_NAME": "lane-x"}, on_stop=stop, name="holder-x", **kw)
        return code, sent, audited, secret, order

    def test_s7_the_audit_lines_have_exactly_their_fields_and_no_argument_or_environment(self):
        for kind, command, default in self.FAMILY:
            with self.subTest(kind=kind):
                self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
                code, sent, audited, secret, order = self.reported(self.RUNNABLE[kind], ceiling=str(default))
                self.assertEqual(code, 124)
                self.assertEqual([a[0] for a in audited], ["heavy.stopped", "heavy.lane_told"], "the stop is audited first, the message after")
                (event, subject, detail), (_e2, _s2, told) = audited
                self.assertEqual(subject, "holder-x")
                self.assertEqual(set(detail), {"kind", "ran_seconds", "ceiling_seconds", "pid", "reason", "source"})
                self.assertEqual((detail["kind"], detail["ceiling_seconds"], detail["pid"], detail["source"], detail["reason"]), (kind, default, self.px[4242], "wrapper", "time"))
                self.assertGreater(detail["ran_seconds"], default)
                self.assertEqual(told, {"pid": self.px[4242], "source": "wrapper", "lane_told": "sent"})
                self.assertEqual(order[0], ("audit", 0), "the heavy.stopped line is written before the first signal")
                line = json.dumps(list(audited))
                for text in (secret, "FLEET_HEAVY_SECRET_930", "PATH"):
                    self.assertNotIn(text, line)

    def test_s8_the_lane_is_told_once_with_fixed_words_and_an_unknown_name_is_said_in_the_audit(self):
        for answer in ("sent", "unknown name", "failed", KeyError("the sender is broken")):
            with self.subTest(answer=answer):
                self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
                code, sent, audited, secret, order = self.reported(self.RUNNABLE["full"], answer=answer)
                self.assertEqual(code, 124, "a message that fails never changes the exit status")
                self.assertEqual(len(sent), 1, "told once, no retry")
                self.assertEqual(sent[0][0], "lane-x", "the recorded lane, not the holder's name")
                self.assertEqual(audited[1][2]["lane_told"], answer if isinstance(answer, str) else "failed")
                self.assertRegex(sent[0][1], r"^heavy: your full run \(pid %d\) was stopped after \d+ s; its ceiling was 5400 s \(FLEET_HEAVY_MAX_SECONDS or heavy --ceiling asks for more, up to 14400\)$" % self.px[4242])

    def test_m3_an_audit_sink_that_fails_never_prevents_the_stop_and_the_failure_is_on_the_record(self):
        self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
        code, sent, audited, secret, order = self.reported(self.RUNNABLE["full"], audit_fails=True)
        self.assertEqual(code, 124)
        self.assertTrue(self.signals, "the run was stopped")
        self.assertEqual(list(audited), [])
        self.assertIn("could not write the audit line heavy.stopped (OSError: the audit sink is full): the run is stopped all the same", self.out.getvalue())
        self.assertEqual(len(sent), 1, "the lane is still told")

    @staticmethod
    def broken_sender(lane, text):
        raise KeyError("the sender is broken")

    def test_the_express_slot_keeps_its_120_s_ceiling_whatever_the_kind(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for index in range(3):   # the three ordinary slots are busy: a --short run takes the express one
            fd = os.open(os.path.join(tmp, "heavy-slot-%d.lock" % index), os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        test = self

        class FakeChild:
            pid = self.px[4242]

            def __init__(child, argv, **kwargs):
                pass

            def wait(child):
                test.exited.wait(30)
                return -9
        reads = BoundedList()

        def table():
            reads.append(1)
            if len(reads) > 2 or self.exited.is_set():
                self.exited.set()
                return []
            self.now[0] += 121
            return [(self.px[4242], 1, os.getpgrp(), 1)]
        dog = {"read_table": table, "pause": lambda s: False, "killpg": lambda n, g: self.exited.set(), "kill": lambda n, g: self.exited.set(), "lsof": lambda path: set(), "every": 0.0, "grace": 0.001, "sweep_pause": lambda s: None}
        with mock.patch.object(self.hs.subprocess, "Popen", FakeChild):
            code = self.hs.run(self.RUNNABLE["full"], tmp, 3, 5, "w", environ={"PATH": os.environ["PATH"]}, out=self.out, clock=lambda: self.now[0], sleep=lambda s: None,
                               watchdog=dog, short=True, ceiling="99999", line={"start_of": lambda pid: "t0", "alive": lambda pid, start: True})
        self.assertEqual(code, 124)
        self.assertIn("time ceiling of 120 s", self.out.getvalue())

    def test_s13_neither_the_message_nor_the_line_carries_a_canary_in_any_of_three_forms(self):
        secret = self.canary()
        for form, command, environ in (("argument", self.RUNNABLE["full"] + [secret], {}), ("environment", self.RUNNABLE["full"], {"FLEET_HEAVY_SECRET_930": secret}),
                                       ("both", self.RUNNABLE["full"] + [secret], {"FLEET_HEAVY_SECRET_930": secret})):
            with self.subTest(form=form):
                self.signals, self.exited = BoundedList(), threading.Event()
                sent, audited = BoundedList(), BoundedList()
                self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()
                self.drive(command, [5401], environ_extra=environ, on_stop=self.hooks(sent, audited))
                blob = json.dumps(list(sent) + list(audited))
                for forbidden in (secret, "FLEET_HEAVY_SECRET_930", "PATH", "nowhere"):
                    self.assertNotIn(forbidden, blob)

    def test_the_switchboard_sends_only_to_a_known_agent_and_says_so_for_an_unknown_name(self):
        calls = []
        engine = types.SimpleNamespace(now=lambda: 1.0, discover=lambda now: types.SimpleNamespace(agents={"coder-a": object()}), send=lambda *a, **k: calls.append(a) or {"sent": True})
        self.assertEqual(sb.heavy_stop_send(engine, "coder-a", "text"), "sent")
        self.assertEqual(sb.heavy_stop_send(engine, "nobody", "text"), "unknown name")
        self.assertEqual(len(calls), 1, "nothing is sent for an unknown name")
        engine.send = lambda *a, **k: {"sent": False}
        self.assertEqual(sb.heavy_stop_send(engine, "coder-a", "text"), "failed")

    # ---- the orphan: the slot is really locked, the clock, the table, the signals and the lock listing are injected

    def orphan_rig(self, record, rows=None, alive=lambda pid, start: False, held_by=None, survivor=False):
        held_by = held_by or (self.px[4242], self.px[4243])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "heavy-slot-0.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        os.write(fd, json.dumps(record).encode())
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the "run" holds the slot
        gone = threading.Event()
        signals = BoundedList()

        def record_signal(number, sig):
            signals.append((number, sig))
            if sig == signal.SIGKILL and not survivor:
                gone.set()
        table = rows or [(self.px[4242], 1, self.px[4242], 1, "t"), (self.px[4243], self.px[4242], self.px[4242], 1, "t"), (self.px[999], 1, self.px[999], 1, "t"), (self.px[555], 1, self.px[555], 1, "t")]
        dog = {"read_table": lambda: [] if gone.is_set() else table, "kill": record_signal, "killpg": record_signal, "lsof": lambda p: set(held_by), "grace": 0.001,
               "sweep_pause": lambda s: None, "pause": lambda s: False, "me": (os.getpid(), os.getppid(), os.getpgrp()), "rounds": 1}
        return tmp, dog, signals, alive

    @property
    def RECORD(self):
        return {"name": "h", "pid": self.px[4242], "wrapper": self.px[31337], "command": "x", "since": 1000, "kind": "full", "ceiling": 5400, "lane": "lane-x", "wstart": "w0", "cstart": "c0"}

    def orphan(self, ran, record=None, start_of=lambda pid: "c0", **rig):
        record = record or dict(self.RECORD)
        tmp, dog, signals, alive = self.orphan_rig(record, **rig)
        told = BoundedList()

        def on_stop(phase, info, holder, lane):
            told.append((phase, info, holder, lane, len(signals)))
        stopped = self.hs.check_orphans(tmp, 3, None, lambda: 1000.0 + ran, on_stop, BoundedOut(), alive=alive, watchdog=dog, start_of=start_of)
        self.orphan_args = (tmp, dog, alive, on_stop, start_of)
        return stopped, told, signals

    def test_s9_an_orphan_is_left_alone_at_its_ceiling_and_stopped_past_it_measured_from_the_slot_start(self):
        stopped, told, signals = self.orphan(5400)
        self.assertEqual((stopped, list(signals)), ([], []), "at the ceiling nothing is signalled")
        stopped, told, signals = self.orphan(5401)
        self.assertTrue({self.px[4242]} <= {p for p, _s in signals} <= {self.px[4242], self.px[4243]}, "the run's group or tree, nothing else (self.px[999] and self.px[555] are strangers)")
        self.assertEqual(len(stopped), 1)
        self.assertEqual([(t[0], t[4] == 0) for t in told[:1]], [("decided", True)], "the audit phase runs before the first signal")
        self.assertEqual([t[0] for t in told], ["decided", "told"])
        phase, info, holder, lane, _n = told[1]
        self.assertEqual((info["source"], info["kind"], info["ceiling_seconds"], info["pid"], info["ran_seconds"], holder, lane), ("orphan", "full", 5400, self.px[4242], 5401, "h", "lane-x"))
        stopped, told, signals = self.orphan(99999, alive=lambda pid, start: True)
        self.assertEqual((stopped, list(signals)), ([], []), "a live wrapper's run is never touched by the orphan check")

    def test_s10_an_old_record_reads_as_other_and_a_reused_wrapper_pid_is_an_orphan(self):
        old = {"name": "h", "pid": self.px[4242], "wrapper": self.px[31337], "command": "x", "since": 1000}
        self.assertEqual(self.orphan(3600, old)[0], [])
        stopped, told, signals = self.orphan(3601, old)
        self.assertEqual((stopped[0]["kind"], stopped[0]["ceiling_seconds"]), ("other", 3600))
        seen = []
        stopped, told, signals = self.orphan(6000, alive=lambda pid, start: seen.append((pid, start)) or False)   # the start time recorded is the one asked for
        self.assertEqual(seen, [(self.px[31337], "w0")])
        self.assertEqual(len(stopped), 1, "a pid reused by another process (another start time) is judged gone")

    def test_u3_an_orphan_stop_signals_only_the_runs_own_processes(self):
        stopped, told, signals = self.orphan(9000, held_by=(self.px[4242], os.getpid(), os.getppid(), 1))
        self.assertTrue({p for p, _s in signals} <= {self.px[4242], self.px[4243]}, signals)
        self.assertFalse({os.getpid(), os.getppid(), 1, self.px[999], self.px[555]} & {p for p, _s in signals})

    def test_m1_a_pid_from_the_record_is_a_root_only_with_its_recorded_start_time(self):
        """The run's command has exited, a descendant (self.px[4300]) keeps the lock, and the recorded command pid self.px[4242] now belongs to a bystander."""
        rows = [(self.px[4242], 1, self.px[4242], 1, "bystander"), (self.px[4300], 1, self.px[4300], 1, "t"), (self.px[555], 1, self.px[555], 1, "t")]
        base = dict(self.RECORD)
        cases = (("another process has the pid", base, lambda pid: "someone-else"),
                 ("an older record without the start time", {k: v for k, v in base.items() if k != "cstart"}, lambda pid: "c0"),
                 ("an empty start time", dict(base, cstart=""), lambda pid: "c0"),
                 ("the start time cannot be read", base, lambda pid: ""))
        for name, record, start_of in cases:
            with self.subTest(name):
                stopped, told, signals = self.orphan(9000, record, start_of=start_of, rows=rows, held_by=(self.px[4300],))
                self.assertEqual(len(stopped), 1)
                self.assertEqual({p for p, _s in signals}, {self.px[4300]}, "only the lock holder is signalled: not the pid of the record, not a stranger")
        for pid in (1, 0, os.getpid(), os.getppid()):
            with self.subTest(pid=pid):
                stopped, told, signals = self.orphan(9000, dict(base, pid=pid), start_of=lambda p: "c0", rows=rows + [(pid, 1, pid, 1, "t")] if pid > 1 else rows, held_by=(self.px[4300],))
                self.assertFalse({1, os.getpid(), os.getppid(), os.getpgrp()} & {p for p, _s in signals}, "pid 1, the checker, its parent and its group are never signalled")

    def test_m1_with_the_matching_start_time_the_recorded_pid_is_still_stopped(self):
        stopped, told, signals = self.orphan(9000, start_of=lambda pid: "c0")
        self.assertIn(self.px[4242], {p for p, _s in signals})

    def test_m3_a_report_hook_that_fails_before_the_stop_is_said_and_the_run_is_stopped_all_the_same(self):
        self.signals, self.exited, self.out = BoundedList(), threading.Event(), BoundedOut()

        def broken(phase, info, holder, lane):
            raise RuntimeError("the hook is broken")
        code, _seen = self.drive(self.RUNNABLE["full"], [5401], on_stop=broken)
        self.assertEqual(code, 124)
        self.assertTrue(self.signals)
        self.assertEqual(self.out.getvalue().count("the stop report failed (RuntimeError: the hook is broken)"), 2, "said for the decided phase and for the told phase")

    def test_a_tick_that_does_not_stop_marks_nothing_and_the_next_stop_audits_and_tells_once(self):
        """The two checkers (the daemon and a waiting wrapper) meet: a tick whose stop does not happen must leave the run unreported."""
        for case in ("the orphan lock is busy", "the holders are not seen twice", "the slot changed hands"):
            with self.subTest(case):
                holders = [self.px[4242], self.px[4243]]
                base = {k: v for k, v in self.RECORD.items() if k != "cstart" or case != "the holders are not seen twice"}   # no recorded start: only lock holders are roots
                tmp, dog, signals, alive = self.orphan_rig(dict(base), held_by=holders)
                gate = os.open(os.path.join(tmp, self.hs.ORPHAN_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
                self.addCleanup(os.close, gate)
                slot = os.path.join(tmp, "heavy-slot-0.lock")
                real = dict(base)
                told = BoundedList()
                on_stop = lambda phase, info, holder, lane: told.append(phase)
                state = {"swap": False}

                def lsof(path):
                    if state["swap"]:   # the slot is taken by a newer run while the look is made
                        with open(path, "r+b") as handle:
                            handle.truncate(0)
                            handle.write(json.dumps(dict(real, since=5000)).encode())
                    return set(holders)
                dog["lsof"] = lsof
                if case == "the orphan lock is busy":
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif case == "the holders are not seen twice":
                    holders[:] = []
                else:
                    state["swap"] = True
                check = lambda: self.hs.check_orphans(tmp, 3, None, lambda: 1000.0 + 9000, on_stop, BoundedOut(), alive=alive, watchdog=dog, start_of=lambda p: "c0")
                self.assertEqual((check(), list(told), list(signals)), ([], [], []), "tick 1: no stop, nothing reported")
                self.assertFalse(os.path.exists(os.path.join(tmp, "heavy-told-0.json")), "tick 1 marks nothing")
                if case == "the orphan lock is busy":
                    fcntl.flock(gate, fcntl.LOCK_UN)
                elif case == "the holders are not seen twice":
                    holders[:] = [self.px[4242], self.px[4243]]
                else:
                    state["swap"] = False
                    with open(slot, "r+b") as handle:
                        handle.truncate(0)
                        handle.write(json.dumps(real).encode())
                self.assertEqual(len(check()), 1, "tick 2 stops the run")
                self.assertEqual(list(told), ["decided", "told"], "audited once and told once")
                self.assertTrue(os.path.exists(os.path.join(tmp, "heavy-told-0.json")))

    def test_the_lane_is_told_once_for_a_run_that_survives_the_stop_and_is_stopped_again(self):
        stopped, told, signals = self.orphan(9000, survivor=True)
        tmp, dog, alive, on_stop, start_of = self.orphan_args
        first = len(signals)
        again = self.hs.check_orphans(tmp, 3, None, lambda: 1000.0 + 9100, on_stop, BoundedOut(), alive=alive, watchdog=dog, start_of=start_of)
        self.assertEqual(len(again), 1, "the survivor is stopped again")
        self.assertGreater(len(signals), first)
        self.assertEqual([t[0] for t in told], ["decided", "told"], "reported once for the same run")

    def test_a_slot_that_changed_hands_during_the_stop_is_not_stopped(self):
        tmp, dog, signals, alive = self.orphan_rig({"name": "h", "pid": self.px[4242], "wrapper": self.px[31337], "command": "x", "since": 1000})
        newer = [0]

        def lsof(path):   # the look finds the holders, and the slot is then taken by a newer run: its record has another start
            with open(path, "r+b") as handle:
                handle.truncate(0)
                handle.write(json.dumps({"name": "n", "pid": self.px[4242], "wrapper": self.px[31337], "since": 5000}).encode())
            return {self.px[4242]}
        dog["lsof"] = lsof
        stopped = self.hs.check_orphans(tmp, 3, None, lambda: 20000.0, None, BoundedOut(), alive=alive, watchdog=dog, start_of=lambda p: "c0")
        self.assertEqual((stopped, list(signals)), ([], []))

    def test_two_checkers_never_stop_and_report_the_same_orphan(self):
        tmp, dog, signals, alive = self.orphan_rig({"name": "h", "pid": self.px[4242], "wrapper": self.px[31337], "command": "x", "since": 1000})
        gate = os.open(os.path.join(tmp, self.hs.ORPHAN_LOCK), os.O_RDWR | os.O_CREAT, 0o600)   # another checker is at it
        self.addCleanup(os.close, gate)
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.hs.check_orphans(tmp, 3, None, lambda: 20000.0, None, BoundedOut(), alive=alive, watchdog=dog), [])
        self.assertEqual(list(signals), [])

    def test_the_orphan_check_reports_nothing_and_stops_nothing_for_a_free_slot(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "heavy-slot-0.lock"), "w") as handle:
            handle.write(json.dumps({"name": "h", "pid": self.px[4242], "wrapper": self.px[31337], "since": 1}))
        self.assertEqual(self.hs.check_orphans(tmp, 3, None, lambda: 10 ** 9, None, BoundedOut(), alive=lambda p, s: False), [])

    def test_s11_every_case_named_in_the_ask_has_a_test(self):
        names = {n for n in dir(type(self)) if n.startswith("test_")}
        for case in ("test_s2_", "test_s3_", "test_s4_", "test_s5_", "test_s6_", "test_s7_", "test_s8_", "test_s9_", "test_s10_", "test_s13_", "test_u1_", "test_u2_", "test_u3_", "test_m1_", "test_m2_", "test_m3_"):
            self.assertTrue(any(n.startswith(case) for n in names), "no test for %s" % case)
        slots_tests = {n for n in dir(TestHeavySlots) if n.startswith("test_")}
        self.assertIn("test_s9_a_wrapper_killed_with_sigkill_leaves_a_run_that_the_orphan_bound_stops_and_audits", slots_tests)

    def test_s1_s12_the_docs_state_the_cause_the_measured_table_the_ceilings_and_how_to_raise_one(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        start = text.index("### `heavy`: CPU slots")
        section = text[start:text.index("\n### ", start + 10)]
        for needle in ("measured from the slot start", "7200", "heavy.stopped", "--ceiling", "FLEET_HEAVY_MAX_SECONDS", "0 no longer means off", "is read afresh by every `heavy` run"):
            self.assertIn(needle, section)
        for kind, _c, default in self.FAMILY:
            self.assertRegex(section, r"\| *%s *\|[^\n]*\b%d\b" % (kind, default), "a table row for the kind %s with its default" % kind)
            self.assertRegex(section, r"\| *%s *\|[^\n]*\| *[0-9]+ (runs|jobs|samples|holders)?" % kind)
        self.assertIn("14400", section)
        self.assertNotIn("default 7200", section)


class TestHeavySlots(Base):
    """`fleet-switchboard heavy` (ask #34): a flock slot semaphore in the state directory; the COMMAND holds the lock; the queue, the kill, the exit status, taskpolicy
    or nice, the holder list, the wait limit and the no-shell guarantee are each run for real with small commands."""

    def setUp(self):
        super().setUp()
        self.hs = sb.load_heavy_slots()
        self.dir = self.home / "slots"
        self.dir.mkdir()
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=90)
            except Exception:
                pass

    def env(self, **more):
        env = dict(os.environ, XDG_STATE_HOME=str(self.dir), FLEET_HEAVY_REPORT_SECONDS="0.3", PATH=os.environ["PATH"])
        for key in ("FLEET_HEAVY_SLOT", "FLEET_HEAVY", "CI"):   # the suite may itself run in a slot, under FLEET_HEAVY=off, or in CI: these tests are about the slots
            env.pop(key, None)
        env.update(more)
        return env

    def state(self):
        path = self.dir / "fleet-switchboard"
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def start(self, *words, slots=3, wait_max=None, name="t", env=None, new_session=True, **popen):
        argv = [sys.executable, str(SWITCHBOARD), "heavy", "--slots", str(slots), "--name", name]
        if wait_max is not None:
            argv += ["--wait-max", str(wait_max)]
        argv = argv + ["--"] + list(words)
        if self.START_DELAY:   # a holder slow to write its record: the real wrapper holds its lock at once but its record (os.pwrite) lands START_DELAY seconds late
            runner = ("import os, sys, time, runpy; real, delay = os.pwrite, float(sys.argv[1]); os.pwrite = lambda *a: (time.sleep(delay), real(*a))[1]; "
                      "script = sys.argv[2]; sys.argv = [script] + sys.argv[3:]; runpy.run_path(script, run_name='__main__')")
            argv = [sys.executable, "-c", runner, str(self.START_DELAY), str(SWITCHBOARD)] + argv[2:]
        proc = subprocess.Popen(argv, env=env or self.env(), start_new_session=new_session, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=popen.pop("stdin", subprocess.DEVNULL), text=True, **popen)
        self.procs.append(proc)
        return proc

    def waiter(self, tag):
        """A command that says it started and runs until its release file exists."""
        script = 'touch "%s/started.%s"; while [ ! -e "%s/release.%s" ]; do sleep 0.05; done' % (self.home, tag, self.home, tag)
        return ["sh", "-c", script]

    HOLD_CEILING_SECONDS = 300   # the integration test's preconditions: a bound that costs nothing when they hold, on the slowest runner

    def drain(self, proc):
        """What a wrapper has written to its stderr so far, without waiting for more."""
        got = b""
        while proc.stderr is not None and select.select([proc.stderr], [], [], 0)[0]:
            chunk = os.read(proc.stderr.fileno(), 65536)
            if not chunk:
                break
            got += chunk
        return got.decode("utf-8", "replace")

    def why(self, wrappers):
        """The message of a failed precondition: who holds a slot, which commands started, and for each wrapper its exit status (or still running) and its stderr, so a
        slow runner and a wrapper that ran its command WITHOUT a slot (`heavy: no slot (...)`) can be told apart."""
        lines = ["holders: %s" % self.hs.holders(self.dir / "fleet-switchboard", 3), "started: %s" % self.started()]   # the path as it is: never made here, so a refused one is shown as it is
        for tag, proc in wrappers.items():
            code = proc.poll()
            lines.append("wrapper %s (pid %d): %s; stderr: %r" % (tag, proc.pid, "exit %d" % code if code is not None else "still running", self.drain(proc)))
        return "\n".join(lines)

    def until(self, test, seconds=90):
        end = time.time() + seconds
        while time.time() < end:
            if test():
                return True
            time.sleep(0.05)
        return False

    def started(self):
        return sorted(p.name for p in self.home.glob("started.*"))

    READY_TRIES = 600    # the readiness helper: 600 reads of the holder record, 0.1 s apart (60 s: a bound for a hang, not a delay: a healthy holder is ready in milliseconds), unless a test names its own bound
    READY_PAUSE = 0.1
    START_DELAY = 0      # seconds a holder is made slow to start (the injected-delay class sets it)

    @staticmethod
    def holders_ready(found, count, name=None, wrapper_pids=()):
        """The readiness decision, from what `holders` listed: `count` holders, each with a real name and command (the record is written) and, for the real-process
        tests, a pid that is NOT its wrapper's. The wrapper writes the record TWICE: first its own pid, then (a moment later, after truncating the file first) the
        command's pid: the first record is not the holder's final one, and a read between the second truncate and the second write shows '?'. Ready is the final record."""
        known = [h for h in found if h["name"] != "?" and h["command"] != "?" and h["pid"] is not None and h["pid"] not in set(wrapper_pids)
                 and (name is None or h["name"] == name)]
        return len(found) == count and len(known) == count

    def ready_holders(self, count, slots=3, name=None, seconds=None, state=None, wrappers=()):
        """Wait until `count` holders are READY, then return them. Ready is read from the record the product itself reads (`holders`), never from a timer and never
        from the lock alone: the lock is taken first and the record written after, so a held lock is not ready. `wrappers` are the heavy wrapper processes started for
        the holders: a holder is ready only once its record shows the COMMAND's pid (see holders_ready). Bounded: READY_TRIES reads (or `seconds` / READY_PAUSE); when
        it runs out it fails and says which holder was expected and what was missing."""
        tries = int(seconds / self.READY_PAUSE) if seconds is not None else self.READY_TRIES
        state = state or self.state()
        pids = [w.pid for w in wrappers]
        found = []
        for attempt in range(max(1, tries)):
            found = self.hs.holders(state, slots)
            if self.holders_ready(found, count, name, pids):
                return found
            time.sleep(self.READY_PAUSE)
        self.fail("holder %r not ready after %d reads of its record (%.1f s): expected %d ready holder(s) with the command's pid (not the wrapper pids %s), found %s (a '?' is a held lock whose record is not written yet)" % (
            name, max(1, tries), max(1, tries) * self.READY_PAUSE, count, pids, found))

    def test_s9_a_wrapper_killed_with_sigkill_leaves_a_run_that_the_orphan_bound_stops_and_audits(self):
        """The 2 h 13 min case for real: the watchdog lives in the wrapper, the wrapper is killed, the run keeps the slot; the orphan bound (clock moved past the ceiling)
        stops the sleeping run and reports it with source orphan. A run whose wrapper is alive is not touched by the same check."""
        found = shutil.which("lsof", path=os.environ["PATH"] + os.pathsep + "/usr/sbin" + os.pathsep + "/usr/bin")
        if found is None:
            self.skipTest("lsof is needed to find what holds the slot")
        patch = mock.patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep + os.path.dirname(found)})   # a runner's PATH may lack /usr/sbin
        patch.start()
        self.addCleanup(patch.stop)
        wrapper = self.start("sleep", "300", name="orphan-lane")
        self.assertTrue(self.until(lambda: any(h["pid"] and h["pid"] != wrapper.pid and h["name"] != "?" for h in self.hs.holders(self.state(), 3)), 120), self.why({"w": wrapper}))
        held = self.hs.holders(self.state(), 3)[0]
        later = lambda: time.time() + 4000   # past kind "other" (3600 s)
        self.assertEqual(self.hs.check_orphans(self.state(), 3, None, later, None, io.StringIO()), [], "the wrapper is alive: its run is not an orphan")
        os.kill(wrapper.pid, signal.SIGKILL)
        wrapper.wait(timeout=30)
        self.assertEqual([h["pid"] for h in self.hs.holders(self.state(), 3)], [held["pid"]], "the run still holds the slot")
        told = []
        self.assertEqual(self.hs.check_orphans(self.state(), 3, None, lambda: time.time() + 100, lambda *a: told.append(a), io.StringIO()), [], "under the ceiling: left alone")
        stopped = self.hs.check_orphans(self.state(), 3, None, later, lambda *a: told.append(a), io.StringIO())
        self.assertEqual([(s["source"], s["kind"], s["pid"]) for s in stopped], [("orphan", "other", held["pid"])])
        self.assertEqual((told[0][2], told[0][3]), ("orphan-lane", os.environ.get("FLEET_HEAVY_NAME") or os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"))
        self.assertEqual([t[0] for t in told], ["decided", "told"])
        self.assertTrue(self.until(lambda: not self.hs.holders(self.state(), 3), 30), "the slot is free: the run is gone")

    def test_s1_four_callers_of_three_slots_queue_and_the_fourth_runs_when_one_frees(self):
        """The integration check with real processes (the queue logic itself is tested deterministically in TestHeavyQueueLogic)."""
        procs = {tag: self.start(*self.waiter(tag), name="who-%s" % tag) for tag in "abcd"[:3]}
        ok = self.until(lambda: len(self.started()) == 3, self.HOLD_CEILING_SECONDS)
        self.assertTrue(ok, "3 commands started\n" + (self.why(procs) if not ok else ""))
        # the three locks are really held now (the kernel's own answer, not a file): the fourth cannot take one, whatever the machine's speed
        try:
            self.ready_holders(3, seconds=self.HOLD_CEILING_SECONDS, wrappers=list(procs.values()))   # the three locks held AND their records written (the kernel's own answer, not a timer)
        except AssertionError as err:
            self.fail("3 of 3 slots held before the fourth starts\n%s\n%s" % (err, self.why(procs)))
        fourth = self.start(*self.waiter("d"), name="who-d")
        lines, queued = [], threading.Event()

        def pump():   # event driven: the wrapper's stderr is read as it arrives, so no poll interval or start-up time decides anything
            for line in fourth.stderr:
                lines.append(line)
                if "waiting for a slot" in line:
                    queued.set()
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        self.assertTrue(queued.wait(self.HOLD_CEILING_SECONDS), "the fourth wrapper (exit %r) never said it was waiting; holders: %s; started: %s" % (
            fourth.poll(), self.hs.holders(self.state(), 3), self.started()))
        self.assertEqual(len(self.started()), 3, "at most 3 run at once")
        (self.home / "release.a").write_text("")
        self.assertTrue(self.until(lambda: "started.d" in self.started()), "the fourth runs once a slot is free")
        for tag in "bcd":
            (self.home / ("release." + tag)).write_text("")
        for proc in list(procs.values()) + [fourth]:
            proc.wait(timeout=90)
        reader.join(timeout=90)
        err = "".join(lines)
        self.assertIn("heavy: waiting for a slot (3 of 3 busy", err)
        self.assertRegex(err, r"who-[abc] sh")

    def test_the_diagnosis_of_a_failed_precondition_shows_a_wrapper_that_ran_without_a_slot(self):
        """A wrapper whose state directory is refused runs its command with NO slot (`heavy: no slot (...)`): the command starts, no slot is held."""
        (self.dir / "fleet-switchboard").write_text("a file where the state directory should be")
        proc = self.start("sh", "-c", 'touch "%s/started.z"' % self.home, name="who-z")
        self.assertEqual(proc.wait(timeout=self.HOLD_CEILING_SECONDS), 0)
        message = self.why({"z": proc}).replace(str(self.dir), "<dir>")
        self.assertEqual(message.count("heavy: no slot"), 1, message)
        self.assertEqual(message.count("exit 0"), 1, message)
        self.assertEqual(message.count("holders: []"), 1, message)
        self.assertEqual(message.count("started: ['started.z']"), 1, message)

    def test_the_ceiling_of_the_integration_preconditions_is_300_seconds(self):
        self.assertEqual(self.HOLD_CEILING_SECONDS, 300)
        source = inspect.getsource(self.test_s1_four_callers_of_three_slots_queue_and_the_fourth_runs_when_one_frees)
        self.assertEqual(source.count("self.HOLD_CEILING_SECONDS"), 3, "both preconditions and the queued wait use it")

    def test_s2_a_holder_killed_with_its_whole_group_frees_its_slot_by_itself(self):
        holder = self.start(*self.waiter("h"), slots=1, name="doomed")
        self.assertTrue(self.until(lambda: "started.h" in self.started()))
        self.assertEqual([h["name"] for h in self.ready_holders(1, slots=1, name="doomed", wrappers=[holder])], ["doomed"])
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=90)
        waiter = self.start("sh", "-c", 'touch "%s/second"' % self.home, slots=1)
        self.assertEqual(waiter.wait(timeout=90), 0)
        self.assertTrue((self.home / "second").exists(), "no cleanup was needed")
        self.assertEqual(self.hs.holders(self.state(), 1), [])

    def test_s3_stdin_stdout_and_the_exit_status_pass_through_and_a_signal_reaches_the_command(self):
        proc = self.start("sh", "-c", "cat; exit 3", stdin=subprocess.PIPE)
        out, err = proc.communicate("some data\n", timeout=90)
        self.assertEqual((out, proc.returncode), ("some data\n", 3))
        pidfile = self.home / "pid"
        proc = self.start("sh", "-c", 'echo $$ > "%s"; exec sleep 60' % pidfile)
        self.assertTrue(self.until(pidfile.exists))
        child = int(pidfile.read_text())
        self.assertTrue(self.until(lambda: any(h["pid"] == child for h in self.hs.holders(self.state(), 3))), "the wrapper has recorded its command")
        proc.send_signal(signal.SIGTERM)   # to the wrapper alone: it forwards it
        self.assertEqual(proc.wait(timeout=90), 128 + signal.SIGTERM)
        self.assertTrue(self.until(lambda: not self.alive(child)), "the command was stopped and reaped: nothing is left behind")

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)   # a zombie of ours would still answer kill(0)
            return done == 0
        except ChildProcessError:
            return True

    def test_s3_the_wrapper_reaps_its_child_and_leaves_no_zombie(self):
        """In this very process (so the wrapper IS the parent): when `run` returns, its child has already been waited for, not left a zombie."""
        pidfile = self.home / "short-pid"
        err = io.StringIO()
        status = self.hs.run(["sh", "-c", 'echo $$ > "%s"' % pidfile], self.state(), 1, 10, name="inproc", environ=self.env(), out=err)
        self.assertEqual(status, 0, err.getvalue())
        pid = int(pidfile.read_text())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)   # nothing of ours is left to reap: a zombie would answer here

    @staticmethod
    def state_of(pid):
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return out or None

    def test_s4_the_command_runs_under_taskpolicy_when_it_exists_and_nice_when_it_does_not(self):
        fake = self.home / "fakebin"
        fake.mkdir()
        record = self.home / "record"
        (fake / "taskpolicy").write_text('#!/bin/sh\necho "$1" > "%s"\nshift\nexec "$@"\n' % record)
        os.chmod(str(fake / "taskpolicy"), 0o755)
        env = self.env(PATH="%s:%s" % (fake, os.environ["PATH"]))
        self.assertEqual(self.start("true", env=env).wait(timeout=90), 0)
        self.assertEqual(record.read_text().strip(), "-b", "run as taskpolicy -b <command>")
        self.assertEqual(self.hs.command_prefix(lambda n: "/x/taskpolicy" if n == "taskpolicy" else "/x/nice"), ["/x/taskpolicy", "-b"])
        self.assertEqual(self.hs.command_prefix(lambda n: "/x/nice" if n == "nice" else None), ["/x/nice", "-n", "10"])
        self.assertEqual(self.hs.command_prefix(lambda n: None), [])
        # and with no taskpolicy the real nice runs it at a lower priority
        stub = self.home / "nobin"
        stub.mkdir()
        for tool in ("nice", "sh", "ps"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, str(stub / tool))
        out = self.home / "niceness"
        proc = self.start("sh", "-c", 'ps -o nice= -p $$ > "%s"' % out, env=self.env(PATH=str(stub)))
        self.assertEqual(proc.wait(timeout=90), 0, proc.stderr.read())
        self.assertEqual(int(out.read_text().strip()), min(20, os.nice(0) + 10), "ten above this process's own niceness")

    def test_s5_the_holder_list_names_who_holds_a_slot_and_a_free_slot_says_nothing(self):
        proc = self.start("sleep", "5", slots=2, name="the-coder")
        (entry,) = self.ready_holders(1, slots=2, name="the-coder", wrappers=[proc])
        self.assertEqual((entry["name"], entry["command"]), ("the-coder", "sleep"))
        self.assertIsInstance(entry["pid"], int)
        self.assertLessEqual(abs(entry["since"] - time.time()), 30)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=90)
        self.assertTrue(self.until(lambda: self.hs.holders(self.state(), 2) == []))
        mode = os.stat(str(self.state() / "heavy-slot-0.lock")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_s6_past_the_wait_limit_it_exits_75_and_does_not_run_the_command(self):
        holder = self.start("sleep", "30", slots=1, name="busy-one")
        self.ready_holders(1, slots=1, name="busy-one", wrappers=[holder])
        marker = self.home / "marker"
        late = self.start("touch", str(marker), slots=1, wait_max=1)
        self.assertEqual(late.wait(timeout=90), 75)
        self.assertIn("heavy: no slot after", late.stderr.read())
        self.assertFalse(marker.exists())

    def test_s7_the_suite_takes_a_slot_unless_it_is_off_inside_one_or_in_ci(self):
        self.assertEqual(self.hs.should_take_slot({}), "taken")
        self.assertEqual(self.hs.should_take_slot({"FLEET_HEAVY": "off"}), "off")
        self.assertEqual(self.hs.should_take_slot({"CI": "true"}), "off")
        self.assertEqual(self.hs.should_take_slot({"FLEET_HEAVY_SLOT": "1"}), "inside")
        env = {"XDG_STATE_HOME": str(self.dir)}
        wrapped = self.hs.wrap_argv(["TestX"], env, "/py", "/sw", "/dir/test-switchboard")
        self.assertEqual(wrapped, ["/py", "/sw", "heavy", "--name", "test-switchboard", "--", "/py", "/dir/test-switchboard", "TestX"])
        self.assertIsNone(self.hs.wrap_argv([], dict(env, FLEET_HEAVY="off"), "/py", "/sw", "/s"))
        self.assertIsNone(self.hs.wrap_argv([], dict(env, FLEET_HEAVY_SLOT="0"), "/py", "/sw", "/s"))

    def test_s7_a_state_directory_that_cannot_be_written_runs_the_suite_unslotted_with_one_line(self):
        blocked = self.home / "blocked"
        blocked.write_text("a file, so no directory can be made under it")
        err = io.StringIO()
        self.assertIsNone(self.hs.wrap_argv([], {"XDG_STATE_HOME": str(blocked)}, "/py", "/sw", "/s", err=err))
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("running without a slot", err.getvalue())

    def test_u1_a_wrapper_killed_alone_leaves_the_slot_busy_exactly_while_its_command_runs(self):
        pidfile = self.home / "orphan-pid"
        wrapper = self.start("sh", "-c", 'echo $$ > "%s"; while [ ! -e "%s/orphan-release" ]; do sleep 0.05; done' % (pidfile, self.home), slots=1)
        self.assertTrue(self.until(pidfile.exists))
        self.assertTrue(self.until(lambda: any(h["pid"] == int(pidfile.read_text()) for h in self.hs.holders(self.state(), 1))))
        os.kill(wrapper.pid, signal.SIGKILL)   # the wrapper only: the command goes on
        wrapper.wait(timeout=90)
        self.assertEqual(len(self.hs.holders(self.state(), 1)), 1, "busy while the command runs")
        (self.home / "orphan-release").write_text("")
        self.assertTrue(self.until(lambda: self.hs.holders(self.state(), 1) == [], 90), "free once it ends")

    def test_u2_the_command_is_not_run_through_a_shell_and_keeps_its_environment_and_directory(self):
        work = self.home / "cwd"
        work.mkdir()
        proc = self.start(sys.executable, "-c", "import sys, os; print(sys.argv[1:], os.environ['X_MARK'], os.getcwd())", "a b;c", "$HOME",
                          env=self.env(X_MARK="kept"), cwd=str(work))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip(), "['a b;c', '$HOME'] kept %s" % os.path.realpath(str(work)))

    def test_a_signal_while_waiting_a_zero_wait_limit_and_a_command_that_cannot_start(self):
        holder = self.start("sleep", "30", slots=1)
        self.ready_holders(1, slots=1, name="t", wrappers=[holder])
        waiting = self.start("true", slots=1)
        seen = ""
        end = time.time() + 90
        while "waiting for a slot" not in seen and time.time() < end:   # the wrapper says so once it is queued
            if select.select([waiting.stderr], [], [], 0.2)[0]:
                seen += os.read(waiting.stderr.fileno(), 4096).decode()
        self.assertIn("waiting for a slot", seen)
        waiting.send_signal(signal.SIGTERM)
        self.assertEqual(waiting.wait(timeout=90), 128 + signal.SIGTERM, "a wrapper that is still waiting just ends")
        zero = self.start("true", slots=1, wait_max=0)
        self.assertEqual(zero.wait(timeout=90), 75)
        not_executable = self.home / "notexec"
        not_executable.write_text("#!/bin/sh\n")   # present, not executable
        self.assertEqual(self.start(str(not_executable), slots=2).wait(timeout=90), 127)

    def test_what_is_shown_about_a_holder_never_carries_a_terminal_escape(self):
        state = self.state()
        (state / "heavy-slot-0.lock").write_text(json.dumps({"name": "\x1b[2Jevil\x9b31m", "command": "a\nb", "pid": 1, "since": 1}))
        fd = os.open(str(state / "heavy-slot-0.lock"), os.O_RDWR)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            (entry,) = self.hs.holders(state, 1)
        finally:
            os.close(fd)
        shown = entry["name"] + entry["command"]
        self.assertEqual([c for c in shown if ord(c) < 32 or 0x7f <= ord(c) <= 0x9f], [])
        self.assertIn("evil", shown)
        self.assertEqual(self.hs.holder_name({"FLEET_HEAVY_NAME": "x\x1b[0my"}), "x [0my")

    def test_a_bad_report_interval_falls_back_to_the_default(self):
        out = self.start("true", env=self.env(FLEET_HEAVY_REPORT_SECONDS="soon"))
        self.assertEqual(out.wait(timeout=90), 0)

    def test_a_missing_or_unwritable_state_directory_runs_the_command_without_a_slot_with_one_line(self):
        marker = self.home / "ran"
        blocked = self.home / "blocked"
        blocked.write_text("a file: no directory can be made under it")
        proc = self.start("sh", "-c", 'touch "%s"' % marker, env=self.env(XDG_STATE_HOME=str(blocked)))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(marker.exists(), "the command ran")
        self.assertEqual(err.count("heavy: no slot ("), 1, err)
        self.assertNotIn("Traceback", err)
        marker.unlink()
        readonly = self.home / "readonly"
        readonly.mkdir()
        os.chmod(str(readonly), 0o500)
        self.addCleanup(os.chmod, str(readonly), 0o700)
        proc = self.start("sh", "-c", 'touch "%s"' % marker, env=self.env(XDG_STATE_HOME=str(readonly)))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(marker.exists())
        self.assertNotIn("Traceback", err)
        # and in process: a directory that does not exist at all
        errors = io.StringIO()
        self.assertEqual(self.hs.run(["true"], str(self.home / "no" / "such" / "dir"), 1, 5, environ=self.env(), out=errors), 0)
        self.assertEqual(errors.getvalue().count("heavy: no slot ("), 1)

    def test_nested_heavy_inside_a_slot_takes_no_second_slot_so_four_levels_of_three_slots_finish(self):
        inner = ['"%s"' % sys.executable, '"%s"' % SWITCHBOARD, "heavy", "--slots", "3", "--wait-max", "20", "--"]
        script = "echo level-4"
        for level in (3, 2, 1):
            script = " ".join(inner) + " sh -c '%s'" % script.replace("'", "'\\''")
        proc = self.start("sh", "-c", script, slots=3, wait_max=20)
        out, err = proc.communicate(timeout=90)
        self.assertEqual((proc.returncode, out.strip()), (0, "level-4"), err)
        self.assertEqual(err.count("already inside a slot"), 3)
        self.assertNotIn("no slot after", err)

    def test_heavy_off_runs_the_command_directly_with_no_slot_and_no_line(self):
        out = self.home / "off-out"
        proc = self.start("sh", "-c", 'echo "$FLEET_HEAVY_SLOT" > "%s"' % out, env=self.env(FLEET_HEAVY="off"))
        stdout, err = proc.communicate(timeout=90)
        self.assertEqual((proc.returncode, err), (0, ""))
        self.assertEqual(out.read_text().strip(), "", "no slot variable: it ran as it is")
        self.assertFalse(list(self.state().glob("heavy-slot-*.lock")), "no slot file was even made")

    def test_the_cpu_line_shows_the_slots_in_use_through_the_real_heavy_module(self):
        cg = sb.load_cpu_guard()
        self.assertEqual(cg.slots_text({}, self.state()), "heavy slots 0/3")
        holder = self.start("sleep", "30", name="the-coder")
        self.ready_holders(1, name="the-coder", wrappers=[holder])
        line = cg.slots_text({}, self.state())
        self.assertRegex(line, r"^heavy slots 1/3 \(the-coder sleep \d+[ms]\)$")
        self.assertEqual(cg.slots_text({"heavy": {"slots": 5}}, self.state())[:15], "heavy slots 1/5")
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=90)
        self.assertTrue(self.until(lambda: cg.slots_text({}, self.state()) == "heavy slots 0/3"), "a killed holder is not shown")

    def test_a_missing_command_and_misuse_are_said_plainly(self):
        self.assertEqual(self.start("no-such-command-here").wait(timeout=90), 127)
        for words in ([], ["--slots", "0", "--", "true"], ["--slots", "x", "--", "true"], ["--wait-max", "-1", "--", "true"]):
            proc = subprocess.Popen([sys.executable, str(SWITCHBOARD), "heavy"] + words, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.communicate(timeout=90)
            self.assertEqual(proc.returncode, sb.EXIT_USAGE, words)

    def test_the_config_key_is_validated(self):
        for ok in (None, {}, {"slots": 1}, {"slots": 8}):
            self.hs.settings(ok)
        for bad in (3, {"slots": 0}, {"slots": "3"}, {"slots": True}, {"slots": 100}, {"cores": 2}):
            with self.assertRaises(self.hs.HeavyError):
                self.hs.settings(bad)
        self.assertEqual(self.hs.settings(None), 3)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "heavy": {"slots": 0}})

    def test_the_documents_name_the_command_and_the_agent_definitions_say_it_once(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("fleet-switchboard heavy", text)
        line = "full suites and browser/e2e runs go through `fleet-switchboard heavy -- <cmd>`"
        for path in ("agents/opencode/coder.md", "agents/opencode/orchestrator.md", "skills/fleet-coordination/SKILL.md"):
            found = (REPO / path).read_text()
            self.assertEqual(found.lower().count(line), 1, path)


class TestHeavySlotsReadiness(StandInPids, TestHeavySlots):
    """#688: the readiness helper of TestHeavySlots. Its own checks run here (the inherited tests below are removed: they run once, in TestHeavySlots, and the three
    that failed in CI run again in TestHeavySlotsSlowStart)."""

    def test_the_helper_reads_the_holder_record_the_product_reads_and_the_class_has_no_bare_sleep_wait(self):
        helper = inspect.getsource(TestHeavySlots.ready_holders)
        self.assertIn("self.hs.holders(", helper)
        source = inspect.getsource(TestHeavySlots)
        self.assertEqual(source.count("            time.sleep("), 2, "only the two bounded waits (until, ready_holders) sleep: no test waits by a bare sleep")
        self.assertEqual(TestHeavySlots.READY_TRIES * TestHeavySlots.READY_PAUSE, 60.0)

    def test_a_holder_whose_lock_is_held_but_record_is_not_written_is_not_ready_and_a_never_ready_holder_fails_within_the_bound_naming_it(self):
        import fcntl
        state = self.state()
        fd = os.open(str(state / "heavy-slot-0.lock"), os.O_RDWR | os.O_CREAT, 0o600)   # held, with no record: what the product lists as '?'
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual([h["name"] for h in self.hs.holders(state, 3)], ["?"])
        begun = time.time()
        with self.assertRaises(AssertionError) as caught:
            self.ready_holders(1, name="slow-one", seconds=1)
        self.assertLess(time.time() - begun, 2.5)
        self.assertIn("slow-one", str(caught.exception))
        self.assertIn("not ready", str(caught.exception))
        self.assertIn("'?'", str(caught.exception))
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"name": "slow-one", "command": "sleep", "pid": 1, "since": time.time()}).encode())
        (entry,) = self.ready_holders(1, name="slow-one", seconds=1)
        self.assertEqual((entry["name"], entry["command"]), ("slow-one", "sleep"))

    def test_the_readiness_decision_is_the_final_record_not_the_lock_and_not_the_wrappers_first_record(self):
        """Deterministic: no process, no clock. The cases a lock-only (or first-record) readiness would accept and this one must not."""
        def held(name, command, pid):
            return {"slot": 0, "name": name, "command": command, "pid": pid, "since": 1}
        ready = self.holders_ready
        self.assertFalse(ready([held("?", "?", None)], 1, "the-coder", [self.px[100]]), "the lock is held, no record yet")
        self.assertFalse(ready([held("the-coder", "sleep", self.px[100])], 1, "the-coder", [self.px[100]]), "the wrapper's own first record: the second write (truncate, then the command's pid) is still to come")
        self.assertFalse(ready([held("?", "?", None)], 1, None, []), "a '?' holder is never ready, with or without a name asked for")
        self.assertFalse(ready([held("other", "sleep", self.px[200])], 1, "the-coder", [self.px[100]]), "the wrong holder")
        self.assertFalse(ready([], 1, "the-coder", [self.px[100]]), "no holder")
        self.assertFalse(ready([held("the-coder", "sleep", self.px[200]), held("?", "?", None)], 2, None, [self.px[100]]), "one of two is not ready")
        self.assertTrue(ready([held("the-coder", "sleep", self.px[200])], 1, "the-coder", [self.px[100]]), "the command's pid: the final record")

    def test_the_helper_waits_through_the_wrappers_first_record_until_the_second_is_written(self):
        """The product's own two records, in order and with a deterministic gap: the helper returns only after the second."""
        import fcntl
        state = self.state()
        fd = os.open(str(state / "heavy-slot-0.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.hs._record(fd, "the-coder", self.px[100], "sleep", time.time())   # the wrapper's first record: pid 100 is the wrapper
        wrapper = mock.Mock(pid=self.px[100])
        with self.assertRaises(AssertionError) as caught:
            self.ready_holders(1, name="the-coder", seconds=0.5, wrappers=[wrapper])
        self.assertIn("not ready", str(caught.exception))
        self.hs._record(fd, "the-coder", self.px[200], "sleep", time.time())   # the second: the command's pid
        (entry,) = self.ready_holders(1, name="the-coder", seconds=0.5, wrappers=[wrapper])
        self.assertEqual(entry["pid"], self.px[200])


class TestHeavySlotsSlowStart(TestHeavySlots):
    """#688: the three tests that failed in CI, run again with every holder one second slow to write its record (its lock is held at once)."""
    START_DELAY = 1


class FakeActions:
    """A fake GitHub for `reds`: a pull request, runs per event, an artifact list and artifact zips. Records every path it is asked (all GETs)."""

    REPO = "acme/widget"

    def __init__(self):
        self.calls, self.argvs, self.runs, self.artifacts, self.zips = [], [], {}, {}, {}
        self.pull = {"head": {"sha": "b" * 40}, "base": {"repo": {"default_branch": "main"}}}
        self.fail_paths = set()
        self.next_id = 100

    WORKFLOWS = {"A": (11, ".github/workflows/a-ci.yml"), "B": (22, ".github/workflows/b-style.yml")}

    def pr_run(self, names, complete=True, artifact=True, workflow="A", created="2027-01-15T08:00:00Z", **more):
        return self.add_run("pull_request", "feature", names, complete, created, artifact, sha="b" * 40, workflow=workflow, **more)

    def add_run(self, event, branch, names, complete=True, created="2027-01-15T07:00:00Z", artifact=True, sha=None, expired=False, raw=None, workflow="A", no_workflow_id=False):
        self.next_id += 1
        wid, path = self.WORKFLOWS[workflow]
        run = {"id": self.next_id, "event": event, "head_branch": branch, "head_sha": sha or ("%07d" % self.next_id) + "c" * 33, "created_at": created, "status": "completed",
               "workflow_id": wid, "name": "Workflow " + workflow, "path": path}
        if no_workflow_id:
            del run["workflow_id"]
        self.runs.setdefault(event, []).append(run)
        if artifact:
            self.zips[self.next_id + 5000] = raw if raw is not None else _artifact_zip(names, complete, self.next_id)
            self.artifacts[self.next_id] = {"id": self.next_id + 5000, "expired": expired, "workflow_run": {"id": self.next_id}, "size_in_bytes": len(self.zips[self.next_id + 5000])}
        return self.next_id

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        path = argv[-1]
        self.calls.append(path)
        self.argvs.append(argv)
        if len(argv) != 3 or argv[1] != "api" or any(a.startswith("-") for a in argv[2:]):
            raise AssertionError("fake github serves `gh api <path>`, a plain GET; got %r" % argv)
        done = lambda text, code=0, err="": subprocess.CompletedProcess(argv, code, text, err)
        if any(bad in path for bad in self.fail_paths):
            return done("", 1, "gh: HTTP 500")
        base = "repos/%s/" % self.REPO
        if path == base + "pulls/12":
            return done(json.dumps(self.pull))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        found = re.fullmatch(re.escape(base) + r"actions/workflows/([^/?]+)/runs", urllib.parse.urlparse(path).path)
        if found:   # one workflow's own runs, by id or by file name
            which = urllib.parse.unquote(found.group(1))
            event = query["event"][0]
            runs = [r for r in self.runs.get(event, []) if (getattr(self, "leak_foreign", False) or str(r.get("workflow_id")) == which or r["path"].endswith("/" + which)) and ("branch" not in query or r["head_branch"] == query["branch"][0])]
            return done(json.dumps({"workflow_runs": sorted(runs, key=lambda r: r["created_at"], reverse=True)[:int(query.get("per_page", ["30"])[0])]}))
        if path.startswith(base + "actions/runs?"):
            if "head_sha" not in query:
                raise AssertionError("reds must not list the repo's runs by event alone: that was the defect (%r)" % path)
            event = query["event"][0]
            runs = [r for r in self.runs.get(event, []) if ("head_sha" not in query or r["head_sha"] == query["head_sha"][0]) and ("branch" not in query or r["head_branch"] == query["branch"][0])]
            return done(json.dumps({"workflow_runs": sorted(runs, key=lambda r: r["created_at"], reverse=True)[:int(query.get("per_page", ["30"])[0])]}))
        if path.startswith(base + "actions/artifacts?"):
            return done(json.dumps({"artifacts": list(self.artifacts.values())}))
        found = re.fullmatch(re.escape(base) + r"actions/artifacts/(\d+)/zip", path)
        if found:
            return subprocess.CompletedProcess(argv, 0, self.zips[int(found.group(1))], b"")
        raise AssertionError("fake github: no route for %r" % path)


class TestReds(Base):
    """`reds OWNER/REPO#N` (ask #342): classify from failing-tests artifacts: main's latest run, flaky (2 of main's last 5 usable runs), or new."""

    def setUp(self):
        super().setUp()
        self.hs = sb.load_reds()
        self.fake = FakeActions()
        self.state = self.home / "reds-state"
        self.state.mkdir(mode=0o700)
        self.clock = [1_800_000_000.0]

    def now(self):
        return self.clock[0]

    def created(self, minutes_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock[0] - minutes_ago * 60))

    class Reader:
        def __init__(self, runner, repo):
            self.runner, self.repo = runner, repo

        def json(self, path):
            done = self.runner(["gh", "api", path])
            if done.returncode != 0:
                raise sb.load_reds().RedsError(done.stderr)
            return json.loads(done.stdout)

        def zip(self, path):
            done = self.runner(["gh", "api", path])
            if done.returncode != 0:
                raise sb.load_reds().RedsError(done.stderr)
            return done.stdout

    def run_reds(self, events=("push",), cache=True, workflow=None):
        out = io.StringIO()
        code = self.hs.run(self.Reader(self.fake, FakeActions.REPO), FakeActions.REPO, 12, list(events), self.state if cache else None, self.now, out, workflow)
        return code, out.getvalue()

    def main_runs(self, histories, event="push", branch="main"):
        """histories: failing-name lists, newest first."""
        for position, names in enumerate(histories):
            self.fake.add_run(event, branch, names, created=self.created(10 + position * 60))

    def test_s1_a_name_on_main_s_latest_run_is_main_two_of_five_is_flaky_and_the_rest_is_new(self):
        self.fake.pr_run(["t/a.test::a", "t/b.test::b", "t/c.test::c"])
        self.main_runs([["t/a.test::a"], ["t/b.test::b"], [], ["t/b.test::b"], []])
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(len([l for l in lines if l.startswith("    t/a.test::a")]), 1)
        self.assertIn("also failing on main (1):\n    t/a.test::a (1 of 5 main runs)", out)
        self.assertIn("flaky on main (1):\n    t/b.test::b (2 of 5 main runs)", out)
        self.assertIn("new (1):\n    t/c.test::c", out)
        self.assertRegex(out, r"main's latest run: push [0-9c]{7} \d+m ago \(workflow Workflow A; reference events: push\)")

    def test_s2_one_failure_in_five_is_not_flaky_two_is(self):
        self.fake.pr_run(["x::once", "y::twice"])
        self.main_runs([[], ["x::once", "y::twice"], [], ["y::twice"], []])
        code, out = self.run_reds()
        self.assertIn("flaky on main (1):\n    y::twice (2 of 5 main runs)", out)
        self.assertIn("new (1):\n    x::once (1 of 5 main runs)", out)

    def test_s3_runs_without_an_artifact_incomplete_or_expired_are_unknown_and_never_passing(self):
        self.fake.pr_run(["f::flaky-one"])
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(10))
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(70))
        self.fake.add_run("push", "main", [], created=self.created(130), artifact=False)
        self.fake.add_run("push", "main", ["f::flaky-one"], complete=False, created=self.created(190))
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(250), expired=True)
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 2 usable, 3 unknown", out)
        self.assertIn("flaky on main", out)
        self.assertNotIn("flaky-one (2 of 5", out)
        self.assertIn("(2 of 2 main runs)", out)

    def test_s3_an_unknown_latest_run_means_failing_on_main_cannot_be_said(self):
        self.fake.pr_run(["a::fails"])
        self.fake.add_run("push", "main", ["a::fails"], created=self.created(10), artifact=False)
        self.fake.add_run("push", "main", ["a::fails"], created=self.created(70))
        code, out = self.run_reds()
        self.assertIn('UNKNOWN (no artifact), so "failing on main" cannot be said', out)
        self.assertNotIn("also failing on main (1)", out)

    def test_s4_names_are_stripped_bounded_and_at_most_two_thousand_are_read(self):
        names = ["bad\x1b[2Jname\x9b\u2028::x", "L" * 500] + ["n%d" % i for i in range(3000)]
        self.fake.pr_run(names)
        self.main_runs([[]])
        code, out = self.run_reds()
        self.assertEqual([c for c in out if (ord(c) < 32 and c != "\n") or 0x7f <= ord(c) <= 0x9f or c in "\u2028\u2029"], [])
        self.assertLessEqual(max(len(line) for line in out.splitlines()), 240)
        self.assertIn("2000 failing in the pull request's run", out)

    def test_s5_cold_and_warm_calls_stay_within_the_limits_and_nothing_is_written(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"], [], ["a::t"], [], ["a::t"]])
        self.run_reds()
        cold = len(self.fake.calls)
        self.assertLessEqual(cold, 12, self.fake.calls)
        del self.fake.calls[:]
        self.run_reds()
        self.assertLessEqual(len(self.fake.calls), 3, self.fake.calls)
        self.assertTrue(self.fake.argvs and all(a[1] == "api" and len(a) == 3 for a in self.fake.argvs), "every call is a plain `gh api <path>` GET")
        # two reference events: 14 and 4
        second = FakeActions()
        second.pr_run(["a::t"])
        for position in range(3):
            second.add_run("merge_group", "gh-readonly-queue/main/pr-%d-abc" % position, ["a::t"], created=self.created(10 + position * 60))
        for position in range(2):
            second.add_run("schedule", "main", ["a::t"], created=self.created(40 + position * 60))
        shutil.rmtree(str(self.state))
        self.state.mkdir(mode=0o700)
        self.fake = second
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(second.calls), 14, second.calls)
        del second.calls[:]
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(second.calls), 4, second.calls)

    def test_s5_without_a_cache_every_call_is_made_again(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]] * 5)
        self.run_reds(cache=False)
        first = len(self.fake.calls)
        self.run_reds(cache=False)
        self.assertEqual(len(self.fake.calls), 2 * first)

    def test_s6_a_repo_with_only_merge_group_and_schedule_runs_uses_them_and_the_default_uses_push(self):
        self.fake.pr_run(["a::t", "b::u"])
        self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-9-aaa", ["a::t"], created=self.created(10))
        self.fake.add_run("merge_group", "gh-readonly-queue/release/pr-9-aaa", ["b::u"], created=self.created(5))   # another branch's queue: not main
        self.fake.add_run("schedule", "main", ["b::u"], created=self.created(70))
        self.fake.add_run("schedule", "feature", ["b::u"], created=self.created(1))
        code, out = self.run_reds(events=("merge_group", "schedule"))
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out)
        self.assertRegex(out, r"main's latest run: merge_group [0-9c]{7} ")
        self.assertIn("also failing on main (1):\n    a::t", out)
        code, out = self.run_reds(events=("push",))
        self.assertIn("main's latest run: none found", out)
        self.assertEqual(self.hs.DEFAULT_EVENTS, ["push"])

    def test_s7_full_name_first_then_a_unique_leaf_marked_and_a_duplicate_leaf_is_never_guessed(self):
        self.fake.pr_run(["a/x.test::exact", "p/y.test::renamed", "q/z.test::dup", "m::lonely"])
        self.main_runs([["a/x.test::exact", "other/y.test::renamed", "r/one.test::dup", "s/two.test::dup", "lonely"]])
        code, out = self.run_reds()
        self.assertIn("a/x.test::exact (1 of 1 main runs)\n", out)
        self.assertIn("p/y.test::renamed (1 of 1 main runs); matched by name only", out)
        self.assertIn("m::lonely (1 of 1 main runs); matched by name only", out)
        self.assertIn("new (1):\n    q/z.test::dup\n", out, "a leaf that is not unique stays unmatched")

    def test_s8_the_prs_own_incomplete_flag_never_judges_main(self):
        self.fake.pr_run(["a::t"], complete=False)
        self.main_runs([["a::t"], []])
        code, out = self.run_reds()
        self.assertIn("its list is INCOMPLETE", out)
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out)
        self.assertIn("also failing on main (1)", out)

    def test_u1_a_failed_read_says_so_classifies_nothing_and_exits_1(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]])
        for bad in ("pulls/12", "actions/runs?head_sha", "actions/workflows/11/runs", "actions/artifacts?name"):
            fake = self.fake
            fake.fail_paths = {bad}
            code, out = self.run_reds(cache=False)
            self.assertEqual(code, 1, bad)
            self.assertIn("nothing was classified", out)
            self.assertNotIn("also failing on main", out)
        self.fake.fail_paths = set()

    def test_a_garbage_artifact_a_pr_without_a_run_and_a_pr_without_an_artifact_are_said(self):
        self.fake.pr_run(["a::t"], artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 3, "unknown, not classified")
        self.assertIn("no failing-tests artifact on the pull request's run", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)   # the fakes reuse run ids
        broken = FakeActions()
        broken.add_run("pull_request", "feature", [], created=self.created(1), sha="b" * 40, raw=b"not a zip")
        self.fake = broken
        code, out = self.run_reds()
        self.assertIn("unknown, nothing was classified", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        empty = FakeActions()
        self.fake = empty
        code, out = self.run_reds()
        self.assertIn("no completed pull_request run", out)

    def unknown_case(self, build):
        self.fake = FakeActions()
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        self.fake.pr_run(["a::t", "b::u"])
        build(self.fake)
        return self.run_reds()

    def assert_cannot_classify(self, code, out, why):
        self.assertEqual(code, self.hs.EXIT_UNKNOWN, out)
        self.assertIn("no usable reference run (%s): cannot classify" % why, out)
        self.assertIn("unknown (2):\n    a::t\n    b::u", out)
        self.assertNotIn("new (", out, "a red is never called new on no evidence")
        self.assertNotIn("also failing on main", out)
        self.assertNotIn("flaky on main", out)

    def test_with_no_usable_reference_run_the_reds_are_unknown_never_new(self):
        code, out = self.unknown_case(lambda fake: None)
        self.assert_cannot_classify(code, out, "none found")
        code, out = self.unknown_case(lambda fake: [fake.add_run("push", "main", ["a::t"], created=self.created(10 + n * 60), expired=True) for n in range(3)])
        self.assert_cannot_classify(code, out, "no artifact: expired or never uploaded")
        code, out = self.unknown_case(lambda fake: [fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(10 + n * 60)) for n in range(3)])
        self.assert_cannot_classify(code, out, "incomplete: not all tests ran")
        code, out = self.unknown_case(lambda fake: fake.add_run("push", "main", [], created=self.created(10), raw=b"not a zip"))
        self.assert_cannot_classify(code, out, "unreadable: the artifact is not a zip")
        code, out = self.unknown_case(lambda fake: (fake.add_run("push", "main", [], created=self.created(10), artifact=False),
                                                    fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(70))))
        self.assert_cannot_classify(code, out, "incomplete: not all tests ran; no artifact: expired or never uploaded")

    def test_an_unknown_latest_run_with_older_complete_runs_classifies_by_those_alone_and_says_so(self):
        def build(fake):
            fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(10))   # latest: incomplete
            fake.add_run("push", "main", ["a::t"], created=self.created(70))
            fake.add_run("push", "main", ["a::t"], created=self.created(130))
        code, out = self.unknown_case(build)
        self.assertEqual(code, 0, out)
        self.assertIn("main's latest run is unknown: classified by the 2 older complete run(s) only; \"failing on main\" is not said", out)
        self.assertIn("flaky on main (1):\n    a::t (2 of 2 main runs)", out)
        self.assertIn("new (1):\n    b::u\n", out, "absent from every COMPLETE run: new is allowed here")
        self.assertNotIn("also failing on main (1)", out)

    def test_a_mix_of_unknown_and_one_complete_run_classifies_from_the_complete_one(self):
        def build(fake):
            fake.add_run("push", "main", [], created=self.created(10), artifact=False)
            fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(70))
            fake.add_run("push", "main", ["a::t"], created=self.created(130))
        code, out = self.unknown_case(build)
        self.assertEqual(code, 0, out)
        self.assertIn("1 older complete run(s) only", out)
        self.assertIn("new (2):\n    a::t (1 of 1 main runs)\n    b::u\n", out)
        self.assertIn("main runs: 3 listed (workflow Workflow A), 1 usable, 2 unknown", out)

    # -- the workflow of the artifact (fix to #342: main's runs are the runs of the workflow that holds the pull request's artifact)

    def test_s1_only_the_artifact_workflow_s_main_runs_are_listed_and_another_workflow_is_never_unknown(self):
        self.fake.pr_run(["a::t", "b::u"])
        for position in range(5):
            self.fake.add_run("push", "main", ["a::t"] if position % 2 == 0 else [], created=self.created(10 + position * 60), workflow="A")
            self.fake.add_run("push", "main", [], created=self.created(5 + position * 60), workflow="B", artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 5 usable, 0 unknown", out)
        self.assertIn("workflow Workflow A; reference events: push", out)
        listed = [c for c in self.fake.calls if "actions/workflows/" in c]
        self.assertEqual(len(listed), 1)
        self.assertIn("workflows/11/runs", listed[0])

    def test_s1b_a_missing_artifact_of_the_chosen_workflow_is_unknown_and_another_workflow_never_is(self):
        self.fake.pr_run(["a::t"])
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        self.fake.add_run("push", "main", [], created=self.created(20), workflow="A", artifact=False)       # A without its artifact: unknown, as before
        self.fake.add_run("push", "main", [], created=self.created(5), workflow="B", artifact=False)        # B: never listed
        code, out = self.run_reds()
        self.assertIn("main runs: 2 listed (workflow Workflow A), 1 usable, 1 unknown", out)

    def test_s2_the_newest_pull_request_run_that_holds_the_artifact_is_the_pull_request_s_run_and_the_workflow_is_named(self):
        self.fake.pr_run([], artifact=False, workflow="B", created="2027-01-15T09:00:00Z")   # the newest run, another workflow, no artifact
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("1 failing in the pull request's run of workflow Workflow A", out)
        self.assertIn("also failing on main (1)", out)
        self.assertNotIn("workflows hold a failing-tests artifact", out)

    def test_s2b_when_two_workflows_hold_an_artifact_it_says_so_in_one_line_and_names_the_one_used(self):
        self.fake.pr_run(["a::t"], workflow="B", created="2027-01-15T09:00:00Z")
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="B")
        code, out = self.run_reds()
        self.assertEqual(len([l for l in out.splitlines() if "workflows hold a failing-tests artifact" in l]), 1)
        self.assertIn("2 workflows hold a failing-tests artifact on this pull request (Workflow A, Workflow B): the newest run's workflow, Workflow B, was used", out)
        self.assertIn("(workflow Workflow B;", out)

    def test_s3_a_pull_request_none_of_whose_runs_holds_an_artifact_cannot_be_classified_and_main_is_not_listed(self):
        self.fake.pr_run([], artifact=False, workflow="A")
        self.fake.pr_run([], artifact=False, workflow="B", created="2027-01-15T07:30:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("no failing-tests artifact on the pull request's run")]), 1)
        self.assertFalse([c for c in self.fake.calls if "actions/workflows/" in c], "no main run of any workflow was listed")

    def test_s4_the_messages_name_the_workflow_and_say_how_many_of_its_runs_were_looked_at(self):
        self.fake.pr_run(["a::t"])
        for position in range(5):
            self.fake.add_run("push", "main", [], created=self.created(10 + position * 60), workflow="A", artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertEqual(len([l for l in out.splitlines() if l == "no failing-tests artifact in the newest 5 runs of workflow Workflow A"]), 1)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 0 usable, 5 unknown", out)

    def test_s5_the_call_counts_with_two_reference_events_stay_within_14_cold_and_4_warm(self):
        self.fake.pr_run(["a::t"])
        for position in range(3):
            self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-%d-abc" % position, ["a::t"], created=self.created(10 + position * 60), workflow="A")
        for position in range(2):
            self.fake.add_run("schedule", "main", ["a::t"], created=self.created(40 + position * 60), workflow="A")
            self.fake.add_run("schedule", "main", [], created=self.created(41 + position * 60), workflow="B", artifact=False)
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(self.fake.calls), 14, self.fake.calls)
        cold = len(self.fake.calls)
        del self.fake.calls[:]
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(self.fake.calls), 4, self.fake.calls)
        self.assertTrue(all(len(a) == 3 for a in self.fake.argvs), "plain GETs only")

    def test_s6_the_workflow_override_picks_the_pull_request_s_run_of_that_file_and_lists_its_runs(self):
        self.fake.pr_run(["a::t"], workflow="B", created="2027-01-15T09:00:00Z")     # newer, with an artifact, but not the configured one
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(5), workflow="B")
        code, out = self.run_reds(workflow="a-ci.yml")
        self.assertEqual(code, 0, out)
        self.assertIn("in the pull request's run of workflow Workflow A", out)
        self.assertIn("main runs: 1 listed (workflow Workflow A)", out)
        listed = [c for c in self.fake.calls if "actions/workflows/" in c]
        self.assertIn("workflows/a-ci.yml/runs", listed[0])
        code, out = self.run_reds(workflow="nothing.yml")
        self.assertEqual(code, 3)
        self.assertIn("no completed run of workflow nothing.yml", out)

    def test_s6_the_override_setting_is_strict_a_file_name_only(self):
        self.assertEqual(self.hs.workflows({"workflow": {"acme/widget": "ci.yml"}}), {"acme/widget": "ci.yml"})
        for bad in ("../ci.yml", ".github/workflows/ci.yml", "https://x/ci.yml", "ci", "ci.yml?x=1", "", 5, None, "ci.yml\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.hs.workflows({"workflow": {"acme/widget": bad}})
        for bad in ({"workflow": []}, {"workflow": {"nope": "ci.yml"}}):
            with self.assertRaises(ValueError):
                self.hs.workflows(bad)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "reds": {"workflow": {"acme/widget": "../x.yml"}}})

    def test_a_run_with_no_workflow_id_says_workflow_unknown_and_never_lists_by_event_alone(self):
        self.fake.pr_run(["a::t"], no_workflow_id=True)
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertIn("names no workflow (workflow unknown): cannot classify", out)
        self.assertFalse([c for c in self.fake.calls if "actions/workflows/" in c or ("actions/runs?" in c and "head_sha" not in c)])

    def test_a_non_positive_workflow_id_is_unknown_and_a_foreign_workflow_run_in_a_listing_is_skipped(self):
        for bad in (0, -3, True):
            with self.subTest(bad=bad):
                self.fake = FakeActions()
                shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
                self.fake.pr_run(["a::t"])
                self.fake.runs["pull_request"][-1]["workflow_id"] = bad
                code, out = self.run_reds()
                self.assertEqual(code, 3, out)
                self.assertIn("workflow unknown", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        self.fake = FakeActions()
        self.fake.pr_run(["a::t"])
        own = self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        foreign = self.fake.add_run("push", "main", ["a::t"], created=self.created(5), workflow="A")
        for run in self.fake.runs["push"]:
            if run["id"] == foreign:
                run["workflow_id"] = 22   # a listing that hands back another workflow's run: skipped, defensively
        self.fake.leak_foreign = True
        code, out = self.run_reds()
        self.assertIn("main runs: 1 listed (workflow Workflow A), 1 usable, 0 unknown", out)

    def test_the_real_command_reads_through_the_real_reader_with_the_configured_events_and_exits_1_on_a_failed_read(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": "gh", "reds": {"reference_events": {"acme/widget": ["merge_group", "schedule"]}}})
        self.fake.pr_run(["a::t", "b::u"])
        self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-1-x", ["a::t"], created=self.created(10))
        self.fake.add_run("schedule", "main", ["b::u"], created=self.created(70))
        self.fake.add_run("push", "main", ["b::u"], created=self.created(5))   # a push run: not a reference event of this repo
        with captured() as (out, err):
            code = sb.main(["reds", "acme/widget#12"], runner=self.fake, now=self.now)
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out.getvalue())
        self.assertIn("reference events: merge_group, schedule)", out.getvalue())
        self.assertTrue(any("event=merge_group" in c for c in self.fake.calls) and not any("event=push" in c for c in self.fake.calls))
        self.fake.fail_paths = {"actions/artifacts?name"}
        shutil.rmtree(str(sb.state_dir_path() / "reds-cache"), ignore_errors=True)
        with captured() as (out, err):
            code = sb.main(["reds", "acme/widget#12"], runner=self.fake, now=self.now)
        self.assertEqual(code, 1)
        self.assertIn("nothing was classified", out.getvalue())
        self.assertNotIn("also failing on main", out.getvalue())

    def test_an_oversized_or_unsized_artifact_is_refused_before_the_download_and_a_full_artifact_page_is_said(self):
        self.fake.pr_run(["a::t"])
        run = self.fake.add_run("push", "main", ["a::t"], created=self.created(10))
        self.fake.artifacts[run]["size_in_bytes"] = self.hs.ZIP_MAX + 1
        code, out = self.run_reds()
        self.assertIn("main runs: 1 listed (workflow Workflow A), 0 usable, 1 unknown; run %d: artifact too large" % run, out)
        self.assertFalse([c for c in self.fake.calls if c.endswith("/zip") and str(self.fake.artifacts[run]["id"]) in c], "never downloaded")
        del self.fake.artifacts[run]["size_in_bytes"]
        shutil.rmtree(str(self.state / "reds-cache"))
        code, out = self.run_reds()
        self.assertIn("artifact too large or of unknown size", out)
        # a full page: a run missing from it may be older than the page
        self.fake.artifacts.update({10_000 + n: {"id": 20_000 + n, "expired": False, "workflow_run": {"id": 10_000 + n}, "size_in_bytes": 10} for n in range(100)})
        missing = self.fake.add_run("push", "main", ["a::t"], created=self.created(5), artifact=False)
        shutil.rmtree(str(self.state / "reds-cache"))
        code, out = self.run_reds()
        self.assertIn("run %d: no artifact in the newest 100" % missing, out)

    def test_main_s_artifacts_are_capped_too_and_a_corrupt_cache_entry_is_a_miss(self):
        self.fake.pr_run(["n1"])
        self.main_runs([["n%d" % i for i in range(3000)]])
        code, out = self.run_reds()
        self.assertIn("n1 (1 of 1 main runs)", out)
        cached = sorted((self.state / "reds-cache" / "acme_widget").glob("*.json"))
        for path in cached:
            body = json.loads(path.read_text())
            self.assertLessEqual(len(body.get("failed", [])), self.hs.NAMES_MAX)
            path.write_text("{garbage")
        del self.fake.calls[:]
        code, out = self.run_reds()
        self.assertEqual(code, 0, "a corrupt cache entry is read again, not trusted")
        self.assertGreater(len(self.fake.calls), 4)

    def test_the_age_is_right_in_any_time_zone(self):
        stamp = calendar.timegm((2027, 1, 15, 8, 0, 0))
        for zone in ("UTC", "America/New_York", "Asia/Kolkata"):
            with mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                self.assertEqual(self.hs._when("2027-01-15T07:00:00Z", stamp), 3600)
        time.tzset()

    def test_the_artifact_reader_is_bounded_and_strict(self):
        for raw in (b"x" * (self.hs.ZIP_MAX + 1), b"PK not really", ):
            with self.assertRaises(self.hs.RedsError):
                self.hs.read_zip(raw)
        with self.assertRaises(self.hs.RedsError):
            self.hs.read_zip("a text, not bytes")
        for body in ('{"v": 2, "complete": true, "failed": []}', '{"v": 1, "complete": "yes", "failed": []}', "[1]", "nope"):
            with self.assertRaises(self.hs.RedsError):
                self.hs.parse_artifact(body)

    def test_the_cache_is_private_bounded_and_remembers_a_missing_artifact_only_for_a_while(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]])
        self.run_reds()
        files = list((self.state / "reds-cache" / "acme_widget").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual({f.stat().st_mode & 0o777 for f in files}, {0o600})
        cache = self.hs.Cache(self.state, "acme/widget", self.now)
        cache.put(5, {"unknown": "no artifact"})
        self.assertEqual(cache.get(5)["unknown"], "no artifact")
        self.clock[0] += self.hs.UNKNOWN_TTL + 1
        self.assertIsNone(cache.get(5), "a missing artifact is looked for again later")

    def test_the_config_key_is_validated_and_the_command_takes_only_owner_repo_n(self):
        self.assertEqual(self.hs.settings({"reference_events": {"acme/widget": ["merge_group", "schedule"]}}), {"acme/widget": ["merge_group", "schedule"]})
        for bad in (3, {"x": 1}, {"reference_events": []}, {"reference_events": {"nope": ["push"]}}, {"reference_events": {"a/b": []}},
                    {"reference_events": {"a/b": ["pushh"]}}, {"reference_events": {"a/b": ["push", "push"]}}):
            with self.assertRaises(ValueError):
                self.hs.settings(bad)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "reds": {"reference_events": {"a": ["push"]}}})
        with captured() as (out, err):
            self.assertEqual(sb.main(["reds", "no-hash"], runner=self.fake, now=self.now), sb.EXIT_USAGE)


class TestJanitorLiveKeeps(Base):
    """Ask #932: the janitor keeps what a live agent owns, uses or touched (the registry, the access time, a file a live process holds open), and every doubt
    keeps. Fixture directories and fakes for the registry, the clock and the open-file check only: the real temp directory is never read. Each entry here is one
    the old rule (modification and change time only) would have removed."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",), config={})
        self.jan = sb.load_disk_janitor()
        self.temp = self.home / "jtmp"
        self.temp.mkdir()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "temp_janitor": {"dir": str(self.temp)}})
        self.world.clock.t = time.time() + 10 * 86400   # a change time cannot be set back: the clock moves forward instead
        self.now = self.world.clock.now()
        (self.world.state / "state-marker.json").write_text("{}")
        self.open_paths = []
        self.open_timeouts = []
        self.lsof_code = 0
        self.lsof_error = None
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]

    def routed(self, argv, **kwargs):
        if argv[0] == "lsof":
            if "-d" in argv:
                return subprocess.CompletedProcess(argv, 0, "p1\nfcwd\nn/\n", "")
            self.open_timeouts.append(kwargs.get("timeout"))
            if self.lsof_error:
                raise self.lsof_error
            if self.lsof_code:
                return subprocess.CompletedProcess(argv, self.lsof_code, "", "")
            return subprocess.CompletedProcess(argv, 0, "p1\n" + "".join("fx\nn%s\n" % p for p in self.open_paths), "")
        return self.world.route(argv, **kwargs)

    def old(self, name, days=10, directory=False, atime_days=None):
        path = self.temp / name
        if directory:
            path.mkdir()
            (path / "inner.txt").write_text("x")
        else:
            path.write_text("x")
        stamp = self.now - days * 86400
        read = self.now - (days if atime_days is None else atime_days) * 86400
        for p in ([path / "inner.txt"] if directory else []) + [path]:
            os.utime(str(p), (read, stamp))
        return path

    def apply(self):
        with captured() as (out, err):
            code = sb.main(janitor_review(self, ["--apply"]), runner=self.routed, now=self.world.clock.now, socket_call=self.world.herdr.socket)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def registry(self, agents=None, retired=None):
        (self.world.state / sb.PANE_HEALTH_FILE).write_text(json.dumps({"agents": agents or {}, "states": {}, "missing": {}}))
        (self.world.state / sb.RETIRED_FILE).write_text(json.dumps(retired or {}))

    def test_s1_an_entry_a_live_or_recently_retired_agent_owns_is_kept(self):
        tmpdir = self.old("live", directory=True)              # the agent's own TMPDIR
        holds = self.old("holds-a-cwd", directory=True)                # it contains the agent's working directory
        (holds / "work").mkdir()
        recent = self.old("retired-recently", directory=True)          # a retired agent's working directory, retired 2 days ago
        stale = self.old("retired-long-ago", directory=True)           # retired 20 days ago: the registry no longer holds it
        self.registry({"live": {"cwd": str(holds / "work"), "session": "ses_a"}},
                      {"gone": {"at": self.now - 2 * 86400, "session": "ses_b", "record": {"cwd": str(recent)}},
                       "older": {"at": self.now - 20 * 86400, "session": "ses_c", "record": {"cwd": str(stale)}}})
        with mock.patch.object(sb, "agent_tmp_root", lambda config: self.temp):
            out = self.apply()
        for kept in (tmpdir, holds, recent):
            self.assertTrue(kept.exists(), kept.name)
        names = [n for n, _ in sb.janitor_agent_roots(self.world.engine, self.now, 7)]
        self.assertIn("gone", names)
        self.assertNotIn("older", names, "retired past the janitor's own day limit: no longer held by the registry")
        self.assertIn("kept live: it is, holds or sits inside", out)
        # an entry that sits INSIDE an agent's directory
        inside = self.old("sits-inside", directory=True)
        self.registry({"live": {"cwd": str(self.temp), "session": "ses_a"}})
        self.apply()
        self.assertTrue(inside.exists())

    def test_s1_the_one_function_is_the_only_reader_of_the_agent_list(self):
        entry = self.old("owned-by-the-fake", directory=True)
        seen = []

        def fake(engine, now, days):
            seen.append(days)
            return [("fake-agent", str(entry))]
        with mock.patch.object(sb, "janitor_agent_roots", fake):
            self.apply()
        self.assertTrue(entry.exists())
        self.assertEqual(seen[0], 7)

    def test_s2_a_read_keeps_an_entry_and_a_clock_decides(self):
        read = self.old("read-yesterday.log", atime_days=1)
        untouched = self.old("untouched.log")
        self.apply()
        self.assertTrue(read.exists(), "10 days by modification, 1 day by access")
        self.assertFalse(untouched.exists())
        directory = self.old("dir-read-yesterday", directory=True, atime_days=1)
        self.apply()
        self.assertTrue(directory.exists(), "a file read inside a directory keeps the directory")

    def test_s2_a_directorys_own_access_time_is_not_counted(self):
        plain = self.old("listed-only", directory=True)
        os.utime(str(plain), (self.now, self.now - 10 * 86400))   # only the directory's own access time is new (listing it does that)
        (plain / "inner.txt").touch()
        os.utime(str(plain / "inner.txt"), (self.now - 10 * 86400,) * 2)
        self.apply()
        self.assertFalse(plain.exists())

    def test_s3_a_file_a_live_process_has_open_keeps_its_entry(self):
        held = self.old("held-open", directory=True)
        free = self.old("not-open", directory=True)
        self.open_paths = [str(held / "inner.txt"), "/somewhere/else"]
        out = self.apply()
        self.assertTrue(held.exists())
        self.assertFalse(free.exists())
        self.assertIn("kept held-open: a live process has a file of it open", out)

    def test_s7_every_doubt_keeps_and_the_pass_ends_in_time(self):
        unreadable_registry = self.old("registry-doubt", directory=True)
        (self.world.state / sb.PANE_HEALTH_FILE).write_text("{not json")
        out = self.apply()
        self.assertTrue(unreadable_registry.exists())
        self.assertIn("pane-health.json cannot be read", out)
        self.registry()
        atime = self.old("atime-doubt")
        with mock.patch.object(self.jan, "_atime", side_effect=OSError("no atime")):
            self.apply()
        self.assertTrue(atime.exists())
        timeout = self.old("open-check-timeout", directory=True)
        self.lsof_error = subprocess.TimeoutExpired(["lsof"], 30)
        out = self.apply()
        self.assertTrue(timeout.exists())
        self.assertIn("did not finish", out)
        self.lsof_error = None
        self.apply()
        self.assertFalse(timeout.exists(), "with every source readable and nothing holding it, the same entry goes")

    def test_s7_a_registry_that_raises_anything_keeps_the_entry(self):
        entry = self.old("registry-raises", directory=True)
        with mock.patch.object(sb, "janitor_agent_roots", side_effect=RuntimeError("boom")):
            out = self.apply()
        self.assertTrue(entry.exists())
        self.assertIn("agent registry cannot be read", out)

    def test_s7_the_open_file_check_is_bounded_and_every_failure_of_it_keeps(self):
        entry = self.old("open-check-fails", directory=True)
        self.lsof_code = 2
        out = self.apply()
        self.assertTrue(entry.exists())
        self.assertIn("the open-file check failed", out)
        self.assertEqual(set(self.open_timeouts), {sb.JANITOR_OPEN_SECONDS})
        self.lsof_code = 0
        with mock.patch.object(sb, "JANITOR_OPEN_SECONDS", -1):   # the resolving of the list is bounded too
            self.open_paths = ["/x"]
            out = self.apply()
        self.assertTrue(entry.exists())
        self.assertIn("resolving the open-file list took longer", out)

    def test_s7_an_access_time_that_cannot_be_read_inside_a_directory_keeps_it(self):
        entry = self.old("inner-atime-doubt", directory=True)
        real = self.jan._atime
        with mock.patch.object(self.jan, "_atime", lambda info: (_ for _ in ()).throw(OSError("x")) if info.st_size == 1 and info.st_mode & 0o100000 else real(info)):
            self.apply()
        self.assertTrue(entry.exists())

    def test_s7_a_retired_entry_with_a_time_that_cannot_be_read_counts_as_recent(self):
        self.registry({}, {"odd": {"at": "yesterday", "session": "ses_x", "record": {"cwd": str(self.temp / "odd-cwd")}}})
        self.assertIn("odd", [n for n, _ in sb.janitor_agent_roots(self.world.engine, self.now, 7)])

    def test_s4_the_records_in_doubt_are_read_as_doubt(self):
        for text in ("{" , json.dumps({"rule": ["not", "scalar"]}), json.dumps({"rule": "x" * 5000})):
            (self.world.state / sb.TEMP_JANITOR_GATE_FILE).write_text(text)
            self.assertEqual(sb.read_janitor_record(self.world.state, sb.TEMP_JANITOR_GATE_FILE), (False, {}))
        self.assertEqual(sb.janitor_apply_allowed(self.world.state, "r")[1], "unreadable")

    def test_a_links_own_access_time_is_not_a_read(self):
        link = self.temp / "old-link"
        os.symlink(str(self.home), str(link))
        os.utime(str(link), (self.now, self.now - 10 * 86400), follow_symlinks=False)   # only the link's access time is new
        self.apply()
        self.assertFalse(os.path.lexists(str(link)))

    def test_the_fingerprint_covers_every_setting_that_changes_what_is_selected(self):
        keys = set(self.jan.DEFAULTS)
        self.assertEqual(keys, set(self.jan.SELECTION_KEYS) | set(self.jan.OTHER_KEYS), "every setting is classified as selecting or not")
        self.assertFalse(set(self.jan.SELECTION_KEYS) & set(self.jan.OTHER_KEYS))
        base = {"days": 7, "keep": [], "dir": str(self.temp)}
        other = {"days": 8, "keep": ["x"], "dir": str(self.home)}
        for key in self.jan.SELECTION_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(self.jan.rule_fingerprint(base), self.jan.rule_fingerprint(dict(base, **{key: other[key]})))
        for key in self.jan.OTHER_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.jan.rule_fingerprint(base), self.jan.rule_fingerprint(dict(base, **{key: 123})))

    def test_each_of_is_contains_and_sits_inside_keeps_on_its_own(self):
        entry = self.old("the-entry", directory=True)
        (entry / "work").mkdir()
        for label, root in (("is", entry), ("contains", entry / "work"), ("sits inside", self.temp)):
            with self.subTest(label), mock.patch.object(sb, "janitor_agent_roots", lambda e, n, d, root=root: [("fake", str(root))]):
                self.assertTrue(sb.janitor_busy_function(self.world.engine, self.routed, self.jan)(str(entry)), label)
        with mock.patch.object(sb, "janitor_agent_roots", lambda e, n, d: [("fake", str(self.home / "elsewhere"))]):
            self.assertIsNone(sb.janitor_busy_function(self.world.engine, self.routed, self.jan)(str(entry)))

    def test_a_process_working_in_an_entry_and_a_file_open_in_it_each_keep_it_on_their_own(self):
        entry = self.old("the-entry", directory=True)
        busy_for = lambda: sb.janitor_busy_function(self.world.engine, self.routed, self.jan)
        with mock.patch.object(sb, "janitor_agent_roots", lambda e, n, d: []):
            self.assertIsNone(busy_for()(str(entry)))
            self.open_paths = [str(entry)]
            self.assertIn("file of it open", busy_for()(str(entry)))
            self.open_paths = [str(entry).upper()]   # a case-insensitive volume may spell it otherwise
            self.assertIn("file of it open", busy_for()(str(entry)))
            self.open_paths = []
            real = self.routed
            cwd = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "p1\nfcwd\nn%s\n" % entry, "") if "-d" in argv else real(argv, **kw)
            self.assertIn("working directory", sb.janitor_busy_function(self.world.engine, cwd, self.jan)(str(entry)))
            upper = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "p1\nfcwd\nn%s\n" % str(entry).upper(), "") if "-d" in argv else real(argv, **kw)
            self.assertIn("working directory", sb.janitor_busy_function(self.world.engine, upper, self.jan)(str(entry)))

    def test_s7_a_retired_list_that_cannot_be_read_keeps_everything(self):
        entry = self.old("retired-doubt", directory=True)
        (self.world.state / sb.PANE_HEALTH_FILE).write_text(json.dumps({"agents": {}, "states": {}, "missing": {}}))
        (self.world.state / "registry.json").write_text("[1, 2")   # the registry is the one source of retirements (#929); retired.json is derived and never read
        out = self.apply()
        self.assertTrue(entry.exists())
        self.assertIn("cannot read the agent registry", out)

    def test_u1_an_entry_nothing_owns_uses_or_touched_is_removed_as_before(self):
        gone = [self.old("old-%d.log" % n) for n in range(2)] + [self.old("old-dir", directory=True)]
        self.registry({"live": {"cwd": str(self.home / "elsewhere")}})
        self.apply()
        self.assertEqual([p.exists() for p in gone], [False, False, False])

    def test_the_janitors_own_preview_does_not_keep_the_entries_it_lists(self):
        listed = self.old("listed-in-the-preview.log")
        other = self.old("named-by-another-event.log")
        lines = [{"event": "janitor.would_remove", "detail": {"names": ["listed-in-the-preview.log"]}},
                 {"event": "temp.removed", "subject": "listed-in-the-preview.log"},
                 {"event": "launched", "subject": "coder", "detail": {"note": "see named-by-another-event.log"}}]
        (self.world.state / "audit.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
        self.apply()
        self.assertFalse(listed.exists())
        self.assertTrue(other.exists(), "a record by anything else still names it")

    def test_the_rule_fingerprint_follows_the_rule_and_never_a_name(self):
        base = self.jan.rule_fingerprint({"days": 7, "keep": ["a"]})
        self.assertNotEqual(base, self.jan.rule_fingerprint({"days": 7, "keep": ["a"], "dir": "/x/y"}), "another directory is another rule")
        self.assertNotEqual(base, self.jan.rule_fingerprint({"days": 8, "keep": ["a"]}))
        self.assertNotEqual(base, self.jan.rule_fingerprint({"days": 7, "keep": ["b"]}))
        with mock.patch.object(self.jan, "RULE_VERSION", "3"):
            self.assertNotEqual(base, self.jan.rule_fingerprint({"days": 7, "keep": ["a"]}))
        self.assertNotEqual(self.jan.preview_fingerprint(base, ["a"]), self.jan.preview_fingerprint(base, ["b"]))


class TestDaemonTempJanitor(Base):
    """Ask #762: the daemon applies the weekly temp janitor itself (the Docker reaper's path), on the janitor's own rule, dry until `daemon_apply` is on.
    Fixture directories and fakes only: the real temp directory is never read or written."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",), config={})
        self.jan = sb.load_disk_janitor()
        self.temp = self.home / "jtmp"
        self.temp.mkdir()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "temp_janitor": {"dir": str(self.temp), "keep": ["keepme-*"]}})
        self.world.clock.t = time.time() + 10 * 86400
        self.now = self.world.clock.now()
        (self.world.state / "state-marker.json").write_text("{}")
        self.calls = []
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})

    def routed(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, "p1\nfcwd\nn/\n", "")
        return self.world.route(argv, **kwargs)

    def old(self, name, days=10, directory=False):
        path = self.temp / name
        if directory:
            path.mkdir()
            (path / "inner.txt").write_text("x")
        else:
            path.write_text("x")
        stamp = self.now - days * 86400
        for p in ([path / "inner.txt"] if directory else []) + [path]:
            os.utime(str(p), (stamp, stamp))
        return path

    def fixture(self):
        gone = self.old("scratch-old-unnamed.log")
        named = self.old("named-by-the-records.txt")
        (self.world.state / "reminders.json").write_text(json.dumps({"r": "see named-by-the-records.txt"}))
        marked = self.old("marked-dir", directory=True)
        (marked / ".fleet-keep").write_text("")
        link = self.temp / "a-link-to-elsewhere"
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        os.symlink(str(elsewhere), str(link))
        os.utime(str(link), (self.now - 10 * 86400,) * 2, follow_symlinks=False)
        return gone, [named, marked, self.old("recent.txt", days=1), elsewhere]

    def ack(self, cfg):
        """The preview was sent and the yes recorded, for the rule `cfg` makes (the state a daemon that has been cleared is in)."""
        rule = self.jan.rule_fingerprint(self.jan.settings(cfg))
        (self.world.state / sb.TEMP_JANITOR_GATE_FILE).write_text(json.dumps({"rule": rule, "preview": "p" * 16, "sent": True, "count": 0, "nonce": "n1"}))
        (self.world.state / sb.TEMP_JANITOR_ACK_FILE).write_text(json.dumps({"rule": rule, "preview": "p" * 16, "nonce": "n1", "by": "cos", "at": 1}))

    def daemon(self, temp_janitor=None, runner=True, acked=True):
        engine = self.world.engine
        cfg = {"dir": str(self.temp), "keep": ["keepme-*"]}
        cfg.update(temp_janitor or {})
        if acked and cfg.get("daemon_apply"):
            self.ack(cfg)
        engine.config = dict(engine.config, temp_janitor=cfg)
        engine.temp_janitor_runner = self.routed if runner else None
        engine.now = lambda: self.now
        source = [s for s in engine.sources if getattr(s, "__name__", "") == "temp_janitor_facts"][0]
        return engine, source

    def go(self, source):
        source()
        if source.thread:
            source.thread.join(20)

    def events(self, name):
        return [e for e in self.world.audit() if e["event"] == name]

    def test_s1_s2_the_daemon_applies_only_the_dry_runs_selection(self):
        gone, kept = self.fixture()
        engine, source = self.daemon({"daemon_apply": True})
        before = self.jan.scan(self.temp, self.world.state, self.now, self.jan.settings({"keep": ["keepme-*"]}), busy=lambda p: None)
        listed = sorted(r["name"] for r in before if r["action"] == "remove")
        self.assertEqual(listed, ["a-link-to-elsewhere", "scratch-old-unnamed.log"])
        self.go(source)
        self.assertFalse(gone.exists())
        self.assertFalse(os.path.lexists(str(self.temp / "a-link-to-elsewhere")), "a link is removed as a link")
        for path in kept:
            self.assertTrue(path.exists(), path.name)
        self.assertEqual(sorted(e["subject"] for e in self.events("temp.removed")), listed, "the names the daemon removed are the dry run's")

    def test_s5_one_summary_with_count_bytes_names_only_and_the_budget(self):
        self.old("a-old-one.txt")
        self.old("b-old-two.txt")
        engine, source = self.daemon({"daemon_apply": True, "weekly_scan_seconds": 300})
        self.go(source)
        self.assertEqual(len(self.events("temp.removed")), 2)
        summary = self.events("janitor.summary")
        self.assertEqual(len(summary), 1)
        detail = summary[0]["detail"]
        self.assertEqual((detail["count"], detail["bytes"], detail["names"], detail["budget_seconds"]), (2, 2, ["a-old-one.txt", "b-old-two.txt"], 300))
        self.assertIn("used_seconds", detail)
        self.assertNotIn(str(self.temp), json.dumps(detail["names"]))

    def test_s7_the_default_is_a_dry_run_audited_with_the_list_and_nothing_moves(self):
        gone, kept = self.fixture()
        engine, source = self.daemon()
        self.go(source)
        self.assertTrue(gone.exists())
        self.assertEqual(self.events("temp.removed"), [])
        self.assertEqual(self.events("janitor.summary"), [])
        dry = self.events("janitor.would_remove")
        self.assertEqual(len(dry), 1)
        self.assertEqual(dry[0]["detail"]["names"], ["a-link-to-elsewhere", "scratch-old-unnamed.log"])
        self.assertFalse((self.world.state / self.jan.CURSOR_FILE).exists(), "a dry run does not move the cursor")

    def test_s8_the_switch_is_a_boolean(self):
        with self.assertRaises(ValueError):
            self.jan.validate({"daemon_apply": "yes"})
        self.assertIsNone(self.jan.validate({"daemon_apply": True}))
        self.assertIs(self.jan.settings(None)["daemon_apply"], False)

    def test_s3_the_command_line_still_refuses_an_agent_and_an_unknown_caller(self):
        victim = self.old("old-file.txt")
        engine, source = self.daemon({"daemon_apply": True})   # the switch does not open the command line
        for who in ("coder", None):
            if who:
                os.environ["HERDR_PANE_ID"] = self.world.panes[who]
            else:
                os.environ["HERDR_PANE_ID"] = "p_unknown_pane"
            with captured() as (out, err):
                code = sb.main(janitor_review(self, ["--apply"]), runner=self.routed, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            self.assertEqual(code, 1)
            self.assertIn("only the operator or the Chief of Staff may --apply the temp janitor", err.getvalue())
            self.assertTrue(victim.exists())

    def test_s4_an_engine_that_is_not_the_daemon_never_runs_whatever_the_environment_holds(self):
        victim = self.old("old-file.txt")
        engine, source = self.daemon({"daemon_apply": True}, runner=False)
        with mock.patch.dict(os.environ, {"FLEET_TEMP_JANITOR_APPLY": "1", "FLEET_DAEMON": "1", "TEMP_JANITOR_DAEMON_APPLY": "true"}):
            self.go(source)
        self.assertTrue(victim.exists())
        self.assertIsNone(source.thread)
        self.assertEqual([e for e in self.world.audit() if e["event"].startswith(("temp.", "janitor."))], [])
        self.assertEqual(self.calls, [])

    def test_the_kill_switch_runs_nothing(self):
        victim = self.old("old-file.txt")
        engine, source = self.daemon({"daemon_apply": True, "enabled": False})
        self.go(source)
        self.assertTrue(victim.exists())
        self.assertIsNone(source.thread)

    def test_s6_every_doubt_keeps_the_entry_and_says_so(self):
        # an unreadable state file: the janitor judges nothing
        victim = self.old("old-file.txt")
        (self.world.state / "broken.json").write_text("x")
        os.chmod(str(self.world.state / "broken.json"), 0)
        self.addCleanup(os.chmod, str(self.world.state / "broken.json"), 0o600)
        engine, source = self.daemon({"daemon_apply": True})
        self.go(source)
        self.assertTrue(victim.exists())
        self.assertEqual(self.events("temp.removed"), [])
        self.assertTrue(any("not be read" in n or "not judged" in n or "nothing was judged" in n for n in self.events("janitor.summary")[0]["detail"]["notes"]))

    def test_s6_a_scan_over_the_budget_and_an_entry_changed_after_the_scan_are_kept(self):
        a, b = self.old("a-old.txt"), self.old("b-old.txt")
        ticks = [0.0]

        def clock():
            ticks[0] += 100.0
            return ticks[0]
        real_run = self.jan.run
        engine, source = self.daemon({"daemon_apply": True, "weekly_scan_seconds": 150, "scan_seconds": 86400})
        with mock.patch.object(self.jan, "run", lambda *a_, **k: real_run(*a_, clock=clock, **k)):
            self.go(source)
        self.assertEqual([a.exists(), b.exists()].count(False), 1, "exactly one entry was reached; the one the budget did not reach is never removed")
        self.assertTrue(any("not judged" in n for n in self.events("janitor.summary")[0]["detail"]["notes"]))
        # changed between the scan and the removal: the second judgement of the same entry sees a newer file
        for path in self.temp.iterdir():
            path.unlink()
        c = self.old("c-old.txt")
        real, seen = self.jan.judge, [0]

        def judge(path, *args, **kwargs):
            seen[0] += 1
            if seen[0] == 2:
                os.utime(str(c), (self.now, self.now))
            return real(path, *args, **kwargs)
        (self.world.state / sb.TEMP_JANITOR_FILE).unlink()
        before = len(self.events("temp.removed"))
        engine, source = self.daemon({"daemon_apply": True})
        with mock.patch.object(self.jan, "judge", judge):
            self.go(source)
        self.assertGreaterEqual(seen[0], 2)
        self.assertTrue(c.exists())
        self.assertEqual(len(self.events("temp.removed")), before)

    def test_s9_the_weekly_gate(self):
        victim = self.old("old-file.txt")
        stamp = self.world.state / sb.TEMP_JANITOR_FILE
        engine, source = self.daemon({"daemon_apply": False})
        stamp.write_text(json.dumps({"at": self.now - 2 * 86400}))
        self.go(source)
        self.assertEqual(self.events("janitor.would_remove"), [], "2 days ago: not yet")
        stamp.write_text(json.dumps({"at": self.now - 6 * 86400 - 60}))
        self.go(source)
        self.assertEqual(len(self.events("janitor.would_remove")), 1)
        self.assertEqual(json.loads(stamp.read_text())["at"], self.now)
        self.go(source)
        self.assertEqual(len(self.events("janitor.would_remove")), 1, "never twice within 6 days")
        stamp.write_text("{garbage")
        engine.now = lambda: self.now + 30 * 86400
        self.go(source)
        self.assertEqual(len(self.events("janitor.would_remove")), 1, "a stamp that cannot be read means do not run")
        self.assertEqual(len(self.events("janitor.state_unreadable")), 1)
        self.assertTrue(victim.exists())
        stamp.write_text(json.dumps({"at": 0}))   # a stamp that cannot be read (permission) is the same doubt
        os.chmod(str(stamp), 0)
        self.addCleanup(os.chmod, str(stamp), 0o600)
        self.go(source)
        self.assertEqual(len(self.events("janitor.would_remove")), 1)

    # -- ask #932: a changed selection rule makes one preview and applies nothing until the yes to that exact preview is recorded

    def preview_facts(self, engine, source):
        engine.discover(self.now)
        return [f for f in source() if f.kind == "janitor"]

    def week(self, engine):
        self.now += 7 * 86400
        engine.now = lambda: self.now

    def record_ack(self, preview):
        os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
        with captured() as (out, err):
            code = sb.main(["disk", "janitor-ack", preview], runner=self.routed, now=self.world.clock.now, socket_call=self.world.herdr.socket)
        return code, err.getvalue()

    def test_958_the_commands_fingerprint_equals_the_daemons_preview_for_the_same_rule_and_names(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        preview = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
        with captured() as (out, err):
            sb.main(["disk", "janitor", "--weekly"], runner=self.routed, now=self.world.clock.now, socket_call=self.world.herdr.socket)
        self.assertIn("preview fingerprint: %s" % preview, out.getvalue())

    def test_s4_one_preview_then_nothing_is_applied_until_the_yes_to_that_exact_preview(self):
        victim = self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        self.assertTrue(victim.exists(), "no yes: nothing is removed")
        self.assertEqual(self.events("temp.removed"), [])
        facts = self.preview_facts(engine, source)
        self.assertEqual(len(facts), 1, "ONE preview message")
        self.assertEqual(facts[0].recipient, "cos")
        preview = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        self.assertIn("scratch-old-unnamed.log", facts[0].summary)
        self.assertIn("janitor-ack %s" % preview, facts[0].summary)
        # a week later: still no yes, still no second message, still nothing removed
        self.week(engine)
        self.go(source)
        self.assertEqual(self.preview_facts(engine, source), [])
        self.assertTrue(victim.exists())
        self.assertEqual(self.events("temp.removed"), [])
        # a yes for an older preview is refused and records nothing
        code, err = self.record_ack("0" * 16)
        self.assertEqual(code, 1)
        self.assertIn("no pending temp janitor preview", err)
        self.assertFalse((self.world.state / sb.TEMP_JANITOR_ACK_FILE).exists())
        self.week(engine)
        self.go(source)
        self.assertTrue(victim.exists())
        # the yes to the exact preview: the next weekly run applies
        code, err = self.record_ack(preview)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.events("janitor.ack")), 1)
        self.week(engine)
        self.go(source)
        self.assertFalse(victim.exists())
        self.assertEqual([e["subject"] for e in self.events("temp.removed")], ["scratch-old-unnamed.log"])

    def test_s4_a_changed_rule_needs_a_new_preview_and_a_new_yes(self):
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.old("first-old.log")
        self.go(source)
        self.preview_facts(engine, source)
        first = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        self.assertEqual(self.record_ack(first)[0], 0)
        second_victim = self.old("second-old.log")
        engine, source = self.daemon({"daemon_apply": True, "days": 8}, acked=False)   # the rule changed: days 7 -> 8
        self.week(engine)
        self.go(source)
        self.assertTrue(second_victim.exists(), "the yes was for the old rule")
        self.assertEqual(self.events("temp.removed"), [])
        facts = self.preview_facts(engine, source)
        self.assertEqual(len(facts), 1, "a new preview")
        newest = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        self.assertNotEqual(first, newest)
        self.week(engine)
        self.go(source)
        self.assertTrue(second_victim.exists(), "the old yes does not carry over")
        self.assertEqual(self.record_ack(newest)[0], 0)
        self.week(engine)
        self.go(source)
        self.assertFalse(second_victim.exists())

    def test_s4_a_record_in_doubt_applies_nothing(self):
        victim = self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=True)
        (self.world.state / sb.TEMP_JANITOR_ACK_FILE).write_text("{broken")
        self.go(source)
        self.assertTrue(victim.exists())
        self.assertEqual(self.events("janitor.would_remove")[0]["detail"]["gate"], "unreadable")

    def test_s4_a_yes_recorded_under_another_rule_applies_nothing(self):
        victim = self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=True)
        ack = self.world.state / sb.TEMP_JANITOR_ACK_FILE
        ack.write_text(json.dumps(dict(json.loads(ack.read_text()), rule="another-rule")))
        self.go(source)
        self.assertTrue(victim.exists())

    def test_s4_an_ordinary_agent_and_an_unknown_caller_cannot_record_the_yes(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        self.preview_facts(engine, source)
        preview = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        for pane in (self.world.panes["coder"], "p_unknown_pane"):
            os.environ["HERDR_PANE_ID"] = pane
            with captured() as (out, err):
                code = sb.main(["disk", "janitor-ack", preview], runner=self.routed, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            self.assertEqual(code, 1)
            self.assertIn("only the operator or the Chief of Staff", err.getvalue())
            self.assertFalse((self.world.state / sb.TEMP_JANITOR_ACK_FILE).exists())

    def test_s4_with_no_chief_of_staff_running_the_preview_waits_and_is_then_sent_once(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        found = engine.discover(self.now)
        with mock.patch.object(engine, "discovery", type("D", (), {"agents": {}, "problems": []})()):
            self.assertEqual([f for f in source() if f.kind == "janitor"], [])
        self.assertFalse(json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["sent"])
        self.assertEqual(len(self.preview_facts(engine, source)), 1)
        self.assertTrue(json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["sent"])

    def test_s4_a_gate_that_moved_before_delivery_sends_nothing_stale(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        gate = self.world.state / sb.TEMP_JANITOR_GATE_FILE
        gate.write_text(json.dumps(dict(json.loads(gate.read_text()), preview="another-preview")))
        self.assertEqual(self.preview_facts(engine, source), [])
        self.assertFalse(json.loads(gate.read_text()).get("sent"))

    def test_s4_a_gate_that_cannot_be_marked_sent_is_retried(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        with mock.patch.object(sb, "_write_small", side_effect=OSError("full")):
            self.assertEqual(self.preview_facts(engine, source), [])
        self.assertEqual(len(self.preview_facts(engine, source)), 1)

    def test_s4_every_comparison_of_the_gate_is_pinned(self):
        state = self.world.state
        gate = {"rule": "r1", "preview": "p1", "sent": True, "count": 1, "nonce": "n1"}
        ack = {"rule": "r1", "preview": "p1", "nonce": "n1", "by": "cos", "at": 1}

        def allowed(gate_changes=None, ack_changes=None, rule="r1"):
            (state / sb.TEMP_JANITOR_GATE_FILE).write_text(json.dumps(dict(gate, **(gate_changes or {}))))
            (state / sb.TEMP_JANITOR_ACK_FILE).write_text(json.dumps(dict(ack, **(ack_changes or {}))))
            return sb.janitor_apply_allowed(state, rule)[0]
        self.assertTrue(allowed())
        self.assertFalse(allowed(rule="r2"), "another rule")
        self.assertFalse(allowed(ack_changes={"rule": "r2"}), "a yes under another rule")
        self.assertFalse(allowed(ack_changes={"preview": "p0"}), "a yes to another preview")
        self.assertFalse(allowed(gate_changes={"sent": False}), "a preview nobody was told")
        self.assertFalse(allowed(ack_changes={"nonce": "n0"}), "a yes given to an earlier preview")
        self.assertFalse(allowed(gate_changes={"nonce": ""}, ack_changes={"nonce": ""}), "no nonce at all")

    def test_s4_the_daemons_apply_follows_each_comparison_of_the_gate(self):
        cases = {"all match": ({}, {}, True), "ack under another rule": ({}, {"rule": "other"}, False), "ack to another preview": ({}, {"preview": "other"}, False),
                 "preview never sent": ({"sent": False}, {}, False), "ack to an earlier preview (nonce)": ({}, {"nonce": "old"}, False)}
        for label, (gate_changes, ack_changes, applies) in cases.items():
            with self.subTest(label):
                for p in self.temp.iterdir():
                    p.unlink()
                victim = self.old("scratch-old-unnamed.log")
                (self.world.state / sb.TEMP_JANITOR_FILE).unlink(missing_ok=True)
                engine, source = self.daemon({"daemon_apply": True}, acked=True)
                gate_path, ack_path = self.world.state / sb.TEMP_JANITOR_GATE_FILE, self.world.state / sb.TEMP_JANITOR_ACK_FILE
                gate_path.write_text(json.dumps(dict(json.loads(gate_path.read_text()), **gate_changes)))
                ack_path.write_text(json.dumps(dict(json.loads(ack_path.read_text()), **ack_changes)))
                self.go(source)
                self.assertEqual(not victim.exists(), applies, label)
                self.now += 7 * 86400
                engine.now = lambda: self.now

    def test_s4_two_previews_under_one_rule_carry_different_nonces(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        gate = self.world.state / sb.TEMP_JANITOR_GATE_FILE
        self.go(source)
        first = json.loads(gate.read_text())
        self.week(engine)
        self.go(source)   # the first preview was never handed over: a second one is made under the same rule
        second = json.loads(gate.read_text())
        self.assertEqual(first["rule"], second["rule"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertTrue(first["nonce"] and second["nonce"])

    def test_s4_a_yes_cannot_be_recorded_for_a_preview_nobody_was_told_of(self):
        self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)   # the gate exists with sent false: the message was never handed over
        gate = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())
        self.assertFalse(gate["sent"])
        code, err = self.record_ack(gate["preview"])
        self.assertEqual(code, 1)
        self.assertFalse((self.world.state / sb.TEMP_JANITOR_ACK_FILE).exists())

    def test_s4_a_rule_that_changes_and_comes_back_needs_a_new_yes(self):
        victim = self.old("scratch-old-unnamed.log")
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.go(source)
        self.preview_facts(engine, source)
        first = json.loads((self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text())["preview"]
        self.assertEqual(self.record_ack(first)[0], 0)
        engine, source = self.daemon({"daemon_apply": True, "days": 8}, acked=False)   # the rule moves to B
        self.week(engine)
        self.go(source)
        self.assertFalse((self.world.state / sb.TEMP_JANITOR_ACK_FILE).exists(), "a new preview ends the earlier yes")
        self.preview_facts(engine, source)
        engine, source = self.daemon({"daemon_apply": True}, acked=False)   # and back to A, same names
        self.week(engine)
        self.go(source)
        self.preview_facts(engine, source)
        self.week(engine)
        self.go(source)
        self.assertTrue(victim.exists(), "the old yes does not unlock it again")
        self.assertEqual(self.events("temp.removed"), [])

    def test_s8_a_name_is_only_in_the_private_audit_and_the_message(self):
        engine, source = self.daemon({"daemon_apply": True}, acked=False)
        self.old("distinctive-fixture-name.log")
        self.go(source)
        facts = self.preview_facts(engine, source)
        self.assertIn("distinctive-fixture-name.log", facts[0].summary)
        for event in ("janitor.preview",):
            self.assertEqual(len(self.events(event)), 1)
        public = json.dumps([e for e in self.world.audit() if e["event"] in ("janitor.preview", "janitor.ack")])
        public += (self.world.state / sb.TEMP_JANITOR_GATE_FILE).read_text()
        self.assertNotIn("distinctive-fixture-name", public)

    def test_a_failure_inside_the_run_is_one_janitor_error_and_removes_nothing(self):
        victim = self.old("old-file.txt")
        engine, source = self.daemon({"daemon_apply": True})
        with mock.patch.object(self.jan, "run", side_effect=RuntimeError("boom")):
            self.go(source)
        self.assertTrue(victim.exists())
        self.assertEqual(len(self.events("janitor.error")), 1)

    def test_s12_the_janitors_sweep_state_is_its_own_and_never_the_main_threads(self):
        engine, source = self.daemon({"daemon_apply": True})
        inside, leave, seen = threading.Event(), threading.Event(), {}

        def janitor_thread():
            with sb.worktree_sweep(engine):
                seen["own"] = engine._wt_sweep
                inside.set()
                leave.wait(10)
            seen["after"] = engine._wt_sweep
        worker = threading.Thread(target=janitor_thread)
        worker.start()
        self.assertTrue(inside.wait(10))
        try:
            self.assertFalse(engine._wt_sweep, "the main thread is not in a sweep because another thread is")
            self.assertTrue(seen["own"])
            engine._wt_session_index = ({"x": "/elsewhere"}, True)   # the main thread's own state
        finally:
            leave.set()
            worker.join(10)
        self.assertFalse(seen["after"])
        self.assertEqual(engine._wt_session_index, ({"x": "/elsewhere"}, True), "the janitor leaving its sweep did not clear the main thread's cache")
        engine._wt_session_index = None
        with mock.patch.object(sb.V2Client, "session_list_page", wraps=engine.v2.session_list_page) as listed:   # a main-thread check reads the current list
            sb.worktree_session_index(engine)
            sb.worktree_session_index(engine)
        self.assertEqual(listed.call_count, 2)

    def test_s13_a_session_started_in_an_entry_after_the_scan_keeps_it(self):
        victim = self.old("old-file-dir", directory=True)
        engine, source = self.daemon({"daemon_apply": True})
        engine.discover(self.now)
        sid = self.world.sessions["coder"]
        self.world.v2.sessions[sid]["directory"] = str(self.home)
        real, calls = self.jan.judge, [0]

        def judge(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:   # after the scan judged it, before the removal re-judges it
                self.world.v2.sessions[sid]["directory"] = str(victim)
            return real(path, *args, **kwargs)
        with mock.patch.object(self.jan, "judge", judge):
            self.go(source)
        self.assertGreaterEqual(calls[0], 2)
        self.assertTrue(victim.exists())
        self.assertEqual(self.events("temp.removed"), [])
        self.assertIn("kept old-file-dir: it changed since the scan", self.events("janitor.summary")[0]["detail"]["notes"])

    def test_s10_no_gh_and_no_network(self):
        self.old("old-file.txt")
        engine, source = self.daemon({"daemon_apply": True})
        self.go(source)
        self.assertTrue(self.calls)
        for argv in self.calls:
            self.assertNotIn(os.path.basename(argv[0]), ("gh", "curl", "git", "ssh"))
            self.assertNotIn("api", argv[:2] if os.path.basename(argv[0]) == "gh" else [])

    def test_s11_the_docs_name_the_one_apply_path_and_the_plist_example_has_no_janitor(self):
        text = (Path(sb.__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        self.assertIn("There is one apply path (#958)", text)
        self.assertIn("<string>/home/user/.local/bin/fleet-switchboard disk prune</string></array>", text)

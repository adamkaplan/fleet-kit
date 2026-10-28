"""Test classes moved out of bin/test-switchboard (text unchanged): cases_13."""
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
from support.loader import _load, captured, copy_entry, sb, switchboard_source
from support.orders_and_adapters import FakePruneDocker
from support.paths import BIN, REPO
from support.rigs import World
from support.service_and_merge import V2TransportBase
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE, FakeHTTP


class TestCpuGuard(Base):
    """The CPU guard (P0 CPU item D): one os.getloadavg per pass, a status line, one wake per episode, no process in a normal pass."""

    PS = "  60.0 /usr/local/bin/node\n  25.5 /Applications/Docker.app/Contents/MacOS/com.docker.backend\n  12.0 /usr/bin/python3\n  2.0 /bin/zsh\n  garbage\n"

    def setUp(self):
        super().setUp()
        self.load = [86.0]    # the five-minute average (what the alarm reads)
        self.load1 = [None]   # the one-minute average; None = the same
        self.cores = 14
        self.loads_read = []
        self.world = World(self, agents=("coder",), config={})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.engine = self.world.engine
        self.engine.discover(self.world.clock.now())
        self.cg = sb.load_cpu_guard()
        self.ps_calls = []

        def getloadavg():
            self.loads_read.append(self.load[0])
            return (self.load1[0] if self.load1[0] is not None else self.load[0], self.load[0], self.load[0])
        patcher = mock.patch.object(os, "getloadavg", getloadavg)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(os, "cpu_count", lambda: self.cores)
        patcher2.start()
        self.addCleanup(patcher2.stop)

        def ps(argv, **kwargs):
            self.ps_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, self.PS, "")
        self.engine.cpu_ps_runner = ps

    def settle(self, engine=None):
        """Wait for the ps thread of the engine's CPU source (it runs off the pass)."""
        for source in (engine or self.engine).sources:
            if isinstance(source, sb.CpuFacts) and source.thread is not None:
                source.thread.join(10)

    def facts(self, engine=None):
        engine = engine or self.engine
        found, problems = engine.collect_facts()
        self.assertEqual(problems, [])
        return [f for f in found if f.kind == "cpu"]

    def audits(self, name):
        return [e for e in self.world.audit() if e["event"] == name]

    def run_for(self, minutes, step=30, engine=None):
        """Passes every `step` seconds for `minutes`; returns the facts of the last pass."""
        last = []
        for _ in range(int(minutes * 60 / step)):
            self.world.tick(step)
            last = self.facts(engine)
        return last

    def test_s1_the_status_line_reads_load_cores_and_ratio_marks_high_only_above_the_guard_and_starts_no_process(self):
        cfg = self.cg.settings(None)
        self.load1[0] = None
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("a process was started")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")):
            self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "load 86 on 14 cores (6.1x) [HIGH]")
            self.load[0] = 4.0
            self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "load 4 on 14 cores (0.3x)")
            self.load[0] = 42.0
            self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "load 42 on 14 cores (3.0x)", "exactly at the guard is not above it")
            self.world.tick(60)
            self.facts()   # a daemon pass with a normal load
        self.assertEqual(self.ps_calls, [])

    def test_s1_status_shows_the_cpu_line(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.assertIn("cpu        load 86 on 14 cores (6.1x) [HIGH]", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))

    def test_s2_one_wake_per_episode_after_the_minutes_and_one_bounded_ps(self):
        self.assertEqual(self.run_for(4), [], "4 minutes above the guard: not yet")
        self.assertEqual(self.ps_calls, [])
        keys = set()
        for _ in range(40):
            for fact in self.run_for(0.5):
                keys.add(fact.key)
        self.assertEqual(len(keys), 1, "the same key on every later pass")
        self.settle()
        (fact,) = self.facts()
        self.assertIn("cpu high: load 86 on 14 cores (6.1x over five minutes", fact.summary)
        self.assertIn("top: node 60%", fact.summary)
        self.assertEqual(len(self.audits("cpu.high")), 1)
        self.assertEqual(self.ps_calls, [self.cg.PS_ARGV], "exactly one ps in the whole episode")
        self.assertNotIn("/Applications", fact.summary, "only base names")

    def test_s3_the_episode_ends_below_two_thirds_of_the_guard_and_a_second_rise_is_a_second_episode(self):
        self.run_for(6)
        self.settle()
        (first,) = self.facts()
        self.load[0] = 35.0   # 2.5x: between the re-arm ratio and the guard
        self.assertEqual([f.key for f in self.run_for(1)], [first.key])
        self.assertEqual(self.audits("cpu.recovered"), [])
        self.load[0] = 20.0   # 1.4x
        self.assertEqual(self.run_for(1), [])
        self.assertEqual(len(self.audits("cpu.recovered")), 1)
        self.load[0] = 90.0
        self.run_for(6)
        self.settle()
        (second,) = self.facts()
        self.assertNotEqual(second.key, first.key)
        self.assertEqual(len(self.audits("cpu.high")), 2)
        self.assertEqual(len(self.ps_calls), 2, "one ps per episode")

    def test_a_dip_below_the_guard_before_the_minutes_resets_the_clock(self):
        self.run_for(4)
        self.load[0] = 10.0
        self.run_for(1)
        self.load[0] = 86.0
        self.assertEqual(self.run_for(4), [])
        self.assertEqual(self.audits("cpu.high"), [])

    def test_s4_a_restart_does_not_begin_a_second_episode(self):
        self.run_for(6)
        self.settle()
        (first,) = self.facts()
        restarted = self.world.new_engine("daemon")
        restarted.discover(self.world.clock.now())
        restarted.cpu_ps_runner = self.engine.cpu_ps_runner
        facts = self.run_for(2, engine=restarted)
        self.assertEqual([f.key for f in facts], [first.key])
        self.assertEqual(len(self.audits("cpu.high")), 1)
        self.assertEqual(len(self.ps_calls), 1)

    def test_s5_false_turns_everything_off_and_the_load_is_not_read(self):
        self.engine.config["cpu_guard"] = False
        self.assertEqual(self.run_for(10), [])
        self.assertEqual(self.loads_read, [])
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "cpu_guard": False})
        self.assertNotIn("cpu        ", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))
        self.assertEqual(self.loads_read, [])

    def test_s6_the_line_adds_the_heavy_slots_only_from_a_working_heavy_slots_module(self):
        cfg = self.cg.settings(None)
        read = self.cg.reading()
        plain = "load 86 on 14 cores (6.1x) [HIGH]"
        module = self.home / "heavy_slots.py"
        with mock.patch.object(self.cg, "HEAVY_SLOTS_PATH", str(self.home / "absent.py")):
            self.assertIsNone(self.cg.slots_text({}, self.world.state), "no heavy module yet: no slots text")
        good = ("def settings(raw):\n    return (raw or {}).get('slots', 3)\n"
                "def holders(state_dir, maximum):\n    return [{'slot': 1, 'name': 'coder-live', 'pid': 7, 'command': 'make test', 'since': 1000.0}, "
                "{'slot': 2, 'name': 'coder\\x1b[2J', 'pid': 8, 'command': 'npm', 'since': 'soon'}]\n")
        for label, body, want in (("a good module", good, "heavy slots 2/3 (coder-live make test 6m, coder??2J npm)"),
                                  ("no holders", "def settings(raw):\n    return 3\ndef holders(state_dir, maximum):\n    return []\n", "heavy slots 0/3"),
                                  ("a raising module", "def settings(raw):\n    raise RuntimeError('x')\ndef holders(state_dir, maximum):\n    return []\n", None),
                                  ("bad types", "def settings(raw):\n    return 'three'\ndef holders(state_dir, maximum):\n    return []\n", None),
                                  ("a module that does not parse", "def settings(:\n", None)):
            with self.subTest(label):
                module.write_text(body)
                with mock.patch.object(self.cg, "HEAVY_SLOTS_PATH", str(module)):
                    text = self.cg.slots_text({}, self.world.state, now=1000.0 + 360)
                self.assertEqual(text, want)
                self.assertEqual(self.cg.status_line(cfg, read, text), plain + ("; " + want if want else ""))
        module.write_text(good)
        with mock.patch.object(self.cg, "HEAVY_SLOTS_PATH", str(module)):
            self.assertIn("heavy slots 2/5", self.cg.slots_text({"heavy": {"slots": 5}}, self.world.state, now=1500.0), "the module's own settings read the config's heavy section")

    def test_a_one_minute_spike_never_wakes_anyone_only_the_five_minute_average_does(self):
        self.load[0] = 10.0        # the five-minute average is fine
        self.load1[0] = 200.0      # the one-minute average is a spike
        self.assertEqual(self.run_for(20), [])
        self.assertEqual(self.audits("cpu.high"), [])
        self.assertIn("[HIGH]", self.cg.status_line(self.cg.settings(None), self.cg.reading()), "the displayed one-minute load still shows it")

    def test_the_ps_runs_off_the_pass_so_a_hung_ps_never_delays_it(self):
        release, entered = threading.Event(), threading.Event()

        def hung(argv, **kwargs):
            entered.set()
            release.wait(30)
            return subprocess.CompletedProcess(argv, 0, self.PS, "")
        self.engine.cpu_ps_runner = hung
        self.run_for(6)   # the passes return while the ps is still blocked: if a pass waited for it, this would not come back until `release` (30 s)
        self.assertTrue(entered.wait(10))
        self.assertFalse(release.is_set())
        (fact,) = self.facts()
        self.assertNotIn("top:", fact.summary, "the categories are not there yet")
        release.set()
        self.settle()
        (fact,) = self.facts()
        self.assertIn("top: node 60%", fact.summary)

    def test_a_slow_ps_of_an_earlier_episode_never_overwrites_a_later_ones_text(self):
        self.run_for(6)
        self.settle()
        stale = threading.Event()
        gate = threading.Event()

        def slow(argv, **kwargs):
            gate.wait(30)
            return subprocess.CompletedProcess(argv, 0, "  99.0 /usr/bin/stale\n", "")
        self.engine.cpu_ps_runner = slow
        self.load[0] = 20.0
        self.run_for(1)   # the first episode ends
        self.load[0] = 90.0
        self.run_for(6)   # a second begins; its ps is blocked
        self.engine.cpu_ps_runner = self.engine.cpu_ps_runner
        source = [s for s in self.engine.sources if isinstance(s, sb.CpuFacts)][0]
        old_thread = source.thread
        source.thread = None   # the engine's view of the episode moves on
        gate.set()
        old_thread.join(10)
        self.assertEqual(source.detail, "", "a thread that is no longer the current one wrote nothing")

    def test_no_chief_of_staff_means_no_episode_until_one_is_running(self):
        self.engine.discovery.agents.pop("cos", None)
        self.assertEqual(self.run_for(7), [])
        self.assertEqual(self.audits("cpu.high"), [])
        self.world.add_agent("cos2", fleet={"role": "chief-of-staff"})
        self.engine.discover(self.world.clock.now())
        facts = self.run_for(6)
        self.assertEqual(len({fact.key for fact in facts}), 1, "the episode begins once a Chief of Staff exists, with one key")
        self.assertTrue(facts)
        self.assertEqual(len(self.audits("cpu.high")), 1)

    def test_the_episode_file_is_atomic_0600_bounded_and_a_failed_save_is_said_and_tried_again(self):
        self.run_for(6)
        path = self.world.state / self.cg.STATE_FILE
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(list(self.world.state.glob(self.cg.STATE_FILE + ".*.tmp")), [])
        for bad in ("not json", "[]", "x" * 10000, json.dumps({"episode": "soon"})):
            path.write_text(bad)
            self.assertIsNone(self.cg.load_episode(self.world.state), bad[:12])
        self.load[0] = 20.0
        with mock.patch.object(self.cg.os, "replace", side_effect=OSError("disk full")):
            self.run_for(1)   # the episode ends but the file cannot be written
        self.assertEqual(len([e for e in self.world.audit() if e["event"] == "cpu.guard_file"]), 1)
        self.assertEqual(list(self.world.state.glob(self.cg.STATE_FILE + ".*.tmp")), [])
        self.run_for(1)   # the next pass tries again, and now it works
        self.assertEqual(self.cg.load_episode(self.world.state), None)

    def test_a_ps_that_fails_or_a_runaway_holders_list_changes_nothing_but_the_text(self):
        self.engine.cpu_ps_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no")
        self.run_for(6)
        self.settle()
        (fact,) = self.facts()
        self.assertNotIn("top:", fact.summary)
        module = self.home / "heavy_slots.py"
        module.write_text("def settings(raw):\n    return 1000\ndef holders(state_dir, maximum):\n    return range(10 ** 9)\n")
        with mock.patch.object(self.cg, "HEAVY_SLOTS_PATH", str(module)):
            self.assertEqual(self.cg.slots_text({}, self.world.state), "heavy slots 1001/1000", "the count is cut at one past the cap, never walked")
        module.write_text("import sys\nsys.exit(3)\n")
        with mock.patch.object(self.cg, "HEAVY_SLOTS_PATH", str(module)):
            self.assertIsNone(self.cg.slots_text({}, self.world.state), "a module that exits takes nobody down")

    def test_bad_inputs_are_said_and_never_guessed_a_missing_core_count_is_not_one_core(self):
        cfg = self.cg.settings(None)
        for label, cores, want in (("no core count", None, "load 20, cores unknown"), ("zero cores", 0, "load 20, cores unknown"), ("negative cores", -3, "load 20, cores unknown"),
                                   ("a core count that is not a number", "many", "load 20, cores unknown")):
            with self.subTest(label):
                self.cores, self.load[0] = cores, 20.0
                self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), want, "no ratio and no marker: never one core")
                self.assertEqual(self.run_for(10), [], "a normal load on an unknown core count wakes nobody")
                self.assertEqual(self.audits("cpu.high"), [])
        self.cores = 14
        for label, value, reason in (("nan", float("nan"), "not a finite number"), ("inf", float("inf"), "not a finite number"), ("negative", -1.0, "not a finite number")):
            with self.subTest(label):
                self.load[0] = value
                line = self.cg.status_line(cfg, self.cg.reading())
                self.assertEqual(line, "unreadable: the load average is not a finite number of at least 0")
                self.assertEqual(self.run_for(10), [], "a bad load wakes nobody and raises nothing")
        self.load[0] = 86.0
        with mock.patch.object(os, "getloadavg", side_effect=OSError("no load average")):
            self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "unreadable: OSError: no load average")
        with mock.patch.object(os, "getloadavg", return_value=()):
            self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "unreadable: IndexError: tuple index out of range")
        self.assertEqual(self.cg.status_line(cfg, self.cg.reading()), "load 86 on 14 cores (6.1x) [HIGH]", "and a good reading is as before")

    def test_status_says_when_the_load_or_the_cores_are_unknown_instead_of_leaving_the_line_out(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.load[0] = float("nan")
        self.assertIn("cpu        unreadable: the load average is not a finite number", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))
        self.load[0], self.cores = 20.0, None
        self.assertIn("cpu        load 20, cores unknown", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))

    def test_s7_the_config_key_is_validated(self):
        for ok in (None, False, {}, {"high_ratio": 4.5, "minutes": 10}, {"minutes": 1}):
            sb.validate_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, cpu_guard=ok), "t")
        self.assertEqual({k: v for k, v in self.cg.settings(None).items() if k != "rearm_ratio"}, {"high_ratio": 3.0, "minutes": 5})
        self.assertIsNone(self.cg.settings(False))
        got = self.cg.settings({"high_ratio": 4.5, "minutes": 10})
        self.assertEqual((got["high_ratio"], got["minutes"], got["rearm_ratio"]), (4.5, 10, 3.0))
        for bad in (True, 5, [], {"high_ratio": 0}, {"high_ratio": -1}, {"high_ratio": "3"}, {"minutes": 0}, {"minutes": 500}, {"nope": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(sb.ConfigError):
                    sb.validate_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, cpu_guard=bad), "t")

    def test_an_unreadable_load_says_nothing_and_a_failing_ps_still_wakes(self):
        with mock.patch.object(os, "getloadavg", side_effect=OSError("no")):
            self.assertEqual(self.run_for(7), [])
            self.assertTrue(self.cg.status_line(self.cg.settings(None), self.cg.reading()).startswith("unreadable: OSError"))
        self.settle()
        self.engine.cpu_ps_runner = lambda argv, **kw: (_ for _ in ()).throw(OSError("no ps"))
        self.run_for(6)
        self.settle()
        (fact,) = self.facts()
        self.assertNotIn("top:", fact.summary)
        self.assertIn("cpu high", fact.summary)

    def test_the_categories_are_summed_by_base_name_and_hostile_names_are_cleaned(self):
        cats = self.cg.categories("  10 /a/node\n  5 /b/node\n  3 \x1b[2Jevil\n  1 /c/x y\n")
        self.assertEqual(cats[0], ("node", 15.0))
        self.assertNotIn("\x1b", self.cg.detail(cats))


class TestDiskGuard(Base):
    """The disk guard: one stat of the state directory's volume per pass; the Chief of Staff is told once per episode below disk_guard.min_free_gb; `status`
    says how much is free; `disk prune` runs exactly two docker commands and refuses without docker."""

    GB = 10 ** 9

    def setUp(self):
        super().setUp()
        self.free = [200 * self.GB]
        self.stats = []
        self.world = World(self, agents=("coder",), config={})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.engine = self.world.engine
        self.engine.discover(self.world.clock.now())

        self.after_prune = None

        def usage(path):
            self.stats.append(str(path))
            free = self.after_prune if self.after_prune is not None and len(self.stats) > 1 else self.free[0]
            return collections.namedtuple("usage", "total used free")(1000 * self.GB, 1000 * self.GB - free, free)
        patcher = mock.patch.object(sb.shutil, "disk_usage", usage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def facts(self, engine=None):
        engine = engine or self.engine
        found, problems = engine.collect_facts()
        self.assertEqual(problems, [])
        return [f for f in found if f.kind == "disk"]

    def audits(self, name):
        return [e for e in self.world.audit() if e["event"] == name]

    def test_the_config_key_is_validated_and_off_means_off(self):
        for ok in (None, False, {}, {"min_free_gb": 80}, {"min_free_gb": 80, "hard_free_gb": 20}, {"hard_free_gb": 0}):
            sb.validate_disk_guard(ok, "t")
        for bad in (True, 5, [], {"min_free_gb": -1}, {"min_free_gb": "50"}, {"nope": 1}, {"min_free_gb": 5, "hard_free_gb": 10}, {"hard_free_gb": 60}):
            with self.subTest(bad=bad):
                with self.assertRaises(sb.ConfigError):
                    sb.validate_disk_guard(bad, "t")
        self.assertEqual(sb.disk_guard_settings({}), {"min_free_gb": 50, "hard_free_gb": 10})
        self.assertIsNone(sb.disk_guard_settings({"disk_guard": False}))

    def test_the_status_line_says_how_much_is_free_and_when_it_is_low(self):
        self.free[0] = 238 * self.GB
        self.assertEqual(sb.disk_status_line({}, self.world.state), "238 GB free (24%)")   # 238 of the 1000 GB the stand-in volume has, 23.8%
        self.free[0] = 30 * self.GB
        self.assertEqual(sb.disk_status_line({}, self.world.state), "30 GB free (3%): LOW (below 50 GB; hard line 10 GB)")
        self.free[0] = 5 * self.GB
        self.assertIn("VERY LOW", sb.disk_status_line({}, self.world.state))
        self.assertIn("below 80 GB", sb.disk_status_line({"disk_guard": {"min_free_gb": 80}}, self.world.state))

    def test_status_shows_the_disk_line_and_not_when_the_guard_is_off(self):
        self.free[0] = 238 * self.GB
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.assertIn("disk       238 GB free", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "disk_guard": False})
        self.assertNotIn("disk       ", sb.format_status(sb.collect_status(self.world.route, self.world.clock.now)))

    def test_one_stat_per_pass_and_nothing_while_the_disk_is_fine(self):
        for _ in range(5):
            self.assertEqual(self.facts(), [])
        self.assertEqual(len(self.stats), 5, "one disk_usage per collection: no walk")
        self.assertEqual(self.audits("disk.low"), [])

    def test_the_chief_of_staff_is_told_once_per_episode_and_the_episode_ends_above_the_threshold_plus_ten_percent(self):
        self.free[0] = 40 * self.GB
        (first,) = self.facts()
        self.assertEqual((first.recipient, first.kind, first.sender), ("cos", "disk", "switchboard"))
        self.assertIn("disk low: 40 GB free", first.summary)
        for free in (38, 30, 52, 54):                       # lower, and back up but not above 55
            self.free[0] = free * self.GB
            self.world.tick(30)
            (again,) = self.facts()
            self.assertEqual(again.key, first.key, "the same episode: the same key, so it is delivered once")
        self.assertEqual(len(self.audits("disk.low")), 1, "audited once")
        self.free[0] = 56 * self.GB                         # above 50 + 10%
        self.assertEqual(self.facts(), [])
        self.assertEqual(len(self.audits("disk.recovered")), 1)
        self.world.tick(100)
        self.free[0] = 45 * self.GB
        (second,) = self.facts()
        self.assertNotEqual(second.key, first.key, "a new episode: a new fact")
        self.assertEqual(len(self.audits("disk.low")), 2)

    def test_two_episodes_that_recover_at_the_same_figure_are_both_audited(self):
        for episode in range(2):
            self.free[0] = 40 * self.GB
            self.world.tick(100)
            self.facts()
            self.free[0] = 60 * self.GB   # the same figure each time
            self.facts()
        self.assertEqual(len(self.audits("disk.low")), 2)
        self.assertEqual(len(self.audits("disk.recovered")), 2, "not swallowed as a repeat of the same detail")

    def test_a_restart_does_not_begin_a_second_episode_and_a_hard_line_is_said_once(self):
        self.free[0] = 40 * self.GB
        (first,) = self.facts()
        self.assertEqual(json.loads((self.world.state / sb.DISK_GUARD_FILE).read_text())["low"], True)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.world.state / sb.DISK_GUARD_FILE)).st_mode), 0o600)
        self.world.tick(60)
        restarted = self.world.new_engine("daemon")
        restarted.discover(self.world.clock.now())
        (again,) = self.facts(restarted)
        self.assertEqual(again.key, first.key)
        self.assertEqual(len(self.audits("disk.low")), 1)
        self.free[0] = 8 * self.GB
        self.facts(restarted)
        self.facts(restarted)
        self.assertEqual(len(self.audits("disk.hard")), 1, "worse inside the episode: said once, not a second wake")

    def test_the_config_key_is_checked_by_validate_config_too(self):
        sb.validate_config({"opencode": FAKE_OPENCODE, "disk_guard": {"min_free_gb": 80}})
        for bad in ("yes", {"hard_free_gb": 99}):
            with self.assertRaises(sb.ConfigError):
                sb.validate_config({"opencode": FAKE_OPENCODE, "disk_guard": bad})

    def test_with_no_chief_of_staff_no_episode_begins_until_there_is_one_and_an_unreadable_volume_is_audited_once(self):
        solo = World(self, agents=("coder",), config={})
        solo.engine.discover(solo.clock.now())
        self.free[0] = 5 * self.GB
        found, _ = solo.engine.collect_facts()
        self.assertEqual([f for f in found if f.kind == "disk"], [])
        self.assertFalse((solo.state / sb.DISK_GUARD_FILE).exists(), "no episode was opened: nobody to tell")
        solo.add_agent("cos", fleet={"role": "chief-of-staff"})
        solo.engine.discover(solo.clock.now())
        found, _ = solo.engine.collect_facts()
        self.assertEqual(len([f for f in found if f.kind == "disk"]), 1, "the first pass with a Chief of Staff tells it")
        with mock.patch.object(sb.shutil, "disk_usage", side_effect=OSError("gone")):
            self.facts()
            self.facts()
        self.assertEqual(len(self.audits("disk.unreadable")), 1)

    def test_an_episode_that_starts_below_the_hard_line_and_a_zero_size_volume(self):
        self.free[0] = 4 * self.GB
        (fact,) = self.facts()
        self.assertEqual(len(self.audits("disk.low")), 1)
        self.assertTrue(self.audits("disk.low")[0]["detail"]["hard"])
        self.assertEqual(self.audits("disk.hard"), [], "it began hard: nothing worse to say")
        with mock.patch.object(sb.shutil, "disk_usage", lambda path: collections.namedtuple("u", "total used free")(0, 0, 7 * self.GB)):
            self.assertEqual(sb.disk_status_line({}, self.world.state), "7 GB free: VERY LOW (below 50 GB; hard line 10 GB)")

    def test_a_failed_save_of_the_episode_is_said_and_leaves_no_temp_file(self):
        self.free[0] = 40 * self.GB
        with mock.patch.object(sb.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(len(self.facts()), 1, "the guard still works in memory")
        self.assertEqual(list(self.world.state.glob("disk-guard.json.*.tmp")), [])
        self.assertEqual(len(self.audits("disk.guard_file")), 1)

    def test_a_guard_that_is_off_or_a_volume_that_cannot_be_read_tells_nobody(self):
        self.free[0] = 1 * self.GB
        self.engine.config["disk_guard"] = False
        self.assertEqual(self.facts(), [])
        self.engine.config["disk_guard"] = None
        with mock.patch.object(sb.shutil, "disk_usage", side_effect=OSError("gone")):
            self.assertEqual(self.facts(), [])
            self.assertIsNone(sb.disk_status_line({}, self.world.state))

    # -- disk prune

    def fake_docker(self, name="docker"):
        path = self.home / name
        path.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(str(path), 0o755)
        return str(path)

    def prune(self, *argv, exits=(0, 0, 0), outputs=("Total reclaimed space: 1.2GB", "Total reclaimed space: 300MB", "Total reclaimed space: 5MB"), docker=None, fake=None):
        fake = fake or FakePruneDocker(exits=exits, outputs=outputs)
        path = docker or self.fake_docker()
        with mock.patch.dict(os.environ), captured() as (out, err):
            os.environ.pop("DOCKER_API_VERSION", None)   # a developer's own pin must not decide a test
            code = sb.main(["disk", "prune", "--docker", path, *argv], runner=fake, now=self.world.clock.now)
        self.last_fake = fake
        return code, out.getvalue(), err.getvalue(), [[path] + c for c in fake.prune_calls]

    def test_prune_runs_exactly_the_three_docker_commands_and_prints_what_was_reclaimed(self):
        docker = self.fake_docker()
        self.after_prune = 215 * self.GB   # the second reading of the volume, after the prune
        code, out, err, calls = self.prune(docker=docker)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [[docker, "builder", "prune", "-f", "--filter", "until=168h"], [docker, "image", "prune", "-a", "-f", "--filter", "until=168h"],
                                 [docker, "volume", "prune", "-f"]])
        for argv in calls:
            self.assertFalse({"system", "container", "--volumes", "--all"} & set(argv), "never a container or a system prune")
        self.assertEqual([a for a in calls if "volume" in a][0].count("-a"), 0, "the volume prune never passes -a")
        self.assertIn("builder prune: reclaimed 1.2GB", out)
        self.assertIn("image prune: reclaimed 300MB", out)
        self.assertIn("volume prune: reclaimed 5MB", out)
        self.assertRegex(out, r"disk: 200 GB free before, 215 GB after")

    def test_a_dry_run_prints_the_commands_and_runs_none(self):
        code, out, err, calls = self.prune("--dry-run")
        self.assertEqual((code, calls), (0, []))
        self.assertIn("builder prune -f --filter until=168h", out)
        self.assertIn("image prune -a -f --filter until=168h", out)

    VOLUMES = (("anon1", True, True, "1.5MB"), ("anon2", True, True, "1.5MB"), ("named", False, True, "9MB"), ("inuse", True, False, "4MB"), ("bad name\x1b[2J", True, True, "1MB"))

    def test_a_dry_run_lists_the_candidate_anonymous_volumes_and_runs_no_prune(self):
        fake = FakePruneDocker(volumes=self.VOLUMES)
        code, out, err, calls = self.prune("--dry-run", fake=fake)
        self.assertEqual((code, fake.prune_calls), (0, []))
        self.assertIn("candidate anonymous volumes: 2 (3.0 MB)", out)
        self.assertIn("  anon1", out)
        self.assertNotIn("named", out)
        self.assertNotIn("inuse", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("would run docker volume prune -f (never -a)", out)

    def test_an_unsafe_or_unreadable_server_refuses_the_volume_prune_and_the_other_two_still_run(self):
        cases = (("22.0.1", {"server_api": "1.41", "client_api": "1.41"}, sb.EXIT_OK), ("", {"version_code": 1}, sb.EXIT_ERROR),
                 ("29.7.2", {"client_api": "1.41"}, sb.EXIT_OK),                       # a new server reached by an old-API client
                 ("29.7.2", {"platform": "podman 5.1"}, sb.EXIT_OK))                  # a compatible program, not Docker
        for version, extra, want in cases:
            with self.subTest(version=version, extra=extra):
                fake = FakePruneDocker(version=version or "29.7.2", exits=(0, 0), outputs=("", ""), **extra)
                code, out, err, calls = self.prune(fake=fake)
                self.assertEqual([c[1:3] for c in calls], [["builder", "prune"], ["image", "prune"]])
                self.assertIn("volume prune: refused", out)
                self.assertEqual(code, want)
        with mock.patch.dict(os.environ, {"DOCKER_API_VERSION": "1.41"}):
            fake = FakePruneDocker(exits=(0, 0), outputs=("", ""))
            with captured() as (out_, err_):
                sb.main(["disk", "prune", "--docker", self.fake_docker()], runner=fake, now=self.world.clock.now)
            out, calls = out_.getvalue(), fake.prune_calls
        self.assertEqual(len(calls), 2)
        self.assertIn("DOCKER_API_VERSION=1.41", out)
        fake = FakePruneDocker(version="23.0.0", server_api="1.42", client_api="1.42", exits=(0, 0, 0), outputs=("", "", ""))
        self.assertEqual(len(self.prune(fake=fake)[3]), 3, "23.0 and API 1.42 are the first that run it")

    def test_a_dry_run_on_an_unsafe_server_says_refused_and_does_not_say_it_would_run(self):
        code, out, err, calls = self.prune("--dry-run", fake=FakePruneDocker(version="22.0.1", server_api="1.41", client_api="1.41"))
        self.assertIn("volume prune: refused", out)
        self.assertIn("the other two would still run", out)
        self.assertNotIn("would run docker volume prune", out)
        self.assertNotIn("the other two ran", out)

    def test_a_dry_run_caps_its_list_counts_past_the_examine_limit_and_says_when_a_size_or_list_is_unreadable(self):
        many = tuple(("v%03d" % i, True, True, "1MB") for i in range(25))
        code, out, err, calls = self.prune("--dry-run", fake=FakePruneDocker(volumes=many))
        self.assertIn("candidate anonymous volumes: 25 (25.0 MB)", out)
        self.assertIn("5 more", out)
        with mock.patch.object(sb, "DISK_VOLUME_LIMIT", 10):
            code, out, err, calls = self.prune("--dry-run", fake=FakePruneDocker(volumes=many))
        self.assertIn("candidate anonymous volumes: >=10 ", out)
        code, out, err, calls = self.prune("--dry-run", fake=FakePruneDocker(volumes=many, df_fails=True))
        self.assertIn("25 (size not read)", out)

    def test_a_dry_run_whose_volume_list_is_unreadable_says_so_and_still_names_the_command(self):
        real = FakePruneDocker()

        def runner(argv, **kwargs):
            if argv[1:3] == ["volume", "ls"]:
                return subprocess.CompletedProcess(argv, 1, "", "daemon down")
            return real(argv, **kwargs)
        with captured() as (out, err):
            code = sb.main(["disk", "prune", "--dry-run", "--docker", self.fake_docker()], runner=runner, now=self.world.clock.now)
        self.assertIn("candidate anonymous volumes: not readable", out.getvalue())
        self.assertIn("would run docker volume prune -f", out.getvalue())

    def test_a_failing_or_raising_volume_prune_is_said_exits_with_an_error_and_the_first_two_still_ran(self):
        fake = FakePruneDocker(exits=(0, 0, 1), outputs=("", "", ""))
        code, out, err, calls = self.prune(fake=fake)
        self.assertEqual((code, len(calls)), (sb.EXIT_ERROR, 3))
        self.assertIn("volume prune: failed (exit 1)", out)
        real = FakePruneDocker()

        def runner(argv, **kwargs):
            if argv[1:3] == ["volume", "prune"]:
                raise subprocess.TimeoutExpired(argv, 600)
            return real(argv, **kwargs)
        with captured() as (out, err):
            code = sb.main(["disk", "prune", "--docker", self.fake_docker()], runner=runner, now=self.world.clock.now)
        self.assertEqual(code, sb.EXIT_ERROR)
        self.assertIn("volume prune: failed", out.getvalue())

    def test_the_volume_prune_never_carries_all_in_any_server_version(self):
        for version in ("23.0.0", "27.3.1", "29.7.2"):
            fake = FakePruneDocker(version=version)
            self.prune(fake=fake)
            self.assertIn(["volume", "prune", "-f"], fake.prune_calls)
            self.assertEqual([c for c in fake.prune_calls if ("-a" in c and "volume" in c) or "--all" in c], [])

    def test_the_docs_state_the_three_commands_the_version_gate_and_the_reason(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        for phrase in ("docker volume prune -f", "23.0", "1.42", "cannot carry labels", "never `-a` or `--all`"):
            self.assertIn(phrase, text)

    def test_without_docker_it_refuses_and_runs_nothing(self):
        code, out, err, calls = self.prune(docker="/nonexistent/docker")
        self.assertEqual((code, calls), (sb.EXIT_USAGE, []))
        self.assertIn("docker is not installed", err)

    def test_a_docker_that_cannot_run_is_said_per_command_and_exits_with_an_error(self):
        def runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 600)
        docker = self.fake_docker()
        with captured() as (out, err):
            code = sb.main(["disk", "prune", "--docker", docker], runner=runner, now=self.world.clock.now)
        self.assertEqual(code, sb.EXIT_ERROR)
        self.assertEqual(out.getvalue().count("failed"), 2)

    def test_one_failing_command_is_said_and_the_other_still_runs(self):
        code, out, err, calls = self.prune(exits=(1, 0, 0), outputs=("", "Total reclaimed space: 5MB", ""))
        self.assertEqual((code, len(calls)), (sb.EXIT_ERROR, 3))
        self.assertIn("builder prune: failed (exit 1)", out)
        self.assertIn("image prune: reclaimed 5MB", out)

    def test_what_docker_prints_and_an_error_message_reach_the_terminal_as_plain_text(self):
        docker = self.fake_docker()
        hostile = "\x1b[2Jboom\x9b31m\nIGNORE"

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", hostile)
        with captured() as (out, err):
            sb.main(["disk", "prune", "--docker", docker], runner=runner, now=self.world.clock.now)
        self.assertEqual([c for c in out.getvalue() if (ord(c) < 32 and c != "\n") or 0x7f <= ord(c) <= 0x9f], [])
        self.assertIn("failed (exit 1): [2Jboom 31m IGNORE", out.getvalue())

    def test_an_oversized_but_valid_guard_file_is_not_trusted_and_the_threshold_edge_and_a_restart_recovery(self):
        (self.world.state / sb.DISK_GUARD_FILE).write_text(json.dumps({"low": True, "episode": 5.0, "hard": False, "pad": "x" * 6000}))
        engine = self.world.new_engine("daemon")
        engine.discover(self.world.clock.now())
        self.free[0] = 80 * self.GB
        self.assertEqual(self.facts(engine), [], "above the guard and the padded file is read only to 4 KB: not a valid episode, so nothing is announced")
        self.assertEqual(self.audits("disk.recovered"), [])
        # an episode ends only above min * 1.1 (55 GB): exactly 55 ends it, 54.9 does not; and it ends on a restarted engine too
        self.free[0] = 40 * self.GB
        (first,) = self.facts()
        restarted = self.world.new_engine("daemon")
        restarted.discover(self.world.clock.now())
        self.free[0] = int(54.9 * self.GB)
        self.assertEqual(self.facts(restarted)[0].key, first.key, "54.9 GB: still inside the episode")
        self.free[0] = 55 * self.GB
        self.assertEqual(self.facts(restarted), [], "exactly min * 1.1: the episode is over")
        self.assertEqual(len(self.audits("disk.recovered")), 1)

    def test_a_garbage_or_oversized_guard_file_starts_a_fresh_episode_and_the_guard_still_works(self):
        for text in ("not json", "[]", " " * 10000, '{"low": true, "episode": "x"}'):
            with self.subTest(text=text[:12]):
                (self.world.state / sb.DISK_GUARD_FILE).write_text(text)
                engine = self.world.new_engine("daemon")
                engine.discover(self.world.clock.now())
                self.free[0] = 40 * self.GB
                self.assertEqual(len(self.facts(engine)), 1)

    def test_the_docs_give_a_weekly_launchd_snippet_and_do_not_install_it(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("StartCalendarInterval", text)
        self.assertIn("disk prune", text)
        self.assertIn("not installed", text)


class TestV2Light(Base):
    """v2_light (the owner's hotfix switch): every optional read of session history is OFF; a send is still delivered, once; the daemon
    still finds its agents. A 20-agent idle fleet costs a handful of calls a minute."""

    AGENTS = tuple("agent%d" % n for n in range(20))
    HISTORY = ("session.message.list", "session.inbox.list")

    def world(self, agents=None, **config):
        config.setdefault("v2_cache_seconds", 900)
        config.setdefault("v2_light", True)
        return World(self, agents=agents or self.AGENTS, config=config)

    @staticmethod
    def ops(world, since=0):
        return dict(collections.Counter(c[2] for c in world.v2.calls[since:] if len(c) > 2 and c[1] == "api"))

    def test_the_default_is_off_and_it_must_be_a_boolean(self):
        self.assertIs(sb.DEFAULTS["v2_light"], False)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "v2_light": "yes"})

    def test_an_idle_fleet_of_twenty_makes_at_most_ten_calls_a_minute_and_reads_no_history(self):
        world = self.world()
        for _ in range(2):   # the first passes find the agents
            world.engine.run_pass()
            world.tick(30)
        mark = len(world.v2.calls)
        for _ in range(20):   # ten minutes at a 30 s poll
            world.engine.run_pass()
            world.tick(30)
        found = self.ops(world, mark)
        print("v2_light idle: %d calls in 10 minutes: %s" % (sum(found.values()), found))
        self.assertLessEqual(sum(found.values()), 100, found)
        for op in self.HISTORY:
            self.assertEqual(self.ops(world).get(op, 0), 0, "%s is a history read: %s" % (op, self.ops(world)))

    def test_without_the_switch_the_same_fleet_reads_history(self):
        world = self.world(v2_light=False, v2_cache_seconds=0)
        for _ in range(4):
            world.engine.run_pass()
            world.tick(30)
        self.assertGreater(sum(self.ops(world).get(op, 0) for op in self.HISTORY), 20, self.ops(world))

    def test_a_send_is_delivered_once_and_a_restart_does_not_deliver_it_again(self):
        world = self.world(agents=("platform", "coder"))
        world.fact("platform", "lab.fact:1", "First")
        world.tick(90)
        report = world.engine.run_pass()
        self.assertEqual([s["keys"] for s in report["sent"]], [["lab.fact:1"]])
        self.assertEqual(self.ops(world).get("session.inbox.list", 0), 0, self.ops(world))
        self.assertLessEqual(self.ops(world).get("session.message.list", 0), 2, "the engagement gate's read before the write, nothing else")
        world.tick(5)
        self.assertEqual(world.engine.run_pass()["sent"], [], "delivered on the send's own success: not sent again")
        mode = (world.state / sb.SENT_KEYS_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        world.engine = world.new_engine()   # a restart
        world.tick(5)
        self.assertEqual(world.engine.run_pass()["sent"], [], "the ledger survives a restart")
        self.assertEqual(len(world.admissions("platform")), 1)

    def test_a_person_typing_still_holds_the_wake_back_as_a_note_with_the_switch_on(self):
        world = self.world(agents=("platform", "coder"))
        world.you("platform")
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        mark = len(world.v2.calls)
        world.engine.run_pass()
        (admitted,) = world.admissions("platform")
        self.assertEqual((admitted["resume"], admitted["delivery"]), (False, "steer"), "a note for an engaged person, not a wake")
        self.assertTrue(any(len(c) > 2 and c[2] == "session.message.list" for c in world.v2.calls[mark:]), "the read just before the write is made")
        mark = len(world.v2.calls)
        world.tick(5)
        world.engine.run_pass()
        self.assertEqual(self.ops(world, mark).get("session.message.list", 0), 0, "and only then: nothing pending, no read")

    def test_two_writers_do_not_lose_each_others_keys(self):
        world = self.world(agents=("platform",))
        first, second = sb.SentLedger(world.state), sb.SentLedger(world.state)
        first.confirm("ses_example", ["k1"], 1.0)
        second.confirm("ses_example", ["k2"], 2.0)   # the second never saw k1 in memory
        first.confirm("ses_example", ["k3"], 3.0)
        self.assertEqual(sorted(sb.SentLedger(world.state).keys["ses_example"]), ["k1", "k2", "k3"])
        self.assertEqual(sorted(second.known("ses_example", 0)), ["k1", "k2", "k3"], "a read sees what another process wrote")

    def test_a_crash_between_the_write_and_the_record_is_at_most_once_and_says_so(self):
        world = self.world(agents=("platform",))
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        self.addCleanup(os.environ.__setitem__, sb.FAULT_ENV, "")
        os.environ[sb.FAULT_ENV] = "crash-after-send"
        with self.assertRaises(sb.SimulatedCrash):
            world.engine.run_pass()
        os.environ[sb.FAULT_ENV] = ""
        ledger = sb.SentLedger(world.state)
        self.assertEqual(ledger.unknown_count(), 1)
        world.engine = world.new_engine()   # a restart
        world.tick(5)
        self.assertEqual(world.engine.run_pass()["sent"], [], "unknown fate counts as delivered: never blindly re-sent")
        self.assertEqual(len(world.admissions("platform")), 1)

    def test_the_ledger_is_bounded(self):
        world = self.world(agents=("platform",))
        ledger = world.engine.sent
        for n in range(sb.SENT_PER_SESSION + 50):
            ledger.reserve("ses_example", ["k%d" % n], float(n))
            ledger.confirm("ses_example", ["k%d" % n], float(n))
        self.assertEqual(len(ledger.keys["ses_example"]), sb.SENT_PER_SESSION)
        self.assertIn("k%d" % (sb.SENT_PER_SESSION + 49), ledger.keys["ses_example"])
        for n in range(sb.SENT_SESSIONS + 20):
            ledger.confirm("ses_%d" % n, ["k"], float(1000 + n))
        self.assertEqual(len(ledger.keys), sb.SENT_SESSIONS)

    def test_a_missing_ledger_is_empty_but_a_corrupt_one_stops_the_sends_and_is_not_overwritten(self):
        world = self.world(agents=("platform",))
        self.assertEqual(sb.SentLedger(world.state).keys, {})
        path = world.state / sb.SENT_KEYS_FILE
        path.write_text('{"sessions": {"ses_example": {"k": "not a time"}}}')
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).known("ses_example", 0)
        path.write_text("not json")
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).known("ses_example", 0)
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).reserve("ses_example", ["k"], 1.0)
        self.assertEqual(path.read_text(), "not json", "the evidence is kept")
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        report = world.engine.run_pass()
        self.assertEqual(report["sent"], [], "nothing is sent on a ledger that cannot be trusted")
        self.assertEqual(len(world.admissions("platform")), 0)

    def test_a_ledger_that_cannot_be_written_refuses_the_send(self):
        world = self.world(agents=("platform",))
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        (world.state / (sb.SENT_KEYS_FILE + ".lock")).mkdir()   # a directory where the lock file goes: it cannot be opened
        report = world.engine.run_pass()
        self.assertEqual(report["sent"], [])
        self.assertEqual(len(world.admissions("platform")), 0, "no ledger, no write")
        self.assertTrue(report["errors"])

    def refuse_with(self, world, error):
        def refuse(params, body):
            raise error
        real = world.v2.op_session_synthetic
        world.v2.op_session_synthetic = refuse
        return real

    def test_a_definite_refusal_is_released_and_goes_next_pass(self):
        world = self.world(agents=("platform",))
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        real = self.refuse_with(world, FakeHTTP(400, "BadRequest", "no"))
        self.assertEqual(world.engine.run_pass()["sent"], [])
        self.assertEqual(sb.SentLedger(world.state).unknown_count(), 0, "released: not left unknown")
        self.assertEqual(sorted(sb.SentLedger(world.state).known(world.sessions["platform"], 0)), [])
        world.v2.op_session_synthetic = real
        world.tick(5)
        self.assertEqual([s["keys"] for s in world.engine.run_pass()["sent"]], [["lab.fact:1"]])

    def test_a_key_another_sender_holds_is_not_sent_again(self):
        world = self.world(agents=("platform",))
        sid = world.sessions["platform"]
        self.assertTrue(sb.SentLedger(world.state).reserve(sid, ["lab.fact:1"], 1.0))
        self.assertFalse(sb.SentLedger(world.state).reserve(sid, ["lab.fact:1"], 2.0), "the second sender is refused")
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        self.assertEqual(world.engine.run_pass()["sent"], [])
        self.assertEqual(len(world.admissions("platform")), 0)

    def test_a_ripe_delivery_reads_the_prompt_once_and_a_waiting_or_held_plan_reads_nothing(self):
        world = self.world(agents=("platform",))
        world.fact("platform", "lab.fact:1")
        world.tick(5)   # inside the batch window: a plan that waits
        mark = len(world.v2.calls)
        report = world.engine.run_pass()
        self.assertEqual(report["waiting"], ["platform"])
        self.assertEqual(self.ops(world, mark).get("session.message.list", 0), 0, "waiting: no read")
        world.tick(90)
        mark = len(world.v2.calls)
        world.engine.run_pass()
        reads = self.ops(world, mark).get("session.message.list", 0)
        self.assertTrue(1 <= reads <= 2, "ripe: the read that decides note or wake, and the one before the write (%d)" % reads)

    def test_a_stop_a_summary_and_a_lapsed_note_read_nothing_and_say_nothing(self):
        world = self.world(agents=("platform",))
        engine, sid = world.engine, world.sessions["platform"]
        world.v2._message(world.v2.sessions[sid], "assistant", "all done", None, world.clock.now())
        source = sb.WorkerFacts()
        source.bind(engine)
        observation = sb.Observation("platform", sid, world.panes["platform"], "idle", {"name": "platform", "reports_to": "boss"},
                                     idle_at=world.clock.now() - 10, outcome="ok")
        mark = list(world.v2.calls)
        self.assertEqual(source.last_reply(sid, 1), "", "no summary is read")
        self.assertIsNone(sb.triage_stop(engine, source, observation, "boss", 1, ""), "a stop is only its idle rider")
        self.assertEqual(engine.convert_lapsed_notes(observation, world.clock.now()), [])
        self.assertEqual(world.v2.calls, mark, "none of the three made a call")

    def test_an_oversized_or_unreadable_ledger_stops_the_sends_and_is_kept(self):
        world = self.world(agents=("platform",))
        path = world.state / sb.SENT_KEYS_FILE
        path.write_text(" " * (sb.SENT_FILE_MAX + 1))
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).known("ses_example", 0)
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).reserve("ses_example", ["k"], 1.0)
        self.assertEqual(len(path.read_text()), sb.SENT_FILE_MAX + 1, "kept as it was")
        path.unlink()
        path.mkdir()   # a directory where the file goes: not missing, not readable
        with self.assertRaises(sb.V2Error):
            sb.SentLedger(world.state).known("ses_example", 0)

    def test_a_call_that_never_ran_is_released_and_unknown_reservations_are_never_trimmed(self):
        for error in (sb.V2Error("unavailable", None, "cannot run"), sb.V2Error("refused", None, "no")):
            with self.subTest(reason=error.reason):
                world = self.world(agents=("platform",))
                world.fact("platform", "lab.fact:1")
                world.tick(90)
                self.refuse_with(world, error)
                world.engine.run_pass()
                self.assertEqual(sb.SentLedger(world.state).unknown_count(), 0, "released")
        ledger = sb.SentLedger(world.state)
        ledger.reserve("ses_example", ["old"], 1.0)
        for n in range(sb.SENT_PER_SESSION + 20):
            ledger.confirm("ses_example", ["k%d" % n], float(10 + n))
        self.assertIn("old", ledger.known("ses_example", 0), "an unknown-fate reservation outlives the trim")

    def test_a_timeout_or_unreadable_reply_keeps_the_reservation_and_is_not_sent_again(self):
        for error in (sb.V2Error("timeout", None, "no answer"), sb.V2Error("bad_json", None, "garbage"), sb.V2Error("http", 500, "boom"),
                      sb.V2Error("http", 504, "gateway"), sb.V2Error("http", 408, "slow")):
            with self.subTest(reason=error.reason, status=error.status):
                world = self.world(agents=("platform",))
                world.fact("platform", "lab.fact:1")
                world.tick(90)
                real = self.refuse_with(world, error)
                world.engine.run_pass()
                self.assertEqual(sb.SentLedger(world.state).unknown_count(), 1, "fate unknown: reserved")
                world.v2.op_session_synthetic = real
                world.tick(5)
                self.assertEqual(world.engine.run_pass()["sent"], [], "counts as delivered: at most once")
                self.assertEqual(len(world.admissions("platform")), 0)

    def test_a_409_is_delivered(self):
        world = self.world(agents=("platform",))
        world.fact("platform", "lab.fact:1")
        world.tick(90)
        self.refuse_with(world, FakeHTTP(409, "Conflict", "exists"))
        report = world.engine.run_pass()
        self.assertEqual([s["keys"] for s in report["sent"]], [["lab.fact:1"]])
        self.assertEqual(sb.SentLedger(world.state).unknown_count(), 0)
        self.assertEqual(sorted(sb.SentLedger(world.state).known(world.sessions["platform"], 0)), ["lab.fact:1"])

    def test_concurrent_writers_lose_nothing(self):
        world = self.world(agents=("platform",))
        barrier = threading.Barrier(4)

        def write(n):
            ledger = sb.SentLedger(world.state)
            barrier.wait()
            for i in range(15):
                ledger.confirm("ses_example", ["w%d-%d" % (n, i)], float(n * 100 + i))
        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(sb.SentLedger(world.state).keys["ses_example"]), 60)

    def test_status_says_what_is_off_and_how_many_sends_are_of_unknown_fate(self):
        world = self.world(agents=("platform",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "v2_light": True})
        sb.SentLedger(world.state).reserve("ses_example", ["k1"], 1.0)
        report = sb.collect_status(world.route, world.clock.now)
        self.assertIn("ON (features off:", report["v2_light"])
        self.assertIn("compactions", report["v2_light"])
        self.assertIn("1 send(s) of unknown fate", report["v2_light"])
        self.assertIn("v2_light", sb.format_status(report))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.assertNotIn("v2_light", sb.collect_status(world.route, world.clock.now))

    def test_status_on_a_corrupt_ledger_says_so_and_exits_zero(self):
        world = self.world(agents=("platform",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "v2_light": True})
        (world.state / sb.SENT_KEYS_FILE).write_text("not json")
        report = sb.collect_status(world.route, world.clock.now)
        self.assertIn("sent-keys ledger: UNREADABLE (sends stopped: fail closed)", report["v2_light"])
        self.assertIn("UNREADABLE", sb.format_status(report))
        with captured() as (out, err):
            code = sb.main(["status"], runner=world.route, now=world.clock.now)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("sent-keys ledger: UNREADABLE (sends stopped: fail closed)", out.getvalue())

    def test_each_optional_read_is_off_and_answers_what_it_did_not_see(self):
        world = self.world(agents=("platform",))
        engine, sid = world.engine, world.sessions["platform"]
        world.you("platform", "hello")
        mark = list(world.v2.calls)
        self.assertTrue(sb.brief_in_transcript(engine, sid), "no scan: taken to hold the brief, never sent twice")
        self.assertEqual(engine.reads.compaction_page(sid, 5, world.clock.now()), [])
        self.assertEqual(engine.reads.human_prompts_newest(sid), [])
        self.assertEqual(sb.inbox_human_prompts(engine, sid), [])
        self.assertEqual(sb.transcript_lines(engine, sid, 0), [])
        self.assertEqual(sb.synthetic_lines(engine, sid, 0, waiting=True), [])
        self.assertEqual(engine.reads.inbox(sid, world.clock.now()), [])
        self.assertEqual(world.v2.calls, mark, "none of them made a call")
        self.assertTrue(engine.reads.human_prompts_newest(sid, fresh=True), "the read before a write is still made")


class TestV2ReadCorrectness(Base):
    """The remembered reads never make the daemon act on a stale world: keys, prompts, compactions, the budget."""

    def world(self, **config):
        config.setdefault("v2_cache_seconds", 120)
        return World(self, agents=("coder", "platform"), config=config)

    def synthetic(self, world, sid, key, text="note"):
        world.v2._message(world.v2.sessions[sid], "synthetic", text, {"fleet": {"keys": [key], "from": "switchboard"}}, world.clock.now())

    @staticmethod
    def count(world, op, since=0):
        return len([c for c in world.v2.calls[since:] if len(c) > 2 and c[2] == op])

    def test_a_synthetic_message_that_lands_after_the_probe_is_found_and_the_pages_are_read_again(self):
        world = self.world()
        sid, engine = world.sessions["coder"], world.engine
        self.synthetic(world, sid, "k1")
        since = world.clock.now() - 100
        first, complete = engine.delivered_keys(sid, since)
        self.assertEqual((sorted(first), complete), (["k1"], True))
        mark = len(world.v2.calls)
        again, _ = engine.delivered_keys(sid, since)
        self.assertEqual(sorted(again), ["k1"])
        self.assertEqual(self.count(world, "session.message.list", mark), 1, "nothing changed: the probe alone")
        world.tick(3)
        self.synthetic(world, sid, "k2")
        mark = len(world.v2.calls)
        later, _ = engine.delivered_keys(sid, since)
        self.assertEqual(sorted(later), ["k1", "k2"], "the new key is found, not served from memory")
        self.assertGreater(self.count(world, "session.message.list", mark), 1, "the pages were read again")

    def test_an_earlier_since_than_the_remembered_one_reads_the_pages_again(self):
        world = self.world()
        sid, engine = world.sessions["coder"], world.engine
        self.synthetic(world, sid, "old")
        world.tick(500)
        self.synthetic(world, sid, "new")
        recent, _ = engine.delivered_keys(sid, world.clock.now() - 100)
        self.assertEqual(sorted(recent), ["new"])
        older, _ = engine.delivered_keys(sid, world.clock.now() - 1000)
        self.assertEqual(sorted(older), ["new", "old"], "a longer look-back is a new read, not the remembered short one")

    def test_a_person_s_prompt_behind_more_than_ten_fleet_messages_is_still_seen(self):
        world = self.world()
        sid = world.sessions["coder"]
        session = world.v2.sessions[sid]
        world.v2._message(session, "user", "please stop", None, world.clock.now())          # a person
        for n in range(15):
            world.tick(1)
            world.v2._message(session, "user", "task %d" % n, {"fleet": {"from": "fleet-task"}}, world.clock.now())
        prompts = world.engine.reads.human_prompts_newest(sid)
        self.assertEqual([m["text"] for m in prompts], ["please stop"])
        for n in range(60):
            world.tick(1)
            world.v2._message(session, "user", "more %d" % n, {"fleet": {"from": "fleet-task"}}, world.clock.now())
        self.assertEqual(world.engine.reads.human_prompts_newest(sid), [], "buried under more than the wide read covers: said in the docstring, and a write is re-checked")

    def test_compactions_are_remembered_for_the_memory_but_never_more_than_thirty_seconds_and_a_write_forgets_them(self):
        world = self.world(v2_cache_seconds=300)
        sid, engine = world.sessions["coder"], world.engine
        engine.reads.compaction_page(sid, 5, world.clock.now())
        mark = len(world.v2.calls)
        engine.reads.compaction_page(sid, 5, world.clock.now() + 29)
        self.assertEqual(self.count(world, "session.message.list", mark), 0)
        engine.reads.compaction_page(sid, 5, world.clock.now() + 31)
        self.assertEqual(self.count(world, "session.message.list", mark), 1, "at most 30 s, whatever v2_cache_seconds says")
        engine.reads.forget(sid)
        engine.reads.compaction_page(sid, 5, world.clock.now() + 32)
        self.assertEqual(self.count(world, "session.message.list", mark), 2, "a write to the session forgets it")

    def test_a_prompt_is_read_again_only_when_the_session_moved_and_forget_clears_it(self):
        world = self.world()
        sid, engine = world.sessions["coder"], world.engine
        info = world.v2.op_session_get({"sessionID": sid}, None)
        engine.reads.human_prompt(sid, info, world.clock.now())
        mark = len(world.v2.calls)
        engine.reads.human_prompt(sid, info, world.clock.now())
        self.assertEqual(self.count(world, "session.message.list", mark), 0, "time.updated did not move")
        engine.reads.forget(sid)
        engine.reads.human_prompt(sid, info, world.clock.now())
        self.assertEqual(self.count(world, "session.message.list", mark), 1, "forgotten: read again")

    def test_a_prompt_read_past_the_budget_returns_what_is_remembered(self):
        world = self.world(v2_reads_per_pass=0)
        sid, engine = world.sessions["coder"], world.engine
        info = world.v2.op_session_get({"sessionID": sid}, None)
        engine.reads.human_prompt(sid, info, world.clock.now())   # the first read is always made
        moved = dict(info, time=dict(info["time"], updated=info["time"]["updated"] + 5000))
        mark = len(world.v2.calls)
        engine.reads.begin_pass()
        engine.reads.human_prompt(sid, moved, world.clock.now())
        self.assertEqual(self.count(world, "session.message.list", mark), 0, "no budget: the remembered prompt stands")
        self.assertEqual(engine.reads.deferred, 1)

    def test_the_brief_a_launch_sends_forgets_what_was_remembered(self):
        world = self.world()
        sid = world.sessions["coder"]
        info = world.v2.op_session_get({"sessionID": sid}, None)
        world.engine.reads.inbox(sid, world.clock.now())
        world.engine.reads.human_prompt(sid, info, world.clock.now())
        self.assertTrue(sid in world.engine.reads.inboxes and sid in world.engine.reads.prompts)
        world.v2.prompt = lambda *args, **kwargs: None   # the fake takes no brief: what matters is what the daemon forgets afterwards
        sb.send_brief(world.engine, "coder", sid, "Do the thing")
        self.assertFalse(sid in world.engine.reads.inboxes or sid in world.engine.reads.prompts or sid in world.engine.reads.sessions)

    def test_the_delivered_keys_memory_drops_its_oldest_entry_not_all_of_them(self):
        world = self.world()
        engine = world.engine
        engine._delivered = {"ses_%d" % n: (None, 0, sb.KeyTimes()) for n in range(201)}
        sid = world.sessions["coder"]
        engine.delivered_keys(sid, world.clock.now() - 100)
        self.assertEqual(len(engine._delivered), 201, "one in, the oldest out")
        self.assertNotIn("ses_0", engine._delivered)
        self.assertIn("ses_200", engine._delivered)


class TestV2Http(StandInPids, V2TransportBase):
    def test_a_read_goes_over_http_with_the_operations_own_path_and_query_and_the_same_decoded_result(self):
        client = self.client()
        self.assertEqual(client.server_info(), {"version": "2.0.24"})
        page, cursor = client.message_page("ses_a", 5, "synthetic", None)
        self.assertEqual([m["id"] for m in page], ["msg_0", "msg_1"])
        self.assertEqual(cursor, {"next": None})
        self.assertEqual(client.message_list("ses_a", limit=900), [{"id": "msg_0", "pad": "x"}, {"id": "msg_1", "pad": "x"}])
        self.assertEqual(self.cli_calls, [], "no process was started")
        paths = [p for _, p, _ in self.service.requests]
        self.assertEqual(paths[0], "/api/info")
        self.assertEqual(paths[1], "/api/session/ses_a/message?limit=5&type=synthetic")
        self.assertEqual(paths[2], "/api/session/ses_a/message?limit=%d" % sb.V2_READ_LIMIT_MAX, "a page is never larger than the cap")
        self.assertEqual(client.calls_by_transport(), {"http": 3}, "one record: every call, with the way it travelled")
        self.assertEqual(client.calls_by_operation(), {"server.info": 1, "session.message.list": 2})

    def test_a_write_carries_its_json_body_and_a_session_id_is_quoted(self):
        client = self.client()
        client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_1", "text": "hi"})
        method, path, body = self.service.requests[-1]
        self.assertEqual((method, path, json.loads(body)), ("POST", "/api/session/ses_a/synthetic", {"id": "msg_1", "text": "hi"}))
        self.assertIsNone(sb.V2Client._target(sb.V2_HTTP_OPERATIONS["session.get"], {"sessionID": "a b/c", "extra": "x"}), "an unknown parameter goes to the CLI")
        self.assertEqual(sb.V2Client._target(sb.V2_HTTP_OPERATIONS["session.get"], {"sessionID": "a b/c"}), "/api/session/a%20b%2Fc")

    def test_reads_share_one_keep_alive_connection_and_a_write_uses_its_own(self):
        client = self.client()
        for _ in range(20):
            client.session_active()
        self.assertEqual((len(self.service.requests), self.service.connections), (20, 1))
        client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_1", "text": "hi"})
        self.assertEqual(self.service.connections, 2)

    def test_an_http_error_is_a_v2_error_with_the_status_and_the_body_like_the_cli(self):
        client = self.client()
        with self.assertRaises(sb.V2Error) as caught:
            client.session_get("ses_missing")
        self.assertEqual((caught.exception.reason, caught.exception.status), ("http", 404))
        self.assertIn("SessionNotFoundError", str(caught.exception))
        self.assertEqual(self.cli_calls, [], "a service answer is never retried through the CLI")

    def test_a_new_password_after_a_service_restart_is_read_again_once(self):
        client = self.client()
        client.session_active()
        self.service.up = False
        self.service.password = "pw-secret-2"
        self.service.up = True
        self.service.write_service_file(self.service_file)
        self.assertEqual(client.session_active(), {"ses_a": {"status": "idle"}}, "the restart is survived mid-run")
        self.assertEqual((self.cli_calls, client.calls_by_transport()), ([], {"http": 2}))

    def test_a_dead_keep_alive_connection_is_replaced_and_the_read_goes_on(self):
        client = self.client()
        client.session_active()
        self.service.up = False
        client.http._conn.sock = object()    # the connection looks open to the client
        self.service.up = True
        client.http._conn.broken = True
        self.assertEqual(client.session_active(), {"ses_a": {"status": "idle"}})
        self.assertEqual(self.cli_calls, [])

    def test_a_wrong_password_twice_falls_back_to_the_cli_and_never_loops(self):
        client = self.client()
        self.service.password = "changed-and-the-file-is-stale"
        self.assertEqual(client.session_active(), {"via": "cli"})
        self.assertEqual((len(self.cli_calls), client.calls_by_transport()), (1, {"cli-fallback": 1}), "a fallback counts once, as the CLI call that served it")

    def test_a_bad_service_file_uses_the_cli_and_the_service_is_not_contacted(self):
        cases = (("none", lambda: None), ("stale pid", lambda: self.service.write_service_file(self.service_file, pid=self.px[99999999])),
                 ("remote", lambda: self.service.write_service_file(self.service_file, url="http://203.0.113.9:80")),
                 ("a name, not a literal address", lambda: self.service.write_service_file(self.service_file, url="http://localhost:49374")),
                 ("world readable", lambda: self.service.write_service_file(self.service_file, mode=0o644)),
                 ("garbage", lambda: self.write_private("{not json")),
                 ("huge", lambda: self.write_private(" " * 10000)),
                 ("huge but valid JSON up front", lambda: self.write_private(json.dumps({"url": "http://127.0.0.1:49374", "password": self.service.password}) + " " * 10000)))
        for name, writer in cases:
            with self.subTest(name=name):
                self.service_file.exists() and self.service_file.unlink()
                writer()
                self.cli_calls.clear()
                before = len(self.service.requests)
                client = sb.V2Client("/fake/bin/opencode", {}, self.runner, 5, http=self.route(alive=lambda pid: pid != self.px[99999999]))
                self.assertEqual(client.session_active(), {"via": "cli"})
                self.assertEqual(len(self.service.requests), before, "the service was not contacted")
        for good in ("http://127.0.0.1:49374", "http://[::1]:49374"):
            with self.subTest(good=good):
                self.service.write_service_file(self.service_file, url=good)
                self.assertEqual(self.client().session_active(), {"ses_a": {"status": "idle"}})

    def test_a_connection_refused_falls_back_to_the_cli_which_may_start_the_service(self):
        client = self.client()
        self.service.up = False
        self.assertEqual(client.session_active(), {"via": "cli"})
        self.assertEqual(client.calls_by_transport(), {"cli-fallback": 1})

    def test_a_slow_service_is_a_timeout_error_and_is_not_asked_again_through_the_cli(self):
        client = self.client(timeout=5)
        self.service.delay = 6
        with self.assertRaises(sb.V2Error) as caught:
            client.session_active()
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertEqual((self.cli_calls, client.calls_by_transport()), ([], {"http": 1}), "it reached the service: counted, and not repeated")

    def test_a_reply_that_trickles_in_is_cut_at_the_total_deadline_not_per_socket_operation(self):
        now = [100.0]

        class Trickle:
            def __init__(trickle):
                trickle.reads = 0

            def read(trickle, amount=None):
                trickle.reads += 1
                now[0] += 2.0   # one byte, 2 s later: every socket operation is within its own timeout
                return b"x"
        trickle = Trickle()
        with mock.patch.object(sb.time, "monotonic", lambda: now[0]):
            with self.assertRaises(socket.timeout):
                sb.V2Http._read_body(trickle, types.SimpleNamespace(sock=None), now[0] + 5.0)
        self.assertEqual(trickle.reads, 3, "stopped at the total deadline (5 s), not after the whole reply")
        sock = types.SimpleNamespace(timeouts=[], settimeout=lambda seconds: sock.timeouts.append(round(seconds, 1)))
        conn = types.SimpleNamespace(sock=sock)
        with mock.patch.object(sb.time, "monotonic", lambda: 100.0):
            sb.V2Http._budget(conn, 103.0)
            with self.assertRaises(socket.timeout):
                sb.V2Http._budget(conn, 100.0)
        self.assertEqual(sock.timeouts, [3.0], "the socket gets what is left of the total time: connect, send, headers and body share it")
        done = type("Done", (), {"read": lambda self, amount=None, _b=[b"abc", b""]: _b.pop(0)})()
        self.assertEqual(sb.V2Http._read_body(done, types.SimpleNamespace(sock=None), time.monotonic() + 5), b"abc")

    def test_a_symlinked_service_file_and_an_open_directory_are_refused_and_the_service_not_contacted(self):
        real = self.home / "opencode" / "real.json"
        self.service_file.rename(real)
        self.service_file.symlink_to(real)
        before = len(self.service.requests)
        self.assertEqual(self.client().session_active(), {"via": "cli"}, "a symlink is refused, not followed")
        self.service_file.unlink()
        real.rename(self.service_file)
        os.chmod(str(self.service_file.parent), 0o777)
        self.assertEqual(self.client().session_active(), {"via": "cli"}, "a directory others can write to cannot be trusted")
        os.chmod(str(self.service_file.parent), 0o755)
        self.assertEqual(len(self.service.requests), before)
        self.assertEqual(self.client().session_active(), {"ses_a": {"status": "idle"}}, "and the same file in a closed directory is accepted")

    def test_a_reply_over_the_cap_is_refused_not_buffered(self):
        client = self.client()
        self.service.big = 5000
        with mock.patch.object(sb, "V2_HTTP_MAX_BODY", 1000):
            with self.assertRaises(sb.V2Error) as caught:
                client.message_list("ses_a", limit=5)
        self.assertEqual(caught.exception.reason, "too_large")

    def test_a_write_that_may_have_been_received_is_never_replayed_or_sent_again_through_the_cli(self):
        client = self.client()
        self.service.drop = True
        with self.assertRaises(sb.V2Error) as caught:
            client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_9", "text": "once"})
        self.assertEqual(caught.exception.reason, "unavailable")
        self.assertEqual((len(self.service.requests), self.cli_calls), (1, []), "received once, not repeated, not sent through the CLI")

    def test_a_write_the_service_refused_twice_for_its_password_goes_through_the_cli_once_because_it_was_never_received(self):
        client = self.client()
        self.service.password = "rotated-and-the-file-is-stale"
        result = client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_9", "text": "x"})
        self.assertEqual(result, {"via": "cli"})
        self.assertEqual((len(self.cli_calls), self.service.requests), (1, []), "the service never processed it, so the CLI (which authenticates itself) is safe")
        self.assertEqual(client.calls_by_transport(), {"cli-fallback": 1})

    def test_a_probe_that_raises_something_other_than_a_v2_error_does_not_leave_optional_reads_waiting_for_ever(self):
        now = [0.0]
        client = sb.V2Client("/fake/bin/opencode", {}, self.runner, 5, http=self.route(), monotonic=lambda: now[0])
        for _ in range(sb.V2_TIMEOUT_STREAK):
            client.breaker.record(20, True)
        now[0] += sb.V2_BACKOFF_BASE + 1
        with mock.patch.object(client, "_transport", side_effect=KeyError("a bug")):
            with self.assertRaises(KeyError):
                client.session_active()   # it was the probe
        self.assertEqual(client.session_active(), {"ses_a": {"status": "idle"}}, "the next optional read is the next probe, not a skipped read")

    def test_a_refused_password_on_a_write_is_retried_once_because_the_service_never_saw_it(self):
        client = self.client()
        client.session_active()
        self.service.password = "rotated"
        self.service.write_service_file(self.service_file)
        client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_9", "text": "x"})
        self.assertEqual([m for m, _, _ in self.service.requests].count("POST"), 1)
        self.assertEqual(self.cli_calls, [])

    def test_the_password_is_in_no_error_no_status_file_and_no_audit_event(self):
        world = World(self, agents=("platform",))
        engine = world.new_engine()
        engine.v2 = client = self.client()
        engine.v2.on_event = lambda name, detail: engine._note(name, "v2", detail)
        client.state_dir = world.state
        self.service.drop = True
        with self.assertRaises(sb.V2Error) as caught:
            client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_9", "text": "x"})
        self.service.drop = False
        for _ in range(sb.V2_TIMEOUT_STREAK):
            self.service.delay = 99
            with self.assertRaises(sb.V2Error):
                client.session_active()
        client._write_link(True)
        everything = str(caught.exception) + (world.state / sb.V2_LINK_STATUS_FILE).read_text() + (world.state / "audit.jsonl").read_text()
        self.assertIn("v2.backoff", everything, "the audit has the episode")
        self.assertNotIn(self.service.password, everything)
        self.assertNotIn("Basic ", everything)

    def test_under_the_test_suite_a_real_run_is_refused_before_the_http_route_or_the_cli_is_reached(self):
        client = sb.V2Client("/fake/bin/opencode", {}, subprocess.run, 5, http=self.route())
        with self.assertRaises(sb.V2Error) as caught:
            client.session_active()
        self.assertEqual(caught.exception.reason, "refused")
        self.assertEqual((self.service.requests, client.calls_by_transport()), ([], {}), "nothing was sent and nothing was counted")

    def test_the_config_key_forces_the_cli_route_and_a_bad_value_is_refused(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FLEET_SWITCHBOARD_V2_CLI", None)
            self.assertIsNotNone(sb.V2Client(sys.executable, {}, subprocess.run, 5, transport="auto").http)
        self.assertIsNone(sb.V2Client(sys.executable, {}, subprocess.run, 5, transport="cli").http, "the kill switch: always `opencode api`")
        base = {"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}
        self.assertEqual(sb.validate_config(dict(base))["v2_transport"], "auto")
        self.assertEqual(sb.validate_config(dict(base, v2_transport="cli"))["v2_transport"], "cli")
        for bad in ("http", "", None, 1, True):
            with self.subTest(bad=bad), self.assertRaises(sb.ConfigError):
                sb.validate_config(dict(base, v2_transport=bad))

    def test_every_real_client_construction_passes_the_kill_switch(self):
        sources = [switchboard_source(), (REPO / "bin" / "judge-eval").read_text()]
        sites = [line for text in sources for line in text.splitlines() if re.search(r"\bV2Client\(", line) and "class V2Client" not in line]
        self.assertGreaterEqual(len(sites), 5)
        for line in sites:
            with self.subTest(line=line.strip()[:80]):
                self.assertIn('transport=config["v2_transport"]', line, "a construction that omits it would ignore the kill switch")

    def test_a_fake_runner_or_the_cli_switch_means_no_http_route_at_all(self):
        self.assertIsNone(sb.V2Client("/fake/bin/opencode", {}, self.runner, 5).http)
        with mock.patch.dict(os.environ, {"FLEET_SWITCHBOARD_V2_CLI": "1"}):
            self.assertIsNone(sb.V2Client(sys.executable, {}, subprocess.run, 5).http)
        self.assertIsNotNone(sb.V2Client(sys.executable, {}, subprocess.run, 5).http, "a real run with a real binary uses the route")


class TestV2ClientBackpressure(Base):
    """The client over a latency-injecting fake CLI: the breaker's effect on real calls, the audit once per episode, the status line."""

    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        self.calls = []
        self.delay = 0.1
        self.timeout = False

    def runner(self, argv, **kwargs):
        self.calls.append(argv[2])
        self.clock[0] += self.delay
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"data": []}), "")

    def client(self):
        return sb.V2Client("/fake/bin/opencode", {}, self.runner, 1, monotonic=lambda: self.clock[0])

    def test_after_timeouts_an_optional_read_fails_fast_without_a_process_and_a_write_still_goes(self):
        client = self.client()
        self.timeout = True
        for _ in range(sb.V2_TIMEOUT_STREAK):
            with self.assertRaises(sb.V2Error):
                client.session_active()
        self.timeout = False
        before = len(self.calls)
        with self.assertRaises(sb.V2Error) as caught:
            client.session_active()
        self.assertEqual(caught.exception.reason, "backoff")
        self.assertEqual(len(self.calls), before, "no process was started for a skipped read")
        client._call("session.synthetic", {"sessionID": "ses_a"}, {"id": "msg_1", "text": "wake"})
        client.session_get("ses_a")   # discovery and the re-check before a write: a delivery cannot go without it
        client.inbox_list("ses_a")
        client.message_page("ses_a", 10, "synthetic")
        self.assertEqual(len(self.calls), before + 4, "a write, the session read, the inbox and the delivery key read all went through")
        self.assertEqual(client.breaker.skipped, 1, "the one skipped read is counted once, in one place")
        with self.assertRaises(sb.V2Error):
            client.message_list("ses_a", limit=5)   # an ordinary transcript read is optional

    def test_it_resumes_by_itself_once_the_service_answers_quickly_again(self):
        client = self.client()
        self.timeout = True
        for _ in range(sb.V2_TIMEOUT_STREAK):
            with self.assertRaises(sb.V2Error):
                client.session_active()
        self.timeout = False
        self.clock[0] += sb.V2_BACKOFF_BASE + 1
        for _ in range(sb.V2_RECOVER_OK):
            client.session_active()
        self.assertEqual(client.breaker.state, "closed")
        client.session_get("ses_a")

    def test_the_engine_audits_one_v2_backoff_per_episode_and_the_daemons_link_status_says_so(self):
        world = World(self, agents=("platform",))
        engine = world.new_engine()
        client = engine.v2
        client.monotonic = lambda: self.clock[0]
        client.breaker.clock = client.monotonic
        client.state_dir = world.state
        world.v2.fail_with = None
        real = client.runner

        def slow(argv, **kwargs):
            self.clock[0] += 30
            return real(argv, **kwargs)
        client.runner = slow
        for _ in range(sb.V2_SLOW_MIN_SAMPLES + 4):
            try:
                client.session_active()
            except sb.V2Error:
                pass
        events = [e for e in world.audit() if e["event"] in ("v2.backoff", "v2.resume")]
        self.assertEqual([e["event"] for e in events], ["v2.backoff"], "one line for the episode, not one per skipped read")
        self.assertEqual(events[0]["detail"]["episode"], 1)
        line = sb.v2_link_status_line(world.state, time.time())
        self.assertIn("OPEN", line)
        self.assertIn("v2.backoff", line)
        self.assertEqual(stat.S_IMODE((world.state / sb.V2_LINK_STATUS_FILE).stat().st_mode), 0o600)

    def test_a_status_line_is_only_shown_for_a_written_file_and_reads_closed_when_healthy(self):
        self.assertIsNone(sb.v2_link_status_line(self.home, time.time()))
        client = self.client()
        client.state_dir = self.home
        client.session_active()
        line = sb.v2_link_status_line(self.home, time.time())
        self.assertIn("breaker CLOSED", line)
        self.assertIn("fell back to the CLI", line)


class TestSlice3Modules(Base):
    """#437 slice 3: the loaders come from one generator, three more modules declare their own key, and two modules added on two branches never touch a shared line."""

    OLD = [   # what validate_config said before the move (recorded from the previous program): the error text is the contract
        ("temp_janitor", {"days": 0}, "config: temp_janitor.days must be a whole number from 1 to 365, got 0"),
        ("temp_janitor", "x", "config: 'temp_janitor' must be an object with only enabled, days, keep, dir, scan_seconds, weekly_scan_seconds and daemon_apply, got 'x'"),
        ("chat_clear", {"live": "y"}, 'config: \'chat_clear\' must be {"enabled": true|false, "live": true|false}, got {\'live\': \'y\'}'),
        ("chat_clear", [1], 'config: \'chat_clear\' must be {"enabled": true|false, "live": true|false}, got [1]'),
        ("impact_floor", {"enabled": "y"}, "config: 'impact_floor.enabled' must be true or false"),
        ("impact_floor", "x", "config: 'impact_floor' must be an object"),
        ("impact_floor", {"zzz": 1}, "config: 'impact_floor' has key(s) this tool does not read: zzz"),
    ]
    GOOD = {"temp_janitor": [None, {"days": 3}], "chat_clear": [None, {"live": True}], "impact_floor": [None, {"enabled": True}]}

    def test_s1_every_loader_is_generated_and_returns_the_module_load_module_returns(self):
        tree = ast.parse(switchboard_source())
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertEqual(len(sb.HELPER_MODULES), 23)
        self.assertEqual(defined & set(sb.HELPER_MODULES), set(), "0 of the loaders has a def of its own")
        for function, module in sb.HELPER_MODULES.items():
            with self.subTest(function=function):
                loader = getattr(sb, function)
                self.assertIs(loader(), sb.load_module(module))
                self.assertIs(loader(), loader(), "the second call returns the identical object")
                self.assertEqual(loader.__name__, function)

    def test_s2_the_moved_keys_give_the_old_results_and_the_old_error_text(self):
        for key, values in self.GOOD.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    sb.validate_config({key: value})
        sb.validate_config({})
        for key, value, text in self.OLD:
            with self.subTest(key=key, value=value), self.assertRaises(sb.ConfigError) as caught:
                sb.validate_config({key: value})
            self.assertEqual(str(caught.exception), text)

    def test_s3_the_three_keys_are_declared_by_their_modules_and_in_no_shared_place(self):
        declared = sb.config_declarations()
        for key, module in (("temp_janitor", "disk_janitor"), ("chat_clear", "chat_clear"), ("impact_floor", "impact_floor")):
            with self.subTest(key=key):
                self.assertEqual(declared[key], module)
                self.assertNotIn(key, sb.TOP_LEVEL_EXTRA_KEYS)
                self.assertIn(key, sb.config_known_keys())
        source = inspect.getsource(sb.validate_config)
        for text in ('"temp_janitor"', '"chat_clear"', '"impact_floor"'):
            self.assertNotIn(text, source, "no inline block for it is left in the validation chain")

    def make_program(self, extra):
        """A copy of the real program and its modules in a scratch directory, with `extra` files added to its bin."""
        directory = Path(tempfile.mkdtemp(prefix="slice3-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for path in BIN.glob("*.py"):
            shutil.copy(str(path), str(directory / path.name))
        copy_entry(directory)
        for name, text in extra.items():
            (directory / name).write_text(text)
        return directory

    def load_copy(self, directory):
        loader = importlib.machinery.SourceFileLoader("sb_copy_%d" % id(directory), str(directory / "fleet-switchboard"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    def test_s4_a_fixture_module_with_a_key_needs_no_change_to_the_program(self):
        fixture = 'CONFIG_KEY = "slice3_key"\nCONFIG_DEFAULT = {"n": 2}\n\n\ndef validate(raw, source):\n    if not isinstance(raw, dict):\n        raise ValueError("%s: slice3 bad" % source)\n\n\ndef hello():\n    return "hi"\n'
        directory = self.make_program({"slice3_mod.py": fixture})
        before = (directory / "fleet-switchboard").read_bytes()
        copy = self.load_copy(directory)
        self.assertEqual(copy.load_module("slice3_mod").hello(), "hi", "found by name from its own file")
        self.assertEqual(copy.validate_config({})["slice3_key"], {"n": 2})
        with self.assertRaises(copy.ConfigError) as caught:
            copy.validate_config({"slice3_key": 5})
        self.assertEqual(str(caught.exception), "config: slice3 bad")
        self.assertEqual((directory / "fleet-switchboard").read_bytes(), before)

    def test_s5_two_branches_that_each_add_a_module_merge_without_a_conflict_and_both_keys_work_after_the_merge(self):
        directory = Path(tempfile.mkdtemp(prefix="slice3-merge-"))
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "bin").mkdir()
        for path in BIN.glob("*.py"):
            shutil.copy(str(path), str(directory / "bin" / path.name))
        copy_entry(directory / "bin")

        def git(*args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"] + list(args), cwd=str(directory),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

        self.assertEqual(git("init", "-q", "-b", "main").returncode, 0)
        git("add", "-A"), git("commit", "-q", "-m", "base")
        for branch, key in (("one", "fixture_one"), ("two", "fixture_two")):
            git("checkout", "-q", "-b", branch, "main")
            (directory / "bin" / ("%s.py" % key)).write_text('CONFIG_KEY = "%s"\nCONFIG_DEFAULT = {"n": 1}\n\n\ndef validate(raw, source):\n    if raw is not None and not isinstance(raw, dict):\n        raise ValueError("%s: bad")\n' % (key, key))
            git("add", "-A"), git("commit", "-q", "-m", branch)
            self.assertEqual(git("diff", "--name-only", "main", branch).stdout.split(), ["bin/%s.py" % key], "0 lines of the program changed")
        git("checkout", "-q", "one")
        merged = git("merge", "--no-edit", "two")
        self.assertEqual(merged.returncode, 0, merged.stdout)
        loader = importlib.machinery.SourceFileLoader("sb_merged_%d" % id(directory), str(directory / "bin" / "fleet-switchboard"))
        program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(program)
        seen = []
        program.warn_unknown_config = lambda message, section="", names=(), known=(): seen.append(list(names))
        config = program.validate_config({})
        self.assertEqual((config["fixture_one"], config["fixture_two"]), ({"n": 1}, {"n": 1}), "both modules are found and defaulted after the merge, with no line in the program")
        self.assertEqual(seen, [])
        with self.assertRaises(program.ConfigError):
            program.validate_config({"fixture_two": "bad"})
        # the test fails when the behaviour breaks: a program that kept a shared key table (no declaration scan) does not know the keys
        program.config_declarations = lambda: {}
        program.validate_config({"fixture_one": {"n": 2}, "fixture_two": {"n": 3}})
        self.assertEqual(seen, [["fixture_one", "fixture_two"]], "with the declarations removed the keys are unknown: this test would fail")

    def test_a_missing_module_file_refuses_the_configuration_for_each_of_the_three_moved_keys_with_the_floor_message(self):
        for key, module, good in (("temp_janitor", "disk_janitor", {"days": 3}), ("chat_clear", "chat_clear", {"live": True}), ("impact_floor", "impact_floor", {"enabled": True})):
            with self.subTest(key=key):
                directory = Path(tempfile.mkdtemp(prefix="slice3-missing-"))
                self.addCleanup(shutil.rmtree, directory, True)
                for path in BIN.glob("*.py"):
                    if path.stem != module:   # the module file is gone
                        shutil.copy(str(path), str(directory / path.name))
                with mock.patch.object(sb, "CONFIG_DECLARATIONS_DIR", str(directory)), self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config({key: good})
                self.assertEqual(str(caught.exception), "config key %r must be declared by %s.py, and it is not (the module is missing, its CONFIG_KEY line is missing or damaged, "
                                 "or it names another key): no configuration is loaded" % (key, module))

    def test_when_two_keys_are_wrong_the_declared_modules_win_in_module_name_order_and_the_earlier_checks_win_over_them(self):
        bad = {"temp_janitor": "x", "chat_clear": "x", "impact_floor": "x"}
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict(bad))
        self.assertIn("'chat_clear'", str(caught.exception), "chat_clear.py sorts before disk_janitor.py and impact_floor.py")
        del bad["chat_clear"]
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict(bad))
        self.assertIn("temp_janitor", str(caught.exception), "disk_janitor.py sorts before impact_floor.py")
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"chat_clear": "x", "cpu_guard": "x"})
        self.assertIn("cpu_guard", str(caught.exception), "the checks that run before the declared ones still win")
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"chat_clear": "x", "docker_reaper": "x"})
        self.assertIn("chat_clear", str(caught.exception), "a declared module's check wins over the ones that follow it")

    def test_s6_a_default_owned_key_and_a_key_with_its_validator_in_the_program_stay_where_they_were(self):
        self.assertTrue({"reds", "heavy", "cpu_guard"} <= set(sb.DEFAULTS))
        self.assertTrue({"sweep", "triage", "plan_review", "impact_hold"} <= sb.TOP_LEVEL_EXTRA_KEYS)
        for key, value in (("sweep", "x"), ("impact_hold", "x"), ("plan_review", "x")):
            with self.subTest(key=key), self.assertRaises(sb.ConfigError):
                sb.validate_config({key: value})

    def test_s7_a_module_name_that_is_a_path_or_missing_is_refused(self):
        for name in ("../fleet_health", "/etc/passwd", "no_such_module_xyz", "a/b", ""):
            with self.subTest(name=name), self.assertRaises((ValueError, ImportError, OSError)):
                sb.load_module(name)

    def test_u2_a_missing_or_broken_module_fails_with_an_error_and_is_not_kept(self):
        directory = self.make_program({"broken_mod.py": "def (:\n"})
        copy = self.load_copy(directory)
        with self.assertRaises(SyntaxError):
            copy.load_module("broken_mod")
        with self.assertRaises(SyntaxError):
            copy.load_module("broken_mod")   # a failed import is never kept: it fails again
        with self.assertRaises(FileNotFoundError):
            copy.load_module("absent_mod")
        self.assertNotIn("broken_mod", sys.modules)


class TestModuleLoader(Base):
    """Ask #437 slice 1: one generic loader behind the old `load_<module>()` names."""

    OLD = {
        "load_decision_core": "decision_core", "load_plan_check": "plan_check", "load_plan_ledger": "plan_ledger",
        "load_report_fold": "report_fold", "load_triage_plan_open": "triage_plan_open", "load_pr_events": "pr_events",
        "load_disk_janitor": "disk_janitor", "load_heavy_slots": "heavy_slots", "load_cpu_guard": "cpu_guard",
        "load_health_records": "health_records", "load_reds": "reds", "load_impact_floor": "impact_floor",
        "load_plan_review": "plan_review", "load_external_adapter": "external_adapter",
        "load_decision_sync": "decision_sync", "load_work_sync": "work_sync", "load_agent_calls": "agent_calls",
        "load_triage_digest": "triage_digest", "load_dead_hint": "dead_hint", "load_fleet_health": "fleet_health",
    }

    def _copy(self, extra=None):
        """A copy of the program in a temp bin directory, with the named modules beside it, loaded as a module."""
        tmp = Path(tempfile.mkdtemp(prefix="ldtest"))
        self.addCleanup(shutil.rmtree, tmp, True)
        held = dict(sys.modules)
        self.addCleanup(lambda: (sys.modules.clear(), sys.modules.update(held)))
        copy_entry(tmp)
        for module in BIN.glob("*.py"):
            shutil.copy(module, tmp / module.name)
        for name, text in (extra or {}).items():
            (tmp / (name + ".py")).write_text(text)
        return tmp, _load("ld_" + tmp.name, tmp / "fleet-switchboard")

    def test_s1_each_old_loader_loads_its_old_path_and_caches(self):
        self.assertEqual(len(self.OLD), 20)
        for fn, name in self.OLD.items():
            first = getattr(sb, fn)()
            self.assertEqual(os.path.realpath(first.__file__), os.path.realpath(str(BIN / (name + ".py"))), fn)
            self.assertIs(getattr(sb, fn)(), first, fn)

    def test_s2_a_missing_or_broken_module_fails_as_before(self):
        tmp, mod = self._copy({"broken_one": "def (:\n"})
        with self.assertRaises(FileNotFoundError):
            mod.load_module("absent_one")
        with self.assertRaises(SyntaxError):
            mod.load_module("broken_one")
        self.assertNotIn("broken_one", sys.modules)

    def test_s4_a_new_module_loads_with_no_edit_to_the_program(self):
        tmp, mod = self._copy({"fixture_mod": "VALUE = 7\n"})
        first = mod.load_module("fixture_mod")
        self.assertEqual(first.VALUE, 7)
        self.assertIs(mod.load_module("fixture_mod"), first)

    def test_s9_concurrent_first_loads_all_get_the_finished_module_and_a_failing_one_fails_for_every_thread(self):
        tmp, mod = self._copy({
            "slow_mod": "import time\ntime.sleep(0.3)\nREADY = True\n",
            "failing_mod": "import time\ntime.sleep(0.2)\nraise RuntimeError('boom')\n"})
        got, errors, barrier = [], [], threading.Barrier(8)

        def worker(name):
            barrier.wait()
            try:
                module = mod.load_module(name)
                got.append((module, getattr(module, "READY", False)))
            except RuntimeError as err:
                errors.append(str(err))
        threads = [threading.Thread(target=worker, args=("slow_mod",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(len(got), 8)
        self.assertEqual(len({id(module) for module, _ in got}), 1, "one object for every thread")
        self.assertTrue(all(ready for _, ready in got), "no thread saw a half-loaded module")
        barrier, errors = threading.Barrier(8), []
        threads = [threading.Thread(target=worker, args=("failing_mod",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, ["boom"] * 8, "the failing module raises for every thread")
        self.assertNotIn("failing_mod", sys.modules)

    def test_s9b_a_slow_import_never_delays_another_module_and_a_module_may_load_another(self):
        tmp, mod = self._copy({
            "slow_one": "import threading\nthreading._ld_started.set()\nthreading._ld_gate.wait(20)\nREADY = True\n",
            "quick_one": "VALUE = 1\n", "fresh_one": "VALUE = 2\n",
            "b_mod": "VALUE = 'b'\n",
            "a_mod": "import threading\nB = threading._ld_load('b_mod')\n"})
        threading._ld_started, threading._ld_gate, threading._ld_load = threading.Event(), threading.Event(), mod.load_module
        self.addCleanup(lambda: [delattr(threading, n) for n in ("_ld_started", "_ld_gate", "_ld_load") if hasattr(threading, n)])
        self.addCleanup(threading._ld_gate.set)
        quick = mod.load_module("quick_one")
        slow = threading.Thread(target=lambda: mod.load_module("slow_one"))
        slow.start()
        self.assertTrue(threading._ld_started.wait(10))
        self.assertIs(mod.load_module("quick_one"), quick, "an already loaded module needs no lock")
        self.assertEqual(mod.load_module("fresh_one").VALUE, 2, "a first load of another module does not wait for the slow import")
        self.assertTrue(slow.is_alive(), "both returned while the slow import was still running")
        threading._ld_gate.set()
        slow.join(20)
        self.assertEqual(mod.load_module("a_mod").B.VALUE, "b", "a module that loads another while it runs")

    def test_s8_a_hostile_name_is_refused(self):
        tmp, mod = self._copy()
        (tmp.parent / "outside_mod.py").write_text("VALUE = 1\n")
        self.addCleanup((tmp.parent / "outside_mod.py").unlink)
        for name in ("../outside_mod", os.path.join(os.sep, "etc", "hosts"), "a/b", "", None):
            with self.assertRaises(ValueError, msg=repr(name)):
                mod.load_module(name)
        with self.assertRaises(FileNotFoundError):
            mod.load_module("no_such_module")
        self.assertNotIn("outside_mod", sys.modules)


class TestTempJanitorResume(Base):
    """Ask #469: a scan that runs out of its time budget records the name it stopped at, and the next run continues there."""

    def setUp(self):
        super().setUp()
        self.jan = sb.load_disk_janitor()
        self.temp = self.home / "jtmp"
        self.temp.mkdir()
        self.state = self.home / "jstate"
        self.state.mkdir()
        (self.state / "state-marker.json").write_text("{}")
        self.now = time.time() + 10 * 86400
        self.t = [0.0]
        self.judged = []
        real = self.jan.judge

        def counting(path, *a, **k):
            self.judged.append(os.path.basename(path))
            row = real(path, *a, **k)
            self.t[0] += 1.0
            return row
        patcher = mock.patch.object(self.jan, "judge", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, name, days):
        path = self.temp / name
        path.write_text("x")
        stamp = self.now - days * 86400
        os.utime(str(path), (stamp, stamp))
        return path

    def twelve(self, old_from=None):
        for n in range(1, 13):
            self.entry("e%02d.txt" % n, 20 if old_from and n >= old_from else 1)

    def run_once(self, seconds=4.5, apply=True, **extra):
        self.t[0] = 0.0
        self.judged.clear()
        cfg = self.jan.settings(dict({"scan_seconds": seconds}, **extra))
        lines, removed, kept = self.jan.run(self.temp, self.state, self.now, cfg, apply, lambda *a: None, clock=lambda: self.t[0], busy=lambda path: None)
        return lines, removed, kept, list(self.judged)

    def cursor(self):
        return self.jan.read_cursor(self.state)

    def test_s1_every_entry_is_judged_exactly_once_over_three_runs(self):
        self.twelve()
        seen = []
        for _ in range(3):
            seen.extend(self.run_once()[3])
        self.assertEqual(sorted(seen), ["e%02d.txt" % n for n in range(1, 13)])
        self.assertEqual(len(seen), len(set(seen)), "none judged twice before the cycle completes")

    def test_s2_a_cursor_naming_a_deleted_entry_resumes_at_the_next_name(self):
        self.twelve()
        self.run_once()
        self.assertEqual(self.cursor(), "e05.txt")
        (self.temp / "e05.txt").unlink()
        self.assertEqual(self.run_once()[3][0], "e06.txt", "by name, not by position: nothing after the deleted entry is skipped")

    def test_s3_an_entry_that_was_not_reached_is_never_removed_and_is_not_judged(self):
        self.twelve(old_from=6)
        lines, removed, kept, judged = self.run_once()
        self.assertEqual(removed, 0 + sum(1 for n in judged if int(n[1:3]) >= 6), "only judged entries can be removed")
        for n in range(6, 13):
            name = "e%02d.txt" % n
            self.assertEqual((self.temp / name).exists(), name not in judged, name)
        self.assertEqual(len(judged), 5)
        self.assertIn("5 judged, 7 not judged", lines)
        self.assertFalse(any(line.startswith("kept e") and "time budget" in line for line in lines), "not judged is never reported as kept for a reason")

    def test_s4_each_report_says_judged_and_not_judged_and_the_last_says_the_cycle_is_complete(self):
        self.twelve()
        reports = [self.run_once()[0] for _ in range(3)]
        self.assertIn("5 judged, 7 not judged", reports[0])
        self.assertIn("5 judged, 2 not judged", reports[1])
        self.assertIn("2 judged, 0 not judged", reports[2])
        self.assertIn("cycle complete", reports[2])
        self.assertEqual(sum(line == "cycle complete" for report in reports for line in report), 1)
        self.assertIsNone(self.cursor(), "a finished cycle clears the cursor")

    def test_s5_a_missing_empty_or_corrupt_cursor_starts_at_the_first_name(self):
        self.twelve()
        path = self.state / self.jan.CURSOR_FILE
        for content in (None, "", "{garbage", '{"after": 5}', '{"after": ""}'):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content)
            self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt", repr(content))

    def test_s6_the_weekly_budget_is_longer_and_both_come_from_configuration(self):
        cfg = self.jan.settings({})
        self.assertGreater(cfg["weekly_scan_seconds"], cfg["scan_seconds"])
        self.jan.validate({"scan_seconds": 30, "weekly_scan_seconds": 900}, "config")
        for bad in ({"scan_seconds": 0}, {"weekly_scan_seconds": "long"}, {"scan_seconds": True}, {"weekly_scan_seconds": 10 ** 9}):
            with self.assertRaises(ValueError) as caught:
                self.jan.validate(bad, "config")
            self.assertIn(list(bad)[0], str(caught.exception))
        self.twelve()
        self.assertEqual(len(self.run_once(seconds=2.5, weekly_scan_seconds=8.5, apply=False)[3]), 3)
        self.t[0] = 0.0
        self.judged.clear()
        lines, removed, kept = self.jan.run(self.temp, self.state, self.now, self.jan.settings({"scan_seconds": 2.5, "weekly_scan_seconds": 8.5}), False,
                                            lambda *a: None, clock=lambda: self.t[0], busy=lambda path: None, weekly=True)
        self.assertEqual(len(self.judged), 9, "the weekly run uses the longer budget")

    def test_s7_a_scan_that_finishes_behaves_as_before_and_clears_the_cursor(self):
        self.twelve(old_from=11)
        (self.state / self.jan.CURSOR_FILE).write_text('{"after": "e10.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertEqual(sorted(set(judged)), ["e11.txt", "e12.txt"], "(each removal candidate is judged once more right before it is removed)")
        self.assertEqual(removed, 2)
        self.assertIn("cycle complete", lines)
        self.assertFalse((self.state / self.jan.CURSOR_FILE).exists())

    def test_s8_no_thread_and_no_process_is_added(self):
        self.twelve()
        before = threading.active_count()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("a process was started")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")):
            self.run_once()
            self.run_once()
        self.assertEqual(threading.active_count(), before)

    def test_a_cursor_at_the_end_wraps_a_run_with_no_progress_keeps_it_and_a_dry_run_leaves_an_existing_one(self):
        self.twelve()
        cursor = self.state / self.jan.CURSOR_FILE
        cursor.write_text('{"after": "e12.txt"}')
        self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt", "a cursor at the last name wraps to the first")
        cursor.write_text('{"after": "zzz-after-everything"}')
        self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt")
        self.assertEqual(json.loads(cursor.read_text()), {"after": "zzz-after-everything"}, "a dry run leaves an existing cursor")
        cursor.write_text('{"after": "e04.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=-1)
        self.assertEqual(judged, [], "the budget was spent before any entry")
        self.assertEqual(json.loads(cursor.read_text()), {"after": "e04.txt"}, "no progress: the cursor stays")

    def test_the_cursor_file_is_not_one_of_the_fleets_records(self):
        self.twelve()
        victim = self.entry("zz-victim-name.txt", 20)
        (self.state / self.jan.CURSOR_FILE).write_text('{"after": "zz-victim-name.txt"}')
        text, complete = self.jan.named_text(self.state)
        self.assertNotIn("zz-victim-name.txt", text)
        self.assertTrue(complete)

    def test_a_corrupt_cursor_a_symlink_or_directory_it_names_and_a_type_change_never_remove_what_was_not_judged(self):
        outside = self.home / "outside-dir"
        outside.mkdir(mode=0o700)
        os.chmod(str(outside), 0o700)   # a private target: only the symlink check can refuse it
        (outside / "precious.txt").write_text("keep me")
        self.twelve(old_from=6)
        cursor = self.state / self.jan.CURSOR_FILE
        cursor.write_text("\x00{not json")
        lines, removed, kept, judged = self.run_once(seconds=100)
        for n in range(1, 13):
            name = "e%02d.txt" % n
            self.assertEqual((self.temp / name).exists(), not (n >= 6 and name in judged), name)
        self.assertEqual(sorted(set(judged)), ["e%02d.txt" % n for n in range(1, 13)], "a corrupt cursor judged from the first name")
        for n in range(6, 13):
            self.entry("e%02d.txt" % n, 20)
        (self.temp / "e03.txt").unlink()
        (self.temp / "e03.txt").symlink_to(outside)   # the cursor will name an entry that is now a link to a directory
        cursor.write_text('{"after": "e03.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertNotIn("e03.txt", judged, "the run resumes AFTER the named entry")
        self.assertTrue((outside / "precious.txt").exists())
        self.assertTrue((self.temp / "e03.txt").is_symlink())
        # an entry that changes type between its judging and its removal is not removed
        self.entry("e20.txt", 20)
        real_scan = self.jan.scan

        def scan_then_swap(*a, **k):
            rows = real_scan(*a, **k)
            os.unlink(str(self.temp / "e20.txt"))
            (self.temp / "e20.txt").symlink_to(outside)
            return rows
        cursor.unlink(missing_ok=True)
        with mock.patch.object(self.jan, "scan", scan_then_swap):
            lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertTrue((outside / "precious.txt").exists(), "a link is never followed")
        self.assertTrue((self.temp / "e20.txt").is_symlink(), "it changed since the scan: kept")
        self.assertIn("kept e20.txt: it changed since the scan", lines)

    def test_the_weekly_flag_is_parsed_and_the_command_passes_it_to_the_run(self):
        parser = sb.build_parser()
        self.assertTrue(parser.parse_args(["disk", "janitor", "--apply", "--weekly"]).weekly)
        self.assertFalse(parser.parse_args(["disk", "janitor", "--apply"]).weekly)
        source = inspect.getsource(sb.cmd_disk_janitor)
        self.assertIn('weekly = bool(getattr(args, "weekly", False))', source)
        self.assertIn("weekly=weekly, outcome=preview", source)

    def test_a_dry_run_reads_the_cursor_and_never_moves_it_and_the_file_is_private(self):
        self.twelve()
        self.run_once(apply=False)
        self.assertIsNone(self.cursor(), "a preview does not change what the next real run covers")
        self.run_once()
        mode = os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        stamp = os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mtime_ns
        self.jan.write_cursor(self.state, self.cursor())
        self.assertEqual(os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mtime_ns, stamp, "written only when it changes")


class TestAgentRegistry(unittest.TestCase):
    """Slice 2 of #903: bin/registry.py by itself (the frozen interface of docs/agent-registry.md): atomic write, version rule, the fold of today's
    records, the registry's own check of a removal's evidence. HERDR_* and OPENCODE_* are unset for every test."""

    def setUp(self):
        for key in [k for k in os.environ if k.startswith(("HERDR_", "OPENCODE_"))]:
            self.addCleanup(os.environ.__setitem__, key, os.environ[key])
            del os.environ[key]
        self.state = Path(tempfile.mkdtemp(prefix="registry-test"))
        self.addCleanup(shutil.rmtree, str(self.state), True)
        self.reg = sb.load_module("registry")

    def old_records(self, **extra):
        row = {"session": "ses_a", "pane": "w1:p1", "role": "coder", "charter": 903, "repo": "acme-org/widget-service",
               "workspace": "w1", "cwd": "/home/user/work", "seen": 5}
        (self.state / "pane-health.json").write_text(json.dumps({"agents": {"alpha": dict(row, **extra)}, "states": {"alpha": {"state": "detached"}}, "missing": {}}))

    def test_s8_every_field_the_old_readers_use_is_in_the_registry_after_the_fold(self):
        self.old_records()
        row = self.reg.read(self.state)["agents"]["alpha"]
        for field in ("session", "pane", "role", "charter", "repo", "workspace", "cwd"):
            with self.subTest(field=field):
                self.assertEqual(row[field], {"session": "ses_a", "pane": "w1:p1", "role": "coder", "charter": 903, "repo": "acme-org/widget-service",
                                              "workspace": "w1", "cwd": "/home/user/work"}[field])
        self.assertEqual((row["state"], row["kind"], row["run_id"]), ("detached", "local", None))
        self.assertEqual(oct((self.state / "registry.json").stat().st_mode & 0o777), "0o600")

    def test_the_fold_keeps_a_retirement_with_its_record_headless_copy_and_a_cloud_worker_with_its_run_id(self):
        entry = {"at": int(time.time()), "by": "boss", "reason": "done", "session": "ses_b", "auto": False,
                 "record": {"pane": "w1:p2", "workspace": "w1", "cwd": "/home/user/b"}, "headless": {"session": "ses_b", "name": "beta"}}
        (self.state / "retired.json").write_text(json.dumps({"beta": entry}))
        (self.state / "cloud").mkdir()
        (self.state / "cloud" / "gamma.json").write_text(json.dumps({"worker": "gamma", "repo": "acme-org/widget-service", "runtime_id": "rt_1", "run_id": "run_example", "session": "ses_c"}))
        got = self.reg.read(self.state)
        self.assertEqual(got["retired"]["beta"], entry)
        self.assertEqual(self.reg.get_retired(self.state, "beta"), entry)
        self.assertEqual((got["agents"]["gamma"]["kind"], got["agents"]["gamma"]["run_id"], got["agents"]["gamma"]["session"]), ("cloud", "run_example", "ses_c"))

    def test_a_retired_record_with_no_session_covers_nothing_and_known_agent_finds_no_one(self):
        (self.state / "retired.json").write_text(json.dumps({"beta": {"at": 1, "by": "x", "reason": "r", "session": None, "record": {"pane": "p"}}}))
        self.assertEqual(sb.known_agent(self.state, "beta"), {})
        self.assertFalse(self.reg.covers(self.reg.get_retired(self.state, "beta"), None))
        self.assertTrue(self.reg.upsert(self.state, "beta", {"session": "ses_new", "pane": "p9"})["changed"])   # not refused as retired

    def test_known_agent_reads_a_retired_agents_record_from_the_registry_alone(self):
        self.assertTrue(self.reg.upsert(self.state, "alpha", {"session": "ses_a", "pane": "w1:p1", "workspace": "w1", "cwd": "/home/user/work", "role": "coder"})["changed"])
        self.assertTrue(self.reg.retire(self.state, "alpha", "boss", "done")["changed"])
        self.assertNotIn("alpha", self.reg.read(self.state)["agents"])
        kept = sb.known_agent(self.state, "alpha")
        self.assertEqual((kept["session"], kept["workspace"], kept["cwd"]), ("ses_a", "w1", "/home/user/work"))
        self.assertTrue(self.reg.unretire(self.state, "alpha")["changed"])
        self.assertEqual(self.reg.get(self.state, "alpha")["workspace"], "w1")
        self.assertEqual(sb.read_retired(self.state), {})

    def test_a_newer_version_reads_nothing_says_so_and_a_writer_writes_nothing(self):
        text = json.dumps({"version": 9, "agents": {"a": {"session": "ses_x"}}, "retired": {}})
        (self.state / "registry.json").write_text(text)
        got = self.reg.read(self.state)
        self.assertEqual((got["whole"], got["agents"], got["problem"]), (False, {}, "registry.json is version 9; this reader knows 1: nothing read"))
        self.assertEqual(self.reg.upsert(self.state, "b", {"session": "ses_y"}), {"changed": False, "problem": got["problem"]})
        self.assertEqual((self.state / "registry.json").read_text(), text)
        with self.assertRaises(sb.RetiredUnreadable):
            sb.registry_retired_strict(self.state)
        for bad in ("{", "[]", '{"version": "1"}', '{"version": 1, "agents": []}'):
            (self.state / "registry.json").write_text(bad)
            self.assertFalse(self.reg.read(self.state)["whole"], bad)

    def test_s6_two_writers_and_a_reader_never_see_a_torn_file_in_1000_rounds(self):
        self.reg.upsert(self.state, "seed", {"session": "ses_seed", "cwd": "x" * 5000})
        stop, torn = threading.Event(), []

        def write(tag):
            n = 0
            while not stop.is_set():
                n += 1
                self.reg.upsert(self.state, "w%s" % tag, {"session": "ses_%s_%d" % (tag, n), "cwd": "y" * 3000})

        threads = [threading.Thread(target=write, args=(t,)) for t in "ab"]
        for t in threads:
            t.start()
        try:
            for _ in range(1000):
                got = self.reg.read(self.state)
                if not got["whole"] or "seed" not in got["agents"]:
                    torn.append(got["problem"])
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(torn, [])
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.endswith(".tmp")], [])

    def test_upsert_with_nothing_changed_writes_and_audits_nothing(self):
        self.assertTrue(self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1"})["changed"])
        path = self.state / "registry.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns, (self.state / "registry-audit.jsonl").read_text())
        self.assertEqual(self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1"}), {"changed": False, "problem": None})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, (self.state / "registry-audit.jsonl").read_text()), before)

    def test_reconcile_remove_checks_the_evidence_itself_and_audits_a_removal(self):
        self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1", "state": "working"})
        alive = {"pane_listed": True, "process_found": True}
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", alive, True)["changed"], False)     # whole=True is not enough for a live agent
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", {"pane_listed": False, "process_found": False}, True)["changed"], False)   # recorded state is not dead
        self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1", "state": "dead"})
        gone = {"pane_listed": False, "process_found": False}
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", gone, False)["changed"], False)    # a partial read removes nothing
        self.assertIn("a", self.reg.read(self.state)["agents"])
        self.assertTrue(self.reg.reconcile_remove(self.state, "a", gone, True)["changed"])
        self.assertNotIn("a", self.reg.read(self.state)["agents"])
        lines = [json.loads(l) for l in (self.state / "registry-audit.jsonl").read_text().splitlines()]
        self.assertEqual([l["event"] for l in lines if l["event"] == "reconcile_remove"], ["reconcile_remove"])
        self.assertEqual([l for l in lines if l["event"] == "reconcile_remove"][0]["evidence"], gone)
        refused = [l for l in lines if l["event"] == "reconcile_refused"]
        self.assertEqual(len(refused), 3)                                   # live evidence, state not dead, partial read: each audited with evidence and reason
        self.assertEqual(refused[0]["evidence"], alive)
        self.assertTrue(all(l["why"] and "nothing removed" in l["why"] for l in refused))

    def test_a_dead_row_is_removed_only_when_each_evidence_key_shows_it_gone_and_each_refusal_is_audited(self):
        gone = {"pane_listed": False, "process_found": False}
        cases = {"pane_listed true": {"pane_listed": True, "process_found": False}, "process_found true": {"pane_listed": False, "process_found": True},
                 "pane_listed missing": {"process_found": False}, "process_found missing": {"pane_listed": False}, "evidence None": None,
                 "evidence a string": "gone", "evidence a list": [gone]}
        for label, evidence in cases.items():
            with self.subTest(label=label):
                self.reg.upsert(self.state, "d", {"session": "ses_d", "state": "dead"})
                before = len([l for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "reconcile_refused" in l])
                got = self.reg.reconcile_remove(self.state, "d", evidence, True)
                self.assertEqual(got["changed"], False)
                self.assertIn("d", self.reg.read(self.state)["agents"])
                after = [json.loads(l) for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "reconcile_refused" in l]
                self.assertEqual(len(after), before + 1)
                self.assertEqual(after[-1]["evidence"], evidence if isinstance(evidence, dict) else None)
        self.assertTrue(self.reg.reconcile_remove(self.state, "d", gone, True)["changed"])

    def test_retire_then_undo_keeps_a_local_a_cloud_and_a_headless_row_what_it_was(self):
        self.reg.upsert(self.state, "loc", {"session": "ses_l", "pane": "p1", "workspace": "w1"})
        self.reg.upsert(self.state, "cld", {"session": "ses_c", "repo": "acme-org/widget-service", "kind": "cloud", "run_id": "run_example"})
        self.reg.retire(self.state, "loc", "boss", "done")
        self.reg.retire(self.state, "cld", "cloud-worker", "done")
        self.reg.retire(self.state, "hdl", "boss", "done", session="ses_h", extra={"headless": {"session": "ses_h"}})
        for name in ("loc", "cld", "hdl"):
            self.assertTrue(self.reg.unretire(self.state, name)["changed"])
        agents = self.reg.read(self.state)["agents"]
        self.assertEqual((agents["loc"]["kind"], agents["loc"]["run_id"], agents["loc"]["workspace"]), ("local", None, "w1"))
        self.assertEqual((agents["cld"]["kind"], agents["cld"]["run_id"], agents["cld"]["session"]), ("cloud", "run_example", "ses_c"))
        self.assertNotIn("hdl", agents)                                       # a headless worker comes back from its own launch record, not as a pane row

    def test_the_fold_does_not_bring_back_a_retired_cloud_worker_but_a_relaunch_under_the_name_stays(self):
        (self.state / "cloud").mkdir()
        (self.state / "cloud" / "w1.json").write_text(json.dumps({"worker": "w1", "session": "ses_c", "run_id": "r1"}))
        (self.state / "cloud" / "w2.json").write_text(json.dumps({"worker": "w2", "session": "ses_new", "run_id": "r2"}))
        (self.state / "retired.json").write_text(json.dumps({"w1": {"at": int(time.time()), "by": "x", "reason": "r", "session": "ses_c"},
                                                            "w2": {"at": int(time.time()), "by": "x", "reason": "r", "session": "ses_old"}}))
        got = self.reg.read(self.state)
        self.assertEqual(sorted(got["agents"]), ["w2"])
        self.assertEqual(sorted(got["retired"]), ["w1", "w2"])

    def test_retire_and_close_leave_agents_keep_the_record_and_audit(self):
        for name in ("a", "b"):
            self.reg.upsert(self.state, name, {"session": "ses_" + name, "pane": "p" + name, "workspace": "w1"})
        self.reg.retire(self.state, "a", "boss", "done", auto=True)
        self.reg.close(self.state, "b", "boss", "pane closed")
        got = self.reg.read(self.state)
        self.assertEqual(got["agents"], {})
        self.assertEqual((got["retired"]["a"]["auto"], got["retired"]["b"]["reason"], got["retired"]["b"]["record"]["workspace"]), (True, "pane closed", "w1"))
        self.assertEqual(sorted(json.loads(l)["event"] for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "retire" in l or "close" in l), ["close", "retire"])
        self.assertEqual(json.loads((self.state / "retired.json").read_text()).keys(), {"a", "b"})   # derived output, the old format


class TestClosingKeywordWarning(unittest.TestCase):
    """#1197: `preflight-pr` warns, never blocks, when the body closes an ask. The REAL `cmd_preflight_pr` runs with a faked runner.
    CLOSED rule (guard): the warning path makes only reads: every `gh` call it builds is `gh pr view` or `gh api` (no write verb).
    Known limits: a work item with a live check and no parent is not an ask here; a plain work item under an ordinary parent counts as an ask; the set of reads is read from the syntax tree of the two helpers only."""
    URL = "https://github.com/o/r/pull/9"

    def run_body(self, body, asks=(), unreadable=False):
        def runner(argv, **kwargs):
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = "/work/repo\n"
            elif argv[:3] == ["gh", "pr", "view"]:
                if unreadable:
                    done.returncode = 1
                else:
                    done.stdout = json.dumps({"body": body, "url": self.URL})
            elif argv[:2] == ["gh", "api"]:
                number = int(argv[2].split("/")[4])
                if number in asks:
                    done.stdout = json.dumps({"number": 34, "body": "an ordinary parent"})
                elif number == 700:   # parent is a charter
                    done.stdout = json.dumps({"number": 612, "body": "```yaml\norchestrator: x\n```"})
                else:
                    done.returncode = 1
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        return code, [l for l in out.getvalue().splitlines() if l.startswith("warning:")]

    def test_closes_an_ask_is_warned_once_and_the_exit_is_unchanged(self):
        code, warned = self.run_body("Closes #5", asks=(5,))
        self.assertEqual(code, 0)
        self.assertEqual(len(warned), 1)
        self.assertIn("o/r#5", warned[0])
        self.assertIn("Refs #5", warned[0])

    def test_refs_an_ask_is_silent(self):
        self.assertEqual(self.run_body("Refs #5", asks=(5,)), (0, []))

    def test_a_closing_keyword_for_a_plain_work_item_or_under_a_charter_is_silent(self):
        self.assertEqual(self.run_body("Closes #6", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("Fixes #700", asks=(5,)), (0, []))

    def test_several_keywords_in_one_body(self):
        code, warned = self.run_body("Closes #5\nfixes #6\nResolves #7 and Closes #5\nResolves o/x#8", asks=(5, 7, 8))
        self.assertEqual(code, 0)
        self.assertEqual([("#5" in w, "#7" in w, "#8" in w) for w in warned], [(True, False, False), (False, True, False), (False, False, True)])

    def test_a_keyword_inside_code_is_silent(self):
        body = "```\nCloses #5\n```\nand `Fixes #5` here\n~~~sh\nResolves #5\n~~~"
        self.assertEqual(self.run_body(body, asks=(5,)), (0, []))

    def test_a_quoted_line_and_an_html_comment_are_silent_but_a_keyword_outside_both_warns(self):
        self.assertEqual(self.run_body("> Closes #5", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("  > quoted\n>Fixes #5", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!-- Closes #5 -->", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!--\nCloses #5\n-->", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!-- never closed\nCloses #5", asks=(5,)), (0, []))
        code, warned = self.run_body("> Closes #6\n<!-- Closes #7 -->\nCloses #5", asks=(5, 6, 7))
        self.assertEqual((code, len(warned), "#5" in warned[0]), (0, 1, True))

    def test_mutant_without_the_quote_and_comment_strip_fails_the_silent_cases(self):
        real = sb.closing_keyword_issues
        with mock.patch.object(sb, "closing_keyword_issues", lambda body: real(str(body).replace("<!--", "").replace("-->", "").replace(">", ""))):
            with self.assertRaises(AssertionError):
                self.test_a_quoted_line_and_an_html_comment_are_silent_but_a_keyword_outside_both_warns()

    def test_a_list_of_numbers_and_an_issue_url_warn_for_each(self):
        code, warned = self.run_body("Closes #5, #6 and #7", asks=(5, 6, 7))
        self.assertEqual([[n in w for n in ("#5", "#6", "#7")] for w in warned], [[True, False, False], [False, True, False], [False, False, True]])
        code, warned = self.run_body("Closes https://github.com/o/r/issues/5", asks=(5,))
        self.assertEqual((code, len(warned), "o/r#5" in warned[0]), (0, 1, True))
        code, warned = self.run_body("Fixes https://github.com/x/y/issues/5, o2/p#6", asks=(5, 6))
        self.assertEqual([("x/y#5" in warned[0]), ("o2/p#6" in warned[1])], [True, True])

    def test_a_hung_or_slow_gh_is_quiet_and_every_call_has_a_timeout(self):
        seen = []

        def runner(argv, **kwargs):
            seen.append((argv[:2], kwargs.get("timeout")))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        self.assertFalse(sb.issue_is_ask(runner, "o/r", 5))
        self.assertEqual(sb.preflight_body_warnings(runner), [])
        self.assertEqual(len(seen), 2)
        for argv, timeout in seen:
            self.assertIsInstance(timeout, (int, float), argv)
            self.assertGreater(timeout, 0)

    def test_an_unreadable_body_is_silent(self):
        self.assertEqual(self.run_body("Closes #5", asks=(5,), unreadable=True), (0, []))

    def test_mutant_that_never_warns_fails_the_warned_case(self):
        with mock.patch.object(sb, "closing_warnings", lambda body, repo, is_ask: []):
            with self.assertRaises(AssertionError):
                self.test_closes_an_ask_is_warned_once_and_the_exit_is_unchanged()

    def test_mutant_that_warns_on_refs_fails_the_silent_case(self):
        real = sb.closing_keyword_issues
        with mock.patch.object(sb, "closing_keyword_issues", lambda body: real(str(body).replace("Refs", "Closes"))):
            with self.assertRaises(AssertionError):
                self.test_refs_an_ask_is_silent()

    def test_the_warning_path_only_reads(self):
        tree = ast.parse(switchboard_source())
        seen = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("issue_is_ask", "preflight_body_warnings"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.List) and sub.elts and isinstance(sub.elts[0], ast.Constant) and sub.elts[0].value == "gh":
                        seen.append(tuple(e.value for e in sub.elts[1:3] if isinstance(e, ast.Constant)))
        self.assertEqual(sorted(seen), [("api",), ("pr", "view")])

    def test_the_rule_is_in_the_texts_and_the_docs(self):
        for rel in ("skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/copilot/coder.agent.md"):
            text = (REPO / rel).read_text()
            self.assertIn("Refs #N", text, rel)
            self.assertIn("never `Closes`", text, rel)
        docs = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("`Refs #N`, never `Closes`", docs)
        self.assertIn("The warning never blocks", docs)


class TestPreflightPr(unittest.TestCase):
    """`preflight-pr` (#716): one PASS or FAIL line per CI-critical class, run under `heavy`; the runner is faked, nothing real runs."""
    SENTENCE = "`fleet-switchboard preflight-pr` runs the CI-critical focused classes"

    def run_preflight(self, changed, failing=(), timing_out=()):
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = "/work/repo\n"
            elif argv[:2] == ["git", "merge-base"]:
                done.stdout = "abc123\n"
            elif argv[:2] == ["git", "diff"]:
                done.stdout = "\n".join(changed)
            elif "heavy" in argv:
                command = " ".join(argv[argv.index("--") + 1:])
                if any(word in command for word in timing_out):
                    raise subprocess.TimeoutExpired(argv, 1)
                done.returncode = 1 if any(word in command for word in failing) else 0
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        return code, out.getvalue().splitlines(), calls

    def test_s1_every_class_passing_prints_one_pass_line_each_and_exits_0(self):
        code, lines, calls = self.run_preflight(["bin/fleet-switchboard"])
        self.assertEqual(code, 0)
        self.assertEqual([l.split()[:2] for l in lines], [["PASS", "cli-surface"], ["PASS", "hygiene"], ["PASS", "public-names"]])
        self.assertEqual([c for c in calls if "heavy" in c[0] and "cloud" in " ".join(c[0])], [])

    def test_s2_a_failing_class_prints_fail_with_its_name_and_exits_1(self):
        code, lines, _ = self.run_preflight(["x"], failing=("check-public-names",))
        self.assertEqual(code, 1)
        self.assertEqual([l for l in lines if l.startswith("FAIL")].__len__(), 1)
        self.assertTrue([l for l in lines if l.startswith("FAIL")][0].startswith("FAIL public-names"))

    def test_a_slot_wait_is_never_a_timeout_and_the_ceiling_runs_inside_the_slot(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            done = mock.Mock(returncode=0, stdout="/work/repo\n" if argv[:2] == ["git", "rev-parse"] else "abc\n" if argv[:2] == ["git", "merge-base"] else "", stderr="")
            if "heavy" in argv:
                done.returncode = 75   # all slots held: heavy gave up waiting, and says so with the marker line that carries the caller's own nonce (an exit status alone is a result)
                done.stderr = "heavy: [noslot %s]\n" % kwargs["env"]["FLEET_HEAVY_NOSLOT_NONCE"]
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual(code, 1)
        self.assertNotIn("timed out", out.getvalue())
        self.assertIn("no heavy slot", out.getvalue())
        self.assertIn("waiting for a slot", err.getvalue())
        for argv, kwargs in calls:
            if "heavy" in argv:
                self.assertNotIn("timeout", kwargs)   # no outer clock around the queue
                self.assertIn("FLEET_HEAVY_MAX_SECONDS", kwargs["env"])

    def test_exit_124_from_the_heavy_watchdog_is_a_timeout_line_with_its_ceiling(self):
        code, lines, calls = self.run_preflight(["x"])
        self.assertEqual({c[1]["env"]["FLEET_HEAVY_MAX_SECONDS"] for c in calls if "heavy" in c[0]}, {"240", "60"})
        calls_124 = []

        def runner(argv, **kwargs):
            done = mock.Mock(returncode=124 if "heavy" in argv else 0, stdout="/r\n", stderr="")
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (4.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual(code, 1)
        self.assertIn("FAIL cli-surface", out.getvalue())
        self.assertIn("timed out after 480s running, its ceiling (load average 4.0 on 4 cores)", out.getvalue())   # S2: 240 * (1 + 4/4)

    def test_s1_the_ceiling_grows_with_the_load_and_never_passes_the_maximum(self):
        low, high, absurd = (sb.preflight_ceiling(600, load, 8) for load in (1, 60, 1000))
        self.assertGreater(high, low)
        self.assertLessEqual(max(low, high), sb.PREFLIGHT_CEILING_MAX)
        self.assertEqual(absurd, sb.PREFLIGHT_CEILING_MAX)   # S3
        self.assertEqual(sb.preflight_ceiling(600, 0, 8), 600)
        self.assertEqual(sb.preflight_ceiling(600, "x", 0), 600)

    def test_s2_a_class_over_its_ceiling_says_it_timed_out(self):
        code, lines, _ = self.run_preflight(["x"], timing_out=("TestHygiene",))
        self.assertEqual(code, 1)
        self.assertIn("timed out after", [l for l in lines if l.startswith("FAIL hygiene")][0])

    def test_s3_a_cloud_change_runs_the_cloud_suites_with_a_49_character_tmpdir(self):
        code, lines, calls = self.run_preflight(["cloud/worker/guard.py"])
        self.assertEqual(code, 0)
        self.assertEqual(len([l for l in lines if l.startswith("PASS cloud")]), 1)
        envs = [c[1]["env"] for c in calls if "heavy" in c[0] and "cloud" in " ".join(c[0])]
        self.assertEqual(len(envs), 3)
        for env in envs:
            self.assertEqual(len(os.path.basename(env["TMPDIR"])), 49)
        self.assertEqual(len([c for c in calls if "heavy" in c[0]]), 6)

    def test_s4_it_only_runs_git_reads_and_heavy_tests_and_leaves_no_scratch(self):
        _, _, calls = self.run_preflight(["cloud/x"])
        for argv, kwargs in calls:
            self.assertTrue(argv[0] == "git" and argv[1] in ("rev-parse", "merge-base", "diff") or "heavy" in argv or argv[:3] == ["gh", "pr", "view"] or argv[:2] == ["gh", "api"], argv)   # #1197: the two gh READS of the body warning
        scratch = {os.path.dirname(c[1]["env"]["TMPDIR"]) for c in calls if "TMPDIR" in c[1].get("env", {}) and "cloud" in " ".join(c[0])}
        self.assertTrue(scratch and not any(os.path.exists(d) for d in scratch))

    @classmethod
    def sentence_count(cls, text):
        return " ".join(text.split()).count(cls.SENTENCE)

    def test_s5_the_coordination_skill_names_the_command_once_and_the_count_is_exact(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertEqual(self.sentence_count(text), 1)
        self.assertEqual(self.sentence_count(text.replace(self.SENTENCE, "")), 0)
        self.assertEqual(self.sentence_count(text + "\n" + self.SENTENCE), 2)

    def test_s6_the_class_list_is_one_table_with_each_class_once(self):
        source = switchboard_source()
        self.assertEqual(source.count("PREFLIGHT_CLASSES = ("), 1)
        table = source.split("PREFLIGHT_CLASSES = (", 1)[1].split("\n)\n", 1)[0]
        names = [row[0] for row in sb.PREFLIGHT_CLASSES]
        self.assertEqual(len(names), 4)
        for name in names:
            self.assertEqual(table.count('("%s",' % name), 1)

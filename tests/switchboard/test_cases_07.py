"""Test classes moved out of bin/test-switchboard (text unchanged): cases_07."""
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
from support.loader import captured, sb, switchboard_source
from support.orders_and_adapters import pr_view_json
from support.paths import REPO
from support.rigs import FakeGitHub, GithubRig, World, build_carry_repo, made_up_pid
from support.scenario_roles import GH_REPO
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE, herdr_typing_violations


class TestFacts(Base):
    def test_round_trip_and_defaults(self):
        made = sb.Fact.from_json({"key": "lab.fact:1", "recipient": "platform", "kind": "review",
                                  "summary": "s", "created": EPOCH})
        self.assertEqual(made.to_json(), {"key": "lab.fact:1", "recipient": "platform", "kind": "review",
                                          "summary": "s", "created": EPOCH, "issue": None, "batch": True,
                                          "from": None, "rider": False, "orders": False})

    def test_bad_facts_are_refused_with_the_field_named(self):
        good = {"key": "k", "recipient": "platform", "kind": "x", "summary": "s", "created": EPOCH}
        for change, needle in (({"key": ""}, "key"), ({"recipient": None}, "recipient"),
                               ({"created": "now"}, "created"),
                               ({"issue": 0}, "issue"), ({"batch": "yes"}, "batch"), ({"from": 3}, "from"),
                               ({"summary": None}, "summary"), ({"extra": 1}, "extra")):
            with self.subTest(change=change):
                with self.assertRaises(sb.FactError) as caught:
                    sb.Fact.from_json(dict(good, **change))
                self.assertIn(needle, str(caught.exception))
        with self.assertRaises(sb.FactError):
            sb.Fact.from_json({"key": "k"})

    def test_the_lab_source_reads_only_in_the_lab(self):
        world = World(self)
        world.fact("platform", "lab.fact:1")
        source = sb.lab_fact_source(world.state)
        self.assertEqual([f.key for f in source()], ["lab.fact:1"])
        os.environ[sb.LAB_ENV] = "0"
        self.assertEqual(source(), [])
        os.environ.pop(sb.LAB_ENV)
        self.assertEqual(source(), [])

    def test_a_bad_lab_line_fails_the_source_loudly(self):
        world = World(self)
        world.fact("platform", "lab.fact:1")
        with open(str(world.state / sb.LAB_FACTS), "a") as handle:
            handle.write('{"key": "lab.fact:2"}\n')
        with self.assertRaises(sb.FactError) as caught:
            sb.lab_fact_source(world.state)()
        self.assertIn(":2:", str(caught.exception))

    def test_the_registration_point_lists_the_lab_source(self):
        self.assertEqual([s.__name__ for s in sb.fact_sources(self.home)],
                         ["lab_facts", "worker_facts", "reminder_facts", "report_rider_facts", "foreground_facts",
                          "orders_facts", "sweep_facts", "triage_facts", "plan_wake_facts", "alert_wake_facts", "red_ci_facts", "job_facts", "pane_facts", "pane_exit_facts", "headless_restart_facts", "disk_facts", "cpu_facts", "spotlight_facts", "docker_reap_facts", "temp_janitor_facts", "heavy_orphan_facts"])

    def test_every_fact_source_class_in_the_module_is_registered(self):   # #1030: derived, so a new source cannot be left out
        registered = {type(s).__name__ for s in sb.fact_sources(self.home)}
        classes = {n for n, v in vars(sb).items() if isinstance(v, type) and n.endswith("Facts") and v.__module__ == sb.__name__}
        self.assertEqual(classes - registered, set(), "a *Facts class is not in fact_sources (list it, or rename it)")


class TestDiscovery(Base):
    def test_fleet_agents_are_herdr_agents_whose_session_names_them(self):
        world = World(self, agents=("platform",))
        stranger = world.v2.create_session({"other": True})
        world.herdr.add("w9:p1", stranger)
        world.herdr.panes["w9:p2"] = {"session": None, "status": "idle"}
        found = world.engine.discover(world.clock.now())
        self.assertEqual(sorted(found.agents), ["platform"])
        seen = found.agents["platform"]
        self.assertEqual((seen.session_id, seen.pane, seen.status), (world.sessions["platform"], "w1:p1", "idle"))
        self.assertEqual(seen.fleet, {"name": "platform", "role": "coder"})

    def test_inbox_items_carry_their_metadata_under_payload(self):
        # session.inbox.list in v2 2.0.22 (checked in the Lab): no top-level metadata.
        item = {"id": "msg_1", "type": "synthetic", "delivery": "queue",
                "payload": {"text": "t", "metadata": {"fleet": {"keys": ["k1"], "from": "switchboard"}}}}
        self.assertEqual(sb._fleet_keys(item), ["k1"])
        self.assertEqual(sb._fleet_meta(item)["from"], "switchboard")
        self.assertEqual(sb._fleet_keys({"metadata": {"fleet": {"keys": ["k2"]}}}), ["k2"])

    def test_a_wake_waiting_behind_a_busy_turn_is_not_sent_again(self):
        world = World(self, agents=("platform",))
        world.v2.start_turn(world.sessions["platform"], "fleet task", fleet=True, lasts=600, steps=1)
        world.fact("platform", "lab.fact:busy")
        world.tick(100)
        world.engine.run_pass()
        for _ in range(5):
            world.tick(5)
            world.engine.run_pass()
        sends = [argv for argv in world.v2.calls if argv[2:3] == ["session.synthetic"]]
        self.assertEqual(len(sends), 1, "the waiting item's keys are read from v2's inbox, so it is sent once")
        self.assertEqual(len(world.admissions("platform")), 1)

    def test_herdr_names_the_session_as_an_object_or_a_string(self):
        # herdr 0.9.3's `agent list` gives {"kind": "id", "value": "ses_..."} (S6).
        for entry, expected in (({"agent_session": {"kind": "id", "source": "herdr:opencode", "value": "ses_1"}}, "ses_1"),
                                ({"agent_session": {"value": "ses_2"}}, "ses_2"),
                                ({"agent_session": "ses_3"}, "ses_3"),
                                ({"agent_session": {"kind": "path", "value": "/tmp/x"}}, None),
                                ({"agent_session": {"kind": "id", "value": ""}}, None),
                                ({"agent_session": None}, None), ({}, None), ("ses_4", None)):
            with self.subTest(entry=entry):
                self.assertEqual(sb.agent_session(entry), expected)

    def test_status_comes_from_herdr(self):
        world = World(self, agents=())
        for herdr_status, expected in (("working", "working"), ("idle", "idle"), ("done", "idle"),
                                       ("blocked", "blocked"), ("unknown", "unknown"), ("weird", "unknown")):
            world.add_agent("a-" + herdr_status, status=herdr_status)
        found = world.engine.discover(world.clock.now()).agents
        self.assertEqual({name: o.status for name, o in found.items()},
                         {"a-working": "working", "a-idle": "idle", "a-done": "idle", "a-blocked": "blocked",
                          "a-unknown": "unknown", "a-weird": "unknown"})

    def test_idle_at_outcome_and_your_last_prompt(self):
        world = World(self, agents=("platform",))
        sid = world.sessions["platform"]
        world.you("platform", "hi")                       # yours, at 0
        world.tick(30)
        world.v2.start_turn(sid, "fleet task", fleet=True)  # the fleet's, at 30
        world.tick(10)
        seen = world.engine.discover(world.clock.now()).agents["platform"]
        self.assertEqual(seen.last_human_prompt, EPOCH)
        self.assertEqual((seen.idle_at, seen.outcome), (EPOCH + 30, "success"))
        self.assertTrue(seen.engaged(world.clock.now(), 600))

    def test_a_prompt_older_than_the_window_is_not_engagement(self):
        world = World(self, agents=("platform",))
        world.you("platform")
        world.tick(601)
        seen = world.engine.discover(world.clock.now()).agents["platform"]
        self.assertIsNone(seen.last_human_prompt)
        self.assertFalse(seen.engaged(world.clock.now(), 600))
        argv = [a for a in world.v2.calls if a[2] == "session.message.list"][-1]
        self.assertIn("limit=%d" % sb.V2Reads.PROMPT_READ, argv)   # a small read of the newest user messages, not a 200-message page
        self.assertIn("type=user", argv)

    def test_two_sessions_with_one_name_are_a_conflict(self):
        world = World(self, agents=("platform",))
        world.add_agent("platform")
        seen = world.engine.discover(world.clock.now()).agents["platform"]
        self.assertEqual(len(seen.conflict), 1)
        self.assertIn("more than one session", sb.hold_reason(seen))

    def test_herdr_or_v2_down_is_a_reason_not_a_crash(self):
        world = World(self, agents=("platform",))
        world.herdr.down = True
        found = world.engine.discover(world.clock.now())
        self.assertEqual(found.agents, {})
        self.assertIn("herdr unreachable", found.missing("platform"))
        world.herdr.down, world.v2.down = False, True
        self.assertIn("v2 unreachable", world.engine.discover(world.clock.now()).missing("platform"))

    def test_a_session_v2_no_longer_has_is_skipped(self):
        world = World(self, agents=("platform",))
        del world.v2.sessions[world.sessions["platform"]]
        found = world.engine.discover(world.clock.now())
        self.assertEqual((found.agents, found.problems), ({}, []))


class TestConversion(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform",))
        self.world.you("platform")
        self.world.fact("platform", "lab.fact:1")
        self.world.tick(90)
        self.world.engine.run_pass()
        (self.note,) = self.world.admissions("platform")
        self.assertFalse(self.note["resume"])

    def test_a_note_waits_while_you_are_engaged(self):
        self.world.tick(400)
        self.assertEqual(self.world.engine.run_pass()["converted"], [])
        self.assertIsNone(self.note["delivered_at"])

    def test_a_note_that_outlives_engagement_becomes_a_wake(self):
        self.world.tick(600 - 90)
        report = self.world.engine.run_pass()
        self.assertEqual(report["converted"], [self.note["id"]])
        self.assertIsNotNone(self.note["delivered_at"])
        updates = [w for w in self.world.v2.writes if w["op"] == "session.inbox.update"]
        self.assertEqual(len(updates), 2)
        audited = [(e["detail"]["delivery"], e["phase"]) for e in self.world.audit() if e["event"] == "v2.inbox.update"]
        self.assertEqual(audited, [("queue", "intent"), ("queue", "result"), ("steer", "intent"), ("steer", "result")])
        self.assertEqual(self.world.model_calls("platform", "turn_start")[-1]["inputs"][0]["keys"], ["lab.fact:1"])

    def test_a_blocked_agent_keeps_its_note(self):
        self.world.herdr.panes[self.world.panes["platform"]]["status"] = "blocked"
        self.world.tick(600)
        self.assertEqual(self.world.engine.run_pass()["converted"], [])


class TestDaemonWaits(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform",))

    def counted(self, on_pass=None):
        passes = []
        real = self.world.engine.run_pass

        def run_pass():
            passes.append(time.time())
            if on_pass:
                on_pass(len(passes))
            return real()

        self.world.engine.run_pass = run_pass
        return passes

    def test_a_poke_makes_a_pass_at_once_not_after_the_poll(self):
        passes = self.counted()
        thread = threading.Thread(target=sb.run_daemon, args=(self.world.engine, self.world.state, 30, None, 2))
        thread.start()
        deadline = time.time() + 5
        while not passes and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(passes)
        started = time.time()
        while not sb.send_poke(self.world.state) and time.time() < deadline:
            time.sleep(0.01)  # the socket is bound just after the lock is taken
        thread.join(8)
        self.assertFalse(thread.is_alive(), "30 s poll: the poke did not wake the daemon")
        self.assertEqual(len(passes), 2)
        self.assertLess(time.time() - started, 5)

    def test_every_poke_waiting_is_drained_before_the_next_pass(self):
        def burst(number):
            if number == 1:  # three events arrive during the first pass
                for _ in range(3):
                    sb.send_poke(self.world.state)

        passes = self.counted(burst)
        sb.run_daemon(self.world.engine, self.world.state, 0.6, None, passes=3)
        # pass 2 follows the burst at once; the burst made no pass 3 of its own,
        # so pass 3 waited out the poll.
        self.assertLess(passes[1] - passes[0], 0.4)
        self.assertGreaterEqual(passes[2] - passes[1], 0.5)

    def test_the_socket_is_closed_when_the_daemon_stops(self):
        sb.run_daemon(self.world.engine, self.world.state, 5, lambda sock, t: None, passes=1)
        self.assertFalse(sb.send_poke(self.world.state))

    def test_a_socket_that_cannot_be_bound_costs_only_the_wakeups(self):
        with mock.patch.object(sb, "open_poke_socket", side_effect=OSError(48, "Address already in use")):
            waits = []
            done = sb.run_daemon(self.world.engine, self.world.state, 5, lambda sock, t: waits.append(sock),
                                 passes=2)
        self.assertEqual((done, waits), (2, [None]))
        self.assertTrue(any(e["event"] == "poke.unavailable" for e in self.world.audit()))


class TestPaneBoundByProcess(Base):
    """The multiplexer's agent report names session B for a pane whose process runs session A: A keeps the pane."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("agent-a", "agent-b"))
        self.engine = self.world.engine
        self.engine.state_dir = self.world.state
        self.a, self.b = self.world.sessions["agent-a"], self.world.sessions["agent-b"]
        self.pane_a, self.pane_b = self.world.panes["agent-a"], self.world.panes["agent-b"]
        self.registry = sb.load_module("registry")

    def lines(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def lie(self):
        """The report for A's pane names B's session; A's process still runs A. B's own pane is gone."""
        self.world.herdr.panes[self.pane_a]["session"] = self.b
        del self.world.herdr.panes[self.pane_b]

    def run_pass(self):
        found = self.engine.discover(self.world.clock.now())
        sb.pane_health_pass(self.engine, self.world.clock.now())
        return found

    def test_S2_a_disagreeing_report_keeps_a_bound_and_alive_and_b_gets_no_pane(self):
        self.lie()
        found = self.run_pass()
        self.assertEqual(found.agents["agent-a"].pane, self.pane_a)
        self.assertNotIn("agent-b", found.agents)
        rows = self.registry.read(self.world.state)["agents"]
        self.assertEqual(rows["agent-a"]["pane"], self.pane_a)
        self.assertNotEqual(rows["agent-a"]["state"], "dead")
        self.assertFalse(rows.get("agent-b", {}).get("pane"))
        self.run_pass()
        lines = self.lines("fleetpane.report_disagrees")
        self.assertEqual(len(lines), 1)
        self.assertIn(self.a, json.dumps(lines[0]))
        self.assertIn(self.b, json.dumps(lines[0]))

    def test_S4_a_live_pane_is_not_rebound_and_an_unreadable_process_list_binds_nothing_new(self):
        self.run_pass()
        got = self.registry.upsert(self.world.state, "agent-b", {"session": self.b, "pane": self.pane_a, "role": "coder"})
        self.assertIn("agent-a", got["problem"])
        self.assertIn("agent-b", got["problem"])
        self.assertEqual(self.registry.read(self.world.state)["agents"]["agent-a"]["pane"], self.pane_a)
        sb.record_launch_charter(self.engine, "agent-b", self.b, {"role": "orchestrator", "charter": 5}, pane=self.pane_a)
        self.assertEqual(self.registry.read(self.world.state)["agents"]["agent-a"]["pane"], self.pane_a)
        self.assertNotEqual(self.registry.read(self.world.state)["agents"].get("agent-b", {}).get("pane"), self.pane_a)
        with mock.patch.object(sb.HerdrClient, "pane_process_info", side_effect=sb.HerdrError("down", "herdr is down")):
            found = self.engine.discover(self.world.clock.now())
        self.assertIn("cannot read", found.agents["agent-a"].problem)

    def test_S3_a_cloud_worker_is_never_recorded_with_a_pane(self):
        got = self.registry.upsert(self.world.state, "cloud-w", {"session": "ses_cloud", "kind": "cloud", "pane": self.pane_a})
        self.assertIn("no pane", got["problem"])
        self.assertNotIn("cloud-w", self.registry.read(self.world.state)["agents"])

    def test_S5_the_wrong_records_of_the_case_are_put_right_by_the_pass(self):
        self.run_pass()
        self.registry.upsert(self.world.state, "agent-a", {"session": self.a, "pane": self.pane_a, "state": "dead"})
        self.registry.upsert(self.world.state, "cloud-w", {"session": "ses_cloud", "pane": "w9:p9"})
        cloud = self.world.state / "cloud"
        cloud.mkdir(exist_ok=True)
        (cloud / "cloud-w.json").write_text(json.dumps({"worker": "cloud-w", "hard_deadline": "2999-01-01T00:00:00Z"}))
        self.world.clock.advance(sb.PANE_CHECK_SECONDS + 1) if hasattr(self.world.clock, "advance") else self.world.tick(sb.PANE_CHECK_SECONDS + 1)
        self.run_pass()
        rows = self.registry.read(self.world.state)["agents"]
        self.assertNotEqual(rows["agent-a"]["state"], "dead")
        self.assertEqual((rows["cloud-w"]["kind"], rows["cloud-w"]["pane"]), ("cloud", None))

    def test_S6_a_pane_whose_process_runs_the_session_is_never_dead_even_when_the_list_omits_it(self):
        self.run_pass()
        del self.world.herdr.panes[self.pane_b]
        self.world.herdr.panes[self.pane_a]["session"] = None
        self.world.herdr.panes[self.pane_a]["undetected"] = True
        for _ in range(sb.PANE_MISSING_PASSES + 2):
            self.run_pass()
        self.assertNotEqual(self.registry.read(self.world.state)["agents"]["agent-a"]["state"], "dead")


    # -- S11: a local agent that reuses the name of a FINISHED cloud worker keeps its pane

    def cloud_record(self, name, **extra):
        cloud = self.world.state / "cloud"
        cloud.mkdir(exist_ok=True)
        (cloud / (name + ".json")).write_text(json.dumps(dict({"worker": name, "hard_deadline": "2999-01-01T00:00:00Z"}, **extra)))

    def collide(self, how):
        if how == "ended":
            self.cloud_record("agent-a", retired=True)
        elif how == "deadline":
            self.cloud_record("agent-a", hard_deadline="2001-01-01T00:00:00Z")
        elif how == "reported":
            self.cloud_record("agent-a")
            (self.world.state / sb.LAST_REPORT_FILE).write_text(json.dumps({"agent-a": {"state": "done", "at": int(self.world.clock.now())}}))

    def test_S11_a_local_agent_reusing_a_finished_cloud_workers_name_keeps_its_pane_and_state(self):
        for how in ("ended", "deadline", "reported", "retired"):
            with self.subTest(how=how):
                self.run_pass()
                before = self.registry.read(self.world.state)["agents"]["agent-a"]["state"]
                if how == "retired":
                    self.cloud_record("agent-a")
                    self.registry.retire(self.world.state, "agent-a", "test", "finished", session="ses_old_cloud")
                else:
                    self.collide(how)
                self.run_pass()
                row = self.registry.read(self.world.state)["agents"]["agent-a"]
                self.assertEqual((row.get("kind") or "local", row["pane"]), ("local", self.pane_a))
                self.assertEqual(row["state"], before)
                self.assertEqual(self.lines("fleetpane.cloud_repaired"), [])

    def test_S11_a_mutant_that_repairs_on_file_existence_fails(self):
        self.run_pass()
        self.cloud_record("agent-a", hard_deadline="2001-01-01T00:00:00Z")
        with mock.patch.object(sb, "live_cloud_record", lambda state, name, retired, now: (Path(state) / "cloud" / (name + ".json")).is_file()):
            self.run_pass()
        self.assertTrue(self.lines("fleetpane.cloud_repaired"))   # the mutant repairs: the test above would catch it

    # -- S12: when the terminal report is trusted

    def test_S12_a_report_is_trusted_when_the_process_line_names_no_session(self):
        self.world.herdr.panes[self.pane_a]["program"] = self.world.herdr.panes[self.pane_a]["program"].split(" -s")[0]
        entry = {"pane_id": self.pane_a, "agent_session": self.a, "agent_status": "idle"}
        self.assertEqual(self.engine.pane_truth(entry), entry)

    def test_S12_a_cloud_session_in_the_report_is_never_bound(self):
        self.registry.upsert(self.world.state, "cloud-w", {"session": "ses_cloud", "kind": "cloud"})
        self.world.herdr.panes[self.pane_a]["program"] = self.world.herdr.panes[self.pane_a]["program"].split(" -s")[0]
        self.assertIsNone(self.engine.pane_truth({"pane_id": self.pane_a, "agent_session": "ses_cloud", "agent_status": "idle"}))

    def test_S12_a_disagreeing_report_binds_nothing_for_the_reported_agent(self):
        got = self.engine.pane_truth({"pane_id": self.pane_a, "agent_session": self.b, "agent_status": "idle"})
        self.assertEqual(got["agent_session"], self.a)
        self.assertEqual(len(self.lines("fleetpane.report_disagrees")), 1)

    # -- S13: several sessions in one pane

    def procs(self, *cmdlines):
        return [{"cmdline": c, "name": c.split(" ")[0], "pid": 1} for c in cmdlines]

    def truth_with(self, processes, reported):
        with mock.patch.object(sb.HerdrClient, "pane_process_info", return_value=processes):
            return self.engine.pane_truth({"pane_id": self.pane_a, "agent_session": reported, "agent_status": "idle"})

    def test_S13_the_result_is_the_same_in_both_process_orders_and_a_grep_names_no_session(self):
        exe = self.engine.executable
        mine, other = "%s -s %s" % (exe, self.a), "grep -s ses_other"
        for order in ((mine, other), (other, mine)):
            with self.subTest(order=order):
                got = self.truth_with(self.procs(*order), self.b)
                self.assertEqual(got["agent_session"], self.a)

    def test_S13_two_distinct_sessions_bind_nothing_with_one_audit_line(self):
        exe = self.engine.executable
        for order in ((self.a, self.b), (self.b, self.a)):
            self.assertIsNone(self.truth_with(self.procs(*["%s -s %s" % (exe, s) for s in order]), self.a))
        self.assertEqual(len(self.lines("fleetpane.report_ambiguous")), 1)

    # -- S14: a lingering process is held, and said

    def test_S14_a_pane_gone_from_the_list_but_alive_by_process_is_said_once_after_many_passes(self):
        self.run_pass()
        del self.world.herdr.panes[self.pane_b]
        self.world.herdr.panes[self.pane_a]["session"] = None
        self.world.herdr.panes[self.pane_a]["undetected"] = True
        for _ in range(sb.PANE_HELD_PASSES + 5):
            self.run_pass()
        self.assertEqual(len(self.lines("fleetpane.missing_held")), 1)
        self.assertNotEqual(self.registry.read(self.world.state)["agents"]["agent-a"]["state"], "dead")


class TestServiceEnvironmentHoldsNoPane(StandInPids, Base):
    """#1066: the OpenCode service is started WITHOUT any herdr variable, whichever process starts it."""

    def callers_env(self):
        return {"PATH": "/usr/bin", "HERDR_PANE_ID": "w1:p2", "HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1", "HERDR_ENV": "1",
                "HERDR_BIN_PATH": "/x/herdr", "HERDR_SOCKET_PATH": "/x/herdr.sock", "HOME": "/home/user"}

    def test_S9_ensure_starts_the_daemon_and_with_it_the_service_with_no_herdr_variable(self):
        seen = {}

        def spawn(argv, **kw):
            seen.update(kw["env"])
            raise OSError("stop here")
        with mock.patch.dict(os.environ, self.callers_env(), clear=True):
            with self.assertRaises(OSError):
                sb.spawn_daemon(Path(tempfile.mkdtemp()), spawn)
        self.assertEqual([n for n in ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_ENV", "HERDR_BIN_PATH", "HERDR_SOCKET_PATH") if n in seen], [])
        self.assertEqual(seen.get(sb.KEPT_SOCKET_ENV), "/x/herdr.sock")   # the daemon still finds herdr
        self.assertEqual(sb.herdr_socket_path(seen), "/x/herdr.sock")

    def test_S9_every_name_is_stripped_not_only_the_pane_one(self):
        for name in sb.SERVICE_STRIP_ENV:
            with self.subTest(name=name):
                self.assertNotIn(name, sb.service_environment({name: "x", "PATH": "/bin"}))

    def test_S10_status_names_a_service_that_carries_one_and_the_clean_restart_and_restarts_nothing(self):
        state = Path(tempfile.mkdtemp())
        (state / "opencode").mkdir()
        (state / "opencode" / "service.json").write_text(json.dumps({"pid": made_up_pid()}))
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "opencode serve PATH=/bin HERDR_PANE_ID=w1:p2 HOME=/h", "")
        found = sb.service_herdr_env(runner, {"XDG_STATE_HOME": str(state)})
        self.assertEqual(found, ["HERDR_PANE_ID"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["ps", "eww"])
        self.assertIn("service restart", sb.service_clean_restart_command())
        self.assertIn("-u HERDR_PANE_ID", sb.service_clean_restart_command())

    def test_S10_a_service_with_a_clean_environment_or_none_readable_reports_nothing(self):
        state = Path(tempfile.mkdtemp())
        self.assertEqual(sb.service_herdr_env(lambda *a, **k: None, {"XDG_STATE_HOME": str(state)}), [])
        (state / "opencode").mkdir()
        (state / "opencode" / "service.json").write_text(json.dumps({"pid": 1}))
        clean = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "opencode serve HOME=/h", "")
        self.assertEqual(sb.service_herdr_env(clean, {"XDG_STATE_HOME": str(state)}), [])

    # -- R-CODE-1: only the six environment ENTRIES go out of a unit, never another line

    def whole_line_filter(self, text):
        """The mutant: the old filter, which dropped every line that merely held a HERDR_ name."""
        return "".join(l for l in text.splitlines(True) if not any(n + "=" in l or ("<key>%s</key>" % n) in l for n in sb.SERVICE_STRIP_ENV)
                       and not any(n in l for n in ("HERDR_",)))

    def unit_with_entries(self, kind):
        """A throwaway kit whose template of `kind` also holds the six entries (and a comment naming one)."""
        kit = Path(tempfile.mkdtemp())
        (kit / "templates").mkdir()
        name = sb.SERVICE_LABEL + ".plist" if kind == "launchd" else sb.SERVICE_UNIT
        real = (sb.KIT_ROOT / "templates" / name).read_text()
        if kind == "launchd":
            extra = "".join("\t\t<key>%s</key>\n\t\t<string>x</string>\n" % n for n in sb.SERVICE_STRIP_ENV)
            text = real.replace("\t\t<key>PATH</key>", extra + "\t\t<key>PATH</key>", 1)
        else:
            extra = "".join('Environment="%s=x"\n' % n for n in sb.SERVICE_STRIP_ENV)
            text = real.replace('Environment="PATH=__PATH__"', extra + 'Environment="PATH=__PATH__"', 1)
        self.assertNotEqual(text, real)
        (kit / "templates" / name).write_text(text)
        return kit

    def test_the_unit_keeps_its_path_line_even_when_the_path_holds_herdr_text_and_loses_only_the_six_entries(self):
        for kind in ("launchd", "systemd"):
            values = dict(sb.service_values(sb.KIT_ROOT, ["/bin"]), __PATH__="/bin:/opt/HERDR_PANE_ID=w1:/opt/HERDR_x")
            for kit, label in ((sb.KIT_ROOT, "the shipped template"), (self.unit_with_entries(kind), "a template holding the six entries")):
                with self.subTest(kind=kind, template=label):
                    text = sb.render_service(kind, values, kit)
                    self.assertIn("/bin:/opt/HERDR_PANE_ID=w1:/opt/HERDR_x", text)   # the PATH line survives
                    for n in sb.SERVICE_STRIP_ENV:
                        self.assertNotIn('Environment="%s=' % n, text)
                        self.assertNotIn("<key>%s</key>" % n, text)
        self.assertIn("UnsetEnvironment=HERDR_PANE_ID", sb.render_service("systemd", sb.service_values(sb.KIT_ROOT, ["/bin"])))   # a line that only names them stays

    def test_every_one_of_the_six_names_is_stripped_alone_and_a_longer_name_is_not(self):
        for n in sb.SERVICE_STRIP_ENV:
            with self.subTest(name=n):
                self.assertEqual(sb.strip_service_env_entries('a\nEnvironment="%s=1"\nb\n' % n), "a\nb\n")
                self.assertEqual(sb.strip_service_env_entries("a\n<key>%s</key>\n<string>1</string>\nb\n" % n), "a\nb\n")
                keep = 'a\nEnvironment="X%s=1"\nEnvironment="%s_MORE=1"\nb\n' % (n, n)
                self.assertEqual(sb.strip_service_env_entries(keep), keep)

    def test_a_mutant_that_goes_back_to_the_whole_line_filter_loses_the_path_line(self):
        values = dict(sb.service_values(sb.KIT_ROOT, ["/bin"]), __PATH__="/bin:/opt/HERDR_PANE_ID=w1")
        with mock.patch.object(sb, "strip_service_env_entries", lambda text: text):
            text = self.whole_line_filter(sb.render_service("systemd", values))
        self.assertNotIn("/opt/HERDR_PANE_ID=w1", text)   # the mutant drops the PATH line: the test above would fail on it

    # -- R-TEST-1: the daemon's own run path

    def test_the_launchd_value_may_span_lines_and_still_goes_with_its_key(self):
        for n in sb.SERVICE_STRIP_ENV:
            with self.subTest(name=n):
                one = "a\n\t<key>%s</key>\n\t<string>x</string>\nb\n" % n
                many = "a\n\t<key>%s</key>\n\t<string>\n\t\tx\n\t</string>\nb\n" % n
                self.assertEqual((sb.strip_service_env_entries(one), sb.strip_service_env_entries(many)), ("a\nb\n", "a\nb\n"))
        keep = "<key>PATH</key>\n<string>\n/bin\n</string>\n"
        self.assertEqual(sb.strip_service_env_entries(keep), keep)

    def test_a_real_process_started_by_cmd_run_holds_none_of_the_six_names(self):
        """Real subprocess (R-TEST-1): cmd_run in a fresh interpreter; the environment its child sees, not a stub's."""
        script = ("import importlib.machinery, importlib.util, json, os, subprocess, sys, types\n"
                  "l = importlib.machinery.SourceFileLoader('sbx', %r); spec = importlib.util.spec_from_loader('sbx', l)\n"
                  "sb = importlib.util.module_from_spec(spec); sys.modules['sbx'] = sb; l.exec_module(sb)\n"
                  "def stop(*a, **k):\n"
                  "    out = subprocess.run(['env'], capture_output=True, text=True).stdout\n"
                  "    print(json.dumps(sorted(x.split('=')[0] for x in out.splitlines() if '=' in x)))\n"
                  "    raise SystemExit(0)\n"
                  "sb.build_engine = stop\n"
                  "sb.cmd_run(types.SimpleNamespace(once=False, standby=False, json=False, poll=None), None, None, None)\n") % str(REPO / "bin" / "fleet-switchboard")
        env = dict(self.callers_env(), HOME=tempfile.mkdtemp())
        done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        names = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual([n for n in sb.SERVICE_STRIP_ENV if n in names], [], done.stderr[-300:])
        self.assertIn(sb.KEPT_SOCKET_ENV, names)

    def test_service_herdr_env_reads_a_real_process_with_ps_eww(self):
        """Real subprocess: a child that holds HERDR_PANE_ID is named; a clean one is not."""
        state = Path(tempfile.mkdtemp())
        (state / "opencode").mkdir()
        results = {}
        for label, extra in (("dirty", {"HERDR_PANE_ID": "w1:p2"}), ("clean", {})):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=dict({"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, **extra))
            try:
                time.sleep(0.7)   # the interpreter has started: ps shows the environment of the process, not of the forking parent. (A system binary such as sleep hides its own)
                (state / "opencode" / "service.json").write_text(json.dumps({"pid": child.pid}))
                results[label] = sb.service_herdr_env(subprocess.run, {"XDG_STATE_HOME": str(state)})
            finally:
                child.kill()
                child.wait()
        self.assertEqual(results, {"dirty": ["HERDR_PANE_ID"], "clean": []})

    def run_daemon_env(self):
        """cmd_run (not --once) with all six names set; the environment build_engine sees."""
        seen = {}

        def stop(*a, **k):
            seen.update(os.environ)
            raise RuntimeError("stop")
        env = self.callers_env()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(sb, "build_engine", side_effect=stop):
                with self.assertRaises(RuntimeError):
                    sb.cmd_run(types.SimpleNamespace(once=False, standby=False, json=False, poll=None), None, None, None)
        return seen

    def test_cmd_run_leaves_the_daemon_and_so_the_service_with_none_of_the_six_names(self):
        seen = self.run_daemon_env()
        self.assertEqual([n for n in sb.SERVICE_STRIP_ENV if n in seen], [])
        self.assertEqual(seen.get(sb.KEPT_SOCKET_ENV), "/x/herdr.sock")   # the daemon still finds herdr

    def test_a_mutant_cmd_run_that_passes_the_names_through_is_caught(self):
        with mock.patch.object(sb, "SERVICE_STRIP_ENV", ()):
            seen = self.run_daemon_env()
        self.assertEqual([n for n in ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID") if n in seen], [])   # these are in CALLER_ENV_NAMES too
        self.assertIn("HERDR_ENV", seen)   # the three only SERVICE_STRIP_ENV names carry would leak: the real test above catches it
        self.assertIn("HERDR_BIN_PATH", seen)

    # -- R-TEST-5: the status line and the error paths of service_herdr_env

    def service_dir(self, content=None, raw=None):
        state = Path(tempfile.mkdtemp())
        (state / "opencode").mkdir()
        if raw is not None:
            (state / "opencode" / "service.json").write_text(raw)
        elif content is not None:
            (state / "opencode" / "service.json").write_text(json.dumps(content))
        return {"XDG_STATE_HOME": str(state)}

    def test_service_herdr_env_names_each_of_the_six_and_only_the_exact_name(self):
        env = self.service_dir({"pid": self.px[4242]})
        for n in sb.SERVICE_STRIP_ENV:
            with self.subTest(name=n):
                ran = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "opencode serve HOME=/h %s=v" % n, "")
                self.assertEqual(sb.service_herdr_env(ran, env), [n])
                other = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "opencode serve X%s=v %s_MORE=v" % (n, n), "")
                self.assertEqual(sb.service_herdr_env(other, env), [])

    def test_service_herdr_env_gives_nothing_on_every_error_path_and_never_a_guess(self):
        clean = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "opencode serve HERDR_PANE_ID=w1:p2", "")
        def raises(exc):
            def run(argv, **kw):
                raise exc
            return run
        cases = (("no service file", self.service_dir(), clean),
                 ("a file that is not json", self.service_dir(raw="{torn"), clean),
                 ("a file with no pid", self.service_dir({"url": "x"}), clean),
                 ("a pid that is not a number", self.service_dir({"pid": "abc"}), clean),
                 ("ps times out", self.service_dir({"pid": self.px[4242]}), raises(subprocess.TimeoutExpired("ps", 5))),
                 ("ps cannot start", self.service_dir({"pid": self.px[4242]}), raises(OSError("no ps"))),
                 ("the runner answers nothing", self.service_dir({"pid": self.px[4242]}), lambda *a, **k: None),
                 ("ps prints nothing for a gone process", self.service_dir({"pid": self.px[4242]}), lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")))
        for label, env, runner in cases:
            with self.subTest(case=label):
                self.assertEqual(sb.service_herdr_env(runner, env), [])

    def test_S17_a_service_file_nested_deeper_than_the_bound_reads_as_no_variables(self):
        clean = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "opencode serve HERDR_PANE_ID=w1:p2", "")
        deep = "[" * 5000 + "]" * 5000
        self.assertEqual(sb.service_herdr_env(clean, self.service_dir(raw=deep)), [])

    def test_a_mutant_that_guesses_on_an_error_path_is_caught(self):
        with mock.patch.object(sb, "SERVICE_STRIP_ENV", ()):
            self.assertEqual(sb.service_herdr_env(lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "x HERDR_PANE_ID=1", ""), self.service_dir({"pid": 1})), [])
        self.assertEqual(sb.service_herdr_env(lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "x HERDR_PANE_ID=1", ""), self.service_dir({"pid": 1})), ["HERDR_PANE_ID"])

    def test_the_status_line_for_a_running_service_that_carries_one_names_it_and_the_one_clean_restart(self):
        report = {"service": {"kind": "launchd", "installed": True, "path": "/p", "herdr_env": ["HERDR_PANE_ID", "HERDR_ENV"]}}
        service = report["service"]
        carrying = [l for l in self.install_lines(service) if "carries" in l]
        self.assertEqual(len(carrying), 1)
        self.assertIn("HERDR_PANE_ID, HERDR_ENV", carrying[0])
        self.assertEqual(carrying[0].count("service restart"), 1)
        self.assertIn(sb.service_clean_restart_command(), carrying[0])
        for n in sb.SERVICE_STRIP_ENV:
            self.assertIn("-u " + n, sb.service_clean_restart_command())
        self.assertEqual([l for l in self.install_lines(dict(service, herdr_env=[])) if "carries" in l], [])   # clean: no line
        self.assertEqual([l for l in self.install_lines({k: v for k, v in service.items() if k != "herdr_env"}) if "carries" in l], [])

    def test_the_status_line_restarts_nothing(self):
        ran = []
        with mock.patch.object(subprocess, "run", side_effect=lambda *a, **k: ran.append(a)):
            self.install_lines({"kind": "launchd", "installed": True, "path": "/p", "herdr_env": ["HERDR_ENV"]})
        self.assertEqual(ran, [])

    def install_lines(self, service):
        daemon = {"running": False, "pid": None, "key_source": None}
        return sb.format_install_lines({"daemon": daemon, "key": sb.key_report(None, daemon, service), "plugin": {"state": "unknown", "detail": ""}, "service": service})

    def test_a_mutant_status_that_never_reads_the_service_environment_loses_the_line(self):
        service = {"kind": "launchd", "installed": True, "path": "/p", "herdr_env": ["HERDR_ENV"]}
        self.assertTrue([l for l in self.install_lines(service) if "carries" in l])
        with mock.patch.object(sb, "service_herdr_env", lambda *a, **k: ["HERDR_ENV"]):
            self.assertEqual(sb.collect_status(lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""), time.time)["service"]["herdr_env"], ["HERDR_ENV"])   # collect_status carries what it reads


class TestEveryFunctionOf1066IsPinned(unittest.TestCase):
    """The family of #1066: every function the change added or changed has a row naming the test that drives it; a row whose function or test is gone fails."""

    ROWS = {
        "Switchboard.pane_truth": ("TestPaneBoundByProcess", "test_S2_a_disagreeing_report_keeps_a_bound_and_alive_and_b_gets_no_pane"),
        "Switchboard.discover": ("TestPaneBoundByProcess", "test_S4_a_live_pane_is_not_rebound_and_an_unreadable_process_list_binds_nothing_new"),
        "pane_health_pass": ("TestPaneBoundByProcess", "test_S6_a_pane_whose_process_runs_the_session_is_never_dead_even_when_the_list_omits_it"),
        "herdr_socket_path": ("TestServiceEnvironmentHoldsNoPane", "test_S9_ensure_starts_the_daemon_and_with_it_the_service_with_no_herdr_variable"),
        "service_environment": ("TestServiceEnvironmentHoldsNoPane", "test_S9_every_name_is_stripped_not_only_the_pane_one"),
        "daemon_environment": ("TestIdentityCheck", "test_a_daemon_started_from_an_agents_shell_carries_no_caller_identity"),
        "spawn_daemon": ("TestServiceEnvironmentHoldsNoPane", "test_S9_ensure_starts_the_daemon_and_with_it_the_service_with_no_herdr_variable"),
        "cmd_run": ("TestServiceEnvironmentHoldsNoPane", "test_cmd_run_leaves_the_daemon_and_so_the_service_with_none_of_the_six_names"),
        "service_herdr_env": ("TestServiceEnvironmentHoldsNoPane", "test_service_herdr_env_gives_nothing_on_every_error_path_and_never_a_guess"),
        "service_clean_restart_command": ("TestServiceEnvironmentHoldsNoPane", "test_the_status_line_for_a_running_service_that_carries_one_names_it_and_the_one_clean_restart"),
        "format_install_lines": ("TestServiceEnvironmentHoldsNoPane", "test_the_status_line_for_a_running_service_that_carries_one_names_it_and_the_one_clean_restart"),
        "collect_status": ("TestServiceEnvironmentHoldsNoPane", "test_a_mutant_status_that_never_reads_the_service_environment_loses_the_line"),
        "render_service": ("TestServiceEnvironmentHoldsNoPane", "test_the_unit_keeps_its_path_line_even_when_the_path_holds_herdr_text_and_loses_only_the_six_entries"),
        "strip_service_env_entries": ("TestServiceEnvironmentHoldsNoPane", "test_every_one_of_the_six_names_is_stripped_alone_and_a_longer_name_is_not"),
        "registry.upsert": ("TestPaneBoundByProcess", "test_S3_a_cloud_worker_is_never_recorded_with_a_pane"),
    }

    def test_every_row_names_a_function_and_a_test_that_exist(self):
        for fn, (cls, test) in self.ROWS.items():
            with self.subTest(function=fn):
                owner = sb.load_module("registry") if fn.startswith("registry.") else sb.Switchboard if fn.startswith("Switchboard.") else sb
                self.assertTrue(callable(getattr(owner, fn.split(".")[-1], None)), fn)
                self.assertTrue(callable(getattr(globals()[cls], test, None)), "%s.%s" % (cls, test))


class TestRestartGuard(Base):
    """After a herdr restart a pane can come back as a shell, and herdr resumes
    the agent by typing `opencode` into it, which may start another OpenCode.
    Nothing is delivered to a pane that does not run the configured v2."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform",))
        self.pane = self.world.panes["platform"]

    def wrong_program(self, program="/opt/homebrew/bin/opencode --session ses_x"):
        self.world.herdr.panes[self.pane]["program"] = program

    def test_runs_program_means_the_executable_alone_or_with_arguments(self):
        exe = "/v2/bin/opencode"
        for cmdline, expected in ((exe, True), (exe + " -s ses_1", True), (exe + "-v1 -s x", False),
                                  ("/opt/homebrew/bin/opencode", False), ("-zsh", False), (None, False)):
            with self.subTest(cmdline=cmdline):
                self.assertEqual(sb.runs_program(cmdline, exe), expected)

    def test_a_pane_on_another_program_is_held_and_nothing_is_written(self):
        self.wrong_program()
        self.world.fact("platform", "lab.fact:1", batch=False)
        report = self.world.engine.run_pass()
        self.assertEqual(self.world.v2.writes, [])
        self.assertEqual(report["sent"], [])
        self.assertIn("different opencode", report["held"][0]["reason"])
        self.assertTrue(any(e["event"] == "hold" and "different opencode" in e["detail"]["reason"]
                            for e in self.world.audit()))

    def test_the_guard_is_audited_even_when_nothing_is_pending(self):
        self.wrong_program()
        self.world.engine.run_pass()
        self.world.engine.run_pass()  # a steady state is audited once, not every pass
        guards = [e for e in self.world.audit() if e["event"] == "program.guard"]
        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0]["subject"], "platform")

    def test_it_is_toasted_once_and_the_hold_ends_when_the_pane_is_right(self):
        self.wrong_program("-zsh")
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.engine.run_pass()
        self.world.tick(5)
        self.world.engine.run_pass()
        self.assertEqual([t["title"] for t in self.world.herdr.toasts], ["platform: pane runs a different opencode"])
        self.wrong_program("%s -s %s" % (FAKE_OPENCODE, self.world.sessions["platform"]))
        self.world.tick(5)
        report = self.world.engine.run_pass()
        self.assertEqual(len(report["sent"]), 1)

    def test_the_recheck_before_a_write_sees_a_pane_that_changed_meanwhile(self):
        self.world.fact("platform", "lab.fact:1", batch=False)
        engine = self.world.engine
        discovery = engine.discover(self.world.clock.now())
        facts, _ = engine.collect_facts()
        plan = engine.plan("platform", facts, discovery, self.world.clock.now())
        self.assertEqual(plan.action, "wake")
        self.wrong_program()
        outcome = engine.deliver(plan)
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["action"], "hold")
        self.assertEqual(self.world.v2.writes, [])

    def test_send_is_refused_for_such_a_pane(self):
        self.wrong_program()
        outcome = self.world.new_engine("cli").send("coder", "platform", "hi")
        self.assertEqual(outcome["action"], "hold")
        self.assertEqual(self.world.v2.writes, [])

    def test_a_pane_that_cannot_be_read_is_held_not_assumed_fine(self):
        self.world.herdr.panes[self.pane]["program"] = None
        original = self.world.herdr._process_info

        def failing(rest):
            raise LookupError("no such pane")

        self.world.herdr._process_info = failing
        self.world.fact("platform", "lab.fact:1", batch=False)
        report = self.world.engine.run_pass()
        self.assertEqual(report["sent"], [])
        self.assertIn("cannot read what pane", report["held"][0]["reason"])
        self.world.herdr._process_info = original

    def test_a_wrapper_is_checked_by_the_executable_it_runs(self):
        world = World(self, agents=("platform",), config={
            "opencode": "/fake/bin/wrapper", "opencode_executable": FAKE_OPENCODE})
        world.fact("platform", "lab.fact:1", batch=False)
        self.assertEqual(len(world.engine.run_pass()["sent"]), 1)  # the pane runs FAKE_OPENCODE
        other = World(self, agents=("platform",), config={
            "opencode": "/fake/bin/wrapper", "opencode_executable": "/fake/bin/the-real-one"},
            state=self.home / "other")
        other.fact("platform", "lab.fact:1", batch=False)
        self.assertEqual(other.engine.run_pass()["sent"], [])

    def test_a_relative_executable_is_a_config_error(self):
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode_executable": "opencode"})
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"launch_env": {"A": 1}})


class TestRender(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.engine = self.world.engine

    def pane(self, name="platform"):
        return self.world.herdr.panes[self.world.panes[name]]

    def block(self, name="platform"):
        self.pane(name)["status"] = "blocked"

    def badge_writes(self):
        return [a for a in self.world.herdr.calls if a[2:3] == ["report-metadata"]]

    def test_held_items_are_the_badge_and_it_is_written_only_when_it_changes(self):
        self.block()
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.fact("platform", "lab.fact:2", batch=False)
        report = self.engine.run_pass()
        self.assertEqual(report["badges"], [{"agent": "platform", "count": 2}])
        self.assertEqual(self.pane()["tokens"], {"unread": "2"})
        self.assertEqual(len(self.badge_writes()), 1)
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual(len(self.badge_writes()), 1)  # the agent list already shows 2

    def test_the_badge_is_restored_after_herdr_forgets_its_tokens(self):
        self.block()
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.engine.run_pass()
        self.pane()["tokens"] = {}  # a herdr restart
        self.engine.run_pass()
        self.assertEqual(self.pane()["tokens"], {"unread": "1"})

    def test_it_clears_when_nothing_is_unread_and_not_before(self):
        self.engine.run_pass()
        self.assertEqual(self.badge_writes(), [])  # nothing unread, no token: no write
        self.block()
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.engine.run_pass()
        self.pane()["status"] = None
        self.world.tick(5)
        self.engine.run_pass()  # unblocked: the item is delivered as a wake
        self.assertEqual(self.pane()["tokens"], {})
        self.assertEqual(self.badge_writes()[-1][2:], ["report-metadata", self.world.panes["platform"],
                                                      "--source", "fleet-switchboard", "--clear-token", "unread"])

    def test_a_note_is_unread_until_a_turn_carries_it(self):
        self.world.you("platform")  # you are engaged: the next item is a note
        self.world.tick(1)
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.fact("platform", "lab.fact:2", batch=False)
        self.engine.run_pass()
        self.assertEqual(len(self.world.v2.sessions[self.world.sessions["platform"]]["inbox"]), 1)
        self.assertEqual(self.pane()["tokens"], {"unread": "2"})  # one note, two facts
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual(self.pane()["tokens"], {"unread": "2"})
        self.world.you("platform", "and next?")  # a turn starts and carries the note
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual(self.pane()["tokens"], {})

    def test_a_wake_is_never_unread(self):
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.engine.run_pass()
        self.assertEqual(self.pane()["tokens"], {})

    def test_badge_writes_are_audited_before_and_after(self):
        self.block()
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.engine.run_pass()
        entries = [e for e in self.world.audit() if e["event"] == "herdr.badge"]
        self.assertEqual([e["phase"] for e in entries], ["intent", "result"])
        self.assertEqual((entries[0]["detail"]["count"], entries[0]["detail"]["was"]), (1, None))

    def test_a_blocked_agent_is_toasted_once_until_the_reason_changes(self):
        self.block()
        for _ in range(3):
            self.engine.run_pass()
            self.world.tick(5)
        self.assertEqual([t["title"] for t in self.world.herdr.toasts], ["platform is blocked"])
        self.pane()["status"] = None
        self.engine.run_pass()
        self.world.tick(5)
        self.block()  # blocked again: a new reason after the old one cleared
        self.engine.run_pass()
        self.assertEqual(len(self.world.herdr.toasts), 2)

    def test_an_item_held_fifteen_minutes_is_toasted_and_not_before(self):
        self.world.fact("ghost", "lab.fact:1", batch=False)
        self.engine.run_pass()
        self.world.tick(14 * 60)
        self.engine.run_pass()
        self.assertEqual(self.world.herdr.toasts, [])
        self.world.tick(61)
        self.engine.run_pass()
        self.assertEqual([t["title"] for t in self.world.herdr.toasts], ["ghost: 1 item(s) held"])
        self.assertIn("ghost not found", self.world.herdr.toasts[0]["body"])
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual(len(self.world.herdr.toasts), 1)

    def test_a_changed_hold_reason_is_a_new_toast(self):
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.herdr.panes[self.world.panes["platform"]]["status"] = "unknown"
        self.engine.run_pass()
        self.world.tick(16 * 60)
        self.engine.run_pass()
        self.assertEqual(len(self.world.herdr.toasts), 1)
        self.block()
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual([t["title"] for t in self.world.herdr.toasts],
                         ["platform: 1 item(s) held", "platform is blocked"])

    def test_one_notification_call_per_pass_even_with_two_reasons(self):
        self.block("platform")
        self.block("coder")
        self.engine.run_pass()
        calls = [a for a in self.world.herdr.calls if a[1] == "notification"]
        self.assertEqual(len(calls), 1)
        self.world.tick(5)
        self.engine.run_pass()
        self.assertEqual(sorted(t["title"] for t in self.world.herdr.toasts),
                         ["coder is blocked", "platform is blocked"])

    def test_rate_limited_busy_and_no_client_are_owed_and_retried_next_pass(self):
        self.block()
        self.world.herdr.toast_replies = ["rate_limited", "busy", "no_foreground_client"]
        for expected in (0, 0, 0, 1):
            report = self.engine.run_pass()
            self.assertEqual(len(self.world.herdr.toasts), expected)
            self.world.tick(5)
        self.assertEqual([t["why"] for t in report["toasts"]], ["shown"])

    def test_disabled_is_final_and_not_retried(self):
        self.block()
        self.world.herdr.toast_replies = ["disabled"]
        for _ in range(3):
            self.engine.run_pass()
            self.world.tick(5)
        self.assertEqual(len([a for a in self.world.herdr.calls if a[1] == "notification"]), 1)

    def test_the_reply_is_audited(self):
        self.block()
        self.world.herdr.toast_replies = ["rate_limited"]
        self.engine.run_pass()
        entries = [e for e in self.world.audit() if e["event"] == "herdr.notification"]
        self.assertEqual([e["phase"] for e in entries], ["intent", "result"])
        self.assertEqual(entries[1]["detail"]["reply"], {"shown": False, "reason": "rate_limited"})

    def test_a_failing_toast_never_stops_a_delivery(self):
        self.block("coder")
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.herdr.toast_replies = []
        real = self.world.herdr._notification_show
        self.world.herdr._notification_show = lambda rest: (_ for _ in ()).throw(LookupError("herdr broke"))
        report = self.engine.run_pass()
        self.world.herdr._notification_show = real
        self.assertEqual(len(report["sent"]), 1)
        self.assertEqual([e["step"] for e in report["errors"]] if False else
                         [e["subject"] for e in report["errors"]], [None])

    def test_a_read_only_engine_renders_nothing(self):
        self.block()
        self.world.fact("platform", "lab.fact:1", batch=False)
        self.world.new_engine(writable=False).run_pass()
        self.assertEqual(self.badge_writes(), [])
        self.assertEqual(self.world.herdr.toasts, [])


class TestGithubReceive(Base):
    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self)
        self.hub, self.gh = self.rig.hub, self.rig.gh
        self.hub.tick()  # starts the forwarder: a secret exists now
        self.secret = self.gh.procs[-1].secret
        self.gh.issue(12, parent=1)

    def post(self, event="issue_comment", payload=None, secret=None, signature="auto", path="/github", raw=None):
        payload = payload if payload is not None else {
            "action": "created", "issue": self.gh.issues[12],
            "comment": {"id": 5001, "body": "hi", "user": {"login": "octocat"}},
            "repository": {"full_name": GH_REPO}}
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1"}
        if signature == "auto":
            signature = sb.sign_body(secret or self.secret, body)
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.hub.receive(path, headers, body)

    def rejected(self):
        return [e for e in self.rig.world.audit() if e["event"] == "github.rejected"]

    def pr_payload(self, action, **pr):
        return {"action": action, "pull_request": dict({"id": 91, "number": 9, "body": "Closes #12", "state": "open", "title": "t"}, **pr),
                "repository": {"full_name": GH_REPO}}

    def test_a_pull_request_push_and_a_comment_through_the_hub_leave_one_movement_row(self):
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.post("pull_request", self.pr_payload("synchronize"))[0], 202)
        rows = sb.load_fleet_health().read_moves(str(self.rig.world.state), 0)
        self.assertEqual([(r["kind"], r["numbers"], r["repo"]) for r in rows], [("push", [9, 12], GH_REPO)])
        self.assertEqual(stat.S_IMODE((self.rig.world.state / "ask-moves.jsonl").stat().st_mode), 0o600)

    def test_a_movement_row_that_cannot_be_written_is_audited_and_the_delivery_still_succeeds(self):
        with mock.patch.object(sb.load_fleet_health(), "append_moves", side_effect=OSError("disk full")):
            self.assertEqual(self.post("pull_request", self.pr_payload("opened"))[0], 202)
        failed = [e for e in self.rig.world.audit() if e["event"] == "load.moves_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("disk full", failed[0]["detail"]["error"])

    def test_a_closed_issue_delivery_marks_the_repository_stale_and_a_comment_does_not(self):
        health = sb.load_fleet_health()
        self.assertEqual(self.post()[0], 202)                                              # a comment
        self.assertIsNone(health.stale_since(str(self.rig.world.state), GH_REPO))
        self.assertEqual(self.post("issues", {"action": "closed", "issue": self.gh.issues[12], "repository": {"full_name": GH_REPO}})[0], 202)
        self.assertIsNotNone(health.stale_since(str(self.rig.world.state), GH_REPO))
        self.assertEqual(stat.S_IMODE((self.rig.world.state / health.STALE_MARK_FILE).stat().st_mode), 0o600)

    def test_a_stale_mark_that_cannot_be_written_is_audited_and_the_delivery_still_succeeds(self):
        with mock.patch.object(sb.load_fleet_health(), "mark_stale", side_effect=OSError("disk full")):
            self.assertEqual(self.post("issues", {"action": "closed", "issue": self.gh.issues[12], "repository": {"full_name": GH_REPO}})[0], 202)
        self.assertEqual(len([e for e in self.rig.world.audit() if e["event"] == "load.stale_failed"]), 1)

    def test_a_signed_event_is_accepted_and_queued_by_its_object_id(self):
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.rig.keys(), ["github.comment:5001"])

    def test_the_github_received_entry_keeps_a_few_ids_and_a_count_not_every_key(self):
        self.assertEqual(self.post()[0], 202)
        (one,) = [e for e in self.rig.world.audit() if e["event"] == "github.received"]
        self.assertEqual((one["detail"]["delivery"], one["detail"]["count"], one["detail"]["keys"]), ("d-1", 1, ["github.comment:5001"]))
        many = [sb.GitHubItem("github.comment:%d" % (7000 + n) + "x" * 100, "github.comment", "c", GH_REPO, [], [], None, 1.0, [], False) for n in range(8)]
        with mock.patch.object(sb, "webhook_items", lambda event, payload, now: many):
            self.assertEqual(self.post()[0], 202)
        last = [e for e in self.rig.world.audit() if e["event"] == "github.received"][-1]["detail"]
        self.assertEqual(last["count"], 8)
        self.assertEqual(len(last["keys"]), 5)
        self.assertTrue(all(len(k) <= 80 for k in last["keys"]))

    def test_an_unsigned_event_is_refused_audited_and_ingests_nothing(self):
        self.assertEqual(self.post(signature=None)[0], 401)
        self.assertEqual(self.rig.keys(), [])
        self.assertEqual([e["detail"]["reason"] for e in self.rejected()], ["signature missing"])

    def test_a_wrongly_signed_event_is_refused_audited_and_ingests_nothing(self):
        for what, kwargs in (("another secret", {"secret": "z" * 64}), ("garbage", {"signature": "sha256=00"}),
                             ("a stale secret", {})):
            with self.subTest(what=what):
                if what == "a stale secret":
                    self.gh.kill_forwarder()
                    self.rig.pump(10)
                    self.assertNotEqual(self.gh.procs[-1].secret, self.secret)
                code, _ = self.post(**kwargs) if what != "a stale secret" else self.post(secret=self.secret)
                self.assertEqual(code, 401)
                self.assertEqual(self.rig.keys(), [])
        self.assertEqual({e["detail"]["reason"] for e in self.rejected()}, {"signature does not match"})

    def test_the_signature_is_checked_before_the_body_is_read(self):
        # A body that is not JSON gets 401 when unsigned, and 400 only once it is signed.
        self.assertEqual(self.post(raw=b"{not json", signature=None)[0], 401)
        self.assertEqual(self.post(raw=b"{not json")[0], 400)
        self.assertEqual(self.post(raw=b"[1]")[0], 400)

    def test_only_the_github_path_answers(self):
        self.assertEqual(self.post(path="/other")[0], 404)
        self.assertEqual(self.rig.keys(), [])

    def test_an_event_for_another_repo_is_ignored(self):
        payload = {"action": "created", "issue": self.gh.issues[12], "comment": {"id": 5002},
                   "repository": {"full_name": "someone/else"}}
        self.assertEqual(self.post(payload=payload)[0], 202)
        self.assertEqual(self.rig.keys(), [])

    def test_events_nobody_handles_ingest_nothing(self):
        self.assertEqual(self.post(event="ping", payload={"zen": "x", "repository": {"full_name": GH_REPO}})[0], 202)
        self.assertEqual(self.post(event="push", payload={"repository": {"full_name": GH_REPO}})[0], 202)
        self.assertEqual(self.rig.keys(), [])

    def test_a_flood_of_bad_signatures_is_mostly_counted_not_audited(self):
        for _ in range(sb.GITHUB_REJECT_AUDITS + 30):
            self.post(signature="sha256=00")
        self.assertEqual(len(self.rejected()), sb.GITHUB_REJECT_AUDITS)
        self.rig.world.clock.t += 61
        self.post(signature="sha256=00")
        self.assertEqual(self.rejected()[-1]["detail"]["also_rejected"], 30)

    def test_a_redelivery_with_a_new_delivery_id_is_one_item(self):
        for delivery in ("d-1", "d-2"):
            self.gh.send("issue_comment", {"action": "created", "issue": self.gh.issues[12],
                                           "comment": {"id": 5001, "body": "hi"}}, delivery)
        self.assertEqual(self.rig.keys(), ["github.comment:5001"])


class TestGithubPrRouting(Base):
    """PR 52: events on a PR the fleet opened reach the agent responsible for it, without a closing reference."""

    def setUp(self):
        super().setUp()
        self.agents = {"platform": {"role": "orchestrator", "charter": 1},
                       "coder": {"role": "coder", "issue": 12, "reports_to": "platform"}}
        self.start()

    def start(self, config=None):
        self.rig = GithubRig(self, agents=self.agents, config=config)
        self.gh, self.hub, self.world = self.rig.gh, self.rig.hub, self.rig.world
        self.gh.issue(1)
        self.gh.issue(12, parent=1)
        self.gh.issue(13, parent=1)
        self.gh.issue(20)
        self.hub.tick()

    def pr(self, number, title="a change", body="", ref="a-branch"):
        pull = self.gh.pull(number, title=title)
        pull["body"], pull["head"]["ref"] = body, ref
        return pull

    def notice(self, number, sender="coder", boss="platform"):
        return sb.record_pr_notice(self.world.state, GH_REPO, number, sender, "ready", self.world.clock.now(), boss)

    def check(self, number, conclusion="failure", cid=8001, event="check_suite"):
        self.gh.send(event, {"action": "completed", event: {"id": cid, "conclusion": conclusion, "name": "ci",
                                                           "pull_requests": [{"number": number}]}})

    def routed(self):
        return {f.key: f.recipient for f in self.hub.facts()}

    def audit(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def routed_by(self):
        return {k: e["detail"]["routed_by"] for e in self.audit("github.routed") for k in e["detail"]["keys"]}

    # -- the links

    def test_a_pr_with_a_notice_goes_to_its_sender(self):
        self.pr(40)
        self.notice(40)
        self.check(40)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "coder"})
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "notice"})

    def test_a_notice_whose_sender_is_gone_goes_to_the_senders_boss(self):
        self.pr(40)
        self.notice(40, sender="ghost", boss="platform")
        self.check(40)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "platform"})
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "notice"})

    def test_a_notice_whose_sender_and_boss_are_gone_is_unrouted_and_the_audit_names_the_pr(self):
        self.pr(40)
        self.notice(40, sender="ghost", boss="ghost-boss")
        self.check(40)
        self.assertEqual(self.routed(), {})
        (line,) = self.audit("github.unrouted")
        self.assertEqual((line["subject"], line["detail"]["prs"], line["detail"]["count"]), (GH_REPO, [40], 1))

    def test_after_a_withdraw_a_comment_on_the_pr_no_longer_routes_to_the_old_sender(self):
        self.pr(40)
        self.notice(40)
        self.assertEqual(sb.withdraw_pr_notice(self.world.state, GH_REPO, 40, "coder")[0], "withdrawn")
        self.check(40)
        self.assertEqual(self.routed(), {}, "no notice, so no notice link")
        (line,) = self.audit("github.unrouted")
        self.assertEqual(line["detail"]["prs"], [40])

    def test_after_a_drop_the_pr_routes_as_a_pr_with_no_notice(self):
        self.pr(41, body="This is the work for #12, no keyword.")
        self.notice(41, sender="stray-agent", boss="platform")
        sb.write_pr_notices(self.world.state, [])  # what apply_notice_effects does for a dropped entry
        self.check(41)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "platform"}, "the PR's own references decide, not a notice")
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "reference"})

    def test_the_notice_records_the_senders_boss_and_a_resend_keeps_it(self):
        item, _ = self.notice(40)
        self.assertEqual(item["boss"], "platform")
        item, refreshed = sb.record_pr_notice(self.world.state, GH_REPO, 40, "coder", "again", 1.0)
        self.assertEqual((refreshed, item["boss"]), (True, "platform"))

    def test_a_pr_with_one_plain_reference_goes_to_the_owner_of_that_issue(self):
        self.pr(41, body="This is the work for #12, no keyword.")
        self.check(41)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "platform"})
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "reference"})

    def test_a_pr_with_two_plain_references_stays_unrouted(self):
        self.pr(41, title="for #12", body="and also #13")
        self.check(41)
        self.assertEqual(self.routed(), {})
        self.assertEqual(self.audit("github.unrouted")[0]["detail"]["prs"], [41])

    def test_a_reference_to_an_issue_nobody_owns_stays_unrouted(self):
        self.pr(41, body="about #20")
        self.check(41)
        self.assertEqual(self.routed(), {})

    def test_an_issue_number_in_the_head_branch_name_is_a_reference(self):
        self.pr(42, ref="drift12")
        self.check(42)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "platform"})
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "reference"})

    def test_a_closing_keyword_still_wins_over_a_notice(self):
        self.gh.pull(30, closes=(12,))
        self.notice(30, sender="coder")
        self.check(30)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "platform"})
        self.assertEqual(self.routed_by(), {"github.check:8001:failure": "closing"})

    def test_a_pr_that_closes_something_is_not_given_a_weak_reference(self):
        self.pr(43, body="Closes #20. Related to #12.")
        self.check(43)
        self.assertEqual(self.routed(), {})

    # -- which events are delivered

    def test_success_checks_and_workflow_runs_are_not_delivered_on_the_new_links(self):
        self.pr(40)
        self.notice(40)
        self.check(40, "success", 8001)
        self.check(40, "success", 8002, event="workflow_run")
        self.check(40, "skipped", 8003)
        self.assertEqual(self.routed(), {})
        self.assertEqual(self.audit("github.unrouted"), [])
        (line,) = self.audit("github.not_delivered")
        self.assertEqual((line["detail"]["routed_by"], line["detail"]["count"]), ("notice", 3))

    def test_a_success_on_a_pr_that_routes_by_a_closing_keyword_is_still_delivered(self):
        self.gh.pull(30, closes=(12,))
        self.check(30, "success")
        self.assertEqual(self.routed(), {"github.check:8001:success": "platform"})

    def test_failed_cancelled_and_timed_out_checks_and_runs_are_delivered(self):
        self.pr(40)
        self.notice(40)
        self.check(40, "failure", 8001)
        self.check(40, "cancelled", 8002)
        self.check(40, "timed_out", 8003, event="workflow_run")
        self.assertEqual(self.routed(), {"github.check:8001:failure": "coder", "github.check:8002:cancelled": "coder",
                                         "github.check:run-8003:timed_out": "coder"})

    def review(self, number, state, rid):
        pull = self.gh.pulls[number]
        self.gh.send("pull_request_review", {"action": "submitted", "pull_request": pull, "review": {
            "id": rid, "state": state, "body": "see the notes", "user": {"login": "rev"}}})

    def test_a_review_that_asks_for_changes_is_delivered_and_an_approval_is_not(self):
        self.pr(40)
        self.notice(40)
        self.review(40, "approved", 6001)
        self.review(40, "changes_requested", 6002)
        self.assertEqual(self.routed(), {"github.review:6002": "coder"})

    def pr_comment(self, number, body, cid, login="alice", user_type="User"):
        issue = dict(self.gh.pulls[number], pull_request={"url": "x"})
        self.gh.send("issue_comment", {"action": "created", "issue": issue, "comment": {
            "id": cid, "body": body, "user": {"login": login, "type": user_type}}})

    def test_a_persons_comment_is_delivered_and_a_bots_and_the_fleets_are_not(self):
        self.pr(40)
        self.notice(40)
        self.pr_comment(40, "please rename this", 5001)
        self.pr_comment(40, "coverage 90%", 5002, login="codecov[bot]", user_type="Bot")
        self.pr_comment(40, "preview ready", 5003, login="deploy-helper", user_type="Bot")
        self.pr_comment(40, "%s\nmy own words" % sb.fleet_mark("coder"), 5004)
        self.assertEqual(self.routed(), {"github.comment:5001": "coder"})

    def test_an_author_in_the_ignore_list_is_not_delivered(self):
        self.start({"github_ignore_authors": ["shared-account"]})
        self.pr(40)
        self.notice(40)
        self.pr_comment(40, "from the agents' own account", 5001, login="shared-account")
        self.assertEqual(self.routed(), {})

    def test_a_bot_comment_on_a_closing_routed_pr_is_delivered_as_before(self):
        self.gh.pull(30, closes=(12,))
        self.pr_comment(30, "coverage 90%", 5002, login="codecov[bot]", user_type="Bot")
        self.assertEqual(self.routed(), {"github.comment:5002": "platform"})

    def test_a_redelivered_event_is_one_item(self):
        self.pr(40)
        self.notice(40)
        for delivery in ("d-1", "d-2"):
            self.check(40)
            self.gh.send("check_suite", {"action": "completed", "check_suite": {
                "id": 8001, "conclusion": "failure", "pull_requests": [{"number": 40}]}}, delivery)
        self.assertEqual(self.routed(), {"github.check:8001:failure": "coder"})
        self.assertEqual(len(self.hub.queue), 1)

    # -- merge conflicts

    def held(self, number, head, mergeable="CONFLICTING", state="open"):
        key = "%s#%d" % (GH_REPO, number)
        self.world.engine.notice_facts[key] = {"at": self.world.clock.now(), "stale": False, "error": None,
                                               "facts": {"state": state, "mergeable": mergeable, "head": head}}

    def test_the_notice_read_asks_for_the_merge_state_and_keeps_it(self):
        self.assertIn("mergeable", sb.PR_VIEW_FIELDS)
        self.assertIn("headRefOid", sb.PR_VIEW_FIELDS)
        facts = sb.pr_facts_from(dict(json.loads(pr_view_json()), mergeable="conflicting", headRefOid="abc123"))
        self.assertEqual((facts["mergeable"], facts["head"]), ("CONFLICTING", "abc123"))

    def test_a_conflict_on_a_pr_with_a_notice_is_routed_once_per_head_commit(self):
        self.pr(40)
        self.notice(40)
        self.held(40, "aaa1111")
        self.assertEqual(self.routed(), {"github.conflict:%s#40:aaa1111" % GH_REPO: "coder"})
        self.assertEqual(self.routed_by(), {"github.conflict:%s#40:aaa1111" % GH_REPO: "notice"})
        self.hub.acknowledge(["github.conflict:%s#40:aaa1111" % GH_REPO])
        self.assertEqual(self.routed(), {}, "read again, delivered, and not queued a second time")
        self.held(40, "bbb2222")
        self.assertEqual(list(self.routed()), ["github.conflict:%s#40:bbb2222" % GH_REPO])

    def test_a_pr_that_merges_cleanly_or_is_unknown_or_closed_is_no_conflict(self):
        self.pr(40)
        self.notice(40)
        for mergeable, state in (("MERGEABLE", "open"), ("UNKNOWN", "open"), ("CONFLICTING", "merged")):
            self.held(40, "aaa1111", mergeable, state)
            self.assertEqual(self.routed(), {})

    def test_a_pr_event_that_carries_the_state_gives_a_conflict_without_a_notice(self):
        self.pr(41, body="for #12")
        self.gh.pulls[41]["mergeable_state"] = "dirty"
        self.gh.send("pull_request", {"action": "opened", "pull_request": self.gh.pulls[41]})
        self.assertIn("github.conflict:%s#41:sha41" % GH_REPO, self.routed())
        self.gh.send("pull_request", {"action": "reopened", "pull_request": self.gh.pulls[41]}, "d-2")
        self.assertEqual([k for k in self.hub.queue if k.startswith("github.conflict")],
                         ["github.conflict:%s#41:sha41" % GH_REPO], "the same head is the same item")
        self.gh.pulls[41]["mergeable_state"] = "clean"
        self.gh.send("pull_request", {"action": "reopened", "pull_request": self.gh.pulls[41]}, "d-3")
        self.assertEqual(len([k for k in self.hub.queue if k.startswith("github.conflict")]), 1)

    # -- the audit

    def test_the_unrouted_audit_line_names_the_prs_and_the_repo(self):
        self.pr(50)
        self.pr(51)
        self.check(50, cid=8001)
        self.check(51, cid=8002)
        self.routed()
        (line,) = self.audit("github.unrouted")
        self.assertEqual((line["subject"], line["detail"]["prs"], line["detail"]["count"]), (GH_REPO, [50, 51], 2))


class TestGithubDelivery(Base):
    """The queue holds an item until the engine has delivered it; nothing else is stored."""

    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self)
        self.gh, self.hub, self.world = self.rig.gh, self.rig.hub, self.rig.world
        self.gh.issue(12, parent=1)
        self.hub.tick()

    def test_a_webhook_is_delivered_once_after_the_batch_window_and_leaves_the_queue(self):
        self.gh.comment(12, "hello", 5001)
        self.rig.pump(60)
        self.assertEqual(self.rig.delivered(), [])
        self.assertEqual(self.rig.keys(), ["github.comment:5001"], "held in memory until delivered")
        self.rig.pump(60)
        self.assertEqual(self.rig.delivered(), ["github.comment:5001"])
        self.assertEqual(self.rig.keys(), [])

    def test_an_item_that_is_held_stays_queued(self):
        self.world.herdr.panes[self.world.panes["platform"]]["status"] = "blocked"
        self.gh.comment(12, "hello", 5001)
        self.rig.pump(200)
        self.assertEqual(self.rig.delivered(), [])
        self.assertEqual(self.rig.keys(), ["github.comment:5001"])

    def test_the_same_object_seen_again_after_delivery_is_not_delivered_again(self):
        self.gh.comment(12, "hello", 5001)
        self.rig.pump(120)
        self.gh.send("issue_comment", {"action": "created", "issue": self.gh.issues[12],
                                       "comment": self.gh.comments[0]}, "d-2")
        self.assertEqual(self.rig.keys(), ["github.comment:5001"], "queued again: the queue stores nothing")
        self.rig.pump(200)
        self.assertEqual(self.rig.delivered(), ["github.comment:5001"])
        self.assertEqual(self.rig.keys(), [], "the transcript already had it, so it was acknowledged")

    def test_the_repeat_is_delivered_again_when_dedupe_is_off(self):
        os.environ[sb.FAULT_ENV] = "no-dedupe"
        self.gh.comment(12, "hello", 5001)
        self.rig.pump(120)
        self.gh.send("issue_comment", {"action": "created", "issue": self.gh.issues[12],
                                       "comment": self.gh.comments[0]}, "d-2")
        self.gh.comment(12, "another", 5002)  # a new key makes a new message id, so v2 admits the repeat
        self.rig.pump(200)
        self.assertEqual(self.rig.delivered(), ["github.comment:5001"] * 2 + ["github.comment:5002"])

    def test_a_restart_loses_the_queue_and_the_catch_up_gets_it_back(self):
        self.gh.comment(12, "hello", 5001)
        self.assertEqual(self.rig.keys(), ["github.comment:5001"])
        stored = [p for p in self.world.state.rglob("*") if p.is_file() and p.name != "audit.jsonl"]
        for path in stored:
            self.assertNotIn("github.comment:5001", path.read_text(errors="replace"), path.name)
        (self.world.state / sb.READ_MARKER_FILE).write_text(json.dumps({GH_REPO: sb.iso(self.world.clock.now() - 600)}))
        restarted = GithubRig(self)  # a new process: nothing in memory but the marker it left
        self.assertEqual(restarted.keys(), [])
        restarted.gh.comments, restarted.gh.issues = self.gh.comments, self.gh.issues
        restarted.world.clock.t = self.world.clock.now()
        restarted.hub.tick()
        restarted.pump(15)
        self.assertEqual(restarted.keys(), ["github.comment:5001"], "the catch-up read brings it back")

    def test_a_sent_key_is_acknowledged_even_when_only_the_delivery_path_saw_it(self):
        self.gh.comment(12, "hello", 5001)
        self.rig.pump(120)
        self.assertEqual(self.rig.keys(), [])
        source = inspect.getsource(sb.Switchboard.deliver)
        self.assertIn("self._acknowledge(keys)", source)

    def test_facts_the_source_has_are_the_engines_and_status_reads_them_without_acknowledging(self):
        self.gh.comment(12, "hello", 5001)
        engine = self.hub.attach(self.world.new_engine(writable=False))
        facts, problems = engine.collect_facts()
        self.assertEqual(([f.key for f in facts], problems), (["github.comment:5001"], []))
        self.assertEqual(self.rig.keys(), ["github.comment:5001"])


class TestGithubCatchUp(Base):
    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self, config={"sweep": False, "triage": False})  # this class counts every read: the sweep's are not its subject
        self.gh, self.hub, self.world = self.rig.gh, self.rig.hub, self.rig.world
        self.world.clock.t += 7200
        self.gh.issue(12, parent=1)

    def test_one_read_follows_each_start_and_nothing_polls(self):
        self.hub.tick()
        self.assertEqual(self.gh.api_calls, [], "not before the forwarder has had time to connect")
        self.rig.pump(10)
        first = len(self.gh.api_calls)
        self.assertGreater(first, 0)
        self.rig.pump(3000)
        self.assertEqual(len(self.gh.api_calls), first, "a quiet, healthy forwarder is never polled")

    def test_a_restarted_forwarder_is_followed_by_another_read_that_recovers_the_gap(self):
        self.rig.pump(15)
        self.gh.kill_forwarder()
        self.gh.comment(12, "while down", 5001)
        self.assertEqual(self.rig.keys(), [], "the event was lost: nobody was forwarding")
        self.rig.pump(5)  # exit seen, backoff
        self.rig.pump(20)  # restart, then the read
        self.assertEqual(self.rig.keys() or self.rig.delivered(), ["github.comment:5001"])

    def test_a_webhook_and_a_catch_up_of_one_object_are_one_delivery(self):
        self.rig.pump(15)
        self.gh.comment(12, "both ways", 5001)  # webhook now
        self.gh.kill_forwarder()
        self.rig.pump(30)  # restart + catch-up reads the same comment
        self.rig.pump(200)
        self.assertEqual(self.rig.delivered(), ["github.comment:5001"])

    def test_a_failed_read_is_retried_and_reported(self):
        self.gh.api_down = True
        self.rig.pump(15)
        self.assertIn("HTTP 502", self.rig.watch().catch_up_error)
        self.gh.comment(12, "later", 5001, forward=False)
        self.gh.api_down = False
        self.rig.pump(sb.GITHUB_LOOKUP_RETRY + 10)
        self.assertIsNone(self.rig.watch().catch_up_error)
        self.assertEqual(self.rig.keys() or self.rig.delivered(), ["github.comment:5001"])

    def test_one_failing_read_does_not_stop_the_others(self):
        self.gh.comment(12, "x", 5001, forward=False)
        original = self.gh.answer

        def answer(path, query):
            if path == "pulls":
                raise AssertionError("never mind")
            return original(path, query)

        self.gh.answer = lambda path, query: (_ for _ in ()).throw(sb.GitHubUnavailable("pulls down")) \
            if path == "pulls" else original(path, query)
        self.gh.runner_original = self.gh.runner
        self.hub.runner = lambda argv, **kw: (subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 500")
                                              if argv[-1].split("?")[0].endswith("/pulls") else self.gh.runner(argv))
        items, errors = self.hub.catch_up_items(GH_REPO, self.world.clock.now())
        self.assertEqual([i.key for i in items], ["github.comment:5001"])
        self.assertEqual(len(errors), 1)
        self.assertIn("pulls", errors[0])

    def test_paginated_output_is_read_page_by_page(self):
        self.assertEqual(sb.decode_json_stream('[{"a": 1}]\n[{"a": 2}]\n{"b": 3}'), [[{"a": 1}], [{"a": 2}], {"b": 3}])
        self.assertEqual(sb.decode_json_stream(""), [])
        with self.assertRaises(ValueError):
            sb.decode_json_stream("[{")
        self.gh.comment(12, "a", 5001, forward=False)
        self.gh.comment(12, "b", 5002, forward=False)
        items, errors = self.hub.catch_up_items(GH_REPO, self.world.clock.now())
        self.assertEqual(sorted(i.key for i in items), ["github.comment:5001", "github.comment:5002"])

    def test_gh_that_cannot_run_or_answers_nonsense_is_an_unavailable_read(self):
        for runner in (lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("no gh")),
                       lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "<html>", ""),
                       lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 5))):
            self.hub.runner = runner
            with self.assertRaises(sb.GitHubUnavailable):
                self.hub.gh_api("repos/x/y/issues")


class TestGithubStatus(Base):
    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self)
        self.hub, self.gh, self.world = self.rig.hub, self.rig.gh, self.rig.world

    def status(self):
        return sb.github_status(self.world.config, self.world.state)

    def test_no_watched_repo_adds_no_lines(self):
        self.assertEqual(sb.format_github(sb.github_status(sb.validate_config({}), self.world.state)), [])

    def test_without_a_daemon_it_says_the_state_is_unknown(self):
        lines = sb.format_github(self.status())
        self.assertIn("older events are lost", lines[0])
        self.assertIn("daemon not running", lines[1])

    def test_a_snapshot_of_a_live_daemon_shows_each_watch(self):
        self.hub.tick()
        report = self.status()
        self.assertTrue(report["daemon"])
        self.assertEqual([(w["repo"], w["state"]) for w in report["watches"]], [(GH_REPO, "running")])
        self.assertIn("%s" % GH_REPO, sb.format_github(report)[1])

    def test_a_conflict_is_reported_as_one(self):
        self.gh.occupied = True
        self.hub.tick()
        self.world.clock.t += 5
        self.hub.tick()
        text = "\n".join(sb.format_github(self.status()))
        self.assertIn("conflict", text)
        self.assertIn("catch-up reads only", text)

    def test_repeated_failures_show(self):
        self.gh.spawn_error = "nope"
        for _ in range(3):
            self.world.clock.t += 400
            self.hub.tick()
        self.assertIn("3 failure(s)", "\n".join(sb.format_github(self.status())))

    def test_a_live_forwarder_shows_up_age_and_a_recovered_drop_as_history(self):
        self.hub.tick()
        w = self.hub.watches[GH_REPO]
        self.world.clock.t += 100
        self.gh.kill_forwarder()
        self.hub.tick()
        self.world.clock.t += 10
        self.hub.tick()
        text = "\n".join(sb.format_github(self.status()))
        self.assertIn("live, up", text)
        self.assertIn("1 in the last hour", text)
        self.assertNotIn("ALARM", text)
        self.assertNotIn("failure(s)", text)
        self.assertNotIn("exited with code", text)

    def test_health_alarms_on_long_down_and_on_flapping(self):
        now = 10000.0
        down = {"mode": "backoff", "down_since": now - 301, "drops": [[now - 301, 100]]}
        self.assertIn("down for", sb.forwarder_health(down, now)["alarm"])
        brief = {"mode": "backoff", "down_since": now - 100, "drops": [[now - 100, 100]]}
        self.assertIsNone(sb.forwarder_health(brief, now)["alarm"])
        flap = {"mode": "forwarder", "started": sb.iso(now - 5), "drops": [[now - 60 * i, 200] for i in range(1, 8)]}
        self.assertIn("flapping", sb.forwarder_health(flap, now)["alarm"])
        short = {"mode": "forwarder", "started": sb.iso(now - 5), "drops": [[now - 60 * i, 20] for i in range(1, 4)]}
        self.assertIn("under 60s", sb.forwarder_health(short, now)["alarm"])
        calm = {"mode": "forwarder", "started": sb.iso(now - 5), "drops": [[now - 3000, 5]]}
        self.assertIsNone(sb.forwarder_health(calm, now)["alarm"])
        self.assertIsNone(sb.forwarder_health(dict(down, mode="polling"), now)["alarm"])

    def test_conflict_is_a_quiet_state_not_an_alarm(self):
        now = 10000.0
        conflict = {"mode": "conflict", "down_since": now - 4000, "drops": [[now - 60 * i, 5] for i in range(1, 9)]}
        health = sb.forwarder_health(conflict, now)
        self.assertIsNone(health["alarm"])
        self.assertEqual(health["quiet"], "conflict")
        self.gh.occupied = True
        self.hub.tick()
        self.world.clock.t += 5000
        self.hub.tick()
        report = sb.github_status(self.world.config, self.world.state, None, self.world.clock.now())
        text = "\n".join(sb.format_github(report))
        self.assertIn("conflict", text)
        self.assertNotIn("ALARM", text)
        self.assertNotIn(", down ", text)

    def test_a_spawn_failure_sets_down_since_and_alarms(self):
        self.gh.spawn_error = "nope"
        self.hub.tick()
        self.assertIsNotNone(self.hub.watches[GH_REPO].down_since)
        self.world.clock.t += 400
        self.hub.tick()
        report = sb.github_status(self.world.config, self.world.state, None, self.world.clock.now())
        text = "\n".join(sb.format_github(report))
        self.assertIn("ALARM: forwarder down for", text)

    def test_a_snapshot_of_a_dead_pid_is_not_believed(self):
        self.hub.tick()
        path = self.world.state / sb.WATCH_FILE
        data = json.loads(path.read_text())
        data["pid"] = 2 ** 22 + 1234567
        path.write_text(json.dumps(data))
        self.assertFalse(self.status()["daemon"])

    def test_stop_marks_the_snapshot_as_no_daemon(self):
        self.hub.tick()
        self.hub.stop()
        self.assertFalse(self.status()["daemon"])

    def test_a_snapshot_for_other_repos_is_not_shown(self):
        self.hub.tick()
        other = sb.validate_config({"github_repos": ["x/y"]})
        self.assertEqual(sb.github_status(other, self.world.state)["watches"], [])

    def test_the_status_command_includes_github(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "github_repos": [GH_REPO]})
        with captured() as (out, _):
            code = sb.main(["status", "--json"], runner=self.world.route, now=self.world.clock.now)
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["github"]["repos"]), (0, [GH_REPO]))
        with captured() as (out, _):
            sb.main(["status"], runner=self.world.route, now=self.world.clock.now)
        self.assertIn("github", out.getvalue())


class TestGithubDaemon(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})

    def test_no_repo_means_a_hub_with_no_watches_that_opens_and_starts_nothing(self):
        # PR 15: the watched set is derived on every pass, so the hub always exists; with nothing to watch it idles
        engine, state, config = sb.build_engine(self.world.route, self.world.clock.now, "daemon")
        with mock.patch.object(sb.signal, "signal"):
            hub = sb.attach_github(engine, config, state, self.world.route, self.world.clock.now)
        self.assertIsNotNone(hub)
        self.assertEqual(hub.watches, {})
        with mock.patch.object(sb.GitHubHub, "open_listener") as opened:
            engine.run_pass()
        opened.assert_not_called()

    def test_with_repos_the_hub_is_a_fact_source_and_the_daemon_starts_it_only_inside_its_lock(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "github_repos": [GH_REPO]})
        started = []
        with mock.patch.object(sb, "spawn_forwarder", side_effect=lambda *a: started.append(a)), \
                mock.patch.object(sb.signal, "signal"), mock.patch.object(sb.GitHubHub, "open_listener", return_value=1):
            with sb.single_instance(self.world.state):
                with captured():
                    code = sb.main(["run"], runner=self.world.route, now=self.world.clock.now)
            self.assertEqual((code, started), (1, []), "the lock is refused before anything starts")

    def test_the_daemon_runs_the_hub_alongside_the_passes_and_stops_it_at_the_end(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "github_repos": [GH_REPO]})
        gh = FakeGitHub(self.world.clock)
        sleeps = []

        def sleep(sock, seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt

        runner = lambda argv, **kw: self.world.route(argv, **kw) if argv[0] != "gh" else gh.runner(argv)
        with mock.patch.object(sb, "spawn_forwarder", side_effect=gh.spawn), \
                mock.patch.object(sb.signal, "signal") as handler, \
                mock.patch.object(sb.GitHubHub, "open_listener", return_value=4711), \
                mock.patch.object(sb.GitHubHub, "_supervise"), mock.patch.object(sb.GitHubHub, "_thread"):
            code = sb.main(["run"], runner=runner, now=self.world.clock.now, wait=sleep)
        self.assertEqual((code, sleeps), (0, [5, 5]))
        self.assertEqual(handler.call_args[0][0], sb.signal.SIGTERM)
        self.assertTrue((self.world.state / sb.WATCH_FILE).exists())
        self.assertIsNone(json.loads((self.world.state / sb.WATCH_FILE).read_text())["pid"], "stopped")

    def test_run_once_never_starts_a_forwarder(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "github_repos": [GH_REPO]})
        with mock.patch.object(sb, "spawn_forwarder") as spawn:
            code = sb.main(["run", "--once"], runner=self.world.route, now=self.world.clock.now)
        self.assertEqual(code, 0)
        spawn.assert_not_called()

    def test_the_engine_never_types_into_a_pane_for_github_either(self):
        self.assertEqual(herdr_typing_violations(switchboard_source()), [])


class FrictionBase(Base):
    def setUp(self):
        super().setUp()
        old = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)
        self.make()

    def make(self, section=None, **kwargs):
        config = {"friction_wake": dict({"enabled": True}, **(section or {}))} if section != "off" else {}
        self.world = World(self, agents=(), config=config, **kwargs)
        for name, role in (("platform", "orchestrator"), ("web", "orchestrator"), ("coder", "coder"),
                           ("cos", "chief-of-staff")):
            self.world.add_agent(name, fleet={"role": role})

    def pass_at(self, seconds):
        """A pass after `seconds`, and the next one (a wake is made at the end of one pass and delivered by the
        next, the way every reminder is). Returns what the two sent."""
        self.world.tick(seconds)
        first = self.world.engine.run_pass()
        return {"sent": first["sent"] + self.world.engine.run_pass()["sent"]}

    def friction(self, name):
        return [a for a in self.world.admissions(name) if "Daily friction check" in a["text"]]


class TestFrictionWake(FrictionBase):
    def test_it_is_off_by_default_and_the_defaults_are_the_documented_ones(self):
        self.assertNotIn("friction_wake", sb.DEFAULTS)
        self.assertEqual([s.__name__ for s in sb.fact_sources(self.home)],
                         ["lab_facts", "worker_facts", "reminder_facts", "report_rider_facts", "foreground_facts",
                          "orders_facts", "sweep_facts", "triage_facts", "plan_wake_facts", "alert_wake_facts", "red_ci_facts", "job_facts", "pane_facts", "pane_exit_facts", "headless_restart_facts", "disk_facts", "cpu_facts", "spotlight_facts", "docker_reap_facts", "temp_janitor_facts", "heavy_orphan_facts"])
        self.assertNotIn("friction_wake", sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}))
        self.assertEqual(sb.FRICTION_DEFAULTS, {"enabled": False, "hour": 9, "to": "cos", "cos_to": "improver", "text": None})
        self.make("off")
        self.pass_at(3600)
        self.assertEqual([self.friction(n) for n in ("platform", "web")], [[], []])
        self.make({})  # a section that is present and says nothing: still enabled only by `enabled`
        raw = sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "friction_wake": {}})
        self.assertFalse(raw["friction_wake"]["enabled"])

    def test_the_default_text_is_the_fixed_one(self):
        self.pass_at(3600)
        (found,) = self.friction("platform")
        self.assertIn("Daily friction check (one short answer): what slowed you or your coders down in the last day? "
                      "If anything is worth fixing, send cos a one-paragraph proposal: fleet-switchboard send "
                      "cos --issue <n> \"<proposal>\". If nothing, do nothing and say nothing.", found["text"])

    def test_one_wake_per_orchestrator_per_day_not_for_cos_or_coders(self):
        self.assertEqual(self.pass_at(0)["sent"], [])  # 08:00, before the hour
        self.pass_at(3600)  # 09:00
        self.assertEqual((len(self.friction("platform")), len(self.friction("web"))), (1, 1))
        self.assertEqual(self.friction("coder"), [])
        for _ in range(5):
            self.assertEqual(self.pass_at(600)["sent"], [], "nothing is sent again, queued or not")
        self.assertEqual((len(self.friction("platform")), len(self.friction("web"))), (1, 1), "not twice in a day")
        self.assertEqual(len([e for e in self.world.audit() if e["event"] == "friction.wake"]), 3)  # two orchestrators and cos
        self.pass_at(86400)
        self.assertEqual(len(self.friction("platform")), 2, "and again the next day")

    def test_a_restart_does_not_repeat_it_with_or_without_the_state_file(self):
        self.pass_at(3600)
        self.world.engine = self.world.new_engine()
        self.pass_at(5)
        self.assertEqual(len(self.friction("platform")), 1)
        (self.world.state / sb.FRICTION_STATE).unlink()
        self.world.engine = self.world.new_engine()
        self.pass_at(5)
        self.assertLessEqual(len(self.friction("platform")), 2, "a lost file is at most one extra wake")

    def test_an_engaged_orchestrator_is_skipped_until_it_is_not(self):
        self.world.tick(3590)
        self.world.you("platform")
        self.pass_at(15)
        self.assertEqual(self.friction("platform"), [])
        self.assertEqual(len(self.friction("web")), 1)
        self.pass_at(700)  # the engagement window has passed
        self.assertEqual(len(self.friction("platform")), 1)

    def test_a_blocked_orchestrator_gets_none(self):
        self.world.herdr.panes[self.world.panes["platform"]]["status"] = "blocked"
        self.pass_at(3600)
        self.assertEqual(self.friction("platform"), [])

    def test_with_no_hour_it_is_every_24_hours_from_the_first(self):
        self.make({"hour": None})
        self.pass_at(10)
        self.assertEqual(len(self.friction("platform")), 1)
        self.pass_at(80000)
        self.assertEqual(len(self.friction("platform")), 1)
        self.pass_at(6400)
        self.assertEqual(len(self.friction("platform")), 2)

    def test_the_recipient_and_the_text_are_config(self):
        self.make({"to": "improver", "text": "What slowed you down?"})
        self.pass_at(3600)
        self.assertEqual(len([a for a in self.world.admissions("platform") if "What slowed you down?" in a["text"]]), 1)
        (self.world.state / sb.FRICTION_STATE).unlink()
        self.make({"to": "improver"})
        self.pass_at(3600)
        self.assertIn("fleet-switchboard send improver --issue <n>", self.friction("platform")[0]["text"])

    def test_cos_is_woken_in_the_same_slot_with_its_own_recipient_and_state_key(self):
        self.pass_at(3600)
        (found,) = self.friction("cos")
        self.assertIn("fleet-switchboard send improver --issue <n>", found["text"])
        self.assertIn("send improver a one-paragraph proposal", found["text"])
        self.assertIn("cos:cos", sb.read_friction_state(self.world.state))
        for _ in range(3):
            self.pass_at(600)
        self.assertEqual(len(self.friction("cos")), 1, "one wake per slot")
        self.pass_at(86400)
        self.assertEqual(len(self.friction("cos")), 2)

    def test_cos_to_is_config_and_a_custom_text_does_not_replace_cos_text(self):
        self.make({"cos_to": "tuner", "text": "What slowed you down?"})
        self.pass_at(3600)
        self.assertIn("send tuner", self.friction("cos")[0]["text"])
        self.assertEqual(len(self.friction("platform")), 0)

    def test_the_agent_named_by_cos_to_is_never_woken_about_itself(self):
        self.make({"cos_to": "cos"})
        self.pass_at(3600)
        self.assertEqual(self.friction("cos"), [])
        self.assertEqual(len(self.friction("platform")), 1)

    def test_an_engaged_or_blocked_cos_is_skipped(self):
        self.world.herdr.panes[self.world.panes["cos"]]["status"] = "blocked"
        self.pass_at(3600)
        self.assertEqual(self.friction("cos"), [])
        self.assertEqual(len(self.friction("platform")), 1)

    def test_status_shows_the_friction_line(self):
        cfg = sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "friction_wake": {"enabled": True}})
        self.assertEqual(sb.friction_status_line(cfg, self.world.state),
                         "friction wake: on, 09:00 to cos; cos to improver; last slot none yet")
        self.pass_at(3600)
        self.assertNotIn("none yet", sb.friction_status_line(cfg, self.world.state))
        off = sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.assertEqual(sb.friction_status_line(off, self.world.state), "friction wake: off")
        text = switchboard_source()   # and `status` prints it, indented under the others
        self.assertIn('report["friction"] = friction_status_line(config, state)', text)
        self.assertIn('lines.extend("      " + report["friction"]', text)

    def test_nobody_due_means_no_agent_is_read(self):
        self.pass_at(3600)
        before = len(self.world.v2.calls)
        self.pass_at(5)
        self.assertEqual([c for c in self.world.v2.calls[before:] if "prompt" in c or "admit" in c], [])

    def test_the_config_is_validated_like_the_others(self):
        base = {"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}
        for bad, word in (({"enabled": "yes"}, "enabled"), ({"hour": 24}, "hour"), ({"hour": "9"}, "hour"),
                          ({"hour": True}, "hour"), ({"to": "Bad Name"}, "'friction_wake.to'"), ({"cos_to": "Bad Name"}, "'friction_wake.cos_to'"), ({"cos_to": 5}, "cos_to"),
                          ({"text": ""}, "text"), ({"text": 5}, "text")):
            with self.subTest(bad=bad), self.assertRaises(sb.ConfigError) as caught:
                sb.validate_config(dict(base, friction_wake=bad))
            self.assertIn(word, str(caught.exception))
        with self.assertRaises(sb.ConfigError):
            sb.validate_config(dict(base, friction_wake=[]))


class TestChiefOfStaffNativePermissions(unittest.TestCase):
    """The Chief of Staff's (and its subagent's) v2 `permissions:` list is SHORT, hand-written and deny-by-default (#191): a focus, not a fence.
    Check its shape, that it stays short, and evaluate it with OpenCode's documented semantics (whole-value glob, any matching deny denies,
    else an allow, else the base deny)."""
    PATHS = [REPO / "agents" / "opencode" / "chief-of-staff.md", REPO / "agents" / "opencode" / "cos-subagent.md"]
    PATH = PATHS[0]
    TEMP = "/private/var/folders/ab/T/opencode"
    MAX_BLOCK_LINES = 210

    @staticmethod
    def block(path):
        """(front matter text, the lines of the `permissions:` block, its rules as dicts). One rule per line: `- {action: shell, resource: "x", effect: allow}`."""
        front = path.read_text().split("\n---\n", 1)[0]
        lines = front.splitlines()
        start = lines.index("permissions:")
        rules = []
        for line in lines[start + 1:]:
            if line.lstrip().startswith("#"):
                continue
            m = re.fullmatch(r"  - \{action: (.+?), resource: (\".*\"), effect: (allow|deny)\}", line)
            assert m, "not a one-line rule: %r" % line
            rules.append({"action": json.loads(m.group(1)) if m.group(1).startswith('"') else m.group(1), "resource": json.loads(m.group(2)), "effect": m.group(3)})
        return front, lines[start:], rules

    @classmethod
    def parse(cls, path):
        front, _, rules = cls.block(path)
        return front, rules

    @classmethod
    def rules(cls):
        return cls.parse(cls.PATH)

    @staticmethod
    def evaluate(rules, action, resource):
        def match(pattern):
            rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
            if pattern.endswith(" *"):  # OpenCode: a trailing ` *` also matches the bare command
                rx = rx[:-len(re.escape(" ") + ".*")] + "(?: .*)?"
            return re.fullmatch(rx, resource, re.S) is not None
        verdict = "deny"
        for r in rules:   # v2: the LAST matching rule decides (proved on the real harness); the list keeps every deny after every allow
            if r["action"] in ("*", action) and match(r["resource"]):
                verdict = r["effect"]
        return verdict

    def test_the_block_is_short_enough_for_a_person_to_read_and_cannot_regrow(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, lines, _ = self.block(path)
                self.assertLess(len(lines), self.MAX_BLOCK_LINES, "the permission block of %s is %d lines: keep it a short list, no enumerated variants" % (path.name, len(lines)))
                self.assertLess(len(path.read_text().split("\n---\n", 1)[0].splitlines()), self.MAX_BLOCK_LINES + 20)
        # the generator that once enumerated every variant (30,000 lines) is gone and must not come back
        self.assertFalse((REPO / "tools" / "cos-permissions-proof" / "gen_permissions.py").exists())

    def test_every_deny_comes_after_every_allow_because_the_last_match_wins(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                effects = [r["effect"] for r in rules[1:]]
                last_allow = max(i for i, e in enumerate(effects) if e == "allow")
                self.assertNotIn("allow", effects[effects.index("deny"):] if "deny" in effects else [])
                self.assertEqual(effects[last_allow + 1:], ["deny"] * (len(effects) - last_allow - 1))
                resources = [r["resource"] for r in rules]
                for guard in ("fleet-switchboard gates *enforce*", "fleet-switchboard gates on*", "fleet-switchboard waive *G6*"):
                    # the timeout form is its own line, or one `*`-led line covers the bare and the timeout forms (the Chief of Staff's)
                    self.assertTrue("timeout * " + guard in resources or "*" + guard in resources, guard)

    def test_the_first_rule_denies_everything_and_nothing_ever_asks(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
                self.assertEqual({r["effect"] for r in rules}, {"allow", "deny"})

    def test_v1_keys_that_would_void_or_widen_the_rules_are_absent(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                front, _ = self.parse(path)
                self.assertNotRegex(front, r"(?m)^name:")  # a named agent makes v2 ignore `permissions:` entirely
                self.assertNotRegex(front, r"(?m)^permission:")
                self.assertNotRegex(front, r"bash:")

    def test_no_shell_allow_is_a_bare_wildcard(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                for r in rules:
                    if r["action"] == "shell" and r["effect"] == "allow":
                        self.assertNotIn(r["resource"], ("*", "* *"))

    def test_edit_is_only_the_temp_dir_and_subagents_are_denied(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                self.assertEqual([(r["resource"], r["effect"]) for r in rules if r["action"] == "edit"], [("/private/var/folders/*/T/opencode/*", "allow")])
                for action in ("subagent", "task"):
                    self.assertEqual([r["effect"] for r in rules if r["action"] == action], ["deny"])
                self.assertEqual(self.evaluate(rules, "edit", "/home/a/Code/x/f.py"), "deny")
                self.assertEqual(self.evaluate(rules, "edit", self.TEMP + "/note.md"), "allow")

    def test_the_job_runs_and_what_is_outside_it_is_denied(self):
        _, rules = self.rules()
        for command in ("fleet-switchboard decisions list --tier all", "sleep 20",
                        "gh issue view 5 --json body -q .body", "gh issue create --title x --body y", "fleet-switchboard disk", "fleet-switchboard disk janitor --apply", "fleet-switchboard disk reap-docker --apply", "fleet-switchboard disk prune", "gh api repos/o/r/issues/comments/1 --jq .body", "grep foo", "cut -c1-100", "jq length",
                        "gh pr merge 5 --squash", "gh issue close 5", "gh pr comment 5 --body ok", "az webapp show -n a -g g", "az group list",
                        "fleet-switchboard handoff --issue 5 --brief-file -", "fleet-switchboard launch v --agent orchestrator",
                        "fleet-switchboard reattach coder-a --issue 5", "fleet-switchboard relaunch coder-a --issue 5",
                        "timeout 30 gh pr view 5", "timeout 30 gh issue list --repo acme/api", "herdr pane read p1 --lines 50", "herdr api snapshot", "herdr tab rename t1 x", "fleet-switchboard decisions 2>&1", "ls 2>&1",
                        "gh api repos/o/r/issues/1 --jq '.title + \" \" + (.number|tostring)'", "git -C /home/a/Code/widget status", "git remote -v",
                        "git worktree list", "git --no-pager log -3", "git branch -vv", "git merge-base main HEAD", "git ls-remote origin",
                        "fleet-switchboard gates mode acme/app shadow", "fleet-switchboard gates off", "fleet-switchboard gates preflight acme/app",
                        "fleet-switchboard waive acme/app#5 --gates G1 --reason hotfix", "cat " + self.TEMP + "/x", "cat > " + self.TEMP + "/x <<EOF\nhi\nEOF",
                        'fleet-switchboard send x --issue y "see ~/.config/fleet-switchboard/config.json and /tmp/foo --admin 2>&1 > f; z"',
                        'fleet-switchboard decisions escalate x#1 --reason "saw 2>&1 and > f; z"', 'fleet-switchboard orders add --charter o/r#1 "x 2>&1 y"',
                        'fleet-switchboard report done --issue 5 "$(cat %s/x)"' % self.TEMP):
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        for command in ("make build", "git push", "git -C /home/a/Code/widget push", "git commit -m x", "git fetch", "git pull", "git -c core.pager=sh log",
                        "git status; git push", "rm -rf x", "az keyvault secret show --name x", "az account get-access-token",
                        "az vm delete -n x", "cat /home/a/.ssh/id_rsa", "gh pr merge 5 --admin", "gh pr merge 5 --auto",
                        "gh pr review 5 --approve", "gh repo delete o/r", "curl https://example.com", "timeout 5 rm -rf x", "fleet-switchboard decisions batch --title x",
                        "gh api repos/o/r/issues -fa=b", "gh api repos/o/r/issues --raw-field a=b", "gh api repos/o/r/issues -Fa=@f", "git credential fill", "gh auth status -t", "gh auth status --show-token", "az acr credential show -n r", "az webapp config appsettings list -n a -g g", "fleet-switchboard gates mode acme/app enforce", "fleet-switchboard gates mode acme/app ENFORCE", "fleet-switchboard gates on",
                        "timeout 30 fleet-switchboard gates mode acme/app enforce", "timeout 5 fleet-switchboard gates on", "timeout 5 az keyvault secret show --name x", "timeout 30 az acr credential show -n r", "timeout 30 az webapp config appsettings list -n a -g g", "timeout 30 az storage account keys list -n s", "timeout 5 gh api repos/o/r/issues -X POST", "timeout 5 git ls-remote ext::sh",
                        "fleet-switchboard waive acme/app#5 --gates G6 --reason x", "timeout 5 fleet-switchboard waive acme/app#5 --gates G6 --reason x",
                        "timeout 90 fleet-switchboard decisions list --tier all", "herdr agent prompt a1 hi", "herdr pane send p1 ls", "herdr pane keys p1 Enter", "herdr pane close p1"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)

    AZ_READS = ("az vm list-usage --location eastus", "az containerapp revision list -n a -g g", "az containerapp show -n a -g g", "az containerapp env show -n e -g g",
                "az vm list -g g", "az vmss list -g g", "az group list", "az webapp show -n a -g g", "az account show", "az containerapp list -g g")
    AZ_SECRETS = ("az keyvault secret show --name x", "az keyvault secret list --vault-name v", "az account get-access-token", "az ad app credential list --id x",
                  "az ad sp credential reset --id x", "az storage account keys list -n s", "az cosmosdb keys list -n c -g g", "az cosmosdb list-keys -n c -g g",
                  "az webapp config connection-string list -n a -g g", "az webapp config appsettings list -n a -g g", "az containerapp secret list -n a -g g",
                  "az containerapp secret show -n a -g g --secret-name s", "az functionapp keys list -n f -g g", "az sql db show-connection-string -n d",
                  "az containerapp exec -n a -g g", "az containerapp up -n a", "az containerapp delete -n a -g g", "az containerapp update -n a -g g", "az vm start -n v -g g",
                  "az rest --method get --url https://x", "az acr credential show -n r")

    AZ_MORE_SECRETS = ("az signalr key list -n s -g g", "az eventgrid topic key list -n t -g g", "az search admin-key show --service-name s -g g",
                       "az search query-key list --service-name s -g g", "az webapp deployment list-publishing-profiles -n a -g g",
                       "az functionapp deployment list-publishing-credentials -n f -g g", "az keyvault list", "az vm delete -n list -g g")
    TIMEOUT_WRITES = ("timeout 5 gh secret set X --body view", "timeout 5 gh repo delete o/r view", "timeout 5 gh api repos/o/r/issues -X POST -f t=view",
                      "timeout 5 gh workflow run ci.yml --ref view", "timeout 5 gh pr merge 5 --body view")

    def test_az_reads_are_allowed_generally_and_every_secret_reading_or_writing_verb_stays_denied(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                for command in self.AZ_READS + ("gh issue create --title x --body y", "fleet-switchboard disk", "fleet-switchboard disk janitor --apply",
                                                "timeout 30 gh pr view 5", "timeout 30 gh issue list --repo acme/api"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
                for command in self.TIMEOUT_WRITES + ("fleet-switchboard disk prune --docker /bin/sh", "fleet-switchboard disk prune --docker=/bin/sh", "fleet-switchboard disk --docker /tmp/x report",
                                                      "fleet-switchboard disk reap-docker --apply --docker /tmp/x/docker", "fleet-switchboard disk report --docker=/bin/sh"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
                for command in ("fleet-switchboard disk prune", "fleet-switchboard disk prune --dry-run", "fleet-switchboard disk report"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
                for command in self.AZ_SECRETS + self.AZ_MORE_SECRETS:
                    self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
                    self.assertEqual(self.evaluate(rules, "shell", "timeout 30 " + command), "deny", "timeout 30 " + command)

    READ_ONLY_ADDED = ("fleet-switchboard review measure --since 7", "fleet-switchboard review laws acme/api#5", "fleet-switchboard impact floor acme/api#5 --json",
                       "fleet-switchboard impact hold --dry-run", "fleet-switchboard pr-explain acme/api#5 --check", "fleet-switchboard agents verify")

    def test_the_review_and_impact_reads_are_allowed_and_each_is_read_only_and_the_writes_next_to_them_are_not(self):
        """The cos list gains six read-only lines: review measure, review laws, impact floor, impact hold --dry-run, pr-explain and agents verify
        (`gates *`, `plan *` and `status *` were already there). Each prints and writes nothing. The write subcommands of the same words stay denied."""
        _, rules = self.rules()
        for command in self.READ_ONLY_ADDED:
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        # no blanket `timeout ? fleet-switchboard *` any more: a write behind a timeout prefix is denied, like the bare one (the Chief of Staff needs no prefix)
        for command in ("timeout 30 fleet-switchboard review record acme/api#5 --reviewer code --block f", "timeout 30 fleet-switchboard impact clear acme/api#5 --reason x",
                        "timeout 30 fleet-switchboard verdict --all --pr acme/api#5", "timeout 5 fleet-switchboard send x y", "timeout 30 fleet-switchboard review laws acme/api#5"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        for command in ("fleet-switchboard verdict --all --pr acme/api#5", "fleet-switchboard review record acme/api#5 --reviewer code --block f"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard review record acme/api#5 --reviewer code --block f"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard impact clear acme/api#5 --reason x"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard impact hold"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard agents install"), "deny")
        for gate in ("fleet-switchboard gates mode acme/app enforce", "timeout 30 fleet-switchboard gates mode acme/app ENFORCE", "fleet-switchboard gates on",
                     "timeout 5 fleet-switchboard waive acme/app#5 --gates G8 --reason x", "timeout 5 fleet-switchboard decisions batch --title x"):
            self.assertEqual(self.evaluate(rules, "shell", gate), "deny", gate)   # the collapsed denies still catch both the bare and the timeout forms

    def test_retire_is_allowed_for_the_chief_of_staff_and_every_write_next_to_it_stays_denied(self):
        _, rules = self.rules()
        for command in ("fleet-switchboard retire worker --reason done", "fleet-switchboard retire --undo worker", "fleet-switchboard retire worker"):
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        for command in ("timeout 30 fleet-switchboard retire worker", "fleet-switchboard review record acme/api#5 --reviewer code --block f",
                        "fleet-switchboard impact clear acme/api#5 --reason x", "fleet-switchboard verdict --all --pr acme/api#5",
                        "herdr pane close w1:p1", "herdr tab close w1:t1", "fleet-switchboard gates mode acme/app enforce"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)        # no raw herdr, and the other writes are as before
        for path in self.PATHS:
            _, lines, _ = self.block(path)
            self.assertLess(len(lines), self.MAX_BLOCK_LINES)

    def test_the_subagent_has_the_same_list_without_handoff_or_launch(self):
        _, rules = self.parse(self.PATHS[1])
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard send x y"), "allow")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard handoff --issue 5"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard launch v --agent orchestrator"), "deny")
        for command in ("fleet-switchboard reattach x", "fleet-switchboard relaunch x", "fleet-switchboard retire x"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        self.assertEqual(self.evaluate(rules, "shell", "make build"), "deny")

    def test_the_prose_matches_the_rules(self):
        text = self.PATH.read_text()
        self.assertIn("I don't have a problem with you using gh to write comments or close/merge PRs", text)
        self.assertIn("route the work to the owning orchestrator", text)
        self.assertIn("keeps you **focused on your job**", text)
        self.assertNotIn("never merge,", text)
        self.assertNotIn("ASKS your principal", text)  # one story: a denial is hard and nothing asks


class TestCosTriageAllowRule(unittest.TestCase):
    """The Chief of Staff definitions allow `fleet-switchboard triage *` with one rule each, and no other switchboard allow rule is added."""
    FILES = ("agents/opencode/chief-of-staff.md", "agents/opencode/cos-subagent.md")
    RULE = '  - {action: shell, resource: "fleet-switchboard triage *", effect: allow}'

    def _lines(self, rel):
        return (REPO / rel).read_text().splitlines()

    def test_each_file_carries_exactly_one_triage_allow_rule_and_no_deny_or_wider_rule(self):
        for rel in self.FILES:
            with self.subTest(file=rel):
                lines = self._lines(rel)
                self.assertEqual(lines.count(self.RULE), 1)
                triage = [l for l in lines if "fleet-switchboard triage" in l and l.lstrip().startswith("- {action:")]
                self.assertEqual(triage, [self.RULE])  # no second triage rule, no deny, no variant
                for l in lines:  # fail closed: nothing allows the whole command or a wildcard prefix
                    if l.lstrip().startswith("- {action: shell") and "effect: allow" in l:
                        self.assertNotRegex(l, r'resource: "fleet-switchboard \*"')
                        self.assertNotRegex(l, r'resource: "\*+fleet-switchboard')

    def test_the_switchboard_allow_rules_are_the_fixed_old_list_plus_triage_move_reassign(self):
        old = {
            "chief-of-staff.md": "send report remind intent intents status pending whoami notice decisions orders gates waive plan labels handoff launch reattach relaunch retire disk".split(),
            "cos-subagent.md": "send report remind intent intents status disk pending whoami version notice decisions orders gates waive plan labels".split(),
        }
        for rel in self.FILES:
            with self.subTest(file=rel):
                got = [m.group(1) for l in self._lines(rel) for m in [re.match(r'  - \{action: shell, resource: "fleet-switchboard ([a-z]+) \*", effect: allow\}$', l)] if m]
                self.assertEqual(sorted(got), sorted(old[rel.split("/")[-1]] + ["triage", "move", "reassign", "resume"]))


class TestCosDelegatedClearPermission(unittest.TestCase):
    """Ask #720: the Chief of Staff definition allows the delegated clear (`impact clear * --under-order *`) and no other impact command beyond the
    two read-only ones. The copilot definition has no per-command list (a `tools:` allow-list and prose), so the line is only in the opencode one."""
    RULE = '  - {action: shell, resource: "fleet-switchboard impact clear * --under-order *", effect: allow}'
    PATH = REPO / "agents" / "opencode" / "chief-of-staff.md"

    def test_S1_the_line_once_and_the_other_impact_permissions_are_the_two_read_only_ones(self):
        lines = self.PATH.read_text().splitlines()
        self.assertEqual(lines.count(self.RULE), 1)
        others = [l for l in lines if l.lstrip().startswith("- {action: shell") and "fleet-switchboard impact " in l and l != self.RULE]
        self.assertEqual(others, ['  - {action: shell, resource: "fleet-switchboard impact floor *", effect: allow}',
                                  '  - {action: shell, resource: "fleet-switchboard impact hold --dry-run*", effect: allow}'])

    def test_S1_the_copilot_definition_names_no_impact_command(self):
        text = (REPO / "agents" / "copilot" / "chief-of-staff.agent.md").read_text()
        self.assertNotIn("fleet-switchboard impact", text)
        self.assertNotIn("--under-order", text)

    def test_S2_last_match_wins_allows_only_the_delegated_clear(self):
        _, rules = TestChiefOfStaffNativePermissions.parse(self.PATH)
        verdicts = [TestChiefOfStaffNativePermissions.evaluate(rules, "shell", c) for c in (
            "fleet-switchboard impact clear 5 --reason why",
            "fleet-switchboard impact clear 5 --reason why --under-order 1",
            "fleet-switchboard impact waive 5")]
        self.assertEqual(verdicts, ["deny", "allow", "deny"])


class TestClearCarry(unittest.TestCase):
    """#510: an owner clear carries over a pure merge of main and over nothing else. Every case runs on a REAL git repository (neutral names)."""

    REPO = "acme-org/widget-api"

    def setUp(self):
        self.cc = sb.load_module("clear_carry")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        self.api = build_carry_repo(self.dir)
        self.cleared = self.api.rev("feature")
        self.fp = self.fingerprint()

    def write(self, path, text, binary=False):
        target = self.dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.write_bytes if binary else target.write_text)(text)

    def fingerprint(self):
        fingerprint, head, files = self.cc.own_change_fingerprint(self.api.get, self.REPO, 7)
        self.assertEqual(head, self.api.rev("feature"))
        return fingerprint

    def main_moves(self, *names, edit=None):
        g = self.api.git
        g("checkout", "-q", "main")
        for name in names:
            self.write(name, "from main %s\n" % name)
        if edit:
            self.write(*edit)
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "feature")

    def merge_main(self):
        self.api.git("merge", "-q", "--no-edit", "main")
        return self.api.rev("feature")

    def decide(self, new):
        return self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new)

    def test_s1_a_pure_merge_of_main_carries_the_clear_and_the_fingerprint_is_the_original(self):
        self.main_moves("m1.txt", "m2.txt", "m3.txt")
        new = self.merge_main()
        verdict = self.decide(new)
        self.assertEqual((verdict["carried"], verdict["reason"], verdict["fingerprint"], verdict["from"], verdict["to"]), (True, None, self.fp, self.cleared, new))

    def test_s2_a_commit_of_its_own_an_edited_line_an_added_or_a_removed_file_void_the_clear(self):
        g = self.api.git
        for label, change in (("a commit of its own", lambda: (self.write("d.txt", "d\n"), g("add", "-A"), g("commit", "-q", "-m", "own"))),
                              ("an edited line", lambda: (self.write("a.txt", "one\nTWO!\nthree\nfour\nfive\nsix\nseven\n"), g("commit", "-q", "-am", "edit"))),
                              ("an added file", lambda: (self.write("e.txt", "e\n"), g("add", "-A"), g("commit", "-q", "-m", "add"))),
                              ("a removed file", lambda: (g("rm", "-q", "c.txt"), g("commit", "-q", "-m", "remove")))):
            with self.subTest(label):
                g("checkout", "-q", "-B", "feature", self.cleared)
                self.main_moves("m_%s.txt" % label.replace(" ", "_"))
                self.merge_main()
                change()
                verdict = self.decide(self.api.rev("feature"))
                self.assertFalse(verdict["carried"], label)
                self.assertIn("commit of the pull request's own", verdict["reason"])
        # an "evil merge": main merged, and the merge commit itself edits or adds a file
        for label, edit in (("edits a cleared file", lambda: self.write("c.txt", "changed in the merge\n")), ("adds a file", lambda: self.write("d.txt", "sneaked in\n"))):
            with self.subTest(label):
                g("checkout", "-q", "-B", "feature", self.cleared)
                self.main_moves("n_%s.txt" % label.replace(" ", "_"))
                g("merge", "-q", "--no-commit", "--no-ff", "main")
                edit()
                g("add", "-A")
                g("commit", "-q", "-m", "merge main (and more)")
                verdict = self.decide(self.api.rev("feature"))
                self.assertFalse(verdict["carried"], label)
                self.assertIn("relative to both parents", verdict["reason"])

    def test_s3_a_conflict_resolved_by_hand_voids_the_clear_even_though_the_file_list_is_the_same(self):
        g = self.api.git
        self.main_moves(edit=("a.txt", "one\nTWO from main\nthree\nfour\nfive\nsix\nseven\n"))
        done = subprocess.run(["git", "-C", str(self.dir), "merge", "--no-edit", "main"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0, "the merge conflicts in a.txt")
        self.write("a.txt", "one\nTWO resolved by hand\nthree\nfour\nfive\nsix\nseven\n")
        g("add", "-A")
        g("commit", "-q", "-m", "merge main, conflict resolved")
        verdict = self.decide(self.api.rev("feature"))
        self.assertFalse(verdict["carried"])
        self.assertIn("a.txt", verdict["reason"])
        self.assertIn("relative to both parents", verdict["reason"])
        same_files = self.cc.own_change(self.api.get, self.REPO, 7)[0]
        self.assertEqual(sorted(f["path"] for f in same_files), ["a.txt", "c.txt"], "the same two files: only the content check catches it")

    def test_s4_a_rewritten_history_with_the_same_final_diff_is_void(self):
        g = self.api.git
        self.main_moves("m1.txt")
        g("rebase", "-q", "main")
        new = self.api.rev("feature")
        self.assertNotEqual(new, self.cleared)
        self.assertEqual(self.fingerprint(), self.fp, "the rebased pull request has exactly the same own change")
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("rewritten history", verdict["reason"])

    def test_s5_a_cut_file_list_an_omitted_patch_and_a_read_error_void_the_clear_and_name_what_could_not_be_read(self):
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.api.changed_files_override = 5
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("cut: GitHub lists 2 of 5 files", verdict["reason"])
        self.api.changed_files_override = None
        self.api.fail = "/compare/"
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("could not be read", verdict["reason"])
        self.api.fail = None
        g = self.api.git
        g("checkout", "-q", "-B", "feature", self.cleared)
        self.write("blob.dat", bytes(range(256)) * 4, binary=True)
        g("add", "-A")
        g("commit", "-q", "-m", "binary")
        with self.assertRaises(self.cc.CarryVoid) as raised:
            self.cc.own_change_fingerprint(self.api.get, self.REPO, 7)
        self.assertIn("patch of blob.dat is omitted", str(raised.exception))
        binary_cleared = self.api.rev("feature")
        self.main_moves("m2.txt")
        merged = self.merge_main()
        verdict = self.cc.decide(self.api.get, self.REPO, 7, binary_cleared, "sha256:" + "0" * 64, merged)
        self.assertFalse(verdict["carried"])
        self.assertIn("omitted", verdict["reason"])
        verdict = self.cc.decide(self.api.get, self.REPO, 7, self.cleared, None, new)
        self.assertEqual((verdict["carried"], verdict["reason"]), (False, "no fingerprint was recorded with the clear"))
        self.assertFalse(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, "not-a-hash", new)["carried"])

    def test_a_merge_of_a_side_branch_a_retargeted_base_and_a_head_that_moves_mid_read_all_void(self):
        g = self.api.git
        g("checkout", "-q", "-b", "side", self.cleared)
        self.write("side.txt", "side\n")
        g("add", "-A")
        g("commit", "-q", "-m", "side")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-ff", "--no-edit", "side")
        verdict = self.decide(self.api.rev("feature"))
        self.assertFalse(verdict["carried"])
        self.assertIn("not on main", verdict["reason"])
        g("checkout", "-q", "-B", "feature", self.cleared)
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.assertTrue(self.decide(new)["carried"])
        g("branch", "release", self.cleared)
        self.api.base = "release"
        self.assertFalse(self.decide(new)["carried"], "a retargeted base is a different change")
        self.api.base = "main"
        calls = {"n": 0}
        real = self.cc.pull_head

        def moving(get, repo, number):
            calls["n"] += 1
            head, pull = real(get, repo, number)
            return (head if calls["n"] == 1 else "0" * 40), pull
        self.cc.pull_head = moving
        self.addCleanup(setattr, self.cc, "pull_head", real)
        self.assertFalse(self.decide(new)["carried"])

    def test_s6_successive_merges_are_each_compared_with_the_original_and_an_own_change_makes_the_next_void(self):
        g = self.api.git
        carried = []
        for n in (1, 2, 3):
            self.main_moves("m%d.txt" % n)
            carried.append(self.decide(self.merge_main())["carried"])   # each decided when it is the current head, against the ORIGINAL cleared head and fingerprint
        self.assertEqual(carried, [True, True, True], "three merges, three carries")
        self.write("a.txt", "one\nTWO\nthree\nfour\nfive\nsix\nSEVEN\n")
        g("commit", "-q", "-am", "own change after the second merge")
        self.main_moves("m4.txt")
        fourth = self.merge_main()
        verdict = self.decide(fourth)
        self.assertFalse(verdict["carried"], "a change of its own after a carry makes every later head void")

    def test_a_pure_merge_whose_own_change_is_not_the_one_cleared_is_void_at_the_last_check_and_each_merge_is_compared_with_the_original(self):
        g = self.api.git
        self.main_moves("m1.txt")
        new = self.merge_main()
        wrong = "sha256:" + "1" * 64
        verdict = self.cc.decide(self.api.get, self.REPO, 7, self.cleared, wrong, new)
        self.assertFalse(verdict["carried"], "the merge chain and the merges are clean: only the fingerprint comparison can refuse this")
        self.assertIn("is not the one the owner cleared", verdict["reason"])
        self.assertEqual(verdict["fingerprint"], self.fp, "the verdict carries the fingerprint it computed, for the audit")
        # drift: a pure merge whose own change differs from the ORIGINAL but equals the PREVIOUS head's is still void
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new)["carried"])
        previous_fp = self.fingerprint()
        self.assertEqual(previous_fp, self.fp)
        drifted = "sha256:" + "2" * 64            # what a build comparing with an earlier carried head would hold after a small change slipped in
        self.main_moves("m2.txt")
        second = self.merge_main()
        self.assertFalse(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, drifted, second)["carried"])

    def test_a_pure_rename_and_a_renamed_and_edited_file_carry_over_a_merge_and_an_edit_after_the_clear_does_not(self):
        g = self.api.git
        g("checkout", "-q", "-B", "feature", "main")
        g("mv", "src/x.py", "src/y.py")
        g("commit", "-q", "-m", "rename")
        cleared = self.api.rev("feature")
        files, _, _ = self.cc.own_change(self.api.get, self.REPO, 7)
        self.assertEqual([(f["path"], f["status"], f["previous"]) for f in files], [("src/y.py", "renamed", "src/x.py")])
        fp = self.fingerprint()
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, cleared, fp, new)["carried"], "a pure rename's blob id pins its content")
        # a rename that is also edited carries too (its patch is present and pinned); an edit AFTER the clear does not
        g("checkout", "-q", "-B", "feature", "main")
        g("mv", "src/x.py", "src/z.py")
        self.write("src/z.py", "def x():\n    return 2\n")
        g("add", "-A")
        g("commit", "-q", "-m", "rename and edit")
        edited = self.api.rev("feature")
        edited_fp = self.fingerprint()
        self.main_moves("m2.txt")
        merged = self.merge_main()
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, edited, edited_fp, merged)["carried"])
        self.write("src/z.py", "def x():\n    return 3\n")
        g("commit", "-q", "-am", "edit after the clear")
        self.main_moves("m3.txt")
        verdict = self.cc.decide(self.api.get, self.REPO, 7, edited, edited_fp, self.merge_main())
        self.assertFalse(verdict["carried"])
        self.assertIn("commit of the pull request's own", verdict["reason"])

    def test_s8_there_is_one_fingerprint_function_and_the_carry_code_holds_no_path_or_class_literal(self):
        import ast
        main = switchboard_source() + "\n" + (Path(sb.__file__).parent / "impact_hold.py").read_text()   # #532: the clear's wrapper moved to bin/impact_hold.py, still "the switchboard's" code
        module = Path(sb.__file__).parent / "clear_carry.py"
        text = module.read_text()
        defs = [n.name for src in (main, text) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and "fingerprint" in n.name]
        self.assertEqual(sorted(defs), ["clear_fingerprint", "fingerprint_of", "own_change_fingerprint"], "one computation (fingerprint_of), one entry point (own_change_fingerprint) and the clear's wrapper")
        calls = [n for n in ast.walk(ast.parse(main)) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "own_change_fingerprint"]
        self.assertEqual(len(calls), 1, "the clear's wrapper is the only caller in the switchboard; the carry's decide calls the same function inside the module")
        imports = sorted(a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Import) for a in n.names)
        self.assertEqual(imports, ["hashlib", "json"], "the carry imports no floor, glob or class code: it decides from GitHub's answers alone")


class TestNativeChildSession(Base):
    def test_a_pane_less_child_with_the_parents_fleet_metadata_is_not_a_second_agent(self):
        world = World(self, agents=("cos",))
        child = world.v2.create_session({"fleet": {"name": "cos", "role": "chief-of-staff"}})
        world.v2.sessions[child]["parentID"] = world.sessions["cos"]  # herdr shows no pane for it
        found = world.engine.discover(world.clock.now())
        self.assertEqual(list(found.agents), ["cos"])
        self.assertEqual((found.agents["cos"].session_id, found.agents["cos"].conflict), (world.sessions["cos"], []))

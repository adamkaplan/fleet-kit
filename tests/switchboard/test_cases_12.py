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
from support.core import Base, SAFE_PATH
from support.fake_jev import FAKE_SB_SOURCE, SHIM, SHIM_LOOKUP_SECONDS, write_fake_env
from support.loader import sb
from support.rebind import rebound
from support.scenario_roles import FAKE_GH


class TestAgentShellCalls(Base):
    """#367: calls made from an agent's shell are counted per account and per hour, with nothing else recorded."""

    def setUp(self):
        Base.setUp(self)
        if time.time() % 3600 > 3570 or time.time() % 3600 < 10:   # the shim stamps real time: never start these across an hour boundary
            time.sleep(40)
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text("#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho real gh out\nexit 7\n")
        fake.chmod(0o755)
        self.sb = self.home / "fakesb"
        self.sb.write_text("#!/bin/sh\nif [ \"$1\" = whoami ]; then\n  if [ \"$2\" = --json ]; then echo '{\"name\": \"worker\", \"repo\": \"acme/app\"}'; else echo worker; fi; exit 0; fi\nexit 2\n")
        self.sb.chmod(0o755)
        self.state = self.home / "state"
        self.state.mkdir()
        self.users({"acme/app": "acme-a"})
        sb.agent_calls.fold(str(self.state), time.time() - 7200)     # the daemon switches counting on, two hours ago

    def users(self, mapping):
        config = self.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"gh_users": mapping}))

    def shim(self, *args, env=None):
        environ = {"PATH": "%s:%s:%s" % (SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home), "FLEET_SWITCHBOARD_STATE": str(self.state),
                   "FLEET_SWITCHBOARD_BIN": str(self.sb), "FLEET_SHIM_LOOKUP_SECONDS": SHIM_LOOKUP_SECONDS}
        environ.update(env or {})
        return subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, env=environ, timeout=60)

    def test_s1_calls_are_counted_per_account(self):
        for _ in range(5):
            self.shim("api", "repos/acme/app/issues/5")
        self.users({"acme/app": "acme-b"})
        for _ in range(3):
            self.shim("api", "repos/acme/app/issues/6")
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("agent shell calls this hour: acme-a 5, acme-b 3", text)

    def test_s2_the_hours_are_listed_and_an_hour_before_counting_began_is_not_recorded(self):
        now = time.time()
        sb.agent_calls.fold(str(self.state), now, {"acme-a": 4})
        lines = [l for l in sb.agent_calls.status_lines(str(self.state), now) if " hour " in l and "Z:" in l]
        self.assertEqual(len(lines), 6)
        self.assertEqual(len([l for l in lines if "not recorded" in l]), 3)                 # the 3 hours before counting began
        self.assertEqual(len([l for l in lines if "agent shell" in l]), 3)
        self.assertIn("daemon 4", lines[-1])

    def test_s6_a_counting_failure_changes_nothing_the_caller_sees(self):
        (self.state / "agent-calls.jsonl").mkdir()                                            # the spool cannot be opened for appending
        done = self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))

    def test_s7_only_the_account_the_class_and_the_time_are_stored(self):
        done = self.shim("api", "repos/acme/app/issues/5?token=queryvalue", "-f", "body=secretbody", env={"GH_TOKEN": "secrettoken"})
        text = (self.state / "agent-calls.jsonl").read_text()
        for leak in ("secrettoken", "faketoken", "secretbody", "queryvalue", "acme/app"):
            self.assertNotIn(leak, text)
        row = json.loads(text.splitlines()[0])
        self.assertEqual((row["account"], row["endpoint"], sorted(row)), ("acme-a", "api issues/#", ["account", "endpoint", "lane", "ts"]))   # the lane is the one field added (#867)

    def test_nothing_is_counted_until_the_daemon_has_switched_counting_on_and_the_switchboards_own_calls_are_not(self):
        (self.state / "agent-calls-hours.json").unlink()
        self.shim("api", "repos/acme/app/issues/5")
        self.assertFalse((self.state / "agent-calls.jsonl").exists())
        sb.agent_calls.fold(str(self.state), time.time())
        self.shim("auth", "status")
        self.assertFalse((self.state / "agent-calls.jsonl").exists())

    def test_s8_a_corrupt_count_file_starts_a_fresh_count_and_status_still_reads(self):
        (self.state / "agent-calls-hours.json").write_text("{not json")
        self.assertIn("not recorded", "\n".join(sb.agent_calls.status_lines(str(self.state), time.time())))
        self.shim("api", "repos/acme/app/issues/5")
        self.assertTrue(sb.agent_calls.fold(str(self.state), time.time(), {"acme-a": 1}))   # the daemon's next fold rebuilds the file
        self.assertEqual(json.loads((self.state / "agent-calls-hours.json").read_text())["v"], 1)
        self.assertEqual(sum(sum(r.get("agent", {}).values()) for r in sb.agent_calls.load_hours(str(self.state))[0].values()), 1)

    def test_a_full_spool_takes_no_more_lines_and_the_hours_kept_are_bounded(self):
        (self.state / "agent-calls.jsonl").write_text("x" * 300000)
        self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual(len((self.state / "agent-calls.jsonl").read_text()), 300000)
        (self.state / "agent-calls.jsonl").unlink()
        now = time.time()
        for back in range(sb.agent_calls.HOURS_KEPT + 5):
            sb.agent_calls.fold(str(self.state), now - back * 3600, {"acme-a": 1})
        self.assertLessEqual(len(sb.agent_calls.load_hours(str(self.state))[0]), sb.agent_calls.HOURS_KEPT)

    def test_the_daemon_counts_only_its_new_calls_each_pass_and_keeps_them_when_the_write_fails(self):
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""),
                                 time.time, environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=time.time, _note=lambda *a, **k: None)
        accounts.read(accounts.runner, "repos/acme/app/issues/1", "acme/app", 5)
        sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                      # nothing new: nothing added
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("daemon", {}).values()) for r in hours.values()), 1)
        accounts.read(accounts.runner, "repos/acme/app/issues/2", "acme/app", 5)
        with mock.patch.object(sb.agent_calls, "_write_json", return_value=False):
            sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                      # the failed pass's call is counted now
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("daemon", {}).values()) for r in hours.values()), 2)

    def test_lane_s1_calls_are_split_per_lane_and_account_and_status_shows_them(self):
        self.shim("api", "repos/acme/app/issues/5")                                        # lane worker (the fake switchboard's whoami name)
        self.shim("api", "repos/acme/app/issues/6")
        self.users({"acme/app": "acme-b"})
        self.shim("pr", "view", "3", env={"FLEET_SWITCHBOARD_BIN": "/nonexistent"})          # no identity and no role: unknown
        self.shim("pr", "view", "3", env={"FLEET_SWITCHBOARD_BIN": "/nonexistent", "FLEET_SWITCHBOARD_ROLE": "tester"})   # a caller's variable never names its lane
        self.assertEqual([json.loads(l).get("lane") for l in (self.state / "agent-calls.jsonl").read_text().splitlines()], ["worker", "worker", "unknown", "unknown"])
        sb.agent_calls.fold(str(self.state), time.time(), {"acme-a": {"issues/#": 2}})
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        lanes = {}
        for row in hours.values():
            for lane, accounts in row.get("lanes", {}).items():
                lanes[lane] = sum(sum(e.values()) for e in accounts.values())
        self.assertEqual(lanes, {"worker": 2, "unknown": 2, "daemon": 2})
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("this hour per lane: daemon 2, unknown 2, worker 2", text)
        self.assertIn("lane worker: acme-a 2 (api issues/# 2)", text)

    def test_lane_an_old_spool_line_without_a_lane_is_read_as_unknown_and_a_hostile_lane_is_cleaned(self):
        (self.state / "agent-calls.jsonl").write_text(json.dumps({"ts": int(time.time()), "account": "acme-a", "endpoint": "pr view"}) + "\n"
                                                      + json.dumps({"ts": int(time.time()), "account": "acme-a", "endpoint": "pr view", "lane": "a b\nc"}) + "\n")
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("unknown 1", text)
        self.assertIn("a?b?c 1", text)
        self.assertNotIn("a b", text)

    def test_lane_s7_no_value_reaches_the_record_beside_the_lane(self):
        self.shim("api", "repos/acme/app/issues/5?token=queryvalue", "-f", "body=secretbody", env={"GH_TOKEN": "secrettoken"})
        row = json.loads((self.state / "agent-calls.jsonl").read_text().splitlines()[0])
        self.assertEqual(sorted(row), ["account", "endpoint", "lane", "ts"])
        sb.agent_calls.fold(str(self.state), time.time())
        record = (self.state / "agent-calls-hours.json").read_text()
        for leak in ("secrettoken", "secretbody", "queryvalue", "acme/app"):
            self.assertNotIn(leak, record)

    def test_daemon_classes_keep_their_class_and_two_that_reduce_alike_add(self):
        now = time.time()
        sb.agent_calls.fold(str(self.state), now, {"acme-a": {"api issues/#": 2, "api pulls/#": 1, "api graphql": 1}})
        sb.agent_calls.fold(str(self.state), now, {"acme-b": {"api zz-one": 2, "api zz-two": 1, "browse x": 1}})   # three classes that all reduce to `other`/`api repo`/`browse`
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        row = list(hours.values())[-1]
        self.assertEqual(row["daemon"], {"acme-a": 4, "acme-b": 4})
        cells = row["lanes"]["daemon"]
        self.assertEqual(cells["acme-a"], {"api issues/#": 2, "api pulls/#": 1, "api graphql": 1})
        self.assertEqual(sum(cells["acme-b"].values()), 4)
        self.assertEqual(cells["acme-b"]["api repo"], 3)                                  # two classes reduced to one word: 2 + 1, not the last one
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""), time.time, environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=time.time, _note=lambda *a, **k: None)
        for path in ("repos/acme/app/issues/1", "repos/acme/app/pulls/2", "graphql"):
            accounts.read(accounts.runner, path, "acme/app", 5)
        sb.record_github_calls(engine)
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        mine = list(hours.values())[-1]["lanes"]["daemon"]["default"]
        self.assertEqual(mine, {"api issues/#": 1, "api pulls/#": 1, "api graphql": 1})   # the daemon's own classes survive the fold

    def test_lane_a_failed_write_leaves_no_temp_file(self):
        with mock.patch.object(sb.agent_calls.os, "replace", side_effect=OSError("no")):
            self.assertFalse(sb.agent_calls._write_json(str(self.state / "x.json"), {"a": 1}))
        self.assertEqual([f.name for f in self.state.iterdir() if f.name.startswith(".agent-calls-")], [])

    def test_lane_lanes_are_bounded_per_hour(self):
        lanes = "\n".join(json.dumps({"ts": int(time.time()), "account": "a", "endpoint": "pr view", "lane": "l%d" % i}) for i in range(60))
        (self.state / "agent-calls.jsonl").write_text(lanes + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        row = list(hours.values())[0]["lanes"]
        self.assertLessEqual(len(row), sb.agent_calls.LANES_PER_HOUR + 1)
        self.assertEqual(sum(sum(e.values()) for a in row.values() for e in a.values()), 60)

    def test_lane_s10_an_unreadable_spool_still_runs_the_command(self):
        (self.state / "agent-calls.jsonl").mkdir()
        done = self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))

    def test_usage_s2_the_daemon_records_real_usage_once_a_minute_without_counting_it(self):
        reset = int(time.time() // 3600) * 3600 + 3600   # the window then covers this whole hour bucket: no proration in the figure
        asked = []

        def runner(argv, **kw):
            asked.append((argv, kw.get("env", {}).get("FLEET_SWITCHBOARD_OWN_GH")))
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "tok\n", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"resources": {"core": {"limit": 5000, "used": 4000, "remaining": 1000, "reset": reset}}}), "")

        clock = [time.time()]
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "acme-a"}}, runner, lambda: clock[0], environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=lambda: clock[0], _note=lambda *a, **k: None)
        sb.agent_calls.fold(str(self.state), clock[0], {"acme-a": 100})
        sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                       # inside the minute: no second read
        self.assertEqual(len([a for a in asked if a[0][1:] == ["api", "rate_limit"]]), 1)
        self.assertEqual([a[1] for a in asked if a[0][1:] == ["api", "rate_limit"]], [None])  # no marker any more: the daemon reaches the real gh by path (own_gh_path), so the shim never sees, hence never counts, it
        self.assertEqual([a[0][0] for a in asked if a[0][1:] == ["api", "rate_limit"]], [FAKE_GH])
        self.assertEqual(len(accounts.calls), 0, "the rate-limit read is not a counted call")
        accounts.read(runner, "repos/acme/app/issues/1", "acme/app", 5)
        self.assertEqual(len(accounts.calls), 1, "the daemon's OWN counter records a read it makes (the shim's per-account file never sees it)")
        self.assertEqual(asked[-1][0][0], FAKE_GH)
        body = json.loads((self.state / "github-usage.json").read_text())["accounts"]["acme-a"]
        self.assertEqual((body["used"], body["remaining"], body["reset"]), (4000, 1000, reset))
        text = "\n".join(sb.agent_calls.usage_lines(str(self.state), clock[0]))
        self.assertIn("real usage acme-a: used 4000, remaining 1000", text)
        self.assertIn("recorded 100, outside the shim and the daemon about 3900", text)
        clock[0] += 61
        sb.record_github_calls(engine)
        self.assertEqual(len([a for a in asked if a[0][1:] == ["api", "rate_limit"]]), 2)

    def test_usage_the_daemons_read_runs_the_real_gh_binary_and_its_own_counter_counts_it_not_the_shims_file(self):
        real = self.home / "realgh"
        real.write_text("#!/bin/sh\necho \"real $*\" >> %s\necho '{}'\n" % (self.home / "real.log"))
        real.chmod(0o755)
        accounts = sb.GhAccounts({"gh": str(real), "call_timeout_seconds": 5, "gh_users": {}}, subprocess.run, time.time, environ={})
        done = accounts.read(subprocess.run, "repos/acme/app/issues/1", "acme/app", 5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / "real.log").read_text().splitlines(), ["real api repos/acme/app/issues/1"], "the REAL gh binary ran, by path")
        self.assertEqual(len(accounts.calls), 1, "the daemon's own counter counted it")
        self.assertFalse((self.state / "agent-calls.jsonl").exists(), "the shim's per-account call file does not record the daemon's own reads")

    def test_usage_a_garbled_read_is_left_out_and_never_raises(self):
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "acme-a"}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not json", ""),
                                 time.time, environ={})
        self.assertEqual(accounts.read_usage(), {})
        self.assertIn("not recorded", "\n".join(sb.agent_calls.usage_lines(str(self.state), time.time())))

    def test_usage_the_default_token_the_account_cap_a_failed_runner_and_a_stale_reading(self):
        good = json.dumps({"resources": {"core": {"limit": 5000, "used": 10, "remaining": 4990, "reset": int(time.time()) + 600}}})
        users = {"acme/r%d" % i: "acct%d" % i for i in range(12)}

        def runner(argv, **kw):
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "tok\n", "")
            if kw["env"].get("GH_TOKEN") == "tok" and "acct3" in str(self.__dict__.get("bad", "")):
                raise OSError("boom")
            return subprocess.CompletedProcess(argv, 0, good, "")

        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": users}, runner, time.time, environ={"GH_TOKEN": "inherited"})
        found = accounts.read_usage()
        self.assertEqual(len(found), sb.agent_calls.USAGE_ACCOUNTS + 1)    # the cap, plus the default token's reading
        self.assertIn("default", found)
        failing = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "a"}},
                                lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no"), time.time, environ={})
        self.assertEqual(failing.read_usage(), {})
        sb.agent_calls.record_usage(str(self.state), time.time() - 900, {"a": (1, 4999, 5000, int(time.time()) + 600)})
        self.assertIn("(read 15 min ago)", "\n".join(sb.agent_calls.usage_lines(str(self.state), time.time())))

    def test_lane_endpoint_classes_per_lane_and_account_are_bounded(self):
        rows = "\n".join(json.dumps({"ts": int(time.time()), "account": "a", "endpoint": "api " + sorted(sb.agent_calls.API_SUB)[i], "lane": "w"}) for i in range(40))
        (self.state / "agent-calls.jsonl").write_text(rows + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        cell = list(sb.agent_calls.load_hours(str(self.state))[0].values())[0]["lanes"]["w"]["a"]
        self.assertEqual((len(cell), sum(cell.values()), cell["other"]), (sb.agent_calls.ENDPOINTS_PER_CELL + 1, 40, 10))

    def shim_module(self):
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("gh_shim_endpoint", str(SHIM))
        spec = importlib.util.spec_from_loader("gh_shim_endpoint", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_endpoint_class_s7_no_name_reaches_any_record_from_any_call_shape(self):
        """PC-1 as a class: an allow-list, so a name-bearing and a name-free variant of every gh shape records no name token (shim, spool, fold, status)."""
        shim = self.shim_module()
        name = "zzname"
        shapes = [["api", "https://api.github.com/repos/acme-%s/app-%s/pulls/5" % (name, name)], ["api", "repos/acme-%s/app-%s/issues/5?q=%s" % (name, name, name)],
                  ["api", "/repos/o-%s/n-%s/contents/dir-%s/file-%s.py" % (name, name, name, name)], ["api", "orgs/acme-%s/members" % name], ["api", "users/bob-%s/repos" % name],
                  ["api", "teams/t-%s" % name], ["api", "search/issues?q=repo:acme-%s/app" % name], ["api", "user/repos"], ["api", "graphql"], ["api", "rate_limit"],
                  ["api", "repos/o/n/git/ref/heads/feature-%s" % name], ["api", "http://host-%s.example/repos/o/n/issues/1" % name],
                  ["browse", "acme-%s/app" % name], ["repo", "clone", "acme-%s/app" % name], ["repo", "view", "acme-%s/app" % name], ["pr", "view", "5", "-R", "acme-%s/app" % name],
                  ["pr", "list"], ["issue", "list"], ["issue", "view", "acme-%s" % name], ["release", "view", "v-%s" % name], ["run", "view", "123"], ["workflow", "run", "ci-%s.yml" % name],
                  ["gist", "view", "g-%s" % name], ["search", "prs", name], ["auth", "status"], ["label", "list"], ["ruleset", "view", "r-%s" % name],
                  ["frobnicate-%s" % name, "x"], ["pr", "wibble-%s" % name]]
        for args in shapes:
            got = shim.call_endpoint(args)
            self.assertNotIn(name, got, args)
            self.assertNotIn("acme", got)
            self.assertEqual(got, sb.agent_calls.reduce_endpoint(got), "idempotent")                     # the daemon side reduces again to the same
            self.assertEqual(sb.agent_calls.reduce_endpoint(" ".join(shim.command_words(args)[:2])), got, "the shim and the daemon side are one rule")
            self.assertNotIn(name, sb.agent_calls._endpoint("api https://api.github.com/repos/acme/app-%s/x" % name))
        self.assertEqual(shim.call_endpoint(["api", "https://api.github.com/repos/acme/app/pulls/5"]), "api pulls/#")
        self.assertEqual((shim.call_endpoint(["api", "orgs/acme-org/members"]), shim.call_endpoint(["browse", "acme/app"])), ("api orgs", "browse"))
        self.assertEqual(shim.call_endpoint(["pr", "wibble"]), "pr")
        self.assertEqual(shim.call_endpoint(["frob", "x"]), "other")

    def test_endpoint_class_an_old_or_hostile_spool_line_cannot_carry_a_name_in(self):
        (self.state / "agent-calls.jsonl").write_text(json.dumps({"ts": int(time.time()), "account": "a", "lane": "w", "endpoint": "api https://api.github.com/repos/acme/app-zzname/pulls/#"}) + "\n"
                                                      + json.dumps({"ts": int(time.time()), "account": "a", "lane": "w", "endpoint": "browse acme/zzname"}) + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        self.assertNotIn("zzname", (self.state / "agent-calls-hours.json").read_text())
        self.assertNotIn("zzname", "\n".join(sb.agent_calls.status_lines(str(self.state), time.time())))

    def test_endpoint_class_the_daemons_own_record_is_reduced_too(self):
        self.assertEqual(sb._endpoint_of("orgs/acme-org/members?x=1"), "orgs")
        self.assertEqual(sb._endpoint_of("repos/acme/app/issues/12/parent"), "issues/#/parent")

    def test_the_fold_moves_the_spool_into_the_hours_and_loses_no_line(self):
        for _ in range(2):
            self.shim("api", "repos/acme/app/issues/5")
        now = time.time()
        self.assertTrue(sb.agent_calls.fold(str(self.state), now))
        self.assertFalse((self.state / "agent-calls.jsonl").exists())
        hours, since = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("agent", {}).values()) for r in hours.values()), 2)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.state / "agent-calls-hours.json")).st_mode), 0o600)


class TestShimReadCache(Base):
    """#867 clauses 3 and 4: a repeated identical `gh api <path>` read is answered from a short cache and revalidated with the entity tag; a write by the same agent
    drops it; below 15 percent of the budget the shim says so once and serves a cached read instead of spending."""

    def setUp(self):
        TestAgentShellCalls.setUp(self)
        (self.bin / "gh").write_text(
            "#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho \"$@\" >> \"$FAKE_LOG\"\n"
            "case \"$*\" in\n *If-None-Match*) printf 'HTTP/2.0 304 Not Modified\\r\\n\\r\\n'; exit 0;;\n"
            " \"api -i \"*) printf 'HTTP/2.0 200 OK\\r\\nEtag: \"v1\"\\r\\n\\r\\n{\"n\":1}'; exit 0;;\n"
            " \"api \"*) printf '{\"n\":1}'; exit 0;;\nesac\necho wrote\nexit 0\n")
        self.log = self.home / "fake.log"
        self.other = self.home / "othersb"
        self.other.write_text(self.sb.read_text().replace("worker", "other"))
        self.other.chmod(0o755)

    def run_shim(self, *args, env=None):
        return TestAgentShellCalls.shim(self, *args, env=dict({"FAKE_LOG": str(self.log)}, **(env or {})))

    def network(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def age(self, seconds):
        cache = self.state / "gh-read-cache"
        for name in os.listdir(cache):
            if name.startswith(".w-"):
                mark = json.loads((cache / name).read_text())
                mark["ts"] -= seconds   # an agent's write mark ages with the entries
                (cache / name).write_text(json.dumps(mark))
                continue
            entry = json.loads((cache / name).read_text())
            entry["ts"] -= seconds
            (cache / name).write_text(json.dumps(entry))

    def usage(self, remaining, limit=5000, account="acme-a"):
        (self.state / "github-usage.json").write_text(json.dumps({"v": 1, "at": time.time(), "accounts": {account: {"used": limit - remaining, "remaining": remaining, "limit": limit, "reset": time.time() + 900}}}))

    users = TestAgentShellCalls.users
    shim_module = TestAgentShellCalls.shim_module
    PATH = "repos/acme/app/issues/5"

    def test_s3_a_repeated_read_is_served_from_the_cache_then_revalidated_and_a_304_is_not_counted(self):
        first = self.run_shim("api", self.PATH)
        second = self.run_shim("api", self.PATH)
        self.assertEqual((first.stdout, second.stdout, second.returncode), ('{"n":1}', '{"n":1}', 0))
        self.assertEqual(len(self.network()), 1, "2 reads, 1 network call")
        self.age(31)
        third = self.run_shim("api", self.PATH)
        self.assertEqual(third.stdout, '{"n":1}')
        self.assertEqual(len(self.network()), 2)
        self.assertIn("If-None-Match: \"v1\"", self.network()[1])
        self.assertEqual(len((self.state / "agent-calls.jsonl").read_text().splitlines()), 1, "the 304 is not counted as a call")

    def marker_mutant(self, func):
        """#1216: a copy of the shim directory in which `func` honours the retired marker again (returns at once)."""
        source = SHIM.read_text()
        start = source.index("def %s(" % func)
        head = source[start:]
        at = head.index(":\n", head.index(")")) + 2
        if head[at:].lstrip().startswith('"""'):
            at = head.index('"""', head.index('"""', at) + 3) + 4
        guard = '    if os.environ.get("FLEET_SWITCHBOARD_OWN_GH") == "1":\n        return None\n'
        target = self.home / ("mutant-" + func)
        shutil.copytree(str(SHIM.parent), str(target))
        (target / "gh").write_text(source[:start] + head[:at] + guard + head[at:])
        return target / "gh"

    def test_mutants_honouring_the_marker_in_count_call_and_the_cache_fail_these_cases(self):   # #1216
        marker = {"FLEET_SWITCHBOARD_OWN_GH": "1"}
        for func in ("count_call", "cached_read"):
            with self.subTest(func=func):
                (self.state / "agent-calls.jsonl").unlink() if (self.state / "agent-calls.jsonl").exists() else None
                shutil.rmtree(str(self.state / "gh-read-cache"), True)
                self.log.write_text("")
                self.run_shim("api", self.PATH, env=marker); self.run_shim("api", self.PATH, env=marker)
                spooled = (self.state / "agent-calls.jsonl").read_text().splitlines() if (self.state / "agent-calls.jsonl").exists() else []
                real = (len(spooled), len(self.network()))
                self.assertEqual(real, (1, 1), "the real shim, marker set: one call counted, the repeat served from the cache")
                (self.state / "agent-calls.jsonl").unlink()
                shutil.rmtree(str(self.state / "gh-read-cache"), True)
                self.log.write_text("")
                mutant = self.marker_mutant(func)
                with mock.patch.dict(globals(), {"SHIM": mutant}), rebound("SHIM", mutant):
                    self.run_shim("api", self.PATH, env=marker); self.run_shim("api", self.PATH, env=marker)
                spooled = (self.state / "agent-calls.jsonl").read_text().splitlines() if (self.state / "agent-calls.jsonl").exists() else []
                self.assertNotEqual((len(spooled), len(self.network())), real, "the mutant differs: the case above fails it")

    def test_s8_a_write_by_the_same_agent_drops_its_entries_and_another_agents_write_does_not(self):
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), 1)
        self.run_shim("api", self.PATH, env={"FLEET_SWITCHBOARD_BIN": str(self.other)})   # the other agent has its own entry (key includes the agent)
        self.assertEqual(len(self.network()), 2)
        self.run_shim("issue", "edit", "5", "-t", "x", env={"FLEET_SWITCHBOARD_BIN": str(self.other)})   # a write by the other agent
        self.run_shim("api", self.PATH)
        self.assertEqual(len([l for l in self.network() if l.startswith("api")]), 2, "the first agent's entry survived the other agent's write")
        self.run_shim("issue", "edit", "5", "-t", "x")                                     # a write by the first agent
        self.run_shim("api", self.PATH)
        self.assertEqual(len([l for l in self.network() if l.startswith("api")]), 3, "its next read goes to GitHub")

    def test_s4_below_15_percent_one_warning_a_cached_read_is_served_and_a_write_is_never_refused(self):
        self.usage(500)
        first = self.run_shim("api", self.PATH)
        self.assertEqual(first.stderr.count("warning"), 1)
        write = self.run_shim("issue", "edit", "5", "-t", "x")
        self.assertEqual((write.returncode, write.stderr.count("warning")), (0, 0))      # one line a minute
        self.age(20)                                                                      # past the write's grace window
        self.run_shim("api", self.PATH)                                                   # the write dropped the entry: this read spends and stores
        before = len(self.network())
        self.age(100)                                                                     # older than the fresh bound, still in the stale bound
        again = self.run_shim("api", self.PATH)
        self.assertEqual((again.stdout, len(self.network())), ('{"n":1}', before), "served from the cache instead of spending")

    def test_s5_s9_far_from_the_limit_output_and_exit_status_are_those_of_the_plain_call(self):
        self.usage(3000)
        for args in (("api", self.PATH), ("issue", "edit", "5", "-t", "x"), ("pr", "view", "3")):
            plain = self.run_shim(*args, env={"FLEET_SHIM_CACHE": "off"})
            now = self.run_shim(*args)
            self.assertEqual((now.stdout, now.stderr, now.returncode), (plain.stdout, plain.stderr, plain.returncode), args)
            self.assertNotIn("warning", now.stderr)

    def test_s10_a_cache_that_cannot_be_written_still_reaches_gh(self):
        (self.state / "gh-read-cache").write_text("not a directory")
        (self.state / "agent-calls.jsonl").mkdir()
        done = self.run_shim("api", self.PATH)
        self.assertEqual((done.returncode, done.stdout, len(self.network())), (0, '{"n":1}', 1))

    def test_edges_an_error_answer_garbage_a_corrupt_entry_and_a_304_with_no_entry_pass_through_and_cache_nothing(self):
        gh = self.bin / "gh"
        good = gh.read_text()
        gh.write_text("#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho \"$@\" >> \"$FAKE_LOG\"\n"
                      "case \"$FAKE_MODE\" in\n 404) printf 'HTTP/2.0 404 Not Found\\r\\n\\r\\n{\"message\":\"Not Found\"}'; echo 'gh: Not Found (HTTP 404)' >&2; exit 1;;\n"
                      " garbage) printf 'not http at all'; exit 0;;\n 304) printf 'HTTP/2.0 304 Not Modified\\r\\n\\r\\n'; exit 0;;\nesac\nexit 0\n")
        for mode, code, out in (("404", 1, '{"message":"Not Found"}'), ("garbage", 0, "not http at all"), ("304", 0, "")):
            done = self.run_shim("api", self.PATH, env={"FAKE_MODE": mode})
            self.assertEqual((done.returncode, done.stdout), (code, out), mode)
            if mode == "404":
                self.assertIn("HTTP 404", done.stderr)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [], mode)
        gh.write_text(good)
        self.run_shim("api", self.PATH)
        cache = self.state / "gh-read-cache"
        for name in os.listdir(cache):
            (cache / name).write_text("{not json")
        before = len(self.network())
        self.assertEqual(self.run_shim("api", self.PATH).stdout, '{"n":1}')
        self.assertEqual(len(self.network()), before + 1, "a corrupt entry is a miss")
        self.age(700)
        before = len(self.network())
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 1, "an entry older than the bound is a miss")

    def test_edges_low_budget_with_no_entry_spends_and_a_stale_reading_is_ignored(self):
        self.usage(100)
        first = self.run_shim("api", self.PATH)
        self.assertEqual((first.stdout, len(self.network())), ('{"n":1}', 1))          # nothing to serve: it spends, never refuses
        (self.state / "github-usage.json").write_text(json.dumps({"v": 1, "at": time.time() - 3600, "accounts": {"acme-a": {"used": 4900, "remaining": 100, "limit": 5000, "reset": time.time()}}}))
        shutil.rmtree(str(self.state / "gh-low-warned"), ignore_errors=True)
        self.age(100)
        again = self.run_shim("api", self.PATH)
        self.assertEqual((again.stderr, len(self.network())), ("", 2), "a reading an hour old is ignored: no warning, and the stale entry is revalidated")

    def test_edges_a_path_that_names_the_working_directory_or_a_host_is_not_cached(self):
        self.run_shim("api", "repos/{owner}/{repo}/issues")
        self.run_shim("api", self.PATH, env={"GH_HOST": "ghe.example"})
        cache = self.state / "gh-read-cache"
        self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [])

    def test_edges_a_failed_output_after_the_request_never_makes_a_second_request(self):
        module = self.shim_module()
        calls = []
        real_run = module.subprocess.run

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'HTTP/2.0 200 OK\r\nEtag: "v1"\r\n\r\n{"n":1}', b"")

        class Broken:
            def isatty(self):
                return False

            @property
            def buffer(self):
                raise BrokenPipeError("closed")

        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        with mock.patch.object(module.subprocess, "run", fake_run), mock.patch.object(module.sys, "stdout", Broken()):
            status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
        self.assertEqual((status, len(calls)), (0, 1))

    def test_m2_paths_that_report_a_state_are_never_cached_in_any_spelling(self):
        forms = ["rate_limit", "/rate_limit", "rate_limit?x=1", "RATE_LIMIT", "rate_limit/", "https://api.github.com/rate_limit", "%72ate_limit", "%2572ate_limit", "repos/../rate_limit",
                 "api/v3/rate_limit", "user", "user/", "applications/abc/token", "installation/repositories", "notifications",
                 "repos/acme/app/commits/abc/check-runs", "repos/acme/app/commits/abc/status", "repos/acme/app/actions/runs/1", "repos/acme/app/actions/jobs/2"]
        for form in forms:
            self.log.write_text("") if self.log.exists() else None
            self.run_shim("api", form)
            self.run_shim("api", form)
            self.assertEqual(len(self.network()), 2, form)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [], form)

    def test_m2_the_table_is_what_the_shim_denies(self):
        module = self.shim_module()
        for word in module.NEVER_CACHED:
            self.assertTrue(module.never_cached("repos/o/n/%s" % word) or module.never_cached(word), word)
            self.assertIsNone(module.cacheable_read(["api", "/%s/" % word.upper()]), word)
        self.assertIsNotNone(module.cacheable_read(["api", "repos/acme/app/issues/5"]))
        self.assertIsNotNone(module.cacheable_read(["api", "repos/user/status/issues/1"]), "an owner or repository NAMED like a state word is still a name")

    def test_m1_a_read_in_flight_across_a_write_by_the_same_agent_never_stores_its_old_answer(self):
        module = self.shim_module()
        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        out = io.BytesIO()

        class Out:
            def isatty(self):
                return False
            buffer = out

        for write_finishes_first in (True, False):
            shutil.rmtree(str(self.state / "gh-read-cache"), ignore_errors=True)

            def slow_read(argv, **kw):
                module.cache_drop("worker")                      # the same agent's write begins while the request is in flight
                if write_finishes_first:
                    module.cache_mark("worker")                  # and ends before the read does
                return subprocess.CompletedProcess(argv, 0, b'HTTP/2.0 200 OK\r\nEtag: "v1"\r\n\r\n{"old":1}', b"")

            with mock.patch.object(module.subprocess, "run", slow_read), mock.patch.object(module.sys, "stdout", Out()):
                module.cache_directory(create=True)
                os.utime(str(self.state / "gh-read-cache"))
                status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
            self.assertEqual(status, 0)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in os.listdir(cache) if n.endswith(".json")], [], "the pre-write answer was not stored (write finishing first: %s)" % write_finishes_first)

    def test_m1_a_read_made_inside_the_grace_window_after_a_write_goes_to_github_and_stores_nothing(self):
        self.run_shim("api", self.PATH)
        self.run_shim("issue", "edit", "5", "-t", "x")
        before = len(self.network())
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 2)
        self.age(20)
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 3, "after the window the cache works again")

    def test_cross_calls_two_accounts_do_not_share_and_a_paginated_read_is_not_served_from_the_cache(self):
        self.run_shim("api", self.PATH)
        self.users({"acme/app": "acme-b"})
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), 2, "another account is another key")
        self.run_shim("api", self.PATH, "--paginate")
        self.assertEqual(len(self.network()), 3, "a call of another shape is never answered from the cache")

    def overlapped(self, module, entry_age, answer, write_between=True):
        """Run cached_read for a path with an entry of `entry_age` seconds, while a write by the same agent happens during the request. Returns (status, files left)."""
        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        shutil.rmtree(str(self.state / "gh-read-cache"), ignore_errors=True)
        directory = module.cache_directory(create=True)
        file = module.cache_file(directory, "worker", "acme-a", self.PATH)
        module.cache_store(directory, file, '"v1"', '{"old":1}', time.time() - entry_age)
        out = io.BytesIO()

        class Out:
            def isatty(self):
                return False
            buffer = out

        def request(argv, **kw):
            if write_between:
                module.cache_drop("worker")   # the same agent writes while the request is in flight
            return subprocess.CompletedProcess(argv, 0, answer, b"")

        with mock.patch.object(module.subprocess, "run", request), mock.patch.object(module.sys, "stdout", Out()):
            status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
        return status, [n for n in os.listdir(directory) if n.endswith(".json")], out.getvalue()

    def test_every_store_of_the_cache_is_guarded_by_the_write_mark_and_each_is_pinned(self):
        """Completeness: the shim's cache is written in exactly these places; a new one must be added here with an overlapped test."""
        source = SHIM.read_text()
        self.assertEqual(source.count("cache_store("), 3, "one definition and two call sites (the 304 refresh and the 2xx store): pin any new one below")
        self.assertEqual(source.count("not raced"), 2, "both call sites are behind the write mark")
        module = self.shim_module()
        # the 304 refresh: an old entry is revalidated while the same agent writes: nothing is rewritten, and the next read goes to GitHub
        status, files, out = self.overlapped(module, 40, b"HTTP/2.0 304 Not Modified\r\n\r\n")
        self.assertEqual((status, files), (0, []))
        # the 2xx store: same overlap
        status, files, out = self.overlapped(module, 40, b'HTTP/2.0 200 OK\r\nEtag: "v2"\r\n\r\n{"new":1}')
        self.assertEqual((status, files, out), (0, [], b'{"new":1}'))
        # and with no write in between both are stored (the guard is not a blanket refusal)
        status, files, out = self.overlapped(module, 40, b"HTTP/2.0 304 Not Modified\r\n\r\n", write_between=False)
        self.assertEqual((status, len(files), out), (0, 1, b'{"old":1}'))
        # the drop itself removes only this agent's entries
        directory = module.cache_directory()
        other = module.cache_file(directory, "someone", "acme-a", self.PATH)
        module.cache_store(directory, other, None, "{}", time.time())
        module.cache_drop("worker")
        self.assertTrue(os.path.exists(other))

    def test_m2_double_percent_encoding_is_read_as_what_it_decodes_to(self):
        module = self.shim_module()
        self.assertEqual(module.normal_api_path("%2572ate_limit"), ["rate_limit"])
        self.assertTrue(module.never_cached("%2572ate_limit"))

    def test_the_cache_is_private_and_holds_no_token(self):
        self.run_shim("api", self.PATH, env={"GH_TOKEN": "secrettoken"})
        cache = self.state / "gh-read-cache"
        self.assertEqual(stat.S_IMODE(os.stat(str(cache)).st_mode), 0o700)
        for name in os.listdir(cache):
            self.assertEqual(stat.S_IMODE(os.stat(str(cache / name)).st_mode), 0o600)
            self.assertNotIn("acme", name)
            for leak in ("secrettoken", "faketoken", "acme-a"):
                self.assertNotIn(leak, (cache / name).read_text())

    def test_only_the_plain_get_shape_is_cached_and_the_cache_is_bounded(self):
        self.run_shim("api", self.PATH, "--jq", ".n")
        self.run_shim("api", self.PATH, "-X", "GET")
        self.run_shim("pr", "view", "3")
        self.assertFalse((self.state / "gh-read-cache").exists() and os.listdir(self.state / "gh-read-cache"))
        module = TestAgentShellCalls.shim_module(self)
        directory = self.state / "gh-read-cache"
        directory.mkdir(exist_ok=True)
        module.CACHE_FILES_MAX = 10
        for i in range(15):
            module.cache_store(str(directory), str(directory / ("a%02d.json" % i)), None, "{}", time.time())
        self.assertLessEqual(len([n for n in os.listdir(directory) if n.endswith(".json")]), 10)
        old = directory / "old.json"
        old.write_text("{}")
        os.utime(str(old), (time.time() - 3600, time.time() - 3600))
        module.cache_store(str(directory), str(directory / "new.json"), None, "{}", time.time())
        self.assertFalse(old.exists(), "an entry older than the age bound is removed")


class TestAskIssueSubject(unittest.TestCase):
    """#1130: `prove`, `verdict` and `gates` accept an ASK ISSUE as the subject, bound to the hash of the plan comment on it. Fakes only."""

    REPO, ASK, PR = "acme-org/widget-service", "acme-org/widget-service#1130", "acme-org/widget-service#9"
    HEAD = "d" * 40
    SCEN = [{"id": "S1", "evaluator": "reviewer", "proof": "test", "claim": "pr"}, {"id": "S2", "evaluator": "reviewer", "proof": "test", "claim": "pr"},
            {"id": "S3", "evaluator": "reviewer", "proof": "test", "claim": "pr"}]

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = Path(self.dir)
        self.hash = "sha256:" + "1" * 64
        self.has_plan = True
        self.pr_readable = False
        self.caller = "coder1"
        self.pages = {"repos/%s/issues/1130/comments?per_page=100&page=1" % self.REPO: [{"id": 5, "created_at": "2026-01-01", "body": "## Plan (scenarios): x\n"}]}
        self.issue = {"user": {"login": "human"}, "body": "## Intent\nreport it\nDone when: reported\n", "state": "open", "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/1000",
                      "labels": []}
        self.reviewers = ["rev1"]

    def api(self, path, repo=None):
        if path == "repos/%s/issues/1130" % self.REPO:
            return self.issue
        if path.startswith("repos/other-org/gadget/issues/7"):
            return dict(self.issue) if "/comments" not in path else []
        if path == "repos/%s/issues/9" % self.REPO:
            return {"pull_request": {"url": "x"}, "user": {"login": "a"}}
        if "/comments" in path and not self.has_plan:
            return []
        if path in self.pages:
            return self.pages[path]
        raise sb.IntentError("not_found", path)

    def plan(self):
        return {"result": "pass", "defects": [], "scenarios_parsed": self.SCEN, "plan_hash": getattr(self, "stale_hash", None) or self.hash, "text": "", "source": "comment 5"}

    def now(self):
        return 1_000_000

    def entries(self):
        return sb.plan_ledger.read_ledger(self.state)[0]

    def run_cmd(self, command, subject=None, **kw):
        """(exit or None, refusal text or None, stdout, stderr)."""
        args = types.SimpleNamespace(pr=subject or self.ASK, plan=None, ask=None, head=None, scenario=["S1"], all_scenarios=False, evaluator="reviewer",
                                     result="pass", evidence="ran it", command=None, json=False, rest=[], all_gates=False)
        for k, v in kw.items():
            setattr(args, k, v)
        engine = types.SimpleNamespace(intents=types.SimpleNamespace(_api=self.api), audit=None, actor="t", herdr=None, v2=None,
                                       discover=lambda now: types.SimpleNamespace(agents={}), now=lambda: 1.0)
        config = {"intent_repo": self.REPO, "plan_evaluators": {"reviewer": self.reviewers}}
        picture = ({"head": self.HEAD, "ci": "green"}, "PLAN: %s" % self.ASK, [], None) if self.pr_readable and subject == self.PR else (None, "", [], "no pull request found")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sb, "build_engine", lambda *a, **k: (engine, self.state, config)), mock.patch.object(sb, "pr_picture", lambda *a: picture), \
                mock.patch.object(sb, "identify_caller", lambda *a: self.caller), mock.patch.object(sb, "checked_plan", lambda *a, **k: self.plan()), \
                mock.patch.object(sb, "launched_testers_for", lambda c: ({}, {}, {})), mock.patch.object(sb, "read_open_overlap", lambda *a: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = getattr(sb, command)(args, None, self.now)
            except sb.LedgerCommandError as exc:
                return None, str(exc), out.getvalue(), err.getvalue()
        text = err.getvalue()
        if code == 2 and ("not recorded:" in text or "refused:" in text) and command != "cmd_gates_ask_close":
            # the wrapper turns a refusal into a final line on stderr and exit 2: same refusal, same shape as a raised one
            return None, text.strip(), out.getvalue(), text
        return code, None, out.getvalue(), err.getvalue()

    def prove(self, sid, **kw):
        self.caller = "coder1"
        return self.run_cmd("cmd_prove", scenario=[sid], **kw)

    def judge(self, sid, **kw):
        self.caller = "rev1"
        return self.run_cmd("cmd_verdict", scenario=sid, **kw)

    def record_all(self, ids=("S1", "S2", "S3")):
        for sid in ids:
            self.assertEqual(self.prove(sid)[0], sb.EXIT_OK)
            self.assertEqual(self.judge(sid)[0], sb.EXIT_OK, self.judge(sid))

    def test_s2_a_proof_and_a_verdict_on_an_issue_name_the_issue_and_the_plan_hash(self):
        self.assertEqual(self.prove("S1")[0], sb.EXIT_OK)
        self.assertEqual(self.judge("S1")[0], sb.EXIT_OK)
        rows = self.entries()
        self.assertEqual([(e["kind"], e["pr"], e["plan_hash"], e["head"]) for e in rows],
                         [("proof", self.ASK, self.hash, "1" * 40), ("verdict", self.ASK, self.hash, "1" * 40)])

    def test_s3_an_issue_with_no_plan_comment_is_refused_by_name_and_a_revised_plan_voids_the_record(self):
        self.has_plan = False
        for command in ("prove", "judge"):
            code, why, _, _ = getattr(self, command)("S1")
            self.assertIsNone(code)
            self.assertIn("has no plan comment", why)
        self.assertEqual(self.entries(), [])
        self.has_plan = True
        self.record_all(("S1",))
        self.hash = "sha256:" + "2" * 64
        out = self.run_cmd("cmd_gates")[2]
        self.assertIn("proof #1 void: plan changed", out)

    def test_s4_gates_on_the_issue_shows_the_scenarios_proven_and_judged(self):
        self.record_all()
        out = self.run_cmd("cmd_gates", json=True)[2]
        g1 = next(g for g in json.loads(out)["gates"] if g["gate"] == "G1")
        self.assertEqual(g1["status"], "ok")
        self.assertIn("3 of 3 claimed scenarios proven and judged", g1["detail"])

    def test_s5_the_closing_command_names_what_was_recorded_or_refuses_naming_the_unjudged_scenario(self):
        self.enterContext(mock.patch.object(sb, "unclaimed_verdict", lambda *a, **k: None))
        self.record_all(("S1", "S2"))
        self.assertEqual(self.prove("S3")[0], sb.EXIT_OK)
        code, why, out, err = self.run_cmd("cmd_gates_ask_close")
        self.assertEqual(code, sb.EXIT_ERROR)
        self.assertIn("not judged: S3", err)
        self.assertEqual(out, "")
        self.assertEqual(self.judge("S3")[0], sb.EXIT_OK)
        code, why, out, err = self.run_cmd("cmd_gates_ask_close")
        self.assertEqual(code, sb.EXIT_OK)
        self.assertIn("S1, S2, S3", out)

    def test_s6_mutant_that_records_on_a_pull_request_subject_only_fails(self):
        with mock.patch.object(sb, "ask_subject", lambda *a, **k: None):
            self.assertIsNone(self.prove("S1")[0])
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.prove("S1")[0], sb.EXIT_OK)    # the real build records

    def test_s6_mutant_that_ignores_the_hash_keeps_a_record_valid_after_a_revised_plan(self):
        self.record_all(("S1",))
        self.hash = "sha256:" + "2" * 64        # the plan was revised; the mutant keeps binding to the hash it first read
        self.stale_hash = "sha256:" + "1" * 64
        out = self.run_cmd("cmd_gates")[2]
        self.assertNotIn("void", out)       # the mutant leaves the old proof valid: the void test above fails on it

    def test_s6_mutant_that_accepts_an_issue_with_no_plan_records_one(self):
        self.has_plan = False
        with mock.patch.object(sb, "ask_plan_comment", lambda *a: "x"):
            self.assertEqual(self.prove("S1")[0], sb.EXIT_OK)
        self.assertEqual(len(self.entries()), 1)    # the refusal test above fails on it

    def test_a_second_plan_looking_comment_refuses_prove_and_verdict_naming_both(self):
        self.pages = {"repos/%s/issues/1130/comments?per_page=100&page=1" % self.REPO: [
            {"id": 5, "created_at": "2026-01-01", "body": "## Plan (scenarios): x\n"}, {"id": 8, "created_at": "2026-01-02", "body": "## Plan (scenarios): y\n"}]}
        for command in ("prove", "judge"):
            code, why, _, _ = getattr(self, command)("S1")
            self.assertIsNone(code)
            self.assertIn("comment 8", why)
            self.assertIn("comment 5", why)
        self.assertEqual(self.entries(), [])

    def test_s6_mutant_ask_path_that_skips_the_second_plan_rule_records(self):
        self.pages = {"repos/%s/issues/1130/comments?per_page=100&page=1" % self.REPO: [
            {"id": 5, "created_at": "2026-01-01", "body": "## Plan (scenarios): x\n"}, {"id": 8, "created_at": "2026-01-02", "body": "## Plan (scenarios): y\n"}]}
        with mock.patch.object(sb, "second_plan_defects", lambda d: []):
            self.assertEqual(self.prove("S1")[0], sb.EXIT_OK)    # the refusal test above fails on it

    def refused_everywhere(self, needle):
        for command in ("prove", "judge"):
            code, why, _, _ = getattr(self, command)("S1")
            self.assertIsNone(code)
            self.assertIn(needle, why)
        code, why, _, _ = self.run_cmd("cmd_gates_ask_close")
        self.assertIsNone(code)
        self.assertIn(needle, why)
        self.assertEqual(self.entries(), [])

    def test_m1_a_closed_issue_is_refused(self):
        self.issue["state"] = "closed"
        self.refused_everywhere("is closed")

    def test_m1_an_issue_with_no_done_when_is_refused(self):
        self.issue["body"] = "## Intent\nreport it\n"
        self.refused_everywhere("no `## Intent`")
        self.issue["body"] = "just text"
        self.refused_everywhere("no `## Intent`")

    def test_m1_a_charter_is_refused_by_label_and_by_having_no_parent(self):
        self.issue["labels"] = [{"name": "someone:orchestrator"}]
        self.refused_everywhere("a charter (it carries")
        self.issue["labels"] = []
        del self.issue["parent_issue_url"]
        self.refused_everywhere("no parent issue")

    def test_m1_mutants_that_accept_each_case_record_one(self):
        for change, key in (({"state": "closed"}, "state"), ({"body": "## Intent\nx\n"}, "body"), ({"labels": [{"name": "u:orchestrator"}]}, "labels")):
            self.issue.update(change)
            with mock.patch.object(sb, "ask_refusal", lambda *a: None):
                self.assertEqual(self.prove("S1")[0], sb.EXIT_OK, key)
            self.assertEqual(len(self.entries()), 1)    # the refusal tests above fail on it
            self.setUp()

    def test_m2_the_author_of_the_ask_may_not_record_the_verdict_but_may_prove(self):
        self.reviewers = ["rev1", "human"]
        self.caller = "human"
        self.assertEqual(self.run_cmd("cmd_prove", scenario=["S1"])[0], sb.EXIT_OK)       # the author's step
        self.caller = "human"
        code, why, _, _ = self.run_cmd("cmd_verdict", scenario="S1")
        self.assertIsNone(code)
        self.assertIn("author of the ask", why)
        self.assertEqual([e["kind"] for e in self.entries()], ["proof"])

    def test_m2_the_author_alone_is_a_known_writer_so_another_agent_may_judge(self):
        self.assertEqual(self.judge("S1")[0], sb.EXIT_OK)    # no proof recorded yet: the ask's author is the known writer

    def test_m2_mutants_that_drop_the_author_fail_these_tests(self):
        real = sb.plan_ledger.known_writers
        with mock.patch.object(sb.plan_ledger, "known_writers", lambda entries, pr, senders=(): bool(real(entries, pr, ()))):
            code, why, _, _ = self.judge("S1")
        self.assertIsNone(code)          # the mutant refuses: "the author alone is a known writer" fails on it
        self.assertIn("no writer", why)
        self.reviewers = ["rev1", "human"]
        self.prove("S1")
        orig = sb.item_issue
        def no_author(*a):
            got = orig(*a)
            return dict(got, author=None)
        self.caller = "human"
        with mock.patch.object(sb, "item_issue", no_author):
            code, why = self.run_cmd("cmd_verdict", scenario="S1")[:2]
        self.assertEqual(code, sb.EXIT_OK, why)   # the mutant lets the author judge: the refusal test above fails on it

    def test_p1_every_subject_form_resolves_to_the_issue_or_is_refused_by_name(self):
        for form in ("acme-org/widget-service#1130",):
            self.assertEqual(self.prove("S1", subject=form)[0], sb.EXIT_OK)
        for form in ("1130", "#1130", "https://github.com/acme-org/widget-service/issues/1130"):
            for command in ("prove", "judge"):
                code, why, _, _ = getattr(self, command)("S1", subject=form)
                self.assertIsNone(code, form)
                self.assertTrue(why, form)
        self.assertEqual({e["pr"] for e in self.entries()}, {self.ASK})
        code, why, _, _ = self.prove("S1", subject="https://github.com/acme-org/widget-service/pull/9")
        self.assertNotIn("ask", (why or "").split("refused")[0])    # a pull request URL stays a pull request subject

    def test_p2_another_repositorys_issue_is_resolved_or_refused_by_name(self):
        code, why, _, _ = self.prove("S1", subject="other-org/gadget#7")
        self.assertTrue(code == sb.EXIT_OK or why)
        self.assertTrue(all(e["pr"] == "other-org/gadget#7" for e in self.entries()))

    def test_s7_the_docs_state_the_issue_subject_the_hash_binding_and_the_closing_rule(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        section = text.split("## Ask issue as the subject", 1)[1].split("\n## ", 1)[0]
        for needle in ("plan comment", "plan hash", "gates ask-close"):
            self.assertIn(needle, section)

    def test_u1_a_pull_request_subject_behaves_as_before(self):
        self.pr_readable = True
        self.hash = "sha256:" + "1" * 64
        results = []
        self.caller = "coder1"
        results.append(self.run_cmd("cmd_prove", subject=self.PR)[:2])
        self.caller = "rev1"
        results.append(self.run_cmd("cmd_verdict", subject=self.PR, scenario="S1")[:2])      # a verdict on a PR with a known writer
        self.caller = "coder1"
        results.append(self.run_cmd("cmd_verdict", subject=self.PR, scenario="S1")[:2])       # the writer judging its own work
        results.append(self.run_cmd("cmd_prove", subject=self.PR, scenario=["S9"])[:2])      # an unknown scenario
        self.assertEqual(results[0][0], sb.EXIT_OK)
        self.assertEqual(results[1][0], sb.EXIT_OK)
        self.assertIn("refused", results[2][1])
        self.assertEqual(results[3][0], sb.EXIT_PARTIAL)
        self.assertEqual({e["head"] for e in self.entries()}, {self.HEAD})      # a pull request's record still names the head

    def test_u2_a_revised_plan_voids_the_old_record_and_a_new_record_counts(self):
        self.record_all(("S1",))
        self.hash = "sha256:" + "2" * 64
        self.assertEqual(self.prove("S1")[0], sb.EXIT_OK)
        report = json.loads(self.run_cmd("cmd_gates", json=True)[2])
        s1 = next(g for g in report["gates"] if g["gate"] == "G1")
        self.assertIn("S1", " ".join(s1["missing"]))
        proofs = [e for e in self.entries() if e["kind"] == "proof"]
        self.assertEqual([e["plan_hash"] for e in proofs], ["sha256:" + "1" * 64, "sha256:" + "2" * 64])

    def test_u3_no_refusal_writes_a_row(self):
        self.has_plan = False
        self.prove("S1"), self.judge("S1")
        self.has_plan = True
        self.prove("S9")                                   # an unknown scenario
        self.run_cmd("cmd_prove", scenario=["S1"], plan="acme-org/widget-service#5")   # another plan
        self.assertEqual(self.entries(), [])

    def same_number(self, as_pull_request):
        """Issue 9 and pull request 9 of one repository are ONE number: the read of `issues/9` answers as the issue or as the pull request (as GitHub does)."""
        plain = self.api
        self.nine = {"user": {"login": "human"}, "body": "## Intent\nreport it\nDone when: reported\n", "state": "open", "labels": [],
                     "parent_issue_url": "https://api.github.com/repos/acme-org/widget-service/issues/1000"}
        self.pages["repos/%s/issues/9/comments?per_page=100&page=1" % self.REPO] = [{"id": 6, "created_at": "2026-01-01", "body": "## Plan (scenarios): x\n"}]

        def api(path, repo=None):
            if path == "repos/%s/issues/9" % self.REPO:
                return dict(self.nine, pull_request={"url": "x"}) if as_pull_request[0] else self.nine
            return plain(path, repo)
        self.api = api

    def test_x1_a_pull_request_of_the_number_of_an_issue_is_refused_by_name_and_writes_nothing(self):
        self.same_number([True])
        code, why, out, err = self.prove("S1", subject=self.PR)          # as an ask: it is no ask, so no record binds to the plan comment of the number
        self.assertIsNone(code)
        self.assertNotIn("recorded #", out + err)
        self.assertEqual(self.entries(), [])
        code, why, out, err = self.run_cmd("cmd_item_prove", item=self.PR)    # as a work item: refused by name
        self.assertIsNone(code)
        self.assertEqual((why or "").count("is a pull request"), 1, why)
        self.assertEqual(self.entries(), [])

    def test_x2_a_verdict_on_issue_9_is_not_seen_by_pull_request_9(self):
        mode = [False]
        self.same_number(mode)
        self.assertEqual(self.prove("S1", subject=self.PR)[0], sb.EXIT_OK)
        self.assertEqual(self.judge("S1", subject=self.PR)[0], sb.EXIT_OK)
        self.assertEqual(len([e for e in self.entries() if e["kind"] == "verdict"]), 1)    # the issue sees 1
        mode[0] = True
        self.pr_readable = True
        report = json.loads(self.run_cmd("cmd_gates", subject=self.PR, json=True)[2])
        self.assertEqual(report["head"] if "head" in report else self.HEAD, self.HEAD)
        seen = [e for e in self.entries() if e["kind"] == "verdict" and e["head"] == self.HEAD]
        self.assertEqual(len(seen), 0)                                                       # the pull request sees 0
        g1 = next(g for g in report["gates"] if g["gate"] == "G1")
        self.assertIn("S1", " ".join(g1["missing"]))

    def test_x3_mutant_that_removes_the_pull_request_refusal_records_on_it(self):
        self.same_number([True])
        real = sb.item_issue

        def mutant(engine, repo, number):
            data = engine.intents._api("repos/%s/issues/%d" % (repo, number), repo)
            return real(types.SimpleNamespace(intents=types.SimpleNamespace(_api=lambda *a: {k: v for k, v in data.items() if k != "pull_request"})), repo, number)
        with mock.patch.object(sb, "item_issue", mutant):
            self.assertEqual(self.prove("S1", subject=self.PR)[0], sb.EXIT_OK)
        self.assertEqual(len(self.entries()), 1)       # test_x1 fails on it: it wants a refusal and 0 rows

    def test_x3_mutant_that_reads_an_issues_record_as_the_pull_requests_sees_one(self):
        mode = [False]
        self.same_number(mode)
        self.prove("S1", subject=self.PR), self.judge("S1", subject=self.PR)
        mode[0] = True
        self.pr_readable = True
        self.HEAD = sb.plan_ledger.ask_head(self.hash)     # the mutant: the pull request's head IS the issue's plan head, so the issue's record is read as its own
        report = json.loads(self.run_cmd("cmd_gates", subject=self.PR, json=True)[2])
        g1 = next(g for g in report["gates"] if g["gate"] == "G1")
        self.assertNotIn("S1", " ".join(g1["missing"]))   # test_x2 fails on it: it wants S1 missing


class TestClosedUnmergedAskClose(unittest.TestCase):
    """#1347: `gates ask-close` through the real command with fakes: the refusal, the baseline, the bound and the waiver. Borrows the fakes of TestAskIssueSubject (its own tests are not repeated)."""

    REPO, ASK, PR, HEAD, SCEN = (TestAskIssueSubject.REPO, TestAskIssueSubject.ASK, TestAskIssueSubject.PR, TestAskIssueSubject.HEAD, TestAskIssueSubject.SCEN)
    api, plan, now, entries, run_cmd, prove, judge, record_all = (TestAskIssueSubject.api, TestAskIssueSubject.plan, TestAskIssueSubject.now, TestAskIssueSubject.entries,
                                                                  TestAskIssueSubject.run_cmd, TestAskIssueSubject.prove, TestAskIssueSubject.judge, TestAskIssueSubject.record_all)

    def setUp(self):
        TestAskIssueSubject.setUp(self)
        self.found = []
        self.too_many = False
        self.broken = None
        self.seen_pages = []

    def merged(self, number, claim):
        sb.plan_ledger.append_checked(self.state, self.now(), lambda have, seq: {"kind": "claim", "pr": "acme-org/widget-service#%d" % number, "head": "a" * 40, "claim_text": claim})
        return dict(number=number, state="closed", merged_at="1970-01-12T14:06:40Z" if False else _dt.datetime.fromtimestamp(self.now() + 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    body="PLAN: %s\nCLAIMS: %s" % (self.ASK, claim), pull_request={"merged_at": _dt.datetime.fromtimestamp(self.now() + 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def closed(self, number, claim):
        return dict(number=number, state="closed", merged_at=None, body="PLAN: %s\nCLAIMS: %s" % (self.ASK, claim), pull_request={"merged_at": None})

    def ask_close(self):
        test = self
        class Reader:
            def __init__(self, *a, **k):
                pass
            def _api(self, path, repo=None):
                page = int(re.search(r"&page=(\d+)", path).group(1))
                test.seen_pages.append(page)
                if test.broken == "list error":
                    raise RuntimeError("the search failed")
                if test.broken == "bad shape":
                    return {"items": "not a list"}
                items = [dict(number=3000 + n, state="open", body="no plan", pull_request={"merged_at": None}) for n in range(300)] if test.too_many else test.found
                return {"total_count": len(items), "items": items[(page - 1) * 100:page * 100]}
        real = sb._ledger_context
        def ctx(*a, **k):
            got = real(*a, **k)
            got.config = dict(got.config, gh="gh", call_timeout_seconds=5)
            return got
        with mock.patch.object(sb, "IntentReader", Reader), mock.patch.object(sb, "GhAccounts", lambda *a, **k: None), mock.patch.object(sb, "_ledger_context", ctx):
            return self.run_cmd("cmd_gates_ask_close")

    def waiver(self, head):
        sb.plan_ledger.append_checked(self.state, self.now(), lambda have, seq: {"kind": "waiver", "pr": self.ASK, "head": head, "gates": ["CU"], "expires": self.now() + 86400, "by": "cos", "result": "pass", "reason": "ship"})

    def test_s5_the_refusal_names_the_unclaimed_scenarios_and_the_closed_pull_request(self):
        self.record_all()
        self.found = [self.merged(11, "S1"), self.closed(12, "S2 S3")]
        code, why, out, err = self.ask_close()
        self.assertEqual(code, sb.EXIT_ERROR)
        self.assertIn("S2, S3 unclaimed by any merged pull request", err)
        self.assertIn("#12", err)
        self.assertEqual(out, "")

    def test_s5_a_mutant_without_the_refusal_is_caught(self):
        with mock.patch.object(sb, "unclaimed_verdict", lambda *a, **k: None):
            with self.assertRaises(AssertionError):
                self.test_s5_the_refusal_names_the_unclaimed_scenarios_and_the_closed_pull_request()

    def baseline(self):
        for claims in (None, "S1 S2 S3"):
            self.found = [self.merged(21, claims or ""), self.merged(22, "S2 S3")] if claims else []
            plain = self.ask_close()
            self.found = self.found + [self.closed(23, "S1")] if claims else [self.closed(23, "")]
            self.assertEqual(self.ask_close(), plain)      # the closed pull request changes nothing
            self.assertNotIn("unclaimed by any merged", plain[3])
            self.assertEqual(plain[0], sb.EXIT_OK)

    def test_s6_u4_ask_close_behaves_as_before_for_no_claim_lines_and_full_coverage(self):
        self.record_all()
        self.baseline()

    def test_s6_u4_a_mutant_that_flags_every_ask_with_a_closed_pull_request_is_caught(self):
        real = sb.unclaimed_verdict
        def mutant(args, ctx, runner, now, body=None):
            got = real(args, ctx, runner, now, body)
            closed = []
            if got is None:
                sb.plan_peers(ctx, runner, now, closed)
                if closed:
                    return ("uncovered scenarios: S3", None, ["S3"], [n for n, _ in closed])
            return got
        with mock.patch.object(sb, "unclaimed_verdict", mutant):
            with self.assertRaises(AssertionError):
                self.test_s6_u4_ask_close_behaves_as_before_for_no_claim_lines_and_full_coverage()

    def bound_and_waiver(self):
        self.record_all()
        self.too_many = True
        code, why, out, err = self.ask_close()
        self.assertEqual(code, sb.EXIT_ERROR)
        self.assertIn("too many pull requests to judge", err)
        self.assertIn("--gates CU", err)
        self.assertLessEqual(max(self.seen_pages), sb.PEER_PAGES)
        self.waiver("e" * 40)       # another head
        self.assertEqual(self.ask_close()[0], sb.EXIT_ERROR)
        self.waiver(sb.plan_ledger.ask_head(self.hash))
        code, why, out, err = self.ask_close()
        self.assertEqual(code, sb.EXIT_OK, err)

    def test_the_bound_refuses_and_only_a_waiver_for_the_head_passes(self):
        self.bound_and_waiver()

    def test_the_bound_and_waiver_mutants_are_caught(self):
        with mock.patch.object(sb, "_cu_waived", lambda *a, **k: False):
            with self.assertRaises(AssertionError):
                self.bound_and_waiver()

    def failed_read(self, kind):
        self.record_all()
        if kind == "too many pull requests":
            self.too_many = True
        elif kind == "unknown sibling":   # merged, and no claim line was ever recorded for it
            self.found = [dict(number=31, state="closed", merged_at="2026-01-01T00:00:00Z", body="PLAN: %s\nCLAIMS: S1" % self.ASK, pull_request={"merged_at": "2026-01-01T00:00:00Z"})]
        else:
            self.broken = kind

    KINDS = ("too many pull requests", "list error", "unknown sibling", "bad shape")

    def every_failed_read_refuses_and_the_waiver_passes(self):
        for kind in self.KINDS:
            self.setUp()
            self.failed_read(kind)
            code, why, out, err = self.ask_close()
            self.assertEqual(code, sb.EXIT_ERROR, (kind, err))
            self.assertIn("list error" if kind == "bad shape" else kind, err)   # a bad shape is a list error of the one rule
            self.assertIn("--gates CU", err)
            self.assertEqual(out, "")
            self.waiver("e" * 40)      # another head: still refused
            self.assertEqual(self.ask_close()[0], sb.EXIT_ERROR, kind)
            self.waiver(sb.plan_ledger.ask_head(self.hash))
            self.assertEqual(self.ask_close()[0], sb.EXIT_OK, (kind, self.ask_close()[3]))

    def test_s10_a_failed_read_of_each_kind_refuses_the_close_and_only_the_head_bound_waiver_passes(self):
        self.every_failed_read_refuses_and_the_waiver_passes()

    def test_s10_a_mutant_that_fails_open_on_a_failed_read_is_caught(self):
        real = sb.unclaimed_verdict
        def open_again(args, ctx, runner, now, body=None):
            got = real(args, ctx, runner, now, body)
            return got if got is None or got[2] or got[0] == "too many pull requests" else None   # the old behaviour: only the bound refused
        with mock.patch.object(sb, "unclaimed_verdict", open_again):
            with self.assertRaises(AssertionError):
                self.every_failed_read_refuses_and_the_waiver_passes()

    def test_a_plain_pull_request_and_a_non_ask_issue_never_raise(self):
        self.pr_readable = True
        for command in ("cmd_gates", "cmd_gates_ask_close"):
            for subject in (self.PR, None):
                self.run_cmd(command, subject=subject)
        self.issue["body"] = "just text"
        for command in ("cmd_gates", "cmd_gates_ask_close"):
            self.assertTrue(self.run_cmd(command) is not None)
        self.assertIn("not an ask issue", self.run_cmd("cmd_gates_ask_close", subject=self.PR)[1] or "")


class TestAskWaive(unittest.TestCase):
    """#1347 S14, S15: `waive` for an ASK issue through the real `cmd_waive`; the waiver is bound to the plan hash and clears the refusal of `gates ask-close`."""

    T = TestClosedUnmergedAskClose
    REPO, ASK, PR = T.REPO, T.ASK, T.PR
    api, plan, now, entries, run_cmd, prove, judge, record_all = (TestAskIssueSubject.api, TestAskIssueSubject.plan, TestAskIssueSubject.now, TestAskIssueSubject.entries,
                                                                  TestAskIssueSubject.run_cmd, TestAskIssueSubject.prove, TestAskIssueSubject.judge, TestAskIssueSubject.record_all)
    SCEN, HEAD = TestAskIssueSubject.SCEN, TestAskIssueSubject.HEAD
    ask_close = T.ask_close
    failed_read = T.failed_read
    REASON = "the pull requests of the ask cannot be read: reviewed by hand"

    def setUp(self):
        TestAskIssueSubject.setUp(self)
        self.found, self.too_many, self.broken, self.seen_pages = [], False, None, []
        self.kind = ("agent", "cos", "chief-of-staff")

    def waive(self, subject=None, gates="CU", reason=None):
        test = self
        class Reader:
            def __init__(self, *a, **k):
                pass
            def _api(self, path, repo=None):
                if test.broken:
                    raise RuntimeError("the search failed")
                return {"total_count": 0, "items": []}
        real = sb._ledger_context
        def ctx(*a, **k):
            got = real(*a, **k)
            got.config = dict(got.config, gh="gh", call_timeout_seconds=5)
            return got
        with mock.patch.object(sb, "gates_caller_kind", lambda *a: self.kind), mock.patch.object(sb, "IntentReader", Reader), mock.patch.object(sb, "GhAccounts", lambda *a, **k: None), \
                mock.patch.object(sb, "_ledger_context", ctx):
            return self.run_cmd("cmd_waive", subject=subject, gates=gates, reason=self.REASON if reason is None else reason)

    def waivers(self):
        return [e for e in self.entries() if e["kind"] == "waiver"]

    def refused_ask(self):
        self.record_all()
        self.failed_read("list error")
        self.assertEqual(self.ask_close()[0], sb.EXIT_ERROR)

    def happy(self):
        self.refused_ask()
        code, why, out, err = self.waive()
        self.assertEqual(code, sb.EXIT_OK, (why, err))
        (row,) = self.waivers()
        self.assertEqual((row["pr"], row["head"], row["gates"], row["by"]), (self.ASK, sb.plan_ledger.ask_head(self.hash), ["CU"], "cos"))
        self.assertEqual(self.ask_close()[0], sb.EXIT_OK)
        self.hash = "sha256:" + "2" * 64      # the plan is revised: the waiver is void
        self.record_all()                     # every scenario is proven and judged again at the new hash, so only the failed read can refuse
        self.assertEqual(self.ask_close()[0], sb.EXIT_ERROR)

    def test_s14_the_chief_of_staff_waives_the_ask_and_a_revised_plan_voids_it(self):
        self.happy()      # the issue author (a shared login) is not the chief of staff's fleet name: not a writer by accident

    def test_s14_a_writer_of_the_ask_and_a_plain_agent_cannot_waive(self):
        self.refused_ask()
        self.kind = ("agent", "coder1", "chief-of-staff")      # coder1 recorded the proofs of the ask
        self.assertIn("a writer cannot waive", self.waive()[1] or "")
        self.kind = ("agent", "coder2", "coder")
        self.assertIn("may not waive", self.waive()[1])
        self.assertEqual(self.waivers(), [])
        self.assertEqual(self.ask_close()[0], sb.EXIT_ERROR)

    def test_s15_gate_other_than_cu_short_reason_and_work_item_are_refused(self):
        self.refused_ask()
        self.assertIn("only CU", self.waive(gates="G1")[1])
        self.assertIn("at least", self.waive(reason="short")[1])
        with mock.patch.object(sb, "item_entry_refusal", lambda *a, **k: "%s is a work item" % self.ASK):
            self.assertIn("work item", self.waive()[1])
        self.assertEqual(self.waivers(), [])

    def test_s15_an_ask_that_is_not_refused_records_nothing_new(self):
        self.record_all()
        code, why, out, err = self.waive()
        self.assertEqual(code, sb.EXIT_OK)
        self.assertIn("nothing recorded", out)
        self.assertEqual(self.waivers(), [])

    def test_the_mutants_are_caught(self):
        for patch, case in ((mock.patch.object(sb, "ask_subject", lambda *a, **k: None), "happy"),                       # any issue number refused: no waiver recorded
                            (mock.patch.object(sb.plan_ledger, "authors_of", lambda *a, **k: set()), "test_s14_a_writer_of_the_ask_and_a_plain_agent_cannot_waive"),   # no writer check
                            (mock.patch.object(sb.plan_ledger, "ask_head", lambda h: "e" * 40), "happy")):               # head not the plan hash: a revised plan keeps the waiver
            self.setUp()
            with patch:
                with self.assertRaises(AssertionError, msg=case):
                    getattr(self, case)()


class TestGhShimGateProbe(Base):
    """The shim asks `gates-check` before `pr ready` and `pr merge`: a refusal (exit 10, only in an enforcing repo) stops the command."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        self.log = self.home / "gh-log.json"
        fake = self.bin / "gh"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        "if sys.argv[1:3] == ['repo', 'view']:\n    v = os.environ.get('FAKE_REPO')\n    print(v or ''); sys.exit(0 if v else 1)\n"
                        "if sys.argv[1:3] == ['pr', 'view'] and 'reviews,headRefOid,reviewDecision' in sys.argv:\n"
                        "    if os.environ.get('FAKE_REVIEWS_EXIT'): sys.exit(int(os.environ['FAKE_REVIEWS_EXIT']))\n"
                        "    print(os.environ.get('FAKE_REVIEWS') or '{\"reviews\": [], \"headRefOid\": \"' + 'a'*40 + '\"}'); sys.exit(0)\n"
                        "if sys.argv[1:3] == ['pr', 'view']:\n    v = os.environ.get('FAKE_PRURL')\n    print(v or ''); sys.exit(0 if v else 1)\n"
                        "json.dump({'argv': sys.argv[1:]}, open(%r,'w'))\n"
                        "print('real gh out'); sys.exit(7)\n" % str(self.log))
        fake.chmod(0o755)
        self.calls_log = self.home / "gates-calls.log"
        self.sb = self.home / "fakesb"
        self.sb.write_text("#!/bin/sh\n" + FAKE_SB_SOURCE +
                           "if [ \"$1\" = whoami ]; then\n  if [ \"$2\" = --json ]; then echo '{\"name\": \"worker\", \"repo\": \"acme/app\"}'; else echo worker; fi; exit 0; fi\n"
                           "if [ \"$1\" = review ] && [ \"$2\" = blockers ]; then\n  echo \"$@\" >> %r\n  [ -n \"$FAKE_BLOCKERS_EXIT\" ] && exit $FAKE_BLOCKERS_EXIT\n  [ -n \"$FAKE_BLOCKERS\" ] && echo \"$FAKE_BLOCKERS\"; exit 0; fi\n"
                           "if [ \"$1\" = gates-check ]; then\n  echo \"$@ own=$FLEET_SWITCHBOARD_OWN_GH\" >> %r\n"
                           "  [ -n \"$FAKE_GATE_SLEEP\" ] && sleep $FAKE_GATE_SLEEP\n  if [ -n \"$FAKE_GATE_JSON\" ]; then echo \"$FAKE_GATE_JSON\"; else echo '{\"verdict\": \"x\"}'; fi; exit ${FAKE_GATE_EXIT:-0}\nfi\nexit 2\n" % (str(self.calls_log) + ".review", str(self.calls_log)))
        self.sb.chmod(0o755)
        subprocess.run([str(self.sb), "whoami", "--json"], capture_output=True, timeout=60)   # the first start of a new file is the slow one
        self.state = self.home / "state"
        self.state.mkdir()
        self.write_gates({"acme/app": {"mode": "shadow"}})

    def write_gates(self, repos):
        config = self.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"plan_gates": {"repos": repos}} if repos is not None else {}))

    def run_shim(self, *args, env=None, binary=True):
        environ = {"PATH": "%s:%s:%s" % (SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home),
                   "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SHIM_LOOKUP_SECONDS": SHIM_LOOKUP_SECONDS,
                   "FLEET_SHIM_GATE_SECONDS": SHIM_LOOKUP_SECONDS}   # the gate question's bound too: the largest the shim allows
        if binary:
            environ["FLEET_SWITCHBOARD_BIN"] = str(self.sb)
        environ.update(env or {})
        write_fake_env(self.home, environ)
        if self.log.exists():
            self.log.unlink()
        started = time.time()
        done = subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, env=environ, timeout=60)
        self.elapsed = time.time() - started
        seen = json.loads(self.log.read_text()) if self.log.exists() else None
        return done, seen

    def gate_calls(self):
        return self.calls_log.read_text().splitlines() if self.calls_log.exists() else []

    def spool(self):
        path = self.state / "gate-failopen.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_ready_and_merge_ask_gates_check_for_the_right_pr_and_the_real_gh_runs_unchanged(self):
        for args, kind in ((("pr", "ready", "12", "-R", "acme/app"), "ready"), (("pr", "merge", "12", "--repo", "acme/app", "--squash"), "merge"),
                           (("pr", "merge", "https://github.com/acme/app/pull/12", "--delete-branch"), "merge"),
                           (("pr", "ready", "12"), "ready")):   # a bare number: the repo is the caller's own (whoami)
            with self.subTest(args=args):
                if self.calls_log.exists():
                    self.calls_log.unlink()
                done, seen = self.run_shim(*args, env={"FAKE_REPO": "acme/app"})
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), "the real gh's exit code and output pass through")
                self.assertEqual(seen["argv"], list(args))
                (call,) = self.gate_calls()
                self.assertEqual(call, "gates-check acme/app#12 --for %s --json own=" % kind)

    def review(self, state, who="rev", at="2026-01-01T00:00:00Z"):
        return {"state": state, "author": {"login": who}, "submittedAt": at}

    def merge(self, *extra, reviews=(), env=None, args=("pr", "merge", "12", "-R", "acme/app", "--merge")):
        e = {"FAKE_REPO": "acme/app", "FAKE_REVIEWS": json.dumps({"reviews": list(reviews), "headRefOid": self.HEAD})}
        e.update(env or {})
        return self.run_shim(*args, *extra, env=e)

    def test_a_merge_over_a_changes_requested_review_is_refused_naming_the_reviewer_in_any_repo(self):
        done, seen = self.merge(reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")])
        self.assertEqual(done.returncode, 10)
        self.assertIn("blocking review(s): reviewer-a: CHANGES_REQUESTED", done.stderr)
        self.assertIn("--over-review", done.stderr)
        self.assertIsNone(seen, "the real gh never ran")
        done, seen = self.merge("--admin", reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")])
        self.assertEqual((done.returncode, seen), (10, None))                    # --admin changes nothing
        done, seen = self.merge(reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")], env={"FAKE_REPO": "other/repo"},
                                args=("pr", "merge", "12", "-R", "other/repo", "--merge"))
        self.assertEqual(done.returncode, 10)                                    # a repo not listed under plan_gates is covered too

    OLD = "b" * 40

    def review_at(self, state, who, oid):
        return dict(self.review(state, who), commit={"oid": oid})

    def merge_decided(self, reviews, decision, *extra):
        e = {"FAKE_REPO": "acme/app", "FAKE_REVIEWS": json.dumps({"reviews": reviews, "headRefOid": self.HEAD, "reviewDecision": decision})}
        return self.run_shim("pr", "merge", "12", "-R", "acme/app", "--merge", *extra, env=e)

    def test_s1_a_changes_requested_review_at_the_current_head_blocks(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)], "CHANGES_REQUESTED")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("blocking review(s): reviewer-a: CHANGES_REQUESTED", done.stderr)

    def test_s2_an_old_head_changes_requested_review_blocks_while_github_says_changes_requested(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD)], "CHANGES_REQUESTED")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("reviewer-a: CHANGES_REQUESTED", done.stderr)

    def test_s3_a_newer_comment_by_the_same_reviewer_supersedes_an_old_head_changes_requested_review(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD), self.review_at("COMMENTED", "reviewer-a", self.HEAD)]
        done, seen = self.merge_decided(reviews, "APPROVED")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        self.assertEqual([l for l in self.spool() if l["event"] == "over_review"], [], "no over-review line")

    def test_an_old_head_changes_requested_review_with_no_newer_review_does_not_block_unless_github_says_so(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD)], "APPROVED")
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_a_review_without_a_commit_blocks_and_a_comment_before_it_or_another_changes_request_does_not_clear_it(self):
        for reviews in ([self.review("CHANGES_REQUESTED", "reviewer-a")],
                        [self.review_at("COMMENTED", "reviewer-a", self.HEAD), self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)],
                        [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD), self.review_at("COMMENTED", "reviewer-a", self.OLD),
                         self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)]):
            with self.subTest(reviews=[r["state"] for r in reviews]):
                done, seen = self.merge_decided(reviews, None)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)

    def test_a_same_head_changes_requested_review_is_not_cleared_by_a_comment_from_the_same_shared_login(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD), self.review_at("COMMENTED", "reviewer-a", self.HEAD)]
        for decision in ("", None, "APPROVED"):
            with self.subTest(decision=decision):
                done, seen = self.merge_decided(reviews, decision)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)
        done, seen = self.merge_decided(reviews + [self.review_at("APPROVED", "reviewer-a", self.HEAD)], "APPROVED")   # an approval clears it
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_s4_a_newer_comment_by_another_reviewer_does_not_supersede(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD), self.review_at("COMMENTED", "reviewer-b", self.HEAD)]
        done, seen = self.merge_decided(reviews, "")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("reviewer-a: CHANGES_REQUESTED", done.stderr)
        self.assertNotIn("reviewer-b", done.stderr)

    def test_s5_an_unreadable_reviews_read_fails_open_with_one_warning_and_the_override_is_still_logged(self):
        done, seen = self.merge(env={"FAKE_REVIEWS_EXIT": "1"})
        self.assertEqual(done.returncode, 7)
        self.assertEqual(done.stderr.count("review check skipped"), 1, done.stderr)

    def test_s7_the_over_review_reason_rule_holds_with_the_head_rule(self):
        block = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)]
        done, seen = self.merge("--over-review", "abcdefghi", reviews=block)
        self.assertEqual((done.returncode, seen), (10, None))
        done, seen = self.merge("--over-review", "abcdefghij", reviews=block)
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(len([l for l in self.spool() if l["event"] == "over_review"]), 1)

    def test_a_dismissed_or_superseded_review_and_plain_comments_do_not_block(self):
        for reviews in ([self.review("CHANGES_REQUESTED", "rev"), self.review("DISMISSED", "rev")],
                        [self.review("CHANGES_REQUESTED", "rev"), self.review("APPROVED", "rev")],
                        [self.review("CHANGES_REQUESTED", "rev"), self.review("COMMENTED", "other"), self.review("APPROVED", "rev")],
                        [self.review("APPROVED", "rev"), self.review("COMMENTED", "rev")]):
            with self.subTest(reviews=[r["state"] for r in reviews]):
                done, seen = self.merge(reviews=reviews)
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge(reviews=[self.review("APPROVED", "a"), self.review("CHANGES_REQUESTED", "b")])   # one reviewer's block is enough
        self.assertEqual(done.returncode, 10)

    def test_a_recorded_blocking_verdict_at_the_current_head_blocks_and_is_named(self):
        done, seen = self.merge(env={"FAKE_BLOCKERS": "security recorded request-changes (2 blocking finding(s)) at aaaaaaaa"})
        self.assertEqual(done.returncode, 10)
        self.assertIn("security recorded request-changes", done.stderr)
        asked = (self.calls_log.parent / (self.calls_log.name + ".review")).read_text().split()
        self.assertEqual(asked, ["review", "blockers", "acme/app#12", "--head", self.HEAD])      # the shim binds the question to the head it read

    def test_over_review_with_a_reason_goes_ahead_is_stripped_and_logged_and_without_one_it_is_refused(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge("--over-review", "the owner accepted the risk in chat", reviews=block)
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        self.assertNotIn("--over-review", " ".join(seen["argv"]))
        self.assertNotIn("the owner accepted", " ".join(seen["argv"]))
        self.assertIn("on purpose", done.stderr)
        (line,) = [l for l in self.spool() if l["event"] == "over_review"]
        self.assertIn("worker", line["detail"])
        self.assertIn("the owner accepted the risk in chat", line["detail"])
        self.assertIn("reviewer-a: CHANGES_REQUESTED", line["detail"])
        self.assertEqual(line["pr"], "acme/app#12")
        done, seen = self.merge("--over-review=a reason of enough length", reviews=block)           # the = spelling
        self.assertEqual(done.returncode, 7)
        for extra in (("--over-review", "short"), ("--over-review",)):
            done, seen = self.merge(*extra, reviews=block)
            self.assertEqual((done.returncode, seen), (10, None))
            self.assertIn("at least 10 characters", done.stderr)
        done, seen = self.merge("--over-review", "a reason that nobody needs", reviews=[])           # nothing to override: just stripped
        self.assertEqual((done.returncode, "--over-review" in " ".join(seen["argv"])), (7, False))

    def test_a_reviews_api_or_switchboard_failure_lets_the_merge_through_with_a_visible_warning(self):
        done, seen = self.merge(env={"FAKE_REVIEWS_EXIT": "1"})
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))
        self.assertIn("could not be read): not checked", done.stderr)
        done, seen = self.merge(env={"FAKE_BLOCKERS_EXIT": "1"})
        self.assertEqual(done.returncode, 7)
        self.assertIn("recorded verdicts could not be read", done.stderr)
        done, seen = self.merge(env={"FAKE_REVIEWS": "not json"})
        self.assertEqual(done.returncode, 7)
        self.assertIn("not checked", done.stderr)

    def tagged(self, first, who="shared", tag="rev", at="2026-01-01T00:00:00Z", state="COMMENTED"):
        body = "%s\n\nfindings...\n\n<!-- fleet-switchboard:from=%s -->" % (first, tag)
        return {"state": state, "author": {"login": who}, "submittedAt": at, "body": body}

    def test_a_commented_review_whose_first_line_says_changes_requested_at_the_current_head_blocks_and_a_later_approve_clears_it(self):
        cr = self.tagged("Review of %s: CHANGES REQUESTED" % self.HEAD)
        done, seen = self.merge(reviews=[cr])
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("shared/rev: CHANGES REQUESTED (Review of %s)" % self.HEAD[:8], done.stderr)
        for spelling in ("Review of %s: CHANGES REQUESTED" % self.HEAD[:7], "review of %s - changes requested" % self.HEAD, "Review of %s: REQUEST CHANGES" % self.HEAD):
            done, seen = self.merge(reviews=[self.tagged(spelling)])                  # a short sha prefix and the other spellings count
            self.assertEqual(done.returncode, 10, spelling)
        done, seen = self.merge(reviews=[cr, self.tagged("Review of %s: APPROVE" % self.HEAD)])
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge(reviews=[cr, self.tagged("Review of %s: APPROVED" % self.HEAD, tag="other")])   # another reviewer's approve does not clear this one
        self.assertEqual(done.returncode, 10)
        done, seen = self.merge(reviews=[self.tagged("Review of %s: APPROVE" % self.HEAD), cr])                 # the latest one decides
        self.assertEqual(done.returncode, 10)

    def test_a_changes_requested_tag_at_an_old_head_and_untagged_comments_never_block(self):
        old = "b" * 40
        for reviews in ([self.tagged("Review of %s: CHANGES REQUESTED" % old)],
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": "CHANGES REQUESTED, please fix this"}],
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": "Looks fine.\nReview of %s: CHANGES REQUESTED" % self.HEAD}],   # not the FIRST line
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": None}],
                        [self.tagged("Review of nothex: CHANGES REQUESTED")]):
            with self.subTest(body=str(reviews[0].get("body"))[:40]):
                done, seen = self.merge(reviews=reviews)
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge("--over-review", "the owner reviewed this head in person", reviews=[self.tagged("Review of %s: CHANGES REQUESTED" % self.HEAD)])
        self.assertEqual(done.returncode, 7)                                    # the explicit override still works

    def test_an_override_whose_log_cannot_be_written_is_refused_and_a_pr_number_after_the_flag_is_not_its_reason(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge("--over-review", "a fine long reason here", reviews=block, env={"FLEET_SWITCHBOARD_STATE": str(self.home / "no-such-state")})
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("could not be logged", done.stderr)
        done, seen = self.merge(reviews=block, args=("pr", "merge", "--over-review", "12", "-R", "acme/app", "--merge"))   # `12` is the PR
        self.assertEqual(done.returncode, 10)
        self.assertIn("at least 10 characters", done.stderr)
        done, seen = self.merge("--over-review", "x" * 300, reviews=block)       # a long reason: the head and the overridden review still fit the line
        self.assertEqual(done.returncode, 7)
        line = [l for l in self.spool() if l["event"] == "over_review"][-1]["detail"]
        self.assertIn(self.HEAD[:12], line)
        self.assertIn("reviewer-a: CHANGES_REQUESTED", line)
        done, seen = self.merge("--over-review", "a reason\x1b[31m with escapes", reviews=block)
        logged = [l for l in self.spool() if l["event"] == "over_review"][-1]["detail"]
        self.assertNotIn("\x1b", logged)
        self.assertIn("with escapes", logged)

    def test_only_merge_is_checked_and_the_marker_buys_nothing(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge(reviews=block, args=("pr", "ready", "12", "-R", "acme/app"))
        self.assertEqual(done.returncode, 7)

    def test_a_blocking_review_refuses_a_merge_with_the_marker_set_and_unset(self):   # #1216 S2: the review probe does not honour the marker
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        for env in ({"FLEET_SWITCHBOARD_OWN_GH": "1"}, {}):
            with self.subTest(env=env):
                done, seen = self.merge(reviews=block, env=env)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)
                self.assertIn("blocking review", done.stderr)

    HEAD = "a" * 40
    REFUSAL = ("fleet-switchboard: merge of acme/app#12 refused: missing G1: no passing CI record at this head. Way out: ask the chief of "
               "staff for a logged waiver (`fleet-switchboard waive acme/app#12 --gates G1 --reason ...`; never for G6 or G8), or the chief "
               "of staff can switch the repo to shadow (`fleet-switchboard gates mode acme/app shadow`).")

    def test_an_enforcing_refusal_stops_the_command_and_prints_the_reason(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        for args in (("pr", "merge", "12", "-R", "acme/app"), ("pr", "ready", "12", "-R", "acme/app")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})})
                self.assertEqual(done.returncode, 10)
                self.assertIsNone(seen, "the real gh did not run")
                self.assertEqual(done.stdout, "")
                self.assertEqual(done.stderr.strip(), self.REFUSAL)
                self.assertEqual(self.spool()[-1]["event"], "refused")

    def test_a_refusal_with_no_message_still_stops(self):
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"mode": "enforce"})})
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("refused", done.stderr)

    def test_everything_else_runs_the_real_gh(self):
        for code in ("3", "1", "127", "0"):   # fail-open, crash, missing, allow (a valid waiver, a shadow repo, a kill switch all answer 0)
            with self.subTest(code=code):
                done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": code})
                self.assertEqual((done.returncode, seen["argv"]), (7, ["pr", "merge", "12", "-R", "acme/app"]))
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "0", "FAKE_GATE_JSON": "not json"})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"])

    def test_an_enforcing_merge_is_bound_to_the_checked_head(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        ok = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": self.HEAD})}
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--squash", env=ok)
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--squash", "--match-head-commit", self.HEAD])
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", self.HEAD, env=ok)
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--match-head-commit", self.HEAD], "the caller's own binding stands")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit=" + "b" * 40, env=ok)
        self.assertEqual((done.returncode, seen), (10, None), "a binding to another head is refused")
        self.assertIn("not the head the gates were checked at", done.stderr)
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env=ok)
        self.assertEqual(seen["argv"], ["pr", "ready", "12", "-R", "acme/app"], "ready is not bound")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_JSON": json.dumps({"mode": "enforce"})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"], "no head known: fail-open, recorded")
        self.assertEqual(self.spool()[-1]["event"], "fail_open")

    def test_auto_merge_is_refused_only_when_enforcing(self):
        enforcing = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": self.HEAD})}
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--auto", env=enforcing)
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("--auto", done.stderr)
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--auto",
                                   env={"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "shadow", "head": self.HEAD})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--auto"], "shadow never refuses and never rewrites")

    ENFORCE = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": "a" * 40})}

    def test_the_head_binding_cannot_be_dodged_by_how_the_flag_is_spelled(self):
        head = "a" * 40
        for extra, want in ((["--match-head-commit", ""], []), (["--match-head-commit="], []), (["--match-head-commit"], []),
                            (["--match-head-commit", head.upper()], []), (["--match-head-commit=" + head, "--match-head-commit", ""], []),
                            (["--match-head-commit", "", "--match-head-commit", head], [])):
            with self.subTest(extra=extra):
                done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", *extra, env=self.ENFORCE)
                self.assertEqual(done.returncode, 7)
                self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--match-head-commit", head])
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", "a", env=self.ENFORCE)
        self.assertEqual((done.returncode, seen), (10, None), "a prefix is not the head")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", "b" * 40, "--match-head-commit=" + head, env=self.ENFORCE)
        self.assertEqual((done.returncode, seen), (10, None), "every given value must be the checked head")

    def test_a_bare_pr_number_uses_the_repo_the_real_gh_would(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, FAKE_REPO="other/place"))
        self.assertEqual(self.gate_calls(), [], "the directory's repo is not listed: not asked, not rewritten")
        self.assertEqual(seen["argv"], ["pr", "merge", "5"])
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, FAKE_REPO="acme/app"))
        self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=")
        self.assertEqual(seen["argv"][-2:], ["--match-head-commit", "a" * 40])
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, GH_REPO="acme/app"))
        self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=", "GH_REPO is honoured")
        self.calls_log.unlink()
        done, seen = self.run_shim("pr", "merge", "5", env=self.ENFORCE)   # the real gh cannot say which repo: never guess the fleet repo
        self.assertEqual((done.returncode, self.gate_calls()), (7, []))
        self.assertIn("could not resolve the repo", self.spool()[-1]["detail"])

    def test_ordinary_spellings_are_understood(self):
        head = "a" * 40
        for args in (("pr", "merge", "-sd", "5", "-R", "acme/app"), ("pr", "merge", "5", "-R", "acme/app", "-sd"),
                     ("pr", "merge", "5", "--squash=true", "-R", "acme/app"), ("pr", "merge", "5", "-Racme/app"),
                     ("pr", "merge", "5", "--repo=acme/app", "--delete-branch=false"),
                     ("pr", "merge", "https://github.com/acme/app/pull/5/files")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=self.ENFORCE)
                self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=")
                self.assertEqual(seen["argv"], list(args) + ["--match-head-commit", head])
        for args in (("pr", "merge", "5", "-R", "acme/app", "--auto=true"), ("pr", "merge", "5", "-R", "acme/app", "-sd", "--auto")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=self.ENFORCE)
                self.assertEqual((done.returncode, seen), (10, None))
        done, seen = self.run_shim("pr", "merge", "5", "-R", "acme/app", "--auto=false", env=self.ENFORCE)
        self.assertEqual(seen["argv"][-2:], ["--match-head-commit", head], "--auto=false is not auto")

    def test_no_argument_and_a_branch_name_are_resolved_by_the_real_gh_or_recorded(self):
        for args in (("pr", "merge"), ("pr", "merge", "my-branch", "-R", "acme/app"), ("pr", "ready")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=dict(self.ENFORCE, FAKE_PRURL="https://github.com/acme/app/pull/8"))
                self.assertEqual(self.gate_calls()[-1].split()[1], "acme/app#8")
                self.assertEqual(seen["argv"][:len(args)], list(args))
        self.calls_log.unlink()
        done, seen = self.run_shim("pr", "merge", env=self.ENFORCE)
        self.assertEqual((done.returncode, self.gate_calls()), (7, []))
        self.assertIn("could not resolve the PR", self.spool()[-1]["detail"])
        done, seen = self.run_shim("pr", "ready", "12", "--undo", "-R", "acme/app", env=self.ENFORCE)
        self.assertIn("shape not understood", self.spool()[-1]["detail"])

    def test_a_refusal_is_printed_without_control_characters_and_only_for_an_enforce_reply(self):
        reply = json.dumps({"mode": "enforce", "message": "bad \x1b]0;title\x07 \x1b[2Jtext"})
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": reply})
        self.assertEqual((done.returncode, done.stderr), (10, "bad ]0;title [2Jtext\n"))
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app",
                                   env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"mode": "shadow", "message": "x"})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"], "exit 10 from a shadow reply does not refuse")

    def test_a_check_that_hangs_costs_at_most_the_bound_and_the_command_still_runs(self):
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env={"FAKE_GATE_SLEEP": "30", "FLEET_SHIM_GATE_SECONDS": "1"})
        self.assertEqual(seen["argv"], ["pr", "ready", "12", "-R", "acme/app"])
        self.assertLess(self.elapsed, 6)
        (entry,) = self.spool()
        self.assertEqual(entry["event"], "fail_open")
        self.assertIn("no answer within 1s", entry["detail"])

    def test_the_bound_is_clamped_and_defaults_to_three_seconds(self):
        source = SHIM.read_text()
        self.assertIn("GATE_SECONDS = 3.0", source)
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env={"FAKE_GATE_SLEEP": "30", "FLEET_SHIM_GATE_SECONDS": "0.01"})
        self.assertLess(self.elapsed, 25, "a bound below 0.2 s is raised to 0.2 s, not 0")

    def test_with_no_switchboard_the_command_runs_and_the_gap_is_recorded(self):
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", binary=False, env={"HOME": str(self.home / "nohome")})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"])

    def test_shapes_it_does_not_understand_pass_through_and_are_not_probed(self):
        for args in (("pr", "merge"), ("pr", "merge", "my-branch", "-R", "acme/app"), ("pr", "ready", "12", "--undo", "-R", "acme/app"),
                     ("pr", "merge", "12", "13", "-R", "acme/app"), ("pr", "merge", "12", "--mystery", "-R", "acme/app"),
                     ("pr", "merge", "12", "-R", "not a repo"), ("pr", "merge", "--", "12"), ("pr", "ready", "0", "-R", "acme/app"),
                     ("pr", "create", "--title", "t", "--body", "b"), ("pr", "view", "12", "-R", "acme/app"),
                     ("pr", "merge", "https://example.com/acme/app/pull/12")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args)
                if args[1] != "view":
                    self.assertEqual(seen["argv"][:2], list(args)[:2])
                self.assertEqual(self.gate_calls(), [])

    def test_a_repo_not_listed_is_not_probed_and_the_match_ignores_case(self):
        self.run_shim("pr", "merge", "12", "-R", "acme/other")
        self.assertEqual(self.gate_calls(), [])
        self.write_gates({"Acme/App": {"mode": "shadow"}})
        self.run_shim("pr", "merge", "12", "-R", "ACME/app")
        self.assertEqual(len(self.gate_calls()), 1)
        self.write_gates({})
        self.calls_log.unlink()
        self.run_shim("pr", "merge", "12", "-R", "acme/app")
        self.assertEqual(self.gate_calls(), [], "no plan_gates in the config: nothing is probed")
        self.write_gates(None)
        self.run_shim("pr", "merge", "12", "-R", "acme/app")
        self.assertEqual(self.gate_calls(), [])

    def test_a_fork_pr_url_is_asked_about_under_its_own_repo(self):
        self.write_gates({"acme/app": {"mode": "shadow"}})
        self.run_shim("pr", "merge", "https://github.com/someone/app/pull/9")
        self.assertEqual(self.gate_calls(), [], "a fork's PR is another repo: not listed, not probed")

    def mutant_shim(self, func):
        """A copy of the whole shim directory in which `func` honours the marker again (returns at once for it); SHIM points at it while the case runs."""
        source = SHIM.read_text()
        head = source[source.index("def %s(" % func):]
        at = head.index(":\n", head.index(")")) + 2
        guard = '    if os.environ.get("FLEET_SWITCHBOARD_OWN_GH") == "1":\n        return None\n'
        if source[source.index("def %s(" % func) + at - 2 - 1:][:0] == "":
            pass
        # a docstring comes first: put the guard after it
        body = head[at:]
        stripped = body.lstrip()
        if stripped.startswith('"""'):
            end = body.index('"""', body.index('"""') + 3) + 3
            at += end + 1
        mutant = source.replace(head, head[:at] + guard + head[at:], 1)
        ast.parse(mutant)
        target = self.home / "mutant-shims"
        shutil.copytree(SHIM.parent, target)
        (target / "gh").write_text(mutant)
        return target / "gh"

    def test_mutants_that_honour_the_marker_again_fail_the_cases(self):   # #1216 S6: review probe and gate probe
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        self.write_gates({"acme/app": {"mode": "enforce"}})
        gate = {"FLEET_SWITCHBOARD_OWN_GH": "1", "FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})}
        for func, run in (("review_probe", lambda: self.merge(reviews=block, env={"FLEET_SWITCHBOARD_OWN_GH": "1"})),
                          ("gate_probe", lambda: self.run_shim("pr", "merge", "12", "-R", "acme/app", env=gate))):
            with self.subTest(func=func):
                real = self.run_shim_result_code(run)
                self.assertEqual(real, 10, "the real shim refuses")
                mutant = self.mutant_shim(func)
                try:
                    with mock.patch.dict(globals(), {"SHIM": mutant}), rebound("SHIM", mutant):
                        self.assertNotEqual(self.run_shim_result_code(run), 10, "the mutant lets the merge run")
                finally:
                    shutil.rmtree(self.home / "mutant-shims", ignore_errors=True)

    def run_shim_result_code(self, run):
        done, seen = run()
        return done.returncode

    def test_table_bad_inputs_behave_the_same_with_the_marker_set_and_unset(self):   # #1216: the marker changes nothing for any bad input
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        self.write_gates({"acme/app": {"mode": "enforce"}})
        table = (("no switchboard", dict(binary=False), {}),
                 ("reviews not json", {}, {"FAKE_REVIEWS": "not json"}),
                 ("reviews call error", {}, {"FAKE_REVIEWS_EXIT": "3"}),
                 ("blockers call error", {}, {"FAKE_BLOCKERS_EXIT": "5"}),
                 ("gate call error", {}, {"FAKE_GATE_EXIT": "9"}),
                 ("gate wrong type", {}, {"FAKE_GATE_JSON": "[1]"}),
                 ("gate not json", {}, {"FAKE_GATE_JSON": "nope", "FAKE_GATE_EXIT": "10"}),
                 ("gate refuses", {}, {"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": "no"})}))
        for name, kw, env in table:
            with self.subTest(case=name):
                got = []
                for extra in ({}, {"FLEET_SWITCHBOARD_OWN_GH": "1"}):
                    done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env=dict(env, **extra), **kw)
                    got.append((done.returncode, seen is None))
                self.assertEqual(got[0], got[1], "the marker changes nothing")
        for extra in ({}, {"FLEET_SWITCHBOARD_OWN_GH": "1"}):   # a blocking review is refused either way
            done, seen = self.merge(reviews=block, env=extra)
            self.assertEqual((done.returncode, seen), (10, None))

    def test_the_marker_does_not_skip_the_gate_probe(self):   # #1216 S3: the gate probe does not honour the marker
        self.write_gates({"acme/app": {"mode": "enforce"}})
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FLEET_SWITCHBOARD_OWN_GH": "1", "FAKE_GATE_EXIT": "10",
            "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})})
        self.assertEqual((done.returncode, seen), (10, None), done.stderr)
        self.assertEqual(len(self.gate_calls()), 1)

    def test_the_spool_is_private_clipped_and_bounded(self):
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})
        path = self.state / "gate-failopen.jsonl"
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        path.write_text("x" * 300000)
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})
        self.assertEqual(len(path.read_text()), 300000, "a full spool takes no more lines")
        path.unlink()
        self.state.rmdir()
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})   # no state dir: nothing written, nothing fails

    def test_signing_and_the_account_environment_are_untouched(self):
        done, seen = self.run_shim("pr", "comment", "3", "--body", "hello")
        self.assertIn("fleet-switchboard:from=worker", seen["argv"][-1])
        self.assertEqual(self.gate_calls(), [])

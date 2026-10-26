"""Test classes moved out of bin/test-switchboard (text unchanged): cases_03."""
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
from support.core import Base, FakeRunner, SAFE_PATH
from support.fake_jev import FakeOpener, JEV_SETTINGS, jev_reply, noul_answers
from support.installs_and_keys import InstallCase, KeyCase, PR16_KEY, tree_of
from support.loader import SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, captured, class_source, sb, sc, switchboard_source
from support.orders_and_adapters import EA, ExternalAdapterCase, FakeDoorServer, FakeWorkHost, FakeWorkSink, fake_mapping, report, worker
from support.paths import BIN, REPO
from support.policy_and_import import TRIAL
from support.scenario_roles import AGENT_FILES, ROLE_BYTE_LIMITS, role_of
from support.wire_fakes import EPOCH, MSG, SES, routed


class TestV2Client(Base):
    def client(self, **runner_args):
        runner = FakeRunner(**runner_args)
        return sb.V2Client("/opt/lab/bin/opencode", {"XDG_DATA_HOME": "/tmp/lab-data"}, runner, 7), runner

    def test_argv_for_every_method(self):
        meta = {"fleet": {"name": "coder", "role": "coder"}}
        cases = [
            (lambda c: c.server_info(), ["server.info"]),
            (lambda c: c.session_create("build", "Coder", "/srv/repo", meta),
             ["session.create", "-d", json.dumps(
                 {"agent": "build", "location": {"directory": "/srv/repo"}, "metadata": meta, "title": "Coder"},
                 separators=(",", ":"), sort_keys=True)]),
            (lambda c: c.session_create("build", "Coder", "/srv/repo", meta, permissions=[{"action": "deny"}]),
             ["session.create", "-d", json.dumps(
                 {"agent": "build", "location": {"directory": "/srv/repo"}, "metadata": meta,
                  "permissions": [{"action": "deny"}], "title": "Coder"},
                 separators=(",", ":"), sort_keys=True)]),
            (lambda c: c.session_get(SES), ["session.get", "--param", "sessionID=" + SES]),
            (lambda c: c.session_active(), ["session.active"]),
            (lambda c: c.message_list(SES), ["session.message.list", "--param", "sessionID=" + SES]),
            (lambda c: c.message_list(SES, limit=20, order="desc", type="synthetic", cursor="cur_1"),
             ["session.message.list", "--param", "sessionID=" + SES, "--param", "limit=20",
              "--param", "order=desc", "--param", "type=synthetic", "--param", "cursor=cur_1"]),
            (lambda c: c.synthetic(SES, "[switchboard] hi", {"fleet": {"keys": ["a"]}}, False, "queue", MSG),
             ["session.synthetic", "--param", "sessionID=" + SES, "-d", json.dumps(
                 {"delivery": "queue", "id": MSG, "metadata": {"fleet": {"keys": ["a"]}},
                  "resume": False, "text": "[switchboard] hi"}, separators=(",", ":"), sort_keys=True)]),
            (lambda c: c.prompt(SES, "brief"),
             ["session.prompt", "--param", "sessionID=" + SES, "-d", '{"text":"brief"}']),
            (lambda c: c.prompt(SES, "brief", metadata={"fleet": {}}, delivery="queue", resume=True),
             ["session.prompt", "--param", "sessionID=" + SES, "-d",
              '{"delivery":"queue","metadata":{"fleet":{}},"resume":true,"text":"brief"}']),
            (lambda c: c.inbox_list(SES), ["session.inbox.list", "--param", "sessionID=" + SES]),
            (lambda c: c.inbox_update(SES, MSG, "steer"),
             ["session.inbox.update", "--param", "sessionID=" + SES, "--param", "inboxID=" + MSG,
              "-d", '{"delivery":"steer"}']),
        ]
        for call, tail in cases:
            client, runner = self.client()
            call(client)
            with self.subTest(op=tail[0]):
                self.assertEqual(runner.argv, ["/opt/lab/bin/opencode", "api"] + tail)
                self.assertEqual(runner.kwargs["timeout"], 7)
                # the reply goes to a file, never inherited and never a pipe: v2 cuts a large reply on a pipe
                self.assertNotIn(runner.kwargs["stdout"], (None, subprocess.PIPE))
                self.assertEqual(runner.kwargs["stderr"], subprocess.PIPE)
                self.assertEqual(runner.kwargs["stdin"], subprocess.DEVNULL)

    def test_every_call_goes_through_one_method(self):
        source = class_source("V2Client")
        self.assertEqual(source.count("self.runner("), 1)
        self.assertEqual(source.count('"api"'), 1)
        self.assertIn("self.runner(", class_source("V2Client", "_cli"))   # the CLI route; the HTTP route has its own class and is reached from `_transport`
        self.assertEqual((source.count("self._cli("), class_source("V2Client", "_transport").count("self._cli(")), (2, 2),
                         "only the transport step reaches the CLI (directly, or after the HTTP route could not serve the call)")
        self.assertIn("self._transport(", class_source("V2Client", "_call"))

    def test_env_is_the_process_env_with_the_config_env_on_top(self):
        os.environ["XDG_DATA_HOME"] = "/live/data"
        os.environ["SOMETHING_ELSE"] = "kept"
        client, runner = self.client()
        client.server_info()
        env = runner.kwargs["env"]
        self.assertEqual(env["XDG_DATA_HOME"], "/tmp/lab-data")
        self.assertEqual(env["SOMETHING_ELSE"], "kept")
        self.assertEqual(os.environ["XDG_DATA_HOME"], "/live/data")

    def test_data_envelope_is_unwrapped(self):
        client, _ = self.client(default=(0, '{"data": {"ses_1": {"type": "running"}}}', ""))
        self.assertEqual(client.session_active(), {"ses_1": {"type": "running"}})
        client, _ = self.client(default=(0, '{"data": {}}', ""))
        self.assertEqual(client.session_active(), {})
        client, _ = self.client(default=(0, '[{"id": "msg_1"}]', ""))
        self.assertEqual(client.message_list(SES), [{"id": "msg_1"}])
        client, _ = self.client(default=(0, "  \n", ""))
        self.assertIsNone(client.server_info())

    def test_http_error_carries_its_status_and_body(self):
        client, _ = self.client(default=(1, '{"name":"NotFound","message":"no such session"}',
                                         "HTTP 404 Not Found\n"))
        with self.assertRaises(sb.V2Error) as caught:
            client.session_get(SES)
        err = caught.exception
        self.assertEqual((err.reason, err.status), ("http", 404))
        self.assertIn("no such session", err.detail)
        self.assertIn("404", str(err))

    def test_nonzero_exit_without_http_status(self):
        client, _ = self.client(default=(1, "", "error: cannot find the service\nmore\n"))
        with self.assertRaises(sb.V2Error) as caught:
            client.server_info()
        self.assertEqual((caught.exception.reason, caught.exception.status), ("exit", None))
        self.assertNotIn("\n", caught.exception.detail)
        client, _ = self.client(default=(3, "", ""))
        with self.assertRaises(sb.V2Error) as caught:
            client.server_info()
        self.assertIn("exit 3", str(caught.exception))

    def test_timeout_and_missing_binary(self):
        client, _ = self.client(default=subprocess.TimeoutExpired(["x"], 7))
        with self.assertRaises(sb.V2Error) as caught:
            client.session_active()
        self.assertEqual(caught.exception.reason, "timeout")
        client, _ = self.client(default=FileNotFoundError(2, "No such file or directory"))
        with self.assertRaises(sb.V2Error) as caught:
            client.session_active()
        self.assertEqual(caught.exception.reason, "unavailable")

    def test_bad_json_on_success(self):
        client, _ = self.client(default=(0, "Commands:\n  opencode api\n", ""))
        with self.assertRaises(sb.V2Error) as caught:
            client.server_info()
        self.assertEqual(caught.exception.reason, "bad_json")

    def test_errors_never_carry_the_environment(self):
        client = sb.V2Client("/opt/lab/bin/opencode", {"OPENCODE_SERVER_PASSWORD": "hunter2-value"},
                             FakeRunner(default=(1, "", "HTTP 401 Unauthorized")), 7)
        with self.assertRaises(sb.V2Error) as caught:
            client.server_info()
        self.assertNotIn("hunter2-value", str(caught.exception))

    def test_arguments_are_checked_before_anything_runs(self):
        client, runner = self.client()
        bad = [
            lambda: client.synthetic(SES, "t", {}, "false", "queue", MSG),
            lambda: client.synthetic(SES, "t", {}, False, "interrupt", MSG),
            lambda: client.synthetic(SES, "t", {}, False, "queue", "abc"),
            lambda: client.message_list(SES, order="newest"),
            lambda: client.session_create("build", "t", "relative/dir", {}),
            lambda: client.inbox_update(SES, MSG, "now"),
            lambda: client.prompt(SES, "t", delivery="later"),
        ]
        for call in bad:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(runner.calls, [])


class TestStatus(Base):
    def status(self, runner, *argv):
        with captured() as (out, err):
            code = sb.main(["status", *argv], runner=runner)
        return code, out.getvalue(), err.getvalue()

    def test_all_ok(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode", "herdr": "/opt/herdr"})
        runner = routed()
        code, out, err = self.status(runner)
        self.assertEqual(code, 0, out)
        self.assertIn("version 2.0.22", out)
        self.assertIn("2 agent(s)", out)
        self.assertEqual(err, "")
        self.assertEqual([argv for argv, _ in runner.calls],
                         [["/opt/lab/bin/opencode", "api", "server.info"], ["/opt/herdr", "agent", "list"],
                          ["/opt/herdr", "plugin", "list"]])  # PR 16: whether herdr lists the plugin; read-only
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    def test_an_unknown_config_key_is_shown_with_the_nearest_known_key(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode", "herdr": "/opt/herdr", "batch_secs": 5, "policy": {"budgetms": 5}, "zzz_unrelated": 1})
        code, out, err = self.status(routed())
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(?m)^WARN  config +batch_secs is not read by this tool \(did you mean batch_seconds\?\)$")
        self.assertRegex(out, r"(?m)^WARN  config +policy\.budgetms is not read by this tool \(did you mean budget_ms\?\)$")
        self.assertRegex(out, r"(?m)^WARN  config +zzz_unrelated is not read by this tool$")   # nothing close: no guess
        self.assertEqual(out.count("WARN  config"), 3)
        self.assertIn("batch_secs", err)   # and still on stderr

    def test_a_clean_config_shows_no_warning_even_after_an_earlier_load_saw_one(self):
        sb.warn_unknown_config("x", "", ["leftover"], ["opencode"])
        self.write_config({"opencode": "/opt/lab/bin/opencode", "herdr": "/opt/herdr"})
        code, out, err = self.status(routed())
        self.assertNotIn("WARN  config", out)

    def test_unconfigured_v2_is_reported_and_never_run(self):
        # A bare name would be resolved through PATH, which can find OpenCode v1.
        runner = routed()
        code, out, _ = self.status(runner)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^ERROR v2 +not configured: ")
        self.assertEqual([argv for argv, _ in runner.calls], [["herdr", "agent", "list"], ["herdr", "plugin", "list"]])
        self.assertNotIn("api", [argv[1] for argv, _ in runner.calls], "v2 is never run when it is not configured")

    def test_failures_are_one_line_each_and_exit_1(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode"})
        runner = routed(v2=(1, '{"name":"ServiceUnavailable"}', "HTTP 503 Service Unavailable\n"),
                        herdr=FileNotFoundError(2, "No such file or directory"))
        code, out, err = self.status(runner)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^ERROR v2 +http 503: ")
        self.assertRegex(out, r"(?m)^ERROR herdr +unavailable: ")
        self.assertNotIn("Traceback", out + err)

    def test_json_report(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode"})
        runner = routed(v2=subprocess.TimeoutExpired(["opencode"], 20), herdr=(0, "garbage", ""))
        code, out, _ = self.status(runner, "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertFalse(report["ok"])
        self.assertIn("timeout", report["v2"]["error"])
        self.assertIn("bad_json", report["herdr"]["error"])
        self.assertTrue(report["config"]["ok"])
        self.assertTrue(report["state_dir"]["ok"])

    def test_an_unexpected_exception_is_still_one_line(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode"})
        runner = routed(v2=RuntimeError("boom"))
        code, out, err = self.status(runner)
        self.assertEqual(code, 1)
        self.assertIn("unexpected RuntimeError: boom", out)
        self.assertNotIn("Traceback", out + err)

    def test_bad_config_skips_the_probes_and_says_why(self):
        self.write_config({"batch_seconds": "soon"})
        runner = routed()
        code, out, _ = self.status(runner)
        self.assertEqual(code, 1)
        self.assertIn("batch_seconds", out)
        self.assertIn("not checked: config unusable", out)
        self.assertEqual(runner.calls, [])

    def test_open_state_dir_and_bad_fault_are_reported(self):
        self.state_dir.mkdir(parents=True)
        os.chmod(str(self.state_dir), 0o755)
        os.environ[sb.FAULT_ENV] = "no-such-fault"
        code, out, _ = self.status(routed())
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^ERROR state +state directory must be private")
        self.assertRegex(out, r"(?m)^ERROR lab +.*no-such-fault")

    def test_env_values_are_never_printed(self):
        self.write_config({"opencode_env": {"OPENCODE_SERVER_PASSWORD": "pw-value-123"}})
        for argv in ((), ("--json",)):
            _, out, err = self.status(routed(), *argv)
            self.assertNotIn("pw-value-123", out + err)
            self.assertIn("OPENCODE_SERVER_PASSWORD", out)

    def test_lab_faults_are_shown(self):
        self.write_config({"opencode": "/opt/lab/bin/opencode"})
        os.environ[sb.LAB_ENV] = "1"
        os.environ[sb.FAULT_ENV] = "no-batch"
        code, out, _ = self.status(routed())
        self.assertEqual(code, 0)
        self.assertIn("faults requested: no-batch", out)

    def test_real_process_with_failing_fakes_prints_no_traceback(self):
        opencode = self.fake_bin("opencode", "echo '{\"name\":\"Down\"}'\necho 'HTTP 502 Bad Gateway' >&2\nexit 1\n")
        herdr = self.fake_bin("herdr", "echo 'not json'\n")
        self.write_config({"opencode": str(opencode), "herdr": str(herdr), "call_timeout_seconds": 5})
        proc = self.run_cli(SWITCHBOARD, "status")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^ERROR v2 +http 502")
        self.assertRegex(proc.stdout, r"(?m)^ERROR herdr +bad_json")
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        missing = self.home / "absent-binary"
        self.write_config({"opencode": str(missing), "herdr": str(missing)})
        proc = self.run_cli(SWITCHBOARD, "status", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["v2"]["ok"], False)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_real_process_with_working_fakes(self):
        opencode = self.fake_bin("opencode", "echo '{\"data\":{\"version\":\"2.0.22\"}}'\n")
        herdr = self.fake_bin("herdr", "echo '{\"result\":{\"agents\":[]}}'\n")
        self.write_config({"opencode": str(opencode), "herdr": str(herdr)})
        proc = self.run_cli(SWITCHBOARD, "status")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_version_and_usage_exit_codes(self):
        proc = self.run_cli(SWITCHBOARD, "version")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "fleet-switchboard %s" % sb.VERSION)
        self.assertEqual(self.run_cli(SWITCHBOARD).returncode, 2)
        self.assertEqual(self.run_cli(SWITCHBOARD, "deliver").returncode, 2)
        self.assertEqual(self.run_cli(SWITCHBOARD, "status", "--bogus").returncode, 2)


class TestPoke(Base):
    def setUp(self):
        super().setUp()
        self.state = sb.ensure_state_dir()

    def test_a_poke_reaches_the_daemons_socket_and_a_burst_drains_at_once(self):
        sock = sb.open_poke_socket(self.state)
        self.addCleanup(sock.close)
        self.assertFalse(sb.wait_for_poke(sock, 0.01))
        for _ in range(3):
            self.assertTrue(sb.send_poke(self.state))
        self.assertTrue(sb.wait_for_poke(sock, 1))
        self.assertEqual(sb.drain_pokes(sock), 3)
        self.assertFalse(sb.wait_for_poke(sock, 0.05))
        self.assertEqual(stat.S_IMODE((self.state / sb.POKE_SOCKET).stat().st_mode), 0o600)

    def test_a_poke_with_no_daemon_is_false_not_an_error(self):
        self.assertFalse(sb.send_poke(self.state))  # no socket file
        self.assertFalse(sb.send_poke(self.home / "nowhere"))  # no state dir
        (self.state / sb.POKE_SOCKET).write_text("a stale file")
        self.assertFalse(sb.send_poke(self.state))  # a file nobody listens on

    def test_a_stale_socket_is_replaced(self):
        old = sb.open_poke_socket(self.state)
        old.close()  # the file stays, as after a crash
        self.assertTrue((self.state / sb.POKE_SOCKET).exists())
        new = sb.open_poke_socket(self.state)
        self.addCleanup(new.close)
        self.assertTrue(sb.send_poke(self.state))
        self.assertTrue(sb.wait_for_poke(new, 1))

    def test_the_state_dir_path_length_does_not_matter(self):
        deep = self.home
        while len(str(deep / sb.POKE_SOCKET)) < 150:
            deep = deep / "a-directory-with-a-long-name"
        deep.mkdir(parents=True)
        sock = sb.open_poke_socket(deep)
        self.addCleanup(sock.close)
        self.assertTrue(sb.send_poke(deep))
        self.assertTrue(sb.wait_for_poke(sock, 1))

    def test_the_working_directory_is_restored(self):
        before = os.getcwd()
        sb.send_poke(self.state)
        sock = sb.open_poke_socket(self.state)
        sock.close()
        self.assertEqual(os.getcwd(), before)

    def test_a_poke_to_a_full_socket_gives_up_instead_of_blocking(self):
        sock = sb.open_poke_socket(self.state)
        self.addCleanup(sock.close)
        started = time.time()
        results = [sb.send_poke(self.state) for _ in range(20000)]
        self.assertIn(False, results)  # the socket filled; the poke was dropped, not waited for
        self.assertLess(time.time() - started, 20)

    def test_the_poke_command_is_silent_and_always_exits_zero(self):
        # Its speed is the alarm's job (test_a_poke_is_bounded_by_its_alarm), not a wall clock around a process start.
        for setup in ("no state dir", "a daemon"):
            if setup == "a daemon":
                sock = sb.open_poke_socket(self.state)
                self.addCleanup(sock.close)
            result = self.run_cli(SWITCHBOARD, "poke")
            with self.subTest(setup=setup):
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(sb.wait_for_poke(sock, 1))  # and the one to a daemon arrived

    def test_a_poke_is_bounded_by_its_alarm(self):
        self.assertLessEqual(sb.POKE_ALARM, 2)
        for raises in (False, True):
            events = []

            def send(*a, **k):
                events.append("send")
                if raises:
                    raise OSError("down")

            with self.subTest(raises=raises), mock.patch.object(sb.signal, "alarm", side_effect=lambda n: events.append(("alarm", n)) or 0), \
                    mock.patch.object(sb, "send_poke", side_effect=send):
                sb.cmd_poke(None)
            self.assertEqual(events, [("alarm", sb.POKE_ALARM), "send", ("alarm", 0)])

    def test_poke_never_calls_herdr_or_v2_and_needs_no_config(self):
        def boom(argv, **kwargs):
            raise AssertionError("poke ran %r" % argv)

        with captured() as (out, err):
            code = sb.main(["poke"], runner=boom)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "", ""))
        self.assertFalse(self.config_file.exists())

    def test_a_poke_arms_an_alarm_that_is_disarmed_after(self):
        sb.cmd_poke(None)
        self.assertEqual(signal.alarm(0), 0)  # nothing left armed in this process


class GitHubOverGit:
    """The slice of GitHub's REST answers the carry reads, produced from a REAL git repository (so a merge, a conflict, a rebase, a binary file and a rename are the real thing):
    pulls/N, pulls/N/files, compare/A...B and commits/SHA, with GitHub's shapes (a patch starts at the first hunk and is absent for a binary file, files are paged by 100)."""

    def __init__(self, repo_dir, branch="feature", base="main", number=7):
        self.dir, self.branch, self.base, self.number = str(repo_dir), branch, base, number
        self.calls, self.fail, self.changed_files_override = [], None, None

    def git(self, *args, check=True):
        done = subprocess.run(["git", "-C", self.dir] + list(args), capture_output=True, text=True)
        if check and done.returncode != 0:
            raise RuntimeError("git %s: %s" % (" ".join(args), done.stderr.strip()))
        return done.stdout

    def rev(self, ref):
        return self.git("rev-parse", ref).strip()

    def files_between(self, a, b):
        key = (self.rev(a), self.rev(b))
        self.cache = getattr(self, "cache", {})
        if key not in self.cache:
            self.cache[key] = self._files_between(a, b)
        return self.cache[key]

    def _files_between(self, a, b):
        merge_base = self.git("merge-base", a, b).strip()
        names = [line.split("\t") for line in self.git("diff", "-M", "--name-status", merge_base, b).splitlines()]
        patches = [section for section in ("\n" + self.git("diff", "-M", merge_base, b)).split("\ndiff --git ")[1:]]
        tree_b = {line.split("\t")[1]: line.split()[2] for line in self.git("ls-tree", "-r", b).splitlines()}
        tree_base = {line.split("\t")[1]: line.split()[2] for line in self.git("ls-tree", "-r", merge_base).splitlines()}
        out = []
        for parts, section in zip(names, patches):
            code, paths = parts[0], parts[1:]
            path, previous = paths[-1], (paths[0] if code.startswith("R") else None)
            status = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}[code[0]]
            patch = section[section.index("\n@@") + 1:].rstrip("\n") if "\n@@" in section else None
            changes = sum(1 for l in (patch or "").splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
            blob = tree_base[path] if status == "removed" else tree_b[path]
            item = {"filename": path, "status": status, "sha": blob, "changes": changes}
            if previous:
                item["previous_filename"] = previous
            if patch is not None:
                item["patch"] = patch
            out.append(item)
        return out

    def page(self, items, query):
        number = int(query.get("page", ["1"])[0])
        return items[(number - 1) * 100:number * 100]

    def get(self, path):
        self.calls.append(path)
        if self.fail and self.fail in path:
            raise RuntimeError("HTTP 502 for %s" % path)
        head = self.rev(self.branch)
        key = (path, head, self.base, self.changed_files_override)   # a git repository is immutable by sha: the same question at the same tip has the same answer
        self.answers = getattr(self, "answers", {})
        if key not in self.answers:
            self.answers[key] = self._answer(path, head)
        return self.answers[key]

    def _answer(self, path, head):
        route, _, query = path.partition("?")
        query = dict(q.split("=", 1) for q in query.split("&") if q)
        query = {k: [v] for k, v in query.items()}
        parts = route.split("/")                      # repos / owner / repo / ...
        tail = "/".join(parts[3:])
        if tail == "pulls/%d" % self.number:
            files = self.files_between(self.base, head)
            return {"head": {"sha": head}, "base": {"ref": self.base}, "changed_files": self.changed_files_override if self.changed_files_override is not None else len(files)}
        if tail == "pulls/%d/files" % self.number:
            return self.page(self.files_between(self.base, head), query)
        if tail.startswith("compare/"):
            a, b = tail[len("compare/"):].split("...")
            if self.rev(a) == self.rev(b):
                status = "identical"
            elif subprocess.run(["git", "-C", self.dir, "merge-base", "--is-ancestor", a, b]).returncode == 0:
                status = "ahead"
            elif subprocess.run(["git", "-C", self.dir, "merge-base", "--is-ancestor", b, a]).returncode == 0:
                status = "behind"
            else:
                status = "diverged"
            commits = []
            for line in self.git("rev-list", "--parents", "--reverse", "%s..%s" % (a, b)).splitlines():
                sha, *parents = line.split()
                commits.append({"sha": sha, "parents": [{"sha": p} for p in parents]})
            return {"status": status, "total_commits": len(commits), "commits": self.page(commits, query), "files": self.page(self.files_between(a, b), query)}
        if tail.startswith("commits/"):
            sha = tail[len("commits/"):]
            parents = self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]
            files = []
            if parents:
                for line in self.git("diff", "--name-status", parents[0], sha).splitlines():
                    code, path = line.split("\t")[0], line.split("\t")[-1]
                    files.append({"filename": path, "status": {"A": "added", "M": "modified", "D": "removed"}.get(code[0], "modified"), "sha": self.rev("%s:%s" % (sha if code[0] != "D" else parents[0], path))})
            return {"sha": sha, "parents": [{"sha": p} for p in parents], "files": self.page(files, query)}
        raise RuntimeError("the fake GitHub has no answer for %s" % path)


class TestReportFoldVariants(unittest.TestCase):
    """#126 from first principles: a report about an issue that is already the person's decision must not sit beside it as a
    second, cos-tier entry, however the report names the issue or reaches it. Synthetic names; 4-digit issue numbers."""

    HOME = "acme-org/widget-service"

    def derive(self, items, parents=None, hub=None):
        cache = {("parent", self.HOME, n): (0, p) for n, p in (parents or {}).items()}
        engine = types.SimpleNamespace(config={"intent_repo": self.HOME}, discovery=None, _note=lambda *a, **k: None,
                                       hub=hub, intents=types.SimpleNamespace(_cache=cache, parent=lambda n, repo=None: None))
        found = list(items)
        with mock.patch.object(sb, "decision_repos", return_value=[self.HOME]), \
                mock.patch.object(sb, "decisions_from_github", lambda e, n: ([x for x in found if x["kind"] == "issue"], [])), \
                mock.patch.object(sb, "decisions_from_requests", lambda e, n: ([], [])), \
                mock.patch.object(sb, "decisions_from_reports", lambda e, n: ([x for x in found if x["kind"] == "report"], [])):
            decisions, _ = sb.derive_decisions(engine, EPOCH + 60)
        return decisions

    def issue(self, n, tier="human", parent=None, repo=None):
        return {"kind": "issue", "title": "Issue %d" % n, "ask": n, "agent": "orch", "since": EPOCH, "seed": "i%d" % n,
                "bare": True, "repo": repo or self.HOME, "tier": tier, "parent": parent}

    def report(self, text, ask=None, repo=None, known=True, agent="coder", tier="cos"):
        return {"kind": "report", "title": text, "ask": ask, "agent": agent, "since": EPOCH + 10, "seed": "r%s%s" % (ask, text),
                "bare": False, "repo": repo or self.HOME, "tier": tier, "repo_known": known}

    def shape(self, found):
        return sorted((d["kind"], d["tier"], d["ask"]) for d in found)

    def only_the_decision(self, found, ask=1234):
        self.assertEqual(self.shape(found), [("issue", "human", ask)])
        return found[0]

    def test_the_same_ask_by_number(self):
        self.only_the_decision(self.derive([self.issue(1234), self.report("q", 1234)]))

    def test_the_issue_named_by_a_short_ref_a_full_ref_a_url_or_a_bare_number(self):
        for text in ("blocked on widget-service#1234", "see acme-org/widget-service#1234",
                     "see https://github.com/acme-org/widget-service/issues/1234", "blocked on #1234"):
            with self.subTest(text=text):
                self.only_the_decision(self.derive([self.issue(1234), self.report(text)]))

    def test_a_report_tagged_to_another_ask_but_naming_the_escalated_issue_folds_in(self):
        for text in ("blocked on widget-service#1234", "see #1234", "see acme-org/widget-service#1234 please"):
            with self.subTest(text=text):
                found = self.derive([self.issue(1234), self.report(text, ask=1000)])
                entry = self.only_the_decision(found)
                self.assertEqual(entry["reported_by"], ["coder"])
                self.assertIn(text, entry["reported_lines"][0])                       # the text stays reachable

    def test_the_ask_chain_in_both_directions(self):
        # the report is about WORK ITEM 1240 whose ASK 1234 is the escalated decision (the parent is known)
        entry = self.only_the_decision(self.derive([self.issue(1234), self.report("q", 1240)], parents={1240: 1234}))
        self.assertEqual(entry["reported_by"], ["coder"])
        # the report is about the ASK 1234; the escalated decision is its work item 1240
        entry = self.only_the_decision(self.derive([self.issue(1240, parent=1234), self.report("q", 1234)]), ask=1240)
        self.assertEqual(entry["reported_by"], ["coder"])

    def test_the_repo_spelling_does_not_matter(self):
        self.only_the_decision(self.derive([self.issue(1234), self.report("q", 1234, repo="Acme-Org/Widget-Service")]))
        self.only_the_decision(self.derive([self.issue(1234, repo="ACME-ORG/widget-service"), self.report("q", 1234)]))

    def test_a_report_on_an_issue_that_is_only_cos_tier_or_unrelated_stays_its_own_entry(self):
        found = self.derive([self.issue(1234, tier="cos"), self.report("blocked on #1234", ask=1000)])
        self.assertEqual(self.shape(found), [("issue", "cos", 1234), ("report", "cos", 1000)])      # cos-tier: no chain/text fold
        found = self.derive([self.issue(1234), self.report("nothing named here", ask=1000),
                             self.report("q", 1240)], parents={1240: 9999})
        self.assertEqual(self.shape(found), [("issue", "human", 1234), ("report", "cos", 1000), ("report", "cos", 1240)])
        found = self.derive([self.issue(1234), self.report("blocked on other-repo#1234", ask=1000),
                             self.report("see #1234", ask=1001, repo="acme-org/other-service", known=True)])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "report", "report"])           # another repo's #1234 is not ours

    def test_two_escalated_issues_named_is_ambiguous_and_two_work_items_of_the_ask_are_too(self):
        found = self.derive([self.issue(1234), self.issue(1235), self.report("blocked on #1234 and #1235", ask=1000)])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "issue", "report"])
        found = self.derive([self.issue(1240, parent=1234), self.issue(1241, parent=1234), self.report("q", 1234)])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "issue", "report"])

    def test_the_escalation_arriving_after_the_report_folds_it_on_the_next_pass_and_nothing_is_stored(self):
        report = self.report("blocked on #1234", ask=1000)
        before = self.derive([self.issue(1234, tier="cos"), report])                 # not escalated yet: two entries
        self.assertEqual(self.shape(before), [("issue", "cos", 1234), ("report", "cos", 1000)])
        after = self.derive([self.issue(1234, tier="human"), report])                # escalated: the next pass folds it
        self.only_the_decision(after)
        again = self.derive([self.issue(1234, tier="human"), report])                # a restart: the same derivation, no memory
        self.only_the_decision(again)
        self.assertEqual(after[0]["id"], again[0]["id"])

    def test_the_daemon_follows_the_chain_with_a_bounded_read_and_a_command_reads_nothing(self):
        reads = []
        engine = types.SimpleNamespace(hub=object(), intents=types.SimpleNamespace(
            _cache={}, parent=lambda n, repo=None: reads.append(n) or 1234))
        lookup = sb.chain_parent_lookup(engine)
        self.assertEqual([lookup(self.HOME, n) for n in (1240, 1241, 1242, 1243, 1244)], [1234, 1234, 1234, None, None])
        self.assertEqual(reads, [1240, 1241, 1242])                                  # FOLD_PARENT_READS = 3 per pass
        quiet = types.SimpleNamespace(hub=None, intents=types.SimpleNamespace(_cache={}, parent=lambda n, repo=None: reads.append(n)))
        self.assertIsNone(sb.chain_parent_lookup(quiet)(self.HOME, 1250))
        self.assertEqual(reads, [1240, 1241, 1242])                                  # no read without the hub


class TestDecisionWriteWaitsForTheList(Base):
    """`decisions escalate|resolve|answer|supersede` leave the list showing the new state when they return, or say honestly that it is
    applied at the next pass. The daemon is the one writer of decisions.json: the command only pokes it and reads. Synthetic names."""

    def setUp(self):
        super().setUp()
        self.state = sb.ensure_state_dir(None)
        self.now = [0.0]
        self.slept = []
        patch = mock.patch.dict(sb._refresh_clock, {"sleep": self.sleep, "monotonic": lambda: self.now[0]})
        patch.start()
        self.addCleanup(patch.stop)
        self.on_sleep = None

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds
        if self.on_sleep:
            self.on_sleep(len(self.slept))

    def daemon_writes(self, decisions):
        body = {"decisions": decisions, "errors": [], "written_at": sb.iso(time.time()), "generation": 1, "daemon_pid": 1}
        (self.state / sb.DECISIONS_FILE).write_text(json.dumps(body))

    def entry(self, ident, tier="cos"):
        return {"id": ident, "kind": "issue", "tier": tier, "title": "t", "repo": "acme/api", "ask": 7}

    def finish(self, action, idents, poked=True):
        return sb.refreshed_or_honest_line(action, idents, poked, state_dir=str(self.state))

    def test_an_escalation_waits_until_the_file_shows_the_owners_tier_and_then_says_nothing(self):
        self.daemon_writes([self.entry("api#7")])                       # the old state: still the Chief of Staff's
        self.on_sleep = lambda n: self.daemon_writes([self.entry("api#7", "human")]) if n == 2 else None   # the daemon's pass lands
        self.assertIsNone(self.finish("escalate", ["api#7"]))
        self.assertEqual(len(self.slept), 2)                            # it waited for the pass, not the whole bound

    def test_resolve_answer_and_supersede_wait_until_every_decision_is_gone(self):
        for action in ("resolve", "answer", "supersede"):
            with self.subTest(action=action):
                self.slept.clear()
                self.now[0] = 0.0
                self.daemon_writes([self.entry("api#7"), self.entry("api#8"), self.entry("api#9")])
                self.on_sleep = lambda n: self.daemon_writes([self.entry("api#9")]) if n == 1 else None
                self.assertIsNone(self.finish(action, ["api#7", "api#8"]))   # both are gone; another decision may stay

    def test_a_file_that_never_changes_gives_one_honest_line_after_the_bound(self):
        self.daemon_writes([self.entry("api#7")])
        line = self.finish("escalate", ["api#7"])
        self.assertIn("applied at the next pass", line)
        self.assertIn("may still show the old state", line)
        self.assertGreaterEqual(self.now[0], sb.DECISION_REFRESH_SECONDS)   # it waited the bound, no more
        self.assertLess(self.now[0], sb.DECISION_REFRESH_SECONDS + 1)

    def test_a_stale_or_missing_file_is_not_a_current_list(self):
        self.assertIn("applied at the next pass", self.finish("resolve", ["api#7"]))          # no file at all
        self.daemon_writes([])
        old = time.time() - sb.DECISIONS_STALE_SECONDS - 60
        os.utime(str(self.state / sb.DECISIONS_FILE), (old, old))
        self.now[0] = 0.0
        self.assertIn("applied at the next pass", self.finish("resolve", ["api#7"]))          # an old file shows nothing as current

    def test_with_no_daemon_it_says_so_and_does_not_wait(self):
        self.daemon_writes([self.entry("api#7")])
        line = self.finish("escalate", ["api#7"], poked=False)
        self.assertIn("the daemon is not running", line)
        self.assertIn("ensure", line)
        self.assertEqual(self.slept, [])

    def test_the_command_only_reads_the_file_the_daemon_owns(self):
        self.daemon_writes([self.entry("api#7")])
        path = self.state / sb.DECISIONS_FILE
        before = (path.read_text(), path.stat().st_mtime_ns)
        self.finish("escalate", ["api#7"])
        self.assertEqual((path.read_text(), path.stat().st_mtime_ns), before)
        text = switchboard_source()
        body = text[text.index("def refreshed_or_honest_line"):text.index("def finish_decision_write")]
        for forbidden in ("write_text", "os.replace", "open(", "publish", "write_decisions"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_the_write_commands_poke_then_wait_and_say_the_line_on_stderr_not_stdout(self):
        calls = []
        with mock.patch.object(sb, "send_poke", lambda d: calls.append("poke") or True), \
                mock.patch.object(sb, "refreshed_or_honest_line", lambda a, i, p, state_dir=None: calls.append((a, i, p)) or "later"):
            with captured() as (out, err):
                sb.finish_decision_write("escalate", ["api#7"])
                sb.finish_decision_write("supersede", ["api#7", "api#8", None])
        self.assertEqual(calls, ["poke", ("escalate", ["api#7"], True), "poke", ("supersede", ["api#7", "api#8"], True)])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().count("later"), 2)

    def test_a_failed_supersede_neither_waits_nor_claims_a_change(self):
        args = mock.Mock(ids=["api#7"], id=None)
        for code, expect in ((sb.EXIT_ERROR, 0), (sb.EXIT_OK, 1)):
            with self.subTest(code=code):
                calls = []
                with mock.patch.object(sb, "fresh_engine_decisions", lambda *a, **k: (mock.Mock(), [], None)), \
                        mock.patch.object(sb, "decision_actor", lambda *a: (None, None)), \
                        mock.patch.object(sb, "cmd_decision_supersede", lambda *a: code), \
                        mock.patch.object(sb, "finish_decision_write", lambda a, i: calls.append((a, i))):
                    self.assertEqual(sb.cmd_decision_write(args, "supersede", None, 0), code)
                self.assertEqual(len(calls), expect)

    def test_nothing_in_it_can_raise_into_the_command(self):
        with mock.patch.object(sb, "send_poke", side_effect=RuntimeError("boom")):
            with captured() as (out, err):
                sb.finish_decision_write("escalate", ["api#7"])                    # a failure here never fails the write
        self.assertEqual(out.getvalue(), "")


class TestTrialDecisionsPlugin(Base):
    """PR 14: the trial driver installs the Decisions plugin into its own profile, and says whether it is there."""

    def trial(self):
        trial = TRIAL.Trial(self.home / "trial")
        os.makedirs(trial.lab.opencode_config, exist_ok=True)
        return trial

    def cli_json(self, trial):
        return json.loads((Path(trial.lab.opencode_config) / "cli.json").read_text())

    def test_it_copies_the_plugin_into_the_profile_and_names_the_directory_in_cli_json(self):
        trial = self.trial()
        self.assertEqual(trial.install_decisions_plugin(), "installed")
        target = Path(trial.lab.opencode_config) / "fleet-decisions"
        for name in ("tui.tsx", "decisions.mjs", "health.mjs"):
            self.assertEqual((target / name).read_text(), (REPO / "plugins" / "fleet-decisions-tui" / name).read_text())
        self.assertEqual(self.cli_json(trial), {"plugins": ["./fleet-decisions"]})

    def test_it_is_idempotent_and_keeps_what_cli_json_already_names(self):
        trial = self.trial()
        path = Path(trial.lab.opencode_config) / "cli.json"
        path.write_text(json.dumps({"plugins": ["./herdr-opencode"], "other": {"kept": True}}))
        trial.install_decisions_plugin()
        first = path.read_text()
        self.assertEqual(trial.install_decisions_plugin(), "unchanged")
        self.assertEqual(path.read_text(), first)
        self.assertEqual(self.cli_json(trial), {"plugins": ["./herdr-opencode", "./fleet-decisions"],
                                                "other": {"kept": True}})

    def test_a_cli_json_that_is_not_json_is_refused_not_overwritten(self):
        trial = self.trial()
        path = Path(trial.lab.opencode_config) / "cli.json"
        path.write_text("{oops")
        with self.assertRaises(TRIAL.TrialError) as caught:
            trial.install_decisions_plugin()
        self.assertIn("cli.json", str(caught.exception))
        self.assertEqual(path.read_text(), "{oops")

    def test_status_says_whether_it_is_installed_or_out_of_date(self):
        trial = self.trial()
        self.assertEqual(trial.decisions_plugin_status(), "not installed")
        trial.install_decisions_plugin()
        self.assertEqual(trial.decisions_plugin_status(), "installed")
        (Path(trial.lab.opencode_config) / "fleet-decisions" / "tui.tsx").write_text("old")
        self.assertEqual(trial.decisions_plugin_status(), "out of date: run `switchboard-trial up`")
        trial.install_decisions_plugin()
        (Path(trial.lab.opencode_config) / "cli.json").write_text('{"plugins": []}')
        self.assertEqual(trial.decisions_plugin_status(), "not installed")

    def test_an_older_trials_wrapper_is_refreshed_so_the_plugin_can_find_the_file(self):
        trial = self.trial()
        trial.wrapper.parent.mkdir(parents=True, exist_ok=True)
        trial.wrapper.write_text("#!/bin/sh\nexec true\n")
        self.assertEqual(trial.install_decisions_plugin(), "installed")
        self.assertIn("FLEET_SWITCHBOARD_STATE", trial.wrapper.read_text())
        self.assertTrue(os.access(str(trial.wrapper), os.X_OK))
        self.assertEqual(trial.install_decisions_plugin(), "unchanged")

    def test_the_wrapper_tells_the_plugin_where_the_state_directory_is(self):
        trial = self.trial()
        self.assertIn('export FLEET_SWITCHBOARD_STATE="$LAB/profile/state/fleet-switchboard"', trial.wrapper_text())
        self.assertEqual(str(trial.switchboard_state), str(trial.root / "profile" / "state" / "fleet-switchboard"))

    def test_up_installs_it_and_never_restarts_the_service(self):
        trial = self.trial()
        trial.save({"repo": "o/r", "charter": 1, "model": "p/m"})
        calls = []
        with mock.patch.object(TRIAL.Trial, "logged_in", return_value=True), \
                mock.patch.object(TRIAL.Trial, "model_problem", return_value=None), \
                mock.patch.object(TRIAL.Trial, "ensure_service", side_effect=lambda: calls.append("service") or "already running"), \
                mock.patch.object(TRIAL.Trial, "start_daemon", return_value="already running"), \
                mock.patch.object(TRIAL.Trial, "workspace_id", return_value="wT"), \
                mock.patch.object(TRIAL.Trial, "checkout", return_value=self.home), \
                mock.patch.object(TRIAL.Trial, "ensure_agent", return_value="already running"), \
                mock.patch.object(TRIAL.Trial, "close_spare_pane"), \
                mock.patch.object(TRIAL.Trial, "wrapper_run", side_effect=lambda *a, **k: calls.append(a) or
                                  subprocess.CompletedProcess([], 0, "", "")), \
                captured() as (out, err):
            code = TRIAL.cmd_up(trial, types.SimpleNamespace(fresh=False))
        self.assertEqual(code, 0)
        self.assertEqual(trial.decisions_plugin_status(), "installed")
        self.assertEqual(calls, ["service"], "the service was only asked to be running, never restarted or stopped")
        self.assertIn("a TUI picks the plugin up when it is restarted", out.getvalue())

    def test_status_reports_it(self):
        trial = self.trial()
        trial.save({"repo": "o/r", "charter": 1})
        trial.install_decisions_plugin()
        with mock.patch.object(TRIAL.Trial, "run_fleet", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")), \
                mock.patch.object(TRIAL.Trial, "workspace_id", return_value=None), captured() as (out, err):
            TRIAL.cmd_status(trial, None)
        self.assertRegex(out.getvalue(), r"decisions plugin\s+installed")

    def test_the_plugin_files_the_trial_installs_are_the_kits_own(self):
        self.assertEqual(sorted(sb.DECISIONS_PLUGIN_FILES), ["decisions.mjs", "health.mjs", "tui.tsx"])  # PR 16: the shared installer's
        for name in sb.DECISIONS_PLUGIN_FILES:
            self.assertTrue((REPO / "plugins" / "fleet-decisions-tui" / name).is_file())


class TestCommissioningDocs(unittest.TestCase):
    SKILL = REPO / "skills" / "fleet-setup" / "SKILL.md"

    def test_the_skill_is_written_like_the_installer_and_names_the_decision_and_the_check(self):
        text = self.SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: fleet-setup\n"))
        for phrase in ("CHECK:", "DO:", "VERIFY:", "PROVES:", "Create <owner>/<repo>", "(private) under account",
                       "<user>:awaiting-user", "report question", "--new-workspace", "<user>:orchestrator",
                       "fleet-switchboard status", "gh_users", "gh repo create", "A token is never text"):
            self.assertIn(phrase, text, phrase)
        self.assertEqual(len(re.findall(r"^## STEP \d", text, re.M)), 4)

    def lanes_section(self):
        text = self.SKILL.read_text()
        start = text.index("## Splitting a project into lanes")
        return text[start:]

    def test_s5_the_lanes_section_has_each_element_the_ask_names(self):
        section = self.lanes_section()
        elements = ("### Lane step 1 \u2014 The workspace and the orchestrator", "### Lane step 2 \u2014 The charter",
                    "### Lane step 3 \u2014 The labels", "### Lane step 4 \u2014 The standing orders, copied",
                    "### Lane step 5 \u2014 Coders finishing under their old boss")
        self.assertEqual(sum(1 for e in elements if e in section), 5, "5 of 5 named elements")
        self.assertEqual(len(re.findall(r"^## Splitting a project into lanes$", self.SKILL.read_text(), re.M)), 1)
        self.assertEqual(len(re.findall(r"^## STEP \d", self.SKILL.read_text(), re.M)), 4, "the four commissioning steps are unchanged")

    def test_s14_the_orders_step_copies_word_for_word_and_grants_nothing(self):
        section = self.lanes_section()
        step = section[section.index("### Lane step 4"):section.index("### Lane step 5")]
        self.assertIn("fleet-switchboard orders copy --source <old charter> --charter <new charter>", step, "the copy step names the source")
        self.assertIn("word for word", step)
        self.assertIn("never add one the sibling does not hold", step.replace("\n         ", " "))
        for pattern in (r"\bgrant", r"\bauthori[sz]", r"\bmay (merge|approve|deploy|decide)", r"\bis allowed to", r"\byou (may|can) (approve|merge|deploy|decide)"):   # "permissions allow" is a limit on the Chief of Staff, not a grant to anyone
            self.assertIsNone(re.search(pattern, section, re.I), "0 sentences grant an authority: %s" % pattern)

    def test_s18_the_checkpoint_form_is_what_launch_parses_and_the_handover_template_has_six_sections(self):
        section = self.lanes_section()
        lines = [l.strip() for l in section.splitlines() if l.strip().startswith("CHECKPOINT:")]
        self.assertEqual(len(lines), 1, "1 checkpoint example")
        self.assertEqual(sb.parse_checkpoint(lines[0]), (1800, 3600), "the launch parser accepts it")
        template = section[section.index("A handover note has these six sections"):]
        headings = re.findall(r"^\s+## (.+)$", template, re.M)
        self.assertEqual(headings, list(sb.load_module("lanes").HANDOVER_SECTIONS), "6 of 6 sections, the ones `move` writes")

    LANE_RULE = "Split a lane under the owner's standing order that lets you commission new orchestrators without asking, then tell the person after, citing that order (`orders --cos` or `orders list --charter <n>` shows it); with no such standing order, propose the split first and act only after the person agrees (quote their agreement)."

    def test_s9_the_chief_of_staff_definitions_carry_the_lane_split_rule_once_and_outside_the_permissions(self):
        for name in ("opencode/chief-of-staff.md", "copilot/chief-of-staff.agent.md"):
            text = (REPO / "agents" / name).read_text()
            with self.subTest(file=name):
                self.assertEqual(text.count(self.LANE_RULE), 1, "the rule sentence appears 1 time")
                front = text.split("\n---\n", 1)[0]
                self.assertNotIn("Split a lane", front, "it is prose, never part of the front matter or its permissions")
                self.assertIn("\"Splitting a project into lanes\"", text, "it points to the fleet-setup section that exists")

    def test_s1_the_skill_says_the_same_sentence_once_with_both_branches(self):
        text = self.SKILL.read_text()
        flat = " ".join(text.split())
        self.assertEqual(flat.count(" ".join(self.LANE_RULE.split())), 1, "one source of truth: the same sentence, once")
        self.assertIn("the standing order, read and cited, or the person's agreement, quoted", flat, "step 1 checks which case holds")
        for name in ("opencode/chief-of-staff.md", "copilot/chief-of-staff.agent.md"):
            body = (REPO / "agents" / name).read_text()
            self.assertIn("with no such standing order, propose the split first", body)
            self.assertIn("then tell the person after, citing that order", body)

    def test_s2_the_chief_of_staff_cap_is_the_measured_size_of_its_largest_definition(self):
        sizes = []
        for path in AGENT_FILES:
            if role_of(path) != "chief-of-staff":
                continue
            text = path.read_text()
            if path.parent.name == "opencode":
                text = text.split("\n---\n", 1)[1]
            sizes.append(len(text.encode("utf-8")))
        self.assertEqual(max(sizes), ROLE_BYTE_LIMITS["chief-of-staff"], "the cap rose by exactly what was added: it sits at the new size")

    def test_the_section_the_definitions_point_to_exists_in_the_skill(self):
        self.assertIn("## Splitting a project into lanes", self.SKILL.read_text())

    def test_the_chief_of_staff_loads_it_and_never_creates_a_repo(self):
        for path in AGENT_FILES:
            if role_of(path) != "chief-of-staff":
                continue
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertIn("`fleet-setup`", text)
                self.assertIn("Never run `gh repo create`", text)

    def test_install_links_the_skill(self):
        self.assertIn('ln -s "$PWD/skills/fleet-setup"', (REPO / "INSTALL.md").read_text())

    def test_the_pr15_catalog_is_present_with_a_control_each(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        for sid, key, value in (("M2", "fault", "static-repos"), ("M3", "fault", "one-account"),
                                ("M4", "variant", None), ("M5", "fault", "repo-create-allowed"),
                                ("M6", "variant", None)):
            with self.subTest(scenario=sid):
                self.assertIn(sid, found)
                self.assertEqual(found[sid]["since_pr"], 15)
                self.assertIn("offline", found[sid]["tiers"])
                self.assertIn(key, found[sid]["control"])
                if value:
                    self.assertEqual(found[sid]["control"][key], value)

    def test_the_docs_say_what_pr15_built(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        for phrase in ("| 15 | `switchboard/multi-repo` |", "### Many repos (PR 15)", "gh_users", "github_poll_seconds",
                       "--new-workspace", "owner/repo#N", "static-repos", "bare-issue-number", "one-account",
                       "repo-create-allowed"):
            self.assertIn(phrase, text, phrase)
        self.assertNotIn("TODO(PR 14)", text, "the join with the Decisions list is done, and the Log says so")
        for sid in ("M2", "M3", "M4", "M5", "M6"):
            self.assertRegex(text, r"(?m)^\| %s \|" % sid)


class TestKeyFile(KeyCase):
    def test_the_default_key_file_sits_beside_the_config(self):
        self.assertEqual(sb.jev_key_file(dict(sb.JEV_DEFAULTS)), self.key_file)
        self.assertIsNone(sb.JEV_DEFAULTS["key_file"])

    def test_the_variable_wins_over_the_file(self):
        self.put_key()
        os.environ["OPENROUTER_API_KEY"] = "from-the-environment"
        self.assertEqual(sb.locate_jev_key(dict(sb.JEV_DEFAULTS)), ("from-the-environment", "environment"))

    def test_the_file_is_read_when_the_variable_is_unset(self):
        self.put_key()
        self.assertEqual(sb.locate_jev_key(dict(sb.JEV_DEFAULTS)), (PR16_KEY, "file"))

    def test_the_variable_named_by_key_env_is_the_one_read(self):
        os.environ["MY_JEV_KEY"] = "named"
        self.assertEqual(sb.locate_jev_key(dict(sb.JEV_DEFAULTS, key_env="MY_JEV_KEY")), ("named", "environment"))

    def test_no_key_anywhere_is_none(self):
        self.assertEqual(sb.locate_jev_key(dict(sb.JEV_DEFAULTS)), (None, "none"))

    def test_a_file_anyone_else_could_read_is_refused_and_says_why(self):
        for mode in (0o640, 0o604, 0o660, 0o644):
            with self.subTest(mode=oct(mode)):
                self.put_key(mode=mode)
                with self.assertRaises(sb.KeyFileError) as caught:
                    sb.locate_jev_key(dict(sb.JEV_DEFAULTS))
                self.assertIn("0600", str(caught.exception))
                self.assertIn(str(self.key_file), str(caught.exception))
                self.assertNotIn(PR16_KEY, str(caught.exception))

    def test_a_file_you_may_read_and_write_alone_is_taken(self):
        for mode in (0o600, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.put_key(mode=mode)
                self.assertEqual(sb.locate_jev_key(dict(sb.JEV_DEFAULTS))[1], "file")

    def test_a_link_a_directory_a_stranger_s_file_and_an_empty_file_are_refused(self):
        real = self.home / "elsewhere.key"
        real.write_text(PR16_KEY)
        os.chmod(str(real), 0o600)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real), str(self.key_file))
        with self.assertRaises(sb.KeyFileError) as caught:
            sb.locate_jev_key(dict(sb.JEV_DEFAULTS))
        self.assertIn("regular file", str(caught.exception))
        self.key_file.unlink()
        self.key_file.mkdir()
        with self.assertRaises(sb.KeyFileError):
            sb.locate_jev_key(dict(sb.JEV_DEFAULTS))
        self.key_file.rmdir()
        self.put_key()
        with mock.patch.object(sb.os, "getuid", return_value=os.getuid() + 1), \
                self.assertRaises(sb.KeyFileError) as caught:
            sb.locate_jev_key(dict(sb.JEV_DEFAULTS))
        self.assertIn("owned by you", str(caught.exception))
        self.key_file.write_text("  \n")
        os.chmod(str(self.key_file), 0o600)
        with self.assertRaises(sb.KeyFileError) as caught:
            sb.locate_jev_key(dict(sb.JEV_DEFAULTS))
        self.assertIn("empty", str(caught.exception))

    def test_a_key_file_path_in_the_config_is_used_and_must_be_absolute(self):
        other = self.home / "custom.key"
        other.write_text(PR16_KEY)
        os.chmod(str(other), 0o600)
        config = sb.validate_config({"jev": {"key_file": str(other)}})
        self.assertEqual(sb.locate_jev_key(config["jev"]), (PR16_KEY, "file"))
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"jev": {"key_file": "relative.key"}})
        self.assertIn("key_file", str(caught.exception))

    def test_the_client_sends_the_file_s_key_only_as_the_authorization_header(self):
        self.put_key()
        opener = FakeOpener(jev_reply(noul_answers(q=0.2)))
        client = sb.JevClient(dict(JEV_SETTINGS), opener=opener)
        out = client.decide({"x": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(opener.calls[0]["headers"]["Authorization"], "Bearer " + PR16_KEY)
        self.assertNotIn(PR16_KEY, json.dumps(opener.calls[0]["body"]))
        self.assertNotIn(PR16_KEY, json.dumps(out))

    def test_a_refused_file_makes_the_client_fail_open_without_the_key(self):
        self.put_key(mode=0o644)
        client = sb.JevClient(dict(JEV_SETTINGS), opener=FakeOpener(jev_reply(noul_answers(q=0.2))))
        with self.assertRaises(sb.JevError) as caught:
            client.decide({"x": 1}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(caught.exception.reason, "unauthorized")
        self.assertNotIn(PR16_KEY, str(caught.exception) + caught.exception.detail)


class TestTodolistPlugin(Base):
    """The vendored todo plugin: src/ copied beside a one-line loader in plugins/, temp config dirs only."""

    def setUp(self):
        super().setUp()
        self.config = self.home / "cfg"
        self.src = REPO / "plugins" / "opencode-todolist" / "src"
        self.loader = self.config / "plugins" / "opencode-todolist.ts"

    def test_a_fresh_install_copies_every_src_file_byte_for_byte_and_writes_the_loader(self):
        self.assertTrue(sb.install_todolist_plugin(self.config, REPO))
        files = [p for p in self.src.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertEqual((self.config / "opencode-todolist" / path.relative_to(self.src)).read_bytes(), path.read_bytes())
        self.assertEqual(self.loader.read_text(), 'export { default } from "../opencode-todolist/src/index.ts"\n')
        self.assertEqual(sb.todolist_plugin_status(self.config, REPO), "already installed")

    def test_a_second_run_changes_nothing(self):
        sb.install_todolist_plugin(self.config, REPO)
        before = tree_of(self.config)
        self.assertFalse(sb.install_todolist_plugin(self.config, REPO))
        self.assertEqual(tree_of(self.config), before)

    def test_a_loader_the_kit_did_not_write_is_not_overwritten(self):
        self.loader.parent.mkdir(parents=True)
        self.loader.write_text("// mine\n")
        with self.assertRaises(sb.InstallError):
            sb.install_todolist_plugin(self.config, REPO)
        self.assertEqual(self.loader.read_text(), "// mine\n")
        self.assertFalse((self.config / "opencode-todolist").exists())

    def test_a_src_file_the_kit_did_not_write_is_not_overwritten(self):
        (self.config / "opencode-todolist").mkdir(parents=True)
        (self.config / "opencode-todolist" / "index.ts").write_text("// mine\n")
        with self.assertRaises(sb.InstallError):
            sb.install_todolist_plugin(self.config, REPO)
        self.assertEqual((self.config / "opencode-todolist" / "index.ts").read_text(), "// mine\n")
        self.assertFalse(self.loader.exists())

    def test_status_says_differs_from_the_kit_after_an_edit_and_a_rerun_restores_it(self):
        self.assertEqual(sb.todolist_plugin_status(self.config, REPO), "not installed")
        sb.install_todolist_plugin(self.config, REPO)
        (self.config / "opencode-todolist" / "todos.ts").write_text("edited\n")
        self.assertEqual(sb.todolist_plugin_status(self.config, REPO), "differs from the kit")
        self.assertTrue(sb.install_todolist_plugin(self.config, REPO))
        self.assertEqual(sb.todolist_plugin_status(self.config, REPO), "already installed")

    def test_uninstall_removes_only_what_the_kit_wrote(self):
        sb.install_todolist_plugin(self.config, REPO)
        (self.config / "opencode-todolist" / "todos.ts").write_text("edited\n")
        (self.config / "plugins" / "other.js").write_text("x")
        self.assertTrue(sb.uninstall_todolist_plugin(self.config, REPO))
        self.assertFalse(self.loader.exists())
        self.assertEqual(sorted(p.name for p in (self.config / "opencode-todolist").iterdir()), ["todos.ts"])
        self.assertTrue((self.config / "plugins" / "other.js").exists())
        (self.config / "opencode-todolist" / "todos.ts").unlink()
        self.assertFalse(sb.uninstall_todolist_plugin(self.config, REPO))
        self.assertFalse((self.config / "opencode-todolist").exists())

    def test_uninstall_of_a_foreign_loader_leaves_it(self):
        self.loader.parent.mkdir(parents=True)
        self.loader.write_text("// mine\n")
        self.assertFalse(sb.uninstall_todolist_plugin(self.config, REPO))
        self.assertEqual(self.loader.read_text(), "// mine\n")


class ReaperOnlyFakes:
    """The reaper-only install (ask #982): the parser's own arguments, the fake machine, the fake home."""

    # A file the LAUNCHER's own reader accepts: a digest image, an https server with a DNS host, a GUID subscription.
    CLOUD_ENV_GOOD = ("CW_ALLOWED_REPOS=acme-org/widget-service\nCW_CLOUD_IMAGE=registry.acme.example/widget@sha256:" + "ab" * 32 + "\n"
                      "CW_CLOUD_SERVER=https://api.acme.example:6443\nCW_CLOUD_NAMESPACE=acme-cw\n"
                      "CW_VAULT_NAME=acme-vault\nCW_VAULT_SUBSCRIPTION=00000000-0000-4000-8000-000000000001\n")

    def ensure_cloud_env(self, text=None, mode=0o600):
        """The settings file the reaper-only install requires, as a good one: 0600, the user's own, the cluster login names
        and the vault names (the token file name comes from the environment of the install, set here for the test).
        Written only when there is none yet. Returns its path."""
        path = sb.cloud_settings_path()
        patcher = mock.patch.dict(os.environ, {"CW_CLOUD_TOKEN_FILE": "/home/user/.cw/token"})
        patcher.start()
        self.addCleanup(patcher.stop)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(str(path.parent), 0o700)   # the launcher refuses a directory others can write, whatever the umask of the test
            path.write_text(self.CLOUD_ENV_GOOD if text is None else text)
            os.chmod(str(path), mode)
        return path

    def reaper_only(self, *flags, hook=None):
        """`install --cloud-reaper-only` as the parser gives it. Returns (output, error)."""
        self.ensure_cloud_env()
        out = io.StringIO()
        args = sb.build_parser().parse_args(["install", "--cloud-reaper-only"] + [f for f in flags if f != "--dry-run"] + (
            ["--dry-run"] if "--dry-run" in flags else []))
        installer = sb.Installer(self.machine, self.daemons, time.time, self.PLATFORM, REPO,
                                 dry_run=args.dry_run, out=out, shared_profile=args.shared_profile,
                                 profile=args.profile, gh_user=args.gh_user)
        if hook is not None:
            hook(installer)
        try:
            installer.install_cloud_reaper_only()
        except sb.InstallError as err:
            return out.getvalue(), str(err)
        return out.getvalue(), None

    def run_install(self, *argv):
        """`install` as the CLI runs it: the parser's arguments, the fake machine, the fake daemon. (code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        args = sb.build_parser().parse_args(["install"] + list(argv))
        real = sb.Installer

        def installer(runner, spawn, now, **kwargs):   # the platform and the kit of the test, not the real ones
            return real(runner, spawn, now, self.PLATFORM, REPO, **kwargs)

        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sb, "Installer", installer):
            code = sb.cmd_install(args, self.machine, time.time, self.daemons)
        return code, out.getvalue(), err.getvalue()

    def leaves(self, before):
        """The new paths under the fake home that hold something: the ones no other new path is inside."""
        after = tree_of(self.home)
        fresh = sorted(path for path in after if path not in before)
        found = []
        for path in fresh:
            prefix = path if path.endswith("/") else path + "/"
            if not any(other.startswith(prefix) for other in fresh if other != path):
                found.append(path)
        return found

    def step_names(self):
        """The installer's step methods, from its own code: a new one must be classified."""
        return sorted(set(re.findall(r"def (step_\w+)\(", class_source("Installer"))))

    def installer_functions(self):
        """The module-level functions the installer's steps call, from the code: a new one must be classified."""
        tree = ast.parse(switchboard_source())   # the entry and every part
        module = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        installer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Installer")
        called = {node.func.id for node in ast.walk(installer)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        return sorted(called & module)

    def reaper_path(self):
        if self.PLATFORM == "darwin":
            return self.home / "Library" / "LaunchAgents" / "com.fleet-kit.cloud-reaper.plist"
        return self.home / "xdg-config" / "systemd" / "user" / "fleet-kit-cloud-reaper.service"

    def fresh_state_dir(self):
        """The state directory as a machine that has never run the switchboard has it: not there."""
        shutil.rmtree(str(sb.state_dir_path()), ignore_errors=True)

    def reaper_load_calls(self):
        return [c for c in self.machine.calls if "cloud-reaper" in " ".join(c) and any(word in ("bootstrap", "enable") for word in c)]


class TestTheReaperOnlyDryRunLive(Base):
    """The real command, a throwaway home and a stub service manager on PATH: nothing real is loaded."""

    def kit(self):
        """A copy of the kit in a private directory: the reaper's own path walk refuses a checkout under a
        world-writable directory, and the run must not touch the checkout it runs in."""
        root = Path(os.path.realpath(str(self.home))) / "kit"
        shutil.copytree(REPO / "bin", root / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "cloud", root / "cloud", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        shutil.copytree(REPO / "templates", root / "templates")
        return root

    def stub(self, name, body):
        path = self.home / "stub" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        os.chmod(str(path), 0o755)
        return path

    def test_the_real_command_prints_the_step_and_writes_nothing(self):
        root = self.kit()
        self.stub("launchctl", "#!/bin/sh\ncase \"$1\" in print) exit 113 ;; *) exit 1 ;; esac\n")
        self.stub("systemctl", "#!/bin/sh\ncase \"$*\" in *is-enabled*) exit 1 ;; *) exit 1 ;; esac\n")
        home = self.home / "throwaway"
        home.mkdir()
        settings = home / ".config" / "fleet-switchboard" / "cloud.env"
        settings.parent.mkdir(parents=True)
        settings.write_text(ReaperOnlyFakes.CLOUD_ENV_GOOD)
        os.chmod(str(settings), 0o600)
        env = {"HOME": str(home), "CW_CLOUD_TOKEN_FILE": "/home/user/.cw/token", "PATH": "%s:%s" % (self.home / "stub", os.environ.get("PATH", "")),
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local" / "state"),
               "FLEET_SWITCHBOARD_UNDER_TEST": "1"}
        done = subprocess.run([sys.executable, "-B", str(root / "bin" / "fleet-switchboard"), "install",
                               "--cloud-reaper-only", "--dry-run"], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("cloud reaper", done.stdout)
        self.assertIn("would run", done.stdout)
        self.assertEqual(sorted(str(p) for p in home.rglob("*")), sorted([str(home / ".config"), str(settings.parent), str(settings)]))


class TestInstallPutsTheCliOnPath(InstallCase):
    """The Chief of Staff's shell must find `fleet-switchboard`."""

    def setUp(self):
        super().setUp()
        self.target = REPO / "bin" / "fleet-switchboard"
        self.local = self.home / ".local" / "bin"
        self.link = self.local / "fleet-switchboard"

    def full(self, *flags, **kw):
        return self.install("--opencode", self.machine.binary, *flags, **kw)

    def on_path(self, *dirs):
        return mock.patch.dict(os.environ, {"PATH": ":".join([str(d) for d in dirs] + [SAFE_PATH])})

    def test_it_links_into_local_bin_says_where_and_prints_the_line_when_that_is_not_on_path(self):
        out, error = self.full()
        self.assertIsNone(error, error)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(str(self.link)), str(self.target))
        self.assertEqual(self.statuses(out)["cli link"], ["done"])
        self.assertIn(str(self.local), out)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', out)

    def test_it_says_a_moved_checkout_breaks_the_link_and_install_repairs_it(self):
        out, _ = self.full()
        text = " ".join(out.split())
        self.assertIn("moved or deleted checkout", text)
        self.assertIn("`fleet-switchboard install` again", text)

    def test_local_bin_on_the_path_needs_no_line(self):
        with self.on_path(self.local):
            out, error = self.full()
        self.assertTrue(self.link.is_symlink())
        self.assertNotIn("export PATH", out)
        self.assertIn("on your PATH", out)

    def test_without_local_bin_on_the_path_the_first_directory_under_home_that_is(self):
        mine = self.home / "bin"
        mine.mkdir()
        with self.on_path("/usr/local/bin", mine):
            out, error = self.full()
        self.assertTrue((mine / "fleet-switchboard").is_symlink())
        self.assertFalse(self.link.exists())
        self.assertIn(str(mine), out)
        self.assertNotIn("export PATH", out)

    def test_a_second_run_changes_nothing_and_says_already_done(self):
        self.full()
        before = tree_of(self.home)
        out, _ = self.full()
        self.assertEqual(self.statuses(out)["cli link"], ["already done"])
        self.assertEqual(tree_of(self.home), before)

    def test_a_correct_link_made_by_hand_reads_already_done(self):
        self.local.mkdir(parents=True)
        os.symlink(str(self.target), str(self.link))
        out, _ = self.full()
        self.assertEqual(self.statuses(out)["cli link"], ["already done"])
        self.assertEqual(os.readlink(str(self.link)), str(self.target))

    def test_a_different_file_is_never_overwritten_and_is_reported(self):
        self.local.mkdir(parents=True)
        self.link.write_text("mine\n")
        out, error = self.full()
        self.assertIsNone(error, error)
        self.assertEqual(self.link.read_text(), "mine\n")
        self.assertTrue(self.statuses(out)["cli link"][0].startswith("skipped"), out)
        self.assertIn(str(self.link), out)
        self.assertIn("left alone", out)

    def test_a_link_to_a_different_existing_file_is_left_alone_too(self):
        elsewhere = self.home / "elsewhere" / "fleet-switchboard"
        elsewhere.parent.mkdir()
        elsewhere.write_text("#!/bin/sh\n")
        self.local.mkdir(parents=True)
        os.symlink(str(elsewhere), str(self.link))
        out, _ = self.full()
        self.assertEqual(os.readlink(str(self.link)), str(elsewhere))
        self.assertIn("left alone", out)

    def test_a_link_to_a_checkout_that_is_gone_is_repaired(self):
        self.local.mkdir(parents=True)
        os.symlink(str(self.home / "old-checkout" / "bin" / "fleet-switchboard"), str(self.link))
        out, _ = self.full()
        self.assertEqual(os.readlink(str(self.link)), str(self.target))
        self.assertEqual(self.statuses(out)["cli link"], ["done"])

    def test_a_dry_run_writes_nothing(self):
        before = tree_of(self.home)
        out, _ = self.full("--dry-run")
        self.assertEqual(tree_of(self.home), before)
        self.assertFalse(self.local.exists())
        self.assertEqual(self.statuses(out)["cli link"], ["would be done"])

    def test_uninstall_removes_only_a_link_that_points_at_this_checkout(self):
        self.full()
        out, error = self.install("--uninstall")
        self.assertIsNone(error, error)
        self.assertFalse(self.link.is_symlink() or self.link.exists())
        self.assertEqual(self.statuses(out)["cli link"], ["done"])
        again, _ = self.install("--uninstall")
        self.assertNotEqual(self.statuses(again)["cli link"], ["done"])

    def test_uninstall_leaves_a_file_and_a_link_that_are_not_ours(self):
        self.local.mkdir(parents=True)
        self.link.write_text("mine\n")
        self.install("--uninstall")
        self.assertEqual(self.link.read_text(), "mine\n")
        self.link.unlink()
        elsewhere = self.home / "elsewhere" / "fleet-switchboard"
        elsewhere.parent.mkdir()
        elsewhere.write_text("#!/bin/sh\n")
        os.symlink(str(elsewhere), str(self.link))
        self.install("--uninstall")
        self.assertEqual(os.readlink(str(self.link)), str(elsewhere))

    def test_uninstall_dry_run_removes_nothing(self):
        self.full()
        before = tree_of(self.home)
        out, _ = self.install("--uninstall", "--dry-run")
        self.assertEqual(tree_of(self.home), before)
        self.assertEqual(self.statuses(out)["cli link"], ["would be done"])


class TestExternalAdapterKeys(ExternalAdapterCase):
    def test_env_source(self):
        self.assertEqual(EA.load_key(EA.parse_key_source("env:K"), environ={"K": " abc \n"}).reveal(), "abc")
        with self.assertRaises(EA.KeySourceError):
            EA.load_key(EA.parse_key_source("env:K"), environ={})

    def test_file_source_needs_mode_0600(self):
        path = self.tmp / "key"
        path.write_text("file-key-value\n")
        os.chmod(path, 0o600)
        self.assertEqual(EA.load_key(EA.parse_key_source("file:%s" % path)).reveal(), "file-key-value")
        for mode in (0o644, 0o640, 0o604, 0o660):
            os.chmod(path, mode)
            with self.assertRaises(EA.KeySourceError) as ctx:
                EA.load_key(EA.parse_key_source("file:%s" % path))
            self.assertIn("0600", str(ctx.exception))
            self.assertNotIn("file-key-value", str(ctx.exception))
        with self.assertRaises(EA.KeySourceError):
            EA.load_key(EA.parse_key_source("file:%s" % (self.tmp / "absent")))

    def test_command_source_runs_without_a_shell_and_is_held_in_memory(self):
        argv = [sys.executable, "-c", "print('cmd-key-value')"]
        self.assertEqual(EA.load_key(EA.parse_key_source({"command": argv})).reveal(), "cmd-key-value")
        self.assertEqual(EA.load_key(EA.parse_key_source("command:" + json.dumps(argv))).reveal(), "cmd-key-value")
        # a shell metacharacter is an argument, not a shell
        marker = self.tmp / "ran"
        argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", "x; touch %s" % marker]
        EA.load_key(EA.parse_key_source({"command": argv}))
        self.assertFalse(marker.exists())

    def test_command_timeout_failure_and_empty_output(self):
        slow = [sys.executable, "-c", "import time; time.sleep(5)"]
        started = time.time()
        with self.assertRaises(EA.KeySourceError) as ctx:
            EA.load_key(EA.parse_key_source({"command": slow, "timeout": 0.3}))
        self.assertIn("did not answer", str(ctx.exception))
        self.assertLess(time.time() - started, 3)
        bad = [sys.executable, "-c", "import sys; print('leaked-key-value'); sys.exit(3)"]
        with self.assertRaises(EA.KeySourceError) as ctx:
            EA.load_key(EA.parse_key_source({"command": bad}))
        self.assertIn("exit 3", str(ctx.exception))
        self.assertNotIn("leaked-key-value", str(ctx.exception))
        with self.assertRaises(EA.KeySourceError):
            EA.load_key(EA.parse_key_source({"command": [sys.executable, "-c", "pass"]}))
        with self.assertRaises(EA.KeySourceError):
            EA.load_key(EA.parse_key_source({"command": ["/nonexistent/binary-xyz"]}))

    def test_the_key_never_appears_in_any_text(self):
        key = FakeDoorServer.KEY
        adapter = self.adapter("http")
        texts = [repr(adapter.keys.current()), str(adapter.keys.current()), repr(adapter.keys)]
        with self.assertRaises(EA.CredentialError) as ctx:  # the server echoes the credential it was shown
            self.server.fail_next("raise", 401, {"code": "cred", "message": "refused Key %s for %s" % (key, key)})
            self.push(adapter, self.decision(), "k-1")
        texts.append(str(ctx.exception))
        self.assertIn("[key]", str(ctx.exception))
        self.server.fail_next("raise", 400, {"code": "bad", "message": "Bearer %s" % key})
        with self.assertRaises(EA.InvalidRequest) as ctx:
            self.push(adapter, self.decision(), "k-2")
        texts.append(str(ctx.exception))
        self.server.fail_next("raise", 500, {"code": "x", "message": key}, count=5)
        with self.assertRaises(EA.Unavailable) as ctx:
            self.push(adapter, self.decision(), "k-3")
        texts.append(str(ctx.exception))
        self.server.down = True
        texts.append(str(adapter.health()))
        self.server.down = False
        self.server.fail_next(None, 401, {"code": "cred", "message": key})
        texts.append(str(adapter.health()))
        for text in texts:
            self.assertNotIn(key, text)
        with self.assertRaises(TypeError):
            import pickle
            pickle.dumps(adapter.keys.current())
        self.assertEqual(EA.scrub("a %s b Bearer abc.def" % key, [EA.Secret(key)]), "a [key] b Bearer [key]")

    def test_a_refused_key_rotates_to_the_next_source(self):
        env = {"OLD": "stale-key", "NEW": FakeDoorServer.KEY}
        data = fake_mapping(self.server.url, "http", key_source=["env:OLD", "env:NEW"])
        adapter = EA.McpRestAdapter(data, sleep=self.slept.append, environ=env)
        self.assertEqual(self.push(adapter, self.decision(), "k-1")[:3], "it-")
        self.assertEqual(adapter.keys.index, 1)

    def test_a_bad_source_is_found_at_start(self):
        adapter = EA.McpRestAdapter(fake_mapping(self.server.url), environ={})
        self.assertIn("not set", adapter.start())
        self.assertIsNone(self.adapter().start())
        self.assertIn("not set", adapter.health())


class TestExternalAdapterOperations(ExternalAdapterCase):
    def each_channel(self):
        for channel in ("tool", "http"):
            self.setUp()
            yield channel
            self.server.close()

    def test_raise_maps_the_decision_over_both_channels(self):
        for channel in self.each_channel():
            adapter = self.adapter(channel)
            ident = self.push(adapter, self.decision(), "decision-key-1")
            sent = [a for c, o, a in self.server.requests if o == "raise"][0]
            self.assertEqual(self.server.log[-1], (channel, "raise"))
            self.assertEqual(sent["label"], "routine", channel)
            self.assertEqual(sent["floor"], "minor")
            self.assertEqual(sent["seat"], "seat-1")
            self.assertEqual(sent["body"], "Ship it?\n\nThe build is green.\n\nref: dk:decision-key-1")
            self.assertEqual(sent["advice"], "yes")
            self.assertEqual(sent["choices"], [{"name": "approve", "key": "c1"},
                                               {"name": "hold", "price": "a day", "key": "c2"}])
            self.assertEqual(str(uuid.UUID(sent["op_id"])), sent["op_id"])
            self.assertEqual(self.server.items[ident]["floor"], "minor")

    def test_the_floor_follows_the_tier_and_a_hint_may_only_raise_it(self):
        adapter = self.adapter("http")
        for tier, hint, floor in (("orchestrator", None, "minor"), ("cos", None, "major"), ("human", None, "sealed"),
                                  ("orchestrator", "moderate", "middling"), ("cos", "low", "major")):
            self.push(adapter, self.decision(tier=tier, consequence=hint), "k-%s-%s" % (tier, hint))
            self.assertEqual(self.server.requests[-1][2]["floor"], floor)
        with self.assertRaises(EA.InvalidRequest):
            self.push(adapter, self.decision(tier="nobody"), "k-x")

    def test_options_with_their_own_ids_and_optional_fields(self):
        adapter = self.adapter("tool")
        self.push(adapter, self.decision(options=[{"label": "a", "id": "mine-1", "recommended": True}],
                                   grounds="g", stakes="s", evidence=[{"text": "t", "link": "http://127.0.0.1/e"}]),
                     "k-o")
        sent = self.server.requests[-1][2]
        self.assertEqual(sent["choices"], [{"name": "a", "preferred": True, "key": "mine-1"}])
        self.assertEqual((sent["because"], sent["stakes"]), ("g", "s"))
        self.assertEqual(sent["refs"], [{"note": "t", "href": "http://127.0.0.1/e"}])

    def test_a_recommendation_is_required_or_defaulted(self):
        adapter = self.adapter("http")
        self.push(adapter, self.decision(recommendation=None), "k-d")
        self.assertEqual(self.server.requests[-1][2]["advice"], "proceed")
        data = fake_mapping(self.server.url, "http")
        data.pop("defaults")
        bare = EA.McpRestAdapter(data, environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY})
        with self.assertRaises(EA.InvalidRequest):
            self.push(bare, self.decision(recommendation=None), "k-e")

    def test_revise_withdraw_and_acted_over_both_channels(self):
        for channel in self.each_channel():
            adapter = self.adapter(channel)
            ident = self.push(adapter, self.decision(), "k-1")
            self.assertEqual(self.push(adapter, {"op": "revise", "external_id": ident, "body": "new words"}, "k-r"), ident)
            self.assertEqual(self.server.items[ident]["body"], "new words\n\nref: dk:k-r")
            self.assertEqual(self.server.log[-1], (channel, "revise"))
            self.server.items[ident]["answer"] = {"answer_id": "a-1"}
            self.assertEqual(adapter.acted(ident, "a-1", "done", "did it", "k-a"), ident)
            self.assertEqual(self.server.requests[-1][2]["result"], "did")
            with self.assertRaises(EA.Conflict):
                adapter.acted(ident, "not-an-answer", "done")
            second = self.push(adapter, self.decision(), "k-2")
            self.push(adapter, {"external_id": second, "status": "withdrawn", "reason": "gone"}, "k-w")
            self.assertEqual(self.server.items[second]["withdrawn"], {"why": "gone"})
            self.assertEqual(self.server.log[-1], (channel, "withdraw"))
            self.push(adapter, {"op": "withdraw", "external_id": second}, "k-w")  # idempotent

    def test_revise_and_withdraw_after_a_decision_are_refused(self):
        adapter = self.adapter("tool")
        ident = self.push(adapter, self.decision(), "k-1")
        self.server.items[ident].update(state="closed", answer={"answer_id": "a-1"})
        with self.assertRaises(EA.AlreadyDecided):
            self.push(adapter, {"op": "revise", "external_id": ident, "body": "late"}, "k-r")
        with self.assertRaises(EA.AlreadyDecided):
            self.push(adapter, {"op": "withdraw", "external_id": ident}, "k-w")
        with self.assertRaises(EA.InvalidRequest):
            self.push(adapter, {"op": "revise", "external_id": ident}, "k-r2")

    def test_status_without_a_mapping_for_it_is_a_local_only_no_op_not_an_error(self):
        adapter = self.adapter()
        self.assertNotIn("status", adapter.capabilities)
        before = len(self.server.log)
        self.assertEqual(self.push(adapter, {"op": "status", "external_id": "x", "status": "s"}, "k"), "x")
        self.assertEqual(len(self.server.log), before, "nothing was sent: the engine must not retry it forever")

    def test_idempotent_replay_and_conflict(self):
        for channel in self.each_channel():
            adapter = self.adapter(channel)
            first = self.push(adapter, self.decision(), "same-key")
            self.assertEqual(self.push(adapter, self.decision(), "same-key"), first)
            self.assertEqual(len(self.server.items), 1)
            with self.assertRaises(EA.Conflict):
                self.push(adapter, self.decision(body="a different body"), "same-key")
            other = self.push(adapter, self.decision(), "another-key")
            self.assertNotEqual(other, first)
            # a key that already is a UUID is kept as it is
            ident = str(uuid.uuid4())
            self.push(adapter, self.decision(), ident)
            self.assertEqual(self.server.requests[-1][2]["op_id"], ident)

    def test_lookup_finds_an_item_by_its_decision_key(self):
        adapter = self.adapter("http")
        first = self.push(adapter, self.decision(), "k-1")
        self.assertEqual(adapter.lookup("dk:k-1"), {"external_id": first, "revision": 1})
        self.assertIsNone(adapter.lookup("dk:never-raised"))
        self.assertIsNone(adapter.lookup("dk:k"))  # a prefix of a key is not the key
        self.server.items[first]["withdrawn"] = {"why": "x"}
        self.assertIsNone(adapter.lookup("dk:k-1"))
        bare = fake_mapping(self.server.url, "http")
        bare["options"] = {"id_prefix": "c"}
        self.assertIsNone(EA.McpRestAdapter(bare, environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY}).lookup("dk:k-1"))

    def test_read_items_are_normalised(self):
        adapter = self.adapter("tool")
        ident = self.push(adapter, self.decision(), "k-1")
        self.server.items[ident].update(state="closed", answer=self.answer_fields(), ask_back=None)
        found = adapter.read_items()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["external_id"], ident)
        self.assertEqual(found[0]["status"], "decided")
        self.assertEqual(found[0]["answer"]["option_id"], "k1")
        self.assertEqual(found[0]["answer"]["by"]["role"], "human")

    def test_health(self):
        adapter = self.adapter("http")
        self.assertIsNone(adapter.health())
        self.server.disabled = True
        self.assertIn("disabled", adapter.health())
        self.server.disabled = False
        self.server.down = True
        self.assertIn("request failed", adapter.health())
        self.server.down = False
        data = fake_mapping(self.server.url)
        data["key_source"] = "env:NOT_SET_ANYWHERE"
        self.assertIn("not set", EA.McpRestAdapter(data, environ={}).health())


class TestExternalAdapterPort(ExternalAdapterCase):
    """The step-4 port shape: push(operation) -> {external_id, url}, pull_changes -> (events, cursor)."""

    def test_pull_changes_yields_port_events_only(self):
        s = self.server
        s.add_change("rating", "it-1", level="major", floor="minor")
        s.add_change("answer", "it-1", **self.answer_fields())
        s.add_change("answer", "it-2", **self.answer_fields(answer_id="a-2", how="ask", chosen=None, words=None,
                                                           ask_back="which?", who={"type": "bot"}))
        s.add_change("flip", "it-1", **self.answer_fields(answer_id="a-3", how="no", chosen=None, words="x",
                                                         no_reason="nope", who={"type": "owner_bot"}),
                     replaces=self.answer_fields())
        s.add_change("pull", "it-3", why="gone")
        s.add_change("restated", "it-4", body="b", rerate=True)
        s.add_change("answer", "it-5", **self.answer_fields(answer_id="a-5", who={"type": "stranger"}))
        for channel in ("tool", "http"):
            events, cursor = self.adapter(channel).pull_changes(None)
            self.assertEqual([e["type"] for e in events], ["answered", "clarify", "reversed", "withdrawn", "answered"])
            a, c, r, w, u = events
            self.assertEqual((a["answered_by"], a["option_id"], a["text"], a["external_id"]),
                             ("human", "k1", "approve", "it-1"))
            self.assertEqual((c["answered_by"], c["text"], c["option_id"]), ("project agent", "which?", None))
            self.assertEqual((r["answered_by"], r["text"], r["replaces"]["id"]), ("owner's agent", "nope", "a-1"))
            self.assertEqual(w["text"], "gone")
            self.assertEqual(u["answered_by"], "unknown")
            self.assertEqual(cursor, 106 + 1)  # the cursor moves past the changes that are not port events
            for e in events:
                self.assertTrue({"event_id", "external_id", "type", "answered_by", "option_id", "text", "at"} <= set(e))

    def test_a_raise_returns_the_id_and_the_operation_floor_is_used(self):
        adapter = self.adapter("tool")
        operation = {"op": "raise", "key": "dk:1", "external_id": None, "revision": 1, "floor": "high",
                     "decision": {"title": "T", "headline": "A longer headline", "tier": "orchestrator",
                                  "agent": "coder1", "repo": "acme/api", "ask": 7, "display_id": "#7",
                                  "rows": {"1": "first", "2": "second"}}}
        done = adapter.push(operation, "dk:1")
        self.assertEqual(set(done), {"external_id", "url"})
        sent = self.server.requests[-1][2]
        self.assertEqual(sent["floor"], "major")  # the operation's floor, not the tier_map's
        self.assertEqual(sent["label"], "routine")
        self.assertEqual(sent["body"], "T\n\nA longer headline\n\nFrom: coder1 in acme/api on #7\n\n"
                                       "1. first\n2. second\n\nref: dk:1")
        again = adapter.push(operation, "dk:1")
        self.assertEqual(again, done)
        operation.update(op="revise", external_id=done["external_id"], revision=2)
        operation["decision"]["title"] = "T2"
        self.assertEqual(adapter.push(operation, "dk:1:r2")["external_id"], done["external_id"])
        operation.update(op="withdraw", note="superseded")
        self.assertEqual(adapter.push(operation, "dk:1:w")["external_id"], done["external_id"])
        self.assertEqual(self.server.items[done["external_id"]]["withdrawn"], {"why": "superseded"})
        with self.assertRaises(EA.InvalidRequest):
            adapter.push({"op": "dance", "decision": {}}, "k")
        with self.assertRaises(EA.InvalidRequest):
            adapter.push({"op": "revise", "external_id": None, "decision": {"title": "x"}}, "k")

    def test_an_error_is_raised_for_the_engine_to_retry(self):
        adapter = self.adapter("http")
        self.server.down = True
        with self.assertRaises(Exception):
            adapter.pull_changes(None)

    def test_every_page_of_a_truncated_read_is_followed_over_both_channels(self):
        for channel in ("tool", "http"):
            self.server.items.clear()
            self.server.changes.clear()
            self.server.page_size = 2
            adapter = self.adapter(channel)
            ids = [self.push(adapter, self.decision(title="item %d" % n), "k-%d-%s" % (n, channel)) for n in range(5)]
            self.assertEqual(sorted(i["external_id"] for i in adapter.read_items()), sorted(ids))
            self.assertEqual(adapter.lookup("dk:k-4-%s" % channel)["external_id"], ids[4], "found on the last page")
            for n in range(5):
                self.server.add_change("answer", ids[n], **self.answer_fields(answer_id="a-%d" % n))
            events, cursor = adapter.pull_changes(None)
            self.assertEqual([e["answer_id"] for e in events], ["a-%d" % n for n in range(5)])
            self.assertEqual(cursor, self.server.seq)
            self.assertEqual(adapter.pull_changes(cursor), ([], cursor))
            self.assertGreaterEqual(self.server.count("feed", channel), 4)

    def test_permanent_errors_are_marked_and_transient_ones_are_not(self):
        for cls in (EA.CredentialError, EA.DisabledError, EA.NotFound, EA.InvalidRequest, EA.Conflict,
                    EA.AlreadyDecided, EA.MappingError, EA.KeySourceError, EA.Unsupported):
            self.assertTrue(cls("x").permanent, cls.__name__)
        self.assertFalse(EA.RateLimited("x", 1).permanent)
        self.assertFalse(EA.Unavailable("x").permanent)
        adapter = self.adapter("http")
        for status, code, permanent in ((404, "nf", True), (400, "bad", True), (401, "cred", True),
                                        (409, "decided", True), (403, "off", True), (500, "x", False),
                                        (429, "slow", False)):
            self.server.inject.clear()
            self.server.fail_next("raise", status, {"code": code, "message": "m"}, {"Retry-After": "99"}, count=9)
            with self.assertRaises(EA.AdapterError) as ctx:
                self.push(adapter, self.decision(), "k-%d" % status)
            self.assertIs(ctx.exception.permanent, permanent, status)

    def test_a_cursor_that_goes_back_is_ignored(self):
        adapter = self.adapter("http")
        adapter.door.call = lambda *a, **k: {"cursor": 3, "feed": []}
        self.assertEqual(adapter.pull_changes(10)[1], 10)

    def test_scrub_takes_out_a_key_with_url_unsafe_characters(self):
        self.assertEqual(EA.scrub("x a/b+c y", [EA.Secret("a/b+c")]), "x [key] y")
        self.assertEqual(EA.scrub("x a%2Fb%2Bc y", [EA.Secret("a/b+c")]), "x [key] y")


class TestExternalAdapterWork(ExternalAdapterCase):
    def test_put_is_idempotent_and_goes_over_http_only(self):
        adapter = self.adapter("tool")
        first = adapter.work.put("w-1", "Build the thing", kind="assignment", state="running", doer="coder")
        self.assertTrue(first["recorded"])
        self.assertEqual(first["item"]["state"], "going")
        again = adapter.work.put("w-1", "Build the thing", kind="assignment", state="running", doer="coder")
        self.assertFalse(again["recorded"])
        self.assertEqual(self.server.count("work_put", "tool"), 0)
        self.assertEqual(self.server.count("work_put", "http"), 2)
        self.assertEqual(adapter.work.get("w-1")["item"]["title"], "Build the thing")
        self.assertEqual(len(adapter.work.list()), 1)
        with self.assertRaises(EA.InvalidRequest):
            adapter.work.put("w-2", "t", unmapped_field=1)
        with self.assertRaises(EA.Unsupported):
            adapter.door.call("work_put", {}, ident="w", force="tool")

    def test_a_waiting_or_blocked_state_carries_a_reason_where_the_mapping_names_one(self):
        adapter = self.adapter()
        adapter.work.put("w-8", "t", state="awaiting", progress="needs a review")
        self.assertEqual(self.server.tasks[("seat-1", "w-8")]["why_state"], "needs a review")
        adapter.work.put("w-7", "t", state="blocked")
        self.assertEqual(self.server.tasks[("seat-1", "w-7")]["why_state"], "blocked")
        adapter.work.put("w-6", "t", state="running", progress="going")
        self.assertNotIn("why_state", self.server.tasks[("seat-1", "w-6")])
        self.clock_now[0], self.server.now = 100, 1100.0
        adapter.work.patch("w-6", state="blocked")
        self.assertEqual(self.server.tasks[("seat-1", "w-6")]["why_state"], "going", "the last progress line")
        adapter.work.put("w-5", "t", state="awaiting", state_reason="given")
        self.assertEqual(self.server.tasks[("seat-1", "w-5")]["why_state"], "given")

    def test_an_empty_line_is_not_sent(self):
        adapter = self.adapter()
        first = adapter.work.put("w-9", "t", state="running", progress="", doer="  ")
        self.assertTrue(first["recorded"])
        self.assertNotIn("progress", self.server.tasks[("seat-1", "w-9")])
        self.assertEqual(adapter.work.patch("w-9", progress=" "), {"sent": False, "unchanged": True})

    def test_updates_are_coalesced_and_the_last_state_wins(self):
        adapter = self.adapter()
        adapter.work.put("w-1", "t", state="running")
        self.server.now, self.clock_now[0] = 1000.0, 0.0
        first = adapter.work.patch("w-1", progress="a")
        self.assertEqual((first["sent"], first["deferred"]), (False, True))  # the put just recorded: wait
        self.clock_now[0] = 30
        adapter.work.patch("w-1", progress="b")
        second = adapter.work.patch("w-1", progress="c", doer="d")
        self.assertAlmostEqual(second["due_in"], 30)
        self.assertEqual(self.server.count("work_patch"), 0)
        self.server.now, self.clock_now[0] = 1061.0, 61
        self.assertEqual(adapter.work.flush()["w-1"]["sent"], True)
        self.assertEqual(self.server.count("work_patch"), 1)
        task = self.server.tasks[("seat-1", "w-1")]
        self.assertEqual((task["progress"], task["doer"]), ({"text": "c"}, "d"))  # b was never sent
        again = adapter.work.patch("w-1", progress="c")
        self.assertEqual(again, {"sent": False, "unchanged": True})
        self.assertEqual(adapter.work.flush(), {})

    def test_a_rate_limit_defers_and_keeps_the_pending_state(self):
        adapter = self.adapter()
        adapter.work.put("w-1", "t")
        self.clock_now[0] = 100  # our clock says it is due, the server's says it is not
        self.server.now = 1010.0
        result = adapter.work.patch("w-1", progress="x")
        self.assertEqual((result["sent"], result["deferred"], result["due_in"]), (False, True, 60.0))
        self.assertEqual(self.slept, [])  # not slept: reported
        self.assertEqual(adapter.work.flush(), {})  # still deferred by the wait
        self.clock_now[0], self.server.now = 161, 1100.0
        self.assertTrue(adapter.work.flush()["w-1"]["sent"])
        self.assertEqual(self.server.tasks[("seat-1", "w-1")]["progress"], {"text": "x"})

    def test_finish_is_final_and_idempotent(self):
        adapter = self.adapter()
        adapter.work.put("w-1", "t")
        adapter.work.patch("w-1", progress="pending")
        done = adapter.work.finish("w-1", "done", "all good")
        self.assertEqual(done["item"]["end"], {"state": "ok", "said": "all good"})
        self.assertNotIn("w-1", adapter.work.items)  # pending dropped
        adapter.work.finish("w-1", "done", "all good")  # the same outcome again is fine
        with self.assertRaises(EA.Conflict):
            adapter.work.finish("w-1", "failed", "no")
        self.server.now = 5000.0
        with self.assertRaises(EA.Conflict):
            adapter.door.call("work_patch", {"progress": "late"}, ident="w-1", force="http")

    def test_work_errors_map_like_the_rest(self):
        adapter = self.adapter()
        self.server.disabled = True
        with self.assertRaises(EA.DisabledError):
            adapter.work.put("w-1", "t")
        self.server.disabled = False
        with self.assertRaises(EA.NotFound):
            adapter.work.get("never")

    def test_the_interval_is_at_least_a_minute(self):
        self.assertEqual(self.adapter().work.interval, 60.0)


class TestWorkSyncPump(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.sink, self.host, self.t = FakeWorkSink(), FakeWorkHost(), [1000.0]
        self.settings = {"work_grace_seconds": 600, "work_outbox_alarm_depth": 3, "retry_base_seconds": 5,
                         "retry_max_seconds": 60}
        self.pump = self.make()

    def make(self):
        return sb.work_sync.WorkSync(self.settings, self.dir, self.sink, self.host, lambda: self.t[0])

    def cycle(self, advance=0):
        self.t[0] += advance
        return self.pump.cycle()

    def test_first_sight_is_one_put_and_nothing_more_while_nothing_changes(self):
        self.host.agent_list = [worker()]
        self.cycle()
        self.cycle(100)
        self.assertEqual(self.sink.kinds(), ["put"])
        _, work_id, title, fields = self.sink.calls[0]
        self.assertEqual((title, fields["state"], fields["kind"], fields["serves"]), ("The ask api#2", "queued", "coder", "acme/api#2"))
        self.assertEqual(work_id, sb.work_sync.work_id_of("w1|acme/api|2"))

    def test_a_change_is_patched_once_per_interval_and_the_last_state_wins(self):
        self.host.agent_list = [worker()]
        self.cycle()
        self.host.agent_list = [worker(status="working")]
        self.cycle(10)                                            # inside the interval: held
        self.assertEqual(self.sink.kinds(), ["put"])
        self.host.report_list = [report("question", "which table?")]
        self.host.agent_list = [worker(status="idle")]            # the state moved again: still one write due
        self.cycle(10)
        self.assertEqual(self.sink.kinds(), ["put"])
        self.cycle(50)                                            # the interval has run out
        self.assertEqual(self.sink.kinds(), ["put", "patch"])
        self.assertEqual(self.sink.calls[1][2], {"state": "awaiting", "progress": "which table?"})
        self.cycle(10)
        self.assertEqual(self.sink.kinds(), ["put", "patch"])
        self.host.agent_list = [worker(status="working")]
        self.cycle(61)                                            # and again after another interval
        self.assertEqual(self.sink.kinds(), ["put", "patch", "patch"])

    def test_the_interval_is_the_sinks_and_never_below_a_minute(self):
        self.sink.interval = 5
        self.assertEqual(self.pump.interval, 60)
        self.sink.interval = 300
        self.assertEqual(self.pump.interval, 300)

    def test_an_assignment_that_ends_is_finished_exactly_once(self):
        self.host.agent_list = [worker(status="working")]
        self.cycle()
        self.host.report_list = [report("done", "shipped the parser")]
        self.host.agent_list = [worker(status="idle")]
        self.cycle(1)
        self.assertEqual(self.sink.kinds(), ["put", "finish"])
        self.assertEqual(self.sink.calls[1][2:], ("done", "shipped the parser"))
        for _ in range(3):
            self.cycle(500)
        self.host.agent_list = []
        self.cycle(5000)
        self.assertEqual(self.sink.kinds(), ["put", "finish"], "finished once, never again, not cancelled later")

    def test_a_restart_makes_no_duplicate(self):
        self.host.agent_list = [worker()]
        self.cycle()
        self.pump = self.make()                                   # the daemon restarted: the state file is read again
        self.cycle(100)
        self.assertEqual(self.sink.kinds(), ["put"])
        (self.dir / "work-items.json").unlink()                   # even a lost state file: the same id, the put is idempotent
        self.pump = self.make()
        self.cycle(100)
        self.assertEqual({c[1] for c in self.sink.calls}, {sb.work_sync.work_id_of("w1|acme/api|2")})

    def test_an_agent_that_disappears_is_cancelled_after_the_grace_period(self):
        self.host.agent_list = [worker(status="working")]
        self.cycle()
        self.host.agent_list = []
        self.cycle(100)
        self.cycle(400)
        self.assertEqual(self.sink.kinds(), ["put"])
        self.cycle(200)
        self.assertEqual(self.sink.kinds(), ["put", "finish"])
        self.assertEqual(self.sink.calls[1][2], "cancelled")

    def test_a_sink_that_is_down_never_raises_and_drains_when_it_is_back(self):
        self.sink.down = True
        self.host.agent_list = [worker(), worker("w2", issue=3)]
        status = self.cycle()
        self.assertEqual((status["open"], status["outbox"], status["errors"]), (2, 2, 2))
        self.assertIn("down", status["error"])
        self.assertEqual(self.sink.calls, [])
        self.cycle(1)                                             # inside the backoff: not tried
        self.assertEqual(len([e for e in self.host.events if e[0] == "work.failed"]), 2)
        self.sink.down = False
        status = self.cycle(10)
        self.assertEqual((status["outbox"], status["errors"]), (0, 0))
        self.assertEqual(self.sink.kinds(), ["put", "put"])

    def test_a_deep_outbox_is_an_alarm_and_nothing_is_dropped(self):
        self.sink.down = True
        self.host.agent_list = [worker("w%d" % n, issue=n + 1) for n in range(5)]
        status = self.cycle()
        self.assertTrue(status["alarm"])
        self.assertEqual(status["outbox"], 5)

    def test_a_deferred_patch_is_kept_and_tried_later(self):
        self.host.agent_list = [worker()]
        self.cycle()
        self.sink.defer = 30
        self.host.agent_list = [worker(status="working")]
        self.cycle(100)
        self.assertEqual(self.sink.kinds(), ["put"])
        self.sink.defer = None
        self.cycle(10)                                            # inside the deferral
        self.assertEqual(self.sink.kinds(), ["put"])
        self.cycle(30)
        self.assertEqual(self.sink.kinds(), ["put", "patch"])

    def test_two_assignments_for_one_agent_are_two_items(self):
        self.host.agent_list = [worker(issue=2, status="working")]
        self.cycle()
        self.host.agent_list = [worker(issue=3, status="working")]   # reassigned to another ask
        self.cycle(1)
        ids = [c[1] for c in self.sink.calls if c[0] == "put"]
        self.assertEqual(len(set(ids)), 2)
        self.cycle(700)                                           # the first is no longer held: cancelled
        self.assertEqual([c[2] for c in self.sink.calls if c[0] == "finish"], ["cancelled"])

    def test_the_audit_names_each_write_and_holds_no_secret(self):
        self.host.agent_list = [worker()]
        self.host.report_list = [report("question", "use Bearer abcdef123456 or sk-abcdefghij1234?")]
        self.cycle()
        self.assertEqual([e[0] for e in self.host.events], ["work.put"])
        self.assertNotIn("abcdef123456", json.dumps(self.sink.calls))
        self.assertNotIn("abcdef123456", (self.dir / "work-items.json").read_text())


class TestDecisionOrder(unittest.TestCase):
    """Ask #67: the order of decisions (`Waits for:`), parsed, arranged, and drawn by the CLI. Synthetic names only."""

    def entry(self, ident, number, waits=None, repo="acme/api", since="2027-01-01T00:00:00Z", kind="issue"):
        found = {"id": ident, "kind": kind, "repo": repo, "ask": number, "agent": "coder", "since": since, "tier": "human", "title": "Item %d" % number,
                 "headline": "Item %d" % number}
        if waits:
            found["waits_for"] = waits
        return found

    def render(self, decisions):
        return sb.format_decisions({"stale": False, "decisions": decisions, "errors": []}, sb.parse_iso("2027-01-02T00:00:00Z")).split("\n")

    def test_the_waits_for_line_is_read_only_inside_the_decision_required_section(self):
        body = ("Waits for: acme/api#9\n## Decision required\nPick one.\nWaits for: acme/api#2, #3, not-a-ref, ACME/Web#7, #3\n"
                "## Notes\nWaits for: acme/api#4\n")
        self.assertEqual(sb.decision_core.parse_waits_for(body, "acme/api"), ["acme/api#2", "acme/api#3", "acme/web#7"])
        self.assertEqual(sb.decision_core.parse_waits_for("## Decision required\nno line\n", "acme/api"), [])
        self.assertEqual(sb.decision_core.parse_waits_for("## Decision required (re-verified)\r\nWaits for: #5\r\n", "acme/api"), ["acme/api#5"])
        self.assertEqual(sb.decision_core.parse_waits_for("## Decision required\nWaits for: #5\n", None), [], "a bare number with no repo to mean is skipped")
        many = "## Decision required\nWaits for: " + ", ".join("#%d" % n for n in range(1, 30))
        self.assertEqual(len(sb.decision_core.parse_waits_for(many, "acme/api")), sb.decision_core.WAITS_FOR_MAX)
        self.assertEqual(sb.decision_core.parse_waits_for(None, "acme/api"), [])

    def test_an_entry_carries_waits_for_only_when_the_issue_names_one(self):
        item = lambda n, waits: {"kind": "issue", "title": "t%d" % n, "ask": n, "agent": None, "since": 100.0, "seed": "issue:acme/api#%d" % n,
                                 "bare": True, "repo": "acme/api", "tier": "human", "waits_for": waits}
        entries = sb.decision_core.build_entries([item(1, ["acme/api#2"]), item(2, [])], ["d1", "d2"], "acme/api", lambda repo, number: None)
        by = {e["ask"]: e for e in entries}
        self.assertEqual(by[1]["waits_for"], ["acme/api#2"])
        self.assertNotIn("waits_for", by[2])

    def test_a_blocked_item_is_indented_under_what_it_waits_for_and_the_first_actionable_is_next(self):
        decisions = [self.entry("#1", 1, ["acme/api#2"]), self.entry("#2", 2), self.entry("#3", 3, ["acme/api#1"]), self.entry("#4", 4)]
        sb.decision_core.annotate_order(decisions)
        lines = self.render(decisions)
        self.assertRegex(lines[0], r"^#2 +waiting 1d +coder +Item 2  \[next\]$")
        self.assertRegex(lines[1], r"^   #1 +waiting 1d +coder +Item 1  \[waits for #2\]$")
        self.assertRegex(lines[2], r"^      #3 +waiting 1d +coder +Item 3  \[waits for #1\]$")
        self.assertRegex(lines[3], r"^#4 +waiting 1d +coder +Item 4$")
        self.assertEqual(decisions[1]["mark"], "next")
        self.assertEqual([d.get("mark") for d in decisions if d["id"] != "#2"], [None, None, None])

    def test_a_cycle_is_drawn_at_the_top_marked_and_never_next(self):
        decisions = [self.entry("#1", 1, ["acme/api#2"]), self.entry("#2", 2, ["acme/api#1"]), self.entry("#3", 3)]
        sb.decision_core.annotate_order(decisions)
        lines = self.render(decisions)
        self.assertRegex(lines[0], r"^#1 .*\[cycle: they wait for each other\]$")
        self.assertRegex(lines[1], r"^#2 .*\[cycle: they wait for each other\]$")
        self.assertRegex(lines[2], r"^#3 .*\[next\]$", "the first item that CAN be done is next, not a cycle member")
        self.assertEqual(sb.decision_core.find_cycles(decisions), [["acme/api#1", "acme/api#2"]])

    def test_a_self_wait_and_a_three_cycle_are_cycles_and_the_rest_is_next(self):
        decisions = [self.entry("#1", 1, ["acme/api#2"]), self.entry("#2", 2, ["acme/api#3"]), self.entry("#3", 3, ["acme/api#1"]),
                     self.entry("#4", 4, ["acme/api#4"]), self.entry("#5", 5, ["acme/api#6"]), self.entry("#6", 6, ["acme/api#5"]), self.entry("#7", 7)]
        self.assertEqual(sb.decision_core.find_cycles(decisions), [["acme/api#1", "acme/api#2", "acme/api#3"], ["acme/api#4"], ["acme/api#5", "acme/api#6"]])
        sb.decision_core.annotate_order(decisions)
        self.assertEqual([(d["id"], d.get("mark")) for d in sb.decision_core.arranged(decisions)],
                         [("#1", "cycle"), ("#2", "cycle"), ("#3", "cycle"), ("#4", "cycle"), ("#5", "cycle"), ("#6", "cycle"), ("#7", "next")])

    def test_a_wait_for_something_that_is_not_an_open_decision_blocks_nothing(self):
        decisions = [self.entry("#1", 1, ["acme/api#99"]), self.entry("#2", 2)]
        sb.decision_core.annotate_order(decisions)
        self.assertEqual([(d["id"], d["depth"], d.get("mark")) for d in sb.decision_core.arranged(decisions)], [("#1", 0, "next"), ("#2", 0, None)])

    def test_a_list_in_which_nothing_waits_is_exactly_what_it_was(self):
        decisions = [self.entry("#2", 2, since="2027-01-01T06:00:00Z"), self.entry("#1", 1)]
        before = self.render([dict(d) for d in decisions])
        sb.decision_core.annotate_order(decisions)
        self.assertEqual(self.render(decisions), before)
        self.assertTrue(all(set(d) == {"id", "kind", "repo", "ask", "agent", "since", "tier", "title", "headline"} for d in decisions), "no field was added")
        self.assertNotIn("next", "\n".join(before))

    def test_a_chain_deeper_than_the_cap_stays_at_the_cap_and_is_still_a_chain(self):
        n = sb.decision_core.ORDER_DEPTH_MAX + 3
        decisions = [self.entry("#%d" % i, i, ["acme/api#%d" % (i + 1)] if i < n else None) for i in range(1, n + 1)]
        sb.decision_core.annotate_order(decisions)
        depths = [d["depth"] for d in sb.decision_core.arranged(decisions)]
        self.assertEqual(depths, [min(i, sb.decision_core.ORDER_DEPTH_MAX) for i in range(n)])
        self.assertEqual(max(depths), sb.decision_core.ORDER_DEPTH_MAX)

    def test_the_docs_describe_the_line_the_next_mark_and_the_two_sweep_findings(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        section = text.split("### The order of decisions (`Waits for:`)", 1)[1]
        for needle in ("Waits for:", "`next`", "`cycle`", "**S7**", "**S8**", "writes nothing to GitHub"):
            self.assertIn(needle, section)

    def test_a_report_is_never_a_target_so_nothing_waits_under_it(self):
        decisions = [self.entry("r1", 5, kind="report"), self.entry("#6", 6, ["acme/api#5"])]
        sb.decision_core.annotate_order(decisions)
        self.assertEqual(decisions[1]["depth"], 0, "a report is not an issue decision: nothing waits under it")
        self.assertEqual(decisions[0].get("mark"), None)


class TestAuthorityModulesGuard(unittest.TestCase):
    """Ask #532, slice 1: the code that decides who may clear, hold or merge lives in modules the owner-hold rule lists, and a module that does and is neither listed nor
    exempted BY NAME fails this guard. The vocabulary, the listing and the exemptions are all DATA in the matrix file; this test has no module name of its own."""

    def setUp(self):
        self.floor = sb.load_module("impact_floor")
        self.matrix = self.floor.load_matrix()
        self.block = self.matrix["owner_hold"]

    @staticmethod
    def authority_functions(directory, vocabulary):
        """{module path relative to the repository (bin/NAME.py): [function names]} for every module of `directory` that defines a function whose name has a word of the vocabulary."""
        import ast
        words, found = set(vocabulary), {}
        for path in sorted(Path(directory).glob("*.py")):
            tree = ast.parse(path.read_text())
            names = sorted({n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and words & set(n.name.strip("_").split("_"))})
            if names:
                found["bin/" + path.name] = names
        return found

    @staticmethod
    def role_checks(directory, literals):
        """{module path (bin/NAME.py): [the literal compared]} for every module of `directory` with a COMPARISON (==, !=, in, not in) against one of the role literals (the code, not a docstring):
        the second rule, independent of function names."""
        import ast
        wanted, found = set(literals), {}

        def strings(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.value
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                for item in node.elts:
                    yield from strings(item)
        for path in sorted(Path(directory).glob("*.py")):
            hits = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Compare):
                    for side in [node.left] + list(node.comparators):
                        hits |= wanted & set(strings(side))
            if hits:
                found["bin/" + path.name] = sorted(hits)
        return found

    def unlisted(self, directory, block=None):
        """The modules of `directory` that, by a function name with a word of the vocabulary OR by a comparison against a role literal, decide who may act, and are in no top tier path (as a
        path or a glob) and in no exemption."""
        block = block or self.block
        listed = {g for globs in block["top_tier_paths"].values() for g in globs}
        exempt = set(block["authority_modules"]["exempt"])
        found = set(self.authority_functions(directory, block["authority_modules"]["vocabulary"])) | set(self.role_checks(directory, block["authority_modules"]["role_literals"]))
        return sorted(m for m in found if not self.floor.matches(m, sorted(listed)) and m not in exempt)

    def test_s1_the_named_modules_are_top_tier_paths_and_every_one_is_also_in_the_floor(self):
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        named = [g for g in self.block["top_tier_paths"]["authority"] if g.startswith("bin/") and g.endswith(".py")]
        self.assertTrue(len(named) >= 7, "the matrix lists the authority modules of bin/")
        for module in named:
            self.assertIn(module, self.floor.top_tier_hits([module], self.matrix).get("authority", []), module + " is a top tier path")
            self.assertTrue(self.floor.floor_paths([module], globs, exclude, force), module + " is in the floor")
            self.assertEqual(self.floor.owner_decide(self.matrix, [module], [], None, self.floor.floor_paths([module], globs, exclude, force))[0], "hold", module)

    def test_the_real_bin_directory_has_no_unlisted_unexempted_authority_module(self):
        self.assertEqual(self.unlisted(BIN), [], "a module with authority code is neither a top tier path nor exempted by name in the matrix")

    def test_s2_a_new_module_with_a_clear_decision_that_is_not_listed_fails_and_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "new_gate.py").write_text("def may_clear_hold(pr):\n    return True\n")
            (Path(tmp) / "harmless.py").write_text("def format_line(x):\n    return x\n")
            self.assertEqual(self.unlisted(tmp), ["bin/new_gate.py"])
            exempted = json.loads(json.dumps(self.block))
            exempted["authority_modules"]["exempt"]["bin/new_gate.py"] = "a reason"
            self.assertEqual(self.unlisted(tmp, exempted), [], "exempted by name")
            listed = json.loads(json.dumps(self.block))
            listed["top_tier_paths"]["authority"].append("bin/new_gate.py")
            self.assertEqual(self.unlisted(tmp, listed), [], "listed")

    def test_a_renamed_function_cannot_hide_a_role_check_and_a_docstring_or_an_assignment_is_not_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "quiet_name.py").write_text('def run(kind):\n    return kind[1] == "chief-of-staff"\n')
            (Path(tmp) / "tuple_form.py").write_text('def run(role):\n    return role in ("orchestrator", "x")\n')
            (Path(tmp) / "not_a_check.py").write_text('"""the chief-of-staff and the operator"""\nNAME = "chief-of-staff"\ndef run(x):\n    return {"role": "operator"}\n')
            self.assertEqual(self.unlisted(tmp), ["bin/quiet_name.py", "bin/tuple_form.py"])
            exempted = json.loads(json.dumps(self.block))
            exempted["authority_modules"]["exempt"].update({"bin/quiet_name.py": "a reason", "bin/tuple_form.py": "a reason"})
            self.assertEqual(self.unlisted(tmp, exempted), [])
            wider = json.loads(json.dumps(self.block))
            wider["authority_modules"]["role_literals"].append("x")
            self.assertEqual(self.unlisted(tmp, wider), ["bin/quiet_name.py", "bin/tuple_form.py"])

    def test_a_caller_check_name_is_in_the_vocabulary_and_the_modules_that_gate_a_caller_are_listed(self):
        words = set(self.block["authority_modules"]["vocabulary"])
        self.assertTrue({"caller", "refuse", "refusal", "role", "chief", "operator"} <= words)
        found = set(self.authority_functions(BIN, sorted(words))) | set(self.role_checks(BIN, self.block["authority_modules"]["role_literals"]))
        listed = {g for globs in self.block["top_tier_paths"].values() for g in globs}
        self.assertTrue(found, "the guard finds the authority modules of bin/")
        for module in found:
            self.assertTrue(module in listed or module in self.block["authority_modules"]["exempt"], module)
        self.assertGreaterEqual(len(found & listed), 10, "the modules the guard finds that are listed")

    def test_every_exemption_carries_a_trace_of_where_its_values_go_and_the_limits_are_stated(self):
        exempt = self.block["authority_modules"]["exempt"]
        self.assertTrue(exempt)
        for module, reason in exempt.items():
            self.assertIn("Trace:", reason, module + ": an exemption says where its values go (no value of it feeds an authority function)")
        limits = self.block["authority_modules"]["limits"]
        self.assertTrue(isinstance(limits, list) and len(limits) >= 3 and all(isinstance(x, str) and len(x) > 40 for x in limits))
        self.assertIn("cannot see", " ".join(limits))

    def test_the_external_adapter_that_feeds_answered_by_is_listed_not_exempted(self):
        listed = {g for globs in self.block["top_tier_paths"].values() for g in globs}
        self.assertNotIn("bin/external_adapter.py", self.block["authority_modules"]["exempt"])
        self.assertIn("bin/external_adapter.py", listed)

    def test_every_exemption_names_a_real_module_with_a_reason_and_is_not_also_listed(self):
        listed = {g for globs in self.block["top_tier_paths"].values() for g in globs}
        for module, reason in self.block["authority_modules"]["exempt"].items():
            self.assertTrue((BIN.parent / module).is_file(), module + " does not exist")
            self.assertTrue(isinstance(reason, str) and len(reason) >= 20, module + " needs a reason")
            self.assertNotIn(module, listed, module + " is both exempted and listed")

    def test_s3_the_code_names_no_module_of_the_rule_the_matrix_file_does(self):
        modules = [g for g in self.block["top_tier_paths"]["authority"] if g.startswith("bin/") and g.endswith(".py")] + list(self.block["authority_modules"]["exempt"])
        self.assertTrue(len(modules) >= 12)
        for code in [*SWITCHBOARD_SOURCE_FILES] + sorted(BIN.glob("*.py")):   # the code; the tests (this file) name modules as fixtures
            text = code.read_text()
            for module in modules:
                self.assertNotIn('"%s"' % module, text, "%s writes the path %s in code" % (code.name, module))

    def test_the_vocabulary_is_a_usable_list_of_words(self):
        words = self.block["authority_modules"]["vocabulary"]
        self.assertTrue(words and all(isinstance(w, str) and re.fullmatch(r"[a-z]+", w) for w in words))


class TestSkillsSync(unittest.TestCase):
    """Installed skills equal the kit's (ask 596): the check reads, the sync copies, install reuses the step."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.kit = self.tmp / "kit"
        self.names = ["fleet-a", "fleet-b", "fleet-c", "fleet-d", "fleet-e"]
        for n in self.names:
            (self.kit / "skills" / n).mkdir(parents=True)
            (self.kit / "skills" / n / "SKILL.md").write_text("kit %s\n" % n)
        self.dest = self.tmp / "installed"
        for n in self.names:
            (self.dest / n).mkdir(parents=True)
            (self.dest / n / "SKILL.md").write_text("kit %s\n" % n)
            os.utime(str(self.dest / n / "SKILL.md"), (1000, 1000))

    def skill(self, n):
        return self.dest / n / "SKILL.md"

    def drift(self):
        (self.dest / "fleet-b" / "SKILL.md").write_text("old\n")
        os.utime(str(self.skill("fleet-b")), (1000, 1000))
        self.skill("fleet-d").unlink()

    def test_s1_in_sync_names_no_skill(self):
        ok, text = sb.skills_status_line([self.dest], self.kit)
        self.assertTrue(ok)
        self.assertEqual(text.count("in sync"), 1)
        self.assertFalse([n for n in self.names if n in text])

    def test_s2_names_exactly_the_two_and_the_command(self):
        self.drift()
        ok, text = sb.skills_status_line([self.dest], self.kit)
        self.assertFalse(ok)
        self.assertEqual([n for n in self.names if n in text], ["fleet-b", "fleet-d"])
        self.assertEqual(text.count("skills sync"), 1)
        self.assertEqual(sorted(w for _, _, w in sb.skills_drift([self.dest], self.kit)), ["differs", "missing"])

    def test_s3_sync_changes_two_files_only(self):
        self.drift()
        before = {n: self.skill(n).stat().st_mtime for n in self.names if self.skill(n).exists()}
        changed = sb.sync_skills(self.dest, self.kit)
        self.assertEqual(len(changed), 2)
        for n in ("fleet-a", "fleet-c", "fleet-e"):
            self.assertEqual(self.skill(n).stat().st_mtime, before[n])
        self.assertTrue(sb.skills_status_line([self.dest], self.kit)[0])

    def test_s4_backup_and_mode(self):
        self.drift()
        os.chmod(str(self.skill("fleet-b")), 0o600)
        sb.sync_skills(self.dest, self.kit)
        backups = list((self.dest / "fleet-b").glob("SKILL.md.bak-*"))
        self.assertEqual([b.read_text() for b in backups], ["old\n"])
        self.assertEqual(self.skill("fleet-b").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.skill("fleet-b").read_text(), "kit fleet-b\n")
        self.assertEqual([p.name for p in (self.dest / "fleet-b").iterdir() if p.name.startswith(".")], [])

    def test_s5_nothing_else_touched(self):
        self.drift()
        (self.dest / "other").mkdir()
        (self.dest / "other" / "SKILL.md").write_text("mine\n")
        (self.dest / "opencode.json").write_text("{}\n")
        sb.sync_skills(self.dest, self.kit)
        self.assertEqual((self.dest / "other" / "SKILL.md").read_text(), "mine\n")
        self.assertEqual((self.dest / "opencode.json").read_text(), "{}\n")

    def test_s6_check_writes_nothing(self):
        self.drift()
        seen = lambda: sorted((str(p), p.stat().st_mtime) for p in self.dest.rglob("*"))
        before = seen()
        os.chmod(str(self.dest), 0o555)
        self.addCleanup(os.chmod, str(self.dest), 0o755)
        ok, _ = sb.skills_status_line([self.dest], self.kit)
        self.assertFalse(ok)
        self.assertEqual(seen(), before)

    def test_install_uses_the_same_step(self):
        self.drift()
        self.assertTrue(sb.install_skills(self.dest, self.kit))
        self.assertFalse(sb.install_skills(self.dest, self.kit))
        self.assertTrue(sb.skills_status_line([self.dest], self.kit)[0])

    def run_cmd(self, action):
        home = self.tmp / "home"
        env = {"HOME": str(home), "XDG_DATA_HOME": str(self.tmp / "data")}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sb, "KIT_ROOT", self.kit), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sb.cmd_skills(types.SimpleNamespace(skills_action=action), None, None)
        return code, out.getvalue(), err.getvalue()

    def test_command_exit_codes_and_no_folder(self):
        code, _, err = self.run_cmd("sync")
        self.assertEqual(code, 1)
        self.assertIn("no installed skills folder", err)
        code, out, _ = self.run_cmd("check")
        self.assertEqual(code, 0)
        self.assertIn("no installed skills folder", out)

    def test_command_check_fails_and_sync_clears_it(self):
        target = self.tmp / "data" / sb.PROG / "skills"
        shutil.copytree(str(self.dest), str(target))
        (target / "fleet-b" / "SKILL.md").write_text("old\n")
        code, out, _ = self.run_cmd("check")
        self.assertEqual((code, "fleet-b" in out, "skills sync" in out), (1, True, True))
        self.assertEqual(self.run_cmd("sync")[0], 0)
        self.assertEqual(self.run_cmd("check")[0], 0)

    def test_two_backups_in_one_second_do_not_collide(self):
        with mock.patch.object(sb.time, "strftime", return_value="20260101000000"):
            for text in ("one\n", "two\n"):
                self.skill("fleet-b").write_text(text)
                sb.sync_skills(self.dest, self.kit)
        got = sorted(b.read_text() for b in (self.dest / "fleet-b").glob("SKILL.md.bak-*"))
        self.assertEqual(got, ["one\n", "two\n"])

    def test_a_failed_write_leaves_no_temporary_file(self):
        self.skill("fleet-b").write_text("old\n")
        with mock.patch.object(sb.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sb.sync_skills(self.dest, self.kit)
        self.assertEqual([p.name for p in (self.dest / "fleet-b").iterdir() if p.name.startswith(".")], [])
        self.assertEqual(self.skill("fleet-b").read_text(), "old\n")

    def test_locations_name_both_folders(self):
        home, data = self.tmp / "home", self.tmp / "data"
        (data / "skills").mkdir(parents=True)
        (home / ".agents" / "skills").mkdir(parents=True)
        self.assertEqual(sb.skill_locations(home, data), [data / "skills", home / ".agents" / "skills"])
        self.assertEqual(sb.skill_locations(self.tmp / "none", self.tmp / "none"), [])

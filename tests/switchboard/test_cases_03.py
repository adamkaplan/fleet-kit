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
from support.installs_and_keys import InstallCase, tree_of
from support.loader import SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, captured, class_source, sb, sc, switchboard_source
from support.orders_and_adapters import EA, ExternalAdapterCase, FakeDoorServer, FakeWorkHost, FakeWorkSink, fake_mapping, report, worker
from support.paths import BIN, REPO
from support.policy_and_import import TRIAL
from support.scenario_roles import AGENT_FILES, ROLE_BYTE_LIMITS, role_of
from support.service_and_merge import feed_line
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


class TestSyncReportFreezePerBoss(unittest.TestCase):
    def test_the_cycle_scopes_the_freeze_by_the_errors_boss(self):
        source = (REPO / "bin" / "decision_sync.py").read_text()
        self.assertIn('e.get("source") == "reports" and e.get("boss")', source)
        self.assertIn("plan_operations(desired, self._projected(), kinds, now, scoped)", source)


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


class TestExternalAdapterErrors(ExternalAdapterCase):
    def test_http_statuses_map_to_the_hierarchy_and_4xx_is_not_retried(self):
        cases = ((401, "cred", EA.CredentialError), (403, "off", EA.DisabledError), (404, "nf", EA.NotFound),
                 (400, "bad", EA.InvalidRequest), (415, "media", EA.InvalidRequest), (409, "clash", EA.Conflict),
                 (422, "odd", EA.InvalidRequest), (409, "decided", EA.AlreadyDecided))
        adapter = self.adapter("http")
        for status, code, cls in cases:
            self.slept.clear()
            before = len(self.server.requests)
            self.server.fail_next("raise", status, {"code": code, "message": "m"})
            with self.assertRaises(cls) as ctx:
                self.push(adapter, self.decision(), "k-%d" % status)
            self.assertIs(type(ctx.exception), cls)
            self.assertEqual(self.slept, [], status)
            self.assertEqual(len(self.server.requests), before)  # the injected reply is the one and only call

    def test_a_4xx_is_asked_once(self):
        adapter = self.adapter("http")
        for status in (400, 401, 403, 404, 409):
            self.server.fail_next("raise", status, {"code": "z", "message": "m"}, count=10)
            with self.assertRaises(EA.AdapterError):
                self.push(adapter, self.decision(), "k")
            self.assertEqual(len([1 for i in self.server.inject if i["count"] == 9]), 1, status)
            self.server.inject.clear()

    def test_429_honours_retry_after_then_succeeds(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later"}, {"Retry-After": "2"})
        self.assertTrue(self.push(adapter, self.decision(), "k-1").startswith("it-"))
        self.assertEqual(self.slept, [2.0])

    def test_a_long_retry_after_is_reported_not_slept(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later"}, {"Retry-After": "60"})
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-1")
        self.assertEqual(ctx.exception.retry_after, 60.0)
        self.assertEqual(self.slept, [])
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later", "retry_after_s": 7})
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-2")
        self.assertEqual(ctx.exception.retry_after, 7.0)  # no header: the mapped body field
        self.server.fail_next("raise", 429, {"code": "x", "message": "later"}, count=10)
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-3")
        self.assertEqual(ctx.exception.retry_after, 1.0)  # neither: the default (short: slept, then given up)
        self.assertEqual(self.slept, [1.0, 1.0])

    def test_5xx_is_retried_with_a_doubling_delay_and_a_cap(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 503, count=2)
        self.assertTrue(self.push(adapter, self.decision(), "k-1").startswith("it-"))
        self.assertEqual(self.slept, [0.5, 1.0])
        self.slept.clear()
        self.server.fail_next("raise", 500, count=10)
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-2")
        self.assertEqual(self.slept, [0.5, 1.0])  # three attempts, two waits
        self.slept.clear()
        many = self.adapter("http", retry={"attempts": 5})
        self.server.inject.clear()
        self.server.fail_next("raise", 500, count=10)
        with self.assertRaises(EA.Unavailable):
            self.push(many, self.decision(), "k-3")
        self.assertEqual(self.slept, [0.5, 1.0, 1.5, 1.5])  # capped at max_delay

    def test_a_timeout_a_dropped_connection_and_a_closed_port_are_unavailable_and_retried(self):
        data = fake_mapping(self.server.url, "http", timeouts={"request": 0.3}, retry={"attempts": 2})
        adapter = EA.McpRestAdapter(data, sleep=self.slept.append, environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY})
        self.server.delay = 1.0
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-1")
        self.assertEqual(self.slept, [0.5])
        self.server.delay = 0
        self.server.down = True
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-2")
        self.server.down = False
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        data["base_url"] = "http://127.0.0.1:%d" % port
        self.slept.clear()
        with self.assertRaises(EA.Unavailable):
            self.push(EA.McpRestAdapter(data, sleep=self.slept.append,
                                        environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY}), self.decision(), "k-3")
        self.assertEqual(self.slept, [0.5])

    def test_tool_channel_refusals_map_by_code(self):
        adapter = self.adapter("tool")
        for code, cls in (("off", EA.DisabledError), ("decided", EA.AlreadyDecided), ("clash", EA.Conflict),
                          ("slow", EA.RateLimited), ("nf", EA.InvalidRequest), ("bad", EA.InvalidRequest)):
            self.slept.clear()
            self.server.fail_next("raise", 400, {"code": code, "message": "refused: %s" % code, "retry_after_s": 90},
                                  level="result")
            with self.assertRaises(cls) as ctx:
                self.push(adapter, self.decision(), "k-" + code)
            self.assertIs(type(ctx.exception), cls)
            self.assertEqual(self.slept, [])

    def test_tool_channel_credential_and_schema_failures(self):
        adapter = self.adapter("tool")
        self.server.fail_next("raise", 401, {"code": "cred", "message": "no"}, level="http")
        with self.assertRaises(EA.CredentialError):
            self.push(adapter, self.decision(), "k-1")
        self.server.fail_next("raise", 503, level="http")
        self.assertTrue(self.push(adapter, self.decision(), "k-2").startswith("it-"))  # retried
        # schema failures arrive as text, not JSON
        with self.assertRaises(EA.InvalidRequest) as ctx:
            adapter.door.mcp.call_tool("item_raise", {"bad_schema": 1})
        self.assertIn("validation", str(ctx.exception))
        with self.assertRaises(EA.InvalidRequest):
            adapter.door.mcp.call_tool("no_such_tool", {})

    def test_the_tool_session_sse_and_expiry(self):
        adapter = self.adapter("tool")
        self.assertEqual(adapter.door.mcp.list_tools()[:2], ["seats_list", "item_raise"])
        self.assertEqual(adapter.door.mcp.session, "sess-1")
        self.push(adapter, self.decision(), "k-1")
        self.assertEqual(self.server.sid, 1)  # one session for all of it
        self.server.sessions.clear()  # the server forgot it
        self.push(adapter, self.decision(), "k-2")
        self.assertEqual(self.server.sid, 2)
        self.server.session_miss = 400   # some servers answer a session they forgot with a 400
        self.server.sessions.clear()
        self.push(adapter, self.decision(), "k-2b")
        self.assertEqual(self.server.sid, 3)
        with self.assertRaises(EA.InvalidRequest):   # a real 400 with a good session is asked once, not looped
            self.server.fail_next("raise", 400, {"code": "bad", "message": "m"}, count=2)
            self.push(adapter, self.decision(), "k-2c")
        self.assertEqual(self.server.sid, 4)
        self.server.session_miss = 404
        self.server.sse = True
        fresh = self.adapter("tool")
        self.assertTrue(self.push(fresh, self.decision(), "k-3").startswith("it-"))
        self.assertEqual(fresh.pull_normalised(None)[1], 0)

    def test_a_list_of_messages_is_joined(self):
        adapter = self.adapter("tool")
        self.server.fail_next("raise", 400, {"code": "bad", "message": ["first", "second"]}, level="result")
        with self.assertRaises(EA.InvalidRequest) as ctx:
            self.push(adapter, self.decision(), "k-1")
        self.assertIn("first; second", str(ctx.exception))

    def test_a_redirect_is_never_followed(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 302, {"code": "x", "message": "moved"}, {"Location": "http://127.0.0.1:1/"})
        with self.assertRaises(EA.InvalidRequest):
            self.push(adapter, self.decision(), "k-1")


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


class TestJobTool(unittest.TestCase):
    """`job start` / `job status`: detached long runs, told apart by evidence. Real short processes; each test waits on the process it started
    (no polling of a clock), so a loaded machine only makes it slower."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)
        patcher = mock.patch.dict(os.environ, {sb.JOBS_DIR_ENV: str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs, self.pids = {}, []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.SubprocessError:
                pass

    def spawn(self, argv, **kwargs):
        proc = subprocess.Popen(argv, **kwargs)
        self.last = proc
        return proc

    def job(self, *argv, spawn=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ):
            code = sb.main(["job", *argv], spawn=spawn or self.spawn)
        return code, out.getvalue().strip(), err.getvalue()

    def started(self, name, *command):
        code, out, err = self.job("start", name, "--", *command)
        self.assertEqual(code, 0, err)
        self.procs[name] = self.last
        record = sb.job_read(name, self.root)
        self.pids.append(record["pid"])
        return record

    def settled(self, name, want):
        """Wait for the job's own process (the wrapper), then read its state: no polling."""
        self.procs[name].wait(timeout=60)
        state = sb.job_state(sb.job_read(name, self.root), self.root)
        self.assertEqual(state[0], want)
        return state

    def test_a_finished_job_reads_its_exit_code_and_its_result_file(self):
        self.started("suite", "sh", "-c", "echo hello; exit 3")
        self.assertEqual(self.settled("suite", "finished"), ("finished", 3))
        code, out, err = self.job("status", "suite")
        self.assertEqual(code, 1, "a non-zero exit is not success")
        self.assertRegex(out, r"^finished: suite exit code 3 after it started \d+s ago; result .*out\.log$")
        self.assertEqual((self.root / "suite" / "out.log").read_text(), "hello\n")

    def test_a_job_that_exits_zero_reads_finished_and_status_succeeds(self):
        self.started("ok", "sh", "-c", "exit 0")
        self.settled("ok", "finished")
        self.assertEqual(self.job("status", "ok")[0], 0)

    def test_a_killed_job_reads_dead_with_no_exit_code(self):
        record = self.started("long", "sleep", "60")
        code, out, err = self.job("status", "long")
        self.assertEqual((code, out.split(":")[0]), (0, "alive"))
        os.killpg(record["pid"], signal.SIGKILL)   # the whole group: what a restart does
        self.assertEqual(self.settled("long", "dead"), ("dead", None))
        code, out, err = self.job("status", "long")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"^dead: long was killed \(no exit code\); it started \d+s ago; what it printed is in .*out\.log$")
        self.assertFalse((self.root / "long" / "exit").exists())

    def test_a_pid_reused_by_another_process_is_not_the_job(self):
        self.started("reuse", "sleep", "60")
        job = sb.job_read("reuse", self.root)
        job["token"] = "Thu Jan  1 00:00:00 1970"   # the recorded start time is not this process's
        (self.root / "reuse" / "job.json").write_text(json.dumps(job))
        self.assertEqual(sb.job_state(sb.job_read("reuse", self.root), self.root)[0], "dead")

    def test_a_record_with_no_pid_reads_unknown(self):
        self.assertEqual(sb.job_state({"name": "x"}, self.root), ("unknown", None))

    def test_a_name_still_alive_is_refused_and_a_dead_or_finished_one_is_replaced_with_its_markers_cleared(self):
        self.started("again", "sleep", "60")
        code, out, err = self.job("start", "again", "--", "true")
        self.assertEqual(code, 1)
        self.assertIn("is still running", err)
        record = self.started("dead", "sleep", "60")
        os.killpg(record["pid"], signal.SIGKILL)
        self.settled("dead", "dead")
        self.job("status", "dead")   # collected
        self.assertTrue((self.root / "dead" / "collected").exists())
        self.started("dead", "sh", "-c", "exit 2")
        self.assertEqual(self.settled("dead", "finished"), ("finished", 2))
        self.assertFalse((self.root / "dead" / "collected").exists(), "a new run starts uncollected")

    def test_a_command_that_cannot_start_is_said_and_leaves_no_record(self):
        def refuse(argv, **kwargs):
            raise OSError(errno.ENOENT, "No such file or directory")
        code, out, err = self.job("start", "nope", "--", "x", spawn=refuse)
        self.assertEqual(code, 1)
        self.assertIn("cannot start x", err)
        self.assertIsNone(sb.job_read("nope", self.root))

    def test_a_bad_name_no_command_and_an_unknown_job_are_refused(self):
        self.assertEqual(self.job("start", "Bad Name", "--", "true")[0], 2)
        self.assertEqual(self.job("start", "x", "--")[0], 2)
        code, out, err = self.job("status", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no job named nope", err)

    def test_asking_after_it_died_collects_it_so_nobody_else_is_told(self):
        record = self.started("c", "sleep", "60")
        os.killpg(record["pid"], signal.SIGKILL)
        self.settled("c", "dead")
        self.assertFalse((self.root / "c" / "collected").exists())
        self.job("status", "c")
        self.assertTrue((self.root / "c" / "collected").exists())

    def test_a_jobs_dir_that_is_not_private_is_refused_and_not_believed(self):
        os.chmod(str(self.root), 0o755)
        code, out, err = self.job("start", "x", "--", "true")
        self.assertEqual(code, 1)
        self.assertIn("not a private directory", err)
        self.assertFalse(sb.job_private(self.root))
        os.chmod(str(self.root), 0o700)
        self.assertTrue(sb.job_private(self.root))

    def test_a_record_is_written_whole_or_not_at_all(self):
        target = self.root / "f"
        sb.job_write_atomic(target, "one")
        with mock.patch.object(sb.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sb.job_write_atomic(target, "two")
        self.assertEqual(target.read_text(), "one", "the old file is intact")
        self.assertEqual([p.name for p in self.root.iterdir()], ["f"], "no temp file is left")

    def test_the_starter_is_established_from_its_own_session_and_otherwise_nobody(self):
        with mock.patch.object(sb, "build_engine", return_value=(types.SimpleNamespace(herdr=None, v2=None), None, None)), \
                mock.patch.object(sb, "session_identity", return_value={"name": "coder"}), mock.patch.dict(os.environ, {sb.SESSION_ENV: "ses_example1"}):
            self.assertEqual(sb.job_starter(None, time.time), ("ses_example1", "coder"))
        with mock.patch.object(sb, "build_engine", side_effect=sb.ConfigError("no config")):
            self.assertEqual(sb.job_starter(None, time.time), (None, None))


class TestPlanKind(unittest.TestCase):
    """#1094: GitHubHub.plan_kind: plan / noplan / unknown, one cached read per issue, no write."""

    def hub(self, answers):
        import types
        calls, t = [], [0.0]

        def gh_api(path, paginate=True, timeout=None, **kw):
            calls.append(path)
            a = answers(path) if callable(answers) else answers
            if isinstance(a, Exception):
                raise a
            return [a]
        hub = types.SimpleNamespace(lock=threading.RLock(), _cache={}, now=lambda: t[0], gh_api=gh_api,
                                    config={"call_timeout_seconds": 5})
        return hub, calls, t

    def kind(self, hub, n=1):
        return sb.GitHubHub.plan_kind(hub, "acme/api", n)

    def test_the_three_kinds(self):
        plan = [{"id": 1, "created_at": "2026-01-01", "body": "## Plan (scenarios)\n\nx"}]
        self.assertEqual(self.kind(self.hub(plan)[0]), "plan")
        self.assertEqual(self.kind(self.hub([{"id": 1, "body": "hello"}])[0]), "noplan")
        self.assertEqual(self.kind(self.hub(sb.GitHubUnavailable("down"))[0]), "unknown")
        self.assertEqual(self.kind(self.hub({"not": "a list"})[0]), "unknown")

    def test_a_second_refresh_within_the_ttl_adds_no_read(self):
        hub, calls, t = self.hub([])
        for n in range(1, 6):
            self.kind(hub, n)
        self.assertEqual(len(calls), 5)
        for n in range(1, 6):
            self.kind(hub, n)
        self.assertEqual(len(calls), 5, "no read of the second refresh")
        t[0] += sb.GITHUB_LOOKUP_TTL + 1
        self.kind(hub, 1)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(c.startswith("repos/") and "comments" in c for c in calls))

    def test_a_full_page_never_reads_noplan_and_a_plan_on_it_is_unknown(self):
        full = [{"id": i, "created_at": "2026-01-01", "body": "x"} for i in range(100)]
        self.assertEqual(self.kind(self.hub(full)[0]), "unknown")
        full[3]["body"] = "## Plan (scenarios)\n\nx"
        self.assertEqual(self.kind(self.hub(full)[0]), "unknown")
        self.assertEqual(self.kind(self.hub(full[:99])[0]), "plan")

    def test_a_plan_in_the_body_reads_noplan(self):
        body = "## Plan (scenarios)\n\nx"

        def answers(path):
            return {"body": body} if path.endswith("/issues/1") else [{"id": 1, "body": "hi"}]
        hub, calls, t = self.hub(answers)
        self.assertEqual(self.kind(hub), "noplan")   # the ask says a plan COMMENT; a plan-looking BODY does not count

    def test_every_row_is_read_over_several_refreshes(self):
        hub, calls, t = self.hub([])
        hub.plan_kind = lambda r, n, b=None: sb.GitHubHub.plan_kind(hub, r, n, b)
        records = [{"repo": "acme/api", "number": n} for n in range(60)]
        for _ in range(7):
            sb.GitHubHub.plan_kinds(hub, records, [sb.PLAN_KIND_READS_PER_REFRESH])
        self.assertEqual(len(calls), 60)
        kinds = sb.GitHubHub.plan_kinds(hub, records, [sb.PLAN_KIND_READS_PER_REFRESH])
        self.assertEqual(set(kinds.values()), {"noplan"})

    def test_never_read_rows_are_served_before_retries(self):
        bad = set(range(11))

        def answers(path):
            n = int(path.split("/issues/")[1].split("/")[0])
            return sb.GitHubUnavailable("x") if n in bad else []
        hub, calls, t = self.hub(answers)
        hub.plan_kind = lambda r, n, b=None: sb.GitHubHub.plan_kind(hub, r, n, b)
        records = [{"repo": "acme/api", "number": n} for n in range(11)]
        sb.GitHubHub.plan_kinds(hub, records, [10])
        t[0] += sb.GITHUB_LOOKUP_RETRY + 1
        records += [{"repo": "acme/api", "number": n} for n in range(100, 105)]
        calls.clear()
        kinds = sb.GitHubHub.plan_kinds(hub, records, [10])
        for n in range(100, 105):
            self.assertEqual(kinds[("acme/api", n)], "noplan")

    def test_one_page_one_call_no_pagination(self):
        hub, calls, t = self.hub([])
        seen = []
        orig = hub.gh_api
        hub.gh_api = lambda path, paginate=True, **kw: (seen.append(paginate), orig(path, paginate=paginate, **kw))[1]
        self.kind(hub)
        self.assertEqual(seen, [False])
        self.assertIn("per_page=100", calls[0])

    def test_a_failed_read_is_retried_after_the_short_ttl_not_the_full_one(self):
        for err in (sb.GitHubUnavailable("403"), sb.GitHubUnavailable("404"), TimeoutError("t")):
            hub, calls, t = self.hub(err)
            self.assertEqual(self.kind(hub), "unknown")
            self.kind(hub)
            self.assertEqual(len(calls), 1)
            t[0] += sb.GITHUB_LOOKUP_RETRY + 1
            self.kind(hub)
            self.assertEqual(len(calls), 2, "retried after the short ttl")

    def test_a_refresh_makes_at_most_the_cap_of_reads(self):
        hub, calls, t = self.hub([])
        budget = [sb.PLAN_KIND_READS_PER_REFRESH]
        kinds = [sb.GitHubHub.plan_kind(hub, "acme/api", n, budget) for n in range(60)]
        self.assertEqual(len(calls), sb.PLAN_KIND_READS_PER_REFRESH)
        self.assertEqual(kinds.count("unknown"), 60 - sb.PLAN_KIND_READS_PER_REFRESH)
        self.assertEqual(sb.PLAN_KIND_READS_PER_REFRESH, 10)

    def test_the_cache_is_bounded(self):
        hub, calls, t = self.hub([])
        for n in range(sb.PLAN_KIND_CACHE_MAX + 50):
            self.kind(hub, n)
        self.assertLessEqual(len(hub._cache), sb.PLAN_KIND_CACHE_MAX)


class TestPluginPush(unittest.TestCase):
    """#290 slice 6: the plugin's pushes. The receiver only MARKS a session; the pass thread makes the one bounded read."""
    SESSION = "ses_alpha"

    class Stub:
        def __init__(self):
            self.calls = []

        def message_list(self, session, limit=None, order=None, type=None, cursor=None):
            self.calls.append((session, limit, type))
            return []

        def __getattr__(self, name):   # any other call (a write, a send) is a failure of the test
            raise AssertionError("the push must cause no call but a bounded read: %s" % name)

    def setUp(self):
        self.now, self.audits = [1000.0], []
        audit = types.SimpleNamespace(append=lambda actor, name, subject, detail: self.audits.append((name, detail)))
        self.feed = sb.EventFeed(None, audit, lambda: self.now[0])
        self.feed.connected, self.feed.needs_resync = True, False
        self.receiver = sb.EventReceiver(self.feed, audit, lambda: self.now[0], known=lambda: {self.SESSION, "ses_beta"})

    def message(self, **fields):
        body = {"v": 1, "kind": "event", "event": "prompt", "sessionID": self.SESSION, "at": 1000000}
        body.update(fields)
        return json.dumps(body).encode()

    def test_s9_a_good_message_marks_only_that_session_and_the_next_pass_reads_it_once(self):
        engine = types.SimpleNamespace(v2=self.Stub())
        self.assertTrue(self.receiver.handle(self.message()))
        self.assertEqual(engine.v2.calls, [], "the receiver reads nothing")
        self.assertEqual(list(self.feed.seen), [self.SESSION])
        self.feed.tick(engine, [self.SESSION, "ses_beta"])
        self.assertEqual(engine.v2.calls, [(self.SESSION, sb.FEED_PAGE, "assistant")], "one bounded read, for that session only")
        self.feed.tick(engine, [self.SESSION, "ses_beta"])
        self.assertEqual(len(engine.v2.calls), 1, "read once: nothing new was pushed")

    def test_s10_seven_kinds_of_bad_message_touch_nothing_and_are_counted(self):
        bad = [self.message(pad="x" * 3000), self.message(v=2), self.message(event="nonsense"), self.message(sessionID="not a session"),
               self.message(text="a prompt"), b"this is not json", bytes(range(128, 200))]
        for raw in bad:
            self.assertFalse(self.receiver.handle(raw))
        self.assertEqual((self.feed.seen, self.receiver.status()["ignored"]), ({}, 7))
        self.assertFalse(self.receiver.handle(self.message(sessionID="ses_stranger")), "a valid id of no agent of this fleet proves nothing")

    def test_s11_a_burst_is_limited_counted_audited_once_and_never_queued(self):
        for _ in range(100):
            self.receiver.handle(self.message())
        status = self.receiver.status()
        self.assertEqual((status["accepted"], status["rate_limited"]), (sb.PLUGIN_SESSION_PER_SECOND, 100 - sb.PLUGIN_SESSION_PER_SECOND))
        self.assertEqual([n for n, _ in self.audits].count("event.rate_limited"), 1)
        self.now[0] += 2
        self.assertTrue(self.receiver.handle(self.message()), "the next second starts clean: nothing was queued")

    def test_s15_an_event_limited_away_is_still_found_by_the_reconciliation_and_said_to_be_missed(self):
        engine = types.SimpleNamespace(v2=types.SimpleNamespace(calls=[], message_list=lambda *a, **k: [{"id": "msg_new"}]))
        self.feed.need("test")
        self.feed.handle(feed_line("session.step.ended", session=self.SESSION, assistantMessageID="msg_old"))
        for _ in range(sb.PLUGIN_SESSION_PER_SECOND + 5):
            self.receiver.handle(self.message())
        self.assertEqual(self.receiver.status()["rate_limited"], 5, "the last pushes were limited away")
        self.feed.seen[self.SESSION]["read"] = self.now[0] + 1   # the pushed read already happened: only the reconciliation is left to find it
        self.now[0] += sb.FEED_RECONCILE_SECONDS + 2
        self.feed.tick(engine, [self.SESSION])
        self.assertEqual([n for n, _ in self.audits].count("event.missed"), 1)

    def test_a_push_is_read_while_the_stream_is_down_at_most_four_sessions_a_pass_and_a_failed_read_is_tried_again(self):
        sessions = ["ses_%02d" % n for n in range(6)]
        self.receiver.known = lambda: set(sessions)
        self.feed.connected = False   # the stream is down and nobody registered as a consumer
        for session in sessions:
            self.receiver.handle(self.message(sessionID=session))
        engine = types.SimpleNamespace(v2=self.Stub())
        self.feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), sb.FEED_PUSHED_PER_PASS)
        self.feed.tick(engine, sessions)
        self.assertEqual(sorted(c[0] for c in engine.v2.calls), sessions, "the other two the next pass; none twice")

        class Failing:
            calls = 0

            def message_list(self, *a, **k):
                Failing.calls += 1
                raise sb.V2Error("backoff", None, "skipped")
        self.now[0] += 1
        self.receiver.handle(self.message(sessionID="ses_00"))
        self.feed.tick(types.SimpleNamespace(v2=Failing()), sessions)
        self.feed.tick(types.SimpleNamespace(v2=Failing()), sessions)
        self.assertEqual(Failing.calls, 2, "a failed read is tried again, and counted")
        self.assertEqual(self.feed.status()["read_errors"], 2)

    def test_under_v2_light_a_push_causes_no_read(self):
        engine = types.SimpleNamespace(v2=self.Stub(), light=True, feed=self.feed, discovery=types.SimpleNamespace(agents={"a": types.SimpleNamespace(session_id=self.SESSION)}))
        self.receiver.handle(self.message())
        sb.feed_tick(engine)
        self.assertEqual(engine.v2.calls, [], "the owner's hotfix switch stops every optional history read, a pushed one too")

    def test_s16_with_no_pushes_ten_simulated_minutes_for_twenty_idle_agents_cost_at_most_fifty_calls(self):
        engine = types.SimpleNamespace(v2=self.Stub())
        self.feed.need("test")
        sessions = ["ses_%02d" % n for n in range(20)]
        for _ in range(120):
            self.now[0] += 5
            self.feed.tick(engine, sessions)
        self.assertLessEqual(len(engine.v2.calls), 50)

    def test_u4_no_text_of_a_message_reaches_the_audit_or_the_status(self):
        self.receiver.handle(self.message(text="SENTINEL-PROMPT-TEXT"))
        for _ in range(40):
            self.receiver.handle(self.message(event="idle"))
        self.assertNotIn("SENTINEL-PROMPT-TEXT", repr(self.audits) + repr(self.receiver.status()) + repr(self.feed.status()) + repr(self.feed.seen))

    def test_u5_a_pushed_claim_causes_no_write(self):
        engine = types.SimpleNamespace(v2=self.Stub())   # any call but message_list raises
        self.receiver.handle(self.message(id="msg_claimed", person=True))
        self.feed.tick(engine, [self.SESSION])
        self.assertEqual(len(engine.v2.calls), 1)

    def test_the_socket_carries_pushes_to_the_receiver_without_a_reply_and_judge_requests_as_before(self):
        state = Path(tempfile.mkdtemp(prefix="sbp"))
        self.addCleanup(shutil.rmtree, state, True)
        arrived = threading.Event()

        def on_event(raw):
            self.receiver.handle(raw)
            arrived.set()
        server = sb.JudgeServer(state, lambda text: sb.policy_reply("deny", "judged"), on_event).start()
        self.addCleanup(server.stop)
        reply = sb.ask_judge_daemon(state, json.dumps({"sessionID": self.SESSION, "tool": "shell", "arguments": "ls", "outcome": "allow"}), 2)
        self.assertEqual(reply["outcome"], "deny", "a judge request is answered as before")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        with sb.in_directory(state):
            sock.connect(sb.POLICY_SOCKET)
        sock.sendall(self.message() + b"\n")   # no shutdown(SHUT_WR): the server ends at the newline and may close first, and macOS then refuses it (ENOTCONN)
        self.assertEqual(sock.recv(100), b"", "a push gets no reply")
        sock.close()
        self.assertTrue(arrived.wait(5), "the receiver was reached")   # the wait is only a failure bound
        self.assertEqual(self.receiver.status()["accepted"], 1)

    def test_the_socket_is_bound_for_pushes_when_the_judge_is_off_and_only_when_a_feed_runs(self):
        state = Path(tempfile.mkdtemp(prefix="sbp"))
        self.addCleanup(shutil.rmtree, state, True)
        config = {"policy": {"enabled": False}, "v2_plugin_events": "auto"}
        self.assertIsNone(sb.start_judge_server(config, state, None, time.time, types.SimpleNamespace(feed=None, audit=None)))
        engine = types.SimpleNamespace(feed=self.feed, audit=None)
        server = sb.start_judge_server(config, state, None, time.time, engine)
        self.addCleanup(server.stop)
        self.assertTrue((state / sb.POLICY_SOCKET).exists())
        self.assertIsNotNone(engine.event_receiver)
        server.stop()
        self.assertIsNone(sb.start_judge_server(dict(config, v2_plugin_events="off"), state, None, time.time, engine), "off: the gate is the only signal")


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

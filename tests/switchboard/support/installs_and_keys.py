"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): installs_and_keys."""
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
from support.paths import BIN, REPO
from support.core import Base
from support.loader import sb


GOOD_PLAN = """## Intent
Commissioning works.
Done when: (1) an owner can commission an agent; (2) it answers a chat message.

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: S2
    covers: DW-2
    text: Given a commissioned agent, when the owner sends a chat message, then it answers
    proof: live
    pass: the reply arrives within 5 s and is longer than 0 characters
    discriminates: a stub that returns a canned empty reply fails
    evaluator: tester
  - id: U1
    covers: PC-1
    text: Existing projects with agents are listed exactly as before this change
    proof: test
    pass: the golden list output is byte-identical to the stored fixture
    discriminates: a change in ordering or fields shows as a diff against the fixture
    evaluator: ci
```
"""


GOOD_DONE_WHEN = "(1) an owner can commission an agent; (2) it answers a chat message."


DECISIONS_PLUGIN = REPO / "plugins" / "fleet-decisions-tui"


ACME_API, ACME_WEB = "acme/api", "acme/web"


FAKE_TOKENS = {"alice-bot": "tok-alice-bot-SECRET-1111", "acme-ci": "tok-acme-ci-SECRET-2222"}


DEFAULT_TOKEN = "tok-default-SECRET-0000"


ALL_FAKE_TOKENS = list(FAKE_TOKENS.values()) + [DEFAULT_TOKEN]


def secrets_in(text):
    return [token for token in ALL_FAKE_TOKENS if token in text]


PR16_KEY = "pr16-canary-key-3c91d7a2e8f4"  # a made-up key: it only has to be findable


PR16_NOTICE = "No decision-model key: messages are not classified and tool calls are not judged."


class KeyCase(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.key_file = self.config_file.parent / "jev.key"

    def put_key(self, text=PR16_KEY, mode=0o600):
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        if self.key_file.exists():
            self.key_file.unlink()
        self.key_file.write_text(text + "\n")
        os.chmod(str(self.key_file), mode)
        return self.key_file


class FakeDaemons:
    """Stands in for Popen in ensure-on-use tests: like a daemon, the spawned process takes the lock."""

    def __init__(self, test, state):
        self.state, self.spawned = state, []
        test.addCleanup(self.close)
        self.fds = []

    def __call__(self, argv, **kwargs):
        self.spawned.append((argv, kwargs))
        fd = os.open(str(self.state / "run.lock"), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.fds.append(fd)

    def close(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []


class InstallFakes:
    """A pretend machine for `install`: OpenCode answers --version, herdr lists and links plugins, and the
    service managers load and unload. Records every call. Nothing real runs, and nothing real is touched."""

    def __init__(self, home, version="opencode v2.0.22"):
        self.calls, self.version = [], version
        self.linked = False
        self.loaded = False
        self.reaper_loaded = False
        self.fail = {}
        self.integration_calls, self.integration_reports_at = [], None
        self.binary = str(home / "v2dir" / "opencode.exe")
        os.makedirs(os.path.dirname(self.binary))
        Path(self.binary).write_text("#!/bin/sh\n")
        os.chmod(self.binary, 0o755)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        for fragment, code in self.fail.items():
            if fragment in " ".join(argv):
                return subprocess.CompletedProcess(argv, code, "", "boom: %s" % fragment)
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == self.binary and argv[1:] == ["--version"]:
            return ok(self.version + "\n")
        if argv[0] == self.binary:
            return subprocess.CompletedProcess(argv, 1, "", "not running")
        if argv[:3] == ["herdr", "plugin", "list"]:
            return ok("1 plugin installed:\n- board (Board) enabled\n" + ("- fleet-switchboard (Fleet Switchboard) enabled\n" if self.linked else ""))
        if argv[:3] == ["herdr", "plugin", "link"]:
            self.linked = True
            return ok()
        if argv[:3] == ["herdr", "plugin", "unlink"]:
            self.linked = False
            return ok()
        if argv[:3] == ["herdr", "integration", "status"] or argv[:4] in (["herdr", "integration", "install", "opencode"],
                                                                        ["herdr", "integration", "uninstall", "opencode"]):
            # herdr writes its OpenCode integration to $HOME/.config/opencode and ignores XDG_CONFIG_HOME: where
            # the file lands depends on the HOME the caller gave it, and a link there carries it elsewhere
            home = (kwargs.get("env") or {}).get("HOME") or os.environ["HOME"]
            target = os.path.join(home, ".config", "opencode", "plugins", "herdr-agent-state.js")
            self.integration_calls.append((argv[2], dict(kwargs.get("env") or {})))
            if argv[2] == "install":
                os.makedirs(os.path.dirname(target), exist_ok=True)
                Path(target).write_text("// herdr agent state\n")
                return ok("installed opencode integration at %s\n" % target)
            if argv[2] == "uninstall":
                if os.path.exists(target):
                    os.unlink(target)
                return ok("uninstalled\n")
            shown = self.integration_reports_at or target
            state = "current (v13)" if os.path.exists(target) else "not installed"
            return ok("claude: current (v10) (/elsewhere/hooks/herdr-agent-state.sh)\nopencode: %s (%s)\n" % (state, shown))
        reaper = "cloud-reaper" in " ".join(argv)   # the reaper's service is loaded and unloaded on its own
        if argv[0] == "launchctl":
            if argv[1] == "print":
                return subprocess.CompletedProcess(argv, 0 if (self.reaper_loaded if reaper else self.loaded) else 113, "", "")
            setattr(self, "reaper_loaded" if reaper else "loaded", argv[1] == "bootstrap")
            return ok()
        if argv[0] == "systemctl":
            if "is-enabled" in argv:
                return subprocess.CompletedProcess(argv, 0 if (self.reaper_loaded if reaper else self.loaded) else 1, "", "")
            if "enable" in argv:
                setattr(self, "reaper_loaded" if reaper else "loaded", True)
            if "disable" in argv:
                setattr(self, "reaper_loaded" if reaper else "loaded", False)
            return ok()
        raise AssertionError("install reached for a real program: %r" % argv)

    def commands(self, program):
        return [c for c in self.calls if c[0] == program]


def tree_of(root):
    """Every file under root with its content: what a dry run must leave exactly as it was."""
    found = {}
    for base, dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(base, name)
            found[path] = open(path, "rb").read()
        for name in dirs:
            found[os.path.join(base, name) + "/"] = None
    return found


class InstallCase(Base):
    PLATFORM = "darwin"

    def setUp(self):
        super().setUp()
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["XDG_DATA_HOME"] = str(self.home / "xdg-data")
        self.machine = InstallFakes(self.home)
        self.daemons = FakeDaemons(self, sb.ensure_state_dir())
        self.data = self.home / "xdg-data" / "fleet-switchboard"
        self.private = self.data / "profile"            # v2's own profile: XDG_CONFIG_HOME, XDG_DATA_HOME and HOME of its wrapper
        self.profile = self.private / "config" / "opencode"
        self.wrapper = self.data / "bin" / "fleet-opencode"
        self.shared = self.home / "xdg-config" / "opencode"  # what the person's ordinary OpenCode reads

    def install(self, *flags, status="STATUS\n", platform=None):
        out = io.StringIO()
        args = sb.build_parser().parse_args(["install"] + [f for f in flags if f != "--dry-run"] + (
            ["--dry-run"] if "--dry-run" in flags else []))
        installer = sb.Installer(self.machine, self.daemons, time.time, platform or self.PLATFORM, REPO,
                                 dry_run="--dry-run" in flags, out=out, shared_profile=args.shared_profile,
                                 profile=args.profile, gh_user=args.gh_user)
        try:
            if args.uninstall:
                installer.uninstall(not args.no_service, not args.no_herdr_link)
            else:
                installer.install(args.opencode, not args.no_service, not args.no_herdr_link,
                                  status=lambda: status, with_reaper=args.with_cloud_reaper)
        except sb.InstallError as err:
            return out.getvalue(), str(err)
        return out.getvalue(), None

    def statuses(self, text):
        """step -> its status word(s), from lines like `config   done /path`."""
        found = {}
        for line in text.splitlines():
            match = re.match(r"^(\S+(?: \S+)*?) {2,}(done|already done|would be done|skipped \(.*?\)|will run|would run|FAILED)", line)
            if match:
                found.setdefault(match.group(1), []).append(match.group(2))
        return found

    def service_path(self):
        if self.PLATFORM == "darwin":
            return self.home / "Library" / "LaunchAgents" / "com.fleet-kit.switchboard.plist"
        return self.home / "xdg-config" / "systemd" / "user" / "fleet-kit-switchboard.service"


class CloudReaperInstall:
    def reaper_path(self):
        if self.PLATFORM == "darwin":
            return self.home / "Library" / "LaunchAgents" / "com.fleet-kit.cloud-reaper.plist"
        return self.home / "xdg-config" / "systemd" / "user" / "fleet-kit-cloud-reaper.service"

    def reaper_calls(self):
        return [c for c in self.machine.calls if c[0] in ("launchctl", "systemctl") and "cloud-reaper" in " ".join(c)
                or c[:3] == ["systemctl", "--user", "daemon-reload"]]

    def test_the_default_install_does_not_touch_the_reaper(self):
        out, error = self.install("--opencode", self.machine.binary)
        self.assertIsNone(error, error)
        self.assertFalse(self.reaper_path().exists())
        self.assertNotIn("cloud reaper", out)
        self.assertFalse([c for c in self.machine.calls if "cloud-reaper" in " ".join(c)])

    def test_opt_in_writes_loads_once_and_a_second_run_changes_nothing(self):
        out, error = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIsNone(error, error)
        self.assertTrue(self.reaper_path().is_file())
        self.assertEqual(self.statuses(out)["cloud reaper"], ["will run", "done"])
        loads = [c for c in self.machine.calls if c[:2] == (["launchctl", "bootstrap"] if self.PLATFORM == "darwin" else [""])[:2]
                 and str(self.reaper_path()) in c] if self.PLATFORM == "darwin" else \
            [c for c in self.machine.calls if c[:4] == ["systemctl", "--user", "enable", "--now"] and c[4] == "fleet-kit-cloud-reaper.service"]
        self.assertEqual(len(loads), 1)
        second, _ = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertEqual(self.statuses(second)["cloud reaper"], ["already done"])

    def test_uninstall_unloads_and_removes_it_and_a_never_installed_one_is_silent(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        out, _ = self.install("--uninstall")
        self.assertFalse(self.reaper_path().exists())
        off = ["launchctl", "bootout", "gui/%d/com.fleet-kit.cloud-reaper" % os.getuid()] if self.PLATFORM == "darwin" else \
            ["systemctl", "--user", "disable", "--now", "fleet-kit-cloud-reaper.service"]
        self.assertIn(off, self.machine.calls)
        out2, _ = self.install("--uninstall")
        self.assertNotIn("cloud reaper", out2)

    def test_a_dry_run_writes_nothing(self):
        out, _ = self.install("--opencode", self.machine.binary, "--with-cloud-reaper", "--dry-run")
        self.assertFalse(self.reaper_path().exists())
        self.assertEqual(self.statuses(out)["cloud reaper"], ["would run", "would be done"])
        self.assertFalse([c for c in self.machine.calls if c[0] in ("launchctl", "systemctl") and "cloud-reaper" in " ".join(c)
                          and c[1] in ("bootstrap", "bootout", "enable", "disable")])

    def test_a_changed_setting_unloads_rewrites_and_loads_again_in_that_order(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        before = len(self.machine.calls)
        with mock.patch.dict(os.environ, {"CW_CLOUD_NAMESPACE": "acme-cw"}):
            out, error = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIsNone(error, error)
        order = [c for c in self.machine.calls[before:] if "cloud-reaper" in " ".join(c) and "print" not in c and "is-enabled" not in c]
        self.assertEqual(len(order), 2, order)
        self.assertIn("disable" if self.PLATFORM == "linux" else "bootout", order[0])
        self.assertIn("enable" if self.PLATFORM == "linux" else "bootstrap", order[1])
        self.assertIn("acme-cw", self.reaper_path().read_text())

    def test_a_failing_load_stops_the_install_with_the_command_named(self):
        self.machine.fail["bootstrap" if self.PLATFORM == "darwin" else "enable --now fleet-kit-cloud-reaper"] = 5
        out, error = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIsNotNone(error)
        self.assertRegex(error, "bootstrap|enable")

    SAMPLE_SETTINGS = {"CW_KUBE_CONTEXT": "acme-cw", "CW_CLOUD_NAMESPACE": "acme-cw", "CW_CLOUD_SERVER": "https://api.example.internal:6443",
                       "CW_CLOUD_TOKEN_FILE": "/home/user/.cw/token", "CW_KUBECONFIG": "/home/user/.kube/config", "CW_KUBECTL": "kubectl",
                       "CW_CLOUD_CA_FILE": "/home/user/ca.pem", "CW_CLOUD_RECORD_DIR": "/home/user/runs", "CW_RUN_RECORD_DIR": "/home/user/runs",
                       "CW_CLOUD_AUDIT": "/home/user/audit.jsonl"}

    def test_uninstall_removes_and_unloads_a_reaper_installed_with_any_one_setting_and_with_all(self):
        self.assertEqual(sorted(self.SAMPLE_SETTINGS), sorted(sb.REAPER_SETTING_NAMES))   # a name added needs a row
        cases = [{name: value} for name, value in self.SAMPLE_SETTINGS.items()] + [dict(self.SAMPLE_SETTINGS), {}]
        for settings in cases:
            with self.subTest(settings=sorted(settings)):
                self.setUp()
                with mock.patch.dict(os.environ, settings):
                    self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
                self.assertTrue(self.reaper_path().is_file())
                self.assertTrue(self.machine.reaper_loaded)
                out, error = self.install("--uninstall")
                self.assertIsNone(error, error)
                self.assertFalse(self.reaper_path().exists(), out)
                self.assertFalse(self.machine.reaper_loaded)   # unloaded, not just deleted

    def test_a_file_that_is_not_ours_is_left_but_the_job_is_still_unloaded(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.reaper_path().write_text("a file someone else made\n")
        out, _ = self.install("--uninstall")
        self.assertTrue(self.reaper_path().exists())
        self.assertFalse(self.machine.reaper_loaded)
        self.assertIn("the job is unloaded", out)

    def test_uninstall_with_no_service_still_unloads_and_removes_the_reaper(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        out, _ = self.install("--uninstall", "--no-service")
        self.assertFalse(self.reaper_path().exists())
        self.assertTrue(self.service_path().exists())   # the switchboard's own service is what --no-service leaves
        self.assertFalse(self.machine.reaper_loaded)

    def test_the_settings_come_from_the_environment_and_a_password_one_stops_the_install(self):
        with mock.patch.dict(os.environ, {"CW_CLOUD_NAMESPACE": "acme-cw"}):
            self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIn("CW_CLOUD_NAMESPACE", self.reaper_path().read_text())
        self.assertIn("acme-cw", self.reaper_path().read_text())
        with mock.patch.dict(os.environ, {"CW_CLOUD_SERVER": "https://u:p4ss@api.example.internal"}):
            out, error = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIn("CW_CLOUD_SERVER", error)
        self.assertNotIn("p4ss", error + out)


# The states a file the installer writes can be found in: name -> how to make it at `path` (victim is a file elsewhere)
BAD_TARGET_STATES = {
    "a symlink": lambda path, victim: os.symlink(str(victim), str(path)),
    "a FIFO": lambda path, victim: os.mkfifo(str(path)),
    "a directory": lambda path, victim: os.mkdir(str(path)),
    "group-writable": lambda path, victim: (path.write_text("mine\n"), os.chmod(str(path), 0o664)),
    "owned by another user": lambda path, victim: path.write_text("mine\n"),   # the installer is made to see another uid
}


BAD_STATE_REASON = {"a symlink": "symbolic link", "a FIFO": "not a regular file", "a directory": "not a regular file",
                    "group-writable": "written by its group", "owned by another user": "owned by another user"}


class WrittenFilesAreGuarded:
    """One row per file the installer writes (the switchboard's own service file and the reaper's) x one per bad state."""

    def targets(self):
        return {"service": (self.service_path(), ()), "cloud reaper": (self.reaper_path(), ("--with-cloud-reaper",))}

    def attempt(self, state, path, flags, uninstall=False):
        victim = self.home / "victim"
        victim.write_text("precious\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        BAD_TARGET_STATES[state](path, victim)
        patch = mock.patch.object(sb, "current_uid", return_value=os.getuid() + 1) if state == "owned by another user" else mock.patch.object(sb, "current_uid", sb.current_uid)
        with patch:
            if uninstall:
                result = self.install("--uninstall")
            else:
                result = self.install("--opencode", self.machine.binary, *flags)
        return victim, result

    def test_install_refuses_each_bad_state_for_each_file_and_changes_nothing(self):
        for label, (path, flags) in self.targets().items():
            for state in BAD_TARGET_STATES:
                with self.subTest(file=label, state=state):
                    self.setUp()   # a fresh machine for each row
                    path, flags = self.targets()[label]
                    victim, (out, error) = self.attempt(state, path, flags)
                    self.assertIsNotNone(error, "%s %s: no refusal" % (label, state))
                    self.assertIn(str(path), error)
                    self.assertIn(BAD_STATE_REASON[state], error)   # the right check refused it, not another one
                    self.assertEqual(victim.read_text(), "precious\n")
                    if state == "a symlink":
                        self.assertTrue(path.is_symlink())
                    loads = [c for c in self.machine.calls if c[0] in ("launchctl", "systemctl") and c[1] in ("bootstrap", "enable")
                             and (label != "cloud reaper" or "cloud-reaper" in " ".join(c))]
                    self.assertEqual(loads, [])

    def test_uninstall_leaves_each_bad_state_for_each_file_alone_and_says_so(self):
        for label, (path, flags) in self.targets().items():
            for state in BAD_TARGET_STATES:
                with self.subTest(file=label, state=state):
                    self.setUp()
                    path, flags = self.targets()[label]
                    victim, (out, error) = self.attempt(state, path, flags, uninstall=True)
                    self.assertIsNone(error, error)
                    self.assertEqual(victim.read_text(), "precious\n")
                    self.assertTrue(os.path.lexists(str(path)), "%s %s was removed" % (label, state))
                    self.assertIn("skipped", out)

    def test_uninstall_removes_only_the_file_this_installers_template_makes(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.reaper_path().write_text("a file someone else made\n")
        out, error = self.install("--uninstall")
        self.assertTrue(self.reaper_path().exists())
        self.assertIn("is not the file this installer writes", out)
        self.reaper_path().unlink()

    def test_a_replaced_file_is_replaced_by_rename_never_written_through(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        inode = os.stat(str(self.reaper_path())).st_ino
        with mock.patch.dict(os.environ, {"CW_CLOUD_NAMESPACE": "acme-cw"}):
            self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertNotEqual(os.stat(str(self.reaper_path())).st_ino, inode)
        self.assertEqual([p.name for p in self.reaper_path().parent.iterdir() if p.name.startswith(".")], [])   # no temp file left


class RealStatusAfterInstall:
    """The real `status` an install prints is about the platform install ran for, not the one the tests run on."""

    def installed_status(self, installed_for, host):
        out = io.StringIO()
        installer = sb.Installer(self.machine, self.daemons, time.time, installed_for, REPO, out=out)
        with mock.patch.object(sb.sys, "platform", host):
            installer.install(self.machine.binary)
        return out.getvalue()


# ======================================================================
# PR 17: standing orders an agent does not forget
#
# docs/switchboard.md, "Standing orders (PR 17)". The faults added here:
# orders-start-only, orders-wake, anyone-can-order, orders-never-judged.
# ======================================================================
ORDERS_CHARTER_BODY = "```yaml\norchestrator: platform\nstatus: active\n```\n\nThe charter.\n"


TIER_LABELS = {"awaiting_label": "me:awaiting-user", "awaiting_cos_label": "me:awaiting-cos"}


API, WEB = "acme/api", "acme/web"

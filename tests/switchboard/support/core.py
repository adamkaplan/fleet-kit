"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): core."""
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
from support.loader import sb


class FakeRunner:
    """Stands in for subprocess.run. Records every call; answers from a script.

    Every call gets `default`, or `route(argv)` when a route is given. An
    answer is (returncode, stdout, stderr), or an exception instance to raise.
    """

    def __init__(self, default=(0, '{"data": {}}', ""), route=None):
        self.calls = []
        self.default = default
        self.route = route

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        answer = self.route(argv) if self.route else self.default
        if isinstance(answer, BaseException):
            raise answer
        code, stdout, stderr = answer
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


SAFE_PATH = "/usr/bin:/bin"


REAL_NODE = shutil.which("node")   # looked up at import, before a test narrows PATH (the plugin cross-check needs it)


def remove_tree_patiently(tmp, tries=20, pause=0.1):
    """#93: clean a TemporaryDirectory up even when a daemon thread of the test is still writing into it for a
    moment (`Directory not empty`): try again until it is gone, then let the last error show."""
    for attempt in range(tries):
        try:
            tmp.cleanup()
            return
        except OSError:
            if attempt == tries - 1:
                raise
            time.sleep(pause)


def cos_rules_text(*rules, shape="flow"):
    """A chief-of-staff definition's permission list from (resource, effect) pairs, in the installed one-line flow shape (#191) or the older block shape."""
    if shape == "flow":
        return '  - {action: "*", resource: "*", effect: deny}\n' + "".join('  - {action: shell, resource: "%s", effect: %s}\n' % r for r in rules)
    return "".join('  - action: "shell"\n    resource: "%s"\n    effect: %s\n' % r for r in rules)


COS_GUARDS = (("fleet-switchboard gates *enforce*", "deny"), ("fleet-switchboard gates *ENFORCE*", "deny"), ("fleet-switchboard gates on*", "deny"),
              ("fleet-switchboard waive *G6*", "deny"), ("fleet-switchboard waive *G8*", "deny"))


# the installed shape: broad allows, guards after them
COS_ALLOWING = cos_rules_text(("fleet-switchboard gates *", "allow"), ("fleet-switchboard waive *", "allow"), *COS_GUARDS)


COS_ALLOWING_FULL = cos_rules_text(("fleet-switchboard gates *", "allow"), ("fleet-switchboard waive *", "allow"), ("timeout ?? fleet-switchboard *", "allow"),
                                   *COS_GUARDS, *((r.replace("fleet-switchboard", "timeout * fleet-switchboard", 1), e) for r, e in COS_GUARDS))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(remove_tree_patiently, tmp)
        self.home = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "xdg-config"),
            "XDG_STATE_HOME": str(self.home / "xdg-state"),
            # A test that forgets to inject a fake must fail, not reach the
            # machine's real herdr or OpenCode: neither lives in these.
            "PATH": SAFE_PATH,
            # the installed chief-of-staff definition: one that allows the way out of a refusal (the gate preflight reads it)
            sb.COS_DEFINITION_ENV: str(self.home / "cos-definition.md"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # The version of a program is asked for under a production bound (3 s) a loaded machine can overrun: tests get a bound that cannot
        # expire, and a clean cache; the one test of the bound patches its own.
        bound = mock.patch.object(sb, "PROGRAM_VERSION_SECONDS", 120)
        bound.start()
        self.addCleanup(bound.stop)
        sb._VERSIONS.clear()
        # No test reads the real machine's load or memory: the cpu and memory guards run in every engine pass, and a loaded CI runner would wake the Chief of
        # Staff in an unrelated test. A test of the guards patches its own figures over these.
        guard = sb.load_cpu_guard()
        for target, name, value in ((os, "getloadavg", lambda: (0.1, 0.1, 0.1)), (guard, "memory_reading", lambda: guard.Memory(0, 10 * 10 ** 9, 5 * 10 ** 9)),
                                    (guard, "process_table", lambda *a, **k: {})):
            calm = mock.patch.object(target, name, value)
            calm.start()
            self.addCleanup(calm.stop)
        (self.home / "cos-definition.md").write_text(COS_ALLOWING)
        for name in (sb.LAB_ENV, sb.FAULT_ENV, "HERDR_PANE_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(name, None)
        self.config_file = self.home / "xdg-config" / "fleet-switchboard" / "config.json"
        self.state_dir = self.home / "xdg-state" / "fleet-switchboard"

    def write_config(self, obj):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def fake_bin(self, name, body):
        path = self.home / name
        path.write_text("#!/bin/sh\n" + body)
        os.chmod(str(path), 0o755)
        return path

    def run_cli(self, path, *argv):
        env = dict(os.environ)
        config = Path(env.get("XDG_CONFIG_HOME", "")) / "fleet-switchboard" / "config.json"
        try:
            if str(self.home) in config.read_text():   # its opencode is a fake script of this test's own, never the real service
                env[sb.ALLOW_REAL_V2_ENV] = "1"
        except OSError:
            pass
        return subprocess.run([sys.executable, "-B", str(path), *argv],
                              capture_output=True, text=True, env=env)

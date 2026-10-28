"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): bases."""
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
import importlib
from support.paths import BIN, REPO
from support.core import Base
from support.fake_hubs import LEDGER_HEAD, LEDGER_PLAN
from support.loader import captured, sb
from support.rigs import World, made_up_pid
from support.scenario_roles import FAKE_GH
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


class PidTable(dict):
    """`self.px[4242]`: the made-up number a test uses where it once wrote the literal 4242. The same key is the same number inside one test, and
    each test gets numbers of its own."""

    def __missing__(self, key):
        self[key] = made_up_pid()
        return self[key]


class StandInPids:
    """Mixin: `self.px`, one fresh PidTable per test."""

    @property
    def px(self):
        return self.__dict__.setdefault("_px", PidTable())


class LedgerBase(Base):
    """#107: the evidence ledger (`prove`, `verdict`) and the read-only gates view. Synthetic names; nothing enforces."""

    NAMES = ("coder", "rev", "tst", "own", "cibot", "other")

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=self.NAMES)
        self.plan_issue = {"number": 7, "state": "open", "title": "plan", "body": LEDGER_PLAN, "updated_at": "2026-01-01T00:00:00Z"}
        self.comments = []
        self.pr = {"title": "Add it", "state": "OPEN", "headRefOid": LEDGER_HEAD, "reviewDecision": "", "author": {"login": "shared"},
                   "body": "PLAN: acme/app#7\ncanon: updated the docs", "labels": [], "reviews": [], "comments": [],
                   "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}]}
        self.gh_down = False
        self.configure()

    extra_config = None

    def configure(self, evaluators=None):
        raw = {"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "intent_repo": "acme/app"}
        raw.update(self.extra_config or {})
        raw["plan_evaluators"] = evaluators if evaluators is not None else {
            "reviewer": ["rev"], "tester": ["tst"], "owner": ["own"], "ci": ["cibot"]}
        self.write_config(raw)

    def route(self, argv, **kwargs):
        if argv[0] != FAKE_GH:
            return self.world.route(argv, **kwargs)
        if "--method" in argv or "-X" in argv:
            raise AssertionError("the ledger commands must only read GitHub: %r" % (argv,))
        if self.gh_down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: network unreachable")
        if argv[1] == "pr" and argv[2] == "view":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
        path = argv[2]
        if "/comments" in path:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.comments if "&page=1" in path else []), "")
        if re.search(r"/issues/7$", path):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.plan_issue), "")
        return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")

    terminal_problem = None   # what the owner-terminal check says in these tests (None: it is the owner's terminal)

    def run_as(self, who, *argv):
        if who is None:
            os.environ.pop("HERDR_PANE_ID", None)
        else:
            os.environ["HERDR_PANE_ID"] = self.world.panes[who]
        with captured() as (out, err), mock.patch.object(sb, "owner_terminal_problem", lambda *a, **k: self.terminal_problem):
            code = sb.main(list(argv), runner=self.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    PR = "acme/app#9"

    def prove(self, who, sid, *more, result="pass"):
        return self.run_as(who, "prove", sid, "--pr", self.PR, "--evidence", "ran the suite: 12 passed", "--result", result, *more)

    def verdict(self, who, sid, evaluator, result="pass", *more):
        return self.run_as(who, "verdict", sid, "--pr", self.PR, "--as", evaluator, "--result", result,
                           "--evidence", "judged it: matches the scenario", *more)

    def gates(self, *more):
        code, out, err = self.run_as("coder", "gates", self.PR, "--json", *more)
        return code, (json.loads(out) if out.strip().startswith("{") else None), err

    def ledger(self):
        path = self.world.state / "plan-ledger.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def row(self, report, gate):
        return next(r for r in report["gates"] if r["gate"] == gate)

    def all_proven(self):
        for sid in ("S1", "S2", "S3", "U1"):
            self.assertEqual(self.prove("coder", sid)[0], 0)
        for who, sid, ev in (("cibot", "S1", "ci"), ("tst", "S2", "tester"), ("rev", "S3", "reviewer"), ("cibot", "U1", "ci")):
            self.assertEqual(self.verdict(who, sid, ev)[0], 0, (who, sid))

"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): service_and_merge."""
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


class FakeOpenCodeService:
    """The OpenCode service, in memory: no socket, no thread, no sleep. It answers like the service (Basic auth `opencode:<password>`, the paths of
    the operation table); `connect` is the connection factory a test gives V2Http. Knobs: `up`, `delay` (a delay longer than the client's timeout
    is a timeout), `drop` (the service closes without a reply after receiving the request), `big` (bytes in a transcript reply), `password`."""

    def __init__(self, password="pw-secret-1"):
        self.password, self.up, self.delay, self.drop, self.big = password, True, 0.0, False, 0
        self.requests, self.connections = [], 0
        self.stream = []           # lines GET /api/event streams; a None is a quiet stretch (a read timeout); the stream then closes
        self.stream_status = 200

    def answer(self, method, path, body, authorization):
        token = base64.b64encode(("opencode:" + self.password).encode()).decode()
        if authorization != "Basic " + token:
            return 401, {"error": "unauthorized"}
        if path == "/api/event":
            self.requests.append((method, path, body))
            return self.stream_status, "STREAM"
        self.requests.append((method, path, body))
        if path.startswith("/api/session/ses_a/message"):
            return 200, {"data": [{"id": "msg_%d" % i, "pad": "x" * (self.big or 1)} for i in range(1 if self.big else 2)], "cursor": {"next": None}}
        if path == "/api/info":
            return 200, {"version": "2.0.24"}
        if path == "/api/session/active":
            return 200, {"ses_a": {"status": "idle"}}
        if path.startswith("/api/session/ses_a/synthetic") and method == "POST":
            return 200, {"ok": True}
        if path == "/api/session/ses_missing":
            return 404, {"_tag": "SessionNotFoundError"}
        return 404, {"error": "no such path"}

    def connect(self, host, port, timeout=None):
        return FakeServiceConnection(self, timeout)

    def write_service_file(self, path, url="http://127.0.0.1:49374", pid=None, mode=0o600):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"id": "x", "version": "2.0.24", "url": url, "pid": pid or os.getpid(), "password": self.password}))
        os.chmod(str(path), mode)


class FakeServiceResponse:
    def __init__(self, status, raw):
        self.status, self._raw, self.will_close = status, raw, False
        self.on_read = None   # a test hook: called before each read (to advance a fake clock)

    def read(self, amount=None):
        if self.on_read is not None:
            self.on_read()
        if amount is None:
            raw, self._raw = self._raw, b""
        else:
            raw, self._raw = self._raw[:amount], self._raw[amount:]
        return raw


class FakeStreamResponse:
    """An event stream response: readline gives the scripted lines, a None raises the read timeout, the end is b""."""

    def __init__(self, status, lines):
        self.status, self.lines, self.will_close = status, lines, True

    def readline(self, limit=-1):
        if not self.lines:
            return b""
        line = self.lines.pop(0)
        if line is None:
            raise socket.timeout("timed out")
        raw = (line + "\n").encode()
        return raw[:limit] if limit and limit > 0 else raw

    def read(self, amount=None):
        return b"{}"


class FakeServiceConnection:
    """What http.client.HTTPConnection gives V2Http, over the in-memory service."""

    def __init__(self, service, timeout):
        self.service, self.timeout, self.sock, self.pending, self.broken = service, timeout, None, None, False

    def connect(self):
        if not self.service.up:
            raise ConnectionRefusedError(61, "Connection refused")
        self.sock = object()
        self.service.connections += 1

    def request(self, method, target, body=None, headers=None):
        if self.broken or not self.service.up:
            raise BrokenPipeError(32, "Broken pipe")
        self.pending = (method, target, body or b"", (headers or {}).get("Authorization"))

    def getresponse(self):
        method, target, body, auth = self.pending
        status, obj = self.service.answer(method, target, body, auth)
        if obj == "STREAM":
            return FakeStreamResponse(status, list(self.service.stream))
        if self.service.drop:
            self.broken = True
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        if self.service.delay > (self.timeout or 1e9):
            raise socket.timeout("timed out")
        return FakeServiceResponse(status, json.dumps(obj).encode())

    def close(self):
        self.sock = None


class V2TransportBase(Base):
    def setUp(self):
        super().setUp()
        self.service = FakeOpenCodeService()
        self.service_file = self.home / "opencode" / "service.json"
        self.service.write_service_file(self.service_file)
        self.cli_calls = []

    def runner(self, argv, **kwargs):
        self.cli_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"via": "cli"}}), "")

    def write_private(self, text):
        """A service file that is private to the user (0600), so that only its content or size can be what is wrong with it."""
        self.service_file.write_text(text)
        os.chmod(str(self.service_file), 0o600)

    def route(self, timeout=5, alive=None):
        return sb.V2Http(self.service_file, timeout, alive=alive or (lambda pid: True), connect=self.service.connect)

    def client(self, timeout=5, **kwargs):
        return sb.V2Client("/fake/bin/opencode", {}, self.runner, timeout, http=self.route(timeout), **kwargs)


def feed_line(kind, session="ses_w", created=None, **data):
    body = {"id": "evt_%s" % kind, "type": kind, "data": dict({"sessionID": session}, **data)}
    if created is not None:
        body["created"] = created
    return "data: " + json.dumps(body)


class FakeMergedGh:
    """`gh` for the merged-label pass: `pr view N` answers from `answers` ((code, stdout, stderr) or an exception); a label DELETE is recorded and succeeds."""

    def __init__(self, answers=None):
        self.answers, self.reads, self.removed = answers or {}, [], []

    def __call__(self, argv, **kwargs):
        if argv[1:3] == ["pr", "view"]:
            self.reads.append((argv[3], argv[argv.index("--repo") + 1]))
            answer = self.answers[int(argv[3])]
            if isinstance(answer, Exception):
                raise answer
            return subprocess.CompletedProcess(argv, answer[0], answer[1], answer[2])
        if "--method" in argv and argv[argv.index("--method") + 1] == "DELETE":
            self.removed.append(urllib.parse.unquote(argv[2].rsplit("/", 1)[1]))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError("the merged-label pass reaches for nothing else: %r" % (argv,))


def write_lane_hours(state, lanes, hour=None):
    """The daemon's hourly record with `lanes` ({lane: {account: {endpoint class: n}}}) for the current hour (counting on, as the daemon makes it)."""
    start = int(time.time()) // 3600 * 3600 if hour is None else hour
    (Path(state) / "agent-calls-hours.json").write_text(json.dumps({"v": 1, "since": time.time() - 7200, "hours": {str(start): {"lanes": lanes}}}))


MG_TIP, MG_NEWTIP, MG_HEAD = "1" * 40, "2" * 40, "a" * 40


def mg_iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class FakeMergeGitHub:
    """A fake GitHub for the merge gate: `fake(argv) -> (code, out, err)`. Reads and writes are logged apart; a close then reopen starts a new run on the
    CURRENT tip that finishes `rerun_after` seconds later with `rerun_result` (None: never). `sleep` moves the clock and finishes what is due."""

    def __init__(self, number=12, repo="acme-org/widget-service", start=1_800_000_000):
        self.number, self.repo, self.now = number, repo, start
        self.tip, self.head, self.base, self.default, self.state, self.labels = MG_TIP, MG_HEAD, "main", "main", "OPEN", []
        self.runs = [self.make_run(1, "ci", 10, "completed", "success", MG_TIP, start - 600)]
        self.branch_runs = [{"id": 90, "name": "ci", "workflow_id": 10, "head_sha": MG_TIP, "conclusion": "success", "status": "completed", "created_at": mg_iso(start - 900)}]
        self.jobs = {2: [{"name": "unit-tests", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]}
        self.reads, self.writes, self.broken = [], [], set()
        self.merges, self.blocking_review, self.merge_own = [], False, []
        self.rerun_after, self.rerun_result, self.reopen_fails, self.due = 60, "success", False, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for run in list(self.due):
            if self.now >= run["_due"]:
                run.update(status="completed", conclusion=self.rerun_result)
                self.due.remove(run)

    def make_run(self, rid, name, workflow, status, conclusion, base, created):
        return {"id": rid, "name": name, "workflow_id": workflow, "status": status, "conclusion": conclusion, "head_sha": self.head if hasattr(self, "head") else MG_HEAD,
                "created_at": mg_iso(created), "pull_requests": [{"number": self.number, "base": {"sha": base, "ref": "main"}}]}

    def __call__(self, argv, own=True):
        if argv[0] == "api" and argv[1] == "-X":
            self.writes.append(list(argv))
            state = argv[-1].split("=", 1)[1]
            if state == "closed":
                self.state = "CLOSED"
            elif self.reopen_fails:
                return 1, "", "HTTP 502"
            else:
                self.state = "OPEN"
                run = self.make_run(2, "ci", 10, "in_progress", None, self.tip, self.now)
                run["_due"] = self.now + self.rerun_after
                if self.rerun_result is not None:
                    self.due.append(run)
                self.runs = [run]
            return 0, "{}", ""
        if argv[:2] == ["pr", "merge"]:
            self.merges.append(list(argv))
            self.merge_own.append(own)
            return 0, "", ""
        self.reads.append(list(argv))
        key = " ".join(argv)
        if any(word in key for word in self.broken):
            return 1, "", "HTTP 500 boom"
        if argv[:2] == ["pr", "view"]:
            reviews = [{"author": {"login": "reviewer-x"}, "state": "CHANGES_REQUESTED", "commit": {"oid": self.head}}] if self.blocking_review else []
            if "reviews" in key:
                return 0, json.dumps({"headRefOid": self.head, "reviews": reviews, "reviewDecision": ""}), ""
            return 0, json.dumps({"headRefOid": self.head, "baseRefName": self.base, "state": self.state, "isDraft": False, "labels": [{"name": n} for n in self.labels]}), ""
        path = argv[1]
        if path == "repos/%s" % self.repo:
            return 0, json.dumps({"default_branch": self.default}), ""
        if "/git/ref/heads/" in path:
            return 0, json.dumps({"object": {"sha": self.tip}}), ""
        if "event=pull_request" in path:
            return 0, json.dumps({"workflow_runs": [{k: v for k, v in r.items() if k != "_due"} for r in self.runs]}), ""
        if "event=push" in path:
            return 0, json.dumps({"workflow_runs": self.branch_runs}), ""
        found = re.search(r"/runs/(\d+)/jobs", path)
        if found:
            return 0, json.dumps({"jobs": self.jobs.get(int(found.group(1)), [])}), ""
        return 1, "", "HTTP 404"

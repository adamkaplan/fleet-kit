"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): orders_and_adapters."""
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
from support.loader import _load, captured, sb


def cos_orders_file(test):
    return test.config_file.parent / "standing-orders.md"


def put_cos_orders(test, text, mtime=None):
    path = cos_orders_file(test)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))
    return path


# ======================================================================
# PR notices: `notice pr`, the fourth source of derive_decisions, the Heads-up section's data
#
# docs/switchboard.md, "PR notices". A notice is information, not a decision: kind `notice`, tier `human`, id
# `pr:<repo>#N`, never counted with the decisions. Facts come from one cached `gh pr view` per open notice.
# ======================================================================
def pr_view_json(title="Add the parser", state="OPEN", additions=120, deletions=14, files=5, checks=None,
                 review="APPROVED", **more):
    checks = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}] if checks is None else checks
    return json.dumps(dict({"title": title, "state": state, "additions": additions, "deletions": deletions,
                            "changedFiles": files, "statusCheckRollup": checks, "reviewDecision": review}, **more))


class FakeGhPr:
    """A `gh` runner for `pr view`: answers per PR number from `answers` (a (code, stdout, stderr) tuple, or an
    exception); records every call. Reaching for anything else fails the test."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        assert argv[1:3] == ["pr", "view"], argv
        answer = self.answers[int(argv[3])]
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(argv, answer[0], answer[1], answer[2])


HEAD_A, HEAD_B = "a" * 40, "b" * 40


def captain_pr(body="MERGE: captain\n\nland it", head=HEAD_A, reviews=None, author="coder-bot", state="OPEN", comments=(), **more):
    """pr_view_json for a PR with a body, an author, a head and reviews [(login, state, commit oid)]."""
    fields = {"body": body, "headRefOid": head, "author": {"login": author}, "reviews": [
        dict({"author": {"login": r[0]}, "state": r[1], "commit": {"oid": r[2]}}, **({"body": r[3]} if len(r) > 3 else {}))
        for r in (reviews or [])]}
    fields["comments"] = [{"author": {"login": author}, "body": text + (
        "\n<!-- fleet-switchboard:from=%s -->" % who if who else "")} for who, text in comments]
    fields.update(more)
    return pr_view_json(state=state, **fields)


# ======================================================================
# The Decision baseline (characterisation). It pins what the Decision subsystem does TODAY, through the seams
# that outlive a refactor: the CLI (`decisions`, `decisions --json`, `decisions --fresh`, `status`, the write
# commands), decisions.json, the effects on the fake gh and the fake v2, and the plugin's view of the same list.
# The expected outputs are plain files in scenarios/switchboard/decisions-baseline/ (synthetic names only).
# A refactor that keeps behaviour changes no test and no file here, only the code under test. To record the
# files again after a CHANGE OF BEHAVIOUR THAT WAS MEANT: DECISIONS_BASELINE_UPDATE=1 bin/test-switchboard
# TestDecisionBaseline, then read the diff of the files like any other change. docs/switchboard.md, "Testing".
#
# Where only an internal function can be called there is ONE adapter function (named baseline_*, below): the
# single place to repoint when the engine's decision code moves.
# ======================================================================
BASELINE_DIR = REPO / "scenarios" / "switchboard" / "decisions-baseline"


BASELINE_UPDATE = os.environ.get("DECISIONS_BASELINE_UPDATE") == "1"


def baseline_failing_send(reason):
    """ADAPTER: the engine's `send` delivers nothing: `reason` is why, or None for an error (today Switchboard.send)."""
    if reason is None:
        return mock.patch.object(sb.Switchboard, "send", side_effect=RuntimeError("no such agent"))
    return mock.patch.object(sb.Switchboard, "send", return_value={"sent": False, "reason": reason})


@contextlib.contextmanager
def baseline_failing_source(name):
    """ADAPTER: one source of the derivation raises (today the decisions_from_<name> function the derivation calls)."""
    target = {"v2": "decisions_from_requests", "github": "decisions_from_github", "reports": "decisions_from_reports",
              "notices": "decisions_from_notices"}[name]
    with mock.patch.object(sb, target, side_effect=RuntimeError("%s is broken" % name)):
        yield


_BASELINE_JSON_ERROR = re.compile(r"(?:Expecting|Extra data|Unterminated|Invalid|Illegal)[^\n]*")


def baseline_portable(data):
    """`data` with the interpreter's own wording taken out (a json parse error reads differently from Python
    3.9 to 3.12), so the files are the same on every Python. The product cuts the message at a fixed width, so it is dropped to its end."""
    if isinstance(data, str):
        return _BASELINE_JSON_ERROR.sub("<json parse error>", data)
    if isinstance(data, list):
        return [baseline_portable(x) for x in data]
    if isinstance(data, dict):
        return {k: baseline_portable(v) for k, v in data.items()}
    return data


def baseline_golden(test, name, data):
    """Compare `data` with the golden file `name`.json (or record it under DECISIONS_BASELINE_UPDATE=1)."""
    data = baseline_portable(data)
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path = BASELINE_DIR / (name + ".json")
    if BASELINE_UPDATE:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    test.assertTrue(path.exists(), "no golden file %s: record it with DECISIONS_BASELINE_UPDATE=1" % path.name)
    test.maxDiff = None
    test.assertEqual(path.read_text(), text, "the Decision baseline %s changed: behaviour changed" % path.name)


EXTERNAL_ADAPTER = BIN / "external_adapter.py"


EA = _load("external_adapter_under_test", EXTERNAL_ADAPTER)


class FakeDoorServer:
    """A stdlib http.server: an HTTP API under /v1 and a JSON-RPC tool channel at /rpc, one credential header."""

    KEY = "k-test-secret-0123456789"
    NAMESPACE = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

    def __init__(self):
        import http.server
        self.items, self.changes, self.log, self.requests = {}, [], [], []
        self.inject, self.delay, self.down, self.disabled, self.sse = [], 0, False, False, False
        self.seq, self.now, self.tasks, self.sessions, self.sid = 100, 1000.0, {}, set(), 0
        self.seen, self.lock = {}, threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.handle(self, "GET")

            def do_POST(self):
                outer.handle(self, "POST")

            def do_PUT(self):
                outer.handle(self, "PUT")

            def do_PATCH(self):
                outer.handle(self, "PATCH")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- helpers for tests
    def add_change(self, what, item, gap=1, **fields):
        self.seq += gap
        change = {"n": self.seq, "what": what, "item": item, "when": "t%d" % self.seq}
        change.update(fields)
        self.changes.append(change)
        return change

    def fail_next(self, op=None, status=500, body=None, headers=None, level="http", count=1):
        self.inject.append({"op": op, "status": status, "body": body if body is not None else {"code": "x",
                            "message": "injected"}, "headers": headers or {}, "level": level, "count": count})

    def count(self, op, channel=None):
        return len([1 for c, o in self.log if o == op and (channel is None or c == channel)])

    # ---- the transport
    def send(self, h, status, body=None, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode("utf-8") if body is not None else b""
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def handle(self, h, method):
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        if self.down:
            h.close_connection = True
            h.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.dispatch_http(h, method, raw)

    def take(self, op, level):
        for one in self.inject:
            if one["level"] == level and one["op"] in (None, op):
                one["count"] -= 1
                if one["count"] <= 0:
                    self.inject.remove(one)
                return one
        return None

    def dispatch_http(self, h, method, raw):
        parts = urllib.parse.urlsplit(h.path)
        got = h.headers.get("X-Auth", "")
        if got != "Key " + self.KEY:
            return self.send(h, 401, {"code": "cred", "message": "credential refused: %s" % got})
        if method in ("POST", "PUT", "PATCH") and "json" not in (h.headers.get("Content-Type") or ""):
            return self.send(h, 415, {"code": "media", "message": "json only"})
        query = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path == "/rpc":
            return self.rpc(h, raw)
        op, ident, seat = self.route(method, parts.path)
        if op is None:
            return self.send(h, 404, {"code": "nf", "message": "no such path"})
        shot = self.take(op, "http")
        if shot:
            return self.send(h, shot["status"], shot["body"], shot["headers"])
        try:
            args = json.loads(raw) if raw else {}
        except ValueError:
            return self.send(h, 400, {"code": "bad", "message": "not json"})
        args.update(query)
        self.log.append(("http", op))
        self.requests.append(("http", op, args))
        status, body, headers = self.run(op, args, ident, seat)
        self.send(h, status, body, headers)

    ROUTES = [("GET", r"^/v1/seats$", "feed"), ("POST", r"^/v1/items$", "raise"), ("GET", r"^/v1/items$", "items"),
              ("POST", r"^/v1/items/([^/]+)/revise$", "revise"), ("POST", r"^/v1/items/([^/]+)/withdraw$", "withdraw"),
              ("POST", r"^/v1/items/([^/]+)/acted$", "acted"),
              ("PUT", r"^/v1/seats/([^/]+)/tasks/([^/]+)$", "work_put"),
              ("PATCH", r"^/v1/seats/([^/]+)/tasks/([^/]+)$", "work_patch"),
              ("POST", r"^/v1/seats/([^/]+)/tasks/([^/]+)/finish$", "work_finish"),
              ("GET", r"^/v1/seats/([^/]+)/tasks/([^/]+)$", "work_get"),
              ("GET", r"^/v1/seats/([^/]+)/tasks$", "work_list")]

    def route(self, method, path):
        for m, pattern, op in self.ROUTES:
            found = re.match(pattern, path)
            if m == method and found:
                groups = [urllib.parse.unquote(g) for g in found.groups()]
                if op.startswith("work"):
                    return op, (groups[1] if len(groups) > 1 else None), groups[0]
                return op, (groups[0] if groups else None), None
        return None, None, None

    TOOLS = {"seats_list": "feed", "item_raise": "raise", "items_read": "items", "item_revise": "revise",
             "item_withdraw": "withdraw", "item_acted": "acted"}

    def rpc(self, h, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return self.send(h, 400, {"code": "bad", "message": "not json"})
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            self.sid += 1
            session = "sess-%d" % self.sid
            self.sessions.add(session)
            return self.reply(h, mid, {"protocolVersion": "2025-03-26", "capabilities": {}}, {"Mcp-Session-Id": session})
        if h.headers.get("Mcp-Session-Id") not in self.sessions:
            return self.send(h, self.session_miss, {"code": "nf", "message": "no session"})
        if mid is None:
            return self.send(h, 202, raw=b"")
        if method == "tools/list":
            return self.reply(h, mid, {"tools": [{"name": n} for n in self.TOOLS]})
        if method != "tools/call":
            return self.reply(h, mid, None, error={"code": -32601, "message": "no method"})
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        op = self.TOOLS.get(name)
        if op is None:
            return self.reply(h, mid, {"content": [{"type": "text", "text": "MCP error -32602: unknown tool"}],
                                       "isError": True})
        shot = self.take(op, "http")
        if shot:
            return self.send(h, shot["status"], shot["body"], shot["headers"])
        if "bad_schema" in args:
            return self.reply(h, mid, {"content": [{"type": "text", "text": "MCP error -32602: Input validation "
                                                                          "error: bad_schema"}], "isError": True})
        shot = self.take(op, "result")
        if shot:
            return self.reply(h, mid, {"content": [{"type": "text", "text": json.dumps(shot["body"])}],
                                       "isError": True})
        self.log.append(("tool", op))
        self.requests.append(("tool", op, args))
        args = dict(args)
        ident = args.pop("id", None)
        status, body, _ = self.run(op, args, ident, None)
        self.reply(h, mid, {"content": [{"type": "text", "text": json.dumps(body)}], "isError": status >= 300})

    def reply(self, h, mid, result, headers=None, error=None):
        envelope = {"jsonrpc": "2.0", "id": mid}
        envelope.update({"error": error} if error else {"result": result})
        if self.sse:
            data = ("event: message\ndata: %s\n\n" % json.dumps(envelope)).encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(data)
        else:
            self.send(h, 200, envelope, headers)

    # ---- the made-up protocol
    RAISE_KEYS = {"op_id", "seat", "label", "body", "advice", "floor", "choices"}
    RAISE_OPT = {"because", "stakes", "refs"}

    def run(self, op, args, ident, seat):
        if self.disabled and op not in ("feed", "items", "work_get", "work_list"):
            return (409 if op.startswith("work") else 403), {"code": "off", "message": "disabled"}, {}
        return getattr(self, "op_" + op)(args, ident, seat)

    session_miss = 404   # the status of a call that names a session the server does not know
    page_size = 0   # 0: no paging; else a reply holds this many entries and names the token of the next page

    def paged(self, rows, args):
        if not self.page_size:
            return rows, {}
        start = int(str(args.get("token") or "p0")[1:])
        chunk = rows[start:start + self.page_size]
        more = {"more": {"token": "p%d" % (start + self.page_size)}} if start + self.page_size < len(rows) else {}
        return chunk, more

    def op_feed(self, args, ident, seat):
        since = int(args.get("since") or 0)
        new = [c for c in self.changes if c["n"] > since]
        chunk, more = self.paged(new, args)
        body = {"state": "off" if self.disabled else "on", "seats": [{"seat": "seat-1"}], "feed": chunk,
                "cursor": max([c["n"] for c in chunk] + [since])}
        body.update(more)
        return 200, body, {}

    def op_raise(self, args, ident, seat):
        if not self.RAISE_KEYS <= set(args) or set(args) - self.RAISE_KEYS - self.RAISE_OPT:
            return 400, {"code": "bad", "message": "invalid body"}, {}
        try:
            uuid.UUID(args["op_id"])
        except ValueError:
            return 400, {"code": "bad", "message": "invalid body"}, {}
        if args["seat"] != "seat-1":
            return 404, {"code": "nf", "message": "no such seat"}, {}
        if not args["body"] or not args["advice"]:
            return 400, {"code": "bad", "message": "invalid body"}, {}
        item_id = "it-" + hashlib.sha256(args["op_id"].encode()).hexdigest()[:8]
        if item_id in self.items:
            if self.items[item_id]["_args"] != args:
                return 409, {"code": "clash", "message": "operation conflict"}, {}
            return 201, {"item": item_id}, {}
        choices = []
        for index, choice in enumerate(args["choices"]):
            one = dict(choice)
            one.setdefault("key", "k%d" % (index + 1))
            choices.append(one)
        self.items[item_id] = {"item": item_id, "state": "unrated", "body": args["body"], "choices": choices,
                               "advice": args["advice"], "answer": None, "ask_back": None, "withdrawn": None,
                               "reversals": [], "opened": "t0", "op_id": args["op_id"], "floor": args["floor"],
                               "label": args["label"], "_args": args}
        return 201, {"item": item_id}, {}

    def visible(self, item):
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def op_items(self, args, ident, seat):
        chunk, more = self.paged([self.visible(i) for i in self.items.values()], args)
        return 200, dict({"items": chunk, "state": "on"}, **more), {}

    def find(self, ident):
        return self.items.get(ident)

    def op_revise(self, args, ident, seat):
        item = self.find(ident)
        if item is None:
            return 404, {"code": "nf", "message": "no such item"}, {}
        if item["answer"] is not None and item["state"] == "closed" or item["withdrawn"]:
            return 409, {"code": "decided", "message": "read its answer instead"}, {}
        if not ({"body", "advice", "choices"} & set(args)):
            return 400, {"code": "bad", "message": "incomplete"}, {}
        item.update({k: v for k, v in args.items() if k in ("body", "advice", "choices")})
        item["state"] = "unrated"
        return 200, {"ok": True}, {}

    def op_withdraw(self, args, ident, seat):
        item = self.find(ident)
        if item is None:
            return 404, {"code": "nf", "message": "no such item"}, {}
        if item["state"] == "closed":
            return 409, {"code": "decided", "message": "read its answer instead"}, {}
        item["withdrawn"] = {"why": args.get("why")}
        item["state"] = "pulled"
        return 200, {"ok": True, "withdrawn": item["withdrawn"]}, {}

    def op_acted(self, args, ident, seat):
        item = self.find(ident)
        if item is None:
            return 404, {"code": "nf", "message": "no such item"}, {}
        known = {(item["answer"] or {}).get("answer_id")} | {r.get("answer_id") for r in item["reversals"]}
        if args.get("on") not in known:
            return 409, {"code": "clash", "message": "that is not an answer to this item"}, {}
        return 200, {"ok": True, "on": args["on"]}, {}

    def task_of(self, seat, ident):
        return self.tasks.get((seat, ident))

    def needs_reason(self, args):
        return args.get("state") in ("paused", "stuck") and not args.get("why_state")

    def op_work_put(self, args, ident, seat):
        if self.needs_reason(args):
            return 400, {"code": "bad", "message": "say what the work waits for"}, {}
        if not args.get("title") or ("progress" in args and not (args["progress"] or {}).get("text")):
            return 400, {"code": "bad", "message": "title or progress text is empty"}, {}
        task = self.task_of(seat, ident)
        if task is None:
            task = {"id": ident, "end": None, "recorded_at": self.now}
            task.update(args)
            self.tasks[(seat, ident)] = task
            return 200, {"task": task, "recorded": True}, {}
        if task["end"]:
            return 409, {"code": "task_done", "message": "finished"}, {}
        return 200, {"task": task, "recorded": False}, {}

    def op_work_patch(self, args, ident, seat):
        if self.needs_reason(args):
            return 400, {"code": "bad", "message": "say what the work waits for"}, {}
        task = self.task_of(seat, ident)
        if task is None:
            return 404, {"code": "nf", "message": "no task"}, {}
        if task["end"]:
            return 409, {"code": "task_done", "message": "finished"}, {}
        if all(task.get(k) == v for k, v in args.items()):
            return 200, {"task": task, "recorded": False}, {}
        if self.now - task["recorded_at"] < 60:
            return 429, {"code": "slow", "message": "too soon", "retry_after_s": 60}, {"Retry-After": "60"}
        task.update(args)
        task["recorded_at"] = self.now
        return 200, {"task": task, "recorded": True}, {}

    def op_work_finish(self, args, ident, seat):
        task = self.task_of(seat, ident)
        if task is None:
            return 404, {"code": "nf", "message": "no task"}, {}
        if task["end"] and task["end"]["state"] != args.get("end"):
            return 409, {"code": "task_done", "message": "finished otherwise"}, {}
        task["end"] = {"state": args.get("end"), "said": args.get("said")}
        return 200, {"task": task, "recorded": True}, {}

    def op_work_get(self, args, ident, seat):
        task = self.task_of(seat, ident)
        return (200, {"task": task}, {}) if task else (404, {"code": "nf", "message": "no task"}, {})

    def op_work_list(self, args, ident, seat):
        return 200, {"tasks": [t for (s, _), t in self.tasks.items() if s == seat]}, {}


FAKE_MAPPING = {
    "mapping_version": 1, "base_url": "http://127.0.0.1:1", "tool_path": "/rpc", "http_prefix": "/v1",
    "auth": {"header": "X-Auth", "scheme": "Key"}, "key_source": "env:FAKE_DOOR_KEY", "channel": "tool",
    "project": "seat-1", "timeouts": {"request": 2, "command": 5},
    "retry": {"attempts": 3, "base_delay": 0.5, "max_delay": 1.5, "max_retry_after_sleep": 5,
              "default_retry_after": 1},
    "poll_interval": 30, "work_interval": 60, "idempotency": {"namespace": FakeDoorServer.NAMESPACE},
    "operations": {
        "feed": {"tool": "seats_list", "http": {"method": "GET", "path": "/seats"}, "cursor_param": "since",
                 "next_param": "token"},
        "raise": {"tool": "item_raise", "http": {"method": "POST", "path": "/items"}},
        "items": {"tool": "items_read", "http": {"method": "GET", "path": "/items"}, "next_param": "token"},
        "revise": {"tool": "item_revise", "id_arg": "id", "http": {"method": "POST", "path": "/items/{id}/revise"}},
        "withdraw": {"tool": "item_withdraw", "id_arg": "id",
                     "http": {"method": "POST", "path": "/items/{id}/withdraw"}},
        "acted": {"tool": "item_acted", "id_arg": "id", "http": {"method": "POST", "path": "/items/{id}/acted"}},
        "work_put": {"http": {"method": "PUT", "path": "/seats/{project}/tasks/{id}"}},
        "work_patch": {"http": {"method": "PATCH", "path": "/seats/{project}/tasks/{id}"}},
        "work_finish": {"http": {"method": "POST", "path": "/seats/{project}/tasks/{id}/finish"}},
        "work_get": {"http": {"method": "GET", "path": "/seats/{project}/tasks/{id}"}},
        "work_list": {"http": {"method": "GET", "path": "/seats/{project}/tasks"}}},
    "fields": {
        "raise": {"idempotency": "op_id", "project": "seat", "class": "label", "text": "body",
                  "recommendation": "advice", "floor": "floor", "options": "choices", "grounds": "because",
                  "stakes": "stakes", "evidence": "refs"},
        "option": {"label": "name", "cost": "price", "recommended": "preferred", "id": "key"},
        "evidence": {"text": "note", "link": "href"},
        "revise": {"idempotency": "op_id", "text": "body", "recommendation": "advice", "options": "choices"},
        "withdraw": {"reason": "why"},
        "acted": {"idempotency": "op_id", "answer": "on", "outcome": "result", "note": "note"},
        "work": {"title": "title", "kind": "kind", "state": "state", "state_reason": "why_state", "progress": "progress",
                 "progress_text": "text", "doer": "doer", "end": "end", "summary": "said"}},
    "responses": {
        "item_id": "item", "items": "items",
        "item": {"id": "item", "status": "state", "text": "body", "answer": "answer", "clarification": "ask_back",
                 "withdrawn": "withdrawn", "reversals": "reversals", "opened_at": "opened", "idempotency": "op_id"},
        "feed": {"cursor": "cursor", "changes": "feed", "status": "state"}, "page": {"info": "more", "next": "token"},
        "change": {"seq": "n", "kind": "what", "item_id": "item", "at": "when", "tier": "level", "floor": "floor",
                   "text": "body", "awaits_rating": "rerate", "replaces": "replaces", "reason": "why"},
        "answer": {"id": "answer_id", "disposition": "how", "option_id": "chosen", "text": "words", "rationale": "why",
                   "clarification": "ask_back", "rejection": "no_reason", "by": "who"},
        "work": {"item": "task", "items": "tasks", "recorded": "recorded"}},
    "enums": {
        "tier": {"low": "minor", "moderate": "middling", "high": "major", "reserved": "sealed"},
        "disposition": {"accept": "yes", "edit": "reword", "reject": "no", "clarify": "ask"},
        "status": {"awaiting_rating": "unrated", "with_owner": "waiting", "sent_back": "returned",
                   "decided": "closed", "withdrawn": "pulled"},
        "change_kind": {"answered": "answer", "rated": "rating", "revised": "restated", "reversed": "flip",
                        "withdrawn": "pull"},
        "outcome": {"done": "did", "undone": "undid", "not_undone": "could_not"},
        "work_state": {"queued": "waiting", "running": "going", "awaiting": "paused", "blocked": "stuck"},
        "work_end": {"done": "ok", "failed": "bad", "cancelled": "dropped"},
        "integration_status": {"active": "on", "disabled": "off"}},
    "tier_map": {"orchestrator": {"class": "routine", "floor": "low"}, "cos": {"class": "notable", "floor": "high"},
                 "human": {"class": "grave", "floor": "reserved"}},
    "roles": [{"when": {"type": "person"}, "role": "human"}, {"when": {"type": "bot"}, "role": "orchestrator"},
              {"when": {"type": "owner_bot"}, "role": "cos"}],
    "errors": {"code_field": "code", "message_field": "message", "retry_after_field": "retry_after_s",
               "codes": {"off": "DisabledError", "decided": "AlreadyDecided", "clash": "Conflict",
                         "slow": "RateLimited"}},
    "options": {"id_prefix": "c", "key_marker": "ref:"}, "defaults": {"recommendation": "proceed"}}


def fake_mapping(base_url, channel="tool", **over):
    data = copy.deepcopy(FAKE_MAPPING)
    data["base_url"], data["channel"] = base_url, channel
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data


class ExternalAdapterCase(unittest.TestCase):
    """A fake server, and an adapter on it with recorded sleeps and a controllable clock."""

    def setUp(self):
        self.server = FakeDoorServer()
        self.addCleanup(self.server.close)
        self.slept, self.clock_now = [], [0.0]
        self.tmp = Path(tempfile.mkdtemp(prefix="ext-adapter-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def adapter(self, channel="tool", **over):
        return EA.McpRestAdapter(fake_mapping(self.server.url, channel, **over), sleep=self.slept.append,
                                 clock=lambda: self.clock_now[0], environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY})

    def push(self, adapter, d, key):
        """The port's operation for a decision-shaped dict (op, external_id, status, reason are the operation's)."""
        d = dict(d)
        op, ext, reason = d.pop("op", None), d.pop("external_id", None), d.pop("reason", None)
        if op is None:
            op = "raise" if not ext else ("withdraw" if d.get("status") == "withdrawn" else "revise")
        operation = {"op": op, "key": "dk:" + key, "external_id": ext, "revision": 1, "decision": d}
        if reason:
            operation["note"] = reason
        done = adapter.push(operation, key)
        assert set(done) == {"external_id", "url"}, done
        return done["external_id"]

    def decision(self, **over):
        found = {"title": "Ship it?", "body": "The build is green.", "tier": "orchestrator",
                 "recommendation": "yes", "options": ["approve", {"label": "hold", "cost": "a day"}]}
        found.update(over)
        return found

    def answer_fields(self, **over):
        found = {"answer_id": "a-1", "how": "yes", "chosen": "k1", "words": "approve", "who": {"type": "person"}}
        found.update(over)
        return found


class FakeExternal(sb.decision_core.ExternalDecisionAdapter):
    """An in-process adapter: items by outside id, idempotent by key, a switch for down, and a queue of events."""

    def __init__(self):
        self.up, self.items, self.applied_keys, self.calls, self.events = True, {}, {}, [], []
        self.lose_ack = False   # apply the next write, then fail as if the answer was lost

    def health(self):
        return None if self.up else "the fake is down"

    def push(self, operation, idempotency_key):
        self.calls.append((operation["op"], operation["key"], idempotency_key))
        if not self.up:
            raise ConnectionError("the fake is down")
        if idempotency_key in self.applied_keys:
            return self.applied_keys[idempotency_key]
        op = operation["op"]
        if op == "raise":
            ext = "ext-%d" % (len(self.items) + 1)
            self.items[ext] = {"key": operation["key"], "decision": operation["decision"], "floor": operation["floor"],
                               "state": "open", "notes": [], "revisions": [operation["revision"]]}
        else:
            ext = operation["external_id"]
            item = self.items[ext]
            if op == "revise":
                item["decision"], item["floor"] = operation["decision"], operation["floor"]
                item["revisions"].append(operation["revision"])
            elif op == "status":
                item["floor"] = operation["floor"]
                if operation.get("note"):
                    item["notes"].append(operation["note"])
            elif op == "withdraw":
                item["state"] = "withdrawn"
        answer = {"external_id": ext}
        self.applied_keys[idempotency_key] = answer
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("the answer was lost")
        return answer

    def lookup(self, key):
        for ext, item in self.items.items():
            if item["key"] == key:
                return {"external_id": ext, "revision": item["revisions"][-1]}
        return None

    def pull_changes(self, cursor):
        start = cursor or 0
        return list(self.events[start:]), len(self.events)

    def open_items(self):
        return {e: i for e, i in self.items.items() if i["state"] == "open"}


class FakeWorkSink(sb.work_sync.WorkSink):
    interval = 60

    def __init__(self):
        self.calls, self.down, self.defer = [], False, None

    def _do(self, *call):
        if self.down:
            raise ConnectionError("the sink is down")
        self.calls.append(call)

    def put(self, work_id, title, **fields):
        self._do("put", work_id, title, fields)

    def patch(self, work_id, **fields):
        if self.defer:
            return {"sent": False, "deferred": True, "due_in": self.defer}
        self._do("patch", work_id, fields)

    def finish(self, work_id, end, summary, **fields):
        self._do("finish", work_id, end, summary)

    def kinds(self):
        return [c[0] for c in self.calls]


class FakeWorkHost(sb.work_sync.Host):
    def __init__(self):
        self.agent_list, self.report_list, self.link_table, self.events = [], [], {}, []

    def agents(self):
        return list(self.agent_list)

    def reports(self):
        return list(self.report_list)

    def title_of(self, repo, issue):
        return "The ask %s#%d" % (repo.split("/")[1], issue)

    def links(self):
        return self.link_table

    def audit(self, event, subject, detail):
        self.events.append((event, subject, detail))


def worker(name="w1", issue=2, repo="acme/api", status="idle", **more):
    return dict({"name": name, "role": "coder", "reports_to": "boss", "repo": repo, "issue": issue, "status": status,
                 "intent": None}, **more)


def report(state, text="", at=1, sender="w1", issue=2):
    return {"sender": sender, "issue": issue, "repo": "acme/api", "state": state, "text": text, "at": at}


class PermanentRefusal(Exception):
    permanent = True


class PickyExternal(FakeExternal):
    """A fake that refuses chosen operations: `refuse[op]` is the exception to raise instead of applying it."""

    def __init__(self):
        super().__init__()
        self.refuse, self.seen = {}, []

    def push(self, operation, idempotency_key):
        self.seen.append(operation)
        if operation["op"] in self.refuse and self.up:
            self.calls.append((operation["op"], operation["key"], idempotency_key))
            raise self.refuse[operation["op"]]
        return super().push(operation, idempotency_key)


class FakePruneDocker:
    """A scripted docker for `disk prune` and the report: a server version, volumes (name, anonymous, dangling, size text) and a record of every argv.
    `prune_calls` are the prune commands only (the read-only questions are in `calls`)."""

    def __init__(self, version="29.7.2", volumes=(), exits=(0, 0, 0), outputs=("Total reclaimed space: 1.2GB", "Total reclaimed space: 300MB", "Total reclaimed space: 5MB"),
                 version_code=0, platform="Docker Desktop 4.89.0", server_api="1.55", client_api="1.55", df_fails=False):
        self.version, self.volumes, self.version_code = version, list(volumes), version_code
        self.platform, self.server_api, self.client_api, self.df_fails = platform, server_api, client_api, df_fails
        self.calls, self.prune_calls = [], []
        self.replies = iter(zip(exits, outputs))

    def __call__(self, argv, **kwargs):
        a = list(argv[1:])
        self.calls.append(a)
        out = lambda text, code=0, err="": subprocess.CompletedProcess(argv, code, text, err)
        if a[:1] == ["version"]:
            return out("%s|%s|%s|%s\n" % (self.version, self.platform, self.server_api, self.client_api), self.version_code, "no server" if self.version_code else "")
        if a[:2] == ["volume", "ls"]:
            return out("\n".join(v[0] for v in self.volumes if v[2]))
        if a[:1] == ["inspect"]:
            ids = a[a.index("--") + 1:]
            return out(json.dumps([{"Name": v[0], "Labels": ({"com.docker.volume.anonymous": ""} if v[1] else {"x": "y"})} for v in self.volumes if v[0] in ids]))
        if a[:2] == ["system", "df"] and "-v" not in a:
            return out("TYPE TOTAL SIZE\nImages 3 1.2GB\n")
        if a[:2] == ["system", "df"] and self.df_fails:
            return out("", 1, "df failed")
        if a[:2] == ["system", "df"]:
            return out(json.dumps([{"Name": v[0], "Size": v[3]} for v in self.volumes]))
        if "prune" in a:
            self.prune_calls.append(a)
            code, text = next(self.replies)
            return out(text, code, "boom" if code else "")
        raise AssertionError("an unexpected docker call: %r" % a)


def reviewed_janitor_argv(argv, run):
    """`argv` for `disk janitor` with the reviewed fingerprint a person would copy from a dry run: `run(argv)` runs the command and returns its output text."""
    argv = list(argv)
    if "--apply" not in argv or "--i-reviewed" in argv:
        return argv
    dry = [a for a in argv if a != "--apply"]
    text = run(dry)
    found = re.findall(r"preview fingerprint: ([0-9a-f]{16})", text)
    return argv + ["--i-reviewed", found[-1] if found else "0" * 16]


def janitor_review(test, argv):
    """The full `disk janitor` argv with the fingerprint of a dry run just made, as a person would copy it."""
    def dry(args):
        with captured() as (out, err):
            sb.main(["disk", "janitor", *args], runner=test.routed, now=test.world.clock.now, socket_call=test.world.herdr.socket)
        return out.getvalue()
    return ["disk", "janitor", *reviewed_janitor_argv(argv, dry)]


class CallBound(AssertionError):
    pass


class BoundedList(list):
    """A list of recorded calls that refuses the 10001st: a mutant that never leaves its loop fails fast instead of growing until the machine is out of memory."""
    BOUND = 10000

    def append(self, item):
        if len(self) >= self.BOUND:
            raise CallBound("call bound of %d recorded calls passed: a loop that never ends" % self.BOUND)
        super().append(item)


class BoundedOut(io.StringIO):
    """The wrapper's output in a test: refuses more than 1 MB, or more than 10000 writes, for the same reason."""
    LIMIT = 1024 * 1024

    def __init__(self):
        super().__init__()
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self.writes > BoundedList.BOUND or self.tell() + len(text) > self.LIMIT:
            raise CallBound("call bound passed: the output holds %d bytes after %d writes" % (self.tell(), self.writes))
        return super().write(text)

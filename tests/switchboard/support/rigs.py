"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): rigs."""
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
from support.carry_git import GitHubOverGit
from support.loader import sb, with_http_head
from support.scenario_roles import GH_BIN, GH_REPO
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE, FakeV2, SimClock, TOKEN_NAME, TYPING_VERBS, _usage, parse_flags


def set_retired(state, retired):
    """A test sets the retired map: in the registry when it exists (the record), and in retired.json (its derived copy), as the registry itself would."""
    state = Path(state)
    (state / "retired.json").write_text(json.dumps(retired))
    path = state / "registry.json"
    if path.exists():
        doc = json.loads(path.read_text())
        doc["retired"] = retired
        path.write_text(json.dumps(doc))


_MADE_UP_LOCK = threading.Lock()


_MADE_UP_GIVEN = set()      # every number handed out in this run: never again


_MADE_UP_NEXT = [None]      # where the next look starts: above the largest live process


def _pid_limit():
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text()) - 1
    except (OSError, ValueError):
        return 99998         # macOS and the BSDs


def _pid_alive(pid):
    """True when a process has this number. No such process is the ONLY answer that frees it: another user's process (PermissionError) and any other
    error are alive (when in doubt a number is not handed out)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists, it is only not ours
    except OSError:
        return True          # when in doubt it is alive
    return True


_ANCESTORS = [None]


def _ancestors():
    """This process, its parent, its grandparent and so on up to 1: numbers a stand-in must never be (read once: a made-up number is asked for from inside
    threads, and a table read there would be slow)."""
    if _ANCESTORS[0] is None:
        _ANCESTORS[0] = _read_ancestors()
    return _ANCESTORS[0]


def _read_ancestors():
    chain, table = {os.getpid(), os.getppid()}, {}
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=20)
        for line in done.stdout.splitlines():
            words = line.split()
            if len(words) == 2 and all(w.isdigit() for w in words):
                table[int(words[0])] = int(words[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return chain
    pid = os.getppid()
    while pid > 1 and table.get(pid) and table[pid] not in chain:
        pid = table[pid]
        chain.add(pid)
    return chain


def _live_pids():
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=20)
        return {int(word) for word in done.stdout.split() if word.isdigit()}
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


_MADE_UP_BLOCK = [1024]          # a test is given room for this many numbers above the base, or the helper wraps BEFORE its first number (no test asks for more: a test pins it)


_MADE_UP_FRESH = [True]          # the next call is the first of a test (set at the start of every test run)


_MADE_UP_TEST = [None]           # the id of the test that is running


_MADE_UP_BY_TEST = {}            # test id -> the numbers it was given, in order: recorded WHERE THE HELPER IS CALLED, by any caller


def made_up_pid():
    """A process number no process has now: not this process, not its parent or grandparents, not a live process, not a number given before in this run.
    It asks for a number above the largest live one and proves it is not alive. A wrap to the small numbers at the top of the range happens only BETWEEN tests
    (before the first number of a test, when its block does not fit above the base): inside one test the numbers only ever ascend, so no test can depend on
    their order, and a test that asks for more than fits raises instead of wrapping. The test id is recorded with each number (`_MADE_UP_BY_TEST`)."""
    with _MADE_UP_LOCK:
        if _MADE_UP_NEXT[0] is None:
            # FLEET_TEST_PID_BASE pins where the first look starts (to run from the top of the range); default: above the largest live process
            _MADE_UP_NEXT[0] = int(os.environ.get("FLEET_TEST_PID_BASE") or 0) or max(_live_pids() | {os.getpid(), os.getppid()}) + 1
        limit, never = _pid_limit(), _ancestors() | {os.getpid(), os.getppid()}
        start = _MADE_UP_NEXT[0]
        if _MADE_UP_FRESH[0]:
            _MADE_UP_FRESH[0] = False
            if not 1 < start <= limit - _MADE_UP_BLOCK[0]:
                start = 2                      # between tests: begin again from the small numbers
        for pid in range(start, limit + 1):
            if pid in _MADE_UP_GIVEN or pid in never or _pid_alive(pid):
                continue
            _MADE_UP_GIVEN.add(pid)
            _MADE_UP_NEXT[0] = pid + 1
            _MADE_UP_BY_TEST.setdefault(_MADE_UP_TEST[0], []).append(pid)
            return pid
    raise AssertionError("a test asked for more made-up process numbers than fit above %d: a wrap inside one test would make their order depend on the machine" % start)


class FakeHerdr:
    """herdr 0.9.3's CLI and socket, for what the switchboard uses, as strict
    as the real one: the pane id of `report-metadata` and the title of
    `notification show` are positional, and a wrong shape is a usage error
    (exit 2), never a quiet success. Status follows the fake v2 session
    (working while a turn runs, `done` after one) unless a test pins it.

    Typing is refused (exit 2) and recorded in `typing_attempts`, except for
    the one scenario baseline, where the runner sets `allow_typing`: then
    `agent prompt` does what the TUI does, putting the text after what is in
    the input box and submitting the lot.

    Toasts follow herdr's rules: one a second, server-wide, none queued. A test
    can script other answers with `toast_replies`. A pane opened by
    `layout.apply` runs its command's argv, as a pane without a shell does.
    `launch_mode` makes the launched pane misbehave: "shell" (a shell, not the
    program), "other-session", "undetected" (herdr never names an agent).
    """

    def __init__(self, v2):
        self.v2 = v2
        self.panes = {}
        self.calls = []
        self.socket_calls = []
        self.typing_attempts = []
        self.toasts = []            # every toast shown: {"title", "body", "at"}
        self.toast_replies = []     # scripted reasons for the next notification calls
        self.closed = []
        self.input_box = {}         # pane -> the text typed in its input box
        self.allow_typing = False
        self.launch_mode = "ok"
        self.down = False
        self.socket_down = False
        self._pane_n = 0
        self._last_toast = None
        self.workspaces = {"w1": {"workspace_id": "w1", "label": "Switchboard Lab", "number": 1}}  # PR 16
        self.shell_panes = set()    # PR 16: the empty shell pane a new workspace opens with

    def new_pane_id(self, workspace="w1"):
        self._pane_n += 1
        return "%s:p%d" % (workspace, self._pane_n)

    def add(self, pane, session, status=None, program=None):
        self.panes[pane] = {"session": session, "status": status, "tokens": {},
                            "program": program or "%s -s %s" % (FAKE_OPENCODE, session)}

    def status(self, pane):
        entry = self.panes[pane]
        if entry["status"]:
            return entry["status"]
        session = self.v2.sessions.get(entry["session"])
        if session is None:
            return "unknown"
        if session["turn"] is not None:
            return "working"
        return "done" if session["idle"] is not None else "idle"

    def agent(self, pane):
        # herdr 0.9's shape: the session is an object, not a string (S6).
        entry = self.panes[pane]
        body = {"pane_id": pane, "agent": "opencode", "agent_status": self.status(pane),
                "workspace_id": pane.split(":")[0], "tab_id": self.tab_of(pane)}
        if entry["session"] and not entry.get("undetected"):
            body["agent_session"] = {"agent": "opencode", "kind": "id", "source": "herdr:opencode",
                                     "value": entry["session"]}
        if entry.get("tokens"):
            body["tokens"] = dict(entry["tokens"])  # values are strings, as in herdr
        return body

    # -- the CLI
    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        args = argv[1:]
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "error: no herdr server\n")
        if args[:2] in TYPING_VERBS and not (self.allow_typing and args[:2] == ["agent", "prompt"]):
            self.typing_attempts.append(args)
            return _usage(argv, "fake herdr: typing refused: %r" % args)
        handler = {("agent", "list"): self._agent_list, ("agent", "get"): self._agent_get,
                   ("agent", "prompt"): self._agent_prompt, ("pane", "report-metadata"): self._report_metadata,
                   ("notification", "show"): self._notification_show, ("pane", "process-info"): self._process_info,
                   ("pane", "close"): self._pane_close, ("workspace", "list"): self._workspace_list,
                   ("workspace", "create"): self._workspace_create, ("pane", "get"): self._pane_get, ("pane", "list"): self._pane_list,
                   ("tab", "close"): self._tab_close, ("tab", "get"): self._tab_get, ("tab", "list"): self._tab_list}.get(tuple(args[:2]))
        if tuple(args[:2]) == ("pane", "read"):
            positional, flags = parse_flags(args[2:], ("--source", "--lines", "--format"))
            if positional[0] not in self.panes:
                return subprocess.CompletedProcess(argv, 1, "", "error: pane %s not found\n" % positional[0])
            entry = self.panes[positional[0]]
            if entry.get("read_fails"):
                return subprocess.CompletedProcess(argv, 1, "", "error: read timed out\n")
            return subprocess.CompletedProcess(argv, 0, entry.get("screen") or "  Build auto · model\n", "")
        if handler is None:
            return _usage(argv, "fake herdr: refused %r" % args)
        try:
            body = handler(args[2:])
        except ValueError as err:
            return _usage(argv, str(err))
        except LookupError as err:
            return subprocess.CompletedProcess(argv, 1, "", "error: %s\n" % err)
        return subprocess.CompletedProcess(argv, 0, json.dumps(body) if body is not None else "", "")

    def _pane(self, pane):
        if pane not in self.panes:
            raise LookupError("no agent in pane %s" % pane)
        return pane

    def _pane_list(self, rest):
        """Every pane: the agent panes and `plain_terminals` (a list of {"pane_id", "cwd", "foreground_cwd"}), as `herdr pane list` shows them."""
        parse_flags(rest, ())
        panes = []
        for pane in sorted(self.panes):
            entry = self.panes[pane]
            panes.append({"pane_id": pane, "cwd": entry.get("cwd") or "/tmp", "foreground_cwd": entry.get("foreground_cwd") or entry.get("cwd") or "/tmp",
                          **({"revision": entry["revision"]} if "revision" in entry else {})})
        panes.extend(getattr(self, "plain_terminals", []))
        return {"result": {"type": "pane_list", "panes": panes}}

    def _agent_list(self, rest):
        parse_flags(rest, ())
        return {"result": {"type": "agent_list", "agents": [self.agent(p) for p in sorted(self.panes)]}}

    def _agent_get(self, rest):
        positional, _ = parse_flags(rest, ())
        if len(positional) != 1:
            raise ValueError("agent get takes one target")
        return {"result": {"type": "agent_info", "agent": self.agent(self._pane(positional[0]))}}

    def _agent_prompt(self, rest):
        positional, _ = parse_flags(rest, ())
        pane, text = self._pane(positional[0]), " ".join(positional[1:])
        draft = self.input_box.pop(pane, "")
        self.v2.start_turn(self.panes[pane]["session"], (draft + " " + text).strip(), fleet=False)
        return {"result": {"type": "ok"}}

    def _report_metadata(self, rest):
        positional, flags = parse_flags(rest, ("--source", "--token", "--clear-token", "--seq", "--ttl-ms"))
        if len(positional) != 1:
            raise ValueError("pane report-metadata takes the pane id as its one positional argument")
        pane = self._pane(positional[0])
        source = (flags.get("--source") or [""])[0]
        if not re.match(r"^[A-Za-z0-9:._-]{1,80}$", source):
            raise ValueError("--source is required: 1-80 of [A-Za-z0-9:._-]")
        sets, clears = flags.get("--token", []), flags.get("--clear-token", [])
        if not sets and not clears:
            raise ValueError("report-metadata needs --token or --clear-token")
        tokens = self.panes[pane].setdefault("tokens", {})
        for item in sets:
            name, _, value = item.partition("=")
            if not TOKEN_NAME.match(name) or not value.strip() or len(value) > 80:
                raise ValueError("bad token %r" % item)
            tokens[name] = value
        for name in clears:
            if not TOKEN_NAME.match(name):
                raise ValueError("bad token name %r" % name)
            tokens.pop(name, None)
        return None

    def _notification_show(self, rest):
        positional, flags = parse_flags(rest, ("--body", "--sound"))
        if len(positional) != 1 or not positional[0].strip():
            raise ValueError("notification show takes the title as its one positional argument")
        title, now = positional[0], self.v2.clock.now()
        if len(title) > 80:
            raise ValueError("fake herdr: title over 80 characters")
        reason = self.toast_replies.pop(0) if self.toast_replies else None
        if reason is None and self._last_toast is not None and now - self._last_toast < 1:
            reason = "rate_limited"
        if reason is None:
            self._last_toast = now
            self.toasts.append({"title": title, "body": (flags.get("--body") or [None])[0], "at": now})
        return {"result": {"type": "notification_show", "shown": reason is None, "reason": reason or "shown"}}

    @property
    def shell_pid(self):
        """The shell process number the fake reports for a pane: made up (a number no live process has), the same for the life of this fake."""
        if "_shell_pid" not in self.__dict__:
            self.__dict__["_shell_pid"] = made_up_pid()
        return self.__dict__["_shell_pid"]

    def _plain(self, pane):
        return next((t for t in getattr(self, "plain_terminals", []) if t["pane_id"] == pane), None)

    def _process_info(self, rest):
        positional, flags = parse_flags(rest, ("--pane",))
        if positional or len(flags.get("--pane", [])) != 1:
            raise ValueError("pane process-info takes --pane <ID>")
        plain = self._plain(flags["--pane"][0])
        if plain is not None and plain.get("process_fails"):
            raise LookupError("process info of %s cannot be read" % plain["pane_id"])
        if plain is not None:   # a terminal with no agent: its foreground processes are what the test says (default: a bare shell)
            return {"result": {"type": "pane_process_info", "process_info": {"pane_id": plain["pane_id"], "shell_pid": 1,
                                                                              "foreground_processes": [{"cmdline": n, "name": n, "pid": 1} for n in plain.get("processes", ["zsh"])]}}}
        pane = self._pane(flags["--pane"][0])
        program = self.panes[pane].get("program")
        processes = [{"cmdline": program, "name": program.split(" ")[0].rsplit("/", 1)[-1], "pid": self.shell_pid}] if program else []
        return {"result": {"type": "pane_process_info", "process_info": {
            "pane_id": pane, "foreground_processes": processes, "shell_pid": self.shell_pid}}}

    def tab_of(self, pane):
        return "%s:t%s" % (pane.split(":")[0], pane.split("p")[-1])

    def _pane_get(self, rest):
        positional, _ = parse_flags(rest, ())
        pane = positional[0]
        if pane not in self.panes:
            raise LookupError("pane %s not found" % pane)
        entry = self.panes[pane]
        session = self.v2.sessions.get(entry["session"]) or {}
        name = (((session.get("metadata") or {}).get("fleet")) or {}).get("name") or "opencode"
        title = entry.get("title") if entry.get("title") is not None else "OC | %s" % name
        body = {"pane_id": pane, "workspace_id": pane.split(":")[0], "tab_id": self.tab_of(pane), "cwd": entry.get("cwd") or "/tmp",
                "agent_status": self.status(pane), "terminal_title_stripped": title}
        return {"result": {"type": "pane_info", "pane": body}}

    def _tab_list(self, rest):
        parse_flags(rest, ())
        return {"result": {"type": "tab_list", "tabs": [{"tab_id": self.tab_of(p), "workspace_id": p.split(":")[0], "label": self.panes[p].get("tab_label", "")} for p in sorted(self.panes)]}}

    def _tab_get(self, rest):
        positional, _ = parse_flags(rest, ())
        members = [p for p in self.panes if self.tab_of(p) == positional[0]]
        if not members:
            raise LookupError("tab %s not found" % positional[0])
        return {"result": {"type": "tab_info", "tab": {"tab_id": positional[0], "pane_count": len(members) + len(self.extra_panes.get(positional[0], []))}}}

    extra_panes = {}

    def _tab_close(self, rest):
        positional, _ = parse_flags(rest, ())
        for pane in list(self.panes):
            if self.tab_of(pane) == positional[0]:
                self.closed.append(pane)
                del self.panes[pane]
                return {"result": {"type": "ok"}}
        raise LookupError("tab %s not found" % positional[0])

    def _pane_close(self, rest):
        positional, _ = parse_flags(rest, ())
        if len(positional) != 1:
            raise ValueError("pane close takes one pane id")
        plain = self._plain(positional[0])
        if plain is not None:
            self.plain_terminals.remove(plain)
            self.closed.append(positional[0])
            return {"result": {"type": "ok"}}
        if positional[0] in self.shell_panes:
            self.shell_panes.discard(positional[0])
            self.closed.append(positional[0])
            return {"result": {"type": "ok"}}
        self.closed.append(self._pane(positional[0]))
        del self.panes[positional[0]]
        return {"result": {"type": "ok"}}

    def _workspace_list(self, rest):
        parse_flags(rest, ())
        return {"result": {"type": "workspace_list", "workspaces": [dict(w) for w in self.workspaces.values()]}}

    def _workspace_create(self, rest):
        """PR 16: `herdr workspace create [--cwd P] [--label T] [--focus|--no-focus]`: one workspace with one empty shell pane."""
        positional, flags = parse_flags(rest, ("--cwd", "--label", "--env"), bare=("--focus", "--no-focus"))
        if positional:
            raise ValueError("workspace create takes no positional argument")
        if "--focus" in flags and "--no-focus" in flags:
            raise ValueError("--focus and --no-focus conflict")
        self._workspace_n = getattr(self, "_workspace_n", len(self.workspaces)) + 1
        number = self._workspace_n
        wid = "w%d" % number
        label = (flags.get("--label") or ["%d" % number])[0]
        self.workspaces[wid] = {"workspace_id": wid, "label": label, "number": number}
        self.created_workspaces = getattr(self, "created_workspaces", []) + [{"id": wid, "cwd": (flags.get("--cwd") or [None])[0],
                                                                                "label": label, "argv": list(rest), "focus": "--focus" in flags and "--no-focus" not in flags}]
        root = self.new_pane_id(wid)
        self.shell_panes.add(root)
        return {"result": {"type": "workspace_created", "workspace": dict(self.workspaces[wid]),
                           "root_pane": {"pane_id": root, "workspace_id": wid}}}

    # -- the socket
    def socket(self, path, method, params, timeout):
        self.socket_calls.append((method, copy.deepcopy(params)))
        if self.socket_down:
            raise sb.HerdrError("unavailable", "cannot reach herdr's socket %s" % path)
        if method != "layout.apply":
            return {"error": {"code": "unknown_method", "message": method}}
        return self._layout_apply(params)

    def _layout_apply(self, params):
        root = params.get("root") or {}
        if set(params) - {"workspace_id", "tab_label", "root"} or set(root) - {"type", "label", "cwd", "command", "env"}:
            return {"error": {"code": "invalid_request", "message": "unknown field"}}
        command = root.get("command")
        if root.get("type") != "pane" or not (isinstance(command, list) and command) \
                or not os.path.isabs(root.get("cwd") or "") or not isinstance(root.get("env"), dict) \
                or not all(isinstance(v, str) for v in root["env"].values()):
            return {"error": {"code": "invalid_request", "message": "bad layout"}}
        workspace = params["workspace_id"]
        pane = self.new_pane_id(workspace)
        session = command[2] if len(command) > 2 and command[1] == "-s" else None
        self.add(pane, session, program=" ".join(command))
        entry = self.panes[pane]
        entry["env"], entry["cwd"], entry["argv"] = dict(root["env"]), root["cwd"], list(command)
        if self.launch_mode == "shell":
            entry["program"] = "-zsh"
        elif self.launch_mode == "other-session":
            entry["session"] = next(iter(self.v2.sessions))
        elif self.launch_mode == "undetected":
            entry["undetected"] = True
        return {"id": "x", "result": {"type": "layout_apply", "layout": {
            "workspace_id": workspace, "tab_id": "%s:t%s" % (workspace, pane.split("p")[-1]),
            "root": {"type": "pane", "pane_id": pane, "label": root["label"], "cwd": root["cwd"],
                     "command": command}}}}


class World:
    """Fake v2, fake herdr and a simulated clock, and an engine on the real clients."""

    def __init__(self, test, agents=("platform", "coder"), config=None, state=None):
        os.environ[sb.LAB_ENV] = "1"  # lab facts are the event source in these tests
        patch = mock.patch.object(sb, "memory_sleep", lambda seconds: None)   # a new launch's memory test (#779) never waits 30 s here
        patch.start()
        test.addCleanup(patch.stop)
        self.clock = SimClock()
        self.v2 = FakeV2(self.clock)
        self.herdr = FakeHerdr(self.v2)
        self.state = sb.ensure_state_dir(state)
        self.audit_path = self.state / "audit.jsonl"
        self.v2.audit_path = self.audit_path
        self.sessions, self.panes = {}, {}
        for name in agents:
            self.add_agent(name)
        empty_log = self.state / "herdr-server.log"   # #1030: the pane-exit pass reads herdr's log; a world must never read the real one, nor note a missing one
        empty_log.write_text("")
        raw = {"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "herdr_log": str(empty_log),
               "v2_cache_seconds": getattr(test, "V2_CACHE", 0)}   # the scenarios of old read the world afresh every pass; TestV2ReadVolume and the *WithTheMemoryOn classes turn it on
        raw.update(config or {})
        self.config = sb.validate_config(raw)
        self.engine = self.new_engine()

    def add_agent(self, name, status=None, fleet=None):
        meta = {"name": name, "role": "coder"}
        meta.update(fleet or {})
        sid = self.v2.create_session({"fleet": meta})
        pane = self.herdr.new_pane_id()
        self.herdr.add(pane, sid, status)
        self.sessions[name], self.panes[name] = sid, pane
        if meta.get("role") == "orchestrator" and isinstance(meta.get("charter"), int):   # #969: the registry is the record of a charter; a first-seen agent takes its launch value (as the pass does)
            sb.load_module("registry").upsert(self.state, name, {"session": sid, "pane": pane, "role": "orchestrator", "charter": meta["charter"], "repo": meta.get("repo")})
        return sid

    def new_engine(self, actor="daemon", writable=True):
        return sb.Switchboard(self.config, sb.V2Client(FAKE_OPENCODE, {}, self.v2, 5),
                              sb.HerdrClient(FAKE_HERDR, self.herdr, socket_call=self.herdr.socket),
                              sb.AuditLog(self.audit_path) if writable else None,
                              sb.fact_sources(self.state), self.clock.now, actor,
                              intents=getattr(self, "intents", None), state_dir=self.state)

    def route(self, argv, **kwargs):
        """One runner for the CLI: each fake answers its own binary; nothing else runs."""
        if argv[0] == FAKE_OPENCODE:
            return self.v2(argv, **kwargs)
        if argv[0] == FAKE_HERDR:
            return self.herdr(argv, **kwargs)
        if argv[0] in sb.MEMORY_PROBES.values():   # a healthy, flat machine: the memory test of a new launch (#779) passes
            return subprocess.CompletedProcess(argv, 0, "System-wide memory free percentage: 80%\n" if argv[0].endswith("memory_pressure") else "Swapouts:  5.\n", "")
        raise AssertionError("a test reached for a real binary: %r" % argv[0])

    def fact(self, to, key, summary="something happened", **extra):
        line = {"key": key, "recipient": to, "kind": extra.pop("kind", "lab.fact"), "summary": summary,
                "created": self.clock.now()}
        line.update(extra)
        with open(str(self.state / sb.LAB_FACTS), "a") as handle:
            handle.write(json.dumps(line) + "\n")

    def tick(self, seconds=1):
        for _ in range(int(seconds)):
            self.clock.t += 1
            self.v2.advance()

    def you(self, name, text="what is new?"):
        self.v2.start_turn(self.sessions[name], text, fleet=False)

    def admissions(self, name):
        return self.v2.admissions(self.sessions[name])

    def audit(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def model_calls(self, name, phase=None):
        return [c for c in self.v2.model_calls
                if c["session"] == self.sessions[name] and (phase is None or c["phase"] == phase)]


class FakeProc:
    """A forwarder child: alive until it exits or is told to stop."""

    _pids = iter(made_up_pid, None)   # a number no live process has (#886)

    def __init__(self, argv):
        self.argv, self.code, self.pid = list(argv), None, next(FakeProc._pids)
        self.secret = next(a for a in argv if a.startswith("--secret="))[len("--secret="):]

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def exit(self, code=1):
        self.code = code


class FakeGitHub:
    """GitHub's objects, `gh api` over them, and `gh webhook forward` as processes.

    `runner` is what the hub runs for `gh api`; `spawn` is what it runs for the
    forwarder. A forwarder that is alive carries each event to the hub, signed
    with the secret from its own argv; with none alive an event is lost, and
    only a later `gh api` read finds it. `occupied` makes every forwarder die
    at once with "Hook already exists".
    """

    def __init__(self, clock, repo=GH_REPO):
        self.clock, self.repo, self.hub = clock, repo, None
        self.issues, self.comments, self.pulls, self.reviews, self.suites, self.runs = {}, [], {}, {}, [], []
        self.procs, self.api_calls, self.occupied = [], [], False
        self.api_down, self.spawn_error, self.exit_text = False, None, "error: Hook already exists on %s\n" % repo
        self.token, self.exists, self.webhook_ok = None, True, True  # PR 15: who may read it, whether it is there
        self.leak = False

    def stamp(self):
        return sb.iso(self.clock.now())

    # -- objects
    def issue(self, number, title="an issue", parent=None, body="", labels=(), age=30 * 86400):
        """An issue that existed `age` seconds ago and has not changed since: outside the look-back window."""
        obj = {"id": 9000 + number, "number": number, "title": title, "body": body, "state": "open",
               "updated_at": sb.iso(self.clock.now() - age), "user": {"login": "owner"}, "labels": [{"name": n} for n in labels]}
        if parent:
            obj["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (self.repo, parent)
        self.issues[number] = obj
        return obj

    def set_label(self, number, name, on, title=None):
        """PR 14: put a label on an issue or take it off, and send the `issues` event GitHub would."""
        issue = self.issues.get(number) or self.issue(number)
        names = [label["name"] for label in issue["labels"] if label["name"] != name] + ([name] if on else [])
        issue["labels"] = [{"name": n} for n in names]
        if title:
            issue["title"] = title
        issue["updated_at"] = self.stamp()
        return self.send("issues", {"action": "labeled" if on else "unlabeled", "issue": dict(issue),
                                    "label": {"name": name}})

    def pull(self, number, closes=(), title="a change", state="open", merged=False):
        body = " ".join("Closes #%d." % n for n in closes)
        self.pulls[number] = {"id": 7000 + number, "number": number, "title": title, "body": body, "state": state,
                              "merged": merged, "updated_at": self.stamp(), "user": {"login": "coder"},
                              "head": {"sha": "sha%d" % number}}
        return self.pulls[number]

    def comment(self, number, body, cid, login="octocat", forward=True):
        issue = self.issues.get(number) or self.issue(number)
        stamp = self.stamp()
        comment = {"id": cid, "body": body, "user": {"login": login}, "created_at": stamp, "updated_at": stamp,
                   "issue_url": "https://api.github.com/repos/%s/issues/%d" % (self.repo, number)}
        self.comments.append(comment)
        issue["updated_at"] = stamp
        if forward:
            return self.send("issue_comment", {"action": "created", "issue": dict(issue), "comment": comment})

    # -- the webhook side
    def forwarder(self):
        alive = [p for p in self.procs if p.poll() is None]
        return alive[-1] if alive else None

    def send(self, event, payload, delivery="d-1"):
        """GitHub sends an event. Returns the hub's (status, text), or None when nobody forwards."""
        proc = self.forwarder()
        if proc is None:
            return None
        body = json.dumps(dict(payload, repository={"full_name": self.repo})).encode()
        headers = {"X-Hub-Signature-256": sb.sign_body(proc.secret, body), "X-GitHub-Event": event,
                   "X-GitHub-Delivery": delivery}
        return self.hub.receive("/github", headers, body)

    def kill_forwarder(self):
        proc = self.forwarder()
        if proc is not None:
            proc.exit(1)

    def spawn(self, argv, log_path, env=None):
        if self.spawn_error:
            raise OSError(self.spawn_error)
        proc = FakeProc(argv)
        proc.env = env
        self.procs.append(proc)
        if self.occupied:
            Path(log_path).write_text(self.exit_text)
            proc.exit(1)
        elif not self.exists:
            Path(log_path).write_text("error: HTTP 404: Not Found (https://api.github.com/repos/%s/hooks)\n" % self.repo)
            proc.exit(1)
        elif not self.webhook_ok:
            Path(log_path).write_text("error: Must have admin rights to Repository.\n")
            proc.exit(1)
        elif self.token is not None and (env or {}).get("GH_TOKEN") != self.token:
            Path(log_path).write_text("error: HTTP 401: Bad credentials\n")
            proc.exit(1)
        return proc

    # -- gh api
    def runner(self, argv, **kwargs):
        return with_http_head(self.runner_plain, argv, kwargs, self.__dict__.setdefault("http", {}))

    def runner_plain(self, argv, kwargs):
        if argv[0] != GH_BIN or argv[1] != "api":
            raise AssertionError("a test reached for a real binary: %r" % argv[:2])
        path = argv[-1]
        self.api_calls.append(path)
        if not self.exists or (self.token is not None and (kwargs.get("env") or {}).get("GH_TOKEN") != self.token):
            if self.leak:  # a hostile gh that echoes the credential it was given into its error
                return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 401 bad credentials %s\n"
                                                   % (kwargs.get("env") or {}).get("GH_TOKEN"))
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        if self.api_down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 502\n")
        bare = path.split("?")[0][len("repos/%s/" % self.repo):]
        found = re.match(r"^issues/(\d+)/(parent|sub_issues)$", bare)
        if found:  # PR 15: what the Intent reader asks (GET only)
            number, which = int(found.group(1)), found.group(2)
            mine = self.issues.get(number, {})
            children = [i for i in self.issues.values()
                        if str(i.get("parent_issue_url", "")).endswith("/issues/%d" % number)]
            parent = self.issues.get(_url_tail(mine.get("parent_issue_url")))
            if which == "parent" and parent is None:
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
            data = parent if which == "parent" else children
        else:
            query = urllib_query(path)
            data = self.answer(bare, query)
            if isinstance(data, list) and "page" in query and "--paginate" not in argv:  # GitHub's own paging
                size = int(query.get("per_page", 30))
                data = data[(int(query["page"]) - 1) * size:int(query["page"]) * size]
            elif isinstance(data, dict) and "page" in query:
                for field in ("workflow_runs", "check_suites"):
                    if isinstance(data.get(field), list):
                        size = int(query.get("per_page", 30))
                        data = {field: data[field][(int(query["page"]) - 1) * size:int(query["page"]) * size]}
        pages = data if "--paginate" in argv and isinstance(data, list) else [data]
        if isinstance(data, list):
            pages = [[item] for item in data] if "--paginate" in argv else [data]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(p) for p in pages), "")

    def since(self, items, query, field="updated_at"):
        floor = query.get("since", "")
        return [i for i in items if i.get(field, "") >= floor]

    def answer(self, path, query):
        parts = path.split("/")
        if path == "issues/comments":
            return self.since(self.comments, query)
        if path == "issues":
            as_issue = [dict(p, pull_request={"url": "https://api.github.com/repos/x/pulls/%d" % p["number"]})
                        for p in self.pulls.values()]
            found = self.since(list(self.issues.values()) + as_issue, query)
            if "labels" in query:  # PR 14: the read of the issues that carry one label
                wanted = urllib.parse.unquote(query["labels"])
                found = [i for i in found if wanted in [label["name"] for label in i.get("labels") or []]]
            if query.get("state") == "open":
                found = [i for i in found if i.get("state", "open") == "open"]
            return found
        if parts[0] == "issues" and len(parts) == 2:
            number = int(parts[1])
            if number in self.issues:
                return self.issues[number]
            return dict(self.pulls[number], pull_request={"url": "x"}) if number in self.pulls else {"number": number}
        if path == "pulls":
            return sorted(self.pulls.values(), key=lambda p: p["updated_at"], reverse=True)
        if parts[0] == "pulls" and len(parts) == 2:
            return self.pulls.get(int(parts[1]), {})
        if parts[0] == "pulls" and parts[2:] == ["reviews"]:
            return self.reviews.get(int(parts[1]), [])
        if parts[0] == "commits" and parts[2:] == ["check-suites"]:
            return {"check_suites": [s for s in self.suites if s["sha"] == parts[1]]}
        if path == "actions/runs":
            return {"workflow_runs": self.runs}
        raise AssertionError("fake gh api: no route for %r" % path)


def _url_tail(url):
    match = re.search(r"/issues/(\d+)$", url or "")
    return int(match.group(1)) if match else None


def urllib_query(path):
    query = {}
    for pair in path.partition("?")[2].split("&"):
        if pair:
            name, _, value = pair.partition("=")
            query[name] = value
    return query


class GithubRig:
    """A hub on fake GitHub, attached to an engine on the fake fleet, on one simulated clock."""

    def __init__(self, test, agents=None, config=None, repos=(GH_REPO,)):
        raw = {"github_repos": list(repos), "gh": GH_BIN}
        raw.update(config or {})
        self.world = World(test, agents=(), config=raw)
        for name, fleet in (agents if agents is not None else
                            {"platform": {"role": "orchestrator", "charter": 1}}).items():
            self.world.add_agent(name, fleet=fleet)
        self.gh = FakeGitHub(self.world.clock)
        self.hub = sb.GitHubHub(self.world.config, self.world.state, sb.AuditLog(self.world.audit_path),
                                self.gh.runner, self.gh.spawn, self.world.clock.now)
        self.hub.autostart, self.hub.port, self.gh.hub = False, 4711, self.hub
        self.world.engine = self.hub.attach(self.world.new_engine())

    def pump(self, seconds, every=5):
        """Simulated time: the supervisor ticks, then a pass runs, every `every` seconds."""
        for _ in range(0, int(seconds), every):
            self.world.tick(every)
            self.hub.tick()
            self.world.engine.run_pass()

    def watch(self, repo=GH_REPO):
        return self.hub.watches[repo]

    def keys(self):
        return sorted(item.key for item in self.hub.queue.values())

    def delivered(self, name="platform"):
        return sorted(k for a in self.world.admissions(name) for k in a["keys"])

    def state_text(self):
        """Every byte under the state directory, the audit log included."""
        return "\n".join(p.read_text(errors="replace") for p in sorted(self.world.state.rglob("*")) if p.is_file())


def build_carry_repo(directory):
    """A real git repository (neutral names): `main` with a few files and a `feature` branch whose own change is a.txt edited and c.txt added. Returns the GitHubOverGit over it."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    api = GitHubOverGit(directory)
    g = api.git

    def write(path, text):
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "dev@example.test")
    g("config", "user.name", "Dev")
    g("config", "commit.gpgsign", "false")
    write("README.md", "widget api\n")
    write("a.txt", "one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
    write("src/x.py", "def x():\n    return 1\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "feature")
    write("a.txt", "one\nTWO\nthree\nfour\nfive\nsix\nseven\n")
    write("c.txt", "new file\n")
    g("add", "-A")
    g("commit", "-q", "-m", "the change")
    return api


def _artifact_zip(names, complete=True, run=1, head="a" * 40):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("failing-tests.json", json.dumps({"v": 1, "run": run, "head": head, "complete": complete, "failed": names}))
    return stream.getvalue()

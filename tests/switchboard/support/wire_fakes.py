"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): wire_fakes."""
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
from support.core import FakeRunner
from support.loader import sb


SES = "ses_0123456789abcdef"


MSG = "msg_0123456789abcdef0123456789"


FORBIDDEN_HERDR = ("prompt", "send-keys", "send-text", "run", "focus")


def herdr_typing_violations(source):
    """Every way `source` could make herdr type into a pane or move focus.

    Used on bin/fleet-switchboard, and on a planted violation to show it bites.
    """
    found = []
    tree = ast.parse(source)
    tokens = [node.value for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str) and not re.search(r"\s", node.value)]
    for token in tokens:
        if token in ("send-keys", "send-text"):
            found.append(token)
        if "focus" in token.lower() and not token.startswith("--no-") and token != "--focus":
            found.append(token)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HerdrClient":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and inner.value in FORBIDDEN_HERDR:
                    found.append("HerdrClient uses %r" % inner.value)
    # PR 16: the one `--focus` is `workspace create`, which only a person's own `bootstrap` runs
    allowed = [inner for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "workspace_create"
               for inner in ast.walk(node)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "--focus" and not any(node is inner for inner in allowed):
            found.append("--focus outside HerdrClient.workspace_create")
    for token in tokens:  # the socket: any herdr method but the one allowed is a way in
        if re.match(r"^(agent|pane|tab|workspace|terminal|layout|window|input)\.", token) \
                and token not in SOCKET_ALLOWED:
            found.append("socket method %s" % token)
    for pattern in (r"""["']agent["']\s*,\s*["'](prompt|send-keys)["']""",
                    r"""["']pane["']\s*,\s*["'](send-text|send-keys|run)["']"""):
        found.extend(m.group(0) for m in re.finditer(pattern, source))
    return found


DOC_FAULTS = ("no-engagement-gate", "no-recheck", "no-dedupe", "steer-not-queue", "no-batch",
              "no-intent-header", "no-classify", "crash-after-send", "no-policy-judge",
              "judge-loosens", "no-maxims", "no-worker-source", "no-note-conversion",
              "no-import-metadata", "idle-wakes", "no-rider", "no-nudge-once", "no-fail-open",
              "ignore-reports", "no-description",
              "no-decisions-file", "stale-as-fresh", "decision-resolves-never",  # PR 14
              "static-repos", "bare-issue-number", "one-account", "repo-create-allowed",  # PR 15
              "key-missing-silent", "no-ensure-on-use", "shared-profile-default",  # PR 16
              "key-env-only-unseen",  # PR 16, the first real install
              "config-read-once", "catch-up-blocks-pass",  # PR 16, reload and the slow catch-up
              "catch-up-on-fresh-start", "full-window-every-time",  # PR 16, forward only
              "orders-start-only", "orders-wake", "anyone-can-order", "orders-never-judged",  # PR 17
              "tier-flat", "cos-orders-unfed", "anyone-edits-cos-orders", "answer-unjudged")  # PR 18


VALID = {
    "id": "N1",
    "title": "A worker finishes while you are talking to its orchestrator",
    "problem": 3,
    "since_pr": 4,
    "tiers": ["offline", "lab-scripted"],
    "tags": ["example"],
    "cast": {
        "cos": {"role": "chief-of-staff"},
        "platform": {"role": "orchestrator", "reports_to": "cos", "charter": 1},
        "coder": {"role": "coder", "reports_to": "platform", "issue": 2},
        "helper": {"role": "cos-subagent", "reports_to": "cos", "issue": 3},
    },
    "issues": {
        "1": {"intent": "Run the platform", "done_when": None},
        "2": {"intent": "Add a health check", "done_when": "GET /health returns 200", "parent": 1},
        "3": {"intent": None},
    },
    "steps": [
        {"at": "0s", "actor": "coder", "do": "turn", "lasts": "30s"},
        {"at": "10s", "actor": "you", "do": "message", "to": "platform", "text": "status?"},
        {"at": "10s", "actor": "world", "do": "comment", "issue": 2, "body": "looks good"},
        {"at": "2m", "actor": "you", "do": "draft", "to": "platform", "text": "and next"},
        {"at": "2h", "actor": "world", "do": "wait"},
    ],
    "expect": [
        {"name": "one-note", "that": "delivered", "to": "platform", "key": "worker.idle:coder:*",
         "mode": "note", "count": 1},
        {"that": "no_machine_turn", "agent": "platform"},
        {"that": "message_has_intent", "to": "platform", "issue": 2},
    ],
    "control": {"fault": "no-engagement-gate", "fails": ["no_machine_turn", "one-note"]},
}


def mutate(change):
    obj = copy.deepcopy(VALID)
    change(obj)
    return obj


def _set(path, value):
    def change(obj):
        target = obj
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return change


def _drop(path):
    def change(obj):
        target = obj
        for part in path[:-1]:
            target = target[part]
        del target[path[-1]]
    return change


REJECTED = [
    # (what, change, a substring of the expected error)
    ("unknown top-level key", _set(["notes"], "x"), "unknown top-level key 'notes'"),
    ("id lower case", _set(["id"], "n1"), "id:"),
    ("id three letters", _set(["id"], "ABC1"), "id:"),
    ("id no number", _set(["id"], "N"), "id:"),
    ("id not a string", _set(["id"], 1), "id:"),
    ("title empty", _set(["title"], " "), "title:"),
    ("title not a string", _set(["title"], 3), "title:"),
    ("problem 5", _set(["problem"], 5), "problem:"),
    ("problem 0", _set(["problem"], 0), "problem:"),
    ("problem string", _set(["problem"], "3"), "problem:"),
    ("problem bool", _set(["problem"], True), "problem:"),
    ("since_pr 0", _set(["since_pr"], 0), "since_pr:"),
    ("since_pr string", _set(["since_pr"], "4"), "since_pr:"),
    ("since_pr float", _set(["since_pr"], 1.5), "since_pr:"),
    ("tiers empty", _set(["tiers"], []), "tiers:"),
    ("tiers unknown", _set(["tiers"], ["offline", "cloud"]), "tiers:"),
    ("tiers string", _set(["tiers"], "offline"), "tiers:"),
    ("tiers repeated", _set(["tiers"], ["offline", "offline"]), "tiers:"),
    ("tags string", _set(["tags"], "spike"), "tags:"),
    ("tags non-string", _set(["tags"], [1]), "tags:"),
    ("cast not an object", _set(["cast"], ["coder"]), "cast:"),
    ("cast member not an object", _set(["cast", "coder"], "coder"), "cast.coder:"),
    ("cast role unknown", _set(["cast", "coder", "role"], "reviewer"), "cast.coder.role"),
    ("cast role missing", _drop(["cast", "cos", "role"]), "cast.cos.role"),
    ("cast reports_to unknown", _set(["cast", "coder", "reports_to"], "nobody"), "cast.coder.reports_to"),
    ("cast reports_to self", _set(["cast", "coder", "reports_to"], "coder"), "cast.coder.reports_to"),
    ("cast issue string", _set(["cast", "coder", "issue"], "2"), "cast.coder.issue"),
    ("cast charter zero", _set(["cast", "platform", "charter"], 0), "cast.platform.charter"),
    ("cast unknown key", _set(["cast", "coder", "model"], "x"), "unknown key 'model'"),
    ("cast name reserved", _set(["cast", "you"], {"role": "coder"}), "reserved"),
    ("issues not an object", _set(["issues"], []), "issues:"),
    ("issue key not a number", _set(["issues", "#2"], {"intent": "x"}), "issues.#2"),
    ("issue not an object", _set(["issues", "2"], "x"), "issues.2:"),
    ("issue intent missing", _drop(["issues", "2", "intent"]), "issues.2.intent"),
    ("issue intent not a string", _set(["issues", "2", "intent"], 5), "issues.2.intent"),
    ("issue done_when not a string", _set(["issues", "2", "done_when"], ["x"]), "issues.2.done_when"),
    ("issue parent string", _set(["issues", "2", "parent"], "1"), "issues.2.parent"),
    ("issue unknown key", _set(["issues", "2", "owner"], "x"), "unknown key 'owner'"),
    ("steps not a list", _set(["steps"], {}), "steps:"),
    ("step not an object", _set(["steps", 0], "turn"), "steps[0]:"),
    ("step at without unit", _set(["steps", 1, "at"], "10"), "steps[1].at"),
    ("step at in days", _set(["steps", 1, "at"], "1d"), "steps[1].at"),
    ("step at a number", _set(["steps", 1, "at"], 10), "steps[1].at"),
    ("step at missing", _drop(["steps", 1, "at"]), "steps[1].at"),
    ("step times decrease", _set(["steps", 4, "at"], "90s"), "times must not decrease"),
    ("step actor unknown", _set(["steps", 0, "actor"], "nobody"), "steps[0].actor"),
    ("step actor missing", _drop(["steps", 0, "actor"]), "steps[0].actor"),
    ("step do unknown", _set(["steps", 0, "do"], "type"), "steps[0].do"),
    ("step do missing", _drop(["steps", 0, "do"]), "steps[0].do"),
    ("expect empty", _set(["expect"], []), "expect:"),
    ("expect not a list", _set(["expect"], {"that": "delivered"}), "expect:"),
    ("expectation not an object", _set(["expect", 1], "no_machine_turn"), "expect[1]:"),
    ("expectation that unknown", _set(["expect", 1, "that"], "happy"), "expect[1].that"),
    ("expectation that missing", _drop(["expect", 2, "that"]), "expect[2].that"),
    ("expectation name repeated", _set(["expect", 2, "name"], "one-note"), "used twice"),
    ("expectation name empty", _set(["expect", 2, "name"], ""), "expect[2].name"),
    ("control not an object", _set(["control"], "no-batch"), "control:"),
    ("control without a kind", _set(["control"], {"fails": ["no_machine_turn"]}), "exactly one"),
    ("control with two kinds", _set(["control", "baseline"], "fleet-heartbeat"), "exactly one"),
    ("control fault unknown", _set(["control", "fault"], "no-safety"), "unknown fault"),
    ("control baseline empty", _set(["control"], {"baseline": "", "fails": ["one-note"]}), "control.baseline"),
    ("control variant not an object", _set(["control"], {"variant": "x", "fails": ["one-note"]}), "control.variant"),
    ("control fails missing", _drop(["control", "fails"]), "control.fails"),
    ("control fails empty", _set(["control", "fails"], []), "control.fails"),
    ("control fails not a list", _set(["control", "fails"], "one-note"), "control.fails"),
    ("control fails unmatched", _set(["control", "fails"], ["one-note", "draft_intact"]), "'draft_intact' matches no"),
    ("control unknown key", _set(["control", "why"], "x"), "unknown key 'why'"),
    # Field by field, for the kinds the offline runner implements.
    ("fact without key", _set(["steps", 2], {"at": "10s", "actor": "world", "do": "fact", "to": "coder",
                                              "summary": "s"}), "steps[2].key: required"),
    ("fact to a stranger", _set(["steps", 2], {"at": "10s", "actor": "world", "do": "fact", "to": "nobody",
                                                "key": "k", "summary": "s"}), "steps[2].to: must be a cast name"),
    ("fact field typo", _set(["steps", 2], {"at": "10s", "actor": "world", "do": "fact", "too": "coder",
                                             "to": "coder", "key": "k", "summary": "s"}), "unknown field 'too'"),
    ("fact batch not a bool", _set(["steps", 2], {"at": "10s", "actor": "world", "do": "fact", "to": "coder",
                                                   "key": "k", "summary": "s", "batch": "no"}), "steps[2].batch"),
    ("turn of zero steps", _set(["steps", 0, "steps"], 0), "steps[0].steps"),
    ("turn lasts forever", _set(["steps", 0, "lasts"], "ever"), "steps[0].lasts"),
    ("pass with an unknown fault", _set(["steps", 4], {"at": "2h", "actor": "world", "do": "pass",
                                                        "faults": ["no-safety"]}), "steps[4].faults"),
    ("wait with a field", _set(["steps", 4, "for"], "ever"), "unknown field 'for'"),
    ("delivered in an unknown mode", _set(["expect", 0, "mode"], "push"), "expect[0].mode"),
    ("delivered without key", _drop(["expect", 0, "key"]), "expect[0].key: required"),
    ("model_saw at an unknown moment", _set(["expect", 1], {"that": "model_saw", "agent": "coder", "key": "k",
                                                             "when": "later"}), "expect[1].when"),
    ("message_count without count", _set(["expect", 1], {"that": "message_count", "to": "coder"}),
     "expect[1].count: required"),
    # Who may act, and rules across fields.
    ("an agent sends your message", _set(["steps", 1, "actor"], "coder"), "'coder' cannot message"),
    ("you take a turn", _set(["steps", 0, "actor"], "you"), "'you' cannot turn"),
    ("the world drafts", _set(["steps", 3, "actor"], "world"), "'world' cannot draft"),
    ("your permission answer without to", _set(["steps", 2], {"at": "10s", "actor": "you", "do": "permission"}),
     "steps[2].to: required for your answer"),
    ("an agent's ask with a decision", _set(["steps", 2], {"at": "10s", "actor": "coder", "do": "permission",
                                                            "decision": "once"}), "takes no `to` or `decision`"),
    ("a draft of two lines", _set(["steps", 3, "text"], "a\nb"), "steps[3].text"),
    ("within a step that does not exist", _set(["expect", 1], {"that": "within", "agent": "coder", "step": 5,
                                                                "seconds": 2}), "expect[1].step"),
    ("a message read back by neither key nor text", _set(["expect", 1], {
        "that": "metadata_roundtrip", "agent": "coder", "of": "message"}), "exactly one of `key` or `text`"),
    ("a status nobody reports", _set(["expect", 1], {"that": "status_is", "agent": "coder", "status": "busy",
                                                      "at": "5s"}), "expect[1].status"),
    ("no_machine_turn with a field typo", _set(["expect", 1, "agnet"], "platform"), "unknown field 'agnet'"),
    ("a variant with an unknown key", _set(["control"], {"variant": {"engaged_minutes": 0}, "fails": ["one-note"]}),
     "control.variant: unknown key"),
    ("a variant for a step that does not exist", _set(["control"], {"variant": {"steps": {"7": {"text": "x"}}},
                                                                     "fails": ["one-note"]}), "not the index"),
    ("a variant that breaks the scenario", _set(["control"], {"variant": {"expect": {"one-note": {"mode": "push"}}},
                                                               "fails": ["one-note"]}),
     "control.variant makes expect[0].mode"),
]


# One valid instance of every kind, each field set (the vocabulary is checked in every tier).
STEP_EXAMPLES = {
    "turn": {"lasts": "30s", "steps": 2},
    "message": {"to": "platform", "text": "status?", "lasts": "5s"},
    "draft": {"to": "platform", "text": "half typed"},
    "away": {},
    "delete-state": {},
    "permission": {"to": "coder", "decision": "always"},
    "comment": {"issue": 2, "body": "looks good", "repo": "acme/web"},  # PR 15
    "leave": {"agent": "coder"},  # PR 15
    "kill-daemon": {}, "restart-daemon": {}, "wait": {},
    "close-pane": {"agent": "coder"},
    "edit-intent": {"issue": 2, "intent": "Add a health check and a probe", "done_when": None},
    "synthetic": {"to": "coder", "key": "k", "text": "t", "id": "same", "resume": False, "delivery": "steer"},
    "note": {"to": "coder", "key": "k", "text": "t"},
    "wake": {"to": "coder", "key": "k", "text": "t", "delivery": "queue"},
    "fact": {"to": "platform", "key": "lab.fact:1", "summary": "s", "kind": "review", "issue": 2,
             "from": "coder", "batch": False},
    "pass": {"faults": ["no-dedupe", "crash-after-send"]},
    "launch": {"agent": "coder", "brief": "Add a health check", "new_workspace": "Acme Web"},
    "handoff": {"issue": 2, "brief": "Done so far: a\nNext steps: b"},  # PR 8
    "report": {"state": "done", "text": "PR is up", "issue": 2},  # PR 13
    "awaiting": {"issue": 5, "on": True, "title": "Pick the database"},  # PR 14
    "send": {"to": "platform", "text": "use postgres", "issue": 2},
    "delete-decisions": {},
    "use": {"command": "decisions"},  # PR 16
    "bootstrap": {},
    "close-agent": {"agent": "coder"},
    "edit-config": {"add_repo": "acme/web", "broken": False},  # PR 16
    "orders": {"charter": 1, "action": "add", "text": "approve deploys here", "number": 1},  # PR 17
    "edit-orders": {"charter": 1, "text": "approve deploys here"},
    "compact": {"agent": "platform", "status": "completed"},
}


EXPECT_EXAMPLES = {
    "delivered": {"to": "platform", "key": "lab.fact:*", "mode": "note", "count": 0, "keys": 1},
    "message_count": {"to": "platform", "count": 1},
    "model_saw": {"agent": "platform", "key": "lab.fact:*", "when": "between_steps", "count": 0},
    "no_machine_turn": {"agent": "platform"},
    "within": {"agent": "platform", "step": 1, "seconds": 2.5, "key": "k"},
    "draft_intact": {"agent": "platform", "text": "and next"},
    "message_has_intent": {"to": "platform", "issue": 2},
    "classified": {"agent": "cos", "step": 1, "as": "current-work"},
    "handed_off": {"issue": 3},
    "toast": {"agent": "coder", "text": "held", "count": 1},
    "repo_watch": {"repo": "acme/web", "how": "polling"},  # PR 15
    "repo_membership": {"repo": "acme/web", "joined": True, "left": False},
    "status_says": {"text": "polling every"},
    "config_says": {"at": "90s", "text": "INVALID"},  # PR 16
    "launch_refused": {"agent": "coder", "text": "Acme Web"},
    "tool_outcome": {"agent": "coder", "tool": "shell", "outcome": "completed"},
    "judged": {"agent": "cos", "question": "Does it say plainly that the work failed?", "threshold": 0.8},
    "status_is": {"agent": "coder", "status": ["working", "blocked"], "at": "5s"},
    "metadata_roundtrip": {"agent": "coder", "of": "message", "text": "status?", "fleet": None},
    "pane_runs_v2": {"agent": "coder"},
    "no_typing": {"agent": "coder"},
    "message_shows": {"to": "platform", "text": "no intent recorded"},  # PR 6
    "intent_gap": {"issue": 2},
    "decisions": {"at": "30s", "count": 1, "text": "Which database?"},  # PR 14
    "decisions_say": {"at": "30s", "text": "not updating"},
    "status_first_line": {"text": "No decision-model key"},  # PR 16
    "bootstrap_says": {"step": 1, "text": "already runs"},
    "fleet_sessions": {"agent": "coder", "count": 1},
    "orders_on_github": {"charter": 1, "count": 1, "text": "approve deploys"},  # PR 17
    "orders_refused": {"agent": "coder", "count": 1},
}


def routed(v2=(0, '{"data": {"version": "2.0.22"}}', ""), herdr=(0, '{"result": {"agents": [{}, {}]}}', "")):
    """A runner that answers the configured v2 binary and herdr differently."""
    def route(argv):
        return v2 if argv[1:2] == ["api"] else herdr
    return FakeRunner(route=route)


EPOCH = 1800000000.0  # where every simulated clock starts


FAKE_OPENCODE = "/fake/bin/opencode"


FAKE_HERDR = "/fake/bin/herdr"


class SimClock:
    """Simulated time. Nothing in these tests sleeps."""

    def __init__(self, start=EPOCH):
        self.t = float(start)

    def now(self):
        return self.t

    def ms(self, at=None):
        return int(round((self.t if at is None else at) * 1000))


class FakeHTTP(Exception):
    def __init__(self, status, name, message):
        super().__init__(message)
        self.status, self.name, self.message = status, name, message


class Enveloped(list):
    """A list a fake operation returns with the rest of v2's answer beside `data`."""

    envelope = None


def _record_keys(record):
    """Keys of a transcript message (`metadata`) or an inbox item (`payload.metadata`)."""
    meta = record.get("metadata") or (record.get("payload") or {}).get("metadata") or {}
    keys = (meta.get("fleet") or {}).get("keys")
    return list(keys) if isinstance(keys, list) else []


class FakeV2:
    """`opencode api` with the spike-verified semantics (docs/switchboard.md, S2-S5).

    It is a runner, so the engine reaches it through V2Client._call exactly as
    it reaches the real binary. Oracles read its state directly: transcripts,
    the inbox, admissions and every model call.

    - A note (`resume` false, `steer`) on an idle session starts no turn and
      waits in the inbox; it reaches the model in the same call as the next
      turn's trigger, placed just before it.
    - A `steer` item on a busy session is injected at the next step of the turn,
      or, after the last step, delivered at the end with one more model call.
    - A `queue` item waits; it runs after the turn's final reply, never between
      steps, in the same busy span. `time.idle` is written once, at the end.
    - `resume` true on an idle session starts a turn at once.
    - inbox.update to `steer` on a waiting item starts a turn when idle; steer
      to steer is refused with 409.
    - A repeated message id is admitted once; the repeat returns the original.
      Ids are global, so a repeat aimed at another session is not admitted
      there either (the assumption the derived id is built around).

    Not modelled, and refused loudly: a fleet prompt to a busy session. (Yours is
    modelled in the transcript only: see human_prompt_while_busy.) A turn
    started by a wake lasts `wake_lasts` seconds (0 by default).
    """

    def __init__(self, clock):
        self.clock = clock
        self.sessions = {}
        self.admitted = {}     # message id -> admission, across sessions
        self.model_calls = []
        self.writes = []       # every write, with the audit line count at that moment
        self.calls = []
        self.down = False
        self.audit_path = None
        self.wake_lasts = 0
        self.before_synthetic = None   # a hook run as a synthetic arrives (the end-of-turn race)
        self.interrupts = []           # every session.interrupt: {"session", "resume", "was_busy", "at"}
        self.measured = None           # #1345: {(state, form): turn started?} from the measured table (tests/switchboard/fixtures/delivery_measure.json); None keeps the older rule (resume true starts a turn on an idle session)
        self._ids = 0

    def _id(self, prefix):
        self._ids += 1
        return "%s_fake%06d" % (prefix, self._ids)

    # -- the runner interface
    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "error: cannot reach the OpenCode service\n")
        if argv[1:2] != ["api"] or len(argv) < 3:
            return subprocess.CompletedProcess(argv, 2, "", "fake opencode: only `api` is modelled\n")
        if argv[2] == "GET":   # `opencode api GET /api/agent` (the registered agents): not modelled here, so the service reads as unreachable
            return subprocess.CompletedProcess(argv, 1, "", "fake opencode: GET is not modelled\n")
        op, params, body, rest = argv[2], {}, None, argv[3:]
        while rest:
            flag, value, rest = rest[0], rest[1], rest[2:]
            if flag == "--param":
                name, _, val = value.partition("=")
                params[name] = val
            elif flag == "-d":
                body = json.loads(value)
            else:
                raise AssertionError("fake opencode: unexpected argument %r" % flag)
        handler = getattr(self, "op_" + op.replace(".", "_"), None)
        if handler is None:
            return subprocess.CompletedProcess(argv, 1, '{"name":"NotFound"}', "HTTP 404 Not Found\n")
        try:
            data = handler(params, body)
        except FakeHTTP as err:
            return subprocess.CompletedProcess(
                argv, 1, json.dumps({"name": err.name, "message": err.message}),
                "HTTP %d %s\n" % (err.status, err.name))
        payload = {"data": data}
        payload.update(getattr(data, "envelope", None) or {})  # PR 8: a listing's cursor
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    # -- state helpers
    def create_session(self, metadata, title="fake", agent=None, directory=None):
        sid = self._id("ses")
        self.sessions[sid] = {"id": sid, "title": title, "metadata": copy.deepcopy(metadata),
                              "agent": agent, "directory": directory,
                              "created": self.clock.now(), "idle": None, "idle_writes": [], "outcome": None,
                              "messages": [], "inbox": [], "turn": None, "turns": 0}
        return sid

    def _session(self, params):
        session = self.sessions.get(params.get("sessionID"))
        if session is None:
            raise FakeHTTP(404, "NotFound", "no such session")
        return session

    def busy(self, sid):
        return self.sessions[sid]["turn"] is not None

    def _write(self, op, sid, ident):
        lines = 0
        if self.audit_path is not None and Path(self.audit_path).exists():
            lines = len(Path(self.audit_path).read_text().splitlines())
        self.writes.append({"op": op, "session": sid, "id": ident, "at": self.clock.now(), "audit_lines": lines})

    def _message(self, s, type_, text, metadata, at, mid=None):
        message = {"id": mid or self._id("msg"), "sessionID": s["id"], "type": type_, "text": text,
                   "time": {"created": self.clock.ms(at)}}
        if metadata is not None:
            message["metadata"] = copy.deepcopy(metadata)
        s["messages"].append(message)
        return message

    def _deliver(self, s, items, at):
        """Move inbox items into the transcript; they reach the model now."""
        delivered = []
        for item in items:
            s["inbox"].remove(item)
            delivered.append(self._message(s, item.get("type", "synthetic"), item["text"], item.get("metadata"), at, item["id"]))
            if item["id"] in self.admitted:  # your own prompt is not a switchboard admission
                self.admitted[item["id"]]["delivered_at"] = at
        return delivered

    def _model_call(self, s, phase, inputs, at):
        self.model_calls.append({"session": s["id"], "at": at, "phase": phase, "turn": s["turns"],
                                 "inputs": [{"id": m["id"], "type": m["type"], "keys": _record_keys(m)}
                                            for m in inputs]})

    def _start(self, s, at, lasts=0, steps=1, prompt=None, items=()):
        """A turn starts. Waiting notes ride in front of what started it (S3)."""
        s["turns"] += 1
        items = list(items)
        notes = [i for i in s["inbox"] if i["delivery"] == "steer" and i not in items]
        inputs = self._deliver(s, notes, at) + self._deliver(s, items, at)
        if prompt is not None:
            inputs.append(self._message(s, "user", prompt[0], prompt[1], at))
        self._model_call(s, "turn_start", inputs, at)
        s["turn"] = {"steps": [at + lasts * i / steps for i in range(1, steps)], "end": at + lasts}
        if lasts <= 0 and steps <= 1:
            self._finish(s, at)

    def _finish(self, s, at):
        """The final reply; then whatever waits runs in the same busy span (S3, S4)."""
        if s["idle"] is not None and at <= s["idle"]:
            at = s["idle"] + 0.001  # v2's time.idle only moves forward, even for a turn that takes no time
        self._message(s, "assistant", "reply", None, at)
        while s["inbox"]:
            steer = [i for i in s["inbox"] if i["delivery"] == "steer"]
            self._model_call(s, "after_reply", self._deliver(s, steer or s["inbox"][:1], at), at)
            self._message(s, "assistant", "reply", None, at)
        s["turn"] = None
        s["idle"] = at
        s["idle_writes"].append(at)
        s["outcome"] = "success"

    def advance(self):
        """Run every turn event due by now: steps, then the final reply."""
        now = self.clock.now()
        for s in self.sessions.values():
            while s["turn"] is not None:
                turn = s["turn"]
                if turn["steps"] and turn["steps"][0] <= now:
                    at = turn["steps"].pop(0)
                    steer = [i for i in s["inbox"] if i["delivery"] == "steer"]
                    self._model_call(s, "between_steps", self._deliver(s, steer, at), at)
                elif turn["end"] <= now:
                    self._finish(s, turn["end"])
                else:
                    break

    def start_turn(self, sid, text, fleet, lasts=0, steps=1, metadata=None):
        """A prompt: yours (no fleet metadata) or the fleet's, starting a turn of `steps` model calls."""
        s = self.sessions[sid]
        if s["turn"] is not None:
            if metadata is None and not fleet:  # PR 8: you type while it works
                return self.human_prompt_while_busy(sid, text)
            raise AssertionError("fake opencode: a prompt to a busy session is not modelled")
        if metadata is None and fleet:
            metadata = {"fleet": {"from": "fleet-task"}}
        self._start(s, self.clock.now(), lasts, steps, prompt=(text, metadata))

    def admissions(self, sid):
        return [a for a in self.admitted.values() if a["session"] == sid]

    def compact(self, sid, status="completed"):
        """PR 17: a compaction in the transcript, in the shape the Lab measured on v2 2.0.22 (docs/switchboard.md,
        "Standing orders", Lab proof): a message of type `compaction`, id `msg_...`, `time.created`, `status`
        running, completed or failed, a `reason`, `summary` and `recent` while running and when completed, an
        `error` when failed. It keeps its id as it goes from running to completed. Returns its id."""
        s = self.sessions[sid]
        message = self._message(s, "compaction", None, None, self.clock.now())
        del message["text"]
        message.update(status=status, reason="manual")
        if status == "failed":
            message["error"] = {"type": "compaction.failed", "message": "Compaction summary did not match the required template"}
        else:
            message.update(summary="## Objective\n- probe\n", recent="[User]: hi")
        return message["id"]

    def finish_compaction(self, sid, mid, status="completed"):
        for message in self.sessions[sid]["messages"]:
            if message["id"] == mid:
                message["status"] = status
                message.pop("error", None)

    def human_prompt_while_busy(self, sid, text):
        """PR 8: you send a message while the session works. It is in the transcript at once,
        as the TUI shows it, and the running turn goes on undisturbed. When the model first
        sees it, and how the TUI admits it, is spike SF1's question: nothing here says."""
        s = self.sessions[sid]
        at, mid = self.clock.now(), self._id("msg")
        item = {"id": mid, "sessionID": sid, "type": "user", "text": text, "metadata": None, "resume": False,
                "delivery": "steer", "time": {"created": self.clock.ms(at)}}
        s["inbox"].append(item)  # SF1: in the inbox at once, consumed at the next step boundary

    # -- operations
    def op_server_info(self, params, body):
        return {"version": "2.0.22-fake"}

    def op_session_create(self, params, body):
        return self._info(self.sessions[self.create_session(
            body.get("metadata") or {}, body.get("title"), body.get("agent"),
            (body.get("location") or {}).get("directory"))])

    def _info(self, s):
        stamps = [self.clock.ms(s["created"])] + [m["time"]["created"] for m in s["messages"] if isinstance(m.get("time"), dict) and "created" in m["time"]]
        if s["idle"] is not None:
            stamps.append(self.clock.ms(s["idle"]))
        return {"id": s["id"], "title": s["title"], "metadata": copy.deepcopy(s["metadata"]),
                "time": {"created": self.clock.ms(s["created"]), "updated": max(stamps),
                         "idle": None if s["idle"] is None else self.clock.ms(s["idle"])},
                "outcome": s["outcome"], "location": {"directory": s.get("directory")}}

    def op_session_get(self, params, body):
        return self._info(self._session(params))

    def op_session_active(self, params, body):
        return {sid: {"type": "busy"} for sid, s in self.sessions.items() if s["turn"] is not None}

    def op_session_message_list(self, params, body):
        s = self._session(params)
        messages = list(s["messages"]) if params.get("order") == "asc" else list(reversed(s["messages"]))
        if "type" in params:
            messages = [m for m in messages if m["type"] == params["type"]]
        if "limit" in params:
            return self.page_of(messages, params)
        return copy.deepcopy(messages)

    CURSOR_PREFIX = "cur_at_"

    def page_of(self, messages, params):
        """PR 8: a page and the cursor v2 returns beside it. A cursor token names the
        offset of the next page, in the listing's own order (newest first by default);
        `order` and `cursor` together are refused, as v2 says. `previous` points back."""
        start = 0
        if "cursor" in params:
            if "order" in params:
                raise FakeHTTP(400, "InvalidRequestError", "do not combine cursor with order")
            if not params["cursor"].startswith(self.CURSOR_PREFIX):
                raise FakeHTTP(400, "InvalidCursorError", "bad cursor")
            start = int(params["cursor"][len(self.CURSOR_PREFIX):])
        size = int(params["limit"])
        page = Enveloped(copy.deepcopy(messages[start:start + size]))
        more = start + size < len(messages)
        page.envelope = {"cursor": {"next": self.CURSOR_PREFIX + str(start + size) if more else None,
                                    "previous": self.CURSOR_PREFIX + str(max(0, start - size)) if start else None}}
        return page

    def op_session_interrupt(self, params, body):
        """VERIFIED in a throwaway v2 2.0.24 (steer probe): the running turn is cut at once (its in-flight tool
        call aborted), the session is idle with outcome interrupted, and a waiting steering input stays in the
        inbox unless `resume` is true, which runs it as a new turn. Idle: a no-op, {"interrupted": false}."""
        s = self._session(params)
        resume = params.get("resume") == "true"
        busy = s["turn"] is not None
        at = self.clock.now()
        self.interrupts.append({"session": s["id"], "resume": resume, "was_busy": busy, "at": at})
        self._write("session.interrupt", s["id"], None)
        if busy:
            s["turn"] = None
            s["idle"] = max(at, (s["idle"] or 0) + 0.001)
            s["idle_writes"].append(s["idle"])
            s["outcome"] = "interrupted"
            if resume and s["inbox"]:
                self._start(s, at, self.wake_lasts, 1, items=[i for i in s["inbox"] if i["delivery"] == "steer"])
        return {"interrupted": busy}

    @staticmethod
    def form_of(delivery, resume):
        """The name of a delivery form in the measured table: `<delivery>_resume` with resume true, `<delivery>` with resume false, `prompt` for a plain prompt (delivery None)."""
        if delivery is None:
            return "prompt"
        return delivery + "_resume" if resume else delivery

    def state_of(self, s):
        """The measured case a session is in: working, idle after a provider error (outcome `failed`), or idle after a normal turn."""
        if s["turn"] is not None:
            return "working"
        return "idle_after_provider_error" if s["outcome"] == "failed" else "idle_after_normal_turn"

    def starts_turn(self, s, delivery, resume):
        """Does this form start a turn on this idle session? With the measured table: what was measured; without it: resume true does."""
        if self.measured is None:
            return bool(resume)
        return bool(self.measured[(self.state_of(s), self.form_of(delivery, resume))])

    def op_session_synthetic(self, params, body):
        if self.before_synthetic is not None:
            hook, self.before_synthetic = self.before_synthetic, None
            hook()
        s = self._session(params)
        mid = body["id"]
        if mid in self.admitted:  # admitted once; the repeat returns the original (S2)
            return copy.deepcopy(self.admitted[mid]["record"])
        at = self.clock.now()
        record = {"id": mid, "sessionID": s["id"], "type": "synthetic"}
        item = {"id": mid, "sessionID": s["id"], "text": body["text"], "metadata": copy.deepcopy(body.get("metadata")),
                "resume": body["resume"], "delivery": body["delivery"], "time": {"created": self.clock.ms(at)}}
        fleet = (body.get("metadata") or {}).get("fleet") or {}
        self.admitted[mid] = {"id": mid, "session": s["id"], "resume": body["resume"], "delivery": body["delivery"],
                              "at": at, "delivered_at": None, "keys": list(fleet.get("keys") or []),
                              "from": fleet.get("from"), "text": body["text"], "record": record,
                              "description": body.get("description")}
        self._write("session.synthetic", s["id"], mid)
        if s["turn"] is not None and self.measured is not None and not self.measured[("working", self.form_of(body["delivery"], body["resume"]))]:
            return copy.deepcopy(record)   # measured: this form never reaches the model of a working session (admitted, never delivered)
        s["inbox"].append(item)
        if s["turn"] is None and self.starts_turn(s, body["delivery"], body["resume"]):
            self._start(s, at, self.wake_lasts, 1, items=[item])
        return copy.deepcopy(record)

    def op_session_prompt(self, params, body):
        s = self._session(params)
        if self.measured is not None and (s["turn"] is not None or not self.starts_turn(s, None, None)):
            # measured: a plain prompt to a working session reaches the model at its next step (a steer), and one the idle session ignores waits in the inbox; a working session
            # whose measured answer is "never reaches the model" drops it
            mid = self._id("msg")
            if s["turn"] is None or self.measured[("working", "prompt")]:
                s["inbox"].append({"id": mid, "sessionID": s["id"], "type": "user", "text": body["text"], "metadata": copy.deepcopy(body.get("metadata")),
                                   "resume": False, "delivery": "steer", "time": {"created": self.clock.ms(self.clock.now())}})
            return {"id": mid}
        self.start_turn(s["id"], body["text"], fleet=bool((body.get("metadata") or {}).get("fleet")),
                        metadata=body.get("metadata"))
        return {"id": s["messages"][-1]["id"]}

    @staticmethod
    def _inbox_record(item):
        """An inbox item as v2 2.0.22 returns it: text and metadata under
        `payload`, and no `resume` (S3, S5)."""
        return {"id": item["id"], "sessionID": item["sessionID"], "time": copy.deepcopy(item["time"]),
                "type": item.get("type", "synthetic"), "payload": {"text": item["text"], "metadata": copy.deepcopy(item.get("metadata"))},
                "delivery": item["delivery"]}

    def op_session_inbox_list(self, params, body):
        return [self._inbox_record(item) for item in self._session(params)["inbox"]]

    def op_session_inbox_update(self, params, body):
        s = self._session(params)
        item = next((i for i in s["inbox"] if i["id"] == params.get("inboxID")), None)
        if item is None:
            raise FakeHTTP(404, "NotFound", "no such pending input")
        if body["delivery"] == "steer" and item["delivery"] == "steer":
            raise FakeHTTP(409, "Conflict", "Pending input cannot change to steer")
        self._write("session.inbox.update", s["id"], item["id"])
        item["delivery"] = body["delivery"]
        if body["delivery"] == "steer" and s["turn"] is None:
            self._start(s, self.clock.now(), self.wake_lasts, 1, items=[item])
        return self._inbox_record(item)


HERDR_ALLOWED = (["agent", "list"], ["agent", "get"], ["pane", "report-metadata"], ["notification", "show"],
                 ["pane", "process-info"], ["pane", "close"], ["workspace", "list"], ["workspace", "create"],
                 ["plugin", "list"], ["pane", "get"], ["pane", "read"], ["tab", "close"], ["tab", "get"], ["tab", "list"])  # PR 16; #973: the bell migration reads the tab labels once; the control plane reads a pane and closes a stale tab: `status` reads herdr's plugin list; `bootstrap` makes a workspace


SOCKET_ALLOWED = ("layout.apply",)


TYPING_VERBS = (["agent", "prompt"], ["agent", "send-keys"], ["agent", "start"], ["pane", "send-text"],
                ["pane", "send-keys"], ["pane", "run"])


TOKEN_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _usage(argv, text):
    return subprocess.CompletedProcess(argv, 2, "", "error: %s\n" % text)


def parse_flags(args, valued, bare=()):
    """(positionals, flags) of a herdr subcommand, strictly: an unknown flag, a
    flag without its value, or a repeated flag is a usage error, as in clap."""
    positional, flags, i = [], {}, 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            if arg in bare:
                flags[arg] = True
            elif arg in valued and i + 1 < len(args):
                flags.setdefault(arg, []).append(args[i + 1])
                i += 1
            else:
                raise ValueError("unexpected argument %r" % arg)
        else:
            positional.append(arg)
        i += 1
    return positional, flags


def fact(key, created=EPOCH, recipient="platform", **extra):
    return sb.Fact(key, recipient, extra.pop("kind", "lab.fact"), extra.pop("summary", "s %s" % key), created, **extra)


def observation(status="idle", human=None, **extra):
    return sb.Observation("platform", "ses_1", "w1:p1", status, {"name": "platform"},
                          last_human_prompt=human, **extra)

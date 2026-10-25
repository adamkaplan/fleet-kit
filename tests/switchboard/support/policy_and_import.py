"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): policy_and_import."""
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
from support.loader import _load, sb
from support.scenario_roles import FAKE_GH, FAKE_REPO, LEGACY_POLICY
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


# ============================================================================
# PR 9: the tool-call policy judge, its plugin and scenarios P1 to P6
#
# One section, so the stack rebases cleanly: the offline runner learns the
# `tool-call` step and the `policy_outcome` oracle by attachment (as PR 4 and
# PR 5 did), then the tests. Nothing here reaches a real binary or a network:
# Jev is a replay file, a fake or a fake opener; v2 and GitHub are the fakes.
# ============================================================================
POLICY_REPLAY = REPO / "scenarios" / "jev" / "policy.json"


FLEET_HOOKS = REPO / "plugins" / "fleet-hooks"


NODE = shutil.which("node")  # found before each test narrows PATH


def policy_raw_config(**policy):
    """A validated config with the judge on, answering from the replay file."""
    return sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH,
                               "intent_repo": FAKE_REPO, "policy": {**LEGACY_POLICY, "enabled": True, **policy},
                               "jev": {"replay": str(POLICY_REPLAY)}})


def policy_replay_file(home, rules, name="replay.json"):
    path = Path(home) / name
    path.write_text(json.dumps({"rules": rules}))
    return str(path)


LOW = {"outside_intent": 0.0, "hard_to_reverse": 0.0, "speaks_for_you": 0.0, "outside_scope": 0.0}


BUDGET_TEST_MS = 1500   # #93: a real-time budget a loaded runner cannot outrun before the worker thread has started


HARNESS = r"""
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import plugin, * as hooks from "%(url)s";

const results = {};
const spawns = [];

function fakeSpawn(script) {
  return (bin, args, opts) => {
    const child = new EventEmitter();
    child.stdin = new PassThrough();
    child.stdout = new PassThrough();
    child.killed = null;
    child.kill = (signal) => { child.killed = signal; };
    child.unref = () => {};
    let written = "";
    child.stdin.on("data", (c) => { written += c; });
    const record = { bin, args, opts, child, get stdin() { return written; } };
    spawns.push(record);
    if (script.throws) throw new Error("spawn failed");
    if (script.error) setTimeout(() => child.emit("error", new Error("ENOENT")), 1);
    else if (script.reply !== undefined) {
      setTimeout(() => {
        if (script.reply !== null) child.stdout.write(script.reply + "\n");
        if (script.close !== false) child.emit("close", 0);
      }, script.delayMs || 1);
    }
    return child;
  };
}

async function run(name, input, script, timeoutMs = 300) {
  const spawnFn = fakeSpawn(script);
  const handler = hooks.makeHandler({ bin: "/abs/fleet-switchboard", timeoutMs, spawnFn });
  const started = Date.now();
  await handler(input);
  const last = spawns[spawns.length - 1];
  results[name] = { effect: input.effect, message: input.message, ms: Date.now() - started,
                    killed: last ? last.child.killed : null, spawned: spawns.length };
}

const base = (effect) => ({ sessionID: "ses_abc", agent: "coder", action: "shell", resources: ["git push --force"], effect });
const reply = (outcome, reason = "why") => JSON.stringify({ outcome, reason, judged: true, cached: false, latency_ms: 5 });

%(body)s

console.log(JSON.stringify(results));
"""


# ======================================================================
# PR 10: moving a v1 session into v2 (import-v1), the cutover runbook, M1
#
# One section. It extends the fakes and the offline runner by attachment, so
# nothing above changes except one line in OfflineRun.__init__ (an agent that
# an `import-v1` step creates does not exist beforehand, as with `launch`).
# Nothing here runs a real binary: v1 is a fake that answers `export` only.
# ======================================================================
FAKE_V1 = "/fake/bin/opencode-v1"


V1_FIXTURES = REPO / "scenarios" / "v1-fixtures"


V1_MAIN = V1_FIXTURES / "chief-of-staff-export.json"


V1_CHILD = V1_FIXTURES / "subagent-export.json"


V1_MAIN_ID, V1_CHILD_ID = "ses_fixture00000000000001", "ses_fixture00000000000002"


def v1_export(path=V1_MAIN):
    return json.loads(path.read_text())


def convert(export=None, **kwargs):
    return sb.convert_v1_export(v1_export() if export is None else export, kwargs.pop("fleet", {"name": "chief", "role": "chief-of-staff"}),
                                **kwargs)


FORBIDDEN_IN_PUBLIC_TEXT = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/",
                                                "/ho" + "me/", "direc" + "tor(?!y|ies)"]), re.IGNORECASE)


TRIAL = _load("t_switchboard_trial", BIN / "switchboard-trial")

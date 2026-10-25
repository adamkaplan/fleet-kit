"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): fake_jev."""
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


KEY_VALUE = "sk-or-test-0123456789abcdef"  # a made-up key: it only has to be findable


class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class FakeOpener:
    """urlopen's shape. Answers from `reply`: a body, or an exception instance to raise."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, request, timeout=None):
        self.calls.append({"url": request.full_url, "method": request.get_method(), "timeout": timeout,
                           "headers": dict(request.header_items()), "body": json.loads(request.data)})
        if isinstance(self.reply, BaseException):
            raise self.reply
        return FakeResponse(self.reply)


def http_error(code, body=b"{}"):
    return sb.urllib.error.HTTPError("https://x.test", code, "msg", {}, io.BytesIO(body))


def noul_answers(**given):
    return {name: {"type": "noul", "noul": p} for name, p in given.items()}


def choice_answer(choice, probabilities):
    return {"type": "choice", "choice": choice, "probabilities": probabilities}


def jev_reply(answers, **extra):
    body = {"model": "typesafe/jev-1.13-20260917", "answers": answers,
            "usage": {"input_tokens": 120, "output_tokens": 9, "cost": 3.5e-05}}
    body.update(extra)
    return body


class FakeJev:
    """The decision model with deterministic answers and no network.

    `rule` is answers (as in a Jev reply), a JevError reason string, None
    (unreachable), or a function (state, questions) -> one of those.
    """

    def __init__(self, rule=None, latency_ms=12):
        self.rule, self.calls, self.latency_ms = rule, [], latency_ms

    def decide(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        answers = self.rule(state, questions) if callable(self.rule) else self.rule
        if isinstance(answers, str):
            raise sb.JevError(answers, "fake", latency_ms=self.latency_ms)
        if answers is None:
            raise sb.JevError("unreachable", "fake had no answer", latency_ms=self.latency_ms)
        return {"model": "fake", "answers": sb.check_answers(questions, answers),
                "usage": {"in": 100, "out": 5, "cost": 3e-05}, "latency_ms": self.latency_ms}


def confident(choice, top=0.95, others=("current", "new")):
    """Answers to a classify question: `choice` at `top`, the rest sharing what is left."""
    rest = [o for o in others if o != choice]
    probabilities = {choice: top}
    probabilities.update({o: (1 - top) / len(rest) for o in rest} if rest else {})
    return {sb.CLASSIFY_ID: choice_answer(choice, probabilities)}


JEV_SETTINGS = dict(sb.JEV_DEFAULTS)


SHIM = REPO / "shims" / "gh"


# The shim looks its identity up with a short deadline (3 s by default, two process starts for an older switchboard). A test that expects a NAME must not
# depend on how busy the machine is, so its shim runs get the longest deadline the shim allows (10 s). The tests of the deadline itself set their own.
SHIM_LOOKUP_SECONDS = "10"


GIT_SHIM = REPO / "shims" / "git"


def write_fake_env(home, environ):
    """The shim starts the switchboard with an ALLOW-LISTED environment, so a `FAKE_*` the test sets never reaches a fake switchboard: hand them over the way the
    account's own program would find them, in a file under TMPDIR (a name the shim does pass). The fake sources it when it is there."""
    lines = ["%s=%s; export %s" % (k, shlex.quote(v), k) for k, v in sorted(environ.items()) if k.startswith("FAKE_")]
    (home / "fake-sb.env").write_text("\n".join(lines) + "\n")
    environ["TMPDIR"] = str(home)


FAKE_SB_SOURCE = '[ -f "$TMPDIR/fake-sb.env" ] && . "$TMPDIR/fake-sb.env"\n'

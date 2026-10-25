"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): loader."""
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


def with_http_head(serve, argv, kwargs, counters=None):
    """What `gh api -i [-H "If-None-Match: <etag>"] <path>` does, over a fake that answers `gh api <path>` (`serve(argv_without_flags, kwargs)`): the answer
    gets an HTTP header block with an ETag (a hash of the body) and a conditional read whose tag still matches is answered 304 (exit 1, as the real gh
    does). Anything that is not exactly that shape goes to `serve` untouched."""
    argv = list(argv)
    flags = argv[2:-1]
    tag_in = flags[2].split(": ", 1)[1] if flags[:2] == ["-i", "-H"] and len(flags) == 3 and flags[2].startswith("If-None-Match: ") else None
    if not (flags[:1] == ["-i"] and (len(flags) == 1 or tag_in is not None)):
        return serve(argv, kwargs)
    plain = serve(argv[:2] + argv[-1:], kwargs)
    if counters is not None:
        counters["http"] = counters.get("http", 0) + 1
    if plain.returncode != 0:
        return plain
    tag = 'W/"%s"' % hashlib.sha256(plain.stdout.encode()).hexdigest()[:16]
    if tag_in == tag:
        if counters is not None:
            counters["not_modified"] = counters.get("not_modified", 0) + 1
        return subprocess.CompletedProcess(argv, 1, "HTTP/2.0 304 Not Modified\r\nEtag: %s\r\n\r\n" % tag, "gh: HTTP 304\n")
    return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 200 OK\r\nEtag: %s\r\nX-Ratelimit-Remaining: 4000\r\n\r\n%s" % (tag, plain.stdout), plain.stderr)


SWITCHBOARD = BIN / "fleet-switchboard"


def _manifest_files():
    """Every file that holds the daemon's code, read from the manifest the entry itself reads (a part cannot escape a scan that reads this list)."""
    lines = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.lstrip().startswith("#")]
    return tuple((BIN / n).resolve() for n in lines)


SWITCHBOARD_SOURCE_FILES = _manifest_files()


def switchboard_source():
    """The daemon's code as every scan reads it: the files of SWITCHBOARD_SOURCE_FILES, in order, as one text."""
    return "\n".join(path.read_text() for path in SWITCHBOARD_SOURCE_FILES)


SCENARIO = BIN / "fleet-scenario"


def copy_entry(directory):
    """The entry with its manifest and its parts, copied into `directory` as the program is laid out in bin/."""
    directory = Path(directory)
    shutil.copy(str(SWITCHBOARD), str(directory / "fleet-switchboard"))
    shutil.copy(str(BIN / "fleet-switchboard.parts"), str(directory / "fleet-switchboard.parts"))
    shutil.copytree(str(BIN / "switchboard"), str(directory / "switchboard"), dirs_exist_ok=True)


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sb = _load("t_fleet_switchboard", SWITCHBOARD)


sc = _load("t_fleet_scenario", SCENARIO)


def class_source(name, method=None):
    """The source of a class in bin/fleet-switchboard, or of one of its methods."""
    source = switchboard_source()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            if method is None:
                return ast.get_source_segment(source, node)
            for inner in node.body:
                if isinstance(inner, ast.FunctionDef) and inner.name == method:
                    return ast.get_source_segment(source, inner)
    raise LookupError("%s.%s not found" % (name, method))


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def class_source_fn(name):
    """The source of a module-level function of bin/fleet-switchboard."""
    source = switchboard_source()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise LookupError(name)

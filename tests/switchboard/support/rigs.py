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


def set_retired(state, retired):
    """A test sets the retired map: in the registry when it exists (the record), and in retired.json (its derived copy), as the registry itself would."""
    state = Path(state)
    (state / "retired.json").write_text(json.dumps(retired))
    path = state / "registry.json"
    if path.exists():
        doc = json.loads(path.read_text())
        doc["retired"] = retired
        path.write_text(json.dumps(doc))


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

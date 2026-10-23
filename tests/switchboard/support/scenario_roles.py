"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): scenario_roles."""
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
from support.orders_and_adapters import report


JEV_REPLAY = REPO / "scenarios" / "jev" / "replay.json"


def format_scenario_report(report):
    lines = ["%s: run %d/%d pass; control %s:" % (
        report["id"], sum(r["ok"] for r in report["run"]), len(report["run"]), report["control_kind"])]
    for r in report["control"]:
        lines.append("    %-20s %s  (%s)" % (r["label"], "pass" if r["ok"] else "FAIL", r["seen"]))
    return "\n".join(lines)


GH_REPO = "fleet/scratch"


GH_BIN = "/fake/bin/gh"


def _stdlib_roots():
    paths = {sysconfig.get_paths()[k] for k in ("stdlib", "platstdlib")}
    return tuple(os.path.realpath(p) for p in paths)


BRIEF_30_60 = "ISSUE: x\nCHECKPOINT: Report at 30 minutes (kind working, one line); overdue at 60.\n"


FAKE_GH = "/fake/bin/gh"


def is_floor_view(argv):
    """A floor read of a pull request: a `pr view` of its files that is not `review record`'s own read (which also asks for the base name, #1047), so a test counting floor reads leaves the record out."""
    return "files" in argv[-1] and "baseRefName" not in argv[-1]


FAKE_REPO = "acme/fleet"


# The judge tests were written for a judge that fires at 0.5 on every call; the defaults of issue 87 (the routine pre-filter, 0.9 for
# three questions) are tested in TestJudgeAccuracy. These fixtures keep the earlier behaviour so those tests still say what they said.
LEGACY_POLICY = {"routine_filter": False, "thresholds": {q: 0.5 for q in ("outside_intent", "hard_to_reverse", "speaks_for_you", "outside_scope")}}


AGENT_FILES = tuple(sorted((REPO / "agents" / "opencode").glob("*.md"))
                    + sorted((REPO / "agents" / "copilot").glob("*.agent.md")))


# chief-of-staff +350: the quiet `.` ack (#96). chief-of-staff +150: the signed-comment sentence. PR 13 (`report`): coder +300, orchestrator +150, chief-of-staff +100
# chief-of-staff +400: read-only/merge STOP lines (#105; the native permissions list is excluded from the count)
# #129: the waiting definitions (the label and the Decision-required rule, in the coder and orchestrator; the same-turn
# escalation line in the chief of staff): coder +500, orchestrator +500, chief-of-staff +100
# #133: a held PR is held by LABELS (merge-captain and a hold-<reason>), the first line only for a dated window: coder +250, orchestrator +250, chief-of-staff +250
# #130: +200 B each for the parked sentence
# #68: coder +150, orchestrator +450: the launch self-wake sentence and the held-PR text for the copilot definitions
# #109: coder +1 line, +100 B; orchestrator +100 B: the fleet-planning load line
# #109: chief-of-staff +100 B: the fleet-planning load line
# one-command verdict text: coder +2 lines +450 B, orchestrator +150 B
# #34: the one `heavy` line (full suites and e2e runs go through fleet-switchboard heavy): coder +1 line +100 B, orchestrator +90 B
# two maxims restored in the coder: +2 lines, +200 B
# hygiene pointer: coder -2 lines, -100 B (three maxims became one pointer to the standards)
# #107 follow-up: coder +300 B for the spelled-out fallback and the never-your-own rule
# `pr-state`: coder +1 line +140 B, orchestrator +1 line +120 B: the one sentence that sends an agent to the daemon's cache instead of a GitHub polling loop
# #401 slice 1: the one-sentence GitHub writing rule in its own short section: coder +4 lines +226 B, orchestrator +2 lines +220 B (the wider runtime's file sets each limit)
# #341: the one never-`git stash` sentence (the files sat at their caps): coder +86 B, orchestrator +60 B, no new line
ROLE_BYTE_LIMITS = {"coder": 8585, "orchestrator": 11290, "chief-of-staff": 12097, "cos-subagent": 3500}


WORKER_ROLES = ("coder", "cos-subagent")  # PR 8: the roles that do an assignment, not a judgement of others


def role_of(path):
    return path.name.split(".")[0]

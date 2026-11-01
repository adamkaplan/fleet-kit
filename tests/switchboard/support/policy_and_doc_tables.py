"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): policy_and_doc_tables."""
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
from support.delivery_measure import REPO
from support.loader import sb


def measured_table(fixture, override=None):
    """{(case, form): turn started?} of the fixture's rows, with `override` ({(case, form): bool}) changed on a copy: the mutants of the fake."""
    table = {(row["case"], row["form"]): bool(row["turn_started"]) for row in fixture["rows"]}
    table.update(override or {})
    return table


def derived_forms():
    """Every way one message is delivered, from the client's own table: each delivery with resume true and false, and a plain prompt."""
    return [d + "_resume" if resume else d for d in sb.V2Client.DELIVERIES for resume in (True, False)] + ["prompt"]


DOC_EXAMPLE_LINE = "in 15 min, at 2030-01-02T15:05:05Z"   # the one line docs/switchboard.md shows for a reminder (ask #968)


PR4_CATALOG = {"N1": 3, "N2": 3, "W1": 4, "W3": 4, "Q1": 3, "R1": 4, "H1": None, "R3": None}


SHARED_MAXIMS = ("Festina lente", "Chesterton's fence", "Cut the root, not the branch", "Outcomes, not mechanics",
                 "Say it failed", "A diagnosis is not a mandate", "Don't widen the ask",
                 "Permission doesn't travel", "An empty queue is not a mandate")


HYGIENE_MAXIMS = ("Say it failed",)   # the one maxim the coder holds as a rule (R-CODE-1); the owner's two principles for how a coder works stay in the definition


COS_ONLY_MAXIMS = ("message stands alone", "No change, no message",
                   "Evidence, consequence, options, recommendation")


RUBRIC = ("grows the contract", "can't be undone", "speaks for your principal", "a key that isn't yours",
          "ready for your principal's eyes", "stuck after trying")


ROLE_LINE_LIMITS = {"coder": 134, "orchestrator": 180, "chief-of-staff": 162, "cos-subagent": 70}  # lines; they must not grow


def maxims_block(text):
    match = re.search(r"^## Maxims\n(.*?)(?=^## )", text, re.S | re.M)
    return match.group(1) if match else ""


POLICY_CASES = {  # answers -> judged outcome with nothing configured (allow)
    "nothing high": ({"outside_intent": 0.1, "hard_to_reverse": 0.2, "speaks_for_you": 0.0, "outside_scope": 0.3}, "allow"),
    "outside intent": ({"outside_intent": 0.9}, "hard-deny"),
    "hard to reverse": ({"hard_to_reverse": 0.8}, "ask"),
    "speaks for you": ({"speaks_for_you": 0.8}, "ask"),
    "outside scope": ({"outside_scope": 0.8}, "ask"),
    "outside intent beats the rest": ({"outside_intent": 0.9, "hard_to_reverse": 0.9, "outside_scope": 0.9}, "hard-deny"),
    "at the threshold": ({"hard_to_reverse": 0.5}, "ask"),
    "just under": ({"hard_to_reverse": 0.499, "outside_intent": 0.499}, "allow"),
}


CANARY = "canary-key-7f3a9c1e5b2d"  # a made-up key: it only has to be findable


CUTOVER_DOC = REPO / "docs" / "switchboard-cutover.md"


PRE_EXISTING_REDEFINITIONS = {}  # names a bin file already defined twice before PR 10 (none)


# ======================================================================
# #96: a worker.stopped that would wake the Chief of Staff alone is judged: "does this tell the person anything new?"
# ======================================================================
STOPPED_NEWS_RULES = [
    {"contains": "NEWS-NO", "noul": {"stopped_news": 0.02, "report_owed": 0.95}},
    {"contains": "NEWS-YES", "noul": {"stopped_news": 0.9, "report_owed": 0.95}},
    {"contains": "NEWS-MID", "noul": {"stopped_news": 0.3, "report_owed": 0.95}},
    {"contains": "NEWS-DOWN", "error": "unreachable"},
    {"contains": "OWED-ME", "noul": {"report_owed": 0.95}},
]


KEEP_OUT = object()   # a field a raw test record leaves out


# (plan hash, scenario hash) of five plan texts with a tester measure scenario, computed BEFORE the change to how G1 reads an unwritten claim (the checker is not touched by it): an unwritten claim and a
# written `claim: pr` share one scenario hash because the canonical text has always filled `pr` in.
PINNED_G1_HASHES = [
    ("sha256:f59db1a8ea7a7a5a9a14aea40ed171467a1e44a2c22f5b7dad7ee8db9525800c", "sha256:1eb0d1ae3803c387308206baebdee4ec8f46ea649a0bc93ea21d154c9c0c2172"),
    ("sha256:cda1cf00b68e99a39c2333618de5eb886785c7a380e9f78c4d3a8f700e441269", "sha256:1eb0d1ae3803c387308206baebdee4ec8f46ea649a0bc93ea21d154c9c0c2172"),
    ("sha256:50a9daea80cdb6d0deb32da91d8b0b0ef938642b69c70ef6d5bb16db17146c98", "sha256:82e8896b50f5a203fa7c9f454d9f0de39a71a5f07183d9847c0b1f71e303238e"),
    ("sha256:d5dde84d977987a17ed348cc1c2023a1456ef00b00d6b98ba412eccd8b6932a9", "sha256:1eb0d1ae3803c387308206baebdee4ec8f46ea649a0bc93ea21d154c9c0c2172"),
    ("sha256:d2bead8ec573f01a5fb2ddc9acb39c65c9398838fb1c741d3897c2602a0eab33", "sha256:bfa5d6a9830a09f93946c6a90158edb784c7ca24778c4ece33870c9077a92e64"),
]


IMPACT_TABLE = {   # the plan's table (S10): severity -> probability -> action; H hold, 2 second review, . none
    "Catastrophic": dict(zip(("Improbable", "Remote", "Occasional", "Probable", "Frequent"), "HHHHH")),
    "Critical": dict(zip(("Improbable", "Remote", "Occasional", "Probable", "Frequent"), "2HHHH")),
    "Marginal": dict(zip(("Improbable", "Remote", "Occasional", "Probable", "Frequent"), ".22HH")),
    "Negligible": dict(zip(("Improbable", "Remote", "Occasional", "Probable", "Frequent"), "...22")),
}


VERBS = ["add_argument", "add_argument_group", "add_mutually_exclusive_group", "add_parser", "add_subparsers", "set_defaults"]


FREE_KEYWORDS = ["help", "dest", "metavar", "required", "nargs", "const", "func"]   # pinned equal to the matrix by a test of the allow table


CHANGE_KINDS = ("base", "record", "matrix", "rating", "head", "none")   # (#999) what can change under a hold the daemon put on, with the head unchanged: the ONE table every row below derives from


STRUCTURAL_RULE = ("When a function decides from text (an error message, a substring, a pattern, a name list), the first-round finding asks for an allow-list, "
                   "a structured field or exact equality, and for a completeness test derived from the code, with one mutant per member, so the class is closed in one "
                   "round and not found one instance at a time.")


def structural_rule_homes(root):
    """Every file that must carry STRUCTURAL_RULE, derived from the tree: each reviewer definition found in the directory (no count), the review skill, the rule's own page in the review laws' documentation."""
    root = Path(root)
    return sorted((root / "agents" / "reviewers").glob("*.md")) + [root / "skills" / "fleet-review" / "SKILL.md", root / "docs" / "review-laws" / "structural-rule.md"]


def structural_rule_problems(root):
    """[(file name, why)] for every home that does not hold the sentence exactly once, or holds a second near copy of it."""
    out = []
    for path in structural_rule_homes(root):
        text = " ".join(path.read_text().split())
        if text.count(STRUCTURAL_RULE) != 1:
            out.append((path.name, "the shared sentence appears %d times, not once" % text.count(STRUCTURAL_RULE)))
        elif text.count("decides from text") != 1:
            out.append((path.name, "a second wording of the rule"))
    return out


# ---- docs/agent-credentials.md (#568): the document has its sections and no token-like text (S1, S2, S3, S5, U1)
CRED_DOC_PATH = str(REPO / "docs" / "agent-credentials.md")


CRED_TOKEN_PATTERNS = [
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bxox[baprs]-[A-Za-z0-9-]{10,}", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"(?i)\b(token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9+/_-]{24,}",
]


CRED_CRITERIA = ("Cost", "What breaks", "Rotation", "Who merges")


def cred_doc_sections(text):
    """{heading: body} for every `## ` section."""
    parts = re.split(r"(?m)^## (.+)$", text)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


NO_CAP_WORDS = re.compile(r"(cap|quota|ceiling|allowance)", re.I)


AGENT_WORDS = re.compile(r"(agent|lane|caller|worker|spender)", re.I)


def per_agent_limits(path, text):
    """[name] of every identifier, attribute, dictionary key or constant string of the Python source `text` that names a limit per agent: a cap word together with an
    agent word (`agent_cap`, `lane_quota`, `max_calls_per_agent`), or a per-agent word alone. Names of the form `NOT_AGENTS` (the lanes a message never names) are not limits."""
    tree = ast.parse(text)
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60:
            names.append(node.value)
        for name in names:
            if re.search(r"max_?calls|calls_?per|per_?(agent|lane|caller)_?(cap|limit|quota|max|count|calls)", name, re.I) or (NO_CAP_WORDS.search(name) and AGENT_WORDS.search(name)):
                found.add(name)
    return sorted(found)


# Live observers: the call sites that ask herdr which panes exist right now (`"agent", "list"`), keyed by (file, function), each a NAMED entry with its reason,
# never a silent skip. The heartbeat's dated entry (2026-11-03) ended with #973: its bell opt-in is the registry's `bell` field. The two left are the live source the registry is fed from.
LIVE_OBSERVERS = {
    ("bin/fleet-switchboard", "agent_list"): "discovery's own herdr client: the live source the registry is fed from (herdr and the v2 service), not a record of who exists",
    ("bin/proof-switchboard", "pane_of_session"): "the proof harness finds the pane of one session in a lab; it keeps no list",
}


# Views that read an old file from outside Python, from their own installed copy: derived output of the registry until the last slice (docs/agent-registry.md).
DERIVED_VIEWS = {"plugins/fleet-decisions-tui/health.mjs": "pane-health.json"}

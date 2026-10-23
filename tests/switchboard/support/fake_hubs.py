"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): fake_hubs."""
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
from support.loader import sb


# ======================================================================
# PR 13: workers report, a bare idle wakes nobody, every message has a line
#
# docs/switchboard.md, "Workers report (PR 13)". The faults added here:
# idle-wakes, no-rider, no-nudge-once, no-fail-open, ignore-reports,
# no-description.
# ======================================================================
REPORT_REPLAY_RULES = [
    {"contains": "OWED-ME", "noul": {"report_owed": 0.95}},
    {"contains": "JEV-DOWN", "error": "unreachable"},
    {"noul": {"report_owed": 0.05}},
]


def write_report_replay(home, rules=None):
    path = Path(home) / "report-replay.json"
    path.write_text(json.dumps({"rules": REPORT_REPLAY_RULES if rules is None else rules}))
    return str(path)


class StalledHub:
    """The one list the stalled rule's open-PR input comes from: what `ci_record_pass` reads through `hub.read_pages`."""

    def __init__(self):
        self.records, self.cut, self.fail = [], False, False
        self.paths = []

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        self.paths.append(path)
        if self.fail:
            raise sb.GitHubUnavailable("the list could not be read")
        limit = (cap or 1) * 100   # the real reader stops at its page cap and says so
        return list(self.records)[:limit], self.cut or len(self.records) > limit


class WaitingInputs:
    """#816: the inputs the shared waiting predicate reads, wired the way the daemon fills them (the PR list and its read-whole mark come from
    `ci_record_pass` over a fake hub; the issue list is the triage's, marked by hand). Used by the stalled rule's and the idle rule's tests."""
    REPO = "acme/widget-service"

    def wire_inputs(self):
        engine = self.world.engine
        engine.state_dir = self.world.state
        engine.config["intent_repo"] = self.REPO
        engine.config["plan_gates"] = {"repos": {self.REPO: {"mode": "shadow"}}}
        engine.gh_runner = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called"))   # no call is made to judge a worker
        self.hub = engine.hub = StalledHub()
        self.t0 = self.world.clock.now()
        self.params = {"head": "a" * 40, "issue_labels": (), "pr_labels": (), "checks": None, "pr": True, "at": self.t0}
        self.apply()
        (self.world.state / sb.DECISIONS_FILE).write_text(json.dumps({"decisions": [], "errors": []}))

    def apply(self):
        """Read the lists as the daemon does: the issue list is the triage's (marked by hand: not this change's code), the PR list is `ci_record_pass`'s."""
        engine, prm = self.world.engine, self.params
        at, now = sb.iso(prm["at"]), self.world.clock.now()
        engine.triage = sb.TriageState()
        engine.triage.units[(self.REPO, "issues")] = {"at": now, "data": [
            {"number": n, "updated_at": at, "created_at": at, "labels": [{"name": l} for l in (prm["issue_labels"] if n == 2 else ())]}
            for n in range(1, 100)], "truncated": False, "error": None}
        sb.read_mark(engine, "issues:" + self.REPO, True, "", now)
        sb.read_mark(engine, "decisions", True, "", now)
        self.hub.records = [{"number": 9, "body": "PLAN: https://github.com/%s/issues/2\n" % self.REPO, "updated_at": at, "created_at": sb.iso(prm["at"] - 86400), "head": {"sha": prm["head"]},
                             "labels": [{"name": n} for n in prm["pr_labels"]], **prm.get("pr_extra", {})}] if prm["pr"] else []
        engine.__dict__.pop("ci_record_state", None)
        engine.__dict__.get("read_whole", {}).pop("pulls:" + self.REPO, None)
        sb.ci_record_pass(engine, now)
        if prm["pr"] and prm["checks"] is not None:
            engine.ci_record_state.pr_state["%s#9" % self.REPO].update(
                {"examined_head": prm["head"], "checks": [{"name": "ci", "conclusion": c} for c in prm["checks"]]})

    def set_lists(self, **changes):
        self.params.update(changes)
        self.apply()


def sweep_issue(number, body="", labels=(), pr=False, author=None, created=None, updated=None):
    record = {"number": number, "body": body, "labels": [{"name": n} for n in labels]}
    if created:
        record["created_at"] = created
    if updated:
        record["updated_at"] = updated
    if author:
        record["user"] = {"login": author}
    if pr:
        record["pull_request"] = {"url": "x"}
    return record


# ======================================================================
# #130: the triage function (rule family T). THE OWNER'S HOLD: it writes nothing to GitHub. Synthetic repos only.
# ======================================================================
class FakeTriageHub:
    """Reads only: read_pages / _read_one over canned repos. A write cannot even be asked for: there is no such method."""

    def __init__(self):
        self.open, self.comments, self.closed, self.live = {}, {}, {}, {}   # per repo: open {n: rec}; comments [rec]; closed {label: [rec]}
        self.pulls = {}   # per repo: [recently closed pull request records] (#384)
        self.calls, self.unreadable = [], set()
        self.fail = []   # path fragments whose list reads fail (one kind of unit at a time)
        self.timelines, self.cut = {}, set()   # (repo, number) -> [timeline events] (#576); numbers whose timeline page is not the whole timeline
        self.cut_open = {}                     # #851: repo -> how many open issues its list read returns before it is cut (the read says it is not whole)

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        self.calls.append(path)
        if any(f in path for f in self.fail):
            raise sb.GitHubUnavailable("HTTP 502: a partial restart read")
        repo = re.search(r"repos/([^/]+/[^/]+)/", path).group(1)
        if repo in self.unreadable:
            raise sb.GitHubUnavailable("HTTP 404: Not Found")
        match = re.search(r"/issues/(\d+)/timeline", path)
        if match:
            number = int(match.group(1))
            events = self.timelines.get((repo, number))
            if events is None:
                raise sb.GitHubUnavailable("HTTP 500: no timeline")
            return list(events), number in self.cut
        match = re.search(r"/issues/(\d+)/comments", path)
        if match:
            return [c for c in self.comments.get(repo, []) if c["issue_url"].endswith("/%s" % match.group(1))][::-1], False
        if "/issues/comments" in path:
            return list(self.comments.get(repo, [])), False
        if re.search(r"/pulls\?", path):
            return list(self.pulls.get(repo, [])), False
        if "state=closed" in path:
            label = unquote(re.search(r"labels=([^&]+)", path).group(1))
            return list(self.closed.get(repo, {}).get(label, [])), False
        if repo in self.cut_open:
            return list(self.open.get(repo, {}).values())[:self.cut_open[repo]], True
        return list(self.open.get(repo, {}).values()), False

    def _read_one(self, repo, path, now, fresh=False):
        self.calls.append(path)
        number = int(path.rsplit("/", 1)[1])
        if repo in self.unreadable:
            raise sb.GitHubUnavailable("HTTP 404: Not Found")
        for pool in (self.live.get(repo, {}), self.open.get(repo, {})):
            if number in pool:
                return dict(pool[number])
        for records in self.closed.get(repo, {}).values():
            for record in records:
                if record["number"] == number:
                    return dict(record, state="closed")
        return {"number": number, "state": "closed"}


def tri_issue(clock_now, number, created=5.0, updated=None, author="bot", assignees=(), labels=(), body="", pr=False,
              draft=False, parent=None, repo="acme/api", state="open", title=None):
    """A record `created` / `updated` days old (updated defaults to created)."""
    updated = created if updated is None else updated
    record = {"number": number, "state": state, "body": body, "user": {"login": author},
              "assignees": [{"login": a} for a in assignees], "labels": [{"name": l} for l in labels],
              "created_at": sb.iso(clock_now - created * 86400), "updated_at": sb.iso(clock_now - updated * 86400)}
    if title is not None:
        record["title"] = title
    if pr:
        record["pull_request"] = {"url": "x"}
        record["draft"] = draft
    if parent:
        record["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (repo, parent)
    return record


# ======================================================================
# PR 14: Decisions, a read-only list of what waits on the human
#
# docs/switchboard.md, "Decisions (PR 14)". The faults added here:
# no-decisions-file, stale-as-fresh, decision-resolves-never.
# ======================================================================
DECISION_LABEL = "me:awaiting-user"


LEDGER_PLAN = """## Intent
Commissioning works.
Done when: (1) an owner can commission an agent; (2) it answers a chat message.

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: S2
    covers: DW-2
    text: Given a commissioned agent, when the owner sends a chat message, then it answers
    proof: live
    pass: the reply arrives within 5 s and is longer than 0 characters
    discriminates: a stub that returns a canned empty reply fails
    evaluator: tester
  - id: S3
    covers: DW-2
    text: Given the chat window, when the reply arrives, then the header shows the agent state
    proof: screenshot
    pass: the header shows the word "Ready" after the reply
    discriminates: a build that leaves the header blank shows an empty header in the screenshot
    evaluator: reviewer
  - id: S4
    covers: PC-1
    text: Given a plan section claimed at the epic, when the epic is integrated, then it holds
    proof: live
    pass: the integrated build returns status 200 for 3 of 3 sample projects
    discriminates: a build that drops the third project returns 2 of 3 and fails
    evaluator: owner
    claim: epic
  - id: U1
    covers: PC-1
    text: Existing projects with agents are listed exactly as before this change
    proof: test
    pass: the golden list output is byte-identical to the stored fixture
    discriminates: a change in ordering or fields shows as a diff against the fixture
    evaluator: ci
```
"""


LEDGER_HEAD = "a" * 40


LEDGER_NEW_HEAD = "b" * 40


class FakeHub:
    """The daemon's GitHub reader, for the ci recorder's pass: `read_pages` answers a list of open PRs per repo and counts reads."""

    def __init__(self, prs):
        self.prs, self.reads, self.down = prs, [], False

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        self.reads.append(path)
        if self.down:
            raise sb.GitHubUnavailable("network unreachable")
        repo = re.match(r"repos/([^/]+/[^/]+)/pulls", path).group(1)
        return list(self.prs.get(repo, [])), False


FLOOR_CONFIG = {"impact_floor": {"repos": {"acme/app": {"globs": ["migrations/**", "api/routes/**", "config/secrets*"],
                                                         "exclude": ["docs/**"]}}}}


def top_tier_migrations(test):
    """(#451 slice 3) The impact hold labels by the OWNER-HOLD rule, which reads the matrix file's top tier paths. These classes' fixtures use a synthetic path
    (`migrations/**`) as their floor path: for them the matrix the decision loads gains that one path as a top tier path, so the fixtures keep their
    meaning: a change to a top tier path holds."""
    real = sb.impact_floor.load_matrix

    def loaded(path=None):
        matrix = real(path)
        matrix["owner_hold"]["top_tier_paths"]["authority"] = list(matrix["owner_hold"]["top_tier_paths"]["authority"]) + ["migrations/**"]
        return matrix
    test.top_tier_patcher = mock.patch.object(sb.impact_floor, "load_matrix", loaded)
    test.top_tier_patcher.start()
    test.addCleanup(test.top_tier_patcher.stop)

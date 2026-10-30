"""Test classes moved out of bin/test-switchboard (text unchanged): cases_08."""
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
from support.core import Base
from support.loader import SWITCHBOARD, captured, copy_entry, sb, switchboard_source
from support.paths import BIN, REPO
from support.rigs import _artifact_zip
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


class FakeActions:
    """A fake GitHub for `reds`: a pull request, runs per event, an artifact list and artifact zips. Records every path it is asked (all GETs)."""

    REPO = "acme/widget"

    def __init__(self):
        self.calls, self.argvs, self.runs, self.artifacts, self.zips = [], [], {}, {}, {}
        self.pull = {"head": {"sha": "b" * 40}, "base": {"repo": {"default_branch": "main"}}}
        self.fail_paths = set()
        self.next_id = 100

    WORKFLOWS = {"A": (11, ".github/workflows/a-ci.yml"), "B": (22, ".github/workflows/b-style.yml")}

    def pr_run(self, names, complete=True, artifact=True, workflow="A", created="2027-01-15T08:00:00Z", **more):
        return self.add_run("pull_request", "feature", names, complete, created, artifact, sha="b" * 40, workflow=workflow, **more)

    def add_run(self, event, branch, names, complete=True, created="2027-01-15T07:00:00Z", artifact=True, sha=None, expired=False, raw=None, workflow="A", no_workflow_id=False):
        self.next_id += 1
        wid, path = self.WORKFLOWS[workflow]
        run = {"id": self.next_id, "event": event, "head_branch": branch, "head_sha": sha or ("%07d" % self.next_id) + "c" * 33, "created_at": created, "status": "completed",
               "workflow_id": wid, "name": "Workflow " + workflow, "path": path}
        if no_workflow_id:
            del run["workflow_id"]
        self.runs.setdefault(event, []).append(run)
        if artifact:
            self.zips[self.next_id + 5000] = raw if raw is not None else _artifact_zip(names, complete, self.next_id)
            self.artifacts[self.next_id] = {"id": self.next_id + 5000, "expired": expired, "workflow_run": {"id": self.next_id}, "size_in_bytes": len(self.zips[self.next_id + 5000])}
        return self.next_id

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        path = argv[-1]
        self.calls.append(path)
        self.argvs.append(argv)
        if len(argv) != 3 or argv[1] != "api" or any(a.startswith("-") for a in argv[2:]):
            raise AssertionError("fake github serves `gh api <path>`, a plain GET; got %r" % argv)
        done = lambda text, code=0, err="": subprocess.CompletedProcess(argv, code, text, err)
        if any(bad in path for bad in self.fail_paths):
            return done("", 1, "gh: HTTP 500")
        base = "repos/%s/" % self.REPO
        if path == base + "pulls/12":
            return done(json.dumps(self.pull))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        found = re.fullmatch(re.escape(base) + r"actions/workflows/([^/?]+)/runs", urllib.parse.urlparse(path).path)
        if found:   # one workflow's own runs, by id or by file name
            which = urllib.parse.unquote(found.group(1))
            event = query["event"][0]
            runs = [r for r in self.runs.get(event, []) if (getattr(self, "leak_foreign", False) or str(r.get("workflow_id")) == which or r["path"].endswith("/" + which)) and ("branch" not in query or r["head_branch"] == query["branch"][0])]
            return done(json.dumps({"workflow_runs": sorted(runs, key=lambda r: r["created_at"], reverse=True)[:int(query.get("per_page", ["30"])[0])]}))
        if path.startswith(base + "actions/runs?"):
            if "head_sha" not in query:
                raise AssertionError("reds must not list the repo's runs by event alone: that was the defect (%r)" % path)
            event = query["event"][0]
            runs = [r for r in self.runs.get(event, []) if ("head_sha" not in query or r["head_sha"] == query["head_sha"][0]) and ("branch" not in query or r["head_branch"] == query["branch"][0])]
            return done(json.dumps({"workflow_runs": sorted(runs, key=lambda r: r["created_at"], reverse=True)[:int(query.get("per_page", ["30"])[0])]}))
        if path.startswith(base + "actions/artifacts?"):
            return done(json.dumps({"artifacts": list(self.artifacts.values())}))
        found = re.fullmatch(re.escape(base) + r"actions/artifacts/(\d+)/zip", path)
        if found:
            return subprocess.CompletedProcess(argv, 0, self.zips[int(found.group(1))], b"")
        raise AssertionError("fake github: no route for %r" % path)


class TestReds(Base):
    """`reds OWNER/REPO#N` (ask #342): classify from failing-tests artifacts: main's latest run, flaky (2 of main's last 5 usable runs), or new."""

    def setUp(self):
        super().setUp()
        self.hs = sb.load_reds()
        self.fake = FakeActions()
        self.state = self.home / "reds-state"
        self.state.mkdir(mode=0o700)
        self.clock = [1_800_000_000.0]

    def now(self):
        return self.clock[0]

    def created(self, minutes_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock[0] - minutes_ago * 60))

    class Reader:
        def __init__(self, runner, repo):
            self.runner, self.repo = runner, repo

        def json(self, path):
            done = self.runner(["gh", "api", path])
            if done.returncode != 0:
                raise sb.load_reds().RedsError(done.stderr)
            return json.loads(done.stdout)

        def zip(self, path):
            done = self.runner(["gh", "api", path])
            if done.returncode != 0:
                raise sb.load_reds().RedsError(done.stderr)
            return done.stdout

    def run_reds(self, events=("push",), cache=True, workflow=None):
        out = io.StringIO()
        code = self.hs.run(self.Reader(self.fake, FakeActions.REPO), FakeActions.REPO, 12, list(events), self.state if cache else None, self.now, out, workflow)
        return code, out.getvalue()

    def main_runs(self, histories, event="push", branch="main"):
        """histories: failing-name lists, newest first."""
        for position, names in enumerate(histories):
            self.fake.add_run(event, branch, names, created=self.created(10 + position * 60))

    def test_s1_a_name_on_main_s_latest_run_is_main_two_of_five_is_flaky_and_the_rest_is_new(self):
        self.fake.pr_run(["t/a.test::a", "t/b.test::b", "t/c.test::c"])
        self.main_runs([["t/a.test::a"], ["t/b.test::b"], [], ["t/b.test::b"], []])
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(len([l for l in lines if l.startswith("    t/a.test::a")]), 1)
        self.assertIn("also failing on main (1):\n    t/a.test::a (1 of 5 main runs)", out)
        self.assertIn("flaky on main (1):\n    t/b.test::b (2 of 5 main runs)", out)
        self.assertIn("new (1):\n    t/c.test::c", out)
        self.assertRegex(out, r"main's latest run: push [0-9c]{7} \d+m ago \(workflow Workflow A; reference events: push\)")

    def test_s2_one_failure_in_five_is_not_flaky_two_is(self):
        self.fake.pr_run(["x::once", "y::twice"])
        self.main_runs([[], ["x::once", "y::twice"], [], ["y::twice"], []])
        code, out = self.run_reds()
        self.assertIn("flaky on main (1):\n    y::twice (2 of 5 main runs)", out)
        self.assertIn("new (1):\n    x::once (1 of 5 main runs)", out)

    def test_s3_runs_without_an_artifact_incomplete_or_expired_are_unknown_and_never_passing(self):
        self.fake.pr_run(["f::flaky-one"])
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(10))
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(70))
        self.fake.add_run("push", "main", [], created=self.created(130), artifact=False)
        self.fake.add_run("push", "main", ["f::flaky-one"], complete=False, created=self.created(190))
        self.fake.add_run("push", "main", ["f::flaky-one"], created=self.created(250), expired=True)
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 2 usable, 3 unknown", out)
        self.assertIn("flaky on main", out)
        self.assertNotIn("flaky-one (2 of 5", out)
        self.assertIn("(2 of 2 main runs)", out)

    def test_s3_an_unknown_latest_run_means_failing_on_main_cannot_be_said(self):
        self.fake.pr_run(["a::fails"])
        self.fake.add_run("push", "main", ["a::fails"], created=self.created(10), artifact=False)
        self.fake.add_run("push", "main", ["a::fails"], created=self.created(70))
        code, out = self.run_reds()
        self.assertIn('UNKNOWN (no artifact), so "failing on main" cannot be said', out)
        self.assertNotIn("also failing on main (1)", out)

    def test_s4_names_are_stripped_bounded_and_at_most_two_thousand_are_read(self):
        names = ["bad\x1b[2Jname\x9b\u2028::x", "L" * 500] + ["n%d" % i for i in range(3000)]
        self.fake.pr_run(names)
        self.main_runs([[]])
        code, out = self.run_reds()
        self.assertEqual([c for c in out if (ord(c) < 32 and c != "\n") or 0x7f <= ord(c) <= 0x9f or c in "\u2028\u2029"], [])
        self.assertLessEqual(max(len(line) for line in out.splitlines()), 240)
        self.assertIn("2000 failing in the pull request's run", out)

    def test_s5_cold_and_warm_calls_stay_within_the_limits_and_nothing_is_written(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"], [], ["a::t"], [], ["a::t"]])
        self.run_reds()
        cold = len(self.fake.calls)
        self.assertLessEqual(cold, 12, self.fake.calls)
        del self.fake.calls[:]
        self.run_reds()
        self.assertLessEqual(len(self.fake.calls), 3, self.fake.calls)
        self.assertTrue(self.fake.argvs and all(a[1] == "api" and len(a) == 3 for a in self.fake.argvs), "every call is a plain `gh api <path>` GET")
        # two reference events: 14 and 4
        second = FakeActions()
        second.pr_run(["a::t"])
        for position in range(3):
            second.add_run("merge_group", "gh-readonly-queue/main/pr-%d-abc" % position, ["a::t"], created=self.created(10 + position * 60))
        for position in range(2):
            second.add_run("schedule", "main", ["a::t"], created=self.created(40 + position * 60))
        shutil.rmtree(str(self.state))
        self.state.mkdir(mode=0o700)
        self.fake = second
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(second.calls), 14, second.calls)
        del second.calls[:]
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(second.calls), 4, second.calls)

    def test_s5_without_a_cache_every_call_is_made_again(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]] * 5)
        self.run_reds(cache=False)
        first = len(self.fake.calls)
        self.run_reds(cache=False)
        self.assertEqual(len(self.fake.calls), 2 * first)

    def test_s6_a_repo_with_only_merge_group_and_schedule_runs_uses_them_and_the_default_uses_push(self):
        self.fake.pr_run(["a::t", "b::u"])
        self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-9-aaa", ["a::t"], created=self.created(10))
        self.fake.add_run("merge_group", "gh-readonly-queue/release/pr-9-aaa", ["b::u"], created=self.created(5))   # another branch's queue: not main
        self.fake.add_run("schedule", "main", ["b::u"], created=self.created(70))
        self.fake.add_run("schedule", "feature", ["b::u"], created=self.created(1))
        code, out = self.run_reds(events=("merge_group", "schedule"))
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out)
        self.assertRegex(out, r"main's latest run: merge_group [0-9c]{7} ")
        self.assertIn("also failing on main (1):\n    a::t", out)
        code, out = self.run_reds(events=("push",))
        self.assertIn("main's latest run: none found", out)
        self.assertEqual(self.hs.DEFAULT_EVENTS, ["push"])

    def test_s7_full_name_first_then_a_unique_leaf_marked_and_a_duplicate_leaf_is_never_guessed(self):
        self.fake.pr_run(["a/x.test::exact", "p/y.test::renamed", "q/z.test::dup", "m::lonely"])
        self.main_runs([["a/x.test::exact", "other/y.test::renamed", "r/one.test::dup", "s/two.test::dup", "lonely"]])
        code, out = self.run_reds()
        self.assertIn("a/x.test::exact (1 of 1 main runs)\n", out)
        self.assertIn("p/y.test::renamed (1 of 1 main runs); matched by name only", out)
        self.assertIn("m::lonely (1 of 1 main runs); matched by name only", out)
        self.assertIn("new (1):\n    q/z.test::dup\n", out, "a leaf that is not unique stays unmatched")

    def test_s8_the_prs_own_incomplete_flag_never_judges_main(self):
        self.fake.pr_run(["a::t"], complete=False)
        self.main_runs([["a::t"], []])
        code, out = self.run_reds()
        self.assertIn("its list is INCOMPLETE", out)
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out)
        self.assertIn("also failing on main (1)", out)

    def test_u1_a_failed_read_says_so_classifies_nothing_and_exits_1(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]])
        for bad in ("pulls/12", "actions/runs?head_sha", "actions/workflows/11/runs", "actions/artifacts?name"):
            fake = self.fake
            fake.fail_paths = {bad}
            code, out = self.run_reds(cache=False)
            self.assertEqual(code, 1, bad)
            self.assertIn("nothing was classified", out)
            self.assertNotIn("also failing on main", out)
        self.fake.fail_paths = set()

    def test_a_garbage_artifact_a_pr_without_a_run_and_a_pr_without_an_artifact_are_said(self):
        self.fake.pr_run(["a::t"], artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 3, "unknown, not classified")
        self.assertIn("no failing-tests artifact on the pull request's run", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)   # the fakes reuse run ids
        broken = FakeActions()
        broken.add_run("pull_request", "feature", [], created=self.created(1), sha="b" * 40, raw=b"not a zip")
        self.fake = broken
        code, out = self.run_reds()
        self.assertIn("unknown, nothing was classified", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        empty = FakeActions()
        self.fake = empty
        code, out = self.run_reds()
        self.assertIn("no completed pull_request run", out)

    def unknown_case(self, build):
        self.fake = FakeActions()
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        self.fake.pr_run(["a::t", "b::u"])
        build(self.fake)
        return self.run_reds()

    def assert_cannot_classify(self, code, out, why):
        self.assertEqual(code, self.hs.EXIT_UNKNOWN, out)
        self.assertIn("no usable reference run (%s): cannot classify" % why, out)
        self.assertIn("unknown (2):\n    a::t\n    b::u", out)
        self.assertNotIn("new (", out, "a red is never called new on no evidence")
        self.assertNotIn("also failing on main", out)
        self.assertNotIn("flaky on main", out)

    def test_with_no_usable_reference_run_the_reds_are_unknown_never_new(self):
        code, out = self.unknown_case(lambda fake: None)
        self.assert_cannot_classify(code, out, "none found")
        code, out = self.unknown_case(lambda fake: [fake.add_run("push", "main", ["a::t"], created=self.created(10 + n * 60), expired=True) for n in range(3)])
        self.assert_cannot_classify(code, out, "no artifact: expired or never uploaded")
        code, out = self.unknown_case(lambda fake: [fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(10 + n * 60)) for n in range(3)])
        self.assert_cannot_classify(code, out, "incomplete: not all tests ran")
        code, out = self.unknown_case(lambda fake: fake.add_run("push", "main", [], created=self.created(10), raw=b"not a zip"))
        self.assert_cannot_classify(code, out, "unreadable: the artifact is not a zip")
        code, out = self.unknown_case(lambda fake: (fake.add_run("push", "main", [], created=self.created(10), artifact=False),
                                                    fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(70))))
        self.assert_cannot_classify(code, out, "incomplete: not all tests ran; no artifact: expired or never uploaded")

    def test_an_unknown_latest_run_with_older_complete_runs_classifies_by_those_alone_and_says_so(self):
        def build(fake):
            fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(10))   # latest: incomplete
            fake.add_run("push", "main", ["a::t"], created=self.created(70))
            fake.add_run("push", "main", ["a::t"], created=self.created(130))
        code, out = self.unknown_case(build)
        self.assertEqual(code, 0, out)
        self.assertIn("main's latest run is unknown: classified by the 2 older complete run(s) only; \"failing on main\" is not said", out)
        self.assertIn("flaky on main (1):\n    a::t (2 of 2 main runs)", out)
        self.assertIn("new (1):\n    b::u\n", out, "absent from every COMPLETE run: new is allowed here")
        self.assertNotIn("also failing on main (1)", out)

    def test_a_mix_of_unknown_and_one_complete_run_classifies_from_the_complete_one(self):
        def build(fake):
            fake.add_run("push", "main", [], created=self.created(10), artifact=False)
            fake.add_run("push", "main", ["a::t"], complete=False, created=self.created(70))
            fake.add_run("push", "main", ["a::t"], created=self.created(130))
        code, out = self.unknown_case(build)
        self.assertEqual(code, 0, out)
        self.assertIn("1 older complete run(s) only", out)
        self.assertIn("new (2):\n    a::t (1 of 1 main runs)\n    b::u\n", out)
        self.assertIn("main runs: 3 listed (workflow Workflow A), 1 usable, 2 unknown", out)

    # -- the workflow of the artifact (fix to #342: main's runs are the runs of the workflow that holds the pull request's artifact)

    def test_s1_only_the_artifact_workflow_s_main_runs_are_listed_and_another_workflow_is_never_unknown(self):
        self.fake.pr_run(["a::t", "b::u"])
        for position in range(5):
            self.fake.add_run("push", "main", ["a::t"] if position % 2 == 0 else [], created=self.created(10 + position * 60), workflow="A")
            self.fake.add_run("push", "main", [], created=self.created(5 + position * 60), workflow="B", artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 5 usable, 0 unknown", out)
        self.assertIn("workflow Workflow A; reference events: push", out)
        listed = [c for c in self.fake.calls if "actions/workflows/" in c]
        self.assertEqual(len(listed), 1)
        self.assertIn("workflows/11/runs", listed[0])

    def test_s1b_a_missing_artifact_of_the_chosen_workflow_is_unknown_and_another_workflow_never_is(self):
        self.fake.pr_run(["a::t"])
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        self.fake.add_run("push", "main", [], created=self.created(20), workflow="A", artifact=False)       # A without its artifact: unknown, as before
        self.fake.add_run("push", "main", [], created=self.created(5), workflow="B", artifact=False)        # B: never listed
        code, out = self.run_reds()
        self.assertIn("main runs: 2 listed (workflow Workflow A), 1 usable, 1 unknown", out)

    def test_s2_the_newest_pull_request_run_that_holds_the_artifact_is_the_pull_request_s_run_and_the_workflow_is_named(self):
        self.fake.pr_run([], artifact=False, workflow="B", created="2027-01-15T09:00:00Z")   # the newest run, another workflow, no artifact
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 0, out)
        self.assertIn("1 failing in the pull request's run of workflow Workflow A", out)
        self.assertIn("also failing on main (1)", out)
        self.assertNotIn("workflows hold a failing-tests artifact", out)

    def test_s2b_when_two_workflows_hold_an_artifact_it_says_so_in_one_line_and_names_the_one_used(self):
        self.fake.pr_run(["a::t"], workflow="B", created="2027-01-15T09:00:00Z")
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="B")
        code, out = self.run_reds()
        self.assertEqual(len([l for l in out.splitlines() if "workflows hold a failing-tests artifact" in l]), 1)
        self.assertIn("2 workflows hold a failing-tests artifact on this pull request (Workflow A, Workflow B): the newest run's workflow, Workflow B, was used", out)
        self.assertIn("(workflow Workflow B;", out)

    def test_s3_a_pull_request_none_of_whose_runs_holds_an_artifact_cannot_be_classified_and_main_is_not_listed(self):
        self.fake.pr_run([], artifact=False, workflow="A")
        self.fake.pr_run([], artifact=False, workflow="B", created="2027-01-15T07:30:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("no failing-tests artifact on the pull request's run")]), 1)
        self.assertFalse([c for c in self.fake.calls if "actions/workflows/" in c], "no main run of any workflow was listed")

    def test_s4_the_messages_name_the_workflow_and_say_how_many_of_its_runs_were_looked_at(self):
        self.fake.pr_run(["a::t"])
        for position in range(5):
            self.fake.add_run("push", "main", [], created=self.created(10 + position * 60), workflow="A", artifact=False)
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertEqual(len([l for l in out.splitlines() if l == "no failing-tests artifact in the newest 5 runs of workflow Workflow A"]), 1)
        self.assertIn("main runs: 5 listed (workflow Workflow A), 0 usable, 5 unknown", out)

    def test_s5_the_call_counts_with_two_reference_events_stay_within_14_cold_and_4_warm(self):
        self.fake.pr_run(["a::t"])
        for position in range(3):
            self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-%d-abc" % position, ["a::t"], created=self.created(10 + position * 60), workflow="A")
        for position in range(2):
            self.fake.add_run("schedule", "main", ["a::t"], created=self.created(40 + position * 60), workflow="A")
            self.fake.add_run("schedule", "main", [], created=self.created(41 + position * 60), workflow="B", artifact=False)
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(self.fake.calls), 14, self.fake.calls)
        cold = len(self.fake.calls)
        del self.fake.calls[:]
        self.run_reds(events=("merge_group", "schedule"))
        self.assertLessEqual(len(self.fake.calls), 4, self.fake.calls)
        self.assertTrue(all(len(a) == 3 for a in self.fake.argvs), "plain GETs only")

    def test_s6_the_workflow_override_picks_the_pull_request_s_run_of_that_file_and_lists_its_runs(self):
        self.fake.pr_run(["a::t"], workflow="B", created="2027-01-15T09:00:00Z")     # newer, with an artifact, but not the configured one
        self.fake.pr_run(["a::t"], workflow="A", created="2027-01-15T08:00:00Z")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        self.fake.add_run("push", "main", ["a::t"], created=self.created(5), workflow="B")
        code, out = self.run_reds(workflow="a-ci.yml")
        self.assertEqual(code, 0, out)
        self.assertIn("in the pull request's run of workflow Workflow A", out)
        self.assertIn("main runs: 1 listed (workflow Workflow A)", out)
        listed = [c for c in self.fake.calls if "actions/workflows/" in c]
        self.assertIn("workflows/a-ci.yml/runs", listed[0])
        code, out = self.run_reds(workflow="nothing.yml")
        self.assertEqual(code, 3)
        self.assertIn("no completed run of workflow nothing.yml", out)

    def test_s6_the_override_setting_is_strict_a_file_name_only(self):
        self.assertEqual(self.hs.workflows({"workflow": {"acme/widget": "ci.yml"}}), {"acme/widget": "ci.yml"})
        for bad in ("../ci.yml", ".github/workflows/ci.yml", "https://x/ci.yml", "ci", "ci.yml?x=1", "", 5, None, "ci.yml\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.hs.workflows({"workflow": {"acme/widget": bad}})
        for bad in ({"workflow": []}, {"workflow": {"nope": "ci.yml"}}):
            with self.assertRaises(ValueError):
                self.hs.workflows(bad)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "reds": {"workflow": {"acme/widget": "../x.yml"}}})

    def test_a_run_with_no_workflow_id_says_workflow_unknown_and_never_lists_by_event_alone(self):
        self.fake.pr_run(["a::t"], no_workflow_id=True)
        self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        code, out = self.run_reds()
        self.assertEqual(code, 3, out)
        self.assertIn("names no workflow (workflow unknown): cannot classify", out)
        self.assertFalse([c for c in self.fake.calls if "actions/workflows/" in c or ("actions/runs?" in c and "head_sha" not in c)])

    def test_a_non_positive_workflow_id_is_unknown_and_a_foreign_workflow_run_in_a_listing_is_skipped(self):
        for bad in (0, -3, True):
            with self.subTest(bad=bad):
                self.fake = FakeActions()
                shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
                self.fake.pr_run(["a::t"])
                self.fake.runs["pull_request"][-1]["workflow_id"] = bad
                code, out = self.run_reds()
                self.assertEqual(code, 3, out)
                self.assertIn("workflow unknown", out)
        shutil.rmtree(str(self.state / "reds-cache"), ignore_errors=True)
        self.fake = FakeActions()
        self.fake.pr_run(["a::t"])
        own = self.fake.add_run("push", "main", ["a::t"], created=self.created(10), workflow="A")
        foreign = self.fake.add_run("push", "main", ["a::t"], created=self.created(5), workflow="A")
        for run in self.fake.runs["push"]:
            if run["id"] == foreign:
                run["workflow_id"] = 22   # a listing that hands back another workflow's run: skipped, defensively
        self.fake.leak_foreign = True
        code, out = self.run_reds()
        self.assertIn("main runs: 1 listed (workflow Workflow A), 1 usable, 0 unknown", out)

    def test_the_real_command_reads_through_the_real_reader_with_the_configured_events_and_exits_1_on_a_failed_read(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": "gh", "reds": {"reference_events": {"acme/widget": ["merge_group", "schedule"]}}})
        self.fake.pr_run(["a::t", "b::u"])
        self.fake.add_run("merge_group", "gh-readonly-queue/main/pr-1-x", ["a::t"], created=self.created(10))
        self.fake.add_run("schedule", "main", ["b::u"], created=self.created(70))
        self.fake.add_run("push", "main", ["b::u"], created=self.created(5))   # a push run: not a reference event of this repo
        with captured() as (out, err):
            code = sb.main(["reds", "acme/widget#12"], runner=self.fake, now=self.now)
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("main runs: 2 listed (workflow Workflow A), 2 usable, 0 unknown", out.getvalue())
        self.assertIn("reference events: merge_group, schedule)", out.getvalue())
        self.assertTrue(any("event=merge_group" in c for c in self.fake.calls) and not any("event=push" in c for c in self.fake.calls))
        self.fake.fail_paths = {"actions/artifacts?name"}
        shutil.rmtree(str(sb.state_dir_path() / "reds-cache"), ignore_errors=True)
        with captured() as (out, err):
            code = sb.main(["reds", "acme/widget#12"], runner=self.fake, now=self.now)
        self.assertEqual(code, 1)
        self.assertIn("nothing was classified", out.getvalue())
        self.assertNotIn("also failing on main", out.getvalue())

    def test_an_oversized_or_unsized_artifact_is_refused_before_the_download_and_a_full_artifact_page_is_said(self):
        self.fake.pr_run(["a::t"])
        run = self.fake.add_run("push", "main", ["a::t"], created=self.created(10))
        self.fake.artifacts[run]["size_in_bytes"] = self.hs.ZIP_MAX + 1
        code, out = self.run_reds()
        self.assertIn("main runs: 1 listed (workflow Workflow A), 0 usable, 1 unknown; run %d: artifact too large" % run, out)
        self.assertFalse([c for c in self.fake.calls if c.endswith("/zip") and str(self.fake.artifacts[run]["id"]) in c], "never downloaded")
        del self.fake.artifacts[run]["size_in_bytes"]
        shutil.rmtree(str(self.state / "reds-cache"))
        code, out = self.run_reds()
        self.assertIn("artifact too large or of unknown size", out)
        # a full page: a run missing from it may be older than the page
        self.fake.artifacts.update({10_000 + n: {"id": 20_000 + n, "expired": False, "workflow_run": {"id": 10_000 + n}, "size_in_bytes": 10} for n in range(100)})
        missing = self.fake.add_run("push", "main", ["a::t"], created=self.created(5), artifact=False)
        shutil.rmtree(str(self.state / "reds-cache"))
        code, out = self.run_reds()
        self.assertIn("run %d: no artifact in the newest 100" % missing, out)

    def test_main_s_artifacts_are_capped_too_and_a_corrupt_cache_entry_is_a_miss(self):
        self.fake.pr_run(["n1"])
        self.main_runs([["n%d" % i for i in range(3000)]])
        code, out = self.run_reds()
        self.assertIn("n1 (1 of 1 main runs)", out)
        cached = sorted((self.state / "reds-cache" / "acme_widget").glob("*.json"))
        for path in cached:
            body = json.loads(path.read_text())
            self.assertLessEqual(len(body.get("failed", [])), self.hs.NAMES_MAX)
            path.write_text("{garbage")
        del self.fake.calls[:]
        code, out = self.run_reds()
        self.assertEqual(code, 0, "a corrupt cache entry is read again, not trusted")
        self.assertGreater(len(self.fake.calls), 4)

    def test_the_age_is_right_in_any_time_zone(self):
        stamp = calendar.timegm((2027, 1, 15, 8, 0, 0))
        for zone in ("UTC", "America/New_York", "Asia/Kolkata"):
            with mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                self.assertEqual(self.hs._when("2027-01-15T07:00:00Z", stamp), 3600)
        time.tzset()

    def test_the_artifact_reader_is_bounded_and_strict(self):
        for raw in (b"x" * (self.hs.ZIP_MAX + 1), b"PK not really", ):
            with self.assertRaises(self.hs.RedsError):
                self.hs.read_zip(raw)
        with self.assertRaises(self.hs.RedsError):
            self.hs.read_zip("a text, not bytes")
        for body in ('{"v": 2, "complete": true, "failed": []}', '{"v": 1, "complete": "yes", "failed": []}', "[1]", "nope"):
            with self.assertRaises(self.hs.RedsError):
                self.hs.parse_artifact(body)

    def test_the_cache_is_private_bounded_and_remembers_a_missing_artifact_only_for_a_while(self):
        self.fake.pr_run(["a::t"])
        self.main_runs([["a::t"]])
        self.run_reds()
        files = list((self.state / "reds-cache" / "acme_widget").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual({f.stat().st_mode & 0o777 for f in files}, {0o600})
        cache = self.hs.Cache(self.state, "acme/widget", self.now)
        cache.put(5, {"unknown": "no artifact"})
        self.assertEqual(cache.get(5)["unknown"], "no artifact")
        self.clock[0] += self.hs.UNKNOWN_TTL + 1
        self.assertIsNone(cache.get(5), "a missing artifact is looked for again later")

    def test_the_config_key_is_validated_and_the_command_takes_only_owner_repo_n(self):
        self.assertEqual(self.hs.settings({"reference_events": {"acme/widget": ["merge_group", "schedule"]}}), {"acme/widget": ["merge_group", "schedule"]})
        for bad in (3, {"x": 1}, {"reference_events": []}, {"reference_events": {"nope": ["push"]}}, {"reference_events": {"a/b": []}},
                    {"reference_events": {"a/b": ["pushh"]}}, {"reference_events": {"a/b": ["push", "push"]}}):
            with self.assertRaises(ValueError):
                self.hs.settings(bad)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "reds": {"reference_events": {"a": ["push"]}}})
        with captured() as (out, err):
            self.assertEqual(sb.main(["reds", "no-hash"], runner=self.fake, now=self.now), sb.EXIT_USAGE)


class TestTempJanitorResume(Base):
    """Ask #469: a scan that runs out of its time budget records the name it stopped at, and the next run continues there."""

    def setUp(self):
        super().setUp()
        self.jan = sb.load_disk_janitor()
        self.temp = self.home / "jtmp"
        self.temp.mkdir()
        self.state = self.home / "jstate"
        self.state.mkdir()
        (self.state / "state-marker.json").write_text("{}")
        self.now = time.time() + 10 * 86400
        self.t = [0.0]
        self.judged = []
        real = self.jan.judge

        def counting(path, *a, **k):
            self.judged.append(os.path.basename(path))
            row = real(path, *a, **k)
            self.t[0] += 1.0
            return row
        patcher = mock.patch.object(self.jan, "judge", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, name, days):
        path = self.temp / name
        path.write_text("x")
        stamp = self.now - days * 86400
        os.utime(str(path), (stamp, stamp))
        return path

    def twelve(self, old_from=None):
        for n in range(1, 13):
            self.entry("e%02d.txt" % n, 20 if old_from and n >= old_from else 1)

    def run_once(self, seconds=4.5, apply=True, **extra):
        self.t[0] = 0.0
        self.judged.clear()
        cfg = self.jan.settings(dict({"scan_seconds": seconds}, **extra))
        lines, removed, kept = self.jan.run(self.temp, self.state, self.now, cfg, apply, lambda *a: None, clock=lambda: self.t[0], busy=lambda path: None)
        return lines, removed, kept, list(self.judged)

    def cursor(self):
        return self.jan.read_cursor(self.state)

    def test_s1_every_entry_is_judged_exactly_once_over_three_runs(self):
        self.twelve()
        seen = []
        for _ in range(3):
            seen.extend(self.run_once()[3])
        self.assertEqual(sorted(seen), ["e%02d.txt" % n for n in range(1, 13)])
        self.assertEqual(len(seen), len(set(seen)), "none judged twice before the cycle completes")

    def test_s2_a_cursor_naming_a_deleted_entry_resumes_at_the_next_name(self):
        self.twelve()
        self.run_once()
        self.assertEqual(self.cursor(), "e05.txt")
        (self.temp / "e05.txt").unlink()
        self.assertEqual(self.run_once()[3][0], "e06.txt", "by name, not by position: nothing after the deleted entry is skipped")

    def test_s3_an_entry_that_was_not_reached_is_never_removed_and_is_not_judged(self):
        self.twelve(old_from=6)
        lines, removed, kept, judged = self.run_once()
        self.assertEqual(removed, 0 + sum(1 for n in judged if int(n[1:3]) >= 6), "only judged entries can be removed")
        for n in range(6, 13):
            name = "e%02d.txt" % n
            self.assertEqual((self.temp / name).exists(), name not in judged, name)
        self.assertEqual(len(judged), 5)
        self.assertIn("5 judged, 7 not judged", lines)
        self.assertFalse(any(line.startswith("kept e") and "time budget" in line for line in lines), "not judged is never reported as kept for a reason")

    def test_s4_each_report_says_judged_and_not_judged_and_the_last_says_the_cycle_is_complete(self):
        self.twelve()
        reports = [self.run_once()[0] for _ in range(3)]
        self.assertIn("5 judged, 7 not judged", reports[0])
        self.assertIn("5 judged, 2 not judged", reports[1])
        self.assertIn("2 judged, 0 not judged", reports[2])
        self.assertIn("cycle complete", reports[2])
        self.assertEqual(sum(line == "cycle complete" for report in reports for line in report), 1)
        self.assertIsNone(self.cursor(), "a finished cycle clears the cursor")

    def test_s5_a_missing_empty_or_corrupt_cursor_starts_at_the_first_name(self):
        self.twelve()
        path = self.state / self.jan.CURSOR_FILE
        for content in (None, "", "{garbage", '{"after": 5}', '{"after": ""}'):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content)
            self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt", repr(content))

    def test_s6_the_weekly_budget_is_longer_and_both_come_from_configuration(self):
        cfg = self.jan.settings({})
        self.assertGreater(cfg["weekly_scan_seconds"], cfg["scan_seconds"])
        self.jan.validate({"scan_seconds": 30, "weekly_scan_seconds": 900}, "config")
        for bad in ({"scan_seconds": 0}, {"weekly_scan_seconds": "long"}, {"scan_seconds": True}, {"weekly_scan_seconds": 10 ** 9}):
            with self.assertRaises(ValueError) as caught:
                self.jan.validate(bad, "config")
            self.assertIn(list(bad)[0], str(caught.exception))
        self.twelve()
        self.assertEqual(len(self.run_once(seconds=2.5, weekly_scan_seconds=8.5, apply=False)[3]), 3)
        self.t[0] = 0.0
        self.judged.clear()
        lines, removed, kept = self.jan.run(self.temp, self.state, self.now, self.jan.settings({"scan_seconds": 2.5, "weekly_scan_seconds": 8.5}), False,
                                            lambda *a: None, clock=lambda: self.t[0], busy=lambda path: None, weekly=True)
        self.assertEqual(len(self.judged), 9, "the weekly run uses the longer budget")

    def test_s7_a_scan_that_finishes_behaves_as_before_and_clears_the_cursor(self):
        self.twelve(old_from=11)
        (self.state / self.jan.CURSOR_FILE).write_text('{"after": "e10.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertEqual(sorted(set(judged)), ["e11.txt", "e12.txt"], "(each removal candidate is judged once more right before it is removed)")
        self.assertEqual(removed, 2)
        self.assertIn("cycle complete", lines)
        self.assertFalse((self.state / self.jan.CURSOR_FILE).exists())

    def test_s8_no_thread_and_no_process_is_added(self):
        self.twelve()
        before = threading.active_count()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("a process was started")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")):
            self.run_once()
            self.run_once()
        self.assertEqual(threading.active_count(), before)

    def test_a_cursor_at_the_end_wraps_a_run_with_no_progress_keeps_it_and_a_dry_run_leaves_an_existing_one(self):
        self.twelve()
        cursor = self.state / self.jan.CURSOR_FILE
        cursor.write_text('{"after": "e12.txt"}')
        self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt", "a cursor at the last name wraps to the first")
        cursor.write_text('{"after": "zzz-after-everything"}')
        self.assertEqual(self.run_once(apply=False)[3][0], "e01.txt")
        self.assertEqual(json.loads(cursor.read_text()), {"after": "zzz-after-everything"}, "a dry run leaves an existing cursor")
        cursor.write_text('{"after": "e04.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=-1)
        self.assertEqual(judged, [], "the budget was spent before any entry")
        self.assertEqual(json.loads(cursor.read_text()), {"after": "e04.txt"}, "no progress: the cursor stays")

    def test_the_cursor_file_is_not_one_of_the_fleets_records(self):
        self.twelve()
        victim = self.entry("zz-victim-name.txt", 20)
        (self.state / self.jan.CURSOR_FILE).write_text('{"after": "zz-victim-name.txt"}')
        text, complete = self.jan.named_text(self.state)
        self.assertNotIn("zz-victim-name.txt", text)
        self.assertTrue(complete)

    def test_a_corrupt_cursor_a_symlink_or_directory_it_names_and_a_type_change_never_remove_what_was_not_judged(self):
        outside = self.home / "outside-dir"
        outside.mkdir(mode=0o700)
        os.chmod(str(outside), 0o700)   # a private target: only the symlink check can refuse it
        (outside / "precious.txt").write_text("keep me")
        self.twelve(old_from=6)
        cursor = self.state / self.jan.CURSOR_FILE
        cursor.write_text("\x00{not json")
        lines, removed, kept, judged = self.run_once(seconds=100)
        for n in range(1, 13):
            name = "e%02d.txt" % n
            self.assertEqual((self.temp / name).exists(), not (n >= 6 and name in judged), name)
        self.assertEqual(sorted(set(judged)), ["e%02d.txt" % n for n in range(1, 13)], "a corrupt cursor judged from the first name")
        for n in range(6, 13):
            self.entry("e%02d.txt" % n, 20)
        (self.temp / "e03.txt").unlink()
        (self.temp / "e03.txt").symlink_to(outside)   # the cursor will name an entry that is now a link to a directory
        cursor.write_text('{"after": "e03.txt"}')
        lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertNotIn("e03.txt", judged, "the run resumes AFTER the named entry")
        self.assertTrue((outside / "precious.txt").exists())
        self.assertTrue((self.temp / "e03.txt").is_symlink())
        # an entry that changes type between its judging and its removal is not removed
        self.entry("e20.txt", 20)
        real_scan = self.jan.scan

        def scan_then_swap(*a, **k):
            rows = real_scan(*a, **k)
            os.unlink(str(self.temp / "e20.txt"))
            (self.temp / "e20.txt").symlink_to(outside)
            return rows
        cursor.unlink(missing_ok=True)
        with mock.patch.object(self.jan, "scan", scan_then_swap):
            lines, removed, kept, judged = self.run_once(seconds=100)
        self.assertTrue((outside / "precious.txt").exists(), "a link is never followed")
        self.assertTrue((self.temp / "e20.txt").is_symlink(), "it changed since the scan: kept")
        self.assertIn("kept e20.txt: it changed since the scan", lines)

    def test_the_weekly_flag_is_parsed_and_the_command_passes_it_to_the_run(self):
        parser = sb.build_parser()
        self.assertTrue(parser.parse_args(["disk", "janitor", "--apply", "--weekly"]).weekly)
        self.assertFalse(parser.parse_args(["disk", "janitor", "--apply"]).weekly)
        source = inspect.getsource(sb.cmd_disk_janitor)
        self.assertIn('weekly = bool(getattr(args, "weekly", False))', source)
        self.assertIn("weekly=weekly, outcome=preview", source)

    def test_a_dry_run_reads_the_cursor_and_never_moves_it_and_the_file_is_private(self):
        self.twelve()
        self.run_once(apply=False)
        self.assertIsNone(self.cursor(), "a preview does not change what the next real run covers")
        self.run_once()
        mode = os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        stamp = os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mtime_ns
        self.jan.write_cursor(self.state, self.cursor())
        self.assertEqual(os.stat(str(self.state / self.jan.CURSOR_FILE)).st_mtime_ns, stamp, "written only when it changes")


class TestAgentRegistry(unittest.TestCase):
    """Slice 2 of #903: bin/registry.py by itself (the frozen interface of docs/agent-registry.md): atomic write, version rule, the fold of today's
    records, the registry's own check of a removal's evidence. HERDR_* and OPENCODE_* are unset for every test."""

    def setUp(self):
        for key in [k for k in os.environ if k.startswith(("HERDR_", "OPENCODE_"))]:
            self.addCleanup(os.environ.__setitem__, key, os.environ[key])
            del os.environ[key]
        self.state = Path(tempfile.mkdtemp(prefix="registry-test"))
        self.addCleanup(shutil.rmtree, str(self.state), True)
        self.reg = sb.load_module("registry")

    def old_records(self, **extra):
        row = {"session": "ses_a", "pane": "w1:p1", "role": "coder", "charter": 903, "repo": "acme-org/widget-service",
               "workspace": "w1", "cwd": "/home/user/work", "seen": 5}
        (self.state / "pane-health.json").write_text(json.dumps({"agents": {"alpha": dict(row, **extra)}, "states": {"alpha": {"state": "detached"}}, "missing": {}}))

    def test_s8_every_field_the_old_readers_use_is_in_the_registry_after_the_fold(self):
        self.old_records()
        row = self.reg.read(self.state)["agents"]["alpha"]
        for field in ("session", "pane", "role", "charter", "repo", "workspace", "cwd"):
            with self.subTest(field=field):
                self.assertEqual(row[field], {"session": "ses_a", "pane": "w1:p1", "role": "coder", "charter": 903, "repo": "acme-org/widget-service",
                                              "workspace": "w1", "cwd": "/home/user/work"}[field])
        self.assertEqual((row["state"], row["kind"], row["run_id"]), ("detached", "local", None))
        self.assertEqual(oct((self.state / "registry.json").stat().st_mode & 0o777), "0o600")

    def test_the_fold_keeps_a_retirement_with_its_record_headless_copy_and_a_cloud_worker_with_its_run_id(self):
        entry = {"at": int(time.time()), "by": "boss", "reason": "done", "session": "ses_b", "auto": False,
                 "record": {"pane": "w1:p2", "workspace": "w1", "cwd": "/home/user/b"}, "headless": {"session": "ses_b", "name": "beta"}}
        (self.state / "retired.json").write_text(json.dumps({"beta": entry}))
        (self.state / "cloud").mkdir()
        (self.state / "cloud" / "gamma.json").write_text(json.dumps({"worker": "gamma", "repo": "acme-org/widget-service", "runtime_id": "rt_1", "run_id": "run_example", "session": "ses_c"}))
        got = self.reg.read(self.state)
        self.assertEqual(got["retired"]["beta"], entry)
        self.assertEqual(self.reg.get_retired(self.state, "beta"), entry)
        self.assertEqual((got["agents"]["gamma"]["kind"], got["agents"]["gamma"]["run_id"], got["agents"]["gamma"]["session"]), ("cloud", "run_example", "ses_c"))

    def test_a_retired_record_with_no_session_covers_nothing_and_known_agent_finds_no_one(self):
        (self.state / "retired.json").write_text(json.dumps({"beta": {"at": 1, "by": "x", "reason": "r", "session": None, "record": {"pane": "p"}}}))
        self.assertEqual(sb.known_agent(self.state, "beta"), {})
        self.assertFalse(self.reg.covers(self.reg.get_retired(self.state, "beta"), None))
        self.assertTrue(self.reg.upsert(self.state, "beta", {"session": "ses_new", "pane": "p9"})["changed"])   # not refused as retired

    def test_known_agent_reads_a_retired_agents_record_from_the_registry_alone(self):
        self.assertTrue(self.reg.upsert(self.state, "alpha", {"session": "ses_a", "pane": "w1:p1", "workspace": "w1", "cwd": "/home/user/work", "role": "coder"})["changed"])
        self.assertTrue(self.reg.retire(self.state, "alpha", "boss", "done")["changed"])
        self.assertNotIn("alpha", self.reg.read(self.state)["agents"])
        kept = sb.known_agent(self.state, "alpha")
        self.assertEqual((kept["session"], kept["workspace"], kept["cwd"]), ("ses_a", "w1", "/home/user/work"))
        self.assertTrue(self.reg.unretire(self.state, "alpha")["changed"])
        self.assertEqual(self.reg.get(self.state, "alpha")["workspace"], "w1")
        self.assertEqual(sb.read_retired(self.state), {})

    def test_a_newer_version_reads_nothing_says_so_and_a_writer_writes_nothing(self):
        text = json.dumps({"version": 9, "agents": {"a": {"session": "ses_x"}}, "retired": {}})
        (self.state / "registry.json").write_text(text)
        got = self.reg.read(self.state)
        self.assertEqual((got["whole"], got["agents"], got["problem"]), (False, {}, "registry.json is version 9; this reader knows 1: nothing read"))
        self.assertEqual(self.reg.upsert(self.state, "b", {"session": "ses_y"}), {"changed": False, "problem": got["problem"]})
        self.assertEqual((self.state / "registry.json").read_text(), text)
        with self.assertRaises(sb.RetiredUnreadable):
            sb.registry_retired_strict(self.state)
        for bad in ("{", "[]", '{"version": "1"}', '{"version": 1, "agents": []}'):
            (self.state / "registry.json").write_text(bad)
            self.assertFalse(self.reg.read(self.state)["whole"], bad)

    def test_s6_two_writers_and_a_reader_never_see_a_torn_file_in_1000_rounds(self):
        self.reg.upsert(self.state, "seed", {"session": "ses_seed", "cwd": "x" * 5000})
        stop, torn = threading.Event(), []

        def write(tag):
            n = 0
            while not stop.is_set():
                n += 1
                self.reg.upsert(self.state, "w%s" % tag, {"session": "ses_%s_%d" % (tag, n), "cwd": "y" * 3000})

        threads = [threading.Thread(target=write, args=(t,)) for t in "ab"]
        for t in threads:
            t.start()
        try:
            for _ in range(1000):
                got = self.reg.read(self.state)
                if not got["whole"] or "seed" not in got["agents"]:
                    torn.append(got["problem"])
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(torn, [])
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.endswith(".tmp")], [])

    def test_upsert_with_nothing_changed_writes_and_audits_nothing(self):
        self.assertTrue(self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1"})["changed"])
        path = self.state / "registry.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns, (self.state / "registry-audit.jsonl").read_text())
        self.assertEqual(self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1"}), {"changed": False, "problem": None})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, (self.state / "registry-audit.jsonl").read_text()), before)

    def test_reconcile_remove_checks_the_evidence_itself_and_audits_a_removal(self):
        self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1", "state": "working"})
        alive = {"pane_listed": True, "process_found": True}
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", alive, True)["changed"], False)     # whole=True is not enough for a live agent
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", {"pane_listed": False, "process_found": False}, True)["changed"], False)   # recorded state is not dead
        self.reg.upsert(self.state, "a", {"session": "ses_a", "pane": "p1", "state": "dead"})
        gone = {"pane_listed": False, "process_found": False}
        self.assertEqual(self.reg.reconcile_remove(self.state, "a", gone, False)["changed"], False)    # a partial read removes nothing
        self.assertIn("a", self.reg.read(self.state)["agents"])
        self.assertTrue(self.reg.reconcile_remove(self.state, "a", gone, True)["changed"])
        self.assertNotIn("a", self.reg.read(self.state)["agents"])
        lines = [json.loads(l) for l in (self.state / "registry-audit.jsonl").read_text().splitlines()]
        self.assertEqual([l["event"] for l in lines if l["event"] == "reconcile_remove"], ["reconcile_remove"])
        self.assertEqual([l for l in lines if l["event"] == "reconcile_remove"][0]["evidence"], gone)
        refused = [l for l in lines if l["event"] == "reconcile_refused"]
        self.assertEqual(len(refused), 3)                                   # live evidence, state not dead, partial read: each audited with evidence and reason
        self.assertEqual(refused[0]["evidence"], alive)
        self.assertTrue(all(l["why"] and "nothing removed" in l["why"] for l in refused))

    def test_a_dead_row_is_removed_only_when_each_evidence_key_shows_it_gone_and_each_refusal_is_audited(self):
        gone = {"pane_listed": False, "process_found": False}
        cases = {"pane_listed true": {"pane_listed": True, "process_found": False}, "process_found true": {"pane_listed": False, "process_found": True},
                 "pane_listed missing": {"process_found": False}, "process_found missing": {"pane_listed": False}, "evidence None": None,
                 "evidence a string": "gone", "evidence a list": [gone]}
        for label, evidence in cases.items():
            with self.subTest(label=label):
                self.reg.upsert(self.state, "d", {"session": "ses_d", "state": "dead"})
                before = len([l for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "reconcile_refused" in l])
                got = self.reg.reconcile_remove(self.state, "d", evidence, True)
                self.assertEqual(got["changed"], False)
                self.assertIn("d", self.reg.read(self.state)["agents"])
                after = [json.loads(l) for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "reconcile_refused" in l]
                self.assertEqual(len(after), before + 1)
                self.assertEqual(after[-1]["evidence"], evidence if isinstance(evidence, dict) else None)
        self.assertTrue(self.reg.reconcile_remove(self.state, "d", gone, True)["changed"])

    def test_retire_then_undo_keeps_a_local_a_cloud_and_a_headless_row_what_it_was(self):
        self.reg.upsert(self.state, "loc", {"session": "ses_l", "pane": "p1", "workspace": "w1"})
        self.reg.upsert(self.state, "cld", {"session": "ses_c", "repo": "acme-org/widget-service", "kind": "cloud", "run_id": "run_example"})
        self.reg.retire(self.state, "loc", "boss", "done")
        self.reg.retire(self.state, "cld", "cloud-worker", "done")
        self.reg.retire(self.state, "hdl", "boss", "done", session="ses_h", extra={"headless": {"session": "ses_h"}})
        for name in ("loc", "cld", "hdl"):
            self.assertTrue(self.reg.unretire(self.state, name)["changed"])
        agents = self.reg.read(self.state)["agents"]
        self.assertEqual((agents["loc"]["kind"], agents["loc"]["run_id"], agents["loc"]["workspace"]), ("local", None, "w1"))
        self.assertEqual((agents["cld"]["kind"], agents["cld"]["run_id"], agents["cld"]["session"]), ("cloud", "run_example", "ses_c"))
        self.assertNotIn("hdl", agents)                                       # a headless worker comes back from its own launch record, not as a pane row

    def test_the_fold_does_not_bring_back_a_retired_cloud_worker_but_a_relaunch_under_the_name_stays(self):
        (self.state / "cloud").mkdir()
        (self.state / "cloud" / "w1.json").write_text(json.dumps({"worker": "w1", "session": "ses_c", "run_id": "r1"}))
        (self.state / "cloud" / "w2.json").write_text(json.dumps({"worker": "w2", "session": "ses_new", "run_id": "r2"}))
        (self.state / "retired.json").write_text(json.dumps({"w1": {"at": int(time.time()), "by": "x", "reason": "r", "session": "ses_c"},
                                                            "w2": {"at": int(time.time()), "by": "x", "reason": "r", "session": "ses_old"}}))
        got = self.reg.read(self.state)
        self.assertEqual(sorted(got["agents"]), ["w2"])
        self.assertEqual(sorted(got["retired"]), ["w1", "w2"])

    def test_retire_and_close_leave_agents_keep_the_record_and_audit(self):
        for name in ("a", "b"):
            self.reg.upsert(self.state, name, {"session": "ses_" + name, "pane": "p" + name, "workspace": "w1"})
        self.reg.retire(self.state, "a", "boss", "done", auto=True)
        self.reg.close(self.state, "b", "boss", "pane closed")
        got = self.reg.read(self.state)
        self.assertEqual(got["agents"], {})
        self.assertEqual((got["retired"]["a"]["auto"], got["retired"]["b"]["reason"], got["retired"]["b"]["record"]["workspace"]), (True, "pane closed", "w1"))
        self.assertEqual(sorted(json.loads(l)["event"] for l in (self.state / "registry-audit.jsonl").read_text().splitlines() if "retire" in l or "close" in l), ["close", "retire"])
        self.assertEqual(json.loads((self.state / "retired.json").read_text()).keys(), {"a", "b"})   # derived output, the old format


class FakeQueueGh:
    """`gh` for the merge queue ejection pass (#584): the merge queue runs of a repository, a pull request's state and queue entry (GraphQL), a run's artifacts
    and jobs. Counts what it is asked, per repository."""

    def __init__(self):
        self.runs, self.pulls, self.zips, self.jobs, self.calls, self.artifact_name = {}, {}, {}, {}, [], "failing-tests"
        self.fail, self.graphql_fault = set(), None

    def add(self, repo, run_id, pr, now, conclusion="failure", status="completed", name="ci", age=60, created_age=None, names=None, complete=True, raw=None, jobs=()):
        stamp = lambda ago: sb.iso(now - ago)
        self.runs.setdefault(repo, []).append({"id": run_id, "name": name, "event": "merge_group", "status": status, "conclusion": conclusion,
                                               "head_branch": "gh-readonly-queue/main/pr-%d-%s" % (pr, "%07x" % run_id), "created_at": stamp(created_age or age + 30), "updated_at": stamp(age)})
        self.pulls.setdefault((repo, pr), ("OPEN", False))
        if raw is not None:
            self.zips[run_id] = raw
        elif names is not None:
            self.zips[run_id] = _artifact_zip(names, complete, run_id)
        self.jobs[run_id] = list(jobs)
        return run_id

    def count(self, repo, needle):
        return len([c for c in self.calls if repo in c and needle in c])

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        done = lambda text, code=0, err="": subprocess.CompletedProcess(argv, code, text, err)
        if argv[1:3] == ["api", "graphql"]:
            fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
            repo, pr = "%s/%s" % (fields["owner"], fields["name"]), int(fields["number"])
            self.calls.append("graphql %s#%d" % (repo, pr))
            if self.graphql_fault == "exit":
                return done("", 1, "gh: HTTP 502")
            if self.graphql_fault == "weird":
                return done('{"data": {"repository": {"pullRequest": {"state": "WEIRD", "mergeQueueEntry": null}}}}')
            if self.graphql_fault == "malformed":
                return done('{"data": {"repository": null}}')
            state, queued = self.pulls[(repo, pr)]
            return done(json.dumps({"data": {"repository": {"pullRequest": {"state": state, "mergeQueueEntry": {"id": "Q", "state": queued if isinstance(queued, str) else "QUEUED"} if queued else None}}}}))
        path = argv[-1]
        self.calls.append(path)
        if any(bad in path for bad in self.fail):
            return done("", 1, "gh: HTTP 500")
        found = re.fullmatch(r"repos/([^/]+/[^/]+)/actions/runs\?event=merge_group&per_page=\d+", path)
        if found:
            return done(json.dumps({"workflow_runs": self.runs.get(found.group(1), [])}))
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts\?name=([^&]+)&per_page=\d+", path)
        if found:
            run = int(found.group(1))
            ok = run in self.zips and urllib.parse.unquote(found.group(2)) == self.artifact_name
            return done(json.dumps({"artifacts": [{"id": run + 9000, "name": self.artifact_name, "expired": False, "size_in_bytes": len(self.zips[run])}] if ok else []}))
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/artifacts/(\d+)/zip", path)
        if found:
            return subprocess.CompletedProcess(argv, 0, self.zips[int(found.group(1)) - 9000], b"")
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/jobs\?filter=latest&per_page=\d+", path)
        if found:
            return done(json.dumps({"jobs": [{"name": n, "conclusion": c} for n, c in self.jobs[int(found.group(1))]]}))
        raise AssertionError("the ejection pass reaches for nothing else: %r" % (argv,))


class TestClosingKeywordWarning(unittest.TestCase):
    """#1197: `preflight-pr` warns, never blocks, when the body closes an ask. The REAL `cmd_preflight_pr` runs with a faked runner.
    CLOSED rule (guard): the warning path makes only reads: every `gh` call it builds is `gh pr view` or `gh api` (no write verb).
    Known limits: a work item with a live check and no parent is not an ask here; a plain work item under an ordinary parent counts as an ask; the set of reads is read from the syntax tree of the two helpers only."""
    URL = "https://github.com/o/r/pull/9"

    def run_body(self, body, asks=(), unreadable=False):
        def runner(argv, **kwargs):
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = "/work/repo\n"
            elif argv[:3] == ["gh", "pr", "view"]:
                if unreadable:
                    done.returncode = 1
                else:
                    done.stdout = json.dumps({"body": body, "url": self.URL})
            elif argv[:2] == ["gh", "api"]:
                number = int(argv[2].split("/")[4])
                if number in asks:
                    done.stdout = json.dumps({"number": 34, "body": "an ordinary parent"})
                elif number == 700:   # parent is a charter
                    done.stdout = json.dumps({"number": 612, "body": "```yaml\norchestrator: x\n```"})
                else:
                    done.returncode = 1
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        return code, [l for l in out.getvalue().splitlines() if l.startswith("warning:")]

    def test_closes_an_ask_is_warned_once_and_the_exit_is_unchanged(self):
        code, warned = self.run_body("Closes #5", asks=(5,))
        self.assertEqual(code, 0)
        self.assertEqual(len(warned), 1)
        self.assertIn("o/r#5", warned[0])
        self.assertIn("Refs #5", warned[0])

    def test_refs_an_ask_is_silent(self):
        self.assertEqual(self.run_body("Refs #5", asks=(5,)), (0, []))

    def test_a_closing_keyword_for_a_plain_work_item_or_under_a_charter_is_silent(self):
        self.assertEqual(self.run_body("Closes #6", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("Fixes #700", asks=(5,)), (0, []))

    def test_several_keywords_in_one_body(self):
        code, warned = self.run_body("Closes #5\nfixes #6\nResolves #7 and Closes #5\nResolves o/x#8", asks=(5, 7, 8))
        self.assertEqual(code, 0)
        self.assertEqual([("#5" in w, "#7" in w, "#8" in w) for w in warned], [(True, False, False), (False, True, False), (False, False, True)])

    def test_a_keyword_inside_code_is_silent(self):
        body = "```\nCloses #5\n```\nand `Fixes #5` here\n~~~sh\nResolves #5\n~~~"
        self.assertEqual(self.run_body(body, asks=(5,)), (0, []))

    def test_a_quoted_line_and_an_html_comment_are_silent_but_a_keyword_outside_both_warns(self):
        self.assertEqual(self.run_body("> Closes #5", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("  > quoted\n>Fixes #5", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!-- Closes #5 -->", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!--\nCloses #5\n-->", asks=(5,)), (0, []))
        self.assertEqual(self.run_body("<!-- never closed\nCloses #5", asks=(5,)), (0, []))
        code, warned = self.run_body("> Closes #6\n<!-- Closes #7 -->\nCloses #5", asks=(5, 6, 7))
        self.assertEqual((code, len(warned), "#5" in warned[0]), (0, 1, True))

    def test_mutant_without_the_quote_and_comment_strip_fails_the_silent_cases(self):
        real = sb.closing_keyword_issues
        with mock.patch.object(sb, "closing_keyword_issues", lambda body: real(str(body).replace("<!--", "").replace("-->", "").replace(">", ""))):
            with self.assertRaises(AssertionError):
                self.test_a_quoted_line_and_an_html_comment_are_silent_but_a_keyword_outside_both_warns()

    def test_a_list_of_numbers_and_an_issue_url_warn_for_each(self):
        code, warned = self.run_body("Closes #5, #6 and #7", asks=(5, 6, 7))
        self.assertEqual([[n in w for n in ("#5", "#6", "#7")] for w in warned], [[True, False, False], [False, True, False], [False, False, True]])
        code, warned = self.run_body("Closes https://github.com/o/r/issues/5", asks=(5,))
        self.assertEqual((code, len(warned), "o/r#5" in warned[0]), (0, 1, True))
        code, warned = self.run_body("Fixes https://github.com/x/y/issues/5, o2/p#6", asks=(5, 6))
        self.assertEqual([("x/y#5" in warned[0]), ("o2/p#6" in warned[1])], [True, True])

    def test_a_hung_or_slow_gh_is_quiet_and_every_call_has_a_timeout(self):
        seen = []

        def runner(argv, **kwargs):
            seen.append((argv[:2], kwargs.get("timeout")))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        self.assertFalse(sb.issue_is_ask(runner, "o/r", 5))
        self.assertEqual(sb.preflight_body_warnings(runner), [])
        self.assertEqual(len(seen), 2)
        for argv, timeout in seen:
            self.assertIsInstance(timeout, (int, float), argv)
            self.assertGreater(timeout, 0)

    def test_an_unreadable_body_is_silent(self):
        self.assertEqual(self.run_body("Closes #5", asks=(5,), unreadable=True), (0, []))

    def test_mutant_that_never_warns_fails_the_warned_case(self):
        with mock.patch.object(sb, "closing_warnings", lambda body, repo, is_ask: []):
            with self.assertRaises(AssertionError):
                self.test_closes_an_ask_is_warned_once_and_the_exit_is_unchanged()

    def test_mutant_that_warns_on_refs_fails_the_silent_case(self):
        real = sb.closing_keyword_issues
        with mock.patch.object(sb, "closing_keyword_issues", lambda body: real(str(body).replace("Refs", "Closes"))):
            with self.assertRaises(AssertionError):
                self.test_refs_an_ask_is_silent()

    def test_the_warning_path_only_reads(self):
        tree = ast.parse(switchboard_source())
        seen = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("issue_is_ask", "preflight_body_warnings"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.List) and sub.elts and isinstance(sub.elts[0], ast.Constant) and sub.elts[0].value == "gh":
                        seen.append(tuple(e.value for e in sub.elts[1:3] if isinstance(e, ast.Constant)))
        self.assertEqual(sorted(seen), [("api",), ("pr", "view")])

    def test_the_rule_is_in_the_texts_and_the_docs(self):
        for rel in ("skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/copilot/coder.agent.md"):
            text = (REPO / rel).read_text()
            self.assertIn("Refs #N", text, rel)
            self.assertIn("never `Closes`", text, rel)
        docs = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("`Refs #N`, never `Closes`", docs)
        self.assertIn("The warning never blocks", docs)


class TestPreflightPr(unittest.TestCase):
    """`preflight-pr` (#716): one PASS or FAIL line per CI-critical class, run under `heavy`; the runner is faked, nothing real runs."""
    SENTENCE = "`fleet-switchboard preflight-pr` runs the CI-critical focused classes"

    def run_preflight(self, changed, failing=(), timing_out=()):
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = "/work/repo\n"
            elif argv[:2] == ["git", "merge-base"]:
                done.stdout = "abc123\n"
            elif argv[:2] == ["git", "diff"]:
                done.stdout = "\n".join(changed)
            elif "heavy" in argv:
                command = " ".join(argv[argv.index("--") + 1:])
                if any(word in command for word in timing_out):
                    raise subprocess.TimeoutExpired(argv, 1)
                done.returncode = 1 if any(word in command for word in failing) else 0
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        return code, out.getvalue().splitlines(), calls

    def test_s1_every_class_passing_prints_one_pass_line_each_and_exits_0(self):
        code, lines, calls = self.run_preflight(["bin/fleet-switchboard"])
        self.assertEqual(code, 0)
        self.assertEqual([l.split()[:2] for l in lines], [["PASS", "cli-surface"], ["PASS", "hygiene"], ["PASS", "public-names"]])
        self.assertEqual([c for c in calls if "heavy" in c[0] and "cloud" in " ".join(c[0])], [])

    def test_s2_a_failing_class_prints_fail_with_its_name_and_exits_1(self):
        code, lines, _ = self.run_preflight(["x"], failing=("check-public-names",))
        self.assertEqual(code, 1)
        self.assertEqual([l for l in lines if l.startswith("FAIL")].__len__(), 1)
        self.assertTrue([l for l in lines if l.startswith("FAIL")][0].startswith("FAIL public-names"))

    def test_a_slot_wait_is_never_a_timeout_and_the_ceiling_runs_inside_the_slot(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            done = mock.Mock(returncode=0, stdout="/work/repo\n" if argv[:2] == ["git", "rev-parse"] else "abc\n" if argv[:2] == ["git", "merge-base"] else "", stderr="")
            if "heavy" in argv:
                done.returncode = 75   # all slots held: heavy gave up waiting, and says so with the marker line that carries the caller's own nonce (an exit status alone is a result)
                done.stderr = "heavy: [noslot %s]\n" % kwargs["env"]["FLEET_HEAVY_NOSLOT_NONCE"]
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual(code, 1)
        self.assertNotIn("timed out", out.getvalue())
        self.assertIn("no heavy slot", out.getvalue())
        self.assertIn("waiting for a slot", err.getvalue())
        for argv, kwargs in calls:
            if "heavy" in argv:
                self.assertNotIn("timeout", kwargs)   # no outer clock around the queue
                self.assertIn("FLEET_HEAVY_MAX_SECONDS", kwargs["env"])

    def test_exit_124_from_the_heavy_watchdog_is_a_timeout_line_with_its_ceiling(self):
        code, lines, calls = self.run_preflight(["x"])
        self.assertEqual({c[1]["env"]["FLEET_HEAVY_MAX_SECONDS"] for c in calls if "heavy" in c[0]}, {"240", "60"})
        calls_124 = []

        def runner(argv, **kwargs):
            done = mock.Mock(returncode=124 if "heavy" in argv else 0, stdout="/r\n", stderr="")
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (4.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual(code, 1)
        self.assertIn("FAIL cli-surface", out.getvalue())
        self.assertIn("timed out after 480s running, its ceiling (load average 4.0 on 4 cores)", out.getvalue())   # S2: 240 * (1 + 4/4)

    def test_s1_the_ceiling_grows_with_the_load_and_never_passes_the_maximum(self):
        low, high, absurd = (sb.preflight_ceiling(600, load, 8) for load in (1, 60, 1000))
        self.assertGreater(high, low)
        self.assertLessEqual(max(low, high), sb.PREFLIGHT_CEILING_MAX)
        self.assertEqual(absurd, sb.PREFLIGHT_CEILING_MAX)   # S3
        self.assertEqual(sb.preflight_ceiling(600, 0, 8), 600)
        self.assertEqual(sb.preflight_ceiling(600, "x", 0), 600)

    def test_s2_a_class_over_its_ceiling_says_it_timed_out(self):
        code, lines, _ = self.run_preflight(["x"], timing_out=("TestHygiene",))
        self.assertEqual(code, 1)
        self.assertIn("timed out after", [l for l in lines if l.startswith("FAIL hygiene")][0])

    def test_s3_a_cloud_change_runs_the_cloud_suites_with_a_49_character_tmpdir(self):
        code, lines, calls = self.run_preflight(["cloud/worker/guard.py"])
        self.assertEqual(code, 0)
        self.assertEqual(len([l for l in lines if l.startswith("PASS cloud")]), 1)
        envs = [c[1]["env"] for c in calls if "heavy" in c[0] and "cloud" in " ".join(c[0])]
        self.assertEqual(len(envs), 3)
        for env in envs:
            self.assertEqual(len(os.path.basename(env["TMPDIR"])), 49)
        self.assertEqual(len([c for c in calls if "heavy" in c[0]]), 6)

    def test_s4_it_only_runs_git_reads_and_heavy_tests_and_leaves_no_scratch(self):
        _, _, calls = self.run_preflight(["cloud/x"])
        for argv, kwargs in calls:
            self.assertTrue(argv[0] == "git" and argv[1] in ("rev-parse", "merge-base", "diff") or "heavy" in argv or argv[:3] == ["gh", "pr", "view"] or argv[:2] == ["gh", "api"], argv)   # #1197: the two gh READS of the body warning
        scratch = {os.path.dirname(c[1]["env"]["TMPDIR"]) for c in calls if "TMPDIR" in c[1].get("env", {}) and "cloud" in " ".join(c[0])}
        self.assertTrue(scratch and not any(os.path.exists(d) for d in scratch))

    @classmethod
    def sentence_count(cls, text):
        return " ".join(text.split()).count(cls.SENTENCE)

    def test_s5_the_coordination_skill_names_the_command_once_and_the_count_is_exact(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertEqual(self.sentence_count(text), 1)
        self.assertEqual(self.sentence_count(text.replace(self.SENTENCE, "")), 0)
        self.assertEqual(self.sentence_count(text + "\n" + self.SENTENCE), 2)

    def test_s6_the_class_list_is_one_table_with_each_class_once(self):
        source = switchboard_source()
        self.assertEqual(source.count("PREFLIGHT_CLASSES = ("), 1)
        table = source.split("PREFLIGHT_CLASSES = (", 1)[1].split("\n)\n", 1)[0]
        names = [row[0] for row in sb.PREFLIGHT_CLASSES]
        self.assertEqual(len(names), 4)
        for name in names:
            self.assertEqual(table.count('("%s",' % name), 1)


class TestFocusedLocalRuns(unittest.TestCase):
    """#942: the documented default (focused runs, CI runs the whole suite), the refusal of a second full-suite job in a worktree and the preflight skip of a sub-check
    CI runs. Real slot files and queue in a temp state directory, a fake worktree function, a stub script as the command; nothing real is run. Each family is ONE table;
    each row has a test and a mutant (the defence swapped for a weaker one with mock.patch, and the attack then succeeds)."""
    DEFAULT = "Run the focused classes of what you changed through `heavy --short`; CI runs the whole suite."
    EXCEPTION = "Run the whole suite only for a change to the CI workflow itself or to a test CI cannot run; say which in the pull request."
    REFUSAL = "The heavy wrapper refuses a second full-suite job in a worktree while one is queued or running."
    FILES = ("skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/copilot/coder.agent.md", "docs/switchboard.md")
    A, B = "/work/a", "/work/b"

    def setUp(self):
        self.init()
        self.addCleanup(self.cleanup)

    def reset(self):
        self.cleanup()
        self.init()

    def init(self):
        self.hs = sb.load_heavy_slots()
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state")
        os.mkdir(self.state)
        self.script = os.path.join(self.dir, "test-fake")   # a program that classifies as full
        Path(self.script).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.script, 0o755)
        self.bin = os.path.join(self.dir, "bin")   # the only PATH the child gets: a fake `timeout` (macOS ships none, Ubuntu ships GNU's); no real program is needed
        os.mkdir(self.bin)
        os.symlink(shutil.which("sh"), os.path.join(self.bin, "sh"))   # `sh -c` is on both runners
        self.fake_timeout()
        self.children, self.fds = [], []

    def cleanup(self):
        for child in self.children:
            child.kill()
            child.wait()
        for fd in self.fds:
            os.close(fd)
        self.children, self.fds = [], []
        shutil.rmtree(self.dir, True)

    def fake_timeout(self):
        path = os.path.join(self.bin, "timeout")
        Path(path).write_text('#!/bin/sh\nshift\nexec "$@"\n')   # drops the duration, runs the rest
        os.chmod(path, 0o755)

    def environ(self):
        return {"PATH": self.bin, "FLEET_HEAVY_NAME": "second"}

    def start(self, command, worktree, short=False, wait_max=1):
        out = io.StringIO()
        code = self.hs.run(command, self.state, 3, wait_max, "second", environ=self.environ(), out=out, short=short, worktree=lambda cwd: worktree)
        return code, out.getvalue()

    def hold(self, worktree, kind="full"):
        """A job RUNNING in `worktree`: a slot whose lock is held, with the record a wrapper writes."""
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        self.hs._record(fd, "first", os.getpid(), "timeout", time.time(), {"kind": kind, "worktree": worktree, "program": "test-switchboard"})

    def queue(self, worktree, kind="full"):
        """A job QUEUED in `worktree`: an entry of a live process (a sleeping child) in the waiting line."""
        child = subprocess.Popen(["sleep", "60"])
        self.children.append(child)
        line = self.hs.Line(self.state, "first", pid=child.pid, meta={"kind": kind, "worktree": worktree, "program": "test-switchboard", "since": int(time.time())})
        line.place()
        # every slot busy, so the entry stays in the line (a ticket never outlives the slot its owner took)

    def fill(self):
        for _ in range(3):
            fd = self.hs._take(self.state, 3)[0]
            os.ftruncate(fd, 0)   # a busy slot that has no record (yet)
            self.fds.append(fd)

    # ---- THE refusal table: (id, how the first job exists, its worktree, the second command, the second's worktree, refused?)
    FULL, FOCUSED, WRAPPED = ["{s}"], ["{s}", "-k", "x"], ["timeout", "900", "{s}"]
    ROWS = (
        ("running-same-worktree", "run", "/work/a", FULL, "/work/a", True),
        ("queued-same-worktree", "queue", "/work/a", FULL, "/work/a", True),
        ("wrapped-in-timeout", "run", "/work/a", WRAPPED, "/work/a", True),
        ("focused-not-refused", "run", "/work/a", FOCUSED, "/work/a", False),
        ("other-worktree-not-refused", "run", "/work/a", FULL, "/work/b", False),
        ("first-ended-not-refused", "none", "/work/a", FULL, "/work/a", False),
        ("first-is-focused-not-refused", "run-focused", "/work/a", FULL, "/work/a", False),
        ("worktree-unknown-not-refused", "run", "/work/a", FULL, None, False),
    )

    def attempt(self, row):
        _id, how, first_tree, command, second_tree, _refused = row
        if how == "run":
            self.hold(first_tree)
        elif how == "run-focused":
            self.hold(first_tree, kind="focused")
        elif how == "queue":
            self.fill()
            self.queue(first_tree)
        command = [self.script if word == "{s}" else word for word in command]
        if how == "queue":   # the queue entry exists; the second run must be refused before it waits (a refusal needs no slot)
            return self.start(command, second_tree, wait_max=0)
        return self.start(command, second_tree)

    def test_every_row_of_the_refusal_table(self):
        for row in self.ROWS:
            with self.subTest(row=row[0]):
                self.reset()
                code, text = self.attempt(row)
                if row[5]:
                    self.assertEqual(code, 64)
                    self.assertIn("heavy: refused: a full-suite job is already queued or running in this worktree (first, test-switchboard, pid ", text)
                    self.assertIn(", since ", text)
                    if row[1] == "run":
                        self.assertIn("pid %d" % os.getpid(), text)
                else:
                    self.assertEqual(code, 0, text)
                    self.assertNotIn("refused", text)

    def test_a_mutant_without_the_fake_timeout_fails_the_wrapped_row_as_command_not_found_on_every_os(self):
        row = [r for r in self.ROWS if r[0] == "wrapped-in-timeout"][0]
        os.unlink(os.path.join(self.bin, "timeout"))
        self.hold(self.A)
        code, text = self.attempt(row)
        self.assertEqual(code, 127, text)

    def test_a_mutant_that_never_refuses_lets_every_refused_row_run(self):
        for row in self.ROWS:
            if row[5]:
                with self.subTest(row=row[0]), mock.patch.object(self.hs, "second_job_notice", lambda *a, **k: None):
                    self.reset()
                    self.assertNotEqual(self.attempt(row)[0], 64)

    def test_a_mutant_that_always_refuses_stops_every_allowed_row(self):
        for row in self.ROWS:
            if not row[5]:
                with self.subTest(row=row[0]), mock.patch.object(self.hs, "second_job_notice", lambda *a, **k: "heavy: refused: x"):
                    self.reset()
                    code = self.attempt(row)[0]
                    if row[4] is not None and row[0] != "focused-not-refused":   # a focused run or an unknown worktree never asks
                        self.assertEqual(code, 64)
                    else:
                        self.assertEqual(code, 0)

    def test_the_table_covers_every_kind_of_the_one_classifier(self):
        kinds = {kind for kind, _s, _m in self.hs.KINDS} - {"quick"}   # "quick" is a ceiling row taken by name (run_quick); the classifier never returns it
        probes = {"full": [], "focused": ["-k", "x"], "e2e": ["playwright", "test"], "other": ["ls"]}
        self.assertEqual(set(probes), kinds, "a kind with no probe")
        for kind, rest in probes.items():
            with self.subTest(kind=kind):
                self.reset()
                command = [self.script] + rest if kind in ("full", "focused") else rest
                self.hold(self.A)   # a full job is running in worktree A
                code, text = self.start(command, self.A) if kind in ("full", "focused") else (None, "")
                self.assertEqual(self.hs.classify(command), kind)
                self.assertEqual(code == 64, kind == "full") if code is not None else None

    def test_every_other_kind_runs_while_a_full_job_holds_the_worktree(self):
        for kind, rest in (("focused", ["-k", "x"]), ("other", None), ("e2e", None)):
            with self.subTest(kind=kind):
                self.reset()
                self.hold(self.A)
                if rest is None:
                    with mock.patch.object(self.hs, "classify", lambda command, short=False, kind=kind: kind):
                        code, text = self.start([self.script], self.A)
                else:
                    code, text = self.start([self.script] + rest, self.A)
                self.assertEqual(code, 0, text)

    def test_the_wrapper_is_a_visible_holder_before_it_leaves_the_line(self):
        seen = []
        real = self.hs.Line.leave

        def leave(line):
            seen.extend(i for i in self.hs._held_records(self.state) if i.get("worktree") == self.A and i.get("kind") == "full")
            return real(line)
        with mock.patch.object(self.hs.Line, "leave", leave):
            self.assertEqual(self.start([self.script], self.A)[0], 0)
        self.assertTrue(seen, "no record naming the worktree when the line was left")

    def test_the_marker_preflight_looks_for_is_in_the_text_the_wrapper_writes(self):
        hs = sb.load_heavy_slots()
        text = hs.second_job_notice(self.state_for_marker(), "/work/a", [{"kind": "full", "worktree": "/work/a", "name": "n", "pid": 1, "since": 1}])
        source = switchboard_source()
        literal = re.search(r'"(heavy: refused: [^"]+)" in \(done\.stderr', source).group(1)
        self.assertTrue(text.startswith(literal), text)

    def state_for_marker(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_a_test_that_itself_exits_64_is_a_plain_failure_not_a_refusal(self):
        def runner(argv, **kwargs):
            return mock.Mock(returncode=64 if "heavy" in argv else 0, stdout="/r\n", stderr="usage error\n" if "heavy" in argv else "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            self.assertEqual(sb.cmd_preflight_pr(None, runner, time.time), 1)
        self.assertIn("FAIL cli-surface (", out.getvalue())
        self.assertIn("exit 64:", out.getvalue())
        self.assertNotIn("refused", out.getvalue())

    def test_preflight_reports_a_refused_sub_check_as_a_fail_that_says_so(self):
        def runner(argv, **kwargs):
            done = mock.Mock(returncode=64 if "heavy" in argv else 0, stdout="/r\n", stderr="heavy: refused: a full-suite job is already queued or running in this worktree (first, x, pid 1, since t)\n" if "heavy" in argv else "")
            return done
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            self.assertEqual(sb.cmd_preflight_pr(None, runner, time.time), 1)
        self.assertIn("FAIL cli-surface (", out.getvalue())
        self.assertIn("refused: a full-suite job is already queued or running", out.getvalue())

    def test_u2_the_refusal_reads_the_kind_from_the_one_classifier(self):
        self.hold(self.A)
        with mock.patch.object(self.hs, "classify", lambda command, short=False: "focused"):
            self.assertEqual(self.start([self.script], self.A)[0], 0)
        with mock.patch.object(self.hs, "classify", lambda command, short=False: "full"):
            self.assertEqual(self.start(["sh", "-c", "true"], self.A)[0], 64)

    def test_u1_a_run_that_is_not_refused_prints_nothing_new_and_a_full_house_still_exits_75(self):
        code, text = self.start([self.script], self.A)
        self.assertEqual((code, text), (0, ""))
        self.fill()
        code, text = self.start([self.script], self.A, wait_max=0)
        self.assertEqual(code, 75)
        self.assertTrue(text.startswith("heavy: no slot after 0 s (3 of 3 busy: "), text)
        self.assertNotIn("refused", text)

    def test_s7_the_only_ceilings_are_the_per_kind_table(self):
        self.assertEqual(self.hs.KIND_DEFAULTS, {"e2e": 3600, "focused": 1800, "full": 5400, "quick": 600, "other": 3600})
        names = sorted(name for name in vars(self.hs) if re.search(r"(CEIL|SECONDS|MAX_SECONDS|HARD_MAX)", name) and name.isupper())
        self.assertEqual(names, ["DEFAULT_MAX_SECONDS", "EXPRESS_SECONDS", "HARD_MAX_SECONDS", "KILL_GRACE_SECONDS", "POLL_SECONDS", "SWEEP_SECONDS", "WAIT_REPORT_SECONDS", "WATCH_SECONDS"])

    def test_the_worktree_is_the_git_toplevel_by_real_path(self):
        def repo(name):
            path = os.path.join(self.dir, name)
            os.makedirs(os.path.join(path, "sub"))
            subprocess.run(["git", "init", "-q", path], check=True)
            return path
        one, two = repo("one"), repo("two")
        link = os.path.join(self.dir, "link")
        os.symlink(one, link)
        top = os.path.realpath(one)
        self.assertEqual(self.hs.worktree_of(one), top)
        self.assertEqual(self.hs.worktree_of(os.path.join(one, "sub")), top)
        self.assertEqual(self.hs.worktree_of(os.path.join(link, "sub")), top)
        self.assertNotEqual(self.hs.worktree_of(two), top)
        self.assertIsNone(self.hs.worktree_of(os.path.join(self.dir, "state")))   # no git tree here (a temp dir): nothing is refused
        self.assertIsNone(self.hs.worktree_of(os.path.join(self.dir, "missing")))

    # ---- the words, once each, the same in every file
    def test_s2_s8_each_sentence_is_in_each_file_once_and_the_same_words(self):
        for path in self.FILES:
            text = " ".join((REPO / path).read_text().split())
            for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL):
                with self.subTest(path=path, sentence=sentence[:30]):
                    self.assertEqual(text.count(sentence), 1)

    def sentences_once(self, text):
        text = " ".join(text.split())
        return all(text.count(sentence) == 1 for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL))

    def test_a_mutant_that_drops_the_exception_or_words_it_differently_fails_the_same_check(self):
        for path in self.FILES:
            text = (REPO / path).read_text()
            self.assertTrue(self.sentences_once(text), path)
            for name, mutant in (("dropped", text.replace(self.EXCEPTION, "")), ("reworded", text.replace("to a test CI cannot run", "to a slow test")),
                                 ("doubled", text + "\n" + self.EXCEPTION)):
                with self.subTest(path=path, mutant=name):
                    self.assertFalse(self.sentences_once(mutant))

    def write_record(self, record):
        """A slot held by this test with `record` written WHOLE (truncated first); read back as the dict meant, before anything is asserted of it."""
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
        self.assertEqual(self.hs._read_info(fd), record)
        return fd

    def dead_pid(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        return dead.pid

    LIVE_CASES = (   # (id, record fields, refused?)
        ("both-dead", {"pid": "DEAD", "wrapper": "DEAD"}, False),
        ("command-alive", {"pid": "LIVE", "wrapper": "DEAD"}, True),
        ("wrapper-alive", {"pid": "DEAD", "wrapper": "LIVE"}, True),
        ("pid-reused-by-another-process", {"pid": "LIVE", "cstart": "Mon Jan  1 00:00:00 1990", "wrapper": "DEAD"}, False),
        ("wrapper-pid-reused", {"pid": "DEAD", "wrapper": "LIVE", "wstart": "Mon Jan  1 00:00:00 1990"}, False),
        ("pid-with-its-real-start", {"pid": "LIVE", "cstart": "REAL", "wrapper": "DEAD"}, True),
    )

    def case(self, fields):
        live, dead = os.getpid(), self.dead_pid()
        record = {"kind": "full", "worktree": self.A, "name": "ghost", "program": "x", "since": 1}
        for key, value in fields.items():
            record[key] = live if value == "LIVE" else dead if value == "DEAD" else self.hs._start_of(live) if value == "REAL" else value
        return record

    def test_a_held_lock_is_refused_only_for_a_holder_that_is_really_running(self):
        for name, fields, refused in self.LIVE_CASES:
            with self.subTest(case=name):
                self.reset()
                self.write_record(self.case(fields))
                self.assertEqual(self.hs.second_job_notice(self.state, self.A, []) is not None, refused)

    def test_mutants_of_the_liveness_rule_flip_a_case(self):
        flips = {}
        for label, patch in (("always-live", mock.patch.object(self.hs, "_pid_alive", lambda pid, start=None: True)),
                             ("pid-only", mock.patch.object(self.hs, "LIVE_KEYS", (("pid", ""), ("wrapper", "")))),
                             ("wrapper-half-dropped", mock.patch.object(self.hs, "LIVE_KEYS", (("pid", "cstart"),))),
                             ("command-half-dropped", mock.patch.object(self.hs, "LIVE_KEYS", (("wrapper", "wstart"),)))):
            with patch:
                flips[label] = []
                for name, fields, refused in self.LIVE_CASES:
                    self.reset()
                    self.write_record(self.case(fields))
                    if (self.hs.second_job_notice(self.state, self.A, []) is not None) != refused:
                        flips[label].append(name)
        self.assertEqual(flips, {"always-live": ["both-dead", "pid-reused-by-another-process", "wrapper-pid-reused"],
                                 "pid-only": ["pid-reused-by-another-process", "wrapper-pid-reused"],
                                 "wrapper-half-dropped": ["wrapper-alive"], "command-half-dropped": ["command-alive", "pid-with-its-real-start"]})

    def test_a_malformed_record_is_read_without_error(self):
        self.assertIsNone(self.hs.second_job_notice(self.state, self.A, [{"kind": "full", "worktree": "/elsewhere"}]))
        text = self.hs.second_job_notice(self.state, self.A, [{"kind": "full", "worktree": self.A, "name": "\x1b[31mq", "pid": "x", "since": "y"}])
        self.assertIn("pid unknown, since unknown time", text)
        self.assertNotIn("\x1b", text)
        fd = self.hs._take(self.state, 3)[0]
        self.fds.append(fd)
        os.ftruncate(fd, 0)
        os.pwrite(fd, b'{"kind": "full", "worktree"', 0)   # cut off: no record
        self.assertIsNone(self.hs.second_job_notice(self.state, self.A, []))

    def test_the_sentences_name_roles_never_agents_or_people(self):
        for sentence in (self.DEFAULT, self.EXCEPTION, self.REFUSAL):
            self.assertNotRegex(sentence, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")

    def test_s1_the_docs_state_the_measured_cause_before_the_default(self):
        text = " ".join((REPO / "docs" / "switchboard.md").read_text().split())
        section = text.split("**Focused by default, one full-suite job per worktree (#942).**", 1)[1]
        before = section.split(self.DEFAULT, 1)[0]
        for figure in ("19 full local runs", "14 worktrees", "Five of the 14 worktrees", "upper bound"):
            self.assertIn(figure, before)
        self.assertEqual(text.count("**Focused by default, one full-suite job per worktree (#942).**"), 1)

    # ---- preflight: THE table of workflow cases (name, workflow text, changed files, expected job or None)
    PR_ON = "on:\n  pull_request:\n  push:\n    branches: [main]\n"
    JOB = ("jobs:\n  test-cloud:\n    runs-on: ubuntu-latest\n    steps:\n      - name: launcher\n        if: ${{ !cancelled() }}\n        run: |\n"
           "          bin/test-cloud-launcher 2>&1 | tee out.txt\n")

    def cases(self):
        wf = self.PR_ON + self.JOB
        return (
            ("runs-on-pull-request", wf, ["cloud/x"], "test-cloud"),
            ("workflow-does-not-name-it", wf.replace("bin/test-cloud-launcher", "bin/test-other"), ["cloud/x"], None),
            ("workflow-unreadable", None, ["cloud/x"], None),
            ("branch-changes-the-workflow", wf, ["cloud/x", ".github/workflows/test.yml"], None),
            ("no-pull-request-trigger", wf.replace("pull_request:", "workflow_dispatch:"), ["cloud/x"], None),
            ("job-level-condition", wf.replace("    runs-on", "    if: github.ref == 'x'\n    runs-on"), ["cloud/x"], None),
            ("step-continue-on-error", wf.replace("        run: |", "        continue-on-error: true\n        run: |"), ["cloud/x"], None),
            ("step-condition-it-may-not-run", wf.replace("${{ !cancelled() }}", "${{ github.ref == 'x' }}"), ["cloud/x"], None),
            ("only-a-name-or-comment", wf.replace("          bin/test-cloud-launcher 2>&1 | tee out.txt", "          # bin/test-cloud-launcher\n          true"), ["cloud/x"], None),
            ("changed-files-unknown", wf, None, None),
            ("job-continue-on-error-true", wf.replace("    runs-on", "    continue-on-error: true\n    runs-on"), ["cloud/x"], None),
            ("job-continue-on-error-expression", wf.replace("    runs-on", "    continue-on-error: ${{ matrix.x }}\n    runs-on"), ["cloud/x"], None),
            ("job-continue-on-error-false-counts", wf.replace("    runs-on", "    continue-on-error: false\n    runs-on"), ["cloud/x"], "test-cloud"),
            ("step-continue-on-error-expression", wf.replace("        run: |", "        continue-on-error: ${{ true }}\n        run: |"), ["cloud/x"], None),
            ("run-only-echoes-the-name", wf.replace("          bin/test-cloud-launcher 2>&1 | tee out.txt", "          echo bin/test-cloud-launcher"), ["cloud/x"], None),
            ("single-line-run-echo", wf.replace("        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt", "        run: echo bin/test-cloud-launcher"), ["cloud/x"], None),
            ("single-line-run-counts", wf.replace("        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt", "        run: bin/test-cloud-launcher"), ["cloud/x"], "test-cloud"),
            ("failure-swallowed-with-or-true", wf.replace("tee out.txt", "tee out.txt || true"), ["cloud/x"], None),
            ("failure-swallowed-with-set-plus-e", wf.replace("          bin/test-cloud-launcher", "          set +e\n          bin/test-cloud-launcher"), ["cloud/x"], None),
            ("mentioned-after-another-command", wf.replace("          bin/test-cloud-launcher", "          true; bin/test-cloud-launcher"), ["cloud/x"], None),
            ("list-form-trigger", wf.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", "on: [push, pull_request]\n"), ["cloud/x"], "test-cloud"),
            ("trigger-with-paths-filter", wf.replace("  pull_request:\n", "  pull_request:\n    paths: ['docs/**']\n"), ["cloud/x"], None),
            ("trigger-with-branches-filter", wf.replace("  pull_request:\n", "  pull_request:\n    branches: [main]\n"), ["cloud/x"], None),
            ("pull-request-target-only", wf.replace("  pull_request:\n", "  pull_request_target:\n"), ["cloud/x"], None),
        )

    def preflight(self, workflow, changed):
        calls = []
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        if workflow is not None:
            os.makedirs(os.path.join(root, ".github", "workflows"))
            Path(root, ".github", "workflows", "test.yml").write_text(workflow)

        def runner(argv, **kwargs):
            calls.append(list(argv))
            done = mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                done.stdout = root + "\n"
            elif argv[:2] == ["git", "merge-base"]:
                done.stdout = "abc123\n" if changed is not None else ""
                done.returncode = 0 if changed is not None else 1
            elif argv[:2] == ["git", "diff"]:
                done.stdout = "\n".join(changed or [])
            return done
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sb, "preflight_load", lambda: (0.0, 4)):
            code = sb.cmd_preflight_pr(None, runner, time.time)
        self.assertEqual([c[:2] for c in calls if c[0] == "git"], [c[:2] for c in calls if c[0] == "git" and c[1] in ("rev-parse", "merge-base", "diff")])
        heavy = [" ".join(c[c.index("--") + 1:]) for c in calls if "heavy" in c]
        return code, out.getvalue(), heavy

    def test_s3_s4_every_workflow_case(self):
        for name, workflow, changed, job in self.cases():
            with self.subTest(case=name):
                code, out, heavy = self.preflight(workflow, changed)
                launcher = [c for c in heavy if "bin/test-cloud-launcher" in c]
                self.assertEqual(code, 0)
                if job:
                    self.assertEqual(launcher, [])
                    self.assertIn("preflight-pr: cloud launcher: skipped, CI runs it for this pull request (job %s)\n" % job, out)
                else:
                    self.assertEqual(len(launcher), 1)
                    self.assertNotIn("skipped", out)

    def test_every_job_level_key_that_can_hide_a_failure_is_refused_at_job_and_step_level(self):
        wf = self.PR_ON + self.JOB
        for key in sb.PREFLIGHT_JOB_KEYS_HIDING:
            value = "'x'" if key == "if" else "true"
            with self.subTest(key=key, level="job"):
                self.assertIsNone(sb.workflow_job_running(wf.replace("    runs-on", "    %s: %s\n    runs-on" % (key, value)), "bin/test-cloud-launcher"))
            with self.subTest(key=key, level="step"):
                text = wf.replace("        run: |", "        %s: %s\n        run: |" % (key, value)).replace("        if: ${{ !cancelled() }}\n", "", 1 if key == "if" else 0)
                self.assertIsNone(sb.workflow_job_running(text, "bin/test-cloud-launcher"))
        self.assertEqual(sb.workflow_job_running(wf, "bin/test-cloud-launcher"), "test-cloud")

    def test_a_mutant_that_forgets_a_job_level_key_skips_a_suite_ci_cannot_fail_on(self):
        wf = self.PR_ON + self.JOB.replace("    runs-on", "    continue-on-error: true\n    runs-on")
        with mock.patch.object(sb, "PREFLIGHT_JOB_KEYS_HIDING", ("if",)):
            self.assertEqual(sb.workflow_job_running(wf, "bin/test-cloud-launcher"), "test-cloud")   # the weakened rule would skip it
        self.assertIsNone(sb.workflow_job_running(wf, "bin/test-cloud-launcher"))

    def test_the_skip_leaves_the_other_cloud_suites_and_classes_as_they_were(self):
        code, out, heavy = self.preflight(self.PR_ON + self.JOB, ["cloud/x"])
        self.assertEqual([c for c in heavy if "cloud" in c], ["bin/test-cloud-worker", "cloud/tests/test-cloud"])
        self.assertEqual(len(heavy), 5)
        self.assertEqual([l.split()[:2] for l in out.splitlines() if l.startswith("PASS")], [["PASS", "cli-surface"], ["PASS", "hygiene"], ["PASS", "public-names"], ["PASS", "cloud"]])

    def test_every_sub_check_of_every_class_is_skipped_only_when_the_table_says_so(self):
        every = [" ".join(command) for _n, _c, commands, _u in sb.PREFLIGHT_CLASSES for command in commands]
        skippable = {command for command, _shown in sb.PREFLIGHT_CI_SKIPS}
        self.assertTrue(skippable <= set(every), "a skip row for a command no class runs")
        steps = "".join("      - run: %s\n" % command for command in every)
        workflow = self.PR_ON + "jobs:\n  everything:\n    steps:\n" + steps
        code, out, heavy = self.preflight(workflow, ["cloud/x"])
        for command in every:
            with self.subTest(command=command):
                self.assertEqual(command in heavy, command not in skippable)

    def test_a_mutant_that_always_runs_or_always_skips_the_sub_check_fails_a_case(self):
        with mock.patch.object(sb, "preflight_ci_runs", lambda *a, **k: None):
            self.assertIn("bin/test-cloud-launcher", self.preflight(self.PR_ON + self.JOB, ["cloud/x"])[2])
        with mock.patch.object(sb, "preflight_ci_runs", lambda *a, **k: "any-job"):
            self.assertNotIn("bin/test-cloud-launcher", self.preflight(None, ["cloud/x"])[2])


class TestBatchingDocs(unittest.TestCase):
    """#834 slice 1: the batching procedure, its body template and its owner decisions, each said once."""
    HEAD = "## Batching: the integration branch and the batch pull request (#834)"

    def section(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertEqual(text.count(self.HEAD), 1)
        return text.split(self.HEAD, 1)[1]  # the last section of the file; its template holds "## " lines, so no split on them

    def test_the_topics_are_each_a_heading_once(self):
        section = self.section()
        for topic in ("### What batches and what goes straight", "### The cadence and who keeps the batch", "### The freeze",
                      "### The sync", "### An ask whose change is only in `integration`", "### The steps a lane follows"):
            self.assertEqual(section.count(topic), 1, topic)

    def test_the_hold_rule_uses_the_matrix_terms_and_marks_the_delegation_a_target(self):
        section = self.section()
        for phrase in ("`owner_hold`", "Target, after #835 lands", "`delegated_clear`", "aggregated diff", "taken out of draft", "a planned follow-up (not built)",
                       "voids the owner's clear", "before the next change merges into `integration`", "delete head branch on merge"):
            self.assertEqual(section.count(phrase), 1, phrase)

    def test_the_batch_hold_sentence_is_in_the_rule_and_in_d1_and_in_the_skill(self):
        sentence = "until #835 lands, then only when a row is rated High"
        self.assertEqual(self.section().count(sentence), 2)  # the hold rule and decision D1
        self.assertEqual((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().count(sentence), 1)

    def test_no_label_and_no_existing_audit_report_are_claimed(self):
        section = self.section()
        self.assertNotIn("batch-ready", section)
        self.assertNotIn("batch-ready", (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text())
        self.assertNotIn("audit report", section)

    def test_the_lane_steps_are_numbered_and_each_carries_a_command(self):
        steps = self.section().split("### The steps a lane follows", 1)[1].split("###", 1)[0].strip().splitlines()
        numbered = [l for l in steps if l[:2] in ("1.", "2.", "3.", "4.", "5.", "6.")]
        self.assertEqual(len(numbered), 6)
        for line in numbered:
            self.assertIn("`", line)

    def test_the_skill_names_the_batching_rules_in_one_paragraph(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        paragraphs = [p for p in text.split("\n\n") if "`integration` branch" in p]
        self.assertEqual(len(paragraphs), 1)
        for word in ("batch pull request", "freeze", "held"):
            self.assertIn(word, paragraphs[0])

    def test_the_template_has_the_v2_sections_a_four_column_header_and_one_example_row(self):
        block = self.section().split("```markdown\n", 1)[1].split("```", 1)[0]
        for heading in ("## Why this needs you", "## What changes for you", "## Example", "## Questions for you"):
            self.assertEqual(block.count(heading), 1, heading)
        self.assertIn("Recommendation:", block)
        rows = [l for l in block.splitlines() if l.startswith("| Change |")]
        self.assertEqual(rows, ["| Change | Impact | Hold reasons and who cleared them | Evidence |"])
        after = block.split(rows[0], 1)[1].split("\n\n", 1)[0].strip().splitlines()
        self.assertEqual(len(after), 2)  # the separator and one example row
        self.assertEqual(len(after[1].strip().strip("|").split("|")), 4)

    def test_each_owner_decision_is_once_with_its_recommendation_or_decision(self):
        decisions = self.section().split("### Owner decisions and follow-up asks", 1)[1]
        for key, word in (("**D1**", "decided"), ("**D2**", "Recommendation"), ("**D3**", "Recommendation")):
            lines = [l for l in decisions.splitlines() if l.startswith("- " + key)]
            self.assertEqual(len(lines), 1, key)
            self.assertIn(word, lines[0])

    def test_the_three_prohibitions_are_said_once_each(self):
        section = self.section()
        for phrase in ("a force push", "a rebase of a shared branch", "an agent changing a ruleset"):
            self.assertEqual(section.count(phrase), 1, phrase)


class FakePullFiles:
    """GitHub's pull request answers for ONE pull request, no more generous than the real API: the files are served 100 a page whatever per_page asks, only the first 3000 are ever listed (a page past
    the end is empty), `changed_files` is the true count, and `gh pr view` lists 100 files at most with no source path for a rename. `faults` is {"pull" or page number: [fault, ...]}: one fault is used up
    by each call of that read, in order (a timeout, an HTTP 502, an answer that is not JSON, one over the size cap, an answer that is not a list). `heads` is what the pull request's head is at each read of it."""

    HEAD = "c" * 40
    LONG = "E" * 450 + " THE-END"   # an error text far over every old clip (80 to 600 characters), ending in a marker

    def __init__(self, files, number=9, heads=None, count=None):
        self.files, self.number = files, number
        self.count = len(files) if count is None else count
        self.faults, self.calls = {}, []
        self.heads = list(heads or [])

    @staticmethod
    def make(size, status="modified"):
        return [{"filename": "docs/f%04d.md" % i, "status": status, "sha": "e" * 40, "patch": "@@ -1 +1 @@\n-a\n+b", "changes": 2} for i in range(size)]

    def view(self):
        """what `gh pr view --json files,...` says: at most 100 files, a rename without its source"""
        shown = [{"path": f["filename"] if isinstance(f, dict) else "x", "additions": 1, "deletions": 1, "changeType": f["status"].upper() if isinstance(f, dict) else "MODIFIED"} for f in self.files[:100]]
        return dict({"headRefOid": self.HEAD, "baseRefOid": "b" * 40, "changedFiles": self.count, "files": shown}, **({"statusCheckRollup": self.rollup} if getattr(self, "rollup", None) else {}))   # `rollup` is set only by a clear that must show CI to its brief (#1215)

    def fault(self, key):
        queue = self.faults.get(key) or []
        return queue.pop(0) if queue else None

    def __call__(self, argv, **kwargs):
        path = argv[2]
        self.calls.append(path)
        route, _, query = path.partition("?")
        query = dict(q.split("=", 1) for q in query.split("&") if q)
        tail = route.split("/", 3)[3]
        if tail == "pulls/%d" % self.number:
            key, body = "pull", None
            head = (self.heads.pop(0) if self.heads else self.HEAD)
            body = {"head": {"sha": head}, "base": {"sha": "b" * 40, "ref": "main"}, "changed_files": self.count}
        elif tail == "pulls/%d/files" % self.number:
            page, size = int(query.get("page", "1")), min(int(query.get("per_page", "30")), 100)
            key, body = page, (self.files[:3000])[(page - 1) * size:page * size]
        elif tail.startswith("compare/"):
            key, body = "compare", {"merge_base_commit": {"sha": "b" * 40}}
        else:
            raise AssertionError("the fake GitHub has no answer for %s" % path)
        fault = self.fault(key)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        if fault == "longerror":
            return subprocess.CompletedProcess(argv, 1, "", "gh: " + FakePullFiles.LONG)
        if fault == "http502":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)")
        if fault in ("http403", "http404", "http422"):
            return subprocess.CompletedProcess(argv, 1, "", "gh: Error (HTTP %s)" % fault[4:])
        if fault == "bigbody":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)\n" + "B" * (1 << 20))
        if fault == "bodystatus":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Validation Failed (HTTP 422)\n{\"message\": \"see HTTP 404 and HTTP 502\"}")
        if fault == "notjson":
            return subprocess.CompletedProcess(argv, 0, "<html>bad gateway</html>", "")
        if fault == "toolarge":
            return subprocess.CompletedProcess(argv, 0, "[" + "0," * (sb.CARRY_ANSWER_MAX // 2 + 1) + "0]", "")
        if fault == "notlist":
            return subprocess.CompletedProcess(argv, 0, json.dumps(["x"] if key in ("pull", "compare") else {"message": "Server Error"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    def pages_asked(self, page):
        return len([c for c in self.calls if c.endswith("&page=%d" % page)])


class TestCloudWatchBounds(unittest.TestCase):
    """#1073 review: every entry kind and every bound of the cloud watch read has a test and a mutant (table in docs/switchboard.md, Cloud watch)."""
    SRC = (REPO / "bin" / "cloud_worker.py")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.dir = self.tmp / "cloud"
        self.dir.mkdir()
        self.cloud = sb.load_module("cloud_worker").bind(sb.__dict__)

    def module(self, old=None, new=None):
        text = self.SRC.read_text()
        if old is not None:
            self.assertIn(old, text)
            text = text.replace(old, new)
        mod = types.ModuleType("cloud_mutant")
        mod.__file__ = str(self.SRC)
        sys.modules["cloud_mutant"] = mod
        self.addCleanup(sys.modules.pop, "cloud_mutant", None)
        exec(compile(text, str(self.SRC), "exec"), mod.__dict__)
        return mod.bind(sb.__dict__)

    def guarded(self, fn, seconds=2):
        import signal
        def hung(*a):
            raise AssertionError("the read hung")
        old = signal.signal(signal.SIGALRM, hung)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def read(self, mod=None, **kw):
        mod = mod or self.cloud
        return self.guarded(lambda: mod.read_records_bounded(self.tmp, **kw))

    def put(self, name, data=None):
        (self.dir / name).write_bytes(json.dumps({"w": 1}).encode() if data is None else data)

    # each kind: (check on the real module, mutant text change that must make the check fail)
    def kind_cases(self):
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        def fifo():
            os.mkfifo(str(self.dir / "x.json"))
        def sym():
            os.symlink(str(outside), str(self.dir / "x.json"))
        def adir():
            (self.dir / "x.json").mkdir()
        return {
            "bad_name": (lambda: self.put("bad name!.json"), None, None),
            "directory": (adir, None, None),
            "not_regular": (fifo, "if not stat.S_ISREG(os.fstat(fd).st_mode):", "if False:"),
            "fifo": (fifo, "os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW", "os.O_RDONLY | os.O_NOFOLLOW"),
            "symlink": (sym, "os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW", "os.O_RDONLY | os.O_NONBLOCK"),
            "too_large": (lambda: self.put("x.json", b"{\"a\": \"" + b"a" * 70000 + b"\"}"), "if len(raw) > max_bytes:", "if False:"),
            "not_utf8": (lambda: self.put("x.json", b"\xff\xfe{}"), "except UnicodeDecodeError:", "except ZeroDivisionError:"),
            "not_json": (lambda: self.put("x.json", b"{nope"), "except ValueError:\n        return None, \"not_json\"", "except ValueError:\n        return None, \"too_deep\""),
            "wrong_shape": (lambda: self.put("x.json", b"[1]"), "if not isinstance(record, dict):", "if False:"),
            "too_deep": (lambda: self.put("x.json", b"[" * 30000 + b"]" * 30000), None, None),   # mutants: test_depth_* below
        }

    def test_each_kind_is_skipped_and_counted_under_its_own_name(self):
        for kind, (setup, _old, _new) in self.kind_cases().items():
            with self.subTest(kind=kind):
                for f in list(self.dir.iterdir()):
                    shutil.rmtree(str(f)) if f.is_dir() and not f.is_symlink() else f.unlink()
                setup()
                got = self.read()
                want = {"fifo": "not_regular", "directory": "not_regular"}.get(kind, kind)
                self.assertEqual(got["records"], [])
                self.assertEqual(got["skips"][want], 1, got)
                self.assertEqual(got["skipped"], 1)

    def test_each_kind_has_a_mutant_that_fails_its_check(self):
        for kind, (setup, old, new) in self.kind_cases().items():
            if old is None:
                continue
            with self.subTest(kind=kind):
                for f in list(self.dir.iterdir()):
                    shutil.rmtree(str(f)) if f.is_dir() and not f.is_symlink() else f.unlink()
                setup()
                want = {"fifo": "not_regular", "directory": "not_regular"}.get(kind, kind)
                try:
                    got = self.read(self.module(old, new))
                except (AssertionError, Exception):
                    continue   # the mutant hung or raised: caught
                self.assertNotEqual((got["records"], got["skips"][want]), ([], 1), "the mutant of %s survived" % kind)

    # ---- #1073 repair of #1155: a Python-independent depth limit (the scan never reads the interpreter's recursion limit)
    def nested(self, n):
        return "".join('{"a":' for _ in range(n)) + "1" + "}" * n

    def test_depth_exactly_at_the_limit_parses_and_one_over_is_too_deep(self):
        limit = self.cloud.WATCH_MAX_DEPTH
        self.assertEqual(limit, 64)
        self.put("x.json", self.nested(limit).encode())
        got = self.read()
        self.assertEqual((len(got["records"]), got["skipped"]), (1, 0))
        for f in self.dir.iterdir():
            f.unlink()
        self.put("x.json", self.nested(limit + 1).encode())
        got = self.read()
        self.assertEqual((got["records"], got["skips"]["too_deep"]), ([], 1))

    def test_brackets_in_strings_and_escaped_quotes_do_not_count(self):
        c = self.cloud
        self.assertFalse(c.nesting_too_deep(json.dumps({"a": "[" * 500 + "{" * 500})))
        self.assertFalse(c.nesting_too_deep(json.dumps({"a": 'x\\" ' + "[" * 500})))
        self.assertFalse(c.nesting_too_deep('{"a": "\\\\", "b": "[[[[[[[[[["}', 2))
        self.assertTrue(c.nesting_too_deep("[" * 65))
        self.assertTrue(c.nesting_too_deep('["\\"", ' + "[" * 70))

    def test_all_bracket_file_of_64_kib_is_too_deep_and_fast(self):
        import time
        self.put("x.json", b"[" * 65536)
        start = time.monotonic()
        got = self.read()
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(got["skips"]["too_deep"], 1)
        start = time.monotonic()
        self.assertTrue(self.cloud.nesting_too_deep("[" * 65536))
        self.assertLess(time.monotonic() - start, 1.0)

    def reader_checks(self):
        deep = self.nested(65)
        def watch(mod):
            self.put("x.json", deep.encode())
            return mod.read_records_bounded(self.tmp)["skips"]["too_deep"] == 1
        def shared(mod):
            f = self.dir / "y.json"
            f.write_text(deep)
            return mod.read_record_file(str(f), 65536) == (None, "too_deep")
        def load(mod):
            (self.dir / "z.json").write_text(deep)
            os.chmod(str(self.dir / "z.json"), 0o600)
            try:
                mod.load_record(self.tmp, "z")
            except mod.CloudError as err:
                return "nested deeper" in str(err)
            return False
        return {
            "read_records_bounded (_read_one)": (watch, "        record = parse_bounded(text)\n    except TooDeep:\n        return None, \"too_deep\"\n    except ValueError:\n        return None, \"not_json\"\n    if not isinstance(record, dict):\n        return None, \"wrong_shape\"\n    return record, None\n\n\ndef read_records_bounded", None),
            "read_record_file": (shared, None, None),
            "load_record": (load, "record = parse_bounded(_read_private(path, RECORD_MAX))", "record = json.loads(_read_private(path, RECORD_MAX))"),
        }

    def test_every_reader_refuses_too_deep_with_its_own_kind(self):
        for name, (check, _o, _n) in self.reader_checks().items():
            with self.subTest(reader=name):
                self.assertTrue(check(self.cloud))

    def test_a_limit_of_a_billion_fails_every_reader_on_every_python(self):
        mutant = self.module("WATCH_MAX_DEPTH = 64 ", "WATCH_MAX_DEPTH = 10**9 ")
        for name, (check, _o, _n) in self.reader_checks().items():
            with self.subTest(reader=name):
                for f in list(self.dir.iterdir()):
                    f.unlink()
                try:
                    self.assertFalse(check(mutant))
                except Exception:
                    pass

    def test_each_reader_mutant_that_drops_the_bounded_parse_is_caught(self):
        """Per-reader mutant: that reader parses with a plain json.loads and a 65-deep record is no longer refused."""
        checks = self.reader_checks()
        text = self.SRC.read_text()
        # one mutant per reader: swap that reader's parse_bounded call for json.loads (the first, second, third call site in file order)
        sites = [i for i in range(len(text)) if text.startswith("parse_bounded(", i) and not text.startswith("parse_bounded(text):", i)]
        self.assertEqual(len(sites), 3)
        for site, key in zip(sites, ("read_record_file", "load_record", "read_records_bounded (_read_one)")):
            with self.subTest(reader=key):
                mutated = text[:site] + "json.loads(" + text[site + len("parse_bounded("):]
                mod = types.ModuleType("cloud_mutant")
                mod.__file__ = str(self.SRC)
                sys.modules["cloud_mutant"] = mod
                self.addCleanup(sys.modules.pop, "cloud_mutant", None)
                exec(compile(mutated, str(self.SRC), "exec"), mod.__dict__)
                for f in list(self.dir.iterdir()):
                    f.unlink()
                check = checks[key][0]
                try:
                    ok = check(mod.bind(sb.__dict__))
                except Exception:
                    ok = False
                self.assertFalse(ok, "the mutant of %s survived" % key)

    def test_every_untrusted_record_parse_is_bounded_and_listed_in_the_docs(self):
        """Completeness: every json.loads in cloud_worker.py of a record file goes through parse_bounded, and the docs table names each reader."""
        text = self.SRC.read_text()
        loads = [l.strip() for l in text.splitlines() if "json.loads(" in l and not l.lstrip().startswith("#")]
        self.assertEqual(loads, ["return json.loads(text)", "body = json.loads(out) if (out or \"\").strip() else None"])
        import re
        readers = sorted(set(re.findall(r"^def (\w+)\(", text, re.M)) & {"read_record_file", "_read_one", "load_record"})
        sites = text.count("parse_bounded(")
        self.assertEqual((len(readers), sites), (3, 4))   # 3 call sites + the definition
        docs = (REPO / "docs" / "switchboard.md").read_text()
        section = docs[docs.index("## Cloud watch"):docs.index("## The assembler")]
        for name in readers + ["live_cloud_by_lane", "WATCH_MAX_DEPTH", "parse_bounded"]:
            self.assertIn(name, section)

    def test_mutant_plain_open_hangs_on_a_fifo(self):
        os.mkfifo(str(self.dir / "x.json"))
        plain = self.module("fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)", "fd = os.open(path, os.O_RDONLY)")
        with self.assertRaises(AssertionError):
            self.read(plain)
        self.assertEqual(self.read()["skips"]["not_regular"], 1)

    def test_files_examined_bound_with_more_bad_files_than_the_bound(self):
        for i in range(self.cloud.WATCH_MAX_FILES + 30):
            self.put("b%04d.json" % i, b"{nope")
        got = self.read()
        self.assertEqual(got["skipped"], self.cloud.WATCH_MAX_FILES)
        self.assertEqual(got["bound"], "files")
        mutant = self.module("elif examined >= max_files:", "elif False:")
        self.assertGreater(self.read(mutant)["skipped"], self.cloud.WATCH_MAX_FILES)

    def test_record_bound_names_records(self):
        for i in range(self.cloud.WATCH_MAX_RECORDS + 3):
            self.put("w%03d.json" % i)
        got = self.read()
        self.assertEqual((len(got["records"]), got["bound"]), (self.cloud.WATCH_MAX_RECORDS, "records"))
        mutant = self.module("if len(out[\"records\"]) >= max_records:", "if False:")
        self.assertGreater(len(self.read(mutant)["records"]), self.cloud.WATCH_MAX_RECORDS)

    def test_time_bound_stops_and_says_so(self):
        for i in range(10):
            self.put("w%d.json" % i)
        ticks = iter(range(0, 1000))
        got = self.read(clock=lambda: next(ticks) * 0.5, seconds=1.0)
        self.assertEqual(got["bound"], "time")
        self.assertLess(len(got["records"]), 10)
        mutant = self.module("elif clock() > deadline:", "elif False:")
        ticks = iter(range(0, 1000))
        self.assertEqual(len(self.read(mutant, clock=lambda: next(ticks) * 0.5, seconds=1.0)["records"]), 10)

    def test_rotation_gives_every_record_a_turn(self):
        for i in range(8):
            self.put("w%d.json" % i)
        seen, cursor = set(), None
        for _ in range(4):
            got = self.read(max_records=2, cursor=cursor)
            cursor = got["cursor"]
            seen |= {r["w"] for r in got["records"]}
            self.assertEqual(len(got["records"]), 2)
        names = set()
        cursor = None
        for _ in range(4):
            got = self.read(max_records=2, cursor=cursor)
            cursor = got["cursor"]
            names.add(cursor)
        self.assertEqual(len(names), 4)
        mutant = self.module("after = [n for n in names if n > cursor]", "after = []")
        cursor, last = None, set()
        for _ in range(4):
            got = self.read(mutant, max_records=2, cursor=cursor)
            cursor = got["cursor"]
            last.add(cursor)
        self.assertEqual(len(last), 1)

    def test_tick_makes_no_write_process_network_or_remove_call_and_audits_the_bound(self):
        for i in range(self.cloud.WATCH_MAX_FILES + 2):
            self.put("b%04d.json" % i, b"{nope")
        audit = []
        engine = types.SimpleNamespace(audit=types.SimpleNamespace(append=lambda *a: audit.append(a)), actor="daemon")
        def boom(*a, **k):
            raise AssertionError("the tick made a forbidden call")
        import socket
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "st")}):
            (sb.state_dir_path() / "cloud").mkdir(parents=True)
            for f in self.dir.iterdir():
                shutil.copy(str(f), str(sb.state_dir_path() / "cloud" / f.name))
            with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), mock.patch.object(os, "remove", boom), \
                    mock.patch.object(os, "unlink", boom), mock.patch.object(os, "rename", boom), mock.patch.object(os, "write", boom), \
                    mock.patch.object(socket, "socket", boom), mock.patch("builtins.open", boom):
                sb.cloud_watch_tick(engine, None)
                sb.cloud_watch_tick(engine, None)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][1], "cloud-watch.capped")
        self.assertEqual(audit[0][3]["bound"], "files")
        self.assertEqual(audit[0][3]["skipped"], {"not_json": self.cloud.WATCH_MAX_FILES})

    def test_mutants_write_call_and_no_audit_are_caught(self):
        # a tick that writes or never audits fails the recording test above: prove the audit one by a source mutant
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertIn('engine.audit.append(engine.actor, "cloud-watch.capped"', src)
        self.assertNotIn("os.unlink", src[src.index("def cloud_watch_tick"):src.index("def finish_pass")])
        self.assertNotIn("subprocess", src[src.index("def cloud_watch_tick"):src.index("def finish_pass")])


class TestArchTools(unittest.TestCase):
    """The two move tools (S0 of #1092): `arch-split-check` reads the surface of the daemon as its own entry assembles it, and `arch-part-cut` never overwrites a part."""

    def tool(self, name, *args):
        return subprocess.run([sys.executable, "-B", str(REPO / "tools" / name)] + [str(a) for a in args], capture_output=True, text=True)

    def test_the_surface_of_the_daemon_is_read_as_its_entry_assembles_it_not_as_a_text_join(self):
        listed = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.lstrip().startswith("#")]
        files = [str(BIN / n) for n in listed]
        out = Path(tempfile.mkdtemp()) / "surface.txt"
        self.addCleanup(shutil.rmtree, str(out.parent), True)
        done = self.tool("arch-split-check", "--write-surface", out, "--after", *files)
        self.assertEqual(done.returncode, 0, done.stderr[-600:])
        self.assertEqual(out.read_text().split(), sorted(dir(sb)), "the names the tool reads are the names of the daemon as it loads")

    def test_a_cut_refuses_a_part_name_already_listed_and_a_name_given_twice_and_writes_nothing(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "switchboard").mkdir()
        entry = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
        (root / "entry").write_text(entry)
        (root / "entry.parts").write_text("# parts\nentry\nswitchboard/taken.py\n")
        (root / "switchboard" / "taken.py").write_text("KEEP = 1\n")
        common = ["--entry", root / "entry", "--manifest", root / "entry.parts", "--parts-dir", root / "switchboard", "--write"]
        for cuts, word in ((["taken:1:2"], "already exists"), (["fresh:1:2", "fresh:5:6"], "given twice")):
            with self.subTest(cuts=cuts):
                done = self.tool("arch-part-cut", *common, *cuts)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(word, done.stderr)
                self.assertEqual((root / "entry").read_text(), entry, "the entry is untouched")
                self.assertEqual((root / "switchboard" / "taken.py").read_text(), "KEEP = 1\n", "the listed part is not overwritten")
                self.assertFalse((root / "switchboard" / "fresh.py").exists())
        done = self.tool("arch-part-cut", *common, "fresh:1:2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((root / "switchboard" / "fresh.py").read_text(), "def one():\n    return 1\n")
        self.assertIn("switchboard/fresh.py", (root / "entry.parts").read_text())

    @staticmethod
    def snapshot(root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}

    def cut_tree(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "switchboard").mkdir()
        (root / "out").mkdir()
        (root / "entry").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        (root / "entry.parts").write_text("# parts\nentry\nswitchboard/taken.py\n")
        (root / "switchboard" / "taken.py").write_text("KEEP = 1\n")
        (root / "switchboard" / "Mixed.py").write_text("MIXED = 1\n")   # differs from a new name only in case
        (root / "switchboard" / "exists.py").write_text("EXISTS = 1\n")   # on disk, not in the manifest
        return root, ["--entry", root / "entry", "--manifest", root / "entry.parts", "--parts-dir", root / "switchboard", "--write"]

    def test_a_cut_refuses_a_traversal_an_outside_name_an_existing_file_and_a_bad_range_and_writes_nothing(self):
        root, common = self.cut_tree()
        before = self.snapshot(root)
        refused = (
            ("a path traversal", ["../out/esc:1:2"], "lower-case letters"),
            ("an absolute name", ["/tmp/escape:1:2"], "lower-case letters"),
            ("a name with a directory", ["sub/x:1:2"], "lower-case letters"),
            ("a capital", ["Part:1:2"], "lower-case letters"),
            ("a name that ends in a newline", ["fresh\n:1:2"], "lower-case letters"),
            ("a name that is only a newline", ["\n:1:2"], "lower-case letters"),
            ("a name with a leading digit", ["1fresh:1:2"], "lower-case letters"),
            ("a lookalike name", ["fresh\u200b:1:2"], "lower-case letters"),
            ("a name that matches an existing file but for case", ["mixed:1:2"], "already exists"),
            ("an existing file that is not listed", ["exists:1:2"], "already exists"),
            ("a listed part", ["taken:1:2"], "already exists"),
            ("a range outside the entry", ["fresh:1:99"], "not inside the entry"),
            ("a reversed range", ["fresh:6:1"], "not inside the entry"),
            ("a statement cut in two", ["fresh:1:1"], "cut by"),
            ("two cuts that overlap", ["a:1:2", "b:2:2"], "overlap"),
            ("a valid cut then an existing file: the first is not written", ["fresh:1:2", "exists:5:6"], "already exists"),
            ("a valid cut then a traversal: the first is not written", ["fresh:1:2", "../out/esc:5:6"], "lower-case letters"),
            ("a spec that is not NAME:FIRST:LAST", ["fresh"], "not NAME:FIRST:LAST"),
        )
        for label, cuts, word in refused:
            with self.subTest(case=label):
                done = self.tool("arch-part-cut", *common, *cuts)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn("nothing written", done.stderr)
                self.assertIn(word, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(self.snapshot(root), before, "no file changed, no file appeared: the entry, the manifest, the parts and the directory outside")

    def test_a_symlinked_or_outside_parts_directory_is_refused_and_nothing_is_written(self):
        root, common = self.cut_tree()
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(outside), True)
        (root / "link").symlink_to(outside)
        before, outside_before = self.snapshot(root), self.snapshot(outside)
        for label, parts in (("a symlinked directory", root / "link"), ("a directory outside the manifest's", outside)):
            with self.subTest(case=label):
                args = [a for a in common]
                args[args.index("--parts-dir") + 1] = parts
                done = self.tool("arch-part-cut", *args, "fresh:1:2")
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn("nothing written", done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(self.snapshot(root), before)
                self.assertEqual(self.snapshot(outside), outside_before)

    def test_a_cut_that_would_leave_a_decorator_behind_is_refused_and_one_that_takes_it_is_not(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "switchboard").mkdir()
        entry = "import functools\n\n\n@functools.lru_cache(None)\ndef cached():\n    return 1\n\n\ndef plain():\n    return 2\n"
        (root / "entry").write_text(entry)
        (root / "entry.parts").write_text("entry\n")
        common = ["--entry", root / "entry", "--manifest", root / "entry.parts", "--parts-dir", root / "switchboard", "--write"]
        before = self.snapshot(root)
        done = self.tool("arch-part-cut", *common, "fresh:5:6")   # from the def line: the decorator on line 4 stays behind
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("cut by", done.stderr)
        self.assertEqual(self.snapshot(root), before)
        done = self.tool("arch-part-cut", *common, "fresh:4:6")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((root / "switchboard" / "fresh.py").read_text(), "@functools.lru_cache(None)\ndef cached():\n    return 1\n")

    def test_a_part_that_does_not_parse_is_a_named_failure_in_the_statement_comparison(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "good.py").write_text("A = 1\n")
        (root / "bad.py").write_text("def broken(:\n")
        done = self.split_check([root / "good.py"], [root / "bad.py"], "--no-surface")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertIn("bad.py does not parse", done.stderr)
        missing = self.split_check([root / "good.py"], [root / "absent.py"], "--no-surface")
        self.assertNotIn("Traceback", missing.stdout + missing.stderr)
        self.assertIn("cannot read", missing.stderr)

    def test_a_cut_writes_only_inside_the_parts_directory_and_lists_the_part_relative_to_the_manifest(self):
        root, common = self.cut_tree()
        done = self.tool("arch-part-cut", *common, "fresh:1:2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.snapshot(root)), ["entry", "entry.parts", "switchboard/Mixed.py", "switchboard/exists.py", "switchboard/fresh.py", "switchboard/taken.py"])
        self.assertEqual((root / "entry.parts").read_text().splitlines()[-1], "switchboard/fresh.py")
        self.assertEqual(list((root / "out").iterdir()), [])

    def split_check(self, before, after, *flags):
        return self.tool("arch-split-check", "--before", *before, "--after", *after, *flags)

    def test_a_side_that_cannot_be_loaded_is_a_named_failure_after_the_statement_differences_not_a_traceback(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "a.py").write_text("A = 1\n")
        (root / "b.py").write_text("B = 2\n")
        (root / "b_bad.py").write_text("B = undefined_name_here\n")
        done = self.split_check([root / "a.py", root / "b.py"], [root / "a.py", root / "b_bad.py"])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertIn("statement removed or changed (before): b.py:1 Assign B", done.stdout)   # the differences come first and name the part
        self.assertIn("statement added or changed (after): b_bad.py:1 Assign B", done.stdout)
        self.assertIn("after: cannot load the program (in b_bad.py:1): NameError", done.stdout)
        self.assertLess(done.stdout.index("statement removed"), done.stdout.index("after: cannot load"))
        self.assertIn("surface not read", done.stdout)

    def entry_tree(self, with_manifest=True):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        for sibling in BIN.glob("*.py"):
            shutil.copy(str(sibling), str(root / sibling.name))
        if with_manifest:
            copy_entry(root)
        else:
            shutil.copy(str(SWITCHBOARD), str(root / "fleet-switchboard"))
        return root

    def test_a_part_of_the_daemon_that_uses_an_undefined_name_at_load_time_is_named_by_part_and_side(self):
        root = self.entry_tree()
        part = root / "switchboard" / "judge.py"
        part.write_text(part.read_text() + "\nLOAD_TIME_USE = an_undefined_name_at_load_time\n")
        listed = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.lstrip().startswith("#")]
        done = self.split_check([BIN / n for n in listed], [root / n for n in listed])
        self.assertEqual(done.returncode, 1, done.stdout[-400:] + done.stderr[-400:])
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertIn("statement added or changed (after): judge.py", done.stdout)
        self.assertIn("after: cannot load the program (in judge.py:", done.stdout)
        self.assertIn("NameError", done.stdout)

    def test_an_entry_with_no_manifest_beside_it_is_a_named_failure_that_says_how_to_lay_it_out(self):
        lone = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(lone), True)
        shutil.copy(str(SWITCHBOARD), str(lone / "fleet-switchboard"))   # the entry alone, as `git show REV:bin/fleet-switchboard > tmp` leaves it
        done = self.split_check([lone / "fleet-switchboard"], [lone / "fleet-switchboard"])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertIn("before: cannot load the program", done.stdout)
        self.assertIn("fleet-switchboard.parts", done.stdout)

    def test_a_file_given_that_is_not_in_the_manifest_beside_the_entry_is_refused_by_name(self):
        root = self.entry_tree()
        extra = root / "extra_part.py"
        extra.write_text("EXTRA = 1\n")
        listed = [n.strip() for n in (BIN / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.lstrip().startswith("#")]
        done = self.split_check([root / n for n in listed], [root / n for n in listed] + [extra])
        self.assertEqual(done.returncode, 1, done.stdout[-300:])
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertIn("after: extra_part.py is not in", done.stdout)
        self.assertIn("would not reach the surface", done.stdout)

    def test_a_hostile_pair_one_statement_dropped_and_one_added_is_named_by_the_statements_and_by_the_surface(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "before.py").write_text("KEEP = 1\n\n\ndef dropped():\n    return 1\n")
        (root / "after.py").write_text("KEEP = 1\n\n\ndef added():\n    return 1\n")   # the same number of statements: only the multiset differs
        done = self.split_check([root / "before.py"], [root / "after.py"])
        self.assertEqual(done.returncode, 1, done.stdout)
        for line in ("statement removed or changed (before): before.py:4 FunctionDef dropped", "statement added or changed (after): after.py:4 FunctionDef added",
                     "name only before: dropped", "name only after: added"):
            self.assertIn(line, done.stdout)
        same = self.split_check([root / "before.py"], [root / "before.py"])
        self.assertEqual(same.returncode, 0, same.stdout)

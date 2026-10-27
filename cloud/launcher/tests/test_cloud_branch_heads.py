#!/usr/bin/env python3
"""A lane is told when its cloud worker's branch gets a new head (ask #1407).

Offline: a fake remote answers the daemon's ref reads; the real `cloud_branch_tick` runs over real record files. No network, no real credential.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SWITCHBOARD = os.path.join(KIT, "bin", "fl" + "eet-switchboard")
loader = importlib.machinery.SourceFileLoader("cw_branch_sb", SWITCHBOARD)
spec = importlib.util.spec_from_loader("cw_branch_sb", loader)
sb = importlib.util.module_from_spec(spec)
sys.modules["cw_branch_sb"] = sb
loader.exec_module(sb)
CLOUD = sb.load_module("cloud_worker").bind(vars(sb))

REPO = "acme-org/widget-service"
NOW = 1_800_000_000.0
SECRET = "synthetic-token-value"


def sha(n):
    return ("%02x" % n) * 20


class Remote:
    """The fake remote: {branch: head} (a missing key is a 404, "down" an unreadable remote). Counts each read; the clock advances per read."""

    def __init__(self, clock):
        self.heads, self.reads, self.down, self.tick, self.clock = {}, [], False, 0.0, clock

    def __call__(self, argv, **kw):
        path = argv[-1]
        self.reads.append(path)
        self.clock.t += self.tick
        branch = path.split("/git/ref/heads/", 1)[1]
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "dial tcp: connection refused")
        if branch not in self.heads:
            return subprocess.CompletedProcess(argv, 1, 'HTTP/2.0 404 Not Found\r\n\r\n{"message":"Not Found"}', "gh: Not Found (HTTP 404)")
        body = json.dumps({"object": {"sha": self.heads[branch]}})
        return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body, "")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Audit:
    def __init__(self):
        self.lines = []

    def append(self, actor, name, subject, detail=None):
        self.lines.append((name, detail))

    def named(self, name):
        return [d for n, d in self.lines if n == name]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name).resolve()
        (self.state / "cloud").mkdir()
        self.clock = Clock()
        self.remote = Remote(self.clock)
        self.audit = Audit()
        self.accounts = sb.GhAccounts({"gh": "gh", "call_timeout_seconds": 5, "gh_users": {}}, self.remote, lambda: NOW, environ={})
        self.accounts.conditional = True   # as the daemon runs it
        self.discovery = SimpleNamespace(problems=[], agents={"lane-a": SimpleNamespace(fleet={"role": "orchestrator"})})
        self.engine = SimpleNamespace(state_dir=self.state, accounts=self.accounts, audit=self.audit, actor="daemon", now=lambda: NOW,
                                      branch_clock=self.clock, cloud_watch=None)

    def record(self, name, branch=None, boss="lane-a", **extra):
        body = {"worker": name, "url": "https://pod.example.internal/" + name, "runtime_id": "rt-" + name, "hard_deadline": "2099-01-01T00:00:00Z",
                "repo": REPO, "branch": branch or "work/" + name, "reports_to": boss}
        body.update(extra)
        (self.state / "cloud" / (name + ".json")).write_text(json.dumps(body))

    def run_pass(self):
        self.engine.cloud_watch = CLOUD.read_records_bounded(self.state)
        return sb.cloud_branch_tick(self.engine, self.discovery)

    def messages(self):
        return [r["text"] if "text" in r else r for r in sb.read_reminders(self.state)]

    def message_texts(self):
        return [str(m.get("text") or m.get("body") or m) for m in sb.read_reminders(self.state)]

    def usage(self, remaining, limit=5000, at=NOW):
        (self.state / "github-usage.json").write_text(json.dumps({"at": at, "accounts": {"default": {"remaining": remaining, "limit": limit}}}))


class TestPush(Base):
    def test_s2_three_moved_branches_give_three_reads_and_three_messages(self):
        for n in range(3):
            self.record("pod%d" % n)
            self.remote.heads["work/pod%d" % n] = sha(n + 1)
        self.run_pass()                       # the first heads: recorded as the baseline
        for n in range(3):
            self.remote.heads["work/pod%d" % n] = sha(n + 10)
        self.remote.reads.clear()
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 3)
        texts = sorted(self.message_texts())
        self.assertEqual(len(texts), 3)
        for n, text in enumerate(texts):
            self.assertIn("pod pod%d pushed work/pod%d head %s" % (n, n, sha(n + 10)[:8]), text)
            self.assertNotIn(sha(n + 10), text)
            self.assertNotIn(SECRET, text)

    def test_s2_twenty_five_branches_are_cut_at_twenty_reads_with_one_audit_line(self):
        for n in range(25):
            self.record("p%02d" % n)
            self.remote.heads["work/p%02d" % n] = sha(n + 1)
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 20)
        self.assertEqual(len(self.audit.named("cloud-branch.capped")), 1)
        self.remote.reads.clear()
        self.run_pass()                       # the carried branches are read next: round robin
        self.assertEqual(len(self.remote.reads), 20)
        first_ten = {r for r in self.remote.reads}
        self.assertTrue(any(r.endswith("work/p24") for r in first_ten))

    def test_s2_a_remote_slower_than_five_seconds_stops_the_pass(self):
        for n in range(6):
            self.record("s%d" % n)
            self.remote.heads["work/s%d" % n] = sha(n + 1)
        self.remote.tick = 3.0
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 2)   # 3 s, 6 s: past the 5 s of the pass
        self.assertEqual(self.audit.named("cloud-branch.capped")[0]["bound"], "time")

    def test_s3_first_head_equal_to_the_launch_head_is_not_told_and_the_same_head_again_is_not_told(self):
        self.record("one", launch_head=sha(5))
        self.remote.heads["work/one"] = sha(5)
        self.run_pass()
        self.assertEqual(self.message_texts(), [])
        self.remote.heads["work/one"] = sha(6)
        self.run_pass()
        self.assertEqual(len(self.message_texts()), 1)
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.message_texts()), 1)

    def test_s3_a_first_head_that_differs_from_the_launch_head_is_told(self):
        self.record("one", launch_head=sha(5))
        self.remote.heads["work/one"] = sha(6)
        self.run_pass()
        self.assertEqual(len(self.message_texts()), 1)

    def test_s3_a_missing_branch_gives_no_message_and_no_error_and_its_appearance_is_told(self):
        self.record("one")
        self.run_pass()
        self.assertEqual(self.message_texts(), [])
        self.assertEqual(self.audit.lines, [])
        self.remote.heads["work/one"] = sha(7)
        self.run_pass()
        self.assertEqual(len(self.message_texts()), 1)

    def test_s3_an_unreadable_remote_gives_one_audit_line_per_pass_and_no_message(self):
        for n in range(3):
            self.record("d%d" % n)
        self.remote.down = True
        self.run_pass()
        self.assertEqual(len(self.audit.named("cloud-branch.unreadable")), 1)
        self.assertEqual(self.message_texts(), [])
        self.run_pass()
        self.assertEqual(len(self.audit.named("cloud-branch.unreadable")), 2)

    def test_s4_another_lane_and_a_retired_or_cleared_record_are_never_read(self):
        self.record("mine")
        self.record("theirs", boss="lane-b")
        self.record("retired", retired=True)
        self.record("gone")
        (self.state / "cloud" / "gone.json").unlink()
        for branch in ("mine", "theirs", "retired", "gone"):
            self.remote.heads["work/" + branch] = sha(3)
        self.run_pass()
        self.assertEqual([r.rsplit("/", 1)[1] for r in self.remote.reads], ["mine"])

    def test_s5_a_mutant_that_tells_on_every_pass_fails_the_same_head_test(self):
        source = Path(SWITCHBOARD).read_text()
        needle = '        if entry.get("head") == head:\n            continue\n'
        self.assertEqual(source.count(needle), 1)   # the mutation applies
        mutant = source.replace(needle, "")
        ns = {"__name__": "mutant_sb", "__file__": SWITCHBOARD}
        mloader = importlib.machinery.SourceFileLoader("mutant_sb", SWITCHBOARD)
        mspec = importlib.util.spec_from_loader("mutant_sb", mloader)
        module = importlib.util.module_from_spec(mspec)
        sys.modules["mutant_sb"] = module
        exec(compile(mutant, SWITCHBOARD, "exec"), module.__dict__)
        self.assertNotEqual(module.cloud_branch_tick.__code__.co_code, sb.cloud_branch_tick.__code__.co_code)
        self.record("one", launch_head=sha(5))
        self.remote.heads["work/one"] = sha(6)
        self.engine.cloud_watch = CLOUD.read_records_bounded(self.state)
        for _ in range(2):
            module.cloud_branch_tick(self.engine, self.discovery)
        self.assertEqual(len(self.message_texts()), 2)   # the mutant tells twice: the real build tells once (the test above)

    def test_s8_below_the_low_budget_line_there_are_no_reads_and_one_audit_line(self):
        for n in range(3):
            self.record("b%d" % n)
            self.remote.heads["work/b%d" % n] = sha(n + 1)
        self.usage(remaining=100)
        self.run_pass()
        self.assertEqual(self.remote.reads, [])
        self.assertEqual(len(self.audit.named("cloud-branch.skipped_budget")), 1)
        self.assertEqual(self.message_texts(), [])

    def test_s8_above_the_line_and_a_stale_or_missing_reading_read_as_usual(self):
        for n in range(3):
            self.record("b%d" % n)
            self.remote.heads["work/b%d" % n] = sha(n + 1)
        for setup in (lambda: self.usage(remaining=4000), lambda: self.usage(remaining=1, at=NOW - 3600), lambda: None):
            self.remote.reads.clear()
            setup()
            self.run_pass()
            self.assertEqual(len(self.remote.reads), 3)
        self.assertEqual(self.audit.named("cloud-branch.skipped_budget"), [])


class TestDocs(unittest.TestCase):
    def section(self):
        text = (Path(KIT) / "docs" / "switchboard.md").read_text()
        found = re.search(r"^## Cloud branch heads.*?(?=^## )", text, re.S | re.M)
        self.assertIsNotNone(found)
        return found.group(0)

    def test_s1_the_section_states_the_cause(self):
        s = self.section()
        for phrase in ("has no switchboard command", "its push raises no event", "six to nine hours, twice"):
            self.assertIn(phrase, s)

    def test_s6_the_section_says_what_the_message_means_and_how_it_pairs_with_the_pod_status_line(self):
        s = self.section()
        self.assertIn("pod NAME pushed BRANCH head XXXXXXXX", s)
        self.assertIn("the worker's branch moved", s)
        self.assertIn("pod status line of the sibling ask #1397", s)


if __name__ == "__main__":
    unittest.main()

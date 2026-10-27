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


OTHER_REPO = "acme-org/other-service"
_mutants = []


def load_mutant(needle, replacement):
    """The daemon with ONE edit applied (asserted: the needle is in the source exactly once), loaded as a module of its own."""
    source = Path(SWITCHBOARD).read_text()
    assert source.count(needle) == 1, "the mutation does not apply"
    name = "mutant_push_%d" % len(_mutants)
    mloader = importlib.machinery.SourceFileLoader(name, SWITCHBOARD)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, mloader))
    sys.modules[name] = module
    exec(compile(source.replace(needle, replacement), SWITCHBOARD, "exec"), module.__dict__)
    _mutants.append(name)
    return module


class FakeForwarder:
    def poll(self):
        return None


class PushBase(Base):
    """A real hub with a live (fake) forwarder for REPO; deliveries are signed as `receive` requires. No network."""

    def setUp(self):
        super().setUp()
        self.loads = 0
        self.hub_clock = Clock()
        self.hub_clock.t = NOW
        self.make_hub(sb)

    def make_hub(self, module):
        config = module.validate_config({"github_repos": [REPO]})
        self.hub = module.GitHubHub(config, self.state, self.audit, runner=self.remote, spawn=None, now=self.hub_clock)
        watch = self.hub.watches[REPO]
        watch.secret, watch.proc = "synthetic-secret", FakeForwarder()
        self.hub.discover = lambda: self.discovery
        self.engine.hub = self.hub
        real = self.hub._push_workers_load

        def counted(now):
            self.loads += 1
            return real(now)
        self.hub._push_workers_load = counted

    def deliver(self, event, payload, secret="synthetic-secret", repo=REPO):
        body = json.dumps(dict(payload, repository={"full_name": repo})).encode()
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": sb.sign_body(secret, body)}
        return self.hub.receive("/github", headers, body)

    def push(self, branch, head, **extra):
        payload = {"ref": "refs/heads/" + branch, "after": head, "before": sha(0)}
        payload.update(extra)
        return self.deliver("push", payload)

    def clear_messages(self):
        sb.write_reminders(self.state, [])


class TestPushRoute(PushBase):
    def test_s11_three_pushes_give_three_messages_and_no_read(self):
        for n in range(3):
            self.record("pod%d" % n, launch_head=sha(1))
        for n in range(3):
            self.assertEqual(self.push("work/pod%d" % n, sha(n + 10))[0], 202)
        texts = sorted(self.message_texts())
        self.assertEqual(len(texts), 3)
        for n, text in enumerate(texts):
            self.assertIn("pod pod%d pushed work/pod%d head %s" % (n, n, sha(n + 10)[:8]), text)
            self.assertNotIn(sha(n + 10), text)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("synthetic-secret", text)
        self.assertEqual(self.remote.reads, [])
        self.assertEqual(self.audit.named("github.rejected"), [])

    def test_s11_a_mutant_that_reads_the_remote_for_a_repository_with_a_live_forwarder_fails(self):
        for n in range(3):
            self.record("pod%d" % n)
            self.remote.heads["work/pod%d" % n] = sha(n + 1)
        mutant = load_mutant("hub is not None and hub.forwarder_live(e[1])", "False")
        self.engine.cloud_watch = CLOUD.read_records_bounded(self.state)
        mutant.cloud_branch_tick(self.engine, self.discovery)
        self.assertEqual(len(self.remote.reads), 3)            # the mutant reads a forwarder repository
        self.remote.reads.clear()
        self.run_pass()
        self.assertEqual(self.remote.reads, [])                # the build does not

    def test_s12_six_cases_send_nothing_and_a_deletion_and_a_tag_are_ignored(self):
        self.record("mine", launch_head=sha(5))
        self.record("theirs", boss="lane-b", launch_head=sha(1))
        self.record("retired", retired=True, launch_head=sha(1))
        self.record("again", launch_head=sha(1))
        self.record("first", launch_head=sha(5))
        outcomes = [
            self.push("work/unknown", sha(9))[0],                              # a branch no recorded worker has
            self.push("work/theirs", sha(9))[0],                               # a worker of another lane
            self.push("work/retired", sha(9))[0],                              # a retired worker
            self.push("work/first", sha(5))[0],                                # the first head equals the launch head
        ]
        self.assertEqual(outcomes, [202] * 4)
        self.assertEqual(self.message_texts(), [])
        self.push("work/again", sha(9))
        self.assertEqual(len(self.message_texts()), 1)
        self.clear_messages()
        self.push("work/again", sha(9))                                         # the same head again
        self.assertEqual(self.message_texts(), [])
        body = json.dumps({"ref": "refs/heads/work/mine", "after": sha(9), "repository": {"full_name": REPO}}).encode()
        status = self.hub.receive("/github", {"X-GitHub-Event": "push", "X-Hub-Signature-256": sb.sign_body("wrong-secret", body)}, body)[0]
        self.assertEqual(status, 401)                                           # a bad signature is refused as before
        self.assertEqual(self.message_texts(), [])
        self.push("work/mine", sha(9), deleted=True)
        self.push("work/mine", "0" * 40)
        self.deliver("push", {"ref": "refs/tags/work/mine", "after": sha(9)})
        self.assertEqual(self.message_texts(), [])
        self.assertEqual(self.remote.reads, [])

    def test_s12_a_mutant_that_tells_on_every_push_of_the_repository_fails(self):
        self.record("mine", launch_head=sha(1))
        mutant = load_mutant("matches = table.get((repo, branch)) or []", "matches = [m for ms in table.values() for m in ms]")
        self.make_hub(mutant)
        self.push("work/unknown", sha(9))
        self.assertEqual(len(self.message_texts()), 1)          # the mutant tells for a branch no worker has: the build does not (above)

    def test_s12_a_mutant_without_the_head_dedupe_tells_the_same_head_twice(self):
        self.record("mine", launch_head=sha(1))
        mutant = load_mutant('        if entry.get("head") == head:\n            return None\n', "")
        self.assertNotEqual(mutant.cloud_push_told.__code__.co_code, sb.cloud_push_told.__code__.co_code)
        for _ in range(2):
            mutant.cloud_push_told(self.state, "mine", "work/mine", "lane-a", sha(1), sha(9), NOW)
        self.assertEqual(len(self.message_texts()), 2)
        self.clear_messages()
        for _ in range(2):
            sb.cloud_push_told(self.state, "mine2", "work/mine2", "lane-a", sha(1), sha(9), NOW)
        self.assertEqual(len(self.message_texts()), 1)

    def test_the_push_route_and_the_read_share_the_dedupe_file(self):
        self.record("one", launch_head=sha(1))
        self.push("work/one", sha(9))
        self.assertEqual(len(self.message_texts()), 1)
        self.engine.cloud_watch = CLOUD.read_records_bounded(self.state)
        self.remote.heads["work/one"] = sha(9)
        self.engine.hub = None                                   # a pass that reads (no forwarder known)
        sb.cloud_branch_tick(self.engine, self.discovery)
        self.assertEqual(len(self.remote.reads), 1)
        self.assertEqual(len(self.message_texts()), 1)           # the head the push told is not told again by the read


class TestFallback(PushBase):
    def test_s13_the_default_list_and_the_forwarder_arguments_hold_push(self):
        self.assertIn("push", sb.DEFAULTS["github_events"])
        self.assertIn("push", sb.validate_config({})["github_events"])
        argv = self.hub.argv_for(self.hub.watches[REPO])
        events = [a for a in argv if a.startswith("--events=")][0][len("--events="):].split(",")
        self.assertIn("push", events)

    def test_s13_the_fallback_reads_one_repository_of_two(self):
        self.record("watched", repo=REPO)
        self.record("unwatched", repo=OTHER_REPO)
        self.remote.heads["work/watched"] = sha(1)
        self.remote.heads["work/unwatched"] = sha(1)
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 1)
        self.assertIn(OTHER_REPO, self.remote.reads[0])
        self.hub.watches[REPO].proc = None                       # its forwarder is down: it is read too
        self.remote.reads.clear()
        self.engine.branch_interval = 0
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 2)

    def test_s10_a_branch_is_read_at_most_once_per_sixty_seconds(self):
        self.engine.branch_interval = sb.Switchboard.branch_interval
        self.assertEqual(self.engine.branch_interval, 60)
        self.record("first", repo=OTHER_REPO)
        self.remote.heads["work/first"] = sha(1)
        self.remote.heads["work/second"] = sha(1)
        self.run_pass()
        self.assertEqual(len(self.remote.reads), 1)
        self.record("second", repo=OTHER_REPO)
        self.remote.reads.clear()
        self.clock.t = 30.0
        self.run_pass()                                          # inside 60 s: the first is not read, the new branch is read at once
        self.assertEqual([r.rsplit("/", 1)[1] for r in self.remote.reads], ["second"])
        self.remote.reads.clear()
        self.clock.t = 61.0
        self.run_pass()                                          # 61 s after the first read: it is read again; the second (31 s) is not
        self.assertEqual([r.rsplit("/", 1)[1] for r in self.remote.reads], ["first"])
        (self.state / "cloud" / "first.json").unlink()
        self.run_pass()
        self.assertEqual(sorted(self.engine._branch_read_at), ["second"])   # the times are kept for the recorded workers only
        self.clock.t = 200.0
        self.usage(remaining=100)
        self.remote.reads.clear()
        self.run_pass()
        self.assertEqual(self.remote.reads, [])                  # the budget skip still applies
        self.assertEqual(len(self.audit.named("cloud-branch.skipped_budget")), 1)

    def test_s10_a_mutant_with_no_interval_reads_the_first_branch_in_every_pass(self):
        self.record("first", repo=OTHER_REPO)
        self.remote.heads["work/first"] = sha(1)
        self.engine.branch_interval = 60
        mutant = load_mutant("\n                and not (interval and e[0] in read_at and started - read_at[e[0]] < interval)]", "]")
        self.engine.cloud_watch = CLOUD.read_records_bounded(self.state)
        for t in (0.0, 10.0):
            self.clock.t = t
            mutant.cloud_branch_tick(self.engine, self.discovery)
        self.assertEqual(len(self.remote.reads), 2)              # the mutant reads twice inside 60 s
        self.remote.reads.clear()
        self.engine._branch_read_at = {}
        for t in (0.0, 10.0):
            self.clock.t = t
            self.run_pass()
        self.assertEqual(len(self.remote.reads), 1)


class TestVolume(PushBase):
    def burst(self):
        for i in range(5100):
            if i % 51 == 50:
                n = i // 51
                issue = {"id": 900 + n, "number": 12, "title": "t", "state": "open", "user": {"login": "octocat"}, "labels": [], "updated_at": "2026-01-01T00:00:00Z"}
                comment = {"id": 5000 + n, "body": "hi", "user": {"login": "octocat"}}
                self.deliver("issue_comment", {"action": "created", "issue": issue, "comment": comment})
            else:
                self.push("work/nobody-%d" % i, sha(i % 200 + 1))

    def test_s14_five_thousand_unmatched_pushes_add_no_item_no_read_and_at_most_one_audit_line(self):
        self.record("mine", launch_head=sha(1))
        self.burst()
        self.assertEqual(len(self.hub.queue), 100)
        self.assertEqual(self.remote.reads, [])
        received = self.audit.named("github.received")
        self.assertEqual(len(received), 100)                     # the 100 others are audited as before; a dropped push has no such line
        pushes = [n for n, _ in self.audit.lines if n.startswith("github.") and n != "github.received"]
        self.assertLessEqual(len(pushes), 1)
        self.assertLessEqual(self.loads, 51)
        self.hub_clock.t += 61                                    # the counter is flushed once a minute: ONE line with the count
        self.push("work/nobody-x", sha(3))
        flushed = self.audit.named("github.push_dropped")
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0]["pushes"], 5001)
        self.assertEqual(self.message_texts(), [])

    def test_s14_a_mutant_that_reads_the_record_list_for_every_push_fails(self):
        mutant = load_mutant("cached[1] >= PUSH_CACHE_DELIVERIES or ", "True or ")
        self.make_hub(mutant)
        for i in range(200):
            self.push("work/nobody-%d" % i, sha(3))
        self.assertEqual(self.loads, 200)                         # the mutant: one read per delivery; the build reads at most 51 times in 5100

    def test_s14_a_mutant_that_audits_every_dropped_push_fails(self):
        mutant = load_mutant("            if now - self._push_flushed < 60:\n                return\n", "")
        self.make_hub(mutant)
        for i in range(50):
            self.push("work/nobody-%d" % i, sha(3))
        self.assertEqual(len(self.audit.named("github.push_dropped")), 50)


class TestPushDocs(unittest.TestCase):
    def test_s13_the_docs_name_push_and_say_a_config_with_its_own_list_must_add_it_and_the_fallback_scope(self):
        text = (Path(KIT) / "docs" / "switchboard.md").read_text()
        found = re.search(r"^## Cloud branch heads.*?(?=^## )", text, re.S | re.M)
        s = found.group(0)
        for phrase in ("`push`", "a config with its own event list must add it", "forwarders must be restarted for a new event list to take effect",
                       "no live forwarder", "once per 60 seconds", "branches x 60 an hour"):
            self.assertIn(phrase, s)


if __name__ == "__main__":
    unittest.main()

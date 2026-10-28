"""Test classes moved out of bin/test-switchboard (text unchanged): cases_10."""
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
from support.bases import StandInPids
from support.core import Base
from support.fake_hubs import DECISION_LABEL
from support.installs_and_keys import ACME_API, ACME_WEB, DEFAULT_TOKEN, FAKE_TOKENS, GOOD_DONE_WHEN, GOOD_PLAN, KeyCase, PR16_KEY, PR16_NOTICE, TIER_LABELS, secrets_in
from support.loader import captured, sb, switchboard_source
from support.paths import REPO
from support.policy_and_import import FORBIDDEN_IN_PUBLIC_TEXT
from support.rigs import FakeGitHub, GithubRig, World
from support.scenario_roles import FAKE_GH, GH_BIN, GH_REPO, JEV_REPLAY
from support.service_and_merge import V2TransportBase, feed_line
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE, FakeHTTP, SimClock


class TestPlanCheckCommand(Base):
    """#135: the command: exit codes, the issue reader (injected), --file, --json. GETs only."""

    def setUp(self):
        super().setUp()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "intent_repo": "acme/app"})
        self.calls = []
        self.issue = {"number": 7, "state": "open", "title": "t", "body": GOOD_PLAN, "updated_at": "2026-01-01T00:00:00Z"}
        self.comments = []
        self.down = False

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] != FAKE_GH or argv[1] != "api":
            raise AssertionError("plan check may only run `gh api`: %r" % (argv,))
        if "--method" in argv or "-X" in argv or "-f" in argv or "--input" in argv:
            raise AssertionError("plan check must only GET: %r" % (argv,))
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: network unreachable")
        path = argv[2]
        if "/comments" in path:
            page = int(re.search(r"&page=(\d+)", path).group(1))
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.comments[(page - 1) * 100:page * 100]), "")
        if re.search(r"/issues/7$", path):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.issue), "")
        return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")

    def run_cmd(self, *argv):
        with captured() as (out, err):
            code = sb.main(["plan", "check", *argv], runner=self.runner, now=self.world_now)
        return code, out.getvalue(), err.getvalue()

    def world_now(self):
        return 1000.0

    def test_a_good_plan_from_an_issue_body_passes_with_exit_0(self):
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 0, err)
        self.assertIn("plan check: PASS", out)
        self.assertIn("source: issue body of acme/app#7", out)
        self.assertRegex(out, r"plan hash: sha256:[0-9a-f]{64}")

    def test_a_bad_plan_fails_with_exit_1_and_one_line_per_defect(self):
        self.issue["body"] = GOOD_PLAN.replace("pass: the reply arrives within 5 s and is longer than 0 characters", "pass: works")
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 1)
        self.assertIn("plan check: FAIL", out)
        defects = [l.strip() for l in out.splitlines() if l.strip().startswith("DEFECT")]
        self.assertEqual(len(defects), 1)
        self.assertIn("DEFECT S2: the pass rule 'works' says nothing a wrong build could fail", defects[0])
        self.assertIn("vague-pass x1", out)

    def test_the_latest_plan_comment_is_the_plan(self):
        broken = GOOD_PLAN.replace("exactly 1 agent", "works").replace('named "alpha"', "")
        self.issue["body"] = "## Intent\nx\nDone when: %s\n\nSee the plan comment.\n" % GOOD_DONE_WHEN
        self.comments = [{"id": 11, "created_at": "2026-01-01T00:00:00Z", "body": "## Plan (scenarios)\n\n" + broken[broken.index("```yaml"):]},
                         {"id": 12, "created_at": "2026-01-02T00:00:00Z", "body": "## Plan (scenarios) rev 2\n\n" + GOOD_PLAN[GOOD_PLAN.index("```yaml"):],
                          "user": {"login": "planner"}}]
        code, out, err = self.run_cmd("https://github.com/acme/app/issues/7")
        self.assertEqual(code, 1, out + err)   # #978: a repost is a defect naming both comments; the latest is still the one read
        self.assertIn("source: comment 12 by planner of acme/app#7", out)
        self.assertIn("comment 12 looks like a plan but comment 11 is already the plan", out)

    def test_one_canonical_comment_with_two_receipts_passes_and_names_the_canonical_one(self):
        h = "sha256:" + "b" * 64
        receipt = "<!-- plan-receipt -->\nplan hash: %s\nwhat changed\nwhy" % h
        self.issue["body"] = "## Intent\nx\nDone when: %s\n" % GOOD_DONE_WHEN
        self.comments = [{"id": 11, "created_at": "2026-01-01T00:00:00Z", "body": "## Plan (scenarios)\n\n" + GOOD_PLAN[GOOD_PLAN.index("```yaml"):]},
                         {"id": 12, "created_at": "2026-01-02T00:00:00Z", "body": receipt},
                         {"id": 13, "created_at": "2026-01-03T00:00:00Z", "body": receipt}]
        code, out, err = self.run_cmd("https://github.com/acme/app/issues/7")
        self.assertEqual(code, 0, out + err)
        self.assertIn("source: comment 11", out)
        self.comments[2]["body"] += "\n## Plan (scenarios)"
        code, out, err = self.run_cmd("https://github.com/acme/app/issues/7")
        self.assertEqual(code, 1, out + err)
        self.assertIn("receipt carries plan text", out)

    def test_an_issue_comment_url_picks_that_comment(self):
        self.comments = [{"id": 11, "created_at": "2026-01-01T00:00:00Z", "body": "## Plan (scenarios)\n\n" + GOOD_PLAN[GOOD_PLAN.index("```yaml"):]}]
        self.issue["body"] = "## Intent\nx\nDone when: %s\n" % GOOD_DONE_WHEN
        code, out, err = self.run_cmd("https://github.com/acme/app/issues/7#issuecomment-11")
        self.assertEqual(code, 0, out + err)
        self.assertIn("source: comment 11", out)
        code, out, err = self.run_cmd("https://github.com/acme/app/issues/7#issuecomment-99")
        self.assertEqual(code, 2)
        self.assertIn("no comment 99", err)

    def test_a_bare_number_uses_the_intent_repo(self):
        code, out, err = self.run_cmd("7")
        self.assertEqual(code, 0, err)
        self.assertIn("acme/app#7", out)

    def test_unreadable_inputs_exit_2_and_say_why(self):
        self.down = True
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read", err)
        self.down = False
        for argv, fragment in ((("acme/app#99",), "cannot read"), (("not a plan",), "is not a plan"), ((), "give a plan"),
                               (("--file", str(self.home / "missing.md")), "cannot read")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cmd(*argv)
                self.assertEqual(code, 2)
                self.assertIn(fragment, err)
        self.issue["body"] = "## Intent\nx\nDone when: (1) y\n\nno block here"
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 2)
        self.assertIn("no fenced yaml block", err)
        self.issue["body"] = "```yaml\nscenarios:\n\t- id: S1\n```"
        self.assertEqual(self.run_cmd("acme/app#7")[0], 2)

    def test_file_and_json_and_done_when_override(self):
        path = self.home / "plan.md"
        path.write_text(GOOD_PLAN)
        code, out, err = self.run_cmd("--file", str(path), "--json")
        body = json.loads(out)
        self.assertEqual((code, body["result"], body["scenarios"], body["defects"]), (0, "pass", 3, []))
        self.assertEqual(self.calls, [], "a file is read without GitHub")
        code, out, err = self.run_cmd("--file", str(path), "--done-when", "(1) a; (2) b; (3) c", "--json")
        self.assertEqual(code, 1)
        self.assertIn("uncovered-clause", [d["code"] for d in json.loads(out)["defects"]])
        code, out, err = self.run_cmd("--file", str(self.home / "nope.md"), "--json")
        self.assertEqual((code, json.loads(out)["result"]), (2, "error"))

    def test_a_plan_issue_with_no_done_when_takes_it_from_the_ask(self):
        self.issue["body"] = GOOD_PLAN.replace("Done when: " + GOOD_DONE_WHEN, "")
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 1)
        self.assertIn("no Done-when", out)
        code, out, err = self.run_cmd("acme/app#7", "--done-when", GOOD_DONE_WHEN)
        self.assertEqual(code, 0, out)

    def test_more_than_a_hundred_comments_are_paged(self):
        filler = [{"id": 1000 + n, "created_at": "2026-01-01T00:00:%02dZ" % (n % 60), "body": "chatter"} for n in range(120)]
        plan = {"id": 5000, "created_at": "2027-01-01T00:00:00Z", "body": "## Plan (scenarios)\n\n" + GOOD_PLAN[GOOD_PLAN.index("```yaml"):]}
        self.comments = filler + [plan]
        self.issue["body"] = "## Intent\nx\nDone when: %s\n" % GOOD_DONE_WHEN
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 0, out + err)
        self.assertIn("comment 5000", out)
        self.assertEqual(len([c for c in self.calls if "/comments" in c[2]]), 2)

    def test_the_ask_is_authoritative_and_a_plan_with_fewer_clauses_fails(self):
        weaker = GOOD_PLAN.replace("(1) an owner can commission an agent; (2) it answers a chat message.", "(1) an owner can commission an agent.")
        weaker = weaker.replace("covers: DW-2", "covers: PC-2")
        self.issue["body"] = weaker
        asks = {"8": {"number": 8, "state": "open", "title": "ask", "body": "## Intent\nx\nDone when: " + GOOD_DONE_WHEN,
                      "updated_at": "2026-01-01T00:00:00Z"}}
        base = self.runner

        def runner(argv, **kw):
            if re.search(r"/issues/8$", argv[2]):
                return subprocess.CompletedProcess(argv, 0, json.dumps(asks["8"]), "")
            return base(argv, **kw)
        self.runner = runner
        code, out, err = self.run_cmd("acme/app#7", "--ask", "acme/app#8")
        self.assertEqual(code, 1, out + err)
        self.assertIn("authority", out)
        self.assertIn("DW-2 is not covered", out)
        code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 0, "without --ask the plan's own Done-when is all there is")

    def test_two_hashes_are_printed_and_the_plan_hash_covers_the_whole_plan(self):
        code, out, err = self.run_cmd("acme/app#7", "--json")
        first = json.loads(out)
        self.assertEqual(sorted(k for k in first if k.endswith("hash")), ["plan_hash", "scenario_hash"])
        self.issue["body"] = GOOD_PLAN.replace("Commissioning works.", "Commissioning works, and has a new constraint.")
        second = json.loads(self.run_cmd("acme/app#7", "--json")[1])
        self.assertEqual(second["scenario_hash"], first["scenario_hash"])
        self.assertNotEqual(second["plan_hash"], first["plan_hash"])
        self.issue["body"] = GOOD_PLAN.replace("Commissioning works.", "Commissioning   works.\n\n")
        self.assertEqual(json.loads(self.run_cmd("acme/app#7", "--json")[1])["plan_hash"], first["plan_hash"], "whitespace only")
        code, out, err = self.run_cmd("acme/app#7")
        self.assertIn("scenario hash:", out)
        self.assertIn("plan hash:", out)

    def test_a_missing_plan_checker_stops_only_this_command(self):
        with mock.patch.object(sb, "load_plan_check", side_effect=FileNotFoundError("plan_check.py is gone")):
            code, out, err = self.run_cmd("acme/app#7")
        self.assertEqual(code, 2)
        self.assertIn("cannot be loaded", err)
        text = switchboard_source()
        self.assertNotRegex(text, r"(?m)^plan_check = load_plan_check\(\)")

    def test_it_only_reads_and_never_ensures_the_daemon(self):
        self.run_cmd("acme/app#7")
        self.assertTrue(self.calls)
        self.assertTrue(all(c[1] == "api" and "--method" not in c for c in self.calls))
        self.assertNotIn("plan", sb.ENSURE_ON_USE_COMMANDS)


class TestPlanExplain(Base):
    """#353: `plan check --explain` and `plan template`."""

    DONE = "(1) a thing works (2) the other is shown"
    BAD = ("```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: Given a thing, when it runs, then it works\n    proof: test\n"
           "    pass: works\n    discriminates: a stub that returns an empty list fails the count\n    evaluator: ci\n```\n")

    def setUp(self):
        super().setUp()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "intent_repo": "acme/app"})
        self.calls, self.comments, self.down = [], [], False
        self.issue = {"number": 7, "state": "open", "title": "t", "updated_at": "2026-01-01T00:00:00Z",
                      "body": "## Intent\nx\nDone when: %s\n" % self.DONE}

    runner = TestPlanCheckCommand.runner
    world_now = TestPlanCheckCommand.world_now

    def cmd(self, *argv):
        with captured() as (out, err):
            code = sb.main(["plan", *argv], runner=self.runner, now=self.world_now)
        return code, out.getvalue(), err.getvalue()

    def plan_file(self, text):
        path = Path(self.home) / "plan.md"
        path.write_text(text)
        return str(path)

    def test_every_defect_is_followed_by_its_rule_and_fix(self):
        path = self.plan_file(self.BAD)
        code, out, _ = self.cmd("check", "--file", path, "--done-when", self.DONE, "--explain")
        self.assertEqual(code, 1)
        defects = [l for l in out.splitlines() if l.strip().startswith("DEFECT")]
        whys = [l for l in out.splitlines() if l.strip().startswith("WHY ")]
        self.assertEqual((len(defects), len(whys)), (3, 3), out)   # vague pass, uncovered DW-2, no unchanged scenario
        self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith("FIX ")]), 3)

    def test_every_code_the_checker_can_produce_has_an_explanation(self):
        source = Path(sb.plan_check.__file__).read_text()
        codes = set(re.findall(r'bad\((?:ident|"-"), "([a-z-]+)"', source)) | set(re.findall(r'"code": "([a-z-]+)"', source))
        codes |= {"missing-size", "bad-size", "size-in-days"} | {"missing-%s" % key for key in sb.plan_check.REQUIRED}
        codes.discard("missing-%s")
        self.assertGreater(len(codes), 20)
        self.assertEqual([c for c in sorted(codes) if sb.plan_check.explain(c) is None], [])

    def test_without_the_flag_the_output_is_what_it_was(self):
        path = self.plan_file(self.BAD)
        _, plain, _ = self.cmd("check", "--file", path, "--done-when", self.DONE)
        _, explained, _ = self.cmd("check", "--file", path, "--done-when", self.DONE, "--explain")
        self.assertNotIn("WHY ", plain)
        self.assertEqual([l for l in explained.splitlines() if not l.strip().startswith(("WHY ", "FIX "))], plain.splitlines())
        pick = lambda text: re.search(r"plan hash: (sha256:[0-9a-f]{64})", text).group(1)
        self.assertEqual(pick(plain), pick(explained))

    def test_the_template_has_the_clauses_and_one_stub_each_and_is_read_only(self):
        code, out, err = self.cmd("template", "acme/app#7")
        self.assertEqual(code, 0, err)
        self.assertIn("plan-format: 2", out)
        self.assertEqual(len(re.findall(r"covers: DW-", out)), 2)
        self.assertIn("(2) the other is shown", out)
        self.assertTrue(self.calls and all(c[1] == "api" and "--method" not in c for c in self.calls))

    def test_the_template_with_its_placeholders_filled_in_passes(self):
        code, out, _ = self.cmd("template", "--done-when", self.DONE)
        self.assertEqual(code, 0)
        filled = out.replace("<S|M|L|XL> (<what makes it that size>)", "S (one file)")
        counter = iter(range(1, 100))
        def fill(match):
            n = next(counter)
            return {"text": "Given case %d is set up, when it runs, then it behaves" % n, "pass": "the call number %d returns status 200 and exactly %d items" % (n, n),
                    "discriminates": "a stub that returns an empty list for item %d fails the count" % n}.get(match.group(1), "something real and long enough for %d" % n)
        filled = re.sub(r"(?m)^(    (?:text|pass|discriminates): )<.*>$", lambda m: m.group(1) + fill(re.match(r"(\w+)", m.group(0).strip())), filled)
        filled = re.sub(r"(?m)^(PC-1|Out of scope|Areas touched|Risks and rollback): <.*>$", lambda m: m.group(1) + ": something real", filled)
        self.assertNotIn("<", filled)
        code, result, _ = self.cmd("check", "--file", self.plan_file(filled), "--done-when", self.DONE)
        self.assertEqual(code, 0, result)
        self.assertIn("plan check: PASS", result)

    def test_control_characters_and_long_text_in_the_ask_never_reach_the_terminal(self):
        self.issue["body"] = "## Intent\nx\nDone when: (1) first \x1b]0;owned\x07 clause (2) %s\n" % ("y" * 900)
        code, out, _ = self.cmd("template", "acme/app#7")
        self.assertEqual(code, 0)
        self.assertNotRegex(out, "[\x00-\x08\x0b-\x1f\x7f-\x9f]")
        self.assertIn("first", out)
        self.assertLessEqual(max(len(l) for l in out.splitlines()), 340)

    def test_template_with_neither_ask_nor_done_when_and_with_an_unreadable_ask_are_errors(self):
        code, _, err = self.cmd("template")
        self.assertEqual(code, 2)
        self.assertIn("give the ask", err)
        self.down = True
        code, out, err = self.cmd("template", "acme/app#7")
        self.assertEqual((code, out), (2, ""))

    def test_explain_follows_a_warning_too(self):
        path = self.plan_file(self.BAD.replace("proof: test", "proof: test").replace("evaluator: ci", "evaluator: adam"))
        _, out, _ = self.cmd("check", "--file", path, "--done-when", "(1) a thing works", "--explain")
        lines = out.splitlines()
        at = next(i for i, l in enumerate(lines) if l.startswith("WARN") and "adam" in l)
        self.assertTrue(lines[at + 1].strip().startswith("WHY evaluator-alias"))

    def test_an_ask_with_no_done_when_is_said_not_prefilled(self):
        self.issue["body"] = "## Intent\nx\n"
        code, out, err = self.cmd("template", "acme/app#7")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no Done-when", err)


class GitHubFarm:
    """Several fake repos behind one `gh`. A repo mapped to an account answers only to that account's
    token, as a repo another account owns would answer 404 to a stranger. `gh auth token --user` hands
    out the fake tokens. Everything the hub ran is kept in `ran`, for the "secret never text" scan."""

    def __init__(self, clock, repos, accounts=None):
        self.repos = {repo: FakeGitHub(clock, repo) for repo in repos}
        self.accounts = dict(accounts or {})  # repo -> gh account
        for repo, account in self.accounts.items():
            self.repos[repo].token = FAKE_TOKENS[account]
        self.ran, self.token_asks = [], []
        self.hub = None

    def attach(self, hub):
        self.hub = hub
        for gh in self.repos.values():
            gh.hub = hub

    def runner(self, argv, **kwargs):
        argv = list(argv)
        self.ran.append(argv)
        if argv[:2] == [GH_BIN, "auth"]:
            if argv[2:4] != ["token", "--user"] or "GH_TOKEN" in (kwargs.get("env") or {}):
                raise AssertionError("fake gh: `auth token --user NAME`, with no GH_TOKEN in the environment: %r" % argv)
            self.token_asks.append(argv[4])
            token = FAKE_TOKENS.get(argv[4])
            if token is None:
                return subprocess.CompletedProcess(argv, 1, "", "no token found for %s\n" % argv[4])
            return subprocess.CompletedProcess(argv, 0, token + "\n", "")
        found = re.match(r"^repos/([^/]+/[^/]+)/", argv[-1])
        gh = self.repos.get(found.group(1)) if found else None
        if gh is None:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        return gh.runner(argv, **kwargs)

    def spawn(self, argv, log_path, env=None):
        self.ran.append(list(argv))
        repo = next(a for a in argv if a.startswith("--repo="))[len("--repo="):]
        return self.repos[repo].spawn(argv, log_path, env)


class FarmRig:
    """A hub on a farm of fake repos, over an engine on the fake fleet, on one simulated clock."""

    def __init__(self, test, agents=None, repos=(ACME_API, ACME_WEB), accounts=None, baseline=(), config=None):
        raw = {"github_repos": list(baseline), "gh": GH_BIN,
               "gh_users": {repo: account for repo, account in (accounts or {}).items()}}
        raw.update(config or {})
        self.world = World(test, agents=(), config=raw)
        for name, fleet in (agents or {}).items():
            self.world.add_agent(name, fleet=fleet)
        self.farm = GitHubFarm(self.world.clock, repos, accounts)
        self.hub = sb.GitHubHub(self.world.config, self.world.state, sb.AuditLog(self.world.audit_path),
                                self.farm.runner, self.farm.spawn, self.world.clock.now)
        self.hub.autostart, self.hub.port = False, 4711
        self.farm.attach(self.hub)
        self.world.engine = self.hub.attach(self.world.new_engine())

    def pump(self, seconds, every=5):
        for _ in range(0, int(seconds), every):
            self.world.tick(every)
            self.hub.tick()
            self.world.engine.run_pass()

    def repo(self, name):
        return self.farm.repos[name]

    def leave(self, name):
        self.world.herdr.panes.pop(self.world.panes.pop(name))

    def toasts(self):
        return [t["title"] for t in self.world.herdr.toasts]


class TestWatchedReposAreDerived(Base):
    def test_a_repo_joins_when_an_agent_naming_it_appears_and_leaves_when_the_agent_goes(self):
        rig = FarmRig(self, baseline=[ACME_API])
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_API])
        rig.world.add_agent("web-orch", fleet={"role": "orchestrator", "charter": 1, "repo": ACME_WEB})
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_API, ACME_WEB])
        forwarder = rig.repo(ACME_WEB).forwarder()
        self.assertIsNotNone(forwarder)
        rig.leave("web-orch")
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_API], "no live agent and no baseline names it any more")
        self.assertEqual(forwarder.poll(), -15, "its forwarder was stopped")
        self.assertIsNotNone(rig.repo(ACME_API).forwarder(), "the baseline repo is untouched")

    def test_a_charter_with_no_repo_of_its_own_watches_the_intent_repo(self):
        rig = FarmRig(self, agents={"orch": {"role": "orchestrator", "charter": 1}}, config={"intent_repo": ACME_WEB})
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_WEB])

    def test_the_static_repos_fault_never_notices_a_new_repo(self):
        os.environ[sb.FAULT_ENV] = "static-repos"
        rig = FarmRig(self, agents={"web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}},
                      baseline=[ACME_API])
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_API])

    def test_a_watch_that_left_is_never_started_again_by_a_tick_that_already_held_it(self):
        rig = FarmRig(self, agents={"web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}})
        rig.pump(10)
        old = rig.hub.watches[ACME_WEB]
        rig.leave("web-orch")
        rig.pump(10)
        before = len(rig.repo(ACME_WEB).procs)
        rig.world.clock.t += 600
        rig.hub._tick_watch(old, rig.world.clock.now())  # the supervisor's copy of the list was taken before
        self.assertEqual(len(rig.repo(ACME_WEB).procs), before)

    def test_an_unreadable_fleet_never_drops_a_watch(self):
        rig = FarmRig(self, agents={"web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}})
        rig.pump(10)
        rig.world.herdr.down = True
        rig.pump(10)
        self.assertEqual(sorted(rig.hub.watches), [ACME_WEB])

    def test_a_restart_with_no_state_derives_the_same_set_in_one_pass(self):
        rig = FarmRig(self, agents={"web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}})
        rig.pump(10)
        shutil.rmtree(str(rig.world.state))
        rig.world.state.mkdir(mode=0o700)
        fresh = FarmRig.__new__(FarmRig)
        fresh.__dict__.update(rig.__dict__)
        fresh.hub = sb.GitHubHub(rig.world.config, rig.world.state, sb.AuditLog(rig.world.audit_path),
                                 rig.farm.runner, rig.farm.spawn, rig.world.clock.now)
        fresh.hub.autostart, fresh.hub.port = False, 4711
        rig.farm.attach(fresh.hub)
        fresh.world.engine = fresh.hub.attach(fresh.world.new_engine())
        fresh.pump(10)
        self.assertEqual(sorted(fresh.hub.watches), [ACME_WEB])


class TestForwarderFallback(Base):
    AGENTS = {"web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}}

    def rig(self, **kwargs):
        rig = FarmRig(self, agents=self.AGENTS, **kwargs)
        rig.repo(ACME_WEB).issue(2, parent=1)
        return rig

    def test_a_repo_without_webhook_rights_is_polled_says_so_and_toasts_once(self):
        rig = self.rig()
        rig.repo(ACME_WEB).webhook_ok = False
        rig.pump(300)
        watch = rig.hub.watches[ACME_WEB]
        self.assertTrue(watch.polling)
        info = watch.to_json()
        self.assertEqual(info["mode"], "polling")
        self.assertIn("admin rights", info["last_error"])
        self.assertEqual(len([t for t in rig.toasts() if ACME_WEB in t]), 1, rig.toasts())

    def test_a_polled_repos_events_still_arrive_without_a_forwarder(self):
        rig = self.rig()
        rig.repo(ACME_WEB).webhook_ok = False
        rig.pump(20)
        rig.repo(ACME_WEB).comment(2, "please look", 5001, forward=False)
        rig.pump(260)
        keys = [k for a in rig.world.admissions("web-orch") for k in a["keys"]]
        self.assertEqual(keys, ["github.comment:5001"])

    def test_the_poll_is_gentle_on_the_api(self):
        rig = self.rig(config={"github_poll_seconds": 60})
        rig.repo(ACME_WEB).webhook_ok = False
        rig.pump(660)
        reads = [c for c in rig.repo(ACME_WEB).api_calls if c.startswith("repos/%s/issues/comments" % ACME_WEB)]
        self.assertLessEqual(len(reads), 13, "one read of comments a minute, not one a pass")
        self.assertGreaterEqual(len(reads), 8)

    def test_a_repo_that_does_not_exist_yet_retries_and_starts_when_it_appears(self):
        rig = self.rig()
        rig.repo(ACME_WEB).exists = False
        rig.pump(60)
        watch = rig.hub.watches[ACME_WEB]
        self.assertEqual(watch.to_json()["mode"], "failed")
        self.assertIn("Not Found", watch.last_error)
        rig.repo(ACME_WEB).exists = True
        rig.pump(400)
        self.assertEqual(rig.hub.watches[ACME_WEB].to_json()["mode"], "forwarder")
        self.assertIsNotNone(rig.repo(ACME_WEB).forwarder())

    def test_a_forwarder_that_recovers_stops_the_polling(self):
        rig = self.rig()
        rig.repo(ACME_WEB).webhook_ok = False
        rig.pump(60)
        self.assertTrue(rig.hub.watches[ACME_WEB].polling)
        rig.repo(ACME_WEB).webhook_ok = True
        rig.pump(400)
        self.assertFalse(rig.hub.watches[ACME_WEB].polling)
        self.assertEqual(rig.hub.watches[ACME_WEB].state, "running")


class TestAccountPerRepo(Base):
    ACCOUNTS = {ACME_API: "alice-bot", ACME_WEB: "acme-ci"}
    AGENTS = {"api-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_API},
              "web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}}

    def rig(self, **kwargs):
        rig = FarmRig(self, agents=self.AGENTS, accounts=self.ACCOUNTS, **kwargs)
        for repo in (ACME_API, ACME_WEB):
            rig.repo(repo).issue(2, parent=1)
        return rig

    def test_each_forwarder_gets_its_own_accounts_token_in_its_environment_only(self):
        rig = self.rig()
        rig.pump(20)
        for repo, account in self.ACCOUNTS.items():
            proc = rig.repo(repo).forwarder()
            self.assertIsNotNone(proc, repo)
            self.assertEqual(proc.env["GH_TOKEN"], FAKE_TOKENS[account])
            self.assertNotIn(FAKE_TOKENS[account], " ".join(proc.argv))

    def test_catch_up_and_lookup_reads_use_the_repos_own_account(self):
        rig = self.rig()
        rig.repo(ACME_WEB).webhook_ok = False
        rig.pump(120)
        self.assertEqual(rig.hub.watches[ACME_WEB].catch_up_error, None, "the right token reads the repo")
        self.assertIn("acme-ci", rig.farm.token_asks)

    def test_the_one_account_fault_reads_every_repo_with_the_default_token(self):
        os.environ[sb.FAULT_ENV] = "one-account"
        rig = self.rig()
        rig.pump(60)
        self.assertEqual(rig.farm.token_asks, [])
        self.assertTrue(all(not rig.repo(r).forwarder() for r in (ACME_API, ACME_WEB)), "the strangers' token is refused")

    def test_a_token_gh_will_not_give_is_a_failure_that_names_the_account_never_a_token(self):
        rig = self.rig(config={"gh_users": {ACME_API: "alice-bot", ACME_WEB: "nobody"}})
        rig.pump(20)
        error = rig.hub.watches[ACME_WEB].last_error
        self.assertIn("nobody", error)
        self.assertNotIn("SECRET", error)


class TestStatusPerRepo(Base):
    AGENTS = {"api-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_API},
              "web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB},
              "web-coder": {"role": "coder", "issue": 2, "repo": ACME_WEB}, "cos": {"role": "chief-of-staff"}}
    ACCOUNTS = {ACME_API: "alice-bot", ACME_WEB: "acme-ci"}

    def setUp(self):
        super().setUp()
        self.rig = FarmRig(self, agents=self.AGENTS, accounts=self.ACCOUNTS, baseline=[ACME_API])
        self.rig.repo(ACME_WEB).webhook_ok = False
        for repo in (ACME_API, ACME_WEB):
            self.rig.repo(repo).issue(2, parent=1)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": GH_BIN, "github_repos": [ACME_API],
                           "gh_users": self.ACCOUNTS})
        self.rig.pump(60)

    def status(self, *argv):
        with captured() as (out, _):
            code = sb.main(["status", *argv], runner=self.rig.world.route, now=self.rig.world.clock.now)
        return code, out.getvalue()

    def test_json_lists_each_repo_how_it_is_watched_its_account_and_its_agents(self):
        code, out = self.status("--json")
        detail = {d["repo"]: d for d in json.loads(out)["github"]["detail"]}
        self.assertEqual(sorted(detail), [ACME_API, ACME_WEB])
        self.assertEqual((detail[ACME_API]["how"], detail[ACME_API]["account"], detail[ACME_API]["agents"]),
                         ("forwarder", "alice-bot", ["api-orch"]))
        self.assertEqual((detail[ACME_WEB]["how"], detail[ACME_WEB]["account"], detail[ACME_WEB]["agents"]),
                         ("polling", "acme-ci", ["web-coder", "web-orch"]))
        self.assertIn("cannot create a webhook", detail[ACME_WEB]["why"])

    def test_the_text_says_it_in_a_line_per_repo(self):
        _, out = self.status()
        lines = {line.split()[0]: line for line in out.splitlines() if line.strip().startswith("acme/")}
        self.assertIn("forwarder", lines[ACME_API])
        self.assertIn("account alice-bot", lines[ACME_API])
        self.assertIn("api-orch", lines[ACME_API])
        self.assertIn("polling every 60s", lines[ACME_WEB])
        self.assertIn("account acme-ci", lines[ACME_WEB])
        self.assertIn("web-coder, web-orch", lines[ACME_WEB])

    def test_no_token_appears_anywhere_in_status(self):
        for argv in ((), ("--json",)):
            _, out = self.status(*argv)
            for token in list(FAKE_TOKENS.values()) + [DEFAULT_TOKEN]:
                self.assertNotIn(token, out)

    def test_a_repo_no_agent_or_baseline_names_is_not_listed(self):
        self.rig.leave("web-orch")
        self.rig.leave("web-coder")
        self.rig.pump(10)
        _, out = self.status("--json")
        self.assertEqual([d["repo"] for d in json.loads(out)["github"]["detail"]], [ACME_API])

    def test_without_a_daemon_the_how_is_unknown_but_the_account_and_agents_are_known(self):
        (self.rig.world.state / sb.WATCH_FILE).unlink()
        _, out = self.status("--json")
        detail = {d["repo"]: d for d in json.loads(out)["github"]["detail"]}
        self.assertEqual((detail[ACME_WEB]["how"], detail[ACME_WEB]["account"]), ("unknown", "acme-ci"))

    def test_a_repo_with_no_mapped_account_says_default(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": GH_BIN, "github_repos": [ACME_API]})
        _, out = self.status("--json")
        self.assertEqual({d["account"] for d in json.loads(out)["github"]["detail"]}, {"default"})


class TestSecretNeverTextInTheHub(Base):
    def test_a_forwarder_and_a_poll_that_fail_with_a_leaking_gh_leave_no_token_anywhere(self):
        rig = FarmRig(self, agents=TestAccountPerRepo.AGENTS, accounts=TestAccountPerRepo.ACCOUNTS)
        for repo in (ACME_API, ACME_WEB):
            rig.repo(repo).issue(2, parent=1)
        rig.pump(20)
        for repo in (ACME_API, ACME_WEB):
            rig.repo(repo).leak, rig.repo(repo).exists = True, False
            rig.repo(repo).token = "someone-elses"
            rig.repo(repo).kill_forwarder()
        rig.pump(400)
        self.assertTrue(all(w.polling for w in rig.hub.watches.values()), "the polls ran, and failed")
        self.assertTrue(all(w.catch_up_error for w in rig.hub.watches.values()))
        rig.world.engine.audit.append("daemon", "probe", None, {})
        found = []
        for watch in rig.hub.watches.values():
            found += secrets_in(json.dumps(watch.to_json()))
        found += secrets_in(rig.world.state_text() if hasattr(rig.world, "state_text") else "")
        text = "\n".join(p.read_text(errors="replace") for p in sorted(rig.world.state.rglob("*")) if p.is_file())
        text += "\n".join(m.get("text") or "" for s in rig.world.v2.sessions.values() for m in s["messages"])
        text += "\n".join(t["title"] + (t["body"] or "") for t in rig.world.herdr.toasts)
        self.assertEqual(found + secrets_in(text), [])
        argvs = " ".join(" ".join(a) for a in rig.farm.ran)
        self.assertEqual(secrets_in(argvs), [], "no argv, forwarder or read, holds a token")


class TestDecisionsAcrossRepos(Base):
    """The Decisions list (PR 14) over many repos (PR 15): an id carries its repo, every watched repo is read
    with its own account, and nothing depends on the intent repo."""
    ACCOUNTS = {ACME_API: "alice-bot", ACME_WEB: "acme-ci"}
    AGENTS = {"api-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_API},
              "web-orch": {"role": "orchestrator", "charter": 1, "repo": ACME_WEB}}

    def rig(self, agents=None, repos=(ACME_API, ACME_WEB), accounts=None, **config):
        rig = FarmRig(self, agents=self.AGENTS if agents is None else agents, repos=repos,
                      accounts=accounts, config=dict({"awaiting_label": DECISION_LABEL}, **config))
        rig.world.engine.hub = rig.hub
        return rig

    def derive(self, rig):
        rig.world.engine.discover(rig.world.clock.now())
        rig.world.engine.collect_facts()
        return sb.derive_decisions(rig.world.engine, rig.world.clock.now())

    def label_both(self, rig):
        rig.repo(ACME_API).issue(2, title="Ship the API?", labels=[DECISION_LABEL])
        rig.repo(ACME_WEB).issue(2, title="Launch the page?", labels=[DECISION_LABEL])
        rig.pump(70)

    def test_one_issue_number_in_two_repos_is_two_decisions_each_naming_its_repo(self):
        rig = self.rig()
        self.label_both(rig)
        found, errors = self.derive(rig)
        self.assertEqual(errors, [])
        self.assertEqual(sorted((d["id"], d["repo"], d["ask"]) for d in found),
                         [("api#2", ACME_API, 2), ("web#2", ACME_WEB, 2)])

    def test_with_one_repo_the_id_is_the_bare_number_and_the_repo_is_still_recorded(self):
        rig = self.rig(agents={"api-orch": self.AGENTS["api-orch"]}, repos=(ACME_API,))
        rig.repo(ACME_API).issue(2, title="Ship the API?", labels=[DECISION_LABEL])
        rig.pump(70)
        (one,), errors = self.derive(rig)
        self.assertEqual((one["id"], one["repo"]), ("#2", ACME_API))

    def test_two_repos_that_share_a_name_are_told_apart_by_their_owner(self):
        rig = self.rig(agents={"a": {"role": "orchestrator", "charter": 1, "repo": "acme/site"},
                               "b": {"role": "orchestrator", "charter": 1, "repo": "beta/site"}},
                       repos=("acme/site", "beta/site"))
        rig.repo("acme/site").issue(2, title="one", labels=[DECISION_LABEL])
        rig.repo("beta/site").issue(2, title="two", labels=[DECISION_LABEL])
        rig.pump(70)
        found, _ = self.derive(rig)
        self.assertEqual(sorted(d["id"] for d in found), ["acme/site#2", "beta/site#2"])

    def test_every_watched_repo_is_read_whatever_the_intent_repo_and_the_baseline_are(self):
        rig = self.rig(intent_repo="acme/elsewhere")
        self.assertEqual(rig.world.config["github_repos"], [])
        self.label_both(rig)
        found, errors = self.derive(rig)
        self.assertEqual((len(found), errors), (2, []))

    def test_each_repo_is_read_with_its_own_account(self):
        rig = self.rig(accounts=self.ACCOUNTS)
        self.label_both(rig)
        found, errors = self.derive(rig)
        self.assertEqual((sorted(d["repo"] for d in found), errors), ([ACME_API, ACME_WEB], []))
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "one-account"
        lone = self.rig(accounts=self.ACCOUNTS)
        self.label_both(lone)
        found, errors = self.derive(lone)
        self.assertEqual(found, [], "with one account for everything, the repos the other account owns are unreadable")
        self.assertTrue(errors)

    def test_a_repo_read_that_fails_is_said_by_repo_and_does_not_hide_the_other(self):
        rig = self.rig(accounts=self.ACCOUNTS)
        rig.repo(ACME_WEB).token = "tok-revoked"  # the account's token no longer opens this repo
        self.label_both(rig)
        found, errors = self.derive(rig)
        self.assertEqual([d["id"] for d in found], ["api#2"])
        self.assertTrue(any(ACME_WEB in e["error"] for e in errors), errors)
        self.assertFalse(any(ACME_API in e["error"] for e in errors), errors)

    def test_the_bare_issue_number_fault_drops_the_repo_from_the_id(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "bare-issue-number"
        rig = self.rig()
        self.label_both(rig)
        found, _ = self.derive(rig)
        self.assertNotIn("api#2", [d["id"] for d in found])
        self.assertEqual(len({d["id"] for d in found}), 2, "the ids still never collide")

    def test_the_file_and_the_command_show_the_repo_tag(self):
        rig = self.rig()
        self.label_both(rig)
        rig.world.engine.run_pass()
        body = json.loads((rig.world.state / sb.DECISIONS_FILE).read_text())
        self.assertEqual(sorted(d["id"] for d in body["decisions"]), ["api#2", "web#2"])
        text = sb.format_decisions(sb.decisions_report(rig.world.state, rig.world.clock.now()), rig.world.clock.now())
        self.assertIn("api#2  waiting", text)
        self.assertIn("web#2  waiting", text)

    def test_status_counts_the_decisions_of_each_repo_when_several_are_watched(self):
        rig = self.rig()
        self.label_both(rig)
        rig.world.engine.run_pass()
        summary = sb.decisions_summary(rig.world.state, rig.world.clock.now)
        self.assertEqual(summary["by_repo"], {ACME_API: 1, ACME_WEB: 1})
        self.assertIn("(acme/api 1, acme/web 1;", "\n".join(sb.format_decisions_status(summary)))

    def test_status_names_no_repo_with_one_repo(self):
        rig = self.rig(agents={"api-orch": self.AGENTS["api-orch"]}, repos=(ACME_API,))
        rig.repo(ACME_API).issue(2, title="Ship the API?", labels=[DECISION_LABEL])
        rig.pump(70)
        rig.world.engine.run_pass()
        text = "\n".join(sb.format_decisions_status(sb.decisions_summary(rig.world.state, rig.world.clock.now)))
        self.assertIn("1 waiting on you", text)
        self.assertNotIn("acme", text)

    def test_decisions_fresh_reads_every_watched_repo_with_its_account(self):
        rig = self.rig(accounts=self.ACCOUNTS)
        self.label_both(rig)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": GH_BIN, "awaiting_label": DECISION_LABEL,
                           "gh_users": self.ACCOUNTS})
        route = lambda argv, **kw: rig.farm.runner(argv, **kw) if argv[0] == GH_BIN else rig.world.route(argv, **kw)
        report = sb.fresh_decisions(route, rig.world.clock.now)
        self.assertEqual(report["errors"], [])
        self.assertEqual(sorted(d["id"] for d in report["decisions"]), ["api#2", "web#2"])

    def block(self, rig, name, boss, repo):
        rig.world.add_agent(name, fleet={"role": "coder", "reports_to": boss, "issue": 2, "repo": repo})
        rig.world.v2.start_turn(rig.world.sessions[name], "fleet task", fleet=True, lasts=86400)
        rig.world.herdr.panes[rig.world.panes[name]]["status"] = "blocked"
        rig.world.v2.ask_permission(rig.world.sessions[name])

    def test_a_blocked_workers_decision_and_toast_and_pending_name_its_repo(self):
        rig = self.rig()
        self.block(rig, "w-api", "api-orch", ACME_API)
        self.block(rig, "w-web", "web-orch", ACME_WEB)
        found, _ = self.derive(rig)
        self.assertEqual(sorted((d["repo"], d["id"][:5]) for d in found), [(ACME_API, "api#2"), (ACME_WEB, "web#2")])
        rig.pump(10)
        toasts = [t["body"] for t in rig.world.herdr.toasts]
        self.assertTrue(any("(issue api#2)" in body for body in toasts), toasts)
        self.assertTrue(any("(issue web#2)" in body for body in toasts), toasts)
        (admitted,) = rig.world.admissions("web-orch")  # what `pending` would have shown, delivered by the pass
        self.assertIn("from w-web (issue web#2)", admitted["text"])
        self.assertNotIn("api#2", admitted["text"])

    def test_a_reports_issue_tag_is_read_back_to_its_repo(self):
        match = sb.REPORT_LINE.match("- report from w-web (issue web#2): question: which db?")
        self.assertEqual(match.groups(), ("w-web", "web", "2", "question", "which db?"))
        bare = sb.REPORT_LINE.match("- report from coder (issue #2): question: which db?")
        self.assertEqual(bare.groups(), ("coder", None, "2", "question", "which db?"))
        self.assertEqual(sb.repo_of_tag("web", [ACME_API, ACME_WEB]), ACME_WEB)
        self.assertEqual(sb.repo_of_tag("beta/site", ["acme/site", "beta/site"]), "beta/site")
        self.assertIsNone(sb.repo_of_tag("site", ["acme/site", "beta/site"]), "an ambiguous short tag names nobody")

    def test_the_decision_id_helper_prefixes_the_tag(self):
        items = [{"seed": "a", "ask": 2, "agent": "x", "bare": True, "since": EPOCH, "repo": ACME_API},
                 {"seed": "b", "ask": 2, "agent": "x", "bare": True, "since": EPOCH, "repo": ACME_WEB},
                 {"seed": "c", "ask": 2, "agent": "x", "bare": False, "since": EPOCH, "repo": ACME_WEB}]
        ids = sb.assign_decision_ids(items, [ACME_API, ACME_WEB])
        self.assertEqual(ids[:2], ["api#2", "web#2"])
        self.assertRegex(ids[2], r"^web#2[a-z]{2}$")


class StatusCase(KeyCase):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=())
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "jev": {}})

    def status(self, *flags):
        with captured() as (out, err):
            code = sb.main(["status"] + list(flags), runner=self.world.route, now=self.world.clock.now)
        return code, out.getvalue()


class TestStatusSaysWhenThereIsNoKey(StatusCase):
    def test_a_missing_key_is_the_first_line_in_plain_words(self):
        code, text = self.status()
        self.assertEqual(text.splitlines()[0], PR16_NOTICE)
        self.assertEqual(code, 0, "a missing key is a warning, not a failure of the switchboard")

    def test_a_key_in_the_variable_or_the_file_removes_the_line(self):
        os.environ["OPENROUTER_API_KEY"] = "v"
        self.assertTrue(self.status()[1].startswith("fleet-switchboard "))
        del os.environ["OPENROUTER_API_KEY"]
        self.put_key()
        text = self.status()[1]
        self.assertTrue(text.startswith("fleet-switchboard "))
        self.assertIn("key        file", text)

    def test_a_refused_file_is_still_no_key_and_says_why(self):
        self.put_key(mode=0o644)
        text = self.status()[1]
        self.assertEqual(text.splitlines()[0], PR16_NOTICE)
        self.assertIn("0600", text)

    def test_a_replayed_decision_model_needs_no_key(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "jev": {"replay": str(JEV_REPLAY)}})
        self.assertTrue(self.status()[1].startswith("fleet-switchboard "))

    def test_no_decision_model_configured_is_not_this_warning(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.assertTrue(self.status()[1].startswith("fleet-switchboard "))

    def test_the_json_says_where_the_key_comes_from_and_never_what_it_is(self):
        code, text = self.status("--json")
        self.assertEqual(json.loads(text)["key"], {"source": "none", "cli": "none", "daemon": None, "file": False,
                                                   "notice": PR16_NOTICE, "problem": None})
        self.put_key()
        code, text = self.status("--json")
        self.assertEqual(json.loads(text)["key"]["source"], "file")
        self.assertNotIn(PR16_KEY, text)

    def test_the_fault_key_missing_silent_hides_the_line_and_only_in_the_lab(self):
        os.environ[sb.FAULT_ENV] = "key-missing-silent"
        os.environ.pop(sb.LAB_ENV, None)
        self.assertEqual(self.status()[1].splitlines()[0], PR16_NOTICE, "outside the Lab a fault does nothing")
        os.environ[sb.LAB_ENV] = "1"
        text = self.status()[1]
        self.assertTrue(text.startswith("fleet-switchboard "))
        self.assertNotIn("No decision-model key", text)
        self.assertIn("key-missing-silent", sb.KNOWN_FAULTS)

    def test_status_names_the_daemon_the_key_the_plugin_and_the_service(self):
        text = self.status()[1]
        for label in ("daemon", "key", "plugin", "service"):
            self.assertRegex(text, r"(?m)^      %s +\S" % label)
        self.assertIn("daemon     not running", text)
        self.assertIn("service    not installed", text)


class BootstrapCase(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=())
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.work = self.home / "chief-dir"
        self.work.mkdir()

    def bootstrap(self, *flags):
        with captured() as (out, err):
            code = sb.main(["bootstrap"] + list(flags), runner=self.world.route, now=self.world.clock.now,
                           socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def cos_sessions(self):
        return [sid for sid, s in self.world.v2.sessions.items() if (s.get("metadata") or {}).get("fleet", {}).get("name") == "cos"]


class TestBootstrap(BootstrapCase):
    def test_the_first_run_opens_a_focused_workspace_and_launches_the_chief_of_staff(self):
        code, out, err = self.bootstrap("--dir", str(self.work))
        self.assertEqual(code, 0, err)
        made = self.world.herdr.created_workspaces
        self.assertEqual(len(made), 1)
        self.assertEqual((made[0]["label"], made[0]["cwd"], made[0]["focus"]), ("Chief of Staff", str(self.work), True))
        done = json.loads(out)
        self.assertEqual(done["action"], "created")
        [session] = self.cos_sessions()
        info = self.world.v2.sessions[session]
        self.assertEqual(info["metadata"]["fleet"], {"name": "cos", "role": "chief-of-staff"})
        self.assertEqual(done["session"], session)
        self.assertEqual(done["workspace"], made[0]["id"])

    def test_the_brief_tells_it_to_look_and_to_say_plainly_what_is_missing(self):
        self.bootstrap("--dir", str(self.work))
        [session] = self.cos_sessions()
        text = self.world.v2.sessions[session]["messages"][0]["text"]
        for needle in ("fleet-switchboard status", "key", "repos", "Introduce yourself", "work on"):
            self.assertIn(needle, text)
        self.assertNotIn("reports_to", text)

    def test_it_closes_only_the_empty_shell_pane_the_new_workspace_came_with(self):
        self.bootstrap("--dir", str(self.work))
        made = self.world.herdr.created_workspaces[0]
        self.assertEqual(len(self.world.herdr.closed), 1)
        self.assertTrue(self.world.herdr.closed[0].startswith(made["id"] + ":"))
        self.assertEqual(len(self.world.herdr.panes), 1)

    def test_a_second_chief_of_staff_is_refused_naming_the_one_that_runs(self):
        self.bootstrap("--dir", str(self.work))
        before = (len(self.world.herdr.created_workspaces), len(self.cos_sessions()))
        [pane] = list(self.world.herdr.panes)
        code, out, err = self.bootstrap("--dir", str(self.work))
        self.assertEqual(code, 1)
        self.assertIn("cos", err)
        self.assertIn(pane, err)
        self.assertEqual((len(self.world.herdr.created_workspaces), len(self.cos_sessions())), before)

    def test_with_the_pane_gone_the_session_is_resumed_in_its_workspace(self):
        first = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.world.herdr.panes.clear()
        code, out, err = self.bootstrap("--dir", str(self.work))
        self.assertEqual(code, 0, err)
        again = json.loads(out)
        self.assertEqual((again["action"], again["session"], again["workspace"]),
                         ("resumed", first["session"], first["workspace"]))
        self.assertEqual(self.cos_sessions(), [first["session"]], "no second session")
        self.assertEqual(len(self.world.herdr.created_workspaces), 1, "no second workspace")

    def test_a_session_whose_workspace_is_gone_gets_a_new_workspace_and_is_resumed(self):
        first = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.world.herdr.panes.clear()
        del self.world.herdr.workspaces[first["workspace"]]
        again = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.assertEqual((again["action"], again["session"]), ("resumed", first["session"]))
        self.assertNotEqual(again["workspace"], first["workspace"])

    def test_a_workspace_that_shares_the_label_is_never_taken_for_the_first_run(self):
        self.world.herdr.workspaces["wOLD"] = {"workspace_id": "wOLD", "label": "Chief of Staff", "number": 9}
        done = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.assertNotEqual(done["workspace"], "wOLD")
        self.assertEqual(len(self.world.herdr.created_workspaces), 1)

    def test_no_focus_asks_herdr_not_to_take_it(self):
        self.bootstrap("--dir", str(self.work), "--no-focus")
        self.assertIs(self.world.herdr.created_workspaces[0]["focus"], False)
        self.assertIn("--no-focus", self.world.herdr.created_workspaces[0]["argv"])

    def test_the_label_is_a_choice(self):
        self.bootstrap("--dir", str(self.work), "--workspace", "Fleet Office")
        self.assertEqual(self.world.herdr.created_workspaces[0]["label"], "Fleet Office")

    def test_a_directory_that_does_not_exist_is_refused_before_anything_opens(self):
        code, out, err = self.bootstrap("--dir", str(self.home / "nope"))
        self.assertEqual(code, 1)
        self.assertEqual(self.world.herdr.created_workspaces if hasattr(self.world.herdr, "created_workspaces") else [], [])

    def test_it_reuses_the_launch_and_resume_code_and_adds_no_launch_of_its_own(self):
        source = inspect.getsource(sb.bootstrap_cos)
        self.assertIn("launch_agent(", source)
        self.assertIn("resume_agent(", source)
        self.assertNotIn("open_tab_running", source)
        self.assertNotIn("session_create", source)


class TestServiceTemplates(Base):
    TEMPLATES = ("com.fleet-kit.switchboard.plist", "fleet-kit-switchboard.service")

    def values(self):
        return sb.service_values(REPO, ["/opt/tools/bin", "/usr/bin"])

    def test_the_plist_passes_plutil_and_plistlib(self):
        import plistlib
        text = sb.render_service("launchd", self.values(), REPO)
        body = plistlib.loads(text.encode())
        self.assertEqual(body["Label"], "com.fleet-kit.switchboard")
        self.assertIs(body["RunAtLoad"], True)
        self.assertIs(body["KeepAlive"], True)
        self.assertEqual(body["ProgramArguments"][-2:], ["run", "--standby"])
        self.assertTrue(body["ProgramArguments"][2].endswith("/bin/fleet-switchboard"))
        self.assertTrue(body["StandardErrorPath"].startswith(str(sb.state_dir_path())))
        self.assertTrue(body["StandardOutPath"].startswith(str(sb.state_dir_path())))
        self.assertIn("/opt/tools/bin", body["EnvironmentVariables"]["PATH"].split(":"))
        if not shutil.which("plutil"):
            self.skipTest("plutil is macOS only")
        done = subprocess.run(["plutil", "-lint", "-"], input=text.encode(), capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_the_unit_parses_and_keeps_the_daemon_alive(self):
        import configparser
        text = sb.render_service("systemd", self.values(), REPO)
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string(text)
        service = parser["Service"]
        self.assertIn("run --standby", service["ExecStart"])
        self.assertEqual((service["Restart"], parser["Install"]["WantedBy"]), ("always", "default.target"))
        self.assertIn("/opt/tools/bin", service["Environment"] + text)
        self.assertNotIn("/dev/null", "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))

    def test_neither_holds_a_secret_or_an_unfilled_placeholder_or_a_personal_name(self):
        os.environ["OPENROUTER_API_KEY"] = PR16_KEY
        for kind, name in (("launchd", self.TEMPLATES[0]), ("systemd", self.TEMPLATES[1])):
            rendered = sb.render_service(kind, self.values(), REPO)
            self.assertNotIn(PR16_KEY, rendered)
            self.assertNotRegex(rendered, r"(?i)api_key|secret|token|password|bearer")
            bare = re.sub(r"<!--.*?-->|#.*", "", rendered, flags=re.S)
            for value in sorted({str(v) for v in self.values().values()}, key=len, reverse=True):
                bare = bare.replace(value, "")  # #93: a filled-in path (a runner's `/__w/...`) is not a placeholder
            self.assertIsNone(re.search(r"__[A-Z][A-Z0-9_]*__", bare), "an unfilled placeholder in %s" % name)
            template = (REPO / "templates" / name).read_text()
            for number, line in enumerate(template.splitlines(), 1):
                self.assertIsNone(FORBIDDEN_IN_PUBLIC_TEXT.search(line), "%s:%d %s" % (name, number, line))

    def test_a_runner_path_with_double_underscores_is_not_a_placeholder(self):
        """#93: CI checkouts live under paths like `/home/runner/work/_temp/__w/repo`."""
        repo = self.home / "work" / "__w" / "repo"
        shutil.copytree(REPO / "templates", repo / "templates")
        for kind in ("launchd", "systemd"):
            rendered = sb.render_service(kind, sb.service_values(repo, ["/opt/tools/bin", "/usr/bin"]), repo)
            self.assertIn("/__w/", rendered)
            bare = re.sub(r"<!--.*?-->|#.*", "", rendered, flags=re.S)
            for value in sorted({str(v) for v in sb.service_values(repo, ["/opt/tools/bin"]).values()}, key=len, reverse=True):
                bare = bare.replace(value, "")
            self.assertIsNone(re.search(r"__[A-Z][A-Z0-9_]*__", bare))

    def test_the_templates_never_send_output_to_the_void(self):
        plist = re.sub(r"<!--.*?-->", "", (REPO / "templates" / self.TEMPLATES[0]).read_text(), flags=re.S)
        self.assertNotIn("/dev/null", plist)
        self.assertIn("launchd-stderr.log", plist)
        self.assertIn("__STATE_DIR__", plist)

    def test_the_path_holds_the_tools_the_daemon_runs(self):
        dirs = sb.service_path_dirs(REPO, ["/opt/gh/bin/gh", "/opt/herdr/bin/herdr", "herdr"])
        for needed in (str(REPO / "bin"), "/opt/gh/bin", "/opt/herdr/bin", os.path.dirname(sys.executable), "/usr/bin"):
            self.assertIn(needed, dirs)
        self.assertEqual(len(dirs), len(set(dirs)))

    def test_run_standby_waits_for_a_held_lock_instead_of_exiting(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        state = sb.ensure_state_dir()
        holder = os.open(str(state / "run.lock"), os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        slept, ran = [], []

        def sleeper(seconds):
            slept.append(seconds)
            os.close(holder)  # the other daemon goes away
        world = World(self, agents=())
        with mock.patch.object(sb.time, "sleep", sleeper), mock.patch.object(sb, "run_daemon", lambda *a, **k: ran.append(1)), \
                captured():
            code = sb.main(["run", "--standby"], runner=world.route, now=world.clock.now)
        self.assertEqual((code, slept, ran), (0, [sb.STANDBY_SECONDS], [1]))


class TestPaneProgramBySymlink(Base):
    """A pane runs the resolved path of a binary the config names by its stable link: the same program."""

    def setUp(self):
        super().setUp()
        self.real = self.home / "Cellar" / "opencode" / "2.0.22" / "bin" / "opencode"
        self.real.parent.mkdir(parents=True)
        self.real.write_text("#!/bin/sh\n")
        self.link = self.home / "opt" / "bin" / "opencode"
        self.link.parent.mkdir(parents=True)
        os.symlink(str(self.real), str(self.link))

    def test_runs_program_compares_by_realpath_on_both_sides(self):
        spaced = self.home / "Application Support" / "bin" / "opencode"
        spaced.parent.mkdir(parents=True)
        spaced.write_text("#!/bin/sh\n")
        spaced_link = self.home / "opt" / "bin" / "opencode-spaced"
        os.symlink(str(spaced), str(spaced_link))
        other = str(self.home / "other" / "opencode")
        for cmdline, exe, expected in (
                (str(self.real) + " -s ses_1", str(self.link), True),
                (str(self.link) + " -s ses_1", str(self.real), True),
                (str(self.real), str(self.link), True),
                (str(self.link), str(self.link), True),
                (str(spaced) + " -s ses_1", str(spaced_link), True),
                (other + " -s ses_1", str(self.link), False),
                (str(self.real) + "-v1 -s ses_1", str(self.link), False),
                ("-zsh", str(self.link), False),
                (None, str(self.link), False)):
            with self.subTest(cmdline=cmdline, exe=exe):
                self.assertEqual(sb.runs_program(cmdline, exe), expected)

    def program(self, name, version, directory):
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo %r\nexit 0\n" % version)
        path.chmod(0o755)
        return path

    def test_a_pane_started_with_the_bare_command_is_the_configured_program_when_it_resolves_to_it(self):
        configured = self.program("opencode", "opencode 2.0.22", self.home / "Cellar" / "bin")
        link_dir = self.home / "brew" / "bin"
        link_dir.mkdir(parents=True)
        os.symlink(str(configured), str(link_dir / "opencode"))
        other_dir = self.home / "other" / "bin"
        self.program("opencode", "opencode 9.9.9", other_dir)
        copy_dir = self.home / "copy" / "bin"
        self.program("opencode", "opencode 2.0.22", copy_dir)
        self.program("node", "opencode 2.0.22", self.home / "node" / "bin")                 # another name, same --version text
        for label, path, exe, cmdline, expected in (
                ("bare name through a symlink, configured by the link", str(link_dir), str(link_dir / "opencode"),
                 "opencode -s ses_x", True),
                ("bare name, configured by the real file", str(link_dir), str(configured), "opencode -s ses_x", True),
                ("bare name alone", str(link_dir), str(configured), "opencode", True),
                ("absolute and equal (unchanged)", "", str(configured), str(configured) + " -s ses_x", True),
                ("a genuinely different opencode on the path", str(other_dir), str(configured), "opencode -s ses_x", False),
                ("a copy with the same version", str(copy_dir), str(configured), "opencode -s ses_x", True),
                ("a bare name that is not on the path", str(self.home / "empty"), str(configured), "opencode -s ses_x", False),
                ("a shell", "/bin:/usr/bin", str(configured), "zsh", False),
                ("a login shell", "/bin:/usr/bin", str(configured), "-zsh", False),
                ("another program with the same version text", str(self.home / "node" / "bin"), str(configured),
                 "node app.js", False),
                ("a relative path", str(link_dir), str(configured), "./opencode -s x", False),
                ("nothing", str(link_dir), str(configured), None, False)):
            with self.subTest(label):
                with mock.patch.dict(os.environ, {"PATH": path}):
                    self.assertEqual(sb.runs_program(cmdline, exe), expected)

    def test_the_version_is_asked_for_once_per_file_and_never_blocks(self):
        one = self.program("opencode", "opencode 1", self.home / "v" / "bin")
        sb._VERSIONS.clear()
        calls = []
        real = sb.subprocess.run

        def counting(argv, **kwargs):
            calls.append(list(argv))
            return real(argv, **kwargs)
        with mock.patch.object(sb.subprocess, "run", counting):
            self.assertEqual((sb.program_version(str(one)), sb.program_version(str(one))), ("opencode 1", "opencode 1"))
            self.assertEqual(len(calls), 1)
            slow = self.program("slow", "x", self.home / "s" / "bin")
            slow.write_text("#!/bin/sh\nsleep 30\n")
            with mock.patch.object(sb, "PROGRAM_VERSION_SECONDS", 0.3):
                self.assertIsNone(sb.program_version(str(slow)))                           # bounded, and undecided is held
        self.assertIsNone(sb.program_version(str(self.home / "missing")))

    def test_a_cos_pane_launched_with_the_bare_command_is_not_held_but_a_different_opencode_is(self):
        world = World(self, agents=("platform",))
        engine, pane, session = world.engine, world.panes["platform"], world.sessions["platform"]
        world.herdr.panes[pane]["program"] = "opencode -s %s" % session
        with mock.patch.object(sb.shutil, "which", return_value=FAKE_OPENCODE):
            self.assertFalse(engine.discover(world.clock.now()).agents["platform"].wrong_program)
            self.assertIsNone(sb.pane_problem(engine, pane, session))
        with mock.patch.object(sb.shutil, "which", return_value="/elsewhere/bin/opencode"), \
                mock.patch.object(sb, "program_version", lambda path: "opencode 9" if "elsewhere" in path else "opencode 2"):
            seen = engine.discover(world.clock.now()).agents["platform"]
            self.assertTrue(seen.wrong_program)
            self.assertIn("runs a different opencode", seen.problem)

    def test_a_pane_on_the_resolved_path_is_not_wrong_for_discovery_or_for_launch_verification(self):
        world = World(self, agents=("platform",))
        engine, pane, session = world.engine, world.panes["platform"], world.sessions["platform"]
        engine.executable = str(self.link)
        world.herdr.panes[pane]["program"] = "%s -s %s" % (self.real, session)
        self.assertIsNone(sb.pane_problem(engine, pane, session))
        self.assertFalse(engine.discover(world.clock.now()).agents["platform"].wrong_program)
        engine.executable = str(self.real)
        world.herdr.panes[pane]["program"] = "%s -s %s" % (self.link, session)
        self.assertIsNone(sb.pane_problem(engine, pane, session))
        self.assertFalse(engine.discover(world.clock.now()).agents["platform"].wrong_program)

    def test_another_binary_is_still_a_wrong_program(self):
        world = World(self, agents=("platform",))
        engine, pane, session = world.engine, world.panes["platform"], world.sessions["platform"]
        engine.executable = str(self.link)
        world.herdr.panes[pane]["program"] = "%s -s %s" % (self.home / "other" / "opencode", session)
        self.assertIn("not the configured v2", sb.pane_problem(engine, pane, session))
        self.assertTrue(engine.discover(world.clock.now()).agents["platform"].wrong_program)


class TestBootstrapSendsTheBriefOnce(BootstrapCase):
    """The first attempt can die after the session is made and before its brief is: the resume must still send it."""

    def half_made(self):
        """A cos session with no pane and no brief: what a bootstrap that died after session.create leaves."""
        session = self.world.v2.create_session({"fleet": {"name": "cos", "role": "chief-of-staff"}}, "cos",
                                               "chief-of-staff", str(self.work))
        return session

    def briefs(self, session):
        return [m for m in self.world.v2.sessions[session]["messages"]
                if ((m.get("metadata") or {}).get("fleet") or {}).get("brief")]

    def test_a_resume_of_a_session_with_no_brief_sends_it_once(self):
        session = self.half_made()
        code, out, err = self.bootstrap("--dir", str(self.work))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["action"], "resumed")
        [brief] = self.briefs(session)
        self.assertEqual(brief["text"], sb.COS_BRIEF)

    def test_a_resume_of_a_session_that_holds_the_brief_sends_nothing(self):
        first = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.assertEqual(len(self.briefs(first["session"])), 1)
        self.world.herdr.panes.clear()
        before = len(self.world.v2.sessions[first["session"]]["messages"])
        again = json.loads(self.bootstrap("--dir", str(self.work))[1])
        self.assertEqual(again["action"], "resumed")
        self.assertEqual(len(self.world.v2.sessions[first["session"]]["messages"]), before)
        self.assertEqual(len(self.briefs(first["session"])), 1)

    def test_a_third_run_after_the_repair_sends_nothing_more(self):
        session = self.half_made()
        self.bootstrap("--dir", str(self.work))
        self.world.herdr.panes.clear()
        self.bootstrap("--dir", str(self.work))
        self.world.herdr.panes.clear()
        self.bootstrap("--dir", str(self.work))
        self.assertEqual(len(self.briefs(session)), 1)

    def test_a_message_that_is_not_the_brief_does_not_count_as_one(self):
        session = self.half_made()
        self.world.v2.start_turn(session, "hello", fleet=False)
        self.bootstrap("--dir", str(self.work))
        self.assertEqual(len(self.briefs(session)), 1)

    def test_a_failed_send_closes_the_pane_it_opened_and_a_new_run_sends_it(self):
        session = self.half_made()
        self.world.v2.op_session_prompt = mock.Mock(side_effect=FakeHTTP(500, "Boom", "no"))
        code, out, err = self.bootstrap("--dir", str(self.work))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.briefs(session), [])
        self.assertEqual(len(self.world.herdr.closed), 1)
        del self.world.v2.op_session_prompt
        self.assertEqual(self.bootstrap("--dir", str(self.work))[0], 0)
        self.assertEqual(len(self.briefs(session)), 1)

    def test_a_listing_too_long_to_read_to_the_end_is_taken_to_hold_the_brief(self):
        class Long:
            def message_list(self, session, limit=None, order=None, type=None, cursor=None):
                return [{"id": "msg_%d" % n, "type": "user"} for n in range(limit)]   # a full page of the oldest user messages and no brief among them

        class Engine:
            v2 = Long()
        self.assertTrue(sb.brief_in_transcript(Engine(), "ses_x"), "more user messages than the read covers: taken to hold the brief")

        class Short:
            def message_list(self, session, limit=None, order=None, type=None, cursor=None):
                return [{"id": "msg_0", "type": "user"}]

        class Engine2:
            v2 = Short()
        self.assertFalse(sb.brief_in_transcript(Engine2(), "ses_x"), "a short listing with no brief: not sent yet")

    def test_launch_and_bootstrap_send_it_by_the_one_function(self):
        self.assertIn("send_brief(", inspect.getsource(sb.launch_agent))
        self.assertIn("resume_brief(", inspect.getsource(sb.bootstrap_cos))
        self.assertIn("send_brief(", inspect.getsource(sb.resume_brief))
        self.assertNotIn("brief", inspect.getsource(sb.resume_agent).replace("no brief is sent", ""))


class TestTheDaemonKnowsItsKey(StatusCase):
    """`status` is run from a shell that may hold the key; the daemon, started by launchd or systemd, may not."""

    def setUp(self):
        super().setUp()
        self.held = []
        self.addCleanup(self.release)

    def release(self):
        for fd in self.held:
            os.close(fd)

    def daemon_started_with(self, source, pid=None):
        state = sb.ensure_state_dir()
        fd = os.open(str(state / "run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())
        self.held.append(fd)
        (state / "daemon.json").write_text(json.dumps({"pid": pid or os.getpid(), "key_source": source}))

    def service_installed(self):
        kind = sb.service_kind()
        if kind is None:
            self.skipTest("no service manager on this platform")
        path = sb.service_file_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")

    def first(self):
        return self.status()[1].splitlines()[0]

    def test_the_daemon_writes_where_its_key_came_from_beside_its_pid(self):
        world = World(self, agents=())
        sb.run_daemon(world.engine, world.state, 5, wait=lambda sock, timeout: None, passes=1,
                      record={"key_source": "environment"})
        record = json.loads((world.state / "daemon.json").read_text())
        self.assertEqual((record["pid"], record["key_source"]), (os.getpid(), "environment"))

    def test_the_source_is_one_of_file_environment_or_none(self):
        settings = dict(sb.JEV_DEFAULTS)
        config = {"jev": settings}
        self.assertEqual(sb.key_source_of(config, {}), "none")
        self.assertEqual(sb.key_source_of(config, {sb.JEV_KEY_ENV: "v"}), "environment")
        self.put_key()
        self.assertEqual(sb.key_source_of(config, {}), "file")
        self.assertEqual(sb.daemon_record(config, {}), {"key_source": "file"})

    def test_run_hands_the_record_to_the_daemon(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "jev": {}})
        seen = []
        os.environ["OPENROUTER_API_KEY"] = "v"
        world = World(self, agents=())
        with mock.patch.object(sb, "run_daemon", lambda *a, **k: seen.append(k)), \
                mock.patch.object(sb, "start_judge_server", lambda *a, **k: None), captured():
            sb.main(["run"], runner=world.route, now=world.clock.now)
        self.assertEqual(seen[0]["record"]["key_source"], "environment")

    def test_a_shell_with_the_key_and_a_daemon_without_it_is_the_first_line_in_plain_words(self):
        self.daemon_started_with("none")
        os.environ["OPENROUTER_API_KEY"] = "v"
        code, text = self.status()
        line = text.splitlines()[0]
        self.assertEqual(code, 0)
        for needle in ("this shell", "daemon", "fleet-switchboard key set"):
            self.assertIn(needle, line)
        self.assertNotIn("v\n", text)

    def test_status_reports_the_daemons_source_not_the_shells(self):
        self.daemon_started_with("none")
        os.environ["OPENROUTER_API_KEY"] = "v"
        report = json.loads(self.status("--json")[1])["key"]
        self.assertEqual((report["source"], report["cli"], report["daemon"]), ("none", "environment", "none"))
        self.assertIn("key        none", self.status()[1])

    def test_a_service_and_a_key_only_in_the_shell_says_the_service_will_not_have_it(self):
        self.service_installed()
        os.environ["OPENROUTER_API_KEY"] = "v"
        line = self.first()
        for needle in ("service", "shell", "fleet-switchboard key set"):
            self.assertIn(needle, line)

    def test_a_daemon_started_with_the_key_but_a_service_and_no_file_still_says_so(self):
        self.service_installed()
        self.daemon_started_with("environment")
        line = self.first()
        self.assertIn("fleet-switchboard key set", line)
        self.assertIn("service", line)

    def test_a_saved_key_file_removes_the_warning(self):
        self.service_installed()
        self.daemon_started_with("environment")
        os.environ["OPENROUTER_API_KEY"] = "v"
        self.put_key()
        text = self.status()[1]
        self.assertTrue(text.startswith("fleet-switchboard "), text.splitlines()[0])
        self.assertIn("key        environment", text)

    def test_a_running_daemon_with_a_file_key_is_quiet(self):
        self.put_key()
        self.daemon_started_with("file")
        text = self.status()[1]
        self.assertTrue(text.startswith("fleet-switchboard "))
        self.assertIn("key        file", text)

    def test_the_variable_in_the_shell_and_nothing_else_names_where_it_lives(self):
        os.environ["OPENROUTER_API_KEY"] = "v"
        text = self.status()[1]
        self.assertIn("key        environment (", text)
        self.assertIn("key set", text)

    def test_no_key_anywhere_is_the_old_notice_even_with_a_daemon_that_says_none(self):
        self.daemon_started_with("none")
        self.assertEqual(self.first(), PR16_NOTICE)

    def test_a_record_from_another_process_is_not_the_running_daemons(self):
        self.daemon_started_with("environment", pid=os.getpid() + 7)
        self.put_key()
        report = json.loads(self.status("--json")[1])["key"]
        self.assertEqual((report["source"], report["daemon"]), ("file", None))

    def test_a_record_with_no_daemon_running_is_not_read(self):
        (sb.ensure_state_dir() / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "key_source": "environment"}))
        self.assertEqual(self.first(), PR16_NOTICE)

    def test_the_fault_hides_the_unseen_key_warning_and_only_in_the_lab(self):
        self.daemon_started_with("none")
        os.environ["OPENROUTER_API_KEY"] = "v"
        os.environ[sb.FAULT_ENV] = "key-env-only-unseen"
        os.environ.pop(sb.LAB_ENV, None)
        self.assertIn("fleet-switchboard key set", self.first(), "outside the Lab a fault does nothing")
        os.environ[sb.LAB_ENV] = "1"
        text = self.status()[1]
        self.assertTrue(text.startswith("fleet-switchboard "), "the control: the fault removes the warning")
        self.assertNotIn("key set", text.split("key        ")[0])
        self.assertIn("key-env-only-unseen", sb.KNOWN_FAULTS)

    def test_key_status_says_when_the_variable_is_all_there_is(self):
        os.environ["OPENROUTER_API_KEY"] = "v"
        with captured() as (out, err):
            sb.main(["key", "status"])
        self.assertEqual(out.getvalue(), "environment (only this shell)\n")
        self.put_key()
        with captured() as (out, err):
            sb.main(["key", "status"])
        self.assertEqual(out.getvalue(), "environment\n")


class TestASlowCatchUpDoesNotFreezeThePass(Base):
    """Found on a real machine: a first catch-up of a busy repo (thousands of items nobody owns, and a workflow
    read that timed out) kept the pass busy for minutes, and `decisions` said the daemon was not updating."""

    ITEMS = 1200

    def rig(self):
        rig = GithubRig(self, config={"triage": False})  # the pass budget under test is the routing's; triage has its own (pass_seconds)
        self.calls = 0
        original = rig.gh.runner

        def slow(argv, **kwargs):
            self.calls += 1
            rig.world.clock.t += 1  # a gh call that takes a second
            if "actions/runs" in argv[-1]:
                rig.world.clock.t += 19
                raise subprocess.TimeoutExpired(argv, 20)
            return original(argv, **kwargs)
        rig.hub.runner = slow
        rig.hub.markers[GH_REPO] = rig.world.clock.now() - 3600  # a repo that was read an hour ago: only its gap is read
        for number in range(100, 100 + self.ITEMS):
            rig.gh.comment(number, "nobody owns this", 6000 + number, forward=False)
        return rig

    def pass_seconds(self, rig):
        before = rig.world.clock.now()
        rig.world.engine.run_pass()
        return rig.world.clock.now() - before

    def test_the_pass_stays_inside_its_budget_whatever_is_waiting(self):
        rig = self.rig()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertLessEqual(len(rig.hub.queue), sb.GITHUB_PAGE_CAP * sb.GITHUB_PAGE_SIZE)
        took = self.pass_seconds(rig)
        self.assertLessEqual(took, sb.GITHUB_ROUTE_SECONDS + sb.GITHUB_ROUTE_READ_TIMEOUT, took)
        self.assertGreater(len(rig.hub.queue), 0, "what did not fit waits for the next pass")

    def test_the_decisions_file_is_never_stale_because_of_it(self):
        rig = self.rig()
        rig.world.engine.run_pass()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        for _ in range(3):
            self.pass_seconds(rig)
            self.assertFalse(sb.decisions_report(rig.world.state, rig.world.clock.now())["stale"])

    def test_it_drains_over_passes_and_audits_the_unowned_in_counts_not_one_line_each(self):
        rig = self.rig()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        kept = len(rig.hub.queue)
        for _ in range(kept):
            if not rig.hub.queue:
                break
            rig.world.engine.run_pass()
        self.assertEqual(len(rig.hub.queue), 0)
        lines = [e for e in rig.world.audit() if e["event"] == "github.unrouted"]
        self.assertEqual(sum(e["detail"]["count"] for e in lines), kept)
        self.assertLess(len(lines), kept // 4)
        for e in lines:
            self.assertLessEqual(len(e["detail"]["keys"]), sb.GITHUB_AUDIT_KEYS)

    def test_one_catch_up_keeps_the_newest_items_only_and_says_so(self):
        rig = self.rig()
        with mock.patch.object(sb, "GITHUB_CATCHUP_ITEMS", 30):
            rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertIn("items: kept the newest", rig.watch().catch_up_error)
        [line] = [e for e in rig.world.audit() if e["event"] == "github.catch_up"][-1:]
        self.assertEqual(line["detail"]["count"], 30)
        self.assertLessEqual(len(line["detail"]["keys"]), sb.GITHUB_AUDIT_KEYS)
        [cut] = [e for e in rig.world.audit() if e["event"] == "github.catch_up.truncated"]
        self.assertGreater(cut["detail"]["items_skipped"], 0)

    def test_a_read_that_times_out_is_reported_once_and_not_tried_every_pass(self):
        rig = self.rig()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        runs = lambda: len([c for c in rig.gh.api_calls if "actions/runs" in c])
        self.assertEqual(runs(), 0, "the fake never saw it: the slow runner raised first")
        tried = [0]
        base = rig.hub.runner

        def count(argv, **kwargs):
            if "actions/runs" in argv[-1]:
                tried[0] += 1
            return base(argv, **kwargs)
        rig.hub.runner = count
        for _ in range(5):
            rig.world.clock.t += sb.GITHUB_LOOKUP_RETRY + 5
            rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertEqual(tried[0], 0, "inside its backoff it is not tried again")
        self.assertIsNone(rig.watch().catchup_at, "a best-effort read is not retried for the window it missed")
        rig.world.clock.t += sb.GITHUB_READ_BACKOFF
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertEqual(tried[0], 1, "after it, once")

    def test_a_catch_up_stops_reading_when_its_time_is_spent(self):
        rig = GithubRig(self)
        original = rig.gh.runner

        def slow(argv, **kwargs):
            rig.world.clock.t += 60
            return original(argv, **kwargs)
        rig.hub.runner = slow
        rig.hub.markers[GH_REPO] = rig.world.clock.now() - 3600
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertIn("left to the next catch-up", rig.watch().catch_up_error)

    def test_the_fault_removes_every_bound_and_the_control_fails(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "catch-up-blocks-pass"
        rig = self.rig()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertGreater(len(rig.hub.queue), sb.GITHUB_CATCHUP_ITEMS)
        took = self.pass_seconds(rig)
        self.assertGreater(took, sb.DECISIONS_STALE_SECONDS, "with the bounds off the pass is the minutes the owner saw")
        self.assertIn("catch-up-blocks-pass", sb.KNOWN_FAULTS)


class TestTheConfigIsReloadedWhenTheFileChanges(Base):
    def setUp(self):
        super().setUp()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 5})
        self.clock = SimClock()
        self.records = []
        self.world = World(self, agents=())
        os.environ.pop(sb.LAB_ENV, None)
        self.watch = sb.ConfigWatch(self.config_file, sb.load_runnable_config(), self.clock.now,
                                    sb.AuditLog(self.world.audit_path), "daemon",
                                    lambda **fields: self.records.append(fields))

    def edit(self, obj):
        self.write_config(obj)
        info = self.config_file.stat()
        os.utime(str(self.config_file), (info.st_atime, info.st_mtime + len(self.records) + 1 + self.clock.t % 7))

    def audit(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def settle(self):
        """The file has stayed as it is for longer than the stability window."""
        self.assertIsNone(self.watch.check())
        self.clock.t += sb.CONFIG_STABLE_SECONDS + 0.5
        return self.watch.check()

    def test_an_unchanged_file_is_never_read_again(self):
        for _ in range(3):
            self.clock.t += 5
            self.assertIsNone(self.watch.check())
        self.assertFalse(self.watch.pending)

    def test_a_valid_change_is_applied_once_it_has_been_still_for_a_second(self):
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 9, "github_repos": ["a/b"]})
        self.assertIsNone(self.watch.check())
        self.clock.t += 0.5
        self.assertIsNone(self.watch.check(), "not mid-write")
        self.clock.t += 1
        self.assertEqual(self.watch.check(), "reload")
        self.assertEqual(self.watch.changed, ["github_repos", "poll_seconds"], "names, never values")
        self.assertEqual(self.watch.new_config["github_repos"], ["a/b"])

    def test_two_quick_edits_are_one_reload(self):
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 6})
        self.assertIsNone(self.watch.check())
        self.clock.t += 0.8
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 7})
        self.assertIsNone(self.watch.check(), "it changed again: the window starts over")
        self.clock.t += 0.8
        self.assertIsNone(self.watch.check())
        self.clock.t += 0.5
        self.assertEqual(self.watch.check(), "reload")
        self.assertEqual(self.watch.new_config["poll_seconds"], 7)

    def test_a_touch_with_the_same_content_reloads_nothing(self):
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 5})
        self.assertIsNone(self.settle())
        self.assertEqual(self.audit("config.invalid"), [])

    def test_a_broken_file_never_stops_it_is_audited_once_and_recorded(self):
        self.edit("{ not json \"sk-secret-canary\" ")
        self.assertIsNone(self.settle())
        for _ in range(3):
            self.clock.t += 5
            self.assertIsNone(self.watch.check())
        [event] = self.audit("config.invalid")
        self.assertIn("not valid JSON", event["detail"]["reason"])
        self.assertNotIn("sk-secret-canary", json.dumps(event))
        self.assertEqual(self.records[-1]["config_invalid"]["reason"], event["detail"]["reason"])
        self.assertEqual(self.watch.config["poll_seconds"], 5, "still the config it loaded")

    def test_a_file_that_parses_but_is_not_a_valid_config_is_invalid_too(self):
        for bad in ({"opencode": FAKE_OPENCODE, "github_repos": "not-a-list"}, {"poll_seconds": 5}):
            with self.subTest(bad=bad):
                self.edit(bad)
                self.assertIsNone(self.settle())
                self.assertIsNotNone(self.watch.invalid)

    def test_a_declaration_that_goes_missing_is_refused_on_reload_and_the_last_good_config_keeps_running(self):
        with mock.patch.object(sb, "CONFIG_MUST_DECLARE", {"excluded_repos": "no_such_module"}):
            self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 9})
            self.assertIsNone(self.settle(), "refused: nothing to rebuild")
            self.assertIsNotNone(self.watch.invalid)
            self.assertIn("no_such_module.py", self.watch.invalid["reason"])

    def test_fixing_it_recovers_and_a_different_fix_reloads(self):
        self.edit("{")
        self.settle()
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 5})
        self.assertIsNone(self.settle(), "back to what it already runs: nothing to rebuild")
        self.assertIsNone(self.watch.invalid)
        self.assertEqual(self.records[-1], {"config_invalid": None})
        self.assertEqual(len(self.audit("config.recovered")), 1)
        self.edit("{")
        self.settle()
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 8})
        self.assertEqual(self.settle(), "reload")

    def test_the_fault_ignores_every_change_and_only_in_the_lab(self):
        os.environ[sb.FAULT_ENV] = "config-read-once"
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 9})
        self.assertEqual(self.settle(), "reload", "outside the Lab a fault does nothing")
        os.environ[sb.LAB_ENV] = "1"
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 11})
        self.assertIsNone(self.settle(), "the control: the change is ignored")
        self.assertIn("config-read-once", sb.KNOWN_FAULTS)

    def test_the_start_path_and_a_reload_use_one_validator(self):
        self.assertIn("load_runnable_config(", inspect.getsource(sb.build_engine))
        self.assertIn("load_runnable_config(", inspect.getsource(sb.ConfigWatch.check))

    def test_run_daemon_checks_every_pass_and_waits_a_second_while_a_change_settles(self):
        waits, reloads = [], []
        self.edit({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "poll_seconds": 9})
        self.watch.check()

        def wait(sock, timeout):
            waits.append(timeout)
            self.clock.t += 1.5
        sb.run_daemon(self.world.engine, self.world.state, 5, wait=wait, passes=2, watch=self.watch,
                      on_reload=reloads.append)
        self.assertEqual(waits[0], sb.CONFIG_STABLE_SECONDS)
        self.assertEqual(reloads, [self.watch])


class TestStatusShowsTheDaemonsConfig(TestTheDaemonKnowsItsKey):
    def test_the_daemon_records_when_it_loaded_and_status_shows_it(self):
        self.daemon_started_with("none")
        record = sb.ensure_state_dir() / "daemon.json"
        body = json.loads(record.read_text())
        body["config_loaded_at"] = "2026-10-06T10:00:00Z"
        record.write_text(json.dumps(body))
        self.assertIn("config     loaded 2026-10-06T10:00:00Z", self.status()[1])

    def test_an_invalid_file_is_a_first_class_line_with_both_times(self):
        self.daemon_started_with("none")
        record = sb.ensure_state_dir() / "daemon.json"
        body = json.loads(record.read_text())
        body.update(config_loaded_at="2026-10-06T10:00:00Z",
                    config_invalid={"since": "2026-10-06T10:05:00Z", "reason": "bad json", "loaded_at": "2026-10-06T10:00:00Z"})
        record.write_text(json.dumps(body))
        text = self.status()[1]
        self.assertIn("config     INVALID since 2026-10-06T10:05:00Z: bad json; still running on the config loaded at "
                      "2026-10-06T10:00:00Z", text)
        self.assertEqual(json.loads(self.status("--json")[1])["daemon"]["config_invalid"]["reason"], "bad json")

    def test_a_running_daemon_updates_its_record_and_a_restart_clears_the_invalid_mark(self):
        state = sb.ensure_state_dir()
        sb.update_daemon_record(state, config_invalid={"since": "t", "reason": "r", "loaded_at": "l"})
        self.assertEqual(json.loads((state / "daemon.json").read_text())["config_invalid"]["reason"], "r")
        world = World(self, agents=())
        sb.run_daemon(world.engine, state, 5, wait=lambda s, t: None, passes=1, record={"key_source": "none"})
        self.assertNotIn("config_invalid", json.loads((state / "daemon.json").read_text()))


class TestRestartCommand(StandInPids, Base):
    def setUp(self):
        super().setUp()
        self.held = []
        self.addCleanup(lambda: [os.close(fd) for fd in self.held])
        self.state = sb.ensure_state_dir()

    def hold(self, pid):
        fd = os.open(str(self.state / "run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, ("%d\n" % pid).encode())
        self.held.append(fd)
        return fd

    def run_restart(self, command, kill):
        runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, command + "\n", "")
        with captured() as (out, err):
            code = sb.cmd_restart(mock.Mock(), runner, spawn=mock.Mock(), kill=kill, sleep=lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def test_it_refuses_a_pid_that_is_not_a_switchboard_daemon_and_signals_nothing(self):
        self.hold(self.px[4242])
        signalled = []
        code, out, err = self.run_restart("/usr/bin/vim notes.txt", lambda pid, sig: signalled.append((pid, sig)))
        self.assertEqual((code, signalled), (1, []))
        self.assertIn("not a fleet-switchboard daemon", err)

    def test_it_stops_a_daemon_gracefully_by_its_pid_and_then_ensures(self):
        fd = self.hold(self.px[4242])
        signalled = []

        def kill(pid, sig):
            signalled.append((pid, sig))
            fcntl.flock(fd, fcntl.LOCK_UN)  # the daemon let go of its lock
            os.close(fd)
            self.held.remove(fd)
        spawned = []
        with mock.patch.object(sb, "ensure_daemon", lambda state, spawn: spawned.append(1) or "started"):
            code, out, err = self.run_restart("/usr/bin/python3 /kit/bin/fleet-switchboard run", kill)
        self.assertEqual((code, signalled, out.strip()), (0, [(self.px[4242], signal.SIGTERM)], "daemon started"))
        self.assertEqual(spawned, [1])

    def test_a_daemon_that_does_not_stop_is_not_killed_harder(self):
        self.hold(self.px[4242])
        signalled = []
        code, out, err = self.run_restart("python3 fleet-switchboard run", lambda pid, sig: signalled.append(sig))
        self.assertEqual((code, signalled), (1, [signal.SIGTERM]))
        self.assertIn("nothing stronger", err)

    def test_with_no_daemon_it_just_ensures(self):
        with mock.patch.object(sb, "ensure_daemon", lambda state, spawn: "started"):
            code, out, err = self.run_restart("", lambda pid, sig: self.fail("nothing to signal"))
        self.assertEqual((code, out.strip()), (0, "daemon started"))

    def test_the_command_exists_and_never_ensures_on_use(self):
        self.assertIn("restart", sb.build_parser()._subparsers._group_actions[0].choices)
        self.assertNotIn("restart", sb.ENSURE_ON_USE_COMMANDS)


class TestGithubReadsMoveForwardOnly(Base):
    """A read marker per repo: a fresh start reads no history, a restart reads only the gap, in bounded pages."""

    def rig(self, **kwargs):
        rig = GithubRig(self, **kwargs)
        self.rig_ = rig
        return rig

    def calls(self, rig, fragment):
        return [c for c in rig.gh.api_calls if fragment in c]

    def stamp(self, rig, ago):
        return sb.iso(rig.world.clock.now() - ago)

    def test_a_repo_with_no_marker_is_not_read_at_all_and_its_marker_is_set_to_now(self):
        rig = self.rig()
        rig.gh.comment(12, "old news", 5001, forward=False)
        rig.gh.pull(30)
        now = rig.world.clock.now()
        rig.hub.catch_up(rig.watch(), now)
        self.assertEqual(self.calls(rig, "issues/comments") + self.calls(rig, "/pulls") + self.calls(rig, "actions/runs"), [])
        self.assertEqual(rig.hub.markers[GH_REPO], now)
        self.assertEqual(rig.keys(), [])
        [line] = [e for e in rig.world.audit() if e["event"] == "github.catch_up"]
        self.assertIs(line["detail"]["fresh_start"], True)

    def test_the_awaiting_user_read_stays_on_a_fresh_start_and_is_paginated(self):
        rig = self.rig()
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        reads = self.calls(rig, "labels=")
        self.assertEqual(len(reads), 2, "PR 18: one read per label (yours, the Chief of Staff's), never one per issue")
        self.assertIn("page=1", reads[0])
        self.assertNotIn("--paginate", " ".join(map(str, reads)))

    def test_with_a_marker_only_the_gap_is_read_from_a_little_before_it(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        rig.gh.comment(12, "in the gap", 5002, forward=False)
        rig.hub.catch_up(rig.watch(), now)
        [first] = self.calls(rig, "issues/comments")[:1]
        self.assertIn("since=%s" % sb.iso(now - 600 - sb.GITHUB_MARKER_SLACK).replace(":", "%3A") if "%3A" in first
                      else "since=%s" % sb.iso(now - 600 - sb.GITHUB_MARKER_SLACK), first)
        self.assertEqual(rig.keys(), ["github.comment:5002"])
        self.assertEqual(rig.hub.markers[GH_REPO], now, "the marker moves to the start of a successful catch-up")

    def test_a_marker_older_than_the_look_back_is_clamped_and_says_so_once(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 5 * 86400
        rig.hub.catch_up(rig.watch(), now)
        [first] = self.calls(rig, "issues/comments")[:1]
        self.assertIn(sb.iso(now - 86400), first)
        [skipped] = [e for e in rig.world.audit() if e["event"] == "github.catch_up.skipped"]
        self.assertIn("skipped", skipped["detail"]["why"])

    def test_the_marker_is_a_private_file_written_once_a_pass_and_deleting_it_is_a_fresh_start(self):
        rig = self.rig()
        path = rig.world.state / sb.READ_MARKER_FILE
        writes = []
        original = os.replace
        with mock.patch.object(sb.os, "replace", lambda a, b: writes.append(str(b)) or original(a, b)):
            rig.hub.catch_up(rig.watch(), rig.world.clock.now())
            rig.hub.catch_up(rig.watch(), rig.world.clock.now() + 1)
            rig.hub.tick()
            rig.hub.tick()
        self.assertEqual([w for w in writes if w.endswith(sb.READ_MARKER_FILE)], [str(path)], "once, for the pass")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(json.loads(path.read_text())), [GH_REPO])
        self.assertEqual(sorted(p.name for p in rig.world.state.iterdir() if p.name.startswith("github-read")),
                         [sb.READ_MARKER_FILE])
        self.assertTrue(sb.GitHubHub(rig.world.config, rig.world.state, now=rig.world.clock.now).markers)
        path.unlink()
        self.assertEqual(sb.GitHubHub(rig.world.config, rig.world.state, now=rig.world.clock.now).markers, {})

    def test_a_marker_in_the_future_or_unreadable_is_not_trusted(self):
        rig = self.rig()
        path = rig.world.state / sb.READ_MARKER_FILE
        path.write_text(json.dumps({GH_REPO: self.stamp(rig, -9999), "x/y": "nonsense"}))
        hub = sb.GitHubHub(rig.world.config, rig.world.state, now=rig.world.clock.now)
        self.assertEqual(hub.markers, {GH_REPO: rig.world.clock.now()})
        path.write_text("{ not json")
        self.assertEqual(sb.GitHubHub(rig.world.config, rig.world.state, now=rig.world.clock.now).markers, {})

    def test_a_live_webhook_moves_the_marker_unless_a_catch_up_still_has_a_gap_to_close(self):
        rig = self.rig()
        rig.pump(20)
        watch = rig.watch()
        self.assertIsNotNone(rig.gh.forwarder())
        rig.hub.markers[GH_REPO] = rig.world.clock.now() - 900
        watch.catchup_at = None
        rig.world.clock.t += 7
        rig.gh.comment(12, "live", 5003)
        self.assertEqual(rig.hub.markers[GH_REPO], rig.world.clock.now())
        before = rig.hub.markers[GH_REPO]
        watch.catchup_at = rig.world.clock.now() + 5
        rig.world.clock.t += 7
        rig.gh.comment(12, "live again", 5004)
        self.assertEqual(rig.hub.markers[GH_REPO], before, "the gap a restart left is closed by its catch-up first")

    def test_a_list_read_stops_at_its_page_cap_audits_one_truncated_line_and_still_moves_forward(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 3600
        for number in range(300, 300 + 700):
            rig.gh.comment(number, "x", 7000 + number, forward=False)
        rig.hub.catch_up(rig.watch(), now)
        pages = [c for c in self.calls(rig, "issues/comments")]
        self.assertEqual(len(pages), sb.GITHUB_PAGE_CAP)
        [cut] = [e for e in rig.world.audit() if e["event"] == "github.catch_up.truncated"]
        self.assertIn("comments", cut["detail"]["capped_reads"])
        self.assertIs(cut["detail"]["marker_advanced"], True)
        self.assertEqual(rig.hub.markers[GH_REPO], now, "forward only: no chasing the rest")

    def test_pulls_stop_paging_when_a_page_is_older_than_the_window_and_old_ones_cost_no_reads(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        for number in range(1000, 1250):
            rig.gh.pull(number)
            rig.gh.pulls[number]["updated_at"] = sb.iso(now - 7 * 86400 - number)
        rig.gh.pull(2000)
        rig.hub.catch_up(rig.watch(), now)
        self.assertEqual(len(self.calls(rig, "/pulls?")), 1, "the first page already ends before the window")
        reviews = self.calls(rig, "/reviews")
        self.assertEqual([c for c in reviews if "pulls/2000/" not in c], [], "only a PR updated in the window is looked into")

    def test_a_failed_primary_read_keeps_the_marker_and_retries_the_same_bounded_window(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        original = rig.hub.runner
        down = [True]

        def runner(argv, **kwargs):
            if down[0] and "issues/comments" in argv[-1]:
                return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 502")
            return original(argv, **kwargs)
        rig.hub.runner = runner
        rig.hub.catch_up(rig.watch(), now)
        self.assertEqual(rig.hub.markers[GH_REPO], now - 600)
        self.assertEqual(rig.watch().catchup_at, now + sb.GITHUB_LOOKUP_RETRY)
        down[0] = False
        rig.world.clock.t += sb.GITHUB_LOOKUP_RETRY
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        since = [c for c in self.calls(rig, "issues/comments")][-1]
        self.assertIn(sb.iso(now - 600 - sb.GITHUB_MARKER_SLACK), since.replace("%3A", ":"), "the same window, never the full look-back")
        self.assertEqual(rig.hub.markers[GH_REPO], rig.world.clock.now())

    def test_a_failed_secondary_read_is_not_retried_for_the_window_it_missed_and_the_marker_moves(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        original = rig.hub.runner
        rig.hub.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 502") \
            if "actions/runs" in argv[-1] else original(argv, **kw)
        rig.hub.catch_up(rig.watch(), now)
        self.assertIn("workflow runs", rig.watch().catch_up_error)
        self.assertIsNone(rig.watch().catchup_at)
        self.assertEqual(rig.hub.markers[GH_REPO], now)

    def test_a_forwarder_restart_reads_a_short_gap_from_the_in_memory_marker(self):
        rig = self.rig()
        rig.pump(30)
        first = rig.world.clock.now()
        self.assertEqual(rig.hub.markers[GH_REPO] <= first, True)
        rig.gh.kill_forwarder()
        rig.pump(120)
        since = [c for c in self.calls(rig, "issues/comments")][-1].replace("%3A", ":")
        self.assertNotIn(sb.iso(rig.world.clock.now() - 86400)[:13], since, "not a day")
        stamp = since.split("since=")[1].split("&")[0]
        self.assertLess(rig.world.clock.now() - sb.parse_iso(stamp), 400)

    def test_the_fault_a_fresh_start_reads_the_whole_look_back_and_the_control_fails(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "catch-up-on-fresh-start"
        rig = self.rig()
        rig.gh.comment(12, "history", 5001, forward=False)
        rig.hub.catch_up(rig.watch(), rig.world.clock.now())
        self.assertNotEqual(self.calls(rig, "issues/comments"), [], "with the fault history is read")
        self.assertEqual(rig.keys(), ["github.comment:5001"])
        self.assertIn("catch-up-on-fresh-start", sb.KNOWN_FAULTS)

    def test_the_fault_a_forwarder_repo_reads_the_whole_window_every_time_and_the_control_fails(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "full-window-every-time"
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        rig.hub.catch_up(rig.watch(), now)
        [first] = self.calls(rig, "issues/comments")[:1]
        self.assertIn(sb.iso(now - 86400), first.replace("%3A", ":"))
        self.assertIn("full-window-every-time", sb.KNOWN_FAULTS)

    def test_after_the_state_is_deleted_nothing_is_replayed_and_nothing_is_delivered_twice(self):
        rig = self.rig()
        rig.gh.issue(12, parent=1)
        rig.pump(30)
        rig.gh.comment(12, "delivered once", 5001)
        rig.pump(200)
        self.assertEqual(rig.delivered(), ["github.comment:5001"])
        (rig.world.state / sb.READ_MARKER_FILE).unlink()
        fresh = sb.GitHubHub(rig.world.config, rig.world.state, sb.AuditLog(rig.world.audit_path), rig.gh.runner,
                             rig.gh.spawn, rig.world.clock.now)
        fresh.autostart, fresh.port, rig.gh.hub = False, 4711, fresh
        rig.hub.stop()
        rig.world.engine = fresh.attach(rig.world.new_engine())
        rig.hub = fresh
        rig.pump(400)
        self.assertEqual(rig.keys(), [])
        self.assertEqual(rig.delivered(), ["github.comment:5001"])

    def test_status_shows_how_far_each_repo_was_read(self):
        rig = self.rig()
        now = rig.world.clock.now()
        rig.hub.markers[GH_REPO] = now - 600
        rig.hub._write_snapshot(now)
        rig.world.engine.run_pass()
        rig.world.clock.t += 5
        report = sb.github_status(rig.world.config, rig.world.state, {}, rig.world.clock.now())
        text = "\n".join(sb.format_github(report))
        self.assertIn("read to 10m ago", text)

    def test_the_marker_file_is_the_one_new_stored_file_and_the_design_doc_says_so(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("github-read.json", text)
        self.assertIn("forward only", text.lower())


class TestHubTiers(Base):
    """The daemon's read of both labels: two reads per repo, a tier on every record."""

    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self, config=dict(TIER_LABELS))
        self.world, self.gh, self.hub = self.rig.world, self.rig.gh, self.rig.hub

    def derive(self):
        self.world.engine.discover(self.world.clock.now())
        return sb.derive_decisions(self.world.engine, self.world.clock.now())[0]

    def tiers(self):
        return {d["ask"]: d["tier"] for d in self.derive()}

    def test_your_label_is_human_the_chief_of_staffs_is_cos_and_both_is_human(self):
        self.gh.issue(5, labels=["me:awaiting-user"])
        self.gh.issue(6, labels=["me:awaiting-cos"])
        self.gh.issue(7, labels=["me:awaiting-cos", "me:awaiting-user"])
        self.gh.issue(8, labels=["bug"])
        self.rig.pump(15)
        self.assertEqual(self.tiers(), {5: "human", 6: "cos", 7: "human"})

    def test_a_catch_up_makes_two_reads_per_repo_one_per_label_never_one_per_issue(self):
        for number in range(5, 25):
            self.gh.issue(number, labels=["me:awaiting-cos"])
        self.rig.pump(15)
        before = len([c for c in self.gh.api_calls if "labels=" in c])
        self.hub.catch_up(self.rig.watch(), self.world.clock.now())
        reads = [c for c in self.gh.api_calls if "labels=" in c][before:]
        self.assertEqual(len(reads), 2, reads)
        self.assertEqual(sorted(urllib.parse.unquote(re.search(r"labels=([^&]+)", c).group(1)) for c in reads),
                         ["me:awaiting-cos", "me:awaiting-user"])
        self.assertTrue(all("page=1" in c and "--paginate" not in c for c in reads))
        self.assertEqual(len(self.tiers()), 20)

    def test_a_capped_read_says_so_and_does_not_move_the_picture_forward_silently(self):
        for number in range(5, 5 + sb.GITHUB_PAGE_SIZE * sb.GITHUB_PAGE_CAP + 3):
            self.gh.issue(number, labels=["me:awaiting-cos"])
        self.rig.pump(15)
        capped = [e for e in self.world.audit() if e["event"] == "github.catch_up.truncated"]
        self.assertTrue(capped)
        self.assertIn("awaiting-cos issues", capped[-1]["detail"]["capped_reads"])

    def test_a_webhook_moves_an_issue_between_tiers_with_no_gh_call(self):
        issue = self.gh.issue(7, labels=["me:awaiting-cos"], age=0)
        self.rig.pump(15)
        self.assertEqual(self.tiers(), {7: "cos"})
        calls = len(self.gh.api_calls)
        issue["labels"] = [{"name": "me:awaiting-user"}]
        self.gh.send("issues", {"action": "labeled", "issue": dict(issue), "label": {"name": "me:awaiting-user"}})
        self.assertEqual(self.tiers(), {7: "human"})
        issue["labels"] = []
        self.gh.send("issues", {"action": "unlabeled", "issue": dict(issue), "label": {"name": "me:awaiting-user"}})
        self.assertEqual(self.derive(), [])
        self.assertEqual(len(self.gh.api_calls), calls)

    def test_the_time_waited_survives_an_escalation(self):
        issue = self.gh.issue(7, labels=["me:awaiting-cos"], age=0)
        self.rig.pump(15)
        first = self.derive()[0]["since"]
        self.world.tick(120)
        issue["labels"] = [{"name": "me:awaiting-user"}]
        self.hub.read_awaiting(GH_REPO, self.world.clock.now())
        self.assertEqual((self.derive()[0]["tier"], self.derive()[0]["since"]), ("human", first))

    def test_the_tier_flat_fault_shows_everything_to_you(self):
        self.gh.issue(6, labels=["me:awaiting-cos"])
        self.rig.pump(15)
        self.assertEqual(self.tiers(), {6: "cos"})
        os.environ[sb.FAULT_ENV] = "tier-flat"
        self.assertEqual(self.tiers(), {6: "human"})


class TestRetireLostHeadless(StandInPids, Base):
    """#678: `retire` of a headless worker whose session is gone (a record on file, nothing discovered): authority, the record, the remembered boss."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("boss", "other", "cos"))
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.ap = sb.load_agent_presence()
        self.engine = self.world.new_engine("cli")
        self.engine.state_dir = self.world.state
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        for patch in (mock.patch.object(sb.V2Client, "service_identity", lambda client: 100),
                      mock.patch.object(sb, "owner_terminal_problem", lambda *a, **k: None),   # the test shell is the operator's own terminal
                      mock.patch.dict(os.environ, {}, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.record = Path(self.world.state) / "headless" / "lostw.json"

    def lose(self):
        sid = self.world.v2.create_session({"fleet": {"name": "lostw", "role": "coder", "reports_to": "boss", "headless": True}}, directory=str(self.home))
        self.ap.save_record(self.world.state, "lostw", sid, None, now=self.world.clock.now() - 1000)
        self.engine.discover(self.world.clock.now())
        self.engine.collect_facts()                                       # a pass sees it live: the boss is remembered
        del self.world.v2.sessions[sid]
        return sid

    def run_cli(self, *argv):
        with captured() as (out, err):
            code = sb.main(list(argv), runner=self.world.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def bosses(self):
        return json.loads((Path(self.world.state) / sb.HEADLESS_SERVICE_FILE).read_text())["bosses"]

    def test_the_recorded_boss_retires_a_lost_worker_and_the_record_and_remembered_boss_are_gone_after_the_next_pass(self):
        self.lose()
        self.assertEqual(self.bosses(), {"lostw": "boss"})
        os.environ["HERDR_PANE_ID"] = self.world.panes["boss"]
        code, out, err = self.run_cli("retire", "lostw")
        self.assertEqual(code, 0, err)
        self.assertNotIn("not a fleet agent", err)
        self.assertFalse(self.record.exists())
        self.assertIn("lostw", sb.read_retired(self.world.state))
        self.engine.discover(self.world.clock.now())
        lost = [f for f in self.engine.collect_facts()[0] if f.kind == "headless.lost"]
        self.assertEqual((lost, self.bosses()), ([], {}))

    def test_the_chief_of_staff_and_the_operator_may_retire_a_lost_worker(self):
        for who in ("cos", None):
            self.lose()
            if who:
                os.environ["HERDR_PANE_ID"] = self.world.panes[who]
            else:
                os.environ.pop("HERDR_PANE_ID", None)
            self.assertEqual(self.run_cli("retire", "lostw")[0], 0, who)
            self.assertFalse(self.record.exists(), who)
            self.run_cli("retire", "--undo", "lostw")
            self.ap.remove_record(self.world.state, "lostw")

    def test_another_orchestrator_is_refused_a_lost_workers_record_stays_and_an_unknown_name_is_still_unknown(self):
        self.lose()
        os.environ["HERDR_PANE_ID"] = self.world.panes["other"]
        code, out, err = self.run_cli("retire", "lostw")
        self.assertNotEqual(code, 0)
        self.assertIn("refused", err)
        self.assertTrue(self.record.exists())
        self.assertEqual(sb.read_retired(self.world.state), {})
        os.environ.pop("HERDR_PANE_ID", None)
        code, out, err = self.run_cli("retire", "nobody-here")
        self.assertNotEqual(code, 0)
        self.assertIn("not a fleet agent this switchboard has seen", err)

    def test_a_live_headless_workers_remembered_boss_cannot_retire_it_when_its_real_boss_is_another(self):
        sid = self.world.v2.create_session({"fleet": {"name": "lostw", "role": "coder", "reports_to": "other", "headless": True}}, directory=str(self.home))
        self.ap.save_record(self.world.state, "lostw", sid, None, now=self.world.clock.now() - 1000)
        (Path(self.world.state) / sb.HEADLESS_SERVICE_FILE).write_text(json.dumps({"service": {"pid": self.px[100]}, "bosses": {"lostw": "boss"}}))
        self.assertIn("lostw", self.engine.discover(self.world.clock.now()).agents)   # the session lives
        os.environ["HERDR_PANE_ID"] = self.world.panes["boss"]
        code, out, err = self.run_cli("retire", "lostw")
        self.assertNotEqual(code, 0)
        self.assertIn("refused", err)
        self.assertTrue(self.record.exists())
        self.assertEqual(sb.read_retired(self.world.state), {})

    def test_a_lost_record_with_no_remembered_boss_is_retired_by_the_chief_of_staff_but_not_by_an_orchestrator(self):
        sid = self.world.v2.create_session({"fleet": {"name": "lostw", "role": "coder", "reports_to": "boss", "headless": True}}, directory=str(self.home))
        self.ap.save_record(self.world.state, "lostw", sid, None, now=self.world.clock.now() - 1000)
        del self.world.v2.sessions[sid]                                   # gone before any pass saw it
        os.environ["HERDR_PANE_ID"] = self.world.panes["boss"]
        self.assertNotEqual(self.run_cli("retire", "lostw")[0], 0)
        self.assertTrue(self.record.exists())
        os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
        self.assertEqual(self.run_cli("retire", "lostw")[0], 0)
        self.assertFalse(self.record.exists())


class TestJobFacts(StandInPids, unittest.TestCase):
    """A dead job its starter has not looked at is one line to the starter's boss, once."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)
        patcher = mock.patch.dict(os.environ, {sb.JOBS_DIR_ENV: str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = types.SimpleNamespace(name="coder", session_id="ses_example1", conflict=False, problem=None,
                                            fleet={"reports_to": "platform"}, status="idle")
        boss = types.SimpleNamespace(name="platform", session_id="ses_example2", conflict=False, problem=None, fleet={}, status="idle")
        self.discovery = types.SimpleNamespace(agents={"coder": self.worker, "platform": boss})
        self.engine = types.SimpleNamespace(discovery=self.discovery, now=lambda: 2000.0)
        self.source = sb.JobFacts()
        self.source.bind(self.engine)

    def record(self, name, session="ses_example1", pid=None, started=1000.0, starter="coder", **extra):
        pid = pid or self.px[99999999]
        directory = self.root / name
        directory.mkdir()
        (directory / "job.json").write_text(json.dumps(dict({"name": name, "argv": ["x"], "pid": pid, "started": started, "token": None,
                                                              "session": session, "starter": starter}, **extra)))
        return directory

    def test_a_dead_job_of_a_worker_is_one_fact_for_its_boss_with_a_stable_key(self):
        self.record("suite")
        (fact,) = self.source()
        self.assertEqual((fact.key, fact.recipient, fact.kind, fact.batch), ("job.dead:suite:1000", "platform", "job.dead", False))
        self.assertIn("coder's job suite was killed before it finished", fact.summary)
        self.assertEqual([f.key for f in self.source()], [fact.key], "the same key every time: delivery sends it once")

    def test_a_job_started_again_under_the_same_name_has_a_new_key(self):
        self.record("suite", started=1000.0)
        (first,) = self.source()
        shutil.rmtree(str(self.root / "suite"))
        self.record("suite", started=1500.0)
        (second,) = self.source()
        self.assertNotEqual(first.key, second.key)

    def test_a_collected_a_finished_an_alive_and_a_session_less_job_make_no_fact(self):
        self.record("collected").joinpath("collected").write_text("1")
        self.record("finished").joinpath("exit").write_text("0\n")
        self.record("alive", pid=os.getpid())
        self.record("nosession", session=None)
        self.record("stranger", session="ses_unknown")
        self.record("impostor", starter="platform")   # the session is the worker's, the name is not
        self.assertEqual(self.source(), [])

    def test_a_worker_with_no_boss_a_conflicted_worker_and_a_missing_dir_make_no_fact(self):
        self.record("suite")
        self.worker.fleet = {}
        self.assertEqual(self.source(), [])
        self.worker.fleet = {"reports_to": "platform"}
        self.worker.conflict = True
        self.assertEqual(self.source(), [])
        self.worker.conflict = False
        shutil.rmtree(str(self.root))
        self.assertEqual(self.source(), [])

    def test_a_jobs_dir_others_can_write_a_foreign_name_and_a_planted_record_are_not_believed(self):
        self.record("suite")
        os.chmod(str(self.root), 0o777)
        self.assertEqual(self.source(), [], "the dir is not private")
        os.chmod(str(self.root), 0o700)
        (self.root / "Bad Name").mkdir()
        (self.root / "Bad Name" / "job.json").write_text(json.dumps({"name": "Bad Name", "pid": self.px[99999999], "session": "ses_example1", "starter": "coder", "started": 1.0}))
        self.assertEqual([f.key for f in self.source()], ["job.dead:suite:1000"], "a name outside the pattern is skipped")
        with mock.patch.object(sb.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(sb.job_read("suite", self.root), "a record owned by someone else is not believed")

    def test_the_source_is_registered_and_the_nudge_names_the_tool(self):
        self.assertTrue(any(isinstance(x, sb.JobFacts) for x in sb.fact_sources(self.root)))
        self.assertIn("job start <name> -- <command>", sb.NUDGE_TEXT % ("platform", "fleet-switchboard", "fleet-switchboard", "fleet-switchboard"))
        for rel in ("agents/opencode/coder.md", "skills/fleet-coordination/SKILL.md", "docs/switchboard.md"):
            self.assertIn("job status", (REPO / rel).read_text(), rel)


class TestEventFeedRun(V2TransportBase):
    def setUp(self):
        super().setUp()
        self.service.stream = []

    def test_the_feed_marks_a_gap_on_every_opened_connection_and_reconnects_with_growing_waits(self):
        sleeps, feed = [], None

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 4:
                feed.stop()
        feed = sb.EventFeed(self.route(5), None, sleep=sleep)
        self.service.stream = [feed_line("session.step.ended", assistantMessageID="msg_a"), feed_line("session.execution.succeeded", created=7)]
        feed.needs_resync = False
        feed.run()
        self.assertEqual(sleeps, [1, 2, 4, 8], "a stream that ends is a gap: reconnect, waiting longer each time")
        self.assertEqual(feed.status()["connects"], 4)
        self.assertTrue(feed.needs_resync)

    def test_a_connection_that_lived_starts_the_wait_over_and_the_wait_is_capped(self):
        clock, sleeps, feed = [0.0], [], None

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += sb.FEED_STABLE_SECONDS + 1 if len(sleeps) == 3 else 0.1
            if len(sleeps) == 5:
                feed.stop()
        feed = sb.EventFeed(self.route(5), None, now=lambda: clock[0], sleep=sleep)
        self.service.stream = []
        feed.run()
        self.assertEqual(sleeps[:2], [1, 2])
        with mock.patch.object(sb, "FEED_BACKOFF_MAX", 2):
            capped, feed2 = [], None

            def sleep2(seconds):
                capped.append(seconds)
                if len(capped) == 4:
                    feed2.stop()
            feed2 = sb.EventFeed(self.route(5), None, sleep=sleep2)
            feed2.run()
        self.assertEqual(capped, [1, 2, 2, 2])

    def test_a_service_that_stays_down_costs_nothing_no_gap_no_resync_no_read_no_audit_line(self):
        world = World(self, agents=("platform", "coder"))
        engine = world.new_engine()
        self.service.up = False
        waits = []
        engine.feed = feed = sb.EventFeed(self.route(5), engine.audit, world.clock.now, sleep=lambda s: (waits.append(s), len(waits) >= 5 and feed.stop()))
        feed.run()
        engine.discover(world.clock.now())
        before = len(world.v2.calls)
        for _ in range(3):
            sb.feed_tick(engine)
        self.assertEqual((feed.status()["connects"], feed.status()["errors"]), (0, 5))
        self.assertEqual(len(world.v2.calls), before, "no read at all while the service is down")
        self.assertEqual([e for e in world.audit() if e["event"] in ("event.resync", "event.missed")], [])

    def test_the_password_is_in_no_feed_state_no_audit_line_and_no_status_file(self):
        world = World(self, agents=("platform", "coder"))
        engine = world.new_engine()
        engine.feed = feed = sb.EventFeed(self.route(5), engine.audit, world.clock.now, sleep=lambda s: feed.stop())
        feed.need("test")
        self.service.password = "wrong"
        feed.run()
        self.service.password = "pw-secret-1"
        self.service.write_service_file(self.service_file)
        feed.stop_event.clear()
        feed.sleep = lambda s: feed.stop()
        self.service.stream = [feed_line("session.step.ended", assistantMessageID="msg_a")]
        feed.run()
        engine.discover(world.clock.now())
        feed.connected = True
        sb.feed_tick(engine)
        sb.record_v2_calls(engine)
        everything = json.dumps(feed.status()) + (world.state / "audit.jsonl").read_text() + (world.state / sb.V2_CALLS_FILE).read_text()
        self.assertIn("event.resync", everything)
        self.assertNotIn(self.service.password, everything)
        self.assertNotIn("Basic ", everything)
        self.assertNotIn("wrong", everything)


class TestCatchingUpStatus(StatusCase):
    """#467 slice 3: the daemon records which budgeted readers are still pending (a small file, only when it changes) and `status` says so in one line."""

    def setUp(self):
        super().setUp()
        self.engine = types.SimpleNamespace(state_dir=self.world.state, now=self.world.clock.now)
        self.path = self.world.state / sb.CATCHING_UP_FILE

    def pending(self, load=(), repos=(), orders=()):
        sb.set_source_state(self.engine, "load_alert", list(load))
        sb.set_source_state(self.engine, "catch_up", list(repos))
        sb.set_source_state(self.engine, "charters", list(orders))
        return sb.write_catching_up(self.engine)

    def lines(self):
        return [l for l in self.status()[1].splitlines() if "catching up" in l]

    def test_s1_one_line_names_the_sources_still_pending_and_nothing_when_they_are_done(self):
        self.pending(load=["asks of o1", "asks of o2"], repos=["acme/api (3 passes)"])
        (line,) = self.lines()
        self.assertIn("load alert (2: asks of o1, asks of o2)", line)
        self.assertIn("catch-up (1: acme/api (3 passes))", line)
        self.assertNotIn("charter orders", line)
        self.pending()
        self.assertEqual(self.lines(), [])

    def test_s2_the_file_is_rewritten_only_when_it_changes_or_is_old(self):
        written = [self.pending(load=["asks of o1"])]
        for _ in range(4):
            self.world.tick(10)
            written.append(self.pending(load=["asks of o1"]))
        self.world.tick(10)
        written.append(self.pending(load=["asks of o1", "asks of o2"]))
        self.assertEqual(written, [True, False, False, False, False, True], "no heartbeat inside two minutes: 2 writes for 6 passes")
        self.world.tick(sb.CATCHING_UP_REWRITE_SECONDS + 1)
        self.assertTrue(self.pending(load=["asks of o1", "asks of o2"]), "an unchanged pending state is rewritten after two minutes: its age says the daemon is alive")
        self.assertTrue(self.pending(), "the first empty state after a pending one is written (status must not keep showing the old record)")
        self.assertFalse(self.pending(), "nothing pending and already empty: nothing is written")

    def test_s3_a_record_the_daemon_has_stopped_updating_is_not_shown(self):
        self.pending(load=["asks of o1"])
        now = self.world.clock.now()
        self.assertEqual(len(sb.catching_up_lines(self.world.state, now + 60)), 1)
        self.assertEqual(sb.catching_up_lines(self.world.state, now + 11 * 60), [])

    def test_a_stale_non_empty_file_is_emptied_by_the_first_pass_that_finds_nothing_pending(self):
        self.path.write_text(json.dumps({"at": sb.iso(self.world.clock.now()), "sources": {"load_alert": ["x"]}}))
        self.assertTrue(self.pending(), "a record from a daemon that died: the first pass with nothing pending replaces it")
        self.assertEqual(json.loads(self.path.read_text())["sources"], {})
        self.assertEqual(self.lines(), [])

    def test_more_than_three_waiting_are_cut_and_a_hostile_source_name_is_a_plain_short_line(self):
        self.pending(load=["a1", "a2", "a3", "a4", "a5"])
        (line,) = self.lines()
        self.assertIn("load alert (5: a1, a2, a3, ...)", line)
        self.path.write_text(json.dumps({"at": sb.iso(self.world.clock.now()), "sources": {"\x1b[31mevil\x1b]0;x\x07" + "n" * 80: ["w"]}}))
        (line,) = self.lines()
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)
        self.assertLess(len(line), 90)

    def test_a_future_record_a_huge_file_and_deep_nesting_say_nothing(self):
        now = self.world.clock.now()
        self.path.write_text(json.dumps({"at": sb.iso(now + 3600), "sources": {"load_alert": ["x"]}}))
        self.assertEqual(self.lines(), [], "a record dated in the future is not shown for ever")
        self.path.write_text(json.dumps({"at": sb.iso(now), "sources": {"load_alert": ["x" * 100] * 2000}}))
        self.assertEqual(self.lines(), [])
        self.path.write_text("[" * 100000)
        self.assertEqual((self.status()[0], self.lines()), (0, []))

    def test_s5_the_file_is_private_and_names_only_sources_and_counts(self):
        self.pending(load=["asks of o1"], repos=["acme/api (2 passes)"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        body = json.loads(self.path.read_text())
        self.assertEqual(sorted(body), ["at", "sources"])
        self.assertEqual(sorted(body["sources"]), ["catch_up", "load_alert"])
        self.assertNotIn(str(self.world.state), self.path.read_text())

    def test_s6_a_missing_or_unreadable_file_says_nothing_and_status_does_not_fail(self):
        self.assertEqual((self.status()[0], self.lines()), (0, []))
        for text in ("{not json", "[]", '{"at": "x", "sources": {}}', '{"sources": "no"}', json.dumps({"at": sb.iso(self.world.clock.now()), "sources": {"load_alert": "no"}})):
            self.path.write_text(text)
            self.assertEqual((self.status()[0], self.lines()), (0, []), text)

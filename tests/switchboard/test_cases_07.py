"""Test classes moved out of bin/test-switchboard (text unchanged): cases_07."""
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
from support.installs_and_keys import GOOD_DONE_WHEN, GOOD_PLAN
from support.loader import SWITCHBOARD, _load, captured, copy_entry, sb, switchboard_source
from support.paths import BIN, REPO
from support.rigs import build_carry_repo
from support.scenario_roles import FAKE_GH
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE




class TestChiefOfStaffNativePermissions(unittest.TestCase):
    """The Chief of Staff's (and its subagent's) v2 `permissions:` list is SHORT, hand-written and deny-by-default (#191): a focus, not a fence.
    Check its shape, that it stays short, and evaluate it with OpenCode's documented semantics (whole-value glob, any matching deny denies,
    else an allow, else the base deny)."""
    PATHS = [REPO / "agents" / "opencode" / "chief-of-staff.md", REPO / "agents" / "opencode" / "cos-subagent.md"]
    PATH = PATHS[0]
    TEMP = "/private/var/folders/ab/T/opencode"
    MAX_BLOCK_LINES = 210

    @staticmethod
    def block(path):
        """(front matter text, the lines of the `permissions:` block, its rules as dicts). One rule per line: `- {action: shell, resource: "x", effect: allow}`."""
        front = path.read_text().split("\n---\n", 1)[0]
        lines = front.splitlines()
        start = lines.index("permissions:")
        rules = []
        for line in lines[start + 1:]:
            if line.lstrip().startswith("#"):
                continue
            m = re.fullmatch(r"  - \{action: (.+?), resource: (\".*\"), effect: (allow|deny)\}", line)
            assert m, "not a one-line rule: %r" % line
            rules.append({"action": json.loads(m.group(1)) if m.group(1).startswith('"') else m.group(1), "resource": json.loads(m.group(2)), "effect": m.group(3)})
        return front, lines[start:], rules

    @classmethod
    def parse(cls, path):
        front, _, rules = cls.block(path)
        return front, rules

    @classmethod
    def rules(cls):
        return cls.parse(cls.PATH)

    @staticmethod
    def evaluate(rules, action, resource):
        def match(pattern):
            rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
            if pattern.endswith(" *"):  # OpenCode: a trailing ` *` also matches the bare command
                rx = rx[:-len(re.escape(" ") + ".*")] + "(?: .*)?"
            return re.fullmatch(rx, resource, re.S) is not None
        verdict = "deny"
        for r in rules:   # v2: the LAST matching rule decides (proved on the real harness); the list keeps every deny after every allow
            if r["action"] in ("*", action) and match(r["resource"]):
                verdict = r["effect"]
        return verdict

    def test_the_block_is_short_enough_for_a_person_to_read_and_cannot_regrow(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, lines, _ = self.block(path)
                self.assertLess(len(lines), self.MAX_BLOCK_LINES, "the permission block of %s is %d lines: keep it a short list, no enumerated variants" % (path.name, len(lines)))
                self.assertLess(len(path.read_text().split("\n---\n", 1)[0].splitlines()), self.MAX_BLOCK_LINES + 20)
        # the generator that once enumerated every variant (30,000 lines) is gone and must not come back
        self.assertFalse((REPO / "tools" / "cos-permissions-proof" / "gen_permissions.py").exists())

    def test_every_deny_comes_after_every_allow_because_the_last_match_wins(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                effects = [r["effect"] for r in rules[1:]]
                last_allow = max(i for i, e in enumerate(effects) if e == "allow")
                self.assertNotIn("allow", effects[effects.index("deny"):] if "deny" in effects else [])
                self.assertEqual(effects[last_allow + 1:], ["deny"] * (len(effects) - last_allow - 1))
                resources = [r["resource"] for r in rules]
                for guard in ("fleet-switchboard gates *enforce*", "fleet-switchboard gates on*", "fleet-switchboard waive *G6*"):
                    # the timeout form is its own line, or one `*`-led line covers the bare and the timeout forms (the Chief of Staff's)
                    self.assertTrue("timeout * " + guard in resources or "*" + guard in resources, guard)

    def test_the_first_rule_denies_everything_and_nothing_ever_asks(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
                self.assertEqual({r["effect"] for r in rules}, {"allow", "deny"})

    def test_v1_keys_that_would_void_or_widen_the_rules_are_absent(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                front, _ = self.parse(path)
                self.assertNotRegex(front, r"(?m)^name:")  # a named agent makes v2 ignore `permissions:` entirely
                self.assertNotRegex(front, r"(?m)^permission:")
                self.assertNotRegex(front, r"bash:")

    def test_no_shell_allow_is_a_bare_wildcard(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                for r in rules:
                    if r["action"] == "shell" and r["effect"] == "allow":
                        self.assertNotIn(r["resource"], ("*", "* *"))

    def test_edit_is_only_the_temp_dir_and_subagents_are_denied(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                self.assertEqual([(r["resource"], r["effect"]) for r in rules if r["action"] == "edit"], [("/private/var/folders/*/T/opencode/*", "allow")])
                for action in ("subagent", "task"):
                    self.assertEqual([r["effect"] for r in rules if r["action"] == action], ["deny"])
                self.assertEqual(self.evaluate(rules, "edit", "/home/a/Code/x/f.py"), "deny")
                self.assertEqual(self.evaluate(rules, "edit", self.TEMP + "/note.md"), "allow")

    def test_the_job_runs_and_what_is_outside_it_is_denied(self):
        _, rules = self.rules()
        for command in ("fleet-switchboard decisions list --tier all", "sleep 20",
                        "gh issue view 5 --json body -q .body", "gh issue create --title x --body y", "fleet-switchboard disk", "fleet-switchboard disk janitor --apply", "fleet-switchboard disk reap-docker --apply", "fleet-switchboard disk prune", "gh api repos/o/r/issues/comments/1 --jq .body", "grep foo", "cut -c1-100", "jq length",
                        "gh pr merge 5 --squash", "gh issue close 5", "gh pr comment 5 --body ok", "az webapp show -n a -g g", "az group list",
                        "fleet-switchboard handoff --issue 5 --brief-file -", "fleet-switchboard launch v --agent orchestrator",
                        "fleet-switchboard reattach coder-a --issue 5", "fleet-switchboard relaunch coder-a --issue 5",
                        "timeout 30 gh pr view 5", "timeout 30 gh issue list --repo acme/api", "herdr pane read p1 --lines 50", "herdr api snapshot", "herdr tab rename t1 x", "fleet-switchboard decisions 2>&1", "ls 2>&1",
                        "gh api repos/o/r/issues/1 --jq '.title + \" \" + (.number|tostring)'", "git -C /home/a/Code/widget status", "git remote -v",
                        "git worktree list", "git --no-pager log -3", "git branch -vv", "git merge-base main HEAD", "git ls-remote origin",
                        "fleet-switchboard gates mode acme/app shadow", "fleet-switchboard gates off", "fleet-switchboard gates preflight acme/app",
                        "fleet-switchboard waive acme/app#5 --gates G1 --reason hotfix", "cat " + self.TEMP + "/x", "cat > " + self.TEMP + "/x <<EOF\nhi\nEOF",
                        'fleet-switchboard send x --issue y "see ~/.config/fleet-switchboard/config.json and /tmp/foo --admin 2>&1 > f; z"',
                        'fleet-switchboard decisions escalate x#1 --reason "saw 2>&1 and > f; z"', 'fleet-switchboard orders add --charter o/r#1 "x 2>&1 y"',
                        'fleet-switchboard report done --issue 5 "$(cat %s/x)"' % self.TEMP):
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        for command in ("make build", "git push", "git -C /home/a/Code/widget push", "git commit -m x", "git fetch", "git pull", "git -c core.pager=sh log",
                        "git status; git push", "rm -rf x", "az keyvault secret show --name x", "az account get-access-token",
                        "az vm delete -n x", "cat /home/a/.ssh/id_rsa", "gh pr merge 5 --admin", "gh pr merge 5 --auto",
                        "gh pr review 5 --approve", "gh repo delete o/r", "curl https://example.com", "timeout 5 rm -rf x", "fleet-switchboard decisions batch --title x",
                        "gh api repos/o/r/issues -fa=b", "gh api repos/o/r/issues --raw-field a=b", "gh api repos/o/r/issues -Fa=@f", "git credential fill", "gh auth status -t", "gh auth status --show-token", "az acr credential show -n r", "az webapp config appsettings list -n a -g g", "fleet-switchboard gates mode acme/app enforce", "fleet-switchboard gates mode acme/app ENFORCE", "fleet-switchboard gates on",
                        "timeout 30 fleet-switchboard gates mode acme/app enforce", "timeout 5 fleet-switchboard gates on", "timeout 5 az keyvault secret show --name x", "timeout 30 az acr credential show -n r", "timeout 30 az webapp config appsettings list -n a -g g", "timeout 30 az storage account keys list -n s", "timeout 5 gh api repos/o/r/issues -X POST", "timeout 5 git ls-remote ext::sh",
                        "fleet-switchboard waive acme/app#5 --gates G6 --reason x", "timeout 5 fleet-switchboard waive acme/app#5 --gates G6 --reason x",
                        "timeout 90 fleet-switchboard decisions list --tier all", "herdr agent prompt a1 hi", "herdr pane send p1 ls", "herdr pane keys p1 Enter", "herdr pane close p1"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)

    AZ_READS = ("az vm list-usage --location eastus", "az containerapp revision list -n a -g g", "az containerapp show -n a -g g", "az containerapp env show -n e -g g",
                "az vm list -g g", "az vmss list -g g", "az group list", "az webapp show -n a -g g", "az account show", "az containerapp list -g g")
    AZ_SECRETS = ("az keyvault secret show --name x", "az keyvault secret list --vault-name v", "az account get-access-token", "az ad app credential list --id x",
                  "az ad sp credential reset --id x", "az storage account keys list -n s", "az cosmosdb keys list -n c -g g", "az cosmosdb list-keys -n c -g g",
                  "az webapp config connection-string list -n a -g g", "az webapp config appsettings list -n a -g g", "az containerapp secret list -n a -g g",
                  "az containerapp secret show -n a -g g --secret-name s", "az functionapp keys list -n f -g g", "az sql db show-connection-string -n d",
                  "az containerapp exec -n a -g g", "az containerapp up -n a", "az containerapp delete -n a -g g", "az containerapp update -n a -g g", "az vm start -n v -g g",
                  "az rest --method get --url https://x", "az acr credential show -n r")

    AZ_MORE_SECRETS = ("az signalr key list -n s -g g", "az eventgrid topic key list -n t -g g", "az search admin-key show --service-name s -g g",
                       "az search query-key list --service-name s -g g", "az webapp deployment list-publishing-profiles -n a -g g",
                       "az functionapp deployment list-publishing-credentials -n f -g g", "az keyvault list", "az vm delete -n list -g g")
    TIMEOUT_WRITES = ("timeout 5 gh secret set X --body view", "timeout 5 gh repo delete o/r view", "timeout 5 gh api repos/o/r/issues -X POST -f t=view",
                      "timeout 5 gh workflow run ci.yml --ref view", "timeout 5 gh pr merge 5 --body view")

    def test_az_reads_are_allowed_generally_and_every_secret_reading_or_writing_verb_stays_denied(self):
        for path in self.PATHS:
            with self.subTest(file=path.name):
                _, rules = self.parse(path)
                for command in self.AZ_READS + ("gh issue create --title x --body y", "fleet-switchboard disk", "fleet-switchboard disk janitor --apply",
                                                "timeout 30 gh pr view 5", "timeout 30 gh issue list --repo acme/api"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
                for command in self.TIMEOUT_WRITES + ("fleet-switchboard disk prune --docker /bin/sh", "fleet-switchboard disk prune --docker=/bin/sh", "fleet-switchboard disk --docker /tmp/x report",
                                                      "fleet-switchboard disk reap-docker --apply --docker /tmp/x/docker", "fleet-switchboard disk report --docker=/bin/sh"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
                for command in ("fleet-switchboard disk prune", "fleet-switchboard disk prune --dry-run", "fleet-switchboard disk report"):
                    self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
                for command in self.AZ_SECRETS + self.AZ_MORE_SECRETS:
                    self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
                    self.assertEqual(self.evaluate(rules, "shell", "timeout 30 " + command), "deny", "timeout 30 " + command)

    READ_ONLY_ADDED = ("fleet-switchboard review measure --since 7", "fleet-switchboard review laws acme/api#5", "fleet-switchboard impact floor acme/api#5 --json",
                       "fleet-switchboard impact hold --dry-run", "fleet-switchboard pr-explain acme/api#5 --check", "fleet-switchboard agents verify")

    def test_the_review_and_impact_reads_are_allowed_and_each_is_read_only_and_the_writes_next_to_them_are_not(self):
        """The cos list gains six read-only lines: review measure, review laws, impact floor, impact hold --dry-run, pr-explain and agents verify
        (`gates *`, `plan *` and `status *` were already there). Each prints and writes nothing. The write subcommands of the same words stay denied."""
        _, rules = self.rules()
        for command in self.READ_ONLY_ADDED:
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        # no blanket `timeout ? fleet-switchboard *` any more: a write behind a timeout prefix is denied, like the bare one (the Chief of Staff needs no prefix)
        for command in ("timeout 30 fleet-switchboard review record acme/api#5 --reviewer code --block f", "timeout 30 fleet-switchboard impact clear acme/api#5 --reason x",
                        "timeout 30 fleet-switchboard verdict --all --pr acme/api#5", "timeout 5 fleet-switchboard send x y", "timeout 30 fleet-switchboard review laws acme/api#5"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        for command in ("fleet-switchboard verdict --all --pr acme/api#5", "fleet-switchboard review record acme/api#5 --reviewer code --block f"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard review record acme/api#5 --reviewer code --block f"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard impact clear acme/api#5 --reason x"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard impact hold"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard agents install"), "deny")
        for gate in ("fleet-switchboard gates mode acme/app enforce", "timeout 30 fleet-switchboard gates mode acme/app ENFORCE", "fleet-switchboard gates on",
                     "timeout 5 fleet-switchboard waive acme/app#5 --gates G8 --reason x", "timeout 5 fleet-switchboard decisions batch --title x"):
            self.assertEqual(self.evaluate(rules, "shell", gate), "deny", gate)   # the collapsed denies still catch both the bare and the timeout forms

    def test_retire_is_allowed_for_the_chief_of_staff_and_every_write_next_to_it_stays_denied(self):
        _, rules = self.rules()
        for command in ("fleet-switchboard retire worker --reason done", "fleet-switchboard retire --undo worker", "fleet-switchboard retire worker"):
            self.assertEqual(self.evaluate(rules, "shell", command), "allow", command)
        for command in ("timeout 30 fleet-switchboard retire worker", "fleet-switchboard review record acme/api#5 --reviewer code --block f",
                        "fleet-switchboard impact clear acme/api#5 --reason x", "fleet-switchboard verdict --all --pr acme/api#5",
                        "herdr pane close w1:p1", "herdr tab close w1:t1", "fleet-switchboard gates mode acme/app enforce"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)        # no raw herdr, and the other writes are as before
        for path in self.PATHS:
            _, lines, _ = self.block(path)
            self.assertLess(len(lines), self.MAX_BLOCK_LINES)

    def test_the_subagent_has_the_same_list_without_handoff_or_launch(self):
        _, rules = self.parse(self.PATHS[1])
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard send x y"), "allow")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard handoff --issue 5"), "deny")
        self.assertEqual(self.evaluate(rules, "shell", "fleet-switchboard launch v --agent orchestrator"), "deny")
        for command in ("fleet-switchboard reattach x", "fleet-switchboard relaunch x", "fleet-switchboard retire x"):
            self.assertEqual(self.evaluate(rules, "shell", command), "deny", command)
        self.assertEqual(self.evaluate(rules, "shell", "make build"), "deny")

    def test_the_prose_matches_the_rules(self):
        text = self.PATH.read_text()
        self.assertIn("I don't have a problem with you using gh to write comments or close/merge PRs", text)
        self.assertIn("route the work to the owning orchestrator", text)
        self.assertIn("keeps you **focused on your job**", text)
        self.assertNotIn("never merge,", text)
        self.assertNotIn("ASKS your principal", text)  # one story: a denial is hard and nothing asks


class TestCosTriageAllowRule(unittest.TestCase):
    """The Chief of Staff definitions allow `fleet-switchboard triage *` with one rule each, and no other switchboard allow rule is added."""
    FILES = ("agents/opencode/chief-of-staff.md", "agents/opencode/cos-subagent.md")
    RULE = '  - {action: shell, resource: "fleet-switchboard triage *", effect: allow}'

    def _lines(self, rel):
        return (REPO / rel).read_text().splitlines()

    def test_each_file_carries_exactly_one_triage_allow_rule_and_no_deny_or_wider_rule(self):
        for rel in self.FILES:
            with self.subTest(file=rel):
                lines = self._lines(rel)
                self.assertEqual(lines.count(self.RULE), 1)
                triage = [l for l in lines if "fleet-switchboard triage" in l and l.lstrip().startswith("- {action:")]
                self.assertEqual(triage, [self.RULE])  # no second triage rule, no deny, no variant
                for l in lines:  # fail closed: nothing allows the whole command or a wildcard prefix
                    if l.lstrip().startswith("- {action: shell") and "effect: allow" in l:
                        self.assertNotRegex(l, r'resource: "fleet-switchboard \*"')
                        self.assertNotRegex(l, r'resource: "\*+fleet-switchboard')

    def test_the_switchboard_allow_rules_are_the_fixed_old_list_plus_triage_move_reassign(self):
        old = {
            "chief-of-staff.md": "send report remind intent intents status pending whoami notice decisions orders gates waive plan labels handoff launch reattach relaunch retire disk".split(),
            "cos-subagent.md": "send report remind intent intents status disk pending whoami version notice decisions orders gates waive plan labels".split(),
        }
        for rel in self.FILES:
            with self.subTest(file=rel):
                got = [m.group(1) for l in self._lines(rel) for m in [re.match(r'  - \{action: shell, resource: "fleet-switchboard ([a-z]+) \*", effect: allow\}$', l)] if m]
                self.assertEqual(sorted(got), sorted(old[rel.split("/")[-1]] + ["triage", "move", "reassign", "resume"]))


class TestCosDelegatedClearPermission(unittest.TestCase):
    """Ask #720: the Chief of Staff definition allows the delegated clear (`impact clear * --under-order *`) and no other impact command beyond the
    two read-only ones. The copilot definition has no per-command list (a `tools:` allow-list and prose), so the line is only in the opencode one."""
    RULE = '  - {action: shell, resource: "fleet-switchboard impact clear * --under-order *", effect: allow}'
    PATH = REPO / "agents" / "opencode" / "chief-of-staff.md"

    def test_S1_the_line_once_and_the_other_impact_permissions_are_the_two_read_only_ones(self):
        lines = self.PATH.read_text().splitlines()
        self.assertEqual(lines.count(self.RULE), 1)
        others = [l for l in lines if l.lstrip().startswith("- {action: shell") and "fleet-switchboard impact " in l and l != self.RULE]
        self.assertEqual(others, ['  - {action: shell, resource: "fleet-switchboard impact floor *", effect: allow}',
                                  '  - {action: shell, resource: "fleet-switchboard impact hold --dry-run*", effect: allow}'])

    def test_S1_the_copilot_definition_names_no_impact_command(self):
        text = (REPO / "agents" / "copilot" / "chief-of-staff.agent.md").read_text()
        self.assertNotIn("fleet-switchboard impact", text)
        self.assertNotIn("--under-order", text)

    def test_S2_last_match_wins_allows_only_the_delegated_clear(self):
        _, rules = TestChiefOfStaffNativePermissions.parse(self.PATH)
        verdicts = [TestChiefOfStaffNativePermissions.evaluate(rules, "shell", c) for c in (
            "fleet-switchboard impact clear 5 --reason why",
            "fleet-switchboard impact clear 5 --reason why --under-order 1",
            "fleet-switchboard impact waive 5")]
        self.assertEqual(verdicts, ["deny", "allow", "deny"])


class TestChatClearOffers(unittest.TestCase):
    """#457 slice 1: the offers, the person's messages and the checks no model can waive. Fixtures only: nothing here touches a real PR."""

    NAMES = {"merge-captain": "u:merge-captain", "hold-impact": "u:hold-impact", "hold-credential": "u:hold-credential",
             "hold-policy": "u:hold-policy", "hold-security": "u:hold-security", "hold-contract": "u:hold-contract",
             "hold-prod-config": "u:hold-prod-config", "hold-other-area": "u:hold-other-area", "hold-owner": "u:hold-owner", "parked": "u:parked"}

    def setUp(self):
        self.cc = sb.load_module("chat_clear")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name

    def hold(self, number, labels, head="a" * 40, repo="acme/widget", summary="touches the gate"):
        return {"pr": "%s#%d" % (repo, number), "head": head, "labels": labels, "summary": summary}

    def offers(self, *numbers, head="a" * 40):
        holds = [self.hold(n, ["u:hold-impact"], head=head) for n in numbers]
        return self.cc.refresh_offers(self.state, holds, self.NAMES, 1000)["offers"]

    def test_the_owner_tier_labels_are_offered_however_they_were_put_on_and_other_holds_and_parked_are_not(self):
        for labels in (["u:hold-impact"], ["u:hold-credential"], ["u:hold-policy"], ["u:merge-captain"],
                       [{"name": "u:hold-credential"}, {"name": "u:parked"}]):
            self.assertTrue(self.cc.is_offerable(self.NAMES, labels), labels)
        for labels in (["u:parked"], [], ["u:hold-impact", "u:hold-security"], ["u:merge-captain", "u:hold-prod-config"], ["bug"]):
            self.assertFalse(self.cc.is_offerable(self.NAMES, labels), labels)
        self.assertEqual(self.cc.owner_tier_labels(self.NAMES, ["u:parked", "u:hold-credential", "u:merge-captain"]),
                         ["u:hold-credential", "u:merge-captain"], "only owner-tier labels, never parked")

    def test_an_offer_keeps_the_head_he_was_shown_until_a_question_shows_him_the_new_one(self):
        self.offers(366)
        refreshed = self.cc.refresh_offers(self.state, [self.hold(366, ["u:hold-impact"], head="b" * 40)], self.NAMES, 2000)
        self.assertEqual(refreshed["offers"]["acme/widget#366"]["head"], "a" * 40, "a push does not change what he was shown")
        self.assertEqual(refreshed["added"], [])
        offer = self.cc.read_offers(self.state)["acme/widget#366"]
        self.assertFalse(self.cc.head_matches(offer, "b" * 40), "a push after the offer clears nothing")
        self.assertTrue(self.cc.head_matches(offer, "a" * 40))
        self.assertTrue(self.cc.show(self.state, "acme/widget#366", "b" * 40, 3000))
        self.assertTrue(self.cc.head_matches(self.cc.read_offers(self.state)["acme/widget#366"], "b" * 40))
        self.assertFalse(self.cc.show(self.state, "acme/widget#999", "b" * 40, 3000), "no offer, nothing moved")

    def test_a_pr_that_is_no_longer_held_loses_its_offer_and_an_unchanged_set_writes_nothing(self):
        self.offers(366, 389)
        path = Path(self.state) / "chat-clear-offers.json"
        writes = []
        real = self.cc.write_offers
        self.cc.write_offers = lambda *a, **k: writes.append(a) or real(*a, **k)
        self.addCleanup(setattr, self.cc, "write_offers", real)
        same = self.cc.refresh_offers(self.state, [self.hold(366, ["u:hold-impact"]), self.hold(389, ["u:hold-impact"])], self.NAMES, 1001)
        self.assertEqual((same["added"], same["dropped"]), ([], []))
        self.assertEqual(writes, [], "nothing changed: write_offers was not called")
        gone = self.cc.refresh_offers(self.state, [self.hold(389, ["u:hold-impact"]), self.hold(400, ["u:parked"])], self.NAMES, 1002)
        self.assertEqual(sorted(gone["offers"]), ["acme/widget#389"])
        self.assertEqual(gone["dropped"], ["acme/widget#366"])
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def entry(self, **kw):
        return dict({"head": "a" * 40, "shown": True, "shown_at": 1000, "labels": ["u:hold-impact"], "summary": "s"}, **kw)

    def test_an_unreadable_or_oversized_offers_file_offers_nothing_and_a_bad_entry_is_dropped_alone(self):
        path = Path(self.state) / "chat-clear-offers.json"
        path.write_text("{not json")
        self.assertEqual(self.cc.read_offers(self.state), {})
        path.write_text(json.dumps({"offers": {"bad key": self.entry(), "acme/widget#1": self.entry(head=""), "acme/widget#2": self.entry()}}))
        self.assertEqual(sorted(self.cc.read_offers(self.state)), ["acme/widget#2"])
        path.write_text(" " * (self.cc.OFFERS_MAX_BYTES + 1))
        self.assertEqual(self.cc.read_offers(self.state), {})
        self.assertEqual(self.cc.read_offers(Path(self.state) / "nowhere"), {})

    def test_a_damaged_entry_is_dropped_never_defaulted_to_shown_and_never_touches_the_others(self):
        path = Path(self.state) / "chat-clear-offers.json"
        bad = {"no_shown": {k: v for k, v in self.entry().items() if k != "shown"}, "shown_str": self.entry(shown="true"), "shown_one": self.entry(shown=1),
               "shown_null": self.entry(shown=None), "no_at": {k: v for k, v in self.entry().items() if k != "shown_at"}, "at_str": self.entry(shown_at="1000"),
               "at_neg": self.entry(shown_at=-1), "at_bool": self.entry(shown_at=True), "labels_str": self.entry(labels="u:hold-impact"),
               "labels_mixed": self.entry(labels=["u:hold-impact", 5]), "no_summary": {k: v for k, v in self.entry().items() if k != "summary"},
               "summary_int": self.entry(summary=5), "head_int": self.entry(head=5), "not_dict": "x"}
        entries = {"acme/widget#%d" % (100 + n): v for n, v in enumerate(bad.values())}
        entries["acme/widget#1"] = self.entry()
        entries["acme/widget#2"] = self.entry(shown=False)
        path.write_text(json.dumps({"offers": entries}))
        loaded = self.cc.read_offers(self.state)
        self.assertEqual(sorted(loaded), ["acme/widget#1", "acme/widget#2"], "every damaged entry is dropped, the 2 whole ones are kept as they were")
        self.assertIs(loaded["acme/widget#2"]["shown"], False)
        refreshed = self.cc.refresh_offers(self.state, [self.hold(100, ["u:hold-impact"], head="c" * 40), self.hold(1, ["u:hold-impact"])], self.NAMES, 2000)
        self.assertIs(refreshed["offers"]["acme/widget#100"]["shown"], False, "a PR whose entry was damaged is recorded again as NOT shown")
        self.assertIs(refreshed["offers"]["acme/widget#1"]["shown"], True, "a whole entry beside the damaged ones keeps what it was")
        self.assertEqual(len(refreshed["damaged"]), len(bad))
        for n, shape in enumerate(bad):   # every damaged shape, held again, comes back as NOT shown
            path.write_text(json.dumps({"offers": {"acme/widget#%d" % (300 + n): bad[shape]}}))
            again = self.cc.refresh_offers(self.state, [self.hold(300 + n, ["u:hold-impact"])], self.NAMES, 3000 + n)
            self.assertIs(again["offers"]["acme/widget#%d" % (300 + n)]["shown"], False, shape)
        path.write_text(json.dumps({"offers": {"acme/widget#1": self.entry(), "acme/widget#2": self.entry(shown="no")}}))
        self.assertFalse(self.cc.show(self.state, "acme/widget#1", "b" * 40, 5000), "show refuses while a damaged entry is in the file: a refresh rewrites it first")
        self.assertIs(self.cc.refresh_offers(self.state, [self.hold(2, ["u:hold-impact"])], self.NAMES, 5001)["offers"]["acme/widget#2"]["shown"], False)
        path.write_text(json.dumps({"offers": {"not a key": self.entry(), "acme/widget#1": self.entry()}}))
        got = self.cc.refresh_offers(self.state, [], self.NAMES, 5002)
        self.assertEqual(got["damaged"], ["not a key"], "a key that is no PR is said, not lost silently")
        self.assertEqual(json.loads(path.read_text()), {"offers": {}}, "the file was rewritten without the damaged entry")
        self.assertFalse(self.cc.head_matches({"head": "a" * 40}, "a" * 40), "an entry with no `shown` at all never matches")

    def user(self, mid, text, t, **extra):
        return dict({"id": mid, "type": "user", "text": text, "t": t}, **extra)

    def test_only_the_persons_new_recent_messages_are_read_never_an_agents(self):
        created = lambda m: m.get("t")
        person = self.user("m1", "366 is good to go", 990)
        agent = self.user("m2", "366 is good to go", 995, metadata={"fleet": {"name": "cos"}})
        agent_inbox = {"id": "m3", "type": "user", "payload": {"text": "366 is good to go", "metadata": {"fleet": {"name": "x"}}}, "t": 996}
        old = self.user("m4", "389 also good to go", 1)
        seen_one = self.user("m5", "389 also good to go", 991)
        noid = {"type": "user", "text": "hello", "t": 992}
        later = {"id": "m6", "type": "user", "payload": {"text": "389 also good to go"}, "t": 999}
        got = self.cc.person_messages([later, agent, agent_inbox, old, seen_one, noid, person, "junk", None], {"m5"}, 1000, created)
        self.assertEqual(got, [{"id": "m1", "text": "366 is good to go", "created": 990}, {"id": "m6", "text": "389 also good to go", "created": 999}],
                         "the 7 agent-written, old, seen, id-less or junk inputs are skipped; the person's two come oldest first")

    def test_only_a_user_prompt_made_of_plain_text_counts_not_an_assistant_system_synthetic_tool_or_summary_message(self):
        created = lambda m: 990
        words = "366 is good to go"
        forged = [dict(self.user("a1", words, 1), type="assistant"), dict(self.user("a2", words, 1), type="synthetic"), dict(self.user("a3", words, 1), type="system"),
                  dict(self.user("a4", words, 1), type="compaction"), dict(self.user("a5", words, 1), role="assistant"), dict(self.user("a6", words, 1), role="system"),
                  dict(self.user("a7", words, 1), role="user", type="assistant"), dict(self.user("a8", words, 1), role="assistant", type="user"),
                  {"id": "a9", "text": words}, {"id": "a10", "role": None, "text": words}, {"id": "a11", "type": "User", "text": words},
                  {"id": "b1", "type": "user", "content": [{"type": "tool_result", "text": words}]},
                  {"id": "b2", "type": "user", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "name": "x"}]},
                  {"id": "b3", "type": "user", "content": [{"type": "compaction", "text": words}]},
                  {"id": "b4", "type": "user", "content": [{"text": words}]}, {"id": "b5", "type": "user", "content": []}, {"id": "b6", "type": "user", "content": "x"},
                  {"id": "b7", "type": "user", "content": [{"type": "text", "text": 5}]}, {"id": "b8", "type": "user", "text": 5},
                  {"id": "b9", "type": "user", "text": "ok", "content": [{"type": "text", "text": words}]}, {"id": "c1", "type": "user"},
                  {"id": "c2", "type": "user", "payload": {"text": 5}}]
        for message in forged:
            self.assertEqual(self.cc.person_messages([dict(message, t=990)], set(), 1000, created), [], message)
        whole = [dict(self.user("p1", words, 1), role="user"), {"id": "p2", "role": "user", "text": words}, {"id": "p3", "type": "user", "content": [{"type": "text", "text": "366 is"}, {"type": "text", "text": "good to go"}]},
                 {"id": "p4", "type": "user", "text": "366 is\ngood to go", "content": [{"type": "text", "text": "366 is"}, {"type": "text", "text": "good to go"}]}]
        self.assertEqual([m["id"] for m in self.cc.person_messages(whole, set(), 1000, created)], ["p1", "p2", "p3", "p4"])

    def test_an_agents_message_is_never_the_persons_whatever_shape_its_metadata_takes(self):
        created = lambda m: 990
        shapes = [{"metadata": {"fleet": {"name": "cos"}}}, {"metadata": {"fleet": "x"}}, {"metadata": {"fleet": True}},
                  {"metadata": {"fleet": None}}, {"metadata": "x"}, {"metadata": ["fleet"]},
                  {"metadata": {}, "payload": {"metadata": {"fleet": {"name": "x"}}}}, {"metadata": {"a": 1}, "payload": {"metadata": {"fleet": 1}}},
                  {"payload": "x"}, {"payload": {"metadata": "x"}}]
        for n, shape in enumerate(shapes):
            message = dict(shape, id="a%d" % n, type="user", text="366 is good to go")
            self.assertEqual(self.cc.person_messages([message], set(), 1000, created), [], shape)
        person = [self.user("p1", "366", 1), self.user("p2", "366", 1, metadata={}), self.user("p3", "366", 1, metadata={"a": 1}, payload={"metadata": {}})]
        self.assertEqual([m["id"] for m in self.cc.person_messages(person, set(), 1000, created)], ["p1", "p2", "p3"])

    def test_the_age_the_future_the_cap_and_the_text_length_of_messages(self):
        times = {"edge": 100, "over": 99, "ahead": 1060, "farahead": 1061, "none": None, "str": "100", "nan": float("nan"), "bool": True}
        created = lambda m: times.get(m["id"], 1000)
        ids = lambda prompts: [m["id"] for m in self.cc.person_messages(prompts, set(), 1000, created, max_age=900)]
        self.assertEqual(ids([{"id": k, "type": "user", "text": "x"} for k in times]), ["edge", "ahead"], "exactly max_age old is read, one second older and a time too far ahead are not, no time or a non-number is not")
        long = {"id": "long", "type": "user", "content": [{"type": "text", "text": "a" * 5000}, {"type": "text", "text": "b"}]}
        self.assertEqual(len(self.cc.person_messages([long], set(), 1000, created)[0]["text"]), self.cc.TEXT_MAX)
        many = [{"id": "m%d" % n, "type": "user", "text": "x"} for n in range(80)]
        self.assertEqual(len(self.cc.person_messages(many, set(), 1000, created)), self.cc.MESSAGES_SCANNED)

    def test_at_most_offers_max_offers_the_existing_ones_first_and_the_summary_is_cut_and_clean(self):
        self.offers(7)
        holds = [self.hold(n, ["u:hold-impact"]) for n in range(100, 100 + self.cc.OFFERS_MAX + 1)] + [self.hold(7, ["u:hold-impact"])]
        got = self.cc.refresh_offers(self.state, holds, self.NAMES, 1005)
        self.assertEqual(len(got["offers"]), self.cc.OFFERS_MAX)
        self.assertTrue(got["capped"])
        self.assertIn("acme/widget#7", got["offers"], "an offer that already existed is kept before a new one is taken")
        long = self.cc.refresh_offers(self.state, [self.hold(1, ["u:hold-impact"], summary="x\x1b[31m" + "ab " * 200 + "\n")], self.NAMES, 1006)["offers"]["acme/widget#1"]["summary"]
        self.assertEqual(len(long), self.cc.SUMMARY_MAX)
        self.assertNotIn("\x1b", long)

    def test_head_matches_refuses_what_is_not_a_shown_exact_head(self):
        offer = {"head": "a" * 40, "shown": True}
        self.assertTrue(self.cc.head_matches(offer, "a" * 40))
        for current in (None, "", 5, "b" * 40):
            self.assertFalse(self.cc.head_matches(offer, current), repr(current))
        for bad in (None, "x", {"head": "a" * 40, "shown": False}, {"head": "a" * 40, "shown": "yes"}):
            self.assertFalse(self.cc.head_matches(bad, "a" * 40), repr(bad))

    def test_a_file_that_cannot_be_used_is_set_aside_and_what_is_recorded_after_it_is_not_clearable_until_shown(self):
        path = Path(self.state) / "chat-clear-offers.json"
        path.write_text("{not json")
        got = self.cc.refresh_offers(self.state, [self.hold(366, ["u:hold-impact"])], self.NAMES, 1000)
        self.assertIn("cannot be read", got["problem"])
        self.assertEqual(got["quarantined"], "chat-clear-offers.json.corrupt-1000")
        self.assertEqual((Path(self.state) / got["quarantined"]).read_text(), "{not json", "the bad file is kept, never overwritten")
        offer = got["offers"]["acme/widget#366"]
        self.assertFalse(offer["shown"])
        self.assertFalse(self.cc.head_matches(offer, "a" * 40), "recorded after a loss and never shown: nothing clears")
        self.assertTrue(self.cc.show(self.state, "acme/widget#366", "a" * 40, 1001))
        self.assertTrue(self.cc.head_matches(self.cc.read_offers(self.state)["acme/widget#366"], "a" * 40))

    def test_a_missing_label_name_makes_a_pr_not_offerable_and_a_lock_held_elsewhere_changes_nothing(self):
        names = {k: v for k, v in self.NAMES.items() if k != "hold-contract"}
        self.assertFalse(self.cc.is_offerable(names, ["u:hold-impact"]), "a hold it cannot name is a hold it cannot see")
        self.offers(366)
        with self.cc._locked(self.state):
            with self.assertRaises(self.cc.OffersBusy):
                self.cc.refresh_offers(self.state, [], self.NAMES, 2000, lock_seconds=0)
            with self.assertRaises(self.cc.OffersBusy):
                self.cc.show(self.state, "acme/widget#366", "b" * 40, 2000, lock_seconds=0)
        self.assertEqual(self.cc.read_offers(self.state)["acme/widget#366"]["head"], "a" * 40, "a busy lock changed nothing")

    def test_a_failed_write_leaves_no_temporary_file_and_the_old_offers(self):
        self.offers(366)
        real = os.replace
        def boom(*a):
            raise OSError("disk full")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.cc.write_offers(self.state, {})
        finally:
            os.replace = real
        self.assertEqual(sorted(p.name for p in Path(self.state).iterdir() if p.name.startswith("chat-clear-offers.json.")), [])
        self.assertIn("acme/widget#366", self.cc.read_offers(self.state))

    def test_a_hold_that_gains_another_hold_label_loses_its_offer_and_a_malformed_hold_is_skipped(self):
        self.offers(366)
        got = self.cc.refresh_offers(self.state, [self.hold(366, ["u:hold-impact", "u:hold-security"]), {"pr": None, "head": "a", "labels": ["u:hold-impact"]},
                                                  {"pr": "acme/widget#5", "head": 7, "labels": ["u:hold-impact"]}, {"head": "a"}], self.NAMES, 1010)
        self.assertEqual((got["offers"], got["dropped"]), ({}, ["acme/widget#366"]))
        path = Path(self.state) / "chat-clear-offers.json"
        asides = set()
        for body in (" " * (self.cc.OFFERS_MAX_BYTES + 1), json.dumps([1]), json.dumps({"offers": 5})):
            path.write_text(body)
            got = self.cc.refresh_offers(self.state, [], self.NAMES, 1011)
            self.assertTrue(got["problem"] and got["quarantined"], body[:10])
            self.assertTrue(got["quarantined"].startswith("chat-clear-offers.json.corrupt-1011"))
            asides.add(got["quarantined"])
            self.assertFalse(path.exists(), "the unusable file was set aside, not rewritten in place")
        self.assertEqual(len(asides), 3, "three unusable files, three distinct aside names")

    def test_a_non_ascii_summary_is_kept_whole(self):
        got = self.cc.refresh_offers(self.state, [self.hold(1, ["u:hold-impact"], summary="caf\u00e9 \u2014 gate")], self.NAMES, 1)
        self.assertEqual(got["offers"]["acme/widget#1"]["summary"], "caf\u00e9 \u2014 gate")
        self.assertEqual(self.cc.read_offers(self.state)["acme/widget#1"]["summary"], "caf\u00e9 \u2014 gate")
        self.assertIn("caf\u00e9".encode("utf-8"), (Path(self.state) / "chat-clear-offers.json").read_bytes(), "written as UTF-8, not escaped")
        lone = self.cc.refresh_offers(self.state, [self.hold(2, ["u:hold-impact"], summary="bad \ud800 text")], self.NAMES, 2)
        self.assertEqual(lone["offers"]["acme/widget#2"]["summary"], "bad ? text", "a lone surrogate never stops a refresh")
        twice = self.cc.refresh_offers(self.state, [self.hold(3, ["u:hold-impact"], head="a" * 40), self.hold(3, ["u:hold-impact"], head="b" * 40)], self.NAMES, 3)
        self.assertEqual((twice["added"], twice["offers"]["acme/widget#3"]["head"]), (["acme/widget#3"], "a" * 40), "a PR twice in one read is offered once")

    def test_every_run_of_digits_is_a_number_however_it_is_joined_and_one_message_names_one(self):
        n = self.cc.numbers_in
        self.assertEqual(n("366 is good to go"), [366])
        self.assertEqual(n("#389 also good to go"), [389])
        self.assertEqual(n("366 and 389, 366 again"), [366, 389])
        self.assertEqual(n("the launchd one is good to go"), [])
        self.assertEqual(n("all impacts cleared"), [])
        for joined in ("366-367", "366/367", "366,367", "366, 367", "366..367", "366...367", "366+367", "366&367", "366 and 367", "366 & 367", "#366 #367", "366;367", "366_367", "366 to 367", "366\u2013367"):
            self.assertEqual(n(joined), [366, 367], joined)
        self.assertEqual(n("0.97 sure, v2.0.28"), [0, 97, 2, 28], "digits inside decimals and versions are numbers too: it fails toward asking")
        self.assertEqual(n("\u0663\u0666\u0666 and \uff13\uff16\uff16"), [], "only ASCII digits are digits")
        self.assertLessEqual(len(str(max(n("9" * 5000 + " 1")))), self.cc.TEXT_MAX, "a huge digit run never raises and is cut at the text cap")

    def test_the_screen_never_lets_a_judge_widen_what_the_message_and_the_offers_say(self):
        offers = self.offers(366, 389, 400)    # shown at 1000
        sc = lambda text, o=offers, created=1001, now=1002: self.cc.screen(text, o, created, now)
        self.assertEqual(sc("366 is good to go"), {"verdict": "candidate", "pr": "acme/widget#366"})
        self.assertEqual(sc("366 is good to go.")["verdict"], "candidate", "a full stop after the number is fine")
        self.assertEqual(sc("#366 is good to go")["verdict"], "candidate")
        for text, why in (("all impacts cleared", "no_number"), ("the launchd one is good to go", "no_number"), ("366 and 389 are good to go", "several_numbers"),
                          ("366-367 are good to go", "several_numbers"), ("366/367 are good to go", "several_numbers"), ("366, 389 good to go", "several_numbers"),
                          ("366..389 good to go", "several_numbers"), ("366+389 good to go", "several_numbers"), ("366&389 good to go", "several_numbers"),
                          ("abc366 is good to go", "unclear_number"), ("366abc is good to go", "unclear_number"), ("366_ is good to go", "unclear_number"),
                          ("_366 is good to go", "unclear_number"), ("0366 is good to go", "unclear_number"), ("3.66 is good to go", "several_numbers"),
                          ("v366.1 is good to go", "several_numbers"), ("500 is good to go", "not_offered")):
            got = sc(text)
            self.assertEqual((got["verdict"], got["why"]), ("question", why), text)
        self.assertEqual(sc("366 is good to go", {})["why"], "no_offers")
        twice = {"acme/widget#366": self.entry(), "acme/other#366": self.entry(head="b" * 40)}
        got = sc("366 is good to go", twice)
        self.assertEqual((got["why"], got["prs"]), ("ambiguous_number", ["acme/other#366", "acme/widget#366"]))

    def test_a_message_that_reaches_the_text_cap_is_a_question_whatever_it_says_and_odd_text_is_no_number(self):
        offers = self.offers(366)
        for text in ("9" * 5000 + " is good to go", "366 is good to go" + " " * self.cc.TEXT_MAX + "389", "366 is good to go" + " " * (self.cc.TEXT_MAX - len("366 is good to go"))):
            self.assertEqual(self.cc.screen(text, offers, 1001, 1002)["why"], "too_long", len(text))
        under = "366 is good to go" + " " * (self.cc.TEXT_MAX - len("366 is good to go") - 1)
        self.assertEqual(len(under), self.cc.TEXT_MAX - 1)
        self.assertEqual(self.cc.screen(under, offers, 1001, 1002)["verdict"], "candidate", "one under the cap is read whole")
        self.assertEqual(self.cc.screen("9" * (self.cc.TEXT_MAX - 1), offers, 1001, 1002)["why"], "not_offered", "a long digit run under the cap is one number, never an error")
        self.assertEqual(self.cc.screen(None, offers, 1001, 1002)["why"], "no_number")
        self.assertEqual(self.cc.screen(5, offers, 1001, 1002)["why"], "no_number")

    def test_a_key_with_a_trailing_newline_is_no_pr_and_entries_past_the_cap_come_back_not_shown(self):
        path = Path(self.state) / "chat-clear-offers.json"
        path.write_text(json.dumps({"offers": {"acme/widget#1\n": self.entry(), "acme/widget#2": self.entry()}}))
        loaded, problem, damaged = self.cc._load(self.state)
        self.assertEqual(sorted(loaded), ["acme/widget#2"])
        self.assertEqual(damaged, {"acme/widget#1\n"}, "the key with the newline is said, not lost")
        extra = self.cc.OFFERS_MAX + 2
        many = {"acme/widget#%d" % n: self.entry() for n in range(1, extra + 1)}
        path.write_text(json.dumps({"offers": many}))
        loaded, problem, damaged = self.cc._load(self.state)
        self.assertEqual(sorted(loaded), sorted("acme/widget#%d" % n for n in range(1, self.cc.OFFERS_MAX + 1)), "the first OFFERS_MAX in file order are kept")
        self.assertEqual(damaged, {"acme/widget#%d" % n for n in range(self.cc.OFFERS_MAX + 1, extra + 1)}, "the 2 past the cap are said, not silently lost")
        first = "acme/widget#%d" % (self.cc.OFFERS_MAX + 1)
        got = self.cc.refresh_offers(self.state, [self.hold(self.cc.OFFERS_MAX + 1, ["u:hold-impact"])], self.NAMES, 9000)
        self.assertEqual(list(got["offers"]), [first])
        self.assertIs(got["offers"][first]["shown"], False, "a PR that was past the cap and is held alone is recorded as NOT shown")
        self.assertEqual(got["damaged"], sorted(damaged))

    def test_a_message_counts_only_if_it_was_written_after_the_offer_was_shown_and_an_offer_expires(self):
        offers = self.offers(366)   # shown_at 1000
        sc = lambda created, now: self.cc.screen("366 is good to go", offers, created, now)
        self.assertEqual(sc(999, 1100)["why"], "before_offer", "written before the offer was shown")
        self.assertEqual(sc(1000, 1100)["why"], "before_offer", "the same second: refused")
        self.assertEqual(sc(1001, 1100)["verdict"], "candidate", "one second after: counts")
        last = 1000 + self.cc.OFFER_TTL
        self.assertEqual(sc(1001, last)["verdict"], "candidate", "exactly the lifetime old: still counts")
        self.assertEqual(sc(1001, last + 1)["why"], "expired")
        for bad in (None, "1001", float("nan"), True):
            self.assertEqual(sc(bad, 1100)["why"], "no_time", repr(bad))
        self.assertEqual(self.cc.screen("366 is good to go", offers, 1001, None)["why"], "no_time")
        self.assertEqual(sc(1000 + self.cc.MESSAGE_SKEW + 101, 1100)["why"], "no_time", "a message from the future")
        self.assertEqual(self.cc.screen("366 is good to go", {"acme/widget#366": self.entry(shown_at=2000)}, 2001, 1000)["why"], "no_time", "an offer shown in the future")
        for bad in ({"head": "a" * 40}, {"head": "a" * 40, "shown_at": "1000"}, {"head": "a" * 40, "shown_at": 1.5}):
            self.assertEqual(self.cc.screen("366 is good to go", {"acme/widget#366": bad}, 1001, 1100)["why"], "no_time", bad)
        self.cc.show(self.state, "acme/widget#366", "a" * 40, 5000)   # shown again at 5000: an older message no longer counts
        again = self.cc.read_offers(self.state)
        self.assertEqual(self.cc.screen("366 is good to go", again, 4000, 5100)["why"], "before_offer")
        self.assertEqual(self.cc.screen("366 is good to go", again, 5001, 5100)["verdict"], "candidate")

    def test_this_module_has_no_command_and_no_caller_in_the_cli_and_writes_no_label(self):
        source = (Path(sb.__file__).parent / "chat_clear.py").read_text()
        for word in ("subprocess", "urllib", "socket", "change_issue_labels"):
            self.assertNotIn(word, source, "slice 1 reaches nothing: no process, no network, no GitHub write")
        import ast
        main = switchboard_source()
        for fn in [n for n in ast.walk(ast.parse(main)) if isinstance(n, ast.FunctionDef) and n.name.startswith("cmd_")]:
            self.assertNotIn("chat_clear", ast.get_source_segment(main, fn), "%s: no command reaches the chat clear (slice 3's hook is the daemon's alone; TestChatClearHook S7 counts the call sites)" % fn.name)


class TestClearCarry(unittest.TestCase):
    """#510: an owner clear carries over a pure merge of main and over nothing else. Every case runs on a REAL git repository (neutral names)."""

    REPO = "acme-org/widget-api"

    def setUp(self):
        self.cc = sb.load_module("clear_carry")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        self.api = build_carry_repo(self.dir)
        self.cleared = self.api.rev("feature")
        self.fp = self.fingerprint()

    def write(self, path, text, binary=False):
        target = self.dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.write_bytes if binary else target.write_text)(text)

    def fingerprint(self):
        fingerprint, head, files = self.cc.own_change_fingerprint(self.api.get, self.REPO, 7)
        self.assertEqual(head, self.api.rev("feature"))
        return fingerprint

    def main_moves(self, *names, edit=None):
        g = self.api.git
        g("checkout", "-q", "main")
        for name in names:
            self.write(name, "from main %s\n" % name)
        if edit:
            self.write(*edit)
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "feature")

    def merge_main(self):
        self.api.git("merge", "-q", "--no-edit", "main")
        return self.api.rev("feature")

    def decide(self, new):
        return self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new)

    def test_s1_a_pure_merge_of_main_carries_the_clear_and_the_fingerprint_is_the_original(self):
        self.main_moves("m1.txt", "m2.txt", "m3.txt")
        new = self.merge_main()
        verdict = self.decide(new)
        self.assertEqual((verdict["carried"], verdict["reason"], verdict["fingerprint"], verdict["from"], verdict["to"]), (True, None, self.fp, self.cleared, new))

    def test_s2_a_commit_of_its_own_an_edited_line_an_added_or_a_removed_file_void_the_clear(self):
        g = self.api.git
        for label, change in (("a commit of its own", lambda: (self.write("d.txt", "d\n"), g("add", "-A"), g("commit", "-q", "-m", "own"))),
                              ("an edited line", lambda: (self.write("a.txt", "one\nTWO!\nthree\nfour\nfive\nsix\nseven\n"), g("commit", "-q", "-am", "edit"))),
                              ("an added file", lambda: (self.write("e.txt", "e\n"), g("add", "-A"), g("commit", "-q", "-m", "add"))),
                              ("a removed file", lambda: (g("rm", "-q", "c.txt"), g("commit", "-q", "-m", "remove")))):
            with self.subTest(label):
                g("checkout", "-q", "-B", "feature", self.cleared)
                self.main_moves("m_%s.txt" % label.replace(" ", "_"))
                self.merge_main()
                change()
                verdict = self.decide(self.api.rev("feature"))
                self.assertFalse(verdict["carried"], label)
                self.assertIn("commit of the pull request's own", verdict["reason"])
        # an "evil merge": main merged, and the merge commit itself edits or adds a file
        for label, edit in (("edits a cleared file", lambda: self.write("c.txt", "changed in the merge\n")), ("adds a file", lambda: self.write("d.txt", "sneaked in\n"))):
            with self.subTest(label):
                g("checkout", "-q", "-B", "feature", self.cleared)
                self.main_moves("n_%s.txt" % label.replace(" ", "_"))
                g("merge", "-q", "--no-commit", "--no-ff", "main")
                edit()
                g("add", "-A")
                g("commit", "-q", "-m", "merge main (and more)")
                verdict = self.decide(self.api.rev("feature"))
                self.assertFalse(verdict["carried"], label)
                self.assertIn("relative to both parents", verdict["reason"])

    def test_s3_a_conflict_resolved_by_hand_voids_the_clear_even_though_the_file_list_is_the_same(self):
        g = self.api.git
        self.main_moves(edit=("a.txt", "one\nTWO from main\nthree\nfour\nfive\nsix\nseven\n"))
        done = subprocess.run(["git", "-C", str(self.dir), "merge", "--no-edit", "main"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0, "the merge conflicts in a.txt")
        self.write("a.txt", "one\nTWO resolved by hand\nthree\nfour\nfive\nsix\nseven\n")
        g("add", "-A")
        g("commit", "-q", "-m", "merge main, conflict resolved")
        verdict = self.decide(self.api.rev("feature"))
        self.assertFalse(verdict["carried"])
        self.assertIn("a.txt", verdict["reason"])
        self.assertIn("relative to both parents", verdict["reason"])
        same_files = self.cc.own_change(self.api.get, self.REPO, 7)[0]
        self.assertEqual(sorted(f["path"] for f in same_files), ["a.txt", "c.txt"], "the same two files: only the content check catches it")

    def test_s4_a_rewritten_history_with_the_same_final_diff_is_void(self):
        g = self.api.git
        self.main_moves("m1.txt")
        g("rebase", "-q", "main")
        new = self.api.rev("feature")
        self.assertNotEqual(new, self.cleared)
        self.assertEqual(self.fingerprint(), self.fp, "the rebased pull request has exactly the same own change")
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("rewritten history", verdict["reason"])

    def test_s5_a_cut_file_list_an_omitted_patch_and_a_read_error_void_the_clear_and_name_what_could_not_be_read(self):
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.api.changed_files_override = 5
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("cut: GitHub lists 2 of 5 files", verdict["reason"])
        self.api.changed_files_override = None
        self.api.fail = "/compare/"
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"])
        self.assertIn("could not be read", verdict["reason"])
        self.api.fail = None
        g = self.api.git
        g("checkout", "-q", "-B", "feature", self.cleared)
        self.write("blob.dat", bytes(range(256)) * 4, binary=True)
        g("add", "-A")
        g("commit", "-q", "-m", "binary")
        with self.assertRaises(self.cc.CarryVoid) as raised:
            self.cc.own_change_fingerprint(self.api.get, self.REPO, 7)
        self.assertIn("patch of blob.dat is omitted", str(raised.exception))
        binary_cleared = self.api.rev("feature")
        self.main_moves("m2.txt")
        merged = self.merge_main()
        verdict = self.cc.decide(self.api.get, self.REPO, 7, binary_cleared, "sha256:" + "0" * 64, merged)
        self.assertFalse(verdict["carried"])
        self.assertIn("omitted", verdict["reason"])
        verdict = self.cc.decide(self.api.get, self.REPO, 7, self.cleared, None, new)
        self.assertEqual((verdict["carried"], verdict["reason"]), (False, "no fingerprint was recorded with the clear"))
        self.assertFalse(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, "not-a-hash", new)["carried"])

    def test_a_merge_of_a_side_branch_a_retargeted_base_and_a_head_that_moves_mid_read_all_void(self):
        g = self.api.git
        g("checkout", "-q", "-b", "side", self.cleared)
        self.write("side.txt", "side\n")
        g("add", "-A")
        g("commit", "-q", "-m", "side")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-ff", "--no-edit", "side")
        verdict = self.decide(self.api.rev("feature"))
        self.assertFalse(verdict["carried"])
        self.assertIn("not on main", verdict["reason"])
        g("checkout", "-q", "-B", "feature", self.cleared)
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.assertTrue(self.decide(new)["carried"])
        g("branch", "release", self.cleared)
        self.api.base = "release"
        self.assertFalse(self.decide(new)["carried"], "a retargeted base is a different change")
        self.api.base = "main"
        calls = {"n": 0}
        real = self.cc.pull_head

        def moving(get, repo, number):
            calls["n"] += 1
            head, pull = real(get, repo, number)
            return (head if calls["n"] == 1 else "0" * 40), pull
        self.cc.pull_head = moving
        self.addCleanup(setattr, self.cc, "pull_head", real)
        self.assertFalse(self.decide(new)["carried"])

    def test_s6_successive_merges_are_each_compared_with_the_original_and_an_own_change_makes_the_next_void(self):
        g = self.api.git
        carried = []
        for n in (1, 2, 3):
            self.main_moves("m%d.txt" % n)
            carried.append(self.decide(self.merge_main())["carried"])   # each decided when it is the current head, against the ORIGINAL cleared head and fingerprint
        self.assertEqual(carried, [True, True, True], "three merges, three carries")
        self.write("a.txt", "one\nTWO\nthree\nfour\nfive\nsix\nSEVEN\n")
        g("commit", "-q", "-am", "own change after the second merge")
        self.main_moves("m4.txt")
        fourth = self.merge_main()
        verdict = self.decide(fourth)
        self.assertFalse(verdict["carried"], "a change of its own after a carry makes every later head void")

    def test_a_pure_merge_whose_own_change_is_not_the_one_cleared_is_void_at_the_last_check_and_each_merge_is_compared_with_the_original(self):
        g = self.api.git
        self.main_moves("m1.txt")
        new = self.merge_main()
        wrong = "sha256:" + "1" * 64
        verdict = self.cc.decide(self.api.get, self.REPO, 7, self.cleared, wrong, new)
        self.assertFalse(verdict["carried"], "the merge chain and the merges are clean: only the fingerprint comparison can refuse this")
        self.assertIn("is not the one the owner cleared", verdict["reason"])
        self.assertEqual(verdict["fingerprint"], self.fp, "the verdict carries the fingerprint it computed, for the audit")
        # drift: a pure merge whose own change differs from the ORIGINAL but equals the PREVIOUS head's is still void
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new)["carried"])
        previous_fp = self.fingerprint()
        self.assertEqual(previous_fp, self.fp)
        drifted = "sha256:" + "2" * 64            # what a build comparing with an earlier carried head would hold after a small change slipped in
        self.main_moves("m2.txt")
        second = self.merge_main()
        self.assertFalse(self.cc.decide(self.api.get, self.REPO, 7, self.cleared, drifted, second)["carried"])

    def test_a_pure_rename_and_a_renamed_and_edited_file_carry_over_a_merge_and_an_edit_after_the_clear_does_not(self):
        g = self.api.git
        g("checkout", "-q", "-B", "feature", "main")
        g("mv", "src/x.py", "src/y.py")
        g("commit", "-q", "-m", "rename")
        cleared = self.api.rev("feature")
        files, _, _ = self.cc.own_change(self.api.get, self.REPO, 7)
        self.assertEqual([(f["path"], f["status"], f["previous"]) for f in files], [("src/y.py", "renamed", "src/x.py")])
        fp = self.fingerprint()
        self.main_moves("m1.txt")
        new = self.merge_main()
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, cleared, fp, new)["carried"], "a pure rename's blob id pins its content")
        # a rename that is also edited carries too (its patch is present and pinned); an edit AFTER the clear does not
        g("checkout", "-q", "-B", "feature", "main")
        g("mv", "src/x.py", "src/z.py")
        self.write("src/z.py", "def x():\n    return 2\n")
        g("add", "-A")
        g("commit", "-q", "-m", "rename and edit")
        edited = self.api.rev("feature")
        edited_fp = self.fingerprint()
        self.main_moves("m2.txt")
        merged = self.merge_main()
        self.assertTrue(self.cc.decide(self.api.get, self.REPO, 7, edited, edited_fp, merged)["carried"])
        self.write("src/z.py", "def x():\n    return 3\n")
        g("commit", "-q", "-am", "edit after the clear")
        self.main_moves("m3.txt")
        verdict = self.cc.decide(self.api.get, self.REPO, 7, edited, edited_fp, self.merge_main())
        self.assertFalse(verdict["carried"])
        self.assertIn("commit of the pull request's own", verdict["reason"])

    def test_s8_there_is_one_fingerprint_function_and_the_carry_code_holds_no_path_or_class_literal(self):
        import ast
        main = switchboard_source() + "\n" + (Path(sb.__file__).parent / "impact_hold.py").read_text()   # #532: the clear's wrapper moved to bin/impact_hold.py, still "the switchboard's" code
        module = Path(sb.__file__).parent / "clear_carry.py"
        text = module.read_text()
        defs = [n.name for src in (main, text) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and "fingerprint" in n.name]
        self.assertEqual(sorted(defs), ["clear_fingerprint", "fingerprint_of", "own_change_fingerprint"], "one computation (fingerprint_of), one entry point (own_change_fingerprint) and the clear's wrapper")
        calls = [n for n in ast.walk(ast.parse(main)) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "own_change_fingerprint"]
        self.assertEqual(len(calls), 1, "the clear's wrapper is the only caller in the switchboard; the carry's decide calls the same function inside the module")
        imports = sorted(a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Import) for a in n.names)
        self.assertEqual(imports, ["hashlib", "json"], "the carry imports no floor, glob or class code: it decides from GitHub's answers alone")


class TestConflictCarry(unittest.TestCase):
    """#815: an owner clear carries over a merge of main whose conflict was resolved by hand, only on unchanged added and removed LINES, with no protected path touched and a confirmation. Real git."""

    REPO = "acme-org/widget-api"
    RESOLVED = "zero\none\nTWO\nTHREE\nfour\nfive\nsix\nseven\n"

    def setUp(self):
        self.cc = sb.load_module("clear_carry")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        self.api = build_carry_repo(self.dir)
        self.base_main = self.api.rev("main")
        self.arm()

    def arm(self):
        self.cleared = self.api.rev("feature")
        self.fp = self.cc.own_change_fingerprint(self.api.get, self.REPO, 7)[0]
        self.lfp = self.cc.own_change_fingerprint(self.api.get, self.REPO, 7, lines=True)[0]

    def write(self, path, text):
        target = self.dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def conflict_merge(self, resolved=None, main_files=(), after=None):
        """Main edits the line next to the pull request's (a real conflict) and moves the file down; the merge is resolved by hand. `after()` may change more before the merge commit."""
        g = self.api.git
        g("checkout", "-q", "main")
        self.write("a.txt", "zero\none\ntwo\nTHREE\nfour\nfive\nsix\nseven\n")
        for path in main_files:
            self.write(path, "from main\n")
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "feature")
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.dir), "merge", "--no-commit", "--no-ff", "main"], capture_output=True).returncode, 0, "the fixture must really conflict")
        self.write("a.txt", resolved or self.RESOLVED)
        if after:
            after()
        g("add", "-A")
        g("commit", "-q", "-m", "merge main, conflict resolved")
        return self.api.rev("feature")

    def decide(self, new, confirm=lambda head: "ledger entry 1", protected="table", lines=True):
        return self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new, lines_fingerprint=self.lfp if lines else None, confirm=confirm,
                              protected=sb.impact_floor.carry_never_paths() if protected == "table" else protected)

    def test_s1_a_conflict_resolved_by_hand_with_the_lines_unchanged_carries_only_through_the_lines_route(self):
        new = self.conflict_merge()
        pure = self.cc.decide(self.api.get, self.REPO, 7, self.cleared, self.fp, new)
        self.assertFalse(pure["carried"], "the pure route refuses it, as before")
        self.assertIn("both parents", pure["reason"])
        verdict = self.decide(new)
        self.assertEqual((verdict["carried"], verdict["route"], verdict["reason"], verdict["confirmation"], verdict["from"], verdict["to"], verdict["lines_fingerprint"]),
                         (True, "lines", None, "ledger entry 1", self.cleared, new, self.lfp))

    def test_the_lines_fingerprint_ignores_context_and_line_numbers_and_sees_every_added_or_removed_line(self):
        patch = "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three"
        moved = "@@ -40,5 +41,5 @@\n zero\n one\n-two\n+TWO\n THREE\n four"
        self.assertEqual(self.cc.changed_lines(patch), ["-two", "+TWO"])
        self.assertEqual(self.cc.changed_lines(patch), self.cc.changed_lines(moved))
        for other in ("@@ -1,3 +1,3 @@\n one\n-two\n+TWO!\n three", "@@ -1,3 +1,3 @@\n one\n-twp\n+TWO\n three", "@@ -1,3 +1,3 @@\n one\n+TWO\n three", "@@ -1,3 +1,4 @@\n one\n-two\n+TWO\n+x\n three"):
            self.assertNotEqual(self.cc.changed_lines(patch), self.cc.changed_lines(other), other)
        self.assertEqual(self.cc.changed_lines("@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b"), ["-a", "\\ No newline at end of file", "+b"])
        self.assertEqual(self.cc.changed_lines("@@ -1 +1 @@\n a\n\\ No newline at end of file\n-b\n+c"), ["-b", "+c"], "a marker after a context line is about the surroundings")

    def test_s2_one_added_or_removed_line_that_differs_is_refused_and_so_is_a_commit_of_its_own(self):
        def edit(path, text):
            return lambda: self.write(path, text)
        cases = (("an added line edited", dict(resolved=self.RESOLVED.replace("TWO\n", "TWO!\n")), "a.txt"),
                 ("a removed line kept", dict(resolved=self.RESOLVED.replace("TWO\n", "two\nTWO\n")), "a.txt"),
                 ("the change dropped", dict(resolved=self.RESOLVED.replace("TWO", "two")), "a.txt"),
                 ("a line added by the resolution", dict(resolved=self.RESOLVED + "extra\n"), "a.txt"),
                 ("the added file edited", dict(after=edit("c.txt", "new file edited\n")), "c.txt"),
                 ("the added file dropped", dict(after=lambda: self.api.git("rm", "-q", "-f", "c.txt")), "c.txt"),
                 ("a file added by the resolution", dict(after=edit("d.txt", "sneaked in\n")), "d.txt"))
        for label, kwargs, named in cases:
            with self.subTest(label):
                g = self.api.git
                g("checkout", "-q", "-B", "feature", self.cleared)
                g("branch", "-q", "-f", "main", self.base_main)
                verdict = self.decide(self.conflict_merge(**kwargs))
                self.assertFalse(verdict["carried"], label)
                self.assertEqual(verdict["route"], None, label)
                self.assertIn("differs", verdict["reason"], label)
                self.assertFalse(verdict["waiting"], "a changed line is not a missing confirmation: %s" % label)
        g = self.api.git
        g("checkout", "-q", "-B", "feature", self.cleared)
        g("branch", "-q", "-f", "main", self.base_main)
        new = self.conflict_merge()
        self.write("e.txt", "own\n")
        g("add", "-A")
        g("commit", "-q", "-m", "own")
        verdict = self.decide(self.api.rev("feature"))
        self.assertFalse(verdict["carried"])
        self.assertIn("commit of the pull request's own", verdict["reason"])

    def test_s3_every_member_of_the_table_is_refused_when_a_merge_touches_it_or_the_pull_request_changes_it(self):
        table = sb.impact_floor.carry_never_paths()
        self.assertGreaterEqual(len(table), 4)
        for path in table:
            for how in ("a merge touches it", "the pull request's own change touches it"):
                with self.subTest(path=path, how=how):
                    self.tearDown_repo()
                    g = self.api.git
                    if how.startswith("the pull"):
                        self.write(path, "own change\n")
                        g("add", "-A")
                        g("commit", "-q", "-m", "own change in a protected path")
                    self.arm()
                    verdict = self.decide(self.conflict_merge(main_files=([path] if not how.startswith("the pull") else [])))
                    self.assertFalse(verdict["carried"], (path, how))
                    self.assertIn("never carried", verdict["reason"], (path, how))
                    self.assertFalse(verdict["waiting"])

    def tearDown_repo(self):
        shutil.rmtree(str(self.dir))
        self.api = build_carry_repo(self.dir)

    def test_a_protected_path_is_matched_in_any_case_and_spelling(self):
        for spelled in ("BIN/Impact_Hold.py", "./bin/impact_hold.py", "bin\\impact_hold.py", "/bin/impact_hold.py"):
            self.assertEqual(self.cc._norm(spelled), "bin/impact_hold.py", spelled)
        self.assertEqual(self.cc._norm(".github/x"), ".github/x", "a leading dot of a name is not a ./ prefix")

    def test_a_rename_away_from_a_protected_path_and_an_odd_spelling_are_refused_through_decide(self):
        g = self.api.git
        g("checkout", "-q", "main")
        self.write("bin/impact_hold.py", "x = 1\n")
        g("add", "-A")
        g("commit", "-q", "-m", "main gets the file")
        self.base_main = self.api.rev("main")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-edit", "main")
        g("mv", "bin/impact_hold.py", "bin/other_name.py")
        g("commit", "-q", "-m", "the pull request renames it away")
        self.arm()
        verdict = self.decide(self.conflict_merge())
        self.assertFalse(verdict["carried"], "the rename's previous path is a protected path")
        self.assertIn("impact_hold.py", verdict["reason"])
        self.tearDown_repo()
        self.base_main = self.api.rev("main")
        self.arm()
        verdict = self.decide(self.conflict_merge(main_files=["BIN/Impact_Hold.py"]))
        self.assertFalse(verdict["carried"], "an odd spelling is the same path")
        self.assertIn("never carried", verdict["reason"])

    def test_the_table_is_the_clear_code_the_matrix_names_and_every_clear_or_hold_module(self):
        table = set(sb.impact_floor.carry_never_paths())
        root = Path(sb.__file__).resolve().parent.parent
        matrix = json.loads((root / "docs/review-laws/impact-matrix.json").read_text())
        listed = set(matrix["owner_hold"]["delegated_clear"]["clear_code_paths"]) if "owner_hold" in matrix else set(matrix["delegated_clear"]["clear_code_paths"])
        self.assertEqual(sorted(table - listed), [], "every protected path is one the matrix lists as the clear's own code")
        exact = {x for x in listed if "*" not in x}
        self.assertEqual(sorted(exact - table), [], "every exact path the matrix lists as the clear's own code is never carried (plan_ledger decides who may confirm)")
        left_out = sorted(listed - table)
        self.assertTrue(all("*" in x for x in left_out), "only the directory globs are left out, on purpose: %s" % left_out)
        for path in table:
            self.assertTrue((root / path).exists(), path)
        named = {"bin/%s" % p.name for p in (root / "bin").glob("*.py") if re.search(r"clear|hold|floor", p.name)}
        self.assertEqual(sorted(named - table), [], "a bin module named for the clear, the hold or the floor is in the table")
        self.assertIn("docs/review-laws/impact-matrix.json", table)

    def test_s4_the_confirmation_is_asked_for_exactly_this_head_and_an_empty_answer_leaves_the_carry_waiting(self):
        new = self.conflict_merge()
        asked = []
        for label, confirm in (("none", lambda head: None), ("empty", lambda head: ""), ("a void", lambda head: (_ for _ in ()).throw(self.cc.CarryVoid("no confirmation at this head")))):
            verdict = self.decide(new, confirm=lambda head, c=confirm: (asked.append(head), c(head))[1])
            self.assertFalse(verdict["carried"], label)
            self.assertTrue(verdict["waiting"], label)
            self.assertIn("no confirmation", verdict["reason"], label)
        self.assertEqual(set(asked), {new})
        self.assertFalse(self.decide(new, confirm=None)["carried"], "no confirmation source: refused")
        self.assertFalse(self.decide(new, lines=False)["carried"], "a clear with no lines fingerprint: refused")

    def test_s3_no_table_given_refuses_the_conflict_route(self):
        new = self.conflict_merge()
        for protected in (None, (), []):
            self.assertFalse(self.decide(new, protected=protected)["carried"], repr(protected))

    def test_u2_a_cut_omitted_or_unreadable_input_refuses_the_carry(self):
        new = self.conflict_merge(after=lambda: (self.dir / "b.bin").write_bytes(b"\x00\x01\xff\xfe" * 8))
        verdict = self.decide(new)
        self.assertFalse(verdict["carried"], "an omitted patch (a binary file) is never read as unchanged")
        g = self.api.git
        g("checkout", "-q", "-B", "feature", self.cleared)
        g("branch", "-q", "-f", "main", self.base_main)
        new = self.conflict_merge()
        self.assertTrue(self.decide(new)["carried"], "control: the same merge without the binary file carries")
        for label, setup in (("the file list is cut", lambda: setattr(self.api, "changed_files_override", 99)), ("a read fails", lambda: setattr(self.api, "fail", "/files"))):
            with self.subTest(label):
                setup()
                self.addCleanup(setattr, self.api, "changed_files_override", None)
                self.addCleanup(setattr, self.api, "fail", None)
                verdict = self.decide(new)
                self.assertFalse(verdict["carried"], label)
                self.assertTrue(verdict["reason"], label)
                self.api.changed_files_override, self.api.fail = None, None
        # the head moves while it is read
        real = self.api.get
        moved = []

        def get(path):
            answer = real(path)
            if path.endswith("pulls/7/files?per_page=100&page=1") and not moved:
                moved.append(1)
                self.api.git("commit", "-q", "--allow-empty", "-m", "moved")
            return answer
        self.api.get = get
        self.assertFalse(self.decide(new)["carried"])


class TestRoutineLocal(unittest.TestCase):
    """Issue 87: the pre-filter that says a command is a read or a local reversible step, and nothing else."""

    ROUTINE = [
        "git status", "git status -s -b", "git diff", "git diff --stat", "git diff origin/main --stat", "git diff --cached --name-only", "git log --oneline -5",
        "git log -n 5 --stat", "git show --stat HEAD", "git show HEAD", "git add -A", "git add src/a.py docs/b.md", "git commit -m 'fix the thing (#9)'",
        "git commit --amend --no-edit", "git rev-parse --short HEAD", "git grep -n foo", "git grep -n foo src", "grep -n foo src/a.py", "grep -rn foo src docs",
        "ls", "ls -la bin", "cat docs/a.md", "cat -n a.txt", "head -n 5 a.txt", "tail -5 a.txt", "wc -l a.txt", "uptime", "pwd", "sleep 20",
        "node --test tests/a.test.mjs", "python3 bin/test-switchboard TestX.test_y", "fleet-switchboard report done --issue 9 'built it'",
        "fleet-switchboard report working 'still on it'", "fleet-switchboard report done 'a' 'b'",
        # round 2: the fleet's own messaging, and gh reads
        "fleet-switchboard notice pr acme/api#7", "fleet-switchboard notice pr https://github.com/acme/api/pull/7 'x'", "fleet-switchboard intent 87", "fleet-switchboard intent acme/api#87",
        "fleet-switchboard orders list --charter 5", "fleet-switchboard orders list --charter 5 --json", "fleet-switchboard orders --cos", "fleet-switchboard decisions", "fleet-switchboard decisions list",
        "gh pr view 94", "gh pr view 94 --repo acme/api --json state,title,mergeable", "gh pr list --repo acme/api --state open --limit 30", "gh pr checks 3 --repo acme/api",
        "gh pr diff 3 --name-only", "gh issue view 9 --comments", "gh issue list --repo acme/api --label wip --state all", "gh api 'repos/acme/api/pulls?state=open&per_page=50'",
    ]
    # every bypass the review of PR #90 named, and more
    NOT_ROUTINE = [
        "", "   ", "a" * 1100, "ls\x00", "ls\nls",
        # an environment prefix runs code before the command
        "GIT_SSH_COMMAND=x git status", "LD_PRELOAD=/tmp/x.so ls", "FOO=1 ls", "PATH=/tmp ls", "GIT_EXTERNAL_DIFF=x git diff", "env FOO=1 ls", "git status GIT_X=1",
        # git flags that write, run a program or reach out
        "git diff --output=out", "git diff --output out", "git log --output=out", "git show --output=x HEAD", "git grep -O sh foo", "git grep --open-files-in-pager=sh foo",
        "git grep -f patterns foo", "git fetch --upload-pack=sh", "git fetch origin", "git ls-remote --upload-pack=x", "git diff --ext-diff", "git log --format=%H", "git log --pretty=oneline",
        "git status --foo", "git -C /etc status", "git -c core.pager=sh log", "git --git-dir=/x status", "git --exec-path=/x status", "git commit", "git commit -m", "git commit -a", "git commit -m x --no-verify",
        "git commit -F /etc/passwd", "git add", "git add .env", "git add ../x", "git add .git/config", "git push", "git push --force", "git reset --hard", "git clean -fd", "git checkout -- .", "git rebase main",
        "git branch -D x", "git stash drop", "git tag v1", "git remote add o u", "git config user.email x", "git worktree add ../x", "git submodule update", "git gc", "git filter-branch",
        # other commands: dropped from the list because their flags can write or run a program
        "sort -o out in", "sort in", "rg --pre sh x", "rg foo", "sed -n 'w out' f", "sed -n '1e id' f", "sed -i '' 's/a/b/' f", "sed -n 1p f", "awk 'BEGIN{system(\"id\")}'",
        "gh auth status --show-token", "gh pr edit 1 --add-label x", "gh api repos/a/b", "gh pr merge 1", "find . -delete", "find . -exec id ;", "xargs ls", "tee x", "cp a b", "mv a b",
        "mkdir x", "touch x", "rm x", "rm -rf build", "cd bin", "echo hi", "printf x", "date", "date -s x", "env", "printenv", "which ls", "stat x", "file x", "diff a b", "jq . a.json",
        "curl http://x", "wget http://x", "ssh host ls", "docker ps", "docker compose up", "npm test", "npm publish", "npx foo", "python3 evil.py", "python3 bin/test-x; id", "python3 bin/../evil.py",
        "python3 -c 'import os'", "python3 - <<'EOF'\nprint(1)\nEOF", "node script.js", "node --test /etc/x", "node --test ../x", "node --eval x", "bash -c ls", "sh x.sh", "eval ls", "nice ls", "timeout 5 ls",
        # chaining, pipes, redirects, substitution, globs, quoting tricks
        "ls; ls", "ls && ls", "ls || ls", "ls | head", "git diff | head", "ls &", "ls > x", "ls >> x", "ls 2>&1", "ls > /dev/null", "ls < x", "cat a | sh", "ls $(id)", "ls `id`", "ls ${HOME}", "echo $HOME",
        "ls *", "ls ?", "ls [a]", "ls ~", "ls ~root", "cat <(ls)", "ls 'unbalanced", "ls \"unbalanced", "ls #comment", "ls !", "ls \\n", "ls {a,b}", "ls (a)",
        # paths outside the worktree, dotfiles, credentials
        "cat /etc/passwd", "cat /private/tmp/x", "cat ~/.ssh/id_rsa", "cat ../x", "cat a/../../x", "cat .env", "cat .git/config", "cat .ssh/id_rsa", "cat a/.env", "cat secrets.txt", "cat my_token.txt",
        "cat server.pem", "cat id_rsa", "cat .netrc", "cat credentials.json", "cat .npmrc", "ls /", "ls -la /etc", "ls ..", "ls .git", "ls .ssh", "head -n 5 /etc/hosts", "tail ../x", "wc -l /etc/passwd",
        "grep -rn foo /etc", "grep -rn foo ..", "grep foo", "grep -e foo a", "git grep", "git diff ../x", "git diff /etc/x", "git log -- /etc", "git show HEAD:../x", "git diff a..b",
        # flags not on the list, or with the wrong value
        "ls -z", "ls --color=always", "ls -la -z", "cat -z a", "head -n x a", "head -n", "head -z a", "tail -f a", "wc -m a", "grep -P foo a", "grep --include=x foo a", "git log -n", "git log -n x",
        "git log --all", "git diff -U99999 --", "sleep", "sleep x", "sleep 1 2", "sleep -1", "pwd -P", "uptime -p",
        # fleet-switchboard: only report <state> [--issue N] "<one line>"
        "fleet-switchboard launch x", "fleet-switchboard decisions answer 1 yes", "fleet-switchboard orders", "fleet-switchboard report", "fleet-switchboard report nonsense x", "fleet-switchboard report done",
        "fleet-switchboard report done --issue x 'y'", "fleet-switchboard report done --foo 'y'", "./fleet-switchboard report done 'x'", "bin/fleet-switchboard report done 'x'",
    ]

    def test_reads_and_local_reversible_steps_are_routine(self):
        for command in self.ROUTINE:
            with self.subTest(command=command):
                self.assertTrue(sb.routine_local(command), command)

    def test_anything_that_acts_outside_or_cannot_be_read_with_certainty_is_not(self):
        for command in self.NOT_ROUTINE:
            with self.subTest(command=command):
                self.assertFalse(sb.routine_local(command), command)
        for odd in (None, 5, [], {}, b"ls"):
            self.assertFalse(sb.routine_local(odd))

    def test_the_defaults_and_the_setting(self):
        self.assertEqual(sb.POLICY_DEFAULT_THRESHOLDS, {"outside_intent": 0.9, "speaks_for_you": 0.9, "outside_scope": 0.9, "hard_to_reverse": 0.5})
        self.assertEqual(sb.validate_config({"jev": {}, "policy": {}})["policy"]["thresholds"], sb.POLICY_DEFAULT_THRESHOLDS)
        got = sb.validate_config({"jev": {}, "policy": {"thresholds": {"outside_intent": 0.5}}})["policy"]
        self.assertEqual((got["thresholds"]["outside_intent"], got["thresholds"]["speaks_for_you"], got["routine_filter"]), (0.5, 0.9, True))
        for bad in ("yes", 1, None):
            with self.subTest(bad=bad), self.assertRaises(sb.ConfigError):
                sb.validate_config({"jev": {}, "policy": {"routine_filter": bad}})
        self.assertEqual(sb.combine_policy({"outside_intent": 0.6}, None, "allow")["outcome"], "hard-deny", "the library default is unchanged")


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


class TestHeavySlots(Base):
    """`fleet-switchboard heavy` (ask #34): a flock slot semaphore in the state directory; the COMMAND holds the lock; the queue, the kill, the exit status, taskpolicy
    or nice, the holder list, the wait limit and the no-shell guarantee are each run for real with small commands."""

    def setUp(self):
        super().setUp()
        self.hs = sb.load_heavy_slots()
        self.dir = self.home / "slots"
        self.dir.mkdir()
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=90)
            except Exception:
                pass

    def env(self, **more):
        env = dict(os.environ, XDG_STATE_HOME=str(self.dir), FLEET_HEAVY_REPORT_SECONDS="0.3", PATH=os.environ["PATH"])
        for key in ("FLEET_HEAVY_SLOT", "FLEET_HEAVY", "CI"):   # the suite may itself run in a slot, under FLEET_HEAVY=off, or in CI: these tests are about the slots
            env.pop(key, None)
        env.update(more)
        return env

    def state(self):
        path = self.dir / "fleet-switchboard"
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def start(self, *words, slots=3, wait_max=None, name="t", env=None, new_session=True, **popen):
        argv = [sys.executable, str(SWITCHBOARD), "heavy", "--slots", str(slots), "--name", name]
        if wait_max is not None:
            argv += ["--wait-max", str(wait_max)]
        argv = argv + ["--"] + list(words)
        if self.START_DELAY:   # a holder slow to write its record: the real wrapper holds its lock at once but its record (os.pwrite) lands START_DELAY seconds late
            runner = ("import os, sys, time, runpy; real, delay = os.pwrite, float(sys.argv[1]); os.pwrite = lambda *a: (time.sleep(delay), real(*a))[1]; "
                      "script = sys.argv[2]; sys.argv = [script] + sys.argv[3:]; runpy.run_path(script, run_name='__main__')")
            argv = [sys.executable, "-c", runner, str(self.START_DELAY), str(SWITCHBOARD)] + argv[2:]
        proc = subprocess.Popen(argv, env=env or self.env(), start_new_session=new_session, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=popen.pop("stdin", subprocess.DEVNULL), text=True, **popen)
        self.procs.append(proc)
        return proc

    def waiter(self, tag):
        """A command that says it started and runs until its release file exists."""
        script = 'touch "%s/started.%s"; while [ ! -e "%s/release.%s" ]; do sleep 0.05; done' % (self.home, tag, self.home, tag)
        return ["sh", "-c", script]

    HOLD_CEILING_SECONDS = 300   # the integration test's preconditions: a bound that costs nothing when they hold, on the slowest runner

    def drain(self, proc):
        """What a wrapper has written to its stderr so far, without waiting for more."""
        got = b""
        while proc.stderr is not None and select.select([proc.stderr], [], [], 0)[0]:
            chunk = os.read(proc.stderr.fileno(), 65536)
            if not chunk:
                break
            got += chunk
        return got.decode("utf-8", "replace")

    def why(self, wrappers):
        """The message of a failed precondition: who holds a slot, which commands started, and for each wrapper its exit status (or still running) and its stderr, so a
        slow runner and a wrapper that ran its command WITHOUT a slot (`heavy: no slot (...)`) can be told apart."""
        lines = ["holders: %s" % self.hs.holders(self.dir / "fleet-switchboard", 3), "started: %s" % self.started()]   # the path as it is: never made here, so a refused one is shown as it is
        for tag, proc in wrappers.items():
            code = proc.poll()
            lines.append("wrapper %s (pid %d): %s; stderr: %r" % (tag, proc.pid, "exit %d" % code if code is not None else "still running", self.drain(proc)))
        return "\n".join(lines)

    def until(self, test, seconds=90):
        end = time.time() + seconds
        while time.time() < end:
            if test():
                return True
            time.sleep(0.05)
        return False

    def started(self):
        return sorted(p.name for p in self.home.glob("started.*"))

    READY_TRIES = 600    # the readiness helper: 600 reads of the holder record, 0.1 s apart (60 s: a bound for a hang, not a delay: a healthy holder is ready in milliseconds), unless a test names its own bound
    READY_PAUSE = 0.1
    START_DELAY = 0      # seconds a holder is made slow to start (the injected-delay class sets it)

    @staticmethod
    def holders_ready(found, count, name=None, wrapper_pids=()):
        """The readiness decision, from what `holders` listed: `count` holders, each with a real name and command (the record is written) and, for the real-process
        tests, a pid that is NOT its wrapper's. The wrapper writes the record TWICE: first its own pid, then (a moment later, after truncating the file first) the
        command's pid: the first record is not the holder's final one, and a read between the second truncate and the second write shows '?'. Ready is the final record."""
        known = [h for h in found if h["name"] != "?" and h["command"] != "?" and h["pid"] is not None and h["pid"] not in set(wrapper_pids)
                 and (name is None or h["name"] == name)]
        return len(found) == count and len(known) == count

    def ready_holders(self, count, slots=3, name=None, seconds=None, state=None, wrappers=()):
        """Wait until `count` holders are READY, then return them. Ready is read from the record the product itself reads (`holders`), never from a timer and never
        from the lock alone: the lock is taken first and the record written after, so a held lock is not ready. `wrappers` are the heavy wrapper processes started for
        the holders: a holder is ready only once its record shows the COMMAND's pid (see holders_ready). Bounded: READY_TRIES reads (or `seconds` / READY_PAUSE); when
        it runs out it fails and says which holder was expected and what was missing."""
        tries = int(seconds / self.READY_PAUSE) if seconds is not None else self.READY_TRIES
        state = state or self.state()
        pids = [w.pid for w in wrappers]
        found = []
        for attempt in range(max(1, tries)):
            found = self.hs.holders(state, slots)
            if self.holders_ready(found, count, name, pids):
                return found
            time.sleep(self.READY_PAUSE)
        self.fail("holder %r not ready after %d reads of its record (%.1f s): expected %d ready holder(s) with the command's pid (not the wrapper pids %s), found %s (a '?' is a held lock whose record is not written yet)" % (
            name, max(1, tries), max(1, tries) * self.READY_PAUSE, count, pids, found))

    def test_s9_a_wrapper_killed_with_sigkill_leaves_a_run_that_the_orphan_bound_stops_and_audits(self):
        """The 2 h 13 min case for real: the watchdog lives in the wrapper, the wrapper is killed, the run keeps the slot; the orphan bound (clock moved past the ceiling)
        stops the sleeping run and reports it with source orphan. A run whose wrapper is alive is not touched by the same check."""
        found = shutil.which("lsof", path=os.environ["PATH"] + os.pathsep + "/usr/sbin" + os.pathsep + "/usr/bin")
        if found is None:
            self.skipTest("lsof is needed to find what holds the slot")
        patch = mock.patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep + os.path.dirname(found)})   # a runner's PATH may lack /usr/sbin
        patch.start()
        self.addCleanup(patch.stop)
        wrapper = self.start("sleep", "300", name="orphan-lane")
        self.assertTrue(self.until(lambda: any(h["pid"] and h["pid"] != wrapper.pid and h["name"] != "?" for h in self.hs.holders(self.state(), 3)), 120), self.why({"w": wrapper}))
        held = self.hs.holders(self.state(), 3)[0]
        later = lambda: time.time() + 4000   # past kind "other" (3600 s)
        self.assertEqual(self.hs.check_orphans(self.state(), 3, None, later, None, io.StringIO()), [], "the wrapper is alive: its run is not an orphan")
        os.kill(wrapper.pid, signal.SIGKILL)
        wrapper.wait(timeout=30)
        self.assertEqual([h["pid"] for h in self.hs.holders(self.state(), 3)], [held["pid"]], "the run still holds the slot")
        told = []
        self.assertEqual(self.hs.check_orphans(self.state(), 3, None, lambda: time.time() + 100, lambda *a: told.append(a), io.StringIO()), [], "under the ceiling: left alone")
        stopped = self.hs.check_orphans(self.state(), 3, None, later, lambda *a: told.append(a), io.StringIO())
        self.assertEqual([(s["source"], s["kind"], s["pid"]) for s in stopped], [("orphan", "other", held["pid"])])
        self.assertEqual((told[0][2], told[0][3]), ("orphan-lane", os.environ.get("FLEET_HEAVY_NAME") or os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"))
        self.assertEqual([t[0] for t in told], ["decided", "told"])
        self.assertTrue(self.until(lambda: not self.hs.holders(self.state(), 3), 30), "the slot is free: the run is gone")

    def test_s1_four_callers_of_three_slots_queue_and_the_fourth_runs_when_one_frees(self):
        """The integration check with real processes (the queue logic itself is tested deterministically in TestHeavyQueueLogic)."""
        procs = {tag: self.start(*self.waiter(tag), name="who-%s" % tag) for tag in "abcd"[:3]}
        ok = self.until(lambda: len(self.started()) == 3, self.HOLD_CEILING_SECONDS)
        self.assertTrue(ok, "3 commands started\n" + (self.why(procs) if not ok else ""))
        # the three locks are really held now (the kernel's own answer, not a file): the fourth cannot take one, whatever the machine's speed
        try:
            self.ready_holders(3, seconds=self.HOLD_CEILING_SECONDS, wrappers=list(procs.values()))   # the three locks held AND their records written (the kernel's own answer, not a timer)
        except AssertionError as err:
            self.fail("3 of 3 slots held before the fourth starts\n%s\n%s" % (err, self.why(procs)))
        fourth = self.start(*self.waiter("d"), name="who-d")
        lines, queued = [], threading.Event()

        def pump():   # event driven: the wrapper's stderr is read as it arrives, so no poll interval or start-up time decides anything
            for line in fourth.stderr:
                lines.append(line)
                if "waiting for a slot" in line:
                    queued.set()
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        self.assertTrue(queued.wait(self.HOLD_CEILING_SECONDS), "the fourth wrapper (exit %r) never said it was waiting; holders: %s; started: %s" % (
            fourth.poll(), self.hs.holders(self.state(), 3), self.started()))
        self.assertEqual(len(self.started()), 3, "at most 3 run at once")
        (self.home / "release.a").write_text("")
        self.assertTrue(self.until(lambda: "started.d" in self.started()), "the fourth runs once a slot is free")
        for tag in "bcd":
            (self.home / ("release." + tag)).write_text("")
        for proc in list(procs.values()) + [fourth]:
            proc.wait(timeout=90)
        reader.join(timeout=90)
        err = "".join(lines)
        self.assertIn("heavy: waiting for a slot (3 of 3 busy", err)
        self.assertRegex(err, r"who-[abc] sh")

    def test_the_diagnosis_of_a_failed_precondition_shows_a_wrapper_that_ran_without_a_slot(self):
        """A wrapper whose state directory is refused runs its command with NO slot (`heavy: no slot (...)`): the command starts, no slot is held."""
        (self.dir / "fleet-switchboard").write_text("a file where the state directory should be")
        proc = self.start("sh", "-c", 'touch "%s/started.z"' % self.home, name="who-z")
        self.assertEqual(proc.wait(timeout=self.HOLD_CEILING_SECONDS), 0)
        message = self.why({"z": proc}).replace(str(self.dir), "<dir>")
        self.assertEqual(message.count("heavy: no slot"), 1, message)
        self.assertEqual(message.count("exit 0"), 1, message)
        self.assertEqual(message.count("holders: []"), 1, message)
        self.assertEqual(message.count("started: ['started.z']"), 1, message)

    def test_the_ceiling_of_the_integration_preconditions_is_300_seconds(self):
        self.assertEqual(self.HOLD_CEILING_SECONDS, 300)
        source = inspect.getsource(self.test_s1_four_callers_of_three_slots_queue_and_the_fourth_runs_when_one_frees)
        self.assertEqual(source.count("self.HOLD_CEILING_SECONDS"), 3, "both preconditions and the queued wait use it")

    def test_s2_a_holder_killed_with_its_whole_group_frees_its_slot_by_itself(self):
        holder = self.start(*self.waiter("h"), slots=1, name="doomed")
        self.assertTrue(self.until(lambda: "started.h" in self.started()))
        self.assertEqual([h["name"] for h in self.ready_holders(1, slots=1, name="doomed", wrappers=[holder])], ["doomed"])
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=90)
        waiter = self.start("sh", "-c", 'touch "%s/second"' % self.home, slots=1)
        self.assertEqual(waiter.wait(timeout=90), 0)
        self.assertTrue((self.home / "second").exists(), "no cleanup was needed")
        self.assertEqual(self.hs.holders(self.state(), 1), [])

    def test_s3_stdin_stdout_and_the_exit_status_pass_through_and_a_signal_reaches_the_command(self):
        proc = self.start("sh", "-c", "cat; exit 3", stdin=subprocess.PIPE)
        out, err = proc.communicate("some data\n", timeout=90)
        self.assertEqual((out, proc.returncode), ("some data\n", 3))
        pidfile = self.home / "pid"
        proc = self.start("sh", "-c", 'echo $$ > "%s"; exec sleep 60' % pidfile)
        self.assertTrue(self.until(pidfile.exists))
        child = int(pidfile.read_text())
        self.assertTrue(self.until(lambda: any(h["pid"] == child for h in self.hs.holders(self.state(), 3))), "the wrapper has recorded its command")
        proc.send_signal(signal.SIGTERM)   # to the wrapper alone: it forwards it
        self.assertEqual(proc.wait(timeout=90), 128 + signal.SIGTERM)
        self.assertTrue(self.until(lambda: not self.alive(child)), "the command was stopped and reaped: nothing is left behind")

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)   # a zombie of ours would still answer kill(0)
            return done == 0
        except ChildProcessError:
            return True

    def test_s3_the_wrapper_reaps_its_child_and_leaves_no_zombie(self):
        """In this very process (so the wrapper IS the parent): when `run` returns, its child has already been waited for, not left a zombie."""
        pidfile = self.home / "short-pid"
        err = io.StringIO()
        status = self.hs.run(["sh", "-c", 'echo $$ > "%s"' % pidfile], self.state(), 1, 10, name="inproc", environ=self.env(), out=err)
        self.assertEqual(status, 0, err.getvalue())
        pid = int(pidfile.read_text())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)   # nothing of ours is left to reap: a zombie would answer here

    @staticmethod
    def state_of(pid):
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return out or None

    def test_s4_the_command_runs_under_taskpolicy_when_it_exists_and_nice_when_it_does_not(self):
        fake = self.home / "fakebin"
        fake.mkdir()
        record = self.home / "record"
        (fake / "taskpolicy").write_text('#!/bin/sh\necho "$1" > "%s"\nshift\nexec "$@"\n' % record)
        os.chmod(str(fake / "taskpolicy"), 0o755)
        env = self.env(PATH="%s:%s" % (fake, os.environ["PATH"]))
        self.assertEqual(self.start("true", env=env).wait(timeout=90), 0)
        self.assertEqual(record.read_text().strip(), "-b", "run as taskpolicy -b <command>")
        self.assertEqual(self.hs.command_prefix(lambda n: "/x/taskpolicy" if n == "taskpolicy" else "/x/nice"), ["/x/taskpolicy", "-b"])
        self.assertEqual(self.hs.command_prefix(lambda n: "/x/nice" if n == "nice" else None), ["/x/nice", "-n", "10"])
        self.assertEqual(self.hs.command_prefix(lambda n: None), [])
        # and with no taskpolicy the real nice runs it at a lower priority
        stub = self.home / "nobin"
        stub.mkdir()
        for tool in ("nice", "sh", "ps"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, str(stub / tool))
        out = self.home / "niceness"
        proc = self.start("sh", "-c", 'ps -o nice= -p $$ > "%s"' % out, env=self.env(PATH=str(stub)))
        self.assertEqual(proc.wait(timeout=90), 0, proc.stderr.read())
        self.assertEqual(int(out.read_text().strip()), min(20, os.nice(0) + 10), "ten above this process's own niceness")

    def test_s5_the_holder_list_names_who_holds_a_slot_and_a_free_slot_says_nothing(self):
        proc = self.start("sleep", "5", slots=2, name="the-coder")
        (entry,) = self.ready_holders(1, slots=2, name="the-coder", wrappers=[proc])
        self.assertEqual((entry["name"], entry["command"]), ("the-coder", "sleep"))
        self.assertIsInstance(entry["pid"], int)
        self.assertLessEqual(abs(entry["since"] - time.time()), 30)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=90)
        self.assertTrue(self.until(lambda: self.hs.holders(self.state(), 2) == []))
        mode = os.stat(str(self.state() / "heavy-slot-0.lock")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_s6_past_the_wait_limit_it_exits_75_and_does_not_run_the_command(self):
        holder = self.start("sleep", "30", slots=1, name="busy-one")
        self.ready_holders(1, slots=1, name="busy-one", wrappers=[holder])
        marker = self.home / "marker"
        late = self.start("touch", str(marker), slots=1, wait_max=1)
        self.assertEqual(late.wait(timeout=90), 75)
        self.assertIn("heavy: no slot after", late.stderr.read())
        self.assertFalse(marker.exists())

    def test_s7_the_suite_takes_a_slot_unless_it_is_off_inside_one_or_in_ci(self):
        self.assertEqual(self.hs.should_take_slot({}), "taken")
        self.assertEqual(self.hs.should_take_slot({"FLEET_HEAVY": "off"}), "off")
        self.assertEqual(self.hs.should_take_slot({"CI": "true"}), "off")
        self.assertEqual(self.hs.should_take_slot({"FLEET_HEAVY_SLOT": "1"}), "inside")
        env = {"XDG_STATE_HOME": str(self.dir)}
        wrapped = self.hs.wrap_argv(["TestX"], env, "/py", "/sw", "/dir/test-switchboard")
        self.assertEqual(wrapped, ["/py", "/sw", "heavy", "--name", "test-switchboard", "--", "/py", "/dir/test-switchboard", "TestX"])
        self.assertIsNone(self.hs.wrap_argv([], dict(env, FLEET_HEAVY="off"), "/py", "/sw", "/s"))
        self.assertIsNone(self.hs.wrap_argv([], dict(env, FLEET_HEAVY_SLOT="0"), "/py", "/sw", "/s"))

    def test_s7_a_state_directory_that_cannot_be_written_runs_the_suite_unslotted_with_one_line(self):
        blocked = self.home / "blocked"
        blocked.write_text("a file, so no directory can be made under it")
        err = io.StringIO()
        self.assertIsNone(self.hs.wrap_argv([], {"XDG_STATE_HOME": str(blocked)}, "/py", "/sw", "/s", err=err))
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("running without a slot", err.getvalue())

    def test_u1_a_wrapper_killed_alone_leaves_the_slot_busy_exactly_while_its_command_runs(self):
        pidfile = self.home / "orphan-pid"
        wrapper = self.start("sh", "-c", 'echo $$ > "%s"; while [ ! -e "%s/orphan-release" ]; do sleep 0.05; done' % (pidfile, self.home), slots=1)
        self.assertTrue(self.until(pidfile.exists))
        self.assertTrue(self.until(lambda: any(h["pid"] == int(pidfile.read_text()) for h in self.hs.holders(self.state(), 1))))
        os.kill(wrapper.pid, signal.SIGKILL)   # the wrapper only: the command goes on
        wrapper.wait(timeout=90)
        self.assertEqual(len(self.hs.holders(self.state(), 1)), 1, "busy while the command runs")
        (self.home / "orphan-release").write_text("")
        self.assertTrue(self.until(lambda: self.hs.holders(self.state(), 1) == [], 90), "free once it ends")

    def test_u2_the_command_is_not_run_through_a_shell_and_keeps_its_environment_and_directory(self):
        work = self.home / "cwd"
        work.mkdir()
        proc = self.start(sys.executable, "-c", "import sys, os; print(sys.argv[1:], os.environ['X_MARK'], os.getcwd())", "a b;c", "$HOME",
                          env=self.env(X_MARK="kept"), cwd=str(work))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip(), "['a b;c', '$HOME'] kept %s" % os.path.realpath(str(work)))

    def test_a_signal_while_waiting_a_zero_wait_limit_and_a_command_that_cannot_start(self):
        holder = self.start("sleep", "30", slots=1)
        self.ready_holders(1, slots=1, name="t", wrappers=[holder])
        waiting = self.start("true", slots=1)
        seen = ""
        end = time.time() + 90
        while "waiting for a slot" not in seen and time.time() < end:   # the wrapper says so once it is queued
            if select.select([waiting.stderr], [], [], 0.2)[0]:
                seen += os.read(waiting.stderr.fileno(), 4096).decode()
        self.assertIn("waiting for a slot", seen)
        waiting.send_signal(signal.SIGTERM)
        self.assertEqual(waiting.wait(timeout=90), 128 + signal.SIGTERM, "a wrapper that is still waiting just ends")
        zero = self.start("true", slots=1, wait_max=0)
        self.assertEqual(zero.wait(timeout=90), 75)
        not_executable = self.home / "notexec"
        not_executable.write_text("#!/bin/sh\n")   # present, not executable
        self.assertEqual(self.start(str(not_executable), slots=2).wait(timeout=90), 127)

    def test_what_is_shown_about_a_holder_never_carries_a_terminal_escape(self):
        state = self.state()
        (state / "heavy-slot-0.lock").write_text(json.dumps({"name": "\x1b[2Jevil\x9b31m", "command": "a\nb", "pid": 1, "since": 1}))
        fd = os.open(str(state / "heavy-slot-0.lock"), os.O_RDWR)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            (entry,) = self.hs.holders(state, 1)
        finally:
            os.close(fd)
        shown = entry["name"] + entry["command"]
        self.assertEqual([c for c in shown if ord(c) < 32 or 0x7f <= ord(c) <= 0x9f], [])
        self.assertIn("evil", shown)
        self.assertEqual(self.hs.holder_name({"FLEET_HEAVY_NAME": "x\x1b[0my"}), "x [0my")

    def test_a_bad_report_interval_falls_back_to_the_default(self):
        out = self.start("true", env=self.env(FLEET_HEAVY_REPORT_SECONDS="soon"))
        self.assertEqual(out.wait(timeout=90), 0)

    def test_a_missing_or_unwritable_state_directory_runs_the_command_without_a_slot_with_one_line(self):
        marker = self.home / "ran"
        blocked = self.home / "blocked"
        blocked.write_text("a file: no directory can be made under it")
        proc = self.start("sh", "-c", 'touch "%s"' % marker, env=self.env(XDG_STATE_HOME=str(blocked)))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(marker.exists(), "the command ran")
        self.assertEqual(err.count("heavy: no slot ("), 1, err)
        self.assertNotIn("Traceback", err)
        marker.unlink()
        readonly = self.home / "readonly"
        readonly.mkdir()
        os.chmod(str(readonly), 0o500)
        self.addCleanup(os.chmod, str(readonly), 0o700)
        proc = self.start("sh", "-c", 'touch "%s"' % marker, env=self.env(XDG_STATE_HOME=str(readonly)))
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(marker.exists())
        self.assertNotIn("Traceback", err)
        # and in process: a directory that does not exist at all
        errors = io.StringIO()
        self.assertEqual(self.hs.run(["true"], str(self.home / "no" / "such" / "dir"), 1, 5, environ=self.env(), out=errors), 0)
        self.assertEqual(errors.getvalue().count("heavy: no slot ("), 1)

    def test_nested_heavy_inside_a_slot_takes_no_second_slot_so_four_levels_of_three_slots_finish(self):
        inner = ['"%s"' % sys.executable, '"%s"' % SWITCHBOARD, "heavy", "--slots", "3", "--wait-max", "20", "--"]
        script = "echo level-4"
        for level in (3, 2, 1):
            script = " ".join(inner) + " sh -c '%s'" % script.replace("'", "'\\''")
        proc = self.start("sh", "-c", script, slots=3, wait_max=20)
        out, err = proc.communicate(timeout=90)
        self.assertEqual((proc.returncode, out.strip()), (0, "level-4"), err)
        self.assertEqual(err.count("already inside a slot"), 3)
        self.assertNotIn("no slot after", err)

    def test_heavy_off_runs_the_command_directly_with_no_slot_and_no_line(self):
        out = self.home / "off-out"
        proc = self.start("sh", "-c", 'echo "$FLEET_HEAVY_SLOT" > "%s"' % out, env=self.env(FLEET_HEAVY="off"))
        stdout, err = proc.communicate(timeout=90)
        self.assertEqual((proc.returncode, err), (0, ""))
        self.assertEqual(out.read_text().strip(), "", "no slot variable: it ran as it is")
        self.assertFalse(list(self.state().glob("heavy-slot-*.lock")), "no slot file was even made")

    def test_the_cpu_line_shows_the_slots_in_use_through_the_real_heavy_module(self):
        cg = sb.load_cpu_guard()
        self.assertEqual(cg.slots_text({}, self.state()), "heavy slots 0/3")
        holder = self.start("sleep", "30", name="the-coder")
        self.ready_holders(1, name="the-coder", wrappers=[holder])
        line = cg.slots_text({}, self.state())
        self.assertRegex(line, r"^heavy slots 1/3 \(the-coder sleep \d+[ms]\)$")
        self.assertEqual(cg.slots_text({"heavy": {"slots": 5}}, self.state())[:15], "heavy slots 1/5")
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=90)
        self.assertTrue(self.until(lambda: cg.slots_text({}, self.state()) == "heavy slots 0/3"), "a killed holder is not shown")

    def test_a_missing_command_and_misuse_are_said_plainly(self):
        self.assertEqual(self.start("no-such-command-here").wait(timeout=90), 127)
        for words in ([], ["--slots", "0", "--", "true"], ["--slots", "x", "--", "true"], ["--wait-max", "-1", "--", "true"]):
            proc = subprocess.Popen([sys.executable, str(SWITCHBOARD), "heavy"] + words, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.communicate(timeout=90)
            self.assertEqual(proc.returncode, sb.EXIT_USAGE, words)

    def test_the_config_key_is_validated(self):
        for ok in (None, {}, {"slots": 1}, {"slots": 8}):
            self.hs.settings(ok)
        for bad in (3, {"slots": 0}, {"slots": "3"}, {"slots": True}, {"slots": 100}, {"cores": 2}):
            with self.assertRaises(self.hs.HeavyError):
                self.hs.settings(bad)
        self.assertEqual(self.hs.settings(None), 3)
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "heavy": {"slots": 0}})

    def test_the_documents_name_the_command_and_the_agent_definitions_say_it_once(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        self.assertIn("fleet-switchboard heavy", text)
        line = "full suites and browser/e2e runs go through `fleet-switchboard heavy -- <cmd>`"
        for path in ("agents/opencode/coder.md", "agents/opencode/orchestrator.md", "skills/fleet-coordination/SKILL.md"):
            found = (REPO / path).read_text()
            self.assertEqual(found.lower().count(line), 1, path)


class TestHeavySlotsSlowStart(TestHeavySlots):
    """#688: the three tests that failed in CI, run again with every holder one second slow to write its record (its lock is held at once)."""
    START_DELAY = 1


class TestWeeklyJobRewrite(Base):
    """Ask #958: the installer drops the janitor from a weekly job installed from the old documentation snippet, and only from that."""
    PROG = "/home/user/.local/bin/fleet-switchboard"
    OLD = "%s disk prune; %s disk janitor --apply --weekly" % (PROG, PROG)

    def setUp(self):
        super().setUp()
        self.agents = Path.home() / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True, exist_ok=True)
        self.assertTrue(str(self.agents).startswith(str(self.home)), "the test must never touch a real LaunchAgents")

    def plist(self, command, name="com.example.fleet-disk-prune.plist", extra=""):
        text = ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n  <key>Label</key><string>com.example.fleet-disk-prune</string>\n'
                '  <key>ProgramArguments</key>\n  <array><string>/bin/sh</string><string>-c</string><string>%s</string></array>\n%s</dict>\n</plist>\n' % (command, extra))
        (self.agents / name).write_text(text)
        return self.agents / name, text

    def step(self, platform="darwin", dry=False):
        out = io.StringIO()
        sb.Installer(subprocess.run, subprocess.Popen, lambda: 1700000000.0, platform, REPO, out=out, dry_run=dry).step_janitor_job()
        return out.getvalue()

    def backups(self):
        return sorted(p.name for p in self.agents.iterdir() if ".bak-" in p.name)

    def test_958_an_exact_match_is_backed_up_and_rewritten_and_the_prune_part_is_byte_identical(self):
        path, text = self.plist(self.OLD)
        out = self.step()
        self.assertIn("done", out)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual((self.agents / self.backups()[0]).read_text(), text)
        self.assertEqual(path.read_text(), text.replace(self.OLD, "%s disk prune" % self.PROG))
        self.assertNotIn("disk janitor", path.read_text())
        self.assertIn("%s disk prune" % self.PROG, path.read_text())   # the Docker prune command, unchanged
        self.assertIn("already done", self.step())   # a second run changes nothing
        self.assertEqual(len(self.backups()), 1)

    def test_958_a_job_that_differs_by_one_character_is_reported_and_left_byte_identical(self):
        for command in (self.OLD + " ", self.OLD.replace("--weekly", "--weekl"), self.OLD.replace("; ", ";  "),
                        "%s disk prune; /other/fleet-switchboard disk janitor --apply --weekly" % self.PROG,
                        self.OLD + "; echo hi"):
            for old in self.agents.iterdir():
                old.unlink()
            path, text = self.plist(command)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            out = self.step()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, command)
            self.assertEqual(self.backups(), [], command)
            self.assertIn("nothing was changed", out)

    def test_958_no_job_changes_nothing_and_a_dry_run_writes_nothing(self):
        self.assertIn("already done", self.step())
        self.assertEqual(list(self.agents.iterdir()), [])
        path, text = self.plist(self.OLD)
        self.assertIn("would be done", self.step(dry=True))
        self.assertEqual(path.read_text(), text)
        self.assertEqual(self.backups(), [])
        self.assertIn("skipped", self.step(platform="linux"))

    def test_958_s11_unreadable_symlink_none_and_a_second_run_change_nothing_and_the_first_backup_survives(self):
        original = self.plist(self.OLD, "real.plist")[1]
        os.symlink(str(self.agents / "real.plist"), str(self.agents / "link.plist"))
        (self.agents / "real.plist").rename(self.agents / "real.txt")
        before = {p.name: p.read_bytes() for p in self.agents.iterdir() if not p.is_symlink()}
        locked, locked_text = self.plist(self.OLD, "locked.plist")
        os.chmod(str(locked), 0)
        try:
            self.step()
        finally:
            os.chmod(str(locked), 0o644)
        self.assertEqual(self.backups(), [])
        self.assertEqual(locked.read_text(), locked_text, "an unreadable file is left as it is")
        self.assertEqual({p.name: p.read_bytes() for p in self.agents.iterdir() if not p.is_symlink() and p.name != "locked.plist"}, before)
        for p in list(self.agents.iterdir()):
            p.unlink()
        path, text = self.plist(self.OLD)
        self.step()
        first = (self.agents / self.backups()[0]).read_bytes()
        self.step()
        self.step()
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual((self.agents / self.backups()[0]).read_bytes(), first)
        self.assertEqual(first, text.encode())

    def test_958_backup_collision_and_a_doubled_command_leave_the_file_alone(self):
        path, text = self.plist(self.OLD)
        taken = path.with_name(path.name + ".bak-1700000000")
        taken.write_text("earlier backup")
        out = self.step()
        self.assertIn("exists", out)
        self.assertEqual(path.read_text(), text)
        self.assertEqual(taken.read_text(), "earlier backup")
        taken.unlink()
        twice = self.plist(self.OLD, "twice.plist", extra="  <key>Note</key><string>%s</string>\n" % self.OLD)
        out = self.step()
        self.assertEqual(twice[0].read_text(), twice[1])
        self.assertIn("nothing was changed", out)

    def test_958_each_part_of_the_exactness_rule_is_pinned_by_its_own_row(self):
        """The rule has four parts: the program list's shape, the shell, the command's equality and the single occurrence. One row for each, and a plist plus
        the old command in a second key for the count."""
        prog = self.PROG
        rows = {
            "shell is zsh": ("/bin/zsh", "-c", self.OLD, None),
            "shell is bash": ("/bin/bash", "-c", self.OLD, None),
            "flag is -lc": ("/bin/sh", "-lc", self.OLD, None),
            "flag is -e": ("/bin/sh", "-e", self.OLD, None),
            "list has a fourth word": ("/bin/sh", "-c", self.OLD, "<string>extra</string>"),
            "janitor flag differs": ("/bin/sh", "-c", self.OLD.replace("--apply", "--apply "), None),
            "second program differs": ("/bin/sh", "-c", "%s disk prune; /elsewhere/fleet-switchboard disk janitor --apply --weekly" % prog, None),
            "text after the command": ("/bin/sh", "-c", self.OLD + "; true", None),
        }
        for label, (shell, flag, command, more) in rows.items():
            for old in self.agents.iterdir():
                old.unlink()
            path = self.agents / "job.plist"
            path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n  <key>ProgramArguments</key>\n  <array><string>%s</string><string>%s</string>'
                            '<string>%s</string>%s</array>\n</dict>\n</plist>\n' % (shell, flag, command, more or ""))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            out = self.step()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, label)
            self.assertEqual(self.backups(), [], label)
            self.assertIn("nothing was changed", out, label)
        for old in self.agents.iterdir():
            old.unlink()
        path, text = self.plist(self.OLD, extra="  <key>Note</key><string>%s</string>\n" % self.OLD)   # the single occurrence
        self.step()
        self.assertEqual(path.read_text(), text)
        self.assertEqual(self.backups(), [])

    def test_958_a_symlinked_job_is_never_rewritten_through_the_link(self):
        real = self.agents.parent / "real-job.plist"
        real.write_text(self.plist(self.OLD, "tmp.plist")[1])
        (self.agents / "tmp.plist").unlink()
        os.symlink(str(real), str(self.agents / "job.plist"))
        before = real.read_bytes()
        try:
            self.step()
        except sb.InstallError:
            pass   # the write check refuses the link: the install stops rather than writing through it
        self.assertEqual(real.read_bytes(), before)
        self.assertEqual([n for n in self.backups()], [])

    def test_958_s7_the_documentation_snippet_is_what_the_installer_produces(self):
        docs = (Path(sb.__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        start = docs.index("<?xml version", docs.index("com.example.fleet-disk-prune.plist"))
        snippet = docs[start:docs.index("```", start)]
        self.assertIn("%s disk prune</string>" % self.PROG, snippet)
        old = snippet.replace("%s disk prune</string>" % self.PROG, "%s</string>" % self.OLD)
        (self.agents / "x.plist").write_text(old)
        self.step()
        self.assertEqual((self.agents / "x.plist").read_text(), snippet)
        self.assertIn("`--i-reviewed", docs)

    def test_958_every_old_snippet_command_the_docs_ever_showed_has_no_janitor_apply_now(self):
        docs = (Path(sb.__file__).resolve().parent.parent / "docs" / "switchboard.md").read_text()
        snippet = docs[docs.index("<key>Label</key><string>com.example.fleet-disk-prune</string>"):]
        snippet = snippet[:snippet.index("</plist>")]
        self.assertEqual(snippet.count("disk janitor --apply"), 0)
        self.assertIn("--i-reviewed", docs)


class TestSlice3Modules(Base):
    """#437 slice 3: the loaders come from one generator, three more modules declare their own key, and two modules added on two branches never touch a shared line."""

    OLD = [   # what validate_config said before the move (recorded from the previous program): the error text is the contract
        ("temp_janitor", {"days": 0}, "config: temp_janitor.days must be a whole number from 1 to 365, got 0"),
        ("temp_janitor", "x", "config: 'temp_janitor' must be an object with only enabled, days, keep, dir, scan_seconds, weekly_scan_seconds and daemon_apply, got 'x'"),
        ("chat_clear", {"live": "y"}, 'config: \'chat_clear\' must be {"enabled": true|false, "live": true|false}, got {\'live\': \'y\'}'),
        ("chat_clear", [1], 'config: \'chat_clear\' must be {"enabled": true|false, "live": true|false}, got [1]'),
        ("impact_floor", {"enabled": "y"}, "config: 'impact_floor.enabled' must be true or false"),
        ("impact_floor", "x", "config: 'impact_floor' must be an object"),
        ("impact_floor", {"zzz": 1}, "config: 'impact_floor' has key(s) this tool does not read: zzz"),
    ]
    GOOD = {"temp_janitor": [None, {"days": 3}], "chat_clear": [None, {"live": True}], "impact_floor": [None, {"enabled": True}]}

    def test_s1_every_loader_is_generated_and_returns_the_module_load_module_returns(self):
        tree = ast.parse(switchboard_source())
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertEqual(len(sb.HELPER_MODULES), 23)
        self.assertEqual(defined & set(sb.HELPER_MODULES), set(), "0 of the loaders has a def of its own")
        for function, module in sb.HELPER_MODULES.items():
            with self.subTest(function=function):
                loader = getattr(sb, function)
                self.assertIs(loader(), sb.load_module(module))
                self.assertIs(loader(), loader(), "the second call returns the identical object")
                self.assertEqual(loader.__name__, function)

    def test_s2_the_moved_keys_give_the_old_results_and_the_old_error_text(self):
        for key, values in self.GOOD.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    sb.validate_config({key: value})
        sb.validate_config({})
        for key, value, text in self.OLD:
            with self.subTest(key=key, value=value), self.assertRaises(sb.ConfigError) as caught:
                sb.validate_config({key: value})
            self.assertEqual(str(caught.exception), text)

    def test_s3_the_three_keys_are_declared_by_their_modules_and_in_no_shared_place(self):
        declared = sb.config_declarations()
        for key, module in (("temp_janitor", "disk_janitor"), ("chat_clear", "chat_clear"), ("impact_floor", "impact_floor")):
            with self.subTest(key=key):
                self.assertEqual(declared[key], module)
                self.assertNotIn(key, sb.TOP_LEVEL_EXTRA_KEYS)
                self.assertIn(key, sb.config_known_keys())
        source = inspect.getsource(sb.validate_config)
        for text in ('"temp_janitor"', '"chat_clear"', '"impact_floor"'):
            self.assertNotIn(text, source, "no inline block for it is left in the validation chain")

    def make_program(self, extra):
        """A copy of the real program and its modules in a scratch directory, with `extra` files added to its bin."""
        directory = Path(tempfile.mkdtemp(prefix="slice3-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for path in BIN.glob("*.py"):
            shutil.copy(str(path), str(directory / path.name))
        copy_entry(directory)
        for name, text in extra.items():
            (directory / name).write_text(text)
        return directory

    def load_copy(self, directory):
        loader = importlib.machinery.SourceFileLoader("sb_copy_%d" % id(directory), str(directory / "fleet-switchboard"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    def test_s4_a_fixture_module_with_a_key_needs_no_change_to_the_program(self):
        fixture = 'CONFIG_KEY = "slice3_key"\nCONFIG_DEFAULT = {"n": 2}\n\n\ndef validate(raw, source):\n    if not isinstance(raw, dict):\n        raise ValueError("%s: slice3 bad" % source)\n\n\ndef hello():\n    return "hi"\n'
        directory = self.make_program({"slice3_mod.py": fixture})
        before = (directory / "fleet-switchboard").read_bytes()
        copy = self.load_copy(directory)
        self.assertEqual(copy.load_module("slice3_mod").hello(), "hi", "found by name from its own file")
        self.assertEqual(copy.validate_config({})["slice3_key"], {"n": 2})
        with self.assertRaises(copy.ConfigError) as caught:
            copy.validate_config({"slice3_key": 5})
        self.assertEqual(str(caught.exception), "config: slice3 bad")
        self.assertEqual((directory / "fleet-switchboard").read_bytes(), before)

    def test_s5_two_branches_that_each_add_a_module_merge_without_a_conflict_and_both_keys_work_after_the_merge(self):
        directory = Path(tempfile.mkdtemp(prefix="slice3-merge-"))
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "bin").mkdir()
        for path in BIN.glob("*.py"):
            shutil.copy(str(path), str(directory / "bin" / path.name))
        copy_entry(directory / "bin")

        def git(*args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"] + list(args), cwd=str(directory),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

        self.assertEqual(git("init", "-q", "-b", "main").returncode, 0)
        git("add", "-A"), git("commit", "-q", "-m", "base")
        for branch, key in (("one", "fixture_one"), ("two", "fixture_two")):
            git("checkout", "-q", "-b", branch, "main")
            (directory / "bin" / ("%s.py" % key)).write_text('CONFIG_KEY = "%s"\nCONFIG_DEFAULT = {"n": 1}\n\n\ndef validate(raw, source):\n    if raw is not None and not isinstance(raw, dict):\n        raise ValueError("%s: bad")\n' % (key, key))
            git("add", "-A"), git("commit", "-q", "-m", branch)
            self.assertEqual(git("diff", "--name-only", "main", branch).stdout.split(), ["bin/%s.py" % key], "0 lines of the program changed")
        git("checkout", "-q", "one")
        merged = git("merge", "--no-edit", "two")
        self.assertEqual(merged.returncode, 0, merged.stdout)
        loader = importlib.machinery.SourceFileLoader("sb_merged_%d" % id(directory), str(directory / "bin" / "fleet-switchboard"))
        program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(program)
        seen = []
        program.warn_unknown_config = lambda message, section="", names=(), known=(): seen.append(list(names))
        config = program.validate_config({})
        self.assertEqual((config["fixture_one"], config["fixture_two"]), ({"n": 1}, {"n": 1}), "both modules are found and defaulted after the merge, with no line in the program")
        self.assertEqual(seen, [])
        with self.assertRaises(program.ConfigError):
            program.validate_config({"fixture_two": "bad"})
        # the test fails when the behaviour breaks: a program that kept a shared key table (no declaration scan) does not know the keys
        program.config_declarations = lambda: {}
        program.validate_config({"fixture_one": {"n": 2}, "fixture_two": {"n": 3}})
        self.assertEqual(seen, [["fixture_one", "fixture_two"]], "with the declarations removed the keys are unknown: this test would fail")

    def test_a_missing_module_file_refuses_the_configuration_for_each_of_the_three_moved_keys_with_the_floor_message(self):
        for key, module, good in (("temp_janitor", "disk_janitor", {"days": 3}), ("chat_clear", "chat_clear", {"live": True}), ("impact_floor", "impact_floor", {"enabled": True})):
            with self.subTest(key=key):
                directory = Path(tempfile.mkdtemp(prefix="slice3-missing-"))
                self.addCleanup(shutil.rmtree, directory, True)
                for path in BIN.glob("*.py"):
                    if path.stem != module:   # the module file is gone
                        shutil.copy(str(path), str(directory / path.name))
                with mock.patch.object(sb, "CONFIG_DECLARATIONS_DIR", str(directory)), self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config({key: good})
                self.assertEqual(str(caught.exception), "config key %r must be declared by %s.py, and it is not (the module is missing, its CONFIG_KEY line is missing or damaged, "
                                 "or it names another key): no configuration is loaded" % (key, module))

    def test_when_two_keys_are_wrong_the_declared_modules_win_in_module_name_order_and_the_earlier_checks_win_over_them(self):
        bad = {"temp_janitor": "x", "chat_clear": "x", "impact_floor": "x"}
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict(bad))
        self.assertIn("'chat_clear'", str(caught.exception), "chat_clear.py sorts before disk_janitor.py and impact_floor.py")
        del bad["chat_clear"]
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict(bad))
        self.assertIn("temp_janitor", str(caught.exception), "disk_janitor.py sorts before impact_floor.py")
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"chat_clear": "x", "cpu_guard": "x"})
        self.assertIn("cpu_guard", str(caught.exception), "the checks that run before the declared ones still win")
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"chat_clear": "x", "docker_reaper": "x"})
        self.assertIn("chat_clear", str(caught.exception), "a declared module's check wins over the ones that follow it")

    def test_s6_a_default_owned_key_and_a_key_with_its_validator_in_the_program_stay_where_they_were(self):
        self.assertTrue({"reds", "heavy", "cpu_guard"} <= set(sb.DEFAULTS))
        self.assertTrue({"sweep", "triage", "plan_review", "impact_hold"} <= sb.TOP_LEVEL_EXTRA_KEYS)
        for key, value in (("sweep", "x"), ("impact_hold", "x"), ("plan_review", "x")):
            with self.subTest(key=key), self.assertRaises(sb.ConfigError):
                sb.validate_config({key: value})

    def test_s7_a_module_name_that_is_a_path_or_missing_is_refused(self):
        for name in ("../fleet_health", "/etc/passwd", "no_such_module_xyz", "a/b", ""):
            with self.subTest(name=name), self.assertRaises((ValueError, ImportError, OSError)):
                sb.load_module(name)

    def test_u2_a_missing_or_broken_module_fails_with_an_error_and_is_not_kept(self):
        directory = self.make_program({"broken_mod.py": "def (:\n"})
        copy = self.load_copy(directory)
        with self.assertRaises(SyntaxError):
            copy.load_module("broken_mod")
        with self.assertRaises(SyntaxError):
            copy.load_module("broken_mod")   # a failed import is never kept: it fails again
        with self.assertRaises(FileNotFoundError):
            copy.load_module("absent_mod")
        self.assertNotIn("broken_mod", sys.modules)


class TestModuleLoader(Base):
    """Ask #437 slice 1: one generic loader behind the old `load_<module>()` names."""

    OLD = {
        "load_decision_core": "decision_core", "load_plan_check": "plan_check", "load_plan_ledger": "plan_ledger",
        "load_report_fold": "report_fold", "load_triage_plan_open": "triage_plan_open", "load_pr_events": "pr_events",
        "load_disk_janitor": "disk_janitor", "load_heavy_slots": "heavy_slots", "load_cpu_guard": "cpu_guard",
        "load_health_records": "health_records", "load_reds": "reds", "load_impact_floor": "impact_floor",
        "load_plan_review": "plan_review", "load_external_adapter": "external_adapter",
        "load_decision_sync": "decision_sync", "load_work_sync": "work_sync", "load_agent_calls": "agent_calls",
        "load_triage_digest": "triage_digest", "load_dead_hint": "dead_hint", "load_fleet_health": "fleet_health",
    }

    def _copy(self, extra=None):
        """A copy of the program in a temp bin directory, with the named modules beside it, loaded as a module."""
        tmp = Path(tempfile.mkdtemp(prefix="ldtest"))
        self.addCleanup(shutil.rmtree, tmp, True)
        held = dict(sys.modules)
        self.addCleanup(lambda: (sys.modules.clear(), sys.modules.update(held)))
        copy_entry(tmp)
        for module in BIN.glob("*.py"):
            shutil.copy(module, tmp / module.name)
        for name, text in (extra or {}).items():
            (tmp / (name + ".py")).write_text(text)
        return tmp, _load("ld_" + tmp.name, tmp / "fleet-switchboard")

    def test_s1_each_old_loader_loads_its_old_path_and_caches(self):
        self.assertEqual(len(self.OLD), 20)
        for fn, name in self.OLD.items():
            first = getattr(sb, fn)()
            self.assertEqual(os.path.realpath(first.__file__), os.path.realpath(str(BIN / (name + ".py"))), fn)
            self.assertIs(getattr(sb, fn)(), first, fn)

    def test_s2_a_missing_or_broken_module_fails_as_before(self):
        tmp, mod = self._copy({"broken_one": "def (:\n"})
        with self.assertRaises(FileNotFoundError):
            mod.load_module("absent_one")
        with self.assertRaises(SyntaxError):
            mod.load_module("broken_one")
        self.assertNotIn("broken_one", sys.modules)

    def test_s4_a_new_module_loads_with_no_edit_to_the_program(self):
        tmp, mod = self._copy({"fixture_mod": "VALUE = 7\n"})
        first = mod.load_module("fixture_mod")
        self.assertEqual(first.VALUE, 7)
        self.assertIs(mod.load_module("fixture_mod"), first)

    def test_s9_concurrent_first_loads_all_get_the_finished_module_and_a_failing_one_fails_for_every_thread(self):
        tmp, mod = self._copy({
            "slow_mod": "import time\ntime.sleep(0.3)\nREADY = True\n",
            "failing_mod": "import time\ntime.sleep(0.2)\nraise RuntimeError('boom')\n"})
        got, errors, barrier = [], [], threading.Barrier(8)

        def worker(name):
            barrier.wait()
            try:
                module = mod.load_module(name)
                got.append((module, getattr(module, "READY", False)))
            except RuntimeError as err:
                errors.append(str(err))
        threads = [threading.Thread(target=worker, args=("slow_mod",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(len(got), 8)
        self.assertEqual(len({id(module) for module, _ in got}), 1, "one object for every thread")
        self.assertTrue(all(ready for _, ready in got), "no thread saw a half-loaded module")
        barrier, errors = threading.Barrier(8), []
        threads = [threading.Thread(target=worker, args=("failing_mod",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, ["boom"] * 8, "the failing module raises for every thread")
        self.assertNotIn("failing_mod", sys.modules)

    def test_s9b_a_slow_import_never_delays_another_module_and_a_module_may_load_another(self):
        tmp, mod = self._copy({
            "slow_one": "import threading\nthreading._ld_started.set()\nthreading._ld_gate.wait(20)\nREADY = True\n",
            "quick_one": "VALUE = 1\n", "fresh_one": "VALUE = 2\n",
            "b_mod": "VALUE = 'b'\n",
            "a_mod": "import threading\nB = threading._ld_load('b_mod')\n"})
        threading._ld_started, threading._ld_gate, threading._ld_load = threading.Event(), threading.Event(), mod.load_module
        self.addCleanup(lambda: [delattr(threading, n) for n in ("_ld_started", "_ld_gate", "_ld_load") if hasattr(threading, n)])
        self.addCleanup(threading._ld_gate.set)
        quick = mod.load_module("quick_one")
        slow = threading.Thread(target=lambda: mod.load_module("slow_one"))
        slow.start()
        self.assertTrue(threading._ld_started.wait(10))
        self.assertIs(mod.load_module("quick_one"), quick, "an already loaded module needs no lock")
        self.assertEqual(mod.load_module("fresh_one").VALUE, 2, "a first load of another module does not wait for the slow import")
        self.assertTrue(slow.is_alive(), "both returned while the slow import was still running")
        threading._ld_gate.set()
        slow.join(20)
        self.assertEqual(mod.load_module("a_mod").B.VALUE, "b", "a module that loads another while it runs")

    def test_s8_a_hostile_name_is_refused(self):
        tmp, mod = self._copy()
        (tmp.parent / "outside_mod.py").write_text("VALUE = 1\n")
        self.addCleanup((tmp.parent / "outside_mod.py").unlink)
        for name in ("../outside_mod", os.path.join(os.sep, "etc", "hosts"), "a/b", "", None):
            with self.assertRaises(ValueError, msg=repr(name)):
                mod.load_module(name)
        with self.assertRaises(FileNotFoundError):
            mod.load_module("no_such_module")
        self.assertNotIn("outside_mod", sys.modules)


class TestDeployFromMainSentence(unittest.TestCase):
    SENTENCE = ("Production is deployed from the head of main only: never from a side branch, a cherry-pick or a revert off main; "
                "when main is not shippable, the fix goes into main (the owner's standing order).")

    def test_s2_the_coordination_skill_states_the_deploy_rule_once(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertEqual(text.count(self.SENTENCE), 1)

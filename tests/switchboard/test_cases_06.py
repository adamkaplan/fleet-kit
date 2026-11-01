"""Test classes moved out of bin/test-switchboard (text unchanged): cases_06."""
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
from support.core import Base, FakeRunner
from support.installs_and_keys import ACME_API, ACME_WEB, CloudReaperInstall, DECISIONS_PLUGIN, InstallCase, RealStatusAfterInstall, WrittenFilesAreGuarded
from support.loader import SWITCHBOARD, class_source, sb, sc, switchboard_source
from support.paths import REPO
from support.policy_and_import import FAKE_V1, V1_MAIN, V1_MAIN_ID
from support.scenario_roles import JEV_REPLAY
from support.wire_fakes import FAKE_OPENCODE, SES, herdr_typing_violations


class ClosingRunner:
    """herdr's runner that closes a pane the moment the engine reads it with
    `agent get`: the pane closes between the decision and the delivery."""

    def __init__(self, herdr, pane):
        self.herdr, self.pane, self.fired = herdr, pane, False

    def __call__(self, argv, **kwargs):
        if list(argv[1:3]) == ["agent", "get"] and argv[3:4] == [self.pane] and not self.fired:
            self.fired = True
            del self.herdr.panes[self.pane]
        return self.herdr(argv, **kwargs)


class TestV2ClientPR4(Base):
    def test_argv_of_the_two_new_reads(self):
        for call, op in ((lambda c: c.permission_list(SES), "session.permission.list"),
                         (lambda c: c.form_list(SES), "session.form.list")):
            runner = FakeRunner()
            call(sb.V2Client("/opt/lab/bin/opencode", {}, runner, 7))
            self.assertEqual(runner.argv, ["/opt/lab/bin/opencode", "api", op, "--param", "sessionID=" + SES])
        self.assertEqual(class_source("V2Client").count("self.runner("), 1)


class TestPR7Source(unittest.TestCase):
    def test_the_engine_still_never_types(self):
        self.assertEqual(herdr_typing_violations(switchboard_source()), [])

    def test_the_decision_model_section_is_one_clearly_marked_block(self):
        source = switchboard_source()
        self.assertEqual(source.count("\n# PR 7: the decision model"), 1)

    def test_the_replay_answers_name_no_private_text(self):
        text = JEV_REPLAY.read_text().lower()
        for fragment in ("sk-", "bearer", "http"):
            self.assertNotIn(fragment, text)


class TestPolicyRedaction(unittest.TestCase):
    def test_credential_shapes_are_removed(self):
        for secret, text in (
                ("sk-or-test-0123456789abcdef", "curl -H 'Authorization: Bearer sk-or-test-0123456789abcdef' x"),
                ("ghp_0123456789abcdefABCDEF", "git push https://ghp_0123456789abcdefABCDEF@host/r"),
                ("hunter2hunter2", "deploy --password hunter2hunter2"),
                ("s3cr3tvalue99", "API_TOKEN=s3cr3tvalue99 ./run"),
                ("AKIA0123456789ABCDEF", "aws --key AKIA0123456789ABCDEF s3 ls"),
                ("realpass", "git clone https://user:realpass@example.test/r.git")):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, sb.redact_command_text(text))

    def test_a_known_key_is_removed_wherever_it_appears_and_ordinary_text_survives(self):
        self.assertNotIn("KEYVALUE", sb.redact_command_text("echo KEYVALUE", ["KEYVALUE"]))
        self.assertEqual(sb.redact_command_text("git log --oneline -5"), "git log --oneline -5")
        self.assertEqual(sb.redact_command_text(None), "")


class TestImportV1Config(Base):
    def test_the_defaults(self):
        self.assertEqual(sb.V1_MOVE_DEFAULTS, {"v1_opencode": None, "import_body_limit_bytes": 120000})
        self.assertIsNone(sb.load_config()["v1_opencode"])

    def test_v1_must_be_an_absolute_path_and_not_the_v2_binary(self):
        for raw, word in (({"v1_opencode": "opencode"}, "absolute"), ({"v1_opencode": 3}, "absolute"),
                          ({"v1_opencode": "/a/b", "opencode": "/a/b"}, "same binary"),
                          ({"import_body_limit_bytes": 10}, "at least"), ({"import_body_limit_bytes": True}, "at least"),
                          ({"import_body_limit_bytes": "9"}, "at least")):
            with self.subTest(raw=raw):
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config(raw)
                self.assertIn(word, str(caught.exception))
        self.assertEqual(sb.validate_config({"v1_opencode": FAKE_V1, "opencode": FAKE_OPENCODE})["v1_opencode"], FAKE_V1)


class TestImportV1AsAProcess(Base):
    def test_a_dry_run_through_the_real_entry_point_and_a_script_standing_in_for_v2(self):
        v2 = self.fake_bin("opencode-v2", 'echo \'{"data": [], "cursor": {"previous": null, "next": null}}\'\n')
        self.write_config({"opencode": str(v2)})
        proc = self.run_cli(SWITCHBOARD, "import-v1", "--file", str(V1_MAIN), "--name", "chief", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would import: v1 %s" % V1_MAIN_ID, proc.stdout)
        self.assertFalse(self.state_dir.exists(), "a dry run does not even make the state directory")

    def test_the_command_is_listed_in_help(self):
        self.assertIn("import-v1", self.run_cli(SWITCHBOARD, "--help").stdout)


class MutantSpliceError(Exception):
    """A mutant whose target is not in the function it names: a broken test, never a caught mutant."""


class TestSyncReportFreezePerBoss(unittest.TestCase):
    def test_the_cycle_scopes_the_freeze_by_the_errors_boss(self):
        source = (REPO / "bin" / "decision_sync.py").read_text()
        self.assertIn('e.get("source") == "reports" and e.get("boss")', source)
        self.assertIn("plan_operations(desired, self._projected(), kinds, now, scoped)", source)


class TestD6Scenario(Base):
    def test_d6_is_present_with_a_control_that_names_what_it_breaks(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        self.assertIn("D6", found)
        d6 = found["D6"]
        self.assertEqual((d6["since_pr"], d6["control"]["fault"]), (15, "bare-issue-number"))
        self.assertIn("offline", d6["tiers"])
        self.assertEqual(sorted(d6["repos"]), [ACME_API, ACME_WEB])
        self.assertEqual([s["repo"] for s in d6["steps"] if s["do"] == "awaiting"], [ACME_API, ACME_WEB])

    def test_the_awaiting_step_may_name_a_repo(self):
        self.assertEqual(sorted(sc.STEP_FIELDS["awaiting"]), ["issue", "label", "on", "repo", "title"])  # PR 18: label


class TestInstallOnLinux(InstallCase):
    PLATFORM = "linux"

    def test_the_unit_is_written_and_enabled_and_removed_again(self):
        out, error = self.install("--opencode", self.machine.binary)
        self.assertIsNone(error, error)
        self.assertTrue(self.service_path().is_file())
        commands = self.machine.commands("systemctl")
        self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "fleet-kit-switchboard.service"], commands)
        second, _ = self.install("--opencode", self.machine.binary)
        self.assertEqual(self.statuses(second)["service"], ["already done"])
        self.install("--uninstall")
        self.assertFalse(self.service_path().exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", "fleet-kit-switchboard.service"],
                      self.machine.commands("systemctl"))


class TestInstallOnAnUnknownPlatform(InstallCase):
    PLATFORM = "plan9"

    def test_the_service_is_skipped_with_the_reason(self):
        out, error = self.install("--opencode", self.machine.binary)
        self.assertIsNone(error, error)
        self.assertIn("skipped (no service manager", self.statuses(out)["service"][0] + out)


class TestCloudReaperInstallOnMacOS(CloudReaperInstall, WrittenFilesAreGuarded, InstallCase):
    PLATFORM = "darwin"

    def test_the_written_plist_is_loaded_at_login_restarts_and_lints(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        import plistlib
        body = plistlib.loads(self.reaper_path().read_bytes())
        self.assertIs(body["RunAtLoad"], True)
        self.assertIs(body["KeepAlive"], True)
        boots = [c for c in self.machine.calls if c[:2] == ["launchctl", "bootstrap"] and c[-1] == str(self.reaper_path())]
        self.assertEqual(len(boots), 1)
        if not shutil.which("plutil"):
            self.skipTest("plutil is macOS only")
        done = subprocess.run(["plutil", "-lint", str(self.reaper_path())], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())


class TestCloudReaperInstallOnLinux(CloudReaperInstall, WrittenFilesAreGuarded, InstallCase):
    PLATFORM = "linux"

    def test_the_written_unit_is_enabled_now_and_verifies(self):
        self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIn(["systemctl", "--user", "enable", "--now", "fleet-kit-cloud-reaper.service"], self.machine.calls)
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze is not here")
        done = subprocess.run(["systemd-analyze", "verify", str(self.reaper_path())], capture_output=True)
        for word in (b"Unknown key", b"Invalid", b"Failed to parse", b"Unknown section"):
            self.assertNotIn(word, done.stderr)


class TestRealStatusAfterInstallOnMacOS(RealStatusAfterInstall, InstallCase):
    PLATFORM = "darwin"

    def test_the_service_it_just_wrote_is_installed_whatever_the_host_is(self):
        for host in ("darwin", "linux"):
            with self.subTest(host=host):
                self.assertIn("service    installed (%s)" % self.service_path(), self.installed_status("darwin", host))


class TestRealStatusAfterInstallOnLinux(RealStatusAfterInstall, InstallCase):
    PLATFORM = "linux"

    def test_the_service_it_just_wrote_is_installed_whatever_the_host_is(self):
        for host in ("linux", "darwin"):
            with self.subTest(host=host):
                self.assertIn("service    installed (%s)" % self.service_path(), self.installed_status("linux", host))


class OrdersReachTheJudgeWhole:
    """Every path an orders text takes to the judge's input: a charter's (resolve_policy_context), the Chief of Staff's
    file (context_of) and an owner's agent's inbound answer (judge_inbound_answer). Each is driven through its real code
    with 30 long orders; the last order must be in the judge's input, whole."""

    @staticmethod
    def long_orders(prefix, count=30, width=250):
        return ["%s %02d %s end-%s-%02d" % (prefix, n, "x" * width, prefix, n) for n in range(count)]

    class Spy:
        def __init__(self):
            self.seen = []

        def decide(self, state, questions):
            self.seen.append(state)
            raise sb.JevError("unreachable", "spy")


class Unavailable(Exception):
    permanent = False
    retry_after = 3


class AlreadyDecidedRefusal(Exception):
    """What the real adapter raises for a 409 "already decided": a permanent refusal that says it is settled outside."""

    permanent = True
    settled_outside = True


class TestOwnerEvaluatorsAreRare(unittest.TestCase):
    """The planning skill and the plan template say owner evaluators are rare and `claim: epic` (the cos rule: no routine PR waits on the owner)."""

    def test_the_skill_and_the_template_say_so(self):
        skill = (REPO / "skills" / "fleet-planning" / "SKILL.md").read_text()
        template = (REPO / "docs" / "plan-template.md").read_text()
        for text in (skill, template):
            self.assertIn("Owner evaluators are rare", text)
            self.assertIn("own eyes are the test", text)
            self.assertIn("claim: epic", text)
            self.assertIn("never for a routine PR" if text is skill else "never waits on the owner", text)
        self.assertIn("its own light plan", template)


class TestDecisionsStaleBound(unittest.TestCase):
    """Work item #449: the staleness bound of the decisions list is 180 s on both sides, and the docs say so."""

    def test_s1_the_daemon_and_plugin_bounds_are_180_seconds(self):
        self.assertEqual(sb.DECISIONS_STALE_SECONDS, 180)
        self.assertIn("export const STALE_MS = 180_000", (DECISIONS_PLUGIN / "decisions.mjs").read_text())
        self.assertIn("export const DECISIONS_STALE_MS = 180_000", (DECISIONS_PLUGIN / "health.mjs").read_text(), "the health view's copy of the bound")

    def test_s3_the_docs_no_longer_say_120_s_for_the_decisions_staleness(self):
        self.assertNotIn("older than 120 s", (DECISIONS_PLUGIN / "README.md").read_text())
        lines = (REPO / "docs" / "switchboard.md").read_text().splitlines()
        stale = [line for line in lines if "120 s" in line and ("stale" in line or "Staleness" in line or "not updating" in line or "older than" in line)]
        self.assertEqual(stale, [])
        self.assertGreaterEqual(len([line for line in lines if "180 s" in line and ("stale" in line or "Staleness" in line or "not updating" in line or "older than" in line)]), 4)


class StubHub:
    def __init__(self, entries):
        self.entries = entries

    def pr_event_snapshot(self):
        return {ref: dict(entry) for ref, entry in self.entries.items()}


class EjectionHub:
    def __init__(self, owner="orch"):
        self.owner, self.routed, self.notified = owner, [], {}

    def notice_names(self, repo, number):
        return self.notified.get((repo, number), [])

    def route_of(self, item, agents, now):
        self.routed.append(item.numbers)
        return (self.owner, "closing") if self.owner else (None, None)


class TestScratchFileRule(unittest.TestCase):
    """Ask #653: the coordination skill tells coders to keep scratch files in a private mktemp -d directory."""

    RULE = 'Write scratch files (PR bodies, message bodies, notes) under a private directory made with mktemp -d, never under a fixed name in the shared temp folder: another agent may write the same name, and a body file is published as it stands.'

    def test_s1_the_skill_carries_the_scratch_rule_exactly_once(self):
        text = (REPO / "skills/fleet-coordination/SKILL.md").read_text()
        self.assertEqual(text.count(self.RULE), 1)

    def test_s2_it_is_never_inside_a_code_fence(self):
        inside = False
        for line in (REPO / "skills/fleet-coordination/SKILL.md").read_text().splitlines():
            if line.strip().startswith("```"):
                inside = not inside
            elif inside:
                self.assertNotIn("mktemp -d", line)

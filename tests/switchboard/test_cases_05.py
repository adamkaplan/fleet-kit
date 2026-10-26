"""Test classes moved out of bin/test-switchboard (text unchanged): cases_05."""
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
import datetime  # noqa: E402  (this section's only import)
from support.core import Base, FakeRunner
from support.fake_jev import FakeJev, FakeOpener, JEV_SETTINGS, KEY_VALUE, confident
from support.installs_and_keys import API, DECISIONS_PLUGIN, InstallCase, ORDERS_CHARTER_BODY, WEB
from support.loader import SCENARIO, SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, _load, captured, class_source, class_source_fn, sb, sc, switchboard_source
from support.orders_and_adapters import EXTERNAL_ADAPTER, FakeWorkHost, cos_orders_file, put_cos_orders, report, worker
from support.paths import BIN, REPO
from support.policy_and_import import NODE, TRIAL, policy_raw_config
from support.scenario_roles import AGENT_FILES, JEV_REPLAY, role_of
from support.service_and_merge import feed_line
from support.wire_fakes import DOC_FAULTS, EPOCH, FAKE_HERDR, FAKE_OPENCODE, SES, _set, fact, herdr_typing_violations, mutate, observation


class TestFaults(Base):
    def test_the_list_is_the_documented_one(self):
        self.assertEqual(tuple(sb.KNOWN_FAULTS), DOC_FAULTS)
        self.assertEqual(sc.KNOWN_FAULTS, DOC_FAULTS)

    def test_off_outside_the_lab(self):
        os.environ[sb.FAULT_ENV] = "no-batch"
        self.assertFalse(sb.fault("no-batch"))
        for marker in ("0", "true", "yes", ""):
            os.environ[sb.LAB_ENV] = marker
            self.assertFalse(sb.fault("no-batch"), marker)

    def test_on_in_the_lab_only_for_named_faults(self):
        os.environ[sb.LAB_ENV] = "1"
        self.assertFalse(sb.fault("no-batch"))
        os.environ[sb.FAULT_ENV] = " no-batch , no-recheck,"
        self.assertTrue(sb.fault("no-batch"))
        self.assertTrue(sb.fault("no-recheck"))
        self.assertFalse(sb.fault("no-dedupe"))

    def test_an_unknown_name_raises_in_or_out_of_the_lab(self):
        for lab in ("1", None):
            with self.subTest(lab=lab):
                if lab:
                    os.environ[sb.LAB_ENV] = lab
                else:
                    os.environ.pop(sb.LAB_ENV, None)
                os.environ[sb.FAULT_ENV] = "no-batch,no-bacth"
                with self.assertRaises(sb.FaultError) as caught:
                    sb.fault("no-batch")
                self.assertIn("no-bacth", str(caught.exception))
        os.environ.pop(sb.FAULT_ENV)
        with self.assertRaises(sb.FaultError):
            sb.fault("not-a-fault")


class TestMessageId(unittest.TestCase):
    def test_shape(self):
        self.assertRegex(sb.derive_message_id("ses_1", ["a"]), r"^msg_[0-9a-f]{26}$")

    def test_pinned_canonical_form(self):
        # A change here changes every id, and v2 would admit retried messages twice.
        canonical = json.dumps(["ses_1", ["a", "b"]], separators=(",", ":"))
        expected = "msg_" + hashlib.sha256(canonical.encode()).hexdigest()[:26]
        self.assertEqual(sb.derive_message_id("ses_1", ["b", "a"]), expected)

    def test_stable_and_independent_of_order_and_duplicates(self):
        first = sb.derive_message_id("ses_1", ["worker.idle:ses_2:10", "reminder:3"])
        self.assertEqual(first, sb.derive_message_id("ses_1", ["reminder:3", "worker.idle:ses_2:10"]))
        self.assertEqual(first, sb.derive_message_id("ses_1", ("reminder:3", "reminder:3", "worker.idle:ses_2:10")))
        self.assertEqual(first, sb.derive_message_id("ses_1", iter(["worker.idle:ses_2:10", "reminder:3"])))

    def test_recipient_and_keys_both_matter(self):
        base = sb.derive_message_id("ses_1", ["a"])
        self.assertNotEqual(base, sb.derive_message_id("ses_2", ["a"]))
        self.assertNotEqual(base, sb.derive_message_id("ses_1", ["a", "b"]))
        # No ambiguity from joining: one key with a comma is not two keys.
        self.assertNotEqual(sb.derive_message_id("ses_1", ["a,b"]), sb.derive_message_id("ses_1", ["a", "b"]))

    def test_nothing_to_derive_from_is_refused(self):
        for recipient, keys in (("ses_1", []), ("", ["a"]), ("ses_1", [""]), ("ses_1", [1])):
            with self.subTest(recipient=recipient, keys=keys):
                with self.assertRaises(ValueError):
                    sb.derive_message_id(recipient, keys)


class TestDecide(Base):
    def decide(self, pending, seen, now=EPOCH + 100, missing=None):
        return sb.decide("platform", pending, seen, now, 90, 600, missing)

    def test_every_branch(self):
        cases = [
            # (what, pending, observation, now, action, reason contains)
            ("young batch waits", [fact("a", EPOCH + 50)], observation(), EPOCH + 100, "wait", "batching until"),
            ("ripe batch goes", [fact("a", EPOCH), fact("b", EPOCH + 50)], observation(), EPOCH + 90, "wake", ""),
            ("send skips the batch", [fact("a", EPOCH + 99, batch=False)], observation(), EPOCH + 100, "wake", ""),
            ("send carries the batch with it", [fact("a", EPOCH + 99), fact("b", EPOCH + 99, batch=False)],
             observation(), EPOCH + 100, "wake", ""),
            ("not found holds", [fact("a")], None, EPOCH + 100, "hold", "not found"),
            ("blocked holds", [fact("a")], observation("blocked"), EPOCH + 100, "hold", "blocked"),
            ("unknown holds", [fact("a")], observation("unknown"), EPOCH + 100, "hold", "status unknown"),
            ("unreadable holds", [fact("a")], observation(problem="cannot read"), EPOCH + 100, "hold", "cannot read"),
            ("working still wakes", [fact("a")], observation("working"), EPOCH + 100, "wake", ""),
            ("engaged gets a note", [fact("a")], observation(human=EPOCH + 40), EPOCH + 100, "note", "you prompted"),
            ("engagement lapses", [fact("a")], observation(human=EPOCH - 600), EPOCH + 100, "wake", ""),
        ]
        for what, pending, seen, now, action, needle in cases:
            with self.subTest(what=what):
                plan = self.decide(pending, seen, now)
                self.assertEqual(plan.action, action, plan.reason)
                self.assertIn(needle, plan.reason)
        self.assertIsNone(self.decide([], observation()))
        self.assertEqual(self.decide([fact("a")], None, missing="herdr unreachable").reason, "herdr unreachable")

    def test_faults_switch_one_safeguard_each_only_in_the_lab(self):
        young = [fact("a", EPOCH + 99)]
        engaged = observation(human=EPOCH + 90)
        os.environ[sb.FAULT_ENV] = "no-batch,no-engagement-gate"
        self.assertEqual(self.decide(young, engaged).action, "wait")  # not in the Lab: no effect
        os.environ[sb.LAB_ENV] = "1"
        os.environ[sb.FAULT_ENV] = "no-batch"
        self.assertEqual(self.decide(young, engaged).action, "note")
        os.environ[sb.FAULT_ENV] = "no-engagement-gate"
        self.assertEqual(self.decide([fact("a")], engaged).action, "wake")


class TestApprovalExemptionGuard(unittest.TestCase):
    """#951: while bin/plan_approval.py is exempt from the authority list, no module of the clear/hold closure may depend on it."""

    MESSAGE = "list bin/plan_approval.py as an authority module, then drop the exemption"
    NEEDLES = ("plan_approval", "plan.approved")

    def setUp(self):
        self.matrix = json.loads((REPO / "docs/review-laws/impact-matrix.json").read_text())
        hold = self.matrix["owner_hold"]
        names = hold["delegated_clear"]["clear_code_paths"] + hold["top_tier_paths"]["authority"]
        self.modules = sorted({p for p in names if p.startswith("bin/") and p.endswith(".py")})

    def exemption_stands(self):
        block = self.matrix["owner_hold"]["top_tier_paths"]
        return "bin/plan_approval.py" in self.matrix["owner_hold"]["authority_modules"]["exempt"] and "bin/plan_approval.py" not in block["authority"]

    def offenders(self, texts):
        return sorted(name for name, text in texts.items() if any(n in text for n in self.NEEDLES))

    def real_texts(self):
        return {name: (REPO / name).read_text() for name in self.modules}

    def test_the_closure_is_derived_and_complete(self):
        for name in ("bin/delegated_clear.py", "bin/impact_hold.py", "bin/impact_floor.py", "bin/merge_gate.py", "bin/plan_ledger.py"):
            self.assertIn(name, self.modules)

    def test_no_clear_or_hold_module_depends_on_plan_approval(self):
        if not self.exemption_stands():
            self.skipTest("the exemption is gone")
        self.assertEqual(self.offenders(self.real_texts()), [], self.MESSAGE)

    def test_mutant_an_import_in_a_closure_module_fails_the_scan(self):
        texts = self.real_texts()
        texts["bin/merge_gate.py"] = "import plan_approval\n" + texts["bin/merge_gate.py"]
        self.assertEqual(self.offenders(texts), ["bin/merge_gate.py"], self.MESSAGE)
        texts = self.real_texts()
        texts["bin/impact_hold.py"] += "\nx = 'plan.approved'\n"
        self.assertEqual(self.offenders(texts), ["bin/impact_hold.py"], self.MESSAGE)


class PipeTruncatingRunner:
    """OpenCode v2's `api` command loses the end of a large reply when it writes to a pipe and exits
    (measured on a real 500 KB transcript: 293 KB arrived, cut inside a string); to a file it is whole.
    This runner does the same: a captured pipe gets the first PIPE_LIMIT bytes, a file gets everything."""

    PIPE_LIMIT = 65536

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, argv, **kwargs):
        sink = kwargs.get("stdout")
        if sink is not None and sink != subprocess.PIPE:
            sink.write(self.payload)
            sink.flush()
            return subprocess.CompletedProcess(argv, 0, None, "")
        return subprocess.CompletedProcess(argv, 0, self.payload[:self.PIPE_LIMIT], "")


class TestV2ClientBigReply(Base):
    def test_a_reply_larger_than_a_pipe_survives_whole(self):
        # The first real use: the Chief of Staff's transcript grew past what v2 flushes to a pipe, and
        # every read of it failed with bad_json, which held deliveries to it and hid its decisions.
        filler = "x" * 4000
        messages = [{"id": "msg_%03d" % n, "type": "assistant", "text": filler} for n in range(60)]
        payload = json.dumps({"data": messages, "cursor": {}})
        self.assertGreater(len(payload), 2 * PipeTruncatingRunner.PIPE_LIMIT)
        client = sb.V2Client("/opt/lab/bin/opencode", {}, PipeTruncatingRunner(payload), 7)
        self.assertEqual(len(client.message_list(SES, limit=200)), 60)

    def test_a_reply_that_fits_still_reads(self):
        payload = json.dumps({"data": [{"id": "msg_001"}]})
        client = sb.V2Client("/opt/lab/bin/opencode", {}, PipeTruncatingRunner(payload), 7)
        self.assertEqual(client.message_list(SES), [{"id": "msg_001"}])

    def test_an_empty_continuation_is_the_end_of_the_listing_not_an_error(self):
        # Measured on a real 104-message transcript read with limit 200: v2 names a `next` cursor even
        # though that page held everything, and following it answers an empty page. That is the end.
        # It used to raise bad_json ("the cursor does not walk older as assumed"), so the decisions
        # list could never read the reports that wait on you.
        def route(argv):
            if "cursor=c1" in argv:
                return (0, json.dumps({"data": [], "cursor": {"next": None, "previous": None}}), "")
            return (0, json.dumps({"data": [{"id": "msg_001"}, {"id": "msg_002"}],
                                   "cursor": {"next": "c1", "previous": None}}), "")
        client = sb.V2Client("/opt/lab/bin/opencode", {}, FakeRunner(route=route), 7)
        pages = list(client.message_pages(SES, 200, "synthetic", max_pages=6))
        self.assertEqual([[m["id"] for m in page] for page, _ in pages], [["msg_001", "msg_002"], []])
        self.assertIs(pages[-1][1], False)  # the walk says it is over, so no caller holds on "more"

    def test_a_continuation_that_repeats_a_message_is_still_an_error(self):
        def route(argv):
            return (0, json.dumps({"data": [{"id": "msg_001"}], "cursor": {"next": "c1", "previous": None}}), "")
        client = sb.V2Client("/opt/lab/bin/opencode", {}, FakeRunner(route=route), 7)
        with self.assertRaises(sb.V2Error):
            list(client.message_pages(SES, 200, "synthetic", max_pages=6))


class TestSelfWakeInstructions(unittest.TestCase):
    NEEDLE = "fleet-switchboard remind <your name> <when> --issue <n>"

    def test_the_role_files_say_it_in_one_sentence(self):
        for path in AGENT_FILES:
            text = " ".join(path.read_text().split())
            with self.subTest(file=path.name):
                self.assertEqual(role_of(path) in ("coder", "orchestrator"), self.NEEDLE in text)
                if self.NEEDLE in text:
                    self.assertIn("set a reminder for yourself", text)
                    self.assertIn("end your turn", text)
                    self.assertIn("Never poll", text)

    def test_the_skill_says_it_fully(self):
        text = " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())
        for phrase in (self.NEEDLE, "a CI run, a rate limit, a checkpoint", "end your turn", "Never poll in a loop",
                       "--checkpoint 30m --overdue 60m", "only if the worker has not reported", "daily wake"):
            self.assertIn(phrase, text)

    def test_the_trimmed_files_kept_their_stop_and_authority_rules(self):
        rules = {"coder": ("NEVER apply `awaiting-user` yourself", "Never deploy", "Never force-push",
                           "Never merge your own PR", "Two failed attempts", "Changing a flag", "Never close another agent's"),
                 "orchestrator": ("NEVER apply `awaiting-user` yourself", "Never deploy", "applied before you ask",
                                  "After two failed attempts", "never widen your own access")}
        for path in AGENT_FILES:
            if role_of(path) in rules:
                text = " ".join(path.read_text().split()).lower()
                for rule in rules[role_of(path)]:
                    with self.subTest(file=path.name, rule=rule):
                        self.assertIn(rule.lower(), text)


class TestScenarioVocabularyPR4(Base):
    def put(self, name, obj, directory=None):
        directory = directory or (self.home / "scenarios")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(obj))
        return path

    def test_a_scenario_may_shorten_the_engine_timing(self):
        scenario = mutate(lambda o: o.update(config={"engaged_minutes": 3, "batch_seconds": 30}))
        self.assertEqual(sc.validate(scenario), [])

    def test_bad_config_is_refused_field_by_field(self):
        for config, word in (({}, "non-empty"), ([], "non-empty"), ({"poll_seconds": 5}, "unknown key"),
                             ({"engaged_minutes": 0}, "engaged_minutes"), ({"batch_seconds": "90"}, "batch_seconds"),
                             ({"engaged_minutes": True}, "engaged_minutes")):
            with self.subTest(config=config):
                errors = sc.validate(mutate(lambda o, c=config: o.update(config=c)))
                self.assertTrue(any(word in e for e in errors), errors)

    def test_every_config_key_is_one_the_engine_reads(self):
        self.assertLessEqual(set(sc.CONFIG_KEYS), set(sb.DEFAULTS))

    def test_delete_state_is_a_world_step_without_fields(self):
        step = {"at": "100h", "actor": "world", "do": "delete-state"}
        self.assertEqual(sc.validate(mutate(lambda o: o["steps"].append(step))), [])
        errors = sc.validate(mutate(lambda o: o["steps"].append(dict(step, actor="you"))))
        self.assertTrue(any("cannot delete-state" in e for e in errors), errors)
        errors = sc.validate(mutate(lambda o: o["steps"].append(dict(step, path="/"))))
        self.assertTrue(any("unknown field" in e for e in errors), errors)

    def test_delivered_may_name_text_it_must_carry(self):
        self.assertEqual(sc.validate(mutate(lambda o: o["expect"][0].update(text="issue #2"))), [])
        self.assertTrue(sc.validate(mutate(lambda o: o["expect"][0].update(text=2))))

    def test_coverage_through_a_pr_enforces_only_the_problems_due_by_then(self):
        directory = self.home / "scenarios"
        self.put("a.json", mutate(lambda o: o.update(id="A1", problem=3)))
        run = lambda *a: self.run_cli(SCENARIO, "coverage", "--dir", str(directory), *a)
        self.assertEqual(run("--enforce", "--through-pr", "2").returncode, 0)
        late = run("--enforce", "--through-pr", "4")
        self.assertEqual(late.returncode, 1)
        self.assertIn("problem(s) 4", late.stderr)
        self.assertEqual(run("--enforce").returncode, 1)
        self.assertIn("not due until PR 4", run("--through-pr", "2").stdout)
        self.put("b.json", mutate(lambda o: o.update(id="B1", problem=4)))
        self.assertEqual(run("--enforce", "--through-pr", "4").returncode, 0)
        self.assertEqual(run("--enforce", "--through-pr", "7").returncode, 1)

    def test_the_repo_covers_problems_3_and_4_by_pr_4(self):
        proc = self.run_cli(SCENARIO, "coverage", "--enforce", "--through-pr", "4")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.run_cli(SCENARIO, "coverage", "--enforce", "--through-pr", "1").returncode, 0)

    def test_due_prs_follow_the_catalog(self):
        self.assertEqual(set(sc.PROBLEM_DUE_PR), set(sc.PROBLEMS))
        found = sc.load_dir()
        firsts = [s["since_pr"] for s in found if s["problem"] == 4]
        self.assertEqual(min(firsts), sc.PROBLEM_DUE_PR[4])


class TestLabHerdrShim(Base):
    def setUp(self):
        super().setUp()
        self.lab = _load("t_proof_shim", BIN / "proof-switchboard")
        self.real = self.fake_bin("real-herdr", 'echo "$@" >> "$0.calls"; echo \'{"result": {}}\'\n')
        self.shim = self.home / "herdr-shim"
        self.lab.write_herdr_shim(str(self.shim), str(self.home), str(self.real))

    def run_shim(self, *args):
        return subprocess.run([str(self.shim), *args], capture_output=True, text=True, env=dict(os.environ))

    def calls(self):
        path = Path(str(self.real) + ".calls")
        return path.read_text().splitlines() if path.exists() else []

    def log(self):
        path = self.home / self.lab.SHIM_LOG
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_it_passes_everything_through_untouched(self):
        proc = self.run_shim("agent", "list")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, '{"result": {}}'))
        self.assertEqual(self.calls(), ["agent list"])
        self.assertEqual(self.log(), [])

    def test_it_records_each_toast_it_is_asked_for_and_still_passes_it_on(self):
        self.run_shim("notification", "show", "--title", "Fleet: coder is blocked", "--body", "waiting")
        (entry,) = self.log()
        self.assertEqual(entry["kind"], "toast")
        self.assertEqual(entry["argv"], ["notification", "show", "--title", "Fleet: coder is blocked", "--body", "waiting"])
        self.assertEqual(len(self.calls()), 1)

    def test_an_armed_close_fires_once_on_the_next_read_of_that_pane(self):
        (self.home / self.lab.SHIM_ARM).write_text("w1:p2\n")
        self.run_shim("agent", "get", "w1:p1")
        self.assertEqual(self.calls(), ["agent get w1:p1"], "another pane is left alone")
        self.run_shim("agent", "get", "w1:p2")
        self.assertEqual(self.calls(), ["agent get w1:p1", "pane close w1:p2", "agent get w1:p2"])
        self.assertEqual([e["kind"] for e in self.log()], ["closed"])
        self.run_shim("agent", "get", "w1:p2")
        self.assertEqual(self.calls()[-1], "agent get w1:p2")
        self.assertEqual(len([c for c in self.calls() if c.startswith("pane close")]), 1)

    def test_nothing_is_closed_unless_armed(self):
        self.run_shim("agent", "get", "w1:p2")
        self.assertEqual(self.calls(), ["agent get w1:p2"])


class TestIntentChange(unittest.TestCase):
    def payload(self, old, new, action="edited", updated="2027-01-15T08:00:00Z", **extra):
        body = {"action": action, "issue": {"number": 7, "body": new, "updated_at": updated},
                "sender": {"login": "alice"}, "changes": {"body": {"from": old}}}
        body.update(extra)
        return body

    OLD = "## Intent\nAdd a health check\nDone when: GET /health returns 200\n"

    def test_an_intent_edit_is_an_item_for_the_chief_of_staff_showing_old_and_new(self):
        new = "## Intent\nAdd a readiness check\nDone when: GET /health returns 200\n"
        change = sb.intent_change_fact(self.payload(self.OLD, new))
        self.assertEqual((change.key, change.recipient, change.issue, change.kind, change.sender),
                         ("intent.edit:7:2027-01-15T08:00:00Z", "chief-of-staff", 7, "intent.edit", "alice"))
        self.assertEqual(change.summary.splitlines(), [
            "The Intent section of #7 was edited on GitHub:", "Intent: Add a health check -> Add a readiness check"])
        self.assertEqual(change.created, datetime.datetime(2027, 1, 15, 8, 0, tzinfo=datetime.timezone.utc).timestamp())

    def test_a_done_when_change_is_shown_too_and_a_missing_side_is_none(self):
        new = "## Intent\nAdd a health check\n"
        summary = sb.intent_change_fact(self.payload(self.OLD, new), "cos").summary
        self.assertIn("Done when: GET /health returns 200 -> (none)", summary)
        self.assertNotIn("Intent: Add a health check ->", summary)
        added = sb.intent_change_fact(self.payload("nothing here", self.OLD)).summary
        self.assertIn("Intent: (none) -> Add a health check", added)

    def test_an_edit_that_leaves_the_intent_alone_is_nothing(self):
        new = self.OLD + "\n## Notes\nmore text\n"
        self.assertIsNone(sb.intent_change_fact(self.payload(self.OLD, new)))
        self.assertIsNone(sb.intent_change_toast(self.payload(self.OLD, new)))

    def test_other_events_are_nothing(self):
        new = "## Intent\nChanged\n"
        self.assertIsNone(sb.intent_change_fact(self.payload(self.OLD, new, action="opened")))
        title_edit = self.payload(self.OLD, new)
        del title_edit["changes"]["body"]
        self.assertIsNone(sb.intent_change_fact(title_edit))
        self.assertIsNone(sb.intent_change_fact("not a payload"))

    def test_the_toast_is_a_title_and_a_body(self):
        toast = sb.intent_change_toast(self.payload(self.OLD, "## Intent\nNew\nDone when: GET /health returns 200\n"))
        self.assertEqual(toast, {"title": "Intent changed on #7", "body": "Intent: Add a health check -> New"})

    def test_the_key_makes_a_redelivered_event_one_item(self):
        new = "## Intent\nNew\n"
        a = sb.intent_change_fact(self.payload(self.OLD, new))
        b = sb.intent_change_fact(self.payload(self.OLD, new))
        c = sb.intent_change_fact(self.payload(self.OLD, new, updated="2027-01-15T09:00:00Z"))
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.key, c.key)

    def test_a_payload_without_updated_at_is_refused(self):
        bad = self.payload(self.OLD, "## Intent\nNew\n", updated=None)
        with self.assertRaises(sb.FactError):
            sb.intent_change_fact(bad)


class TestVerdictRecordingText(unittest.TestCase):
    """The reviewer and tester roles record their verdict in ONE command as the last step of the review they already do (text only)."""
    ONE = 'fleet-switchboard verdict --all --pr <repo#N> --as reviewer|tester --result pass|fail --head <full head sha> --evidence'

    def test_the_skill_and_the_coder_definitions_say_the_one_command_and_the_per_scenario_fallback(self):
        for rel in ("skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/copilot/coder.agent.md"):
            with self.subTest(file=rel):
                text = " ".join((REPO / rel).read_text().split())
                self.assertIn(self.ONE, text)
                self.assertIn("per-scenario form" if "SKILL" in rel else "until it is merged, record each scenario", text)
                self.assertIn("A push voids it", text)
                self.assertIn("fleet-switchboard verdict <scenario> --pr <repo#N> --as reviewer|tester --result pass|fail --head <full head sha>", text)  # the spelled-out fallback
                self.assertIn("Never record a verdict on a PR you wrote", text)

    def test_the_orchestrator_definitions_point_at_it_and_the_planning_skill_says_owner_evaluators_are_rare(self):
        for rel in ("agents/opencode/orchestrator.md", "agents/copilot/orchestrator.agent.md"):
            with self.subTest(file=rel):
                self.assertIn("Reviewers and testers record their verdicts when they finish", (REPO / rel).read_text())
        planning = " ".join((REPO / "skills" / "fleet-planning" / "SKILL.md").read_text().split())
        self.assertIn("Owner evaluators are rare", planning)
        self.assertIn("a coder's only extra step is `prove`", planning)


class TestWorkerRulesInSkills(unittest.TestCase):
    """#719: the message cap, the daemon-recorded ci verdict and the plan check wordings, each said once."""
    CAP = "`send` messages are capped (about 3000 characters in practice, 4000 hard): put long text in a file made under `mktemp -d` and pass it as `\"$(cat <file>)\"`."
    CI = "The daemon records the `ci` verdict itself about five minutes after CI is green at the head, so read `fleet-switchboard gates <pr>` and do not ask for `gates ci-record`, which stays for a hurry."
    LIST = "## Wordings `plan check` accepts"
    SHAPES = ("every declared `DW`/`PC` is covered by a scenario", "`discriminates` names a concrete wrong build (a stub that returns an empty list)",
              "`pass` has an observable number or status", "the declared clauses joined equal the ask's Done-when", "check a slice with `--ask <its own work item>`")

    def read(self, skill):
        return (REPO / "skills" / skill / "SKILL.md").read_text()

    def test_the_message_cap_sentence_is_in_the_coordination_skill_once(self):
        self.assertEqual(self.read("fleet-coordination").count(self.CAP), 1)

    def test_the_ci_verdict_sentence_is_in_the_skills_once_overall(self):
        self.assertEqual(sum(self.read(s).count(self.CI) for s in ("fleet-coordination", "fleet-planning")), 1)

    def test_the_plan_check_wording_list_is_in_the_planning_skill_once_with_its_five_shapes(self):
        text = self.read("fleet-planning")
        self.assertEqual(text.count(self.LIST), 1)
        section = text.split(self.LIST, 1)[1].split("\n## ", 1)[0]
        for shape in self.SHAPES:
            self.assertEqual(section.count(shape), 1, shape)

    RERUN = "`gh run rerun --failed` reuses the run's old merge ref, so a fix that landed on the base branch afterwards does not reach the pull request; closing and reopening it (head unchanged) recomputes the merge ref and starts fresh CI, and for a held pull request the orchestrator that owns it says so first, because the owner's clear is bound to the head, which does not change. A run's logs may be readable while it is in progress, once a job has uploaded its logs, through the jobs logs API (`gh api repos/<repo>/actions/jobs/<id>/logs`, add `--allow-escape-sequences`; it answers 404 before that), because `gh run view --log-failed` waits for the run to end."

    def test_the_rerun_and_in_progress_logs_paragraph_is_in_the_coordination_skill_exactly_once(self):
        self.assertEqual(self.read("fleet-coordination").count(self.RERUN), 1)
        skill = self.read("fleet-coordination")
        for word in ("rerun --failed", "old merge ref", "reopening", "bound to the head", "actions/jobs", "log-failed"):
            self.assertIn(word, skill)

    def test_the_rerun_paragraph_names_roles_never_agents_or_people(self):
        self.assertNotRegex(self.read("fleet-coordination").split("`gh run rerun --failed`",1)[1].split("\n\n",1)[0], r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")

    def test_the_texts_name_roles_never_agents_or_people(self):
        for text in (self.CAP, self.CI, self.LIST) + self.SHAPES:
            self.assertNotRegex(text, r"(?i)\b(coder\d|cos\b|fleet-upkeep|adam\b)")


class TestJevConfig(Base):
    def config(self, **raw):
        return sb.validate_config(dict({"opencode": FAKE_OPENCODE}, **raw))

    def test_no_jev_keys_leave_the_config_as_it_was(self):
        config = self.config()
        self.assertNotIn("jev", config)
        self.assertNotIn("shadow_judges", config)

    def test_a_jev_object_is_completed_with_defaults(self):
        config = self.config(jev={"min_confidence": 0.8})
        self.assertEqual(config["jev"], dict(JEV_SETTINGS, min_confidence=0.8))
        self.assertEqual(config["jev"]["model"], "typesafe/jev-1.13")
        self.assertEqual(config["jev"]["key_env"], "OPENROUTER_API_KEY")
        self.assertEqual(config["jev"]["timeout_seconds"], 20)

    def test_unknown_subkeys_are_warned_about_by_name_and_ignored(self):
        sb._WARNED_CONFIG.clear()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.config(jev={"api_key": "x"})
        self.assertRegex(err.getvalue(), "'jev' contains key.*api_key")

    def test_shadow_judges_follows_whether_jev_is_configured(self):
        self.assertTrue(self.config(jev={})["shadow_judges"])
        self.assertFalse(self.config(jev={}, shadow_judges=False)["shadow_judges"])
        self.assertFalse(self.config(shadow_judges=False)["shadow_judges"])
        with self.assertRaisesRegex(sb.ConfigError, "needs a 'jev' object"):
            self.config(shadow_judges=True)

    def test_bad_values_are_refused_with_the_key_named(self):
        cases = [({"key_env": KEY_VALUE}, "jev.key_env"), ({"key_env": ""}, "jev.key_env"),
                 ({"url": "http://openrouter.ai/x"}, "jev.url"), ({"url": "ftp://x"}, "jev.url"),
                 ({"timeout_seconds": 0}, "jev.timeout_seconds"), ({"timeout_seconds": "9"}, "jev.timeout_seconds"),
                 ({"min_confidence": 0}, "jev.min_confidence"), ({"min_confidence": 1.5}, "jev.min_confidence"),
                 ({"model": ""}, "jev.model"), ({"replay": "relative.json"}, "jev.replay"),
                 ({"record_file": "r.jsonl"}, "jev.record_file"),
                 ({"record_file": "/tmp/a", "replay": "/tmp/b"}, "record real calls")]
        for jev, needle in cases:
            with self.subTest(jev=jev):
                with self.assertRaises(sb.ConfigError) as caught:
                    self.config(jev=jev)
                self.assertIn(needle, str(caught.exception))
        with self.assertRaisesRegex(sb.ConfigError, "jev"):
            self.config(jev=["x"])
        with self.assertRaisesRegex(sb.ConfigError, "shadow_judges"):
            self.config(jev={}, shadow_judges="yes")

    def test_the_refusal_of_a_key_in_the_config_does_not_echo_it_as_a_secret_rule(self):
        # The error names the setting; the value shown is the user's own mistake, but
        # the documented place for the key is the environment, and the message says so.
        with self.assertRaisesRegex(sb.ConfigError, "NAME of an environment variable"):
            self.config(jev={"key_env": KEY_VALUE})

    def test_http_is_allowed_only_for_the_local_machine(self):
        self.assertEqual(self.config(jev={"url": "http://127.0.0.1:8080/x"})["jev"]["url"], "http://127.0.0.1:8080/x")
        self.assertEqual(self.config(jev={"url": "http://localhost:8080/x"})["jev"]["url"], "http://localhost:8080/x")
        with self.assertRaises(sb.ConfigError):
            self.config(jev={"url": "http://localhost.evil.test/x"})


class TestReplayJev(Base):
    def replay(self, data):
        path = self.home / "replay.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return sb.ReplayJev(str(path))

    def question(self, *options):
        return {"c": {"type": "choice", "instructions": "x", "criteria": {o: o for o in options}}}

    def test_rules_match_the_message_in_order_and_scale_to_the_options_offered(self):
        replay = self.replay({"rules": [{"contains": "alpha", "choice": {"new": 0.9, "current": 0.1, "615": 0.5}},
                                        {"contains": "gone", "error": "timeout"},
                                        {"choice": {"current": 1}}]})
        out = replay.decide({"message": "an alpha thing"}, self.question("current", "new"))
        self.assertEqual(out["answers"]["c"]["choice"], "new")
        self.assertAlmostEqual(sum(out["answers"]["c"]["probabilities"].values()), 1.0)
        self.assertEqual(replay.decide({"message": "else"}, self.question("current", "new"))["answers"]["c"]["choice"],
                         "current")
        with self.assertRaises(sb.JevError) as caught:
            replay.decide({"message": "gone"}, self.question("current", "new"))
        self.assertEqual(caught.exception.reason, "timeout")

    def test_noul_rules_answer_by_question_id_and_default_to_low(self):
        replay = self.replay({"rules": [{"noul": {"hard_to_reverse": 0.9}}]})
        out = replay.decide({"tool": "x"}, sb.policy_questions())
        self.assertEqual({k: v["noul"] for k, v in out["answers"].items()},
                         {"outside_intent": 0.0, "hard_to_reverse": 0.9, "speaks_for_you": 0.0, "outside_scope": 0.0})

    def test_a_missing_or_broken_file_or_an_unmatched_question_is_a_bad_response_not_a_crash(self):
        for replay in (sb.ReplayJev(str(self.home / "nope.json")), self.replay("not json {"),
                       self.replay({"rules": [{"contains": "zzz", "choice": {"new": 1}}]})):
            with self.subTest(replay=replay.path):
                with self.assertRaises(sb.JevError) as caught:
                    replay.decide({"message": "x"}, self.question("current", "new"))
                self.assertEqual(caught.exception.reason, "bad_response")

    def test_the_committed_answers_file_is_valid_and_hand_written(self):
        data = json.loads(JEV_REPLAY.read_text())
        self.assertEqual(set(data), {"note", "rules"})
        replay = sb.ReplayJev(str(JEV_REPLAY))
        out = replay.decide({"message": "x billing export"}, sb.classify_request("x", None, None, [])[1])
        self.assertEqual(out["answers"][sb.CLASSIFY_ID]["choice"], "new")

    def test_build_jev_picks_the_backend_the_config_names(self):
        self.assertIsNone(sb.build_jev({}))
        self.assertIsInstance(sb.build_jev({"jev": dict(JEV_SETTINGS)}), sb.JevClient)
        self.assertIsInstance(sb.build_jev({"jev": dict(JEV_SETTINGS, replay="/x/y.json")}), sb.ReplayJev)


class TestClassify(unittest.TestCase):
    ASKS = [{"issue": 615, "intent": "fix the project agents", "owner": "cos-615"}]

    def classify(self, answers, asks=(), **kwargs):
        jev = FakeJev(answers)
        out = sb.classify(jev, "a new message", "the one before", {"issue": 7, "intent": "Add health", "done_when": "200"},
                          list(asks), **kwargs)
        self.jev = jev
        return out

    def test_the_question_has_current_one_option_per_open_ask_and_new(self):
        self.classify(confident("current"), self.ASKS)
        state, questions = self.jev.calls[0]["state"], self.jev.calls[0]["questions"]
        question = questions[sb.CLASSIFY_ID]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(list(question["criteria"]), ["current", "615", "new"])
        self.assertEqual(state["message"], "a new message")
        self.assertEqual(state["previous_message"], "the one before")
        self.assertIn("Add health", state["current_ask"]["intent"])
        self.assertIn("done when: 200", state["current_ask"]["intent"])
        self.assertEqual(state["open_asks"][0], {"issue": "615", "intent": "fix the project agents", "owner": "cos-615"})
        self.assertEqual(len(questions), 1, "one question per message")

    def test_the_current_ask_is_not_also_an_ask_option(self):
        options = sb.classify_options({"issue": 615}, self.ASKS)
        self.assertEqual(list(options), ["current", "new"])

    def test_a_confident_answer_is_taken_whatever_it_is(self):
        for choice in ("current", "615", "new"):
            with self.subTest(choice=choice):
                out = self.classify(confident(choice, 0.93, ("current", "615", "new")), self.ASKS)
                self.assertEqual((out["choice"], out["confident"]), (choice, True))
                self.assertEqual(out["jev_choice"], choice)
                self.assertIn("0.93", out["reason"])
                self.assertEqual(out["usage"]["cost"], 3e-05)
                self.assertEqual(out["latency_ms"], 12)

    def test_the_threshold_is_inclusive_and_configurable(self):
        self.assertTrue(self.classify(confident("new", 0.7))["confident"])
        out = self.classify(confident("new", 0.69))
        self.assertEqual((out["choice"], out["confident"], out["jev_choice"]), ("current", False, "new"))
        self.assertIn("unsure", out["reason"])
        self.assertEqual(self.classify(confident("new", 0.69), min_confidence=0.6)["choice"], "new")
        self.assertEqual(self.classify(confident("new", 0.9), min_confidence=0.95)["choice"], "current")

    def test_no_probabilities_is_not_confidence(self):
        out = self.classify({sb.CLASSIFY_ID: {"type": "choice", "choice": "new"}})
        self.assertEqual((out["choice"], out["confident"]), ("current", False))
        self.assertIn("no probabilities", out["reason"])

    def test_every_error_defaults_to_current_with_its_reason(self):
        for reason in sb.JEV_REASONS:
            with self.subTest(reason=reason):
                out = self.classify(reason)
                self.assertEqual((out["choice"], out["confident"]), ("current", False))
                self.assertEqual(out["reason"], "jev " + reason)
                self.assertEqual(out["latency_ms"], 12)

    def test_long_messages_are_cut_before_they_are_sent(self):
        state, _ = sb.classify_request("x" * 5000, None, None, [])
        self.assertLessEqual(len(state["message"]), sb.JEV_TEXT_LIMIT)


class TestP5SlowOrUnreachableJudge(Base):
    """Catalog P5: the configured outcome stands, and nothing blocks on the judge.

    The tool-call hook arrives with PR 9, so this runs the combiner's whole path
    (`judge_tool_call`) against a fake that times out, as the scenario would:
    the safeguard is "no answer keeps the configured outcome" and its control
    is the Lab fault `judge-loosens`.
    """

    def judged(self, configured, reason):
        return sb.judge_tool_call(FakeJev(reason), configured, {"tool": "shell"})

    def test_every_failure_keeps_every_configured_outcome(self):
        for reason in sb.JEV_REASONS:
            for configured in sb.POLICY_OUTCOMES:
                with self.subTest(reason=reason, configured=configured):
                    out = self.judged(configured, reason)
                    self.assertEqual(out["outcome"], configured)
                    self.assertIn("configured outcome stands", out["reason"])
                    self.assertEqual(out["fired"], [])
                    self.assertEqual(out["latency_ms"], 12)

    def test_the_slow_judge_is_cut_off_by_the_client_timeout_not_waited_for(self):
        opener = FakeOpener(sb.socket.timeout("slow"))
        os.environ["OPENROUTER_API_KEY"] = KEY_VALUE
        client = sb.JevClient(dict(JEV_SETTINGS, timeout_seconds=0.5), opener=opener)
        out = sb.judge_tool_call(client, "ask", {"tool": "shell"})
        self.assertEqual((out["outcome"], opener.calls[0]["timeout"]), ("ask", 0.5))

    def test_the_control_judge_loosens_downgrades_it(self):
        os.environ[sb.LAB_ENV] = "1"
        os.environ[sb.FAULT_ENV] = "judge-loosens"
        loosened = self.judged("ask", "timeout")["outcome"]
        self.assertEqual(loosened, "allow")
        self.assertNotEqual(loosened, "ask", "the run's expectation fails under the control")
        os.environ[sb.FAULT_ENV] = ""
        self.assertEqual(self.judged("ask", "timeout")["outcome"], "ask")


class TestBriefParsing(unittest.TestCase):
    def test_the_sections_are_read_with_or_without_markup(self):
        parsed = sb.parse_brief("## Handoff: ask 2\nGoal: retyped, ignored\n**Done so far:** a\nb\n"
                                "## Next steps\n1. c\n- Watch out for: d\n")
        self.assertEqual(parsed, {"goal": "retyped, ignored", "done so far": "a\nb", "next steps": "1. c",
                                  "watch out for": "d"})

    def test_a_brief_without_done_so_far_or_next_steps_is_refused(self):
        for text, missing in (("Next steps: x\n", "Done so far"), ("Done so far: x\n", "Next steps"),
                              ("Watch out for: x\n", "Done so far and no Next steps"),
                              ("Done so far:\nNext steps: x\n", "Done so far")):
            with self.subTest(brief=text), self.assertRaises(sb.HandoffRefused) as caught:
                sb.parse_brief(text)
            self.assertIn(missing, str(caught.exception))

    def test_words_outside_the_sections_are_refused_not_dropped(self):
        with self.assertRaises(sb.HandoffRefused) as caught:
            sb.parse_brief("Please take over.\nDone so far: a\nNext steps: b\n")
        self.assertIn("outside its sections", str(caught.exception))

    def test_an_oversized_brief_is_refused(self):
        with self.assertRaises(sb.HandoffRefused):
            sb.parse_brief("Done so far: " + "x" * sb.BRIEF_LIMIT + "\nNext steps: b")

    def test_the_markdown_is_the_docs_with_goal_from_the_ask(self):
        issue = sb.IssueIntent(615, "open", "t", "Fix the agents", "both answer", None, False)
        text = sb.handoff_markdown(issue, {"goal": "mine", "done so far": "a", "next steps": "b"})
        self.assertEqual(text, "## Handoff: ask 615\nGoal: Fix the agents (Done when: both answer)\n"
                               "Done so far: a\nNext steps: b\nWatch out for: nothing noted")
        bare = sb.IssueIntent(7, "open", "t", None, None, None, False)
        self.assertIn("Goal: no intent recorded\n", sb.handoff_markdown(bare, {"done so far": "a", "next steps": "b"}))


class TestPolicyConfig(Base):
    def test_off_by_default_with_documented_defaults(self):
        policy = sb.validate_config({})["policy"]
        self.assertEqual(policy, {"enabled": False, "budget_ms": 1500, "thresholds": sb.POLICY_DEFAULT_THRESHOLDS, "routine_filter": True, "cache_seconds": 300,
                                  "cache_entries": 200, "shadow": False})

    def check_rejected(self, policy, fragment, **extra):
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict({"policy": policy}, **extra))
        self.assertIn(fragment, str(caught.exception))

    def test_unknown_subkeys_are_warned_about_by_name_and_ignored(self):
        for policy, name in (({"budgetms": 5}, "budgetms"), ({"enabled": False, "extra": 1}, "extra")):
            sb._WARNED_CONFIG.clear()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                sb.validate_config({"policy": policy})
            self.assertIn(name, err.getvalue())

    def test_each_value_is_checked(self):
        for policy, fragment in (({"enabled": "yes"}, "policy.enabled"), ({"budget_ms": 0}, "policy.budget_ms"),
                                 ({"budget_ms": "9"}, "policy.budget_ms"), ({"budget_ms": True}, "policy.budget_ms"),
                                 ({"cache_seconds": -1}, "policy.cache_seconds"), ({"shadow": "yes"}, "policy.shadow"),
                                 ({"cache_entries": 0}, "policy.cache_entries"),
                                 ({"cache_entries": 99999}, "policy.cache_entries"),
                                 ({"thresholds": []}, "policy.thresholds"),
                                 ({"thresholds": {"outside_intent": 0}}, "policy.thresholds.outside_intent"),
                                 ({"thresholds": {"outside_intent": 1.5}}, "policy.thresholds.outside_intent"),
                                 ({"thresholds": {"nothing": 0.5}}, "names no question")):
            with self.subTest(policy=policy):
                self.check_rejected(policy, fragment)
        self.check_rejected("on", "must be an object")

    def test_enabling_needs_a_decision_model(self):
        self.check_rejected({"enabled": True}, "needs a 'jev' object")
        config = sb.validate_config({"policy": {"enabled": True, "budget_ms": 400, "thresholds": {"outside_scope": 0.8}},
                                     "jev": {}})
        self.assertEqual((config["policy"]["budget_ms"], config["policy"]["thresholds"]), (400, dict(sb.POLICY_DEFAULT_THRESHOLDS, outside_scope=0.8)))

    def test_the_fault_exists_and_works_only_in_the_lab(self):
        self.assertIn("no-policy-judge", sb.KNOWN_FAULTS)
        os.environ[sb.FAULT_ENV] = "no-policy-judge"
        os.environ.pop(sb.LAB_ENV, None)
        request = {"configured": "allow", "tool": "shell"}
        self.assertIsNone(sb.policy_unjudged_reason(policy_raw_config(), request))
        os.environ[sb.LAB_ENV] = "1"
        self.assertIn("no-policy-judge", sb.policy_unjudged_reason(policy_raw_config(), request))


class TestPolicyRequest(unittest.TestCase):
    GOOD = {"sessionID": "ses_0123abcd", "tool": "shell", "arguments": "git status", "outcome": "ask"}

    def parse(self, **change):
        return sb.parse_policy_request(json.dumps(dict(self.GOOD, **change)))

    def test_a_good_request(self):
        self.assertEqual(self.parse(), {"session": "ses_0123abcd", "tool": "shell", "arguments": "git status",
                                        "configured": "ask"})

    def test_arguments_may_be_missing_or_structured_and_are_bounded(self):
        self.assertEqual(self.parse(arguments=None)["arguments"], "")
        self.assertEqual(self.parse(arguments={"b": 1, "a": 2})["arguments"], '{"a":2,"b":1}')
        self.assertEqual(len(self.parse(arguments="x" * 99999)["arguments"]), sb.POLICY_ARG_LIMIT)

    def test_every_malformed_request_is_refused(self):
        bad = ["", "not json", "[]", "7", '"x"', "null", json.dumps({}),
               json.dumps(dict(self.GOOD, sessionID="x; rm -rf /")), json.dumps(dict(self.GOOD, sessionID="--help")),
               json.dumps(dict(self.GOOD, sessionID=7)), json.dumps(dict(self.GOOD, tool="rm")),
               json.dumps(dict(self.GOOD, tool=None)), json.dumps(dict(self.GOOD, outcome="hard-deny")),
               json.dumps(dict(self.GOOD, outcome="permit")), json.dumps({k: v for k, v in self.GOOD.items() if k != "outcome"})]
        for text in bad:
            with self.subTest(text=text[:50]):
                with self.assertRaises(sb.PolicyRequestError):
                    sb.parse_policy_request(text)

    def test_the_tool_names_are_the_documented_nine(self):
        self.assertEqual(sb.POLICY_TOOLS, ("shell", "edit", "webfetch", "subagent", "read", "glob", "grep", "skill", "other"))
        self.assertEqual(sb.POLICY_PASS_THROUGH, ("read", "glob", "grep", "skill"))


class TestJudgeToolWritesNothingElsewhere(unittest.TestCase):
    """One AST test: the policy section never writes to GitHub or herdr."""

    START, END = "# PR 9: the tool-call policy judge", "def build_parser():"
    FORBIDDEN_NAMES = {"HerdrClient", "herdr", "notification_show", "pane_report_metadata", "agent_prompt",
                       "agent_send_keys", "launch_agent", "send_poke", "cmd_send", "cmd_launch", "GitHubHub"}
    WRITE_FLAGS = ("--method", "-X", "--input", "-f", "-F", "--field", "--raw-field", "POST", "PATCH", "PUT", "DELETE")
    WRITE_PHRASES = ("issue comment", "issue edit", "pr merge", "pr comment", "sub_issues", "/comments", "/labels")

    def section(self):
        """The policy section: its part files, and the statements of it that stay in the entry (S2 keeps the ones the clear's closure reads), between the DEFAULTS line and the next section."""
        entry = switchboard_source()
        kept = entry[entry.index('DEFAULTS["policy"] = None'):entry.index("# PR 10: move a v1 session")]
        return "\n".join([kept] + [path.read_text() for path in SWITCHBOARD_SOURCE_FILES if path.name in ("judge_request.py", "judge.py")])   # the policy judge's own parts: another part (plan review, which launches lanes) is not the policy section

    def test_no_herdr_and_no_github_write_is_reachable_from_the_section(self):
        tree = ast.parse(self.section())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertEqual(sorted(names & self.FORBIDDEN_NAMES), [])
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for text in strings:
            self.assertNotIn(text, self.WRITE_FLAGS)
            for phrase in self.WRITE_PHRASES:
                self.assertNotIn(phrase, text)

    def test_its_only_github_reads_are_get_calls_through_the_reader(self):
        tree = ast.parse(self.section())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_api"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(herdr_typing_violations(switchboard_source()), [], "the engine still never types")

    def test_the_section_is_one_clearly_marked_block(self):
        self.assertEqual(switchboard_source().count("\n# PR 9: the tool-call policy judge"), 1)


class TestImportV1Client(Base):
    def client(self, **kwargs):
        runner = FakeRunner(**kwargs)
        return sb.V2Client("/opt/lab/bin/opencode", {}, runner, 7), runner

    def test_argv_of_the_import(self):
        client, runner = self.client()
        info, messages = {"id": "ses_a"}, [{"id": "msg_a"}]
        client.session_import(info, messages, {"directory": "/work"})
        self.assertEqual(runner.argv[:3], ["/opt/lab/bin/opencode", "api", "experimental.session.import"])
        self.assertEqual(runner.argv[3], "-d")
        self.assertEqual(json.loads(runner.argv[4]), {"info": info, "messages": messages, "location": {"directory": "/work"}})
        self.assertEqual(len(runner.argv), 5, "no --param: the body names the session")

    def test_the_location_is_left_out_when_not_given(self):
        client, runner = self.client()
        client.session_import({"id": "ses_a"}, [])
        self.assertNotIn("location", json.loads(runner.argv[4]))

    def test_the_arguments_are_checked_before_anything_runs(self):
        client, runner = self.client()
        for call in (lambda: client.session_import({"id": "x"}, []), lambda: client.session_import({}, []),
                     lambda: client.session_import("ses_a", []), lambda: client.session_import({"id": "ses_a"}, "x")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(runner.calls, [])

    def test_the_list_keeps_its_cursor(self):
        body = {"data": [{"id": "ses_1"}], "cursor": {"previous": None, "next": "c2"}}
        client, runner = self.client(default=(0, json.dumps(body), ""))
        self.assertEqual(client.session_list_page("/work", "c1", 50), ([{"id": "ses_1"}], "c2"))
        self.assertEqual(runner.argv, ["/opt/lab/bin/opencode", "api", "session.list", "--param", "limit=50",
                                       "--param", "directory=/work", "--param", "cursor=c1"])
        last = {"data": [], "cursor": {"previous": "p", "next": None}}
        client, _ = self.client(default=(0, json.dumps(last), ""))
        self.assertEqual(client.session_list_page(), ([], None))

    def test_a_list_without_data_is_an_error(self):
        client, _ = self.client(default=(0, "{}", ""))
        with self.assertRaises(sb.V2Error):
            client.session_list_page()

    def test_the_other_calls_still_unwrap_data(self):
        client, _ = self.client(default=(0, '{"data": {"id": "ses_1"}, "cursor": {}}', ""))
        self.assertEqual(client.session_get("ses_1"), {"id": "ses_1"})

    def test_still_one_runner_call(self):
        self.assertEqual(class_source("V2Client").count("self.runner("), 1)


# ======================================================================
# #117: the waiting-on-the-person sweep (S1 to S5), synthetic repos only
# ======================================================================
class FakeSweepHub:
    """read_pages / _read_one over a canned repo: `open` is {number: issue or PR record}, `comments` a list of records;
    every call is counted."""

    def __init__(self):
        self.open, self.comments, self.closed, self.reads, self.single = {}, [], set(), [], []
        self.closed_records = {}   # the closed or merged items the closed-state list read returns
        self.caps, self.fresh, self.live = [], [], {}   # live: a fresher copy of an item than `open` shows

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        self.reads.append(path)
        self.caps.append(cap)
        match = re.search(r"/issues/(\d+)/comments", path)
        if match:   # one item's own comments (a fresh re-check)
            number = int(match.group(1))
            return [c for c in self.comments if c["issue_url"].endswith("/%d" % number)][::-1], False
        if "/issues/comments" in path:
            return list(self.comments), False
        if "state=closed" in path:
            return list(self.closed_records.values()), False
        return list(self.open.values()), False

    def _read_one(self, repo, path, now, fresh=False):
        self.single.append((repo, path))
        self.fresh.append(fresh)
        number = int(path.rsplit("/", 1)[1])
        data = dict(self.live.get(number, self.open.get(number, self.closed_records.get(number, {}))))
        data["number"] = number
        data["state"] = "closed" if number in self.closed else "open"
        return data


class TestTriageRuleInputs(unittest.TestCase):
    NAMES = {"agents", "decisions", "issues", "comments", "merged", "closed"}

    def kinds(self):
        kinds = set(sb.TRIAGE_KINDS)                                                 # every rule the triage can raise (a seen record is "<rule> <ref>")
        kinds |= set(re.findall(r'"([A-Z][A-Z0-9]*) " \+', (REPO / "bin" / "triage_digest.py").read_text()))   # the key prefixes the seen-memory writers build
        return kinds

    def test_every_record_kind_the_writers_can_produce_has_an_input_set_of_registry_names(self):
        kinds = self.kinds()
        self.assertIn("PARKED", kinds)                                               # the regex must still find the writers' prefixes
        for kind in sorted(kinds):
            needs = sb.triage_inputs(kind)
            self.assertIsNotNone(needs, "a record kind needs the inputs it judges: %s" % kind)
            self.assertTrue(needs and set(needs) <= self.NAMES, kind)
            if kind not in ("T5", "T6", "T10"):                                      # a kind that judges ours-or-not reads the running agents
                self.assertIn("agents", needs, kind)

    def test_the_other_owner_and_invalid_park_kinds_judge_the_union_of_every_rules_inputs(self):
        union = {name for needs in sb.TRIAGE_RULE_INPUTS.values() for name in needs}
        for kind in sb.TRIAGE_UNION_KINDS:
            self.assertEqual(set(sb.triage_inputs(kind)), union, kind)               # derived from the table, never a second hand list
        with mock.patch.dict(sb.TRIAGE_RULE_INPUTS, {"T99": ("newinput",)}):
            self.assertIn("newinput", sb.triage_inputs("PARKED"))                    # a new rule's input is included at once

    def marks(self, needs, bad=None, repo="acme/api"):
        engine = types.SimpleNamespace(read_whole={}, _note=lambda *a, **k: None)
        for name in self.NAMES | set(needs):
            key = name if name in ("agents", "decisions") else "%s:%s" % (name, repo)
            sb.read_mark(engine, key, name != bad, "" if name != bad else "why", 1.0)
        return engine

    def test_each_kind_is_kept_while_any_one_input_it_judges_is_not_whole_and_released_when_all_are(self):
        for kind in sorted(self.kinds()):
            needs = sb.triage_inputs(kind)
            for bad in needs:
                with self.subTest(kind=kind, bad=bad):
                    self.assertTrue(sb.triage_digest_keep(self.marks(needs, bad))(kind, "acme/api#1"))   # that one input alone is not whole
            with self.subTest(kind=kind, all="whole"):
                self.assertFalse(sb.triage_digest_keep(self.marks(needs))(kind, "acme/api#1"))

    def test_the_prune_keeps_the_records_of_a_rule_the_table_does_not_know(self):
        keep = sb.triage_digest_keep(self.marks(("agents", "issues")))
        self.assertFalse(keep("T2", "acme/api#1"))
        with mock.patch.dict(sb.TRIAGE_RULE_INPUTS, {}, clear=True):
            self.assertTrue(keep("T2", "acme/api#1"))                                # unknown: kept


class TestStatusIntentGapsAreBounded(unittest.TestCase):
    """#86: the intent-gap check in `status` runs its reads in parallel, within one time budget."""

    class Reader:
        def __init__(self, delays):
            self.delays = delays
            self.seen = []

        def missing_intent(self, number):
            self.seen.append(number)
            time.sleep(self.delays.get(number, 0))
            if number == 13:
                raise sb.IntentError("gh", "boom")
            return [number] if number % 2 else []

    def report(self, delays, budget, numbers):
        engine = types.SimpleNamespace(intents=self.Reader(delays))
        with mock.patch.object(sb, "referenced_issues", return_value=numbers):
            return sb.intent_gap_report(engine, None, [], budget=budget)

    def test_every_issue_is_read_and_errors_are_kept(self):
        got = self.report({}, 5, [1, 2, 3, 13])
        self.assertEqual((got["intent_gaps"], got["intent_errors"]), ([1, 3], ["#13: gh: boom"]))
        self.assertNotIn("intent_unchecked", got)

    def test_the_reads_overlap(self):
        started = time.time()
        got = self.report(dict.fromkeys(range(1, 9), 0.3), 5, list(range(1, 9)))
        self.assertLess(time.time() - started, 1.2, "eight 0.3 s reads in parallel, not 2.4 s serial")
        self.assertEqual(got["intent_gaps"], [1, 3, 5, 7])

    def test_a_read_past_the_budget_is_counted_not_waited_for(self):
        started = time.time()
        got = self.report({2: 3.0}, 0.3, [1, 2, 3])
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(got["intent_unchecked"], 1)
        self.assertEqual(got["intent_gaps"], [1, 3])
        lines = sb.intent_gap_lines(got)
        self.assertTrue(any("not checked" in l and "partial" in l for l in lines), lines)

    def test_no_reader_is_an_empty_report(self):
        self.assertEqual(sb.intent_gap_report(types.SimpleNamespace(intents=None), None, []), {})


class TestPlanCheckHash(unittest.TestCase):
    """#364: a `#` inside a clause or scenario field is text; only `#` then a space (or the end) starts an inline comment."""

    def block(self, lines):
        return "```yaml\nclauses:\n%s```\n" % "".join("  %s\n" % l for l in lines)

    def test_a_hash_with_digits_is_kept_in_a_clause_quoted_or_not(self):
        for first in ("see #348", '"see #348"', "'see #348'"):
            self.assertEqual(sb.plan_check.declared_clauses(self.block(["DW-1: " + first, "DW-2: other"])), ["see #348", "other"], first)

    def test_a_hash_with_digits_is_kept_in_scenario_fields_quoted_or_not(self):
        for value in ("exits 1 for issue #12", '"exits 1 for issue #12"'):
            text = ("scenarios:\n  - id: S1\n    covers: DW-1\n    text: Given issue #12 is open, when run, then it is shown\n    proof: test\n"
                    "    pass: %s\n    discriminates: a build that cuts at #12 reads a shorter text\n    evaluator: ci\n" % value)
            (scenario,) = sb.plan_check.parse_scenarios(text.splitlines())
            self.assertEqual(scenario["pass"], "exits 1 for issue #12")
            self.assertEqual(scenario["text"], "Given issue #12 is open, when run, then it is shown")
            self.assertEqual(scenario["discriminates"], "a build that cuts at #12 reads a shorter text")

    def test_a_comment_is_still_a_comment(self):
        for value in ("alpha # a comment", "alpha #", "alpha   #  more"):
            self.assertEqual(sb.plan_check.declared_clauses(self.block(["DW-1: " + value])), ["alpha"], value)
        self.assertEqual(sb.plan_check.declared_clauses(self.block(["# a comment line", "DW-1: alpha"])), ["alpha"])

    def test_a_mismatch_names_the_first_differing_clause_and_what_was_declared(self):
        done = "(1) plans are recorded (2) alignment is reviewed (3) the plan is approved"
        text = ("```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: Given a thing, when run, then it behaves\n    proof: test\n"
                "    pass: the call returns status 200 and exactly 1 items\n    discriminates: a stub that returns an empty list fails the count\n"
                "    evaluator: ci\n```\n" + self.block(["DW-1: plans are recorded", "DW-2: alignment is see", "DW-3: the plan is approved"]))
        messages = [d["message"] for d in sb.plan_check.check(text, done)["defects"] if d["code"] == "declared-clauses-mismatch"]
        self.assertEqual(len(messages), 1)
        self.assertIn("DW-2 differs: declared 'alignment is see'", messages[0])
        self.assertIn("alignment is reviewed", messages[0])
        one = "plans are recorded, alignment is reviewed, the plan is approved"
        text2 = text.split("```yaml\nclauses:")[0] + self.block(["DW-1: plans are recorded", "DW-2: alignment is see", "DW-3: the plan is approved"])
        messages = [d["message"] for d in sb.plan_check.check(text2, one)["defects"] if d["code"] == "declared-clauses-mismatch"]
        self.assertIn("DW-2 does not continue the ask: declared 'alignment is see'", messages[0])


class TestDecisionIds(unittest.TestCase):
    def item(self, seed, ask=None, agent="coder", bare=False, since=EPOCH):
        return {"seed": seed, "ask": ask, "agent": agent, "bare": bare, "since": since}

    def test_an_issues_own_label_is_the_bare_issue_number(self):
        (found,) = sb.assign_decision_ids([self.item("gh:acme/x#5", ask=5, bare=True)])
        self.assertEqual(found, "#5")

    def test_other_decisions_on_an_issue_get_a_short_letter_suffix_the_same_every_time(self):
        first = sb.assign_decision_ids([self.item("per:ses_1:a", ask=2)])
        again = sb.assign_decision_ids([self.item("per:ses_1:a", ask=2)])
        self.assertEqual(first, again)
        self.assertRegex(first[0], r"^#2[a-z]{2}$")

    def test_a_decision_with_no_issue_is_named_for_its_agent_and_a_number(self):
        (found,) = sb.assign_decision_ids([self.item("per:ses_1:a", agent="platform")])
        self.assertRegex(found, r"^platform-[0-9]{3}$")

    def test_an_id_does_not_change_when_another_decision_on_the_issue_goes_away(self):
        one, two = self.item("per:ses_1:a", ask=2), self.item("per:ses_1:b", ask=2, since=EPOCH + 1)
        both = sb.assign_decision_ids([one, two])
        alone = sb.assign_decision_ids([two])
        self.assertEqual(both[1], alone[0])
        self.assertNotEqual(both[0], both[1])

    def test_two_open_decisions_never_share_an_id_even_when_their_hashes_collide(self):
        items = [self.item("seed-%d" % n, ask=2, since=EPOCH + n) for n in range(300)]
        ids = sb.assign_decision_ids(items)
        self.assertEqual(len(set(ids)), 300)

    def test_the_same_issue_number_in_two_repos_is_not_one_id(self):
        ids = sb.assign_decision_ids([self.item("gh:a/x#5", ask=5, bare=True), self.item("gh:b/x#5", ask=5, bare=True,
                                                                                    since=EPOCH + 1)])
        self.assertEqual(ids[0], "#5")
        self.assertNotEqual(ids[1], "#5")


class TestDecisionsPluginSource(unittest.TestCase):
    def code(self, name):
        text = (DECISIONS_PLUGIN / name).read_text()
        return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())

    def test_the_plugin_has_the_shape_the_spike_verified(self):
        text = self.code("tui.tsx")
        self.assertIn("@opentui/solid", (DECISIONS_PLUGIN / "tui.tsx").read_text().splitlines()[0])
        self.assertIn("export default {", text)
        self.assertIn('id: "fleet.decisions"', text)
        self.assertIn("setup(api", text)
        self.assertIn('append: "sidebar.content"', text)
        self.assertEqual(json.loads((DECISIONS_PLUGIN / "package.json").read_text())["type"], "module")

    def test_it_watches_the_directory_and_never_the_file_because_the_file_is_replaced_by_rename(self):
        text = self.code("tui.tsx")
        self.assertRegex(text, r"watch\(dir,")
        self.assertNotIn("DECISIONS_FILE", text)
        self.assertIn("setInterval(refresh, SAFETY_MS)", text)

    def test_it_is_read_only_holds_no_credential_spawns_nothing_and_calls_nothing_over_the_network(self):
        for name in ("tui.tsx", "decisions.mjs", "health.mjs"):
            # the one link host the panel may name (https://github.com, a row's issue or PR) and the one mouse
            # handler it may have (onMouseUp on a row: copy its link) are the only exceptions; nothing else changes
            code = self.code(name).lower().replace("https:", "").replace("onmouseup", "")
            for needle in ("child_process", "spawn", "exec(", "execsync", "fetch(", "http", "net\"", "api.client",
                           "writefile", "token", "secret", "password", "openrouter", "onmouse", "dialog", "keymap",
                           "insertText".lower()):
                self.assertNotIn(needle, code, "%s: %s" % (name, needle))

    def test_the_files_carry_no_personalisation(self):
        pattern = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/", "direc" + "tor(?!y|ies)"]),
                             re.IGNORECASE)
        for path in sorted(DECISIONS_PLUGIN.iterdir()):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(pattern.search(line), "%s:%d %s" % (path.name, number, line))


class TestDecisionsInTheLabTier(Base):
    """The Lab runner reads the list only at the end of a play, and says so for any other time."""

    def run_of(self, steps_end="6m"):
        lab = _load("t_proof_switchboard_14", BIN / "proof-switchboard")
        scenario = {"steps": [{"at": "10s"}, {"at": steps_end}]}
        engine = types.SimpleNamespace(env=lambda: dict(os.environ))
        fake = types.SimpleNamespace(scenario=scenario, engine=engine,
                                     seconds=lambda text: sc.duration_seconds(text))
        return lab, fake

    def test_it_runs_the_command_on_the_engines_state_when_the_play_ends(self):
        lab, fake = self.run_of()
        done = subprocess.CompletedProcess([], 0, "No decisions are waiting on you.\n", "")
        with mock.patch.object(lab.subprocess, "run", return_value=done) as ran:
            ok, seen = lab.ScenarioRun.check_decisions_say(fake, {"at": "6m", "text": "No decisions are waiting"})
        self.assertTrue(ok, seen)
        self.assertEqual(ran.call_args[0][0][-1], "decisions")
        with mock.patch.object(lab.subprocess, "run", return_value=done):
            self.assertFalse(lab.ScenarioRun.check_decisions_say(fake, {"at": "6m", "text": "not updating"})[0])

    def test_a_time_before_the_end_is_refused_not_guessed(self):
        lab, fake = self.run_of()
        with mock.patch.object(lab.subprocess, "run") as ran:
            ok, seen = lab.ScenarioRun.check_decisions_say(fake, {"at": "30s", "text": "x"})
        self.assertFalse(ok)
        self.assertIn("only when the play ends", seen)
        ran.assert_not_called()

    def test_r3_carries_the_expectation_and_the_lab_tier_still_plays_it(self):
        lab = _load("t_proof_switchboard_14b", BIN / "proof-switchboard")
        r3 = sc.load(sc.DEFAULT_DIR / "r3-state-dir-deleted.json")
        self.assertIn("decisions_say", [e["that"] for e in r3["expect"]])
        self.assertEqual(r3["control"]["fails"], ["worker-once"])
        self.assertEqual([r for r in lab.unsupported(r3, sc) if "decisions" in r], [])


class TestDecisionsScenarios(Base):
    def test_the_pr14_catalog_is_present_with_a_control_each(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        for sid, fault_name in (("D2", None), ("D3", "decision-resolves-never"), ("D4", "no-decisions-file"),
                                ("D5", "stale-as-fresh")):
            with self.subTest(scenario=sid):
                self.assertIn(sid, found)
                self.assertEqual(found[sid]["since_pr"], 14)
                self.assertIn("offline", found[sid]["tiers"])
                if fault_name:
                    self.assertEqual(found[sid]["control"].get("fault"), fault_name)
                else:
                    self.assertIn("control", found[sid])

    def test_every_new_fault_is_known_and_off_outside_the_lab(self):
        for name in ("no-decisions-file", "stale-as-fresh", "decision-resolves-never"):
            with self.subTest(fault=name):
                self.assertIn(name, sb.KNOWN_FAULTS)
                os.environ[sb.FAULT_ENV] = name
                os.environ.pop(sb.LAB_ENV, None)
                self.assertFalse(sb.fault(name))
                os.environ[sb.LAB_ENV] = "1"
                self.assertTrue(sb.fault(name))

    def test_the_vocabulary_has_the_decision_steps_and_oracles(self):
        self.assertEqual(sorted(sc.STEP_FIELDS["awaiting"]), ["issue", "label", "on", "repo", "title"])  # PR 18: label
        self.assertEqual(sorted(sc.STEP_FIELDS["send"]), ["issue", "text", "to"])
        self.assertEqual(sc.STEP_FIELDS["delete-decisions"], {})
        self.assertEqual(sorted(sc.EXPECT_FIELDS["decisions"]), ["as", "at", "count", "text", "tier"])  # PR 18
        self.assertEqual(sorted(sc.EXPECT_FIELDS["decisions_say"]), ["at", "text"])


class TestMultiRepoRefs(Base):
    def test_a_bare_number_stays_an_int_and_a_qualified_one_names_its_repo(self):
        bare, qualified = sb.parse_issue_ref("7"), sb.parse_issue_ref("acme/api#7")
        self.assertEqual((int(bare), sb.issue_repo(bare)), (7, None))
        self.assertEqual((int(qualified), sb.issue_repo(qualified)), (7, "acme/api"))
        self.assertEqual(qualified + 1, 8)
        self.assertEqual(json.dumps({"n": qualified}), '{"n": 7}')

    def test_nonsense_is_refused_with_the_accepted_forms(self):
        for text in ("", "0", "-1", "acme#2", "acme/api#", "acme/api/x#2", "a b#2", "#"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    sb.parse_issue_ref(text)
                self.assertIn("owner/repo#N", str(caught.exception))

    def test_the_cli_takes_a_qualified_reference_everywhere_an_issue_goes(self):
        parser = sb.build_parser()
        for argv in (["send", "bob", "hi", "--issue", "acme/api#2"], ["report", "done", "ok", "--issue", "acme/api#2"],
                     ["intent", "acme/api#2"], ["handoff", "--issue", "acme/api#2", "--brief-file", "-"],
                     ["remind", "bob", "5m", "hi", "--issue", "acme/api#2"],
                     ["comment", "--issue", "acme/api#2", "--body-file", "-"]):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(sb.issue_repo(args.issue), "acme/api")
                self.assertEqual(int(args.issue), 2)

    def test_a_tag_is_the_repo_name_and_the_owner_when_two_repos_share_a_name(self):
        self.assertEqual(sb.issue_tag("acme/api", 2, ["acme/api", "acme/web"]), "api#2")
        self.assertEqual(sb.issue_tag("acme/api", 2, ["acme/api", "other/api"]), "acme/api#2")
        self.assertEqual(sb.issue_tag(None, 2, ["acme/api", "acme/web"]), "#2")


class TestMultiRepoResolution(Base):
    def cfg(self, **raw):
        return sb.validate_config(raw)

    def test_a_qualifier_wins(self):
        config = self.cfg(intent_repo="acme/api", github_repos=["acme/api", "acme/web"])
        self.assertEqual(sb.resolve_issue(config, sb.parse_issue_ref("acme/web#3"), {"repo": "acme/api"},
                                          ["acme/api", "acme/web"]), ("acme/web", 3))

    def test_a_bare_number_is_the_callers_repo_then_the_intent_repo(self):
        config = self.cfg(intent_repo="acme/api")
        self.assertEqual(sb.resolve_issue(config, 3, {"repo": "acme/web"}, ["acme/api", "acme/web"]), ("acme/web", 3))
        self.assertEqual(sb.resolve_issue(config, 3, {}, ["acme/api"]), ("acme/api", 3))
        self.assertEqual(sb.resolve_issue(self.cfg(), 3, {}, []), (None, 3))
        self.assertEqual(sb.resolve_issue(config, None, {}, []), (None, None))

    def test_a_bare_number_from_a_caller_with_no_repo_is_refused_when_two_repos_are_watched(self):
        config = self.cfg(intent_repo="acme/api")
        with self.assertRaises(sb.IssueRefError) as caught:
            sb.resolve_issue(config, 3, {"name": "cos"}, ["acme/api", "acme/web"])
        self.assertIn("acme/api#3", str(caught.exception))
        self.assertIn("acme/web#3", str(caught.exception))

    def test_the_bare_issue_number_fault_ignores_the_qualifier(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "bare-issue-number"
        config = self.cfg(intent_repo="acme/api")
        self.assertEqual(sb.resolve_issue(config, sb.parse_issue_ref("acme/web#3"), {"repo": "acme/api"},
                                          ["acme/api", "acme/web"]), ("acme/api", 3))


@unittest.skipUnless(NODE, "node is not installed: the decisions plugin tests need it (skipped, not passed)")
class TestDecisionsPluginShowsTheRepoTag(Base):
    def test_a_decision_starts_with_the_tag_and_no_line_is_wider_than_a_row(self):
        script = self.home / "tag-harness.mjs"
        script.write_text("""import { entryOf } from "%s";
const now = Date.now();
const d = { id: "acme/site#2ab", repo: "acme/site", title: "x".repeat(80), since: new Date(now - 90000).toISOString() };
console.log(JSON.stringify([entryOf(d, now), entryOf({ ...d, id: "api#2", repo: "acme/api" }, now)]));
""" % (DECISIONS_PLUGIN / "decisions.mjs").resolve().as_uri())
        done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        long_e, short_e = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(long_e["head"], "❔ acme/site#2ab 1m")
        self.assertEqual(short_e["head"], "❔ api#2 1m")
        self.assertEqual(len(long_e["lines"]), 2)  # two lines at most, the second ending in an ellipsis
        self.assertTrue(long_e["lines"][1].endswith("\u2026"))
        self.assertTrue(all(len(x) <= 34 for x in long_e["lines"]))

    def test_the_view_counts_decisions_and_keys_entries_by_the_tagged_id(self):
        script = self.home / "tag-view.mjs"
        script.write_text("""import { buildView } from "%s";
const now = Date.now();
const mk = (id, repo) => ({ id, repo, title: "t", since: new Date(now).toISOString() });
console.log(JSON.stringify(buildView({ status: "ok", errors: [], decisions: [mk("api#2", "a/api"), mk("web#2", "a/web")] }, now)));
""" % (DECISIONS_PLUGIN / "decisions.mjs").resolve().as_uri())
        done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        view = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual([e["key"] for g in view["groups"] for e in g["entries"]], ["api#2", "web#2"])


class TestTheTrialSharesTheInstaller(Base):
    """One path: `fleet-switchboard install` and `switchboard-trial` put the same pieces into a profile by the same functions."""

    def trial(self):
        return TRIAL.Trial(root=self.home / "trial")

    def test_the_trial_s_skills_agents_and_plugins_come_from_the_shared_functions(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        with mock.patch.object(trial.sb, "install_skills", return_value=True) as skills, \
                mock.patch.object(trial.sb, "install_decisions_plugin", return_value=False) as plugin:
            trial.install_skills()
            self.assertEqual(trial.install_decisions_plugin(), "unchanged")
        skills.assert_called_once_with(trial.skills_dir(), TRIAL.REPO_ROOT)
        plugin.assert_called_once_with(trial.lab.opencode_config, TRIAL.REPO_ROOT)

    def test_the_trial_has_no_installer_of_its_own(self):
        source = (BIN / "switchboard-trial").read_text()
        for gone in ("agent_copy(", "fleet-hooks.js", "def read_cli_json", "DECISIONS_PLUGIN_FILES", "fleet-decisions-tui"):
            self.assertNotIn(gone, source)
        for shared in ("sb.install_agents", "sb.install_skills", "sb.install_hooks_plugin", "sb.install_decisions_plugin",
                       "sb.decisions_plugin_status"):
            self.assertIn(shared, source)

    def test_both_write_the_same_files_into_a_profile(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        os.makedirs(trial.lab.opencode_config)
        trial.guarded(trial.sb.install_agents, trial.lab.opencode_config, TRIAL.DEFAULT_MODEL, TRIAL.REPO_ROOT)
        trial.guarded(trial.sb.install_hooks_plugin, trial.lab.opencode_config, TRIAL.REPO_ROOT)
        trial.install_skills()
        self.assertEqual(trial.install_decisions_plugin(), "installed")
        other = self.home / "other-profile"
        sb.install_agents(other, sb.STANDARD_MODEL, REPO)
        sb.install_hooks_plugin(other, REPO)
        sb.install_decisions_plugin(other, REPO)
        for relative in ("agents/coder.md", "plugins/fleet-hooks.js", "fleet-decisions/tui.tsx", "cli.json"):
            self.assertEqual((Path(trial.lab.opencode_config) / relative).read_text(), (other / relative).read_text(), relative)

    def test_the_trial_refuses_a_cli_json_it_does_not_understand_as_before(self):
        trial = self.trial()
        os.makedirs(trial.lab.opencode_config)
        (Path(trial.lab.opencode_config) / "cli.json").write_text("[1, 2]")
        with self.assertRaises(TRIAL.TrialError):
            trial.install_decisions_plugin()
        self.assertEqual((Path(trial.lab.opencode_config) / "cli.json").read_text(), "[1, 2]")

    def test_the_constants_the_two_drivers_share_agree(self):
        self.assertEqual(TRIAL.DEFAULT_MODEL, sb.STANDARD_MODEL)
        proof = _load("t_proof_switchboard_pr16", BIN / "proof-switchboard")
        self.assertEqual((proof.V2_PACKAGE, proof.V2_VERSION), (sb.V2_PACKAGE, sb.V2_VERSION))


class TestParseOrders(unittest.TestCase):
    def test_the_section_is_one_order_per_bullet(self):
        body = ("```yaml\norchestrator: platform\n```\n\nThe charter.\n\n## Standing orders\n\n"
                "- approve deploys in this repo (ordered by alice, 2027-01-05)\n"
                "* self-certify (ordered by alice, 2027-01-06)\n\n## Notes\n- not an order\n")
        self.assertEqual(sb.parse_orders(body), ["approve deploys in this repo (ordered by alice, 2027-01-05)",
                                                  "self-certify (ordered by alice, 2027-01-06)"])

    def test_the_legacy_line_counts_as_one_more_order(self):
        body = "## Standing orders\n- one (ordered by alice, 2027-01-05)\n\nStanding authority: may merge green PRs\n"
        self.assertEqual(sb.parse_orders(body), ["one (ordered by alice, 2027-01-05)", "may merge green PRs"])
        self.assertEqual(sb.parse_orders("Standing authority: may merge\n"), ["may merge"])

    def test_nothing_in_a_fenced_block_counts(self):
        for body in ("```\n## Standing orders\n- hidden\n```\n", "~~~\n## Standing orders\n- hidden\n~~~\n",
                     "## Standing orders\n```\n- hidden\n```\n"):
            with self.subTest(body=body):
                self.assertEqual(sb.parse_orders(body), [])

    def test_absent_empty_or_none_is_no_orders(self):
        for body in (None, "", "no section", "## Standing orders\n\nnothing here\n", "Standing authority: none"):
            with self.subTest(body=body):
                self.assertEqual(sb.parse_orders(body), [])

    def test_heading_levels_case_and_crlf(self):
        self.assertEqual(sb.parse_orders("### standing ORDERS  \r\n- a\r\n- b\r\n"), ["a", "b"])

    def test_the_old_function_still_answers_through_the_one_parser(self):
        self.assertEqual(sb.parse_standing_authority("## Standing orders\n- a\n- b\n"), "a; b")
        self.assertEqual(sb.parse_standing_authority("Standing authority: may merge\n"), "may merge")
        self.assertIsNone(sb.parse_standing_authority("nothing"))


class TestOrdersBodyEdits(unittest.TestCase):
    def test_add_creates_the_section_at_the_end_and_keeps_every_other_byte(self):
        new = sb.add_order_to_body(ORDERS_CHARTER_BODY, "approve deploys", "alice", "2027-01-05")
        self.assertTrue(new.startswith(ORDERS_CHARTER_BODY))
        self.assertEqual(new[len(ORDERS_CHARTER_BODY):],
                         "\n## Standing orders\n\n- approve deploys (ordered by alice, 2027-01-05)\n")

    def test_add_appends_to_an_existing_section(self):
        body = "Intro\n\n## Standing orders\n\n- one (ordered by alice, 2027-01-01)\n\n## Notes\nkept\n"
        new = sb.add_order_to_body(body, "two", "bob", "2027-01-02")
        self.assertEqual(new, "Intro\n\n## Standing orders\n\n- one (ordered by alice, 2027-01-01)\n"
                              "- two (ordered by bob, 2027-01-02)\n\n## Notes\nkept\n")

    def test_remove_edits_only_the_section_and_the_last_one_deletes_it(self):
        added = sb.add_order_to_body(ORDERS_CHARTER_BODY, "a", "alice", "2027-01-05")
        added = sb.add_order_to_body(added, "b", "alice", "2027-01-05")
        one = sb.remove_order_from_body(added, 1)
        self.assertEqual(sb.parse_orders(one), ["b (ordered by alice, 2027-01-05)"])
        self.assertTrue(one.startswith(ORDERS_CHARTER_BODY))
        self.assertEqual(sb.remove_order_from_body(one, 1), ORDERS_CHARTER_BODY)

    def test_remove_keeps_what_follows_the_section(self):
        body = "Intro\n\n## Standing orders\n- only (ordered by alice, 2027-01-01)\n\n## Notes\nkept\n"
        self.assertEqual(sb.remove_order_from_body(body, 1), "Intro\n\n## Notes\nkept\n")

    def test_a_crlf_body_gets_crlf_lines(self):
        new = sb.add_order_to_body("Intro\r\n", "a", "alice", "2027-01-05")
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_the_legacy_line_is_numbered_but_cannot_be_removed(self):
        body = "## Standing orders\n- a (ordered by alice, 2027-01-01)\n\nStanding authority: may merge\n"
        with self.assertRaises(sb.OrdersError) as raised:
            sb.remove_order_from_body(body, 2)
        self.assertIn("Standing authority", str(raised.exception))
        with self.assertRaises(sb.OrdersError):
            sb.remove_order_from_body(body, 3)

    def test_the_text_is_one_line_of_one_to_300_characters(self):
        for text in ("", "   ", "a\nb", "x" * 301):
            with self.subTest(text=text[:20]):
                with self.assertRaises(sb.OrdersError):
                    sb.add_order_to_body(ORDERS_CHARTER_BODY, text, "alice", "2027-01-05")
        sb.add_order_to_body(ORDERS_CHARTER_BODY, "x" * 300, "alice", "2027-01-05")

    def test_a_charter_carries_any_number_of_orders(self):
        body = ORDERS_CHARTER_BODY
        for n in range(32):
            body = sb.add_order_to_body(body, "order %d" % n, "alice", "2027-01-05")
        self.assertEqual(body.count("(ordered by alice, 2027-01-05)"), 32)


class TestCharterEditGuard(unittest.TestCase):
    def hits(self, command, charters=(1,)):
        return sb.charter_edit_target(command, set(charters))

    def test_gh_issue_edit_of_a_charter(self):
        for command in ("gh issue edit 1 --body-file x", "gh issue edit #1 --add-label a",
                        "gh issue edit https://github.com/acme/api/issues/1 --title t", "gh -R acme/api issue edit 1 -b x",
                        "echo hi && gh issue edit 1 -b x", "bash -c 'gh issue edit 1 -b x'", "GH issue edit 1"[:0] + "gh  issue   edit  1"):
            with self.subTest(command=command):
                self.assertEqual(self.hits(command), 1)

    def test_gh_api_patch_put_delete_of_a_charter(self):
        for command in ("gh api repos/acme/api/issues/1 --method PATCH --input body.json",
                        "gh api repos/acme/api/issues/1 -X PUT -f state=closed",
                        "gh api -X DELETE repos/acme/api/issues/1", "gh api repos/acme/api/issues/1 -XPATCH",
                        "gh api repos/acme/api/issues/1 --method=patch -f body=x"):
            with self.subTest(command=command):
                self.assertEqual(self.hits(command), 1)

    def test_what_is_not_an_edit_of_a_charter(self):
        for command in ("gh issue edit 2 --body x", "gh issue view 1", "gh issue comment 1 -b hi",
                        "gh api repos/acme/api/issues/1", "gh api repos/acme/api/issues/1/comments --method POST",
                        "gh api repos/acme/api/issues/12 --method PATCH", "gh pr edit 1 --title t",
                        "git log --oneline", "echo gh issue edit 1"[:0] + "ls"):
            with self.subTest(command=command):
                self.assertIsNone(self.hits(command))

    def test_no_known_charter_is_no_hit(self):
        self.assertIsNone(self.hits("gh issue edit 1 -b x", charters=()))


class TestOrdersFact(unittest.TestCase):
    def test_a_fact_has_an_orders_attribute_false_by_default(self):
        self.assertFalse(fact("k").orders)
        self.assertTrue(sb.Fact("orders:1:ab:start", "p", "orders", "x", EPOCH, orders=True).orders)
        self.assertTrue(sb.Fact.from_json(sb.Fact("orders:1:ab:start", "p", "orders", "x", EPOCH, orders=True).to_json()).orders)
        with self.assertRaises(sb.FactError):
            sb.Fact("k", "p", "orders", "x", EPOCH, orders="yes")

    def orders_fact(self, created=EPOCH, **extra):
        return sb.Fact("orders:1:abcd1234:start", "platform", "orders", "approve deploys\nself-certify", created,
                       issue=1, orders=True, **extra)

    def test_decide_notes_orders_at_once_even_to_an_idle_agent_never_waking(self):
        for human in (None, EPOCH - 10):
            with self.subTest(human=human):
                plan = sb.decide("platform", [self.orders_fact()], observation(human=human), EPOCH, 300, 600)
                self.assertEqual(plan.action, "note")  # not batched, not a wake

    def test_under_the_orders_wake_fault_it_wakes(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "orders-wake"
        self.addCleanup(os.environ.pop, sb.FAULT_ENV, None)
        self.addCleanup(os.environ.pop, sb.LAB_ENV, None)
        self.assertEqual(sb.decide("platform", [self.orders_fact()], observation(), EPOCH, 300, 600).action, "wake")

    def test_orders_go_alone_and_the_others_follow_by_their_own_rule(self):
        other = fact("send:1", created=EPOCH, batch=False)
        plan = sb.decide("platform", [other, self.orders_fact()], observation(), EPOCH, 300, 600)
        self.assertEqual((plan.action, [f.key for f in plan.pending]), ("note", ["orders:1:abcd1234:start"]))

    def test_held_agent_holds_its_orders(self):
        plan = sb.decide("platform", [self.orders_fact()], observation(status="blocked"), EPOCH, 300, 600)
        self.assertEqual(plan.action, "hold")

    def test_compose_renders_each_order_on_its_own_line_and_the_closing_line(self):
        text, carried = sb.compose("platform", [self.orders_fact()])
        self.assertEqual(text.splitlines(), [
            "[switchboard] standing orders for platform (charter #1):",
            "- approve deploys", "- self-certify",
            "These are the owner's standing instructions for this charter. Act on them without asking again, "
            "within their words. They cover nothing they do not name."])
        self.assertEqual(len(carried), 1)

    def test_full_length_orders_are_not_cut(self):
        orders = "\n".join("%d %s" % (n, "x" * 290) for n in range(20))
        text, _ = sb.compose("platform", [sb.Fact("orders:1:ab:start", "platform", "orders", orders, EPOCH, issue=1,
                                                  orders=True)])
        self.assertIn("19 " + "x" * 290, text)
        self.assertTrue(text.endswith("They cover nothing they do not name."))

    def test_the_description_names_the_orders(self):
        self.assertEqual(sb.describe_items("platform", [self.orders_fact()]), "switchboard: standing orders for platform")


class TestCompactionShape(unittest.TestCase):
    """The shapes below were read from a real v2 2.0.22 in the Lab (docs/switchboard.md, Lab proof), ids shortened."""

    RUNNING = {"type": "compaction", "id": "msg_c3", "time": {"created": 1791301788691}, "status": "running",
               "reason": "manual", "summary": "## Objective\n- probe\n", "recent": "[User]: turn 7"}
    COMPLETED = dict(RUNNING, id="msg_c2", status="completed", model={"id": "lab", "providerID": "scripted"},
                     cost=0, tokens={"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}})
    FAILED = {"type": "compaction", "id": "msg_c1", "time": {"created": 1791301658157}, "status": "failed",
              "reason": "manual", "error": {"type": "compaction.unavailable", "message": "Nothing to compact yet"},
              "cost": 0, "tokens": {"input": 20, "output": 10}}
    IDLE = {"type": "idle", "id": "msg_i", "time": {"created": 1791301788000}, "outcome": "success"}

    def test_the_newest_completed_compaction_names_the_key_newest_first(self):
        self.assertEqual(sb.completed_compaction([self.RUNNING, self.COMPLETED, self.FAILED, self.IDLE]),
                         ("msg_c2", 1791301788.691))
        self.assertEqual(sb.completed_compaction([self.FAILED, self.RUNNING, self.IDLE]), (None, None))
        self.assertEqual(sb.completed_compaction([]), (None, None))
        self.assertEqual(sb.completed_compaction(None), (None, None))

    def test_a_compaction_keeps_its_id_from_running_to_completed(self):
        self.assertEqual(sb.completed_compaction([dict(self.RUNNING, status="completed")])[0], "msg_c3")

    def test_the_inbox_item_the_real_v2_returns_for_a_note_is_recognised(self):
        item = {"id": "msg_n", "sessionID": "ses_x", "time": {"created": 1}, "type": "synthetic", "delivery": "steer",
                "payload": {"text": "t", "metadata": {"fleet": {"keys": ["orders:1:612ab2c6:start"], "from": "switchboard"}}}}
        self.assertTrue(sb._is_orders_note(item))
        other = dict(item, payload={"text": "t", "metadata": {"fleet": {"keys": ["send:a:1"], "from": "switchboard"}}})
        self.assertFalse(sb._is_orders_note(other))
        self.assertFalse(sb._is_orders_note({"type": "compaction", "payload": {}, "delivery": "steer"}))


class TestOrdersRoleText(unittest.TestCase):
    def text(self, *parts):
        return (REPO.joinpath(*parts)).read_text()

    def test_orchestrator_and_coder_know_what_the_note_is_and_is_not(self):
        for role in ("orchestrator", "coder"):
            with self.subTest(role=role):
                text = " ".join(self.text("agents", "opencode", role + ".md").split())
                self.assertIn("`[switchboard] standing orders` note is the owner's standing instruction", text)
                self.assertIn("without asking again, within its words", text)
                self.assertIn("covers nothing it does not name", text)
                self.assertIn("Never edit it", text)
                self.assertIn("Chief of Staff or the owner records orders", text)

    def test_the_chief_of_staff_records_reads_back_and_never_widens(self):
        text = " ".join(self.text("agents", "opencode", "chief-of-staff.md").split())
        self.assertIn('`fleet-switchboard orders add --charter <owner/repo#n> "<their words>"`', text)
        self.assertIn("read the list back", text)
        self.assertIn("ask nothing it already answers", text)
        self.assertIn("`orders remove`", text)
        self.assertIn("Never invent or widen an order", text)

    def test_the_skills_say_the_format_and_who_writes(self):
        charter = " ".join(self.text("skills", "fleet-charter", "SKILL.md").split())
        self.assertIn("## Standing orders", charter)
        self.assertIn("- <text> (ordered by <name>, <YYYY-MM-DD>)", charter)
        self.assertIn("Only the Chief of Staff records them", charter)
        coordination = " ".join(self.text("skills", "fleet-coordination", "SKILL.md").split())
        self.assertIn("standing orders", coordination)
        self.assertIn("covers nothing it does not name", coordination)


class TestOrdersScenarios(Base):
    def test_the_pr17_catalog_is_present_with_a_control_each(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        for sid, fault_name in (("O1", "orders-wake"), ("O2", "orders-start-only"), ("O3", "orders-never-judged"),
                                ("O4", "anyone-can-order"), ("O5", "one-account")):
            with self.subTest(scenario=sid):
                self.assertIn(sid, found)
                self.assertEqual(found[sid]["since_pr"], 17)
                self.assertEqual(found[sid]["tiers"], ["offline"])
                self.assertEqual(found[sid]["control"].get("fault"), fault_name)

    def test_every_new_fault_is_known_and_off_outside_the_lab(self):
        for name in ("orders-start-only", "orders-wake", "anyone-can-order", "orders-never-judged"):
            with self.subTest(fault=name):
                self.assertIn(name, sb.KNOWN_FAULTS)
                os.environ[sb.FAULT_ENV] = name
                os.environ.pop(sb.LAB_ENV, None)
                self.assertFalse(sb.fault(name))
                os.environ[sb.LAB_ENV] = "1"
                self.assertTrue(sb.fault(name))

    def test_the_vocabulary_has_the_orders_steps_and_oracles(self):
        self.assertEqual(sorted(sc.STEP_FIELDS["orders"]), ["action", "charter", "number", "text"])
        self.assertEqual(sorted(sc.STEP_FIELDS["edit-orders"]), ["charter", "text"])
        self.assertEqual(sorted(sc.STEP_FIELDS["compact"]), ["agent", "status"])
        self.assertEqual(sorted(sc.EXPECT_FIELDS["orders_on_github"]), ["charter", "count", "text"])
        self.assertEqual(sorted(sc.EXPECT_FIELDS["orders_refused"]), ["agent", "count"])


class TestTierConfig(Base):
    def test_the_new_keys_have_documented_defaults(self):
        config = sb.validate_config({})
        self.assertEqual((config["awaiting_cos_label"], config["decisions_cos_grace_seconds"], config["cos_orders_file"]),
                         (None, 300, None))

    def test_the_cos_label_defaults_to_the_os_users_awaiting_cos(self):
        with mock.patch.object(sb.getpass, "getuser", return_value="osuser"):
            self.assertEqual(sb.awaiting_cos_label({"awaiting_cos_label": None}), "osuser:awaiting-cos")
        self.assertEqual(sb.awaiting_cos_label({"awaiting_cos_label": "x:cos"}), "x:cos")

    def test_good_values(self):
        config = sb.validate_config({"awaiting_cos_label": "x:cos", "decisions_cos_grace_seconds": 0,
                                     "cos_orders_file": "/etc/orders.md"})
        self.assertEqual((config["awaiting_cos_label"], config["decisions_cos_grace_seconds"], config["cos_orders_file"]),
                         ("x:cos", 0, "/etc/orders.md"))
        self.assertEqual(sb.validate_config({"decisions_cos_grace_seconds": 12.5})["decisions_cos_grace_seconds"], 12.5)

    def test_bad_values_are_refused_by_name(self):
        for key, value in (("awaiting_cos_label", ""), ("awaiting_cos_label", "a,b"), ("awaiting_cos_label", 3),
                           ("awaiting_cos_label", "x\ny"), ("decisions_cos_grace_seconds", -1),
                           ("decisions_cos_grace_seconds", "5"), ("decisions_cos_grace_seconds", True),
                           ("cos_orders_file", "orders.md"), ("cos_orders_file", 7), ("cos_orders_file", "")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config({key: value})
                self.assertIn(key, str(caught.exception))

    def test_the_two_labels_must_differ(self):
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"awaiting_label": "x:wait", "awaiting_cos_label": "x:wait"})


class TestServiceIdentity(unittest.TestCase):
    """#433 slice 4: the REAL fresh read of the service's process id (`V2Http.service_identity`), over a temp service file."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.dir.chmod(0o700)
        self.file = self.dir / "service.json"
        self.alive = {111: True, 222: True}
        self.http = sb.V2Http(self.file, 5, alive=lambda pid: self.alive.get(pid, False))

    def write(self, pid, mode=0o600, password="s3cret-value"):
        body = {"url": "http://127.0.0.1:49374", "password": password}
        if pid is not None:
            body["pid"] = pid
        self.file.write_text(json.dumps(body))
        self.file.chmod(mode)

    def test_a_changed_pid_is_seen_on_the_next_call_and_nothing_is_cached(self):
        self.write(111)
        self.assertEqual(self.http.service_identity(), 111)
        self.http.base_url()                                              # a cached connection record exists now
        self.write(222)
        self.assertEqual(self.http.service_identity(), 222, "a fresh read, not the cached one")

    def test_a_missing_unsafe_dead_or_odd_pid_gives_none_and_the_password_is_never_returned(self):
        self.assertIsNone(self.http.service_identity(), "no file")
        self.write(111, mode=0o644)
        self.assertIsNone(self.http.service_identity(), "a file others can read is not trusted")
        self.write(333)
        self.assertIsNone(self.http.service_identity(), "a pid that is gone")
        self.write(True)
        self.assertIsNone(self.http.service_identity(), "a bool is not a pid")
        self.write(None)
        self.assertIsNone(self.http.service_identity(), "no pid in the file")
        self.write(111)
        self.assertNotIn("s3cret", repr(self.http.service_identity()))
        self.dir.chmod(0o777)
        self.assertIsNone(self.http.service_identity(), "a directory others can write is not trusted")
        self.dir.chmod(0o700)

    def test_load_still_connects_with_the_same_checks(self):
        self.write(111)
        self.assertEqual(self.http.base_url(), "http://127.0.0.1:49374")
        self.write(333)
        self.http._info = None
        self.assertIsNone(self.http.base_url(), "a service file naming a dead process is still refused")


class TestCosOrdersFile(Base):
    def test_the_path_is_beside_the_config_unless_the_config_names_one(self):
        self.assertEqual(sb.cos_orders_path({}), self.home / "xdg-config" / "fleet-switchboard" / "standing-orders.md")
        self.assertEqual(sb.cos_orders_path({"cos_orders_file": "/x/y.md"}), Path("/x/y.md"))

    def test_a_missing_file_is_none_stated_and_not_an_error(self):
        found = sb.read_cos_orders({})
        self.assertEqual((found["exists"], found["text"], found["size"]), (False, None, 0))

    def test_the_text_is_the_file_with_the_templates_comment_taken_out(self):
        put_cos_orders(self, sb.COS_ORDERS_TEMPLATE)
        self.assertIsNone(sb.read_cos_orders({})["text"], "the template alone states no order")
        put_cos_orders(self, sb.COS_ORDERS_TEMPLATE + "\n- Approve read-only prompts.\n")
        self.assertEqual(sb.read_cos_orders({})["text"], "- Approve read-only prompts.")

    def test_eight_kilobytes_is_the_limit_and_a_larger_file_is_refused_and_says_so(self):
        put_cos_orders(self, "x" * 8192)
        self.assertEqual(sb.read_cos_orders({})["size"], 8192)
        put_cos_orders(self, "x" * 8193)
        with self.assertRaises(sb.CosOrdersError) as caught:
            sb.read_cos_orders({})
        self.assertIn("8193 bytes", str(caught.exception))
        self.assertIn("8 KB", str(caught.exception))

    def test_it_is_read_again_only_when_the_mtime_or_size_changes(self):
        path = put_cos_orders(self, "- one", mtime=1000)
        self.assertEqual(sb.read_cos_orders({})["text"], "- one")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("read again")):
            self.assertEqual(sb.read_cos_orders({})["text"], "- one", "a cached read: one stat per call")
        put_cos_orders(self, "- two", mtime=2000)
        self.assertEqual(sb.read_cos_orders({})["text"], "- two")
        path.write_text("- thr")
        os.utime(str(path), (2000, 2000))
        self.assertEqual(sb.read_cos_orders({})["text"], "- two", "the same mtime and size: nothing to re-read")

    def test_status_shows_the_path_size_and_mtime_or_none(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        text = sb.format_status(sb.collect_status(FakeRunner(), time.time))
        self.assertIn("cos orders none", text)
        self.assertIn("standing-orders.md", text)
        put_cos_orders(self, "- one", mtime=1791000000)
        report = sb.collect_status(FakeRunner(), time.time)
        self.assertEqual((report["cos_orders"]["size"], report["cos_orders"]["exists"]), (5, True))
        self.assertIn("(5 bytes, modified 2026-", sb.format_status(report))
        put_cos_orders(self, "x" * 9000)
        self.assertIn("is 9000 bytes", sb.format_status(sb.collect_status(FakeRunner(), time.time)))


class TestOrdersCosCommand(Base):
    def main(self, *argv):
        with captured() as (out, err):
            code = sb.main(list(argv), runner=FakeRunner(), now=time.time)
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_the_path_and_the_file_read_only(self):
        path = put_cos_orders(self, "- Answer read-only prompts.\n")
        code, out, err = self.main("orders", "--cos")
        self.assertEqual(code, 0, err)
        self.assertIn(str(path), out)
        self.assertIn("- Answer read-only prompts.", out)
        self.assertEqual(path.read_text(), "- Answer read-only prompts.\n")

    def test_no_file_says_none_and_where_it_would_be(self):
        code, out, err = self.main("orders", "--cos")
        self.assertEqual(code, 0, err)
        self.assertIn("none", out)
        self.assertIn("standing-orders.md", out)

    def test_a_file_that_is_too_large_is_refused_with_a_nonzero_exit(self):
        put_cos_orders(self, "x" * 9000)
        code, out, err = self.main("orders", "--cos")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("9000 bytes", err)

    def test_there_is_no_command_that_writes_it(self):
        for argv in (("orders", "--cos", "add", "x"), ("orders", "--cos", "--write", "x")):
            with self.subTest(argv=argv):
                self.assertNotEqual(self.main(*argv)[0], 0)
        self.assertFalse(cos_orders_file(self).exists())
        self.assertNotIn("write_text", class_source_fn("cmd_orders_cos"))

    def test_orders_with_no_action_and_no_flag_is_a_usage_error(self):
        code, out, err = self.main("orders")
        self.assertEqual(code, 2)
        self.assertIn("--cos", err)

    def test_help_shows_the_feature_and_the_charter_orders_are_as_they_were(self):
        with captured() as (out, _):
            with self.assertRaises(SystemExit):
                sb.build_parser().parse_args(["orders", "--help"])
        for word in ("--cos", "list", "add", "remove", "chief-of-staff", "local markdown file"):
            self.assertIn(word, out.getvalue())


class TestInstallMakesTheOrdersTemplate(InstallCase):
    def full(self, *flags):
        return self.install("--opencode", self.machine.binary, *flags)

    def path(self):
        return self.home / "xdg-config" / "fleet-switchboard" / "standing-orders.md"

    def test_it_makes_a_template_with_a_comment_header_and_says_so(self):
        out, error = self.full()
        self.assertIsNone(error)
        self.assertEqual(self.statuses(out)["standing orders"], ["done"])
        text = self.path().read_text()
        self.assertTrue(text.startswith("<!--"))
        self.assertIn("standing orders", text)
        self.assertIn("never loosen a configured deny", text)
        self.assertIsNone(sb.cos_orders_text_of(text), "the template states no order")
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)

    def test_it_never_overwrites_a_file_that_is_there(self):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text("- mine\n")
        out, error = self.full()
        self.assertEqual(self.statuses(out)["standing orders"], ["already done"])
        self.assertEqual(self.path().read_text(), "- mine\n")
        self.full()
        self.assertEqual(self.path().read_text(), "- mine\n")

    def test_a_second_run_leaves_the_template_as_the_owner_edited_it(self):
        self.full()
        self.path().write_text(sb.COS_ORDERS_TEMPLATE + "\n- edited\n")
        self.full()
        self.assertTrue(self.path().read_text().endswith("- edited\n"))

    def test_a_dry_run_makes_nothing(self):
        out, error = self.full("--dry-run")
        self.assertEqual(self.statuses(out)["standing orders"], ["would be done"])
        self.assertFalse(self.path().exists())

    def test_uninstall_leaves_it(self):
        self.full()
        self.path().write_text("- keep me\n")
        out, error = self.install("--uninstall")
        self.assertIsNone(error)
        self.assertEqual(self.path().read_text(), "- keep me\n")
        self.assertIn("your standing orders file", out)

    def test_the_config_can_move_the_file(self):
        target = self.home / "elsewhere" / "orders.md"
        self.write_config({"cos_orders_file": str(target)})
        self.full()
        self.assertTrue(target.is_file())
        self.assertFalse(self.path().exists())


class TestTierRoleText(unittest.TestCase):
    def text(self, *parts):
        return " ".join((REPO.joinpath(*parts)).read_text().split())

    def test_orchestrator_and_coder_raise_a_decision_to_the_chief_of_staff_and_never_apply_awaiting_user(self):
        for role in ("orchestrator", "coder"):
            for tree in ("opencode", "copilot"):
                name = role + (".md" if tree == "opencode" else ".agent.md")
                with self.subTest(role=role, tree=tree):
                    text = self.text("agents", tree, name)
                    self.assertIn('fleet-switchboard report question "..."', text)
                    self.assertIn("awaiting-cos", text)
                    self.assertIn("NEVER apply `awaiting-user` yourself", text)
                    self.assertIn("switchboard message", text)
                    self.assertIn("remove your `awaiting-cos` label", text)

    def test_the_chief_of_staff_works_its_list_each_wake_and_never_edits_its_orders(self):
        for tree, name in (("opencode", "chief-of-staff.md"), ("copilot", "chief-of-staff.agent.md")):
            with self.subTest(tree=tree):
                text = " ".join(self.text("agents", tree, name).replace("`", "").lower().split())
                for needle in ("fleet-switchboard decisions", "decisions answer <id> allow|deny", "decisions resolve <id>",
                               "quoting the order in one line", "never both", "never invent or widen an order",
                               "never edit", "orders file", "if unsure, escalate", "restate what is unchanged",
                               "decisions escalate <id> --reason"):
                    self.assertIn(needle, text)

    def test_the_skills_say_the_same_briefly_and_setup_runs_labels_ensure(self):
        charter = self.text("skills", "fleet-charter", "SKILL.md")
        self.assertIn("awaiting-cos", charter)
        self.assertIn("NEVER apply `awaiting-user` yourself", charter)
        coordination = self.text("skills", "fleet-coordination", "SKILL.md")
        self.assertIn("awaiting-cos", coordination)
        self.assertIn("standing-orders.md", coordination)
        setup = self.text("skills", "fleet-setup", "SKILL.md")
        self.assertIn("fleet-switchboard labels ensure", setup)
        self.assertIn("standing-orders.md", setup)


class TestTierScenarios(Base):
    def test_the_pr18_catalog_is_present_with_a_control_each(self):
        found = {s["id"]: s for s in map(sc.load, sc.scenario_files(sc.DEFAULT_DIR))}
        for sid, control in (("T1", ("fault", "tier-flat")), ("T2", ("variant", None)), ("T3", ("fault", "answer-unjudged")),
                             ("T4", ("fault", "cos-orders-unfed")), ("T5", ("variant", None)),
                             ("T6", ("fault", "anyone-edits-cos-orders"))):
            with self.subTest(scenario=sid):
                self.assertIn(sid, found)
                self.assertEqual(found[sid]["since_pr"], 18)
                self.assertEqual(found[sid]["tiers"], ["offline"])
                kind, name = control
                self.assertIn(kind, found[sid]["control"])
                if name:
                    self.assertEqual(found[sid]["control"][kind], name)

    def test_every_new_fault_is_known_and_off_outside_the_lab(self):
        for name in ("tier-flat", "cos-orders-unfed", "anyone-edits-cos-orders", "answer-unjudged"):
            with self.subTest(fault=name):
                self.assertIn(name, sb.KNOWN_FAULTS)
                os.environ[sb.FAULT_ENV] = name
                os.environ.pop(sb.LAB_ENV, None)
                self.assertFalse(sb.fault(name))
                os.environ[sb.LAB_ENV] = "1"
                self.assertTrue(sb.fault(name))

    def test_the_vocabulary_has_the_new_steps_and_oracles(self):
        self.assertEqual(sorted(sc.STEP_FIELDS["decide"]), ["action", "note", "reason", "ref", "value"])
        self.assertEqual(sorted(sc.STEP_FIELDS["edit-cos-orders"]), ["text"])
        self.assertIn("label", sc.STEP_FIELDS["awaiting"])
        self.assertEqual(sc.STEP_ACTORS["decide"], ("cast",))
        self.assertEqual(sorted(sc.EXPECT_FIELDS["issue_labels"]), ["has", "issue", "lacks"])
        self.assertEqual(sorted(sc.EXPECT_FIELDS["permission_answered"]), ["agent", "count", "decision"])
        self.assertIn("as", sc.EXPECT_FIELDS["decisions"])
        self.assertIn("tier", sc.EXPECT_FIELDS["decisions"])

    def test_the_vocabulary_checks_the_new_values(self):
        base = mutate(_set(["steps", 0], {"at": "0s", "actor": "coder", "do": "decide", "action": "answer", "ref": "x"}))
        self.assertEqual(sc.validate(base), [])
        for field, bad in (("action", "approve"), ("value", "a\nb")):
            step = {"at": "0s", "actor": "coder", "do": "decide", "action": "answer", "ref": "x", field: bad}
            self.assertTrue(sc.validate(mutate(_set(["steps", 0], step))), field)
        item = {"that": "decisions", "at": "30s", "count": 0, "as": "nobody"}
        self.assertTrue([e for e in sc.validate(mutate(_set(["expect", 2], item))) if "as" in e])
        item = {"that": "decisions", "at": "30s", "count": 0, "tier": "everyone"}
        self.assertTrue([e for e in sc.validate(mutate(_set(["expect", 2], item))) if "tier" in e])

    def test_the_readme_documents_the_new_vocabulary(self):
        readme = (REPO / "scenarios" / "switchboard" / "README.md").read_text()
        for word in ("`decide`", "`edit-cos-orders`", "`issue_labels`", "`permission_answered`", "`label`"):
            self.assertIn(word, readme)


class TestExternalAdapterHygiene(unittest.TestCase):
    # Fragments, so this list does not spell the words it forbids.
    DENYLIST = ["sp" + "ire", "ven" + "ue", "jan" + "us", "plug" + "in", "spp" + "k", "under" + "study",
                "fleet-conn" + "ector", "don" + "ald", "/api/" + "plugin", "open" + "router"]

    def test_the_module_names_no_product(self):
        text = EXTERNAL_ADAPTER.read_text().lower()
        for word in self.DENYLIST:
            self.assertNotIn(word, text, "the module must stay generic: %r" % word)

    def test_the_module_is_stdlib_only_and_python_3_9_syntax(self):
        tree = ast.parse(EXTERNAL_ADAPTER.read_text(), feature_version=(3, 9))
        allowed = {"collections", "hashlib", "http", "importlib", "json", "os", "re", "stat", "subprocess", "sys",
                   "time", "urllib", "uuid", "abc"}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add((node.module or "").split(".")[0])
        self.assertLessEqual(found, allowed)

    def test_it_imports_under_python_3_9(self):
        old = shutil.which("python3.9") or ("/usr/bin/python3" if os.path.exists("/usr/bin/python3") else None)
        if old is None:
            self.skipTest("no python 3.9 interpreter to import it with")
        shown = subprocess.run([old, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True,
                               text=True).stdout.strip()
        if shown != "(3, 9)":
            self.skipTest("no python 3.9 interpreter (found %s)" % shown)
        done = subprocess.run([old, "-I", "-B", "-c",
                               "import importlib.util as u; s = u.spec_from_file_location('ea', %r); m = "
                               "u.module_from_spec(s); s.loader.exec_module(m); print(m.McpRestAdapter.__name__)"
                               % str(EXTERNAL_ADAPTER)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "McpRestAdapter")

    def test_no_sudo_anywhere_in_it(self):
        self.assertNotIn("sudo", EXTERNAL_ADAPTER.read_text())


class TestExternalDecisionRules(unittest.TestCase):
    core = sb.decision_core

    def test_keys_are_stable_and_the_idempotency_key_follows_the_operation(self):
        a = self.core.decision_key("issue:acme/api#2")
        self.assertEqual(a, self.core.decision_key("issue:acme/api#2"))
        self.assertNotEqual(a, self.core.decision_key("issue:acme/api#3"))
        k = self.core.idempotency_key(a, "revise", 2)
        self.assertEqual(k, self.core.idempotency_key(a, "revise", 2))
        self.assertEqual(len({k, self.core.idempotency_key(a, "revise", 3), self.core.idempotency_key(a, "raise", 2),
                              self.core.idempotency_key(self.core.decision_key("x"), "revise", 2)}), 4)

    def test_authority_follows_the_role_of_the_answerer(self):
        ok = lambda role, tier: self.core.answer_authority(role, tier)[0]
        self.assertEqual([ok("project_agent", t) for t in ("orchestrator", "cos", "human")], [True, False, False])
        self.assertEqual([ok("owner_agent", t) for t in ("orchestrator", "cos", "human")], [True, True, False])
        self.assertEqual([ok("human", t) for t in ("orchestrator", "cos", "human")], [True, True, True])
        self.assertFalse(ok(None, "cos"))
        self.assertFalse(ok("human", "nobody"))
        self.assertEqual([self.core.normalize_role(x) for x in ("project agent", "Owner's agent", "human", "robot")],
                         ["project_agent", "owner_agent", "human", None])

    def test_a_permission_request_and_a_notice_never_go_out(self):
        for kind in ("permission", "notice"):
            self.assertFalse(self.core.goes_outward({"kind": kind, "repo": API}, [API], ("issue", "report", "question")))
        self.assertTrue(self.core.goes_outward({"kind": "issue", "repo": API}, [API]))
        self.assertFalse(self.core.goes_outward({"kind": "issue", "repo": WEB}, [API]))
        self.assertFalse(self.core.goes_outward({"kind": "question", "repo": API}, [API]))
        self.assertTrue(self.core.goes_outward({"kind": "question", "repo": API}, [API], ("question",)))

    def test_the_payload_carries_no_credential(self):
        payload = self.core.outward_payload({"title": "use Bearer abcdef123456 and sk-abcdefghij1234", "headline": "h",
                                             "kind": "report", "id": "#2", "tier": "cos"}, "floor-b")
        self.assertNotIn("abcdef123456", json.dumps(payload))
        self.assertNotIn("sk-abcdefghij1234", json.dumps(payload))


class TestWorkItemRules(unittest.TestCase):
    ws = sb.work_sync

    def test_the_key_and_the_item_id_are_stable_and_per_assignment(self):
        a = self.ws.work_id_of(self.ws.assignment_key("w1", "acme/api", 2))
        self.assertEqual(a, self.ws.work_id_of(self.ws.assignment_key("w1", "acme/api", 2)))
        self.assertEqual(len({a, self.ws.work_id_of(self.ws.assignment_key("w1", "acme/api", 3)),
                              self.ws.work_id_of(self.ws.assignment_key("w2", "acme/api", 2)),
                              self.ws.work_id_of(self.ws.assignment_key("w1", "acme/web", 2))}), 4)

    def test_the_state_of_an_assignment(self):
        cases = [("idle", [], ("queued", None)), ("working", [], ("running", None)),
                 ("working", [report("question", "which?")], ("running", None)),
                 ("idle", [report("question", "which?")], ("awaiting", None)),
                 ("idle", [report("blocked", "stuck")], ("blocked", None)),
                 ("blocked", [], ("blocked", None)), ("idle", [report("working", "on it")], ("awaiting", None)),
                 ("idle", [report("paused", "later")], ("awaiting", None)),
                 ("idle", [report("working", "x", 1), report("done", "shipped", 2)], ("done", "done")),
                 ("working", [report("failed", "broke")], ("failed", "failed")),
                 ("idle", [report("withdrawn", "moot")], ("cancelled", "cancelled")),
                 ("idle", [report("done", "old", 1), report("working", "again", 2)], ("awaiting", None))]
        for status, mine, want in cases:
            with self.subTest(status=status, reports=[r["state"] for r in mine]):
                state, end, _ = self.ws.state_of(status, mine)
                self.assertEqual((state, end), want)
        self.assertEqual(self.ws.state_of("idle", [report("question", "which table?")])[2], "which table?")

    def test_serves_is_the_outside_item_id_when_the_ask_is_linked_else_the_issue_ref(self):
        links = {"k": {"repo": "acme/api", "ask": 2, "external_id": "ext-9"}}
        self.assertEqual(self.ws.serves_of(links, "acme/api", 2), "ext-9")
        self.assertEqual(self.ws.serves_of(links, "acme/api", 3), "acme/api#3")
        self.assertEqual(self.ws.serves_of({}, "acme/api", 2), "acme/api#2")
        self.assertEqual(self.ws.serves_of({"k": dict(links["k"], withdrawn=True)}, "acme/api", 2), "acme/api#2")

    def test_which_agents_are_assignments_and_their_fields(self):
        host = FakeWorkHost()
        agents = [worker("w1"), worker("noissue", issue=None), worker("noboss", reports_to=None),
                  worker("norepo", repo=None), worker("w2", issue=5, intent="Ship the parser. More text.")]
        found = self.ws.assignments(agents, [], host.title_of, {})
        self.assertEqual(sorted(a["agent"] for a in found.values()), ["w1", "w2"])
        by = {a["agent"]: a for a in found.values()}
        self.assertEqual((by["w1"]["title"], by["w1"]["kind"], by["w1"]["state"], by["w1"]["serves"]),
                         ("The ask api#2", "coder", "queued", "acme/api#2"))
        self.assertEqual(by["w2"]["title"], "Ship the parser")


class TestComposeTrap(Base):
    """`tools/fleet-compose-trap.sh`: the compose project is brought down WITH its volumes once, on a normal exit and on an interrupt."""

    def run_harness(self, how):
        log = self.home / "docker.log"
        fake = self.home / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "docker").write_text("#!/bin/sh\necho \"$@\" >> %s\n" % log)
        os.chmod(str(fake / "docker"), 0o755)
        script = self.home / "harness.sh"
        script.write_text("#!/usr/bin/env bash\n. %s\nfleet_compose_trap -p proj -f compose.test.yml\n%s\n" % (
            REPO / "tools" / "fleet-compose-trap.sh", "exit 0" if how == "exit" else "sleep 30 &\ntouch %s\nwait" % (self.home / "ready")))
        env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]))
        proc = subprocess.Popen(["bash", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))   # a suite started in the background inherits SIGINT ignored, which no trap can undo
        if how != "exit":
            for _ in range(200):   # the child says when its trap is set and it waits
                if (self.home / "ready").exists():
                    break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("the harness never got ready")
            proc.send_signal(signal.SIGINT if how == "interrupt" else signal.SIGTERM)
        proc.wait(timeout=20)
        return log.read_text().splitlines() if log.exists() else []

    def test_s5_a_normal_exit_runs_down_with_volumes_once(self):
        self.assertEqual(self.run_harness("exit"), ["compose -p proj -f compose.test.yml down -v --remove-orphans"])

    def test_s5_an_interrupt_and_a_termination_run_it_once_each(self):
        for how in ("interrupt", "terminate"):
            with self.subTest(how=how):
                (self.home / "docker.log").unlink(missing_ok=True)
                (self.home / "ready").unlink(missing_ok=True)
                self.assertEqual(self.run_harness(how), ["compose -p proj -f compose.test.yml down -v --remove-orphans"])

    def test_the_helper_never_uses_sudo(self):
        self.assertNotIn("sudo", (REPO / "tools" / "fleet-compose-trap.sh").read_text().replace("Never uses sudo.", ""))


class TestDaemonPriority(Base):
    """P0 CPU item C: the daemon's LaunchAgent asks for the Interactive process class, with no root; ensure's own start is unchanged."""

    def template(self):
        import plistlib
        return plistlib.loads((REPO / "templates" / "com.fleet-kit.switchboard.plist").read_bytes())

    def test_s1_the_template_asks_for_the_interactive_class_and_nothing_that_needs_root(self):
        plist = self.template()
        self.assertEqual(plist["ProcessType"], "Interactive")
        for key in ("UserName", "GroupName", "Nice", "SessionCreate"):
            self.assertNotIn(key, plist)
        self.assertEqual(plist["Label"], "com.fleet-kit.switchboard")
        self.assertEqual(plist["ProgramArguments"][-2:], ["run", "--standby"])

    def test_s2_what_install_writes_parses_and_says_the_same(self):
        import plistlib
        values = sb.service_values(REPO, sb.service_path_dirs(REPO, []))
        written = plistlib.loads(sb.render_service("launchd", values, REPO).encode())
        self.assertEqual(written["ProcessType"], "Interactive")
        self.assertEqual(written["ProgramArguments"][-2:], ["run", "--standby"])
        self.assertEqual(written["ProgramArguments"][0], values["__PYTHON__"])
        self.assertIsNone(re.search(r"__[A-Z_]+__", json.dumps(written)), "every placeholder is filled")

    def test_s3_ensures_own_start_is_unchanged(self):
        seen = []
        shell = {"PATH": "/usr/bin", "HERDR_PANE_ID": "w1:p1", "FLEET_SWITCHBOARD_ROLE": "coder", "HERDR_SOCKET_PATH": "/s"}
        with mock.patch.dict(os.environ, shell, clear=True):
            sb.spawn_daemon(self.world_state(), spawn=lambda argv, **kw: seen.append((argv, kw)))
        argv, kw = seen[0]
        self.assertEqual(argv[-1], "run")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["cwd"], str(self.world_state()))
        self.assertNotIn("HERDR_PANE_ID", kw["env"])
        self.assertNotIn("FLEET_SWITCHBOARD_ROLE", kw["env"])
        self.assertFalse({"launchctl", "taskpolicy", "nice", "sudo"} & {os.path.basename(a) for a in argv}, "no wrapper around the start, by name or by path")

    def world_state(self):
        path = self.home / "state"
        path.mkdir(exist_ok=True)
        return path

    def test_s4_the_docs_say_how_to_opt_in_check_and_go_back(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        section = text[text.index("### The daemon's priority under load"):]
        section = section[:section.index("\n### ", 10)] if "\n### " in section[10:] else section
        for needle in ("ProcessType", "Interactive", "opt-in", "launchctl print", "no sudo", "fleet-switchboard install", "--uninstall"):
            self.assertIn(needle, section)


class TestGithubWritingRule(unittest.TestCase):
    """Ask #401, slice 1: the same one-sentence writing rule for GitHub text in the two skills, the coder and orchestrator definitions of both runtimes and the
    pull request template."""

    RULE = ('Write GitHub text so a person can read it: short paragraphs, real Markdown lists with one item per line, headings for sections, '
            'never an inline "(1) ... (2) ..." chain, and code and commands in code spans or blocks.')
    FILES = ("skills/fleet-charter/SKILL.md", "skills/fleet-coordination/SKILL.md", "agents/opencode/coder.md", "agents/opencode/orchestrator.md",
             "agents/copilot/coder.agent.md", "agents/copilot/orchestrator.agent.md", ".github/pull_request_template.md")

    def test_s1_every_file_carries_the_rule_sentence_exactly(self):
        for rel in self.FILES:
            with self.subTest(file=rel):
                self.assertIn(self.RULE, (REPO / rel).read_text())

    def test_s2_it_is_one_sentence_everywhere_and_never_inside_a_code_fence(self):
        found = set()
        for rel in self.FILES:
            text = (REPO / rel).read_text()
            for line in text.splitlines():
                at = line.find("Write GitHub text so a person can read it")
                if at >= 0:
                    found.add(line[at:].replace(" -->", "").rstrip())
            inside, fenced = False, False
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    inside = not inside
                elif inside and "Write GitHub text so a person can read it" in line:
                    fenced = True
            self.assertFalse(fenced, "%s: the rule sits inside a code fence" % rel)
        self.assertEqual(found, {self.RULE})

    def test_the_rule_is_not_in_the_front_matter_of_a_definition(self):
        for rel in self.FILES[2:6]:
            text = (REPO / rel).read_text()
            head = text.split("\n---\n", 1)[0]
            self.assertNotIn("Write GitHub text", head, rel)
            self.assertTrue(text.startswith("---\n"), rel)


class LightProbe:
    """Makes `Switchboard.light` observable: every read of it records the source line that asked, and a chosen set of lines is told `False` (the guard removed there)
    while every other line is told `True`. It changes no code: the same engine, the same fake source, one guard at a time."""

    FILE = str(SWITCHBOARD)
    # the line a file starts at in switchboard_source() (the files are joined with one newline), so a read in a part is told apart by its own file
    OFFSETS = {}
    _at = 0
    for _path in SWITCHBOARD_SOURCE_FILES:
        OFFSETS[str(_path)] = _at
        _at += _path.read_text().count("\n") + 1

    def __init__(self, flip=()):
        self.flip, self.hits = set(flip), collections.Counter()

    def __enter__(self):
        probe = self

        def get(engine):
            frame = sys._getframe(1)
            offset = probe.OFFSETS.get(frame.f_code.co_filename)
            if offset is not None:
                line = frame.f_lineno + offset
                probe.hits[line] += 1
                if any(lo <= line <= hi for lo, hi in probe.flip):
                    return False
            return engine.__dict__["light"]

        def put(engine, value):
            engine.__dict__["light"] = value
        self.patch = mock.patch.object(sb.Switchboard, "light", property(get, put), create=True)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def touched(self, lo, hi):
        return sum(n for line, n in self.hits.items() if lo <= line <= hi)


class TestEventFeedLines(unittest.TestCase):
    def test_only_the_wanted_events_are_kept_and_no_text_is_ever_kept(self):
        feed = sb.EventFeed(None)
        for line in ("event: x", "data: not json", "data: []", feed_line("session.tool.success", assistantMessageID="msg_t", content="x" * 5000),
                     feed_line("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="SECRET-TEXT"),
                     feed_line("session.execution.succeeded", created=5000)):
            feed.handle(line)
        self.assertEqual(feed.status()["events"], 2, "the tool output and the junk were not kept")
        self.assertEqual(feed.seen["ses_w"], {"assistant": "msg_a", "idle_ms": 5000, "read": None, "event_at": feed.seen["ses_w"]["event_at"]})
        self.assertNotIn("SECRET-TEXT", repr(feed.seen), "an event's text can be partial or stale: it is never kept")

    def test_the_step_ended_and_failed_and_interrupted_branches_and_a_line_without_a_session(self):
        feed = sb.EventFeed(None)
        feed.handle(feed_line("session.step.ended", assistantMessageID="msg_s"))
        self.assertEqual(feed.seen["ses_w"]["assistant"], "msg_s")
        for kind in ("session.execution.failed", "session.execution.interrupted"):
            feed.handle(feed_line(kind, created=7000))
            self.assertEqual(feed.seen["ses_w"]["idle_ms"], 7000)
        feed.handle('data: {"type": "session.text.ended", "data": {"text": "t"}}')
        self.assertEqual(feed.status()["sessions"], 1)

    def test_the_sessions_kept_are_bounded(self):
        feed = sb.EventFeed(None)
        with mock.patch.object(sb, "FEED_SESSIONS_MAX", 3):
            for n in range(10):
                feed.handle(feed_line("session.execution.succeeded", session="ses_%d" % n, created=1))
        self.assertEqual(feed.status()["sessions"], 3)

    def test_a_session_is_touched_when_an_event_named_it_after_it_was_last_read(self):
        feed = sb.EventFeed(None, now=lambda: 100.0)
        self.assertTrue(feed.touched_since_read("ses_w"), "never read")
        feed.seen["ses_w"] = {"assistant": None, "idle_ms": None, "read": 100.0, "event_at": None}
        self.assertFalse(feed.touched_since_read("ses_w"))
        feed.now = lambda: 101.0
        feed.handle(feed_line("session.step.ended", assistantMessageID="msg_s"))
        self.assertTrue(feed.touched_since_read("ses_w"))


class TestPaneWarningRule(unittest.TestCase):
    class Herdr:
        def __init__(self, pane_session):
            self.pane_session = pane_session

        def agent_get(self, pane):
            return {"agent_session": self.pane_session}

        def agent_list(self):
            return []

    class V2:
        def __init__(self, names):
            self.names = names

        def session_get(self, session):
            return {"metadata": {"fleet": {"name": self.names[session]}}} if self.names.get(session) else {"metadata": {}}

    def warn(self, pane_session, names):
        sb._WARNED_PANES.clear()
        with captured() as (_, err):
            sb.warn_pane_disagrees(self.Herdr(pane_session), self.V2(names), {"HERDR_PANE_ID": "w1:p1"}, "ses_own", "coder")
        return err.getvalue()

    def test_s6_the_same_fleet_agent_under_another_session_is_silent(self):
        self.assertEqual(self.warn("ses_old", {"ses_old": "coder", "ses_own": "coder"}), "")

    def test_s6_a_different_fleet_agent_or_no_fleet_agent_still_warns_once(self):
        self.assertIn("belongs to session ses_old (other)", self.warn("ses_old", {"ses_old": "other", "ses_own": "coder"}))
        self.assertIn("belongs to session ses_old (not a fleet agent)", self.warn("ses_old", {"ses_own": "coder"}))


class TestCallerWorkspace(unittest.TestCase):
    class Herdr:
        def __init__(self, agents):
            self.agents = agents

        def agent_list(self):
            if self.agents is None:
                raise sb.HerdrError("unavailable", "down")
            return self.agents

    def engine(self, agents):
        return types.SimpleNamespace(herdr=self.Herdr(agents))

    own = [{"agent_session": "ses_own", "pane_id": "wW:p1", "workspace_id": "wW"}]

    def go(self, agents, explicit, **environ):
        with captured() as (_, err):
            return sb.caller_workspace(self.engine(agents), explicit, environ), err.getvalue()

    def test_s7_a_disagreeing_inherited_workspace_is_not_used_and_both_are_named(self):
        got, err = self.go(self.own, None, OPENCODE_SESSION_ID="ses_own", HERDR_WORKSPACE_ID="wD")
        self.assertEqual(got, "wW")
        self.assertIn("wW", err)
        self.assertIn("wD", err)

    def test_s8_an_explicit_workspace_wins_and_an_unresolvable_caller_keeps_the_variable(self):
        self.assertEqual(self.go(self.own, "wX", OPENCODE_SESSION_ID="ses_own", HERDR_WORKSPACE_ID="wD"), ("wX", ""))
        self.assertEqual(self.go(self.own, None, HERDR_WORKSPACE_ID="wD"), ("wD", ""))                            # a plain shell
        self.assertEqual(self.go([], None, OPENCODE_SESSION_ID="ses_own", HERDR_WORKSPACE_ID="wD"), ("wD", ""))   # herdr does not show the session
        self.assertEqual(self.go(None, None, OPENCODE_SESSION_ID="ses_own", HERDR_WORKSPACE_ID="wD"), ("wD", ""))  # herdr unreadable

    def test_u3_when_they_agree_nothing_is_said(self):
        self.assertEqual(self.go(self.own, None, OPENCODE_SESSION_ID="ses_own", HERDR_WORKSPACE_ID="wW"), ("wW", ""))
        self.assertEqual(self.go(self.own, None, OPENCODE_SESSION_ID="ses_own"), ("wW", ""))

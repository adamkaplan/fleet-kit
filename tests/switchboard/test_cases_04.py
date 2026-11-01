"""Test classes moved out of bin/test-switchboard (text unchanged): cases_04."""
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
from support.fake_hubs import WaitingInputs
from support.fake_jev import FakeJev, JEV_SETTINGS, KEY_VALUE, confident
from support.installs_and_keys import InstallCase, KeyCase, PR16_KEY
from support.loader import SCENARIO, captured, sb, sc, switchboard_source
from support.orders_and_adapters import EA, ExternalAdapterCase, fake_mapping, pr_view_json
from support.paths import REPO
from support.policy_and_import import FORBIDDEN_IN_PUBLIC_TEXT
from support.scenario_roles import FAKE_GH, GH_REPO
from support.service_and_merge import V2TransportBase, feed_line
from support.wire_fakes import DOC_FAULTS, EPOCH, EXPECT_EXAMPLES, FAKE_HERDR, FAKE_OPENCODE, MSG, REJECTED, SES, STEP_EXAMPLES, SimClock, VALID, _drop, _set, fact, mutate, observation


class TestAuditLog(Base):
    def setUp(self):
        super().setUp()
        self.path = sb.ensure_state_dir() / "audit.jsonl"
        self.audit = sb.AuditLog(self.path)

    def entries(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_append_writes_one_json_line_per_entry(self):
        self.audit.append("daemon", "hold", "coder", {"why": "blocked"})
        self.audit.append("daemon", "pass", None)
        entries = self.entries()
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual({k: first[k] for k in ("actor", "event", "subject", "detail")},
                         {"actor": "daemon", "event": "hold", "subject": "coder", "detail": {"why": "blocked"}})
        self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_append_never_rewrites_what_is_there(self):
        self.path.write_text('{"earlier": true}\n')
        os.chmod(str(self.path), 0o600)
        self.audit.append("cli", "send", "platform")
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[0], '{"earlier": true}')
        self.assertEqual(len(lines), 2)

    def test_file_is_private(self):
        old = os.umask(0)
        try:
            self.audit.append("cli", "send", "platform")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_each_write_is_flushed_to_disk(self):
        with mock.patch.object(sb.os, "fsync", wraps=os.fsync) as fsync:
            self.audit.append("cli", "send", "platform")
        self.assertEqual(fsync.call_count, 1)

    def test_external_writes_intent_then_result(self):
        with self.audit.external("daemon", "v2.synthetic", "platform", {"message_id": MSG}) as outcome:
            self.assertEqual([e["phase"] for e in self.entries()], ["intent"])
            outcome["receipt"] = "msg_x"
        intent, result = self.entries()
        self.assertEqual((intent["phase"], result["phase"]), ("intent", "result"))
        self.assertEqual(intent["call"], result["call"])
        self.assertEqual(intent["detail"], {"message_id": MSG})
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], {"receipt": "msg_x"})
        self.assertEqual((result["event"], result["subject"]), ("v2.synthetic", "platform"))

    def test_external_records_the_error_and_reraises(self):
        with self.assertRaises(sb.V2Error):
            with self.audit.external("daemon", "v2.synthetic", "platform"):
                raise sb.V2Error("http", 503, "service down")
        intent, result = self.entries()
        self.assertEqual(intent["phase"], "intent")
        self.assertFalse(result["ok"])
        self.assertIn("V2Error", result["error"])
        self.assertIn("503", result["error"])

    def test_credentials_are_redacted_at_any_depth(self):
        self.audit.append("daemon", "config", "x", {
            "token": "t-1", "api_key": "k-2", "Password": "p-3", "client_secret": "s-4",
            "Authorization": "Bearer a-5", "OPENROUTER_API_KEY": "k-6",
            "nested": [{"headers": {"authorization": "Bearer b-7"}}, {"accessToken": "t-8"}],
            "keys": ["worker.idle:ses_1:1700000000"], "plain": "visible"})
        text = self.path.read_text()
        for secret in ("t-1", "k-2", "p-3", "s-4", "a-5", "k-6", "b-7", "t-8"):
            self.assertNotIn(secret, text)
        detail = self.entries()[0]["detail"]
        self.assertEqual(detail["token"], sb.REDACTED)
        self.assertEqual(detail["nested"][0]["headers"]["authorization"], sb.REDACTED)
        self.assertEqual(detail["plain"], "visible")
        # The fact keys a message covers are what the audit exists to show.
        self.assertEqual(detail["keys"], ["worker.idle:ses_1:1700000000"])

    def test_external_redacts_both_entries(self):
        with self.audit.external("daemon", "x", "y", {"secret": "s-1"}) as outcome:
            outcome["token"] = "t-2"
        text = self.path.read_text()
        self.assertNotIn("s-1", text)
        self.assertNotIn("t-2", text)


class TestScenarioValidator(unittest.TestCase):
    def test_the_full_fixture_is_valid(self):
        self.assertEqual(sc.validate(copy.deepcopy(VALID)), [])

    def test_valid_alternatives(self):
        alternatives = [
            _set(["problem"], None),
            _set(["control"], {"baseline": "fleet-heartbeat", "fails": ["no_machine_turn"]}),
            _set(["control"], {"variant": {"steps": {"1": {"text": "x"}}, "expect": {"one-note": {"count": 2}}},
                               "fails": ["one-note"]}),
            _set(["steps"], []),
            _drop(["issues"]),
            _drop(["tags"]),
            _set(["tiers"], ["lab-model"]),
            _set(["tags"], ["spike"]),
        ]
        for change in alternatives:
            obj = mutate(change)
            with self.subTest(obj={k: obj.get(k) for k in ("problem", "control", "steps", "tiers")}):
                self.assertEqual(sc.validate(obj), [])

    def test_each_rule_rejects_its_fixture(self):
        for what, change, needle in REJECTED:
            with self.subTest(what=what):
                errors = sc.validate(mutate(change))
                self.assertTrue(errors, "accepted: %s" % what)
                self.assertTrue(any(needle in e for e in errors), "%s: %s" % (what, errors))

    def test_every_required_key_is_required(self):
        for key in sc.REQUIRED:
            with self.subTest(key=key):
                self.assertIn("%s: required" % key, sc.validate(mutate(_drop([key]))))

    def test_not_an_object(self):
        self.assertEqual(sc.validate([VALID]), ["scenario: must be a JSON object"])

    def test_every_documented_value_is_accepted(self):
        for do in sc.ACTIONS:
            for actor in sc.STEP_ACTORS[do]:
                step = {"at": "0s", "actor": "coder" if actor == "cast" else actor, "do": do, "why": "a comment"}
                step.update(STEP_EXAMPLES[do])
                if do == "permission" and actor == "cast":
                    step = {k: v for k, v in step.items() if k not in ("to", "decision")}
                self.assertEqual(sc.validate(mutate(_set(["steps", 0], step))), [], (do, actor))
        for that in sc.EXPECTATIONS:
            item = {"that": that, "why": "a comment"}
            item.update(EXPECT_EXAMPLES[that])
            self.assertEqual(sc.validate(mutate(_set(["expect", 2], item))), [], that)
        for name in DOC_FAULTS:
            self.assertEqual(sc.validate(mutate(_set(["control", "fault"], name))), [], name)

    def test_every_field_checked_kind_has_an_example(self):
        self.assertEqual(sorted(STEP_EXAMPLES), sorted(sc.STEP_FIELDS))
        self.assertEqual(sorted(EXPECT_EXAMPLES), sorted(sc.EXPECT_FIELDS))

    def test_every_tier_is_checked_field_by_field(self):
        # One vocabulary: a Lab-only scenario has no fields of its own.
        for tiers in (["offline"], ["lab-scripted"], ["lab-model"]):
            with self.subTest(tiers=tiers):
                errors = sc.validate(mutate(lambda o: (o.update(tiers=tiers),
                                                       o["steps"][0].update(script="sleep=5 reply=DONE"),
                                                       o["expect"][0].update(role="user"))))
                self.assertTrue(any("unknown field 'script'" in e for e in errors), errors)
                self.assertTrue(any("unknown field 'role'" in e for e in errors), errors)

    def test_a_variant_is_patched_in_and_the_result_checked(self):
        patched = sc.apply_variant(VALID, {"steps": {"1": {"text": "x"}}, "expect": {"one-note": {"count": 2},
                                                                                   "1": {"why": "w"}}})
        self.assertEqual((patched["steps"][1]["text"], patched["expect"][0]["count"], patched["expect"][1]["why"]),
                         ("x", 2, "w"))
        self.assertEqual(VALID["steps"][1]["text"], "status?", "the scenario itself is not changed")
        for variant, needle in (({"cast": {}}, "unknown key"), ({"steps": {"9": {"text": "x"}}}, "not the index"),
                                ({"steps": {"one": {"text": "x"}}}, "not the index"),
                                ({"expect": {"nobody": {"count": 2}}}, "matches no expectation"),
                                ({"steps": {"1": {}}}, "non-empty object"), ("x", "must be an object")):
            with self.subTest(variant=variant):
                with self.assertRaises(sc.VariantError) as caught:
                    sc.apply_variant(VALID, variant)
                self.assertIn(needle, str(caught.exception))

    def test_durations(self):
        self.assertEqual([sc.duration_seconds(t) for t in ("0s", "10s", "2m", "2h", "150s")],
                         [0, 10, 120, 7200, 150])
        for bad in ("", "1.5s", "-1s", "1 s", "1d", None, 3):
            self.assertIsNone(sc.duration_seconds(bad), bad)


class TestScenarioFiles(Base):
    def put(self, name, obj, directory=None):
        directory = directory or self.home / "scenarios"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return path

    def test_load_and_load_dir(self):
        self.put("n1.json", VALID)
        self.put("w1.json", mutate(_set(["id"], "W1")))
        self.put("README.md", "not a scenario")
        self.assertEqual(sc.load(self.home / "scenarios" / "n1.json")["id"], "N1")
        self.assertEqual([s["id"] for s in sc.load_dir(self.home / "scenarios")], ["N1", "W1"])
        self.put("x1.json", mutate(_set(["id"], "x")))
        with self.assertRaises(sc.ScenarioError) as caught:
            sc.load_dir(self.home / "scenarios")
        self.assertIn("x1.json", str(caught.exception))

    def test_load_dir_refuses_a_repeated_id(self):
        self.put("a.json", VALID)
        self.put("b.json", VALID)
        with self.assertRaises(sc.ScenarioError):
            sc.load_dir(self.home / "scenarios")

    def test_load_refuses_bad_json(self):
        with self.assertRaises(sc.ScenarioError):
            sc.load(self.put("bad.json", "{"))

    def test_coverage_lists_every_problem(self):
        scenarios = [mutate(_set(["id"], "A%d" % n)) for n in range(3)]
        scenarios[1]["problem"] = 1
        scenarios[2]["problem"] = None
        self.assertEqual(sc.coverage(scenarios), {1: ["A1"], 2: [], 3: ["A0"], 4: []})

    def test_validate_cli_exit_codes(self):
        good = self.put("n1.json", VALID)
        self.assertEqual(self.run_cli(SCENARIO, "validate", str(good)).returncode, 0)
        self.assertEqual(self.run_cli(SCENARIO, "validate", str(good.parent)).returncode, 0)
        bad = self.put("bad.json", mutate(_set(["since_pr"], 0)), self.home / "bad")
        proc = self.run_cli(SCENARIO, "validate", str(bad))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("since_pr", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.run_cli(SCENARIO, "validate", str(self.put("j.json", "{", self.home / "j"))).returncode, 1)
        self.assertEqual(self.run_cli(SCENARIO, "validate", str(self.home / "missing.json")).returncode, 1)
        empty = self.home / "empty"
        empty.mkdir()
        self.assertEqual(self.run_cli(SCENARIO, "validate", str(empty)).returncode, 0)
        self.assertEqual(self.run_cli(SCENARIO, "frobnicate").returncode, 2)

    def test_validate_cli_refuses_a_repeated_id_across_files(self):
        self.put("a.json", VALID, self.home / "dup")
        self.put("b.json", VALID, self.home / "dup")
        proc = self.run_cli(SCENARIO, "validate", str(self.home / "dup"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("also used by", proc.stderr)

    def test_validate_cli_defaults_to_the_repo_scenarios(self):
        proc = self.run_cli(SCENARIO, "validate")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sc.DEFAULT_DIR, REPO / "scenarios" / "switchboard")

    def test_list_and_coverage_cli(self):
        directory = self.home / "scenarios"
        for n, problem in enumerate((1, 2, 3)):
            self.put("a%d.json" % n, mutate(lambda o, n=n, p=problem: (o.update(id="A%d" % n, problem=p))))
        listing = self.run_cli(SCENARIO, "list", "--dir", str(directory))
        self.assertEqual(listing.returncode, 0)
        self.assertIn("A2", listing.stdout)
        self.assertEqual(self.run_cli(SCENARIO, "coverage", "--dir", str(directory)).returncode, 0)
        enforced = self.run_cli(SCENARIO, "coverage", "--enforce", "--dir", str(directory))
        self.assertEqual(enforced.returncode, 1)
        self.assertIn("problem(s) 4", enforced.stderr)
        self.put("a3.json", mutate(lambda o: o.update(id="A3", problem=4)))
        self.assertEqual(self.run_cli(SCENARIO, "coverage", "--enforce", "--dir", str(directory)).returncode, 0)
        self.put("bad.json", "{")
        self.assertEqual(self.run_cli(SCENARIO, "list", "--dir", str(directory)).returncode, 1)


class TestGithubRealChild(Base):
    """The one test that starts a real process: a script of our own, never a real `gh`."""

    def setUp(self):
        super().setUp()
        self.config = sb.validate_config({"github_repos": [GH_REPO], "gh": str(self.home / "gh")})
        self.state = sb.ensure_state_dir()

    def hub(self, body):
        self.fake_bin("gh", body)
        hub = sb.GitHubHub(self.config, self.state, now=time.time, runner=lambda *a, **k: self.fail("gh api"))
        hub.port = 4711
        self.addCleanup(hub.stop)
        return hub

    def wait_for(self, check):
        deadline = time.time() + 5
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_the_child_leads_its_own_session_and_writes_to_the_log_not_our_pipes(self):
        hub = self.hub('echo "forwarding $*" | sed "s/--secret=[0-9a-f]*/--secret=X/"; sleep 30\n')
        watch = hub.watches[GH_REPO]
        hub._start(watch, time.time())
        pid = watch.proc.pid
        self.assertEqual(os.getpgid(pid), pid, "its own process group: start_new_session")
        self.assertTrue(self.wait_for(lambda: "forwarding webhook forward" in hub._log_tail(watch)))
        self.assertEqual(stat.S_IMODE(hub.log_path(watch).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / "logs").stat().st_mode), 0o700)
        hub.stop()
        self.assertTrue(self.wait_for(lambda: watch.proc is None))
        self.assertTrue(self.wait_for(lambda: not self.alive(pid)))

    def alive(self, pid):
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            return waited == 0
        except ChildProcessError:
            return False

    def test_it_sees_hook_already_exists_from_a_real_process(self):
        hub = self.hub('echo "error: Hook already exists on fleet/scratch" >&2; exit 1\n')
        watch = hub.watches[GH_REPO]
        hub._start(watch, time.time())
        self.assertTrue(self.wait_for(lambda: watch.proc.poll() is not None))
        hub._exited(watch, watch.proc.poll(), time.time())
        self.assertEqual(watch.state, "conflict")

    def test_it_inherits_none_of_our_pipes(self):
        source = inspect.getsource(sb.spawn_forwarder)
        for needle in ("stdin=subprocess.DEVNULL", "stdout=fd", "stderr=fd", "start_new_session=True", "close_fds=True"):
            self.assertIn(needle, source)
        self.assertNotIn("PIPE", source)

    def test_a_missing_gh_is_a_backoff_not_a_crash(self):
        hub = sb.GitHubHub(sb.validate_config({"github_repos": [GH_REPO], "gh": "/no/such/gh"}), self.state)
        hub.port = 4711
        hub.tick()
        self.assertEqual(hub.watches[GH_REPO].state, "backoff")
        self.assertIn("cannot start", hub.watches[GH_REPO].last_error)


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


class TestPolicyCache(Base):
    def make(self, ttl=60, limit=3, name="c.json"):
        self.clock = SimClock()
        return sb.BoundedFileCache(self.home / name, self.clock.now, ttl, limit)

    def test_round_trip_expiry_and_privacy(self):
        cache = self.make()
        self.assertTrue(cache.put("k", {"answers": {"a": 0.1}}))
        self.assertEqual(cache.get("k"), {"answers": {"a": 0.1}})
        self.assertEqual(stat.S_IMODE(os.stat(str(self.home / "c.json")).st_mode), 0o600)
        self.clock.t += 59
        self.assertIsNotNone(cache.get("k"))
        self.clock.t += 2
        self.assertIsNone(cache.get("k"))

    def test_it_is_bounded_and_drops_the_oldest(self):
        cache = self.make(limit=3)
        for number in range(6):
            self.clock.t += 1
            cache.put("k%d" % number, number)
        data = json.loads((self.home / "c.json").read_text())["entries"]
        self.assertEqual(sorted(data), ["k3", "k4", "k5"])

    def test_expired_entries_are_not_kept_in_the_file(self):
        cache = self.make(ttl=10)
        cache.put("old", 1)
        self.clock.t += 11
        cache.put("new", 2)
        self.assertEqual(sorted(json.loads((self.home / "c.json").read_text())["entries"]), ["new"])

    def test_writes_are_atomic_and_leave_no_temp_file(self):
        cache = self.make()
        for number in range(5):
            cache.put("k%d" % number, number)
        self.assertEqual(sorted(p.name for p in self.home.glob("c.json*")), ["c.json"])

    def test_a_failed_write_is_not_an_error_and_cleans_up(self):
        cache = self.make(name="missing-dir/c.json")
        self.assertFalse(cache.put("k", 1))
        self.assertIsNone(cache.get("k"))

    def test_corrupt_oversized_or_wrongly_shaped_files_read_as_empty(self):
        cache = self.make()
        for text in ("{not json", "[]", '{"entries": []}', '{"entries": {"k": 5}}',
                     '{"entries": {"k": {"at": "x", "value": 1}}}', "x" * (sb.POLICY_CACHE_BYTES + 1)):
            with self.subTest(text=text[:30]):
                (self.home / "c.json").write_text(text)
                self.assertIsNone(cache.get("k"))
        self.assertTrue(cache.put("k", 1))  # and the next write replaces the bad file
        self.assertEqual(cache.get("k"), 1)

    def test_an_entry_from_the_future_is_not_trusted(self):
        cache = self.make()
        cache.put("k", 1)
        self.clock.t -= 1000
        self.assertIsNone(cache.get("k"))

    def test_a_ttl_of_zero_turns_it_off(self):
        cache = self.make(ttl=0)
        self.assertFalse(cache.put("k", 1))
        self.assertFalse((self.home / "c.json").exists())
        self.assertIsNone(cache.get("k"))


class OwnedInputs(WaitingInputs):
    """#874: the daemon's lists for a scene of an ask (#2), its work item (#3), a work item below it (#4) and an unrelated issue (#5). Ages are
    counted back from the moment of each read (the daemon's pass re-reads the lists every few minutes), so time can pass and the lists stay fresh. The
    pull request list is `ci_record_pass`'s over a fake hub that honours the cut and the failure; the issue list is the triage's."""
    OLD, RECENT = 10 * 3600, 600
    NO_HEAD = object()

    def wire_scene(self):
        self.scene = {"issues": {3: {"parent": 2}, 4: {"parent": 3}}, "prs": []}
        self.wire_inputs()

    def pr(self, number, body, age=None, base="main", created=None, head=None, **extra):
        return dict({"number": number, "body": body, "age": self.OLD if age is None else age, "created": 86400 if created is None else created, "base": base,
                     "head": "a" * 40 if head is None else head}, **extra)

    def plan(self, n):
        return "PLAN: https://github.com/%s/issues/%d\n" % (self.REPO, n)

    def set_scene(self, prs=None, issues=None, **issue_extra):
        if prs is not None:
            self.scene["prs"] = prs
        for n, fields in (issues or {}).items():
            self.scene["issues"].setdefault(n, {}).update(fields)
        self.apply()

    def apply(self):
        engine, scene = self.world.engine, self.scene
        now = self.world.clock.now()
        def row(n):
            f = scene["issues"].get(n, {})
            record = {"number": n, "updated_at": sb.iso(now - f.get("age", self.OLD)), "created_at": sb.iso(now - self.OLD),
                      "labels": [{"name": l} for l in f.get("labels", ())]}
            if "raw_time" in f:
                record["updated_at"] = f["raw_time"]
            if f.get("parent") is not None:
                record["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (f.get("parent_repo", self.REPO), f["parent"])
            return record
        engine.triage = sb.TriageState()
        engine.triage.units[(self.REPO, "issues")] = {"at": now, "data": [row(n) for n in range(1, 100)], "truncated": False, "error": None}
        sb.read_mark(engine, "issues:" + self.REPO, True, "", now)
        sb.read_mark(engine, "decisions", True, "", now)
        self.hub.records = [{"number": p["number"], "body": p["body"], "updated_at": p["age"][1] if isinstance(p["age"], tuple) else sb.iso(now - p["age"]), "created_at": sb.iso(now - p["created"]),
                             "head": {"sha": p["head"]}, "base": {"ref": p["base"]}, "labels": [{"name": l} for l in p.get("labels", ())],
                             **{k: v for k, v in p.items() if k in ("requested_reviewers", "requested_teams", "draft")}} for p in scene["prs"]]
        for record, p in zip(self.hub.records, scene["prs"]):
            if p["head"] is self.NO_HEAD:
                del record["head"]   # a record with no head at all

        engine.__dict__.pop("ci_record_state", None)
        engine.__dict__.get("read_whole", {}).pop("pulls:" + self.REPO, None)
        sb.ci_record_pass(engine, now)

    def owned(self):
        engine = self.world.engine
        records = engine.triage.units[(self.REPO, "issues")]["data"]
        pulls = engine.ci_record_state.lists[sb.plan_ledger.normal_repo(self.REPO)][1]
        return sb.worker_owned(records, pulls, 2, self.REPO)

    def owned_numbers(self):
        got = self.owned()
        return sorted(r["number"] for r in got.issues), sorted(r["number"] for r in got.pulls)


# ======================================================================
# #133: held-for-the-owner state as labels. Neutral names only: acme-org/widget-service, the owner.
# ======================================================================
class TestCaptainLabels(Base):
    U = "tester"   # the `<user>:` prefix of the labels

    def labels(self, *kinds):
        return ["%s:%s" % (self.U, k) for k in kinds]

    def window(self, until):
        return mock.patch.object(sb, "CAPTAIN_MARKER_UNTIL", until)

    def test_the_label_makes_a_pr_captain_with_no_first_line_at_all(self):
        self.assertTrue(sb.is_captain({"labels": self.labels("merge-captain"), "body": "Adds the widget."}))
        self.assertTrue(sb.is_captain({"labels": [{"name": self.labels("merge-captain")[0]}], "body": None}))   # label dicts too
        self.assertFalse(sb.is_captain({"labels": self.labels("hold-security"), "body": "x"}))          # a hold alone is not a merge class
        self.assertFalse(sb.is_captain({"labels": ["bug", "merge-captain-ish", "x:merge-captain-not"], "body": "x"}))
        self.assertFalse(sb.is_captain(None))

    def test_the_first_line_marker_counts_only_inside_the_dated_window_and_only_without_a_merge_label(self):
        pr = {"labels": [], "body": "\n\nMERGE: captain\nrest"}
        end = datetime.datetime.strptime(sb.CAPTAIN_MARKER_UNTIL, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp() + 86400
        self.assertTrue(sb.is_captain(pr, now=end - 60))                      # the last minute of the window
        self.assertFalse(sb.is_captain(pr, now=end + 60))                     # the day after: the line is no longer read
        with self.window(None):                                               # the one-line way to drop the marker
            self.assertFalse(sb.is_captain(pr))
            self.assertTrue(sb.is_captain(dict(pr, labels=self.labels("merge-captain"))))
        for body in ("see MERGE: captain", "intro\nMERGE: captain", "MERGE: captainship", "merge: captain"):
            self.assertFalse(sb.is_captain({"labels": [], "body": body}, now=end - 60), body)

    def test_the_window_ends_30_days_after_the_labels_landed_on_2026_10_11(self):
        self.assertEqual(sb.CAPTAIN_MARKER_UNTIL, "2026-11-10")           # what the definitions tell agents and the owner approved
        landed = datetime.datetime(2026, 10, 11, tzinfo=datetime.timezone.utc)
        end = datetime.datetime.strptime(sb.CAPTAIN_MARKER_UNTIL, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        self.assertEqual((end - landed).days, 30)
        pr = {"labels": [], "body": "MERGE: captain\nx"}
        day = lambda y, m, d, h=12: datetime.datetime(y, m, d, h, tzinfo=datetime.timezone.utc).timestamp()
        self.assertTrue(sb.is_captain(pr, now=day(2026, 11, 10)))          # the last day still reads the line
        self.assertFalse(sb.is_captain(pr, now=day(2026, 11, 11, 1)))      # the day after it does not

    def test_the_label_wins_when_it_disagrees_with_the_line(self):
        auto = {"labels": self.labels("merge-automatic"), "body": "MERGE: captain\nx"}
        self.assertFalse(sb.is_captain(auto))                                  # the label says automatic: not held
        held = {"labels": self.labels("merge-captain"), "body": "MERGE: automatic\nx"}
        self.assertTrue(sb.is_captain(held))
        self.assertEqual(sb.pr_marker_state(auto["labels"], auto["body"])["findings"], ["disagree"])
        self.assertEqual(sb.pr_marker_state(held["labels"], held["body"])["findings"], ["disagree"])
        both = sb.pr_marker_state(self.labels("merge-captain", "merge-automatic"), "x")
        self.assertEqual((both["captain"], both["findings"]), (True, ["disagree"]))   # two merge labels: held, and said so
        agree = sb.pr_marker_state(self.labels("merge-captain"), "MERGE: captain\nx")
        self.assertEqual((agree["captain"], agree["findings"]), (True, []))
        line_only = sb.pr_marker_state([], "MERGE: captain\nx")
        self.assertEqual((line_only["captain"], line_only["findings"]), (True, ["marker-without-label"]))

    def test_hold_reasons_come_from_the_hold_labels_only_and_sorted(self):
        self.assertEqual(sb.pr_hold_reasons(self.labels("hold-security", "merge-captain", "hold-contract", "hold-other-area")),
                         ["contract", "other-area", "security"])
        self.assertEqual(sb.pr_hold_reasons(["bug", None, 5]), [])
        self.assertEqual(sb.pr_hold_reasons(self.labels("hold-impact", "merge-captain")), ["impact"])   # #179 L4: the same held-PR label set
        self.assertTrue(sb.pr_marker_state(self.labels("merge-captain", "hold-impact"), "")["captain"])

    def test_the_pr_facts_use_the_labels_and_carry_the_holds(self):
        data = dict(json.loads(pr_view_json()), body="plain", headRefOid="a" * 40, author={"login": "coder-bot"},
                    labels=[{"name": n} for n in self.labels("merge-captain", "hold-credential")])
        facts = sb.pr_facts_from(data)
        self.assertEqual((facts["captain"], facts["holds"], facts["captain_findings"]), (True, ["credential"], []))
        self.assertIn("labels", sb.PR_VIEW_FIELDS)
        old = sb.pr_facts_from(dict(data, labels=[], body="MERGE: captain\nx"))
        self.assertEqual((old["captain"], old["captain_findings"]), (True, ["marker-without-label"]))     # still works in the window
        self.assertFalse(sb.pr_facts_from(dict(data, labels=[], body="plain"))["captain"])

    def test_every_caller_reads_the_one_helper(self):
        text = switchboard_source()
        callers = [l for l in text.splitlines() if "pr_is_captain(" in l and not l.lstrip().startswith(("def ", "return pr_marker"))]
        self.assertEqual(callers, [], "a caller still reads the first line alone: %r" % callers)


class TestLedgerReadRace(unittest.TestCase):
    """A ledger read that races an append is not a cut-off (#673); a real cut-off or rewrite is still reported."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = 1767225600
        for n in (1, 2):
            self.append(n)

    def append(self, n):
        return sb.plan_ledger.append_entry(self.dir, {"kind": "proof", "pr": "acme/api#1", "scenario": "S%d" % n, "result": "pass"}, self.now)

    def read_with_append_after_file_read(self):
        """Read the ledger; the moment its file has been read, a writer completes an append (entry and head pointer)."""
        real, fired = Path.read_text, []

        def hooked(path, *a, **kw):
            text = real(path, *a, **kw)
            if path.name == sb.plan_ledger.LEDGER_FILE and not fired:
                fired.append(1)
                self.append(3)
            return text
        with mock.patch.object(Path, "read_text", hooked):
            result = sb.plan_ledger.read_ledger(self.dir)
        self.assertTrue(fired, "the interleaving hook ran")
        return result

    def test_s1_a_read_that_races_an_append_reports_no_break(self):
        entries, problem = self.read_with_append_after_file_read()
        self.assertIsNone(problem)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(sb.plan_ledger.read_ledger(self.dir)[0]), 3)

    def test_s4_a_racing_read_of_a_long_ledger_answers_like_a_quiet_read(self):
        sb.plan_ledger.append_many(self.dir, [{"kind": "proof", "pr": "acme/api#1", "scenario": "S%d" % n, "result": "pass"}
                                              for n in range(3, 1103)], self.now)
        quiet = sb.plan_ledger.read_ledger(self.dir)
        self.assertEqual((len(quiet[0]), quiet[1]), (1102, None))
        self.append(1103)
        # the same interleaving, on 1000+ entries: an append completes right after the file was read
        real, fired = Path.read_text, []

        def hooked(path, *a, **kw):
            text = real(path, *a, **kw)
            if path.name == sb.plan_ledger.LEDGER_FILE and not fired:
                fired.append(1)
                self.append(1104)
            return text
        with mock.patch.object(Path, "read_text", hooked):
            raced = sb.plan_ledger.read_ledger(self.dir)
        self.assertEqual((len(raced[0]), raced[1]), (1103, None))

    def test_s2_a_cut_off_ledger_still_reports_the_break(self):
        self.append(3)
        path = Path(self.dir) / sb.plan_ledger.LEDGER_FILE
        path.write_text("".join(path.read_text().splitlines(True)[:2]))
        self.assertIn("ends before entry 3", sb.plan_ledger.read_ledger(self.dir)[1])

    def test_s5_a_rewritten_middle_entry_is_reported_with_the_same_message(self):
        self.append(3)
        path = Path(self.dir) / sb.plan_ledger.LEDGER_FILE
        lines = path.read_text().splitlines()
        lines[1] = json.dumps(dict(json.loads(lines[1]), result="fail"), sort_keys=True)
        path.write_text("\n".join(lines) + "\n")
        self.assertEqual(sb.plan_ledger.read_ledger(self.dir)[1], "entry 2 does not follow from the one before it: the ledger was edited")

    def test_s3_a_rewritten_ledger_still_reports_the_break(self):
        path = Path(self.dir) / sb.plan_ledger.LEDGER_FILE
        forged = dict(json.loads(path.read_text().splitlines()[0]), result="fail")
        forged["hash"] = sb.plan_ledger.entry_hash(forged)
        path.write_text(json.dumps(forged, sort_keys=True) + "\n")
        self.assertIsNotNone(sb.plan_ledger.read_ledger(self.dir)[1])


class RecFacts(dict):
    """(#986) A facts mapping that RECORDS every key touched, by any spelling; an access it cannot attribute to a key records '*'."""

    seen = None   # set by the test: the one shared set of keys
    quiet = False   # true while the proxy is built from a picture: that copy is not a read

    def _note(self, key):
        if not RecFacts.quiet:
            RecFacts.seen.add(key)

    def __getitem__(self, key):
        self._note(key)
        return dict.__getitem__(self, key)

    def __setitem__(self, key, value):
        self._note(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._note(key)
        dict.__delitem__(self, key)

    def __contains__(self, key):
        self._note(key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        self._note(key)
        return dict.get(self, key, default)

    def pop(self, key, *default):
        self._note(key)
        return dict.pop(self, key, *default)

    def setdefault(self, key, default=None):
        self._note(key)
        return dict.setdefault(self, key, default)

    def update(self, *a, **k):
        RecFacts.seen.update(dict(*a, **k))
        RecFacts.seen.add("*")
        dict.update(self, *a, **k)

    def _star(self):
        if not RecFacts.quiet:
            RecFacts.seen.add("*")

    def items(self):
        self._star()
        return dict.items(self)

    def keys(self):
        self._star()
        return dict.keys(self)

    def values(self):
        self._star()
        return dict.values(self)

    def __iter__(self):
        self._star()
        return dict.__iter__(self)

    def copy(self):
        self._star()
        return RecFacts(dict.copy(self))

    def __or__(self, other):
        self._star()
        return dict.__or__(self, other)

    def __reversed__(self):
        self._star()
        return dict.__reversed__(self)

    def __eq__(self, other):
        self._star()
        return dict.__eq__(self, other)

    __hash__ = None

    def __repr__(self):
        self._star()
        return dict.__repr__(self)


class TestPlanCheckParser(unittest.TestCase):
    """#135: the strict yaml-subset parser, on synthetic blocks."""

    def parse(self, block):
        return sb.plan_check.parse_scenarios(block.strip("\n").splitlines())

    def refused(self, block, fragment):
        with self.assertRaises(sb.plan_check.PlanFormatError) as caught:
            self.parse(block)
        self.assertIn(fragment, str(caught.exception))

    def test_quotes_comments_and_plain_values(self):
        got = self.parse("""
scenarios:
  - id: S1            # a comment
    text: "say \\"hi\\" # not a comment"
    pass: 'it''s 5 s' # a comment
    covers: DW-1
  # a whole-line comment

  - id: S2
    text: plain with: a colon inside
""")
        self.assertEqual(got[0], {"id": "S1", "text": 'say "hi" # not a comment', "pass": "it's 5 s", "covers": "DW-1"})
        self.assertEqual(got[1]["text"], "plain with: a colon inside")

    def test_a_value_continues_on_deeper_lines_and_block_scalars_work(self):
        got = self.parse("""
scenarios:
  - id: S1
    text: first part
      second part
        third part
    pass: >
      folded one
      folded two
    discriminates: |
      literal one
      literal two
    covers: DW-1
""")
        self.assertEqual(got[0]["text"], "first part second part third part")
        self.assertEqual(got[0]["pass"], "folded one folded two")
        self.assertEqual(got[0]["discriminates"], "literal one\nliteral two")
        self.assertEqual(got[0]["covers"], "DW-1")

    def test_each_thing_outside_the_subset_is_refused_with_a_reason(self):
        for block, fragment in (
                ("scenarios:\n  - id: S1\n\ttext: x", "tab"),
                ("scenarios:\n  - id: S1\n    id: S2", "twice"),
                ("scenarios:\n  - id: S1\n    text: [a, b]", "quote it"),
                ("scenarios:\n  - id: S1\n    text: &anchor x", "quote it"),
                ("scenarios:\n  - id: S1\n    text: !tag x", "quote it"),
                ("scenarios:\n  - id: S1\n    text: {a: b}", "quote it"),
                ("scenarios:\n  - id: S1\n    text: > 5 s", "quote it"),
                ("scenarios:\n  - id: S1\n    text: 'open", "unterminated"),
                ('scenarios:\n  - id: S1\n    text: "open', "unterminated"),
                ('scenarios:\n  - id: S1\n    text: "bad \\x escape"', "escapes"),
                ("scenarios:\n  - id: S1\n    sub:\n      nested: 1", "nested value"),
                ("scenarios:\n  - id: S1\n  - - nested", "nested list"),
                ("scenarios:\n---\n  - id: S1", "document"),
                ("scenarios:", "no items"),
                ("other:\n  - id: S1", "start with `scenarios:`"),
                ("scenarios:\n  - id: S1\n   text: off by one", "column"),
                ("scenarios:\n  - just words", "key: value")):
            with self.subTest(block=block):
                self.refused(block, fragment)

    def test_size_limits(self):
        with self.assertRaises(sb.plan_check.PlanFormatError):
            sb.plan_check.parse_scenarios(["scenarios:", "  - id: S1", "    text: " + "x" * (sb.plan_check.MAX_BYTES + 1)])
        with self.assertRaises(sb.plan_check.PlanFormatError):
            sb.plan_check.parse_scenarios(["scenarios:", "  - id: S1", "    text: " + "x" * (sb.plan_check.MAX_LINE + 1)])
        many = ["scenarios:"] + ["  - id: S%d" % n for n in range(sb.plan_check.MAX_SCENARIOS + 1)]
        with self.assertRaises(sb.plan_check.PlanFormatError):
            sb.plan_check.parse_scenarios(many)

    def test_exactly_one_scenarios_block_is_wanted(self):
        pc = sb.plan_check
        with self.assertRaises(pc.PlanFormatError):
            pc.scenario_block("no fence here")
        with self.assertRaises(pc.PlanFormatError):
            pc.scenario_block("```yaml\nother: 1\n```")
        two = "```yaml\nscenarios:\n  - id: S1\n```\n\n~~~\nscenarios:\n  - id: S2\n~~~"
        with self.assertRaises(pc.PlanFormatError):
            pc.scenario_block(two)
        mixed = "```yaml\nstatus: active\n```\n```yaml\n# note\nscenarios:\n  - id: S1\n```"
        self.assertEqual(pc.scenario_block(mixed)[-1], "  - id: S1")


class TestPlanCheckRound2(unittest.TestCase):
    """#135 round 2: false positives from a real 52-scenario plan, reproduced with paraphrased, synthetic text."""

    def test_zero_and_exact_counts_of_any_noun_are_observables(self):
        for rule in ("the agent sends 0 accepts to the boss", "0 cancellations originating from the agent appear in the log",
                     "the reader received 0 responses from the queue", "the summary shows (0 new decisions, 1 accept)",
                     "the run asserts 0 tool calls and 0 facts", "the job makes exactly 3 retries before it stops",
                     "the page lists exactly 2 open questions"):
            with self.subTest(rule=rule):
                self.assertIsNone(sb.plan_check.pass_rule_problem(rule))
        for rule in ("after the change, 1 thing is fine", "the thing works after 1 try", "works for 0 users happily today",
                     "the agent has 3 accepts and that is fine", "0 hits yes fine ok"):
            with self.subTest(still_vague=rule):
                self.assertIsNotNone(sb.plan_check.pass_rule_problem(rule))

    def test_a_status_class_counts_like_a_status(self):
        for rule in ("the endpoint returns a 4xx for a bad token", "the call answers 5xx under load",
                     "a missing record gives a 404 and a body naming the id", "the reply status is 2xx"):
            with self.subTest(rule=rule):
                self.assertIsNone(sb.plan_check.pass_rule_problem(rule))
        self.assertIsNotNone(sb.plan_check.pass_rule_problem("the endpoint returns some kind of error class"))

    def test_byte_equal_phrases_are_strong(self):
        for rule in ("yields identical output (byte equal)", "the two outputs are byte for byte the same file",
                     "the exported file is byte-for-byte the stored copy", "the output is byte-equal to the golden file"):
            with self.subTest(rule=rule):
                self.assertIsNone(sb.plan_check.pass_rule_problem(rule))

    GOOD_WRONG_BUILDS = (
        "a build that drops the retry queue sends the message twice and the count reads 2",
        "a build that skips the dedupe step finds two rows for one key",
        "a build that ignores the header changes the status to the default value",
        "an implementation that caches the first reply returns a stale value on the second call",
        "a handler that only logs the event never queues the follow-up job",
        "a version that loses the draft keeps the old title after a restart")
    BAD_WRONG_BUILDS = ("a bad build", "a build that fails", "a wrong build would fail this", "the scenario would fail",
                        "fails", "it is wrong", "n/a", "a broken implementation")

    def test_a_concrete_wrong_build_is_recognised_by_structure_not_a_verb_list(self):
        pass_rule = "the list call returns status 200 and exactly 1 agent named alpha"
        for text in self.GOOD_WRONG_BUILDS:
            with self.subTest(good=text):
                self.assertIsNone(sb.plan_check.discriminates_problem(text, pass_rule))
        for text in self.BAD_WRONG_BUILDS:
            with self.subTest(bad=text):
                self.assertIsNotNone(sb.plan_check.discriminates_problem(text, pass_rule))

    def test_overlap_with_the_pass_rule_warns_and_only_a_near_copy_fails(self):
        pc = sb.plan_check
        pass_rule = "the list call returns status 200 and exactly 1 agent named alpha"
        some = "a build that returns status 200 and the list call lists exactly 2 agents named alpha"
        self.assertIsNone(pc.discriminates_problem(some, pass_rule), "a wrong build is the right one with a thing changed")
        self.assertIsNotNone(pc.discriminates_overlap_warning(some, pass_rule))
        near = "the list call returns status 200 and not exactly 1 agent named alpha"
        self.assertIn("reworded", pc.discriminates_problem(near, pass_rule))
        self.assertIsNone(pc.discriminates_overlap_warning("a stub that returns an empty list for every project", pass_rule))
        scenario = dict(id="S1", covers="DW-1", text="a scenario that is long enough", proof="test", evaluator="ci",
                        **{"pass": pass_rule, "discriminates": some})
        defects, warnings = pc.validate([scenario], ["a"], "unchanged: none because this is only a unit test")
        self.assertEqual([d["code"] for d in defects], [])
        self.assertEqual([w["code"] for w in warnings], ["discriminates-overlap"])


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


class TestMultiRepoConfig(Base):
    def cfg(self, **raw):
        return sb.validate_config(raw)

    def test_defaults(self):
        config = self.cfg()
        self.assertEqual((config["gh_users"], config["github_poll_seconds"]), ({}, 60))

    def test_good_gh_users(self):
        config = self.cfg(gh_users={"acme/api": "alice-bot", "acme/*": "acme-ci"}, github_poll_seconds=30)
        self.assertEqual(config["gh_users"], {"acme/api": "alice-bot", "acme/*": "acme-ci"})
        self.assertIsNot(config["gh_users"], self.cfg(gh_users=config["gh_users"])["gh_users"])

    def test_bad_values_are_refused_by_name(self):
        for key, value in (("gh_users", ["a"]), ("gh_users", {"acme": "x"}), ("gh_users", {"acme/api": ""}),
                           ("gh_users", {"acme/api": 3}), ("gh_users", {"*/api": "x"}),
                           ("gh_users", {"acme/api": "--token"}), ("gh_users", {"acme/a b": "x"}),
                           ("github_poll_seconds", 0), ("github_poll_seconds", True), ("github_poll_seconds", "60"),
                           ("github_poll_seconds", 5)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(sb.ConfigError) as caught:
                    self.cfg(**{key: value})
                self.assertIn(key, str(caught.exception))

    def test_the_account_is_the_repo_entry_then_the_owner_entry(self):
        accounts = sb.GhAccounts(self.cfg(gh_users={"acme/api": "alice-bot", "acme/*": "acme-ci"}), FakeRunner())
        self.assertEqual(accounts.account_for("acme/api"), "alice-bot")
        self.assertEqual(accounts.account_for("acme/web"), "acme-ci")
        self.assertIsNone(accounts.account_for("other/x"))

    def test_an_underscore_login_is_an_account(self):
        config = self.cfg(gh_users={"acme-infra/*": "alice_corp"})
        self.assertEqual(config["gh_users"], {"acme-infra/*": "alice_corp"})
        self.assertEqual(sb.GhAccounts(config, FakeRunner()).account_for("acme-infra/x"), "alice_corp")

    def test_an_underscore_login_is_an_ignored_author(self):
        config = self.cfg(github_ignore_authors=["alice_corp"])
        self.assertEqual(config["github_ignore_authors"], ["alice_corp"])

    def test_a_launch_into_acme_infra_uses_the_underscore_account(self):
        config = self.cfg(gh_users={"acme-infra/*": "alice_corp"})
        runner = FakeRunner(default=(0, "tok\n", ""))
        accounts = sb.GhAccounts(config, runner, environ={})
        self.assertIsNone(sb.launch_account_problem(config, accounts, "acme-infra/x"))
        accounts.token_for(accounts.account_for("acme-infra/x"))
        self.assertEqual(runner.argv, [config["gh"], "auth", "token", "--user", "alice_corp"])

    def test_logins_that_are_still_refused(self):
        for bad in ("--token", "_x", "a b"):
            for key, value in (("gh_users", {"acme/api": bad}), ("github_ignore_authors", [bad])):
                with self.subTest(login=bad, key=key):
                    with self.assertRaises(sb.ConfigError):
                        self.cfg(**{key: value})

    def test_the_watched_set_is_the_baseline_and_every_live_agents_repo_and_charter(self):
        config = self.cfg(github_repos=["acme/api"], intent_repo="acme/ops")
        agents = {"a": {"repo": "acme/web"}, "b": {"charter": 1}, "c": {"role": "coder"}, "d": {"repo": "acme/api"}}
        self.assertEqual(sb.watched_repos(config, agents), ["acme/api", "acme/ops", "acme/web"])
        self.assertEqual(sb.watched_repos(self.cfg(), {"c": {"role": "coder"}}), [])

    def test_the_static_repos_fault_reads_only_the_baseline(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "static-repos"
        config = self.cfg(github_repos=["acme/api"])
        self.assertEqual(sb.watched_repos(config, {"a": {"repo": "acme/web"}}), ["acme/api"])

    def test_the_four_faults_are_known(self):
        for name in ("static-repos", "bare-issue-number", "one-account", "repo-create-allowed"):
            self.assertIn(name, sb.KNOWN_FAULTS)


class TestKeyCommand(KeyCase):
    def run_key(self, *args, stdin=PR16_KEY + "\n"):
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), captured() as (out, err):
            code = sb.main(["key"] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_set_saves_the_key_as_a_private_file_and_prints_only_that(self):
        code, out, err = self.run_key("set")
        self.assertEqual((code, out, err), (0, "key saved\n", ""))
        self.assertEqual(self.key_file.read_text(), PR16_KEY + "\n")
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(str(self.key_file.parent))), ["jev.key"])

    def test_set_replaces_an_old_key_and_leaves_no_temporary_file(self):
        self.run_key("set", stdin="old-key-value\n")
        self.run_key("set")
        self.assertEqual(self.key_file.read_text(), PR16_KEY + "\n")
        self.assertEqual(sorted(os.listdir(str(self.key_file.parent))), ["jev.key"])

    def test_set_is_atomic_a_failed_write_keeps_the_old_key(self):
        self.run_key("set", stdin="old-key-value\n")
        with mock.patch.object(sb.os, "replace", side_effect=OSError("disk gone")):
            code, out, err = self.run_key("set")
        self.assertEqual(code, 1)
        self.assertEqual(self.key_file.read_text(), "old-key-value\n")
        self.assertEqual(sorted(os.listdir(str(self.key_file.parent))), ["jev.key"])
        self.assertNotIn(PR16_KEY, out + err)

    def test_set_from_a_terminal_reads_without_echo(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        with mock.patch.object(sys, "stdin", Tty("")), \
                mock.patch.object(sb.getpass, "getpass", return_value=PR16_KEY) as asked, captured() as (out, err):
            code = sb.main(["key", "set"])
        self.assertEqual((code, out.getvalue()), (0, "key saved\n"))
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(self.key_file.read_text(), PR16_KEY + "\n")

    def test_set_refuses_nothing_at_all_and_a_key_with_spaces_without_echoing_either(self):
        for text in ("", "\n", "two words\n"):
            with self.subTest(text=text):
                code, out, err = self.run_key("set", stdin=text)
                self.assertEqual(code, 2)
                self.assertFalse(self.key_file.exists())
                self.assertNotIn("two words", out + err)

    def test_the_key_is_never_an_argument_and_there_is_no_show(self):
        for argv in (["key", "set", PR16_KEY], ["key", "show"], ["key", "set", "--key", PR16_KEY]):
            with self.subTest(argv=argv), captured():
                self.assertEqual(sb.main(argv), 2)

    def test_status_says_file_environment_or_none(self):
        self.assertEqual(self.run_key("status")[1], "none\n")
        self.put_key()
        self.assertEqual(self.run_key("status")[1], "file\n")
        os.environ["OPENROUTER_API_KEY"] = "v"
        self.assertEqual(self.run_key("status")[1], "environment\n", "a saved file too: not only this shell")
        self.key_file.unlink()
        self.assertEqual(self.run_key("status")[1], "environment (only this shell)\n")

    def test_status_of_a_refused_file_says_none_and_why_on_stderr(self):
        self.put_key(mode=0o644)
        code, out, err = self.run_key("status")
        self.assertEqual((code, out), (1, "none\n"))
        self.assertIn("0600", err)
        self.assertNotIn(PR16_KEY, out + err)

    def test_set_writes_to_the_path_the_config_names(self):
        other = self.home / "else" / "my.key"
        self.write_config({"jev": {"key_file": str(other)}})
        self.assertEqual(self.run_key("set")[0], 0)
        self.assertEqual(other.read_text(), PR16_KEY + "\n")
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o600)


class TestTheReapersCommandPathIsWalked(InstallCase):
    """The service runs one file: the launcher's own walk (one copy, in cloud_worker.py) judges it and every parent above it."""

    def kit(self):
        root = Path(os.path.realpath(str(self.home))) / "kit"
        shutil.copytree(REPO / "cloud" / "launcher", root / "cloud" / "launcher", ignore=shutil.ignore_patterns("tests", "__pycache__"), symlinks=True)
        shutil.copytree(REPO / "templates", root / "templates")
        for path in [root] + [p for p in root.rglob("*")]:
            os.chmod(str(path), 0o755 if path.is_dir() or os.access(str(path), os.X_OK) else 0o644)
        return root

    def problem(self, root):
        installer = sb.Installer(self.machine, self.daemons, time.time, "darwin", root, dry_run=True, out=io.StringIO(),
                                 shared_profile=False, profile=None, gh_user=None)
        return installer.reaper_command_problem()

    def test_a_clean_copy_passes_and_each_bad_state_is_named(self):
        root = self.kit()
        self.assertIsNone(self.problem(root))
        script = root / "cloud" / "launcher" / "cloud_worker.py"
        rows = {
            "the file is group-writable": (lambda: os.chmod(str(script), 0o664), lambda: os.chmod(str(script), 0o644), "group- or world-writable"),
            "the launcher directory is group-writable": (lambda: os.chmod(str(script.parent), 0o775), lambda: os.chmod(str(script.parent), 0o755), "parent that is group- or world-writable"),
            "the kit root is group-writable": (lambda: os.chmod(str(root), 0o775), lambda: os.chmod(str(root), 0o755), "parent that is group- or world-writable"),
        }
        for name, (make, undo, reason) in rows.items():
            with self.subTest(row=name):
                make()
                self.assertIn(reason, self.problem(root) or "")
                undo()
        self.assertIsNone(self.problem(root))

    def test_the_file_being_judged_is_never_run(self):
        root = self.kit()
        script = root / "cloud" / "launcher" / "cloud_worker.py"
        marker = root / "ran"
        script.write_text("open(%r, 'w').write('ran')\n" % str(marker))
        os.chmod(str(script), 0o666)   # writable by others: it must be refused, and not imported on the way
        self.assertIn("group- or world-writable", self.problem(root) or "")
        self.assertFalse(marker.exists())

    def test_a_symlinked_parent_is_named(self):
        root = self.kit()
        real = root / "elsewhere"
        (root / "cloud" / "launcher").rename(real)
        os.symlink(str(real), str(root / "cloud" / "launcher"))
        self.assertIn("symlink", self.problem(root) or "")

    def test_a_symlinked_file_or_parent_is_named(self):
        root = self.kit()
        script = root / "cloud" / "launcher" / "cloud_worker.py"
        real = root / "elsewhere.py"
        script.rename(real)
        os.symlink(str(real), str(script))
        self.assertIn("symlink", self.problem(root) or "")

    def test_the_install_refuses_before_writing_when_the_path_is_bad(self):
        with mock.patch.object(sb.Installer, "reaper_command_problem", return_value="has a parent that is group- or world-writable"):
            out, error = self.install("--opencode", self.machine.binary, "--with-cloud-reaper")
        self.assertIn("cloud_worker.py", error)
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.fleet-kit.cloud-reaper.plist").exists())
        self.assertFalse([c for c in self.machine.calls if "cloud-reaper" in " ".join(c) and c[1] == "bootstrap"])


class TestInstallOwnsWhatAPaneRuns(InstallCase):
    """`opencode` and `opencode_executable` are the two keys install owns: a config from an earlier install, or an
    older binary, no longer survives it."""

    def full(self, *flags, **kw):
        return self.install("--opencode", self.machine.binary, *flags, **kw)

    def config(self):
        return json.loads(self.config_file.read_text())

    def stale(self):
        self.write_config({"opencode": "/old/place/fleet-opencode", "opencode_executable": "/old/place/opencode",
                           "poll_seconds": 7, "github_repos": ["acme/api"], "jev": {"model": "mine/model"},
                           "policy": {"enabled": False}})

    def test_a_stale_pair_is_replaced_and_says_what_it_was(self):
        self.stale()
        out, error = self.full()
        self.assertIsNone(error, error)
        config = self.config()
        self.assertEqual((config["opencode"], config["opencode_executable"]), (str(self.wrapper), self.machine.binary))
        self.assertIn("updated", out)
        self.assertIn("(was /old/place/fleet-opencode)", out)
        self.assertIn("(was /old/place/opencode)", out)

    def test_no_other_key_is_touched(self):
        self.stale()
        self.full()
        config = self.config()
        self.assertEqual((config["poll_seconds"], config["github_repos"], config["jev"], config["policy"]),
                         (7, ["acme/api"], {"model": "mine/model"}, {"enabled": False}))
        self.assertEqual([k for k in config][:6], ["opencode", "opencode_executable", "poll_seconds", "github_repos", "jev", "policy"])

    def test_the_second_run_finds_nothing_to_update(self):
        self.stale()
        self.full()
        text = self.config_file.read_text()
        out, _ = self.full()
        self.assertEqual(self.statuses(out)["config"], ["already done"])
        self.assertNotIn("updated", out)
        self.assertEqual(self.config_file.read_text(), text)

    def test_a_dry_run_says_it_would_update_and_writes_nothing(self):
        self.stale()
        before = self.config_file.read_text()
        out, _ = self.full("--dry-run")
        self.assertEqual(self.config_file.read_text(), before)
        self.assertEqual(self.statuses(out)["config"], ["would be done"])
        self.assertIn("updated", out)

    def test_a_shared_profile_has_no_wrapper_and_both_keys_are_the_binary_as_given_not_resolved(self):
        real = self.home / "Cellar" / "opencode" / "bin" / "opencode"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n")
        link = self.home / "opt" / "bin" / "opencode"
        link.parent.mkdir(parents=True)
        os.symlink(str(real), str(link))
        self.machine.binary = str(link)
        self.stale()
        out, error = self.full("--shared-profile")
        self.assertIsNone(error, error)
        config = self.config()
        self.assertEqual((config["opencode"], config["opencode_executable"]), (str(link), str(link)))
        self.assertNotIn("Cellar", config["opencode"] + config["opencode_executable"])
        self.assertNotIn("launch_env", config)

    def test_a_shared_profile_install_writes_no_wrapper_and_says_so(self):
        out, error = self.full("--shared-profile")
        self.assertIsNone(error, error)
        self.assertFalse(self.wrapper.exists())
        self.assertFalse(self.wrapper.parent.exists() and list(self.wrapper.parent.iterdir()))
        self.assertTrue(self.statuses(out)["wrapper"][0].startswith("skipped"), out)
        again, _ = self.full("--shared-profile")
        self.assertFalse(self.wrapper.exists())

    def test_a_private_install_keeps_the_wrapper_and_launch_env_which_the_wrapper_needs(self):
        out, error = self.full()
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(self.config()["launch_env"], {sb.PANE_ENV: "1"})
        self.assertIn("pane_env", inspect.getsource(sb.Installer.wrapper_text), "launch_env only matters to that wrapper")

    def test_uninstall_still_removes_a_wrapper_an_earlier_install_made(self):
        self.full()
        self.assertTrue(self.wrapper.is_file())
        out, error = self.install("--uninstall", "--shared-profile")
        self.assertIsNone(error, error)
        self.assertFalse(self.wrapper.exists())


class TestInstallDocs(unittest.TestCase):
    def setUp(self):
        self.install = (REPO / "INSTALL.md").read_text()
        self.design = (REPO / "docs" / "switchboard.md").read_text()
        self.trial = (REPO / "docs" / "switchboard-trial.md").read_text()
        start = self.install.index("## The Fleet Switchboard")
        self.section = self.install[start:self.install.index("## When you finish")]

    def test_the_install_guide_has_the_switchboard_steps_in_its_own_style(self):
        for number in range(14, 20):
            self.assertRegex(self.section, r"(?m)^## STEP %d " % number)
        for label in ("CHECK:", "DO:", "VERIFY:", "PROVES:", "HUMAN:"):
            self.assertGreaterEqual(self.section.count(label), 6 if label != "HUMAN:" else 3, label)
        for needle in ("auth login", "key set", "bootstrap", "install --dry-run", "herdr plugin list", "opencode v2"):
            self.assertIn(needle, self.section)
        flat = " ".join(self.section.split())
        for needle in ("puts the CLI on PATH", "~/.local/bin", "moved or deleted checkout", "updated ... (was <old>)",
                       "only in the environment", "environment (only this shell)", "sends the brief once"):
            self.assertIn(needle, flat)
        last = self.section[self.section.index("## STEP 19"):]
        for line in ("daemon", "key", "plugin", "service"):
            self.assertIn("`%s`" % line, last)
        self.assertIn("fleet-switchboard status", last)

    def test_the_install_guide_says_the_profile_is_private_and_uses_the_wrapper(self):
        for needle in ("private v2 profile", "--shared-profile", "fleet-opencode", "~/.config/opencode", "dry run prints the profile path"):
            self.assertIn(needle, " ".join(self.section.split()))
        self.assertNotIn("fleet-switchboard/bin/opencode", self.install)
        for line in self.section.splitlines():
            if "auth login" in line or "auth list" in line:
                self.assertIn("fleet-opencode", line)

    def test_the_existing_steps_stay(self):
        for number in range(1, 14):
            self.assertRegex(self.install, r"(?m)^## STEP %d " % number)

    def test_the_new_part_is_clean_of_names_home_paths_secrets_and_emoji(self):
        for number, line in enumerate(self.section.splitlines(), 1):
            self.assertIsNone(FORBIDDEN_IN_PUBLIC_TEXT.search(line), "INSTALL.md section:%d %s" % (number, line))
            self.assertTrue(all(ord(ch) < 0x2600 for ch in line), "a symbol in: %s" % line)
        for fragment in ("sk-", "bearer ", "password=", "token="):
            self.assertNotIn(fragment, self.section.lower())

    def test_every_command_the_install_guide_shows_exists(self):
        parser = sb.build_parser()
        commands = set(parser._subparsers._group_actions[0].choices)
        for command in re.findall(r"fleet-switchboard ([a-z][a-z-]+)", self.section):
            self.assertIn(command, commands | {"is", "are", "says", "begins", "key"}, command)
        for flag in ("--opencode", "--no-service", "--no-herdr-link", "--dry-run", "--uninstall"):
            self.assertIn(flag, parser._subparsers._group_actions[0].choices["install"].format_help())

    def test_the_design_doc_has_the_status_row_the_catalog_and_the_log(self):
        self.assertRegex(self.design, r"(?m)^\| 16 \| `switchboard/install` \| .+ \| ready for review \|$")
        self.assertRegex(self.design, r"(?m)^- 2026-10-06: PR 16, Install\.")
        for scenario in ("E1", "E2", "E3"):
            self.assertRegex(self.design, r"(?m)^\| %s \|" % scenario)
        for needle in ("### Install (PR 16)", "shared-profile-default", "--shared-profile", "fleet-opencode", "Found in review",
                       "herdr integration", "private profile", "status_first_line", "key-missing-silent", "no-ensure-on-use", "bootstrap", "launchctl bootstrap gui/",
                       "systemctl --user enable --now", "key set", "ensure_daemon", "Lab proof", "Not verified live",
                       "--standby", "`jev.key_file`", "what the first real install found", "`cli link`", "daemon.json",
                       "key-env-only-unseen", "environment (only this shell)", "owns exactly two keys", "`send_brief`",
                       "realpath", "`ConfigWatch`", "config-read-once", "catch-up-blocks-pass", "FLEET_SWITCHBOARD_LOCK_FD",
                       "`fleet-switchboard restart`", "| C2 |"):
            self.assertIn(needle, self.design)
        section = self.design[self.design.index("### Install (PR 16)"):self.design.index("### Data")]
        for number, line in enumerate(section.splitlines(), 1):
            self.assertIsNone(FORBIDDEN_IN_PUBLIC_TEXT.search(line), "switchboard.md Install:%d %s" % (number, line))

    def test_the_trial_doc_says_it_is_a_thin_wrapper_and_points_real_users_at_install(self):
        text = " ".join(self.trial.split())
        self.assertIn("thin wrapper for an isolated experiment", text)
        self.assertIn("INSTALL.md", text)
        self.assertIn("fleet-switchboard install", text)


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


class LabelGh:
    """`gh api` over issues with labels, in any number of repos: the reads the daemon makes (a list of the open
    issues with one label) and the writes `decisions` and `labels ensure` make (labels, comments). A runner.
    A write checks its shape, and records the account's token it ran with."""

    def __init__(self, clock):
        self.clock, self.issues, self.labels, self.comments = clock, {}, {}, []
        self.calls, self.envs, self.down, self.fail = [], [], False, set()
        self.fail_writes = set()   # repos whose writes (not reads) fail

    def issue(self, repo, number, title="an issue", labels=(), body=None, pr=False, author=None):
        self.issues[(repo, number)] = {"number": number, "title": title, "state": "open", "labels": list(labels),
                                       "updated_at": sb.iso(self.clock.now() - 60)}
        if body is not None:
            self.issues[(repo, number)]["body"] = body
        if pr:
            self.issues[(repo, number)]["pull_request"] = {"url": "x"}
        if author:
            self.issues[(repo, number)]["user"] = {"login": author}

    def names(self, repo, number):
        return sorted(self.issues[(repo, number)]["labels"])

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(kwargs.get("env"))
        if argv[:2] != [FAKE_GH, "api"]:
            raise AssertionError("fake gh serves `gh api` only, got %r" % argv)
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: network unreachable\n")
        path = argv[2]
        if any(repo in path for repo in self.fail):
            return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 403\n")
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        if method != "GET" and any(repo in path for repo in self.fail_writes):
            return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 403\n")
        data = json.loads(kwargs["input"]) if "input" in kwargs else None
        if method != "GET" and "--input" in argv and data is None:
            raise AssertionError("a write passes its body as input")
        missing = subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        found = re.match(r"^repos/([^/]+/[^/?]+)/issues\?(.*)$", path)
        if found and method == "GET":
            query = dict(pair.partition("=")[::2] for pair in found.group(2).split("&"))
            label = urllib.parse.unquote(query["labels"]) if "labels" in query else None
            rows = [dict(i, labels=[{"name": n} for n in i["labels"]]) for (repo, _), i in sorted(self.issues.items())
                    if repo == found.group(1) and (label is None or label in i["labels"]) and i["state"] == "open"]
            page = int(query.get("page", "1"))
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows if page == 1 else []), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/issues$", path)
        if found and method == "POST":
            repo = found.group(1)
            number = max([n for (r, n) in self.issues if r == repo] + [100]) + 1
            self.issue(repo, number, data["title"], data.get("labels", ()), data.get("body"))
            self.issues[(repo, number)]["updated_at"] = sb.iso(self.clock.now())
            return subprocess.CompletedProcess(argv, 0, json.dumps({"number": number, "html_url": "https://github.com/%s/issues/%d" % (repo, number)}), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/issues/(\d+)/(labels|comments)(?:/(.+))?$", path)
        if found:
            key = (found.group(1), int(found.group(2)))
            if key not in self.issues:
                return missing
            issue = self.issues[key]
            if found.group(3) == "labels" and method == "POST":
                issue["labels"] = sorted(set(issue["labels"]) | set(data["labels"]))
            elif found.group(3) == "labels" and method == "DELETE":
                name = urllib.parse.unquote(found.group(4))
                if name not in issue["labels"]:
                    return missing
                issue["labels"].remove(name)
            elif found.group(3) == "comments" and method == "POST":
                self.comments.append({"repo": key[0], "issue": key[1], "body": data["body"]})
            else:
                raise AssertionError("fake gh: no such write %r" % argv)
            issue["updated_at"] = sb.iso(self.clock.now())
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/labels(?:/(.+))?$", path)
        if found:
            repo = found.group(1)
            have = self.labels.setdefault(repo, {})
            if method == "GET":
                name = urllib.parse.unquote(found.group(2))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"name": name}), "") if name in have else missing
            if method == "POST" and not found.group(2):
                have[data["name"]] = data
                return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        raise AssertionError("fake gh: no route for %r" % (argv,))


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


class TestExternalAdapterMapping(ExternalAdapterCase):
    def good(self):
        return fake_mapping("http://127.0.0.1:9")

    def refused(self, data, needle):
        with self.assertRaises(EA.MappingError) as ctx:
            EA.MappingFile.from_dict(data)
        self.assertIn(needle, str(ctx.exception))

    def test_a_good_mapping_loads_from_a_file(self):
        path = self.tmp / "m.json"
        path.write_text(json.dumps(self.good()))
        mapping = EA.MappingFile.load(path)
        self.assertEqual(mapping.channel, "tool")
        self.assertEqual(mapping.to_external("tier", "reserved"), "sealed")
        self.assertEqual(mapping.to_generic("tier", "major"), "high")

    def test_refusals(self):
        data = self.good()
        data["surprise"] = 1
        self.refused(data, "unknown top-level keys")
        data = self.good()
        data["api_key"] = "x"
        self.refused(data, "never inline")
        data = self.good()
        data["key_source"] = "sk-literal-key-value"
        self.refused(data, "never inline")
        data = self.good()
        data["base_url"] = "http://example.invalid"
        self.refused(data, "loopback")
        data = self.good()
        data["base_url"] = "http://user:pw@127.0.0.1:9"
        self.refused(data, "credential")
        data = self.good()
        data["work_interval"] = 30
        self.refused(data, "work_interval")
        data = self.good()
        data["tier_map"].pop("cos")
        self.refused(data, "tier_map")
        data = self.good()
        data["roles"][0]["role"] = "king"
        self.refused(data, "role rule")
        data = self.good()
        data["operations"]["work_put"]["tool"] = "w"
        self.refused(data, "HTTP only")
        data = self.good()
        data["operations"]["revise"]["http"]["path"] = "/items/revise"
        self.refused(data, "{id}")
        data = self.good()
        data["enums"]["tier"].pop("high")
        self.refused(data, "enums.tier")
        data = self.good()
        data["fields"]["raise"].pop("floor")
        self.refused(data, "fields.raise.floor")
        data = self.good()
        data["errors"]["codes"]["x"] = "NoSuchClass"
        self.refused(data, "errors.codes")
        data = self.good()
        data["operations"].pop("feed")
        self.refused(data, "operations.feed")
        data = self.good()
        data["key_source"] = ["env:A", "env:B", "env:C"]
        self.refused(data, "two for a rotation")

    def test_a_bad_file_is_refused(self):
        with self.assertRaises(EA.MappingError):
            EA.MappingFile.load(self.tmp / "missing.json")
        (self.tmp / "bad.json").write_text("{nope")
        with self.assertRaises(EA.MappingError):
            EA.MappingFile.load(self.tmp / "bad.json")

    def test_the_port_and_the_capabilities(self):
        adapter = self.adapter()
        self.assertIsInstance(adapter, sb.decision_core.ExternalDecisionAdapter)
        for name in ("push", "pull", "revise", "withdraw", "acted", "work", "lookup", "options", "idempotent_raise"):
            self.assertIn(name, adapter.capabilities)
        self.assertNotIn("status", adapter.capabilities)
        data = fake_mapping(self.server.url)
        for name in ("work_put", "work_patch", "work_finish", "work_get", "work_list", "acted"):
            data["operations"].pop(name)
        slim = EA.McpRestAdapter(data, environ={"FAKE_DOOR_KEY": "k"})
        self.assertNotIn("work", slim.capabilities)
        self.assertNotIn("acted", slim.capabilities)
        self.assertIsNone(slim.work)


class TestExternalAdapterFeed(ExternalAdapterCase):
    def test_the_cursor_is_stable_when_nothing_happened_and_a_repeat_is_safe(self):
        for channel in ("tool", "http"):
            self.server.changes.clear()
            self.server.seq = 100
            adapter = self.adapter(channel)
            events, cursor = adapter.pull_normalised(None)
            self.assertEqual(events, [])
            self.server.add_change("rating", "it-1", level="major", floor="minor")
            self.server.add_change("restated", "it-1", gap=7, body="x", rerate=True)  # gaps are normal
            events, cursor = adapter.pull_normalised(0)
            self.assertEqual([e["seq"] for e in events], [101, 108])
            self.assertEqual(cursor, 108)
            again, same = adapter.pull_normalised(0)
            self.assertEqual(([e["event_id"] for e in again], same), ([e["event_id"] for e in events], 108))
            nothing, kept = adapter.pull_normalised(cursor)
            self.assertEqual((nothing, kept), ([], 108))
            self.assertEqual(adapter.pull_normalised(cursor)[1], 108)
            ahead, held = adapter.pull_normalised(500)  # never goes backwards
            self.assertEqual((ahead, held), ([], 500))

    def test_the_result_unpacks_and_reads_by_name(self):
        result = self.adapter().pull_normalised(None)
        events, cursor = result
        self.assertEqual((result["events"], result["next_cursor"], result.events, result.next_cursor),
                         (events, cursor, events, cursor))

    def test_events_are_normalised(self):
        s = self.server
        s.add_change("rating", "it-1", level="major", floor="minor")
        s.add_change("answer", "it-1", **self.answer_fields())
        s.add_change("answer", "it-2", **self.answer_fields(answer_id="a-2", how="reword", chosen=None, words="do B",
                                                           who={"type": "owner_bot"}))
        s.add_change("answer", "it-3", **self.answer_fields(answer_id="a-3", how="no", chosen=None,
                                                           words="the original", no_reason="too risky",
                                                           who={"type": "bot"}))
        s.add_change("answer", "it-4", **self.answer_fields(answer_id="a-4", how="ask", chosen=None, words=None,
                                                           ask_back="which branch?"))
        s.add_change("flip", "it-1", **self.answer_fields(answer_id="a-5", how="no", chosen=None, no_reason="changed my mind"),
                     replaces=self.answer_fields())
        s.add_change("pull", "it-5", why="gone")
        s.add_change("restated", "it-6", body="new text", rerate=True)
        s.add_change("seat_paused", None)
        for channel in ("tool", "http"):
            events = self.adapter(channel).pull_normalised(None)[0]
            by = {e["seq"]: e for e in events}
            self.assertEqual([e["type"] for e in events],
                             ["rated", "answered", "answered", "answered", "sent_back", "reversed", "withdrawn",
                              "revised", "other"])
            self.assertEqual((by[101]["tier"], by[101]["floor"]), ("high", "low"))
            a = by[102]["answer"]
            self.assertEqual((a["id"], a["disposition"], a["meaning"], a["option_id"], a["text"]),
                             ("a-1", "accept", "allow", "k1", "approve"))
            self.assertEqual(by[102]["by"]["role"], "human")
            self.assertEqual((by[103]["answer"]["disposition"], by[103]["answer"]["meaning"],
                              by[103]["answer"]["option_id"], by[103]["answer"]["text"], by[103]["by"]["role"]),
                             ("edit", "text", None, "do B", "cos"))
            self.assertEqual((by[104]["answer"]["disposition"], by[104]["answer"]["meaning"],
                              by[104]["answer"]["rejection"], by[104]["by"]["role"]),
                             ("reject", "deny", "too risky", "orchestrator"))
            self.assertNotIn("answer", by[105])
            self.assertEqual(by[105]["clarification"], "which branch?")
            self.assertEqual(by[106]["answer"]["disposition"], "reject")
            self.assertEqual(by[106]["replaces"]["id"], "a-1")
            self.assertEqual(by[106]["replaces"]["option_id"], "k1")
            self.assertEqual(by[107]["reason"], "gone")
            self.assertEqual((by[108]["text"], by[108]["awaits_rating"]), ("new text", True))
            self.assertEqual(by[109]["raw_kind"], "seat_paused")
            self.assertEqual(len({e["event_id"] for e in events}), len(events))

    def test_an_unknown_answerer_has_no_role(self):
        self.server.add_change("answer", "it-1", **self.answer_fields(who={"type": "stranger"}))
        event = self.adapter().pull_normalised(None)[0][0]
        self.assertEqual(event["by"]["role"], "unknown")
        self.assertEqual(event["by"]["raw"], {"type": "stranger"})


class TestJudgeSample(unittest.TestCase):
    """bin/judge-sample (issue 87): a stratified, reproducible sample from the audit log, never written inside the repo."""

    def row(self, i, verdict, agent="coder-a", shadow=True):
        state = {"arguments": "cmd %d" % i, "intent": "i", "done_when": "d", "tool": "shell", "role": "coder"}
        return {"event": "policy.judge", "ts": "2026-10-08T00:00:%02dZ" % (i % 60), "detail": {
            "shadow": shadow, "judged": True, "verdict": verdict, "agent": agent, "role": "coder", "tool": "shell", "ask": 1,
            "fired": ["outside_intent"], "answers": {}, "thresholds": {}, "reason": "r", "question": {"state": state}}}

    def test_it_draws_each_kind_distinct_reproducible_and_refuses_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            rows = [self.row(i, v, agent="a%d" % (i % 3)) for i, v in enumerate(["would-deny", "would-ask", "unchanged"] * 20)]
            rows += [self.row(0, "would-deny", agent="a0"), self.row(1, "would-deny", shadow=False)]  # a repeat, and a non-shadow
            audit.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
            out = Path(tmp) / "s.jsonl"
            cmd = [sys.executable, str(REPO / "bin" / "judge-sample"), "--audit", str(audit), "--out", str(out), "--deny", "5", "--ask", "4", "--control", "3"]
            self.assertEqual(subprocess.run(cmd, capture_output=True, text=True).returncode, 0)
            first = out.read_text()
            got = [json.loads(l) for l in first.splitlines()]
            self.assertEqual(sorted(r["verdict"] for r in got).count("would-deny"), 5)
            self.assertEqual((len(got), len({(r["arguments"], r["verdict"]) for r in got})), (12, 12))
            self.assertTrue(all(r["label"] is None and r["intent"] == "i" for r in got))
            subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(out.read_text(), first, "the same seed gives the same sample")
            bad = subprocess.run(cmd[:-6] + ["--out", str(REPO / "bin" / "never.jsonl")], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)
            self.assertFalse((REPO / "bin" / "never.jsonl").exists())

    def test_since_keeps_only_later_verdicts_and_a_short_kind_gives_what_there_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            rows = []
            for i, v in enumerate(["would-deny", "would-ask", "unchanged"] * 4):
                row = self.row(i, v)
                row["ts"] = "2026-10-08T22:%02d:00.000Z" % (10 + i)   # 22:10 ... 22:21
                rows.append(row)
            bad = self.row(99, "would-deny")
            bad["ts"] = "garbage"
            rows.append(bad)
            audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            out = Path(tmp) / "s.jsonl"

            def run(*extra):
                return subprocess.run([sys.executable, str(REPO / "bin" / "judge-sample"), "--audit", str(audit), "--out", str(out),
                                       "--deny", "40", "--ask", "40", "--control", "40", *extra], capture_output=True, text=True)
            done = run("--since", "2026-10-08T22:14:00Z")
            self.assertEqual(done.returncode, 0, done.stderr)
            got = [json.loads(l) for l in out.read_text().splitlines()]
            self.assertTrue(all(r["ts"] >= "2026-10-08T22:14" for r in got))
            self.assertEqual(len(got), 8, "the garbage ts and the verdicts before 22:14 are out; 22:14 itself is in")
            self.assertIn("fewer than asked", done.stdout)
            self.assertIn("deny 2 of 40", done.stdout)
            self.assertIn("since 2026-10-08T22:14:00Z", done.stdout)
            for given in ("2026-10-08T22:14:00+00:00", "2026-10-08T22:14:00", "2026-10-08T23:14:00+01:00"):
                run("--since", given)
                self.assertEqual(len(out.read_text().splitlines()), 8, given)
            self.assertEqual(len(run("--since", "2026-10-08T22:15:00Z") and out.read_text().splitlines()), 7)
            self.assertEqual(run("--since", "yesterday").returncode, 2)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(len(out.read_text().splitlines()), 13, "no --since: every verdict, including the one with no readable time")


class TestV2Breaker(Base):
    """The breaker with a fake clock: slow or timing-out calls open it, optional reads are skipped, essential calls go on, it resumes gradually."""

    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.breaker = sb.V2Breaker(lambda: self.now[0])

    def slow(self, n, seconds=sb.V2_SLOW_SECONDS + 1, timed_out=False):
        events = []
        for _ in range(n):
            event = self.breaker.record(seconds, timed_out)
            if event:
                events.append(event)
        return events

    def test_it_stays_closed_while_calls_are_fast_and_while_there_are_too_few_samples(self):
        self.assertEqual(self.slow(30, 0.05), [])
        self.assertTrue(self.breaker.allows(False))
        fresh = sb.V2Breaker(lambda: self.now[0])
        self.assertEqual([fresh.record(60, False) for _ in range(sb.V2_SLOW_MIN_SAMPLES - 1)], [None] * (sb.V2_SLOW_MIN_SAMPLES - 1))

    def test_a_slow_p95_opens_it_once_per_episode_and_optional_reads_are_skipped_but_essential_calls_go_on(self):
        events = self.slow(sb.V2_SLOW_MIN_SAMPLES)
        self.assertEqual([e["event"] for e in events], ["v2.backoff"])
        self.assertIn("p95", events[0]["reason"])
        self.assertEqual(self.slow(5), [], "no second v2.backoff while the same episode goes on")
        self.assertFalse(self.breaker.allows(False))
        self.assertTrue(self.breaker.allows(True), "a wake or delivery is never skipped")
        self.assertEqual(self.breaker.skipped, 1)

    def test_timeouts_in_a_row_open_it_even_with_few_samples(self):
        events = self.slow(sb.V2_TIMEOUT_STREAK, 20, True)
        self.assertEqual(len(events), 1)
        self.assertIn("timed out", events[0]["reason"])

    def test_after_the_backoff_one_probe_at_a_time_a_slow_probe_doubles_the_wait_and_good_probes_close_it(self):
        self.slow(sb.V2_TIMEOUT_STREAK, 20, True)
        self.now[0] += sb.V2_BACKOFF_BASE - 1
        self.assertFalse(self.breaker.allows(False), "still inside the backoff")
        self.now[0] += 2
        self.assertTrue(self.breaker.allows(False), "the first probe")
        self.assertFalse(self.breaker.allows(False), "only one probe at a time")
        event = self.breaker.record(20, True, probe=True)
        self.assertEqual((event["event"], event["seconds"]), ("v2.backoff", int(sb.V2_BACKOFF_BASE * 2)))
        self.assertEqual(event["episode"], 1, "a failed probe is the same episode")
        self.now[0] += sb.V2_BACKOFF_BASE * 2 + 1
        closed = None
        for _ in range(sb.V2_RECOVER_OK):
            self.assertEqual(self.breaker.allows(False), "probe")
            closed = self.breaker.record(0.05, False, probe=True)
        self.assertEqual(closed, {"event": "v2.resume", "episode": 1})
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allows(False))

    def test_the_wait_is_capped_and_a_new_episode_gets_a_new_number(self):
        self.breaker.level = 40
        self.assertEqual(self.breaker._wait(), sb.V2_BACKOFF_MAX)
        self.slow(sb.V2_TIMEOUT_STREAK, 20, True)
        self.now[0] += sb.V2_BACKOFF_BASE + 1
        self.breaker.allows(False)
        for _ in range(sb.V2_RECOVER_OK):
            self.breaker.record(0.05, False, probe=True)
            self.breaker.allows(False)
        again = self.slow(sb.V2_TIMEOUT_STREAK, 20, True)
        self.assertEqual([e["episode"] for e in again], [2])


class SlowProbeBreaker(sb.V2Breaker):
    """A breaker whose `probe_out` hands the processor to another thread between being read and being written (no wait): an unguarded
    check-then-set shows itself at once."""

    @property
    def probe_out(self):
        seen = self._probe_out
        time.sleep(0)
        return seen

    @probe_out.setter
    def probe_out(self, value):
        self._probe_out = value


class TestV2BreakerEdges(Base):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.breaker = sb.V2Breaker(lambda: self.now[0])

    def open_it(self):
        for _ in range(sb.V2_TIMEOUT_STREAK):
            self.breaker.record(20, True)
        self.now[0] += sb.V2_BACKOFF_BASE + 1

    def test_exactly_the_slow_threshold_is_not_slow_and_one_hair_over_is(self):
        fresh = sb.V2Breaker(lambda: self.now[0])
        self.assertEqual([fresh.record(sb.V2_SLOW_SECONDS, False) for _ in range(sb.V2_SLOW_MIN_SAMPLES + 2)], [None] * (sb.V2_SLOW_MIN_SAMPLES + 2))
        over = sb.V2Breaker(lambda: self.now[0])
        events = [over.record(sb.V2_SLOW_SECONDS + 0.001, False) for _ in range(sb.V2_SLOW_MIN_SAMPLES)]
        self.assertEqual([e["event"] for e in events if e], ["v2.backoff"])

    def test_an_essential_call_finishing_during_a_probe_is_not_the_probes_result(self):
        self.open_it()
        self.assertEqual(self.breaker.allows(False), "probe")
        self.assertIsNone(self.breaker.record(99, True), "a slow essential call: it is not the probe")
        self.assertEqual((self.breaker.state, self.breaker.level, self.breaker.probe_out), ("probing", 0, True))
        self.assertIsNone(self.breaker.record(0.01, False))
        self.assertEqual(self.breaker.good, 0, "a fast essential call is not a good probe either")

    def test_a_probe_that_ends_in_no_answer_frees_the_slot_instead_of_blocking_optional_reads_for_ever(self):
        self.open_it()
        self.assertEqual(self.breaker.allows(False), "probe")
        self.assertFalse(self.breaker.allows(False))
        self.breaker.abandon()
        self.assertEqual(self.breaker.allows(False), "probe")

    def test_threads_asking_at_once_get_exactly_one_probe_each_round(self):
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)   # threads switch as often as Python allows, so an unguarded check-then-set shows itself
        self.addCleanup(sys.setswitchinterval, previous)
        for round_number in range(40):
            breaker = SlowProbeBreaker(lambda: self.now[0])
            for _ in range(sb.V2_TIMEOUT_STREAK):
                breaker.record(20, True)
            got, barrier = [], threading.Barrier(8)

            def ask():
                barrier.wait()
                got.append(breaker.allows(False))

            threads = [threading.Thread(target=ask) for _ in range(8)]
            self.now[0] += sb.V2_BACKOFF_BASE + 1
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(map(str, got)), ["False"] * 7 + ["probe"], "round %d" % round_number)
            self.now[0] = 1000.0


class TestEventFeedStreamEdges(V2TransportBase):
    """V2Http.stream_lines: the limits and error paths the feed relies on."""

    def test_a_service_file_that_moved_is_read_again_after_a_refused_connection(self):
        route = self.route(5)
        self.service.up = False
        with self.assertRaises(sb.V2Unreachable):
            list(route.stream_lines("/api/event"))
        self.assertIsNone(route._info, "the address may have changed: the file is read again")
        self.service.up = True
        self.assertIs(next(route.stream_lines("/api/event")), sb.STREAM_OPENED)

    def test_a_non_finite_time_or_any_surprise_never_kills_the_reader_thread(self):
        feed = sb.EventFeed(None)
        for created in ("NaN", "Infinity", "-Infinity"):
            feed.handle('data: {"type": "session.execution.succeeded", "created": %s, "data": {"sessionID": "ses_w"}}' % created)
            self.assertIsInstance(feed.seen["ses_w"]["idle_ms"], int)
        class Boom:
            def stream_lines(self, path):
                raise RuntimeError("anything")
        sleeps = []
        crashing = sb.EventFeed(Boom(), None, sleep=lambda s: (sleeps.append(s), crashing.stop()))
        crashing.run()
        self.assertEqual((crashing.status()["errors"], crashing.status()["down"]), (1, "RuntimeError"), "counted, and the loop goes on instead of dying")

    def test_a_read_timeout_yields_the_idle_marker_and_any_other_error_does_not(self):
        self.service.stream = [feed_line("session.step.ended", assistantMessageID="m"), None]
        got = list(self.route(5).stream_lines("/api/event"))
        self.assertIs(got[0], sb.STREAM_OPENED)
        self.assertIs(got[-1], sb.STREAM_IDLE, "quiet for the whole idle time: ours to end")
        conn_class = type(self.service.connect("h", 1, timeout=5))
        real_get = conn_class.getresponse

        def broken(conn):
            response = real_get(conn)
            response.readline = lambda limit=-1: (_ for _ in ()).throw(OSError("connection reset"))
            return response
        with mock.patch.object(conn_class, "getresponse", broken):
            got = list(self.route(5).stream_lines("/api/event"))
        self.assertEqual(got, [sb.STREAM_OPENED], "a reset is not the idle end: the next open is a gap")

    def test_a_non_200_answer_is_an_error_and_no_gap_is_marked(self):
        self.service.stream_status = 500
        feed = sb.EventFeed(self.route(5), None, sleep=lambda s: feed.stop())
        feed.needs_resync = False
        feed.run()
        self.assertEqual((feed.status()["errors"], feed.status()["connects"], feed.needs_resync), (1, 0, False))

    def test_an_oversized_line_is_refused_and_counted(self):
        self.service.stream = ["data: " + "x" * 5000]
        with mock.patch.object(sb, "V2_STREAM_LINE_MAX", 1000):
            feed = sb.EventFeed(self.route(5), None, sleep=lambda s: feed.stop())
            feed.run()
        self.assertEqual(feed.status()["errors"], 1)

    def test_a_quiet_stretch_ends_the_connection_and_the_feed_reconnects_never_reading_on_after_the_timeout(self):
        self.service.stream = [feed_line("session.step.ended", assistantMessageID="msg_a"), None, feed_line("session.step.ended", assistantMessageID="msg_never")]
        sleeps, feed = [], None

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                feed.stop()
        feed = sb.EventFeed(self.route(5), None, sleep=sleep)
        feed.needs_resync = False
        feed.run()
        self.assertEqual(feed.status()["connects"], 2, "ended, then reconnected")
        self.assertEqual(feed.seen["ses_w"]["assistant"], "msg_a", "the line after the timeout was never read on that connection")

    def test_a_refused_password_is_unreachable_and_counted_not_a_crash(self):
        self.service.password = "rotated-and-stale"
        feed = sb.EventFeed(self.route(5), None, sleep=lambda s: feed.stop())
        feed.run()
        self.assertEqual((feed.status()["errors"], feed.status()["down"]), (1, "V2Unreachable"))

    def test_the_wiring_creates_the_feed_only_for_auto_with_an_http_route(self):
        engine = types.SimpleNamespace(v2=types.SimpleNamespace(http=object()), audit=None)
        with mock.patch.object(sb.EventFeed, "start"):
            self.assertIsNotNone(sb.start_event_feed(engine, {"v2_events": "auto"}, time.time))
            self.assertIsNone(sb.start_event_feed(types.SimpleNamespace(v2=types.SimpleNamespace(http=object()), audit=None), {"v2_events": "off"}, time.time))
            self.assertIsNone(sb.start_event_feed(types.SimpleNamespace(v2=types.SimpleNamespace(http=None), audit=None), {"v2_events": "auto"}, time.time), "no HTTP route: no feed")
        self.assertIsNone(sb.start_event_feed(types.SimpleNamespace(v2=types.SimpleNamespace(), audit=None), {"v2_events": "auto"}, time.time))
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "v2_events": "bogus"})


class TestCloudWatchTick(unittest.TestCase):
    """#1073: one cloud_watch_tick per pass in finish_pass, under try/except, with bounded reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = sb.state_dir_path() / "cloud"
        self.audit = []
        audit = types.SimpleNamespace(append=lambda *a: self.audit.append(a))
        self.engine = types.SimpleNamespace(audit=audit, actor="daemon", now=time.time)
        self.engine._failed = lambda subject, step, err, report: self.audit.append(("error", step, str(err)))

    def records(self, n):
        self.dir.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (self.dir / ("w%d.json" % i)).write_text(json.dumps({"worker": "w%d" % i}))

    def finish(self, tick=None):
        calls = {"tick": 0, "health": 0}
        real = sb.cloud_watch_tick
        def counted(engine, discovery):
            calls["tick"] += 1
            return (tick or real)(engine, discovery)
        def health(*a):
            calls["health"] += 1
        quiet = lambda *a, **k: None
        with mock.patch.multiple(sb, cloud_watch_tick=counted, health_write=health, prune_delivered_reminders=quiet, friction_tick=quiet,
                                 load_tick=quiet, prune_report_riders=quiet, publish_decisions=quiet, write_catching_up=quiet, schedule_judge_warm=quiet):
            sb.finish_pass(self.engine, None, {"errors": []})
        return calls

    def test_S2_runs_once_and_reads_both_records(self):
        self.records(2)
        calls = self.finish()
        self.assertEqual(calls["tick"], 1)
        self.assertEqual(len(self.engine.cloud_watch["records"]), 2)

    def test_S3_a_raising_tick_is_audited_and_later_ticks_run(self):
        def boom(engine, discovery):
            raise RuntimeError("boom")
        calls = self.finish(boom)
        self.assertIn("cloud-watch", [a[1] for a in self.audit if a[0] == "error"])
        self.assertEqual(calls["health"], 1)

    def test_S4_reads_at_most_the_cap_skips_bad_files_and_audits_once(self):
        cloud = sb.load_module("cloud_worker")
        self.records(cloud.WATCH_MAX_RECORDS + 5)
        (self.dir / "big.json").write_text("{\"x\": \"" + "a" * (cloud.WATCH_MAX_BYTES + 10) + "\"}")
        (self.dir / "bad.json").write_text("{not json")
        sb.cloud_watch_tick(self.engine, None)
        sb.cloud_watch_tick(self.engine, None)
        self.assertEqual(len(self.engine.cloud_watch["records"]), cloud.WATCH_MAX_RECORDS)
        self.assertEqual(len([a for a in self.audit if a[1] == "cloud-watch.capped"]), 1)

    def test_S4_bad_files_are_skipped_and_counted(self):
        self.records(1)
        (self.dir / "big.json").write_text("x" * 70000)
        (self.dir / "bad.json").write_text("{not json")
        sb.cloud_watch_tick(self.engine, None)
        self.assertEqual(len(self.engine.cloud_watch["records"]), 1)
        self.assertEqual(self.engine.cloud_watch["skipped"], 2)

    def test_U2_no_directory_no_error_no_directory_created(self):
        calls = self.finish()
        self.assertEqual(calls["tick"], 1)
        self.assertEqual(self.audit, [])
        self.assertFalse(self.dir.exists())


class TestEndTickWithoutStateDirectory(unittest.TestCase):
    """#1480: the end tick reads the state directory with a default: an engine object that has none returns nothing and raises nothing."""

    def test_an_engine_object_with_no_state_directory_attribute_returns_an_empty_list(self):
        engine = types.SimpleNamespace(audit=None, actor="daemon", now=time.time)
        self.assertFalse(hasattr(engine, "state_dir"))
        self.assertEqual(sb.cloud_end_tick(engine, None), [])

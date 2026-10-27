"""Test classes moved out of bin/test-switchboard (text unchanged): cases_02."""
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
from support.fake_jev import FakeOpener, JEV_SETTINGS, KEY_VALUE, choice_answer, http_error, jev_reply, noul_answers
from support.installs_and_keys import DECISIONS_PLUGIN
from support.loader import captured, class_source, sb
from support.orders_and_adapters import EA, ExternalAdapterCase, FakeDoorServer, fake_mapping
from support.paths import BIN, REPO
from support.policy_and_import import V1_CHILD, V1_MAIN, V1_MAIN_ID, convert, v1_export
from support.service_and_merge import feed_line
from support.wire_fakes import EPOCH


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


class TestJevClient(Base):
    def setUp(self):
        super().setUp()
        os.environ["OPENROUTER_API_KEY"] = KEY_VALUE
        self.questions = {"q": {"type": "noul", "instructions": "x", "criteria": {"true": "t", "false": "f"}}}
        self.state = {"message": "hello"}

    def client(self, reply, **settings):
        self.opener = FakeOpener(reply)
        ticks = iter(x / 10.0 for x in range(0, 1000, 3))
        return sb.JevClient(dict(JEV_SETTINGS, **settings), opener=self.opener, clock=lambda: next(ticks))

    def decide(self, reply, **settings):
        return self.client(reply, **settings).decide(self.state, self.questions)

    def test_the_request_is_the_systemone_shape_with_the_key_in_a_header_only(self):
        out = self.decide(jev_reply(noul_answers(q=0.23)))
        call = self.opener.calls[0]
        self.assertEqual(call["url"], "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["timeout"], 20)
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + KEY_VALUE)
        self.assertEqual(call["body"], {"model": "typesafe/jev-1.13", "state": self.state, "questions": self.questions})
        self.assertNotIn(KEY_VALUE, json.dumps(call["body"]) + call["url"])
        self.assertEqual(out["answers"]["q"]["noul"], 0.23)
        self.assertEqual(out["usage"], {"in": 120, "out": 9, "cost": 3.5e-05})
        self.assertEqual(out["model"], "typesafe/jev-1.13-20260917")

    def test_the_key_is_read_at_the_call_from_the_variable_named_in_the_config(self):
        client = self.client(jev_reply(noul_answers(q=0.5)), key_env="MY_JEV_KEY")
        os.environ.pop("MY_JEV_KEY", None)
        with self.assertRaises(sb.JevError) as caught:
            client.decide(self.state, self.questions)
        self.assertEqual(caught.exception.reason, "unauthorized")
        self.assertEqual(self.opener.calls, [], "no key means no call")
        self.assertIn("MY_JEV_KEY", str(caught.exception))
        os.environ["MY_JEV_KEY"] = "first-" + KEY_VALUE
        client.decide(self.state, self.questions)
        os.environ["MY_JEV_KEY"] = "second-" + KEY_VALUE
        client.decide(self.state, self.questions)
        self.assertEqual([c["headers"]["Authorization"] for c in self.opener.calls],
                         ["Bearer first-" + KEY_VALUE, "Bearer second-" + KEY_VALUE])

    def test_the_key_is_never_stored_on_the_client(self):
        client = self.client(jev_reply(noul_answers(q=0.5)))
        client.decide(self.state, self.questions)
        held = repr({k: v for k, v in vars(client).items() if k != "environ"})
        self.assertFalse(KEY_VALUE in held, "the key is held by the client")
        source = class_source("JevClient")
        for banned in ("print(", "sys.stderr", "logging", "environ[", "self.key"):
            self.assertNotIn(banned, source)

    def test_every_http_status_has_its_reason(self):
        for code, reason in ((400, "invalid"), (422, "invalid"), (401, "unauthorized"), (403, "unauthorized"),
                             (402, "no_credit"), (413, "too_large"), (429, "rate_limited"), (500, "unreachable"),
                             (503, "unreachable")):
            with self.subTest(code=code):
                with self.assertRaises(sb.JevError) as caught:
                    self.decide(http_error(code))
                self.assertEqual(caught.exception.reason, reason)

    def test_timeouts_and_unreachable(self):
        urlerror = sb.urllib.error.URLError
        cases = [(sb.socket.timeout("t"), "timeout"), (TimeoutError("t"), "timeout"),
                 (urlerror(sb.socket.timeout("t")), "timeout"), (urlerror("no route to host"), "unreachable"),
                 (ConnectionResetError("reset"), "unreachable"), (OSError("down"), "unreachable")]
        for error, reason in cases:
            with self.subTest(error=error):
                with self.assertRaises(sb.JevError) as caught:
                    self.decide(error)
                self.assertEqual(caught.exception.reason, reason)

    def test_the_call_is_bounded_by_the_configured_timeout(self):
        self.decide(jev_reply(noul_answers(q=0.1)), timeout_seconds=3.5)
        self.assertEqual(self.opener.calls[0]["timeout"], 3.5)

    def test_unusable_answers_are_bad_responses(self):
        good = noul_answers(q=0.1)
        cases = {"not json": b"<html>", "a list": b"[]", "no answers": {"model": "m"},
                 "unanswered": {"answers": {}}, "out of range": {"answers": noul_answers(q=1.5)},
                 "not a number": {"answers": noul_answers(q="high")},
                 "a list of the wrong length": {"answers": [good["q"], good["q"]]}}
        for name, reply in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(sb.JevError) as caught:
                    self.decide(reply)
                self.assertEqual(caught.exception.reason, "bad_response")

    def test_a_list_of_answers_is_read_in_question_order(self):
        out = self.decide({"answers": [noul_answers(q=0.4)["q"]]})
        self.assertEqual(out["answers"]["q"]["noul"], 0.4)

    def test_choice_answers_are_checked_against_the_options(self):
        question = {"c": {"type": "choice", "instructions": "x", "criteria": {"current": "a", "new": "b"}}}
        bad = [{"c": choice_answer("other", {"current": 0.5, "new": 0.5})},
               {"c": choice_answer("new", {"current": 0.5, "new": 0.1})},
               {"c": choice_answer("new", {"current": 0.5, "new": 0.5, "extra": 0.0})},
               {"c": {"type": "choice", "choice": "new", "probabilities": ["x"]}}]
        for answers in bad:
            with self.subTest(answers=answers):
                with self.assertRaises(sb.JevError):
                    self.client({"answers": answers}).decide(self.state, question)
        ok = self.client({"answers": {"c": choice_answer("new", {"current": 0.2, "new": 0.8})}}).decide(self.state, question)
        self.assertEqual(ok["answers"]["c"]["choice"], "new")

    def test_latency_is_measured_for_answers_and_for_errors(self):
        out = self.decide(jev_reply(noul_answers(q=0.1)))
        self.assertGreater(out["latency_ms"], 0)
        with self.assertRaises(sb.JevError) as caught:
            self.decide(http_error(429))
        self.assertGreater(caught.exception.latency_ms, 0)

    def test_the_key_never_reaches_an_error_even_when_the_provider_echoes_it(self):
        echo = ("bad key %s, Authorization: Bearer %s" % (KEY_VALUE, KEY_VALUE)).encode()
        for error in (http_error(401, echo), sb.urllib.error.URLError("refused %s" % KEY_VALUE),
                      OSError("Bearer " + KEY_VALUE)):
            with self.subTest(error=error):
                with self.assertRaises(sb.JevError) as caught:
                    self.decide(error)
                self.assertNotIn(KEY_VALUE, str(caught.exception))
                self.assertNotIn(KEY_VALUE, caught.exception.detail)
        out = self.decide(jev_reply(noul_answers(q=0.1), model="m " + KEY_VALUE))
        self.assertNotIn(KEY_VALUE, json.dumps(out))

    def test_the_response_body_is_closed(self):
        client = self.client(jev_reply(noul_answers(q=0.1)))
        responses = []
        original = client.opener

        def spy(request, timeout=None):
            responses.append(original(request, timeout))
            return responses[-1]
        client.opener = spy
        client.decide(self.state, self.questions)
        self.assertTrue(responses[0].closed)

    def test_real_calls_can_be_recorded_without_the_key_and_replayed(self):
        path = self.home / "recorded.jsonl"
        self.decide(jev_reply(noul_answers(q=0.31)), record_file=str(path))
        text = path.read_text()
        self.assertNotIn(KEY_VALUE, text)
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        pair = json.loads(text)
        self.assertEqual(pair["request"]["questions"], self.questions)
        replayed = sb.ReplayJev(str(path)).decide(self.state, self.questions)
        self.assertEqual(replayed["answers"]["q"]["noul"], 0.31)
        with self.assertRaises(sb.JevError) as caught:  # a reworded question is not silently answered
            sb.ReplayJev(str(path)).decide({"message": "other"}, self.questions)
        self.assertEqual(caught.exception.reason, "bad_response")

    def test_a_failed_call_records_nothing(self):
        path = self.home / "recorded.jsonl"
        with self.assertRaises(sb.JevError):
            self.decide(http_error(429), record_file=str(path))
        self.assertFalse(path.exists())


class TestImportV1Converter(unittest.TestCase):
    def setUp(self):
        self.export = v1_export()
        self.out = convert(self.export)

    def test_one_v2_message_per_v1_message_in_the_same_order_with_the_same_times(self):
        source = self.export["messages"]
        got = self.out["messages"]
        self.assertEqual(len(got), len(source))
        self.assertEqual([m["type"] for m in got], [m["info"]["role"] for m in source])
        self.assertEqual([m["time"]["created"] for m in got], [m["info"]["time"]["created"] for m in source])
        self.assertEqual(got[1]["time"]["completed"], source[1]["info"]["time"]["completed"])

    def test_ids_are_v2s_and_the_same_every_time(self):
        again = convert(v1_export())
        self.assertEqual(self.out["info"]["id"], again["info"]["id"])
        self.assertRegex(self.out["info"]["id"], r"^ses_[0-9a-f]{26}$")
        ids = [m["id"] for m in self.out["messages"]]
        self.assertEqual(ids, [m["id"] for m in again["messages"]])
        for i in ids:
            self.assertRegex(i, r"^msg_[0-9a-f]{26}$")
        self.assertEqual(ids, sorted(ids), "sorting by id keeps the order")
        self.assertEqual(len(set(ids)), len(ids))

    def test_another_session_gets_other_ids(self):
        other = v1_export()
        other["info"]["id"] = "ses_fixture00000000000009"
        self.assertNotEqual(convert(other)["info"]["id"], self.out["info"]["id"])
        self.assertNotEqual(convert(other)["messages"][0]["id"], self.out["messages"][0]["id"])

    def test_the_fleet_metadata_is_stamped_with_where_it_came_from(self):
        self.assertEqual(self.out["info"]["metadata"],
                         {"fleet": {"name": "chief", "role": "chief-of-staff", "imported_from": "v1:" + V1_MAIN_ID}})

    def test_no_fleet_means_no_metadata(self):
        self.assertNotIn("metadata", convert(fleet=None)["info"])

    def test_the_fault_removes_the_metadata_only_in_the_lab(self):
        os.environ[sb.FAULT_ENV] = "no-import-metadata"
        self.assertIn("metadata", convert()["info"])
        os.environ[sb.LAB_ENV] = "1"
        try:
            self.assertNotIn("metadata", convert()["info"])
        finally:
            os.environ.pop(sb.LAB_ENV)
            os.environ.pop(sb.FAULT_ENV)

    def test_the_session_info_is_summed_and_located(self):
        info = self.out["info"]
        self.assertEqual(info["location"], {"directory": "/work/example-repo"})
        self.assertEqual(info["projectID"], "fixture-project")
        self.assertEqual(info["title"], "Chief of Staff (fixture)")
        self.assertAlmostEqual(info["cost"], 4 * 0.0125)
        self.assertEqual(info["tokens"]["input"], 4 * 1200)
        self.assertEqual(info["time"]["created"], self.export["info"]["time"]["created"])
        self.assertGreaterEqual(info["time"]["updated"], self.out["messages"][-1]["time"]["created"])
        self.assertNotIn("idle", info["time"], "an import must not look like a finished worker turn")
        self.assertNotIn("parentID", info)

    def test_the_converted_session_passes_the_schema(self):
        info = self.out["info"]
        self.assertEqual(sb.v2_import_problems(info, self.out["messages"], info["location"]), [])

    def test_user_text_files_and_agents(self):
        first = self.out["messages"][0]
        self.assertEqual(first["text"], "Plan the work for the platform charter and list what is open.")
        inline, reference = first["files"]
        self.assertEqual(inline, {"mime": "text/plain", "data": "aGVsbG8gd29ybGQ=", "source": {"type": "inline"},
                                  "name": "note.txt"})
        self.assertEqual(reference["source"], {"type": "uri", "uri": "file:///work/example-repo/PLAN.md"})
        self.assertEqual(reference["mention"], {"start": 0, "end": 8, "text": "@PLAN.md"})
        self.assertEqual(first["agents"], [{"name": "explore", "mention": {"start": 9, "end": 17, "text": "@explore"}}])

    def test_assistant_content_keeps_its_order_and_tool_calls(self):
        content = self.out["messages"][1]["content"]
        self.assertEqual([c["type"] for c in content], ["reasoning", "text", "tool", "tool"])
        self.assertEqual(content[0]["time"], {"created": 1790000002100, "completed": 1790000002400})
        done, failed = content[2], content[3]
        self.assertEqual((done["id"], done["name"], done["state"]["status"]), ("call_1", "read", "completed"))
        self.assertEqual(done["state"]["input"], {"filePath": "/work/example-repo/PLAN.md"})
        self.assertEqual(done["state"]["content"], [
            {"type": "text", "text": "# Plan\nTwo asks are open."},
            {"type": "file", "uri": "data:image/png;base64,iVBORw0KGgo=", "mime": "image/png", "name": "chart.png"}])
        self.assertEqual(done["state"]["metadata"], {"preview": "# Plan", "title": "PLAN.md"})
        self.assertEqual(done["time"], {"created": 1790000002500, "ran": 1790000002500, "completed": 1790000002600})
        self.assertEqual(failed["state"], {"status": "error", "input": {"command": "false"},
                                           "error": {"type": "error", "message": "Command exited with code 1"}})

    def test_pending_and_running_tools_and_a_snapshot(self):
        third = self.out["messages"][2]
        self.assertEqual(third["content"][0]["state"], {"status": "streaming", "input": "{\"pattern\": \"ask"})
        self.assertEqual(third["content"][1]["state"]["status"], "running")
        second = self.out["messages"][1]
        self.assertEqual(second["snapshot"], {"start": "snap-aaaa", "end": "snap-bbbb", "files": ["PLAN.md", "notes/todo.md"]})

    def test_finish_error_and_retry_are_mapped(self):
        fifth = self.out["messages"][4]
        self.assertEqual(fifth["finish"], "length")
        self.assertEqual(fifth["error"], {"type": "MessageAbortedError", "message": "The operation was aborted"})
        self.assertEqual(fifth["retry"], {"attempt": 2, "at": 1790000062500,
                                          "error": {"type": "APIError", "message": "rate limited"}})
        odd = v1_export()
        odd["messages"][1]["info"]["finish"] = "other"
        got = convert(odd)["messages"][1]
        self.assertEqual((got["finish"], got["rawFinish"]), ("unknown", "other"))

    def test_what_has_no_equivalent_is_listed_not_lost(self):
        kinds = sorted((d["kind"], d["message"][-2:]) for d in self.out["dropped"])
        self.assertEqual(kinds, [
            ("compaction", "04"), ("ignored-text", "04"), ("snapshot", "02"), ("step-marker", "02"),
            ("step-marker", "02"), ("step-marker", "03"), ("step-marker", "03"), ("subtask", "04"),
            ("synthetic-text", "01"), ("unknown:future-kind", "05")])
        for item in self.out["dropped"]:
            self.assertEqual(sorted(item), ["kind", "message", "part", "reason"])
            self.assertTrue(item["message"].startswith("msg_fixture"), "named by the v1 id")
            self.assertTrue(item["reason"])

    def test_what_was_filled_in_is_listed(self):
        self.assertEqual([(d["field"], d["message"][-2:]) for d in self.out["defaulted"]], [("file.data", "01")])
        bare = v1_export()
        for key in ("agent", "mode", "modelID", "providerID"):
            bare["messages"][6]["info"].pop(key, None)
        got = convert(bare)
        self.assertEqual(got["messages"][6]["agent"], "build")
        self.assertEqual(got["messages"][6]["model"], {"id": "unknown", "providerID": "unknown"})
        self.assertEqual({"agent", "model"}, {d["field"] for d in got["defaulted"] if d["message"].endswith("07")})

    def test_the_tool_output_limit_cuts_and_says_so(self):
        got = convert(tool_output_limit=5)
        self.assertEqual(got["messages"][1]["content"][2]["state"]["content"][0]["text"], "# Pla")
        self.assertIn("tool.output", [d["field"] for d in got["defaulted"]])

    def test_a_summary_counts_by_kind(self):
        rows = sb.summarise_dropped(self.out["dropped"])
        self.assertEqual(rows[0]["kind"], "step-marker")
        self.assertEqual(rows[0]["count"], 4)
        self.assertEqual(sum(r["count"] for r in rows), len(self.out["dropped"]))

    def test_an_export_that_would_lose_a_message_is_refused(self):
        cases = {
            "not an object": lambda e: [],
            "no messages": lambda e: {"info": e["info"]},
            "bad session id": lambda e: e["info"].update(id="x"),
            "no directory": lambda e: e["info"].pop("directory"),
            "relative directory": lambda e: e["info"].update(directory="work"),
            "no info on a message": lambda e: e["messages"][0].pop("info"),
            "unknown role": lambda e: e["messages"][0]["info"].update(role="system"),
            "no time": lambda e: e["messages"][0]["info"].pop("time"),
            "no id": lambda e: e["messages"][0]["info"].pop("id"),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                export = v1_export()
                replaced = change(export)
                if isinstance(replaced, (list, dict)) and name in ("not an object", "no messages"):
                    export = replaced
                with self.assertRaises(sb.ImportV1Error):
                    convert(export)

    def test_the_fixture_has_no_personal_text(self):
        for path in (V1_MAIN, V1_CHILD):
            text = path.read_text().lower()
            for fragment in ("/us" + "ers/", "/ho" + "me/", "@", "sk-", "bearer", "http"):
                self.assertNotIn(fragment, text.replace("@plan.md", "").replace("@explore", ""), (path.name, fragment))

    def test_every_part_kind_the_converter_handles_is_in_the_fixture(self):
        kinds = {p["type"] for m in v1_export()["messages"] for p in m["parts"]}
        self.assertLessEqual({"text", "reasoning", "tool", "file", "agent", "step-start", "step-finish", "patch",
                              "snapshot", "retry", "subtask", "compaction"}, kinds)
        states = {p["state"]["status"] for m in v1_export()["messages"] for p in m["parts"] if p["type"] == "tool"}
        self.assertEqual(states, {"pending", "running", "completed", "error"})


class TestPlanCheckFeedback(unittest.TestCase):
    """#135 follow-up: what the first real plan (52 scenarios, one ask of 7 comma-separated clauses) showed."""

    ONE_SENTENCE = ("plans are recorded on GitHub, alignment is reviewed, misalignment is escalated, integration branches "
                    "are used, tools fit, rollout is measured, the plan is approved")
    SEVEN = ["plans are recorded on GitHub", "alignment is reviewed", "misalignment is escalated",
             "integration branches are used", "tools fit", "rollout is measured", "the plan is approved"]

    def scenario(self, n, covers, extra="", ident=None):
        return ("  - id: %s\n    covers: %s\n    text: Given the thing number %d is set up, when run, then it behaves\n"
                "    proof: test\n    pass: the call number %d returns status 200 and exactly %d items\n"
                "    discriminates: a stub that returns an empty list for item %d fails the count\n    evaluator: ci\n%s"
                % (ident or "S%d" % n, covers, n, n, n, n, extra))

    def plan(self, clauses=True, cover=range(1, 8), extra_blocks="", slices=None):
        body = "".join(self.scenario(n, "DW-%d" % n) for n in cover) + self.scenario(90, "PC-1", ident="U1")
        text = "```yaml\nscenarios:\n" + body + "```\n"
        if clauses:
            text += "```yaml\nclauses:\n" + "".join("  DW-%d: %s\n" % (i, c) for i, c in enumerate(self.SEVEN, 1)) + "```\n"
        return text + extra_blocks

    def codes(self, text, done_when=None):
        result = sb.plan_check.check(text, done_when or self.ONE_SENTENCE)
        return sorted(d["code"] for d in result["defects"]), result

    def test_without_a_declared_list_a_one_sentence_done_when_is_one_clause_and_says_so(self):
        codes, result = self.codes(self.plan(clauses=False))
        self.assertEqual(len(result["clauses"]), 1)
        self.assertIn("covers-unknown-clause", codes)
        messages = " ".join(d["message"] for d in result["defects"] if d["code"] == "covers-unknown-clause")
        self.assertIn("declare the clauses", messages)
        self.assertIn("one-clause-many-parts", [w["code"] for w in result["warnings"]])

    def test_declared_clauses_that_reproduce_the_ask_are_used(self):
        codes, result = self.codes(self.plan())
        self.assertEqual((len(result["clauses"]), "covers-unknown-clause" in codes, "declared-clauses-mismatch" in codes),
                         (7, False, False))
        self.assertEqual(result["result"], "pass", result["defects"])

    def test_declared_clauses_that_differ_from_the_ask_fail(self):
        weaker = self.plan().replace("DW-7: the plan is approved", "DW-7: the plan is welcomed")
        self.assertIn("declared-clauses-mismatch", self.codes(weaker)[0])
        fewer = self.plan(cover=range(1, 7)).replace("  DW-7: the plan is approved\n", "")
        self.assertIn("declared-clauses-mismatch", self.codes(fewer)[0], "a plan can never weaken coverage")
        self.assertIn("do not match the ask", " ".join(d["message"] for d in self.codes(fewer)[1]["defects"]))

    def test_the_declared_list_may_split_on_the_asks_separators_and_ignore_case_and_numbers(self):
        done = "(1) Plans are recorded on GitHub; (2) alignment is reviewed. (3) Tools fit"
        text = ("```yaml\nscenarios:\n" + self.scenario(1, "DW-1") + self.scenario(2, "DW-2") + self.scenario(3, "DW-3")
                + self.scenario(9, "DW-1", ident="U1") + "```\n```yaml\nclauses:\n  DW-1: plans are recorded on github\n"
                "  DW-2: ALIGNMENT is reviewed\n  DW-3: tools   fit\n```\n")
        self.assertEqual(sb.plan_check.check(text, done)["result"], "pass")

    def test_a_declared_list_can_never_collapse_or_merge_the_asks_numbered_clauses(self):
        done = "(1) plans are recorded; (2) alignment is reviewed; (3) tools fit"
        scenarios = "```yaml\nscenarios:\n" + self.scenario(1, "DW-1") + self.scenario(9, "PC-1", ident="U1") + "```\n"
        collapsed = scenarios + "```yaml\nclauses:\n  DW-1: plans are recorded; alignment is reviewed; tools fit\n```\n"
        got = sb.plan_check.check(collapsed, done)
        self.assertEqual(got["result"], "fail")
        self.assertIn("declared-clauses-mismatch", [d["code"] for d in got["defects"]])
        self.assertIn("uncovered-clause", [d["code"] for d in got["defects"]], "coverage still asks for all 3")
        merged = scenarios.replace("covers: DW-1", "covers: DW-2", 1) + (
            "```yaml\nclauses:\n  DW-1: plans are recorded\n  DW-2: alignment is reviewed; tools fit\n```\n")
        self.assertIn("declared-clauses-mismatch", [d["code"] for d in sb.plan_check.check(merged, done)["defects"]])
        three = "".join(self.scenario(n, "DW-%d" % n) for n in (1, 2, 3)) + self.scenario(9, "PC-1", ident="U1")
        proper = "```yaml\nscenarios:\n" + three + "```\n```yaml\nclauses:\n  DW-1: plans are recorded\n  DW-2: alignment is reviewed\n  DW-3: tools fit\n```\n"
        self.assertEqual(sb.plan_check.check(proper, done)["result"], "pass")
        listed = "1. plans are recorded\n2. alignment is reviewed\n3. tools fit"
        self.assertIn("declared-clauses-mismatch", [d["code"] for d in sb.plan_check.check(collapsed, listed)["defects"]])

    def test_the_clauses_block_is_strict(self):
        for block in ("clauses:\n  DW-2: out of order", "clauses:\n  DW-1: ok enough here\n  DW-3: skipped",
                      "clauses:\n  nonsense", "clauses:\n", "clauses:\n\tDW-1: tab"):
            with self.subTest(block=block):
                with self.assertRaises(sb.plan_check.PlanFormatError):
                    sb.plan_check.declared_clauses("```yaml\n%s\n```" % block)
        with self.assertRaises(sb.plan_check.PlanFormatError):
            sb.plan_check.declared_clauses("```yaml\nclauses:\n  DW-1: a\n```\n```yaml\nclauses:\n  DW-1: b\n```")
        self.assertIsNone(sb.plan_check.declared_clauses("no block"))

    SCENARIOS = ("```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: a scenario that is long enough\n    proof: test\n"
                 "    pass: exit status is 0 and count equals 3\n    discriminates: a stub shows count equals 0 and fails\n    evaluator: ci\n```\n")

    def test_a_number_in_parentheses_outside_the_done_when_line_is_not_a_clause(self):
        body = ("## Intent\nMake the check readable.\nDone when: (1) first part; (2) second part; (3) third part.\n\n"
                "## Facts\nThe old list (2) and the example (3) are only prose here.\n\n" + self.SCENARIOS)
        done_when = sb.parse_intent(body)[1]
        self.assertEqual(sb.plan_check.clauses_of(done_when), ["first part", "second part", "third part."])
        got = sb.plan_check.check(body, done_when)
        self.assertEqual(len(got["clauses"]), 3)
        self.assertNotIn("out of order", " ".join(d["message"] for d in got["defects"]))

    def test_an_out_of_order_declared_clause_names_its_text_and_its_line(self):
        long_text = "the second clause says a great deal " + "more than eighty characters " * 4
        body = "Facts line one.\nFacts line two.\n\n```yaml\nclauses:\n  DW-1: first\n  DW-3: " + long_text + "\n```\n"
        with self.assertRaises(sb.plan_check.PlanFormatError) as caught:
            sb.plan_check.declared_clauses(body)
        message = str(caught.exception)
        self.assertIn("clauses line 3 (plan line 7)", message)
        self.assertIn("DW-3 is out of order, expected DW-2", message)
        self.assertIn(repr(" ".join(long_text.split())[:80]), message)
        self.assertNotIn(" ".join(long_text.split())[:81], message)

    def test_a_done_when_value_with_its_label_reads_the_same_as_one_without(self):
        for clauses in ("a single clause with no number", "(1) first part; (2) second part"):
            body = "## Intent\nMake it work.\nDone when: " + clauses + "\n\n" + self.SCENARIOS
            plain = sb.plan_check.check(body, clauses, clauses)
            labelled = sb.plan_check.check(body, "Done when: " + clauses, "Done when: " + clauses)
            mixed = sb.plan_check.check(body, "Done when: " + clauses, clauses)
            with self.subTest(clauses=clauses):
                for other in (labelled, mixed):
                    self.assertEqual((other["defects"], other["scenario_hash"], other["plan_hash"], other["clauses"]),
                                     (plain["defects"], plain["scenario_hash"], plain["plan_hash"], plain["clauses"]))
                    self.assertNotIn("done-when-differs", [d["code"] for d in other["defects"]])

    def test_a_numbered_list_done_when_is_split_one_clause_per_line(self):
        self.assertEqual(sb.plan_check.clauses_of("1. first thing\n2. second thing\n3) third thing"),
                         ["first thing", "second thing", "third thing"])
        self.assertEqual(sb.plan_check.clauses_of("1. only one line"), ["1. only one line"])

    def test_ids_may_carry_a_lowercase_suffix_and_sort_by_number_then_suffix(self):
        pc = sb.plan_check
        for ok in ("S1", "S11b", "U3a", "S100"):
            self.assertRegex(ok, pc.ID_RE.pattern)
        for bad in ("S1B", "S", "X1", "S1bb", "S-1", "s1"):
            self.assertIsNone(pc.ID_RE.match(bad), bad)
        self.assertEqual(sorted(["S12", "S11b", "S2", "S11", "U1", "S11a"], key=pc._id_key),
                         ["S2", "S11", "S11a", "S11b", "S12", "U1"])
        codes, result = self.codes(self.plan().replace("id: S7", "id: S7b"))
        self.assertNotIn("bad-id", codes)
        codes, result = self.codes(self.plan().replace("id: S7", "id: S6"))
        self.assertIn("duplicate-id", codes)

    def test_every_message_says_what_good_looks_like(self):
        pc = sb.plan_check
        self.assertIn("a stub that returns an empty list", pc.discriminates_problem("n/a", "x"))
        self.assertIn("a stub that returns an empty list", pc.discriminates_problem("a wrong build would fail this", "x"))
        self.assertIn("a stub that returns an empty list", pc.discriminates_problem("", "x"))
        vague = pc.pass_rule_problem("it works fine for the user today")
        self.assertIn("'it works fine for the user today'", vague.replace('"', "'"))
        for element in ("exit", "threshold", "expected literal", "named comparison"):
            self.assertIn(element, vague)
        scenarios = [dict(id="S1x!", covers="DW", text="t" * 12, proof="test", evaluator="ci", **{"pass": "exit status is 0 always",
                     "discriminates": "a stub that returns an empty list"})]
        messages = " ".join(d["message"] for d in pc.validate(scenarios, ["a"], "")[0])
        for fragment in ("S<number>", "S11b", "DW-<n>", "PC-<n>"):
            self.assertIn(fragment, messages)

    PASS_CORPUS = ("0 occurrences of X in the tree", "10 of 10 files contain Y", "output is identical per file to the baseline",
                   "exit status is 0", "count of Z equals 3", "the exit code is 0 after the run finishes",
                   "the list returns status 200 and exactly 1 agent named \"alpha\"", "the reply arrives within 5 s",
                   "latency stays under 200 ms at p95", "the page shows the word 'Ready' after loading",
                   "the golden output is byte-identical to the stored fixture", "the number of failures is 0",
                   "3 of 3 retries succeed and the error count is 0", "the config file is identical to the template",
                   "the build output has no more than 2 warnings", "disk usage is below 80 percent after the run",
                   "the response body contains \"created\" and status 201")
    FAIL_CORPUS = ("it works for 1 user", "works correctly after the change is applied", "output matches the expectations",
                   "'works'", '"works"', "result = ok and everything fine", "the feature behaves as intended by the team",
                   "everything is fine and the user is happy", "works", "ok", "it passes", "no errors", "non-empty",
                   "output is identical to the expectations", "3 of 4 people like it", "the total is 1 user", "the thing equals 5 somehow maybe",
                   "0 hits yes fine ok", "the result is 0 or so", "the output is identical to the same", "the system is in a good state afterwards",
                   "after the change, 1 thing is fine", "the thing works after 1 try", "looks right to the reviewer")

    def test_a_corpus_of_pass_rules_both_ways(self):
        pc = sb.plan_check
        for rule in self.PASS_CORPUS:
            with self.subTest(good=rule):
                self.assertIsNone(pc.pass_rule_problem(rule))
        for rule in self.FAIL_CORPUS:
            with self.subTest(bad=rule):
                self.assertIsNotNone(pc.pass_rule_problem(rule))

    def test_pc_constraints_are_checked_when_declared_and_warned_when_not(self):
        listed = self.plan() + "\n## Constraints\n- PC-1: nothing outside the repo changes\n- PC-2: no new dependency\n"
        codes, result = self.codes(listed)
        self.assertEqual(codes, ["uncovered-constraint"])
        self.assertIn("PC-2", result["defects"][0]["message"])
        wrong = listed.replace("covers: PC-1", "covers: PC-9")
        self.assertIn("covers-unknown-constraint", self.codes(wrong)[0])
        codes, result = self.codes(self.plan())
        self.assertEqual(codes, [])
        self.assertEqual([w["code"] for w in result["warnings"]], ["no-constraint-list"])
        self.assertEqual(sb.plan_check.declared_constraints("```\nPC-4: in a fence\n```\n**PC-2**: bold\n  - PC-3. item"), {2, 3})

    def test_size_guidance_is_per_slice_when_slices_are_used(self):
        pc = sb.plan_check
        def many(count, slice_of):
            return [dict(id="S%d" % n, slice=slice_of(n), covers="PC-1", text="t", proof="test", evaluator="ci") for n in range(1, count + 1)]
        warn = lambda scenarios: [w["message"] for w in pc.validate(scenarios, ["a"], "")[1] if w["code"] == "many-scenarios"]
        self.assertEqual(len(warn(many(52, lambda n: "s%d" % (n % 3)))), 0, "52 scenarios over 3 slices of about 17: no nag")
        self.assertEqual(len(warn(many(52, lambda n: "one"))), 1, "52 in one slice: that slice is over 30")
        self.assertEqual(len(warn(many(85, lambda n: "s%d" % (n % 5)))), 1, "over 80 in total")
        self.assertEqual(len(warn(many(31, lambda n: ""))), 1, "no slices: 30 in total is the guidance")
        self.assertEqual(len(warn(many(30, lambda n: ""))), 0)

    def test_the_text_output_groups_defects_by_kind_with_counts(self):
        pc = sb.plan_check
        text = re.sub(r"pass: .*", "pass: works fine for everyone here", self.plan())
        result = pc.check(text, self.ONE_SENTENCE)
        lines = pc.render(result, "file x")
        self.assertIn("defects: vague-pass x8", lines)
        self.assertIn("vague-pass x8", lines)
        self.assertEqual(len([l for l in lines if l.startswith("  DEFECT")]), pc.GROUP_LINES)
        self.assertTrue(any("and 3 more" in l for l in lines))
        self.assertEqual(len(result["defects"]), 8, "the data (--json) has every defect")


class TestPlanReceipts(unittest.TestCase):
    """#978: one canonical plan comment edited in place, a short receipt per revision, a repost reported, the three documents pinned."""
    H = "sha256:" + "a" * 64
    PLAN = "## Plan (scenarios)\nplan-format: 2\n"

    def receipt(self, cid, extra="", day=5):
        return {"id": cid, "created_at": "2026-01-0%dT00:00:00Z" % day, "body": "<!-- plan-receipt -->\nplan hash: %s\nwhy it changed\nand what%s" % (self.H, extra)}

    def plan(self, cid, day=1):
        return {"id": cid, "created_at": "2026-01-0%dT00:00:00Z" % day, "body": self.PLAN}

    def test_one_canonical_comment_with_two_receipts_passes_and_is_the_one_used(self):
        comments = [self.plan(10), self.receipt(11, day=2), self.receipt(12, day=3)]
        self.assertEqual(sb.plan_check.plan_comment_defects(comments), [])
        self.assertEqual(sb.plan_check.select_plan_text("b", comments)[1], "comment 10")

    def test_a_receipt_that_quotes_the_plan_heading_is_not_a_plan(self):
        quoting = {"id": 13, "created_at": "2026-01-09T00:00:00Z", "body": "<!-- plan-receipt -->\nplan hash: %s\n## Plan (scenarios)\nx" % self.H}
        self.assertEqual(sb.plan_check.select_plan_text("b", [self.plan(10), quoting])[1], "comment 10")

    def test_a_reposted_plan_is_reported_naming_both_comments(self):
        defects = sb.plan_check.plan_comment_defects([self.plan(10), self.plan(20, day=4)])
        self.assertEqual([d["code"] for d in defects], ["second-plan-comment"])
        self.assertIn("20", defects[0]["message"])
        self.assertIn("10", defects[0]["message"])

    def test_a_receipt_that_carries_plan_text_is_reported(self):
        for extra in ("\n## Plan (scenarios)", "\nplan-format: 2", "\nscenarios:\n  - id: S1", "\n```yaml\nx\n```"):
            with self.subTest(extra=extra):
                defects = sb.plan_check.plan_comment_defects([self.plan(10), self.receipt(11, extra)])
                self.assertEqual([d["code"] for d in defects], ["bad-receipt"])
                self.assertIn("receipt carries plan text", defects[0]["message"])

    def test_a_malformed_receipt_is_reported(self):
        bad = {"id": 14, "created_at": "2026-01-09T00:00:00Z", "body": "<!-- plan-receipt -->\nno hash here\nreason"}
        self.assertEqual([d["code"] for d in sb.plan_check.plan_comment_defects([bad])], ["bad-receipt"])

    def test_no_receipts_and_one_plan_is_as_before(self):
        self.assertEqual(sb.plan_check.plan_comment_defects([self.plan(10)]), [])
        self.assertEqual(sb.plan_check.plan_comment_defects([]), [])

    class Reader:
        def __init__(inner, comments):
            inner.comments = comments

        def _api(inner, path, repo):
            return inner.comments if path.split("?")[0].endswith("/comments") else {"body": ""}

    def test_the_defects_reach_the_result_of_load_plan(self):
        for comments, ok in (([self.plan(10), self.receipt(11)], True), ([self.plan(10), self.plan(20, day=4)], False)):
            defects = sb.fetch_plan(self.Reader(comments), "o/r", 1)[3]
            self.assertEqual(not defects, ok)

    def test_an_explicit_comment_id_still_reports_a_second_plan_naming_both(self):
        for explicit in (10, 20):
            defects = sb.fetch_plan(self.Reader([self.plan(10), self.plan(20, day=4)]), "o/r", 1, explicit)[3]
            self.assertEqual([d["code"] for d in defects], ["second-plan-comment"])
            self.assertIn("comment 20", defects[0]["message"])
            self.assertIn("comment 10", defects[0]["message"])

    def test_no_module_global_holds_the_defects(self):
        self.assertFalse(hasattr(sb, "PLAN_COMMENT_DEFECTS"))

    def test_the_route_lookup_reports_a_second_plan_as_a_defect(self):
        t = TestPlanKind()
        hub = t.hub([self.plan(10), self.plan(20, day=4)])[0]
        self.assertEqual(t.kind(hub), "defect")
        self.assertEqual(t.kind(t.hub([self.plan(10), self.receipt(11)])[0]), "plan")

    def test_mutant_route_lookup_that_skips_the_rule_fails(self):
        t = TestPlanKind()
        with mock.patch.object(sb, "second_plan_defects", lambda d: []):
            self.assertEqual(t.kind(t.hub([self.plan(10), self.plan(20, day=4)])[0]), "plan")   # the test above fails on it

    def documents(self):
        texts = {"skill": (REPO / "skills" / "fleet-planning" / "SKILL.md").read_text(), "template": (REPO / "docs" / "plan-template.md").read_text()}
        texts["brief"] = sb.load_plan_review().lane_brief("L1", "ref", "h", "plan", "intent", "dw")
        return texts

    def pinned(self, texts):
        return all(sb.plan_check.PLAN_RULE in t for t in texts.values())

    def test_the_three_documents_say_the_rule_once_in_the_same_words(self):
        texts = self.documents()
        self.assertTrue(self.pinned(texts))
        for name, text in texts.items():
            self.assertEqual(text.count(sb.plan_check.PLAN_RULE), 1, name)
        self.assertEqual(sb.load_plan_review().PLAN_RULE, sb.plan_check.PLAN_RULE)

    def test_the_pin_fails_when_the_sentence_is_removed_from_any_one_document(self):
        for name in ("skill", "template", "brief"):
            with self.subTest(removed=name):
                texts = self.documents()
                texts[name] = texts[name].replace(sb.plan_check.PLAN_RULE, "")
                self.assertFalse(self.pinned(texts))

    def test_a_mutant_that_treats_a_receipt_as_a_plan_is_caught(self):
        quoting = self.receipt(13, "\n## Plan (scenarios)")
        comments = [self.plan(10), quoting]
        self.assertEqual([d["code"] for d in sb.plan_check.plan_comment_defects(comments)], ["bad-receipt"])
        original = sb.load_plan_check().is_receipt
        sb.load_plan_check().is_receipt = lambda comment: False   # the mutant: a receipt is not recognised, so its text is never held to the receipt rule
        try:
            self.assertEqual(sb.plan_check.plan_comment_defects(comments), [])
        finally:
            sb.load_plan_check().is_receipt = original

    def test_a_mutant_that_ignores_a_second_plan_comment_is_caught(self):
        comments = [self.plan(10), self.plan(20, day=4)]
        original = sb.load_plan_check()._is_plan_comment
        sb.load_plan_check()._is_plan_comment = lambda comment: comment.get("id") == 10
        try:
            self.assertEqual(sb.plan_check.plan_comment_defects(comments), [])   # the mutant sees no repost
        finally:
            sb.load_plan_check()._is_plan_comment = original
        self.assertEqual(len(sb.plan_check.plan_comment_defects(comments)), 1)


class TestDecisionHeadlines(unittest.TestCase):
    """The readable headline and the folding of a report into the issue it is about (no process, no network)."""

    HOME = "acme/web"

    def derive(self, items, cached=None, repos=("acme/web", "acme/api")):
        engine = types.SimpleNamespace(
            config={"intent_repo": self.HOME}, discovery=None, _note=lambda *a, **k: None,
            intents=types.SimpleNamespace(_cache={("issue", r, n): (0, types.SimpleNamespace(title=t))
                                                  for (r, n), t in (cached or {}).items()}))
        found = list(items)
        with mock.patch.object(sb, "decision_repos", return_value=list(repos)), \
                mock.patch.object(sb, "decisions_from_github", lambda e, n: ([x for x in found if x["kind"] == "issue"], [])), \
                mock.patch.object(sb, "decisions_from_requests", lambda e, n: ([], [])), \
                mock.patch.object(sb, "decisions_from_reports", lambda e, n: ([x for x in found if x["kind"] == "report"], [])):
            decisions, errors = sb.derive_decisions(engine, EPOCH + 60)
        self.assertEqual(errors, [])
        return decisions

    @staticmethod
    def issue(ask, title, repo=HOME, tier="human"):
        return {"kind": "issue", "title": title, "ask": ask, "agent": "orch", "since": EPOCH, "seed": "i%s%s" % (repo, ask),
                "bare": True, "repo": repo, "tier": tier}

    @staticmethod
    def report(text, ask=None, repo=HOME, agent="coder", tier="cos", since=EPOCH):
        return {"kind": "report", "title": text, "ask": ask, "agent": agent, "since": since, "seed": "r%s%s%s" % (agent, ask, text),
                "bare": False, "repo": repo, "tier": tier}

    def test_the_panel_width_is_the_one_the_plugin_draws(self):
        source = (DECISIONS_PLUGIN / "decisions.mjs").read_text()
        self.assertIn("ROW_WIDTH = %d " % sb.HEADLINE_WIDTH, source)
        self.assertIn("HEADLINE_LINES = %d " % sb.HEADLINE_LINES, source)

    def test_urls_become_short_refs_and_other_urls_their_host(self):
        self.assertEqual(sb.short_refs("see https://github.com/acme/store-api/issues/9109, and "
                                       "https://github.com/acme/web/pull/7. Also https://docs.example.org/a/b?c=1 ok"),
                         "see store-api#9109, and web#7. Also docs.example.org ok")

    def test_an_issue_decision_headline_is_its_title(self):
        (found,) = self.derive([self.issue(2, "Pick the database for https://github.com/acme/web/issues/9")])
        self.assertEqual(found["headline"], "Pick the database for web#9")
        self.assertEqual(found["title"], "Pick the database for https://github.com/acme/web/issues/9")

    def test_a_report_headline_names_the_issue_it_is_about_from_the_cache_then_the_gist(self):
        (found,) = self.derive([self.report("Which database should it use?", ask=5)], cached={(self.HOME, 5): "Storage choice"})
        self.assertEqual(found["headline"], "Storage choice: Which database should it use?")

    def test_a_report_with_no_cached_issue_keeps_its_report_line_and_reads_nothing_new(self):
        reads = []
        engine = types.SimpleNamespace(intents=types.SimpleNamespace(
            _cache={}, issue=lambda *a: reads.append(a)))
        self.assertIsNone(sb.cached_issue_title(engine, self.HOME, 5, {}))
        self.assertEqual(reads, [])
        (found,) = self.derive([self.report("Which database should it use?", ask=5)])
        self.assertEqual(found["headline"], "Which database should it use?")

    def test_a_long_report_starting_with_a_url_fits_two_lines_and_ends_in_an_ellipsis(self):
        text = "https://github.com/acme/store-api/issues/9109 " + "needs a decision on the schema " * 12
        (found,) = self.derive([self.report(text)])
        self.assertTrue(found["headline"].startswith("store-api#9109 needs"))
        self.assertTrue(found["headline"].endswith("\u2026"))
        self.assertLessEqual(sb._wrap_count(found["headline"].split(), sb.HEADLINE_WIDTH), 2)
        self.assertNotIn("http", found["headline"])
        self.assertTrue(found["title"].startswith("https://github.com/acme/store-api/issues/9109 needs"))  # title unchanged

    def test_a_headline_that_fits_is_never_cut(self):
        self.assertEqual(sb.fit_lines("two short words"), "two short words")
        self.assertEqual(sb.fit_lines("x" * 200)[-1], "\u2026")
        self.assertEqual(sb.fit_lines("aa bb cc dd ee", 5, 2), "aa bb cc\u2026")
        self.assertEqual(sb.fit_lines("aa bb cc", 5, 2), "aa bb cc")

    def test_a_long_issue_title_and_a_long_gist_both_stay_in_two_lines(self):
        (found,) = self.derive([self.report("gist " * 40, ask=5)], cached={(self.HOME, 5): "title " * 30})
        self.assertLessEqual(sb._wrap_count(found["headline"].split(), sb.HEADLINE_WIDTH), 2)
        self.assertIn("gist", found["headline"])

    def test_a_report_on_an_issue_that_is_a_decision_is_shown_once_under_the_issues_id(self):
        found = self.derive([self.issue(5, "Storage choice"), self.report("Which database?", ask=5, agent="coder")])
        (only,) = found
        self.assertEqual((only["kind"], only["ask"], only["reported_by"]), ("issue", 5, ["coder"]))
        self.assertIn("5", only["id"])

    def test_the_same_number_in_another_repo_is_not_the_same_question(self):
        found = self.derive([self.issue(5, "Storage choice"), self.report("Which database?", ask=5, repo="acme/api")])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "report"])
        self.assertNotIn("reported_by", [k for d in found for k in d])

    def test_an_untagged_report_naming_exactly_one_open_issue_is_folded_in(self):
        found = self.derive([self.issue(5, "Storage choice"), self.issue(6, "Other"),
                             self.report("blocked on web#5 until you pick", agent="c1"),
                             self.report("see https://github.com/acme/web/issues/6", agent="c2")])
        self.assertEqual(sorted((d["ask"], d.get("reported_by")) for d in found), [(5, ["c1"]), (6, ["c2"])])

    def test_an_untagged_report_naming_none_or_two_stays_a_decision_of_its_own(self):
        found = self.derive([self.issue(5, "A"), self.issue(6, "B"),
                             self.report("which of web#5 or web#6?", agent="c1"),
                             self.report("nothing here, or web#99", agent="c2"),
                             self.report("api#5 is a different repo", agent="c3")])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "issue", "report", "report", "report"])

    @staticmethod
    def guessed(text, ask, repo, agent="coder", since=EPOCH + 5):
        """A report whose repo is only its sender's (the line named no repo the switchboard knows)."""
        return dict(TestDecisionHeadlines.report(text, ask=ask, repo=repo, agent=agent, since=since), repo_known=False)

    def test_a_report_with_a_guessed_repo_folds_into_the_one_human_issue_decision_with_its_number(self):
        found = self.derive([self.issue(783, "Retire the legacy apps"), self.guessed("blocked: need your call", 783, "acme/api")])
        (only,) = found
        self.assertEqual((only["kind"], only["tier"], only["ask"]), ("issue", "human", 783))
        self.assertEqual(only["reported_by"], ["coder"])
        self.assertEqual(only["reported_lines"], ["coder: blocked: need your call"])   # the report's line stays reachable

    def test_every_folded_line_is_kept_up_to_three_clipped_and_the_issues_id_is_unchanged(self):
        alone = self.derive([self.issue(783, "Retire")])
        reports = [self.guessed("line %d " % i + "x" * 400, 783, "acme/api", "c%d" % i, EPOCH + 5 + i) for i in range(5)]
        both = self.derive([self.issue(783, "Retire")] + reports)
        self.assertEqual((both[0]["id"], both[0]["reported_by"]), (alone[0]["id"], ["c0", "c1", "c2", "c3", "c4"]))
        kept = both[0]["reported_lines"]
        self.assertEqual([l.split(":")[0] for l in kept], ["c2", "c3", "c4"])          # the newest three, oldest first
        self.assertTrue(all(len(l) <= len("c0: ") + 160 for l in kept))
        two = self.derive([self.issue(783, "Retire"), reports[0], reports[1]])
        self.assertEqual(len(two[0]["reported_lines"]), 2)                               # fewer than three: all of them

    def test_a_named_repo_that_differs_never_folds_watched_or_not(self):
        named = lambda repo: dict(self.report("other repo", ask=5, repo=repo), repo_known=True)
        found = self.derive([self.issue(5, "web five"), named("acme/api"), named("elsewhere/other")])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "report", "report"])
        self.assertNotIn("reported_lines", found[0])

    def test_a_guessed_repo_does_not_fold_when_the_number_is_ambiguous_or_the_issue_is_absent(self):
        found = self.derive([self.issue(5, "web five"), self.issue(5, "api five", repo="acme/api"),
                             self.guessed("which?", 5, "acme/other"), self.guessed("no such issue", 99, "acme/api")])
        kinds = sorted((d["kind"], d["ask"]) for d in found)
        self.assertEqual(kinds, [("issue", 5), ("issue", 5), ("report", 5), ("report", 99)])   # two #5s: not guessed; 99: no issue

    def test_a_report_that_names_its_repo_never_folds_into_another_repos_issue(self):
        found = self.derive([self.issue(783, "Retire"), self.report("other repo", ask=783, repo="acme/api")])
        self.assertEqual(sorted(d["kind"] for d in found), ["issue", "report"])

    def test_a_report_on_an_issue_that_is_only_cos_tier_still_folds_as_it_always_did(self):
        found = self.derive([self.issue(7, "cos issue", tier="cos"), self.guessed("q", 7, "acme/api")])
        self.assertEqual([(d["kind"], d["tier"]) for d in found], [("issue", "cos")])

    def test_the_folded_reports_external_link_is_withdrawn_once_and_the_issue_is_not_double_raised(self):
        core = sb.decision_core
        links = {"report-key": {"kind": "report", "boss": "cos", "revision": 1, "digest": "d", "tier": "cos", "floor": "f"},
                 "issue-key": {"kind": "issue", "revision": 1, "digest": "d", "tier": "human", "floor": "g"}}
        desired = {"issue-key": {"payload": {}, "digest": "d", "tier": "human", "floor": "g", "kind": "issue", "thread": None}}
        ops = core.plan_operations(desired, links, (), 0, ())
        self.assertEqual([(o["op"], o["key"]) for o in ops], [("withdraw", "report-key")])
        links["report-key"].update(withdrawn=True)
        self.assertEqual(core.plan_operations(desired, links, (), 0, ()), [])      # once, and nothing raised twice

    def test_ids_are_the_ones_the_unfolded_list_assigns(self):
        both = self.derive([self.issue(5, "Storage choice"), self.report("Which database?", ask=5)])
        alone = self.derive([self.issue(5, "Storage choice")])
        self.assertEqual(both[0]["id"], alone[0]["id"])


class TestExternalAdapterErrors(ExternalAdapterCase):
    def test_http_statuses_map_to_the_hierarchy_and_4xx_is_not_retried(self):
        cases = ((401, "cred", EA.CredentialError), (403, "off", EA.DisabledError), (404, "nf", EA.NotFound),
                 (400, "bad", EA.InvalidRequest), (415, "media", EA.InvalidRequest), (409, "clash", EA.Conflict),
                 (422, "odd", EA.InvalidRequest), (409, "decided", EA.AlreadyDecided))
        adapter = self.adapter("http")
        for status, code, cls in cases:
            self.slept.clear()
            before = len(self.server.requests)
            self.server.fail_next("raise", status, {"code": code, "message": "m"})
            with self.assertRaises(cls) as ctx:
                self.push(adapter, self.decision(), "k-%d" % status)
            self.assertIs(type(ctx.exception), cls)
            self.assertEqual(self.slept, [], status)
            self.assertEqual(len(self.server.requests), before)  # the injected reply is the one and only call

    def test_a_4xx_is_asked_once(self):
        adapter = self.adapter("http")
        for status in (400, 401, 403, 404, 409):
            self.server.fail_next("raise", status, {"code": "z", "message": "m"}, count=10)
            with self.assertRaises(EA.AdapterError):
                self.push(adapter, self.decision(), "k")
            self.assertEqual(len([1 for i in self.server.inject if i["count"] == 9]), 1, status)
            self.server.inject.clear()

    def test_429_honours_retry_after_then_succeeds(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later"}, {"Retry-After": "2"})
        self.assertTrue(self.push(adapter, self.decision(), "k-1").startswith("it-"))
        self.assertEqual(self.slept, [2.0])

    def test_a_long_retry_after_is_reported_not_slept(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later"}, {"Retry-After": "60"})
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-1")
        self.assertEqual(ctx.exception.retry_after, 60.0)
        self.assertEqual(self.slept, [])
        self.server.fail_next("raise", 429, {"code": "slow", "message": "later", "retry_after_s": 7})
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-2")
        self.assertEqual(ctx.exception.retry_after, 7.0)  # no header: the mapped body field
        self.server.fail_next("raise", 429, {"code": "x", "message": "later"}, count=10)
        with self.assertRaises(EA.RateLimited) as ctx:
            self.push(adapter, self.decision(), "k-3")
        self.assertEqual(ctx.exception.retry_after, 1.0)  # neither: the default (short: slept, then given up)
        self.assertEqual(self.slept, [1.0, 1.0])

    def test_5xx_is_retried_with_a_doubling_delay_and_a_cap(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 503, count=2)
        self.assertTrue(self.push(adapter, self.decision(), "k-1").startswith("it-"))
        self.assertEqual(self.slept, [0.5, 1.0])
        self.slept.clear()
        self.server.fail_next("raise", 500, count=10)
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-2")
        self.assertEqual(self.slept, [0.5, 1.0])  # three attempts, two waits
        self.slept.clear()
        many = self.adapter("http", retry={"attempts": 5})
        self.server.inject.clear()
        self.server.fail_next("raise", 500, count=10)
        with self.assertRaises(EA.Unavailable):
            self.push(many, self.decision(), "k-3")
        self.assertEqual(self.slept, [0.5, 1.0, 1.5, 1.5])  # capped at max_delay

    def test_a_timeout_a_dropped_connection_and_a_closed_port_are_unavailable_and_retried(self):
        data = fake_mapping(self.server.url, "http", timeouts={"request": 0.3}, retry={"attempts": 2})
        adapter = EA.McpRestAdapter(data, sleep=self.slept.append, environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY})
        self.server.delay = 1.0
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-1")
        self.assertEqual(self.slept, [0.5])
        self.server.delay = 0
        self.server.down = True
        with self.assertRaises(EA.Unavailable):
            self.push(adapter, self.decision(), "k-2")
        self.server.down = False
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        data["base_url"] = "http://127.0.0.1:%d" % port
        self.slept.clear()
        with self.assertRaises(EA.Unavailable):
            self.push(EA.McpRestAdapter(data, sleep=self.slept.append,
                                        environ={"FAKE_DOOR_KEY": FakeDoorServer.KEY}), self.decision(), "k-3")
        self.assertEqual(self.slept, [0.5])

    def test_tool_channel_refusals_map_by_code(self):
        adapter = self.adapter("tool")
        for code, cls in (("off", EA.DisabledError), ("decided", EA.AlreadyDecided), ("clash", EA.Conflict),
                          ("slow", EA.RateLimited), ("nf", EA.InvalidRequest), ("bad", EA.InvalidRequest)):
            self.slept.clear()
            self.server.fail_next("raise", 400, {"code": code, "message": "refused: %s" % code, "retry_after_s": 90},
                                  level="result")
            with self.assertRaises(cls) as ctx:
                self.push(adapter, self.decision(), "k-" + code)
            self.assertIs(type(ctx.exception), cls)
            self.assertEqual(self.slept, [])

    def test_tool_channel_credential_and_schema_failures(self):
        adapter = self.adapter("tool")
        self.server.fail_next("raise", 401, {"code": "cred", "message": "no"}, level="http")
        with self.assertRaises(EA.CredentialError):
            self.push(adapter, self.decision(), "k-1")
        self.server.fail_next("raise", 503, level="http")
        self.assertTrue(self.push(adapter, self.decision(), "k-2").startswith("it-"))  # retried
        # schema failures arrive as text, not JSON
        with self.assertRaises(EA.InvalidRequest) as ctx:
            adapter.door.mcp.call_tool("item_raise", {"bad_schema": 1})
        self.assertIn("validation", str(ctx.exception))
        with self.assertRaises(EA.InvalidRequest):
            adapter.door.mcp.call_tool("no_such_tool", {})

    def test_the_tool_session_sse_and_expiry(self):
        adapter = self.adapter("tool")
        self.assertEqual(adapter.door.mcp.list_tools()[:2], ["seats_list", "item_raise"])
        self.assertEqual(adapter.door.mcp.session, "sess-1")
        self.push(adapter, self.decision(), "k-1")
        self.assertEqual(self.server.sid, 1)  # one session for all of it
        self.server.sessions.clear()  # the server forgot it
        self.push(adapter, self.decision(), "k-2")
        self.assertEqual(self.server.sid, 2)
        self.server.session_miss = 400   # some servers answer a session they forgot with a 400
        self.server.sessions.clear()
        self.push(adapter, self.decision(), "k-2b")
        self.assertEqual(self.server.sid, 3)
        with self.assertRaises(EA.InvalidRequest):   # a real 400 with a good session is asked once, not looped
            self.server.fail_next("raise", 400, {"code": "bad", "message": "m"}, count=2)
            self.push(adapter, self.decision(), "k-2c")
        self.assertEqual(self.server.sid, 4)
        self.server.session_miss = 404
        self.server.sse = True
        fresh = self.adapter("tool")
        self.assertTrue(self.push(fresh, self.decision(), "k-3").startswith("it-"))
        self.assertEqual(fresh.pull_normalised(None)[1], 0)

    def test_a_list_of_messages_is_joined(self):
        adapter = self.adapter("tool")
        self.server.fail_next("raise", 400, {"code": "bad", "message": ["first", "second"]}, level="result")
        with self.assertRaises(EA.InvalidRequest) as ctx:
            self.push(adapter, self.decision(), "k-1")
        self.assertIn("first; second", str(ctx.exception))

    def test_a_redirect_is_never_followed(self):
        adapter = self.adapter("http")
        self.server.fail_next("raise", 302, {"code": "x", "message": "moved"}, {"Location": "http://127.0.0.1:1/"})
        with self.assertRaises(EA.InvalidRequest):
            self.push(adapter, self.decision(), "k-1")


class TestJobTool(unittest.TestCase):
    """`job start` / `job status`: detached long runs, told apart by evidence. Real short processes; each test waits on the process it started
    (no polling of a clock), so a loaded machine only makes it slower."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)
        patcher = mock.patch.dict(os.environ, {sb.JOBS_DIR_ENV: str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs, self.pids = {}, []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.SubprocessError:
                pass

    def spawn(self, argv, **kwargs):
        proc = subprocess.Popen(argv, **kwargs)
        self.last = proc
        return proc

    def job(self, *argv, spawn=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ):
            code = sb.main(["job", *argv], spawn=spawn or self.spawn)
        return code, out.getvalue().strip(), err.getvalue()

    def started(self, name, *command):
        code, out, err = self.job("start", name, "--", *command)
        self.assertEqual(code, 0, err)
        self.procs[name] = self.last
        record = sb.job_read(name, self.root)
        self.pids.append(record["pid"])
        return record

    def settled(self, name, want):
        """Wait for the job's own process (the wrapper), then read its state: no polling."""
        self.procs[name].wait(timeout=60)
        state = sb.job_state(sb.job_read(name, self.root), self.root)
        self.assertEqual(state[0], want)
        return state

    def test_a_finished_job_reads_its_exit_code_and_its_result_file(self):
        self.started("suite", "sh", "-c", "echo hello; exit 3")
        self.assertEqual(self.settled("suite", "finished"), ("finished", 3))
        code, out, err = self.job("status", "suite")
        self.assertEqual(code, 1, "a non-zero exit is not success")
        self.assertRegex(out, r"^finished: suite exit code 3 after it started \d+s ago; result .*out\.log$")
        self.assertEqual((self.root / "suite" / "out.log").read_text(), "hello\n")

    def test_a_job_that_exits_zero_reads_finished_and_status_succeeds(self):
        self.started("ok", "sh", "-c", "exit 0")
        self.settled("ok", "finished")
        self.assertEqual(self.job("status", "ok")[0], 0)

    def test_a_killed_job_reads_dead_with_no_exit_code(self):
        record = self.started("long", "sleep", "60")
        code, out, err = self.job("status", "long")
        self.assertEqual((code, out.split(":")[0]), (0, "alive"))
        os.killpg(record["pid"], signal.SIGKILL)   # the whole group: what a restart does
        self.assertEqual(self.settled("long", "dead"), ("dead", None))
        code, out, err = self.job("status", "long")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"^dead: long was killed \(no exit code\); it started \d+s ago; what it printed is in .*out\.log$")
        self.assertFalse((self.root / "long" / "exit").exists())

    def test_a_pid_reused_by_another_process_is_not_the_job(self):
        self.started("reuse", "sleep", "60")
        job = sb.job_read("reuse", self.root)
        job["token"] = "Thu Jan  1 00:00:00 1970"   # the recorded start time is not this process's
        (self.root / "reuse" / "job.json").write_text(json.dumps(job))
        self.assertEqual(sb.job_state(sb.job_read("reuse", self.root), self.root)[0], "dead")

    def test_a_record_with_no_pid_reads_unknown(self):
        self.assertEqual(sb.job_state({"name": "x"}, self.root), ("unknown", None))

    def test_a_name_still_alive_is_refused_and_a_dead_or_finished_one_is_replaced_with_its_markers_cleared(self):
        self.started("again", "sleep", "60")
        code, out, err = self.job("start", "again", "--", "true")
        self.assertEqual(code, 1)
        self.assertIn("is still running", err)
        record = self.started("dead", "sleep", "60")
        os.killpg(record["pid"], signal.SIGKILL)
        self.settled("dead", "dead")
        self.job("status", "dead")   # collected
        self.assertTrue((self.root / "dead" / "collected").exists())
        self.started("dead", "sh", "-c", "exit 2")
        self.assertEqual(self.settled("dead", "finished"), ("finished", 2))
        self.assertFalse((self.root / "dead" / "collected").exists(), "a new run starts uncollected")

    def test_a_command_that_cannot_start_is_said_and_leaves_no_record(self):
        def refuse(argv, **kwargs):
            raise OSError(errno.ENOENT, "No such file or directory")
        code, out, err = self.job("start", "nope", "--", "x", spawn=refuse)
        self.assertEqual(code, 1)
        self.assertIn("cannot start x", err)
        self.assertIsNone(sb.job_read("nope", self.root))

    def test_a_bad_name_no_command_and_an_unknown_job_are_refused(self):
        self.assertEqual(self.job("start", "Bad Name", "--", "true")[0], 2)
        self.assertEqual(self.job("start", "x", "--")[0], 2)
        code, out, err = self.job("status", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no job named nope", err)

    def test_asking_after_it_died_collects_it_so_nobody_else_is_told(self):
        record = self.started("c", "sleep", "60")
        os.killpg(record["pid"], signal.SIGKILL)
        self.settled("c", "dead")
        self.assertFalse((self.root / "c" / "collected").exists())
        self.job("status", "c")
        self.assertTrue((self.root / "c" / "collected").exists())

    def test_a_jobs_dir_that_is_not_private_is_refused_and_not_believed(self):
        os.chmod(str(self.root), 0o755)
        code, out, err = self.job("start", "x", "--", "true")
        self.assertEqual(code, 1)
        self.assertIn("not a private directory", err)
        self.assertFalse(sb.job_private(self.root))
        os.chmod(str(self.root), 0o700)
        self.assertTrue(sb.job_private(self.root))

    def test_a_record_is_written_whole_or_not_at_all(self):
        target = self.root / "f"
        sb.job_write_atomic(target, "one")
        with mock.patch.object(sb.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sb.job_write_atomic(target, "two")
        self.assertEqual(target.read_text(), "one", "the old file is intact")
        self.assertEqual([p.name for p in self.root.iterdir()], ["f"], "no temp file is left")

    def test_the_starter_is_established_from_its_own_session_and_otherwise_nobody(self):
        with mock.patch.object(sb, "build_engine", return_value=(types.SimpleNamespace(herdr=None, v2=None), None, None)), \
                mock.patch.object(sb, "session_identity", return_value={"name": "coder"}), mock.patch.dict(os.environ, {sb.SESSION_ENV: "ses_example1"}):
            self.assertEqual(sb.job_starter(None, time.time), ("ses_example1", "coder"))
        with mock.patch.object(sb, "build_engine", side_effect=sb.ConfigError("no config")):
            self.assertEqual(sb.job_starter(None, time.time), (None, None))


class TestPlanKind(unittest.TestCase):
    """#1094: GitHubHub.plan_kind: plan / noplan / unknown, one cached read per issue, no write."""

    def hub(self, answers):
        import types
        calls, t = [], [0.0]

        def gh_api(path, paginate=True, timeout=None, **kw):
            calls.append(path)
            a = answers(path) if callable(answers) else answers
            if isinstance(a, Exception):
                raise a
            return [a]
        hub = types.SimpleNamespace(lock=threading.RLock(), _cache={}, now=lambda: t[0], gh_api=gh_api,
                                    config={"call_timeout_seconds": 5})
        return hub, calls, t

    def kind(self, hub, n=1):
        return sb.GitHubHub.plan_kind(hub, "acme/api", n)

    def test_the_three_kinds(self):
        plan = [{"id": 1, "created_at": "2026-01-01", "body": "## Plan (scenarios)\n\nx"}]
        self.assertEqual(self.kind(self.hub(plan)[0]), "plan")
        self.assertEqual(self.kind(self.hub([{"id": 1, "body": "hello"}])[0]), "noplan")
        self.assertEqual(self.kind(self.hub(sb.GitHubUnavailable("down"))[0]), "unknown")
        self.assertEqual(self.kind(self.hub({"not": "a list"})[0]), "unknown")

    def test_a_second_refresh_within_the_ttl_adds_no_read(self):
        hub, calls, t = self.hub([])
        for n in range(1, 6):
            self.kind(hub, n)
        self.assertEqual(len(calls), 5)
        for n in range(1, 6):
            self.kind(hub, n)
        self.assertEqual(len(calls), 5, "no read of the second refresh")
        t[0] += sb.GITHUB_LOOKUP_TTL + 1
        self.kind(hub, 1)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(c.startswith("repos/") and "comments" in c for c in calls))

    def test_a_full_page_never_reads_noplan_and_a_plan_on_it_is_unknown(self):
        full = [{"id": i, "created_at": "2026-01-01", "body": "x"} for i in range(100)]
        self.assertEqual(self.kind(self.hub(full)[0]), "unknown")
        full[3]["body"] = "## Plan (scenarios)\n\nx"
        self.assertEqual(self.kind(self.hub(full)[0]), "unknown")
        self.assertEqual(self.kind(self.hub(full[:99])[0]), "plan")

    def test_a_plan_in_the_body_reads_noplan(self):
        body = "## Plan (scenarios)\n\nx"

        def answers(path):
            return {"body": body} if path.endswith("/issues/1") else [{"id": 1, "body": "hi"}]
        hub, calls, t = self.hub(answers)
        self.assertEqual(self.kind(hub), "noplan")   # the ask says a plan COMMENT; a plan-looking BODY does not count

    def test_every_row_is_read_over_several_refreshes(self):
        hub, calls, t = self.hub([])
        hub.plan_kind = lambda r, n, b=None: sb.GitHubHub.plan_kind(hub, r, n, b)
        records = [{"repo": "acme/api", "number": n} for n in range(60)]
        for _ in range(7):
            sb.GitHubHub.plan_kinds(hub, records, [sb.PLAN_KIND_READS_PER_REFRESH])
        self.assertEqual(len(calls), 60)
        kinds = sb.GitHubHub.plan_kinds(hub, records, [sb.PLAN_KIND_READS_PER_REFRESH])
        self.assertEqual(set(kinds.values()), {"noplan"})

    def test_never_read_rows_are_served_before_retries(self):
        bad = set(range(11))

        def answers(path):
            n = int(path.split("/issues/")[1].split("/")[0])
            return sb.GitHubUnavailable("x") if n in bad else []
        hub, calls, t = self.hub(answers)
        hub.plan_kind = lambda r, n, b=None: sb.GitHubHub.plan_kind(hub, r, n, b)
        records = [{"repo": "acme/api", "number": n} for n in range(11)]
        sb.GitHubHub.plan_kinds(hub, records, [10])
        t[0] += sb.GITHUB_LOOKUP_RETRY + 1
        records += [{"repo": "acme/api", "number": n} for n in range(100, 105)]
        calls.clear()
        kinds = sb.GitHubHub.plan_kinds(hub, records, [10])
        for n in range(100, 105):
            self.assertEqual(kinds[("acme/api", n)], "noplan")

    def test_one_page_one_call_no_pagination(self):
        hub, calls, t = self.hub([])
        seen = []
        orig = hub.gh_api
        hub.gh_api = lambda path, paginate=True, **kw: (seen.append(paginate), orig(path, paginate=paginate, **kw))[1]
        self.kind(hub)
        self.assertEqual(seen, [False])
        self.assertIn("per_page=100", calls[0])

    def test_a_failed_read_is_retried_after_the_short_ttl_not_the_full_one(self):
        for err in (sb.GitHubUnavailable("403"), sb.GitHubUnavailable("404"), TimeoutError("t")):
            hub, calls, t = self.hub(err)
            self.assertEqual(self.kind(hub), "unknown")
            self.kind(hub)
            self.assertEqual(len(calls), 1)
            t[0] += sb.GITHUB_LOOKUP_RETRY + 1
            self.kind(hub)
            self.assertEqual(len(calls), 2, "retried after the short ttl")

    def test_a_refresh_makes_at_most_the_cap_of_reads(self):
        hub, calls, t = self.hub([])
        budget = [sb.PLAN_KIND_READS_PER_REFRESH]
        kinds = [sb.GitHubHub.plan_kind(hub, "acme/api", n, budget) for n in range(60)]
        self.assertEqual(len(calls), sb.PLAN_KIND_READS_PER_REFRESH)
        self.assertEqual(kinds.count("unknown"), 60 - sb.PLAN_KIND_READS_PER_REFRESH)
        self.assertEqual(sb.PLAN_KIND_READS_PER_REFRESH, 10)

    def test_the_cache_is_bounded(self):
        hub, calls, t = self.hub([])
        for n in range(sb.PLAN_KIND_CACHE_MAX + 50):
            self.kind(hub, n)
        self.assertLessEqual(len(hub._cache), sb.PLAN_KIND_CACHE_MAX)


class TestAuditRotation(unittest.TestCase):
    """The audit log is rotated at an append boundary: the old part is archived (gzip, 0600, atomic), the recent part stays, readers of the tail keep working,
    a line appended during the swap is carried, archives age out, and a non-intent append is not fsynced on every write."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="audit-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.path = self.dir / "audit.jsonl"
        self.log = sb.AuditLog(self.path)

    def line(self, when, event="x", pad=0):
        stamp = _dt.datetime.fromtimestamp(when, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps({"actor": "t", "detail": {"pad": "p" * pad}, "event": event, "subject": None, "ts": stamp}, sort_keys=True) + "\n"

    def fill(self, now, old=30, recent=10, pad=300):
        with open(str(self.path), "w") as handle:
            for n in range(old):
                handle.write(self.line(now - 3 * 86400 + n, "old", pad))
            for n in range(recent):
                handle.write(self.line(now - 60 + n, "recent", pad))

    META = ("audit.rotated", "audit.rotate_failed")

    def events(self, path=None, meta=False):
        """The event names in the file; a line that is not JSON reads as "?"; the log's own rotation notices only with meta=True."""
        found = []
        for text in (path or self.path).read_text().splitlines():
            try:
                found.append(json.loads(text)["event"])
            except ValueError:
                found.append("?")
        return found if meta else [e for e in found if e not in self.META]

    def test_the_old_part_is_archived_and_the_last_day_stays_in_the_active_file(self):
        now = time.time()
        self.fill(now)
        size = self.path.stat().st_size
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", size - 1):
            archive = self.log.rotate(now)
        self.assertEqual(archive.name, "audit-%s.jsonl.gz" % _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d"))
        self.assertEqual(oct(archive.stat().st_mode & 0o777), "0o600")
        with gzip.open(str(archive), "rt") as packed:
            self.assertEqual([json.loads(l)["event"] for l in packed], ["old"] * 30)
        self.assertEqual(self.events(), ["recent"] * 10)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.tmp")), [], "no temp file left")

    def test_nothing_happens_at_or_below_the_threshold_and_an_append_over_it_rotates_by_itself(self):
        now = time.time()
        self.fill(now)
        size = self.path.stat().st_size
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", size):
            self.assertIsNone(self.log.rotate(now))
            self.assertEqual(len(self.events()), 40)
            self.log.append("t", "after", None, {})          # the append finds the file over the threshold
        self.assertEqual(len(list(self.dir.glob("audit-*.jsonl.gz"))), 1)
        self.assertEqual(self.events()[-1], "after")
        self.assertNotIn("old", self.events())

    def test_a_file_that_is_all_recent_keeps_at_most_three_quarters_of_the_threshold(self):
        now = time.time()
        self.fill(now, old=0, recent=60)
        size = self.path.stat().st_size
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", size - 1):
            self.assertIsNotNone(self.log.rotate(now))
        self.assertLessEqual(self.path.stat().st_size, (size - 1) * 3 // 4 + 400)
        self.assertEqual(self.events()[-1], "recent")

    def test_a_line_appended_during_the_swap_is_carried_not_lost(self):
        now = time.time()
        self.fill(now)
        size = self.path.stat().st_size
        real = sb.AuditLog._swap

        def swap_with_a_late_append(this, keep_temp, offset):
            sb.AuditLog(this.path).append("t", "late")   # an appender writes to the OLD file just before the swap
            return real(this, keep_temp, offset)
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", size - 1), mock.patch.object(sb.AuditLog, "_swap", swap_with_a_late_append):
            self.log.rotate(now)
        self.assertEqual(self.events(), ["recent"] * 10 + ["late"])

    def test_readers_of_the_tail_work_across_a_rotation(self):
        now = time.time()
        with open(str(self.path), "w") as handle:
            for n in range(40):
                entry = {"ts": _dt.datetime.fromtimestamp(now - 3 * 86400 + n, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": "p", "event": "policy.judge",
                         "subject": "s", "detail": {"judged": True, "outcome": "allow", "latency_ms": 10, "pad": "p" * 300}}
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
            for n in range(5):
                entry = {"ts": _dt.datetime.fromtimestamp(now - 30 + n, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": "p", "event": "policy.judge",
                         "subject": "s", "detail": {"judged": True, "outcome": "ask", "configured": "allow", "latency_ms": 20}}
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        before = sb.policy_sample(self.dir, now, 3600)
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", self.path.stat().st_size - 1):
            self.log.rotate(now)
        after = sb.policy_sample(self.dir, now, 3600)
        self.assertEqual((after["calls"], after["tightened_ask"], after["latency_p50_ms"]), (before["calls"], before["tightened_ask"], before["latency_p50_ms"]))
        self.assertEqual(after["calls"], 5)
        self.assertIsNone(sb.stopped_news_summary(self.dir, now))   # reads the same tail: no error across the swap

    def test_archives_older_than_fourteen_days_are_deleted_and_the_rest_kept(self):
        now = time.time()
        self.fill(now)
        old, young = self.dir / "audit-2020-01-01.jsonl.gz", self.dir / "audit-archive-20261014T0100.jsonl.gz"   # the second: a hand-made archive of another name
        for path, age in ((old, 15 * 86400), (young, 13 * 86400)):
            path.write_bytes(b"x")
            os.utime(str(path), (now - age, now - age))
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", self.path.stat().st_size - 1):
            self.log.rotate(now)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists(), "a hand-made archive is kept, by its mtime, and never renamed or opened")
        self.assertEqual(young.read_bytes(), b"x")

    def test_a_second_rotation_on_the_same_day_does_not_overwrite_the_first_archive(self):
        now = time.time()
        for _ in range(2):
            self.fill(now)
            with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", self.path.stat().st_size - 1):
                self.log.rotate(now)
        self.assertEqual(len(list(self.dir.glob("audit-*.jsonl.gz"))), 2)

    def test_a_held_lock_skips_the_rotation_and_a_failed_rotation_leaves_the_log_whole(self):
        now = time.time()
        self.fill(now)
        before = self.path.read_text()
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", len(before) - 1):
            lock = os.open(str(self.dir / "audit.jsonl.rotate.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.assertIsNone(self.log.rotate(now), "another process is rotating")
            finally:
                os.close(lock)
            with mock.patch.object(sb.AuditLog, "_swap", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.log.rotate(now)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.tmp")) + sorted(p.name for p in self.dir.glob("audit-*.gz")), [], "no archive, no temp file")
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", 1), mock.patch.object(sb.AuditLog, "rotate", side_effect=OSError("boom")):
            self.log.append("t", "still-written")   # a rotation that fails never fails the append
        self.assertEqual(self.events()[-1], "still-written")

    def test_only_an_intent_is_fsynced_every_time_the_others_at_most_once_a_second(self):
        syncs, clock = [], [1000.0]
        with mock.patch.object(sb.os, "fsync", lambda fd: syncs.append(fd)), mock.patch.object(sb.time, "monotonic", lambda: clock[0]):
            sb.AuditLog._synced.clear()
            for _ in range(10):
                self.log.append("t", "x")
            self.assertEqual(len(syncs), 1, "ten quick appends: one fsync")
            for _ in range(3):
                self.log.append("t", "x", None, {}, phase="intent")
            self.assertEqual(len(syncs), 4, "every intent is synced: it must be on disk before the write it announces")
            clock[0] += 1.5
            self.log.append("t", "x")
            self.assertEqual(len(syncs), 5, "after a second the next one is synced again")

    def test_an_appender_waits_for_the_exclusive_swap_lock_and_then_writes_to_the_file_in_place(self):
        """The swap holds `audit.jsonl.append.lock` exclusively: an appender started meanwhile blocks (the lock decides, so there is no race here) and writes after."""
        self.log.append("t", "before")
        lock = os.open(str(self.dir / "audit.jsonl.append.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = []
        thread = threading.Thread(target=lambda: done.append(sb.AuditLog(self.path).append("t", "during")))
        thread.start()
        thread.join(0.3)
        self.assertTrue(thread.is_alive(), "the appender is blocked on the lock the swap holds")
        self.assertEqual(self.events(), ["before"])
        os.replace(str(self.path), str(self.dir / "moved"))      # the swap: the file is replaced while the appender waits
        (self.dir / "audit.jsonl").write_text(self.line(time.time(), "carried"))
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.events(), ["carried", "during"], "the line landed in the NEW file, not the replaced one")

    def test_a_line_with_no_timestamp_in_the_recent_part_is_kept_and_a_file_already_rotated_is_left_alone(self):
        now = time.time()
        with open(str(self.path), "w") as handle:
            for n in range(20):
                handle.write(self.line(now - 3 * 86400 + n, "old", 300))
            handle.write("not json at all\n")
            handle.write(self.line(now - 5, "recent"))
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", self.path.stat().st_size - 1):
            self.assertIsNotNone(self.log.rotate(now))
            self.assertEqual(self.events(), ["?", "recent"], "an unreadable line is kept with the recent part, not dropped")
            self.assertEqual(self.events(meta=True)[-1], "audit.rotated", "the rotation says it happened")
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", 10 ** 9):
            self.assertIsNone(self.log.rotate(now), "another process rotated first: nothing to do")

    def test_a_rotation_that_keeps_failing_is_said_once_on_stderr_and_in_the_audit(self):
        sb.AuditLog._failed.clear()
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", 1), mock.patch.object(sb.AuditLog, "rotate", side_effect=PermissionError("denied")), captured() as (out, err):
            for _ in range(3):
                self.log.append("t", "x")
        self.assertEqual(err.getvalue().count("could not be rotated"), 1)
        self.assertEqual([e for e in self.events(meta=True) if e == "audit.rotate_failed"], ["audit.rotate_failed"])

    def test_a_rotation_and_a_pruning_are_audited(self):
        now = time.time()
        self.fill(now)
        old = self.dir / "audit-2020-01-01.jsonl.gz"
        old.write_bytes(b"x")
        os.utime(str(old), (now - 20 * 86400, now - 20 * 86400))
        with mock.patch.object(sb, "AUDIT_ROTATE_BYTES", self.path.stat().st_size - 1):
            self.log.rotate(now)
        entry = json.loads(self.path.read_text().splitlines()[-1])
        self.assertEqual(entry["event"], "audit.rotated")
        self.assertEqual((entry["detail"]["pruned"], entry["detail"]["archive"].startswith("audit-")), (["audit-2020-01-01.jsonl.gz"], True))


class TestAgentsVerify(unittest.TestCase):
    """`agents verify`: the running service's registered agents against the rules each kit definition declares (not a count: the service adds default
    rules of its own and their number changes between versions)."""

    DEFAULTS_10 = [{"action": "read", "resource": "*.env", "effect": "ask"}, {"action": "*", "resource": "*", "effect": "allow"}] + [
        {"action": "default%d" % n, "resource": "*", "effect": "allow"} for n in range(8)]
    DEFAULTS_5 = DEFAULTS_10[:5]

    @staticmethod
    def rows_of(name, drop=(), flip=()):
        rules = [r for r in sb.agent_definition_rules(name) if r not in drop]
        return [{"action": a, "resource": res, "effect": ({"allow": "deny", "deny": "allow", "ask": "allow"}[e] if (a, res, e) in flip else e)}
                for a, res, e in rules]

    def listing(self, defaults=None, **changes):
        """The service's answer: each agent of ours with its own rules (as `agents verify` reads them) plus `defaults`; `changes` {name: (mode, rows)}."""
        defaults = self.DEFAULTS_10 if defaults is None else defaults
        rows = {name: (mode, self.rows_of(name) + defaults) for name, mode in sb.EXPECTED_AGENTS.items()}
        rows.update(changes)
        rows["build"] = ("primary", list(defaults))   # not ours: ignored
        return json.dumps({"location": {}, "data": [{"id": n, "mode": m, "permissions": r} for n, (m, r) in rows.items()]})

    def run_verify(self, stdout="", returncode=0, raises=None, drop=()):
        def runner(argv, **kwargs):
            self.assertEqual(argv, ["opencode", "api", "GET", "/api/agent"])
            self.assertIn("timeout", kwargs)
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, "boom" if returncode else "")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.dict(os.environ):   # main() sets a variable for its own process
            code = sb.main(["agents", "verify"], runner=runner)
        return code, out.getvalue(), err.getvalue()

    def test_every_agent_registered_as_written_passes_under_a_ten_rule_and_a_five_rule_default(self):
        for defaults in (self.DEFAULTS_10, self.DEFAULTS_5, []):
            with self.subTest(defaults=len(defaults)):
                code, out, err = self.run_verify(self.listing(defaults))
                self.assertEqual((code, err), (0, ""))
                self.assertIn("agents: PASS (9 of 9", out)

    def test_a_reviewer_registered_as_a_primary_with_default_rules_fails_and_is_named(self):
        code, out, err = self.run_verify(self.listing(**{"reviewer-code": ("primary", list(self.DEFAULTS_10))}))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  reviewer-code: registered as primary, expected subagent", out)
        self.assertEqual(out.count("FAIL"), 1)

    def test_a_default_agent_with_none_of_our_rules_fails_whatever_its_default_count(self):
        """The parse failure: OpenCode registers a default primary agent. For the coder that is the mode it should have, so the rules decide."""
        for defaults in (self.DEFAULTS_10, self.DEFAULTS_5):
            with self.subTest(defaults=len(defaults)):
                code, out, err = self.run_verify(self.listing(**{"coder": ("primary", list(defaults))}))
                self.assertEqual(code, 1)
                self.assertRegex(out, r"FAIL  coder: \d+ of its \d+ rules \(from .*coder.md\) are not registered as written")

    def test_a_missing_rule_and_a_rule_with_another_effect_fail_and_a_missing_agent_too(self):
        coder = sb.agent_definition_rules("coder")
        listing = self.listing(**{"coder": ("primary", self.rows_of("coder", drop=[coder[-1]]) + self.DEFAULTS_5),
                                  "orchestrator": ("primary", self.rows_of("orchestrator", flip=[sb.agent_definition_rules("orchestrator")[0]]) + self.DEFAULTS_5)})
        code, out, err = self.run_verify(listing.replace('"id": "reviewer-impact"', '"id": "someone-else"'))
        self.assertEqual(code, 1)
        self.assertRegex(out, r"coder: 1 of its %d rules \(from .*coder.md\) are not registered as written" % len(coder))
        self.assertRegex(out, r"orchestrator: 1 of its \d+ rules \(from .*orchestrator.md\)")
        self.assertIn("reviewer-impact: not registered", out)

    def test_the_order_of_the_registered_rules_does_not_matter(self):
        rows = list(reversed(self.rows_of("coder") + self.DEFAULTS_5))
        code, out, err = self.run_verify(self.listing(**{"coder": ("primary", rows)}))
        self.assertEqual((code, err), (0, ""))

    def test_every_shipped_definition_is_read_whole_no_line_of_its_permission_block_is_skipped(self):
        for name in sb.EXPECTED_AGENTS:
            rules, unread, path = sb.agent_definition(name)
            self.assertEqual(unread, 0, "%s: %d lines of its permission block are not in a form the check reads" % (name, unread))
            self.assertTrue(rules, name)

    def test_a_malformed_quoted_rule_is_unread_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "opencode"
            folder.mkdir()
            (folder / "coder.md").write_text('---\nname: coder\nmode: primary\npermissions:\n  - {action: edit, resource: "a\\qb", effect: allow}\n'
                                             '  - {action: read, resource: "*", effect: allow}\n---\nbody\n')
            rules, unread, _ = sb.agent_definition("coder", root)
            self.assertEqual((rules, unread), ([("read", "*", "allow")], 1))
            found = {"coder": ("primary", {("read", "*", "allow")})}
            self.assertRegex("\n".join(sb.agents_problems(found, {"coder": "primary"}, root)), r"coder: 1 lines of the permission block in .*coder.md are not in a form")

    def run_main_verify(self, *argv, definitions=True):
        """`agents verify` through main() with a service answering from the shipped definitions, and (definitions False) none found beside the binary."""
        listing = self.listing()

        def runner(argv_, **kwargs):
            return subprocess.CompletedProcess(argv_, 0, listing, "")
        out, err = io.StringIO(), io.StringIO()
        patched = unittest.mock.patch.object(sb, "agent_definition", lambda name, root=None: None) if not definitions else contextlib.nullcontext()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.dict(os.environ), patched:
            code = sb.main(["agents", "verify", *argv], runner=runner)
        return code, out.getvalue(), err.getvalue()

    def test_with_no_definitions_beside_the_binary_the_pass_says_mode_only_and_strict_exits_one(self):
        code, out, err = self.run_main_verify(definitions=False)
        self.assertEqual(code, 0, err)
        self.assertIn("agents: PASS on mode only for 9 of 9", out)
        self.assertIn("their rules were not compared", out)
        code, out, err = self.run_main_verify("--strict", definitions=False)
        self.assertEqual(code, 1)
        self.assertIn("PASS on mode only", out)
        code, out, err = self.run_main_verify("--strict")   # definitions present: a full PASS, strict or not
        self.assertEqual((code, err), (0, ""))
        self.assertIn("agents: PASS (9 of 9", out)

    def test_a_line_the_check_cannot_read_and_a_definition_that_is_not_there_are_said_not_passed(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "opencode"
            folder.mkdir()
            (folder / "coder.md").write_text("---\nname: coder\nmode: primary\npermission:\n  edit: allow\n  bash:\n    git *: allow\n---\nbody\n")
            found = {"coder": ("primary", {("edit", "*", "allow")})}
            self.assertRegex("\n".join(sb.agents_problems(found, {"coder": "primary"}, root)), r"coder: 2 lines of the permission block in .*coder.md are not in a form")
            unchecked = []
            self.assertEqual(sb.agents_problems(found, {"coder": "primary"}, root + "/none", unchecked), [])
            self.assertEqual(unchecked, ["coder"], "the rules were not compared: said, not passed")
            self.assertEqual(sb.agents_problems({"coder": ("subagent", set())}, {"coder": "primary"}, root + "/none"), [
                "coder: registered as subagent, expected primary (its front matter probably failed to parse: quote any description with a colon)"], "the mode is still checked")

    def test_the_definitions_are_read_as_the_service_registers_them(self):
        """A permission map entry `edit: allow` is action edit, resource `*`; `bash` is action `shell`. A list entry keeps its action, resource and effect."""
        rules = sb.agent_definition_rules("coder")
        self.assertIn(("edit", "*", "allow"), rules)
        self.assertIn(("shell", "*", "allow"), rules)
        self.assertIn(("external_directory", "*", "allow"), rules)
        self.assertNotIn(("bash", "*", "allow"), rules)
        self.assertIn(("*", "*", "deny"), sb.agent_definition_rules("reviewer-code"))
        self.assertIn(("shell", "fleet-switchboard review laws *", "allow"), sb.agent_definition_rules("reviewer-code"))
        self.assertIsNone(sb.agent_definition_rules("nobody"))

    def test_a_service_that_is_not_running_is_said_and_is_not_a_failure(self):
        for kwargs in ({"raises": subprocess.TimeoutExpired("opencode", 20)}, {"raises": FileNotFoundError(2, "No such file")}, {"returncode": 1}):
            with self.subTest(kwargs=sorted(kwargs)):
                code, out, err = self.run_verify(**kwargs)
                self.assertEqual(code, 0)
                self.assertIn("not running or not reachable, so nothing was checked", out)

    def test_strict_makes_a_service_that_is_not_running_a_failure(self):
        out, err = io.StringIO(), io.StringIO()
        def down(argv, **kwargs):
            raise FileNotFoundError(2, "No such file")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.dict(os.environ):
            self.assertEqual(sb.main(["agents", "verify", "--strict"], runner=down), 1)
            self.assertEqual(sb.main(["agents", "verify"], runner=down), 0)
        self.assertEqual(out.getvalue().count("nothing was checked"), 2)

    def test_an_answer_that_is_not_the_agent_list_fails(self):
        code, out, err = self.run_verify("<html>not json</html>")
        self.assertEqual(code, 1)
        self.assertIn("not the agent list", err)

    def test_the_table_names_every_agent_the_kit_installs_and_each_definition_declares_rules_and_the_mode(self):
        self.assertEqual(set(sb.EXPECTED_AGENTS), set(sb.KIT_AGENTS))
        for name, mode in sb.EXPECTED_AGENTS.items():
            folder = "reviewers" if name.startswith("reviewer-") else "opencode"
            text = (REPO / "agents" / folder / (name + ".md")).read_text()
            self.assertRegex(text, r"(?m)^mode: %s$" % mode if mode == "subagent" else r"(?m)^description:")
            self.assertTrue(sb.agent_definition_rules(name), "%s declares no rules the check can read: its permission block would not be verified" % name)
        self.assertIn("agents verify", (REPO / "INSTALL.md").read_text())
        self.assertIn("agents verify", (REPO / "docs" / "switchboard.md").read_text())
        self.assertIn("--strict", (REPO / "INSTALL.md").read_text())


class TestReviewLawsStarterSet(Base):
    """#179 slice L1: the generic review laws and standards starter set (docs/review-laws/) and `review-laws install`."""

    DIR = REPO / "docs" / "review-laws"
    FILES = ("REVIEW_LAWS.md", "CODING_STANDARDS.md", "SECURITY_STANDARDS.md", "TEST_STANDARDS.md")
    PREFIX = {"REVIEW_LAWS.md": "LAW", "CODING_STANDARDS.md": "CODE", "SECURITY_STANDARDS.md": "SEC", "TEST_STANDARDS.md": "TEST"}
    RULE = re.compile(r"^- \*\*R-(LAW|CODE|SEC|TEST)-([0-9]+) [^*]+\*\* .+$")

    def rules(self, name):
        text = (self.DIR / name).read_text()
        body = text.split("\n## Rules\n", 1)[1]
        lines = []
        for line in body.splitlines():
            if line.startswith("## "):
                break
            if line.strip():
                lines.append(line)
        return lines

    def install(self, *argv):
        with captured() as (out, err):
            code = sb.main(["review-laws", "install", *argv], runner=lambda *a, **k: None, now=lambda: 0)
        return code, out.getvalue(), err.getvalue()

    def test_every_file_opens_with_the_extends_line_naming_the_version(self):
        version = (self.DIR / "VERSION").read_text().strip()
        self.assertRegex(version, r"^review-laws-[0-9]+$")
        for name in self.FILES:
            self.assertEqual((self.DIR / name).read_text().splitlines()[0], "extends: fleet-kit-review-laws @ %s" % version, name)

    def test_every_rule_is_a_numbered_citable_line_with_a_unique_sequential_id(self):
        for name in self.FILES:
            with self.subTest(name=name):
                lines = self.rules(name)
                self.assertGreaterEqual(len(lines), 10)
                numbers = []
                for line in lines:
                    match = self.RULE.match(line)
                    self.assertIsNotNone(match, "a rules line that is not a numbered rule: %r" % line[:80])
                    self.assertEqual(match.group(1), self.PREFIX[name])
                    numbers.append(int(match.group(2)))
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)), "ids are sequential and unique")

    def test_the_standards_stay_short_enough_for_a_reviewer_to_carry(self):
        for name in self.FILES[1:]:
            self.assertLess(len((self.DIR / name).read_bytes()), 6144, name)
        self.assertLess(len((self.DIR / "REVIEW_LAWS.md").read_bytes()), 10240)

    def test_review_laws_cite_mil_std_882e_with_software_definitions_the_matrix_and_the_one_likelihood_sentence(self):
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        self.assertIn("MIL-STD-882E", text)
        for word in ("Catastrophic", "Critical", "Marginal", "Negligible", "Frequent", "Probable", "Occasional", "Remote", "Improbable",
                     "Eliminated", "High", "Serious", "Medium", "Low"):
            self.assertIn(word, text)
        self.assertIn("not rated", text)
        self.assertEqual(text.count("Judge likelihood at the scale the change will run"), 1)
        self.assertNotIn("fleet-wide", text, "the single-item versus fleet-wide split is not adopted")
        rows = [l for l in text.splitlines() if re.match(r"^\| (Catastrophic|Critical|Marginal|Negligible) \| (Serious|High|Medium|Low) \(", l)]
        self.assertEqual(len(rows), 4)
        cells = {}
        for row in rows:
            parts = [c.strip() for c in row.strip("|").split("|")]
            self.assertEqual(len(parts), 6, row)
            for cell in parts[1:]:
                self.assertRegex(cell, r"^(High|Serious|Medium|Low) \([H2.]\)$")
            cells[parts[0]] = parts[1:]
        # the hold bands are exactly serious and high; medium asks for a second review; low does nothing
        for severity, row in cells.items():
            for cell in row:
                level, action = re.match(r"^(\w+) \((.)\)$", cell).groups()
                self.assertEqual(action, {"High": "H", "Serious": "H", "Medium": "2", "Low": "."}[level], cell)
        self.assertEqual(cells["Catastrophic"], ["Serious (H)", "High (H)", "High (H)", "High (H)", "High (H)"])

    def test_the_matrix_fixture_is_the_single_source_and_the_laws_table_equals_it(self):
        fixture = json.loads((self.DIR / "impact-matrix.json").read_text())
        self.assertEqual(fixture["severities"], ["Catastrophic", "Critical", "Marginal", "Negligible"])
        self.assertEqual(fixture["probabilities"], ["Improbable", "Remote", "Occasional", "Probable", "Frequent"])
        self.assertEqual(fixture["not_rated"], ["Eliminated"])
        self.assertEqual(sorted(set(fixture["actions"].values())), sorted(["H", "2", "."]))
        cells = fixture["cells"]
        self.assertEqual(sum(len(row) for row in cells.values()), 20)
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        for severity in fixture["severities"]:
            row = next(l for l in text.splitlines() if l.startswith("| %s | " % severity) and "(" in l)
            parts = [c.strip() for c in row.strip("|").split("|")]
            want = ["%s (%s)" % (cells[severity][p]["level"], cells[severity][p]["action"]) for p in fixture["probabilities"]]
            self.assertEqual(parts[1:], want, severity)
        for severity, row in cells.items():
            for probability, cell in row.items():
                self.assertEqual(cell["action"], fixture["actions"][cell["level"]], "%s/%s" % (severity, probability))
        self.assertIn("impact-matrix.json", text, "the laws name their single source")

    def test_the_laws_state_the_rules_the_plan_requires(self):
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        for needle in ("50 turns", "`incomplete`", "never builds, runs or tests", "base branch", "A finding that cites none is a proposal",
                       "must not contradict the repository's `AGENTS.md`", "untrusted text"):
            self.assertIn(needle, text)

    def test_the_scope_of_the_read_only_rule_and_the_floor_as_the_matrixs_companion_are_stated(self):
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        rule2 = next(l for l in text.splitlines() if l.startswith("- **R-LAW-2 "))
        self.assertIn("reviewer agent bound by these laws", rule2)
        self.assertIn("independent verification agent", rule2)
        self.assertIn("may run the repository's named checks", rule2)
        self.assertIn("says so in its own copy", rule2)
        rules = {l.split(" ", 2)[1].strip("*"): l for l in text.splitlines() if l.startswith("- **R-LAW-")}
        self.assertIn("The floor is the matrix's companion", rules["R-LAW-13"])
        self.assertIn("its authority is the matrix (R-LAW-11)", rules["R-LAW-13"])
        self.assertIn("the impact matrix below", rules["R-LAW-11"])
        self.assertNotIn("also holds changes", text, "the floor is a numbered rule, not an uncited aside")

    def test_r_law_16_and_17_are_in_both_copies_after_r_law_15_and_the_version_is_bumped(self):
        docs = (self.DIR / "REVIEW_LAWS.md").read_text()
        root = (REPO / "REVIEW_LAWS.md").read_text()
        self.assertEqual(docs, root, "the repository's own copy equals the starter set")
        for name in self.FILES:   # the repository's own copies may carry a section of their own; they open with the same version line
            self.assertEqual((self.DIR / name).read_text().splitlines()[0], (REPO / name).read_text().splitlines()[0], name)
        ids = [l.split(" ", 2)[1].strip("*") for l in docs.splitlines() if l.startswith("- **R-LAW-")]
        self.assertEqual(ids[ids.index("R-LAW-15") + 1:ids.index("R-LAW-15") + 3], ["R-LAW-16", "R-LAW-17"])
        self.assertEqual((self.DIR / "VERSION").read_text().strip(), "review-laws-2")
        rules = {l.split(" ", 2)[1].strip("*"): l for l in docs.splitlines() if l.startswith("- **R-LAW-")}
        for needle in ("A block about behaviour names its check", "check result not read", "stays at most a note", "it never blocks (R-LAW-14)", "reading the head's check results is reading"):
            self.assertIn(needle, rules["R-LAW-16"])
        self.assertNotIn("is a proposal and does not block", rules["R-LAW-16"])
        for needle in ("never the author's to chase", "once per head", "never by the author of the change", "counts for nothing in place of the missing one"):
            self.assertIn(needle, rules["R-LAW-17"])

    def test_owner_evaluators_are_rare_and_the_laws_say_so(self):
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        rule = next(l for l in text.splitlines() if l.startswith("- **R-LAW-15 "))
        for needle in ("Owner evaluators are rare", "own eyes are the test", "once at the epic's integrated head", "never waits on the owner", "claim: epic"):
            self.assertIn(needle, rule)

    def test_every_rule_says_how_it_is_checked_and_a_note_only_rule_never_blocks_alone(self):
        kinds = set()
        for name in self.FILES:
            for line in self.rules(name):
                match = re.search(r"_check: (diff|test|search|note only): [^_]+_$", line)
                self.assertIsNotNone(match, "no check hint: %r" % line[:90])
                kinds.add(match.group(1))
        self.assertEqual(kinds, {"diff", "test", "search", "note only"})
        text = (self.DIR / "REVIEW_LAWS.md").read_text()
        rule14 = next(l for l in text.splitlines() if l.startswith("- **R-LAW-14 "))
        self.assertIn("note only", rule14)
        self.assertIn("never blocks by itself", rule14)
        for rule in ("R-CODE-9", "R-CODE-10", "R-TEST-8"):
            line = next(l for name in self.FILES for l in self.rules(name) if l.startswith("- **%s " % rule))
            self.assertIn("check: note only", line, rule)

    def test_the_standards_carry_the_review_findings_that_recur(self):
        coding = "\n".join(self.rules("CODING_STANDARDS.md")).lower()
        security = "\n".join(self.rules("SECURITY_STANDARDS.md")).lower()
        for needle in ("fail open", "atomic", "check and act", "public-safe"):
            self.assertIn(needle, coding)
        for needle in ("identify the actor", "secret never becomes text", "untrusted text is data", "guardrail is not a boundary"):
            self.assertIn(needle, security)

    def test_the_starter_set_holds_no_private_name(self):
        names = os.environ.get("FLEET_PRIVATE_NAMES_FILE")
        script = REPO / "bin" / "check-public-names"
        done = subprocess.run([sys.executable, str(script), "--root", str(self.DIR)] + (["--names-file", names] if names else []),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_install_copies_exactly_the_four_files_each_opening_with_the_extends_line_and_says_what_to_edit(self):
        target = self.home / "acme-widget"
        target.mkdir()
        code, out, err = self.install(str(target))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(self.FILES), "nothing else is written, no stray temp file")
        version = (self.DIR / "VERSION").read_text().strip()
        for name in self.FILES:
            self.assertEqual((target / name).read_text(), (self.DIR / name).read_text())
            self.assertEqual((target / name).read_text().splitlines()[0], "extends: fleet-kit-review-laws @ %s" % version)
            self.assertIn(name, out)
        self.assertIn("What to edit", out)
        self.assertIn("never loosen", out)

    def test_install_never_overwrites_a_repositorys_own_copy_unless_forced_and_leaves_agents_md_alone(self):
        target = self.home / "acme-widget"
        target.mkdir()
        (target / "CODING_STANDARDS.md").write_text("our own rules\n")
        (target / "AGENTS.md").write_text("build with make\n")
        code, out, err = self.install(str(target))
        self.assertEqual(code, 2)
        self.assertIn("already has CODING_STANDARDS.md", err)
        self.assertEqual((target / "CODING_STANDARDS.md").read_text(), "our own rules\n")
        self.assertFalse((target / "REVIEW_LAWS.md").exists(), "nothing was written")
        code, out, err = self.install(str(target), "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual((target / "CODING_STANDARDS.md").read_text(), (self.DIR / "CODING_STANDARDS.md").read_text())
        self.assertEqual((target / "AGENTS.md").read_text(), "build with make\n")
        self.assertIn("AGENTS.md: it was not touched", out)

    def test_install_never_follows_a_symlink_even_a_dangling_one_and_not_with_force(self):
        target = self.home / "acme-widget"
        target.mkdir()
        elsewhere = self.home / "elsewhere.md"
        (target / "REVIEW_LAWS.md").symlink_to(elsewhere)   # dangling: the file it points at does not exist
        for argv in ((str(target),), (str(target), "--force")):
            code, out, err = self.install(*argv)
            self.assertEqual(code, 2)
            self.assertIn("symbolic link", err)
            self.assertFalse(elsewhere.exists(), "nothing was written through the link")
            self.assertTrue((target / "REVIEW_LAWS.md").is_symlink())
            self.assertFalse((target / "CODING_STANDARDS.md").exists())

    def test_install_says_the_version_once(self):
        target = self.home / "acme-widget"
        target.mkdir()
        code, out, err = self.install(str(target))
        self.assertNotIn("vreview-laws", out)
        self.assertIn("(review-laws-2)", out)

    def test_install_refuses_a_path_that_is_not_a_directory(self):
        code, out, err = self.install(str(self.home / "nowhere"))
        self.assertEqual(code, 2)
        self.assertIn("is not a directory", err)

    def test_the_install_stays_in_step_with_the_starter_set(self):
        # a starter set whose header differs from VERSION is refused by the command (it never copies an inconsistent set)
        real = sb.review_laws_source
        copy = self.home / "set"
        shutil.copytree(self.DIR, copy)
        (copy / "VERSION").write_text("2\n")
        target = self.home / "t"
        target.mkdir()
        with mock.patch.object(sb, "review_laws_source", lambda: copy):
            code, out, err = self.install(str(target))
        self.assertEqual(code, 2)
        self.assertIn("inconsistent", err)
        self.assertEqual(list(target.iterdir()), [])

    def test_the_docs_name_the_command(self):
        self.assertIn("review-laws install", (REPO / "docs" / "switchboard.md").read_text())


class TestPluginPush(unittest.TestCase):
    """#290 slice 6: the plugin's pushes. The receiver only MARKS a session; the pass thread makes the one bounded read."""
    SESSION = "ses_alpha"

    class Stub:
        def __init__(self):
            self.calls = []

        def message_list(self, session, limit=None, order=None, type=None, cursor=None):
            self.calls.append((session, limit, type))
            return []

        def __getattr__(self, name):   # any other call (a write, a send) is a failure of the test
            raise AssertionError("the push must cause no call but a bounded read: %s" % name)

    def setUp(self):
        self.now, self.audits = [1000.0], []
        audit = types.SimpleNamespace(append=lambda actor, name, subject, detail: self.audits.append((name, detail)))
        self.feed = sb.EventFeed(None, audit, lambda: self.now[0])
        self.feed.connected, self.feed.needs_resync = True, False
        self.receiver = sb.EventReceiver(self.feed, audit, lambda: self.now[0], known=lambda: {self.SESSION, "ses_beta"})

    def message(self, **fields):
        body = {"v": 1, "kind": "event", "event": "prompt", "sessionID": self.SESSION, "at": 1000000}
        body.update(fields)
        return json.dumps(body).encode()

    def test_s9_a_good_message_marks_only_that_session_and_the_next_pass_reads_it_once(self):
        engine = types.SimpleNamespace(v2=self.Stub())
        self.assertTrue(self.receiver.handle(self.message()))
        self.assertEqual(engine.v2.calls, [], "the receiver reads nothing")
        self.assertEqual(list(self.feed.seen), [self.SESSION])
        self.feed.tick(engine, [self.SESSION, "ses_beta"])
        self.assertEqual(engine.v2.calls, [(self.SESSION, sb.FEED_PAGE, "assistant")], "one bounded read, for that session only")
        self.feed.tick(engine, [self.SESSION, "ses_beta"])
        self.assertEqual(len(engine.v2.calls), 1, "read once: nothing new was pushed")

    def test_s10_seven_kinds_of_bad_message_touch_nothing_and_are_counted(self):
        bad = [self.message(pad="x" * 3000), self.message(v=2), self.message(event="nonsense"), self.message(sessionID="not a session"),
               self.message(text="a prompt"), b"this is not json", bytes(range(128, 200))]
        for raw in bad:
            self.assertFalse(self.receiver.handle(raw))
        self.assertEqual((self.feed.seen, self.receiver.status()["ignored"]), ({}, 7))
        self.assertFalse(self.receiver.handle(self.message(sessionID="ses_stranger")), "a valid id of no agent of this fleet proves nothing")

    def test_s11_a_burst_is_limited_counted_audited_once_and_never_queued(self):
        for _ in range(100):
            self.receiver.handle(self.message())
        status = self.receiver.status()
        self.assertEqual((status["accepted"], status["rate_limited"]), (sb.PLUGIN_SESSION_PER_SECOND, 100 - sb.PLUGIN_SESSION_PER_SECOND))
        self.assertEqual([n for n, _ in self.audits].count("event.rate_limited"), 1)
        self.now[0] += 2
        self.assertTrue(self.receiver.handle(self.message()), "the next second starts clean: nothing was queued")

    def test_s15_an_event_limited_away_is_still_found_by_the_reconciliation_and_said_to_be_missed(self):
        engine = types.SimpleNamespace(v2=types.SimpleNamespace(calls=[], message_list=lambda *a, **k: [{"id": "msg_new"}]))
        self.feed.need("test")
        self.feed.handle(feed_line("session.step.ended", session=self.SESSION, assistantMessageID="msg_old"))
        for _ in range(sb.PLUGIN_SESSION_PER_SECOND + 5):
            self.receiver.handle(self.message())
        self.assertEqual(self.receiver.status()["rate_limited"], 5, "the last pushes were limited away")
        self.feed.seen[self.SESSION]["read"] = self.now[0] + 1   # the pushed read already happened: only the reconciliation is left to find it
        self.now[0] += sb.FEED_RECONCILE_SECONDS + 2
        self.feed.tick(engine, [self.SESSION])
        self.assertEqual([n for n, _ in self.audits].count("event.missed"), 1)

    def test_a_push_is_read_while_the_stream_is_down_at_most_four_sessions_a_pass_and_a_failed_read_is_tried_again(self):
        sessions = ["ses_%02d" % n for n in range(6)]
        self.receiver.known = lambda: set(sessions)
        self.feed.connected = False   # the stream is down and nobody registered as a consumer
        for session in sessions:
            self.receiver.handle(self.message(sessionID=session))
        engine = types.SimpleNamespace(v2=self.Stub())
        self.feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), sb.FEED_PUSHED_PER_PASS)
        self.feed.tick(engine, sessions)
        self.assertEqual(sorted(c[0] for c in engine.v2.calls), sessions, "the other two the next pass; none twice")

        class Failing:
            calls = 0

            def message_list(self, *a, **k):
                Failing.calls += 1
                raise sb.V2Error("backoff", None, "skipped")
        self.now[0] += 1
        self.receiver.handle(self.message(sessionID="ses_00"))
        self.feed.tick(types.SimpleNamespace(v2=Failing()), sessions)
        self.feed.tick(types.SimpleNamespace(v2=Failing()), sessions)
        self.assertEqual(Failing.calls, 2, "a failed read is tried again, and counted")
        self.assertEqual(self.feed.status()["read_errors"], 2)

    def test_under_v2_light_a_push_causes_no_read(self):
        engine = types.SimpleNamespace(v2=self.Stub(), light=True, feed=self.feed, discovery=types.SimpleNamespace(agents={"a": types.SimpleNamespace(session_id=self.SESSION)}))
        self.receiver.handle(self.message())
        sb.feed_tick(engine)
        self.assertEqual(engine.v2.calls, [], "the owner's hotfix switch stops every optional history read, a pushed one too")

    def test_s16_with_no_pushes_ten_simulated_minutes_for_twenty_idle_agents_cost_at_most_fifty_calls(self):
        engine = types.SimpleNamespace(v2=self.Stub())
        self.feed.need("test")
        sessions = ["ses_%02d" % n for n in range(20)]
        for _ in range(120):
            self.now[0] += 5
            self.feed.tick(engine, sessions)
        self.assertLessEqual(len(engine.v2.calls), 50)

    def test_u4_no_text_of_a_message_reaches_the_audit_or_the_status(self):
        self.receiver.handle(self.message(text="SENTINEL-PROMPT-TEXT"))
        for _ in range(40):
            self.receiver.handle(self.message(event="idle"))
        self.assertNotIn("SENTINEL-PROMPT-TEXT", repr(self.audits) + repr(self.receiver.status()) + repr(self.feed.status()) + repr(self.feed.seen))

    def test_u5_a_pushed_claim_causes_no_write(self):
        engine = types.SimpleNamespace(v2=self.Stub())   # any call but message_list raises
        self.receiver.handle(self.message(id="msg_claimed", person=True))
        self.feed.tick(engine, [self.SESSION])
        self.assertEqual(len(engine.v2.calls), 1)

    def test_the_socket_carries_pushes_to_the_receiver_without_a_reply_and_judge_requests_as_before(self):
        state = Path(tempfile.mkdtemp(prefix="sbp"))
        self.addCleanup(shutil.rmtree, state, True)
        arrived = threading.Event()

        def on_event(raw):
            self.receiver.handle(raw)
            arrived.set()
        server = sb.JudgeServer(state, lambda text: sb.policy_reply("deny", "judged"), on_event).start()
        self.addCleanup(server.stop)
        reply = sb.ask_judge_daemon(state, json.dumps({"sessionID": self.SESSION, "tool": "shell", "arguments": "ls", "outcome": "allow"}), 2)
        self.assertEqual(reply["outcome"], "deny", "a judge request is answered as before")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        with sb.in_directory(state):
            sock.connect(sb.POLICY_SOCKET)
        sock.sendall(self.message() + b"\n")   # no shutdown(SHUT_WR): the server ends at the newline and may close first, and macOS then refuses it (ENOTCONN)
        self.assertEqual(sock.recv(100), b"", "a push gets no reply")
        sock.close()
        self.assertTrue(arrived.wait(5), "the receiver was reached")   # the wait is only a failure bound
        self.assertEqual(self.receiver.status()["accepted"], 1)

    def test_the_socket_is_bound_for_pushes_when_the_judge_is_off_and_only_when_a_feed_runs(self):
        state = Path(tempfile.mkdtemp(prefix="sbp"))
        self.addCleanup(shutil.rmtree, state, True)
        config = {"policy": {"enabled": False}, "v2_plugin_events": "auto"}
        self.assertIsNone(sb.start_judge_server(config, state, None, time.time, types.SimpleNamespace(feed=None, audit=None)))
        engine = types.SimpleNamespace(feed=self.feed, audit=None)
        server = sb.start_judge_server(config, state, None, time.time, engine)
        self.addCleanup(server.stop)
        self.assertTrue((state / sb.POLICY_SOCKET).exists())
        self.assertIsNotNone(engine.event_receiver)
        server.stop()
        self.assertIsNone(sb.start_judge_server(dict(config, v2_plugin_events="off"), state, None, time.time, engine), "off: the gate is the only signal")


class TestReconcileOnRead(unittest.TestCase):
    """Slice 4 of #903 (#919): reconcile on read, never a question mark, dead never drawn."""

    AGENTS = {
        "alpha": {"session": "ses_a", "pane": "p_1", "role": "coder", "state": "working", "kind": "local"},
        "ghost": {"session": "ses_g", "pane": "p_9", "role": "coder", "state": "dead", "kind": "local"},
        "bare": {"session": "ses_b", "pane": "p_2", "role": "coder", "state": None, "kind": "local"},
    }

    def setUp(self):
        self.mod = sb.load_module("agent_reconcile")
        self.dir = Path(tempfile.mkdtemp(prefix="recon"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def registry(self, whole=True):
        """The REAL registry module (slice 2) over a state directory holding the three agents; `whole=False` corrupts the file so the read is partial."""
        reg = sb.load_module("registry")
        for name, agent in self.AGENTS.items():
            reg.upsert(self.dir, name, agent)
        self.reg_state(reg)
        if not whole:
            (self.dir / "registry.json").write_text("{torn")
        return reg

    def reg_state(self, reg):
        path = self.dir / "registry.json"
        doc = json.loads(path.read_text())
        for name, agent in self.AGENTS.items():
            doc["agents"][name]["state"] = agent["state"]
        path.write_text(json.dumps(doc))

    def audit_lines(self):
        path = self.dir / "registry-audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def live(gone=("ghost",)):
        return lambda name, agent: {"pane": name not in gone, "process": name not in gone}

    def test_s3_a_whole_read_removes_the_dead_agent_once_with_one_audit_line(self):
        reg = self.registry()
        removed = self.mod.reconcile_on_read(self.dir, reg.read(self.dir), self.live(), reg)
        self.assertEqual(removed, ["ghost"])
        lines = [l for l in self.audit_lines() if l.get("event") == "reconcile_remove"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["evidence"]["pane_listed"], False)
        self.assertEqual(sorted(reg.read(self.dir)["agents"]), ["alpha", "bare"])

    def test_s3_a_partial_read_removes_nothing_and_asks_the_registry_nothing(self):
        reg = self.registry(whole=False)
        removed = self.mod.reconcile_on_read(self.dir, reg.read(self.dir), self.live(), reg)
        self.assertEqual(removed, [])
        self.assertEqual([l for l in self.audit_lines() if l.get("event") == "reconcile_remove"], [])
        self.assertEqual((self.dir / "registry.json").read_text(), "{torn")   # untouched

    def test_s3_a_stub_that_ignores_the_whole_flag_is_caught(self):
        """The discriminator: with a read that is not whole, a view that acted anyway would remove 1 where 0 are expected."""
        reg = self.registry(whole=False)
        forged = dict(reg.read(self.dir), agents=dict(self.AGENTS))
        self.assertEqual(self.mod.reconcile_on_read(self.dir, forged, self.live(), reg), [])   # the flag is read from the read itself

    def test_s3_the_registry_refuses_a_live_agent_even_with_whole_true(self):
        reg = self.registry()
        result = reg.reconcile_remove(self.dir, "alpha", {"state": "working", "pane_listed": False, "process_found": False}, True)
        self.assertFalse(result["changed"])   # recorded `working`: refused whatever the caller says
        self.assertIn("alpha", reg.read(self.dir)["agents"])
        self.assertFalse(reg.reconcile_remove(self.dir, "ghost", {"pane_listed": False, "process_found": False}, False)["changed"])
        self.assertTrue(self.mod.reconcile_on_read(self.dir, reg.read(self.dir), lambda n, a: {"pane": True, "process": True}, reg) == [])

    def test_s3_an_unchecked_agent_is_not_removed(self):
        """herdr could not say (None): no evidence of death, so nothing is asked of the registry."""
        reg = self.registry()
        unknown = lambda name, agent: {"pane": None, "process": None}
        self.assertEqual(self.mod.reconcile_on_read(self.dir, reg.read(self.dir), unknown, reg), [])
        self.assertIn("ghost", reg.read(self.dir)["agents"])
        broken = lambda name, agent: 1 / 0
        self.assertEqual(self.mod.reconcile_on_read(self.dir, reg.read(self.dir), broken, reg), [])

    def test_s3_a_half_known_live_check_removes_nothing(self):
        reg = self.registry()
        for live in ({"pane": False, "process": None}, {"pane": True, "process": False}, {"pane": None, "process": False}):
            self.assertEqual(self.mod.reconcile_on_read(self.dir, reg.read(self.dir), lambda n, a, live=live: live, reg), [], live)
        self.assertIn("ghost", reg.read(self.dir)["agents"])

    def test_s3_reconcile_agents_on_read_removes_through_the_real_registry_with_herdr_gone_and_keeps_it_when_herdr_is_down(self):
        class Herdr:
            def __init__(self, down):
                self.down = down

            def pane_list(self):
                if self.down:
                    raise sb.HerdrError("unavailable", "cannot run herdr: pane p_9 not found")
                return [{"pane_id": "p_1"}, {"pane_id": "p_2"}]
        reg = self.registry()
        self.assertEqual(sb.reconcile_agents_on_read(self.dir, Herdr(True)), [])
        self.assertIn("ghost", reg.read(self.dir)["agents"])
        self.assertEqual(sb.reconcile_agents_on_read(self.dir, Herdr(False)), ["ghost"])
        self.assertEqual(sorted(reg.read(self.dir)["agents"]), ["alpha", "bare"])

    def test_s3_the_callers_own_guards_with_a_recording_registry(self):
        """The caller's guards, not the registry's backstop: a recording fake counts the calls, so a removed guard shows."""
        class Recording:
            def __init__(self):
                self.calls = []

            def no_pane_to_judge(self, row):
                return sb.load_module("registry").no_pane_to_judge(row)

            def reconcile_remove(self, state_dir, name, evidence, whole):
                self.calls.append((name, evidence, whole))
                return {"changed": True, "problem": None}
        dead = {"ghost": {"state": "dead", "pane": "p_9"}}
        whole = {"whole": True, "agents": dead}
        cases = {
            "not whole": ({"whole": False, "agents": dead}, {"pane": False, "process": False}, 0),
            "whole key missing": ({"agents": dead}, {"pane": False, "process": False}, 0),
            "pane unchecked": (whole, {"pane": None, "process": False}, 0),
            "process unchecked": (whole, {"pane": False, "process": None}, 0),
            "process present": (whole, {"pane": False, "process": True}, 0),
            "pane present": (whole, {"pane": True, "process": False}, 0),
            "both gone": (whole, {"pane": False, "process": False}, 1),
            "recorded live": ({"whole": True, "agents": {"ghost": {"state": "working"}}}, {"pane": False, "process": False}, 0),
        }
        for label, (read, live, count) in cases.items():
            with self.subTest(case=label):
                rec = Recording()
                self.mod.reconcile_on_read(self.dir, read, lambda n, a, live=live: live, rec)
                self.assertEqual(len(rec.calls), count)
                if count:
                    self.assertEqual(rec.calls[0][2], True)
                    self.assertEqual((rec.calls[0][1]["pane_listed"], rec.calls[0][1]["process_found"]), (False, False))

    def test_s3_herdr_live_check_gives_death_evidence_only_for_a_whole_list_that_lacks_the_exact_pane_id(self):
        class Herdr:
            def __init__(self, listing=None, error=None, procs=()):
                self.listing, self.error, self.procs = listing, error, procs

            def pane_list(self):
                if self.error:
                    raise sb.HerdrError(*self.error)
                return self.listing

            def pane_process_info(self, pane):
                return list(self.procs)
        agent = {"pane": "p_1"}
        gone, unknown = {"pane": False, "process": False}, {"pane": None, "process": None}
        cases = {
            "listed and present, process running": (Herdr([{"pane_id": "p_1"}], procs=[{"cmdline": "x"}]), {"pane": True, "process": True}),
            "listed and present, no process": (Herdr([{"pane_id": "p_1"}]), {"pane": True, "process": False}),
            "listed and absent": (Herdr([{"pane_id": "p_2"}, {"pane_id": "p_3"}]), gone),
            "empty list": (Herdr([]), gone),
            "a pane whose id contains this one": (Herdr([{"pane_id": "p_10"}, {"pane_id": "xp_1"}]), gone),
            "list errors with a socket path holding the id": (Herdr(error=("exit", "connect /tmp/p_1/herdr.sock: No such file or directory")), unknown),
            "list errors: pane p_1 not found": (Herdr(error=("exit", "pane p_1 not found")), unknown),
            "list errors: structured not found": (Herdr(error=("exit", json.dumps({"code": "pane_not_found", "message": "pane p_1 not found"}))), unknown),
            "list errors: unavailable": (Herdr(error=("unavailable", "cannot run herdr: No such file or directory")), unknown),
            "list errors: timeout": (Herdr(error=("timeout", "herdr did not answer within 5s")), unknown),
            "list errors: bad json": (Herdr(error=("bad_json", "herdr returned non-JSON")), unknown),
            "list errors: bad shape": (Herdr(error=("bad_shape", "expected result.panes in herdr's reply")), unknown),
            "list is not a list": (Herdr({"panes": []}), unknown),
            "an entry without a pane_id": (Herdr([{"pane_id": "p_2"}, {"id": "p_1"}]), unknown),
            "an entry with a non-string pane_id": (Herdr([{"pane_id": 1}]), unknown),
            "an entry that is not an object": (Herdr(["p_2"]), unknown),
        }
        for label, (herdr, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(sb.herdr_live_check(herdr)("a", agent), expected)
        self.assertEqual(sb.herdr_live_check(Herdr([]))("a", {}), gone)

    def test_s3_herdr_live_check_never_reads_error_text(self):
        """Derived from the code: the function holds no text matching on an error (no substring test, no regex over a message)."""
        import inspect
        source = inspect.getsource(sb.herdr_live_check)
        tree = ast.parse(source)
        self.assertEqual([n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("detail", "reason", "args", "strerror")], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in ("str", "search", "match", "lower")], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Compare) and any(isinstance(o, ast.In) for o in n.ops)], [])
        self.assertNotIn("re.", source)

    def test_s7_only_a_view_reads_ever_calls_reconcile_remove(self):
        """Derived from the code: every call of `reconcile_remove` in the program, plugins and modules sits in agent_reconcile.py (the view's
        helper) or in registry.py (its definition); the daemon's passes and sweeps hold none, so no removal exists without a read."""
        allowed = {"agent_reconcile.py", "registry.py"}
        roots = [BIN, BIN.parent / "plugins"]
        hits = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or "node_modules" in path.parts or path.name.startswith("test-") or ".test." in path.name:
                    continue
                if path.suffix not in (".py", ".mjs", ".js", ".tsx") and path.name != "fleet-switchboard":
                    continue
                if "reconcile_remove(" in path.read_text(errors="replace") and path.name not in allowed:
                    hits.append(str(path.relative_to(BIN.parent)))
        self.assertEqual(hits, [])

    def views(self):
        """The text of the four Python views drawn over a state directory that holds a dead agent, a live one and one with no state row."""
        state = self.dir / "state"
        state.mkdir()
        panes = {"agents": {n: {"session": a["session"], "pane": a["pane"], "role": "coder", "workspace": "w1"} for n, a in self.AGENTS.items()},
                 "states": {"alpha": {"state": "attached"}, "ghost": {"state": "dead", "pane": "p_9", "at": 1}}, "missing": {}}
        (state / sb.PANE_HEALTH_FILE).write_text(json.dumps(panes))
        (state / "registry.json").write_text(json.dumps({"version": 1, "agents": self.AGENTS, "retired": {}}))
        load = {"latest": {"orch": {"open_asks": 1, "stalled": 0, "oldest_stall_hours": 0, "moved_1h": 0, "messages": 0, "decisions": 0, "busy": 1, "idle": 0}}, "alert": False}
        (state / "load.json").write_text(json.dumps(load))
        (state / "decisions.json").write_text(json.dumps({"decisions": [], "at": 0}))
        recorder = sb.load_module("health_records").Recorder(lambda: 1000)
        return {
            "status": "\n".join(sb.pane_health_status_lines(state, 1000)),
            "load line": "\n".join(sb.load_fleet_health().status_lines(state, 1000)),
            "decisions": "\n".join(sb.format_decisions_status(sb.decisions_summary(state, lambda: 1000))),
            "health records": json.dumps(recorder.body()),
        }

    def test_s4_no_python_view_prints_a_question_mark_for_an_agent_state(self):
        for view, text in self.views().items():
            with self.subTest(view=view):
                self.assertNotIn("?", text.replace("`", "").replace("%s " % sb.PROG, "") if view == "decisions" else text)

    def test_s10_no_python_view_draws_the_word_dead(self):
        for view, text in self.views().items():
            with self.subTest(view=view):
                self.assertNotIn("dead", text.lower())

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
from support.core import Base, FakeRunner
from support.fake_hubs import FakeTriageHub, tri_issue
from support.fake_jev import FakeJev, FakeOpener, JEV_SETTINGS, KEY_VALUE, confident, http_error
from support.loader import captured, sb, switchboard_source
from support.paths import REPO
from support.rigs import GithubRig, World, build_carry_repo
from support.scenario_roles import FAKE_GH, GH_REPO
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE, HERDR_ALLOWED, SES, fact


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


class TestClearMarker(Base):
    """#522: the owner's clear also changes the first line `MERGE: captain` of the body to `MERGE: automatic`, through ONE shared function, never on a carry, never undoing the clear."""

    REPO, PR = "acme-org/widget-api", "acme-org/widget-api#7"
    HEAD = "a" * 40
    BODY = "MERGE: captain\r\n\r\nPLAN: x\nMERGE: captain (quoted later)\n\ttail  \n"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("cos",), config={"gh": FAKE_GH, "impact_hold": {"enabled": True}})
        self.engine = self.world.new_engine("daemon")
        self.engine.gh_runner, self.engine.state_dir = self.route, self.world.state
        self.names = sb.label_names(self.engine.config)
        self.body, self.head, self.writes, self.reads = self.BODY, self.HEAD, [], 0
        self.read_fails, self.write_fails, self.change_between = False, False, None
        self.labels = [self.names["merge-captain"], self.names["hold-impact"]]

    def route(self, argv, **kwargs):
        if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
            if self.read_fails:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
            raw = {"state": "OPEN", "headRefOid": self.head, "title": "t", "labels": [{"name": n} for n in self.labels], "body": self.body}
            self.reads = getattr(self, "reads", 0) + 1
            if self.change_between is not None and self.reads == 1:   # the clear's own read (which the edit reuses) is the first: the re-read before the write sees another body
                self.body, self.change_between = self.change_between, None
            return subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
        if argv[0] == FAKE_GH and argv[1] == "api" and "--method" in argv:
            method = argv[argv.index("--method") + 1]
            if method == "PATCH":
                if self.write_fails:
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
                self.writes.append(json.loads(kwargs["input"])["body"])
                self.body = self.writes[-1]
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            if method == "DELETE":
                self.labels = [l for l in self.labels if not argv[2].endswith("/" + urllib.parse.quote(l, safe=""))]
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return self.world.route(argv, **kwargs)

    def clear(self, **kwargs):
        return sb.impact_clear_core(self.engine, self.route, self.REPO, 7, "owner", "a false hold: it is a fixture", **kwargs)

    def events(self, name):
        return [e for e in self.world.audit() if e["event"] == name]

    def test_s1_s8_the_clear_changes_only_the_first_line_and_every_other_byte_stays(self):
        lines = self.clear()
        self.assertEqual(self.body, "MERGE: automatic\r\n\r\nPLAN: x\nMERGE: captain (quoted later)\n\ttail  \n")
        self.assertEqual(len(self.writes), 1)
        self.assertIn("changed the first line of the body of %s from MERGE: captain to MERGE: automatic" % self.PR, lines)
        self.assertNotIn(self.names["hold-impact"], self.labels)

    def test_s2_the_chat_clear_goes_through_the_same_shared_function(self):
        calls = []
        real = sb.neutralise_marker
        sb.neutralise_marker = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
        self.addCleanup(setattr, sb, "neutralise_marker", real)
        self.clear(remove=[self.names["hold-impact"], self.names["merge-captain"]], head=self.HEAD, via="chat:m1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.body.startswith("MERGE: automatic"))

    def test_s3_a_body_whose_first_line_is_not_the_marker_is_not_written(self):
        for body in ("Title\nMERGE: captain\n", "", "MERGE: automatic\n", "\n\nMERGE: captainship\n"):
            self.body, self.writes = body, []
            self.assertEqual(sb.marker_edit(body) is None, True, body)
            self.clear()
            self.assertEqual((self.writes, self.body), ([], body))

    def test_a_blank_lines_before_the_marker_are_kept_and_the_first_non_empty_line_is_the_one(self):
        self.body = "\n  \nMERGE: captain\nrest"
        self.clear()
        self.assertEqual(self.body, "\n  \nMERGE: automatic\nrest")

    def test_s4_one_audit_line_holds_the_head_and_both_versions_of_the_line(self):
        self.clear()
        (line,) = self.events("gate.clear_marker")
        self.assertEqual(line["detail"], {"head": self.HEAD, "before": "MERGE: captain", "after": "MERGE: automatic"})

    def test_s5_a_body_that_cannot_be_read_or_written_or_changed_meanwhile_leaves_the_clear_standing_and_says_so(self):
        for label, arrange in (("read", lambda: setattr(self, "read_fails", True)),
                               ("write", lambda: setattr(self, "write_fails", True)),
                               ("changed", lambda: setattr(self, "change_between", "MERGE: captain\nan edit by someone\n"))):
            self.setUp()
            arrange()
            if label == "read":
                self.read_fails = False   # the clear itself reads the PR; only the body edit's reads fail
                real = sb.read_pr_json
                count = {"n": 0}

                def flaky(*a, **k):
                    count["n"] += 1
                    if count["n"] > 1:
                        raise sb.NoticeError("gh pr view failed: HTTP 502")
                    return real(*a, **k)
                sb.read_pr_json = flaky
                self.addCleanup(setattr, sb, "read_pr_json", real)
            lines = self.clear()
            self.assertTrue(any("marker line remains" in l for l in lines), (label, lines))
            self.assertEqual(self.writes, [], label)
            self.assertNotIn(self.names["hold-impact"], self.labels, "the clear stands: " + label)
            self.assertEqual(len(self.events("gate.clear_marker_failed")), 1, label)
            self.assertEqual(self.events("gate.clear_marker"), [])
            if label == "read":
                sb.read_pr_json = real

    def test_s6_no_clear_no_edit_and_a_head_that_is_not_the_cleared_one_is_refused_before_any_write(self):
        self.assertEqual(self.writes, [])
        report = {"pr": self.PR, "head": self.HEAD, "reasons": ["a floor path"], "floor_paths": ["x"], "globs": ["x"]}
        sb.apply_impact_hold(self.engine, self.REPO, 7, report, {"labels": []})
        self.assertEqual(self.writes, [], "the daemon's hold pass never edits a body")
        with self.assertRaises(sb.NoticeError):
            self.clear(head="b" * 40, remove=[self.names["hold-impact"]])
        self.assertEqual((self.writes, self.body), ([], self.BODY))
        self.assertEqual(sb.neutralise_marker(self.engine, self.route, self.REPO, 7, "c" * 40), [], "a head the owner did not clear is not touched")
        self.assertEqual(self.writes, [])

    def test_a_failing_audit_after_the_write_never_fails_the_clear_and_says_what_is_true(self):
        real = self.engine.audit.append

        def append(actor, event, *rest, **kw):
            if event == "gate.clear_marker":
                raise OSError("disk full")
            return real(actor, event, *rest, **kw)
        self.engine.audit.append = append
        lines = self.clear()
        self.assertTrue(self.body.startswith("MERGE: automatic"))
        self.assertNotIn(self.names["hold-impact"], self.labels)
        self.assertEqual(self.events("gate.clear_marker"), [])
        self.assertFalse(any("marker line remains" in l for l in lines), lines)
        self.assertTrue(any("audit line of that change could not be written" in l for l in lines), lines)

    def test_the_text_of_the_line_is_printed_as_one_line_without_control_characters(self):
        self.body = "MERGE: captain\x1b[31m red\x07 bell\nrest\n"
        lines = self.clear()
        self.assertFalse(any(re.search(r"[\x00-\x08\x0b-\x1f\x7f]", l) for l in lines), lines)
        self.assertTrue(self.body.startswith("MERGE: automatic\x1b[31m red"), "the body keeps its bytes: only the printed text is cleaned")

    def test_a_head_that_moves_between_the_two_reads_and_a_write_that_does_not_stick_are_both_said(self):
        real = sb.read_pr_json
        count = {"n": 0}

        def moving(*a, **k):
            count["n"] += 1
            raw = real(*a, **k)
            if count["n"] == 2:
                raw = dict(raw, headRefOid="d" * 40)
            return raw
        sb.read_pr_json = moving
        self.addCleanup(setattr, sb, "read_pr_json", real)
        lines = self.clear()
        self.assertTrue(any("marker line remains" in l for l in lines), lines)
        self.assertEqual(self.writes, [])
        sb.read_pr_json = real
        self.setUp()
        route = self.route

        def drops(argv, **kwargs):
            if argv[1] == "api" and "PATCH" in argv:
                return subprocess.CompletedProcess(argv, 0, "{}", "")   # said fine, never stored
            return route(argv, **kwargs)
        self.engine.gh_runner = drops
        lines = sb.impact_clear_core(self.engine, drops, self.REPO, 7, "owner", "a false hold: it is a fixture")
        self.assertTrue(any("marker line remains" in l and "read back" in l for l in lines), lines)
        self.assertEqual(self.body, self.BODY)

    def test_the_marker_edit_is_pure_and_follows_the_gates_own_reading(self):
        for body in ("MERGE: captain", "  MERGE: captain: pending\nx", "\n\nMERGE: captain\r\nMERGE: captain\r\n"):
            new, before, after = sb.marker_edit(body)
            self.assertEqual(sb.pr_marker(new), "automatic", body)
            self.assertEqual(len(new.splitlines()), len(body.splitlines()))
            self.assertEqual(after, before.replace("MERGE: captain", "MERGE: automatic"))
            self.assertEqual(new.replace("MERGE: automatic", "MERGE: captain", 1), body)
        self.assertIsNone(sb.marker_edit(None))
        self.assertFalse(sb.pr_marker_state([], sb.marker_edit("MERGE: captain\n")[0], now=0)["captain"])


class TestClearCarryInTheDaemon(Base):
    """#510: the clear stores the fingerprint, the daemon's hold skips a head the clear carries to (only with `impact_hold.carry_clear`), audits it once, shows it in the gate row and
    the owner can revoke it. A real git repository stands behind a fake GitHub; a fake gh records every write."""

    REPO, PR = "acme-org/widget-api", "acme-org/widget-api#7"

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = build_carry_repo(Path(self.tmp.name) / "repo")
        self.writes, self.api_gets = [], 0
        self.labels = []
        self.build(carry=True)

    def build(self, carry):
        impact = {"enabled": True, "carry_clear": carry} if carry is not None else {"enabled": True}
        self.world = World(self, agents=(), config={"gh": FAKE_GH, "impact_hold": impact})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.engine = self.world.new_engine("daemon")
        self.engine.gh_runner, self.engine.state_dir = self.route, self.world.state
        self.names = sb.label_names(self.engine.config)
        state = Path(self.world.state) / sb.IMPACT_STATE_FILE
        if state.exists():
            state.unlink()

    def route(self, argv, **kwargs):
        if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
            raw = {"state": "OPEN", "headRefOid": self.api.rev("feature"), "title": "t", "labels": [{"name": n} for n in self.labels], "body": getattr(self, "body", ""), "author": {"login": getattr(self, "author_login", "bot")}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
        if argv[0] == FAKE_GH and argv[1] == "api" and "--method" in argv:
            method = argv[argv.index("--method") + 1]
            if method == "PATCH" and argv[2].endswith("/pulls/7"):
                self.body = json.loads(kwargs["input"])["body"]
            if method == "GET" and "/labels/" in argv[2]:
                return subprocess.CompletedProcess(argv, 0, "{}", "")           # a label that already exists
            if method == "GET":
                self.api_gets += 1
                try:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(self.api.get(argv[2])), "")
                except RuntimeError as err:
                    return subprocess.CompletedProcess(argv, 1, "", str(err))
            self.writes.append((method, argv[2], json.loads(kwargs["input"]) if kwargs.get("input") else None))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return self.world.route(argv, **kwargs)

    def events(self, name):
        return [e for e in self.world.audit() if e["event"] == name]

    def adds(self):
        return [w for w in self.writes if w[0] == "POST" and w[1].endswith("/labels")]

    def clear(self):
        self.labels = [self.names["merge-captain"], self.names["hold-impact"]]
        sb.impact_clear_core(self.engine, self.route, self.REPO, 7, "owner", "a false hold: it is a fixture")
        self.labels = []
        return self.api.rev("feature")

    def merge_main(self, name="m1.txt"):
        g = self.api.git
        g("checkout", "-q", "main")
        (Path(self.api.dir) / name).write_text("from main\n")
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-edit", "main")
        return self.api.rev("feature")

    def hold(self, head, labels=()):
        report = {"pr": self.PR, "head": head, "reasons": ["a floor path"], "floor_paths": ["x"], "globs": ["x"]}
        return sb.apply_impact_hold(self.engine, self.REPO, 7, report, {"labels": [{"name": l} for l in labels]})

    def test_s7_a_carried_clear_never_rewrites_the_body_and_the_neutralised_line_stays_neutral(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.assertEqual(self.body, "MERGE: automatic\n\nthe change\n")
        before = self.body
        self.writes.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new), 0)
        self.assertEqual(len(self.events("gate.clear_carried")), 1)
        self.assertEqual(([w for w in self.writes if w[1].endswith("/pulls/7")], self.body), ([], before))
        self.assertFalse(sb.pr_marker_state([], self.body, now=0)["captain"], "the gate reads no hold from the text")

    def test_891_a_carried_clear_touches_no_hold_label_and_a_re_hold_never_writes_the_owners_own_hold(self):
        owner_hold = self.names[sb.OWNER_HOLD_KEY]
        self.clear()
        self.writes.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new, labels=(owner_hold,)), 0)
        self.assertEqual((len(self.events("gate.clear_carried")), self.writes), (1, []), "carried: no label is added, adopted or removed, the owner's own hold included")
        self.writes.clear()
        changed = self.api.rev("feature")
        self.assertEqual(self.hold(changed, labels=(owner_hold,)), 0)   # the same head again: still nothing
        self.assertEqual([w for w in self.adds() if owner_hold in json.dumps(w)], [], "the owner's own hold is never written by the daemon")

    def test_830_a_pure_merge_carry_is_not_held_again_and_a_changed_pull_request_is(self):
        self.clear()
        new = self.merge_main()
        report = {"pr": self.PR, "head": new, "reasons": [], "floor_paths": [], "globs": []}
        self.assertTrue(sb._impact_rehold_now(self.engine, self.REPO, 7, self.PR, report, {"labels": []}))
        self.assertEqual(self.adds(), [], "the pure merge carries: no hold written")
        state = sb.read_impact_state(self.world.state)[0]
        self.assertEqual(state["carried"][self.PR]["head"], new)
        self.assertIsNone(sb.rehold_reason(self.engine, state, self.REPO, self.PR, new))
        self.build(carry=False)
        self.clear()
        new = self.merge_main("m2.txt")
        self.writes.clear()
        self.assertTrue(sb._impact_rehold_now(self.engine, self.REPO, 7, self.PR, dict(report, head=new), {"labels": []}))
        self.assertEqual(len(self.adds()), 1, "with the carry off every new head is held again")

    def test_s4_a_carried_clear_tells_the_chief_of_staff_once_with_both_heads(self):
        cleared = self.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new), 0)
        told = [m.group(1) for a in self.world.admissions("cos") for m in [re.search(r"(owner's clear carried [^\n]*)", a["text"])] if m]
        self.assertEqual(told, ["owner's clear carried %s from %s to %s; lane unknown" % (self.PR, cleared[:8], new[:8])])
        for _ in range(3):
            self.hold(new)
        self.assertEqual(len([a for a in self.world.admissions("cos") if "owner's clear carried" in a["text"]]), 1, "a second pass sends nothing more")
        (event,) = self.events("gate.clear_notified")[-1:]
        self.assertEqual(event["detail"]["kind"], "carry")

    def test_a_send_that_blocks_does_not_delay_or_alter_the_carry(self):
        self.clear()
        new = self.merge_main()
        release = threading.Event()
        self.addCleanup(release.set)
        real = self.engine.send
        self.engine.send = lambda *a, **k: (release.wait(1.5), real(*a, **k))[1]
        spent, real_notify = [], sb.notify_clear

        def timed(*a, **k):
            began = time.monotonic()
            try:
                return real_notify(*a, **k)
            finally:
                spent.append(time.monotonic() - began)
        with mock.patch.object(sb, "NOTIFY_CLEAR_DEADLINE_SECONDS", 0.2), mock.patch.object(sb, "notify_clear", timed):
            self.assertEqual(self.hold(new), 0)
        self.assertEqual(len(spent), 1)
        self.assertLess(spent[0], 1.0, "the carry waited for the blocked send no longer than the deadline")
        self.assertEqual(sb.read_impact_state(self.world.state)[0]["carried"][self.PR]["head"], new, "the carry is recorded")
        self.assertEqual(self.adds(), [], "no hold written")
        self.assertTrue([e for e in self.events("gate.clear_notify_failed") if "timed out" in e["detail"].get("error", "")])
        release.set()

    def test_a_carry_that_is_not_recorded_for_the_head_sends_no_notice(self):
        self.clear()
        new = self.merge_main()
        carry = {"carried": True, "from": "f" * 40, "fingerprint": "sha256:x", "reason": None, "to": new}   # a verdict for a clear that is not the recorded one
        calls = []
        real = sb.notify_clear
        sb.notify_clear = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, sb, "notify_clear", real)
        with mock.patch.object(sb, "impact_carry_decision", return_value=carry):
            self.hold(new)
        self.assertEqual(calls, [], "the carry was not recorded for this head: nothing is said")

    def test_the_clear_stores_the_fingerprint_with_the_head_only_when_the_switch_is_on(self):
        cleared = self.clear()
        entry = sb.read_impact_state(self.world.state)[0]["clears"][self.PR]
        self.assertEqual(entry["head"], cleared)
        self.assertTrue(entry["fingerprint"].startswith("sha256:"))
        (event,) = self.events("gate.hold_cleared")
        self.assertEqual(event["detail"]["fingerprint"], entry["fingerprint"])
        self.build(carry=None)   # the default: off
        gets = self.api_gets
        self.clear()
        self.assertNotIn("fingerprint", sb.read_impact_state(self.world.state)[0]["clears"][self.PR])
        self.assertEqual(self.api_gets, gets, "off: the clear reads nothing new from GitHub")

    def test_s1_s7_a_pure_merge_of_main_carries_no_hold_is_written_it_is_audited_once_and_the_gate_row_says_carried(self):
        cleared = self.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new), 0)
        self.assertEqual(self.adds(), [], "no hold written for the carried head")
        (carried,) = self.events("gate.clear_carried")
        fingerprint = sb.read_impact_state(self.world.state)[0]["clears"][self.PR]["fingerprint"]
        self.assertEqual((carried["detail"]["head"], carried["detail"]["from"], carried["detail"]["fingerprint"]), (new, cleared, fingerprint))
        gets = self.api_gets
        for _ in range(3):
            self.assertEqual(self.hold(new), 0)
        self.assertEqual((len(self.events("gate.clear_carried")), self.api_gets), (1, gets), "decided once per new head: one audit line and no repeated reads")
        report = {"gates": [{"gate": "G6", "status": "ok", "detail": "not a held PR"}], "head": new}
        sb.gates_carried_note(report, types.SimpleNamespace(state=self.world.state, pr=self.PR, config=self.engine.config))
        self.assertIn("carried to this head", report["gates"][0]["detail"])
        self.assertIn(cleared[:8], report["gates"][0]["detail"])
        self.assertEqual(report["carried"]["from"], cleared)
        other = {"gates": [{"gate": "G6", "status": "ok", "detail": "not a held PR"}], "head": "b" * 40}
        sb.gates_carried_note(other, types.SimpleNamespace(state=self.world.state, pr=self.PR, config=self.engine.config))
        self.assertEqual(other["gates"][0]["detail"], "not a held PR", "only the carried head says carried")

    def test_a_commit_of_its_own_voids_the_clear_the_hold_is_written_and_the_void_is_audited_once_with_the_reason(self):
        self.clear()
        g = self.api.git
        (Path(self.api.dir) / "d.txt").write_text("own\n")
        g("add", "-A")
        g("commit", "-q", "-m", "own commit")
        new = self.api.rev("feature")
        self.assertGreater(self.hold(new), 0)
        self.assertEqual(len(self.adds()), 1, "held as today")
        (void,) = self.events("gate.clear_carry_void")
        self.assertIn("commit of the pull request's own", void["detail"]["reason"])
        gets = self.api_gets
        self.hold(new, labels=[self.names["merge-captain"], self.names["hold-impact"]])
        self.assertEqual((len(self.events("gate.clear_carry_void")), self.api_gets), (1, gets), "once per head")
        self.assertEqual(sb.read_impact_state(self.world.state)[0].get("carried", {}), {})

    def test_s9_off_by_default_the_merge_of_main_is_held_as_today_and_a_clear_without_a_fingerprint_never_carries(self):
        self.build(carry=None)
        self.clear()
        new = self.merge_main()
        self.assertGreater(self.hold(new), 0)
        self.assertEqual((len(self.adds()), self.events("gate.clear_carried"), self.events("gate.clear_carry_void")), (1, [], []))
        self.build(carry=True)
        self.writes.clear()
        entry = {"head": "c" * 40, "reason": "an old clear with no fingerprint", "by": "owner", "at": 1}
        sb.write_impact_state(self.world.state, {"clears": {self.PR: entry}, "applied": {}})
        self.assertGreater(self.hold(self.api.rev("feature")), 0)
        (void,) = self.events("gate.clear_carry_void")[-1:]
        self.assertEqual(void["detail"]["reason"], "no fingerprint was recorded with the clear")
        for value in ("true", 1, None):
            self.assertFalse(sb.impact_floor.carry_enabled({"impact_hold": {"enabled": True, "carry_clear": value}}, self.REPO), repr(value))
        self.assertFalse(sb.impact_floor.carry_enabled({"impact_hold": {"enabled": False, "carry_clear": True}}, self.REPO), "no carry without an enforced hold")
        with self.assertRaises(sb.ConfigError):
            sb.validate_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "impact_hold": {"enabled": True, "carry_clear": "yes"}})

    def test_an_unreadable_state_file_is_said_in_the_gate_report_not_swallowed(self):
        real = sb.read_impact_state

        def broken(*args, **kwargs):
            raise OSError("disk gone")
        sb.read_impact_state = broken
        self.addCleanup(setattr, sb, "read_impact_state", real)
        report = {"gates": [{"gate": "G6", "status": "ok", "detail": "not a held PR"}], "head": "a" * 40}
        sb.gates_carried_note(report, types.SimpleNamespace(state=self.world.state, pr=self.PR, config=self.engine.config))
        self.assertIn("OSError", report["carried_error"])
        self.assertEqual(report["gates"][0]["detail"], "not a held PR")

    def test_u2_the_owner_removes_a_carried_clear_the_pr_is_held_again_and_the_carry_does_not_return(self):
        self.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new), 0)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "impact_hold": {"enabled": True, "carry_clear": True}})
        with mock.patch.object(sb, "gates_caller_kind", return_value=("person", "owner")):
            code = sb.cmd_impact_clear(types.SimpleNamespace(pr=self.PR, reason="take the clear back, it was a mistake", revoke=True), self.route, self.world.clock.now)
        self.assertEqual(code, 0)
        state = sb.read_impact_state(self.world.state)[0]
        self.assertEqual((state["clears"], state.get("carried", {})), ({}, {}))
        (revoked,) = self.events("gate.clear_revoked")
        self.assertEqual(revoked["detail"]["carried_head"], new)
        self.assertGreater(self.hold(new), 0, "held again at the next pass")
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual(len(self.events("gate.clear_carried")), 1, "and it does not carry again for that head")
        with mock.patch.object(sb, "gates_caller_kind", return_value=("agent", "cos", "chief-of-staff")):
            code = sb.cmd_impact_clear(types.SimpleNamespace(pr=self.PR, reason="an agent may not take it back", revoke=True), self.route, self.world.clock.now)
        self.assertNotEqual(code, 0)

    def test_a_recorded_carry_stops_counting_when_the_owner_switches_the_carry_off_and_a_state_file_that_never_carried_reads_as_before(self):
        self.clear()
        new = self.merge_main()
        self.assertEqual(self.hold(new), 0)
        self.assertEqual(sb.read_impact_state(self.world.state)[0]["carried"][self.PR]["head"], new)
        self.build(carry=None)   # the owner turns the switch off (same state directory, state file kept)
        sb.write_impact_state(self.world.state, {"clears": {self.PR: {"head": "c" * 40, "reason": "x" * 12, "by": "owner", "at": 1}}, "applied": {},
                                                 "carried": {self.PR: {"head": new, "from": "c" * 40, "fingerprint": "sha256:" + "0" * 64, "at": 2}}})
        self.assertGreater(self.hold(new), 0, "a carry on file does not count once the switch is off")
        plain = Path(self.world.state) / sb.IMPACT_STATE_FILE
        plain.write_text(json.dumps({"clears": {self.PR: {"head": "d" * 40}}, "applied": {}}))
        self.assertEqual(sb.read_impact_state(self.world.state)[0], {"clears": {self.PR: {"head": "d" * 40}}, "applied": {}}, "no carried or carry_void key appears out of nowhere")
        with mock.patch.object(sb, "gates_caller_kind", return_value=("person", "owner")):
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "impact_hold": {"enabled": True}})
            before = plain.read_text()
            code = sb.cmd_impact_clear(types.SimpleNamespace(pr="acme-org/other#9", reason="there is nothing to take back here", revoke=True), self.route, self.world.clock.now)
        self.assertEqual((code, plain.read_text()), (0, before), "a revoke with nothing to take back writes nothing")

    def test_the_gates_command_calls_the_carried_note_and_a_state_error_is_said_not_swallowed(self):
        import ast
        main = switchboard_source()
        fn = next(n for n in ast.walk(ast.parse(main)) if isinstance(n, ast.FunctionDef) and n.name == "cmd_gates")
        self.assertIn("gates_carried_note(report, ctx)", ast.get_source_segment(main, fn))
        report = {"gates": [{"gate": "G6", "status": "ok", "detail": "not a held PR"}], "head": "a" * 40}
        (Path(self.world.state) / sb.IMPACT_STATE_FILE).write_text(json.dumps({"clears": {}, "applied": {}, "carried": {self.PR: "not an object"}}))
        sb.gates_carried_note(report, types.SimpleNamespace(state=self.world.state, pr=self.PR, config=self.engine.config))
        self.assertEqual(report["gates"][0]["detail"], "not a held PR")

    def test_a_new_clear_after_a_carry_starts_again_from_its_own_head(self):
        self.clear()
        carried = self.merge_main()
        self.hold(carried)
        again = self.clear()
        self.assertEqual(again, carried)
        entry = sb.read_impact_state(self.world.state)[0]
        self.assertEqual(entry["clears"][self.PR]["head"], carried)
        self.assertEqual(entry.get("carried", {}), {}, "the earlier carry is not part of the new clear")


class TestConflictCarryInTheDaemon(Base):
    """#815 in the daemon: the carry reads the ledger for the reviewer's confirmation, waits (and holds) until there is one, audits one line naming both heads, and lifts what it held."""

    REPO, PR = TestClearCarryInTheDaemon.REPO, TestClearCarryInTheDaemon.PR
    RESOLVED = TestConflictCarry.RESOLVED
    route, events, adds, clear, hold = (TestClearCarryInTheDaemon.__dict__[n] for n in ("route", "events", "adds", "clear", "hold"))
    write, conflict_merge = TestConflictCarry.__dict__["write"], TestConflictCarry.__dict__["conflict_merge"]

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "repo"
        self.api = build_carry_repo(self.dir)
        self.writes, self.api_gets, self.labels = [], 0, []
        self.base_main = self.api.rev("main")
        self.build(carry=True)

    def build(self, carry):
        impact = {"enabled": True, "carry_clear": carry} if carry is not None else {"enabled": True}
        self.world = World(self, agents=(), config={"gh": FAKE_GH, "impact_hold": impact, "plan_evaluators": {"reviewer": ["rev1"]}})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.engine = self.world.new_engine("daemon")
        self.engine.gh_runner, self.engine.state_dir = self.route, self.world.state
        self.names = sb.label_names(self.engine.config)

    def confirm(self, head, by="rev1", reviewer="code", verdict="approve", flag=True, pr=None, by_role="reviewer"):
        entry = {"kind": "review_record", "pr": pr or self.PR, "head": head, "reviewer": reviewer, "verdict": verdict, "blocks": 0 if verdict != "request-changes" else 1, "notes": 0, "by": by, "summary": "", "by_role": by_role}
        if flag:
            entry["carry_confirmed"] = True
        return sb.plan_ledger.append_entry(self.world.state, entry, self.world.clock.now())

    def held_state(self):
        return sb.read_impact_state(self.world.state)[0]

    def test_s1_s6_u1_a_confirmed_conflict_resolution_carries_audits_one_line_with_both_heads_and_writes_no_clear(self):
        cleared = self.clear()
        before = json.dumps(self.held_state()["clears"], sort_keys=True)
        new = self.conflict_merge()
        stored = self.confirm(new)
        self.assertEqual(self.hold(new), 0)
        self.assertEqual(self.adds(), [], "no hold written")
        (carried,) = self.events("gate.clear_carried")
        detail = carried["detail"]
        self.assertEqual((detail["head"], detail["from"], detail["route"]), (new, cleared, "lines"))
        self.assertRegex(detail["head"], r"^[0-9a-f]{40}$")
        self.assertRegex(detail["from"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(detail["head"], detail["from"])
        clears = self.held_state()["clears"]
        self.assertEqual((detail["fingerprint"], detail["lines_fingerprint"]), (clears[self.PR]["fingerprint"], clears[self.PR]["lines_fingerprint"]))
        self.assertIn("by rev1", detail["confirmation"])
        self.assertIn(str(stored["seq"]), detail["confirmation"])
        self.assertEqual(json.dumps(clears, sort_keys=True), before, "the owner's clear record is byte-identical: only the carried record is new")
        self.assertEqual(clears[self.PR]["head"], cleared, "the clear is untouched: still the owner's, at its own head")
        self.assertEqual(self.held_state()["carried"][self.PR]["route"], "lines")
        gets = self.api_gets
        for _ in range(3):
            self.assertEqual(self.hold(new), 0)
        self.assertEqual((len(self.events("gate.clear_carried")), self.api_gets), (1, gets), "a second pass writes and reads nothing")
        self.assertEqual(sb.rehold_reason(self.engine, self.held_state(), self.REPO, self.PR, new), None, "#830's one reading of 'held again' agrees: carried")

    def test_s4_a_missing_stale_forged_or_self_made_confirmation_never_carries_and_the_pull_request_is_held_meanwhile(self):
        cleared = self.clear()
        cases = (("absent", lambda new: None),
                 ("recorded for the cleared head", lambda new: self.confirm(cleared)),
                 ("recorded for another head", lambda new: self.confirm("a" * 40)),
                 ("recorded for another pull request", lambda new: self.confirm(new, pr="acme-org/widget-api#8")),
                 ("by an unregistered name", lambda new: self.confirm(new, by="mallory")),
                 ("by the person's own terminal", lambda new: self.confirm(new, by="person")),
                 ("by an agent whose recorded role is not the reviewer's", lambda new: self.confirm(new, by_role="coder")),
                 ("with no recorded role (a record made before roles were kept)", lambda new: self.confirm(new, by_role=None)),
                 ("without the confirmation flag", lambda new: self.confirm(new, flag=False)),
                 ("with a request-changes verdict", lambda new: self.confirm(new, verdict="request-changes")),
                 ("by the writer of the pull request", lambda new: (sb.plan_ledger.append_entry(self.world.state, {"kind": "proof", "pr": self.PR, "scenario": "S1", "result": "pass", "by": "rev1", "head": new, "plan_ref": "x"}, self.world.clock.now()), self.confirm(new))),
                 ("by a sender of its notice", lambda new: (sb.record_pr_notice(self.world.state, self.REPO, 7, "rev1", "n", self.world.clock.now()), self.confirm(new))),
                 ("a comment or a label only", lambda new: setattr(self, "body", "carry-confirmed: yes\nMERGE: captain")))
        for label, make in cases:
            with self.subTest(label):
                g = self.api.git
                g("checkout", "-q", "-B", "feature", cleared)
                g("branch", "-q", "-f", "main", self.base_main)
                self.clear_again(cleared)
                self.writes.clear()
                self.labels = []
                new = self.conflict_merge()
                make(new)
                self.assertGreater(self.hold(new), 0, label)
                self.assertEqual(len(self.adds()), 1, label)
                self.assertEqual(self.events("gate.clear_carried"), [], label)
                state = self.held_state()
                self.assertNotIn(self.PR, state.get("carried", {}), label)
                self.assertNotIn(self.PR, state.get("carry_void", {}), "a missing confirmation is a wait, not a void: %s" % label)
                self.assertEqual(state["carry_wait"][self.PR]["head"], new, label)

    def test_s4b_a_confirmation_the_same_writer_took_back_or_one_by_the_reported_author_or_with_an_unreadable_ledger_never_carries(self):
        cleared = self.clear()
        for label in ("superseded by the same writer, without the flag", "superseded by a request-changes", "the author GitHub reports", "an unreadable ledger"):
            with self.subTest(label):
                g = self.api.git
                g("checkout", "-q", "-B", "feature", cleared)
                g("branch", "-q", "-f", "main", self.base_main)
                self.clear_again(cleared)
                self.writes.clear()
                self.labels = []
                self.author_login = "bot"
                new = self.conflict_merge()
                if label.startswith("superseded by the same"):
                    self.confirm(new)
                    self.confirm(new, flag=False)
                elif label.startswith("superseded by a request"):
                    self.confirm(new)
                    self.confirm(new, verdict="request-changes")
                elif label.startswith("the author"):
                    self.author_login = "rev1"
                    self.confirm(new)
                else:
                    self.confirm(new)
                    with open(str(Path(self.world.state) / "plan-ledger.jsonl"), "a") as handle:
                        handle.write("not json\n")
                self.assertGreater(self.hold(new), 0, label)
                self.assertEqual(self.events("gate.clear_carried"), [], label)
                self.assertNotIn(self.PR, self.held_state().get("carried", {}), label)
            self.author_login = "bot"
        g = self.api.git
        g("checkout", "-q", "-B", "feature", cleared)
        g("branch", "-q", "-f", "main", self.base_main)
        for name in ("plan-ledger.jsonl", "plan-ledger.head"):
            (Path(self.world.state) / name).unlink(missing_ok=True)
        self.clear_again(cleared)
        new = self.conflict_merge()
        self.confirm(new)
        self.assertEqual(self.hold(new), 0, "control: with none of those, the same merge carries")

    def test_s4c_an_unreadable_score_block_refuses_every_confirmation(self):
        cleared = self.clear()
        module = sb.load_module("delegated_clear")
        for label, patch in (("none", mock.patch.object(module, "score_block", return_value=None)), ("raises", mock.patch.object(module, "score_block", side_effect=OSError("gone")))):
            with self.subTest(label):
                g = self.api.git
                g("checkout", "-q", "-B", "feature", cleared)
                g("branch", "-q", "-f", "main", self.base_main)
                self.clear_again(cleared)
                self.writes.clear()
                self.labels = []
                new = self.conflict_merge()
                self.confirm(new)
                with patch:
                    self.assertGreater(self.hold(new), 0, label)
                self.assertEqual(self.events("gate.clear_carried"), [], label)
                self.assertIn(self.PR, self.held_state()["carry_wait"], "a missing role list is a wait, not a carry: %s" % label)

    def clear_again(self, cleared):
        """the next case starts from the same owner clear: rebuild its state"""
        self.build(carry=True)
        g = self.api.git
        g("checkout", "-q", "-B", "feature", cleared)
        self.clear()

    def test_s5_a_push_after_the_confirmation_needs_a_new_one_and_a_late_one_carries_and_lifts_the_hold(self):
        cleared = self.clear()
        self.world.clock.t += 60
        b = self.conflict_merge()
        self.confirm(b)
        self.assertEqual(self.hold(b), 0)
        self.assertEqual(len(self.events("gate.clear_carried")), 1)
        g = self.api.git
        g("checkout", "-q", "main")
        (Path(self.dir) / "m9.txt").write_text("more main\n")
        g("add", "-A")
        g("commit", "-q", "-m", "main moves again")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-edit", "main")
        c = self.api.rev("feature")
        self.assertGreater(self.hold(c), 0, "no confirmation at the new head: held")
        self.assertEqual(len(self.events("gate.clear_carried")), 1, "the confirmation for B does not carry C")
        added = [l for w in self.adds() for l in (w[2] or {}).get("labels", [])]
        self.assertTrue(added)
        self.labels = list(added)
        self.writes.clear()
        gets = self.api_gets
        self.hold(c)
        self.assertEqual(self.api_gets, gets, "still waiting and nothing new in the ledger: no read of GitHub")
        self.confirm(c)
        self.assertEqual(self.hold(c), 0)
        self.assertEqual(len(self.events("gate.clear_carried")), 2)
        removed = [w for w in self.writes if w[0] == "DELETE"]
        self.assertTrue(removed, "the labels this daemon put on the head while the confirmation was missing are lifted")
        self.assertNotIn(self.PR, self.held_state()["applied"])
        self.assertEqual(self.held_state()["carried"][self.PR]["head"], c)

    def test_s7_a_credential_or_identity_path_change_is_carried_like_any_other(self):
        for path in ("shims/launcher.sh", "cloud/launcher/repo-approvals.json", "bin/switchboard-trial"):
            with self.subTest(path):
                self.build(carry=True)
                g = self.api.git
                g("checkout", "-q", "-B", "feature", "main")
                g("reset", "-q", "--hard", "main")
                shutil.rmtree(str(self.dir))
                self.api = build_carry_repo(self.dir)
                self.write(path, "own change\n")
                self.api.git("add", "-A")
                self.api.git("commit", "-q", "-m", "own change in a sensitive path")
                cleared = self.clear()
                new = self.conflict_merge()
                self.confirm(new)
                self.assertEqual(self.hold(new), 0, path)
                self.assertEqual(self.held_state()["carried"][self.PR]["from"], cleared)

    def test_a_new_clear_and_a_revoke_both_forget_a_waiting_carry(self):
        self.clear()
        new = self.conflict_merge()
        self.assertGreater(self.hold(new), 0)
        self.assertIn(self.PR, self.held_state()["carry_wait"])
        self.labels = [self.names["merge-captain"], self.names["hold-impact"]]
        sb.impact_clear_core(self.engine, self.route, self.REPO, 7, "owner", "cleared again at the new head: a fixture")
        self.assertNotIn(self.PR, self.held_state().get("carry_wait", {}), "a new clear")
        self.assertEqual(self.held_state()["clears"][self.PR]["head"], new)
        state = self.held_state()
        state["carry_wait"] = {self.PR: {"head": "c" * 40, "marks": 0, "reason": "waiting again, for the new clear"}}
        sb.write_impact_state(self.world.state, state)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "impact_hold": {"enabled": True, "carry_clear": True}})
        with mock.patch.object(sb, "gates_caller_kind", return_value=("person", "owner")):
            self.assertEqual(sb.cmd_impact_clear(types.SimpleNamespace(pr=self.PR, reason="take the clear back, it was a mistake", revoke=True), self.route, self.world.clock.now), 0)
        self.assertNotIn(self.PR, self.held_state().get("carry_wait", {}), "a revoke")

    def test_s8_with_the_switch_off_nothing_new_is_stored_and_no_conflict_carry_is_made(self):
        self.build(carry=None)
        gets = self.api_gets
        cleared = self.clear()
        entry = self.held_state()["clears"][self.PR]
        self.assertEqual(sorted(entry), ["at", "by", "head", "reason", "removed"], "the clear stores no fingerprint of either form: it is as it was before the carry existed")
        self.assertEqual(self.api_gets, gets)
        new = self.conflict_merge()
        self.confirm(new)
        self.assertGreater(self.hold(new), 0)
        self.assertEqual((self.events("gate.clear_carried"), self.events("gate.clear_carry_void")), ([], []))
        self.assertEqual(self.held_state().get("carried", {}), {})

    def test_the_clear_stores_the_lines_fingerprint_next_to_the_full_one_and_the_pure_merge_still_carries(self):
        cleared = self.clear()
        entry = self.held_state()["clears"][self.PR]
        self.assertTrue(entry["lines_fingerprint"].startswith("sha256:"))
        self.assertNotEqual(entry["lines_fingerprint"], entry["fingerprint"])
        g = self.api.git
        g("checkout", "-q", "main")
        (Path(self.dir) / "m1.txt").write_text("from main\n")
        g("add", "-A")
        g("commit", "-q", "-m", "main moves")
        g("checkout", "-q", "feature")
        g("merge", "-q", "--no-edit", "main")
        new = self.api.rev("feature")
        self.assertEqual(self.hold(new), 0)
        (carried,) = self.events("gate.clear_carried")
        self.assertNotIn("route", carried["detail"], "the pure carry's audit line reads as before")

    # -- clause (7): a re-hold puts the body's first line back to `MERGE: captain`

    def own_commit(self):
        g = self.api.git
        (Path(self.dir) / "d.txt").write_text("own\n")
        g("add", "-A")
        g("commit", "-q", "-m", "own commit")
        return self.api.rev("feature")

    def rehold(self, head):
        report = {"pr": self.PR, "head": head, "reasons": ["held again"], "floor_paths": [], "globs": [], "rehold": "held again"}
        return sb.apply_impact_hold(self.engine, self.REPO, 7, report, {"labels": []})

    def body_patches(self):
        return [w for w in self.writes if w[0] == "PATCH" and w[1].endswith("/pulls/7")]

    def test_s9_a_re_hold_rewrites_the_first_line_to_captain_once_per_head_and_audits_it_in_the_reapplied_line(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.assertEqual(self.body, "MERGE: automatic\n\nthe change\n")
        self.writes.clear()
        new = self.own_commit()
        self.assertGreater(self.rehold(new), 0)
        self.assertEqual(self.body, "MERGE: captain\n\nthe change\n", "only the marker changed")
        self.assertEqual(len(self.body_patches()), 1)
        (done,) = self.events("gate.hold_reapplied")
        self.assertEqual(done["detail"]["marker"], {"before": "MERGE: automatic", "after": "MERGE: captain"})
        self.labels = [l for w in self.adds() for l in w[2]["labels"]]
        for _ in range(3):
            self.rehold(new)
        self.assertEqual((len(self.body_patches()), len(self.events("gate.hold_reapplied"))), (1, 1), "once per head")
        self.assertTrue(sb.impact_rehold_open(self.world.state, self.REPO, 7))

    def test_s10_a_carried_clear_never_rewrites_the_body_and_marks_no_re_hold(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.writes.clear()
        new = self.conflict_merge()
        self.confirm(new)
        self.assertEqual(self.hold(new), 0)
        self.assertEqual((self.body_patches(), self.body), ([], "MERGE: automatic\n\nthe change\n"))
        self.assertFalse(sb.impact_rehold_open(self.world.state, self.REPO, 7))

    def test_s11_a_body_with_no_merge_line_or_one_that_already_says_captain_is_left_alone(self):
        for label, body in (("no MERGE line", "just text\nMERGE: automatic\n"), ("already captain", "MERGE: captain\nx\n"), ("empty", ""), ("a MERGE word inside", "see MERGE: automatic below\n")):
            with self.subTest(label):
                self.build(carry=True)
                g = self.api.git
                g("checkout", "-q", "-B", "feature", "main")
                self.writes.clear()
                self.labels = []
                self.body = body
                self.clear()
                self.writes.clear()
                self.labels = []
                self.body = body   # whatever the clear left, the body now reads as the case says
                before = self.body
                (Path(self.dir) / ("e_%d.txt" % len(label))).write_text("own\n")
                g("add", "-A")
                g("commit", "-q", "-m", "own commit")
                self.assertGreater(self.rehold(self.api.rev("feature")), 0)
                self.assertEqual((self.body_patches(), self.body), ([], before))
                (done,) = self.events("gate.hold_reapplied")[-1:]
                self.assertNotIn("marker", done["detail"], label)

    def test_s12_an_unreadable_body_writes_nothing_and_the_labels_still_stand(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.writes.clear()
        new = self.own_commit()
        reads, real = [], self.route

        def route(argv, **kwargs):
            if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
                reads.append(1)
                if len(reads) == 2:   # the read right before the write
                    return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 502")
            return real(argv, **kwargs)
        self.engine.gh_runner = route
        self.assertGreater(self.rehold(new), 0)
        self.assertEqual((self.body_patches(), self.body), ([], "MERGE: automatic\n\nthe change\n"))
        self.assertEqual(len(self.adds()), 1, "the hold labels are written all the same")
        (done,) = self.events("gate.hold_reapplied")
        self.assertIn("error", done["detail"]["marker"])

    def test_s12b_a_body_read_back_that_is_not_the_one_written_is_said_and_the_labels_stand(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.writes.clear()
        new = self.own_commit()
        reads, real = [], self.route

        def route(argv, **kwargs):
            done = real(argv, **kwargs)
            if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
                reads.append(1)
                if len(reads) == 3:   # the read back after the write
                    raw = json.loads(done.stdout)
                    raw["body"] = "MERGE: automatic\n\nnot what was written\n"
                    return subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
            return done
        self.engine.gh_runner = route
        self.assertGreater(self.rehold(new), 0)
        (done,) = self.events("gate.hold_reapplied")
        self.assertEqual(done["detail"]["marker"], {"error": "the body read back is not the one written"})
        self.assertEqual(len(self.adds()), 1)

    def test_s13_a_body_that_changed_while_it_was_being_edited_is_not_written(self):
        self.body = "MERGE: captain\n\nthe change\n"
        self.clear()
        self.writes.clear()
        new = self.own_commit()
        reads, real = [], self.route

        def route(argv, **kwargs):
            if argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
                reads.append(1)
                if len(reads) == 2:
                    self.body = "MERGE: automatic\n\nthe author edited this\n"
            return real(argv, **kwargs)
        self.engine.gh_runner = route
        self.rehold(new)
        self.assertEqual((self.body_patches(), self.body), ([], "MERGE: automatic\n\nthe author edited this\n"))

    def test_a_conflict_merge_after_a_rewritten_history_or_with_a_changed_line_is_void_not_waiting(self):
        self.clear()
        new = self.conflict_merge(resolved=self.RESOLVED.replace("TWO", "TWO!"))
        self.confirm(new)
        self.assertGreater(self.hold(new), 0)
        self.assertEqual(self.events("gate.clear_carried"), [])
        state = self.held_state()
        self.assertIn(self.PR, state["carry_void"])
        self.assertIn("differs", state["carry_void"][self.PR]["reason"])
        gets = self.api_gets
        self.hold(new)
        self.assertEqual(self.api_gets, gets, "a void is decided once per head")


class TestShadowClassifier(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("cos",), config={"jev": {"replay": "/fake/replay.json"}})
        self.world.add_agent("coder", fleet={"role": "coder", "issue": 615, "reports_to": "cos"})
        self.world.sessions["cos"]
        self.fix_role("cos", "chief-of-staff")
        self.world.engine = self.world.new_engine()
        self.jev = self.world.engine.jev = FakeJev(lambda s, q: confident("new", 0.9, tuple(q[sb.CLASSIFY_ID]["criteria"])))

    def fix_role(self, name, role):
        self.world.v2.sessions[self.world.sessions[name]]["metadata"]["fleet"]["role"] = role

    def say(self, text, lasts=30):
        self.world.v2.start_turn(self.world.sessions["cos"], text, fleet=False, lasts=lasts)

    def passes(self, count=1):
        for _ in range(count):
            self.world.engine.run_pass()
            self.world.tick(5)

    def classifications(self):
        return [e for e in self.world.audit() if e["event"] == "classify"]

    def test_a_new_prompt_while_busy_is_classified_once_and_audited(self):
        self.say("Also, the billing export?")
        self.passes(3)
        self.assertEqual(len(self.jev.calls), 1)
        (entry,) = self.classifications()
        detail = entry["detail"]
        self.assertTrue(entry["subject"].startswith("classify:%s:" % self.world.sessions["cos"]))
        self.assertEqual(detail["agent"], "cos")
        self.assertEqual(detail["question"]["state"]["message"], "Also, the billing export?")
        self.assertEqual(detail["question"]["options"], ["current", "615", "new"])
        self.assertEqual((detail["answer"]["choice"], detail["answer"]["confident"]), ("new", True))
        self.assertEqual((detail["latency_ms"], detail["usage"]["cost"], detail["model"]), (12, 3e-05, "fake"))
        self.assertTrue(detail["shadow"])

    def test_it_takes_no_action_at_all(self):
        self.say("Also, the billing export?")
        self.passes(3)
        self.assertEqual(self.world.v2.writes, [])
        self.assertEqual(self.world.admissions("cos"), [])
        self.assertEqual(self.world.admissions("coder"), [])
        self.assertEqual([e for e in self.world.audit() if e["event"] not in ("classify",)], [])
        self.assertEqual([c[1:3] for c in self.world.herdr.calls if c[1:3] not in HERDR_ALLOWED], [])

    def test_each_new_message_is_classified_once_and_the_previous_one_is_sent_along(self):
        self.say("first", lasts=5)
        self.passes(1)
        self.world.tick(10)
        self.say("second", lasts=30)
        self.passes(3)
        self.assertEqual([c["state"]["message"] for c in self.jev.calls], ["first", "second"])
        self.assertIsNone(self.jev.calls[0]["state"]["previous_message"])
        self.assertEqual(self.jev.calls[1]["state"]["previous_message"], "first")
        keys = [e["subject"] for e in self.classifications()]
        self.assertEqual(len(set(keys)), 2)

    def test_a_restart_forgets_in_memory_but_only_reclassifies_a_message_still_in_the_window(self):
        self.say("hello", lasts=300)
        self.passes(1)
        self.world.engine = self.world.new_engine()
        self.world.engine.jev = self.jev
        self.passes(1)
        self.assertEqual(len(self.jev.calls), 2, "dedupe is in memory; a restart classifies the newest prompt again")
        self.world.tick(11 * 60)
        self.world.engine = self.world.new_engine()
        self.world.engine.jev = self.jev
        self.passes(1)
        self.assertEqual(len(self.jev.calls), 2, "a prompt older than the engagement window is left alone")

    def test_nothing_to_choose_between_means_no_call(self):
        self.world.v2.sessions[self.world.sessions["coder"]]["metadata"]["fleet"].pop("issue")
        self.say("a lone message", lasts=0)
        self.passes(2)
        self.assertEqual(self.jev.calls, [], "idle, and no other ask")

    def test_other_asks_alone_are_enough_to_classify_an_idle_chief_of_staff(self):
        self.say("a message", lasts=0)
        self.passes(1)
        self.assertEqual(len(self.jev.calls), 1)

    def test_only_a_chief_of_staff_is_classified(self):
        self.fix_role("cos", "orchestrator")
        self.say("a message")
        self.passes(2)
        self.assertEqual(self.jev.calls, [])

    def test_the_fleets_own_messages_are_not_yours(self):
        self.world.v2.start_turn(self.world.sessions["cos"], "fleet task", fleet=True, lasts=30)
        self.passes(2)
        self.assertEqual(self.jev.calls, [])

    def test_unconfigured_or_switched_off_it_does_nothing(self):
        self.say("a message")
        self.world.engine.config = dict(self.world.engine.config, shadow_judges=False)
        self.passes(2)
        self.assertEqual(self.jev.calls, [])
        self.world.engine.config = dict(self.world.engine.config, shadow_judges=True)
        self.world.engine.jev = None
        self.passes(1)
        self.assertEqual(self.classifications(), [])
        bare = World(self, agents=("cos",), state=self.home / "bare")
        self.assertIsNone(bare.engine.jev)
        self.assertNotIn("jev", bare.engine.config)

    def test_the_no_classify_fault_skips_it_in_the_lab_only(self):
        os.environ[sb.FAULT_ENV] = "no-classify"
        self.say("a message")
        self.passes(1)
        self.assertEqual(self.jev.calls, [])
        os.environ.pop(sb.LAB_ENV)
        self.passes(1)
        self.assertEqual(len(self.jev.calls), 1)

    def test_an_unreachable_jev_is_audited_as_current_and_never_retried_for_the_same_message(self):
        self.jev.rule = "unreachable"
        self.say("a message")
        self.passes(3)
        (entry,) = self.classifications()
        self.assertEqual((entry["detail"]["answer"]["choice"], entry["detail"]["answer"]["reason"]),
                         ("current", "jev unreachable"))
        self.assertEqual(len(self.jev.calls), 1)

    def test_a_crash_in_the_classifier_is_audited_and_the_pass_goes_on(self):
        def explode(state, questions):
            raise RuntimeError("boom")
        self.jev.rule = explode
        self.world.fact("coder", "lab.fact:x", "something")
        self.say("a message")
        self.world.tick(100)
        report = self.world.engine.run_pass()
        errors = [e for e in self.world.audit() if e["event"] == "error"]
        self.assertEqual(errors[0]["detail"]["step"], "classify")
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["sent"]), 1, "delivery still happened")

    def test_the_key_never_reaches_the_audit(self):
        os.environ["OPENROUTER_API_KEY"] = KEY_VALUE
        opener = FakeOpener(http_error(401, ("echo " + KEY_VALUE).encode()))
        self.world.engine.jev = sb.JevClient(dict(JEV_SETTINGS), opener=opener)
        self.say("a message")
        self.passes(1)
        self.assertEqual(self.classifications()[0]["detail"]["answer"]["reason"], "jev unauthorized")
        self.assertNotIn(KEY_VALUE, self.world.audit_path.read_text())

    def test_the_audit_redacts_nothing_it_needs(self):
        # `usage` uses names the audit's redaction does not treat as credentials.
        entry = sb.redact({"usage": {"in": 1, "out": 2, "cost": 3e-05}})
        self.assertEqual(entry, {"usage": {"in": 1, "out": 2, "cost": 3e-05}})


class TestMessagePaging(Base):
    """V2Client reads past one page by following v2's cursor (PR 4's known gap)."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("cos",))
        self.sid = self.world.sessions["cos"]
        self.client = sb.V2Client(FAKE_OPENCODE, {}, self.world.v2, 5)

    def fill(self, count, step=1):
        for number in range(count):
            self.world.v2.sessions[self.sid]["messages"].append(
                {"id": "msg_p%05d" % number, "sessionID": self.sid, "type": "synthetic", "text": "x",
                 "metadata": {"fleet": {"keys": ["k%d" % number], "from": "switchboard"}},
                 "time": {"created": self.world.clock.ms(EPOCH + number * step)}})

    def test_the_cursor_argv_is_strict(self):
        runner = FakeRunner(default=(0, '{"data": [], "cursor": {"next": null, "previous": null}}', ""))
        client = sb.V2Client("/opt/lab/bin/opencode", {}, runner, 7)
        self.assertEqual(client.message_page(SES, 200, "synthetic", "cur_9"), ([], {"next": None, "previous": None}))
        self.assertEqual(runner.argv, ["/opt/lab/bin/opencode", "api", "session.message.list", "--param", "sessionID=" + SES,
                                       "--param", "limit=200", "--param", "type=synthetic", "--param", "cursor=cur_9"])
        self.assertNotIn("order=desc", runner.argv)  # v2: a cursor is not combined with order
        for bad in ("", 7, "x" * (sb.CURSOR_LIMIT + 1)):
            with self.subTest(cursor=bad), self.assertRaises(ValueError):
                client.message_page(SES, 200, "synthetic", bad)

    def test_a_listing_without_a_cursor_object_gives_none(self):
        client = sb.V2Client(FAKE_OPENCODE, {}, FakeRunner(default=(0, '{"data": [{"id": "m"}]}', "")), 5)
        self.assertEqual(client.message_page(SES, 5), ([{"id": "m"}], None))

    def test_450_messages_are_read_in_nine_pages_of_fifty_and_none_is_missed(self):
        self.fill(450)
        keys, complete = self.world.engine.delivered_keys(self.sid, EPOCH - 60)
        self.assertTrue(complete)
        self.assertEqual(keys, {"k%d" % n for n in range(450)})
        reads = [c for c in self.world.v2.calls if c[2] == "session.message.list" and "type=synthetic" in c and "limit=%d" % sb.SYNTHETIC_PAGE in c]
        self.assertEqual(len(reads), 9, "nine pages of fifty (the probe of limit 1 is not counted)")
        self.assertTrue(all("limit=%d" % sb.SYNTHETIC_PAGE in c for c in reads))
        self.assertEqual([c for c in reads[1:] if "order=desc" in c or "order=asc" in c], [])

    def test_a_recipient_with_450_synthetic_messages_is_not_held(self):
        self.fill(450)
        engine = self.world.engine
        plan = engine.plan("cos", [fact("brand-new", EPOCH, recipient="cos")], engine.discover(EPOCH + 1000), EPOCH + 1000)
        self.assertEqual(plan.action, "wake", plan.reason)

    def test_the_read_stops_at_the_oldest_fact_without_reading_every_page(self):
        self.fill(450)
        # the newest 50 are k400..k449, made at EPOCH+400 and later: a horizon of EPOCH+420 lies in the first page
        keys, complete = self.world.engine.delivered_keys(self.sid, EPOCH + 420)
        self.assertTrue(complete)
        self.assertEqual(len([c for c in self.world.v2.calls if c[2] == "session.message.list" and "limit=%d" % sb.SYNTHETIC_PAGE in c]), 1,
                         "the first page reaches the horizon, so no cursor is followed")

    def test_more_pages_than_the_bound_hold_rather_than_guess(self):
        self.fill(450)
        with mock.patch.object(sb, "MAX_READ_PAGES", 2):
            _, complete = self.world.engine.delivered_keys(self.sid, EPOCH - 60)
        self.assertFalse(complete)

    def test_a_cursor_that_does_not_walk_older_holds_and_says_so(self):
        self.fill(450)
        original = self.world.v2.page_of

        def stuck(messages, params):
            page = original(messages, params)
            page.envelope["cursor"]["next"] = self.world.v2.CURSOR_PREFIX + "0"  # always the first page again
            return page
        self.world.v2.page_of = stuck
        engine = self.world.engine
        plan = engine.plan("cos", [fact("brand-new", EPOCH, recipient="cos")], engine.discover(EPOCH + 1000), EPOCH + 1000)
        self.assertEqual(plan.action, "hold")
        self.assertIn("cannot read what cos already has", plan.reason)

    def test_a_full_page_with_no_next_cursor_is_the_end_not_a_hold(self):
        self.fill(sb.SYNTHETIC_PAGE)
        keys, complete = self.world.engine.delivered_keys(self.sid, EPOCH - 60)
        self.assertTrue(complete)
        self.assertEqual(len(keys), sb.SYNTHETIC_PAGE)
        self.assertEqual(len([c for c in self.world.v2.calls if c[2] == "session.message.list"]), 1)

    def test_an_answer_with_no_cursor_and_a_full_page_still_holds(self):
        self.fill(sb.SYNTHETIC_PAGE)
        self.world.v2.page_of = lambda messages, params: copy.deepcopy(messages[:int(params["limit"])])
        _, complete = self.world.engine.delivered_keys(self.sid, EPOCH - 60)
        self.assertFalse(complete)


class TestClassifierModeConfig(Base):
    JEV = {"jev": {"replay": "/fake/replay.json"}}

    def test_shadow_is_the_default_and_act_is_the_only_other_mode(self):
        self.assertEqual(sb.validate_config({})["classifier_mode"], "shadow")
        self.assertEqual(sb.validate_config(dict(self.JEV, classifier_mode="act"))["classifier_mode"], "act")
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config(dict(self.JEV, classifier_mode="enforce"))
        self.assertIn("shadow, act", str(caught.exception))

    def test_acting_without_a_decision_model_is_refused(self):
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"classifier_mode": "act"})
        self.assertIn("jev", str(caught.exception))

    def test_acting_is_off_in_the_engine_unless_both_are_set(self):
        world = World(self, agents=("cos",), config=dict(self.JEV, classifier_mode="act"))
        self.assertTrue(sb.acting(world.engine))
        world.engine.jev = None
        self.assertFalse(sb.acting(world.engine))
        self.assertFalse(sb.acting(World(self, agents=("cos",), state=self.home / "s2", config=self.JEV).engine))


class TestSignedCommentsAreNotEvents(Base):
    def setUp(self):
        super().setUp()
        self.rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1}})
        self.gh, self.hub = self.rig.gh, self.rig.hub
        self.gh.issue(1)
        self.gh.issue(12, parent=1)
        self.hub.tick()

    def test_a_signed_comment_is_dropped_from_the_webhook_and_an_unsigned_one_is_kept(self):
        self.gh.comment(12, "I finished.\n\n<!-- fleet-switchboard:from=platform -->", 5001)
        self.gh.comment(12, "A human note", 5002)
        self.assertEqual(sorted(f.key for f in self.hub.facts()), ["github.comment:5002"])

    def test_the_catch_up_read_drops_it_too_so_a_restart_does_not_bring_it_back(self):
        self.gh.comment(12, "I finished.\n\n<!-- fleet-switchboard:from=platform -->", 5001, forward=False)
        self.gh.comment(12, "A human note", 5002, forward=False)
        items, errors = self.hub.catch_up_items(GH_REPO, self.rig.world.clock.now())
        self.assertEqual(([i.key for i in items if i.kind == "github.comment"], errors), (["github.comment:5002"], []))

    def test_a_signed_review_is_dropped_as_well(self):
        review = {"id": 77, "state": "commented", "body": "ok\n\n<!-- fleet-switchboard:from=cos -->", "user": {"login": "x"}}
        self.assertIsNone(sb._review_item(GH_REPO, review, {"number": 5}, 0))
        self.assertIsNotNone(sb._review_item(GH_REPO, dict(review, body="ok"), {"number": 5}, 0))


class TestIgnoredAuthors(Base):
    """When the agents run as a bot account rather than as you, the author does say who wrote it."""

    def rig(self, ignore):
        rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1}},
                        config={"github_ignore_authors": ignore})
        rig.gh.issue(1)
        rig.gh.issue(12, parent=1)
        rig.hub.tick()
        return rig

    def test_events_by_an_ignored_author_are_not_delivered_and_the_audit_says_so(self):
        rig = self.rig(["Fleet-Bot"])
        rig.gh.comment(12, "from an agent", 5001, login="fleet-bot")
        rig.gh.comment(12, "from the human", 5002, login="you")
        self.assertEqual(sorted(f.key for f in rig.hub.facts()), ["github.comment:5002"])
        ignored = [e for e in rig.world.audit() if e["event"] == "github.ignored"]
        self.assertEqual(ignored[0]["detail"], {"keys": ["github.comment:5001"], "authors": ["fleet-bot"]})

    def test_with_nobody_ignored_every_author_is_delivered(self):
        rig = self.rig([])
        rig.gh.comment(12, "a", 5001, login="fleet-bot")
        self.assertEqual([f.key for f in rig.hub.facts()], ["github.comment:5001"])

    def test_the_setting_is_a_list_of_logins(self):
        base = {"opencode": None, "gh": "gh"}
        self.assertEqual(sb.validate_config(dict(base, github_ignore_authors=["a-b", "bot[bot]"]))["github_ignore_authors"],
                         ["a-b", "bot[bot]"])
        for bad in ("fleet-bot", [1], ["has space"], ["-x"], [""]):
            with self.subTest(value=bad), self.assertRaises(sb.ConfigError):
                sb.validate_config(dict(base, github_ignore_authors=bad))


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


class TestNotFleetSessionsAreNotAskedAboutEveryPass(Base):
    """The live herdr lists every pane of every workspace. The trial's daemon asked its v2 about all fifteen,
    every pass: 2.7 s a pass, which is why a decoration landed after the step boundary."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform",))
        self.engine = self.world.engine
        self.plain = self.world.v2.create_session({})  # no metadata.fleet
        self.entry = {"pane_id": "wZ:p9", "agent": "opencode", "agent_status": "idle",
                      "agent_session": {"kind": "id", "value": self.plain}}

    def gets(self, session):
        return len([c for c in self.world.v2.calls if "session.get" in c and "sessionID=" + session in c])

    def test_a_session_that_is_no_fleet_agent_is_asked_about_once_in_a_while(self):
        for _ in range(3):
            self.engine.discover(self.world.clock.now(), entries=[self.entry])
        self.assertEqual(self.gets(self.plain), 1)
        self.world.tick(self.engine.NOT_FLEET_SECONDS + 1)
        self.engine.discover(self.world.clock.now(), entries=[self.entry])
        self.assertEqual(self.gets(self.plain), 2, "believed for a while, not for ever")

    def test_a_session_v2_does_not_have_is_remembered_too_but_an_unreachable_v2_is_not(self):
        ghost = dict(self.entry, agent_session={"kind": "id", "value": "ses_neverexisted"})
        for _ in range(3):
            self.engine.discover(self.world.clock.now(), entries=[ghost])
        self.assertEqual(self.gets("ses_neverexisted"), 1)
        real = self.engine.v2.session_get
        self.engine.v2.session_get = lambda s: (_ for _ in ()).throw(sb.V2Error("timeout", None, "slow"))
        found = self.engine.discover(self.world.clock.now(), entries=[dict(self.entry, agent_session={"kind": "id", "value": "ses_other"})])
        self.assertTrue(found.problems)
        self.assertNotIn("ses_other", self.engine._not_fleet, "a transient error says nothing about the session")
        self.engine.v2.session_get = real

    def test_a_fleet_agent_is_never_remembered_as_none(self):
        for _ in range(3):
            found = self.engine.discover(self.world.clock.now())
        self.assertIn("platform", found.agents)
        self.assertNotIn(self.world.sessions["platform"], self.engine._not_fleet)
        self.assertEqual(self.gets(self.world.sessions["platform"]), 3, "a fleet agent is read every pass")


class TestForwarderReasonLine(Base):
    USAGE = ["Receive test events locally", "", "Usage:", "  forward --events=<types> [--url=<url>] [flags]",
             "Examples:", "$ gh webhook forward --events=issues --repo=monalisa/smile", "Flags:",
             "  -U, --url string   Address of the local server to receive events. If omitted, events will be printed."]

    def lines(self, *head):
        return [line for line in list(head) + self.USAGE if line.strip()]

    def test_the_error_line_wins_over_the_usage_text_that_follows_it(self):
        lines = self.lines("Error: error connecting to GitHub: websocket: bad handshake")
        self.assertEqual(sb.forwarder_reason_line(lines), "Error: error connecting to GitHub: websocket: bad handshake")
        self.assertNotEqual(lines[-1], sb.forwarder_reason_line(lines), "the old choice was the last line")

    def test_with_no_error_word_the_first_line_that_is_not_usage_is_used(self):
        self.assertEqual(sb.forwarder_reason_line(self.lines("the connection went away")), "the connection went away")

    def test_output_that_is_all_usage_still_gives_a_line(self):
        self.assertTrue(sb.forwarder_reason_line(self.USAGE))

    def test_a_dead_forwarders_reason_in_the_audit_is_that_line(self):
        rig = GithubRig(self)
        rig.hub.tick()
        proc = rig.gh.procs[-1]
        with open(str(rig.hub.log_path(rig.watch())), "w") as log:
            log.write("\n".join(self.lines("Error: HTTP 422: Validation Failed")) + "\n")
        proc.exit(1)
        rig.hub.tick()
        self.assertIn("Validation Failed", rig.watch().last_error)
        self.assertNotIn("--url string", rig.watch().last_error)


class TestScheduleJudgeWarm(Base):
    def rig(self, ttl=300, warmer=True):
        world = World(self, agents=("a", "b"))
        engine = world.engine
        engine.config = dict(engine.config, policy={"cache_seconds": ttl})
        calls, done = [], threading.Event()
        if warmer:
            def warm(sessions):
                calls.append(list(sessions))
                done.set()
            engine.judge_warm = warm
        return world, engine, calls, done

    def schedule(self, world, engine, done):
        done.clear()
        sb.schedule_judge_warm(engine, engine.discover(world.clock.now()))
        done.wait(1)

    def test_it_warms_every_session_once_then_waits_half_the_cache_lifetime(self):
        world, engine, calls, done = self.rig()
        self.schedule(world, engine, done)
        self.assertEqual(calls, [sorted(world.sessions.values())])
        world.tick(60)
        self.schedule(world, engine, done)
        self.assertEqual(len(calls), 1, "not again within half of 300 s")
        world.tick(100)
        self.schedule(world, engine, done)
        self.assertEqual(len(calls), 2)

    def test_a_daemon_pass_starts_the_warm_up(self):
        world, engine, calls, done = self.rig()
        engine.run_pass()
        self.assertTrue(done.wait(2))
        self.assertEqual(calls, [sorted(world.sessions.values())])

    def test_it_does_nothing_without_a_warmer_or_with_the_cache_off(self):
        for kwargs in ({"warmer": False}, {"ttl": 0}):
            with self.subTest(kwargs=kwargs):
                world, engine, calls, done = self.rig(**kwargs)
                self.schedule(world, engine, done)
                self.assertEqual(calls, [])

    def test_a_failing_warmer_never_breaks_the_pass(self):
        warm = sb.make_judge_warmer({"opencode": None, "policy": {"cache_seconds": 300}}, subprocess.run, time.time)
        with captured() as (out, err):
            warm(["ses_x"])  # no opencode configured: the judge cannot be built, and that is only reported
        self.assertIn("skipped", err.getvalue())


class TestTriageT5Rollup(Base):
    """#766: a T5 decision is listed once past a day, then only in one daily roll-up line. Temp state dir, a fake hub: nothing real."""
    DAY = 86400

    def setUp(self):
        Base.setUp(self)
        self.world = World(self, agents=(), config={"github_repos": ["acme/api"]})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.hub = FakeTriageHub()
        self.engine = self.world.engine
        self.engine.hub = self.hub
        self.engine.config["triage"] = dict(sb.TRIAGE_DEFAULTS, repos=["acme/api"], our_logins=["bot"], enabled=True, backoff=True)
        self.engine.discover(self.world.clock.now())
        self.hub.open["acme/api"] = {}
        self.now = self.world.clock.now()

    def decision(self, n, hours=30, kind="issue"):
        d = {"id": "d%d" % n, "kind": kind, "tier": "human", "repo": "acme/api", "since": sb.iso(self.now - hours * 3600)}
        d["number" if kind == "captain" else "ask"] = n
        return d

    def read_notices(self):
        """Run the production notice reader; `self.notices_cut` makes one noticed PR unread (the reader's own budget skip)."""
        now = self.world.clock.now()
        if getattr(self, "notices_cut", False):
            item = {"repo": "acme/api", "number": 99, "created": now, "note": None, "from": "platform"}
            skipped = {"at": 0, "facts": None, "stale": True, "skipped": True, "confirmed": None, "error": "budget"}
            with mock.patch.object(sb, "read_pr_notices", return_value=[item]), mock.patch.object(sb, "notice_facts", return_value=skipped):
                sb.decisions_from_notices(self.engine, now)
        else:
            sb.decisions_from_notices(self.engine, now)

    def run_pass(self, decisions, tick=1, errors=()):
        """One pass; returns (T5 refs in the digest, the roll-up lines queued)."""
        self.world.tick(tick)
        seen_before = set(getattr(self.engine, "triage", None).facts) if getattr(self.engine, "triage", None) else set()
        shown = []
        real = sb.triage_queue_fact
        def spy(state, due, *a, **k):
            shown.extend(ref for rule, ref in due if rule in ("T5", "T6"))
            return real(state, due, *a, **k)
        with mock.patch.object(sb, "triage_queue_fact", spy):
            self.read_notices()                                                      # the REAL notice reader marks the input
            sb.triage_pass(self.engine, decisions, self.world.clock.now(), errors)
        lines = [v[0] for k, v in self.engine.triage.facts.items() if k.startswith("triage.rollup") and k not in seen_before]
        return shown, lines

    def test_s1_s2_listed_once_then_never_again_even_after_a_reload(self):
        ds = [self.decision(7)]
        self.assertEqual(self.run_pass(ds)[0], ["acme/api#7"])
        self.assertEqual(self.run_pass(ds, 2 * 3600)[0], [])
        self.engine.triage.seen = self.engine.triage.meta = None      # a restart: the memory comes back from the file
        self.assertEqual(self.run_pass(ds, 3 * self.DAY)[0], [])

    def test_s3_one_roll_up_line_a_day_naming_the_listed_items(self):
        ds = [self.decision(7), self.decision(9)]
        self.assertEqual(self.run_pass(ds), (["acme/api#7", "acme/api#9"], []))     # the listing itself is not the roll-up
        self.assertEqual(self.run_pass(ds, 3600), ([], []))                          # a day has not passed since the listing
        shown, lines = self.run_pass(ds, self.DAY)
        self.assertEqual((shown, lines), ([], ["2 items waiting on the owner for more than a day: #7 #9"]))
        self.assertEqual(self.run_pass(ds, 3600), ([], []))                          # once in 24 hours
        self.assertEqual(self.run_pass(ds, 3600)[1], [])

    def test_s8_three_thousand_items_are_one_capped_line_under_the_limit(self):
        seen = {"T5 acme/api#%d" % i: {"at": float(i), "n": 1, "sig": "", "flagged": False} for i in range(3000)}
        line = sb.triage_digest.rollup(seen, {}, [k[3:] for k in seen], 10 * self.DAY)
        self.assertLess(len(line), 1500)
        self.assertTrue(line.startswith("3000 items waiting on the owner for more than a day: #0 #1 #2 "))
        self.assertEqual(line.count("#"), 30)                                       # 30 names, oldest listing first
        self.assertTrue(line.endswith("... and 2970 more (fleet-switchboard decisions --tier all)"))
        long = {"T5 %s/%s#%d" % ("o" * 90, "r" * 90 + str(i), i): {"at": float(i), "n": 1, "sig": "", "flagged": False} for i in range(40)}
        self.assertLess(len(sb.triage_digest.rollup(long, {}, [k[3:] for k in long], 10 * self.DAY)), 1500)   # long names: still under it

    def test_s9_the_names_carry_the_repository_only_when_there_are_several(self):
        rec = {"at": 0.0, "n": 1, "sig": "", "flagged": False}
        two = {"T5 acme/api#7": rec, "T5 acme/web#7": rec}
        self.assertEqual(sb.triage_digest.rollup(two, {}, ["acme/api#7", "acme/web#7"], self.DAY),
                         "2 items waiting on the owner for more than a day: acme/api#7 acme/web#7")
        self.assertEqual(sb.triage_digest.rollup({"T5 acme/api#7": rec}, {}, ["acme/api#7"], self.DAY),
                         "1 items waiting on the owner for more than a day: #7")

    def test_s10_an_undelivered_roll_up_stays_one_pending_fact_for_the_fact_mechanism_to_retry(self):
        ds = [self.decision(7)]
        self.run_pass(ds)
        self.assertEqual(len(self.run_pass(ds, self.DAY)[1]), 1)
        pending = lambda: {k: v[0] for k, v in self.engine.triage.facts.items() if k.startswith("triage.rollup")}
        before = pending()
        for _ in range(3):
            self.run_pass(ds, 3600)
        self.assertEqual(pending(), before)                                          # kept, same text, not duplicated

    def test_s4_an_item_that_leaves_the_tier_disappears_and_an_empty_roll_up_is_not_sent(self):
        ds = [self.decision(7), self.decision(9)]
        self.run_pass(ds)
        self.assertEqual(self.run_pass(ds[1:], self.DAY)[1], ["1 items waiting on the owner for more than a day: #9"])
        self.assertEqual(self.run_pass([], self.DAY)[1], [])

    def test_s5_a_returning_item_is_listed_once_as_new(self):
        ds = [self.decision(7)]
        self.run_pass(ds)
        self.run_pass([], 3600)                                                      # it left
        self.assertEqual(self.run_pass(ds, 3600)[0], ["acme/api#7"])
        self.assertEqual(self.run_pass(ds, 3600)[0], [])

    def seen_keys(self):
        return sorted(self.engine.triage.seen)

    def test_s6_an_owner_tier_t6_is_listed_once_then_only_in_the_roll_up_with_t5(self):
        ds = [self.decision(51, hours=72, kind="captain"), self.decision(7)]
        shown, _ = self.run_pass(ds)
        self.assertEqual(sorted(shown), ["acme/api#51", "acme/api#7"])
        for _ in range(3):
            self.assertEqual(self.run_pass(ds, 3600)[0], [])                         # not again, backoff or no backoff
        lines = self.run_pass(ds, self.DAY)[1]
        self.assertEqual(lines, ["2 items waiting on the owner for more than a day: #7 #51"])

    def test_s6b_a_t6_not_at_the_owners_tier_keeps_its_backoff_and_stays_out_of_the_roll_up(self):
        ds = [self.decision(51, hours=72, kind="captain")]
        self.run_pass(ds)
        pair = ("T6", "acme/api#51")
        seen = self.engine.triage.seen
        self.assertIn("T6 acme/api#51", seen)
        due, _ = sb.triage_digest.select(seen, {}, {}, [pair], self.now + 3 * self.DAY, True, None, 86400.0, 366, ())
        self.assertEqual(due, [pair])                                                # not in `once`: the day's backoff lists it again
        self.assertIsNone(sb.triage_digest.rollup(seen, {}, [], self.now + 3 * self.DAY))

    def test_a_not_whole_pass_keeps_the_t5_and_t6_records_and_a_whole_pass_without_an_item_prunes_it(self):
        ds = [self.decision(7), self.decision(51, hours=72, kind="captain")]
        self.run_pass(ds)
        self.assertEqual(self.seen_keys(), ["T5 acme/api#7", "T6 acme/api#51"])
        self.run_pass([], 3600, errors=[{"source": "github", "error": "not read yet"}])
        self.assertEqual(self.seen_keys(), ["T5 acme/api#7", "T6 acme/api#51"])      # kept: the list was not whole
        self.assertEqual(sb.whole(self.engine, "decisions")[0], False)
        self.assertIn("whole.skipped", [e["event"] for e in self.world.audit()])
        self.run_pass(ds[1:], 3600)
        self.assertEqual(self.seen_keys(), ["T6 acme/api#51"])                       # a whole pass without #7: forgotten
        self.run_pass([], 3600)
        self.assertEqual(self.seen_keys(), [])

    def test_a_returning_item_after_a_whole_absence_is_new_once(self):
        ds = [self.decision(51, hours=72, kind="captain")]
        self.run_pass(ds)
        self.run_pass([], 3600)
        self.assertEqual(self.run_pass(ds, 3600)[0], ["acme/api#51"])
        self.assertEqual(self.run_pass(ds, 3600)[0], [])

    def test_a_restart_with_a_first_pass_that_is_not_whole_then_a_whole_pass_lists_nothing_already_listed(self):
        ds = [self.decision(7), self.decision(51, hours=72, kind="captain")]
        self.run_pass(ds)
        self.engine.triage.seen = self.engine.triage.meta = None                     # a restart: memory in the engine is gone
        self.engine.__dict__.pop("read_whole", None)
        self.assertEqual(sb.whole(self.engine, "decisions"), (False, "not read yet in this process"))
        self.assertEqual(self.run_pass([], 3600, errors=[{"source": "github", "error": "not read yet"}])[0], [])
        self.assertEqual(self.run_pass(ds, 3600)[0], [])                             # nothing listed again
        self.assertEqual(self.seen_keys(), ["T5 acme/api#7", "T6 acme/api#51"])

    def test_the_records_of_a_repository_whose_issue_list_was_not_read_whole_are_kept(self):
        for damage in ({"error": "rate limited"}, {"truncated": True}):
            with self.subTest(damage=damage):
                self.run_pass([self.decision(7)], 3600)                              # past the digest hour: it is listed
                unit = self.engine.triage.units[("acme/api", "issues")]
                self.engine.triage.units[("acme/api", "issues")] = dict(unit, **damage)
                self.run_pass([], 60)                                                # the decision list is whole and has no #7
                self.assertEqual(sb.whole(self.engine, "decisions")[0], True)
                self.assertEqual(sb.whole(self.engine, "issues:acme/api")[0], False)
                self.assertEqual(self.seen_keys(), ["T5 acme/api#7"])                # kept: its repository's list was not whole
                self.engine.triage.units[("acme/api", "issues")] = unit
                self.run_pass([], 60)                                                # a whole pass without it: forgotten
                self.assertEqual(self.seen_keys(), [])

    def test_a_skipped_prune_says_so_once_per_pass_and_input(self):
        self.run_pass([self.decision(7)])
        self.run_pass([], 3600, errors=[{"source": "github", "error": "not read yet"}])
        keep = sb.triage_digest_keep(self.engine)
        self.assertTrue(keep("T5", "acme/api#7") and keep("T6", "acme/api#9"))      # two prunes in one pass
        lines = [e for e in self.world.audit() if e["event"] == "whole.skipped" and e["detail"]["input"] == "decisions"]
        self.assertEqual(len(lines), 1)

    def test_a_pass_with_unread_notices_is_not_whole_and_keeps_the_t6_records(self):
        ds = [self.decision(51, hours=72, kind="captain"), self.decision(7)]
        self.run_pass(ds)
        self.assertEqual(self.seen_keys(), ["T5 acme/api#7", "T6 acme/api#51"])
        self.notices_cut = True                                                       # no error, but the real notice reader skips a noticed PR
        self.run_pass([], 3600)
        self.assertEqual(sb.whole(self.engine, "decisions"), (False, "1 notices unread"))
        self.assertEqual(self.seen_keys(), ["T5 acme/api#7", "T6 acme/api#51"])      # kept
        self.notices_cut = False
        self.assertEqual(self.run_pass(ds, 3600)[0], [])                             # nothing already listed is listed again
        self.assertEqual(sb.whole(self.engine, "decisions"), (True, ""))
        self.run_pass(ds[:1], 3600)
        self.assertEqual(self.seen_keys(), ["T6 acme/api#51"])                       # a whole pass without #7 prunes it

    def test_the_registry_adds_no_read_to_a_pass(self):
        ds = [self.decision(7)]
        self.run_pass(ds, 3600)
        before = len(self.hub.calls)
        self.run_pass(ds, 3600)
        with_registry = len(self.hub.calls) - before
        with mock.patch.object(sb, "read_mark", lambda *a, **k: None):             # the same pass with the registry switched off
            before = len(self.hub.calls)
            self.run_pass(ds, 3600)
            without = len(self.hub.calls) - before
        self.assertEqual(with_registry, without)

    def test_the_registry_marks_an_input_whole_only_after_a_clean_read_and_a_restart_clears_it(self):
        self.assertEqual(sb.whole(self.engine, "decisions"), (False, "not read yet in this process"))
        sb.read_mark(self.engine, "decisions", False, "errors", 1.0)
        self.assertEqual(sb.whole(self.engine, "decisions"), (False, "errors"))
        sb.read_mark(self.engine, "decisions", True, "", 2.0)
        self.assertEqual(sb.whole(self.engine, "decisions"), (True, ""))

    def test_s7_the_memory_is_in_the_seen_file_of_the_state_dir(self):
        self.run_pass([self.decision(7)])
        raw = json.loads((Path(self.engine.state_dir) / sb.triage_digest.SEEN_FILE).read_text())
        self.assertIn("T5 acme/api#7", raw["items"])
        self.run_pass([self.decision(7)], self.DAY)
        self.assertIsNotNone(json.loads((Path(self.engine.state_dir) / sb.triage_digest.SEEN_FILE).read_text())["rollup"])
        self.engine.triage.seen = self.engine.triage.meta = None      # a restart: the roll-up time comes back from the file
        self.assertEqual(self.run_pass([self.decision(7)], 3600)[1], [])   # still once in 24 hours

    def test_a_rollup_time_that_is_not_a_number_reads_as_none_sent(self):
        d = str(self.world.state)
        sb.triage_digest.write_json(os.path.join(d, sb.triage_digest.SEEN_FILE), {"v": 1, "items": {}, "swept": True, "last": None, "rollup": "x"})
        self.assertEqual(sb.triage_digest.load_meta(d), {"known": True, "swept": True, "last": None})


class TestAlertColdStart(Base):
    """#1140: after a start the alert repositories' `issues` lists are read before every other unit; six repositories, one alert, four reads a pass."""
    REPOS = ["acme-org/r%d" % i for i in range(1, 7)]
    ALERT = "acme-org/r6"   # the last by name: the old order reads it last

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=(), config={"github_repos": list(self.REPOS)})
        self.world.add_agent("orch", fleet={"role": "orchestrator", "charter": 1, "repo": self.ALERT})
        self.hub = FakeTriageHub()
        self.engine = self.world.engine
        self.engine.hub = self.hub
        self.engine.config["triage"] = dict(sb.TRIAGE_DEFAULTS, repos=list(self.REPOS), our_logins=["bot"], calls_per_pass=6,
                                            alert_labels={self.ALERT: ["ops-alert"]})
        self.engine.discover(self.world.clock.now())
        for repo in self.REPOS:
            self.hub.open[repo] = {}
        self.hub.open[self.ALERT][7] = tri_issue(self.world.clock.now(), 7, labels=["ops-alert"], title="Nightly job failed", repo=self.ALERT)

    def passes(self, n=1):
        self.hub.calls.clear()
        for _ in range(n):
            self.world.tick(1)
            sb.triage_pass(self.engine, [], self.world.clock.now(), ())

    def sent(self):
        return [e for e in self.world.audit() if e["event"] == "alert_wake.sent"]

    def test_the_first_pass_reads_the_alert_issues_and_the_wake_sends(self):
        self.passes()
        self.assertEqual(len([c for c in self.hub.calls if "per_page" in c]), 4)   # the budget holds: four reads, never five
        self.assertTrue(sb.whole(self.engine, "issues:" + self.ALERT)[0])
        self.assertEqual(len(self.sent()), 1)

    def test_with_no_alert_labels_the_old_order_is_kept(self):
        self.engine.config["triage"]["alert_labels"] = {}
        self.passes()
        self.assertFalse(sb.whole(self.engine, "issues:" + self.ALERT)[0])
        self.assertEqual(self.sent(), [])

    def test_the_skip_line_says_how_far_the_start_has_got(self):
        """The counts come from a real pass: the alert repository is unreadable, so its issues input stays unread and the wake says so."""
        self.hub.unreadable.add(self.ALERT)
        self.passes()
        read = len([c for c in self.hub.calls if self.ALERT not in c and "per_page" in c])   # the successful reads, counted from the hub
        wanted = len(self.REPOS) * (3 + len(sb.triage_awaiting_labels(self.engine)))
        self.assertGreater(read, 0)
        lines = [e["detail"]["why"] for e in self.world.audit() if e["event"] == "whole.skipped" and e["detail"]["input"] == "issues:" + self.ALERT]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("(%d of %d units read)" % (read, wanted)), (lines, read, wanted))

    def test_a_failed_refresh_does_not_count_as_read(self):
        self.engine.triage = sb.TriageState()
        self.engine.triage.wanted = 30
        self.engine.triage.units[(self.REPOS[0], "issues")] = {"at": 0, "data": [], "truncated": False, "error": None}
        self.engine.triage.units[(self.REPOS[1], "issues")] = {"at": 0, "data": [], "truncated": False, "error": "HTTP 502"}
        sb.read_mark(self.engine, "issues:" + self.ALERT, False, "issues not read yet", self.world.clock.now())
        sb.whole_skipped(self.engine, "alert_wake", "issues:" + self.ALERT)
        lines = [e["detail"]["why"] for e in self.world.audit() if e["event"] == "whole.skipped"]
        self.assertTrue(any(l.endswith("(1 of 30 units read)") for l in lines), lines)

    def test_a_name_that_is_not_an_issues_input_and_no_wanted_keep_the_plain_line(self):
        self.engine.triage = sb.TriageState()
        self.engine.triage.units[(self.REPOS[0], "issues")] = {"at": 0, "data": [], "truncated": False, "error": None}
        sb.read_mark(self.engine, "agents", False, "agents not read yet", self.world.clock.now())
        self.engine.triage.wanted = 30
        sb.whole_skipped(self.engine, "x", "agents")
        sb.read_mark(self.engine, "issues:" + self.ALERT, False, "issues not read yet", self.world.clock.now())
        self.engine.triage.wanted = 0
        sb.whole_skipped(self.engine, "y", "issues:" + self.ALERT)
        lines = sorted(e["detail"]["why"] for e in self.world.audit() if e["event"] == "whole.skipped")
        self.assertEqual(lines, ["agents not read yet", "issues not read yet"])


class TestAlertRefreshOrder(Base):
    """#1140: the order in which `triage_refresh` reads, driven for real against the fake hub. Units here: issues, comments, merged."""
    REPOS = ["acme-org/r%d" % i for i in range(1, 7)]
    UNITS = ["issues", "comments", "merged"]

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=(), config={"github_repos": list(self.REPOS)})
        self.engine = self.world.engine
        self.hub = FakeTriageHub()
        self.engine.hub = self.hub
        self.state = sb.TriageState()
        self.state.started = self.engine.now()
        for repo in self.REPOS:
            self.hub.open[repo] = {}

    def refresh(self, alerts, budget=100, now=None):
        self.engine.config["triage"] = {"alert_labels": {r: ["ops-alert"] for r in alerts}}
        self.hub.calls.clear()
        sb.triage_refresh(self.engine, self.state, self.REPOS, self.UNITS, now or self.world.clock.now(), [budget], 1)
        return [self.read_of(c) for c in self.hub.calls]

    @staticmethod
    def read_of(path):
        repo = re.search(r"repos/([^/]+/[^/]+)/", path).group(1)
        unit = "merged" if "/pulls" in path else "comments" if "/issues/comments" in path else "issues"
        return repo, unit

    def old_order(self, skip=()):
        return sorted(((r, u) for r in self.REPOS for u in self.UNITS if (r, u) not in skip), key=lambda t: (0,) + t)

    def test_two_alert_repositories_are_read_first_and_the_rest_keep_the_old_order(self):
        order = self.refresh(["acme-org/r5", "acme-org/r6"])
        first = [("acme-org/r5", "issues"), ("acme-org/r6", "issues")]
        self.assertEqual(order[:2], first)
        self.assertEqual(order[2:], self.old_order(skip=first))   # no other unit waits for more than those two lists, none is dropped

    def test_no_alert_labels_reads_in_the_old_order(self):
        self.assertEqual(self.refresh([]), self.old_order())

    def test_fresh_alert_units_change_nothing(self):
        now = self.world.clock.now()
        for r in ("acme-org/r5", "acme-org/r6"):
            self.state.units[(r, "issues")] = {"at": now, "data": [], "truncated": False, "error": None}
        fresh = [("acme-org/r5", "issues"), ("acme-org/r6", "issues")]
        self.assertEqual(self.refresh(["acme-org/r5", "acme-org/r6"]), self.old_order(skip=fresh))

    def test_a_stale_but_read_alert_unit_keeps_its_place_in_the_old_order(self):
        now = self.world.clock.now()
        self.state.units[("acme-org/r6", "issues")] = {"at": now - 10000, "data": [], "truncated": False, "error": None}
        order = self.refresh(["acme-org/r6"])
        self.assertNotEqual(order[0], ("acme-org/r6", "issues"))   # a read-but-stale unit is not "not read yet"
        self.assertEqual(order[-1], ("acme-org/r6", "issues"))   # it has at > 0, so the unread units sort before it

    def test_an_unreadable_alert_repository_does_not_stop_the_others(self):
        self.hub.unreadable.add("acme-org/r6")
        order = self.refresh(["acme-org/r6"])
        self.assertEqual(order[0], ("acme-org/r6", "issues"))
        self.assertIn("acme-org/r6", self.state.unreadable)
        for repo in self.REPOS[:5]:
            for unit in self.UNITS:
                self.assertIn((repo, unit), self.state.units)

    def test_a_budget_smaller_than_the_alert_lists_reads_only_alert_lists_in_order(self):
        order = self.refresh(["acme-org/r5", "acme-org/r6"], budget=2)   # one kept in reserve: one read
        self.assertEqual(order, [("acme-org/r5", "issues")])
        self.state.units.clear()
        order = self.refresh(["acme-org/r5", "acme-org/r6"], budget=3)
        self.assertEqual(order, [("acme-org/r5", "issues"), ("acme-org/r6", "issues")])


class TestInstallStatusUsesTheInstalledPlatform(Base):
    def test_service_report_takes_the_platform(self):
        self.assertEqual(sb.service_report("darwin")["kind"], "launchd")
        self.assertEqual(sb.service_report("linux")["kind"], "systemd")
        self.assertIsNone(sb.service_report("plan9")["kind"])
        self.assertEqual(sb.service_report()["kind"], sb.service_kind())

    def test_collect_status_passes_it_on(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        world = World(self, agents=())
        report = sb.collect_status(world.route, world.clock.now, platform="linux")
        self.assertEqual(report["service"]["kind"], "systemd")


class TestReloadInPlace(Base):
    def test_it_stops_the_forwarders_and_the_judge_says_so_and_starts_over(self):
        rig = GithubRig(self)
        rig.pump(10)
        proc = rig.gh.forwarder()
        self.assertIsNotNone(proc)
        stopped = []

        class Judge:
            def stop(self):
                stopped.append("judge")
        watch = mock.Mock(changed=["github_repos"])
        executed = []
        sb.reload_in_place(rig.world.engine, rig.hub, Judge(), watch, ["/py", "/bin/fleet-switchboard", "run"],
                           executed.append)
        self.assertEqual(executed, [["/py", "/bin/fleet-switchboard", "run"]])
        self.assertIsNotNone(proc.poll(), "its forwarder was stopped before the exec")
        self.assertEqual(stopped, ["judge"])
        [event] = [e for e in rig.world.audit() if e["event"] == "config.reloaded"]
        self.assertEqual(event["detail"]["changed"], ["github_repos"])
        self.assertEqual(event["detail"]["forwarders_stopped"], [GH_REPO])

    def test_an_exec_that_fails_leaves_the_daemon_running_on_what_it_had(self):
        rig = GithubRig(self)
        rig.pump(10)
        rig.hub.autostart = False
        started = []
        rig.hub.start = lambda: started.append("hub")

        def broken(argv):
            raise OSError("no such interpreter")
        sb.reload_in_place(rig.world.engine, rig.hub, None, mock.Mock(changed=["x"]), ["/nope"], broken)
        self.assertEqual(started, ["hub"])
        self.assertEqual(len([e for e in rig.world.audit() if e["event"] == "config.reload_failed"]), 1)

    def test_cmd_run_wires_the_watch_and_a_re_exec_without_standby(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        seen = []
        world = World(self, agents=())
        with mock.patch.object(sb, "run_daemon", lambda *a, **k: seen.append(k)), \
                mock.patch.object(sb, "start_judge_server", lambda *a, **k: None), captured():
            sb.main(["run", "--standby", "--poll", "7"], runner=world.route, now=world.clock.now)
        self.assertIsInstance(seen[0]["watch"], sb.ConfigWatch)
        self.assertIn("config_loaded_at", seen[0]["record"])
        with mock.patch.object(sb, "reload_in_place") as reload:
            seen[0]["on_reload"](seen[0]["watch"])
        argv = reload.call_args[0][4]
        self.assertEqual(argv[2:], ["run", "--poll", "7"])
        self.assertNotIn("--standby", argv)


class TestDeployFromMainSentence(unittest.TestCase):
    SENTENCE = ("Production is deployed from the head of main only: never from a side branch, a cherry-pick or a revert off main; "
                "when main is not shippable, the fix goes into main (the owner's standing order).")

    def test_s2_the_coordination_skill_states_the_deploy_rule_once(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertEqual(text.count(self.SENTENCE), 1)

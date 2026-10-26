"""Test classes moved out of bin/test-switchboard (text unchanged): cases_01."""
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
from support.core import Base, SAFE_PATH
from support.fake_jev import FAKE_SB_SOURCE, GIT_SHIM, SHIM, SHIM_LOOKUP_SECONDS, write_fake_env
from support.installs_and_keys import InstallCase, PR16_KEY, PR16_NOTICE, tree_of
from support.loader import captured, sb
from support.paths import BIN, REPO
from support.policy_and_import import TRIAL
from support.service_and_merge import feed_line


class _StubJev:
    """A judge that answers from a table: {option: probability} for the options the question offers (the rest 0); counts its calls."""

    def __init__(self, picks=None, error=None, junk=None):
        self.picks, self.error, self.junk, self.calls = picks or {}, error, junk, []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        criteria = questions["clears"]["criteria"]
        if self.junk is not None:
            return {"model": "m", "answers": {"clears": self.junk}, "usage": {}, "latency_ms": 1}
        probabilities = {option: self.picks.get(option, 0.0) for option in criteria}
        probabilities.setdefault("nothing", 0.0)
        total = sum(probabilities.values()) or 1.0
        probabilities = {k: v / total for k, v in probabilities.items()}
        top = max(probabilities, key=lambda k: probabilities[k])
        return {"model": "m", "answers": {"clears": {"type": "choice", "choice": top, "probabilities": probabilities}}, "usage": {}, "latency_ms": 7}


class TestChatClearJudge(unittest.TestCase):
    """#457 slice 2: the judge's one question and the decision. Fixtures only; nothing is cleared by this slice."""

    P366, P389, P400 = "acme/widget#366", "acme/widget#389", "acme/widget#400"

    def setUp(self):
        self.cc = sb.load_module("chat_clear")
        self.offers = {pr: {"head": "%08x" % (n * 4097) + "0" * 32, "shown": True, "shown_at": 1000, "labels": ["u:hold-impact"], "summary": "touches the gate %d" % n}
                       for pr, n in ((self.P366, 366), (self.P389, 389), (self.P400, 400))}

    def decide(self, text, jev, offers=None, created=1001, now=1002, **kw):
        return self.cc.decide(jev, text, self.offers if offers is None else offers, created, now, **kw)

    def test_a_confident_judge_that_picks_the_one_candidate_clears_and_one_call_is_made_with_the_offers(self):
        jev = _StubJev({self.P366: 0.97, "several": 0.01, "asks": 0.01, "nothing": 0.01})
        got = self.decide("366 is good to go", jev)
        self.assertEqual((got["action"], got["pr"], got["why"]), ("clear", self.P366, "confirmed"))
        self.assertEqual(len(jev.calls), 1)
        state, questions = jev.calls[0]
        self.assertEqual(state["message"], "366 is good to go")
        self.assertEqual([h["pr"] for h in state["held"]], [self.P366, self.P389, self.P400])
        self.assertEqual(list(questions["clears"]["criteria"]), [self.P366, self.P389, self.P400, "several", "asks", "nothing"])

    def test_the_judge_never_widens_a_wrong_pr_a_number_less_message_or_a_hedge_is_a_question_whatever_it_says(self):
        for text, why in (("the launchd one is good to go", "no_number"), ("366 is not good to go", "hedged"), ("is 366 good to go?", "hedged"),
                          ("366 is good to go once CI passes", "hedged"), ("366 and 389 are good to go", "several_numbers"), ("500 is good to go", "not_offered")):
            jev = _StubJev({self.P366: 0.99})   # a judge that would clear 366 at 0.99
            got = self.decide(text, jev)
            self.assertEqual((got["action"], got["why"]), ("ask", why), text)
            self.assertEqual(len(jev.calls), 1, text)
        jev = _StubJev({self.P389: 0.99})
        got = self.decide("366 is good to go", jev)
        self.assertEqual((got["action"], got["why"]), ("ask", "jev"), "the judge picks ANOTHER offer: not a confirmation")
        for choice in ("several", "asks"):
            got = self.decide("366 is good to go", _StubJev({choice: 0.99}))
            self.assertEqual(got["action"], "ask", choice)

    def test_a_message_from_before_the_offer_a_cut_message_and_an_old_offer_are_asked_about_never_cleared(self):
        jev = _StubJev({self.P366: 0.99})
        for kw, why in ((dict(created=1000), "before_offer"), (dict(created=999), "before_offer"), (dict(now=1000 + self.cc.OFFER_TTL + 1), "expired"), (dict(created=None), "no_time")):
            got = self.decide("366 is good to go", jev, **kw)
            self.assertEqual((got["action"], got["why"]), ("ask", why), kw)
            self.assertIn("Nothing was cleared", got["question"])
        got = self.decide("366 is good to go" + " " * self.cc.TEXT_MAX, jev)
        self.assertEqual((got["action"], got["why"]), ("ask", "too_long"))
        self.assertEqual(self.decide("366 is good to go", jev)["action"], "clear", "the same message, in time, clears")

    def test_the_floor_is_an_allow_list_curly_apostrophes_exceptions_and_times_are_never_a_plain_clearing_sentence(self):
        jev = _StubJev({self.P366: 0.99})
        for text in ("366 isn\u2019t good to go", "366 isn't good to go", "366 don\u2019t clear", "366 won\u2019t be ready, but good to go", "366 is good to go except the docs",
                     "366 is not yet good to go", "366 is good to go only after review", "366 is good to go without the tests", "366 is pending, good to go later",
                     "366 is good to go tomorrow", "366 first then good to go", "366 is almost good to go", "366 is good to go soon", "366 is good to go while CI runs",
                     "366 is still good to go", "366 good to go lol", "please do not clear 366 good to go", "366 is good to go \u2014 wait", "366 is good to go\nand 5 more words"):
            got = self.decide(text, jev)
            self.assertEqual((got["action"], got["why"]), ("ask", "hedged" if "\n" not in text else "several_numbers"), text)
        self.assertEqual(self.decide("nope, 366", jev)["action"], "none", "a refusal with no clearing words in it is not about clearing: nothing is asked, nothing cleared")
        for text in ("366 is good to go", "366 is good to go.", "#366 is good to go", "366 good to go", "ok to merge 366, thanks", "366 looks good, go ahead", "Please ship 366", "366 is cleared"):
            self.assertEqual(self.decide(text, jev)["action"], "clear", text)

    def test_a_report_about_the_past_is_not_a_grant_only_a_present_state_or_an_instruction_clears(self):
        jev = _StubJev({self.P366: 0.99})
        past = ("366 was merged", "366 was good to go", "366 shipped", "366 released", "366 merged", "366 cleared yesterday", "366 was cleared", "366 were cleared", "366 has been cleared",
                "366 had been approved", "366 already cleared", "366 got merged", "366 is done", "366 went out", "366 was good, go ahead", "cleared 366", "approved 366",
                "last 366 good to go", "earlier 366 is good to go", "366 good to go yesterday", "366 is already good to go", "366 has been good to go", "366 was approved",
                "I cleared 366", "I approved 366", "we shipped 366", "366 was released", "366 released yesterday", "366 would be good to go", "366 had good to go",
                "366. I cleared it", "366 I cleared it", "366 you approved it", "366 I approved", "366 you cleared it", "366 cleared it", "366 approved it", "please cleared 366",
                "366 good to go cleared", "366 also cleared it", "366 is cleared it", "366 cleared, I think", "366 is approved by you", "thanks for the 366 approved", "thank you for 366 cleared", "you 366 approved", "thanks 366 cleared", "for 366 cleared", "then 366 cleared", "now 366 approved", "ok 366 cleared", "lgtm 366 approved", "a pr 366 approved", "ok 366 is approved, thanks", "366 thanks for the release", "366 thank you for the merge", "366 thanks for the approve", "366 good to go, thanks for the clear", "366 thanks ship", "366 thank you merge")
        for text in past:
            got = self.decide(text, jev)
            self.assertNotEqual(got["action"], "clear", text)
            if self.cc.CLEARISH.search(text):
                self.assertEqual((got["action"], got["why"]), ("ask", "hedged"), text)
        for text in ("366 is cleared", "366 cleared", "clear 366", "366 is approved", "366 approved", "approve 366", "ship 366", "release 366", "merge 366", "366 is good to go",
                     "366 also good to go", "#366 is cleared.", "366 cleared, thanks", "366 is approved, thank you", "ok to merge 366, thanks", "ship 366, thank you", "please clear 366 thanks", "366 also approved", "ok to merge 366", "go ahead ship 366", "please clear 366"):
            self.assertEqual(self.decide(text, jev)["action"], "clear", text)
        for word in ("i", "was", "were", "had", "has", "have", "been", "got", "already", "merged", "shipped", "released", "done", "went", "yesterday", "earlier", "last"):
            self.assertNotIn(word, self.cc.PLAIN_WORDS, word)

    def test_a_bad_nan_boolean_or_lower_bar_is_the_floor_and_the_bar_can_only_be_raised(self):
        for bar in (None, "x", [], float("nan"), True, 2, -1, 0, 0.1, 0.9, float("inf")):
            jev = _StubJev({self.P366: 0.85, "asks": 0.15})
            self.assertEqual(self.decide("366 is good to go", jev, min_confidence=bar)["action"], "ask", repr(bar))
            self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.95, "asks": 0.05}), min_confidence=bar)["action"], "clear", repr(bar))
        self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.95, "asks": 0.05}), min_confidence=0.97)["action"], "ask")

    def test_an_answer_whose_choice_is_not_an_option_not_the_most_probable_tied_or_not_numbers_confirms_nothing(self):
        for junk in ({"type": "choice", "choice": "acme/widget#999", "probabilities": {"acme/widget#999": 0.99}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95, self.P389: 0.97}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95, self.P389: 0.95}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: True}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: 1.5}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95, "x": "y"}},
                     {"type": "choice", "choice": self.P366, "probabilities": {self.P366: float("nan")}},
                     {"type": "choice", "choice": ["list"], "probabilities": {self.P366: 0.95}}, {"type": "choice", "choice": None, "probabilities": {}}):
            got = self.decide("366 is good to go", _StubJev(junk=junk))
            self.assertEqual((got["action"], got["why"]), ("ask", "jev"), junk)
            self.assertFalse(got["judge"]["confident"], junk)

    def test_every_reason_screen_can_give_has_a_sentence_and_nothing_that_looks_like_a_secret_reaches_the_judge_or_the_question(self):
        reasons = set()
        offers = dict(self.offers)
        twice = {self.P366: self.offers[self.P366], "acme/other#366": dict(self.offers[self.P366])}
        for text, o, created, now in (("x", offers, 1001, 1002), ("366 and 389", offers, 1001, 1002), ("500 good to go", offers, 1001, 1002), ("abc366 good to go", offers, 1001, 1002),
                                      ("366 is not good", offers, 1001, 1002), ("9" * self.cc.TEXT_MAX, offers, 1001, 1002), ("366 good to go", twice, 1001, 1002),
                                      ("366 good to go", offers, 1000, 1002), ("366 good to go", offers, 1001, 1000 + self.cc.OFFER_TTL + 5), ("366 good to go", offers, None, 1002)):
            reasons.add(self.cc.screen(text, o, created, now)["why"])
        reasons.discard(None)
        self.assertEqual(reasons - set(self.cc.REASONS), set(), "a reason without a sentence would reach the owner as a raw code")
        token = "ghp_" + "A1b2C3d4" * 4
        secret_offers = {self.P366: dict(self.offers[self.P366], summary="touches %s and Bearer abcdefghijklmnop0123456789 and %s" % (token, "f" * 40))}
        jev = _StubJev({self.P366: 0.99})
        got = self.decide("366 is good to go", jev, offers=secret_offers)
        self.assertEqual(got["action"], "clear")
        self.assertNotIn(token, json.dumps(jev.calls) + json.dumps(got["judge"]))
        self.assertNotIn("f" * 40, json.dumps(jev.calls))
        self.assertIn("[secret]", json.dumps(jev.calls))
        stub = _StubJev({self.P366: 0.99})
        asked = self.decide("366 is good to go once %s" % token, stub, offers=secret_offers)
        self.assertEqual(asked["action"], "ask")
        self.assertEqual(len(stub.calls), 1, "the judge is still asked, and what it is sent holds no secret")
        sent = stub.calls[0][0]["message"]
        self.assertIn("[secret]", sent)
        self.assertNotIn(token, json.dumps(stub.calls))
        self.assertNotIn(token, asked["question"])
        self.assertNotIn("abcdefghijklmnop0123456789", asked["question"])
        shapes = ("github_pat_" + "a1B2c3D4" * 3, "sk-" + "a1B2c3D4" * 3, "xoxb-" + "1234567890" * 2, "AKIA" + "ABCDEFGH0123", "gho_" + "A1b2C3d4" * 3, "G" * 20 + "H" * 20 + "+/==",
                  "f" * 40, "Bearer " + "x" * 20)
        for shape in shapes:
            self.assertEqual(self.cc._clean("see %s here" % shape, 200), "see [secret] here", shape)
        for near in ("a" * 39 + " b", "ghp_short", "sk-short", "AKIA123", "a-normal-path/with/words and-numbers-12345"):
            self.assertEqual(self.cc._clean(near, 200), near, "a near miss is kept: %s" % near)

    def test_a_question_without_a_mark_a_symbol_and_a_sum_over_one_never_reach_a_clear(self):
        jev = _StubJev({self.P366: 0.99})
        for text in ("is 366 good to go", "was 366 approved", "is 366 ok to merge", "are 366 and ... good to go", "be 366 good to go", "366 good to go \u274c", "366 good to go /s",
                     "366 good to go :)", "366 good to go - maybe", "366 good to go (not really)", "366 good to go; wait", "366 \U0001F44D good to go", "366 is good to go\u2026"):
            got = self.decide(text, jev)
            self.assertEqual(got["action"], "ask", text)
            self.assertIn(got["why"], ("hedged", "no_number"), text)
        for text in ("366, is it good to go", "366 was it ok to merge", "366 is it good to go", "ok to merge 366 is it", "366 good to go, are you ok", "366 good to go be ok",
                     "366 good to go \uff15", "366 \u0663 good to go"):
            got = self.decide(text, jev)
            self.assertEqual((got["action"], got["why"]), ("ask", "hedged"), text)
        for text in ("366 is good to go", "366 is good to go!", "ok to merge #366, thanks.", "Ship 366 now", "366 is approved", "366 good to go"):
            self.assertEqual(self.decide(text, jev)["action"], "clear", text)
        self.assertEqual(self.decide(None, jev)["action"], "none", "a non-string message is read as empty: no clearing words, nothing")
        # a judge answer that is not validated can never say "not about clearing": the owner is still asked
        for junk in ({"type": "choice", "choice": "nothing", "probabilities": {"nothing": 5}}, {"type": "choice", "choice": "nothing", "probabilities": {"nothing": 0.9, "asks": 0.9}},
                     {"type": "choice", "choice": "nothing", "probabilities": {"nothing": 0.5, "asks": 0.5}}, {"type": "choice", "choice": "nothing", "probabilities": {"nothing": 0.9, "asks": 0.2, "x": -0.1}}):
            got = self.decide("all good to go", _StubJev(junk=junk))
            self.assertEqual(got["action"], "ask", junk)
            self.assertIs(got["judge"]["validated"], False, "an answer that failed a check can never say 'not about clearing'")
        got = self.decide("all good to go", _StubJev({"nothing": 0.9, "asks": 0.1}))
        self.assertEqual(got["action"], "none", "a validated answer that says nothing stays quiet")
        under = self.decide("366 is good to go", _StubJev(junk={"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95}}))
        self.assertEqual((under["action"], under["why"]), ("ask", "jev"), "probabilities that add up to 0.95 are not an answer")
        extra = self.decide("366 is good to go", _StubJev(junk={"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95, "asks": 0.05, "bogus": 0.0}}))
        self.assertEqual(extra["action"], "clear")
        self.assertNotIn("bogus", extra["judge"]["probabilities"], "only offered options are kept")
        for junk in ({"type": "choice", "choice": self.P366, "probabilities": {self.P366: 0.95, self.P389: 0.9}},
                     {"type": "choice", "choice": "nothing", "probabilities": ["x"]}, {"type": "choice", "choice": "nothing", "probabilities": {"nothing": "y"}},
                     {"type": "choice", "choice": "nothing", "probabilities": {"nothing": 0.9, 5: 0.1}}, {"type": "choice", "choice": 5, "probabilities": {"nothing": 1.0}}):
            for text in ("366 is good to go", "all good to go, thanks"):
                got = self.decide(text, _StubJev(junk=junk))   # the decision never raises on a malformed answer
                self.assertEqual(got["action"], "ask", (text, junk))

    def test_all_impacts_cleared_asks_once_and_names_all_three_holds(self):
        jev = _StubJev({"several": 0.95})
        got = self.decide("all impacts cleared", jev)
        self.assertEqual(len(jev.calls), 1, "one judge call, one question")
        self.assertEqual((got["action"], got["why"]), ("ask", "no_number"))
        for pr in (self.P366, self.P389, self.P400):
            self.assertIn(pr, got["question"])
        self.assertEqual(got["shown"], [self.P366, self.P389, self.P400])
        self.assertEqual(got["question"].count("Nothing was cleared"), 1)

    def test_an_unreachable_unsure_or_unreadable_judge_confirms_nothing_and_asks(self):
        class Down(Exception):
            reason = "unreachable"
        for jev, fragment in ((_StubJev(error=Down("secret-token-in-text")), "jev unreachable"), (_StubJev({self.P366: 0.60, self.P389: 0.40}), "unsure"),
                              (_StubJev(junk={"type": "choice", "choice": "x"}), "no probabilities"), (_StubJev(junk="junk"), "unreadable"), (None, "no judge")):
            got = self.decide("366 is good to go", jev)
            self.assertEqual((got["action"], got["why"]), ("ask", "jev"), fragment)
            self.assertIn(fragment, got["judge"]["reason"])
            self.assertFalse(got["judge"]["confident"])
            self.assertNotIn("secret-token", json.dumps(got["judge"]), "an error's text is never kept")
        self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.89, "asks": 0.11}))["action"], "ask", "0.89 is below the bar")
        self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.95, "asks": 0.05}), min_confidence=0.99)["action"], "ask", "a higher configured bar raises it")
        self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.91, "asks": 0.09}), min_confidence=0.1)["action"], "clear", "a lower configured bar never lowers it below 0.9... and 0.91 passes")
        self.assertEqual(self.decide("366 is good to go", _StubJev({self.P366: 0.85, "asks": 0.15}), min_confidence=0.1)["action"], "ask")

    def test_chat_that_is_not_about_clearing_costs_no_judge_call_and_no_question_and_nothing_held_means_nothing(self):
        jev = _StubJev({self.P366: 0.99})
        for text in ("what is the weather", "thanks, 366 looks interesting", ""):
            got = self.decide(text, jev)
            self.assertEqual((got["action"], got["question"]), ("none", None), text)
        self.assertEqual(jev.calls, [], "no clearing words: the judge was not called")
        got = self.decide("366 is good to go", jev, offers={})
        self.assertEqual((got["action"], got["why"]), ("none", "no_offers"))
        self.assertEqual(jev.calls, [])
        got = self.decide("all good to go, thanks", _StubJev({"nothing": 0.9}))
        self.assertEqual((got["action"], got["why"]), ("none", "not about clearing"), "the judge says it is not about clearing: the owner is not asked")
        got = self.decide("366 is good to go", _StubJev({"nothing": 0.95}))
        self.assertEqual(got["action"], "none")

    def test_the_question_names_the_new_head_cleans_control_characters_and_is_bounded(self):
        offers = {"acme/widget#%d" % n: {"head": "a" * 40, "summary": "s\x1b[31m%d\n" % n} for n in range(30)}
        text = self.cc.question_text("hedged", offers)
        self.assertNotIn("\x1b", text)
        self.assertEqual(text.count("\n- "), self.cc.QUESTION_OFFERS_MAX)
        self.assertIn("and 20 more", text)
        one = self.cc.question_text("jev", offers, ["acme/widget#3"], {"acme/widget#3": "b" * 40})
        self.assertIn("it has a new head, bbbbbbbb, since you were shown this one", one)
        self.assertIn("`3 is good to go`", one)

    def test_the_real_replay_judge_reads_todays_three_messages_and_the_state_names_no_secret(self):
        rules = [{"contains": "all impacts cleared", "choice": {"several": 0.95, "asks": 0.05}},
                 {"contains": "389 also", "choice": {self.P389: 0.96, "nothing": 0.04}},
                 {"contains": "366 is good", "choice": {self.P366: 0.97, "asks": 0.03}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replay.json"
            path.write_text(json.dumps({"rules": rules}))
            jev = sb.ReplayJev(str(path), "m")
            got = {text: self.decide(text, jev) for text in ("366 is good to go", "389 also good to go", "all impacts cleared")}
        self.assertEqual([(g["action"], g["pr"]) for g in got.values()], [("clear", self.P366), ("clear", self.P389), ("ask", None)])
        self.assertEqual(got["all impacts cleared"]["judge"]["jev_choice"], "several")


class TestGitShim(Base):
    """The `git` shim: the network verbs get the fleet's credential helper, everything else runs the real git unchanged. A fake real git records what it got."""

    RESET = "credential.https://github.com.helper="
    HELPER = "credential.https://github.com.helper=!gh auth git-credential"

    def setUp(self):
        super().setUp()
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        self.log = self.home / "git-log.json"
        self.starts = self.home / "git-starts"
        fake = self.bin / "git"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        "open(%r,'a').write('x')\n"
                        "json.dump({'argv': sys.argv[1:], 'stdin': None if sys.stdin.isatty() else sys.stdin.read(), 'token_env': os.environ.get('GH_TOKEN'), 'env': {k: os.environ.get(k) for k in ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE')}}, open(%r,'w'))\n"
                        "print('real git out'); sys.exit(int(os.environ.get('FAKE_GIT_EXIT','0')))\n" % (str(self.starts), str(self.log)))
        fake.chmod(0o755)
        self.write_users({"acme/*": "acct-acme"})

    def write_users(self, users):
        config = self.home / "xdg-config" / "fleet-switchboard"
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.json").write_text(json.dumps({"gh_users": users} if users is not None else {}))

    def run_git(self, *args, stdin="", env=None, path=None):
        environ = {"PATH": path or "%s:%s:%s" % (GIT_SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "xdg-config")}
        environ.update(env or {})
        for p in (self.log, self.starts):
            if p.exists():
                p.unlink()
        done = subprocess.run([sys.executable, str(GIT_SHIM)] + list(args), input=stdin, capture_output=True, text=True, env=environ, timeout=60)
        seen = json.loads(self.log.read_text()) if self.log.exists() else None
        if seen is not None:   # #1050: every call carries the no-dialog pair; the older tests below read the arguments without it (the new tests read `no_dialog_env` and check the pair)
            argv = seen["argv"]
            pair = [i for i in range(len(argv) - 1) if argv[i] == "-c" and argv[i + 1] == "credential.interactive=false"]
            self.assertTrue(pair, "no -c credential.interactive=false in %r" % (argv,))
            seen["argv_with_pair"] = list(argv)
            del argv[pair[0]:pair[0] + 2]
        return done, seen

    def routed(self, *globals_, verb, rest=()):
        return list(globals_) + ["-c", self.RESET, "-c", self.HELPER, verb] + list(rest)

    def fake_switchboard(self, read_repo):
        sb_bin = self.home / "sb"
        answer = {"name": "w", "repo": "acme-org/widget-service", "own": True}
        if read_repo != "absent":
            answer["read_repo"] = read_repo
        sb_bin.write_text("#!/bin/sh\n[ \"$1\" = whoami ] && echo '%s' && exit 0\nexit 2\n" % json.dumps(answer))
        sb_bin.chmod(0o755)
        return {"FLEET_SWITCHBOARD_BIN": str(sb_bin)}

    def test_a_push_by_a_worker_with_a_read_repo_is_refused_when_it_targets_that_repository_or_cannot_be_read(self):
        env = self.fake_switchboard("acme-org/widget-api")
        for target in ("https://github.com/acme-org/widget-api.git", "git@github.com:Acme-Org/Widget-Api.git", "https://github.com/acme-org/widget-api",
                       "file:///tmp/x", "https://evil.example/acme-org/widget-service"):
            with self.subTest(target=target):
                done, seen = self.run_git("push", target, "main", env=env)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIsNone(seen)
        for pushed in (("-o", "x", "https://github.com/acme-org/widget-api.git"), ("--push-option", "x", "https://github.com/acme-org/widget-api.git"),
                       ("--repo=https://github.com/acme-org/widget-api.git",), ("-u", "https://github.com/acme-org/widget-api.git", "main"),
                       ("--receive-pack=x", "git@github.com:acme-org/widget-api.git"), ("-o", "x", "file:///tmp/x")):
            with self.subTest(pushed=pushed):
                done, seen = self.run_git("push", *pushed, env=env)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIsNone(seen)
        done, seen = self.run_git("-C", "/w", "push", "https://github.com/acme-org/widget-api.git", env=env)
        self.assertEqual((done.returncode, seen), (1, None))
        done, seen = self.run_git("push", "https://github.com/acme-org/widget-service.git", "main", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNotNone(seen)
        for state in ("absent", "Bad/Record", False):
            done, seen = self.run_git("push", "https://github.com/acme-org/widget-api.git", env=self.fake_switchboard(state))
            self.assertEqual(done.returncode == 1, state != "absent", (state, done.stderr))
        done, seen = self.run_git("fetch", "https://github.com/acme-org/widget-api.git", env=env)
        self.assertEqual(done.returncode, 0, "a fetch (a read) of the read repository is not a push")

    def test_s1_every_network_verb_gets_the_reset_then_the_fleets_helper_right_before_the_verb(self):
        cases = (("push", ["origin", "branch"]), ("fetch", ["--all"]), ("pull", ["--ff-only"]), ("clone", ["https://github.com/acme/api.git", "x"]),
                 ("ls-remote", ["origin"]))
        for verb, rest in cases:
            for globals_ in ([], ["-C", "/work/tree"], ["-c", "user.name=x", "--no-pager"], ["--git-dir=/g", "-C", "/w", "-p"]):
                with self.subTest(verb=verb, globals=globals_):
                    done, seen = self.run_git(*globals_, verb, *rest)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(seen["argv"], self.routed(*globals_, verb=verb, rest=rest))
        done, seen = self.run_git("remote", "update", "--prune")
        self.assertEqual(seen["argv"], self.routed(verb="remote", rest=["update", "--prune"]))
        done, seen = self.run_git("-C", "/w", "remote", "-v", "update")
        self.assertEqual(seen["argv"], self.routed("-C", "/w", verb="remote", rest=["-v", "update"]))

    def test_s2_every_other_command_runs_the_real_git_with_the_same_arguments_input_and_exit_status(self):
        for args in (("status", "-sb"), ("log", "--oneline", "-5"), ("diff", "--stat"), ("commit", "-m", "x"), ("remote", "add", "o", "u"), ("remote", "-v"),
                     ("config", "credential.helper"), ("stash", "list"), ("-C", "/w", "checkout", "-b", "x"), ("--version",), (), ("push-like",)):
            with self.subTest(args=args):
                done, seen = self.run_git(*args, stdin="input text", env={"FAKE_GIT_EXIT": "3"})
                self.assertEqual(done.returncode, 3)
                self.assertEqual(seen["argv"], list(args))
                self.assertEqual(seen["stdin"], "input text")
                self.assertEqual(done.stdout, "real git out\n")
        done, seen = self.run_git("push", "origin", stdin="in", env={"FAKE_GIT_EXIT": "5"})
        self.assertEqual((done.returncode, seen["stdin"]), (5, "in"), "a network verb keeps its input and exit status too")

    def test_s3_a_callers_own_credential_configuration_is_left_alone(self):
        for args in (("-c", "credential.helper=store", "push", "origin"), ("-c", "credential.https://github.com.helper=!x", "fetch"),
                     ("--config-env", "credential.helper=SOME_ENV", "pull"), ("--config-env=credential.helper=SOME_ENV", "push"),
                     ("-c", "Credential.UseHttpPath=true", "push")):
            with self.subTest(args=args):
                done, seen = self.run_git(*args)
                self.assertEqual(seen["argv"], list(args))
        done, seen = self.run_git("-c", "user.name=x", "push")
        self.assertEqual(seen["argv"], self.routed("-c", "user.name=x", verb="push"), "a -c that is not a credential setting does not count")

    def test_s3_it_finds_the_real_git_behind_its_own_directory_and_never_runs_itself(self):
        twice = "%s:%s:%s:%s" % (GIT_SHIM.parent, GIT_SHIM.parent, self.bin, SAFE_PATH)
        done, seen = self.run_git("status", path=twice)
        self.assertEqual((done.returncode, seen["argv"], self.starts.read_text()), (0, ["status"], "x"), "the fake started once")
        done, seen = self.run_git("status", path=str(GIT_SHIM.parent))
        self.assertEqual((done.returncode, seen), (127, None))
        self.assertIn("no real git", done.stderr)
        link = self.home / "linkdir"
        link.mkdir()
        (link / "git").symlink_to(GIT_SHIM)
        done, seen = self.run_git("status", path="%s:%s" % (link, str(GIT_SHIM.parent)))
        self.assertEqual((done.returncode, seen), (127, None), "a symlink to the shim is the shim, not a real git")
        done, seen = self.run_git("status", path="%s:%s" % (GIT_SHIM.parent, SAFE_PATH), env={"FLEET_REAL_GIT": str(self.bin / "git")})
        self.assertEqual((done.returncode, seen["argv"]), (0, ["status"]), "FLEET_REAL_GIT names the real git")

    def test_a_parse_it_does_not_understand_and_a_real_git_that_cannot_start_never_block_the_command(self):
        for args in (("-c",), ("-C",), ("-c", "user.name=x")):   # a value option with no value, or no verb at all: nothing to route
            with self.subTest(args=args):
                done, seen = self.run_git(*args)
                self.assertEqual(seen["argv"], list(args))
        broken = self.home / "brokenbin"
        broken.mkdir()
        (broken / "git").write_text("#!/nonexistent/interpreter\n")
        (broken / "git").chmod(0o755)
        done, seen = self.run_git("status", path="%s:%s" % (GIT_SHIM.parent, broken))
        self.assertEqual((done.returncode, seen), (126, None))
        self.assertIn("git: cannot run", done.stderr)

    def test_it_routes_only_when_accounts_are_mapped_and_not_switched_off(self):
        for label, setup, env in (("no gh_users at all", lambda: self.write_users(None), {}), ("an empty map", lambda: self.write_users({}), {}),
                                  ("switched off", lambda: self.write_users({"acme/*": "a"}), {"FLEET_GIT_SHIM": "off"}),
                                  ("an unreadable config", lambda: (self.home / "xdg-config" / "fleet-switchboard" / "config.json").write_text("{bad"), {})):
            with self.subTest(label):
                setup()
                done, seen = self.run_git("push", "origin", env=env)
                self.assertEqual(seen["argv"], ["push", "origin"], "git keeps its own credentials")
        self.write_users({"acme/*": "a"})
        done, seen = self.run_git("push", "origin")
        self.assertEqual(seen["argv"], self.routed(verb="push", rest=["origin"]))
        for sub in ("show", "prune", "update"):
            done, seen = self.run_git("remote", sub, "origin")
            self.assertEqual(seen["argv"], self.routed(verb="remote", rest=[sub, "origin"]), sub)
        done, seen = self.run_git("remote", "add", "o", "u")
        self.assertEqual(seen["argv"], ["remote", "add", "o", "u"])

    def test_the_shim_reads_no_token_and_prints_nothing_of_its_own(self):
        done, seen = self.run_git("push", env={"GH_TOKEN": "tok-secret"})
        self.assertNotIn("tok-secret", " ".join(seen["argv"]) + done.stdout + done.stderr)
        self.assertEqual(done.stderr, "")

    def test_s5_the_fleet_coordination_skill_says_git_push_needs_no_manual_credential_route(self):
        skill = " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())
        self.assertIn("git push works as the repository's account", skill)
        self.assertIn("no manual -c", skill)
        self.assertIn("guardrail, not a boundary", skill)

    def test_the_shim_is_executable_stdlib_only_and_impersonal(self):
        text = GIT_SHIM.read_text()
        self.assertTrue(os.access(str(GIT_SHIM), os.X_OK))
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/env python3")
        pattern = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/"]), re.IGNORECASE)
        for number, line in enumerate(text.splitlines(), 1):
            self.assertIsNone(pattern.search(line), "%d: %s" % (number, line))


    # ---- #1050 (in TestGitShim, which owns the fake real git and run_git)
    """#1050: no git call of the fleet may open a credential dialog. S2 (the shim's environment and argument on every verb and host), S3 (launch environments), S4 (heavy and
    test setup), S5 (a real git, a recording fake helper, three mutants of the shim), S6 (docs), S7 (nothing writes the global git configuration)."""

    NO = "credential.interactive=false"
    VERBS = (("push", ["origin", "b"]), ("fetch", ["--all"]), ("pull", ["--ff-only"]), ("clone", ["URL", "x"]), ("ls-remote", ["URL"]), ("remote", ["update"]),
             ("status", ["-sb"]), ("log", ["-1"]), ("commit", ["-m", "x"]), ("config", ["a.b"]))
    HOSTS = ("https://github.com/acme/api.git", "https://git.example.test/acme/api.git")

    def test_s2_every_verb_for_the_code_host_and_another_host_carries_all_three_settings(self):
        count = 0
        for host in self.HOSTS:
            for verb, rest in self.VERBS:
                with self.subTest(host=host, verb=verb):
                    done, seen = self.run_git(verb, *[host if a == "URL" else a for a in rest])
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(seen["env"], {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
                    self.assertEqual(seen["argv_with_pair"].count(self.NO), 1)
                    self.assertLess(seen["argv_with_pair"].index(self.NO), seen["argv_with_pair"].index(verb), "the setting is a global option, before the verb")
                    count += 1
        self.assertEqual(count, len(self.HOSTS) * len(self.VERBS))

    def test_s2_a_callers_interactive_true_is_overridden_and_another_credential_setting_stays(self):
        done, seen = self.run_git("-c", "credential.interactive=true", "-c", "credential.helper=store", "-c", "GCM_INTERACTIVE=always", "fetch")
        argv = seen["argv_with_pair"]
        self.assertGreater(argv.index(self.NO), argv.index("credential.interactive=true"), "ours comes last, so it wins")
        self.assertIn("credential.helper=store", argv)
        done, seen = self.run_git("-c", "credential.interactive=true", "status", env={"GIT_TERMINAL_PROMPT": "1", "GCM_INTERACTIVE": "always"})
        self.assertEqual(seen["env"], {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}, "a caller's environment cannot turn them back on")

    def test_s3_the_launch_environments_hold_both_variables(self):
        both = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        env = sb.launch_base_env({"opencode_env": {"GIT_TERMINAL_PROMPT": "1", "X": "1"}, "launch_env": {"GCM_INTERACTIVE": "always"}})   # a terminal launch and a reattach
        self.assertEqual({k: env[k] for k in both}, both)
        self.assertEqual(env["X"], "1")
        seen = {}

        def runner(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, "", "")
        sb.V2Client("/opt/lab/bin/opencode", {"GIT_TERMINAL_PROMPT": "1"}, runner, 7).server_info()   # the service client (a headless worker's tools)
        self.assertEqual({k: seen["env"][k] for k in both}, both)
        entry = (REPO / "cloud" / "worker" / "entrypoint.sh").read_text()
        self.assertRegex(entry, r"(?m)^GIT_TERMINAL_PROMPT=0; export GIT_TERMINAL_PROMPT$")   # the pod's entrypoint, before it runs the command
        self.assertRegex(entry, r"(?m)^GCM_INTERACTIVE=never; export GCM_INTERACTIVE$")
        self.assertLess(entry.index("export GCM_INTERACTIVE"), entry.index('exec "$@"'))

    def test_s4_the_heavy_wrapper_and_the_test_setup_set_both_variables(self):
        out = self.home / "heavy-env"
        hs = sb.load_heavy_slots()
        code = hs.run([sys.executable, "-c", "import os,sys;open(sys.argv[1],'w').write(os.environ.get('GIT_TERMINAL_PROMPT','')+' '+os.environ.get('GCM_INTERACTIVE',''))", str(out)],
                      str(self.home / "heavy-state"), environ={"PATH": os.environ["PATH"], "FLEET_HEAVY": "off", "GIT_TERMINAL_PROMPT": "1"}, out=io.StringIO())
        self.assertEqual((code, out.read_text()), (0, "0 never"))
        self.assertEqual((os.environ["GIT_TERMINAL_PROMPT"], os.environ["GCM_INTERACTIVE"]), ("0", "never"), "this suite's own setup")

    def test_s4_no_test_runs_real_git_against_a_local_server_without_a_private_home(self):
        offenders = []
        for path in sorted(BIN.glob("test-*")) + sorted((REPO / "cloud").rglob("test*.py")):
            text = path.read_text(errors="replace")
            local = re.search(r"git[^\n]{0,80}(?:ls-remote|clone|fetch|push)[^\n]{0,120}http://(?:127\.0\.0\.1|localhost)", text)
            if local and "GIT_CONFIG_NOSYSTEM" not in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def fake_helper(self):
        starts = self.home / "helper-starts"
        helper = self.home / "helper"
        # a stand-in for a helper that opens a dialog unless told not to (as Git Credential Manager does: GCM_INTERACTIVE and its `credential.interactive` setting; git's own prompt is GIT_TERMINAL_PROMPT)
        helper.write_text("#!/bin/sh\n[ \"$GCM_INTERACTIVE\" = never ] && [ \"$GIT_TERMINAL_PROMPT\" = 0 ] && [ \"$(git config credential.interactive)\" = false ] || echo started >> %s\n" % starts)
        helper.chmod(0o755)
        (self.home / "private").mkdir(exist_ok=True)
        self.helper_path = helper   # handed to git through GIT_CONFIG_COUNT in `lsremote`: no git configuration file is written
        return starts

    def lsremote(self, shim, env_drop=(), extra_env=None):
        """git ls-remote to a closed local port through `shim` (a copy of the real one, or a mutant), with a private home and a helper that records its start."""
        starts = self.fake_helper()
        if starts.exists():
            starts.unlink()
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        got = []

        def serve():   # a server that answers 401 once: this is what makes git ask its credential helper
            try:
                conn, _ = server.accept()
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                conn.close()
            except OSError:
                pass
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        real = shutil.which("git", path=SAFE_PATH) or shutil.which("git")
        env = {"PATH": "%s:%s" % (os.path.dirname(real), SAFE_PATH), "HOME": str(self.home / "private"), "GIT_CONFIG_NOSYSTEM": "1", "FLEET_REAL_GIT": real,
               "XDG_CONFIG_HOME": str(self.home / "xdg-none"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": str(self.helper_path)}
        env.update(extra_env or {})   # (the sentinel test points the home variables at its scratch home)
        started = time.time()
        done = subprocess.run([sys.executable, str(shim), "ls-remote", "http://127.0.0.1:%d/x.git" % port], capture_output=True, text=True, env=env, timeout=30)
        took = time.time() - started
        server.close()
        thread.join(5)
        return done, took, starts.exists() and starts.read_text().count("started")

    def mutant(self, name, old, new):
        text = GIT_SHIM.read_text()
        self.assertIn(old, text, "the mutant %s no longer applies" % name)
        path = self.home / ("mutant-" + name)
        path.mkdir()
        (path / "stash_guard.py").write_text((GIT_SHIM.parent / "stash_guard.py").read_text())
        (path / "git").write_text(text.replace(old, new, 1))
        (path / "git").chmod(0o755)
        return path / "git"

    def test_s5_a_local_port_url_fails_at_once_and_the_helper_never_starts(self):
        if not (shutil.which("git", path=SAFE_PATH) or shutil.which("git")):
            self.skipTest("no real git")
        done, took, starts = self.lsremote(GIT_SHIM)
        self.assertEqual((done.returncode, starts), (128, 0), done.stderr)
        self.assertLess(took, 5)
        self.assertNotIn("started", done.stdout + done.stderr)

    def test_s5_three_mutants_of_the_shim_start_the_helper_or_do_not_fail(self):
        if not (shutil.which("git", path=SAFE_PATH) or shutil.which("git")):
            self.skipTest("no real git")
        mutants = {
            "drop-terminal-prompt": ('{"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}', '{"GCM_INTERACTIVE": "never"}'),
            "drop-gcm-interactive": ('{"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}', '{"GIT_TERMINAL_PROMPT": "0"}'),
            "interactive-back-on": ('NO_DIALOG_CONFIG = "credential.interactive=false"', 'NO_DIALOG_CONFIG = "credential.interactive=true"'),
            "code-host-only": ("    return no_dialog(route_credentials(args))", "    return no_dialog(route_credentials(args)) if any('github.com' in a for a in args) else route_credentials(args)"),
        }
        for name, (old, new) in mutants.items():
            with self.subTest(name):
                done, took, starts = self.lsremote(self.mutant(name, old, new))
                self.assertTrue(starts >= 1 or done.returncode != 128, "the mutant %s was not caught: exit %s, %s helper starts" % (name, done.returncode, starts))

    def test_s6_the_documentation_names_each_place_next_to_both_variables(self):
        text = (REPO / "docs" / "no-credential-dialog.md").read_text()
        rows = [line for line in text.splitlines() if line.startswith("| ") and "GIT_TERMINAL_PROMPT" in line and "GCM_INTERACTIVE" in line]
        for place in ("shims/git", "bin/fleet-switchboard", "entrypoint.sh", "bin/heavy_slots.py"):
            self.assertEqual(len([r for r in rows if place in r]), 1, place)
        self.assertEqual(len(rows), 4)

    # S7/U1, a cheap first line only: a text scan is a deny-list and cannot see a spelling built from pieces ("--glo"+"bal", a split-joined XDG path). The ROOT check is
    # behavioural (test_s7_u2_...). Each row below has a hostile sample that ONLY that row finds, and a test that skips each row in turn and fails (one mutant per row).
    _G, _S = "glo" + "bal", "sys" + "tem"
    CONFIG_WRITE_FLAGS = ["--" + w for w in ("add", "unset", "unset-all", "replace-all", "edit", "rename-section", "remove-section")]
    CONFIG_WRITE_SPELLINGS = (   # (name, regex over one line, a hostile line that only this row catches)
        ("config command with a write flag", r"\bconfig\b[^\n]*(?:%s)(?![\w-])" % "|".join(re.escape(f) for f in CONFIG_WRITE_FLAGS),
         'run(["git", "con' + 'fig", "--a' + 'dd", "k", "v"])'),
        ("the short write flag", r"\bcon" + r"fig\b[^\n]*\s-e\b", "git con" + "fig -e"),
        ("the global or system scope", r"\bconfig\b[^\n]*--(?:%s|%s)(?![\w-])" % (_G, _S), 'os.system("git config --' + _S + ' a b")'),
        ("the file scope", r"\bcon" + r"fig\b[^\n]*(?:\s-f\b|--fi" + "le\\b)", "git con" + "fig -f out.cfg a b"),
        ("the home gitconfig path", r"\.git" + "config", 'open(os.path.expanduser("~")+"/.git' + 'config","a")'),
        ("the XDG git directory", r"\.con" + r"fig/git\b|XDG_CONFIG" + r"_HOME[^\n]*/git\b|XDG_CONFIG" + r"_HOME[^\n]*[\"']git[\"']", 'echo x > "$XDG_CONFIG' + '_HOME/git/con' + 'fig"'),
        ("the global/system override variables", r"GIT_CONFIG_(?:%s|%s)\b" % (_G.upper(), _S.upper()), "GIT_CONFIG_" + _G.upper() + " = path"),
    )

    @classmethod
    def config_write_hits(cls, text):
        return [(n, line) for line in text.splitlines() for n, rx, _ in cls.CONFIG_WRITE_SPELLINGS if re.search(rx, line)]

    @staticmethod
    def added_lines(diff):
        return "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))

    def test_s7_u1_every_row_of_the_scan_finds_its_sample_and_only_that_row_does(self):
        for name, rx, hostile in self.CONFIG_WRITE_SPELLINGS:
            with self.subTest(name):   # the mutant: skip this row; the sample must then go unfound by every other row, so the row cannot be dropped unseen
                found = [n for n, other, _ in self.CONFIG_WRITE_SPELLINGS if re.search(other, hostile)]
                self.assertEqual(found, [name])
        for extra in ("git con" + "fig --fi" + "le=/x/y a b", "git con" + "fig -" + "f /x a b"):
            self.assertTrue(self.config_write_hits(extra), extra)
        for clean in ('env = {"GIT_CONFIG_NO' + 'SYSTEM": "1"}', "git config credential.interactive", 'run(["git", "config", "--get", "a.b"])'):
            self.assertEqual(self.config_write_hits(clean), [], clean)

    def test_s7_u2_the_focused_paths_leave_a_sentinel_home_byte_identical_and_add_no_file(self):
        if not (shutil.which("git", path=SAFE_PATH) or shutil.which("git")):
            self.skipTest("no real git")
        scratch = self.home / "sentinel-home"
        (scratch / (".con" + "fig") / "git").mkdir(parents=True)
        (scratch / (".git" + "config")).write_text("[user]\n\tname = Sentinel\n")
        (scratch / (".con" + "fig") / "git" / "con" "fig").write_text("[core]\n\tabbrev = 9\n")

        def snapshot():
            return {str(p.relative_to(scratch)): (p.read_bytes() if p.is_file() else None) for p in sorted(scratch.rglob("*"))}
        before = snapshot()
        pointed = {"HOME": str(scratch), "GIT_CONFIG_" + self._G.upper(): str(scratch / (".git" + "config")), "XDG_CONFIG" + "_HOME": str(scratch / (".con" + "fig"))}
        with mock.patch.dict(os.environ, pointed):
            sb.launch_base_env({"opencode_env": {}, "launch_env": {}})
            sb.V2Client("/opt/lab/bin/opencode", {}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""), 7).server_info()
            hs = sb.load_heavy_slots()
            hs.run([sys.executable, "-c", "pass"], str(self.home / "heavy-sentinel"), environ=dict(os.environ, FLEET_HEAVY="off"), out=io.StringIO())
            self.run_git("fetch", "--all")
            self.run_git("config", "a.b")
            done, took, starts = self.lsremote(GIT_SHIM, extra_env=pointed)   # the real shim in front of a real git, the sentinel as its home
            self.assertEqual((done.returncode, starts), (128, 0), done.stderr)
        self.assertEqual(snapshot(), before, "a focused path wrote, changed or added a file in the home it was pointed at")

    def test_s7_u1_no_file_of_this_change_writes_the_global_git_configuration(self):
        base = None
        for ref in ("origin/main", "main"):
            probe = subprocess.run(["git", "-C", str(REPO), "merge-base", ref, "HEAD"], capture_output=True, text=True)
            if probe.returncode == 0 and probe.stdout.strip():
                base = probe.stdout.strip()
                break
        if base is None:
            self.skipTest("no main to diff against (a shallow checkout)")
        diff = subprocess.run(["git", "-C", str(REPO), "diff", base, "-U0", "--no-color"], capture_output=True, text=True, timeout=60).stdout
        self.assertEqual(self.config_write_hits(self.added_lines(diff)), [])


class TestGhShim(Base):
    """The agent types plain `gh issue comment ...`; the shim signs it. A fake gh records what it was given."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        self.log = self.home / "gh-log.json"
        fake = self.bin / "gh"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        "files={a: open(a).read() for a in sys.argv[1:] if os.path.isfile(a)}\n"
                        "json.dump({'argv': sys.argv[1:], 'stdin': None if sys.stdin.isatty() else sys.stdin.read(), 'files': files}, "
                        "open(%r,'w'))\nprint('real gh out'); sys.stderr.write('real gh err\\n'); sys.exit(int(os.environ.get('FAKE_GH_EXIT','0')))\n"
                        % str(self.log))
        fake.chmod(0o755)
        self.who = self.home / "whoami"
        # One process start answers: `whoami --json` gets the JSON a current switchboard gives; only FAKE_WHO_OLD=1 makes it an old
        # one (plain text, so the shim asks again with a second start: the fallback, and only its test wants that).
        self.who.write_text("#!/bin/sh\n" + FAKE_SB_SOURCE + "[ -n \"$FAKE_WHO_SLEEP\" ] && sleep \"$FAKE_WHO_SLEEP\"\n[ \"$1\" = whoami ] || exit 2\n"
                            "[ \"$FAKE_WHO_EXIT\" ] && exit $FAKE_WHO_EXIT\n"
                            "if [ \"$2\" = --json ] && [ -z \"$FAKE_WHO_OLD\" ]; then echo \"{\\\"name\\\": \\\"${FAKE_WHO:-platform}\\\"}\"; exit 0; fi\n"
                            "echo \"${FAKE_WHO:-platform}\"\n")
        self.who.chmod(0o755)
        # The first start of a new file is the slow one on some machines (a scan of it): pay for it here, not inside the shim's deadline.
        subprocess.run([str(self.who), "whoami", "--json"], capture_output=True, timeout=60)

    def run_shim(self, *args, stdin="", env=None, who=True):
        environ = {"PATH": "%s:%s:%s" % (SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home), "FLEET_SHIM_LOOKUP_SECONDS": SHIM_LOOKUP_SECONDS}
        if who:
            environ["FLEET_SWITCHBOARD_BIN"] = str(self.who)
        environ.update(env or {})
        write_fake_env(self.home, environ)
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run([sys.executable, str(SHIM)] + list(args), input=stdin, capture_output=True, text=True,
                              env=environ, timeout=60)
        seen = json.loads(self.log.read_text()) if self.log.exists() else None
        return done, seen

    def link_switchboard(self, target=None, dangling=False):
        link = self.home / ".local" / "bin"
        link.mkdir(parents=True, exist_ok=True)
        (link / "fleet-switchboard").symlink_to(self.home / "gone" if dangling else (target or self.who))

    # --- #767: a body file directly in a shared temp directory is warned about, never refused
    def shim_warnings(self, *args, env=None):
        done, seen = self.run_shim(*args, env=env)
        return done, seen, [l for l in done.stderr.splitlines() if "shared temp directory" in l]

    def test_s3_a_body_file_in_a_shared_temp_directory_warns_once_and_runs_unchanged(self):
        root = self.home / "profile-tmp"
        env = {"FLEET_AGENT_TMPDIR": str(root / "coder")}
        for flag, path in (("--body-file", "/tmp/x.md"), ("-F", "/private/tmp/y.md"), ("--body-file", str(root / "z.md"))):
            with self.subTest(path=path):
                done, seen, lines = self.shim_warnings("issue", "comment", "2", flag, path, env=env)
                self.assertEqual((done.returncode, len(lines)), (0, 1))
                self.assertIn("$TMPDIR", lines[0])
                self.assertEqual(seen["argv"][:3], ["issue", "comment", "2"])
        done, seen, lines = self.shim_warnings("pr", "create", "--body-file=/tmp/a.md", env=env)
        self.assertEqual(len(lines), 1)
        done, seen, lines = self.shim_warnings("pr", "create", "-F/tmp/a.md", env=env)
        self.assertEqual(len(lines), 1)
        done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", "/tmp/../tmp/q.md", env=env)
        self.assertEqual(len(lines), 1)
        done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", "/tmp/sub/../x.md", env=env)
        self.assertEqual(len(lines), 1)   # folded lexically to /tmp/x.md

    def test_s4_an_own_directory_a_mktemp_subdirectory_and_a_relative_path_give_no_warning(self):
        root = self.home / "profile-tmp"
        env = {"FLEET_AGENT_TMPDIR": str(root / "coder")}
        for path in (str(root / "coder" / "x.md"), "/tmp/tmp.AbC123/x.md", "body.md", "./body.md", "/tmp/sub/../tmp.d/x.md"):
            with self.subTest(path=path):
                done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", path, env=env)
                self.assertEqual((done.returncode, lines), (0, []))

    def test_s5_the_path_is_never_opened_stat_ed_or_read(self):
        spec = importlib.util.spec_from_loader("gh_shim_767", importlib.machinery.SourceFileLoader("gh_shim_767", str(SHIM)))
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        fifo = self.home / "pipe"
        os.mkfifo(str(fifo))
        locked = self.home / "locked.md"
        locked.write_text("x")
        locked.chmod(0)
        paths = ["/tmp/%s" % fifo.name, "/tmp/missing-767.md", "/tmp/%s" % locked.name]
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise AssertionError("touched the filesystem")
        with mock.patch.multiple(os, stat=boom, lstat=boom, open=boom, listdir=boom, access=boom), \
                mock.patch.multiple(os.path, exists=boom, isfile=boom, isdir=boom, islink=boom, realpath=boom), \
                mock.patch("builtins.open", boom):
            for path in paths:
                self.assertEqual(shim.shared_body_file(["issue", "comment", "2", "--body-file", path], {}), path)
        self.assertEqual(calls, [])
        done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", paths[1])
        self.assertEqual((done.returncode, len(lines)), (0, 1))

    def test_the_warning_shows_no_control_characters(self):
        done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", "/tmp/a\x1b[31m\rb.md")
        self.assertEqual(len(lines), 1)
        self.assertNotIn("\x1b", done.stderr.split("real gh")[0])
        self.assertNotIn("\r", done.stderr)

    def test_the_unchanged_exit_code_and_gh_api_fields(self):
        done, seen, lines = self.shim_warnings("issue", "comment", "2", "--body-file", "/tmp/x.md", env={"FAKE_GH_EXIT": "3"})
        self.assertEqual((done.returncode, len(lines)), (3, 1))
        done, seen, lines = self.shim_warnings("api", "x", "-F", "/tmp/x.md")
        self.assertEqual(lines, [])

    def test_unset_env_falls_back_to_the_linked_switchboard(self):
        self.link_switchboard()
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", who=False)
        self.assertIn("from=platform", seen["argv"][-1])

    def test_the_env_var_wins_over_the_link(self):
        self.link_switchboard()
        other = self.home / "other-who"
        other.write_text("#!/bin/sh\necho elsewhere\n")
        other.chmod(0o755)
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", env={"FLEET_SWITCHBOARD_BIN": str(other)})
        self.assertIn("from=elsewhere", seen["argv"][-1])

    def test_a_relative_env_var_is_agent_not_the_link(self):
        self.link_switchboard()
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", env={"FLEET_SWITCHBOARD_BIN": "rel/x"})
        self.assertIn("from=agent", seen["argv"][-1])

    def test_no_link_or_a_dangling_one_or_a_non_executable_is_agent(self):
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", who=False)
        self.assertIn("from=agent", seen["argv"][-1])
        self.link_switchboard(dangling=True)
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", who=False)
        self.assertIn("from=agent", seen["argv"][-1])
        (self.home / ".local" / "bin" / "fleet-switchboard").unlink()
        plain = self.home / "plain"
        plain.write_text("x")
        self.link_switchboard(plain)
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", who=False)
        self.assertIn("from=agent", seen["argv"][-1])

    def test_a_failing_or_odd_whoami_through_the_link_is_agent(self):
        self.link_switchboard()
        for env in ({"FAKE_WHO_EXIT": "1"}, {"FAKE_WHO": "bad name!"}):
            done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", who=False, env=env)
            self.assertIn("from=agent", seen["argv"][-1])

    MARK = "<!-- fleet-switchboard:from=platform -->"

    def test_a_slow_identity_lookup_is_the_whole_of_the_old_flakiness(self):
        """The signing tests failed on a busy machine ('from=agent' for 'from=platform'): the shim's 3 s lookup deadline ran out because the lookup (two process
        starts) was slow, not because the real switchboard was reached (the run's environment is scrubbed and its binary is the fake). Shown on purpose: with a
        deadline shorter than the lookup the name is `agent`; with the deadline the tests now use it is the name."""
        slow = {"FAKE_WHO_SLEEP": "1.5"}   # the old-style fake: two starts
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", env=dict(slow, FLEET_SHIM_LOOKUP_SECONDS="0.2"))
        self.assertIn("from=agent", seen["argv"][-1], "a lookup slower than its deadline falls back to agent")
        done, seen = self.run_shim("issue", "comment", "2", "--body", "hi", env=slow)
        self.assertIn("from=platform", seen["argv"][-1], "the same slow lookup within the tests' deadline is the name")

    def test_each_way_of_giving_a_body_is_signed(self):
        signed = "hello\n\n" + self.MARK + "\n"
        for args, expected in (
                (("issue", "comment", "2", "--body", "hello"), ["issue", "comment", "2", "--body", signed]),
                (("issue", "comment", "2", "-b", "hello"), ["issue", "comment", "2", "-b", signed]),
                (("pr", "comment", "7", "--body", "hello", "-R", "o/r"), ["pr", "comment", "7", "--body", signed, "-R", "o/r"]),
                (("issue", "comment", "2", "--body=hello"), ["issue", "comment", "2", "--body=" + signed]),
                (("issue", "comment", "2", "-bhello"), ["issue", "comment", "2", "-b" + signed]),
                (("pr", "review", "7", "--approve", "--body", "hello"), ["pr", "review", "7", "--approve", "--body", signed]),
                (("issue", "create", "--title", "t", "--body", "hello"), ["issue", "create", "--title", "t", "--body", signed]),
                (("pr", "create", "--title", "t", "--body", "hello"), ["pr", "create", "--title", "t", "--body", signed])):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(seen["argv"], expected)

    def test_a_body_file_is_signed_through_a_copy_and_the_original_is_left_alone(self):
        original = self.home / "body.md"
        original.write_text("from a file")
        done, seen = self.run_shim("issue", "comment", "2", "--body-file", str(original))
        (sent_path,) = [a for a in seen["argv"] if a.endswith(".md") and a != str(original)]
        self.assertEqual(seen["files"][sent_path], "from a file\n\n" + self.MARK + "\n")
        self.assertEqual(original.read_text(), "from a file")
        self.assertFalse(os.path.exists(sent_path), "the signed copy is removed afterwards")

    def test_a_body_on_stdin_is_signed(self):
        done, seen = self.run_shim("issue", "comment", "2", "-F", "-", stdin="from stdin\nline two")
        self.assertEqual(seen["argv"], ["issue", "comment", "2", "-F", "-"])
        self.assertEqual(seen["stdin"], "from stdin\nline two\n\n" + self.MARK + "\n")

    def test_an_already_signed_body_is_not_signed_twice(self):
        done, seen = self.run_shim("issue", "comment", "2", "--body", "x\n\n<!-- fleet-switchboard:from=cos -->")
        self.assertEqual(seen["argv"][4].count("fleet-switchboard:from"), 1)

    def test_everything_else_runs_the_real_gh_with_exactly_the_same_arguments(self):
        for args in (("issue", "view", "2", "--json", "body"), ("pr", "list"), ("api", "repos/o/r/issues/2/comments",
                     "-f", "body=x"), ("issue", "comment", "2"), ("issue", "comment", "2", "--editor"),
                     ("issue", "comment"), ("--version",), ()):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args)
                self.assertEqual(seen["argv"], list(args))

    def test_the_real_gh_output_and_exit_code_pass_through(self):
        done, _ = self.run_shim("issue", "comment", "2", "--body", "x", env={"FAKE_GH_EXIT": "3"})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "real gh out\n", "real gh err\n"))
        done, _ = self.run_shim("issue", "view", "2", env={"FAKE_GH_EXIT": "4"})
        self.assertEqual(done.returncode, 4)

    def test_the_signature_names_the_agent_or_says_agent_when_it_cannot_be_found(self):
        _, seen = self.run_shim("issue", "comment", "2", "--body", "x", env={"FAKE_WHO": "cos-2"})
        self.assertIn("from=cos-2", seen["argv"][4])
        for kwargs in ({"who": False}, {"env": {"FAKE_WHO_EXIT": "1"}}, {"env": {"FAKE_WHO": "Not A Name!"}},
                       {"env": {"FLEET_SWITCHBOARD_BIN": "relative/fleet-switchboard"}},
                       {"env": {"FLEET_SWITCHBOARD_BIN": "/does/not/exist"}}):
            with self.subTest(kwargs=kwargs):
                done, seen = self.run_shim("issue", "comment", "2", "--body", "x", **kwargs)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("from=agent", seen["argv"][4], "still signed: the signature is what stops the echo")

    def test_it_never_calls_itself_and_says_so_when_there_is_no_real_gh_behind_it(self):
        environ = {"PATH": str(SHIM.parent), "HOME": str(self.home)}
        done = subprocess.run([sys.executable, str(SHIM), "issue", "view", "1"], capture_output=True, text=True, env=environ)
        self.assertEqual(done.returncode, 127)
        self.assertIn("no real gh", done.stderr)

    def test_an_unreadable_body_file_runs_the_command_unchanged(self):
        done, seen = self.run_shim("issue", "comment", "2", "--body-file", "/no/such/file.md")
        self.assertEqual(seen["argv"], ["issue", "comment", "2", "--body-file", "/no/such/file.md"])

    # ---- readable text: the shim warns on stderr, never refuses (ask #401, shims/body_warn.py)
    WARN = "may be hard to read"
    LONG = "word " * 140                                  # 700 characters
    GOOD = "Short paragraph one.\n\n" + "- first item\n- second item\n- third item\n\n" + ("A fair sentence. " * 17).strip() + "\n"

    def load_warn(self):
        spec = importlib.util.spec_from_file_location("body_warn_t", str(SHIM.parent / "body_warn.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_s2_each_bad_comment_prints_one_warning_and_the_real_gh_runs_with_its_status(self):
        for body in ("line one\\nline two", self.LONG, "Steps: (1) a (2) b (3) c done."):
            with self.subTest(body=body[:20]):
                done, seen = self.run_shim("issue", "comment", "2", "--body", body, env={"FAKE_GH_EXIT": "3"})
                self.assertEqual(done.returncode, 3)
                self.assertEqual(done.stderr.count(self.WARN), 1, done.stderr)
                self.assertIn("real gh out", done.stdout)
                self.assertIn("Markdown lists", done.stderr)
                self.assertEqual(seen["argv"][:3], ["issue", "comment", "2"])

    def test_s2_the_other_spellings_and_commands_warn_too(self):
        for args in (("pr", "create", "--title", "t", "--body=a\\nb"), ("issue", "edit", "2", "-ba\\nb"), ("pr", "review", "2", "-b", "x\\ny")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args)
                self.assertEqual(done.stderr.count(self.WARN), 1, done.stderr)
                self.assertIsNotNone(seen)

    def test_s2_a_body_file_and_stdin_are_never_opened_or_read_by_the_warning(self):
        module = self.load_warn()
        fifo = self.home / "pipe"
        os.mkfifo(fifo)
        (self.home / "f.md").write_text("one\\ntwo\n")
        for args in (("issue", "comment", "2", "--body-file", str(fifo)), ("issue", "comment", "2", "-F", str(fifo)), ("issue", "comment", "2", "--body-file=" + str(fifo)),
                     ("issue", "comment", "2", "-F", str(self.home / "f.md")), ("issue", "comment", "2", "-F", "-")):
            with self.subTest(args=args):
                self.assertIsNone(module.warning(list(args)))   # a FIFO with no writer would hang a warning that opened it
        # through the shim: a regular file and stdin go to gh without a warning (the shim's own signing reads them, as before)
        for args, stdin in ((("issue", "comment", "2", "-F", str(self.home / "f.md")), None), (("issue", "comment", "2", "-F", "-"), "a\\nb")):
            with self.subTest(shim=args):
                done, seen = self.run_shim(*args, stdin=stdin)
                self.assertNotIn(self.WARN, done.stderr)
                self.assertIsNotNone(seen)

    def test_s3_a_fence_or_a_code_span_hides_what_would_warn(self):
        for body in ("Intro.\n\n```\n" + self.LONG + "\n(1) (2) (3) \\n\n```\n", "Run `printf 'a\\nb (1) (2) (3)'` and see.", "~~~sh\nx\\ny\n~~~\n"):
            with self.subTest(body=body[:20]):
                done, _ = self.run_shim("issue", "comment", "2", "--body", body)
                self.assertNotIn(self.WARN, done.stderr)

    def test_s4_a_well_formed_body_prints_nothing_and_the_real_gh_runs_once(self):
        done, seen = self.run_shim("issue", "comment", "2", "--body", self.GOOD)
        self.assertEqual(done.stderr, "real gh err\n")
        self.assertIsNotNone(seen)
        self.assertGreater(max(len(p) for p in self.GOOD.split("\n\n")), 200)

    def test_s4_a_long_list_is_not_one_long_paragraph(self):
        done, _ = self.run_shim("issue", "comment", "2", "--body", "\n".join("- item number %d of the list" % n for n in range(40)))
        self.assertNotIn(self.WARN, done.stderr)

    def test_u1_a_command_with_no_text_flag_loads_nothing_and_spawns_nothing_extra(self):
        done, seen = self.run_shim("issue", "view", "2")
        self.assertEqual(done.stderr, "real gh err\n")
        source = (SHIM.parent / "body_warn.py").read_text()
        self.assertNotRegex(source, r"import .*\b(subprocess|socket|urllib|http|os)\b")

    def test_s8_an_inline_post_starts_no_process_and_no_network_and_opens_no_file(self):
        module = self.load_warn()
        def boom(*a, **k):
            raise AssertionError("no process, network or file")
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), mock.patch.object(socket, "socket", boom), \
                mock.patch.object(os, "system", boom), mock.patch.object(os, "open", boom), mock.patch.object(os, "stat", boom), mock.patch("builtins.open", boom):
            self.assertIn(self.WARN, module.warning(["issue", "comment", "2", "--body", "a\\nb"]))
            self.assertIsNone(module.warning(["issue", "comment", "2", "--body-file", "/no/such"]))

    def test_a_doubt_means_no_warning(self):
        module = self.load_warn()
        for args in (["issue", "comment", "2", "--body"], ["issue", "view", "2", "--body", "a\\nb"], ["issue"]):
            with self.subTest(args=args):
                self.assertIsNone(module.warning(args))

    def test_s6_the_warning_files_name_no_private_thing(self):
        pattern = re.compile("|".join(["jan" + "us", "sp" + "ire", "adk" + "aplan", "/Us" + "ers/", "direc" + "tor(?!y|ies)"]), re.I)
        for path in (SHIM.parent / "body_warn.py",):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(pattern.search(line), "%s:%d" % (path.name, number))


class TestTrialDriver(Base):
    def trial(self):
        return TRIAL.Trial(self.home / "trial")

    def test_its_root_is_never_a_live_directory(self):
        root = str(self.trial().root)
        for live in ("~/.config/opencode", "~/.local/share/opencode", "~/.local/state/opencode",
                     "~/.config/herdr", "~/.local/state/herdr"):
            self.assertFalse(root.startswith(os.path.expanduser(live)), live)

    def fake_gh(self, token="canary-gh"):
        """A gh that answers `auth token` and records the GH_CONFIG_DIR it was asked under."""
        path = self.home / "fake-gh"
        path.write_text('#!/bin/sh\necho "$GH_CONFIG_DIR" > "%s"\necho "$HOME" > "%s"\necho "$@" > "%s"\n'
                        '[ -n "%s" ] && echo "%s"\n[ -n "%s" ]\n'
                        % (self.home / "gh-config-seen", self.home / "gh-home-seen", self.home / "gh-args-seen",
                           token, token, token))
        path.chmod(0o755)
        return str(path)

    def wrapper_env(self, gh_token="canary-gh", **extra):
        trial = self.trial()
        trial.gh_bin, trial.gh_config_dir = self.fake_gh(gh_token), "/real/gh"
        probe = trial.wrapper_text().rsplit("exec ", 1)[0] + 'env\n'
        env = dict({"PATH": SAFE_PATH}, **extra)
        return subprocess.run([shutil.which("sh"), "-c", probe], env=env, capture_output=True, text=True).stdout

    def test_the_wrapper_takes_every_credential_out_before_it_gives_agents_the_gh_token(self):
        text = self.trial().wrapper_text()
        self.assertLess(text.index('unset "$name"'), text.index('export GH_TOKEN="$GH_FOR_AGENTS"'),
                        "the token is added after the strip")
        self.assertIn("_API_KEY", text)  # the OpenRouter key is of this shape
        self.assertIn("COPILOT", text)
        self.assertNotIn("OPENROUTER", text, "the wrapper never names the key, it strips by shape")
        out = self.wrapper_env(OPENROUTER_API_KEY="canary-key", SOME_SECRET="canary-secret", KEEP_ME="yes",
                               GH_TOKEN="ambient-gh", GITHUB_TOKEN="ambient-github")
        self.assertIn("GH_TOKEN=canary-gh", out)
        self.assertIn("KEEP_ME=yes", out)
        for canary in ("canary-key", "canary-secret", "ambient-gh", "ambient-github"):
            self.assertNotIn(canary, out)

    def test_the_token_comes_from_the_users_real_gh_config_not_the_trials_home(self):
        """The trial found agents with no GH_TOKEN: the token was handed to the service only, but the
        agents' shell runs in the pane's process. The wrapper asks gh on every start."""
        self.wrapper_env()
        self.assertEqual((self.home / "gh-config-seen").read_text().strip(), "/real/gh")
        self.assertNotIn("GH_CONFIG_DIR", self.wrapper_env(), "agents do not inherit the real gh config dir")

    def test_gh_runs_under_the_users_real_home_because_macos_keeps_its_token_in_the_login_keychain(self):
        trial = self.trial()
        trial.gh_bin, trial.gh_config_dir = self.fake_gh(), "/real/gh"
        probe = trial.wrapper_text().rsplit("exec ", 1)[0] + 'env\n'
        subprocess.run([shutil.which("sh"), "-c", probe], env={"PATH": SAFE_PATH, "HOME": "/users-real-home"},
                       capture_output=True, text=True)
        self.assertEqual((self.home / "gh-home-seen").read_text().strip(), "/users-real-home")

    def recording_gh(self, fail_on=None):
        """A gh that appends each call to a log, and fails the call whose first words are `fail_on`."""
        path = self.home / "rec-gh"
        log = self.home / "rec-gh.log"
        path.write_text('#!/bin/sh\necho "$GH_TOKEN|$@" >> "%s"\n[ "$1 $2" = "%s" ] && { echo "HTTP 404" >&2; exit 1; }\n'
                        '[ "$1" = auth ] && echo tok\nexit 0\n' % (log, fail_on or "no such"))
        path.chmod(0o755)
        return str(path), log

    def test_init_creates_the_charter_labels_with_the_os_users_prefix_and_labels_the_charter(self):
        trial = self.trial()
        trial.gh_bin, log = self.recording_gh()
        trial.gh_user = "the-pinned-one"
        with mock.patch.object(TRIAL.getpass, "getuser", return_value="osuser"):
            names = trial.ensure_repo_labels("o/r", 7)
        self.assertEqual(names, ["osuser:orchestrator", "osuser:awaiting-cos", "osuser:awaiting-user"])
        calls = [line.split("|", 1)[1].split() for line in log.read_text().splitlines() if "auth token" not in line]
        self.assertEqual([c[:3] for c in calls], [["label", "create", "osuser:orchestrator"],
                                                   ["label", "create", "osuser:awaiting-cos"],
                                                   ["label", "create", "osuser:awaiting-user"],
                                                   ["issue", "edit", "7"]])
        for call in calls[:3]:
            self.assertIn("--force", call, "idempotent: an existing label is updated, not an error")
        self.assertIn("osuser:orchestrator", calls[3])

    def test_a_label_failure_stops_init_and_says_which_call_failed(self):
        trial = self.trial()
        trial.gh_bin, _ = self.recording_gh(fail_on="label create")
        trial.gh_user = "u"
        with self.assertRaises(TRIAL.TrialError) as caught:
            trial.ensure_repo_labels("o/r", 1)
        self.assertIn("gh label create", str(caught.exception))

    def test_the_wrapper_names_the_shims_the_plugin_puts_first_on_the_agents_path(self):
        self.assertIn('export FLEET_SWITCHBOARD_SHIMS="%s"' % (REPO / "shims"), self.trial().wrapper_text())
        self.assertTrue(os.access(str(REPO / "shims" / "gh"), os.X_OK))

    def test_the_wrapper_asks_for_the_pinned_account_not_whichever_gh_has_active(self):
        """The active gh account changed under the trial (to one that cannot see the repo). The agents
        and the daemon must keep running as the account chosen at init."""
        trial = self.trial()
        trial.gh_bin, trial.gh_config_dir, trial.gh_user = self.fake_gh(), "/real/gh", "the-pinned-one"
        probe = trial.wrapper_text().rsplit("exec ", 1)[0] + 'env\n'
        subprocess.run([shutil.which("sh"), "-c", probe], env={"PATH": SAFE_PATH}, capture_output=True, text=True)
        self.assertEqual((self.home / "gh-args-seen").read_text().split(), ["auth", "token", "--user", "the-pinned-one"])

    def test_the_daemons_token_is_the_pinned_accounts_and_only_in_its_environment(self):
        trial = self.trial()
        trial.gh_bin, trial.gh_config_dir, trial.gh_user = self.fake_gh("pinned-token"), "/real/gh", "the-pinned-one"
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ambient"}):
            self.assertEqual(trial.switchboard_env()["GH_TOKEN"], "pinned-token")
        self.assertEqual((self.home / "gh-args-seen").read_text().split(), ["auth", "token", "--user", "the-pinned-one"])
        trial.root.mkdir(parents=True, exist_ok=True)
        trial.save({"repo": "o/r"})
        self.assertNotIn("pinned-token", trial.settings_file.read_text())

    def test_without_a_pinned_account_no_token_is_asked_for(self):
        trial = self.trial()
        trial.gh_bin, trial.gh_user = self.fake_gh(), None
        self.assertIsNone(trial.gh_token())
        self.assertNotIn("--user", trial.wrapper_text().split("GH_FOR_AGENTS=")[1].split("\n")[0])

    def test_init_refuses_when_gh_cannot_give_a_token_for_the_account(self):
        trial = self.trial()
        trial.gh_bin = self.fake_gh(token="")
        with self.assertRaises(TRIAL.TrialError) as caught:
            trial.init("o/r", 1, "p/m", gh_user="someone")
        self.assertIn("--gh-user", str(caught.exception))

    def test_the_gh_token_is_never_written_to_the_trials_files(self):
        trial = self.trial()
        trial.gh_bin, trial.gh_config_dir = self.fake_gh(), "/real/gh"
        self.assertNotIn("canary-gh", trial.wrapper_text())
        self.assertNotIn("FLEET_TRIAL_GH_TOKEN", inspect.getsource(TRIAL))

    def test_agents_find_fleet_switchboard_on_their_path(self):
        probe = self.trial().wrapper_text().rsplit("exec ", 1)[0] + 'command -v fleet-switchboard\n'
        out = subprocess.run([shutil.which("sh"), "-c", probe], env={"PATH": SAFE_PATH}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), str(BIN / "fleet-switchboard"))

    def test_up_names_the_models_v2_does_list_when_the_configured_one_is_missing(self):
        trial = self.trial()
        listing = subprocess.CompletedProcess([], 0, "github-copilot/a github-copilot/b other/c\n", "")
        with mock.patch.object(TRIAL.Trial, "wrapper_run", return_value=listing):
            self.assertIsNone(trial.model_problem("github-copilot/a"))
            problem = trial.model_problem("github-copilot/zzz")
        self.assertIn("github-copilot/a, github-copilot/b", problem)
        self.assertNotIn("other/c", problem)

    def test_the_wrapper_without_a_gh_login_gives_agents_none(self):
        self.assertNotIn("GH_TOKEN", self.wrapper_env(gh_token="", GH_TOKEN="ambient"))

    def test_the_switchboard_env_points_at_the_trials_dirs_and_the_real_herdr(self):
        trial = self.trial()
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": "/run/herdr.sock"}):
            env = TRIAL.Trial(self.home / "trial").switchboard_env()
        self.assertEqual(env["XDG_CONFIG_HOME"], str(trial.config_home))
        self.assertEqual(env["XDG_STATE_HOME"], str(trial.state_home))
        self.assertEqual(env["HERDR_SOCKET_PATH"], "/run/herdr.sock")
        self.assertNotIn("HERDR_PANE_ID", env, "the daemon is not a pane")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/home/u/.config"}):
            self.assertEqual(TRIAL.Trial(self.home / "trial").switchboard_env()["GH_CONFIG_DIR"], "/home/u/.config/gh",
                             "the daemon's gh keeps the account the user logged in, not an empty one under the trial's dir")

    def test_an_uninitialised_trial_says_what_to_run(self):
        with self.assertRaises(TRIAL.TrialError) as caught:
            self.trial().require_init()
        self.assertIn("init", str(caught.exception))

    def test_up_refuses_without_a_copilot_login(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        trial.save({"repo": "o/r", "charter": 1})
        with mock.patch.object(TRIAL.Trial, "logged_in", return_value=False), \
                self.assertRaises(TRIAL.TrialError) as caught:
            TRIAL.cmd_up(trial, None)
        self.assertIn("login", str(caught.exception))

    def test_the_empty_pane_herdr_makes_with_the_workspace_is_closed_once_agents_exist(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        trial.save({"repo": "o/r", "spare_pane": "wX:p1"})
        closed = []

        def herdr(*args, **kwargs):
            if args[:2] == ("pane", "list"):
                return {"panes": [{"pane_id": "wX:p1"}, {"pane_id": "wX:p2"}]}
            closed.append(args)
            return {}

        with mock.patch.object(trial.proof, "herdr", herdr):
            self.assertTrue(trial.close_spare_pane())
            self.assertEqual(closed, [("pane", "close", "wX:p1")])
            self.assertNotIn("spare_pane", trial.settings())
            self.assertFalse(trial.close_spare_pane(), "recorded once, closed once")

    def test_the_spare_pane_is_kept_while_it_is_the_only_pane(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        trial.save({"spare_pane": "wX:p1"})
        calls = []
        with mock.patch.object(trial.proof, "herdr", lambda *a, **k: calls.append(a) or {"panes": [{"pane_id": "wX:p1"}]}):
            self.assertFalse(trial.close_spare_pane())
        self.assertNotIn(("pane", "close", "wX:p1"), calls)

    def test_a_running_service_is_never_restarted_by_up(self):
        trial = self.trial()
        calls = []

        def wrapper_run(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess([], 0, "http://127.0.0.1:49380\n" if args == ("service", "status") else "", "")

        with mock.patch.object(TRIAL.Trial, "wrapper_run", side_effect=wrapper_run):
            self.assertEqual(trial.ensure_service(), "already running")
        self.assertEqual(calls, [("service", "status")], "nothing but a status read")

    def test_a_stopped_service_is_started_through_the_wrapper(self):
        trial = self.trial()
        calls = []

        def wrapper_run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess([], 0, "stopped\n" if args == ("service", "status") else "", "")

        with mock.patch.object(TRIAL.Trial, "wrapper_run", side_effect=wrapper_run):
            self.assertIn("started", trial.ensure_service())
        self.assertEqual(calls[-1], (("service", "start"), {}), "no environment is passed: the wrapper builds it")

    def restart_rig(self, running=("cos", "platform"), sessions=None):
        """A trial whose agents, sessions and herdr are stubs that record what the driver does."""
        trial = self.trial()
        trial.root.mkdir(parents=True, exist_ok=True)
        trial.save({"repo": "o/r", "charter": 1})
        sessions = sessions if sessions is not None else {"cos": "ses_cos", "platform": "ses_platform"}
        panes = {"cos": "wT:p2", "platform": "wT:p3"}
        log = []
        trial.observation = lambda name: {"session": sessions[name], "pane": panes[name]} if name in running else None
        trial.latest_session = lambda name: sessions.get(name)
        trial.workspace_id = lambda create=False: "wT"
        trial.ensure_service = lambda: "already running"
        trial.close_pane = lambda pane, workspace: log.append(("close", pane, workspace))
        trial.resume = lambda workspace, name, session: log.append(("resume", name, session)) or {"pane": "wT:p9"}
        trial.launch = lambda *a, **k: log.append(("launch", a[1])) or {"pane": "wT:p8"}
        return trial, log

    def run_restart(self, trial, names):
        with mock.patch.object(TRIAL, "Trial", return_value=trial), captured():
            return TRIAL.cmd_restart(trial, types.SimpleNamespace(names=names))

    def test_restart_closes_each_pane_then_reopens_the_same_session(self):
        trial, log = self.restart_rig()
        self.run_restart(trial, [])
        self.assertEqual(log, [("close", "wT:p2", "wT"), ("resume", "cos", "ses_cos"),
                               ("close", "wT:p3", "wT"), ("resume", "platform", "ses_platform")])

    def test_restart_of_one_agent_touches_only_that_one(self):
        trial, log = self.restart_rig()
        self.run_restart(trial, ["platform"])
        self.assertEqual(log, [("close", "wT:p3", "wT"), ("resume", "platform", "ses_platform")])

    def test_restart_after_down_resumes_the_session_without_closing_anything(self):
        trial, log = self.restart_rig(running=())
        self.run_restart(trial, ["cos"])
        self.assertEqual(log, [("resume", "cos", "ses_cos")])

    def test_restart_refuses_an_agent_that_never_existed_and_an_unknown_name(self):
        trial, log = self.restart_rig(running=(), sessions={})
        with self.assertRaises(TRIAL.TrialError) as caught:
            self.run_restart(trial, ["cos"])
        self.assertIn("run `switchboard-trial up`", str(caught.exception))
        with self.assertRaises(TRIAL.TrialError):
            self.run_restart(trial, ["stranger"])
        self.assertEqual(log, [])

    def test_a_pane_outside_the_trials_workspace_is_never_closed(self):
        trial = self.trial()
        with mock.patch.object(trial.proof, "herdr", lambda *a, **k: {"pane": {"workspace_id": "wLIVE"}}
                               if a[:2] == ("pane", "get") else self.fail("closed %r" % (a,))):
            with self.assertRaises(TRIAL.TrialError) as caught:
                trial.close_pane("wLIVE:p1", "wT")
        self.assertIn("refusing to close", str(caught.exception))

    def test_up_resumes_an_agent_that_has_a_session_and_no_pane_and_creates_one_that_never_existed(self):
        trial, log = self.restart_rig(running=("platform",), sessions={"cos": "ses_cos", "platform": "ses_platform"})
        self.assertIn("resumed session ses_cos", trial.ensure_agent("cos", "wT", "/d", "o/r", 1))
        self.assertEqual(trial.ensure_agent("platform", "wT", "/d", "o/r", 1), "already running")
        trial2, log2 = self.restart_rig(running=(), sessions={})
        self.assertIn("created", trial2.ensure_agent("cos", "wT", "/d", "o/r", 1))
        self.assertEqual(log2, [("launch", "cos")])

    def test_up_fresh_creates_a_new_session_even_when_one_exists(self):
        trial, log = self.restart_rig(running=(), sessions={"cos": "ses_cos"})
        self.assertIn("created", trial.ensure_agent("cos", "wT", "/d", "o/r", 1, fresh=True))
        self.assertEqual(log, [("launch", "cos")])

    def test_the_newest_session_with_the_agents_name_is_the_one_resumed(self):
        trial = self.trial()
        body = {"data": [{"id": "ses_old", "time": {"created": 1}, "metadata": {"fleet": {"name": "cos"}}},
                         {"id": "ses_new", "time": {"created": 9}, "metadata": {"fleet": {"name": "cos"}}},
                         {"id": "ses_other", "time": {"created": 99}, "metadata": {"fleet": {"name": "platform"}}},
                         {"id": "ses_plain", "time": {"created": 99}, "metadata": None}]}
        done = subprocess.CompletedProcess([], 0, json.dumps(body), "")
        with mock.patch.object(TRIAL.Trial, "wrapper_run", return_value=done):
            self.assertEqual(trial.latest_session("cos"), "ses_new")
            self.assertIsNone(trial.latest_session("coder"))

    def test_the_kits_skills_are_installed_in_the_trial_and_the_config_points_at_them(self):
        trial = self.trial()
        trial.root.mkdir(parents=True)
        names = trial.install_skills()
        self.assertIn("fleet-charter", names)
        self.assertEqual((trial.skills_dir() / "fleet-charter" / "SKILL.md").read_text(),
                         (REPO / "skills" / "fleet-charter" / "SKILL.md").read_text())
        self.assertEqual(trial.opencode_config("p/m")["skills"], {"paths": [str(trial.skills_dir())]})
        self.assertFalse(str(trial.skills_dir()).startswith(os.path.expanduser("~/.agents")))

    def test_the_coordination_skill_sends_switchboard_agents_to_launch_not_to_type_a_bare_opencode(self):
        """The trial's orchestrator followed this skill, typed `opencode` into a new tab and got the old v1
        install, outside the switchboard, typing its brief with `herdr agent prompt`."""
        text = " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())
        head = text.split("How you start a named agent without the switchboard", 1)[0]
        self.assertIn("fleet-switchboard launch <name>", head)
        self.assertIn("--brief-file -", head)
        self.assertIn("Do **not** use the `herdr tab create` / `herdr agent start` / `herdr agent prompt` routes", head)
        self.assertIn("legacy fleet's route", head)
        self.assertLess(text.index("fleet-switchboard launch <name>"), text.index("herdr agent start <name> --kind"))

    def test_the_charter_skill_does_not_tell_a_switchboard_agent_to_ack_a_wake(self):
        text = " ".join((REPO / "skills" / "fleet-charter" / "SKILL.md").read_text().split())
        self.assertIn("A `[switchboard]` message has no acknowledgement", text)
        self.assertIn("Only a message starting `Heartbeat` expects an acknowledgement", text)
        self.assertNotIn("If your fleet runs a heartbeat that expects an acknowledgement, send exactly one", text)

    def test_the_parser_knows_its_commands(self):
        parser = TRIAL.build_parser()
        for argv in (["init", "--repo", "o/r", "--charter", "1"], ["login"], ["up"], ["up", "--fresh"], ["restart"],
                     ["restart", "cos"], ["status"], ["down"]):
            self.assertIn(parser.parse_args(argv).action, TRIAL.COMMANDS)

    def test_the_service_port_is_not_the_labs(self):
        self.assertEqual(TRIAL.SERVICE_PORT, 49380)
        self.assertNotEqual(TRIAL.SERVICE_PORT, 49374)


class TestInstall(InstallCase):
    def full(self, *flags, **kw):
        return self.install("--opencode", self.machine.binary, *flags, **kw)

    def test_a_full_install_does_every_step_and_says_so(self):
        out, error = self.full()
        self.assertIsNone(error)
        steps = self.statuses(out)
        for name in ("opencode v2", "config", "wrapper", "agents", "skills", "skills path", "fleet-hooks plugin",
                     "decisions plugin", "todolist plugin", "herdr plugin", "service", "daemon"):
            self.assertIn(name, steps, "%s: no line in\n%s" % (name, out))
        self.assertEqual(steps["config"], ["done"])
        self.assertIn("STATUS", out, "the last step prints status")

    def test_it_writes_the_config_the_pieces_and_the_service(self):
        self.full()
        config = json.loads(self.config_file.read_text())
        wrapper = self.wrapper
        self.assertEqual((config["opencode"], config["opencode_executable"]), (str(wrapper), self.machine.binary))
        self.assertEqual((config["github_repos"], config["jev"], config["policy"]),
                         ([], {"model": sb.JEV_MODEL}, {"enabled": True}))
        self.assertNotIn("intent_repo", config)
        sb.validate_config(config)
        self.assertTrue(os.access(str(wrapper), os.X_OK))
        for name in sb.KIT_AGENTS:
            text = (self.profile / "agents" / (name + ".md")).read_text()
            self.assertIn(sb.STANDARD_MODEL, text)
            self.assertNotIn("__PROVIDER__", text)
        for skill in sb.kit_skill_names(REPO):
            self.assertTrue((self.home / "xdg-data" / "fleet-switchboard" / "skills" / skill / "SKILL.md").is_file())
        self.assertEqual((self.profile / "plugins" / "fleet-hooks.js").read_text(),
                         (REPO / "plugins" / "fleet-hooks" / "index.js").read_text())
        self.assertEqual(json.loads((self.profile / "cli.json").read_text())["plugins"], ["./fleet-decisions"])
        self.assertTrue((self.profile / "fleet-decisions" / "tui.tsx").is_file())
        self.assertIn(str(self.home / "xdg-data" / "fleet-switchboard" / "skills"),
                      json.loads((self.profile / "opencode.json").read_text())["skills"]["paths"])
        self.assertTrue(self.service_path().is_file())

    def test_the_wrapper_sets_the_plugins_environment_and_holds_no_secret(self):
        os.environ["OPENROUTER_API_KEY"] = PR16_KEY
        self.full()
        text = self.wrapper.read_text()
        for needle in ("FLEET_SWITCHBOARD_BIN=", "FLEET_SWITCHBOARD_SHIMS=", "FLEET_SWITCHBOARD_STATE=", "exec "):
            self.assertIn(needle, text)
        self.assertIn(self.machine.binary, text)
        self.assertNotIn(PR16_KEY, text)

    def test_a_second_run_changes_nothing_and_every_step_says_already_done(self):
        self.full()
        before, calls = tree_of(self.home), len(self.machine.calls)
        out, error = self.full()
        self.assertIsNone(error)
        steps = self.statuses(out)
        for name, words in steps.items():
            if name != "daemon":
                self.assertEqual(words, ["already done"], "%s on the second run:\n%s" % (name, out))
        self.assertEqual(tree_of(self.home), before)
        mutating = [c for c in self.machine.calls[calls:] if c[:3] in (["herdr", "plugin", "link"],) or c[:2] == ["launchctl", "bootstrap"]]
        self.assertEqual(mutating, [])

    def test_dry_run_prints_every_step_and_writes_nothing(self):
        before = tree_of(self.home)
        out, error = self.full("--dry-run")
        self.assertIsNone(error)
        self.assertEqual(tree_of(self.home), before)
        steps = self.statuses(out)
        for name in ("opencode v2", "config", "wrapper", "agents", "skills", "skills path", "fleet-hooks plugin",
                     "decisions plugin", "todolist plugin", "herdr plugin", "service"):
            self.assertIn(name, steps, name)
        self.assertEqual(steps["todolist plugin"], ["would be done"])
        self.assertFalse((self.profile / "opencode-todolist").exists())
        self.assertEqual(steps["config"], ["would be done"])
        run = [c for c in self.machine.calls if c[:3] == ["herdr", "plugin", "link"] or c[0] in ("launchctl",) and c[1] != "print"]
        self.assertEqual(run, [], "a dry run starts and loads nothing")
        self.assertEqual(self.daemons.spawned, [])

    def test_dry_run_on_an_installed_machine_says_already_done(self):
        self.full()
        out, _ = self.full("--dry-run")
        for name, words in self.statuses(out).items():
            if name != "daemon":
                self.assertEqual(words, ["already done"], name)

    def test_uninstall_reverses_the_pieces_the_link_and_the_service_and_leaves_the_rest(self):
        self.full()
        self.config_file.write_text(self.config_file.read_text())
        (self.state_dir / "audit.jsonl").write_text("{}\n")
        out, error = self.install("--uninstall")
        self.assertIsNone(error)
        self.assertFalse(self.service_path().exists())
        self.assertFalse(self.machine.linked)
        self.assertFalse(self.machine.loaded)
        for name in sb.KIT_AGENTS:
            self.assertFalse((self.profile / "agents" / (name + ".md")).exists())
        self.assertFalse((self.profile / "plugins" / "fleet-hooks.js").exists())
        self.assertFalse((self.profile / "fleet-decisions").exists())
        self.assertFalse((self.profile / "cli.json").exists())
        self.assertFalse((self.profile / "opencode.json").exists())
        self.assertFalse((self.home / "xdg-data" / "fleet-switchboard" / "skills").exists())
        self.assertFalse(self.wrapper.exists())
        self.assertTrue(self.config_file.exists(), "config is left alone")
        self.assertTrue((self.state_dir / "audit.jsonl").exists(), "state is left alone")
        again, _ = self.install("--uninstall")
        for name, words in self.statuses(again).items():
            self.assertNotEqual(words, ["done"], "a second uninstall finds nothing to do: %s" % name)

    def test_a_dry_run_of_uninstall_says_what_it_would_remove_and_removes_nothing(self):
        self.full()
        before, calls = tree_of(self.home), len(self.machine.calls)
        out, error = self.install("--uninstall", "--dry-run")
        self.assertIsNone(error)
        self.assertEqual(tree_of(self.home), before)
        self.assertTrue(self.machine.linked and self.machine.loaded)
        self.assertEqual([c for c in self.machine.calls[calls:] if c[:3] == ["herdr", "plugin", "unlink"]
                          or c[:2] == ["launchctl", "bootout"]], [])
        steps = self.statuses(out)
        for name in ("agents", "skills", "fleet-hooks plugin", "decisions plugin", "todolist plugin", "wrapper", "service"):
            self.assertEqual(steps[name][-1], "would be done", name)

    def test_uninstall_keeps_what_the_user_added_or_edited(self):
        self.full()
        (self.profile / "cli.json").write_text(json.dumps({"plugins": ["./fleet-decisions", "./mine"], "theme": "x"}))
        (self.profile / "agents" / "coder.md").write_text("my own coder\n")
        (self.profile / "opencode.json").write_text(json.dumps(
            {"model": "m", "provider": {"p": 1}, "skills": {"paths": [str(self.home / "xdg-data" / "fleet-switchboard" / "skills"), "/mine"]}}))
        self.install("--uninstall")
        self.assertEqual(json.loads((self.profile / "cli.json").read_text()), {"plugins": ["./mine"], "theme": "x"})
        self.assertEqual((self.profile / "agents" / "coder.md").read_text(), "my own coder\n")
        self.assertEqual(json.loads((self.profile / "opencode.json").read_text()),
                         {"model": "m", "provider": {"p": 1}, "skills": {"paths": ["/mine"]}})

    def test_the_users_config_keys_keep_their_values_and_their_order(self):
        mine = '{\n  "poll_seconds": 7,\n  "herdr": "herdr",\n  "jev": {"model": "mine/model"},\n  "github_repos": ["acme/api"]\n}\n'
        self.write_config(mine)
        self.full()
        config = json.loads(self.config_file.read_text(), object_pairs_hook=list)
        self.assertEqual([k for k, _ in config][:4], ["poll_seconds", "herdr", "jev", "github_repos"])
        found = dict(config)
        self.assertEqual((found["poll_seconds"], found["jev"], found["github_repos"]),
                         (7, [("model", "mine/model")], ["acme/api"]))
        self.assertIn("opencode", found)

    def test_a_config_with_every_key_is_not_rewritten(self):
        self.full()
        mtime = self.config_file.stat().st_mtime_ns
        text = self.config_file.read_text()
        time.sleep(0.01)
        self.full()
        self.assertEqual((self.config_file.read_text(), self.config_file.stat().st_mtime_ns), (text, mtime))

    def test_an_invalid_config_is_refused_and_not_overwritten(self):
        self.write_config("{not json")
        out, error = self.full()
        self.assertIn("config", error)
        self.assertEqual(self.config_file.read_text(), "{not json")

    def test_a_v1_binary_is_refused_with_how_to_get_v2_and_nothing_is_written(self):
        self.machine.version = "1.4.2"
        before = tree_of(self.home)
        out, error = self.full()
        self.assertIn("v1", error)
        self.assertIn("npm install --prefix", error)
        self.assertIn("%s@%s" % (sb.V2_PACKAGE, sb.V2_VERSION), error)
        self.assertEqual(tree_of(self.home), before)

    def test_a_bare_name_and_a_missing_binary_are_refused(self):
        out, error = self.install("--opencode", "opencode")
        self.assertIn("absolute", error)
        out, error = self.install("--opencode", str(self.home / "nowhere" / "opencode"))
        self.assertIn("no OpenCode v2 found", error)

    def test_the_binary_may_be_found_in_a_known_place(self):
        known = self.home / "xdg-data" / "fleet-switchboard" / "v2" / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        known.parent.mkdir(parents=True)
        known.write_text("#!/bin/sh\n")
        self.machine.binary = str(known)
        out, error = self.install()
        self.assertIsNone(error, error)
        self.assertEqual(json.loads(self.config_file.read_text())["opencode_executable"], str(known))

    def test_the_flags_skip_the_service_and_the_link_and_say_so(self):
        out, error = self.full("--no-service", "--no-herdr-link")
        steps = self.statuses(out)
        self.assertEqual(steps["service"], ["skipped (--no-service)"])
        self.assertEqual(steps["herdr plugin"], ["skipped (--no-herdr-link)"])
        self.assertFalse(self.service_path().exists())
        self.assertEqual([c for c in self.machine.calls if c[:3] == ["herdr", "plugin", "link"]], [])
        self.assertEqual(self.machine.commands("launchctl"), [])

    def test_the_link_says_exactly_what_it_will_do_before_it_does_it(self):
        out, error = self.full()
        text = out[out.index("herdr plugin"):]
        for needle in ("plugin link", str(REPO / "herdr"), "every pane", "ensure", "poke", "unlink"):
            self.assertIn(needle, text)
        self.assertLess(out.index("every pane"), out.index("done linked"), "the notice comes before the act")

    def test_a_failed_link_stops_the_install_naming_why(self):
        self.machine.fail = {"plugin link": 1}
        out, error = self.full()
        self.assertIn("plugin link", error)
        self.assertEqual(self.machine.commands("launchctl"), [])

    def test_no_secret_is_printed_or_written_anywhere_but_the_key_file(self):
        os.environ["OPENROUTER_API_KEY"] = PR16_KEY
        out, error = self.full()
        self.assertNotIn(PR16_KEY, out + str(error))
        for path, content in tree_of(self.home).items():
            if content is not None:
                self.assertNotIn(PR16_KEY.encode(), content, path)
        for call in self.machine.calls:
            self.assertNotIn(PR16_KEY, " ".join(call))

    def test_the_last_step_runs_ensure_and_then_status(self):
        out, error = self.full(status="THE STATUS\n")
        self.assertEqual(len(self.daemons.spawned), 1)
        self.assertTrue(out.rstrip().endswith("THE STATUS"))

    def test_the_real_status_after_an_install_names_the_missing_key_first(self):
        out = io.StringIO()
        installer = sb.Installer(self.machine, self.daemons, time.time, "darwin", REPO, out=out)
        installer.install(self.machine.binary)
        self.assertIn(PR16_NOTICE, out.getvalue())
        self.assertLess(out.getvalue().index(PR16_NOTICE), out.getvalue().index("fleet-switchboard 0.1.0"))
        self.assertIn("service    installed", out.getvalue())

    def test_the_cli_runs_install_and_returns_its_exit_code(self):
        os.environ["XDG_DATA_HOME"] = str(self.home / "xdg-data")
        with captured() as (out, err):
            code = sb.main(["install", "--opencode", self.machine.binary, "--no-service", "--no-herdr-link", "--dry-run"],
                           runner=self.machine, now=time.time, spawn=self.daemons)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("would be done", out.getvalue())
        with captured() as (out, err):
            code = sb.main(["install", "--opencode", "bare"], runner=self.machine, now=time.time, spawn=self.daemons)
        self.assertEqual(code, 1)
        self.assertIn("absolute", err.getvalue())


class TestInstallPrivateProfile(InstallCase):
    """The MUST-FIX of PR 16's review: the v2 profile is private. `install` used to write a v2-shaped plugin,
    a `fleet-decisions` directory, `skills.paths` and `cli.json` into the directory the ordinary OpenCode (v1)
    reads too, where they can break every v1 agent at start."""

    def full(self, *flags, **kw):
        return self.install("--opencode", self.machine.binary, *flags, **kw)

    def snapshot_of_the_shared_places(self):
        """Every place the ordinary OpenCode reads: the XDG one, and ~/.config/opencode when XDG_CONFIG_HOME is unset."""
        return {str(path): tree_of(path) if path.exists() else None
                for path in (self.shared, self.home / ".config" / "opencode", self.home / ".local" / "share" / "opencode")}

    def shared_is_untouched(self):
        before = self.snapshot_of_the_shared_places()
        self.full()
        return self.snapshot_of_the_shared_places() == before

    def test_the_default_profile_is_private_and_the_shared_directory_is_never_written(self):
        self.shared.mkdir(parents=True)
        (self.shared / "opencode.json").write_text('{"model": "v1/model"}\n')
        (self.shared / "agents").mkdir()
        (self.shared / "agents" / "mine.md").write_text("mine\n")
        self.assertTrue(self.shared_is_untouched())
        for relative in ("agents/coder.md", "plugins/fleet-hooks.js", "fleet-decisions/tui.tsx", "cli.json", "opencode.json"):
            self.assertTrue((self.profile / relative).is_file(), relative)
        self.assertIn(str(self.data / "skills"), json.loads((self.profile / "opencode.json").read_text())["skills"]["paths"])

    def test_the_fault_that_writes_to_the_shared_directory_by_default_is_caught_by_that_test(self):
        os.environ[sb.LAB_ENV], os.environ[sb.FAULT_ENV] = "1", "shared-profile-default"
        self.assertIn("shared-profile-default", sb.KNOWN_FAULTS)
        self.assertFalse(self.shared_is_untouched(), "the control: with the fault, the shared directory is written")
        self.assertTrue((self.shared / "plugins" / "fleet-hooks.js").is_file())
        self.assertTrue((self.shared / "cli.json").is_file())

    def test_the_fault_is_off_outside_the_lab(self):
        os.environ[sb.FAULT_ENV] = "shared-profile-default"
        os.environ.pop(sb.LAB_ENV, None)
        self.assertTrue(self.shared_is_untouched())

    def test_a_dry_run_against_a_temporary_home_touches_nothing_under_config_opencode(self):
        """With XDG_CONFIG_HOME unset, the ordinary OpenCode reads ~/.config/opencode: nothing is created or changed there."""
        os.environ.pop("XDG_CONFIG_HOME")
        ordinary = self.home / ".config" / "opencode"
        self.assertFalse(ordinary.exists())
        before = tree_of(self.home)
        out, error = self.full("--dry-run")
        self.assertIsNone(error, error)
        self.assertFalse(ordinary.exists(), "a dry run creates no ~/.config/opencode")
        self.assertEqual(tree_of(self.home), before, "and writes nothing at all")
        ordinary.mkdir(parents=True)
        (ordinary / "agents").mkdir()
        (ordinary / "agents" / "mine.md").write_text("mine\n")
        before = tree_of(ordinary)
        self.full("--dry-run")
        self.assertEqual(tree_of(ordinary), before)
        self.full()  # and the real thing leaves it alone too
        self.assertEqual(tree_of(ordinary), before)

    def test_a_dry_run_shows_the_profile_path_it_would_use(self):
        out, error = self.full("--dry-run")
        self.assertIsNone(error, error)
        self.assertIn(str(self.private), out)
        self.assertEqual(self.statuses(out)["profile"], ["would be done"])
        self.assertFalse(self.private.exists())
        custom = self.home / "elsewhere" / "profile"
        out, _ = self.full("--dry-run", "--profile", str(custom))
        self.assertIn(str(custom), out)

    def test_the_profile_path_may_be_chosen_and_must_be_absolute(self):
        custom = self.home / "elsewhere" / "profile"
        out, error = self.full("--profile", str(custom))
        self.assertIsNone(error, error)
        self.assertTrue((custom / "config" / "opencode" / "agents" / "coder.md").is_file())
        self.assertFalse(self.private.exists())
        out, error = self.full("--profile", "relative/path")
        self.assertIn("absolute", error)

    def wrapper_probe(self, *, gh_token="canary-gh", flags=(), **extra):
        """What the wrapper's environment looks like, by running it up to its exec with a stand-in `gh` that `install`
        found on PATH."""
        fakebin = self.home / "fakebin"
        fakebin.mkdir(exist_ok=True)
        gh = fakebin / "gh"
        gh.write_text('#!/bin/sh\necho "$@" > "%s"\necho "$GH_CONFIG_DIR" > "%s"\n[ -n "%s" ] && echo "%s"\n[ -n "%s" ]\n'
                      % (self.home / "gh-args", self.home / "gh-config", gh_token, gh_token, gh_token))
        gh.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": "%s:%s" % (fakebin, SAFE_PATH)}):
            shutil.rmtree(str(self.data), ignore_errors=True)
            self.full(*flags)
        probe = self.wrapper.read_text().rsplit("exec ", 1)[0] + "env\n"
        env = dict({"PATH": SAFE_PATH, "HOME": str(self.home)}, **extra)
        return subprocess.run([shutil.which("sh"), "-c", probe], env=env, capture_output=True, text=True).stdout

    def test_the_wrapper_runs_v2_in_the_private_profile_as_the_trials_wrapper_does(self):
        out = self.wrapper_probe(XDG_CONFIG_HOME=str(self.shared.parent), XDG_DATA_HOME="/real/data")
        seen = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        self.assertEqual(seen["XDG_CONFIG_HOME"], str(self.private / "config"))
        self.assertEqual(seen["XDG_DATA_HOME"], str(self.private / "data"))
        self.assertEqual(seen["XDG_STATE_HOME"], str(self.private / "state"))
        self.assertEqual(seen["XDG_CACHE_HOME"], str(self.private / "cache"))
        self.assertEqual(seen["TMPDIR"], str(self.private / "tmp"))
        self.assertEqual(seen["HOME"], str(self.private / "home"), "XDG alone leaves `home` live: v2 would read the real home's skills")
        self.assertEqual(seen["FLEET_SWITCHBOARD_STATE"], str(sb.state_dir_path()))
        self.assertEqual(seen["FLEET_SWITCHBOARD_BIN"], str(REPO / "bin" / "fleet-switchboard"))
        self.assertEqual(seen["FLEET_SWITCHBOARD_SHIMS"], str(REPO / "shims"))
        self.assertTrue(seen["PATH"].startswith(str(REPO / "bin") + ":"))

    def test_the_wrapper_strips_credentials_by_shape_then_hands_agents_the_gh_token_as_the_trial_does(self):
        out = self.wrapper_probe(OPENROUTER_API_KEY="canary-key", SOME_SECRET="canary-secret", KEEP_ME="yes",
                                 GH_TOKEN="ambient-gh", GITHUB_TOKEN="ambient-github", COPILOT_TOKEN="ambient-copilot")
        self.assertIn("GH_TOKEN=canary-gh", out)
        self.assertIn("KEEP_ME=yes", out)
        for canary in ("canary-key", "canary-secret", "ambient-gh", "ambient-github", "ambient-copilot"):
            self.assertNotIn(canary, out)
        text = self.wrapper.read_text()
        self.assertLess(text.index('unset "$name"'), text.index('export GH_TOKEN="$GH_FOR_AGENTS"'))
        self.assertIn(TRIAL.KEY_NAME_PATTERN, text, "the same shape as the trial's wrapper strips")
        self.assertEqual(sb.KEY_NAME_PATTERN, TRIAL.KEY_NAME_PATTERN)
        self.assertNotIn("canary-gh", text, "the token is asked for at every start and never written")
        self.assertNotIn("OPENROUTER", text)

    def test_the_wrapper_asks_gh_before_home_is_replaced_and_for_the_chosen_account_only(self):
        self.wrapper_probe(flags=("--gh-user", "the-pinned-one"))
        self.assertEqual((self.home / "gh-args").read_text().split(), ["auth", "token", "--user", "the-pinned-one"])
        text = self.wrapper.read_text()
        self.assertLess(text.index("GH_FOR_AGENTS="), text.index("export HOME="))
        self.wrapper_probe()
        self.assertEqual((self.home / "gh-args").read_text().split(), ["auth", "token"], "no account chosen: gh's active one")
        self.assertEqual((self.home / "gh-config").read_text().strip(), str(self.home / "xdg-config" / "gh"),
                         "gh's own config, read before the private XDG_CONFIG_HOME hides it")

    def test_a_pane_keeps_its_herdr_identity_only_when_the_config_says_so_and_the_config_does(self):
        self.full()
        config = json.loads(self.config_file.read_text())
        self.assertEqual(config["launch_env"], {"FLEET_SWITCHBOARD_LAB_PANE": "1"})
        self.assertEqual(config["opencode"], str(self.wrapper))
        text = self.wrapper.read_text()
        self.assertIn('unset HERDR_ENV HERDR_PANE_ID HERDR_SOCKET_PATH HERDR_TAB_ID HERDR_WORKSPACE_ID', text)
        quiet = self.wrapper_probe(HERDR_PANE_ID="p1")
        self.assertNotIn("HERDR_PANE_ID", quiet)
        kept = self.wrapper_probe(HERDR_PANE_ID="p1", FLEET_SWITCHBOARD_LAB_PANE="1")
        self.assertIn("HERDR_PANE_ID=p1", kept)

    def test_the_herdr_opencode_integration_is_installed_into_the_private_profile_through_a_private_home(self):
        out, error = self.full()
        self.assertIsNone(error, error)
        home = self.private / "home"
        link = home / ".config" / "opencode"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), str(self.profile))
        self.assertTrue((self.profile / "plugins" / "herdr-agent-state.js").is_file(), "it landed in the private profile")
        self.assertFalse((self.shared / "plugins").exists())
        self.assertFalse((self.home / ".config" / "opencode").exists())
        installs = [env for what, env in self.machine.integration_calls if what == "install"]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0]["HOME"], str(home), "herdr ignores XDG_CONFIG_HOME: HOME is what places it")
        self.assertEqual(self.statuses(out)["herdr integration"], ["done"])
        again, _ = self.full()
        self.assertEqual(self.statuses(again)["herdr integration"], ["already done"])
        self.assertEqual(len([1 for what, _ in self.machine.integration_calls if what == "install"]), 1)

    def test_it_refuses_to_let_herdr_install_anywhere_but_the_private_profile(self):
        self.machine.integration_reports_at = str(self.home / ".config" / "opencode" / "plugins" / "herdr-agent-state.js")
        out, error = self.full()
        self.assertIn("outside", error)
        self.assertEqual([c for c in self.machine.integration_calls if c[0] == "install"], [])
        self.assertEqual(self.machine.commands("launchctl"), [], "the install stopped there")

    def test_the_integration_follows_the_herdr_flag_and_a_dry_run_only_looks(self):
        out, _ = self.full("--no-herdr-link")
        self.assertEqual(self.statuses(out)["herdr integration"], ["skipped (--no-herdr-link)"])
        self.assertEqual(self.machine.integration_calls, [])
        shutil.rmtree(str(self.data))
        out, _ = self.full("--dry-run")
        self.assertEqual(self.statuses(out)["herdr integration"], ["would be done"])
        self.assertEqual([c for c in self.machine.integration_calls if c[0] != "status"], [])
        self.assertFalse((self.private / "home").exists())

    def test_the_shared_profile_is_used_only_when_asked_and_the_person_is_told_in_plain_words(self):
        out, error = self.full("--shared-profile")
        self.assertIsNone(error, error)
        self.assertIn("ordinary OpenCode will read", out)
        self.assertIn(str(self.shared), out)
        self.assertLess(out.index("ordinary OpenCode will read"), out.index("agents"), "said before anything is written")
        self.assertTrue((self.shared / "plugins" / "fleet-hooks.js").is_file())
        self.assertTrue((self.shared / "cli.json").is_file())
        self.assertFalse(self.private.exists(), "no private profile is made")
        self.assertEqual(self.machine.integration_calls, [])
        self.assertEqual(self.statuses(out)["herdr integration"], ["skipped (--shared-profile)"])
        self.assertFalse(self.wrapper.exists(), "nothing runs a wrapper in a shared profile, so none is written")

    def test_a_private_install_says_the_shared_directory_is_left_alone(self):
        out, _ = self.full()
        self.assertNotIn("ordinary OpenCode will read", out)
        self.assertIn("not touched", out)

    def test_uninstall_removes_only_what_it_installed_and_only_in_the_private_profile(self):
        self.full()
        (self.profile / "agents" / "mine.md").write_text("mine\n")
        (self.profile / "cli.json").write_text(json.dumps({"plugins": ["./fleet-decisions", "./mine"]}))
        self.shared.mkdir(parents=True)
        (self.shared / "agents").mkdir()
        (self.shared / "agents" / "coder.md").write_text((self.profile / "agents" / "coder.md").read_text())
        (self.shared / "cli.json").write_text(json.dumps({"plugins": ["./fleet-decisions"]}))
        shared_before = tree_of(self.shared)
        out, error = self.install("--uninstall")
        self.assertIsNone(error, error)
        self.assertEqual(tree_of(self.shared), shared_before, "the shared directory is never read for removal")
        self.assertEqual((self.profile / "agents" / "mine.md").read_text(), "mine\n")
        self.assertEqual(json.loads((self.profile / "cli.json").read_text()), {"plugins": ["./mine"]})
        self.assertFalse((self.profile / "agents" / "coder.md").exists())
        self.assertFalse((self.profile / "plugins" / "fleet-hooks.js").exists())
        self.assertFalse((self.profile / "plugins" / "herdr-agent-state.js").exists(), "herdr's integration is removed too")
        self.assertFalse((self.private / "home" / ".config" / "opencode").exists(), "the link is ours: removed")
        self.assertFalse(self.wrapper.exists())
        self.assertTrue((self.private / "data").is_dir(), "the profile's data, its sessions, stay")
        uninstalls = [env for what, env in self.machine.integration_calls if what == "uninstall"]
        self.assertEqual([env["HOME"] for env in uninstalls], [str(self.private / "home")])
        again, _ = self.install("--uninstall")
        for name, words in self.statuses(again).items():
            self.assertNotEqual(words, ["done"], name)

    def test_uninstall_never_removes_a_home_link_that_is_not_ours(self):
        self.full()
        link = self.private / "home" / ".config" / "opencode"
        link.unlink()
        link.mkdir()
        (link / "theirs.txt").write_text("x\n")
        self.install("--uninstall")
        self.assertTrue((link / "theirs.txt").exists())

    def test_uninstall_of_the_shared_profile_is_asked_for_by_name(self):
        self.full("--shared-profile")
        out, error = self.install("--uninstall", "--shared-profile")
        self.assertIsNone(error, error)
        self.assertFalse((self.shared / "plugins" / "fleet-hooks.js").exists())
        self.assertFalse((self.shared / "cli.json").exists())

    def test_the_trial_keeps_its_own_isolated_profile_and_the_functions_it_shares(self):
        trial = TRIAL.Trial(self.home / "trial")
        self.assertNotEqual(Path(trial.lab.opencode_config), self.profile)
        self.assertTrue(str(trial.lab.opencode_config).startswith(str(self.home / "trial")))
        self.assertIn("sb.install_agents", (BIN / "switchboard-trial").read_text())
        self.assertTrue(callable(sb.ensure_home_link) and callable(sb.herdr_integration_status))


class TestEventFeedReads(unittest.TestCase):
    class Stub:
        def __init__(self, pages=None, fail=False):
            self.calls, self.pages, self.fail = [], pages or [[]], fail

        def message_list(self, session, limit=None, order=None, type=None, cursor=None):
            self.calls.append((session, limit, type))
            if self.fail:
                raise sb.V2Error("backoff", None, "skipped")
            return self.pages[0]

    def feed(self, now, audits=None):
        audit = types.SimpleNamespace(append=lambda actor, name, subject, detail: audits.append((name, detail))) if audits is not None else None
        feed = sb.EventFeed(None, audit, lambda: now[0])
        feed.connected, feed.needs_resync = True, False
        feed.need("test")   # a reader that wants the feed's reads (without one the feed only observes)
        return feed

    def test_an_assistant_message_no_event_announced_is_found_by_one_bounded_read_and_said_to_be_missed(self):
        now, audits = [1000.0], []
        feed = self.feed(now, audits)
        feed.handle(feed_line("session.step.ended", session="ses_a", assistantMessageID="msg_old"))
        engine = types.SimpleNamespace(v2=self.Stub([[{"id": "msg_new"}]]))
        feed.tick(engine, ["ses_a"])
        self.assertEqual(engine.v2.calls, [("ses_a", sb.FEED_PAGE, "assistant")], "one bounded read, newest assistant message only")
        self.assertEqual(audits, [("event.missed", {"session": "ses_a", "last_seen": "msg_old", "found": "msg_new"})])
        feed.tick(engine, ["ses_a"])
        self.assertEqual(len(engine.v2.calls), 1, "within the interval: no read")
        now[0] += sb.FEED_RECONCILE_SECONDS + 1
        feed.tick(engine, ["ses_a"])
        self.assertEqual((len(engine.v2.calls), len(audits)), (2, 1), "read again after the interval; nothing new, so nothing missed")
        self.assertEqual(feed.status()["missed"], 1)

    def test_the_reconciliation_is_spread_and_a_read_that_fails_is_tried_again(self):
        feed = self.feed([1000.0])
        engine = types.SimpleNamespace(v2=self.Stub(fail=True))
        sessions = ["s%d" % n for n in range(10)]
        feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), sb.FEED_RECONCILES_PER_PASS, "at most this many per pass")
        engine.v2.fail = False
        feed.tick(engine, sessions)
        feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), 3 * sb.FEED_RECONCILES_PER_PASS, "the failed ones came round again")

    def test_a_resync_is_spread_over_passes_and_a_flapping_stream_cannot_cause_a_read_storm(self):
        now, audits = [1000.0], []
        feed = self.feed(now, audits)
        feed.needs_resync = True
        engine = types.SimpleNamespace(v2=self.Stub())
        sessions = ["s%d" % n for n in range(10)]
        feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), sb.FEED_RESYNC_PER_PASS, "a burst is never made: the first pass reads a few")
        self.assertTrue(feed.needs_resync, "the rest wait for the next passes")
        for _ in range(2):
            feed.tick(engine, sessions)
        self.assertEqual(sorted(c[0] for c in engine.v2.calls), sorted(sessions), "every session was read once, spread over three passes")
        self.assertFalse(feed.needs_resync)
        self.assertEqual([n for n, _ in audits].count("event.resync"), 3)
        for _ in range(5):   # the stream flaps: a new gap each time, within the minimum interval
            feed.needs_resync = True
            now[0] += 5
            feed.tick(engine, sessions)
        self.assertEqual(len(engine.v2.calls), len(sessions), "no session is read again within the minimum interval, however often the stream reconnects")

    def test_a_resync_backlog_does_not_skip_the_reconcile_of_a_session_that_is_due(self):
        now = [5000.0]
        feed = self.feed(now)
        feed.needs_resync = True
        feed.seen["old"] = {"assistant": "m", "idle_ms": None, "read": 5000.0 - sb.FEED_RECONCILE_SECONDS - 1, "event_at": None}
        engine = types.SimpleNamespace(v2=self.Stub())
        sessions = ["old"] + ["s%d" % n for n in range(8)]
        feed.tick(engine, sessions)
        read = [c[0] for c in engine.v2.calls]
        self.assertIn("old", read, "reconciled in the same pass as the resync of the others")
        self.assertEqual(len([r for r in read if r != "old"]), sb.FEED_RESYNC_PER_PASS)

    def test_a_failing_few_cannot_starve_the_rest_and_failures_are_counted(self):
        now = [1000.0]

        class Flaky:
            def __init__(f):
                f.calls = []

            def message_list(f, session, **k):
                f.calls.append(session)
                if session in ("s0", "s1", "s2", "s3"):
                    raise sb.V2Error("backoff", None, "skipped")
                return []
        feed = self.feed(now)
        feed.needs_resync = True
        engine = types.SimpleNamespace(v2=Flaky())
        sessions = ["s%d" % n for n in range(8)]
        feed.tick(engine, sessions)
        feed.tick(engine, sessions)
        self.assertTrue(set(sessions[4:]) <= set(engine.v2.calls), "the sessions after the failing four were reached on the second pass")
        self.assertEqual(feed.status()["read_errors"], 4)

    def test_the_owners_light_switch_means_the_feed_makes_no_read_of_session_history(self):
        feed = self.feed([1000.0])
        feed.needs_resync = True
        engine = types.SimpleNamespace(feed=feed, light=True, discovery=types.SimpleNamespace(agents={"a": types.SimpleNamespace(session_id="ses_a")}),
                                       v2=types.SimpleNamespace(message_list=lambda *a, **k: self.fail("a read under v2_light")), _note=lambda *a: None)
        sb.feed_tick(engine)
        self.assertTrue(feed.needs_resync, "nothing was done, nothing is forgotten")
        engine.light = False
        engine.v2 = self.Stub()
        sb.feed_tick(engine)
        self.assertEqual(len(engine.v2.calls), 1, "and with the switch off the same feed reads")

    def test_with_no_consumer_the_feed_only_observes_and_makes_no_read_at_all(self):
        now = [1000.0]

        class Counting:
            def __init__(c):
                c.calls = 0

            def message_list(c, *a, **k):
                c.calls += 1
                return []
        feed = sb.EventFeed(None, None, lambda: now[0])
        feed.connected, feed.needs_resync = True, True
        engine = types.SimpleNamespace(v2=Counting())
        sessions = ["s%d" % n for n in range(20)]
        for _ in range(360):   # 30 simulated minutes of 5 s passes, 20 idle agents, a connected quiet stream
            now[0] += 5
            feed.tick(engine, sessions)
        no_consumer = engine.v2.calls
        feed.need("a reader")
        feed.needs_resync = True
        for _ in range(360):
            now[0] += 5
            feed.tick(engine, sessions)
        print("\nfeed reads over 30 simulated minutes, 20 idle sessions, connected quiet stream: no consumer %d; with one consumer %d (resync 20 + reconcile)"
              % (no_consumer, engine.v2.calls - no_consumer))
        self.assertEqual(no_consumer, 0)
        self.assertLessEqual(engine.v2.calls, 20 + 2 * 20, "one resync read per session, then at most one reconcile read per session per 10 minutes")

    def test_the_feeds_own_idle_reopen_is_not_a_gap_and_any_other_end_is(self):
        feed = sb.EventFeed(None)
        feed.needs_resync = False
        feed.consume(iter([sb.STREAM_OPENED, sb.STREAM_IDLE]))
        self.assertTrue(feed.quiet_end)
        feed.needs_resync = False
        feed.consume(iter([sb.STREAM_OPENED]))
        self.assertFalse(feed.needs_resync, "reopened after our own idle timeout: nothing was lost to a drop")
        self.assertEqual(feed.status()["connects"], 2)
        feed.consume(iter([sb.STREAM_OPENED]))   # that connection ended some other way (no STREAM_IDLE)
        feed.needs_resync = False
        feed.consume(iter([sb.STREAM_OPENED]))
        self.assertTrue(feed.needs_resync, "a close or a drop is a gap")

    def test_an_idle_end_followed_by_a_failed_open_is_not_remembered_the_next_open_is_a_gap(self):
        feed = sb.EventFeed(None)
        feed.consume(iter([sb.STREAM_OPENED, sb.STREAM_IDLE]))
        self.assertTrue(feed.quiet_end)

        class Down:
            def stream_lines(self, path):
                raise sb.V2Unreachable("down")
        failing = sb.EventFeed(Down(), None, sleep=lambda s: failing.stop())
        failing.quiet_end = True
        failing.run()
        self.assertFalse(failing.quiet_end, "a failed open means events may have been missed: the idle exemption is gone")
        failing.needs_resync = False
        failing.stop_event.clear()
        failing.consume(iter([sb.STREAM_OPENED]))
        self.assertTrue(failing.needs_resync, "idle end, a failed open, then a successful one must resync")

    def test_the_sessions_kept_stay_bounded_when_failed_reads_name_new_ones(self):
        feed = sb.EventFeed(None, None, lambda: 1.0)
        feed.connected, feed.needs_resync = True, False
        feed.need("t")
        engine = types.SimpleNamespace(v2=types.SimpleNamespace(message_list=lambda *a, **k: (_ for _ in ()).throw(sb.V2Error("backoff", None, "x"))))
        with mock.patch.object(sb, "FEED_SESSIONS_MAX", 3):
            for n in range(10):
                feed._read(engine, "ses_%d" % n, 1.0, announced=True)
        self.assertLessEqual(feed.status()["sessions"], 3)

    def test_ids_over_the_cap_are_not_kept_and_the_generator_is_closed_when_the_feed_stops(self):
        feed = sb.EventFeed(None)
        feed.handle(feed_line("session.step.ended", session="s" * (sb.FEED_ID_MAX + 1), assistantMessageID="m"))
        feed.handle(feed_line("session.step.ended", session="ses_ok", assistantMessageID="m" * (sb.FEED_ID_MAX + 1)))
        self.assertEqual(list(feed.seen), ["ses_ok"])
        self.assertIsNone(feed.seen["ses_ok"]["assistant"], "an over-long id is dropped, the event still counts")
        closed = []

        def lines():
            try:
                yield sb.STREAM_OPENED
                yield "data: {}"
            finally:
                closed.append(True)
        gen = lines()
        feed.stop()
        feed.consume(gen)
        self.assertEqual(closed, [True], "closed at once, not when collected")

    def test_at_least_a_whole_pass_of_persistently_failing_sessions_go_to_the_back_and_the_rest_are_reached(self):
        now = [1000.0]

        class Flaky:
            def __init__(f):
                f.calls = []

            def message_list(f, session, **k):
                f.calls.append(session)
                if session in failing:
                    raise sb.V2Error("backoff", None, "skipped")
                return [{"id": "m"}]
        sessions = ["s%d" % n for n in range(sb.FEED_RESYNC_PER_PASS + 6)]
        failing = set(sessions[:sb.FEED_RESYNC_PER_PASS + 1])   # more than a whole pass of sessions keep failing
        feed = self.feed(now)
        feed.needs_resync = True
        engine = types.SimpleNamespace(v2=Flaky())
        for _ in range(4):
            now[0] += 200
            feed.tick(engine, sessions)
        self.assertTrue(set(sessions[len(failing):]) <= set(engine.v2.calls), "the healthy sessions were reached although a whole pass of others keeps failing")

    def test_a_feed_that_is_not_connected_reads_nothing_the_old_reads_are_all_there_is(self):
        feed = sb.EventFeed(None, None, lambda: 1000.0)
        feed.needs_resync = True
        feed.need("test")
        engine = types.SimpleNamespace(v2=types.SimpleNamespace(message_list=lambda *a, **k: self.fail("a read while the stream is down")))
        feed.tick(engine, ["ses_a"])

    def test_a_feed_that_breaks_never_fails_the_pass_and_is_said_once(self):
        notes = []
        feed = sb.EventFeed(None)
        feed.tick = mock.Mock(side_effect=RuntimeError("boom"))
        engine = types.SimpleNamespace(feed=feed, discovery=None, _note=lambda *a: notes.append(a))
        sb.feed_tick(engine)
        self.assertEqual(notes[0][0], "event.feed.error")
        sb.feed_tick(types.SimpleNamespace())   # no feed at all: nothing

    def test_the_status_line_says_the_stream_is_up_or_that_the_old_reads_are_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = types.SimpleNamespace(state_dir=tmp, now=lambda: 1000.0, v2=types.SimpleNamespace(calls_by_operation=lambda *a, **k: {}))
            engine.feed = sb.EventFeed(None)
            engine.feed.connected = True
            sb.record_v2_calls(engine)
            self.assertIn("events: stream", sb.v2_calls_status(tmp, 1000.0))
            engine.feed.connected = False
            sb.record_v2_calls(engine)
            self.assertIn("events: DOWN: old reads", sb.v2_calls_status(tmp, 1000.0))


class TestConfigDeclarations(Base):
    """#437 slice 2: a module declares its own configuration key, default and validator; the shared code reads the declarations from the bin directory only."""

    FIXTURE = ('CONFIG_KEY = "fixture_key"\nCONFIG_DEFAULT = {"n": 1}\n\n\n'
               'def validate(raw, source):\n    if not isinstance(raw, dict):\n        raise ValueError("fixture: bad value")\n')

    def bin(self, must=None, **files):
        directory = Path(tempfile.mkdtemp(prefix="decl-"))
        floor = mock.patch.object(sb, "CONFIG_MUST_DECLARE", must or {})
        floor.start()
        self.addCleanup(floor.stop)
        self.addCleanup(shutil.rmtree, directory, True)
        for name, text in files.items():
            (directory / name).write_text(text)
        patcher = mock.patch.object(sb, "CONFIG_DECLARATIONS_DIR", str(directory))
        patcher.start()
        self.addCleanup(patcher.stop)
        return directory

    def warnings(self):
        seen = []
        patcher = mock.patch.object(sb, "warn_unknown_config", lambda message, section="", names=(), known=(): seen.append(list(names)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_s1_a_declared_key_is_known_defaulted_and_needs_no_shared_line(self):
        self.bin(**{"fixture_mod.py": self.FIXTURE})
        seen = self.warnings()
        self.assertEqual(sb.validate_config({})["fixture_key"], {"n": 1}, "the declared default is applied when the key is absent")
        self.assertEqual(sb.validate_config({"fixture_key": {"n": 5}})["fixture_key"], {"n": 5})
        self.assertEqual(seen, [], "no unknown-key warning for a declared key")
        self.assertIn("fixture_key", sb.config_known_keys())
        self.assertNotIn("fixture_key", sb.TOP_LEVEL_EXTRA_KEYS)
        self.assertNotIn("fixture_key", sb.DEFAULTS)

    def test_s2_the_declared_validator_runs_and_its_message_is_the_error(self):
        self.bin(**{"fixture_mod.py": self.FIXTURE})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"fixture_key": "nope"})
        self.assertEqual(str(caught.exception), "fixture: bad value")

    def test_s4_a_key_declared_twice_or_owned_by_the_shared_defaults_is_refused_naming_both(self):
        self.bin(**{"one.py": self.FIXTURE, "two.py": self.FIXTURE})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.config_declarations()
        self.assertIn("one.py", str(caught.exception))
        self.assertIn("two.py", str(caught.exception))
        self.bin(**{"shared.py": 'CONFIG_KEY = "poll_seconds"\n'})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.config_declarations()
        self.assertIn("shared.py", str(caught.exception))
        self.assertIn("shared defaults", str(caught.exception))

    def test_s5_only_py_files_directly_in_the_bin_directory_are_read(self):
        directory = self.bin(**{"real.py": 'CONFIG_KEY = "real_key"\n', "backup.py.bak": 'CONFIG_KEY = "bak_key"\n', "notes.txt": 'CONFIG_KEY = "txt_key"\n'})
        (directory / "sub").mkdir()
        (directory / "sub" / "deep.py").write_text('CONFIG_KEY = "deep_key"\n')
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "out.py").write_text('CONFIG_KEY = "outside_key"\n')
        (directory / "link.py").symlink_to(outside / "out.py")
        self.assertEqual(sb.config_declarations(), {"real_key": "real"}, "0 decoys: a subdirectory, a backup, a text file and a symlink are not read")

    def test_s6_shared_keys_stay_shared_and_an_undeclared_key_still_warns(self):
        self.bin(**{"fixture_mod.py": self.FIXTURE})
        seen = self.warnings()
        sb.validate_config({"reds": None, "heavy": None, "nowhere_key": 1})
        self.assertEqual(seen, [["nowhere_key"]])

    def test_the_listing_is_cached_until_a_file_changes_and_a_caller_cannot_change_the_cache(self):
        directory = self.bin(**{"one.py": 'CONFIG_KEY = "one_key"\n'})
        first = sb.config_declarations()
        first["tampered"] = "x"
        self.assertEqual(sb.config_declarations(), {"one_key": "one"}, "the returned dict is a copy")
        (directory / "two.py").write_text('CONFIG_KEY = "two_key"\n')
        self.assertEqual(sb.config_declarations(), {"one_key": "one", "two_key": "two"}, "a new file is seen")
        (directory / "one.py").write_text('CONFIG_KEY = "uno_key"  # renamed\n')
        self.assertEqual(sb.config_declarations(), {"uno_key": "one", "two_key": "two"}, "an edited file is seen")

    def test_an_unreadable_file_or_directory_fails_the_whole_configuration_naming_it(self):
        directory = self.bin(**{"locked.py": 'CONFIG_KEY = "locked_key"\n'})
        real_open = open
        def deny(path, *args, **kwargs):
            if str(path).endswith("locked.py"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)
        with mock.patch("builtins.open", deny), self.assertRaises(sb.ConfigError) as caught:
            sb.config_declarations()
        self.assertIn("locked.py", str(caught.exception))
        with mock.patch.object(sb, "CONFIG_DECLARATIONS_DIR", str(directory / "missing")), self.assertRaises(sb.ConfigError) as caught:
            sb.config_declarations()
        self.assertIn("cannot be read", str(caught.exception))

    def test_a_declaring_module_must_hold_its_key_and_a_validate(self):
        self.bin(**{"novalidate.py": 'CONFIG_KEY = "nv_key"\n'})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({})
        self.assertIn("novalidate.py", str(caught.exception))
        self.assertIn("no validate", str(caught.exception))
        self.bin(**{"liar.py": 'CONFIG_KEY = "doc_key"\nif True:\n    CONFIG_KEY = "other_key"\ndef validate(raw, source):\n    pass\n'})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({})
        self.assertIn("liar.py", str(caught.exception))
        self.assertIn("'doc_key'", str(caught.exception))

    def test_a_validator_that_fails_any_other_way_is_a_broken_module(self):
        self.bin(**{"crash.py": 'CONFIG_KEY = "crash_key"\ndef validate(raw, source):\n    raise KeyError("boom")\n'})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({})
        self.assertIn("crash.py", str(caught.exception))
        self.assertIn("no configuration is loaded", str(caught.exception))

    def test_the_declaration_line_has_its_edges(self):
        big = "#" * 1000000 + '\nCONFIG_KEY = "late_key"\n'
        self.bin(**{"commented.py": 'CONFIG_KEY = "ok_key"   # a comment\n', "single.py": "CONFIG_KEY = 'sq_key'\n", "spaced.py": 'CONFIG_KEY   =   "sp_key"\n',
                    "tabbed.py": 'CONFIG_KEY\t=\t"tb_key"\n', "typed.py": 'CONFIG_KEY: str = "ty_key"\n', "late.py": big,
                    "nested.py": 'if True:\n    CONFIG_KEY = "nested_key"\n', "text.py": '# CONFIG_KEY = "text_key"\nx = "CONFIG_KEY = \'str_key\'"\n'})
        self.assertEqual(sb.config_declarations(), {"ok_key": "commented", "sq_key": "single", "sp_key": "spaced", "tb_key": "tabbed", "ty_key": "typed"},
                         "quotes, spacing, a tab and an annotation do not matter; a nested statement, a comment, a string and a line past the 1 MB read are not declarations")

    def test_a_key_of_the_wrong_form_is_refused_naming_the_file(self):
        for text in ('CONFIG_KEY = "Upper"\n', 'CONFIG_KEY = "1key"\n', 'CONFIG_KEY = 3\n'):
            with self.subTest(text):
                self.bin(**{"badform.py": text})
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.config_declarations()
                self.assertIn("badform.py", str(caught.exception))

    def test_a_required_key_whose_declaration_is_not_found_refuses_the_whole_configuration(self):
        must = {"excluded_repos": "repo_exclusion"}
        validate = 'def validate(raw, source):\n    pass\n'
        good = {"single quotes": "CONFIG_KEY = 'excluded_repos'\n", "extra spaces": 'CONFIG_KEY    =    "excluded_repos"\n', "a tab": 'CONFIG_KEY\t=\t"excluded_repos"\n'}
        for what, line in good.items():
            with self.subTest(what):
                self.bin(must=must, **{"repo_exclusion.py": line + validate})
                self.assertEqual(sb.validate_config({}).get("excluded_repos"), None)
        bad = {"commented out": '# CONFIG_KEY = "excluded_repos"\n' + validate, "truncated": 'CONFIG_KEY = "excl', "a renamed key": 'CONFIG_KEY = "excluded_repo"\n' + validate,
               "inside a string": 'x = """\nCONFIG_KEY = "excluded_repos"\n"""\n' + validate, "empty module": ""}
        for what, text in bad.items():
            with self.subTest(what):
                self.bin(must=must, **{"repo_exclusion.py": text})
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config({"excluded_repos": ["acme/api"]})
                self.assertIn("repo_exclusion.py", str(caught.exception))
                self.assertIn("excluded_repos", str(caught.exception))
        self.bin(must=must)
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({})
        self.assertIn("repo_exclusion.py", str(caught.exception), "the module is missing")

    def test_the_real_floor_names_the_two_moved_keys_and_the_real_modules_meet_it(self):
        self.assertEqual(sb.CONFIG_MUST_DECLARE, {"excluded_repos": "repo_exclusion", "spotlight_guard": "spotlight_guard", "temp_janitor": "disk_janitor",
                                                  "chat_clear": "chat_clear", "impact_floor": "impact_floor"})
        self.assertEqual({k: sb.config_declarations()[k] for k in sb.CONFIG_MUST_DECLARE}, sb.CONFIG_MUST_DECLARE)

    def test_s7_the_moved_keys_are_known_through_their_declarations(self):
        self.assertTrue({"spotlight_guard", "excluded_repos"} <= set(sb.config_known_keys()))
        self.assertFalse({"spotlight_guard", "excluded_repos"} & sb.TOP_LEVEL_EXTRA_KEYS)
        declared = sb.config_declarations()
        self.assertEqual((declared["excluded_repos"], declared["spotlight_guard"]), ("repo_exclusion", "spotlight_guard"))

    def test_a_declaring_module_that_cannot_load_fails_the_whole_configuration_naming_its_file(self):
        self.bin(**{"broken.py": 'CONFIG_KEY = "broken_key"\nthis is not python\n', "fine.py": 'x = (\n'})
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({})
        self.assertIn("broken.py", str(caught.exception))
        self.assertNotIn("fine.py", str(caught.exception), "only a declaring module fails the load")
        self.assertIn("no configuration is loaded", str(caught.exception))

    def test_a_module_that_does_not_declare_is_never_imported(self):
        self.bin(**{"fine.py": 'raise RuntimeError("imported")\n', "decl.py": self.FIXTURE})
        self.assertEqual(sb.validate_config({})["fixture_key"], {"n": 1})

    def test_the_moved_keys_validate_as_before_and_a_bad_value_keeps_its_message(self):
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"excluded_repos": "acme/api"})
        self.assertIn("'excluded_repos' must be a list", str(caught.exception))
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"spotlight_guard": {"folders": 3}})
        self.assertIn("spotlight_guard", str(caught.exception))
        self.assertNotIn("excluded_repos", sb.validate_config({}), "an absent key with no default adds nothing")

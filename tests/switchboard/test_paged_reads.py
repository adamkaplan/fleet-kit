"""Paginated account reads are counted per page and conditional on page 1 (#1515: S3, S4, S9, U2)."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support.loader import sb  # noqa: E402
from support.paths import REPO  # noqa: E402

sys.path.insert(0, str(REPO / "bin"))
import paged_reads  # noqa: E402

TOKEN = "ghp_PLANTEDTOKENVALUE123"


class PagedGh:
    """A fake gh: three pages of two records, a Link header on pages 1 and 2, an ETag, and 304 on a matching If-None-Match."""

    def __init__(self, pages=3, etag='"v1"'):
        self.pages, self.etag, self.argvs = pages, etag, []

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        path = argv[-1]
        number = int(path.rsplit("&page=", 1)[1])
        if "If-None-Match: " + self.etag in argv:
            return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 304 Not Modified\r\n\r\n", "")
        link = 'Link: <https://x/next>; rel="next"\r\n' if number < self.pages else ""
        body = json.dumps([{"n": number * 10 + i} for i in range(2)])
        return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 200 OK\r\nEtag: %s\r\n%s\r\n%s" % (self.etag, link, body), "")


def layer():
    accounts = sb.GhAccounts(sb.validate_config({}), None, environ={}, now=lambda: 1000.0) if False else sb.GhAccounts(sb.validate_config({}), None, environ={})
    accounts.conditional = True
    return accounts


def count(accounts):
    return sum(1 for c in accounts.calls)


class PagedReadTests(unittest.TestCase):
    def test_three_pages_are_three_counted_requests_shown_alike(self):   # S3
        accounts, gh = layer(), PagedGh()
        done = accounts.read(gh, "repos/o/r/pulls?state=open", "o/r", 5, paginate=True)
        self.assertEqual(count(accounts), 3)
        self.assertEqual(len(gh.argvs), 3)
        self.assertEqual(sb.decode_json_stream(done.stdout), [[{"n": 10}, {"n": 11}], [{"n": 20}, {"n": 21}], [{"n": 30}, {"n": 31}]])
        summary = accounts.call_summary(accounts.now())
        self.assertEqual(sum(sum(e.values()) for e in summary["last_minute"].values()), 3)

    def test_the_first_page_alone_carries_the_validator_and_a_304_costs_nothing(self):   # S4
        accounts, gh = layer(), PagedGh()
        first = accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        before = len(accounts.calls)
        again = accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        self.assertEqual(len(gh.argvs) - 3, 1)
        self.assertEqual(sum(1 for c in list(accounts.calls)[before:] if not c[3]), 0)
        self.assertEqual(again.stdout, first.stdout)
        self.assertEqual([a for a in gh.argvs[:3] if "-H" in a], [])
        self.assertIn("If-None-Match: " + gh.etag, gh.argvs[3])

    def test_a_changed_first_page_reads_every_page_again(self):
        accounts, gh = layer(), PagedGh()
        accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        gh.etag = '"v2"'
        accounts.etags[("default", "paged:repos/o/r/pulls")] = ('"v1"', "old")
        done = accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        self.assertNotEqual(done.stdout, "old")
        self.assertEqual(len(gh.argvs), 6)

    def test_a_header_less_answer_is_passed_through_as_before(self):
        accounts = layer()
        done = accounts.read(lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "[1]", ""), "repos/o/r/pulls", "o/r", 5, paginate=True)
        self.assertEqual((done.stdout, count(accounts)), ("[1]", 1))

    def test_mutant_counting_a_paginated_read_as_one_fails(self):   # S9
        accounts, gh = layer(), PagedGh()
        real = paged_reads.read_pages

        def mutant(accts, *a, **k):
            done = real(accts, *a, **k)
            while len(accts.calls) > 1:
                accts.calls.pop()
            return done
        with mock.patch.object(paged_reads, "read_pages", mutant):
            sb.load_module("paged_reads").read_pages  # the daemon resolves the module by path; the mutant is the module attribute
            accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        self.assertNotEqual(count(accounts), 3, "the mutant counts one: test_three_pages... would fail on it")

    def test_the_records_hold_no_token_or_header(self):   # U2
        accounts, gh = layer(), PagedGh()
        accounts.read(gh, "repos/o/r/pulls", "o/r", 5, paginate=True)
        text = json.dumps([list(c) for c in accounts.calls]) + json.dumps(accounts.call_summary(accounts.now()))
        for planted in (TOKEN, "Authorization", "Etag", "If-None-Match"):
            self.assertNotIn(planted, text)


if __name__ == "__main__":
    unittest.main()

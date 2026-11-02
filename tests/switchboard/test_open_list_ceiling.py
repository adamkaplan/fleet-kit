"""#1886: every open item is read (up to OPEN_LIST_PAGES pages), or a named alert says why not."""
import re
from pathlib import Path
import types

from support.core import Base
from support.loader import sb

NOW = 1798000000.0


def record(n, pr=False):
    out = {"number": n, "state": "open", "title": "t", "body": "", "labels": [], "user": {"login": "u"}, "created_at": "2026-10-01T00:00:00Z",
           "updated_at": "2026-10-01T00:00:00Z", "comments": 0}
    if pr:
        out["pull_request"] = {}
    return out


class PagedHub:
    """A hub that pages like GitHub: 100 a page, `cap` pages at most, newest first; every request is counted."""

    def __init__(self, total):
        self.open = [record(n, pr=(n == 1)) for n in range(total, 0, -1)]   # #1 is the oldest, a held pull request
        self.requests = []

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        if "state=open" not in path:
            self.requests.append(path)
            return [], False
        out, number = [], 0
        while True:
            number += 1
            self.requests.append(path + "&page=%d" % number)
            page = self.open[(number - 1) * 100:number * 100]
            out.extend(page)
            if len(page) < 100:
                return out, False
            if number >= (cap or 5):
                return out, len(self.open) > len(out)

    def open_requests(self):
        return [r for r in self.requests if "state=open" in r]


class TestOpenListCeiling(Base):
    def read_sweep(self, hub):
        budget = [100]
        got = sb.sweep_read_repo(hub, "acme/api", NOW, 7, sb.sweep_phrase_re(["Alex"]), budget, sb.SweepState().strong)
        return got, 100 - budget[0]

    def read_triage(self, hub):
        budget = [100]
        records, cut = sb.triage_read_unit(hub, "acme/api", "issues", NOW, budget)
        return records, cut, 100 - budget[0]

    def test_s2_the_sweep_reads_all_456_and_the_oldest_held_pull_request(self):
        hub = PagedHub(456)
        got, _ = self.read_sweep(hub)
        self.assertEqual(len(got["items"]), 456, msg="the open items were cut at 200")
        self.assertIn(1, got["items"], msg="the open items were cut at 200")
        self.assertEqual(len(hub.open_requests()), 5)

    def test_s3_the_triage_reads_all_456(self):
        hub = PagedHub(456)
        records, cut, _ = self.read_triage(hub)
        self.assertEqual(len(records), 456, msg="the open items were cut at 200")
        self.assertFalse(cut)
        self.assertEqual(len(hub.open_requests()), 5)

    def test_s6_a_small_repository_costs_two_requests_and_a_large_one_five(self):
        for total, want in ((150, 2), (456, 5)):
            hub = PagedHub(total)
            self.read_sweep(hub)
            self.assertEqual(len(hub.open_requests()), want)
            hub = PagedHub(total)
            self.read_triage(hub)
            self.assertEqual(len(hub.open_requests()), want)

    def test_s6_the_budget_is_charged_the_pages_read(self):
        self.assertEqual(sb.sweep_pages_used([0] * 200, True), 2)
        self.assertEqual(sb.sweep_pages_used([0] * 456, True), 5)
        self.assertEqual(sb.sweep_pages_used([0] * 150, False), 2)

    def test_s8_the_default_budget_reads_a_whole_repository_at_the_ceiling(self):
        self.assertEqual(sb.SWEEP_CALLS_PER_PASS, 16)
        hub = PagedHub(1000)
        budget = [sb.SWEEP_CALLS_PER_PASS]
        state = sb.SweepState()
        engine = types.SimpleNamespace(hub=hub, now=lambda: NOW)
        state.started = NOW
        sb.sweep_refresh(engine, state, ["acme/api"], NOW, {"comment_window_days": 7}, sb.sweep_phrase_re(["Alex"]), budget)
        self.assertEqual(len(state.repos), 1)
        self.assertGreaterEqual(budget[0], 0)

    def test_u3_one_constant_and_the_open_reads_name_it(self):
        root = Path(__file__).resolve().parents[2] / "bin"
        entry, triage = (root / "fleet-switchboard").read_text(), (root / "switchboard" / "triage.py").read_text()
        self.assertEqual(len(re.findall(r"^OPEN_LIST_PAGES = ", entry, re.M)), 1)
        self.assertNotIn("OPEN_LIST_PAGES =", triage)
        self.assertEqual(len(re.findall(r"cap=OPEN_LIST_PAGES", entry + triage)), 3)   # the sweep, the triage, the CI recorder
        self.assertEqual(len(re.findall(r"open_pages\(lambda", entry)), 3)   # the asks, the labels and the charters
        self.assertEqual(sb.OPEN_LIST_PAGES, 10)

    def test_s7_asks_and_pages_follow_the_ceiling(self):
        calls = []

        def api(path, repo=None, meta=None):
            page = int(re.search(r"&page=(\d+)", path).group(1))
            calls.append(page)
            return [{"number": 1000 + (page - 1) * 100 + i, "state": "open", "labels": [], "title": "t", "body": ""} for i in range(100 if page < 2 else 30)]
        reader = sb.IntentReader.__new__(sb.IntentReader)
        reader.repo, reader._cache, reader.now, reader._api = "acme/api", {}, lambda: NOW, api
        self.assertEqual(len(reader.open_asks(5)), 130)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(sb.open_pages(lambda page: [{}] * 100 if page <= 10 else [{}])), 1000)

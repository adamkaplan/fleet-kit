"""#1886: every open item is read (up to OPEN_LIST_PAGES pages), or a named alert says why not."""
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import types

from support.core import Base
from support.rebind import rebound
from support.loader import copy_entry, _load, sb, switchboard_source


def edit_program(directory, edit):
    """Apply `edit` (text -> text) to the daemon's code as ONE text (the entry and the parts it is made of, in a copy at `directory`) and write each file
    back; returns the edited text. The edit may land in any file."""
    directory = Path(directory)
    files = [directory / "fleet-switchboard"] + sorted((directory / "switchboard").glob("*.py"))
    mark = "\n#@@FILE %d@@\n"
    edited = edit("".join(mark % i + f.read_text() for i, f in enumerate(files)))
    for i, f in enumerate(files):
        head, _, rest = edited.partition(mark % i)
        f.write_text(rest.partition(mark % (i + 1))[0] if i + 1 < len(files) else rest)
    return edited

NOW = 1798000000.0


def record(n, pr=False):
    out = {"number": n, "state": "open", "title": "t", "body": "", "labels": [], "user": {"login": "u"}, "created_at": "2026-10-01T00:00:00Z",
           "updated_at": "2026-10-01T00:00:00Z", "comments": 0}
    if pr:
        out["pull_request"] = {}
    return out


PAGE_SIZE = 100   # the fixture hub's page size


def pages_for(total):
    return -(-total // PAGE_SIZE)


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

    def read_comments(self, consumer, repo, since, cap, budget=None):   # #1889: what read_pages answered for the comments path
        return self.read_pages("repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100" % (repo, since), older=("created_at", since), cap=cap)

    def read_open_pages(self, path):
        return self.read_pages(path, cap=sb.OPEN_LIST_PAGES)

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
        self.assertEqual(len(hub.open_requests()), pages_for(456))

    def test_s3_the_triage_reads_all_456(self):
        hub = PagedHub(456)
        records, cut, _ = self.read_triage(hub)
        self.assertEqual(len(records), 456, msg="the open items were cut at 200")
        self.assertFalse(cut)
        self.assertEqual(len(hub.open_requests()), pages_for(456))

    def test_s6_a_small_repository_costs_two_requests_and_a_large_one_five(self):
        for total, want in ((150, pages_for(150)), (456, pages_for(456))):
            hub = PagedHub(total)
            self.read_sweep(hub)
            self.assertEqual(len(hub.open_requests()), want)
            hub = PagedHub(total)
            self.read_triage(hub)
            self.assertEqual(len(hub.open_requests()), want)

    def test_s6_the_budget_is_charged_the_pages_read(self):
        self.assertEqual(sb.sweep_pages_used([0] * 200, True), 2)
        self.assertEqual(sb.sweep_pages_used([0] * 456, True), pages_for(456))
        self.assertEqual(sb.sweep_pages_used([0] * 150, False), 2)

    def test_s8_the_default_budget_reads_a_whole_repository_at_the_ceiling(self):
        self.assertGreaterEqual(sb.SWEEP_CALLS_PER_PASS, sb.OPEN_LIST_PAGES)   # a whole repository at the ceiling fits one pass
        hub = PagedHub(1000)
        budget = [sb.SWEEP_CALLS_PER_PASS]
        state = sb.SweepState()
        engine = types.SimpleNamespace(hub=hub, now=lambda: NOW, config={})   # sweep_refresh reads engine.config (#2320)
        state.started = NOW
        sb.sweep_refresh(engine, state, ["acme/api"], NOW, {"comment_window_days": 7}, sb.sweep_phrase_re(["Alex"]), budget)
        self.assertEqual(len(state.repos), 1)
        self.assertGreaterEqual(budget[0], 0)

    def test_u3_one_constant_and_the_open_reads_name_it(self):
        root = Path(__file__).resolve().parents[2] / "bin"
        entry, triage = switchboard_source(), (root / "switchboard" / "triage.py").read_text()   # the entry and its parts
        self.assertEqual(len(re.findall(r"^OPEN_LIST_PAGES = ", entry, re.M)), 1)
        self.assertNotIn("OPEN_LIST_PAGES =", triage)
        self.assertGreaterEqual(len(re.findall(r"cap=OPEN_LIST_PAGES", entry)), 3)   # the sweep, the triage, the CI recorder
        self.assertGreaterEqual(len(re.findall(r"open_pages\(lambda", entry)), 3)   # the asks, the labels and the charters
        self.assertGreaterEqual(sb.OPEN_LIST_PAGES * PAGE_SIZE, 456)   # the ceiling covers the 456-item repository the ask named

    def test_s7_asks_and_pages_follow_the_ceiling(self):
        calls = []

        def api(path, repo=None, meta=None):
            found = re.search(r"&page=(\d+)", path)
            page = int(found.group(1)) if found else 1   # the first page is asked exactly as before (PC-1)
            calls.append(page)
            return [{"number": 1000 + (page - 1) * 100 + i, "state": "open", "labels": [], "title": "t", "body": ""} for i in range(100 if page < 2 else 30)]
        reader = sb.IntentReader.__new__(sb.IntentReader)
        reader.repo, reader._cache, reader.now, reader._api = "acme/api", {}, lambda: NOW, api
        self.assertEqual(len(reader.open_asks(5)), 130)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(sb.open_pages(lambda page: [{}] * 100 if page <= 10 else [{}])), 1000)


class TestOpenListAlert(Base):
    """#1886 S4: 2,000 open items raise ONE alert a day (fake clock: two days), naming the repository, "1,000 or more" and the ceiling."""
    DAY1 = 1798000000.0

    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit, self.reminders = [], []
        cos = types.SimpleNamespace(fleet={"role": "chief-of-staff"})
        self.engine = types.SimpleNamespace(state_dir=str(self.state_dir), actor="daemon", discovery=types.SimpleNamespace(agents={"cos": cos}),
                                            audit=types.SimpleNamespace(append=lambda *a: self.audit.append(a)))
        real = sb.add_repeat_reminder
        sb.add_repeat_reminder = lambda *a, **k: self.reminders.append(a) or real(*a, **k)
        self.addCleanup(setattr, sb, "add_repeat_reminder", real)

    def passes(self, start, n=10):
        for i in range(n):
            sb.open_list_alert(self.engine, "acme/api", start + i * 900)

    def test_s4_one_alert_a_day_and_one_more_the_next_day(self):
        self.passes(self.DAY1)
        self.assertEqual(len(self.reminders), 1)
        text = self.reminders[0][4]
        for part in ("acme/api", "1,000 or more", "1000"):
            self.assertIn(part, text)
        self.assertEqual(len(self.audit), 1)
        self.passes(self.DAY1 + 86400)
        self.assertEqual(len(self.reminders), 2)

    def test_an_uncut_list_and_an_absent_state_file_are_silent(self):
        self.assertEqual(sb.take_open_cuts(), [])
        self.assertEqual((self.reminders, self.audit), ([], []))
        self.assertFalse((self.state_dir / "open-list-alert.json").exists())


class TestOpenListCeilingMutant(Base):
    """#1886 S5: a copy of the code with the open lists capped at 2 pages again fails the S2 and S3 tests; the control copy passes first."""
    NAMES = ("test_s2_the_sweep_reads_all_456_and_the_oldest_held_pull_request", "test_s3_the_triage_reads_all_456")

    def run_against(self, source_edit, tag):
        with tempfile.TemporaryDirectory() as tmp:
            for other in (Path(__file__).resolve().parents[2] / "bin").glob("*.py"):
                (Path(tmp) / other.name).write_bytes(other.read_bytes())   # the flat modules the entry loads by name
            copy_entry(tmp)
            entry = Path(tmp) / "fleet-switchboard"
            edited = edit_program(tmp, source_edit)
            copy = _load("t_open_list_" + tag, entry)
            suite = unittest.TestSuite(TestOpenListCeiling(name) for name in self.NAMES)
            result = unittest.TestResult()
            with mock.patch.dict(globals(), {"sb": copy}), rebound("sb", copy):
                suite.run(result)
        return edited, result

    def test_s5_the_control_passes_and_the_mutant_fails_with_the_text_of_the_check(self):
        _, control = self.run_against(lambda t: t, "control")
        self.assertEqual((len(control.failures), len(control.errors), control.testsRun), (0, 0, 2), msg=str(control.errors))
        line = "OPEN_LIST_PAGES = 10 "
        applied = []

        def mutate(text):
            self.assertEqual(text.count(line), 1)
            applied.append(True)
            return text.replace(line, "OPEN_LIST_PAGES = 2  ", 1)

        edited, mutant = self.run_against(mutate, "mutant")
        self.assertTrue(applied)
        self.assertIn("OPEN_LIST_PAGES = 2 ", edited)
        self.assertEqual(len(mutant.failures), 2, msg=str(mutant.errors))
        for _, trace in mutant.failures:
            self.assertIn("the open items were cut at 200", trace)

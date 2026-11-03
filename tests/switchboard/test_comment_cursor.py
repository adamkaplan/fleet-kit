"""#1889: the comments of a window are read incrementally (the REAL hub.read_pages over a fake gh runner that filters `since`, pages and counts); the overlap list is read whole or says how many."""
import json
import math
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.rebind import rebound
from support.loader import copy_entry, _load, sb

NOW = 1798000000.0
REPO = "acme/api"
DAY = 86400


class CommentWorld:
    """The comments of one repository as GitHub serves them: `since` is strict, newest first, 100 a page; every request and every comment served is counted."""

    def __init__(self, since_epoch, total, start=60, step=200):
        self.since_epoch = since_epoch
        self.comments = [self.make(i, since_epoch + start + i * step) for i in range(total)]
        self.requests, self.served, self.next_id = [], {}, total
        self.last = since_epoch + start + total * step

    @staticmethod
    def make(n, when):
        return {"id": n, "created_at": sb.iso(when), "body": "c%d" % n, "user": {"login": "u"}}

    def add(self, count, step=10):
        for _ in range(count):
            self.last += step
            self.comments.append(self.make(self.next_id, self.last))
            self.next_id += 1

    def gh_api(self, path, paginate=True, timeout=None):
        self.requests.append(path)
        query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
        page = int(query.get("page", "1"))
        rows = sorted((c for c in self.comments if c["created_at"] > query["since"]), key=lambda c: (c["created_at"], c["id"]), reverse=True)
        out = rows[(page - 1) * 100:page * 100]
        for c in out:
            self.served[c["id"]] = self.served.get(c["id"], 0) + 1
        return [out]


class HubCase(Base):
    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit, self.clock = [], [NOW]
        self.since = sb.iso(NOW - 7 * DAY)

    def hub(self, world):
        hub = sb.GitHubHub.__new__(sb.GitHubHub)
        hub.state_dir, hub.actor, hub.now = self.state_dir, "daemon", lambda: self.clock[0]
        hub.audit = types.SimpleNamespace(append=lambda *a: self.audit.append(a))
        hub.gh_api = world.gh_api
        return hub

    def fallbacks(self):
        return [a for a in self.audit if a[1] == "comment_cursor.fallback"]


class TestCommentCursor(HubCase):
    def world(self, total=2582):
        return CommentWorld(NOW - 7 * DAY, total)

    def test_s2_every_comment_of_the_window_is_read_once(self):
        world = self.world()
        hub = self.hub(world)
        got, cut = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual((len(got), cut), (2582, False))
        for _ in range(30):
            world.add(10)
            got, cut = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            self.assertEqual(len(got), len(world.comments))
        twice = [i for i, n in world.served.items() if n > 1]
        self.assertEqual((len(world.served), len(twice), len(world.comments)), (len(world.comments), 0, 2882), msg="every comment of the window is read once: %d read, %d read twice" % (len(world.served), len(twice)))
        control = self.world()
        capped, _ = self.hub(control).read_pages("repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100" % (REPO, self.since),
                                                 older=("created_at", self.since), cap=sb.SWEEP_LIST_PAGES)
        self.assertEqual(len(capped), sb.SWEEP_LIST_PAGES * sb.GITHUB_PAGE_SIZE)

    def test_s3_an_absent_state_an_unreadable_one_and_a_restart_read_the_window(self):
        windows = 0
        for kind in ("absent", "unreadable", "restart"):
            self.audit.clear()
            for leftover in self.state_dir.glob("comment-cursors.json"):
                leftover.unlink()
            world = self.world(300)
            if kind == "unreadable":
                (self.state_dir / "comment-cursors.json").write_text("{not json")
            if kind == "restart":
                self.hub(world).read_comments("sweep", REPO, self.since, cap=3)   # a first process writes the cursor
                world.requests.clear()
                self.audit.clear()
            hub = self.hub(world)
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            windows += ("since=%s&" % self.since) in world.requests[0]
            self.assertEqual(len(self.fallbacks()), 1, msg=kind)
        self.assertEqual(windows, 3, msg="%d of 3 reads started at the window's start" % windows)

    def test_s4_a_window_not_read_to_its_start_is_said_once_a_day(self):
        cos = types.SimpleNamespace(fleet={"role": "chief-of-staff"})
        engine = types.SimpleNamespace(state_dir=str(self.state_dir), actor="daemon", discovery=types.SimpleNamespace(agents={"cos": cos}),
                                       audit=types.SimpleNamespace(append=lambda *a: self.audit.append(a)))
        told = []
        real = sb.add_repeat_reminder
        sb.add_repeat_reminder = lambda *a, **k: told.append(a) or real(*a, **k)
        self.addCleanup(setattr, sb, "add_repeat_reminder", real)
        world = CommentWorld(NOW - 7 * DAY, 3500, start=2 * DAY, step=100)
        hub = self.hub(world)

        def one_read(now):
            self.clock[0] = now
            hub.read_comments("sweep", REPO, sb.iso(now - 7 * DAY), cap=sb.COMMENT_WINDOW_PAGES)
            for repo, (held, days) in sb.take_comment_cuts().items():
                sb.comment_window_alert(engine, repo, held, days, now)

        one_read(NOW)
        one_read(NOW + 900)
        named = [a for a in told if REPO in a[4] and "3,000" in a[4]]
        self.assertEqual(len(named), 1)
        self.assertIn("7-day", named[0][4])
        one_read(NOW + DAY)
        self.assertEqual(len([a for a in told if REPO in a[4] and "3,000" in a[4]]), 2)

    def test_s5_the_first_read_costs_the_pages_of_the_window_and_a_steady_read_one(self):
        world = self.world()
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(len(world.requests), math.ceil(len(world.comments) / sb.GITHUB_PAGE_SIZE))
        world.requests.clear()
        world.add(10)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(len(world.requests), 1)

    def test_u1_requests_per_hour_before_and_after(self):
        reads_per_hour = 3600 // sb.SWEEP_TTL
        before = reads_per_hour * sb.SWEEP_LIST_PAGES
        world = self.world()
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        first = len(world.requests)
        world.requests.clear()
        for _ in range(reads_per_hour):
            world.add(10)
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        after = len(world.requests)
        self.assertEqual((before, first, after), (8, 26, 4))

    def test_a_small_repository_reads_what_it_read_before(self):
        world = self.world(150)
        got, cut = self.hub(world).read_comments("triage", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual([c["id"] for c in got], [c["id"] for c in sorted(world.comments, key=lambda c: c["created_at"], reverse=True)])
        self.assertFalse(cut)


def fake_overlap_runner(total):
    def run(argv, **kwargs):
        limit = int(argv[argv.index("--limit") + 1])
        listing = [{"number": n, "files": [{"path": "f%d.py" % n}], "changedFiles": 1} for n in range(1, total + 1)][:limit]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listing), stderr="")
    return run


class TestOverlapListWhole(Base):
    def read(self, total):
        engine = types.SimpleNamespace(config={"gh": "gh", "call_timeout_seconds": 30}, gh_runner=None, now=lambda: NOW,
                                       accounts=types.SimpleNamespace(env_for=lambda repo: None))
        with mock.patch.object(sb, "refuse_excluded", lambda *a: None):
            listing = sb.read_open_overlap(engine, REPO, fake_overlap_runner(total))
        return listing, engine._overlap_read[REPO]

    def test_s6_every_open_pull_request_is_read_or_the_line_says_how_many(self):
        listing, read_n = self.read(118)
        self.assertEqual((len(listing), read_n), (118, 118), msg="every open pull request is read or the line says how many: %d of 118" % len(listing))
        self.assertLess(read_n, sb.OVERLAP_LIST_CAP)
        listing, read_n = self.read(250)
        self.assertEqual(read_n, sb.OVERLAP_LIST_CAP, msg="every open pull request is read or the line says how many: %d of 250" % read_n)
        self.assertEqual("read %d of at least %d open pull requests" % (read_n, read_n), "read 200 of at least 200 open pull requests")


class TestCommentCursorMutants(Base):
    """#1889 S7: the cursor ignored, and the overlap cap back at 50, each fail the test with its anchored text; the unmutated copy passes first."""
    CASES = (("cursor", "test_s2_every_comment_of_the_window_is_read_once", TestCommentCursor, "every comment of the window is read once"),
             ("overlap", "test_s6_every_open_pull_request_is_read_or_the_line_says_how_many", TestOverlapListWhole, "every open pull request is read or the line says how many"))

    def run_against(self, edit, tag, cls, name):
        with tempfile.TemporaryDirectory() as tmp:
            for other in (Path(__file__).resolve().parents[2] / "bin").glob("*.py"):
                (Path(tmp) / other.name).write_bytes(other.read_bytes())
            copy_entry(tmp)
            entry = Path(tmp) / "fleet-switchboard"
            entry.write_text(edit(entry.read_text()))
            copy = _load("t_comment_cursor_" + tag, entry)
            result = unittest.TestResult()
            with mock.patch.dict(globals(), {"sb": copy}), rebound("sb", copy):
                unittest.TestSuite([cls(name)]).run(result)
        return result

    def test_s7_the_controls_pass_and_each_mutant_fails_with_its_text(self):
        for tag, name, cls, text in self.CASES:
            control = self.run_against(lambda t: t, tag + "_control", cls, name)
            self.assertEqual((len(control.failures), len(control.errors), control.testsRun), (0, 0, 1), msg=str(control.failures + control.errors))
        edits = {"cursor": ("have, mark = held.get(key), cursors.get(name)", "have, mark = None, None"),
                 "overlap": ("OVERLAP_LIST_CAP = 200 ", "OVERLAP_LIST_CAP = 50 ")}
        for tag, name, cls, text in self.CASES:
            old, new = edits[tag]
            counts = []

            def mutate(source):
                counts.append(source.count(old))
                return source.replace(old, new, 1)

            result = self.run_against(mutate, tag, cls, name)
            self.assertEqual(counts, [1])
            self.assertEqual(len(result.failures), 1, msg=str(result.errors))
            self.assertIn(text, result.failures[0][1])

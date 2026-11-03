"""#1889: the comments of a window are read incrementally (the REAL hub.read_pages over a fake gh runner that filters `since`, pages and counts); the overlap list is read whole or says how many."""
import json
import math
import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.rebind import rebound
from support.loader import copy_entry, _load, sb
from test_open_list_cuts import real_hub

NOW = 1798000000.0
REPO = "acme/api"
DAY = 86400


class CommentWorld:
    """The comments of one repository as GitHub serves them: `since` is INCLUSIVE (>=) and filters on updated_at, newest created first, 100 a page; every request and every comment served is counted."""

    def __init__(self, since_epoch, total, start=60, step=200):
        self.since_epoch = since_epoch
        self.comments = [self.make(i, since_epoch + start + i * step) for i in range(total)]
        self.requests, self.served, self.next_id = [], {}, total
        self.last = since_epoch + start + total * step

    @staticmethod
    def make(n, when):
        return {"id": n, "created_at": sb.iso(when), "updated_at": sb.iso(when), "body": "c%d" % n, "user": {"login": "u"}}

    def add(self, count, step=10):
        for _ in range(count):
            self.last += step
            self.comments.append(self.make(self.next_id, self.last))
            self.next_id += 1

    def edit(self, n, when):
        """An edit moves updated_at only: the comment keeps its created_at."""
        self.comments[n]["updated_at"] = sb.iso(when)

    def gh_api(self, path, paginate=True, timeout=None):
        self.requests.append(path)
        query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
        page = int(query.get("page", "1"))
        rows = sorted((c for c in self.comments if c["updated_at"] >= query["since"]), key=lambda c: (c["created_at"], c["id"]), reverse=True)
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


class PublishMixin:
    """The real publish_decisions raises the comment-window alert (run inside TestCommentCursor, the one wired class)."""

    def publish_engine(self, with_cos):
        cos = {"cos": types.SimpleNamespace(fleet={"role": "chief-of-staff"})} if with_cos else {}
        return types.SimpleNamespace(config={"plan_gates": {"repos": {REPO: {}}}, "github_repos": [REPO]}, state_dir=str(self.state_dir), actor="daemon",
                                     discovery=types.SimpleNamespace(agents=cos), audit=types.SimpleNamespace(append=lambda *a: self.audit.append(a)),
                                     hub=real_hub(0), now=lambda: NOW, _note=lambda *a, **k: None)

    def publish(self, engine):
        quiet = lambda *a, **k: None
        with mock.patch.object(sb, "derive_decisions", lambda e, n: ([], [])), mock.patch.object(sb, "sweep_decisions", lambda e, d, n, errs: d), \
                mock.patch.object(sb, "triage_pass", quiet), mock.patch.object(sb, "pr_events_pass", quiet), mock.patch.object(sb, "pane_health_pass", quiet), \
                mock.patch.object(sb, "decision_store", lambda e: types.SimpleNamespace(publish=lambda *a, **k: True)), \
                mock.patch.object(sb, "chat_clear_pass", quiet), mock.patch.object(sb, "apply_notice_effects", quiet), mock.patch.object(sb, "prune_escalations_pass", quiet), \
                mock.patch.object(sb, "sync_external", lambda *a, **k: []), mock.patch.object(sb, "work_sync_external", quiet):
            sb.publish_decisions(engine)

    def test_the_real_publish_decisions_takes_the_cuts_and_alerts(self):
        sb.take_comment_cuts()
        sb._COMMENT_CUTS[("sweep", REPO)] = (3000, 7)
        self.publish(self.publish_engine(True))
        self.assertEqual([a[1] for a in self.audit if a[1].startswith("comment_window.")], ["comment_window.cut"])
        self.assertEqual(sb.take_comment_cuts(), {})

    def test_without_a_chief_of_staff_the_cut_is_one_unsent_line_a_day(self):
        sb.take_comment_cuts()
        sb._UNSENT_DAY.clear()
        self.addCleanup(sb._UNSENT_DAY.clear)
        for _ in range(2):
            sb._COMMENT_CUTS[("sweep", REPO)] = (3000, 7)
            self.publish(self.publish_engine(False))
        self.assertEqual([a[1] for a in self.audit if a[1].startswith("comment_window.")], ["comment_window.cut_unsent"])


class TestCommentCursor(PublishMixin, HubCase):
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
        ids = [c["id"] for c in got]   # the boundary comment is served again (since is inclusive) and merged by id: distinct ids are what is read once
        twice = sum(n - 1 for n in world.served.values()) - 30   # the only re-served comment of each of the 30 steady reads is the boundary one
        twice += len(ids) - len(set(ids))
        self.assertEqual((len(set(world.served)), twice, len(world.comments)), (len(world.comments), 0, 2882), msg="every comment of the window is read once: %d read, %d read twice" % (len(set(world.served)), twice))
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

    def test_the_boundary_comment_and_an_edited_old_comment_are_served_again_and_merged_once(self):
        world = self.world(150)
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        newest = max(c["created_at"] for c in world.comments)
        world.requests.clear()
        world.served.clear()
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)   # nothing new: the comment created at the cursor is served again
        self.assertEqual(world.served, {149: 1})
        self.assertEqual((len(got), got[0]["created_at"]), (150, newest))
        world.edit(3, NOW + 5)   # an old comment edited after the cursor
        world.served.clear()
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(sorted(world.served), [3, 149])
        ids = [c["id"] for c in got]
        self.assertEqual((len(ids), len(set(ids)), 3 in ids), (150, 150, True))
        self.assertEqual(next(c for c in got if c["id"] == 3)["updated_at"], sb.iso(NOW + 5))

    def test_equal_second_comments_keep_the_api_order_and_latest_strong_sees_the_same_one(self):
        world = CommentWorld(NOW - 7 * DAY, 0)
        when = NOW - DAY
        world.comments = [CommentWorld.make(9, when), CommentWorld.make(10, when), CommentWorld.make(5, when - 100)]
        hub = self.hub(world)
        old, _ = hub.read_pages("repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100" % (REPO, self.since), older=("created_at", self.since), cap=2)
        self.assertEqual([c["id"] for c in old], [10, 9, 5])
        got, _ = self.hub(world).read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual([c["id"] for c in got], [10, 9, 5])
        self.assertEqual(got[0]["id"], old[0]["id"])   # the comment a check sees first is the same as before
        numeric = CommentWorld(NOW - 7 * DAY, 0)
        numeric.comments = [CommentWorld.make(100, when), CommentWorld.make(99, when)]   # ids compared as strings would put 99 first
        got, _ = self.hub(numeric).read_comments("sweep", REPO, self.since, cap=2)
        self.assertEqual([c["id"] for c in got], [100, 99])

    def test_records_without_a_valid_created_at_or_an_id_are_kept(self):
        world = CommentWorld(NOW - 7 * DAY, 0)
        good = CommentWorld.make(1, NOW - DAY)
        world.comments = [good]
        hub = self.hub(world)
        listing = [[good, {"id": 2, "body": "no date"}, {"id": 3, "created_at": 5, "body": "bad date"},
                    {"created_at": sb.iso(NOW - 2 * DAY), "body": "a"}, {"created_at": sb.iso(NOW - 2 * DAY), "body": "b"}]]
        hub.gh_api = lambda path, paginate=True, timeout=None: listing
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=1)
        self.assertEqual(sorted(c["body"] for c in got), ["a", "b", "bad date", "c1", "no date"])
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=1)   # served again: the id-less pair stays two, not four
        self.assertEqual(sorted(c["body"] for c in got), ["a", "b", "bad date", "c1", "no date"])

    def test_each_consumer_keeps_its_own_cut_entry(self):
        sb.take_comment_cuts()
        world = CommentWorld(NOW - 7 * DAY, 3500, start=2 * DAY, step=100)
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        hub.read_comments("triage", REPO, sb.iso(NOW - 14 * DAY), cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(sorted(sb._COMMENT_CUTS), [("sweep", REPO), ("triage", REPO)])
        taken = sb.take_comment_cuts()
        self.assertEqual(list(taken), [REPO])   # one alert a day per repository
        self.assertEqual(sb._COMMENT_CUTS, {})

    def test_a_sliding_window_drops_the_held_comments_older_than_it(self):
        world = self.world(150)   # comments from since+60s, 200 s apart: about 8 hours
        hub = self.hub(world)
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(len(got), 150)
        later = sb.iso(NOW - 7 * DAY + 60 + 100 * 200)   # the window's start passes the oldest 100
        got, _ = hub.read_comments("sweep", REPO, later, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(len(got), 50, msg="held comments older than the window drop")
        self.assertTrue(all(c["created_at"] >= later for c in got))

    def test_the_fallback_audit_is_one_line_over_a_window_read_and_a_later_incremental_read(self):
        world = self.world(300)
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        world.add(5)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(len(self.fallbacks()), 1, msg="the fallback is written once, not at every read")

    def test_the_cursor_file_is_written_only_on_a_change_mode_0600_and_a_failed_replace_leaves_the_old_one(self):
        path = self.state_dir / "comment-cursors.json"
        world = self.world(50)
        hub = self.hub(world)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        os.utime(path, ns=(10 ** 18, 10 ** 18))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)   # no new comment
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before, msg="no write without a change")
        os.chmod(path, 0o644)
        world.add(1)
        hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertNotEqual(path.read_bytes(), before[0])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        kept = path.read_bytes()
        world.add(1)
        store = sb.load_module("comment_cursor")
        with mock.patch.object(store.os, "replace", side_effect=OSError("boom")):
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual(path.read_bytes(), kept)

    def test_the_cursor_is_saved_once_and_not_at_every_read(self):
        world = self.world(50)
        hub = self.hub(world)
        store = sb.load_module("comment_cursor")
        with mock.patch.object(store, "save", wraps=store.save) as save:
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            self.assertEqual(save.call_count, 1, msg="control: the first read saves the cursor once")
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            self.assertEqual(save.call_count, 1, msg="no write without a change: store.save is called %d times over 3 reads with one change" % save.call_count)
            world.add(1)
            hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
            self.assertEqual(save.call_count, 2)

    def test_records_without_a_valid_created_at_are_held_through_an_incremental_read(self):
        world = CommentWorld(NOW - 7 * DAY, 3)
        hub = self.hub(world)
        odd = {"id": 900, "body": "no date"}
        real = world.gh_api
        hub.gh_api = lambda path, paginate=True, timeout=None: [real(path)[0] + ([odd] if "page=1" in path or "page=" not in path else [])]
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertIn("no date", [c["body"] for c in got])
        hub.gh_api = real   # the incremental read no longer serves it: the held record stays, once
        world.add(1)
        got, _ = hub.read_comments("sweep", REPO, self.since, cap=sb.COMMENT_WINDOW_PAGES)
        self.assertEqual([c["body"] for c in got].count("no date"), 1, msg="a record without a valid created_at is held, not dropped")
        self.assertEqual(len(got), 5)

    def test_both_windows_are_named_when_both_consumers_are_cut(self):
        sb.take_comment_cuts()
        sb._COMMENT_CUTS[("sweep", REPO)] = (3000, 7)
        sb._COMMENT_CUTS[("triage", REPO)] = (3000, 14)
        taken = sb.take_comment_cuts()
        sent = []
        engine = self.publish_engine(True)
        with mock.patch.object(sb, "add_repeat_reminder", lambda *a, **k: sent.append(a[4])):
            for repo, (held, days) in taken.items():
                sb.comment_window_alert(engine, repo, held, days, NOW)
        self.assertEqual(len(sent), 1)
        self.assertIn("7-day and 14-day windows", sent[0])

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

    def test_the_gates_line_is_written_by_cmd_gates_from_the_read_count(self):
        """Limit: the real `cmd_gates` needs the heavy host fixture (LedgerBase of bin/test-switchboard, outside this file); the read count is tested through the real
        `read_open_overlap` above and the formatting is pinned here in the source of `cmd_gates`, which holds it inline."""
        import inspect
        source = inspect.getsource(sb.cmd_gates)
        self.assertIn('read_n = getattr(ctx.engine, "_overlap_read", {}).get(ctx.repo, 0)', source)
        self.assertIn("if read_n >= OVERLAP_LIST_CAP:", source)
        self.assertIn('lines.append("read %d of at least %d open pull requests" % (read_n, read_n))', source)


class TestCommentCursorMutants(Base):
    """#1889 S7: the cursor ignored, and the overlap cap back at 50, each fail the test with its anchored text; the unmutated copy passes first."""
    CASES = (("cursor", "test_s2_every_comment_of_the_window_is_read_once", TestCommentCursor, "every comment of the window is read once"),
             ("overlap", "test_s6_every_open_pull_request_is_read_or_the_line_says_how_many", TestOverlapListWhole, "every open pull request is read or the line says how many"),
             ("prune", "test_a_sliding_window_drops_the_held_comments_older_than_it", TestCommentCursor, "held comments older than the window drop"))

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
                 "overlap": ("OVERLAP_LIST_CAP = 200 ", "OVERLAP_LIST_CAP = 50 "),
                 "prune": ('        have = {k: v for k, v in have.items() if v["created_at"] >= since}\n', '        have = {k: v for k, v in have.items()}\n')}
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

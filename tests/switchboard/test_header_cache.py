"""The shared file cache of the ask header (#1520): scenarios S2-S6, S9 and U1, U2 of the plan (rev 2). Plain fakes, no daemon.

A fake gh counts the requests it is asked. The reader is the real IntentReader over the real GhAccounts; the command running is set the way `dispatch` sets it."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb


class FakeGh:
    """`gh api repos/R/issues/N` and `.../parent`: counts every request; the bodies can be changed between reads."""

    def __init__(self):
        self.requests = []
        self.not_modified = 0   # requests answered 304: free against the budget
        self.conditional = []   # the requests that sent a validator
        self.issues = {}     # number -> dict(state, body)
        self.parents = {}    # number -> parent number

    def issue(self, number, intent="Keep the header", done="the header is shown", state="open"):
        self.issues[number] = {"number": number, "state": state, "updated_at": "2026-10-29T01:00:00Z", "title": "t",
                               "body": "## Intent\n\n%s\n\nDone when: %s\n" % (intent, done), "extra": "PLANTED-FIELD"}

    def __call__(self, argv, **kw):
        done = self.answer(argv)
        if "-i" not in argv or done.returncode != 0:
            return done
        import hashlib
        etag = '"%s"' % hashlib.sha1(done.stdout.encode()).hexdigest()[:16]
        sent = [argv[i + 1].split(": ", 1)[1] for i, a in enumerate(argv[:-1]) if a == "-H" and argv[i + 1].startswith("If-None-Match: ")]
        if sent:
            self.conditional.append(argv[-1])
        if sent and sent[0] == etag:
            self.not_modified += 1
            return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 304 Not Modified\r\netag: %s\r\n\r\n" % etag, "")
        return subprocess.CompletedProcess(argv, 0, "HTTP/2.0 200 OK\r\netag: %s\r\n\r\n%s" % (etag, done.stdout), "")

    def budget_counted(self):
        return len(self.requests) - self.not_modified

    def answer(self, argv):
        path = argv[-1]
        self.requests.append(path)
        parts = path.split("/")   # repos / owner / name / issues / N [/ parent]
        number = int(parts[4])
        if len(parts) == 6:
            if number in self.parents:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.issues.get(self.parents[number], {"number": self.parents[number]})), "")
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        if number not in self.issues:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.issues[number]), "")


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class HeaderCacheBase(unittest.TestCase):
    REPO = "acme/widgets"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name, "GH_TOKEN": "TOKEN-NEVER-STORED"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh, self.clock = FakeGh(), Clock()
        self.gh.issue(5)
        self.gh.issue(7, intent="The parent ask")
        self.gh.parents[5] = 7
        self.config = {"gh": "gh", "call_timeout_seconds": 5, "gh_users": {}, "excluded_repos": []}
        self.action(None)
        self.addCleanup(self.action, None)

    def action(self, name, fresh=False):
        sb.HEADER_CACHE_ACTION[0], sb.HEADER_CACHE_FRESH[0] = (sb.header_cache_action(name) if name else None), fresh

    def reader(self, config=None):
        """A new reader: a new process, as every command is (its own in-memory cache is empty)."""
        config = self.config if config is None else config
        reader = sb.IntentReader("gh", self.REPO, self.gh, self.clock, 5, accounts=sb.GhAccounts(config, self.gh, self.clock))
        reader.config = config
        return reader

    def header(self, number=5, config=None):
        return self.reader(config).header(number, self.REPO)

    def count(self, since):
        return len(self.gh.requests) - since

    def files(self):
        directory = Path(sb.state_dir_path()) / "header-cache"
        return sorted(directory.glob("*.json")) if directory.is_dir() else []


class TestAges(HeaderCacheBase):
    def test_second_display_command_inside_the_age_makes_zero_reads(self):   # S2
        self.action("send")
        first = self.header()
        made = len(self.gh.requests)
        self.assertGreaterEqual(made, 2)   # the issue and its parent (and the parent's own parent check)
        self.clock.t += 300
        self.action("pending")
        self.assertEqual(self.header(), first)
        self.assertEqual(self.count(made), 0)

    def test_after_the_open_age_one_read_is_made(self):   # S2
        self.action("send")
        self.header()
        made = len(self.gh.requests)
        self.clock.t += 11 * 60
        self.header()
        self.assertGreater(self.count(made), 0)
        again = len(self.gh.requests)
        self.header()
        self.assertEqual(self.count(again), 0, "the read at 11 minutes stored a new entry")

    def test_the_refresh_after_the_age_is_conditional_and_an_unchanged_issue_costs_nothing(self):   # S7 (rev 3)
        self.action("send")
        first = self.header()
        self.clock.t += 11 * 60
        self.assertEqual(self.header(), first)   # the first refresh has no validator stored: it asks for one (a plain counted read)
        counted, free = self.gh.budget_counted(), self.gh.not_modified
        self.clock.t += 11 * 60
        self.assertEqual(self.header(), first)
        self.assertLessEqual(self.gh.budget_counted() - counted, 1)   # every refresh with a validator was a 304; only the 404 of "no parent" has none and is read plainly
        self.assertGreater(self.gh.not_modified, free)
        self.assertGreater(len(self.gh.conditional), 0)
        self.gh.issue(5, intent="Changed intent")   # a changed issue answers 200 and the header is the new one
        self.clock.t += 11 * 60
        self.assertIn("Changed intent", " ".join(self.header()[1]))

    def test_closed_issue_is_served_a_day_and_read_again_after(self):   # S2
        self.gh.issue(5, state="closed")
        self.gh.issue(7, state="closed")
        self.action("report")
        self.header()
        self.reader().issue(7, self.REPO)   # a parent is as old as its own state: the entry of 7 knows it is closed
        made = len(self.gh.requests)
        self.clock.t += 23 * 3600
        self.header()
        self.assertEqual(self.count(made), 0)
        self.clock.t += 2 * 3600   # 25 h
        self.header()
        self.assertGreater(self.count(made), 0)

    def test_mutant_that_ignores_the_age_fails(self):   # S9: the age is what bounds a cache the webhook did not drop
        self.action("send")
        self.header()
        made = len(self.gh.requests)
        self.clock.t += 3 * 3600
        with mock.patch.object(sb.load_module("header_cache"), "age_for", lambda state: 10 ** 9):   # the mutant
            self.header()
        self.assertEqual(self.count(made), 0, "the mutant serves a 3 hour old open entry")
        self.header()   # the real one reads again
        self.assertGreater(self.count(made), 0)

    def test_header_changed_within_the_age_is_served_old_by_display_and_new_by_always_fresh(self):   # U1
        self.action("send")
        old = self.header()
        self.clock.t += 300
        self.gh.issue(5, intent="A changed intent")
        self.action("send")
        self.assertEqual(self.header(), old)
        self.action("gates-check")
        self.assertIn("A changed intent", " ".join(self.header()[1]))
        self.clock.t += 400   # 11+ minutes after the entry
        self.action("send")
        self.assertIn("A changed intent", " ".join(self.header()[1]))

    def test_failed_read_is_never_cached(self):   # PC-1
        self.action("send")
        with self.assertRaises(sb.IntentError):
            self.header(99)
        self.assertEqual(self.files(), [])


class TestSets(HeaderCacheBase):
    def test_always_fresh_commands_read_despite_a_warm_entry(self):   # S3
        self.action("send")
        self.header()
        warm = len(self.gh.requests)
        fresh = sorted(sb.always_fresh_commands())
        self.assertIn("plan", fresh)
        self.assertIn("verdict", fresh)
        for name in fresh:
            before = len(self.gh.requests)
            self.action(name)
            self.header()
            self.assertGreater(self.count(before), 0, "%s served the warm entry" % name)

    def test_charter_set_is_always_fresh_and_never_cache_allowed(self):   # S3/S4: the charter-set guard's label and parent reads decide the charter set
        self.assertIn("charter", sb.always_fresh_commands())
        self.assertNotIn("charter", sb.cache_allowed_commands())
        self.action("send")
        self.header()
        before = len(self.gh.requests)
        self.action("charter")
        self.header()
        self.assertGreater(self.count(before), 0, "charter served the warm entry")

    def test_mutant_that_lets_an_always_fresh_command_use_the_cache_fails(self):   # S3
        self.action("send")
        self.header()
        with mock.patch.object(sb, "always_fresh_commands", lambda: set()), mock.patch.object(sb, "cache_allowed_commands", lambda: {"plan"}):   # the mutant
            before = len(self.gh.requests)
            self.action("plan")
            self.header()
            self.assertEqual(self.count(before), 0, "the mutant reads the cache")
        before = len(self.gh.requests)
        self.action("plan")
        self.header()
        self.assertGreater(self.count(before), 0)

    def test_fresh_flag_and_a_write_to_the_issue_drop_the_entry(self):   # S3
        self.action("send")
        self.header()
        before = len(self.gh.requests)
        self.action("send", fresh=True)
        self.header()
        self.assertGreater(self.count(before), 0)
        self.action("send")
        self.header()
        warm = len(self.gh.requests)
        engine = mock.Mock(config=self.config, accounts=None)
        sb._gh_write(engine, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""), self.REPO, "repos/%s/issues/5/comments" % self.REPO, "POST", {"body": "x"})
        self.header()
        self.assertGreater(self.count(warm), 0)

    def write_to_issue(self):
        engine = mock.Mock(config=self.config, accounts=None)
        sb._gh_write(engine, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""), self.REPO, "repos/%s/issues/5/comments" % self.REPO, "POST", {"body": "x"})

    def warm_entry_survives_a_write_by(self, name):
        """True when a warm entry still serves a read after `name` has written to the issue."""
        self.action("send")
        self.header()
        self.action(name)
        self.write_to_issue()
        self.action("send")
        mark = len(self.gh.requests)
        self.header()
        return self.count(mark) == 0

    def test_a_write_by_an_always_fresh_command_drops_a_warm_entry(self):   # done-when 2
        for name in ("plan", "gates", "merge-gate"):
            self.assertFalse(self.warm_entry_survives_a_write_by(name), name)

    def test_a_write_by_a_cache_allowed_command_still_drops_it(self):
        self.assertFalse(self.warm_entry_survives_a_write_by("send"))

    def test_a_write_with_no_command_set_drops_it_too(self):
        self.assertFalse(self.warm_entry_survives_a_write_by(None))

    def test_a_delete_failure_never_raises(self):
        self.action("send")
        self.header()
        with mock.patch.object(os, "unlink", side_effect=PermissionError("no")):
            self.write_to_issue()
        with mock.patch.object(os, "listdir", side_effect=OSError("gone")):
            self.write_to_issue()

    def test_the_old_behaviour_only_cache_allowed_writers_drop_fails_the_test(self):   # the mutant
        hc = sb.load_module("header_cache")
        real = hc.drop
        hc.drop = lambda *a, **k: real(*a, **k) if sb.HEADER_CACHE_ACTION[0] is not None else 0
        try:
            self.action("plan")
            self.assertIsNone(sb.HEADER_CACHE_ACTION[0])
            self.assertTrue(self.warm_entry_survives_a_write_by("plan"))   # the mutant keeps the entry: the test above would fail
        finally:
            hc.drop = real

    def test_every_command_that_reads_the_header_is_in_a_set(self):   # S4
        self.assertEqual(self.unplaced(), [])

    def test_a_planted_recording_command_in_neither_set_is_named(self):   # S4
        def cmd_planted_thing(args, runner, now):
            return sb.intent_reader_for({}, runner, now)   # reads the header
        cmd_planted_thing.records = "proof"
        with mock.patch.dict(sb.__dict__, {"cmd_planted_thing": cmd_planted_thing}):
            self.assertIn("planted-thing", sb.always_fresh_commands())   # a recording command is fresh by derivation
            sb.cmd_planted_thing.records = None
            self.assertEqual(self.unplaced(), ["planted-thing"])

    def unplaced(self):
        """The commands whose handler reads the header itself (its source names the reader or the header) and are in neither set: derived from the code, not retyped."""
        import inspect
        fresh, allowed = sb.always_fresh_commands(), sb.cache_allowed_commands()
        readers = ("intent_reader_for", "IntentReader(", "_intent_reader(", ".header(", ".intents.issue(", ".intents.parent(", ".ask_of(", ".missing_intent(")
        registered = sb.build_parser().commands
        found = []
        for name, fn in sorted(vars(sb).items()):
            if not (name.startswith("cmd_") and callable(fn)):
                continue
            action = name[4:].replace("_", "-")
            try:
                source = inspect.getsource(fn)
            except (OSError, TypeError):
                continue
            if any(word in source for word in readers) and action not in fresh | allowed and (action in registered or name == "cmd_planted_thing"):
                found.append(action)
        return found

    def test_the_handler_flags_dispatch_reads_agree_with_the_derived_sets(self):   # S4
        registered = sb.build_parser().commands
        for action in sorted(registered):
            handler = sb.resolve_command(action, registered)
            self.assertEqual(sb.handler_header_action(action, handler), sb.header_cache_action(action), action)

    def test_the_environment_only_makes_a_read_fresher(self):   # rev 3: FLEET_HEADER_FRESH=1 drops; no environment value enables the cache or sets an age
        registered = sb.build_parser().commands
        seen = []
        def spy(*a, **k):
            seen.append((sb.HEADER_CACHE_ACTION[0], sb.HEADER_CACHE_FRESH[0]))
            return 0
        args = mock.Mock(action="send")
        for env, want in (({}, False), ({"FLEET_HEADER_FRESH": "1"}, True), ({"FLEET_HEADER_FRESH": "0"}, False), ({"FLEET_HEADER_CACHE": "on", "FLEET_HEADER_AGE": "99999"}, False)):
            with mock.patch.dict(os.environ, env), mock.patch.object(sb, "_dispatch", spy):
                os.environ.pop("FLEET_HEADER_FRESH", None) if "FLEET_HEADER_FRESH" not in env else None
                sb.dispatch(args, registered)
            self.assertEqual(seen[-1], ("send", want), env)
        self.assertEqual((sb.HEADER_CACHE_ACTION[0], sb.HEADER_CACHE_FRESH[0]), (None, False))
        self.assertNotIn("--fresh", (Path(__file__).resolve().parents[2] / "bin" / "header_cache.py").read_text())

    def test_the_sets_do_not_overlap(self):
        self.assertEqual(sb.always_fresh_commands() & sb.cache_allowed_commands(), set())
        self.assertTrue({"send", "report", "comment", "pending", "remind", "status"} <= sb.cache_allowed_commands())


class TestEvents(HeaderCacheBase):
    def hub(self):
        reader = self.reader()
        engine = mock.Mock(intents=reader)
        return reader, engine

    def test_reopened_edited_transferred_and_closed_events_drop_the_entry(self):   # S5
        for action in ("reopened", "edited", "transferred", "closed"):
            with self.subTest(action):
                self.gh.issue(5, state="closed")
                self.action("send")
                self.header()
                warm = len(self.gh.requests)
                self.header()
                self.assertEqual(self.count(warm), 0)
                reader, engine = self.hub()
                sb.GitHubHub.note_issue_edit(mock.Mock(engine=engine, lock=threading.RLock(), intent_edits={}), "issues", self.REPO,
                                             {"action": action, "issue": {"number": 5, "state": "open"}})
                self.header()
                self.assertGreater(self.count(warm), 0, action)

    def test_no_event_webhook_down_is_served_only_until_the_age(self):   # S5
        self.action("send")
        self.header()
        warm = len(self.gh.requests)
        self.clock.t += 9 * 60
        self.header()
        self.assertEqual(self.count(warm), 0)
        self.clock.t += 2 * 60
        self.header()
        self.assertGreater(self.count(warm), 0)


class TestSafety(HeaderCacheBase):
    def test_two_writers_do_not_corrupt_an_entry(self):   # S6
        hc = sb.load_module("header_cache")
        issue = {"number": 5, "state": "open", "intent": "i", "done_when": "d", "updated_at": "u", "charter": False}

        def write(k):
            for n in range(60):
                hc.store(sb.state_dir_path(), "default", self.REPO, 5, "issue" if (n + k) % 2 else "parent", issue if (n + k) % 2 else {"parent": 7}, self.clock())

        threads = [threading.Thread(target=write, args=(k,)) for k in (0, 1)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        (path,) = self.files()
        data = json.loads(path.read_text())
        self.assertEqual(data["number"], 5)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)   # U2
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual([p for p in path.parent.iterdir() if p.suffix == ".tmp"], [])

    def test_one_issue_per_file_and_only_the_named_fields(self):   # S9, U2
        self.action("send")
        self.header()
        files = self.files()
        self.assertEqual(len(files), 2)   # issue 5 and issue 7
        text = " ".join(f.read_text() for f in files)
        for planted in ("PLANTED-FIELD", "TOKEN-NEVER-STORED", "updated_at\": \"2026-10-29T01:00:00Z\", \"title"):
            self.assertNotIn(planted, text)
        allowed = {"v", "state", "account", "repo", "number", "issue", "parent"}
        named = set(sb.load_module("header_cache").ISSUE_FIELDS) | {"at", "etag"}
        for f in files:
            data = json.loads(f.read_text())
            self.assertLessEqual(set(data), allowed)
            self.assertLessEqual(set(data.get("issue", {})), named)
            self.assertEqual(data["number"] in (5, 7), True)
            self.assertNotIn("title", text)

    def test_other_account_gets_no_cached_body(self):   # S6
        self.action("send")
        config_a = dict(self.config, gh_users={self.REPO: "alice"})
        config_b = dict(self.config, gh_users={self.REPO: "bob"})

        def tokens(argv, **kw):
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0 if argv[-1] == "alice" else 1, "tok-alice\n" if argv[-1] == "alice" else "", "no token")
            return self.gh(argv, **kw)
        for config in (config_a,):
            reader = sb.IntentReader("gh", self.REPO, tokens, self.clock, 5, accounts=sb.GhAccounts(config, tokens, self.clock))
            reader.config = config
            reader.header(5, self.REPO)
        before = len(self.gh.requests)
        reader = sb.IntentReader("gh", self.REPO, tokens, self.clock, 5, accounts=sb.GhAccounts(config_b, tokens, self.clock))
        reader.config = config_b
        with self.assertRaises(sb.IntentError):   # bob has no token: refused before the cache, no body of alice's
            reader.header(5, self.REPO)
        self.assertEqual(self.count(before), 0)
        self.assertEqual(len(self.files()), 2)
        # a second account that CAN read gets its own entry, not alice's
        config_c = dict(self.config, gh_users={self.REPO: "alice"}, excluded_repos=[self.REPO])
        reader = sb.IntentReader("gh", self.REPO, tokens, self.clock, 5, accounts=sb.GhAccounts(config_c, tokens, self.clock))
        reader.config = config_c
        with self.assertRaises(sb.IntentError):   # excluded: no cached body either
            reader.header(5, self.REPO)

    def test_damaged_cache_is_answered_fresh_without_an_error(self):   # S6, U2
        self.action("send")
        expected = self.header()
        hc = sb.load_module("header_cache")
        cases = {}
        directory = Path(sb.state_dir_path()) / "header-cache"
        for name in ("corrupt", "oversized", "unreadable", "unwritable"):
            for f in self.files():
                f.write_text("{}")
            files = self.files()
            if name == "corrupt":
                for f in files:
                    f.write_text("{not json")
            elif name == "oversized":
                for f in files:
                    f.write_text(json.dumps({"v": 1, "pad": "x" * (hc.MAX_BYTES + 10)}))
            elif name == "unreadable":
                for f in files:
                    os.chmod(f, 0)
            else:
                for f in files:
                    f.unlink()
                os.chmod(directory, 0o500)
            before = len(self.gh.requests)
            try:
                self.assertEqual(self.header(), expected, name)
            finally:
                os.chmod(directory, 0o700)
                for f in self.files():
                    os.chmod(f, 0o600)
            self.assertGreater(self.count(before), 0, name)

    def test_setting_off_reads_fresh_every_time(self):   # S6
        self.action("send")
        off = dict(self.config, header_cache="off")
        self.header(config=off)
        self.assertEqual(self.files(), [])
        before = len(self.gh.requests)
        self.header(config=off)
        self.assertGreater(self.count(before), 0)
        self.assertTrue(sb.load_module("header_cache").enabled({}))
        self.assertFalse(sb.load_module("header_cache").enabled({"header_cache": "off"}))

    def test_entries_are_bounded_oldest_first(self):   # S6
        hc = sb.load_module("header_cache")
        issue = {"number": 1, "state": "open", "intent": "i", "done_when": "d", "updated_at": "u", "charter": False}
        with mock.patch.object(hc, "MAX_ENTRIES", 5):
            for n in range(12):
                os.utime(sb.state_dir_path(), None) if Path(sb.state_dir_path()).exists() else None
                hc.store(sb.state_dir_path(), "default", self.REPO, n, "issue", dict(issue, number=n), self.clock())
        self.assertLessEqual(len(self.files()), 5)


class TestReplay(HeaderCacheBase):
    """S7: a SYNTHETIC hour of the shape of the audit hour 01:00-02:00Z of 29 Oct (the recorded hour is private): the command kinds and counts, neutral issue numbers."""
    HOUR = (("send", 548), ("remind", 113), ("plan", 22), ("comment", 16), ("verdict", 14), ("notice", 12), ("send", 13))   # v2.synthetic, reminder.add, plan.approved, github.comment, plan.proof, notice.pr, cloud.send
    ISSUES = 60

    def replay(self, cache_on, shape):
        """(budget-counted reads, plain request count) of the synthetic hour. shape: "burst" (an ask is worked in a burst), "uniform" (each issue hit evenly over the hour: command i at i/N of the hour, issue i mod 60) or "random" (random issue and time)."""
        import random
        rng = random.Random(1520)
        for n in range(1, self.ISSUES + 1):
            self.gh.issue(1000 + n)
            self.gh.parents[1000 + n] = 900 + n // 10
        for n in range(90, 97):
            self.gh.issue(900 + n - 90)
        commands = [name for name, count in self.HOUR for _ in range(count)]
        rng.shuffle(commands)
        stamps = sorted(rng.uniform(0, 3600) for _ in commands)
        if shape == "uniform":
            stamps = [3600.0 * i / len(commands) for i in range(len(commands))]
        config = dict(self.config, header_cache="on" if cache_on else "off")
        start, begun, free = self.clock.t, len(self.gh.requests), self.gh.not_modified
        for i, (name, at) in enumerate(zip(commands, stamps)):
            self.clock.t = start + at
            number = 1000 + 1 + {"burst": int(at / 3600 * self.ISSUES), "uniform": i % self.ISSUES}.get(shape, rng.randrange(self.ISSUES))
            self.action(name if name in sb.always_fresh_commands() or name in sb.cache_allowed_commands() else "send")
            try:
                self.header(number, config=config)
            except sb.IntentError:
                pass
        plain = len(self.gh.requests) - begun
        return plain - (self.gh.not_modified - free), plain

    def figures(self, shape):
        without = self.replay(False, shape)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_STATE_HOME"] = self.tmp.name
        with_cache = self.replay(True, shape)
        print("\nS7 replay (%s%s): budget-counted without the cache %d, with it %d (%.2f%%); plain requests without %d, with %d (of which %d answered 304)"
              % (shape, {"burst": ", the burst shape", "uniform": ", evenly spread", "random": ", random spread"}[shape], without[0], with_cache[0], 100.0 * with_cache[0] / without[0], without[1], with_cache[1], with_cache[1] - with_cache[0]))
        return without, with_cache

    def test_burst_shape_counted_reads_are_at_most_a_fifth(self):   # S7
        without, with_cache = self.figures("burst")
        self.assertLessEqual(with_cache[0] * 5, without[0])

    def test_uniform_shape_counted_reads_are_at_most_a_fifth(self):   # S7: the bar is 20 percent, never moved
        without, with_cache = self.figures("uniform")
        self.assertLessEqual(with_cache[0] * 5, without[0])

    def test_random_spread_figure_is_measured_too(self):   # S7: quoted, not asserted at 20 percent
        without, with_cache = self.figures("random")
        self.assertLess(with_cache[0], without[0])

    def test_a_plain_refresh_after_the_age_would_fail_the_uniform_bar(self):   # S7 mutant: no validator sent
        hc = sb.load_module("header_cache")
        with mock.patch.object(hc, "stale", lambda *a, **k: None):
            without, with_cache = self.figures("uniform")
        self.assertGreater(with_cache[0] * 5, without[0])


class TestIsolation(HeaderCacheBase):   # S11
    def other_state(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        return other.name

    def stray_is_served(self):
        """Store an entry under this test's state directory, copy its file into another state directory under the name that directory would use; True when a read under the other one is served from it."""
        self.action("send")
        self.header()
        strays = self.files()
        hc = sb.load_module("header_cache")
        other = self.other_state()
        before = Path(sb.state_dir_path())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": other}):
            target = Path(sb.state_dir_path()) / hc.DIR_NAME
            target.mkdir(parents=True)
            account = sb.GhAccounts(self.config, self.gh, self.clock).describe(self.REPO)
            for stray in strays:   # each file under the name the other directory would give that issue
                number = json.loads(stray.read_text())["number"]
                (target / hc._name(sb.state_dir_path(), account, self.REPO, number)).write_bytes(stray.read_bytes())
            self.assertNotEqual(before, Path(sb.state_dir_path()))
            mark = len(self.gh.requests)
            self.header()
            return self.count(mark) == 0

    def test_a_stray_entry_in_another_state_directory_is_never_served(self):
        self.assertFalse(self.stray_is_served())

    def test_a_cache_keyed_by_the_issue_number_alone_fails_it(self):   # the mutant
        hc = sb.load_module("header_cache")
        with mock.patch.object(hc, "_state_key", lambda state: "same"):
            self.assertTrue(self.stray_is_served())   # served across directories: the test above would fail

    def test_every_test_gets_its_own_state_directory_and_one_shared_directory_is_caught(self):
        first = Path(sb.state_dir_path())
        self.tmp.cleanup()
        self.setUp()
        self.assertNotEqual(first, Path(sb.state_dir_path()))
        # the mutant: two tests share one directory, and the second is served the first's entry
        self.action("send")
        self.header()
        mark = len(self.gh.requests)
        self.header()
        self.assertEqual(self.count(mark), 0)   # the same directory serves it: only a distinct directory per test prevents this

    def test_under_the_test_runner_a_state_directory_the_test_did_not_set_is_not_used(self):
        hc = sb.load_module("header_cache")
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.dict(os.environ, {"FLEET_SWITCHBOARD_UNDER_TEST": "1"}):
            os.environ.pop("XDG_STATE_HOME", None)
            self.assertFalse(hc.store(home, "a", self.REPO, 5, "parent", {"parent": 1}, 1.0))
            self.assertIsNone(hc.load(home, "a", self.REPO, 5, "parent", 1.0))
            self.assertFalse(Path(home, hc.DIR_NAME).exists())
            os.environ["XDG_STATE_HOME"] = self.tmp.name
            self.assertFalse(hc.usable(home))
            self.assertTrue(hc.usable(os.path.join(self.tmp.name, "x")))


class TestDocs(unittest.TestCase):
    def test_docs_say_the_six_facts(self):   # S10
        text = (Path(__file__).resolve().parents[2] / "docs" / "switchboard.md").read_text()
        start = text.index("Header cache (#1520)")
        section = text[start:start + 6000]
        for fact in ("header-cache", "ALWAYS-FRESH", "CACHE-ALLOWED", "10 minutes", "24 hours", "FLEET_HEADER_FRESH=1", "header_cache", "restart"):
            self.assertIn(fact, section)


if __name__ == "__main__":
    unittest.main()

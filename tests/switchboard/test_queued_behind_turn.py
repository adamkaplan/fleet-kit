"""#1801: a queued message says it queued behind a turn and how long it waited (S2 to S5, S7, S8).

REAL here: the daemon's compose, Engine.deliver, the pass step, queued-wait.json, the audit file and the pending command. FAKE: the session
(`ExternalWorld`-style fakes of v2 and herdr in support/): its inbox, its turn and its clock; the consumption of a queued item is the test
removing it from the fake inbox, because the fake is what stands for the session reading it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from support.core import Base
from support.herdr_world import World
from support.loader import captured, sb
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE

WAIT = 58   # minutes a queued message waited; asserted over PENDING_WAIT_MINUTES in the tests
IN_TURN_TEXT = "a message admitted while the session is in a turn says it queued behind a turn"
WAITED_TEXT = "a consumed message says how long it waited from delivery to read"
WORDS = "queued behind a turn"
IDLE_STEP, IDLE_OFFSET = 61, 7   # seconds between the idle fixtures (one more than a minute, so the minute digit moves)
ROOT = Path(__file__).resolve().parents[2]

# The first header line of 20 messages admitted to an IDLE session, recorded by running idle_headers() on the head of #1842 (03eb3175) BEFORE
# the change of #1801.
RECORDED = json.loads(Path(__file__).with_name("fixtures").joinpath("queued_behind_turn_idle_headers.json").read_text()) \
    if Path(__file__).with_name("fixtures").joinpath("queued_behind_turn_idle_headers.json").exists() else None


def fresh_module():
    """A FRESH module object of bin/queued_wait.py, exec'd by path as the daemon loads modules."""
    module = types.ModuleType("queued_wait_fresh")
    module.__file__ = str(ROOT / "bin" / "queued_wait.py")
    exec(compile((ROOT / "bin" / "queued_wait.py").read_text(), module.__file__, "exec"), module.__dict__)
    return module


class QueuedWaitModuleTests:
    """The module on its own (#1801 findings): the second pass, the lock, the read bound, a missing id, the cap and the drop."""

    def qw_setup(self):
        self.dir = tempfile.mkdtemp(prefix="qw-", dir="/tmp/opencode" if os.path.isdir("/tmp/opencode") else None)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.q = fresh_module()

    def note(self, mid="m1", session="s1", delivered=1000, keys=("k1",)):
        if not hasattr(self, "q"):
            self.qw_setup()
        return self.q.note_delivery(self.dir, mid, "platform", session, list(keys), delivered, json.loads)

    def observe(self, now, inbox):
        if not hasattr(self, "q"):
            self.qw_setup()
        return self.q.observe(self.dir, now, json.loads, lambda sid: inbox)

    def test_an_empty_list_then_the_item_again_writes_no_consumed(self):
        self.note()
        self.assertEqual(self.observe(2000, []), [])
        self.assertEqual(self.observe(2100, [{"id": "m1"}]), [])
        self.assertEqual(self.observe(2200, []), [])   # the sighting was reset: this is a first one again
        self.assertEqual(self.observe(2300, [{"id": "m1"}]), [])

    def test_two_passes_with_it_gone_write_one_with_the_first_time(self):
        self.note()
        self.assertEqual(self.observe(2000, []), [])
        (event,) = self.observe(2100, [])
        self.assertEqual((event[0], event[2]["consumed"], event[2]["waited_seconds"]), ("message.consumed", 2000, 1000))
        self.assertEqual(self.observe(2200, []), [])

    def test_an_unreadable_inbox_concludes_nothing(self):
        self.note()
        self.assertEqual(self.observe(2000, None), [])
        self.assertEqual(self.observe(2100, None), [])
        self.assertEqual(json.loads(Path(self.dir, "queued-wait.json").read_text())["m1"].get("gone_since"), None)

    def test_two_threads_noting_fifty_each_end_with_all_notes(self):
        self.qw_setup()
        each = min(50, self.q.MAX_NOTES // 2)
        errors = []

        def run(tag):
            try:
                for i in range(each):
                    self.note("%s%d" % (tag, i), delivered=i)
            except Exception as err:
                errors.append(err)
        threads = [threading.Thread(target=run, args=(t,)) for t in "ab"]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(json.loads(Path(self.dir, "queued-wait.json").read_text())), 2 * each)

    def test_reads_per_pass_are_bounded_oldest_first(self):
        self.qw_setup()
        limit = self.q.MAX_READS_PER_PASS
        for i in range(limit + 5):
            self.note("m%d" % i, session="s%d" % i, delivered=1000 + i)
        seen = []
        self.q.observe(self.dir, 5000, json.loads, lambda sid: seen.append(sid) or [{"id": "m" + sid[1:]}])
        self.assertEqual(seen, ["s%d" % i for i in range(limit)])
        seen[:] = []
        self.q.observe(self.dir, 5100, json.loads, lambda sid: seen.append(sid) or [{"id": "m" + sid[1:]}])
        self.assertEqual(len(seen), limit)   # still the oldest first; every note is read at most limit per pass

    def test_a_missing_message_id_is_keyed_by_the_first_key_or_skipped(self):
        self.assertEqual(self.note(None, keys=("kx", "ky")), [])
        self.assertIn("kx", json.loads(Path(self.dir, "queued-wait.json").read_text()))
        (event,) = self.note(None, keys=())
        self.assertEqual(event[0], "queued_wait.skipped")

    def test_the_cap_evicts_the_oldest_with_an_event_the_next_pass_audits(self):
        self.qw_setup()
        cap = self.q.MAX_NOTES
        for i in range(cap):
            self.note("m%d" % i, delivered=1000 + i)
        (event,) = self.note("extra", delivered=9999)
        self.assertEqual((event[0], event[2]["message_id"]), ("queued_wait.evicted", "m0"))
        notes = json.loads(Path(self.dir, "queued-wait.json").read_text())
        self.assertEqual((len(notes), "m0" in notes, "extra" in notes), (cap, False, True))
        self.assertEqual(self.observe(10000, None), [])   # the caller audits the returned event: nothing is kept for a later pass
        self.assertFalse(Path(self.dir, "queued-wait-events.json").exists())

    def test_a_seven_day_drop_is_audited_once(self):
        self.note(delivered=1000)
        now = 1000 + self.q.DROP_AFTER + 1
        (event,) = self.observe(now, [{"id": "m1"}])
        self.assertEqual((event[0], event[2]["message_id"]), ("message.consumed.dropped", "m1"))
        self.assertEqual(self.observe(now + 1, [{"id": "m1"}]), [])

    def test_the_file_is_0600_after_a_rewrite_and_a_gone_session_concludes_nothing(self):
        self.note()
        self.note("m2", delivered=1001)
        self.observe(2000, None)
        self.assertEqual(Path(self.dir, "queued-wait.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in Path(self.dir).glob("*.tmp")], [])

    def test_a_fresh_load_reads_the_file_and_a_dict_mutant_fails(self):
        self.qw_setup()
        self.note()
        again = fresh_module()   # a fresh object: nothing in memory, so only the file can carry the note
        self.assertEqual(again.observe(self.dir, 2000, json.loads, lambda s: []), [])
        (event,) = again.observe(self.dir, 2100, json.loads, lambda s: [])
        self.assertEqual(event[2]["waited_seconds"], 1000)
        source = (ROOT / "bin" / "queued_wait.py").read_text()
        anchor = "        old = _read(state_dir, parse)\n        new = {k: dict(v) for k, v in old.items()}"
        self.assertEqual(source.count(anchor), 1)
        # the mutant keeps notes in a module-level dict (never reads the file): a fresh load has none, so the control's line is not written
        mutant = types.ModuleType("qw_mutant")
        exec(compile(source.replace(anchor, "        old = _MEM\n        new = {k: dict(v) for k, v in old.items()}").replace(
            "_counter = ", "_MEM = {}\n_counter = ", 1), "m", "exec"), mutant.__dict__)
        other = tempfile.mkdtemp(prefix="qw-", dir=self.dir)
        self.note("m9")
        self.assertEqual(mutant.observe(other, 2000, json.loads, lambda s: []), [])
        self.assertEqual(mutant.observe(other, 2100, json.loads, lambda s: []), [])   # never reads the file: no consumed line where the control wrote one


OTHER_NOW = EPOCH + 100000
OTHER_SHAPES = Path(__file__).with_name("fixtures").joinpath("queued_behind_turn_other_headers.json")


def other_shape_headers():
    """First header lines of other shapes (delayed, held, supersedes, a date), composed with in_turn off. The fixture was generated by running
    this function on `git archive origin/cw/header-date-2` (the BASE, before the change)."""
    out = []
    for age in (1900, 3600, 8000, 90000):   # delayed (held 31 min, 1 h is under 2 h so min, 2 h+, and another day)
        out.append(sb.compose("platform", [sb.Fact("send:c:1", "platform", "send", "late", OTHER_NOW - age, batch=False, sender="coder")],
                              delivered=OTHER_NOW)[0].splitlines()[0])
    for age, earlier in ((10, (OTHER_NOW - 500,)), (20, (OTHER_NOW - 500, OTHER_NOW - 700)), (2500, (OTHER_NOW - 3000,)), (30, (OTHER_NOW - 90000,))):
        fact = sb.Fact("send:c:2", "platform", "send", "newer", OTHER_NOW - age, batch=False, sender="coder")
        fact.supersedes = earlier
        out.append(sb.compose("platform", [fact], delivered=OTHER_NOW)[0].splitlines()[0])
    for held in (1900, 4000):   # a held header with newest-sent: two facts, the older one late
        facts = [sb.Fact("send:c:%d" % j, "platform", "send", "m%d" % j, OTHER_NOW - (held if j == 0 else 60 * j), batch=False, sender="coder")
                 for j in range(3)]
        out.append(sb.compose("platform", facts, delivered=OTHER_NOW)[0].splitlines()[0])
    return out


class TestQueuedBehindTurn(QueuedWaitModuleTests, Base):

    CONFIG = {"send_to_working": "queue"}   # a send to a working session is QUEUED (the delivery the case of 1 November 2026 shows)

    def world_of(self, config=None):
        world = World(self, agents=("cos",), config=config or self.CONFIG)
        sid = world.add_agent("platform", fleet={"role": "orchestrator", "reports_to": "cos"})
        return world, sid

    def work(self, world, sid):
        world.v2.start_turn(sid, "task", fleet=True, lasts=6 * 3600, steps=3)
        world.tick(5)

    def send(self, world, text="answer: use plan B"):
        return world.engine.send("cos", "platform", text)

    def intents(self, world):
        return [e for e in world.audit() if e["event"] == "v2.synthetic" and e["phase"] == "intent"]

    def idle_headers(self):
        out = []
        for i in range(len(RECORDED)):   # the fixture list decides how many
            world, _sid = self.world_of({})
            world.tick(i * IDLE_STEP + IDLE_OFFSET)
            self.assertTrue(world.engine.send("cos", "platform", "msg %d" % i)["sent"])
            (admitted,) = world.admissions("platform")
            out.append(admitted["text"].splitlines()[0])
        return out

    def consume(self, world, sid):
        world.v2.sessions[sid]["inbox"] = []   # the session took it (the fake stands for the session)

    def queued_working(self, config=None):
        world, sid = self.world_of(config)
        self.work(world, sid)
        self.assertTrue(self.send(world)["sent"])
        return world, sid

    def pending_out(self, world):
        with captured() as (out, err):
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
            code = sb.main(["pending", "platform"], runner=world.route, now=world.clock.now)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def consumed(self, world):
        return [e for e in world.audit() if e["event"] == "message.consumed"]

    # ---- S2 (REAL compose and deliver; the session is FAKE)
    def test_s2_a_message_admitted_in_a_turn_says_so_and_an_idle_one_prints_as_recorded(self):
        world, _sid = self.queued_working()
        (admitted,) = world.admissions("platform")
        header = admitted["text"].splitlines()[0]
        self.assertEqual(admitted["text"].count(WORDS), 1, IN_TURN_TEXT)
        self.assertIn("delivered %s, %s)" % (sb.clock_utc(world.clock.now()), WORDS), header, IN_TURN_TEXT)
        self.assertIsNotNone(RECORDED, "the recorded headers are missing")
        self.assertEqual(len(RECORDED), 20)   # the brief's count; idle_headers() follows the fixture's length
        headers = self.idle_headers()
        self.assertEqual([h for h, r in zip(headers, RECORDED) if h != r], [], "an idle header differs from its recorded bytes")
        self.assertEqual(sum(h.count(WORDS) for h in headers), 0)
        # headers of OTHER shapes (delayed, held, supersedes), recorded on the BASE (before the change), byte for byte
        recorded = json.loads(OTHER_SHAPES.read_text())
        self.assertEqual(len(recorded), 10)
        self.assertEqual(other_shape_headers(), recorded, "a header of another shape differs from its recorded bytes")
        for shape in ("delayed", "held", "supersedes"):
            self.assertTrue(any(shape in h for h in recorded), shape)

    # ---- S3 (REAL audit line)
    def test_s3_the_audit_line_says_whether_the_session_was_in_a_turn(self):
        busy, _ = self.queued_working()
        idle, _ = self.world_of({})
        self.assertTrue(self.send(idle)["sent"])
        flags = [e["detail"]["in_turn"] for e in self.intents(idle)]   # the worlds of one test share the audit file: it holds both lines, busy first
        self.assertEqual(flags, [True, False])   # one of each, the working session's first

    def lines(self, world, event):
        return [e for e in world.audit() if e["event"] == event]

    def test_a_busy_lock_is_audited_as_skipped_through_deliver(self):
        import fcntl
        self.q = fresh_module()
        world, sid = self.world_of()
        self.work(world, sid)
        fd = os.open(str(Path(world.engine.state_dir) / self.q.LOCK), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertTrue(self.send(world)["sent"])
        finally:
            os.close(fd)
        (line,) = self.lines(world, "queued_wait.skipped")
        self.assertEqual(line["detail"]["reason"], "lock busy")

    def test_an_eviction_at_the_cap_is_audited_as_evicted_through_deliver(self):
        self.q = fresh_module()
        world, sid = self.world_of()
        self.work(world, sid)
        notes = {"old%d" % i: {"name": "platform", "session": "x", "keys": [], "delivered": 1000 + i} for i in range(self.q.MAX_NOTES)}
        Path(world.engine.state_dir, self.q.FILE).write_text(json.dumps(notes))
        self.assertTrue(self.send(world)["sent"])
        (line,) = self.lines(world, "queued_wait.evicted")
        self.assertEqual(line["detail"]["message_id"], "old0")

    # ---- S4 (REAL note, pass, audit and pending)
    def test_s4_a_consumed_message_says_how_long_it_waited(self):
        world, sid = self.queued_working()
        world.tick(WAIT * 60)
        self.consume(world, sid)
        world.engine.run_pass()   # the first pass that sees it gone: not yet concluded
        self.assertEqual(self.consumed(world), [])
        world.tick(60)
        world.engine.run_pass()   # the second pass confirms; the time stays the first pass's
        (line,) = self.consumed(world)
        self.assertEqual(line["detail"]["waited_seconds"], WAIT * 60, WAITED_TEXT)
        world.engine.run_pass()
        self.assertEqual(len(self.consumed(world)), 1, "one line only")
        self.assertIn("waited %d min" % WAIT, self.pending_out(world), WAITED_TEXT)
        self.assertNotIn("not yet read", self.pending_out(world))

    # ---- S7
    def test_s7_an_unconsumed_message_says_not_yet_read_only_over_five_minutes(self):
        world, _sid = self.queued_working()
        limit = fresh_module().PENDING_WAIT_MINUTES
        self.assertGreater(WAIT, limit)
        under = limit - 2
        world.tick(under * 60)
        self.assertEqual(self.pending_out(world).count("waited"), 0)
        world.tick((WAIT - under) * 60)
        self.assertIn("waited %d min, not yet read" % WAIT, self.pending_out(world))
        self.assertEqual(self.consumed(world), [])

    # ---- S8
    def test_s8_the_wait_survives_a_restart(self):
        world, sid = self.queued_working()
        world.tick(WAIT * 60)
        world.engine = world.new_engine()   # a new engine on the same state dir
        self.consume(world, sid)
        world.engine.run_pass()
        world.tick(60)
        world.engine = world.new_engine()   # and again: the sighting is in the file
        world.engine.run_pass()
        (line,) = self.consumed(world)
        self.assertEqual(line["detail"]["waited_seconds"], WAIT * 60)
        world.engine = world.new_engine()
        world.engine.run_pass()
        self.assertEqual(len(self.consumed(world)), 1)
        path = world.state / "queued-wait.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    # ---- S5: mutants run in a copy of the tree, controls first
    def copy_tree(self):
        target = Path(tempfile.mkdtemp(prefix="qbt-", dir="/tmp/opencode" if os.path.isdir("/tmp/opencode") else None))
        self.addCleanup(shutil.rmtree, str(target), True)
        shutil.copytree(str(ROOT), str(target / "t"), ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return target / "t"

    def run_in(self, tree, name):
        env = dict(os.environ, QUEUED_BEHIND_TURN_INNER="1")
        done = subprocess.run([sys.executable, "-B", str(tree / "bin" / "test-switchboard"), "TestQueuedBehindTurn.%s" % name],
                              capture_output=True, text=True, env=env, cwd=str(tree), timeout=600)
        return done.returncode, done.stdout + done.stderr

    def mutate(self, tree, rel, old, new):
        path = tree / rel
        text = path.read_text()
        self.assertEqual(text.count(old), 1, "the mutant's anchor must occur once in %s" % rel)
        path.write_text(text.replace(old, new))

    @unittest.skipIf(os.environ.get("QUEUED_BEHIND_TURN_INNER"), "the inner run of a mutant copy")
    def test_s5_the_mutants_fail_with_the_anchored_texts_after_the_controls_pass(self):
        for name in ("test_s2_a_message_admitted_in_a_turn_says_so_and_an_idle_one_prints_as_recorded",
                     "test_s4_a_consumed_message_says_how_long_it_waited"):
            code, out = self.run_in(self.copy_tree(), name)
            self.assertEqual(code, 0, "the unmutated control must pass first: " + out[-600:])
        tree = self.copy_tree()
        self.mutate(tree, "bin/switchboard/decision.py", '", queued behind a turn" if in_turn else ""', '""')
        code, out = self.run_in(tree, "test_s2_a_message_admitted_in_a_turn_says_so_and_an_idle_one_prints_as_recorded")
        self.assertNotEqual(code, 0)
        self.assertIn(IN_TURN_TEXT, out)
        tree = self.copy_tree()
        self.mutate(tree, "bin/queued_wait.py", 'note["consumed"] = first', 'note["consumed"] = note["delivered"]')
        code, out = self.run_in(tree, "test_s4_a_consumed_message_says_how_long_it_waited")
        self.assertNotEqual(code, 0)
        self.assertIn(WAITED_TEXT, out)

    # ---- the light switch (#1801): the queued-wait pass reads no inbox when light is on
    LIGHT_TEXT = "the queued-wait pass makes no session read under the light switch"
    FORBIDDEN_READS = ("session.message.list", "session.inbox.list", "session.list")
    GUARD = "if self.state_dir is not None and not self.light:   # #1801: a queued message gone from its inbox"

    def test_the_owners_light_switch_means_the_queued_wait_pass_reads_no_inbox(self):
        world, sid = self.queued_working()   # a RECORDED queued wait (REAL deliver; the session is FAKE)
        self.assertTrue((world.state / "queued-wait.json").exists())
        world.engine.light = True
        self.consume(world, sid)
        world.tick(WAIT * 60)
        mark = len(world.v2.calls)
        world.engine.run_pass()
        world.tick(60)
        world.engine.run_pass()
        reads = [c[2] for c in world.v2.calls[mark:] if len(c) > 2 and c[1] == "api" and c[2] in self.FORBIDDEN_READS]
        self.assertEqual(reads, [], self.LIGHT_TEXT)
        self.assertEqual(self.consumed(world), [], self.LIGHT_TEXT + ": under light reads.inbox answers [] with no call, so an unguarded pass would wrongly conclude a message consumed")
        if os.environ.get("QUEUED_BEHIND_TURN_INNER"):
            return
        name = "test_the_owners_light_switch_means_the_queued_wait_pass_reads_no_inbox"
        code, out = self.run_in(self.copy_tree(), name)
        self.assertEqual(code, 0, "the unmutated control must pass first: " + out[-600:])   # REAL
        tree = self.copy_tree()
        self.mutate(tree, "bin/fleet-switchboard", self.GUARD, "if self.state_dir is not None:   # #1801: a queued message gone from its inbox")
        code, out = self.run_in(tree, name)   # FAKE: the mutant is a textual copy, run in a copy of the tree
        self.assertNotEqual(code, 0)
        self.assertIn(self.LIGHT_TEXT, out)


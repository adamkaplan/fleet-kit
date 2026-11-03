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
import types
import unittest
from pathlib import Path
from support.core import Base
from support.herdr_world import World
from support.loader import captured, sb
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE

WAIT = 58   # minutes a queued message waited; over PENDING_WAIT_MINUTES (5)
IN_TURN_TEXT = "a message admitted while the session is in a turn says it queued behind a turn"
WAITED_TEXT = "a consumed message says how long it waited from delivery to read"
WORDS = "queued behind a turn"
ROOT = Path(__file__).resolve().parents[2]

# The first header line of 20 messages admitted to an IDLE session, recorded by running idle_headers() on the head of #1842 (03eb3175) BEFORE
# the change of #1801.
RECORDED = json.loads(Path(__file__).with_name("fixtures").joinpath("queued_behind_turn_idle_headers.json").read_text()) \
    if Path(__file__).with_name("fixtures").joinpath("queued_behind_turn_idle_headers.json").exists() else None


class TestQueuedBehindTurn(Base):

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
        for i in range(20):
            world, _sid = self.world_of({})
            world.tick(i * 61 + 7)
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
        self.assertEqual(len(RECORDED), 20)
        headers = self.idle_headers()
        self.assertEqual([h for h, r in zip(headers, RECORDED) if h != r], [], "an idle header differs from its recorded bytes")
        self.assertEqual(sum(h.count(WORDS) for h in headers), 0)

    # ---- S3 (REAL audit line)
    def test_s3_the_audit_line_says_whether_the_session_was_in_a_turn(self):
        busy, _ = self.queued_working()
        idle, _ = self.world_of({})
        self.assertTrue(self.send(idle)["sent"])
        flags = [e["detail"]["in_turn"] for e in self.intents(idle)]   # the worlds of one test share the audit file: it holds both lines, busy first
        self.assertEqual((flags.count(True), flags.count(False)), (1, 1))

    # ---- S4 (REAL note, pass, audit and pending)
    def test_s4_a_consumed_message_says_how_long_it_waited(self):
        world, sid = self.queued_working()
        world.tick(WAIT * 60)
        self.consume(world, sid)
        world.engine.run_pass()
        (line,) = self.consumed(world)
        self.assertEqual(line["detail"]["waited_seconds"], WAIT * 60, WAITED_TEXT)
        world.engine.run_pass()
        self.assertEqual(len(self.consumed(world)), 1, "one line only")
        self.assertIn("waited %d min" % WAIT, self.pending_out(world), WAITED_TEXT)
        self.assertNotIn("not yet read", self.pending_out(world))

    # ---- S7
    def test_s7_an_unconsumed_message_says_not_yet_read_only_over_five_minutes(self):
        world, _sid = self.queued_working()
        world.tick(3 * 60)
        self.assertEqual(self.pending_out(world).count("waited"), 0)
        world.tick((WAIT - 3) * 60)
        self.assertIn("waited %d min, not yet read" % WAIT, self.pending_out(world))
        self.assertEqual(self.consumed(world), [])

    # ---- S8
    def test_s8_the_wait_survives_a_restart(self):
        world, sid = self.queued_working()
        world.tick(WAIT * 60)
        world.engine = world.new_engine()   # a new engine on the same state dir
        self.consume(world, sid)
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
        self.mutate(tree, "bin/queued_wait.py", 'note["consumed"] = now', 'note["consumed"] = note["delivered"]')
        code, out = self.run_in(tree, "test_s4_a_consumed_message_says_how_long_it_waited")
        self.assertNotEqual(code, 0)
        self.assertIn(WAITED_TEXT, out)

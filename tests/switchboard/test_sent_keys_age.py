"""#2094: a key delivered once to a session is never delivered to it again: the sent-keys trim keeps a key for SENT_MIN_AGE (S2 to S4, U1). World is the support World."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from support.core import Base
from support.herdr_world import World
from support.loader import sb
from support.paths import BIN, REPO

KEY = "triage:3f8b3cef22:20761"
ANCHOR = "a delivered key is never delivered again"


class TestSentLedgerBounds(Base):
    """The ledger itself: the age floor and the hard caps. Light mode never reads the transcript, so read-limit growth, resume and compaction do not apply."""

    def keys(self, ledger, sid):
        return ledger.load()[0].get(sid, {})

    def test_a_key_younger_than_the_floor_survives_a_count_over_400(self):
        ledger = sb.SentLedger(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, str(ledger.path.parent), True)
        for n in range(450):   # 450 keys within minutes: all younger than SENT_MIN_AGE of the newest
            ledger.confirm("ses_a", ["k%d" % n], 1000.0 + n)
        self.assertEqual(len(self.keys(ledger, "ses_a")), 450, "the floor keeps them past the count of %d" % sb.SENT_PER_SESSION)
        ledger.confirm("ses_a", ["late"], 1000.0 + 450 + sb.SENT_MIN_AGE)   # control: now the early ones are older than the floor
        got = self.keys(ledger, "ses_a")
        self.assertIn("late", got)
        self.assertEqual(len(got), sb.SENT_PER_SESSION, "control: past the floor the count trim applies")

    def test_the_hard_caps_bound_the_file_per_session_and_in_total_oldest_first(self):
        caps = {"SENT_PER_SESSION": 2, "SENT_HARD_PER_SESSION": 5, "SENT_HARD_TOTAL": 8}
        with mock.patch.dict(sb.SentLedger.update.__globals__, caps):   # the names update() reads, wherever the module is loaded from
            ledger = sb.SentLedger(Path(tempfile.mkdtemp()))
            self.addCleanup(shutil.rmtree, str(ledger.path.parent), True)
            for sid, base in (("ses_a", 0), ("ses_b", 10), ("ses_c", 20)):
                for n in range(1, 11):
                    ledger.confirm(sid, ["%s:%d" % (sid, n)], float(base + n))
            sessions = ledger.load()[0]
            self.assertTrue(all(len(mine) <= caps["SENT_HARD_PER_SESSION"] for mine in sessions.values()), "the per-session cap")
            self.assertEqual(sum(len(mine) for mine in sessions.values()), caps["SENT_HARD_TOTAL"], "the total cap")
            self.assertEqual(sorted(v for mine in sessions.values() for v in mine.values()), [18.0, 19.0, 20.0, 26.0, 27.0, 28.0, 29.0, 30.0], "oldest first")


class TestSentKeysAge(Base):
    """Light mode never reads the transcript (the ledger answers), so read-limit growth, resume and compaction do not apply to these tests.

    A fact delivered once, then 500 newer keys over 6 hours, then offered again: one delivery in all."""

    def world(self):
        return World(self, agents=("platform", "coder", "cos"), config={"v2_cache_seconds": 900, "v2_light": True})

    def deliveries(self, world, key):
        """The sends of the key, counted from the audit's intent lines (the fake's own admissions fold a repeat of the same message id)."""
        return sum(1 for e in world.audit() if e["event"] == "v2.synthetic" and e.get("phase") == "intent" and key in (e["detail"] or {}).get("keys", []))

    def deliver_then_crowd(self, world, restart):
        world.fact("platform", KEY, "Digest", created=world.clock.now(), kind="triage")
        world.tick(90)
        world.engine.run_pass()
        self.assertEqual(self.deliveries(world, KEY), 1, "control: delivered once")
        first = world.clock.now()
        ledger, sid = sb.SentLedger(world.state), world.sessions["platform"]
        for n in range(500):   # 500 newer keys over 6 hours
            ledger.confirm(sid, ["lab.fact:n%d" % n], first + 1 + n * 43.0)
        world.clock.t += 6 * 3600
        if restart:
            world.engine = world.new_engine()
        return first

    def offer_again(self, world, first, passes=4):
        for _ in range(passes):
            world.fact("platform", KEY, "Digest", created=first, kind="triage")
            world.tick(30)
            world.engine.run_pass()

    def s2(self, restart):
        world = self.world()
        first = self.deliver_then_crowd(world, restart)
        self.offer_again(world, first)
        self.assertEqual(self.deliveries(world, KEY), 1, ANCHOR)
        why = [e for e in world.audit() if e["event"] == "resend.skipped"]
        self.assertEqual(len(why), 1, "DW-2: ONE audit line says why the held fact was not sent again, written once")
        self.assertIn("already delivered to this session", why[0]["detail"]["why"])
        self.assertEqual(why[0]["detail"]["keys"], [KEY])

    def test_s2_a_delivered_key_is_never_delivered_again(self):
        self.s2(False)

    def test_s2_a_delivered_key_survives_a_daemon_restart(self):
        self.s2(True)

    def hold_lines(self, world):
        return [e for e in world.audit() if e["event"] == "hold"]

    def test_s3_an_unreadable_ledger_sends_0_and_writes_one_hold_line(self):
        world = self.world()
        (world.state / sb.SENT_KEYS_FILE).write_text("not a ledger")
        world.fact("platform", KEY, "Digest", kind="triage")
        for _ in range(3):
            world.tick(90)
            self.assertEqual(world.engine.run_pass()["sent"], [])
        self.assertEqual(self.deliveries(world, KEY), 0)
        self.assertEqual(len(self.hold_lines(world)), 1)

    def test_s3_an_early_ending_read_sends_0_and_writes_one_hold_line_and_a_readable_ledger_sends(self):
        world = World(self, agents=("platform", "coder", "cos"), config={"v2_cache_seconds": 900})   # light off: the transcript is read
        sid = world.sessions["platform"]
        for n in range(3):   # more newer synthetic messages than the pages read (2 x 1): the read ends before it reaches the fact
            world.v2.sessions[sid]["messages"].append({"id": "msg_x%d" % n, "type": "synthetic", "time": {"created": int((world.clock.now() + n) * 1000)}, "text": "x"})
        world.fact("platform", KEY, "Digest", created=world.clock.now() - 5000, kind="triage")
        with mock.patch.object(sb, "SYNTHETIC_PAGE", 1), mock.patch.object(sb, "MAX_READ_PAGES", 2):
            for _ in range(3):
                world.tick(90)
                self.assertEqual(world.engine.run_pass()["sent"], [])
        self.assertEqual(self.deliveries(world, KEY), 0)
        self.assertEqual(len(self.hold_lines(world)), 1)
        holds = len(self.hold_lines(world))   # the control shares this test's state directory and audit: it adds no hold line
        control = self.world()   # control: a readable ledger sends
        control.fact("platform", KEY, "Digest", kind="triage")
        control.tick(90)
        control.engine.run_pass()
        self.assertEqual(self.deliveries(control, KEY), 1)
        self.assertEqual(len(self.hold_lines(control)), holds)

    def test_s3_a_ledger_that_cannot_be_read_holds_the_fact_and_sends_nothing(self):
        world = self.world()
        (world.state / sb.SENT_KEYS_FILE).write_text("not a ledger")
        world.fact("platform", KEY, "Digest", kind="triage")
        world.tick(90)
        report = world.engine.run_pass()
        self.assertEqual(report["sent"], [])
        self.assertEqual(self.deliveries(world, KEY), 0)
        world.tick(30)
        self.assertEqual(world.engine.run_pass()["sent"], [])
        self.assertEqual(self.deliveries(world, KEY), 0)

    def test_u1_a_new_key_is_delivered_once(self):
        world = self.world()
        first = self.deliver_then_crowd(world, False)
        world.fact("platform", "lab.fact:brand-new", "New", created=world.clock.now())
        for _ in range(3):
            world.tick(30)
            world.engine.run_pass()
        self.assertEqual(self.deliveries(world, "lab.fact:brand-new"), 1)

    def test_s4_the_count_only_trim_restored_in_a_copy_is_caught(self):
        source = (BIN / "switchboard" / "engine.py").read_text()
        needle = "young[sid] = {k: v for k, v in mine.items() if v > newest - SENT_MIN_AGE}"
        self.assertEqual(source.count(needle), 1, "the mutant's anchor")
        self.assertEqual(self.run_copy(source), 0, "control: the unmutated copy passes")
        self.assertNotEqual(self.run_copy(source.replace(needle, "young[sid] = {}")), 0, "the mutant must fail")
        out = self.run_copy(source.replace(needle, "young[sid] = {}"), text=True)
        self.assertIn(ANCHOR, out)

    def run_copy(self, engine_text, text=False):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(str(REPO), str(copy), ignore=shutil.ignore_patterns(".git", "__pycache__"))
            (copy / "bin" / "switchboard" / "engine.py").write_text(engine_text)
            done = subprocess.run([sys.executable, "-m", "unittest", "test_sent_keys_age.TestSentKeysAge.test_s2_a_delivered_key_survives_a_daemon_restart"],
                                  cwd=str(copy / "tests" / "switchboard"), capture_output=True, text=True,
                                  env=dict(os.environ, PYTHONPATH=str(copy / "tests" / "switchboard")))
            return done.stdout + done.stderr if text else done.returncode


if __name__ == "__main__":
    unittest.main()

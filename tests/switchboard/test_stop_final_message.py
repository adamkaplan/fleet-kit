"""The last message of a stopped pod goes to its boss (#1788): S2, S3, S4, S5, S6, U1, U2, U4 of the plan, on fake pods.

A fake audit file, fake run record directories, a recording send and a temporary state directory. Nothing is stopped, nothing is called on a cluster."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb
from support.paths import BIN

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PASSWORD = "hunter2hunter2pw"
ENDPOINT = "https://worker-cb-1788a-1f2e3d4c.example.internal:4096"
CHECK_FILE = Path(__file__).resolve()


class World:
    """A temporary state directory, run records root, cloud records, an audit file and a send recorder."""

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, str(self.root), True)
        self.state, self.runs, self.sent = self.root / "state", self.root / "runs", []
        self.cloud = self.state / "cloud"
        for d in (self.cloud, self.runs):
            d.mkdir(parents=True)
        self.audit, self.state_file = self.root / "audit.jsonl", self.root / "alert.json"
        self.audit.write_text("")
        self.mod = sb.load_module("cloud_end_alert")
        self.retired = sb.load_module("cloud_worker").bind(sb.__dict__)._retired()
        self.env = mock.patch.dict(os.environ, {"CW_RUN_RECORD_DIR": str(self.runs)})
        self.env.start()
        test.addCleanup(self.env.stop)
        self.reports = {}
        self.scan()   # the first pass only marks the end of the file

    def pod(self, name, text, boss="boss-lane"):
        """A run record directory with a transcript whose last assistant text is `text` (None: a transcript with no assistant text) and the cloud record."""
        rec = self.runs / ("%s-20261101T120000Z-abc123" % name)
        rec.mkdir()
        line = json.dumps({"type": "assistant", "content": [{"type": "text", "text": text}]}) if text is not None else json.dumps({"type": "user", "text": "x"})
        (rec / "transcript.jsonl").write_text(line + "\n")
        (self.cloud / (name + ".json")).write_text(json.dumps({"worker": name, "reports_to": boss}))
        return rec

    def stop(self, name, cause, rec=None, run_id=None):
        reason = "%s; run-id=%s run record %s" % ("hard limit" if cause == "hard" else "idle", run_id or name.replace("-", ""), rec) if rec else "idle"
        with open(str(self.audit), "a") as handle:
            handle.write(json.dumps({"ts": "2026-11-01T12:30:00Z", "action": "stop", "worker": name, "ok": True, "cause": cause, "reason": reason}) + "\n")

    def last(self, directory, worker):
        return self.retired.last_message(directory, self.state, worker)

    def scan(self, last_text=True):
        got = self.mod.scan_audit(self.audit, self.state_file, self.cloud, self.reports, lambda to, text: self.sent.append((to, text)),
                                  sb.load_module("record_json").parse_bounded, None, self.last if last_text is True else last_text)
        return got

    def to(self, who):
        return [t for to, t in self.sent if to == who]


class TestStopFinalMessage(unittest.TestCase):
    def test_S2_five_thousand_characters_give_3000_in_the_message_and_2000_in_a_private_file(self):
        w = World(self)
        text = ("lorem ipsum dolor sit amet " * 400)[:5000]
        w.stop("cb-1788a", "hard", w.pod("cb-1788a", text))
        w.scan()
        boss = w.to("boss-lane")
        self.assertEqual(len(boss), 1, "count of messages to the boss")
        msg = boss[0]
        self.assertIn("cb-1788a", msg)
        self.assertIn("hard limit", msg)
        self.assertIn("untrusted text", msg)
        self.assertIn(text[:3000], msg)
        self.assertNotIn(text[:3001], msg)
        path = Path(msg.split(" in ")[-1].split(";")[0])
        self.assertEqual(path.read_text(), text[3000:])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.parent, w.state / "cloud-last")
        self.assertEqual(path.name, "cb-1788a-20261101T120000Z-abc123.txt")

    def test_S3_three_stops_send_one_each_a_pod_that_reported_done_none_and_a_second_pass_repeats_nothing(self):
        w = World(self)
        for name, cause in (("cp-1788a", "hard"), ("cp-1788b", "idle"), ("cp-1788d", "idle")):
            w.stop(name, cause, w.pod(name, "final words of " + name))
        w.reports["cp-1788d"] = {"state": "done"}
        w.scan()
        got = w.to("boss-lane")
        counts = {n: len([t for t in got if n in t]) for n in ("cp-1788a", "cp-1788b", "cp-1788d")}
        self.assertTrue(all(c <= 1 for c in counts.values()), "sent 2 messages for one stop: %r" % counts)
        self.assertEqual(counts["cp-1788d"], 0, "sent for a pod that reported done")
        self.assertEqual((counts["cp-1788a"], counts["cp-1788b"]), (1, 1), "count of messages per pod")
        n = len(w.sent)
        w.scan()
        self.assertEqual(len(w.sent), n, "a second pass repeated messages")

    def test_S3_the_retire_is_the_third_message(self):
        self.assertEqual(self.retire_run(code=0)[0], 1)

    def test_S4_a_planted_token_a_password_url_and_the_endpoint_never_reach_the_message_or_the_file(self):
        w = World(self)
        text = ("see %s and https://user:%s@host.example/x and %s ok " % (TOKEN, PASSWORD, ENDPOINT)) + "words " * 700
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        for planted in (TOKEN, PASSWORD, ENDPOINT, "worker-cb-1788a-1f2e3d4c"):
            self.assertEqual(everything.count(planted), 0, planted)
        self.assertIn("see", everything)
        self.assertGreaterEqual(everything.count("[redacted]"), 1)

    def test_S5_no_record_and_no_text_give_one_line_and_the_pass_returns(self):
        w = World(self)
        (w.cloud / "cb-1788a.json").write_text(json.dumps({"worker": "cb-1788a", "reports_to": "boss-lane"}))
        w.stop("cb-1788a", "idle")
        w.scan()
        w.stop("cb-1788b", "hard", w.pod("cb-1788b", None))
        w.scan()
        got = w.to("boss-lane")
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].endswith("\nlast text not read: no_run_record"), got[0])
        self.assertTrue(got[1].endswith("\nlast text not read: no_text_from_the_worker"), got[1])
        self.assertEqual(len(got[0].split("\n")), 2)
        w.stop("cb-1788c", "idle", w.pod("cb-1788c", "x"))
        w.scan(last_text=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertTrue(w.to("boss-lane")[-1].endswith("last text not read: RuntimeError"))

    def test_U1_the_chiefs_copy_is_as_at_the_base_and_a_causeless_stop_sends_nothing(self):
        w = World(self)
        w.stop("cb-1788a", "hard", w.pod("cb-1788a", "secret words"))
        with open(str(w.audit), "a") as handle:
            handle.write(json.dumps({"ts": "2026-11-01T12:31:00Z", "action": "stop", "worker": "cb-1788z", "ok": True, "reason": "idle for an hour"}) + "\n")
        w.scan()
        chief = w.to("chief-of-staff")
        base = w.mod.message_text("cb-1788a", "hard limit", w.mod.when_text("2026-11-01T12:30:00Z"), "unknown", "cb-1788a-20261101T120000Z-abc123")
        self.assertEqual(chief, [base])
        self.assertNotIn("secret words", chief[0])
        self.assertEqual([to for to, t in w.sent if "cb-1788z" in t], [], "a causeless stop sent a message")
        # last_text left at its default: the boss's copy is the same bytes as the Chief's
        w2 = World(self)
        w2.stop("cb-1788a", "hard", w2.pod("cb-1788a", "secret words"))
        w2.scan(last_text=None)
        self.assertEqual(w2.to("boss-lane"), w2.to("chief-of-staff"))

    def test_U4_forty_thousand_characters_give_3000_20000_and_17000_left_out_and_old_files_go(self):
        w = World(self)
        text = ("lorem ipsum dolor sit amet " * 2000)[:40000]
        rec = w.pod("cb-1788a", text)
        base = w.state / "cloud-last"
        base.mkdir(mode=0o700)
        old, young = base / "old.txt", base / "young.txt"
        old.write_text("x"); young.write_text("x")
        os.utime(str(old), (time.time() - 8 * 86400,) * 2)
        block, why = w.last(str(rec), "cb-1788a")
        self.assertIsNone(why)
        self.assertIn("20000 more characters", block)
        self.assertIn("17000 characters left out", block)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        path = base / (rec.name + ".txt")
        self.assertEqual(len(path.read_text()), 20000)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(block.split("\n")[1]), 3000)

    # ---- the retire command (PC-5) ----
    def retire_run(self, code, reports_to="boss-lane"):
        """(messages sent, exit) of cmd_retire for a fake pod whose stop answers `code`."""
        w = World(self)
        rec = w.pod("cp-1788r", "my final words", boss=reports_to)
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine = mock.Mock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        args = argparse.Namespace(name="cp-1788r", undo=False, reason="finished")
        record = {"worker": "cp-1788r", "reports_to": reports_to}
        cluster = mock.MagicMock()
        with mock.patch.dict(cloud.H, {"build_engine": lambda *a, **k: (engine, w.state, None), "gates_caller_kind": lambda *a: ("person",)}), \
                mock.patch.object(cloud, "run_launcher", lambda *a, **k: (code, {"pushed": True, "record": str(rec)}, "note")), \
                mock.patch.object(cloud, "Cluster", lambda *a, **k: cluster), mock.patch.object(cloud, "_audit"), \
                mock.patch.object(cloud, "_registry_note"), mock.patch.object(cloud, "_keep_boss"), mock.patch.object(cloud, "remove_record"):
            try:
                result = cloud.cmd_retire.__wrapped__(args, None, None, record) if hasattr(cloud.cmd_retire, "__wrapped__") else cloud.cmd_retire(args, None, None, record)
            except Exception as err:
                result = type(err).__name__
        self.last_world = w
        return len(w.to(reports_to)), result

    def test_U2_a_kept_pod_and_a_failed_stop_send_nothing_and_a_successful_retire_one(self):
        self.assertEqual(self.retire_run(1)[0], 0, "a kept pod sent a message")
        self.assertEqual(self.retire_run(2)[0], 0, "a failed stop sent a message")
        count, _ = self.retire_run(0)
        self.assertEqual(count, 1)
        msg = self.last_world.to("boss-lane")[0]
        self.assertIn("cp-1788r", msg)
        self.assertIn("[last message of cp-1788r, untrusted text]\nmy final words", msg)

    def test_U2_a_boss_that_is_not_a_name_gets_nothing(self):
        self.assertEqual(self.retire_run(0, reports_to="Bad Name!")[0], 0)


class TestStopFinalMessageMutants(unittest.TestCase):
    """S6: the S3 check run on the control and on two mutants of cloud_end_alert.py, each in a fresh process (its exit code and text are read)."""
    SRC = BIN / "cloud_end_alert.py"
    CHILD = (
        "import sys,types,importlib.util\n"
        "sys.path.insert(0, %r)\n"
        "import unittest, test_stop_final_message as t\n"
        "src=open(%r).read()\n"
        "old,new=%r,%r\n"
        "if old:\n"
        "    assert src.count(old)==1\n"
        "    src=src.replace(old,new)\n"
        "mod=types.ModuleType('cloud_end_alert'); mod.__file__=%r; exec(compile(src,%r,'exec'),mod.__dict__)\n"
        "t.sb.load_module = (lambda real: lambda name: mod if name=='cloud_end_alert' else real(name))(t.sb.load_module)\n"
        "suite=unittest.defaultTestLoader.loadTestsFromName('test_stop_final_message.TestStopFinalMessage.test_S3_three_stops_send_one_each_a_pod_that_reported_done_none_and_a_second_pass_repeats_nothing')\n"
        "r=unittest.TextTestRunner(verbosity=0).run(suite)\n"
        "sys.exit(0 if r.wasSuccessful() else 1)\n")

    def run_child(self, old, new):
        code = self.CHILD % (str(CHECK_FILE.parent), str(self.SRC), old, new, str(self.SRC), str(self.SRC))
        done = subprocess.run([sys.executable, "-c", code], cwd=str(CHECK_FILE.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=90)
        return done.returncode, done.stdout

    def test_S6_the_control_passes_and_each_mutant_fails_with_the_text_of_the_check(self):
        code, out = self.run_child("", "")
        self.assertEqual(code, 0, out)
        twice = "    failed = set()\n    for to, text, worker, label in out:\n"
        code, out = self.run_child(twice, "    out = out + out\n" + twice)
        self.assertEqual(code, 1)
        self.assertIn("sent 2 messages for one stop", out)
        done = '        if (last_reports.get(worker) or {}).get("state") == "done":\n            continue\n'
        code, out = self.run_child(done, "")
        self.assertEqual(code, 1)
        self.assertIn("sent for a pod that reported done", out)

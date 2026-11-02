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


class StopWorld:
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
        w = StopWorld(self)
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
        w = StopWorld(self)
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
        w = StopWorld(self)
        text = ("see %s and https://user:%s@host.example/x and %s ok " % (TOKEN, PASSWORD, ENDPOINT)) + "words " * 700
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        for planted in (TOKEN, PASSWORD, ENDPOINT, "worker-cb-1788a-1f2e3d4c"):
            self.assertEqual(everything.count(planted), 0, planted)
        self.assertIn("see", everything)
        self.assertGreaterEqual(everything.count("[redacted]"), 1)

    def test_S5_no_record_and_no_text_give_one_line_and_the_pass_returns(self):
        w = StopWorld(self)
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
        w = StopWorld(self)
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
        w2 = StopWorld(self)
        w2.stop("cb-1788a", "hard", w2.pod("cb-1788a", "secret words"))
        w2.scan(last_text=None)
        self.assertEqual(w2.to("boss-lane"), w2.to("chief-of-staff"))

    def test_U4_forty_thousand_characters_give_3000_20000_and_17000_left_out_and_old_files_go(self):
        w = StopWorld(self)
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
        w = StopWorld(self)
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

    def test_U10_the_boss_name_is_the_agent_name_pattern(self):
        for bad in ("1boss", "a.b", "a" * 33):
            self.assertEqual(self.retire_run(0, reports_to=bad)[0], 0, "a boss named %r got a message" % bad)
        self.assertEqual(self.retire_run(0, reports_to="chief-of-staff")[0], 1)


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


OLDER_TEXT = "words of the OLDER run of this name"
NEWER_TEXT = "words of the NEWER run of this name"


def make_record(parent, name, stamp, text):
    rec = Path(parent) / ("%s-%s-abc123" % (name, stamp))
    rec.mkdir(parents=True)
    (rec / "transcript.jsonl").write_text(json.dumps({"type": "assistant", "content": [{"type": "text", "text": text}]}) + "\n")
    return rec


class TestStopFinalMessageTwoRuns(unittest.TestCase):
    """S9, U7 (PC-7): the text read is the one of THIS stop's record, never the newest record of the name."""

    def two_runs(self, newer_has_record):
        w = StopWorld(self)
        older = make_record(w.runs, "cb-1788a", "20261101T100000Z", OLDER_TEXT)
        (w.cloud / "cb-1788a.json").write_text(json.dumps({"worker": "cb-1788a", "reports_to": "boss-lane"}))
        newer = make_record(w.runs, "cb-1788a", "20261101T120000Z", NEWER_TEXT) if newer_has_record else None
        return w, older, newer

    def retire_path(self, w, record):
        """The boss's messages of cmd_retire whose stop answer names `record` (None: no field)."""
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine = mock.Mock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        body = {"pushed": True}
        if record is not None:
            body["record"] = str(record)
        args = argparse.Namespace(name="cb-1788a", undo=False, reason="finished")
        with mock.patch.dict(cloud.H, {"build_engine": lambda *a, **k: (engine, w.state, None), "gates_caller_kind": lambda *a: ("person",)}), \
                mock.patch.object(cloud, "run_launcher", lambda *a, **k: (0, body, "note")), \
                mock.patch.object(cloud, "Cluster", lambda *a, **k: mock.MagicMock()), mock.patch.object(cloud, "_audit"), \
                mock.patch.object(cloud, "_registry_note"), mock.patch.object(cloud, "_keep_boss"), mock.patch.object(cloud, "remove_record"):
            cloud.cmd_retire(args, None, None, {"worker": "cb-1788a", "reports_to": "boss-lane"})
        return w.to("boss-lane")

    def test_S9_control_the_newer_run_with_its_own_record_sends_its_own_text(self):
        w, older, newer = self.two_runs(True)
        got = self.retire_path(w, newer)
        self.assertEqual((len(got), got[0].count(NEWER_TEXT), got[0].count(OLDER_TEXT)), (1, 1, 0), got)
        w2, older, newer = self.two_runs(True)
        w2.stop("cb-1788a", "idle", newer)
        w2.scan()
        got = w2.to("boss-lane")
        self.assertEqual((len(got), got[0].count(NEWER_TEXT), got[0].count(OLDER_TEXT)), (1, 1, 0), got)

    def test_S9_the_newer_run_without_a_record_never_sends_the_older_runs_text(self):
        w, older, _ = self.two_runs(False)
        got = self.retire_path(w, None)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count(OLDER_TEXT), 0, "the older run's text was sent as the last message")
        self.assertTrue(got[0].endswith("\nlast text not read: no_run_record"), got[0])
        w2, older, _ = self.two_runs(False)
        w2.stop("cb-1788a", "idle")
        w2.scan()
        got = w2.to("boss-lane")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count(OLDER_TEXT), 0, "the older run's text was sent as the last message")
        self.assertIn("last text not read: no_run_record", got[0])

    def test_U7_a_directory_outside_the_root_or_of_another_pod_is_never_read(self):
        w = StopWorld(self)
        outside = make_record(w.root / "elsewhere", "cb-1788a", "20261101T120000Z", "PLANTED outside")
        other = make_record(w.runs, "cb-1788b", "20261101T120000Z", "PLANTED other pod")
        (w.cloud / "cb-1788a.json").write_text(json.dumps({"worker": "cb-1788a", "reports_to": "boss-lane"}))
        (w.cloud / "cb-1788c.json").write_text(json.dumps({"worker": "cb-1788c", "reports_to": "boss-lane"}))
        w.stop("cb-1788a", "idle", outside)
        w.stop("cb-1788c", "idle", other)
        w.scan()
        got = w.to("boss-lane")
        self.assertEqual(len([t for t in got if "last text not read: no_run_record" in t]), 2, got)
        self.assertEqual(len([t for t in got if "PLANTED" in t]), 0, got)
        for rec in (outside, other):
            self.assertEqual(w.last(str(rec), "cb-1788a"), (None, "no run record"))
        before = len(w.to("boss-lane"))
        self.retire_path(w, outside)
        self.retire_path(w, other)
        later = w.to("boss-lane")[before:]
        self.assertEqual(len([t for t in later if "last text not read: no_run_record" in t]), 2, later)
        self.assertEqual(len([t for to, t in w.sent if "PLANTED" in t]), 0)


    def test_U9_a_link_in_the_run_root_to_a_directory_outside_is_never_read(self):
        w = StopWorld(self)
        (w.cloud / "cb-1788a.json").write_text(json.dumps({"worker": "cb-1788a", "reports_to": "boss-lane"}))
        outside = make_record(w.root / "elsewhere", "cb-1788a", "20261101T120000Z", "PLANTED through a link")
        link = w.runs / "cb-1788a-20261101T120000Z-abc123"
        os.symlink(str(outside), str(link))
        got = self.retire_path(w, link)
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].endswith("\nlast text not read: no_run_record"), got[0])
        self.assertEqual(sum(t.count("PLANTED through a link") for t in got), 0, got)
        os.unlink(str(link))
        real = make_record(w.runs, "cb-1788a", "20261101T120000Z", "its own text")
        self.assertEqual(real, link)
        got = self.retire_path(w, real)
        self.assertEqual((len(got), got[-1].count("its own text")), (2, 1), got)

class TestStopFinalMessageTwoRunsMutant(unittest.TestCase):
    """S10: the S9 check on the control and on a mutant that reads the newest record of the name, each in a fresh process."""
    CHILD = (
        "import sys,unittest\n"
        "sys.path.insert(0, %r)\n"
        "import test_stop_final_message as t\n"
        "if %r:\n"
        "    r = t.sb.load_module('cloud_worker').bind(t.sb.__dict__)._retired()\n"
        "    real = r.last_message\n"
        "    r.last_message = lambda d, s, p, clock=None: real((r.retired_run(p) or ['', ''])[0], s, p, clock)\n"
        "suite = unittest.defaultTestLoader.loadTestsFromName('test_stop_final_message.TestStopFinalMessageTwoRuns.test_S9_the_newer_run_without_a_record_never_sends_the_older_runs_text')\n"
        "res = unittest.TextTestRunner(verbosity=0).run(suite)\n"
        "sys.exit(0 if res.wasSuccessful() else 1)\n")

    def run_child(self, mutant):
        done = subprocess.run([sys.executable, "-c", self.CHILD % (str(CHECK_FILE.parent), mutant)], cwd=str(CHECK_FILE.parent),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=90)
        return done.returncode, done.stdout

    def test_S10_the_control_passes_and_the_mutant_fails_with_the_text_of_the_check(self):
        code, out = self.run_child(False)
        self.assertEqual(code, 0, out)
        code, out = self.run_child(True)
        self.assertEqual(code, 1, out)
        self.assertIn("the older run's text was sent as the last message", out)


class TestStopFinalMessageHardening(unittest.TestCase):
    """PC-9 (U11..U14): the Lows of the second strict security pair, on fake pods."""

    def test_U11_an_upper_case_endpoint_is_in_neither_the_message_nor_the_file(self):
        w = StopWorld(self)
        big = "HTTPS://Worker-X-ab12.Example.Internal:4096"
        text = "see %s early " % big + "words " * 700 + " late %s end" % big
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        self.assertEqual(everything.count(big), 0, "an upper-case endpoint reached a lane")
        self.assertEqual(everything.lower().count("worker-x-ab12"), 0, "an upper-case endpoint reached a lane")
        self.assertIn("early", everything)

    def test_U12_a_wide_cloud_last_is_tightened_and_a_link_or_a_foreign_one_is_refused(self):
        w = StopWorld(self)
        base = w.state / "cloud-last"
        base.mkdir()
        os.chmod(str(base), 0o777)
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", "lorem " * 1000))
        w.scan()
        mode = stat.S_IMODE(base.stat().st_mode)
        self.assertEqual(mode & ~0o700, 0, "a cloud-last directory stayed wider than 0700")
        self.assertEqual(mode, 0o700)
        self.assertEqual(len(list(base.iterdir())), 1)
        self.assertNotIn("last text not read", w.to("boss-lane")[0])
        unsafe = "\nlast text not read: cloud_last_unsafe"
        w2 = StopWorld(self)
        elsewhere = w2.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(str(elsewhere), str(w2.state / "cloud-last"))
        w2.stop("cb-1788a", "idle", w2.pod("cb-1788a", "lorem " * 1000))
        w2.scan()
        self.assertTrue(w2.to("boss-lane")[0].endswith(unsafe), w2.to("boss-lane"))
        self.assertEqual(list(elsewhere.iterdir()), [])
        w3 = StopWorld(self)
        w3.stop("cb-1788a", "idle", w3.pod("cb-1788a", "lorem " * 1000))
        with mock.patch.object(os, "getuid", lambda: os.geteuid() + 1):
            w3.scan()
        self.assertTrue(w3.to("boss-lane")[0].endswith(unsafe), w3.to("boss-lane"))
        self.assertEqual(list((w3.state / "cloud-last").iterdir()), [])

    def test_U13_a_token_in_the_retire_reason_is_not_in_the_message(self):
        w = StopWorld(self)
        rec = w.pod("cp-1788r", "my final words")
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine = mock.Mock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        args = argparse.Namespace(name="cp-1788r", undo=False, reason="leaked %s done" % TOKEN)
        with mock.patch.dict(cloud.H, {"build_engine": lambda *a, **k: (engine, w.state, None), "gates_caller_kind": lambda *a: ("person",)}), \
                mock.patch.object(cloud, "run_launcher", lambda *a, **k: (0, {"pushed": True, "record": str(rec)}, "note")), \
                mock.patch.object(cloud, "Cluster", lambda *a, **k: mock.MagicMock()), mock.patch.object(cloud, "_audit"), \
                mock.patch.object(cloud, "_registry_note"), mock.patch.object(cloud, "_keep_boss"), mock.patch.object(cloud, "remove_record"):
            cloud.cmd_retire(args, None, None, {"worker": "cp-1788r", "reports_to": "boss-lane"})
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count(TOKEN), 0, "a token in the retire reason reached a lane")
        self.assertIn("leaked", got[0])

    def test_U14_a_directory_swapped_for_a_link_after_the_check_is_not_followed(self):
        w = StopWorld(self)
        rec = w.pod("cb-1788a", "its own text")
        target = make_record(w.root / "elsewhere", "cb-1788a", "20261101T120000Z", "PLANTED by the swap")
        w.stop("cb-1788a", "idle", rec)
        real, fired = os.path.realpath, []

        def swap(path, *a, **k):
            out = real(path, *a, **k)
            if os.path.basename(str(path)) == rec.name and not fired:   # the check has just resolved the directory: swap it now
                fired.append(1)
                shutil.rmtree(str(rec))
                os.symlink(str(target), str(rec))
            return out
        with mock.patch.object(os.path, "realpath", swap):
            w.scan()
        self.assertEqual(len(fired), 1, "the swap hook did not run")
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0].count("PLANTED by the swap"), 0, "a directory swapped after the check was followed")
        self.assertTrue(got[0].endswith("\nlast text not read: no_run_record"), got[0])


class TestStopFinalMessageHardeningMutant(unittest.TestCase):
    """U11..U14: each check on the control and on a mutant of a SCRATCH COPY of the source (outside the repository), each in a fresh process."""
    CHILD = (
        "import sys,os,tempfile,importlib.util,unittest\n"
        "sys.path.insert(0, %r)\n"
        "import test_stop_final_message as t\n"
        "name,real,old,new,test=%r,%r,%r,%r,%r\n"
        "if old:\n"
        "    src=open(real).read()\n"
        "    assert src.count(old)==1, 'anchor not found once'\n"
        "    scratch=tempfile.mkdtemp()\n"
        "    import glob,shutil\n"
        "    [shutil.copy(f,scratch) for f in glob.glob(os.path.join(os.path.dirname(real),'*.py'))]\n"
        "    path=os.path.join(scratch, name+'.py')\n"
        "    open(path,'w').write(src.replace(old,new))\n"
        "    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod)\n"
        "    t.sb.load_module = (lambda real_load: lambda n: mod if n==name else real_load(n))(t.sb.load_module)\n"
        "suite=unittest.defaultTestLoader.loadTestsFromName('test_stop_final_message.TestStopFinalMessageHardening.'+test)\n"
        "r=unittest.TextTestRunner(verbosity=0).run(suite)\n"
        "sys.exit(0 if r.wasSuccessful() else 1)\n")
    CASES = (
        ("cloud_retired", "test_U11_an_upper_case_endpoint_is_in_neither_the_message_nor_the_file", '(:[0-9]+)?", re.I)', '(:[0-9]+)?")',
         "an upper-case endpoint reached a lane"),
        ("cloud_retired", "test_U12_a_wide_cloud_last_is_tightened_and_a_link_or_a_foreign_one_is_refused", "os.fchmod(dfd, 0o700)", "pass",
         "a cloud-last directory stayed wider than 0700"),
        ("cloud_worker", "test_U13_a_token_in_the_retire_reason_is_not_in_the_message", "_scrub_lib()(_one(reason, 120), (), 200)", "_one(reason, 120)",
         "a token in the retire reason reached a lane"),
        ("cloud_retired", "test_U14_a_directory_swapped_for_a_link_after_the_check_is_not_followed", "_last_worker_text_at(directory, (before.st_dev, before.st_ino))",
         "_last_worker_text(directory)", "a directory swapped after the check was followed"),
    )

    def run_child(self, name, test, old, new):
        code = self.CHILD % (str(CHECK_FILE.parent), name, str(BIN / (name + ".py")), old, new, test)
        done = subprocess.run([sys.executable, "-c", code], cwd=str(CHECK_FILE.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=120)
        return done.returncode, done.stdout

    def test_U11_to_U14_the_control_passes_and_each_mutant_fails_with_the_text_of_the_check(self):
        for name, test, old, new, text in self.CASES:
            code, out = self.run_child(name, test, "", "")
            self.assertEqual(code, 0, "control of %s: %s" % (test, out))
            code, out = self.run_child(name, test, old, new)
            self.assertEqual(code, 1, "mutant of %s: %s" % (test, out))
            self.assertIn(text, out, test)


class TestStopFinalMessageEdges(unittest.TestCase):
    """PC-10 (U15..U17): the Medium and the Lows of the third strict security pair, on fake pods."""

    def test_U15_a_rebuilt_endpoint_is_in_neither_the_message_nor_the_file(self):
        w = StopWorld(self)
        text = "see https://" + ENDPOINT + "worker-x-ab12.example.internal:4096 early " + "words " * 700 + " late"
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        self.assertEqual(everything.count("worker-x-ab12.example.internal"), 0, "a rebuilt endpoint reached a lane")
        self.assertIn("early", everything)

    def test_U15_a_text_that_still_changes_after_20_passes_is_refused_as_endpoint_loop(self):
        w = StopWorld(self)
        deep = ENDPOINT
        for _ in range(25):
            deep = "ht" + deep + "tps://worker-q.example.internal:1"
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", "start " + deep + " " + "words " * 700))
        w.scan()
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith("\nlast text not read: endpoint_loop"), got[0])

    def test_U16_a_plain_file_as_cloud_last_is_refused_and_the_cleaning_keeps_what_it_should(self):
        w = StopWorld(self)
        (w.state / "cloud-last").write_text("a plain file")
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", "lorem " * 1000))
        w.scan()
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].endswith("\nlast text not read: cloud_last_unsafe"), got[0])
        w2 = StopWorld(self)
        base = w2.state / "cloud-last"
        base.mkdir(mode=0o700)
        now = time.time()
        old = now - 8 * 86400
        outside = w2.root / "outside.txt"
        outside.write_text("x")
        os.utime(str(outside), (old, old))
        files = {"old.txt": base / "old.txt", "young.txt": base / "young.txt", "old.dat": base / "old.dat"}
        for path in files.values():
            path.write_text("x")
        for key in ("old.txt", "old.dat"):
            os.utime(str(files[key]), (old, old))
        os.symlink(str(outside), str(base / "link.txt"))
        os.utime(str(base / "link.txt"), (old, old), follow_symlinks=False)
        self.assertEqual(w2.retired.clean_last(base, now), 1, "the cleaning removed a file it should keep")
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["link.txt", "old.dat", "young.txt"],
                         "the cleaning removed a file it should keep")
        self.assertTrue(outside.exists(), "the cleaning removed a file it should keep")

    def test_U17_a_directory_whose_identity_differs_from_the_resolved_path_is_not_read(self):
        w = StopWorld(self)
        rec = w.pod("cb-1788a", "its own text")
        w.stop("cb-1788a", "idle", rec)
        real = os.lstat

        def other(path, *a, **k):
            out = real(path, *a, **k)
            if str(path) == str(rec):   # the check saw another directory than the one opened afterwards
                fields = list(out)
                fields[1] += 1
                return os.stat_result(fields)
            return out
        with mock.patch.object(os, "lstat", other):
            w.scan()
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0].count("its own text"), 0, "a directory with another identity was read")
        self.assertTrue(got[0].endswith("\nlast text not read: no_run_record"), got[0])


class TestStopFinalMessageEdgesMutant(unittest.TestCase):
    """U15..U17: each check on the control and on a mutant of a SCRATCH COPY of the source (outside the repository), each in a fresh process."""
    CHILD = TestStopFinalMessageHardeningMutant.CHILD.replace("TestStopFinalMessageHardening.", "TestStopFinalMessageEdges.")
    CASES = (
        ("cloud_retired", "test_U15_a_rebuilt_endpoint_is_in_neither_the_message_nor_the_file", "        if not changed:\n", "        if True:\n",
         "a rebuilt endpoint reached a lane"),
        ("cloud_retired", "test_U16_a_plain_file_as_cloud_last_is_refused_and_the_cleaning_keeps_what_it_should",
         "if stat.S_ISREG(st.st_mode) and name.endswith(\".txt\") and st.st_mtime < now - LAST_KEEP_SECONDS:\n                    os.unlink(name, dir_fd=dfd)\n",
         "if st.st_mtime < now - LAST_KEEP_SECONDS:\n                    os.unlink(str(base / name))\n",
         "the cleaning removed a file it should keep"),
        ("cloud_retired", "test_U17_a_directory_whose_identity_differs_from_the_resolved_path_is_not_read",
         "if (st.st_dev, st.st_ino) != tuple(ident):", "if False:", "a directory with another identity was read"),
    )
    run_child = TestStopFinalMessageHardeningMutant.run_child

    def test_U15_to_U17_the_control_passes_and_each_mutant_fails_with_the_text_of_the_check(self):
        for name, test, old, new, text in self.CASES:
            code, out = self.run_child(name, test, "", "")
            self.assertEqual(code, 0, "control of %s: %s" % (test, out))
            code, out = self.run_child(name, test, old, new)
            self.assertEqual(code, 1, "mutant of %s: %s" % (test, out))
            self.assertIn(text, out, test)

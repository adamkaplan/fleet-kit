"""The last message of a stopped pod goes to its boss (#1788): S2, S3, S4, S5, S6, U1, U2, U4 of the plan, on fake pods.

A fake audit file, fake run record directories, a recording send and a temporary state directory. Nothing is stopped, nothing is called on a cluster."""
import argparse
import gzip
import json
import os
import random
import re
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
        self.assertEqual(len(chief), 1)
        self.assertTrue(chief[0].startswith(base + " "), "the message is the base text, then the end hook's PR line (#2147)")
        self.assertRegex(chief[0][len(base) + 1:], r"^PR (#\d+ marked ready[^.]*|not marked ready: [^.]+)\.$")
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
        head, kept = sb.load_module("cloud_retired").LAST_HEAD, sb.load_module("cloud_retired").LAST_FILE
        left_out = len(text) - head - kept   # the three cuts come from the named limits, whatever their values
        self.assertIn("%d more characters" % kept, block)
        self.assertIn("%d characters left out" % left_out, block)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        path = base / (rec.name + ".txt")
        self.assertEqual(len(path.read_text()), kept)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(block.split("\n")[1]), head)

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

    def test_R2202_a_retired_worker_before_its_stop_line_is_alerted_to_its_boss_only(self):
        w = StopWorld(self)
        rec = w.pod("cl7699c", "done")
        (rec / "run.json").write_text(json.dumps({"end_hook": {"pushed": False, "head_on": None, "wip_branch": None}}))
        (w.cloud / "cl7699c.json").unlink()   # the lane retired it first
        kept = w.state / "cloud-boss"
        kept.mkdir()
        (kept / "cl7699c.json").write_text(json.dumps({"worker": "cl7699c", "boss": "boss-lane", "ended": "2026-11-01T12:00:00Z"}))
        w.stop("cl7699c", "hard", rec)
        w.scan()
        self.assertTrue(w.to("boss-lane"), "the boss got no notice")
        self.assertEqual(w.to("chief-of-staff"), [], "a clean end went to the Chief of Staff as well")

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
        ("cloud_retired", "test_U11_an_upper_case_endpoint_is_in_neither_the_message_nor_the_file", "low, n, out, i, keep = _fold_case(text), len(text), [], 0, 0",
         "low, n, out, i, keep = text, len(text), [], 0, 0",
         "an upper-case endpoint reached a lane"),
        ("cloud_retired", "test_U12_a_wide_cloud_last_is_tightened_and_a_link_or_a_foreign_one_is_refused", "os.fchmod(dfd, 0o700)", "pass",
         "a cloud-last directory stayed wider than 0700"),
        ("cloud_worker", "test_U13_a_token_in_the_retire_reason_is_not_in_the_message", "_one(_scrub_lib()(reason, (), 200), 120)", "_one(reason, 120)",
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
        text = "see wor" + ENDPOINT + "ker-x-ab12.example.internal:4096 early " + "words " * 700 + " late"
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        self.assertEqual(everything.count("worker-x-ab12.example.internal"), 0, "a rebuilt endpoint reached a lane")
        self.assertIn("early", everything)

    def test_U15_a_text_that_still_changes_after_20_passes_is_refused_as_endpoint_loop(self):
        w = StopWorld(self)
        deep = ENDPOINT
        for _ in range(25):
            deep = "https://wor" + deep + "ker-q.example.internal:1"
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


class TestStopFinalMessageBare(unittest.TestCase):
    """PC-11 (U18..U20): the Lows of the fourth strict security pair, on fake pods."""

    def test_U18_a_bare_endpoint_host_is_in_neither_the_message_nor_the_file(self):
        w = StopWorld(self)
        hosts = ("worker-x-ab12.example.internal", "worker-x-ab12.example.internal:4096", "WORKER-X-AB12.EXAMPLE.INTERNAL", "Worker-X-ab12.Example.Internal:4096")
        text = "see %s and %s early " % hosts[:2] + "words " * 700 + " late %s and %s end" % hosts[2:]
        w.stop("cb-1788a", "idle", w.pod("cb-1788a", text))
        w.scan()
        everything = w.to("boss-lane")[0] + "".join(p.read_text() for p in (w.state / "cloud-last").iterdir())
        self.assertEqual(everything.lower().count("worker-x-ab12.example.internal"), 0, "a bare endpoint host reached a lane")
        self.assertIn("early", everything)

    def test_U19_a_token_straddling_the_120_character_cut_leaves_no_prefix(self):
        w = StopWorld(self)
        rec = w.pod("cp-1788r", "my final words")
        reason = "word " * 22 + TOKEN + " " + "end " * 8
        self.assertLess(reason.index(TOKEN), 120)   # the token starts before the cut at 120 ...
        self.assertGreater(reason.index(TOKEN) + len(TOKEN), 120)   # ... and ends past it: it straddles the cut
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine = mock.Mock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        args = argparse.Namespace(name="cp-1788r", undo=False, reason=reason)
        with mock.patch.dict(cloud.H, {"build_engine": lambda *a, **k: (engine, w.state, None), "gates_caller_kind": lambda *a: ("person",)}), \
                mock.patch.object(cloud, "run_launcher", lambda *a, **k: (0, {"pushed": True, "record": str(rec)}, "note")), \
                mock.patch.object(cloud, "Cluster", lambda *a, **k: mock.MagicMock()), mock.patch.object(cloud, "_audit"), \
                mock.patch.object(cloud, "_registry_note"), mock.patch.object(cloud, "_keep_boss"), mock.patch.object(cloud, "remove_record"):
            cloud.cmd_retire(args, None, None, {"worker": "cp-1788r", "reports_to": "boss-lane"})
        got = w.to("boss-lane")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count(TOKEN[:8]), 0, "part of a token in the retire reason reached a lane")
        self.assertIn("word", got[0])

    def test_U20_the_file_run_directly_runs_and_counts_its_tests(self):
        if os.environ.get("FLEET_U20_NESTED"):
            self.skipTest("the nested direct run: it counts this test, it does not start another")
        path = os.environ.get("FLEET_U20_FILE") or str(CHECK_FILE)
        want = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]).countTestCases()
        env = dict(os.environ, FLEET_U20_NESTED="1")
        env.pop("FLEET_U20_FILE", None)
        done = subprocess.run([sys.executable, path], cwd=str(Path(path).parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=800)
        self.assertIn("Ran %d tests" % want, done.stdout, "the file ran no tests")
        self.assertEqual(done.returncode, 0, done.stdout[-2000:])

    U21_TEXTS = (("worker-", 28000), ("worker-a.", 21700), ("worker-" + "a" * 60, 2900), ("http://", 28000))
    U21_CHILD = (
        "import sys,os,tempfile,importlib.util,glob,shutil\n"
        "real,old,new=sys.argv[1:4]\n"
        "src=open(real).read()\n"
        "assert src.count(old)==1, 'anchor not found once'\n"
        "scratch=tempfile.mkdtemp()\n"
        "[shutil.copy(f,scratch) for f in glob.glob(os.path.join(os.path.dirname(real),'*.py'))]\n"
        "path=os.path.join(scratch,'cloud_retired.py')\n"
        "open(path,'w').write(src.replace(old,new))\n"
        "spec=importlib.util.spec_from_file_location('cloud_retired',path); mod=importlib.util.module_from_spec(spec); sys.modules['cloud_retired']=mod; spec.loader.exec_module(mod)\n"
        "class C: pass\n"
        "text='worker-'*28000\n"
        "c=C(); c.n=0; c.limit=330*len(text)\n"
        "try:\n"
        "    mod._remove_hosts(text, c)\n"
        "except mod.StepLimit:\n"
        "    print('the host scan is not linear: the counter passed', c.limit); sys.exit(1)\n"
        "print('steps', c.n)\n")

    def test_U21_the_host_scan_is_linear_and_counted_by_steps_not_by_the_clock(self):
        class Counter:
            n = 0
            limit = 0
        mod = sb.load_module("cloud_retired")
        for unit, times in self.U21_TEXTS:
            text = unit * times
            self.assertTrue(0.95 * mod.LAST_SCRUB_MAX <= len(text) <= mod.LAST_SCRUB_MAX, len(text))   # a text near the scrub's input maximum
            c = Counter()
            c.n = 0
            c.limit = 330 * len(text)
            try:
                got = mod._remove_hosts(text, c)
            except mod.StepLimit:
                self.fail("the host scan is not linear: %r" % unit)
            self.assertLessEqual(c.n, c.limit, "the host scan is not linear: %r" % unit)
            if unit == "worker-":
                self.assertEqual(got, text)
        # the mutant: a SCRATCH COPY (outside the repository) whose scan moves on by one character instead of jumping to the end of its run
        done = subprocess.run([sys.executable, "-c", self.U21_CHILD, str(BIN / "cloud_retired.py"), "i = max(j + 1, k)", "i = j + 1"], cwd=str(CHECK_FILE.parent),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
        self.assertEqual(done.returncode, 1, "mutant of U21: %s" % done.stdout)
        self.assertIn("the host scan is not linear", done.stdout)

    U22_OLD = re.compile(r"(?:https?://)?worker-[a-z0-9-]+\.[a-z0-9.-]+(:[0-9]+)?", re.I)   # the old host pattern, the oracle (ask #1788 PC-12)
    U22_FIXED = ("worker-b.1.:123456", "worker-b.x-1:4096409623", "worker-ab.b.:40964096409623worker-http://1",
                 "worker-" + "a" * 70 + ".example.internal", "worker-a." + "b" * 300)
    U22_PIECES = ("worker-", "Worker-", "WORKER-", "http://", "https://", "HTTP://", ".", ".", "a", "b", "x-1", "0", "1", "123456", "4096", ":", ":", "/", " ",
                   "\u0130", "\u0131", "\u017f", "\u212a")   # U23: the four letters whose case folding is not ASCII or not one character
    U22_CHILD = (
        "import sys,os,tempfile,importlib.util,glob,shutil\n"
        "sys.path.insert(0, %r)\n"
        "import test_stop_final_message as t\n"
        "real,old,new=sys.argv[1:4]\n"
        "src=open(real).read()\n"
        "if old:\n"
        "    assert src.count(old)==1, 'anchor not found once'\n"
        "    src=src.replace(old,new)\n"
        "scratch=tempfile.mkdtemp()\n"
        "[shutil.copy(f,scratch) for f in glob.glob(os.path.join(os.path.dirname(real),'*.py'))]\n"
        "path=os.path.join(scratch,'cloud_retired.py')\n"
        "open(path,'w').write(src)\n"
        "spec=importlib.util.spec_from_file_location('cloud_retired',path); mod=importlib.util.module_from_spec(spec); sys.modules['cloud_retired']=mod; spec.loader.exec_module(mod)\n"
        "bad=t.TestStopFinalMessageBare.u22_leaks(mod._remove_hosts)\n"
        "if bad:\n"
        "    print('the scan left characters the old pattern removed: %%d texts, the first %%r' %% (len(bad), bad[0])); sys.exit(1)\n"
        "print('no text left more')\n") % str(CHECK_FILE.parent)

    @classmethod
    def u22_leaks(cls, remove):
        def fix(step, text):
            for _ in range(200):
                got = step(text)
                if got == text:
                    return got
                text = got
            raise AssertionError("no fixed point")
        rng = random.Random(7)
        texts = list(cls.U22_FIXED)
        texts += ["".join(rng.choice(cls.U22_PIECES) for _ in range(rng.randint(1, 14))) for _ in range(20000)]
        return [t for t in texts if len(fix(remove, t)) > len(fix(lambda x: cls.U22_OLD.sub("", x), t))]

    def test_U22_the_scan_never_leaves_more_than_the_old_pattern_removed(self):
        mod = sb.load_module("cloud_retired")
        bad = self.u22_leaks(mod._remove_hosts)
        self.assertEqual(bad, [], "the scan left characters the old pattern removed: %d texts, the first %r" % (len(bad), bad[:1]))
        # the mutant: a SCRATCH COPY (outside the repository) whose port reads at most 5 digits again; the control first
        src = str(BIN / "cloud_retired.py")
        run = lambda old, new: subprocess.run([sys.executable, "-c", self.U22_CHILD, src, old, new], cwd=str(CHECK_FILE.parent), stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
        done = run("", "")
        self.assertEqual(done.returncode, 0, "control of U22: %s" % done.stdout)
        old = 'while m < n and low[m] in "0123456789": m += 1'
        done = run(old, 'while m < n and m - k - 1 < 5 and low[m] in "0123456789": m += 1')
        self.assertEqual(done.returncode, 1, "mutant of U22: %s" % done.stdout)
        self.assertIn("the scan left characters the old pattern removed", done.stdout)
        self.assertIn("worker-b.1.:123456", done.stdout)   # the first text is named


    U23_FIXED = ("\u0130" * 40 + " see worker-x-ab12.example.internal:4096 end", "\u0130 worker-x-ab12.example.internal",
                 "wor\u212aer-x-ab12.example.internal", "worker-x\u0131ab12.example.internal", "worker-\u017fa.example.internal:4096")
    U23_CHILD = U22_CHILD.replace("bad=t.TestStopFinalMessageBare.u22_leaks(mod._remove_hosts)", "bad=t.TestStopFinalMessageBare.u23_longer(mod._remove_hosts)").replace(
        "the scan left characters the old pattern removed", "a host was left behind letters whose lower case is longer")

    @classmethod
    def u23_longer(cls, remove):
        bad = []
        for text in cls.U23_FIXED:
            got, want = remove(text), cls.U22_OLD.sub("", text)
            if len(got) > len(want) or "example.internal" in got:
                bad.append(text)
        return bad

    def test_U23_letters_whose_lower_case_is_longer_do_not_hide_a_host(self):
        mod = sb.load_module("cloud_retired")
        bad = self.u23_longer(mod._remove_hosts)
        self.assertEqual(bad, [], "a host was left behind letters whose lower case is longer: %d of 5, the first %r" % (len(bad), bad[:1]))
        self.assertEqual(self.u22_leaks(mod._remove_hosts), [], "the scan left characters the old pattern removed (corpus with the four letters)")
        src = str(BIN / "cloud_retired.py")
        run = lambda old, new: subprocess.run([sys.executable, "-c", self.U23_CHILD, src, old, new], cwd=str(CHECK_FILE.parent), stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
        done = run("", "")
        self.assertEqual(done.returncode, 0, "control of U23: %s" % done.stdout)
        done = run("low, n, out, i, keep = _fold_case(text), len(text), [], 0, 0", "low, n, out, i, keep = text.lower(), len(text), [], 0, 0")
        self.assertEqual(done.returncode, 1, "mutant of U23: %s" % done.stdout)
        self.assertIn("a host was left behind letters whose lower case is longer", done.stdout)


class TestStopFinalMessageBareMutant(unittest.TestCase):
    """U18, U19: each check on the control and on a mutant of a SCRATCH COPY of the source (outside the repository). U20: on a scratch copy of the file."""
    # the scratch copy sits in <top>/bin with <top>/cloud linked to the repository's cloud/, where the launcher's one scrub is found by path
    CHILD = TestStopFinalMessageHardeningMutant.CHILD.replace("TestStopFinalMessageHardening.", "TestStopFinalMessageBare.").replace(
        "    scratch=tempfile.mkdtemp()\n",
        "    top=tempfile.mkdtemp(); scratch=os.path.join(top,'bin'); os.mkdir(scratch); os.symlink(os.path.join(os.path.dirname(os.path.dirname(real)),'cloud'), os.path.join(top,'cloud'))\n")
    CASES = (
        ("cloud_retired", "test_U18_a_bare_endpoint_host_is_in_neither_the_message_nor_the_file", "        start = j\n",
         "        start = j\n        if not (low.startswith(\"https://\", j - 8) and j >= 8 or low.startswith(\"http://\", j - 7) and j >= 7):\n"
         "            i = k\n            continue\n",
         "a bare endpoint host reached a lane"),
        ("cloud_worker", "test_U19_a_token_straddling_the_120_character_cut_leaves_no_prefix", "_one(_scrub_lib()(reason, (), 200), 120)",
         "_scrub_lib()(_one(reason, 120), (), 200)", "part of a token in the retire reason reached a lane"),
    )
    run_child = TestStopFinalMessageHardeningMutant.run_child

    def test_U18_U19_the_control_passes_and_each_mutant_fails_with_the_text_of_the_check(self):
        for name, test, old, new, text in self.CASES:
            code, out = self.run_child(name, test, "", "")
            self.assertEqual(code, 0, "control of %s: %s" % (test, out))
            code, out = self.run_child(name, test, old, new)
            self.assertEqual(code, 1, "mutant of %s: %s" % (test, out))
            self.assertIn(text, out, test)

    def u20(self, path):
        env = dict(os.environ, FLEET_U20_FILE=path)
        code = ("import sys,unittest\nsys.path.insert(0, %r)\n"
                "suite=unittest.defaultTestLoader.loadTestsFromName('test_stop_final_message.TestStopFinalMessageBare.test_U20_the_file_run_directly_runs_and_counts_its_tests')\n"
                "r=unittest.TextTestRunner(verbosity=0).run(suite)\nsys.exit(0 if r.wasSuccessful() else 1)\n") % str(CHECK_FILE.parent)
        done = subprocess.run([sys.executable, "-c", code], cwd=str(CHECK_FILE.parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=900)
        return done.returncode, done.stdout

    def test_U20_a_copy_without_the_main_entry_is_seen(self):
        if os.environ.get("FLEET_U20_NESTED"):
            self.skipTest("the nested direct run does not start another")
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        here = os.path.join(scratch, "tests", "switchboard")
        shutil.copytree(str(CHECK_FILE.parent / "support"), os.path.join(here, "support"), ignore=shutil.ignore_patterns("__pycache__"))
        os.symlink(str(BIN), os.path.join(scratch, "bin"))
        src = CHECK_FILE.read_text()
        tail = 'if __name__ == "__main__":\n    unittest.main()\n'
        self.assertEqual(src.count(tail), 1, "anchor not found once")
        copy = os.path.join(here, "test_stop_final_message.py")
        with open(copy, "w") as handle:
            handle.write(src.replace(tail, ""))
        code, out = self.u20(os.path.join(here, "test_stop_final_message.py"))
        self.assertEqual(code, 1, "mutant of U20: %s" % out)
        self.assertIn("the file ran no tests", out)
        code, out = self.u20(str(CHECK_FILE))
        self.assertEqual(code, 0, "control of U20: %s" % out)


class TestEndNoticeHeadLine(unittest.TestCase):
    """#2267 S2, S3: the end notice's one line for the last pushed head and its checks."""

    def world(self, name, hook, head_sha="a" * 40, repo="o/r"):
        w = StopWorld(self)
        rec = w.pod(name, "words")
        (rec / "run.json").write_text(json.dumps({"head_sha": head_sha, "end_hook": hook}))
        (w.cloud / (name + ".json")).write_text(json.dumps({"worker": name, "reports_to": "boss-lane", "repo": repo}))
        w.stop(name, "hard", rec)
        return w

    def run_scan(self, w, reader):
        w.mod_scan = w.mod.scan_audit(w.audit, w.state_file, w.cloud, w.reports, lambda to, text: w.sent.append((to, text)),
                                      sb.load_module("record_json").parse_bounded, None, None, head_checks=reader)

    PUSHED = {"pushed": False, "wip_branch": None, "head_on": "feat/x"}

    def test_S2_green_red_and_pending_each_give_one_line_with_branch_sha_and_state(self):
        for state, want in (("passed", "checks passed"), ("failed: lint, unit", "checks failed: lint, unit"), ("pending", "checks pending")):
            w = self.world("cb-2267a", self.PUSHED)
            self.run_scan(w, lambda repo, sha, state=state: state)
            msg = w.to("boss-lane")[0]
            self.assertIn("Last head: feat/x@aaaaaaaa, %s." % want, msg)
            self.assertEqual(msg.count("Last head:"), 1)

    def test_S2_control_without_a_reader_the_notice_has_no_such_line(self):
        w = self.world("cb-2267b", self.PUSHED)
        self.run_scan(w, None)
        self.assertNotIn("Last head", w.to("boss-lane")[0])

    def test_S3_nothing_pushed_says_so_and_is_sent(self):
        w = self.world("cb-2267c", {"pushed": False, "wip_branch": None, "head_on": None})
        self.run_scan(w, lambda repo, sha: "passed")
        self.assertIn("Last head: nothing pushed.", w.to("boss-lane")[0])

    def test_S3_a_failing_or_slow_reader_says_unknown_and_the_notice_is_still_sent(self):
        def boom(repo, sha):
            raise OSError("down")
        for reader in (boom, lambda repo, sha: None):
            w = self.world("cb-2267d", self.PUSHED)
            self.run_scan(w, reader)
            self.assertEqual(len(w.to("boss-lane")), 1)
            self.assertEqual(len(w.to("chief-of-staff")), 1)
            self.assertIn("Last head: feat/x@aaaaaaaa, checks unknown.", w.to("boss-lane")[0])

    def test_S3_a_missing_sha_says_head_unknown(self):
        w = self.world("cb-2267e", self.PUSHED, head_sha=None)
        self.run_scan(w, lambda repo, sha: "passed")
        self.assertIn("Last head: head unknown.", w.to("boss-lane")[0])

    def test_wip_branch_is_named_with_sha_or_sha_unknown(self):
        hook = {"pushed": True, "wip_branch": "w1/wip", "head_on": None}
        w = self.world("cb-2267f", hook)
        self.run_scan(w, lambda repo, sha: "passed")
        self.assertIn("Last head: wip branch w1/wip@aaaaaaaa, checks passed.", w.to("boss-lane")[0])
        w = self.world("cb-2267g", hook, head_sha=None)
        self.run_scan(w, lambda repo, sha: "passed")
        self.assertIn("Last head: wip branch w1/wip, sha unknown.", w.to("boss-lane")[0])
        w = self.world("cb-2267h", dict(hook, wip_branch="bad branch!"))
        self.run_scan(w, lambda repo, sha: "passed")
        self.assertIn("Last head: head unknown.", w.to("boss-lane")[0])

    def line(self, hook, state="passed", sha="a" * 40):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        Path(d, "run.json").write_text(json.dumps({"head_sha": sha, "end_hook": hook}))
        return sb.load_module("cloud_end_alert").head_line(d, "o/r", lambda repo, s: state, sb.load_module("record_json").parse_bounded)

    def test_head_line_sanitises_state_and_branch(self):
        self.assertEqual(self.line(self.PUSHED, "bad\x00<x>\nstate"), "Last head: feat/x@aaaaaaaa, checks bad?? x ?state.".replace("?? x ?", "?<x>?").replace("<x>", "?x?"))
        self.assertEqual(self.line(self.PUSHED, "ok"), "Last head: feat/x@aaaaaaaa, checks ok.")
        for bad in ("feat/x y", "a;b", "unknown", "x" * 101, 5):
            self.assertEqual(self.line(dict(self.PUSHED, head_on=bad)), "Last head: head unknown.")


class TestEndNoticeOwnPushes(unittest.TestCase):
    """Fix forward of #2267: a pod that pushed to its own branch (the end hook saw only the launch branch) is not 'nothing pushed'."""

    NOTHING = {"pushed": False, "wip_branch": None, "head_on": None}
    PUSHED = {"pushed": False, "wip_branch": None, "head_on": "feat/x"}
    SHA = "b" * 40

    def audit_with(self, *lines):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "audit.jsonl")
        Path(path).write_text("".join(json.dumps(x) + "\n" for x in lines))
        return path

    def sent(self, worker, branch, sha):
        return {"event": "push_watch.sent", "subject": worker, "detail": {"to": "boss", "sha": sha[:8], "branch": branch, "head": sha}}

    def born(self, worker):
        return {"event": "cloud.created", "subject": worker, "detail": {}}

    def world(self, name, hook, audit_path, head_sha=None):
        w = StopWorld(self)
        rec = w.pod(name, "words")
        (rec / "run.json").write_text(json.dumps({"head_sha": head_sha, "end_hook": hook}))
        (w.cloud / (name + ".json")).write_text(json.dumps({"worker": name, "reports_to": "boss-lane", "repo": "o/r"}))
        w.stop(name, "hard", rec)
        parse = sb.load_module("record_json").parse_bounded
        mod = sb.load_module("cloud_end_alert")
        w.mod.scan_audit(w.audit, w.state_file, w.cloud, w.reports, lambda to, text: w.sent.append((to, text)), parse, None, None,
                         head_checks=lambda repo, sha: "passed", last_push=mod.push_reader(audit_path, parse))
        return w.to("boss-lane")[0]

    def test_own_push_in_the_audit_is_named_when_the_hook_saw_nothing(self):
        path = self.audit_with(self.sent("cb-own1", "old/b", "c" * 40), self.sent("other", "z/z", "d" * 40), self.sent("cb-own1", "cb-own1/work", self.SHA))
        self.assertIn("Last head: cb-own1/work@bbbbbbbb, checks passed.", self.world("cb-own1", self.NOTHING, path))

    def test_control_no_push_anywhere_says_nothing_pushed(self):
        path = self.audit_with(self.born("cb-own2"), self.sent("other", "z/z", "d" * 40))
        self.assertIn("Last head: nothing pushed.", self.world("cb-own2", self.NOTHING, path))

    def test_a_small_live_file_without_the_pods_creation_is_head_unknown(self):
        path = self.audit_with(self.sent("other", "z/z", "d" * 40), self.born("someone-else"))   # no cloud.created of cb-own20 in view
        self.assertLess(os.path.getsize(path), sb.load_module("cloud_end_alert").READ_BYTES)
        self.assertIn("Last head: head unknown.", self.world("cb-own20", self.NOTHING, path))

    def test_a_rotated_audit_with_the_pods_creation_in_the_archive_is_head_unknown(self):
        path = self.audit_with(self.sent("other", "z/z", "d" * 40))
        with gzip.open(os.path.join(os.path.dirname(path), "audit-2026-11-01.jsonl.gz"), "wt") as gz:   # the archive holds the creation and the push
            gz.write(json.dumps(self.born("cb-own21")) + "\n" + json.dumps(self.sent("cb-own21", "own/b", self.SHA)) + "\n")
        self.assertIn("Last head: head unknown.", self.world("cb-own21", self.NOTHING, path))

    def test_the_pods_creation_in_view_and_a_push_after_it_is_named(self):
        path = self.audit_with(self.born("cb-own22"), self.sent("cb-own22", "own/b", self.SHA))
        self.assertIn("Last head: own/b@bbbbbbbb", self.world("cb-own22", self.NOTHING, path))

    def test_the_hooks_own_head_wins(self):
        path = self.audit_with(self.sent("cb-own3", "cb-own3/work", self.SHA))
        self.assertIn("Last head: feat/x@aaaaaaaa, checks passed.", self.world("cb-own3", self.PUSHED, path, head_sha="a" * 40))

    def test_unreadable_audit_keeps_the_old_behaviour(self):
        self.assertIn("Last head: nothing pushed.", self.world("cb-own4", self.NOTHING, "/nonexistent/audit.jsonl"))

    def test_each_guard_of_the_reader_drops_its_own_hostile_line(self):
        for branch, sha in (("bad branch\n<x>", self.SHA), ("a" * 31, self.SHA), ("ghp_x", self.SHA), ("sk-ab", self.SHA), ("AKIAxx", self.SHA), ("xoxb-1", self.SHA), ("eyJx", self.SHA), ("github_pat_x", self.SHA), ("unknown", self.SHA),
                            ("a/" * 50 + "b", self.SHA), ("ok/b", "bbbbbbbb"), ("ok/b", "B" * 40), ("ok/b", 5), (5, self.SHA)):
            path = self.audit_with(self.born("cb-own5"), {"event": "push_watch.sent", "subject": "cb-own5", "detail": {"branch": branch, "head": sha}})
            self.assertIn("Last head: nothing pushed.", self.world("cb-own5", self.NOTHING, path), (branch, sha))

    def test_garbage_and_misshapen_lines_are_skipped_and_an_earlier_push_wins(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "audit.jsonl")
        Path(path).write_text("\n".join([json.dumps(self.sent("cb-own13", "ok/b", self.SHA)), "push_watch.sent {not json", json.dumps(["push_watch.sent"]),
                                          json.dumps({"event": "push_watch.sent", "subject": "cb-own13", "detail": "str"})]) + "\n")
        self.assertIn("Last head: ok/b@bbbbbbbb", self.world("cb-own13", self.NOTHING, path))

    def test_a_line_written_by_the_real_appender_is_read_back(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "audit.jsonl")
        sb.AuditLog(path).append("daemon", "push_watch.sent", "cb-own14", {"to": "boss", "sha": self.SHA[:8], "branch": "own/b", "head": self.SHA})   # redaction is by key name
        self.assertIn("Last head: own/b@bbbbbbbb", self.world("cb-own14", self.NOTHING, path))

    def test_another_pods_creation_does_not_reset_this_pods_push(self):
        other = {"event": "cloud.created", "subject": "someone-else", "detail": {}}
        path = self.audit_with(self.sent("cb-own15", "own/b", self.SHA), other)
        self.assertIn("Last head: own/b@bbbbbbbb", self.world("cb-own15", self.NOTHING, path))

    def test_no_audit_path_reads_no_file_named_none(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        Path(d, "None").write_text(json.dumps(self.sent("cb-own16", "own/b", self.SHA)) + "\n")
        here = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, here)
        self.assertIsNone(sb.load_module("cloud_end_alert").push_reader(None, sb.load_module("record_json").parse_bounded)("cb-own16"))

    def test_a_valid_push_survives_a_newer_hostile_line(self):
        path = self.audit_with(self.sent("cb-own7", "ok/b", self.SHA), self.sent("cb-own7", "bad branch", "c" * 40))
        self.assertIn("Last head: ok/b@bbbbbbbb", self.world("cb-own7", self.NOTHING, path))

    def test_a_push_of_an_earlier_pod_of_the_same_name_is_not_this_pods(self):
        created = {"event": "cloud.created", "subject": "cb-own8", "detail": {}}
        path = self.audit_with(self.sent("cb-own8", "old/b", self.SHA), created)
        self.assertIn("Last head: nothing pushed.", self.world("cb-own8", self.NOTHING, path))
        path = self.audit_with(self.sent("cb-own8", "old/b", self.SHA), created, self.sent("cb-own8", "new/b", "c" * 40))
        self.assertIn("Last head: new/b@cccccccc", self.world("cb-own8", self.NOTHING, path))

    def test_only_the_tail_of_a_large_audit_is_read_and_a_cut_push_line_is_not_reported(self):
        mod = sb.load_module("cloud_end_alert")
        pad = json.dumps({"event": "x", "subject": "p", "detail": {"t": "y" * 1000}}) + "\n"
        push = json.dumps(self.sent("cb-own9", "gone/b", self.SHA)) + "\n"
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "audit.jsonl")
        want = mod.READ_BYTES - len(push) // 2   # bytes after the push line: the read window then starts in the middle of the push line
        empty = len(json.dumps({"event": "x", "subject": "p", "detail": {"t": ""}})) + 1
        body = push + json.dumps({"event": "x", "subject": "p", "detail": {"t": "y" * (want - empty)}}) + "\n"
        Path(path).write_text(body)
        self.assertTrue(0 < len(body) - mod.READ_BYTES < len(push), "fixture: the window must start inside the push line")
        self.assertIn("Last head: head unknown.", self.world("cb-own9", self.NOTHING, path))   # not proven: the window does not show the pod's start
        Path(path).write_text(pad * (mod.READ_BYTES // len(pad) // 2) + json.dumps({"event": "cloud.created", "subject": "cb-own9", "detail": {}}) + "\n" + pad * (mod.READ_BYTES // len(pad) // 2 + 5))
        self.assertGreater(os.path.getsize(path), mod.READ_BYTES)
        self.assertIn("Last head: nothing pushed.", self.world("cb-own9", self.NOTHING, path))   # proven: the pod's creation is in view
        Path(path).write_text(pad * (mod.READ_BYTES // len(pad) + 5) + json.dumps(self.sent("cb-own9", "near/b", self.SHA)) + "\n")
        self.assertIn("Last head: near/b@bbbbbbbb", self.world("cb-own9", self.NOTHING, path))

    def test_a_clean_hook_branch_without_a_sha_falls_back_to_the_own_push(self):
        path = self.audit_with(self.sent("cb-own17", "own/b", self.SHA))
        self.assertIn("Last head: own/b@bbbbbbbb", self.world("cb-own17", self.PUSHED, path, head_sha=None))

    def test_another_event_naming_push_watch_sent_is_no_push(self):
        path = self.audit_with(self.born("cb-own18"), {"event": "push_watch.quiet", "subject": "cb-own18", "detail": {"note": "push_watch.sent", "branch": "own/b", "head": self.SHA}})
        self.assertIn("Last head: nothing pushed.", self.world("cb-own18", self.NOTHING, path))

    def test_a_hook_with_an_error_and_no_head_falls_back_to_the_own_push(self):
        path = self.audit_with(self.sent("cb-own10", "own/b", self.SHA))
        self.assertIn("Last head: own/b@bbbbbbbb", self.world("cb-own10", {"error": "x", "head_on": None, "wip_branch": None}, path))

    def test_a_fifo_or_symlink_audit_is_not_read(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fifo = os.path.join(d, "f")
        os.mkfifo(fifo)
        real = self.audit_with(self.sent("cb-own11", "own/b", self.SHA))
        link = os.path.join(d, "l")
        os.symlink(real, link)
        for path in (fifo, link):
            self.assertIn("Last head: nothing pushed.", self.world("cb-own11", self.NOTHING, path))

    def test_the_daemon_tick_wires_the_reader_to_its_audit_file(self):
        w = StopWorld(self)
        rec = w.pod("cb-own12", "words")
        (rec / "run.json").write_text(json.dumps({"head_sha": None, "end_hook": self.NOTHING}))
        (w.cloud / "cb-own12.json").write_text(json.dumps({"worker": "cb-own12", "reports_to": "boss-lane", "repo": "o/r"}))
        engine = mock.Mock()
        engine.state_dir = str(w.state)
        engine.audit.path = self.audit_with(self.sent("cb-own12", "own/b", self.SHA))
        with mock.patch.dict(os.environ, {"CW_CLOUD_AUDIT": str(w.audit)}), mock.patch.object(sb, "cloud_head_checks", lambda e, repo, sha: "passed"):
            sb.cloud_end_tick(engine, None)   # the first pass only marks the end of the file
            w.stop("cb-own12", "hard", rec)
            sb.cloud_end_tick(engine, None)
        self.assertTrue(engine.send.called)
        self.assertIn("Last head: own/b@bbbbbbbb, checks passed.", " ".join(str(c) for c in engine.send.call_args_list))

    def test_a_raising_reader_keeps_the_old_behaviour(self):
        w = StopWorld(self)
        rec = w.pod("cb-own6", "words")
        (rec / "run.json").write_text(json.dumps({"head_sha": None, "end_hook": self.NOTHING}))
        (w.cloud / "cb-own6.json").write_text(json.dumps({"worker": "cb-own6", "reports_to": "boss-lane", "repo": "o/r"}))
        w.stop("cb-own6", "hard", rec)
        def boom(worker):
            raise OSError("x")
        w.mod.scan_audit(w.audit, w.state_file, w.cloud, w.reports, lambda to, text: w.sent.append((to, text)),
                         sb.load_module("record_json").parse_bounded, None, None, head_checks=lambda r, s: "passed", last_push=boom)
        self.assertIn("Last head: nothing pushed.", w.to("boss-lane")[0])


class TestCloudHeadChecks(unittest.TestCase):
    """#2267: the daemon's cloud_head_checks over a stubbed engine.intents._api."""

    def check(self, api):
        eng = mock.Mock()
        eng.intents._api = api
        return sb.cloud_head_checks(eng, "o/r", "a" * 40)

    @staticmethod
    def page(*runs):
        return {"check_runs": [dict(id=i + 1, name=n, status=s, conclusion=c, started_at="2026-01-01T00:00:0%dZ" % i) for i, (n, s, c) in enumerate(runs)], "total_count": len(runs)}

    def test_each_state(self):
        self.assertEqual(self.check(lambda path, repo: self.page(("a", "completed", "success"))), "passed")
        self.assertEqual(self.check(lambda path, repo: self.page(("a", "completed", "success"), ("b", "in_progress", None))), "pending")
        self.assertEqual(self.check(lambda path, repo: self.page(("lint", "completed", "failure"), ("a", "completed", "success"))), "failed: lint")
        self.assertEqual(self.check(lambda path, repo: self.page()), "none yet")

    def test_refusal_and_exception_give_unknown(self):
        self.assertEqual(self.check(lambda path, repo: None), "unknown")
        def boom(path, repo):
            raise OSError("down")
        self.assertEqual(self.check(boom), "unknown")


if __name__ == "__main__":
    unittest.main()

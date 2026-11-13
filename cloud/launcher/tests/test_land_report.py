"""Ask #2448: the daily land report (cloud/launcher/land_report.py), scenarios S1 to S8. Offline: fake run records in a temporary directory, a fake clock,
recording senders, a fake git. Each mutant is a scratch copy of the module with one text replaced (asserted to occur once); its control (the real module
through the same check) runs first and must pass, and the mutant must fail with the anchored text."""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(os.path.dirname(HERE), "land_report.py")
sys.path.insert(0, os.path.dirname(HERE))
import land_report as real  # noqa: E402

NOW = dt.datetime(2026, 11, 12, 13, 5, tzinfo=dt.timezone.utc)
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def mutant(old, new):
    with open(SOURCE) as f:
        src = f.read()
    assert src.count(old) == 1, old
    spec = importlib.util.spec_from_loader("land_report_mutant", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src.replace(old, new), "land_report_mutant", "exec"), mod.__dict__)
    return mod


class Root:
    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.dir, self.n = self.tmp.name, 0

    def add(self, worker="bob-1", repo="o/r", end=None, minutes=10, code=0, tail="", sha=None, reason="stop", land=True, raw=None, boss=None):
        self.n += 1
        end = end or NOW - dt.timedelta(hours=2)
        rid = "%s-20261112T0%05dZ-%06x" % (worker, self.n, self.n)
        d = os.path.join(self.dir, rid)
        os.mkdir(d)
        runs = [{"name": "land", "exit": code, "woke": False, "log_tail": tail}] if land else []
        boss = boss or __import__("re").sub(r"-\d+$", "", worker)   # the lane: the boss the record carries
        rec = {"worker": worker, "boss": boss, "repository": repo, "start": iso(end - dt.timedelta(minutes=minutes)), "end": iso(end), "exit_reason": reason,
               "head_sha": sha, "end_hook": {"pushed": True, "run_logs": {"runs": runs, "wake_lines": []}}}
        with open(os.path.join(d, "run.json"), "w") as f:
            f.write(raw if raw is not None else json.dumps(rec))
        return d


def report(mod, root, landed=(), bound=50, now=NOW, unread=None):
    recs, bad = mod.load_records(root.dir, now - dt.timedelta(hours=24), now)
    prev, _ = mod.load_records(root.dir, now - dt.timedelta(hours=48), now - dt.timedelta(hours=24))
    return mod.build(recs, bad if unread is None else unread, list(landed), bound, prev, "2026-11-12")[0]


def commit(sha, lines, files=1, rounds=1):
    return {"sha": sha.ljust(40, "0"), "files": files, "lines": lines, "rounds": rounds}


class S1(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        for lane, codes in (("a", [0, 0, 0, 18, 13, 3]), ("b", [0, 0, 18, 13, 124, 77])):
            for c in codes:
                r.add(worker=lane + "-1", code=c)
        for _ in range(2):   # outside the window: never counted
            r.add(worker="a-1", end=NOW - dt.timedelta(hours=30))
        out = report(mod, r, landed=[commit("1", 5)])
        a = [l for l in out.splitlines() if "/ a:" in l][0]
        b = [l for l in out.splitlines() if "/ b:" in l][0]
        self.assertIn("attempted 6, landed 3, failed: council 1, tests 1, environment 1, time limit 0, other 0", a, "S1 lane a")
        self.assertIn("attempted 6, landed 2, failed: council 1, tests 1, environment 0, time limit 1, other 1", b, "S1 lane b")

    def test_control(self):
        self.check(real)

    def test_mutant_counts_every_record(self):
        self.check(real)
        m = mutant("        if not start <= ended < end:\n            continue\n", "")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S1 lane a", str(cm.exception))


class S2(unittest.TestCase):
    def build(self, mod, today=(10, 20, 30, 40, 100), yesterday=(25,)):
        r = Root(self)
        for m in today:
            r.add(minutes=m)
        for m in yesterday:
            r.add(minutes=m, end=NOW - dt.timedelta(hours=30))
        r.add(worker="small-1", minutes=5)
        return report(mod, r)

    def check(self, mod):
        out = self.build(mod)
        self.assertIn("median 30 min, p90 100 min (n=5), trend +5 min", out, "S2 numbers")
        self.assertIn("/ small: attempted 1", out)
        self.assertIn("land wall time: n/a", [l for l in out.splitlines() if "land wall time" in l][-1], "S2 small lane")

    def test_control(self):
        self.check(real)

    def test_mutant_mean(self):
        self.check(real)
        m = mutant("def median(values):\n    v = sorted(values)\n", "def median(values):\n    v = sorted(values); return sum(v) / len(v) if v else None\n")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S2 numbers", str(cm.exception))


STEP = "x\nstep=tests:server seconds=120\nstep=tests:pg seconds=60\n"


class S3(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        r.add(tail=STEP)
        r.add(tail="step=tests:server seconds=240\nstep=tests:pg seconds=100\n")
        r.add(tail="no times here")
        out = report(mod, r)
        self.assertIn("tests:server: median 180 s (n=2); no step data 1", out, "S3 server")
        self.assertIn("tests:pg: median 80 s (n=2); no step data 1", out, "S3 pg")

    def test_control(self):
        self.check(real)

    def test_mutant_missing_is_zero(self):
        self.check(real)
        m = mutant("                else:\n                    none += 1\n", "                else:\n                    vals.append(0.0)\n")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S3 server", str(cm.exception))


class S4(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        r.add()
        out = report(mod, r, landed=[commit("%d" % i, n, files=i + 1) for i, n in enumerate((3, 8, 12, 40, 90, 300))])
        self.assertIn("share over 50 lines: 2 of 6", out, "S4 share")
        self.assertIn("median 26 lines", out)
        self.assertIn("median 3.5 files", out)
        self.assertIn("share over 8 lines: 4 of 6", report(mod, r, landed=[commit("%d" % i, n) for i, n in enumerate((3, 8, 12, 40, 90, 300))], bound=8), "S4 bound is a setting")

    def test_control(self):
        self.check(real)

    def test_mutant_at_least(self):
        self.check(real)
        m = mutant('over = sum(1 for c in size if c["lines"] > bound)', 'over = sum(1 for c in size if c["lines"] >= bound)')
        # 50 itself is not in the data, so the mutant is caught by the bound-8 case (8 is not over 8)
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S4 bound", str(cm.exception))

    def test_small_bound_setting(self):
        self.assertEqual(real.small_bound({}), 50)
        self.assertEqual(real.small_bound({"CW_LAND_REPORT_SMALL_LINES": "20"}), 20)
        self.assertEqual(real.small_bound({"CW_LAND_REPORT_SMALL_LINES": "-3"}), 50)
        self.assertEqual(real.small_bound({"CW_LAND_REPORT_SMALL_LINES": "x"}), 50)


class S5(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        r.add()
        out = report(mod, r, landed=[commit("%d" % i, 5, rounds=n) for i, n in enumerate((1, 1, 2, 4))])
        self.assertIn("median 1.5, max 4, 3 or more: 1", out, "S5 rounds")

    def test_control(self):
        self.check(real)

    def test_mutant_sum(self):
        self.check(real)
        m = mutant('"median %s, max %d, 3 or more: %d" % (num(median(rounds))', '"median %s, max %d, 3 or more: %d" % (num(sum(rounds))')
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S5 rounds", str(cm.exception))

    def test_trailer_parse(self):
        self.assertEqual(real.parse_trailer("files=3 lines=40 rounds=2"), {"files": 3, "lines": 40, "rounds": 2})
        self.assertEqual(real.parse_trailer("unit 5 of 5"), {})
        self.assertEqual(real.parse_trailer(None), {})


class S6(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        r.add(worker="w-1", sha="a" * 40, reason="stop")
        r.add(worker="w-2", sha="b" * 40, reason="idle limit")
        r.add(worker="w-3", sha=None, reason="hard limit")
        r.add(worker="w-4", sha="c" * 40, reason="stop")
        out = report(mod, r, landed=[commit("a" * 40, 5)])
        self.assertIn("pods ended unlanded: 3 of 4", out, "S6 count")
        for n, why in (("w-2", "idle"), ("w-3", "hard"), ("w-4", "stop")):
            self.assertRegex(out, r"%s \(%s" % (n, why))
        self.assertNotIn("w-1 (", out)

    def test_control(self):
        self.check(real)

    def test_mutant_every_pod(self):
        self.check(real)
        m = mutant("unlanded = [r for r in records if not is_landed(r)]", "unlanded = list(records)")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S6 count", str(cm.exception))


class S7(unittest.TestCase):
    def check(self, mod):
        r = Root(self)
        for c in (0, 0, 18):
            r.add(code=c)
        r.add(raw="{not json")
        real_dir = r.add()
        os.rename(os.path.join(real_dir, "run.json"), os.path.join(real_dir, "target"))
        os.symlink("target", os.path.join(real_dir, "run.json"))
        r.add(raw=json.dumps({"pad": "x" * (5 * 1024 * 1024)}))
        r.add(tail=TOKEN + " Bearer abcdefghijkl")
        out = report(mod, r)
        self.assertIn("3 records not read", out, "S7 unread")
        self.assertIn("attempted 4, landed 3", out, "S7 others")
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("abcdefghijkl", out)

    def test_control(self):
        self.check(real)

    def test_mutant_stops_at_corrupt(self):
        self.check(real)
        m = mutant("        if rec is None:\n            unread += 1\n            continue\n", "        if rec is None:\n            break\n")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S7", str(cm.exception))

    def test_scrub(self):
        self.assertNotIn(TOKEN, real.scrub("a " + TOKEN + " b"))
        self.assertEqual(real.scrub("a\u202eb\x00c\n"), "abc\n")


class S8(unittest.TestCase):
    def setUp(self):
        self.root = Root(self)
        self.state = os.path.join(self.root.dir, ".land-report.json")
        self.env = {"CW_LAND_REPORT_ISSUE": "o/r#7"}
        self.sent, self.posted, self.logs = [], [], []

    def run_pass(self, mod, now, landed=(), ok=True):
        def send(text, issue):
            self.sent.append((text, issue))
            return ok

        def comment(line, issue):
            self.posted.append((line, issue))
            return ok
        return mod.daily_pass(self.env, now, self.root.dir, self.state, send=send, comment=comment, log=self.logs.append, git=lambda *a: list(landed))

    def check(self, mod):
        self.root.add(sha="a" * 40)
        self.sent, self.posted = [], []
        self.assertEqual(self.run_pass(mod, NOW.replace(hour=12), [commit("a", 5)]), "not due")
        self.assertEqual(self.run_pass(mod, NOW, [commit("a", 5)]), "sent")
        self.assertEqual(self.run_pass(mod, NOW.replace(minute=40), [commit("a", 5)]), "not due", "S8 repeat")
        self.assertEqual((len(self.sent), len(self.posted)), (1, 1), "S8 one each")
        self.assertEqual(self.sent[0][1], "o/r#7")
        self.assertEqual(self.run_pass(mod, NOW + dt.timedelta(days=1)), "sent")
        self.assertEqual((len(self.sent), len(self.posted)), (2, 2), "S8 next day")
        self.assertIn("no landings", self.sent[1][0])
        self.assertEqual(len(self.sent[1][0].splitlines()), 1)

    def test_control(self):
        self.check(real)

    def test_mutant_no_last_day(self):
        self.check(real)
        m = mutant('    return now.hour >= SEND_HOUR and not done', '    return now.hour >= SEND_HOUR')
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("S8 repeat", str(cm.exception))

    def test_failed_part_is_retried_alone(self):
        self.root.add()
        self.assertEqual(self.run_pass(real, NOW, ok=False), "partial")
        self.assertEqual(real.read_state(self.state)["message"], False)
        self.sent, self.posted = [], []
        self.assertEqual(self.run_pass(real, NOW.replace(minute=30)), "sent")
        self.assertEqual((len(self.sent), len(self.posted)), (1, 1))
        self.assertEqual(self.run_pass(real, NOW.replace(minute=50)), "not due")

    def test_comment_alone_retried(self):
        self.root.add()
        calls = []
        real.daily_pass(self.env, NOW, self.root.dir, self.state, send=lambda t, i: True, comment=lambda t, i: False, git=lambda *a: [])
        real.daily_pass(self.env, NOW.replace(minute=20), self.root.dir, self.state, send=lambda t, i: calls.append("m") or True,
                        comment=lambda t, i: calls.append("c") or True, git=lambda *a: [])
        self.assertEqual(calls, ["c"])

    def test_raising_sender_and_missing_issue(self):
        self.root.add()

        def boom(*a):
            raise OSError("x " + TOKEN)
        self.assertEqual(real.daily_pass(self.env, NOW, self.root.dir, self.state, send=boom, comment=boom, log=self.logs.append, git=lambda *a: []), "partial")
        self.assertNotIn(TOKEN, " ".join(self.logs))
        self.env = {}
        os.unlink(self.state)
        self.assertEqual(self.run_pass(real, NOW), "failed")

    def test_sender_calls_are_argv_lists(self):
        got = []
        real.send_message("t", "o/r#7", run=lambda argv, **kw: got.append((argv, kw)) or type("R", (), {"returncode": 0})())
        real.post_comment("c", "o/r#7", run=lambda argv, **kw: got.append((argv, kw)) or type("R", (), {"returncode": 0})())
        self.assertEqual(got[0][0], ["fl" + "eet-switchboard", "send", "cos", "--issue", "o/r#7", "t"])
        self.assertEqual(got[1][0], ["gh", "issue", "comment", "7", "--repo", "o/r", "--body", "c"])
        self.assertTrue(all(kw["timeout"] == 60 and "shell" not in kw for _, kw in got))
        self.assertFalse(real.post_comment("c", "--bad", run=lambda *a, **k: 1 / 0))

    def test_git_reads(self):
        out = "\x1e" + "a" * 40 + "\x1ffiles=2 lines=9\x1frounds=3\n\x1e" + "b" * 40 + "\x1f\x1f"
        seen = []
        got = real.git_landed("/x", "origin/main", NOW, run=lambda argv, **kw: seen.append(argv) or type("R", (), {"returncode": 0, "stdout": out})())
        self.assertEqual(got, [{"sha": "a" * 40, "files": 2, "lines": 9, "rounds": 3}])
        self.assertEqual(seen[0][:4], ["git", "-C", "/x", "log"])
        self.assertIsNone(real.git_landed("/x", "-bad", NOW))
        self.assertIsNone(real.git_landed("", "origin/main", NOW))
        self.assertIsNone(real.git_landed("/x", "origin/main", NOW, run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})()))


WORKER = os.path.join(os.path.dirname(HERE), "cloud_worker.py")


class FakeRecorder:
    def __init__(self, root, boom=False):
        self._root, self._boom = root, boom

    def root(self):
        if self._boom:
            raise OSError("no root")
        return self._root


class FakeLauncher:
    def __init__(self, root, env, boom=False):
        self.recorder, self.env = FakeRecorder(root, boom), env


class RunSmoke(unittest.TestCase):
    """#2448: the REAL Launcher through the reaper's call with the report enabled: a message is attempted and the state written; a raise fails it."""

    def test_real_launcher(self):
        import cloud_worker as cw
        import contextlib
        import io
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        git = os.path.join(tmp, "g")
        subprocess.run(["git", "init", "-q", git], check=True)
        env = {"CW_KUBECTL": "/nonexistent", "CW_CLOUD_AUDIT": os.path.join(tmp, "a.jsonl"), "CW_CLOUD_IMAGE": "r.example.internal/w:1",
               "CW_RUN_RECORD_DIR": os.path.join(tmp, "rec"), "CW_LAND_REPORT_ISSUE": "acme-org/widget-service#1", "CW_LAND_REPORT_GIT_DIR": git}
        launcher = cw.Launcher(cw.Kube(env), env, clock=lambda: NOW)
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        SB = "fl" + "eet-switchboard"   # the executable name, built from two parts (the names rule)
        for prog in (SB, "gh"):   # stubs: each records that it was called
            with open(os.path.join(bindir, prog), "w") as f:
                f.write("#!/bin/sh\necho \"$1\" >> %s/%s.calls\n" % (tmp, prog))
            os.chmod(os.path.join(bindir, prog), 0o755)
        err = io.StringIO()
        old, os.environ["PATH"] = os.environ.get("PATH", ""), bindir + os.pathsep + os.environ.get("PATH", "")
        try:
            with contextlib.redirect_stderr(err):
                real.run(launcher, NOW)
        finally:
            os.environ["PATH"] = old
        self.assertNotIn("failed", err.getvalue())
        with open(os.path.join(tmp, SB + ".calls")) as f:
            self.assertEqual(f.read(), "send\n", "one message attempted")
        self.assertTrue(os.path.exists(os.path.join(tmp, "rec", ".land-report.json")), "state file written")


class RunCall(unittest.TestCase):
    """The reaper's one call, land_report.run(launcher, now), through a fake launcher (a stub recorder.root(), an env)."""

    def check(self, mod):
        tmp = Root(self)
        elsewhere = Root(self)
        tmp.add(sha="a" * 40)
        launcher = FakeLauncher(tmp.dir, {"CW_LAND_REPORT_ISSUE": "o/r#7"})
        empty = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, empty)
        old, os.environ["PATH"] = os.environ.get("PATH", ""), empty   # the senders find no program: the pass still writes its state
        import contextlib
        import io
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                mod.run(launcher, NOW)
        finally:
            os.environ["PATH"] = old
        found = [n for n in os.listdir(tmp.dir) if n.endswith("land-report.json")]
        self.assertTrue(found, "RUN env reached the pass: no state file written")
        self.assertEqual(found, [".land-report.json"], "RUN state path")
        self.assertEqual(os.listdir(elsewhere.dir), [])
        self.assertIn("cloud-worker: land report: the message was not sent", err.getvalue(), "RUN log reaches stderr")

    def check_raise(self, mod):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mod.run(FakeLauncher("/nonexistent", {}, boom=True), NOW)
        self.assertEqual(err.getvalue(), "cloud-worker: the land report failed: OSError in test_land_report.py:root\n", "RUN stderr line")

    def test_control(self):
        self.check(real)
        self.check_raise(real)

    def test_mutant_state_path(self):
        self.check(real)
        m = mutant('os.path.join(root, ".land-report.json")', 'os.path.join(root, "land-report.json")')
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("RUN state path", str(cm.exception))

    def test_mutant_env_dropped(self):
        self.check(real)
        m = mutant("daily_pass(launcher.env, now, root", "daily_pass({}, now, root")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("RUN env reached the pass", str(cm.exception))

    def test_mutant_log_dropped(self):
        self.check(real)
        m = mutant('log=lambda t: print("cloud-worker: " + t, file=sys.stderr))', "log=None)")
        with self.assertRaises(AssertionError) as cm:
            self.check(m)
        self.assertIn("RUN log reaches stderr", str(cm.exception))

    def test_mutant_exception_escapes(self):
        self.check_raise(real)
        m = mutant("    except Exception as err:\n        print(\"cloud-worker: the land report failed", "    except ZeroDivisionError as err:\n        print(\"cloud-worker: the land report failed")
        with self.assertRaises(OSError) as cm:
            self.check_raise(m)
        self.assertEqual(str(cm.exception), "no root", "RUN exception escaped")


class S9(unittest.TestCase):
    """Lanes by boss, rows without a land run skipped, the first-repo setting, the line cap (ask #2448, the first report was unreadable)."""
    def build(self, mod, first=""):
        r = Root(self)
        for i in range(3):   # one boss, three pods: ONE lane (the pod names differ)
            r.add(worker="pod%d-%d" % (i, i), boss="orch-one", repo="acme-org/widget-service", code=0)
        for i in range(2):
            r.add(worker="idle%d-1" % i, boss="orch-one", repo="acme-org/widget-service", land=False)
        r.add(worker="idle9-1", boss="orch-three", repo="acme-org/widget-service", land=False)
        r.add(worker="other-1", boss="orch-two", repo="acme-org/gadget-api", code=18)
        r.add(worker="solo-3", repo="acme-org/gadget-api", code=13, boss="x")
        recs, bad = mod.load_records(r.dir, NOW - dt.timedelta(hours=24), NOW)
        for rec in recs:
            if rec["worker"] == "solo-3":
                rec["lane"] = mod.lane_of("solo-3")
        return mod.build(recs, bad, [commit("1", 5)], 50, (), "2026-11-12", first)[0]

    def check(self, mod):
        out = self.build(mod)
        self.assertIn("acme-org/widget-service / orch-one: attempted 3, landed 3", out, "S9 by boss")
        self.assertEqual(out.count("acme-org/widget-service /"), 1, "S9 one lane per boss")
        self.assertNotIn("idle0", out.split("pods ended unlanded")[0], "S9 no row for a pod without a land run")
        self.assertIn("3 pods with no land run skipped", out, "S9 skipped line")
        self.assertNotIn("orch-three", out, "S9 no row for a lane with attempted 0")
        self.assertIn("solo (lane unknown)", out, "S9 fallback names the prefix")
        self.assertLess(out.index("widget-service"), out.index("gadget-api"), "S9 most attempts first")
        first = self.build(mod, "acme-org/gadget-api")
        self.assertLess(first.index("gadget-api"), first.index("widget-service"), "S9 first repo")

    def many(self, mod):
        r = Root(self)
        for i in range(40):
            r.add(worker="p-%d" % i, boss="orch%02d" % i, repo="acme-org/widget-service", code=0, tail="x" * 50)
        return report(mod, r, landed=[commit("1", 5)])

    def check_cap(self, mod):
        out = self.many(mod)
        self.assertLess(len(out.splitlines()), 20, "S9 line cap")
        self.assertLessEqual(len(out), 3500, "S9 char cap")
        self.assertRegex(out, r"\d+ more lanes: attempted \d+, landed \d+", "S9 more lanes")
        self.assertIn("pods ended unlanded", out)

    def test_control(self):
        self.check(real)
        self.check_cap(real)

    def test_mutant_group_by_pod(self):
        mod = mutant('"lane": lane_of(rec.get("worker"), rec.get("boss"))', '"lane": name_text(rec.get("worker"), 64)')
        self.check(real)
        with self.assertRaises(AssertionError) as cm:
            self.check(mod)
        self.assertIn("S9 by boss", str(cm.exception))

    def test_mutant_prints_rows_without_land_run(self):
        mod = mutant("for k, rs in lanes.items() if any(r[\"land\"] for r in rs))", "for k, rs in lanes.items())")
        self.check(real)
        with self.assertRaises(AssertionError) as cm:
            self.check(mod)
        self.assertIn("S9", str(cm.exception))

    def test_mutant_first_repo_ignored(self):
        mod = mutant("(kv[0][0] != first_repo, ", "(False, ")
        self.check(real)
        with self.assertRaises(AssertionError) as cm:
            self.check(mod)
        self.assertIn("S9 first repo", str(cm.exception))

    def test_mutant_no_cap(self):
        mod = mutant("fit = len(rows) if 2 * len(rows) <= room else max(0, (room - 1) // 2)", "fit = len(rows)")
        self.check_cap(real)
        with self.assertRaises(AssertionError) as cm:
            self.check_cap(mod)
        self.assertIn("S9 line cap", str(cm.exception))

    def test_mutant_fallback_not_marked(self):
        mod = mutant('+ " (lane unknown)"', '+ ""')
        self.check(real)
        with self.assertRaises(AssertionError) as cm:
            self.check(mod)
        self.assertIn("S9 fallback", str(cm.exception))

    def test_setting_validated(self):
        self.assertEqual(real.first_repo("acme-org/widget-service"), "acme-org/widget-service")
        self.assertEqual(real.first_repo("bad name"), "")
        self.assertEqual(real.first_repo(None), "")


def reaper_module_name(src):
    """The module name the reaper loads by name: found by ast as the constant in `__import__("<name>").run(...)` (the call site, not a copied string)."""
    import ast
    names = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run" and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name) and node.func.value.func.id == "__import__"
                and node.func.value.args and isinstance(node.func.value.args[0], ast.Constant) and "now" in [getattr(a, "id", None) for a in node.args]):
            names.append(node.func.value.args[0].value)
    assert len(names) == 1, names
    return names[0]


class ReaperLoad(unittest.TestCase):
    def check(self, src):
        name = reaper_module_name(src)
        spec = importlib.util.find_spec(name)
        self.assertIsNotNone(spec, "REAPER load %s" % name)
        self.assertEqual(os.path.realpath(spec.origin), os.path.realpath(SOURCE), "REAPER load target")
        module = importlib.util.module_from_spec(spec)   # loaded from the spec found above: no import by a name read at run time
        spec.loader.exec_module(module)
        self.assertTrue(callable(getattr(module, "run", None)), "REAPER run")

    def test_control(self):
        with open(WORKER) as f:
            self.check(f.read())

    def test_mutant_renamed(self):
        with open(WORKER) as f:
            src = f.read()
        self.check(src)
        old = '__import__("land_report").run('
        self.assertEqual(src.count(old), 1)
        with self.assertRaises(AssertionError) as cm:
            self.check(src.replace(old, '__import__("land_reports").run('))
        self.assertIn("REAPER load land_reports", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

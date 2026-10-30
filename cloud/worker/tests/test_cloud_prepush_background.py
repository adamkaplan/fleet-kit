#!/usr/bin/env python3
"""Scenarios S2-S10 of ask #1616: the background run of bin/cloud-prepush and its result file, on throwaway repositories."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stub_support  # noqa: E402
import test_cloud_prepush as tp  # noqa: E402

ROOT = tp.ROOT
SCRIPT = os.environ.get("PREPUSH_UNDER_TEST") or tp.SCRIPT
UNDER_MUTANT = bool(os.environ.get("PREPUSH_UNDER_TEST"))
TD = tp.TD
RESULTS = {}


def tally(key):
    RESULTS[key] = RESULTS.get(key, 0) + 1


def tests3(sleep_seconds):
    """3 files that read a.txt: a passing one, a slow one (passes), one that fails on the branch only."""
    return {
        TD + "test_a_pass.py": "# a.txt\n" + tp.PASS,
        TD + "test_b_slow.py": "# a.txt\nimport time, sys\ntime.sleep(%s)\nsys.exit(0)\n" % sleep_seconds,
        TD + "test_c_fail.py": "# a.txt\nimport sys\nsys.exit(1 if 'NEW' in open('a.txt').read() else 0)\n",
    }


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        pass
    return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-bg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def repo(self, sleep_seconds=3):
        return tp.make_repo(self.tmp, tests3(sleep_seconds))

    def install(self, script=None):
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        shutil.copy(script or SCRIPT, os.path.join(d, "cloud-prepush"))
        with open(os.path.join(d, "cloud-pr-preflight"), "w") as f:
            f.write("import sys\nsys.exit(0)\n")
        return os.path.join(d, "cloud-prepush")

    def start_background(self, repo, env=None, script=None):
        """Start the detached run through stub_support.start; returns (starter, seconds, result dir, installed script)."""
        installed = self.install(script)
        result = os.path.join(self.tmp, "result")
        e = dict(tp.ENV, CLOUD_PREPUSH_RESULT=result, **(env or {}))
        t0 = time.monotonic()
        p = stub_support.start(self, [sys.executable, installed, "--background", "trunk"], cwd=repo, env=e,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(timeout=60)
        took = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, err)
        self.addCleanup(self.end_run, result)
        return p, took, result, installed, out

    def end_run(self, result):
        try:
            pid = json.load(open(os.path.join(result, "result.json")))["pid"]
            pid = pid or 0
        except (OSError, ValueError, KeyError):
            return
        if pid > 0 and alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(100):
                if not alive(pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)

    def doc(self, result):
        return json.load(open(os.path.join(result, "result.json")))

    def wait(self, result, cond, limit=90):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                d = self.doc(result)
            except (OSError, ValueError):
                d = None
            if d and cond(d):
                return d
            time.sleep(0.2)
        self.fail("the condition did not hold in %d s" % limit)

    def read(self, installed, result, cwd):
        return subprocess.run([sys.executable, installed, "--read", result], cwd=cwd, capture_output=True, text=True, env=tp.ENV)

    def finished(self, result):
        return self.wait(result, lambda d: d["status"] == "finished")


@unittest.skipIf(UNDER_MUTANT, "run on the script under test only")
class Foreground(Base):
    def test_s2_the_foreground_run_cannot_finish_a_file_longer_than_its_bound(self):
        repo = self.repo(3)
        p = tp.run(repo, env={"CLOUD_PREPUSH_TIMEOUT": "2"})
        lines = [l for l in p.stdout.splitlines() if l.startswith("COULD NOT RUN")]
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("timed out after", lines[0])
        self.assertEqual(p.returncode, 1)
        print("S2: COULD NOT RUN files %d (timed out after), exit %d" % (len(lines), p.returncode), file=sys.stderr)


class Background(Base):
    def test_s3_s4_start_returns_at_once_and_the_run_finishes_after_the_starter_and_its_group_end(self):
        repo = self.repo(3)
        p, took, result, installed, out = self.start_background(repo, {"CLOUD_PREPUSH_TIMEOUT": "20"})
        path_printed = sum(1 for l in out.splitlines() if l.endswith("result.json") and os.path.join(result, "") in l)
        pid = self.wait(result, lambda d: d["pid"] > 0)["pid"]
        was_alive = alive(pid)
        self.assertLess(took, 5)
        self.assertEqual(path_printed, 1, out)
        self.assertTrue(was_alive)
        print("S3: background start exit %d in %.1f seconds, result path printed %d, run alive %d"
              % (p.returncode, took, path_printed, int(was_alive)), file=sys.stderr)
        try:  # the starting command's whole group ends now: the run must go on
            os.killpg(p.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        d = self.wait(result, lambda d: d["status"] == "finished", 40)
        done = [f for f in d["files"] if f["branch"] != "not started" and f["base"] != "not started" and f["branch_seconds"] is not None]
        self.assertEqual(len(done), 3, d)
        tally("finished")
        tally("files3") if len(done) == 3 else None
        print("S4: run finished after the starting process ended %d, files in the result %d of 3"
              % (RESULTS.get("finished", 0), len(done)), file=sys.stderr)

    @unittest.skipIf(UNDER_MUTANT, "S5 compares two runs")
    def test_s5_a_file_longer_than_the_foreground_bound_passes_in_the_background_run(self):
        repo = self.repo(3)
        fg = tp.run(repo, env={"CLOUD_PREPUSH_TIMEOUT": "2"})
        nofg = sum(1 for l in fg.stdout.splitlines() if l.startswith("COULD NOT RUN") and "test_b_slow" in l)
        p, took, result, installed, out = self.start_background(repo, {"CLOUD_PREPUSH_TIMEOUT": "20"})
        d = self.finished(result)
        slow = [f for f in d["files"] if f["file"].endswith("test_b_slow.py")][0]
        self.assertEqual((slow["branch"], slow["base"]), ("pass", "pass"))
        self.assertGreaterEqual(slow["branch_seconds"], 2.5)
        print("S5: background passed 1, foreground could not run %d" % nofg, file=sys.stderr)
        self.assertEqual(nofg, 1)

    def test_s6_a_failing_file_is_named_with_its_result(self):
        repo = self.repo(1)
        p, took, result, installed, out = self.start_background(repo, {"CLOUD_PREPUSH_TIMEOUT": "20"})
        d = self.finished(result)
        fail = [f for f in d["files"] if f["file"].endswith("test_c_fail.py")]
        self.assertEqual([(f["branch"], f["base"]) for f in fail], [("fail", "pass")])
        r = self.read(installed, result, repo)
        named = [l for l in r.stdout.splitlines() if l.startswith("FAILS on the branch, passes on the base:") and "test_c_fail" in l]
        print("S6: failing file named %d, read exit %d" % (len(named), r.returncode), file=sys.stderr)
        self.assertEqual((len(named), r.returncode), (1, 1), r.stdout)
        self.assertIn("cloud-prepush: run 3, identical: 2", r.stdout)

    def test_s7_a_run_in_progress_is_reported_as_running_and_never_as_passed(self):
        repo = self.repo(6)
        p, took, result, installed, out = self.start_background(repo, {"CLOUD_PREPUSH_TIMEOUT": "30"})
        d = self.wait(result, lambda d: any(f["file"].endswith("test_b_slow.py") and "running" in (f["branch"], f["base"]) for f in d["files"]), 25)
        r = self.read(installed, result, repo)
        running = [l for l in r.stdout.splitlines() if l.startswith("RUNNING ") and "test_b_slow" in l]
        d = self.doc(result)
        unfinished_passed = [f for f in d["files"] if f["file"].endswith(("test_b_slow.py", "test_c_fail.py"))
                             and (f["branch"] == "pass" and f["base"] == "pass" and f["kind"] is None)]
        unfinished_passed += [f for f in d["files"] if f["kind"] is None and "pass" in (f["branch"], f["base"]) and f["branch"] != f["base"] == "pass"]
        print("S7: running lines %d, unfinished files reported passed %d, read exit %d"
              % (len(running), len(unfinished_passed), r.returncode), file=sys.stderr)
        self.assertEqual(len(running), 1, r.stdout)
        self.assertEqual(r.returncode, 4)
        self.assertIn("still running", r.stdout)
        self.assertNotIn("test_c_fail.py: pass", r.stdout)
        self.assertEqual(unfinished_passed, [])
        notstarted = [f for f in d["files"] if f["file"].endswith("test_c_fail.py")][0]
        self.assertEqual((notstarted["branch"], notstarted["base"]), ("not started", "not started"))

    def test_read_of_a_missing_file_exits_2(self):
        inst = self.install()
        self.assertEqual(self.read(inst, os.path.join(self.tmp, "nothing"), self.tmp).returncode, 2)
        bad = os.path.join(self.tmp, "bad.json")
        open(bad, "w").write("{not json")
        self.assertEqual(self.read(inst, bad, self.tmp).returncode, 2)


@unittest.skipIf(UNDER_MUTANT, "run on the script under test only")
class Mutants(Base):
    def run_mutant(self, name, old, new, test):
        s = open(tp.SCRIPT).read()
        self.assertEqual(s.count(old), 1, "mutation not applied: " + name)
        scratch = tempfile.mkdtemp(prefix="prepush-mutant-")  # outside the repository
        self.addCleanup(shutil.rmtree, scratch, True)
        path = os.path.join(scratch, "cloud-prepush")
        open(path, "w").write(s.replace(old, new, 1))
        self.assertNotEqual(open(path).read(), s)
        p = stub_support.start(self, [sys.executable, os.path.abspath(__file__), test],
                               env=dict(os.environ, PREPUSH_UNDER_TEST=path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=170)
        return p.returncode

    def test_s8_the_mutants_fail_their_scenario(self):
        failing = applied = 0
        for name, old, new, test in (
            ("running reported as passed", 'IN_PROGRESS = "running"', 'IN_PROGRESS = "pass"',
             "Background.test_s7_a_run_in_progress_is_reported_as_running_and_never_as_passed"),
            ("run kept in the starter's group", "stdin=nul, stdout=out, stderr=out, start_new_session=True, close_fds=True)",
             "stdin=nul, stdout=out, stderr=out, close_fds=True)",
             "Background.test_s3_s4_start_returns_at_once_and_the_run_finishes_after_the_starter_and_its_group_end"),
        ):
            rc = self.run_mutant(name, old, new, test)
            applied += 1
            failing += 1 if rc == 1 else 0
        print("S8: %d of 2 mutants fail, %d of 2 mutations applied" % (failing, applied), file=sys.stderr)
        self.assertEqual((failing, applied), (2, 2))


@unittest.skipIf(UNDER_MUTANT, "run on the script under test only")
class Reads(Base):
    def test_s9_every_read_of_the_result_file_parses_while_it_is_rewritten(self):
        loader = importlib.machinery.SourceFileLoader("prepush_module", tp.SCRIPT)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("prepush_module", loader))
        loader.exec_module(mod)
        rec = mod.Recorder(self.tmp)
        rec.select(["f%d" % i for i in range(200)])
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                rec.set("f%d" % (i % 200), branch="pass", text="x" * 2000)
                i += 1

        t = threading.Thread(target=writer)
        t.start()
        ok = 0
        try:
            for _ in range(100):
                try:
                    d = json.load(open(os.path.join(self.tmp, "result.json")))
                    ok += 1 if d["status"] == "running" and len(d["files"]) == 200 else 0
                except ValueError:
                    pass
        finally:
            stop.set()
            t.join()
        print("S9: reads that parse %d of 100" % ok, file=sys.stderr)
        self.assertEqual(ok, 100)


@unittest.skipIf(UNDER_MUTANT, "run on the script under test only")
class Documents(unittest.TestCase):
    def test_s10_the_documents_say_how_to_start_read_and_poll(self):
        readme = open(os.path.join(ROOT, "cloud", "README.md")).read()
        worker = open(os.path.join(ROOT, "cloud", "worker", "agents", "worker.md")).read()
        found = 0
        for ok in (
            "bin/cloud-prepush --background <base>" in readme and "bin/cloud-prepush --background <base>" in worker,
            "bin/cloud-prepush --read FILE" in readme and "exit status is not 4" in worker,
            "does not count as activity for the idle limit" in readme and "does not count as activity for the idle limit" in worker,
        ):
            found += 1 if ok else 0
        print("S10: %d of 3 sentences found" % found, file=sys.stderr)
        self.assertEqual(found, 3)


if __name__ == "__main__":
    unittest.main()

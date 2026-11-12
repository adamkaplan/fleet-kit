"""Ask #2432: the end hook's answer carries the pod's run files (`runs`, `wake_lines`) and the run record keeps them as `run_logs`.

A fake pod directory (a temporary directory standing for /work/runs and the progress file). The mutant is a scratch copy of the end hook without
the read (the text asserted once, the replacement computed, a no-op failing); its control (the unmutated hook) runs first."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"
ANCHOR = "the run files were not saved"


def load(path):
    spec = importlib.util.spec_from_file_location("end_hook_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mutant_copy(directory):
    """The real hook whose `run_files` returns nothing: the run files are not saved."""
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        src = f.read()
    old = "def run_files(directory=None, progress=None):\n"
    assert src.count(old) == 1
    new = src.replace(old, old + "    return {\"runs\": [], \"wake_lines\": []}\n")
    assert new != src
    path = os.path.join(directory, "end_hook.py")
    with open(path, "w") as f:
        f.write(new)
    return path


class Fake(unittest.TestCase):
    hook_path = os.path.join(LAUNCHER, "end_hook.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = os.path.join(self.tmp.name, "runs")
        self.progress = os.path.join(self.tmp.name, "progress.txt")
        os.makedirs(self.runs)
        self.h = load(self.hook_path)
        self.h.RUNS_DIR, self.h.PROGRESS_FILE = self.runs, self.progress

    def put(self, name, text, age=0):
        p = os.path.join(self.runs, name)
        with open(p, "w") as f:
            f.write(text)
        t = time.time() - age
        os.utime(p, (t, t))

    def answer(self):
        return self.h.with_runs({"pushed": False})

    def three_runs(self):
        self.put("a.log", "one\ntwo\n", age=30)
        self.put("a.exit", "13\n", age=30)
        self.put("a.woke", "", age=30)
        self.put("b.log", "still going\n", age=20)
        self.put("b.pid", "123\n", age=20)
        self.put("c.log", "x" * 5000 + "\n", age=10)
        self.put("c.exit", "0\n", age=10)

    def test_s2_three_runs(self):
        self.three_runs()
        runs = {r["name"]: r for r in self.answer()["runs"]}
        self.assertEqual(len(runs), 3, ANCHOR)
        self.assertEqual((runs["a"]["exit"], runs["a"]["woke"]), (13, True), ANCHOR)
        self.assertEqual(runs["a"]["log_tail"], "one\ntwo")
        self.assertIsNone(runs["b"]["exit"])
        self.assertFalse(runs["b"]["woke"])
        self.assertEqual(runs["c"]["exit"], 0)
        for r in runs.values():
            self.assertLessEqual(len(r["log_tail"]), 3000)
        self.assertEqual([r["name"] for r in self.answer()["runs"]], ["c", "b", "a"])   # newest first

    def test_s3_token_redacted_and_twenty_of_25(self):
        self.put("tok.log", "start\nGITHUB_TOKEN=%s\nusing %s here\n" % (TOKEN, TOKEN), age=1)
        for i in range(25):
            self.put("r%02d.log" % i, "line %d\n" % i, age=100 + i)
        ans = self.answer()
        self.assertNotIn(TOKEN, json.dumps(ans))
        self.assertIn("[redacted]", ans["runs"][0]["log_tail"])
        self.assertEqual(len(ans["runs"]), 20)
        self.assertEqual(ans["runs"][0]["name"], "tok")
        self.assertEqual(ans["runs"][1]["name"], "r00")

    def test_s3_whole_addition_is_capped(self):
        for i in range(20):
            self.put("big%02d.log" % i, "".join("line %d é\n" % j for j in range(400)), age=i)
        self.assertLessEqual(len(json.dumps(self.h.run_files())), 20000)

    def test_s4_wake_lines_capped_and_missing_directory(self):
        lines = ["run job%d exited %d after 3s; last 20 lines:" % (i, i) for i in range(12)]
        lines += ["cw-run noise exited 0 after 1s", "other", "wake not delivered", "run x exited 1 " + "y" * 400 + " " + TOKEN]
        with open(self.progress, "w") as f:
            f.write("\n".join(lines) + "\n")
        got = self.answer()["wake_lines"]
        self.assertEqual(len(got), 10)
        self.assertIn("wake not delivered", got)
        self.assertEqual(got[0], "run job4 exited 4 after 3s; last 20 lines:")
        self.assertTrue(all(len(x) <= 200 for x in got))
        self.assertNotIn(TOKEN, json.dumps(got))
        self.h.RUNS_DIR, self.h.PROGRESS_FILE = os.path.join(self.tmp.name, "nowhere"), os.path.join(self.tmp.name, "none")
        self.assertEqual(self.answer(), {"pushed": False, "runs": [], "wake_lines": []})

    def test_s5_slash_link_and_long_names_are_not_read(self):
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "w") as f:
            f.write("TARGET-SECRET-CONTENT\n")
        os.symlink(outside, os.path.join(self.runs, "lnk.log"))
        os.symlink(outside, os.path.join(self.runs, "lnk2.exit"))
        self.put("x" * 65 + ".log", "LONGNAME\n")
        self.put("ok.log", "fine\n")
        ans = self.answer()
        self.assertEqual([r["name"] for r in ans["runs"]], ["ok"])
        self.assertNotIn("TARGET-SECRET-CONTENT", json.dumps(ans))
        self.assertNotIn("LONGNAME", json.dumps(ans))
        self.assertFalse(self.h.RUN_NAME_RE.fullmatch("a/b"))
        self.assertFalse(self.h.RUN_NAME_RE.fullmatch("x" * 65))

    def test_a_secret_split_by_the_read_window_is_not_shown(self):
        window = self.h.READ_TAIL_BYTES
        head = "word=hunter2hunter2 tail\n"
        tail_end = "last line\n"
        fill = window - len(head) - len(tail_end)   # four long lines
        lines = ["y" * (fill // 4 - 1) + "\n"] * 3
        lines.append("y" * (fill - 3 * len(lines[0]) - 1) + "\n")
        with open(os.path.join(self.runs, "w.log"), "w") as f:
            f.write("A" * 500 + "pass" + head + "".join(lines) + tail_end)   # the window begins right after `pass`
        self.assertEqual(os.path.getsize(os.path.join(self.runs, "w.log")), 504 + window)
        tail = self.answer()["runs"][0]["log_tail"]
        self.assertNotIn("hunter2", tail)
        self.assertIn("last line", tail)

    def test_oversized_and_unreadable_never_raise(self):
        self.put("odd.log", "text\n")
        os.mkdir(os.path.join(self.runs, "dir.log"))
        self.h.MAX_RUN_FILE = 2
        ans = self.answer()
        self.assertEqual([r["name"] for r in ans["runs"]], ["odd"])
        self.assertEqual(ans["runs"][0]["log_tail"], "")


class Mutant(unittest.TestCase):
    def check(self, hook_path):
        with tempfile.TemporaryDirectory() as t:
            runs = os.path.join(t, "runs")
            os.makedirs(runs)
            with open(os.path.join(runs, "a.log"), "w") as f:
                f.write("one\n")
            with open(os.path.join(runs, "a.exit"), "w") as f:
                f.write("13\n")
            h = load(hook_path)
            h.RUNS_DIR, h.PROGRESS_FILE = runs, os.path.join(t, "p")
            ans = h.with_runs({})
            self.assertEqual([(r["name"], r["exit"]) for r in ans["runs"]], [("a", 13)], ANCHOR)

    def test_s7_control_then_mutant(self):
        self.check(os.path.join(LAUNCHER, "end_hook.py"))   # the control: the unmutated hook passes
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(AssertionError) as cm:
                self.check(mutant_copy(t))
            self.assertIn(ANCHOR, str(cm.exception))


class Record(unittest.TestCase):
    """S6: a fake pod end through the launcher's stop keeps `run_logs` in the run record; a failing store changes nothing else."""
    def setUp(self):
        import test_stop_no_checkout as t
        self.t = t
        self.case = t.StopNoCheckout("stop_pod")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    ANSWER = {"pushed": True, "wip_branch": "coder1/wip",
              "runs": [{"name": "job", "exit": 13, "woke": True, "log_tail": "line1\nline2"},
                       {"name": "bad/name", "exit": 1, "woke": False, "log_tail": "x"}],
              "wake_lines": ["run job exited 13 after 3s; last 20 lines:"]}

    def test_s6_run_logs_in_the_record(self):
        code, err, record = self.case.stop_pod("coder1", hook_result=self.ANSWER)
        self.assertEqual(code, 0, err)
        self.assertEqual(record["end_hook"]["run_logs"],
                         {"runs": [{"name": "job", "exit": 13, "woke": True, "log_tail": "line1\nline2"}],
                          "wake_lines": ["run job exited 13 after 3s; last 20 lines:"]})
        self.assertEqual(record["end_hook"]["wip_branch"], "coder1/wip")

    def test_s6_a_failing_store_leaves_the_stop_unchanged(self):
        code, err, plain = self.case.stop_pod("coder2", hook_result={"pushed": True, "wip_branch": "coder2/wip"})
        cw = self.t.cw

        real = cw.scrub

        def failing(text, known=(), limit=300):   # only the run log tail is scrubbed with this limit
            if limit == 3000:
                raise RuntimeError("boom")
            return real(text, known, limit)
        cw.scrub = failing
        self.addCleanup(setattr, cw, "scrub", real)
        code2, err2, rec2 = self.case.stop_pod("coder3", hook_result=dict(self.ANSWER, wip_branch="coder3/wip"))
        self.assertEqual(code2, 0, err2)
        self.assertIsNone(self.case.pod("coder3"))
        self.assertEqual(rec2["end_hook"]["wip_branch"], "coder3/wip")
        self.assertEqual(rec2["end_hook"]["run_logs"], {"runs": [], "wake_lines": []})
        self.assertEqual({k: v for k, v in rec2["end_hook"].items() if k != "run_logs"}.keys(), {k: v for k, v in plain["end_hook"].items() if k != "run_logs"}.keys())


if __name__ == "__main__":
    unittest.main()

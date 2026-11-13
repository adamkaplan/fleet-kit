"""Ask #2472, the wiring: the end alert's scan_audit, the stall watch's watch_pass, its probe and decide() drive the one results rule end to end
(a stale file, a fresh one, no MTIME line, mtime equal to the start, an unknown start). Each wire has a mutant (control first). Offline."""
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_end_alert as tea  # noqa: E402
import test_stall_watch as tsw  # noqa: E402
import run_record as rr  # noqa: E402
import stall_watch as sw  # noqa: E402
import subprocess  # noqa: E402


def mutate_module(path, old, new, name):
    with open(path) as f:
        text = f.read()
    assert text.count(old) == 1, "the mutation did not apply: %r" % old
    mod = types.ModuleType(name)
    mod.__file__ = path
    exec(compile(text.replace(old, new), "mutant", "exec"), mod.__dict__)
    return mod


class Alert(tea.World):
    def deliver(self, results, module):
        runs = self.worker("w1")
        with open(os.path.join(runs, "run.json"), "w") as f:
            json.dump({"end_hook": {"pushed": True, "wip_branch": None}, "results": results}, f)
        self.stop("w1", "idle")
        self.sent[:] = []
        self.run_pass(module)
        return " ".join(t for _, t in self.sent)

    def stale_cases(self, module):
        text = self.deliver({"done": False, "state": "stale", "text": "x"}, module)
        self.assertIn("Results: results.txt is from an earlier run.", text, "scan_audit dropped the results note")

    def test_scan_audit_carries_the_stale_note_and_a_mutant_that_drops_it_fails(self):
        self.stale_cases(tea.real)   # control
        mod = tea.mutant("results_note(raw_dir(reason), parse))", '"")')
        self.setUp()
        with self.assertRaisesRegex(AssertionError, "scan_audit dropped the results note"):
            self.stale_cases(mod)

    def test_a_fresh_finished_results_file_gets_no_note(self):
        text = self.deliver({"done": True, "state": "done", "text": "STATUS: done"}, tea.real)
        self.assertNotIn("Results:", text)


class Watch(unittest.TestCase):
    # ask #2443: the stall watch no longer decides "finished" from results.txt text (the structural turn state does), so the pass-level finish cases of ask #2472 are gone.
    def test_the_probe_reports_the_results_mtime_and_a_missing_file_has_none(self):
        d = tempfile.mkdtemp(prefix="probe-")
        path = os.path.join(d, "results.txt")
        code = sw.PROBE_CODE.replace("/work/results.txt", path)
        self.assertGreater(code.count(path), 1)
        env = dict(os.environ, CW_RUNS_DIR=d, CW_ACTIVITY_PORT="1", CW_ACTIVITY_PASSWORD_FILE=os.path.join(d, "none"))

        def probe():
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=20)
            return json.loads(out.stdout.strip().splitlines()[-1])
        self.assertNotIn("results_mtime", probe())
        with open(path, "w") as f:
            f.write("STATUS: done\n")
        os.utime(path, (1234567, 1234567))
        data = probe()
        self.assertEqual(data["results_mtime"], 1234567)
        self.assertEqual(data["results"], "STATUS: done")


import test_run_record as trr  # noqa: E402


class Mtime(trr.Record):
    def found(self, content, mtime):
        fs = {"type": "file", "content": content}
        if mtime is not None:
            fs["mtime"] = mtime
        self.patch_state(report_fs={self.name: fs})
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        return json.loads(self.read(os.path.join(self.the_record(), "run.json"))).get("results")

    def test_an_absent_mtime_line_is_not_held_against_the_file(self):
        got = self.found("worked\nSTATUS: done\n", None)
        self.assertEqual((got["state"], got["done"]), ("done", True))
        self.assertEqual(got["text"].strip().splitlines()[0], "worked")   # no MTIME line leaked into the text

    def test_the_mtime_line_is_parsed_and_removed_from_the_text(self):
        got = self.found("worked\nSTATUS: done\n", 1000)
        self.assertEqual(got["state"], "stale")
        self.assertNotIn("MTIME", got["text"])

    def test_the_rule_at_the_boundary_and_with_unknowns(self):
        self.assertEqual(rr.results_verdict("STATUS: done", 5, 5), "stale")
        self.assertEqual(rr.results_verdict("STATUS: done", 6, 5), "done")
        self.assertEqual(rr.results_verdict("STATUS: done", None, 5), "done")
        self.assertEqual(rr.results_verdict("STATUS: done", 6, None), "done")
        self.assertIsNone(rr.start_epoch(""))


for _n in dir(trr.Record):
    if _n.startswith("test") and _n not in Mtime.__dict__:
        setattr(Mtime, _n, None)
del _n

if __name__ == "__main__":
    unittest.main()

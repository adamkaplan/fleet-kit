"""Ask #1855, constraint PC-3: the audit writes the seconds (S8, S9, S10, S12).

A stop line holds `steps` (record, hook, refresh, delete, scrub), a renewal line and a refresh line hold `seconds`. The cluster is the fake
kubectl of test_cloud_worker, the clock is a fake clock that the patched sub-steps advance (no real clock is read, no assertion is on real
time). Each scenario has a control (the real launcher, first) and a mutant (a scratch copy of the module text, the text asserted to occur once).
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402

cw = tcw.cw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
NAMES = ("record", "hook", "refresh", "delete", "scrub", "other")
SECRETS = ["s" + hashlib.sha256(b"step-%d" % i).hexdigest()[:30] for i in range(3)]   # synthetic, assembled at run time
LINES = []   # the S lines printed at the end


def load_cost_report():
    spec = importlib.util.spec_from_file_location("cloud_cost_report_s10", os.path.join(ROOT, "bin", "cloud_cost_report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mutant_module(*pairs):
    """The launcher text with each (old, new) applied; each `old` asserted to occur ONCE (the mutation asserted applied)."""
    with open(cw.__file__) as f:
        text = f.read()
    for old, new in pairs:
        assert text.count(old) == 1, "the mutation did not apply: %r" % old[:60]
        text = text.replace(old, new)
    mod = types.ModuleType("cw_mutant_steps")
    mod.__file__ = cw.__file__
    sys.modules["cw_mutant_steps"] = mod
    exec(compile(text, "cw_mutant_steps", "exec"), mod.__dict__)
    return mod


class Steps(tcw.Base):
    def setUp(self):
        super().setUp()
        self.create()
        self.pod0 = self.pod()   # the pod as the renewal reads it (a stop removes it from the fake cluster)

    def cli(self, mod, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(list(argv), self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        return code

    def patched(self, mod, hook, refresh_seconds=0, outside=0):
        """The fake sub-steps on the fake clock: record 2, hook 30 per run, delete 3, the stop's own scrub 1.5, refresh as given, and `outside` seconds
        inside no step (the mark of the pod)."""
        clock = self.clock
        L, R = mod.Launcher, mod.run_record.Recorder
        answers = list(hook)

        def record(rec, *a, **k):
            clock.advance(seconds=2)
            return ""

        real_view, real_mark = L._hook_view, L._mark

        def hook_view(launcher, res):
            clock.advance(seconds=1.5)
            return real_view(launcher, res)

        def mark(launcher, *a, **k):
            clock.advance(seconds=outside)
            return real_mark(launcher, *a, **k)

        def run_hook(launcher, name):
            clock.advance(seconds=30)
            return answers.pop(0)

        real_remove = L._remove

        def remove(launcher, *a, **k):
            clock.advance(seconds=3)
            return real_remove(launcher, *a, **k)

        def refresh(launcher, pod, now):
            clock.advance(seconds=refresh_seconds)
            return mod.REFRESH_USED

        stack = [mock.patch.object(L, "_hook_view", hook_view), mock.patch.object(L, "_mark", mark), mock.patch.object(R, "record", record), mock.patch.object(L, "run_hook", run_hook),
                 mock.patch.object(L, "_remove", remove), mock.patch.object(L, "refresh_for_stop", refresh)]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def stop_line(self, mod, hook, **kw):   # kw: refresh_seconds, outside
        self.patched(mod, hook, **kw)
        code = self.cli(mod, "stop", "coder7", "--reason", "retire")
        stops = [l for l in self.audit_lines() if l["action"] == "stop"]
        return code, stops[-1]

    # ---- S8
    def check_s8(self, mod):
        code, line = self.stop_line(mod, [{"pushed": False, "wip_branch": None}])
        if "steps" not in line:
            raise AssertionError("a stop wrote no step seconds")
        self.assertEqual((code, line["ok"]), (0, True))
        self.assertEqual(line["steps"], {"record": 2.0, "hook": 30.0, "delete": 3.0, "scrub": 1.5, "other": 0.0})   # no refresh key, no key besides the names and other
        return line["steps"]

    def test_s8_control(self):
        steps = self.check_s8(cw)
        LINES.append("S8: steps %.1f/%.1f/%.1f/%.1f, refresh key absent %s" % (steps["record"], steps["hook"], steps["delete"], steps["scrub"], "refresh" not in steps))

    def test_s8_a_stop_that_refreshes_writes_five_steps(self):
        code, line = self.stop_line(cw, [{"pushed": False, "wip_branch": None, "error": "rejected", "refresh": "needed"},
                                         {"pushed": False, "wip_branch": None}], refresh_seconds=4)
        self.assertEqual((code, line["ok"]), (0, True))
        self.assertEqual(line["steps"], {"record": 2.0, "hook": 60.0, "refresh": 4.0, "delete": 3.0, "scrub": 1.5, "other": 0.0})
        LINES.append("S8: refreshing stop %d steps (and other)" % (len(line["steps"]) - 1))

    def test_s8_a_stop_that_fails_in_the_hook_writes_the_steps_it_reached(self):
        code, line = self.stop_line(cw, [{"pushed": False, "wip_branch": None, "error": "rejected"}])
        self.assertNotEqual(code, 0)
        self.assertIs(line["ok"], False)
        self.assertEqual(line["steps"], {"record": 2.0, "hook": 30.0, "scrub": 1.5, "other": 0.0})
        LINES.append("S8: failing stop reached steps %s" % "/".join(sorted(line["steps"])))

    def test_s8_mutant(self):
        mod = mutant_module(('            line["steps"] = clean\n', "            pass\n"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s8(mod)
        self.assertEqual(str(cm.exception), "a stop wrote no step seconds")
        LINES.append("S8: control 1 of 1, mutant 1 of 1 with the text, mutation applied True")

    # ---- S9
    def renewal_lines(self, mod, fail=False):
        clock = self.clock
        L = mod.Launcher
        pod = self.pod0

        def renew(launcher, pod, now, force=False):
            clock.advance(seconds=114 if not fail else 7)
            if fail:
                raise mod._NotRenewed("the mint refused")
            return True
        with mock.patch.object(L, "_renew_git_token", renew):
            mod.Launcher(mod.Kube(self.env), self.env, clock).renew_git_tokens([pod], clock())
        return [l for l in self.audit_lines() if l["action"] == "renew-git-token"]

    def refresh_line(self, mod):
        clock = self.clock
        token_file = os.path.join(self.tmp.name, "token")
        env = {"CW_CLOUD_TOKEN_FILE": token_file, "CW_CLOUD_AUDIT": self.env["CW_CLOUD_AUDIT"]}

        def cluster_token(*a, **k):
            clock.advance(seconds=333)
            return "t" * 20
        with mock.patch.object(mod, "cluster_token", cluster_token), mock.patch.object(mod, "write_token_file", lambda *a, **k: None):
            result = mod.ClusterTokenRefresher(env, clock).run(force=True)
        self.assertEqual(result.state, "written")
        return [l for l in self.audit_lines() if l["action"] == "refresh-cluster-token"][-1]

    def check_s9(self, mod):
        lines = self.renewal_lines(mod)
        if not lines or "seconds" not in lines[-1]:
            raise AssertionError("a renewal wrote no seconds")
        self.assertEqual(lines[-1]["seconds"], 114.0)
        return lines[-1]["seconds"]

    def test_s9_control(self):
        renewal = self.check_s9(cw)
        refresh = self.refresh_line(cw)
        self.assertEqual(refresh["seconds"], 333.0)
        failed = self.renewal_lines(cw, fail=True)[-1]
        self.assertIs(failed["ok"], False)
        self.assertEqual(failed["seconds"], 7.0)
        LINES.append("S9: renewal %.1f, refresh %.1f (2 of 2); failed renewal seconds kept %s" % (renewal, refresh["seconds"], failed["seconds"] == 7.0))

    def test_s9_mutant(self):
        mod = mutant_module(('"renew-git-token", self._cost_or_zero(pod), True, "", seconds=took)', '"renew-git-token", self._cost_or_zero(pod), True, "")'))
        with self.assertRaises(AssertionError) as cm:
            self.check_s9(mod)
        self.assertEqual(str(cm.exception), "a renewal wrote no seconds")
        LINES.append("S9: control 1 of 1, mutant 1 of 1 with the text")

    # ---- S10
    HOOK_TEXT = "pod said " + " ".join(SECRETS) + " at /home/x/.state 10.1.2.3"

    def check_s10(self, mod):
        self.patched(mod, [{"pushed": False, "wip_branch": None, "error": self.HOOK_TEXT, "refresh": "needed"}, {"pushed": False, "wip_branch": None, "note": self.HOOK_TEXT}], refresh_seconds=4)
        self.assertEqual(self.cli(mod, "stop", "coder7", "--reason", "retire"), 0)
        self.renewal_lines(mod, fail=True)
        self.refresh_line(mod)
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            raw = f.read()
        lines = [json.loads(x) for x in raw.splitlines()]
        stop = [l for l in lines if l["action"] == "stop"][-1]
        for k, v in stop["steps"].items():
            if k not in NAMES or isinstance(v, bool) or not isinstance(v, (int, float)):
                raise AssertionError("a step held a text")
        return raw, lines, stop

    def test_s10_control(self):
        raw, lines, stop = self.check_s10(cw)
        held = sum(1 for l in lines if l["action"] in ("stop", "renew-git-token", "refresh-cluster-token") and any(s in json.dumps(l) for s in SECRETS))
        self.assertEqual(held, 0)
        self.assertNotIn("10.1.2.3", raw)
        self.assertNotIn("/home/x", raw)
        self.assertEqual(len(stop["steps"]), 6)
        # the readers read the lines as before: the same report and the same last line with and without the new fields
        report, path_with, path_without = load_cost_report(), self.env["CW_CLOUD_AUDIT"], os.path.join(self.tmp.name, "plain-all.jsonl")
        report_without = os.path.join(self.tmp.name, "without.jsonl")
        with open(path_without, "w") as f, open(report_without, "w") as g:   # the report takes its own actions only (it refuses a renewal line either way)
            for l in lines:
                plain = json.dumps({k: v for k, v in l.items() if k not in ("steps", "seconds")}) + "\n"
                f.write(plain)
                if l["action"] in report.ACTIONS:
                    g.write(plain)
        args = ["2026-10-14", None, "--node-price", "0.5", "--node-vcpu", "4", "--node-hours", "20"]
        outs = []
        report_with = os.path.join(self.tmp.name, "with.jsonl")
        with open(report_with, "w") as f:
            for l in lines:
                if l["action"] in report.ACTIONS:
                    f.write(json.dumps(l) + "\n")
        for p in (report_with, report_without):
            out = io.StringIO()
            code = report.main([args[0], p] + args[2:], out)
            outs.append((code, out.getvalue()))
        self.assertEqual(outs[0][0], 0)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(cw.last_audit_line(path_with, "stop"), cw.last_audit_line(path_without, "stop"))
        self.assertEqual(cw.last_audit_line(path_with, "renew-git-token"), cw.last_audit_line(path_without, "renew-git-token"))
        LINES.append("S10: keys %d of 6 names, values %d of 6 numbers, %d of 3 lines hold a synthetic secret, cost report totals equal %s" % (
            len(stop["steps"]), len(stop["steps"]), held, outs[0] == outs[1]))

    def test_s10_mutant(self):
        mod = mutant_module(("clean = _clean_steps(steps)", "clean = dict(steps, hook=str(self.last_hook)) if steps else {}"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s10(mod)
        self.assertEqual(str(cm.exception), "a step held a text")
        LINES.append("S10: control 1 of 1, mutant 1 of 1 with the text")


    # ---- S12
    def check_s12(self, mod, outside=4.5):
        code, line = self.stop_line(mod, [{"pushed": False, "wip_branch": None}], outside=outside)
        self.assertEqual(code, 0)
        steps = line["steps"]
        total = sum(steps.values())
        if "other" not in steps or abs(total - line["seconds"]) > 0.1:
            raise AssertionError("a stop left seconds unaccounted: steps sum %.1f against %.1f" % (total, line["seconds"]))
        return line, steps

    def test_s12_control(self):
        line, steps = self.check_s12(cw)
        self.assertEqual(line["seconds"], 41.0)
        self.assertEqual(steps, {"record": 2.0, "hook": 30.0, "delete": 3.0, "scrub": 1.5, "other": 4.5})
        LINES.append("S12: seconds %.1f, steps sum %.1f (other %.1f)" % (line["seconds"], sum(steps.values()), steps["other"]))

    def test_s12_a_stop_with_no_outside_time_has_other_zero(self):
        line, steps = self.check_s12(cw, outside=0)
        self.assertEqual((line["seconds"], steps["other"]), (36.5, 0.0))
        LINES.append("S12: no-outside stop other %.1f" % steps["other"])

    def test_s12_mutant(self):
        mod = mutant_module(('            info["steps"]["other"] = max(0.0, total - sum(v for k, v in info["steps"].items() if k != "other"))', "            pass"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s12(mod)
        self.assertEqual(str(cm.exception), "a stop left seconds unaccounted: steps sum 36.5 against 41.0")
        LINES.append("S12: control 1 of 1, mutant 1 of 1 with the text (36.5 against 41.0)")


def tearDownModule():
    for l in LINES:
        print(l)


if __name__ == "__main__":
    unittest.main()

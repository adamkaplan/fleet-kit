"""The reaper as a managed process (work item of #876): why it exits, one pass over mixed pods, and a restart that meets a
pod the last pass already stopped. Offline: the fake kubectl and a fake clock. Every ending of the process is one row of ENDINGS."""
import ast
import io
import json
import os
import re
import signal
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

LINE = r"cloud-worker: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ reaper exiting: "
SOURCE = open(os.path.join(os.path.dirname(HERE), "cloud_worker.py")).read()

# Every way the reaper process ends: row name -> (the reason its one line gives, where the code writes it).
ENDINGS = {
    "end of a single pass": ("end of its pass", "_reap_main"),
    "signal": ("stopped by signal 15", "_reap_main"),
    "interrupt": ("stopped by interrupt", "_reap_main"),
    "crash in a pass": ("error: boom", "_reap_main"),
    "login failure: token file unusable": ("cluster login failed: token unavailable", "main"),
    "login failure: no login set": ("cluster login failed: no login is set", "main"),
}


def _clean_hook(case):
    case.patch_state(hook_env={"CW_WORKDIR": base.make_repo(case.tmp.name, "r", False)[0], "CW_WORKER": "x"})


class ReaperEndings(base.Base):
    def run_ending(self, name):
        """Drive the real main() to that ending; returns (code or exception, stderr)."""
        err, orig = io.StringIO(), cw.Launcher.reap
        self.addCleanup(setattr, cw.Launcher, "reap", orig)
        env, argv, sleep = dict(self.env), ["reap"], (lambda s: None)
        if name == "signal":
            argv = ["reap", "--loop", "5"]
            sleep = lambda s: os.kill(os.getpid(), signal.SIGTERM)
        elif name == "interrupt":
            cw.Launcher.reap = lambda self_: (_ for _ in ()).throw(KeyboardInterrupt())
        elif name == "crash in a pass":
            cw.Launcher.reap = lambda self_: (_ for _ in ()).throw(RuntimeError("boom"))
        elif name.startswith("login failure: token"):
            del env["CW_KUBE_CONTEXT"]
            env.update(CW_CLOUD_TOKEN_FILE=os.path.join(self.tmp.name, "missing"), CW_CLOUD_SERVER="https://api.example.internal")
        elif name.startswith("login failure: no login"):
            del env["CW_KUBE_CONTEXT"]
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cw.main(argv, env, self.clock, sleep)
        except RuntimeError as e:   # the crash propagates, as it always did, after the line
            code = e
        return code, err.getvalue()

    def test_every_ending_writes_exactly_one_line_with_its_own_reason(self):
        for name, (reason, _) in ENDINGS.items():
            with self.subTest(ending=name):
                code, err = self.run_ending(name)
                self.assertEqual(err.count("reaper exiting:"), 1, err)
                self.assertRegex(err, LINE + re.escape(reason))
                if name != "end of a single pass":
                    self.assertNotEqual(code, 0, name)
                for other, (r, _) in ENDINGS.items():   # a generic reason for every exit fails here
                    if other != name and not r.startswith(reason[:15]):
                        self.assertNotIn("reaper exiting: " + r, err)

    def test_a_login_failure_exits_non_zero_and_never_prints_the_token(self):
        code, err = self.run_ending("login failure: token file unusable")
        self.assertEqual(code, cw.EXIT_CLOUD)
        secret = "".join(chr(97 + (i * 7) % 26) for i in range(40))   # built at run time: no secret-shaped literal here
        tok = os.path.join(self.tmp.name, "token")
        with open(tok, "w") as f:
            f.write(secret)
        env = dict(self.env, CW_CLOUD_TOKEN_FILE=tok)   # a readable token but no server: Kube() refuses after reading it
        del env["CW_KUBE_CONTEXT"]
        out = io.StringIO()
        with redirect_stderr(out), redirect_stdout(io.StringIO()):
            code = cw.main(["reap"], env, self.clock, lambda s: None)
        self.assertEqual(code, cw.EXIT_USAGE)
        self.assertRegex(out.getvalue(), LINE + "cluster login failed: no server")
        self.assertNotIn(secret, out.getvalue())

    def test_the_table_is_every_place_the_code_writes_the_exit_line(self):
        tree = ast.parse(SOURCE)
        sites = {}
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_reap_exit_line"]:
                sites.setdefault(fn.name, []).append(call)
        self.assertEqual(sorted(sites), ["_reap_main", "main"])
        self.assertEqual(len(sites["_reap_main"]), 1)
        self.assertEqual(len(sites["main"]), 2)
        fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_reap_main"][0]
        whys = {ast.unparse(a.value)[:20] for a in ast.walk(fn) if isinstance(a, ast.Assign) and any(getattr(t, "id", "") == "why" for t in a.targets)}
        self.assertEqual(len(whys), 3, whys)   # pass, stopped by (signal or interrupt), error
        self.assertEqual(sum(1 for _, w in ENDINGS.values() if w == "_reap_main"), 4)   # the 3 forms: pass, stopped (x2), error
        self.assertEqual(sum(1 for _, w in ENDINGS.values() if w == "main"), 2)
        for call in sites["main"]:   # a login reason names the login and carries no environment value
            self.assertIn("cluster login failed", ast.unparse(call))

    def test_the_interval_is_never_slower_than_the_floor(self):
        self.assertEqual(cw.REAP_FLOOR_SECONDS, 60)
        slept = []
        orig = cw.Launcher.reap
        calls = []

        def once(self_):
            calls.append(1)
            if len(calls) > 1:
                raise cw.Interrupted("signal 15")
            return []
        cw.Launcher.reap = once
        self.addCleanup(setattr, cw.Launcher, "reap", orig)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            cw.main(["reap", "--loop", "3600"], self.env, self.clock, slept.append)
        self.assertEqual(slept, [60])


class OnePassOverMixedPods(base.Base):
    def setUp(self):
        super().setUp()
        for w, hard in (("idle1", "2h"), ("past1", "10m"), ("bad1", "2h")):
            self.create(w, "--hard", hard)
        _clean_hook(self)
        for w in ("idle1", "past1"):
            self.set_activity(w, w == "past1", base.T0)   # past1 is busy: only the hard limit can stop it
        pod = self.st()["pods"][cw.pod_name("bad1")]
        pod["metadata"]["annotations"]["cw/hard-deadline"] = "not a time"
        st = self.st()
        st["pods"][cw.pod_name("bad1")] = pod
        self.write_state(st)
        self.clock.advance(minutes=11)

    def test_one_idle_one_past_hard_one_unreadable_gives_two_stops_and_one_line(self):
        res = cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual(sorted(r[:2] for r in res), [("bad1", "unreadable deadline"), ("idle1", "idle"), ("past1", "hard limit")])
        self.assertEqual(sorted(r[2] for r in res), [0, 0, 1])
        self.assertEqual(sorted(l["worker"] for l in self.audit_lines() if l["action"] == "stop"), ["idle1", "past1"])
        self.assertIsNone(self.pod("idle1"))
        self.assertIsNone(self.pod("past1"))
        self.assertIsNotNone(self.pod("bad1"))

    def test_the_loop_goes_on_after_the_unreadable_pod(self):
        passes = []
        orig = cw.Launcher.reap

        def counted(self_):
            passes.append(1)
            if len(passes) > 1:
                raise cw.Interrupted("signal 15")
            return orig(self_)
        cw.Launcher.reap = counted
        self.addCleanup(setattr, cw.Launcher, "reap", orig)
        code, out, err = self.run_cli("reap", "--loop", "30")
        self.assertEqual(code, cw.EXIT_INTERRUPTED)
        self.assertEqual(len(passes), 2)   # the second pass ran: the bad pod did not end the process
        self.assertIn("unreadable deadline", out)
        self.assertEqual(err.count("reaper exiting:"), 1)


class RestartMeetsAStoppedPod(base.Base):
    def stops(self):
        return [l for l in self.audit_lines() if l["action"] == "stop"] if os.path.exists(self.env["CW_CLOUD_AUDIT"]) else []

    def calls(self, verb):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [c for c in map(json.loads, f) if verb in c]

    def test_the_next_pass_after_a_stop_stops_nothing(self):
        self.create("past1", "--hard", "10m")
        _clean_hook(self)
        self.set_activity("past1", True, base.T0)
        self.clock.advance(minutes=11)
        self.assertEqual(self.run_cli("reap")[0], 0)
        self.assertEqual(len(self.stops()), 1)
        deletes = len(self.calls("delete"))
        code, out, err = self.run_cli("reap")   # the restarted reaper
        self.assertEqual((code, json.loads(out)), (0, []))
        self.assertNotIn("error", err.lower().replace("reaper exiting: end of its pass", ""))
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(len(self.calls("delete")), deletes)

    def test_a_pod_listed_before_the_previous_pass_stopped_it_is_not_stopped_twice(self):
        """The restarted reaper holds a list taken before the old process finished: the stop is by name and finds no pod."""
        self.create("past1", "--hard", "10m")
        _clean_hook(self)
        self.set_activity("past1", True, base.T0)
        stale = cw.Kube(self.env).list_pods()
        self.clock.advance(minutes=11)
        self.assertEqual(self.run_cli("reap")[0], 0)
        deletes, execs = len(self.calls("delete")), len(self.calls("exec"))
        launcher = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        launcher.kube.list_pods = lambda: stale
        res = launcher.reap()
        self.assertEqual([r[2] for r in res], [0])   # no error
        self.assertEqual(len(self.calls("delete")), deletes)   # nothing deleted again
        self.assertEqual(len(self.calls("exec")), execs)       # the end hook did not run again
        self.assertIsNone(self.pod("past1"))
        self.assertEqual([l["ok"] for l in self.stops()], [True, True])   # the second is the by-name stop that found nothing


if __name__ == "__main__":
    unittest.main()

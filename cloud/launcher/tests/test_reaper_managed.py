"""The reaper as a managed process (work item of #876): why it exits, one pass over mixed pods, and a restart that meets a
pod the last pass already stopped. Offline: the fake kubectl and a fake clock. Every ending of the process is one row of ENDINGS."""
import ast
import io
import itertools
import json
import os
import re
import signal
import subprocess
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
    "another reaper holds the lock": ("not started: another reaper holds the lock", "_reap_main"),
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
        elif name == "another reaper holds the lock":
            self.holder = cw.reaper_lock(cw.reaper_identity("cw"))
            self.addCleanup(os.close, self.holder)
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
                if name == "another reaper holds the lock":
                    self.assertEqual(code, cw.EXIT_REFUSED)
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
        self.assertEqual(len(whys), 4, whys)   # pass, stopped by (signal or interrupt), error, not started
        self.assertEqual(sum(1 for _, w in ENDINGS.values() if w == "_reap_main"), 5)   # the 4 forms: pass, stopped (x2), error, lock
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

    def _list_in_order(self, order):
        st = self.st()
        pods = st["pods"]
        st["pods"] = {cw.pod_name(w): pods[cw.pod_name(w)] for w in order}
        self.write_state(st)
        self.assertEqual([p["metadata"]["name"] for p in cw.Kube(self.env).list_pods()], [cw.pod_name(w) for w in order])

    def test_one_idle_one_past_hard_one_unreadable_gives_two_stops_and_one_line_in_every_order(self):
        """The bad pod may come first, second or last: a pass that stops at it (return or raise) or skips the next pod fails."""
        for order in itertools.permutations(("idle1", "past1", "bad1")):
            with self.subTest(order=order):
                self.setUp()   # a fresh cluster for each order
                self._list_in_order(order)
                code, out, _ = self.run_cli("reap")
                self.assertEqual(code, cw.EXIT_HOOK)   # the pass finishes, with the documented non-zero status
                res = json.loads(out)
                self.assertEqual(sorted((r["worker"], r["reason"]) for r in res), [("bad1", "unreadable deadline"), ("idle1", "idle"), ("past1", "hard limit")])
                self.assertEqual(sorted(r["exit"] for r in res), [0, 0, 1])
                self.assertEqual(sorted((l["worker"], l.get("reason", "").split(";")[0]) for l in self.audit_lines() if l["action"] == "stop"),
                                 sorted([("idle1", "idle"), ("past1", "hard limit")]))
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


class TwoReapersOneCluster(base.Base):
    def test_a_second_reaper_with_another_audit_directory_is_still_a_rival(self):
        """The lock is keyed on what the reaper acts on (cluster and namespace), not on where it keeps its own files."""
        holder = cw.reaper_lock(cw.reaper_identity("cw"))
        self.addCleanup(os.close, holder)
        other = dict(self.env, CW_CLOUD_AUDIT=os.path.join(self.tmp.name, "elsewhere", "audit.jsonl"), XDG_STATE_HOME=os.path.join(self.tmp.name, "state"))
        before = len(self.calls())
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cw.main(["reap"], other, self.clock, lambda s: None)
        self.assertEqual(code, cw.EXIT_REFUSED)
        self.assertRegex(err.getvalue(), LINE + "not started: another reaper holds the lock")
        self.assertEqual(len(self.calls()), before)   # no kubectl call: no end hook

    def setUp(self):
        super().setUp()
        self.create("past1", "--hard", "10m")
        self.create("past2", "--hard", "10m", "--run-id", "aaaaaaaa11111111")
        _clean_hook(self)
        for w in ("past1", "past2"):
            self.set_activity(w, True, base.T0)
        self.clock.advance(minutes=11)

    def calls(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [json.loads(x) for x in f]

    def logins(self):
        """One rival per way of logging in (each a login the launcher accepts): none of them changes what the reaper acts on."""
        tok = os.path.join(self.tmp.name, "token")
        with open(tok, "w") as f:
            f.write("".join(chr(97 + (i * 3) % 26) for i in range(40)))   # built at run time
        by_server = lambda server: {"CW_CLOUD_SERVER": server, "CW_CLOUD_TOKEN_FILE": tok}
        return {
            "a kube context": {"CW_KUBE_CONTEXT": "acme-other"},
            "a kubeconfig and a context": {"CW_KUBECONFIG": os.path.join(self.tmp.name, "kubeconfig"), "CW_KUBE_CONTEXT": "acme-cw"},
            "a token file and a server": by_server("https://api.example.internal"),
            "a server with a trailing slash": by_server("https://api.example.internal/"),
            "a server in capitals": by_server("HTTPS://API.EXAMPLE.INTERNAL"),
        }

    def test_a_rival_is_a_rival_whatever_its_login_says(self):
        holder = cw.reaper_lock(cw.reaper_identity("cw"))   # the service: a token-file login
        self.addCleanup(os.close, holder)
        for name, login in self.logins().items():
            with self.subTest(login=name):
                other = {k: v for k, v in self.env.items() if k != "CW_KUBE_CONTEXT"}
                other.update(login)
                before = len(self.calls())
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    code = cw.main(["reap"], other, self.clock, lambda s: None)
                self.assertEqual(code, cw.EXIT_REFUSED, err.getvalue())
                self.assertRegex(err.getvalue(), LINE + "not started: another reaper holds the lock")
                self.assertEqual(len(self.calls()), before)

    def test_the_key_holds_nothing_about_the_login(self):
        import inspect
        self.assertEqual(list(inspect.signature(cw.reaper_identity).parameters), ["namespace"])   # a login input put back changes this
        self.assertEqual(cw.reaper_identity("cw"), cw.reaper_identity("cw"))
        self.assertNotEqual(cw.reaper_identity("cw"), cw.reaper_identity("other"))

    def test_a_reaper_of_another_namespace_is_not_a_rival(self):
        holder = cw.reaper_lock(cw.reaper_identity("cw"))
        self.addCleanup(os.close, holder)
        other = dict(self.env, CW_CLOUD_NAMESPACE="other")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cw.main(["reap"], other, self.clock, lambda s: None)
        self.assertNotEqual(code, cw.EXIT_REFUSED, err.getvalue())

    def test_a_user_with_no_password_entry_gets_a_reason_and_the_launcher_still_imports(self):
        old = cw.REAPER_LOCK_ROOT
        cw.REAPER_LOCK_ROOT = None
        self.addCleanup(setattr, cw, "REAPER_LOCK_ROOT", old)
        def nobody(uid):
            raise KeyError(uid)
        orig = cw.pwd.getpwuid
        self.addCleanup(setattr, cw.pwd, "getpwuid", orig)
        cw.pwd.getpwuid = nobody
        with self.assertRaises(cw.ReaperLocked) as ctx:
            cw.reaper_lock(cw.reaper_identity("cw"))
        self.assertIn("password database", str(ctx.exception))
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cw.main(["status"], self.env, self.clock, lambda s: None)   # a command that takes no lock still works
        self.assertEqual(code, 0, err.getvalue())

    def test_a_second_reaper_started_while_the_first_stops_exits_and_touches_nothing(self):
        seen, real = {}, cw.Launcher.stop

        def stop_with_a_rival(self_, a, reason=None):
            if "second" not in seen:   # the rival starts while the first reaper is in the middle of a pass
                before = len(self.calls())
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    seen["second"] = cw.main(["reap"], self.env, self.clock, lambda s: None)
                seen["err"], seen["calls"] = err.getvalue(), len(self.calls()) - before
            return real(self_, a, reason)
        cw.Launcher.stop = stop_with_a_rival
        self.addCleanup(setattr, cw.Launcher, "stop", real)
        code, out, err = self.run_cli("reap")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["second"], cw.EXIT_REFUSED)
        self.assertEqual(seen["calls"], 0)   # the second reaper made no kubectl call at all
        self.assertRegex(seen["err"], LINE + "not started: another reaper holds the lock")
        hooks = [c for c in self.calls() if "cw-end-hook" in c]
        self.assertEqual(len(hooks), 2)   # one end hook per pod, never two for one pod
        self.assertEqual(sorted(l["worker"] for l in self.audit_lines() if l["action"] == "stop"), ["past1", "past2"])

    def test_the_stop_carries_the_pods_own_run_id_where_it_has_one(self):
        self.run_cli("reap")
        reasons = {l["worker"]: l["reason"] for l in self.audit_lines() if l["action"] == "stop"}
        self.assertIn("run-id=aaaaaaaa11111111", reasons["past2"])
        self.assertNotIn("run-id", reasons["past1"])   # a pod made without one is stopped by name, as before

    def test_a_killed_holder_releases_the_lock(self):
        code = ("import sys,time;sys.path.insert(0,%r);import cloud_worker as c;c.reaper_lock(c.reaper_identity('cw'), sys.argv[1]);print('held',flush=True);time.sleep(60)" % cw.__file__.rsplit("/", 1)[0])
        holder = subprocess.Popen([sys.executable, "-c", code, self.lock_root], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        import select
        self.assertTrue(select.select([holder.stdout], [], [], 20)[0], "the holder did not start")   # fail fast, never hang
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertEqual(self.run_cli("reap")[0], cw.EXIT_REFUSED)   # alive: refused
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        self.assertEqual(self.run_cli("reap")[0], 0)   # killed with no chance to clean up: the kernel let go

    def test_a_lock_path_that_is_not_the_users_private_file_is_refused(self):
        d = self.lock_root
        ident = cw.reaper_identity("cw")
        os.makedirs(d, mode=0o700, exist_ok=True)
        lock = os.path.join(d, "reaper-%s.lock" % __import__("hashlib").sha256(ident.encode()).hexdigest()[:16])
        victim = os.path.join(self.tmp.name, "victim")
        open(victim, "w").close()
        os.chmod(victim, 0o600)   # a private file of the user, so only refusing the link itself can stop the symlink row
        rows = {
            "a symlink": lambda: os.symlink(victim, lock),
            "a directory": lambda: os.mkdir(lock),
            "group readable": lambda: (open(lock, "w").close(), os.chmod(lock, 0o640)),
            "a FIFO": lambda: os.mkfifo(lock),
        }
        for name, make in rows.items():
            with self.subTest(row=name):
                if os.path.lexists(lock):
                    os.path.isdir(lock) and not os.path.islink(lock) and os.rmdir(lock) or (os.path.lexists(lock) and os.unlink(lock))
                make()
                with self.assertRaises(cw.ReaperLocked):
                    cw.reaper_lock(ident)
        os.unlink(lock)
        os.chmod(d, 0o770)
        with self.assertRaises(cw.ReaperLocked):   # a state directory others can write
            cw.reaper_lock(ident)
        os.chmod(d, 0o700)


if __name__ == "__main__":
    unittest.main()

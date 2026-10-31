"""#1579 second part: the reaper restarts itself between passes when the installed code changes, and its login refresh never leaves a long gap.

Offline: fakes of test_reaper_renewal.py (clock, cluster, pods). The mutants are scratch copies OUTSIDE the repository, the edit asserted applied,
each run in a child process through stub_support.start that must exit with status 1.
"""
import contextlib
import datetime as dt
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
import stub_support  # noqa: E402
import test_reaper_renewal as R  # noqa: E402
import cloud_worker as real  # noqa: E402

MODULE_ENV = "CW_TEST_RESTART_MODULE"
MIN = R.MIN


def target():
    path = os.environ.get(MODULE_ENV)
    return R.load_module(path) if path else real


def make_tree(directory, text="one"):
    for rel in real.CODE_IDENTITY_FILES[:2]:   # the third file is left missing: a missing file is part of the identity
        path = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


def change_tree(directory):
    with open(os.path.join(directory, real.CODE_IDENTITY_FILES[0]), "a") as f:
        f.write("x")


def run_loop(mod, tmp, tree, sleep, stop=None, wrap_clock=None):
    clock = R.Clock()
    launcher, kube, refresher, ticker, renewed, attempts, env = R.build(mod, clock, tmp, stop)
    if wrap_clock:
        wrap_clock(launcher)
    devnull = os.open(os.devnull, os.O_RDONLY)
    err = io.StringIO()
    with mock.patch.object(mod, "reaper_lock", return_value=devnull), mock.patch.object(mod, "Launcher", return_value=launcher), \
            contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            status = mod._reap_main(env, R.types_ns(loop=60), sleep, clock=clock, refresher=R.Refresher(kube), kube=kube,
                                    ticker_wait=lambda: __import__("time").sleep(0.01), tree=tree)
        except KeyboardInterrupt:
            status = None
    return status, err.getvalue(), R.audit_lines(env), launcher


class S13Restart(unittest.TestCase):
    def test_identity_follows_bytes_and_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tree:
            make_tree(tree)
            a = real.code_identity(tree)
            self.assertEqual(a, real.code_identity(tree))
            change_tree(tree)
            self.assertNotEqual(a, real.code_identity(tree))

    def test_exit_after_a_change_and_none_without_one(self):
        mod = target()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tree:
            make_tree(tree)
            count = []

            def sleep(s):
                count.append(1)
                if len(count) == 3:
                    change_tree(tree)
                if len(count) > 20:
                    raise KeyboardInterrupt   # a loop that never compares ends here, with no exit status
            status, err, audit, _ = run_loop(mod, tmp, tree, sleep)
            changed = [x for x in audit if x["action"] == "reaper-restart"]
            exits_after = 1 if status == 0 else 0
            self.assertEqual((status, len(changed)), (0, 1))
            self.assertEqual(changed[0]["reason"], "the installed code changed")
            self.assertEqual(changed[0]["worker"], "")
            self.assertEqual(len([l for l in err.splitlines() if l.startswith("cloud-worker: the installed code changed")]), 1)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tree:
            make_tree(tree)
            count = []

            def sleep(s):
                count.append(1)
                if len(count) == 10:
                    raise KeyboardInterrupt
            status2, err2, audit2, _ = run_loop(mod, tmp, tree, sleep)
            exits_none = 0 if status2 is None else 1
        print("S13: exits after a change %d (status %d), audit lines %d, exits in 10 unchanged passes %d" % (exits_after, status, len(changed), exits_none))
        self.assertEqual(exits_none, 0)

    def test_a_single_pass_never_restarts(self):
        mod = real
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tree:
            make_tree(tree)
            clock = R.Clock()
            launcher, kube, refresher, ticker, renewed, attempts, env = R.build(mod, clock, tmp)
            launcher.reap = lambda: (change_tree(tree), [])[1]
            devnull = os.open(os.devnull, os.O_RDONLY)
            with mock.patch.object(mod, "reaper_lock", return_value=devnull), mock.patch.object(mod, "Launcher", return_value=launcher), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                mod._reap_main(env, R.types_ns(loop=0), lambda s: None, clock=clock, refresher=R.Refresher(kube), kube=kube,
                               ticker_wait=lambda: __import__("time").sleep(0.01), tree=tree)
            self.assertEqual([x for x in R.audit_lines(env) if x["action"] == "reaper-restart"], [])


class S14AfterTheStop(unittest.TestCase):
    def test_the_exit_comes_after_the_stop_and_its_end_hook(self):
        mod = target()
        events = []
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tree:
            make_tree(tree)
            holder = {}

            def stop(args, reason):
                events.append("stop-start")
                change_tree(tree)
                holder["launcher"].clock()   # the end hook runs: time passes with the change already on disk
                events.append("stop-end")
                return {}

            def wrap_clock(launcher):
                holder["launcher"] = launcher

            status, err, audit, _ = run_loop(mod, tmp, tree, lambda s: None, stop, wrap_clock)
            events.append("exit")
        interrupted = events.count("stop-start") - events.count("stop-end")
        print("S14: stops interrupted %d, exit after the stop ended %d" % (interrupted, 1 if events[-2:] == ["stop-end", "exit"] else 0))
        self.assertEqual(interrupted, 0)
        self.assertEqual(events[-2:], ["stop-end", "exit"])
        self.assertEqual(status, 0)


class S15Docs(unittest.TestCase):
    def test_two_sentences(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split()).lower()
        found = [p in text for p in ("the reaper restarts itself when the installed code changes", "a change of its service file (its environment or arguments) still needs the install command")]
        print("S15: %d of 2 sentences found" % sum(found))
        self.assertTrue(found[0] and found[1])
        self.assertIn("install command", text)


class LoginModel(R.Refresher):
    """A model of the real refresher: after a failed attempt it answers 'waiting' for 300 s (CLUSTER_TOKEN_RETRY_SECONDS), then tries again."""
    RETRY_GAP = 300

    def __init__(self, kube, clock, good, failed, attempts):
        super().__init__(kube)
        self.clock, self.good, self.failed, self.attempts = clock, good, failed, attempts
        self.last_failure, self.from_ticker = None, False

    def run(self, force=False):
        now = self.clock.now
        if not force and self.last_failure is not None and 0 <= (now - self.last_failure).total_seconds() < self.RETRY_GAP:
            return R.types_ns(state="waiting", token="", reason="the vault did not answer")
        if self.from_ticker:
            self.attempts.append(now)
        if self.from_ticker and now >= R.T0 + 100 * MIN and len(self.failed) < 2:
            self.failed.append(now)
            self.last_failure = now
            return R.types_ns(state="failed", token="", reason="the vault did not answer")
        self.last_failure = None
        self.good.append(now)
        return super().run(force)


class S12LoginGap(unittest.TestCase):
    def test_the_longest_gap_between_successful_refreshes_is_within_30_minutes(self):
        mod = target()
        clock = R.Clock()
        good, failed, attempts = [], [], []
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            launcher, kube, refresher, ticker, renewed, attempted, env = R.build(mod, clock, tmp, with_ticker=True, wait=lambda: None)
            model = LoginModel(kube, clock, good, failed, attempts)
            ticker.refresher = model
            good.append(clock.now)
            with contextlib.redirect_stderr(err):
                for step in range(3 * 60 * 4):
                    clock.now += dt.timedelta(seconds=15)
                    minute = step // 4
                    stalled = 40 <= minute < 90
                    raised = minute in (120, 121)   # a pass that raises before the renewal step: no refresh from it either
                    if minute % 5 == 0 and step % 4 == 0 and not stalled and not raised:
                        model.from_ticker = False
                        model.run()
                    if ticker.due():
                        model.from_ticker = True
                        ticker.tick()
        good.append(clock.now)
        gap = max((b - a).total_seconds() for a, b in zip(good, good[1:])) / 60
        lines = [l for l in err.getvalue().splitlines() if "login refresh failed" in l]
        delays = [min((t - f).total_seconds() for t in attempts if t > f) / 60 if any(t > f for t in attempts) else 999 for f in failed]
        print("S12: longest gap between successful login refreshes %d minutes (limit 30), failed refreshes reported %d" % (round(gap), len(lines)))
        print("S12: longest wait from a failed refresh to the next attempt %s minutes (limit 6)" % (round(max(delays), 1) if delays else None))
        self.assertLessEqual(gap, 30)
        self.assertEqual((len(failed), len(lines)), (2, 2))
        self.assertLessEqual(max(delays), 6)   # the ticker's retry follows the refresher's 300 s gap: not a full cadence later


def mutant(directory, name, edits):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    for old, new in edits:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    d = os.path.join(directory, name)
    os.makedirs(d)
    path = os.path.join(d, "cloud_worker_scratch.py")
    with open(path, "w") as f:
        f.write(src)
    return path


class Mutants(unittest.TestCase):
    def child(self, name, edits, test, want=1):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant(tmp, name, edits)   # outside the repository; the edit asserted applied
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reaper_restart." + test], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=120)[0]
        self.assertEqual(p.returncode, 1, out)
        return out

    def test_a_loop_that_never_compares_fails_the_restart_test(self):
        self.child("mutant-13", [("if code_identity(tree) != identity:", "if False:")], "S13Restart.test_exit_after_a_change_and_none_without_one")

    def test_a_restart_at_the_moment_of_the_change_fails_the_stop_test(self):
        self.child("mutant-14", [("            began = clock()\n", "            began = clock()\n            _o = L.clock\n            L.clock = lambda: (code_identity(tree) == identity or sys.exit(0)) and _o()   # the restart at the moment of the change\n")], "S14AfterTheStop")

    def test_a_refresh_on_the_pass_cadence_only_shows_the_gap(self):
        self.child("mutant-12", [("RENEW_CADENCE_SECONDS = 600 ", "RENEW_CADENCE_SECONDS = 10 ** 9 ")], "S12LoginGap")

    def test_a_ticker_without_the_retry_flag_fails_the_login_test(self):
        out = self.child("mutant-12b", [("return self.retry or (self.clock()", "return (self.clock()")], "S12LoginGap")
        import re
        m = re.search(r"S12: longest gap between successful login refreshes (\d+) minutes", out)
        w = re.search(r"next attempt (\S+) minutes", out)
        self.assertTrue(m, out)
        print("S12: retry-dropped mutant gap %s minutes (limit 30); wait from a failed refresh to the next attempt %s minutes (limit 6)" % (
            m.group(1), w.group(1) if w else "?"))


def kit_files_of(rel):
    """The kit files (cloud/launcher/X.py, bin/X.py) that the file `rel` names: by import (module level and inside functions) and by a
    spec_from_file_location / SourceFileLoader call whose arguments carry a 'X.py' string."""
    import ast
    with open(os.path.join(ROOT, rel)) as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if called in ("spec_from_file_location", "SourceFileLoader"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.endswith(".py"):
                        names.add(sub.value[:-3])
    return {r for n in names for r in ("cloud/launcher/%s.py" % n, "bin/%s.py" % n) if os.path.isfile(os.path.join(ROOT, r))}


class S13ImportsInIdentity(unittest.TestCase):
    def test_every_imported_kit_module_is_in_the_identity_list(self):
        listed = list(target().CODE_IDENTITY_FILES)
        found, todo = set(), list(listed)
        while todo:   # the transitive closure over the listed modules and what they name
            for rel in kit_files_of(todo.pop()) - found:
                found.add(rel)
                todo.append(rel)
        found.discard("cloud/launcher/cloud_worker.py")
        missing = sorted(rel for rel in found if rel not in listed)
        print("S13: kit modules found transitively %d, all in the identity list" % len(found))
        self.assertEqual(missing, [])


class S13ImportsMutant(unittest.TestCase):
    def drop(self, old):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
                src = f.read()
            assert src.count(old) == 1
            d = os.path.join(tmp, "mutant-13b")
            os.makedirs(d)
            path = os.path.join(d, "cloud_worker_scratch.py")
            with open(path, "w") as f:
                f.write(src.replace(old, ""))
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reaper_restart.S13ImportsInIdentity"], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=120)[0]
        self.assertEqual(p.returncode, 1, out)

    def test_the_list_without_token_mint_fails_the_test(self):
        self.drop(', "cloud/launcher/token_mint.py"')

    def test_the_list_without_record_json_fails_the_test(self):
        self.drop(', "bin/record_json.py"')


if __name__ == "__main__":
    unittest.main()

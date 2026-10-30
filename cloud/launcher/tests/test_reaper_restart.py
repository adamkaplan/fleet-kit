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


class S12LoginGap(unittest.TestCase):
    def test_the_longest_gap_between_successful_refreshes_is_within_30_minutes(self):
        mod = target()
        clock = R.Clock()
        good, failed = [], []

        class Flaky(R.Refresher):
            ticker_calls = 0

            def run(self, force=False):
                r = super().run(force)
                if getattr(self, "from_ticker", False):
                    Flaky.ticker_calls += 1
                    if clock.now >= R.T0 + 100 * MIN and len(failed) < 2:
                        failed.append(clock.now)
                        return R.types_ns(state="failed", token="", reason="the vault did not answer")
                good.append(clock.now)
                return r
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            launcher, kube, refresher, ticker, renewed, attempts, env = R.build(mod, clock, tmp, with_ticker=True, wait=lambda: None)
            flaky = Flaky(kube)
            ticker.refresher = flaky
            good.append(clock.now)
            with contextlib.redirect_stderr(err):
                for step in range(3 * 60 * 4):
                    clock.now += dt.timedelta(seconds=15)
                    minute = step // 4
                    stalled = 40 <= minute < 90
                    raised = minute in (120, 121)   # a pass that raises before the renewal step: no refresh from it either
                    if minute % 5 == 0 and step % 4 == 0 and not stalled and not raised:
                        flaky.from_ticker = False
                        flaky.run()
                    if ticker.due():
                        flaky.from_ticker = True
                        ticker.tick()
        good.append(clock.now)
        gap = max((b - a).total_seconds() for a, b in zip(good, good[1:])) / 60
        lines = [l for l in err.getvalue().splitlines() if "login refresh failed" in l]
        print("S12: longest gap between successful login refreshes %d minutes (limit 30), failed refreshes reported %d" % (round(gap), len(lines)))
        self.assertLessEqual(gap, 30)
        self.assertEqual((len(failed), len(lines)), (2, 2))


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
    def child(self, name, edits, test):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant(tmp, name, edits)   # outside the repository; the edit asserted applied
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reaper_restart." + test], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=120)[0]
        self.assertEqual(p.returncode, 1, out)

    def test_a_loop_that_never_compares_fails_the_restart_test(self):
        self.child("mutant-13", [("if code_identity(tree) != identity:", "if False:")], "S13Restart.test_exit_after_a_change_and_none_without_one")

    def test_a_restart_at_the_moment_of_the_change_fails_the_stop_test(self):
        self.child("mutant-14", [("            began = clock()\n", "            began = clock()\n            _o = L.clock\n            L.clock = lambda: (code_identity(tree) == identity or sys.exit(0)) and _o()   # the restart at the moment of the change\n")], "S14AfterTheStop")

    def test_a_refresh_on_the_pass_cadence_only_shows_the_gap(self):
        self.child("mutant-12", [("RENEW_CADENCE_SECONDS = 600 ", "RENEW_CADENCE_SECONDS = 10 ** 9 ")], "S12LoginGap")


class S13ImportsInIdentity(unittest.TestCase):
    def test_every_imported_kit_module_is_in_the_identity_list(self):
        import ast
        with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):   # module level and inside functions
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
        kit = sorted(rel for n in names for rel in ("cloud/launcher/%s.py" % n, "bin/%s.py" % n) if os.path.isfile(os.path.join(ROOT, rel)))
        listed = target().CODE_IDENTITY_FILES
        missing = [rel for rel in kit if rel not in listed]
        print("S13: imported kit modules %d, all in the identity list" % len(kit))
        self.assertEqual(missing, [])


class S13ImportsMutant(unittest.TestCase):
    def test_the_list_without_token_mint_fails_the_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
                src = f.read()
            old = ', "cloud/launcher/token_mint.py"'
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


if __name__ == "__main__":
    unittest.main()

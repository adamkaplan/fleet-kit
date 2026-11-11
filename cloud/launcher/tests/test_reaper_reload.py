"""Ask #2385: the reaper reads the settings file again at each pass (reload_settings). Offline: fake settings files in temp directories, a
fake kube and refresher, the real _reap_main loop with the real Launcher class (its reap replaced by a probe of the renewal's allow-list check).
Scenarios S2 S3 S4 S5 S6 S7; the mutant (the reaper reads the file only at its start) is run from a scratch copy by the Mutant test."""
import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402
from test_cloud_settings import GOOD  # noqa: E402  (a complete good settings set)

MODULE_ENV = "CW_TEST_REAPER_RELOAD_MODULE"
T0 = dt.datetime(2026, 11, 1, 9, 0, 0, tzinfo=dt.timezone.utc)
NEW = "acme-org/added-later"
ANCHOR = "the reaper kept the settings of its start"


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    spec = importlib.util.spec_from_file_location("cloud_worker_reload_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def text(**over):
    values = dict(GOOD, **over)
    return "".join("%s=%s\n" % kv for kv in values.items() if kv[1] is not None)


class Scene:
    """A settings file in a private directory, an environment that applied it (as main() does for the real process) and the holder."""

    def __init__(self, tmp, **over):
        self.mod, self.dir = target_module(), os.path.join(tmp, "cfg")
        os.mkdir(self.dir, 0o700)
        self.path = os.path.join(self.dir, "cloud.env")
        self.mod.SETTINGS_PATH = self.path
        self.write(text(**over))
        self.env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}
        self.mod.apply_settings(self.env)
        self.applied = {n: self.env[n] for n, _, _ in self.mod.SETTINGS if n in self.env}
        self.state = {}
        self.reads = 0
        orig = self.mod.apply_settings

        def counting(env=None, path=None):
            self.reads += 1
            return orig(env, path)
        self.mod.apply_settings = counting
        self.orig = orig

    def write(self, body, mode=0o600):
        with open(self.path, "w") as f:
            f.write(body)
        os.chmod(self.path, mode)

    def reload(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.mod.reload_settings(self.env, self.applied, self.state)
        return [x for x in err.getvalue().splitlines() if x]

    def close(self):
        self.mod.apply_settings = self.orig
        self.mod.SETTINGS_PATH = None


class FakeKube:
    def __init__(self):
        self.on_unauthorized = None

    def add_secrets(self, *a, **k):
        pass

    def close(self):
        pass


class FakeRefresher:
    notices = []

    def __init__(self, *a, **k):
        self.kube = None

    def run(self, unauthorized=False):
        return types.SimpleNamespace(token="", state="fresh", reason="")


class FakeTicker:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def close(self):
        pass


def run_passes(scene, steps):
    """Run the real _reap_main loop; `steps[i]` is done between pass i and pass i+1 (the last ends the loop). Returns (probe results, stderr).
    The probe is the renewal's own check, run on the launcher's environment in each pass."""
    mod, results, passes = scene.mod, [], []

    def reap(self):
        passes.append(1)
        assert self.env is scene.env, "the launcher reads the reaper's own environment"
        try:
            mod.check_repo_allowed(NEW, self.env)
            results.append("accepted")
        except mod.CloudError:
            results.append("refused")
        return []

    def sleep(seconds):
        i = len(passes) - 1
        if i >= len(steps):
            raise KeyboardInterrupt()
        steps[i]()

    saved = (mod.Launcher.reap, mod.RenewalTicker, mod.reaper_lock, mod.ClusterTokenRefresher, mod.Launcher.audit)
    mod.Launcher.reap, mod.RenewalTicker, mod.reaper_lock, mod.ClusterTokenRefresher = reap, FakeTicker, (lambda identity: os.open(os.devnull, os.O_RDONLY)), FakeRefresher
    mod._REAP_APPLIED = scene.applied
    err, out = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                mod._reap_main(scene.env, argparse.Namespace(loop=60), sleep, clock=lambda: T0, refresher=FakeRefresher(), kube=FakeKube(), tree=None)
            except KeyboardInterrupt:
                pass
    finally:
        mod.Launcher.reap, mod.RenewalTicker, mod.reaper_lock, mod.ClusterTokenRefresher, mod.Launcher.audit = saved
        mod._REAP_APPLIED = None
    return results, [x for x in err.getvalue().splitlines() if "settings file" in x], len(passes)


class Reload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def scene(self, **over):
        s = Scene(self.tmp, **over)
        self.addCleanup(s.close)
        return s

    def test_s2_a_repository_added_between_passes_is_accepted_by_the_second_pass(self):
        s = self.scene()
        both = GOOD["CW_ALLOWED_REPOS"] + "," + NEW
        results, lines, passes = run_passes(s, [lambda: s.write(text(CW_ALLOWED_REPOS=both))])
        self.assertEqual(results[:1], ["refused"], "pass 1 holds the first values")
        self.assertEqual(results, ["refused", "accepted"], ANCHOR)

    def test_s3_an_unchanged_file_is_read_once_and_a_removed_setting_is_dropped(self):
        s = self.scene(CW_TOOLING_REPO="acme-org/tooling")
        self.assertEqual(s.env.get("CW_TOOLING_REPO"), "acme-org/tooling")
        s.reads = 0
        self.assertEqual(s.reload(), [])   # the first pass reads the file (the start's reading was before the counter)
        self.assertEqual(s.reads, 1)
        s.reload()
        s.reload()
        self.assertEqual(s.reads, 1, "an unchanged file (size, mtime, hash) is not read again")
        s.write(text())   # the tooling setting is removed
        s.reload()
        self.assertEqual(s.reads, 2)
        self.assertNotIn("CW_TOOLING_REPO", s.env)
        self.assertNotIn("CW_TOOLING_REPO", s.applied)
        self.assertEqual(s.env["CW_ALLOWED_REPOS"], GOOD["CW_ALLOWED_REPOS"])

    def test_s4_a_value_the_caller_set_itself_is_kept(self):
        s = self.scene()
        s.env["CW_MAX_PERSISTENT"] = "3"   # not in the file, not applied by the reaper: the caller's own
        s.write(text(CW_ALLOWED_REPOS=GOOD["CW_ALLOWED_REPOS"] + "," + NEW))
        self.assertEqual(s.reload(), [])
        self.assertEqual(s.env.get("CW_MAX_PERSISTENT"), "3")
        self.assertNotIn("CW_MAX_PERSISTENT", s.applied)
        self.assertIn(NEW, s.env["CW_ALLOWED_REPOS"])

    def test_s5_a_bad_file_keeps_the_old_values_one_line_per_reason(self):
        s = self.scene()
        good2 = text(CW_ALLOWED_REPOS=GOOD["CW_ALLOWED_REPOS"] + "," + NEW)
        s.write(text(), mode=0o644)   # pass 1: a mode that is too open
        results, lines, passes = run_passes(s, [lambda: s.write("this is not a setting line\n"), lambda: s.write("this is not a setting line\n"), lambda: s.write(good2)])
        self.assertEqual(passes, 4, "every pass runs")
        self.assertEqual(results, ["refused", "refused", "refused", "accepted"])
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertTrue(line.startswith("cloud-worker: settings file not reloaded: "), line)
            for value in list(GOOD.values()) + [NEW]:
                self.assertNotIn(value, line)
        self.assertIn("mode", lines[0])
        self.assertIn("not KEY=VALUE", lines[1])

    def test_s6_the_namespace_and_the_server_are_reported_and_kept(self):
        for name, value in (("CW_CLOUD_SERVER", "https://other.example.internal:6443"), ("CW_CLOUD_NAMESPACE", "cw-other")):
            s = self.scene() if name == "CW_CLOUD_SERVER" else Scene(tempfile.mkdtemp(dir=self.tmp))
            old = s.env[name]
            s.write(text(**{name: value, "CW_ALLOWED_REPOS": GOOD["CW_ALLOWED_REPOS"] + "," + NEW}))
            lines = s.reload()
            self.assertEqual(s.env[name], old, "kept until a restart")
            self.assertEqual(len(lines), 1, lines)
            self.assertIn(name, lines[0])
            self.assertNotIn(value, lines[0])
            self.assertIn(NEW, s.env["CW_ALLOWED_REPOS"], "the allow-list of the same file is applied")
            self.assertEqual(s.reload(), [], "the same file: no second line")
            s.close()


class Mutant(unittest.TestCase):
    def run_one(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "test_reaper_reload.Reload.test_s2_a_repository_added_between_passes_is_accepted_by_the_second_pass"],
                              cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)

    def test_s7_a_reaper_that_reads_the_file_only_at_its_start_fails_with_the_anchored_text(self):
        with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
            src = f.read()
        old = "            if _REAP_APPLIED is not None:   # the settings file is read again"
        self.assertEqual(src.count(old), 1)
        with tempfile.TemporaryDirectory() as tmp:
            control = os.path.join(tmp, "control.py")
            mutant = os.path.join(tmp, "mutant.py")
            with open(control, "w") as f:
                f.write(src)
            with open(mutant, "w") as f:
                f.write(src.replace(old, "            if False and _REAP_APPLIED is not None:   # the settings file is read again"))
            c = self.run_one(control)   # control first
            self.assertEqual(c.returncode, 0, c.stdout)
            self.assertIn("Ran 1 test", c.stdout)
            p = self.run_one(mutant)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(ANCHOR, p.stdout)


if __name__ == "__main__":
    unittest.main()

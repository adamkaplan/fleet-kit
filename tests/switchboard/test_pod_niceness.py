"""#1794: in a cloud worker pod `command_prefix` of bin/heavy_slots.py gives no niceness; on any other machine it gives the three answers it gave before."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.loader import SWITCHBOARD, sb
from support.paths import BIN

HEAVY = BIN / "heavy_slots.py"
POD = {"CW_AUDIT_RUNTIME_ID": "pod/x", "CW_HARD_DEADLINE": "2030-01-01T00:00:00Z"}
FOUND = (lambda n: "/x/taskpolicy" if n == "taskpolicy" else "/x/nice", lambda n: "/x/nice" if n == "nice" else None, lambda n: None)
KEPT = (["/x/taskpolicy", "-b"], ["/x/nice", "-n", "10"], [])
CHECK_TEXT = "the niceness was dropped without the pod marker"
KEPT_TEXT = "the niceness was kept in a pod"


def check_without_marker(module):
    """The niceness is kept on a fake environment without the marker: 3 of 3 answers as at the base."""
    got = [module.command_prefix(which, {}) for which in FOUND]
    if got != list(KEPT):
        raise AssertionError("%s: %r" % (CHECK_TEXT, got))


def check_in_pod(module):
    """The niceness is dropped on a fake environment with the marker: 3 of 3 empty prefixes."""
    got = [module.command_prefix(which, POD) for which in FOUND]
    if got != [[], [], []]:
        raise AssertionError("%s: %r" % (KEPT_TEXT, got))


def load(path):
    spec = importlib.util.spec_from_file_location("heavy_slots_under_test", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPodNiceness(unittest.TestCase):
    def setUp(self):
        self.hs = sb.load_heavy_slots()
        self.home = Path(tempfile.mkdtemp(prefix="podnice."))
        self.addCleanup(shutil.rmtree, str(self.home), True)

    def test_s2_the_pod_marker_gives_the_empty_prefix_and_its_absence_the_three_answers(self):
        self.assertEqual([self.hs.command_prefix(which, POD) for which in FOUND], [[], [], []])
        check_without_marker(self.hs)
        check_in_pod(self.hs)

    def test_s9_a_blank_or_zero_deadline_with_the_pod_id_is_the_marker(self):
        for deadline in (" ", "0"):
            env = {"CW_AUDIT_RUNTIME_ID": "pod/x", "CW_HARD_DEADLINE": deadline}
            self.assertEqual(self.hs.command_prefix(FOUND[1], env), [], "a non-empty deadline is the marker: %r gave a prefix" % deadline)

    def test_s3_a_part_of_the_marker_keeps_the_niceness(self):
        partial = ({"CW_AUDIT_RUNTIME_ID": "pod/x"}, {"CW_HARD_DEADLINE": "2030-01-01T00:00:00Z"},
                   {"CW_AUDIT_RUNTIME_ID": "", "CW_HARD_DEADLINE": "2030-01-01T00:00:00Z"},
                   {"CW_AUDIT_RUNTIME_ID": "x", "CW_HARD_DEADLINE": "2030-01-01T00:00:00Z"})
        kept = [env for env in partial if self.hs.command_prefix(FOUND[1], env) == ["/x/nice", "-n", "10"]]
        self.assertEqual(len(kept), 4, "4 of 4 environments keep the niceness, got %d" % len(kept))

    def run_mutant(self, source):
        copy = self.home / "heavy_slots.py"
        (copy.parent / "heavy_slots_watch.py").write_text((HEAVY.parent / "heavy_slots_watch.py").read_text())
        copy.write_text(source)
        script = "import sys; sys.path.insert(0, %r); import test_pod_niceness as t; t.check_without_marker(t.load(%r))" % (str(Path(__file__).parent), str(copy))
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=str(Path(__file__).parent))

    def test_s5_a_mutant_that_drops_the_niceness_everywhere_fails_and_the_control_passes(self):
        source = HEAVY.read_text()
        control = self.run_mutant(source)
        self.assertEqual(control.returncode, 0, control.stderr)
        marker = '    found = which("taskpolicy")\n'
        self.assertEqual(source.count(marker), 1)
        mutant = self.run_mutant(source.replace(marker, "    return []\n" + marker))
        self.assertEqual(mutant.returncode, 1)
        self.assertIn(CHECK_TEXT, mutant.stderr)

    def test_s10_a_mutant_that_never_drops_the_niceness_fails_and_the_control_passes(self):
        source = HEAVY.read_text()
        script = "import sys; sys.path.insert(0, %r); import test_pod_niceness as t; t.check_in_pod(t.load(%r))"
        def run(text):
            copy = self.home / "heavy_slots.py"
            (copy.parent / "heavy_slots_watch.py").write_text((HEAVY.parent / "heavy_slots_watch.py").read_text())
            copy.write_text(text)
            return subprocess.run([sys.executable, "-c", script % (str(Path(__file__).parent), str(copy))], capture_output=True, text=True, cwd=str(Path(__file__).parent))
        control = run(source)
        self.assertEqual(control.returncode, 0, control.stderr)
        check = '    if str(environ.get("CW_AUDIT_RUNTIME_ID", "")).startswith("pod/") and str(environ.get("CW_HARD_DEADLINE", "")) != "":\n        return []\n'
        self.assertEqual(source.count(check), 1)
        mutant = run(source.replace(check, ""))
        self.assertEqual(mutant.returncode, 1)
        self.assertIn(KEPT_TEXT, mutant.stderr)

    def wrapper(self, env_more):
        """Run the real wrapper on a child that prints its niceness and the slot record it can see; return (niceness, record count while running)."""
        env = dict(os.environ, XDG_STATE_HOME=str(self.home), PATH=os.environ["PATH"])
        for key in ("FLEET_HEAVY_SLOT", "FLEET_HEAVY", "CI", "CW_AUDIT_RUNTIME_ID", "CW_HARD_DEADLINE", sb.UNDER_TEST_ENV):   # the slot wrapper reads the daemon's own config by design: it is not a test, so the guard marker is not passed to it
            env.pop(key, None)
        env.update(env_more)
        stub = self.home / "nobin"
        stub.mkdir(exist_ok=True)
        for tool in ("nice", "sh", "ps", "ls", "grep"):
            found = shutil.which(tool)
            if found and not (stub / tool).exists():
                os.symlink(found, str(stub / tool))
        env["PATH"] = str(stub)
        out = self.home / "out"
        script = 'ps -o nice= -p $$ > "%s"; ls "%s/fleet-switchboard" | grep -c "heavy-slot-.*lock" >> "%s"' % (out, self.home, out)
        done = subprocess.run([sys.executable, str(SWITCHBOARD), "heavy", "--slots", "3", "--name", "t", "--", "sh", "-c", script],
                              env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(done.returncode, 0, done.stderr)
        nice, files = out.read_text().split()
        return int(nice), int(files)

    def test_u1_without_the_marker_the_wrapper_runs_the_child_niced(self):
        nice, _ = self.wrapper({})
        self.assertEqual(nice, min(20, os.nice(0) + 10))

    def test_u2_a_marker_set_by_hand_drops_the_niceness_and_nothing_else(self):
        nice, files = self.wrapper(POD)
        self.assertEqual(nice, os.nice(0), "the child runs at the wrapper's own niceness")
        self.assertGreaterEqual(files, 1, "the slot is taken during the run")
        self.assertEqual(self.hs.holders(self.home / "fleet-switchboard", 3), [], "and released after")


if __name__ == "__main__":
    unittest.main()

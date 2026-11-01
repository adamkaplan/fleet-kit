"""Ask #1774: the heavy wrapper reads the slot-lock holders from the process directory, so a pod without `lsof` says nothing extra. Neutral names (acme)."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
BIN = Path(os.environ.get("HEAVY_HOLDERS_BIN") or REPO / "bin")   # a scratch copy of bin/ can be tried (the mutant checks)
ANCHOR = "heavy: cannot list who holds the slot lock"


def load():
    spec = importlib.util.spec_from_file_location("heavy_slots", BIN / "heavy_slots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = ("import sys, runpy; sys.path.insert(0, %r); import heavy_slots as h; real = h.Watchdog.__init__; "
          "h.Watchdog.__init__ = lambda s, *a, **k: real(s, *a, **dict(k, every=0.2)); "
          "script = sys.argv[1]; sys.argv = [script] + sys.argv[2:]; runpy.run_path(script, run_name='__main__')")


class TestHeavyHolders(unittest.TestCase):
    def setUp(self):
        self.hs = load()
        self.tmp = Path(tempfile.mkdtemp(prefix="acme-holders-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fake_proc(self, lock):
        root = self.tmp / "proc"
        for pid, target in (("101", lock), ("102", lock), ("103", lock), ("104", lock), ("105", "/elsewhere")):
            fd = root / pid / "fd"
            fd.mkdir(parents=True)
            os.symlink("/dev/null", fd / "0")
            os.symlink(target, fd / "7")
        (root / "self").mkdir()
        (root / "notapid.txt").write_text("x")
        return root

    def no_lsof_path(self):
        bindir = self.tmp / "nolsof"
        bindir.mkdir(exist_ok=True)
        for tool in ("sh", "sleep", "echo", "ps"):
            found = shutil.which(tool)
            if found and not (bindir / tool).exists():
                os.symlink(found, bindir / tool)
        return str(bindir)

    def test_s2_the_pids_that_hold_the_path_without_lsof(self):
        lock = str(self.tmp / "heavy-slot-0.lock")
        root = self.fake_proc(lock)
        real_lstat, real_listdir, real_readlink = os.lstat, os.listdir, os.readlink

        def lstat(path, *a, **k):
            got = real_lstat(path, *a, **k)
            if str(path).endswith("/103"):   # another user's process
                fields = list(got)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return got

        def listdir(path="."):
            if str(path).endswith("/104/fd"):
                raise PermissionError(13, "refused")
            return real_listdir(path)

        def readlink(path, *a, **k):
            if str(path).endswith("/102/fd/0"):   # the entry vanishes under the read
                raise FileNotFoundError(2, "gone")
            return real_readlink(path, *a, **k)

        with mock.patch.dict(os.environ, {"PATH": self.no_lsof_path()}), mock.patch("subprocess.run") as run, mock.patch("os.lstat", lstat), \
                mock.patch("os.listdir", listdir), mock.patch("os.readlink", readlink):
            self.assertEqual(self.hs._proc_lock_pids(lock, str(root)), {101, 102})
            with mock.patch.object(self.hs, "PROC_ROOT", str(root)):
                self.assertEqual(self.hs.read_lock_holders(lock), {101, 102})
        self.assertEqual(run.call_count, 0)

    def lsof_dir(self, body):
        bindir = self.tmp / "withlsof"
        bindir.mkdir(exist_ok=True)
        (bindir / "lsof").write_text("#!/bin/sh\n" + body)
        (bindir / "lsof").chmod(0o755)
        return str(bindir)

    def test_s6_a_missing_process_directory_runs_lsof_as_before(self):
        gone = str(self.tmp / "no-proc")
        with mock.patch.object(self.hs, "PROC_ROOT", gone):
            with mock.patch.dict(os.environ, {"PATH": self.lsof_dir("echo 12; echo 34\n") + os.pathsep + "/usr/bin" + os.pathsep + "/bin"}):
                self.assertEqual(self.hs.read_lock_holders("/x/lock"), {12, 34})
            with mock.patch.dict(os.environ, {"PATH": self.lsof_dir("exit 1\n") + os.pathsep + "/usr/bin" + os.pathsep + "/bin"}):
                self.assertEqual(self.hs.read_lock_holders("/x/lock"), set())
            empty = self.tmp / "empty"
            empty.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}):
                with self.assertRaises(OSError) as caught:
                    self.hs.read_lock_holders("/x/lock")
        self.assertEqual(str(caught.exception), "lsof failed: [Errno 2] No such file or directory: 'lsof'")

    def test_s7_nothing_raises_and_an_unreadable_directory_falls_back_to_lsof(self):
        lock = str(self.tmp / "lock")
        root = self.fake_proc(lock)
        self.assertEqual(self.hs._proc_lock_pids(str(self.tmp / "missing-lock"), str(root)), set())
        self.assertIsNone(self.hs._proc_lock_pids(lock, str(self.tmp / "none")))
        with mock.patch("os.listdir", side_effect=PermissionError(13, "refused")):
            self.assertIsNone(self.hs._proc_lock_pids(lock, str(root)))
        for error in (ProcessLookupError, OSError, PermissionError):
            with mock.patch("os.readlink", side_effect=error(1, "x")):
                self.assertEqual(self.hs._proc_lock_pids(lock, str(root)), set())
        with mock.patch.object(self.hs, "PROC_ROOT", str(root)), mock.patch("os.listdir", side_effect=PermissionError(13, "refused")), \
                mock.patch.dict(os.environ, {"PATH": self.lsof_dir("echo 77\n") + os.pathsep + "/usr/bin" + os.pathsep + "/bin"}):
            self.assertEqual(self.hs.read_lock_holders(lock), {77})

    def run_wrapper(self, name, stranger):
        """One wrapper run in a world without lsof; returns (merged text and status, stdout, stderr and status of a second, separate run's streams)."""
        state = self.tmp / name
        (state / "fleet-switchboard").mkdir(parents=True, mode=0o700)
        lock = state / "fleet-switchboard" / "heavy-slot-0.lock"
        held = None
        if stranger:   # a process outside the command's tree that has the slot lock open
            lock.touch(mode=0o600)
            held = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); print('up', flush=True); time.sleep(60)", str(lock)],
                                    stdout=subprocess.PIPE, start_new_session=True)
            self.addCleanup(held.kill)
            self.assertEqual(held.stdout.readline().strip(), b"up")
        env = {k: v for k, v in os.environ.items() if k not in ("FLEET_HEAVY_SLOT", "FLEET_HEAVY", "CI")}
        env.update(XDG_STATE_HOME=str(state), PATH=self.no_lsof_path(), FLEET_HEAVY_MEM_MB="100000", FLEET_HEAVY_REPORT_SECONDS="0.3")
        argv = [sys.executable, "-c", RUNNER % str(BIN), str(BIN / "fleet-switchboard"), "heavy", "--slots", "1", "--name", "acme", "--",
                "sh", "-c", "echo out-line; echo err-line >&2; sleep 1.5; echo done-line"]
        merged = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, stdin=subprocess.DEVNULL)
        split = subprocess.run(argv, env=env, capture_output=True, timeout=90, stdin=subprocess.DEVNULL)
        return (merged.returncode, merged.stdout), (split.returncode, split.stdout, split.stderr)

    def test_s3_the_text_is_the_same_with_the_slot_free_and_held(self):
        free = self.run_wrapper("free", False)
        held = self.run_wrapper("held", True)
        for label, got in (("free", free), ("held", held)):
            for text in (got[0][1], got[1][2]):
                lines = [l for l in text.decode("utf-8", "replace").splitlines() if ANCHOR in l]
                if lines:
                    self.fail("slot %s: %d line(s): %s" % (label, len(lines), "; ".join(lines)))
        self.assertIn(b"out-line", free[0][1])
        self.assertIn(b"done-line", free[0][1])
        self.assertEqual(free[0][1].count(b"out-line"), 1)
        self.assertEqual(held[1][1], free[1][1], "standard output differs")
        self.assertEqual(held[1][2], free[1][2], "standard error differs")
        self.assertEqual(held[0][1], free[0][1], "the merged text differs")
        self.assertEqual((held[0][0], held[1][0]), (free[0][0], free[1][0]), "exit statuses differ")


if __name__ == "__main__":
    unittest.main()

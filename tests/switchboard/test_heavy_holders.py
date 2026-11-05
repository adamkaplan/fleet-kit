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


RUNNER = ("import sys, runpy; sys.path.insert(0, %r); import heavy_slots as h; real = h.Watchdog.__init__; h.PROC_ROOT = __import__('os').environ.get('ACME_PROC_ROOT', h.PROC_ROOT); "
          "h.Watchdog.__init__ = lambda s, *a, **k: real(s, *a, **dict(k, every=0.2)); "
          "script = sys.argv[1]; sys.argv = [script] + sys.argv[2:]; runpy.run_path(script, run_name='__main__')")


class TestHeavyHolders(unittest.TestCase):
    def setUp(self):
        self.hs = load()
        self.tmp = Path(tempfile.mkdtemp(prefix="acme-holders-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fake_proc(self, lock):
        root = self.tmp / "proc"
        if lock.startswith(str(self.tmp)) and not os.path.lexists(lock):
            Path(lock).write_text("")   # the lock is matched by device and inode: it must exist
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
        real_lstat, real_listdir, real_stat = os.lstat, os.listdir, os.stat

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

        def stat(path, *a, **k):
            if str(path).endswith("/102/fd/0"):   # the entry vanishes under the read
                raise FileNotFoundError(2, "gone")
            return real_stat(path, *a, **k)

        with mock.patch.dict(os.environ, {"PATH": self.no_lsof_path()}), mock.patch("subprocess.run") as run, mock.patch("os.lstat", lstat), \
                mock.patch("os.listdir", listdir), mock.patch("os.stat", stat):
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
            real = os.stat
            with mock.patch("os.stat", lambda p, *a, e=error, **k: real(p, *a, **k) if "/fd/" not in str(p) else (_ for _ in ()).throw(e(1, "x"))):
                self.assertEqual(self.hs._proc_lock_pids(lock, str(root)), set())
        with mock.patch.object(self.hs, "PROC_ROOT", str(root)), mock.patch("os.listdir", side_effect=PermissionError(13, "refused")), \
                mock.patch.dict(os.environ, {"PATH": self.lsof_dir("echo 77\n") + os.pathsep + "/usr/bin" + os.pathsep + "/bin"}):
            self.assertEqual(self.hs.read_lock_holders(lock), {77})

    def entries(self, target, pids=("201", "202")):
        root = self.tmp / "proc2"
        for pid in pids:
            (root / pid / "fd").mkdir(parents=True)
            os.symlink(target, root / pid / "fd" / "7")
        return root

    def test_a_symlinked_lock_path_finds_the_holders(self):
        real = self.tmp / "real" / "lock"
        real.parent.mkdir()
        real.write_text("")
        link = self.tmp / "linked-lock"
        os.symlink(real, link)
        root = self.entries(real)
        self.assertNotEqual(str(link), str(real))
        self.assertEqual(self.hs._proc_lock_pids(str(link), str(root)), {201, 202})

    def test_b_a_hard_link_or_another_path_of_the_same_file(self):
        real = self.tmp / "lock"
        real.write_text("")
        hard = self.tmp / "hard-lock"
        os.link(real, hard)
        root = self.entries(real)
        self.assertEqual(self.hs._proc_lock_pids(str(hard), str(root)), {201, 202})
        self.assertEqual(self.hs._proc_lock_pids(str(self.tmp / "." / "lock"), str(root)), {201, 202})

    def test_c_mutant_the_string_compare_fails_a(self):
        scratch = self.tmp / "scratch-bin"
        shutil.copytree(BIN, scratch)
        text = (scratch / "heavy_slots.py").read_text()
        new = text.replace("if (seen.st_dev, seen.st_ino) == want:", "if os.readlink(os.path.join(root, name, 'fd', fd)) == str(path):")
        self.assertNotEqual(new, text)
        script = ("import os,sys,unittest;os.environ['HEAVY_HOLDERS_BIN']=sys.argv[1];sys.path.insert(0,sys.argv[2]);"
                  "import test_heavy_holders as t;s=unittest.defaultTestLoader.loadTestsFromName('TestHeavyHolders.test_a_symlinked_lock_path_finds_the_holders',t);"
                  "r=unittest.TextTestRunner(verbosity=0).run(s);sys.exit(0 if r.wasSuccessful() else 1)")
        here = str(Path(__file__).parent)

        def run(bindir):
            return subprocess.run([sys.executable, "-c", script, str(bindir), here], capture_output=True, text=True)
        control = run(scratch)   # the unmutated copy passes first
        self.assertEqual(control.returncode, 0, control.stderr)
        (scratch / "heavy_slots.py").write_text(new)
        mutant = run(scratch)
        self.assertEqual(mutant.returncode, 1)
        self.assertIn("AssertionError: Items in the second set but not the first", mutant.stderr)

    def test_d_the_entries_still_skipped(self):
        lock = self.tmp / "lock"
        lock.write_text("")
        other = self.tmp / "other"
        other.write_text("")
        root = self.entries(lock, ("301", "302", "303", "304", "305"))
        os.unlink(root / "302" / "fd" / "7")
        os.symlink(self.tmp / "gone-file", root / "302" / "fd" / "7")   # vanished: dangling
        os.unlink(root / "304" / "fd" / "7")
        os.symlink(other, root / "304" / "fd" / "7")   # unrelated file, other inode
        real_lstat, real_listdir = os.lstat, os.listdir

        def lstat(path, *a, **k):
            got = real_lstat(path, *a, **k)
            if str(path).endswith("/303"):   # another user
                fields = list(got)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return got

        def listdir(path="."):
            if str(path).endswith("/305/fd"):
                raise PermissionError(13, "refused")
            return real_listdir(path)
        with mock.patch("os.lstat", lstat), mock.patch("os.listdir", listdir):
            self.assertEqual(self.hs._proc_lock_pids(str(lock), str(root)), {301})

    def test_e_a_lock_present_while_held_is_found(self):
        lock = self.tmp / "lock"
        lock.write_text("")
        root = self.tmp / "proc3"
        (root / "401" / "fd").mkdir(parents=True)
        # the fake fd link points directly at the lock: the stat follows it to the lock itself, on any platform
        os.symlink(str(lock), root / "401" / "fd" / "7")
        self.assertEqual(self.hs._proc_lock_pids(str(lock), str(root)), {401})

    def test_e2_a_lock_deleted_while_held_is_not_found(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no process directory (/proc/self/fd): the macOS holder read is the lsof route, covered by test_s6/test_s7")
        lock = self.tmp / "lock"
        lock.write_text("")
        root = self.tmp / "proc3"
        (root / "401" / "fd").mkdir(parents=True)
        # a symlink cannot model a deleted lock (on macOS /dev/fd/N has another st_dev), so this needs a real process directory
        with open(lock) as held:
            os.symlink("/proc/self/fd/%d" % held.fileno(), root / "401" / "fd" / "7")
            os.unlink(lock)
            # the fd link still stats the open inode, but the path stat fails, so nothing is compared: an empty set
            self.assertEqual(self.hs._proc_lock_pids(str(lock), str(root)), set())

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
        fake = state / "proc"   # a fake process directory, so the proc route is taken on any platform (a Mac has no /proc)
        (fake / "4242" / "fd").mkdir(parents=True)
        if stranger:
            os.symlink(str(lock), fake / "4242" / "fd" / "3")
        env.update(ACME_PROC_ROOT=str(fake), XDG_STATE_HOME=str(state), XDG_CONFIG_HOME=str(state / "config"), PATH=self.no_lsof_path(), FLEET_HEAVY_MEM_MB="100000", FLEET_HEAVY_REPORT_SECONDS="0.3")
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

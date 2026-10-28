"""The scratch directory (ask #1456): `fleet-switchboard scratch`, the guard in every removing command, the 100 MB warn-only bound.

Each guard has a mutant: a patched build that sweeps (or lets a removal through) and the same check, which must then fail. Names are acme-org names."""
import contextlib
import hashlib
import io
import os
import shutil
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.core import Base   # noqa: E402
from support.loader import REPO, sb   # noqa: E402

scratch_dir = sb.load_module("scratch_dir")
janitor = sb.load_disk_janitor()
MB = 1024 * 1024


def tree_hash(root):
    """One hash of every path and content under `root`; a missing root is its own value."""
    digest = hashlib.sha256()
    if not os.path.exists(str(root)):
        return "missing"
    for base, dirs, files in os.walk(str(root)):
        dirs.sort()
        for name in sorted(files + dirs):
            path = os.path.join(base, name)
            digest.update(path.encode())
            if os.path.isfile(path):
                digest.update(Path(path).read_bytes())
    return digest.hexdigest()


class ScratchCase(Base):
    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.engine = types.SimpleNamespace(state_dir=str(self.state_dir), herdr=None, v2=None, audit=None, actor="switchboard", config={"temp_janitor": {"enabled": False}})   # the daemon source returns right after its scratch look
        self.who = "acme-lane"

    def call(self, *argv, who=None):
        out, err = io.StringIO(), io.StringIO()
        name = who or self.who
        with mock.patch.object(sb, "build_engine", lambda *a, **k: (self.engine, None, {})), mock.patch.object(sb, "identify_caller", lambda *a: name), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sb.main(["scratch", *argv])
        return code, out.getvalue(), err.getvalue()

    def plant(self, name="acme-lane", content="helper"):
        directory = self.state_dir / "scratch" / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "merge.sh").write_text(content)
        return directory


class TestScratchCommand(ScratchCase):
    def test_s2_it_prints_one_path_and_makes_the_directory_private(self):
        code, out, _ = self.call()
        self.assertEqual((code, len(out.splitlines())), (0, 1))
        path = Path(out.strip())
        self.assertEqual(path, self.state_dir / "scratch" / "acme-lane")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o700")

    def test_s2_mutant_a_default_mode_directory_fails_the_mode_check(self):
        old, real_mkdir = os.umask(0), os.mkdir
        try:
            with mock.patch.object(os, "fchmod", lambda fd, mode: None), mock.patch.object(os, "mkdir", lambda p, mode=0o777: real_mkdir(p, 0o755)):
                _, out, _ = self.call()
        finally:
            os.umask(old)
        self.assertNotEqual(oct(Path(out.strip()).stat().st_mode & 0o777), "0o700")

    def test_s3_a_symlink_another_owner_and_a_bad_name_are_each_refused_with_one_line(self):
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        base = self.state_dir / "scratch"
        base.mkdir(mode=0o700)
        (base / "acme-lane").symlink_to(outside)
        before = tree_hash(outside)
        code, out, err = self.call()
        self.assertEqual((code, out, len(err.splitlines())), (2, "", 1))
        self.assertEqual(tree_hash(outside), before)
        (base / "acme-lane").unlink()
        (base / "acme-lane").mkdir(mode=0o700)
        with mock.patch.object(os, "getuid", lambda: os.stat(str(base)).st_uid + 1):
            code, out, err = self.call()
        self.assertEqual((code, out, len(err.splitlines())), (2, "", 1))
        for bad in ("a/b", ".hidden", "../x"):
            with self.subTest(name=bad):
                code, out, err = self.call(who=bad)
                self.assertEqual((code, out, len(err.splitlines())), (2, "", 1))
        self.assertEqual(sorted(os.listdir(str(base))), ["acme-lane"])

    def test_s3_mutant_a_build_that_follows_the_link_is_caught(self):
        outside = self.home / "outside"
        outside.mkdir()
        base = self.state_dir / "scratch"
        base.mkdir(mode=0o700)
        (base / "acme-lane").symlink_to(outside)
        os.chmod(str(outside), 0o700)
        with mock.patch.object(scratch_dir, "_private_dir", lambda path: None):
            code, out, _ = self.call()
        self.assertEqual(code, 0)   # the mutant exits 0 where the real build exits 2: the test above fails it

    def test_s6_s8_an_argument_naming_a_path_or_another_agent_is_refused_and_prints_no_path(self):
        other = self.plant("other-lane")
        before = tree_hash(other)
        for arg in ("../x", "/tmp", "other-lane"):
            with self.subTest(arg=arg):
                code, out, _ = self.call(arg)
                self.assertEqual((code, out), (2, ""))
        self.assertEqual(tree_hash(other), before)

    def test_s6_the_documentation_says_a_scratch_file_is_not_a_place_for_a_secret(self):
        self.assertEqual((REPO / "docs" / "switchboard.md").read_text().count("not a place for a secret"), 1)

    def test_s9_a_new_directory_is_0700_with_a_file_written_in_it(self):
        _, out, _ = self.call()
        path = Path(out.strip())
        (path / "note.txt").write_text("a rule text")
        self.assertEqual((oct(path.stat().st_mode & 0o777), (path / "note.txt").read_text()), ("0o700", "a rule text"))

    def test_s10_over_the_bound_one_warning_exit_0_and_every_file_stays(self):
        _, out, _ = self.call()
        path = Path(out.strip())
        with open(str(path / "big.bin"), "wb") as handle:
            handle.truncate(99 * MB)
        code, out, err = self.call()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(os.listdir(str(path)), ["big.bin"])
        with open(str(path / "big.bin"), "wb") as handle:
            handle.truncate(101 * MB)
        code, out, err = self.call()
        self.assertEqual((code, len(err.splitlines()), "100 MB" in err, "101 MB" in err), (0, 1, True, True))
        self.assertEqual((os.listdir(str(path)), len(out.splitlines())), (["big.bin"], 1))

    def test_s10_mutant_a_build_with_no_warning_prints_none_at_101_mb(self):
        _, out, _ = self.call()
        with open(str(Path(out.strip()) / "big.bin"), "wb") as handle:
            handle.truncate(101 * MB)
        with mock.patch.object(scratch_dir, "BOUND_BYTES", 10 ** 12):
            _, _, err = self.call()
        self.assertEqual(err, "")   # the test above needs one line: this build would fail it


class TestNoRemovingCommandSweeps(ScratchCase):
    def setUp(self):
        super().setUp()
        self.keep = self.plant()
        (self.state_dir / "reminders.json").write_text("{}")   # a state directory with at least one record: with none the janitor judges nothing
        self.audit_lines = []
        old = time.time() - 40 * 86400
        for base, dirs, files in os.walk(str(self.state_dir)):
            for name in dirs + files:
                os.utime(os.path.join(base, name), (old, old))
        os.utime(str(self.state_dir), (old, old))

    def run_janitor(self):
        temp = self.state_dir.parent
        os.chmod(str(temp), 0o700)
        cfg = janitor.settings({"days": 1})
        return janitor.run(temp, str(self.state_dir), time.time() + 40 * 86400, cfg, True, lambda name, detail: self.audit_lines.append(name))

    def run_worktree(self):
        runner = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="no git")
        return sb.reclaim_worktree(self.engine, runner, str(self.keep), {"agent": "acme-lane"}, True)

    def run_rotation(self):
        log = sb.AuditLog(self.state_dir / "audit.jsonl")
        log.path.write_text("x" * 10)
        log.rotate(time.time())

    def run_daemon_pass(self):
        with open(str(self.keep / "big.bin"), "wb") as handle:
            handle.truncate(101 * MB)
        self.engine.temp_janitor_runner = object()
        self.engine.now = time.time
        self.engine._note = lambda *a, **k: None
        source = sb.TempJanitorFacts()
        source.bind(self.engine)
        source()

    def test_s4_each_removing_command_leaves_the_scratch_file_unchanged(self):
        for label, run in (("janitor", self.run_janitor), ("worktree", self.run_worktree), ("rotation", self.run_rotation), ("daemon", self.run_daemon_pass)):
            with self.subTest(command=label):
                before = tree_hash(self.keep)
                run()
                if label == "daemon":
                    os.unlink(str(self.keep / "big.bin"))
                    before = tree_hash(self.keep)
                self.assertEqual(tree_hash(self.keep), before)
                self.assertEqual((self.keep / "merge.sh").read_text(), "helper")

    def test_s4_the_janitor_pass_really_ran_and_the_worktree_was_refused(self):
        lines, removed, kept = self.run_janitor()
        self.assertEqual(removed, 0)
        self.assertTrue(any("scratch" in line for line in lines), lines)
        self.assertEqual(self.run_worktree()[0], "refused")

    def test_s5_mutants_that_sweep_fail_the_same_check(self):
        with mock.patch.object(janitor, "scratch_covers", lambda path, state: False):
            for _ in range(8):   # a re-check can find a change time one tick newer and keep the entry: ask again
                if self.run_janitor()[1]:
                    break
            self.assertFalse((self.keep / "merge.sh").exists(), "the janitor mutant must sweep the file for this test to fail it")
        self.keep = self.plant()
        with mock.patch.object(janitor, "scratch_covers", lambda path, state: False):
            self.assertNotEqual(self.run_worktree()[0], "refused")
        for target, run in (("rotate", self.run_rotation), ("__call__", self.run_daemon_pass)):
            with self.subTest(mutant=target):
                self.keep = self.plant()
                original = sb.AuditLog.rotate if target == "rotate" else sb.TempJanitorFacts.__call__
                def sweeping(*a, **k):
                    shutil.rmtree(str(self.state_dir / "scratch"))
                    return original(*a, **k)
                owner = sb.AuditLog if target == "rotate" else sb.TempJanitorFacts
                with mock.patch.object(owner, target, sweeping):
                    run()
                self.assertFalse((self.keep / "merge.sh").exists())

    def test_u1_everything_that_is_not_scratch_is_still_removed(self):
        temp = self.state_dir.parent
        (temp / "old-cache-dir").mkdir()
        (temp / "old-cache-dir" / "f").write_text("x")
        old = time.time() - 40 * 86400
        for path in (temp / "old-cache-dir" / "f", temp / "old-cache-dir"):
            os.utime(str(path), (old, old))
        shutil.rmtree(str(self.state_dir / "scratch"))   # the state directory is a plain old entry now
        self.keep = self.plant()
        for _ in range(8):   # a re-check can find a change time one tick newer and keep the entry: ask again
            _, removed, _ = self.run_janitor()
            if removed:
                break
        self.assertGreaterEqual(removed, 1)
        self.assertFalse((temp / "old-cache-dir").exists())
        self.assertTrue((self.keep / "merge.sh").exists())


class TestOverBoundAudit(ScratchCase):
    def audit_engine(self):
        lines = []
        audit = types.SimpleNamespace(append=lambda actor, event, subject=None, detail=None, **k: lines.append((event, subject)))
        self.engine.audit, self.engine.now = audit, lambda: self.clock
        self.engine.temp_janitor_runner, self.engine._note = object(), lambda *a, **k: None
        return lines

    def test_s11_two_passes_the_same_day_record_one_line_and_remove_nothing(self):
        keep = self.plant()
        with open(str(keep / "big.bin"), "wb") as handle:
            handle.truncate(101 * MB)
        self.clock = 1_800_000_000.0
        lines = self.audit_engine()
        source = sb.TempJanitorFacts()
        source.bind(self.engine)
        source()
        self.engine.scratch_last = None   # the second pass is a later look, the same day
        self.clock += 3600
        source()
        self.assertEqual(lines, [("scratch.over_bound", "acme-lane")])
        self.assertEqual(sorted(os.listdir(str(keep))), ["big.bin", "merge.sh"])
        self.clock += 86400   # still over the next day: one more line
        self.engine.scratch_last = None
        source()
        self.assertEqual(len(lines), 2)

    def test_s11_mutant_a_daemon_that_writes_every_pass_records_two_lines(self):
        keep = self.plant()
        with open(str(keep / "big.bin"), "wb") as handle:
            handle.truncate(101 * MB)
        self.clock = 1_800_000_000.0
        lines = self.audit_engine()
        source = sb.TempJanitorFacts()
        source.bind(self.engine)
        with mock.patch.object(scratch_dir, "due_today", lambda state, now: scratch_dir.over_bound(state)):
            source()
            self.engine.scratch_last = None
            source()
        self.assertEqual(len(lines), 2)   # the test above asserts 1: this build fails it

    def test_s11_a_directory_under_the_bound_records_nothing(self):
        self.plant()
        self.clock = 1_800_000_000.0
        lines = self.audit_engine()
        source = sb.TempJanitorFacts()
        source.bind(self.engine)
        source()
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()

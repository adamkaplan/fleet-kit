"""#1737: `disk reclaim-worktrees` counts REMOVALS against its cap, and a Rust `target/` directory is build output.

build() gives the tests the fixtures of bin/test-switchboard (HOST is that module, set by its loader line). The mutant is the real function source with
one expression replaced, run from a copy of the module's globals; production is never edited. Every worktree is a temporary repository.
"""
import argparse
import inspect
import os
from pathlib import Path
from unittest import mock

from support.loader import captured, sb

HOST = None   # set by bin/test-switchboard
KEPT, REMOVABLE = 17, 30
OLD = 'sum(r[0] in ("removed", "would-remove") for r in results) >= WORKTREE_RECLAIM_MAX'
NEW = 'len(results) >= WORKTREE_RECLAIM_MAX'
GUARD = 'not any(os.path.islink(os.path.join(path, *q.split("/")[:i + 1])) for i in range(len(q.split("/"))))'   # the symlink guard of the clean step
LEAVE = 'os.path.realpath(os.path.join(path, q)).startswith(root + os.sep)'   # and its guard against a path that leaves the worktree


def build():
    host = HOST

    class TestReclaimCap(host.TestLaunch):
        """The inherited tests are switched off below: only the tests of this file run."""

        def rig(self):
            """A retired agent, a Chief of Staff pane that may apply, and one repository with its remote."""
            self.write_config({"opencode": host.FAKE_OPENCODE, "herdr": host.FAKE_HERDR})
            self.make_repo()
            self.worker()
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            host.set_retired(self.world.state, {"worker": {"at": int(self.world.clock.now()), "by": "x", "reason": "y", "session": "s", "auto": False}})
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]

        def mine(self, name, **kinds):
            path = self.add_worktree(name, **kinds)
            sb.register_worktree(self.world.state, str(path), "worker", None, name, 1)
            return path

        def cap_fixture(self):
            self.rig()
            kept = [self.mine("a-kept%02d" % n, dirty=True) for n in range(KEPT)]      # sort first
            gone = [self.mine("w-rm%02d" % n) for n in range(REMOVABLE)]
            return kept, gone

        def cap_check(self, apply_run):
            """Two runs with the cap of 20: the first removes 20, the second the next 10, the 17 kept ones stay."""
            kept, removable = self.cap_fixture()
            args = argparse.Namespace(apply=True)
            counts = []
            for _ in range(2):
                with captured():
                    apply_run(args, self.world.route, self.world.clock.now)
                counts.append(len([p for p in removable if not p.exists()]))
            self.assertEqual(counts[0], 20, msg="first run removes 20")
            self.assertEqual(counts[1] - counts[0], 10, msg="second run removes the next 10")
            self.assertEqual(len([p for p in kept if p.exists()]), KEPT, msg="the kept ones are all still present")

        def test_a_control_the_real_command_removes_20_then_the_next_10_and_keeps_the_17(self):
            self.cap_check(sb.cmd_disk_reclaim_worktrees)

        def test_b_a_mutant_that_counts_kept_ones_against_the_cap_fails_on_the_first_count(self):
            source = inspect.getsource(sb.cmd_disk_reclaim_worktrees)
            self.assertEqual(source.count(OLD), 1, msg="the expression the mutant replaces is in the function once")
            space = dict(vars(sb))
            exec(source.replace(OLD, NEW), space)
            self.assertIn(NEW, inspect.getsource(sb.cmd_disk_reclaim_worktrees).replace(OLD, NEW), msg="the mutation was applied")
            with self.assertRaisesRegex(AssertionError, r"^3 != 20 : first run removes 20"):
                self.cap_check(space["cmd_disk_reclaim_worktrees"])

        def test_c_a_worktree_with_only_target_files_is_removed_and_one_with_a_stray_file_is_kept_with_its_reason(self):
            self.rig()
            build_out = self.mine("w-target")
            (build_out / "target" / "debug").mkdir(parents=True)
            (build_out / "target" / "debug" / "app").write_text("bin\n")
            stray = self.mine("w-stray")
            (stray / "notes.txt").write_text("keep me\n")
            code, out, err = self.run_cli("disk", "reclaim-worktrees", "--apply")
            self.assertEqual(code, 0, err)
            self.assertFalse(build_out.exists(), msg="the target/ worktree is removed")
            self.assertTrue(stray.exists(), msg="the stray file keeps the worktree")
            self.assertTrue([l for l in out.splitlines() if "kept" in l and "notes.txt" in l], msg=out)

        def test_d_a_target_directory_that_is_ignored_by_gitignore_is_removed_as_well(self):
            self.rig()
            (self.repo_main / ".gitignore").write_text("target/\n")
            self.git(self.repo_main, "add", ".gitignore")
            self.git(self.repo_main, "commit", "-qm", "ignore target")
            self.git(self.repo_main, "push", "-q", "origin", "main")
            ignored = self.mine("w-ign")
            (ignored / "target").mkdir()
            (ignored / "target" / "app").write_text("bin\n")
            self.assertTrue([l for l in self.git(ignored, "status", "--porcelain", "--ignored").splitlines() if l.startswith("!! ")], msg="the fixture really ignores target/")
            code, out, err = self.run_cli("disk", "reclaim-worktrees", "--apply")
            self.assertEqual(code, 0, err)
            self.assertFalse(ignored.exists(), msg=out)

        def test_e_the_dry_run_has_one_line_per_kept_worktree_and_ends_with_the_counts(self):
            self.rig()
            self.mine("w-d1", dirty=True)
            self.mine("w-d2", dirty=True)
            self.mine("w-u1", unpushed=True)
            stray = self.mine("w-s1")
            (stray / "notes.txt").write_text("x\n")
            self.mine("w-ok1")
            self.mine("w-ok2")
            code, out, err = self.run_cli("disk", "reclaim-worktrees")
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertEqual(len([l for l in lines if ": kept: " in l]), 4, msg=out)
            self.assertIn("2 removable", lines[-1])
            self.assertIn("4 kept", lines[-1])
            self.assertIn("uncommitted changes", lines[-1], msg="the kept ones are counted by reason")
            self.assertIn("0 not reached", lines[-1])

        def test_f_nothing_outside_the_registered_worktrees_is_touched_and_a_main_checkout_is_refused(self):
            self.rig()
            unregistered = self.add_worktree("w-unregistered")
            inside = self.mine("w-inside")
            sb.register_worktree(self.world.state, str(self.repo_main), "worker", None, "main", 1)
            code, out, err = self.run_cli("disk", "reclaim-worktrees", "--apply")
            self.assertEqual(code, 0, err)
            self.assertFalse(inside.exists())
            self.assertTrue(unregistered.exists(), msg="an unregistered worktree is not touched")
            self.assertTrue(self.repo_main.exists() and (self.repo_main / ".git").exists(), msg="the main checkout is not touched")
            self.assertTrue([l for l in out.splitlines() if str(self.repo_main) in l and "refused" in l], msg=out)

        def keep_check(self):
            """S10 and S11: a modified tracked file or a notes.txt keeps the worktree whole; a symlinked target/ is kept and the linked directory is untouched."""
            self.rig()
            tracked = self.mine("w-tracked")
            (tracked / "a.txt").write_text("changed\n")
            (tracked / "target").mkdir()
            (tracked / "target" / "app").write_text("bin\n")
            stray = self.mine("w-stray2")
            (stray / "notes.txt").write_text("keep me\n")
            (stray / "target").mkdir()
            (stray / "target" / "app").write_text("bin\n")
            outside = Path(os.path.realpath(str(self.home))) / "linked"
            outside.mkdir()
            (outside / "precious.txt").write_text("mine\n")
            linked = self.mine("w-link")
            os.symlink(str(outside), str(linked / "target"))
            code, out, err = self.run_cli("disk", "reclaim-worktrees", "--apply")
            self.assertEqual(code, 0, err)
            self.assertEqual((tracked / "a.txt").read_text() + (tracked / "target" / "app").read_text(), "changed\nbin\n", msg="a tracked change keeps the worktree whole")
            self.assertTrue((stray / "notes.txt").exists() and (stray / "target" / "app").exists(), msg="a stray file keeps the worktree whole")
            self.assertTrue((outside / "precious.txt").exists(), msg="the linked directory is untouched")
            self.assertTrue(linked.exists(), msg="symlinked target/ keeps the worktree")

        def test_g_control_a_modified_tracked_file_a_stray_file_and_a_symlinked_target_keep_their_worktrees_whole(self):
            self.keep_check()

        def test_h_a_mutant_without_the_symlink_guard_fails_on_the_symlink_count(self):
            source = inspect.getsource(sb.reclaim_worktree)
            self.assertEqual(source.count(GUARD), 1, msg="the guard the mutant replaces is in the function once")
            self.assertEqual(source.count(LEAVE), 1, msg="the second guard the mutant replaces is in the function once")
            mutant = source.replace(GUARD, "True").replace(LEAVE, "True")
            self.assertNotEqual(mutant, source, msg="the mutation was applied")
            space = dict(vars(sb))
            exec(mutant, space)
            with mock.patch.object(sb, "reclaim_worktree", space["reclaim_worktree"]):
                with self.assertRaisesRegex(AssertionError, r"^False is not true : symlinked target/ keeps the worktree"):
                    self.keep_check()

    for name in dir(host.TestLaunch):
        if name.startswith("test") and name not in vars(TestReclaimCap):
            setattr(TestReclaimCap, name, None)
    return TestReclaimCap

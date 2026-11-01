"""The tests of `fleet-switchboard scratch` (#1456): the caller's own scratch directory, the refusals, the guard of every removing command, the 100 MB bound.
Three classes, each loaded by one line of bin/test-switchboard. Each mutant is a build with one change; its control is the unchanged test passing."""
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from unittest import mock
from support.core import Base
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE
from support.herdr_world import World

MB = 1024 * 1024


class ScratchWorld(Base):
    NAME = "acme-lane"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.base = self.world.state / "scratch"

    def scratch(self, *argv, name=None):
        who = self.NAME if name is None else name
        with mock.patch.object(sb, "identify_caller", lambda *a, **k: who), captured() as (out, err):
            code = sb.main(["scratch"] + list(argv), runner=self.world.route, now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def private(self, path):
        """The directory and its parent under the state directory, 0700 (a mkdir takes the umask)."""
        path.mkdir(parents=True, exist_ok=True)
        for item in (path, path.parent):
            os.chmod(str(item), 0o700)
        return path

    def fill(self, megabytes, name=None):
        path = self.private(self.base / (name or self.NAME))
        with open(str(path / "big.bin"), "wb") as handle:
            handle.truncate(megabytes * MB)
        return path


class TestScratchCommand(ScratchWorld):
    """S2, S3, S6, S8, S9, S10, S12."""

    def test_s2_prints_one_path_and_makes_the_directory_private(self):
        code, out, err = self.scratch()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.split()), 1, out)
        path = Path(out.strip())
        self.assertEqual(path.name, "acme-lane")
        self.assertEqual(path, self.base / "acme-lane")
        self.assertEqual(stat.S_IMODE(os.lstat(str(path)).st_mode), 0o700)

    def test_s3_a_symlink_another_owner_and_a_bad_name_are_refused(self):
        elsewhere = self.world.state / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("x")
        self.base.mkdir(mode=0o700)
        os.symlink(str(elsewhere), str(self.base / "acme-lane"))
        code, out, err = self.scratch()
        self.assertEqual((code, out), (2, ""), "a symlink was accepted")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertEqual(sorted(os.listdir(str(elsewhere))), ["keep"])
        os.unlink(str(self.base / "acme-lane"))
        self.scratch(name="acme-lane")
        real = os.lstat

        def other_owner(path, *a, **k):   # the one directory reads as another user's
            info = real(path, *a, **k)
            return os.stat_result(tuple(info)[:4] + (info.st_uid + 1,) + tuple(info)[5:]) if str(path).endswith("/acme-lane") else info
        with mock.patch.object(os, "lstat", other_owner):   # the directory is now another user's
            code, out, err = self.scratch()
        self.assertEqual((code, out), (2, ""), "another owner was accepted")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        for bad in ("a/b", ".hidden", "../x"):
            code, out, err = self.scratch(name=bad)
            self.assertEqual((code, out), (2, ""), "the name %r was accepted" % bad)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertEqual(sorted(os.listdir(str(self.base))), ["acme-lane"], "a directory was made for a bad name")

    def check_arguments_refused(self):
        for argument in ("../x", "/tmp", "other-lane"):
            code, out, err = self.scratch(argument)
            self.assertEqual(code, 2, "the argument %r was accepted: %s" % (argument, out))
            self.assertEqual(out, "", "a path was printed for the argument %r" % argument)
        self.assertFalse((self.base / "other-lane").exists(), "the other agent's directory was made from an argument")

    def test_s8_no_argument_names_a_path_or_another_agent(self):
        self.check_arguments_refused()

    def test_s8_mutant_a_name_taken_from_an_argument_fails_the_check(self):
        def add(sub):
            sub.add_parser("scratch").add_argument("name", nargs="?")

        def cmd(args, runner, now):
            path = sb.Path(self.world.state) / "scratch" / (args.name or "acme-lane")
            path.mkdir(parents=True, exist_ok=True)
            print(path)
            return 0
        with mock.patch.object(sb, "add_scratch_parsers", add), mock.patch.object(sb, "cmd_scratch", cmd):
            with self.assertRaises(AssertionError) as caught:
                self.check_arguments_refused()
        self.assertIn("was accepted", str(caught.exception))

    def test_s3_mutant_a_symlink_accepted_fails_the_refusal(self):
        elsewhere = self.world.state / "elsewhere"
        elsewhere.mkdir()
        self.base.mkdir(mode=0o700)
        os.symlink(str(elsewhere), str(self.base / "acme-lane"))
        with mock.patch.object(sb, "job_private", lambda path: True):
            code, out, err = self.scratch()
        self.assertEqual(code, 0, "the mutant must accept the symlink, or the refusal test pins nothing")
        self.assertTrue(out.strip().endswith("acme-lane"))

    def test_s6_the_other_agents_directory_is_not_listed(self):
        other = self.private(self.base / "other-lane")
        (other / "secret-name.txt").write_text("x")
        code, out, err = self.scratch("other-lane")
        self.assertEqual(code, 2)
        self.assertNotIn("secret-name", out + err)
        code, out, err = self.scratch()
        self.assertEqual(code, 0)
        self.assertNotIn("other-lane", out + err)

    def test_s6_s9_the_documentation_says_a_scratch_file_is_not_a_place_for_a_secret(self):
        text = (Path(__file__).resolve().parents[2] / "docs" / "switchboard.md").read_text()
        self.assertEqual(text.count("not a place for a secret"), 1)

    def test_s9_a_file_written_by_the_documented_example_sits_in_a_private_directory(self):
        code, out, err = self.scratch()
        path = Path(out.strip())
        (path / "helper.sh").write_text("echo ok\n")
        self.assertEqual(stat.S_IMODE(os.lstat(str(path)).st_mode), 0o700)
        self.assertEqual(self.scratch()[0], 0)
        self.assertEqual((path / "helper.sh").read_text(), "echo ok\n")

    def check_bound(self):
        path = self.fill(99)
        code, out, err = self.scratch()
        self.assertEqual((code, err), (0, ""), "99 MB must give no warning")
        self.assertEqual(os.listdir(str(path)), ["big.bin"])
        self.fill(101)
        code, out, err = self.scratch()
        self.assertEqual(code, 0)
        self.assertEqual(len(err.strip().splitlines()), 1, "one warning line for 101 MB: %r" % err)
        self.assertIn("100 MB", err)
        self.assertIn("101 MB", err)
        self.assertEqual(os.listdir(str(path)), ["big.bin"], "a file was lost to the bound")
        self.assertEqual(out.strip(), str(path))

    def test_s10_over_the_bound_one_warning_and_nothing_deleted(self):
        self.check_bound()

    def test_s10_mutant_a_bound_that_deletes_fails_the_check(self):
        real = sb.scratch_warning

        def deleting(path, size):
            for entry in os.listdir(str(path)):
                os.unlink(os.path.join(str(path), entry))
            return real(path, size)
        with mock.patch.object(sb, "scratch_warning", deleting):
            with self.assertRaises(AssertionError) as caught:
                self.check_bound()
        self.assertIn("a file was lost to the bound", str(caught.exception))

    def test_s12_the_command_makes_the_directory_launch_would_make_and_no_other_code_does(self):
        engine = mock.Mock(state_dir=str(self.world.state))
        code, out, err = self.scratch()
        self.assertEqual(Path(out.strip()), sb.agent_scratch_dir(engine, "acme-lane"))
        source = (Path(__file__).resolve().parents[2] / "bin" / "switchboard" / "scratch_dir.py").read_text()
        for call in ("mkdir", "makedirs", "chmod", "os.open("):
            self.assertNotIn(call, source, "the part makes a directory itself")
        calls = []
        real = sb.job_ensure_root
        with mock.patch.object(sb, "job_ensure_root", lambda root: (calls.append(str(root)), real(root))[1]):
            self.scratch(name="fresh-lane")
        self.assertIn(str(self.base / "fresh-lane"), calls)


class TestScratchGuard(ScratchWorld):
    """S4, S5: each removing command, run over a state directory holding a scratch file, leaves it unchanged."""

    def plant(self):
        path = self.private(self.base / self.NAME)
        (path / "helper.sh").write_text("echo keep me\n")
        (self.world.state / "records.json").write_text("{}")   # the janitor judges nothing without a record in the state directory
        return path / "helper.sh"

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    def run_janitor(self):
        """The temp janitor over a temp directory that holds the state directory (the one way its sweep can reach the state directory)."""
        jan = sb.load_disk_janitor()
        temp = self.world.state.parent / "tmp-holding-state"
        temp.mkdir()
        holder = temp / "fleet-state"
        os.rename(str(self.world.state), str(holder))
        os.symlink(str(holder), str(self.world.state))
        cfg = jan.settings({"days": 1})
        # the janitor's own read of the records would touch their access time and keep the entry "changed since the scan": it is told there is nothing to read
        with mock.patch.object(jan, "named_text", lambda state: ("", True)):
            lines, removed, kept = jan.run(str(temp), str(self.world.state), time.time() + 400 * 86400, cfg, True, lambda *a: None, busy=lambda p: None)
        os.unlink(str(self.world.state))
        if holder.exists():
            os.rename(str(holder), str(self.world.state))
        else:
            self.world.state.mkdir()
        return lines

    def run_rotation(self):
        archive = self.world.state / "audit-2000-01-01.jsonl.gz"
        archive.write_bytes(b"x")
        os.utime(str(archive), (1, 1))
        sb.AuditLog(self.world.state / "audit.jsonl").rotate()
        self.assertFalse(archive.exists(), "the rotation did not run its prune")

    def run_reclaim(self):
        with mock.patch.object(sb, "gates_caller_kind", lambda runner, now: ("person", "me")), captured():
            code = sb.main(["disk", "reclaim-worktrees", "--apply"], runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        self.assertEqual(code, 0)

    def run_agent_tmp(self):
        sb.reclaim_agent_tmp(mock.Mock(config={}), self.NAME)

    def check_unchanged(self):
        scratch = self.plant()
        before = self.digest(scratch)
        for label, run in (("the temp janitor", self.run_janitor), ("the state-directory rotation", self.run_rotation),
                           ("disk reclaim-worktrees", self.run_reclaim), ("the agent tmp reclaim", self.run_agent_tmp)):
            run()
            self.assertEqual(self.digest(scratch), before, "%s changed the scratch file %s" % (label, scratch))
            self.assertTrue(scratch.exists(), "%s removed the scratch file %s" % (label, scratch))

    def test_s4_no_removing_command_touches_a_scratch_file(self):
        self.check_unchanged()

    def test_s5_control_the_janitor_does_sweep_what_it_may(self):
        """Without a scratch directory the same janitor run removes the old state tree: the guard, not a no-op janitor, is what keeps the file."""
        self.plant()
        with mock.patch.object(sb.load_disk_janitor(), "holds_scratch", lambda *a: False):
            lines = self.run_janitor()
        self.assertTrue(any(l.startswith("removed ") for l in lines), lines)

    def test_s5_mutant_a_sweep_of_the_scratch_directory_fails_the_check(self):
        jan = sb.load_disk_janitor()
        with mock.patch.object(sb, "load_disk_janitor", lambda: jan), mock.patch.object(jan, "holds_scratch", lambda *a: False):
            with self.assertRaises(AssertionError) as caught:
                self.check_unchanged()
        self.assertIn("the temp janitor", str(caught.exception))
        self.assertIn("helper.sh", str(caught.exception))


class TestScratchBound(ScratchWorld):
    """S11: the daemon records one audit line a day while a scratch directory stays over the bound, and removes nothing."""

    def engine(self, clock):
        lines = []
        audit = mock.Mock(append=lambda actor, event, subject=None, detail=None, **kw: lines.append((event, subject, detail)))
        return mock.Mock(state_dir=str(self.world.state), now=lambda: clock[0], actor="daemon", audit=audit), lines

    def test_s11_one_line_a_day_and_nothing_removed(self):
        path = self.fill(101)
        clock = [1_800_000_000.0]
        engine, lines = self.engine(clock)
        source = sb.ScratchBoundFacts()
        source.bind(engine)
        self.assertEqual(source(), [])
        clock[0] += 3600
        self.assertEqual(source(), [])
        self.assertEqual([l[0] for l in lines], ["scratch.over_bound"], lines)
        self.assertEqual(lines[0][1], "acme-lane")
        self.assertEqual(os.listdir(str(path)), ["big.bin"])
        clock[0] += 86400
        source()
        self.assertEqual(len(lines), 2, "a new day records a new line")

    def test_s11_under_the_bound_no_line(self):
        self.fill(99)
        clock = [1_800_000_000.0]
        engine, lines = self.engine(clock)
        source = sb.ScratchBoundFacts()
        source.bind(engine)
        source()
        self.assertEqual(lines, [])

    def test_s11_mutant_a_line_every_pass_records_two(self):
        self.fill(101)
        clock = [1_800_000_000.0]
        engine, lines = self.engine(clock)
        source = sb.ScratchBoundFacts()
        source.bind(engine)
        with mock.patch.object(sb, "_write_small", lambda *a: None):   # the day is never kept: a line every look
            source()
            clock[0] += 3600
            source()
        self.assertEqual(len(lines), 2, "the mutant must record two lines, or the one-line test pins nothing")

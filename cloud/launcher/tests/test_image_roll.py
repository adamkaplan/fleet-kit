#!/usr/bin/env python3
"""Ask #2391: bin/cloud_image_roll.py (cw-image-roll), offline, with a fake `crane` script and scratch settings files. No network, no real credential."""
import contextlib
import datetime
import io
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / "bin" / "cloud_image_roll.py").read_text()
HEX_A = "a" * 64
HEX_B = "b" * 64
OLD = "c" * 64
SRC = "src.example/team/cw-worker@sha256:" + HEX_A
TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
ANCHOR = "the image setting was changed after a failed copy"
FAKE = """#!/bin/sh
[ "$1" = config ] && { printf '{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}' "1111111111111111111111111111111111111111"; exit 0; }
echo "$@" >> "$CRANE_LOG"
case "$1" in
  copy) echo "login failed token=$FAKE_TOKEN" >&2; [ "$FAKE_COPY" = fail ] && exit 1; exit 0;;
  digest) printf '%s' "$FAKE_DIGEST"; exit 0;;
esac
exit 9
"""
LINES = ["# settings\n", "CW_CLOUD_NAMESPACE=cw\n", "CW_CLOUD_IMAGE=reg.example/cw@sha256:%s\n" % OLD, "CW_CLOUD_OTHER=1\n", "\n", "CW_LAST=x"]


def load(source=SOURCE):
    m = types.ModuleType("cloud_image_roll_under_test")
    exec(compile(source, "cloud_image_roll.py", "exec"), m.__dict__)
    return m


def mutant(old, new):
    assert SOURCE.count(old) == 1, old
    out = SOURCE.replace(old, new)
    assert out != SOURCE
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="image-roll-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.crane = os.path.join(self.tmp, "crane")
        Path(self.crane).write_text(FAKE)
        os.chmod(self.crane, 0o755)
        self.log = os.path.join(self.tmp, "crane.log")
        Path(self.log).write_text("")
        self.dir = os.path.join(self.tmp, "cfg")
        os.mkdir(self.dir, 0o700)
        self.settings = os.path.join(self.dir, "cloud.env")
        self.write_settings(0o600)
        # ask #2418: the proof gate passes here (a fake git that finds the revision on the integration tip, and a fresh pass receipt for the digest)
        gitdir = os.path.join(self.tmp, "gitbin")
        os.mkdir(gitdir)
        Path(gitdir, "git").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(gitdir, "git"), 0o755)
        state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(state, "fl" + "eet-switchboard", "cloud", "image-proof"))
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        Path(state, "fl" + "eet-switchboard", "cloud", "image-proof", "r.receipt").write_text(
            "digest: sha256:%s\ntime: %s\nrevision: %s\nverdict: pass\n" % (HEX_A, stamp, "1" * 40))
        os.chmod(os.path.join(state, "fl" + "eet-switchboard", "cloud", "image-proof", "r.receipt"), 0o600)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state, "PATH": gitdir + os.pathsep + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_settings(self, mode):
        Path(self.settings).write_text("".join(LINES))
        os.chmod(self.settings, mode)
        os.utime(self.settings, (1_000_000_000, 1_000_000_000))

    def state(self):
        st = os.stat(self.settings)
        return Path(self.settings).read_bytes(), st.st_mtime_ns

    def run_cmd(self, module=None, copy="ok", digest="sha256:" + HEX_A, extra=(), uid=None, path=None):
        module = module or load()
        env = {"CRANE": self.crane, "CRANE_LOG": self.log, "FAKE_COPY": copy, "FAKE_DIGEST": digest, "FAKE_TOKEN": TOKEN, "PATH": os.environ.get("PATH", "")}
        argv = [SRC, "dest.example/team/cw-worker", "v1", "--settings", path or self.settings] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if uid is None:
                code = module.main(argv, env)
            else:
                with mock.patch.object(module, "current_uid", lambda: uid):
                    code = module.main(argv, env)
        return code, out.getvalue(), err.getvalue()

    def copies(self):
        return [l for l in Path(self.log).read_text().splitlines() if l]

    def backups(self):
        return sorted(n for n in os.listdir(self.dir) if n != "cloud.env")


def check_failed_copy(case, module):
    """The S2 assertion for a failed copy, with the anchored text a mutant fails with."""
    before = case.state()
    code, out, err = case.run_cmd(module, copy="fail")
    if case.state() != before or case.backups():
        raise AssertionError(ANCHOR)
    case.assertNotEqual(code, 0)
    case.assertEqual(len((out + err).strip().splitlines()), 1)


class Refusals(Base):          # S2, S3, S5
    def test_s2_failed_copy_leaves_the_file_unchanged(self):
        check_failed_copy(self, load())

    def test_s2_different_destination_digest_leaves_the_file_unchanged(self):
        before = self.state()
        code, out, err = self.run_cmd(digest="sha256:" + HEX_B)
        self.assertNotEqual(code, 0)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual((self.state(), self.backups()), (before, []))

    def test_s2_empty_destination_digest_leaves_the_file_unchanged(self):
        before = self.state()
        code, out, err = self.run_cmd(digest="")
        self.assertNotEqual(code, 0)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual((self.state(), self.backups()), (before, []))

    def test_s3_mode_0644_refused_before_any_copy(self):
        self.write_settings(0o644)
        before = self.state()
        code, out, err = self.run_cmd()
        self.assertNotEqual(code, 0)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual((self.copies(), self.state(), self.backups()), ([], before, []))

    def test_s3_other_owner_refused_before_any_copy(self):
        before = self.state()
        code, out, err = self.run_cmd(uid=os.getuid() + 1)
        self.assertNotEqual(code, 0)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertIn("not owned", err)
        self.assertEqual((self.copies(), self.state(), self.backups()), ([], before, []))

    def test_s3_missing_file_refused_before_any_copy(self):
        code, out, err = self.run_cmd(path=os.path.join(self.dir, "none.env"))
        self.assertNotEqual(code, 0)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual((self.copies(), self.backups()), ([], []))

    def test_s5_dry_run_changes_nothing_and_prints_no_token(self):
        before = self.state()
        code, out, err = self.run_cmd(extra=["--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertEqual((self.state(), self.backups()), (before, []))
        self.assertEqual((out + err).count(TOKEN), 0)
        self.assertIn("would change", out)

    def test_s5_failed_copy_prints_no_token(self):
        code, out, err = self.run_cmd(copy="fail")
        self.assertEqual((out + err).count(TOKEN), 0)
        self.assertNotEqual(code, 0)


class Hardening(Base):
    def test_newline_ending_arguments_are_refused(self):
        module = load()
        before = self.state()
        for argv in ([SRC + "\n", "dest.example/x", "v1"], [SRC, "dest.example/x\n", "v1"], [SRC, "dest.example/x", "v1\n"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = module.main(argv + ["--settings", self.settings], {"CRANE": self.crane, "CRANE_LOG": self.log})
            self.assertNotEqual(code, 0, argv)
        self.assertEqual((self.copies(), self.state(), self.backups()), ([], before, []))

    def test_an_edit_made_during_the_copy_is_refused(self):
        module = load()
        real = module.run_crane

        def edit_after_copy(tool, args, env, what, timeout):
            out = real(tool, args, env, what, timeout)
            if args[0] == "copy":
                with open(self.settings, "a") as f:
                    f.write("\nCW_EXTRA=1\n")
            return out
        module.run_crane = edit_after_copy
        code, out, err = self.run_cmd(module)
        self.assertNotEqual(code, 0)
        self.assertIn("changed during the copy", err)
        self.assertEqual(self.backups(), [])

    def test_a_symlinked_settings_file_is_refused(self):
        link = os.path.join(self.dir, "link.env")
        os.symlink(self.settings, link)
        code, out, err = self.run_cmd(path=link)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.copies(), [])


class Success(Base):           # S4
    def test_s4_only_the_image_line_changes_and_one_backup_exists(self):
        old_bytes = Path(self.settings).read_bytes()
        code, out, err = self.run_cmd()
        self.assertEqual(code, 0, err)
        new = Path(self.settings).read_bytes().split(b"\n")
        was = old_bytes.split(b"\n")
        self.assertEqual(len(new), len(was))
        self.assertEqual([i for i, (a, b) in enumerate(zip(was, new)) if a != b], [2])
        self.assertEqual(new[2], b"CW_CLOUD_IMAGE=dest.example/team/cw-worker@sha256:" + HEX_A.encode())
        names = self.backups()
        self.assertEqual(len(names), 1)
        backup = os.path.join(self.dir, names[0])
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        self.assertEqual(Path(backup).read_bytes(), old_bytes)
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)
        self.assertIn(OLD[:12], out)
        self.assertIn(HEX_A[:12], out)
        self.assertNotIn(HEX_A[:13], out)
        self.assertIn(backup, out)
        self.assertEqual(self.copies()[0].split()[0], "copy")


class Mutant(Base):            # S6
    def test_s6_control_passes_and_mutant_fails_with_the_anchored_text(self):
        check_failed_copy(self, load())     # the control first
        self.write_settings(0o600)
        bad = load(mutant("if copy.returncode != 0:", "if False:"))
        with self.assertRaisesRegex(AssertionError, ANCHOR):
            check_failed_copy(self, bad)


if __name__ == "__main__":
    unittest.main()

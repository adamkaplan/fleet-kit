"""Ask #1770: the identity of a temp-janitor entry holds its file type, so a file replaced by a link with the same device, inode and newest mtime is kept at the recheck."""
import os
import re
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb
from support.paths import REPO

NAME = "victim.txt"


class JanitorIdentity(unittest.TestCase):
    def setUp(self):
        self.jan = sb.load_disk_janitor()
        self.root = Path(tempfile.mkdtemp(prefix="jid-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.temp = self.root / "t"
        self.temp.mkdir()
        self.state = self.root / "s"
        self.state.mkdir()
        (self.state / "state-marker.json").write_text("{}")
        self.now = time.time() + 10 * 86400
        self.cfg = self.jan.settings({})

    def judge(self, path):
        return self.jan.judge(str(path), "", self.now, self.cfg, busy=lambda p: None)

    def old_file(self, name=NAME):
        p = self.temp / name
        p.write_text("x")
        os.utime(str(p), (1000.0, 1000.0))
        return p

    def forge(self, target, like):
        """os.lstat that reports `target` with the device, inode and times of `like` and its own mode."""
        real = os.lstat
        fixed = real(str(like))
        mine = str(target)

        def fake(path, *a, **k):
            info = real(path, *a, **k)
            if str(path) != mine:
                return info
            out = {n: getattr(info, n) for n in dir(info) if n.startswith("st_")}
            for n in ("st_dev", "st_ino", "st_mtime", "st_atime", "st_ctime", "st_mtime_ns", "st_atime_ns", "st_ctime_ns"):
                out[n] = getattr(fixed, n)
            return types.SimpleNamespace(**out)
        return fake

    def test_s2_three_types_hold_three_identities(self):
        f = self.old_file("a.txt")
        d = self.temp / "b"
        d.mkdir()
        os.utime(str(d), (1000.0, 1000.0))
        link = self.temp / "c"
        link.symlink_to(f)
        rows = [self.judge(p) for p in (f, d, link)]
        self.assertEqual([r["action"] for r in rows], ["remove"] * 3, [r["why"] for r in rows])
        self.assertEqual(len({r["ident"][3] for r in rows}), 3)

    def test_s2_a_file_and_a_link_sharing_device_inode_and_mtime_differ(self):
        f = self.old_file("a.txt")
        link = self.temp / "c"
        link.symlink_to(f)
        a = self.judge(f)
        with mock.patch("os.lstat", self.forge(link, f)):
            b = self.judge(link)
        self.assertEqual(a["ident"][:3], b["ident"][:3])
        self.assertNotEqual(a["ident"], b["ident"])

    def swap_run(self, apply_swap=True):
        f = self.old_file()
        other = self.root / "outside.txt"
        other.write_text("precious")
        real_scan = self.jan.scan
        like = self.root / "like"
        os.link(str(f), str(like))   # keeps the inode alive; same inode, so the forged link can copy it
        fake = [None]

        def scan_then_swap(*a, **k):
            rows = real_scan(*a, **k)
            os.unlink(str(f))
            f.symlink_to(other)
            fake[0] = self.forge(f, like)
            os.lstat = fake[0]
            return rows
        real_lstat = os.lstat
        try:
            with mock.patch.object(self.jan, "scan", scan_then_swap):
                lines, removed, kept = self.jan.run(self.temp, self.state, self.now, self.cfg, True, lambda *a: None, busy=lambda p: None)
        finally:
            os.lstat = real_lstat
        return f, other, lines, removed

    def test_s3_a_file_replaced_by_a_look_alike_link_is_kept(self):
        f, other, lines, removed = self.swap_run()
        self.assertIn("kept %s: it changed since the scan" % NAME, lines)
        self.assertTrue(f.is_symlink(), "the link is not removed")
        self.assertEqual(removed, 0)
        self.assertTrue(other.exists())

    def test_s3_mutant_the_old_three_tuple_fails_it(self):
        src = (REPO / "bin" / "disk_janitor.py").read_text()
        new = "(info.st_dev, info.st_ino, newest, stat.S_IFMT(info.st_mode))"
        self.assertEqual(src.count(new), 1)
        scratch = Path(tempfile.mkdtemp(prefix="jid-mut-"))
        self.addCleanup(shutil.rmtree, str(scratch), True)
        mutant = scratch / "disk_janitor.py"
        mutant.write_text(src.replace(new, "(info.st_dev, info.st_ino, newest)"))
        real = sb.load_disk_janitor
        import importlib.util
        spec = importlib.util.spec_from_file_location("disk_janitor_mutant", str(mutant))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.jan = mod
        self.cfg = mod.settings({})
        with self.assertRaises(AssertionError) as cm:
            self.test_s3_a_file_replaced_by_a_look_alike_link_is_kept()
        self.assertIn("it changed since the scan", str(cm.exception))

    def test_u1_an_entry_that_vanished_at_the_recheck_is_kept(self):
        f = self.old_file()
        real_scan = self.jan.scan

        def scan_then_vanish(*a, **k):
            rows = real_scan(*a, **k)
            os.unlink(str(f))
            return rows
        with mock.patch.object(self.jan, "scan", scan_then_vanish):
            lines, removed, kept = self.jan.run(self.temp, self.state, self.now, self.cfg, True, lambda *a: None, busy=lambda p: None)
        self.assertIn("kept %s: it changed since the scan" % NAME, lines)
        self.assertEqual(removed, 0)

    def test_u1_an_entry_whose_type_cannot_be_read_at_the_recheck_is_kept(self):
        f = self.old_file()
        real_scan, real_lstat = self.jan.scan, os.lstat

        def scan_then_break(*a, **k):
            rows = real_scan(*a, **k)

            def broken(path, *x, **y):
                if str(path) == str(f):
                    raise PermissionError("no")
                return real_lstat(path, *x, **y)
            os.lstat = broken
            return rows
        try:
            with mock.patch.object(self.jan, "scan", scan_then_break):
                lines, removed, kept = self.jan.run(self.temp, self.state, self.now, self.cfg, True, lambda *a: None, busy=lambda p: None)
        finally:
            os.lstat = real_lstat
        self.assertIn("kept %s: it changed since the scan" % NAME, lines)
        self.assertEqual(removed, 0)
        self.assertTrue(f.exists())

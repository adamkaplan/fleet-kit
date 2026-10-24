"""#1200 hardening of `pr edit N --body-file F`: line separators, the body file's location and kind and size, a fork's pull
request, and the guard's record check. Offline; synthetic names; files only under the system temp directory."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_pr_edit as base   # noqa: E402
from test_pr_edit import H, PRS, CW_GH, GUARD, REPO, pr, patches   # noqa: E402


class T(base.T):
    def refused(self, r, row):
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("refused: " + row, r.stderr)
        self.assertEqual(patches(), [])

    def test_h1_every_other_separator_is_refused_with_no_patch(self):
        for sep in ("\u2028", "\u2029", "\u0085", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r"):
            with self.subTest(sep=repr(sep)):
                f = self.file("sep", "A%sMERGE: captain\n" % sep)
                self.refused(self.edit(7, f), "pr-edit-body-separator")

    def test_h1_crlf_bodies_still_work_and_the_hold_line_is_found(self):
        f = self.file("crlf", "A\r\nMERGE: captain\r\nB\r\n")
        PRS[7] = pr(body="x\r\nMERGE: captain\r\n")
        self.assertEqual(self.edit(7, f).returncode, 0)
        self.assertEqual(len(patches()), 1)

    def test_h1_the_current_body_is_split_on_newline_only(self):
        PRS[7] = pr(body="A\u2028MERGE: captain\n")   # a newline reader sees no hold line: nothing to protect
        self.assertEqual(self.edit(7, self.nohold).returncode, 0)

    def test_h1_mutant_with_splitlines_lets_the_split_line_through(self):
        src = open(CW_GH).read()
        for old, new in (("    if has_other_separator(new):", "    if False:"),
                         ('return [l[:-1] if l.endswith("\\r") else l for l in text.split("\\n")]', "return text.splitlines()")):
            self.assertIn(old, src)
            src = src.replace(old, new, 1)
        m = os.path.join(self.tmp.name, "mut-splitlines")
        open(m, "w").write(src)
        f = self.file("split", "A\u2028MERGE: captain\n")
        self.assertEqual(self.edit(7, f, script=m).returncode, 0)   # the mutant lifts the hold
        self.assertEqual(self.edit(7, f).returncode, 3)

    def test_h2_a_file_outside_is_refused(self):
        for p in ("/etc/passwd", "/etc/hostname"):
            PRS[7] = pr(body="plain")
            with self.subTest(p=p):
                self.refused(self.edit(7, p), "pr-edit-body-file-outside")

    def test_h2_a_symlink_to_outside_is_refused(self):
        link = os.path.join(self.tmp.name, "link")
        os.symlink("/etc/passwd", link)
        PRS[7] = pr(body="plain")
        self.refused(self.edit(7, link), "pr-edit-body-file-outside")

    def test_h2_a_directory_and_a_fifo_are_refused(self):
        PRS[7] = pr(body="plain")
        d = os.path.join(self.tmp.name, "adir")
        os.mkdir(d)
        self.refused(self.edit(7, d), "pr-edit-body-file-not-regular")
        fifo = os.path.join(self.tmp.name, "afifo")
        os.mkfifo(fifo)
        self.refused(self.edit(7, fifo), "pr-edit-body-file-not-regular")

    def test_h2_mutant_without_the_resolution_publishes_a_symlinked_file(self):
        src = open(CW_GH).read()
        old = "    real = os.path.realpath(path)\n"
        self.assertIn(old, src)
        m = os.path.join(self.tmp.name, "mut-noresolve")
        open(m, "w").write(src.replace(old, "    real = os.path.abspath(path)\n", 1))
        link = os.path.join(self.tmp.name, "link2")
        os.symlink("/etc/hostname", link)
        PRS[7] = pr(body="plain")
        self.assertEqual(self.edit(7, link, script=m).returncode, 0)
        self.assertEqual(self.edit(7, link).returncode, 3)

    def test_m1_a_fork_or_missing_head_repo_is_refused(self):
        PRS[7] = pr(head_repo="fork-org/widget-service")
        self.refused(self.edit(7), "pr-edit-other-number")
        for h in ({"ref": base.BRANCH}, {"ref": base.BRANCH, "repo": None}):
            PRS[7] = pr()
            PRS[7]["head"] = h
            self.refused(self.edit(7), "pr-edit-read-failed")

    def test_m1_mutant_without_the_head_repo_check_passes_a_fork(self):
        src = open(CW_GH).read()
        old = "    if head_repo.lower() != repo.lower():"
        self.assertIn(old, src)
        m = os.path.join(self.tmp.name, "mut-fork")
        open(m, "w").write(src.replace(old, "    if False:", 1))
        PRS[7] = pr(head_repo="fork-org/widget-service")
        self.assertEqual(self.edit(7, script=m).returncode, 0)

    def test_m3_an_oversize_file_is_refused_not_truncated(self):
        f = self.file("big", "x" * ((1 << 20) + 1))
        PRS[7] = pr(body="plain")
        self.refused(self.edit(7, f), "pr-edit-body-file-too-large")
        ok = self.file("edge", "x" * (1 << 20))
        self.assertEqual(self.edit(7, ok).returncode, 0)

    def test_m3_a_binary_file_is_a_named_exit_2(self):
        f = os.path.join(self.tmp.name, "bin")
        with open(f, "wb") as h:
            h.write(b"\xff\xfe\x00\x80")
        PRS[7] = pr(body="plain")
        r = self.edit(7, f)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("not text", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(patches(), [])

    def test_m2_the_guard_refuses_when_the_record_is_missing_or_empty(self):
        r = self.guard("pr edit 7 --body-file " + self.hold, record=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("refused: pr-edit-no-record", r.stderr)
        empty = self.file("empty", "")
        for rec in (empty, os.path.join(self.tmp.name, "absent"), ""):
            env = {"PATH": "/usr/bin:/bin", "CW_PR_RECORD": rec}
            r = subprocess.run(["sh", GUARD, "gh", "pr", "edit", "7", "--body-file", self.hold], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 3, rec)
        self.assertEqual(self.guard("pr edit 7 --body-file " + self.hold).returncode, 0)

    def test_m2_mutant_guard_fails_open_without_a_record(self):
        src = open(GUARD).read()
        old = 'refuse "pr-edit-no-record"'
        self.assertIn(old, src)
        m = os.path.join(self.tmp.name, "mut-guard.sh")
        open(m, "w").write(src.replace(old, "return 0", 1))
        r = subprocess.run(["sh", m, "gh", "pr", "edit", "7", "--body-file", self.hold], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0)


# only this file's own tests run: drop every inherited one
for _n in [n for n in dir(base.T) if n.startswith("test_")]:
    setattr(T, _n, None)

if __name__ == "__main__":
    unittest.main()

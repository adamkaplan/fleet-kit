"""#1200 hardening of `pr edit N --body-file F`: line separators, the body file's location and kind and size, a fork's pull
request, and the guard's record check. Offline; synthetic names; files only under the system temp directory."""
import os
import subprocess
import sys
import tempfile
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
        f = self.file("crlf", "A\r\nMERGE: captain\nB\r\n")   # CRLF elsewhere is fine; the hold line is exact in the new body
        PRS[7] = pr(body="x\r\nMERGE: captain\r\n")
        self.assertEqual(self.edit(7, f).returncode, 0)
        self.assertEqual(len(patches()), 1)

    def test_h1_the_current_body_is_split_on_newline_only(self):
        PRS[7] = pr(body="A\u2028MERGE: captain\n")   # a newline reader sees no hold line: nothing to protect
        self.assertEqual(self.edit(7, self.nohold).returncode, 0)

    def test_h1_mutant_with_splitlines_lets_the_split_line_through(self):
        src = open(CW_GH).read()
        for old, new in (("    if has_other_separator(new):", "    if False:"),
                         ('return text.split("\\n")', "return text.splitlines()")):
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
        fd, outside = tempfile.mkstemp(dir="/var/tmp")   # owned by this user, outside /tmp and the worktree
        self.addCleanup(os.unlink, outside)
        os.write(fd, b"plain text\n")
        os.close(fd)
        os.symlink(outside, link)
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


    # ---- ask 1200, review round: body-file roots, owner, NUL, exact hold lines ----
    def mut(self, name, old, new):
        src = open(CW_GH).read()
        self.assertIn(old, src)   # the mutation applies
        m = os.path.join(self.tmp.name, name)
        open(m, "w").write(src.replace(old, new, 1))
        return m

    def foreign_owner(self, target=CW_GH):
        """A script that runs the client with os.stat answering a different owner for every file."""
        p = os.path.join(self.tmp.name, "other-uid.py")
        open(p, "w").write("import os, runpy, sys\nreal = os.stat\n"
                           "class S:\n    def __init__(s, st): s.st = st\n"
                           "    def __getattr__(s, n): return os.getuid() + 1 if n == 'st_uid' else getattr(s.st, n)\n"
                           "os.stat = lambda *a, **k: S(real(*a, **k))\n"
                           "sys.argv = [%r] + sys.argv[1:]\nrunpy.run_path(%r, run_name='__main__')\n" % (target, target))
        return p

    def wfile(self, name, text="A\nMERGE: captain\n"):
        p = os.path.join(self.work, name)
        open(p, "w").write(text)
        return p

    def test_r1_the_git_config_is_refused(self):
        cfg = os.path.join(self.work, ".git", "config")
        self.assertTrue(os.path.isfile(cfg))
        self.refused(self.edit(7, cfg), "pr-edit-body-file-git")

    def test_r1_a_symlink_into_a_git_directory_is_refused(self):
        link = os.path.join(self.tmp.name, "gitlink")
        os.symlink(os.path.join(self.work, ".git", "config"), link)
        self.refused(self.edit(7, link), "pr-edit-body-file-git")

    def test_r1_sensitive_names_are_refused(self):
        for name in (".netrc", "my-credentials.txt", "x.pem", ".env", "id_rsa"):
            with self.subTest(name=name):
                self.refused(self.edit(7, self.file(name, "A\nMERGE: captain\n")), "pr-edit-body-file-sensitive")
        d = os.path.join(self.tmp.name, ".ssh")
        os.mkdir(d)
        open(os.path.join(d, "note"), "w").write("A\nMERGE: captain\n")
        self.refused(self.edit(7, os.path.join(d, "note")), "pr-edit-body-file-sensitive")

    def test_r1_a_file_of_another_owner_or_a_writable_file_is_refused(self):
        f = self.file("theirs", "A\nMERGE: captain\n")
        self.refused(self.edit(7, f, script=self.foreign_owner()), "pr-edit-body-file-not-owned")
        os.chmod(f, 0o666)
        self.refused(self.edit(7, f), "pr-edit-body-file-not-owned")

    def test_r1_a_normal_file_in_the_worktree_and_in_tmp_is_accepted(self):
        self.assertEqual(self.edit(7, self.wfile("body-a")).returncode, 0)
        self.assertEqual(self.edit(7, self.file("body-b", "A\nMERGE: captain\n")).returncode, 0)
        self.assertEqual(len(patches()), 1)

    def test_m1_a_hardlink_to_the_git_config_is_refused_and_a_normal_file_is_accepted(self):
        cfg = os.path.join(self.work, ".git", "config")
        link = os.path.join(self.tmp.name, "hl.txt")
        os.link(cfg, link)
        self.addCleanup(os.unlink, link)
        PRS[7] = pr(body="plain")
        self.refused(self.edit(7, link), "pr-edit-body-file-linked")
        self.assertEqual(self.edit(7, self.file("nlink1", "A\n")).returncode, 0)
        self.assertEqual(len(patches()), 1)

    def test_m1_mutant_without_the_nlink_check_publishes_the_hardlink(self):
        m = self.mut("mut-nonlink", "    if st.st_nlink > 1:", "    if False:")
        self.assertIn("if False:", open(m).read())
        cfg = os.path.join(self.work, ".git", "config")
        link = os.path.join(self.tmp.name, "hl2.txt")
        os.link(cfg, link)
        self.addCleanup(os.unlink, link)
        PRS[7] = pr(body="plain")
        self.assertEqual(self.edit(7, link, script=m).returncode, 0)   # the mutant publishes it
        self.assertEqual(self.edit(7, link).returncode, 3)

    RESIDUAL = ("This narrows what a worker can publish as its own pull request's body; it does not close it: a file the "
                "worker itself copies to an innocent name in its worktree or /tmp can still be published. The boundary is "
                "the token and what the pod holds, not this check.")
    DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "docs", "cloud-worker-interface.md")

    def test_m2_the_docs_state_the_residual_plainly(self):
        self.assertIn(self.RESIDUAL, open(self.DOC).read())
        self.assertNotIn("exactly once", open(self.DOC).read())

    def test_m2_mutant_docs_without_the_sentence_fail_the_check(self):
        mutated = open(self.DOC).read().replace(self.RESIDUAL, "", 1)
        self.assertNotEqual(mutated, open(self.DOC).read())
        self.assertNotIn(self.RESIDUAL, mutated)

    def test_l1_a_crlf_current_body_hold_line_is_protected_but_a_crlf_new_one_is_not_accepted(self):
        PRS[7] = pr(body="x\r\nMERGE: captain\r\n")
        self.refused(self.edit(7, self.nohold), "pr-edit-no-hold-line")
        self.refused(self.edit(7, self.file("crnew", "A\r\nMERGE: captain\r\n")), "pr-edit-no-hold-line")
        PRS[7] = pr(body="x\r\nMERGE: captain\r\n")
        self.assertEqual(self.edit(7, self.file("lfnew", "A\nMERGE: captain\n")).returncode, 0)

    def test_l1_mutant_exact_current_line_only_lifts_the_crlf_protection(self):
        m = self.mut("mut-crcur", 'l in (HOLD, HOLD + "\\r")]', 'l == HOLD]')
        PRS[7] = pr(body="x\r\nMERGE: captain\r\n")
        self.assertEqual(self.edit(7, self.nohold, script=m).returncode, 0)
        self.assertEqual(self.edit(7, self.nohold).returncode, 3)

    def test_l3_the_secret_globs_equal_the_end_hooks(self):
        import importlib.util
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
        spec = importlib.util.spec_from_file_location("end_hook_x", os.path.join(root, "cloud", "launcher", "end_hook.py"))
        eh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eh)
        import re
        src = open(CW_GH).read()
        mine = eval(re.search(r"^SECRET_GLOBS = (\[.*?\])", src, re.S | re.M).group(1))
        self.assertEqual(mine, list(eh.SECRET_GLOBS))

    def test_l3_mutant_with_an_extra_glob_differs(self):
        m = self.mut("mut-glob", '".npmrc"]', '".npmrc", "*.extra"]')
        src = open(m).read()
        self.assertIn('"*.extra"', src)
        self.assertNotIn('"*.extra"', open(CW_GH).read())

    def test_r1_mutant_without_the_git_check_publishes_the_git_config(self):
        m = self.mut("mut-nogit", "    check_body_path(path, real)\n", "    pass\n")
        cfg = os.path.join(self.work, ".git", "config")
        PRS[7] = pr(body="plain")
        self.assertEqual(self.edit(7, cfg, script=m).returncode, 0)   # the mutant publishes it
        self.assertEqual(self.edit(7, cfg).returncode, 3)

    def test_r1_mutant_without_the_owner_check_publishes_a_foreign_file(self):
        m = self.mut("mut-noowner", "    if st.st_uid != os.getuid() or st.st_mode & 0o022:", "    if False:")
        f = self.file("theirs2", "A\nMERGE: captain\n")
        self.assertEqual(self.edit(7, f, script=self.foreign_owner()).returncode, 3)
        # the foreign-owner wrapper around the mutant: accepted
        self.assertEqual(self.edit(7, f, script=self.foreign_owner(m)).returncode, 0)

    def test_r2_a_body_with_a_nul_byte_is_refused(self):
        self.refused(self.edit(7, self.file("nul", "A\nMERGE: captain\n\0x\n")), "pr-edit-body-nul")

    def test_r2_a_carriage_return_hold_line_is_not_a_hold_line(self):
        PRS[7] = pr(body="A\nMERGE: captain\n")
        self.refused(self.edit(7, self.file("crhold", "A\nMERGE: captain\r\n")), "pr-edit-no-hold-line")   # CRLF form: not the hold line
        PRS[7] = pr(body="A\nMERGE: captain\r\n")   # ask 1200 low (a): a CRLF CURRENT body is protected
        self.refused(self.edit(7, self.nohold), "pr-edit-no-hold-line")
        PRS[7] = pr(body="A\nMERGE: captain\n")
        self.refused(self.edit(7, self.file("lonecr", "A\nMERGE: captain\rB\n")), "pr-edit-body-separator")

    def test_r2_two_hold_lines_may_not_drop_to_one(self):
        PRS[7] = pr(body="MERGE: captain\nA\nMERGE: captain\n")
        self.refused(self.edit(7, self.hold), "pr-edit-no-hold-line")
        self.assertEqual(self.edit(7, self.file("two", "MERGE: captain\nB\nMERGE: captain\n")).returncode, 0)


# only this file's own tests run: drop every inherited one
for _n in [n for n in dir(base.T) if n.startswith("test_")]:
    setattr(T, _n, None)


if __name__ == "__main__":
    unittest.main()

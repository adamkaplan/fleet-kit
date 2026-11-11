"""Ask #2426: the pod's pre-push hook and cw-gh RECORD, never refuse: one line "<UTC time> <kind> <remote> <ref> <short sha>" in the pod's notes file
(CW_NOTES_LOG, default /work/.pod-notes.log) for a push to the default branch, a non-fast-forward push, a merge of a pull request and a write to an issue or a
ref through `api`. Each control runs the same check against a copy of today's file with the recording taken out: it must fail with the anchored text.
Offline; no network, no token."""
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
HOOK = os.path.join(WORKER, "pod-hooks", "pre-push")
CW_GH = os.path.join(WORKER, "cw-gh")
ZERO = "0" * 40
LINE_RE = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (%s) (\S+) (\S+) (\S+)$"
ANCHOR = "the push was not recorded"
GH_ANCHOR = "the action was not recorded"


def read(p):
    with open(p) as f:
        return f.read()


def strip_between(text, start, end):
    a = text.index(start)
    b = text.index(end, a) + len(end)
    return text[:a] + text[b:]


class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.repo = os.path.join(cls.d, "r")
        os.mkdir(cls.repo)
        cls.log = os.path.join(cls.d, "notes.log")
        cls.env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": cls.d, "GIT_CONFIG_NOSYSTEM": "1", "CW_NOTES_LOG": cls.log,
                   "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x.invalid", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.invalid"}
        cls.git("init", "-q", "-b", "main", ".")
        cls.c1 = cls.commit("one")
        cls.c2 = cls.commit("two")
        cls.git("checkout", "-q", "-b", "side", cls.c1)
        cls.side = cls.commit("side")

    @classmethod
    def git(cls, *a):
        r = subprocess.run(["git"] + list(a), cwd=cls.repo, env=cls.env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    @classmethod
    def commit(cls, subject):
        cls.git("commit", "-q", "--allow-empty", "-m", subject)
        return cls.git("rev-parse", "HEAD")

    def setUp(self):
        if os.path.exists(self.log):
            os.unlink(self.log)

    def run_hook(self, lines, hook=HOOK, remote="origin", url="/tmp/local.git", env=None):
        stdin = "".join("%s %s %s %s\n" % l for l in lines)
        return subprocess.run(["sh", hook, remote, url], input=stdin, cwd=self.repo, env=env or self.env, capture_output=True, text=True, timeout=30)

    def lines(self):
        return read(self.log).splitlines() if os.path.exists(self.log) else []

    def push(self, dst, local, remote):
        return ("refs/heads/x", local, "refs/heads/" + dst, remote)

    def check_s2(self, hook=HOOK):
        """default branch (fast-forward), non-fast-forward to another branch, plain push to another branch, and the force of the default branch."""
        if os.path.exists(self.log):
            os.unlink(self.log)
        ok = self.run_hook([self.push("main", self.c2, self.c1)], hook)
        self.assertEqual((ok.returncode, ok.stdout, ok.stderr), (0, "", ""))
        got = self.lines()
        if len(got) != 1:
            self.fail("%s: default branch: %r" % (ANCHOR, got))
        m = re.match(LINE_RE % "default-branch-push", got[0])
        self.assertTrue(m, got)
        self.assertEqual(m.groups()[1:], ("origin", "refs/heads/main", self.c2[:10]))
        r = self.run_hook([self.push("topic", self.side, self.c2)], hook)   # side does not contain c2: not a fast-forward
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        got = self.lines()
        if len(got) != 2:
            self.fail("%s: non-fast-forward: %r" % (ANCHOR, got))
        m = re.match(LINE_RE % "non-fast-forward", got[1])
        self.assertTrue(m, got)
        self.assertEqual(m.groups()[1:], ("origin", "refs/heads/topic", self.side[:10]))
        r = self.run_hook([self.push("topic", self.c2, self.c1)], hook)   # a fast-forward to another branch
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        r = self.run_hook([self.push("topic", self.c2, ZERO)], hook)      # a new branch
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.lines()), 2, self.lines())   # nothing for another branch that fast-forwards or is new
        r = self.run_hook([self.push("main", self.side, self.c2)], hook)   # a force of the default branch: both facts
        self.assertEqual(r.returncode, 0)
        kinds = [l.split()[1] for l in self.lines()[2:]]
        self.assertEqual(sorted(kinds), ["default-branch-push", "non-fast-forward"], kinds)

    def test_s2_default_branch_and_non_fast_forward_are_recorded_and_not_refused(self):
        self.check_s2()

    def test_s2_the_recording_changes_no_result(self):
        """The result of the hook is the same with the notes file writable and with it unwritable: nothing is refused because of the recording."""
        lines = [self.push("main", self.c2, self.c1), self.push("topic", self.side, self.c2)]
        a = self.run_hook(lines)
        gone = dict(self.env, CW_NOTES_LOG=os.path.join(self.d, "no", "such", "dir", "log"))
        b = self.run_hook(lines, env=gone)
        isdir = dict(self.env, CW_NOTES_LOG=self.d)
        c = self.run_hook(lines, env=isdir)
        for r in (a, b, c):
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        # a refused push stays refused, with the same words, recorded or not
        net = "https://127.0.0.1:9/x.git"
        r1 = self.run_hook([self.push("main", self.c2, self.c1)], url=net)
        r2 = self.run_hook([self.push("main", self.c2, self.c1)], url=net, env=gone)
        self.assertEqual((r1.returncode, r1.stdout, r1.stderr), (r2.returncode, r2.stdout, r2.stderr))
        self.assertEqual(r1.returncode, 3)

    def test_s3_a_log_that_cannot_be_written_leaves_the_result_and_the_repository_hook_alone(self):
        hooks = os.path.join(self.repo, ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        own = os.path.join(hooks, "pre-push")
        with open(own, "w") as f:
            f.write("#!/bin/sh\ncat > /dev/null\nexit 7\n")
        os.chmod(own, 0o755)
        self.addCleanup(os.unlink, own)
        for path in (os.path.join(self.d, "no", "dir", "log"), self.d, "/proc/version"):
            env = dict(self.env, CW_NOTES_LOG=path)
            r = self.run_hook([self.push("main", self.c2, self.c1)], env=env)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (7, "", ""), path)   # the repository's own hook still decides

    def test_s3_mutant_that_fails_the_push_on_a_log_error_is_caught(self):
        text = read(HOOK)
        p = os.path.join(self.d, "mutant-s3")
        with open(p, "w") as f:
            f.write(text.replace(') >/dev/null 2>&1 || :', ') >/dev/null 2>&1 || exit 9').replace('>> "$NOTES_LOG"\n', '>> "$NOTES_LOG" || exit 9\n'))
        gone = dict(self.env, CW_NOTES_LOG=os.path.join(self.d, "no", "dir", "log"))
        r = self.run_hook([self.push("main", self.c2, self.c1)], hook=p, env=gone)
        self.assertEqual(r.returncode, 9)   # the control: this mutant changes the result; the real hook does not (test above)

    def test_the_remote_is_a_plain_name_a_url_is_not_written_and_text_is_cleaned(self):
        secret = "ghs_" + "A" * 30
        self.run_hook([self.push("main", self.c2, self.c1)], remote="https://user:%s@github.com/acme-org/widget-service.git" % secret)
        self.run_hook([("refs/heads/x", self.side, "refs/heads/top\x01$(id)`x`", self.c2)])
        self.run_hook([self.push("main", self.c2, self.c1)], remote="origin")
        text = read(self.log)
        self.assertNotIn(secret, text)
        self.assertNotIn("@", text)
        got = text.splitlines()
        self.assertEqual(got[0].split()[2], "-")
        self.assertEqual(got[2].split()[2], "origin")
        for l in got:
            self.assertEqual(len(l.split(" ")), 5, l)
            self.assertRegex(l, r"^[A-Za-z0-9:._/ ?-]+$")

    def test_the_file_is_capped_to_its_last_lines(self):
        text = read(HOOK)
        cap = int(re.search(r"^NOTES_MAX_BYTES=(\d+)$", text, re.M).group(1))
        keep = int(re.search(r"^NOTES_KEEP_LINES=(\d+)$", text, re.M).group(1))
        filler = "2000-01-01T00:00:00Z default-branch-push origin refs/heads/main aaaaaaaaaa\n"
        with open(self.log, "w") as f:
            f.write(filler * (cap // len(filler) + 10))
        self.assertGreater(os.path.getsize(self.log), cap)
        self.run_hook([self.push("main", self.c2, self.c1)])
        got = self.lines()
        self.assertEqual(len(got), keep)
        self.assertIn(self.c2[:10], got[-1])
        self.assertLessEqual(os.path.getsize(self.log), cap)
        self.run_hook([self.push("main", self.c2, self.c1)])
        self.assertEqual(len(self.lines()), keep + 1)   # under the cap nothing is cut

    def test_s5_control_the_hook_without_the_recording_records_none(self):
        text = read(HOOK)
        p = os.path.join(self.d, "today")
        with open(p, "w") as f:
            f.write(strip_between(text, "REMOTE_NAME=${1:-}\n", "\nEOF4\n"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s2(p)
        self.assertIn(ANCHOR, str(cm.exception))


class Gh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.log = os.path.join(cls.d, "notes.log")

    def setUp(self):
        if os.path.exists(self.log):
            os.unlink(self.log)

    def gh(self, *args, script=CW_GH, log=None):
        env = {"PATH": "/usr/bin:/bin", "CW_NOTES_LOG": log or self.log, "GH_TOKEN": "synthetic", "CW_GH_API_BASE": "http://127.0.0.1:9"}
        return subprocess.run(["python3", script] + list(args), env=env, capture_output=True, text=True, timeout=30)

    def lines(self):
        return read(self.log).splitlines() if os.path.exists(self.log) else []

    def check_s4(self, script=CW_GH):
        for args, kind, ref in (
            (("pr", "merge", "7", "--squash"), "pr-merge", "pull/7"),
            (("api", "-X", "PUT", "repos/acme-org/widget-service/pulls/7/merge"), "api-merge", "repos/acme-org/widget-service/pulls/7/merge"),
            (("api", "--method", "POST", "repos/acme-org/widget-service/git/refs", "-f", "ref=refs/heads/x"), "ref-write", "repos/acme-org/widget-service/git/refs"),
            (("api", "-X", "PATCH", "repos/acme-org/widget-service/git/refs/heads/x"), "ref-write", "repos/acme-org/widget-service/git/refs/heads/x"),
            (("api", "-X", "POST", "repos/acme-org/widget-service/issues/3/comments", "-f", "body=hi"), "issue-write", "repos/acme-org/widget-service/issues/3/comments"),
            (("api", "repos/acme-org/widget-service/issues/3/labels", "-f", "labels[]=x"), "issue-write", "repos/acme-org/widget-service/issues/3/labels"),
        ):
            before = len(self.lines())
            r = self.gh(*args, script=script)
            self.assertNotIn("Traceback", r.stderr)
            got = self.lines()
            if len(got) != before + 1:
                self.fail("%s: %s" % (GH_ANCHOR, " ".join(args)))
            m = re.match(LINE_RE % kind, got[-1])
            self.assertTrue(m, (args, got[-1]))
            self.assertEqual(m.groups()[1:], ("github", ref, "-"))

    def test_s4_a_merge_and_a_ref_or_issue_write_are_each_recorded(self):
        self.check_s4()

    def test_s4_reads_and_other_verbs_are_not_recorded_and_the_answer_is_unchanged(self):
        for args in (("api", "repos/acme-org/widget-service/pulls/7"), ("api", "-X", "GET", "repos/acme-org/widget-service/issues/3/comments"),
                     ("pr", "view", "7"), ("issue", "view", "3"), ("repo", "view"), ("nothing", "here"), ()):
            self.gh(*args)
        self.assertEqual(self.lines(), [])
        a = self.gh("pr", "merge", "7")
        b = self.gh("pr", "merge", "7", log=os.path.join(self.d, "no", "dir", "log"))   # an unwritable file: the same answer
        self.assertEqual((a.returncode, a.stdout, a.stderr), (b.returncode, b.stdout, b.stderr))
        self.assertNotIn("Traceback", a.stderr)

    def test_s4_the_file_is_capped(self):
        mod = {}
        exec(compile(read(CW_GH), CW_GH, "exec"), mod)
        filler = "2000-01-01T00:00:00Z pr-merge github pull/1 -\n"
        with open(self.log, "w") as f:
            f.write(filler * (mod["NOTES_MAX_BYTES"] // len(filler) + 10))
        self.gh("pr", "merge", "9")
        got = self.lines()
        self.assertEqual(len(got), mod["NOTES_KEEP_LINES"])
        self.assertIn("pull/9", got[-1])

    def test_s5_control_cw_gh_without_the_recording_records_none(self):
        text = read(CW_GH)
        call = "    note_action(argv)   # ask #2426: log only\n"
        self.assertEqual(text.count(call), 1)
        p = os.path.join(self.d, "today-cw-gh")
        with open(p, "w") as f:
            f.write(text.replace(call, ""))
        with self.assertRaises(AssertionError) as cm:
            self.check_s4(p)
        self.assertIn(GH_ANCHOR, str(cm.exception))


if __name__ == "__main__":
    unittest.main()

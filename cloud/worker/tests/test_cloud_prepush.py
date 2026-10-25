#!/usr/bin/env python3
"""Offline tests of bin/cloud-prepush (scenarios S1-S13, U2) on throwaway repositories under the temp directory."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, "bin", "cloud-prepush")
TD = "cloud/worker/tests/"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *a):
    p = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, (a, p.stderr)
    return p.stdout


def put(repo, rel, text, mode=0o755):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def reader(fname, body):
    return "import sys\n%s\nsys.exit(0)\n" % body.replace("FILE", repr(fname))


def make_repo(tmp, tests, scan=False):
    """base commit with a.txt and b.txt, branch 'feat' changing both; returns the repo path."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    put(repo, "a.txt", "one\n", 0o644)
    put(repo, "b.txt", "two\n", 0o644)
    for rel, text in tests.items():
        put(repo, rel, text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, "a.txt", "one NEW\n", 0o644)
    put(repo, "b.txt", "two NEW\n", 0o644)
    git(repo, "add", "a.txt", "b.txt")
    git(repo, "commit", "-q", "-m", "change")
    return repo


PASS = "import sys\nsys.exit(0)\n"


def fixtures():
    return {
        TD + "test_fail_branch_only.py": "# a.txt\nimport sys\nsys.exit(1 if 'NEW' in open('a.txt').read() else 0)\n",
        TD + "test_fail_differently.py": "# a.txt\nprint(open('a.txt').read())\nraise SystemExit(1)\n",
        TD + "test_fail_same.py": "# b.txt\nprint('same failure')\nraise SystemExit(1)\n",
        TD + "test_pass.py": "# a.txt and b.txt\n" + PASS,
        TD + "test_unrelated.py": "# names nothing changed\nraise SystemExit(1)\n",
    }


def run(repo, base="trunk", script=SCRIPT, env=None):
    e = dict(ENV, **(env or {}))
    return subprocess.run([sys.executable, script, base], cwd=repo, capture_output=True, text=True, env=e)


def mutant(tmp, old, new):
    s = open(SCRIPT).read()
    assert old in s, old
    path = os.path.join(tmp, "mutant-prepush")
    open(path, "w").write(s.replace(old, new, 1))
    return path


def core_checks(script, tmp):
    """The suite's decisive checks, returning the names that fail (empty = the script is right)."""
    bad = []
    repo = make_repo(tmp, fixtures())
    p = run(repo, script=script)
    out = p.stdout
    if "run 4, identical 2, could not run 0" not in out:
        bad.append("counts")
    if "test_fail_branch_only.py" not in out or "test_fail_differently.py" not in out or "test_fail_same.py" in out:
        bad.append("differences")
    if p.returncode != 1:
        bad.append("exit")
    shutil.rmtree(repo)
    # S2: selection of tests naming changed files (the tests that name b.txt only, and a.txt only)
    t = {TD + "test_names_a.py": "# a.txt\n" + PASS, TD + "test_names_b.py": "# b.txt\n" + PASS,
         TD + "test_names_ab.py": "# a.txt b.txt\n" + PASS, "bin/test-switchboard": "#!/usr/bin/env python3\n" + PASS,
         TD + "test_other.py": "# nothing\n" + PASS}
    repo = make_repo(tmp, t)
    p = run(repo, script=script)
    if "run 4, identical 4, could not run 0" not in p.stdout or p.returncode != 0:
        bad.append("selection")
    shutil.rmtree(repo)
    # S8: no selection on a non-empty change
    repo = make_repo(tmp, {TD + "test_other.py": "# nothing\n" + PASS})
    p = run(repo, script=script)
    if p.returncode != 1 or "NO test was selected" not in p.stdout:
        bad.append("noselect")
    shutil.rmtree(repo)
    return bad


class Prepush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_s2_s5_s8_core(self):
        self.assertEqual(core_checks(SCRIPT, self.tmp), [])

    def test_s3_worktree_removed_without_force(self):
        repo = make_repo(self.tmp, fixtures())
        run(repo)
        self.assertEqual(len([l for l in git(repo, "worktree", "list").splitlines() if l]), 1, "1 of 1 worktrees remaining")
        text = open(SCRIPT).read()
        self.assertNotIn('"--force"', text)
        self.assertNotIn('"-f"', text)

    def test_s4_list_exists(self):
        p = subprocess.run([sys.executable, SCRIPT, "--check-list"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_s6_exit_codes(self):
        # difference -> 1
        repo = make_repo(self.tmp, fixtures())
        self.assertEqual(run(repo).returncode, 1)
        shutil.rmtree(repo)
        # cannot start (not executable bin test) -> 1
        repo = make_repo(self.tmp, {"bin/test-x": "# a.txt\n" + PASS})
        os.chmod(os.path.join(repo, "bin/test-x"), 0o644)
        git(repo, "update-index", "--chmod=-x", "bin/test-x")
        git(repo, "commit", "-q", "--amend", "-m", "c", "--allow-empty")
        p = run(repo)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("could not run 1", p.stdout)
        shutil.rmtree(repo)
        # timeout -> 1
        repo = make_repo(self.tmp, {TD + "test_slow.py": "# a.txt\nimport time\ntime.sleep(20)\n"})
        p = run(repo, env={"CLOUD_PREPUSH_TIMEOUT": "1"})
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("could not run 1", p.stdout)
        shutil.rmtree(repo)
        # no difference -> 0
        repo = make_repo(self.tmp, {TD + "test_ok.py": "# a.txt\n" + PASS})
        self.assertEqual(run(repo).returncode, 0)

    def test_s7_refusals(self):
        repo = make_repo(self.tmp, fixtures())
        self.assertEqual(run(repo, base="no-such-ref").returncode, 2)
        # a branch with no common history
        git(repo, "checkout", "-q", "--orphan", "other")
        git(repo, "commit", "-q", "-m", "o", "--allow-empty")
        git(repo, "checkout", "-q", "feat")
        self.assertEqual(run(repo, base="other").returncode, 2)
        # shallow clone: the base is not reachable from the tip's history
        sh = os.path.join(self.tmp, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-local", "--branch", "feat", "file://" + repo, sh],
                       check=True, capture_output=True, env=ENV)
        subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", "trunk:basecopy"], cwd=sh, check=True,
                       capture_output=True, env=ENV)
        self.assertEqual(run(sh, base="basecopy").returncode, 2)
        # dirty tree
        put(repo, "a.txt", "dirty\n", 0o644)
        p = run(repo)
        self.assertEqual(p.returncode, 2, p.stdout)

    def test_s9_writes_nothing(self):
        repo = make_repo(self.tmp, fixtures())

        def snap():
            files = sorted(os.path.join(d, f) for d, _, fs in os.walk(repo) if ".git" not in d.split(os.sep) for f in fs)
            return git(repo, "status", "--porcelain", "--untracked-files=all"), files, os.listdir(self.tmp)
        before = snap()
        run(repo)
        self.assertEqual(snap(), before, "identical to the baseline")

    def test_s10_no_network_or_credential(self):
        text = open(SCRIPT).read().lower()
        names = ["curl", "wget", "http", "urllib", "socket", "token", "netrc", "credential", "password", "secret", "ssh"]
        hits = [n for n in names if n in text]
        self.assertEqual(hits, [], "0 occurrences of a network or credential name")

    def test_s11_mutants_refused(self):
        muts = [("names = {os.path.basename(c) for c in changed}", "names = {os.path.basename(c) for c in changed[:1]}"),
                ("if b[1] == a[1]:", "if b[1][0] == 0:"),
                ("NO test was selected; this is not a clean result (advice only)\" % len(changed))\n        return 1",
                 "NO test was selected; this is not a clean result (advice only)\" % len(changed))\n        return 0")]
        refused = 0
        for i, (old, new) in enumerate(muts):
            d = tempfile.mkdtemp(prefix="prepush-mut-")
            try:
                if core_checks(mutant(d, old, new), d):
                    refused += 1
            finally:
                shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(refused, 3, "3 of 3 mutants refused")

    def test_s12_chained(self):
        self.assertIn("cloud/worker/tests/test_cloud_prepush.py", open(os.path.join(ROOT, "bin", "test-cloud-launcher")).read())

    def test_docs_s1_s13_u2(self):
        n = 0
        for rel in ("cloud/README.md", "cloud/worker/agents/worker.md"):
            t = open(os.path.join(ROOT, rel)).read()
            self.assertIn("replaces the hand-written rule", t, rel)
            self.assertIn("bin/cloud-prepush", t, rel)
            self.assertIn("last step before every push", t, rel)
            self.assertIn("advice", t, rel)
            self.assertIn("continuous integration stays the gate", t, rel)
            n += 1
        self.assertEqual(n, 2)
        p = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True, text=True)
        self.assertIn("ADVICE ONLY", p.stdout)
        repo = make_repo(self.tmp, {TD + "test_ok.py": "# a.txt\n" + PASS})
        self.assertIn("advice only", run(repo).stdout)


if __name__ == "__main__":
    unittest.main()

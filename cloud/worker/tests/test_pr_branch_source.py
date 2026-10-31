"""#1200 scenario S17: the client reads its own branch from the pod main process's initial environment (BRANCH_SOURCE, /proc/1/environ), not from
the caller's environment or git HEAD. Where that file cannot be read (a developer machine, a CI runner whose PID 1 is root's) the old rule applies;
it is NOT a bound. A number is ASCII digits only. Offline: BRANCH_SOURCE points at a scratch file in every test."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pr_edit as P  # noqa: E402

CONST = 'BRANCH_SOURCE = "/proc/1/environ"'
VICTIM = "other-branch"   # the head of pull request 8 in the stub


class T(P.T):
    """Reuses the stub server and the helpers of test_pr_edit (not edited); its own tests are the ones named test_s17*."""
    def setUp(self):
        super().setUp()
        self.src = open(P.CW_GH).read()
        self.assertEqual(self.src.count(CONST), 1)
        self.victim_work = os.path.join(self.tmp.name, "victim-work")
        if not os.path.isdir(self.victim_work):
            os.mkdir(self.victim_work)
            subprocess.run(["git", "init", "-q", self.victim_work], check=True)
            subprocess.run(["git", "-C", self.victim_work, "symbolic-ref", "HEAD", "refs/heads/" + VICTIM], check=True)
            subprocess.run(["git", "-C", self.victim_work, "remote", "add", "origin", "https://github.com/%s.git" % P.REPO], check=True)

    def copy(self, source, name="cw-gh-copy", text=None):
        p = os.path.join(self.tmp.name, name)
        body = (text if text is not None else self.src).replace(CONST, 'BRANCH_SOURCE = %r' % source, 1)
        open(p, "w").write(body)
        return p

    def scratch(self, content, name="environ"):
        p = os.path.join(self.tmp.name, name)
        open(p, "wb").write(content)
        return p

    def run_edit(self, script, n, extra=None, cwd=None, env_branch=True):
        H = P.H
        H.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": P.TOKEN, "HOME": "/nonexistent"}
        if env_branch:
            env["CW_BRANCH"] = P.BRANCH
        env.update(extra or {})
        r = subprocess.run([sys.executable, script, "pr", "edit", str(n), "--body-file", self.hold], capture_output=True, text=True, env=env,
                           cwd=cwd or self.work, timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotIn(P.TOKEN, r.stdout + r.stderr)
        return r

    def environ(self, branch=P.BRANCH):
        return b"PATH=/usr/bin\0CW_BRANCH=" + branch.encode() + b"\0HOME=/root\0"

    # (i) forged: env, HEAD, both
    def test_s17_forged_branch_is_refused_with_no_patch(self):
        script = self.copy(self.scratch(self.environ()))
        for name, extra, cwd in (("env", {"CW_BRANCH": VICTIM}, None), ("head", {}, self.victim_work), ("both", {"CW_BRANCH": VICTIM}, self.victim_work)):
            r = self.run_edit(script, 8, extra, cwd, env_branch=False)
            self.assertEqual(r.returncode, 3, (name, r.stderr))
            self.assertIn("refused: pr-edit-other-number", r.stderr, name)
            self.assertEqual(len(P.patches()), 0, name)

    # (ii) the own pull request
    def test_s17_the_own_pull_request_makes_one_patch(self):
        r = self.run_edit(self.copy(self.scratch(self.environ())), 7, {"CW_BRANCH": VICTIM}, self.victim_work, env_branch=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(P.patches()), 1)

    # (iii) readable but no CW_BRANCH (or an empty one): every own edit is refused, the caller's values are ignored
    def test_s17_a_readable_file_without_the_variable_refuses_every_own_edit(self):
        for name, content in (("none", b"PATH=/usr/bin\0HOME=/root\0"), ("empty", b"CW_BRANCH=\0PATH=/x\0"), ("blank", b"")):
            script = self.copy(self.scratch(content, "environ-" + name))
            r = self.run_edit(script, 7)
            self.assertEqual(r.returncode, 3, (name, r.stderr))
            self.assertIn("refused: pr-edit-other-number", r.stderr, name)
            self.assertEqual(len(P.patches()), 0, name)

    # (iv) the fallback, three named cases: the old rule (env CW_BRANCH) applies and the own edit passes
    def fallback_passes(self, source):
        r = self.run_edit(self.copy(source), 7)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(P.patches()), 1)

    def test_s17_fallback_a_missing_path(self):
        self.fallback_passes(os.path.join(self.tmp.name, "no-such-environ"))

    def test_s17_fallback_a_directory(self):
        d = os.path.join(self.tmp.name, "environ-dir")
        os.makedirs(d, exist_ok=True)
        self.fallback_passes(d)

    def test_s17_fallback_a_file_of_mode_000(self):
        p = self.scratch(self.environ(VICTIM), "environ-000")
        os.chmod(p, 0)
        try:
            open(p, "rb").close()
        except OSError:
            pass
        else:
            self.skipTest("this user can read a mode 000 file (root)")
        self.fallback_passes(p)

    # (v) mutants
    def test_s17_a_mutant_reading_the_callers_environment_first_edits_the_forged_branch(self):
        old = "    mine = own_branch()\n"
        self.assertEqual(self.src.count(old), 1)
        mutated = self.src.replace(old, '    mine = os.environ.get("CW_BRANCH") or own_branch()\n', 1)
        self.assertNotEqual(mutated, self.src)   # the mutation was applied
        script = self.copy(self.scratch(self.environ()), "cw-gh-mutant", mutated)
        r = self.run_edit(script, 8, {"CW_BRANCH": VICTIM}, env_branch=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(P.patches()), 1)

    def test_s17_a_mutant_where_the_fallback_wins_over_a_readable_file_fails_the_forged_case(self):
        old = "    mine = own_branch()\n"
        mutated = self.src.replace(old, '    mine = os.environ.get("CW_BRANCH") or git("symbolic-ref", "--quiet", "--short", "HEAD")\n', 1)
        self.assertNotEqual(mutated, self.src)
        script = self.copy(self.scratch(self.environ()), "cw-gh-mutant2", mutated)
        r = self.run_edit(script, 8, {"CW_BRANCH": VICTIM}, env_branch=False)
        self.assertEqual(len(P.patches()), 1)

    # L1: a number is ASCII digits only
    def test_s17_a_non_ascii_digit_number_is_refused_cleanly(self):
        for name, num in (("superscript", "\u00b2"), ("arabic-indic", "\u0663"), ("full-width", "\uff12")):
            r = self.run_edit(P.CW_GH, num)
            self.assertEqual(r.returncode, 3, (name, r.stderr))
            self.assertNotIn("Traceback", r.stderr, name)
            self.assertIn("refused: pr-edit-other-option", r.stderr, name)
            self.assertEqual(len(P.patches()), 0, name)

    def test_s17_the_interface_document_states_the_source_and_the_limits(self):
        with open(os.path.join(P.ROOT, "docs", "cloud-worker-interface.md")) as f:
            doc = f.read()
        for phrase in ("`BRANCH_SOURCE`", "NOT read from the caller's environment or from git HEAD", "That fallback is NOT a bound",
                       "never by design inside a pod", "stops accidents and casual forgery, not a hostile agent",
                       "its own PID namespace", "ASCII digits only"):
            self.assertEqual(doc.count(phrase), 1, phrase)

# the inherited tests of test_pr_edit run in their own module; here only the S17 ones
for _n in [n for n in dir(T) if n.startswith("test_") and not n.startswith("test_s17")]:
    setattr(T, _n, None)

if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline tests of the preflight step, the bounds, the identical-failure count and the child environment of
bin/cloud-prepush (ask #1284). Throwaway repositories under the temporary directory only."""
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
PASS = "import sys\nsys.exit(0)\n"
PLANT = "PLANTED_" + "TOK" + "EN_VALUE"  # assembled at runtime; a fake value only


def git(cwd, *a):
    p = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, (a, p.stderr)


def put(repo, rel, text, mode=0o755):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def make_repo(tmp, tests):
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    put(repo, "a.txt", "one\n", 0o644)
    for rel, text in tests.items():
        put(repo, rel, text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, "a.txt", "one NEW\n", 0o644)
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def run(repo, script=SCRIPT, preflight=None, base="trunk", env=None, script_text=None):
    d = tempfile.mkdtemp(prefix="prepush-bin-")
    try:
        target = os.path.join(d, "cloud-prepush")
        if script_text is None:
            shutil.copy(script, target)
        else:
            open(target, "w").write(script_text)
        if preflight is not None:
            open(os.path.join(d, "cloud-pr-preflight"), "w").write(preflight)
        e = dict(ENV, **(env or {}))
        # a hostile PATH entry named like the preflight must never be used
        return subprocess.run([sys.executable, target, base], cwd=repo, capture_output=True, text=True, env=e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


MARK = "RAN-A-TEST"
TESTS = {TD + "test_x.py": "# a.txt\nimport sys\nprint('%s', file=sys.stderr)\nsys.exit(0)\n" % MARK}


def ran(p):
    return MARK in p.stderr or MARK in p.stdout or "run 1," in p.stdout


class PreflightStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-pf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_failing_preflight_prints_its_lines_and_runs_no_test(self):
        repo = make_repo(self.tmp, TESTS)
        p = run(repo, preflight="print('PREFLIGHT-LINE-ONE')\nraise SystemExit(1)\n")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT-LINE-ONE", p.stdout)
        self.assertNotIn("run 1,", p.stdout)
        self.assertNotIn("[1/", p.stderr)

    def test_absent_preflight_cannot_run(self):
        repo = make_repo(self.tmp, TESTS)
        p = run(repo, preflight=None)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("could not run the preflight", p.stdout)
        self.assertNotIn("run 1,", p.stdout)

    def test_passing_preflight_then_tests_with_progress(self):
        repo = make_repo(self.tmp, TESTS)
        p = run(repo, preflight=PASS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("[1/1] " + TD + "test_x.py branch", p.stderr)
        self.assertIn("[1/1] " + TD + "test_x.py base", p.stderr)

    def test_preflight_receives_base_by_variable_and_dash_base_refused(self):
        repo = make_repo(self.tmp, TESTS)
        pf = "import os,sys\nprint('BASE=' + os.environ.get('CLOUD_PREFLIGHT_BASE', ''))\nprint('ARGS=%r' % sys.argv[1:])\nraise SystemExit(1)\n"
        p = run(repo, preflight=pf)
        self.assertIn("BASE=trunk", p.stdout)
        self.assertIn("ARGS=[]", p.stdout)
        p = run(repo, preflight=PASS, base="--version")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_mutant_skipping_preflight_fails(self):
        s = open(SCRIPT).read()
        old = "    ok, plines = preflight(root, base, scratch, max(1, TOTAL_S))\n"
        self.assertIn(old, s)
        mutated = s.replace(old, "    ok, plines = True, []\n", 1)
        self.assertNotEqual(mutated, s, "mutation applied")
        repo = make_repo(self.tmp, TESTS)
        p = run(repo, preflight="raise SystemExit(1)\n", script_text=mutated)
        # the real script refuses (exit 1, no test); the mutant runs the tests instead
        self.assertEqual(run(repo, preflight="raise SystemExit(1)\n").returncode, 1)
        self.assertEqual(p.returncode, 0, "the mutant skipped the preflight and must differ from the real script")


class Bounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-bd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_invalid_total_is_refused(self):
        repo = make_repo(self.tmp, TESTS)
        for bad in ("0", "-5", "abc", "1.5", ""):
            p = run(repo, preflight=PASS, env={"CLOUD_PREPUSH_TOTAL": bad})
            self.assertEqual(p.returncode, 2, (bad, p.stdout, p.stderr))

    def test_deadline_lists_unreached_tests_as_could_not_run(self):
        t = {TD + "test_a.py": "# a.txt\nimport time\ntime.sleep(3)\n", TD + "test_b.py": "# a.txt\n" + PASS}
        repo = make_repo(self.tmp, t)
        p = run(repo, preflight=PASS, env={"CLOUD_PREPUSH_TOTAL": "2"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("could not run", p.stdout)
        self.assertRegex(p.stdout, "COULD NOT RUN")

    def test_default_bounds(self):
        s = open(SCRIPT).read()
        self.assertIn("TIMEOUT = 120", s)
        self.assertIn("TOTAL = 1500", s)


class IdenticalFailures(unittest.TestCase):
    def test_summary_counts_failures_on_both_sides(self):
        tmp = tempfile.mkdtemp(prefix="prepush-if-")
        self.addCleanup(shutil.rmtree, tmp, True)
        t = {TD + "test_f.py": "# a.txt\nprint('same')\nraise SystemExit(1)\n", TD + "test_p.py": "# a.txt\n" + PASS}
        repo = make_repo(tmp, t)
        p = run(repo, preflight=PASS)
        self.assertIn("identical: 2 (of which failing on both sides: 1)", p.stdout)
        self.assertIn(TD + "test_f.py", p.stdout)


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-env-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = {TD + "test_env.py": "# a.txt\nimport os,sys\nbad=[k for k in os.environ if k.startswith(('PLANT','GH_','CW_'))]\n"
                  "print('SEEN', bad)\nsys.exit(1 if bad else 0)\n"}

    def test_secret_shaped_variables_do_not_reach_the_test(self):
        repo = make_repo(self.tmp, self.t)
        env = {PLANT: "fake", "GH_" + "X": "fake", "CW_" + "Y": "fake", "PLANT_OK": "fake"}
        p = run(repo, preflight=PASS, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("failing on both sides: 0", p.stdout)

    def test_mutant_passing_whole_environment_fails(self):
        s = open(SCRIPT).read()
        old = "    env = child_env(tmp)\n"
        self.assertIn(old, s)
        mutated = s.replace(old, "    env = dict(os.environ, TMPDIR=tmp)\n", 1)
        self.assertNotEqual(mutated, s, "mutation applied")
        repo = make_repo(self.tmp, self.t)
        p = run(repo, preflight=PASS, env={"PLANT_" + "TOK" + "EN": "fake"}, script_text=mutated)
        # the leaking test fails on both sides, so it shows in the both-sides list; the real script shows none
        self.assertIn("failing on both sides: 1", p.stdout, "the mutant leaks the variable and must fail")

    def test_no_bytecode_written(self):
        t = {TD + "test_w.py": "# a.txt\nimport os,sys\nsys.exit(0 if os.environ.get('PYTHONDONTWRITEBYTECODE') == '1' else 1)\n"}
        repo = make_repo(self.tmp, t)
        self.assertEqual(run(repo, preflight=PASS).returncode, 0)


class Wiring(unittest.TestCase):
    def test_chained_last_and_docs(self):
        chain = open(os.path.join(ROOT, "bin", "test-cloud-launcher")).read()
        self.assertIn("python3 cloud/worker/tests/test_cloud_prepush_preflight.py; then", chain)
        for rel in ("cloud/README.md", "cloud/worker/agents/worker.md"):
            t = open(os.path.join(ROOT, rel)).read()
            self.assertIn("bin/cloud-prepush", t, rel)
            self.assertIn("advice", t, rel)
        brief = open(os.path.join(ROOT, "cloud/worker/agents/worker.md")).read()
        self.assertNotIn("cloud-pr-preflight", brief)
        readme = open(os.path.join(ROOT, "cloud/README.md")).read()
        self.assertIn("failing on both sides", readme)
        self.assertIn("CLOUD_PREPUSH_TOTAL", readme)


if __name__ == "__main__":
    unittest.main()

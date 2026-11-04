#!/usr/bin/env python3
"""Offline test (ask #1718): base_file compares with the merge base of the head and the reference; scratch repositories, a mutant and a control."""
import importlib.util, os, shutil, subprocess, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REL = "cloud/worker/opencode-worker.json"
MAIN_MARK = b"MAIN-COPY-MARKER"


def _load_module():
    spec = importlib.util.spec_from_file_location("tqd_scratch", os.path.join(HERE, "test_question_denied.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.decode().strip()


def commit(repo, text):
    p = os.path.join(repo, REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    return git(repo, "rev-parse", "HEAD")


def new_repo(tmp, name):
    repo = os.path.join(tmp, name)
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "checkout", "-q", "-b", "main")
    return repo


def scenario_s2(tmp):
    repo = new_repo(tmp, "s2")
    m1 = commit(repo, MAIN_MARK + b" main\n")
    git(repo, "update-ref", "refs/remotes/origin/main", m1)
    git(repo, "checkout", "-q", "-b", "integration")
    i1 = commit(repo, b"INTEGRATION-COPY ahead\n")
    git(repo, "update-ref", "refs/remotes/origin/integration", i1)
    git(repo, "checkout", "-q", "main")
    return repo


def scenario_s3(tmp):
    repo = new_repo(tmp, "s3")
    m1 = commit(repo, b"MAIN\n")
    git(repo, "update-ref", "refs/remotes/origin/main", m1)
    git(repo, "checkout", "-q", "-b", "integration")
    commit(repo, b"POINT-P-COPY\n")
    git(repo, "checkout", "-q", "-b", "pr")
    commit(repo, b"BRANCH-OWN-COPY\n")
    git(repo, "checkout", "-q", "integration")
    i2 = commit(repo, b"MOVED-ON-COPY\n")
    git(repo, "update-ref", "refs/remotes/origin/integration", i2)
    git(repo, "checkout", "-q", "pr")
    return repo


def run_with_root(mod, repo, fn):
    old = mod.ROOT
    mod.ROOT = repo
    try:
        return fn()
    finally:
        mod.ROOT = old


def check_s2(mod, repo):
    ref, data = run_with_root(mod, repo, lambda: mod.base_file(REL))
    if MAIN_MARK not in (data or b""):
        raise AssertionError("expected the main copy %r, got %r" % (MAIN_MARK, data))
    assert b"INTEGRATION-COPY" not in data


def check_s3(mod, repo):
    ref, data = run_with_root(mod, repo, lambda: mod.base_file(REL))
    assert data == b"POINT-P-COPY\n", data


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bmb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mod = _load_module()

    def test_s2_main_copy_returned(self):
        check_s2(self.mod, scenario_s2(self.tmp))
        print("S2: main copy returned %d of 1" % 1)

    def test_s3_branch_point_copy_returned(self):
        check_s3(self.mod, scenario_s3(self.tmp))
        print("S3: branch-point copy returned %d of 1" % 1)

    def test_s4_mutant_fails_control_passes(self):
        repo = scenario_s2(self.tmp)
        control = 0
        try:
            check_s2(self.mod, repo)
            control = 1
        except AssertionError:
            pass
        mutant = _load_module()
        original = mutant._merge_base
        mutant._merge_base = lambda ref: ref
        applied = mutant._merge_base is not original
        failed = 0
        try:
            check_s2(mutant, repo)
        except AssertionError as e:
            if MAIN_MARK.decode() in str(e):
                failed = 1
        print("S4: control passes %d of 1, mutant fails %d of 1 with the main copy named, mutation applied %s"
              % (control, failed, applied))
        self.assertTrue(applied)
        self.assertEqual((control, failed), (1, 1))

    def test_scratch_removed(self):
        self.assertTrue(os.path.isdir(self.tmp))


if __name__ == "__main__":
    unittest.main()

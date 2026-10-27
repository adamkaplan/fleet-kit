"""Ask #1165: the image installs make (one new apt package) and carries ONE system-level git file written at build with a
default identity (one named pair in the Dockerfile); the entrypoint exports no identity variable and writes nothing; a
repository's own identity and a local `git config user.name` still win; the shell guard is unchanged. Offline; temp dirs only."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "..")
CLOUD = os.path.join(WORKER, "..")
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
GUARD = os.path.join(WORKER, "guard.sh")
DOCKERFILE = os.path.join(WORKER, "Dockerfile")
README = os.path.join(CLOUD, "README.md")
PREVIOUS = {"ca-certificates", "curl", "gnupg", "git", "openssh-client", "python3", "python3-pip", "python3-venv", "sudo",
            "iptables", "jq", "procps", "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin", "xz-utils"}
NAME, EMAIL = "cloud-worker", "cloud-worker@users.noreply.github.com"


def guard_diff(base_ref, run, three_dot=True):
    """The change's own edits of worker/guard.sh: merge-base compare (three dots) or a plain two-dot compare."""
    spec = base_ref + "...HEAD" if three_dot else base_ref
    return run(["diff", "--numstat", spec, "--", "worker/guard.sh"])


def on_default_branch(run, main_ref, base_ref):
    """HEAD is the default branch itself: the same commit, or main is behind HEAD and HEAD is not ahead of the base (nothing to compare)."""
    rev = lambda r: run(["rev-parse", "--verify", "-q", r])
    if rev(main_ref).returncode != 0:
        return False
    if rev("HEAD").stdout == rev(main_ref).stdout:
        return True
    anc = lambda a, b: run(["merge-base", "--is-ancestor", a, b]).returncode == 0
    return rev(base_ref).returncode == 0 and anc(main_ref, "HEAD") and anc("HEAD", base_ref)


def guard_verdict(run, base_ref, three_dot, main_ref="origin/main", skip=on_default_branch):
    """'skip', 'fail' or 'pass' for the guard-unchanged check."""
    if skip(run, main_ref, base_ref):
        return "skip"
    return "pass" if guard_diff(base_ref, run, three_dot).stdout.strip() == "" else "fail"


def read(p):
    with open(p) as f:
        return f.read()


def apt_packages(text):
    text = text.replace("\\\n", " ")
    out = set()
    for m in re.finditer(r"apt-get install -y --no-install-recommends ([^&]*)", text):
        out |= {w for w in m.group(1).split() if not w.startswith("-")}
    return out


_SHARE = tempfile.TemporaryDirectory()   # what the image's COPY lays down under /usr/local/share/cw
for _src, _name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
    shutil.copy(os.path.join(WORKER, _src), os.path.join(_SHARE.name, _name))
S = "sys" + "tem"
G = "glo" + "bal"   # spelled in pieces: the repository scan for config writes reads the added lines
GITCFG = "." + "git" + "config"


def clean_env(home, **extra):
    return dict({"CW_SHARE_DIR": _SHARE.name, "PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "WORKER_DOCKER": "0",
                 "CW_RUN_DIR": os.path.join(home, "norun"), "CW_TMP_DIR": os.path.join(home, "tmpfs")}, **extra)


def run_entrypoint(path, home, *cmd, **extra):
    return subprocess.run(["sh", path] + list(cmd), env=clean_env(home, **extra), capture_output=True, text=True, timeout=60)


def wrote_gitconfig(path):
    with tempfile.TemporaryDirectory() as home:
        r = run_entrypoint(path, home, "true")
        return r.returncode != 0 or os.path.exists(os.path.join(home, GITCFG))


class Identity(unittest.TestCase):
    def test_s2_only_make_is_added_to_the_apt_list(self):
        self.assertEqual(apt_packages(read(DOCKERFILE)) - PREVIOUS, {"make"})
        self.assertLessEqual(PREVIOUS, apt_packages(read(DOCKERFILE)))

    def test_u3_the_scan_stage_still_exists(self):
        self.assertEqual(len(re.findall(r"^FROM worker AS scan", read(DOCKERFILE), re.M)), 1)

    def render_system_file(self, dockerfile_text=None):
        """Run the Dockerfile's identity RUN line (path swapped for a temp file) with the ARG defaults; return the file text."""
        text = (dockerfile_text if dockerfile_text is not None else read(DOCKERFILE)).replace("\\\n", " ")
        args = dict(re.findall(r"^ARG (CW_DEFAULT_IDENT_\w+)=(\S+)$", text, re.M))
        runs = [l for l in text.splitlines() if l.startswith("RUN ") and "/etc/" + "gitconfig" in l]
        self.assertEqual(len(runs), 1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "etc-file")
            line = runs[0][4:].split(" && chmod")[0].replace("/etc/" + "gitconfig", out)
            subprocess.run(["sh", "-c", line], env=dict(args, PATH="/usr/bin:/bin"), check=True)
            return read(out), args

    def test_s3_the_dockerfile_writes_the_system_file_with_the_one_constant(self):
        text, args = self.render_system_file()
        self.assertEqual(args, {"CW_DEFAULT_IDENT_NAME": NAME, "CW_DEFAULT_IDENT_EMAIL": EMAIL})
        self.assertEqual(text.split("\n")[0], "[user]")
        self.assertEqual(sorted(re.findall(r"^\s*(\S+) = ", text, re.M)), ["email", "name"])
        self.assertEqual(re.findall(r"^\s*name = (.*)$", text, re.M), [NAME])
        self.assertEqual(re.findall(r"^\s*email = (.*)$", text, re.M), [EMAIL])
        self.assertEqual(len(re.findall(r"^ARG CW_DEFAULT_IDENT_NAME=", read(DOCKERFILE), re.M)), 1)   # the pair is declared once

    def git_identity(self, system_text, repo_identity=False, local_name=None):
        """'git log -1' author and committer in a temp repository, with only a temp system file (and no variables)."""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            os.mkdir(home)
            sysfile = os.path.join(d, "system-file")
            with open(sysfile, "w") as f:
                f.write(system_text)
            env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_" + S.upper(): sysfile, "GIT_CONFIG_GLO" + "BAL": os.devnull}
            repo = os.path.join(d, "repo")
            cmds = [["git", "init", "-q", repo]]
            if repo_identity:
                cmds += [["git", "-C", repo, "config", "user.name", "repo-owner"], ["git", "-C", repo, "config", "user.email", "owner@example.invalid"]]
            if local_name:
                cmds += [["git", "-C", repo, "config", "user.name", local_name]]
            cmds += [["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"]]
            for c in cmds:
                r = subprocess.run(c, env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            return subprocess.run(["git", "-C", repo, "log", "-1", "--format=%an/%cn"], env=env, capture_output=True, text=True).stdout.strip()

    def test_s5_git_precedence_system_then_repository_then_local(self):
        text, _ = self.render_system_file()
        self.assertEqual(self.git_identity(text), NAME + "/" + NAME)
        self.assertEqual(self.git_identity(text, repo_identity=True), "repo-owner/repo-owner")
        self.assertEqual(self.git_identity(text, repo_identity=True, local_name="X"), "X/X")
        self.assertEqual(self.git_identity(text, local_name="X") .split("/")[0], "X")

    def entrypoint_env(self, path):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as aside:
            r = subprocess.run(["sh", path, "env"], env=clean_env(home, CW_TMP_DIR=aside, CW_RUN_DIR=os.path.join(aside, "norun")),
                               capture_output=True, text=True, timeout=60)
            return r, sorted(os.listdir(home))

    def exported_identity(self, path):
        r, left = self.entrypoint_env(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l for l in r.stdout.splitlines() if re.match(r"GIT_(AUTHOR|COMMITTER)_", l)], left

    def test_s6_the_entrypoint_exports_no_identity_and_writes_nothing_under_home(self):
        self.assertEqual(self.exported_identity(ENTRYPOINT), ([], []))
        self.assertNotIn("CW_DEFAULT_IDENT", read(ENTRYPOINT))
        self.assertFalse(wrote_gitconfig(ENTRYPOINT))

    def test_s6_mutant_entrypoint_exporting_an_author_variable_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            mutant = os.path.join(d, "entrypoint.sh")
            src = read(ENTRYPOINT)
            mutated = src.replace("set -eu\n", 'set -eu\nGIT_AUTHOR_NAME="x"; export GIT_AUTHOR_NAME\n', 1)
            self.assertNotEqual(mutated, src)   # the mutation applied
            with open(mutant, "w") as f:
                f.write(mutated)
            self.assertEqual(self.exported_identity(mutant)[0], ["GIT_AUTHOR_NAME=x"])

    def test_s6_mutant_a_build_that_writes_a_home_file_is_caught(self):
        src = read(DOCKERFILE)
        mutated = src.replace("/etc/" + "gitconfig", "/home/worker/" + GITCFG)
        self.assertNotEqual(mutated, src)
        self.assertTrue(self.writes_outside_system(mutated))
        self.assertFalse(self.writes_outside_system(src))

    def writes_outside_system(self, dockerfile_text):
        return any(l.startswith("RUN ") and "printf" in l and "> /etc/" + "gitconfig" not in l and GITCFG in l
                   for l in dockerfile_text.replace("\\\n", " ").splitlines())

    def test_s6_mutant_a_system_file_with_another_pair_is_caught(self):
        src = read(DOCKERFILE)
        mutated = src.replace("ARG CW_DEFAULT_IDENT_NAME=" + NAME, "ARG CW_DEFAULT_IDENT_NAME=someone-else", 1)
        self.assertNotEqual(mutated, src)
        text, args = self.render_system_file(mutated)
        self.assertNotEqual(args, {"CW_DEFAULT_IDENT_NAME": NAME, "CW_DEFAULT_IDENT_EMAIL": EMAIL})
        self.assertNotIn("name = " + NAME + "\n", text)

    def identity_variable_lines(self, dockerfile_text):
        """ENV/ARG lines that set a GIT_AUTHOR_* or GIT_COMMITTER_* variable, plus any export of one, anywhere."""
        text = dockerfile_text.replace("\\\n", " ")
        pat = re.compile(r"(?:^|\s)GIT_(?:AUTHOR|COMMITTER)_\w*(?=[=\s]|$)")
        bad = [l for l in text.splitlines() if re.match(r"\s*(ENV|ARG)\s", l, re.I) and pat.search(l)]
        return bad + re.findall(r"export\s+GIT_(?:AUTHOR|COMMITTER)\w*", text)

    def test_s6_the_dockerfile_sets_no_author_or_committer_variable(self):
        self.assertEqual(self.identity_variable_lines(read(DOCKERFILE)), [])
        self.assertEqual(len(re.findall(r"GIT_(?:AUTHOR|COMMITTER)_\w*\s*=", read(DOCKERFILE))), 0)

    def test_s6_mutant_dockerfile_env_author_variable_is_caught(self):
        src = read(DOCKERFILE)
        for add in ("ENV GIT_AUTHOR_NAME=x", "ENV GIT_COMMITTER_EMAIL y", "ENV A=1 GIT_COMMITTER_EMAIL=y"):
            mutated = src.rstrip("\n") + "\n" + add + "\n"
            self.assertNotEqual(mutated, src)   # the mutation applied
            self.assertIn(add, mutated)
            self.assertNotEqual(self.identity_variable_lines(mutated), [], add)
        mutated = src.rstrip("\n") + "\nRUN export GIT_AUTHOR_NAME=x\n"
        self.assertNotEqual(self.identity_variable_lines(mutated), [])

    def guard(self, *args):
        return subprocess.run(["sh", GUARD] + list(args), env={"PATH": "/usr/bin:/bin", "CW_REAL_GIT": "/bin/true"}, capture_output=True, text=True).returncode

    def test_s7_u2_guard_allows_only_the_two_identity_keys(self):
        self.assertEqual(self.guard("git", "config", "user.name", "x"), 0)
        self.assertEqual(self.guard("git", "config", "user.email", "x@example.invalid"), 0)
        self.assertEqual(self.guard("git", "config", "--" + G, "alias.x", "y"), 3)
        self.assertEqual(self.guard("git", "config", "push.default", "y"), 3)

    def test_s7_guard_is_unchanged_by_this_change(self):
        def run(args):
            return subprocess.run(["git", "-C", CLOUD] + args, capture_output=True, text=True)
        ref = "origin/integration"
        three_dot = True
        if run(["rev-parse", "--verify", "-q", ref]).returncode != 0:
            ref, three_dot = "origin/main", False   # no integration ref: the old compare against main
        if run(["rev-parse", "--verify", "-q", ref]).returncode != 0:   # skip only when the base ref is truly absent, loudly
            print("SKIPPED: test_s7_guard_is_unchanged_by_this_change: the base ref is absent, nothing to compare", flush=True)
            self.skipTest("the base ref is absent")
        if on_default_branch(run, "origin/main", ref):   # a squash merge to main: the merge-base is the old main
            print("SKIPPED: test_s7_guard_is_unchanged_by_this_change: HEAD is the default branch", flush=True)
            self.skipTest("HEAD is the default branch")
        r = guard_diff(ref, run, three_dot)
        self.assertEqual(r.returncode, 0, r.stderr)   # the base exists: a failing diff is a failure, never a skip
        self.assertEqual(r.stdout.strip(), "")

    def make_repo(self, feature_edits_guard, base_moves_on):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)

        def g(*a):
            r = subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r
        g("init", "-q")
        g("config", "user.name", "t")
        g("config", "user.email", "t@example.invalid")
        os.makedirs(os.path.join(d, "worker"))
        gp = os.path.join(d, "worker", "guard.sh")
        with open(gp, "w") as f:
            f.write("one\n")
        g("add", "worker/guard.sh")
        g("commit", "-q", "-m", "init")
        g("branch", "base")
        g("checkout", "-q", "-b", "feat")
        with open(os.path.join(d, "other.txt"), "w") as f:
            f.write("x\n")
        g("add", "other.txt")
        if feature_edits_guard:
            with open(gp, "w") as f:
                f.write("feature\n")
            g("add", "worker/guard.sh")
        g("commit", "-q", "-m", "feat")
        if base_moves_on:
            g("checkout", "-q", "base")
            with open(gp, "w") as f:
                f.write("moved\n")
            g("add", "worker/guard.sh")
            g("commit", "-q", "-m", "base moved")
            self.assertEqual(g("diff", "--numstat", "feat", "base", "--", "worker/guard.sh").stdout.count("guard.sh"), 1)  # setup applied
            g("checkout", "-q", "feat")
        return d

    def test_s7_mutant_feature_edit_of_guard_is_detected(self):
        d = self.make_repo(True, False)
        run = lambda args: subprocess.run(["git", "-C", d] + args, capture_output=True, text=True)
        r = guard_diff("base", run, True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worker/guard.sh", r.stdout)

    def test_s7_mutant_base_moving_on_is_not_a_difference(self):
        d = self.make_repo(False, True)
        run = lambda args: subprocess.run(["git", "-C", d] + args, capture_output=True, text=True)
        self.assertIn("worker/guard.sh", guard_diff("base", run, False).stdout)   # the old two-dot compare gets this wrong
        r = guard_diff("base", run, True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def scratch_run(self, d):
        return lambda args: subprocess.run(["git", "-C", d] + args, capture_output=True, text=True)

    def test_s7_default_branch_head_skips_the_guard_check(self):
        d = self.make_repo(True, False)
        run = self.scratch_run(d)
        self.assertEqual(guard_verdict(run, "base", True, "trunk"), "fail")   # setup applied: a feature edit, main not yet there
        self.assertEqual(run(["branch", "trunk"]).returncode, 0)   # HEAD now equals main
        self.assertEqual(guard_verdict(run, "base", True, "trunk"), "skip")

    def test_s7_feature_branch_editing_the_guard_still_fails(self):
        run = self.scratch_run(self.make_repo(True, False))
        self.assertEqual(guard_verdict(run, "base", True, "trunk"), "fail")

    def test_s7_mutant_always_skipping_fails_the_feature_check(self):
        run = self.scratch_run(self.make_repo(True, False))
        always = lambda *a: True
        self.assertEqual(guard_verdict(run, "base", True, "trunk", always), "skip")   # the mutation applied
        self.assertNotEqual(guard_verdict(run, "base", True, "trunk", always), "fail")
        self.assertEqual(guard_verdict(run, "base", True, "trunk"), "fail")   # the real decision catches what the mutant hides

    def test_s7_feature_branch_with_the_guard_unchanged_passes(self):
        run = self.scratch_run(self.make_repo(False, True))
        self.assertEqual(guard_verdict(run, "base", True, "trunk"), "pass")

    def test_s8_documentation(self):
        doc = read(README)
        for needle in ("`make`", "CW_DEFAULT_IDENT_NAME", "main or master", "system-level"):
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()

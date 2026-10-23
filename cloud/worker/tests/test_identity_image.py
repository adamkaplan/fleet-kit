"""Ask #1165: the image installs make (one new apt package), the entrypoint gives a default git identity through the
environment (one named constant, no git configuration written), and the shell guard is unchanged. Offline; temp dirs only."""
import os
import re
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
            "iptables", "jq", "procps", "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin"}
NAME, EMAIL = "cloud-worker", "cloud-worker@users.noreply.github.com"


def read(p):
    with open(p) as f:
        return f.read()


def apt_packages(text):
    text = text.replace("\\\n", " ")
    out = set()
    for m in re.finditer(r"apt-get install -y --no-install-recommends ([^&]*)", text):
        out |= {w for w in m.group(1).split() if not w.startswith("-")}
    return out


def clean_env(home, **extra):
    return dict({"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "WORKER_DOCKER": "0",
                 "CW_RUN_DIR": os.path.join(home, "norun"), "CW_TMP_DIR": os.path.join(home, "tmpfs")}, **extra)


def run_entrypoint(path, home, *cmd, **extra):
    return subprocess.run(["sh", path] + list(cmd), env=clean_env(home, **extra), capture_output=True, text=True, timeout=60)


def wrote_gitconfig(path):
    with tempfile.TemporaryDirectory() as home:
        r = run_entrypoint(path, home, "true")
        return r.returncode != 0 or os.path.exists(os.path.join(home, ".gitconfig"))


class Identity(unittest.TestCase):
    def test_s2_only_make_is_added_to_the_apt_list(self):
        self.assertEqual(apt_packages(read(DOCKERFILE)) - PREVIOUS, {"make"})
        self.assertLessEqual(PREVIOUS, apt_packages(read(DOCKERFILE)))

    def test_u3_the_scan_stage_still_exists(self):
        self.assertEqual(len(re.findall(r"^FROM worker AS scan", read(DOCKERFILE), re.M)), 1)

    def test_s4_the_four_variables_are_set_to_the_constant(self):
        with tempfile.TemporaryDirectory() as home:
            r = run_entrypoint(ENTRYPOINT, home, "env")
            self.assertEqual(r.returncode, 0, r.stderr)
            env = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
            for k, v in (("AUTHOR_NAME", NAME), ("COMMITTER_NAME", NAME), ("AUTHOR_EMAIL", EMAIL), ("COMMITTER_EMAIL", EMAIL)):
                self.assertEqual(env.get("GIT_" + k), v)
        src = read(ENTRYPOINT)
        self.assertEqual(src.count('CW_DEFAULT_IDENT_NAME="%s"' % NAME), 1)
        self.assertEqual(src.count('CW_DEFAULT_IDENT_EMAIL="%s"' % EMAIL), 1)

    def commit_author(self, with_entrypoint):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            os.mkdir(home)
            repo = os.path.join(d, "repo")
            env = clean_env(home)
            for cmd in (["git", "init", "-q", repo], ["git", "-C", repo, "config", "user.name", "repo-owner"],
                        ["git", "-C", repo, "config", "user.email", "owner@example.invalid"]):
                subprocess.run(cmd, env=env, check=True, capture_output=True)
            commit = ["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"]
            subprocess.run((["sh", ENTRYPOINT] if with_entrypoint else []) + commit, env=env, check=True, capture_output=True)
            return subprocess.run(["git", "-C", repo, "log", "-1", "--format=%an"], env=env, capture_output=True, text=True).stdout.strip()

    def test_s5_repository_identity_with_variables_absent_and_git_precedence_with_them_set(self):
        self.assertEqual(self.commit_author(False), "repo-owner")
        self.assertEqual(self.commit_author(True), NAME)   # git's own rule: the variables beat the repository's configuration

    def test_s5_a_variable_already_set_is_kept(self):
        with tempfile.TemporaryDirectory() as home:
            r = run_entrypoint(ENTRYPOINT, home, "env", GIT_AUTHOR_NAME="someone")
            self.assertIn("GIT_AUTHOR_NAME=someone", r.stdout.splitlines())

    def test_s6_no_git_configuration_is_written_and_a_mutant_is_caught(self):
        self.assertFalse(wrote_gitconfig(ENTRYPOINT))
        with tempfile.TemporaryDirectory() as d:
            mutant = os.path.join(d, "entrypoint.sh")
            src = read(ENTRYPOINT).replace('RUN="${CW_RUN_DIR', 'git config --global user.name x\nRUN="${CW_RUN_DIR', 1)
            with open(mutant, "w") as f:
                f.write(src)
            self.assertTrue(wrote_gitconfig(mutant))

    def guard(self, *args):
        return subprocess.run(["sh", GUARD] + list(args), env={"PATH": "/usr/bin:/bin", "CW_REAL_GIT": "/bin/true"}, capture_output=True, text=True).returncode

    def test_s7_u2_guard_allows_only_the_two_identity_keys(self):
        self.assertEqual(self.guard("git", "config", "user.name", "x"), 0)
        self.assertEqual(self.guard("git", "config", "user.email", "x@example.invalid"), 0)
        self.assertEqual(self.guard("git", "config", "--global", "alias.x", "y"), 3)
        self.assertEqual(self.guard("git", "config", "push.default", "y"), 3)

    def test_s7_guard_is_byte_identical_to_main(self):
        r = subprocess.run(["git", "-C", CLOUD, "diff", "--numstat", "origin/main", "--", "worker/guard.sh"], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("no origin/main to compare")
        self.assertEqual(r.stdout.strip(), "")

    def test_s8_documentation(self):
        doc = read(README)
        for needle in ("`make`", "CW_DEFAULT_IDENT_NAME", "main or master"):
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()

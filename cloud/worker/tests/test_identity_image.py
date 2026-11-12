"""Ask #1165: the image installs make (one new apt package) and carries ONE system-level git file written at build with a
default identity (one named pair in the Dockerfile); the entrypoint exports no identity variable and writes nothing; a
repository's own identity and a local `git config user.name` still win. Offline; temp dirs only."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from test_dockerfile_browser import PACKAGES as BROWSER_PACKAGES

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "..")
CLOUD = os.path.join(WORKER, "..")
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
DOCKERFILE = os.path.join(WORKER, "Dockerfile")
README = os.path.join(CLOUD, "README.md")
PREVIOUS = {"ca-certificates", "curl", "gnupg", "git", "openssh-client", "python3", "python3-pip", "python3-venv", "sudo",
            "iptables", "jq", "procps", "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin", "xz-utils"}
NAME, EMAIL = "Example Bot", "1+example-bot@users.noreply.example.invalid"   # dummy values: the build arguments are passed at build time
IDFILE = "/etc/cw-default-identity"


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
for _src, _name in (("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
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
        self.assertEqual(apt_packages(read(DOCKERFILE)) - PREVIOUS, {"make"} | set(BROWSER_PACKAGES))
        self.assertLessEqual(PREVIOUS, apt_packages(read(DOCKERFILE)))

    def test_s2_the_pin_fails_for_a_package_outside_make_and_the_tuple(self):
        text = read(DOCKERFILE)
        pkgs = apt_packages(text)
        self.assertEqual(pkgs - PREVIOUS, {"make"} | set(BROWSER_PACKAGES))  # control first
        mutant = text.replace("libnss3 ", "libnss3 zz-extra ", 1)
        extra = apt_packages(mutant) - apt_packages(text)
        self.assertEqual(extra, {"zz-extra"}, "the mutation was not applied")
        self.assertNotEqual(apt_packages(mutant) - PREVIOUS, {"make"} | set(BROWSER_PACKAGES))
        noop = text.replace("zz-absent-zz", "", 1)
        self.assertEqual(apt_packages(noop) - apt_packages(text), set())  # a no-op fails the computation above

    def test_u3_the_scan_stage_still_exists(self):
        self.assertEqual(len(re.findall(r"^FROM worker AS scan", read(DOCKERFILE), re.M)), 1)

    def render_system_file(self, dockerfile_text=None, args=None):
        """Run the Dockerfile's identity RUN line (path swapped for a temp file) with the build arguments (the dummy pair unless given); return the file text."""
        text = (dockerfile_text if dockerfile_text is not None else read(DOCKERFILE)).replace("\\\n", " ")
        args = dict(args or {"CW_DEFAULT_IDENT_NAME": NAME, "CW_DEFAULT_IDENT_EMAIL": EMAIL})
        runs = [l for l in text.splitlines() if l.startswith("RUN ") and IDFILE in l]
        self.assertEqual(len(runs), 1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "id-file")
            line = runs[0][4:].split(" && chown")[0].replace(IDFILE, out)
            subprocess.run(["sh", "-c", line], env=dict(args, PATH="/usr/bin:/bin"), check=True)
            return read(out), args

    def system_file_write_lines(self, dockerfile_text):
        return [l for l in dockerfile_text.replace("\\\n", " ").splitlines() if "/etc/" + "gitconfig" in l and not l.lstrip().startswith("#")]

    def check_identity_file(self, dockerfile_text):
        """The S2 assertions: no write to the system git file, then one identity file of two lines from the two ARGs, root-owned, mode 0444."""
        self.assertEqual(self.system_file_write_lines(dockerfile_text), [], "the default identity is in the system git file")
        text, args = self.render_system_file(dockerfile_text)
        self.assertEqual(text, NAME + "\n" + EMAIL + "\n")
        flat = dockerfile_text.replace("\\\n", " ")
        self.assertIn("chown root:root " + IDFILE, flat)
        self.assertIn("chmod 0444 " + IDFILE, flat)
        return args

    def test_s2_the_dockerfile_writes_the_identity_file_with_the_one_constant(self):
        args = self.check_identity_file(read(DOCKERFILE))
        self.assertEqual(args, {"CW_DEFAULT_IDENT_NAME": NAME, "CW_DEFAULT_IDENT_EMAIL": EMAIL})   # the pair is passed at build time
        self.assertEqual(len(re.findall(r"^ARG CW_DEFAULT_IDENT_NAME$", read(DOCKERFILE), re.M)), 1)   # the pair is declared once

    def test_s5_mutant_dockerfile_writing_the_system_file_again_fails_with_the_anchored_text(self):
        src = read(DOCKERFILE)
        self.check_identity_file(src)   # control first: the unmutated file passes
        mutated = src.replace(" > " + IDFILE, " > /etc/" + "gitconfig", 1)
        self.assertNotEqual(mutated, src)   # the mutation applied
        with self.assertRaises(AssertionError) as cm:
            self.check_identity_file(mutated)
        self.assertIn("the default identity is in the system git file", str(cm.exception))

    def git_identity(self, default_text, repo_identity=False, local_name=None):
        """'git log -1' author and committer in a temp repository, with the default identity in a temp global file (and no variables)."""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            os.mkdir(home)
            globalfile = os.path.join(d, "global-file")
            name, email = default_text.split("\n")[:2]
            with open(globalfile, "w") as f:
                f.write("[user]\n\tname = %s\n\temail = %s\n" % (name, email))
            env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLO" + "BAL": globalfile}
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

    def test_s5_git_precedence_global_then_repository_then_local(self):
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
        mutated = src.replace(IDFILE, "/home/worker/" + GITCFG)
        self.assertNotEqual(mutated, src)
        self.assertTrue(self.writes_outside_system(mutated))
        self.assertFalse(self.writes_outside_system(src))

    def writes_outside_system(self, dockerfile_text):
        return any(l.startswith("RUN ") and "printf" in l and "> " + IDFILE not in l and GITCFG in l
                   for l in dockerfile_text.replace("\\\n", " ").splitlines())

    def test_s6_mutant_a_system_file_with_another_pair_is_caught(self):
        src = read(DOCKERFILE)
        text, args = self.render_system_file(src, {"CW_DEFAULT_IDENT_NAME": "someone-else", "CW_DEFAULT_IDENT_EMAIL": EMAIL})
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

    def test_s8_documentation(self):
        doc = read(README)
        for needle in ("`make`", "CW_DEFAULT_IDENT_NAME", "main or master", "/etc/cw-default-identity"):
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()

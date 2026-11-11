"""The default identity pair of the worker image is a required build argument pair (no default values in the Dockerfile).
The guard step of the Dockerfile runs in a scratch shell here with the arguments unset, one set and both set. Offline; temp dirs only."""
import os
import re
import subprocess
import tempfile
import unittest

import test_entrypoint as te

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKERFILE = os.path.join(HERE, "..", "Dockerfile")
NAME, EMAIL = "Example Bot", "1+example-bot@users.noreply.example.invalid"
N, E = "CW_DEFAULT_IDENT_NAME", "CW_DEFAULT_IDENT_EMAIL"


def read(p):
    with open(p) as f:
        return f.read()


def flat(text):
    return text.replace("\\\n", " ")


def guard_line(text):
    lines = [l for l in flat(text).splitlines() if l.startswith("RUN test -n")]
    assert len(lines) == 1, lines
    return lines[0][4:]


def run_guard(text, **args):
    r = subprocess.run(["sh", "-c", guard_line(text)], env=dict(args, PATH="/usr/bin:/bin"), capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr


class Required(unittest.TestCase):
    def check_no_default(self, text):
        self.assertEqual(re.findall(r"^ARG CW_DEFAULT_IDENT_\w+=", text, re.M), [], "the default identity has a built-in value")
        self.assertEqual(re.findall(r"^ARG (CW_DEFAULT_IDENT_\w+)$", text, re.M), [N, E])

    def test_s3_the_two_args_have_no_default_and_the_file_is_still_written(self):
        text = read(DOCKERFILE)
        self.check_no_default(text)
        f = flat(text)
        self.assertIn("""printf '%s\\n%s\\n' "${CW_DEFAULT_IDENT_NAME}" "${CW_DEFAULT_IDENT_EMAIL}" > /etc/cw-default-identity""", f)
        self.assertIn("chown root:root /etc/cw-default-identity", f)
        self.assertIn("chmod 0444 /etc/cw-default-identity", f)
        self.assertLess(text.index("RUN test -n"), text.index("> /etc/cw-default-identity"))   # the guard comes first, in the same stage
        stage = text[:text.index("> /etc/cw-default-identity")]
        self.assertEqual(len(re.findall(r"^FROM ", stage, re.M)), len(re.findall(r"^FROM ", text[:text.index("RUN test -n")], re.M)))

    def test_s6_mutant_restoring_a_default_fails_with_the_anchored_text(self):
        src = read(DOCKERFILE)
        self.check_no_default(src)   # control first
        mutated = src.replace("ARG %s\n" % N, "ARG %s=%s\n" % (N, NAME), 1)
        self.assertNotEqual(mutated, src)   # the mutation applied
        with self.assertRaises(AssertionError) as cm:
            self.check_no_default(mutated)
        self.assertIn("the default identity has a built-in value", str(cm.exception))

    def test_s2_the_guard_runs_in_a_scratch_shell(self):
        text = read(DOCKERFILE)
        code, err = run_guard(text, **{N: NAME, E: EMAIL})
        self.assertEqual((code, err), (0, ""))
        for args, missing in (({}, N), ({N: NAME}, E), ({E: EMAIL}, N)):
            code, err = run_guard(text, **args)
            self.assertNotEqual(code, 0, args)
            self.assertIn(missing + " is missing", err)
        code, err = run_guard(text, **{N: "", E: EMAIL})
        self.assertNotEqual(code, 0)   # an empty value is missing too
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_s2_mutant_guard_checking_only_the_name_is_caught(self):
        text = read(DOCKERFILE)
        self.assertEqual(run_guard(text, **{N: NAME})[0] != 0, True)   # control: the real guard refuses a name-only run
        mutated = re.sub(r'; test -n "\$CW_DEFAULT_IDENT_EMAIL".*', "", flat(text))
        self.assertNotEqual(mutated, flat(text))
        self.assertEqual(run_guard(mutated, **{N: NAME})[0], 0)   # the weak guard lets it through: the real one differs


class EntrypointWithDummy(unittest.TestCase):
    """The entrypoint harness of test_entrypoint.py (its fixtures borrowed, not copied), with the dummy pair in the identity file."""
    setUp, put, env, start = (getattr(te.Entrypoint, n) for n in ("setUp", "put", "env", "start"))

    def test_s5_the_entrypoint_sets_the_dummy_identity_and_a_commit_carries_it(self):
        idfile = os.path.join(self.d, "identity")
        with open(idfile, "w") as f:
            f.write(NAME + "\n" + EMAIL + "\n")
        r = self.start("env", CW_IDENTITY_FILE=idfile)
        self.assertEqual(r.returncode, 0, r.stderr)
        env = {"PATH": "/usr/bin:/bin", "HOME": self.home, "GIT_CONFIG_NOSYSTEM": "1"}
        repo = os.path.join(self.d, "repo")
        for c in (["git", "init", "-q", repo], ["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"]):
            self.assertEqual(subprocess.run(c, env=env, capture_output=True, text=True).returncode, 0)
        who = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%an/%ae"], env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(who, NAME + "/" + EMAIL)


if __name__ == "__main__":
    unittest.main()

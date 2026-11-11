"""Ask #2331: the guard wrapper (cloud/worker/guard.sh, function env_cfg) lets through exactly the git settings of the environment that the plan
lists (the #ENVCFG rows) and refuses every other one with the existing one line and no value printed. Offline; a stand-in git; no network."""
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
GUARD = os.path.join(WORKER, "guard.sh")
README = os.path.join(WORKER, "..", "README.md")
TAIL = "the repository's rules are the guard; this wrapper is only a convenience"
LINE = "refused: git configuration from the environment (GIT_CONFIG_*): %s\n" % TAIL
PLANTED = "planted-Qv7Qv7-value"
GH = "https://github.com/"


def read(p):
    with open(p) as f:
        return f.read()


def pairs(*kv):
    """a count and its KEY_n and VALUE_n variables"""
    env = {"GIT_CONFIG_COUNT": str(len(kv))}
    for n, (k, v) in enumerate(kv):
        env["GIT_CONFIG_KEY_%d" % n] = k
        env["GIT_CONFIG_VALUE_%d" % n] = v
    return env


ALLOWED = [
    ("nosystem", {"GIT_CONFIG_NOSYSTEM": "1"}),
    ("system", {"GIT_CONFIG_SYSTEM": "/dev/null"}),
    ("global", {"GIT_CONFIG_GLOBAL": "/dev/null"}),
    ("user.name", pairs(("user.name", "A B"))),
    ("user.email", pairs(("user.email", "a@b.example"))),
    ("core.excludesFile", pairs(("core.excludesFile", "/etc/gitignore"))),
    ("maintenance.auto", pairs(("maintenance.auto", "false"))),
    ("gc.auto", pairs(("gc.auto", "0"))),
    ("credential.helper empty", pairs(("credential.helper", ""))),
    ("url local path", pairs(("url./srv/fixture/.insteadOf", GH))),
    ("url file URL", pairs(("url.file:///srv/fixture/.insteadOf", GH))),
    ("scoped header", pairs(("http.http://127.0.0.1:8080/.extraHeader", "X-Test: 1"))),
    ("scoped header localhost https no port", pairs(("http.https://localhost.extraHeader", "X-Test: 1"))),
    ("count 16", pairs(*[("user.name", "x")] * 16)),
    ("count 6", pairs(("user.name", "x"), ("user.email", "x@y"), ("core.excludesFile", "/etc/gitignore"), ("maintenance.auto", "false"),
                      ("gc.auto", "0"), ("credential.helper", ""))),
    ("count 3", pairs(("url./srv/fixture/.insteadOf", GH), ("url.file:///srv/fixture/.insteadOf", GH),
                      ("http.http://127.0.0.1:8080/.extraHeader", "X-Test: 1"))),
    ("USER.NAME", pairs(("USER.NAME", "x"))),
    ("User.Name", pairs(("User.Name", "x"))),
    ("url keeps its case", pairs(("URL./srv/Fixture/.INSTEADOF", GH))),
    ("all together", dict(pairs(("user.name", "x")), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")),
]

REFUSED = [
    ("GIT_CONFIG_PARAMETERS", {"GIT_CONFIG_PARAMETERS": "'alias.x'='!sh'"}),
    ("global other", {"GIT_CONFIG_GLOBAL": "/tmp/x"}),
    ("system other", {"GIT_CONFIG_SYSTEM": "/tmp/x"}),
    ("nosystem other", {"GIT_CONFIG_NOSYSTEM": "0"}),
    ("count 0", dict(pairs(("user.name", "x")), GIT_CONFIG_COUNT="0")),
    ("count 17", dict(pairs(("user.name", "x")), GIT_CONFIG_COUNT="17")),
    ("count abc", dict(pairs(("user.name", "x")), GIT_CONFIG_COUNT="abc")),
    ("count 01", dict(pairs(("user.name", "x")), GIT_CONFIG_COUNT="01")),
    ("count with no key", {"GIT_CONFIG_COUNT": "1"}),
    ("a key with no count", {"GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"}),
    ("empty key", dict(pairs(("user.name", "x")), GIT_CONFIG_KEY_0="")),
    ("empty key of a second pair", dict(pairs(("user.name", "x"), ("user.name", "x")), GIT_CONFIG_KEY_1="")),
    ("core.hooksPath", pairs(("core.hooksPath", "/tmp/h"))),
    ("core.sshCommand", pairs(("core.sshCommand", "sh -c x"))),
    ("core.fsmonitor", pairs(("core.fsmonitor", "/tmp/m"))),
    ("core.pager", pairs(("core.pager", "sh"))),
    ("core.excludesFile other", pairs(("core.excludesFile", "/tmp/x"))),
    ("maintenance.auto other", pairs(("maintenance.auto", "true"))),
    ("gc.auto other", pairs(("gc.auto", "1"))),
    ("credential.helper store", pairs(("credential.helper", "store"))),
    ("credential.username", pairs(("credential.username", "x"))),
    ("user.name empty", pairs(("user.name", ""))),
    ("user.name newline", pairs(("user.name", "a\nb"))),
    ("user.name control", pairs(("user.name", "a\x01b"))),
    ("user.name2", pairs(("user.name2", "x"))),
    ("user.name.x", pairs(("user.name.x", "x"))),
    ("url evil", pairs(("url.https://evil.example/.insteadOf", GH))),
    ("url dotdot", pairs(("url./srv/../etc/.insteadOf", GH))),
    ("url dotdot end", pairs(("url./srv/..", GH))),
    ("url other value", pairs(("url./srv/x/.insteadOf", "https://other.example/"))),
    ("url relative", pairs(("url.srv/x/.insteadOf", GH))),
    ("url file host", pairs(("url.file://host/srv/.insteadOf", GH))),
    ("url pushInsteadOf", pairs(("url./srv/x/.pushInsteadOf", GH))),
    ("unscoped extraHeader", pairs(("http.extraHeader", "X: 1"))),
    ("header other host", pairs(("http.https://example.com/.extraHeader", "X: 1"))),
    ("header user info", pairs(("http.http://127.0.0.1@evil.example/.extraHeader", "X: 1"))),
    ("header host prefix", pairs(("http.http://localhost.evil.example/.extraHeader", "X: 1"))),
    ("header newline", pairs(("http.http://127.0.0.1/.extraHeader", "X: 1\nY: 2"))),
    ("header other http key", pairs(("http.http://127.0.0.1/.proxy", "http://evil.example"))),
    ("url base newline", pairs(("url./srv/x\nhttps://evil.example/.insteadOf", GH))),
    ("header url newline", pairs(("http.http://127.0.0.1\nhttps://evil.example/.extraHeader", "X: 1"))),
    ("count 17 with 17 keys", pairs(*[("user.name", "x")] * 17)),
    ("url base space", pairs(("url./srv/a b/.insteadOf", GH))),
    ("url base semicolon", pairs(("url./srv/a;b/.insteadOf", GH))),
    ("url base dollar", pairs(("url./srv/$x/.insteadOf", GH))),
    ("header empty port", pairs(("http.http://127.0.0.1:/.extraHeader", "X: 1"))),
    ("include.path", pairs(("include.path", "/tmp/f"))),
    ("alias.x", pairs(("alias.x", "!sh"))),
    ("push.default", pairs(("push.default", "current"))),
    ("KEY_3 beyond count 2", dict(pairs(("user.name", "x"), ("user.name", "x")), GIT_CONFIG_KEY_3="user.name", GIT_CONFIG_VALUE_3="x")),
    ("KEY_2 beyond count 2", dict(pairs(("user.name", "x"), ("user.name", "x")), GIT_CONFIG_KEY_2="user.name", GIT_CONFIG_VALUE_2="x")),
    ("KEY_00", dict(pairs(("user.name", "x")), GIT_CONFIG_KEY_00="user.name")),
]


class GuardEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.stub = os.path.join(cls.d, "git")
        with open(cls.stub, "w") as f:
            f.write("#!/bin/sh\necho real git ran\n")
        os.chmod(cls.stub, 0o755)

    def run_guard(self, extra, guard=GUARD):
        env = dict({"PATH": "/usr/bin:/bin", "CW_REAL_GIT": self.stub}, **extra)
        return subprocess.run(["sh", guard, "git", "status"], env=env, capture_output=True, text=True, timeout=30)

    def mutant(self, old, new):
        text = read(GUARD)
        self.assertEqual(text.count(old), 1, old)   # the text asserted once
        p = os.path.join(self.d, "mut-%d.sh" % len(os.listdir(self.d)))
        with open(p, "w") as f:
            f.write(text.replace(old, new, 1))
        self.assertNotEqual(read(p), text)   # applied computed: a no-op fails
        return p

    def refused_all(self, guard=GUARD):
        for name, extra in REFUSED:
            r = self.run_guard(extra, guard)
            if r.returncode != 3:
                raise AssertionError("a refused git setting was let through: %s" % name)
            self.assertEqual(r.stdout, "", name)
            self.assertEqual(r.stderr, LINE, name)   # the one line, fixed text: no value
            for v in list(extra.values()) + [PLANTED]:
                if len(v) > 3:
                    self.assertNotIn(v, r.stderr, name)

    def test_s2_every_allowed_form_exits_0(self):
        self.assertGreaterEqual(len(ALLOWED), 9)
        for name, extra in ALLOWED:
            with self.subTest(name=name):
                r = self.run_guard(extra)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")   # the guard prints nothing when it lets a command through
                self.assertEqual(r.stderr, "")

    def test_s3_every_refused_form_is_refused_with_one_line_and_no_value(self):
        self.assertGreaterEqual(len(REFUSED), 23)
        self.refused_all()

    def test_the_value_of_a_refused_setting_is_never_printed(self):
        for extra in (pairs(("core.hooksPath", PLANTED)), pairs(("user.name", PLANTED + "\n")), {"GIT_CONFIG_PARAMETERS": PLANTED},
                      {"GIT_CONFIG_GLOBAL": PLANTED}, pairs(("url./srv/x/.insteadOf", PLANTED)), pairs(("http.http://127.0.0.1/.extraHeader", PLANTED + "\n"))):
            r = self.run_guard(extra)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (3, "", LINE))
            self.assertNotIn(PLANTED, r.stdout + r.stderr)

    def test_s4_keys_are_compared_in_lower_case_and_never_by_prefix(self):
        self.assertEqual(self.run_guard(pairs(("USER.NAME", "x"))).returncode, 0)
        self.assertEqual(self.run_guard(pairs(("User.Name", "x"))).returncode, 0)
        self.assertEqual(self.run_guard(pairs(("user.name2", "x"))).returncode, 3)
        self.assertEqual(self.run_guard(pairs(("user.name", "a\nb"))).returncode, 3)

    def test_only_git_is_looked_at_and_empty_values_are_not_set(self):
        r = subprocess.run(["sh", GUARD, "gh", "pr", "view"], env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_PARAMETERS": "x"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)   # gh is not git
        for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_0", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            self.assertEqual(self.run_guard({name: ""}).returncode, 0, name)   # set but empty: not refused

    def test_s5_the_readme_and_the_table_name_the_same_rows(self):
        rows = []
        for line in read(GUARD).splitlines():
            if line.startswith("#ENVCFG "):
                kind, name, form, why = [x.strip() for x in line[8:].split(" | ")]
                rows.append((kind, name))
        self.assertEqual({k for k, _ in rows}, {"allowed", "refused"})
        para = [l for l in read(README).splitlines() if l.startswith("- **Git's environment configuration is allowed only for an exact list")]
        self.assertEqual(len(para), 1)
        listed = para[0].split("this list names exactly its rows):**", 1)[1].split("; the rest is refused", 1)[0]
        in_readme = set(re.findall(r"`([^`]+)`", listed))
        allowed = {n for k, n in rows if k == "allowed"}
        self.assertEqual(in_readme, allowed)
        for n in ("GIT_CONFIG_PARAMETERS", "core.hooksPath", "http.extraHeader", "convenience, not the guard"):
            self.assertIn(n, para[0])

    def test_s6_control_first_then_a_mutant_that_allows_core_hooksPath_fails_with_the_anchored_text(self):
        self.refused_all()   # the control: the unmutated wrapper refuses every form
        p = self.mutant("user.email|user.name) env_cfg_text", "user.email|user.name|core.hookspath) env_cfg_text")
        self.assertEqual(self.run_guard(pairs(("core.hooksPath", "/tmp/h")), p).returncode, 0)   # the mutant lets it through
        with self.assertRaisesRegex(AssertionError, "a refused git setting was let through"):
            self.refused_all(p)

    def test_a_mutant_that_allows_any_header_host_or_any_url_base_is_caught(self):
        p = self.mutant("(127\\.0\\.0\\.1|localhost)", "[a-z0-9.]+")
        with self.assertRaisesRegex(AssertionError, "a refused git setting was let through"):
            self.refused_all(p)
        p = self.mutant("env_cfg_local \"$b\"; ", "")
        with self.assertRaisesRegex(AssertionError, "a refused git setting was let through"):
            self.refused_all(p)


if __name__ == "__main__":
    unittest.main()

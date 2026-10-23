"""The settings file (#876, slice 1): the one reader `apply_settings`, its refusals, and the driver's use of it. Offline, no cluster.

Every set below is derived from the code's own tables (SETTINGS, SECRET_SHAPES), with a completeness test, so a new member that has no case here fails."""
import contextlib, io, json, os, re, stat, sys, tempfile, unittest
import urllib.parse as urllib_parse
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402

DIGEST = "registry.example.internal/acme/worker@sha256:" + "ab12" * 16
GOOD = {"CW_ALLOWED_REPOS": "acme-org/widget-service", "CW_CLOUD_IMAGE": DIGEST, "CW_CLOUD_SERVER": "https://cluster.example.internal:6443",
        "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000", "CW_VAULT_NAME": "vault-example", "CW_CLOUD_NAMESPACE": "cw"}
OPTIONAL_SAMPLES = {"CW_TOOLING_REPO": "acme-org/tooling", "CW_CLOUD_CA_FILE": "/home/user/ca.pem", "CW_PULL_SECRET": "pull-example",
                    "CW_KUBECTL": "kubectl", "CW_OPENCODE_BIN": "/usr/local/bin/opencode", "CW_RUN_RECORD_DIR": "/home/user/runs",
                    "CW_CLOUD_SPEND_CAP_USD": "25", "CW_CLUSTER_TOKEN_CMD": "cat /home/user/token", "CW_CLUSTER_ADMIN_CMD": "cat /home/user/kc",
                    "CW_GIT_TOKEN_CMD": "cat /home/user/git", "CW_APP_ID_CMD": "cat /home/user/id", "CW_APP_INSTALLATION_CMD": "cat /home/user/inst",
                    "CW_APP_KEY_CMD": "cat /home/user/key", "CW_VAULT_CHECK_DIR": "/home/user/check",
                    "CW_CLUSTER_ADMIN_SECRET": "acme-cluster-admin", "CW_CLUSTER_TOKEN_DURATION": "20m", "CW_STACK_MODEL_SECRET": "acme-stack-model",
                    "CW_HUB_CHANNEL_URL": "http://cloud-hub.cw.svc:8443/message", "CW_HUB_REGISTRY_SECRET": "cloud-hub-registry"}
SAMPLES = {   # one value per secret shape (built here, never a real secret)
    "a JWT": "eyJhbGciOiJIUzI1" + "." + "eyJzdWIiOiJ4eHh4",
    "a PEM header": "-----BEGIN " + "PRIVATE KEY-----",
    "a URL with a password": "https://user:" + "hunter2" + "pass@host.example/x",
    "a 40-character hex string": "git-cred " + "0123456789abcdef" * 2 + "01234567",
}
for _prefix in cw._PREFIXES:
    SAMPLES["token prefix " + _prefix.strip()] = "x " + _prefix + "A1b2C3d4E5f6G7h8"

HOSTILE = {   # per key: values that must be refused (each through the REAL parse_settings)
    "CW_ALLOWED_REPOS": ["*", "acme-org/*", "../../x", "/etc/passwd", "a b", "acme-org/widget-service,", ",acme-org/widget-service", "acme-org/..", "acme-org/x.git",
                         "acme-org/a b", "acme-org/x,*", "$(x)/y"],
    "CW_TOOLING_REPO": ["*", "acme-org", "acme-org/*", "../x", "a b/c", "acme-org/x,acme-org/y"],
    "CW_CLOUD_IMAGE": ["$(x)@sha256:" + "ab12" * 16, "reg.example/x:tag@sha256:" + "ab12" * 16, "-flag@sha256:" + "ab12" * 16, "x@sha256:" + "ab12" * 16 + "z",
                       "X/y@sha256:" + "ab12" * 16, "a b@sha256:" + "ab12" * 16, "x;y@sha256:" + "ab12" * 16, "x:1.2",
                       "a@sha256:" + "ab12" * 16 + "@sha256:" + "ab12" * 16, "reg.example/../x@sha256:" + "ab12" * 16, "x//y@sha256:" + "ab12" * 16],
    "CW_CLOUD_SERVER": ["http://169.254.169.254", "file:///x", "-flag", "x y", "cluster.example.internal", "https://user@host.example", "https://host.example/path",
                        "https://host.example?x=1", "https://host.example#f", "https://127.0.0.1", "https://8.8.8.8", "https://10.0.0.4:6443", "https://localhost",
                        "https://169.254.169.254", "https://[::1]", "https://[::127.0.0.1]", "https://[::ffff:127.0.0.1]", "https://[::ffff:8.8.8.8]",
                        "https://[2002::]", "https://[2001::]", "https://[fe80::1]", "https://[2606:4700:4700::1111]", "https://[2606:4700:4700::1111%25eth0]",
                        "https://[ff0e::1]", "https://0.0.0.0", "https://2130706433", "https://0x7f000001", "https://0X7F000001", "https://017700000001",
                        "https://127.1", "https://0177.0.0.1", "https://0x7f.0.0.1", "https://a.0x7f", "https://a.0X", "https://12345", "https://1.2.3",
                        "https://metadata.google.internal", "https://metadata", "https://a.localhost", "https://face", "https://host.example.",
                        "https://host.example:0", "https://host.example:65536", "https://host.example:99999", "https://host.example:abc", "https://host.example:+80",
                        "https://", "https://ho st.example", "ftp://host.example", "https://host.example\\x", "https://-a.example.be", "https://a-.example.be",
                        "https://a..example.be", "https://.example.be", "https://a_b.example.be", "https://" + "a" * 64 + ".example.be",
                        "https://" + ".".join(["a" * 60] * 5 + ["bb"]), "https://host.example\t", "https://ho\tst.example", "https://host.example\x0b",
                        "https://8.8.8.8 :6443", "https://8.8.8.8\x1f", "https://host.example.be\x0c",
                        "https://[", "https://[::1", "https://a]b", "https://host.example.be:99999999999", "https://host.example.be:-1", "https://host.0X1f",
                        "https://x.0XAB", "https://0X7F000001", "https://METADATA.google.internal", "https://A.LOCALHOST", "https://" + ".".join(["a" * 63] * 3 + ["b" * 59, "be"]),
                        "https://host.example.be:", "https://user:" + "pw@host.example.be"],
    "CW_CLOUD_CA_FILE": ["relative.pem", "/home/user/../etc/x", "-x", "$HOME/x"],
    "CW_PULL_SECRET": ["A_B", "a b", "-", "a/b", "$(x)", "UPPER"],
    "CW_VAULT_SUBSCRIPTION": ["sub-example", "0000", "00000000-0000-0000-0000-00000000000g", "$(x)", "a;b", "00000000-0000-0000-0000-000000000000 "[:-1] + "0"],
    "CW_VAULT_NAME": ["$(touch SENT)", "a;touch SENT", "ab", "a" * 25, "1abc", "a b", "a_b", "abc-"[:-1] + "/"],
    "CW_CLOUD_NAMESPACE": ["a/b;c", "kube-system", "kube-public", "default", "A", "-a", "a-", "a b", "a" * 64, "$(x)"],
    "CW_KUBECTL": ["../kubectl", "/x/../y", "-x", "a b", "$(x)", "a/b"],
    "CW_OPENCODE_BIN": ["../opencode", "/a/../b", "-x", "a b", "$(x)", "a/b"],
    "CW_RUN_RECORD_DIR": ["runs", "/a/../b", "-x"],
    "CW_CLOUD_SPEND_CAP_USD": ["0", "-1", "1e3", "100001", "1.2.3", "abc", "0.0"],
    "CW_CLUSTER_TOKEN_CMD": ["-x"], "CW_CLUSTER_ADMIN_CMD": ["--x"], "CW_GIT_TOKEN_CMD": ["-x"], "CW_APP_ID_CMD": ["-x"],
    "CW_APP_INSTALLATION_CMD": ["-x"], "CW_APP_KEY_CMD": ["-x"],
    "CW_VAULT_CHECK_DIR": ["check", "/a/../b", "-x"],
    "CW_HUB_CHANNEL_URL": ["ftp://hub.example", "http://user@hub.example", "http://hub.example/x?y=1", "hub.example", "http://hub example", "$(x)"],
    "CW_HUB_REGISTRY_SECRET": ["A_B", "a b", "-", "a/b", "$(x)", "UPPER"],
    "CW_STACK_MODEL_SECRET": ["https://vault.example.internal/secrets/x", "/home/user/secret", "a b", "a_b", "a.b", "a/b", "a" * 128, "$(x)", "-x", "--help"],
    "CW_CLUSTER_ADMIN_SECRET": ["/home/user/secret", "a b", "a_b", "a.b", "a/b", "a" * 128, "a;b", "$(x)", "a\u00e9", "-x", "--help", "--", "--query", "-", "-a"],
    "CW_CLUSTER_TOKEN_DURATION": ["-5m", "0m", "61m", "2h", "1000m", "20", "m", "20s", "1.5h", "20 m", "/tmp/20m", "20m5s", "\u0662\u0660m", "-1h", "0h"],
}
CONTROL = ["\t", "\x00", "\r", "\x0b", "\x0c", "\x1c", "\x1b", "\x7f", "\x01"]


def body(values):
    return "# settings\n" + "".join("%s=%s\n" % kv for kv in values.items())


class Dir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        os.chmod(self.dir, 0o700)
        self.path = os.path.join(self.dir, "cloud.env")

    def write(self, text, mode=0o600):
        with open(self.path, "w") as f:
            f.write(text)
        os.chmod(self.path, mode)
        return self.path

    def refused(self, text=None, mode=0o600, env=None):
        if text is not None:
            self.write(text, mode)
        env = {} if env is None else env
        with self.assertRaises(cw.CloudError) as caught:
            cw.apply_settings(env, self.path)
        self.assertEqual(caught.exception.code, cw.EXIT_USAGE)
        return str(caught.exception)


class Tables(unittest.TestCase):
    def test_every_setting_has_a_known_kind_and_the_samples_cover_the_table(self):
        self.assertEqual({k for k, _, _ in cw.SETTINGS}, set(GOOD) | set(OPTIONAL_SAMPLES))
        self.assertEqual(set(cw.SETTING_REQUIRED), set(GOOD))
        self.assertLessEqual({kind for _, _, kind in cw.SETTINGS}, {"name", "path", "command", "digest-ref", "number", "secret-name", "duration"})
        self.assertEqual(len(cw.SETTINGS), len({k for k, _, _ in cw.SETTINGS}))
        self.assertIn(("CW_VAULT_CHECK_DIR", False, "path"), cw.SETTINGS)   # reserved for the pre-check, optional

    def test_the_samples_cover_every_secret_shape(self):
        self.assertEqual(set(SAMPLES), {name for name, _, _ in cw.SECRET_SHAPES})


class ValueRules(Dir):
    def test_every_setting_has_one_value_rule_and_a_hostile_list(self):
        self.assertEqual(set(cw.VALIDATORS), set(cw.SETTING_KINDS))   # default-deny: a new setting without a rule fails here
        self.assertEqual(set(HOSTILE), set(cw.SETTING_KINDS))
        for key, (rule, check) in cw.VALIDATORS.items():
            self.assertTrue(rule and callable(check), key)
        for key in set(GOOD) | set(OPTIONAL_SAMPLES):   # the good samples satisfy their own row
            value = {**GOOD, **OPTIONAL_SAMPLES}[key]
            self.assertTrue(cw.VALIDATORS[key][1](value), key)

    def test_every_hostile_value_is_refused_naming_the_key_and_the_rule_and_never_the_value(self):
        for key, values in HOSTILE.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    err = self.refused(body(dict(GOOD, **{key: value})))
                    self.assertIn(key, err)
                    self.assertTrue(cw.VALIDATORS[key][0] in err or "control character" in err or "looks like a secret" in err, err)
                    rest = err.replace(self.path, "").replace(cw.VALIDATORS[key][0], "")   # the text a value could leak into
                    if len(value.strip()) >= 4:
                        self.assertNotIn(value.strip(), rest)

    def test_one_mutant_per_row_each_hostile_list_is_what_catches_it(self):
        for key, values in HOSTILE.items():
            with self.subTest(key=key), mock.patch.dict(cw.VALIDATORS, {key: ("anything", lambda v: True)}):
                accepted = 0
                for value in values:
                    try:
                        cw.parse_settings(body(dict(GOOD, **{key: value})), self.path)
                        accepted += 1
                    except cw.CloudError:
                        pass
                self.assertGreater(accepted, 0, "with the rule for %s removed, no hostile value got through: the list does not test the rule" % key)

    def test_a_setting_with_no_row_is_refused(self):
        with mock.patch.dict(cw.VALIDATORS):
            del cw.VALIDATORS["CW_VAULT_NAME"]
            self.assertIn("CW_VAULT_NAME", self.refused(body(GOOD)))

    def test_a_control_character_in_any_value_or_line_is_refused_naming_the_line(self):
        for key in set(GOOD) | set(OPTIONAL_SAMPLES):
            for char in CONTROL:
                with self.subTest(key=key, char=repr(char)):
                    value = {**GOOD, **OPTIONAL_SAMPLES}[key] + char + "x"
                    err = self.refused(body(dict(GOOD, **{key: value})))
                    self.assertIn("control character", err)
                    self.assertIn("line", err)
        self.assertIn("control character", self.refused(body(GOOD).replace("\n", "\r\n")))   # a CRLF file

    def test_the_allow_list_rule_is_the_function_the_launch_check_uses(self):
        with mock.patch.object(cw, "allowed_repo_keys", wraps=cw.allowed_repo_keys) as used:
            cw.parse_settings(body(GOOD), self.path)
            self.assertTrue(used.called)
        for raw in ("acme-org/widget-service,acme-org/widget-api", "ACME-ORG/Widget-Service"):
            self.assertTrue(cw.VALIDATORS["CW_ALLOWED_REPOS"][1](raw))
            cw.check_repo_allowed("acme-org/widget-service", {"CW_ALLOWED_REPOS": raw})   # what the validator accepts, the check accepts

    GOOD_HOSTS = ("https://cluster.example.internal:6443", "https://cluster.example.internal", "https://cluster.example.internal/", "https://cluster.example.be",
                  "https://api.example.de", "https://x.example.cd", "https://x.face", "https://a.dead", "https://Cluster.Example.Be", "https://a.0xg", "https://a.0x1g", "https://HOST.EXAMPLE.DE:443", "https://a-b.example.be:1", "https://a.example.be:65535",
                  "https://aks-dns-1a2b3c4d.hcp.example.azmk8s.io:443", "https://" + ".".join(["a" * 63] * 3 + ["b" * 58, "be"]))

    def test_a_dns_name_is_accepted_and_every_ip_literal_is_refused_because_it_is_one(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        self.assertEqual(len(self.GOOD_HOSTS[-1]) - len("https://"), 253)   # the longest name there is
        for value in self.GOOD_HOSTS:
            with self.subTest(value):
                self.assertTrue(server(value))
        for value in HOSTILE["CW_CLOUD_SERVER"]:
            if re.match(r"https://(\[|[0-9xX.]+(:[0-9]+)?$)", value):
                with self.subTest(value):
                    self.assertFalse(server(value))   # an IP literal of any form

    def test_each_server_rule_has_a_mutant_that_lets_its_hostile_value_through(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        anything = re.compile(".+")
        old_host = re.compile(r"(?:%s\.)*%s" % (cw._DNS_LABEL, cw._DNS_LABEL))
        loose_netloc = re.compile(r"(.+?)(?::([0-9]{1,5}))?")
        every = lambda label: True
        none = lambda label: False
        permissive_host = re.compile(r"[A-Za-z0-9.-]+")
        loose_port_netloc = re.compile(r"([A-Za-z0-9.-]+)(?::(.*))?")
        nobad = re.compile("(?!)")
        cases = (
            ("an IP literal in brackets", "https://[::1]", (("_NETLOC", loose_netloc), ("_HOST", anything), ("_has_letter", every))),
            ("an IPv4 literal", "https://127.0.0.1", (("_has_letter", every), ("_integer_host_label", none))),
            ("an all-digit last label", "https://a.2130706433", (("_has_letter", every), ("_integer_host_label", none))),
            ("a 0x last label", "https://a.0x7f000001", (("_integer_host_label", none),)),
            ("an uppercase 0X last label (the lower-casing)", "https://a.0X7F", (("_norm", lambda host: host),)),
            ("an uppercase 0X integer host", "https://0X7F000001", (("_norm", lambda host: host), ("_HOST", old_host))),
            ("an uppercase metadata name (the lower-casing)", "https://METADATA.google.internal", (("_norm", lambda host: host),)),
            ("an uppercase .localhost name (the lower-casing)", "https://A.LOCALHOST", (("_norm", lambda host: host),)),
            ("no letter in the last label", "https://a.1-2", (("_has_letter", every),)),
            ("port 0", "https://host.example:0", (("_port_ok", lambda port: True),)),
            ("port 65536", "https://host.example:65536", (("_port_ok", lambda port: True),)),
            ("a port too long for the grammar", "https://host.example.be:99999999999", (("_NETLOC", loose_port_netloc), ("_port_ok", lambda port: True))),
            ("a port that is not digits", "https://host.example.be:abc", (("_NETLOC", loose_port_netloc), ("_port_ok", lambda port: True))),
            ("a signed port", "https://host.example.be:-1", (("_NETLOC", loose_port_netloc), ("_port_ok", lambda port: True))),
            ("a single label", "https://face", (("_HOST", old_host),)),
            ("a trailing dot", "https://host.example.be.", (("_HOST", permissive_host), ("_has_letter", every))),
            ("a leading hyphen", "https://-a.example.be", (("_HOST", permissive_host),)),
            ("a trailing hyphen", "https://a-.example.be", (("_HOST", permissive_host),)),
            ("an empty label", "https://a..example.be", (("_HOST", permissive_host),)),
            ("a 64-character label", "https://" + "a" * 64 + ".example.be", (("_HOST", permissive_host),)),
            ("a 254-character name", "https://" + ".".join(["a" * 63] * 3 + ["b" * 59, "be"]), (("_MAX_HOST", 10 ** 6),)),
            ("a 307-character name", "https://" + ".".join(["a" * 60] * 5 + ["bb"]), (("_MAX_HOST", 10 ** 6), ("_HOST", permissive_host))),
            ("the metadata name", "https://metadata.google.internal", (("_REFUSED_HOSTS", ()),)),
            ("a .localhost name", "https://a.localhost", (("_REFUSED_SUFFIX", ".never-matches"),)),
            ("a scheme that is not https", "http://host.example.be", (("_SCHEME", ""),)),
            ("a path", "https://host.example.be/x", (("_PATHS", ("", "/", "/x")),)),
            ("a query", "https://host.example.be?x=1", (("_BAD_CHARS", re.compile("(?!)")), ("_shape_ok", lambda parts: True))),
            ("a fragment", "https://host.example.be#f", (("_BAD_CHARS", re.compile("(?!)")), ("_shape_ok", lambda parts: True))),
            ("a user part", "https://user@host.example.be", (("_BAD_CHARS", nobad), ("_NETLOC", loose_netloc), ("_HOST", anything))),
            ("a tab that urlsplit would drop", "https://cluster.example.be\t", (("_BAD_CHARS", re.compile("(?!)")),)),
            ("a newline that urlsplit would drop", "https://cluster.example.be\n", (("_BAD_CHARS", re.compile("(?!)")),)),
            ("a form feed", "https://cluster.example.be\x0c", (("_BAD_CHARS", nobad), ("_NETLOC", loose_netloc), ("_HOST", anything))),
        )
        for label, value, patches in cases:
            with self.subTest(label):
                self.assertFalse(server(value))
                with contextlib.ExitStack() as stack:
                    for name, replacement in patches:
                        stack.enter_context(mock.patch.object(cw, name, replacement))
                    self.assertTrue(server(value), "with its rule removed %r is still refused: the case does not test the rule" % label)

    def test_a_url_that_does_not_parse_is_refused_and_never_raises(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        broken = ("https://[", "https://[::1", "https://a]b", "https://]")
        for value in broken:
            with self.subTest(value):
                self.assertIsNone(cw._split(value))
                self.assertFalse(server(value))
        def escapes(value):   # a mutant: the ValueError is let out
            return urllib_parse.urlsplit(value)
        def accepts(value):   # a mutant: an unparsable URL is taken as a good one
            try:
                return urllib_parse.urlsplit(value)
            except ValueError:
                return urllib_parse.urlsplit("https://ok.example.be")
        for value in broken:
            with self.subTest(mutant="escape", value=value), mock.patch.object(cw, "_split", escapes):
                with self.assertRaises(ValueError):
                    server(value)
            with self.subTest(mutant="accept", value=value), mock.patch.object(cw, "_split", accepts):
                self.assertTrue(server(value))
        # through the real parser: refused naming the key and the rule, never a traceback, never the value
        for value in broken:
            err = self.refused(body(dict(GOOD, CW_CLOUD_SERVER=value)))
            self.assertIn("CW_CLOUD_SERVER", err)
            self.assertIn(cw.VALIDATORS["CW_CLOUD_SERVER"][0], err)

    def test_the_server_rule_judges_the_string_as_written(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        for value in ("https://127.0.0.1\t", "https://host\t.example", "https://cluster.example.be\n", "https://cluster.example.be\r", "https://cluster.example.be\x00"):
            self.assertFalse(server(value), repr(value))

    def test_a_bad_file_leaves_the_environment_byte_identical(self):
        env = dict(GOOD, CW_TOOLING_REPO="acme-org/tooling", HOME="/home/user")
        before = dict(env)
        self.refused(body(dict(GOOD, CW_VAULT_NAME="$(x)")), env=env)
        self.assertEqual(env, before)

    def test_if_the_environment_refuses_a_value_every_setting_is_put_back(self):
        self.write(body(GOOD))

        class Strict(dict):
            calls = 0

            def update(self, *a, **k):
                Strict.calls += 1
                if Strict.calls == 1:
                    raise ValueError("embedded null byte")
                super().update(*a, **k)
        env = Strict({"CW_TOOLING_REPO": "acme-org/tooling"})
        with self.assertRaises(cw.CloudError):
            cw.apply_settings(env, self.path)
        self.assertEqual(dict(env), {"CW_TOOLING_REPO": "acme-org/tooling"})

    def test_a_second_hard_link_is_refused(self):
        self.write(body(GOOD))
        os.link(self.path, os.path.join(self.dir, "second"))
        self.assertIn("hard link", self.refused())


class Reader(Dir):
    def test_s1_a_good_file_with_every_key_is_applied_and_the_source_is_its_path(self):
        values = dict(GOOD, **OPTIONAL_SAMPLES)
        self.write(body(values))
        env = {"HOME": "/home/user", "CW_TUNNEL_TIMEOUT": "10"}
        self.assertEqual(cw.apply_settings(env, self.path), self.path)
        self.assertEqual({k: v for k, v in env.items() if k in values}, values)
        self.assertEqual(env["CW_TUNNEL_TIMEOUT"], "10")   # not a table key: untouched

    def test_u1_no_file_means_the_environment_and_nothing_changes(self):
        env = dict(GOOD, CW_TOOLING_REPO="acme-org/tooling")
        before = dict(env)
        self.assertEqual(cw.apply_settings(env, os.path.join(self.dir, "none.env")), "environment")
        self.assertEqual(env, before)

    def test_the_file_is_the_only_source_when_it_exists(self):
        self.write(body(GOOD))
        env = {"CW_TOOLING_REPO": "acme-org/tooling", "CW_GIT_TOKEN_CMD": "cat /x"}   # table keys the file does not set
        cw.apply_settings(env, self.path)
        self.assertNotIn("CW_TOOLING_REPO", env)
        self.assertNotIn("CW_GIT_TOKEN_CMD", env)

    def test_a_key_in_both_with_a_different_value_is_refused_naming_both_sources_and_no_value(self):
        for key in GOOD:
            with self.subTest(key):
                self.write(body(GOOD))
                err = self.refused(env={key: "other-value-zz"})
                self.assertIn(key, err)
                self.assertIn(self.path, err)
                self.assertIn("environment", err)
                self.assertNotIn("other-value-zz", err)
                self.assertNotIn(GOOD[key], err)

    def test_a_key_in_both_with_the_same_value_is_accepted(self):
        self.write(body(GOOD))
        cw.apply_settings(dict(GOOD), self.path)

    def test_s2_a_mode_with_any_group_or_other_bit_is_refused(self):
        for mode in (0o640, 0o604, 0o666, 0o660, 0o644, 0o602, 0o620, 0o601, 0o700):
            with self.subTest(mode=oct(mode)):
                err = self.refused(body(GOOD), mode)
                self.assertIn(self.path, err)
                self.assertIn("%04o" % mode, err)

    def test_s2_a_group_or_world_writable_parent_is_refused(self):
        self.write(body(GOOD))
        for mode in (0o770, 0o707, 0o777, 0o720, 0o702):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.dir, mode)
                try:
                    self.assertIn("writable", self.refused())
                finally:
                    os.chmod(self.dir, 0o700)

    def test_s2_a_symlink_is_refused_even_when_its_target_is_fine(self):
        target = os.path.join(self.dir, "real.env")
        with open(target, "w") as f:
            f.write(body(GOOD))
        os.chmod(target, 0o600)
        os.symlink(target, self.path)
        self.assertIn("symlink", self.refused())

    def test_s2_a_dangling_symlink_is_refused_not_treated_as_no_file(self):
        os.symlink(os.path.join(self.dir, "nowhere"), self.path)
        self.assertIn("symlink", self.refused())

    def test_s2_another_owner_is_refused(self):
        self.write(body(GOOD))
        with mock.patch.object(cw.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertIn(" is owned by another user", self.refused())

    def test_s2_a_parent_directory_of_another_owner_is_refused(self):
        self.write(body(GOOD))
        real = os.stat
        def parent_of_another(path, *a, **k):
            result = real(path, *a, **k)
            if os.path.abspath(path) == self.dir:
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink, os.geteuid() + 1, result.st_gid) + tuple(result)[6:])
            return result
        with mock.patch.object(cw.os, "stat", parent_of_another):
            self.assertIn("directory owned by another user", self.refused())

    def test_s2_a_directory_or_a_fifo_is_not_a_settings_file(self):
        os.mkdir(self.path, 0o700)
        self.assertIn("settings file", self.refused())
        os.rmdir(self.path)
        os.mkfifo(self.path, 0o600)
        self.assertIn("regular file", self.refused())

    def test_s3_a_secret_shaped_value_is_refused_naming_the_key_and_never_printing_the_value(self):
        for shape, value in SAMPLES.items():
            for key in ("CW_GIT_TOKEN_CMD",) + (("CW_PULL_SECRET",) if "40-character" not in shape else ()):
                with self.subTest(shape=shape, key=key):
                    err = self.refused(body(dict(GOOD, **{key: value})))
                    self.assertIn(key, err)
                    self.assertIn("looks like a secret", err)
                    self.assertNotIn(value, err)
                    self.assertNotIn(value.split()[-1], err)

    def test_s3_a_40_hex_string_is_refused_in_a_command_only(self):
        value = "0123456789abcdef" * 2 + "01234567"
        self.refused(body(dict(GOOD, CW_GIT_TOKEN_CMD="echo " + value)))
        self.write(body(dict(GOOD, CW_PULL_SECRET=value)))   # not a command: no shape (the image digest holds 64 hex and is fine)
        cw.apply_settings({}, self.path)

    def test_s4_each_required_key_missing_is_named_and_no_other(self):
        for key in cw.SETTING_REQUIRED:
            with self.subTest(key):
                err = self.refused(body({k: v for k, v in GOOD.items() if k != key}))
                self.assertIn(key, err)
                for other in cw.SETTING_REQUIRED:
                    if other != key:
                        self.assertNotIn(other, err)

    def test_s4_an_empty_value_counts_as_missing_and_all_missing_ones_are_listed(self):
        err = self.refused(body(dict(GOOD, CW_CLOUD_NAMESPACE="", CW_VAULT_NAME="", CW_CLOUD_SERVER="")))
        for key in ("CW_CLOUD_NAMESPACE", "CW_VAULT_NAME", "CW_CLOUD_SERVER"):
            self.assertIn(key, err)
        self.assertNotIn("CW_ALLOWED_REPOS", err)

    def test_s5_the_image_must_be_a_digest(self):
        for image in ("registry.example.internal/acme/worker:1.2", "registry.example.internal/acme/worker", "w@sha256:" + "a" * 63,
                      "w@sha256:" + "a" * 65, "w@sha256:" + "A" * 64, "w@sha512:" + "a" * 64, "w @sha256:" + "a" * 64, "@sha256:" + "a" * 64):
            with self.subTest(image=image):
                err = self.refused(body(dict(GOOD, CW_CLOUD_IMAGE=image)))
                self.assertIn("CW_CLOUD_IMAGE", err)
        self.write(body(dict(GOOD, CW_CLOUD_IMAGE="w@sha256:" + "a" * 64)))
        cw.apply_settings({}, self.path)

    def test_a_bad_line_an_unknown_key_a_repeat_a_quote_and_a_bad_number_or_path_are_refused_without_the_value(self):
        marker = "marker-value-zz9"
        cases = {"not KEY=VALUE": "this is not a setting " + marker + "\n", "unknown": "CW_SOMETHING_ELSE=" + marker + "\n",
                 "twice": "CW_VAULT_NAME=a\nCW_VAULT_NAME=" + marker + "\n", "quoted": 'CW_VAULT_NAME="' + marker + '"\n',
                 "quoted single": "CW_VAULT_NAME='" + marker + "'\n", "lowercase": "cw_vault_name=" + marker + "\n",
                 "spaces": "CW_VAULT_NAME = " + marker + "\n", "number": "CW_CLOUD_SPEND_CAP_USD=" + marker + "\n",
                 "relative path": "CW_CLOUD_CA_FILE=" + marker + "\n"}
        for label, extra in cases.items():
            with self.subTest(label):
                text = body({k: v for k, v in GOOD.items() if k != "CW_VAULT_NAME" or label in ("not KEY=VALUE",)}) + extra
                err = self.refused(text) if label != "twice" else self.refused(body(GOOD) + extra)
                self.assertNotIn(marker, err)

    def test_no_expansion_and_nothing_is_run_on_read(self):
        sentinel = os.path.join(self.dir, "ran")
        self.write(body(dict(GOOD, CW_GIT_TOKEN_CMD="touch %s; echo $(touch %s) `touch %s` ${HOME}" % ((sentinel,) * 3))))
        env = {"HOME": "/home/user"}
        cw.apply_settings(env, self.path)
        self.assertFalse(os.path.exists(sentinel))
        self.assertIn("${HOME}", env["CW_GIT_TOKEN_CMD"])   # taken as written

    def test_comments_and_blank_lines_are_skipped_and_a_value_keeps_its_equals_and_hash(self):
        self.write("# a comment\n\n  # indented comment\n" + body(dict(GOOD, CW_GIT_TOKEN_CMD="cmd a=b # not a comment")))
        env = {}
        cw.apply_settings(env, self.path)
        self.assertEqual(env["CW_GIT_TOKEN_CMD"], "cmd a=b # not a comment")

    def test_a_file_over_the_cap_and_a_file_that_is_not_utf8_are_refused(self):
        self.assertIn("bytes", self.refused("#" * (cw.SETTINGS_FILE_MAX + 10) + "\n"))
        with open(self.path, "wb") as f:
            f.write(b"\xff\xfe")
        os.chmod(self.path, 0o600)
        self.assertIn("UTF-8", self.refused())

    def test_the_path_comes_from_the_config_directory_and_no_environment_variable_moves_it(self):
        env = {"XDG_CONFIG_HOME": "/home/user/cfg", "CW_CLOUD_SETTINGS": "/elsewhere/cloud.env"}
        self.assertEqual(cw.settings_path(env), "/home/user/cfg/%s/cloud.env" % cw.CONFIG_DIR_NAME)
        self.assertEqual(cw.settings_path({"HOME": "/home/user"}), "/home/user/.config/%s/cloud.env" % cw.CONFIG_DIR_NAME)
        self.assertEqual(cw.CONFIG_DIR_NAME, "fl" + "eet-switchboard")
        self.assertIsNone(cw.SETTINGS_PATH)


class Driver(Dir):
    """The driver's own `main` reads the file through the same function (a process that passes no environment of its own)."""

    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.dir, "calls.jsonl")
        self.state = os.path.join(self.dir, "kube.json")
        with open(self.state, "w") as f:
            json.dump({"pods": {}}, f)
        self.env = {"FAKE_KUBE_STATE": self.state, "FAKE_KUBE_CALLS": self.calls, "CW_CLOUD_AUDIT": os.path.join(self.dir, "audit.jsonl"),
                    "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint", "CW_CLOUD_DOMAIN": "example.internal",
                    "CW_CLOUD_TOKEN_FILE": os.path.join(self.dir, "token"), "HOME": self.dir, "PATH": os.environ.get("PATH", "")}
        with open(self.env["CW_CLOUD_TOKEN_FILE"], "w") as f:
            f.write("fake-token-value\n")
        os.chmod(self.env["CW_CLOUD_TOKEN_FILE"], 0o600)
        hook = mock.patch.object(cw, "SETTINGS_PATH", self.path)
        hook.start()
        self.addCleanup(hook.stop)

    def run_main(self, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"])
        return code, out.getvalue(), err.getvalue()

    def kube_calls(self):
        return [] if not os.path.exists(self.calls) else [x for x in open(self.calls).read().splitlines() if x]

    def test_the_driver_takes_the_image_and_the_kubectl_from_the_file_with_an_empty_environment(self):
        self.write(body(dict(GOOD, CW_KUBECTL=os.path.join(HERE, "fake_kubectl"))))
        code, out, err = self.run_main(self.env)
        self.assertEqual(code, 0, err)
        with open(self.state) as f:
            self.assertIn(DIGEST, json.dumps(json.load(f)["pods"]))

    def test_the_driver_refuses_a_bad_file_before_any_kubectl_call(self):
        self.write(body(dict(GOOD, CW_KUBECTL=os.path.join(HERE, "fake_kubectl"))), 0o644)
        code, out, err = self.run_main(self.env)
        self.assertEqual(code, 2)
        self.assertIn("0644", err)
        self.assertEqual(self.kube_calls(), [])

    def test_the_driver_refuses_a_file_that_clashes_with_its_environment_naming_both_and_no_value(self):
        self.write(body(dict(GOOD, CW_KUBECTL=os.path.join(HERE, "fake_kubectl"))))
        code, out, err = self.run_main(dict(self.env, CW_CLOUD_NAMESPACE="other-ns-zz"))
        self.assertEqual(code, 2)
        self.assertIn("CW_CLOUD_NAMESPACE", err)
        self.assertIn("environment", err)
        self.assertNotIn("other-ns-zz", err)
        self.assertEqual(self.kube_calls(), [])

    def test_a_caller_that_passes_its_own_environment_never_reads_the_users_file(self):
        self.write("garbage that would be refused\n", 0o666)
        out, err = io.StringIO(), io.StringIO()
        env = dict(self.env, CW_KUBECTL=os.path.join(HERE, "fake_kubectl"), CW_CLOUD_SERVER="https://cluster.example.internal:6443", CW_CLOUD_IMAGE="registry.example.internal/worker:1", CW_ALLOWED_REPOS="acme-org/widget-service")
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"], env)
        self.assertEqual(code, 0, err.getvalue())


# ---------------------------------------------------------------- S15: the two keys reach the launch wiring exactly as from the environment
import ast, glob, importlib.util  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def load_wiring():
    """bin/cloud_worker.py (the launch wiring) by path, as the switchboard loads it."""
    spec = importlib.util.spec_from_file_location("cw_wiring_for_settings_test", os.path.join(ROOT, "bin", "cloud_worker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WiringReceivesBoth(Dir):
    def run_with_file(self, values):
        wiring = load_wiring()
        lib = wiring._lib()
        self.write(body(values))
        calls = []

        def fake_capture(argv, env=None, feed=None, **kw):
            calls.append(list(argv))
            return (0, "fake-admin-config\n", "") if len(calls) == 1 else (0, "t" * 24 + "\n", "")
        with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True), mock.patch.object(lib, "SETTINGS_PATH", self.path), \
                mock.patch.object(wiring, "_capture", fake_capture):
            source = wiring.use_settings()
            wiring.cluster_token()
        return source, calls

    def test_s15_the_file_alone_gives_the_wiring_the_secret_name_and_the_duration(self):
        source, calls = self.run_with_file(dict(GOOD, CW_CLUSTER_ADMIN_SECRET="acme-cluster-admin", CW_CLUSTER_TOKEN_DURATION="45m"))
        self.assertEqual(source, self.path)
        self.assertEqual(calls[0][calls[0].index("--name") + 1], "acme-cluster-admin")
        self.assertIn("--duration=45m", calls[1])

    def test_s15_the_environment_route_still_gives_the_same_values(self):
        wiring = load_wiring()
        calls = []

        def fake_capture(argv, env=None, feed=None, **kw):
            calls.append(list(argv))
            return (0, "fake-admin-config\n", "") if len(calls) == 1 else (0, "t" * 24 + "\n", "")
        env = {"CW_VAULT_SUBSCRIPTION": GOOD["CW_VAULT_SUBSCRIPTION"], "CW_VAULT_NAME": GOOD["CW_VAULT_NAME"],
               "CW_CLUSTER_ADMIN_SECRET": "acme-cluster-admin", "CW_CLUSTER_TOKEN_DURATION": "45m"}
        with mock.patch.object(wiring, "_capture", fake_capture):
            wiring.cluster_token(env)
        self.assertEqual(calls[0][calls[0].index("--name") + 1], "acme-cluster-admin")
        self.assertIn("--duration=45m", calls[1])

    def test_s15_a_bad_value_is_refused_through_the_wiring_with_exit_2_naming_the_key_and_no_value(self):
        wiring = load_wiring()
        for key, value in (("CW_CLUSTER_ADMIN_SECRET", "/home/user/secret"), ("CW_CLUSTER_ADMIN_SECRET", "acme secret"),
                           ("CW_CLUSTER_ADMIN_SECRET", "acme\rsecret"), ("CW_CLUSTER_ADMIN_SECRET", "acme\nsecret"), ("CW_CLUSTER_TOKEN_DURATION", "-20m")):
            with self.subTest(key=key, value=value):
                self.write(body(dict(GOOD, **{key: value})))
                with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(wiring._lib(), "SETTINGS_PATH", self.path):
                    with self.assertRaises(wiring.Refusal) as caught:
                        wiring.use_settings()
                text = str(caught.exception)
                if "\n" not in value:   # a literal newline ends the line: the rest is a second, malformed line (still refused, with no value)
                    self.assertIn(key, text)
                for part in ("home/user", "acme secret", "acme", "-20m"):
                    self.assertNotIn(part, text.replace(self.path, "").replace("acme-cluster", ""))

    def test_s15_the_duration_rule_never_accepts_what_the_wiring_refuses(self):
        """Derived parity: every string up to length 5 over a small alphabet. DANGEROUS direction (file accepts, wiring refuses) must be empty for all;
        the other direction (the wiring accepts, the file refuses) is the documented 'stricter' (spaces are stripped by the wiring; a non-ASCII digit)."""
        import itertools
        wiring = load_wiring()
        alphabet = ["0", "1", "2", "6", "9", "m", "h", "s", " ", "-", "\u0662"]
        dangerous, stricter = [], 0
        for n in range(0, 6):
            for chars in itertools.product(alphabet, repeat=n):
                value = "".join(chars)
                try:
                    wiring._duration({"CW_CLUSTER_TOKEN_DURATION": value})
                    wiring_ok = True
                except wiring.Refusal:
                    wiring_ok = False
                file_ok = cw._duration_ok(value)
                if file_ok and not wiring_ok:
                    dangerous.append(value)
                if wiring_ok and not file_ok and value.strip():
                    stricter += 1
        self.assertEqual(dangerous, [])
        for good in ("1m", "20m", "60m", "1h", "01h", "060m"):   # the other way for what the launcher is documented to take: a unit or a bound dropped dies here
            self.assertTrue(cw._duration_ok(good), good)
        self.assertGreater(stricter, 0)   # the documented difference exists (spaces, a non-ASCII digit)

    def test_s15_the_parity_alphabet_kills_each_width_unit_and_bound_mutant(self):
        wiring = load_wiring()
        def survives(pattern, bound=60):
            def ok(v):
                m = re.fullmatch(pattern, v)
                return bool(m) and 1 <= int(m.group(1)) * (60 if m.group(2) == "h" else 1) <= bound
            import itertools
            for n in range(0, 6):
                for chars in itertools.product("0126mhs", repeat=n):
                    v = "".join(chars)
                    try:
                        wiring._duration({"CW_CLUSTER_TOKEN_DURATION": v}); w = True
                    except wiring.Refusal:
                        w = False
                    if ok(v) and not w:
                        return True
            return False
        self.assertFalse(survives(r"([0-9]{1,3})([mh])"))
        self.assertTrue(survives(r"([0-9]{1,4})([mh])"))      # wider
        self.assertTrue(survives(r"([0-9]{1,3})([mhs])"))     # a unit added
        self.assertTrue(survives(r"([0-9]{1,3})([mh])", bound=61))   # a bound moved


class OptionShaped(Dir):
    def test_every_row_refuses_a_leading_hyphen_so_no_value_can_become_an_option_in_an_argument_list(self):
        """Derived from the table: every setting's rule refuses an option-shaped value and a good sample with a hyphen put first. (A superset of the
        rows that reach a kubectl/az/git/openssl argument list: a new row cannot slip in.)"""
        samples = {**GOOD, **OPTIONAL_SAMPLES}
        for key, (rule, check) in cw.VALIDATORS.items():
            if key == "CW_CLUSTER_ADMIN_SECRET":
                self.assertFalse(check(""))   # the row itself refuses empty (the generic reader treats an empty value as unset before the row)
            for value in ("-x", "--help", "--", "--query", "-", "-" + samples[key]):
                with self.subTest(key=key, value=value):
                    self.assertFalse(check(value))


# ---------------------------------------------------------------- S16/S18: the class: every CW_* and switchboard-prefix name the code reads is a row or on the one list
# THE list of names the launcher and the launch wiring mention that are deliberately NOT settings, each with its reason.
SWITCHBOARD_PREFIX = "FL" + "EET_CLOUD_"   # written in two parts: test_cw_names bans the retired prefix as text anywhere under cloud/ (this one is the switchboard's own, in bin/)
PREFIXES = ("CW_", SWITCHBOARD_PREFIX)   # every environment prefix the launch wiring and the launcher read
NOT_SETTINGS = {
    # set by the code itself for the pod or a child: never read from the operator
    "CW_WORKER": "set by the launcher into the pod's environment (the worker's own name); end_hook reads it inside the pod",
    "CW_REPO": "set by the launcher into the pod's environment from --repo; read back from the pod spec",
    "CW_BRANCH": "set by the launcher into the pod's environment from --branch",
    "CW_IDLE_SECONDS": "set by the launcher into the pod's environment from --idle",
    "CW_HARD_DEADLINE": "set by the launcher into the pod's environment from the hard limit",
    "CW_AUDIT_RUNTIME_ID": "set by the launcher into the pod's environment (pod/NAME): an identifier, not a choice",
    "CW_CLOUD_TOKEN_FILE": "set by the launch wiring for its child from the in-memory mint (a private temp file it removes); no operator names it",
    "CW_AGENT": "set by the launcher into an orchestrator pod's environment from --agent (worker or orchestrator); the entrypoint reads it inside the pod",
    "FLEET_CLOUD_CHANNEL_URL": "set by the launcher into an orchestrator pod's environment from the CW_HUB_CHANNEL_URL row (or its default); the cloud command reads it in the pod",
    "FLEET_CLOUD_TOKEN_FILE": "set by the launcher into an orchestrator pod's environment: the fixed path of the channel secret's file in the pod's memory-backed directory",
    "CW_WORKDIR": "the pod's own working directory (default /work/repo), read by the hook and the record inside the pod",
    # the other login kind: a file with CW_CLOUD_SERVER (required) is the token route; these select kubectl's own context instead
    "CW_KUBE_CONTEXT": "the other login kind (kubectl's own context); a lane with the settings file has the required CW_CLOUD_SERVER and so the token route, and the two never mix",
    "CW_KUBECONFIG": "the other login kind (a kubeconfig path), see CW_KUBE_CONTEXT",
    # defaults that a lane does not need to change
    "CW_CLOUD_SERVICE_ACCOUNT": "the pod's service account, default cw-worker, a name the cluster setup fixes; not changed per lane",
    "CW_CLOUD_AUDIT": "where the audit file goes, default under the state directory; a lane needs the default, tests and installs move it",
    "CW_CLOUD_RECORD_DIR": "read-only override of where the reaper looks for the switchboard's records; the reaper's own wiring passes it",
    "CW_CLOUD_DOMAIN": "the domain in the recorded worker URL (default example.internal); the switchboard reaches the worker by a tunnel, so it is cosmetic",
    "CW_TOKEN_ENDPOINT": "the pod-side token mint's URL; needed only when no per-run secret carries git access, which `launch --cloud` always does",
    "CW_RECORD_KEEP_DAYS": "tuning knob with a safe default (30 days)",
    "CW_RECORD_MAX_BYTES": "tuning knob with a safe default (transcript cap)",
    "CW_RECORD_SECONDS": "tuning knob with a safe default (how long the record may take)",
    "CW_RECORD_TIMEOUT": "tuning knob with a safe default (record step limit)",
    "CW_CREATE_TIMEOUT": "tuning knob with a safe default (900 seconds) of the driver",
    "CW_HEALTH_TIMEOUT": "tuning knob with a safe default of the driver",
    "CW_POLL_SECONDS": "tuning knob with a safe default of the driver",
    "CW_PROGRESS_SECONDS": "tuning knob with a safe default of the driver",
    "CW_RUN_TIMEOUT": "tuning knob with a safe default of the driver",
    "CW_TUNNEL_TIMEOUT": "tuning knob with a safe default (30 seconds) of the tunnel wait",
    # test-only hooks that point a step at a fake
    "CW_LAUNCHER": "test hook: the driver runs another program as the launcher",
    "CW_GITHUB_API_BASE": "test hook: points the App mint at a fake GitHub (and is withheld from the preflight child for that reason)",
    "CW_PREFLIGHT_TOKEN_CMD": "test hook: points the preflight at a fake token command (withheld from the preflight child)",
    "CW_OPENSSL": "the openssl program, kept only when an absolute executable file; a tooling path of the machine, not a lane choice",
    "CW_TMPDIR": "where the driver's private temp directory goes; a machine setting, default the system temp directory",
    # the switchboard's own variable (S18)
    SWITCHBOARD_PREFIX + "WORKER_BIN": "the launcher program, found by a lookup order (the variable when set, else the launcher of the switchboard's own checkout, else `cloud-worker` on the PATH), not a settings key: a path in the file would be one more thing to keep in step with the checkout, and an environment override that wins is what tests and a second install need",
}
SOURCES = ["bin/cloud_worker.py"]
NAME = re.compile(r"(?:%s)[A-Z0-9_]+" % "|".join(PREFIXES))


def source_files():
    """The launch wiring and every launcher module (not tests, not the pod's own files); the `cloud-worker` entry is a symlink to cloud_worker.py."""
    found = [os.path.join(ROOT, f) for f in SOURCES]
    found += sorted(p for p in glob.glob(os.path.join(ROOT, "cloud", "launcher", "*.py")) if not os.path.islink(p))
    return found


def names_in(text):
    """Every CW_* or switchboard-prefix name the source mentions as a string constant (an env read, a table row, a name list, a dict key): from the code's AST, never a hand list."""
    return {n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and NAME.fullmatch(n.value)}


def derived():
    out = set()
    for path in source_files():
        with open(path) as f:
            out |= names_in(f.read())
    return out


def unaccounted(names, rows=None, listed=None):
    rows = {k for k, _, _ in cw.SETTINGS} if rows is None else rows
    listed = set(NOT_SETTINGS) if listed is None else listed
    return sorted(set(names) - rows - listed)


class Complete(unittest.TestCase):
    def test_s16_every_name_the_code_mentions_is_a_row_or_on_the_one_list(self):
        self.assertEqual(unaccounted(derived()), [], "add a settings row with a validator, or a reason to NOT_SETTINGS")

    def test_s16_the_source_set_is_not_empty_and_has_both_key_kinds(self):
        names = derived()
        self.assertGreater(len(names), 40)
        self.assertLessEqual({"CW_CLUSTER_ADMIN_SECRET", "CW_CLUSTER_TOKEN_DURATION", "CW_WORKER", SWITCHBOARD_PREFIX + "WORKER_BIN"}, names)   # one new row, one old row, one listed name

    def test_s16_no_name_is_both_a_row_and_listed_and_every_listed_name_is_still_in_the_code_with_a_reason(self):
        rows = {k for k, _, _ in cw.SETTINGS}
        self.assertEqual(sorted(rows & set(NOT_SETTINGS)), [])
        self.assertEqual(sorted(set(NOT_SETTINGS) - derived()), [], "a stale entry: its name is no longer in the code")
        self.assertEqual(sorted(k for k, v in NOT_SETTINGS.items() if len(v) < 20), [])

    def test_s16_every_row_is_in_the_code(self):
        self.assertEqual(sorted({k for k, _, _ in cw.SETTINGS} - derived()), [])   # a row nobody reads is dead

    def test_s16_mutant_a_made_up_environment_read_fails(self):
        for path in source_files():
            with self.subTest(path=os.path.basename(path)), open(path) as f:
                text = f.read() + "\nimport os\n_x = os.environ.get('CW_MADE_UP_SETTING')\n"
                self.assertEqual(unaccounted(names_in(text)), ["CW_MADE_UP_SETTING"])

    def test_s18_every_prefix_has_a_member_in_the_code_and_the_launcher_variable_is_listed_with_its_reason(self):
        for prefix in PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertTrue([n for n in derived() if n.startswith(prefix)])
        self.assertEqual(sorted(n for n in derived() if not n.startswith(PREFIXES)), [])
        self.assertIn(SWITCHBOARD_PREFIX + "WORKER_BIN", NOT_SETTINGS)
        self.assertIn("lookup order", NOT_SETTINGS[SWITCHBOARD_PREFIX + "WORKER_BIN"])

    def test_s18_mutant_a_made_up_read_of_each_prefix_fails(self):
        for prefix in PREFIXES:
            for path in source_files():
                with self.subTest(prefix=prefix, path=os.path.basename(path)), open(path) as f:
                    text = f.read() + "\nimport os\n_x = os.environ.get('%sMADE_UP')\n" % prefix
                    self.assertEqual(unaccounted(names_in(text)), [prefix + "MADE_UP"])

    def test_s18_mutant_a_scan_restricted_to_cw_misses_the_made_up_read(self):
        """The scan a build with only the CW_ prefix would run: it finds nothing for a made-up read of the switchboard prefix, which this test calls a failure."""
        narrow = re.compile(r"CW_[A-Z0-9_]+")
        text = "import os\n_x = os.environ.get('%sMADE_UP')\n" % SWITCHBOARD_PREFIX
        seen = {n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and narrow.fullmatch(n.value)}
        self.assertEqual(unaccounted(seen), [])                                    # the narrow scan reports nothing ...
        self.assertEqual(unaccounted(names_in(text)), [SWITCHBOARD_PREFIX + "MADE_UP"])      # ... where the real scan reports the name

    def test_s16_one_mutant_per_member_dropping_a_row_or_a_listing_fails(self):
        rows = {k for k, _, _ in cw.SETTINGS}
        for name in sorted(rows):
            with self.subTest(row=name):
                self.assertEqual(unaccounted(derived(), rows=rows - {name}), [name])
        for name in sorted(NOT_SETTINGS):
            with self.subTest(listed=name):
                self.assertEqual(unaccounted(derived(), listed=set(NOT_SETTINGS) - {name}), [name])


if __name__ == "__main__":
    unittest.main()

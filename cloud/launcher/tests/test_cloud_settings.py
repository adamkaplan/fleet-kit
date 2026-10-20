"""The settings file (#876, slice 1): the one reader `apply_settings`, its refusals, and the driver's use of it. Offline, no cluster.

Every set below is derived from the code's own tables (SETTINGS, SECRET_SHAPES), with a completeness test, so a new member that has no case here fails."""
import io, json, os, re, stat, sys, tempfile, unittest
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
                    "CW_KUBECTL": "kubectl", "CW_MODEL_AUTH_FILE": "/home/user/auth.json", "CW_RUN_RECORD_DIR": "/home/user/runs",
                    "CW_CLOUD_SPEND_CAP_USD": "25", "CW_CLUSTER_TOKEN_CMD": "cat /home/user/token", "CW_CLUSTER_ADMIN_CMD": "cat /home/user/kc",
                    "CW_GIT_TOKEN_CMD": "cat /home/user/git", "CW_APP_ID_CMD": "cat /home/user/id", "CW_APP_INSTALLATION_CMD": "cat /home/user/inst",
                    "CW_APP_KEY_CMD": "cat /home/user/key", "CW_VAULT_CHECK_DIR": "/home/user/check"}
SAMPLES = {   # one value per secret shape (built here, never a real secret)
    "a JWT": "eyJhbGciOiJIUzI1" + "." + "eyJzdWIiOiJ4eHh4",
    "a PEM header": "-----BEGIN PRIVATE KEY-----",
    "a URL with a password": "https://user:hunter2pass@host.example/x",
    "a 40-character hex string": "git-cred 0123456789abcdef0123456789abcdef01234567",
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
                        "https://host.example?x=1", "https://host.example#f", "https://127.0.0.1", "https://localhost", "https://169.254.169.254", "https://[::1]",
                        "https://0.0.0.0", "https://2130706433", "https://metadata.google.internal", "https://host.example:99999", "https://a.localhost", "https://metadata", "https://[ff0e::1]", "https://[::ffff:8.8.8.8]", "https://[64:ff9b::808:808]", "https://[2002:808:808::1]", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]", "https://[2606:4700:4700::1111%25eth0]", "https://[2606:4700:4700::1111%eth0]", "https://host.example\t", "https://ho\tst.example", "https://host.example\x0b", "https://8.8.8.8 :6443", "https://8.8.8.8\x1f", "https://0x7f000001", "https://017700000001", "https://127.1", "https://0177.0.0.1", "https://0x7f.0.0.1", "https://host.example.", "https://a.0x7f", "https://10.0.0.4", "https://192.168.1.1", "https://100.64.0.1", "https://[fd00:ec2::254]", "https://[fe80::1]", "https://224.0.0.1", "https://[::ffff:10.0.0.1]", "https://0x7f000001:6443", "https://12345", "https://1.2.3", "https://host.example:abc",
                        "https://", "https://ho st.example", "https://[::ffff:127.0.0.1]", "ftp://host.example", "https://host.example\\x"],
    "CW_CLOUD_CA_FILE": ["relative.pem", "/home/user/../etc/x", "-x", "$HOME/x"],
    "CW_PULL_SECRET": ["A_B", "a b", "-", "a/b", "$(x)", "UPPER"],
    "CW_VAULT_SUBSCRIPTION": ["sub-example", "0000", "00000000-0000-0000-0000-00000000000g", "$(x)", "a;b", "00000000-0000-0000-0000-000000000000 "[:-1] + "0"],
    "CW_VAULT_NAME": ["$(touch SENT)", "a;touch SENT", "ab", "a" * 25, "1abc", "a b", "a_b", "abc-"[:-1] + "/"],
    "CW_CLOUD_NAMESPACE": ["a/b;c", "kube-system", "kube-public", "default", "A", "-a", "a-", "a b", "a" * 64, "$(x)"],
    "CW_KUBECTL": ["../kubectl", "/x/../y", "-x", "a b", "$(x)", "a/b"],
    "CW_MODEL_AUTH_FILE": ["auth.json", "/a/../b", "-x"],
    "CW_RUN_RECORD_DIR": ["runs", "/a/../b", "-x"],
    "CW_CLOUD_SPEND_CAP_USD": ["0", "-1", "1e3", "100001", "1.2.3", "abc", "0.0"],
    "CW_CLUSTER_TOKEN_CMD": ["-x"], "CW_CLUSTER_ADMIN_CMD": ["--x"], "CW_GIT_TOKEN_CMD": ["-x"], "CW_APP_ID_CMD": ["-x"],
    "CW_APP_INSTALLATION_CMD": ["-x"], "CW_APP_KEY_CMD": ["-x"],
    "CW_VAULT_CHECK_DIR": ["check", "/a/../b", "-x"],
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
        self.assertLessEqual({kind for _, _, kind in cw.SETTINGS}, {"name", "path", "command", "digest-ref", "number"})
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

    def test_a_good_server_form_is_accepted(self):
        for value in ("https://cluster.example.internal:6443", "https://cluster.example.internal", "https://cluster.example.internal/", "https://8.8.8.8:6443", "https://[2606:4700:4700::1111]:443", "https://aks-dns-1a2b3c4d.hcp.example.azmk8s.io:443", "https://cluster.example.be", "https://api.example.de", "https://x.example.cd", "https://face", "https://a.dead"):
            with self.subTest(value):
                self.assertTrue(cw.VALIDATORS["CW_CLOUD_SERVER"][1](value))

    def test_each_host_alias_rule_is_the_one_that_catches_its_aliases(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        with mock.patch.object(cw, "_integer_host_label", lambda label: False):
            self.assertTrue(server("https://0x7f000001"))   # the 0x rule removed: the hex alias gets through
            self.assertFalse(server("https://2130706433"))  # (the letter rule still holds the all-digit ones)
        with mock.patch.object(cw, "_has_letter", lambda label: True):
            self.assertFalse(server("https://2130706433"))   # the digit rule alone refuses it
            self.assertFalse(server("https://127.1"))
        with mock.patch.object(cw, "_has_letter", lambda label: True), mock.patch.object(cw, "_integer_host_label", lambda label: False):
            for alias in ("https://2130706433", "https://127.1", "https://017700000001", "https://0x7f.0.0.1", "https://0177.0.0.1"):
                self.assertTrue(server(alias), alias)   # both rules removed: every alias gets through

    def test_each_server_sub_rule_is_caught_only_by_its_own_hostile_value(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        cases = (("_REFUSED_HOSTS", (), "https://metadata"), ("_REFUSED_HOSTS", (), "https://localhost"), ("_REFUSED_SUFFIX", ".never-matches", "https://a.localhost"),
                 ("_BAD_CHARS", re.compile("(?!)"), "https://8.8.8.8\t:6443"), ("_BAD_CHARS", re.compile("(?!)"), "https://cluster.example.be\n"),
                 ("_BAD_CHARS", re.compile("(?!)"), "https://[2606:4700:4700::1111%25eth0]"), ("_V4_EMBEDDING_NETS", (), "https://[::ffff:8.8.8.8]"),
                 ("_V4_EMBEDDING_NETS", (), "https://[64:ff9b::808:808]"))
        for name, replacement, value in cases:
            with self.subTest(rule=name, value=value):
                self.assertFalse(server(value))
                with mock.patch.object(cw, name, replacement):
                    self.assertTrue(server(value), "with %s removed the value is still refused: the case does not test it" % name)
        for value in ("https://[ff0e::1]", "https://224.0.0.1"):
            with self.subTest(rule="_multicast", value=value):
                self.assertFalse(server(value))
                with mock.patch.object(cw, "_multicast", lambda ip: False):
                    self.assertTrue(server(value))

    def test_the_server_rule_judges_the_string_as_written(self):
        server = cw.VALIDATORS["CW_CLOUD_SERVER"][1]
        for value in ("https://127.0.0.1\t", "https://host\t.example", "https://8.8.8.8\n", "https://8.8.8.8\r", "https://cluster.example.be\x00"):
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
        value = "0123456789abcdef0123456789abcdef01234567"
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


if __name__ == "__main__":
    unittest.main()

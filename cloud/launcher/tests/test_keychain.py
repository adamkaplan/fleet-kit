"""Ask #2357 (S2-S7): the setting CW_KEYCHAIN_SERVICE and the one keychain read. Offline: a fake `security` (a script in a temp dir that the
module's fixed program name is pointed at), a fake `az` that FAILS and logs if called, a fake kubectl. Every value is built here.
S2 the setting row; S3 the four reads served by `security`, 0 calls of az; S4 no value in an argument or a log, the program is /usr/bin/security;
S5 unset is today's and the command settings win; S6 a missing and an empty item are refused; S7 a mutant that calls az fails with the anchored text."""
import base64, io, os, subprocess, sys, tempfile, types, unittest, zlib
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import keychain_read  # noqa: E402
from test_cloud_settings import GOOD  # noqa: E402

ANCHOR = "az was called with a keychain service set"
SERVICE = "cw-launcher"
APP_ID = "12345" + "678"
APP_KEY = "-----BEGIN " + "TEST KEY-----\nQUJD" + "REVGR0g=\n-----END " + "TEST KEY-----\n"
KUBECONFIG = "apiVersion: v1\nkind: Config\nmarker: " + "k9Zx" * 5 + "\n"
TOKEN = "tok" + "Qm7" * 8   # the cluster token the fake kubectl makes
SOURCE_PATH = os.path.join(LAUNCHER, "cloud_worker.py")


def store(text):
    return base64.b64encode(text.encode()).decode()


FAKE_SECURITY = """#!/bin/sh
echo "$0 $*" >> "%(log)s"
acct=""
while [ $# -gt 0 ]; do [ "$1" = "-a" ] && acct="$2"; shift; done
case "$acct" in *..*|*/*|"") exit 1 ;; esac
[ -f "%(items)s/$acct" ] || exit 44
if [ -f "%(items)s/$acct.hang" ]; then exec sleep 30; fi
cat "%(items)s/$acct"
"""
FAKE_AZ = """#!/bin/sh
echo "$*" >> "%(log)s"
echo "denied" >&2
exit 1
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.root = os.path.realpath(tmp.name)
        os.chmod(d, 0o700)
        self.items = os.path.join(d, "items")
        os.mkdir(self.items)
        self.sec_log, self.az_log = os.path.join(d, "security.log"), os.path.join(d, "az.log")
        self.security = self.script("security", FAKE_SECURITY % {"log": self.sec_log, "items": self.items})
        self.az = self.script("az", FAKE_AZ % {"log": self.az_log})
        self.kubectl = self.script("kubectl", "#!/bin/sh\necho \"$*\" >> \"%s/kubectl.log\"\ncat > /dev/null\nprintf '%%s' %s\n" % (d, TOKEN))
        self.old = keychain_read.PROGRAM, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS
        keychain_read.PROGRAM, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS = self.security, (self.az,), (self.kubectl,)
        self.addCleanup(self.restore)
        self.put("cw-app-id", APP_ID)
        self.put("cw-app-key", APP_KEY)
        self.put("cw-minter-kubeconfig", KUBECONFIG)
        self.put("fk-cloud-workers-admin-kubeconfig", KUBECONFIG)
        self.put("cw-stack-model-key", "stk" + "Hn4" * 6)
        self.env = {"CW_KEYCHAIN_SERVICE": SERVICE, "PATH": "/usr/bin:/bin"}
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["CW_KEYCHAIN_SERVICE"] = SERVICE
        for k in ("CW_APP_ID_CMD", "CW_APP_KEY_CMD", "CW_CLUSTER_ADMIN_CMD"):
            os.environ.pop(k, None)

    def restore(self):
        keychain_read.PROGRAM, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS = self.old

    def script(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def put(self, account, text, raw=False):
        with open(os.path.join(self.items, account), "w") as f:
            f.write((text if raw else store(text)) + "\n")

    def read_log(self, path):
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()


class Setting(Base):
    def apply(self, value):
        path = os.path.join(self.root, "cloud.env")
        with open(path, "w") as f:
            f.write("".join("%s=%s\n" % kv for kv in dict(GOOD, CW_KEYCHAIN_SERVICE=value).items()))
        os.chmod(path, 0o600)
        env = {}
        return cw.apply_settings(env, path), env

    def test_s2_one_value_is_accepted_and_three_are_refused_naming_the_setting(self):
        source, env = self.apply(SERVICE)
        self.assertEqual(env["CW_KEYCHAIN_SERVICE"], SERVICE)
        for value in ("a b", "x" * 65, ""):
            with self.subTest(value=value[:8]):
                with self.assertRaises(cw.CloudError) as caught:
                    self.apply(value)
                self.assertIn("CW_KEYCHAIN_SERVICE", str(caught.exception))
                self.assertNotIn("\n", str(caught.exception).strip())

    def test_s2_a_leading_dash_and_a_slash_are_refused_too_and_64_is_accepted(self):
        self.apply("a" * 64)
        for value in ("-x", "a/b", "a;b", "\u00e9a"):
            with self.subTest(value=value):
                with self.assertRaises(cw.CloudError):
                    self.apply(value)

    def test_s2_the_preflight_child_keeps_the_setting(self):
        self.assertIn("CW_KEYCHAIN_SERVICE", cw.PREFLIGHT_ENV_NAMES)


class Reads(Base):
    def cluster(self, module=None, strict=False, env=None):
        module = module or cw
        env = dict(env or self.env, CW_VAULT_SUBSCRIPTION=GOOD["CW_VAULT_SUBSCRIPTION"], CW_VAULT_NAME=GOOD["CW_VAULT_NAME"])
        calls = self.calls = []

        def capture(argv, env=None, feed=None, **kw):
            calls.append(list(argv))
            name = os.path.basename(argv[0])
            if name == "az":
                subprocess.run([self.az], capture_output=True)   # the fake az logs the call and fails
                return (1, "", "denied")
            return (0, "kc" if name == "sh" else TOKEN, "")
        err = io.StringIO()
        with redirect_stderr(err):
            token = module.cluster_token(env, "20m", capture=capture, strict=strict)
        return token, calls

    def check_all(self, module=None):
        """The four reads (the App id and key, the cluster login both ways) served by security with az uncalled; the S3 check, run on any build."""
        self.assertEqual(app_token.vault("CW_APP_ID_CMD", "cw-app-id"), APP_ID)
        self.assertEqual(app_token.vault("CW_APP_KEY_CMD", "cw-app-key"), APP_KEY)
        for strict in (False, True):
            token, calls = self.cluster(module, strict=strict)
            self.assertEqual(token, TOKEN)
            self.assertEqual(self.read_log(self.az_log), "", ANCHOR)
            self.assertFalse([c for c in calls if os.path.basename(c[0]) == "az"], ANCHOR)

    def test_s3_every_read_is_served_by_security_and_az_is_never_called(self):
        self.check_all()
        log = self.read_log(self.sec_log)
        for account in ("cw-app-id", "cw-app-key", "cw-minter-kubeconfig"):
            self.assertIn("-a " + account, log)
        self.assertEqual(self.read_log(self.az_log), "")

    def test_s3_the_stack_read_is_served_by_security(self):
        import test_stack_model_key as t
        binmod = t.load_bin()
        env = dict(self.env, CW_STACK_MODEL_SECRET="cw-stack-model-key", CW_VAULT_SUBSCRIPTION=GOOD["CW_VAULT_SUBSCRIPTION"], CW_VAULT_NAME="vault-example",
                   PATH=os.path.dirname(self.az) + ":/usr/bin:/bin")
        self.assertEqual(binmod.stack_model_secret(env), ("cw-stack-model-key", "stk" + "Hn4" * 6))
        self.assertEqual(self.read_log(self.az_log), "")
        self.assertIn("-a cw-stack-model-key", self.read_log(self.sec_log))

    def test_s3_the_cluster_login_names_the_item_by_the_setting_else_the_minter_kubeconfig(self):
        self.cluster()
        self.assertIn("-a cw-minter-kubeconfig", self.read_log(self.sec_log))
        self.cluster(env=dict(self.env, CW_CLUSTER_ADMIN_SECRET="fk-cloud-workers-admin-kubeconfig"))
        self.assertIn("-a fk-cloud-workers-admin-kubeconfig", self.read_log(self.sec_log))

    def test_s4_no_value_in_an_argument_or_a_log_and_the_program_is_the_fixed_path(self):
        self.assertEqual(self.old[0], "/usr/bin/security")   # the real constant (the test points it at its fake)
        err = io.StringIO()
        with redirect_stderr(err):
            self.check_all()
        text = self.read_log(self.sec_log)
        for value in (APP_ID, store(APP_ID), "marker", TOKEN, store(APP_KEY), store(KUBECONFIG)):
            self.assertNotIn(value, text + err.getvalue())
        self.assertTrue(all(line.startswith(self.security + " find-generic-password -s %s -a " % SERVICE) and line.endswith(" -w") for line in text.splitlines()), text)

    def test_s5_unset_the_commands_are_todays_az_vault_route(self):
        os.environ.pop("CW_KEYCHAIN_SERVICE")
        os.environ.update(CW_VAULT_SUBSCRIPTION=GOOD["CW_VAULT_SUBSCRIPTION"], CW_VAULT_NAME="vault-example")
        saw = []
        real = subprocess.run
        subprocess.run = lambda argv, **kw: saw.append(argv) or types.SimpleNamespace(stdout="v\n", stderr="", returncode=0)
        try:
            self.assertEqual(app_token.vault("CW_APP_ID_CMD", "cw-app-id"), "v")
        finally:
            subprocess.run = real
        self.assertEqual(saw, [["az", "keyvault", "secret", "show", "--subscription", GOOD["CW_VAULT_SUBSCRIPTION"], "--vault-name", "vault-example",
                                "--name", "cw-app-id", "--query", "value", "-o", "tsv"]])
        self.assertEqual(self.read_log(self.sec_log), "")
        with self.assertRaises(cw.ClusterLogin):   # the fake az fails: the call itself is what is measured
            self.cluster(env={k: v for k, v in self.env.items() if k != "CW_KEYCHAIN_SERVICE"})
        self.assertEqual(os.path.basename(self.calls[0][0]), "az")
        self.assertIn("cw-cluster-admin-kubeconfig", self.calls[0])

    def test_s5_the_command_settings_win_over_the_keychain(self):
        os.environ["CW_APP_KEY_CMD"] = "printf '%s' command-key"
        self.assertEqual(app_token.vault("CW_APP_KEY_CMD", "cw-app-key"), "command-key")
        self.assertNotIn("cw-app-key", self.read_log(self.sec_log))
        token, calls = self.cluster(env=dict(self.env, CW_CLUSTER_ADMIN_CMD="printf kc"))
        self.assertEqual(calls[0][:2], ["sh", "-c"])
        self.assertNotIn("-a cw-minter-kubeconfig", self.read_log(self.sec_log))

    def test_s6_a_missing_item_and_an_empty_one_are_refused_with_a_vault_kind_and_no_content(self):
        os.remove(os.path.join(self.items, "cw-app-id"))
        self.put("cw-app-key", "")
        for secret in ("cw-app-id", "cw-app-key"):
            with self.subTest(secret=secret):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit):
                    app_token.vault("CW_APP_ID_CMD", secret)
                self.assertEqual(app_token.REFUSAL["label"], "vault other")
                self.assertNotIn(APP_KEY, err.getvalue())
        os.remove(os.path.join(self.items, "cw-minter-kubeconfig"))
        for strict in (False, True):
            with self.assertRaises(cw.ClusterLogin) as caught:
                self.cluster(strict=strict)
            self.assertEqual(caught.exception.kind, "admin-unreadable")

    def test_s6_text_that_is_not_base64_or_zlib_is_refused_without_content(self):
        for text in ("not base64 !!", "gz:" + store("plain, not zlib"), "gz:%%%", store("ok")[:-1]):
            with self.subTest(text=text[:10]):
                self.put("cw-app-id", text, raw=True)
                with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                    app_token.vault("CW_APP_ID_CMD", "cw-app-id")
                self.assertEqual(app_token.REFUSAL["label"], "vault other")
                self.assertNotIn(text, app_token.REFUSAL["line"])

    def test_the_gz_form_and_the_cap(self):
        big = "line of a long kubeconfig %d\n" * 400
        packed = "gz:" + base64.b64encode(zlib.compress(big.encode())).decode()
        self.put("cw-minter-kubeconfig", packed, raw=True)
        self.assertEqual(keychain_read.read(SERVICE, "cw-minter-kubeconfig", trim=False), big)
        with self.assertRaises(keychain_read.KeychainError):
            keychain_read.read(SERVICE, "cw-minter-kubeconfig", limit=100)

    def test_a_timeout_is_keychain_unavailable_with_the_one_clear_line(self):
        open(os.path.join(self.items, "cw-app-id.hang"), "w").close()
        old, keychain_read.TIMEOUT_SECONDS = keychain_read.TIMEOUT_SECONDS, 1
        self.addCleanup(setattr, keychain_read, "TIMEOUT_SECONDS", old)
        with self.assertRaises(keychain_read.KeychainError) as caught:
            keychain_read.read(SERVICE, "cw-app-id")
        self.assertEqual((caught.exception.kind, caught.exception.line), ("keychain-unavailable", "the login keychain is locked or asks for access"))
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            app_token.vault("CW_APP_ID_CMD", "cw-app-id")
        self.assertEqual(app_token.REFUSAL["label"], "keychain-unavailable")
        self.assertIn("locked", app_token.REFUSAL["line"])

    def test_an_attacker_service_or_item_name_never_reaches_the_program(self):
        for service in ("-s", "a b", "a;rm", "$(x)", "x" * 65, "", "a\nb"):
            with self.assertRaises(keychain_read.KeychainError):
                keychain_read.read(service, "cw-app-id")
        for account in ("-w", "a b", "../x", ""):
            with self.assertRaises(keychain_read.KeychainError):
                keychain_read.read(SERVICE, account)
        self.assertEqual(self.read_log(self.sec_log), "")


class Mutant(Reads):
    def scratch(self):
        with open(SOURCE_PATH) as f:
            text = f.read()
        old = "        elif keychain_service(env):"
        self.assertEqual(text.count(old), 1)
        mutated = text.replace(old, "        elif False:")   # the launcher that reads the vault with az even when the service is set
        self.assertNotEqual(mutated, text)
        module = types.ModuleType("cloud_worker_keychain_mutant")
        module.__file__ = SOURCE_PATH
        exec(compile(mutated, SOURCE_PATH, "exec"), module.__dict__)
        module.MINT_AZ_PATHS, module.MINT_KUBECTL_PATHS = cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS
        return module

    def run_mutant_check(self, module):
        for strict in (False, True):
            self.cluster(module, strict=strict)
            self.assertEqual(self.read_log(self.az_log), "", ANCHOR)

    def test_s7_control_passes_then_the_mutant_fails_with_the_anchored_text(self):
        self.run_mutant_check(cw)   # the control first
        module = self.scratch()
        with self.assertRaises(AssertionError) as caught:
            try:
                self.run_mutant_check(module)
            except module.ClusterLogin:
                self.fail(ANCHOR)   # the mutant's az read failed: that IS the az call
        self.assertIn(ANCHOR, str(caught.exception))



if __name__ == "__main__":
    unittest.main()

"""Ask #2357 (S2-S7): the setting CW_SECRET_DIR and the one secret-file read. Offline: a temp directory stands for the home (a test hook), a fake `az`
that FAILS and logs if called, a fake kubectl. Every value is built here; no test touches the real keychain or the real home.
S2 the setting row; S3 the four reads served by the files, 0 calls of az; S4 no value in an argument, a log or stderr and no program run;
S5 unset is today's and the command settings win; S6 a missing, empty, wrong-mode, linked or oversize file is refused with the vault kind;
S7 a mutant that calls az fails with the anchored text."""
import io, os, subprocess, sys, tempfile, types, unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import secret_files  # noqa: E402
from test_cloud_settings import GOOD  # noqa: E402

ANCHOR = "az was called with a secret directory set"
APP_ID = "12345" + "678"
APP_KEY = "-----BEGIN " + "TEST KEY-----\nQUJD" + "REVGR0g=\n-----END " + "TEST KEY-----\n"
KUBECONFIG = "apiVersion: v1\nkind: Config\nmarker: " + "k9Zx" * 5 + "\n"
STACK = "stk" + "Hn4" * 6
TOKEN = "tok" + "Qm7" * 8   # the cluster token the fake capture makes
SOURCE_PATH = os.path.join(LAUNCHER, "cloud_worker.py")
FAKE_AZ = "#!/bin/sh\necho \"$*\" >> \"%(log)s\"\necho denied >&2\nexit 1\n"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        os.chmod(self.root, 0o700)
        self.dir = os.path.join(self.root, "secrets")
        os.mkdir(self.dir, 0o700)
        self.az_log = os.path.join(self.root, "az.log")
        self.az = self.script("az", FAKE_AZ % {"log": self.az_log})
        self.kubectl = self.script("kubectl", "#!/bin/sh\nexit 0\n")
        self.old = secret_files.HOME, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS
        secret_files.HOME, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS = self.root, (self.az,), (self.kubectl,)
        self.addCleanup(self.restore)
        for name, text in (("cw-app-id", APP_ID), ("cw-app-key", APP_KEY), ("cw-minter-kubeconfig", KUBECONFIG),
                           ("fk-cloud-workers-admin-kubeconfig", KUBECONFIG), ("cw-stack-model-key", STACK)):
            self.put(name, text)
        self.env = {"CW_SECRET_DIR": self.dir, "PATH": "/usr/bin:/bin"}
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["CW_SECRET_DIR"] = self.dir
        os.environ["PATH"] = os.path.dirname(self.az) + ":" + os.environ.get("PATH", "")   # a real az call would reach the fake
        for k in ("CW_APP_ID_CMD", "CW_APP_KEY_CMD", "CW_CLUSTER_ADMIN_CMD"):
            os.environ.pop(k, None)
        app_token.REFUSAL.clear()

    def restore(self):
        secret_files.HOME, cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS = self.old

    def script(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def put(self, name, text, mode=0o600):
        path = os.path.join(self.dir, name)
        if os.path.lexists(path):
            os.remove(path)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)

    def az_calls(self):
        if not os.path.exists(self.az_log):
            return ""
        with open(self.az_log) as f:
            return f.read()

    def vault_refused(self, secret="cw-app-id"):
        app_token.REFUSAL.clear()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            app_token.vault("CW_APP_ID_CMD", secret)
        self.assertEqual(app_token.REFUSAL["label"], "vault other")
        return err.getvalue()


class Setting(Base):
    def apply(self, value):
        path = os.path.join(self.root, "cloud.env")
        with open(path, "w") as f:
            f.write("".join("%s=%s\n" % kv for kv in dict(GOOD, CW_SECRET_DIR=value).items()))
        os.chmod(path, 0o600)
        env = {}
        cw.apply_settings(env, path)
        return env

    def test_s2_one_value_is_accepted_and_three_are_refused_naming_the_setting(self):
        self.assertEqual(self.apply(self.dir)["CW_SECRET_DIR"], self.dir)
        for value in ("a b", "/etc", "", self.root + "/../x", "/tmp/x"):
            with self.subTest(value=value):
                with self.assertRaises(cw.CloudError) as caught:
                    self.apply(value)
                self.assertIn("CW_SECRET_DIR", str(caught.exception))
                self.assertNotIn("\n", str(caught.exception).strip())

    def test_s2_the_preflight_child_keeps_the_setting(self):
        self.assertIn("CW_SECRET_DIR", cw.PREFLIGHT_ENV_NAMES)


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
            self.fed = feed
            return (0, "kc" if name == "sh" else TOKEN, "")
        self.err = io.StringIO()
        with redirect_stderr(self.err):
            return module.cluster_token(env, "20m", capture=capture, strict=strict)

    def check_all(self, module=None):
        """The four reads served by the files with az uncalled (S3); run on any build."""
        self.assertEqual(app_token.vault("CW_APP_ID_CMD", "cw-app-id"), APP_ID)
        self.assertEqual(app_token.vault("CW_APP_KEY_CMD", "cw-app-key"), APP_KEY)
        for strict in (False, True):
            self.assertEqual(self.cluster(module, strict=strict), TOKEN)
            self.assertEqual(self.fed, KUBECONFIG.strip())   # the administrator text reached the mint on the pipe
            self.assertEqual(self.az_calls(), "", ANCHOR)
            self.assertFalse([c for c in self.calls if os.path.basename(c[0]) == "az"], ANCHOR)

    def test_s3_every_read_is_served_by_the_files_and_az_is_never_called(self):
        self.check_all()
        self.assertEqual(self.az_calls(), "")

    def test_s3_the_stack_read_is_served_by_the_file(self):
        import test_stack_model_key as t
        binmod = t.load_bin()
        env = dict(self.env, CW_STACK_MODEL_SECRET="cw-stack-model-key", CW_VAULT_SUBSCRIPTION=GOOD["CW_VAULT_SUBSCRIPTION"], CW_VAULT_NAME="vault-example")
        self.assertEqual(binmod.stack_model_secret(env), ("cw-stack-model-key", STACK))
        self.assertEqual(self.az_calls(), "")

    def test_s3_the_cluster_login_names_the_file_by_the_setting_else_the_minter_kubeconfig(self):
        os.remove(os.path.join(self.dir, "fk-cloud-workers-admin-kubeconfig"))
        self.put("cw-minter-kubeconfig", "minter " + KUBECONFIG)
        self.cluster()
        self.assertEqual(self.fed, "minter " + KUBECONFIG.strip())
        self.put("fk-cloud-workers-admin-kubeconfig", "admin " + KUBECONFIG)
        self.cluster(env=dict(self.env, CW_CLUSTER_ADMIN_SECRET="fk-cloud-workers-admin-kubeconfig"))
        self.assertEqual(self.fed, "admin " + KUBECONFIG.strip())

    def test_s4_no_program_is_run_and_no_value_reaches_an_argument_a_log_or_stderr(self):
        ran = []
        for target in ("subprocess.run", "subprocess.Popen"):   # named by string: the real call is never made and nothing is left patched
            patcher = mock.patch(target, side_effect=lambda *a, **k: ran.append(a))
            patcher.start()
            self.addCleanup(patcher.stop)
        err = io.StringIO()
        with redirect_stderr(err):
            self.check_all()
        self.assertEqual(ran, [])   # not even a fake az: no program of any kind
        seen = err.getvalue() + self.err.getvalue() + " ".join(" ".join(c) for c in self.calls)
        for value in (APP_ID, APP_KEY, "marker", TOKEN, STACK):
            self.assertNotIn(value, seen)

    def test_s5_unset_the_reads_are_todays_az_vault_route(self):
        os.environ.pop("CW_SECRET_DIR")
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
        with self.assertRaises(cw.ClusterLogin):   # the fake az fails: the call itself is what is measured
            self.cluster(env={k: v for k, v in self.env.items() if k != "CW_SECRET_DIR"})
        self.assertEqual(os.path.basename(self.calls[0][0]), "az")
        self.assertIn("cw-cluster-admin-kubeconfig", self.calls[0])

    def test_s5_the_command_settings_win_over_the_files(self):
        os.environ["CW_APP_KEY_CMD"] = "printf '%s' command-key"
        self.assertEqual(app_token.vault("CW_APP_KEY_CMD", "cw-app-key"), "command-key")
        self.cluster(env=dict(self.env, CW_CLUSTER_ADMIN_CMD="printf kc"))
        self.assertEqual(self.calls[0][:2], ["sh", "-c"])

    def test_s6_a_missing_and_an_empty_file_are_refused_with_the_vault_kind_and_no_content(self):
        os.remove(os.path.join(self.dir, "cw-app-id"))
        self.put("cw-app-key", "  \n")
        self.assertNotIn(APP_KEY, self.vault_refused("cw-app-id"))
        self.vault_refused("cw-app-key")
        os.remove(os.path.join(self.dir, "cw-minter-kubeconfig"))
        for strict in (False, True):
            with self.assertRaises(cw.ClusterLogin) as caught:
                self.cluster(strict=strict)
            self.assertEqual(caught.exception.kind, "admin-unreadable")
        self.put("cw-minter-kubeconfig", "")
        with self.assertRaises(cw.ClusterLogin):
            self.cluster()

    def test_s6_a_wrong_mode_a_link_a_big_file_and_a_bad_directory_are_refused_without_content(self):
        self.put("cw-app-id", APP_ID, 0o640)
        self.assertNotIn(APP_ID, self.vault_refused())
        self.put("cw-app-id", APP_ID, 0o644)
        self.vault_refused()
        self.put("cw-app-id", APP_ID, 0o400)
        self.vault_refused()
        os.remove(os.path.join(self.dir, "cw-app-id"))
        target = os.path.join(self.root, "elsewhere")
        with open(target, "w") as f:
            f.write(APP_ID)
        os.chmod(target, 0o600)
        os.symlink(target, os.path.join(self.dir, "cw-app-id"))
        self.assertNotIn(APP_ID, self.vault_refused())
        self.put("cw-app-id", "x" * (secret_files.LIMIT + 1))
        self.vault_refused()
        self.put("cw-app-id", APP_ID)
        os.chmod(self.dir, 0o750)
        self.vault_refused()
        os.chmod(self.dir, 0o700)
        link = os.path.join(self.root, "linked")
        os.symlink(self.dir, link)
        os.environ["CW_SECRET_DIR"] = link
        self.vault_refused()
        os.environ["CW_SECRET_DIR"] = "/etc"   # outside the home
        self.vault_refused()
        os.environ["CW_SECRET_DIR"] = self.dir
        self.assertEqual(app_token.vault("CW_APP_ID_CMD", "cw-app-id"), APP_ID)   # the control: all good again

    def test_a_directory_of_another_owner_is_refused(self):
        real = os.geteuid
        os.geteuid = lambda: real() + 1
        try:
            self.vault_refused()
        finally:
            os.geteuid = real

    def test_a_file_name_that_could_leave_the_directory_is_refused(self):
        for name in ("../x", "-w", "a/b", "", "a b"):
            with self.assertRaises(secret_files.SecretError):
                secret_files.read(self.dir, name)


class Mutant(Base):
    def scratch(self):
        with open(SOURCE_PATH) as f:
            text = f.read()
        old = "        elif secret_dir(env):"
        self.assertEqual(text.count(old), 1)
        mutated = text.replace(old, "        elif False:")   # the launcher that reads the vault with az even when the directory is set
        self.assertNotEqual(mutated, text)
        module = types.ModuleType("cloud_worker_secret_mutant")
        module.__file__ = SOURCE_PATH
        exec(compile(mutated, SOURCE_PATH, "exec"), module.__dict__)
        module.MINT_AZ_PATHS, module.MINT_KUBECTL_PATHS = cw.MINT_AZ_PATHS, cw.MINT_KUBECTL_PATHS
        return module

    def check(self, module):
        for strict in (False, True):
            try:
                Reads.cluster(self, module, strict=strict)
            except module.ClusterLogin:
                pass   # the mutant's az read was refused by the fake; the log below says it was called
            self.assertEqual(self.az_calls(), "", ANCHOR)

    def test_s7_control_passes_then_the_mutant_fails_with_the_anchored_text(self):
        self.check(cw)   # the control first
        with self.assertRaises(AssertionError) as caught:
            self.check(self.scratch())
        self.assertIn(ANCHOR, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

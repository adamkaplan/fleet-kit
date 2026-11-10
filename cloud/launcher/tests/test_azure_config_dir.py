"""Ask #2335 (S2-S5): the setting CW_AZURE_CONFIG_DIR. Offline; a settings file in a temp dir; no `az`, no network.

S2: the file's directory reaches the child environment of a vault read and replaces the caller's. S3: unset, the result is today's.
S4: a relative, missing, regular-file or empty value is refused naming the setting and no value. S5: a scratch copy of the launcher that ignores
the setting fails S2's check with the anchored text (its unmutated control passes first)."""
import importlib.util, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_settings import GOOD  # noqa: E402  (a complete good settings set)

ANCHOR = "the settings sign-in was not used"
BLOCK = '    if "CW_AZURE_CONFIG_DIR" in values:'


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        os.chmod(self.root, 0o700)
        self.settings_dir = os.path.join(self.root, "settings-az")
        self.caller_dir = os.path.join(self.root, "caller-az")
        os.mkdir(self.settings_dir)
        os.mkdir(self.caller_dir)
        self.path = os.path.join(self.root, "cloud.env")

    def write(self, extra):
        values = dict(GOOD, **extra)
        with open(self.path, "w") as f:
            f.write("".join("%s=%s\n" % kv for kv in values.items()))
        os.chmod(self.path, 0o600)

    def child(self, module, extra, caller):
        """Apply the settings file to a caller's environment, then build the vault read's clean child environment (both builders)."""
        self.write(extra)
        env = dict(caller)
        module.apply_settings(env, self.path)
        scrubbed = module._scrubbed_env(env, self.root, module.VAULT_CHECK_ENV_NAMES, openssl=False)
        return scrubbed, module.mint_env(env)

    def check_used(self, module):   # S2's check, run against the real launcher and the mutant alike
        scrubbed, minted = self.child(module, {"CW_AZURE_CONFIG_DIR": self.settings_dir}, {"AZURE_CONFIG_DIR": self.caller_dir, "HOME": self.root})
        for got in (scrubbed["AZURE_CONFIG_DIR"], minted["AZURE_CONFIG_DIR"]):
            self.assertEqual(got, self.settings_dir, ANCHOR)


class Used(Base):
    def test_s2_the_settings_directory_replaces_the_callers(self):
        self.check_used(cw)

    def test_s2_the_caller_without_a_value_gets_the_settings_directory_too(self):
        scrubbed, minted = self.child(cw, {"CW_AZURE_CONFIG_DIR": self.settings_dir}, {"HOME": self.root})
        self.assertEqual((scrubbed["AZURE_CONFIG_DIR"], minted["AZURE_CONFIG_DIR"]), (self.settings_dir, self.settings_dir))

    def test_s3_unset_the_result_is_todays(self):
        for caller in ({"AZURE_CONFIG_DIR": self.caller_dir, "HOME": self.root}, {"HOME": self.root}):
            with self.subTest(caller=sorted(caller)):
                self.write({})
                env = dict(caller)
                cw.apply_settings(env, self.path)
                self.assertEqual(env.get("AZURE_CONFIG_DIR"), caller.get("AZURE_CONFIG_DIR"))
                self.assertNotIn("CW_AZURE_CONFIG_DIR", env)
                scrubbed = cw._scrubbed_env(env, self.root, cw.VAULT_CHECK_ENV_NAMES, openssl=False)
                again = cw._scrubbed_env(dict(caller), self.root, cw.VAULT_CHECK_ENV_NAMES, openssl=False)
                self.assertEqual(scrubbed, again)
                self.assertEqual(cw.mint_env(env), cw.mint_env(dict(caller)))
                if "AZURE_CONFIG_DIR" in caller:
                    self.assertEqual(scrubbed["AZURE_CONFIG_DIR"], self.caller_dir)
                else:
                    self.assertNotIn("AZURE_CONFIG_DIR", cw.mint_env(env))


class Refused(Base):
    def test_s4_four_bad_values_are_refused_naming_the_setting_and_no_value(self):
        regular = os.path.join(self.root, "a-file")
        open(regular, "w").close()
        for value in ("relative/dir", os.path.join(self.root, "no-such-dir"), regular, ""):
            with self.subTest(value=value[-12:]):
                self.write({"CW_AZURE_CONFIG_DIR": value})
                env = {"AZURE_CONFIG_DIR": self.caller_dir}
                if value == "":   # an empty value counts as not set, as for every setting: nothing changes
                    cw.apply_settings(env, self.path)
                    self.assertEqual(env["AZURE_CONFIG_DIR"], self.caller_dir)
                    continue
                with self.assertRaises(cw.CloudError) as caught:
                    cw.apply_settings(env, self.path)
                text = str(caught.exception)
                self.assertIn("CW_AZURE_CONFIG_DIR", text)
                self.assertNotIn(value, text)
                self.assertNotIn("\n", text.strip())
                self.assertEqual(env, {"AZURE_CONFIG_DIR": self.caller_dir})   # nothing was applied

    def test_s4_a_dot_dot_segment_is_refused(self):
        self.write({"CW_AZURE_CONFIG_DIR": self.settings_dir + "/../settings-az"})
        with self.assertRaises(cw.CloudError) as caught:
            cw.apply_settings({}, self.path)
        self.assertIn("CW_AZURE_CONFIG_DIR", str(caught.exception))


class Mutant(Base):
    def scratch(self):
        with open(cw.__file__) as f:
            text = f.read()
        self.assertEqual(text.count(BLOCK), 1)
        i = text.index(BLOCK)
        j = text.index("    return path\n", i)
        mutated = text[:i] + text[j:]   # the launcher with the two inserted lines removed: it ignores the setting
        self.assertNotEqual(mutated, text)
        path = os.path.join(self.root, "cloud_worker_mutant.py")
        with open(path, "w") as f:
            f.write(mutated)
        spec = importlib.util.spec_from_file_location("cloud_worker_mutant", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cloud_worker_mutant"] = module
        self.addCleanup(sys.modules.pop, "cloud_worker_mutant", None)
        spec.loader.exec_module(module)
        return module

    def test_s5_control_passes_then_the_mutant_fails_with_the_anchored_text(self):
        self.check_used(cw)   # the control first: the unmutated launcher passes
        mutant = self.scratch()
        with self.assertRaises(AssertionError) as caught:
            self.check_used(mutant)
        self.assertIn(ANCHOR, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

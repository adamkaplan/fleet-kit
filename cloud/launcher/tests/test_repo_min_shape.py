"""CW_REPO_MIN_SHAPE (#1443): a per-repository floor for the pod shape.

Offline: the fake kubectl of the launcher tests records what is applied. Each mutant is an in-memory change that must make S3 fail."""
import argparse, contextlib, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

REPO = "acme-org/widget-service"


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class Validator(unittest.TestCase):
    """S2: the setting's own validator, called directly and through the real file parse."""
    KEY = "CW_REPO_MIN_SHAPE"

    def parse(self, value):
        row = "\n".join("%s=%s" % (k, v) for k, v in {
            "CW_ALLOWED_REPOS": REPO, "CW_CLOUD_IMAGE": "registry.example.internal/acme/worker@sha256:" + "ab12" * 16,
            "CW_CLOUD_SERVER": "https://cluster.example.internal:6443", "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000",
            "CW_VAULT_NAME": "vault-example", "CW_CLOUD_NAMESPACE": "cw"}.items())
        return cw.parse_settings(row + "\n" + self.KEY + "=" + value + "\n")

    def test_a_good_list_is_accepted(self):
        self.assertEqual(self.parse("Acme-Org/Widget-Service=S, acme-org/other=M")[self.KEY], "Acme-Org/Widget-Service=S, acme-org/other=M")
        self.assertEqual(cw.repo_min_shapes("Acme-Org/Widget-Service=S"), {REPO: "S"})

    def test_each_bad_list_refuses_naming_the_key_and_no_value(self):
        bad = ["acme-org/widget-service", "acme-org/widget-service=", "acme-org/widget-service=L", "acme-org/widget-service=s", "widget-service=S",
               "acme-org/*=S", "acme-org/a=S,ACME-ORG/A=M", "acme-org/a=S,,acme-org/b=M", "acme-org/a=S,acme-org/b"]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(cw.CloudError) as ctx:
                    self.parse(value)
                text = str(ctx.exception)
                self.assertIn(self.KEY, text)
                self.assertNotIn(value, text)
        self.assertIsNone(cw.repo_min_shapes("acme-org/a=S\nacme-org/b=M"))
        self.assertIsNone(cw.repo_min_shapes("acme-org/a=S\x07"))
        self.assertFalse(cw.VALIDATORS[self.KEY][1]("acme-org/a=S\n"))

    def test_the_row_is_optional_and_has_a_kind_and_a_rule(self):
        self.assertIn((self.KEY, False, "name"), cw.SETTINGS)
        self.assertIn(self.KEY, cw.VALIDATORS)


class Floor(base.Base):
    """S3 and S4: the launch."""
    SETTING = "acme-org/widget-service=S"

    def made(self, *extra, repo=REPO, setting="__default__"):
        setting = self.SETTING if setting == "__default__" else setting
        if setting is None:
            self.env.pop("CW_REPO_MIN_SHAPE", None)
        else:
            self.env["CW_REPO_MIN_SHAPE"] = setting
        self.write_state({"pods": {}})
        code, out, err = self.run_cli("create", "coder7", "--repo", repo, "--branch", "main", *extra)
        pods = [o for o in self.st()["applied"] if o["kind"] == "Pod"] if "applied" in self.st() else []
        return code, err, (pods[0]["metadata"]["annotations"]["cw/shape"] if pods else None)

    def objects(self):
        return self.st().get("applied", [])

    def test_s3_a_listed_repository_gets_its_floor_and_a_smaller_shape_is_refused(self):
        code, err, shape = self.made()
        self.assertEqual((code, shape), (0, "S"), err)                      # no flag: the floor
        code, err, shape = self.made("--shape", "XS")
        self.assertEqual((code, shape), (2, None), err)                     # smaller: refused
        self.assertEqual(self.objects(), [], "0 objects created")
        lines = [x for x in err.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, err)
        for word in (REPO, "S", "CW_REPO_MIN_SHAPE"):
            self.assertIn(word, lines[0])
        code, err, shape = self.made("--shape", "S")
        self.assertEqual((code, shape), (0, "S"), err)                      # same
        code, err, shape = self.made("--shape", "M")
        self.assertEqual((code, shape), (0, "M"), err)                      # larger
        code, err, shape = self.made(setting="ACME-ORG/Widget-Service=M")
        self.assertEqual((code, shape), (0, "M"), err)                      # case-insensitive

    def test_s3_a_stack_launch_takes_the_larger_shape(self):
        code, err, shape = self.made("--stack", setting="acme-org/widget-service=M")
        self.assertEqual((code, shape), (0, "M"), err)
        code, err, shape = self.made("--stack", setting="acme-org/widget-service=XS")
        self.assertEqual((code, shape), (0, "S"), err)

    def test_s4_an_unlisted_repository_and_an_unset_setting_change_nothing(self):
        self.assertEqual(self.made(repo="a/b")[::2], (0, "XS"))
        self.assertEqual(self.made("--shape", "XS", repo="a/b")[::2], (0, "XS"))
        self.assertEqual(self.made(setting=None)[::2], (0, "XS"))
        self.assertEqual(self.made("--shape", "XS", setting=None)[::2], (0, "XS"))

    def test_s3_check_shape_directly(self):
        for kw, want in ((dict(repo=REPO), "S"), (dict(repo=REPO, agent="orchestrator"), "S"), (dict(repo="a/b"), "XS")):
            a = argparse.Namespace(**kw)
            cw.check_shape(a, {"CW_REPO_MIN_SHAPE": self.SETTING})
            self.assertEqual(a.shape, want)

    # S5: a mutant that ignores the setting
    @contextlib.contextmanager
    def ignoring_the_setting(self):
        real = cw.check_shape
        cw.check_shape = lambda a, env=None: real(a, None)
        try:
            self.assertIsNot(cw.check_shape, real, "the mutation is applied")
            yield
        finally:
            cw.check_shape = real

    def test_s5_a_mutant_that_ignores_the_setting_fails_s3(self):
        for name in ("test_s3_a_listed_repository_gets_its_floor_and_a_smaller_shape_is_refused", "test_s3_a_stack_launch_takes_the_larger_shape",
                     "test_s3_check_shape_directly"):
            with self.ignoring_the_setting():
                result = unittest.TestResult()
                type(self)(name).run(result)
                self.assertTrue(result.failures or result.errors, name)
            result = unittest.TestResult()
            type(self)(name).run(result)
            self.assertTrue(result.wasSuccessful(), name)


class Docs(unittest.TestCase):
    """S1 and S6: both documents say what, why, how, and that the change holds no repository name."""
    DOCS = ("cloud/launcher/README.md", "docs/cloud-worker-interface.md")

    def test_each_document_states_the_setting(self):
        for rel in self.DOCS:
            with self.subTest(doc=rel):
                text = read(rel)
                i = text.index("CW_REPO_MIN_SHAPE")
                section = text[max(0, i - 200):i + 3000]
                self.assertIn("OWNER/NAME=SHAPE", section)                      # what and how to set it
                self.assertIn("settings file", section)
                self.assertIn("exit 137", section)                              # why
                self.assertRegex(section, r"three pods")
                self.assertRegex(section, r"1 CPU,? 2 GiB")
                self.assertIn("2.5 to 7.3 minutes", section)
                self.assertIn("check_shape", section)                           # the function that chooses the shape
                self.assertRegex(section, r"(?i)holds no repository name")


if __name__ == "__main__":
    unittest.main()

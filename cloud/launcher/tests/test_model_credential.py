"""#1088: the live model credential is read from OpenCode's own store (`opencode auth export`), cut to the ONE provider entry. Offline: a stand-in
export command that reads $HOME (the launcher scrubs the environment). Every set below is derived from the code (REFUSALS, the two launchers)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import model_credential as mc  # noqa: E402

FAKE = os.path.join(HERE, "fake_opencode_export.sh")
PROVIDER = "github-copilot"
CANARY = "canary-" + "Qx7Lm2" * 6   # a recognisable fake value, built here


def row(integration, active=True, kind="oauth", ident=None, secret=CANARY):
    return {"id": ident or "id-" + integration, "integrationID": integration, "label": "acme-" + integration, "active": active,
            "value": {"type": kind, "methodID": "m", "access": secret, "refresh": secret + "-r", "expires": 1}}


MINE = row(PROVIDER)
OTHERS = [row("other-one", secret="other-secret-one-value"), row("other-two", kind="api", secret="other-secret-two-value")]
OTHER_BYTES = ("other-secret-one-value", "other-secret-two-value", "id-other-one", "id-other-two", "acme-other-one")
# every refusal, by name: (case, export text or None for "no command", exit code, a word the message must hold)
REFUSALS = {
    "no entry for the provider": (json.dumps(OTHERS), 0, "no entry"),
    "an inactive entry": (json.dumps([row(PROVIDER, active=False)] + OTHERS), 0, "not active"),
    "an entry of another type": (json.dumps([row(PROVIDER, kind="api")] + OTHERS), 0, "not of type oauth"),
    "two entries for the provider": (json.dumps([MINE, row(PROVIDER, ident="id-second")]), 0, "2 entries"),
    "a failing command": (json.dumps([MINE]), 3, "failed"),
    "output that is not JSON": ("not json at all", 0, "not a JSON array"),
    "output over the size limit": (" " * (mc.OUTPUT_LIMIT + 1), 0, "over"),
    "output that is not an array": (json.dumps({PROVIDER: MINE}), 0, "not a JSON array"),
}


class Reader(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.home, "CW_OPENCODE_BIN": FAKE}

    def export(self, text, code=0):
        exit_file = os.path.join(self.home, "export.exit")
        if os.path.exists(exit_file):
            os.remove(exit_file)
        with open(os.path.join(self.home, "export.json"), "w") as f:
            f.write(text)
        if code:
            with open(os.path.join(self.home, "export.exit"), "w") as f:
                f.write(str(code))

    def test_s2_the_provider_entry_is_taken_alone_and_no_other_byte_travels(self):
        self.export(json.dumps([OTHERS[0], MINE, OTHERS[1]]))
        out = mc.read_provider_entry(PROVIDER, self.env)
        got = json.loads(out)
        self.assertEqual(got, [MINE])
        self.assertEqual(len(got), 1)
        for needle in OTHER_BYTES:   # U2
            self.assertNotIn(needle, out)

    def test_s3_each_refusal_is_by_name_and_never_shows_a_value(self):
        for case, (text, code, word) in REFUSALS.items():
            with self.subTest(case):
                self.export(text, code)
                with self.assertRaises(mc.ModelCredentialError) as caught:
                    mc.read_provider_entry(PROVIDER, self.env)
                msg = str(caught.exception)
                self.assertIn(PROVIDER, msg)
                self.assertIn(word, msg)
                self.assertIn("nothing was created", msg)
                self.assertNotIn(CANARY, msg)   # S8
                for needle in OTHER_BYTES:
                    self.assertNotIn(needle, msg)

    def test_s3_output_that_is_not_utf8_is_refused(self):
        with open(os.path.join(self.home, "export.json"), "wb") as f:
            f.write(b"[\xff\xfe]")
        with self.assertRaises(mc.ModelCredentialError) as caught:
            mc.read_provider_entry(PROVIDER, self.env)
        self.assertIn("not a JSON array", str(caught.exception))

    def test_s3_a_missing_command_is_refused_by_name(self):
        for binary in ("/nonexistent/opencode", "opencode-not-installed"):
            with self.subTest(binary):
                with self.assertRaises(mc.ModelCredentialError) as caught:
                    mc.read_provider_entry(PROVIDER, dict(self.env, CW_OPENCODE_BIN=binary))
                self.assertIn("was not found", str(caught.exception))
                self.assertIn(PROVIDER, str(caught.exception))

    def test_s3_a_command_that_never_ends_is_refused(self):
        sleeper = os.path.join(self.home, "opencode")
        with open(sleeper, "w") as f:
            f.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(sleeper, 0o755)
        old, mc.TIMEOUT_SECONDS = mc.TIMEOUT_SECONDS, 0.5
        try:
            with self.assertRaises(mc.ModelCredentialError) as caught:
                mc.read_provider_entry(PROVIDER, dict(self.env, CW_OPENCODE_BIN=sleeper))
        finally:
            mc.TIMEOUT_SECONDS = old
        self.assertIn("timed out", str(caught.exception))

    def test_s4_the_command_is_the_fixed_argv_in_a_scrubbed_environment_and_leaves_no_file(self):
        self.export(json.dumps([MINE]))
        loud = dict(self.env, LANG="C", XDG_DATA_HOME=self.home, CW_CLOUD_TOKEN_FILE="/x", CW_GIT_TOKEN_CMD="y", KUBECONFIG="/k",
                    GH_TOKEN="g", GITHUB_TOKEN="h", CW_MODEL_AUTH_FILE="/legacy", SSH_AUTH_SOCK="/s")
        before = set(os.listdir(self.home))
        mc.read_provider_entry(PROVIDER, loud)
        self.assertEqual(open(os.path.join(self.home, "oc-argv")).read().strip(), "auth export")
        names = {l.split("=", 1)[0] for l in open(os.path.join(self.home, "oc-env")).read().splitlines() if "=" in l}
        names -= {"PWD", "SHLVL", "_", "OLDPWD"}   # what the shell itself adds
        self.assertEqual({n for n in names if not (n in ("PATH", "HOME", "LANG") or n.startswith("XDG_"))}, set())
        for n in ("CW_CLOUD_TOKEN_FILE", "GH_TOKEN", "KUBECONFIG", "CW_GIT_TOKEN_CMD"):
            self.assertNotIn(n, names)
        self.assertEqual(set(os.listdir(self.home)) - before - {"oc-argv", "oc-env"}, set())   # no file of the launcher's holds the export
        for name in os.listdir(self.home):
            if name not in ("export.json",):
                self.assertNotIn(CANARY, open(os.path.join(self.home, name), errors="replace").read(), name)

    def test_s4_the_export_child_inherits_no_stdin(self):
        self.export(json.dumps([MINE]))
        open(os.path.join(self.home, "export.stdin"), "w").close()
        code = "import sys; sys.path.insert(0, %r); import model_credential as mc; mc.read_provider_entry(%r, %r)" % (LAUNCHER, PROVIDER, self.env)
        r = subprocess.run([sys.executable, "-c", code], input=CANARY.encode(), capture_output=True, timeout=60)   # the parent's stdin holds the canary
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(self.home, "oc-stdin")).read(), "")

    def test_the_scrub_function_redacts_the_canary_when_it_is_fed_in(self):
        import cloud_worker as cw
        self.assertNotIn(CANARY, cw.scrub("line " + CANARY, (CANARY,), 500))

    def test_the_binary_setting_defaults_to_opencode_on_path(self):
        self.assertEqual(mc.DEFAULT_BINARY, "opencode")
        self.assertEqual(mc.EXPORT_ARGV, ("auth", "export"))


class Family(unittest.TestCase):
    """The places the legacy reader lived, derived from the tree: none reads the auth file; both launchers use the one reader."""
    def sources(self):
        for rel in ("bin/cloud_worker.py", "cloud/launcher/run_task.py", "cloud/launcher/cloud_worker.py", "cloud/launcher/model_credential.py",
                    "cloud/worker/entrypoint.sh"):
            with open(os.path.join(ROOT, rel)) as f:
                yield rel, f.read()

    def test_s1_the_legacy_auth_file_reader_is_gone(self):
        for rel, text in self.sources():
            with self.subTest(rel):
                self.assertNotIn("CW_MODEL_AUTH_FILE", text)
                self.assertNotIn("opencode/auth.json", text)
                self.assertNotIn('"auth.json"', text)

    def test_both_launchers_use_the_one_reader_and_agree(self):
        texts = dict(self.sources())
        self.assertIn("read_provider_entry(MODEL_PROVIDER", texts["bin/cloud_worker.py"])
        self.assertIn("read_provider_entry(MODEL_PROVIDER", texts["cloud/launcher/run_task.py"])
        sys.path.insert(0, os.path.join(ROOT, "bin"))
        import importlib.machinery, importlib.util
        spec = importlib.util.spec_from_file_location("cw_driver_under_test", os.path.join(ROOT, "bin", "cloud_worker.py"))
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        import run_task
        self.assertEqual(driver.MODEL_PROVIDER, run_task.MODEL_PROVIDER)
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "export.json"), "w") as f:
                json.dump([OTHERS[0], MINE], f)
            env = {"PATH": os.environ["PATH"], "HOME": home, "CW_OPENCODE_BIN": FAKE}
            old = dict(os.environ)
            try:
                os.environ.clear(); os.environ.update(env)
                first = driver.model_entry()
                second = run_task.Driver.model_entry(type("D", (), {"env": env})())
            finally:
                os.environ.clear(); os.environ.update(old)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), [MINE])

    def test_the_settings_table_names_the_binary_and_not_the_legacy_file(self):
        import cloud_worker as cw
        names = [n for n, _, _ in cw.SETTINGS]
        self.assertIn("CW_OPENCODE_BIN", names)
        self.assertNotIn("CW_MODEL_AUTH_FILE", names)

    def test_the_retired_model_auth_file_setting_is_refused_as_unknown(self):
        import cloud_worker as cw
        with self.assertRaises(Exception) as caught:
            cw.parse_settings("CW_MODEL_AUTH_FILE=/home/user/auth.json\n")
        self.assertIn("CW_MODEL_AUTH_FILE", str(caught.exception))
        self.assertNotIn("/home/user/auth.json", str(caught.exception))

    def test_s10_the_docs_say_where_the_credential_is_read_from(self):
        docs = ("docs/cloud-operator-quickstart.md", "cloud/README.md", "cloud/launcher/README.md")
        for rel in docs:
            with self.subTest(rel):
                text = open(os.path.join(ROOT, rel)).read()
                self.assertIn("opencode auth export", text)
                for line in text.splitlines():   # the name may appear only to say it is retired
                    self.assertTrue("CW_MODEL_AUTH_FILE" not in line or "retired" in line, line)
        quick = open(os.path.join(ROOT, docs[0])).read()
        self.assertIn("opencode auth list", quick)
        self.assertIn("/api/provider", quick)


if __name__ == "__main__":
    unittest.main()

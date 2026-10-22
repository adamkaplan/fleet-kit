"""#1088: the pod IMPORTS the model credential into OpenCode's own store before the server starts (S5), refuses to start when the import fails or
the command is missing (S6), and never prints or keeps the credential outside its tmpfs (S8). A stand-in opencode records its calls."""
import os
import unittest

import test_entrypoint as te
from test_entrypoint import MODEL, read

SERVER = 'echo server >> "$FAKE_OC_REC/order"'
REFUSAL = "worker: the model credential import failed; refusing to start"


class Import(te.Entrypoint):
    def order(self):
        path = os.path.join(self.rec, "order")
        return read(path).split() if os.path.exists(path) else []

    def test_s5_the_import_runs_standalone_on_the_secret_content_before_the_server_starts(self):
        r = self.start("sh", "-c", SERVER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(os.path.join(self.rec, "calls")).splitlines(), ["auth import --standalone"])
        self.assertEqual(read(os.path.join(self.rec, "stdin")), MODEL)
        self.assertEqual(self.order(), ["import", "server"])
        self.assertEqual(read(os.path.join(self.run_dir, "model-auth")), MODEL)   # the Secret file is untouched

    def test_s5_no_credential_file_remains_on_tmpfs(self):
        self.assertEqual(self.start().returncode, 0)
        for path, text in self.all_files(skip=(self.run_dir,)):
            self.assertNotIn(MODEL, text, path)
            self.assertNotIn(te.MODEL_VALUES[1], text, path)
        self.assertFalse(os.path.exists(os.path.join(self.tmpfs, "data", "opencode", "auth.json")))

    def test_s5_without_a_model_secret_nothing_is_imported(self):
        os.chmod(os.path.join(self.run_dir, "model-auth"), 0o600)
        os.remove(os.path.join(self.run_dir, "model-auth"))
        self.assertEqual(self.start("sh", "-c", SERVER).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.rec, "calls")))

    def test_s6_a_failed_import_refuses_the_start_with_one_named_line(self):
        r = self.start("sh", "-c", SERVER, FAKE_OC_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), REFUSAL)
        self.assertEqual(self.order(), [])   # the server never started
        self.assertNotIn("server", read(os.path.join(self.rec, "calls")))

    def test_s6_a_missing_opencode_refuses_the_start(self):
        os.remove(os.path.join(self.bin, "opencode"))
        r = self.start("sh", "-c", SERVER, PATH=self.bin + ":/usr/bin:/bin")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), REFUSAL)
        self.assertEqual(self.order(), [])

    def test_s8_the_credential_is_in_no_output_on_success_or_on_failure(self):
        for extra in ({}, {"FAKE_OC_FAIL": "1"}):
            r = self.start("true", **extra)
            self.assert_not_printed(r.stdout + r.stderr, "entrypoint output %r" % extra)


if __name__ == "__main__":
    unittest.main()

"""#1228 (review): the entrypoint, run against a stand-in opencode, never writes the model credential, the git token or the server
password under CW_DISK_DIR (a recursive scan of every file, in its plain, base64 and JSON-escaped forms); a MUTANT with the old layout
is caught; the work tree path is the disk's and an existing clone is kept. Session history is NOT guaranteed clean: tool output can echo
a token into it, and the test documents that limit. Synthetic values only."""
import base64
import json
import os
import tempfile
import unittest

import test_entrypoint as te
from test_entrypoint import ENTRYPOINT, read

CRED = te.MODEL_VALUES[1]   # the planted model credential the harness mounts
ECHOED = "echoed-" + "Lp3Kd9" * 4   # a token-shaped string a tool printed into the session history


def forms(value):
    return [value, base64.b64encode(value.encode()).decode(), json.dumps(value)[1:-1]]


def scan(root, values):
    """Every (path, value) where a file under root holds one of the values in any form."""
    hits = []
    for base, _, files in os.walk(root, followlinks=False):
        for f in files:
            p = os.path.join(base, f)
            if os.path.islink(p):
                continue
            with open(p, "rb") as fh:
                data = fh.read().decode("latin-1")
            for v in values:
                for form in forms(v):
                    if form in data:
                        hits.append((p, v))
    return hits


SESSION = 'cd "$XDG_STATE_HOME" && mkdir -p session && echo "tool output: %s" > session/s1.json && echo work > "$CW_DISK_DIR/work/repo/f"' % ECHOED


class Disk(te.Entrypoint):
    def setUp(self):
        super().setUp()
        disk = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(disk.cleanup)
        self.disk = os.path.realpath(disk.name)
        wk = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(wk.cleanup)
        self.wk = os.path.realpath(wk.name)

    def go(self, script=SESSION, entry=ENTRYPOINT):
        import subprocess
        env = self.env(CW_DISK_DIR=self.disk, CW_WORK_DIR=self.wk, FAKE_OC_STORE="1")
        return subprocess.run(["sh", entry, "sh", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def secrets(self):
        return [CRED, te.MODEL_VALUES[0], te.MODEL_VALUES[2], te.GIT, te.PASSWORD]

    def check(self, entry=ENTRYPOINT):
        r = self.go(entry=entry)
        self.assertEqual(r.returncode, 0, r.stderr)
        return scan(self.disk, self.secrets())

    def test_no_credential_anywhere_under_the_disk(self):
        self.assertEqual(self.check(), [])
        self.assertTrue(scan(self.tmpfs, [CRED]), "the credential IS in the memory-backed store (the stand-in wrote it there)")

    def test_session_history_may_echo_a_token_known_limit(self):
        self.assertEqual(self.check(), [])
        # known limit: tool output can put a token-shaped string into session history on the disk; nothing scrubs it
        self.assertTrue(scan(self.disk, [ECHOED]))
        # what IS guaranteed: the credential store and the git token / model key / password are never under the disk
        self.assertEqual(scan(self.disk, self.secrets()), [])
        self.assertFalse(os.path.exists(os.path.join(self.disk, "session", "opencode")))

    def test_mutant_old_layout_is_caught(self):
        src = read(ENTRYPOINT)
        old = 'data="$TMP/data"   # ALWAYS'
        self.assertIn(old, src)
        mutant = os.path.join(self.d, "entrypoint-old.sh")
        with open(mutant, "w") as f:
            f.write(src.replace(old, 'data="$CW_DISK_DIR/session"; mkdir -p "$data"   # ALWAYS'))
        hits = self.check(mutant)
        self.assertTrue(hits, "the old layout puts the credential on the disk and the scan must see it")

    def test_work_tree_is_the_disks_and_an_existing_clone_is_kept(self):
        self.assertEqual(self.go().returncode, 0)
        link = os.path.join(self.wk, "repo")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.disk, "work", "repo")))
        self.assertEqual(read(os.path.join(link, "f")).strip(), "work")
        self.assertEqual(self.go(script="true").returncode, 0)   # a restart: the clone is not replaced
        self.assertEqual(read(os.path.join(self.disk, "work", "repo", "f")).strip(), "work")

    def test_without_a_disk_nothing_is_linked(self):
        r = self.start("true", CW_WORK_DIR=self.wk)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.wk, "repo")))


if __name__ == "__main__":
    unittest.main()

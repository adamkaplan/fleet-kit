#!/usr/bin/env python3
"""Refs #2440: the proof command reaches the cluster as cw-operator with a short-lived token, and fails at once (exit 2, one line) when kubectl cannot be used.
Offline: fake kubectl and switchboard scripts; the mutant is an in-memory copy of the source run as a child process."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_image_gate as G

MARK = "TOKMARK-9f3a1c77"
PROMPT = "To sign in, use a web browser to open the page https://example.invalid/devicelogin and enter the code ABCD1234"
FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/kubectl.log"
case "$*" in
  *"create token cw-operator --duration=30m"*) [ -f "$FAKE_DIR/refuse" ] && exit 1; printf '%s\\n' TOKMARK-9f3a1c77; exit 0;;
esac
[ -f "$FAKE_DIR/hang" ] && { echo "To sign in, use a web browser to open the page https://example.invalid/devicelogin and enter the code ABCD1234"; sleep 20; exit 0; }
case "$1 $2" in "--token TOKMARK-9f3a1c77") ;; *) echo "no token" >&2; exit 1;; esac
case "$*" in *proof-results*) cat "$FAKE_DIR/results"; exit 0;; esac
cat > /dev/null 2>&1 || true
exit 0
"""
CHILD = ("import sys,types\nm=types.ModuleType('p')\nm.__dict__['__file__']=sys.argv[1]\nexec(compile(sys.argv[2],'p','exec'),m.__dict__)\n"
         "sys.exit(m.main([sys.argv[3],'--repo','acme/widget','--wait','0']))\n")


class ProofOperatorKubectl(G.Base):
    def setUp(self):
        super().setUp()
        Path(self.fake, "kubectl").write_text(FAKE)
        self.env.pop("CW_KUBECTL")
        self.env["PATH"] = self.fake + os.pathsep + os.environ.get("PATH", "")
        self.env["CW_PROOF_KUBECTL_TIMEOUT"] = "2"
        self.env["HOME"] = os.environ.get("HOME", "")
        self.secrets = tempfile.mkdtemp(prefix="proof-secrets-", dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, self.secrets, True)
        os.chmod(self.secrets, 0o700)
        self.env["CW_SECRET_DIR"] = self.secrets

    def minter(self):
        p = os.path.join(self.secrets, "cw-minter-kubeconfig")
        Path(p).write_text("apiVersion: v1\nkind: Config\n")
        os.chmod(p, 0o600)

    def child(self, source=None, bound=25):
        return subprocess.run([sys.executable, "-c", CHILD, str(G.ROOT / "bin" / "cloud_image_proof.py"), source or G.PROOF, G.SRC],
                              env=self.env, capture_output=True, text=True, timeout=bound)

    def everything(self):
        out = ""
        for dirpath, _, names in os.walk(self.tmp):
            for n in names:
                if n not in ("kubectl", "kubectl.log"):
                    out += Path(dirpath, n).read_text(errors="replace")
        return out

    def test_a_sign_in_prompt_ends_at_once_with_one_line_and_the_pod_is_retired(self):
        self.minter()
        Path(self.fake, "hang").write_text("")
        done = self.child()
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("cluster sign-in", done.stderr)
        self.assertEqual(len([l for l in self.log("launcher") if l.startswith("retire ")]), 1)
        self.assertEqual(os.listdir(self.rdir), [])

    def test_a_working_kubectl_with_a_token_reads_the_results_and_never_shows_the_token(self):
        self.minter()
        done = self.child()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(any(l.startswith("--token ") and "proof-results" in l for l in self.log("kubectl")), self.log("kubectl"))
        self.assertNotIn(MARK, done.stdout + done.stderr + self.everything())
        self.assertEqual(len(os.listdir(self.rdir)), 1)

    def test_no_minter_kubeconfig_is_exit_2_with_one_line_and_no_pod(self):
        done = self.child()
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("minter kubeconfig", done.stderr)
        self.assertEqual([l for l in self.log("launcher") if l.startswith("launch ")], [])
        self.assertNotIn(MARK, done.stdout + done.stderr)

    def test_a_refused_token_is_exit_2_with_one_line(self):
        self.minter()
        Path(self.fake, "refuse").write_text("")
        done = self.child()
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("refused", done.stderr)

    def test_a_proof_that_waits_on_the_prompt_is_caught(self):
        self.minter()
        Path(self.fake, "hang").write_text("")
        self.assertEqual(self.child().returncode, 2)   # the control: the real source ends at once
        old = "out, err = proc.communicate(stdin, timeout=limit)"
        self.assertEqual(G.PROOF.count(old), 1)
        waiting = G.PROOF.replace(old, "out, err = proc.communicate(stdin)")
        try:
            done = self.child(waiting, bound=10)
        except subprocess.TimeoutExpired:
            return   # the mutant waited past the bound, as the mutant must
        self.fail("the waiting mutant ended in time with exit %s" % done.returncode)


if __name__ == "__main__":
    unittest.main()

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
tr '\\0' ' ' < /proc/$$/cmdline >> "$FAKE_DIR/argv.log"; echo >> "$FAKE_DIR/argv.log"
tr '\\0' '\\n' < /proc/$$/environ | grep -v '^PATH=' >> "$FAKE_DIR/env.log"
case "$*" in
  *"create token cw-operator --duration=30m"*) [ -f "$FAKE_DIR/refuse" ] && exit 1; printf '%s\\n' TOKMARK-9f3a1c77; exit 0;;
  *"config view"*) echo '{"clusters":[{"name":"c","cluster":{"server":"https://example.invalid:6443","certificate-authority-data":"QQ=="}}]}'; exit 0;;
esac
[ -f "$FAKE_DIR/hang" ] && { echo "To sign in, use a web browser to open the page https://example.invalid/devicelogin and enter the code ABCD1234"; sleep 20; exit 0; }
case "$1" in --token) echo "old form" >> "$FAKE_DIR/oldform.log";; esac
cfg=$(cat "$KUBECONFIG" 2>/dev/null)
case "$cfg" in *TOKMARK-9f3a1c77*) ;; *) case "$1 $2" in "--token TOKMARK-9f3a1c77") ;; *) echo "no token" >&2; exit 1;; esac;; esac
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
        self.assertTrue(any("proof-results" in l for l in self.log("kubectl")), self.log("kubectl"))
        self.assertNotIn(MARK, done.stdout + done.stderr + self.everything())
        self.assertEqual(len(os.listdir(self.rdir)), 1)

    def argv_env_text(self):
        out = ""
        for n in ("argv.log", "env.log", "kubectl.log"):
            p = Path(self.fake, n)
            out += p.read_text(errors="replace") if p.exists() else ""
        return out

    def test_the_token_is_never_in_argv_environment_or_files_of_kubectl(self):
        self.minter()
        done = self.child()
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = Path(self.fake, "argv.log").read_text()
        self.assertIn("proof-results", seen)   # the calls were recorded, so the absence below means something
        self.assertNotIn(MARK, self.argv_env_text() + done.stdout + done.stderr + self.everything())
        self.assertIn("KUBECONFIG=/dev/fd/", Path(self.fake, "env.log").read_text())

    def test_control_the_old_token_on_argv_form_is_seen_by_the_same_check(self):
        self.minter()
        old = 'kubectl, cfg = ([env["CW_KUBECTL"]], None) if env.get("CW_KUBECTL") else (["kubectl"], mint_token(env))'
        self.assertEqual(G.PROOF.count(old), 1)
        argv_form = G.PROOF.replace(old, 'kubectl, cfg = ["kubectl", "--token", json.loads(mint_token(env))["users"][0]["user"]["token"]], None')
        self.child(argv_form)
        self.assertIn(MARK, self.argv_env_text())   # the mutant puts the token back on argv and the check sees it

    def test_a_grandchild_of_kubectl_is_killed_on_the_normal_path(self):
        self.minter()
        Path(self.fake, "results").write_text("")
        src = Path(self.fake, "kubectl").read_text().replace("exit 0\n\"\"\"", "exit 0")
        Path(self.fake, "kubectl").write_text(src.replace("cat > /dev/null 2>&1 || true", "sleep 300 >/dev/null 2>&1 </dev/null & echo $! > \"$FAKE_DIR/gc.pid\"; cat > /dev/null 2>&1 || true"))
        self.child()
        pids = [int(x) for x in Path(self.fake, "gc.pid").read_text().split()] if Path(self.fake, "gc.pid").exists() else []
        self.assertTrue(pids)
        for pid in pids:
            try:
                st = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            self.assertEqual(st, "Z", "grandchild %d lives" % pid)

    def test_no_minter_kubeconfig_is_exit_2_with_one_line_and_the_pod_retired(self):
        done = self.child()
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("minter kubeconfig", done.stderr)
        self.assertEqual(len([l for l in self.log("launcher") if l.startswith("retire ")]), 1)   # the pod it launched is retired
        self.assertNotIn(MARK, done.stdout + done.stderr)

    def test_a_refused_token_is_exit_2_with_one_line(self):
        self.minter()
        Path(self.fake, "refuse").write_text("")
        done = self.child()
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("refused", done.stderr)
        self.assertEqual(len([l for l in self.log("launcher") if l.startswith("retire ")]), 1)

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

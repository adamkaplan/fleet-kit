"""Slice K2 (#563), scenario S4: the image entrypoint installs the per-run model entry and the git credential helper from
mounted files, hands the server password over, and never prints a secret. Offline: planted files in a temp tree, a loopback
stub server, no Docker. The planted values are synthetic."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.join(HERE, "..", "..")
ENTRYPOINT = os.path.join(CLOUD, "worker", "entrypoint.sh")
STUB = os.path.join(HERE, "stub_server.py")
sys.path.insert(0, os.path.join(CLOUD, "launcher"))
import cloud_worker as cw  # noqa: E402  (secret_forms: the encodings a value can be printed in)

# several lengths: short, medium and long; one is JSON-escaped on purpose
MODEL_VALUES = ["ab12CD34e", "modelvalue-Qx7Lm2Qx7Lm2Qx7Lm2", "m" * 7 + "-" + "Zk9Wp3" * 12 + '"q\\z']
MODEL = json.dumps({"type": "oauth", "refresh": MODEL_VALUES[0], "access": MODEL_VALUES[1], "extra": MODEL_VALUES[2]})
GIT = "gitvalue-Rt4Vb8Rt4Vb8Rt4Vb8Rt4Vb8"
PASSWORD = "pwvalue-AbCd1234AbCd1234"
HIDDEN = [MODEL] + MODEL_VALUES + [GIT]   # never in an output; the password is for the server's own environment only


def read(path):
    with open(path, errors="replace") as f:
        return f.read()


class Entrypoint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.d = os.path.realpath(tmp.name)
        self.run_dir, self.tmpfs, self.home, self.bin = [os.path.join(self.d, x) for x in ("run", "tmpfs", "home", "bin")]
        for x in (self.run_dir, self.tmpfs, self.home, self.bin):
            os.mkdir(x)
        self.share = os.path.join(self.d, "share")   # what the image's COPY lays down under /usr/local/share/cw
        os.mkdir(self.share)
        for src, name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
            shutil.copy(os.path.join(CLOUD, "worker", src), os.path.join(self.share, name))
        self.put("model-auth", MODEL)
        self.put("git-token", GIT)
        self.put("server-password", PASSWORD)
        gh = os.path.join(self.bin, "gh")   # a stand-in gh: says how long the token it was given is
        with open(gh, "w") as f:
            f.write('#!/bin/sh\necho "gh token length ${#GH_TOKEN}"\n')
        os.chmod(gh, 0o755)

    def put(self, name, text, mode=0o400):
        path = os.path.join(self.run_dir, name)
        if os.path.exists(path):
            os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)

    def env(self, **extra):
        return dict({"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home, "WORKER_DOCKER": "0", "CW_RUN_DIR": self.run_dir,
                     "CW_TMP_DIR": self.tmpfs, "CW_SHARE_DIR": self.share, "GIT_CONFIG_NOSYSTEM": "1"}, **extra)

    def start(self, *cmd, **extra):
        return subprocess.run(["sh", ENTRYPOINT] + list(cmd or ["true"]), env=self.env(**extra), capture_output=True, text=True, timeout=60)

    def fresh(self, *cmd):
        """A child with a fresh environment, as `kubectl exec` gives the end hook: only HOME and PATH."""
        return subprocess.run(list(cmd), env={"PATH": "/usr/bin:/bin", "HOME": self.home, "GIT_CONFIG_NOSYSTEM": "1"},
                              capture_output=True, text=True, timeout=30)

    def assert_not_printed(self, text, what):
        for v in HIDDEN:
            for form in cw.secret_forms(v):
                self.assertNotIn(form, text, what)

    def all_files(self, skip=()):
        for root, _, names in os.walk(self.d):
            for n in names:
                p = os.path.join(root, n)
                if not p.startswith(tuple(skip)):
                    yield p, read(p)

    # -- S4 --
    def test_s4_the_stub_server_accepts_the_password_and_nothing_is_printed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        client = (
            "import base64, http.client, os, subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, %r, '%d'])\n"
            "try:\n"
            "    for _ in range(100):\n"
            "        try:\n"
            "            c = http.client.HTTPConnection('127.0.0.1', %d, timeout=2)\n"
            "            c.request('GET', '/api/info', headers={'Authorization': 'Basic ' + base64.b64encode(('opencode:' + os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()})\n"
            "            sys.exit(0 if c.getresponse().status == 200 else 3)\n"
            "        except OSError:\n"
            "            time.sleep(0.1)\n"
            "    sys.exit(4)\n"
            "finally:\n"
            "    p.kill()\n" % (STUB, port, port))
        r = self.start(sys.executable, "-c", client)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_not_printed(r.stdout + r.stderr, "entrypoint output")
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        auth = os.path.join(self.tmpfs, "data", "opencode", "auth.json")
        self.assertEqual(stat.S_IMODE(os.stat(auth).st_mode), 0o600)
        self.assertEqual(read(auth), MODEL)

    def test_s4_the_model_entry_is_copied_to_tmpfs_the_data_dir_is_exported_and_the_mounted_file_is_untouched(self):
        r = self.start("sh", "-c", 'echo "$XDG_DATA_HOME|$XDG_CONFIG_HOME"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "%s|%s" % (os.path.join(self.tmpfs, "data"), os.path.join(self.tmpfs, "config")))
        auth = os.path.join(self.tmpfs, "data", "opencode", "auth.json")
        with open(auth, "a") as f:   # the server refreshes its token in the copy
            f.write(" ")
        self.assertEqual(read(os.path.join(self.run_dir, "model-auth")), MODEL)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmpfs, "data", "opencode")).st_mode), 0o700)

    def test_s4_the_model_config_names_the_default_model_and_cw_model_overrides_it(self):
        conf = os.path.join(self.tmpfs, "config", "opencode", "opencode.json")
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(json.loads(read(conf))["model"], "github-copilot/claude-sonnet-5.5")
        self.assertEqual(stat.S_IMODE(os.stat(conf).st_mode), 0o600)
        self.assertEqual(self.start(CW_MODEL="acme/other-model").returncode, 0)
        self.assertEqual(json.loads(read(conf))["model"], "acme/other-model")   # (the file also carries the worker agent: see test_guard.py)

    def test_s4_no_secret_is_in_the_environment_of_the_command_or_in_any_file_but_its_own(self):
        r = self.start("env")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_not_printed(r.stdout + r.stderr, "the command's environment")
        for path, text in self.all_files(skip=(self.run_dir, os.path.join(self.tmpfs, "data"))):
            self.assert_not_printed(text, path)   # the git token and the model entry are in no other file

    def test_s4_the_git_helper_is_found_by_a_fresh_environment_and_reads_the_token_at_use_time(self):
        self.assertEqual(self.start().returncode, 0)
        helper = self.fresh("git", "config", "--global", "credential.helper").stdout.strip()   # what the end hook's git sees
        self.assertEqual(helper, os.path.join(self.tmpfs, "bin", "git-credential-cw"))
        self.assertNotIn(GIT, read(os.path.join(self.home, ".gitconfig")))
        ask = lambda extra, verb="get": subprocess.run([helper, verb], input="protocol=https\nhost=%s\n\n" % extra,
                                                      env={"PATH": "/usr/bin:/bin", "CW_RUN_DIR": self.run_dir}, capture_output=True, text=True)
        got = ask("github.com")
        self.assertEqual(got.stdout, "username=x-access-token\npassword=%s\n" % GIT)
        self.assertEqual(ask("other.example").stdout, "")   # the token is offered to github.com only
        self.assertEqual(ask("github.com", "store").stdout, "")
        self.assertEqual(subprocess.run([helper, "get"], input="protocol=http\nhost=github.com\n\n", capture_output=True, text=True,
                                        env={"PATH": "/usr/bin:/bin", "CW_RUN_DIR": self.run_dir}).stdout, "")   # never over plain http
        os.chmod(os.path.join(self.run_dir, "git-token"), 0o600)
        with open(os.path.join(self.run_dir, "git-token"), "w") as f:   # a rotated file is read at the next call
            f.write("rotated-token-value")
        self.assertIn("password=rotated-token-value", ask("github.com").stdout)

    def test_s4_the_git_helper_reaches_git_itself(self):
        self.assertEqual(self.start().returncode, 0)
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": self.home, "GIT_CONFIG_NOSYSTEM": "1", "CW_RUN_DIR": self.run_dir, "GIT_TERMINAL_PROMPT": "0"})
        self.assertIn("password=%s" % GIT, r.stdout)

    def test_s4_the_gh_wrapper_gives_gh_the_token_for_one_call_only(self):
        r = self.start("sh", "-c", "gh; echo \"left in the environment: ${GH_TOKEN:-nothing}\"")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gh token length %d" % len(GIT), r.stdout)
        self.assertIn("left in the environment: nothing", r.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmpfs, "bin")).st_mode), 0o700)

    def test_s4_a_present_file_that_is_unreadable_or_empty_refuses_and_never_falls_back(self):
        for name in ("model-auth", "git-token", "server-password"):
            for how in ("empty", "unreadable"):
                with self.subTest(name=name, how=how):
                    self.setUp()
                    self.put(name, "" if how == "empty" else "x" * 20, 0o400 if how == "empty" else 0o000)
                    if how == "unreadable" and os.access(os.path.join(self.run_dir, name), os.R_OK):
                        self.skipTest("running as a user that reads anything")
                    r = self.start("echo", "started")
                    self.assertEqual(r.returncode, 1)
                    self.assertNotIn("started", r.stdout)
                    self.assertIn(name, r.stderr)
                    self.assert_not_printed(r.stdout + r.stderr, "a refusal")

    def test_s4_absent_files_are_skipped_and_nothing_is_installed(self):
        for name in os.listdir(self.run_dir):
            os.chmod(os.path.join(self.run_dir, name), 0o600)
            os.remove(os.path.join(self.run_dir, name))
        r = self.start("echo", "started")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "started\n", ""))
        self.assertFalse(os.path.exists(os.path.join(self.tmpfs, "data")))
        self.assertFalse(os.path.exists(os.path.join(self.tmpfs, "bin")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".gitconfig")))


if __name__ == "__main__":
    unittest.main()

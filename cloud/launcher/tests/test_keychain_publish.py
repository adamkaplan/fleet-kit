"""Ask #2357 (S10): the publish script with CW_KEYCHAIN_SERVICE set logs in to the registry with the keychain's token through a fake `crane` and
runs no `az` (a fake az that fails and logs). The fake `security` is reached by a sitecustomize that points keychain_read.PROGRAM at it.
Offline; the archive is built by cloud/tests/make-archives.py; every value is built here."""
import base64, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
PUB = os.path.join(ROOT, "cloud", "worker", "publish-image.sh")
D1 = "sha256:" + "1" * 64
USER, PASSWORD = "cw-pusher", "Pw" + "x7Kq" * 6

CRANE = """#!/usr/bin/env bash
echo "crane $*" >> "$STUB_LOG"
case $1 in
  auth) cat > "$STUB_STATE/stdin"; echo "$DOCKER_CONFIG" > "$STUB_STATE/dockerconfig" ;;
  manifest) echo '{"mediaType":"application/vnd.oci.image.manifest.v1+json","layers":[]}' ;;
  digest) echo "$STUB_D1" ;;
  pull) for a; do dir=$a; done; mkdir -p "$dir"; tar -xf "$STUB_ARCHIVE" -C "$dir" ;;
  copy) exit 0 ;;
esac
"""
AZ = "#!/bin/sh\necho \"az $*\" >> \"$STUB_LOG\"\nexit 1\n"
SECURITY = "#!/bin/sh\necho \"security $*\" >> \"%(log)s\"\ncase \"$*\" in *' -a acr-token '*) cat \"%(item)s\" ;; *) exit 44 ;; esac\n"
SITE = "import sys\nsys.path.insert(0, %r)\nimport keychain_read\nkeychain_read.PROGRAM = %r\n"


class Publish(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.d = tmp.name
        self.bin = os.path.join(d, "bin")
        self.state = os.path.join(d, "state")
        os.makedirs(self.bin)
        os.makedirs(self.state)
        self.log = os.path.join(d, "calls")
        open(self.log, "w").close()
        for name, text in (("crane", CRANE), ("az", AZ)):
            self.script(os.path.join(self.bin, name), text)
        item = os.path.join(d, "item")
        with open(item, "w") as f:
            f.write(base64.b64encode((USER + ":" + PASSWORD).encode()).decode() + "\n")
        self.security = self.script(os.path.join(d, "security"), SECURITY % {"log": self.log, "item": item})
        site = os.path.join(d, "site")
        os.makedirs(site)
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write(SITE % (LAUNCHER, self.security))
        self.site = site
        subprocess.run([sys.executable, os.path.join(ROOT, "cloud", "tests", "make-archives.py"), d, "ghp_" + "f" * 36], check=True, capture_output=True)

    def script(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def run_publish(self, **extra):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "STUB_LOG": self.log, "STUB_STATE": self.state, "STUB_D1": D1,
               "STUB_ARCHIVE": os.path.join(self.d, "oci-clean.tar"), "PYTHONPATH": self.site}
        env.update(extra)
        return subprocess.run([PUB, "--tool", "crane", "--tool-dir", self.bin, "--staging", "acme-registry.example/cw/worker-candidate:abc",
                               "--release", "acme-registry.example/cw/worker:v1"], env=env, capture_output=True, text=True)

    def calls(self):
        with open(self.log) as f:
            return f.read()

    def test_s10_with_the_service_set_crane_logs_in_with_the_token_and_az_is_never_called(self):
        done = self.run_publish(CW_KEYCHAIN_SERVICE="cw-launcher")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertNotIn("az ", calls.replace("crane ", ""), "az was called with a keychain service set")
        self.assertEqual([l for l in calls.splitlines() if l.startswith("az")], [])
        self.assertIn("crane auth login acme-registry.example -u " + USER + " --password-stdin", calls)
        with open(os.path.join(self.state, "stdin")) as f:
            self.assertEqual(f.read(), PASSWORD)   # the password came on standard input
        self.assertIn("-s cw-launcher -a acr-token -w", calls)
        self.assertNotIn(PASSWORD, calls + done.stdout + done.stderr)   # in no argument and no output
        self.assertNotIn(base64.b64encode((USER + ":" + PASSWORD).encode()).decode(), calls + done.stdout + done.stderr)
        self.assertIn("crane copy acme-registry.example/cw/worker-candidate@" + D1 + " acme-registry.example/cw/worker:v1", calls)
        with open(os.path.join(self.state, "dockerconfig")) as f:
            self.assertFalse(os.path.exists(f.read().strip()))   # the private login directory is gone

    def test_unset_the_script_does_no_login(self):
        done = self.run_publish()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("auth login", self.calls())
        self.assertNotIn("security", self.calls())

    def test_a_missing_token_item_publishes_nothing(self):
        os.remove(os.path.join(self.d, "item"))
        done = self.run_publish(CW_KEYCHAIN_SERVICE="cw-launcher")
        self.assertEqual(done.returncode, 3)
        self.assertNotIn("copy", self.calls())
        self.assertIn("nothing published", done.stderr)

    def test_the_scope_map_in_the_runbook_names_two_repositories(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            text = f.read()
        line = [l for l in text.splitlines() if "az acr scope-map create" in l]
        self.assertEqual(len(line), 1)
        self.assertEqual(line[0].count("--repository"), 2)
        self.assertIn("cw/worker content/read content/write", line[0])
        self.assertIn("cw/worker-candidate content/read content/write", line[0])


if __name__ == "__main__":
    unittest.main()

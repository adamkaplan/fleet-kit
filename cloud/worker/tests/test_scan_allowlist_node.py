"""Scan allowlist lines for the official Node binaries (scenarios U4, U5), with mutants. Offline; writes only under the temp dir."""
import os, re, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCAN = os.path.join(ROOT, "cloud/worker/scan-secrets.sh")
LIST = os.path.join(ROOT, "cloud/worker/scan-allowlist.txt")
NODE = "/opt/node/bin/node"


def node_lines():
    with open(LIST) as f:
        return [l.rstrip("\n").split(" ", 3) for l in f if l.startswith(NODE + " ")]


def key_window():
    return ("AK" + "IA" + "ABCDEFGHIJKLMNOP").encode()


class NodeAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "tree")
        os.makedirs(os.path.join(self.d, "opt/node/bin"))
        self.f = os.path.join(self.d, "opt/node/bin/node")

    def write(self, data):
        with open(self.f, "wb") as fh:
            fh.write(data)

    def scan(self, allow=None):
        env = dict(os.environ, SCAN_ALLOWLIST=allow or LIST)
        return subprocess.run(["bash", SCAN, "--dir", self.d], env=env, capture_output=True, text=True).returncode

    def list_with(self, line):
        p = os.path.join(self.tmp.name, "list")
        with open(p, "w") as fh:
            fh.write(line + "\n")
        return p

    def test_u4_two_lines_for_the_node_binary(self):
        ls = node_lines()
        self.assertEqual(len(ls), 2)
        self.assertEqual(len({l[1] for l in ls}), 2)
        for l in ls:
            self.assertRegex(l[1], r"^[0-9a-f]{64}$")
            self.assertEqual(l[2], "aws-access-key")
            self.assertIn("chance", l[3])
            self.assertIn("base64", l[3])

    def test_u5_other_sha_still_hits(self):
        self.write(b"x\n" + key_window() + b"\n")
        self.assertEqual(self.scan(), 1)

    def test_positive_control_no_hit(self):
        self.write(b"nothing here\n")
        self.assertEqual(self.scan(), 0)

    def test_control_matching_sha_is_suppressed(self):
        import hashlib
        self.write(b"x\n" + key_window() + b"\n")
        sha = hashlib.sha256(open(self.f, "rb").read()).hexdigest()
        self.assertEqual(self.scan(self.list_with("%s %s aws-access-key test" % (NODE, sha))), 0)

    def test_mutant_another_sha_does_not_suppress(self):
        self.write(b"x\n" + key_window() + b"\n")
        line = "%s %s aws-access-key test" % (NODE, "0" * 64)
        self.assertNotEqual(line, "")
        self.assertEqual(self.scan(self.list_with(line)), 1)

    def test_mutant_another_pattern_does_not_suppress(self):
        import hashlib
        self.write(b"x\n" + key_window() + b"\n")
        sha = hashlib.sha256(open(self.f, "rb").read()).hexdigest()
        line = "%s %s github-token test" % (NODE, sha)
        self.assertIn("github-token", line)
        self.assertEqual(self.scan(self.list_with(line)), 1)


if __name__ == "__main__":
    unittest.main()

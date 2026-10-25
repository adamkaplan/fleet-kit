#!/usr/bin/env python3
"""Tests of cloud/worker/scan-embedded-roots.py with a SYNTHETIC binary and a SYNTHETIC certificate.

The scanner needs only the SEQUENCE framing (0x30 0x82 <len> 0x30 ...), so the certificate here is a
hand-made 400-byte blob with that framing, not a real certificate. No real fingerprint is used."""
import base64
import hashlib
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCANNER = os.path.join(ROOT, "cloud", "worker", "scan-embedded-roots.py")
rnd = random.Random(1276)
CERT = b"\x30\x82" + (396).to_bytes(2, "big") + b"\x30" + bytes(rnd.randrange(256) for _ in range(395))
assert len(CERT) == 400
FP = hashlib.sha256(CERT).hexdigest()
NOISE = bytes(rnd.randrange(1, 256) for _ in range(5000))


def run(args, lst=None, scanner=SCANNER, env_unset=False):
    env = dict(os.environ)
    env.pop("CW_ROOT_LIST", None)
    if lst is not None and not env_unset:
        env["CW_ROOT_LIST"] = lst
    p = subprocess.run([sys.executable, scanner] + args, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout


class Scanner(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.list = self.w("list", ("%s synthetic-test-root\n" % FP).encode())
        self.der = self.w("der.bin", NOISE + CERT + NOISE)
        self.clean = self.w("clean.bin", NOISE)
        pem = b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(CERT) + b"-----END CERTIFICATE-----\n"
        self.pem = self.w("pem.bin", NOISE + pem + NOISE)
        self.pem_esc = self.w("pem_esc.bin", NOISE + pem.replace(b"\n", b"\\n") + NOISE)

    def w(self, name, data):
        p = os.path.join(self.d.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_der_found(self):
        rc, out = run([self.der], self.list)
        self.assertEqual(rc, 1)
        self.assertIn("FOUND synthetic-test-root " + FP[:16], out)
        self.assertIn("certificates examined", out)

    def test_clean(self):
        rc, out = run([self.clean], self.list)
        self.assertEqual(rc, 0)
        self.assertIn("absent synthetic-test-root", out)
        self.assertNotIn("FOUND", out)

    def test_pem_found(self):
        self.assertEqual(run([self.pem], self.list)[0], 1)

    def test_pem_literal_backslash_n_found(self):
        self.assertEqual(run([self.pem_esc], self.list)[0], 1)

    def refused(self, rc, out):
        self.assertEqual(rc, 2)
        self.assertNotIn("absent", out)

    def test_unset(self):
        self.refused(*run([self.der], None))

    def test_missing_file(self):
        self.refused(*run([self.der], os.path.join(self.d.name, "nope")))

    def test_empty_file(self):
        self.refused(*run([self.clean], self.w("empty", b"")))

    def test_malformed_line(self):
        self.refused(*run([self.clean], self.w("bad", b"nothex label\n")))

    def test_unreadable_binary(self):
        self.refused(*run([os.path.join(self.d.name, "nobin")], self.list))

    def mutated(self, old, new):
        with open(SCANNER, encoding="utf-8") as f:
            s = f.read()
        self.assertIn(old, s)
        m = s.replace(old, new, 1)
        self.assertNotEqual(m, s, "mutation did not apply")
        return self.w("mutant.py", m.encode())

    def test_mutant_skipped_scan_fails(self):
        sc = self.mutated("    return fps\n", "    return set()\n")
        rc, _ = run([self.der], self.list, scanner=sc)
        self.assertNotEqual(rc, 1)  # the real scanner gives 1: the mutant reports clean

    def test_mutant_empty_list_accepted_fails(self):
        sc = self.mutated('raise ValueError("the list is empty")', "pass")
        rc, _ = run([self.clean], self.w("empty2", b""), scanner=sc)
        self.assertNotEqual(rc, 2)

    def test_no_foreign_fingerprint_in_new_files(self):
        import re
        for p in (SCANNER, os.path.join(HERE, "test_root_scanner.py"), os.path.join(HERE, "test_dockerfile_agent.py")):
            with open(p, encoding="utf-8") as f:
                for h in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", f.read()):
                    self.fail("a 64-hex string in %s" % p)


if __name__ == "__main__":
    unittest.main(verbosity=1)

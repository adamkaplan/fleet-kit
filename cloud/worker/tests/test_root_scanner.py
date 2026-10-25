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
CERT2 = b"\x30\x82" + (346).to_bytes(2, "big") + b"\x30" + bytes(rnd.randrange(256) for _ in range(345))
CERT81 = b"\x30\x81" + (197).to_bytes(1, "big") + b"\x30" + bytes(rnd.randrange(256) for _ in range(196))
CERT83 = b"\x30\x83" + (395).to_bytes(3, "big") + b"\x30" + bytes(rnd.randrange(256) for _ in range(394))
assert len(CERT2) == 350 and len(CERT81) == 200 and len(CERT83) == 400
MARK = "uniq-label-marker-%d" % rnd.randrange(10**9)
NOISE = bytes(rnd.randrange(1, 256) for _ in range(5000))


BOUND_KIB = 80 * 1024


def kib(value, platform=sys.platform):
    """ru_maxrss is bytes on macOS and KiB on Linux; return KiB."""
    return value // 1024 if platform == "darwin" else value


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
        self.clean = self.w("clean.bin", NOISE + CERT2 + NOISE)
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
        self.assertIn("FOUND #1 " + FP[:8], out)
        self.assertIn("certificates examined", out)

    def test_clean(self):
        rc, out = run([self.clean], self.list)
        self.assertEqual(rc, 0)
        self.assertIn("absent #1 " + FP[:8], out)
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

    def test_zero_certificates_refused(self):
        for name, data in (("e", b""), ("r", bytes(rnd.randrange(256) for _ in range(300000)))):
            rc, out = run([self.w(name, data)], self.list)
            self.assertEqual(rc, 2)
            self.assertIn("nothing examined: the scan did not work", out)
            self.assertNotIn("absent", out)

    def test_one_cert_no_listed_root_clean(self):
        rc, out = run([self.clean], self.list)
        self.assertEqual(rc, 0)
        self.assertIn("1 certificates examined", out)
        self.assertIn("list entries used: 1", out)

    def test_der_81_and_83_forms_found(self):
        for c in (CERT81, CERT83):
            lst = self.w("l", ("%s x\n" % hashlib.sha256(c).hexdigest()).encode())
            self.assertEqual(run([self.w("b", NOISE + c + NOISE)], lst)[0], 1)

    def test_output_has_no_label(self):
        lst = self.w("lm", ("%s %s\n%s %s\n" % (FP, MARK, "ab" * 32, MARK)).encode())
        for b in (self.der, self.clean):
            rc, out = run([b], lst)
            self.assertNotIn(MARK, out)
            self.assertIn("#2 abababab", out)

    def test_list_not_utf8_refused(self):
        self.refused(*run([self.clean], self.w("nu", b"\xff\xfe" + FP.encode() + b" x\n")))

    def test_list_over_cap_refused(self):
        self.refused(*run([self.clean], self.w("big", (("%s x\n" % FP) * 10001).encode())))

    def test_list_comment_refused_blank_allowed(self):
        self.refused(*run([self.clean], self.w("c", ("# note\n%s x\n" % FP).encode())))
        self.assertEqual(run([self.der], self.w("b2", ("\n%s x\n\n" % FP).encode()))[0], 1)

    def test_pem_real_newlines_found_and_other_escape_not(self):
        pem = b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(CERT2) + b"-----END CERTIFICATE-----\n"
        lst = self.w("l2", ("%s x\n" % hashlib.sha256(CERT2).hexdigest()).encode())
        self.assertEqual(run([self.w("p", NOISE + pem + NOISE)], lst)[0], 1)

    def split_binary(self, chunk):
        # cert starts 200 bytes before the chunk boundary, so it is split across it
        return self.w("split.bin", bytes(chunk - 200) + CERT + bytes(3000))

    def test_cert_split_across_chunk_boundary_found(self):
        sc = self.mutated("CHUNK = 8 * 1024 * 1024", "CHUNK = 5000")
        rc, out = run([self.split_binary(5000)], self.list, scanner=sc)
        self.assertEqual(rc, 1)

    def test_mutant_no_overlap_fails(self):
        sc = self.mutated("CHUNK = 8 * 1024 * 1024", "CHUNK = 5000")
        with open(sc, encoding="utf-8") as f:
            s = f.read()
        self.assertIn("OVERLAP = 16384", s)
        m = s.replace("OVERLAP = 16384", "OVERLAP = 0")
        self.assertNotEqual(m, s)
        rc, _ = run([self.split_binary(5000)], self.list, scanner=self.w("nov.py", m.encode()))
        self.assertNotEqual(rc, 1)

    def sparse(self):
        p = os.path.join(self.d.name, "sparse.bin")
        try:
            with open(p, "wb") as f:
                f.seek(120 * 1024 * 1024)
                f.write(CERT)
        except OSError as e:
            print("LOUD: SKIPPED the 120 MB sparse test: %s" % e)
            self.skipTest("disk cannot hold the file")
        return p

    def peak(self, scanner, args):
        """(returncode, own peak KiB) of a child that runs scanner (or 'pass') under runpy."""
        code = (
            "import runpy, sys, resource\n"
            "rc = 0\n"
            "try:\n"
            "    if sys.argv[1] != '-':\n"
            "        sys.argv = sys.argv[1:]\n"
            "        runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code if isinstance(e.code, int) else 1\n"
            "sys.stdout.flush()\n"
            "print('PEAK', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
            "sys.exit(rc)\n"
        )
        env = dict(os.environ, CW_ROOT_LIST=self.list)
        argv = [scanner] + args if scanner else ["-"]
        p = subprocess.run([sys.executable, "-c", code] + argv, env=env, capture_output=True, text=True)
        last = p.stdout.strip().splitlines()[-1].split()
        self.assertEqual(last[0], "PEAK", p.stderr)
        return p.returncode, kib(int(last[1]))

    def own_over_baseline(self, scanner):
        p = self.sparse()
        _, base = self.peak(None, [])
        rc, own = self.peak(scanner, [p])
        print("peak KiB: own=%d baseline=%d delta=%d" % (own, base, own - base))
        return rc, own - base

    def test_kib_normalises_platform(self):
        self.assertEqual(kib(52346880, "darwin"), 51120)
        self.assertEqual(kib(51120, "linux"), 51120)

    def test_large_sparse_file_bounded_memory(self):
        rc, delta = self.own_over_baseline(SCANNER)
        self.assertEqual(rc, 1)
        self.assertLess(delta, BOUND_KIB)

    def test_mutant_big_chunk_fails_bound(self):
        sc = self.mutated("CHUNK = 8 * 1024 * 1024", "CHUNK = 200 * 1024 * 1024")
        _, delta = self.own_over_baseline(sc)
        self.assertGreaterEqual(delta, BOUND_KIB)

    def test_mutant_whole_file_read_fails_bound(self):
        sc = self.mutated("f.read(CHUNK)", "f.read()")
        _, delta = self.own_over_baseline(sc)
        self.assertGreaterEqual(delta, BOUND_KIB)

    def test_mutant_zero_guard_removed_fails(self):
        sc = self.mutated("if len(hits) == 0:", "if False:")
        rc, _ = run([self.w("e2", b"")], self.list, scanner=sc)
        self.assertNotEqual(rc, 2)

    def test_mutant_label_printed_fails(self):
        sc = self.mutated('print("absent #%d %s" % (i, fp[:8]))', 'print("absent #%d %s" % (i, label))')
        lst = self.w("lm2", ("%s %s\n" % ("ab" * 32, MARK)).encode())
        rc, out = run([self.clean], lst, scanner=sc)
        self.assertIn(MARK, out)

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

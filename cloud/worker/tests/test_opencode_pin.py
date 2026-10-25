#!/usr/bin/env python3
"""The worker image's agent binary pin: a security floor (Bun 1.4.4 or later, agent release 2.1.3 or later).

Parses cloud/worker/Dockerfile. Mutants each assert the mutation applied, then expect the check to fail."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DOCKERFILE = os.path.join(ROOT, "cloud", "worker", "Dockerfile")
FLOOR = (2, 1, 3)


def read():
    with open(DOCKERFILE, encoding="utf-8") as f:
        return f.read()


def check(text):
    """Return a list of problems with the pin in this Dockerfile text."""
    bad = []
    vers = re.findall(r"^ARG OPENCODE_VERSION=(\S*)\s*$", text, re.M)
    shas = re.findall(r"^ARG OPENCODE_SHA256=(\S*)\s*$", text, re.M)
    if len(vers) != 1 or text.count("ARG OPENCODE_VERSION") != 1:
        bad.append("need exactly one OPENCODE_VERSION ARG")
    if len(shas) != 1 or text.count("ARG OPENCODE_SHA256") != 1:
        bad.append("need exactly one OPENCODE_SHA256 ARG")
    if shas and not re.fullmatch(r"[0-9a-f]{64}", shas[0]):
        bad.append("OPENCODE_SHA256 is not 64 lowercase hex")
    if vers:
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", vers[0])
        if not m:
            bad.append("version does not parse")
        elif tuple(int(x) for x in m.groups()) < FLOOR:
            bad.append("version below the security floor")
    # the RUN chain that downloads the binary
    runs = re.findall(r"^RUN (?:[^\n]*\\\n)*[^\n]*$", text, re.M)
    chain = [r for r in runs if "OPENCODE_VERSION" in r]
    if len(chain) != 1:
        bad.append("need exactly one RUN chain that fetches the binary")
    else:
        c = chain[0]
        parts = [p.strip() for p in re.split(r"\\\n", c)]
        flat = " ".join(parts)
        i = flat.find("sha256sum -c")
        j = flat.find("tar ")
        if i < 0:
            bad.append("no sha256sum -c step")
        elif j < 0 or i > j:
            bad.append("sha256sum -c does not precede the unpack")
        else:
            seg = flat[i:j]
            if "||" in seg or "|| true" in flat[i:]:
                bad.append("the sha check is masked")
            if not re.search(r'&&\s*$', seg.strip() + " &&") or "&&" not in seg:
                bad.append("the sha check is not joined to the unpack by &&")
        hosts = set(re.findall(r"https?://([^/\s\"']+)", c))
        if hosts != {"opencode.ai"}:
            bad.append("download host other than opencode.ai: %s" % sorted(hosts))
    return bad


class Pin(unittest.TestCase):
    def test_real_dockerfile(self):
        self.assertEqual(check(read()), [])

    def mutant(self, old, new):
        t = read()
        self.assertIn(old, t)
        m = t.replace(old, new, 1)
        self.assertNotEqual(m, t, "mutation did not apply")
        return check(m)

    def test_old_version_fails_floor(self):
        t = read()
        m = re.sub(r"^(ARG OPENCODE_VERSION=).*$", r"\g<1>2.0.28", t, flags=re.M)
        self.assertNotEqual(m, t, "mutation did not apply")
        self.assertIn("version below the security floor", check(m))

    def test_dropped_sha_check_fails(self):
        probs = self.mutant(' && echo "${OPENCODE_SHA256}  /tmp/oc.tgz" | sha256sum -c - \\\n', "\\\n")
        self.assertTrue(probs)

    def test_masked_sha_check_fails(self):
        probs = self.mutant("sha256sum -c - \\\n", "sha256sum -c - || true \\\n")
        self.assertIn("the sha check is masked", probs)

    def test_other_host_fails(self):
        probs = self.mutant("https://opencode.ai/files", "https://example.com/files")
        self.assertTrue(any("download host" in p for p in probs))


if __name__ == "__main__":
    unittest.main(verbosity=1)

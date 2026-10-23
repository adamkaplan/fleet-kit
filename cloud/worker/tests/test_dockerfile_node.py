"""#1040: the Dockerfile carries one pinned Node 22, verified before unpacking, from no unpinned channel; the docs list it.
Offline: reads files only. Scenarios S2, S3, S5, S7, U2, U3 (S1 is the pull request body; S9 is the unedited tests)."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
DF = os.path.join(CLOUD, "worker", "Dockerfile")


def read(p):
    with open(p) as f:
        return f.read()


def problems(t):
    out = []
    defs = re.findall(r"^ARG NODE_VERSION=(\S+)", t, re.M)
    if len(defs) != 1 or not defs[0].startswith("22."):
        out.append("NODE_VERSION must be defined once, major 22")
    elif t.count(defs[0]) != 1:
        out.append("version spelled more than once")
    # the opencode step also uses sha256sum -c: look at the node block only
    b = t.find("Node runtime")
    i = t.find("sha256sum -c", b) if b >= 0 else -1
    j = t.find("tar -xJf", b) if b >= 0 else -1
    if i < 0 or j < 0 or i > j:
        out.append("checksum check must precede the node tar")
    if t.count("/usr/local/bin/node") != 2 and len(re.findall(r"ln -s /opt/node/bin/node /usr/local/bin/node", t)) != 1:
        out.append("node not linked")
    for blk in t.split("apt-get install")[1:]:
        blk = blk.split(" " + chr(38) * 2 + " ")[0]
        if re.search(r"\b(nodejs|npm)\b", blk):
            out.append("apt nodejs")
    if re.search(r"curl[^\n]*\|\s*(ba)?sh|nodesource|npm install -g|npm i -g", t):
        out.append("unpinned channel")
    if re.search(r"apt-get install[^\n]*\bgh\b|cli\.github\.com|gh_[0-9]", t):
        out.append("real gh installed")
    for s in ("FROM worker AS scan", "COPY --from=scan"):
        if len(re.findall("(?m)^" + re.escape(s), t)) != 1:
            out.append("missing " + s)
    return out


class T(unittest.TestCase):
    def test_real_file_clean(self):
        self.assertEqual(problems(read(DF)), [])

    def test_no_new_secret_names(self):
        t = read(DF)
        self.assertEqual(len(re.findall(r"^(ENV|ARG) \w*(TOKEN|SECRET|PASSWORD|KEY)\w*", t, re.M)), 0)

    def test_mutant_checksum_dropped(self):
        t = read(DF).replace("sha256sum -c -) \\", "cat) \\")
        t = t.replace(" && echo \"${OPENCODE_SHA256}  /tmp/oc.tgz\" | sha256sum -c - \\\n", "")
        self.assertTrue(problems(t))

    def test_mutant_apt_nodejs(self):
        t = read(DF).replace("jq procps", "jq procps nodejs npm")
        self.assertTrue(problems(t))

    def test_mutant_curl_sh(self):
        t = read(DF) + "\nRUN curl -fsSL https://example.invalid/setup | bash -\n"
        self.assertTrue(problems(t))

    def test_mutant_two_versions(self):
        t = read(DF).replace("/dist/v${NODE_VERSION}/${f}", "/dist/v22.23.2/${f}")
        self.assertTrue(problems(t))

    def test_mutant_major_20(self):
        self.assertTrue(problems(read(DF).replace("NODE_VERSION=22.23.2", "NODE_VERSION=20.1.0")))

    def test_docs(self):
        d = read(os.path.join(CLOUD, "README.md"))
        self.assertEqual(d.count("Node 22") >= 1 and d.count("NODE_VERSION") >= 1, True)


if __name__ == "__main__":
    unittest.main()

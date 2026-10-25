"""#1040: the Dockerfile carries one pinned Node 22, verified before unpacking, from no unpinned channel; the docs list it.
Offline: reads files only. Scenarios S2, S3, S5, S7, U2, U3 (S1 is the pull request body; S9 is the unedited tests)."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
DF = os.path.join(CLOUD, "worker", "Dockerfile")
FLOOR = (22, 23, 4)  # security floor: Node >= 22.23.4


def read(p):
    with open(p) as f:
        return f.read()


def problems(t):
    out = []
    defs = re.findall(r"^ARG NODE_VERSION=(\S+)$", t, re.M)
    if len(defs) != 1 or not re.fullmatch(r"22\.\d+\.\d+", defs[0]):
        out.append("NODE_VERSION must be defined once, a full 22.x.y")
    elif tuple(int(x) for x in defs[0].split(".")) < FLOOR:
        out.append("NODE_VERSION below the security floor")
    elif t.count(defs[0]) != 1:
        out.append("version spelled more than once")
    for n in ("NODE_SHA256_X64", "NODE_SHA256_ARM64"):
        if len(re.findall(r"^ARG " + n + r"=[0-9a-f]{64}$", t, re.M)) != 1:
            out.append(n + " must be pinned once as 64 hex")
        if t.count('"${' + n + '}"') != 1:
            out.append(n + " not used for its architecture")
    if re.search(r"SHASUMS|NODE_SHASUMS_URL", t):
        out.append("checksum list or overridable checksum address")
    # the opencode step also uses sha256sum -c: look at the node block only
    b = t.find("# ---- Node runtime")
    e = t.find("# ---- end Node runtime", b)
    blk = t[b:e] if b >= 0 and e > b else ""
    lines = [x.strip() for x in blk.replace(" \\\n", "\n").split("\n")] if blk else []
    run = [x for x in lines if x and not x.startswith("#") and not x.startswith("ARG ")]
    chk = [x for x in run if "sha256sum" in x]
    tar = [k for k, x in enumerate(run) if "tar -xJf" in x]
    if len(chk) != 1 or len(tar) != 1 or run.index(chk[0]) > tar[0]:
        out.append("checksum check must precede the node tar")
    elif not re.fullmatch(r'&& echo "\$\{h\}  /tmp/\$\{f\}" \| sha256sum -c -', chk[0]):
        out.append("checksum check must be a plain enforced sha256sum -c of the pinned value")
    if run and not run[0].startswith("RUN set -eu;"):
        out.append("set -e missing")
    if any(not x.startswith("&& ") for x in run[1:]):
        out.append("&& chain broken")
    if "||" in blk or re.search(r"sha256sum[^\n]*;", blk) or "|| true" in blk:
        out.append("check or chain masked")
    if "chown -R root:root /opt/node" not in blk:
        out.append("/opt/node not root-owned")
    if len(re.findall(r"ln -s /opt/node/bin/node /usr/local/bin/node", t)) != 1:
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

    def mut(self, old, new):
        t = read(DF)
        self.assertIn(old, t)
        m = t.replace(old, new)
        self.assertNotEqual(m, t)
        return m

    def test_mutant_checksum_dropped(self):
        self.assertTrue(problems(self.mut(' && echo "${h}  /tmp/${f}" | sha256sum -c - \\\n', "")))

    def test_mutant_pin_dropped(self):
        self.assertTrue(problems(self.mut('echo "${h}  /tmp/${f}"', 'cat "/tmp/${f}"')))
        self.assertTrue(problems(self.mut('h="${NODE_SHA256_X64}"', 'h=""')))

    def test_mutant_masked_true(self):
        self.assertTrue(problems(self.mut("sha256sum -c - \\\n && mkdir", "sha256sum -c - || true \\\n && mkdir")))
        self.assertTrue(problems(self.mut("sha256sum -c - \\\n && mkdir", "sha256sum -c -; true \\\n && mkdir")))

    def test_mutant_chown_removed(self):
        self.assertTrue(problems(self.mut(" && chown -R root:root /opt/node", "")))

    def test_mutant_check_after_tar(self):
        t = read(DF)
        chk = ' && echo "${h}  /tmp/${f}" | sha256sum -c - \\\n'
        self.assertIn(chk, t)
        tl = t.replace(chk, "")
        k = tl.index(" && ln -s /opt/node/bin/node")
        self.assertTrue(problems(tl[:k] + chk.rstrip("\\\n").rstrip() + " \\\n" + tl[k:]))

    def test_mutant_version_loosened(self):
        for v in ("22.", "22.x", "22.23", "22.23.4-rc1"):
            self.assertTrue(problems(self.mut("NODE_VERSION=22.23.4", "NODE_VERSION=" + v)))

    def test_mutant_shasums_url_back(self):
        self.assertTrue(problems(read(DF).replace("# ---- end Node", "ARG NODE_SHASUMS_URL=https://example.invalid/SHASUMS256.txt\n# ---- end Node")))

    def test_mutant_bad_hash(self):
        self.assertTrue(problems(self.mut("NODE_SHA256_ARM64=6287", "NODE_SHA256_ARM64=zzz4")))

    def test_pinned_values(self):
        t = read(DF)
        self.assertIn("NODE_SHA256_X64=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce", t)
        self.assertIn("NODE_SHA256_ARM64=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b", t)

    def test_mutant_apt_nodejs(self):
        self.assertTrue(problems(self.mut("jq procps", "jq procps nodejs npm")))

    def test_mutant_curl_sh(self):
        t = read(DF) + "\nRUN curl -fsSL https://example.invalid/setup | bash -\n"
        self.assertTrue(problems(t))

    def test_mutant_two_versions(self):
        self.assertTrue(problems(self.mut("/dist/v${NODE_VERSION}/${f}", "/dist/v22.23.4/${f}")))

    def test_version_floor(self):
        t = read(DF)
        v = re.search(r"^ARG NODE_VERSION=(\S+)$", t, re.M).group(1)
        self.assertGreaterEqual(tuple(int(x) for x in v.split(".")), FLOOR)

    def test_mutant_below_floor(self):
        m = self.mut("NODE_VERSION=22.23.4", "NODE_VERSION=22.23.2")
        self.assertIn("NODE_VERSION=22.23.2", m)
        self.assertIn("NODE_VERSION below the security floor", problems(m))

    def test_mutant_major_20(self):
        self.assertTrue(problems(self.mut("NODE_VERSION=22.23.4", "NODE_VERSION=20.1.0")))

    def test_docs(self):
        d = read(os.path.join(CLOUD, "README.md"))
        self.assertTrue("Node 22" in d and "NODE_VERSION" in d and "NODE_SHA256_X64" in d and "not measured" in d)


if __name__ == "__main__":
    unittest.main()

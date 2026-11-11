"""#2383: the Dockerfile carries one pinned Go toolchain, checked before it is unpacked; the docs name it.
Offline: reads files only. Scenarios S2, S3, S4 (a mutant without the hash check fails with an anchored text, its control first)."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
DF = os.path.join(CLOUD, "worker", "Dockerfile")
VERSION = "1.27.4"
SHA = "7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840"
CHECK = ' && echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum -c - \\\n'
UNCHECKED = "the Go archive was not checked"


def read(p):
    with open(p) as f:
        return f.read()


def go_run(t):
    """The one RUN of the Go block, as flat text (continuations joined), or ''."""
    b = t.find("ARG GO_SHA256=")
    e = t.find("# ---- end Go ----", b)
    if b < 0 or e < 0:
        return ""
    blk = t[b:e]
    return " ".join(x.strip() for x in blk.replace("\\\n", " ").split("\n") if x.strip() and not x.startswith(("#", "ARG ", "ENV ")))


def problems(t):
    out = []
    if len(re.findall(r"^ARG GO_VERSION=%s$" % re.escape(VERSION), t, re.M)) != 1:
        out.append("GO_VERSION must be pinned once to " + VERSION)
    if len(re.findall(r"^ARG GO_SHA256=%s$" % SHA, t, re.M)) != 1:
        out.append("GO_SHA256 must be pinned once to the stated hash")
    run = go_run(t)
    if not run.startswith("RUN "):
        out.append("no Go RUN")
    chk = run.find('echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum -c -')
    tar = run.find("tar -C /usr/local -xzf /tmp/go.tgz")
    if chk < 0 or tar < 0 or chk > tar:
        out.append(UNCHECKED)
    if "curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz -o /tmp/go.tgz" not in run:
        out.append("Go archive not downloaded with curl -fsSL from the pinned version")
    if "chown -R root:root /usr/local/go" not in run or "chmod -R go-w /usr/local/go" not in run:
        out.append("/usr/local/go not root-owned and not writable by others")
    for n in ("go", "gofmt"):
        if "ln -s /usr/local/go/bin/%s /usr/local/bin/%s" % (n, n) not in run:
            out.append(n + " not linked")
    if not re.search(r"&& rm /tmp/go\.tgz$", run):
        out.append("archive not removed in the same RUN")
    if "||" in run or re.search(r"sha256sum[^&]*;", run):
        out.append("check or chain masked")
    if len(re.findall(r"^ENV GOTOOLCHAIN=local$", t, re.M)) != 1 or len(re.findall(r"GOTOOLCHAIN", t)) != 1:
        out.append("GOTOOLCHAIN=local must be set once")
    envs = re.findall(r"^ENV\b((?:[^\n]*\\\n)*[^\n]*)", t, re.M)
    for e in envs:
        for name in re.findall(r"(?:^|\s)([A-Za-z_]\w*)=", e.replace("\\\n", " ")):
            if name.startswith("PG") and name != "PG_BUNDLE":
                out.append("PG-prefixed variable " + name)
    if re.search(r"\bexport\s+PG", t):
        out.append("PG-prefixed export")
    if len(re.findall(r"^ENTRYPOINT\b", t, re.M)) != 1 or len(re.findall(r"^CMD\b", t, re.M)) != 1:
        out.append("ENTRYPOINT or CMD changed")
    users = re.findall(r"^USER (\S+)$", t, re.M)
    if users != ["worker", "root", "root", "worker"] or not t.rstrip().endswith("USER worker"):
        out.append("USER lines changed")
    return out


class T(unittest.TestCase):
    def mut(self, old, new):
        t = read(DF)
        self.assertEqual(t.count(old), 1)
        m = t.replace(old, new)
        self.assertNotEqual(m, t)
        return m

    def test_real_file_clean(self):
        self.assertEqual(problems(read(DF)), [])

    def test_mutant_check_dropped(self):
        self.assertEqual(problems(read(DF)), [])  # the control first
        self.assertIn(UNCHECKED, problems(self.mut(CHECK, "")))

    def test_mutant_check_after_unpack(self):
        t = self.mut(CHECK, "")
        k = t.index(" && chown -R root:root /usr/local/go")
        self.assertIn(UNCHECKED, problems(t[:k] + CHECK.rstrip("\\\n").rstrip() + " \\\n" + t[k:]))

    def test_mutant_masked(self):
        self.assertTrue(problems(self.mut("sha256sum -c - \\\n && tar -C", "sha256sum -c - || true \\\n && tar -C")))

    def test_mutants_other(self):
        for old, new in (("GO_VERSION=1.27.4", "GO_VERSION=1.27.3"),
                         ("GO_SHA256=7226", "GO_SHA256=7227"),
                         (" && chown -R root:root /usr/local/go", ""),
                         (" && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt", ""),
                         (" \\\n && rm /tmp/go.tgz", ""),
                         ("ENV GOTOOLCHAIN=local\n", ""),
                         ("ENV GOTOOLCHAIN=local\n", "ENV GOTOOLCHAIN=local\nENV PGHOST=x\n"),
                         ("ENV GOTOOLCHAIN=local\n", "ENV GOTOOLCHAIN=auto\n"),
                         ("USER worker\n", "USER root\n")):
            if old == "USER worker\n":
                t = read(DF)
                self.assertTrue(problems(t.rstrip("\n").rsplit("USER worker", 1)[0] + "USER root\n"))
            else:
                self.assertTrue(problems(self.mut(old, new)), old)

    def test_docs(self):
        d = read(os.path.join(CLOUD, "README.md"))
        self.assertTrue("Go 1.27.4" in d and "GO_VERSION" in d and "GO_SHA256" in d)


if __name__ == "__main__":
    unittest.main()

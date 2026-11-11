"""#2407: the Dockerfile carries one pinned mise binary, checked before it is unpacked; the docs name it.
Offline: reads files only. Scenarios S2, S3, S4 (a mutant without the hash check fails with an anchored text, its control first)."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
DF = os.path.join(CLOUD, "worker", "Dockerfile")
VERSION = "v2026.11.6"
SHA = "d6ebb6f5601bdf5a8d1d56028b24fe575f45075b989941ccc920f95161c5fa28"
CHECK = ' && echo "${MISE_SHA256}  /tmp/mise.tgz" | sha256sum -c - \\\n'
UNCHECKED = "the mise archive was not checked"


def read(p):
    with open(p) as f:
        return f.read()


def mise_run(t):
    """The one RUN of the mise block, as flat text (continuations joined), or ''."""
    b = t.find("ARG MISE_SHA256=")
    e = t.find("# ---- end mise ----", b)
    if b < 0 or e < 0:
        return ""
    blk = t[b:e]
    return " ".join(x.strip() for x in blk.replace("\\\n", " ").split("\n") if x.strip() and not x.startswith(("#", "ARG ", "ENV ")))


def problems(t):
    out = []
    if len(re.findall(r"^ARG MISE_VERSION=%s$" % re.escape(VERSION), t, re.M)) != 1:
        out.append("MISE_VERSION must be pinned once to " + VERSION)
    if len(re.findall(r"^ARG MISE_SHA256=%s$" % SHA, t, re.M)) != 1:
        out.append("MISE_SHA256 must be pinned once to the stated hash")
    run = mise_run(t)
    if not run.startswith("RUN "):
        out.append("no mise RUN")
    chk = run.find('echo "${MISE_SHA256}  /tmp/mise.tgz" | sha256sum -c -')
    tar = run.find("tar -xzf /tmp/mise.tgz")
    ins = run.find("install -o root -g root -m 0755 /tmp/mise/bin/mise /usr/local/bin/mise")
    if chk < 0 or tar < 0 or chk > tar or chk > ins:
        out.append(UNCHECKED)
    if ins < 0:
        out.append("mise not installed root-owned, mode 0755, at /usr/local/bin/mise")
    if "curl -fsSL https://github.com/jdx/mise/releases/download/${MISE_VERSION}/mise-${MISE_VERSION}-linux-x64.tar.gz -o /tmp/mise.tgz" not in run:
        out.append("mise archive not downloaded with curl -fsSL from the pinned version")
    if not re.search(r"&& rm -rf /tmp/mise\.tgz /tmp/mise$", run):
        out.append("archive not removed in the same RUN")
    if "||" in run or re.search(r"sha256sum[^&]*;", run):
        out.append("check or chain masked")
    if re.search(r"python3?\b[^\n]*\bln\b|\bln\b[^\n]*/usr/local/bin/python|update-alternatives", run):
        out.append("python changed by the mise block")
    if re.search(r"^(?:RUN|ENV)\b[^\n]*(?:/usr/local/bin/python3?\b|/usr/bin/python3?\b)[^\n]*\bln\b", t, re.M):
        out.append("python3 or python linked")
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

    def test_mutant_masked(self):
        self.assertTrue(problems(self.mut("sha256sum -c - \\\n && tar -xzf /tmp/mise.tgz", "sha256sum -c - || true \\\n && tar -xzf /tmp/mise.tgz")))

    def test_mutants_other(self):
        for old, new in (("MISE_VERSION=v2026.11.6", "MISE_VERSION=v2026.11.5"),
                         ("MISE_SHA256=d6eb", "MISE_SHA256=d6ec"),
                         ("install -o root -g root -m 0755", "install -m 0777"),
                         (" \\\n && rm -rf /tmp/mise.tgz /tmp/mise", ""),
                         ("# ---- end mise ----\n", "# ---- end mise ----\nENV PGHOST=x\n"),
                         ("# ---- end mise ----\n", "# ---- end mise ----\nRUN ln -sf /usr/local/bin/python3.11 /usr/bin/python3\n")):
            self.assertTrue(problems(self.mut(old, new)), old)
        t = read(DF)
        self.assertTrue(problems(t.rstrip("\n").rsplit("USER worker", 1)[0] + "USER root\n"))

    def test_docs(self):
        d = read(os.path.join(CLOUD, "README.md"))
        self.assertTrue("mise` v2026.11.6" in d and "MISE_VERSION" in d and "MISE_SHA256" in d and "install at run time" in d)


if __name__ == "__main__":
    unittest.main()

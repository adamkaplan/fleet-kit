"""#1353: the worker image carries PostgreSQL 16 and two extensions built in a separate stage, each source pinned by
version and sha256, checked with sha256sum -c before the unpack; nothing starts the database at pod start.
Offline: reads files only. Scenarios S1, S2, S3, S4, S6, S8, S11, U3."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(HERE))
DF = os.path.join(CLOUD, "worker", "Dockerfile")
ENTRY = os.path.join(CLOUD, "worker", "entrypoint.sh")
README = os.path.join(CLOUD, "README.md")
QUICK = os.path.join(os.path.dirname(CLOUD), "docs", "cloud-operator-quickstart.md")
SOURCES = (("PG", "tar -xjf /tmp/pg.tar.bz2"), ("PGVECTOR", "tar -xzf /tmp/pgvector.tgz"), ("PGMQ", "tar -xzf /tmp/pgmq.tgz"))


def read(p):
    with open(p) as f:
        return f.read()


def stage(t):
    b = t.find("# ---- #1353 PostgreSQL build stage")
    e = t.find("# ---- end #1353 PostgreSQL build stage", b)
    return t[b:e] if b >= 0 and e > b else ""


def steps(blk):
    """Each RUN as its list of chained commands."""
    flat = blk.replace("\\\n", " ")
    out = []
    for ln in flat.split("\n"):
        if ln.startswith("RUN "):
            out.append([c.strip() for c in re.split(r" && |; ", ln[4:])])
    return out


def problems(t):
    out = []
    blk = stage(t)
    if not blk:
        return ["no postgres build stage"]
    if len(re.findall(r"(?m)^FROM \S+ AS pgbuild$", blk)) != 1:
        out.append("pgbuild stage missing")
    if not re.search(r"(?m)^FROM buildpack-deps:24\.04@sha256:[0-9a-f]{64} AS pgbuild$", blk):
        out.append("build base not pinned by digest")
    first = re.search(r"AS pgbuild\n((?:ARG .*\n)+)", blk)
    if not first or first.group(1).count("ARG ") != 6:
        out.append("pins must be the six ARG lines at the top of the block")
    ver = re.findall(r"^ARG PG_VERSION=(16\.\d+)$", blk, re.M)
    if len(ver) != 1:
        out.append("PG_VERSION must be a 16.x pinned once")
    flat = blk.replace("\\\n", " ")
    for n, unpack in SOURCES:
        if len(re.findall(r"^ARG " + n + r"_VERSION=\d+(\.\d+)+$", blk, re.M)) != 1:
            out.append(n + " version ARG")
        if len(re.findall(r"^ARG " + n + r"_SHA256=[0-9a-f]{64}$", blk, re.M)) != 1:
            out.append(n + " sha256 must be pinned once as 64 hex")
        runs = [r for r in steps(blk) if any(unpack in c for c in r)]
        if len(runs) != 1:
            out.append(n + " must be unpacked in exactly one step")
            continue
        r = runs[0]
        chk = [k for k, c in enumerate(r) if "sha256sum" in c]
        tar = [k for k, c in enumerate(r) if unpack in c]
        if len(chk) != 1 or len(tar) != 1 or chk[0] > tar[0]:
            out.append(n + " check must precede the unpack")
        elif r[chk[0]] != 'echo "${' + n + '_SHA256}  /tmp/' + {"PG": "pg.tar.bz2", "PGVECTOR": "pgvector.tgz", "PGMQ": "pgmq.tgz"}[n] + '" | sha256sum -c -':
            out.append(n + " check must be a plain enforced sha256sum -c of the pinned value")
        if not r[0].startswith("set -eu"):
            out.append(n + " step without set -eu")
    if "||" in flat or re.search(r"sha256sum[^\n]*;", blk.replace("set -eu;", "")):
        out.append("check or chain masked")
    code = "\n".join(x for x in blk.replace("\\\n", " ").split("\n") if not x.startswith("#"))
    if len(re.findall(r"sha256sum -c", code)) != 3:
        out.append("expected 3 checks")
    # U3: no download of the whole file without a check
    nc = "\n".join(x for x in t.replace("\\\n", " ").split("\n") if not x.startswith("#"))
    if len(re.findall(r"\bcurl\b[^\n]*-o \"?/tmp/", nc)) != len(re.findall(r"sha256sum -c", nc)):
        out.append("a download lacks a sha256sum -c")
    if re.search(r"curl[^\n]*\|\s*(ba)?sh|git clone|apt-get install[^\n]*postgresql", t):
        out.append("unpinned channel")
    if "COPY --from=pgbuild /opt/pg /opt/pg" not in t[t.find("AS final"):]:
        out.append("installed files not copied to the worker stage")
    w = t[t.find("AS worker"):]
    wf = w.replace("\\\n", " ")
    if re.search(r"apt-get install[^\n]*\b(build-essential|gcc|g\+\+|cc)\b", wf):
        out.append("compiler in the worker stage")
    if re.search(r"(?m)^(ENV|EXPOSE).*(PG|5432)", w):
        out.append("port or server environment in the worker stage")
    return out


class T(unittest.TestCase):
    def test_real_file_clean(self):
        self.assertEqual(problems(read(DF)), [])

    def test_three_of_three_pinned(self):
        t = read(DF)
        self.assertEqual(len(re.findall(r"^ARG (PG|PGVECTOR|PGMQ)_SHA256=[0-9a-f]{64}$", stage(t), re.M)), 3)
        self.assertEqual(len(re.findall(r"^ARG (PG|PGVECTOR|PGMQ)_VERSION=", stage(t), re.M)), 3)

    def mut(self, old, new):
        t = read(DF)
        self.assertIn(old, t)
        m = t.replace(old, new, 1)
        self.assertNotEqual(m, t)
        return m

    def test_mutant_checksum_removed(self):
        for n, f in (("PG", "pg.tar.bz2"), ("PGVECTOR", "pgvector.tgz"), ("PGMQ", "pgmq.tgz")):
            line = ' && echo "${' + n + '_SHA256}  /tmp/' + f + '" | sha256sum -c - \\\n'
            self.assertTrue(problems(self.mut(line, "")), n)

    def test_mutant_short_value(self):
        for n in ("PG", "PGVECTOR", "PGMQ"):
            t = read(DF)
            m = re.sub(r"^(ARG " + n + r"_SHA256=)[0-9a-f]{64}$", r"\g<1>abc123", t, count=1, flags=re.M)
            self.assertNotEqual(m, t)
            self.assertTrue(problems(m), n)

    def test_mutant_check_after_unpack(self):
        for n, f, unpack in (("PG", "pg.tar.bz2", "tar -xj"), ("PGVECTOR", "pgvector.tgz", "tar -xz"), ("PGMQ", "pgmq.tgz", "tar -xz")):
            t = read(DF)
            chk = ' && echo "${' + n + '_SHA256}  /tmp/' + f + '" | sha256sum -c - \\\n'
            self.assertIn(chk, t)
            tl = t.replace(chk, "", 1)
            k = tl.index(" && mkdir -p /src/" + ("pg " if n == "PG" else n.lower() + " "))
            k2 = tl.index(" && cd /src/", k)
            m = tl[:k2] + chk.rstrip("\\\n").rstrip() + " \\\n" + tl[k2:]
            self.assertNotEqual(m, t)
            self.assertTrue(problems(m), n)

    def test_mutant_masked_true(self):
        for n, f in (("PG", "pg.tar.bz2"), ("PGVECTOR", "pgvector.tgz"), ("PGMQ", "pgmq.tgz")):
            old = '| sha256sum -c - \\\n'
            t = read(DF)
            i = t.index('echo "${' + n + '_SHA256}')
            j = t.index(old, i)
            m = t[:j] + '| sha256sum -c - || true \\\n' + t[j + len(old):]
            self.assertNotEqual(m, t)
            self.assertTrue(problems(m), n)

    def test_mutant_unpinned_download(self):
        t = read(DF)
        m = t.replace("# ---- end #1353 PostgreSQL build stage", "RUN curl -fsSL https://example.invalid/x.tgz -o /tmp/x.tgz\n# ---- end #1353 PostgreSQL build stage")
        self.assertTrue(problems(m))

    def test_mutant_compiler_in_worker(self):
        self.assertTrue(problems(self.mut("jq procps", "jq procps build-essential")))

    def test_mutant_not_copied(self):
        self.assertTrue(problems(self.mut("COPY --from=pgbuild /opt/pg /opt/pg", "RUN true")))

    def test_entrypoint_starts_nothing(self):
        e = read(ENTRY)
        self.assertEqual(len(re.findall(r"pg_ctl|postgres|initdb|/opt/pg|5432", e)), 0)
        t = read(DF)
        w = t[t.find("AS worker"):]
        self.assertEqual(len(re.findall(r"pg_ctl|initdb", w)), 0)
        self.assertEqual(len(re.findall(r"(?m)^(EXPOSE|CMD|ENTRYPOINT)\b.*(postgres|pg_ctl|5432)", w)), 0)
        self.assertIn("USER worker", w)
        self.assertEqual(len(re.findall(r"(?m)^USER root", t[:t.find("FROM worker AS scan")])), 0)

    def test_scan_detects_a_start(self):
        self.assertEqual(len(re.findall(r"pg_ctl|postgres|initdb", read(ENTRY) + "\npg_ctl start\n")), 1)

    def test_docs(self):
        d = read(README)
        self.assertIn("about 20 of 60 minutes", d)
        for k in ("PG_VERSION", "PGVECTOR_VERSION", "PGMQ_VERSION", "measured by the operator at the first real build", "previous image digest", "pg_ctl"):
            self.assertIn(k, d)
        q = read(QUICK)
        for k in ("PostgreSQL 16", "measured by the operator at the first real build", "previous image digest"):
            self.assertIn(k, q)


if __name__ == "__main__":
    unittest.main()

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
    flat = blk.replace("\\\n", " ")
    # PostgreSQL: version + 64-hex sha256, checked (plain, enforced) before the unpack
    if len(re.findall(r"^ARG PG_VERSION=16\.\d+$", blk, re.M)) != 1:
        out.append("PG version ARG")
    if len(re.findall(r"^ARG PG_SHA256=[0-9a-f]{64}$", blk, re.M)) != 1:
        out.append("PG sha256 must be pinned once as 64 hex")
    runs = [r for r in steps(blk) if any("tar -xjf /tmp/pg.tar.bz2" in c for c in r)]
    if len(runs) != 1:
        out.append("PG must be unpacked in exactly one step")
    else:
        r = runs[0]
        chk = [k for k, c in enumerate(r) if "sha256sum" in c]
        tar = [k for k, c in enumerate(r) if "tar -xjf" in c]
        if len(chk) != 1 or len(tar) != 1 or chk[0] > tar[0]:
            out.append("PG check must precede the unpack")
        elif r[chk[0]] != 'echo "${PG_SHA256}  /tmp/pg.tar.bz2" | sha256sum -c -':
            out.append("PG check must be a plain enforced sha256sum -c of the pinned value")
        if not r[0].startswith("set -eu"):
            out.append("PG step without set -eu")
    # extensions: version + 40-hex commit; clone, checkout, verify BEFORE any build step
    for n, url in (("PGVECTOR", "https://github.com/pgvector/pgvector"), ("PGMQ", "https://github.com/pgmq/pgmq")):
        if len(re.findall(r"^ARG " + n + r"_VERSION=\d+(\.\d+)+$", blk, re.M)) != 1:
            out.append(n + " version ARG")
        if len(re.findall(r"^ARG " + n + r"_COMMIT=[0-9a-f]{40}$", blk, re.M)) != 1:
            out.append(n + " commit must be pinned once as 40 hex")
        if re.search(r"^ARG " + n + r"_SHA256", blk, re.M):
            out.append(n + " still pinned by an archive sha256")
        runs = [r for r in steps(blk) if any("git clone" in c and url in c for c in r)]
        if len(runs) != 1:
            out.append(n + " must be cloned in exactly one step")
            continue
        r = runs[0]
        if not r[0].startswith("set -eu"):
            out.append(n + " step without set -eu")
        ver = [k for k, c in enumerate(r) if c == 'test "$(git rev-parse HEAD)" = "${' + n + '_COMMIT}"']
        co = [k for k, c in enumerate(r) if re.fullmatch(r'git checkout( --detach)? "\$\{' + n + r'_COMMIT\}"', c)]
        mk = [k for k, c in enumerate(r) if re.match(r"make\b", c)]
        if len(ver) != 1 or len(co) != 1 or not mk:
            out.append(n + " needs one checkout of the commit ARG, one comparison of git rev-parse HEAD, and a build")
        elif not (co[0] < ver[0] < min(mk)):
            out.append(n + " comparison must come after the checkout and before any build step")
    if "||" in flat:
        out.append("check or chain masked")
    code = "\n".join(x for x in blk.replace("\\\n", " ").split("\n") if not x.startswith("#"))
    if len(re.findall(r"sha256sum -c", code)) != 1:
        out.append("expected 1 sha256 check (PostgreSQL)")
    if len(re.findall(r"git rev-parse HEAD", code)) != 2:
        out.append("expected 2 commit comparisons")
    if "archive/refs/tags" in blk:
        out.append("a tag archive is used")
    # U3: no download without a verified pin (curl needs a sha256 check; every clone needs a commit comparison)
    nc = "\n".join(x for x in t.replace("\\\n", " ").split("\n") if not x.startswith("#"))
    if len(re.findall(r"\bcurl\b[^\n]*-o \"?/tmp/", nc)) != len(re.findall(r"sha256sum -c", nc)):
        out.append("a download lacks a sha256sum -c")
    if len(re.findall(r"\bgit clone\b", nc)) != len(re.findall(r"git rev-parse HEAD", nc)):
        out.append("a clone lacks a commit comparison")
    if re.search(r"curl[^\n]*\|\s*(ba)?sh|apt-get install[^\n]*postgresql", t):
        out.append("unpinned channel")
    # S12: the build-time secret scan reads /opt/pg
    sc = t[t.find("FROM worker AS scan"):t.find("FROM worker AS final")]
    if "COPY --from=pgbuild /opt/pg /opt/pg" not in sc or sc.find("COPY --from=pgbuild") > sc.find("RUN /scan-secrets.sh"):
        out.append("the scan stage must hold /opt/pg before the scan runs")
    if re.search(r"--skip\s+opt\b", sc):
        out.append("the scan skips /opt")
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
        s = stage(read(DF))
        self.assertEqual(len(re.findall(r"^ARG PG_SHA256=[0-9a-f]{64}$", s, re.M)), 1)
        self.assertEqual(len(re.findall(r"^ARG (PGVECTOR|PGMQ)_COMMIT=[0-9a-f]{40}$", s, re.M)), 2)
        self.assertEqual(len(re.findall(r"^ARG (PG|PGVECTOR|PGMQ)_VERSION=", s, re.M)), 3)
        self.assertNotIn("archive/refs/tags", s)

    def test_commit_is_the_tags_commit(self):
        # recorded from two separate `git ls-remote` calls and the GitHub tag ref (lightweight tags: the tag is the commit)
        s = stage(read(DF))
        self.assertIn("ARG PGVECTOR_COMMIT=f37c13f68b57d2c3472b2214fbcff699d6d34876", s)
        self.assertIn("ARG PGMQ_COMMIT=8eab18272e50077ab9f72d9787296655f2849dcc", s)

    def mut(self, old, new):
        t = read(DF)
        self.assertIn(old, t)
        m = t.replace(old, new, 1)
        self.assertNotEqual(m, t)
        return m

    def mutants(self):
        """(name, mutated text): for each pin kind (PG sha256, pgvector commit, pgmq commit) four mutants."""
        t = read(DF)
        out = []
        pg = ' && echo "${PG_SHA256}  /tmp/pg.tar.bz2" | sha256sum -c - \\\n'
        self.assertIn(pg, t)
        tl = t.replace(pg, "", 1)
        k = tl.index(" && mkdir -p /src/pg ")
        k2 = tl.index(" && cd /src/pg ", k)
        out.append(("PG removed", t.replace(pg, "", 1)))
        out.append(("PG shortened", re.sub(r"^(ARG PG_SHA256=)[0-9a-f]{64}$", r"\g<1>abc123", t, count=1, flags=re.M)))
        out.append(("PG after unpack", tl[:k2] + pg.rstrip("\\\n").rstrip() + " \\\n" + tl[k2:]))
        out.append(("PG masked", t.replace(pg, pg.replace("sha256sum -c - ", "sha256sum -c - || true "), 1)))
        for n in ("PGVECTOR", "PGMQ"):
            cmp_ = ' && test "$(git rev-parse HEAD)" = "${' + n + '_COMMIT}" \\\n'
            self.assertIn(cmp_, t)
            tl = t.replace(cmp_, "", 1)
            out.append((n + " removed", tl))
            out.append((n + " shortened", re.sub(r"^(ARG " + n + r"_COMMIT=)[0-9a-f]{40}$", r"\g<1>abc123", t, count=1, flags=re.M)))
            out.append((n + " after build", self.after_build(tl, n, cmp_)))
            out.append((n + " masked", t.replace(cmp_, cmp_.rstrip("\\\n").rstrip() + " || true \\\n", 1)))
        return out

    def after_build(self, tl, n, cmp_):
        """The comparison moved to the end of the extension's step (after its build and install)."""
        i = tl.index("git clone", tl.index("ARG " + n + "_COMMIT"))
        i = tl.index("/src/" + n.lower(), i)
        e = tl.index("\n", tl.index(" install", i))
        return tl[:e] + cmp_.rstrip("\\\n").rstrip() + tl[e:]

    def test_every_pin_mutant_is_refused(self):
        t = read(DF)
        ms = self.mutants()
        self.assertEqual(len(ms), 12)
        self.assertEqual(problems(t), [])
        for name, m in ms:
            self.assertNotEqual(m, t, name + " not applied")
            self.assertTrue(problems(m), name + " accepted")

    def test_s12_scan_covers_opt_pg(self):
        t = read(DF)
        self.assertEqual(problems(t), [])
        sc = t[t.find("FROM worker AS scan"):t.find("FROM worker AS final")]
        self.assertLess(sc.find("COPY --from=pgbuild /opt/pg /opt/pg"), sc.find("RUN /scan-secrets.sh"))
        self.assertNotRegex(sc, r"--skip\s+opt\b")
        self.assertTrue(problems(self.mut("COPY --from=pgbuild /opt/pg /opt/pg\nCOPY worker/scan-secrets.sh", "COPY worker/scan-secrets.sh")))
        self.assertTrue(problems(self.mut("--skip proc", "--skip opt --skip proc")))

    def test_u3_clone_without_verification(self):
        t = read(DF)
        m = t.replace("# ---- end #1353 PostgreSQL build stage", "RUN git clone https://github.com/acme/x /src/x\n# ---- end #1353 PostgreSQL build stage")
        self.assertTrue(problems(m))
        self.assertTrue(problems(self.mut("https://github.com/pgvector/pgvector /src", "https://github.com/pgvector/pgvector/archive/refs/tags/v1.tar.gz /src")))

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

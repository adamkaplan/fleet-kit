"""Ask #2329: the final stage builds /opt/pg-bundle from /opt/pg with copies (the layout scripts/dev/ephemeral-pg.sh reads through
PG_BUNDLE), sets PG_BUNDLE, starts no server, keeps the non-root run user; and the image carries the token service's launcher files.
Offline: reads the Dockerfile only. The image is not built here. Scenarios S2, S3, S4."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DF = os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "Dockerfile")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
LAUNCHER = os.path.join(ROOT, "cloud", "launcher")
BIN = os.path.join(ROOT, "bin")
IMPORT = re.compile(r"(?m)^[ \t]*(?:import ([\w., ]+)|from (\w+)[\w.]* import)")


# bin/ modules shipped as byte-identical copies in cloud/launcher (bin/ is outside the build context); the launcher's own files that share a
# name with a bin/ file (cloud_worker, token_mint) are different files and are not copies.
BIN_COPIES = ("path_walk.py",)


def walk_imports(start="token_service.py"):
    """Every module token_service.py reaches (import x / from x import y, at any indentation), resolved in cloud/launcher then bin;
    the standard library and anything else not found there is ignored. Returns {file name: directory}."""
    found, todo = {}, [os.path.join(LAUNCHER, start)]
    found[start] = LAUNCHER
    while todo:
        f = todo.pop()
        for m in IMPORT.finditer(read(f)):
            mods = [x.strip().split(" as ")[0].split(".")[0] for x in m.group(1).split(",")] if m.group(1) else [m.group(2)]
            for mod in mods:
                for d in (LAUNCHER, BIN):
                    if os.path.isfile(os.path.join(d, mod + ".py")):
                        if mod + ".py" not in found:
                            found[mod + ".py"] = d
                            todo.append(os.path.join(d, mod + ".py"))
                        break
    return found


def copy_problems(bin_dir, launcher_dir, names):
    """Each named copy must exist in launcher_dir and equal bin_dir's file byte for byte; returns the problems."""
    out = []
    for n in names:
        try:
            with open(os.path.join(bin_dir, n), "rb") as a, open(os.path.join(launcher_dir, n), "rb") as b:
                if a.read() != b.read():
                    out.append(n + " copy differs")
        except OSError:
            out.append(n + " copy is missing")
    return out


def tolerated_all():
    """Modules imported only lazily inside a try block that tolerates failure (cloud_worker.retire_row: `import registry`), not at start:
    listed here, not shipped."""
    return {"registry.py"}


def read(p):
    with open(p) as f:
        return f.read()


def flat(t):
    """Continuation lines joined, comment lines dropped, runs of blanks collapsed."""
    t = t.replace("\\\n", " ")
    t = "\n".join(re.sub(r"[ \t]+", " ", x) for x in t.split("\n") if not x.lstrip().startswith("#"))
    return t


def final_stage(t):
    i = t.find("FROM worker AS final")
    return flat(t[i:]) if i >= 0 else ""


SERVER_WORDS = ("pg_ctl", "pg_ctlcluster", "initdb", "postgres", "systemctl", "service")
# Commands whose arguments only name a file or text (never run it): a server word after them is not a start.
INERT = ("chmod", "chown", "chgrp", "cp", "mv", "ln", "ls", "rm", "mkdir", "touch", "install", "echo", "printf", "cat", "test", "stat", "readlink",
         "useradd", "groupadd")


def starts_server(f):
    """True when a RUN/CMD/ENTRYPOINT line of the stage holds a server word (any path prefix) in a segment (split at && || ; | ( ) $( and a
    backtick) whose command is not an inert one: this catches `then pg_ctl`, `bash -c 'postgres'`, `timeout 5 postgres`, `sudo -u x pg_ctl`."""
    for line in f.split("\n"):
        m = re.match(r"\s*(RUN|CMD|ENTRYPOINT) (.*)", line, re.I)
        if not m:
            continue
        if "<<" in m.group(2):  # a heredoc body is not scanned: refuse it
            return True
        subst = "$(" in m.group(2) or "`" in m.group(2)  # a substitution may run what it names: no segment is inert
        for seg in re.split(r"&|;|\||\(|\)|\$\(|`", m.group(2)):
            words = [w.strip("\"'[],") for w in seg.split()]
            while words and (re.match(r"\w+=", words[0]) or words[0] in ("sudo", "exec", "nohup", "env", "su", "gosu", "then", "do", "else", "if", "!", "time")):
                words = words[1:]
            if words and not subst and os.path.basename(words[0]) in INERT:
                continue
            if any(os.path.basename(w) in SERVER_WORDS for w in words):
                return True
    return False


def problems(t):
    out = []
    f = final_stage(t)
    if not f:
        return ["no final stage"]
    for name, pat in (
        ("bin and lib", r'cp -a /opt/pg/bin /opt/pg/lib "\$l/"'),
        ("the bundle's lib directory", r'l="\$b/usr/lib/postgresql/16"'),
        ("the bundle root", r'b="/home/worker/\.cache/\$\{CW_BUNDLE_CACHE_NAME\}";'),
        ("the extension directory", r'cp -a [^\n]*vector--\*\.sql[^\n]*pgmq--\*\.sql[^\n]*pg_trgm\.control[^\n]*pg_trgm--\*\.sql "\$e/"'),
        ("the extension target", r'e="\$b/usr/share/postgresql/16/extension"'),
        ("the configuration sample", r'cp -a "\$s/postgresql\.conf\.sample" "\$b/usr/share/postgresql/postgresql\.conf\.sample"'),
    ):
        if not re.search(pat, f):
            out.append("the %s was not copied" % name)
    if not re.search(r'cp -a /opt/pg/share "\$l/share"', f):
        out.append("the share directory was not copied")
    if re.search(r"\bln -s|\bln\b[^\n]*/opt/pg", f):
        out.append("a link line")
    if not re.search(r'chown -R root:root "\$b"', f) or not re.search(r'chmod -R go-w,a\+rX "\$b"', f):
        out.append("the bundle is not root-owned and read-only")
    # ask #2367: the bundle is in the cache directory named by a required build argument; no PG-prefixed variable anywhere
    if not re.search(r"(?m)^ARG CW_BUNDLE_CACHE_NAME$", f) or not re.search(r'(?m)^RUN test -n "\$CW_BUNDLE_CACHE_NAME"$', f):
        out.append("the bundle cache name must be a required build argument")
    if re.search(r"(?m)^ARG CW_BUNDLE_CACHE_NAME=", t):
        out.append("the bundle cache name has a default")
    if "PG_BUNDLE" in t or "/opt/pg-bundle" in t:
        out.append("PG_BUNDLE or /opt/pg-bundle is still named")
    if not re.search(r'chown worker:worker /home/worker/\.cache(\s|$)', f):
        out.append("the cache directory is not owned by the run user")
    for env in re.findall(r"(?m)^ENV\b(?:[^\n]*\\\n)*[^\n]*", t):   # an ENV statement with its continuation lines: no key starts with PG
        if re.search(r"(?:^ENV|\s)\s*PG\w*[= ]", env.replace("\\\n", " ")):
            out.append("a PG-prefixed ENV variable")
    ep = os.path.join(os.path.dirname(DF), "entrypoint.sh")
    if os.path.exists(ep) and re.search(r"(?m)^\s*export\s+PG\w*", read(ep)):
        out.append("a PG-prefixed export in the entrypoint")
    pgb = t[t.find("AS pgbuild"):t.find("FROM worker AS scan")]
    if not re.search(r"make -C /src/pg/contrib/pg_trgm PG_CONFIG=/opt/pg/bin/pg_config && make -C /src/pg/contrib/pg_trgm PG_CONFIG=/opt/pg/bin/pg_config install", pgb):
        out.append("pg_trgm is not built from the contrib directory")
    if re.search(r"(?mi)^\s*(CMD|ENTRYPOINT|EXPOSE)\b", f) or starts_server(f):
        out.append("a line starts a server or adds a port")
    users = re.findall(r"(?m)^USER (\S+)$", t)
    if not users or users[-1] != "worker":
        out.append("the run user is not worker")
    cp = f.find("cp -a /opt/pg/bin")
    if re.search(r"\b(curl|wget|apt-get)\b", f[cp:] if cp >= 0 else f):
        out.append("a download or install line after the copy")
    if re.search(r"(?m)^(ENV|ARG) .*(TOKEN|SECRET|PASSWORD|KEY)", f):
        out.append("a secret-looking variable")
    # the token service's files
    w = flat(t[t.find("AS worker"):t.find("FROM worker AS scan")])
    m = re.search(r"(?m)^COPY ((?:launcher/\S+ )+)/opt/cloud/launcher/$", w)
    got = sorted(x[len("launcher/"):] for x in m.group(1).split()) if m else []
    need = sorted(set(walk_imports()) - tolerated_all())
    if got != need:
        out.append("a module the token service imports is not shipped" if m and set(need) - set(got) else "the token service files were not copied")
    elif not re.search(r"chown -R root:root /opt/cloud && [^\n]*chmod 0644 /opt/cloud/launcher/\*\.py", w):
        out.append("the token service files are not root-owned 0644")
    for keep in ("COPY launcher/end_hook.py /usr/local/bin/cw-end-hook", "COPY launcher/token_mint.py /usr/local/bin/cw-token-mint"):
        if keep not in w:
            out.append("a line was changed: " + keep)
    return out


class T(unittest.TestCase):
    def test_real_file_clean(self):
        self.assertEqual(problems(read(DF)), [])

    def test_one_env_line_and_user(self):
        t = read(DF)
        self.assertEqual(len(re.findall(r"(?m)^ENV\b[^\n]*PG_BUNDLE", t)), 0)
        self.assertEqual(len(re.findall(r"(?m)^ARG CW_BUNDLE_CACHE_NAME$", final_stage(t))), 1)
        self.assertEqual(re.findall(r"(?m)^USER (\S+)$", t)[-1], "worker")

    def test_closure_is_shipped_and_copies_equal(self):
        got = walk_imports()
        self.assertIn("path_walk.py", got)
        bins = sorted(set(BIN_COPIES) | {n for n in got if got[n] == BIN and n not in tolerated_all()})
        for n in os.listdir(LAUNCHER):  # a launcher file sharing a bin/ name is either a listed copy or a different file
            if n.endswith(".py") and os.path.isfile(os.path.join(BIN, n)) and n not in BIN_COPIES:
                with open(os.path.join(BIN, n), "rb") as a, open(os.path.join(LAUNCHER, n), "rb") as b:
                    self.assertNotEqual(a.read(), b.read(), n + " is a copy of bin/ but is not in BIN_COPIES")
        self.assertEqual(copy_problems(BIN, LAUNCHER, bins), [])
        self.assertEqual(sorted(got), ["app_token.py", "cloud_worker.py", "path_walk.py", "registry.py", "run_record.py", "token_mint.py", "token_service.py", "wip_save.py"])

    def test_copy_mutant_fails_comparison(self):
        import tempfile
        with tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as l:
            for d in (b, l):
                with open(os.path.join(d, "x.py"), "wb") as f:
                    f.write(b"a = 1\n")
            self.assertEqual(copy_problems(b, l, ["x.py"]), [])  # control
            with open(os.path.join(l, "x.py"), "ab") as f:
                f.write(b" ")
            self.assertEqual(copy_problems(b, l, ["x.py"]), ["x.py copy differs"])
            os.remove(os.path.join(l, "x.py"))
            self.assertEqual(copy_problems(b, l, ["x.py"]), ["x.py copy is missing"])

    def test_registry_is_tolerated_not_shipped(self):
        self.assertNotIn("launcher/registry.py", flat(final_stage(read(DF))))
        self.assertEqual(problems(read(DF)), [])

    def test_missing_module_mutant_control_first(self):
        t = read(DF)
        self.assertEqual(problems(t), [])  # control
        m = self.mutate(" launcher/path_walk.py /opt/cloud/launcher/", " /opt/cloud/launcher/")
        self.assertIn("a module the token service imports is not shipped", problems(m))

    def test_server_start_mutants_control_first(self):
        t = read(DF)
        self.assertEqual(problems(t), [])  # control
        for cmd in ("/opt/pg/bin/pg_ctl start", "/usr/bin/postgres -D /x", "/opt/pg/bin/initdb -D /x",
                    "if true; then pg_ctl start; fi", "bash -c 'postgres -D /x'", "sudo -u pg pg_ctl start", "timeout 5 postgres", "echo hi & postgres", "run postgres -D /x", " RUN postgres -D /x", "RUN $(echo postgres) -D /x", "RUN <<EOF\npostgres -D /x\nEOF", "chmod 1 x & pg_ctl start",
                    "systemctl start x", "service postgresql start"):
            m = read(DF).rstrip("\n") + "\nRUN " + cmd + "\n"
            self.assertIn("a line starts a server", " ".join(problems(m)), cmd)
        for cmd in ("echo postgres", "chmod 0755 /opt/pg/bin/postgres", "cp -a /opt/pg/bin/initdb /x"):
            m = read(DF).rstrip("\n") + "\nRUN " + cmd + "\n"
            self.assertNotIn("a line starts a server", " ".join(problems(m)), cmd)

    def mutate(self, old, new):
        t = read(DF)
        self.assertEqual(t.count(old), 1, "the text to mutate must stand once")
        m = t.replace(old, new)
        self.assertNotEqual(m, t)
        return m

    def test_share_mutant_control_first(self):
        t = read(DF)
        self.assertEqual(problems(t), [])  # control
        m = self.mutate(' \\\n && cp -a /opt/pg/share "$l/share"', "")
        self.assertEqual(problems(m), ["the share directory was not copied"])

    def test_launcher_files_mutant(self):
        t = read(DF)
        self.assertEqual(problems(t), [])  # control
        i = t.index("COPY launcher/token_service.py")
        j = t.index("\n", i)
        m = t[:i] + t[j + 1:]
        self.assertIn("the token service files were not copied", problems(m))

    def test_other_mutants(self):
        t = read(DF)
        self.assertEqual(problems(t), [])
        for old, new, why in (
            ("ARG CW_BUNDLE_CACHE_NAME\n", "ARG CW_BUNDLE_CACHE_NAME=dummy\n", "the bundle cache name has a default"),
            ("ARG CW_BUNDLE_CACHE_NAME\n", "ARG CW_BUNDLE_CACHE_NAME\nENV PG_BUNDLE=/x\n", "a PG-prefixed ENV variable"),
            ("USER worker\n", "USER root\n", None),
            ('cp -a /opt/pg/bin /opt/pg/lib "$l/"', 'ln -s /opt/pg/bin "$l/bin"', None),
            (" && chown worker:worker /home/worker/.cache\n", " && chown worker:worker /home/worker/.cache\nRUN apt-get install -y gcc\n", None),
        ):
            if old == "USER worker\n":
                m = t[: t.rindex(old)] + new
            else:
                m = self.mutate(old, new)
            self.assertTrue(problems(m), old)
            if why:
                self.assertIn(why, problems(m))


if __name__ == "__main__":
    unittest.main()

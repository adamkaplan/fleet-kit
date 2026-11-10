"""Ask #2329: the final stage builds /opt/pg-bundle from /opt/pg with copies (the layout scripts/dev/ephemeral-pg.sh reads through
PG_BUNDLE), sets PG_BUNDLE, starts no server, keeps the non-root run user; and the image carries the token service's launcher files.
Offline: reads the Dockerfile only. The image is not built here. Scenarios S2, S3, S4."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DF = os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "Dockerfile")

TOKEN_FILES = ("token_service.py", "app_token.py", "token_mint.py", "cloud_worker.py", "run_record.py", "wip_save.py")


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


def problems(t):
    out = []
    f = final_stage(t)
    if not f:
        return ["no final stage"]
    for name, pat in (
        ("bin and lib", r'cp -a /opt/pg/bin /opt/pg/lib "\$l/"'),
        ("the bundle's lib directory", r'l="\$b/usr/lib/postgresql/16"'),
        ("the bundle root", r"b=/opt/pg-bundle;"),
        ("the extension directory", r'cp -a [^\n]*vector--\*\.sql[^\n]*pgmq--\*\.sql "\$e/"'),
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
    if len(re.findall(r"(?m)^ENV PG_BUNDLE=/opt/pg-bundle$", f)) != 1:
        out.append("ENV PG_BUNDLE must appear once")
    if re.search(r"(?m)^(CMD|ENTRYPOINT|EXPOSE)\b", f) or re.search(r"pg_ctl|initdb|\bpostgres\b|systemctl|\bservice\b", re.sub(r"/opt/pg\S*|usr/\S*|postgresql\S*", "", f)):
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
    if got != sorted(TOKEN_FILES):
        out.append("the token service files were not copied")
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
        self.assertEqual(len(re.findall(r"(?m)^ENV PG_BUNDLE=/opt/pg-bundle$", final_stage(t))), 1)
        self.assertEqual(re.findall(r"(?m)^USER (\S+)$", t)[-1], "worker")

    def test_token_files_exist_and_cover_imports(self):
        d = os.path.join(os.path.dirname(os.path.dirname(HERE)), "launcher")
        for n in TOKEN_FILES:
            self.assertTrue(os.path.isfile(os.path.join(d, n)), n)
        # every launcher module imported at the top by the set is itself in the set
        have = {n[:-3] for n in TOKEN_FILES}
        local = {n[:-3] for n in os.listdir(d) if n.endswith(".py")}
        for n in TOKEN_FILES:
            for mod in re.findall(r"(?m)^(?:import|from) (\w+)", read(os.path.join(d, n))):
                if mod in local:
                    self.assertIn(mod, have, n + " imports " + mod)

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
            ("ENV \\\n  PG_BUNDLE=/opt/pg-bundle\n", "", "ENV PG_BUNDLE must appear once"),
            ("USER worker\n", "USER root\n", None),
            ('cp -a /opt/pg/bin /opt/pg/lib "$l/"', 'ln -s /opt/pg/bin "$l/bin"', None),
            ("ENV \\\n  PG_BUNDLE=/opt/pg-bundle\n", "ENV \\\n  PG_BUNDLE=/opt/pg-bundle\nRUN apt-get install -y gcc\n", None),
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

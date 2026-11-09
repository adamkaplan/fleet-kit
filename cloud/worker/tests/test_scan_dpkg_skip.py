"""The secret scan skips only UNMODIFIED files of installed distro packages (dpkg list AND md5sums AND the file's md5),
in --dir and in --archive; everything else is scanned; an unusable dpkg database means nothing is skipped (fail closed).
Each rule has a control first and a mutant (a changed copy of the scanner) that must fail with an anchored text.
Offline; writes only under the temp dir. The key-like strings are assembled at run time."""
import hashlib, io, json, os, shutil, stat, subprocess, tarfile, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
WORKER = os.path.join(ROOT, "cloud/worker")
KEY = ("AS" + "IA" + "ABCDEFGHIJKLMNOP").encode()
BODY = b"table of names\n" + KEY + b"\nend\n"
INFO = "var/lib/dpkg/info"


def md5(b):
    return hashlib.md5(b).hexdigest()


def run(args, worker=WORKER, env=None):
    e = dict(os.environ, **(env or {}))
    e.pop("SCAN_ALLOWLIST", None)
    p = subprocess.run(["bash", os.path.join(worker, "scan-secrets.sh")] + args, capture_output=True, text=True, env=e)
    return p.returncode, p.stdout, p.stderr


def put(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def db(root, pkg, files, sums=None, listed=None):
    """files: {path-without-slash: bytes already written}; the list names `listed` (default: files), md5sums `sums`."""
    listed = list(files) if listed is None else listed
    put(root, f"{INFO}/{pkg}.list", ("/.\n/usr\n" + "".join("/" + f + "\n" for f in listed)).encode())
    if sums is None:
        sums = {f: md5(d) for f, d in files.items()}
    put(root, f"{INFO}/{pkg}.md5sums", "".join(f"{m}  {f}\n" for f, m in sums.items()).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dpkgskip-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "root")
        os.makedirs(self.d)

    def scan(self, **kw):
        return run(["--dir", self.d], **kw)


class DirRules(Base):
    F = "usr/lib/x/libfoo.so"

    def test_control_unmodified_owned_file_is_not_a_hit_and_counted(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        rc, out, err = self.scan()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("dpkg: 1 unmodified files of installed distro packages not scanned", out.splitlines())
        self.assertIn("scan-secrets: 0 hits", out.splitlines())

    def test_modified_owned_file_is_a_hit(self):
        put(self.d, self.F, BODY + b"x")
        db(self.d, "foo", {self.F: BODY})
        rc, out, _ = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("usr/lib/x/libfoo.so: aws-access-key", out.splitlines())

    def test_file_no_package_owns_is_a_hit(self):
        put(self.d, self.F, BODY)
        put(self.d, "opt/ours/f", BODY)
        db(self.d, "foo", {self.F: BODY})
        rc, out, _ = self.scan()
        self.assertEqual(rc, 1)
        self.assertEqual([l for l in out.splitlines() if l.endswith(": aws-access-key")], ["opt/ours/f: aws-access-key"])

    def test_md5sums_without_list_entry_is_a_hit(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY}, listed=[])
        self.assertEqual(self.scan()[0], 1)

    def test_list_entry_without_md5sums_is_a_hit(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY}, sums={})
        self.assertEqual(self.scan()[0], 1)

    def test_other_packages_md5sums_does_not_vouch(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY}, sums={})
        put(self.d, f"{INFO}/bar.list", b"/.\n")
        put(self.d, f"{INFO}/bar.md5sums", f"{md5(BODY)}  {self.F}\n".encode())
        self.assertEqual(self.scan()[0], 1)

    def test_database_missing_fails_closed_with_a_stderr_line(self):
        put(self.d, self.F, BODY)
        rc, out, err = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("dpkg database not usable", err)
        self.assertIn("skip set EMPTY, every file is scanned", err)

    def test_malformed_md5sums_fails_closed_even_for_the_good_line(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        with open(os.path.join(self.d, INFO, "foo.md5sums"), "ab") as f:
            f.write(b"not a checksum line\n")
        rc, out, err = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("malformed md5sums of foo", err)

    def test_malformed_list_fails_closed(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        put(self.d, f"{INFO}/foo.list", b"relative/path\n")
        rc, _, err = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("malformed list of foo", err)

    def test_unreadable_md5sums_fails_closed(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        p = os.path.join(self.d, INFO, "foo.md5sums")
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o644)
        rc, _, err = self.scan()
        self.assertEqual(rc, 2)  # the unreadable file is itself "not scanned": never a pass
        self.assertIn("dpkg database not usable", err)

    def test_dotdot_path_in_list_and_md5sums_is_not_skipped(self):
        put(self.d, "f", BODY)
        db(self.d, "foo", {}, sums={"usr/../f": md5(BODY)}, listed=["usr/../f"])
        self.assertEqual(self.scan()[0], 1)

    def test_symlink_escape_is_not_skipped(self):
        put(self.d, "f", BODY)
        os.symlink("..", os.path.join(self.d, "esc"))
        db(self.d, "foo", {}, sums={"esc/f": md5(BODY)}, listed=["esc/f"])
        self.assertEqual(self.scan()[0], 1)

    def test_control_inside_symlink_is_resolved(self):
        # usr-merge: the list says /lib/x/f, the file is in /usr/lib/x/f and /lib is a link to usr/lib
        put(self.d, "usr/lib/x/f", BODY)
        os.symlink("usr/lib", os.path.join(self.d, "lib"))
        db(self.d, "foo", {}, sums={"lib/x/f": md5(BODY)}, listed=["lib/x/f"])
        rc, out, _ = self.scan()
        self.assertEqual(rc, 0, out)

    def test_hardlink_copy_of_a_secret_is_scanned_by_its_own_md5(self):
        # a second name for a modified content is not owned: only the md5 of the content at the listed path counts
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        put(self.d, "opt/ours/g", BODY + b"y")
        os.link(os.path.join(self.d, "opt/ours/g"), os.path.join(self.d, "opt/ours/h"))
        rc, out, _ = self.scan()
        self.assertEqual(rc, 1)
        self.assertEqual(sorted(l for l in out.splitlines() if l.endswith("aws-access-key")),
                         ["opt/ours/g: aws-access-key", "opt/ours/h: aws-access-key"])

    def test_credential_file_by_name_is_still_a_hit(self):
        data = b"machine example.org login u\n"
        put(self.d, "usr/share/x/.netrc", data)
        db(self.d, "foo", {"usr/share/x/.netrc": data})
        rc, out, _ = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("usr/share/x/.netrc: credential-file", out.splitlines())

    def test_the_database_files_are_scanned_too(self):
        put(self.d, self.F, BODY)
        db(self.d, "foo", {self.F: BODY})
        with open(os.path.join(self.d, INFO, "foo.list"), "ab") as f:
            f.write(b"# " + KEY + b"\n")
        rc, _, err = self.scan()
        self.assertEqual(rc, 1)  # the list now has a non-absolute line: malformed, so nothing is skipped either


class MetaStillScanned(Base):
    def stub(self, env, hist):
        b = os.path.join(self.tmp, "stub")
        os.makedirs(b)
        p = os.path.join(b, "docker")
        with open(p, "w") as f:
            f.write('#!/bin/sh\ncase "$1 $2" in\n  "image inspect") printf \'{"Env":["PATH=/bin","%s"]}\\n\' "$STUB_ENV" ;;\n  "history --no-trunc") printf \'/bin/sh -c %s\\n\' "$STUB_HIST" ;;\n  *) exit 1 ;;\nesac\n')
        os.chmod(p, 0o755)
        return {"PATH": b + ":" + os.environ["PATH"], "STUB_ENV": env, "STUB_HIST": hist}

    def test_control_clean_config_and_history(self):
        self.assertEqual(run(["--meta", "any:img"], env=self.stub("A=b", "true"))[0], 0)

    def test_key_in_env_is_a_hit(self):
        rc, out, _ = run(["--meta", "any:img"], env=self.stub("AWS=" + KEY.decode(), "true"))
        self.assertEqual(rc, 1)
        self.assertIn("config.json: aws-access-key", out.splitlines())

    def test_key_in_history_is_a_hit(self):
        rc, out, _ = run(["--meta", "any:img"], env=self.stub("A=b", "echo " + KEY.decode()))
        self.assertEqual(rc, 1)
        self.assertIn("history.txt: aws-access-key", out.splitlines())


def layer(entries):
    """entries: list of (name, bytes | ('sym', target) | ('dir',))"""
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name, v in entries:
            ti = tarfile.TarInfo(name)
            if isinstance(v, tuple) and v[0] == "sym":
                ti.type = tarfile.SYMTYPE
                ti.linkname = v[1]
                t.addfile(ti)
            elif isinstance(v, tuple):
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.size = len(v)
                t.addfile(ti, io.BytesIO(v))
    return b.getvalue()


def archive(path, layers, env="A=b"):
    with tarfile.open(path, "w") as t:
        names = []

        def add(n, d):
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
        for i, ents in enumerate(layers, 1):
            n = f"l{i}/layer.tar"
            add(n, layer(ents))
            names.append(n)
        add("cfg.json", json.dumps({"config": {"Env": [env]}, "history": [{"created_by": "true"}]}).encode())
        add("manifest.json", json.dumps([{"Config": "cfg.json", "Layers": names}]).encode())


def dbents(pkg, files, sums=None):
    sums = {f: md5(d) for f, d in files.items()} if sums is None else sums
    return [(f"{INFO}/{pkg}.list", ("/.\n" + "".join("/" + f + "\n" for f in files)).encode()),
            (f"{INFO}/{pkg}.md5sums", "".join(f"{m}  {f}\n" for f, m in sums.items()).encode())]


F = "usr/lib/x/libfoo.so"


class ArchiveRules(Base):
    def scan_layers(self, layers, env="A=b", **kw):
        a = os.path.join(self.tmp, "i.tar")
        archive(a, layers, env)
        return run(["--archive", a], **kw)

    def test_control_unmodified_owned_file_is_skipped_and_counted(self):
        rc, out, err = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY)]])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("dpkg: 1 unmodified files of installed distro packages not scanned", out.splitlines())

    def test_database_in_an_earlier_layer_and_file_in_a_later_one(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}), [(F, BODY)], [("app/a", b"x")]])
        self.assertEqual(rc, 0, out)

    def test_modified_file_is_a_hit(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY + b"x")]])
        self.assertEqual(rc, 1)
        self.assertIn("layer 1: /" + F + ": aws-access-key", out.splitlines())

    def test_unowned_file_is_a_hit(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY), ("opt/ours/f", BODY)]])
        self.assertEqual(rc, 1)
        self.assertIn("layer 1: /opt/ours/f: aws-access-key", out.splitlines())

    def test_database_missing_fails_closed(self):
        rc, out, err = self.scan_layers([[(F, BODY)]])
        self.assertEqual(rc, 1)
        self.assertIn("dpkg database not usable", err)
        self.assertIn("skip set EMPTY, every file is scanned", err)

    def test_database_deleted_by_a_whiteout_fails_closed(self):
        rc, out, err = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY)], [(INFO + "/.wh.foo.list", b""), (INFO + "/.wh.foo.md5sums", b"")]])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("layer 1: /" + F + ": aws-access-key", out.splitlines())

    def test_opaque_directory_deletes_the_database(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY)], [(INFO + "/.wh..wh..opq", b"")]])
        self.assertEqual(rc, 1)

    def test_later_layer_md5sums_wins(self):
        later = dbents("foo", {F: BODY}, sums={F: "0" * 32})
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY)], later])
        self.assertEqual(rc, 1)
        self.assertIn("layer 1: /" + F + ": aws-access-key", out.splitlines())

    def test_file_modified_in_a_later_layer_is_a_hit_in_that_layer_and_the_deleted_one_is_still_seen(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY + b"1")], [(F, BODY)]])
        self.assertEqual(rc, 1)
        self.assertIn("layer 1: /" + F + ": aws-access-key", out.splitlines())
        self.assertNotIn("layer 2: /" + F + ": aws-access-key", out.splitlines())

    def test_dotdot_and_symlink_escape_are_not_skipped(self):
        a = [(f"{INFO}/foo.list", b"/.\n/usr/../f\n"), (f"{INFO}/foo.md5sums", f"{md5(BODY)}  usr/../f\n".encode()), ("f", BODY)]
        self.assertEqual(self.scan_layers([a])[0], 1)
        b = [(f"{INFO}/foo.list", b"/.\n/esc/f\n"), (f"{INFO}/foo.md5sums", f"{md5(BODY)}  esc/f\n".encode()), ("esc", ("sym", "..")), ("f", BODY)]
        self.assertEqual(self.scan_layers([b])[0], 1)

    def test_control_usr_merge_symlink_is_resolved(self):
        a = [(f"{INFO}/foo.list", b"/.\n/lib/x/f\n"), (f"{INFO}/foo.md5sums", f"{md5(BODY)}  lib/x/f\n".encode()),
             ("lib", ("sym", "usr/lib")), ("usr/lib/x/f", BODY)]
        rc, out, _ = self.scan_layers([a])
        self.assertEqual(rc, 0, out)

    def test_config_env_is_still_scanned(self):
        rc, out, _ = self.scan_layers([dbents("foo", {F: BODY}) + [(F, BODY)]], env="K=" + KEY.decode())
        self.assertEqual(rc, 1)
        self.assertIn("config: aws-access-key", out.splitlines())

    def test_unreadable_layer_is_exit_2(self):
        a = os.path.join(self.tmp, "i.tar")
        archive(a, [dbents("foo", {F: BODY}) + [(F, BODY)]])
        with tarfile.open(a) as t:
            members = {m.name: t.extractfile(m).read() for m in t.getmembers()}
        members["l1/layer.tar"] = members["l1/layer.tar"][:300]
        with tarfile.open(a, "w") as t:
            for n, d in members.items():
                ti = tarfile.TarInfo(n)
                ti.size = len(d)
                t.addfile(ti, io.BytesIO(d))
        rc, out, err = run(["--archive", a])
        self.assertEqual(rc, 2)
        self.assertIn("not scanned", err)


# ---- mutants: a changed copy of the scanner must fail the scenario with the text the rule produces -------------------

class Mutants(Base):
    def mutant(self, fname, old, new):
        w = os.path.join(self.tmp, "w")
        shutil.copytree(WORKER, w, ignore=shutil.ignore_patterns("tests", "__pycache__", "bin", "agents"))
        p = os.path.join(w, fname)
        with open(p) as f:
            s = f.read()
        self.assertEqual(s.count(old), 1, "anchor text not found exactly once: " + old)
        with open(p, "w") as f:
            f.write(s.replace(old, new))
        return w

    def dirscan(self, w, setup):
        setup(self.d)
        return run(["--dir", self.d], worker=w)

    def check(self, fname, old, new, setup, control_rc, mutant_rc):
        self.assertEqual(self.dirscan(WORKER, setup)[0], control_rc)  # control first, on the real scanner
        shutil.rmtree(self.d)
        os.makedirs(self.d)
        w = self.mutant(fname, old, new)
        self.assertEqual(self.dirscan(w, setup)[0], mutant_rc)

    def test_mutant_md5_of_the_file_not_compared(self):
        def setup(d):
            put(d, F, BODY + b"x")
            db(d, "foo", {F: BODY})
        self.check("scan-files.py", "if m5 is not None and m5.hexdigest() in skipmap[shown]:", "if m5 is not None:", setup, 1, 0)

    def test_mutant_list_not_required(self):
        def setup(d):
            put(d, F, BODY)
            db(d, "foo", {F: BODY}, listed=[])
        self.check("scan-files.py", 'if path not in listed or ".." in path.split("/") or m.group(2).startswith("\\\\"):',
                   'if ".." in path.split("/") or m.group(2).startswith("\\\\"):', setup, 1, 0)

    def test_mutant_dotdot_allowed(self):
        def setup(d):
            put(d, "f", BODY)
            db(d, "foo", {}, sums={"usr/../f": md5(BODY)}, listed=["usr/../f"])
        self.check("scan-files.py", 'or ".." in path.split("/") or', "or False or", setup, 1, 0)

    def test_mutant_escape_clamped_at_the_root(self):
        def setup(d):
            put(d, "f", BODY)
            os.symlink("..", os.path.join(d, "esc"))
            db(d, "foo", {}, sums={"esc/f": md5(BODY)}, listed=["esc/f"])
        self.check("scan-files.py", 'if not cur:\n                return None\n            cur.pop()', "if not cur:\n                continue\n            cur.pop()", setup, 1, 0)

    def test_mutant_symlink_not_followed(self):
        def setup(d):
            put(d, "usr/lib/x/f", BODY)
            os.symlink("usr/lib", os.path.join(d, "lib"))
            db(d, "foo", {}, sums={"lib/x/f": md5(BODY)}, listed=["lib/x/f"])
        self.check("scan-files.py", 't = readlink("/" + "/".join(cur))\n        if t is None:', 't = None\n        if t is None:', setup, 0, 1)

    def test_mutant_malformed_md5sums_tolerated(self):
        def setup(d):
            put(d, F, BODY)
            db(d, "foo", {F: BODY})
            with open(os.path.join(d, INFO, "foo.md5sums"), "ab") as f:
                f.write(b"junk\n")
        self.check("scan-files.py", 'raise DpkgError(f"malformed md5sums of {pkg}")', "continue", setup, 1, 0)

    def test_mutant_fail_closed_message_removed(self):
        w = self.mutant("scan-files.py", "skip set EMPTY, every file is scanned", "ok")
        put(self.d, F, BODY)
        rc, out, err = run(["--dir", self.d], worker=w)
        self.assertNotIn("skip set EMPTY", err)
        self.assertEqual(run(["--dir", self.d])[2].count("skip set EMPTY"), 1)

    def test_mutant_archive_md5_not_compared(self):
        a = os.path.join(self.tmp, "i.tar")
        archive(a, [dbents("foo", {F: BODY}) + [(F, BODY + b"x")]])
        self.assertEqual(run(["--archive", a])[0], 1)
        w = self.mutant("scan-layers.py", "return all(md5hex in skipmaps[i].get(path, ()) for i in layer_imgs[lp])",
                        "return all(path in skipmaps[i] for i in layer_imgs[lp])")
        self.assertEqual(run(["--archive", a], worker=w)[0], 0)

    def test_mutant_archive_whiteout_ignored(self):
        a = os.path.join(self.tmp, "i.tar")
        archive(a, [dbents("foo", {F: BODY}) + [(F, BODY)], [(INFO + "/.wh.foo.list", b""), (INFO + "/.wh.foo.md5sums", b"")]])
        self.assertEqual(run(["--archive", a])[0], 1)
        w = self.mutant("scan-layers.py", 'for p in info["wh"]:\n            drop(p, True)', 'for p in info["wh"]:\n            pass')
        self.assertEqual(run(["--archive", a], worker=w)[0], 0)

    def test_mutant_archive_first_layer_wins(self):
        a = os.path.join(self.tmp, "i.tar")
        archive(a, [dbents("foo", {F: BODY}) + [(F, BODY)], dbents("foo", {F: BODY}, sums={F: "0" * 32})])
        self.assertEqual(run(["--archive", a])[0], 1)
        w = self.mutant("scan-layers.py", "    for info in layer_infos:\n", "    for info in reversed(layer_infos):\n")
        self.assertEqual(run(["--archive", a], worker=w)[0], 0)

    def test_mutant_archive_symlink_escape_clamped(self):
        a = os.path.join(self.tmp, "i.tar")
        b = [(f"{INFO}/foo.list", b"/.\n/esc/f\n"), (f"{INFO}/foo.md5sums", f"{md5(BODY)}  esc/f\n".encode()), ("esc", ("sym", "..")), ("f", BODY)]
        archive(a, [b])
        self.assertEqual(run(["--archive", a])[0], 1)
        w = self.mutant("scan-files.py", 'if not cur:\n                return None\n            cur.pop()', "if not cur:\n                continue\n            cur.pop()")
        self.assertEqual(run(["--archive", a], worker=w)[0], 0)


if __name__ == "__main__":
    unittest.main()

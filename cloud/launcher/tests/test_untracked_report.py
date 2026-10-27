#!/usr/bin/env python3
"""Ask #1289: the end hook and its --prestop path name the untracked files they saved (S1-S6, S9, S10, S12, U2). Offline: a bare
repository and a clone under the temporary directory. The hook module is driven in process, so the real code path runs; each mutant is
the hook's own source with one change, loaded as a module, and must fail at least one case."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
KIT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import end_hook  # noqa: E402

GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else end_hook.GIT_CANDIDATES[0]
REPO = "acme-org/widget-service"
CONTENT = "CONTENT-MARKER-never-in-the-line"
TODAYS_KEYS = {"pushed", "wip_branch", "skipped"}


def sh(*a, cwd=None):
    return subprocess.run((GIT,) + a, cwd=cwd, capture_output=True, text=True, check=True).stdout


def load(source, name="end_hook_variant"):
    mod = types.ModuleType(name)
    mod.__file__ = os.path.join(LAUNCHER, "end_hook.py")
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def mutant(old, new):
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        src = f.read()
    assert src.count(old) == 1, old
    return load(src.replace(old, new))


class Tree:
    """A bare origin, a clone with one pushed commit, and (for the prestop path) a fake credential helper."""
    def __init__(self, prestop):
        self.t = tempfile.mkdtemp()
        self.prestop = prestop
        self.origin = os.path.join(self.t, REPO + ".git")
        self.work = os.path.join(self.t, "repo")
        os.makedirs(os.path.dirname(self.origin))
        sh("init", "-q", "--bare", "-b", "main", self.origin)
        sh("clone", "-q", self.origin, self.work)
        for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
            sh("-C", self.work, "config", k, v)
        self.put("a.txt", "a")
        sh("-C", self.work, "add", "-A")
        sh("-C", self.work, "commit", "-qm", "base")
        sh("-C", self.work, "push", "-q", "origin", "HEAD:main")
        if prestop:
            sh("-C", self.work, "remote", "set-url", "origin", "https://github.com/%s.git" % REPO)
            self.helper_dir = os.path.join(self.t, "helper")
            os.makedirs(self.helper_dir)
            helper = os.path.join(self.helper_dir, "git-credential-cw")
            with open(helper, "w") as f:
                f.write("#!/bin/sh\ncat > /dev/null\nprintf 'username=x\\npassword=pw\\n'\n")
            os.chmod(helper, 0o755)

    def put(self, name, content=CONTENT):
        path = os.path.join(self.work, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def run_hook(self, mod):
        """The line the hook printed, as text, and its parsed JSON (None for the prestop line)."""
        out = io.StringIO()
        if not self.prestop:
            with contextlib.redirect_stdout(out):
                res = mod.run(self.work, "w1")
            return res
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CW_WORKDIR": self.work, "CW_WORKER": "w1", "CW_REPO": REPO}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mod, "HOST_URL", "file://%s/" % self.t), \
                mock.patch.object(mod, "HELPER_DIR", self.helper_dir), mock.patch.object(mod, "GIT_BIN", GIT), \
                contextlib.redirect_stdout(out):
            self.rc = mod.prestop_main()
        line = out.getvalue()
        if "; {" not in line:
            return {"line": line, "pushed": False}
        return dict(json.loads(line.split("; ", 1)[1]), line=line, pushed=True, wip_branch=line.split("saved work to ", 1)[1].split(";")[0])

    def wip_files(self):
        p = subprocess.run((GIT, "-C", self.origin, "ls-tree", "-r", "-z", "--name-only", "w1/wip"), capture_output=True, text=True)
        return sorted(n for n in p.stdout.split("\0") if n)

    def close(self):
        shutil.rmtree(self.t, True)


class Cases:
    """The seven cases of S9 plus U2, run against one hook module and one path (end or prestop). Raises AssertionError on the first
    failure; a mutant must make at least one fail."""
    def __init__(self, tc, mod, prestop):
        self.tc, self.mod, self.prestop = tc, mod, prestop

    def tree(self):
        t = Tree(self.prestop)
        self.tc.addCleanup(t.close)
        return t

    def run(self, t):
        return t.run_hook(self.mod)

    def one_stray_file(self):
        t = self.tree(); t.put("stray.txt")
        res = self.run(t)
        self.tc.assertEqual(res["untracked"], ["stray.txt"])
        self.tc.assertEqual(res["untracked_more"], 0)
        self.tc.assertIn("stray.txt", t.wip_files())
        self.tc.assertNotIn(CONTENT, json.dumps(res))

    def many_stray_files(self):
        t = self.tree()
        names = ["f%02d.txt" % i for i in range(12)]
        for n in names:
            t.put(n)
        res = self.run(t)
        self.tc.assertEqual(res["untracked"], names[:10])
        self.tc.assertEqual(res["untracked_more"], 2)
        self.tc.assertEqual(len(res["untracked"]), 10)
        self.tc.assertTrue(set(names) <= set(t.wip_files()))
        self.tc.assertNotIn(CONTENT, res.get("line", "") + json.dumps(res))

    def none(self):
        t = self.tree(); t.put("a.txt", "changed")   # a tracked file changed: saved, but nothing is untracked
        res = self.run(t)
        self.tc.assertEqual(res["untracked"], [])
        self.tc.assertEqual(res["untracked_more"], 0)
        if not self.prestop:
            self.tc.assertEqual(res, {"pushed": True, "wip_branch": "w1/wip", "skipped": [], "untracked": [], "untracked_more": 0})
        clean = self.tree()
        res = self.run(clean)
        if not self.prestop:
            self.tc.assertEqual(res, {"pushed": False, "wip_branch": None, "skipped": [], "untracked": [], "untracked_more": 0})

    def spaces_and_a_newline(self):
        t = self.tree(); t.put("with space.txt"); t.put("line\nbreak.txt")
        res = self.run(t)
        self.tc.assertEqual(res["untracked"], sorted(["with space.txt", "line\nbreak.txt"]))
        self.tc.assertTrue({"with space.txt", "line\nbreak.txt"} <= set(t.wip_files()))
        if self.prestop:
            self.tc.assertEqual(len(res["line"].strip().split("\n")), 1)   # the newline is escaped: still one line

    def secret_looking_file(self):
        t = self.tree(); t.put(".env"); t.put("stray.txt"); t.put("sub/server.pem")
        res = self.run(t)
        self.tc.assertEqual(res["untracked"], ["stray.txt"])
        self.tc.assertFalse([n for n in res["untracked"] if ".env" in n or "pem" in n])
        self.tc.assertEqual(t.wip_files(), ["a.txt", "stray.txt"])
        if not self.prestop:
            self.tc.assertEqual(res["skipped"], [".env", "sub/server.pem"])
        self.tc.assertNotIn(".env", res.get("line", "").replace("saved work", ""))

    def unchanged_save(self):
        t = self.tree(); t.put("stray.txt"); t.put("a.txt", "changed"); t.put(".env")
        res = self.run(t)
        self.tc.assertEqual(t.wip_files(), ["a.txt", "stray.txt"])
        self.tc.assertEqual(sh("-C", t.origin, "rev-parse", "w1/wip").strip(), sh("-C", t.work, "rev-parse", "HEAD").strip())
        self.tc.assertEqual(sh("-C", t.work, "status", "--porcelain").strip(), "?? .env")
        if not self.prestop:
            self.tc.assertEqual({k: v for k, v in res.items() if k in TODAYS_KEYS}, {"pushed": True, "wip_branch": "w1/wip", "skipped": [".env"]})
            self.tc.assertEqual(set(res), TODAYS_KEYS | {"untracked", "untracked_more"})
        else:
            self.tc.assertTrue(res["line"].startswith("cw-prestop: saved work to w1/wip; "))

    def order_and_cap_of_the_report(self):
        entries = ["?? %s" % n for n in ("z", "b", "a", "m", "c", "k", "d", "e", "f", "g", "h", "i")] + [" M tracked", "?? .env"]
        got = self.mod.untracked_report(entries)
        self.tc.assertEqual(got, {"untracked": list("abcdefghik"), "untracked_more": 2})

    ALL = ("order_and_cap_of_the_report", "one_stray_file", "many_stray_files", "none", "spaces_and_a_newline", "secret_looking_file", "unchanged_save")

    def failures(self):
        bad = []
        for name in self.ALL:
            try:
                getattr(self, name)()
            except AssertionError:
                bad.append(name)
            except Exception as e:   # a mutant that breaks the hook outright also fails the case
                bad.append("%s: %s" % (name, type(e).__name__))
        return bad


class Report(unittest.TestCase):
    def test_every_case_holds_on_the_end_path(self):
        for name in Cases.ALL:
            with self.subTest(name):
                getattr(Cases(self, end_hook, False), name)()

    def test_every_case_holds_on_the_prestop_path(self):
        for name in Cases.ALL:
            with self.subTest(name):
                getattr(Cases(self, end_hook, True), name)()

    def test_a_secret_looking_path_is_never_listed_as_saved(self):   # U2
        t = Tree(False); self.addCleanup(t.close)
        t.put(".env"); t.put("deploy.key"); t.put("ok.txt")
        res = t.run_hook(end_hook)
        self.assertEqual(res["skipped"], [".env", "deploy.key"])
        self.assertEqual(res["untracked"], ["ok.txt"])
        self.assertEqual(t.wip_files(), ["a.txt", "ok.txt"])

    def test_the_cap_is_ten_and_the_count_is_the_rest(self):
        entries = ["?? p%02d" % i for i in range(23)] + [" M tracked", "?? .env"]
        got = end_hook.untracked_report(entries)
        self.assertEqual(got["untracked"], ["p%02d" % i for i in range(10)])
        self.assertEqual(got["untracked_more"], 13)


MUTANTS = {
    "names nothing": ("    return {\"untracked\": names[:MAX_UNTRACKED], \"untracked_more\": max(0, len(names) - MAX_UNTRACKED)}",
                      "    return {\"untracked\": [], \"untracked_more\": 0}"),
    "names a secret path as saved": (" and not is_secret(e[3:]))", ")"),
    "drops the cap": ("\"untracked\": names[:MAX_UNTRACKED]", "\"untracked\": names"),
    "no count of the rest": ("max(0, len(names) - MAX_UNTRACKED)}", "0}"),
    "unsorted": ("names = sorted(e[3:] for e in", "names = list(e[3:] for e in"),
}


class Mutants(unittest.TestCase):
    def test_each_mutant_fails_a_case_on_both_paths(self):
        for label, (old, new) in MUTANTS.items():
            mod = mutant(old, new)
            for prestop in (False, True):
                with self.subTest(label, prestop=prestop):
                    self.assertTrue(Cases(self, mod, prestop).failures(), "survived: %s" % label)

    def test_the_real_hook_fails_no_case(self):
        for prestop in (False, True):
            self.assertEqual(Cases(self, end_hook, prestop).failures(), [])


class Docs(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(KIT, *p)) as f:
            return f.read()

    def test_the_cause_is_stated_first_in_the_hook_and_the_documents(self):   # S1
        for doc in (("cloud", "launcher", "end_hook.py"), ("cloud", "README.md")):
            self.assertTrue("names only the skipped secret-looking paths" in self.read(*doc), "/".join(doc))

    def test_the_documents_state_what_the_line_means(self):   # S6, S12
        for doc in (("cloud", "README.md"), ("docs", "cloud-worker-interface.md")):
            text = self.read(*doc)
            for phrase in ("names all untracked paths saved", "untracked_more", "never content", "skipped"):
                self.assertTrue(phrase in text, "%s: %s" % ("/".join(doc), phrase))


if __name__ == "__main__":
    unittest.main()

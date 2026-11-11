"""#1200 scenario S14: the pod's entrypoint exports CW_PR_RECORD (a file inside the worker-private temporary directory) and the client writes the
number of a pull request it creates there (mode 0600, never through a symlink). The record is a log: nothing refuses on it. Offline; the token is synthetic."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pr_edit as P  # noqa: E402

WORKER = os.path.dirname(HERE)
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
CW_GH = os.path.join(WORKER, "cw-gh")
EXPORT_LINE = 'export CW_PR_RECORD="$TMP/pr-record/numbers"\n'
CREATED = 12


class Create(P.H):
    """The stub: a POST to pulls creates number 12 (head: the pod's own branch); """
    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(ln)
        P.H.seen.append(("POST", self.path))
        self.reply(201, {"number": CREATED, "html_url": "https://example.invalid/pull/%d" % CREATED})


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Create)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.addClassCleanup(cls.srv.shutdown)

    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        self.addCleanup(t.cleanup)
        self.d = t.name
        self.run_dir, self.tmpfs, self.share, self.work = [os.path.join(self.d, x) for x in ("run", "tmpfs", "share", "work")]
        for x in (self.run_dir, self.tmpfs, self.share, self.work):
            os.mkdir(x)
        for src, name in (("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
            shutil.copy(os.path.join(WORKER, src), os.path.join(self.share, name))
        subprocess.run(["git", "init", "-q", self.work], check=True)
        subprocess.run(["git", "-C", self.work, "remote", "add", "origin", "https://github.com/%s.git" % P.REPO], check=True)
        self.cw = P.CW_GH

    def env(self, **extra):
        e = {"PATH": "/usr/bin:/bin", "HOME": self.d, "WORKER_DOCKER": "0", "CW_RUN_DIR": self.run_dir, "CW_TMP_DIR": self.tmpfs,
             "CW_SHARE_DIR": self.share, "GIT_CONFIG_NOSYSTEM": "1", "CW_GH_API_BASE": self.base, "GH_TOKEN": P.TOKEN, "CW_BRANCH": P.BRANCH}
        e.update(extra)
        return e

    def start(self, *cmd, entry=ENTRYPOINT, **extra):
        return subprocess.run(["sh", entry] + list(cmd), env=self.env(**extra), capture_output=True, text=True, cwd=self.work, timeout=60, stdin=subprocess.DEVNULL)

    def record_path(self):
        r = self.start("sh", "-c", 'printf %s "${CW_PR_RECORD:-}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def create(self, entry=ENTRYPOINT, **extra):
        return self.start("python3", self.cw, "pr", "create", "--title", "t", "--body", "b", "--head", P.BRANCH, "--base", "main", entry=entry, **extra)



    # S14


    # mutation: an entrypoint that exports nothing

    # the symlink at the record path is not followed
    def test_a_symlink_at_the_record_path_is_not_followed(self):
        outside = os.path.join(self.d, "outside")
        open(outside, "w").write("")
        link = os.path.join(self.d, "numbers")
        for target in (outside, os.path.join(self.d, "missing")):
            with self.subTest(target=os.path.basename(target)):
                if os.path.lexists(link):
                    os.unlink(link)
                os.symlink(target, link)
                r = self.create(CW_PR_RECORD=link)
                self.assertTrue(os.path.islink(link))
                self.assertEqual(open(outside).read(), "")
                self.assertFalse(os.path.exists(os.path.join(self.d, "missing")))
                self.assertNotIn(P.TOKEN, r.stdout + r.stderr)

    # S15: a forged number in the record does not make another branch's pull request editable; own branch AND recorded does



    # S16 (a): the file-editing tool is denied for a path holding pr-record, in both agent blocks
    @staticmethod
    def resolve(rules, path):
        """The documented rule: the last matching rule wins, `*` matches any characters, no match = no decision."""
        import re
        out = None
        for pat, act in rules.items():
            if re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), path, re.S):
                out = act
        return out

    def s16_check(self, conf):
        blocks = (conf["permission"], conf["agent"]["worker"]["permission"])
        denied = ordinary = 0
        for b in blocks:
            self.assertEqual(b["edit"], {"*pr-record*": "deny"})
            for p in ("pr-record", "x/pr-record.json", "pr-record-x"):
                self.assertEqual(self.resolve(b["edit"], p), "deny", p)
                denied += 1
            self.assertIsNone(self.resolve(b["edit"], "src/a.py"))
            ordinary += 1
            self.assertIn("bash", b)
            self.assertEqual(b["question"], "deny")
        return len(blocks), denied, ordinary

    def test_s16_the_edit_tool_is_denied_for_the_record_in_both_blocks(self):
        import json
        with open(os.path.join(WORKER, "opencode-worker.json")) as f:
            text = f.read()
        r = self.s16_check(json.loads(text))
        print("S16: %d of 2 deny entries, 0 allow keys, %d of 6 paths denied, %d of 2 ordinary unmatched" % (r[0], r[1], r[2]))
        # the mutant: the deny key replaced by another; `applied` is computed from the two texts
        old, new = '"*pr-record*": "deny"\n    }', '"*pr-record-none*": "deny"\n    }'
        self.assertEqual(text.count(old), 1)
        changed = text.replace(old, new)
        applied = self.applied(text, changed, old)
        print("S16 mutant: applied=%s" % applied)
        self.assertTrue(applied, "the mutation was not applied")
        with self.assertRaises(AssertionError) as cm:
            self.s16_check(json.loads(changed))
        self.assertIn("{'*pr-record-none*': 'deny'}", str(cm.exception))
        # the no-op control: the removed text equal to the added text must fail the computation
        self.assertFalse(self.applied(text, text.replace(old, old), old))

    @staticmethod
    def applied(original, changed, removed):
        return changed != original and removed in original and removed not in changed

    # S19: the top-level block (read by every agent) holds the deny
    def s19_check(self, conf):
        top = conf["permission"]
        self.assertIn("edit", top, "the default agent is not covered")
        self.assertEqual(top["edit"], {"*pr-record*": "deny"}, "the default agent is not covered")
        got = [self.resolve(top["edit"], p) for p in ("src/a.py", "pr-record", "x/pr-record.json", "pr-record-x")]
        self.assertEqual(got, [None, "deny", "deny", "deny"])
        return got

    def test_s19_the_deny_sits_in_the_top_level_block(self):
        import json
        with open(os.path.join(WORKER, "opencode-worker.json")) as f:
            text = f.read()
        got = self.s19_check(json.loads(text))   # the control first
        old = '    "edit": {\n      "*pr-record*": "deny"\n    },\n'
        self.assertEqual(text.count(old), 1)
        changed = text.replace(old, "")
        applied = self.applied(text, changed, old)
        self.assertTrue(applied, "the mutation was not applied")
        with self.assertRaises(AssertionError) as cm:
            self.s19_check(json.loads(changed))
        self.assertIn("the default agent is not covered", str(cm.exception))
        self.assertFalse(self.applied(text, text.replace(old, old), old))
        print("S19: top-level paths %s, copy without the entry fails with %r, applied=%s, no-op applied=%s" % (
            got, "the default agent is not covered", applied, self.applied(text, text.replace(old, old), old)))

    # S16 (b): a pr-record that is already a symlink refuses the start, and the target's mode is not changed
    def test_s16_a_symlink_at_the_record_directory_refuses_the_start(self):
        target = os.path.join(self.d, "elsewhere")
        os.mkdir(target)
        os.chmod(target, 0o755)
        os.symlink(target, os.path.join(self.tmpfs, "pr-record"))
        r = self.start("sh", "-c", "echo started")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("started", r.stdout)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)

    # the interface document says what the client enforces, what an agent can still do, and the false positives

    def test_s14_the_entrypoint_exports_the_record_and_a_created_pr_is_logged(self):
        path = self.record_path()
        priv = os.path.join(self.tmpfs, "pr-record")
        self.assertEqual(path, os.path.join(priv, "numbers"))
        self.assertEqual(stat.S_IMODE(os.stat(priv).st_mode), 0o700)
        r = self.create()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(path).read().split(), [str(CREATED)])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

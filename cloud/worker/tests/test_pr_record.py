"""#1200 scenario S14: the pod's entrypoint exports CW_PR_RECORD (a file inside the worker-private temporary directory), the client writes the
number of a pull request it creates there (mode 0600, never through a symlink), and an own edit through the guard then passes while another
number refuses. A shell command that names the record is refused by a row of the permission file and of the ONE table. The record is NOT a
security boundary against a determined agent (same user): the head-branch check and the token's scope are. Offline; the token is synthetic."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_guard_table as G  # noqa: E402
import test_pr_edit as P  # noqa: E402

WORKER = os.path.dirname(HERE)
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
GUARD = os.path.join(WORKER, "guard.sh")
CW_GH = os.path.join(WORKER, "cw-gh")
EXPORT_LINE = 'export CW_PR_RECORD="$TMP/pr-record/numbers"\n'
CREATED = 12


class Create(P.H):
    """The stub: a POST to pulls creates number 12 (head: the pod's own branch); a GET answers from P.PRS."""
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
        for src, name in (("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md"), ("guard.sh", "guard.sh")):
            shutil.copy(os.path.join(WORKER, src), os.path.join(self.share, name))
        subprocess.run(["git", "init", "-q", self.work], check=True)
        subprocess.run(["git", "-C", self.work, "remote", "add", "origin", "https://github.com/%s.git" % P.REPO], check=True)
        P.PRS.clear()
        P.PRS[CREATED] = P.pr()
        P.PRS[8] = P.pr(head="other-branch")
        self.body = os.path.join(self.d, "body")
        open(self.body, "w").write("PLAN: x\nMERGE: captain\n\nnew text\n")

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
        return self.start("python3", CW_GH, "pr", "create", "--title", "t", "--body", "b", "--head", P.BRANCH, "--base", "main", entry=entry, **extra)

    def guard(self, record, line):
        env = {"PATH": "/usr/bin:/bin"}
        if record:
            env["CW_PR_RECORD"] = record
        return subprocess.run(["sh", GUARD, "gh"] + line.split(), capture_output=True, text=True, env=env, timeout=30)

    def flow(self, entry=ENTRYPOINT):
        """The S14 run: the exported path, a creation through the client, then the guard on the own number, another number, and (before) none."""
        out = {}
        out["path"] = self.record_path() if entry == ENTRYPOINT else self.start("sh", "-c", 'printf %s "${CW_PR_RECORD:-}"', entry=entry).stdout
        out["before"] = self.guard(out["path"], "pr edit %d --body-file %s" % (CREATED, self.body))
        P.H.seen = []
        out["create"] = self.create(entry=entry)
        out["own"] = self.guard(out["path"], "pr edit %d --body-file %s" % (CREATED, self.body))
        out["other"] = self.guard(out["path"], "pr edit 8 --body-file %s" % self.body)
        return out

    # S14
    def test_s14_the_entrypoint_exports_the_record_and_an_own_edit_passes(self):
        o = self.flow()
        priv = os.path.join(self.tmpfs, "pr-record")
        self.assertEqual(o["path"], os.path.join(priv, "numbers"))
        self.assertEqual(stat.S_IMODE(os.stat(priv).st_mode), 0o700)
        self.assertEqual(o["before"].returncode, 3)   # no own pull request yet: refused, as it must be
        self.assertIn("refused: pr-edit-no-record", o["before"].stderr)
        self.assertEqual(o["create"].returncode, 0, o["create"].stderr)
        self.assertEqual(open(o["path"]).read().split(), [str(CREATED)])
        self.assertEqual(stat.S_IMODE(os.stat(o["path"]).st_mode), 0o600)
        self.assertEqual(o["own"].returncode, 0, o["own"].stderr)
        self.assertEqual(o["other"].returncode, 3)
        self.assertIn("refused: pr-edit-other-number", o["other"].stderr)

    def test_s14_the_own_edit_reaches_the_client_and_makes_one_patch(self):
        path = self.record_path()
        self.assertEqual(self.create().returncode, 0)
        P.H.seen = []
        r = self.start("python3", CW_GH, "pr", "edit", str(CREATED), "--body-file", self.body)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(P.patches()), 1)
        self.assertTrue(os.path.isfile(path))

    # mutation: an entrypoint that exports nothing
    def test_s14_a_mutant_entrypoint_that_exports_nothing_refuses_the_own_edit(self):
        src = open(ENTRYPOINT).read()
        self.assertIn(EXPORT_LINE, src)
        mut = os.path.join(self.d, "entrypoint-mutant.sh")
        open(mut, "w").write(src.replace(EXPORT_LINE, "", 1))
        self.assertNotEqual(open(mut).read(), src)   # the mutation was applied
        o = self.flow(entry=mut)
        self.assertEqual(o["path"], "")
        self.assertEqual(o["own"].returncode, 3)
        self.assertIn("refused: pr-edit-no-record", o["own"].stderr)

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
                self.assertEqual(self.guard(link, "pr edit %d --body-file %s" % (CREATED, self.body)).returncode, 3)
                self.assertNotIn(P.TOKEN, r.stdout + r.stderr)

    # S15: a forged number in the record does not make another branch's pull request editable; own branch AND recorded does
    def client_with_record(self, n, numbers):
        rec = os.path.join(self.d, "forged-record")
        open(rec, "w").write(numbers)
        P.H.seen = []
        env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "CW_GH_API_BASE": self.base, "GH_TOKEN": P.TOKEN, "CW_BRANCH": P.BRANCH, "CW_PR_RECORD": rec}
        r = subprocess.run([sys.executable, CW_GH, "pr", "edit", str(n), "--body-file", self.body], env=env, capture_output=True, text=True,
                           cwd=self.work, timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotIn(P.TOKEN, r.stdout + r.stderr)
        return r

    def test_s15_a_forged_number_of_another_branch_is_refused_and_an_own_number_is_edited(self):
        forged = self.client_with_record(8, "8\n")
        self.assertEqual(forged.returncode, 3, forged.stderr)
        self.assertIn("refused: pr-edit-other-number", forged.stderr)
        self.assertEqual(len(P.patches()), 0)
        own = self.client_with_record(CREATED, "%d\n" % CREATED)
        self.assertEqual(own.returncode, 0, own.stderr)
        self.assertEqual(len(P.patches()), 1)

    # S16 (a): the file-editing tool is denied for a path holding pr-record, in both agent blocks
    def test_s16_the_edit_tool_is_denied_for_the_record_in_both_blocks(self):
        import json
        with open(os.path.join(WORKER, "opencode-worker.json")) as f:
            conf = json.load(f)
        blocks = (conf["permission"], conf["agent"]["worker"]["permission"])
        self.assertEqual(sum(1 for b in blocks if isinstance(b.get("edit"), dict) and b["edit"].get("*pr-record*") == "deny"), 2)
        for b in blocks:
            self.assertEqual(b["edit"].get("*"), "allow")
            self.assertEqual(list(b["edit"]).index("*"), 0)   # the last matching rule wins: the allow-all comes first
            self.assertIn("bash", b)
            self.assertEqual(b["question"], "deny")

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
    def test_the_interface_document_states_the_limits_of_the_record(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(WORKER)), "docs", "cloud-worker-interface.md")) as f:
            doc = f.read()
        for phrase in ("a convenience, not a boundary", "in the record AND its head branch is the pod's own branch",
                       "What an agent here can still do", "replace the directory", "edit the body of a pull request of its OWN branch",
                       "false positives", "already a symlink"):
            self.assertEqual(doc.count(phrase), 1, phrase)

    # the guard row: a command naming the record is refused
    FORMS = ("echo 99 >> $CW_PR_RECORD", "echo 99 > /run/cw-tmp/pr-record/numbers", 'cat "$CW_PR_RECORD"')

    def test_a_command_naming_the_record_is_refused_in_both_blocks(self):
        conf = G.load_conf()
        for form in self.FORMS:
            self.assertEqual(G.conf_decision(conf, form), "deny", form)
        self.assertEqual(G.conf_decision(conf, "ls -l"), "allow")
        self.assertEqual(G.conf_decision(conf, "gh pr edit 7 --body-file /tmp/f"), "allow")
        self.assertEqual(G.conf_decision(conf, "gh pr edit 7 --body-file /tmp/f ; echo $CW_PR_RECORD"), "deny")
        for pat in ("*CW_PR_RECORD*", "*pr-record*"):
            self.assertEqual(conf["permission"]["bash"][pat], "deny")
            self.assertEqual(conf["agent"]["worker"]["permission"]["bash"][pat], "deny")

    def test_a_mutant_permission_file_without_the_row_allows_the_forms(self):
        import json
        conf = json.loads(json.dumps(G.load_conf()))
        for blk in (conf["permission"]["bash"], conf["agent"]["worker"]["permission"]["bash"]):
            del blk["*CW_PR_RECORD*"]
            del blk["*pr-record*"]
        for form in self.FORMS:
            self.assertEqual(G.conf_decision(conf, form), "allow", form)

    # the client and the guard agree on the two table rows
    def test_the_table_rows_agree_in_the_guard_and_the_client(self):
        rows = [r for r in G.rows() if r["id"].startswith("shell-record-")]
        self.assertEqual(sorted(r["id"] for r in rows), ["shell-record-dir", "shell-record-var"])
        for r in rows:
            self.assertEqual(r["kind"], "denied")
            self.assertTrue(G.guard_denies(r["example"]), r["id"])
            P.H.seen = []
            c = subprocess.run([sys.executable, CW_GH] + r["example"].split(), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                               env={"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": P.TOKEN, "HOME": "/nonexistent"})
            self.assertEqual(c.returncode, 2, (r["id"], c.stderr))
            self.assertEqual(P.patches(), [])


if __name__ == "__main__":
    unittest.main()

"""#1200: a worker edits the body of its own pull request, `pr edit N --body-file F` only. Every row of the pr-edit block of the ONE table
(guard-table.tsv) runs through the guard and the client; mutants of the client and the guard each fail. Offline; the token is synthetic."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(WORKER))
GUARD = os.path.join(WORKER, "guard.sh")
CW_GH = os.path.join(WORKER, "cw-gh")
TABLE = os.path.join(WORKER, "guard-table.tsv")
TOKEN = "synthetic-" + "Qm7Lx2" * 6
REPO = "acme-org/widget-service"
BRANCH = "cw/own-branch"
PRS = {}   # number -> pull request object served by the stub; a missing number answers 404


class H(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        H.seen.append(("GET", self.path))
        n = self.path.rsplit("/", 1)[-1]
        if n.isdigit() and int(n) in PRS:
            return self.reply(200, PRS[int(n)])
        self.reply(404, {"message": "Not Found"})

    def do_PATCH(self):
        ln = int(self.headers.get("Content-Length") or 0)
        H.seen.append(("PATCH", self.path, json.loads(self.rfile.read(ln))))
        self.reply(200, {"html_url": "https://example.invalid/pr"})

    do_POST = do_PUT = do_DELETE = do_PATCH


def pr(head=BRANCH, base=REPO, body="PLAN: x\nMERGE: captain\n\nold", head_repo=REPO):
    return {"head": {"ref": head, "repo": {"full_name": head_repo}}, "base": {"repo": {"full_name": base}}, "body": body}


def patches():
    return [x for x in H.seen if x[0] != "GET"]


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.tmp = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.work = os.path.join(cls.tmp.name, "work")
        os.mkdir(cls.work)
        subprocess.run(["git", "init", "-q", cls.work], check=True)
        subprocess.run(["git", "-C", cls.work, "remote", "add", "origin", "https://github.com/%s.git" % REPO], check=True)
        cls.addClassCleanup(cls.srv.shutdown)

    def setUp(self):
        PRS.clear()
        PRS.update({7: pr(), 8: pr(head="other-branch"), 9: pr(base="other-org/other-repo")})
        self.hold = self.file("hold", "PLAN: x\nMERGE: captain\n\nnew text\n")
        self.nohold = self.file("nohold", "PLAN: x\n\nnew text\n")
        self.changed = self.file("changed", "PLAN: x\nMERGE: owner\n\nnew text\n")
        self.record = self.file("record", "7\n10\n")

    def file(self, name, text):
        p = os.path.join(self.tmp.name, name)
        open(p, "w").write(text)
        return p

    def guard(self, line, record=True):
        env = {"PATH": "/usr/bin:/bin"}
        if record:
            env["CW_PR_RECORD"] = self.record
        r = subprocess.run(["sh", GUARD, "gh"] + line.split(), capture_output=True, text=True, env=env, timeout=30)
        return r

    def client(self, line, script=CW_GH, extra=None):
        H.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent", "CW_BRANCH": BRANCH}
        env.update(extra or {})
        r = subprocess.run([sys.executable, script] + line.split(), capture_output=True, text=True, env=env, cwd=self.work, timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        return r

    def edit(self, n, f=None, script=CW_GH):
        return self.client("pr edit %d --body-file %s" % (n, f or self.hold), script)

    # S2, S5
    def test_the_own_edit_is_made_with_one_patch(self):
        r = self.edit(7)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(patches()), 1)
        self.assertEqual(patches()[0][1], "/repos/%s/pulls/7" % REPO)
        self.assertEqual(patches()[0][2], {"body": "PLAN: x\nMERGE: captain\n\nnew text\n"})
        self.assertEqual(self.guard("pr edit 7 --body-file " + self.hold).returncode, 0)

    def test_the_denied_forms_exit_3_with_no_patch(self):
        cases = {
            "pr-edit-other-number": "pr edit 8 --body-file " + self.hold,
            "pr-edit-inline-body": "pr edit 7 --body x",
            "pr-edit-url-other-repo": "pr edit https://github.com/other-org/other-repo/pull/7 --body-file " + self.hold,
            "pr-edit-url-other-repo ": "pr edit 7 --body-file %s --repo other-org/other-repo" % self.hold,
        }
        for row, line in cases.items():
            for who in ("client", "guard"):
                with self.subTest(row=row, line=line, who=who):
                    if who == "client":
                        r = self.client(line)
                        self.assertEqual(len(patches()), 0)
                        if "--body-file" not in line:   # the client verb table stays as pinned: `pr edit` with no --body-file is not available
                            self.assertEqual(r.returncode, 2)
                            continue
                    else:
                        r = self.guard(line)
                    self.assertEqual(r.returncode, 3, r.stderr)
                    self.assertIn("refused: " + row.strip(), r.stderr)
        # a raw API call to the pull request or to the issue of the same number
        for line in ("api -X PATCH repos/%s/pulls/7" % REPO, "api -X PATCH repos/%s/issues/7" % REPO):
            self.assertEqual(self.guard(line).returncode, 3, line)
            r = self.client(line)
            self.assertEqual((r.returncode, len(patches())), (2, 0), line)
        r = self.guard("api -X PATCH repos/%s/pulls/7" % REPO)
        self.assertIn("refused: pr-edit-api", r.stderr)
        self.assertEqual(self.client("issue edit 7 --body-file x").returncode, 2)   # an issue by the same number: no verb

    # S3
    def test_every_other_option_is_refused(self):
        for opt in ("--title t", "--base b", "--add-reviewer u", "--add-project p", "--milestone m", "--remove-milestone"):
            line = "pr edit 7 --body-file %s %s" % (self.hold, opt)
            for who, r in (("guard", self.guard(line)), ("client", self.client(line))):
                self.assertEqual(r.returncode, 3, (who, opt, r.stderr))
                self.assertIn("refused: pr-edit-other-option", r.stderr)
            self.assertEqual(len(patches()), 0)
        for line in ("pr edit 7 --add-label x", "pr edit 7 --add-assignee u"):   # the older pinned texts stay
            self.assertEqual(self.guard(line).returncode, 3)
            self.assertEqual(self.client(line).returncode, 2)   # no --body-file: not available in the client
        self.assertEqual(self.guard("pr merge 7").returncode, 3)
        self.assertEqual(self.guard("pr edit 7 --draft").returncode, 3)

    # S7
    def test_the_hold_line_may_not_be_dropped_or_changed(self):
        for f in (self.nohold, self.changed):
            r = self.edit(7, f)
            self.assertEqual(r.returncode, 3)
            self.assertIn("refused: pr-edit-no-hold-line", r.stderr)
            self.assertEqual(len(patches()), 0)
        twice = self.file("twice", "MERGE: captain\nMERGE: captain\n")
        self.assertEqual(self.edit(7, twice).returncode, 3)
        PRS[7] = pr(body="no hold here")   # the current body has no hold line: any body is allowed
        self.assertEqual(self.edit(7, self.nohold).returncode, 0)

    # S8
    def test_the_read_checks(self):
        r = self.edit(8)
        self.assertEqual((r.returncode, len(patches())), (3, 0))
        r = self.edit(9)
        self.assertEqual((r.returncode, len(patches())), (3, 0))
        r = self.edit(404)
        self.assertEqual((r.returncode, len(patches())), (3, 0))
        self.assertIn("refused: pr-edit-read-failed", r.stderr)
        PRS[7] = {"head": "bad"}
        r = self.edit(7)
        self.assertEqual((r.returncode, len(patches())), (3, 0))
        self.assertIn("refused: pr-edit-read-failed", r.stderr)

    def test_a_pull_request_the_pod_created_is_recorded_and_allowed(self):
        rec = os.path.join(self.tmp.name, "rec2")
        PRS[11] = pr(head=BRANCH)
        r = self.client("pr edit 11 --body-file " + self.hold, extra={"CW_PR_RECORD": rec})
        self.assertEqual(r.returncode, 3)
        open(rec, "w").write("11\n")
        r = self.client("pr edit 11 --body-file " + self.hold, extra={"CW_PR_RECORD": rec})
        self.assertEqual((r.returncode, len(patches())), (0, 1), r.stderr)

    # DW-6: every row of the table, the guard and the client agree
    def test_agreement_on_every_pr_edit_row(self):
        rows = [l.split("\t") for l in open(TABLE).read().splitlines() if l.startswith("pr-edit-")]
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r[0] for r in rows}), 8)
        for rid, kind, tool, method, path, why, example in rows:
            line = example.replace("/tmp/f", self.hold).replace("/tmp/nohold", self.nohold)
            client_only = path.startswith("client only")
            g = self.guard(line)
            if kind == "allowed":
                self.assertEqual(g.returncode, 0, rid)
                self.assertEqual(self.client(line).returncode, 0, rid)
            else:
                self.assertEqual(g.returncode, 0 if client_only else 3, rid)
                if not client_only:
                    self.assertIn("refused: " + rid, g.stderr, rid)
                if rid != "pr-edit-api" and "--body-file" in line:
                    r = self.client(line)
                    self.assertEqual(r.returncode, 3, (rid, r.stderr))
                    self.assertIn("refused: " + rid, r.stderr)
                elif rid != "pr-edit-api":
                    self.assertEqual(self.client(line).returncode, 2, rid)
                else:
                    self.assertEqual(self.client(line).returncode, 2)
                self.assertEqual(len(patches()), 0, rid)

    def test_s12_the_token_table_is_untouched(self):
        r = subprocess.run(["git", "diff", "--quiet", "origin/cw/guard-reads", "--", "cloud/token-table.txt"], cwd=ROOT, capture_output=True)
        self.assertIn(r.returncode, (0, 128))

    # S6, S9: mutants
    def mutant(self, old, new, script=CW_GH):
        src = open(script).read()
        self.assertIn(old, src)
        p = os.path.join(self.tmp.name, "mut-" + str(abs(hash(old + new))))
        open(p, "w").write(src.replace(old, new, 1))
        return p

    def test_mutants_of_the_client_each_fail_the_checks(self):
        m = self.mutant('or not (mine and head == mine):', 'or False:')
        self.assertEqual(self.edit(8, script=m).returncode, 0)   # allows any number (the true build: 3)
        src = open(CW_GH).read()
        for name, old, new, probe in (
            ("title edit", 'if len(args) != 3 or args[1] != "--body-file" or not args[0].isdigit():', "if False:", "pr edit 7 --body-file %s --title x" % self.hold),
            ("inline body", 'if a in ("--body", "-b") or a.startswith(("--body=", "-b")):', "if False:", "pr edit 7 --body-file %s --body x" % self.hold),
            ("no hold check", 'if cur_holds and', "if False and", "pr edit 7 --body-file " + self.nohold),
            ("failed read", 'except (Fail, KeyError, TypeError, ValueError):\n        raise refused("pr-edit-read-failed")', "except (KeyError, TypeError, ValueError):\n        raise refused('pr-edit-read-failed')", "pr edit 404 --body-file " + self.hold),
        ):
            with self.subTest(mutant=name):
                self.assertIn(old, src)
                m = self.mutant(old, new)
                r = self.client(probe, script=m)
                row = {"title edit": "pr-edit-other-option", "inline body": "pr-edit-inline-body", "no hold check": "pr-edit-no-hold-line", "failed read": "pr-edit-read-failed"}[name]
                self.assertNotIn("refused: " + row, r.stderr)   # the mutant no longer refuses by that row
                t = self.client(probe)
                self.assertEqual(t.returncode, 3)
                self.assertIn("refused: " + row, t.stderr)

    def test_mutant_that_skips_the_own_check_edits_another_pull_request(self):
        m = self.mutant('if base_repo.lower() != repo.lower() or not (mine and head == mine):', "if False:")
        self.assertEqual(self.edit(8, script=m).returncode, 0)
        self.assertEqual(self.edit(8).returncode, 3)

    def test_mutants_of_the_guard_each_fail(self):
        src = open(GUARD).read()
        for name, old, new, line in (
            ("any number", 'grep -qx -- "$1" "$CW_PR_RECORD" || refuse "pr-edit-other-number"', "true", "pr edit 8 --body-file f"),
            ("title edit", '{ [ $# -eq 3 ] && [ "$2" = --body-file ]; } || refuse "pr-edit-other-option"', "true", "pr edit 7 --title x"),
            ("inline body", '--body|--body=*|-b|-b?*) refuse "pr-edit-inline-body" ;;', "--nothing) ;;", "pr edit 7 --body x"),
            ("url", '*://*|-R|-R?*|--repo|--repo=*) refuse', "--nothing) true", "pr edit https://github.com/other-org/x/pull/7 --body-file f"),
            ("raw api", '[ "$m" = PATCH ] && refuse "pr-edit-api"', "true", "api -X PATCH repos/acme-org/widget-service/pulls/7"),
        ):
            with self.subTest(mutant=name):
                self.assertIn(old, src)
                p = os.path.join(self.tmp.name, "g-" + name.replace(" ", "-") + ".sh")
                open(p, "w").write(src.replace(old, new, 1))
                env = {"PATH": "/usr/bin:/bin", "CW_PR_RECORD": self.record}
                r = subprocess.run(["sh", p, "gh"] + line.split(), capture_output=True, text=True, env=env, timeout=30)
                row = {"any number": "other-number", "title edit": "other-option", "inline body": "inline-body", "url": "url-other-repo", "raw api": "api"}[name]
                self.assertNotIn("refused: pr-edit-" + row, r.stderr)
                t = self.guard(line)
                self.assertEqual(t.returncode, 3, name)
                self.assertIn("refused: pr-edit-" + row, t.stderr)

    # S1, S10, S13
    def test_docs_list_the_one_form(self):
        t = open(os.path.join(ROOT, "docs", "cloud-worker-interface.md")).read()
        self.assertEqual(t.count("pr edit N --body-file F"), 1)
        para = t[t.index("editing the body of its own pull request (#1200)"):]
        first = para.split("\n\n")[0]
        self.assertIn("guard.sh", first)
        self.assertIn("guard-table.tsv", first)


if __name__ == "__main__":
    unittest.main()

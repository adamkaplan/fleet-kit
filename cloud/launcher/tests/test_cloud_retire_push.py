"""A retired worker's end hook pushes with a freshly minted token (#1018): scenarios S1-S10, S12, U1 of its plan.

Offline: the cluster is the fake kubectl beside this file, the remote is a local HTTP git server that accepts ONE token
(the fresh one) and records what it saw. No token literal is in this file: the values are assembled at runtime.
"""
import http.server
import json
import os
import re
import shlex
import socketserver
import subprocess
import sys
import threading
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
import end_hook  # noqa: E402
cw = base.cw
make_repo = base.make_repo

FRESH = "".join(["gh", "s_", "fresh", uuid.uuid4().hex])   # assembled at runtime: no token-shaped literal in the file
DEAD = "".join(["gh", "s_", "dead", uuid.uuid4().hex])
OTHER = "".join(["gh", "s_", "other", uuid.uuid4().hex])


class _Remote(http.server.BaseHTTPRequestHandler):
    seen = None          # per-server list of (method, path, token or None)
    accept = None
    root = None

    def log_message(self, *a):
        pass

    def _body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                n = int(self.rfile.readline().strip() or b"0", 16)
                if n == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(n)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _serve(self):
        body = self._body()
        auth = self.headers.get("Authorization", "")
        tok = None
        if auth.startswith("Basic "):
            import base64
            tok = base64.b64decode(auth[6:]).decode().partition(":")[2]
        self.server.seen.append((self.command, self.path.split("?")[0], tok))
        if tok != self.server.accept:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="r"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        env = dict(os.environ, GIT_PROJECT_ROOT=self.server.root, GIT_HTTP_EXPORT_ALL="1", REQUEST_METHOD=self.command,
                   PATH_INFO=path, QUERY_STRING=query, CONTENT_TYPE=self.headers.get("Content-Type", ""),
                   CONTENT_LENGTH=str(len(body)), REMOTE_USER="x", REMOTE_ADDR="127.0.0.1")
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(": ")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class RetirePush(base.Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.bare = os.path.join(d, "srv", "repo.git")
        os.makedirs(os.path.dirname(self.bare))
        subprocess.run(["git", "init", "--bare", "-b", "main", self.bare], check=True, capture_output=True)
        self.srv = Server(("127.0.0.1", 0), _Remote)
        self.srv.seen, self.srv.accept, self.srv.root = [], FRESH, os.path.dirname(self.bare)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.work = os.path.join(d, "work")
        g = lambda *a, cwd=None: subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True)
        g("init", "-b", "main", self.work)
        for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
            g("config", k, v, cwd=self.work)
        # the pod's own credential: a helper that answers with the DEAD token (what the per-run token file holds after its hour)
        g("config", "credential.helper", '!f() { [ "$1" = get ] && printf "username=x-access-token\\npassword=%s\\n" ' + DEAD + '; }; f', cwd=self.work)
        g("remote", "add", "origin", "http://127.0.0.1:%d/repo.git" % self.srv.server_address[1], cwd=self.work)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("one\n")
        g("add", "-A", cwd=self.work)
        g("commit", "-m", "work", cwd=self.work)
        self.head = subprocess.run(["git", "-C", self.work, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.patch_state(hook_env={"CW_WORKDIR": self.work, "CW_WORKER": "coder7", "CW_HOOK_GIT_BASE": "http://127.0.0.1:%d" % self.srv.server_address[1],
                                    "CW_REPO": "repo"})
        self.env["CW_GIT_TOKEN_CMD"] = self.mint_cmd(FRESH)
        self.env["FAKE_KUBE_SEEN"] = os.path.join(d, "seen.jsonl")

    # -- helpers --
    def mint_cmd(self, tok):
        """A mint command that reads the token from a file kept out of every scan: the command line itself never holds it."""
        path = os.path.join(self.tmp.name, "mint.tok")
        with open(path, "w") as f:
            f.write(tok)
        return "cat " + path

    def retire(self, how="stop"):
        """One retire through an entry that reaches the hook; the entries are the family (stop, the reaper's two)."""
        if how == "stop":
            self.create("coder7")
            return self.run_cli("stop", "coder7")
        self.create("coder7", "--hard", "1h")
        self.set_activity("coder7", how == "reap-hard", base.T0)
        self.clock.advance(hours=2 if how == "reap-hard" else 3)
        code, out, err = self.run_cli("reap")
        row = json.loads(out)[0] if out.strip().startswith("[") else {}
        return (code if code else (0 if not row.get("error") else 1)), json.dumps(row), err

    def pushed_branch(self):
        p = subprocess.run(["git", "-C", self.bare, "rev-parse", "--verify", "-q", "refs/heads/coder7/wip"], capture_output=True, text=True)
        return p.stdout.strip() or None

    def pushes_with(self, tok):
        return [s for s in self.srv.seen if s[0] == "POST" and s[1].endswith("git-receive-pack") and s[2] == tok]

    def artefacts(self):
        """Everything an observer could read, as one text: kubectl arguments, the fake cluster, the launcher's environment as the
        fake saw it, the run record, the audit line."""
        parts = [open(self.env["FAKE_KUBE_CALLS"]).read(), open(self.state).read(), open(self.env["FAKE_KUBE_SEEN"]).read()]
        for root, _, names in os.walk(self.tmp.name):
            if os.path.basename(root) == "work" or os.sep + "work" + os.sep in root or ".git" in root or "srv" in root:
                continue
            for n in names:
                if n in ("seen.jsonl", "calls.jsonl", "kube.json", "mint.tok"):
                    continue
                try:
                    parts.append(open(os.path.join(root, n), errors="replace").read())
                except OSError:
                    pass
        return "\n".join(parts)

    def kube_calls(self):
        return [json.loads(x) for x in open(self.env["FAKE_KUBE_CALLS"])]

    # -- S2 (also S8's body, S12's, and the entry family) --
    def test_s2_a_dead_pod_credential_still_pushes_with_the_fresh_token_through_every_entry(self):
        for how in ("stop", "reap-hard", "reap-idle"):
            with self.subTest(entry=how):
                self.setUp()
                code, out, err = self.retire(how)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.pushed_branch(), self.head)
                self.assertEqual(len(self.pushes_with(FRESH)), 1)
                self.assertEqual(self.pushes_with(DEAD), [])
                self.assertEqual([s for s in self.srv.seen if s[2] == DEAD], [])   # the old credential was never offered at all
                self.assertIsNone(self.pod())

    # -- S3 --
    def test_s3_the_fresh_token_is_in_no_artefact_on_success_and_on_failure(self):
        for label, tok in (("success", FRESH), ("push refused", OTHER)):
            with self.subTest(case=label):
                self.setUp()
                self.env["CW_GIT_TOKEN_CMD"] = self.mint_cmd(tok)
                code, out, err = self.retire()
                self.assertEqual(code, 0 if label == "success" else 1)
                self.assertTrue(self.kube_calls())
                hay = self.artefacts() + out + err
                self.assertNotIn(tok, hay)
                self.assertEqual(hay.count(tok), 0)
                for c in self.kube_calls():   # the token travelled on stdin: no argument carries it, and the flag does not
                    self.assertFalse(any(tok in a for a in c))

    def test_s3_the_hook_reads_the_token_only_from_stdin(self):
        self.retire()
        execs = [c for c in self.kube_calls() if "exec" in c and "cw-end-hook" in c]
        self.assertEqual(len(execs), 1)
        self.assertIn("-i", execs[0])
        self.assertEqual(execs[0][execs[0].index("cw-end-hook"):], ["cw-end-hook", "--token-stdin"])

    # -- S3b: the repository is the agent's; nothing in it may receive the token --
    def test_s3_a_repository_that_names_another_host_or_runs_code_never_sees_the_token(self):
        other = Server(("127.0.0.1", 0), _Remote)
        other.seen, other.accept, other.root = [], FRESH, self.srv.root
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        loot = os.path.join(self.tmp.name, "loot.txt")
        hook = os.path.join(self.work, ".git", "hooks", "pre-push")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nenv > %s\n" % loot)
        os.chmod(hook, 0o755)
        g = lambda *a: subprocess.run(["git", "-C", self.work] + list(a), check=True, capture_output=True)
        g("config", "core.fsmonitor", "sh -c 'env > %s'" % loot)
        g("remote", "set-url", "origin", "http://127.0.0.1:%d/repo.git" % other.server_address[1])   # the agent points origin at another host
        code, out, err = self.retire()
        self.assertEqual(code, 0, err)   # origin is not trusted and not used: the push went to the fixed base
        self.assertEqual(self.pushed_branch(), self.head)
        self.assertEqual(other.seen, [])
        self.assertNotIn(FRESH, out + err)
        if os.path.exists(loot):   # a monitor may run on the hook's own untokened `git status`: it must never hold the token
            self.assertNotIn(FRESH, open(loot).read())

    def test_s3_repository_scoped_proxy_tls_and_resolve_keys_are_never_read(self):
        # URL-scoped keys of the agent's own config would divert or weaken the connection that carries the token
        g = lambda *a: subprocess.run(["git", "-C", self.work] + list(a), check=True, capture_output=True)
        base = "http://127.0.0.1:%d/" % self.srv.server_address[1]
        for key, val in (("http.proxy", "http://127.0.0.1:1"), ("http." + base + ".proxy", "http://127.0.0.1:1"),
                         ("http." + base + ".sslVerify", "false"), ("http.curloptResolve", "127.0.0.1:%d:127.0.0.1" % 1),
                         ("http.extraHeader", "X-Leak: 1")):
            g("config", key, val)
        code, out, err = self.retire()
        self.assertEqual(code, 0, err)   # the push reached the real (fixed) base despite the diverting keys
        self.assertEqual(self.pushed_branch(), self.head)

    def test_s3_origin_as_a_local_path_with_a_receivepack_command_runs_nothing(self):
        loot = os.path.join(self.tmp.name, "loot2.txt")
        g = lambda *a: subprocess.run(["git", "-C", self.work] + list(a), check=True, capture_output=True)
        g("remote", "set-url", "origin", self.bare)
        g("config", "remote.origin.receivepack", "sh -c 'env > %s'" % loot)
        g("config", "remote.origin.uploadpack", "sh -c 'env > %s'" % loot)
        g("config", "url.file:///.insteadOf", "http://127.0.0.1:%d/" % self.srv.server_address[1])   # and rewrites the fixed URL to a local one
        code, out, err = self.retire()
        self.assertFalse(os.path.exists(loot), "a repository-named command ran")
        self.assertNotIn(FRESH, out + err)

    def test_a_token_without_a_recorded_repository_pushes_nowhere(self):
        self.patch_state(hook_env={"CW_WORKDIR": self.work, "CW_WORKER": "coder7", "CW_HOOK_GIT_BASE": "http://127.0.0.1:%d" % self.srv.server_address[1]})
        code, out, err = self.retire()
        self.assert_kept(code, err, "push")
        self.assertEqual(self.srv.seen, [])

    # -- the explicit-workdir rule, the odd mint outputs, the mint's scope, the flag's absence --
    def test_an_explicit_workdir_that_is_missing_is_an_error_not_no_checkout(self):
        self.patch_state(hook_env={"CW_WORKDIR": os.path.join(self.tmp.name, "nowhere"), "CW_WORKER": "coder7"})
        code, out, err = self.retire()
        self.assert_kept(code, err, "push")

    def test_odd_mint_outputs_keep_the_pod_with_step_mint(self):
        odd = {"empty output, exit 0": "true", "two lines": "printf 'a\\nb'", "over long": "head -c 5000 /dev/zero | tr '\\0' x",
               "exit 1 with a token": "sh -c 'printf %s x; exit 1'"}
        for label, cmd in odd.items():
            with self.subTest(output=label):
                self.setUp()
                self.env["CW_GIT_TOKEN_CMD"] = cmd
                code, out, err = self.retire()
                self.assert_kept(code, err, "mint")

    def test_a_pod_that_records_no_repository_is_a_mint_failure(self):
        self.create("coder7")
        st = self.st()
        for c in st["pods"][cw.pod_name("coder7")]["spec"]["containers"]:
            c["env"] = [e for e in c.get("env", []) if e.get("name") != "CW_REPO"]
        self.write_state(st)
        code, out, err = self.run_cli("stop", "coder7")
        self.assert_kept(code, err, "mint")

    def test_the_mint_is_asked_for_the_repository_recorded_on_the_pod_only(self):
        seen = os.path.join(self.tmp.name, "mint.args")
        self.env["CW_GIT_TOKEN_CMD"] = "sh -c 'echo \"$0 $GH_REPO\" > %s; cat %s' %s" % (seen, self.mint_cmd(FRESH).split()[-1], "acme-org/widget-service")
        self.assertEqual(self.retire()[0], 0)
        self.assertEqual(open(seen).read().split()[-1], "acme-org/widget-service")

    def test_read_token_takes_one_clean_line_only(self):
        import io
        self.assertIsNone(end_hook.read_token(io.StringIO("")))
        self.assertIsNone(end_hook.read_token(io.StringIO("a\nb")))
        self.assertIsNone(end_hook.read_token(io.StringIO("x" * (end_hook.MAX_TOKEN + 1))))
        self.assertEqual(end_hook.read_token(io.StringIO(FRESH + "\n")), FRESH)

    def test_an_image_whose_hook_predates_the_flag_behaves_as_before(self):
        path = os.path.join(self.tmp.name, "old_hook.py")
        src = open(os.path.join(os.path.dirname(HERE), "end_hook.py")).read()
        old = src.replace('token = read_token(sys.stdin) if "--token-stdin" in sys.argv[1:] else None', 'token = None')
        self.assertNotEqual(old, src)
        with open(path, "w") as f:
            f.write(old)
        self.patch_state(hook_path=path)
        code, out, err = self.retire()   # the pod's own credential is dead: refused as today, pod kept, route named
        self.assert_kept(code, err, "push")

    # -- S4 --
    def assert_kept(self, code, err, step):
        self.assertEqual(code, cw.EXIT_HOOK)
        self.assertEqual(self.pod()["metadata"]["annotations"]["cw/kept"], "true")
        self.assertTrue(self.st()["secrets"])
        self.assertEqual(len(os.listdir(os.path.join(os.path.dirname(self.env["CW_CLOUD_AUDIT"]), "runs"))), 1)
        self.assertEqual(err.count("git bundle"), 1)
        self.assertEqual(err.count("pod kept"), 1)
        self.assertIn("step: %s" % step, err)
        self.assertEqual([c for c in self.kube_calls() if c[0] == "delete" or "delete" in c[:6]], [])

    def test_s4_a_failed_mint_keeps_the_pod_and_names_the_step_and_the_route(self):
        self.env["CW_GIT_TOKEN_CMD"] = "false"
        code, out, err = self.retire()
        self.assert_kept(code, err, "mint")
        self.assertIsNone(self.pushed_branch())

    def test_s4_a_refused_push_keeps_the_pod_and_names_the_step_and_the_route(self):
        self.env["CW_GIT_TOKEN_CMD"] = self.mint_cmd(OTHER)
        code, out, err = self.retire()
        self.assert_kept(code, err, "push")

    def test_s4_a_launcher_with_no_mint_settings_is_as_before_and_still_names_the_route(self):
        del self.env["CW_GIT_TOKEN_CMD"]
        code, out, err = self.retire()
        self.assert_kept(code, err, "push")   # the pod's own (dead) credential is all there is

    # -- S5 --
    def test_s5_the_documented_recovery_route_reads_and_changes_nothing(self):
        self.env["CW_GIT_TOKEN_CMD"] = "false"
        self.assertEqual(self.retire()[0], cw.EXIT_HOOK)
        with open(self.state, "rb") as f:
            before = f.read()
        fake = os.path.join(HERE, "fake_kubectl")
        pod = cw.pod_name("coder7")
        env = dict(os.environ, FAKE_KUBE_STATE=self.state)
        kube = [sys.executable, fake, "--context", "acme-cw", "-n", "cw", "exec", pod, "--"]
        bundle = os.path.join(self.tmp.name, "worker.bundle")
        with open(bundle, "wb") as f:
            self.assertEqual(subprocess.run(kube + ["git", "-C", "/work/repo", "bundle", "create", "-", "--all"], env=env, stdout=f).returncode, 0)
        tarball = os.path.join(self.tmp.name, "repo.tar")
        with open(tarball, "wb") as f:
            self.assertEqual(subprocess.run(kube + ["tar", "-C", "/work", "-cf", "-", "repo"], env=env, stdout=f).returncode, 0)
        out = os.path.join(self.tmp.name, "recovered")
        subprocess.run(["git", "clone", "-q", bundle, out], check=True, capture_output=True)
        self.assertEqual(subprocess.run(["git", "-C", out, "rev-parse", "main"], capture_output=True, text=True).stdout.strip(), self.head)
        with open(self.state, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_s5_the_message_carries_the_same_command_the_docs_name(self):
        self.assertIn("git -C /work/repo bundle create - --all", cw.retire_token.RECOVERY)

    # -- S6, S7 --
    def test_s6_a_never_cloned_pod_retires_clean_saying_nothing_to_push(self):
        if os.path.exists(end_hook.DEFAULT_WORKDIR):
            self.skipTest("this machine has a real /work/repo")
        self.patch_state(hook_env={"CW_WORKER": "coder7"})   # no CW_WORKDIR: the pod's default /work/repo, which was never made
        code, out, err = self.retire()
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertFalse(res["pushed"])
        self.assertIn("no checkout; nothing to push", err)
        self.assertNotIn("cannot change to /work/repo", out + err)
        self.assertIsNone(self.pod())
        self.assertEqual(self.st()["secrets"], {})
        self.assertEqual(self.srv.seen, [])   # no token use, no remote call

    def test_s7_a_work_directory_that_is_no_repository_keeps_the_pod(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        self.patch_state(hook_env={"CW_WORKDIR": empty, "CW_WORKER": "coder7"})
        code, out, err = self.retire()
        self.assert_kept(code, err, "push")

    # -- S8: three mutants, each must break S2 --
    def mutant(self, name, old, new, launcher=None):
        src = open(os.path.join(os.path.dirname(HERE), "end_hook.py")).read()
        if old is not None:
            self.assertIn(old, src, name)
            src = src.replace(old, new)
        path = os.path.join(self.tmp.name, "mutant_hook.py")
        with open(path, "w") as f:
            f.write(src)
        self.patch_state(hook_path=path)

    def s2_outcome(self):
        code, out, err = self.retire()
        return code == 0 and self.pushed_branch() == self.head and len(self.pushes_with(FRESH)) == 1 and not self.pushes_with(DEAD)

    def test_s8_the_real_build_passes_the_scenario_the_mutants_fail(self):
        self.assertTrue(self.s2_outcome())

    def test_s8_mutant_hook_reuses_the_old_credential(self):   # the hook never swaps its credential helper
        self.mutant("old credential", '"-c", "credential.helper=", "-c", "credential.helper=" + HELPER', '"-c", "core.pager=cat"')
        self.assertFalse(self.s2_outcome())

    def test_s8_mutant_token_fetched_but_the_old_file_used(self):   # the hook reads the token and then drops it
        self.mutant("token dropped", 'token = read_token(sys.stdin) if "--token-stdin" in sys.argv[1:] else None',
                    'read_token(sys.stdin); token = None')
        self.assertFalse(self.s2_outcome())

    def test_s8_mutant_the_mint_is_skipped(self):
        orig = cw.retire_token.mint
        cw.retire_token.mint = lambda env, repo: None
        self.addCleanup(setattr, cw.retire_token, "mint", orig)
        self.assertFalse(self.s2_outcome())

    def test_s8_mutant_the_mint_is_skipped_by_not_configuring(self):   # a launcher that quietly never sends the token
        orig = cw.retire_token.configured
        cw.retire_token.configured = lambda env: False
        self.addCleanup(setattr, cw.retire_token, "configured", orig)
        self.assertFalse(self.s2_outcome())

    # -- S12 --
    def test_s12_a_failure_leaves_nothing_that_blocks_the_next_retire(self):
        self.env["CW_GIT_TOKEN_CMD"] = "false"
        self.assertEqual(self.retire()[0], cw.EXIT_HOOK)
        self.env["CW_GIT_TOKEN_CMD"] = self.mint_cmd(FRESH)
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pushed_branch(), self.head)
        self.assertIsNone(self.pod())
        self.assertEqual(self.st()["secrets"], {})

    # -- U1 --
    def test_u1_a_launcher_with_no_mint_settings_behaves_exactly_as_before(self):
        # the pod's own credential and its `origin` are used as before (a local bare origin here); with mint settings the push
        # goes to the fixed base instead (S2), so this case is the one the old tests also pin
        del self.env["CW_GIT_TOKEN_CMD"]
        work, _ = make_repo(self.tmp.name, "r", dirty=True)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})
        code, out, err = self.retire()
        res = json.loads(out)
        self.assertEqual((code, res["pushed"], res["wip_branch"], res["skipped"]), (0, True, "coder7/wip", [".env"]))
        self.assertEqual(err.count("WARNING: not saved"), 1)
        self.assertNotIn("pod kept", err)
        # a pod whose work is already on a remote branch still prints pushed false and no note
        self.setUp()
        del self.env["CW_GIT_TOKEN_CMD"]
        work, _ = make_repo(self.tmp.name, "clean", dirty=False)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})
        code, out, err = self.retire()
        self.assertEqual((code, json.loads(out)["pushed"]), (0, False))
        self.assertNotIn("nothing to push", err)


class Docs(unittest.TestCase):
    FILES = ("cloud/launcher/README.md", "cloud/README.md", "docs/cloud-worker-interface.md")
    RULES = {   # the five rules and the route, each a phrase a reader can find
        "fresh-token": "fresh token",
        "keep-and-say-once": "says so once",
        "never-cloned": "nothing to push",
        "secrets-lifetime": "dead token",
        "route": "bundle create - --all",
    }

    def text(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return f.read()

    def section(self, rel):
        t = self.text(rel)
        m = re.search(r"^#+ .*retire path.*$", t, re.M | re.I)
        self.assertIsNotNone(m, rel + " has no retire-path section")
        return t[m.end():].split("\n## ")[0]

    def test_s1_the_cause_comes_before_the_rules(self):
        s = self.section("cloud/launcher/README.md")
        cause = s.find("expired")
        self.assertGreaterEqual(cause, 0)
        self.assertIn("refused", s[:s.find("fresh token")])
        self.assertLess(cause, s.find("fresh token"))

    def test_s10_each_file_states_each_rule_and_the_route(self):
        for rel in self.FILES:
            sec = self.section(rel)
            for rule, phrase in self.RULES.items():
                with self.subTest(file=rel, rule=rule):
                    self.assertIn(phrase, sec)

    def test_s10_the_command_in_the_docs_is_the_one_the_launcher_prints(self):
        for rel in self.FILES:
            self.assertIn("git -C /work/repo bundle create - --all", self.section(rel))


if __name__ == "__main__":
    unittest.main()

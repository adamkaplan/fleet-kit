"""The git token's renewal (#1027): the managed reaper's pass renews a running worker's one-hour git token before it ends.

Offline: the fake kubectl, a fake clock, and either the REAL app_token.py against a loopback stand-in for the GitHub API (the mint route the
launch uses, one throwaway RSA key) or a fake mint command. No token is a literal here: every value is assembled at run time.

The families are derived from the code, never listed twice:
  * every `raise _NotRenewed` and every `return False` of the renewal must have a case in the table (a completeness test), and
  * every plain guard of the renewal is removed in turn (one mutant each, built with `ast` from the real source) and the table must fail.
"""
import argparse
import ast
import base64
import datetime as dt
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
import test_app_token as ta  # noqa: E402
cw = base.cw
T0 = base.T0
LAUNCHER = os.path.dirname(HERE)
SOURCE_PATH = os.path.join(LAUNCHER, "cloud_worker.py")

REPO = "acme-org/widget-service"
OTHER = "acme-org/widget-api"
WORKER = "coder7"
RUN = "0123456789abcdef"
FUNCTIONS = ("_renew_git_token", "_worker_record_repo")


def tok(label):
    """A synthetic token assembled at run time (no secret-shaped literal anywhere in the tests)."""
    return "t" + hashlib.sha256(label.encode()).hexdigest()[:36]


OLD, NEW, OTHER_TOKEN = tok("first"), tok("second"), tok("other run")


def slices(value, n=8):
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def load_source():
    with open(SOURCE_PATH) as f:
        return f.read()


def _is_not_renewed_raise(n):
    return isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and getattr(n.exc.func, "id", "") == "_NotRenewed"


def _is_guard(node):
    """An `if` whose whole body is one `return False` or one `raise _NotRenewed(...)`."""
    if not isinstance(node, ast.If) or len(node.body) != 1 or node.orelse:
        return False
    only = node.body[0]
    if isinstance(only, ast.Return):
        return isinstance(only.value, ast.Constant) and only.value.value is False
    return _is_not_renewed_raise(only)


def renewal_functions(tree):
    return [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name in FUNCTIONS]


def guard_nodes(tree):
    return [n for fn in renewal_functions(tree) for n in ast.walk(fn) if _is_guard(n)]


def raise_sites(tree):
    out = []
    for fn in renewal_functions(tree):
        for n in ast.walk(fn):
            if _is_not_renewed_raise(n):
                arg = n.exc.args[0]
                while not isinstance(arg, ast.Constant):   # "text %s" % value, or one text if a condition else another
                    arg = arg.left if isinstance(arg, ast.BinOp) else arg.body
                out.append(arg.value)
    return out


def quiet_sites(tree):
    return [n for fn in renewal_functions(tree) if fn.name == "_renew_git_token" for n in ast.walk(fn)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is False]


def mutant_module(index):
    """The real source with the index-th plain guard removed; (module, number of guards)."""
    tree = ast.parse(load_source())
    guards = guard_nodes(tree)
    if index >= len(guards):
        return None, len(guards)
    target = guards[index]

    class Drop(ast.NodeTransformer):
        def visit_If(self, node):
            if node is target:
                return ast.copy_location(ast.Pass(), node)
            return self.generic_visit(node)
    mod = types.ModuleType("cloud_worker_mutant")
    mod.__file__ = SOURCE_PATH
    exec(compile(ast.fix_missing_locations(Drop().visit(tree)), SOURCE_PATH, "exec"), mod.__dict__)
    return mod, len(guards)


class Harness(base.Base):
    """One running worker with a per-run Secret in the fake cluster, its worker record, a fake mint and a fake clock."""
    cwm = cw   # the module under test (a mutant swaps it)

    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.records = os.path.join(d, "records")
        os.makedirs(self.records)
        self.minted, self.mode_file = os.path.join(d, "mint.log"), os.path.join(d, "mint.mode")
        script = os.path.join(d, "fake-mint.py")
        with open(script, "w") as f:   # the fake mint: counts its calls, then prints a token or fails the way its mode says
            f.write("import os, sys\n"
                    "mode = open(%r).read().strip()\n"
                    "open(%r, 'a').write(os.environ.get('GH_REPO', '') + '\\n')\n"
                    "if mode == 'echo':\n"
                    "    sys.stderr.write('mint refused ' + %r + '\\n'); sys.exit(1)\n"
                    "if mode == 'fail':\n"
                    "    sys.stderr.write('vault says no\\n'); sys.exit(1)\n"
                    "if mode in ('relabel', 'relabel_norv', 'touch', 'recreate', 'delete', 'relaunch'):\n"
                    "    import json as j\n"
                    "    p = os.environ['FAKE_KUBE_STATE']; st = j.load(open(p)); run = %r\n"
                    "    if mode == 'relabel': st['secret_labels'][run] = {'cw/pod': %r, 'cw/run-id': 'f' * 16}; st['secret_rv'][run] = st['secret_rv'].get(run, 1) + 1\n"
                    "    if mode == 'relabel_norv': st['secret_labels'][run] = {'cw/pod': %r, 'cw/run-id': 'f' * 16}\n"
                    "    if mode == 'touch': st['secret_rv'][run] = st['secret_rv'].get(run, 1) + 1\n"
                    "    if mode == 'recreate': st['secret_meta'][run] = dict(st['secret_meta'][run], uid='uid-new')\n"
                    "    if mode == 'delete': del st['secrets'][run]\n"
                    "    if mode == 'relaunch': st['secret_labels'][run] = {'cw/pod': %r, 'cw/run-id': 'f' * 16}; st['secret_rv'][run] = st['secret_rv'].get(run, 1) + 1; st['secret_meta'][run] = dict(st['secret_meta'][run], uid='uid-new'); st['secret_bodies'][run]['git-token'] = %r\n"
                    "    j.dump(st, open(p, 'w'))\n"
                    "sys.stdout.write('two words' if mode == 'space' else %r)\n" % (self.mode_file, self.minted, OLD, self.name + "-run", self.name, self.name, self.name, OTHER_TOKEN, NEW))
        self.set_mode("ok")
        self.env.update(CW_CLOUD_RECORD_DIR=self.records, CW_GIT_TOKEN_CMD="%s %s" % (sys.executable, script),
                        FAKE_KUBE_ECHO=OLD + " " + NEW, FAKE_KUBE_SEEN=os.path.join(d, "seen.jsonl"))
        self.outputs, self.expect_worker = [], WORKER
        self.install_worker()

    def set_mode(self, mode):
        with open(self.mode_file, "w") as f:
            f.write(mode)

    def mints(self):
        try:
            with open(self.minted) as f:
                return f.read().split()
        except OSError:
            return []

    @property
    def name(self):
        return cw.pod_name(WORKER)

    def install_worker(self, repo=REPO, record=True, record_repo=REPO, phase="Running", secret=True, labels=None, run_id=RUN):
        a = argparse.Namespace(image="registry.example.internal/worker:1", shape="S", repo=repo, branch="main", idle="10m", max_hard="8h",
                               stack=False, run_id=run_id, pull_secret=None)
        hard = T0 + dt.timedelta(hours=3)
        pod = cw.render_pod(a, WORKER, self.name, T0, hard, self.env, run_names=("git-token", "model-auth"))
        pod["status"] = {"phase": phase, "conditions": [{"type": "Ready", "status": "True"}]}
        st = self.st()
        st["pods"] = {self.name: pod}
        st["activity"] = {self.name: {"busy": True, "last_activity": cw.iso(T0)}}
        run = self.name + "-run"
        st["secrets"], st["secret_labels"], st["secret_bodies"], st["secret_meta"], st["secret_rv"] = {}, {}, {}, {}, {}
        if secret:
            st["secrets"] = {run: 1, self.name + "-pw": 1}
            st["secret_labels"] = {run: labels if labels is not None else {"cw/pod": self.name, "cw/run-id": run_id}}
            st["secret_bodies"] = {run: {"git-token": OLD, "model-auth": "{}"}}
            st["secret_meta"] = {run: {"creationTimestamp": cw.iso(T0), "uid": "uid-1"}}
        st["fail_cmd"], st["patch_noop"] = None, None
        self.write_state(st)
        rec = os.path.join(self.records, WORKER + ".json")
        if os.path.exists(rec):
            os.remove(rec)
        if record:
            with open(rec, "w") as f:
                json.dump({"repo": record_repo, "branch": "main", "hard_deadline": cw.iso(hard)}, f)

    def launcher(self):
        return self.cwm.Launcher(self.cwm.Kube(self.env), self.env, self.clock)

    def at(self, **kw):
        self.clock.t = T0 + dt.timedelta(**kw)

    def one_pass(self, L=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            res = (L or self.launcher()).reap()
        self.outputs += [out.getvalue(), err.getvalue()]
        return res

    def secret(self):
        st, run = self.st(), self.name + "-run"
        return {"token": st.get("secret_bodies", {}).get(run, {}).get("git-token"), "meta": st.get("secret_meta", {}).get(run, {}),
                "rv": st.get("secret_rv", {}).get(run, 1)}

    def patches(self):
        try:
            with open(self.env["FAKE_KUBE_CALLS"]) as f:
                return [json.loads(l) for l in f if '"patch"' in l]
        except OSError:
            return []

    def audit(self):
        try:
            return self.audit_lines()
        except OSError:
            return []

    def token_age(self):
        meta = self.secret()["meta"]
        stamp = (meta.get("annotations") or {}).get("cw/git-token-minted") or meta["creationTimestamp"]
        return (self.clock() - cw.parse_iso(stamp)).total_seconds()


class RealMint(Harness):
    """The real app_token.py (the launch's own route) against a loopback stand-in for the GitHub API and a throwaway key."""

    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.keyfile = os.path.join(cls.keydir.name, "k.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.keyfile, "2048"], check=True, capture_output=True)
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), ta.Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.keydir.cleanup()

    def setUp(self):
        super().setUp()
        ta.Stub.seen, ta.Stub.redirect, ta.Stub.pad_to, ta.Stub.lookup = [], None, None, None
        ta.Stub.gets, ta.Stub.installs = [], {"acme-org": 678}
        self.answer()
        del self.env["CW_GIT_TOKEN_CMD"]   # the default route: app_token.py OWNER/NAME
        self.env.update(CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile,
                        CW_GITHUB_API_BASE="http://127.0.0.1:%d" % self.srv.server_port)

    def answer(self, repos=None, perms=None, life=3500):
        ta.Stub.answer = {"token": NEW, "expires_at": ta.iso(life), "permissions": dict(perms if perms is not None else ta.PERMS),
                          "repositories": repos if repos is not None else [{"name": "widget-service", "full_name": REPO, "owner": {"id": 9}}]}

    def drive_fake_clock(self, minutes=63):
        """A pass every 60 seconds from T to T+63 minutes: (mints by T+59, the minute the token was swapped, the oldest token age seen)."""
        L = self.launcher()
        by59, swapped, oldest = None, None, 0
        for minute in range(minutes + 1):
            self.at(minutes=minute)
            self.one_pass(L)
            if minute == 59:
                by59 = len(ta.Stub.seen)
            if swapped is None and self.secret()["token"] == NEW:
                swapped = minute
            oldest = max(oldest, self.token_age())
        return by59, swapped, oldest

    def check_s2(self):
        by59, swapped, oldest = self.drive_fake_clock()
        self.assertEqual(by59, 1, "the mint was called %s times by T+59 minutes" % by59)
        self.assertEqual(swapped, 30)
        self.assertEqual(ta.Stub.seen[0][2], {"repositories": ["widget-service"]})   # one repository, the pod's
        self.assertLess(oldest, 3600, "a token was %d seconds old" % oldest)

    def test_s2_a_renewal_before_expiry_with_one_mint_and_a_token_always_under_an_hour_old(self):
        self.check_s2()

    def test_s10_a_mutant_that_renews_only_at_expiry_fails_the_run(self):
        self.addCleanup(setattr, cw, "GIT_TOKEN_RENEW_AFTER_SECONDS", cw.GIT_TOKEN_RENEW_AFTER_SECONDS)
        cw.GIT_TOKEN_RENEW_AFTER_SECONDS = 3660
        with self.assertRaises(AssertionError) as caught:
            self.check_s2()
        self.assertRegex(str(caught.exception), r"seconds old|mint was called")

    def test_s10_a_mutant_that_renews_on_every_pass_fails_the_run(self):
        self.addCleanup(setattr, cw, "GIT_TOKEN_RENEW_AFTER_SECONDS", cw.GIT_TOKEN_RENEW_AFTER_SECONDS)
        cw.GIT_TOKEN_RENEW_AFTER_SECONDS = 0
        with self.assertRaises(AssertionError):
            self.check_s2()

    def test_s11_a_mutant_that_mints_for_another_repository_fails_the_run(self):
        real = cw.git_token_cmd
        self.addCleanup(setattr, cw, "git_token_cmd", real)
        cw.git_token_cmd = lambda repo, env: real(OTHER, env)
        with self.assertRaises(AssertionError):
            self.check_s2()

    def test_s8_a_reply_with_more_or_other_repositories_or_permissions_or_a_longer_life_is_not_written(self):
        two = [{"name": "widget-service", "full_name": REPO, "owner": {"id": 9}}, {"name": "other-service", "full_name": "acme-org/other-service", "owner": {"id": 9}}]
        other = [{"name": "widget-api", "full_name": OTHER, "owner": {"id": 9}}]
        for what, kw in (("two repositories", {"repos": two}), ("another repository", {"repos": other}),
                         ("an extra permission", {"perms": dict(ta.PERMS, administration="write")}), ("a life of over 61 minutes", {"life": 7300})):
            with self.subTest(what):
                self.install_worker()
                self.at(minutes=31)
                self.answer(**kw)
                before = self.secret()
                res = self.one_pass()
                self.assertEqual(self.secret(), before)
                self.assertEqual([(w, c) for w, _, c in res], [(WORKER, 1)])
                self.assertIn("the mint failed", res[0][1])
                self.assertEqual(ta.Stub.seen[-1][2], {"repositories": ["widget-service"]})

    def test_s9_a_vault_lookup_mint_or_secret_failure_keeps_the_old_token_audits_names_the_worker_and_retries(self):
        faults = {
            "the vault read": (lambda: self.env.update(CW_APP_KEY_CMD="false"), lambda: self.env.update(CW_APP_KEY_CMD="cat '%s'" % self.keyfile)),
            "the installation lookup": (lambda: setattr(ta.Stub, "lookup", (404, {"message": "Not Found"})), lambda: setattr(ta.Stub, "lookup", None)),
            "the mint": (lambda: setattr(ta.Stub, "redirect", "http://127.0.0.1:9/x"), lambda: setattr(ta.Stub, "redirect", None)),
            "the Secret patch": (lambda: self.patch_state(fail_cmd="patch"), lambda: self.patch_state(fail_cmd=None)),
        }
        for what, (inject, clear) in faults.items():
            with self.subTest(what):
                self.install_worker()
                self.at(minutes=31)
                inject()
                lines = len(self.audit())
                res = self.one_pass()
                self.assertEqual(self.secret()["token"], OLD)
                self.assertEqual(len(self.audit()), lines + 1)
                last = self.audit()[-1]
                self.assertEqual((last["action"], last["ok"], last["worker"]), ("renew-git-token", False, WORKER))
                self.assertTrue(last["reason"])
                self.assertEqual([(w, c) for w, _, c in res], [(WORKER, 1)])
                clear()
                self.at(minutes=32)
                res = self.one_pass()
                self.assertEqual(self.secret()["token"], NEW)
                self.assertEqual(res, [(WORKER, "git token renewed", 0)])
                self.assertTrue(self.audit()[-1]["ok"])


def text_mutant(old, new):
    """The real source with one piece of text replaced (a mutant the guard remover cannot make); the replaced text must be there."""
    src = load_source()
    assert src.count(old) == 1, "mutation site not found: %r" % old
    mod = types.ModuleType("cloud_worker_text_mutant")
    mod.__file__ = SOURCE_PATH
    exec(compile(src.replace(old, new), SOURCE_PATH, "exec"), mod.__dict__)
    return mod


class DuringTheMint(Harness):
    """A relaunch of the same worker during the mint (same pod name, so the same Secret name): the token minted for the old run
    must never land in the new run's Secret. Each guard has a scenario only it catches (the others are blind to it)."""

    def run_mode(self, mode, state=None):
        self.install_worker()
        if state:
            self.patch_state(**state)
        self.set_mode(mode)
        self.at(minutes=31)
        before = self.secret()
        res = self.one_pass()
        return before, res

    def assert_refused(self, before, res):
        self.assertEqual(self.secret()["token"], before["token"])   # (a refused patch may be attempted: it must not land)
        self.assertEqual([(w, c) for w, _, c in res], [(WORKER, 1)])
        self.assertIn("the secret changed during the mint: not renewed", res[0][1])
        last = self.audit()
        self.assertEqual(len(last), 1)
        self.assertEqual((last[0]["ok"], last[0]["reason"]), (False, "the secret changed during the mint: not renewed"))

    def test_a_relaunch_during_the_mint_leaves_the_new_runs_secret_whole_and_the_next_pass_retries_it(self):
        before, res = self.run_mode("relaunch")
        self.assertEqual(self.secret()["token"], OTHER_TOKEN)   # the other run's own token, untouched
        self.assertEqual([(w, c) for w, _, c in res], [(WORKER, 1)])
        self.assertEqual(len(self.audit()), 1)
        self.assertNotIn(NEW, json.dumps(self.audit()) + json.dumps(res))
        # the new run's Secret is now labelled for the other run and the pod is not: it is left whole, quietly, until the pod follows
        self.set_mode("ok")
        self.at(minutes=32)
        self.assertEqual(self.one_pass(), [])
        self.assertEqual(self.secret()["token"], OTHER_TOKEN)

    def test_relabelled_during_the_mint(self):
        self.assert_refused(*self.run_mode("relabel"))

    def test_touched_during_the_mint(self):
        self.assert_refused(*self.run_mode("touch"))

    def test_deleted_and_made_again_during_the_mint(self):
        self.assert_refused(*self.run_mode("recreate"))

    def test_the_old_state_stays_and_the_next_pass_renews(self):
        before, res = self.run_mode("touch")
        self.set_mode("ok")
        self.at(minutes=32)
        self.assertEqual(self.one_pass(), [(WORKER, "git token renewed", 0)])
        self.assertEqual(self.secret()["token"], NEW)

    def test_the_patch_carries_the_uid_and_resourceversion_that_were_checked_and_the_token_only_on_stdin(self):
        self.install_worker()
        self.at(minutes=31)
        rv = self.secret()["rv"]
        self.assertEqual(self.one_pass(), [(WORKER, "git token renewed", 0)])
        with open(self.state + ".stdin") as f:
            body = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual((body["metadata"]["uid"], body["metadata"]["resourceVersion"]), ("uid-1", str(rv)))
        for call in self.patches():
            self.assertNotIn(NEW, " ".join(call))

    def killed(self, mod, mode):
        """True when the mutant source writes into a Secret that changed during the mint (the real source never does)."""
        self.cwm = mod
        self.install_worker()
        self.set_mode(mode)
        self.at(minutes=31)
        self.one_pass()
        return self.secret()["token"] == NEW

    def test_the_real_source_survives_none_of_the_three_scenarios(self):
        for mode in ("touch", "recreate", "relabel_norv"):
            with self.subTest(mode):
                self.assertFalse(self.killed(cw, mode))

    def test_mutant_without_the_resourceversion_is_caught(self):
        mod = text_mutant(',\n                                        "resourceVersion": meta.get("resourceVersion")}', "}")
        self.assertTrue(self.killed(mod, "touch"))

    def test_mutant_without_the_uid_is_caught(self):
        mod = text_mutant('"uid": meta.get("uid"),', "")
        self.assertTrue(self.killed(mod, "recreate"))

    def test_mutant_without_the_post_mint_label_recheck_is_caught(self):
        mod = text_mutant('            raise _NotRenewed("the secret changed during the mint: not renewed")\n        minted', '            pass\n        minted')
        self.assertTrue(self.killed(mod, "relabel_norv"))


class PersistentFailure(Harness):
    """One audit line per CHANGE of reason, and the success line when it clears; the report still names the worker every pass."""

    def test_a_failure_that_persists_is_written_once_and_a_new_reason_and_the_clearing_are_written_again(self):
        L = self.launcher()
        self.set_mode("fail")
        for minute in (31, 32, 33):
            self.at(minutes=minute)
            res = self.one_pass(L)
            self.assertEqual([(w, c) for w, _, c in res], [(WORKER, 1)])   # reported every pass
        self.assertEqual(len(self.audit()), 1)
        self.set_mode("space")   # another reason
        self.at(minutes=34)
        self.one_pass(L)
        self.assertEqual(len(self.audit()), 2)
        self.at(minutes=35)
        self.one_pass(self.launcher())   # a new process of the reaper reads the same audit file
        self.assertEqual(len(self.audit()), 2)
        self.set_mode("ok")
        self.at(minutes=36)
        self.assertEqual(self.one_pass(L), [(WORKER, "git token renewed", 0)])
        self.assertEqual([a["ok"] for a in self.audit()], [False, False, True])

    def test_the_same_failure_after_a_clearing_is_written_again(self):
        self.set_mode("fail")
        self.at(minutes=31)
        self.one_pass()
        self.set_mode("ok")
        self.at(minutes=32)
        self.one_pass()
        self.set_mode("fail")
        self.at(minutes=93)
        self.one_pass()
        self.assertEqual([a["ok"] for a in self.audit()], [False, True, False])

    def test_a_mutant_that_writes_every_pass_is_caught(self):
        mod = text_mutant('if self._last_renewal_audit(rid) == (False, self.kube.scrub(why, limit=1000)):', 'if False:')
        self.cwm = mod
        self.set_mode("fail")
        for minute in (31, 32):
            self.at(minutes=minute)
            self.one_pass()
        self.assertEqual(len(self.audit()), 2)   # the real source writes one


class Cases(Harness):
    """Every quiet exit, refusal and failure of the renewal, with the fake mint, as one table checked against the code."""

    def cases(self):
        def env(**kw):
            return lambda: self.env.update(**kw)

        def mode(m):
            return lambda: self.set_mode(m)

        def inst(**kw):
            return lambda: self.install_worker(**kw)

        def record_text(text):
            def go():
                with open(os.path.join(self.records, WORKER + ".json"), "w") as f:
                    f.write(text)
            return go

        def bad_worker_name():
            st = self.st()
            st["pods"][self.name]["metadata"]["annotations"]["cw/worker"] = "Bad Name"
            self.write_state(st)
            self.expect_worker = "Bad Name"

        def no_old_token():
            st = self.st()
            del st["secret_bodies"][self.name + "-run"]["git-token"]
            self.write_state(st)

        def no_uid():
            st = self.st()
            del st["secret_meta"][self.name + "-run"]["uid"]
            self.write_state(st)

        return {
            "quiet: the pod is not running": ("quiet", inst(phase="Pending"), ""),
            "quiet: no per-run Secret": ("quiet", inst(secret=False), ""),
            "quiet: the Secret is another pod's": ("quiet", inst(labels={"cw/pod": "other", "cw/run-id": RUN}), ""),
            "quiet: the Secret is another run's": ("quiet", inst(labels={"cw/pod": self.name, "cw/run-id": "f" * 16}), ""),
            "quiet: the Secret holds no git token": ("quiet", no_old_token, ""),
            "quiet: the token is not yet due": ("quiet", lambda: self.at(minutes=5), ""),
            "refused: no record directory": ("refused", lambda: self.env.pop("CW_CLOUD_RECORD_DIR"), "no worker record to read"),
            "refused: a bad worker name": ("refused", bad_worker_name, "bad worker name"),
            "refused: no worker record": ("refused", inst(record=False), "the worker record is missing or unreadable"),
            "refused: an unreadable worker record": ("refused", record_text("{not json"), "the worker record is missing or unreadable"),
            "refused: a record with no repository": ("refused", record_text('{"branch": "main"}'), "the worker record names no repository"),
            "refused: the record names another repository": ("refused", inst(record_repo="acme-org/other-service"), "repository mismatch"),
            "refused: the repository is not allowed": ("refused", env(CW_ALLOWED_REPOS="acme-org/elsewhere"), "repository not on the allow-list"),
            "failed: the mint fails": ("failed", mode("fail"), "the mint failed"),
            "failed: the mint echoes the old token": ("failed", mode("echo"), "the mint failed"),
            "failed: the mint gives a token with a space": ("failed", mode("space"), "whitespace"),
            "refused: the Secret has no uid": ("refused", no_uid, "no uid or resourceVersion"),
            "failed: the Secret changed during the mint (relabelled by a relaunch)": ("failed", mode("relabel"), "the secret changed during the mint"),
            "failed: the Secret was touched during the mint": ("failed", mode("touch"), "the secret changed during the mint"),
            "failed: the Secret was relabelled without a new resourceVersion": ("failed", mode("relabel_norv"), "the secret changed during the mint"),
            "failed: the Secret was deleted and made again during the mint": ("failed", mode("recreate"), "the secret changed during the mint"),
            "failed: the Secret was deleted during the mint": ("failed", mode("delete"), "the secret changed during the mint"),
            "failed: the Secret patch fails": ("failed", lambda: self.patch_state(fail_cmd="patch"), "the Secret patch failed"),
            "failed: the Secret patch is not confirmed": ("failed", lambda: self.patch_state(patch_noop=True), "not confirmed"),
        }

    def run_case(self, name, setup, kind, text):
        self.install_worker()
        self.set_mode("ok")
        self.expect_worker = WORKER
        self.at(minutes=31)
        setup()
        before, lines = self.secret(), len(self.audit())
        res = self.one_pass()
        after = self.secret()
        if kind == "quiet":
            self.assertEqual((self.mints(), res, len(self.audit()), self.patches()), ([], [], lines, []), name)
            self.assertEqual(after, before, name)
            return
        self.assertEqual(after["token"], before["token"], name)   # the old token stays in every refusal and failure
        self.assertEqual([(w, c) for w, _, c in res], [(self.expect_worker, 1)], name)
        self.assertIn(text, res[0][1], name)
        self.assertEqual(len(self.audit()), lines + 1, name)
        last = self.audit()[-1]
        self.assertEqual((last["action"], last["ok"], last["worker"]), ("renew-git-token", False, self.expect_worker), name)
        self.assertIn(text, last["reason"], name)
        if kind == "refused":
            self.assertEqual((self.mints(), self.patches(), after), ([], [], before), name)
        seen = json.dumps(res) + json.dumps(last) + "".join(self.outputs)
        for v in (OLD, NEW):
            self.assertNotIn(v, seen, name)

    def run_all(self):
        snapshot = dict(self.env)
        for name, (kind, setup, text) in self.cases().items():
            with self.subTest(name):
                self.env.clear()
                self.env.update(snapshot)
                self.outputs = []
                for p in (self.minted, self.env["FAKE_KUBE_CALLS"], self.env["CW_CLOUD_AUDIT"]):
                    if os.path.exists(p):
                        os.remove(p)
                self.run_case(name, setup, kind, text)

    def test_every_case_of_the_table_ends_as_it_says(self):
        self.run_all()

    def test_the_table_has_a_case_for_every_refusal_and_quiet_exit_in_the_code(self):
        tree = ast.parse(load_source())
        covered = [t for k, _, t in self.cases().values() if k != "quiet"]
        for site in raise_sites(tree):
            self.assertTrue(any(c in site for c in covered), "no case for: " + site)
        self.assertEqual(len(quiet_sites(tree)), len([n for n in self.cases() if n.startswith("quiet")]) - 2)   # the Secret check has 3 return-False inputs

    def test_a_stamp_from_the_future_is_renewed_not_trusted(self):
        st = self.st()
        st["secret_meta"][self.name + "-run"]["annotations"] = {"cw/git-token-minted": cw.iso(T0 + dt.timedelta(hours=5))}
        self.write_state(st)
        self.at(minutes=5)
        self.assertEqual(self.one_pass(), [(WORKER, "git token renewed", 0)])
        self.assertEqual(self.secret()["token"], NEW)

    def test_the_renewal_has_plain_guards_to_remove(self):
        self.assertGreaterEqual(len(guard_nodes(ast.parse(load_source()))), 8)


class MutantTable(unittest.TestCase):
    """A mutant is killed when ANY case of the table fails for the source with that one guard removed."""

    def test_each_guard_mutant_is_killed(self):
        survivors, total = [], mutant_module(10 ** 6)[1]
        for i in range(total):
            mod, _ = mutant_module(i)
            inner = type("Inner%d" % i, (Cases,), {"cwm": mod})
            result = unittest.TestResult()
            unittest.TestSuite([inner("test_every_case_of_the_table_ends_as_it_says")]).run(result)
            if result.wasSuccessful():
                survivors.append(i)
        self.assertEqual(survivors, [], "guards whose removal no case notices (of %d)" % total)


class Leaks(Harness):
    """S3 and S12: no token, nor any 8-character slice of one, in any output, audit line, record or argument; the new token only on stdin."""

    def secrets_in(self, text):
        return [v[:4] for v in (OLD, NEW) if any(f in text for f in cw.secret_forms(v)) or any(s in text for s in slices(v))]

    def files(self):
        names = ["audit.jsonl", "calls.jsonl", "seen.jsonl", "mint.log"]
        for n in names:
            p = os.path.join(self.tmp.name, n)
            if os.path.exists(p):
                with open(p, errors="replace") as f:
                    yield n, f.read()
        for n in os.listdir(self.records):
            with open(os.path.join(self.records, n)) as f:
                yield n, f.read()

    def test_s3_s12_a_renewal_a_refusal_and_a_failure_leave_no_token_but_on_the_patch_stdin(self):
        self.at(minutes=31)
        self.one_pass()
        self.assertEqual(self.secret()["token"], NEW)
        self.install_worker(record_repo="acme-org/other-service")
        self.at(minutes=62)
        self.one_pass()
        self.install_worker()
        self.set_mode("echo")
        self.at(minutes=93)
        code, out, err = self.run_cli("reap")
        self.assertEqual(code, cw.EXIT_HOOK)
        self.outputs += [out, err]
        self.assertIn("git token not renewed", out)
        for text in self.outputs:
            self.assertEqual(self.secrets_in(text), [])
        for name, body in self.files():
            self.assertEqual(self.secrets_in(body), [], "a token in %s" % name)
        with open(self.state + ".stdin") as f:
            self.assertIn(base64.b64encode(NEW.encode()).decode(), f.read())
        for call in self.patches():
            self.assertEqual(call[-1], "--patch-file=/dev/stdin")
            self.assertEqual(self.secrets_in(" ".join(call)), [])

    def test_a_mutant_that_puts_the_patch_body_in_an_argument_is_caught(self):
        real = cw.Kube.run

        def leaky(self_, args, stdin=None, **kw):   # the body as an argument too
            return real(self_, args + (["--note=" + stdin] if args[:1] == ["patch"] else []), stdin=stdin, **kw)
        self.addCleanup(setattr, cw.Kube, "run", real)
        cw.Kube.run = leaky
        self.at(minutes=31)
        self.one_pass()
        self.assertTrue(self.secrets_in(" ".join(" ".join(c) for c in self.patches())))   # the argument scan of the test above would fail

    def test_a_mutant_that_does_not_scrub_leaves_the_echoed_token_in_the_audit_line(self):
        real = cw.Kube.scrub
        self.addCleanup(setattr, cw.Kube, "scrub", real)
        cw.Kube.scrub = lambda self_, text, limit=300: str(text)
        self.set_mode("echo")
        self.at(minutes=31)
        self.one_pass()
        self.assertTrue(self.secrets_in(json.dumps(self.audit())))


class OneRoute(unittest.TestCase):
    """The launch and the renewal mint through the same function and the same command."""

    def test_the_driver_calls_the_shared_mint_with_the_command_the_renewal_builds(self):
        import run_task
        seen = []
        real = cw.run_git_token_cmd
        self.addCleanup(setattr, cw, "run_git_token_cmd", real)
        cw.run_git_token_cmd = lambda cmd, repo, env, register=None: seen.append((cmd, repo)) or "x"
        plain = {k: v for k, v in os.environ.items() if k != "CW_GIT_TOKEN_CMD"}
        for env in (plain, dict(plain, CW_GIT_TOKEN_CMD="/home/user/bin/print-token --flag")):
            run_task.Driver(argparse.Namespace(worker=WORKER, repo=REPO), env).git_token()
            self.assertEqual(seen[-1], (cw.git_token_cmd(REPO, env), REPO))

    def test_the_default_command_is_app_token_for_the_repository(self):
        self.assertEqual(cw.git_token_cmd(REPO, {}), [sys.executable, os.path.join(LAUNCHER, "app_token.py"), REPO])


class Render(unittest.TestCase):
    """S4: the per-run Secret reaches the pod through a whole-directory projected volume; a subPath mount is never refreshed by the kubelet."""
    CASES = {"default": {}, "stack": {"stack": True}, "pull secret": {"pull_secret": "regcred"}, "shape M": {"shape": "M"}}
    FIXTURE = os.path.join(HERE, "fixtures", "run_secret_pod.json")

    @staticmethod
    def subpaths(node, path=""):
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in ("subpath", "subpathexpr"):
                    out.append(path + "/" + k)
                out += Render.subpaths(v, path + "/" + k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                out += Render.subpaths(v, "%s[%d]" % (path, i))
        return out

    def render(self, **kw):
        a = argparse.Namespace(image="registry.example.internal/worker:1", shape="S", repo=REPO, branch="main", idle="10m", max_hard="8h",
                               stack=False, run_id=RUN, pull_secret=None)
        for k, v in kw.items():
            setattr(a, k, v)
        return cw.render_pod(a, WORKER, cw.pod_name(WORKER), T0, T0 + dt.timedelta(hours=3), {"CW_CLOUD_IMAGE": "x"}, run_names=("git-token", "model-auth"))

    def check(self, pod):
        self.assertEqual(self.subpaths(pod), [])
        mounts = pod["spec"]["containers"][0]["volumeMounts"]
        self.assertEqual([m for m in mounts if m["mountPath"] == "/run/cw"], [{"name": "pw", "mountPath": "/run/cw", "readOnly": True}])
        vol = [v for v in pod["spec"]["volumes"] if v["name"] == "pw"][0]
        items = [i for s in vol["projected"]["sources"] for i in s.get("secret", {}).get("items", []) if i["key"] == "git-token"]
        self.assertEqual(items, [{"key": "git-token", "path": "git-token", "mode": 0o400}])

    def test_every_rendered_pod_has_no_subpath_and_the_git_token_in_the_projected_volume(self):
        for name, kw in self.CASES.items():
            with self.subTest(name):
                self.check(self.render(**kw))

    def test_the_pod_equals_the_stored_fixture_byte_for_byte(self):
        with open(self.FIXTURE) as f:
            self.assertEqual(json.dumps(self.render(), indent=1, sort_keys=True) + "\n", f.read())

    def test_a_mutant_that_mounts_the_token_with_a_subpath_fails_the_check(self):
        pod = self.render()
        pod["spec"]["containers"][0]["volumeMounts"].append({"name": "pw", "mountPath": "/run/cw/git-token", "subPath": "git-token", "readOnly": True})
        with self.assertRaises(AssertionError):
            self.check(pod)


class Docs(unittest.TestCase):
    """S1: the cause is stated first, before the renewal."""
    README = os.path.join(LAUNCHER, "README.md")
    CAUSE = ("one-hour", "minted once at launch", "lose push", "retire-time push")

    @staticmethod
    def verdict(text):
        at = [text.find(w) for w in Docs.CAUSE]
        renewal = text.find("### How the git token is renewed")
        return -1 not in at and renewal != -1 and max(at) < renewal and "## The git token" in text

    def test_the_readme_states_the_cause_before_the_renewal(self):
        with open(self.README) as f:
            self.assertTrue(self.verdict(f.read()))

    def test_a_readme_with_the_renewal_but_no_cause_fails(self):
        with open(self.README) as f:
            text = f.read()
        self.assertFalse(self.verdict(text.replace("minted once at launch", "minted at launch")))
        self.assertFalse(self.verdict(text.replace("## The git token", "## Tokens")))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)

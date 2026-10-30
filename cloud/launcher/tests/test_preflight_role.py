"""Ask #1573: the preflight reads an organization ruleset with a token of its own read-only role (S2 to S7).

Offline: the stub of test_preflight.py, extended so that /orgs/ answers only a token minted for the preflight role; the real preflight.py
runs as a child. The mutants are scratch copies of the real source, each mutation asserted applied.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import http.server
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import preflight  # noqa: E402
import test_driver as td  # noqa: E402
import test_preflight as tp  # noqa: E402

REPO = tp.REPO
WORKER_TOKEN = tp.TOKEN
PF_TOKEN = "ghs_preflightrole" + "Zk4Mn7" * 6   # the planted marker: a token minted for the preflight role
PF_SET = {"administration": "read", "metadata": "read"}
NAMING = "organization ruleset 9: read with the preflight token"


class RoleStub(tp.Stub):
    """/orgs/ answers the ruleset only to the preflight-role token; PF_CODE is what that token gets instead (None: the ruleset)."""
    pf_code = None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/orgs/"):
            tp.Stub.methods.append("GET")
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer " + PF_TOKEN and RoleStub.pf_code is None and path == "/orgs/acme-org/rulesets/9":
                return self.reply(200, {"id": 9, "target": "branch", "enforcement": "active", "bypass_actors": [],
                                        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}})
            return self.reply(RoleStub.pf_code or 403, {})
        return tp.Stub.do_GET(self)


def copy_program(dest, mutate=None):
    """A private copy of the program (its repo-approvals.json too); mutate(source) -> source is applied to preflight.py."""
    os.makedirs(dest)
    for n in ("preflight.py", "app_token.py", "token_mint.py", "repo-approvals.json"):
        shutil.copy(os.path.join(LAUNCHER, n), dest)
    if mutate:
        p = os.path.join(dest, "preflight.py")
        with open(p) as f:
            src = f.read()
        with open(p, "w") as f:
            f.write(mutate(src))
    return os.path.join(dest, "preflight.py")


def mutation(old, new):
    def apply(src):
        assert src.count(old) == 1, old   # the mutation applies exactly once
        out = src.replace(old, new, 1)
        assert out != src
        return out
    return apply


MUT_WORKER_ROLE = mutation("answer = get(base, path, org_read())", "answer = get(base, path, token)")
MUT_NO_FAIL_CLOSED = mutation('''"is needed: see cloud/launcher/README.md (an entry in repo-approvals.json)" % branch)
            return 1''', '''"is needed: see cloud/launcher/README.md (an entry in repo-approvals.json)" % branch)
            return 0''')


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), RoleStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        tp.Stub.rules, tp.Stub.protected, tp.Stub.fail, tp.Stub.methods, tp.Stub.redirect = list(tp.FULL), False, None, [], None
        tp.Stub.review_count, tp.Stub.enforcement, tp.Stub.include, tp.Stub.conditions = 1, "active", ["~DEFAULT_BRANCH"], "ok"
        tp.Stub.exclude, tp.Stub.target, tp.Stub.default, tp.Stub.rule_ids, tp.Stub.extra, tp.Stub.org, tp.Stub.no_id = [], "branch", "main", {}, {}, False, False
        tp.Stub.org_code, tp.Stub.fail_code, tp.Stub.org_extra, tp.Stub.org_hint = None, 500, [], None
        tp.Stub.org_types = list(tp.FULL)   # the protection comes only from organization ruleset 9
        tp.Stub.bypass, tp.Stub.auto_merge = [], False
        RoleStub.pf_code = None
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.n = 0

    def program(self, mutate=None):
        self.n += 1
        return copy_program(os.path.join(self.dir, "p%d" % self.n), mutate)

    def run_pf(self, prog, role=True, approval=False):
        env = dict(os.environ, CW_GITHUB_API_BASE=self.base, CW_PREFLIGHT_TOKEN_CMD="echo " + WORKER_TOKEN)
        env.pop(preflight.ORG_TOKEN_CMD, None)
        if role:
            env[preflight.ORG_TOKEN_CMD] = "echo " + PF_TOKEN
        if approval:
            with open(os.path.join(os.path.dirname(prog), "repo-approvals.json"), "w") as f:
                json.dump({tp.Preflight.KEY: tp.Preflight.ENTRY}, f)
        return subprocess.run([sys.executable, prog, REPO], env=env, capture_output=True, text=True, timeout=60)

    # the two scenario checks, each returning the list of what is wrong (empty: right), so a mutant can be run through the same code
    def s2_failures(self, prog):
        bad = []
        p = self.run_pf(prog)
        named = [l for l in p.stdout.splitlines() if NAMING in l]
        if p.returncode != 0 or len(named) != 1 or "NOT VERIFIED" in p.stdout:
            bad.append("preflight-role run: exit %s, %d naming lines" % (p.returncode, len(named)))
        w = self.run_pf(prog, role=False)
        fails = [l for l in w.stdout.splitlines() if "FAIL" in l]
        if w.returncode != 1 or len(fails) != 1:
            bad.append("worker-role run: exit %s, %d FAIL lines" % (w.returncode, len(fails)))
        return bad

    def s3_failures(self, prog):
        bad = []
        RoleStub.pf_code = 403
        p = self.run_pf(prog)
        if p.returncode != 1:
            bad.append("403: exit %s" % p.returncode)
        p = self.run_pf(prog, approval=True)
        if p.returncode != 0 or p.stdout.count("NOT VERIFIED") != 1:
            bad.append("403 with an entry: exit %s, %d NOT VERIFIED" % (p.returncode, p.stdout.count("NOT VERIFIED")))
        for code in (404, 500):
            RoleStub.pf_code = code
            p = self.run_pf(prog)
            if p.returncode != 2:
                bad.append("%d: exit %s" % (code, p.returncode))
        return bad


class S2Passes(Harness):
    def test_the_preflight_role_token_verifies_an_organization_only_repository(self):
        self.assertEqual(self.s2_failures(self.program()), [])

    def test_the_organization_read_carries_the_preflight_token_and_the_repository_reads_the_workers(self):
        p = self.run_pf(self.program())
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn(PF_TOKEN, p.stdout + p.stderr)
        self.assertNotIn(WORKER_TOKEN, p.stdout + p.stderr)


class S3FailsClosed(Harness):
    def test_403_exits_1_with_an_entry_it_is_loud_and_404_and_500_exit_2(self):
        self.assertEqual(self.s3_failures(self.program()), [])


class MintedRole(unittest.TestCase):
    """The real path (no test command): the preflight mints the role `preflight`, only for the organization read, and falls back as before."""

    def setUp(self):
        for k in ("CW_PREFLIGHT_TOKEN_CMD", preflight.ORG_TOKEN_CMD):
            p = mock.patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(k, None)

    def test_the_organization_token_is_minted_once_for_the_preflight_role_and_lazily(self):
        with mock.patch.object(app_token, "mint", return_value="tok-pf") as m:
            read, state = preflight.org_token_reader(REPO, "tok-worker")
            m.assert_not_called()   # nothing is minted for a repository with no organization ruleset
            self.assertEqual((read(), read()), ("tok-pf", "tok-pf"))
            m.assert_called_once_with(REPO, "preflight")
            self.assertTrue(state["role"])

    def test_a_role_that_cannot_be_minted_falls_back_to_the_workers_token_and_says_so(self):
        with mock.patch.object(app_token, "mint", side_effect=SystemExit(1)), mock.patch("builtins.print") as pr:
            read, state = preflight.org_token_reader(REPO, "tok-worker")
            self.assertEqual(read(), "tok-worker")
            self.assertFalse(state["role"])
            self.assertIn("worker's token", pr.call_args[0][0])


class S4NeverInAPod(td.Driver):
    PLACES = ("pod spec", "per-run Secrets", "pod environment", "audit", "run record")

    def places(self):
        st = self.st()
        pods = st.get("pods", {})
        envs = [c.get("env") for p in pods.values() for c in (p.get("spec") or {}).get("containers", [])]
        calls = ""
        if os.path.exists(self.env["FAKE_KUBE_CALLS"]):
            calls = self.read(self.env["FAKE_KUBE_CALLS"])
        rec = ""
        d = self.denv["CW_RUN_RECORD_DIR"]
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                rec += "".join(self.read(os.path.join(root, f)) for f in sorted(files))
        return {"pod spec": json.dumps(pods), "per-run Secrets": json.dumps(st.get("secrets", {})) + calls,
                "pod environment": json.dumps(envs), "audit": self.read(self.env["CW_CLOUD_AUDIT"]), "run record": rec}

    def test_the_planted_preflight_token_is_in_0_of_5_places(self):
        self.denv["CW_RUN_RECORD_DIR"] = os.path.join(self.tmp.name, "records")
        self.denv[preflight.ORG_TOKEN_CMD] = "echo " + PF_TOKEN   # the launcher's own environment holds it
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        found = {k: v.count(PF_TOKEN) for k, v in self.places().items()}
        self.assertEqual(found, {k: 0 for k in self.PLACES})
        self.assertNotIn(PF_TOKEN, out)

    def test_the_scan_sees_a_marker_that_is_there(self):   # a build that passed the token on would show 1, not 0
        self.denv["CW_RUN_RECORD_DIR"] = os.path.join(self.tmp.name, "records")
        p, out = self.run_driver(FAKE_KUBE_ECHO=PF_TOKEN)
        self.assertGreaterEqual(sum(v.count(PF_TOKEN) for v in self.places().values()) + out.count(PF_TOKEN), 0)
        pods = {"p": {"spec": {"containers": [{"env": [{"name": "X", "value": PF_TOKEN}]}]}}}
        with mock.patch.object(self, "st", return_value={"pods": pods}):
            seen = self.places()
        self.assertEqual((seen["pod spec"].count(PF_TOKEN), seen["pod environment"].count(PF_TOKEN)), (1, 1))

    def test_the_child_never_inherits_the_preflight_token_command_from_the_caller(self):
        import cloud_worker as cw
        env = cw._preflight_env({preflight.ORG_TOKEN_CMD: "echo " + PF_TOKEN, "CW_APP_ID_CMD": "x"}, "/tmp")
        self.assertNotIn(preflight.ORG_TOKEN_CMD, env)
        self.assertNotIn("preflight", " ".join(cw.PREFLIGHT_ENV_NAMES).replace("PREFLIGHT_ENV", ""))


for _name in dir(td.Driver):   # the inherited driver tests are not this file's: only the ones defined above run
    if _name.startswith("test_") and _name not in vars(S4NeverInAPod):
        setattr(S4NeverInAPod, _name, None)


class S5RoleTable(unittest.TestCase):
    def refused(self, perms, role):
        with mock.patch.object(sys, "stderr", open(os.devnull, "w")):
            try:
                app_token.check_permissions(perms, role)
            except SystemExit:
                return True
        return False

    def test_three_refusals_for_the_preflight_role_and_the_earlier_roles_as_before(self):
        self.assertEqual(app_token.PERMISSIONS_BY_ROLE["preflight"], PF_SET)
        self.assertFalse(self.refused(dict(PF_SET), "preflight"))
        cases = {"extra": dict(PF_SET, contents="read"), "missing": {"metadata": "read"}, "level": dict(PF_SET, administration="write")}
        self.assertEqual({k: self.refused(v, "preflight") for k, v in cases.items()}, {k: True for k in cases})
        # a missing key names the owner step
        err = tempfile.TemporaryFile("w+")
        with mock.patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit):
                app_token.check_permissions({"metadata": "read"}, "preflight")
        err.seek(0)
        self.assertIn(app_token.owner_step("preflight"), err.read())
        for role, want in (("worker", {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}),
                           ("orchestrator", {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"})):
            self.assertEqual(app_token.PERMISSIONS_BY_ROLE[role], want)
            self.assertFalse(self.refused(dict(want), role))
            self.assertTrue(self.refused(dict(want, administration="read"), role))
        self.assertTrue(self.refused(dict(PF_SET, contents="write"), "worker"))

    def test_the_mint_requests_exactly_the_preflight_set(self):
        seen = []

        def fake(method, path, jwt, body=None, **kw):
            seen.append(body)
            return {"token": "x", "expires_at": "2999-01-01T00:00:00Z", "permissions": dict(PF_SET)}
        with mock.patch.object(app_token, "github_json", side_effect=fake), mock.patch.object(app_token, "vault", return_value="12345"), \
                mock.patch.object(app_token, "sign", return_value=b"s"), mock.patch.object(app_token, "find_installation", return_value=(1, 9)), \
                mock.patch.object(sys, "stderr", open(os.devnull, "w")):
            with self.assertRaises(SystemExit):   # the stand-in answer is not a full one: only the request body matters here
                app_token.mint(REPO, "preflight")
        self.assertEqual(seen[0]["permissions"], PF_SET)


class S6Mutants(Harness):
    def test_the_worker_role_mutant_fails_s2_and_the_no_longer_failing_closed_mutant_fails_s3(self):
        real = self.program()
        self.assertEqual((self.s2_failures(real), self.s3_failures(real)), ([], []))
        m1 = self.program(MUT_WORKER_ROLE)
        self.assertNotEqual(self.s2_failures(m1), [])
        m2 = self.program(MUT_NO_FAIL_CLOSED)
        self.assertNotEqual(self.s3_failures(m2), [])
        self.assertIn("403: exit 0", self.s3_failures(m2))


class S7Documents(unittest.TestCase):
    def test_the_readme_has_4_of_4_sentences(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split())
        for phrase in ("the `preflight` role", "organization administration: read, with metadata read",
                       "the owner adds `administration: read, metadata: read` to the App's permissions and accepts them on each installation",
                       "A pod never holds this token"):
            self.assertIn(phrase, text)
        self.assertIn("grants", text.split("the `preflight` role", 1)[1][:3000])


HOOK_NAME = "CW_PREFLIGHT_ORG_TOKEN_CMD"
HOOK_MARKER = "echo planted-marker-hook-7f3a9c"
STAND_IN = (
    "import os, sys\n"
    "dump = repr(sorted(os.environ.items()))\n"
    "print('names=%%d marker=%%d' %% (dump.count(%r), dump.count(%r)))\n" % (HOOK_NAME, HOOK_MARKER)
)
RUNNER = (
    "import os, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "sys.path.extend(sys.argv[3:])\n"
    "import cloud_worker as cw\n"
    "cw.PREFLIGHT_ARGV = [sys.executable, sys.argv[2]]\n"
    "line = cw.check_repo_preflight('acme-org/x', dict(os.environ, **{%r: %r}))\n"
    "print(line)\n"
    "sys.exit(0 if line == 'names=0 marker=0' else 1)\n" % (HOOK_NAME, HOOK_MARKER)
)


class HookStaysOutOfTheChild(unittest.TestCase):
    def run_child(self, workdir, mutate=None):
        launcher = os.path.join(workdir, "launcher")
        os.makedirs(launcher)
        shutil.copy(os.path.join(LAUNCHER, "cloud_worker.py"), launcher)   # a scratch copy of the module; its siblings come from the real directory
        if mutate:
            path = os.path.join(launcher, "cloud_worker.py")
            with open(path) as f:
                src = f.read()
            with open(path, "w") as f:
                f.write(mutate(src))
        stand_in = os.path.join(workdir, "stand_in.py")
        with open(stand_in, "w") as f:
            f.write(STAND_IN)
        return subprocess.run([sys.executable, "-c", RUNNER, launcher, stand_in, LAUNCHER, os.path.join(ROOT, "bin")], capture_output=True, text=True, timeout=60)

    def test_child_does_not_see_the_hook_name_or_its_marker_and_a_mutant_passing_it_fails(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "real"))
            real = self.run_child(os.path.join(d, "real"))
            self.assertEqual(real.returncode, 0, real.stdout + real.stderr)
            self.assertEqual(real.stdout.strip(), "names=0 marker=0")
            print("real child: %s" % real.stdout.strip())
            os.makedirs(os.path.join(d, "mut"))
            old = '    "CW_APP_KEY_CMD",\n)'
            def apply(src):
                assert src.count(old) == 1   # the mutation applies exactly once
                out = src.replace(old, '    "CW_APP_KEY_CMD",\n    "%s",\n)' % HOOK_NAME, 1)
                assert out != src
                return out
            mut = self.run_child(os.path.join(d, "mut"), apply)
            print("mutant child: %s (exit %d)" % (mut.stdout.strip(), mut.returncode))
            self.assertEqual(mut.returncode, 1, mut.stdout + mut.stderr)
            self.assertEqual(mut.stdout.strip(), "names=1 marker=1")


if __name__ == "__main__":
    unittest.main()

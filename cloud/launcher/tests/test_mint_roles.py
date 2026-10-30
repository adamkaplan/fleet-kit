"""The mint takes a role from one table of permission sets (ask #1508, slice 2 of ask #1068).

Offline: a loopback stand-in for the GitHub API (the pattern of test_app_token.py), the fake cluster of the orchestrator tests, and the
reaper's renewal harness of test_git_token_renewal.py. Mutants are built from the real source and each must be caught.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import run_task  # noqa: E402
import test_app_token as tam  # noqa: E402
import test_cloud_worker as base  # noqa: E402
import test_git_token_renewal as gtr  # noqa: E402
import test_orchestrator_agent as toa  # noqa: E402

REPO = "acme-org/widget-service"
# The recorded request bodies, written out literally (the worker's is what the mint sent before the table had roles).
WORKER_SET = {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}
ORCH_SET = {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"}
WORKER_TEXT = "contents: write, pull_requests: write, issues: read, metadata: read"
ORCH_TEXT = "issues: write, pull_requests: read, checks: read, metadata: read"
STEP = "the owner adds %s to the App's permissions and accepts them on each installation"
SETS = {"worker": WORKER_SET, "orchestrator": ORCH_SET}
TEXTS = {"worker": WORKER_TEXT, "orchestrator": ORCH_TEXT}


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def mutant(path, old, new):
    src = read(path)
    assert src.count(old) == 1, old   # the mutation is applied exactly once
    mutated = src.replace(old, new, 1)
    assert mutated != src
    mod = types.ModuleType("mutant_" + os.path.basename(path)[:-3])
    mod.__file__ = os.path.join(ROOT, path)
    exec(compile(mutated, mod.__file__, "exec"), mod.__dict__)
    return mod


def table_failures(m):
    """What a module of app_token's shape gets wrong about the two sets (empty for the real one). 'body' is S2, 'check' is S3."""
    bad = []
    for role, want in SETS.items():
        seen = []

        def fake(method, path, jwt, body=None, **kw):
            seen.append(body)
            return {"token": "x", "expires_at": tam.iso(3500), "permissions": dict(want),
                    "repositories": [{"name": "widget-service", "full_name": REPO, "owner": {"id": 9}}]}
        with mock.patch.object(m, "github_json", side_effect=fake), mock.patch.object(m, "vault", return_value="12345"), \
                mock.patch.object(m, "sign", return_value=b"s"), mock.patch.object(m, "find_installation", return_value=(678, 9)), \
                mock.patch.object(sys, "stderr", open(os.devnull, "w")):
            try:
                m.mint(REPO, role)
            except SystemExit:
                pass
        if not seen or list(seen[0]["permissions"].items()) != list(want.items()) or seen[0]["repositories"] != ["widget-service"]:
            bad.append("body " + role)
        cases = [(dict(want), False), (dict(want, administration="write"), True)]
        cases += [({k: v for k, v in want.items() if k != x}, True) for x in want]
        cases += [(dict(want, **{x: "admin"}), True) for x in want]
        if role == "orchestrator":
            cases += [(dict(want, contents="write"), True), (dict(ORCH_SET, contents="write"), True)]
        for perms, rejected in cases:
            with mock.patch.object(sys, "stderr", open(os.devnull, "w")):
                try:
                    m.check_permissions(perms, role)
                    got = False
                except SystemExit:
                    got = True
            if got != rejected:
                bad.append("check %s %s" % (role, sorted(perms.items())))
    return bad


class Fx(unittest.TestCase):
    setUpClass = classmethod(tam.Mint.setUpClass.__func__)
    tearDownClass = classmethod(tam.Mint.tearDownClass.__func__)
    setUp = tam.Mint.setUp

    def run_kind(self, *extra, repo=REPO):
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        return subprocess.run([sys.executable, tam.MINT, repo, *extra], env=e, capture_output=True, text=True, timeout=60)

    def answer_with(self, perms):
        tam.Stub.answer["permissions"] = dict(perms)


class RequestBodies(Fx):
    def test_s2_the_worker_request_is_byte_equal_to_the_recorded_one(self):
        for extra in ((), ("--role", "worker")):
            with self.subTest(extra=extra):
                tam.Stub.seen = []
                p = self.run_kind(*extra)
                self.assertEqual((p.returncode, p.stdout), (0, tam.TOKEN), p.stderr)
                body = tam.Stub.seen[0][2]
                self.assertEqual(json.dumps(body), json.dumps({"repositories": ["widget-service"], "permissions": {
                    "contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}}))
        self.assertEqual(list(app_token.EXPECTED_PERMISSIONS.items()), list(WORKER_SET.items()))
        self.assertIs(app_token.EXPECTED_PERMISSIONS, app_token.PERMISSIONS_BY_ROLE["worker"])

    def test_s2_the_orchestrator_request_is_exactly_its_four_keys(self):
        self.answer_with(ORCH_SET)
        p = self.run_kind("--role", "orchestrator")
        self.assertEqual((p.returncode, p.stdout), (0, tam.TOKEN), p.stderr)
        perms = tam.Stub.seen[0][2]["permissions"]
        self.assertEqual(perms, ORCH_SET)
        self.assertEqual(sorted(perms), ["checks", "issues", "metadata", "pull_requests"])
        self.assertNotIn("contents", perms)
        self.assertEqual(list(app_token.PERMISSIONS_BY_ROLE), ["worker", "orchestrator"])

    def test_s2_the_texts_of_each_kind_equal_the_table(self):
        for role in SETS:
            with self.subTest(role=role):
                self.assertEqual(app_token.render_permissions(None, role), TEXTS[role])
                self.assertEqual(app_token.owner_step(role), STEP % TEXTS[role])
        self.assertEqual(app_token.render_permissions(), WORKER_TEXT)
        self.assertEqual(app_token.owner_step(), STEP % WORKER_TEXT)

    def test_s2_an_unknown_kind_is_rejected_before_any_network_call(self):
        for extra in (("--role", "nope"), ("--role", ""), ("--role", "Worker")):
            with self.subTest(extra=extra):
                tam.Stub.seen, tam.Stub.gets = [], []
                p = self.run_kind(*extra)
                self.assertEqual((p.returncode, p.stdout), (2, ""))
                self.assertEqual((tam.Stub.seen, tam.Stub.gets), ([], []))
        p = self.run_kind("--role")
        self.assertEqual((p.returncode, p.stdout, tam.Stub.gets), (2, "", []))


class Answers(Fx):
    def test_s3_the_orchestrator_answers(self):
        cases = [("contents write", dict(ORCH_SET, contents="write"), "contents"),
                 ("one key more", dict(ORCH_SET, administration="write"), "administration"),
                 ("checks missing", {k: v for k, v in ORCH_SET.items() if k != "checks"}, "lacks the permission: checks"),
                 ("a wrong level", dict(ORCH_SET, issues="read"), "issues")]
        for name, perms, word in cases:
            with self.subTest(name):
                self.answer_with(perms)
                p = self.run_kind("--role", "orchestrator")
                self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
                self.assertIn(word, p.stderr)
                self.assertNotIn(tam.TOKEN, p.stderr)
                if name == "checks missing":
                    self.assertIn(STEP % ORCH_TEXT, p.stderr)
        self.answer_with(ORCH_SET)
        self.assertEqual(self.run_kind("--role", "orchestrator").stdout, tam.TOKEN)

    def test_s3_the_worker_answers_behave_as_before(self):
        cases = [("contents missing", {k: v for k, v in WORKER_SET.items() if k != "contents"}, "lacks the permission: contents"),
                 ("one key more", dict(WORKER_SET, checks="read"), "checks"),
                 ("the orchestrator set", ORCH_SET, "extra permission")]
        for name, perms, word in cases:
            with self.subTest(name):
                self.answer_with(perms)
                p = self.run_kind()
                self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
                self.assertIn(word, p.stderr)
        self.answer_with(WORKER_SET)
        self.assertEqual(self.run_kind().stdout, tam.TOKEN)
        self.assertEqual(table_failures(app_token), [])


class Launch(base.Base):
    def setUp(self):
        super().setUp()
        self.patch_state(secrets={toa.REGISTRY: 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, toa.REGISTRY: {
            "secrets.json": json.dumps(toa.OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}}, hook_result={"pushed": False, "wip_branch": None})

    launch = toa.Orchestrator.launch   # the helpers of the orchestrator tests, without their test methods
    orch = toa.Orchestrator.orch

    def test_s4_the_launch_records_its_kind_and_delivers_the_per_run_secret_as_for_a_worker(self):
        self.orch()
        self.assertEqual(self.pod("orch1")["metadata"]["annotations"]["cw/role"], "orchestrator")
        self.assertIn("channel-secret", self.st()["secret_bodies"][cw.pod_name("orch1") + "-run"])
        self.launch("coder7")
        self.assertNotIn("cw/role", self.pod("coder7")["metadata"]["annotations"])

    def test_s4_the_launch_mint_command_carries_the_kind_for_an_orchestrator_only(self):
        seen = []
        real = cw.run_git_token_cmd
        self.addCleanup(setattr, cw, "run_git_token_cmd", real)
        cw.run_git_token_cmd = lambda cmd, repo, env, register=None: seen.append(cmd) or "x"
        for ns, env, tail in ((dict(agent="orchestrator"), {}, ["--role", "orchestrator"]), (dict(agent="worker"), {}, []), ({}, {}, []),
                              (dict(agent="orchestrator"), {"CW_GIT_TOKEN_CMD": "my-own-mint --x"}, None)):
            with self.subTest(ns=ns, env=env):
                run_task.Driver(argparse.Namespace(worker="coder7", repo=REPO, **ns), dict(env)).git_token()
                if tail is None:
                    self.assertEqual(seen[-1], ["my-own-mint", "--x"])
                else:
                    self.assertEqual(seen[-1], [sys.executable, os.path.join(LAUNCHER, "app_token.py"), REPO] + tail)
        self.assertEqual(cw.git_token_cmd(REPO, {}, "orchestrator")[-2:], ["--role", "orchestrator"])

    PLANTS = {"CW_APP_KEY_CMD": "cat /keys/planted-key-7Qz", "CW_APP_ID_CMD": "echo 424242", "CW_VAULT_SUBSCRIPTION": "sub-planted-7Qz",
              "CW_VAULT_NAME": "vault-planted-7Qz", "CW_GIT_TOKEN_CMD": "mint-planted-7Qz --go"}

    def test_s5_the_orchestrator_spec_and_secret_hold_no_app_key_id_vault_or_mint_command(self):
        self.env.update(self.PLANTS)
        self.orch()
        shown = json.dumps(self.pod("orch1")) + json.dumps(self.st().get("secret_bodies", {}).get(cw.pod_name("orch1") + "-run", {}))
        for name, value in self.PLANTS.items():
            self.assertNotIn(name, shown)
            self.assertNotIn(value, shown)
        self.assertFalse({"CW_APP_KEY_CMD", "CW_APP_ID_CMD", "CW_VAULT_SUBSCRIPTION", "CW_VAULT_NAME", "CW_GIT_TOKEN_CMD", "CW_APP_INSTALLATION_CMD"}
                         & set(cw.ORCHESTRATOR_ENV_NAMES))

    def render(self, mod):
        a = argparse.Namespace(image="registry.example.internal/worker:1", shape="S", repo=REPO, branch="main", idle="10m", max_hard="8h",
                               stack=False, run_id="0123456789abcdef", pull_secret=None, agent="orchestrator", boss="boss1")
        pod = mod.render_pod(a, "orch1", mod.pod_name("orch1"), base.T0, base.T0 + base.dt.timedelta(hours=3), dict(self.env, **self.PLANTS),
                             run_names=("git-token", "model-auth", "channel-secret"))
        return json.dumps(pod)

    def test_s5_and_s6_the_spec_check_passes_the_real_launcher_and_catches_a_pod_that_gets_the_mint_command(self):
        real = self.render(cw)
        self.assertNotIn("planted", real)
        mut = mutant("cloud/launcher/cloud_worker.py", '"CW_AUDIT_RUNTIME_ID": "pod/" + name}',
                     '"CW_AUDIT_RUNTIME_ID": "pod/" + name, "CW_GIT_TOKEN_CMD": env.get("CW_GIT_TOKEN_CMD", "")}')
        self.assertIn("mint-planted-7Qz", self.render(mut))


class Renewal(gtr.RealMint):
    """The reaper renews under the kind the pod's annotation names; the request of the mint is the evidence."""

    def renew(self, role, cwm=None):
        self.install_worker()
        st = self.st()
        notes = st["pods"][self.name]["metadata"]["annotations"]
        self.assertNotIn("cw/role", notes)   # a worker launch writes no annotation: its pod is what it always was
        if role is None:
            notes.pop("cw/role", None)
        else:
            notes["cw/role"] = role
        self.write_state(st)
        if cwm is not None:
            self.cwm = cwm
        self.answer(perms=ORCH_SET if role == "orchestrator" else WORKER_SET)
        self.at(minutes=31)
        self.one_pass()
        self.assertTrue(tam.Stub.seen, "no mint was made")
        return tam.Stub.seen[-1][2]["permissions"]

    def test_s4_the_renewal_mints_the_orchestrator_set_for_an_orchestrator_pod(self):
        self.assertEqual(self.renew("orchestrator"), ORCH_SET)
        self.assertEqual(self.secret()["token"], gtr.NEW)

    def test_s4_a_worker_pod_and_a_pod_without_the_annotation_renew_as_a_worker(self):
        for role in ("worker", None):
            with self.subTest(role=role):
                tam.Stub.seen = []
                self.assertEqual(self.renew(role), WORKER_SET)

    def test_s6_a_renewal_that_mints_the_worker_set_for_an_orchestrator_is_caught(self):
        mut = mutant("cloud/launcher/cloud_worker.py", 'role = ann(pod, "role", "worker")', 'role = "worker"')
        self.assertNotEqual(self.renew("orchestrator", mut), ORCH_SET)


class Mutants(unittest.TestCase):
    def test_s6_the_real_table_has_no_failure(self):
        self.assertEqual(table_failures(app_token), [])

    def test_s6_an_orchestrator_set_with_contents_write_fails_the_request_and_the_check(self):
        m = mutant("cloud/launcher/app_token.py", '"orchestrator": {"issues": "write",', '"orchestrator": {"contents": "write", "issues": "write",')
        bad = table_failures(m)
        self.assertIn("body orchestrator", bad)
        self.assertTrue([b for b in bad if b.startswith("check orchestrator")])

    def test_s6_a_worker_set_that_differs_fails_the_request(self):
        m = mutant("cloud/launcher/app_token.py", '"pull_requests": "write", "issues": "read", "metadata": "read"},\n    "orch',
                   '"pull_requests": "read", "issues": "read", "metadata": "read"},\n    "orch')
        self.assertIn("body worker", table_failures(m))


FAKE_MINT = "#!%s\nimport json, sys\nopen(sys.argv[0] + '.argv', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\nprint('tok-' + sys.argv[1].split('/')[-1])\n" % sys.executable


class LaunchSideMint(unittest.TestCase):
    """bin/cloud_worker.py git_token: the FIRST token of a launch is minted under the launch's kind (a copy of the module sits in a temporary tree
    whose cloud/launcher/app_token.py is a fake that records its argv)."""
    SRC = "bin/cloud_worker.py"

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mint = os.path.join(tmp.name, "cloud", "launcher", "app_token.py")
        os.makedirs(os.path.dirname(self.mint))
        with open(self.mint, "w") as f:
            f.write(FAKE_MINT)
        self.log = self.mint + ".argv"
        self.env = {k: v for k, v in os.environ.items() if k != "CW_GIT_TOKEN_CMD"}

    def load(self, old=None, new=None):
        src = read(self.SRC)
        if old is not None:
            assert src.count(old) == 1, old   # the mutation is applied exactly once
            mutated = src.replace(old, new, 1)
            assert mutated != src
            src = mutated
        mod = types.ModuleType("launch_side_copy")
        mod.__file__ = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(self.mint))), "bin", "cloud_worker.py")
        exec(compile(src, mod.__file__, "exec"), mod.__dict__)
        mod._capture = lambda argv, env=None: (lambda p: (p.returncode, p.stdout, p.stderr))(
            subprocess.run(argv, env=env, capture_output=True, text=True))   # the module's own capture needs the switchboard's names: a plain run here
        return mod

    def run_mint(self, mod, *args, env=None):
        cluster = types.SimpleNamespace(clean=lambda text, n=200: text)
        with mock.patch.dict(os.environ, env if env is not None else self.env, clear=True):
            tok = mod.git_token(REPO, cluster, *args)
        calls = []
        if os.path.exists(self.log):
            calls = [json.loads(line) for line in open(self.log)]
        return tok, calls

    def test_the_first_token_of_an_orchestrator_launch_carries_its_kind_once(self):
        tok, calls = self.run_mint(self.load(), "orchestrator")
        self.assertEqual(tok, "tok-widget-service")
        self.assertEqual(calls, [[REPO, "--role", "orchestrator"]])
        self.assertEqual(calls[0].count("--role"), 1)

    def test_a_worker_launch_mints_with_the_argv_it_always_had(self):
        for extra in ((), ("worker",)):
            with self.subTest(extra=extra):
                if os.path.exists(self.log):
                    os.remove(self.log)
                _, calls = self.run_mint(self.load(), *extra)
                self.assertEqual(calls, [[REPO]])
        mod = self.load()
        seen = []
        mod._capture = lambda argv, env=None: seen.append(argv) or (0, "x\n", "")
        self.run_mint(mod)
        self.assertEqual([[os.path.realpath(a) for a in argv] for argv in seen],
                         [[os.path.realpath(a) for a in (sys.executable, self.mint, REPO)]])

    def test_a_launch_through_a_symlinked_temporary_directory_compares_resolved_paths(self):
        import tempfile
        real = tempfile.TemporaryDirectory()
        self.addCleanup(real.cleanup)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        link = os.path.join(other.name, "linked")
        os.symlink(real.name, link)
        self.mint = os.path.join(link, "cloud", "launcher", "app_token.py")
        os.makedirs(os.path.dirname(self.mint))
        with open(self.mint, "w") as f:
            f.write(FAKE_MINT)
        self.log = self.mint + ".argv"
        mod = self.load()
        seen = []
        mod._capture = lambda argv, env=None: seen.append(argv) or (0, "x\n", "")
        self.run_mint(mod)
        self.assertEqual(len(seen), 1)
        self.assertEqual(os.path.realpath(seen[0][1]), os.path.realpath(self.mint))
        self.assertEqual([os.path.realpath(a) for a in seen[0]],
                         [os.path.realpath(a) for a in (sys.executable, self.mint, REPO)])

    def test_a_custom_command_runs_unchanged(self):
        own = os.path.join(os.path.dirname(self.mint), "own.py")
        with open(own, "w") as f:
            f.write(FAKE_MINT)
        env = dict(self.env, CW_GIT_TOKEN_CMD="%s %s extra" % (sys.executable, own))
        for kind in ("worker", "orchestrator"):
            with self.subTest(kind=kind):
                if os.path.exists(own + ".argv"):
                    os.remove(own + ".argv")
                self.run_mint(self.load(), kind, env=env)
                self.assertEqual([json.loads(x) for x in open(own + ".argv")], [["extra"]])
        self.assertFalse(os.path.exists(self.log))

    def test_the_launch_passes_its_kind_to_the_mint(self):
        src = read(self.SRC)
        self.assertEqual(src.count("token = git_token(repo, cluster, args.agent if args.agent in _lib().AGENTS else \"worker\")"), 1)

    def test_mutant_without_the_kind_in_the_orchestrator_call_is_caught(self):
        mod = self.load('    if not raw and role != "worker":\n        argv += ["--role", role]\n', "")
        _, calls = self.run_mint(mod, "orchestrator")
        self.assertNotEqual(calls, [[REPO, "--role", "orchestrator"]])

LIMIT_A = "A custom mint command (`CW_GIT_TOKEN_CMD`) ignores the role, so the guarantee that an orchestrator never holds contents write holds only for the default mint command."
LIMIT_B = "An orchestrator pod made before this change has no role annotation and renews as a worker until it ends, so it is relaunched to take the narrower set."


class Documents(unittest.TestCase):
    FILES = ["docs/cloud-operator-quickstart.md", "cloud/launcher/README.md", "cloud/README.md"]

    def check(self, text):
        flat = " ".join(text.split())
        miss = [w for w in ("--role", "`cw/role: orchestrator`", "`%s`" % WORKER_TEXT, "`%s`" % ORCH_TEXT,
                            "The App already holds checks: read (the owner confirmed it on 29 October), so the orchestrator set needs no App change today; if a later set needs a permission the App lacks, the owner adds it and accepts it on each installation, never this change.",
                            LIMIT_A, LIMIT_B,
                            "What the orchestrator's token can and cannot do",
                            "It can read issues, pull requests, check results and repository metadata, and it can write to issues only.",
                            "It cannot push, create a branch, read a file, open, edit, approve or merge a pull request, change a workflow, a setting, a secret or a ruleset, or mint a token.",
                            "CW_GIT_TOKEN_CMD") if w not in flat]
        return miss

    def test_s7_every_document_states_the_roles_the_sets_the_step_and_the_paragraph(self):
        for rel in self.FILES:
            with self.subTest(rel):
                self.assertEqual(self.check(read(rel)), [])

    def test_s7_each_limit_sentence_is_found_once_in_every_document(self):
        for rel in self.FILES:
            flat = " ".join(read(rel).split())
            for s in (LIMIT_A, LIMIT_B):
                with self.subTest(rel=rel, s=s[:20]):
                    self.assertEqual(flat.count(s), 1)

    def test_s7_a_document_without_the_first_limit_fails(self):
        text = read(self.FILES[0])
        mutated = text.replace(LIMIT_A, "")
        self.assertNotEqual(mutated, text)
        self.assertTrue(self.check(mutated))

    def test_s7_the_check_notices_a_missing_set_or_paragraph(self):
        text = read(self.FILES[0])
        self.assertTrue(self.check(text.replace(ORCH_TEXT, "")))
        self.assertTrue(self.check(text.replace("it can write to issues only", "")))


if __name__ == "__main__":
    unittest.main()

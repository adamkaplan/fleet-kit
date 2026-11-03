"""Ask #2024, scenarios S2 to S5 and U1: the role worker-workflows for a repository named in CW_WORKFLOWS_REPOS, and only for it.
A loopback stub for GitHub, a throwaway RSA key, real openssl, synthetic names. Every thread is a daemon thread. No assertion on seconds."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import run_task  # noqa: E402
import test_app_token as tam  # noqa: E402  (the stored fake GitHub: Stub, TOKEN, PERMS, iso)
import token_service  # noqa: E402

LISTED = "acme-org/widget-service"
UNLISTED = "acme-org/other-repo"
FIVE = dict(tam.PERMS, workflows="write")
SRC = os.path.join(os.path.dirname(HERE), "app_token.py")


def apply_mutation(original, old, new):
    """The changed text, with the mutation proved applied from the two texts."""
    assert original.count(old) == 1, "the text to change does not occur exactly once"
    changed = original.replace(old, new)
    assert changed != original, "the mutation was not applied"
    assert old in original and old not in changed, "the mutation was not applied"
    return changed


def load(text):
    mod = type(sys)("app_token_copy")
    mod.__file__ = SRC
    exec(compile(text, "app_token_copy", "exec"), mod.__dict__)
    return mod


def refuses(module, perms, role="worker-workflows"):
    """(refused with exit 1, nothing printed on stdout)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            module.check_permissions(perms, role)
    except SystemExit as e:
        return e.code == 1, out.getvalue() == ""
    return False, out.getvalue() == ""


class Minted(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.keyfile = os.path.join(cls.tmp.name, "k.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.keyfile, "2048"], check=True, capture_output=True)
        os.chmod(cls.keyfile, 0o600)
        cls.srv = tam.http.server.HTTPServer(("127.0.0.1", 0), tam.Stub)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.t.join(5)
        cls.tmp.cleanup()

    def setUp(self):
        tam.Stub.seen, tam.Stub.gets, tam.Stub.deletes = [], [], []
        tam.Stub.installs = {"acme-org": 678}
        tam.Stub.lookup = tam.Stub.raw = tam.Stub.pad_to = tam.Stub.redirect = None

    def mint(self, repo, listed, answer_perms):
        """Run the command the launcher and the renewal build for repo; returns (process, the permissions the request asked for or None)."""
        env = {"CW_WORKFLOWS_REPOS": listed} if listed else {}
        cmd = cw.git_token_cmd(repo, env)
        name = repo.split("/")[1]
        tam.Stub.seen = []
        tam.Stub.answer = {"token": tam.TOKEN, "expires_at": tam.iso(3500), "permissions": dict(answer_perms),
                           "repositories": [{"name": name, "full_name": repo, "owner": {"id": 9}}]}
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_INSTALLATION_CMD="echo 678", CW_APP_KEY_CMD="cat '%s'" % self.keyfile,
                 CW_GITHUB_API_BASE="http://127.0.0.1:%d" % self.srv.server_port, CW_OPENSSL="openssl", CW_ALLOWED_REPOS=LISTED + "," + UNLISTED)
        e.pop("CW_WORKFLOWS_REPOS", None)
        p = subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=60)
        posts = [s[2]["permissions"] for s in tam.Stub.seen if s[0].endswith("access_tokens")]
        return p, (posts[0] if posts else None)

    def test_s2_the_listed_repository_asks_for_five_and_any_other_for_the_stored_four(self):
        p, asked = self.mint(LISTED, LISTED, FIVE)
        self.assertEqual((p.returncode, len(asked), asked), (0, 5, FIVE))
        self.assertEqual(p.stdout.strip(), tam.TOKEN)
        p, asked = self.mint(UNLISTED, LISTED, tam.PERMS)
        self.assertEqual((p.returncode, asked), (0, tam.PERMS))
        n = 0
        for repo in (LISTED, UNLISTED):   # the setting empty: both byte-identical to the fixture
            p, asked = self.mint(repo, "", tam.PERMS)
            self.assertEqual((p.returncode, asked), (0, tam.PERMS), repo)
            n += 1
        print("S2: listed request 5 permissions, other request equals the fixture 1 of 1, empty-setting requests equal the fixture %d of 2" % n)

    def test_s2_the_listed_repository_refuses_the_workers_four_as_an_answer(self):
        p, _ = self.mint(LISTED, LISTED, tam.PERMS)
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        p, _ = self.mint(UNLISTED, LISTED, FIVE)   # an unlisted repository refuses the five
        self.assertEqual((p.returncode, p.stdout), (1, ""))


class Check(unittest.TestCase):
    def test_s3_the_answers_are_judged_as_stated(self):
        cases = [("the four only", dict(tam.PERMS), True), ("six", dict(FIVE, administration="write"), True),
                 ("workflows read", dict(tam.PERMS, workflows="read"), True), ("the right five", dict(FIVE), False)]
        for what, perms, want_refused in cases:
            with self.subTest(what):
                got = refuses(app_token, perms)
                self.assertEqual(got, (True, True) if want_refused else (False, True), what)
        print("S3: 4 of 4 answers judged as stated")

    OLD = "        if k not in EXPECTED_PERMISSIONS:\n"
    NEW = "        if False:\n"

    def six_refused_or_fail(self, module):
        ok, _ = refuses(module, dict(FIVE, administration="write"))
        self.assertTrue(ok, "the six-permission answer was accepted")

    def test_s3_the_mutant_without_the_extra_key_test_fails_with_the_anchored_text_and_the_control_passes(self):
        text = open(SRC).read()
        self.six_refused_or_fail(load(text))   # the control first
        mutant = load(apply_mutation(text, self.OLD, self.NEW))
        with self.assertRaises(AssertionError) as caught:
            self.six_refused_or_fail(mutant)
        self.assertEqual(str(caught.exception), "the six-permission answer was accepted")

    def test_s3_a_no_op_mutation_fails_the_computation(self):
        text = open(SRC).read()
        with self.assertRaises(AssertionError) as caught:
            apply_mutation(text, self.OLD, self.OLD)
        self.assertEqual(str(caught.exception), "the mutation was not applied")


class Paths(unittest.TestCase):
    """S4: the launch, the renewal and the token service, for the listed and the unlisted repository."""
    ENV = {"CW_WORKFLOWS_REPOS": LISTED, "CW_ALLOWED_REPOS": LISTED + "," + UNLISTED}

    def launch_cmd(self, repo, env):
        a = type("A", (), {"repo": repo, "agent": "worker"})()
        d = run_task.Driver(a, dict(env))
        seen = []
        with mock.patch.object(cw, "run_git_token_cmd", lambda cmd, r, e: seen.append(list(cmd)) or "tok"):
            d.git_token()
        return seen[0]

    def test_s4_the_launch_and_the_renewal_pass_the_extra_role_only_for_the_listed_repository(self):
        for repo, want in ((LISTED, True), (UNLISTED, False)):
            launch = self.launch_cmd(repo, self.ENV)
            renew = cw.git_token_cmd(repo, self.ENV)
            for cmd in (launch, renew):
                self.assertEqual(cmd[-2:] == ["--role", "worker-workflows"], want, (repo, cmd))
                self.assertEqual("--role" in cmd, want, (repo, cmd))
        print("S4: launch 2 of 2, renewal 2 of 2, no call with the extra role for an unlisted repository")

    def serve(self, repo, role, minted_perms):
        calls = []

        def mint_fn(r, ro):
            calls.append(ro)
            if minted_perms is None:
                raise SystemExit(1)
            return {"token": "x", "permissions": minted_perms}
        body = json.dumps({"repo": repo, "role": role}).encode()
        status, ans, _ = token_service.handle({"X-Caller-Token": "Bearer synthetic"}, len(body), lambda n: body,
                                              lambda t: (True, "system:serviceaccount:cw:cw-operator"), env=dict(self.ENV), mint_fn=mint_fn)
        return status, ans, calls

    def test_s4_the_token_service_mints_five_for_the_listed_repository_and_four_for_another(self):
        status, ans, calls = self.serve(LISTED, "worker-workflows", FIVE)
        self.assertEqual((status, ans["permissions"], calls), (200, FIVE, ["worker-workflows"]))
        status, ans, calls = self.serve(UNLISTED, "worker", tam.PERMS)
        self.assertEqual((status, ans["permissions"], calls), (200, tam.PERMS, ["worker"]))
        status, ans, calls = self.serve(UNLISTED, "worker-workflows", FIVE)   # the extra role for an unlisted repository: refused, nothing minted
        self.assertEqual((status, ans, calls), (403, None, []))
        print("S4: token service 3 of 3 (five for the listed, four for another, the extra role refused for another)")

    def test_s4_each_path_refuses_a_wrong_answer(self):
        for repo, role in ((LISTED, "worker-workflows"), (UNLISTED, "worker")):
            status, ans, _ = self.serve(repo, role, None)
            self.assertEqual((status, ans), (502, None), repo)
            right = FIVE if role == "worker-workflows" else tam.PERMS
            wrong = tam.PERMS if role == "worker-workflows" else FIVE
            self.assertEqual(refuses(app_token, wrong, role), (True, True), repo)
            self.assertEqual(refuses(app_token, right, role), (False, True), repo)

    def test_u1_with_the_setting_empty_nothing_changes(self):
        env = {"CW_ALLOWED_REPOS": LISTED}
        for repo in (LISTED, UNLISTED):
            self.assertEqual(cw.workflows_role(repo, env), "worker")
            self.assertNotIn("--role", cw.git_token_cmd(repo, env))
            self.assertNotIn("--role", self.launch_cmd(repo, env))
        self.assertEqual(app_token.PERMISSIONS_BY_ROLE["worker"], tam.PERMS)


if __name__ == "__main__":
    unittest.main()

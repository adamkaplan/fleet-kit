"""#1019: the cloud token's one permission table includes `issues: read`, so the worker can read its ask and plan.
Offline: a loopback stub of the GitHub API, a throwaway RSA key, the real mint and preflight, the fake kubectl.

Every set here is derived from `app_token.EXPECTED_PERMISSIONS` (one table); each check has a mutant that must be caught.
The Mac shim (shims/app_mint.py) has its own, different set and is not touched."""
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import app_token  # noqa: E402
import test_app_token as tam  # noqa: E402
import test_driver as td  # noqa: E402
import test_preflight as tp  # noqa: E402

TABLE = app_token.EXPECTED_PERMISSIONS
TEXT = "contents: write, pull_requests: write, issues: read, metadata: read"
PLACES = ["cloud/launcher/README.md", "docs/agent-credentials.md", "docs/cloud-guard-parity.md", "docs/cloud-operator-quickstart.md"]
PHRASE_FILES = ["cloud/launcher/README.md", "docs/agent-credentials.md"]
PHRASES = {"read": re.compile(r"read issues and their comments"),
           "not-write": re.compile(r"cannot write issues, labels, merges or settings"),
           "owner step": re.compile(r"add(s)? `issues: read` to the App's permissions.*accept")}


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def states_table(text):
    """The pairs of the table the text does not state (empty when it states every one, as the rendered text)."""
    return [(k, v) for k, v in TABLE.items() if "`%s: %s`" % (k, v) not in text]


def refused(perms, module=app_token):
    """True when module.check_permissions refuses perms (it exits), without printing."""
    try:
        with mock.patch.object(sys, "stderr", io.StringIO()):
            module.check_permissions(perms)
    except SystemExit:
        return True
    return False


def mutant_module(old, new):
    src = open(os.path.join(os.path.dirname(HERE), "app_token.py")).read()
    assert old in src, old
    mod = type(sys)("app_token_mutant")
    mod.__file__ = os.path.join(os.path.dirname(HERE), "app_token.py")
    exec(compile(src.replace(old, new, 1), mod.__file__, "exec"), mod.__dict__)
    return mod


def wrong_levels(name):
    return [v for v in ("read", "write", "admin") if v != TABLE[name]]


def bad_answers():
    """Every kind of wrong answer, derived from the table: a wrong level of each member, a missing member, an extra key."""
    out = []
    for k in TABLE:
        for v in wrong_levels(k):
            out.append(("%s=%s" % (k, v), dict(TABLE, **{k: v})))
        out.append(("missing " + k, {x: y for x, y in TABLE.items() if x != k}))
    out.append(("extra administration", dict(TABLE, administration="write")))
    out.append(("extra checks", dict(TABLE, checks="read")))
    return out


class Fx(unittest.TestCase):
    setUpClass = classmethod(tam.Mint.setUpClass.__func__)
    tearDownClass = classmethod(tam.Mint.tearDownClass.__func__)
    setUp = tam.Mint.setUp
    run_mint = tam.Mint.run_mint


class Table(Fx):
    def test_the_table_is_exactly_the_four_and_the_rendered_text_is_derived_from_it(self):
        self.assertEqual(TABLE, {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"})
        self.assertEqual(app_token.render_permissions(), TEXT)
        self.assertEqual(list(tam.PERMS.items()), list(TABLE.items()))   # the older suite's fixture is the same table

    def test_s1_the_old_three_permission_answer_is_refused_naming_issues_and_the_owner_step(self):
        Stub = tam.Stub
        Stub.answer["permissions"] = {k: v for k, v in TABLE.items() if k != "issues"}
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)
        self.assertIn("issues", p.stderr)
        self.assertNotIn(tam.TOKEN, p.stderr)

    def test_s2_the_four_permission_answer_prints_the_token(self):
        p = self.run_mint()
        self.assertEqual((p.returncode, p.stdout), (0, tam.TOKEN), p.stderr)

    def test_s3_the_request_asks_for_exactly_the_table_and_the_one_repository(self):
        self.run_mint()
        _, _, body = tam.Stub.seen[0]
        self.assertEqual(body, {"repositories": ["widget-service"], "permissions": dict(TABLE)})
        self.assertEqual(len(body["permissions"]), len(TABLE))

    def test_s6_every_missing_member_is_refused_with_its_name_and_the_owner_step_in_one_line(self):
        for k in TABLE:
            with self.subTest(missing=k):
                tam.Stub.answer["permissions"] = {x: y for x, y in TABLE.items() if x != k}
                p = self.run_mint()
                self.assertEqual((p.returncode, p.stdout), (1, ""))
                line = p.stderr.strip()
                self.assertEqual(len(line.splitlines()), 1)
                self.assertIn("lacks the permission: " + k, line)
                self.assertIn("the owner adds " + TEXT + " to the App's permissions and accepts them on each installation", line)

    def test_s6_a_refused_request_says_the_owner_step_and_other_refusals_do_not(self):
        for code, owner in ((403, True), (422, True), (404, False), (500, False)):
            with self.subTest(code=code):
                with mock.patch.object(app_token, "open_url", side_effect=app_token.urllib.error.HTTPError("u", code, "x", {}, None)), \
                        mock.patch.object(app_token, "vault", side_effect=lambda e, s: "12345" if "ID" in e or "INST" in e else "k"), \
                        mock.patch.object(app_token, "sign", return_value=b"s"), \
                        mock.patch.object(app_token, "find_installation", return_value=(678, 9)), \
                        mock.patch.object(sys, "stderr", io.StringIO()) as err:
                    with self.assertRaises(SystemExit):
                        app_token.mint(tam.REAL)
                self.assertEqual("the owner adds " in err.getvalue(), owner, err.getvalue())
                self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_s7_every_wrong_answer_derived_from_the_table_is_refused_with_no_token(self):
        cases = bad_answers()
        self.assertGreaterEqual(len(cases), 6)
        for name, perms in cases:
            with self.subTest(name):
                tam.Stub.answer["permissions"] = perms
                p = self.run_mint()
                self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
                self.assertNotIn(tam.TOKEN, p.stderr)
        for perms in (None, [], "read", {}):   # an answer with no usable permissions object
            if perms is None:
                del tam.Stub.answer["permissions"]
            else:
                tam.Stub.answer["permissions"] = perms
            self.assertEqual(self.run_mint().stdout, "")

    def test_s8_a_mutant_that_accepts_any_level_of_one_member_is_caught_for_each_member(self):
        for k in TABLE:
            with self.subTest(member=k):
                mut = mutant_module("if perms[k] != want:", 'if k != "%s" and perms[k] != want:' % k)
                self.assertFalse(refused(dict(TABLE), mut))                       # the mutant still takes the good answer
                missed = [n for n, p in bad_answers() if not refused(p, mut) and n.startswith(k + "=")]
                self.assertTrue(missed, "no case catches a mutant that accepts any level of " + k)
                self.assertTrue(all(refused(p) for n, p in bad_answers()))                # and the real code refuses every case

    def test_s8_a_mutant_that_forgets_the_extra_key_or_the_missing_key_is_caught(self):
        extra = mutant_module("if k not in EXPECTED_PERMISSIONS:", "if False:")
        self.assertFalse(all(refused(p, extra) for n, p in bad_answers()))
        missing = mutant_module("if k not in perms:", "if k not in perms:\n            continue\n        if False:")
        self.assertFalse(all(refused(p, missing) for n, p in bad_answers() if n.startswith("missing")))


class OneRepoAndLifetimeUnchanged(Fx):
    def test_u1_the_other_repository_and_the_lifetime_refusals_still_hold_beside_the_new_table(self):
        for repos in ([{"name": "other-service"}], [{"name": "widget-service"}, {"name": "other-service"}]):
            tam.Stub.answer["repositories"] = repos
            self.assertEqual(self.run_mint().stdout, "")
        tam.Stub.answer["repositories"] = [{"name": "widget-service", "full_name": tam.REAL, "owner": {"id": 9}}]
        tam.Stub.answer["expires_at"] = tam.iso(62 * 60 + 30)
        self.assertEqual(self.run_mint().stdout, "")
        tam.Stub.answer["expires_at"] = tam.iso(3500)
        self.assertEqual(self.run_mint().stdout, tam.TOKEN)


class Documents(unittest.TestCase):
    def test_s4_every_place_states_every_pair_of_the_table(self):
        for rel in PLACES:
            with self.subTest(rel):
                self.assertEqual(states_table(read(rel)), [])

    def test_s4_the_check_catches_a_place_with_one_member_gone_or_changed(self):
        for rel in PLACES:
            text = read(rel)
            for k, v in TABLE.items():
                with self.subTest(rel=rel, member=k):
                    self.assertEqual(states_table(text.replace("`%s: %s`" % (k, v), "`%s: %s`" % (k, "admin" if v != "admin" else "read"))), [(k, v)])
                    self.assertEqual(states_table(text.replace("`%s: %s`" % (k, v), "")), [(k, v)])

    def test_s4_the_refusal_text_derives_from_the_table(self):
        self.assertIn(TEXT, app_token.owner_step())

    def test_s10_the_two_documents_say_what_is_read_what_is_not_written_and_the_owner_step(self):
        for rel in PHRASE_FILES:
            text = read(rel)
            for name, rx in PHRASES.items():
                with self.subTest(rel=rel, phrase=name):
                    self.assertRegex(text, rx)
                    self.assertIsNone(rx.search(rx.sub("", text)))   # the check fails once the phrase is removed

    def test_s10_the_quickstart_has_the_owner_step_and_the_old_three_permission_text_is_gone_everywhere(self):
        self.assertRegex(read("docs/cloud-operator-quickstart.md"), PHRASES["owner step"])
        old = "`contents: write`, `pull_requests: write`, `metadata: read`"
        for rel in PLACES + ["cloud/README.md"]:
            with self.subTest(rel):
                self.assertNotIn(old, read(rel))

    def test_u2_the_mac_shim_has_its_own_set_and_is_not_the_cloud_table(self):
        shim = read("shims/app_mint.py")
        self.assertNotIn("EXPECTED_PERMISSIONS", shim)
        self.assertNotIn("app_token", shim)


class PreflightLine(unittest.TestCase):
    def test_s5_the_preflight_reports_the_token_permissions_from_the_table_after_a_mint(self):
        import preflight
        tp.Preflight.setUpClass.__func__(self.__class__)
        self.addClassCleanup(tp.Preflight.tearDownClass.__func__, self.__class__)
        tp.Stub.rules, tp.Stub.protected, tp.Stub.fail, tp.Stub.methods, tp.Stub.redirect = list(tp.FULL), False, None, [], None
        tp.Stub.review_count, tp.Stub.enforcement, tp.Stub.include, tp.Stub.conditions = 1, "active", ["~DEFAULT_BRANCH"], "ok"
        tp.Stub.exclude, tp.Stub.target, tp.Stub.default, tp.Stub.rule_ids, tp.Stub.extra, tp.Stub.org, tp.Stub.no_id = [], "branch", "main", {}, {}, False, False
        tp.Stub.org_code, tp.Stub.org_types, tp.Stub.fail_code, tp.Stub.org_extra, tp.Stub.org_hint = None, [], 500, [], None
        env = {k: v for k, v in os.environ.items() if k != "CW_PREFLIGHT_TOKEN_CMD"}
        env["CW_GITHUB_API_BASE"] = self.base
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(preflight.app_token, "mint", return_value=tp.TOKEN), redirect_stdout(out):
            code = preflight.main([tp.REPO])
        lines = [l for l in out.getvalue().splitlines() if "token permissions:" in l]
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(lines, ["preflight: token permissions: " + TEXT])
        self.assertEqual(len(re.findall(r"\w+: (?:read|write)", lines[0])), len(TABLE))
        self.assertNotIn(tp.TOKEN, out.getvalue())

    def test_s5_a_refused_mint_gives_no_ok_and_no_permissions_line(self):
        import preflight
        out = io.StringIO()
        with mock.patch.object(preflight.app_token, "mint", side_effect=SystemExit(1)), redirect_stdout(out), \
                mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "CW_PREFLIGHT_TOKEN_CMD"}, clear=True):
            code = preflight.main([tp.REPO])
        self.assertEqual(code, 2)
        self.assertNotIn("token permissions", out.getvalue())
        self.assertNotIn("preflight ok", out.getvalue())


class NoPod(td.Driver):
    """S6: the driver's own token step runs the REAL mint against the stub that still answers the old set: nothing is created."""

    def setUp(self):
        super().setUp()
        d = self.tmp.name
        key = os.path.join(d, "mint-key.pem")
        subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
        self.srv_cls = tam.Mint
        tam.Mint.setUpClass.__func__(NoPod)
        self.addCleanup(tam.Mint.tearDownClass.__func__, NoPod)
        tam.Stub.seen, tam.Stub.redirect, tam.Stub.pad_to, tam.Stub.lookup, tam.Stub.raw = [], None, None, None, None
        tam.Stub.installs, tam.Stub.gets, tam.Stub.deletes = {"acme-org": 678}, [], []
        tam.Stub.answer = {"token": tam.TOKEN, "expires_at": tam.iso(3500), "repositories": [{"name": "widget-service", "full_name": td.REPO, "owner": {"id": 9}}],
                           "permissions": {k: v for k, v in TABLE.items() if k != "issues"}}
        with open(self.gitcmd, "w") as f:
            f.write('#!/bin/sh\nCW_APP_ID_CMD="echo 12345" CW_APP_INSTALLATION_CMD="echo 678" CW_APP_KEY_CMD="cat \'%s\'" CW_GITHUB_API_BASE="%s" '
                    'exec "%s" "%s" "$GH_REPO"\n' % (key, self.base, sys.executable, os.path.join(os.path.dirname(HERE), "app_token.py")))

    def test_s6_a_token_without_issues_read_refuses_before_any_pod_exists(self):
        p, out = self.run_driver()
        self.assertEqual(p.returncode, td.cw.EXIT_USAGE, out)
        self.assertIn("lacks the permission: issues", out)
        self.assertIn("the owner adds", out)
        self.assertIn("nothing was created", out)
        self.assertEqual((self.st().get("secrets", {}), self.st()["pods"]), ({}, {}))
        self.assertNotIn('"create"', self.read(self.env["FAKE_KUBE_CALLS"]))
        self.assertNotIn(tam.TOKEN, out)
        tam.Stub.answer["permissions"] = dict(TABLE)   # the same driver with the full table mints and goes on
        p, out = self.run_driver()
        self.assertNotIn("lacks the permission", out)


for _n in [n for n in dir(td.Driver) if n.startswith("test")]:
    setattr(NoPod, _n, None)   # only this file's test; the driver's own suite runs elsewhere


if __name__ == "__main__":
    unittest.main()

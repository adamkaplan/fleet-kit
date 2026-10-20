"""#960: the git token's two permission modes (default, read-only). Offline: a loopback stub for the GitHub API, a throwaway RSA key, real openssl.

Every set below is DERIVED from the mint's one table (app_token.PERMISSION_MODES), with a completeness test; each member of each family has a mutant
(the table or a function changed in process) that must make its row show a different result. Members checked, by place a token or permission set is
built, requested or checked: (1) the request body, (2) the answer check, (3) the command line of the mint, (4) the driver's mint call (run_task.py),
(5) the settings row, (6) the docs. The launch wiring (flag, key, audit, refusal, preflight order) is in bin/test-cloud-worker, class TestTokenMode."""
import contextlib
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
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import run_task  # noqa: E402
import cloud_worker as cw  # noqa: E402

MINT = os.path.join(LAUNCHER, "app_token.py")
FIXTURES = os.path.join(HERE, "fixtures")
TOKEN = "ghs_synthetic" + "Ab3Xy9" * 6   # built here, never a real token
REAL = "acme-org/widget-service"
PLANTED = "planted-secret-value-7Qz"
MODES = tuple(app_token.PERMISSION_MODES)
LEVELS = ("read", "write", "admin")
EXTRA_KEYS = ("issues", "administration", "workflows")


def iso(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def answer_rows(mode):
    """(label, answer permissions, accepted) for a mode: derived from the table, one row per member."""
    want = app_token.PERMISSION_MODES[mode]
    rows = [("exact", dict(want), True)]
    for key in want:
        rows.append(("missing " + key, {k: v for k, v in want.items() if k != key}, False))
        for level in LEVELS:
            if level != want[key]:
                rows.append(("%s as %s" % (key, level), dict(want, **{key: level}), False))
        rows.append(("%s as planted level" % key, dict(want, **{key: PLANTED}), False))
    for key in EXTRA_KEYS + tuple(k for other in MODES for k in app_token.PERMISSION_MODES[other] if k not in want):
        rows.append(("extra " + key, dict(want, **{key: "read"}), False))
    for other in MODES:
        if app_token.PERMISSION_MODES[other] != want:
            rows.append(("the set of mode " + other, dict(app_token.PERMISSION_MODES[other]), False))
    rows += [("empty object", {}, False), ("a list", ["contents"], False), ("no object", None, False)]
    return rows


class Stub(http.server.BaseHTTPRequestHandler):
    answer = {}
    seen = []   # raw request bodies, as bytes

    def log_message(self, *a):
        pass

    def do_POST(self):
        Stub.seen.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        out = json.dumps(Stub.answer).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def refused_in_process(mode, perms):
    """Run the real check in this process: (refused, stderr text)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            app_token.check_permissions(perms, mode)
        except SystemExit:
            return True, err.getvalue()
    return False, err.getvalue()


class Mint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.keyfile = os.path.join(cls.tmp.name, "k.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.keyfile, "2048"], check=True, capture_output=True)
        cls.marker = os.path.join(cls.tmp.name, "vault-ran")
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Stub.seen = []
        if os.path.exists(self.marker):
            os.remove(self.marker)
        self.answer(app_token.PERMISSION_MODES["default"])

    def answer(self, perms):
        Stub.answer = {"token": TOKEN, "expires_at": iso(3500), "repositories": [{"name": "widget-service", "full_name": REAL}]}
        if perms is not None:
            Stub.answer["permissions"] = perms

    def run_mint(self, *args, **env):
        e = dict(os.environ, CW_APP_ID_CMD="echo ran >> '%s'; echo 12345" % self.marker, CW_APP_INSTALLATION_CMD="echo 678",
                 CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, PLANTED_SECRET=PLANTED)
        e.update(env)
        return subprocess.run([sys.executable, MINT, *(args or (REAL,))], env=e, capture_output=True, text=True, timeout=60)

    # -- (1) the request body, both modes
    def test_the_request_of_each_mode_is_its_stored_fixture_byte_for_byte(self):
        for mode, name in (("default", "token_request_default.json"), ("read-only", "token_request_read_only.json")):
            with self.subTest(mode=mode):
                Stub.seen = []
                self.answer(app_token.PERMISSION_MODES[mode])
                args = (REAL,) if mode == "default" else (REAL, "--mode", mode)
                p = self.run_mint(*args)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, TOKEN, ""))
                with open(os.path.join(FIXTURES, name), "rb") as f:
                    self.assertEqual(Stub.seen, [("/app/installations/678/access_tokens", f.read())])

    def test_an_explicit_default_mode_sends_the_default_request(self):
        p = self.run_mint(REAL, "--mode", "default")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(FIXTURES, "token_request_default.json"), "rb") as f:
            self.assertEqual(Stub.seen[0][1], f.read())

    def test_request_mutants_each_change_the_request(self):   # one mutant per member of every row: the row changed, the body must change
        for mode, row in app_token.PERMISSION_MODES.items():
            if mode == "default":
                continue
            clean = app_token.request_body("widget-service", mode)
            for key in row:
                for label, mutant in (("drop", {k: v for k, v in row.items() if k != key}), ("raise", dict(row, **{key: "write"})),
                                      ("add", dict(row, issues="read"))):
                    with self.subTest(mode=mode, key=key, mutant=label), mock.patch.dict(app_token.PERMISSION_MODES, {mode: mutant}):
                        self.assertNotEqual(app_token.request_body("widget-service", mode), clean)
        with mock.patch.dict(app_token.PERMISSION_MODES, {"default": dict(app_token.PERMISSION_MODES["default"])}):   # a default that sent its row would differ from the fixture
            with mock.patch.object(app_token, "DEFAULT_MODE", "nothing"):
                self.assertNotEqual(app_token.request_body("widget-service", "default"), {"repositories": ["widget-service"]})

    # -- (2) the answer check: one row each, both modes, through the real program
    def test_the_answer_table_through_the_real_program(self):
        for mode in MODES:
            for label, perms, accepted in answer_rows(mode):
                with self.subTest(mode=mode, row=label):
                    self.answer(perms)
                    p = self.run_mint(*((REAL,) if mode == "default" else (REAL, "--mode", mode)))
                    if accepted:
                        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, TOKEN, ""))
                    else:
                        self.assertNotEqual(p.returncode, 0)
                        self.assertEqual(p.stdout, "")
                        self.assertEqual(len(p.stderr.strip().splitlines()), 1)
                        for secret in (TOKEN, PLANTED):
                            self.assertNotIn(secret, p.stderr)

    def test_each_row_has_a_mutant_that_flips_it(self):
        """One mutant per member (mode, key) and kind: a check built wrongly accepts the row that member guards, or refuses the exact row."""
        for mode in MODES:
            row = app_token.PERMISSION_MODES[mode]
            rows = answer_rows(mode)
            for key in row:
                mutants = {"ignores the key": {k: v for k, v in row.items() if k != key},
                           "wrong level": dict(row, **{key: "admin"}), "any level": None}
                for label, mutant in mutants.items():
                    with self.subTest(mode=mode, key=key, mutant=label):
                        if mutant is None:   # a check that compares keys only
                            original = app_token.check_permissions
                            def keys_only(perms, m=mode, orig=original):
                                if isinstance(perms, dict) and set(perms) == set(app_token.PERMISSION_MODES[m]):
                                    return
                                orig(perms, m)
                            flipped = [lbl for lbl, perms, ok in rows if self.outcome(keys_only, mode, perms) != (not ok)]
                        else:
                            with mock.patch.dict(app_token.PERMISSION_MODES, {mode: mutant}):
                                flipped = [lbl for lbl, perms, ok in rows if self.outcome(app_token.check_permissions, mode, perms) != (not ok)]
                        self.assertTrue(flipped, "no row notices the mutant")

    @staticmethod
    def outcome(check, mode, perms):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                check(perms, mode)
            except SystemExit:
                return True
        return False

    def test_the_table_is_complete_and_sane(self):
        self.assertEqual(set(MODES), {"default", "read-only"})
        self.assertEqual(app_token.PERMISSION_MODES["default"], {"contents": "write", "pull_requests": "write", "metadata": "read"})
        self.assertEqual(app_token.PERMISSION_MODES["read-only"], {"contents": "read", "metadata": "read"})
        self.assertEqual(app_token.EXPECTED_PERMISSIONS, app_token.PERMISSION_MODES["default"])
        self.assertTrue(refused_in_process("nonsense", app_token.PERMISSION_MODES["default"])[0])   # an unknown mode is refused, never read as default

    # -- (3) the command line of the mint
    def test_a_bad_mode_is_a_usage_error_before_any_vault_or_network_call(self):
        bad = [[REAL, "--mode", "write"], [REAL, "--mode", ""], [REAL, "--mode", "Read-Only"], [REAL, "--mode", "read-only", "--mode", "read-only"],
               [REAL, "--mode", "read-only", "--mode", "default"], [REAL, "--mode"], [REAL, "read-only"], [REAL, "--mode=read-only"],
               [REAL, "--mode", "read-only", "extra"], [REAL, "--token-mode", "read-only"], ["--mode", "read-only", REAL], ["--mode", "read-only"]]
        for argv in bad:
            with self.subTest(argv=argv):
                p = self.run_mint(*argv)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertEqual(Stub.seen, [])
                self.assertFalse(os.path.exists(self.marker), "a vault command ran")

    def test_the_mode_is_never_read_from_the_environment(self):
        names = ("CW_TOKEN_MODE", "CW_MODE", "TOKEN_MODE", "CW_MINT_MODE", "CW_APP_TOKEN_MODE", "APP_TOKEN_MODE", "MODE")
        p = self.run_mint(REAL, **{n: "read-only" for n in names})
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(FIXTURES, "token_request_default.json"), "rb") as f:
            self.assertEqual(Stub.seen[0][1], f.read())

    def test_the_source_reads_no_environment_name_for_the_mode(self):
        with open(MINT) as f:
            text = f.read()
        self.assertNotRegex(text, r"environ[^\n]*MODE")
        self.assertNotRegex(text, r"getenv[^\n]*MODE")

    # -- (4) the driver's own mint call
    def driver_cmd(self, mode, env):
        d = run_task.Driver.__new__(run_task.Driver)
        d.a = mock.Mock(repo=REAL, token_mode=mode)
        d.env = env
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=TOKEN + "\n", stderr="")
        with mock.patch.object(run_task.subprocess, "run", fake_run):
            d.git_token()
        return seen

    def test_the_driver_passes_the_mode_as_an_argument_only_when_asked(self):
        self.assertEqual(self.driver_cmd(None, {})[0][2:], [REAL])
        self.assertEqual(self.driver_cmd("default", {})[0][2:], [REAL])
        self.assertEqual(self.driver_cmd("read-only", {})[0][2:], [REAL, "--mode", "read-only"])

    def test_the_driver_refuses_read_only_with_a_custom_command_and_main_does_before_the_preflight(self):
        with self.assertRaises(cw.CloudError):
            self.driver_cmd("read-only", {"CW_GIT_TOKEN_CMD": "echo x"})
        self.assertEqual(self.driver_cmd("default", {"CW_GIT_TOKEN_CMD": "echo x"})[0], ["echo", "x"])
        calls = []
        env = {"CW_GIT_TOKEN_CMD": "echo x", "CW_ALLOWED_REPOS": REAL}
        with mock.patch.object(cw, "check_repo_preflight", lambda *a: calls.append("preflight")), contextlib.redirect_stderr(io.StringIO()) as err:
            code = run_task.main(["w1", REAL, "main", "/dev/null", "--token-mode", "read-only"], env)
        self.assertEqual((code, calls), (2, []))
        self.assertIn("CW_GIT_TOKEN_CMD", err.getvalue())

    def test_the_driver_reads_the_settings_key_when_no_flag_is_given(self):
        """Through the driver's real main, up to the point the mode is resolved (the preflight is made to stop the run)."""
        stop = RuntimeError("stop after the mode is resolved")
        seen = []

        def fake_driver(a, env):
            seen.append(a.token_mode)
            raise stop
        base = {"CW_ALLOWED_REPOS": REAL}
        for flag, key, want in (((), "read-only", "read-only"), (("--token-mode", "default"), "read-only", "default"), ((), None, "default"),
                                (("--token-mode", "read-only"), "default", "read-only")):
            with self.subTest(flag=flag, key=key):
                env = dict(base, **({"CW_TOKEN_MODE": key} if key else {}), CW_KUBE_CONTEXT="ctx")
                with mock.patch.object(cw, "check_repo_preflight", lambda *a: "ok"), mock.patch.object(run_task, "Driver", fake_driver):
                    with self.assertRaises(RuntimeError):
                        run_task.main(["w1", REAL, "main", "/dev/null", *flag], env)
                self.assertEqual(seen[-1], want)
        for bad in ("", "Read-Only", "write"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(run_task.main(["w1", REAL, "main", "/dev/null"], dict(base, CW_TOKEN_MODE=bad, CW_KUBE_CONTEXT="ctx")), 2)
                self.assertIn("CW_TOKEN_MODE", err.getvalue())

    def test_the_driver_flag_choices_are_the_mints_table(self):
        self.assertEqual(set(cw.TOKEN_MODES), set(app_token.PERMISSION_MODES))

    def test_the_driver_mode_changes_the_command(self):
        self.assertNotEqual(self.driver_cmd("read-only", {})[0], self.driver_cmd(None, {})[0])

    # -- (5) the settings row
    def test_the_settings_key_is_a_row_with_a_validator_that_accepts_exactly_the_modes(self):
        self.assertIn("CW_TOKEN_MODE", cw.SETTING_KINDS)
        reason, ok = cw.VALIDATORS["CW_TOKEN_MODE"]
        self.assertEqual([m for m in MODES if ok(m)], list(MODES))
        for bad in ("Read-Only", "write", "read_only", "", "default,read-only", "read-only "):
            self.assertFalse(ok(bad), bad)

    # -- S8: what the permissions the REAL request carries let a worker do
    NEEDS = {"push": {("contents", "write")}, "open a pull request": {("pull_requests", "write")},
             "comment": {("issues", "write"), ("pull_requests", "write")}}

    @classmethod
    def allowed(cls, perms):
        return {act for act, needs in cls.NEEDS.items() if any(perms.get(k) == lvl for k, lvl in needs)}

    def requested(self, mode):
        """The permissions a launch of this mode asks GitHub for (default asks for none: it gets the App's set, which the answer check pins to its row)."""
        return app_token.request_body("widget-service", mode).get("permissions", app_token.PERMISSION_MODES[mode])

    def test_the_read_only_request_allows_none_of_push_open_a_pull_request_or_comment(self):
        self.assertEqual(self.allowed(self.requested("read-only")), set())
        self.assertEqual(self.allowed(self.requested("default")), set(self.NEEDS))

    def test_a_read_only_build_that_widens_the_table_allows_a_named_action(self):
        """The right build asks for the empty set of actions. Mutant A (contents write added to the read-only row) asks for a token that allows exactly
        {push}; B (pull_requests write) {open a pull request, comment}; C (issues write) {comment}. Each is a different concrete result from the right
        build's empty set, taken from the real request body and the real answer check (which then accepts that wider answer instead of refusing it)."""
        row = app_token.PERMISSION_MODES["read-only"]
        for added, expected in ((("contents", "write"), {"push"}), (("pull_requests", "write"), {"open a pull request", "comment"}),
                                (("issues", "write"), {"comment"})):
            with self.subTest(added=added):
                wider = dict(row, **{added[0]: added[1]})
                with mock.patch.dict(app_token.PERMISSION_MODES, {"read-only": wider}):
                    self.assertEqual(self.allowed(self.requested("read-only")), expected)
                    self.assertFalse(self.outcome(app_token.check_permissions, "read-only", wider))   # the wider answer would now be accepted
                self.assertTrue(self.outcome(app_token.check_permissions, "read-only", wider))   # the right build refuses it

    # -- (6) the guard wrapper and the entrypoint are unchanged (the control is the token's own set)
    def test_the_guard_wrapper_and_the_entrypoint_are_unchanged(self):
        digests = {"guard.sh": "9bff815feddd3424f2f853c8e002333474845ab7d8091f562bbd9256852a9321",
                   "entrypoint.sh": "ea05e699672d4ee445181fdafe567ee0caaa495ba4f3a1ce0898aa33b4bcb4d1"}
        for name, digest in digests.items():
            with open(os.path.join(ROOT, "cloud", "worker", name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest, name)

    # -- (7) the docs state what the table says
    def test_the_docs_name_both_modes_their_sets_and_the_refused_actions(self):
        def render(row):
            return ", ".join("`%s: %s`" % kv for kv in row.items())
        for rel in ("docs/cloud-operator-quickstart.md", "cloud/README.md"):
            with open(os.path.join(ROOT, rel)) as f:
                text = f.read()
            with self.subTest(doc=rel):
                for needle in ("default", "read-only", "--token-mode", "CW_TOKEN_MODE", "push", "pull request", "comment", "CW_GIT_TOKEN_CMD"):
                    self.assertIn(needle, text)
        with open(os.path.join(ROOT, "docs", "cloud-operator-quickstart.md")) as f:
            quick = f.read()
        for mode, row in app_token.PERMISSION_MODES.items():
            self.assertIn("`%s`: %s" % (mode, ", ".join("`%s: %s`" % kv for kv in row.items())), quick)
        self.assertIn("/app/installations/{id}/access_tokens", quick)
        self.assertIn("permissions", quick)


class NarrowingSettings(unittest.TestCase):
    """A setting that NARROWS what a credential may do is never read as unset when blank: in the FILE and in the ENVIRONMENT. The set of such rows is
    DERIVED from the flag on the VALIDATORS row; a new one fails the completeness test until it has an environment route here."""

    REQUIRED = ("CW_ALLOWED_REPOS=acme-org/widget-service\nCW_CLOUD_IMAGE=registry.example.internal/acme/worker@sha256:" + "ab12" * 16 +
                "\nCW_CLOUD_SERVER=https://cluster.example.internal:6443\nCW_VAULT_SUBSCRIPTION=00000000-0000-0000-0000-000000000000\n"
                "CW_VAULT_NAME=vault-example\nCW_CLOUD_NAMESPACE=cw\n")
    ENV_ROUTE = {"CW_TOKEN_MODE": lambda v: cw.token_mode_choice(None, {"CW_TOKEN_MODE": v})}
    BLANKS = ("", "   ", "\t", '""', "''", " # a note", "#", "  \"\"  ")

    @staticmethod
    def narrowing():
        return sorted(k for k, row in cw.VALIDATORS.items() if getattr(row, "narrows", False))

    def test_every_narrowing_row_has_an_environment_route(self):
        self.assertTrue(self.narrowing())
        self.assertEqual(self.narrowing(), sorted(self.ENV_ROUTE))

    def test_a_blank_value_in_the_file_is_refused_naming_the_key(self):
        for key in self.narrowing():
            for blank in self.BLANKS:
                with self.subTest(key=key, blank=blank):
                    with self.assertRaises(cw.CloudError) as caught:
                        cw.parse_settings(self.REQUIRED + "%s=%s\n" % (key, blank), "/home/user/cloud.env")
                    self.assertIn(key, str(caught.exception))

    def test_a_blank_value_in_the_environment_is_refused_never_default(self):
        for key in self.narrowing():
            for blank in ("", " ", "\t", "  "):
                with self.subTest(key=key, blank=blank), self.assertRaises(cw.CloudError) as caught:
                    self.ENV_ROUTE[key](blank)
                self.assertIn(key, str(caught.exception))

    def test_a_real_value_still_reads_in_the_file_and_an_absent_line_is_unset(self):
        self.assertEqual(cw.parse_settings(self.REQUIRED + "CW_TOKEN_MODE=read-only\n").get("CW_TOKEN_MODE"), "read-only")
        self.assertNotIn("CW_TOKEN_MODE", cw.parse_settings(self.REQUIRED + "# no line\n"))

    def test_mutant_the_flag_removed_from_a_row_lets_a_blank_through_the_file(self):
        for key in self.narrowing():
            words, test = cw.VALIDATORS[key]
            with self.subTest(key=key), mock.patch.dict(cw.VALIDATORS, {key: (words, test)}):   # a plain row: no flag
                self.assertNotIn(key, cw.parse_settings(self.REQUIRED + "%s=\n" % key))   # the blank is dropped: the broader default would apply

    def test_a_blank_line_that_is_not_narrowing_is_still_unset(self):
        self.assertNotIn("CW_PULL_SECRET", cw.parse_settings(self.REQUIRED + "CW_PULL_SECRET=\n"))


if __name__ == "__main__":
    unittest.main()

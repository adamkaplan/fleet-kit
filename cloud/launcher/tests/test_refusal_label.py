"""#1548: a launch refused because no token could be minted says why, as one label of a closed list and one scrubbed line.

S2 the 8 labels (7 through the launcher, the eighth through the mint's own exit), S3 the scan of every refusal call, S4 the vault firewall with a planted address, S5 hostile
answers, S6 the two mutants, S7 the documentation, U2 nothing created and the same exit status. Offline: a loopback stub of GitHub,
a fake vault (a shell command), no network."""
import ast, http.server, importlib.util, os, re, shutil, stat, sys, tempfile, threading, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import preflight  # noqa: E402
import test_cloud_preflight_wired as w  # noqa: E402

LABELS = ("vault firewall", "vault auth", "vault other", "App key", "installation", "permissions", "GitHub error", "other")
ADDRESS = "203.0.113.77"
FIREWALL = ("(Forbidden) Client address is not authorized and caller is not a trusted service.\n"
            "Client address: %s\nCode: Forbidden" % ADDRESS)
TOKENISH = "ghs_" + "Ab3dE6" * 8
HOSTILE = {"newline": "first line\nsecond line", "escape": "\x1b[31mred\x1b[0m text", "token": "bad " + TOKENISH,
           "address": "from 198.51.100.9 and fe80::1:2:3:4", "identifier": "account 123456789012 refused", "long": "x" * 5000}


class Stub(http.server.BaseHTTPRequestHandler):
    mode = "404"

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if Stub.mode == "post403":
            return self.reply(200, b'{"id": 5, "account": {"login": "acme-org", "id": 9}}')
        self.reply(404 if Stub.mode == "404" else 500)

    def do_POST(self):
        self.reply(403)


def rd(name):
    with open(os.path.join(LAUNCHER, name)) as f:
        return f.read()


def file_with(d, name, text, mode=0o755):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)
    return p


class Mint(w.Wired):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.sig = file_with(d, "sign.sh", "#!/bin/sh\nprintf sig\n")
        self.said = file_with(d, "said.txt", "", 0o644)
        self.fail_vault = file_with(d, "vault.sh", "#!/bin/sh\ncat %s >&2\nexit 1\n" % self.said)

    def mint_env(self, **kw):
        env = {"CW_APP_ID_CMD": "echo 12345", "CW_APP_KEY_CMD": "echo key", "CW_OPENSSL": self.sig,
               "CW_GITHUB_API_BASE": "http://127.0.0.1:%d" % self.srv.server_address[1]}
        env.update(kw)
        hook = mock.patch.object(w.cw, "PREFLIGHT_TEST_ENV", env)
        hook.start()
        self.addCleanup(hook.stop)

    def said_text(self, text):
        with open(self.said, "w") as f:
            f.write(text)

    def refused_with(self, label, **kw):
        self.mint_env(**kw)
        code, out, err = self.drive()
        self.assertEqual(code, 2, err)                      # U2: the exit status of every earlier refusal of the preflight
        self.assertIn("nothing was created", err)
        self.assertIn("no token could be minted (%s): " % label, err)
        self.nothing_made()
        return out, err


class S2EachLabel(Mint):
    def test_seven_of_seven_labels_through_the_launcher(self):
        self.said_text(FIREWALL)
        cases = (("vault firewall", dict(CW_APP_ID_CMD=self.fail_vault)),
                 ("vault auth", dict(CW_APP_ID_CMD=self.fail_vault)),
                 ("vault other", dict(CW_APP_ID_CMD="exit 1")),
                 ("App key", dict(CW_OPENSSL="/usr/bin/false")),
                 ("installation", dict()), ("permissions", dict()), ("GitHub error", dict()))
        modes = {"installation": "404", "permissions": "post403", "GitHub error": "500"}
        for label, env in cases:
            with self.subTest(label):
                Stub.mode = modes.get(label, "404")
                self.said_text("Please run 'az login' to set up an account." if label == "vault auth" else FIREWALL)
                self.refused_with(label, **env)


class S2Other(unittest.TestCase):
    def test_the_eighth_label_reaches_the_preflight_through_the_mints_own_exit(self):
        def fake(target):
            app_token.die("a bad call", "other")
        with mock.patch.dict(os.environ, {"CW_PREFLIGHT_TOKEN_CMD": ""}), mock.patch.object(app_token, "mint", fake), \
                mock.patch("sys.stderr"):
            with self.assertRaises(preflight.Unreadable) as e:
                preflight.get_token("acme-org/widget-service")
        self.assertEqual(str(e.exception), "no token could be minted (other): a bad call")


class S4Firewall(Mint):
    def test_the_planted_address_is_in_0_places(self):
        self.said_text(FIREWALL)
        out, err = self.refused_with("vault firewall", CW_APP_ID_CMD=self.fail_vault)
        self.assertIn("the vault refused this machine's address", err)
        self.assertIn("the vault owner adds it", err)
        found = {"stdout": out.count(ADDRESS), "stderr": err.count(ADDRESS), "files": 0}
        for root, _, names in os.walk(self.tmp.name):   # the audit and the run record are files under the run's own directory
            for n in names:
                if n != "said.txt":
                    with open(os.path.join(root, n), "rb") as f:
                        found["files"] += f.read().count(ADDRESS.encode())
        self.assertEqual(found, {"stdout": 0, "stderr": 0, "files": 0})
        self.assertNotIn("Forbidden", err)


def hostile_failures(pf, at=app_token):
    """The hostile mint answers through get_token of `pf`: the list of the cases that print a raw character or a long line."""
    bad = []
    for name, text in HOSTILE.items():
        def fake(target, text=text):
            at.die(text, "permissions")
        with mock.patch.dict(os.environ, {"CW_PREFLIGHT_TOKEN_CMD": ""}), mock.patch.object(at, "mint", fake), \
                mock.patch("sys.stderr"):
            try:
                pf.get_token("acme-org/widget-service")
            except pf.Unreadable as e:
                said = str(e)
        head = "no token could be minted (permissions): "
        rest = said[len(head):] if said.startswith(head) else said
        raw = ("\n" in said or "\x1b" in said or TOKENISH in said or "198.51.100.9" in said or "fe80::1" in said
               or "123456789012" in said or len(rest) > 160 or not said.startswith(head))
        if raw:
            bad.append(name)
    return bad


class S5Hostile(unittest.TestCase):
    def test_six_of_six_hostile_answers_print_one_short_clean_line(self):
        self.assertEqual(hostile_failures(preflight), [])

    def test_a_hostile_vault_answer_is_not_printed_through_the_vault_read(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name, text in HOSTILE.items():
            said = file_with(d, name + ".txt", text, 0o644)
            env = {"CW_APP_ID_CMD": "cat %s >&2; exit 1" % said}
            with mock.patch.dict(os.environ, env), mock.patch("sys.stderr") as err:
                with self.assertRaises(SystemExit):
                    app_token.vault("CW_APP_ID_CMD", "cw-app-id")
            shown = "".join(str(c.args[0]) for c in err.write.call_args_list)
            self.assertNotIn(text[:20], shown, name)
            self.assertEqual(app_token.REFUSAL["label"], "vault other", name)


class S6Mutants(unittest.TestCase):
    def mutant(self, src, old, new, name):
        text = rd(src)
        self.assertEqual(text.count(old), 1, "the mutation was not applied")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = file_with(d, src, text.replace(old, new), 0o644)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_raw_line_printer_fails_s5(self):
        pf = self.mutant("preflight.py", "    return scrub(line, (), REFUSAL_LINE_MAX)", "    return str(app_token.REFUSAL.get('line'))", "pf_mut")
        self.assertNotEqual(hostile_failures(pf), [])

    def test_the_all_other_label_fails_s2(self):
        at = self.mutant("app_token.py", 'label=label if label in LABELS else "other"', 'label="other"', "at_mut")
        with mock.patch.dict(os.environ, {"CW_OPENSSL": "/usr/bin/false"}), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                at.sign("key", b"data")
        self.assertNotEqual(at.REFUSAL["label"], "App key")


class S3Scan(unittest.TestCase):
    def test_n_of_n_refusal_calls_carry_a_label_of_the_closed_list(self):
        tree = ast.parse(rd("app_token.py"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "die"]
        bare = [n.lineno for n in calls if len(n.args) < 2 or not (isinstance(n.args[1], ast.Constant) and n.args[1].value in LABELS)]
        self.assertGreaterEqual(len(calls), 30)
        self.assertEqual(bare, [], "%d of %d refusal calls carry no label of the closed list" % (len(bare), len(calls)))
        self.assertEqual(set(LABELS), set(app_token.LABELS))

    def test_the_preflight_matches_no_free_text_of_the_mint(self):
        src = ast.get_source_segment(rd("preflight.py"),
                                     next(n for n in ast.parse(rd("preflight.py")).body
                                          if isinstance(n, ast.FunctionDef) and n.name == "refusal_label"))
        self.assertNotRegex(src, r"re\.|stderr|\.find\(|\bin line\b")


class S7Docs(unittest.TestCase):
    def test_eight_of_eight_labels_are_in_the_readme_with_their_line(self):
        text = rd("README.md")
        for label in LABELS:
            self.assertRegex(text, r"(?m)^\| `%s` \| .+ \| .+ \|$" % re.escape(label), label)


if __name__ == "__main__":
    unittest.main()

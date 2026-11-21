"""A GitHub write never carries a private name (#34): the same list `bin/check-public-names` reads. HOST is bin/test-switchboard."""
import os
import unittest
from unittest import mock

HOST = None   # set by bin/test-switchboard


class TestPublicNamesGuard(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.audit = mock.Mock()
        mod = HOST.sb
        self.sb = mod
        self.write = mod.github_audited(lambda repo, body: (self.audit, "cli", "POST", "repos/%s/issues/1/comments" % repo, repo, 1))(
            lambda repo, body: self.calls.append(body) or "ok")

    def test_private_term_refused_without_echo(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "# c\nfoo\ncase:Bar+\n"}):
            with self.assertRaises(self.sb.IntentError) as ctx:
                self.write("acme/api", "ok\nhas FOO in it")
            self.assertIn("term #1", str(ctx.exception))
            self.assertNotIn("FOO", str(ctx.exception))
            with self.assertRaises(self.sb.IntentError) as ctx:
                self.write("acme/api", {"title": "Barr"})
            self.assertIn("term #2", str(ctx.exception))
            self.assertEqual(self.calls, [])
            self.audit.append.assert_not_called()

    def test_public_words_pass_and_builtin_private_shapes_refuse(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "\\bfleetkit\\b\nacme-secret\nspire-floor-acme\n"}):
            for ok in ("FleetKit", "fleetkit", "fleet-cloud", "spire-floor", "<!-- fleet-switchboard:from=coder -->"):
                self.assertEqual(self.write("acme/api", ok), "ok")
            for bad in ("12345678-1234-1234-1234-123456789abc", "x.vault.azure.net", "fkcwdemo", "acme-secret", "spire-floor-acme"):
                with self.assertRaises(self.sb.IntentError):
                    self.write("acme/api", bad)

    def test_user_fleet_label_exempt(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "janedoe\n"}):
            self.assertEqual(self.write("acme/api", "janedoe:awaiting-cos"), "ok")
            self.assertEqual(self.write("acme/api", ["gh", "issue", "edit", "1", "--add-label", "janedoe:hold-security"]), "ok")
            with self.assertRaises(self.sb.IntentError):
                self.write("acme/api", "assigned to janedoe")

    def test_builtin_namespace_patterns_refused(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "unrelated-term\n"}):
            for bad in ("cw-floor-" + "kata", "cw-floor-" + "kata-operator-binding", "jan" + "us-infra/repo"):
                for repo in ("acme/api", "acme/" + bad):
                    with self.assertRaises(self.sb.IntentError):
                        self.write(repo, bad)
            self.assertEqual(self.write("acme/api", "spire-floor"), "ok")
            with self.assertRaises(self.sb.IntentError):
                self.write("acme/api", "cw-floor-" + "kata:parked")

    def test_direct_gh_path_blocked(self):
        for cmd in ("/opt/homebrew/bin/gh issue comment 1 -b hi", "/usr/local/bin/gh api repos/acme/web/issues"):
            self.assertTrue(self.sb.repo_commissioning_rule(cmd))
        self.assertIsNone(self.sb.repo_commissioning_rule("gh issue comment 1 -b hi"))
        self.assertIsNone(self.sb.repo_commissioning_rule("gh pr view 5 -R acme/api"))
        self.assertIsNone(self.sb.repo_commissioning_rule("gh api repos/acme/web/issues"))

    def test_private_repo_skips_public_names_guard(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "private-org\\b\nsecret-word\n"}):
            self.assertEqual(self.write("private-org/internal-repo", "body with secret-word"), "ok")
            with self.assertRaises(self.sb.IntentError):
                self.write("private-org/internal-repo", "12345678-1234-1234-1234-123456789abc")
            for bad in ("body with private-org", "body with secret-word"):
                with self.assertRaises(self.sb.IntentError):
                    self.write("acme/api", bad)

    def test_case_sensitive_entry_and_clean_text_pass(self):
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "case:Bar\n"}):
            self.assertEqual(self.write("acme/api", "a bar is fine"), "ok")
        self.assertEqual(len(self.calls), 1)

    def test_no_list_under_test_writes(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_PRIVATE_NAMES")}
        env["FLEET_SWITCHBOARD_UNDER_TEST"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.write("acme/api", "anything"), "ok")

    def test_bad_entry_and_missing_named_file_refuse(self):
        for env in ({"FLEET_PRIVATE_NAMES": "case:Bar("}, {"FLEET_PRIVATE_NAMES": "", "FLEET_PRIVATE_NAMES_FILE": "/nonexistent/names.txt"}):
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(self.sb.IntentError) as ctx:
                    self.write("acme/api", "clean")
                self.assertNotIn("Bar(", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_a_first_positional_payload_is_scanned(self):
        write = self.sb.github_audited(lambda argv: (self.audit, "cli", "POST", "repos/acme/api/issues/1/comments", "acme/api", 1))(lambda argv: self.calls.append(argv))
        with mock.patch.dict(os.environ, {"FLEET_PRIVATE_NAMES": "foo"}):
            with self.assertRaises(self.sb.IntentError):
                write(["gh", "issue", "comment", "--body", "Foo"])
        self.assertEqual(self.calls, [])

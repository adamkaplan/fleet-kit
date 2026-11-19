"""A GitHub write never carries a private name (#34): the same list `bin/check-public-names` reads. HOST is bin/test-switchboard."""
import os
import unittest
from unittest import mock

HOST = None   # set by bin/test-switchboard


class TestPublicNamesGuard(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.audit = mock.Mock()
        mod = HOST
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

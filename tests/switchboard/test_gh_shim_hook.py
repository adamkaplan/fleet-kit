"""#2397: the gh shim allows ONE webhook write, DELETE repos/O/R/hooks/ID, and only for a `cli` hook whose config.url is the webhook forwarder's url: the shim reads the hook first
(shims/gh_body.py, the pre-read style of the body guard) with the account the delete would use. Fakes only: no test calls GitHub. The control is the shim of origin/integration."""
import json
import os
import subprocess
import unittest

from test_gh_shim_body import BodyRig, load_module
from test_gh_shim_sign import SHIM_DIR, REPO

URL = "https://webhook-forwarder.github.com/hook"
HOOK = "repos/o/r/hooks/7"

FAKE_REAL_GH_HOOKS = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
home = os.environ["FAKE_DIR"]
if argv[:1] == ["api"] and len(argv) == 2:
    with open(os.path.join(home, "reads.jsonl"), "a") as out:
        out.write(json.dumps({"argv": argv, "token": os.environ.get("GH_TOKEN", "")}) + "\\n")
    mode = open(os.path.join(home, "mode")).read()
    if mode == "fail":
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    if mode == "junk":
        print("not json")
        sys.exit(0)
    print(open(os.path.join(home, "hook")).read())
    sys.exit(0)
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "stdin": ""}) + "\\n")
"""


def hook(name="cli", url=URL):
    return json.dumps({"id": 7, "name": name, "active": True, "config": {"url": url}})


class HookRig(BodyRig):
    def __init__(self, shim_source=None, gh_body_source=None):
        BodyRig.__init__(self, gh_body_source, shim_source)
        (self.bin / "gh").write_text(FAKE_REAL_GH_HOOKS)

    def go(self, args, hook_text=None, mode="ok", stdin=None):
        (self.home / "hook").write_text(hook_text if hook_text is not None else hook())
        (self.home / "current").write_text("")
        (self.home / "mode").write_text(mode)
        reads = self.home / "reads.jsonl"
        if reads.exists():
            reads.unlink()
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (self.shims, self.bin), "HOME": str(self.home), "TMPDIR": str(self.home), "FAKE_GH_LOG": str(self.log),
               "FAKE_DIR": str(self.home), "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SWITCHBOARD_BIN": str(self.sb), "FLEET_SHIM_LOOKUP_SECONDS": "10"}
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run(["python3", str(self.shims / "gh"), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        seen = [json.loads(l) for l in reads.read_text().splitlines()] if reads.exists() else []
        return done, calls, seen


def delete(path=HOOK, *more):
    return ["api", "-X", "DELETE", path, *more]


class TestGhShimHookDelete(unittest.TestCase):
    def setUp(self):
        self.rig = HookRig()
        self.addCleanup(self.rig.close)

    def test_a_cli_hook_with_the_forwarder_url_is_deleted_after_a_read(self):
        done, calls, seen = self.rig.go(delete())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([c["argv"] for c in calls], [delete()])
        self.assertEqual([s["argv"] for s in seen], [["api", HOOK]], "the hook was read first, as a GET")

    def test_another_name_another_url_or_an_unreadable_hook_is_refused(self):
        for label, kwargs in (("name", {"hook_text": hook(name="web")}), ("url", {"hook_text": hook(url="https://example.invalid/hook")}),
                              ("url prefix", {"hook_text": hook(url=URL + "/x")}), ("no config", {"hook_text": json.dumps({"name": "cli"})}),
                              ("not an object", {"hook_text": "[]"}), ("read fails", {"mode": "fail"}), ("not json", {"mode": "junk"})):
            with self.subTest(label):
                done, calls, _ = self.rig.go(delete(), **kwargs)
                self.assertEqual(calls, [])
                self.assertEqual(done.returncode, 1)
                self.assertIn("refused", done.stderr)
                self.assertNotIn("GH_TOKEN", done.stderr)

    def test_a_read_that_fails_says_why_and_never_shows_a_token(self):
        done, calls, _ = self.rig.go(delete(), mode="fail")
        self.assertIn("could not be read", done.stderr)
        self.assertEqual(len([l for l in done.stderr.splitlines() if l.strip()]), 1)

    def test_other_methods_and_other_endpoints_stay_refused_with_no_read_of_the_hook(self):
        for argv in (["api", "-X", "POST", HOOK], ["api", "-X", "PATCH", HOOK, "-f", "active=false"], ["api", "-X", "PUT", HOOK],
                     delete("repos/o/r/hooks/abc"), delete("repos/o/r/hooks/7/config"), delete("repos/o/r/hooks"), delete("repos/o/r/hooks/7x"),
                     delete("repos/o/r/hooks/%37"), delete("repos/o/r/hooks/../hooks/7"), delete("repos/o/r/Hooks/7"), delete("orgs/o/hooks/7"),
                     delete("repos/o/r/issues/7"), delete("repos/o/r/git/refs/heads/x"), delete("repos/o/r"), delete("repos/o/r/hooks/7/"),
                     delete("repositories/9/hooks/7")):
            with self.subTest(argv=argv):
                done, calls, _ = self.rig.go(argv)
                self.assertEqual(calls, [], done.stderr)
                self.assertEqual(done.returncode, 1)

    def test_a_full_url_with_a_hostname_is_refused_so_the_read_is_of_the_hook_deleted(self):
        for argv in (["api", "--hostname", "ghe.example.com", "-X", "DELETE", "https://api.github.com/" + HOOK],
                     ["api", "-X", "DELETE", "--hostname", "ghe.example.com", "//api.github.com/" + HOOK]):
            with self.subTest(argv=argv):
                done, calls, _ = self.rig.go(argv)
                self.assertEqual(calls, [])
                self.assertEqual(done.returncode, 1)

    def test_the_other_spellings_of_the_host_are_read_first_too(self):
        for path in ("https://api.github.com/" + HOOK, "http://api.github.com/" + HOOK, "//api.github.com/" + HOOK, "/" + HOOK, HOOK + "?x=1"):
            with self.subTest(path=path):
                done, calls, seen = self.rig.go(delete(path), hook_text=hook(name="web"))
                self.assertEqual(calls, [], "a hook that is not the forwarder's passed")
                self.assertEqual(len(seen), 1, "it was read first")
                done, calls, seen = self.rig.go(delete(path))
                self.assertEqual(len(calls), 1, done.stderr)

    def test_a_label_named_hooks_is_still_deleted_as_before(self):
        done, calls, _ = self.rig.go(delete("repos/o/r/labels/hooks"))
        self.assertEqual(len(calls), 1, done.stderr)

    def test_a_read_of_a_hook_is_not_a_write_and_is_not_guarded(self):
        done, calls, seen = self.rig.go(["api", HOOK], hook_text=hook(name="web"))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_url_matches_the_daemons(self):
        text = (SHIM_DIR.parent / "bin" / "fleet-switchboard").read_text()
        self.assertIn('GITHUB_FORWARDER_HOOK_URL = "%s"' % URL, text)
        self.assertEqual(load_module().FORWARDER_HOOK_URL, URL)

    def test_a_missing_guard_module_refuses_the_delete(self):
        (self.rig.shims / "gh_body.py").unlink()
        done, calls, _ = self.rig.go(delete())
        self.assertEqual(calls, [])
        self.assertEqual(done.returncode, 1)


def today(name):
    return subprocess.run(["git", "show", "origin/integration:shims/" + name], capture_output=True, text=True, cwd=str(SHIM_DIR.parent), check=True).stdout


class TestGhShimHookControls(unittest.TestCase):
    def test_today_s_shim_refuses_the_delete_of_a_forwarder_hook(self):
        rig = HookRig(shim_source=today("gh"), gh_body_source=today("gh_body.py"))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete())
        self.assertEqual(calls, [], "control: nothing passes on today's shim")
        self.assertIn("REST_WRITES", done.stderr)

    def test_without_the_pre_read_a_hook_of_another_name_would_pass(self):
        # the allow-list alone (today's gh_body.py with the new table row) would let any hook go: the pre-read is what narrows it
        rig = HookRig(gh_body_source=today("gh_body.py"))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete(), hook_text=hook(name="web"))
        self.assertEqual(len(calls), 1, "control: no pre-read, no narrowing")

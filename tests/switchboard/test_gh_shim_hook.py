"""#2397: the gh shim allows ONE webhook write, DELETE repos/O/R/hooks/ID, and only for a `cli` hook whose config.url is the webhook forwarder's url: the shim reads the hook first
(shims/gh_body.py, the pre-read style of the body guard) with the account the delete would use. Fakes only: no test calls GitHub. The controls are mutants of the current shim."""
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

    def test_a_full_url_is_refused_because_the_read_and_the_delete_could_reach_different_hosts(self):
        for path in ("https://api.github.com/" + HOOK, "http://api.github.com/" + HOOK, "//api.github.com/" + HOOK):
            with self.subTest(path=path):
                done, calls, seen = self.rig.go(delete(path))   # even a real forwarder hook: gh sends a URL to its own host, ignoring GH_HOST and --hostname
                self.assertEqual(calls, [])
                self.assertNotEqual(done.returncode, 0)

    def test_the_other_spellings_of_the_path_are_read_first_too(self):
        for path in ("/" + HOOK, HOOK + "?x=1"):
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

    def test_a_flag_and_its_value_before_api_cannot_skip_the_check(self):
        for lead in (["--hostname", "github.com"], ["-H", "x:y"], ["--jq", "."], ["--jq", "search"], ["-q", "auth"], ["--hostname", "help"], ["-f", "a=b"], ["-F", "a=b"], ["--field", "a=b"], ["--input", "-"]):
            with self.subTest(lead=lead):
                done, calls, _ = self.rig.go(lead + delete(), hook_text=hook(name="web"))
                self.assertEqual(calls, [], "a hook that is not the forwarder's passed")
                self.assertNotEqual(done.returncode, 0)

    def test_a_repo_flag_before_api_is_still_the_supported_lead(self):
        done, calls, seen = self.rig.go(["-R", "o/r"] + delete())
        self.assertEqual(len(calls), 1, done.stderr)
        self.assertEqual(len(seen), 1, "it was read first")

    def test_the_attached_method_spellings_are_guarded_like_the_separate_one(self):
        for spelling in (["--method=DELETE"], ["-X=DELETE"], ["-XDELETE"]):
            with self.subTest(spelling=spelling, hook="web"):
                done, calls, _ = self.rig.go(["api"] + spelling + [HOOK], hook_text=hook(name="web"))
                self.assertEqual(calls, [], done.stderr)
                self.assertNotEqual(done.returncode, 0)
            with self.subTest(spelling=spelling, hook="forwarder"):
                done, calls, seen = self.rig.go(["api"] + spelling + [HOOK])
                self.assertEqual(len(seen), 1, "it was read first")
                self.assertEqual(len(calls), 1, done.stderr)

    def test_a_read_with_a_repo_pair_and_a_lead_flag_is_classified_a_read(self):
        gh_body = load_module()
        parsed = []
        def parser(args):
            parsed.append(args)
            return type("P", (), {"effective_method": "GET", "positionals": [], "hostname": None})()
        self.assertIsNone(gh_body.guard(["-R", "o/r", "--jq", ".", "api", HOOK], False, parser, None, None))
        self.assertEqual(parsed, [["api", "--jq", ".", HOOK]], "the -R pair is not handed to the api parser")
        self.assertIsNone(gh_body.guard(["--repo=o/r", "--jq", ".", "api", HOOK], False, parser, None, None))
        self.assertEqual(parsed[-1], ["api", "--jq", ".", HOOK])

    def test_the_lead_handed_to_the_parser_keeps_a_repo_token_that_may_be_a_flag_value(self):
        gh_body = load_module()
        seen = []
        def parser(args):
            seen.append(args)
            return type("P", (), {"effective_method": "GET", "positionals": [], "hostname": None})()
        for lead, handed in ((["--jq", "--repo=o/r"], ["--jq", "--repo=o/r"]), (["--jq", "-R"], ["--jq", "-R"]), (["--jq", ".", "--repo=o/r"], ["--jq", "."])):
            with self.subTest(lead=lead):
                del seen[:]
                gh_body.guard(lead + ["api", HOOK], False, parser, None, None)
                self.assertEqual(seen, [["api"] + handed + [HOOK]])

    def test_a_repo_flag_that_is_the_value_of_an_earlier_flag_is_not_stripped(self):
        for lead in (["--jq", "-R"], ["-q", "-R"], ["-q", "--repo"], ["--jq", "--repo=o/r"]):
            for method in (["-XDELETE"], ["--method=DELETE"], ["-X", "DELETE"]):
                with self.subTest(lead=lead, method=method):
                    done, calls, _ = self.rig.go(lead + ["api"] + method + [HOOK], hook_text=hook(name="web"))
                    self.assertEqual(calls, [], done.stderr)
                    self.assertNotEqual(done.returncode, 0)

    def test_a_missing_guard_module_refuses_the_delete(self):
        (self.rig.shims / "gh_body.py").unlink()
        done, calls, _ = self.rig.go(delete())
        self.assertEqual(calls, [])
        self.assertEqual(done.returncode, 1)


def source(name):
    return (SHIM_DIR / name).read_text()


def mutant(name, old, new):
    text = source(name)
    assert text.count(old) == 1, old   # the control is a mutant of the CURRENT source: it stays true whatever origin/integration holds
    return text.replace(old, new)


class TestGhShimHookControls(unittest.TestCase):
    def test_without_the_allow_list_row_the_delete_of_a_forwarder_hook_is_refused(self):
        rig = HookRig(shim_source=mutant("gh", 'r"hooks/[0-9]+"', 'r"hooks/never-matches"'))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete())
        self.assertEqual(calls, [], "control: nothing passes without the row")
        self.assertIn("REST_WRITES", done.stderr)

    def test_without_the_hook_check_a_hook_of_another_name_would_pass(self):
        # the allow-list row alone would let any hook go: the pre-read and the name and url check are what narrow it
        old = 'if data.get("name") == "cli" and isinstance(config, dict) and config.get("url") == FORWARDER_HOOK_URL:'
        rig = HookRig(gh_body_source=mutant("gh_body.py", old, "if True:"))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete(), hook_text=hook(name="web"))
        self.assertEqual(len(calls), 1, "control: no check, no narrowing")

    def test_without_the_url_refusal_a_full_url_delete_would_read_and_pass(self):
        old = 'if "://" in endpoint or endpoint.startswith("//"):'
        rig = HookRig(gh_body_source=mutant("gh_body.py", old, "if False:"))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete("https://api.github.com/" + HOOK))
        self.assertEqual(len(calls), 1, "control: the refusal is what stops it")

    def test_without_the_lead_flag_block_a_flag_before_api_would_pass(self):
        old = "        if lead and all("
        rig = HookRig(gh_body_source=mutant("gh_body.py", old, "        if False and all("))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(["--hostname", "github.com"] + delete(), hook_text=hook(name="web"))
        self.assertEqual(len(calls), 1, "control: the block is what stops it")

    def test_without_the_numeric_id_test_a_non_numeric_id_is_read_not_left_to_the_allow_list(self):
        # the guard defers a non-numeric id to the allow-list (which refuses); the mutant makes the guard let it through to the shim's row, mutated too
        rig = HookRig(shim_source=mutant("gh", 'r"hooks/[0-9]+"', 'r"hooks/[0-9a-z]+"'))
        self.addCleanup(rig.close)
        done, calls, _ = rig.go(delete("repos/o/r/hooks/abc"), hook_text=hook(name="web"))
        self.assertEqual(len(calls), 1, "control: only the allow-list row refuses a non-numeric id")

"""#1751 pull request B: the gh shim refuses a body write that drops the Intent or the Done-when of the issue it replaces.

The shim runs as a COPY in a private directory in front of a fake real `gh` that answers a body read from the rig and records every write (argv, stdin, the
text of the files it was handed). Reads never reach the write log. Fakes only: no test calls GitHub. The mutants are applied once to a source copy
(the anchored text must occur exactly once), and a battery of the plan's forms is judged on the unmutated copy first (the control).
"""
import importlib.util
import itertools
import json
import pathlib
import shlex
import subprocess
import sys
import unittest
import unittest.mock
import urllib.parse

from test_gh_shim_sign import REPO, SHIM_DIR, Rig

BOTH = "## Intent\nwhy this is wanted\n\nDone when: it works\n\nNext: step\n"
NO_DONE = "## Intent\nwhy this is wanted\n\nNext: step\n"
NO_INTENT = "Done when: it works\n\nNext: step\n"
ONLY_LINE = "Next: step\n"
DROP_WORDS = "would drop the Intent or the Done-when"

FAKE_REAL_GH_BODY_READER = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
home = os.environ["FAKE_DIR"]
if argv[:2] == ["issue", "view"] or (argv[:1] == ["api"] and len(argv) == 2):
    with open(os.path.join(home, "reads.jsonl"), "a") as out:
        out.write(json.dumps(argv) + "\\n")
    mode = open(os.path.join(home, "mode")).read()
    if mode == "fail":
        sys.exit(1)
    data = {"body": None if mode == "null" else open(os.path.join(home, "current")).read() if mode == "ok" else ""}
    if mode == "pr":
        data = {"body": "x", "pull_request": {}}
    print(json.dumps(data))
    sys.exit(0)
files = {}
for a in sys.argv[1:]:
    for p in (a, a.split("=@", 1)[-1], a[2:]):
        if os.path.isfile(p):
            files[p] = open(p).read()
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "files": files}) + "\\n")
"""


def load_module(path=None):
    spec = importlib.util.spec_from_file_location("gh_body_under_test", str(path or SHIM_DIR / "gh_body.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BodyRig(Rig):
    def __init__(self, gh_body_source=None, shim_source=None):
        Rig.__init__(self, shim_source)
        (self.bin / "gh").write_text(FAKE_REAL_GH_BODY_READER)
        if gh_body_source is not None:
            (self.shims / "gh_body.py").write_text(gh_body_source)

    def go(self, args, current=BOTH, mode="ok", stdin=None):
        """(process, writes passed to the real gh, reads made of it) for one call of the shim."""
        (self.home / "current").write_text(current)
        (self.home / "mode").write_text(mode)
        reads = self.home / "reads.jsonl"
        if reads.exists():
            reads.unlink()
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (self.shims, self.bin), "HOME": str(self.home), "TMPDIR": str(self.home), "FAKE_GH_LOG": str(self.log),
               "FAKE_DIR": str(self.home), "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SWITCHBOARD_BIN": str(self.sb),
               "FLEET_SHIM_LOOKUP_SECONDS": "10"}
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run([sys.executable, str(self.shims / "gh"), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        seen = [json.loads(l) for l in reads.read_text().splitlines()] if reads.exists() else []
        return done, calls, seen

    def path(self, name, text):
        target = self.home / name
        target.write_text(text)
        return str(target)


def forms(rig, text):
    """The eight body-writing forms of the plan, each carrying `text`: [(label, argv, stdin or None)]."""
    body_file = rig.path("body.md", text)
    json_text = json.dumps({"body": text})
    issue = "repos/o/r/issues/1"
    return [("--body", ["issue", "edit", "1", "--body", text], None), ("-b", ["issue", "edit", "1", "-b", text], None),
            ("--body=", ["issue", "edit", "1", "--body=" + text], None), ("--body-file", ["issue", "edit", "1", "--body-file", body_file], None),
            ("-F -", ["issue", "edit", "1", "-F", "-"], text), ("api -f", ["api", issue, "-X", "PATCH", "-f", "body=" + text], None),
            ("api -F @file", ["api", issue, "-X", "PATCH", "-F", "body=@" + body_file], None),
            ("api --input -", ["api", issue, "-X", "PATCH", "--input", "-"], json_text)]


def battery(rig):
    """The failures the plan's scenarios S7 and S8 find in this rig (empty for the real shim)."""
    bad = []
    for text in (NO_DONE, NO_INTENT, ONLY_LINE):
        for label, argv, stdin in forms(rig, text):
            done, calls, _ = rig.go(argv, stdin=stdin)
            if calls or done.returncode != 1 or DROP_WORDS not in done.stderr:
                bad.append("not refused: %s without a part" % label)
    for label, argv, stdin in forms(rig, BOTH):
        done, calls, _ = rig.go(argv, stdin=stdin)
        seen = json.dumps(calls)
        if len(calls) != 1 or done.returncode != 0 or "why this is wanted" not in seen or "Done when: it works" not in seen:
            bad.append("not passed: %s with both parts (stdin or files lost)" % label)
    for mode in ("fail", "empty", "null"):
        done, calls, _ = rig.go(["issue", "edit", "1", "--body", NO_DONE], mode=mode)
        if calls or done.returncode != 1 or "could not be read" not in done.stderr:
            bad.append("an unreadable current body passed: " + mode)
    done, calls, _ = rig.go(["api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "body=" + NO_DONE], mode="fail")
    if calls or done.returncode != 1:
        bad.append("the api form passed on a failed read")
    done, calls, _ = rig.go(["issue", "edit", "1", "--body", ONLY_LINE], current="a note with no parts\n")
    if len(calls) != 1 or done.returncode != 0:
        bad.append("a body with no parts was refused")
    query = "mutation { updateIssue(input: {id: \"X\", body: \"y\"}) { clientMutationId } }"
    done, calls, _ = rig.go(["api", "graphql", "-f", "query=" + query])
    if calls or done.returncode != 1:
        bad.append("graphql updateIssue passed")
    done, calls, _ = rig.go(["api", "repos/o/r/issues/1", "-X", "PATCH", "--input", rig.path("junk.json", "not json")])
    if calls or done.returncode != 1:
        bad.append("an unreadable --input passed")
    done, calls, _ = rig.go(["api", "repos/o/r/issues/1", "-X", "PATCH", "-F", "body=@" + str(rig.home / "missing.md")])
    if calls or done.returncode != 1:
        bad.append("an unreadable body file passed")
    return bad


class TestGhBodyGuard(unittest.TestCase):
    def setUp(self):
        self.rig = BodyRig()
        self.addCleanup(self.rig.close)

    def test_the_forms_of_the_plan_are_refused_without_a_part_and_pass_with_both(self):
        self.assertEqual(battery(self.rig), [])

    def test_a_refusal_is_one_line_and_names_the_command_that_changes_one_line(self):
        done, calls, _ = self.rig.go(["issue", "edit", "1", "--body", NO_DONE])
        self.assertEqual(calls, [])
        lines = [l for l in done.stderr.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, done.stderr)
        self.assertIn("fleet-switchboard issue-line", lines[0])

    def test_non_body_writes_reads_and_pull_requests_are_not_touched(self):
        for argv, mode in ((["issue", "edit", "1", "--title", "t"], "ok"), (["issue", "edit", "1", "--add-label", "x"], "ok"),
                           (["api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "state=closed"], "ok"),
                           (["api", "repos/o/r/issues/1/labels", "-f", "labels[]=x"], "ok"), (["api", "repos/o/r/issues/1"], "ok"),
                           (["issue", "view", "1"], "ok"), (["api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "body=" + ONLY_LINE], "pr")):
            with self.subTest(argv=argv):
                done, calls, _ = self.rig.go(argv, mode=mode)
                self.assertEqual(len(calls), 1 if argv[:2] != ["issue", "view"] and argv != ["api", "repos/o/r/issues/1"] else 0, done.stderr)
                self.assertNotIn(DROP_WORDS, done.stderr)

    def test_a_write_reads_the_current_body_at_most_once_per_issue(self):
        _done, _calls, seen = self.rig.go(["issue", "edit", "1", "--body", BOTH])
        self.assertEqual(len(seen), 1)
        _done, _calls, seen = self.rig.go(["issue", "edit", "1", "2", "--body", BOTH])
        self.assertEqual(len(seen), 2)

    def test_a_missing_guard_module_refuses_a_write_and_lets_a_read_run(self):
        (self.rig.shims / "gh_body.py").unlink()
        done, calls, _ = self.rig.go(["issue", "edit", "1", "--body", BOTH])
        self.assertEqual((calls, done.returncode), ([], 1))
        done, calls, _ = self.rig.go(["pr", "list"])
        self.assertEqual(len(calls), 1)


class TestGhBodyRefusalIsPure(unittest.TestCase):
    def test_the_decision_over_every_combination_of_the_two_parts(self):
        module = load_module()
        for cur_i, cur_d, new_i, new_d in itertools.product((False, True), repeat=4):
            def body(i, d):
                return ("## Intent\nwhy\n" if i else "text\n") + ("Done when: x\n" if d else "")
            got = module.refusal(body(cur_i, cur_d), body(new_i, new_d))
            with self.subTest(parts=(cur_i, cur_d, new_i, new_d)):
                self.assertEqual(got is not None, cur_i and cur_d and not (new_i and new_d))

    def test_a_body_that_cannot_be_read_or_is_empty_is_refused_whatever_the_new_one_is(self):
        module = load_module()
        for current in (None, "", "  \n", 5):
            self.assertIn("could not be read", module.refusal(current, BOTH))

    def test_the_parts_are_lines_not_words_inside_a_line(self):
        module = load_module()
        self.assertIsNotNone(module.refusal(BOTH, "see ## Intent and Done when: inline\n"))
        self.assertIsNone(module.refusal(BOTH, BOTH))


class TestGhBodyMutants(unittest.TestCase):
    """S9: each mutant is applied once to a source copy, asserted applied, and fails the battery; the control passes it first."""
    MUTANTS = (("gh", "the guard is not called", "or body_probe(args, gh, write, lookup)", "or None", "not refused"),
               ("gh_body.py", "only the Intent heading is compared", "return bool(HEAD_RE.search(body)) and bool(DONE_RE.search(body))",
                "return bool(HEAD_RE.search(body))", "not refused: --body without a part"),
               ("gh_body.py", "a failed read is read as a body with no parts", "    if data is None:\n        return UNREAD_WHY % (probe.why or \"no reason given\")\n",
                "    if data is None:\n        return None\n", "an unreadable current body passed"))

    def run_with(self, name, source):
        rig = BodyRig(shim_source=source if name == "gh" else None, gh_body_source=source if name != "gh" else None)
        self.addCleanup(rig.close)
        return battery(rig)

    def test_the_control_passes_and_each_mutant_fails_with_its_anchored_text(self):
        for name, label, old, new, text in self.MUTANTS:
            with self.subTest(mutant=label):
                source = (SHIM_DIR / name).read_text()
                self.assertEqual(self.run_with(name, source), [])   # the control
                self.assertEqual(source.count(old), 1, label)
                failures = self.run_with(name, source.replace(old, new, 1))
                self.assertTrue(any(text in f for f in failures), (label, failures))


class FakeGhMixin:
    """A fake real gh (a shell script) in a temporary directory, and a read of it through the guard."""

    def setUp(self):
        import os
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.gh = os.path.join(self.dir.name, "gh")
        self.env = {"FLEET_SHIM_HOLD_SECONDS": "0.5"}
        self.patch = unittest.mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def fake(self, body):
        with open(self.gh, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        import os
        os.chmod(self.gh, 0o755)

    def check(self, module, spend=0.0):
        import time
        probe = module.Probe(self.gh, lambda: None, None)
        time.sleep(spend)
        return module.check_current(probe, ["issue", "view", "1", "--json", "body"], "no parts here\n")



class TestGhBodyReadHasItsOwnClock(FakeGhMixin, unittest.TestCase):
    """#2256: the body read's time limit is counted from the read; a failed read names its reason."""
    OLD_TEXT = "could not be read, or is empty"

    def test_S2_earlier_work_that_spends_the_limit_does_not_stop_the_read(self):
        self.fake("cat <<'EOF'\n" + json.dumps({"body": BOTH}) + "\nEOF")
        got = self.check(load_module(), spend=0.6)
        self.assertEqual(sum(1 for _ in [got] if got and self.OLD_TEXT in got), 0, got)
        self.assertIn("would drop the Intent", got)   # the edit was checked

    def test_S2_control_today_s_code_refuses_with_the_old_text(self):
        import subprocess as sp
        source = sp.run(["git", "show", "18923d472731d74b58033e1a9ddd6f0accf631c4:shims/gh_body.py"], cwd=str(REPO), capture_output=True, text=True, check=True).stdout
        path = pathlib.Path(self.dir.name) / "old_gh_body.py"
        path.write_text(source)
        self.fake("cat <<'EOF'\n" + json.dumps({"body": BOTH}) + "\nEOF")
        got = self.check(load_module(path), spend=0.6)
        self.assertIn(self.OLD_TEXT, got)

    def test_S3_a_failed_read_names_its_reason(self):
        module = load_module()
        cases = (("sleep 5", "timed out"), ("exit 3", "status 3"), ("echo not json", "not JSON"), ("echo '[1]'", "not a JSON object"))
        unnamed = 0
        for script, reason in cases:
            with self.subTest(reason=reason):
                self.fake(script)
                os_env = unittest.mock.patch.dict("os.environ", {"FLEET_SHIM_HOLD_SECONDS": "0.3"})
                with os_env:
                    got = self.check(module)
                self.assertIn(reason, got)
                self.assertNotIn(self.OLD_TEXT, got)
                unnamed += 0 if reason in got else 1
        self.assertEqual(unnamed, 0)

    def test_command_budget_caps_three_hanging_targets(self):
        import time
        module = load_module()
        self.fake("sleep 5")
        probe = module.Probe(self.gh, lambda: None, None)   # limit 0.5 s per read, 1.5 s per command
        started = time.monotonic()
        got = [module.check_current(probe, ["issue", "view", t, "--json", "body"], "x\n") for t in "123"]
        got.append(module.check_current(probe, ["issue", "view", "4", "--json", "body"], "x\n"))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertTrue(all(g and "timed out" in g or "read budget is spent" in g for g in got), got)
        self.assertIn("the command's read budget is spent", got[3])


class TestGhBodyReadNamesItsStderr(FakeGhMixin, unittest.TestCase):
    """#2256 fix forward: a read that exits non-zero names the first part of its stderr (one line, redacted) in the refusal."""
    WORDS = "the read exited with status 1"
    FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"

    def setUp(self):
        FakeGhMixin.setUp(self)
        self.env_patch = unittest.mock.patch.dict("os.environ", {"FLEET_SHIM_HOLD_SECONDS": "10"})   # a loaded runner must not turn a read into a time-out
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def failing(self, module, said, env=None):
        said_file = pathlib.Path(self.dir.name) / "said"
        said_file.write_text(said)
        self.fake("cat " + shlex.quote(str(said_file)) + " >&2\nexit 1")
        probe = module.Probe(self.gh, lambda: env, None)
        return module.check_current(probe, ["issue", "view", "1", "--json", "body"], "no parts here\n")

    def test_the_refusal_shows_the_stderr_of_the_read(self):
        got = self.failing(load_module(), "HTTP 404: Not Found")
        self.assertIn("(%s: HTTP 404: Not Found)" % self.WORDS, got)

    def test_a_silent_read_keeps_the_old_wording(self):
        self.fake("exit 1")
        self.assertIn("(%s)" % self.WORDS, self.check(load_module()))

    def test_a_benign_message_comes_through_unchanged(self):
        said = "HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/1)"
        self.assertIn(": " + said + ")", self.failing(load_module(), said))

    def test_every_secret_shape_is_redacted_and_the_context_is_kept(self):
        env = {"GH_TOKEN": "plainsecretvalue99"}
        samples = (self.FAKE_TOKEN, "Bearer abcdef123456", "Basic dXNlcjpwYXNz", "Authorization: token abcdef123456", "token=abc123xyz", "password: 'two words'",
                   "--token abc123xyz", "plainsecretvalue99", "https://user:hunter2pass@example.com/x",
                   "https://opaquetoken987@example.com/x", "sk-" + "abcdefgh12345678", "github_pat_" + "A" * 24, "AKIA" + "ABCDEFGHIJKL", "xoxb-" + "12345678abcd",
                   "eyJhbGciOi.eyJzdWIiOiIx.sig", "0123456789abcdef0123456789abcdef01234567")
        secrets = (self.FAKE_TOKEN, "abcdef123456", "dXNlcjpwYXNz", "abc123xyz", "two words", "plainsecretvalue99", "hunter2pass", "opaquetoken987", "abcdefgh12345678",
                   "A" * 24, "ABCDEFGHIJKL", "12345678abcd", "eyJzdWIiOiIx", "0123456789abcdef0123456789abcdef01234567")
        for sample in samples:
            with self.subTest(sample=sample[:12]):
                got = self.failing(load_module(), "before " + sample + " after", env)
                for secret in secrets:
                    self.assertNotIn(secret, got)
                self.assertIn("before", got)
                self.assertIn("[redacted]", got)
        got = self.failing(load_module(), "quoted " + urllib.parse.quote("pla in/secretX", safe="") + " end", {"GH_TOKEN": "pla in/secretX"})
        self.assertNotIn("secretX", got)
        got = self.failing(load_module(), "json " + json.dumps("a\"b\\c-secret") + " end", {"GITHUB_TOKEN": 'a"b\\c-secret'})
        self.assertNotIn("c-secret", got)

    def test_the_text_is_one_line_of_printable_ascii_cut_to_a_limit(self):
        module = load_module()
        got = self.failing(module, "a\x1b[31mb\nsecond line\r\n\u202e\u00e9" + "x" * 500)
        self.assertNotIn("\n", got)
        self.assertTrue(all(c.isascii() and c.isprintable() for c in got), got)
        shown = got.split(self.WORDS + ": ", 1)[1]
        self.assertLessEqual(len(module.clean_stderr("y" * 500)), 200)   # the limit of the plan: 200 characters
        self.assertGreater(len(shown), 100)
        self.assertLess(len(shown), 500 + 100)
        self.assertLess(got.index("31mb"), got.index("second line"))
        self.assertEqual(module.clean_stderr("short"), "short")

    def test_a_huge_stderr_is_cheap_and_a_token_at_the_scan_limit_is_not_shown(self):
        import time
        module = load_module()
        started = time.monotonic()
        for text in ("token" * 400000, "auth" * 400000, "a" * 2000000, "x= " * 300000):
            module.clean_stderr(text)
        self.assertLess(time.monotonic() - started, 5.0)
        tail = " " + self.FAKE_TOKEN
        for cut in range(len(self.FAKE_TOKEN)):
            text = "z" * (module.WHY_READ - 1 - cut) + tail
            self.assertNotIn(self.FAKE_TOKEN[:8], module.clean_stderr(text, limit=10 ** 6))

    def test_undecodable_stderr_does_not_hide_the_status(self):
        self.fake("printf '\\377\\376 bad' >&2\nexit 1")
        self.assertIn(self.WORDS, self.check(load_module()))

    def test_the_old_line_is_the_control(self):
        """The mutant puts the old line back: the same read shows no stderr, and the test of the text above fails on it."""
        source = (SHIM_DIR / "gh_body.py").read_text()
        old = ('                said = clean_stderr(done.stderr, env)\n                self.why = "the read exited with status %d%s" % (done.returncode, ": " + said if said else "")\n')
        self.assertEqual(source.count(old), 1)
        path = pathlib.Path(self.dir.name) / "old_gh_body.py"
        path.write_text(source.replace(old, '                self.why = "the read exited with status %d" % done.returncode\n', 1))
        got = self.failing(load_module(path), "HTTP 404: Not Found")
        self.assertIn("(%s)" % self.WORDS, got)
        self.assertNotIn("404", got)

    def shim(self):
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("gh_shim_under_test", str(SHIM_DIR / "gh"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("gh_shim_under_test", loader))
        loader.exec_module(module)
        return module

    def test_the_four_token_names_are_the_same_in_every_copy(self):
        shim, mint = self.shim(), load_module(SHIM_DIR / "app_mint.py")
        self.assertEqual(load_module().TOKEN_ENV_NAMES, shim.TOKEN_ENV_NAMES)
        self.assertEqual(load_module().TOKEN_ENV_NAMES, mint.TOKEN_ENV_NAMES)

    def test_the_read_is_strict_for_a_write_and_retries_a_failed_lookup_once(self):
        shim = self.shim()
        calls = []

        def account_env(gh, lookup, strict=False, **kwargs):
            calls.append((strict, lookup.deadline))
            if len(calls) == 1:
                raise shim.AccountLookupFailed("identity")
            return {"GH_TOKEN": "x"}
        with unittest.mock.patch.object(shim, "account_env", account_env):
            first = shim.Lookup(write=True)
            env_of = shim.body_read_env("gh", first, ["issue", "edit", "1"], True)
            self.assertEqual(env_of(), {"GH_TOKEN": "x"})
            self.assertEqual(env_of(), {"GH_TOKEN": "x"})   # worked out once
        self.assertEqual([c[0] for c in calls], [True, True])
        self.assertNotEqual(calls[0][1], calls[1][1])   # the retry has a fresh deadline

    def test_a_read_that_is_not_for_a_write_is_not_strict(self):
        shim = self.shim()
        seen = []
        with unittest.mock.patch.object(shim, "account_env", lambda gh, lookup, strict=False, **kw: seen.append(strict) or None):
            self.assertIsNone(shim.body_read_env("gh", shim.Lookup(), [], False)())
        self.assertEqual(seen, [False])

    def test_the_wiring_a_lookup_that_keeps_failing_refuses_the_write_by_name_and_looks_up_once(self):
        shim = self.shim()
        ran = pathlib.Path(self.dir.name) / "ran"
        self.fake("touch " + shlex.quote(str(ran)) + "\nexit 0")
        calls = []

        def account_env(gh, lookup, strict=False, **kwargs):
            calls.append(strict)
            raise shim.AccountLookupFailed("identity")
        with unittest.mock.patch.object(shim, "account_env", account_env):
            got = shim.body_probe(["issue", "edit", "1", "2", "--body", "no parts\n"], self.gh, True, shim.Lookup(write=True))
        self.assertIn("AccountLookupFailed", got)
        self.assertIn("refused", got)
        self.assertFalse(ran.exists())   # the stored login never read
        self.assertEqual(calls, [True, True])   # one lookup and its one retry for the whole command (two targets)


if __name__ == "__main__":
    unittest.main()

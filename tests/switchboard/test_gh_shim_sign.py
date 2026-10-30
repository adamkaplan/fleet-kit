"""#1638: every comment a fleet account writes through the `gh` shim carries the marker, whatever command wrote it.

The shim runs as a COPY (shims/ in a temporary directory) in front of a fake real `gh` that records its argv, its stdin and the text of any file it was
handed. The list of signed forms is read from shims/gh with `ast` (never a pattern), and a test fails when a form in the code has no example here.

Closed rule: a call is signed when it is one of the tables SIGNED_COMMANDS, CLOSE_COMMANDS (with --comment/-c) or SIGNED_API (a POST or PATCH with a `body` field);
a signed form whose body cannot be read is refused. Known limits (not signed, not refused): `gh api --input`, a body-less call, `pulls/comments/ID` and a reply
endpoint, short flags bundled together (`-dcx`), and a flag before the verb (ask #1639).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO / "shims"
MARK = "<!-- fleet-switchboard:from=%s -->"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
files = {}
for a in sys.argv[1:]:
    for p in (a, a.split("=@", 1)[-1], a[2:]):
        if os.path.isfile(p):
            files[p] = open(p).read()
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "files": files}) + "\\n")
"""
FAKE_SB = '#!/bin/sh\nif [ "$1" = whoami ]; then\n  echo \'{"name": "alice-bot", "repo": "acme/app"}\'; exit 0\nfi\nexit 2\n'

# one example per form: the keys are what the tables of shims/gh must hold, in the labels the code gives
OLD_EXAMPLE = {"issue": "1", "pr": "1"}
API_EXAMPLES = {"issue-comments": "repos/o/r/issues/1/comments", "issue-comment": "repos/o/r/issues/comments/9",
                "review-comments": "repos/o/r/pulls/1/comments", "reviews": "repos/o/r/pulls/1/reviews"}


def pairs(source, name):
    """Every two-string tuple inside the value assigned to `name` at module level, however the value is spelled (a set, a list, set(...), a union)."""
    found = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Tuple) and len(sub.elts) == 2 and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in sub.elts):
                    found.append((sub.elts[0].value, sub.elts[1].value))
    return found


def forms_without_an_example(source):
    """The forms the code signs that this file has no example for: empty when every form is covered."""
    missing = []
    for group, verb in pairs(source, "SIGNED_COMMANDS"):
        if group not in OLD_EXAMPLE:
            missing.append((group, verb))
    for group, verb in pairs(source, "CLOSE_COMMANDS"):
        if group not in OLD_EXAMPLE or verb != "close":
            missing.append((group, verb))
    for label, _pattern in pairs(source, "SIGNED_API"):
        if label not in API_EXAMPLES:
            missing.append(("api", label))
    return missing


class Rig:
    """A copy of the shim, a fake real gh and a fake switchboard in a private directory."""

    def __init__(self, shim_source=None):
        self.home = Path(tempfile.mkdtemp(prefix="gh-sign-"))
        self.shims = self.home / "shims"
        shutil.copytree(str(SHIM_DIR), str(self.shims))
        if shim_source is not None:
            (self.shims / "gh").write_text(shim_source)
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.sb = self.home / "fakesb"
        self.sb.write_text(FAKE_SB)
        self.sb.chmod(0o755)
        self.log = self.home / "calls.jsonl"
        self.state = self.home / "state"
        self.state.mkdir()

    def close(self):
        shutil.rmtree(str(self.home), ignore_errors=True)

    def run(self, *args, stdin=None, no_whoami=False):
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (self.shims, self.bin), "HOME": str(self.home), "TMPDIR": str(self.home), "FAKE_GH_LOG": str(self.log),
               "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SWITCHBOARD_BIN": str(self.sb), "FLEET_SHIM_LOOKUP_SECONDS": "10"}
        if no_whoami:
            broken = self.home / "brokensb"
            broken.write_text("#!/bin/sh\nexit 1\n")
            broken.chmod(0o755)
            env["FLEET_SWITCHBOARD_BIN"] = str(broken)
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run([sys.executable, str(self.shims / "gh"), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, calls


def battery(rig):
    """A short run of every family; returns the failures it saw (empty for the real shim). The mutants are judged by this."""
    bad = []

    def signed(args, where, who="alice-bot"):
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"][where].count(MARK % who) != 1:
            bad.append("not signed exactly once: %s" % " ".join(args))

    signed(("issue", "comment", "1", "--body", "x"), -1)
    signed(("issue", "close", "1", "--comment", "x"), -1)
    signed(("pr", "close", "1", "-c", "x"), -1)
    signed(("api", API_EXAMPLES["reviews"], "-f", "body=x", "-f", "event=COMMENT"), 3)
    done, calls = rig.run("issue", "close", "1", "--comment", "x " + MARK % "bob")
    if len(calls) != 1 or calls[0]["argv"][-1].count("fleet-switchboard:from=") != 1:
        bad.append("a marked body was signed twice")
    for args in (("api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "body=x"), ("api", "repos/o/r/issues/1/labels", "-f", "body=x"),
                 ("api", "repos/o/r/issues/1/comments"), ("api", "repos/o/r/issues/1/comments", "-X", "GET")):
        done, calls = rig.run(*args)
        if len(calls) != 1 or calls[0]["argv"] != list(args):
            bad.append("an unrelated call changed: %s" % " ".join(args))
    return bad


class TestGhShimSign(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.source = (SHIM_DIR / "gh").read_text()

    def one(self, *args, **kw):
        done, calls = self.rig.run(*args, **kw)
        self.assertEqual(len(calls), 1, (args, done.stderr))
        return calls[0], done

    def assertSigned(self, text, who="alice-bot"):
        self.assertEqual(text.count("fleet-switchboard:from="), 1, text)
        self.assertIn(MARK % who, text)

    # ---- the forms, derived from the code
    def test_every_form_in_the_code_has_an_example_here(self):
        self.assertEqual(forms_without_an_example(self.source), [])
        self.assertEqual(len(pairs(self.source, "SIGNED_COMMANDS")), 5)
        self.assertEqual(len(pairs(self.source, "CLOSE_COMMANDS")), 2)
        self.assertEqual(len(pairs(self.source, "SIGNED_API")), 4)

    def test_a_new_form_spelled_another_way_makes_the_completeness_check_fail(self):
        planted = self.source.replace("CLOSE_COMMANDS = {", "CLOSE_COMMANDS = set([(\"gist\", \"close\")]) | {", 1)
        self.assertNotEqual(planted, self.source)
        self.assertEqual(forms_without_an_example(planted), [("gist", "close")])
        planted = self.source.replace("SIGNED_API = (", "SIGNED_API = ((\"gists\", r\"gists/x\"), ", 1)
        self.assertEqual(forms_without_an_example(planted), [("api", "gists")])

    def test_the_five_old_forms_are_signed_as_before(self):
        for group, verb in sorted(pairs(self.source, "SIGNED_COMMANDS")):
            for flag in (["--body", "x"], ["-b", "x"], ["--body=x"], ["-bx"]):
                with self.subTest(form=(group, verb), flag=flag):
                    call, _ = self.one(group, verb, "1", *flag)
                    self.assertSigned(call["argv"][-1])

    def test_a_body_file_and_stdin_are_signed_and_the_temporary_file_is_removed(self):
        path = self.rig.home / "body.md"
        path.write_text("from a file\n")
        call, _ = self.one("issue", "comment", "1", "--body-file", str(path))
        temp = call["argv"][-1]
        self.assertNotEqual(temp, str(path))
        self.assertSigned(call["files"][temp])
        self.assertFalse(os.path.exists(temp))
        self.assertEqual(path.read_text(), "from a file\n")
        call, _ = self.one("pr", "comment", "1", "-F", "-", stdin="piped\n")
        self.assertSigned(call["stdin"])

    def test_the_close_forms_are_signed_in_every_spelling(self):
        for group, verb in sorted(pairs(self.source, "CLOSE_COMMANDS")):
            for flag, at in ((["--comment", "x"], -1), (["-c", "x"], -1), (["--comment=x"], -1), (["-cx"], -1)):
                with self.subTest(form=(group, verb), flag=flag):
                    call, _ = self.one(group, verb, "1", *flag)
                    self.assertSigned(call["argv"][at])
                    self.assertEqual(call["argv"][:3], [group, verb, "1"])

    def test_a_close_keeps_its_other_flags(self):
        call, _ = self.one("pr", "close", "1", "-c", "bye", "--delete-branch")
        self.assertEqual(call["argv"][5:], ["--delete-branch"])
        self.assertSigned(call["argv"][4])

    def test_the_api_forms_are_signed_in_every_field_spelling(self):
        for label, _pattern in pairs(self.source, "SIGNED_API"):
            for flag in ("-f", "-F", "--field", "--raw-field"):
                with self.subTest(endpoint=label, flag=flag):
                    call, _ = self.one("api", API_EXAMPLES[label], flag, "body=x", "-f", "event=COMMENT")
                    self.assertSigned(call["argv"][3])
                    self.assertEqual(call["argv"][4:], ["-f", "event=COMMENT"])
            with self.subTest(endpoint=label, method="PATCH"):
                call, _ = self.one("api", API_EXAMPLES[label], "-X", "PATCH", "-f", "body=x")
                self.assertSigned(call["argv"][-1])
            with self.subTest(endpoint=label, inline="--method=PATCH --field=body=x"):
                call, _ = self.one("api", "/" + API_EXAMPLES[label], "--method=PATCH", "--field=body=x")
                self.assertSigned(call["argv"][-1])

    def test_an_api_body_file_and_stdin_are_signed(self):
        path = self.rig.home / "b.md"
        path.write_text("file body")
        call, _ = self.one("api", API_EXAMPLES["issue-comments"], "-F", "body=@" + str(path))
        temp = call["argv"][-1][len("body=@"):]
        self.assertSigned(call["files"][temp])
        self.assertFalse(os.path.exists(temp))
        call, _ = self.one("api", API_EXAMPLES["reviews"], "-F", "body=@-", stdin="piped")
        self.assertSigned(call["stdin"])

    def test_a_raw_field_that_looks_like_a_file_is_a_literal(self):
        call, _ = self.one("api", API_EXAMPLES["issue-comments"], "-f", "body=@notafile")
        self.assertSigned(call["argv"][-1])
        self.assertIn("@notafile", call["argv"][-1])

    # ---- what is left alone
    def test_unrelated_calls_are_passed_byte_for_byte(self):
        calls = [("api", "repos/o/r/issues/1"), ("api", "repos/o/r/issues/1/comments"), ("api", "repos/o/r/issues/1/comments", "-X", "GET"),
                 ("api", "repos/o/r/pulls/1/reviews", "-X", "GET"),
                 ("api", "repos/o/r/issues/1", "-X", "PATCH", "-f", "body=x"), ("api", "repos/o/r/issues/1/labels", "-f", "body=x"),
                 ("api", "repos/o/r/issues/1/comments", "-f", "event=x"), ("api", "repos/o/r/issues/1/comments", "-X", "DELETE"),
                 ("api", "repos/o/r/pulls/1/reviews", "--input", "x.json"), ("api", "repos/o/r/pulls/1/reviews", "--input=x.json", "-f", "body=x"),
                 ("api", "repos/o/r/issues/1/comments", "-X", "GET", "-f", "body=x"),
                 ("issue", "view", "1"), ("pr", "list"), ("issue", "close", "1"), ("pr", "close", "1", "--delete-branch"),
                 ("issue", "comment", "1", "--editor"), ("issue", "close", "1", "--comment", "")]
        for args in calls:
            with self.subTest(args=args):
                call, _ = self.one(*args)
                self.assertEqual(call["argv"], list(args))

    def test_a_marked_body_is_not_signed_twice(self):
        marked = "hello\n\n" + MARK % "bob" + "\n"
        for args in (("issue", "comment", "1", "--body", marked), ("issue", "close", "1", "--comment", marked), ("pr", "close", "1", "-c", marked),
                     ("api", API_EXAMPLES["issue-comments"], "-f", "body=" + marked), ("api", API_EXAMPLES["reviews"], "-F", "body=" + marked)):
            with self.subTest(args=args):
                call, _ = self.one(*args)
                self.assertEqual(call["argv"], list(args))

    # ---- the refusal
    def test_a_body_that_cannot_be_read_is_refused_and_nothing_is_posted(self):
        for args in (("issue", "comment", "1", "--body-file", "/nonexistent/x.md"), ("pr", "create", "--body-file=/nonexistent/x.md"),
                     ("api", API_EXAMPLES["issue-comments"], "-F", "body=@/nonexistent/x.md"),
                     ("api", API_EXAMPLES["reviews"], "-F", "body=@/nonexistent/x.md", "-f", "event=COMMENT")):
            with self.subTest(args=args):
                done, calls = self.rig.run(*args)
                self.assertEqual(calls, [])
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
                self.assertIn("nothing was posted", done.stderr)

    def test_a_signing_error_is_a_refusal_not_an_unsigned_post(self):
        done, calls = self.rig.run("issue", "comment", "1", "--body-file", str(self.rig.home))   # a directory
        self.assertEqual(calls, [])
        self.assertNotEqual(done.returncode, 0)

    # ---- the sender
    def test_an_unknown_sender_is_signed_as_agent_and_never_refused(self):
        for args in (("issue", "comment", "1", "--body", "x"), ("issue", "close", "1", "-c", "x"), ("api", API_EXAMPLES["reviews"], "-f", "body=x")):
            with self.subTest(args=args):
                done, calls = self.rig.run(*args, no_whoami=True)
                self.assertEqual(len(calls), 1, done.stderr)
                self.assertIn(MARK % "agent", json.dumps(calls[0]["argv"]))

    # ---- the mutants: each is a copy of the shim with one planted fault, and the battery must see it
    def test_the_battery_passes_for_the_real_shim(self):
        self.assertEqual(battery(self.rig), [])

    def mutant(self, old, new):
        self.assertEqual(self.source.count(old), 1, "the mutant's anchor is gone: " + old)
        rig = Rig(self.source.replace(old, new, 1))
        self.addCleanup(rig.close)
        return battery(rig)

    def test_mutant_a_form_left_unsigned_is_seen(self):
        self.assertTrue(self.mutant('CLOSE_COMMANDS = {("issue", "close"), ("pr", "close")}', "CLOSE_COMMANDS = set()"))
        self.assertTrue(self.mutant('("reviews", r"pulls/[0-9]+/reviews")', '("reviews", r"pulls/[0-9]+/reviewsX")'))

    def test_mutant_a_body_signed_twice_is_seen(self):
        line = 'return body if SIGNED_RE.search(body) else body.rstrip("\\n") + "\\n\\n" + MARK % name + "\\n"'
        self.assertTrue(self.mutant(line, 'return body.rstrip("\\n") + "\\n\\n" + MARK % name + "\\n"'))

    def test_mutant_an_unrelated_call_changed_is_seen(self):
        self.assertTrue(self.mutant("if not any(API_ENDPOINT_RE.match(p) for p in positionals):", "if False:"))


if __name__ == "__main__":
    unittest.main()

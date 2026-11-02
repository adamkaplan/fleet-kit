"""#1895: a pull request body is checked on every write through the shim (shims/body_guard.py, body_probe and --drop-line in shims/gh).

The REAL shim runs (as a copy) in front of a fake real gh that serves the old body of `api repos/o/r/issues/N` and records every other call.
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_gh_shim_sign import REPO, Rig

GUARD_PATH = REPO / "shims" / "body_guard.py"
OLD = "MERGE: captain\nPLAN: the plan\nCLAIMS: a.py\n\nThe text of the description.\n"
CUT = OLD[:25]
NO_PLAN = "MERGE: captain\nCLAIMS: a.py\n\nThe text of the description.\n"
DROPPED = "PLAN: the plan\nCLAIMS: a.py\n\nThe text of the description.\n"
PLAIN = "Just some words in a description, no keyed line.\n"
COMPLETE = OLD + "One more line.\n"
FORMS = ("edit --body", "edit -b", "edit --body-file", "edit -F -", "api -f", "api -F @file", "api --input -")

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
files = {}
for a in argv:
    for p in (a, a.split("=@", 1)[-1], a[2:]):
        if os.path.isfile(p):
            files[p] = open(p).read()
is_read = len(argv) == 2 and argv[0] == "api" and argv[1].startswith("repos/") and "/issues/" in argv[1]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps({"argv": argv, "stdin": stdin, "files": files, "read": is_read}) + "\\n")
if is_read:
    mode = os.environ.get("FAKE_READ", "ok")
    if mode == "fail":
        sys.exit(1)
    if mode == "empty":
        sys.exit(0)
    if mode == "sleep":
        time.sleep(4)
    data = {"number": 1, "body": os.environ.get("FAKE_OLD", "")}
    if mode != "issue":
        data["pull_request"] = {"url": "x"}
    print(json.dumps(data))
"""


def load_file(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BodyRig(Rig):
    def __init__(self, shim_source=None, guard_source=None):
        Rig.__init__(self, shim_source)
        (self.bin / "gh").write_text(FAKE_GH)
        if guard_source is not None:
            (self.shims / "body_guard.py").write_text(guard_source)

    def go(self, *args, stdin=None, read="ok", old=OLD, extra=None):
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (self.shims, self.bin), "HOME": str(self.home), "TMPDIR": str(self.home), "FAKE_GH_LOG": str(self.log),
               "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SWITCHBOARD_BIN": str(self.sb), "FLEET_SHIM_LOOKUP_SECONDS": "10",
               "FAKE_READ": read, "FAKE_OLD": old}
        env.update(extra or {})
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run([sys.executable, str(self.shims / "gh"), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, [c for c in calls if not c["read"]], [c for c in calls if c["read"]]

    def form(self, name, body, number=1):
        """(argv, stdin) of one form of a body write."""
        path = self.home / ("body%d.md" % abs(hash(name)))
        if name == "edit --body":
            return ["pr", "edit", str(number), "--body", body], None
        if name == "edit -b":
            return ["pr", "edit", str(number), "-b", body], None
        if name == "edit --body-file":
            path.write_text(body)
            return ["pr", "edit", str(number), "--body-file", str(path)], None
        if name == "edit -F -":
            return ["pr", "edit", str(number), "-F", "-"], body
        if name == "api -f":
            return ["api", "-X", "PATCH", "repos/o/r/pulls/%d" % number, "-f", "body=" + body], None
        if name == "api -F @file":
            path.write_text(body)
            return ["api", "-X", "PATCH", "repos/o/r/issues/%d" % number, "-F", "body=@" + str(path)], None
        return ["api", "-X", "PATCH", "repos/o/r/pulls/%d" % number, "--input", "-"], json.dumps({"body": body})

    def audit(self):
        path = self.state / "gate-failopen.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def scenario(rig):
    """The failures of the guard on the 7 forms (S3): empty for the real shim. Each message starts with 'body-guard:'."""
    bad = []
    for name in FORMS:
        for label, body in (("empty body", ""), ("cut-off body", CUT), ("body without PLAN:", NO_PLAN)):
            args, stdin = rig.form(name, body)
            done, writes, _reads = rig.go(*args, stdin=stdin)
            if writes or done.returncode != 1 or "gh: refused:" not in done.stderr:
                bad.append("body-guard: %s not refused in %s" % (label, name))
        for label, body, old in (("empty body", "", ""), ("empty body", "", PLAIN), ("cut-off body", PLAIN[:12], PLAIN)):
            args, stdin = rig.form(name, body)
            done, writes, _reads = rig.go(*args, stdin=stdin, old=old)
            if writes or done.returncode != 1 or "gh: refused:" not in done.stderr:
                bad.append("body-guard: %s not refused in %s (old body without keyed lines)" % (label, name))
        args, stdin = rig.form(name, COMPLETE)
        done, writes, _reads = rig.go(*args, stdin=stdin)
        sent = json.dumps(writes[0]) if writes else ""
        if done.returncode != 0 or len(writes) != 1 or COMPLETE.splitlines()[-1] not in json.dumps(writes[0]["stdin"]) + json.dumps(writes[0]["files"]) + sent:
            bad.append("body-guard: complete body not passed in %s" % name)
    return bad


class Base(unittest.TestCase):
    def setUp(self):
        self.rig = BodyRig()
        self.addCleanup(self.rig.close)


class TestBodyGuardRule(unittest.TestCase):
    """S2, U4: the pure rule."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("body_guard_under_test", str(GUARD_PATH))
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_s2_three_refusals_one_line_each(self):
        for new in ("", "  \n", CUT, NO_PLAN):
            said = self.mod.check(OLD, new)
            self.assertIsInstance(said, str, repr(new))
            self.assertNotIn("\n", said)
        self.assertEqual(sum(1 for new in ("", CUT, NO_PLAN) if self.mod.check(OLD, new)), 3)

    def test_s2_acceptances(self):
        self.assertIsNone(self.mod.check(OLD, OLD))
        self.assertIsNone(self.mod.check(OLD, COMPLETE))
        self.assertIsNone(self.mod.check(OLD, OLD.replace("MERGE: captain", "MERGE: automatic")))
        self.assertIsNone(self.mod.check(OLD, NO_PLAN, ("PLAN:",)))
        self.assertIsNotNone(self.mod.check(OLD, NO_PLAN, ("MERGE:",)))
        self.assertIsNone(self.mod.check("", "anything"))

    def test_s2_the_module_holds_no_line_name_and_no_flag(self):
        text = GUARD_PATH.read_text()
        for word in ("MERGE", "PLAN", "CLAIMS"):
            self.assertNotIn(word, text)
        self.assertIsNone(re.search(r"--[a-z]", text))

    def test_u4_the_module_reads_no_file_and_calls_no_process(self):
        tree = ast.parse(GUARD_PATH.read_text())
        imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imports, {"re"})
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"open", "exec", "eval", "__import__"})


class TestBodyGuardShim(Base):
    def test_s3_every_form_refuses_three_bad_bodies_and_passes_a_complete_one(self):
        self.assertEqual(scenario(self.rig), [])

    def test_s3_the_refusal_names_what_is_lacking_and_the_shim_adds_the_option(self):
        args, stdin = self.rig.form("edit --body", NO_PLAN)
        done, writes, reads = self.rig.go(*args)
        self.assertEqual(writes, [])
        self.assertEqual(len(reads), 1)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1)
        self.assertIn("lacks the line PLAN:", done.stderr)
        self.assertIn("--drop-line KEY", done.stderr)

    def test_s4_unreadable_old_body_is_refused(self):
        for read in ("fail", "empty"):
            done, writes, _ = self.rig.go("pr", "edit", "1", "--body", COMPLETE, read=read)
            self.assertEqual((writes, done.returncode), ([], 1), read)
            self.assertIn("could not be read", done.stderr)
        done, writes, _ = self.rig.go("pr", "edit", "--body", COMPLETE, "--repo", "x")   # no number
        self.assertEqual((writes, done.returncode), ([], 1))
        self.assertIn("cannot be resolved", done.stderr)

    def test_s4_a_plain_issue_write_passes_unchanged(self):
        args = ["api", "-X", "PATCH", "repos/o/r/issues/1", "-f", "body="]
        done, writes, reads = self.rig.go(*args, read="issue")
        self.assertEqual(done.returncode, 0)
        self.assertEqual([w["argv"] for w in writes], [args])

    def test_s4_an_unreadable_new_body_file_is_refused(self):
        done, writes, _ = self.rig.go("pr", "edit", "1", "--body-file", str(self.rig.home / "missing"))
        self.assertEqual((writes, done.returncode), ([], 1))
        self.assertIn("cannot be opened", done.stderr)

    def test_s5_drop_line_accepted_audited_and_stripped(self):
        for args in (["pr", "edit", "1", "--drop-line", "MERGE:", "--body", DROPPED],
                     ["api", "-X", "PATCH", "repos/o/r/pulls/1", "--drop-line", "MERGE:", "-f", "body=" + DROPPED]):
            done, writes, _ = self.rig.go(*args)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(writes), 1)
            self.assertNotIn("--drop-line", " ".join(writes[0]["argv"]))
            self.assertNotIn("MERGE:", " ".join(writes[0]["argv"]).replace("--drop-line", ""))
        events = [e for e in self.rig.audit() if e["event"] == "body.drop"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["for"], "MERGE:")
        self.assertEqual([e["pr"] for e in events], ["acme/app#1", "o/r#1"])
        self.assertIn("caller=alice-bot", events[0]["detail"])

    def test_s5_audit_is_written_before_gh_runs(self):
        order = self.rig.home / "order"
        gh = self.rig.bin / "gh"
        gh.write_text(gh.read_text().replace("if is_read:", "if not is_read:\n    open(%r, 'w').write(open(os.path.join(os.environ['FLEET_SWITCHBOARD_STATE'], 'gate-failopen.jsonl')).read())\nif is_read:" % str(order), 1))
        done, writes, _ = self.rig.go("pr", "edit", "1", "--drop-line", "MERGE:", "--body", "PLAN: the plan\nCLAIMS: a.py\n\nThe text of the description.\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("body.drop", order.read_text())

    def test_s5_another_lost_key_is_refused_and_an_unwritable_audit_refuses(self):
        done, writes, _ = self.rig.go("pr", "edit", "1", "--drop-line", "MERGE:", "--body", "CLAIMS: a.py\n\nThe text of the description.\n")
        self.assertEqual((writes, done.returncode), ([], 1))
        self.assertIn("lacks the line PLAN:", done.stderr)
        done, writes, _ = self.rig.go("pr", "edit", "1", "--drop-line", "MERGE:", "--body", DROPPED,
                                      extra={"FLEET_SWITCHBOARD_STATE": str(self.rig.home / "nostate")})
        self.assertEqual((writes, done.returncode), ([], 1))
        self.assertIn("could not be audited", done.stderr)

    def test_u7_bad_drop_values_and_other_commands(self):
        for value in ("MERGE", "merge:", ""):
            done, writes, reads = self.rig.go("pr", "edit", "1", "--drop-line", value, "--body", COMPLETE)
            self.assertEqual((writes, reads, done.returncode), ([], [], 1), value)
            self.assertIn("--drop-line takes an all-capitals key", done.stderr)
        done, writes, reads = self.rig.go("issue", "edit", "1", "--drop-line", "MERGE:", "--body", COMPLETE)
        self.assertEqual((writes, reads, done.returncode), ([], [], 1))
        self.assertIn("valid only for gh pr edit", done.stderr)

    def test_s6_other_writes_are_not_checked(self):
        for args in (("issue", "edit", "1", "--body", ""), ("pr", "create", "--title", "t", "--body", ""),
                     ("api", "graphql", "-f", "query=mutation { updatePullRequest(input: {pullRequestId: \"x\", body: \"\"}) { clientMutationId } }")):
            done, writes, reads = self.rig.go(*args)
            self.assertEqual(reads, [], args)
        done, writes, reads = self.rig.go("issue", "edit", "1", "--body", "")
        self.assertEqual([w["argv"] for w in writes], [["issue", "edit", "1", "--body", ""]])

    def test_u5_the_deadline(self):
        started = time.monotonic()
        done, writes, _ = self.rig.go("pr", "edit", "1", "--body", COMPLETE, read="sleep", extra={"FLEET_SHIM_BODY_SECONDS": "0.2"})
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual((writes, done.returncode), ([], 1))
        self.assertIn("could not be read", done.stderr)
        shim = load_file("shim_under_test", REPO / "shims" / "gh")
        saved = os.environ.pop("FLEET_SHIM_BODY_SECONDS", None)
        try:
            self.assertEqual(shim.body_seconds(), 5.0)
            os.environ["FLEET_SHIM_BODY_SECONDS"] = "99"
            self.assertEqual(shim.body_seconds(), 10.0)
            os.environ["FLEET_SHIM_BODY_SECONDS"] = "0.001"
            self.assertEqual(shim.body_seconds(), 0.2)
        finally:
            os.environ.pop("FLEET_SHIM_BODY_SECONDS", None)
            if saved is not None:
                os.environ["FLEET_SHIM_BODY_SECONDS"] = saved

    def test_u6_stdin_reaches_gh_with_the_same_bytes(self):
        body = COMPLETE + "caf\u00e9 \u2713\n\n"
        done, writes, _ = self.rig.go("pr", "edit", "1", "-F", "-", stdin=body)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([w["stdin"] for w in writes], [body])
        data = json.dumps({"body": body})
        done, writes, _ = self.rig.go("api", "-X", "PATCH", "repos/o/r/pulls/1", "--input", "-", stdin=data)
        self.assertEqual([w["stdin"] for w in writes], [data])

    def test_s7_the_own_writers_pass_the_rule(self):
        impact_hold = load_file("impact_hold_s7", REPO / "bin" / "impact_hold.py")
        daemon = ast.parse((REPO / "bin" / "fleet-switchboard").read_text())
        marker = next(n for n in daemon.body if isinstance(n, ast.FunctionDef) and n.name == "pr_marker")
        impact_hold.re = re
        exec(compile(ast.Module([marker], []), "pr_marker", "exec"), impact_hold.__dict__)   # the daemon's own reader of the first line, bound as `bind` does
        spec = importlib.util.spec_from_file_location("body_guard_s7", str(GUARD_PATH))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        bodies = [OLD, "MERGE: captain\n\nPLAN: p\n", "MERGE: captain Rationale: because\nPLAN: p\nCLAIMS: x\n\ntext\n"]
        passed = 0
        for body in bodies:
            for source, target in (("captain", "automatic"),):
                cleared = impact_hold.marker_edit(body, source, target)[0]
                back = impact_hold.marker_edit(cleared, target, source)[0]
                for new, old in ((cleared, body), (back, cleared)):
                    self.assertIsNone(guard.check(old, new), new)
                    passed += 1
        self.assertEqual(passed, 6)
        tree = ast.parse((REPO / "bin" / "switchboard" / "pr_explain.py").read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertNotIn("subprocess", names)

    def test_s9_the_documentation_has_the_sentence(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        found = [s for s in re.split(r"(?<=\.)\s+", text) if "--drop-line" in s and "pull request body is checked on every write" in s]
        self.assertEqual(len(found), 1)
        for word in ("empty", "cut off", "keyed line"):
            self.assertIn(word, found[0])


class TestBodyGuardMutants(unittest.TestCase):
    """S8: mutants on a SOURCE COPY, each applied exactly once, the control first."""
    SHIM = (REPO / "shims" / "gh").read_text()
    GUARD = GUARD_PATH.read_text()

    def run_mutant(self, which, old, new):
        source = self.SHIM if which == "shim" else self.GUARD
        self.assertEqual(source.count(old), 1, "mutant applies exactly once")
        mutated = source.replace(old, new)
        rig = BodyRig(shim_source=mutated if which == "shim" else None, guard_source=mutated if which == "guard" else None)
        self.addCleanup(rig.close)
        control = BodyRig()
        self.addCleanup(control.close)
        self.assertEqual(scenario(control), [], "the unmutated copy passes first")
        return scenario(rig)

    def test_s8_a_mutant_that_skips_the_rule(self):
        bad = self.run_mutant("shim", 'said = guard.check(data.get("body") or "", text, dropped)', "said = None")
        self.assertTrue(any(m.startswith("body-guard: empty body not refused") for m in bad), bad)

    def test_s8_a_mutant_that_accepts_an_empty_body(self):
        bad = self.run_mutant("guard", "if not new.strip():", "if False:")
        self.assertTrue(any(m.startswith("body-guard: empty body not refused") for m in bad), bad)

    def test_s8_a_mutant_that_does_not_refuse_a_prefix(self):
        bad = self.run_mutant("guard", "if new != old and old.startswith(new):", "if False:")
        self.assertTrue(any(m.startswith("body-guard: cut-off body not refused") for m in bad), bad)

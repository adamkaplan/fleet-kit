"""#1751 pull request B: the gh shim refuses a body write that drops the Intent or the Done-when of the issue it replaces.

The shim runs as a COPY in a private directory in front of a fake real `gh` that answers a body read from the rig and records every write (argv, stdin, the
text of the files it was handed). Reads never reach the write log. Fakes only: no test calls GitHub. The mutants are applied once to a source copy
(the anchored text must occur exactly once), and a battery of the plan's forms is judged on the unmutated copy first (the control).
"""
import importlib.util
import itertools
import json
import subprocess
import sys
import unittest

from test_gh_shim_sign import REPO, SHIM_DIR, Rig

BOTH = "## Intent\nwhy this is wanted\n\nDone when: it works\n\nNext: step\n"
NO_DONE = "## Intent\nwhy this is wanted\n\nNext: step\n"
NO_INTENT = "Done when: it works\n\nNext: step\n"
ONLY_LINE = "Next: step\n"
DROP_WORDS = "would drop the Intent or the Done-when"

FAKE_GH = """#!/usr/bin/env python3
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
        (self.bin / "gh").write_text(FAKE_GH)
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
               ("gh_body.py", "a failed read is read as a body with no parts", "    if data is None:\n        return UNREAD\n",
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


if __name__ == "__main__":
    unittest.main()

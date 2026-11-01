"""#1531: a reader of a changed-file list reads it whole or says it is not (`tools/arch-overlap` and the impact reviewer's role file).

Neutral names (acme-org/widget-api, acme/api#2). HOST is bin/test-switchboard, set by its loader block. Each mutant is the real file with one
replacement, applied (asserted), caught by an anchored failure text, and its unmutated control passes the same check. No test measures a pull request's size.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST = None   # set by bin/test-switchboard
TOOL = REPO / "tools" / "arch-overlap"
ROLE = REPO / "agents" / "reviewers" / "reviewer-impact.md"
DAEMON, TESTS = "bin/fleet-switchboard", "bin/test-switchboard"
BASELINE = "#7 edit f\n    symbols: f\n\nBy section marker of bin/fleet-switchboard:\n  the first: #7\n\n1 open pull requests touch the daemon or its tests\n"   # the output of the tool before this change


def make_repo(root):
    repo = Path(root) / "clone"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(a), check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (repo / "bin").mkdir()
    (repo / DAEMON).write_text("# ------ the first\nX = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n")
    (repo / TESTS).write_text("class A:\n    x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("remote", "add", "origin", str(repo))
    git("checkout", "-q", "-b", "pr")
    (repo / DAEMON).write_text("# ------ the first\nX = 1\n\n\ndef f():\n    return 9\n\n\ndef g():\n    return 2\n")
    git("commit", "-q", "-am", "pr")
    git("update-ref", "refs/pull/7/head", "HEAD")
    git("checkout", "-q", "main")
    return repo


def run_tool(tool, root, list_out, list_rc=0, diff_names=None, diff_rc=1):
    """Run `tool` in a fresh clone with a fake `gh`: `pr list` answers list_out and list_rc, `pr diff N --name-only` answers diff_names and diff_rc."""
    repo = make_repo(tempfile.mkdtemp(dir=root))
    bindir = Path(tempfile.mkdtemp(dir=root))
    (bindir / "list.out").write_text(list_out)
    (bindir / "diff.out").write_text("\n".join(diff_names or []))
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\ncase \"$2\" in\n  list) cat '%s/list.out'; exit %d;;\n  diff) cat '%s/diff.out'; exit %d;;\nesac\nexit 9\n" % (bindir, list_rc, bindir, diff_rc))
    gh.chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
    return subprocess.run([sys.executable, str(tool), "--base", "main"], cwd=repo, env=env, capture_output=True, text=True)


def pr_of(count, listed, **extra):
    """A listing entry as `gh` returns it: `listed` paths (the daemon file last when `count` exceeds the listing) and the numeric count."""
    files = [{"path": "src/f%03d.py" % i} for i in range(listed - 1)] + [{"path": DAEMON}]
    entry = {"number": 7, "title": "edit f", "files": files}
    entry.update(extra)
    if count is not None:
        entry["changedFiles"] = count
    return entry


def overlap_claims(tool, root):
    """Every claim about the reader, as assertions with a leading tag (a mutant fails one of them with its tag)."""
    names = ["src/f%03d.py" % i for i in range(138)] + [DAEMON]
    big = json.dumps([dict(pr_of(139, 100), files=[{"path": "src/f%03d.py" % i} for i in range(100)], changedFiles=139)])
    done = run_tool(tool, root, big, diff_names=names, diff_rc=0)
    if done.returncode != 0 or "#7 edit f" not in done.stdout or "not whole" in done.stdout:
        raise AssertionError("OMITTED a pull request whose listing is cut is not read whole: %r %r" % (done.stdout, done.stderr))
    done = run_tool(tool, root, big, diff_rc=1)   # the whole list cannot be read: it is listed by number, never omitted
    lines = [l for l in done.stdout.splitlines() if "not whole" in l]
    if done.returncode != 0 or len(lines) != 1 or "#7" not in lines[0]:
        raise AssertionError("OMITTED a pull request that cannot be read whole is not listed as not whole: %r" % done.stdout)
    for what, entry in (("missing", pr_of(None, 1)), ("string", pr_of("1", 1))):
        done = run_tool(tool, root, json.dumps([entry]), diff_rc=1)
        lines = [l for l in done.stdout.splitlines() if "not whole" in l]
        if done.returncode != 0 or len(lines) != 1 or "#7" not in lines[0]:
            raise AssertionError("WHOLE-MISREAD a count that is %s is read as whole: %r" % (what, done.stdout))
    for what, out, rc in (("failed", "", 1), ("empty", "", 0), ("not-json", "gateway timeout\n", 0)):
        done = run_tool(tool, root, out, list_rc=rc)
        if "Traceback" in done.stderr:
            raise AssertionError("TRACEBACK on %s output: %s" % (what, done.stderr))
        if done.returncode != 2 or not done.stderr.startswith("arch-overlap: gh pr list failed") or done.stdout:
            raise AssertionError("UNREADABLE %s output is not refused: %r %r %r" % (what, done.returncode, done.stdout, done.stderr))


def role_claims(text):
    steps = text.split("1. List the changed files", 1)
    if len(steps) != 2 or "`gh pr diff <pr> --name-only`" not in steps[1].split("\n", 1)[0]:
        raise AssertionError("ROLE-FIRST-PAGE step 1 does not list with gh pr diff --name-only")
    step1 = steps[1].split("\n", 1)[0]
    if "gh pr view <pr> --json files" in text:
        raise AssertionError("ROLE-FIRST-PAGE the first-page command is still in the role file")
    for need in ("`gh pr view <pr> --json changedFiles`", "`the list is not whole`", "over GitHub's limit"):
        if need not in step1:
            raise AssertionError("ROLE-MISSING step 1 lacks %s" % need)


def build():
    host = HOST

    class TestChangedFilesWhole(unittest.TestCase):
        def setUp(self):
            self.root = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, self.root, True)

        def tool_with(self, old, new):
            source = TOOL.read_text()
            self.assertIn(old, source, "the mutant is applied")
            mutated = source.replace(old, new)
            self.assertNotEqual(mutated, source)
            path = Path(self.root) / "mutant-arch-overlap"
            path.write_text(mutated)
            return path

        def role_with(self, old, new):
            text = ROLE.read_text()
            self.assertIn(old, text, "the mutant is applied")
            self.assertNotEqual(text.replace(old, new), text)
            return text.replace(old, new)

        def test_s2_s9_the_real_tool_passes_every_claim(self):
            overlap_claims(TOOL, self.root)

        def test_s3_a_pull_request_with_a_matching_count_gives_the_stored_baseline(self):
            done = run_tool(TOOL, self.root, json.dumps([pr_of(1, 1)]), diff_rc=1)   # a diff call would list it as not whole
            self.assertEqual((done.returncode, done.stdout), (0, BASELINE), done.stderr)

        def test_s5_a_reader_that_omits_a_pull_request_whose_count_differs_is_caught(self):
            mutant = self.tool_with('not_whole.append(pr.get("number"))', "continue")
            with self.assertRaisesRegex(AssertionError, r"^OMITTED "):
                overlap_claims(mutant, self.root)

        def test_s5_a_reader_that_takes_a_missing_count_as_equal_is_caught(self):
            mutant = self.tool_with('files, total = pr.get("files"), pr.get("changedFiles")',
                                    'files = pr.get("files"); total = pr.get("changedFiles", len(files))')
            with self.assertRaisesRegex(AssertionError, r"^WHOLE-MISREAD "):
                overlap_claims(mutant, self.root)

        def test_s5_a_reader_that_lets_unreadable_output_raise_is_caught(self):
            mutant = self.tool_with("if not isinstance(prs, list) or not all(isinstance(pr, dict) for pr in prs):", "if False:")
            with self.assertRaisesRegex(AssertionError, r"^TRACEBACK "):
                overlap_claims(mutant, self.root)

        def test_s4_s5_the_role_file_lists_with_the_diff_and_the_first_page_mutant_is_caught(self):
            role_claims(ROLE.read_text())   # the unmutated control
            mutant = self.role_with("1. List the changed files with `gh pr diff <pr> --name-only` (bare), and",
                                    "1. List the changed files with `gh pr view <pr> --json files` (bare), and")
            with self.assertRaisesRegex(AssertionError, r"^ROLE-FIRST-PAGE "):
                role_claims(mutant)

        def test_s7_the_reading_order_is_the_order_of_the_listing_and_step_4_refers_to_it(self):
            text = ROLE.read_text()
            step1 = text.split("1. List the changed files", 1)[1].split("\n", 1)[0]
            step4 = text.split("4. Put every file", 1)[1].split("\n", 1)[0]
            self.assertIn("The order of the `gh pr diff <pr> --name-only` listing is your reading order", step1)
            self.assertIn("in the changed-file order (the order of the `gh pr diff <pr> --name-only` listing of step 1)", step4)
            self.assertNotIn("That order is your reading order", text)

        def test_s8_the_new_bare_commands_are_accepted_and_a_redirect_is_refused(self):
            verdict = host.TestReviewerSubagents.verdict
            for role in host.TestReviewerSubagents.ROLES:
                head = (REPO / "agents" / "reviewers" / ("reviewer-%s.md" % role)).read_text().split("---\n")[1]
                perms = [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(r'- \{action: "?([^,"]+)"?, resource: "([^"]+)", effect: (allow|deny)\}', head)]
                for command in ("gh pr view 9 --json changedFiles", "gh pr diff 9 --name-only"):
                    self.assertEqual(verdict(perms, "shell", command), "allow", (role, command))
                for command in ("gh pr diff 9 --name-only > out.txt", "gh pr view 9 --json changedFiles >> out"):
                    self.assertEqual(verdict(perms, "shell", command), "deny", (role, command))

    return TestChangedFilesWhole

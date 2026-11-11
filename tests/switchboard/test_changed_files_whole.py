"""#1531: a reader of a changed-file list reads it whole or says it is not (`tools/arch-overlap` and the impact reviewer's role file).

Counts: 6 not whole cases (138, 140, 139 with the 120th wrong, 139 other files, a failed page read, 3,100 files), 2 read whole (139 equal, exactly 3,000),
1 lying changedFiles (3,000 for 3,100 names) not whole, 20 diff calls, 40 page reads, 5 not whole by number (S10), 4+3+1 mutants of S11/S5/S12 with controls.
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


def write_pages(bindir, number, names):
    """The files endpoint of pull request `number` as GitHub pages it: pages of 100 filenames (a last page of fewer, or empty after an exact multiple)."""
    for k in range(len(names) // 100 + 1):
        (bindir / ("page.%s.%d.json" % (number, k + 1))).write_text(json.dumps([{"filename": n} for n in names[k * 100:(k + 1) * 100]]))


API_CASE = "  repos/*) n=$(echo \"$2\" | sed 's#.*pulls/\\([0-9]*\\)/files.*#\\1#'); k=${2##*page=}; echo \"$n $k\" >> '%s/pagecalls'; cat \"%s/page.$n.$k.json\" 2>/dev/null || exit 1; exit 0;;\n"


def run_tool(tool, root, list_out, list_rc=0, diff_names=None, diff_rc=1, pages=None):
    """Run `tool` in a fresh clone with a fake `gh`: `pr list` answers list_out and list_rc, `pr diff N --name-only` answers diff_names and diff_rc."""
    repo = make_repo(tempfile.mkdtemp(dir=root))
    bindir = Path(tempfile.mkdtemp(dir=root))
    (bindir / "list.out").write_text(list_out)
    (bindir / "diff.out").write_text("\n".join(diff_names or []))
    (bindir / "pagecalls").write_text("")
    if pages is not None:
        write_pages(bindir, 7, pages)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\ncase \"$2\" in\n  list) cat '%s/list.out'; exit %d;;\n  diff) cat '%s/diff.out'; exit %d;;\n%sesac\nexit 9\n" % (bindir, list_rc, bindir, diff_rc, API_CASE % (bindir, bindir)))
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
    done = run_tool(tool, root, big, diff_names=names, diff_rc=0, pages=names)
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


def run_many(tool, root, prs, diffs, files=None):
    """Run `tool` with a fake `gh` that answers `pr list` with `prs`, `pr diff N --name-only` with diffs[N] = (names, exit status) and the files endpoint
    of N with pages of 100 of files[N] (a list of names; None: every page read fails; default: the 139 plain names); returns (done, the numbers asked, the page reads)."""
    repo = make_repo(tempfile.mkdtemp(dir=root))
    bindir = Path(tempfile.mkdtemp(dir=root))
    (bindir / "list.out").write_text(json.dumps(prs))
    (bindir / "calls").write_text("")
    (bindir / "pagecalls").write_text("")
    for number in diffs:
        names = (files or {}).get(number, plain_names(139))
        if names is not None:
            write_pages(bindir, number, names)
    for number, (names, rc) in diffs.items():
        (bindir / ("diff.%s.out" % number)).write_text("\n".join(names))
        (bindir / ("diff.%s.rc" % number)).write_text(str(rc))
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\ncase \"$2\" in\n  list) cat '%s/list.out'; exit 0;;\n  diff) echo \"$3\" >> '%s/calls'; cat \"%s/diff.$3.out\"; exit $(cat \"%s/diff.$3.rc\");;\n%sesac\nexit 9\n" % (bindir, bindir, bindir, bindir, API_CASE % (bindir, bindir)))
    gh.chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
    done = subprocess.run([sys.executable, str(tool), "--base", "main"], cwd=repo, env=env, capture_output=True, text=True)
    return done, (bindir / "calls").read_text().split(), (bindir / "pagecalls").read_text().splitlines()


def cut_pr(number, listed_names, count=139):
    return {"number": number, "title": "edit f", "files": [{"path": n} for n in listed_names], "changedFiles": count}


def plain_names(count, extra=()):
    return ["src/f%03d.py" % i for i in range(count - len(extra))] + list(extra)


def not_whole_numbers(done):
    lines = [l for l in done.stdout.splitlines() if l.startswith("not whole (")]
    return [int(n) for n in re.findall(r"#(\d+)", lines[0].split(":", 1)[1])] if lines else []


def bound_claims(tool, root):
    """S10: 25 cut pull requests (100 listed, 139 counted), the fake diff answers 139 names: exactly 20 calls, the first 20 read whole, the last 5 not whole."""
    prs = [cut_pr(100 + i, plain_names(100)) for i in range(1, 26)]
    done, calls, pages = run_many(tool, root, prs, {100 + i: (plain_names(139), 0) for i in range(1, 26)})
    if done.returncode != 0:
        raise AssertionError("BOUND-RUN the tool failed: %r" % done.stderr)
    if len(calls) != 20:
        raise AssertionError("BOUND %d gh pr diff calls where 20 are expected" % len(calls))
    if len(pages) != 40:
        raise AssertionError("BOUND-PAGES %d page reads where 40 are expected" % len(pages))
    if not_whole_numbers(done) != list(range(121, 126)):
        raise AssertionError("BOUND-LIST the not whole line is %r where #121..#125 are expected" % not_whole_numbers(done))


def trust_claims(tool, root):
    """S11: the fetched names are compared, as a SET, with every page of the files endpoint and their number with changedFiles. 138 names, 140 names, 139 names with
    the 120th wrong, 139 other names, a failed page read and a pull request of 3,100 files are `not whole` and the names are not used (every answer holds the daemon
    file, so a trusted one would put the pull request in the overlap report); 139 names equal as a set to the endpoint's are whole."""
    listed = plain_names(100)
    truth = plain_names(139, [DAEMON])

    counted = []

    def refused(label, names, files=truth, count=139, calls=("7",)):
        done, asked, pages = run_many(tool, root, [cut_pr(7, listed, count)], {7: (names, 0)}, {7: files})
        if done.returncode != 0 or asked != list(calls) or not_whole_numbers(done) != [7] or "#7 edit f" in done.stdout:
            raise AssertionError("%s the answer is used: %r %r" % (label, done.stdout, done.stderr))
        counted.append(label)
        return pages

    refused("TRUSTED an answer of 138 names for a count of 139", plain_names(138, [DAEMON]))
    refused("TRUSTED an answer of 140 names for a count of 139", plain_names(140, [DAEMON]))
    refused("TRUSTED-OTHER 139 names that are all other files", ["other/g%03d.py" % i for i in range(138)] + [DAEMON])
    wrong = list(truth)
    wrong[119] = "zzz/g119.py"   # sorts last: beyond the first 100 names in any order
    refused("TRUSTED-120TH 139 names of which the 120th differs from the endpoint's", wrong)
    refused("TRUSTED-FAILED a failed page read", truth, files=None)
    pages = refused("TRUSTED-3100 a pull request of 3,100 files", plain_names(3100, [DAEMON]), files=plain_names(3100, [DAEMON]), count=3100, calls=())
    if len(pages) > 30:
        raise AssertionError("PAGES-3100 %d page reads where at most 30 are allowed" % len(pages))
    exact = plain_names(3000, [DAEMON])   # exactly 3,000 files: 30 full pages, the diff answers the same names
    done, calls, pages = run_many(tool, root, [cut_pr(7, listed, 3000)], {7: (exact, 0)}, {7: exact})
    if done.returncode != 0 or len(pages) != 30:
        raise AssertionError("PAGES-3000 %d page reads where 30 are expected: %r" % (len(pages), done.stderr))
    total = len(counted) + len(not_whole_numbers(done))
    if total != 6 or "#7 edit f" not in done.stdout:
        raise AssertionError("TRUSTED-3000 %d not whole where 6 expected: a pull request of exactly 3,000 files is read whole: %r" % (total, done.stdout))
    lie = plain_names(3100, [DAEMON])   # says 3,000, the diff answers 3,100 names
    done, calls, pages = run_many(tool, root, [cut_pr(7, listed, 3000)], {7: (lie, 0)}, {7: exact})
    if done.returncode != 0 or not_whole_numbers(done) != [7] or "#7 edit f" in done.stdout:
        raise AssertionError("LIED a changedFiles of 3,000 for a diff of 3,100 names is read whole: %r %r" % (done.stdout, done.stderr))
    done, calls, pages = run_many(tool, root, [cut_pr(7, listed)], {7: (truth, 0)}, {7: truth})
    if done.returncode != 0 or not_whole_numbers(done) != [] or "#7 edit f" not in done.stdout:
        raise AssertionError("TRUSTED-NOT an answer of 139 names equal as a set to the endpoint's is not read whole: %r %r" % (done.stdout, done.stderr))


def partial_claims(tool, root):
    """S12: both whole-reads fail. #7 lists the daemon file, #8 lists neither file."""
    prs = [cut_pr(7, plain_names(100, [DAEMON])), cut_pr(8, plain_names(100))]
    done, calls, pages = run_many(tool, root, prs, {7: ([], 1), 8: ([], 1)})
    if done.returncode != 0 or not_whole_numbers(done) != [7, 8]:
        raise AssertionError("PARTIAL-LINE the not whole line is %r where #7 #8 are expected: %r" % (not_whole_numbers(done), done.stderr))
    reported = re.findall(r"^#(\d+) edit f$", done.stdout, re.M)
    if reported != ["7"] or "1 open pull requests touch the daemon or its tests" not in done.stdout:
        raise AssertionError("PARTIAL %d pull requests in the report where 1 (#7 only) is expected: %r" % (len(reported), done.stdout))


def build():
    host = HOST

    class TestChangedFilesWhole(unittest.TestCase):
        def setUp(self):
            self.root = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, self.root, True)

        def tool_with(self, old, new):
            source = TOOL.read_text()
            self.assertEqual(source.count(old), 1, "the mutant is applied exactly once")
            mutated = source.replace(old, new)
            self.assertNotEqual(mutated, source)
            path = Path(self.root) / "mutant-arch-overlap"
            path.write_text(mutated)
            return path

        def role_with(self, old, new):
            text = ROLE.read_text()
            self.assertEqual(text.count(old), 1, "the mutant is applied exactly once")
            self.assertNotEqual(text.replace(old, new), text)
            return text.replace(old, new)

        def test_s2_s9_the_real_tool_passes_every_claim(self):
            overlap_claims(TOOL, self.root)

        def lower_bound_check(self, tool):
            prs = [cut_pr(7, plain_names(100, [DAEMON]))]
            done, calls, pages = run_many(tool, self.root, prs, {7: ([], 1)})
            lines = [l for l in done.stdout.splitlines() if l.startswith("not whole (")]
            if done.returncode != 0 or len(lines) != 1 or not_whole_numbers(done) != [7]:
                raise AssertionError("LOWER-LINE the not whole line is missing: %r %r" % (done.stdout, done.stderr))
            if "#7 edit f" not in done.stdout:
                raise AssertionError("LOWER-ABOVE the listed names holding the daemon are not reported above: %r" % done.stdout)
            if "their overlap is unknown" in lines[0] or "an overlap listed above is a lower bound" not in lines[0]:
                raise AssertionError("LOWER-TEXT the not whole line is false or lacks the lower bound: %r" % lines[0])

        def test_1531_the_not_whole_line_says_the_overlap_above_is_a_lower_bound(self):
            self.lower_bound_check(TOOL)

        def test_1531_the_old_unknown_overlap_line_is_caught(self):
            self.lower_bound_check(TOOL)   # the unmutated control
            mutant = self.tool_with("an overlap listed above is a lower bound, and a pull request listed here and not above may still overlap",
                                    "their overlap is unknown")
            with self.assertRaisesRegex(AssertionError, r"^LOWER-TEXT "):
                self.lower_bound_check(mutant)

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

        def test_s10_the_bound_of_20_fetches_and_the_rest_are_listed_not_whole(self):
            """25 pull requests, each cut (100 listed, 139 counted): 20 `gh pr diff` calls, #121..#125 are `not whole`."""
            bound_claims(TOOL, self.root)   # the unmutated control

        def test_s10_a_tool_with_no_bound_is_caught(self):
            mutant = self.tool_with("names is None and fetched < WHOLE_FETCH_BOUND:", "names is None:")
            with self.assertRaisesRegex(AssertionError, r"^BOUND 25 gh pr diff calls where 20 are expected"):
                bound_claims(mutant, self.root)

        def test_s10_a_tool_with_a_bound_of_21_is_caught(self):
            mutant = self.tool_with("WHOLE_FETCH_BOUND = 20 ", "WHOLE_FETCH_BOUND = 21 ")
            with self.assertRaisesRegex(AssertionError, r"^BOUND 21 gh pr diff calls where 20 are expected"):
                bound_claims(mutant, self.root)

        def test_s11_fetched_names_that_differ_from_the_files_endpoint_are_not_used(self):
            """NO limit is left: the whole fetched list is compared as a set with every page of the files endpoint (at most 30 pages) and its number with
            changedFiles. 138, 140, 139 with the 120th wrong, 139 other names, a failed page read and a pull request of 3,100 files are `not whole` by number
            and their names are not used (6 not whole); 139 names equal as a set and exactly 3,000 files (30 full pages) are whole; a changedFiles of 3,000 for a
            diff of 3,100 names is `not whole`. Four mutants: no comparison, number only, first 100 names only, stop at 30 full pages (7 where 6)."""
            trust_claims(TOOL, self.root)   # the unmutated control

        def test_s11_a_tool_that_takes_the_fetched_names_without_comparing_is_caught(self):
            mutant = self.tool_with("return names if len(lines) == total and names == set(pages) else None", "return names")
            with self.assertRaisesRegex(AssertionError, r"^TRUSTED an answer of 138 names"):
                trust_claims(mutant, self.root)

        def test_s11_a_tool_that_compares_the_number_only_is_caught(self):
            mutant = self.tool_with(" and names == set(pages) else None", " else None")
            with self.assertRaisesRegex(AssertionError, r"^TRUSTED-OTHER 139 names"):
                trust_claims(mutant, self.root)

        def test_s11_a_tool_that_compares_only_the_first_100_names_is_caught(self):
            mutant = self.tool_with("names == set(pages) else None", "sorted(names)[:100] == sorted(pages)[:100] else None")
            with self.assertRaisesRegex(AssertionError, r"^TRUSTED-120TH 139 names"):
                trust_claims(mutant, self.root)

        def test_s11_a_tool_that_stops_at_30_full_pages_is_caught(self):
            trust_claims(TOOL, self.root)   # the control first
            mutant = self.tool_with("            return names\n    return names\n", "            return names\n    return None\n")
            with self.assertRaisesRegex(AssertionError, r"^TRUSTED-3000 7 not whole where 6 expected"):
                trust_claims(mutant, self.root)

        def test_s12_a_pull_request_not_whole_is_read_from_its_listed_names(self):
            """KNOWN LIMIT, what the code does today: the overlap of a pull request that is not whole is computed from the names that are listed (a lower
            bound). Both are named `not whole`; only the one whose listed names hold the daemon file is in the overlap report."""
            partial_claims(TOOL, self.root)   # the unmutated control

        def test_s12_a_tool_that_reads_a_pull_request_not_whole_as_having_no_names_is_caught(self):
            mutant = self.tool_with('names = {f.get("path") for f in pr.get("files") or [] if isinstance(f, dict)}', "names = set()")
            with self.assertRaisesRegex(AssertionError, r"^PARTIAL 0 pull requests in the report where 1 "):
                partial_claims(mutant, self.root)

    return TestChangedFilesWhole

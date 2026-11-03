"""cloud_quick: the one quick-mode selection shared by bin/cloud-pr-preflight and bin/cloud-prepush (ask #1299).

A quick run checks only the test files that read a changed file, plus the named repository-wide scans. Each scan is a
(file, selector) pair: the file is run with the selector as its one argument, so one test class or method runs through the
same test code CI runs, never the whole suite (a pod cannot finish bin/test-switchboard). Selectors are not copied tests.
"""
import io
import os
import re
import tokenize

# The one list of named scans. Both commands read this list; there is no second copy.
SCAN_TESTS = [
    ("bin/test-switchboard", "TestGitShim.test_s7_u1_no_file_of_this_change_writes_the_global_git_configuration"),
    ("cloud/launcher/tests/test_stub_cleanup.py", "TestScanFindsTheShape"),
    ("cloud/launcher/tests/test_stub_cleanup.py", "TestEverySite"),
    ("bin/test-cloud-neutral-names", None),
]
# Memory-heavy tests: never run by a quick run (a pod has a hard memory limit); reported "skipped: memory-heavy <path>"
# and counted as could not run (exit 1). Continuous integration runs them. The one list of both commands.
HEAVY = ("cloud/worker/tests/test_root_scanner.py",)
TEST_DIRS = ("cloud/launcher/tests", "cloud/worker/tests")


def is_test(rel):
    base = os.path.basename(rel)
    parts = rel.split("/")
    if rel.startswith("bin/test-"):
        return True
    return base.startswith("test_") and base.endswith(".py") and "tests" in parts[:-1]


def readers(root, tracked, changed, test_ok=is_test):
    """Test files (of tracked) that are changed or whose text names a changed file's basename."""
    names = {os.path.basename(c) for c in changed}
    out = []
    for rel in tracked:
        if not rel or not test_ok(rel) or not os.path.isfile(os.path.join(root, rel)):
            continue
        try:
            with open(os.path.join(root, rel), errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if rel in changed or any(n in text for n in names):
            out.append(rel)
    return sorted(out)


def cannot_run_quick(rel):
    """A whole shell suite under bin/ cannot finish in a quick run, unless a named scan runs it whole (no selector).
    A suite with a selector is run only through its selector, as a scan, never whole as a reader."""
    return rel.startswith("bin/test-") and rel not in {r for r, sel in SCAN_TESTS if sel is None}


def split_readers(reads):
    """(runnable, cannot): the readers a quick run can start, and those it cannot."""
    return [r for r in reads if not cannot_run_quick(r)], [r for r in reads if cannot_run_quick(r)]


def not_run_reason(reads, changed):
    """The ONE no-reader verdict of both commands: None when a quick run can start a reader, else the true reason."""
    names = ", ".join(sorted(changed))
    if not reads:
        return "no check reads " + names
    runnable, cannot = split_readers(reads)
    if runnable:
        return None
    return "no check that can run in quick reads %s (cannot run in quick: %s)" % (names, ", ".join(sorted(cannot)))


def left_to_ci(reads_can_run, reads_cannot):
    """The ONE rule of both commands (ask #1730): when at least one reader can run, the readers that cannot are left to CI
    (not a failure); when none can run the old not-clean decision stays (an empty list here)."""
    return sorted(reads_cannot) if reads_can_run and reads_cannot else []


def left_line(left):
    """The last line of a quick run that left checks to CI."""
    return "left to CI (not run in quick): " + ", ".join(sorted(left))


def cannot_lines(reads):
    """One line per reader a quick run cannot start: never silently skipped."""
    return ["NOT RUN: %s cannot run in quick" % r for r in split_readers(reads)[1]]


def mode_line(n_readers, n_changed):
    """The mode line: the match is on the basename, so a doc change can select many files."""
    return "%d checks that read the %d files (basename match)" % (n_readers, n_changed)


def selector_resolves(root, pair):
    """True when the file exists and holds the class (and method, if named) of the selector."""
    rel, sel = pair
    if sel is None:  # a whole script with no class (bin/test-cloud-neutral-names)
        return os.path.isfile(os.path.join(root, rel))
    text = ""
    # bin/test-switchboard imports its classes from tests/switchboard/test_cases_NN.py and runs a selector over them (#1366):
    # the class is found in the script or in those files. A selector found in none of them still fails.
    files = [os.path.join(root, rel)]
    if rel == "bin/test-switchboard":
        d = os.path.join(root, "tests", "switchboard")
        try:
            files += [os.path.join(d, n) for n in sorted(os.listdir(d)) if re.match(r"test_cases_\d+\.py$", n)]
        except OSError:
            pass
    for path in files:
        try:
            with open(path, errors="replace") as f:
                text += f.read() + "\n"
        except OSError:
            continue
    if not text:
        return False
    parts = sel.split(".")
    if not re.search(r"^class %s\b" % re.escape(parts[0]), text, re.M):
        return False
    return len(parts) == 1 or bool(re.search(r"^\s+def %s\b" % re.escape(parts[1]), text, re.M))


def select_quick(root, tracked, changed, scans=None):
    """List of (file, selector or None): readers of the change first, then the scans not already picked whole."""
    scans = SCAN_TESTS if scans is None else scans
    picked = [(r, None) for r in readers(root, tracked, changed)]
    return add_scans(root, picked, scans)


def add_scans(root, picked, scans):
    have = {r for r, _ in picked}
    picked = list(picked)
    for rel, sel in scans:
        if os.path.isfile(os.path.join(root, rel)) and rel not in have:
            picked.append((rel, sel))
    return picked


# ---- the order and the skip rule of a quick run (ask #1733) ----
SECONDS_LIST = "cloud/launcher/tests/quick_seconds.list"  # one line per test file: "<path> <seconds of a timed pod run>"
DEFAULT_SECONDS = 60  # the order's number for a file the list does not hold; such a file is never skipped


def quick_seconds(root):
    """{path: whole seconds} from the list of the checked repository; an absent or bad line is ignored. It only orders and
    skips: it never decides a verdict."""
    out = {}
    try:
        with open(os.path.join(root, SECONDS_LIST), errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _code_text(path, text):
    """The text of a test without its comments (and, for a Python file, its docstrings)."""
    if path.endswith(".py"):
        try:
            toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                    if t.type not in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT)]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            toks = None
        if toks is not None:
            keep = []
            for i, t in enumerate(toks):
                if t.type == tokenize.COMMENT:
                    continue
                before = toks[i - 1].type if i else tokenize.NEWLINE
                after = toks[i + 1].type if i + 1 < len(toks) else tokenize.NEWLINE
                if t.type == tokenize.STRING and before in (tokenize.NEWLINE, tokenize.ENCODING) and after == tokenize.NEWLINE:
                    continue  # a docstring (a string alone on its statement)
                keep.append(t.string)
            return "\n".join(keep)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def reads_by_content(root, rel, changed):
    """True when a changed file's basename occurs in the test outside its comments and docstrings."""
    names = {os.path.basename(c) for c in changed}
    try:
        with open(os.path.join(root, rel), errors="replace") as f:
            text = _code_text(rel, f.read())
    except OSError:
        return False
    return any(n in text for n in names)


def order_quick(root, picks, changed, reads, seconds):
    """The ONE order of both commands: (1) a reader that reads the change by content, (2) a reader that only names it,
    (3) a named scan; inside each the cheapest recorded seconds first (DEFAULT_SECONDS if unrecorded), then the path."""
    reads = set(reads)

    def key(pair):
        rel, sel = pair
        if rel in reads:
            rank = 0 if reads_by_content(root, rel, changed) else 1
        else:
            rank = 2
        return (rank, seconds.get(rel, DEFAULT_SECONDS), rel, sel or "")
    return sorted(picks, key=key)


def fits(rel, seconds, left):
    """False only for a file with a recorded number of seconds above the time left; an unrecorded file is started."""
    return rel not in seconds or seconds[rel] <= left


def order_line(names):
    return "quick order: " + ", ".join(names)

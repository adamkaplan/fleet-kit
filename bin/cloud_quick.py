"""cloud_quick: the one quick-mode selection shared by bin/cloud-pr-preflight and bin/cloud-prepush (ask #1299).

A quick run checks only the test files that read a changed file, plus the named repository-wide scans. Each scan is a
(file, selector) pair: the file is run with the selector as its one argument, so one test class or method runs through the
same test code CI runs, never the whole suite (a pod cannot finish bin/test-switchboard). Selectors are not copied tests.
"""
import os
import re

# The one list of named scans. Both commands read this list; there is no second copy.
SCAN_TESTS = [
    ("bin/test-switchboard", "TestGitShim.test_s7_u1_no_file_of_this_change_writes_the_global_git_configuration"),
    ("cloud/launcher/tests/test_stub_cleanup.py", "TestScanFindsTheShape"),
]
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


def selector_resolves(root, pair):
    """True when the file exists and holds the class (and method, if named) of the selector."""
    rel, sel = pair
    try:
        with open(os.path.join(root, rel), errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    parts = sel.split(".")
    if not re.search(r"^class %s\b" % re.escape(parts[0]), text, re.M):
        return False
    return len(parts) == 1 or bool(re.search(r"^\s+def %s\b" % re.escape(parts[1]), text, re.M))


def select_quick(root, tracked, changed, scans=None):
    """List of (file, selector or None): readers of the change first, then the scans not already picked whole."""
    scans = SCAN_TESTS if scans is None else scans
    picked = [(r, None) for r in readers(root, tracked, changed)]
    have = {r for r, _ in picked}
    for rel, sel in scans:
        if os.path.isfile(os.path.join(root, rel)) and rel not in have:
            picked.append((rel, sel))
    return picked

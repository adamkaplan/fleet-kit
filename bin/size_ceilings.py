#!/usr/bin/env python3
"""The size ceilings of docs/architecture.md, checked on the merge result (#1605).

A `size` row of the Known violations table is a ceiling for one file. Two pull requests that each fit under it alone can together go over it, because
the check of each ran on a merge ref built at the start of its run. This module is the ONE reading of those rows and of a file's line count:
`tests/switchboard/test_architecture.py` (the test of the document), the test workflow's step and the merge script's gate all call it.

    check_merge(root, tip_ref, head_sha) -> [problem line, ...]     (empty: pass)
    python3 bin/size_ceilings.py check --tip <ref> --head <sha>      (exit 0 pass; exit 1 with the problem lines on stdout)

The merge result is made here, from the head sha and the CURRENT tip (`git merge-tree --write-tree`: it writes tree objects only: no branch, no tag,
no working tree, no comment). The ceilings are read from the document AT THE MERGE RESULT. A file fails only when it is over its ceiling after the
merge AND the pull request makes it longer; a file that is over and not made longer by the pull request passes (it is not made worse).
Fails closed: a merge that conflicts cannot be sized (one line naming the conflict), and an unreadable size row is a problem line naming the row.
Known limit: a file with no `size` row is held only by the default limits in the architecture test, on the merge ref; this check does not see it.
Standard library only.
"""
import argparse
import io
import re
import subprocess
import sys

DOCUMENT = "docs/architecture.md"
SECTION = "Known violations"


def count_lines(data):
    """The number of lines of the bytes `data`, counted as a text file is read (utf-8, bad bytes replaced, any newline style)."""
    with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def line_count(path):
    """The number of lines of the file at `path` (the one count of the architecture test and of this check)."""
    with open(path, "rb") as handle:
        return count_lines(handle.read())


def parse_count(cell):
    """The number of a count or ceiling cell (`27,500` is 27500). Raises ValueError when it is not a number."""
    return int(cell.replace(",", ""))


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _section(text, heading):
    match = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(heading), text, re.S | re.M)
    return match.group(1) if match else ""


def size_rows(text):
    """(ceilings, problems) of the document `text`: ceilings is {file path from the repository root: ceiling} for every `size` row of the Known
    violations table, problems is one line per row that cannot be read (a ceiling that is not a number, a missing cell)."""
    ceilings, problems = {}, []
    for line in _section(text, SECTION).splitlines():
        if not line.startswith("| `"):
            continue
        cells = _cells(line)
        source = cells[0].strip("`")
        if len(cells) < 4 or not cells[2]:
            problems.append("the row `%s` of the Known violations table of %s has no kind or no count: cannot read its ceiling" % (source, DOCUMENT))
            continue
        if cells[2] != "size":
            continue
        try:
            ceiling = parse_count(cells[3])
        except ValueError:
            ceiling = None
        if ceiling is None or ceiling < 1:
            problems.append("the size row `%s` of the Known violations table of %s has a ceiling that is not a number (`%s`): cannot read it" % (source, DOCUMENT, cells[3]))
            continue
        ceilings[source if source.startswith(("bin/", "tests/")) else "bin/" + source] = ceiling
    return ceilings, problems


def _git(root, *argv, check=False):
    return subprocess.run(["git", *argv], cwd=str(root), capture_output=True, check=check)


def _blob(root, tree, path):
    """The bytes of `path` in `tree`, or None when the tree holds no such file."""
    done = _git(root, "cat-file", "blob", "%s:%s" % (tree, path))
    return done.stdout if done.returncode == 0 else None


def _commit(root, ref):
    done = _git(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % ref)
    return done.stdout.decode().strip() if done.returncode == 0 else None


def available(root, tip_ref, head_sha):
    """True when both the tip and the head are commits of the repository at `root` (the merge script runs this check only where they are)."""
    return bool(_commit(root, tip_ref) and _commit(root, head_sha))


def check_merge(root, tip_ref, head_sha):
    """The problem lines for merging `head_sha` into `tip_ref` in the repository at `root`; an empty list means the merge keeps every ceiling."""
    tip, head = _commit(root, tip_ref), _commit(root, head_sha)
    if not tip or not head:
        return ["cannot check the size ceilings: %s is not a commit of this repository" % (tip_ref if not tip else head_sha)]
    made = _git(root, "merge-tree", "--write-tree", "--name-only", tip, head)
    out = made.stdout.decode("utf-8", "replace").splitlines()
    if made.returncode == 1 and out:
        files = []
        for name in out[1:]:
            if not name:
                break
            files.append(name)
        return ["the merge of %s with %s conflicts in %s: it cannot be sized, resolve the conflict first" % (head[:8], tip_ref, ", ".join(files) or "files git did not name")]
    if made.returncode != 0 or not out:
        return ["cannot check the size ceilings: git could not make the merge of %s with %s" % (head[:8], tip_ref)]
    tree = out[0].strip()
    text = _blob(root, tree, DOCUMENT)
    ceilings, problems = size_rows((text or b"").decode("utf-8", "replace"))
    for path, ceiling in sorted(ceilings.items()):
        merged = _blob(root, tree, path)
        if merged is None:
            continue
        after = count_lines(merged)
        before = _blob(root, tip, path)
        added = after - (count_lines(before) if before is not None else 0)
        if after > ceiling and added > 0:
            problems.append("%s is %s lines after merging with %s, over its ceiling of %s; this pull request adds %s" % (
                path, format(after, ","), tip_ref, format(ceiling, ","), format(added, ",")))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="check the size ceilings of docs/architecture.md on the merge of a pull request head with the current tip")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--tip", required=True, help="the base branch's current tip (a ref or sha)")
    check.add_argument("--head", required=True, help="the pull request head sha")
    check.add_argument("--root", default=".", help="the repository (default: the current directory)")
    args = parser.parse_args(argv)
    lines = check_merge(args.root, args.tip, args.head)
    for line in lines:
        print(line)
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())

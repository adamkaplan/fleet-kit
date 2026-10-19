"""Which open pull requests change the same files (ask #818): read-only information, never a block.

The pure half: `parse_listing` turns the one bounded `gh pr list --state open --json number,files,changedFiles` answer into {number: [paths]} (a pull
request whose list is missing, truncated or over FILES_CAP is simply absent: it is never guessed at), `shares_line` is the extra line of `gates`, and
`merge_order` is the hint among held pull requests. The read itself is in bin/fleet-switchboard (CLI only; the daemon never reads for this).
Python 3.9 standard library only.
"""

LIST_CAP = 50    # the most open pull requests read, in one call
FILES_CAP = 100  # the most files of one pull request used; a pull request with more changed files is left out
SHOW_FILES = 3   # files named in a `shares` line
SHOW_PRS = 5     # pull requests named in a `shares` line or a merge order


def parse_listing(raw):
    """{number: [paths]} of the pull requests whose whole file list was read. Anything else is left out, never guessed."""
    out = {}
    if not isinstance(raw, list):
        return out
    for item in raw[:LIST_CAP]:
        if not isinstance(item, dict) or isinstance(item.get("number"), bool) or not isinstance(item.get("number"), int):
            continue
        files, total = item.get("files"), item.get("changedFiles")
        if not isinstance(files, list) or isinstance(total, bool) or not isinstance(total, int):
            continue
        if total > FILES_CAP or len(files) != total:
            continue   # truncated or over the cap: not the whole list
        paths = [f.get("path") for f in files if isinstance(f, dict)]
        if len(paths) != total or not all(isinstance(p, str) and p for p in paths):
            continue
        out[item["number"]] = sorted(set(paths))
    return out


def shared(listing, number, other):
    return sorted(set(listing.get(number) or ()) & set(listing.get(other) or ()))


def shares_line(listing, number):
    """`shares <file>, <file> with #N, #M`, or None. Files: the first SHOW_FILES (sorted) changed by this pull request AND another listed one;
    pull requests: the first SHOW_PRS by number. None when this pull request's own list was not read or nothing overlaps."""
    if not isinstance(listing, dict) or number not in listing:
        return None
    others = sorted(n for n in listing if n != number and shared(listing, number, n))
    if not others:
        return None
    files = sorted({p for n in others for p in shared(listing, number, n)})
    return "shares %s with %s" % (", ".join(files[:SHOW_FILES]), ", ".join("#%d" % n for n in others[:SHOW_PRS]))


def merge_order(listing, waited):
    """Hints among held pull requests that share files. `waited` is [(number, since)] of the held ones (since: epoch seconds, smaller waited longer).
    One hint per group of held pull requests linked by shared files, the one that has waited longest first: `merge order: #748, then #736 (shared files)`."""
    held = {n: s for n, s in waited if n in (listing or {})}
    seen, hints = set(), []
    for start in sorted(held):
        if start in seen:
            continue
        group, todo = set(), [start]
        while todo:
            n = todo.pop()
            if n in group:
                continue
            group.add(n)
            todo.extend(m for m in held if m not in group and shared(listing, n, m))
        seen |= group
        if len(group) > 1:
            ordered = sorted(group, key=lambda n: (held[n], n))[:SHOW_PRS]
            hints.append("merge order: %s (shared files)" % ", then ".join("#%d" % n for n in ordered))
    return hints

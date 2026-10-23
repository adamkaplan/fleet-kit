"""An owner's clear CARRIES over a pure merge of main (ask #510).

An owner clear is bound to one head. When main moves, a pull request that conflicts or must be current merges main into its branch: a new head, and the clear no longer applies
though the pull request's OWN change is exactly what the owner cleared. This module decides, from facts read from GitHub, whether the clear may carry to the new head. It is pure
logic over a `get(path)` callable (the switchboard hands it a `gh api` GET with the repo's own account): it writes nothing and holds no credential.

The clear stores, next to the head, a FINGERPRINT of the pull request's own change: its changed files with their blob ids and patch text, against the merge base of that head. At a
new head the clear carries only if ALL hold, each read IN FULL (anything unreadable, truncated or omitted VOIDS the clear: fail closed):

* the cleared head is an ancestor of the new head (a merge of main, never a rewritten history);
* every step from the new head back to the cleared head along first parents is a merge commit of two parents (a commit of the pull request's own is never carried; the main-side commits a merge brings in are not the pull request's);
* the second parent of each of those merges is on the base branch (a side branch of the author's is never carried: a mode change, which no file list shows, could ride on it);
* in every one of those merges no file differs from BOTH parents (a conflict resolved by hand, or a file both sides changed, voids the clear);
* the fingerprint of the new head's own change (and the base branch it is against), against ITS merge base, is identical to the ORIGINAL one stored with the clear (never to an earlier carried head, so carries cannot drift);
* the head did not move while this was read.

A SECOND route (ask #815) carries the clear over a merge whose conflict was resolved by hand: the pull request's added and removed LINES (a lines-only fingerprint, stored with the clear) are
identical, no merge and no part of the pull request's own change touches a protected path (the clear's own code and the impact matrix: the caller's table), and an independent reviewer's
confirmation is recorded for exactly the new head (the caller's `confirm`). See `decide`.

The same function computes the fingerprint at the clear and at the carry (`own_change_fingerprint`). Python 3.9 standard library only.
"""
import hashlib
import json

PAGE = 100
FILE_PAGES = 30                # the pull request files API stops at 3000 files
COMPARE_FILE_PAGES = 3         # compare and commit file lists are cut at 300 files by GitHub
COMMIT_PAGES = 6


class CarryVoid(Exception):
    """The clear does not carry. `str(err)` is the reason, in words: what could not be read, or what changed."""


def _get_list(get, path, pages, what, key=None):
    """Every item of a paginated answer, or CarryVoid. `key` names the list inside an object answer. A list that is still full after `pages` pages is cut: void."""
    items = []
    for page in range(1, pages + 1):
        try:
            answer = get("%s%spage=%d" % (path, "&" if "?" in path else "?", page))
        except CarryVoid:
            raise
        except Exception as err:
            raise CarryVoid("%s could not be read (%s)" % (what, str(err)[:80])) from None
        batch = answer.get(key) if key and isinstance(answer, dict) else answer
        if not isinstance(batch, list):
            raise CarryVoid("%s was not a list" % what)
        items.extend(batch)
        if len(batch) < PAGE:
            return items
    raise CarryVoid("%s is longer than %d items: too long to read in full" % (what, pages * PAGE))


def _pr_files():
    import importlib.util
    import os
    import sys
    if "pr_files" in sys.modules:
        return sys.modules["pr_files"]
    spec = importlib.util.spec_from_file_location("pr_files", os.path.join(os.path.dirname(os.path.realpath(__file__)), "pr_files.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_files"] = module
    spec.loader.exec_module(module)
    return module


def _object(get, path, what):
    try:
        answer = get(path)
    except Exception as err:
        raise CarryVoid("%s could not be read (%s)" % (what, str(err)[:80])) from None
    if not isinstance(answer, dict):
        raise CarryVoid("%s was not an object" % what)
    return answer


def _sha(value):
    return value if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def pull_head(get, repo, number):
    """(head sha, the pull request's answer) from a fresh read."""
    answer = _object(get, "repos/%s/pulls/%d" % (repo, number), "the pull request")
    head = _sha((answer.get("head") or {}).get("sha") if isinstance(answer.get("head"), dict) else None)
    if head is None:
        raise CarryVoid("the pull request's head could not be read")
    return head, answer


def base_ref_of(pull):
    ref = (pull.get("base") or {}).get("ref") if isinstance(pull.get("base"), dict) else None
    if not isinstance(ref, str) or not ref or len(ref) > 200:
        raise CarryVoid("the pull request's base branch could not be read")
    return ref


def own_change(get, repo, number):
    """([{"path", "status", "previous", "sha", "patch"}] sorted by path, head, base branch): the files of the pull request's own change against the merge base of its CURRENT
    head, read in full. Void when the list is cut, when a file's patch is omitted (a binary file, a mode-only change, or a change too large for GitHub) unless it is a pure rename or
    copy, whose blob id pins its content, and when the head moved while the pages were read (the head is read before AND after)."""
    head, pull = pull_head(get, repo, number)
    base = base_ref_of(pull)
    total = pull.get("changed_files")
    if not isinstance(total, int) or isinstance(total, bool):
        raise CarryVoid("the changed-file list is cut: GitHub gave no count of files")
    files, why = _pr_files().read(get, repo, number, total)   # the ONE reader of the list (#1098)
    if files is None:
        raise CarryVoid("the changed-file list is cut: %s" % why)
    out = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise CarryVoid("a changed file could not be read")
        status, patch, sha = item.get("status"), item.get("patch"), _sha(item.get("sha"))
        if sha is None:
            raise CarryVoid("the blob of %s could not be read" % item["filename"][:80])
        pure_move = status in ("renamed", "copied") and not item.get("changes")
        if patch is None and not pure_move:
            raise CarryVoid("the patch of %s is omitted by GitHub (a binary file or too large to read in full)" % item["filename"][:80])
        previous = item.get("previous_filename") if isinstance(item.get("previous_filename"), str) else None
        out.append({"path": item["filename"], "status": str(status), "previous": previous, "sha": sha, "patch": patch if isinstance(patch, str) else ""})
    after, after_pull = pull_head(get, repo, number)
    if after != head or base_ref_of(after_pull) != base:
        raise CarryVoid("the head or the base branch moved while the changed files were being read")
    return sorted(out, key=lambda f: (f["path"], f["status"])), head, base


def changed_lines(patch):
    """The lines a patch adds and removes, in order, as ["+text", "-text"]: context lines, hunk headers and so line numbers are left out (main moves them). A `\\ No newline` marker is
    kept only when it follows an added or removed line (after a context line it says something about the surroundings, not about the change)."""
    out, last = [], ""
    for line in patch.split("\n"):
        if line.startswith(("+", "-")):
            out.append(line)
            last = line[0]
        elif line.startswith("\\"):
            if last:
                out.append(line)
        else:
            last = ""
    return out


def fingerprint_of(files, base, lines=False):
    """`sha256:<hex>` of the canonical form of an own change: the base branch it is against and, in path order, the path, status, previous path, blob id and patch of every file. With
    `lines` (the conflict-resolving carry, ask #815) the form holds, for every file, only the LINES the pull request adds and removes: no blob id and no context (main moves them, so a
    merge that resolved a conflict changes both), except for a pure rename or copy, which has no lines and keeps its blob id. The ONE computation, whichever form."""
    if lines:
        rows = [[f["path"], f["status"], f["previous"], changed_lines(f["patch"]) if f["patch"] else f["sha"]] for f in files]
        canonical = json.dumps({"base": base, "lines": rows}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    else:
        canonical = json.dumps({"base": base, "files": [[f["path"], f["status"], f["previous"], f["sha"], f["patch"]] for f in files]}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def own_change_fingerprint(get, repo, number, lines=False):
    """(fingerprint, head, file count): the ONE function the clear and the carry both use (`lines`: the lines-only form). CarryVoid when the change cannot be read in full."""
    files, head, base = own_change(get, repo, number)
    return fingerprint_of(files, base, lines), head, len(files)


def commits_between(get, repo, cleared, new):
    """The commits from `cleared` to `new` as [{"sha", "parents"}] (the main-side commits a merge brings in included). Void unless `cleared` is an ancestor of `new` and every commit is listed."""
    path = "repos/%s/compare/%s...%s?per_page=%d" % (repo, cleared, new, PAGE)
    first = _object(get, "%s&page=1" % path, "the comparison of the two heads")
    status, total = first.get("status"), first.get("total_commits")
    if status not in ("ahead", "identical"):
        raise CarryVoid("the new head does not contain the cleared head (GitHub says %s): a rewritten history is never carried" % (status if isinstance(status, str) else "nothing"))
    if not isinstance(total, int) or isinstance(total, bool):
        raise CarryVoid("the number of commits between the heads could not be read")
    commits = list(first.get("commits") or [])
    page = 1
    while len(commits) < total and page < COMMIT_PAGES:
        page += 1
        more = _object(get, "%s&page=%d" % (path, page), "the commits between the heads")
        commits.extend(more.get("commits") or [])
    if len(commits) != total:
        raise CarryVoid("GitHub lists %d of %d commits between the heads" % (len(commits), total))
    out = []
    for commit in commits:
        sha = _sha(commit.get("sha")) if isinstance(commit, dict) else None
        parents = [_sha(p.get("sha")) for p in (commit.get("parents") or []) if isinstance(p, dict)] if isinstance(commit, dict) else []
        if sha is None or any(p is None for p in parents):
            raise CarryVoid("a commit between the heads could not be read")
        out.append({"sha": sha, "parents": parents})
    return out


def _files_of(get, path, what, pages):
    items = _get_list(get, path, pages, what, "files")
    out = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str) or _sha(item.get("sha")) is None and item.get("status") != "removed":
            raise CarryVoid("a file of %s could not be read" % what)
        out[item["filename"]] = item.get("status")
    return out


def merge_files(get, repo, merge):
    """({path: status} of the merge against its first parent, the same against its second parent), each read in full."""
    second = merge["parents"][1]
    vs_first = _files_of(get, "repos/%s/commits/%s?per_page=%d" % (repo, merge["sha"], PAGE), "the files of merge %s" % merge["sha"][:8], COMPARE_FILE_PAGES)
    vs_second = _files_of(get, "repos/%s/compare/%s...%s?per_page=%d" % (repo, second, merge["sha"], PAGE), "the files of merge %s against main" % merge["sha"][:8], COMPARE_FILE_PAGES)
    return vs_first, vs_second


def _norm(path):
    if not isinstance(path, str):
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./") or path.startswith("/"):
        path = path[2:] if path.startswith("./") else path[1:]
    return path.casefold()


def check_protected(get, repo, merges, files, protected):
    """Void when a merge of the chain touches (against either parent) a path of `protected`, or when the pull request's own change does (the clear's own code and the impact matrix are
    never carried by the conflict-resolving route). `protected` is the caller's table; compared case-insensitively. An empty table protects nothing, so the caller must give one."""
    table = {_norm(p) for p in protected or ()}
    if not table:
        raise CarryVoid("no list of protected paths was given: the conflict-resolving carry is refused")
    for f in files:
        for path in (f["path"], f["previous"]):
            if path and _norm(path) in table:
                raise CarryVoid("the pull request's own change touches %s, the clear's own code or the impact matrix: never carried" % path[:80])
    for merge in merges:
        vs_first, vs_second = merge_files(get, repo, merge)
        for path in sorted(set(vs_first) | set(vs_second)):
            if _norm(path) in table:
                raise CarryVoid("merge %s touches %s, the clear's own code or the impact matrix: never carried" % (merge["sha"][:8], path[:80]))


def check_merge(get, repo, merge):
    """Void unless this merge commit changed no file relative to BOTH of its parents: a file that differs from both is a conflict resolved by hand or a file both sides touched."""
    if len(merge["parents"]) != 2:
        raise CarryVoid("%s is not a merge of two parents: a commit of the pull request's own is never carried" % merge["sha"][:8])
    vs_first, vs_second = merge_files(get, repo, merge)
    both = sorted(set(vs_first) & set(vs_second))
    if both:
        raise CarryVoid("merge %s changed %s relative to both parents: a conflict was resolved or both sides changed it" % (merge["sha"][:8], ", ".join(both[:3])))


def check_main_side(get, repo, base, merge):
    """Void unless the second parent of the merge is on the base branch (an ancestor of its tip): a merge of main brings in only what main already has, never a side branch the pull
    request's author made (a mode change, which no file list shows, could ride on one)."""
    second = merge["parents"][1]
    answer = _object(get, "repos/%s/compare/%s...%s" % (repo, base, second), "the comparison of main with a merged parent")
    if answer.get("status") not in ("behind", "identical"):
        raise CarryVoid("merge %s brings in %s, which is not on %s: only a merge of the base branch is carried" % (merge["sha"][:8], second[:8], base))


def merge_chain(commits, cleared, new):
    """The merge commits from `new` back to `cleared` along FIRST parents, newest first. `commits` is everything between the heads (the main-side commits a merge brings in are in
    the list too, and are not the pull request's own). Void when a step on that line is not a merge of two parents (a commit of the pull request's own) or when the line does
    not lead to `cleared` (the cleared head is not on the pull request's own history)."""
    parents = {c["sha"]: c["parents"] for c in commits}
    chain, current = [], new
    while current != cleared:
        found = parents.get(current)
        if found is None:
            raise CarryVoid("the cleared head is not on the new head's own history: a rewritten history or a merge the other way round is never carried")
        if len(found) != 2:
            raise CarryVoid("%s is a commit of the pull request's own, not a merge of main" % current[:8])
        chain.append({"sha": current, "parents": found})
        current = found[0]
        if len(chain) > len(commits):
            raise CarryVoid("the history between the heads could not be followed")
    return chain


def decide(get, repo, number, cleared_head, cleared_fingerprint, new_head, lines_fingerprint=None, confirm=None, protected=None):
    """{"carried": bool, "reason": str|None, "fingerprint", "from", "to", "route", "lines_fingerprint", "confirmation"}: may the clear of `cleared_head` (with `cleared_fingerprint`) carry to
    `new_head`? Never raises. Two routes, the first that applies:

    * "pure": every merge changed no file relative to both parents and the full fingerprint is the cleared one (ask #510, unchanged);
    * "lines" (ask #815), only when the clear stored a `lines_fingerprint` and the caller gives `confirm` and `protected`: a conflict resolved by hand is allowed, but no merge nor the pull
      request's own change touches a `protected` path, the lines-only fingerprint equals the stored one, and `confirm(new_head)` names the independent reviewer's recorded
      confirmation for exactly this head (a string reference; None or CarryVoid refuses). The reason of a refused carry is the pure route's when the second route is not offered, else the second's."""
    verdict = {"carried": False, "reason": None, "fingerprint": None, "from": cleared_head, "to": new_head, "route": None, "lines_fingerprint": None, "confirmation": None, "waiting": False}
    try:
        if not (isinstance(cleared_fingerprint, str) and cleared_fingerprint.startswith("sha256:")):
            raise CarryVoid("no fingerprint was recorded with the clear")
        if cleared_head == new_head:
            raise CarryVoid("the head did not move")
        now_head, pull = pull_head(get, repo, number)
        if now_head != new_head:
            raise CarryVoid("the head moved while it was being read")
        base = base_ref_of(pull)
        commits = commits_between(get, repo, cleared_head, new_head)
        if not commits:
            raise CarryVoid("no commit lies between the two heads")
        merges = merge_chain(commits, cleared_head, new_head)
        for merge in merges:
            check_main_side(get, repo, base, merge)
        try:
            for merge in merges:
                check_merge(get, repo, merge)
            fingerprint, head, _ = own_change_fingerprint(get, repo, number)
            verdict["fingerprint"] = fingerprint
            if head != new_head:
                raise CarryVoid("the head moved while it was being read")
            if fingerprint != cleared_fingerprint:
                raise CarryVoid("the pull request's own change is not the one the owner cleared")
            verdict["route"] = "pure"
        except CarryVoid:
            if not (isinstance(lines_fingerprint, str) and lines_fingerprint.startswith("sha256:") and callable(confirm)):
                raise
            files, head, files_base = own_change(get, repo, number)
            if head != new_head:
                raise CarryVoid("the head moved while it was being read")
            check_protected(get, repo, merges, files, protected)
            now_lines = fingerprint_of(files, files_base, True)
            verdict["lines_fingerprint"] = now_lines
            if now_lines != lines_fingerprint:
                raise CarryVoid("a line the pull request adds or removes differs from the one the owner cleared")
            try:
                reference = confirm(new_head)
                if not isinstance(reference, str) or not reference:
                    raise CarryVoid("no confirmation at this head")
            except CarryVoid:
                verdict["waiting"] = True   # everything else holds: only a confirmation is missing, and one may still arrive at this head
                raise
            verdict["confirmation"] = reference
            verdict["fingerprint"] = cleared_fingerprint
            verdict["route"] = "lines"
        verdict["carried"] = True
    except CarryVoid as err:
        verdict["reason"] = str(err)
    except Exception as err:   # any other trouble is a void clear, never a carried one
        verdict["reason"] = "the carry could not be decided (%s)" % type(err).__name__
    return verdict

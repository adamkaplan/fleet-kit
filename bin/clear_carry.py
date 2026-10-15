"""An owner's clear CARRIES over a pure merge of main (ask #510).

An owner clear is bound to one head. When main moves, a pull request that conflicts or must be current merges main into its branch: a new head, and the clear no longer applies
though the pull request's OWN change is exactly what the owner cleared. This module decides, from facts read from GitHub, whether the clear may carry to the new head. It is pure
logic over a `get(path)` callable (the switchboard hands it a `gh api` GET with the repo's own account): it writes nothing and holds no credential.

The clear stores, next to the head, a FINGERPRINT of the pull request's own change: its changed files with their blob ids and patch text, against the merge base of that head. At a
new head the clear carries only if ALL hold, each read IN FULL (anything unreadable, truncated or omitted VOIDS the clear: fail closed):

* the cleared head is an ancestor of the new head (a merge of main, never a rewritten history);
* every step from the new head back to the cleared head along first parents is a merge commit of two parents (a commit of the pull request's own is never carried; the main-side commits a merge brings in are not the pull request's);
* in every one of those merges no file differs from BOTH parents (a conflict resolved by hand, or a file both sides changed, voids the clear);
* the fingerprint of the new head's own change, against ITS merge base, is identical to the ORIGINAL one stored with the clear (never to an earlier carried head, so carries cannot drift);
* the head did not move while this was read.

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
    """The pull request's head sha now, from a fresh read."""
    answer = _object(get, "repos/%s/pulls/%d" % (repo, number), "the pull request")
    head = _sha((answer.get("head") or {}).get("sha") if isinstance(answer.get("head"), dict) else None)
    if head is None:
        raise CarryVoid("the pull request's head could not be read")
    return head, answer


def own_change(get, repo, number):
    """[{"path", "status", "previous", "sha", "patch"}] sorted by path: the files of the pull request's own change against the merge base of its CURRENT head, read in full.
    Void when the list is cut, when a file's patch is omitted (a binary file or a change too large for GitHub) unless it is a pure rename or copy, whose blob id pins its content."""
    head, pull = pull_head(get, repo, number)
    files = _get_list(get, "repos/%s/pulls/%d/files?per_page=%d" % (repo, number, PAGE), FILE_PAGES, "the changed files", None)
    total = pull.get("changed_files")
    if not isinstance(total, int) or isinstance(total, bool) or total != len(files):
        raise CarryVoid("the changed-file list is cut: GitHub lists %d of %s files" % (len(files), total))
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
    return sorted(out, key=lambda f: (f["path"], f["status"])), head


def fingerprint_of(files):
    """`sha256:<hex>` of the canonical form of an own change: path, status, previous path, blob id and patch of every file, in path order."""
    canonical = json.dumps([[f["path"], f["status"], f["previous"], f["sha"], f["patch"]] for f in files], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def own_change_fingerprint(get, repo, number):
    """(fingerprint, head, file count): the ONE function the clear and the carry both use. CarryVoid when the change cannot be read in full."""
    files, head = own_change(get, repo, number)
    return fingerprint_of(files), head, len(files)


def commits_between(get, repo, cleared, new):
    """The commits from `cleared` to `new` as [{"sha", "parents"}], and True when `cleared` is an ancestor of `new`. Void unless every commit is listed."""
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


def check_merge(get, repo, merge):
    """Void unless this merge commit changed no file relative to BOTH of its parents: a file that differs from both is a conflict resolved by hand or a file both sides touched."""
    if len(merge["parents"]) != 2:
        raise CarryVoid("%s is not a merge of two parents: a commit of the pull request's own is never carried" % merge["sha"][:8])
    first, second = merge["parents"]
    vs_first = _files_of(get, "repos/%s/commits/%s?per_page=%d" % (repo, merge["sha"], PAGE), "the files of merge %s" % merge["sha"][:8], COMPARE_FILE_PAGES)
    vs_second = _files_of(get, "repos/%s/compare/%s...%s?per_page=%d" % (repo, second, merge["sha"], PAGE), "the files of merge %s against main" % merge["sha"][:8], COMPARE_FILE_PAGES)
    both = sorted(set(vs_first) & set(vs_second))
    if both:
        raise CarryVoid("merge %s changed %s relative to both parents: a conflict was resolved or both sides changed it" % (merge["sha"][:8], ", ".join(both[:3])))


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


def decide(get, repo, number, cleared_head, cleared_fingerprint, new_head):
    """{"carried": bool, "reason": str|None, "fingerprint", "from", "to"}: may the clear of `cleared_head` (with `cleared_fingerprint`) carry to `new_head`? Never raises."""
    verdict = {"carried": False, "reason": None, "fingerprint": None, "from": cleared_head, "to": new_head}
    try:
        if not (isinstance(cleared_fingerprint, str) and cleared_fingerprint.startswith("sha256:")):
            raise CarryVoid("no fingerprint was recorded with the clear")
        if cleared_head == new_head:
            raise CarryVoid("the head did not move")
        now_head, _ = pull_head(get, repo, number)
        if now_head != new_head:
            raise CarryVoid("the head moved while it was being read")
        commits = commits_between(get, repo, cleared_head, new_head)
        if not commits:
            raise CarryVoid("no commit lies between the two heads")
        for merge in merge_chain(commits, cleared_head, new_head):
            check_merge(get, repo, merge)
        fingerprint, head, _ = own_change_fingerprint(get, repo, number)
        verdict["fingerprint"] = fingerprint
        if head != new_head:
            raise CarryVoid("the head moved while it was being read")
        if fingerprint != cleared_fingerprint:
            raise CarryVoid("the pull request's own change is not the one the owner cleared")
        verdict["carried"] = True
    except CarryVoid as err:
        verdict["reason"] = str(err)
    except Exception as err:   # any other trouble is a void clear, never a carried one
        verdict["reason"] = "the carry could not be decided (%s)" % type(err).__name__
    return verdict

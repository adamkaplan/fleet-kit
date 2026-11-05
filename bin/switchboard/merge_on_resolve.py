# ---------------------------------------------------------------- merge on resolve (#2204): an approved pull request whose only change is a clean tip merge merges at once
#
# A lane marks a pull request with a label ending `:merge-on-resolve` (the opt-in; docs/switchboard.md). When the pull request holds a recorded strict read
# (`Review of <40 hex>: APPROVE`, posted by the fleet's signed account) and every commit after that read's head is a two-parent merge, first-parent chained from the read head, whose second parent is an
# ancestor of the integration tip and whose tree equals `git merge-tree --write-tree <p1> <p2>` (a merge git made, no hand resolution), then, with the tip merging
# clean, `merge-drops` clean and no hold label, the step merges through the lane's own gates (`merge_gate.pass_and_merge`: squash, --match-head-commit) and tells the
# lane. Every other case is told to the lane ONCE per head, in one line, with its reason. The part reads no name of the entry: the entry hands it `ctx` (docs/architecture.md).

MOR_MARK_SUFFIX = ":merge-on-resolve"


def mor_marked(labels):
    return any(str(label).lower().endswith(MOR_MARK_SUFFIX) for label in labels or [])


def mor_read(comments, read):
    """(sha, True) of the LATEST recorded read when it is an APPROVE, (sha, False) when it asks for changes, (None, False) when there is none. `read(comment)` is the daemon's
    `review_read`: (sha, verdict) only for a strict read by the fleet's signed account; any other comment is None and is never looked at."""
    found = (None, False)
    for comment in comments if isinstance(comments, list) else []:
        got = read(comment)
        if got:
            found = (got[0], got[1] == "APPROVE")
    return found


def mor_git(runner, git_dir, argv):
    import subprocess
    done = runner(["git", "-C", str(git_dir)] + argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout or "").strip()


def mor_structure(runner, git_dir, approved, head, tip):
    """(True, "") when every commit after `approved` up to `head` is a git-made merge of the tip; else (False, the one-line reason). Never raises on a git answer."""
    code, full = mor_git(runner, git_dir, ["rev-parse", "--verify", "-q", approved + "^{commit}"])
    if code != 0:
        return False, "the read's head %s is not in the clone" % approved[:8]
    if mor_git(runner, git_dir, ["merge-base", "--is-ancestor", full, head])[0] != 0:
        return False, "the read's head %s is not an ancestor of the head" % full[:8]
    code, listed = mor_git(runner, git_dir, ["rev-list", "--first-parent", "--reverse", "%s..%s" % (full, head)])
    chain = listed.split()
    if code != 0 or not chain:
        return False, "no commit after the read's head %s" % full[:8]
    before = full
    for commit in chain:
        code, line = mor_git(runner, git_dir, ["rev-list", "--parents", "-n", "1", commit])
        parents = line.split()[1:]
        if code != 0 or len(parents) != 2:
            return False, "%s is a commit other than a tip merge" % commit[:8]
        if parents[0] != before:
            return False, "%s does not continue from the read's head by its first parent" % commit[:8]
        if mor_git(runner, git_dir, ["merge-base", "--is-ancestor", parents[1], tip])[0] != 0:
            return False, "%s merges %s, which is not on the integration branch" % (commit[:8], parents[1][:8])
        code, made = mor_git(runner, git_dir, ["merge-tree", "--write-tree", parents[0], parents[1]])
        if code != 0:
            return False, "%s is a hand resolution (git merge-tree of its parents conflicts)" % commit[:8]
        code, tree = mor_git(runner, git_dir, ["rev-parse", commit + "^{tree}"])
        if made.splitlines()[0] != tree:   # the commit's tree is not what git makes of its parents
            return False, "%s is a hand resolution (its tree differs from git merge-tree of its parents)" % commit[:8]
        before = commit
    return True, ""


def mor_run(ctx, repo, number, view):
    """One pull request. `view` is `gh pr view --json state,labels,comments,headRefOid,baseRefName`. Returns (outcome, reason) and, for a reason, calls ctx["tell"] once per head.
    ctx: runner, git_dir, prepare(repo, number, base) -> (tip ref, head ref), is_held(labels), drops(repo, number), union(repo, number), merge(repo, number, checked head) -> (code, lines), tell(repo, number, key, text)."""
    head, ref = view.get("headRefOid"), "%s#%d" % (repo, number)
    labels = [l.get("name") if isinstance(l, dict) else l for l in view.get("labels") or []]
    approved, ok = mor_read(view.get("comments"), ctx["read"])
    if view.get("state") != "OPEN" or not approved or not ok or not isinstance(head, str) or head.startswith(approved):
        return "skip", ""

    def refuse(reason, tell=True):
        if tell:
            ctx["tell"](repo, number, "%s:%s" % (ref, head), "merge-on-resolve: %s was not merged at %s: %s" % (ref, head[:8], reason))
        return "refused", reason
    tip, head_ref = ctx["prepare"](repo, number, view.get("baseRefName"))
    if tip is None:
        return refuse("the clone could not be read", tell=False)
    good, why = mor_structure(ctx["runner"], ctx["git_dir"], approved, head_ref, tip)
    if not mor_marked(labels):
        return refuse("the pull request is not marked %s" % MOR_MARK_SUFFIX, tell=good) if good else ("skip", why)
    if not good:
        return refuse(why)
    if ctx["is_held"](labels):
        return refuse("a hold label is present")
    if mor_git(ctx["runner"], ctx["git_dir"], ["merge-tree", "--write-tree", head_ref, tip])[0] != 0:
        return refuse("the head does not merge cleanly with the current tip")
    for name in ("drops", "union"):
        said = ctx[name](repo, number)
        if said:
            return refuse(said.splitlines()[0][:200])
    code, lines = ctx["merge"](repo, number, head)
    if code != 0:
        return refuse("the merge gates refused: %s" % (lines[-1] if lines else "no message")[:200])
    ctx["tell"](repo, number, "%s:%s:merged" % (ref, head), "merge-on-resolve: %s was merged at %s in the lane's name: only git-made merges of the tip followed the read of %s" % (ref, head[:8], approved[:8]))
    return "merged", ""


MOR_EVERY = 60          # seconds between two looks (the look reads the held units; GitHub is read only for a marked pull request)
MOR_TOLD_FILE = "merge-on-resolve.json"
MOR_TOLD_KEEP = 14 * 86400


def mor_told_load(state_dir, parse):
    try:
        with open("%s/%s" % (state_dir, MOR_TOLD_FILE)) as handle:
            raw = parse(handle.read())
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, (int, float))} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def mor_told_save(state_dir, told):
    import json, os
    path = "%s/%s" % (state_dir, MOR_TOLD_FILE)
    with open(path + ".tmp", "w") as handle:
        handle.write(json.dumps(told, sort_keys=True) + "\n")
    os.replace(path + ".tmp", path)


def merge_on_resolve_tick(engine, env):
    """One look at the marked pull requests, at most once in MOR_EVERY seconds; never raises. env: parse (the bounded JSON reader), repos() -> [repo], marked(repo) -> [number], context(repo, number) ->
    the `mor_run` context plus `gh` and `route(repo, number) -> [text sink]`; a told reason is kept in MOR_TOLD_FILE so a restart never repeats it."""
    now = engine.now()
    if now - getattr(engine, "mor_last", 0) < MOR_EVERY:
        return
    engine.mor_last = now
    try:
        told = mor_told_load(engine.state_dir, env["parse"])
        told = {k: v for k, v in told.items() if now - v < MOR_TOLD_KEEP}
        changed = []
        for repo in env["repos"]():
            for number in env["marked"](repo):
                ctx = env["context"](repo, number)
                inner = ctx["tell"]

                def once(r, n, key, text, inner=inner):
                    if key not in told:
                        told[key] = now
                        changed.append(key)
                        inner(r, n, key, text)
                ctx["tell"] = once
                code, out, _ = ctx["gh"](["pr", "view", str(number), "-R", repo, "--json", "state,labels,comments,headRefOid,baseRefName"])
                view = env["parse"](out) if code == 0 else None
                if not isinstance(view, dict):
                    continue
                outcome, reason = mor_run(ctx, repo, number, view)
                if outcome != "skip":
                    engine._note("merge_on_resolve.%s" % outcome, "%s#%d" % (repo, number), {"reason": reason[:200]})
        if changed:
            mor_told_save(engine.state_dir, told)
    except Exception as err:
        engine._note("merge_on_resolve.error", "", {"error": ("%s: %s" % (type(err).__name__, err))[:160]})

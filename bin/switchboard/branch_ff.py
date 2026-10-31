# ---------------------------------------------------------------- `clear-head` and `branch-ff` (#1712)
#
# An orchestrator may move a pull request's branch to a head that was already read and cleared, with no second pod. Two commands:
#   clear-head PR --sha SHA  the Chief of Staff records that the exact 40-hex sha was read and cleared for that pull request (a state-directory ledger line).
#   branch-ff PR SCRATCH     moves the pull request's HEAD BRANCH to the scratch branch's head by ONE GitHub ref update (`force` false: the server itself
#                            refuses a non-fast-forward), only when every condition holds. It never forces, has no --force option, runs no local git.
# A GitHub read that fails is a refusal. A refusal prints ONE line and writes nothing.

CLEAR_HEADS_FILE = "clear-heads.json"      # {"lines": [{"id", "pr", "sha", "by", "at"}]}
CLEAR_HEADS_MAX_BYTES = 1 << 20
BRANCH_FF_INTEGRATION = "integration"
BRANCH_FF_SHA_RE = re.compile(r"[0-9a-f]{40}")
BRANCH_FF_NAME_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")


def clear_head_lines(state_dir):
    """The ledger's lines; [] when there is no file. Raises ValueError when the file is unreadable or not a ledger (a caller treats that as a refusal)."""
    path = Path(state_dir) / CLEAR_HEADS_FILE
    try:
        with open(str(path), "rb") as handle:
            raw = handle.read(CLEAR_HEADS_MAX_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError as err:
        raise ValueError("cannot read %s: %s" % (CLEAR_HEADS_FILE, err.strerror or err)) from None
    if len(raw) > CLEAR_HEADS_MAX_BYTES:
        raise ValueError("%s is too large" % CLEAR_HEADS_FILE)
    data = load_module("record_json").parse_bounded(raw.decode("utf-8", "replace"))
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        raise ValueError("%s is not a ledger" % CLEAR_HEADS_FILE)
    return [x for x in lines if isinstance(x, dict) and isinstance(x.get("id"), int) and isinstance(x.get("pr"), str) and isinstance(x.get("sha"), str)]


def branch_ff_refuse(text):
    sys.stderr.write("%s: %s\n" % (PROG, _one_line(text, 300)))
    return EXIT_USAGE


def cmd_clear_head(args, runner, now):
    """`clear-head PR --sha SHA`: record that the full sha was read and cleared for the pull request. Only the Chief of Staff; anyone else is refused (audited)."""
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        return branch_ff_refuse(str(err))
    pr = "%s#%d" % (repo, number)
    if not BRANCH_FF_SHA_RE.fullmatch(args.sha or ""):
        return branch_ff_refuse("--sha must be the full 40-character lower-case hex sha")
    kind = gates_caller_kind(runner, now)
    engine, state, _ = build_engine(runner, now, "cli")
    if not load_module("delegated_clear").caller_is_chief(kind):
        engine.audit.append(engine.actor, "clear_head.refused", pr, {"by": kind[1], "sha": args.sha})
        return branch_ff_refuse("clear-head: only the Chief of Staff records a cleared head; %s is not" % kind[1])
    try:
        lines = clear_head_lines(state)
    except ValueError as err:
        return branch_ff_refuse("clear-head: %s: nothing was recorded" % err)
    line = {"id": max([x["id"] for x in lines] + [0]) + 1, "pr": pr, "sha": args.sha, "by": kind[1], "at": int(now())}
    _write_small(state, CLEAR_HEADS_FILE, {"lines": lines + [line]})
    engine.audit.append(engine.actor, "clear_head.recorded", pr, {"by": kind[1], "sha": args.sha, "line": line["id"]})
    print("clear-head #%d recorded: %s at %s (by %s)" % (line["id"], pr, args.sha, kind[1]))
    return EXIT_OK


def branch_ff_read(engine, runner, repo, path):
    """One GET through the engine's gh runner (the repo's account, counted): the answer as a dict. Raises when the read fails or is not an object."""
    done = engine.accounts.read(runner, path, repo, engine.config["call_timeout_seconds"])
    if done.returncode != 0:
        raise ValueError("%s failed" % path.split("?")[0])
    got = load_module("record_json").parse_bounded(done.stdout or "")
    if not isinstance(got, dict):
        raise ValueError("%s answered no object" % path.split("?")[0])
    return got


def branch_ff_first_refusal(engine, runner, state, repo, number, scratch):
    """(refusal text, None) for the FIRST failed condition, or (None, facts) when all hold. Every GitHub read that fails is a refusal."""
    def read(path):
        return branch_ff_read(engine, runner, repo, path)
    pr = "%s#%d" % (repo, number)
    try:
        pull = read("repos/%s/pulls/%d" % (repo, number))
        head, base = pull.get("head") or {}, pull.get("base") or {}
        branch, current = head.get("ref"), head.get("sha")
        if not (isinstance(branch, str) and BRANCH_FF_NAME_RE.fullmatch(branch) and isinstance(current, str) and BRANCH_FF_SHA_RE.fullmatch(current)):
            return "the head of %s could not be read" % pr, None
        if pull.get("state") != "open":
            return "%s is %s, not open" % (pr, "merged" if pull.get("merged") or pull.get("merged_at") else "closed"), None
        if (head.get("repo") or {}).get("full_name") != repo:
            return "%s comes from another repository" % pr, None
        ref = read("repos/%s/git/ref/heads/%s" % (repo, urllib.parse.quote(scratch, safe="/")))
        target = (ref.get("object") or {}).get("sha")
        if not (isinstance(target, str) and BRANCH_FF_SHA_RE.fullmatch(target)):
            return "the head of %s could not be read" % scratch, None
        compared = read("repos/%s/compare/%s...%s" % (repo, current, target))
        if compared.get("status") != "ahead":
            return "the head of %s is not an ancestor of %s (compare status %s)" % (pr, scratch, compared.get("status")), None
        try:
            lines = clear_head_lines(state)
        except ValueError as err:
            return "the clear-head ledger could not be read: %s" % err, None
        found = [x for x in lines if x["pr"] == pr and x["sha"] == target]
        if not found:
            return "no clear-head line for %s at %s" % (pr, target), None
        default = read("repos/%s" % repo).get("default_branch")
        if not isinstance(default, str) or not default:
            return "the default branch of %s could not be read" % repo, None
        if branch in (default, BRANCH_FF_INTEGRATION):
            return "the head branch of %s is %s: never moved here" % (pr, branch), None
    except Exception as err:   # fail closed: a read that fails is a refusal
        return "a GitHub read failed (%s): nothing was moved" % _one_line(str(err), 120), None
    return None, {"branch": branch, "from": current, "to": target, "line": found[0]["id"]}


def cmd_branch_ff(args, runner, now):
    """`branch-ff PR SCRATCH_BRANCH`: move the pull request's head branch to the scratch branch's head (fast-forward only, never forced). One GitHub write."""
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        return branch_ff_refuse(str(err))
    scratch = args.scratch
    if not BRANCH_FF_NAME_RE.fullmatch(scratch or "") or scratch.startswith(("-", "/")) or ".." in scratch:
        return branch_ff_refuse("the scratch branch name is not a plain branch name")
    kind = gates_caller_kind(runner, now)
    engine, state, _ = build_engine(runner, now, "cli")
    why, facts = branch_ff_first_refusal(engine, runner, state, repo, number, scratch)
    if why:
        return branch_ff_refuse("branch-ff refused: " + why)
    pr = "%s#%d" % (repo, number)
    try:
        _gh_write(engine, runner, repo, "repos/%s/git/refs/heads/%s" % (repo, urllib.parse.quote(facts["branch"], safe="/")), "PATCH", {"sha": facts["to"], "force": False})
    except IntentError as err:
        return branch_ff_refuse("branch-ff refused: GitHub did not move the branch: %s" % _one_line(str(err), 150))
    engine.audit.append(engine.actor, "branch.ff", pr, {"by": kind[1], "pr": pr, "from": facts["from"], "to": facts["to"], "clear_head": facts["line"]})
    print("branch.ff by %s: %s %s -> %s (clear-head #%d)" % (kind[1], pr, facts["from"], facts["to"], facts["line"]))
    return EXIT_OK


def add_branch_ff_parsers(sub):
    clear = sub.add_parser("clear-head", help="Chief of Staff only: record that a pull request's exact head sha was read and cleared")
    clear.add_argument("pr", help="The pull request: OWNER/REPO#N or its URL")
    clear.add_argument("--sha", required=True, help="The full 40-hex sha that was read and cleared")
    ff = sub.add_parser("branch-ff", help="Move a pull request's head branch, fast-forward only, to a scratch branch's cleared head (never forces)")
    ff.add_argument("pr", help="The pull request: OWNER/REPO#N or its URL")
    ff.add_argument("scratch", metavar="SCRATCH_BRANCH", help="The branch whose head the pull request's branch moves to")

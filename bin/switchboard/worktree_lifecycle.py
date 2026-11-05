# ---------------------------------------------------------------- worktree lifecycle (#321 slices S2, S3, S10)
#
# `launch` records a linked worktree it is pointed at ONLY when no other agent holds it (it does not run `git worktree add`, so it cannot prove it
# created it: the owner is inferred, which the docs say; an existing owner is never overwritten). `retire`, an automatic retire and
# `disk reclaim-worktrees --apply` remove a REGISTERED worktree only when `git status` is clean AND its HEAD is on a remote branch (pushed or
# merged); otherwise they keep it and say why in one line. A `done` report only says a worktree is reclaimable. Never an unregistered worktree,
# never a main checkout, never `--force`, never a branch. The kill switch is `worktree_reclaim.enabled: false` (and `on_done: false` for the done note).

WORKTREES_FILE = "worktrees.json"
WORKTREE_GIT_SECONDS = 20
WORKTREE_RECLAIM_MAX = 20     # worktrees looked at per call: a pass or a command never does unbounded git work
# Ignored files that are regenerable build output: deleting them with the worktree loses nothing. ANY OTHER ignored file (.env, local config, data)
# is unrecoverable local state, so its worktree is kept.
IGNORED_SAFE = ("node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".venv", "venv", ".DS_Store", ".tox")


def worktree_config(config):
    section = (config or {}).get("worktree_reclaim") or {}
    return {"enabled": section.get("enabled", True) is not False, "on_done": section.get("on_done", True) is not False}


def validate_worktree_config(config, source):
    raw = config.get("worktree_reclaim")
    if raw is None:
        return
    if not isinstance(raw, dict) or set(raw) - {"enabled", "on_done"} or any(not isinstance(v, bool) for v in raw.values()):
        raise ConfigError("%s: 'worktree_reclaim' must be {\"enabled\": true|false, \"on_done\": true|false}, got %r" % (source, raw))


def _git(runner, path, *args):
    """git always runs for real (the `runner` the CLI passes may be a fleet fake that refuses other binaries): argv list, no shell, a timeout."""
    try:
        return subprocess.run(["git", "-C", str(path)] + list(args), capture_output=True, text=True, timeout=WORKTREE_GIT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as err:
        return subprocess.CompletedProcess(args, 255, "", str(err))


def worktree_facts(runner, path):
    """{"exists", "linked", "main", "branch", "dirty", "pushed", "why"}: what git says about `path`. `linked` is True only for a LINKED worktree
    (its git dir differs from the common one); a main checkout is never linked. `pushed` is True when HEAD is on some remote branch (pushed or
    merged). Anything git cannot answer leaves `why` set and is treated as not safe to remove."""
    facts = {"exists": os.path.isdir(path), "linked": False, "main": None, "branch": None, "dirty": None, "pushed": None, "why": None}
    if not facts["exists"]:
        return facts
    gd = _git(runner, path, "rev-parse", "--path-format=absolute", "--git-dir")
    cd = _git(runner, path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if gd.returncode != 0 or cd.returncode != 0:
        facts["why"] = "git cannot read it (%s)" % _one_line(gd.stderr or cd.stderr, 80)
        return facts
    git_dir, common = os.path.realpath(gd.stdout.strip()), os.path.realpath(cd.stdout.strip())
    facts["linked"] = git_dir != common
    facts["main"] = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    head = _git(runner, path, "rev-parse", "--abbrev-ref", "HEAD")
    facts["branch"] = head.stdout.strip() if head.returncode == 0 else None
    status = _git(runner, path, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        facts["why"] = "git status failed (%s)" % _one_line(status.stderr, 80)
        return facts
    facts["dirty"] = [l for l in status.stdout.splitlines() if l.strip()]
    ignored = _git(runner, path, "status", "--porcelain", "--ignored", "--untracked-files=all")
    if ignored.returncode != 0:
        facts["why"] = "git could not list the ignored files (%s)" % _one_line(ignored.stderr, 80)
        return facts
    facts["ignored"] = [l[3:] for l in ignored.stdout.splitlines() if l.startswith("!! ")
                        and not any(part in IGNORED_SAFE or part.endswith(".pyc") for part in l[3:].strip("/").split("/"))]
    contains = _git(runner, path, "branch", "-r", "--contains", "HEAD")
    if contains.returncode != 0:
        facts["why"] = "git could not say whether HEAD is pushed (%s)" % _one_line(contains.stderr, 80)
        return facts
    facts["pushed"] = any(l.strip() and "->" not in l for l in contains.stdout.splitlines())
    return facts


def read_worktrees(state_dir):
    return _read_small(state_dir, WORKTREES_FILE)


def register_worktree(state_dir, path, agent, repo, branch, now):
    """Record one worktree (atomic, 0600, under the state lock). Returns True when written, False when it could not be, and the OWNER's name (a str)
    when another agent already holds it: an existing entry's owner is never overwritten."""
    try:
        with agent_lock(state_dir, "files", seconds=2):
            data = read_worktrees(state_dir)
            held = data.get(str(path))
            if isinstance(held, dict) and held.get("agent") not in (None, agent):
                return held.get("agent")   # a second claim never takes the owner over: the caller says so
            data[str(path)] = {"agent": agent, "repo": repo, "branch": branch, "created": int(now)}
            _write_small(state_dir, WORKTREES_FILE, data)
        return True
    except Exception:
        return False


def _unregister_worktree(state_dir, path):
    """Drop a registry entry. Returns whether it is gone (False: the registry could not be written; the caller says so)."""
    try:
        with agent_lock(state_dir, "files", seconds=2):
            data = read_worktrees(state_dir)
            if data.pop(str(path), None) is not None:
                _write_small(state_dir, WORKTREES_FILE, data)
        return True
    except Exception:
        return False


def worktree_in_use(engine, path):
    """None when no herdr pane (an agent's OR a plain terminal) has its working directory, or its foreground process's, inside `path`; else why it is
    kept. ONE `herdr pane list`, no row cap. Anything herdr cannot say keeps the worktree (no proof, no removal)."""
    herdr = getattr(engine, "herdr", None)
    if herdr is None:
        return None
    try:
        panes = herdr.pane_list()
    except HerdrError as err:
        return "herdr cannot say whether a pane works in it (%s)" % _one_line(str(err), 80)
    root = os.path.realpath(path).rstrip("/") + "/"
    for pane in panes:
        if not isinstance(pane, dict):
            return "herdr listed a pane this cannot read: it cannot say none works in it"
        for key in ("cwd", "foreground_cwd"):
            where = pane.get(key)
            if isinstance(where, str) and where and (os.path.realpath(where) + "/").startswith(root):
                return "herdr pane %s is working in it (%s): close it or leave that directory first" % (pane.get("pane_id"), key)
    return worktree_session_in_use(engine, root)


@contextmanager
def worktree_sweep(engine):
    """One session.list serves every in-use check inside the block; the cache is dropped on the way out, whatever happens."""
    before = (getattr(engine, "_wt_sweep", False), getattr(engine, "_wt_session_index", None))
    engine._wt_sweep, engine._wt_session_index = True, None
    try:
        yield
    finally:
        engine._wt_sweep, engine._wt_session_index = before


WORKTREE_SESSION_PAGES = 20    # pages of session.list read once per sweep; a list longer than this is incomplete


def worktree_session_index(engine):
    """({session id: directory}, complete) from ONE paged `session.list`, cached on the engine for the duration of a sweep (`engine._wt_sweep`);
    a call outside a sweep reads fresh. V2Error propagates (the caller keeps the worktree)."""
    cached = getattr(engine, "_wt_session_index", None)
    if cached is not None and getattr(engine, "_wt_sweep", False):
        return cached
    index, cursor, complete = {}, None, False
    for _ in range(WORKTREE_SESSION_PAGES):
        page, cursor = engine.v2.session_list_page(None, cursor)
        for item in page:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                index[item["id"]] = (item.get("location") or {}).get("directory") or item.get("directory")
        if cursor is None:
            complete = True
            break
    result = (index, complete)
    if getattr(engine, "_wt_sweep", False):
        engine._wt_session_index = result
    return result


def worktree_session_in_use(engine, root):
    """None when no NON-RETIRED agent's OpenCode session runs with its directory inside `root` (realpath, trailing "/"), else why it is kept. A session
    can run in a worktree while its pane sits elsewhere. The agents are those the switchboard knows (the pane-health registry and the last discovery);
    their sessions are looked up in ONE `session.list` per sweep. A list that cannot be read keeps the worktree, and so does a known session missing
    from an incomplete (too long) list; a session missing from a complete list is gone and is no one's directory."""
    if engine.state_dir is None:
        return None
    seen = registry_read(engine.state_dir)
    if not seen["whole"]:
        return "cannot read the agent registry (%s): cannot say no session runs in it" % _one_line(str(seen["problem"]), 80)
    retired = seen["retired"]
    known = {}
    for name, info in seen["agents"].items():   # the registry's local agents (a cloud worker's session is not in the local service)
        if isinstance(info.get("session"), str) and (info.get("kind") or "local") == "local":
            known[name] = info["session"]
    for name, obs in (getattr(getattr(engine, "discovery", None), "agents", None) or {}).items():
        if getattr(obs, "session_id", None):
            known[name] = obs.session_id
    todo = sorted((name, sid) for name, sid in known.items() if name not in retired)
    if not todo:
        return None
    try:
        index, complete = worktree_session_index(engine)
    except V2Error as err:
        return "cannot read the session list (%s): cannot say no session runs in it" % _one_line(str(err), 80)
    for name, sid in todo:
        if sid not in index:
            if not complete:
                return "the session of %s is not in the first %d pages of the session list: cannot say it does not run in it" % (name, WORKTREE_SESSION_PAGES)
            try:   # a list may hide a session `session.get` can read: ask for the one that is missing before calling it gone
                got = engine.v2.session_get(sid)
            except V2Error as err:
                if err.reason == "http" and err.status == 404:
                    continue
                return "cannot read the session of %s (%s): cannot say it does not run in it" % (name, _one_line(str(err), 80))
            index = dict(index, **{sid: ((got.get("location") or {}).get("directory") or got.get("directory")) if isinstance(got, dict) else None})
        where = index[sid]
        if isinstance(where, str) and where and (os.path.realpath(where) + "/").startswith(root):
            return "the session of %s (%s) runs in it: retire it first" % (name, sid)
    return None


def reclaim_worktree(engine, runner, path, entry, apply=True):
    """(status, line) for ONE registered worktree: `removed`, `would-remove`, `kept` (with the reason), `gone` (the directory is already gone: the entry is
    dropped) or `refused` (not a linked worktree: never touched). Never forces, never removes a branch."""
    if not isinstance(entry, dict) or not isinstance(path, str) or not os.path.isabs(path) or path.startswith("-"):
        return "refused", "%s: refused: not a usable registry entry" % _one_line(str(path), 60)
    agent = entry.get("agent")
    facts = worktree_facts(runner, path)
    if not facts["exists"]:
        dropped = apply and _unregister_worktree(engine.state_dir, path)
        return "gone", "%s: already gone%s" % (path, "; dropped from the registry" if dropped else ("; the registry entry could not be dropped" if apply else ""))
    if facts["why"]:
        return "kept", "%s: kept: %s" % (path, facts["why"])
    if not facts["linked"]:
        return "refused", "%s: refused: it is a main checkout, never touched" % path
    if facts["dirty"]:
        return "kept", "%s: kept: uncommitted changes (%d file(s), e.g. %s)" % (path, len(facts["dirty"]), _one_line(facts["dirty"][0], 60))
    if facts.get("ignored"):
        return "kept", "%s: kept: ignored local file(s) that are not known build output would be lost (%d, e.g. %s)" % (path, len(facts["ignored"]), _one_line(facts["ignored"][0], 60))
    if not facts["pushed"]:
        return "kept", "%s: kept: unpushed (HEAD %s is on no remote branch)" % (path, facts["branch"] or "detached")
    busy = worktree_in_use(engine, path)
    if busy:
        return "kept", "%s: kept: %s" % (path, busy)
    if not apply:
        return "would-remove", "%s: would remove (clean, pushed; branch %s)" % (path, facts["branch"])
    again = _git(runner, path, "status", "--porcelain", "--untracked-files=all")   # the window between the check and the remove is as short as it can be
    if again.returncode != 0 or again.stdout.strip():
        return "kept", "%s: kept: it changed while it was being checked" % path
    done = _git(runner, facts["main"], "worktree", "remove", "--", str(path))
    if done.returncode != 0:
        return "kept", "%s: kept: git refused to remove it (%s)" % (path, _one_line(done.stderr, 100))
    stale = "" if _unregister_worktree(engine.state_dir, path) else "; the registry entry could not be dropped (it is harmless: the directory is gone)"
    if engine.audit is not None:
        engine.audit.append(engine.actor, "worktree.removed", agent or path, {"path": str(path), "branch": facts["branch"], "repo": entry.get("repo")})
    return "removed", ("%s: removed (clean, pushed; branch %s kept). The session of %s cannot be resumed from that directory: it is gone; "
                       "the way back is a fresh launch under a new name%s") % (path, facts["branch"], _one_line(str(agent or "its worker"), 40), stale)


def reclaim_for_agent(engine, runner, name, apply=True, why="retired"):
    """Reclaim every REGISTERED worktree of agent `name`. Returns the one-line results (empty when it has none). Off with worktree_reclaim.enabled false."""
    if engine.state_dir is None:
        return []
    mine = {p: e for p, e in read_worktrees(engine.state_dir).items() if isinstance(e, dict) and e.get("agent") == name}
    if not mine:
        return []
    if not worktree_config(engine.config)["enabled"]:
        return ["worktree reclaim is switched off (worktree_reclaim.enabled is false): %d worktree(s) of %s kept" % (len(mine), name)]
    lines = []
    with worktree_sweep(engine):   # one session.list for the whole sweep
        results = [(path,) + reclaim_worktree(engine, runner, path, entry, apply) for path, entry in sorted(mine.items())[:WORKTREE_RECLAIM_MAX]]
    for path, status, line in results:
        lines.append(line)
        if status == "kept" and engine.audit is not None:
            engine.audit.append(engine.actor, "worktree.kept", name, {"path": path, "reason": _one_line(line, 160), "after": why})
    if len(mine) > WORKTREE_RECLAIM_MAX:
        lines.append("%d of %d worktree(s) handled (cap %d); `disk reclaim-worktrees` does the rest" % (WORKTREE_RECLAIM_MAX, len(mine), WORKTREE_RECLAIM_MAX))
    return lines


def worktree_of_directory(runner, directory):
    """(path, branch) when `directory` is a LINKED worktree root, else None."""
    facts = worktree_facts(runner, directory)
    if not facts["exists"] or not facts["linked"]:
        return None
    top = _git(runner, directory, "rev-parse", "--path-format=absolute", "--show-toplevel")
    path = top.stdout.strip() if top.returncode == 0 else directory
    return path, facts["branch"]


def cmd_disk_reclaim_worktrees(args, runner, now):
    """`disk reclaim-worktrees [--apply]`: the registered worktrees of RETIRED agents. A dry run by default; `--apply` is the operator's or the Chief of
    Staff's. Never an unregistered worktree, never a main checkout."""
    engine, state, config = build_engine(runner, now, "cli", writable=bool(args.apply))
    if args.apply:
        kind = gates_caller_kind(runner, now)
        if not (kind[0] == "person" or (kind[0] == "agent" and len(kind) > 2 and kind[2] == "chief-of-staff")):
            raise LaunchError("refused: only the operator or the Chief of Staff may --apply a worktree reclaim (%s is %s)" % (kind[1], "an agent" if kind[0] == "agent" else "not known"))
        if not worktree_config(config)["enabled"]:
            raise LaunchError("worktree reclaim is switched off (worktree_reclaim.enabled is false)")
    retired = registry_retired(state)
    registry = read_worktrees(state)
    todo = {p: e for p, e in registry.items() if isinstance(e, dict) and e.get("agent") in retired}
    removed = kept = 0
    with worktree_sweep(engine):   # one session.list for the whole sweep
        results = [reclaim_worktree(engine, runner, path, entry, bool(args.apply)) for path, entry in sorted(todo.items())[:WORKTREE_RECLAIM_MAX]]
    for status, line in results:
        print(line)
        removed += status in ("removed", "would-remove", "gone")
        kept += status in ("kept", "refused")
    if len(todo) > WORKTREE_RECLAIM_MAX:
        print("%s: only %d of %d worktree(s) handled (cap %d): run it again for the rest" % (PROG, WORKTREE_RECLAIM_MAX, len(todo), WORKTREE_RECLAIM_MAX))
    print("%s: %d registered worktree(s) of retired agents: %d %s, %d kept%s; %d registered in all, %d of agents not retired are left alone" % (
        PROG, len(todo), removed, "removed" if args.apply else "would be removed", kept, "" if args.apply else " (a dry run: add --apply)",
        len(registry), len(registry) - len(todo)))
    return EXIT_OK


def cmd_disk_register_worktree(args, runner, now):
    """`disk register-worktree <path> --agent NAME`: record a worktree launch did not create. Same caller rule as `reattach` (operator, Chief of Staff or the agent's boss)."""
    engine, state, _ = build_engine(runner, now, "cli")
    who = reattach_caller_problem(engine, runner, now, engine.discover(engine.now()), args.agent)
    if who:
        raise LaunchError("refused: %s" % who)
    path = os.path.realpath(args.path)
    found = worktree_of_directory(runner, path)
    if found is None:
        raise LaunchError("%s is not a linked git worktree (a main checkout or a plain directory is never registered)" % path)
    done = register_worktree(state, found[0], args.agent, args.repo, found[1], now())
    if isinstance(done, str):
        raise LaunchError("refused: %s is already registered to %s; a second claim never takes it over" % (found[0], done))
    if done is not True:
        raise LaunchError("the registry could not be written: nothing changed")
    if engine.audit is not None:
        engine.audit.append(engine.actor, "worktree.registered", args.agent, {"path": found[0], "branch": found[1], "by": "register-worktree"})
    print("registered %s (branch %s) for %s" % (found[0], found[1], args.agent))
    return EXIT_OK

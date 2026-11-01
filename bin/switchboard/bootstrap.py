# ----------------------------------------------------------------- bootstrap

COS_BRIEF = """Introduce yourself to the person in one or two sentences: you are the Chief of Staff, the one place
they talk to. You hold the plan and hand work out. You do not do work yourself.

Then run `fleet-switchboard status` and read all of it. Tell the person plainly what you found, in your own
words, and what is missing. Three things matter: whether a decision-model key is saved (without one, messages are
not classified and tool calls are not judged: say so first, and tell them `fleet-switchboard key set` saves one),
whether any GitHub repos are watched (`github_repos` in the switchboard config is empty until they choose), and
whether the daemon, the herdr plugin and the service are all in place.

End by asking what they want to work on. Do not start anything until they answer.
"""


class BootstrapRefused(Exception):
    """`bootstrap` will not open a second Chief of Staff."""


def find_cos_session(v2):
    """The newest v2 session whose fleet name is `cos`, or None: derived from v2, never from a stored id."""
    cursor, found = None, []
    for _ in range(V1_LIST_MAX_PAGES):
        sessions, cursor = v2.session_list_page(None, cursor)
        found.extend(s for s in sessions if isinstance(s, dict) and (_fleet_meta(s) or {}).get("name") == COS_NAME)
        if cursor is None:
            break
    if not found:
        return None
    return max(found, key=lambda s: (s.get("time") or {}).get("created") or 0)


def resume_brief(engine, session_id, pane):
    """A first attempt that died after the session was made leaves a Chief of Staff with no brief. Send it once,
    derived from the transcript: "sent", or "kept" when the session already holds it. A failure closes the pane
    this resume opened, as a failed launch does, and the next run tries again."""
    try:
        if brief_in_transcript(engine, session_id):
            return "kept"
        send_brief(engine, COS_NAME, session_id, COS_BRIEF)
        return "sent"
    except Exception as err:
        left = close_own_pane(engine, COS_NAME, pane, "bootstrap could not send the brief: %s" % _one_line(str(err), 120))
        raise LaunchError("%s (session %s still has no brief; %s; run bootstrap again to resume it and send "
                          "the brief)" % (_one_line(str(err)), session_id, left)) from err


def bootstrap_cos(engine, label=COS_WORKSPACE_LABEL, directory=None, patience=LAUNCH_PATIENCE, sleep=time.sleep,
                  focus=True):
    """Open the Chief of Staff for the first time, or again. {"action", "workspace", "name", "session", "pane", "tab"}.

    A `cos` that already has a pane is refused, naming it. A session without a pane is resumed in the
    workspace labelled `label`, where it was; the first time there is no session, and a new workspace is
    always made (and focused: the person wants to see it), never an old one that happens to share the label.
    The launch and the resume are the ones `launch` uses.
    """
    directory = directory or os.getcwd()
    if not os.path.isabs(directory) or not os.path.isdir(directory):
        raise LaunchError("--dir must be an absolute path of an existing directory, got %r" % directory)
    running = engine.discover(engine.now()).agents.get(COS_NAME)
    if running is not None:
        raise BootstrapRefused("a Chief of Staff already runs: %s in pane %s (session %s). There is one per fleet; "
                               "open its workspace in herdr" % (COS_NAME, running.pane, running.session_id))
    session = find_cos_session(engine.v2)
    existing = None
    if session is not None:
        existing = next((w for w in engine.herdr.workspace_list() if w.get("label") == label), None)
    created = None
    if existing is not None:
        workspace = existing["workspace_id"]
    else:
        with engine._external("herdr.workspace.create", COS_NAME, {"label": label, "cwd": directory}) as result:
            created = engine.herdr.workspace_create(directory, label, focus)
            workspace = result["workspace"] = created["workspace_id"]
    try:
        if session is not None:
            done, action = resume_agent(engine, COS_NAME, session["id"], workspace, patience, sleep), "resumed"
            done["brief"] = resume_brief(engine, session["id"], done["pane"])
        else:
            done, action = launch_agent(engine, COS_NAME, COS_AGENT, directory, workspace, COS_AGENT, None, None,
                                        COS_BRIEF, patience, sleep), "created"
            done["brief"] = "sent"
    except LaunchError as err:
        if created is not None:
            raise LaunchError("%s (the workspace %r was opened and is still there)" % (err, label)) from err
        raise
    if created is not None and created.get("root_pane"):  # the empty shell pane herdr gives a new workspace
        close_own_pane(engine, COS_NAME, created["root_pane"], "the empty pane of the workspace bootstrap opened")
    return dict(done, action=action, workspace=workspace)


def cmd_bootstrap(args, runner, now, socket_call=None):
    engine, _, _ = build_engine(runner, now, "cli", socket_call=socket_call)
    directory = os.path.abspath(args.dir) if args.dir else os.getcwd()
    try:
        done = bootstrap_cos(engine, args.workspace or COS_WORKSPACE_LABEL, directory, focus=not args.stay_put)
    except BootstrapRefused as err:
        sys.stderr.write("%s: bootstrap refused: %s\n" % (PROG, err))
        return EXIT_ERROR
    print(json.dumps(done, sort_keys=True))
    return EXIT_OK


cmd_bootstrap.ensure_on_use = True   # the daemon is alive when it runs

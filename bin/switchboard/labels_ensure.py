# ---- labels ensure

LABEL_SPECS = (("orchestrator", "5319e7", "A charter: an orchestrator's standing job"),
               ("awaiting-cos", "fbca04", "Waiting on the Chief of Staff: it resolves or escalates"),
               ("awaiting-user", "d93f0b", "Waiting on the owner: the fleet's only blocking channel"),
               # #133: held-for-the-owner state as labels (read by the switchboard, the sweep and the triage)
               ("parked", "c5def5", "Deliberately deferred: exempt from the no-next-step flag until its `Parked until: YYYY-MM-DD` line"),
               ("merge-captain", "b60205", "A PR held for the owner: it is not merged until the owner says so"),
               ("merge-automatic", "0e8a16", "A PR that merges on green CI and independent review"),
               ("hold-security", "d93f0b", "Held: a security boundary"),
               ("hold-credential", "d93f0b", "Held: a credential or secret"),
               ("hold-contract", "d93f0b", "Held: a shared contract or interface"),
               ("hold-policy", "d93f0b", "Held: a policy or permission rule"),
               ("hold-prod-config", "d93f0b", "Held: production configuration"),
               ("hold-other-area", "d93f0b", "Held: an area owned by another team"),
               ("hold-impact", "d93f0b", "Held: the impact floor or an independent rating says the change is high-impact"),
               ("hold-owner", "d93f0b", "The owner's own hold; only the owner lifts it"))


def label_names(config):
    names = {"orchestrator": orchestrator_label(config), "awaiting-cos": awaiting_cos_label(config),
             "awaiting-user": awaiting_label(config)}
    for key, _, _ in LABEL_SPECS:
        names.setdefault(key, "%s:%s" % (getpass.getuser(), key))   # the same `<user>:` namespace as the awaiting labels
    return names


def ensure_labels(engine, runner, repo):
    """Create the labels the fleet uses in `repo` (the three awaiting/orchestrator labels and the merge-class and hold labels),
    each with the repo's own gh account, unless they exist.
    Returns {label: "created" | "already there"}. Idempotent."""
    names = label_names(engine.config)
    made = {}
    for key, color, text in LABEL_SPECS:
        label = names[key]
        existing = _gh_write(engine, runner, repo, "repos/%s/labels/%s" % (repo, quote(label, safe="")), "GET",
                             tolerate_missing=True)
        if existing.returncode == 0:
            made[label] = "already there"
            continue
        with engine._external("labels.create", "label:%s:%s" % (repo, label), {"repo": repo, "label": label}):
            _gh_write(engine, runner, repo, "repos/%s/labels" % repo, "POST",
                      {"name": label, "color": color, "description": text})
        made[label] = "created"
    return made


MARKER_PAGES = 10   # pages of 100 open items read per repo; a longer list is said to be cut


def read_open_items(engine, runner, repo):
    """([{"number", "pr", "body", "labels", "author"}], cut): the open issues and PRs of `repo` from `gh api`, read with the
    repo's own account (GET only). IntentError when a read fails."""
    items = []
    for page in range(1, MARKER_PAGES + 1):
        done = _gh_write(engine, runner, repo, "repos/%s/issues?state=open&per_page=100&page=%d" % (repo, page), "GET")
        try:
            records = json.loads(done.stdout or "[]")
        except ValueError:
            raise IntentError("gh", "the list of %s was not JSON" % repo) from None
        if not isinstance(records, list):
            raise IntentError("gh", "the list of %s was not a list" % repo)
        for record in records:
            number = _number(record) if isinstance(record, dict) else None
            if number is None:
                continue
            items.append({"number": number, "pr": _is_pr(record), "body": record.get("body"),
                          "labels": {(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []},
                          "author": pr_login(record.get("user")) or "?"})
        if len(records) < 100:
            return items, False
    return items, True


def markers_to_label(engine, items):
    """[(item, label)]: the items whose first line says MERGE: captain|automatic and which carry NO merge label (an item that
    has one is left alone: the label is the truth, a disagreement is the sweep's finding, not ours to rewrite)."""
    names = label_names(engine.config)
    todo = []
    for item in items:
        if _label_kind(item["labels"], CAPTAIN_MERGE_LABELS):
            continue
        marker = pr_marker(item["body"])
        if marker is not None:
            todo.append((item, names["merge-" + marker]))
    return todo


def apply_from_markers(engine, runner, repo, apply):
    """(lines, changed, cut) for one repo. With `apply` it first ensures the labels exist (idempotent), then adds each missing
    merge label to its item, each audited (`decisions.labels`). Never run by the daemon: only `labels apply-from-markers`."""
    items, cut = read_open_items(engine, runner, repo)
    todo = markers_to_label(engine, items)
    if apply and todo:
        ensure_labels(engine, runner, repo)
    lines, changed = [], 0
    for item, label in todo:
        what = "%s#%d  %s  by %s  + %s" % (repo, item["number"], "PR" if item["pr"] else "issue", item["author"], label)
        if apply:
            change_issue_labels(engine, runner, repo, item["number"], add=[label])
            changed += 1
            lines.append("applied  " + what)
        else:
            lines.append("would add  " + what)
    return lines, changed, cut


def cmd_labels(args, runner, now):
    if args.labels_action == "apply-from-markers":
        return cmd_labels_apply(args, runner, now)
    engine, _, _ = build_engine(runner, now, "cli")
    if args.repo is not None and not REPO_RE.match(args.repo):
        sys.stderr.write("%s: --repo must be OWNER/REPO, got %r\n" % (PROG, args.repo))
        return EXIT_USAGE
    repos = [args.repo] if args.repo else decision_repos(engine)
    if not repos:
        sys.stderr.write("%s: no repo is watched: name one with --repo OWNER/REPO, or add it to github_repos\n" % PROG)
        return EXIT_USAGE
    failed = False
    for repo in repos:
        try:
            made = ensure_labels(engine, runner, repo)
        except IntentError as err:
            sys.stderr.write("%s: %s: %s\n" % (PROG, repo, _one_line(str(err), 300)))
            failed = True
            continue
        for label, what in made.items():
            print("%s  %s  %s" % (repo, label, what))
    return EXIT_ERROR if failed else EXIT_OK


def cmd_labels_apply(args, runner, now):
    engine, _, _ = build_engine(runner, now, "cli", writable=bool(args.apply))
    if args.repo is not None and not REPO_RE.match(args.repo):
        sys.stderr.write("%s: --repo must be OWNER/REPO, got %r\n" % (PROG, args.repo))
        return EXIT_USAGE
    repos = [args.repo] if args.repo else decision_repos(engine)
    if not repos:
        sys.stderr.write("%s: no repo is watched: name one with --repo OWNER/REPO, or add it to github_repos\n" % PROG)
        return EXIT_USAGE
    failed, total = False, 0
    for repo in repos:
        try:
            lines, changed, cut = apply_from_markers(engine, runner, repo, bool(args.apply))
        except IntentError as err:
            sys.stderr.write("%s: %s: %s\n" % (PROG, repo, _one_line(str(err), 300)))
            failed = True
            continue
        total += len(lines)
        for line in lines:
            print(line)
        if cut:
            sys.stderr.write("%s: %s: the open list was cut after %d pages; run again after labelling\n" % (PROG, repo, MARKER_PAGES))
    print("%s: %d item(s) %s" % (PROG, total, "labelled" if args.apply else "would be labelled (a dry run: nothing was written; add --apply)"))
    return EXIT_ERROR if failed else EXIT_OK

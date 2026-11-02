# ---------------------------------------------------------------- `agents verify` (#179): did the service register OUR agents as written?
#
# OpenCode v2 silently registers an agent whose front matter does not parse as a default PRIMARY agent with a handful of rules of its own making.
# The service is asked (`opencode api GET /api/agent`, read-only) and each of our agents is compared with its definition: its mode, and EVERY rule its
# front matter declares (a `permissions:` list of {action, resource, effect}, or a `permission:` map, where `edit: allow` is action edit, resource `*`
# and `bash` is action `shell`) must be among the registered rules with the same effect. Not a count (the service adds default rules of its own, and
# the number of them changes between versions) and not an order (the last match wins, but a present rule is a loaded one). A default agent has none of
# ours, so a failed parse fails it. A service that is not running is said, not failed: nothing was installed wrongly.
EXPECTED_AGENTS = {
    "chief-of-staff": "primary",
    "cos-subagent": "primary",
    "orchestrator": "primary",
    "coder": "primary",
    "reviewer-code": "subagent",
    "reviewer-architecture": "subagent",
    "reviewer-security": "subagent",
    "reviewer-impact": "subagent",
    "reviewer-test": "subagent",
}
AGENTS_VERIFY_TIMEOUT = 20
AGENT_DEFINITION_DIRS = ("opencode", "reviewers")
_RULE_LINE = re.compile(r'^\s*-\s*\{action:\s*(.+?),\s*resource:\s*(".*"),\s*effect:\s*(allow|deny|ask)\}\s*$')
_MAP_LINE = re.compile(r'^  ([A-Za-z_][A-Za-z0-9_]*):\s*(allow|deny|ask)\s*(?:#.*)?$')


def agent_definition(name, root=None):
    """(rules, unread, path) of the kit's definition of agent `name`: its declared rules as a sorted list of (action, resource, effect), the
    lines of its permission block that the parser could not read (so they could not be checked), and the file used; None when the
    definition cannot be found (an install without the kit's agents directory)."""
    base = Path(root) if root is not None else Path(os.path.realpath(__file__)).parent.parent / "agents"
    for folder in AGENT_DEFINITION_DIRS:
        path = base / folder / (name + ".md")
        try:
            text = path.read_text()
        except OSError:
            continue
        front = text.split("\n---\n", 1)[0].splitlines()[1:]   # without the opening ---
        rules, unread, section = set(), 0, None
        for line in front:
            if line.startswith("permissions:"):
                section = "list"
            elif line.startswith("permission:"):
                section = "map"
            elif line and not line.startswith((" ", "#")):
                section = None
            elif section and line.strip() and not line.lstrip().startswith("#"):
                match = _RULE_LINE.match(line) if section == "list" else _MAP_LINE.match(line)
                if match is None:
                    unread += 1
                elif section == "list":
                    try:
                        action = json.loads(match.group(1)) if match.group(1).startswith('"') else match.group(1)
                        rules.add((action, json.loads(match.group(2)), match.group(3)))
                    except ValueError:   # a quoted string that is not valid JSON (a bad escape): not read, so not verified, and said
                        unread += 1
                else:
                    rules.add(("shell" if match.group(1) == "bash" else match.group(1), "*", match.group(2)))
        return sorted(rules), unread, str(path)
    return None


def agent_definition_rules(name, root=None):
    """The declared rules of the definition (see `agent_definition`), or None when it cannot be found."""
    found = agent_definition(name, root)
    return None if found is None else found[0]


def agents_found(runner, opencode, timeout):
    """(state, payload): ("down", why) when the service cannot be asked, else ("up", {name: (mode, {(action, resource, effect)})}) or ("bad", why)."""
    try:
        refuse_real_opencode_under_test(runner, opencode)
        done = runner([opencode, "api", "GET", "/api/agent"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except V2Error as err:
        return "down", str(err)
    except subprocess.TimeoutExpired:
        return "down", "%s did not answer within %ds" % (opencode, timeout)
    except OSError as err:
        return "down", "cannot run %s: %s" % (opencode, err.strerror or err)
    if done.returncode != 0:
        return "down", "%s api GET /api/agent exited %s: %s" % (opencode, done.returncode, _one_line(done.stderr or done.stdout))
    try:
        rows = json.loads(done.stdout)["data"]
        return "up", {row["id"]: (row["mode"], {(p.get("action"), p.get("resource"), p.get("effect")) for p in row["permissions"] if isinstance(p, dict)})
                      for row in rows}
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        return "bad", "the service answered something that is not the agent list (%s)" % err


def agent_body(text):
    """The prose of an agent file: what follows its front matter (the model line, the permissions and the mode are in the front matter, so they are not compared)."""
    return text.split("\n---\n", 1)[1] if "\n---\n" in text else text


def default_installed_agents_dir():
    """Where `install` writes the agent files of the private profile. Under the test suite there is none unless a test names one: the suite never reads the machine's own profile."""
    if os.environ.get(UNDER_TEST_ENV):
        return None
    return _xdg("XDG_DATA_HOME", ".local/share") / PROG / "profile" / "config" / "opencode" / "agents"


def stale_installed_agents(installed_dir, expected=None, root=None):
    """[(name, path)] of the agents whose installed file has a body that is not the kit's (the model line is not compared). An agent with no installed file, or no kit file, is not listed; an installed file that is there but cannot be read is."""
    out = []
    if installed_dir is None:
        return out
    for name in sorted(expected or EXPECTED_AGENTS):
        found = agent_definition(name, root)
        if found is None:
            continue
        installed = Path(installed_dir) / (name + ".md")
        try:
            kit = agent_body(Path(found[2]).read_text())
            current = agent_body(installed.read_text())
        except FileNotFoundError:
            continue   # nothing installed under that name: nothing to compare
        except (OSError, ValueError):
            out.append((name, str(installed)))   # present but unreadable: said as out of date, never passed over
            continue
        if kit != current:
            out.append((name, str(installed)))
    return out


def agents_problems(found, expected=None, root=None, unchecked=None):
    """One line per agent of ours that is missing, in the wrong mode, short of a rule its definition declares, or whose definition has lines
    the check cannot read. `unchecked` (a list, when given) collects the agents whose rules were NOT compared (no definition found): only the mode was."""
    problems = []
    for name, mode in sorted((expected or EXPECTED_AGENTS).items()):
        if name not in found:
            problems.append("%s: not registered (install it, then restart the service)" % name)
            continue
        got_mode, registered = found[name]
        if got_mode != mode:
            problems.append("%s: registered as %s, expected %s (its front matter probably failed to parse: quote any description with a colon)" % (name, got_mode, mode))
            continue
        definition = agent_definition(name, root)
        if definition is None:
            if unchecked is not None:
                unchecked.append(name)   # no definition to compare with: the mode was checked, the rules were not
            continue
        declared, unread, path = definition
        if unread:
            problems.append("%s: %d lines of the permission block in %s are not in a form this check reads, so they were not verified" % (name, unread, path))
        missing = [rule for rule in declared if rule not in registered]
        if missing:
            problems.append("%s: %d of its %d rules (from %s) are not registered as written, for example %s %s %s (its permission block did not load as written)"
                            % (name, len(missing), len(declared), path, missing[0][0], missing[0][1], missing[0][2]))
    return problems


def charters_report(sb_engine, registry_module, now):
    """#969: per orchestrator, where its charter is recorded: the registry row's `charter`, and the live session metadata's `charter` and `issue` with the session's start time. Read-only.
    `metadata_names_other_charter` lists the numbers its session metadata (`charter` or `issue`) names that the registry gives to ANOTHER orchestrator: before #969 the lane lookup counted such an orchestrator as a second owner; now the metadata is ignored and this line is the defect report.
    Returns ([{"name", "session", "started", "repo", "registry_charter", "meta_charter", "meta_issue", "metadata_names_other_charter": [{"charter", "field", "owner"}]}], the registry's problem line or None)."""
    discovery = sb_engine.discover(now)
    reg = registry_module.read(sb_engine.state_dir)
    rows = []
    for name, obs in sorted(discovery.agents.items()):
        fleet = obs.fleet or {}
        if fleet.get("role") != "orchestrator":
            continue
        row = reg["agents"].get(name) if reg["whole"] else None
        rows.append({"name": name, "session": obs.session_id, "started": iso(obs.created) if obs.created else None, "repo": fleet.get("repo"),
                     "registry_charter": row.get("charter") if isinstance(row, dict) else None,
                     "meta_charter": fleet.get("charter"), "meta_issue": fleet.get("issue"), "metadata_names_other_charter": []})
    for row in rows:
        for field in ("charter", "issue"):
            value = row["meta_" + field]
            for other in rows:
                if other is not row and isinstance(value, int) and not isinstance(value, bool) and value == other["registry_charter"] and other["repo"] == row["repo"]:
                    row["metadata_names_other_charter"].append({"charter": value, "field": field, "owner": other["name"]})
    return rows, (None if reg["whole"] else reg["problem"] or "registry not read")


def cmd_agents_charters(args, runner, now):
    """`agents charters [--json]`: read-only. For every live orchestrator: its registry charter, and the live session metadata's `charter` and `issue` with the session's start time, so a charter number
    recorded for the wrong orchestrator is seen with the record that carries it (#969)."""
    engine, _, _ = build_engine(runner, now, "cli", writable=False)
    registry = load_module("registry")
    rows, problem = charters_report(engine, registry, now())
    reg = registry.read(engine.state_dir)
    duplicates = registry.duplicate_charters(reg["agents"]) if reg["whole"] else {}
    if args.json:
        print(json.dumps({"orchestrators": rows, "registry_problem": problem, "duplicates": [{"repo": r, "charter": n, "names": names} for (r, n), names in sorted(duplicates.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))]}, sort_keys=True))
        return EXIT_OK
    if problem:
        print("registry: %s" % problem)
    for r in rows:
        print("%s (session %s, started %s, repo %s): registry charter %s; session metadata charter %s, issue %s" % (
            r["name"], r["session"], r["started"] or "unknown", r["repo"], r["registry_charter"], r["meta_charter"], r["meta_issue"]))
        for claim in r["metadata_names_other_charter"]:
            print("  session metadata disagrees with the registry: its `%s` is %s, a charter the registry gives to %s (the lane lookup ignores the session metadata)" % (claim["field"], claim["charter"], claim["owner"]))
    for (crepo, number), names in sorted(duplicates.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        print("charter #%s of %s is recorded by %d orchestrators in the registry: %s" % (number, crepo, len(names), ", ".join(names)))
    return EXIT_OK


def cmd_agents(args, runner, now):
    """`agents verify`: read-only; exit 0 when every agent of ours is registered as written or the service is not running, 1 otherwise."""
    if args.agents_action == "charters":
        return cmd_agents_charters(args, runner, now)
    state, payload = agents_found(runner, args.opencode, args.timeout)
    if state == "down":
        print("agents: the OpenCode service is not running or not reachable, so nothing was checked (%s). Start it, then run this again." % payload)
        return EXIT_ERROR if args.strict else EXIT_OK
    if state == "bad":
        sys.stderr.write("%s: agents verify: %s\n" % (PROG, payload))
        return EXIT_ERROR
    unchecked = []
    problems = agents_problems(payload, unchecked=unchecked)
    installed_dir = Path(args.installed_dir) if args.installed_dir else default_installed_agents_dir()
    for name, path in stale_installed_agents(installed_dir):
        problems.append("%s: out of date (the installed copy %s has a different wording from the kit's); run `%s install`, then restart the service" % (name, path, PROG))
    if problems:
        for line in problems:
            print("FAIL  " + line)
        print("agents: %d problems among %d agents" % (len(problems), len(EXPECTED_AGENTS)))
        return EXIT_ERROR
    if unchecked:
        print("agents: PASS on mode only for %d of %d (%s): their definitions were not found beside this binary, so their rules were not compared"
              % (len(unchecked), len(EXPECTED_AGENTS), ", ".join(unchecked)))
        return EXIT_ERROR if args.strict else EXIT_OK
    print("agents: PASS (%d of %d registered with the expected mode and every rule their definitions declare; rules loaded, not proof they take effect: the last match wins)" % (len(EXPECTED_AGENTS), len(EXPECTED_AGENTS)))
    return EXIT_OK


def add_agents_parsers(sub):
    agents = sub.add_parser("agents", help="Check that the service registered the kit's agents as written (read-only)")
    agents_sub = agents.add_subparsers(dest="agents_action", required=True)
    verify = agents_sub.add_parser("verify", help="Ask the running OpenCode service for its agents and compare each of ours with its expected mode and the rules its definition declares",
                                   description="Reads `opencode api GET /api/agent` and fails (exit 1) when an agent of the kit is missing, has the wrong mode or "
                                               "lacks a rule its definition declares. A service that is not running is reported, not failed. Run it after installing the agents and "
                                               "restarting the service.")
    charters = agents_sub.add_parser("charters", help="Read-only: per live orchestrator, its registry charter and its live session metadata's charter and issue (where a charter number is recorded)")
    charters.add_argument("--json", action="store_true")
    verify.add_argument("--strict", action="store_true", help="Exit 1 when the service cannot be asked (default: say so and exit 0)")
    verify.add_argument("--installed-dir", default=None, help="The folder of installed agent files to compare bodies with (default: the private profile's agents folder)")
    verify.add_argument("--opencode", default="opencode", help="The opencode executable (default: opencode on PATH)")
    verify.add_argument("--timeout", type=int, default=AGENTS_VERIFY_TIMEOUT, help="Seconds to wait for the service (default %d)" % AGENTS_VERIFY_TIMEOUT)


# ---------------------------------------------------------------- `held-ci` (#1970): the held pull requests' CI tip, and a refresh of ONE on request
#
# TEMPORARY HOME: this command is inlined here until #1092 moves commands to their own modules (the Chief of Staff's ruling: no new flat module).
# Cause: a pull request's CI runs on the merge commit GitHub builds from its head and the base tip AT THE TIME OF THE RUN. A re-run keeps that merge
# commit; only a close and reopen (or a push) builds a new one. So after every merge into the base, the green of every held pull request was computed on an
# older tip. `held-ci` shows that tip (read-only); `held-ci --refresh <pr>` closes and reopens ONE pull request, nothing refreshes by itself.
HELD_CI_PAGES = 100   # pages of 100 read before the list is called cut: a bound only a list that never ends reaches (the shared cap of 5 would cut 600 silently)


def _held_ci_read(hub, path, key=None):
    return _records(hub.gh_api(path, paginate=False), key)


def held_ci_pull(hub, repo, number):
    """The pull request as one read: (head, sorted labels, draft, state, base ref), or None when it is not there."""
    try:
        values = hub.gh_api("repos/%s/pulls/%d" % (repo, number), paginate=False)
    except Exception:
        return None
    for record in values:
        if isinstance(record, dict) and "head" in record:
            return held_ci_facts(record)
    return None


def held_ci_facts(record):
    labels = sorted(str(l.get("name")) for l in record.get("labels") or [] if isinstance(l, dict))
    return {"head": (record.get("head") or {}).get("sha"), "labels": labels, "draft": bool(record.get("draft")),
            "state": record.get("state"), "base": (record.get("base") or {}).get("ref")}


def held_ci_run_tip(hub, repo, head):
    """The base tip the latest run of `head` was computed on: the `base.sha` of the pull request the run records. None when there is no run."""
    runs = _held_ci_read(hub, "repos/%s/actions/runs?event=pull_request&per_page=1&head_sha=%s" % (repo, head), "workflow_runs")
    for run in runs[:1]:
        for pr in run.get("pull_requests") or []:
            sha = (pr.get("base") or {}).get("sha")
            if sha:
                return sha
    return None


def held_ci_state(hub, repo, base, run_tip, tip):
    """`current`, `stale: N commits behind`, or why it is not known."""
    if run_tip is None:
        return "no run"
    if run_tip == tip:
        return "current"
    for found in hub.gh_api("repos/%s/compare/%s...%s" % (repo, run_tip, tip), paginate=False):
        if isinstance(found, dict) and isinstance(found.get("ahead_by"), int):
            return "stale: %d commits behind" % found["ahead_by"]
    return "stale: unknown commits behind"


def held_ci_list(engine, hub, repo, held_label):
    records, cut = hub.read_pages("repos/%s/pulls?state=open&per_page=100" % repo, cap=HELD_CI_PAGES)
    held = [r for r in records if held_label in held_ci_facts(r)["labels"]]
    tips = {}
    for record in sorted(held, key=lambda r: r.get("number") or 0):
        facts = held_ci_facts(record)
        base = facts["base"]
        if base not in tips:
            tips[base] = next((f["commit"]["sha"] for f in hub.gh_api("repos/%s/branches/%s" % (repo, base), paginate=False)
                               if isinstance(f, dict) and isinstance(f.get("commit"), dict)), None)
        run_tip = held_ci_run_tip(hub, repo, facts["head"])
        print("%s#%s  head %s  run computed on tip %s  %s" % (repo, record.get("number"), str(facts["head"])[:8], str(run_tip)[:8] if run_tip else "none",
                                                              held_ci_state(hub, repo, base, run_tip, tips[base])))
    print("%d of %d held pull requests listed (%s)" % (len(held), len(held), held_label))
    if cut:
        print("list cut at %d pages: the open pull requests after page %d are missing; the count above is of the pages read" % (HELD_CI_PAGES, HELD_CI_PAGES))
        return EXIT_ERROR
    return EXIT_OK


def held_ci_refresh(engine, runner, hub, repo, number, held_label):
    before = held_ci_pull(hub, repo, number)
    if before is None:
        sys.stderr.write("%s: held-ci: %s#%d cannot be read; nothing was changed\n" % (PROG, repo, number))
        return EXIT_ERROR
    if before["state"] != "open":
        sys.stderr.write("%s: held-ci: %s#%d is not open; nothing was changed\n" % (PROG, repo, number))
        return EXIT_USAGE
    if before["draft"]:
        sys.stderr.write("%s: held-ci: %s#%d is a draft; a draft is not refreshed, nothing was changed\n" % (PROG, repo, number))
        return EXIT_USAGE
    if held_label not in before["labels"]:
        sys.stderr.write("%s: held-ci: %s#%d is not held (no %s label); nothing was changed\n" % (PROG, repo, number, held_label))
        return EXIT_USAGE
    path = "repos/%s/pulls/%d" % (repo, number)
    try:
        _gh_write(engine, runner, repo, path, "PATCH", {"state": "closed"})
    except IntentError as err:
        sys.stderr.write("%s: held-ci: %s#%d: the close failed (%s); nothing was changed\n" % (PROG, repo, number, _one_line(str(err), 200)))
        return EXIT_ERROR
    try:
        _gh_write(engine, runner, repo, path, "PATCH", {"state": "open"})
    except IntentError as err:   # the close is done: the line is written even so
        engine.audit.append(engine.actor, "held-ci.refresh", "%s#%d" % (repo, number), {"head": before["head"], "outcome": "closed, not reopened"})
        sys.stderr.write("%s: held-ci: %s#%d is CLOSED: the reopen failed (%s). Reopen it: gh api %s --method PATCH -f state=open\n"
                         % (PROG, repo, number, _one_line(str(err), 200), path))
        return EXIT_ERROR
    outcome = "reopened"
    after = held_ci_pull(hub, repo, number)
    engine.audit.append(engine.actor, "held-ci.refresh", "%s#%d" % (repo, number), {"head": before["head"], "outcome": outcome})
    if after is None or after["head"] != before["head"]:
        print("held-ci: %s#%d refreshed, but head changed: %s -> %s" % (repo, number, before["head"], after and after["head"]))
        return EXIT_ERROR
    if after["labels"] != before["labels"]:
        print("held-ci: %s#%d refreshed, but labels changed: %s -> %s" % (repo, number, before["labels"], after["labels"]))
        return EXIT_ERROR
    print("held-ci: %s#%d closed and reopened; head and labels unchanged (head %s)" % (repo, number, str(before["head"])[:8]))
    return EXIT_OK


def cmd_held_ci(args, runner, now):
    """`held-ci`: read-only list of the held pull requests with the tip of their latest run; `--refresh <pr>` closes and reopens ONE."""
    refresh = args.refresh or []
    if len(refresh) > 1:
        sys.stderr.write("%s: held-ci: --refresh names exactly one pull request, got %d; nothing was changed\n" % (PROG, len(refresh)))
        return EXIT_USAGE
    engine, state, config = build_engine(runner, now, "cli", writable=bool(refresh))
    repo = args.repo or config.get("intent_repo")
    if not repo or not REPO_RE.match(repo):
        sys.stderr.write("%s: held-ci: name the repository with --repo OWNER/REPO\n" % PROG)
        return EXIT_USAGE
    hub = GitHubHub(config, state, None, runner, spawn_forwarder, now, "cli")
    hub.engine, engine.hub = engine, hub
    held_label = label_names(config)["merge-captain"]
    try:
        if refresh:
            return held_ci_refresh(engine, runner, hub, repo, refresh[0], held_label)
        return held_ci_list(engine, hub, repo, held_label)
    except GitHubUnavailable as err:
        sys.stderr.write("%s: held-ci: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_ERROR


def add_held_ci_parsers(sub):
    held = sub.add_parser("held-ci", help="List the held pull requests with the tip their latest CI run was computed on; --refresh <pr> closes and reopens one",
                          description="Read-only: for each open pull request labelled merge-captain, its head, the base tip its latest run was computed on, and `current` or "
                                      "`stale: N commits behind`. With --refresh <pr> (exactly one, never a draft, never a pull request that is not held) it closes and reopens that "
                                      "pull request so GitHub builds a new merge commit, checks that the head and the labels are unchanged, and writes one audit line. "
                                      "Refresh only a pull request that is about to go to the owner. Nothing refreshes by itself.")
    held.add_argument("--repo", default=None, help="OWNER/REPO (default: the intent repo)")
    held.add_argument("--refresh", type=int, nargs="+", metavar="PR", default=None, help="Close and reopen this ONE pull request")

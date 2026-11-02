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


# ---------------------------------------------------------------- `ci-failures` (#1968): why did CI fail, for a pod whose token cannot read the Actions API?
# Read-only, lane-side: the lane's own account reads the runs and jobs (the counted `read` of the account layer, GET only) and prints one line per failed job.
# Per call: 1 (the pull request) + 1 (the runs) + R (the jobs of each run) + at most CI_FAILURES_MAX_LOGS (one log per failed job). This home is temporary until #1092.
CI_FAILURES_MAX_LOGS = 5
CI_FAILURES_LOG_CHARS = 200000
CI_FAILURES_MAX_NAMES = 10
CI_FAILURES_NAME_CHARS = 200
_CI_FAILED = ("failure", "timed_out", "startup_failure")
_CI_QUIET = ("success", "skipped", "neutral")
_CI_OPEN = ("queued", "in_progress", "waiting", "pending", "requested")
_CI_TEST_LINE = re.compile(r"^(?:\S+Z )?(?:FAIL|ERROR): (.+?)\s*$", re.M)
_CI_TOKEN = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}")


def ci_failure_names(log):
    """The failing test names of a unittest log (`FAIL: test_x (module.Class)` lines), in order, no repeats, cut to the bound; the names are not scrubbed here."""
    names = []
    for found in _CI_TEST_LINE.findall(log[-CI_FAILURES_LOG_CHARS:]):
        if found not in names:
            names.append(found)
    return [n[:CI_FAILURES_NAME_CHARS] for n in names]


def ci_failure_lines(reading, gh, repo, number, clean):
    """The plain lines `ci-failures` prints for the pull request. Raises reading.ReadError when the pull request, the runs or a job list cannot be read."""
    pr = reading._json(gh, ["api", "repos/%s/pulls/%d" % (repo, number)], "pull request %s#%d" % (repo, number))
    head = ((pr.get("head") or {}).get("sha")) if isinstance(pr, dict) else None
    if not isinstance(head, str) or len(head) != 40:
        raise reading.ReadError("pull request %s#%d has no readable head" % (repo, number))
    runs = reading.read_pr_runs(gh, repo, number, head)
    if not runs:
        return ["no run at head %s: nothing to read" % head[:7]]
    lines, unread, failed, going, logs = [], 0, 0, [], 0
    for run in runs:
        data = reading._json(gh, ["api", "repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100" % (repo, run["id"])], "the jobs of run %s" % run["id"])
        jobs = [j for j in (data.get("jobs") if isinstance(data, dict) else None) or [] if isinstance(j, dict)]
        if run["status"] != "completed":
            going.append(run["workflow"])
        for job in jobs:
            name, conclusion, status = str(job.get("name")), job.get("conclusion"), job.get("status")
            if conclusion in _CI_QUIET or (not conclusion and status in _CI_OPEN):
                continue
            if conclusion not in _CI_FAILED:
                lines.append("job %s: conclusion unknown (%s), not called failed" % (name, conclusion or status or "none"))
                continue
            failed += 1
            step = next((str(s.get("name")) for s in job.get("steps") or [] if isinstance(s, dict) and s.get("conclusion") in _CI_FAILED), "no failed step named")
            if logs >= CI_FAILURES_MAX_LOGS:
                lines.append("FAILED job %s, step %s: log not read (the cap of %d logs is reached)" % (name, step, CI_FAILURES_MAX_LOGS))
                continue
            logs += 1
            code, out, _ = gh(["api", "repos/%s/actions/jobs/%s/logs" % (repo, job.get("id"))])
            if code != 0:
                lines.append("FAILED job %s, step %s: log not readable" % (name, step))
                continue
            names = ci_failure_names(out)
            shown = names[:CI_FAILURES_MAX_NAMES]
            tests = ("tests: " + "; ".join(shown) + (" (+%d more)" % (len(names) - len(shown)) if len(names) > len(shown) else "")) if shown else "no test names found"
            lines.append("FAILED job %s, step %s: %s" % (name, step, tests))
    for workflow in going:
        lines.append("still going: the run of %s has not finished, so this may not be everything" % workflow)
    if not failed and not lines:
        lines.append("no failure at head %s" % head[:7])
    return [clean(line) for line in lines]


def cmd_ci_failures(args, runner, now):
    """`ci-failures <pr>`: read-only. For the latest run at the head of the pull request, one line per failed job (its failed step and the failing test names read from its log), or one plain line."""
    reading = load_module("pr_reading")
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        raise LedgerCommandError(str(err)) from None
    config = load_runnable_config()
    accounts = GhAccounts(config, runner, now)

    def gh(argv):
        try:
            done = accounts.read(runner, argv[1], repo, config["call_timeout_seconds"], stdin=subprocess.DEVNULL)
        except (AccountError, GhRateLimited, OSError, subprocess.SubprocessError) as err:
            return 1, "", _one_line(str(err), 120)
        return done.returncode, done.stdout or "", accounts.scrub(done.stderr or "")

    def clean(line):
        return _CI_TOKEN.sub("[redacted]", redact_command_text(accounts.scrub(line)))[:400]
    try:
        lines = ci_failure_lines(reading, gh, repo, number, clean)
    except reading.ReadError as err:
        print(clean("ci-failures: %s" % err), file=sys.stderr)
        return EXIT_ERROR
    for line in lines:
        print(line)
    return EXIT_OK


def add_ci_failures_parsers(sub):
    sub.add_parser("ci-failures", help="Read-only: why CI failed at a pull request's head, one line per failed job (for a brief; a pod's token cannot read the Actions API)",
                   description="Reads the latest run at the head of the pull request with the lane's own account (GET only) and prints one line per failed job: its failed step and "
                               "the failing test names from its log (at most 5 logs, bounded and scrubbed), or one plain line when the run is still going or has no failure.").add_argument(
        "pr", help="The pull request: owner/repo#N or its URL")

# ---------------------------------------------------------------- `job`: a long run that survives being asked about, and says when it did not survive
#
# A restart of the service (or the pane) can silently kill a coder's background suite, and the coder keeps saying "running" for hours. `job start`
# runs a command detached in its own session and records it under the shared temp dir; the wrapper writes the exit code to a file when the command
# ends, so the three states are told apart by evidence: an exit file (finished, with its code), a live process that is the one that was started
# (alive), neither (dead: killed, no exit code). A dead job its starter has not looked at is reported, once, to the starter's boss (JobFacts).
JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
JOB_DEAD = "job.dead"
JOBS_DIR_ENV = "FLEET_JOBS_DIR"
# The wrapper runs the command in the background and waits, so that a signal it can catch (TERM, HUP, INT) is handled at once: it writes the reason ("signal N") and the
# exit code 128+N, passes the stop on to the command (INT as TERM: a background command ignores INT) and, only once the command is gone, ends (a command that outlives the stop keeps the job alive: the record is never false). A signal that cannot be caught leaves neither file: the daemon says "unknown".
JOB_WRAPPER = ('exit_path=$1; shift; '
               'finish() { printf "%s\\n" "$1" > "$exit_path.tmp" && mv "$exit_path.tmp" "$exit_path"; }; '
               'caught() { printf "signal %s\\n" "$1" > "$exit_path.reason"; kill -"${2:-$1}" "$child" 2>/dev/null; wait "$child"; finish "$((128 + $1))"; exit "$((128 + $1))"; }; '
               'trap "caught 15" TERM; trap "caught 1" HUP; trap "caught 2 15" INT; '
               'missing() { printf "fleet-job: %s: %s\\n" "$1" "$2" >&2; finish "$3"; exit "$3"; }; '
               'case "$1" in */*) [ -e "$1" ] || missing "$1" "No such file or directory" 127; { [ -x "$1" ] && [ ! -d "$1" ]; } || missing "$1" "Permission denied" 126;; '
               '*) command -v "$1" >/dev/null 2>&1 || missing "$1" "command not found" 127;; esac; '
               'exec 3>&2 2>/dev/null; "$@" 2>&3 3>&- & child=$!; wait "$child"; code=$?; finish "$code"; exit "$code"')
JOB_ENDED_MARK = "ended-recorded"   # in the job's directory: the one audit line of how it ended is written once
JOB_LAST_SEEN_MAX = 160
JOB_TAIL_BYTES = 4096


JOB_SCAN_MAX = 200   # the most job directories one daemon pass looks at


def jobs_dir():
    return Path(os.environ.get(JOBS_DIR_ENV) or os.path.join(tempfile.gettempdir(), "fleet-jobs"))


def job_private(path):
    """True when `path` is a directory owned by this user that nobody else can enter or write: a shared temp dir is where another user could
    plant a record, so a jobs dir that is not private is neither written nor believed."""
    try:
        info = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def job_ensure_root(root):
    """Create the jobs dir private (0700). Raises OSError when it exists and is not private."""
    root = Path(root)
    try:
        os.makedirs(str(root), mode=0o700, exist_ok=True)
    except OSError:
        pass
    if not job_private(root):
        raise OSError(errno.EPERM, "%s is not a private directory (owned by you, mode 0700)" % root)


def job_write_atomic(path, text):
    """The file whole or not at all (a reader never sees half of it): a temp file beside it, renamed into place."""
    temp = Path("%s.%d.tmp" % (path, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def job_paths(name, root=None):
    base = Path(root or jobs_dir()) / name
    return {"dir": base, "record": base / "job.json", "log": base / "out.log", "exit": base / "exit", "collected": base / "collected", "reason": base / "exit.reason", "marker": base / JOB_ENDED_MARK}


def job_process_token(pid):
    """What identifies THIS process among later ones with the same pid: its start time as ps prints it, or None when it cannot be read."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = (done.stdout or "").strip()
    return text if done.returncode == 0 and text else None


def job_read(name, root=None):
    paths = job_paths(name, root)
    try:
        if os.lstat(str(paths["record"])).st_uid != os.getuid():
            return None   # a record someone else wrote is not believed
        record = json.loads(paths["record"].read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("name") == name else None


def job_state(record, root=None):
    """("finished", exit code) | ("alive", None) | ("dead", None) | ("unknown", None): by evidence, never by a guess."""
    paths = job_paths(record["name"], root)
    try:
        code = int(paths["exit"].read_text().strip())
        return "finished", code
    except (OSError, ValueError):
        pass
    pid = record.get("pid")
    if not isinstance(pid, int) or pid < 2:
        return "unknown", None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        return "alive", None   # it exists, owned by someone else: not ours to call dead
    token = record.get("token")
    now_token = job_process_token(pid)
    if token and now_token and now_token != token:
        return "dead", None    # the pid was reused by another process: ours is gone
    return "alive", None


def boot_time():
    """When the machine last started, in epoch seconds, or None when it cannot be read (Linux /proc/stat, else the BSD/macOS `sysctl kern.boottime`)."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        done = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        found = re.search(r"sec\s*=\s*([0-9]+)", done.stdout or "")
        return float(found.group(1)) if done.returncode == 0 and found else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def job_last_seen(record, root, now):
    """(the last non-empty line the job printed, how many seconds ago) with control characters turned into spaces and the line bounded; ("", None) when it printed nothing
    or the log cannot be read. A log line is untrusted text."""
    path = job_paths(record["name"], root)["log"]
    try:
        with open(str(path), "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - JOB_TAIL_BYTES))
            tail = handle.read(JOB_TAIL_BYTES).decode("utf-8", "replace")
            age = max(0, int(now - os.fstat(handle.fileno()).st_mtime))
    except OSError:
        return "", None
    lines = [line for line in re.split(r"[\r\n]+", tail) if line.strip()]
    return (_plain(lines[-1], JOB_LAST_SEEN_MAX).strip() if lines else ""), (age if lines else None)


def job_end_reason(record, root, now, boot=None):
    """How a job that ended without finishing normally ended, as the audit line tells it, or None when it is alive, unknown or finished without a signal. The reason is
    what can be told: "signal N" (the wrapper caught it and wrote it), "host rebooted" (the machine started after the job did), else "unknown" (an uncatchable signal;
    its sender cannot be known). Never the command's arguments or environment."""
    state, code = job_state(record, root)
    paths = job_paths(record["name"], root)
    started = float(record.get("started") or 0)
    if state == "finished":
        try:
            said = _plain(paths["reason"].read_text().strip(), 40)
        except OSError:
            return None
        if not re.fullmatch(r"signal [0-9]{1,3}", said):
            return None
        reason = said
    elif state == "dead":
        booted = (boot or boot_time)()
        reason = "host rebooted" if booted is not None and started and booted > started else "unknown"
    else:
        return None
    seen, age = job_last_seen(record, root, now)
    detail = {"reason": reason, "age_seconds": max(0, int(now - started)), "last_seen": seen, "last_seen_age_seconds": age, "source": "job"}
    if code is not None:
        detail["exit_code"] = code
    return detail


def job_record_end(record, root, now, audit, boot=None):
    """The ONE audit line `job.dead` of a job that ended without finishing normally, with its reason and last-seen line; a marker file in the job's directory makes it once
    whoever looks first (the daemon or `job status`). True when this call wrote it. A sink that fails never raises."""
    paths = job_paths(record["name"], root)
    detail = job_end_reason(record, root, now, boot)
    if detail is None:
        return False
    try:
        fd = os.open(str(paths["marker"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except OSError:
        return False   # another look wrote it, or the directory is not writable
    try:
        audit(JOB_DEAD, record["name"], detail)
    except Exception as err:
        try:
            paths["marker"].unlink()   # nothing was written: the next look tries again
        except OSError:
            pass
        sys.stderr.write("%s: job: could not write the audit line %s (%s)\n" % (PROG, JOB_DEAD, _plain("%s: %s" % (type(err).__name__, err), 120)))
    return True


def job_age(seconds):
    seconds = max(0, int(seconds))
    return "%ds" % seconds if seconds < 120 else "%dm" % (seconds // 60) if seconds < 7200 else "%dh" % (seconds // 3600)


def job_line(record, state, code, now, root=None):
    paths = job_paths(record["name"], root)
    age = job_age(now - float(record.get("started") or now))
    if state == "alive":
        return "alive: %s pid %s, running %s; log %s" % (record["name"], record.get("pid"), age, paths["log"])
    if state == "finished":
        return "finished: %s exit code %d after it started %s ago; result %s" % (record["name"], code, age, paths["log"])
    if state == "dead":
        return "dead: %s was killed (no exit code); it started %s ago; what it printed is in %s" % (record["name"], age, paths["log"])
    return "unknown: %s has no process id on record" % record["name"]


def cmd_job(args, runner, now, spawn=subprocess.Popen):
    """`job start <name> -- <command...>` and `job status <name>`. Start is detached and prints one line; status is one line."""
    if not JOB_NAME_RE.match(args.name or ""):
        sys.stderr.write("%s: job: name must match %s, got %r\n" % (PROG, JOB_NAME_RE.pattern, args.name))
        return EXIT_USAGE
    root = jobs_dir()
    if args.job_action == "status":
        record = job_read(args.name, root)
        if record is None:
            sys.stderr.write("%s: job: no job named %s under %s\n" % (PROG, args.name, root))
            return EXIT_ERROR
        state, code = job_state(record, root)
        print(job_line(record, state, code, now(), root))
        try:
            job_record_end(record, root, now(), lambda event, subject, detail: AuditLog(ensure_state_dir() / "audit.jsonl").append("job", event, subject, detail))
        except Exception:
            pass   # `job status` never fails for the audit line's sake
        if state != "alive":   # whoever asked has now seen how it ended: it is collected, and nobody else is told
            try:
                job_write_atomic(job_paths(args.name, root)["collected"], str(int(now())))
            except OSError:
                pass
        return EXIT_OK if state == "alive" or (state == "finished" and code == 0) else EXIT_ERROR
    command = [part for part in (args.command or []) if part != "--"] if args.command and args.command[0] == "--" else list(args.command or [])
    if not command:
        sys.stderr.write("%s: job start: give the command after --, e.g. `job start suite -- bin/test-switchboard`\n" % PROG)
        return EXIT_USAGE
    try:
        job_ensure_root(root)
    except OSError as err:
        sys.stderr.write("%s: job: %s\n" % (PROG, err.strerror or err))
        return EXIT_ERROR
    paths = job_paths(args.name, root)
    lock = os.open(str(root / (args.name + ".lock")), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)   # the check and the start are one step: two starts of one name cannot both pass
        earlier = job_read(args.name, root)
        if earlier is not None and job_state(earlier, root)[0] == "alive":
            sys.stderr.write("%s: job: %s is still running (pid %s): `job status %s`; pick another name or wait\n" % (PROG, args.name, earlier.get("pid"), args.name))
            return EXIT_ERROR
        paths["dir"].mkdir(parents=True, exist_ok=True)
        os.chmod(str(paths["dir"]), 0o700)
        for stale in (paths["record"], paths["exit"], paths["collected"], paths["reason"], paths["marker"], Path(str(paths["exit"]) + ".tmp")):   # the old record goes FIRST: no window shows it uncollected
            try:
                stale.unlink()
            except OSError:
                pass
        log = os.open(str(paths["log"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            process = spawn(["/bin/sh", "-c", JOB_WRAPPER, "fleet-job", str(paths["exit"])] + command, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, cwd=os.getcwd())
        except OSError as err:
            sys.stderr.write("%s: job: cannot start %s: %s\n" % (PROG, command[0], err.strerror or err))
            return EXIT_ERROR
        finally:
            os.close(log)
        session, starter = job_starter(runner, now)
        record = {"name": args.name, "argv": command, "pid": process.pid, "started": now(), "token": job_process_token(process.pid),
                  "cwd": os.getcwd(), "session": session, "starter": starter}
        job_write_atomic(paths["record"], json.dumps(record, indent=1, sort_keys=True) + "\n")
    finally:
        os.close(lock)
    print("started: %s pid %d; `%s job status %s` says how it is; log %s" % (args.name, process.pid, PROG, args.name, paths["log"]))
    return EXIT_OK


def job_starter(runner, now):
    """(session, fleet name) of the agent that starts a job, established the way every command does it (its OWN session, known to v2 as a
    fleet agent), or (None, None): a job nobody can vouch for is never reported to anyone's boss."""
    try:
        engine, _, _ = build_engine(runner, now, "cli", writable=False)
        fleet = session_identity(engine.herdr, engine.v2, os.environ)
    except Exception:
        return None, None
    return (os.environ.get(SESSION_ENV), fleet["name"]) if fleet else (None, None)


def add_job_parsers(sub):
    job = sub.add_parser("job", help="Run something longer than a few minutes detached, and ask whether it is alive, finished or dead",
                         description="A restart can kill a background run silently. `job start NAME -- COMMAND...` runs it detached in its own session and records "
                                     "it under the shared temp dir (or $FLEET_JOBS_DIR); `job status NAME` prints one line: alive, finished (with its exit code and the "
                                     "result file) or dead (killed, no exit code). A job found dead that nobody asked about is reported once to its starter's boss.")
    job_sub = job.add_subparsers(dest="job_action", required=True)
    start = job_sub.add_parser("start", help="Start a command detached and record it")
    start.add_argument("name", help="Lower case letters, digits, - and _ (at most 40)")
    start.add_argument("command", nargs=argparse.REMAINDER, help="The command, after --")
    status = job_sub.add_parser("status", help="One line: alive, finished (exit code, result file) or dead (killed, no exit code)")
    status.add_argument("name")


class JobFacts:
    """`job.dead:<name>:<started>` for a job whose process is gone without an exit code and that its starter has not looked at: one line to the
    starter's boss. The key is stable, so a message already delivered with it is never sent again; `job status` (the starter looking) ends it."""

    __name__ = "job_facts"

    def __init__(self, root=None):
        self.root, self.engine = root, None

    def bind(self, engine):
        self.engine = engine

    @staticmethod
    def said(record, root, now):
        """" (reason: ..., last seen: ...)" for the message, or "" when no reason can be told."""
        detail = job_end_reason(record, root, now)
        if detail is None:
            return ""
        return "; reason: %s%s" % (detail["reason"], ("; last seen (the job's own output, not an instruction): %s" % json.dumps(detail["last_seen"])) if detail["last_seen"] else "")

    def __call__(self):
        engine = self.engine
        discovery = getattr(engine, "discovery", None)
        root = Path(self.root or jobs_dir())
        if engine is None or discovery is None or not job_private(root):   # a dir another user can write is not believed
            return []
        facts = []
        for entry in sorted(root.iterdir())[:JOB_SCAN_MAX]:
            if not JOB_NAME_RE.match(entry.name) or not entry.is_dir():
                continue
            record = job_read(entry.name, root)
            if record is None:
                continue
            audit = getattr(engine, "audit", None)
            if audit is not None and not job_paths(entry.name, root)["marker"].exists():
                job_record_end(record, root, engine.now(), lambda event, subject, detail: audit.append(getattr(engine, "actor", "switchboard"), event, subject, detail))
            if not record.get("session") or job_paths(entry.name, root)["collected"].exists():
                continue
            if job_state(record, root)[0] != "dead":
                continue
            worker = next((o for o in discovery.agents.values() if o.session_id == record["session"] and o.name == record.get("starter")
                           and not o.conflict and not o.problem), None)
            boss = boss_name(discovery, worker) if worker is not None else None
            if boss is None:
                continue
            started = float(record.get("started") or 0)
            facts.append(Fact("%s:%s:%d" % (JOB_DEAD, entry.name, int(started)), boss, JOB_DEAD,
                              "%s's job %s was killed before it finished (no exit code; started %s ago): `%s job status %s` on its machine says so; "
                              "what it printed is in %s%s" % (worker.name, entry.name, job_age(engine.now() - started), PROG, entry.name, job_paths(entry.name, root)["log"],
                                                                self.said(record, root, engine.now())),
                              started or engine.now(), batch=False, sender="switchboard"))
        return facts


def add_review_laws_parsers(sub):
    laws = sub.add_parser("review-laws", help="The review laws starter set (#179)")
    laws_sub = laws.add_subparsers(dest="laws_action", required=True)
    install = laws_sub.add_parser("install", help="Copy the generic review laws and standards into a repository and say what to edit")
    install.add_argument("repo", help="Path of the repository (its root)")
    install.add_argument("--force", action="store_true", help="Replace copies the repository already has")


def add_plan_parsers(sub):
    plan = sub.add_parser("plan", help="Plan-driven development: check a plan's scenarios (gate G0a)")
    plan_sub = plan.add_subparsers(dest="plan_action", required=True)
    check = plan_sub.add_parser("check", help="Check a plan: scenarios well-formed, every Done-when clause covered; "
                                              "prints PASS or FAIL, one line per defect, and the plan's content hash")
    check.add_argument("source", nargs="?", help="owner/repo#N, an issue URL (a #issuecomment-ID picks that comment), "
                                                "or N with intent_repo set; the plan is the latest `Plan (scenarios)` "
                                                "comment, else the issue body")
    check.add_argument("--file", help="Read the plan from this file instead of GitHub")
    check.add_argument("--ask", help="The ask whose Done-when to cover when the plan issue has none of its own")
    check.add_argument("--done-when", dest="done_when", help="The Done-when line to cover (overrides the issue's)")
    check.add_argument("--json", action="store_true", help="Machine-readable result")
    check.add_argument("--explain", action="store_true", help="After each defect (and warning) say the rule it breaks and how to fix it")
    template = plan_sub.add_parser("template", help="Print a prefilled plan for an ask: its Done-when clauses and one scenario stub per clause (read-only)")
    template.add_argument("ask", nargs="?", help="owner/repo#N, an issue URL, or N with intent_repo set: the ask whose Done-when to prefill")
    template.add_argument("--done-when", dest="done_when", help="The Done-when line to prefill from, instead of reading the ask")
    review = plan_sub.add_parser("review", help="The plan review engine (off unless plan_review.enabled): adversarial lanes, at most 3 rounds")
    review_sub = review.add_subparsers(dest="review_action", required=True)
    start = review_sub.add_parser("start", help="Open a review of a plan for its ask and launch the lanes")
    start.add_argument("source", help="owner/repo#N of the plan")
    start.add_argument("--ask", required=True, help="owner/repo#N of the ask (or work item) the plan serves: the count belongs to it")
    start.add_argument("--tier", choices=("light", "normal", "full"), help="Ask for more review than the computed tier (never less)")
    start.add_argument("--json", action="store_true")
    verdict = review_sub.add_parser("verdict", help="(fallback) a lane records its verdict block for its own lane")
    verdict.add_argument("review")
    verdict.add_argument("--lane", required=True)
    verdict.add_argument("--file", help="The verdict block file (default stdin)")
    verdict.add_argument("--json", action="store_true")
    respond = review_sub.add_parser("respond", help="The planner answers a finished round: response table and the new revision")
    respond.add_argument("review")
    respond.add_argument("--plan-ref", dest="plan_ref", required=True, help="owner/repo#N of the revised plan")
    respond.add_argument("--table", required=True, help="File: `<lane>/<id>: accept|reject, why` for each open blocking objection")
    respond.add_argument("--json", action="store_true")
    rstatus = review_sub.add_parser("status", help="Rounds, lanes, open objections and the tally")
    rstatus.add_argument("review")
    rstatus.add_argument("--json", action="store_true")
    abandon = review_sub.add_parser("abandon", help="Clear a stalled open review (the round count continues)")
    abandon.add_argument("review")
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--json", action="store_true")
    decide = review_sub.add_parser("decide", help="Resolve an escalated objection: accept it, or override it with a reason")
    decide.add_argument("review")
    decide.add_argument("--objection", required=True, help="<lane>/<id> as the escalation names it")
    mode = decide.add_mutually_exclusive_group(required=True)
    mode.add_argument("--accept", action="store_true")
    mode.add_argument("--override", action="store_true")
    decide.add_argument("--reason", required=True)
    decide.add_argument("--json", action="store_true")
    close = review_sub.add_parser("close", help="The closing round: every lane confirms the final revision, then the consensus is recorded")
    close.add_argument("review")
    close.add_argument("--plan-ref", dest="plan_ref", required=True, help="owner/repo#N of the final revision (with a `## Decisions` section)")
    close.add_argument("--json", action="store_true")
    rreport = review_sub.add_parser("report", help="What the review cost and what the reviewers caught")
    rreport.add_argument("review")
    rreport.add_argument("--json", action="store_true")
    tick = review_sub.add_parser("tick", help="Housekeeping: relaunch a stuck lane once, then escalate it; remind an undecided escalation")
    tick.add_argument("--json", action="store_true")


POLICY_DEFAULTS = {
    "enabled": False,       # off until the plugin is installed and a `jev` object is configured
    "budget_ms": 1500,      # the whole judgement; no answer in time keeps the configured outcome. Jev alone ran to ~1 s in the trial
    "thresholds": POLICY_DEFAULT_THRESHOLDS,  # question id -> probability at which it fires; a configured entry wins per question
    "cache_seconds": 300,   # how long a verdict or a resolved ask is reused; 0 turns both caches off
    "cache_entries": 200,   # entries kept in each cache file
    "routine_filter": True, # issue 87: a read or a local reversible step needs no model (routine_local); false asks the model about every call
    "shadow": False,        # record what the judge would do (audit policy.judge, `shadow`/`verdict`), enforce nothing
}


DEFAULTS["policy"] = None   # None means POLICY_DEFAULTS


POLICY_CACHE_BYTES = 1 << 20


POLICY_CONTEXT_FILE = "policy-context.json"   # resolved asks and sessions: a disposable copy of what GitHub and v2 say


def validate_policy_config(config, source):
    """Check and complete `policy`: unknown subkeys are refused, every value is checked."""
    raw = config.get("policy")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'policy' must be an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(POLICY_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'policy' contains key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(POLICY_DEFAULTS)), 'policy', unknown, list(POLICY_DEFAULTS))
    settings = dict(POLICY_DEFAULTS, **raw)
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("%s: 'policy.enabled' must be true or false, got %r" % (source, settings["enabled"]))
    for name in ("budget_ms", "cache_seconds"):
        value = settings[name]
        if not _is_number(value) or value < 0 or (name == "budget_ms" and value <= 0):
            raise ConfigError("%s: 'policy.%s' must be a %s number, got %r"
                              % (source, name, "positive" if name == "budget_ms" else "non-negative", value))
    if not isinstance(settings["shadow"], bool):
        raise ConfigError("%s: 'policy.shadow' must be true or false, got %r" % (source, settings["shadow"]))
    entries = settings["cache_entries"]
    if not (isinstance(entries, int) and not isinstance(entries, bool) and 1 <= entries <= 5000):
        raise ConfigError("%s: 'policy.cache_entries' must be an integer from 1 to 5000, got %r" % (source, entries))
    if not isinstance(settings["routine_filter"], bool):
        raise ConfigError("%s: 'policy.routine_filter' must be true or false, got %r" % (source, settings["routine_filter"]))
    settings["thresholds"] = dict(POLICY_DEFAULT_THRESHOLDS, **_checked_policy_thresholds(settings["thresholds"], source))
    if settings["enabled"] and not config.get("jev"):
        raise ConfigError("%s: 'policy.enabled' needs a 'jev' object: there is no decision model to ask" % source)
    config["policy"] = settings


def _checked_policy_thresholds(thresholds, source):
    if not isinstance(thresholds, dict):
        raise ConfigError("%s: 'policy.thresholds' must be an object of question id -> probability" % source)
    for name, value in thresholds.items():
        if name not in POLICY_QUESTIONS:
            raise ConfigError("%s: 'policy.thresholds' names no question %r (known: %s)"
                              % (source, name, ", ".join(POLICY_QUESTIONS)))
        if not _is_number(value) or not 0 < value <= 1:
            raise ConfigError("%s: 'policy.thresholds.%s' must be a number above 0 and at most 1, got %r"
                              % (source, name, value))
    return dict(thresholds)


_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|auth)[A-Za-z0-9_.-]*)"
                r"(\s*[=:]\s*|\s+)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{8,})"), "[redacted]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[redacted]@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*"), "[redacted]"),   # a JWT (#773)
)


def redact_command_text(text, extra=()):
    """`text` with credential-shaped parts replaced, and any value in `extra` (a known key) removed.

    A tool's arguments go to a third-party model and to the audit log; neither
    may see a credential. This is a net, not a promise: it catches the usual shapes.
    """
    text = "" if text is None else str(text)
    for secret in extra:
        if secret:
            text = text.replace(secret, "[redacted]")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

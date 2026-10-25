# ---- #851: an open issue carrying a configured alert label wakes that repository's orchestrator once per issue and label. Reads only what the triage pass holds.

ALERT_WAKE_FILE = "alert-wake.json"   # {"<repo>#<number> <label>": {"at": epoch}}: what was already told (private; survives a restart)
ALERT_WAKES_PER_PASS = 5              # a burst is told in steps of this many issues; the rest is named in one line and follows on the next passes
ALERT_TITLE_CHARS = 100
ALERT_FACT_KEEP = 2 * 86400
ALERT_MEMORY_MAX = 5000
ALERT_KEY_RE = re.compile(r"^[^#\s]+#[0-9]+ \S.*$")
ALERT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class AlertWakeState:
    """What was told (`told`: key -> when) and the lines still waiting (`lines`: key -> {recipient: text}), both kept in ALERT_WAKE_FILE: a line is kept there
    until it is old enough (ALERT_FACT_KEEP) and loaded back as a fact at start, so a daemon that stops between queueing and delivery still delivers it. The
    fact key is stable, and a key already in the recipient's inbox is never delivered again, so re-queueing cannot repeat a delivered wake."""

    def __init__(self, state_dir, now=None):
        self.told, self.lines, self.facts, self.problem = {}, {}, {}, None
        try:
            with open(str(Path(state_dir) / ALERT_WAKE_FILE), "rb") as handle:
                raw = json.loads(handle.read(1000001).decode("utf-8"))   # a bounded read: the file is ours, but a runaway one must not be loaded whole
            if isinstance(raw, dict):
                for key, value in list(raw.items())[:ALERT_MEMORY_MAX]:
                    if not (isinstance(key, str) and ALERT_KEY_RE.match(key) and isinstance(value, dict) and _is_number(value.get("at"))):
                        continue   # a malformed key is dropped, never parsed later
                    self.told[key] = value["at"]
                    lines = value.get("lines")
                    if now is not None and isinstance(lines, dict) and now - value["at"] < ALERT_FACT_KEEP:
                        repo, _, rest = key.partition("#")
                        number = int(rest.split(" ", 1)[0])
                        self.lines[key] = {n: l for n, l in lines.items() if isinstance(n, str) and isinstance(l, str)}
                        for name, text in self.lines[key].items():
                            self.facts["alert:%s:%s" % (key, name)] = (text, value["at"], name, repo, number)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError) as err:
            self.problem = "alert-wake.json cannot be read, so alert issues may be told again: %s" % _one_line(type(err).__name__, 40)


def alert_wake_state(engine):
    state = getattr(engine, "alert_wake", None)
    if state is None:
        state = engine.alert_wake = AlertWakeState(engine.state_dir, engine.now())
        if state.problem:
            engine._note("alert_wake.error", "", {"error": state.problem})
    return state


def alert_line(repo, number, title, label):
    """The one line the orchestrator reads. The title is the issue author's text: one line, bounded, quoted, and data (never an instruction)."""
    return "alert issue %s#%d \"%s\" carries label %s" % (repo, number, _one_line(ALERT_CONTROL_RE.sub(" ", title), ALERT_TITLE_CHARS).replace('"', "'"), label)


def alert_wake_pass(engine, state, cfg, now):
    """Judges an input only when it was read whole: a repository whose issue list was not (cut, failed, never read) wakes nothing and forgets nothing,
    and neither does a pass whose roster of agents was not read whole."""
    configured = {r: list(l) for r, l in (cfg.get("alert_labels") or {}).items() if l}
    if not configured or engine.state_dir is None:
        return
    wake = alert_wake_state(engine)
    for old in [k for k, v in wake.facts.items() if now - v[1] > ALERT_FACT_KEEP]:
        del wake.facts[old]
    if whole_skipped(engine, "alert_wake", "agents"):
        return
    discovery, changed = getattr(engine, "discovery", None), False
    for repo, labels in sorted(configured.items()):
        if whole_skipped(engine, "alert_wake", "issues:" + repo):
            continue
        unit = state.units.get((repo, "issues"))
        if unit is None:
            continue
        open_numbers, pending = set(), []
        for record in unit["data"]:
            item = triage_norm(record)
            if item is None or item["pr"] or item["state"] != "open":
                continue
            open_numbers.add(item["number"])
            title = record.get("title") if isinstance(record.get("title"), str) else ""
            for label in labels:
                key = "%s#%d %s" % (repo, item["number"], label)
                if label in item["labels"] and key not in wake.told:
                    pending.append((item["number"], label, key, title))
        names = sorted(n for n, a in (discovery.agents.items() if discovery else []) if _is_orchestrator(a) and _fleet_repo(a.fleet or {}, engine.config) == repo)
        pending.sort(key=lambda p: (p[0], labels.index(p[1])))
        if names:   # nobody to tell: nothing is remembered, so the first orchestrator of the repository is told
            for number, label, key, title in pending[:ALERT_WAKES_PER_PASS]:
                for name in names:
                    wake.facts["alert:%s:%s" % (key, name)] = (alert_line(repo, number, title, label), now, name, repo, number)
                wake.lines[key] = {name: wake.facts["alert:%s:%s" % (key, name)][0] for name in names}
                wake.told[key] = now
                changed = True
                engine._note("alert_wake.sent", "%s#%d" % (repo, number), {"label": label, "to": names})
            rest = pending[ALERT_WAKES_PER_PASS:]
            if rest:
                digest = hashlib.sha256("\n".join(p[2] for p in rest).encode()).hexdigest()[:12]
                for old in [k for k in wake.facts if k.startswith("alert:%s:more:" % repo)]:
                    del wake.facts[old]   # one summary line per repository: a newer count replaces one not yet delivered
                for name in names:
                    wake.facts["alert:%s:more:%s:%s" % (repo, digest, name)] = (
                        "%d more alert issue(s) of %s will be told on the next passes" % (len(rest), repo), now, name, repo, None)
        for key in [k for k in wake.told if k.startswith(repo + "#") and int(k[len(repo) + 1:].split(" ", 1)[0]) not in open_numbers]:
            del wake.told[key]   # a closed issue is forgotten, on a whole read only
            wake.lines.pop(key, None)
            changed = True
    if changed:
        keep = {k: dict({"at": at}, **({"lines": wake.lines[k]} if k in wake.lines and now - at < ALERT_FACT_KEEP else {}))
                for k, at in sorted(wake.told.items(), key=lambda kv: kv[1])[-ALERT_MEMORY_MAX:]}
        try:
            _write_small(engine.state_dir, ALERT_WAKE_FILE, keep)
        except OSError as err:
            engine._note("alert_wake.error", "", {"error": "alert-wake.json was not saved, so a restart may tell again: %s" % _one_line(type(err).__name__, 40)})


class AlertWakeFacts:
    """The alert lines, as ordinary facts: the usual rule picks a note or a wake, and a key found in the recipient's inbox is never delivered again."""

    __name__ = "alert_wake_facts"

    def __init__(self):
        self.engine = None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        wake = getattr(self.engine, "alert_wake", None) if self.engine else None
        if not wake or not wake.facts:
            return []
        return [Fact(key, name, "alert", text, created, issue=number, sender="switchboard", repo=repo)
                for key, (text, created, name, repo, number) in sorted(wake.facts.items())]


# ---- #1141: a required check that failed at an open head of a worker-owned pull request, and a default branch that turned red or green again, reach the
# people who must act. Reads nothing new: the runs the catch-up already reads. The messages are ordinary facts (the delivery rule holds them while blocked).

RED_CI_FILE = "red-ci.json"   # {"told": {key: at}, "branch": {repo: "red"|"green"}, "lines": {key: {name: text}}} (private; survives a restart)
RED_CI_PER_HOUR = 20          # a bound per hour on new messages; the rest is not told (and not remembered, so a later pass tells it)
RED_CI_KEEP = 2 * 86400


class RedCiState:
    def __init__(self, state_dir, now):
        self.told, self.branch, self.facts, self.recent = {}, {}, {}, []
        try:
            with open(str(Path(state_dir) / RED_CI_FILE), "rb") as handle:
                raw = json.loads(handle.read(1000001).decode("utf-8"))
            if isinstance(raw, dict):
                self.told = {k: v for k, v in (raw.get("told") or {}).items() if isinstance(k, str) and _is_number(v)}
                self.branch = {k: v for k, v in (raw.get("branch") or {}).items() if isinstance(k, str) and v in ("red", "green")}
                for key, lines in (raw.get("lines") or {}).items():
                    at = self.told.get(key)
                    if at is not None and now - at < RED_CI_KEEP and isinstance(lines, dict):
                        for name, text in lines.items():
                            if isinstance(text, str) and isinstance(name, str):
                                self.facts["red-ci:%s:%s" % (key, name)] = (text, at, name, key.split(":")[1].split("#")[0])
        except (OSError, ValueError, UnicodeError):
            pass
        self.lines = {}


def red_ci_state(engine):
    state = getattr(engine, "red_ci", None)
    if state is None:
        state = engine.red_ci = RedCiState(engine.state_dir, engine.now()) if engine.state_dir is not None else RedCiState("/nonexistent-red-ci", engine.now())
    return state


def red_ci_receivers(engine, repo, with_chief):
    discovery = getattr(engine, "discovery", None)
    if discovery is None:
        return []
    names = {n for n, a in discovery.agents.items() if _is_orchestrator(a) and _fleet_repo(a.fleet or {}, engine.config) == repo}
    if with_chief:
        names |= {n for n, a in discovery.agents.items() if (a.fleet or {}).get("role") == "chief-of-staff"}
    return sorted(names)


def red_ci_tell(engine, state, key, repo, text, names, now):
    """ONE message per key, to each receiver; nobody to tell, or over the hourly bound: nothing is remembered, so a later pass tells it."""
    if key in state.told or not names:
        return False
    state.recent = [t for t in state.recent if now - t < 3600]
    if len(state.recent) >= RED_CI_PER_HOUR:
        return False
    state.recent.append(now)
    state.told[key] = now
    state.lines[key] = {n: text for n in names}
    for name in names:
        state.facts["red-ci:%s:%s" % (key, name)] = (text, now, name, repo)
    engine._note("red_ci.sent", repo, {"key": key, "to": names})
    return True


def red_ci_save(engine, state, now):
    keep = {k: at for k, at in sorted(state.told.items(), key=lambda kv: kv[1])[-ALERT_MEMORY_MAX:]}
    lines = {k: {n: v[0] for n, v in [(f.rsplit(":", 1)[1], state.facts[f]) for f in state.facts if f.startswith("red-ci:%s:" % k)]} for k in keep if now - keep[k] < RED_CI_KEEP}
    try:
        _write_small(engine.state_dir, RED_CI_FILE, {"told": keep, "branch": state.branch, "lines": lines})
    except OSError as err:
        engine._note("red_ci.error", "", {"error": _one_line(type(err).__name__, 40)})


def red_ci_latest(repo, runs):
    """The runs that count: the shared reader (bin/ci_runs.py `latest_runs`) keeps only the latest run of each workflow by identity (workflow id, event), never by name.
    The workflow id is already in each run record, so it is seeded into the reader's cache: no read is added."""
    ci = load_module("ci_runs")
    low, entries = repo.lower(), []
    for run in runs:
        if isinstance(run, dict) and isinstance(run.get("id"), int):   # a record with no workflow id is its own group (superseded by nothing, never read by name)
            wid = run["workflow_id"] if type(run.get("workflow_id")) is int else -run["id"]
            ci._put(ci._CACHE, (low, run["id"]), (wid, str(run.get("event") or "")))
        if isinstance(run, dict) and isinstance(run.get("id"), int):
            entries.append(dict(run, detailsUrl="https://github.com/%s/actions/runs/%d" % (repo, run["id"]), name=run.get("name") or "run"))
    kept = {e["id"] for e in ci.latest_runs(entries, low) if e.get("status") not in (ci.UNREADABLE, ci.CAP_STATUS)}
    return [r for r in runs if not (isinstance(r, dict) and isinstance(r.get("id"), int)) or r["id"] in kept]


def red_ci_runs(engine, repo, runs, now):
    """Judge the workflow runs the catch-up read of `repo`. Never raises."""
    try:
        if engine is None or engine.state_dir is None:
            return
        module, state, changed = load_module("red_ci"), red_ci_state(engine), False
        cfg = engine.config or {}
        required = (cfg.get("red_ci_required") or {}).get(repo) or []
        default = (cfg.get("default_branches") or {}).get(repo) or "main"
        discovery = getattr(engine, "discovery", None)
        agents = discovery.agents if discovery else {}
        newest = None
        runs = red_ci_latest(repo, runs)
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                continue
            word, head, url, name = module.colour(run), run.get("head_sha"), run.get("html_url") or "", str(run.get("name") or "run")
            if word == "none" or not isinstance(head, str):
                continue
            prs = [p.get("number") for p in run.get("pull_requests") or [] if isinstance(p, dict) and isinstance(p.get("number"), int)]
            if prs:
                if word != "red" or not module.is_required(required, name):
                    continue
                for number in prs:
                    owner = pick_owner(agents, [number], repo)
                    if owner is None or _is_orchestrator(agents[owner]):
                        continue   # no worker of the lane owns it
                    changed |= red_ci_tell(engine, state, module.pr_key(repo, number, head, name), repo,
                                           module.pr_line(repo, number, name, url, head), red_ci_receivers(engine, repo, False), now)
            elif run.get("head_branch") == default and run.get("event") != "pull_request":
                if newest is None or str(run.get("created_at") or "") > str(newest.get("created_at") or ""):
                    newest = run
        if newest is not None:
            word, rid = module.colour(newest), newest["id"]
            head, url, name = newest.get("head_sha"), newest.get("html_url") or "", str(newest.get("name") or "run")
            if word == "red" and module.branch_key("red", repo, head) not in state.told:
                job = newest.get("failing_job") or name
                ok = red_ci_tell(engine, state, module.branch_key("red", repo, head), repo, module.branch_line("red", repo, default, name, url, head, job),
                                 red_ci_receivers(engine, repo, True), now)
                if ok:
                    state.branch[repo], changed = "red", True
            elif word == "green" and state.branch.get(repo) == "red":
                ok = red_ci_tell(engine, state, module.branch_key("green", repo, rid), repo, module.branch_line("green", repo, default, name, url, head),
                                 red_ci_receivers(engine, repo, True), now)
                if ok:
                    state.branch[repo], changed = "green", True
                    for old in [k for k in state.told if k.startswith("branch-red:%s:" % repo)]:
                        del state.told[old]   # a later red at the same head is a new change
        if changed:
            red_ci_save(engine, state, now)
    except Exception as err:
        engine._note("red_ci.error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})


class RedCiFacts:
    """The red-CI lines, as ordinary facts: the usual rule holds them while the receiver is blocked, and a key in the inbox is never delivered again."""

    __name__ = "red_ci_facts"

    def __init__(self):
        self.engine = None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        state = getattr(self.engine, "red_ci", None) if self.engine else None
        if not state or not state.facts:
            return []
        return [Fact(key, name, "alert", text, created, sender="switchboard", repo=repo)
                for key, (text, created, name, repo) in sorted(state.facts.items())]


def testers_status(config, agents, fleets, state):
    """The `status` lines of the resolved testers per repo with their source (#372); [] when there are none or the picture is unreadable."""
    try:
        if not (agents.get("ok") and fleets is not None):
            return []
        sessions = {entry["observation"]["name"]: entry["observation"].get("session") for entry in (agents.get("list") or []) if entry.get("observation")}
        repo_of = lambda fleet: _fleet_repo(fleet, config)
        repos = {repo_of(fleet) for fleet in fleets.values() if isinstance(fleet, dict) and fleet.get("role") == "orchestrator"} - {None}
        try:
            retired = registry_retired_strict(state)
        except RetiredUnreadable:
            return ["      testers    retired file unreadable: launch-record testers disabled"]   # the same fail-closed rule the verdict gate applies
        return load_launch_testers().status_lines(config.get("plan_evaluators"), fleets, sessions, repos, repo_of, retired)
    except Exception as err:
        return ["      tester     unreadable: the resolved testers could not be listed (%s)" % _one_line(type(err).__name__, 40)]


def triage_status_lines(triage):
    if not triage:
        return []
    parts = ", ".join("%s %d" % (k, n) for k, n in sorted(triage["counts"].items())) or "none"
    lines = ["      triage     %s: %s finding(s) (%s) over %s repo(s) as of %s" % (
        "on (wakes the Chief of Staff; writes nothing to GitHub)" if triage.get("enabled") else "shadow (audit only; writes nothing to GitHub)",
        triage.get("total", 0), parts, triage.get("repos", "?"), triage.get("at", "?"))]
    plans = load_triage_plan_open().unapproved_line(triage.get("plans_unapproved"), triage.get("plans_unapproved_more", 0))
    if plans:
        lines.append("      triage     " + plans)
    if triage.get("waiting_for_batch"):   # #921: counted apart, never a finding
        lines.append("      triage     %d plan issue(s) waiting for the batch (their change merged into the integration branch)" % triage["waiting_for_batch"])
    if triage.get("other_owner"):
        lines.append("      triage     %d other-owner flag(s): not ours, not counted, never a wake on their own" % triage["other_owner"])
    if triage.get("unreadable"):
        lines.append("      triage     unreadable repo(s), no findings from them: " + ", ".join(triage["unreadable"]))
    if triage.get("unchecked_units"):
        lines.append("      triage     unchecked units: " + "; ".join(
            "%s (%s)" % (r, ", ".join(u)) for r, u in sorted(triage["unchecked_units"].items())))
    if triage.get("unchecked"):
        extra = ""
        if triage.get("unchecked_other_owner"):
            extra += " (%d of them other-owner flags)" % triage["unchecked_other_owner"]
        if triage.get("oldest_unchecked_seconds") is not None:
            extra += "; oldest unchecked for %s" % format_age(triage["oldest_unchecked_seconds"])
        lines.append("      triage     %d finding(s) not checked this pass (call budget or a failed read)%s: neither "
                     "raised nor audited" % (triage["unchecked"], extra))
    for repo_of, info in sorted((triage.get("unchecked_why") or {}).items()):   # #605: why, per repository
        text = load_module("triage_ownerless").reasons_line(repo_of, info.get("reasons") or {}, info.get("other_owner", 0), info.get("oldest_seconds"))
        if text:
            lines.append("      triage     " + text)
    for repo_of, info in sorted((triage.get("ownerless") or {}).items()):
        lines.append("      triage     ownerless in %s: %d open issue(s) with no owner and quiet; parked %d; read %s of %s open item(s)" % (
            repo_of, info.get("count", 0), info.get("parked", 0), info.get("read"), info.get("total") if info.get("total") is not None else "an unknown number"))
    if triage.get("truncated"):
        lines.append("      triage     lists capped for " + ", ".join(triage["truncated"]))
    cadence = triage_digest.digest_line(triage.get("digest"), parse_iso(triage.get("at")) or time.time(), triage.get("digest_interval_minutes", 60))   # #406
    if cadence:
        lines.append("      triage     " + cadence)
    if "cost" in triage:   # #349: what the digests cost the Chief of Staff to read, per day
        lines.extend(triage_digest.cost_lines(triage["cost"], str(triage.get("at") or "")[:10]) if triage["cost"]
                     else ["      triage     cost per day: not recorded (nothing delivered yet)"])
    return lines


class TriageFacts:
    """The triage's pending facts for the Chief of Staff (the batched findings and the daily summary), one per Chief of
    Staff that runs; kept until delivered, then they age out of the triage's memory."""

    __name__ = "triage_facts"

    def __init__(self):
        self.engine = None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        engine = self.engine
        state = getattr(engine, "triage", None) if engine else None
        discovery = getattr(engine, "discovery", None) if engine else None
        if not state or not state.facts or discovery is None or discovery.problems:
            return []
        chiefs = sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff")
        return [Fact(key, name, "triage", summary, created, sender="switchboard")
                for key, (summary, created) in sorted(state.facts.items()) for name in chiefs]

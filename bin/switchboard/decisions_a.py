# ---- PR 18: a request the Chief of Staff escalated to you

ESCALATED_FILE = "escalated-requests.json"


ESCALATED_LOCK = "escalated-requests.lock"


@contextmanager
def escalated_lock(state_dir):
    fd = os.open(str(Path(state_dir) / ESCALATED_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_escalated(state_dir):
    """{"<session>:<request id>": iso time} of the permission requests and questions the Chief of Staff escalated.
    A DISPOSABLE file, not a second source of truth: it only moves a request that is still pending from `cos` to
    `human` a little before the grace period would, and it is pruned to the requests pending now. Missing,
    unreadable or malformed reads as empty: the request is then yours when the grace period has run out."""
    try:
        raw = json.loads((Path(state_dir) / ESCALATED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    found = raw.get("requests") if isinstance(raw, dict) else None
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(found, dict) else {}


def write_escalated(state_dir, requests):
    path = Path(state_dir) / ESCALATED_FILE
    temp = path.with_name("%s.%d.tmp" % (ESCALATED_FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"requests": requests}, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def mark_escalated(state_dir, key, when):
    with escalated_lock(state_dir):
        found = read_escalated(state_dir)
        found[key] = iso(when)
        write_escalated(state_dir, found)


BATCH_NOTES_FILE = "batch-notes.json"


BATCH_NOTES_KEEP = 100


def batch_note_key(repo, number, text):
    return "%s#%d:%s" % (repo, number, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])


def read_batch_notes(state_dir):
    """{"<repo>#<n>:<digest of the note>": iso time} of the answer notes already delivered. A DISPOSABLE file: it only stops a re-run of an answer
    (the note went, the comment did not) from sending the same note again. Missing or unreadable reads as empty (then the note is sent again)."""
    try:
        raw = json.loads((Path(state_dir) / BATCH_NOTES_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}


def mark_batch_note(state_dir, key, when):
    """Record the note as delivered and pending (`when` set), or forget it (`when` None: the answer is fully recorded)."""
    found = read_batch_notes(state_dir)
    if when is None:
        found.pop(key, None)
    else:
        found[key] = iso(when)
    kept = dict(sorted(found.items(), key=lambda kv: kv[1])[-BATCH_NOTES_KEEP:])
    path = Path(state_dir) / BATCH_NOTES_FILE
    temp = path.with_name("%s.%d.tmp" % (BATCH_NOTES_FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(kept, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


ESCALATION_MAX_AGE_SECONDS = 14 * 86400   # a captain escalation older than this is forgotten, whatever else holds it


def captain_escalation_leaves(engine, escalated, now):
    """{key: reason} for the captain escalations that may be pruned, and only on evidence: older than ESCALATION_MAX_AGE_SECONDS
    (`expired`), or a list read of that repository that SUCCEEDED in this process (`state.good`) shows the pull request absent
    (`absent`, not from a list that may be cut), at another head (`another head`) or without the hold labels (`no longer held`).
    The list must have been read AFTER the key was written (a fresh escalation is kept this pass). An empty, unread, failed or
    never-listed repository gives none. Never raises."""
    out = {}
    left = (getattr(engine, "notice_effects", None) or {}).get("left") or set()
    state = getattr(engine, "ci_record_state", None)
    good = getattr(state, "good", None) or {}
    for key, stamp in escalated.items():
        if not key.startswith("captain:"):
            continue
        at = parse_iso(stamp)
        if at is not None and now - at > ESCALATION_MAX_AGE_SECONDS:
            out[key] = "expired"
            continue
        try:
            ref, _, head = key[len("captain:"):].rpartition("@")
            repo, _, number = ref.rpartition("#")
            if ref in left:
                out[key] = "merged or closed"
                continue
            read = good.get(plan_ledger.normal_repo(repo))
            if read is None or not number.isdigit() or not head:
                continue
            if at is None or read[0] <= at:
                continue   # the list was read no later than the key was written: it cannot show whether the pull request left
            records, cut = read[1], read[2]
            record = next((r for r in records if isinstance(r, dict) and _number(r) == int(number)
                           and r.get("state", "open") == "open" and not r.get("merged_at")), None)
            if record is None:
                if not cut:
                    out[key] = "absent"
            elif ((record.get("head") or {}).get("sha") if isinstance(record.get("head"), dict) else None) != head:
                out[key] = "another head"
            elif not _label_kind({(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []},
                                 CAPTAIN_MERGE_LABELS[:1] + CAPTAIN_HOLD_LABELS):
                out[key] = "no longer held"
        except Exception:
            continue
    return out


def captain_escalations_to_keep(engine):
    """The escalations of `MERGE: captain` PR decisions that must survive a pass in which the PR could not be judged: those
    whose notice is still on file. An unreadable PR (a restart's first passes, a 404 under the wrong account, an outage) derives
    no captain entry, so its key is not among the pending requests, and pruning on that would demote a decision the Chief of
    Staff escalated. The key leaves when its notice does (the PR merged or closed), or by the head naming it never matching again.
    None when the notices cannot be read: then nothing is pruned at all."""
    try:
        alive = {"%s#%d" % (n["repo"], n["number"]) for n in read_pr_notices(engine.state_dir)}
        escalated = read_escalated(engine.state_dir)
    except (NoticeError, OSError, TypeError):
        return None
    return {k for k in escalated if k.startswith("captain:") and k[len("captain:"):].rpartition("@")[0] in alive}


def prune_escalations_pass(engine, store, now):
    """The daemon's end-of-pass prune. A captain key is kept unless `captain_escalation_leaves` has evidence (a noticed one is kept
    as before, but not past the age cap); every other key goes when no longer pending. Each removed key is audited with its reason."""
    keep = captain_escalations_to_keep(engine)
    if keep is None or engine.request_keys is None:
        return
    for name in ("requests", "notices"):
        if whole_skipped(engine, "escalation.prune", name):
            return   # #805: a worker or a notice that could not be read lists nothing, which is no evidence a request is gone
    escalated = read_escalated(engine.state_dir)
    leaves = captain_escalation_leaves(engine, escalated, now)
    captains = {k for k in escalated if k.startswith("captain:")}
    pending = (set(engine.request_keys) | keep | (captains - set(leaves))) - {k for k, why in leaves.items() if why == "expired"}
    removed = store.prune_escalated(pending)
    for key in sorted(removed or ()):
        if engine.audit is not None:   # not `_note`: the same key pruned twice is two events
            engine.audit.append(engine.actor, "escalation.pruned", key, {"escalation": key, "reason": leaves.get(key, "request no longer pending")})


def prune_escalated(state_dir, pending):
    """Forget every escalation of a request that is no longer pending (the daemon's, once a pass)."""
    if pending is None or not (Path(state_dir) / ESCALATED_FILE).exists():
        return []
    with escalated_lock(state_dir):
        found = read_escalated(state_dir)
        kept = {k: v for k, v in found.items() if k in pending}
        if kept != found:
            write_escalated(state_dir, kept)
        return sorted(set(found) - set(kept))


class BossProblem(str):
    """A problem line that names the boss it is about (#80): the sync freezes only that boss's reports."""

    boss = None


def decisions_from_reports(engine, now):
    """(items, problems): `question` and `blocked` reports delivered to a boss that nobody has answered.

    A report is answered when the boss has since sent that worker something on the same ask (a `send` with no
    issue counts for any ask: the worker has one), or the worker has since reported again on that ask."""
    discovery = engine.discovery
    if discovery is None:
        return [], []
    if discovery.problems:
        raise RuntimeError("the fleet cannot be read: %s" % "; ".join(discovery.problems))
    since = now - engine.config["decisions_lookback_hours"] * 3600
    bosses = sorted({resolve_boss(discovery, _boss_of(o)) for o in discovery.agents.values() if _boss_of(o)})
    repos = decision_repos(engine)
    listed = registry_read(engine.state_dir) if engine.state_dir is not None else {"whole": False, "retired": {}}   # the decisions view: who is still an agent comes from the registry
    items, problems, sends, unread = [], [], {}, {}
    for boss in bosses:
        observation = discovery.agents.get(boss)
        if observation is None or observation.problem:
            problem = BossProblem("cannot read what was reported to %s: %s" % (
                boss, observation.problem if observation else "it is not running"))
            problem.boss = boss
            problems.append(problem)
            continue
        reports = []
        for created, message_id, number, line in synthetic_lines(engine, observation.session_id, since):
            match = REPORT_LINE.match(line)
            if match:
                sender = discovery.agents.get(match.group(1))
                issue = int(match.group(3)) if match.group(3) else None
                reports.append({"at": (created, number), "message": message_id, "sender": match.group(1),
                                "issue": issue, "state": match.group(4), "text": match.group(5), "boss": boss,
                                "repo": (repo_of_tag(match.group(2), repos)
                                         or (_fleet_repo(sender.fleet, engine.config) if sender else None)),
                                "repo_known": bool(match.group(2)),  # #126: a repo NAMED in the line (watched or not) is not a guess
                                "session": observation.session_id})
        # a withdrawal is a rider: no message has carried it into the transcript yet, but it already counts
        for item in (source.entries() if (source := _report_rider_source(engine)) is not None else []):
            state, _, text = item["summary"].partition(": ")
            sender = discovery.agents.get(item.get("from"))
            if resolve_boss(discovery, item["to"]) == boss and state == "withdrawn" and item["created"] >= since and sender is not None:
                reports.append({"at": (item["created"], 1 << 60), "message": item["key"], "sender": sender.name,
                                "issue": item.get("issue"), "state": state, "text": text, "boss": boss,
                                "repo": item.get("repo") or _fleet_repo(sender.fleet, engine.config), "session": None})
        reports.sort(key=lambda r: r["at"])
        for report in reports:
            if report["state"] not in ("question", "blocked") or report["sender"] not in discovery.agents or (listed["whole"] and report["sender"] in listed["retired"]):
                continue  # not a decision, or the worker that asked is retired in the registry (or no longer runs): no one is waiting; a read that is not whole drops nothing
            worker = report["sender"]
            if worker not in unread:
                try:
                    if not fault("decision-resolves-never") and report_answered(engine, report, reports, since, sends):
                        continue
                except (V2Error, RuntimeError) as err:
                    # one worker's unreadable inbox or transcript: its reports stay listed (not answered), the error is
                    # shown once, and every other worker is unaffected
                    unread[worker] = _one_line(str(err), 150)
                    problems.append(unread[worker] if unread[worker].startswith("cannot read what %s" % worker)
                                    else "cannot read what %s was sent: %s" % (worker, unread[worker]))
            title = report["text"] if report["state"] == "question" else "%s: %s" % (report["state"], report["text"])
            if worker in unread:
                considered = "could not read what %s was sent (%s): listed as not answered" % (worker, unread[worker])
            else:
                considered = considered_send(engine, report, repos, since, sends)
            above = _boss_of(discovery.agents[report["boss"]])  # PR 18: a boss that has no boss is the Chief of Staff
            items.append({"kind": "report", "title": title, "ask": report["issue"], "agent": report["sender"],
                          "repo": report["repo"], "tier": decision_core.report_tier(bool(above)),
                          "repo_known": report.get("repo_known", True),
                          "since": report["at"][0], "bare": False, "boss": report["boss"],
                          "seed": "report:%s:%s:%d" % (report["session"], report["message"], report["at"][1]),
                          "considered": considered})
    return items, problems


def considered_send(engine, report, repos, since, sends):
    """One line saying, for a report still open, the newest send from its boss to its worker that the check looked
    at and why it did not answer, or that there was none (`decisions --fresh` prints it). Stable from pass to pass,
    so it never rewrites decisions.json by itself."""
    worker = engine.discovery.agents[report["sender"]]
    if worker.name not in sends:
        sends[worker.name] = synthetic_lines(engine, worker.session_id, since, waiting=True)
    newest = None
    for created, message_id, _, line in sends[worker.name]:
        match = SEND_LINE.match(line)
        if match and match.group(1) == report["boss"] and (newest is None or created > newest[0]):
            newest = (created, match, message_id)
    who = "%s to %s" % (report["boss"], worker.name)
    if newest is None:
        return "no send from %s in the last %g hours" % (who, engine.config["decisions_lookback_hours"])
    created, match, message_id = newest
    admitted = message_id in {line[1] for line in transcript_lines(engine, worker.session_id, since)}
    tag = "%s#%s" % (match.group(2) or "", match.group(3)) if match.group(3) else "no issue"
    if created <= report["at"][0]:
        why = "older than this report"
    elif match.group(3) and report["issue"] is not None and (int(match.group(3)) != report["issue"]
                             or repo_of_tag(match.group(2), repos) not in (None, report["repo"])):
        why = "tagged %s, not this ask" % tag
    else:
        why = "should answer it"
    return "newest send from %s: %s, %s, %s, %s" % (who, iso(created), tag,
                                                    "admitted" if admitted else "queued in the inbox", why)


def report_answered(engine, report, reports, since, sends):
    """True when the report is answered (the rules are decision_core's: a later report of the worker, or a send of
    the boss on the ask). `sends` caches one read of each worker's transcript per derivation. The sends are read
    only when no later report answers, as before, and an unreadable worker raises here."""
    if decision_core.answered_by_later_report(report, reports):
        return True
    worker = engine.discovery.agents[report["sender"]]
    if worker.problem:
        raise RuntimeError("cannot read what %s was sent: %s" % (worker.name, worker.problem))
    if worker.name not in sends:
        sends[worker.name] = synthetic_lines(engine, worker.session_id, since, waiting=True)
    return decision_core.answered_by_send(report, sends[worker.name], decision_repos(engine))


# ---- the readable headline of a decision (the panel shows this; `title` stays for older readers)

HEADLINE_WIDTH = decision_core.HEADLINE_WIDTH   # the Decisions panel's ROW_WIDTH (plugins/fleet-decisions-tui/decisions.mjs); a test keeps them equal


HEADLINE_LINES = decision_core.HEADLINE_LINES


_URL = decision_core._URL


_GITHUB_REF = decision_core._GITHUB_REF


_ISSUE_URL = decision_core._ISSUE_URL


_SHORT_REF = decision_core._SHORT_REF


def short_refs(text):
    return decision_core.short_refs(text)


def _wrap_count(words, width):
    return decision_core.wrap_count(words, width)


def fit_lines(text, width=HEADLINE_WIDTH, lines=HEADLINE_LINES):
    return decision_core.fit_lines(text, width, lines)


def headline_of(item, issue_title):
    return decision_core.headline_of(item, issue_title)


def cached_issue_title(engine, repo, number, listed):
    """The title of issue `repo#number` from what the daemon already holds, never a new `gh` read: an open decision's
    own title (the hub's picture), else the intent reader's cached issue (any age). None when neither knows it."""
    if (repo, number) in listed:
        return listed[(repo, number)] or None
    reader = getattr(engine, "intents", None)
    hit = reader._cache.get(("issue", repo, number)) if reader is not None and repo else None
    found = hit[1] if hit else None
    return (getattr(found, "title", None) or None)


def cached_issue_parent(engine, repo, number):
    """The parent (ask) of issue `repo#number` from the intent reader's cache, never a new `gh` read; None when unknown."""
    reader = getattr(engine, "intents", None)
    hit = reader._cache.get(("parent", repo, number)) if reader is not None and repo else None
    value = hit[1] if hit else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


FOLD_PARENT_READS = 3   # at most this many parent reads in one daemon pass, each cached by the intent reader


def chain_parent_lookup(engine):
    """`known_parent(repo, number)` for the report fold: the cached parent; in the DAEMON (which holds the hub) up to
    FOLD_PARENT_READS fresh reads per pass for a report whose issue has no cached parent, so the ask chain can be followed
    (a report on a work item folds into its escalated ask). A command derivation (`decisions --fresh`) reads nothing."""
    left = [FOLD_PARENT_READS if getattr(engine, "hub", None) is not None else 0]

    def lookup(repo, number):
        found = cached_issue_parent(engine, repo, number)
        reader = getattr(engine, "intents", None)
        if found is not None or reader is None or left[0] <= 0 or not repo:
            return found
        if ("parent", repo, number) in reader._cache:
            return None   # asked recently: it has none
        left[0] -= 1
        try:
            return reader.parent(number, repo)
        except IntentError:
            return None
    return lookup


def named_issues(text, repo, wanted):
    return decision_core.named_issues(text, repo, wanted)


PR_NOTICES_LOCK = "pr-notices.lock"


PR_FACTS_TTL = 60                 # seconds a PR's facts (or a failed read) are kept before `gh pr view` runs again


PR_NOTICE_PASS_BUDGET = 15        # seconds one pass may spend reading notices' PRs; the rest wait for the next pass


PR_OPEN_CONFIRM_SECONDS = 300     # a PR last SEEN open longer ago than this, and not readable now, is not listed (its notice is kept; it returns when it can be read)


PR_NOTICE_UNREADABLE_SECONDS = 3600  # a notice whose PR cannot be read for this long (OBSERVED: see decisions_from_notices) is dropped (was 7 days)


PR_NOTICE_OBSERVE_MAX = 600          # the most one pass may add to that count: a suspended machine or a long gap is not observed time


_BARE_PR = re.compile(r"^#?([0-9]+)$")


def parse_bare_pr_ref(text, directory=None):
    """`1234` or `#1234` -> (repo, number), the repository being the `origin` remote of the working directory (`origin_repository`: never a configured
    default repository). Used by `review laws` only; every other form goes through `parse_pr_ref`. Raises NoticeError saying what to pass."""
    found = _BARE_PR.match((text or "").strip())
    if not found or int(found.group(1)) < 1:
        raise NoticeError("%r is not a pull request: pass 1234, #1234, owner/repo#N or the PR's URL" % _one_line(text, 120))
    repo, why = origin_repository(None, directory or os.getcwd())
    if repo is None:
        raise NoticeError("%s: a bare number needs the working directory's remote: pass owner/repo#N or the PR's URL" % why)
    return repo, int(found.group(1))


@contextmanager
def pr_notices_lock(state_dir):
    fd = os.open(str(Path(state_dir) / PR_NOTICES_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_pr_notices(state_dir, items):
    """Replace the file atomically, mode 0600 (the way report-riders.json is written)."""
    path = Path(state_dir) / PR_NOTICES
    temp = path.with_name("%s.%d.tmp" % (PR_NOTICES, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"notices": items}, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def record_pr_notice(state_dir, repo, number, sender, note, now, boss=None):
    """Record (or refresh) the notice of `repo#number`. A re-send keeps the first time (the hold window runs from
    it) and replaces the note and the sender. Returns (item, refreshed)."""
    with pr_notices_lock(state_dir):
        items = read_pr_notices(state_dir)
        old = next((i for i in items if i["repo"] == repo and i["number"] == number), None)
        item = {"repo": repo, "number": number, "from": sender, "note": note,
                "created": old["created"] if old else now, "refreshed": now}
        senders = notice_senders(old) if old else []
        item["senders"] = [n for n in senders if n != sender][-(NOTICE_SENDERS_MAX - 1):] + ([sender] if sender else [])
        boss = boss or (old or {}).get("boss")
        if boss:  # PR 52: who the sender reports to, so a PR whose sender is gone still reaches someone
            item["boss"] = boss
        if old and old.get("unreadable_since") is not None:
            item["unreadable_since"] = old["unreadable_since"]
            if old.get("unreadable_for") is not None:
                item["unreadable_for"] = old["unreadable_for"]
        write_pr_notices(state_dir, [i for i in items if i is not old] + [item])
    return item, old is not None


def apply_notice_effects(engine):
    """The daemon's end of a pass: forget the notices whose PR is merged, closed or unreadable for good, and note
    when a PR became unreadable (or readable again). Writes only on a change; a failure is tried again next pass."""
    effects = engine.notice_effects
    if engine.state_dir is None or engine.audit is None or not (effects["drop"] or effects["unreadable"] or effects.get("observed")):
        return False
    try:
        with pr_notices_lock(engine.state_dir):
            items = read_pr_notices(engine.state_dir)
            kept = []
            for item in items:
                key = "%s#%d" % (item["repo"], item["number"])
                if key in effects["drop"]:
                    continue
                if key in effects["unreadable"]:
                    item = dict(item)
                    if effects["unreadable"][key] is None:
                        item.pop("unreadable_since", None)
                        item.pop("unreadable_for", None)
                    else:
                        item["unreadable_since"] = effects["unreadable"][key]
                if key in effects["observed"]:
                    item = dict(item)
                    if effects["observed"][key] is None:
                        item.pop("unreadable_for", None)
                    else:
                        item["unreadable_for"] = effects["observed"][key]
                kept.append(item)
            if kept != items:
                write_pr_notices(engine.state_dir, kept)
                return True
    except (OSError, NoticeError):
        pass
    return False


CAPTAIN_MARK = "MERGE: captain"


def pr_is_captain(body):
    """True when the body's FIRST non-empty line starts with `MERGE: captain`. Nothing else of the body is read."""
    return pr_marker(body) == "captain"


def is_captain(pr, now=None):
    """THE one helper: whether a PR is held for the person. `pr` is a dict with `labels` (names or {"name"} dicts) and `body`;
    see `pr_marker_state` for the rule (label first, the first-line marker only inside the transition window)."""
    return pr_marker_state((pr or {}).get("labels"), (pr or {}).get("body"), now)["captain"]


def sweep_clear_agrees(engine, repo, number, labels, marker):
    """(#827, S6b) True when the ONLY disagreement of a pull request's merge label and its first-line marker is an author's `merge-captain` label still on it beside an automatic line, and the owner's clear is recorded for the
    EXACT head the pull request has now (the daemon's own list read; an unknown head, an unreadable impact state or a clear on another head agree with nothing): the clear is the owner's answer for that head, so the leftover label is not a
    disagreement to finish. Read-only."""
    try:
        kinds = _label_kind(labels, CAPTAIN_MERGE_LABELS)
        if kinds != ["merge-captain"] or marker != "automatic" or engine.state_dir is None:
            return False
        pictures = getattr(engine, "ci_record_state", None)
        head = ((pictures.pr_state.get("%s#%d" % (plan_ledger.normal_repo(repo), number)) if pictures is not None else None) or {}).get("head")
        clears, problem = read_impact_state(engine.state_dir)
        return not problem and isinstance(head, str) and bool(head) and (clears["clears"].get("%s#%d" % (repo, number)) or {}).get("head") == head
    except Exception:
        return False


CAPTAIN_NO_REVIEW_SECONDS = 2 * 3600  # a captain PR with no review seen is a decision anyway this long after its notice


CAPTAIN_EVIDENCE_MAX = 5    # signed reviewers listed in a decision


def captain_evidence(facts, senders):
    """[{"by", "kind": review|comment, "state"}] of the review of a `MERGE: captain` PR at its exact head: formal
    reviews by a non-author, plus signed comments naming the head whose signer is not a sender of the PR's notice.
    None when there is none and a source could not be read (review not detectable)."""
    evidence = [dict(r, kind="review") for r in facts.get("reviews") or []]
    for who in (facts.get("signed") or []):
        if who not in senders and who != facts.get("author") and all(e["by"] != who for e in evidence):
            evidence.append({"by": who, "kind": "comment", "state": "signed comment"})
    if evidence:
        return evidence[:CAPTAIN_EVIDENCE_MAX]
    return None if facts.get("reviews") is None or facts.get("signed") is None else []


def notice_facts(engine, repo, number, now, budget=None):
    """{facts, stale, error} for one notice: read at most once per PR_FACTS_TTL seconds (a failed read too), the
    last good facts kept and marked stale when a read fails. `budget` is a one-item list of seconds this pass may
    still spend reading; when it is spent the read is skipped (stale, "skipped": True, not cached) until next pass."""
    key = "%s#%d" % (repo, number)
    held = engine.notice_facts.get(key)
    if held is not None and now - held["at"] < PR_FACTS_TTL:
        return held
    confirmed = held.get("confirmed") if held else None   # when a read last showed it OPEN
    if budget is not None and budget[0] <= 0:
        return {"at": 0, "facts": held["facts"] if held else None, "stale": True, "skipped": True, "confirmed": confirmed,
                "error": "not read this pass: the notice reads' time budget is spent"}
    started = engine.now()
    try:
        facts = read_pr_facts(engine, repo, number)
        held = {"at": now, "facts": facts, "stale": False, "error": None, "confirmed": now if facts.get("state") == "open" else confirmed}
    except NoticeError as err:
        held = {"at": now, "facts": held["facts"] if held else None, "stale": True, "error": _one_line(str(err), 200), "confirmed": confirmed,
                "not_found": isinstance(err, NoticeNotFound)}  # gh ANSWERED that there is no such PR: it worked
    if budget is not None:
        budget[0] -= max(0, engine.now() - started)
    engine.notice_facts[key] = held
    return held


def facts_text(entry):
    """The facts of a notice entry as one line: `+120/-14, 5 files, CI green, approved` (what the panel draws)."""
    parts = []
    if entry.get("additions") is not None and entry.get("deletions") is not None:
        parts.append("+%d/-%d" % (entry["additions"], entry["deletions"]))
    if entry.get("files") is not None:
        parts.append("%d file%s" % (entry["files"], "" if entry["files"] == 1 else "s"))
    if entry.get("ci"):
        parts.append("CI %s" % entry["ci"] if entry["ci"] != "none" else "no CI")
    if entry.get("review"):
        parts.append(entry["review"] if entry["review"] != "none" else "no review")
    return ", ".join(parts)


LABEL_HOLD_KEEP_SECONDS = 86400   # a row kept because its list is cut or gone is dropped after a day (a pull request that merged meanwhile does not stay)


LABEL_HOLD_LIST_CUT = 200   # the daemon's open-pull-request list is read to 2 pages of 100: a list this long may be cut


def label_hold_entries(engine, now, noticed, escalated, in_play):
    """#641: [(order, entry, key)] for the open pull requests that carry the fleet's hold labels (`merge-captain` or a `hold-*`) and have NO recorded notice: a decision row on the labels
    alone, with its head and its hold reasons. Read from the open-pull-request list the daemon ALREADY holds (the ci recorder's: no read of its own, none per pull request), so only the
    daemon's passes make them. One row per pull request (a noticed one keeps its notice row); a merged, closed, excluded or unwatched one adds none; a list that failed or may be cut keeps the
    rows already shown (`engine.hold_rows`). Never raises."""
    state = getattr(engine, "ci_record_state", None)
    if state is None or not getattr(state, "lists", None):
        return []
    try:
        noticed_keys = {decision_core.pr_key(k.rpartition("#")[0], int(k.rpartition("#")[2])) for k in noticed if k.rpartition("#")[2].isdigit()}
        gates = (engine.config.get("plan_gates") or {}).get("repos") or {}
        watched = {plan_ledger.normal_repo(r) for r in decision_repos(engine)} | {plan_ledger.normal_repo(r) for r in gates}   # the list is read for the plan_gates repos
        shown, previous = {}, engine.__dict__.setdefault("hold_rows", {})
        default_repo = engine.config.get("intent_repo")
        for repo_key, (_read_at, records) in sorted(state.lists.items()):
            if repo_key not in watched:
                continue
            try:
                refuse_excluded(engine.config, repo_key)
            except AccountError:
                continue
            seen = set()
            for record in records:
                number = _number(record)
                if number is None or not isinstance(record, dict) or record.get("state", "open") != "open" or record.get("merged_at"):
                    continue
                seen.add(number)
                names = {(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []}
                if not _label_kind(names, CAPTAIN_MERGE_LABELS[:1] + CAPTAIN_HOLD_LABELS):
                    continue
                full = ((record.get("base") or {}).get("repo") or {}).get("full_name") if isinstance(record.get("base"), dict) else None
                repo = full if isinstance(full, str) and plan_ledger.normal_repo(full) == repo_key else repo_key
                head = (record.get("head") or {}).get("sha") if isinstance(record.get("head"), dict) else None
                created = parse_iso(record.get("created_at"))
                shown[(repo_key, number)] = {"repo": repo, "number": number, "title": record.get("title") if isinstance(record.get("title"), str) else "", "head": head,
                                             "since": iso(created if created is not None else now), "holds": pr_hold_reasons(names)}
            if len(records) >= LABEL_HOLD_LIST_CUT:   # the list may be cut: a held pull request beyond it is not dropped, for at most LABEL_HOLD_KEEP_SECONDS (the next complete list drops it at once)
                for key, row in previous.items():
                    if key[0] == repo_key and key[1] not in seen:
                        kept_at = row.get("kept_at", now)
                        if now - kept_at <= LABEL_HOLD_KEEP_SECONDS:
                            shown.setdefault(key, dict(row, kept_at=kept_at))
        for key in [k for k in previous if k[0] not in set(state.lists)]:
            if key[0] not in watched or load_module("repo_exclusion").is_excluded(engine.config, key[0]):
                continue   # a repository that is no longer watched, or is excluded now, keeps nothing
            kept_at = previous[key].get("kept_at", now)
            if now - kept_at <= LABEL_HOLD_KEEP_SECONDS:
                shown.setdefault(key, dict(previous[key], kept_at=kept_at))   # a repository whose list is gone for now keeps its rows, for a day at most
        engine.hold_rows = shown
        out = []
        for (repo_key, number), row in sorted(shown.items()):
            repo = row["repo"]
            if decision_core.pr_key(repo, number) in noticed_keys:
                continue   # a noticed pull request keeps its notice row: one row (the identity is folded once, `pr_key`, for both sources)
            tag = repo if len(in_play) > 1 else repo.split("/")[-1]
            captain_key = "captain:%s#%d@%s" % (repo, number, row["head"]) if row["head"] else None
            entry = decision_core.labels_entry(repo, number, tag, row, bool(captain_key and captain_key in escalated), default_repo)
            if captain_key and isinstance(engine.request_keys, set):
                engine.request_keys.add(captain_key)   # keeps its escalation through the daemon's prune
            out.append(((now, repo, number), entry, "%s#%d" % (repo, number)))
        return out
    except Exception as err:
        engine._note("decisions.hold_rows_error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
        return []


def decisions_from_notices(engine, now):
    """(entries, problems): each recorded PR notice as an entry of kind `notice` (tier `human`, id `pr:<repo>#N`).

    Not a decision, so not counted with them. The facts come from `gh pr view`, cached; a PR that is merged or
    closed is not listed and is queued in engine.notice_effects for the daemon to forget; one that cannot be read
    keeps its last facts, marked stale, and is dropped (and logged) after PR_NOTICE_UNREADABLE_SECONDS."""
    engine.notice_effects = {"drop": set(), "unreadable": {}, "observed": {}, "left": set()}
    engine.notice_unread = 0   # #796: how many noticed PRs this pass has no facts for (skipped by the time budget, or unreadable): the load alert waits while it is above 0
    read_mark(engine, "notices", False, "the notice read did not finish", now)   # #805: never inherited: whole only at the end of a read that completed
    state_dir = getattr(engine, "state_dir", None)
    if state_dir is None:
        read_mark(engine, "notices", True, "", now)
        return [], []
    try:
        items = read_pr_notices(state_dir)
    except NoticeError as err:
        engine.notice_unread = 1   # the notices themselves could not be read: nothing is known about them
        read_mark(engine, "notices", False, "the notices could not be read", now)
        return [], [str(err)]
    hold = engine.config.get("pr_notice_hold_minutes") or 0
    escalated = read_escalated(state_dir)
    # The id's repo form is decided by the repos IN PLAY (the watched ones and the notices' own), counted once each: with
    # more than one, every notice id names its repo in full (`pr:owner/repo#N`); with one, the short name. It used to count a
    # repo twice when it was both watched and noticed, so the form flipped whenever the watch set did (a restart).
    in_play = set(decision_repos(engine)) | {i["repo"] for i in items}
    entries, bad, any_ok = [], {}, False
    budget = [PR_NOTICE_PASS_BUDGET]
    order = lambda i: (i["created"], i["repo"], i["number"])
    # the least recently read go first, so a PR that hangs cannot starve the others of the budget
    seen = lambda i: engine.notice_facts.get("%s#%d" % (i["repo"], i["number"]), {"at": 0})["at"]
    for item in sorted(items, key=lambda i: (seen(i),) + order(i)):
        repo, number = item["repo"], item["number"]
        key = "%s#%d" % (repo, number)
        held = notice_facts(engine, repo, number, now, budget)
        facts = held["facts"]
        if held.get("skipped") or facts is None:
            engine.notice_unread += 1
        if facts is not None and facts["state"] in ("merged", "closed"):
            engine.notice_effects["drop"].add(key)
            engine.notice_effects["left"].add(key)   # read as merged or closed: evidence for the escalation prune
            continue
        since = item.get("unreadable_since")
        if held.get("skipped"):
            pass  # not read this pass: neither readable nor unreadable, so the unreadable clock is left alone
        elif held["stale"]:
            bad[key] = (item, held)
            if held.get("not_found"):
                any_ok = True  # a clear "no such PR" is an answer: gh and the auth work, so this pass counts toward the hour (an auth or network error does not)
            if since is None:
                engine.notice_effects["unreadable"][key] = iso(now)
        else:
            any_ok = True
            if since is not None:
                engine.notice_effects["unreadable"][key] = None
                engine.notice_effects["observed"][key] = None
        if held["stale"] and held.get("confirmed") is not None and now - held["confirmed"] > PR_OPEN_CONFIRM_SECONDS:
            continue   # it was last seen open a while ago and cannot be confirmed now: a row derived from OPEN state is not listed (the notice stays)
        tag = repo if len(in_play) > 1 else repo.split("/")[-1]
        entry = decision_core.notice_entry(repo, number, tag, facts or {}, held["stale"], item.get("from"),
                                           item["created"], item.get("note"), hold, engine.config.get("intent_repo"))
        evidence = captain_evidence(facts, set(notice_senders(item))) if facts and facts.get("captain") else []
        if evidence == [] and now - item["created"] >= CAPTAIN_NO_REVIEW_SECONDS:
            pass  # nothing stalls silently: the notice's own clock (a new head does not restart it) lists it
        elif evidence == []:
            evidence = False
        if facts and facts.get("captain") and facts.get("head") and evidence is not False:
            # a `MERGE: captain` PR is the Chief of Staff's decision, in place of the heads-up (never both)
            captain_key = "captain:%s#%d@%s" % (repo, number, facts["head"])
            entry = decision_core.captain_entry(entry, facts, captain_key in escalated, evidence)
            if isinstance(engine.request_keys, set):
                engine.request_keys.add(captain_key)  # keeps its escalation through the daemon's prune
        entries.append((order(item), entry, key))
    entries.extend(label_hold_entries(engine, now, {key for _, _, key in entries}, escalated, in_play))   # #641: held on labels alone, no notice
    # The drop counts only time the daemon OBSERVED: seconds between passes of this process in which `gh` demonstrably worked (some
    # notice read fine), added for the notices that still cannot be read. A pass where every read fails is an outage (gh, auth, the
    # network), not a bad notice: nobody counts and nobody drops. A new process starts every count again, so downtime is not counted.
    obs = getattr(engine, "notice_obs", None)
    fresh_process = obs is None
    if fresh_process:
        obs = engine.notice_obs = {"last": None}
    gap = min(PR_NOTICE_OBSERVE_MAX, max(0, now - obs["last"])) if (any_ok and obs["last"] is not None) else 0
    obs["last"] = now if any_ok else None
    dropped = set()
    for key, (item, held) in bad.items():
        if fresh_process:
            engine.notice_effects["unreadable"][key] = iso(now)
            engine.notice_effects["observed"][key] = 0
            continue
        if not any_ok:
            continue
        seconds = (item.get("unreadable_for") or 0) + gap
        engine.notice_effects["observed"][key] = seconds
        if seconds >= PR_NOTICE_UNREADABLE_SECONDS:
            engine.notice_effects["drop"].add(key)
            engine._note("notice.dropped", key, {"reason": "unreadable for %d minutes of observed time" % (PR_NOTICE_UNREADABLE_SECONDS // 60),
                                                 "error": held["error"]})
            dropped.add(key)
    read_mark(engine, "notices", not engine.notice_unread, "%d notices unread" % engine.notice_unread, now)
    return [entry for _, entry, key in sorted(entries, key=lambda row: row[0]) if key not in dropped], []

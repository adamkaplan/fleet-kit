"""The integration tip's CI is watched after each merge (ask #2247).

Nothing ran CI on the integration tip after a merge, so a merge that turned the tip red went unseen and pull requests were then merged against 'the reds the
tip already has'. This is the daemon's look at the runs of the tip branch. Per watched repository it lists the recent runs of the branch `BRANCH` (ONE read, no
oftener than LIST_SECONDS), takes the newest finished run of each head in the order the runs began, and for a head whose run FAILED reads that run's jobs (one
read) to name the failed ones. A head is noticed ONCE, to the Chief of Staff and to the lane that merged it (the pull request named by the merge commit's title,
routed like any pull request; with none found, the Chief of Staff alone), naming the merge commit, its pull request and the failed jobs, when
  - the head before it passed, or is not known (unknown counts as green: the first red seen is news), or
  - the tip was already red and this head fails a job the head before it did not (only the NEW failed jobs are named).
The same red repeated on the next head says nothing; a head whose run passed is only remembered. A cancelled or skipped run decides nothing; a run still going waits.
A failure older than WAKE_WINDOW is history: it is remembered as the baseline and told to nobody. Reads are GET only and bounded by the caller's per-pass
budget. What was handled is kept in `tip-watch.json` (state directory) so a restart tells nobody twice. A job name is untrusted text: `queue_ejections.safe` makes it data.

The logic is here; `bin/fleet-switchboard` calls `run_pass` from the daemon's pass with a namespace `sb` of its own helpers (`NEEDS`)."""

import calendar
import re
import subprocess
import time

BRANCH = "integration"   # the branch whose tip is watched
STATE_FILE = "tip-watch.json"
LIST_SECONDS = 300       # a repository's runs are listed no oftener than this
LIST_PAGE = 30
WAKE_WINDOW = 3600       # a red head older than this is history, not news
RETRY_SECONDS = 300
MAX_RANGE = 30           # merges of a range read and named
MAX_RANGE_READS = 12     # extra GETs one run may spend on its range (compare, logs, files), also bounded by the pass budget
MAX_LOG = 400000         # characters of one job log looked at
MAX_LOG_JOBS = 3         # failed jobs whose logs are read for the failing test file
MAX_PR_FILES = 100       # files of one pull request looked at
MAX_JOBS = 10            # failed job names in one line
LEDGER_MAX = 200
NEEDS = ("_RedsReader", "GitHubItem", "PROG", "_read_small", "_write_small", "_one_line", "load_module", "chiefs_of_staff")

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
PR_TITLE = re.compile(r"\(#(\d{1,9})\)|^Merge pull request #(\d{1,9})")
SHA = re.compile(r"[0-9a-f]{7,40}")
LOG_FILE = re.compile(r'File "([^"\n]{1,300}?\.py)"|((?:[\w.\-]+/)*test_?[\w.\-]*\.py)(?=::|:\d|\b)')


def _epoch(text):
    if isinstance(text, str) and ISO.fullmatch(text):
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    return None


def pr_of(run):
    """The pull request a merge commit's title names, or None."""
    message = ((run.get("head_commit") or {}).get("message") or "") if isinstance(run, dict) else ""
    return pr_in(message)


def pr_in(message):
    found = PR_TITLE.findall(str(message).split("\n", 1)[0])
    if not found:
        return None
    number = [a or b for a, b in found][-1]
    return int(number)


def failing_files(text):
    """The test files a job log names (File "..." lines and test ids), in order of first sight, repository-relative where the path shows it."""
    out = []
    for quoted, bare in LOG_FILE.findall(text[:MAX_LOG]):
        path = (quoted or bare).replace("\\", "/")
        for marker in ("/work/", "/repo/", "/checkout/", "/home/runner/work/"):
            if marker in path and "tests/" in path:
                path = path[path.index("tests/"):]
        if "tests/" in path:
            path = path[path.index("tests/"):]
        if path.startswith("test") or "/test" in path:
            if path not in out:
                out.append(path)
    return out


def touches(changed, test_file):
    """(Matching is also by file stem alone: a same-named file in another directory matches.) True when `changed` (paths a pull request touched) holds the test file or the file it tests (test_x.py tests x.py / x)."""
    stem = test_file.rsplit("/", 1)[-1][:-3].replace("-", "_")
    tested = stem[5:] if stem.startswith("test_") else stem[4:] if stem.startswith("test") else stem
    for path in changed:
        if path == test_file or path.endswith("/" + test_file) or test_file.endswith("/" + path):
            return True
        base = path.rsplit("/", 1)[-1]
        base = (base[:-3] if base.endswith(".py") else base).replace("-", "_")
        if tested and base == tested:
            return True
    return False


def newest_by_head(runs, branch=BRANCH):
    """[(began, head, run)] oldest first: the newest run of each head of `branch`; a run that is not a dict, has no usable head or time is ignored."""
    best = {}
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or run.get("head_branch") != branch or not isinstance(run.get("head_sha"), str) or not SHA.fullmatch(run["head_sha"]):
            continue
        began = _epoch(run.get("created_at"))
        if began is None or not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool):
            continue
        old = best.get(run["head_sha"])
        if old is None or (began, run["id"]) > (old[0], old[2]["id"]):
            best[run["head_sha"]] = (began, run["head_sha"], run)
    first = {}
    for run in runs if isinstance(runs, list) else []:   # a head is placed where its FIRST run began (a rerun does not move it)
        if isinstance(run, dict) and run.get("head_sha") in best and _epoch(run.get("created_at")) is not None:
            first[run["head_sha"]] = min(first.get(run["head_sha"], 1 << 60), _epoch(run["created_at"]))
    return sorted(((first[h], h, r) for _, h, r in best.values()), key=lambda t: (t[0], t[1]))


def build_line(sb_safe, repo, head, pr, failed, repeated, span=None):
    names = ", ".join('"%s"' % sb_safe(n, 100) for n in failed[:MAX_JOBS]) + (" and %d more" % (len(failed) - MAX_JOBS) if len(failed) > MAX_JOBS else "")
    what = "failed %d job%s" % (len(failed), "" if len(failed) == 1 else "s")
    lead = "The %s tip of %s is red after the merge %s" % (BRANCH, repo, head[:8]) if not repeated else \
           "The %s tip of %s was already red and the merge %s adds new failing jobs" % (BRANCH, repo, head[:8])
    via = " (pull request #%d)" % pr if pr else " (its pull request is not known)"
    if span and span.get("unread"):   # known to span several merges, the range cannot be read: nobody is blamed
        return "%s: its CI run %s: %s. The run held several merges since the last run; range unread; likeliest: unknown. Fix it forward or revert the culprit; the next merges are not to be judged against this red." % (
            lead.replace(" after the merge", " in the run of the head"), what, names or "(job names unreadable)")
    if span:   # the run held several merges: the newest head is not blamed, the range is named
        merges = "; ".join("%s #%s (%s)" % (m["sha"][:8], m["pr"], sb_safe(m["lane"], 60) if m["lane"] else "lane unknown") for m in span["merges"])
        more = " and %d more" % span["more"] if span.get("more") else ""
        likely = "likeliest: #%s (%s)" % (span["likely"]["pr"], sb_safe(span["likely"]["lane"], 60) if span["likely"]["lane"] else "lane unknown") if span.get("likely") else "likeliest: unknown"
        return "%s: its CI run %s: %s. The run held %s%d merges since the last run: %s%s. %s. Fix it forward or revert the likeliest merge; the next merges are not to be judged against this red." % (
            lead.replace(" after the merge", " in the run of the head"), what, names or "(job names unreadable)", "at least " if span.get("partial") else "", len(span["merges"]) + span.get("more", 0), merges, more, likely)
    return "%s%s: its CI run %s: %s. Fix it forward or revert that merge; the next merges are not to be judged against this red." % (lead, via, what, names or "(job names unreadable)")


def read_ledger(sb, engine):
    """{"heads": {sha: {"ts", "failed": [names] or None, "noted": bool}}, "last": sha or None}. A damaged file reads as empty."""
    raw = sb._read_small(engine.state_dir, STATE_FILE)
    heads = {}
    for sha, value in ((raw.get("heads") if isinstance(raw, dict) else None) or {}).items():
        if isinstance(sha, str) and SHA.fullmatch(sha) and isinstance(value, dict) and isinstance(value.get("ts"), (int, float)):
            failed = value.get("failed")
            heads[sha] = {"ts": value["ts"], "noted": bool(value.get("noted")), "repo": value.get("repo") if isinstance(value.get("repo"), str) else None,
                          "failed": [str(n)[:200] for n in failed[:200]] if isinstance(failed, list) else None}
    return heads


def write_ledger(sb, engine, heads):
    while len(heads) > LEDGER_MAX:
        del heads[min(heads, key=lambda k: heads[k]["ts"])]
    sent = {h: {"failed": v["failed"], "to": sorted(v["to"])} for h, v in engine.__dict__.get("tip_watch", {}).get("sent", {}).items()}
    sb._write_small(engine.state_dir, STATE_FILE, {"heads": heads, "sent": sent})


def read_sent(sb, engine):
    """{sha: {"failed": [names] or None, "to": set}}: the recipients already told of a head whose notice is not complete."""
    raw = sb._read_small(engine.state_dir, STATE_FILE)
    out = {}
    for sha, v in ((raw.get("sent") if isinstance(raw, dict) else None) or {}).items():
        if isinstance(sha, str) and SHA.fullmatch(sha) and isinstance(v, dict) and isinstance(v.get("to"), list):
            f = v.get("failed")
            out[sha] = {"failed": [str(n)[:200] for n in f[:200]] if isinstance(f, list) else None, "to": {str(t)[:100] for t in v["to"][:20]}}
    return out


def watched(sb, engine, repo):
    if sb.load_module("repo_exclusion").is_excluded(engine.config, repo):
        return False
    return repo.lower() in {r.lower() for r in engine.repos_in_play()}


def run_pass(sb, engine, now, calls):
    """One pass. `calls` is the pass's budget of gh reads. Never raises into the caller. Returns the number of reads spent."""
    hub = getattr(engine, "hub", None)
    if engine.audit is None or engine.state_dir is None or hub is None or not hasattr(hub, "route_of"):
        return 0
    discovery = getattr(engine, "discovery", None)
    if discovery is None or discovery.problems:
        return 0
    memory = engine.__dict__.setdefault("tip_watch", {"listed": {}, "runs": {}, "retry": {}, "said": set(), "sent": None})
    if memory["sent"] is None:
        memory["sent"] = read_sent(sb, engine)
    heads = read_ledger(sb, engine)
    before = {k: dict(v) for k, v in heads.items()}
    runner = getattr(engine, "gh_runner", None) or sb.subprocess.run
    safe = sb.load_module("queue_ejections").safe
    spent = 0
    for repo in sorted(engine.repos_in_play()):
        if not watched(sb, engine, repo):
            continue
        reader = sb._RedsReader(engine.config, runner, engine.now, repo)
        if now - memory["listed"].get(repo, -LIST_SECONDS) >= LIST_SECONDS and spent < calls:
            spent += 1
            memory["listed"][repo] = now
            try:
                body = reader.json("repos/%s/actions/runs?branch=%s&per_page=%d" % (repo, BRANCH, LIST_PAGE))
                memory["runs"][repo] = body.get("workflow_runs") if isinstance(body, dict) and isinstance(body.get("workflow_runs"), list) else []
            except Exception as err:   # said once per repository; listed again after the interval
                if repo not in memory["said"]:
                    memory["said"].add(repo)
                    engine.audit.append(engine.actor, "tip_watch.read_failed", repo, {"error": sb._one_line(str(err), 160)})
        previous = []   # the failed jobs of the head before
        prior = None    # the head of the run before
        known = True
        unknown_red = False   # the head before was red and its jobs could not be read
        for began, head, run in newest_by_head(memory["runs"].get(repo)):
            before_head, prior = prior, head
            if head in heads and heads[head].get("repo") in (repo, None):
                previous = heads[head]["failed"] or []
                unknown_red = heads[head]["failed"] is None
                known = not unknown_red
                continue
            if run.get("status") != "completed":
                break   # a run still going: the heads after it wait, so the order is kept
            conclusion = run.get("conclusion")
            if conclusion == "success":
                heads[head] = {"ts": began, "failed": [], "noted": True, "repo": repo}
                previous, known, unknown_red = [], True, False
                continue
            if conclusion in (None, "skipped", "cancelled", "neutral", "stale"):   # cancelled, skipped, neutral: decides nothing
                continue
            if now < memory["retry"].get(head, 0):
                break
            if head not in memory["sent"] and spent >= calls:
                break   # the rest wait for the next pass
            failed = None
            failed_ids = []
            if head not in memory["sent"]:
                spent += 1
                try:
                    done = reader.json("repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, run["id"]))
                    jobs = done.get("jobs") if isinstance(done, dict) else None
                    failed_ids = [j["id"] for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), int) and j.get("conclusion") not in (None, "success", "skipped", "cancelled", "neutral")] if isinstance(jobs, list) else []
                    failed = sorted({safe(j.get("name") or "", 200) for j in jobs if isinstance(j, dict) and j.get("conclusion") not in (None, "success", "skipped", "cancelled", "neutral")} - {""}) if isinstance(jobs, list) else None
                    if failed == []:
                        failed = None   # a red run with no readable failed job: unknown, never []
                except Exception as err:
                    memory["retry"][head] = now + RETRY_SECONDS
                    if head not in memory["said"]:
                        memory["said"].add(head)
                        engine.audit.append(engine.actor, "tip_watch.read_failed", "%s@%s" % (repo, head[:8]), {"error": sb._one_line(str(err), 160)})
                    break
            else:
                failed = memory["sent"][head]["failed"]
            news = list(failed or [])
            repeated = bool(known and previous)
            if repeated:
                news = [n for n in news if n not in previous]
            done_at = _epoch(run.get("updated_at"))
            old = now - (done_at if done_at is not None else began) > WAKE_WINDOW
            if (repeated and failed is not None and not news) or old or (unknown_red and failed is None):
                heads[head] = {"ts": began, "failed": failed, "noted": True, "repo": repo}
                previous, known, unknown_red = failed or [], failed is not None, failed is None
                continue
            budget = [min(MAX_RANGE_READS, max(0, calls - spent))]   # extra reads of this run's range, counted against the pass
            told = _tell(sb, engine, hub, discovery, memory, safe, repo, head, run, failed, news, repeated, now, reader, before_head, failed_ids, budget)
            spent += min(MAX_RANGE_READS, max(0, calls - spent)) - budget[0]
            if not told:
                break
            heads[head] = {"ts": began, "failed": failed, "noted": True, "repo": repo}
            previous, known, unknown_red = failed or [], failed is not None, failed is None
            before = _save(sb, engine, heads, before)
    pruned = {h: v for h, v in memory["sent"].items() if h not in heads}
    if len(pruned) != len(memory["sent"]):
        memory["sent"] = pruned
        before = {}
    _save(sb, engine, heads, before)
    return spent


def _lane(sb, hub, discovery, repo, head, number, now):
    if number is None:
        return None
    item = sb.GitHubItem("tip:%s" % head, "tip", "", repo, [], [("pr", number)], sb.PROG, now, numbers=[number])
    try:
        return hub.route_of(item, discovery.agents, now)[0]
    except Exception:
        return None


def _span(sb, hub, discovery, memory, repo, head, before_head, reader, failed_ids, now, budget=None):
    """None for a run of one merge; {"unread": True} when the run spans several heads but the range cannot be read (nobody is blamed); else
    {"merges": [{"sha","pr","lane"}], "more": n, "partial": bool, "likely": merge or None}. GET only; every read is taken from `budget` (a one-item list)."""
    if not before_head or before_head == head:
        return None
    cached = memory.setdefault("span", {})
    if head in cached:
        return cached[head]
    if budget is None:
        budget = [MAX_RANGE_READS]
    unread = {"unread": True}

    def get(path):
        if budget[0] <= 0:
            raise RuntimeError("range read budget spent")
        budget[0] -= 1
        return reader.json(path)

    partial = False
    try:
        base = "repos/%s/compare/%s...%s?per_page=100" % (repo, before_head, head)
        body = get(base)
        total = body.get("total_commits") if isinstance(body, dict) else None
        commits = body.get("commits") if isinstance(body, dict) else None
        if isinstance(total, int) and isinstance(commits, list) and total > len(commits):   # compare lists the OLDEST first: read the last page, the newest
            partial = True
            body = get(base + "&page=%d" % (-(-total // 100)))
            commits = body.get("commits") if isinstance(body, dict) else None
        merges = []
        for c in commits if isinstance(commits, list) else []:
            message = ((c.get("commit") or {}).get("message") or "") if isinstance(c, dict) else ""
            number = pr_in(message)
            if number is not None and isinstance(c.get("sha"), str) and SHA.fullmatch(c["sha"]):
                merges.append({"sha": c["sha"], "pr": number})
    except Exception:
        return unread
    if not merges:
        return unread
    if len(merges) < 2 and not partial:
        return None
    more = max(0, len(merges) - MAX_RANGE)
    merges = merges[-MAX_RANGE:]
    for m in merges:
        m["lane"] = _lane(sb, hub, discovery, repo, m["sha"], m["pr"], now)
    tests = []
    for job in failed_ids[:MAX_LOG_JOBS]:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            done = reader.accounts.read(reader.runner, "repos/%s/actions/jobs/%d/logs" % (repo, job), repo, reader.config["call_timeout_seconds"], stdin=subprocess.DEVNULL)
            if done.returncode == 0:
                tests += [t for t in failing_files(done.stdout if isinstance(done.stdout, str) else "") if t not in tests]
        except Exception:
            continue
    likely = None
    for m in reversed(merges) if tests else []:   # the newest matching merge first
        try:
            files = get("repos/%s/pulls/%d/files?per_page=%d" % (repo, m["pr"], MAX_PR_FILES))
        except Exception:
            likely = None   # a newer merge unread: an older match would be a guess
            break
        names = [f.get("filename") for f in files if isinstance(f, dict) and isinstance(f.get("filename"), str)] if isinstance(files, list) else []
        if any(touches(names, t) for t in tests):
            likely = m
            break
    cached[head] = {"merges": merges, "more": more, "partial": partial, "likely": likely}
    return cached[head]


def _tell(sb, engine, hub, discovery, memory, safe, repo, head, run, failed, news, repeated, now, reader=None, before_head=None, failed_ids=(), budget=None):
    """True when every recipient was told (a recipient already told is not told again). The Chief of Staff always; the merging lane too when it is known."""
    chiefs = sb.chiefs_of_staff(discovery)
    if not chiefs:
        memory["retry"][head] = now + RETRY_SECONDS
        return False
    number = pr_of(run)
    span = _span(sb, hub, discovery, memory, repo, head, before_head, reader, failed_ids, now, budget) if reader is not None else None
    if span and span.get("unread"):
        lane, number = None, None   # the Chief of Staff alone
    elif span:
        lane = span["likely"]["lane"] if span["likely"] else None   # the likeliest pull request's lane; else the Chief of Staff alone
        number = span["likely"]["pr"] if span["likely"] else None
    else:
        lane = _lane(sb, hub, discovery, repo, head, number, now)
    line = build_line(safe, repo, head, number, news, repeated, span)
    state = memory["sent"].setdefault(head, {"failed": failed, "to": set()})
    ok = True
    for to in [chiefs[0]] + ([lane] if lane and lane != chiefs[0] else []):
        if to in state["to"]:
            continue
        try:
            sent = bool(engine.send(sb.PROG, to, line).get("sent"))
        except Exception:
            sent = False
        if sent:
            state["to"].add(to)
            try:
                write_ledger(sb, engine, read_ledger(sb, engine))   # the recipients told survive a restart
            except OSError:
                pass
        else:
            ok = False
    if not ok:
        memory["retry"][head] = now + RETRY_SECONDS
        return False
    engine.audit.append(engine.actor, "tip_watch.noticed", "%s@%s" % (repo, head[:8]), {"pr": number, "to": sorted(state["to"]), "jobs": len(news), "repeated": repeated})
    return True


def _save(sb, engine, heads, before):
    if heads == before:
        return before
    try:
        write_ledger(sb, engine, heads)
    except OSError as err:
        engine._note("tip_watch.error", None, {"error": "%s not written: %s" % (STATE_FILE, sb._one_line(str(err), 100))})
        return before
    return {k: dict(v) for k, v in heads.items()}

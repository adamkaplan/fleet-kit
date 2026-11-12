"""A cloud worker that the launcher ended alerts its boss and the Chief of Staff (ask #1480).

A failed pod is alerted only when the reaper stops it at its idle or hard limit (cause pod_failed): the reaper reads a pod's activity through the pod, a
failed pod cannot answer, so the reaper does not stop it sooner (a follow-up ask is filed as #1526).

The launcher writes one JSON line per action to its audit file. This pass reads the NEW stop lines (from a stored cursor), and for a stop line that carries the field `cause` (idle,
hard, pod_gone or pod_failed; decided from that field alone), it sends ONE message to the
Chief of Staff always and ONE to the boss recorded in the worker's cloud record when that name is a valid agent name, unless the worker already
reported done. The message holds five facts, plus, for pod_failed, the pod's termination reason and exit code (added by a later change): the worker, the reason, the time, pushed (yes, no or unknown) and the run record's name (or
unknown); never an address, a log line or a transcript.

The state is one small private file (0600): {"offset": bytes already read, "sent": [[run id, cause], ...] (the last 500)}. A missing or corrupt
file starts at the END of the audit file, so old history is never replayed. The state is saved BEFORE the messages go: a crash loses a message,
it never sends one twice. For idle and hard ends one more line names the pod's last head, `branch@sha8` and its check state (#2267), from the end hook or, when that found no head, from the pod's own newest push (`last_push`, push_reader; the branch is checked against a strict pattern). Pure of the daemon: the send function, the paths, the last reports and the shared bounded parse (record_json.parse_bounded) are handed in.
"""
import datetime
import json
import os
import re
import stat

CHIEF = "chief-of-staff"
SENT_KEEP = 500          # the (run id, reason) keys kept
READ_BYTES = 1 << 20     # the most audit bytes one pass reads
FILE_BYTES = 65536       # the most a record file may be
# The launcher writes the field `cause` on every stop it makes of its own accord; the decision reads that field and nothing else (the reason text is
# never read for it: a person's words, even `idle`, send nothing). The audit file and the run records are UNTRUSTED text.
CAUSES = {"idle": "idle limit", "hard": "hard limit", "pod_gone": "pod gone", "pod_failed": "pod failed"}
AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")   # the switchboard's own agent-name pattern (NAME_RE in bin/fleet-switchboard)
WORKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECORD_STAMP = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}"   # the launcher's run record name: <worker>-<UTC stamp>-<6 hex> (cloud/launcher/run_record.py)
SAFE = re.compile(r"[^A-Za-z0-9._/:+@=-]")


def plain(value, limit=80):
    """Text made safe to show: printable ASCII of a small set only, cut short."""
    return SAFE.sub("", str(value))[:limit]


ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def when_text(value):
    """The launcher's own ISO UTC time, or the word unknown (a shape check, never a copy of other text)."""
    return value if isinstance(value, str) and ISO_UTC.fullmatch(value) else "unknown"


def read_small(path, parse):
    """A small regular file's JSON dict, or None (a link, a FIFO, a big or unreadable file all give None). Never blocks, never raises."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = parse(raw[:FILE_BYTES].decode("utf-8")) if len(raw) <= FILE_BYTES else None
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_state(path, size, parse):
    """(offset, sent keys). No file, a corrupt file or a wrong shape: the end of the audit file and no keys."""
    data = read_small(path, parse)
    offset, sent = data.get("offset") if data else None, data.get("sent") if data else None
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not isinstance(sent, list):
        return size, [], True
    keys = [(str(k[0]), str(k[1])) for k in sent if isinstance(k, list) and len(k) == 2]
    if offset > size:
        return size, keys, True   # a truncated or rotated file: start at its end, alert no old line
    return offset, keys, False


def save_state(path, offset, keys):
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"offset": offset, "sent": [list(k) for k in keys[-SENT_KEEP:]]}) + "\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def raw_dir(reason):
    """The run record path the reason names ("" when none)."""
    rec = re.search(r"run record (\S+)", reason)
    return rec.group(1) if rec else ""


def parse_reason(reason):
    """(run id, run record directory name) from the launcher's reason text; "" for what is not there."""
    run = re.search(r"run-id=([0-9A-Za-z]+)", reason)
    rec = re.search(r"run record (\S+)", reason)
    return (run.group(1) if run else ""), (os.path.basename(rec.group(1).rstrip("/")) if rec else "")


def record_name_of(worker, dirname):
    """The run record's name when it matches the launcher's pattern for this worker, else None."""
    return dirname if re.fullmatch(re.escape(worker) + "-" + RECORD_STAMP, dirname or "") else None


def pushed_text(record_dir, parse):
    """`yes`, `no` or `unknown` from the run record's end_hook; never the branch."""
    # Now (ask #2056) the answer is one of four fixed phrases, read only from the run record's end_hook: `head is on branch NAME`,
    # `unpushed work saved to NAME`, `nothing unpushed` or `unknown`. NAME is checked like a branch name.
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    hook = run.get("end_hook") if run else None
    if not isinstance(hook, dict) or not isinstance(hook.get("pushed"), bool):
        return "unknown"
    def name(v):
        return v if isinstance(v, str) and len(v) <= 100 and re.fullmatch(r"[A-Za-z0-9._/-]+", v) and not re.search(r"sk-|gh[opsu]_|github_pat|AKIA|xox|eyJ|[A-Za-z0-9]{30}", v) else None
    head, wip = hook.get("head_on"), hook.get("wip_branch")
    if head is not None and head != "unknown" and not hook["pushed"]:
        return "head is on branch " + name(head) if name(head) else "unknown"
    if hook["pushed"]:
        return "unpushed work saved to " + name(wip) if name(wip) else "unknown"
    if head is None and wip is None and not hook.get("error"):
        return "nothing unpushed"
    return "unknown"


def pr_line(record_dir, parse):
    """The end-hook ask: the one line the end hook recorded about the pull request, only if it is one of the fixed forms; else a fixed unknown."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    hook = run.get("end_hook") if run else None
    return valid_pr_note(hook) or "PR not marked ready: no answer recorded"


def valid_pr_note(hook):
    """The end hook's `pr_note` if it is one of the fixed forms, else None."""
    note = hook.get("pr_note") if isinstance(hook, dict) else None
    if isinstance(note, str) and len(note) <= 120 and re.fullmatch(r"PR (#[0-9]{1,9} marked ready(, review requested|, no review comment is configured)|not marked ready: [A-Za-z0-9 #(),._-]+)", note):
        return note
    return None


def reported_done(record_dir, parse):
    """Ask #2443: True when the run record holds the structural turn state the launcher read from the pod's own server (run.json `turn`, from cw-worker-activity)
    and it says idle, outcome succeeded, finish stop. The results file's text decides nothing here (ask #2392 read its last line)."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    turn = run.get("turn") if isinstance(run, dict) else None
    return isinstance(turn, dict) and turn.get("turn") == "idle" and turn.get("outcome") == "succeeded" and turn.get("finish") == "stop"


def message_text(worker, label, when, pushed, record_name, kept=False, failure=None, done=False):
    if done:   # a pod that finished and sat idle is not a failure
        text = ("[cloud worker ended] %s ended idle after a succeeded turn (the server reported the last turn succeeded with finish stop); the launcher stopped it at its %s; at %s; %s; run record: %s."
                % (worker, label, when, pushed, record_name or "unknown"))
    else:
        text = ("[cloud worker ended] %s was ended by the launcher: reason %s; at %s; %s; run record: %s."
                % (worker, label, when, pushed, record_name or "unknown"))
    if kept:
        text += " The end hook could not push: the pod was kept and the work may be unpushed."
    if failure is not None:
        text += " Pod termination: %s." % failure
    return text


def chief_not_needed(cause, ok, pushed):
    """#2202: True for an end that needs nothing from the Chief of Staff: a clean reason (idle or hard limit), the stop done, and nothing unpushed."""
    return cause in ("idle", "hard") and ok is True and pushed == "nothing unpushed"


def retired_boss(cloud_dir, worker, parse):
    """The boss kept at a worker's removal (<state>/cloud-boss/<worker>.json, bin/cloud_retired.py keep_boss, #1546), or None."""
    found = read_small(os.path.join(os.path.dirname(str(cloud_dir).rstrip("/")), "cloud-boss", worker + ".json"), parse)
    if isinstance(found, dict) and found.get("worker") == worker and isinstance(found.get("boss"), str):
        return found["boss"]
    return None


BRANCH_OK = re.compile(r"[A-Za-z0-9._/-]{1,100}")
SECRETISH = re.compile(r"sk-|gh[opsu]_|github_pat|AKIA|xox|eyJ|[A-Za-z0-9]{30}")


FULL_SHA = re.compile(r"[0-9a-f]{40}")


def clean_branch(branch):
    """The branch text if it is safe to put in a notice (one name, no secret shape), else None."""
    if isinstance(branch, str) and BRANCH_OK.fullmatch(branch) and branch != "unknown" and not SECRETISH.search(branch):
        return branch
    return None


UNPROVEN = ("", "")   # the reader's answer when its window cannot show whether the pod pushed (a long audit, no `cloud.created` of the pod in view)


def push_reader(audit_path, parse):
    """The `last_push` reader of scan_audit: worker -> (branch, full sha) of the pod's newest `push_watch.sent` line of the switchboard audit, or None.
    Reads the last READ_BYTES of a regular file only; an audit that cannot be read at all (none configured, missing, a link or pipe, a read error) gives None: a deliberate exception, the reader then claims nothing and the notice says what the end hook shows, as before.
    A later `cloud.created` line of the same name starts a new pod: its older pushes are dropped. For a readable audit, "nothing pushed" (None) is the answer ONLY when the
    pod's own `cloud.created` line is in view and no push of the pod follows it; when it is not in view (a long file, a rotated archive, a missing line)
    and no push is found, the answer is UNPROVEN (the notice says "head unknown"), whatever the file's size."""
    def read(worker):
        if audit_path is None:
            return None
        try:
            fd = os.open(str(audit_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, TypeError, ValueError):
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            size = info.st_size
            os.lseek(fd, max(0, size - READ_BYTES), os.SEEK_SET)
            chunk = os.read(fd, READ_BYTES)
        except OSError:
            return None
        finally:
            os.close(fd)
        found, wip, covered = None, None, False
        for raw in chunk.split(b"\n"):
            if b"push_watch." not in raw and b"cloud.created" not in raw:
                continue
            try:
                item = parse(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, TypeError):
                continue
            if not isinstance(item, dict) or item.get("subject") != worker:
                continue
            if item.get("event") == "cloud.created":
                found, wip, covered = None, None, True
                continue
            detail = item.get("detail")
            if item.get("event") not in ("push_watch.sent", "push_watch.seen") or not isinstance(detail, dict):
                continue
            branch, sha = clean_branch(detail.get("branch")), detail.get("head")
            if branch and isinstance(sha, str) and FULL_SHA.fullmatch(sha):
                if branch == worker + "/wip":
                    wip = (branch, sha)     # the reaper's save: only when the pod has no other push
                else:
                    found = (branch, sha)   # later lines are newer
        return found or wip or (None if covered else UNPROVEN)
    return read


def own_branch_head(record_dir, repo, record, parse, branch_tip):
    """(branch, full sha) when the pod's task branch (the record's --branch) stands, on GitHub, at the very sha the pod ended on (run.json head_sha), else None.
    The one proof that needs no push line: a pod whose pushes the push watch did not audit (pushed between two ticks, a sha already told under the wip branch). Any failure is None."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    branch, sha = (record or {}).get("branch"), run.get("head_sha") if isinstance(run, dict) else None
    if branch_tip is None or not clean_branch(branch) or not ref_shape(branch) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*", repo if isinstance(repo, str) else "") or branch in NOT_OWN or branch == (record or {}).get("default_branch"):
        return None
    if not (isinstance(sha, str) and FULL_SHA.fullmatch(sha)) or not (isinstance(repo, str) and repo):
        return None
    try:
        return (branch, sha) if branch_tip(repo, branch) == sha else None
    except Exception:
        return None


NOT_OWN = ("main", "master", "integration", "HEAD")


def ref_shape(branch):
    """True for a branch name that is one plain ref path: no empty, `.`-leading or `.lock` segment, no `..`, no edge slash (it is put in a GitHub API path)."""
    parts = branch.split("/")
    return ".." not in branch and all(p and not p.startswith(".") and not p.endswith((".lock", ".")) for p in parts)


def head_line(record_dir, repo, head_checks, parse, pushed=None, worker=None, record=None, branch_tip=None):
    """#2267: the last head the pod pushed (branch, short sha) and its check state; `head_checks(repo, sha)` is handed in (this module reads no network).
    `pushed` is the pod's newest own push (branch, sha) from the switchboard audit: used when the end hook found nothing or no head."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    hook = run.get("end_hook") if isinstance(run, dict) else None
    if not (pushed and pushed != UNPROVEN and pushed[0] != "%s/wip" % worker):   # no audited real push: the task branch standing at the pod's own head is one
        own = own_branch_head(record_dir, repo, record, parse, branch_tip)
        if own:
            return finish_head(repo, own[0], own[1], head_checks)
    if isinstance(hook, dict) and hook.get("head_on") is None and hook.get("wip_branch") is None and not hook.get("error"):
        if not pushed:
            # a PR marked ready proves a push the push watch may not see (a branch of the pod's own choosing): not "nothing pushed"
            note = valid_pr_note(hook)
            return "Last head: head unknown." if note and note.startswith("PR #") else "Last head: nothing pushed."
        return "Last head: head unknown." if pushed == UNPROVEN else finish_head(repo, pushed[0], pushed[1], head_checks)
    branch = hook.get("head_on") if isinstance(hook, dict) else None
    sha = run.get("head_sha") if isinstance(run, dict) else None
    wip = hook.get("wip_branch") if isinstance(hook, dict) and branch is None else None
    if clean_branch(wip) and pushed and pushed != UNPROVEN and pushed[0] != "%s/wip" % worker:
        return finish_head(repo, pushed[0], pushed[1], head_checks)   # a real push of the pod beats the wip save
    if clean_branch(wip):
        if not (isinstance(sha, str) and FULL_SHA.fullmatch(sha)):
            return "Last head: wip branch %s, sha unknown." % wip
        branch = "wip branch " + wip
    elif not (clean_branch(branch) and isinstance(sha, str) and FULL_SHA.fullmatch(sha)):
        return finish_head(repo, pushed[0], pushed[1], head_checks) if pushed and pushed != UNPROVEN else "Last head: head unknown."
    return finish_head(repo, branch, sha, head_checks)


def finish_head(repo, branch, sha, head_checks):
    """The head line for a clean branch and a full sha: the check state is read through the handed-in reader."""
    try:
        state = head_checks(repo, sha) if isinstance(repo, str) and repo else None
    except Exception:
        state = None
    state = state if isinstance(state, str) and state else "unknown"
    return "Last head: %s@%s, checks %s." % (branch, sha[:8], re.sub(r"[^A-Za-z0-9 :,._/#()-]", "?", state)[:200])


def scan_audit(audit_file, state_file, cloud_dir, last_reports, send, parse, audit=None, last_text=None, head_checks=None, last_push=None, branch_tip=None):
    """One pass. `send(recipient, text)` is the daemon's own send path. Returns [(recipient, text)] handed to it. `last_text(directory, worker)` (ask #1788)
    gives (block, None) or (None, why): the boss's copy, not the Chief of Staff's, ends with the pod's last text, framed as untrusted.
    `last_push(worker)` gives (branch, full sha) of the pod's newest own push or None (see push_reader)."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        return []
    offset, keys, fresh = load_state(state_file, size, parse)
    if fresh:
        save_state(state_file, size, keys)
        return []
    if offset >= size:
        if offset != size:
            save_state(state_file, offset, keys)
        return []
    try:
        with open(str(audit_file), "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(READ_BYTES)
    except OSError:
        return []
    cut = chunk.rfind(b"\n") + 1
    if cut == 0:
        cut = len(chunk) if len(chunk) >= READ_BYTES else 0   # a line too long to be ours is skipped; a half-written one waits
    out = []
    for raw in chunk[:cut].split(b"\n"):
        try:
            line = parse(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(line, dict) or line.get("action") != "stop" or not isinstance(line.get("ok"), bool):
            continue
        cause, worker = line.get("cause"), line.get("worker")
        label = CAUSES.get(cause) if isinstance(cause, str) else None
        reason = line.get("reason") if isinstance(line.get("reason"), str) else ""
        if label is None or not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
            continue
        run_id, record_name = parse_reason(reason)
        hour = re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}", str(line.get("ts")))
        if not run_id and not hour:
            continue
        key = (run_id or "%s@%s" % (worker, hour.group(0)), cause)   # no run id: worker, cause and the hour
        if key in keys:
            continue
        if (last_reports.get(worker) or {}).get("state") == "done":
            continue
        record = read_small(os.path.join(str(cloud_dir), worker + ".json"), parse)
        boss = record.get("reports_to") if record and record.get("worker") == worker else None
        if boss is None and not record:
            boss = retired_boss(cloud_dir, worker, parse)   # the live record is gone: the boss the launcher kept at the removal
        name = record_name_of(worker, record_name)
        pushed = pushed_text(raw_dir(reason), parse) if name else "unknown"
        done = bool(name) and cause in ("idle", "hard") and reported_done(raw_dir(reason), parse)
        text = message_text(worker, label, when_text(line.get("ts")), pushed, name, line["ok"] is False, None, done)
        text += " " + pr_line(raw_dir(reason), parse) + "."
        if head_checks is not None and cause in ("idle", "hard"):
            try:
                own = last_push(worker) if last_push is not None else None
            except Exception:
                own = None   # fail mode: no own push is known, the line says what the end hook shows (as before this reader)
            text += " " + head_line(raw_dir(reason) if name else "", (record or {}).get("repo"), head_checks, parse, own, worker, record, branch_tip)
        keys.append(key)
        to_list = [CHIEF] + ([boss] if isinstance(boss, str) and AGENT_NAME.fullmatch(boss) and boss != CHIEF else [])
        boss_text = text
        if len(to_list) > 1 and last_text is not None:
            try:
                block, why = last_text(raw_dir(reason) if name else "", worker)
            except Exception as err:
                block, why = None, type(err).__name__
            boss_text = text + "\n" + (block if block else "last text not read: %s" % plain((why or "unknown").replace(" ", "_"), 120))
        if len(to_list) > 1 and chief_not_needed(cause, line["ok"], pushed):
            to_list = to_list[1:]   # #2202: a clean end with nothing unpushed goes to the boss only
        out.extend((to, text if to == CHIEF else boss_text, worker, label) for to in reversed(to_list))
    save_state(state_file, offset + cut, keys)
    failed = set()
    for to, text, worker, label in out:
        try:
            result = send(to, text)
            ok = not (result is False or (isinstance(result, dict) and result.get("sent") is False))
        except Exception:
            ok = False
        if not ok and (worker, label) not in failed:
            failed.add((worker, label))
            try:
                if audit is not None:
                    audit(worker, {"reason": label})   # the worker and the reason only, never the message text
            except Exception:
                pass
    return [(to, text) for to, text, _w, _l in out]


# ---- ask #1484: a cloud worker that ended its turn and waits tells its recorded boss (the same sender, state file, keys and bound as above) ----
TURN_HOLD = 60          # seconds a turn end is held: a new turn within it cancels the message (a tool-driven continuation)
TURN_CAUSE = "turn_end"  # the second half of the stored key (worker@last update, turn_end)


def turn_message_text(worker, when):
    """Five facts: the worker, the time, the fixed words, how to read the answer and the idle limit. Never the worker's own text."""
    return ("[cloud worker waiting] %s ended its turn and waits for an instruction; at %s; read its answer with: fleet-switchboard read %s;"
            " the idle limit will stop it if nothing is sent." % (worker, when, worker))


def scan_turns(workers, activity, state_file, audit_file, send, parse, now, memory, hold=TURN_HOLD):
    """One pass over the live cloud workers [(worker, boss)]. `activity(worker)` gives (busy, last update in epoch seconds) or None (unreadable: skipped).
    `memory` is the daemon's own dict, kept between passes. One message per turn end, held `hold` seconds; sent through `send(recipient, text)`
    to the recorded boss when it is a valid agent name, else to the Chief of Staff alone. Returns [(recipient, text)] handed to it."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        size = 0
    offset, keys, _fresh = load_state(state_file, size, parse)
    due = []
    for worker, boss in workers:
        if not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
            continue
        try:
            act = activity(worker)
        except Exception:
            act = None
        if not act:
            continue
        busy, updated = act
        mem = memory.setdefault(worker, {"busy": False, "seen": None, "pending": None})
        if busy:
            mem["busy"], mem["pending"] = True, None   # a new turn cancels a pending message
            continue
        if isinstance(updated, bool) or not isinstance(updated, (int, float)):
            continue
        ended = mem["busy"] or (mem["seen"] is not None and updated > mem["seen"])
        mem["busy"], mem["seen"] = False, updated
        if ended and mem["pending"] is None:
            mem["pending"] = (updated, now)
        pending = mem["pending"]
        if pending and now - pending[1] >= hold:
            mem["pending"] = None
            key = ("%s@%d" % (worker, pending[0]), TURN_CAUSE)
            if key in tuple(keys):
                continue
            keys.append(key)
            when = datetime.datetime.fromtimestamp(pending[0], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            to = boss if isinstance(boss, str) and AGENT_NAME.fullmatch(boss) else CHIEF
            due.append((to, turn_message_text(worker, when)))
    if due:
        save_state(state_file, offset, keys)   # saved BEFORE the messages go, as above
        for to, text in due:
            try:
                send(to, text)
            except Exception:
                pass
    return due


# ---- ask #2399: the stall watch's alerts. The reaper has no agent identity and cannot run `send`: it writes ONE `stall-alert` audit line per alert and this
# pass (the daemon's own send path) tells the recorded boss and the Chief of Staff, once per line. The audit file is UNTRUSTED text: every field is checked. ----
STALL_ACTION = "stall-alert"
STALL_CONDITIONS = ("progress-stale", "refusal", "failed-run-idle")   # the fixed three (cloud/launcher/stall_watch.py CONDITIONS)
STALL_ASK = "adamkaplan/fleet-kit#2399"
STALL_EVIDENCE_MAX = 300
ASK_SHAPE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}#)?[1-9][0-9]{0,9}")


def stall_evidence(value):
    """The evidence as one printable line of at most STALL_EVIDENCE_MAX characters (control characters and line breaks become spaces); '' when it is not text."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:STALL_EVIDENCE_MAX]


def stall_text(worker, condition, evidence, when, ask):
    return "[stall] %s %s: %s; at %s; read it with: fleet-switchboard read %s; ask %s" % (worker, condition, evidence, when, worker, ask)


def scan_stall(audit_file, state_file, send, parse):
    """One pass over the NEW `stall-alert` audit lines (own cursor and sent keys in state_file, as scan_audit): per valid line ONE message to the Chief of Staff and
    ONE to the boss the line names (when a valid agent name and not the Chief), through `send(recipient, text)`. The state is saved BEFORE the messages go: a crash loses
    a message and never sends one twice. A line with a bad worker, time, condition or evidence sends nothing. Returns [(recipient, text)] handed to send."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        return []
    offset, keys, fresh = load_state(state_file, size, parse)
    if fresh:
        save_state(state_file, size, keys)
        return []
    if offset >= size:
        if offset != size:
            save_state(state_file, offset, keys)
        return []
    try:
        with open(str(audit_file), "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(READ_BYTES)
    except OSError:
        return []
    cut = chunk.rfind(b"\n") + 1
    if cut == 0:
        cut = len(chunk) if len(chunk) >= READ_BYTES else 0
    out = []
    for raw in chunk[:cut].split(b"\n"):
        if b"stall-alert" not in raw:
            continue
        try:
            line = parse(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(line, dict) or line.get("action") != STALL_ACTION or line.get("ok") is not True:
            continue
        worker, condition, ts, rid = line.get("worker"), line.get("condition"), line.get("ts"), line.get("runtime_id")
        if not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker) or condition not in STALL_CONDITIONS:
            continue
        if when_text(ts) == "unknown" or not isinstance(rid, str) or not re.fullmatch(r"pod/[A-Za-z0-9._-]{1,80}", rid):
            continue
        ev = stall_evidence(line.get("evidence"))
        if not ev:
            continue
        key = ("%s@%s" % (rid, ts), condition)
        if any(key == k for k in keys):
            continue
        keys.append(key)
        ask = line.get("ask")
        ask = ask if isinstance(ask, str) and ASK_SHAPE.fullmatch(ask) else STALL_ASK
        names = line.get("recipients") if isinstance(line.get("recipients"), list) else []
        boss = next((n for n in names if isinstance(n, str) and AGENT_NAME.fullmatch(n) and n != CHIEF), None)
        text = stall_text(worker, condition, ev, ts, ask)
        out.extend((to, text) for to in [CHIEF] + ([boss] if boss else []))
    save_state(state_file, offset + cut, keys)
    for to, text in out:
        try:
            send(to, text)
        except Exception:
            pass
    return out


# ---- ask #2443: the idle notice. The reaper's pass reads each pod's own server and writes ONE `idle-notice` audit line per idle of a session (cloud/launcher/stall_watch.py
# idle_notice); this pass adds the pod's last report of that turn (the `fleet-switchboard report done|failed|blocked "<line>"` call, last-reports.json) and tells the recorded boss
# (the Chief of Staff when none), once per line. The report is the finish signal; the server's outcome and finish, the results file and the commits ahead are EVIDENCE only.
# The audit file is UNTRUSTED text: every field is checked. ----
IDLE_ACTION = "idle-notice"
IDLE_OUTCOMES = ("succeeded", "failed", "interrupted")
IDLE_FINISHES = ("stop", "length", "tool-calls", "content-filter", "error", "unknown")
IDLE_REPORT_MAX = 300
IDLE_KEY = re.compile(r"msg_[A-Za-z0-9_]{1,80}|u[0-9]{1,20}")
REPORT_SKEW = 10   # seconds: the pod's clock and the daemon's differ by a little


def idle_report_text(worker, line, last_reports):
    """`report <state>: "<line>"` when the worker made a report after its turn began, else `idle with no report: assess`. The line is the stored, already redacted report text."""
    entry = last_reports.get(worker) if isinstance(last_reports, dict) else None
    start = line.get("turn_start")
    at = entry.get("at") if isinstance(entry, dict) else None
    if (isinstance(entry, dict) and isinstance(entry.get("state"), str) and re.fullmatch(r"[a-z]{1,12}", entry["state"]) and isinstance(at, (int, float)) and not isinstance(at, bool)
            and isinstance(start, (int, float)) and not isinstance(start, bool) and at >= start - REPORT_SKEW):
        said = stall_evidence(entry.get("text")) if isinstance(entry.get("text"), str) else ""
        return 'report %s%s' % (entry["state"], ': "%s"' % said[:IDLE_REPORT_MAX] if said else "")
    return "idle with no report: assess"


def idle_text(worker, line, report, when, ask):
    unknown = line.get("state") == "unknown"
    head = "[idle] %s idle state unknown: assess" % worker if unknown else "[idle] %s went idle (outcome %s, finish %s): %s" % (worker, line.get("outcome") or "none", line.get("finish") or "none", report)
    ev = stall_evidence(line.get("evidence"))
    jobs = line.get("jobs") if isinstance(line.get("jobs"), list) else []
    jobs = [j for j in jobs if isinstance(j, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", j)][:5]
    ahead = line.get("ahead")
    res = line.get("results_exists")
    deliverable = "results file %s, %s" % ("exists" if res is True else "absent" if res is False else "not read",
                                           "%d commit(s) ahead of the default branch" % ahead if isinstance(ahead, int) and not isinstance(ahead, bool) and 0 <= ahead < 10**6 else "branch not read")
    return "%s; last text: %s; cw-run jobs: %s; evidence only: %s; at %s; read it with: fleet-switchboard read %s; ask %s" % (
        head, '"%s"' % ev if ev else "(none)", ", ".join(jobs) or "none", deliverable, when, worker, ask)


def scan_idle(audit_file, state_file, last_reports, send, parse):
    """One pass over the NEW `idle-notice` audit lines (own cursor and sent keys in state_file): per valid line ONE message to the recorded boss (a valid agent name), else to the
    Chief of Staff, through `send(recipient, text)`. The state is saved BEFORE the message goes. A line with a bad worker, time, runtime id, key or state sends nothing.
    Returns [(recipient, text)] handed to send."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        return []
    offset, keys, fresh = load_state(state_file, size, parse)
    if fresh:
        save_state(state_file, size, keys)
        return []
    if offset >= size:
        if offset != size:
            save_state(state_file, offset, keys)
        return []
    try:
        with open(str(audit_file), "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(READ_BYTES)
    except OSError:
        return []
    cut = chunk.rfind(b"\n") + 1
    if cut == 0:
        cut = len(chunk) if len(chunk) >= READ_BYTES else 0
    out = []
    for raw in chunk[:cut].split(b"\n"):
        if b"idle-notice" not in raw:
            continue
        try:
            line = parse(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(line, dict) or line.get("action") != IDLE_ACTION or line.get("ok") is not True:
            continue
        worker, ts, rid, idle = line.get("worker"), line.get("ts"), line.get("runtime_id"), line.get("idle")
        if not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker) or when_text(ts) == "unknown" or line.get("state") not in ("idle", "unknown"):
            continue
        if not isinstance(rid, str) or not re.fullmatch(r"pod/[A-Za-z0-9._-]{1,80}", rid) or not isinstance(idle, str) or not IDLE_KEY.fullmatch(idle):
            continue
        if line["state"] == "idle" and (line.get("outcome") not in IDLE_OUTCOMES or line.get("finish") not in IDLE_FINISHES + (None,)):
            continue
        key = ("%s@%s" % (rid, idle), "idle")
        if any(key == k for k in keys):
            continue
        keys.append(key)
        ask = line.get("ask")
        ask = ask if isinstance(ask, str) and ASK_SHAPE.fullmatch(ask) else STALL_ASK
        names = line.get("recipients") if isinstance(line.get("recipients"), list) else []
        boss = next((n for n in names if isinstance(n, str) and AGENT_NAME.fullmatch(n)), None)
        out.append((boss or CHIEF, idle_text(worker, line, idle_report_text(worker, line, last_reports), ts, ask)))
    save_state(state_file, offset + cut, keys)
    for to, text in list(out):
        try:
            send(to, text)
        except Exception:
            pass
    return out

"""`reds`: classify a pull request's failing tests (ask #342, slice 1).

CI uploads one `failing-tests` artifact per run holding `failing-tests.json` = {"v": 1, "run": <id>, "head": "<sha>", "complete": true|false,
"failed": ["<path>::<leaf>" or "<leaf>", ...]} (at most 2000 names of at most 200 characters; `complete` false when the run ended before all tests ran).
Given a pull request this reads the PR's own run and MAIN's recent runs (the runs of the repo's reference events: `push` by default; `merge_group` and
`schedule` for a repo with no push trigger), and says which failing names also fail on main's latest run, which are flaky there (2 or more of main's last 5 runs that are usable) and which are new. Read only; bounded calls; every finished run's answer is cached by run id.

  - A run with no artifact, an expired one, an unreadable one or `complete` false is UNKNOWN: it never counts as passing anything.
  - The PR's own list is the PR's claim: its `complete` flag never judges main, and main is only ever judged by main's own runs.
  - Names are untrusted text: bounded, control characters stripped, never executed and never used in a path.
  - Names match in full first; else a leaf (the part after the first `::`) that is UNIQUE within the run's list matches and is marked "matched by name only";
    a leaf that is not unique stays unmatched, never guessed.

Pure of the switchboard: the caller hands it a reader with `json(path)` and `zip(path)` (GET only), the cache directory and the clock."""

import calendar
import io
import json
import os
import re
import time
import urllib.parse
import zipfile

ARTIFACT = "failing-tests"
MEMBER = "failing-tests.json"
NAMES_MAX = 2000
NAME_MAX = 200
ZIP_MAX = 2 * 1024 * 1024          # bytes of an artifact downloaded
JSON_MAX = 1024 * 1024             # bytes of the member read out of it
MAIN_RUNS = 5                      # main's last runs that count
FLAKY_MIN = 2                      # failures among them that make a name flaky
LIST_PAGE = 30                     # runs asked for per reference event
UNKNOWN_TTL = 6 * 3600             # "no artifact yet" is remembered this long (a run may still be uploading)
CACHE_MAX = 300                    # cache files kept
DEFAULT_EVENTS = ["push"]
EVENTS_OK = ("push", "merge_group", "schedule", "workflow_dispatch", "pull_request")
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNKNOWN = 0, 1, 64, 3   # EXIT_UNKNOWN: it could not classify for want of evidence (never 'new' on no evidence)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class RedsError(Exception):
    """A read that failed or an answer that cannot be used: said, and nothing is classified."""


def clean(text, limit=NAME_MAX):
    """A name as shown: control characters (and line separators) become a space, cut to `limit`."""
    return _CONTROL.sub(" ", str(text))[:limit]


def settings(raw):
    """`reds.reference_events` = {"owner/repo": ["merge_group", "schedule"]}; ValueError for a bad shape. Returns the map."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - {"reference_events"}:
        raise ValueError("'reds' must be an object with only \"reference_events\" in it, got %r" % (raw,))
    events = raw.get("reference_events", {})
    if not isinstance(events, dict):
        raise ValueError("'reds.reference_events' must map OWNER/REPO to a list of events, got %r" % (events,))
    for repo, listed in events.items():
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("'reds.reference_events' key %r is not OWNER/REPO" % (repo,))
        if not isinstance(listed, list) or not listed or len(listed) > 3 or any(e not in EVENTS_OK for e in listed) or len(set(listed)) != len(listed):
            raise ValueError("'reds.reference_events' for %s must be 1 to 3 different events of %s, got %r" % (repo, ", ".join(EVENTS_OK), listed))
    return events


def parse_artifact(raw):
    """(complete, [names]) from the bytes of failing-tests.json, or raises RedsError. At most NAMES_MAX names, each cleaned and cut."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RedsError("the artifact is not JSON") from None
    if not isinstance(body, dict) or body.get("v") != 1 or not isinstance(body.get("failed"), list) or not isinstance(body.get("complete"), bool):
        raise RedsError("the artifact is not failing-tests v1")
    names = []
    for item in body["failed"][:NAMES_MAX]:
        if isinstance(item, str) and item.strip():
            names.append(clean(item))
    return body["complete"], names


def read_zip(blob):
    """The bytes of failing-tests.json inside a downloaded artifact (a zip), bounded; RedsError when it is not there or too big."""
    if isinstance(blob, str):
        raise RedsError("the artifact download was not binary")
    if len(blob) > ZIP_MAX:
        raise RedsError("the artifact is larger than %d bytes" % ZIP_MAX)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [i for i in archive.infolist() if i.filename == MEMBER]
            if not names:
                raise RedsError("the artifact has no %s" % MEMBER)
            if names[0].file_size > JSON_MAX:
                raise RedsError("%s is larger than %d bytes" % (MEMBER, JSON_MAX))
            with archive.open(names[0]) as member:
                data = member.read(JSON_MAX + 1)
            if len(data) > JSON_MAX:
                raise RedsError("%s is larger than %d bytes" % (MEMBER, JSON_MAX))
            return data
    except zipfile.BadZipFile:
        raise RedsError("the artifact is not a zip") from None


class Cache:
    """Parsed answers by run id, in the state directory (0600, bounded). A known answer is immutable; "no artifact" is remembered for UNKNOWN_TTL."""

    def __init__(self, directory, repo, now):
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
        self.dir = os.path.join(str(directory), "reds-cache", slug) if directory is not None else None
        self.now = now

    def _path(self, run_id):
        return os.path.join(self.dir, "%d.json" % run_id)

    def get(self, run_id):
        if self.dir is None:
            return None
        try:
            with open(self._path(run_id), "rb") as handle:
                body = json.loads(handle.read(JSON_MAX))
        except (OSError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        if "unknown" in body:
            return body if self.now() - float(body.get("at", 0)) < UNKNOWN_TTL else None
        if isinstance(body.get("complete"), bool) and isinstance(body.get("failed"), list):
            return {"complete": body["complete"], "failed": [str(n) for n in body["failed"][:NAMES_MAX]]}
        return None

    def put(self, run_id, entry):
        if self.dir is None:
            return
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            temp = "%s.%d.tmp" % (self._path(run_id), os.getpid())
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(dict(entry, at=self.now()), sort_keys=True))
            os.replace(temp, self._path(run_id))
            files = sorted((os.path.getmtime(os.path.join(self.dir, f)), f) for f in os.listdir(self.dir) if f.endswith(".json"))
            for _, old in files[:max(0, len(files) - CACHE_MAX)]:
                os.unlink(os.path.join(self.dir, old))
        except OSError:
            pass   # a cache that cannot be written only costs calls


def leaf_of(name):
    return name.split("::", 1)[1] if "::" in name else name


class Index:
    """One run's failed names, ready to match: full names, and the leaves that are unique."""

    def __init__(self, names):
        self.full = set(names)
        counts = {}
        for name in names:
            counts[leaf_of(name)] = counts.get(leaf_of(name), 0) + 1
        self.unique = {leaf_of(n): n for n in names if counts[leaf_of(n)] == 1}

    def match(self, name):
        """"full", "leaf" (a unique leaf: matched by name only) or None."""
        if name in self.full:
            return "full"
        if leaf_of(name) in self.unique:
            return "leaf"
        return None


def classify(pr_names, runs):
    """`runs`: main's usable runs, newest first, each {"names": [...], "latest": bool}. Returns {name: (label, count, usable, by_name_only)} where label is
    "main" (fails on main's latest run, when that run is usable), "flaky" (FLAKY_MIN or more of the usable runs) or "new"."""
    indexes = [Index(r["names"]) for r in runs]
    out = {}
    for name in pr_names:
        hits = [ix.match(name) for ix in indexes]
        count = sum(1 for h in hits if h)
        by_name = any(h == "leaf" for h in hits) and not any(h == "full" for h in hits)
        latest = bool(runs) and runs[0].get("latest") and hits[0]
        label = "main" if latest else "flaky" if count >= FLAKY_MIN else "new"
        out[name] = (label, count, len(runs), by_name and count > 0)
    return out


def _why(reason):
    """A run's unknown reason for the 'no usable reference run' line."""
    if reason.startswith("no artifact"):
        return "no artifact: expired or never uploaded"
    if reason.startswith("incomplete"):
        return "incomplete: not all tests ran"
    return "unreadable: " + clean(reason, 40)


def _age(seconds):
    seconds = max(0, int(seconds))
    return "%ds" % seconds if seconds < 120 else "%dm" % (seconds // 60) if seconds < 7200 else "%dh" % (seconds // 3600) if seconds < 172800 else "%dd" % (seconds // 86400)


def _when(text, now):
    try:
        return now - calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def reference_runs(reader, repo, default_branch, events, newest):
    """main's last runs of the reference events, newest first: [(run id, event, sha, created)]. One list call per event."""
    found = []
    for event in events:
        query = "repos/%s/actions/runs?event=%s&status=completed&per_page=%d" % (repo, event, LIST_PAGE)
        if event != "merge_group":
            query += "&branch=%s" % urllib.parse.quote(default_branch, safe="")
        body = reader.json(query)
        for run in body.get("workflow_runs", []) if isinstance(body, dict) else []:
            if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                continue
            branch = run.get("head_branch") or ""
            if event == "merge_group":
                if not branch.startswith("gh-readonly-queue/%s/" % default_branch):
                    continue
            elif branch != default_branch:
                continue
            found.append((run["id"], event, str(run.get("head_sha") or "")[:40], str(run.get("created_at") or "")))
    found.sort(key=lambda r: r[3], reverse=True)
    return found[:newest]


ARTIFACT_PAGE = 100


def artifact_map(reader, repo):
    """({run id: (artifact id, size in bytes)}, truncated) for the repo's unexpired failing-tests artifacts: ONE list call of the newest ARTIFACT_PAGE.
    `truncated` is true when the page was full: a run missing from it may simply be older than the page."""
    body = reader.json("repos/%s/actions/artifacts?name=%s&per_page=%d" % (repo, ARTIFACT, ARTIFACT_PAGE))
    items = body.get("artifacts", []) if isinstance(body, dict) else []
    found = {}
    for item in items:
        run = (item.get("workflow_run") or {}) if isinstance(item, dict) else {}
        if isinstance(item, dict) and not item.get("expired") and isinstance(item.get("id"), int) and isinstance(run.get("id"), int):
            size = item.get("size_in_bytes")
            found.setdefault(run["id"], (item["id"], size if isinstance(size, int) and not isinstance(size, bool) else None))
    return found, len(items) >= ARTIFACT_PAGE


def load_run(reader, repo, run_id, artifacts, cache, listing):
    """{"complete", "failed"} or {"unknown": reason} for one run: the cache, else the artifact (one download)."""
    hit = cache.get(run_id)
    if hit is not None:
        return hit
    if listing[0] is None:
        listing[0] = artifact_map(reader, repo)
    found, truncated = listing[0]
    artifact = found.get(run_id)
    if artifact is None:
        entry = {"unknown": "no artifact in the newest %d" % ARTIFACT_PAGE if truncated else "no artifact"}
        cache.put(run_id, entry)
        return entry
    artifact, size = artifact
    if size is None or size > ZIP_MAX:   # refused BEFORE the download: the cap must bound what is fetched, not what is kept
        entry = {"unknown": "artifact too large or of unknown size"}
        cache.put(run_id, entry)
        return entry
    try:
        complete, names = parse_artifact(read_zip(reader.zip("repos/%s/actions/artifacts/%d/zip" % (repo, artifact))))
    except RedsError as err:
        entry = {"unknown": clean(err, 80)}
        cache.put(run_id, entry)
        return entry
    entry = {"complete": complete, "failed": names}
    cache.put(run_id, entry)
    return entry


def run(reader, repo, number, events, cache_dir, now, out):
    """Print the classification of `repo`#`number`; the exit status: 0 classified (or said it could not for want of an artifact), 1 a read failed."""
    try:
        pull = reader.json("repos/%s/pulls/%d" % (repo, number))
        head = (pull.get("head") or {}).get("sha")
        default_branch = ((pull.get("base") or {}).get("repo") or {}).get("default_branch")
        if not isinstance(head, str) or not isinstance(default_branch, str) or not re.fullmatch(r"[0-9a-f]{7,40}", head):
            raise RedsError("the pull request has no readable head or default branch")
        runs = reader.json("repos/%s/actions/runs?head_sha=%s&event=pull_request&status=completed&per_page=5" % (repo, head))
        pr_runs = [r for r in (runs.get("workflow_runs", []) if isinstance(runs, dict) else []) if isinstance(r, dict) and isinstance(r.get("id"), int)]
        pr_runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        cache = Cache(cache_dir, repo, now)
        listing = [None]
        if not pr_runs:
            out.write("reds %s#%d: no completed pull_request run for head %s: nothing to classify\n" % (repo, number, head[:7]))
            return EXIT_UNKNOWN
        pr_run = pr_runs[0]
        pr_entry = load_run(reader, repo, pr_run["id"], None, cache, listing)
        refs = reference_runs(reader, repo, default_branch, events, MAIN_RUNS)
        loaded = [(r, load_run(reader, repo, r[0], None, cache, listing)) for r in refs]
    except RedsError as err:
        out.write("reds %s#%d: cannot read (%s): nothing was classified\n" % (repo, number, clean(err, 160)))
        return EXIT_ERROR
    except Exception as err:   # a reader that fails (a network error, a rate-limit pause): said, nothing classified
        out.write("reds %s#%d: cannot read (%s): nothing was classified\n" % (repo, number, clean(err, 160)))
        return EXIT_ERROR
    if "unknown" in pr_entry:
        out.write("reds %s#%d: the pull request's run %s has no usable failing-tests artifact (%s): unknown, nothing was classified\n" % (repo, number, pr_run["id"], pr_entry["unknown"]))
        return EXIT_UNKNOWN
    pr_names = list(dict.fromkeys(pr_entry["failed"]))
    stamp = now()
    usable, unknown = [], []
    for position, (ref, entry) in enumerate(loaded):
        if "unknown" in entry:
            unknown.append((ref, entry["unknown"]))
        elif not entry["complete"]:
            unknown.append((ref, "incomplete: not all tests ran"))
        else:
            usable.append({"names": entry["failed"], "latest": position == 0, "ref": ref})
    out.write("reds %s#%d: %d failing in the pull request's run (%s%s)\n" % (repo, number, len(pr_names), pr_run.get("event", "pull_request"),
                                                                              "" if pr_entry["complete"] else "; its list is INCOMPLETE: not all of its tests ran"))
    if loaded:
        ref = loaded[0][0]
        age = _when(ref[3], stamp)
        line = "main's latest run: %s %s %s (reference events: %s)" % (ref[1], ref[2][:7], _age(age) + " ago" if age is not None else "time unknown", ", ".join(events))
        if not (usable and usable[0]["latest"]):
            why = unknown[0][1] if unknown and unknown[0][0] == ref else "unknown"
            line += ": UNKNOWN (%s), so \"failing on main\" cannot be said" % why
        out.write(line + "\n")
    else:
        out.write("main's latest run: none found among the runs of the reference events (%s)\n" % ", ".join(events))
    if not usable:
        # No complete reference run: nothing to compare with. A red is NEVER called new on no evidence: say why and list the names as unknown.
        reasons = sorted({_why(r[1]) for r in unknown}) or ["none found"]
        out.write("no usable reference run (%s): cannot classify\n" % "; ".join(reasons))
        out.write("  unknown (%d):\n" % len(pr_names))
        for name in pr_names[:200]:
            out.write("    %s\n" % clean(name))
        if len(pr_names) > 200:
            out.write("    ... %d more\n" % (len(pr_names) - 200))
        out.write("main runs: %d listed, 0 usable, %d unknown%s\n" % (len(loaded), len(unknown), "".join("; run %d: %s" % (r[0][0], clean(r[1], 60)) for r in unknown[:5])))
        return EXIT_UNKNOWN
    if not usable[0]["latest"]:
        out.write("main's latest run is unknown: classified by the %d older complete run(s) only; \"failing on main\" is not said\n" % len(usable))
    labels = classify(pr_names, usable)
    for title, key in (("also failing on main", "main"), ("flaky on main", "flaky"), ("new", "new")):
        rows = [n for n in pr_names if labels[n][0] == key]
        out.write("  %s (%d):\n" % (title, len(rows)))
        for name in rows[:200]:
            label, count, total, by_name = labels[name]
            note = " (%d of %d main runs)" % (count, total) if key != "new" or count else ""
            out.write("    %s%s%s\n" % (clean(name), note, "; matched by name only" if by_name else ""))
        if len(rows) > 200:
            out.write("    ... %d more\n" % (len(rows) - 200))
    out.write("main runs: %d listed, %d usable, %d unknown%s\n" % (len(loaded), len(usable), len(unknown), "".join("; run %d: %s" % (r[0][0], clean(r[1], 60)) for r in unknown[:5])))
    return EXIT_OK

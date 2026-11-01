"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): helper_fakes2."""
import collections
import json
import re
import subprocess
import sys
import urllib.parse
from unittest import mock
from support.fake_hubs import FakeTriageHub, WaitingInputs
from support.loader import SWITCHBOARD, SWITCHBOARD_SOURCE_FILES, sb
from support.rigs import _artifact_zip


class OwnedInputs(WaitingInputs):
    """#874: the daemon's lists for a scene of an ask (#2), its work item (#3), a work item below it (#4) and an unrelated issue (#5). Ages are
    counted back from the moment of each read (the daemon's pass re-reads the lists every few minutes), so time can pass and the lists stay fresh. The
    pull request list is `ci_record_pass`'s over a fake hub that honours the cut and the failure; the issue list is the triage's."""
    OLD, RECENT = 10 * 3600, 600
    NO_HEAD = object()

    def wire_scene(self):
        self.scene = {"issues": {3: {"parent": 2}, 4: {"parent": 3}}, "prs": []}
        self.wire_inputs()

    def pr(self, number, body, age=None, base="main", created=None, head=None, **extra):
        return dict({"number": number, "body": body, "age": self.OLD if age is None else age, "created": 86400 if created is None else created, "base": base,
                     "head": "a" * 40 if head is None else head}, **extra)

    def plan(self, n):
        return "PLAN: https://github.com/%s/issues/%d\n" % (self.REPO, n)

    def set_scene(self, prs=None, issues=None, **issue_extra):
        if prs is not None:
            self.scene["prs"] = prs
        for n, fields in (issues or {}).items():
            self.scene["issues"].setdefault(n, {}).update(fields)
        self.apply()

    def apply(self):
        engine, scene = self.world.engine, self.scene
        now = self.world.clock.now()
        def row(n):
            f = scene["issues"].get(n, {})
            record = {"number": n, "updated_at": sb.iso(now - f.get("age", self.OLD)), "created_at": sb.iso(now - self.OLD),
                      "labels": [{"name": l} for l in f.get("labels", ())]}
            if "raw_time" in f:
                record["updated_at"] = f["raw_time"]
            if f.get("parent") is not None:
                record["parent_issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (f.get("parent_repo", self.REPO), f["parent"])
            return record
        engine.triage = sb.TriageState()
        engine.triage.units[(self.REPO, "issues")] = {"at": now, "data": [row(n) for n in range(1, 100)], "truncated": False, "error": None}
        sb.read_mark(engine, "issues:" + self.REPO, True, "", now)
        sb.read_mark(engine, "decisions", True, "", now)
        self.hub.records = [{"number": p["number"], "body": p["body"], "updated_at": p["age"][1] if isinstance(p["age"], tuple) else sb.iso(now - p["age"]), "created_at": sb.iso(now - p["created"]),
                             "head": {"sha": p["head"]}, "base": {"ref": p["base"]}, "labels": [{"name": l} for l in p.get("labels", ())],
                             **{k: v for k, v in p.items() if k in ("requested_reviewers", "requested_teams", "draft")}} for p in scene["prs"]]
        for record, p in zip(self.hub.records, scene["prs"]):
            if p["head"] is self.NO_HEAD:
                del record["head"]   # a record with no head at all

        engine.__dict__.pop("ci_record_state", None)
        engine.__dict__.get("read_whole", {}).pop("pulls:" + self.REPO, None)
        sb.ci_record_pass(engine, now)

    def owned(self):
        engine = self.world.engine
        records = engine.triage.units[(self.REPO, "issues")]["data"]
        pulls = engine.ci_record_state.lists[sb.plan_ledger.normal_repo(self.REPO)][1]
        return sb.worker_owned(records, pulls, 2, self.REPO)

    def owned_numbers(self):
        got = self.owned()
        return sorted(r["number"] for r in got.issues), sorted(r["number"] for r in got.pulls)


class RecFacts(dict):
    """(#986) A facts mapping that RECORDS every key touched, by any spelling; an access it cannot attribute to a key records '*'."""

    seen = None   # set by the test: the one shared set of keys
    quiet = False   # true while the proxy is built from a picture: that copy is not a read

    def _note(self, key):
        if not RecFacts.quiet:
            RecFacts.seen.add(key)

    def __getitem__(self, key):
        self._note(key)
        return dict.__getitem__(self, key)

    def __setitem__(self, key, value):
        self._note(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._note(key)
        dict.__delitem__(self, key)

    def __contains__(self, key):
        self._note(key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        self._note(key)
        return dict.get(self, key, default)

    def pop(self, key, *default):
        self._note(key)
        return dict.pop(self, key, *default)

    def setdefault(self, key, default=None):
        self._note(key)
        return dict.setdefault(self, key, default)

    def update(self, *a, **k):
        RecFacts.seen.update(dict(*a, **k))
        RecFacts.seen.add("*")
        dict.update(self, *a, **k)

    def _star(self):
        if not RecFacts.quiet:
            RecFacts.seen.add("*")

    def items(self):
        self._star()
        return dict.items(self)

    def keys(self):
        self._star()
        return dict.keys(self)

    def values(self):
        self._star()
        return dict.values(self)

    def __iter__(self):
        self._star()
        return dict.__iter__(self)

    def copy(self):
        self._star()
        return RecFacts(dict.copy(self))

    def __or__(self, other):
        self._star()
        return dict.__or__(self, other)

    def __reversed__(self):
        self._star()
        return dict.__reversed__(self)

    def __eq__(self, other):
        self._star()
        return dict.__eq__(self, other)

    __hash__ = None

    def __repr__(self):
        self._star()
        return dict.__repr__(self)


class PolicyCountingJev:
    """Wraps a decision model and counts how often it was asked."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def decide(self, state, questions):
        self.calls += 1
        return self.inner.decide(state, questions)


class FakeOwnerlessHub(FakeTriageHub):
    """The triage hub for #605: the open list is paged by `cap` pages of 100 (cut when more remain) and the repository answers its own count of open items."""

    def __init__(self):
        super().__init__()
        self.open_total = {}

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        if "state=open" in path and "/issues?" in path and cap is not None:
            self.calls.append(path)
            repo = re.search(r"repos/([^/]+/[^/]+)/", path).group(1)
            if repo in self.unreadable:
                raise sb.GitHubUnavailable("HTTP 404: Not Found")
            everything = list(self.open.get(repo, {}).values())
            if "direction=asc" in path:   # GitHub sorts by the updated time; the fake does too
                everything.sort(key=lambda record: record["updated_at"])
            elif "sort=" not in path:
                everything.sort(key=lambda record: record["updated_at"], reverse=True)   # the default listing is newest first
            return everything[:cap * 100], len(everything) > cap * 100
        return super().read_pages(path, key, timeout, older, cap)

    def open_issues_count(self, repo):
        self.calls.append("repos/%s" % repo)
        return self.open_total.get(repo, len(self.open.get(repo, {})))


class LightProbe:
    """Makes `Switchboard.light` observable: every read of it records the source line that asked, and a chosen set of lines is told `False` (the guard removed there)
    while every other line is told `True`. It changes no code: the same engine, the same fake source, one guard at a time."""

    FILE = str(SWITCHBOARD)
    # the line a file starts at in switchboard_source() (the files are joined with one newline), so a read in a part is told apart by its own file
    OFFSETS = {}
    _at = 0
    for _path in SWITCHBOARD_SOURCE_FILES:
        OFFSETS[str(_path)] = _at
        _at += _path.read_text().count("\n") + 1

    def __init__(self, flip=()):
        self.flip, self.hits = set(flip), collections.Counter()

    def __enter__(self):
        probe = self

        def get(engine):
            frame = sys._getframe(1)
            offset = probe.OFFSETS.get(frame.f_code.co_filename)
            if offset is not None:
                line = frame.f_lineno + offset
                probe.hits[line] += 1
                if any(lo <= line <= hi for lo, hi in probe.flip):
                    return False
            return engine.__dict__["light"]

        def put(engine, value):
            engine.__dict__["light"] = value
        self.patch = mock.patch.object(sb.Switchboard, "light", property(get, put), create=True)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def touched(self, lo, hi):
        return sum(n for line, n in self.hits.items() if lo <= line <= hi)


class OrdersReachTheJudgeWhole:
    """Every path an orders text takes to the judge's input: a charter's (resolve_policy_context), the Chief of Staff's
    file (context_of) and an owner's agent's inbound answer (judge_inbound_answer). Each is driven through its real code
    with 30 long orders; the last order must be in the judge's input, whole."""

    @staticmethod
    def long_orders(prefix, count=30, width=250):
        return ["%s %02d %s end-%s-%02d" % (prefix, n, "x" * width, prefix, n) for n in range(count)]

    class Spy:
        def __init__(self):
            self.seen = []

        def decide(self, state, questions):
            self.seen.append(state)
            raise sb.JevError("unreachable", "spy")


class Unavailable(Exception):
    permanent = False
    retry_after = 3


class AlreadyDecidedRefusal(Exception):
    """What the real adapter raises for a 409 "already decided": a permanent refusal that says it is settled outside."""

    permanent = True
    settled_outside = True


class StubHub:
    def __init__(self, entries):
        self.entries = entries

    def pr_event_snapshot(self):
        return {ref: dict(entry) for ref, entry in self.entries.items()}


class EjectionHub:
    def __init__(self, owner="orch"):
        self.owner, self.routed, self.notified = owner, [], {}

    def notice_names(self, repo, number):
        return self.notified.get((repo, number), [])

    def route_of(self, item, agents, now):
        self.routed.append(item.numbers)
        return (self.owner, "closing") if self.owner else (None, None)


class FakeQueueGh:
    """`gh` for the merge queue ejection pass (#584): the merge queue runs of a repository, a pull request's state and queue entry (GraphQL), a run's artifacts
    and jobs. Counts what it is asked, per repository."""

    def __init__(self):
        self.runs, self.pulls, self.zips, self.jobs, self.calls, self.artifact_name = {}, {}, {}, {}, [], "failing-tests"
        self.fail, self.graphql_fault = set(), None

    def add(self, repo, run_id, pr, now, conclusion="failure", status="completed", name="ci", age=60, created_age=None, names=None, complete=True, raw=None, jobs=()):
        stamp = lambda ago: sb.iso(now - ago)
        self.runs.setdefault(repo, []).append({"id": run_id, "name": name, "event": "merge_group", "status": status, "conclusion": conclusion,
                                               "head_branch": "gh-readonly-queue/main/pr-%d-%s" % (pr, "%07x" % run_id), "created_at": stamp(created_age or age + 30), "updated_at": stamp(age)})
        self.pulls.setdefault((repo, pr), ("OPEN", False))
        if raw is not None:
            self.zips[run_id] = raw
        elif names is not None:
            self.zips[run_id] = _artifact_zip(names, complete, run_id)
        self.jobs[run_id] = list(jobs)
        return run_id

    def count(self, repo, needle):
        return len([c for c in self.calls if repo in c and needle in c])

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        done = lambda text, code=0, err="": subprocess.CompletedProcess(argv, code, text, err)
        if argv[1:3] == ["api", "graphql"]:
            fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
            repo, pr = "%s/%s" % (fields["owner"], fields["name"]), int(fields["number"])
            self.calls.append("graphql %s#%d" % (repo, pr))
            if self.graphql_fault == "exit":
                return done("", 1, "gh: HTTP 502")
            if self.graphql_fault == "weird":
                return done('{"data": {"repository": {"pullRequest": {"state": "WEIRD", "mergeQueueEntry": null}}}}')
            if self.graphql_fault == "malformed":
                return done('{"data": {"repository": null}}')
            state, queued = self.pulls[(repo, pr)]
            return done(json.dumps({"data": {"repository": {"pullRequest": {"state": state, "mergeQueueEntry": {"id": "Q", "state": queued if isinstance(queued, str) else "QUEUED"} if queued else None}}}}))
        path = argv[-1]
        self.calls.append(path)
        if any(bad in path for bad in self.fail):
            return done("", 1, "gh: HTTP 500")
        found = re.fullmatch(r"repos/([^/]+/[^/]+)/actions/runs\?event=merge_group&per_page=\d+", path)
        if found:
            return done(json.dumps({"workflow_runs": self.runs.get(found.group(1), [])}))
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts\?name=([^&]+)&per_page=\d+", path)
        if found:
            run = int(found.group(1))
            ok = run in self.zips and urllib.parse.unquote(found.group(2)) == self.artifact_name
            return done(json.dumps({"artifacts": [{"id": run + 9000, "name": self.artifact_name, "expired": False, "size_in_bytes": len(self.zips[run])}] if ok else []}))
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/artifacts/(\d+)/zip", path)
        if found:
            return subprocess.CompletedProcess(argv, 0, self.zips[int(found.group(1)) - 9000], b"")
        found = re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/jobs\?filter=latest&per_page=\d+", path)
        if found:
            return done(json.dumps({"jobs": [{"name": n, "conclusion": c} for n, c in self.jobs[int(found.group(1))]]}))
        raise AssertionError("the ejection pass reaches for nothing else: %r" % (argv,))


class FakePullFiles:
    """GitHub's pull request answers for ONE pull request, no more generous than the real API: the files are served 100 a page whatever per_page asks, only the first 3000 are ever listed (a page past
    the end is empty), `changed_files` is the true count, and `gh pr view` lists 100 files at most with no source path for a rename. `faults` is {"pull" or page number: [fault, ...]}: one fault is used up
    by each call of that read, in order (a timeout, an HTTP 502, an answer that is not JSON, one over the size cap, an answer that is not a list). `heads` is what the pull request's head is at each read of it."""

    HEAD = "c" * 40
    LONG = "E" * 450 + " THE-END"   # an error text far over every old clip (80 to 600 characters), ending in a marker

    def __init__(self, files, number=9, heads=None, count=None):
        self.files, self.number = files, number
        self.count = len(files) if count is None else count
        self.faults, self.calls = {}, []
        self.heads = list(heads or [])

    @staticmethod
    def make(size, status="modified"):
        return [{"filename": "docs/f%04d.md" % i, "status": status, "sha": "e" * 40, "patch": "@@ -1 +1 @@\n-a\n+b", "changes": 2} for i in range(size)]

    def view(self):
        """what `gh pr view --json files,...` says: at most 100 files, a rename without its source"""
        shown = [{"path": f["filename"] if isinstance(f, dict) else "x", "additions": 1, "deletions": 1, "changeType": f["status"].upper() if isinstance(f, dict) else "MODIFIED"} for f in self.files[:100]]
        return dict({"headRefOid": self.HEAD, "baseRefOid": "b" * 40, "changedFiles": self.count, "files": shown}, **({"statusCheckRollup": self.rollup} if getattr(self, "rollup", None) else {}))   # `rollup` is set only by a clear that must show CI to its brief (#1215)

    def fault(self, key):
        queue = self.faults.get(key) or []
        return queue.pop(0) if queue else None

    def __call__(self, argv, **kwargs):
        path = argv[2]
        self.calls.append(path)
        route, _, query = path.partition("?")
        query = dict(q.split("=", 1) for q in query.split("&") if q)
        tail = route.split("/", 3)[3]
        if tail == "pulls/%d" % self.number:
            key, body = "pull", None
            head = (self.heads.pop(0) if self.heads else self.HEAD)
            body = {"head": {"sha": head}, "base": {"sha": "b" * 40, "ref": "main"}, "changed_files": self.count}
        elif tail == "pulls/%d/files" % self.number:
            page, size = int(query.get("page", "1")), min(int(query.get("per_page", "30")), 100)
            key, body = page, (self.files[:3000])[(page - 1) * size:page * size]
        elif tail.startswith("compare/"):
            key, body = "compare", {"merge_base_commit": {"sha": "b" * 40}}
        else:
            raise AssertionError("the fake GitHub has no answer for %s" % path)
        fault = self.fault(key)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        if fault == "longerror":
            return subprocess.CompletedProcess(argv, 1, "", "gh: " + FakePullFiles.LONG)
        if fault == "http502":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)")
        if fault in ("http403", "http404", "http422"):
            return subprocess.CompletedProcess(argv, 1, "", "gh: Error (HTTP %s)" % fault[4:])
        if fault == "bigbody":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Bad Gateway (HTTP 502)\n" + "B" * (1 << 20))
        if fault == "bodystatus":
            return subprocess.CompletedProcess(argv, 1, "", "gh: Validation Failed (HTTP 422)\n{\"message\": \"see HTTP 404 and HTTP 502\"}")
        if fault == "notjson":
            return subprocess.CompletedProcess(argv, 0, "<html>bad gateway</html>", "")
        if fault == "toolarge":
            return subprocess.CompletedProcess(argv, 0, "[" + "0," * (sb.CARRY_ANSWER_MAX // 2 + 1) + "0]", "")
        if fault == "notlist":
            return subprocess.CompletedProcess(argv, 0, json.dumps(["x"] if key in ("pull", "compare") else {"message": "Server Error"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    def pages_asked(self, page):
        return len([c for c in self.calls if c.endswith("&page=%d" % page)])


class StubIssueHub:
    """The hub as the closed-issue pass sees it: the events it holds, and a fresh read per issue (`reads`: number -> dict, or an exception)."""

    def __init__(self, entries, reads):
        self.entries, self.reads, self.read_log = entries, reads, []

    def closed_issue_snapshot(self):
        return {ref: dict(entry) for ref, entry in self.entries.items()}

    def forget_awaiting(self, repo, number):
        self.forgotten = getattr(self, "forgotten", []) + [(repo, number)]

    def _read_one(self, repo, path, now, fresh=False):
        number = int(path.rsplit("/", 1)[1])
        self.read_log.append((repo, number, fresh))
        found = self.reads[number]
        if isinstance(found, Exception):
            raise found
        return dict(found)

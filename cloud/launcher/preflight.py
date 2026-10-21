#!/usr/bin/env python3
"""preflight: check, read-only, that a repository's default branch refuses what a cloud worker must not do.

usage: preflight.py OWNER/REPO [--branch NAME] [--accept-classic]

Reads with GET only (https://api.github.com; CW_GITHUB_API_BASE may name a loopback http URL, for tests):
  /repos/{o}/{r}                       the default branch
  /repos/{o}/{r}/rules/branches/{b}    the rulesets that apply to the branch
  /repos/{o}/{r}/branches/{b}          the `protected` flag
The token comes from app_token.py, or from the command CW_PREFLIGHT_TOKEN_CMD when set (tests).

An ORGANIZATION ruleset the token cannot read (HTTP 403 on its GET: an App token never can) is not counted, one line
each. If everything that is then missing would be supplied by the rules such rulesets carry, the repository fails
(exit 1) unless the owner approval entry for it exists in repo-approvals.json beside this program (see approval()): then it
passes with a loud "NOT VERIFIED" line on every run. Any other failed read (404, 500, a dropped connection, odd JSON,
too large; a 403 on a repository's own ruleset) is exit 2.

PASS (exit 0, "preflight ok: <rule names>") only when the branch has, from rulesets that are `active` and cover it,
a pull_request rule with required_approving_review_count of at least 1 (only the count; dismiss-stale and code-owner
flags are not required) OR a merge_queue rule (a direct push is refused), AND non_fast_forward (no force-push) AND
deletion. A ruleset whose enforcement is `evaluate` or `disabled` is "not enforced" and a ruleset whose conditions do
not cover the branch is refused, each by name (see judge() for how the branch rules answer is used and checked).
A redirect is never followed (exit 2, the status is named).
FAIL (exit 1), one line per missing rule, naming it. A branch protected only by classic branch protection also
fails: a read-only token cannot see its review requirement. --accept-classic is the explicit, owner-approved override,
and it overrides ONLY that one failure (the pull_request/merge_queue rule missing, on a branch GitHub reports as
protected). It never turns an unreadable answer (exit 2), an unprotected branch, or a missing non_fast_forward or
deletion rule into a pass.
Exit 2: the answer could not be read (HTTP error, bad JSON). Silence never passes.

What this CANNOT see: bypass actors (who may skip a rule), and whether a rule is in force for this token's own
identity. The live refusal check (a real push, force-push and delete attempted on a disposable repository, S4) is the
real proof; this is only the cheap guard before a worker is launched.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_token  # noqa: E402

NEED = (("pull_request|merge_queue", "a pull_request or merge_queue rule (direct pushes must be refused)"),
        ("non_fast_forward", "a non_fast_forward rule (force-push must be refused)"),
        ("deletion", "a deletion rule (branch deletion must be refused)"))


class Unreadable(Exception):
    def __init__(self, msg, status=None, limited=False):
        super().__init__(msg)
        self.status = status  # the HTTP status of a refused read, else None
        self.limited = limited  # the refusal shows signs of a rate limit (never printed, only used to refuse)


def say(msg):
    print("preflight: " + msg)


def get(base, path, token):
    req = urllib.request.Request(base + path, method="GET",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cw-preflight"})
    try:
        with app_token.open_url(req) as r:
            raw = r.read(app_token.MAX_BYTES + 1)
        if len(raw) > app_token.MAX_BYTES:
            raise Unreadable("the answer to GET %s is larger than %d bytes" % (path, app_token.MAX_BYTES))
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise Unreadable("GET %s: %s" % (path, app_token.http_error_text(e.code)), e.code, _rate_limited(e))
    except (OSError, ValueError):
        raise Unreadable("GET %s failed or its answer was not JSON" % path)


def _rate_limited(e):
    """True when a refused read shows a rate limit: a Retry-After header, X-RateLimit-Remaining 0, or a rate-limit
    message in the (bounded) body. Read here and exposed to the caller as a flag; nothing of it is printed."""
    try:
        h = e.headers
        if h is not None and (h.get("Retry-After") is not None or str(h.get("X-RateLimit-Remaining", "")).strip() == "0"):
            return True
        body = e.read(4096).decode("utf-8", "replace").lower()
        return "rate limit" in body
    except Exception:
        return True  # cannot tell: refuse


def get_token(target):
    cmd = os.environ.get("CW_PREFLIGHT_TOKEN_CMD")
    if not cmd:
        try:
            token = app_token.mint(target)
            say("token permissions: " + app_token.render_permissions())  # the mint refused anything else
            return token
        except SystemExit:  # app_token already said why, on stderr; a mint that fails is an unreadable answer
            raise Unreadable("no token could be minted")
    try:
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise Unreadable("the token command could not run")
    if p.returncode != 0 or not p.stdout.strip():
        raise Unreadable("the token command failed or printed nothing")
    return p.stdout.strip()


def _glob(pat):
    """GitHub's ref pattern as an anchored regex: ** crosses '/', * does not, ? is one non-'/' character, all else
    literal. None for a pattern we do not understand (character classes, odd tildes, not text)."""
    if not isinstance(pat, str) or not pat or "[" in pat or "]" in pat or pat.startswith("~"):
        return None
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pat[i]
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def covers(conditions, branch, default_branch):
    """(covered, odd): covered is True when a ruleset's ref_name conditions cover refs/heads/<branch>, None when they
    cannot be read; odd names a pattern we did not understand. FAIL CLOSED: an include pattern we do not understand
    covers nothing, and an exclude pattern we do not understand excludes the branch."""
    ref = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref, dict) or not isinstance(ref.get("include"), list) or not isinstance(ref.get("exclude", []), list):
        return None, None
    full = "refs/heads/" + branch
    odd = []

    def hit(pat, excluding):
        if pat == "~ALL":
            return True
        if pat == "~DEFAULT_BRANCH":
            return branch == default_branch
        rx = _glob(pat)
        if rx is None:
            odd.append(str(pat)[:40])
            return excluding  # an unknown include matches nothing; an unknown exclude matches everything
        return bool(rx.match(full))
    inc = any([hit(x, False) for x in ref["include"]])
    exc = any([hit(x, True) for x in ref.get("exclude", [])])
    return (inc and not exc), odd


def judge(rules, branch, default_branch, fetch, unreadable=None):
    """Keep only the rules of rulesets that are active and cover the branch. Returns (problems, kept rules), or
    (problems, None) when a ruleset cannot be read.

    The branch rules endpoint returns only rules that apply to the branch. We rely on that and do not trust it alone:
    every rule must name its ruleset (ruleset_id, ruleset_source_type, ruleset_source), and each ruleset is fetched
    once (GET only, bounded) to read its `enforcement` (must be `active`: `evaluate` and `disabled` are not enforced)
    and its `conditions` (must cover the branch). Anything odd fails closed.

    The one exception: an ORGANIZATION ruleset whose read is refused (a plain HTTP 403, without a sign of a rate limit; an App token can never read
    them) is not counted. Its id goes into `unreadable` (a dict id -> its rules) and the rules it carries are not kept."""
    problems, kept, verdict = [], [], {}
    unreadable = {} if unreadable is None else unreadable
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str):
            say("cannot read: a rule has no type")
            return problems, None
        rid, st, src = r.get("ruleset_id"), r.get("ruleset_source_type"), r.get("ruleset_source")
        if isinstance(rid, bool) or not isinstance(rid, int) or st not in ("Repository", "Organization") \
                or not isinstance(src, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?", src) \
                or any(x in (".", "..") for x in src.split("/")):
            say("cannot read: a %s rule does not name its ruleset" % r["type"][:40])
            return problems, None
        key = (st, src, rid)
        if key not in verdict:
            path = ("/repos/%s/rulesets/%d" if st == "Repository" else "/orgs/%s/rulesets/%d") % (src, rid)
            try:
                rs = fetch(path)
            except Unreadable as e:
                if st != "Organization" or e.status != 403 or e.limited:
                    say("cannot read: %s" % e)
                    return problems, None
                say("organization ruleset %d: not readable with this token, not counted" % rid)
                verdict[key] = None
                unreadable[rid] = []
        if key not in verdict:
            enf = rs.get("enforcement") if isinstance(rs, dict) else None
            cov, odd = covers(rs.get("conditions"), branch, default_branch) if isinstance(rs, dict) else (None, None)
            if enf not in ("active", "evaluate", "disabled") or cov is None or (isinstance(rs, dict) and rs.get("target") != "branch"):
                say("cannot read: ruleset %d has an unreadable enforcement, target or conditions" % rid)
                return problems, None
            if enf != "active":
                problems.append("ruleset %d is not enforced (enforcement is '%s')" % (rid, enf))
            elif not cov:
                problems.append("ruleset %d does not cover the branch%s" % (
                    rid, " (a pattern was not understood: %s)" % ", ".join(odd) if odd else ""))
            verdict[key] = enf == "active" and cov
        if verdict[key] is None:
            unreadable[rid].append(r)
        elif verdict[key]:
            kept.append(r)
    return problems, kept


def evaluate(valid):
    """(missing, has_pr, types, review problems) for a list of rules."""
    types = {r.get("type") for r in valid}
    counts = [(r.get("parameters") or {}).get("required_approving_review_count") for r in valid
              if r.get("type") == "pull_request"]
    has_pr = "pull_request" in types
    reviews_ok = "merge_queue" in types or any(isinstance(c, int) and not isinstance(c, bool) and c >= 1 for c in counts)
    problems, missing = [], []
    if has_pr and not reviews_ok:
        problems.append("the pull_request rule has no required approving reviews (required_approving_review_count must be "
                        "at least 1, or use a merge_queue rule)")
    if not has_pr and "merge_queue" not in types:
        missing.append(NEED[0][1])
    missing += [what for alts, what in NEED[1:] if not (types & set(alts.split("|")))]
    return missing, has_pr, types, problems


def _field(v):
    return isinstance(v, str) and v.strip() != "" and len(v) <= 300 and not re.search("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]", v)


APPROVALS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "repo-approvals.json")


def _no_duplicates(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def approval(target):
    """The owner approval entry for target from repo-approvals.json beside this program (no setting, no flag: an approval
    arrives only through a reviewed pull request that changes that file). Keys are the sha256 hex of the lowercased
    OWNER/NAME. Returns (who, date, pr, reason), None when there is no entry for this repository, or raises Unreadable
    (fail closed) when the file or the matching entry cannot be read."""
    try:
        with open(APPROVALS, "rb") as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise Unreadable("the approvals file is larger than 65536 bytes")
        data = json.loads(raw, object_pairs_hook=_no_duplicates)
    except Unreadable:
        raise
    except Exception:  # OSError, ValueError (a duplicate key too), RecursionError
        raise Unreadable("the approvals file could not be read or is not JSON")
    if not isinstance(data, dict):
        raise Unreadable("the approvals file is not a JSON object")
    key = hashlib.sha256(target.lower().encode()).hexdigest()
    if key not in data:
        return None
    e = data[key]
    if not isinstance(e, dict) or not all(_field(e.get(k)) for k in ("approved_by", "approved_at", "reason", "pr")):
        raise Unreadable("the approval entry for this repository is malformed (approved_by, approved_at, reason and pr "
                         "must be non-empty one-line strings)")
    return e["approved_by"].strip(), e["approved_at"].strip(), e["pr"].strip().lstrip("#"), e["reason"].strip()


def main(argv):
    accept = "--accept-classic" in argv
    argv = [a for a in argv if a != "--accept-classic"]
    branch = None
    if "--branch" in argv:
        i = argv.index("--branch")
        if i + 1 >= len(argv):
            say("usage: preflight.py OWNER/REPO [--branch NAME] [--accept-classic]")
            return 2
        branch = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) != 1 or not app_token.valid_target(argv[0]) \
            or (branch is not None and (not re.fullmatch(r"[A-Za-z0-9_./-]+", branch)
                                  or ".." in branch.split("/"))):
        say("usage: preflight.py OWNER/REPO [--branch NAME] [--accept-classic]")
        return 2
    target = argv[0]
    try:
        try:
            base = app_token.api_base()
        except SystemExit:  # already said why on stderr
            raise Unreadable("the API base is refused")
        token = get_token(target)
        repo = get(base, "/repos/" + target, token)
        branch = branch or (repo.get("default_branch") if isinstance(repo, dict) else None)
        if not isinstance(branch, str) or not branch:
            raise Unreadable("the repository answer has no default branch")
        q = urllib.parse.quote(branch, safe="")
        rules = get(base, "/repos/%s/rules/branches/%s?per_page=100" % (target, q), token)
        info = get(base, "/repos/%s/branches/%s" % (target, q), token)
    except Unreadable as e:
        say("cannot read: %s" % e)
        return 2
    if not isinstance(rules, list) or not isinstance(info, dict):
        say("cannot read: unexpected answer shape")
        return 2
    if len(rules) >= 100:
        say("cannot read: 100 or more rules apply; the answer may be paginated and is not read in full")
        return 2
    unreadable = {}
    problems, valid = judge(rules, branch, repo.get("default_branch"), lambda path: get(base, path, token), unreadable)
    if valid is None:
        return 2
    missing, has_pr, types, review_problem = evaluate(valid)
    problems += review_problem
    if not missing and not problems:
        say("preflight ok: " + ", ".join(sorted(t for t in types if isinstance(t, str))))
        return 0
    if unreadable and not problems and missing:
        # Could the unreadable organization rulesets have supplied everything that is missing? Then (and only then)
        # the owner approval entry is read. A rule the preflight can see is missing stays missing.
        extra = [r for rs in unreadable.values() for r in rs]
        if not evaluate(valid + extra)[0] and not evaluate(valid + extra)[3]:
            ids = ", ".join(str(i) for i in sorted(unreadable))
            try:
                ok = approval(target)
            except Unreadable as e:
                say("cannot read: %s" % e)
                return 2
            if ok:
                say("preflight ok: protection NOT VERIFIED (organization ruleset %s unreadable); owner-approved by %s on %s (pr #%s): %s" % ((ids,) + ok))
                return 0
            say("FAIL on %s: protection comes from an organization ruleset this token cannot read; an owner approval entry "
                "is needed: see cloud/launcher/README.md (an entry in repo-approvals.json)" % branch)
            return 1
    for m in missing:
        say("FAIL on %s: missing %s" % (branch, m))
    for m in problems:
        say("FAIL on %s: %s" % (branch, m))
    only_review = missing == [NEED[0][1]] and not problems and not has_pr
    if info.get("protected") is True:
        say("classic protection: review requirement not readable with this token; use a ruleset or confirm by hand "
            "and pass --accept-classic")
        if accept and only_review:
            say("WARNING: --accept-classic: NOT verified, owner-approved override of classic protection on %s" % branch)
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

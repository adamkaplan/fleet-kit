#!/usr/bin/env python3
"""preflight: check, read-only, that a repository's default branch refuses what a cloud worker must not do.

usage: preflight.py OWNER/REPO [--branch NAME] [--accept-classic]

Reads with GET only (https://api.github.com; CW_GITHUB_API_BASE may name a loopback http URL, for tests):
  /repos/{o}/{r}                       the default branch
  /repos/{o}/{r}/rules/branches/{b}    the rulesets that apply to the branch
  /repos/{o}/{r}/branches/{b}          the `protected` flag
The token comes from app_token.py, or from the command CW_PREFLIGHT_TOKEN_CMD when set (tests).

PASS (exit 0, "preflight ok: <rule names>") only when the branch has a rule of type pull_request or merge_queue (a
direct push is refused by the rule) AND non_fast_forward (no force-push) AND deletion.
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
    pass


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
        raise Unreadable("GET %s: %s" % (path, app_token.http_error_text(e.code)))
    except (OSError, ValueError):
        raise Unreadable("GET %s failed or its answer was not JSON" % path)


def get_token(target):
    cmd = os.environ.get("CW_PREFLIGHT_TOKEN_CMD")
    if not cmd:
        try:
            return app_token.mint(target)
        except SystemExit:  # app_token already said why, on stderr; a mint that fails is an unreadable answer
            raise Unreadable("no token could be minted")
    try:
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise Unreadable("the token command could not run")
    if p.returncode != 0 or not p.stdout.strip():
        raise Unreadable("the token command failed or printed nothing")
    return p.stdout.strip()


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
    types = {r.get("type") for r in rules if isinstance(r, dict)}
    missing = [what for alts, what in NEED if not (types & set(alts.split("|")))]
    if not missing:
        say("preflight ok: " + ", ".join(sorted(t for t in types if isinstance(t, str))))
        return 0
    for m in missing:
        say("FAIL on %s: missing %s" % (branch, m))
    only_review = len(missing) == 1 and missing[0] == NEED[0][1]
    if info.get("protected") is True:
        say("classic protection: review requirement not readable with this token; use a ruleset or confirm by hand "
            "and pass --accept-classic")
        if accept and only_review:
            say("WARNING: --accept-classic: NOT verified, owner-approved override of classic protection on %s" % branch)
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

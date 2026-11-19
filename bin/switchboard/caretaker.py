"""Tuesday and Thursday Caretaker review of aging open issues across watched repos (#2573).

Reviews open issues with no movement for `days` (default 14) across every watched repository
(`github_repos` in `~/.config/fleet-switchboard/config.json`). On each run it either closes an
issue that no longer applies (with a one-line reason) or raises it for awareness to the owning
orchestrator (or `cos` if none); it never assigns or forces work, and emits one summary line
to `cos` per run.
"""
import argparse
import datetime as dt
import json
import os
import pwd
import re
import subprocess
import sys

PROTECTED_SUFFIXES = (":charter", ":orchestrator", ":parked", ":awaiting-user", ":awaiting-cos")
OBSOLETE_RE = re.compile(r"\b(cw-worker|pod-launch|pod reaper|cloud_reaper|integration branch)\b", re.I)


if "github_write" not in globals():   # imported standalone: the audit is the switchboard's own when it is assembled, a no-op here
    class github_write:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return {}

        def __exit__(self, *exc):
            return False


DEFAULT_OTHER_LABELS = ("agent:firstmate",)


def classify_issue(issue, cutoff_iso, other_owners=(), other_labels=DEFAULT_OTHER_LABELS):
    """Return (action, detail): action is skip, skip_other, skip_parked, close or raise."""
    updated = str(issue.get("updatedAt") or issue.get("updated_at") or "").strip()
    if not updated or updated >= cutoff_iso:
        return ("skip", "fresh")
    labels = [
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
        for lbl in (issue.get("labels") or [])
    ]
    labels = [str(n) for n in labels if n]
    author = ((issue.get("author") or {}).get("login") or "").strip()
    assignees = [(a.get("login") or "").strip() for a in (issue.get("assignees") or []) if isinstance(a, dict)]
    owners_set = {str(x).strip().lower() for x in (other_owners or ()) if str(x).strip()}
    labels_set = {str(x).strip().lower() for x in (other_labels or ()) if str(x).strip()}
    if (author.lower() in owners_set and author) or any(a.lower() in owners_set for a in assignees if a) \
            or any(n.lower() in labels_set for n in labels):
        return ("skip_other", "other owner")
    if any(n == "parked" or n.endswith(":parked") for n in labels) or "Parked until:" in str(issue.get("body") or ""):
        return ("skip_parked", "parked")
    if any(n.endswith(PROTECTED_SUFFIXES) for n in labels):
        return ("skip", "protected")
    title = str(issue.get("title") or "")
    if OBSOLETE_RE.search(title):
        return ("close", "Moot after the Spire Floor and main-branch cutovers (#2548, #2564, #2574).")
    for name in labels:
        if isinstance(name, str) and name.startswith("lane:") and name.split(":", 1)[1]:
            return ("raise", name.split(":", 1)[1])
    return ("raise", "cos")


def run_caretaker(config=None, apply_changes=False, days=14, now_dt=None, runner=subprocess.run, gh_bin="gh", audit=None):
    """Scan watched repos for 14+ day stale open issues, close obsolete ones when apply_changes=True, and return a 1-line summary."""
    if config is None:
        home = pwd.getpwuid(os.getuid()).pw_dir
        cfg_path = os.path.join(home, ".config", "fleet-switchboard", "config.json")
        with open(cfg_path, encoding="utf-8") as handle:
            config = json.load(handle)
    repos = list(config.get("github_repos") or [])
    gh_users = dict(config.get("gh_users") or {})
    now_val = now_dt or dt.datetime.now(dt.timezone.utc)
    cutoff_iso = (now_val - dt.timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    other_owners = list(config.get("caretaker_other_owners") or [])
    other_labels = list(config.get("caretaker_other_labels") or DEFAULT_OTHER_LABELS)
    closed, raised, errors = [], [], []
    repo_stats, skipped_other, skipped_parked = {}, [], []
    for repo in repos:
        env = dict(os.environ)
        owner = repo.split("/", 1)[0]
        user = gh_users.get(repo) or gh_users.get(owner + "/*") or gh_users.get(owner)
        if user:
            tok = runner([gh_bin, "auth", "token", "--user", str(user)], capture_output=True, text=True, timeout=15)
            if tok.returncode == 0 and (tok.stdout or "").strip():
                env["GH_TOKEN"] = tok.stdout.strip()
        res = runner(
            [gh_bin, "issue", "list", "-R", repo, "--state", "open", "--limit", "1000", "--json", "number,title,updatedAt,labels,author,assignees,body"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if res.returncode != 0:
            errors.append("%s:list-failed" % repo)
            continue
        try:
            issues = json.loads(res.stdout or "[]")
        except ValueError:
            errors.append("%s:invalid-json" % repo)
            continue
        stale_count, oldest = 0, ""
        for issue in issues:
            upd = str(issue.get("updatedAt") or "").strip()
            if upd and (not oldest or upd < oldest):
                oldest = upd
            num = issue.get("number")
            ref = "%s#%s" % (repo, num)
            action, detail = classify_issue(issue, cutoff_iso, other_owners=other_owners, other_labels=other_labels)
            if action in ("close", "raise"):
                stale_count += 1
            if action == "skip_other":
                skipped_other.append(ref)
            elif action == "skip_parked":
                skipped_parked.append(ref)
            elif action == "close":
                if apply_changes:
                    if audit is None and "github_state_audit" in globals():
                        audit = globals()["github_state_audit"]()
                    with github_write(audit, "caretaker", "POST", "repos/%s/issues/%s" % (repo, num), repo, num):
                        cl = runner(
                            [gh_bin, "issue", "close", str(num), "-R", repo, "--reason", "not planned", "--comment", detail],
                            capture_output=True, text=True, env=env, timeout=30,
                        )
                    if cl.returncode != 0:
                        errors.append("%s:close-failed" % ref)
                        continue
                closed.append(ref)
            elif action == "raise":
                raised.append("%s->%s" % (ref, detail))
        repo_stats[repo] = {"open": len(issues), "stale": stale_count, "oldest": oldest}
    c_str = ", ".join(closed) if closed else "none"
    r_str = ", ".join(raised) if raised else "none"
    counts_str = ", ".join("%s: %d open (oldest %s)" % (r, st["open"], st["oldest"] or "none") for r, st in repo_stats.items())
    summary = (
        "Caretaker (%dd+ stale across %d repos [%s]): closed %d obsolete (%s), "
        "raised %d for awareness (%s), skipped %d (other owner) and %d (parked)."
        % (
            int(days), len(repos), counts_str,
            len(closed), c_str,
            len(raised), r_str,
            len(skipped_other), len(skipped_parked),
        )
    )
    return {
        "closed": closed, "raised": raised, "errors": errors, "summary": summary, "cutoff": cutoff_iso,
        "skipped_other": skipped_other, "skipped_parked": skipped_parked, "repo_stats": repo_stats,
    }


def caretaker_main(argv=None):
    p = argparse.ArgumentParser(description="Tuesday/Thursday Caretaker review of aging open issues (#2573)")
    p.add_argument("--apply", action="store_true", help="Close obsolete issues on GitHub")
    p.add_argument("--days", type=int, default=14, help="Staleness threshold in days (default: 14)")
    args = p.parse_args(argv)
    out = run_caretaker(apply_changes=args.apply, days=args.days)
    print(out["summary"])
    return 1 if out["errors"] else 0


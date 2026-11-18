#!/usr/bin/env python3
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


def classify_issue(issue, cutoff_iso):
    """Return (action, detail) for an issue dict: ('skip'|'close'|'raise', detail)."""
    updated = str(issue.get("updatedAt") or issue.get("updated_at") or "").strip()
    if not updated or updated >= cutoff_iso:
        return ("skip", "fresh")
    labels = [
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
        for lbl in (issue.get("labels") or [])
    ]
    if any(str(name).endswith(PROTECTED_SUFFIXES) for name in labels if name):
        return ("skip", "protected-label")
    title = str(issue.get("title") or "")
    if OBSOLETE_RE.search(title):
        return ("close", "Moot after the Spire Floor and main-branch cutovers (#2548, #2564, #2574).")
    for name in labels:
        if isinstance(name, str) and name.startswith("lane:") and name.split(":", 1)[1]:
            return ("raise", name.split(":", 1)[1])
    return ("raise", "cos")


def run_caretaker(config=None, apply_changes=False, days=14, now_dt=None, runner=subprocess.run, gh_bin="gh"):
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
    closed, raised, errors = [], [], []
    for repo in repos:
        env = dict(os.environ)
        user = gh_users.get(repo) or gh_users.get(repo.split("/", 1)[0])
        if user:
            tok = runner([gh_bin, "auth", "token", "--user", str(user)], capture_output=True, text=True, timeout=15)
            if tok.returncode == 0 and (tok.stdout or "").strip():
                env["GH_TOKEN"] = tok.stdout.strip()
        res = runner(
            [gh_bin, "issue", "list", "-R", repo, "--state", "open", "--limit", "200", "--json", "number,title,updatedAt,labels,body"],
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
        for issue in issues:
            num = issue.get("number")
            ref = "%s#%s" % (repo, num)
            action, detail = classify_issue(issue, cutoff_iso)
            if action == "close":
                if apply_changes:
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
    c_str = ", ".join(closed) if closed else "none"
    r_str = ", ".join(raised) if raised else "none"
    summary = "Caretaker (%dd+ stale across %d repos): closed %d obsolete (%s), raised %d for awareness (%s)." % (
        int(days), len(repos), len(closed), c_str, len(raised), r_str
    )
    return {"closed": closed, "raised": raised, "errors": errors, "summary": summary, "cutoff": cutoff_iso}


def main(argv=None):
    p = argparse.ArgumentParser(description="Tuesday/Thursday Caretaker review of aging open issues (#2573)")
    p.add_argument("--apply", action="store_true", help="Close obsolete issues on GitHub")
    p.add_argument("--days", type=int, default=14, help="Staleness threshold in days (default: 14)")
    args = p.parse_args(argv)
    out = run_caretaker(apply_changes=args.apply, days=args.days)
    print(out["summary"])
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

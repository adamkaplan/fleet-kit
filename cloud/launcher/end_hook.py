#!/usr/bin/env python3
"""fleet-end-hook: runs inside the pod before anything is deleted (contract section 5).

Commits uncommitted work to a WIP commit (secret-looking files left out and reported), pushes HEAD to
<worker>/wip when it is on no remote branch, and prints one JSON line: {pushed, wip_branch, skipped, error}.
A push failure prints `error` and exits 1, so the launcher keeps the pod.
"""
import fnmatch
import json
import os
import subprocess
import sys

SECRET_GLOBS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*",
                "*secret*", "*.kubeconfig", "kubeconfig", ".netrc", ".npmrc"]


def git(workdir, *args, check=True):
    p = subprocess.run(["git", "-C", workdir] + list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or ["git %s failed" % args[0]])[-1])
    return p


def is_secret(path):
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base.lower(), g) for g in SECRET_GLOBS)


def run(workdir, worker):
    status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    paths = [e[3:] for e in status.split("\0") if len(e) > 3]
    skipped = sorted(p for p in paths if is_secret(p))
    if paths:
        keep = [p for p in paths if not is_secret(p)]
        if keep:
            git(workdir, "add", "-A", "--", *keep)
            if git(workdir, "diff", "--cached", "--quiet", check=False).returncode != 0:
                git(workdir, "-c", "user.name=fleet-worker", "-c", "user.email=fleet-worker@invalid",
                    "commit", "-m", "WIP: work in progress saved by %s at the end of the worker" % worker)
    contained = git(workdir, "branch", "-r", "--contains", "HEAD").stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    git(workdir, "push", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch)
    return {"pushed": True, "wip_branch": branch, "skipped": skipped}


def main():
    workdir, worker = os.environ.get("FLEET_WORKDIR", "/work/repo"), os.environ.get("FLEET_WORKER", "")
    if not worker:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": "FLEET_WORKER is not set"}))
        return 1
    try:
        print(json.dumps(run(workdir, worker)))
        return 0
    except RuntimeError as e:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

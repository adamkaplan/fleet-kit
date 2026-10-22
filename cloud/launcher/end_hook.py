#!/usr/bin/env python3
"""cw-end-hook: runs inside the pod before anything is deleted (contract section 5).

Commits uncommitted work to a WIP commit (secret-looking files left out and reported), pushes HEAD to
<worker>/wip when it is on no remote branch, and prints one JSON line: {pushed, wip_branch, skipped, error}.
A push failure prints `error` and exits 1, so the launcher keeps the pod.
"""
import fnmatch
import json
import os
import re
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


def run(workdir, worker, prestop=False):
    status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    paths = [e[3:] for e in status.split("\0") if len(e) > 3]
    skipped = sorted(p for p in paths if is_secret(p))
    if paths:
        keep = [p for p in paths if not is_secret(p)]
        if keep:
            git(workdir, "add", "-A", "--", *keep)
            if git(workdir, "diff", "--cached", "--quiet", check=False).returncode != 0:
                git(workdir, "-c", "user.name=cw-worker", "-c", "user.email=cw-worker@invalid",
                    "commit", "-m", "WIP: work in progress saved by %s at the end of the worker" % worker)
    git(workdir, "fetch", "--prune", "origin")  # refs must be current: a stale one could call unpushed work safe
    contained = git(workdir, "branch", "-r", "--contains", "HEAD").stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    # prestop: a plain push, never forced and no lease: a non-fast-forward answer is a failure, not an overwrite
    git(workdir, "push", *([] if prestop else ["--force-with-lease"]), "origin", "HEAD:refs/heads/" + branch)
    return {"pushed": True, "wip_branch": branch, "skipped": skipped}


def prestop_main():
    """--prestop: silent when there is nothing to do; one JSON line on success or failure is replaced by plain text for cw-prestop."""
    workdir, worker = os.environ.get("CW_WORKDIR", "/work/repo"), os.environ.get("CW_WORKER", "")
    if not worker or not os.path.isdir(os.path.join(workdir, ".git")):
        return 0
    try:
        res = run(workdir, worker, prestop=True)
        if res["pushed"]:
            print("cw-prestop: saved work to %s" % res["wip_branch"])
        return 0
    except (RuntimeError, OSError) as e:
        msg = " ".join(str(e).split())
        msg = re.sub(r"https?://\S+", "<url>", msg)[:80]
        print("cw-prestop: not pushed (%s); work left in %s" % (msg, workdir))
        return 0


def main():
    if "--prestop" in sys.argv[1:]:
        return prestop_main()
    workdir, worker = os.environ.get("CW_WORKDIR", "/work/repo"), os.environ.get("CW_WORKER", "")
    if not worker:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": "CW_WORKER is not set"}))
        return 1
    try:
        print(json.dumps(run(workdir, worker)))
        return 0
    except (RuntimeError, OSError) as e:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

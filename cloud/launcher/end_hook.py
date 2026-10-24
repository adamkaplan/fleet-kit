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


# Pre-stop git runs inside a repository the agent controls: neutralise everything in it that would run code or pick a target.
SAFE_OPTS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=false",
             "-c", "protocol.ext.allow=never", "-c", "core.pager=cat", "-c", "diff.external=",
             "-c", "core.attributesFile=/dev/null", "-c", "url.https://github.com/.insteadOf=https://github.com/"]
DROP_ENV = ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_ASKPASS", "SSH_ASKPASS",
            "GIT_CONFIG_NOSYSTEM", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def git(workdir, *args, check=True, safe=False):
    cmd = ["git", "-C", workdir] + (SAFE_OPTS if safe else []) + list(args)
    env = None
    if safe:
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or ["git %s failed" % args[0]])[-1])
    return p


def is_secret(path):
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base.lower(), g) for g in SECRET_GLOBS)


WORKER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")  # no slash, no space, no leading dash


def valid_worker(worker):
    return bool(WORKER_RE.match(worker)) and ".." not in worker and not worker.endswith((".", ".lock"))


def norm_repo(url):
    """owner/name of a github.com URL (https or scp form), lower case, without .git; None if it is not one."""
    m = re.match(r"^(?:https?://(?:[^/@]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$", url.strip(), re.I)
    return m.group(1).lower() if m else None


class OriginElsewhere(Exception):
    pass


def run(workdir, worker, prestop=False, repo=None):
    sf = prestop
    url = "https://github.com/%s.git" % repo if prestop else None
    if prestop:
        cur = git(workdir, "config", "--local", "--get", "remote.origin.url", check=False, safe=True).stdout.strip()
        if cur and norm_repo(cur) != repo.lower():
            raise OriginElsewhere()
        rw = git(workdir, "config", "--local", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", check=False, safe=True).stdout
        base = "https://github.com/%s.git" % repo
        if any(base.startswith(l.split(None, 1)[1]) for l in rw.splitlines() if " " in l):
            raise OriginElsewhere()
    status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", safe=sf).stdout
    paths = [e[3:] for e in status.split("\0") if len(e) > 3]
    skipped = sorted(p for p in paths if is_secret(p))
    if paths:
        keep = [p for p in paths if not is_secret(p)]
        if keep:
            git(workdir, "add", "-A", "--", *keep, safe=sf)
            if git(workdir, "diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", check=False, safe=sf).returncode != 0:
                git(workdir, "-c", "user.name=cw-worker", "-c", "user.email=cw-worker@invalid",
                    "commit", *(["--no-verify"] if prestop else []), "-m", "WIP: work in progress saved by %s at the end of the worker" % worker, safe=sf)
    # refs must be current: a stale one could call unpushed work safe. prestop fetches from the explicit URL, never the configured origin
    if prestop:
        git(workdir, "fetch", "--prune", url, "+refs/heads/*:refs/remotes/origin/*", safe=True)
    else:
        git(workdir, "fetch", "--prune", "origin")
    contained = git(workdir, "branch", "-r", "--contains", "HEAD", safe=sf).stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    # prestop: a plain push, never forced and no lease: a non-fast-forward answer is a failure, not an overwrite
    if prestop:
        git(workdir, "push", "--no-verify", url, "HEAD:refs/heads/" + branch, safe=True)
    else:
        git(workdir, "push", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch)
    return {"pushed": True, "wip_branch": branch, "skipped": skipped}


def prestop_main():
    """--prestop: the branch is always <worker>/wip. The worker name comes from CW_WORKER, set by the pod spec (fixed at container start,
    the kubelet runs this hook with the spec's environment, not the coder's shell); no argument and no other variable is read.
    silent when there is nothing to do; one JSON line on success or failure is replaced by plain text for cw-prestop."""
    workdir, worker = os.environ.get("CW_WORKDIR", "/work/repo"), os.environ.get("CW_WORKER", "")
    if not valid_worker(worker):
        print("cw-prestop: not pushed (the worker name is not valid); work left in %s" % workdir)
        return 0
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return 0
    repo = os.environ.get("CW_REPO", "")
    if not REPO_RE.match(repo) or repo.startswith(".") or ".." in repo:
        print("cw-prestop: not pushed (the repository name is not valid); work left in %s" % workdir)
        return 0
    try:
        res = run(workdir, worker, prestop=True, repo=repo)
        if res["pushed"]:
            print("cw-prestop: saved work to %s" % res["wip_branch"])
        return 0
    except OriginElsewhere:
        print("cw-prestop: not pushed (origin points elsewhere); work left in %s" % workdir)
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

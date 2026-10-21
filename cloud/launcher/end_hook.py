#!/usr/bin/env python3
"""cw-end-hook: runs inside the pod before anything is deleted (contract section 5).

Commits uncommitted work to a WIP commit (secret-looking files left out and reported), pushes HEAD to
<worker>/wip when it is on no remote branch, and prints one JSON line: {pushed, wip_branch, skipped, error}.
A push failure prints `error` and exits 1, so the launcher keeps the pod.

With `--token-stdin` the launcher's freshly minted repository token arrives on the standard input (one line, read once).
It is used for this hook's own `git fetch` and `git push` only: it reaches git in the environment of that one child
process, through a credential helper whose text holds no token, replacing the pod's own helper for those calls. It is never
an argument, never written to a file, never printed. Without the flag the hook behaves as before.
A missing default checkout (/work/repo: the clone never happened) is not an error: nothing to push, `note` says so.
"""
import fnmatch
import json
import os
import re
import subprocess
from urllib.parse import urlparse
import sys

SECRET_GLOBS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*",
                "*secret*", "*.kubeconfig", "kubeconfig", ".netrc", ".npmrc"]


DEFAULT_WORKDIR = "/work/repo"
GIT_BASE = "https://github.com"   # the only place the fresh token is offered to (CW_HOOK_GIT_BASE: the pod's own setting, for tests)
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")
HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-c", "core.askPass=", "-c", "core.sshCommand=false",
          "-c", "http.proxy=", "-c", "credential.useHttpPath=false"]
MAX_TOKEN = 4096
# One-shot credential source: answers only for the host of origin, with the token from the environment of this child.
HELPER = ('!f() { [ "$1" = get ] || exit 0; h=""; pr=""; while IFS="=" read -r k v; do [ "$k" = host ] && h=$v; [ "$k" = protocol ] && pr=$v; [ -z "$k" ] && break; done; '
          '[ -n "$CW_PUSH_TOKEN" ] && [ "$h" = "$CW_PUSH_HOST" ] && [ "$pr" = "$CW_PUSH_SCHEME" ] || exit 0; '
          'printf "username=x-access-token\\npassword=%s\\n" "$CW_PUSH_TOKEN"; }; f')


def read_token(stream):
    """The one line of the launcher's fresh token (None when none came). Read once, never echoed."""
    raw = stream.read(MAX_TOKEN + 1) or ""
    tok = raw.strip()
    return tok if tok and len(tok) <= MAX_TOKEN and "\n" not in tok else None


def git_base():
    return (os.environ.get("CW_HOOK_GIT_BASE") or GIT_BASE).rstrip("/")


def remote_url():
    """The push target when a fresh token is used: the fixed base plus the repository the POD was started for (its own
    environment), never the `origin` an agent-writable repository config names (that could be a local path whose
    receivepack command would run with the token in its environment)."""
    repo = os.environ.get("CW_REPO", "")
    if not REPO_RE.match(repo):
        raise RuntimeError("no repository is recorded for this worker")
    return "%s/%s.git" % (git_base(), repo)


def git(workdir, *args, check=True, token=None):
    cmd, env = ["git", "-C", workdir], None
    if token:   # the helper text carries no token; the token is in this one child's environment only
        # The repository is the agent's and its config is not trusted: the helper answers for ONE fixed host (never the
        # origin URL read from that config), and nothing the repository can name runs with the token in its environment.
        cmd += ["-c", "credential.helper=", "-c", "credential.helper=" + HELPER] + HARDEN
        u = urlparse(git_base())
        env = dict(os.environ, CW_PUSH_TOKEN=token, CW_PUSH_HOST=u.netloc, CW_PUSH_SCHEME=u.scheme, GIT_ALLOW_PROTOCOL=u.scheme,
                   GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(cmd + list(args), capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or ["git %s failed" % args[0]])[-1])
    return p


def is_secret(path):
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base.lower(), g) for g in SECRET_GLOBS)


def run(workdir, worker, token=None):
    if not os.path.exists(workdir) and "CW_WORKDIR" not in os.environ:   # the clone never happened: nothing to save
        return {"pushed": False, "wip_branch": None, "skipped": [], "note": "no checkout; nothing to push"}
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
    target = remote_url() if token else "origin"
    # refs must be current: a stale one could call unpushed work safe
    if token:
        git(workdir, "fetch", "--prune", target, "+refs/heads/*:refs/remotes/origin/*", token=token)
    else:
        git(workdir, "fetch", "--prune", "origin")
    contained = git(workdir, "branch", "-r", "--contains", "HEAD").stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    if token:   # an explicit URL has no tracking branch: the lease names what the fetch just saw (or that the branch is absent)
        seen = git(workdir, "rev-parse", "-q", "--verify", "refs/remotes/origin/" + branch, check=False).stdout.strip()
        git(workdir, "push", "--force-with-lease=refs/heads/%s:%s" % (branch, seen), target, "HEAD:refs/heads/" + branch, token=token)
    else:
        git(workdir, "push", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch)
    return {"pushed": True, "wip_branch": branch, "skipped": skipped}


def main():
    workdir, worker = os.environ.get("CW_WORKDIR", "/work/repo"), os.environ.get("CW_WORKER", "")
    if not worker:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": "CW_WORKER is not set"}))
        return 1
    token = read_token(sys.stdin) if "--token-stdin" in sys.argv[1:] else None
    try:
        print(json.dumps(run(workdir, worker, token)))
        return 0
    except (RuntimeError, OSError) as e:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e).replace(token, "") if token else str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

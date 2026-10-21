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
import subprocess
import sys

SECRET_GLOBS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*",
                "*secret*", "*.kubeconfig", "kubeconfig", ".netrc", ".npmrc"]


DEFAULT_WORKDIR = "/work/repo"
MAX_TOKEN = 4096
# One-shot credential source: answers only for the host of origin, with the token from the environment of this child.
HELPER = ('!f() { [ "$1" = get ] || exit 0; h=""; while IFS="=" read -r k v; do [ "$k" = host ] && h=$v; [ -z "$k" ] && break; done; '
          '[ -n "$CW_PUSH_TOKEN" ] && [ "$h" = "$CW_PUSH_HOST" ] || exit 0; '
          'printf "username=x-access-token\\npassword=%s\\n" "$CW_PUSH_TOKEN"; }; f')


def read_token(stream):
    """The one line of the launcher's fresh token (None when none came). Read once, never echoed."""
    raw = stream.read(MAX_TOKEN + 1) or ""
    tok = raw.strip()
    return tok if tok and len(tok) <= MAX_TOKEN and "\n" not in tok else None


def origin_host(workdir):
    from urllib.parse import urlparse
    p = subprocess.run(["git", "-C", workdir, "remote", "get-url", "origin"], capture_output=True, text=True)
    return urlparse(p.stdout.strip()).netloc.rpartition("@")[2]   # host[:port], as git names it to a helper


def git(workdir, *args, check=True, token=None):
    cmd, env = ["git", "-C", workdir], None
    if token:   # the helper text carries no token; the token is in this one child's environment only
        cmd += ["-c", "credential.helper=", "-c", "credential.helper=" + HELPER]
        env = dict(os.environ, CW_PUSH_TOKEN=token, CW_PUSH_HOST=origin_host(workdir), GIT_TERMINAL_PROMPT="0")
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
    git(workdir, "fetch", "--prune", "origin", token=token)  # refs must be current: a stale one could call unpushed work safe
    contained = git(workdir, "branch", "-r", "--contains", "HEAD").stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    git(workdir, "push", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch, token=token)
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

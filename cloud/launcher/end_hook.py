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
import shutil
import subprocess
import sys
import tempfile

SECRET_GLOBS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*",
                "*secret*", "*.kubeconfig", "kubeconfig", ".netrc", ".npmrc"]


# Pre-stop git runs inside a repository the agent controls. ALLOW-LIST design: git reads NO global or system configuration and
# no variable of the caller; the only configuration it sees is the pinned -c options below plus the repository's own file, which
# is read first and refused unless every key of it is on a short allow-list (see config_refusal).
# TEST SEAM (the only one): the tests import this module and patch HOST_URL (a file:// prefix standing for the host) and HELPER_DIR.
# Nothing here is read from the environment, so the agent cannot move the push target or the credential helper.
HOST_URL = "https://github.com/"
HELPER_DIR = "/run/cw-tmp/bin"   # the directory entrypoint.sh fills ($TMP/bin, TMP=/run/cw-tmp); the helper is git-credential-cw in it
HELPER_NAME = "git-credential-cw"
KEEP_ENV = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TMPDIR")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def safe_opts(helper):
    """Pinned configuration of every pre-stop git call. credential.helper is reset (empty value) before the one fixed helper."""
    pins = ["core.hooksPath=/dev/null", "core.fsmonitor=false", "core.sshCommand=false", "protocol.ext.allow=never",
            "core.pager=cat", "diff.external=", "core.attributesFile=/dev/null", "http.proxy=", "http.extraHeader=",
            "core.gitProxy=", "core.askPass=", "http.sslVerify=true", "http.followRedirects=false", "credential.useHttpPath=true",
            "credential.helper=", "credential.helper=" + helper]
    return [x for pin in pins for x in ("-c", pin)]


def find_helper():
    """The pod's credential helper (written by entrypoint.sh, answers only for github.com); None if it is not there."""
    p = os.path.join(HELPER_DIR, HELPER_NAME)
    return p if os.path.isfile(p) and not os.path.islink(p) and os.access(p, os.X_OK) else None


class Ctx:
    """The scrubbed environment and pinned options of one pre-stop run; `home` is an empty private directory."""
    def __init__(self, helper):
        self.home = tempfile.mkdtemp(prefix="cw-prestop-")
        self.opts = safe_opts(helper)
        env = {k: v for k, v in os.environ.items() if k in KEEP_ENV}
        env.update({"HOME": self.home, "GIT_CONFIG_" + "GLOBAL": "/dev/null", "GIT_CONFIG_" + "SYSTEM": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        self.env = env

    def close(self):
        shutil.rmtree(self.home, True)


def git(workdir, *args, check=True, ctx=None, opts=True):
    cmd = ["git", "-C", workdir] + (ctx.opts if ctx and opts else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, env=ctx.env if ctx else None)
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


class ConfigRefused(Exception):
    pass


# The repository's own configuration is an untrusted input: ANY key outside this list is a refusal (this covers include.*, includeIf.*,
# url.*, credential.*, http.*, every program-running key and every other remote).
ALLOWED_KEYS = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates", "core.ignorecase",
                "core.precomposeunicode", "core.symlinks", "user.name", "user.email", "remote.origin.url", "remote.origin.pushurl",
                "remote.origin.fetch", "init.defaultbranch", "receive.denycurrentbranch"}
ALLOWED_RE = re.compile(r"^(?:branch\..+\.(?:remote|merge)|(?:gc|pack)\.[^.]+)$")


def config_refusal(workdir, repo, ctx):
    """Read the repository's local configuration, includes followed, with the origin of every key; raise on the first key that is not
    plain. A key that comes from any file but the repository's own config file (an include) is a refusal too."""
    p = subprocess.run(["git", "-C", workdir, "config", "--local", "--includes", "--show-origin", "--list", "-z"],
                       capture_output=True, env=ctx.env)
    if p.returncode != 0:
        raise RuntimeError("the repository configuration could not be read")
    parts = p.stdout.decode("utf-8", "replace").split("\0")
    own = {"file:.git/config", "file:" + os.path.join(workdir, ".git", "config"),
           "file:" + os.path.join(os.path.realpath(workdir), ".git", "config")}
    for i in range(0, len(parts) - 1, 2):
        origin, entry = parts[i], parts[i + 1]
        key, _, value = entry.partition("\n")
        shown = "".join(c if c.isprintable() else "?" for c in key)[:60]
        if origin not in own:
            raise ConfigRefused(shown)
        if key not in ALLOWED_KEYS and not ALLOWED_RE.match(key):
            raise ConfigRefused(shown)
        if key in ("remote.origin.url", "remote.origin.pushurl") and norm_repo(value) != repo.lower():
            raise OriginElsewhere()
        if re.match(r"^branch\..+\.remote$", key) and value not in ("origin", "."):
            raise ConfigRefused(shown)


def run(workdir, worker, prestop=False, repo=None, ctx=None):
    url = HOST_URL + "%s.git" % repo if prestop else None
    if prestop:
        config_refusal(workdir, repo, ctx)
    status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", ctx=ctx).stdout
    paths = [e[3:] for e in status.split("\0") if len(e) > 3]
    skipped = sorted(p for p in paths if is_secret(p))
    if paths:
        keep = [p for p in paths if not is_secret(p)]
        if keep:
            git(workdir, "add", "-A", "--", *keep, ctx=ctx)
            if git(workdir, "diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", check=False, ctx=ctx).returncode != 0:
                git(workdir, "-c", "user.name=cw-worker", "-c", "user.email=cw-worker@invalid",
                    "commit", *(["--no-verify"] if prestop else []), "-m", "WIP: work in progress saved by %s at the end of the worker" % worker, ctx=ctx)
    # refs must be current: a stale one could call unpushed work safe. prestop fetches from the explicit URL, never the configured origin
    if prestop:
        git(workdir, "fetch", "--prune", url, "+refs/heads/*:refs/remotes/origin/*", ctx=ctx)
    else:
        git(workdir, "fetch", "--prune", "origin")
    contained = git(workdir, "branch", "-r", "--contains", "HEAD", ctx=ctx).stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped}
    branch = "%s/wip" % worker
    # prestop: a plain push to the explicit URL, never forced and no lease: a non-fast-forward answer is a failure, not an overwrite
    if prestop:
        git(workdir, "push", "--no-verify", url, "HEAD:refs/heads/" + branch, ctx=ctx)
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
    helper = find_helper()
    if not helper:
        print("cw-prestop: not pushed (the pod's credential helper was not found); work left in %s" % workdir)
        return 0
    ctx = Ctx(helper)
    try:
        res = run(workdir, worker, prestop=True, repo=repo, ctx=ctx)
        if res["pushed"]:
            print("cw-prestop: saved work to %s" % res["wip_branch"])
        return 0
    except OriginElsewhere:
        print("cw-prestop: not pushed (origin points elsewhere); work left in %s" % workdir)
        return 0
    except ConfigRefused as e:
        print("cw-prestop: not pushed (the repository configuration is not a plain one: %s); work left in %s" % (e, workdir))
        return 0
    except (RuntimeError, OSError) as e:
        msg = " ".join(str(e).split())
        msg = re.sub(r"https?://\S+", "<url>", msg)[:80]
        print("cw-prestop: not pushed (%s); work left in %s" % (msg, workdir))
        return 0
    finally:
        ctx.close()


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

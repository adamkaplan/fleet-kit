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
# git is run by ABSOLUTE path, never found on PATH: in a pod the guard wrapper ($TMP/bin/git, first on the entrypoint's PATH) refuses the
# hook's own calls (a global-configuration override variable in the environment) and would make every pre-stop push fail. The wrapper itself execs the real
# binary the image installs (cloud/worker/entrypoint.sh: CW_REAL_GIT, found before the wrapper exists); the image's git package puts
# it at /usr/bin/git. Test seam: the tests patch GIT_BIN like the two constants above.
GIT_CANDIDATES = ("/usr/bin/git", "/usr/local/bin/git")


def _real_git():
    for c in GIT_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return GIT_CANDIDATES[0]


GIT_BIN = _real_git()
KEEP_ENV = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TMPDIR")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")   # owner and name start with a letter or a digit


def safe_opts(helper):
    """Pinned configuration of every pre-stop git call. credential.helper is reset (empty value) before the one fixed helper."""
    pins = ["core.hooksPath=/dev/null", "core.fsmonitor=false", "core.sshCommand=false", "protocol.ext.allow=never",
            "core.pager=cat", "diff.external=", "core.attributesFile=/dev/null", "http.proxy=", "http.extraHeader=",
            "core.gitProxy=", "core.askPass=", "http.sslVerify=true", "http.followRedirects=false", "credential.useHttpPath=true",
            "credential.helper=", "credential.helper=" + helper,
            # an automatic gc/maintenance run must never happen inside the hook (gc has keys that run a program)
            "gc.auto=0", "maintenance.auto=false", "receive.autogc=false"]
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


# A refusal because the git token had ended (a one-hour token) says one of these. A rejected ref, a hostile configuration or a network
# failure says none of them, and so never asks for a refresh.
ENDED_TOKEN_RE = re.compile(r"Authentication failed|Invalid username or token|Bad credentials|returned error: 401|HTTP 401", re.I)


class TokenEnded(RuntimeError):
    """A git call refused because the token had ended: the one case in which the hook signals a refresh (see main)."""


def git(workdir, *args, check=True, ctx=None, opts=True):
    cmd = [GIT_BIN, "-C", workdir] + (ctx.opts if ctx and opts else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, env=ctx.env if ctx else None)
    if check and p.returncode != 0:
        if args and args[0] in ("push", "fetch") and ENDED_TOKEN_RE.search(p.stderr or ""):
            raise TokenEnded("the git token had ended")   # a fixed text: nothing of the remote's answer is carried
        raise RuntimeError((p.stderr.strip().splitlines() or ["git %s failed" % args[0]])[-1])
    return p


def is_secret(path):
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base.lower(), g) for g in SECRET_GLOBS)


WORKER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")  # no slash, no space, no leading dash


def valid_worker(worker):
    return bool(WORKER_RE.match(worker)) and ".." not in worker and not worker.endswith((".", ".lock"))


def norm_repo(url):
    """owner/name of a github.com URL (https or scp form), lower case, without .git; None if it is not one. Userinfo in an https URL
    is stripped before the comparison; this is harmless because the push goes to the URL built from CW_REPO whatever the remote says."""
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
# NO wildcard section: gc.* and pack.* are gone (gc.recentObjectsHook runs a command). The only pattern is the two exact named forms
# branch.<name>.remote (value origin or .) and branch.<name>.merge (value refs/heads/...).
BRANCH_RE = re.compile(r"^branch\.(.+)\.(remote|merge)$")


def config_refusal(workdir, repo, ctx):
    """Read the repository's local configuration, includes followed, with the origin of every key; raise on the first key that is not
    plain. A key that comes from any file but the repository's own config file (an include) is a refusal too."""
    p = subprocess.run([GIT_BIN, "-C", workdir, "config", "--local", "--includes", "--show-origin", "--list", "-z"],
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
        if key not in ALLOWED_KEYS and not BRANCH_RE.match(key):
            raise ConfigRefused(shown)
        if key in ("remote.origin.url", "remote.origin.pushurl") and norm_repo(value) != repo.lower():
            raise OriginElsewhere()
        m = BRANCH_RE.match(key)
        if m and m.group(2) == "remote" and value not in ("origin", "."):
            raise ConfigRefused(shown)
        if m and m.group(2) == "merge" and not value.startswith("refs/heads/"):
            raise ConfigRefused(shown)


def run(workdir, worker, prestop=False, repo=None, ctx=None):
    url = HOST_URL + "%s.git" % repo if prestop else None
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


MAX_NAMES = 50
PREFIX = "cw-prestop: "


def unpushed_names(workdir, ctx, trusted):
    """Names (never contents) of the files whose work is not on a remote, at most MAX_NAMES, sorted; best effort, never raises.
    Untrusted configuration (a refusal, a bad name): only `ls-files --others` runs, which applies no filter and no hook; once the
    configuration has passed the allow-list the status of the tree and the commits on no remote branch are listed too."""
    names = set()

    def add(text):
        for n in text.replace("\0", "\n").split("\n"):
            if n.strip():
                names.add("".join(c if c.isprintable() else "?" for c in n)[:100])
    try:
        add(git(workdir, "ls-files", "--others", "--exclude-standard", "-z", check=False, ctx=ctx).stdout)
        if trusted:
            status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", check=False, ctx=ctx).stdout
            add("\n".join(e[3:] for e in status.split("\0") if len(e) > 3))
            add(git(workdir, "log", "--name-only", "--pretty=format:", "-z", "HEAD", "--not", "--remotes", check=False, ctx=ctx).stdout)
    except (OSError, ValueError):
        pass
    return sorted(names)


def report(workdir, reason, ctx=None, trusted=False, names=True):
    """Exactly one line with a stable prefix, on stdout AND stderr (cw-prestop keeps the last line in the container's output). The line
    is the only trace of a refused run: it lives in the pod's log and ends with the pod unless the cluster ships logs elsewhere (nothing
    in the repository keeps it); the launcher does not collect the hook's output. It says plainly that the work
    is LOST when the pod ends, and lists the unpushed file names (at most MAX_NAMES) so a person can ask for them while the pod exists."""
    own = None
    listed = []
    if names:
        try:
            if ctx is None:
                own = ctx = Ctx("/nonexistent")
            listed = unpushed_names(workdir, ctx, trusted)
        except OSError:
            listed = []
        finally:
            if own:
                own.close()
    line = "%snot pushed (%s); work left in %s and is LOST when the pod ends" % (PREFIX, reason, workdir)
    if listed:
        line += "; unpushed files: " + ", ".join(listed[:MAX_NAMES]) + (" (+%d more)" % (len(listed) - MAX_NAMES) if len(listed) > MAX_NAMES else "")
    print(line)
    print(line, file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    return 0


def prestop_main():
    """--prestop: the branch is always <worker>/wip. The worker name comes from CW_WORKER, set by the pod spec (fixed at container start,
    the kubelet runs this hook with the spec's environment, not the coder's shell); no argument and no other variable is read.
    Silent only when there is nothing to save; EVERY other return prints one `cw-prestop: ...` line (see report)."""
    workdir, worker = os.environ.get("CW_WORKDIR", "/work/repo"), os.environ.get("CW_WORKER", "")
    if not valid_worker(worker):
        return report(workdir, "the worker name is not valid", names=False)
    if not os.path.exists(os.path.join(workdir, ".git")):
        return report(workdir, "there is no git repository here", names=False)
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return report(workdir, ".git is a file: a linked worktree or a separate git directory is not supported", names=False)
    repo = os.environ.get("CW_REPO", "")
    if not REPO_RE.match(repo) or ".." in repo:
        return report(workdir, "the repository name is not valid")
    helper = find_helper()
    if not helper:
        return report(workdir, "the pod's credential helper was not found")
    ctx = Ctx(helper)
    trusted = False
    try:
        config_refusal(workdir, repo, ctx)
        trusted = True
        res = run(workdir, worker, prestop=True, repo=repo, ctx=ctx)
        if res["pushed"]:
            print("cw-prestop: saved work to %s" % res["wip_branch"])
        return 0
    except OriginElsewhere:
        return report(workdir, "origin points elsewhere", ctx)
    except ConfigRefused as e:
        return report(workdir, "the repository configuration is not a plain one: %s" % e, ctx)
    except (RuntimeError, OSError) as e:
        msg = " ".join(str(e).split())
        msg = re.sub(r"https?://\S+", "<url>", msg)[:80]
        return report(workdir, msg, ctx, trusted)
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
    except TokenEnded as e:
        # ONE word to the stop side: ask for a fresh token and run me once more. The hook holds no credential for the minter and asks no one itself.
        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e), "refresh": "needed"}))
        return 1
    except (RuntimeError, OSError) as e:
        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

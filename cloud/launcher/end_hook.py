#!/usr/bin/env python3
"""cw-end-hook: runs inside the pod before anything is deleted (contract section 5).

Commits uncommitted work to a WIP commit (secret-looking files left out and reported), pushes HEAD to
<worker>/wip when it is on no remote branch, and prints one JSON line: {pushed, wip_branch, skipped, untracked, untracked_more, error}.
Cause of the untracked keys (ask #1289): the status list is staged whole (every untracked file but the secret-looking ones), and the
line before that change names only the skipped secret-looking paths, so a stray untracked file (a test's leftover, a scratch file) was saved to the
wip branch without a word. `untracked` now lists the untracked paths that were saved (sorted, at most MAX_UNTRACKED, never content);
`untracked_more` counts the rest. A pod cannot know a declared set of files, so the line names all untracked paths saved; a
secret-looking path is never listed there, only under `skipped`.
A push failure prints `error` and exits 1, so the launcher keeps the pod.
"""
import fnmatch
import json
import os
import re
import shutil
import stat
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


def head_branch(contained):
    """The first branch of `git branch -r --contains` with its origin/ prefix off: at most 100 characters of [A-Za-z0-9._/-], else `unknown`."""
    lines = [x.strip() for x in (contained or "").splitlines() if x.strip()]
    lines = [x for x in lines if "->" not in x and x != "origin/HEAD"]
    name = lines[0].split(" ")[0] if lines else ""
    name = name[len("origin/"):] if name.startswith("origin/") else name
    return name if name and len(name) <= 100 and re.fullmatch(r"[A-Za-z0-9._/-]+", name) else "unknown"


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


class NothingToPush(dict):
    """The result of a run that decided from local references that there was nothing to push (a dict like any other result)."""


FETCH_TIMEOUT_SECONDS = 20


def clean_local(workdir, ctx):
    """True when, from local references only, nothing is unsaved. WORK is any commit reachable from the reflog of HEAD or of any local
    branch, from every entry of the refs/stash reflog, from any tag, from any local branch, or from any worktree's HEAD, that no remote-tracking ref and not the clone's
    starting commit (the oldest entry of the HEAD reflog) reaches; also any change (staged, untracked) in this or any other worktree.
    Any git command that fails, or any doubt, is NOT clean.

    Cause (S1): a read-only reader pod was kept and retried because the end hook did not decide from local state before the remote
    call: it always ran `git fetch` first, and with an ended token that fetch failed (or a local branch had no remote counterpart),
    so the pod was kept as "unsaved" and the stop was retried on every pass. A clean tree needs no remote call."""
    def out(*args):
        p = git(workdir, *args, check=False, ctx=ctx)
        if p.returncode != 0:
            raise RuntimeError("git failed")
        return p.stdout
    try:
        start = out("reflog", "show", "--format=%H", "HEAD").split()
        if not start:
            return False
        roots = set(start)
        for line in out("for-each-ref", "--format=%(refname)", "refs/heads").split():
            roots.add(line)
            roots.update(out("reflog", "show", "--format=%H", line).split())
        roots.update(out("for-each-ref", "--format=%(objectname)", "refs/stash").split())
        if out("for-each-ref", "--format=%(refname)", "refs/stash").strip():
            roots.update(out("reflog", "show", "--format=%H", "refs/stash").split())
        roots.update(out("for-each-ref", "--format=%(refname)", "refs/tags").split())
        for line in out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                wt = line[9:]
                if out("-C", wt, "status", "--porcelain", "--untracked-files=all").strip():
                    return False
            elif line.startswith("HEAD "):
                roots.add(line[5:])
        roots = [r for r in roots if set(r) != {"0"}]
        res = out("rev-list", "--max-count=1", *roots, "--not", "--remotes", start[-1], "--")
        return res.strip() == ""
    except (RuntimeError, OSError):
        return False


def refresh_refs(workdir, remote, ctx):
    """One bounded `git fetch --prune` (no loop, no retry). True if it worked; False on any failure or on the timeout. Its output is
    discarded and never printed or logged."""
    if ctx:
        cmd = [GIT_BIN, "-C", workdir] + ctx.opts + ["fetch", "--prune", remote, "+refs/heads/*:refs/remotes/origin/*"]
    else:
        cmd = [GIT_BIN, "-C", workdir, "fetch", "--prune", remote]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ctx.env if ctx else None,
                           timeout=FETCH_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0

NO_REPOSITORY_NOTE = "no repository in the pod; nothing to push"


def probe_path(path):
    """'absent' only when lstat of the path, or of a parent on the way, says FileNotFoundError or NotADirectoryError; 'dir' for a real
    directory. Any other OSError (permission, loop, name too long) propagates; a link at the path, a dangling link on the way, or a
    non-directory at the path raises RuntimeError. Never the os.path helpers: they turn an error into "not found"."""
    path = os.path.abspath(path)
    parts = [p for p in path.split(os.sep) if p]
    cur = os.sep
    for i, part in enumerate(parts):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except (FileNotFoundError, NotADirectoryError):
            return "absent"
        if stat.S_ISLNK(st.st_mode):
            if i == len(parts) - 1:
                raise RuntimeError("%s is a link, not a directory" % path)
            try:
                os.stat(cur)
            except (FileNotFoundError, NotADirectoryError):
                raise RuntimeError("%s is a dangling link" % cur)
        elif i == len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("%s is not a directory" % path)
    return "dir"


def has_marker(directory):
    """True when directory holds a .git entry (file or directory); False only when lstat says it is absent; other errors propagate."""
    try:
        os.lstat(os.path.join(directory, ".git"))
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def first_entry(directory):
    """True when the directory holds any entry at all (a listing error propagates)."""
    with os.scandir(directory) as it:
        for _ in it:
            return True
    return False


def no_checkout(workdir):
    """True only when the repository directory is absent or empty AND the workspace is absent or empty. Any entry in the
    workspace, or a directory the hook cannot read, raises (the hook exits 1, the pod is kept)."""
    state = probe_path(workdir)
    if state == "dir":
        if has_marker(workdir):
            return False
        if first_entry(workdir):
            raise RuntimeError("%s holds files but no .git: not deleting the pod" % workdir)
        return True
    workspace = os.path.dirname(os.path.abspath(workdir))
    if probe_path(workspace) == "absent":
        return True
    if first_entry(workspace):
        raise RuntimeError("no repository at %s but the workspace %s is not empty: not deleting the pod" % (workdir, workspace))
    return True


MAX_UNTRACKED = 10
MAX_PATH_SHOWN = 200   # a path longer than this is shown cut to its first 200 characters and "..."; the count is not changed by it


def untracked_report(status_entries):
    """The untracked (`??`) paths among the status entries that the save stages (secret-looking ones are not), sorted: the first
    MAX_UNTRACKED and the count of the rest. Names only, never content. Taken from the same list that decides the save."""
    names = sorted(e[3:] for e in status_entries if e.startswith("?? ") and not is_secret(e[3:]))
    shown = [n if len(n) <= MAX_PATH_SHOWN else n[:MAX_PATH_SHOWN] + "..." for n in names[:MAX_UNTRACKED]]
    return {"untracked": shown, "untracked_more": max(0, len(names) - MAX_UNTRACKED)}


def run(workdir, worker, prestop=False, repo=None, ctx=None):
    # Three answers: no checkout at all (see no_checkout: nothing was cloned, nothing can be lost; this answer carries
    # no_repository), a checkout whose push fails (RuntimeError from git: the caller prints `error`, exit 1), and
    # nothing unpushed or pushed (as before). A state the hook cannot read is an error, never "no repository".
    if no_checkout(workdir):
        return {"pushed": False, "wip_branch": None, "skipped": [], "no_repository": True, "note": NO_REPOSITORY_NOTE}
    url = HOST_URL + "%s.git" % repo if prestop else None
    status = git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", ctx=ctx).stdout
    entries = [e for e in status.split("\0") if len(e) > 3]
    paths = [e[3:] for e in entries]
    extra = untracked_report(entries)
    skipped = sorted(p for p in paths if is_secret(p))
    if paths:
        keep = [p for p in paths if not is_secret(p)]
        if keep:
            git(workdir, "add", "-A", "--", *keep, ctx=ctx)
            if git(workdir, "diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", check=False, ctx=ctx).returncode != 0:
                git(workdir, "-c", "user.name=cw-worker", "-c", "user.email=cw-worker@invalid",
                    "commit", *(["--no-verify"] if prestop else []), "-m", "WIP: work in progress saved by %s at the end of the worker" % worker, ctx=ctx)
    if not paths and clean_local(workdir, ctx):   # a clean tree with no commit of its own: nothing to save, no remote call
        return NothingToPush({"pushed": False, "wip_branch": None, "skipped": skipped, "local_only": False, **extra})
    # Nothing to push: first a bounded fetch with prune refreshes the remote-tracking refs, so a remote branch deleted or moved since the
    # last fetch is seen. Only if that fetch itself fails (refused, network, timeout) is the answer taken from the local references.
    refreshed = refresh_refs(workdir, url if prestop else "origin", ctx)
    if git(workdir, "log", "--max-count=1", "--format=%H", "HEAD", "--not", "--remotes", check=False, ctx=ctx).stdout.strip() == "":
        return NothingToPush({"pushed": False, "wip_branch": None, "skipped": skipped, "local_only": not refreshed, **extra})
    # refs must be current: a stale one could call unpushed work safe. prestop fetches from the explicit URL, never the configured origin
    if not refreshed:
        if prestop:
            git(workdir, "fetch", "--prune", url, "+refs/heads/*:refs/remotes/origin/*", ctx=ctx)
        else:
            git(workdir, "fetch", "--prune", "origin")
    contained = git(workdir, "branch", "-r", "--contains", "HEAD", ctx=ctx).stdout.strip()
    if contained:
        return {"pushed": False, "wip_branch": None, "skipped": skipped, "head_on": head_branch(contained), **extra}
    branch = "%s/wip" % worker
    # prestop: a plain push to the explicit URL, never forced and no lease: a non-fast-forward answer is a failure, not an overwrite
    if prestop:
        git(workdir, "push", "--no-verify", url, "HEAD:refs/heads/" + branch, ctx=ctx)
    else:
        git(workdir, "push", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch)
    return {"pushed": True, "wip_branch": branch, "skipped": skipped, **extra}


# ---- the end-hook ask: a finished pod's pull request is marked ready (and the repository's review comment posted once) when the pod ends ----
GH_TIMEOUT = 30


class GhHub:
    """The pull request calls through the pod's `gh` client. Each raises RuntimeError on any failure."""
    def _gh(self, *args):
        try:
            r = subprocess.run(["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=GH_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(type(e).__name__)
        if r.returncode != 0:
            raise RuntimeError("gh exit %d" % r.returncode)
        return r.stdout

    def find(self, branch):
        """The open pull request of the branch as {number, isDraft, headRefOid}, or None."""
        found = json.loads(self._gh("pr", "list", "--head", branch, "--state", "open", "--json", "number,isDraft,headRefOid") or "[]")
        if not found:
            return None
        row = found[0]   # the pod's cw-gh prints the raw REST row (draft, head.sha); the gh field form (isDraft, headRefOid) is read too
        head = row.get("head")
        return {"number": row.get("number"),
                "isDraft": row["isDraft"] if "isDraft" in row else row.get("draft"),
                "headRefOid": row["headRefOid"] if "headRefOid" in row else (head.get("sha") if isinstance(head, dict) else None)}

    def ready(self, number):
        self._gh("pr", "ready", str(number))

    def comment(self, number, text):
        self._gh("pr", "comment", str(number), "--body", text)


def pr_note(workdir, res, hub=None, comment=None):
    """One line for the end notice: `PR #N marked ready, review requested` or `PR not marked ready: <reason>`. Never raises."""
    try:
        if res.get("no_repository"):
            return "PR not marked ready: no repository"
        if res.get("pushed"):
            return "PR not marked ready: unpushed work was left"
        ctx = Ctx("/nonexistent")
        try:
            branch = git(workdir, "rev-parse", "--abbrev-ref", "HEAD", ctx=ctx).stdout.strip()
            sha = git(workdir, "rev-parse", "HEAD", ctx=ctx).stdout.strip()
        finally:
            ctx.close()
        if not branch or branch == "HEAD":
            return "PR not marked ready: no branch"
        hub = hub or GhHub()
        pr = hub.find(branch)
        if not pr:
            return "PR not marked ready: no pull request"
        number = pr.get("number")
        if not pr.get("isDraft"):
            return "PR not marked ready: #%s is not a draft" % number
        if pr.get("headRefOid") != sha:
            return "PR not marked ready: head is not the pushed commit"
        hub.ready(number)
        if comment is None:
            comment = os.environ.get("CW_REVIEW_COMMENT", "")
        if comment:
            hub.comment(number, comment)
            return "PR #%s marked ready, review requested" % number
        return "PR #%s marked ready, no review comment is configured" % number
    except Exception as e:
        return "PR not marked ready: the call failed (%s)" % type(e).__name__


def with_pr(workdir, res, hub=None, comment=None):
    """The hook's answer with `pr_note` added."""
    res["pr_note"] = pr_note(workdir, res, hub, comment)
    return res


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
    try:
        gst = os.lstat(os.path.join(workdir, ".git"))
    except (FileNotFoundError, NotADirectoryError):
        return report(workdir, "there is no git repository here", names=False)
    except OSError as e:
        return report(workdir, "cannot read %s/.git: %s" % (workdir, type(e).__name__), names=False)
    if not stat.S_ISDIR(gst.st_mode):
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
            print("cw-prestop: saved work to %s; %s" % (res["wip_branch"], json.dumps(
                {"untracked": res["untracked"], "untracked_more": res["untracked_more"]})))
        elif isinstance(res, NothingToPush):
            print("cw-prestop: nothing to push (the answer is from local references: the fetch failed)" if res.get("local_only")
                  else "cw-prestop: nothing to push")
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
        print(json.dumps(with_pr(workdir, run(workdir, worker))))
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
